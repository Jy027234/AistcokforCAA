from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from aquant.operations.decision_market_caps import (
    DecisionMarketCapError,
    inject_sidecar,
    load_sidecar,
    market_cap_cents_from_yuan,
    sidecar_from_eastmoney_rows,
    sidecar_from_market_cap_values,
)
from aquant.operations.universe_snapshot import _build_document
from aquant.adapters.providers.eastmoney import FetchOutcome
from aquant.domain.data.db import connect
from aquant.domain.data.forward_archive import ForwardArchive
from tools import collect_market_caps
from tools.collect_market_caps import collect_all_rows


class _PagedClient:
    def __init__(self, pages, totals):
        self.pages = pages
        self.totals = totals

    def universe_page(self, *, page, page_size):
        rows = self.pages[page - 1] if page <= len(self.pages) else []
        total = self.totals[page - 1] if page <= len(self.totals) else self.totals[-1]
        return FetchOutcome(
            True, b"{}", f"rcpt_page_{page}", f"sha256:{page}", None, 200,
        ), rows, total


def test_eastmoney_rows_keep_receipt_and_convert_yuan_to_cents():
    sidecar = sidecar_from_eastmoney_rows(
        [
            {"f12": "600519", "f13": 1, "f20": 1_234.56},
            {"f12": "000001", "f13": 0, "f20": None},
        ],
        market_cap_as_of="2026-09-18",
        observed_at=datetime(2026, 9, 18, 7, 30, tzinfo=timezone.utc),
        receipt_id="rcpt_test",
        content_hash="sha256:abc",
    )

    assert sidecar.items["SH.600519"].market_cap_cents == 123456
    assert "SZ.000001" not in sidecar.items
    assert sidecar.receipt_id == "rcpt_test"
    assert sidecar.content_hash == "sha256:abc"


def test_observation_before_close_is_rejected():
    with pytest.raises(DecisionMarketCapError, match="收盘后"):
        sidecar_from_eastmoney_rows(
            [{"f12": "600519", "f13": 1, "f20": 100}],
            market_cap_as_of="2026-09-18",
            observed_at=datetime(2026, 9, 18, 6, 59, tzinfo=timezone.utc),
            receipt_id="rcpt_test",
            content_hash="sha256:abc",
        )


def test_load_sidecar_requires_latest_quote_day_and_injects_audit_metadata(tmp_path):
    sidecar = sidecar_from_eastmoney_rows(
        [{"f12": "600519", "f13": 1, "f20": 100}],
        market_cap_as_of="2026-09-18",
        observed_at=datetime(2026, 9, 18, 7, 30, tzinfo=timezone.utc),
        receipt_id="rcpt_test",
        content_hash="sha256:abc",
    )
    path = tmp_path / "decision-market-caps.json"
    path.write_text(json.dumps(sidecar.as_dict()), encoding="utf-8")
    loaded = load_sidecar(path, expected_as_of="2026-09-18")
    instruments = [{"instrument_id": "SH.600519"}, {"instrument_id": "SZ.000001"}]
    assert inject_sidecar(instruments, loaded) == (1, 1)
    assert instruments[0]["market_cap_cents"] == 10_000
    assert instruments[0]["market_cap_as_of"] == "2026-09-18"
    assert instruments[0]["market_cap_receipt_id"] == "rcpt_test"
    assert "market_cap_cents" not in instruments[1]

    with pytest.raises(DecisionMarketCapError, match="最近行情日"):
        load_sidecar(path, expected_as_of="2026-09-19")


def test_tencent_sidecar_is_accepted_with_same_audit_contract(tmp_path):
    sidecar = sidecar_from_market_cap_values(
        {"SH.600519": "1567352000000"},
        market_cap_as_of="2026-09-22",
        observed_at=datetime(2026, 9, 22, 8, 30, tzinfo=timezone.utc),
        receipt_id="rcpt_tencent_manifest",
        content_hash="sha256:tencent",
        source_id="tencent-qt",
    )
    path = tmp_path / "decision-market-caps.json"
    path.write_text(json.dumps(sidecar.as_dict()), encoding="utf-8")

    loaded = load_sidecar(path, expected_as_of="2026-09-22")
    assert loaded.source_id == "tencent-qt"
    assert loaded.items["SH.600519"].market_cap_cents == 156_735_200_000_000


def test_market_cap_cents_rejects_non_finite_or_non_positive():
    assert market_cap_cents_from_yuan("1.01") == 101
    for value in (None, 0, -1, "NaN", "Infinity"):
        with pytest.raises(DecisionMarketCapError):
            market_cap_cents_from_yuan(value)


def test_paginated_collection_reaches_declared_total_and_archives_manifest(tmp_path):
    con = connect(tmp_path / "archive.sqlite")
    try:
        archive = ForwardArchive(con, tmp_path / "archive")
        outcome, rows = collect_all_rows(
            _PagedClient(
                pages=[
                    [{"f13": 1, "f12": "600001", "f20": 100}],
                    [{"f13": 0, "f12": "000001", "f20": 200}],
                ],
                totals=[2, 2],
            ),
            archive,
            page_size=1,
            max_pages=5,
        )
        assert outcome.ok
        assert len(rows) == 2
        assert outcome.receipt_id
        assert outcome.content_hash and archive.verify(outcome.content_hash)
    finally:
        con.close()


