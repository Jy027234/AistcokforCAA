"""S2 未通过 ADR-015 数据闸门时不能被登记或用于实验。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "src"))

from main import build_state, create_app  # noqa: E402
from aquant.domain.research.strategies import spec_hash  # noqa: E402


USER = {"X-Aquant-Subject": "user:alice"}


@pytest.fixture()
def api(tmp_path):
    state = build_state(tmp_path)
    with TestClient(create_app(state=state)) as client:
        yield client, state.con


def _experiment_body(strategy_version: str) -> dict:
    return {
        "hypothesis": "S2 quality-value candidate",
        "data_range_start": "2026-06-22",
        "data_range_end": "2026-09-14",
        "universe": ["SYN.A.600519"],
        "feature_version": "f10-v1",
        "strategy_version": strategy_version,
        "primary_metric": "rank_ic",
        "stopping_condition": "stop when required factors are unavailable",
    }


def test_s2_registration_is_rejected_with_machine_readable_reason(api) -> None:
    client, con = api

    response = client.post("/api/v1/strategy-versions", headers=USER, json={
        "strategy_version": "s2-unverified-v1",
        "family": "S2",
        "spec": {"factors": ["F07", "F08", "F09", "F10"]},
    })

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SOURCE_PERMISSION_MISSING"
    assert con.execute(
        "SELECT 1 FROM strategy_version WHERE strategy_version='s2-unverified-v1'"
    ).fetchone() is None

    listed = client.get("/api/v1/strategy-versions").json()
    gate = next(item for item in listed["familyGates"] if item["family"] == "S2")
    assert gate["registrationAvailable"] is False
    assert gate["error"]["code"] == "SOURCE_PERMISSION_MISSING"
    assert gate["trialImpact"]["blocksInitialS1Trial"] is False
    assert "S1_VS_S2_COMPARISON" in gate["trialImpact"]["blockedCapabilities"]
    assert "MANUAL_SIMULATION" in gate["trialImpact"]["availableCapabilities"]


def test_legacy_s2_version_cannot_be_used_to_register_an_experiment(api) -> None:
    client, con = api
    strategy_version = "s2-legacy-unverified"
    spec = {"factors": ["F07", "F08", "F09", "F10"]}
    con.execute(
        "INSERT INTO strategy_version "
        "(strategy_version,family,spec_json,spec_hash,frozen_at) VALUES (?,?,?,?,?)",
        (strategy_version, "S2", json.dumps(spec), spec_hash(spec),
         "2026-09-01T00:00:00+00:00"),
    )
    con.commit()

    response = client.post(
        "/api/v1/experiments", headers=USER,
        json=_experiment_body(strategy_version),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SOURCE_PERMISSION_MISSING"
    assert con.execute(
        "SELECT 1 FROM experiment WHERE strategy_version=?", (strategy_version,)
    ).fetchone() is None
