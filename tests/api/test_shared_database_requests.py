"""The workbench's parallel bootstrap must not overlap shared SQLite access."""
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps/api"))
from main import SNAPSHOT_ID, build_state, create_app


def test_financial_replay_does_not_block_shared_workspace_requests(tmp_path, monkeypatch):
    import main
    monkeypatch.setenv("AQUANT_IDENTITY_MODE", "LOCAL_LOOPBACK_DEMO")
    monkeypatch.setenv("AQUANT_TRIAL_SUBJECT", "user:demo")
    monkeypatch.delenv("AQUANT_S2_TARGET_PERIOD_MAP", raising=False)
    state = build_state(tmp_path)
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(main, "load_simulation_research", lambda path: ({"provenance": []}, {}))
    def paused_replay(**kwargs):
        assert kwargs["snapshot_id"] == SNAPSHOT_ID
        entered.set()
        assert release.wait(timeout=10), "test must release the independent proof reader"
        return {"snapshotId": kwargs["snapshot_id"], "fixture": True}
    monkeypatch.setattr(main, "build_financial_coverage_report", paused_replay)
    with TestClient(create_app(state)) as client, ThreadPoolExecutor(max_workers=2) as pool:
        financial = pool.submit(client.get, "/api/v1/research/financial-coverage",
                                headers={"X-Aquant-Subject": "user:demo"})
        try:
            assert entered.wait(timeout=5)
            status = pool.submit(client.get, "/api/v1/status").result(timeout=5)
            assert status.status_code == 200 and not release.is_set()
            assert status.json()["snapshotId"] == SNAPSHOT_ID
        finally:
            release.set()
        assert financial.result(timeout=5).json() == {"snapshotId": SNAPSHOT_ID, "fixture": True}
    state.con.close()


@pytest.mark.parametrize("snapshot_id,expected", [(SNAPSHOT_ID, 200), ("snap-unpublished-financial", 503)])
def test_isolated_financial_route_keeps_identity_and_published_snapshot_gate(tmp_path, monkeypatch, snapshot_id, expected):
    import main
    monkeypatch.setenv("AQUANT_IDENTITY_MODE", "LOCAL_LOOPBACK_DEMO")
    monkeypatch.setenv("AQUANT_TRIAL_SUBJECT", "user:demo")
    monkeypatch.delenv("AQUANT_S2_TARGET_PERIOD_MAP", raising=False)
    state = build_state(tmp_path)
    monkeypatch.setenv("AQUANT_SNAPSHOT_ID", snapshot_id)
    def shared_snapshot_forbidden():
        raise AssertionError("financial diagnostics must not switch shared service state")
    monkeypatch.setattr(state, "current_snapshot", shared_snapshot_forbidden)
    monkeypatch.setattr(main, "load_simulation_research", lambda path: ({"provenance": []}, {}))
    calls = []
    monkeypatch.setattr(main, "build_financial_coverage_report",
        lambda **kwargs: calls.append(kwargs["snapshot_id"]) or {"snapshotId": kwargs["snapshot_id"]})
    with TestClient(create_app(state)) as client:
        path = "/api/v1/research/financial-coverage"
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"X-Aquant-Subject": "user:other"}).status_code == 403
        response = client.get(path, headers={"X-Aquant-Subject": "user:demo"})
        assert response.status_code == expected
        assert calls == ([SNAPSHOT_ID] if expected == 200 else [])
        if expected == 503:
            assert response.json()["detail"]["code"] == "FINANCIAL_COVERAGE_NOT_READY"
    state.con.close()


def test_parallel_workspace_bootstrap_has_one_consistent_snapshot(tmp_path):
    state = build_state(tmp_path)
    paths = ["/api/v1/status", "/api/v1/candidates", "/api/v1/events",
             "/api/v1/snapshots", "/api/v1/plans?portfolio_id=pf-syn-m"]
    with TestClient(create_app(state)) as client, ThreadPoolExecutor(max_workers=5) as pool:
        for _ in range(3):
            responses = list(pool.map(client.get, paths))
            assert [r.status_code for r in responses] == [200] * 5
            status, candidates, events, snapshots, plans = [r.json() for r in responses]
            assert status["snapshotId"] == candidates["snapshotId"] == events["snapshotId"] == SNAPSHOT_ID
            assert any(s["snapshotId"] == SNAPSHOT_ID for s in snapshots["snapshots"])
            assert plans["portfolioId"] == "pf-syn-m"


def test_shared_connection_requests_do_not_overlap_and_release_after_failure(tmp_path):
    state = build_state(tmp_path)
    app = create_app(state)
    counts = {"active": 0, "maximum": 0}
    guard = threading.Lock()

    @app.get("/api/v1/test/shared-connection")
    def probe(fail: str = ""):
        with guard:
            counts["active"] += 1
            counts["maximum"] = max(counts["maximum"], counts["active"])
        try:
            state.con.execute("SELECT 1").fetchone()
            time.sleep(0.03)  # Keep DB-dependent request work in progress across dispatches.
            if fail == "handled":
                raise HTTPException(409, "fixture failure")
            if fail == "unhandled":
                raise RuntimeError("fixture crash")
            return {"snapshotId": state.current_snapshot()}
        finally:
            with guard:
                counts["active"] -= 1

    # create_app mounts static files last; put this test route ahead of that mount.
    app.router.routes.insert(0, app.router.routes.pop())
    with TestClient(app, raise_server_exceptions=False) as client, ThreadPoolExecutor(max_workers=8) as pool:
        paths = ["/api/v1/test/shared-connection?fail=handled", "/api/v1/test/shared-connection?fail=unhandled"] + ["/api/v1/test/shared-connection"] * 14
        responses = list(pool.map(client.get, paths))
        assert responses[0].status_code == 409
        assert responses[1].status_code == 500
        assert all(r.status_code == 200 and r.json()["snapshotId"] == SNAPSHOT_ID for r in responses[2:])
        assert counts == {"active": 0, "maximum": 1}
        assert client.get("/api/v1/status").status_code == 200