def test_paginated_collection_rejects_changing_total(tmp_path):
    con = connect(tmp_path / "archive.sqlite")
    try:
        outcome, rows = collect_all_rows(
            _PagedClient(
                pages=[
                    [{"f13": 1, "f12": "600001", "f20": 100}],
                    [{"f13": 0, "f12": "000001", "f20": 200}],
                ],
                totals=[2, 3],
            ),
            ForwardArchive(con, tmp_path / "archive"),
            page_size=1,
            max_pages=5,
        )
        assert not outcome.ok
        assert rows == []
        assert "total 变化" in (outcome.detail or "")
    finally:
        con.close()


def test_partial_tencent_pool_uses_eastmoney_fallback(tmp_path, monkeypatch):
    market_day = "2026-09-22"
    calls = []

    class _Sidecar:
        items = {"SH.600519": object(), "SZ.000001": object()}

        def as_dict(self):
            return {"source_id": "eastmoney-direct", "items": {}}

    def _tencent(*args, **kwargs):
        calls.append("tencent")
        return FetchOutcome(True, b"{}", "tencent_rcpt", "sha256:tencent", None, 200), {
            "SH.600519": 100,
        }

    def _eastmoney(*args, **kwargs):
        calls.append("eastmoney")
        return FetchOutcome(True, b"{}", "eastmoney_rcpt", "sha256:eastmoney", None, 200), [
            {"f12": "600519", "f13": 1, "f20": 100},
            {"f12": "000001", "f13": 0, "f20": 200},
        ]

    monkeypatch.setattr(collect_market_caps, "ARCHIVE_ROOT", tmp_path / "archive")
    monkeypatch.setattr(collect_market_caps, "_pool_symbols",
                        lambda path: ["sh600519", "sz000001"])
    monkeypatch.setattr(collect_market_caps, "collect_tencent_pool", _tencent)
    monkeypatch.setattr(collect_market_caps, "collect_all_rows", _eastmoney)
    monkeypatch.setattr(collect_market_caps, "sidecar_from_eastmoney_rows",
                        lambda *args, **kwargs: _Sidecar())
    output = tmp_path / "caps.json"
    monkeypatch.setattr("sys.argv", ["collect_market_caps.py", "--as-of", market_day,
                                     "--output", str(output)])

    assert collect_market_caps.main() == 0
    assert calls == ["tencent", "eastmoney"]
    assert json.loads(output.read_text(encoding="utf-8"))["source_id"] == "eastmoney-direct"


def test_tencent_collection_ignores_unrequested_rows(tmp_path):
    class _Client:
        def market_caps(self, symbols):
            assert symbols == ["sh600519"]
            return FetchOutcome(True, b"{}", "rcpt", "sha256:test", None, 200), [
                {"instrument_id": "SH.600519", "market_time": "2026-09-22T16:00:00",
                 "market_cap_yuan": "100"},
                {"instrument_id": "SZ.000001", "market_time": "2026-09-22T16:00:00",
                 "market_cap_yuan": "200"},
            ]

    con = connect(tmp_path / "archive.sqlite")
    try:
        outcome, values = collect_market_caps.collect_tencent_pool(
            _Client(), ForwardArchive(con, tmp_path / "archive"),
            symbols=["sh600519"], market_day=date(2026, 9, 22))
        assert outcome.ok
        assert values == {"SH.600519": "100"}
    finally:
        con.close()


def test_universe_document_injects_only_valid_sidecar_values(tmp_path):
    sidecar = sidecar_from_eastmoney_rows(
        [{"f12": "600519", "f13": 1, "f20": 100}],
        market_cap_as_of="2026-09-18",
        observed_at=datetime(2026, 9, 18, 7, 30, tzinfo=timezone.utc),
        receipt_id="rcpt_test",
        content_hash="sha256:abc",
    )
    sidecar_path = tmp_path / "decision-market-caps.json"
    sidecar_path.write_text(json.dumps(sidecar.as_dict()), encoding="utf-8")
    checks = []
    doc, instruments, _quotes, _days = _build_document(
        cache={
            "bars": {
                "SH.600519": {
                    "exchange": "SSE", "board": "MAIN",
                    "prev_close_before_window": 999,
                    "rows": [{
                        "trading_day": "2026-09-18", "open_cents": 100,
                        "high_cents": 101, "low_cents": 99,
                        "close_cents": 100, "adjusted_close_cents": 100,
                        "volume_shares": 100, "amount_cents": 1000,
                    }],
                },
            },
            "trading_calendar": ["2026-09-18"],
        },
        pool={"classification_version": "test", "instruments": [{
            "instrument_id": "SH.600519", "exchange": "SSE", "board": "MAIN",
            "name": "测试", "industry": "银行",
        }]},
        window_start=None, window_end=None, window=None,
        financials_path=tmp_path / "missing-financials.json",
        actions_path=tmp_path / "missing-actions.json",
        market_caps_path=sidecar_path,
        check_market_caps=True,
        checks=checks,
    )
    assert doc["instruments"] == instruments
    assert instruments[0]["market_cap_cents"] == 10_000
    assert instruments[0]["market_cap_as_of"] == "2026-09-18"
    market_check = next(c for c in checks if "总市值覆盖率" in c.name)
    assert market_check.ok
