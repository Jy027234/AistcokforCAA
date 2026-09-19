"""研究作业的执行器（主文档 §8.4、§14.2）。

分工
----
`JobStore` 只回答"谁在什么时候持有哪个任务"——它不执行计算。
本模块补上另一半：**把一条 PENDING 作业跑完**，并如实记录结果或失败。

为什么执行与调度分开
--------------------
§14.2 要求作业由独立 worker + 数据库任务租约驱动，不引入消息中间件。
把执行逻辑放在这里而不是塞进 API 进程，是为了让同一条作业既能在
独立 worker 里跑，也能在测试里**同步跑一次**——两条路径共用同一份代码，
不会出现"测试里跑的是另一套逻辑"。

可重放要求（§8.4）
------------------
同一幂等键重复提交**不产生第二次计算**。因此"作业已成功"时 runner
直接返回既有结果并标记 reused，而不是再跑一遍。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from aquant.domain.data.reader import SnapshotReader
from aquant.domain.research.f10 import compute_f10_for_snapshot
from aquant.operations.jobs import Job, JobError, JobStatus, JobStore, job_payload

#: 因子计算作业：在某一快照上算 F10 并落库（§10.2）。
JOB_FACTOR_COMPUTE = "FACTOR_COMPUTE"
#: 证据研究作业：检索并抽取可引用证据（§5.3，需要模型参与）。
JOB_EVIDENCE_RESEARCH = "EVIDENCE_RESEARCH"

KNOWN_JOB_TYPES = (JOB_FACTOR_COMPUTE, JOB_EVIDENCE_RESEARCH)


class ResearchJobError(RuntimeError):
    """作业执行失败。带错误码与修复建议，直接落进 job.error_code/detail。"""

    def __init__(self, code: str, message: str, repair_action: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.repair_action = repair_action


def _payload(job: Job) -> dict:
    try:
        return job_payload(job.payload_json)
    except (TypeError, ValueError) as exc:
        raise ResearchJobError(
            "DATA_NOT_READY",
            f"job {job.job_id} payload is not valid JSON: {exc}",
            "re-submit the job with a well-formed payload") from exc


def run_research_job(con: sqlite3.Connection, reader: SnapshotReader, *,
                     job_id: str, worker_id: str,
                     provider: object | None = None) -> dict:
    """执行**指定**的一条作业。

    与 run_once 的区别只在"挑哪一条"：run_once 自己领一条待执行的，
    这里跑调用方点名的那一条。执行逻辑是同一条路径——两条入口共用
    `_execute`，这样"测试里跑一次"与"worker 领一条"不会分叉成两套行为。
    """

    store = JobStore(con)
    job = store.get(job_id)
    if job.status is JobStatus.SUCCEEDED:
        return _already_done(job)
    if job.status is not JobStatus.PENDING:
        raise JobError(
            "DATA_NOT_READY",
            f"job {job_id} is {job.status.value}, not PENDING",
            job_id, "only a PENDING job can be run; list jobs for runnable ones")
    # 走正常领取路径：租约与状态转移由 JobStore 保证，
    # 不在这里手写 UPDATE，否则会绕过状态机（A13）。
    claimed = store.claim(worker_id, job_types=[job.job_type])
    if claimed is None or claimed.job_id != job_id:
        # 同一类型下先领到了别的作业：领错了就放回去，不要顺手执行它
        if claimed is not None:
            store.release(claimed.job_id, worker_id,
                          reason="claimed for a different job id")
        raise JobError("DATA_NOT_READY",
                       f"job {job_id} could not be claimed", job_id,
                       "another worker may hold the lease")
    return _execute(con, reader, store, claimed, provider=provider)


def _already_done(job: Job) -> dict:
    return {"jobId": job.job_id, "jobType": job.job_type,
            "status": job.status.value,
            "result": json.loads(job.result_json) if job.result_json else None,
            "reused": True}


def run_once(con: sqlite3.Connection, reader: SnapshotReader, *,
             worker_id: str | None = None,
             job_types: list[str] | None = None,
             provider: object | None = None) -> dict | None:
    """领取并执行一条作业。没有待执行作业时返回 None。

    返回 {"jobId","jobType","status","result","reused"}。

    provider：文本模型提供方。**默认不构造**——由需要模型的作业自己
    按 ADR-012 去拿生产实现。测试注入确定性替身，因此离线可跑。
    """

    store = JobStore(con)
    worker_id = worker_id or ("worker-" + uuid.uuid4().hex[:12])
    job = store.claim(worker_id, job_types=job_types)
    if job is None:
        return None
    if job.status is JobStatus.SUCCEEDED:
        # claim 只会给出 PENDING 或租约过期的 RUNNING，正常到不了这里；
        # 留着是为了万一状态机改动时行为仍然正确，而不是把已完成的活再跑一遍
        return _already_done(job)
    return _execute(con, reader, store, job, provider=provider)


def _execute(con: sqlite3.Connection, reader: SnapshotReader,
             store: JobStore, job: Job, *, provider: object | None = None) -> dict:
    """跑一条**已被本 worker 领取**的作业，并落结果或失败。

    幂等（§8.4）：已成功的作业不再重算。这不是优化，是正确性——
    重复计算会写出第二份研究运行，而"同一逻辑作业"的两份结果
    在事后无法分辨哪份是权威。
    """

    if job.status is JobStatus.SUCCEEDED:
        return _already_done(job)

    try:
        result = _dispatch(con, reader, job, provider=provider)
    except ResearchJobError as exc:
        store.finish(job.job_id, JobStatus.FAILED,
                     error_code=exc.code, error_detail=f"{exc.message}｜修复：{exc.repair_action}")
        return {"jobId": job.job_id, "jobType": job.job_type,
                "status": JobStatus.FAILED.value,
                "error": {"code": exc.code, "message": exc.message,
                          "repair_action": exc.repair_action},
                "reused": False}
    except Exception as exc:                              # noqa: BLE001
        # 未预期的异常也要落成 FAILED：让作业停在 RUNNING 会让它
        # 一直占着租约直到超时，而失败原因彻底丢失。
        store.finish(job.job_id, JobStatus.FAILED,
                     error_code="DATA_NOT_READY",
                     error_detail=f"{type(exc).__name__}: {str(exc)[:300]}")
        return {"jobId": job.job_id, "jobType": job.job_type,
                "status": JobStatus.FAILED.value,
                "error": {"code": "DATA_NOT_READY",
                          "message": f"{type(exc).__name__}: {exc}"},
                "reused": False}

    store.finish(job.job_id, JobStatus.SUCCEEDED, result=result)
    return {"jobId": job.job_id, "jobType": job.job_type,
            "status": JobStatus.SUCCEEDED.value, "result": result, "reused": False}




def run_pending(con: sqlite3.Connection, reader: SnapshotReader, *,
                limit: int = 10, worker_id: str | None = None,
                job_types: list[str] | None = None) -> list[dict]:
    """连续执行至多 limit 条作业。供独立 worker 与验收脚本使用。"""

    worker_id = worker_id or ("worker-" + uuid.uuid4().hex[:12])
    out: list[dict] = []
    for _ in range(max(0, limit)):
        done = run_once(con, reader, worker_id=worker_id, job_types=job_types)
        if done is None:
            break
        out.append(done)
    return out


def _dispatch(con: sqlite3.Connection, reader: SnapshotReader, job: Job, *,
              provider: object | None = None) -> dict:
    if job.job_type == JOB_FACTOR_COMPUTE:
        return _compute_factors(con, reader, job)
    if job.job_type == JOB_EVIDENCE_RESEARCH:
        # 推迟到调用时 import：模型客户端是可选依赖，
        # 模块级 import 会让"只想算因子"的路径也依赖它。
        try:
            from aquant.domain.ai.evidence import research_evidence
        except ImportError as exc:
            raise ResearchJobError(
                "DATA_NOT_READY",
                f"evidence research is not available: {exc}",
                "provide the model provider adapter (see ADR-012)") from exc
        return research_evidence(con, reader, job, provider=provider)
    raise ResearchJobError(
        "DATA_NOT_READY",
        f"unknown job type {job.job_type!r}",
        f"use one of {list(KNOWN_JOB_TYPES)}")


def _compute_factors(con: sqlite3.Connection, reader: SnapshotReader, job: Job) -> dict:
    snapshot_id = job.input_snapshot_id
    if not snapshot_id:
        raise ResearchJobError("DATA_NOT_READY", "job has no input snapshot",
                               "submit the job with a snapshot id")
    payload = _payload(job)
    ref = reader.ref(snapshot_id)
    out = compute_f10_for_snapshot(
        con=con, reader=reader, snapshot_id=snapshot_id,
        as_of=ref.as_of_time, limit=payload.get("limit"))
    return {
        "researchRunId": out.get("researchRunId"),
        "factorId": out.get("factorId"),
        "financialStatements": out.get("financialStatements"),
        "skippedStatements": out.get("skippedStatements"),
        "exclusionBreakdown": out.get("exclusionBreakdown"),
        "snapshotId": snapshot_id,
        "asOfTime": ref.as_of_time.isoformat(),
    }


def submit_research_job(con: sqlite3.Connection, *, job_type: str, trading_day: str,
                        snapshot_id: str, config_version: str = "default",
                        payload: dict | None = None,
                        idempotency_namespace: str | None = None,
                        owner_metadata: dict[str, str] | None = None) -> dict:
    """提交一条研究作业。重复提交返回同一 job_id（§8.4）。"""

    if job_type not in KNOWN_JOB_TYPES:
        raise JobError("DATA_NOT_READY",
                       f"unknown job type {job_type!r}", job_type,
                       f"use one of {list(KNOWN_JOB_TYPES)}")
    store = JobStore(con)
    job_id, created = store.submit(
        job_type=job_type, trading_day=trading_day,
        config_version=config_version, input_snapshot_id=snapshot_id,
        payload=payload, idempotency_namespace=idempotency_namespace,
        owner_metadata=owner_metadata)
    job = store.get(job_id)
    return {
        "jobId": job_id,
        "created": created,
        "jobType": job.job_type,
        "status": job.status.value,
        "tradingDay": job.trading_day,
        "snapshotId": job.input_snapshot_id,
        "idempotencyKey": job.idempotency_key,
        "note": ("重复提交返回同一条作业：同一逻辑作业不产生第二次计算（§8.4）"
                 if not created else "作业已入队"),
        "submittedAt": datetime.now(timezone.utc).isoformat(),
    }
