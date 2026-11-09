"""每日流水线的护栏：并发锁与运行日志（§14.2）。

为什么这两件事单独成模块
------------------------
流水线本身只是按顺序调几个已有脚本，没什么逻辑。真正会出事的是：

  * **两次运行重叠**。定时任务偶尔会重叠（上一次跑得慢、或者被手工触发），
    而两次运行会同时写同一份采集缓存与同一个快照目录——
    上一次我在补前收时就因为残留进程互相覆盖缓存，
    排查花了很久，而症状只是"进度不动"。
  * **失败无人知**。定时任务最常见的失败模式不是崩溃，而是**静默地
    什么也没做**（例如市场休市、数据源没更新）。没有留痕就没法回答
    "昨天到底跑了没有、结果是什么"。

因此锁用"独占创建文件"（跨进程有效，且进程崩溃后文件留着——
这本身是线索），日志用 JSONL 追加（每次运行一行，可直接 grep）。
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


class PipelineBusy(RuntimeError):
    """已有一次运行在进行中。**不等待**——等下去只会让两次运行都变慢。"""

    def __init__(self, lock_path: Path, holder: str) -> None:
        super().__init__(f"流水线正在运行中（锁文件 {lock_path}，持有者 {holder}）")
        self.lock_path = lock_path
        self.holder = holder


@dataclass(frozen=True, slots=True)
class LockRecovery:
    """Result of a conservative attempt to preserve and recover an orphan lock."""

    status: str
    detail: str
    archive_path: Path | None = None
    holder: str = ""


_LOCK_SCHEMA = "aquant.pipeline_lock.v2"
_PID_NAMESPACE_RE = re.compile(r"^pid:\[\d+\]$")
_BOOT_RE = re.compile(r"^[0-9a-f-]{36}$")
_START_TICKS_RE = re.compile(r"^\d+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12,64}$", re.IGNORECASE)


def _container_id() -> str | None:
    """Read the stable Docker ID from cgroup or Docker's default hostname."""
    try:
        cgroup = Path("/proc/self/cgroup").read_text(encoding="ascii")
    except OSError:
        cgroup = ""
    match = re.search(r"(?<![0-9a-f])([0-9a-f]{64})(?![0-9a-f])", cgroup, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    hostname = os.environ.get("HOSTNAME", "").strip().lower()
    if Path("/.dockerenv").exists() and _CONTAINER_ID_RE.fullmatch(hostname):
        return hostname
    return None


def _pid_namespace_id() -> str | None:
    try:
        value = os.readlink("/proc/self/ns/pid")
    except OSError:
        return None
    return value if _PID_NAMESPACE_RE.fullmatch(value) else None


def _boot_id() -> str | None:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip().lower()
    except OSError:
        return None
    return value if _BOOT_RE.fullmatch(value) else None


def _process_start_ticks(pid: int) -> str | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        # Field 22 follows the closing ')' of the process name.
        value = text[text.rfind(")") + 1:].split()[19]
    except (OSError, IndexError):
        return None
    return value if _START_TICKS_RE.fullmatch(value) else None


def _lock_file_id(path: Path) -> str:
    canonical = os.path.normcase(str(path.resolve(strict=False)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _owner_line(path: Path, *, pid: int | None = None,
                at: datetime | None = None) -> str:
    owner_pid = os.getpid() if pid is None else int(pid)
    stamp = (at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    return (f"schema={_LOCK_SCHEMA} pid={owner_pid} at={stamp} "
            f"pidns={_pid_namespace_id() or 'unknown'} boot={_boot_id() or 'unknown'} "
            f"start={_process_start_ticks(owner_pid) or 'unknown'} "
            f"container={_container_id() or 'unknown'} "
            f"lock={_lock_file_id(path)}")


def _parse_lock_owner(text: str) -> tuple[dict[str, str] | None, str]:
    fields: dict[str, str] = {}
    try:
        for part in text.strip().split():
            key, value = part.split("=", 1)
            if key in fields:
                return None, "锁记录包含重复字段"
            fields[key] = value
    except ValueError:
        return None, "锁记录格式无法解析"
    if fields.get("schema") != _LOCK_SCHEMA:
        return None, "旧格式或未知格式锁缺少 PID 命名空间/进程身份；保留供人工核对"
    required = {"pid", "at", "pidns", "boot", "start", "container", "lock"}
    if not required.issubset(fields):
        return None, "锁记录缺少 owner 身份字段；保留供人工核对"
    if not fields["pid"].isdigit() or int(fields["pid"]) <= 0:
        return None, "锁记录 PID 无效"
    try:
        stamp = datetime.fromisoformat(fields["at"])
    except ValueError:
        return None, "锁记录时间格式无效"
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        return None, "锁记录时间没有时区"
    if (not _PID_NAMESPACE_RE.fullmatch(fields["pidns"])
            or not _BOOT_RE.fullmatch(fields["boot"])
            or not _START_TICKS_RE.fullmatch(fields["start"])
            or not _CONTAINER_ID_RE.fullmatch(fields["container"])
            or not _SHA256_RE.fullmatch(fields["lock"])):
        return None, "锁记录的 owner 身份格式无效；保留供人工核对"
    return fields, ""


def _try_lock_descriptor(fd: int) -> tuple[bool, str]:
    """Take a nonblocking OS lock; it is released automatically on process exit."""

    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True, ""
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True, ""
    except (ImportError, OSError) as exc:
        if isinstance(exc, OSError) and exc.errno in {
                errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EWOULDBLOCK}:
            return False, "另一个进程仍持有操作系统锁"
        return False, f"无法验证操作系统锁：{type(exc).__name__}: {exc}"


def _pid_exists(pid: int) -> bool | None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        return None
    return True


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _open_lease(path: Path, *, create: bool) -> tuple[int | None, str]:
    """Hold a stable sidecar across metadata archival and writer acquisition."""
    lease = path.with_name(path.name + ".lease")
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT
    try:
        fd = os.open(lease, flags, 0o600)
    except FileNotFoundError:
        return None, "锁租约不存在"
    except OSError as exc:
        return None, f"无法打开锁租约：{type(exc).__name__}: {exc}"
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or not _same_file(opened, lease.lstat()):
            os.close(fd)
            return None, "锁租约不是稳定的普通文件"
        if create and opened.st_size == 0:
            os.write(fd, b"\0")
        return fd, ""
    except OSError as exc:
        os.close(fd)
        return None, f"无法核对锁租约：{type(exc).__name__}: {exc}"


def recover_orphaned_pipeline_lock(path: str | Path) -> LockRecovery:
    """Archive a dead v2 lock from this container; keep legacy locks."""

    target = Path(path)
    try:
        before = target.lstat()
    except FileNotFoundError:
        return LockRecovery("MISSING", "锁文件不存在")
    except OSError as exc:
        return LockRecovery("ERROR", f"无法检查锁文件：{type(exc).__name__}: {exc}")
    if not stat.S_ISREG(before.st_mode):
        return LockRecovery("UNVERIFIED", "锁路径不是普通文件；保留供人工核对")

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, os.O_RDWR | nofollow)
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            return LockRecovery("MISSING", "锁文件已被其他恢复者移除")
        return LockRecovery("ERROR", f"无法打开锁文件：{type(exc).__name__}: {exc}")
    lease_fd: int | None = None
    try:
        opened = os.fstat(fd)
        if not _same_file(before, opened) or not stat.S_ISREG(opened.st_mode):
            return LockRecovery("UNVERIFIED", "锁文件在检查期间发生变化；未修改")
        text = os.read(fd, 8192).decode("utf-8", errors="replace")
        owner, issue = _parse_lock_owner(text)
        if owner is None:
            return LockRecovery("UNVERIFIED", issue, holder=text.strip())
        if owner["lock"] != _lock_file_id(target):
            return LockRecovery("UNVERIFIED", "锁路径归属不匹配；保留锁", holder=text.strip())

        pid = int(owner["pid"])
        container, pidns, boot = _container_id(), _pid_namespace_id(), _boot_id()
        if container is None or pidns is None or boot is None:
            return LockRecovery("UNVERIFIED", "当前容器身份不可验证；保留锁", holder=text.strip())
        if owner["container"] != container:
            return LockRecovery("UNVERIFIED", "锁属于另一容器；保留锁", holder=text.strip())

        same_namespace = owner["pidns"] == pidns
        if same_namespace and owner["boot"] == boot:
            exists = _pid_exists(pid)
            if exists is None:
                return LockRecovery("UNVERIFIED", "无法判断锁 PID 是否存活；保留锁",
                                    holder=text.strip())
            if exists:
                start = _process_start_ticks(pid)
                if start is None:
                    return LockRecovery("UNVERIFIED", "锁 PID 存在但进程代次不可读；保留锁",
                                        holder=text.strip())
                if start == owner["start"]:
                    return LockRecovery("ACTIVE", "锁持有进程仍存活", holder=text.strip())
                # A different start tick means this PID was reused.

        lease_fd, lease_issue = _open_lease(target, create=False)
        if lease_fd is None:
            if lease_issue != "锁租约不存在" or not same_namespace:
                return LockRecovery("UNVERIFIED", lease_issue, holder=text.strip())
        else:
            acquired, detail = _try_lock_descriptor(lease_fd)
            if not acquired:
                status = "ACTIVE" if "仍持有" in detail else "ERROR"
                return LockRecovery(status, detail, holder=text.strip())

        current = target.lstat()
        if not _same_file(opened, current):
            return LockRecovery("UNVERIFIED", "锁文件在恢复期间被替换；未修改",
                                holder=text.strip())
        os.close(fd)
        fd = -1
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archive = target.with_name(
            f"{target.name}.orphaned-{stamp}-pid-{pid}-{uuid.uuid4().hex[:8]}"
        )
        os.link(target, archive, follow_symlinks=False)
        try:
            archived = archive.lstat()
            current = target.lstat()
            if not _same_file(opened, archived) or not _same_file(opened, current):
                archive.unlink(missing_ok=True)
                return LockRecovery("UNVERIFIED", "锁在归档竞争中发生变化；未移除当前锁",
                                    holder=text.strip())
            target.unlink()
        except OSError as exc:
            try:
                if archive.exists() and _same_file(opened, archive.lstat()):
                    archive.unlink()
            except OSError:
                pass
            return LockRecovery("ERROR", f"无法完成孤儿锁归档：{type(exc).__name__}: {exc}",
                                holder=text.strip())
        return LockRecovery("RECOVERED", "孤儿锁已保留归档并移除原路径",
                            archive_path=archive, holder=text.strip())
    except FileNotFoundError:
        return LockRecovery("MISSING", "锁文件已被其他恢复者移除")
    except (OSError, UnicodeError) as exc:
        return LockRecovery("ERROR", f"锁恢复失败：{type(exc).__name__}: {exc}")
    finally:
        if fd >= 0:
            os.close(fd)
        if lease_fd is not None:
            os.close(lease_fd)


class PipelineLock:
    """跨进程互斥，用独占创建和持有到退出的 OS 文件锁。

    崩溃后只自动归档能证明属于当前运行环境、且已无 OS 锁与原进程的
    v2 锁。旧版 ``pid=... at=...`` 不含 PID 命名空间信息，不能只因本
    容器看不到那个 PID 就判断无人写入；这种锁保留并带诊断信息。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._held = False
        self._fd: int | None = None
        self._lock_identity: os.stat_result | None = None

    def __enter__(self) -> "PipelineLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except FileExistsError as exc:
                recovery = recover_orphaned_pipeline_lock(self.path)
                if recovery.status in {"MISSING", "RECOVERED"} and attempt == 0:
                    continue
                holder = recovery.holder or "未知"
                detail = recovery.detail
                if detail:
                    holder = f"{holder}；锁检查：{detail}" if holder != "未知" else detail
                raise PipelineBusy(self.path, holder) from exc

            lock_identity: os.stat_result | None = None
            lease_fd: int | None = None
            try:
                payload = (_owner_line(self.path) + "\n").encode("utf-8")
                written = 0
                while written < len(payload):
                    written += os.write(fd, payload[written:])
                os.fsync(fd)
                lock_identity = os.fstat(fd)
                os.close(fd)
                fd = -1

                lease_fd, detail = _open_lease(self.path, create=True)
                if lease_fd is None:
                    raise PipelineBusy(self.path, detail)
                acquired, detail = _try_lock_descriptor(lease_fd)
                if not acquired:
                    raise PipelineBusy(self.path, detail)
                self._fd = lease_fd
                self._lock_identity = lock_identity
                self._held = True
                return self
            except BaseException:
                try:
                    current = self.path.lstat()
                    if lock_identity is not None and _same_file(lock_identity, current):
                        self.path.unlink()
                except OSError:
                    pass
                if fd >= 0:
                    os.close(fd)
                if lease_fd is not None:
                    os.close(lease_fd)
                raise
        # The retry loop can only end after an unexpected file-system race.
        raise PipelineBusy(self.path, "锁在恢复期间被其他进程重新创建")

    def __exit__(self, *exc: object) -> None:
        if self._held and self._fd is not None:
            # Releasing the lease before unlinking still leaves the O_EXCL path
            # occupied, so another writer cannot pass between these operations.
            os.close(self._fd)
            self._fd = None
            try:
                current = self.path.lstat()
                if (self._lock_identity is not None
                        and _same_file(self._lock_identity, current)):
                    self.path.unlink()
            except FileNotFoundError:
                pass
            self._held = False


@dataclass(slots=True)
class RunRecord:
    """一次运行的留痕。字段刻意少而实：能回答"跑了没有、成没成、为什么"。"""

    trading_day: str
    snapshot_id: str
    outcome: str                      # PUBLISHED / SKIPPED / FAILED
    steps: list[dict] = field(default_factory=list)
    reason: str | None = None
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    #: 采集缓存里**实际**的最后一个交易日（不是"今天"）。
    #:
    #: 记它是为了回答一个运维上很容易被忽略的问题：快照落后了没有。
    #: 光看"上次跑成功是什么时候"不够——天天跑成功但数据源没更新，
    #: 快照照样停在几天前，而界面上一切正常。
    data_last_day: str | None = None
    #: 谁发起的一次运行：manual（命令行/界面按钮）或 scheduler（到点）。
    #: 两者都合法，但**排查时的下一步不一样**：前者要问"为什么点了"，
    #: 后者要问"为什么到点没跑/跑了没成"。
    source: str = "manual"

    def as_dict(self) -> dict:
        return asdict(self)


class RunLog:
    """JSONL 运行日志。每次运行追加一行。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: RunRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.as_dict(), ensure_ascii=False) + "\n")

    def entries(self, limit: int = 20) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines[-limit:] if line.strip()]

    def last_successful_day(self) -> str | None:
        """最近一次成功发布的交易日。用来判断"今天是不是已经跑过了"。"""

        for entry in reversed(self.entries(limit=200)):
            if entry.get("outcome") == "PUBLISHED":
                return entry.get("trading_day")
        return None
