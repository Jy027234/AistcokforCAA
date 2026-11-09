"""已发布快照目录的 S1 决策能力判定。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from aquant.domain.data.reader import SnapshotReader


def _reader(*, missing_adjusted: bool) -> SnapshotReader:
    reader = object.__new__(SnapshotReader)
    reader.instruments = lambda *_args, **_kwargs: [
        {"instrument_id": "CN:600000", "industry_code": "BANK"},
    ]
    reader.daily_quotes = lambda *_args, **_kwargs: [
        SimpleNamespace(
            instrument_id="CN:600000",
            trading_day=date(2026, 1, 1),
            adjusted_close_cents=(None if missing_adjusted and index == 0 else 1000 + index),
        )
        for index in range(61)
    ]
    return reader


def test_s1_decision_capability_rejects_incomplete_adjusted_window():
    capability = _reader(missing_adjusted=True)._s1_decision_capability(
        "snap-old", as_of="2026-09-14T07:00:00+00:00",
        dataset_names={"instruments", "daily_quotes"},
    )

    assert capability.available is False
    assert capability.code == "ADJUSTED_CLOSE_INCOMPLETE"
    assert "1 只证券" in capability.message


def test_s1_decision_capability_accepts_complete_adjusted_window():
    capability = _reader(missing_adjusted=False)._s1_decision_capability(
        "snap-ready", as_of="2026-09-18T07:00:00+00:00",
        dataset_names={"instruments", "daily_quotes"},
    )

    assert capability.available is True
    assert capability.code == "READY"
    assert capability.repair_action is None


def test_published_snapshot_derives_day_and_s1_from_one_raw_quote_scan(monkeypatch):
    as_of = "2026-03-02T15:00:00+00:00"
    verified: list[tuple[str, str]] = []
    instruments = {
        "instrument_id": "CN:600000",
        "industry_code": "BANK",
    }
    store = SimpleNamespace(
        published_snapshot=lambda _snapshot_id: {
            "snapshot_id": "snap-ready", "kind": "EOD", "data_mode": "PRODUCTION",
            "as_of_time": as_of, "input_cutoff_at": as_of,
            "published_at": as_of, "quality_status": "OK",
        },
        require_published=lambda _snapshot_id: {
            "as_of_time": as_of, "input_cutoff_at": as_of,
        },
        datasets=lambda _snapshot_id: [
            {"name": "daily_quotes", "record_count": 61, "coverage_ratio": None,
             "as_of_upper_bound": as_of},
            {"name": "instruments", "record_count": 1, "coverage_ratio": None,
             "as_of_upper_bound": as_of},
        ],
        verify_dataset=lambda snapshot_id, name: verified.append((snapshot_id, name)),
    )
    reader = SnapshotReader(store)
    # Warm dataset cache still needs a fresh integrity check before a catalog
    # summary reuses the cached inputs.
    quotes = [
        {
            "instrument_id": "CN:600000",
            "trading_day": (date(2025, 12, 1) + timedelta(days=index)).isoformat(),
            "open_cents": 1000, "high_cents": 1000, "low_cents": 1000,
            "close_cents": 1000, "adjusted_close_cents": 1000 + index,
            "volume_shares": 1,
        }
        for index in range(61)
    ]
    reader._cache["snap-ready", "daily_quotes"] = quotes
    reader._cache["snap-ready", "instruments"] = [instruments]
    scan_calls: list[str] = []
    instrument_calls: list[str] = []

    original_summary = reader._catalog_quote_summary

    def count_scans(snapshot_id, **kwargs):
        scan_calls.append(snapshot_id)
        return original_summary(snapshot_id, **kwargs)

    monkeypatch.setattr(
        reader, "_catalog_quote_summary", count_scans,
    )
    monkeypatch.setattr(
        reader, "instruments",
        lambda snapshot_id, **_kwargs: instrument_calls.append(snapshot_id) or [instruments],
    )
    monkeypatch.setattr(
        reader, "daily_quotes",
        lambda *_args, **_kwargs: pytest.fail("catalog built public QuoteRows"),
    )

    summary = reader.published_snapshot("snap-ready")

    assert scan_calls == ["snap-ready"]
    assert instrument_calls == ["snap-ready"]
    assert verified == [
        ("snap-ready", "daily_quotes"),
        ("snap-ready", "instruments"),
    ]
    assert summary.trading_day == "2026-01-30"
    assert summary.s1_decision.available is True
    assert summary.s1_decision.code == "READY"


def test_catalog_s1_counts_duplicate_quote_rows_and_preserves_missing_adjusted_rule():
    as_of = "2026-03-02T15:00:00+00:00"
    quotes = [
        {
            "instrument_id": "CN:600000",
            "trading_day": (date(2026, 1, 1) + timedelta(days=index)).isoformat(),
            "open_cents": 1000, "high_cents": 1000, "low_cents": 1000,
            "close_cents": 1000, "adjusted_close_cents": 1000 + index,
            "volume_shares": 1,
        }
        for index in range(60)
    ]
    quotes.append(dict(quotes[-1]))  # Duplicate rows remain separate S1 observations.
    store = SimpleNamespace(require_published=lambda _snapshot_id: {
        "as_of_time": as_of, "input_cutoff_at": as_of,
    })
    reader = SnapshotReader(store)
    reader._cache["snap-ready", "daily_quotes"] = quotes
    instrument_rows = [{"instrument_id": "CN:600000", "industry_code": "BANK"}]
    point = datetime.fromisoformat(as_of)
    quote_summary = reader._catalog_quote_summary("snap-ready", as_of=point)

    assert quote_summary.raw_counts == {"CN:600000": 61}
    assert quote_summary.adjusted_counts == {"CN:600000": 61}
    ready = reader._s1_decision_capability(
        "snap-ready", as_of=as_of,
        dataset_names={"instruments", "daily_quotes"},
        quote_summary=quote_summary, instrument_rows=instrument_rows,
    )
    assert ready.available is True
    assert ready.code == "READY"

    quotes[0]["adjusted_close_cents"] = None
    missing_adjusted_summary = reader._catalog_quote_summary("snap-ready", as_of=point)
    incomplete = reader._s1_decision_capability(
        "snap-ready", as_of=as_of,
        dataset_names={"instruments", "daily_quotes"},
        quote_summary=missing_adjusted_summary, instrument_rows=instrument_rows,
    )
    assert incomplete.available is False
    assert incomplete.code == "ADJUSTED_CLOSE_INCOMPLETE"


def test_bad_catalog_quotes_fail_before_instruments_are_read():
    as_of = "2026-03-02T15:00:00+00:00"
    quote_reads: list[str] = []
    store = SimpleNamespace(
        published_snapshot=lambda _snapshot_id: {
            "snapshot_id": "snap-bad", "kind": "EOD", "data_mode": "PRODUCTION",
            "as_of_time": as_of, "input_cutoff_at": as_of,
            "published_at": as_of, "quality_status": "OK",
        },
        require_published=lambda _snapshot_id: {
            "as_of_time": as_of, "input_cutoff_at": as_of,
        },
        datasets=lambda _snapshot_id: [
            {"name": name, "record_count": 1, "coverage_ratio": None,
             "as_of_upper_bound": as_of}
            for name in ("daily_quotes", "instruments")
        ],
        read_verified_dataset=lambda _snapshot_id, name: (
            quote_reads.append(name) or b"[{\"instrument_id\":\"CN:600000\","
            b"\"trading_day\":\"2026-03-02\",\"open_cents\":1000,"
            b"\"high_cents\":1000,\"low_cents\":1000,"
            b"\"adjusted_close_cents\":1000,\"volume_shares\":1}]"
        ),
    )
    reader = SnapshotReader(store)
    instrument_reads: list[str] = []

    def reject_instrument_read(snapshot_id, **_kwargs):
        instrument_reads.append(snapshot_id)
        raise AssertionError("instruments must not be read after a bad quote row")

    reader.instruments = reject_instrument_read

    with pytest.raises(KeyError, match="close_cents"):
        reader.published_snapshot("snap-bad")

    assert quote_reads == ["daily_quotes"]
    assert instrument_reads == []
