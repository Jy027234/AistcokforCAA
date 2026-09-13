"""任务机制：状态机、租约与幂等。

主文档 §8.4：
    状态机 PENDING → RUNNING → SUCCEEDED / FAILED / BLOCKED，
    带心跳、租约、尝试次数与错误码；
    幂等键 = 作业类型 + 交易日 + 配置版本 + 输入快照，并由数据库唯一约束保证。

§14.2：作业由独立 worker + 数据库任务租约驱动，不引入消息中间件。
§12.1：取消是**请求**，不是"资金状态已恢复"的证明——因此取消不改历史，只改当前状态。

本模块刻意不做的事：不执行计算、不调度线程。它只负责"谁在什么时候持有哪个任务"。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from ..domain.data.db import write_tx


class JobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


#: 允许的状态转移。任何未列出的转移都拒绝——状态不倒退（A13）。
_ALLOWED: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.PENDING: frozenset({JobStatus.RUNNING, JobStatus.BLOCKED}),
    JobStatus.RUNNING: frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED,
                                  JobStatus.BLOCKED, JobStatus.PENDING}),
    # 终态不可再转移（重跑必须产生新作业，见 §8.4 幂等键含配置版本）
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.BLOCKED: frozenset({JobStatus.PENDING, JobStatus.FAILED}),
}


class JobError(Exception):
    def __init__(self, code: str, message: str, object_id: str, repair_action: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.object_id = object_id
        self.repair_action = repair_action


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("job timestamps must be timezone-aware")
    return dt.astimezone(timezone.utc).isoformat()


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text)


def idempotency_key(job_type: str, trading_day: str, config_version: str,
                    input_snapshot_id: str) -> str:
    """§8.4 幂等键 = 作业类型 + 交易日 + 配置版本 + 输入快照。

    刻意做成确定性哈希：同一逻辑作业重复提交必须得到同一键，
    否则数据库唯一约束无法阻止重复计算（S05/A07）。
    """

    raw = "|".join([job_type, trading_day, config_version, input_snapshot_id])
    return "job-" + hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass(slots=True)
class Job:
    job_id: str
    job_type: str
    idempotency_key: str
    status: JobStatus
    attempt_count: int
    trading_day: str | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    error_code: str | None
    error_detail: str | None


class JobStore:
    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con

    # ------------------------------------------------------------ submit
    def submit(self, *, job_type: str, trading_day: str, config_version: str,
               input_snapshot_id: str, payload: dict | None = None) -> tuple[str, bool]:
        """提交作业。返回 (job_id, created)。

        重复提交返回**同一个 job_id 且 created=False**，不新建作业——
        §8.4 与 S05："重试同一冻结计划，订单/费用/成交仅记录一次"。
        """

        key = idempotency_key(job_type, trading_day, config_version, input_snapshot_id)
        existing = self.con.execute(
            "SELECT job_id FROM job WHERE idempotency_key=?", (key,)
        ).fetchone()
        if existing:
            return existing["job_id"], False

        job_id = "job_" + uuid.uuid4().hex[:20]
        now = datetime.now(timezone.utc)
        with write_tx(self.con):
            self.con.execute(
                "INSERT INTO job (job_id,job_type,trading_day,config_version,"
                "input_snapshot_id,idempotency_key,status,attempt_count,payload_json,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,0,?,?,?)",
                (job_id, job_type, trading_day, config_version, input_snapshot_id, key,
                 JobStatus.PENDING.value,
                 json.dumps(payload, ensure_ascii=False) if payload else None,
                 _iso(now), _iso(now)),
            )
        return job_id, True

    # ------------------------------------------------------------ lease
    def claim(self, worker_id: str, *, lease_seconds: int = 300,
              job_types: list[str] | None = None) -> Job | None:
        """领取一个待执行作业。

        只领取 PENDING，或租约**已过期**的 RUNNING——后者意味着上一个 worker 崩了。
        这正是 A16"跨进程故障后恢复"的实现点。
        """

        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=lease_seconds)
        # 参数顺序必须与 SQL 中的占位符一一对应：
        #   status=PENDING  OR  (status=RUNNING AND lease_expires_at < now)  [AND job_type IN ...]
        sql = (
            "SELECT * FROM job WHERE (status=? OR (status=? AND lease_expires_at < ?))"
        )
        params: list = [JobStatus.PENDING.value, JobStatus.RUNNING.value, _iso(now)]
        if job_types:
            sql += " AND job_type IN (" + ",".join("?" * len(job_types)) + ")"
            params.extend(job_types)
        sql += " ORDER BY created_at LIMIT 1"

        with write_tx(self.con):
            row = self.con.execute(sql, params).fetchone()
            if row is None:
                return None
            self.con.execute(
                "UPDATE job SET status=?, lease_owner=?, lease_expires_at=?, "
                "attempt_count=attempt_count+1, heartbeat_at=?, updated_at=? WHERE job_id=?",
                (JobStatus.RUNNING.value, worker_id, _iso(expires), _iso(now), _iso(now),
                 row["job_id"]),
            )
        return self.get(row["job_id"])

    def heartbeat(self, job_id: str, worker_id: str, *, lease_seconds: int = 300) -> None:
        now = datetime.now(timezone.utc)
        with write_tx(self.con):
            cur = self.con.execute(
                "UPDATE job SET heartbeat_at=?, lease_expires_at=?, updated_at=? "
                "WHERE job_id=? AND lease_owner=? AND status=?",
                (_iso(now), _iso(now + timedelta(seconds=lease_seconds)), _iso(now),
                 job_id, worker_id, JobStatus.RUNNING.value),
            )
            if cur.rowcount == 0:
                raise JobError(
                    "DATA_NOT_READY",
                    f"job {job_id} is not held by {worker_id!r}", job_id,
                    "re-claim the job; the lease may have expired or been taken over",
                )

    # ------------------------------------------------------------ finish
    def finish(self, job_id: str, status: JobStatus, *, result: dict | None = None,
               error_code: str | None = None, error_detail: str | None = None) -> None:
        current = self.get(job_id)
        if status not in _ALLOWED[current.status]:
            raise JobError(
                "DATA_NOT_READY",
                f"illegal transition {current.status.value} -> {status.value}", job_id,
                f"allowed from {current.status.value}: "
                f"{sorted(s.value for s in _ALLOWED[current.status])}",
            )
        now = datetime.now(timezone.utc)
        with write_tx(self.con):
            self.con.execute(
                "UPDATE job SET status=?, result_json=?, error_code=?, error_detail=?, "
                "lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE job_id=?",
                (status.value, json.dumps(result, ensure_ascii=False) if result else None,
                 error_code, error_detail, _iso(now), job_id),
            )

    def release(self, job_id: str, worker_id: str, *, reason: str | None = None) -> None:
        """主动释放回 PENDING（例如资源不足）。取消是请求，不是状态已恢复的证明（§12.1）。"""

        current = self.get(job_id)
        if current.status is not JobStatus.RUNNING:
            raise JobError("DATA_NOT_READY", f"job {job_id} is not RUNNING", job_id,
                           "release only a RUNNING job")
        if current.lease_owner != worker_id:
            raise JobError("DATA_NOT_READY", f"job {job_id} not held by {worker_id!r}",
                           job_id, "only the lease holder may release")
        now = datetime.now(timezone.utc)
        with write_tx(self.con):
            self.con.execute(
                "UPDATE job SET status=?, lease_owner=NULL, lease_expires_at=NULL, "
                "error_detail=?, updated_at=? WHERE job_id=?",
                (JobStatus.PENDING.value, reason, _iso(now), job_id),
            )

    # ------------------------------------------------------------ read
    def get(self, job_id: str) -> Job:
        row = self.con.execute("SELECT * FROM job WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise JobError("DATA_NOT_READY", f"unknown job {job_id!r}", job_id,
                           "list jobs and use an existing id")
        return Job(
            job_id=row["job_id"], job_type=row["job_type"],
            idempotency_key=row["idempotency_key"], status=JobStatus(row["status"]),
            attempt_count=row["attempt_count"], trading_day=row["trading_day"],
            lease_owner=row["lease_owner"],
            lease_expires_at=_parse(row["lease_expires_at"]) if row["lease_expires_at"] else None,
            error_code=row["error_code"], error_detail=row["error_detail"],
        )

    def by_key(self, key: str) -> Job | None:
        row = self.con.execute("SELECT job_id FROM job WHERE idempotency_key=?",
                               (key,)).fetchone()
        return self.get(row["job_id"]) if row else None

    def counts_by_status(self) -> dict[str, int]:
        rows = self.con.execute("SELECT status, COUNT(*) n FROM job GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}
