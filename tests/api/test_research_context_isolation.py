"""Slow qualification keeps its own read transaction and leaves the workbench usable."""
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'apps/api'))
import main


def test_qualification_read_is_independent_read_only_and_does_not_block_status(tmp_path, monkeypatch):
    monkeypatch.setenv('AQUANT_IDENTITY_MODE', 'LOCAL_LOOPBACK_DEMO')
    monkeypatch.setenv('AQUANT_TRIAL_SUBJECT', 'user:demo')
    state = main.build_state(tmp_path)
    app = main.create_app(state)
    app.state.aquant_context_isolated = True
    entered, release = threading.Event(), threading.Event()

    def paused_read(path):
        entered.set()
        assert release.wait(10)
        return {}, {}

    def inspect_context(packet, binding, *, reader, snapshot_id, params, limit):
        assert reader.store.con is not state.con
        assert reader.store.con.in_transaction
        assert reader.ref(snapshot_id).snapshot_id == main.SNAPSHOT_ID
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            reader.store.con.execute('CREATE TABLE forbidden_write(value)')
        return {'decisionSnapshotId': snapshot_id, 'nextTradingDay': None}, [], reader

    monkeypatch.setattr(main, 'load_simulation_research_context', paused_read)
    monkeypatch.setattr(main, 'simulation_context', inspect_context)
    try:
        with TestClient(app) as client, ThreadPoolExecutor(max_workers=3) as pool:
            context = pool.submit(client.get, '/api/v1/research/simulation/context')
            try:
                assert entered.wait(5)
                status = pool.submit(client.get, '/api/v1/status').result(5)
                schedule = pool.submit(client.get, '/api/v1/schedule').result(5)
                assert status.status_code == schedule.status_code == 200
                assert not release.is_set()
            finally:
                release.set()
            response = context.result(5)
            assert response.status_code == 200, response.text
            assert response.json()['decisionSnapshotId'] == main.SNAPSHOT_ID
    finally:
        state.con.close()
