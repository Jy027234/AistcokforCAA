"""Startup may warm exact fingerprints, without running financial reports."""
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps/api"))
import main


@pytest.mark.parametrize("configured", [False, True])
def test_financial_warmup_precedes_ready_and_cache_closes(tmp_path, monkeypatch, configured):
    for name in ("AQUANT_RESEARCH_CANDIDATES_FILE", "AQUANT_LIVE_FINANCIAL_FACTS_DB"):
        if configured:
            monkeypatch.setenv(name, "fixture-configured")
        else:
            monkeypatch.delenv(name, raising=False)
    seen = []
    monkeypatch.setattr(main, "warm_financial_repository_versions", lambda: seen.append("warm"))
    monkeypatch.setattr(main, "shutdown_repository_version_cache", lambda: seen.append("close"))
    monkeypatch.setattr(main, "build_financial_coverage_report", lambda **kwargs: pytest.fail("No report during startup"))
    state = main.build_state(tmp_path)
    try:
        with TestClient(main.create_app(state)) as client:
            assert seen == (["warm"] if configured else [])
            assert client.get("/api/v1/health").status_code == 200
        assert seen == (["warm", "close"] if configured else ["close"])
    finally:
        state.con.close()


def test_unavailable_financial_warmup_does_not_hide_failure_or_disable_workbench(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("AQUANT_RESEARCH_CANDIDATES_FILE", "fixture-configured")
    monkeypatch.setenv("AQUANT_LIVE_FINANCIAL_FACTS_DB", "fixture-configured")
    closed = []
    def unavailable():
        raise main.FinancialCheckError("FINANCIAL_CHECK_INPUT_NOT_READY", "private path", "repair")
    monkeypatch.setattr(main, "warm_financial_repository_versions", unavailable)
    monkeypatch.setattr(main, "shutdown_repository_version_cache", lambda: closed.append(True))
    state = main.build_state(tmp_path)
    try:
        with TestClient(main.create_app(state)) as client:
            assert client.get("/api/v1/health").status_code == 200
        assert closed == [True]
        assert "FINANCIAL_CHECK_INPUT_NOT_READY" in caplog.text
        assert "private path" not in caplog.text
    finally:
        state.con.close()
