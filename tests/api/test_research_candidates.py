import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps/api"))
from main import build_state, create_app
from aquant.operations.research_candidates import file_hash


def _artifact(tmp_path):
    source = tmp_path / "trial.json"
    source.write_text(json.dumps({"trading_day": "2026-09-24", "pool_size": 0, "instruments": [],
        "market_cap_source": {"archive_verified": True, "placeholder_used": False,
            "market_cap_as_of": "2026-09-24"}}), encoding="utf-8")
    artifact = tmp_path / "candidates.json"
    artifact.write_text(json.dumps({"schema": "aquant.research-candidates.v1", "researchOnly": True,
        "simulationAvailable": False, "pitMode": "HISTORICAL_RECONSTRUCTED", "strategy": "S2_RESEARCH",
        "dataAsOf": "2026-09-24", "poolSize": 0, "rankedCount": 0, "excludedCount": 0,
        "candidates": [], "source": {"marketCapAsOf": "2026-09-24"},
        "provenance": [{"role": "factor_report", "path": str(source), "contentHash": file_hash(source)}]}), encoding="utf-8")
    return artifact


def test_research_endpoint_does_not_open_s2_or_change_s1_candidate_set(tmp_path, monkeypatch):
    monkeypatch.setenv("AQUANT_IDENTITY_MODE", "LOCAL_LOOPBACK_DEMO")
    artifact = _artifact(tmp_path)
    monkeypatch.setenv("AQUANT_RESEARCH_CANDIDATES_FILE", str(artifact))
    packet = json.loads(artifact.read_text(encoding="utf-8"))
    monkeypatch.setattr("aquant.operations.research_candidates._frozen_description", lambda sources: {
        "candidates": [], "source": packet["source"], "limitations": [], "exclusions": []})
    packet.update(limitations=[], exclusions=[])
    artifact.write_text(json.dumps(packet), encoding="utf-8")
    state = build_state(tmp_path / "app")
    with TestClient(create_app(state=state)) as client:
        before = client.get("/api/v1/candidates").json()
        result = client.get("/api/v1/research/candidates")
        assert result.status_code == 200
        assert result.json()["simulationAvailable"] is False
        assert client.get("/api/v1/candidates").json() == before
        gate = next(x for x in client.get("/api/v1/strategy-versions").json()["familyGates"] if x["family"] == "S2")
        assert gate["registrationAvailable"] is False
        assert client.get("/api/v1/research/candidates?limit=101").status_code == 422


def test_research_endpoint_refuses_remote_mode_and_missing_input(tmp_path, monkeypatch):
    monkeypatch.setenv("AQUANT_RESEARCH_CANDIDATES_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("AQUANT_IDENTITY_MODE", "DEVELOPMENT_SELF_REPORTED")
    state = build_state(tmp_path / "app")
    with TestClient(create_app(state=state)) as client:
        assert client.get("/api/v1/research/candidates").status_code == 403
    monkeypatch.setenv("AQUANT_IDENTITY_MODE", "LOCAL_LOOPBACK_DEMO")
    with TestClient(create_app(state=state)) as client:
        response = client.get("/api/v1/research/candidates")
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "RESEARCH_CANDIDATES_NOT_READY"
