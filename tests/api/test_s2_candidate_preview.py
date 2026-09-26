"""Unreviewed PDF diagnostics stay outside the formal S2 strategy path."""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "src"))

from main import build_state, create_app  # noqa: E402
from aquant.operations.s2_candidate_preview import _factors  # noqa: E402


def _preview() -> dict:
    return {
        "schemaVersion": "aquant.s2_candidate_preview.v1",
        "status": "CANDIDATE_DIAGNOSTIC_ONLY",
        "source": "cninfo",
        "generatedAt": "2026-09-26T00:00:00+00:00",
        "formalPitEligible": False,
        "backtestable": False,
        "rank": None,
        "marketCapStatus": "NOT_USED_PDF_FIRST_SEEN_AFTER_LATEST_PUBLISHED_SNAPSHOT",
        "instruments": [
            {"instrumentId": stock, "factors": {"F07": "0.2", "F08": "1.1",
                                                "F09": "0.05", "F10": None}}
            for stock in ("000333", "600519", "601012")
        ],
    }


def test_local_preview_served_without_opening_s2_gate(tmp_path, monkeypatch) -> None:
    file = tmp_path / "preview.json"
    file.write_text(json.dumps(_preview()), encoding="utf-8")
    monkeypatch.setenv("AQUANT_IDENTITY_MODE", "LOCAL_LOOPBACK_DEMO")
    monkeypatch.setenv("AQUANT_S2_CANDIDATE_PREVIEW_FILE", str(file))
    state = build_state(tmp_path / "app")
    with TestClient(create_app(state=state)) as client:
        response = client.get("/api/v1/research/s2/diagnostic-preview")
        assert response.status_code == 200
        assert response.json()["formalPitEligible"] is False
        assert response.json()["instruments"][0]["factors"]["F10"] is None
        gates = client.get("/api/v1/strategy-versions").json()["familyGates"]
        assert next(g for g in gates if g["family"] == "S2")["registrationAvailable"] is False


def test_candidate_preview_refuses_nonlocal_or_promoted_payload(tmp_path, monkeypatch) -> None:
    file = tmp_path / "preview.json"
    file.write_text(json.dumps(_preview()), encoding="utf-8")
    monkeypatch.setenv("AQUANT_S2_CANDIDATE_PREVIEW_FILE", str(file))
    monkeypatch.setenv("AQUANT_IDENTITY_MODE", "DEVELOPMENT_SELF_REPORTED")
    state = build_state(tmp_path / "app")
    with TestClient(create_app(state=state)) as client:
        assert client.get("/api/v1/research/s2/diagnostic-preview").status_code == 403
    monkeypatch.setenv("AQUANT_IDENTITY_MODE", "LOCAL_LOOPBACK_DEMO")
    promoted = _preview()
    promoted["formalPitEligible"] = True
    file.write_text(json.dumps(promoted), encoding="utf-8")
    with TestClient(create_app(state=state)) as client:
        assert client.get("/api/v1/research/s2/diagnostic-preview").status_code == 503


def test_formula_preview_keeps_f10_empty_and_excludes_negative_profit() -> None:
    # Five H1/FY report bundles. Revenue comparison uses the older FY/H1
    # bridge, while profit and cashflow use the latest FY/H1 bridge.
    def row(revenue: int, profit: int, cash: int, equity: int) -> dict:
        return {"values": {
            "revenue": Decimal(revenue),
            "net_profit_attributable": Decimal(profit),
            "net_profit_consolidated": Decimal(profit),
            "operating_cashflow": Decimal(cash),
            "parent_equity": Decimal(equity),
        }}

    reports = [row(50, 5, 4, 100), row(100, 10, 8, 100),
               row(60, 6, 5, 100), row(120, 12, 10, 110),
               row(72, 8, 7, 120)]
    factors, code, _ = _factors(reports)
    assert code is None
    assert Decimal(factors["F07"]) == Decimal(14) / Decimal(110)
    assert Decimal(factors["F08"]) == Decimal(12) / Decimal(14)
    assert Decimal(factors["F09"]) == Decimal(132) / Decimal(110) - 1
    assert factors["F10"] is None

    reports[4] = row(72, -20, 7, 120)
    factors, code, _ = _factors(reports)
    assert code == "S2_NON_POSITIVE_DENOMINATOR"
    assert all(value is None for value in factors.values())
