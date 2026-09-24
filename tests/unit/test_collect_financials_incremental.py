from __future__ import annotations

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

import tools.collect_financials as collector


def test_available_periods_wait_for_disclosure_windows_in_beijing() -> None:
    def keys(at: date | datetime) -> list[str]:
        return [collector.period_key(p) for p in collector.available_periods(at)]

    assert keys(date(2026, 9, 24))[-1] == "2026Q2"
    assert "2026Q3" not in keys(datetime(2026, 10, 31, 15, 59,
                                          tzinfo=timezone.utc))
    assert keys(datetime(2026, 10, 31, 16, 0,
                         tzinfo=timezone.utc))[-1] == "2026Q3"
    assert keys(date(2027, 4, 30))[-1] == "2026Q3"
    assert keys(date(2027, 5, 1))[-2:] == ["2026Q4", "2027Q1"]


def test_plan_only_missing_failed_or_incomplete_periods() -> None:
    periods = [(2026, 1), (2026, 2), (2026, 3), (2026, 4)]
    rows = {
        "2026Q1": {"netProfit": "10", "CFOToNP": ""},
        "2026Q2": {"netProfit": "20"},
        "2026Q3": {"netProfit": "30", "CFOToNP": "1.2"},
    }
    errors = {"2026Q3": "cash_flow: timeout"}

    assert collector.planned_periods(rows, errors, periods) == periods
    assert collector.planned_periods(rows, errors, periods,
                                     retry_failed=True) == [(2026, 3)]
    assert collector.planned_periods(rows, errors, periods,
                                     refresh=True) == periods


class _FakeClient:
    def __init__(self, calls: list[tuple[str, int, int]]) -> None:
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def profit(self, code: str, *, year: int, quarter: int):
        self.calls.append((code, year, quarter))
        return ({"code": code, "netProfit": "30", "statDate": f"{year}-09-30",
                 "pubDate": f"{year}-10-30"},
                SimpleNamespace())


def test_main_migrates_old_failure_and_fills_only_missing_quarter(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    iid = "SH.600001"
    out = tmp_path / "financials-cache.json"
    pool = tmp_path / "pool.json"
    pool.write_text(json.dumps({"instruments": [{"instrument_id": iid}]}),
                    encoding="utf-8")
    quarter_ends = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}
    existing = {f"2025Q{q}": {"netProfit": str(q), "CFOToNP": "1.0",
                              "statDate": f"2025-{quarter_ends[q]}",
                              "pubDate": "2026-01-01"}
                for q in (1, 2, 4)}
    existing.update({f"2026Q{q}": {"netProfit": str(q), "CFOToNP": "1.0",
                                     "statDate": f"2026-{quarter_ends[q]}",
                                     "pubDate": "2026-09-01"}
                     for q in (1, 2)})
    out.write_text(json.dumps({
        "created_at": "2026-09-01T00:00:00+00:00",
        "statements": {iid: existing},
        "failed": {iid: "old login failure"},
        "periods": ["2025Q1", "2025Q2", "2025Q3", "2025Q4", "2026Q1", "2026Q2"],
    }), encoding="utf-8")

    monkeypatch.setattr(collector, "OUT", out)
    monkeypatch.setattr(collector, "POOL", pool)
    period_selection = collector.available_periods
    monkeypatch.setattr(collector, "available_periods",
                        lambda: period_selection(date(2026, 9, 24)))
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(collector, "BaostockClient",
                        lambda *_args: _FakeClient(calls))
    monkeypatch.setattr(collector, "_cash_flow",
                        lambda *_args: {"CFOToNP": "1.2"})
    monkeypatch.setattr("sys.argv", ["collect_financials.py", "--retry-failed"])

    assert collector.main() == 0
    updated = json.loads(out.read_text(encoding="utf-8"))
    assert calls == [("sh.600001", 2025, 3)]
    assert updated["statements"][iid]["2025Q3"]["CFOToNP"] == "1.2"
    assert updated["statements"][iid]["2025Q1"] == existing["2025Q1"]
    assert updated["failed"] == {}
    assert "2026Q3" not in updated["periods"]

    # When the next disclosure window closes, a normal run adds only that quarter.
    monkeypatch.setattr(collector, "available_periods",
                        lambda: period_selection(date(2026, 11, 1)))
    monkeypatch.setattr("sys.argv", ["collect_financials.py"])
    calls.clear()
    assert collector.main() == 0
    extended = json.loads(out.read_text(encoding="utf-8"))
    assert calls == [("sh.600001", 2026, 3)]
    assert extended["statements"][iid]["2026Q3"]["pubDate"] == "2026-10-30"
    assert extended["statements"][iid]["2025Q1"] == existing["2025Q1"]


