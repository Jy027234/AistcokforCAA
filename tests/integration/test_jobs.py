"""任务机制测试：状态机、幂等、租约与崩溃恢复。

对应主文档 §8.4、§12.1、A07、A13、A16、S05。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from aquant.domain.data.db import apply_migrations, connect, write_tx
from aquant.operations.jobs import (
    JobError,
    JobStatus,
    JobStore,
    idempotency_key,
)


@pytest.fixture()
def store(tmp_path):
    con = connect(tmp_path / "meta.sqlite")
    apply_migrations(con)
    yield JobStore(con)
    con.close()


def submit(store, **kw):
    base = dict(job_type="eod_research", trading_day="2026-09-11",
                config_version="cfg-v1", input_snapshot_id="snap-syn-001")
    base.update(kw)
    return store.submit(**base)


# ------------------------------------------------------------------ 幂等
def test_idempotency_key_is_deterministic():
    a = idempotency_key("eod", "2026-09-11", "cfg-v1", "snap-1")
    b = idempotency_key("eod", "2026-09-11", "cfg-v1", "snap-1")
    assert a == b


def test_any_component_change_produces_a_new_key():
    base = idempotency_key("eod", "2026-09-11", "cfg-v1", "snap-1")
    assert idempotency_key("eod2", "2026-09-11", "cfg-v1", "snap-1") != base
    assert idempotency_key("eod", "2026-09-12", "cfg-v1", "snap-1") != base
    assert idempotency_key("eod", "2026-09-11", "cfg-v2", "snap-1") != base
    assert idempotency_key("eod", "2026-09-11", "cfg-v1", "snap-2") != base


def test_duplicate_submit_returns_same_job(store):
    """A07 / S05：同一作业重复提交十次，只创建一个。"""

    ids = {submit(store)[0] for _ in range(10)}
    assert len(ids) == 1
    assert store.counts_by_status() == {"PENDING": 1}


def test_duplicate_submit_reports_not_created(store):
    _, created_first = submit(store)
    _, created_second = submit(store)
    assert created_first is True
    assert created_second is False


def test_concurrent_submit_is_atomic_across_connections(tmp_path):
    path = tmp_path / "meta.sqlite"
    setup = connect(path)
    apply_migrations(setup)
    setup.close()

    def submit_once(_index: int) -> tuple[str, bool]:
        con = connect(path)
        try:
            return JobStore(con).submit(
                job_type="eod_research",
                trading_day="2026-09-11",
                config_version="cfg-v1",
                input_snapshot_id="snap-syn-001",
                idempotency_namespace="tenant:test|user:alice",
                owner_metadata={
                    "tenant_id": "tenant:test",
                    "actor_user_id": "user:alice",
                },
            )
        finally:
            con.close()

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(submit_once, range(10)))

    assert len({job_id for job_id, _created in results}) == 1
    assert sum(1 for _job_id, created in results if created) == 1
    verify = connect(path, read_only=True)
    try:
        assert verify.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 1
    finally:
        verify.close()


def test_idempotency_namespace_separates_owners(store):
    first, _ = submit(store, idempotency_namespace="tenant:test|user:alice")
    second, _ = submit(store, idempotency_namespace="tenant:test|user:bob")
    assert first != second


def test_different_config_version_is_a_new_job(store):
    """§8.4 幂等键含配置版本：改了配置就是新作业，不得复用旧结果。"""

    submit(store)
    submit(store, config_version="cfg-v2")
    assert store.counts_by_status() == {"PENDING": 2}


# ------------------------------------------------------------------ 状态机
def test_happy_path(store):
    job_id, _ = submit(store)
    job = store.claim("worker-1")
    assert job is not None and job.job_id == job_id
    assert job.status is JobStatus.RUNNING
    assert job.attempt_count == 1
    store.finish(job_id, JobStatus.SUCCEEDED, worker_id="worker-1", attempt_count=1,
                 result={"ok": True})
    assert store.get(job_id).status is JobStatus.SUCCEEDED


def test_terminal_states_cannot_transition(store):
    """A13：状态不倒退。"""

    job_id, _ = submit(store)
    store.claim("w")
    store.finish(job_id, JobStatus.SUCCEEDED, worker_id="w", attempt_count=1)
    for target in (JobStatus.RUNNING, JobStatus.FAILED, JobStatus.PENDING):
        with pytest.raises(JobError) as exc:
            store.finish(job_id, target, worker_id="w")
        assert "illegal transition" in exc.value.message


def test_pending_cannot_jump_to_succeeded(store):
    job_id, _ = submit(store)
    with pytest.raises(JobError):
        store.finish(job_id, JobStatus.SUCCEEDED)


def test_finish_cannot_manufacture_running_without_a_lease(store):
    job_id, _ = submit(store)

    with pytest.raises(JobError):
        store.finish(job_id, JobStatus.RUNNING, worker_id="rogue")

    assert store.get(job_id).status is JobStatus.PENDING
    claimed = store.claim("real-worker", job_id=job_id)
    assert claimed is not None and claimed.lease_owner == "real-worker"


def test_failure_records_error_code(store):
    job_id, _ = submit(store)
    store.claim("w")
    store.finish(job_id, JobStatus.FAILED, worker_id="w", attempt_count=1,
                 error_code="DATA_NOT_READY",
                 error_detail="snapshot missing")
    job = store.get(job_id)
    assert job.status is JobStatus.FAILED
    assert job.error_code == "DATA_NOT_READY"


def test_blocked_can_be_requeued(store):
    """被阻断的作业在外部条件修复后可以重排队。"""

    job_id, _ = submit(store)
    store.claim("w")
    store.finish(job_id, JobStatus.BLOCKED, worker_id="w", attempt_count=1,
                 error_code="DATA_NOT_READY")
    store.finish(job_id, JobStatus.PENDING)
    assert store.get(job_id).status is JobStatus.PENDING


# ------------------------------------------------------------------ 租约
def test_claim_only_returns_one_job_per_worker(store):
    submit(store)
    a = store.claim("worker-a")
    b = store.claim("worker-b")
    assert a is not None
    assert b is None, "已被领取的作业不应被第二个 worker 领走"


def test_heartbeat_requires_lease_ownership(store):
    job_id, _ = submit(store)
    store.claim("worker-a")
    store.heartbeat(job_id, "worker-a", 1)
    with pytest.raises(JobError) as exc:
        store.heartbeat(job_id, "worker-b", 1)
    assert "not held by" in exc.value.message


def test_expired_lease_is_reclaimable(store):
    """A16：worker 崩溃后，租约过期即可被重新领取。"""

    job_id, _ = submit(store)
    store.claim("worker-a", lease_seconds=0)   # 立即过期
    reclaimed = store.claim("worker-b")
    assert reclaimed is not None
    assert reclaimed.job_id == job_id
    assert reclaimed.lease_owner == "worker-b"
    assert reclaimed.attempt_count == 2, "重领必须累加尝试次数，便于诊断反复失败"


def test_reclaimed_job_rejects_stale_worker_callback(store):
    """A13/A16：租约被重领后，旧进程的迟到回调不能覆盖新持有者。"""

    job_id, _ = submit(store)
    store.claim("worker-a", lease_seconds=0, job_id=job_id)
    reclaimed = store.claim("worker-b", job_id=job_id)
    assert reclaimed is not None and reclaimed.lease_owner == "worker-b"

    with pytest.raises(JobError) as exc:
        store.finish(job_id, JobStatus.SUCCEEDED,
                     worker_id="worker-a", attempt_count=1)
    assert "not held by" in exc.value.message
    current = store.get(job_id)
    assert current.status is JobStatus.RUNNING
    assert current.lease_owner == "worker-b"

    store.finish(job_id, JobStatus.SUCCEEDED,
                 worker_id="worker-b", attempt_count=2)
    assert store.get(job_id).status is JobStatus.SUCCEEDED


def test_expired_owner_cannot_finish_before_reclaim(store):
    job_id, _ = submit(store)
    store.claim("worker-a", lease_seconds=0, job_id=job_id)

    with pytest.raises(JobError):
        store.finish(job_id, JobStatus.SUCCEEDED,
                     worker_id="worker-a", attempt_count=1)

    assert store.get(job_id).status is JobStatus.RUNNING
    assert store.claim("worker-b", job_id=job_id) is not None


def test_reclaimed_job_fences_stale_domain_write(store):
    """A16：领域写入与 lease fencing 必须处于同一写事务。"""

    store.con.execute("CREATE TABLE guarded_effect (value TEXT NOT NULL)")
    job_id, _ = submit(store)
    stale = store.claim("worker-a", lease_seconds=0, job_id=job_id)
    assert stale is not None
    reclaimed = store.claim("worker-b", job_id=job_id)
    assert reclaimed is not None

    with pytest.raises(JobError):
        with write_tx(store.con):
            store.assert_lease(job_id, "worker-a", stale.attempt_count)
            store.con.execute("INSERT INTO guarded_effect VALUES ('stale')")

    assert store.con.execute("SELECT COUNT(*) FROM guarded_effect").fetchone()[0] == 0
    with write_tx(store.con):
        store.assert_lease(job_id, "worker-b", reclaimed.attempt_count)
        store.con.execute("INSERT INTO guarded_effect VALUES ('current')")
    assert store.con.execute("SELECT value FROM guarded_effect").fetchone()[0] == "current"


def test_attempt_token_fences_reused_worker_id(store):
    job_id, _ = submit(store)
    first = store.claim("shared-worker", lease_seconds=0, job_id=job_id)
    second = store.claim("shared-worker", job_id=job_id)
    assert first is not None and second is not None
    assert (first.attempt_count, second.attempt_count) == (1, 2)

    with pytest.raises(JobError):
        store.finish(
            job_id, JobStatus.SUCCEEDED,
            worker_id="shared-worker", attempt_count=first.attempt_count,
        )
    with pytest.raises(JobError):
        store.release(
            job_id, "shared-worker", attempt_count=first.attempt_count,
        )
    with pytest.raises(JobError):
        store.heartbeat(job_id, "shared-worker", first.attempt_count)

    current = store.get(job_id)
    assert current.status is JobStatus.RUNNING
    assert current.attempt_count == second.attempt_count


def test_claim_can_target_one_job(store):
    first, _ = submit(store, trading_day="2026-09-07")
    second, _ = submit(store, trading_day="2026-09-08")

    claimed = store.claim("worker", job_id=second)

    assert claimed is not None and claimed.job_id == second
    assert store.get(first).status is JobStatus.PENDING


def test_live_lease_is_not_reclaimable(store):
    submit(store)
    store.claim("worker-a", lease_seconds=3600)
    assert store.claim("worker-b") is None


def test_release_returns_job_to_pending(store):
    job_id, _ = submit(store)
    store.claim("worker-a")
    store.release(job_id, "worker-a", attempt_count=1,
                  reason="quota exhausted")
    job = store.get(job_id)
    assert job.status is JobStatus.PENDING
    assert job.lease_owner is None
    assert job.error_detail == "quota exhausted"


def test_release_requires_ownership(store):
    job_id, _ = submit(store)
    store.claim("worker-a")
    with pytest.raises(JobError):
        store.release(job_id, "worker-b", attempt_count=1)


def test_stale_release_cannot_clear_reclaimed_lease(store):
    job_id, _ = submit(store)
    store.claim("worker-a", lease_seconds=0, job_id=job_id)
    reclaimed = store.claim("worker-b", job_id=job_id)
    assert reclaimed is not None and reclaimed.lease_owner == "worker-b"

    with pytest.raises(JobError):
        store.release(job_id, "worker-a", attempt_count=1)

    current = store.get(job_id)
    assert current.status is JobStatus.RUNNING
    assert current.lease_owner == "worker-b"


def test_features_and_blocked_job_are_distinguishable(store):
    """A13：排队 / 运行 / 等待确认 / 阻断 / 失败 / 取消 / 完成 必须可区分。"""

    a, _ = submit(store, trading_day="2026-09-07")
    b, _ = submit(store, trading_day="2026-09-08")
    c, _ = submit(store, trading_day="2026-09-09")
    store.claim("w")                       # a -> RUNNING
    store.finish(a, JobStatus.FAILED, worker_id="w", attempt_count=1)
    store.claim("w")                       # b -> RUNNING
    store.finish(b, JobStatus.BLOCKED, worker_id="w", attempt_count=1,
                 error_code="DATA_NOT_READY")
    counts = store.counts_by_status()
    assert counts.get("FAILED") == 1
    assert counts.get("BLOCKED") == 1
    assert counts.get("PENDING") == 1      # c 仍在排队


def test_unknown_job_raises_typed_error(store):
    with pytest.raises(JobError) as exc:
        store.get("job_nope")
    assert "unknown job" in exc.value.message


def test_claim_filters_by_job_type(store):
    submit(store, job_type="eod_research")
    submit(store, job_type="weekly_rebalance", trading_day="2026-09-07")
    job = store.claim("w", job_types=["weekly_rebalance"])
    assert job is not None and job.job_type == "weekly_rebalance"
