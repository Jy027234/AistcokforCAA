"""Readonly comparison artifact routes preserve engineering/user origin labels."""
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/api"))
from main import build_state, create_app
from aquant.domain.experiments.comparison import ComparisonStore


def test_readonly_comparison_routes_preserve_saved_engineering_result(tmp_path):
    state = build_state(tmp_path / "api")
    result = {"run_id": "cmp-local-fixture", "status": "COMPLETED", "data_mode": "SYNTHETIC",
              "human_origin": "ENGINEERING_REPLAY", "portfolios": {}, "limitations": ["local contract fixture"]}
    store = ComparisonStore(state.data_dir / "comparisons")
    store.write_once("cmp-local-fixture.result.json", result)
    with TestClient(create_app(state=state)) as client:
        response = client.get("/api/v1/comparisons")
        assert response.status_code == 200 and response.json()["results"] == [result]
        assert client.get("/api/v1/comparisons/cmp-local-fixture").json() == result
        missing = client.get("/api/v1/comparisons/cmp-unknown")
        assert missing.status_code == 404 and missing.json()["detail"]["code"] == "COMPARISON_RESULT_NOT_FOUND"
        assert client.get("/api/v1/comparisons/invalid-id").status_code == 404
        assert state.con.execute("SELECT COUNT(*) FROM simulation_plan").fetchone()[0] == 0


def test_empty_results_do_not_invent_returns(tmp_path):
    state = build_state(tmp_path / "api")
    with TestClient(create_app(state=state)) as client:
        response = client.get("/api/v1/comparisons")
        assert response.status_code == 200 and response.json()["results"] == []
