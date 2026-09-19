"""每日流水线的调度：配置、到点判断、运行请求。

它是什么、不是什么
=================
**是**：一份"每天几点跑"的配置（可在界面上改）、一个纯函数的到点判断、
一张由界面写入的运行请求表，以及一张给界面读的结果摘要表。

**不是**：不是常驻在 API 里的定时器。

为什么调度不放在 API 进程里
--------------------------
主规格 §14.2 要求作业由**独立 worker + 数据库任务租约**驱动，且
v0.2.1/v0.2.2 明确"调度所有权唯一，基座不做第二份日常调度"。把定时器
塞进 API 进程有三个具体的坏结果：

  1. API 一重启（部署、改配置、崩溃）当天的那次运行就没了，而且**没人会知道**；
  2. 采集要跑几十秒到几分钟，跑在 API 进程里会与请求争用同一个库连接；
  3. 多个 API 实例（或一个容器被重启成两份）会各自到点触发，
     变成两次并发采集写同一个缓存——正是 `daily_run` 的锁要防的事。

因此这里只做两件事：**判断该不该跑**，以及**记录谁请求了跑**。
真正的执行在 `tools/scheduler_worker.py`（独立进程），它调用的是
`tools/daily_run.py`——与手工运行**完全同一条路径**，包括并发锁与留痕。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

from ..domain.data.db import write_tx

#: `HH:MM`，24 小时制。**不接受"20:30:00"或"8:30"**：
#: 前者说明调用方在传另一个字段，后者是另一个格式——都按拒绝处理，
#: 而不是"顺手解析一下"。静默接受多种格式，最后总有一处按错的时区或
#: 错的位数理解它。
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")

SCHEDULE_ID = "daily"
LAST_RUN_ID = "last"
WORKER_HEARTBEAT_FILENAME = "scheduler-worker.json"


def write_worker_heartbeat(
    data_dir: str | Path,
    *,
    worker_id: str,
    pid: int,
    started_at: datetime,
    interval_seconds: float,
    status: str = "RUNNING",
    now: datetime | None = None,
) -> Path:
    """原子写入独立 worker 的活性证明。

    调度配置启用只说明“应该运行”，不能证明进程仍活着。心跳放在产品与
    worker 共用的数据目录中，容器 API 无需读取 Windows 进程表也能判断。
    """

    if started_at.tzinfo is None:
        raise ValueError("worker started_at must be timezone-aware")
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("worker heartbeat time must be timezone-aware")
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = root / WORKER_HEARTBEAT_FILENAME
    payload = {
        "workerId": worker_id,
        "pid": int(pid),
        "status": status,
        "startedAt": _iso(started_at),
        "heartbeatAt": _iso(moment),
        "intervalSeconds": float(interval_seconds),
    }
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(root), prefix=".scheduler-worker-",
        suffix=".tmp", delete=False,
    ) as handle:
        temp_name = handle.name
        json.dump(payload, handle, ensure_ascii=False, indent=2)
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
    return target


def worker_liveness(
    data_dir: str | Path,
    *,
    now: datetime | None = None,
    minimum_stale_seconds: float = 90.0,
) -> dict:
    """读取 worker 心跳并判断是否仍新鲜；不读取平台专有进程表。"""

    path = Path(data_dir) / WORKER_HEARTBEAT_FILENAME
    if not path.is_file():
        return {
            "running": False, "status": "MISSING", "heartbeatAt": None,
            "ageSeconds": None, "detail": f"缺少 worker 心跳：{path}",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        heartbeat_at = datetime.fromisoformat(str(payload["heartbeatAt"]))
        if heartbeat_at.tzinfo is None:
            raise ValueError("heartbeatAt has no timezone")
        interval = max(float(payload.get("intervalSeconds") or 0), 0.0)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        return {
            "running": False, "status": "INVALID", "heartbeatAt": None,
            "ageSeconds": None, "detail": f"worker 心跳不可读：{type(exc).__name__}",
        }
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("worker liveness time must be timezone-aware")
    age = max(0.0, (moment.astimezone(timezone.utc)
                    - heartbeat_at.astimezone(timezone.utc)).total_seconds())
    stale_after = max(float(minimum_stale_seconds), interval * 3)
    declared = str(payload.get("status") or "UNKNOWN").upper()
    fresh = age <= stale_after
    running = declared == "RUNNING" and fresh
    status = declared if not fresh else ("RUNNING" if running else declared)
    if not fresh:
        status = "STALE"
    detail = (f"worker 心跳正常（{age:.1f} 秒前）" if running else
              f"worker 心跳状态 {status}（{age:.1f} 秒前）")
    return {
        "running": running,
        "status": status,
        "workerId": payload.get("workerId"),
        "pid": payload.get("pid"),
        "startedAt": payload.get("startedAt"),
        "heartbeatAt": payload.get("heartbeatAt"),
        "ageSeconds": round(age, 1),
        "staleAfterSeconds": round(stale_after, 1),
        "detail": detail,
    }


class ScheduleError(ValueError):
    """配置不合法。**带 repair**：界面要能直接显示怎么改。"""

    def __init__(self, message: str, repair: str) -> None:
        super().__init__(message)
        self.message = message
        self.repair = repair


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("scheduler timestamps must be timezone-aware")
    return dt.astimezone(timezone.utc).isoformat()


def _parse(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def validate_time_of_day(value: str) -> str:
    text = (value or "").strip()
    if not _TIME_RE.match(text):
        raise ScheduleError(
            f"运行时间 {value!r} 不是 HH:MM（24 小时制）",
            "填 24 小时制的 HH:MM，例如 20:30；不要写 20:30:00 或 8:30")
    return text


@dataclass(frozen=True, slots=True)
class RunSchedule:
    enabled: bool
    run_at_local: str = "20:30"
    weekdays_only: bool = True
    interpreter: str = ""
    data_dir: str = ""
    window_start: str = "2026-06-22"
    updated_at: str | None = None
    updated_by: str | None = None

    @property
    def at(self) -> time:
        hour, minute = self.run_at_local.split(":")
        return time(int(hour), int(minute))

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "runAtLocal": self.run_at_local,
            "weekdaysOnly": self.weekdays_only,
            "interpreter": self.interpreter,
            "dataDir": self.data_dir,
            "windowStart": self.window_start,
            "updatedAt": self.updated_at,
            "updatedBy": self.updated_by,
        }


def next_fire(schedule: RunSchedule, *, now: datetime) -> datetime | None:
    """下一次该跑的时刻（now 所在的时区）。停用时返回 None。

    纯函数、可用假时间测试：这是调度里唯一"会算错一天/一小时"的地方，
    因此它不碰数据库、不读时钟。
    """

    if not schedule.enabled:
        return None
    candidate = datetime.combine(now.date(), schedule.at, tzinfo=now.tzinfo)
    if candidate <= now:
        candidate = candidate + timedelta(days=1)
    if schedule.weekdays_only:
        # 工作日定义用自然周一到周五。**不查交易日历**：这份日历要读快照，
        # 而调度器必须能在还没有任何快照的机器上工作。休市日跑了会得到
        # 一次"没有新行情"的按计划跳过——那是正确行为，不是浪费。
        while candidate.weekday() >= 5:
            candidate = candidate + timedelta(days=1)
    return candidate


def is_due(schedule: RunSchedule, *, now: datetime, last_fired_for: date | None) -> bool:
    """现在该不该触发。**每天至多一次**，由 last_fired_for 保证。

    只比"时间到了没有"会出两种错：worker 重启后把当天已经跑过的再跑一次，
    以及错过时刻之后永远不补。因此这里要求：
      1. 今天的触发时刻已过；
      2. 今天还没触发过（last_fired_for != today）；
      3. 今天是允许的日子。
    第 3 条只在最后判断，因此"停用"与"日子不对"都不会留下触发记录。
    """

    if not schedule.enabled:
        return False
    if schedule.weekdays_only and now.weekday() >= 5:
        return False
    if last_fired_for == now.date():
        return False
    return now.timetz().replace(tzinfo=None) >= schedule.at


# ======================================================================
# 存储
# ======================================================================
def load_schedule(con: sqlite3.Connection) -> RunSchedule:
    """读配置。没有行时返回**默认的停用配置**，不自动建行。

    为什么默认停用：调度一旦存在就会按点跑采集。让"装上就自动开始每天
    抓数据"成为默认行为，是替使用者做了一个他没做过的决定。
    """

    row = con.execute(
        "SELECT enabled, run_at_local, weekdays_only, interpreter, data_dir,"
        "       window_start, updated_at, updated_by "
        "FROM run_schedule WHERE schedule_id=?", (SCHEDULE_ID,)).fetchone()
    if row is None:
        return RunSchedule(enabled=False)
    return RunSchedule(
        enabled=bool(row["enabled"]),
        run_at_local=row["run_at_local"],
        weekdays_only=bool(row["weekdays_only"]),
        interpreter=row["interpreter"] or "",
        data_dir=row["data_dir"] or "",
        window_start=row["window_start"] or "2026-06-22",
        updated_at=row["updated_at"],
        updated_by=row["updated_by"],
    )


def save_schedule(con: sqlite3.Connection, *, enabled: bool, run_at_local: str,
                  weekdays_only: bool, interpreter: str, data_dir: str,
                  window_start: str, actor: str,
                  now: datetime | None = None) -> RunSchedule:
    """写配置。**启用时必须有解释器**，否则拒绝。

    这条校验不是形式：没有解释器时 worker 会去用 `sys.executable`，
    而那多半是没装 baostock 的那个——于是每天都失败，且失败信息
    （"baostock 未安装"）看起来像数据源问题，不像配置问题。
    """

    run_at_local = validate_time_of_day(run_at_local)
    interpreter = (interpreter or "").strip()
    if enabled and not interpreter:
        raise ScheduleError(
            "启用每日任务必须指定解释器",
            r"填装了 baostock 的解释器绝对路径，例如 "
            r"E:\IT\Agent\.venv\Scripts\python.exe")
    stamp = _iso(now or datetime.now(timezone.utc))
    with write_tx(con):
        con.execute(
            "INSERT INTO run_schedule (schedule_id,enabled,run_at_local,weekdays_only,"
            "interpreter,data_dir,window_start,updated_at,updated_by) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(schedule_id) DO UPDATE SET enabled=excluded.enabled,"
            "run_at_local=excluded.run_at_local, weekdays_only=excluded.weekdays_only,"
            "interpreter=excluded.interpreter, data_dir=excluded.data_dir,"
            "window_start=excluded.window_start, updated_at=excluded.updated_at,"
            "updated_by=excluded.updated_by",
            (SCHEDULE_ID, int(bool(enabled)), run_at_local, int(bool(weekdays_only)),
             interpreter, (data_dir or "").strip(), (window_start or "").strip(),
             stamp, actor))
    return load_schedule(con)


# ---------------------------------------------------------------- 运行请求
def request_run(con: sqlite3.Connection, *, source: str, requested_by: str,
                reason: str | None = None, now: datetime | None = None) -> str:
    """登记一次运行请求，返回 request_id。**只登记，不执行。**

    同一时刻至多一条未完成的请求：worker 崩在 CLAIMED 之后，
    那条请求会永远留在 CLAIMED——所以 `startup_recovery` 要在启动时
    把它放回 PENDING。残留一条 CLAIMED 而没人处理，是"点了按钮没反应"
    这类问题最常见的根因。
    """

    if source not in ("scheduler", "manual"):
        raise ScheduleError(f"未知来源 {source!r}", "用 scheduler 或 manual")
    pending = con.execute(
        "SELECT request_id FROM pipeline_run_request "
        "WHERE status IN ('PENDING','CLAIMED') ORDER BY requested_at LIMIT 1").fetchone()
    if pending is not None:
        return pending["request_id"]
    request_id = "run_" + uuid.uuid4().hex[:16]
    with write_tx(con):
        con.execute(
            "INSERT INTO pipeline_run_request (request_id,source,reason,requested_by,"
            "requested_at,status) VALUES (?,?,?,?,?,'PENDING')",
            (request_id, source, reason, requested_by,
             _iso(now or datetime.now(timezone.utc))))
    return request_id


def startup_recovery(con: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """把 CLAIMED 放回 PENDING。返回被放回的条数。

    只在 worker **启动时**调用，不在循环里调用：循环里调用会把另一个
    正常运行的 worker 正在执行的那条抢回来，变成两次采集。
    这正是"无法区分'上一次还在跑'与'上一次崩了'"的同一个问题，
    在进程级只能靠"启动时清一次"来近似。
    """

    with write_tx(con):
        cur = con.execute(
            "UPDATE pipeline_run_request SET status='PENDING', claimed_at=NULL,"
            "claimed_by=NULL WHERE status='CLAIMED'")
        return cur.rowcount or 0


def claim_next(con: sqlite3.Connection, *, worker_id: str,
               now: datetime | None = None) -> dict | None:
    """领取一条待执行请求。没有则返回 None。"""

    row = con.execute(
        "SELECT * FROM pipeline_run_request WHERE status='PENDING' "
        "ORDER BY requested_at LIMIT 1").fetchone()
    if row is None:
        return None
    stamp = _iso(now or datetime.now(timezone.utc))
    with write_tx(con):
        cur = con.execute(
            "UPDATE pipeline_run_request SET status='CLAIMED', claimed_at=?,"
            "claimed_by=? WHERE request_id=? AND status='PENDING'",
            (stamp, worker_id, row["request_id"]))
        if not cur.rowcount:
            # 别的 worker 抢先领走了：这是正常的并发，不是错误
            return None
    return dict(row)


def finish_request(con: sqlite3.Connection, *, request_id: str, status: str,
                   exit_code: int | None, detail: str,
                   now: datetime | None = None) -> None:
    if status not in ("DONE", "FAILED", "CANCELLED"):
        raise ScheduleError(f"未知终态 {status!r}", "用 DONE / FAILED / CANCELLED")
    with write_tx(con):
        con.execute(
            "UPDATE pipeline_run_request SET status=?, finished_at=?, exit_code=?,"
            "detail=? WHERE request_id=?",
            (status, _iso(now or datetime.now(timezone.utc)), exit_code,
             detail[:2000], request_id))


def record_last_run(con: sqlite3.Connection, *, request_id: str | None,
                    source: str, record: dict, exit_code: int,
                    now: datetime | None = None) -> None:
    """把留痕记录摘要成一行，供界面读取。**留痕文件仍是权威。**"""

    import json

    with write_tx(con):
        con.execute(
            "INSERT INTO pipeline_last_run (run_id,request_id,source,trading_day,"
            "snapshot_id,outcome,reason,exit_code,started_at,finished_at,"
            "duration_seconds,steps_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET request_id=excluded.request_id,"
            "source=excluded.source, trading_day=excluded.trading_day,"
            "snapshot_id=excluded.snapshot_id, outcome=excluded.outcome,"
            "reason=excluded.reason, exit_code=excluded.exit_code,"
            "started_at=excluded.started_at, finished_at=excluded.finished_at,"
            "duration_seconds=excluded.duration_seconds, steps_json=excluded.steps_json",
            (LAST_RUN_ID, request_id, source, record.get("trading_day"),
             record.get("snapshot_id"), record.get("outcome"), record.get("reason"),
             exit_code, record.get("started_at"), record.get("finished_at"),
             record.get("duration_seconds"),
             json.dumps([{"step": s.get("step"), "ok": s.get("ok"),
                          "seconds": s.get("seconds")}
                         for s in (record.get("steps") or [])], ensure_ascii=False)))


def last_run(con: sqlite3.Connection) -> dict | None:
    row = con.execute("SELECT * FROM pipeline_last_run WHERE run_id=?",
                      (LAST_RUN_ID,)).fetchone()
    if row is None:
        return None
    import json

    try:
        steps = json.loads(row["steps_json"] or "[]")
    except json.JSONDecodeError:
        # 坏行不能让整个状态接口失败——那会把"看不到状态"变成"服务坏了"
        steps = []
    return {
        "requestId": row["request_id"],
        "source": row["source"],
        "tradingDay": row["trading_day"],
        "snapshotId": row["snapshot_id"],
        "outcome": row["outcome"],
        "reason": row["reason"],
        "exitCode": row["exit_code"],
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
        "durationSeconds": row["duration_seconds"],
        "steps": steps,
    }


def last_run_day(con: sqlite3.Connection) -> date | None:
    """上一次运行覆盖的交易日（用于"每天至多一次"的判断）。

    用**运行记录**而不是"配置里的今天"：worker 重启后必须能知道当天跑过没有。
    """

    row = con.execute("SELECT trading_day FROM pipeline_last_run WHERE run_id=?",
                      (LAST_RUN_ID,)).fetchone()
    if row is None or not row["trading_day"]:
        return None
    try:
        return date.fromisoformat(row["trading_day"])
    except ValueError:
        return None


def pending_request(con: sqlite3.Connection) -> dict | None:
    """尚未执行完的请求。**只暴露界面要用的字段**，且按响应体的命名约定
    转成 camelCase——返回原始行会让前端拿到 snake_case，
    而同一个响应体里两种命名并存正是"前端读错字段"的经典来源。"""

    row = con.execute(
        "SELECT request_id, source, status, requested_at, requested_by, reason "
        "FROM pipeline_run_request WHERE status IN ('PENDING','CLAIMED') "
        "ORDER BY requested_at LIMIT 1").fetchone()
    if row is None:
        return None
    return {
        "requestId": row["request_id"],
        "source": row["source"],
        "status": row["status"],
        "requestedAt": row["requested_at"],
        "requestedBy": row["requested_by"],
        "reason": row["reason"],
    }


def status(con: sqlite3.Connection, *, now: datetime | None = None) -> dict:
    """给界面的一份完整状态：配置 + 下一次触发 + 最近一次运行 + 待处理请求。"""

    schedule = load_schedule(con)
    moment = now or datetime.now().astimezone()
    nxt = next_fire(schedule, now=moment)
    return {
        "schedule": schedule.as_dict(),
        "nextFireAt": nxt.isoformat() if nxt else None,
        "lastRun": last_run(con),
        "pending": pending_request(con),
        "checkedAt": moment.isoformat(),
    }
