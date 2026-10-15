"""A user's explicit keep decision persists without changing simulation facts."""
from pathlib import Path
import sys

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/api"))
sys.path.insert(0, str(ROOT / "src"))
from main import build_state, create_app

USER = {"X-Aquant-Subject": "user:alice"}


def _simulation_facts(con):
    tables = (
        "portfolio", "simulation_plan", "plan_confirmation", "order", "fill",
        "fee_charge", "position_lot", "lot_consumption", "cash_entry",
        "receivable", "valuation", "valuation_position",
    )
    return {table: [tuple(row) for row in con.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
            for table in tables}


def test_no_change_saves_visible_snapshot_and_reason_without_overwriting_or_trading(tmp_path, monkeypatch):
    monkeypatch.setenv("AQUANT_IDENTITY_MODE", "local_loopback_demo")
    monkeypatch.setenv("AQUANT_TRIAL_SUBJECT", "user:alice")
    state = build_state(tmp_path)
    try:
        with TestClient(create_app(state=state)) as client:
            visible_snapshot = client.get("/api/v1/status").json()["snapshotId"]
            proposed = {"orders": [{"instrument_id": "SYN.A.600519", "quantity": 100}]}
            original = client.post("/api/v1/decisions", headers=USER, json={
                "portfolio_id": "pf-syn-m", "snapshot_id": visible_snapshot,
                "decision_type": "ACCEPT_MODEL", "model_proposed": proposed,
                "human_final": proposed, "reason_note": "原判断保留原意",
            })
            assert original.status_code == 200, original.text
            original_id = original.json()["decision_id"]
            original_row = dict(state.con.execute(
                "SELECT * FROM decision_log WHERE decision_id=?", (original_id,)).fetchone())
            before = _simulation_facts(state.con)
            final = {"action": "NO_CHANGE", "orders": []}

            saved = client.post("/api/v1/decisions", headers=USER, json={
                "portfolio_id": "pf-syn-m", "snapshot_id": visible_snapshot,
                "decision_type": "NO_CHANGE", "plan_id": None,
                "model_proposed": proposed, "human_final": final,
                "reason_category": "NO_CHANGE", "reason_note": "估值仍偏高，维持当前组合",
                "external_information_used": False, "rule_check": None,
            })
            assert saved.status_code == 200, saved.text
            identifier = saved.json()["decision_id"]
            rows = client.get("/api/v1/decisions", params={"portfolio_id": "pf-syn-m"}).json()["decisions"]
            record = next(row for row in rows if row["decision_id"] == identifier)
            assert record["decision_type"] == "NO_CHANGE"
            assert record["portfolio_id"] == "pf-syn-m"
            assert record["snapshot_id"] == visible_snapshot
            assert record["plan_id"] is None
            assert record["model_proposed"] == proposed
            assert record["human_final"] == final
            assert record["reason_note"] == "估值仍偏高，维持当前组合"
            assert record["reason_category"] == "NO_CHANGE"
            assert record["external_information_used"] is False
            assert record["rule_check"] is None
            assert record["diff"]["identical"] is False
            assert dict(state.con.execute(
                "SELECT * FROM decision_log WHERE decision_id=?", (original_id,)).fetchone()) == original_row
            assert _simulation_facts(state.con) == before
    finally:
        state.con.close()
