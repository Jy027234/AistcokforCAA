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
from typing import Any, Mapping

from ..domain.data.db import write_tx


class JobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


#: 允许的状态转移。任何未列出的转移都拒绝——状态不倒退（A13）。
_ALLOWED: dict[JobStatus, frozenset[JobStatus]] = {
    # PENDING -> RUNNING is owned exclusively by claim(), which also records
    # the lease owner and expiry.  finish() must never manufacture RUNNING.
    JobStatus.PENDING: frozenset({JobStatus.BLOCKED}),
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


_JOB_PAYLOAD_ENVELOPE = "__aquant_job_v1__"


def _normalise_namespace(namespace: str | None) -> str | None:
    """Return a stable namespace value, preserving the legacy ``None`` case."""

    if namespace is None:
        return None
    value = str(namespace).strip()
    if not value:
        raise ValueError("idempotency namespace must not be empty")
    return value


def _normalise_owner_metadata(
    owner_metadata: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    """Keep only the verified owner fields persisted with an agentctl job.

    The handler obtains these values from platform metadata.  JobStore still
    validates them here so callers cannot accidentally create a partially
    owned job which the status capability could later treat as owned.
    """

    if owner_metadata is None:
        return None
    if not isinstance(owner_metadata, Mapping):
        raise ValueError("owner metadata must be an object")
    tenant_id = str(owner_metadata.get("tenant_id") or "").strip()
    actor_user_id = str(owner_metadata.get("actor_user_id") or "").strip()
    if not tenant_id or not actor_user_id:
        raise ValueError("owner metadata requires tenant_id and actor_user_id")
    return {"tenant_id": tenant_id, "actor_user_id": actor_user_id}


def _encode_payload(
    payload: dict | None,
    *,
    idempotency_namespace: str | None,
    owner_metadata: dict[str, str] | None,
) -> str | None:
    """Encode optional ownership metadata without adding a schema column.

    Existing unowned callers retain their exact payload representation.  The
    envelope is only used when namespace or owner data has to be durable; the
    research runner unwraps it before dispatching the user payload.
    """

    if payload is not None and not isinstance(payload, dict):
        raise ValueError("job payload must be an object")
    if idempotency_namespace is None and owner_metadata is None:
        return json.dumps(payload, ensure_ascii=False) if payload else None

    metadata: dict[str, Any] = {"version": 1}
    if idempotency_namespace is not None:
        metadata["idempotency_namespace"] = idempotency_namespace
    if owner_metadata is not None:
        metadata["owner"] = owner_metadata
    envelope = {
        _JOB_PAYLOAD_ENVELOPE: metadata,
        "payload": payload or {},
    }
    return json.dumps(envelope, ensure_ascii=False)


def _payload_metadata(payload_json: str | None) -> tuple[dict[str, str] | None, str | None]:
    """Extract persisted ownership metadata, treating legacy rows as unowned."""

    if not payload_json:
        return None, None
    try:
        decoded = json.loads(payload_json)
    except (TypeError, json.JSONDecodeError):
        return None, None
    if not isinstance(decoded, dict):
        return None, None
    envelope = decoded.get(_JOB_PAYLOAD_ENVELOPE)
    if not isinstance(envelope, dict) or envelope.get("version") != 1:
        return None, None
    owner = envelope.get("owner")
    namespace = envelope.get("idempotency_namespace")
    if not isinstance(owner, dict):
        return None, _normalise_namespace(namespace) if namespace is not None else None
    try:
        normalised_owner = _normalise_owner_metadata(owner)
    except ValueError:
        return None, _normalise_namespace(namespace) if namespace is not None else None
    return normalised_owner, _normalise_namespace(namespace) if namespace is not None else None


def job_payload(payload_json: str | None) -> dict[str, Any]:
    """Return the caller payload, unwrapping the optional durable envelope."""

    if not payload_json:
        return {}
    decoded = json.loads(payload_json)
    if not isinstance(decoded, dict):
        raise ValueError("job payload must decode to an object")
    envelope = decoded.get(_JOB_PAYLOAD_ENVELOPE)
    if not isinstance(envelope, dict):
        return decoded
    payload = decoded.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("enveloped job payload must contain an object")
    return payload


def idempotency_key(job_type: str, trading_day: str, config_version: str,
                    input_snapshot_id: str, *,
                    namespace: str | None = None) -> str:
    """§8.4 幂等键 = 作业类型 + 交易日 + 配置版本 + 输入快照。

    刻意做成确定性哈希：同一逻辑作业重复提交必须得到同一键，
    否则数据库唯一约束无法阻止重复计算（S05/A07）。产品内部调用保持
    原公式；外部多主体入口可额外提供 namespace，避免跨主体复用同一作业。
    """

    resolved_namespace = _normalise_namespace(namespace)
    components = [job_type, trading_day, config_version, input_snapshot_id]
    raw = "|".join(components)
    if resolved_namespace is not None:
        raw = resolved_namespace + "|" + raw
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
    #: 下面几个字段原先只写不读（INSERT 里有、Job 里没有），
    #: 于是执行器拿不到作业参数与输入快照，只能报 AttributeError。
    #: 读取侧必须与写入侧一一对应——少映射一个字段，症状会出现在
    #: 很远的地方（"作业跑不了"），而不是在这里。
    config_version: str | None = None
    input_snapshot_id: str | None = None
    payload_json: str | None = None
    result_json: str | None = None
    # Stored inside payload_json because the existing job schema is shared by
    # older deployments and must not gain ownership columns.
    owner_metadata: dict[str, str] | None = None
    idempotency_namespace: str | None = None


class JobStore:
    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con

    # ------------------------------------------------------------ submit
    def submit(self, *, job_type: str, trading_day: str, config_version: str,
               input_snapshot_id: str, payload: dict | None = None,
               idempotency_namespace: str | None = None,
               owner_metadata: Mapping[str, Any] | None = None) -> tuple[str, bool]:
        """提交作业。返回 (job_id, created)。

        重复提交返回**同一个 job_id 且 created=False**，不新建作业——
        §8.4 与 S05："重试同一冻结计划，订单/费用/成交仅记录一次"。
        """

        resolved_namespace = _normalise_namespace(idempotency_namespace)
        normalised_owner = _normalise_owner_metadata(owner_metadata)
        key = idempotency_key(
            job_type, trading_day, config_version, input_snapshot_id,
            namespace=resolved_namespace,
        )
        encoded_payload = _encode_payload(
            payload,
            idempotency_namespace=resolved_namespace,
            owner_metadata=normalised_owner,
        )
        job_id = "job_" + uuid.uuid4().hex[:20]
        now = datetime.now(timezone.utc)
        with write_tx(self.con):
            # The unique key is the serialization point.  Doing the insert
            # and duplicate lookup in one IMMEDIATE transaction closes the
            # read-then-insert race between separate SQLite connections.
            cur = self.con.execute(
                "INSERT INTO job (job_id,job_type,trading_day,config_version,"
                "input_snapshot_id,idempotency_key,status,attempt_count,payload_json,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,0,?,?,?) "
                "ON CONFLICT(idempotency_key) DO NOTHING",
                (job_id, job_type, trading_day, config_version, input_snapshot_id, key,
                 JobStatus.PENDING.value,
                 encoded_payload,
                 _iso(now), _iso(now)),
            )
            if cur.rowcount == 1:
                return job_id, True
            existing = self.con.execute(
                "SELECT job_id FROM job WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing is None:
                raise JobError(
                    "DATA_NOT_READY",
                    f"job insert for idempotency key {key!r} was ignored",
                    key,
                    "inspect the product job schema and retry the submission",
                )
            existing_job = self.get(existing["job_id"])
            if (
                normalised_owner is not None
                and existing_job.owner_metadata != normalised_owner
            ):
                raise JobError(
                    "SOURCE_PERMISSION_MISSING",
                    "idempotent job key belongs to a different owner",
                    existing_job.job_id,
                    "use the verified tenant/actor namespace for this submission",
                )
            return existing["job_id"], False

    # ------------------------------------------------------------ lease
    def claim(self, worker_id: str, *, lease_seconds: int = 300,
              job_types: list[str] | None = None,
              job_id: str | None = None) -> Job | None:
        """领取一个待执行作业。

        只领取 PENDING，或租约**已过期**的 RUNNING——后者意味着上一个 worker 崩了。
        这正是 A16"跨进程故障后恢复"的实现点。
        ``job_id`` 用于点名领取；省略时按创建时间领取第一条符合条件的作业。
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
        if job_id is not None:
            sql += " AND job_id=?"
            params.append(job_id)
        sql += " ORDER BY created_at LIMIT 1"

        claimed: Job | None = None
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
            # Capture the row before releasing the write transaction.  A
            # zero/short lease may be reclaimed immediately after COMMIT; the
            # caller must still receive the lease it actually acquired rather
            # than a later worker's state.
            claimed = self.get(row["job_id"])
        return claimed

    def heartbeat(self, job_id: str, worker_id: str, attempt_count: int, *,
                  lease_seconds: int = 300) -> None:
        now = datetime.now(timezone.utc)
        with write_tx(self.con):
            cur = self.con.execute(
                "UPDATE job SET heartbeat_at=?, lease_expires_at=?, updated_at=? "
                "WHERE job_id=? AND lease_owner=? AND attempt_count=? AND status=?",
                (_iso(now), _iso(now + timedelta(seconds=lease_seconds)), _iso(now),
                 job_id, worker_id, attempt_count, JobStatus.RUNNING.value),
            )
            if cur.rowcount == 0:
                raise JobError(
                    "DATA_NOT_READY",
                    f"job {job_id} is not held by {worker_id!r}", job_id,
                    "re-claim the job; the lease may have expired or been taken over",
                )

    def assert_lease(self, job_id: str, worker_id: str,
                     attempt_count: int) -> None:
        """Fence a domain write to the current, unexpired job attempt.

        Call this from inside the same ``write_tx`` as the domain mutation.
        The IMMEDIATE transaction then prevents a reclaim between this check
        and the guarded writes.  ``attempt_count`` is the fencing token: a
        scheduler may reuse its worker id across attempts.
        """

        current = self.get(job_id)
        now = datetime.now(timezone.utc)
        active = (
            current.status is JobStatus.RUNNING
            and current.lease_owner == worker_id
            and current.attempt_count == attempt_count
            and current.lease_expires_at is not None
            and current.lease_expires_at >= now
        )
        if not active:
            raise JobError(
                "DATA_NOT_READY",
                f"job {job_id} lease attempt is no longer current",
                job_id,
                "discard the stale result and let the current lease holder continue",
            )

    # ------------------------------------------------------------ finish
    def finish(self, job_id: str, status: JobStatus, *,
               worker_id: str | None = None,
               attempt_count: int | None = None,
               result: dict | None = None,
               error_code: str | None = None, error_detail: str | None = None) -> None:
        """Apply one transition and reject callbacks from stale workers.

        A RUNNING job belongs to its current lease holder.  The state read,
        ownership check and write share one IMMEDIATE transaction so a reclaim
        and a late completion cannot interleave between those steps.
        """

        now = datetime.now(timezone.utc)
        with write_tx(self.con):
            current = self.get(job_id)
            if status not in _ALLOWED[current.status]:
                raise JobError(
                    "DATA_NOT_READY",
                    f"illegal transition {current.status.value} -> {status.value}", job_id,
                    f"allowed from {current.status.value}: "
                    f"{sorted(s.value for s in _ALLOWED[current.status])}",
                )
            if current.status is JobStatus.RUNNING and (
                not worker_id
                or worker_id != current.lease_owner
                or attempt_count != current.attempt_count
                or current.lease_expires_at is None
                or current.lease_expires_at < now
            ):
                raise JobError(
                    "DATA_NOT_READY",
                    f"job {job_id} is not held by {worker_id!r}",
                    job_id,
                    "discard the stale callback; only the current lease holder may finish",
                )
            self.con.execute(
                "UPDATE job SET status=?, result_json=?, error_code=?, error_detail=?, "
                "lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE job_id=?",
                (status.value, json.dumps(result, ensure_ascii=False) if result else None,
                 error_code, error_detail, _iso(now), job_id),
            )

    def release(self, job_id: str, worker_id: str, *,
                attempt_count: int | None = None,
                reason: str | None = None) -> None:
        """主动释放回 PENDING（例如资源不足）。取消是请求，不是状态已恢复的证明（§12.1）。"""

        now = datetime.now(timezone.utc)
        with write_tx(self.con):
            current = self.get(job_id)
            if current.status is not JobStatus.RUNNING:
                raise JobError("DATA_NOT_READY", f"job {job_id} is not RUNNING", job_id,
                               "release only a RUNNING job")
            if (
                current.lease_owner != worker_id
                or attempt_count != current.attempt_count
                or current.lease_expires_at is None
                or current.lease_expires_at < now
            ):
                raise JobError(
                    "DATA_NOT_READY",
                    f"job {job_id} not held by {worker_id!r}",
                    job_id,
                    "only the current unexpired lease attempt may release",
                )
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
        owner_metadata, idempotency_namespace = _payload_metadata(row["payload_json"])
        return Job(
            job_id=row["job_id"], job_type=row["job_type"],
            idempotency_key=row["idempotency_key"], status=JobStatus(row["status"]),
            attempt_count=row["attempt_count"], trading_day=row["trading_day"],
            lease_owner=row["lease_owner"],
            lease_expires_at=_parse(row["lease_expires_at"]) if row["lease_expires_at"] else None,
            error_code=row["error_code"], error_detail=row["error_detail"],
            config_version=row["config_version"],
            input_snapshot_id=row["input_snapshot_id"],
            payload_json=row["payload_json"], result_json=row["result_json"],
            owner_metadata=owner_metadata,
            idempotency_namespace=idempotency_namespace,
        )

    def by_key(self, key: str) -> Job | None:
        row = self.con.execute("SELECT job_id FROM job WHERE idempotency_key=?",
                               (key,)).fetchone()
        return self.get(row["job_id"]) if row else None

    def counts_by_status(self) -> dict[str, int]:
        rows = self.con.execute("SELECT status, COUNT(*) n FROM job GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}
