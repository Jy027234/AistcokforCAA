"""Read-only engineering records stay scoped and cannot become user decisions."""
import sys
from pathlib import Path
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'apps/api'), str(ROOT / 'src')]
import main

def test_forward_route_precedes_generic_comparison_id_and_empty_results_are_real(tmp_path):
    state = main.build_state(tmp_path)
    try:
        with TestClient(main.create_app(state=state)) as client:
            response = client.get('/api/v1/comparisons/forward', headers={'X-Aquant-Subject': 'user:alice'})
            assert response.status_code == 200, response.text
            assert response.json()['series'] == [] and response.json()['count'] == 0
            response = client.get('/api/v1/engineering/simulation/runs', headers={'X-Aquant-Subject': 'user:alice'})
            assert response.status_code == 200 and response.json()['runs'] == []
            response = client.get('/api/v1/engineering/simulation/attributions', headers={'X-Aquant-Subject': 'user:alice'})
            assert response.status_code == 200 and response.json()['reports'] == [] and response.json()['count'] == 0
    finally:
        state.con.close()

def test_engineering_results_filter_other_subject_without_record_mutation(tmp_path, monkeypatch):
    state = main.build_state(tmp_path)
    rows = [{'runId': 'eng-local-alice-fixture', 'subjectId': 'user:alice', 'humanConfirmed': False},
            {'runId': 'eng-local-bob-fixture', 'subjectId': 'user:bob', 'humanConfirmed': False}]
    monkeypatch.setattr(main, 'list_engineering_runs', lambda root: rows)
    try:
        with TestClient(main.create_app(state=state)) as client:
            response = client.get('/api/v1/engineering/simulation/runs', headers={'X-Aquant-Subject': 'user:alice'})
            assert response.status_code == 200
            assert response.json()['runs'] == [rows[0]]
            assert response.json()['count'] == 1 and rows[0]['humanConfirmed'] is False
            assert state.con.execute('SELECT COUNT(*) FROM decision_log').fetchone()[0] == 0
    finally:
        state.con.close()
