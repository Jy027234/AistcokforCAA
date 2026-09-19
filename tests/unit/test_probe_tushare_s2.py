"""Tushare S2 探针的离线用例；不触发真实网络。"""

from __future__ import annotations

import json
from pathlib import Path

from tools.probe_tushare_s2 import (
    EXIT_PERMISSION_DENIED,
    SOURCE_ID,
    TushareS2Probe,
    extract_token,
    main,
)


TOKEN = "a" * 56


def test_extract_token_from_label_chooses_longest_without_returning_label() -> None:
    assert extract_token(f"TUSHARE_TOKEN: shorttoken\n真实值={TOKEN}") == TOKEN


def test_permission_denied_report_is_derived_and_does_not_contain_token(tmp_path: Path) -> None:
    def denied(payload, timeout):
        assert payload["token"] == TOKEN
        return {"code": 40203, "msg": "权限不足", "data": None}

    report = TushareS2Probe(TOKEN, transport=denied).run(
        samples=("600519.SH", "601398.SH"), periods=("20260630",),
        max_requests=6)
    encoded = json.dumps(report, ensure_ascii=False)
    assert report["probe"]["source_id"] == SOURCE_ID
    assert report["probe"]["credentials_valid"] is True
    assert report["probe"]["permission"] == "denied"
    assert TOKEN not in encoded
    assert all(item["row_count"] == 0 for item in report["observations"])
    assert report["normalization"]["rejected_pairs"] == 1


def test_invalid_credentials_stop_before_repeating_requests() -> None:
    calls = []

    def invalid(payload, timeout):
        calls.append(payload["api_name"])
        return {"code": 40101, "msg": "invalid token", "data": None}

    report = TushareS2Probe(TOKEN, transport=invalid).run(
        samples=("600519.SH", "601398.SH"), periods=("20260630", "20260331"),
        max_requests=27)
    assert len(calls) == 1
    assert report["probe"]["credentials_valid"] is False
    assert report["probe"]["stop_reason"] == "credentials_invalid"


def test_granted_sample_uses_adapter_and_reports_normalization() -> None:
    fields = [
        "ts_code", "end_date", "ann_date", "f_ann_date", "report_type",
        "comp_type", "update_flag", "n_income_attr_p", "n_income",
        "revenue", "total_hldr_eqy_exc_min_int", "n_cashflow_act",
    ]
    common = ["600519.SH", "20260630", "20260829", "20260829", "1", "1", "0"]
    values = {
        "income": common + ["10.25", "11.00", "100.50", None, None],
        "balancesheet": common + [None, None, None, "80.00", None],
        "cashflow": common + [None, None, None, None, "14.00"],
    }

    def granted(payload, timeout):
        return {"code": 0, "msg": "", "data": {
            "fields": fields,
            "items": [values[payload["api_name"]]],
        }}

    report = TushareS2Probe(TOKEN, transport=granted).run(
        samples=("600519.SH",), periods=("20260630",), max_requests=3)
    assert report["probe"]["permission"] == "granted"
    assert report["normalization"]["normalized_rows"] == 1
    assert report["normalization"]["rejected_pairs"] == 0


def test_cli_writes_report_and_returns_distinct_permission_exit(tmp_path: Path, monkeypatch) -> None:
    token_file = tmp_path / "token.txt"
    token_file.write_text(f"token={TOKEN}\n", encoding="utf-8")
    output = tmp_path / "report.json"

    def fake_run(self, **kwargs):
        return {
            "probe": {"permission": "denied", "credentials_valid": True},
            "observations": [],
        }

    monkeypatch.setattr(TushareS2Probe, "run", fake_run)
    result = main(["--token-file", str(token_file), "--output", str(output)])
    assert result == EXIT_PERMISSION_DENIED
    body = output.read_text(encoding="utf-8")
    assert TOKEN not in body
    assert json.loads(body)["probe"]["token_present"] is True
    assert "token_file" not in json.loads(body)["probe"]


def test_cli_does_not_record_missing_token_file_path(tmp_path: Path) -> None:
    missing = tmp_path / "private-token-location.txt"
    output = tmp_path / "report.json"

    result = main(["--token-file", str(missing), "--output", str(output)])

    assert result != 0
    body = output.read_text(encoding="utf-8")
    assert str(missing) not in body
    assert "private-token-location" not in body
