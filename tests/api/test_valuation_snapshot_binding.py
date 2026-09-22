"""P0 regression tests for valuation snapshot provenance."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "integration"))

from main import SNAPSHOT_ID, build_state, create_app  # noqa: E402
from aquant.domain.data.ingest import SnapshotBuilder  # noqa: E402
from aquant.operations.snapshot_lifecycle import write_current_pointer  # noqa: E402
from test_m1_ingest_e2e import build_snapshot  # noqa: E402


VALUATION_DAY = "2026-09-11"


def _state_with_two_snapshots(tmp_path):
    state = build_state(tmp_path)
    builder = SnapshotBuilder(state.con, state.root / "datasets")
    build_snapshot(
        state.con, builder, state.store, snapshot_id="snap-syn-002",
    )
    write_current_pointer(state.data_dir, "snap-syn-002")
    # Make the moving pointer advance before the request.  The valuation route
    # must still use the explicitly requested older snapshot.
    assert state.current_snapshot() == "snap-syn-002"
    return state


def test_valuation_keeps_requested_snapshot_after_current_advances(tmp_path):
    state = _state_with_two_snapshots(tmp_path)
    app = create_app(state=state)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/valuations",
            json={
                "portfolio_id": "pf-provenance",
                "snapshot_id": SNAPSHOT_ID,
                "trading_day": VALUATION_DAY,
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["snapshot_id"] == SNAPSHOT_ID
    assert body["as_of"] == state.reader.ref(SNAPSHOT_ID).as_of_time.isoformat()
    row = state.con.execute(
        "SELECT snapshot_id,execution_plan_id,as_of FROM valuation_provenance "
        "WHERE portfolio_id=? AND trading_day=?",
        ("pf-provenance", VALUATION_DAY),
    ).fetchone()
    assert row["snapshot_id"] == SNAPSHOT_ID
    assert row["execution_plan_id"] is None
    assert row["as_of"]


def test_valuation_rejects_different_binding_for_same_portfolio_day(tmp_path):
    state = _state_with_two_snapshots(tmp_path)
    app = create_app(state=state)

    with TestClient(app) as client:
        first = client.post(
            "/api/v1/valuations",
            json={
                "portfolio_id": "pf-provenance",
                "snapshot_id": SNAPSHOT_ID,
                "trading_day": VALUATION_DAY,
            },
        )
        second = client.post(
            "/api/v1/valuations",
            json={
                "portfolio_id": "pf-provenance",
                "snapshot_id": "snap-syn-002",
                "trading_day": VALUATION_DAY,
            },
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "STALE_SNAPSHOT"
    row = state.con.execute(
        "SELECT snapshot_id FROM valuation_provenance "
        "WHERE portfolio_id=? AND trading_day=?",
        ("pf-provenance", VALUATION_DAY),
    ).fetchone()
    assert row["snapshot_id"] == SNAPSHOT_ID
