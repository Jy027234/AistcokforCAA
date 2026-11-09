"""每日流水线的调度 worker（独立进程，长驻）。

它为什么不是 API 里的一个线程
============================
见 `src/aquant/operations/scheduler.py` 的模块说明：API 会重启、采集会与
请求争用连接、多实例会各自触发。这里只强调一条与本文件直接相关的：
**调度器必须能回答"这一次跑了没有"**，而这件事只有在 worker 进程
自己的生命周期里才说得清。

它做什么
========
循环三件事，每一轮都很短：

  1. 到点了吗（`is_due`）？到点就登记一条 scheduler 请求；
  2. 有 PENDING 请求（可能来自界面按钮）吗？有就领取并执行；
  3. 执行 = 起一个子进程跑 `tools/daily_run.py`，**与手工运行同一条路径**
     （包括并发锁、休市判断、留痕、告警），然后把结果写回摘要表。

刻意不做的事
============
  * **不 import 领域计算**：worker 是运维件，不是产品逻辑。它对流水线的
    全部知识就是"怎么起它、怎么看退出码"。
  * **不用线程池并发跑多次**：同一时刻只允许一次运行。`daily_run` 自己
    有锁，但让 worker 也串行可以省掉一类"两次运行交错"的排查。
  * 一般失败保持终态并留痕。仅当 RESEARCH_SIMULATION 的同一日定时任务
    失败记录与本次证据共同确认两次 BaoStock 登录接收超时、一次已清理的
    登录阶段 10002007 网络接收失败，或当前绑定池中每只未完成证券都有明确
    的 10002007 查询传输失败时，且调度仍启用、已间隔至少 15 分钟、没有待处理
    请求或日更锁，最多登记两次同日恢复请求。
    晚间登录故障的最后一次恢复最早于北京时间 21:45 登记。
    `requested_by` 的日期和序号标记阻止重启后重复登记；不循环重试。

用法
====
    python tools/scheduler_worker.py                     # 长驻，按配置到点运行
    python tools/scheduler_worker.py --once              # 只处理一轮（给测试与 cron 用）
    python tools/scheduler_worker.py --now               # 立刻执行一次当前待处理请求
    python tools/scheduler_worker.py --data-dir <dir>    # 指定快照数据目录

退出码：0 正常结束；2 数据目录不可用；1 本轮执行失败（`--once`/`--now`）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, time as clock_time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.data.db import apply_migrations, connect, write_tx  # noqa: E402
from aquant.operations import scheduler  # noqa: E402
from aquant.operations.pipeline import recover_orphaned_pipeline_lock  # noqa: E402

#: 仓库里默认的快照数据目录。与 `daily_run` 的输出目录、API 的
#: `AQUANT_DATA_DIR` 指向同一个地方——三者不一致的话，界面读的是
#: 一份库、调度写的是另一份，而两边都"正常"。
DEFAULT_DATA_DIR = ROOT / "deploy" / "universe-snapshot"

#: 单次运行的超时。**必须有**：采集卡住时 worker 不能跟着一起卡死——
#: 那会让"下一次运行"永远等不到，而且没有任何东西会报错。
# 14日实际采集约19分钟，15日复用原证据的完整恢复仍用1381.2秒
# （其中正式财务冷读1041.3秒）。保留全部正常门禁，给整条日更一小时预算。
RUN_TIMEOUT_SECONDS = 60 * 60
# `tick` keeps the historical `_run_pipeline` call shape so callers/tests that
# replace it remain compatible; the real subprocess still receives the exact
# data root through this short-lived worker-local context.
_PIPELINE_DATA_DIR: Path | None = None
DAILY_TIMEOUT_RETRY_REQUESTED_BY = "scheduler:baostock-login-timeout-retry"
DAILY_TIMEOUT_RETRY_DELAY = timedelta(minutes=15)
DAILY_TIMEOUT_RETRY_LIMIT = 2
DAILY_TIMEOUT_RETRY_KIND = "BAOSTOCK_LOGIN_TIMEOUT"
DAILY_CAPTURE_RETRY_KIND = "BAOSTOCK_CAPTURE_TRANSPORT_FAILURE"
DAILY_LOGIN_RECEIVE_RETRY_KIND = "BAOSTOCK_LOGIN_RECEIVE_FAILURE"
EVENING_LOGIN_RETRY_WINDOW_START = clock_time(20, 30)
EVENING_LOGIN_FINAL_RETRY_NOT_BEFORE = clock_time(21, 45)


def log(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def _worker_id() -> str:
    return f"scheduler-{uuid.uuid4().hex[:8]}"


def _resolve_interpreter(configured: str) -> Path:
    """用哪个解释器跑流水线。

    配置优先；没配就用 `sys.executable`。**默认值可能是错的**——
    流水线依赖 baostock 与 pytest，而启动 worker 的那个 python 未必装了它们。
    因此这里把选中的解释器打出来，让"用错了"至少是可见的。
    """

    if configured.strip():
        return Path(configured.strip())
    return Path(sys.executable)


def _preflight(interpreter: Path, data_dir: Path) -> list[str]:
    """跑之前能查出来的问题，就不要等跑了一半才发现。

    返回问题列表（空 = 没问题）。**不阻止运行**——流水线自己会报错并告警，
    这里只是把"解释器不存在"这种确定性失败提前说出来。
    """

    problems: list[str] = []
    if not interpreter.exists():
        problems.append(f"解释器不存在：{interpreter}")
    if not (ROOT / "tools" / "daily_run.py").exists():
        problems.append(f"找不到流水线脚本：{ROOT / 'tools' / 'daily_run.py'}")
    if os.environ.get("AQUANT_DAILY_MODE") == "RESEARCH_SIMULATION":
        for name in ("daily_research_run.py", "capture_research_quotes.py",
                     "refresh_research_publication.py", "capture_research_simulation_evidence.py"):
            if not (ROOT / "tools" / name).exists():
                problems.append(f"缺少研究/模拟日更脚本：{name}")
    elif not (ROOT / "deploy" / "agentctl-q0" / "universe-bars.json").exists():
        problems.append("缺少采集缓存 universe-bars.json（先跑 tools/collect_universe.py）")
    if not data_dir.exists():
        problems.append(f"数据目录不存在：{data_dir}")
    return problems


def _run_pipeline(*, interpreter: Path, window_start: str, source: str,
                  json_out: Path, data_dir: Path | None = None) -> tuple[int, str]:
    """起子进程跑一次流水线。返回 (exit_code, 输出尾巴)。"""

    resolved_data_dir = data_dir or _PIPELINE_DATA_DIR
    args = [str(interpreter), str(ROOT / "tools" / "daily_run.py"),
            "--source", source, "--window-start", window_start,
            "--json-out", str(json_out)]
    if resolved_data_dir is not None:
        args[4:4] = ["--data-dir", str(resolved_data_dir)]
    if os.environ.get("AQUANT_DAILY_MODE") == "RESEARCH_SIMULATION":
        args.append("--skip-if-done")
    log("执行：" + " ".join(args))
    started = time.time()
    try:
        proc = subprocess.run(args, cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True,
                              timeout=RUN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return 1, f"超时（>{RUN_TIMEOUT_SECONDS}s）后被终止"
    seconds = time.time() - started
    tail = "\n".join((proc.stdout or "").strip().splitlines()[-6:])
    log(f"退出码 {proc.returncode}（{seconds:.1f}s）")
    for line in (proc.stdout or "").strip().splitlines()[-6:]:
        print("      " + line, flush=True)
    if proc.stderr:
        print("      stderr: " + proc.stderr.strip().splitlines()[-1], flush=True)
    return proc.returncode, tail


def _load_record(json_out: Path) -> dict:
    if not json_out.exists():
        return {}
    try:
        return json.loads(json_out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _aware_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _retry_marker_for_day(day: str, attempt: int = 1) -> str:
    return f"{DAILY_TIMEOUT_RETRY_REQUESTED_BY}:{day}:{attempt}"


def _next_retry_marker(con, day: str) -> tuple[str, int] | None:
    prefix = f"{DAILY_TIMEOUT_RETRY_REQUESTED_BY}:{day}:"
    rows = con.execute("SELECT requested_by FROM pipeline_run_request "
                       "WHERE substr(requested_by,1,?)=?",
                       (len(prefix), prefix)).fetchall()
    used: list[int] = []
    valid_attempts = {str(number): number
                       for number in range(1, DAILY_TIMEOUT_RETRY_LIMIT + 1)}
    for row in rows:
        suffix = row["requested_by"][len(prefix):]
        if suffix not in valid_attempts:
            return None
        used.append(valid_attempts[suffix])
    if (len(used) != len(set(used))
            or sorted(used) != list(range(1, len(used) + 1))):
        return None
    attempt = len(used) + 1
    if attempt > DAILY_TIMEOUT_RETRY_LIMIT:
        return None
    return _retry_marker_for_day(day, attempt), attempt


def _capture_retry_hint_is_current(record: dict) -> bool:
    hint = record.get("retryableFailure")
    if not isinstance(hint, dict):
        return False
    if hint.get("kind") == DAILY_TIMEOUT_RETRY_KIND:
        return True
    if hint.get("kind") == DAILY_LOGIN_RECEIVE_RETRY_KIND:
        attempt_dir = record.get("artifactDirectory")
        evidence_path = hint.get("loginAttemptsPath")
        expected_hash = hint.get("loginAttemptsSha256")
        if (not isinstance(attempt_dir, str) or not attempt_dir
                or not isinstance(evidence_path, str) or not evidence_path
                or not isinstance(expected_hash, str) or not expected_hash.startswith("sha256:")):
            return False
        try:
            root = Path(attempt_dir).resolve(strict=True)
            evidence_dir = Path(attempt_dir) / "simulation-evidence"
            if evidence_dir.is_symlink():
                return False
            evidence_root = evidence_dir.resolve(strict=True)
            expected_path = evidence_root / "login-attempts.json"
            path = Path(evidence_path)
            if (not evidence_root.is_relative_to(root) or path.is_symlink()
                    or path.resolve(strict=True) != expected_path
                    or not expected_path.is_file()):
                return False
            raw = expected_path.read_bytes()
            if "sha256:" + hashlib.sha256(raw).hexdigest() != expected_hash:
                return False
            packet = json.loads(raw)
            attempts = packet.get("attempts") if isinstance(packet, dict) else None
            if (not isinstance(packet, dict)
                    or packet.get("schema") != "aquant.baostock_login_attempts.v1"
                    or packet.get("provider") != "baostock"
                    or packet.get("status") != "FAILED"
                    or not isinstance(attempts, list) or len(attempts) != 1):
                return False
            attempt = attempts[0]
            if not isinstance(attempt, dict):
                return False
            cleanup = attempt.get("cleanup") if isinstance(attempt, dict) else None
            return bool(
                attempt.get("attempt") == 1
                and attempt.get("status") == "failed"
                and attempt.get("retryable") is False
                and attempt.get("error_type") == "BaostockUnavailable"
                and attempt.get("error_code") == "10002007"
                and isinstance(attempt.get("error"), str)
                and "10002007" in attempt["error"]
                and any(token in attempt["error"].lower() for token in ("receive", "recv", "接收"))
                and isinstance(attempt.get("diagnostic"), str)
                and ("BaoStock socket closed before response completed" in attempt["diagnostic"]
                     or "接收数据异常" in attempt["diagnostic"])
                and any(token in attempt["diagnostic"].lower() for token in ("receive", "recv", "接收"))
                and isinstance(cleanup, dict)
                and cleanup.get("socket_closed") is True
                and cleanup.get("context_cleared") is True)
        except (OSError, ValueError, TypeError, RuntimeError):
            return False
    if hint.get("kind") != DAILY_CAPTURE_RETRY_KIND:
        return False
    missing = hint.get("missingInstrumentIds")
    attempt_dir = record.get("artifactDirectory")
    if (not isinstance(attempt_dir, str) or not attempt_dir
            or not isinstance(hint.get("checkpointPath"), str)
            or not isinstance(hint.get("checkpointSha256"), str)
            or not hint["checkpointSha256"].startswith("sha256:")
            or not isinstance(missing, list) or not missing
            or any(not isinstance(iid, str) or not iid for iid in missing)
            or len(missing) != len(set(missing))):
        return False
    try:
        attempt_root = Path(attempt_dir).resolve(strict=True)
        evidence_dir = Path(attempt_dir) / "simulation-evidence"
        if evidence_dir.is_symlink():
            return False
        root = evidence_dir.resolve(strict=True)
        if not root.is_relative_to(attempt_root):
            return False
        checkpoint = Path(hint["checkpointPath"])
        if checkpoint.is_symlink() or checkpoint.resolve(strict=True) != root / "checkpoint.json":
            return False
        digest = "sha256:" + hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    except (OSError, ValueError, RuntimeError):
        return False
    return digest == hint["checkpointSha256"]


def _retry_candidate(con, *, root: Path, schedule: scheduler.RunSchedule,
                     now: datetime) -> tuple[str, str, int] | None:
    """Return (market day, requested_by marker) only for a current matching failure."""

    if (os.environ.get("AQUANT_DAILY_MODE") != "RESEARCH_SIMULATION"
            or not schedule.enabled or now.tzinfo is None or now.utcoffset() is None):
        return None
    market_now = now.astimezone(scheduler.MARKET_TIMEZONE)
    market_day = market_now.date().isoformat()
    latest = scheduler.last_run(con)
    record = _load_record(root / "deploy" / "agentctl-q0" / "last-run.json")
    if not isinstance(record, dict) or latest is None:
        return None
    day = record.get("trading_day")
    if (day != market_day or record.get("outcome") != "FAILED"
            or record.get("source") != "scheduler"
            or record.get("mode") != "RESEARCH_SIMULATION"
            or latest.get("outcome") != "FAILED"
            or latest.get("source") != "scheduler"
            or latest.get("tradingDay") != day
            or latest.get("finishedAt") != record.get("finished_at")
            or not isinstance(record.get("retryableFailure"), dict)):
        return None
    if not _capture_retry_hint_is_current(record):
        return None
    try:
        parsed_day = datetime.fromisoformat(day).date()
    except (TypeError, ValueError):
        return None
    if parsed_day.isoformat() != day:
        return None
    started = _aware_utc(record.get("started_at"))
    finished = _aware_utc(record.get("finished_at"))
    moment = now.astimezone(timezone.utc)
    if (started is None or finished is None or finished < started or finished > moment
            or started.astimezone(scheduler.MARKET_TIMEZONE).date() != parsed_day
            or finished.astimezone(scheduler.MARKET_TIMEZONE).date() != parsed_day
            or moment - finished < DAILY_TIMEOUT_RETRY_DELAY):
        return None
    request_id = latest.get("requestId")
    if not request_id:
        return None
    request = con.execute(
        "SELECT source,status FROM pipeline_run_request WHERE request_id=?",
        (request_id,)).fetchone()
    if request is None or request["source"] != "scheduler" or request["status"] != "FAILED":
        return None
    next_marker = _next_retry_marker(con, day)
    if next_marker is None:
        return None
    marker, attempt = next_marker
    if attempt == 2:
        failure_kind = record["retryableFailure"]["kind"]
        failed_local_time = started.astimezone(scheduler.MARKET_TIMEZONE).time()
        if (failure_kind in (DAILY_TIMEOUT_RETRY_KIND, DAILY_LOGIN_RECEIVE_RETRY_KIND)
                and failed_local_time >= EVENING_LOGIN_RETRY_WINDOW_START
                and market_now.time() < EVENING_LOGIN_FINAL_RETRY_NOT_BEFORE):
            return None
        prior_marker = _retry_marker_for_day(day, 1)
        prior = con.execute("SELECT request_id,status FROM pipeline_run_request "
                            "WHERE requested_by=?", (prior_marker,)).fetchone()
        if (prior is None or prior["status"] != "FAILED"
                or latest.get("requestId") != prior["request_id"]):
            return None
    if scheduler.pending_request(con) is not None:
        return None
    lock_path = root / "deploy" / "agentctl-q0" / "daily-run.lock"
    lock = recover_orphaned_pipeline_lock(lock_path)
    if lock.status not in {"MISSING", "RECOVERED"}:
        log(f"有限重试暂不登记：日更锁未能安全恢复（{lock.detail}）")
        return None
    if lock.status == "RECOVERED":
        log(f"已保留并恢复孤儿日更锁：{lock.archive_path}")
    return day, marker, attempt


def _request_same_day_timeout_retry(con, *, root: Path,
                                    now: datetime) -> str | None:
    """Atomically deduplicate and register one dated request through the worker path."""

    with write_tx(con):
        candidate = _retry_candidate(
            con, root=root, schedule=scheduler.load_schedule(con), now=now)
        if candidate is None:
            return None
        day, marker, attempt = candidate
        request_id = "run_" + uuid.uuid4().hex[:16]
        reason = f"同日第 {attempt} 次有限恢复：{day}"
        con.execute(
            "INSERT INTO pipeline_run_request (request_id,source,reason,requested_by,"
            "requested_at,status) VALUES (?,?,?,?,?,'PENDING')",
            (request_id, "scheduler", reason, marker,
             now.astimezone(timezone.utc).isoformat()))
    return request_id


def tick(con, *, worker_id: str, data_dir: Path, schedule: scheduler.RunSchedule,
         now: datetime, dry_run: bool = False) -> str:
    """处理一轮。返回这一步做了什么（给日志与测试断言用）。

    返回值为下面之一：`idle` / `fired` / `busy` / `ran:OK` / `ran:FAILED`。
    """

    # 到点判断之外还要看一件事：**已经有未完成的请求时不再登记**。
    #
    # 少了这一条，worker 会在"已到点、今天还没跑完"的每一轮里重复登记
    # （request_run 幂等所以只是同一条），现象是日志里刷满"到点：登记运行请求"，
    # 而真正该做的事（领取并执行）永远排在它后面。
    pending = scheduler.pending_request(con)
    if pending is None and scheduler.is_due(schedule, now=now,
                                            last_fired_for=scheduler.last_run_day(con)):
        if dry_run:
            return "fired"
        request_id = scheduler.request_run(
            con, source="scheduler", requested_by="scheduler",
            reason=f"到点触发 {schedule.run_at_local}", now=now)
        log(f"到点：登记运行请求 {request_id}")
        return "fired"

    if pending is None and _retry_candidate(con, root=ROOT, schedule=schedule, now=now):
        if dry_run:
            return "fired"
        request_id = _request_same_day_timeout_retry(con, root=ROOT, now=now)
        if request_id is not None:
            log(f"同日 BaoStock 瞬断恢复：登记一次 scheduler 请求 {request_id}")
            return "fired"

    claimed = scheduler.claim_next(con, worker_id=worker_id, now=now)
    if claimed is None:
        return "idle"

    interpreter = _resolve_interpreter(schedule.interpreter)
    problems = _preflight(interpreter, data_dir)
    if problems:
        detail = "；".join(problems)
        log("预检未通过：" + detail)
        finished_at = scheduler.market_now()
        scheduler.finish_request(con, request_id=claimed["request_id"],
                                 status="FAILED", exit_code=2, detail=detail,
                                 now=finished_at, record={"outcome": "FAILED", "reason": detail})
        return "ran:FAILED"

    json_out = ROOT / "deploy" / "agentctl-q0" / "last-run.json"
    if json_out.exists():
        json_out.unlink()
    global _PIPELINE_DATA_DIR
    previous_data_dir = _PIPELINE_DATA_DIR
    _PIPELINE_DATA_DIR = data_dir
    pipeline_error: Exception | None = None
    try:
        code, tail = _run_pipeline(
            interpreter=interpreter,
            window_start=schedule.window_start or "2026-06-22",
            source=claimed["source"], json_out=json_out)
    except Exception as exc:
        pipeline_error = exc
        code, tail = 1, f"流水线执行异常：{type(exc).__name__}: {exc}"
        log(tail)
    finally:
        _PIPELINE_DATA_DIR = previous_data_dir
    record = _load_record(json_out)
    status = "DONE" if code == 0 else "FAILED"
    if pipeline_error is not None:
        record.update({
            "trading_day": record.get("trading_day")
            or now.astimezone(scheduler.MARKET_TIMEZONE).date().isoformat(),
            "snapshot_id": record.get("snapshot_id") or "pending",
            "outcome": "FAILED",
            "reason": tail,
            "started_at": record.get("started_at")
            or now.astimezone(timezone.utc).isoformat(),
            "source": record.get("source") or claimed["source"],
        })
    elif not record:
        record = {
            "trading_day": now.astimezone(scheduler.MARKET_TIMEZONE).date().isoformat(),
            "snapshot_id": "pending",
            "outcome": status,
            "reason": tail or None,
            "started_at": now.astimezone(timezone.utc).isoformat(),
            "source": claimed["source"],
        }
    # A pipeline record carries its real completion timestamp. If the child
    # failed before producing one (timeout/startup exception), use the current
    # worker clock rather than the tick's frozen request-claim timestamp.
    finished_at = _aware_utc(record.get("finished_at")) or scheduler.market_now()
    scheduler.finish_request(con, request_id=claimed["request_id"], status=status,
                             exit_code=code, detail=tail or record.get("reason") or "",
                             now=finished_at, record=record)
    return f"ran:{status}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=None,
                    help="快照数据目录（含 meta.sqlite）。默认取配置，"
                         "再取 AQUANT_DATA_DIR，最后用仓库默认。")
    ap.add_argument("--once", action="store_true",
                    help="只处理一轮就退出（cron / 测试用）")
    ap.add_argument("--now", action="store_true",
                    help="不等配置的时间：登记一条运行请求并立刻处理（"
                         "与界面上的「立刻运行一次」是同一条通道）")
    ap.add_argument("--interval", type=float, default=20.0,
                    help="轮询间隔秒数（长驻模式）")
    ap.add_argument("--interpreter", default=None,
                    help="覆盖数据库中的流水线解释器（容器内使用）。"
                         "宿主常驻任务通常不传，继续使用设置页保存的解释器。")
    ap.add_argument("--dry-run", action="store_true",
                    help="只说会做什么，不登记请求、不执行")
    args = ap.parse_args()

    data_dir = Path(args.data_dir) if args.data_dir else None
    if data_dir is None:
        configured = None
        # 先读一次配置来拿 data_dir：数据目录本身存在配置里，
        # 而配置又在数据目录的库里——鸡生蛋。因此用一个**引导目录**：
        # 环境变量优先，其次是仓库默认，读了配置之后再切换。
        boot = Path(DEFAULT_DATA_DIR)
        env_dir = None

        if os.environ.get("AQUANT_DATA_DIR"):
            env_dir = Path(os.environ["AQUANT_DATA_DIR"])
            boot = env_dir
        if not (boot / "meta.sqlite").is_file():
            print(f"数据目录不可用（没有 meta.sqlite）：{boot}")
            print("用 --data-dir 指定，或先跑 python tools/daily_run.py 建一份快照")
            return 2
        con0 = connect(boot / "meta.sqlite")
        apply_migrations(con0)
        schedule0 = scheduler.load_schedule(con0)
        con0.close()
        if schedule0.data_dir.strip():
            data_dir = Path(schedule0.data_dir.strip())
        else:
            data_dir = boot
    if not (data_dir / "meta.sqlite").is_file():
        print(f"数据目录不可用（没有 meta.sqlite）：{data_dir}")
        return 2

    con = connect(data_dir / "meta.sqlite")
    apply_migrations(con)

    # 启动时把上次崩在 CLAIMED 的请求放回 PENDING。**只在启动时做**：
    # 循环里做会把另一个正常运行的 worker 正在跑的活抢回来。
    recovered = scheduler.startup_recovery(con)
    if recovered:
        log(f"启动恢复：{recovered} 条 CLAIMED 请求放回 PENDING")

    worker_id = _worker_id()
    schedule = scheduler.load_schedule(con)
    interpreter = _resolve_interpreter(args.interpreter or schedule.interpreter)
    log(f"worker {worker_id}｜数据目录 {data_dir}")
    log(f"调度：{'启用' if schedule.enabled else '停用'}"
        f"｜{schedule.run_at_local}"
        f"｜{'周一至周五' if schedule.weekdays_only else '每天'}"
        f"｜解释器 {interpreter}")

    if args.now:
        request_id = scheduler.request_run(
            con, source="manual", requested_by="cli",
            reason="--now 立刻执行一次")
        log(f"登记运行请求 {request_id}")

    def one_round() -> str:
        schedule_now = scheduler.load_schedule(con)
        if args.interpreter:
            from dataclasses import replace
            schedule_now = replace(schedule_now, interpreter=args.interpreter)
        return tick(con, worker_id=worker_id, data_dir=data_dir,
                    schedule=schedule_now, now=scheduler.market_now(),
                    dry_run=args.dry_run)

    if args.once or args.now:
        result = one_round()
        log("本轮：" + result)
        con.close()
        # idle/fired 都算正常结束；ran:DONE 也正常；只有失败才非零。
        return 0 if result in ("idle", "fired") or result.endswith("DONE") else 1

    started_at = scheduler.market_now()
    heartbeat_stop = threading.Event()

    def write_heartbeat(status: str = "RUNNING") -> None:
        try:
            scheduler.write_worker_heartbeat(
                data_dir,
                worker_id=worker_id,
                pid=os.getpid(),
                started_at=started_at,
                interval_seconds=args.interval,
                status=status,
            )
        except OSError as exc:
            # 心跳写失败不应杀死真正的数据流水线，但控制台必须留下原因。
            log(f"worker 心跳写入失败：{exc}")

    def heartbeat_loop() -> None:
        write_heartbeat()
        while not heartbeat_stop.wait(max(1.0, min(args.interval, 20.0))):
            write_heartbeat()

    heartbeat_thread = threading.Thread(
        target=heartbeat_loop, name="aquant-scheduler-heartbeat", daemon=True)
    heartbeat_thread.start()
    try:
        while True:
            result = one_round()
            if result not in ("idle",):
                log("本轮：" + result)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log("收到中断，退出")
        con.close()
        return 0
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2.0)
        write_heartbeat("STOPPED")


if __name__ == "__main__":
    raise SystemExit(main())
