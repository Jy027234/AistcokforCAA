"""已发布快照目录的 S1 决策能力判定。"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

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
