"""快照生命周期基础设施。

生产流水线把 ``snapshot_id`` 当成物理对象的地址：每一次发布都会得到一
个新 ID，已发布对象只读，当前使用哪个对象由数据根目录下的独立指针
``current_snapshot.json`` 决定。

这里不把 ``current`` 当成一个快照 ID，也不在发布新快照时覆盖旧目录。
因此 Reader 仍然可以用旧 ID 做验证和回放，API/worker 只需要解析同一份
指针即可。旧版的 ``AQUANT_SNAPSHOT_ID`` 显式选择仍然优先，用于回放和
兼容已有部署。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


CURRENT_POINTER_FILENAME = "current_snapshot.json"
LEGACY_POINTER_FILENAMES = (
    "current_snapshot",
    "active_snapshot.json",
    "active_snapshot",
)
_SNAPSHOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class SnapshotPointerError(RuntimeError):
    """当前指针不存在、损坏或指向空 ID。"""


@dataclass(frozen=True, slots=True)
class SnapshotPointer:
    snapshot_id: str
    updated_at: str
    source: str = "file"

    def as_dict(self) -> dict[str, str]:
        return {
            "snapshot_id": self.snapshot_id,
            "updated_at": self.updated_at,
            "source": self.source,
        }


def validate_snapshot_id(snapshot_id: str) -> str:
    value = str(snapshot_id or "").strip()
    if not value or not _SNAPSHOT_ID_RE.fullmatch(value):
        raise ValueError(
            "snapshot_id must be 1-128 characters of letters, digits, '.', '_' or '-'")
    return value


def new_snapshot_id(trading_day: date | str, *, now: datetime | None = None) -> str:
    """生成每次调用都唯一的 EOD 物理 ID。

    日期保留在 ID 中便于运维检索，随机部分保证同一天重跑也绝不覆盖
    既有目录。UUID 不依赖系统时钟精度，适合并发触发被锁挡住之前的预生成。
    """

    day = trading_day.isoformat() if isinstance(trading_day, date) else str(trading_day)
    # 只接受 ISO 日期形状，避免把任意用户输入拼进路径。
    try:
        date.fromisoformat(day)
    except ValueError as exc:
        raise ValueError(f"trading_day must be YYYY-MM-DD, got {day!r}") from exc
    # ``now`` is accepted for deterministic callers/documentation; uniqueness
    # intentionally comes from uuid rather than timestamp alone.
    _ = now
    return f"snap-eod-{day}-{uuid.uuid4().hex[:16]}"


def pointer_path(data_root: str | Path) -> Path:
    return Path(data_root) / CURRENT_POINTER_FILENAME


def _read_pointer_file(path: Path) -> SnapshotPointer | None:
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SnapshotPointerError(f"cannot read current snapshot pointer {path}: {exc}") from exc
    if not text:
        raise SnapshotPointerError(f"current snapshot pointer is empty: {path}")
    try:
        raw: Any = json.loads(text)
    except json.JSONDecodeError:
        # A one-line plain-text pointer is accepted for easy manual recovery and
        # compatibility with early local deployments.
        raw = text
    if isinstance(raw, str):
        sid = raw.strip()
        updated = ""
    elif isinstance(raw, dict):
        sid = (raw.get("snapshot_id") or raw.get("snapshotId") or raw.get("id") or "").strip()
        updated = str(raw.get("updated_at") or raw.get("updatedAt") or "")
    else:
        raise SnapshotPointerError(f"invalid current snapshot pointer JSON: {path}")
    try:
        sid = validate_snapshot_id(sid)
    except ValueError as exc:
        raise SnapshotPointerError(f"invalid snapshot id in pointer {path}: {exc}") from exc
    return SnapshotPointer(snapshot_id=sid, updated_at=updated, source=str(path))


def read_current_pointer(data_root: str | Path) -> SnapshotPointer | None:
    """读取指针；缺失返回 ``None``，损坏指针显式报错。"""

    root = Path(data_root)
    primary = _read_pointer_file(pointer_path(root))
    if primary is not None:
        return primary
    for name in LEGACY_POINTER_FILENAMES:
        found = _read_pointer_file(root / name)
        if found is not None:
            return found
    return None


def record_current_snapshot(
    con: sqlite3.Connection,
    snapshot_id: str,
    *,
    updated_at: datetime | None = None,
    pointer_name: str = "current",
) -> SnapshotPointer:
    """写入迁移表中的逻辑指针。

    ``schema/003_snapshot_lifecycle.sql`` 是向后兼容的；旧库若尚未执行
    迁移会得到 sqlite 的正常 ``no such table``，调用方可继续依赖文件指针。
    """

    sid = validate_snapshot_id(snapshot_id)
    stamp = (updated_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    con.execute(
        "INSERT INTO snapshot_pointer(pointer_name,snapshot_id,updated_at) VALUES (?,?,?) "
        "ON CONFLICT(pointer_name) DO UPDATE SET snapshot_id=excluded.snapshot_id, "
        "updated_at=excluded.updated_at",
        (pointer_name, sid, stamp),
    )
    return SnapshotPointer(snapshot_id=sid, updated_at=stamp, source="sqlite")


def read_db_current_snapshot(
    con: sqlite3.Connection, *, pointer_name: str = "current"
) -> SnapshotPointer | None:
    try:
        row = con.execute(
            "SELECT snapshot_id,updated_at FROM snapshot_pointer WHERE pointer_name=?",
            (pointer_name,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    sid = validate_snapshot_id(row[0])
    return SnapshotPointer(snapshot_id=sid, updated_at=str(row[1]), source="sqlite")


def resolve_current_snapshot(
    data_root: str | Path,
    *,
    explicit_snapshot_id: str | None = None,
    connection: sqlite3.Connection | None = None,
    environ: dict[str, str] | None = None,
    legacy_default: str | None = "snap-universe",
) -> str:
    """解析一个明确的当前快照 ID。

    解析优先级为显式参数、``AQUANT_SNAPSHOT_ID``、当前指针，最后才是
    旧部署的固定合成 ID。调用方若不允许兜底可传 ``legacy_default=None``。
    """

    if explicit_snapshot_id and str(explicit_snapshot_id).strip():
        return validate_snapshot_id(str(explicit_snapshot_id))
    env = os.environ if environ is None else environ
    configured = str(env.get("AQUANT_SNAPSHOT_ID") or "").strip()
    if configured:
        return validate_snapshot_id(configured)
    pointer = read_current_pointer(data_root)
    if pointer is not None:
        return pointer.snapshot_id
    if connection is not None:
        pointer = read_db_current_snapshot(connection)
        if pointer is not None:
            return pointer.snapshot_id
    if legacy_default:
        return validate_snapshot_id(legacy_default)
    raise SnapshotPointerError(f"no current snapshot pointer under {Path(data_root)}")


def write_current_pointer(
    data_root: str | Path,
    snapshot_id: str,
    *,
    updated_at: datetime | None = None,
) -> SnapshotPointer:
    """原子替换当前指针，不触碰任何旧快照目录或数据库。"""

    sid = validate_snapshot_id(snapshot_id)
    root = Path(data_root)
    root.mkdir(parents=True, exist_ok=True)
    stamp = (updated_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    pointer = SnapshotPointer(snapshot_id=sid, updated_at=stamp)
    target = pointer_path(root)
    # NamedTemporaryFile + os.replace is atomic on the same volume on Windows
    # and POSIX. Delete=False is required before replacing an open file on Windows.
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(root), prefix=".current_snapshot-",
        suffix=".tmp", delete=False,
    ) as handle:
        temp_name = handle.name
        json.dump(pointer.as_dict(), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temp_name, target)
    except BaseException:
        try:
            Path(temp_name).unlink()
        except OSError:
            pass
        raise
    return pointer


__all__ = [
    "CURRENT_POINTER_FILENAME",
    "LEGACY_POINTER_FILENAMES",
    "SnapshotPointer",
    "SnapshotPointerError",
    "new_snapshot_id",
    "pointer_path",
    "read_current_pointer",
    "record_current_snapshot",
    "read_db_current_snapshot",
    "resolve_current_snapshot",
    "validate_snapshot_id",
    "write_current_pointer",
]
