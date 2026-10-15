import json
from datetime import date

import pytest

from tools.capture_s1_optional_financial_evidence import (
    classify_profit_row,
    load_targets,
)


def _target(iid="SH.600323", period="2026Q2", year=2026, quarter=2):
    market, code = iid.split(".")
    return {"instrumentId": iid,
            "providerCode": ("sh." if market == "SH" else "sz.") + code,
            "year": year, "quarter": quarter, "periodKey": period}


def test_load_targets_requires_exact_provider_identity_and_period(tmp_path):
    path = tmp_path / "demand.json"
    path.write_text(json.dumps({"schema": "aquant.financial-optional-demand.v1",
                                "records": [_target()]}), encoding="utf-8")
    _, rows = load_targets(path)
    assert rows == [_target()]

    bad = tmp_path / "bad.json"
    item = _target()
    item["providerCode"] = "sz.600323"
    bad.write_text(json.dumps({"schema": "aquant.financial-optional-demand.v1",
                               "records": [item]}), encoding="utf-8")
    with pytest.raises(ValueError, match="provider code"):
        load_targets(bad)


def test_load_targets_rejects_duplicate_or_incorrect_period_binding(tmp_path):
    path = tmp_path / "demand.json"
    row = _target()
    path.write_text(json.dumps({"schema": "aquant.financial-optional-demand.v1",
                                "records": [row, row]}), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate target"):
        load_targets(path)

    row = _target(period="2025Q4", year=2025, quarter=4)
    row["quarter"] = 2
    path.write_text(json.dumps({"schema": "aquant.financial-optional-demand.v1",
                                "records": [row]}), encoding="utf-8")
    with pytest.raises(ValueError, match="period fields"):
        load_targets(path)


def test_profit_row_classification_requires_identity_exact_period_and_not_future():
    row = {"code": "sh.600323", "netProfit": "100.25",
           "statDate": "2026-06-30", "pubDate": "2026-08-27"}
    assert classify_profit_row("SH.600323", "2026Q2", row,
                               date(2026, 10, 13)) == ("COLLECTED", None)
    assert classify_profit_row("SH.600323", "2026Q2", None,
                               date(2026, 10, 13))[0] == "NO_RECORD"

    assert "identity mismatch" in classify_profit_row(
        "SH.600323", "2026Q2", {**row, "code": "sz.600323"}, date(2026, 10, 13))[1]
    assert "statDate mismatch" in classify_profit_row(
        "SH.600323", "2026Q2", {**row, "statDate": "2026-09-30"}, date(2026, 10, 13))[1]
    assert "after actual capture date" in classify_profit_row(
        "SH.600323", "2026Q2", {**row, "pubDate": "2026-10-14"}, date(2026, 10, 13))[1]