def test_login_failure_records_each_unattempted_period(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    iid = "SH.600001"
    out = tmp_path / "financials-cache.json"
    pool = tmp_path / "pool.json"
    pool.write_text(json.dumps({"instruments": [{"instrument_id": iid}]}),
                    encoding="utf-8")

    class UnavailableClient:
        def __enter__(self):
            raise collector.BaostockUnavailable("login failed")

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(collector, "OUT", out)
    monkeypatch.setattr(collector, "POOL", pool)
    monkeypatch.setattr(collector, "BaostockClient",
                        lambda *_args: UnavailableClient())
    period_selection = collector.available_periods
    monkeypatch.setattr(collector, "available_periods",
                        lambda: period_selection(date(2026, 9, 24)))
    monkeypatch.setattr("sys.argv", ["collect_financials.py"])

    assert collector.main() == 2
    failed = json.loads(out.read_text(encoding="utf-8"))["failed"]
    assert set(failed[iid]) == {
        "2025Q1", "2025Q2", "2025Q3", "2025Q4", "2026Q1", "2026Q2",
    }


def test_cash_flow_retry_keeps_cached_value_on_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = {"2026Q2": {"netProfit": "20", "CFOToNP": "1.0",
                       "statDate": "2026-06-30", "pubDate": "2026-08-31"}}
    errors = {"2026Q2": "cash_flow: timeout"}
    client = _FakeClient([])
    monkeypatch.setattr(collector, "_cash_flow", lambda *_args: None)

    collector._collect_period(client, "sh.600001", 2026, 2, rows, errors,
                              refresh=False)
    assert rows["2026Q2"]["CFOToNP"] == "1.0"
    assert errors == {"2026Q2": "cash_flow: empty response"}
    assert client.calls == []

    monkeypatch.setattr(collector, "_cash_flow",
                        lambda *_args: {"CFOToNP": "1.5"})
    collector._collect_period(client, "sh.600001", 2026, 2, rows, errors,
                              refresh=False)
    assert rows["2026Q2"]["CFOToNP"] == "1.5"
    assert errors == {}


def test_blank_cfo_row_stays_pending_until_value_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    period = (2026, 2)
    rows = {"2026Q2": {"netProfit": "20", "CFOToNP": "",
                       "statDate": "2026-06-30", "pubDate": "2026-08-31"}}
    errors: dict[str, str] = {}
    client = _FakeClient([])
    assert collector.planned_periods(rows, errors, [period]) == [period]

    monkeypatch.setattr(collector, "_cash_flow",
                        lambda *_args: {"CFOToNP": ""})
    collector._collect_period(client, "sh.600001", *period, rows, errors,
                              refresh=False)
    assert rows["2026Q2"]["CFOToNP"] == ""
    assert errors == {"2026Q2": "cash_flow: empty CFOToNP"}
    assert collector.planned_periods(rows, errors, [period],
                                     retry_failed=True) == [period]

    monkeypatch.setattr(collector, "_cash_flow",
                        lambda *_args: {"CFOToNP": "1.5"})
    collector._collect_period(client, "sh.600001", *period, rows, errors,
                              refresh=False)
    assert rows["2026Q2"]["CFOToNP"] == "1.5"
    assert errors == {}
    assert collector.planned_periods(rows, errors, [period]) == []


def test_blank_profit_row_stays_pending_until_value_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    period = (2026, 2)
    rows = {"2026Q2": {"netProfit": "", "CFOToNP": "1.0",
                       "statDate": "2026-06-30", "pubDate": "2026-08-31"}}
    errors: dict[str, str] = {}

    class ProfitClient:
        value = ""

        def profit(self, *_args, **_kwargs):
            return {"netProfit": self.value}, SimpleNamespace()

    client = ProfitClient()
    assert collector.planned_periods(rows, errors, [period]) == [period]
    collector._collect_period(client, "sh.600001", *period, rows, errors,
                              refresh=False)
    assert errors == {"2026Q2": "profit: incomplete netProfit or report date"}
    assert rows["2026Q2"]["CFOToNP"] == "1.0"

    client.value = "42"
    monkeypatch.setattr(collector, "_cash_flow",
                        lambda *_args: {"CFOToNP": "1.5"})
    collector._collect_period(client, "sh.600001", *period, rows, errors,
                              refresh=False)
    assert rows["2026Q2"]["netProfit"] == "42"
    assert errors == {}
    assert collector.planned_periods(rows, errors, [period]) == []


def test_refresh_empty_profit_keeps_cached_row_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = {"2026Q2": {"netProfit": "20", "CFOToNP": "1.0",
                       "statDate": "2026-06-30", "pubDate": "2026-08-31"}}
    errors: dict[str, str] = {}

    class EmptyProfitClient:
        def profit(self, *_args, **_kwargs):
            return None, SimpleNamespace()

    collector._collect_period(EmptyProfitClient(), "sh.600001", 2026, 2,
                              rows, errors, refresh=True)
    assert rows == {"2026Q2": {"netProfit": "20", "CFOToNP": "1.0",
                               "statDate": "2026-06-30", "pubDate": "2026-08-31"}}
    assert errors == {"2026Q2": "profit: empty response"}

    class RecoveredClient:
        def profit(self, *_args, **_kwargs):
            return {"netProfit": "21", "epsTTM": ""}, SimpleNamespace()

    monkeypatch.setattr(collector, "_cash_flow",
                        lambda *_args: {"CFOToNP": "1.5"})
    collector._collect_period(RecoveredClient(), "sh.600001", 2026, 2,
                              rows, errors, refresh=False)
    assert rows == {"2026Q2": {
        "netProfit": "21", "epsTTM": "", "CFOToNP": "1.5",
        "statDate": "2026-06-30", "pubDate": "2026-08-31",
    }}
    assert errors == {}


def test_cash_flow_query_archives_raw_rows() -> None:
    events: list[object] = []

    class Client:
        _bs = SimpleNamespace(query_cash_flow_data=lambda **_kw: "result")

        def _guard(self):
            events.append("guard")

        def _run(self, call, *, label):
            events.append(label)
            return call()

        def _drain(self, result):
            assert result == "result"
            return ["CFOToNP"], [{"CFOToNP": "1.2"}]

        def _record(self, label, rows):
            events.append((label, rows))

    assert collector._cash_flow(Client(), "sh.600001", 2026, 2) == {
        "CFOToNP": "1.2"
    }
    assert events == [
        "guard", "query_cash_flow_data(sh.600001)",
        ("cash_flow:sh.600001:2026Q2", [{"CFOToNP": "1.2"}]),
    ]
