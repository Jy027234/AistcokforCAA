"""Published optional financials and observed official coverage stay distinct."""
import sys
from pathlib import Path
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'apps/api'), str(ROOT / 'src')]
import main


def test_coverage_uses_current_snapshot_frozen_research_and_explicit_live_db(tmp_path, monkeypatch):
    state = main.build_state(tmp_path)
    source = tmp_path / 'frozen-research.sqlite'
    live = tmp_path / 'observed-facts.sqlite'
    monkeypatch.setenv('AQUANT_LIVE_FINANCIAL_FACTS_DB', str(live))
    monkeypatch.setattr(main, 'load_simulation_research', lambda path: (
        {'provenance': [{'role': 'facts_db', 'path': str(source)}]}, {}))
    captured = {}
    def coverage(**kwargs):
        captured.update(kwargs)
        return {'schema': 'aquant.financial_coverage.v1', 'formalS2': {'strategyFamilyStatus': 'CLOSED'},
                's1Optional': {'statementSecurityCount': 0}, 'historicalReconstructed': {'pitMode': 'HISTORICAL_RECONSTRUCTED'}}
    monkeypatch.setattr(main, 'build_financial_coverage_report', coverage)
    try:
        with TestClient(main.create_app(state=state)) as client:
            response = client.get('/api/v1/research/financial-coverage', headers={'X-Aquant-Subject': 'user:alice'})
            assert response.status_code == 200, response.text
            assert response.json()['formalS2']['strategyFamilyStatus'] == 'CLOSED'
            assert captured['snapshot_id'] == state.current_snapshot()
            assert captured['snapshot_dir'] == state.data_dir
            assert captured['live_facts_db'] == live and captured['reconstructed_facts_db'] == source
            assert captured['priority_instrument_ids'] == set()
            assert state.con.execute('SELECT COUNT(*) FROM decision_log').fetchone()[0] == 0
    finally:
        state.con.close()


def test_coverage_invalid_provenance_reports_failure_without_financial_fallback(tmp_path, monkeypatch):
    state = main.build_state(tmp_path)
    def invalid(path):
        raise ValueError('frozen research input hash changed')
    monkeypatch.setattr(main, 'load_simulation_research', invalid)
    try:
        with TestClient(main.create_app(state=state)) as client:
            response = client.get('/api/v1/research/financial-coverage', headers={'X-Aquant-Subject': 'user:alice'})
            assert response.status_code == 503
            assert response.json()['detail']['code'] == 'FINANCIAL_COVERAGE_NOT_READY'
    finally:
        state.con.close()
