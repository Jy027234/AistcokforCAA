from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import tools.collect_universe as collector


def _bar(day: str, close: int) -> dict:
    return {
        "trading_day": day,
        "open_cents": close - 1,
        "high_cents": close + 2,
        "low_cents": close - 2,
        "close_cents": close,
        "volume_shares": 100,
        "amount_cents": close * 100,
    }


def test_fetch_ranges_only_request_missing_tail() -> None:
    rows = [_bar("2026-09-01", 100), _bar("2026-09-02", 110)]

    assert collector.fetch_ranges_for_entry(
        rows, "2026-09-01", "2026-09-04"
    ) == [("2026-09-03", "2026-09-04")]


def test_merge_rows_deduplicates_and_keeps_latest_sorted() -> None:
    existing = [_bar("2026-09-03", 103), _bar("2026-09-01", 101)]
    incoming = [_bar("2026-09-02", 102), _bar("2026-09-03", 999)]

    merged = collector.merge_rows(existing, incoming)

    assert [row["trading_day"] for row in merged] == [
        "2026-09-01", "2026-09-02", "2026-09-03"
    ]
    assert merged[-1]["close_cents"] == 999


class _FakeBaostock:
    def __init__(self, calls: list[tuple[str, str, str]]) -> None:
        self.calls = calls

    def __enter__(self) -> "_FakeBaostock":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def daily_bars(self, code: str, *, start: str, end: str,
                   adjust: str = "3") -> tuple[list[dict], object]:
        self.calls.append((code, start, end))
        if code == "sh.000001":
            return ([_bar(day, 1) for day in (
                "2026-09-01", "2026-09-02", "2026-09-03"
            )], SimpleNamespace(content_hash="sha256:index"))
        return ([_bar("2026-09-03", 120)],
                SimpleNamespace(content_hash="sha256:security"))

    def all_stock(self, day: str) -> tuple[list[dict], object]:
        return ([{"code": "sh.600001"}],
                SimpleNamespace(content_hash="sha256:universe"))

    def stock_basic(self, code: str) -> tuple[list[dict], object]:
        raise AssertionError("listing lookup is not needed for cached listing date")


def test_main_appends_tail_without_refetching_existing_rows(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "universe-bars.json"
    out.write_text(json.dumps({
        "created_at": "2026-09-01T00:00:00+00:00",
        "window": {
            "first_day": "2026-09-01",
            "last_day": "2026-09-02",
            "trading_days": 2,
        },
        "bars": {
            "SH.600001": {
                "exchange": "SSE",
                "board": "MAIN",
                "rows": [
                    {**_bar("2026-09-02", 110), "prev_close_cents": 100},
                    {**_bar("2026-09-01", 100), "prev_close_cents": 90},
                ],
                "prev_close_before_window": 90,
                "prev_close_attempted": True,
                "window": "2026-09-01..2026-09-02",
                "rows_format": collector.ROWS_FORMAT,
                "listed_on": "2020-01-01",
            }
        },
        "failed": {},
    }, ensure_ascii=False))
    monkeypatch.setattr(collector, "OUT", out)
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        collector, "BaostockClient", lambda *_args, **_kwargs: _FakeBaostock(calls)
    )
    monkeypatch.setattr(
        "sys.argv",
        ["collect_universe.py", "--start", "2026-09-01", "--end", "2026-09-03"],
    )

    assert collector.main() == 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    entry = payload["bars"]["SH.600001"]
    assert [row["trading_day"] for row in entry["rows"]] == [
        "2026-09-01", "2026-09-02", "2026-09-03"
    ]
    assert [row["prev_close_cents"] for row in entry["rows"]] == [90, 100, 110]
    assert entry["rows"][-1]["adjusted_close_cents"] == 120
    security_calls = [call for call in calls if call[0] == "sh.600001"]
    assert security_calls == [
        ("sh.600001", "2026-09-03", "2026-09-03"),
        ("sh.600001", "2026-09-03", "2026-09-03"),
    ]

    # 同一窗口再次运行时，已有尾部覆盖足够，不能重复请求证券日线。
    calls.clear()
    assert collector.main() == 0
    assert [call for call in calls if call[0] == "sh.600001"] == []


def test_format_change_rebuilds_requested_window(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [_bar("2026-09-01", 100), _bar("2026-09-02", 110)]
    out = tmp_path / "universe-bars.json"
    out.write_text(json.dumps({
        "bars": {
            "SH.600001": {
                "exchange": "SSE",
                "board": "MAIN",
                "rows": rows,
                "prev_close_before_window": 90,
                "prev_close_attempted": True,
                "window": "2026-09-01..2026-09-02",
                "rows_format": collector.ROWS_FORMAT - 1,
                "listed_on": "2020-01-01",
            }
        },
        "failed": {},
    }))
    monkeypatch.setattr(collector, "OUT", out)
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        collector, "BaostockClient", lambda *_args, **_kwargs: _FakeBaostock(calls)
    )
    monkeypatch.setattr(
        "sys.argv",
        ["collect_universe.py", "--start", "2026-09-01", "--end", "2026-09-03"],
    )

    assert collector.main() == 0

    security_calls = [call for call in calls if call[0] == "sh.600001"]
    assert security_calls == [
        ("sh.600001", "2026-08-22", "2026-09-03"),
        ("sh.600001", "2026-08-22", "2026-09-03"),
    ]
    payload = json.loads(out.read_text(encoding="utf-8"))
    entry = payload["bars"]["SH.600001"]
    assert entry["rows_format"] == collector.ROWS_FORMAT
    assert [row["trading_day"] for row in entry["rows"]] == [
        "2026-09-03"
    ]
    assert entry["rows"][0]["adjusted_close_cents"] == 120
