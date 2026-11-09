from __future__ import annotations

import hashlib
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "apps" / "api"), str(ROOT / "src")]

import main  # noqa: E402
from aquant.operations.financial_coverage_jobs import JOB_FINANCIAL_CHECK  # noqa: E402
from aquant.operations.jobs import JobStatus, JobStore  # noqa: E402


ALICE = {"X-Aquant-Subject": "user:alice"}
BOB = {"X-Aquant-Subject": "user:bob"}


def _binding(snapshot_id: str, trading_day: str, cutoff: str, revision: str) -> dict:
    value = {
        "snapshotId": snapshot_id,
        "snapshotCutoffAt": cutoff,
        "tradingDay": trading_day,
        "researchSnapshotId": "research-snapshot-1",
        "researchContentHash": "sha256:" + revision * 64,
        "liveFactsVersion": "sha256:" + "1" * 64,
        "proofVersion": "sha256:" + "2" * 64,
        "reconstructedFactsVersion": "sha256:" + "3" * 64,
        "targetMapHash": "sha256:" + "4" * 64,
        "priorityInstrumentIdsHash": "sha256:" + "5" * 64,
        "codeVersion": main.FINANCIAL_CHECK_VERSION,
    }
    value["inputHash"] = "sha256:" + hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return value


@pytest.fixture()
def api(tmp_path, monkeypatch):
    state = main.build_state(tmp_path)
    current = {"revision": "a"}

    def capture(*, data_dir, snapshot_id, trading_day, snapshot_cutoff_at, subject):
        binding = _binding(snapshot_id, trading_day, snapshot_cutoff_at,
                           current["revision"])
        return {
            "inputBinding": binding,
            "inputSpec": {"dataDir": str(data_dir)},
            "idempotencyNamespace": f"test:{subject}:{binding['inputHash']}",
        }

    monkeypatch.setattr(main, "capture_financial_check_inputs", capture)
    app = main.create_app(state=state)
    with TestClient(app) as client:
        yield client, state, current
    state.con.close()


def _submit(client):
    return client.post("/api/v1/research/financial-coverage/jobs", headers=ALICE)


