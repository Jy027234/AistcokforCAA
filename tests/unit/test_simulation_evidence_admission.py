from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from aquant.domain.data.forward_archive import ForwardArchive
from aquant.operations.simulation_evidence import admit_simulation_evidence, freeze_simulation_inputs
from aquant.operations.universe_snapshot import _build_document


SEEN = datetime(2026, 10, 11, 1, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 10, 11, 2, tzinfo=timezone.utc)


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def package(tmp_path):
    con = sqlite3.connect(tmp_path / "archive.sqlite")
    con.row_factory = sqlite3.Row
    archive = ForwardArchive(con, tmp_path / "archive")
    receipts = []

    def record(rows, url):
        body = json.dumps(rows).encode()
        digest, _ = archive.store_bytes(body)
        saved = archive.record(source_id="baostock-socket", url=url, outcome="OK",
                               requested_at=SEEN, responded_at=SEEN,
                               content_hash=digest, byte_size=len(body))
        value = dict(con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?",
                                 (saved.receipt_id,)).fetchone())
        receipts.append(value)
        return value

    calendar = record([
        {"calendar_date": "2026-10-08", "is_trading_day": "1"},
        {"calendar_date": "2026-10-09", "is_trading_day": "1"},
        {"calendar_date": "2026-10-10", "is_trading_day": "0"},
        {"calendar_date": "2026-10-12", "is_trading_day": "1"}], "calendar")
    history_rows = [{"date": "2026-10-09", "code": "sh.600001", "open": "10.00",
                     "high": "10.10", "low": "9.90", "close": "10.05", "preclose": "9.99",
                     "volume": "10000", "amount": "100500.00", "tradestatus": "1", "isST": "0"}]
    history = record(history_rows, "query?code=sh.600001&adjustflag=3")
    adjusted = record([dict(history_rows[0], close="9.80")], "query?code=sh.600001&adjustflag=2")
    basic_row = {"code": "sh.600001", "ipoDate": "2000-01-01", "outDate": "", "type": "1", "status": "1"}
    basic = record([basic_row], "basic")
    stock_row = {"code": "sh.600001", "tradeStatus": "1", "code_name": "test"}
    record([stock_row], "query_all_stock?day=2026-10-09")
    con.close()
    eligibility = {"status": "VERIFIED", "as_of_date": "2026-10-09", "is_st": False,
                   "is_suspended": False, "observed_at": history["first_seen_at"],
                   "available_at": history["first_seen_at"]}
    bar = {"trading_day": "2026-10-09", "open_cents": 1000, "high_cents": 1010,
           "low_cents": 990, "close_cents": 1005, "prev_close_cents": 999,
           "adjusted_close_cents": 980, "volume_shares": 10000, "amount_cents": 10050000,
           "tradestatus": "1", "isST": "0"}
    source = tmp_path / "research.json"
    write(source, {"candidates": [{"instrumentId": "SH.600001", "code": "600001", "board": "MAIN", "industry": "C27"}]})
    summary = {"schema": "aquant.simulation_research_evidence.v1", "evidence_market_date": "2026-10-09",
               "source_inputs": {"research_candidates": {"path": str(source), "sha256": "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()}},
               "source_receipts": receipts, "instruments": [{"instrument_id": "SH.600001", "code": "600001",
               "listed_on": "2000-01-01", "exchange": "SSE", "board": "MAIN", "identity": {"official_basic_row": basic_row, "all_stock_row": stock_row},
               "simulation_eligibility": eligibility, "history_receipt": history,
               "adjusted_history_receipt": adjusted, "listed_on_source": {k: basic[k] for k in ("source_id", "receipt_id", "content_hash")}}]}
    cache = {"schema": "aquant.simulation_universe_cache.v1", "calendar_receipt": calendar,
             "calendar_available_at": calendar["first_seen_at"], "trading_calendar": ["2026-10-08", "2026-10-09"],
             "bars": {"SH.600001": {"listed_on": "2000-01-01", "exchange": "SSE", "board": "MAIN", "simulation_eligibility": eligibility,
                       "prev_close_before_window": 999, "rows": [bar]}}}
    pool = {"instruments": [{"instrument_id": "SH.600001", "listed_on": "2000-01-01", "exchange": "SSE", "board": "MAIN", "industry": "C27"}]}
    paths = {"summary": tmp_path / "summary.json", "cache": tmp_path / "cache.json", "pool": tmp_path / "pool.json"}
    values = {"summary": summary, "cache": cache, "pool": pool}
    for key in paths:
        write(paths[key], values[key])
    return paths, values


def admit(paths, cutoff=CUTOFF):
    return admit_simulation_evidence(paths["summary"], cache_path=paths["cache"], pool_path=paths["pool"], cutoff=cutoff)


def test_future_known_calendar_is_not_a_future_price_or_a_backdated_observation(package):
    paths, _ = package
    proof = admit(paths)
    assert proof["calendar"][-1] == "2026-10-12"
    assert proof["marketDay"] == "2026-10-09"
    assert proof["decisionCutoffAt"] == CUTOFF.isoformat()
    with pytest.raises(ValueError, match="unavailable"):
        admit(paths, datetime(2026, 10, 9, 7, tzinfo=timezone.utc))


@pytest.mark.parametrize("change", ["price", "future_bar", "status", "identity", "industry", "exchange", "active_identity", "input"])
def test_edited_price_status_identity_and_research_are_rejected(package, change):
    paths, values = package
    if change == "price":
        values["cache"]["bars"]["SH.600001"]["rows"][0]["close_cents"] += 1
    elif change == "future_bar":
        values["cache"]["bars"]["SH.600001"]["rows"].append(dict(values["cache"]["bars"]["SH.600001"]["rows"][0], trading_day="2026-10-12"))
    elif change == "status":
        # Even editing both derived files cannot replace the original receipt.
        values["summary"]["instruments"][0]["simulation_eligibility"]["is_st"] = True
    elif change == "identity":
        values["pool"]["instruments"][0]["listed_on"] = "2026-10-09"
    elif change == "industry":
        values["pool"]["instruments"][0]["industry"] = "fake industry"
    elif change == "exchange":
        values["pool"]["instruments"][0]["exchange"] = "SZSE"
    elif change == "active_identity":
        values["summary"]["instruments"][0]["identity"]["all_stock_row"]["tradeStatus"] = "0"
    else:
        paths["summary"].parent.joinpath("research.json").write_text("changed research")
    for key in paths:
        write(paths[key], values[key])
    with pytest.raises(ValueError):
        admit(paths)


def test_raw_archive_tampering_cannot_be_hidden_by_editing_a_summary(package):
    paths, values = package
    digest = values["summary"]["instruments"][0]["history_receipt"]["content_hash"].removeprefix("sha256:")
    paths["summary"].parent.joinpath("archive/raw", digest[:2], digest).write_bytes(b"[]")
    with pytest.raises(ValueError, match="raw response hash"):
        admit(paths)


def test_source_original_is_retained_after_daily_publication_replaces_it(package):
    paths, _ = package
    frozen = freeze_simulation_inputs(paths["summary"], cache_path=paths["cache"], pool_path=paths["pool"],
                                     data_root=paths["summary"].parent / "product")
    from pathlib import Path
    original = Path(frozen["research_candidates"]["originalPath"])
    original_bytes = original.read_bytes()
    original.write_text("tomorrow's research", encoding="utf-8")
    assert Path(frozen["research_candidates"]["path"]).read_bytes() == original_bytes
    assert Path(frozen["simulation_summary"]["path"]).read_bytes() == paths["summary"].read_bytes()


def test_publisher_omits_halt_quotes_and_keeps_provider_ex_dividend_reference(package):
    paths, values = package
    cached = values["cache"]["bars"]["SH.600001"]
    # A halted day may repeat prices. A later ex-dividend preclose can differ
    # from the last trading close and must not be overwritten with that close.
    cached["rows"] = [dict(cached["rows"][0], trading_day="2026-10-08", tradestatus="0"),
                      dict(cached["rows"][0], prev_close_cents=970)]
    values["cache"]["trading_calendar"] = ["2026-10-08", "2026-10-09"]
    _, instruments, quotes, _ = _build_document(cache=values["cache"], pool=values["pool"],
        window_start=None, window_end=None, window=None, financials_path=paths["summary"].parent / "missing-financials",
        actions_path=paths["summary"].parent / "missing-actions", market_caps_path=paths["summary"].parent / "missing-caps",
        check_market_caps=False, checks=[])
    assert len(quotes) == 1
    assert quotes[0]["trading_day"] == "2026-10-09"
    assert quotes[0]["prev_close_cents"] == 970
    assert instruments[0]["simulation_eligibility"]["status"] == "VERIFIED"
