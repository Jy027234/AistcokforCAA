"""Read original persisted judgment without creating or rewriting decision facts."""
from pathlib import Path
import sys

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/api"))
sys.path.insert(0, str(ROOT / "src"))
from main import build_state, create_app

USER = {"X-Aquant-Subject": "user:alice"}


def test_decision_history_reads_original_plans_checks_and_reason_without_mutation(tmp_path):
    state = build_state(tmp_path)
    model = {"orders": [{"instrument_id": "SYN.A.600519", "quantity": 100}],
             "origin": {"research_snapshot": "original-only", "input_hash": "original-content-hash"}}
    human = {"orders": [{"instrument_id": "SYN.A.600519", "quantity": 0}],
             "original_reason": "看了原报告，暂不纳入"}
    checks = {"all_passed": False, "checks": [{"code": "ORIGINAL_RULE", "passed": False}]}
    try:
        with TestClient(create_app(state=state)) as client:
            saved = client.post("/api/v1/decisions", headers=USER, json={
                "portfolio_id": "pf-syn-m", "decision_type": "MODIFY_MODEL",
                "model_proposed": model, "human_final": human, "rule_check": checks,
                "reason_category": "ORIGINAL_RESEARCH", "reason_note": "当时理由不能用新判断覆盖",
                "external_information_used": True,
            })
            assert saved.status_code == 200, saved.text
            identifier = saved.json()["decision_id"]
            before = dict(state.con.execute("SELECT * FROM decision_log WHERE decision_id=?", (identifier,)).fetchone())
            first = client.get("/api/v1/decisions", params={"portfolio_id": "pf-syn-m"})
            second = client.get("/api/v1/decisions", params={"portfolio_id": "pf-syn-m"})
            assert first.status_code == second.status_code == 200
            assert first.json() == second.json()
            row = next(d for d in first.json()["decisions"] if d["decision_id"] == identifier)
            assert row["model_proposed"] == model
            assert row["human_final"] == human
            assert row["rule_check"] == checks
            assert row["reason_note"] == "当时理由不能用新判断覆盖"
            assert row["external_information_used"] is True
            assert row["submitted_at"] == before["submitted_at"]
            assert row["plan_id"] is None
            assert row["snapshot_id"] == before["snapshot_id"]
            assert dict(state.con.execute("SELECT * FROM decision_log WHERE decision_id=?", (identifier,)).fetchone()) == before
    finally:
        state.con.close()


def test_decision_history_preserves_missing_original_payloads(tmp_path):
    state = build_state(tmp_path)
    try:
        with TestClient(create_app(state=state)) as client:
            saved = client.post("/api/v1/decisions", headers=USER, json={
                "portfolio_id": "pf-syn-m", "decision_type": "NO_CHANGE", "reason_note": "今天无需调整"})
            assert saved.status_code == 200, saved.text
            rows = client.get("/api/v1/decisions", params={"portfolio_id": "pf-syn-m"}).json()["decisions"]
            row = next(d for d in rows if d["decision_id"] == saved.json()["decision_id"])
            assert row["model_proposed"] is None
            assert row["human_final"] is None
            assert row["rule_check"] is None
            assert row["diff"]["comparable"] is False
    finally:
        state.con.close()