def test_slow_financial_submission_keeps_workspace_reads_available(api, monkeypatch):
    client, state, _ = api
    entered, release = threading.Event(), threading.Event()
    original_capture = main.capture_financial_check_inputs

    def paused_capture(**kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return original_capture(**kwargs)

    monkeypatch.setattr(main, "capture_financial_check_inputs", paused_capture)
    with ThreadPoolExecutor(max_workers=2) as pool:
        submission = pool.submit(_submit, client)
        try:
            assert entered.wait(timeout=5)
            for path in ("/api/v1/health", "/api/v1/status", "/api/v1/schedule"):
                response = pool.submit(client.get, path).result(timeout=5)
                assert response.status_code == 200
            assert not release.is_set()
        finally:
            release.set()
        response = submission.result(timeout=5)
        assert response.status_code == 202
        job_id = response.json()["jobId"]
        assert client.get("/api/v1/research/jobs/" + job_id, headers=ALICE).status_code == 200
    assert state.con.execute("SELECT count(*) FROM job WHERE job_type='FINANCIAL_CHECK'").fetchone()[0] == 1


def _finish(state, job_id: str, status: JobStatus, *, error_code=None, error_detail=None,
            result=None):
    store = JobStore(state.con)
    claimed = store.claim("test-financial-worker", job_types=[JOB_FINANCIAL_CHECK],
                          job_id=job_id)
    assert claimed is not None
    store.finish(job_id, status, worker_id=claimed.lease_owner,
                 attempt_count=claimed.attempt_count, error_code=error_code,
                 error_detail=error_detail, result=result)


def test_submit_is_enqueue_only_and_same_bound_input_deduplicates(api, monkeypatch):
    client, state, _current = api

    def unexpected_full_report(**_kwargs):
        raise AssertionError("job submission must not run the synchronous report")

    monkeypatch.setattr(main, "build_financial_coverage_report", unexpected_full_report)
    first = _submit(client)
    second = _submit(client)

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    a, b = first.json(), second.json()
    assert a["jobType"] == JOB_FINANCIAL_CHECK
    assert a["status"] == "PENDING" and a["created"] is True
    assert b["jobId"] == a["jobId"] and b["created"] is False
    assert a["snapshotId"] == state.snapshot_id
    assert a["inputBinding"]["snapshotId"] == state.snapshot_id
    assert state.con.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 1
    _current["revision"] = "b"
    changed = _submit(client)
    assert changed.status_code == 202, changed.text
    assert changed.json()["created"] is True
    assert changed.json()["jobId"] != a["jobId"]
    assert state.con.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 2


def test_status_returns_bound_result_and_is_subject_scoped(api, monkeypatch):
    client, state, current = api
    created = _submit(client).json()
    report = {
        "schema": "aquant.financial_coverage.v1",
        "snapshotId": created["snapshotId"],
        "formalS2": {"strategyFamilyStatus": "CLOSED"},
    }
    result = {"report": report, "verifiedAt": "2026-11-01T01:02:03+00:00",
              "binding": created["inputBinding"], "resultHash": "sha256:" + "a" * 64}
    monkeypatch.setattr(main, "read_financial_check_result", lambda _data_dir, _job: result)
    _finish(state, created["jobId"], JobStatus.SUCCEEDED,
            result={"artifactId": created["jobId"], "resultHash": result["resultHash"]})

    response = client.get(f"/api/v1/research/jobs/{created['jobId']}", headers=ALICE)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "SUCCEEDED"
    assert body["snapshotId"] == created["snapshotId"]
    assert body["inputBinding"] == created["inputBinding"]
    assert body["result"]["report"]["formalS2"]["strategyFamilyStatus"] == "CLOSED"
    assert body["isCurrent"] is True

    current["revision"] = "b"
    stale = client.get(f"/api/v1/research/jobs/{created['jobId']}", headers=ALICE)
    assert stale.status_code == 200
    assert stale.json()["result"]["resultHash"] == result["resultHash"]
    assert stale.json()["isCurrent"] is False

    hidden = client.get(f"/api/v1/research/jobs/{created['jobId']}", headers=BOB)
    assert hidden.status_code == 404


def test_failed_status_is_safe_and_explicit_retry_deduplicates(api):
    client, state, _current = api
    parent = _submit(client).json()
    _finish(state, parent["jobId"], JobStatus.FAILED,
            error_code="FINANCIAL_CHECK_INPUT_NOT_READY",
            error_detail="secret path C:\\private\\facts.sqlite")

    failed = client.get(f"/api/v1/research/jobs/{parent['jobId']}", headers=ALICE)
    assert failed.status_code == 200
    assert failed.json()["error"]["code"] == "FINANCIAL_CHECK_INPUT_NOT_READY"
    assert "private" not in failed.text

    path = f"/api/v1/research/financial-coverage/jobs/{parent['jobId']}/retry"
    first = client.post(path, headers=ALICE)
    again = client.post(path, headers=ALICE)
    assert first.status_code == 202, first.text
    assert again.status_code == 202, again.text
    child, duplicate = first.json(), again.json()
    assert child["status"] == "PENDING" and child["created"] is True
    assert duplicate["jobId"] == child["jobId"] and duplicate["created"] is False
    assert child["jobId"] != parent["jobId"]
    assert child["retryOf"] == parent["jobId"]
    assert duplicate["retryOf"] == parent["jobId"]
    assert state.con.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 2

    reopened = _submit(client).json()
    assert reopened["jobId"] == child["jobId"]
    assert reopened["status"] == "PENDING" and reopened["created"] is False
    assert reopened["retryOf"] == parent["jobId"]
    _finish(state, child["jobId"], JobStatus.FAILED,
            error_code="FINANCIAL_CHECK_NOT_READY", error_detail="second failure")
    grandchild = client.post(
        f"/api/v1/research/financial-coverage/jobs/{child['jobId']}/retry", headers=ALICE).json()
    _finish(state, grandchild["jobId"], JobStatus.SUCCEEDED, result={"fixture": True})
    resumed = _submit(client).json()
    assert resumed["jobId"] == grandchild["jobId"]
    assert resumed["status"] == "SUCCEEDED" and resumed["created"] is False
    assert resumed["retryOf"] == child["jobId"]
    assert state.con.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 3
    assert client.get(f"/api/v1/research/jobs/{parent['jobId']}", headers=ALICE).json()["status"] == "FAILED"


def test_retry_requires_failed_parent_and_unchanged_input(api):
    client, state, current = api
    parent = _submit(client).json()
    path = f"/api/v1/research/financial-coverage/jobs/{parent['jobId']}/retry"
    not_failed = client.post(path, headers=ALICE)
    assert not_failed.status_code == 409
    assert not_failed.json()["detail"]["error"]["code"] == "FINANCIAL_CHECK_NOT_RETRYABLE"

    _finish(state, parent["jobId"], JobStatus.FAILED,
            error_code="FINANCIAL_CHECK_NOT_READY", error_detail="failed")
    current["revision"] = "b"
    changed = client.post(path, headers=ALICE)
    assert changed.status_code == 409
    assert changed.json()["detail"]["error"]["code"] == "FINANCIAL_CHECK_INPUT_CHANGED"
    assert state.con.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 1


def test_status_hides_other_job_types(api):
    client, state, _current = api
    row = JobStore(state.con).submit(
        job_type="FACTOR_COMPUTE", trading_day="2026-10-30",
        config_version="f10-v2", input_snapshot_id=state.snapshot_id,
        payload={"limit": 1},
    )[0]
    response = client.get(f"/api/v1/research/jobs/{row}", headers=ALICE)
    assert response.status_code == 404
