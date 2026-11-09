from __future__ import annotations

import json
import threading
import time
from contextlib import closing
from datetime import datetime, timezone

from aquant.domain.data.db import apply_migrations, connect
from aquant.operations.jobs import JobStore
from aquant.operations.research_jobs import JOB_FACTOR_COMPUTE, JOB_FINANCIAL_CHECK
from aquant.operations import research_jobs
from tools import research_job_worker as worker


def _database(data_dir):
    meta = data_dir / "meta.sqlite"
    con = connect(meta)
    apply_migrations(con)
    return meta, con


def _submit(con, job_type, *, snapshot_id):
    config = "financial-check-v1" if job_type == JOB_FINANCIAL_CHECK else "f10-v2"
    return JobStore(con).submit(
        job_type=job_type, trading_day="2026-10-30", config_version=config,
        input_snapshot_id=snapshot_id, payload={"test": True},
    )[0]


def test_worker_once_consumes_one_financial_job_and_leaves_factor_jobs_queued(tmp_path, monkeypatch):
    _meta, con = _database(tmp_path)
    financial_id = _submit(con, JOB_FINANCIAL_CHECK, snapshot_id="snap-financial")
    factor_id = _submit(con, JOB_FACTOR_COMPUTE, snapshot_id="snap-factor")
    con.close()

    monkeypatch.setattr(research_jobs, "_dispatch",
                        lambda _con, _reader, job, **_kwargs: {"processed": job.job_id})
    result = worker.main(["--once", "--data-dir", str(tmp_path)])
    assert result == 0

    with closing(connect(tmp_path / "meta.sqlite", read_only=True)) as check:
        assert check.execute("SELECT status FROM job WHERE job_id=?",
                             (financial_id,)).fetchone()[0] == "SUCCEEDED"
        assert check.execute("SELECT status FROM job WHERE job_id=?",
                             (factor_id,)).fetchone()[0] == "PENDING"
    heartbeat = json.loads((tmp_path / worker.HEARTBEAT_FILE).read_text(encoding="utf-8"))
    assert heartbeat["status"] == "STOPPED"


def test_worker_renews_a_shortened_lease_while_financial_job_runs(tmp_path, monkeypatch):
    meta_path, con = _database(tmp_path)
    job_id = _submit(con, JOB_FINANCIAL_CHECK, snapshot_id="snap-financial")
    worker_id = "financial-heartbeat-test"
    claimed = JobStore(con).claim(worker_id, lease_seconds=0.05,
                                  job_types=[JOB_FINANCIAL_CHECK])
    assert claimed is not None and claimed.job_id == job_id
    con.close()

    monkeypatch.setattr(worker, "WORKER_PULSE_SECONDS", 0.02)
    monkeypatch.setattr(worker, "JOB_RENEW_SECONDS", 0.04)
    monkeypatch.setattr(worker, "JOB_LEASE_SECONDS", 0.25)
    stop = threading.Event()
    heartbeat_path = tmp_path / worker.HEARTBEAT_FILE
    thread = threading.Thread(
        target=worker._heartbeat_loop,
        args=(meta_path, heartbeat_path, worker_id, datetime.now(timezone.utc), stop),
        daemon=True,
    )
    thread.start()
    time.sleep(0.14)
    try:
        with closing(connect(meta_path)) as observer:
            stolen = JobStore(observer).claim(
                "intruder", lease_seconds=1,
                job_types=[JOB_FINANCIAL_CHECK],
            )
            assert stolen is None
    finally:
        stop.set()
        thread.join(timeout=1)
    with closing(connect(meta_path, read_only=True)) as check:
        row = check.execute("SELECT status,lease_owner,attempt_count FROM job WHERE job_id=?",
                            (job_id,)).fetchone()
    assert tuple(row) == ("RUNNING", worker_id, 1)


def test_slow_financial_dispatch_keeps_live_lease_until_completion(tmp_path, monkeypatch):
    meta_path, con = _database(tmp_path)
    job_id = _submit(con, JOB_FINANCIAL_CHECK, snapshot_id="snap-financial")
    con.close()
    started = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(worker, "WORKER_PULSE_SECONDS", 0.01)
    monkeypatch.setattr(worker, "JOB_RENEW_SECONDS", 0.02)
    monkeypatch.setattr(worker, "JOB_LEASE_SECONDS", 0.08)

    def slow_dispatch(_con, _reader, job, **_kwargs):
        assert job.job_type == JOB_FINANCIAL_CHECK
        started.set()
        assert release.wait(2)
        return {"processed": job.job_id}

    monkeypatch.setattr(research_jobs, "_dispatch", slow_dispatch)
    outcomes = []
    process = threading.Thread(
        target=lambda: outcomes.append(worker.main(["--once", "--data-dir", str(tmp_path)])),
        daemon=True,
    )
    process.start()
    assert started.wait(2)
    time.sleep(0.35)
    try:
        with closing(connect(meta_path)) as observer:
            stolen = JobStore(observer).claim(
                "intruder", lease_seconds=1, job_types=[JOB_FINANCIAL_CHECK],
            )
            assert stolen is None
    finally:
        release.set()
        process.join(timeout=2)
    assert not process.is_alive()
    assert outcomes == [0]
    with closing(connect(meta_path, read_only=True)) as check:
        row = check.execute("SELECT status,attempt_count FROM job WHERE job_id=?",
                            (job_id,)).fetchone()
    assert tuple(row) == ("SUCCEEDED", 1)


def test_worker_recovers_an_expired_job_after_previous_process_exit(tmp_path, monkeypatch):
    _meta, con = _database(tmp_path)
    job_id = _submit(con, JOB_FINANCIAL_CHECK, snapshot_id="snap-financial")
    abandoned = JobStore(con).claim(
        "previous-process", lease_seconds=0,
        job_types=[JOB_FINANCIAL_CHECK],
    )
    assert abandoned is not None and abandoned.job_id == job_id
    con.close()

    monkeypatch.setattr(research_jobs, "_dispatch",
                        lambda _con, _reader, job, **_kwargs: {"processed": job.job_id})
    assert worker.main(["--once", "--data-dir", str(tmp_path)]) == 0
    with closing(connect(tmp_path / "meta.sqlite", read_only=True)) as check:
        row = check.execute("SELECT status,attempt_count FROM job WHERE job_id=?",
                            (job_id,)).fetchone()
    assert tuple(row) == ("SUCCEEDED", 2)


def test_healthcheck_requires_a_fresh_running_heartbeat(tmp_path):
    heartbeat_path = tmp_path / worker.HEARTBEAT_FILE
    worker._write_heartbeat(heartbeat_path, worker_id="worker-test",
                            started_at=datetime.now(timezone.utc))
    assert worker.main(["--healthcheck", "--data-dir", str(tmp_path)]) == 0
    payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    payload["heartbeatAt"] = "2020-01-01T00:00:00+00:00"
    heartbeat_path.write_text(json.dumps(payload), encoding="utf-8")
    assert worker.main(["--healthcheck", "--data-dir", str(tmp_path)]) == 1
