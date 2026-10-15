"""Linux/local-fixture contracts for zero-provider finalization; no market proof."""
from datetime import date, datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))
import capture_research_simulation_evidence as capture
import finalize_saved_simulation_evidence as recovery
from aquant.domain.data.forward_archive import ForwardArchive
from aquant.operations.simulation_evidence import admit_simulation_evidence

DAY = "2026-10-14"
IID = "SH.600001"


@pytest.fixture()
def complete_capture(tmp_path):
    con = sqlite3.connect(tmp_path / "archive.sqlite")
    con.row_factory = sqlite3.Row
    archive = ForwardArchive(con, tmp_path / "archive")
    receipts = []
    def record(rows, url):
        body = json.dumps(rows).encode()
        digest, _ = archive.store_bytes(body)
        seen = datetime.now(timezone.utc)
        receipt = archive.record(source_id="baostock-socket", url=url, outcome="OK", requested_at=seen,
            responded_at=seen, content_hash=digest, byte_size=len(body))
        row = dict(con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?", (receipt.receipt_id,)).fetchone())
        row["row_count"] = len(rows)
        receipts.append(row)
        return row
    calendar_raw = [{"calendar_date": (date(2026, 1, 1) + timedelta(days=n)).isoformat(), "is_trading_day": "1"}
                    for n in range(290)]
    calendar = record(calendar_raw, "baostock://www.baostock.com:10030/query_trade_dates?start_date=2026-01-01&end_date=2026-12-31")
    days, start = capture.trading_window(calendar_raw, DAY)
    index_rows = [{"date": day, "close": "10.00"} for day in days]
    index = record(index_rows, "baostock://www.baostock.com:10030/query_history_k_data_plus?code=sh.000001&start=2026-01-01&end=2026-10-14&adjustflag=3")
    stock_row = {"code": "sh.600001", "code_name": "LOCAL FIXTURE", "tradeStatus": "1"}
    stocks = record([stock_row], "baostock://www.baostock.com:10030/query_all_stock?day=2026-10-14")
    basic_row = {"code": "sh.600001", "ipoDate": "2000-01-01", "outDate": "", "type": "1", "status": "1"}
    basic = record([basic_row], "baostock://www.baostock.com:10030/query_stock_basic?code=sh.600001")
    raw = [{"date": day, "code": "sh.600001", "open": "10.00", "high": "10.10", "low": "9.90", "close": "10.00",
            "preclose": "10.00", "volume": "10000", "amount": "100000.00", "tradestatus": "1", "isST": "0"} for day in days[-120:]]
    history = record(raw, "baostock://www.baostock.com:10030/query_history_k_data_plus?code=sh.600001&start=2026-06-17&end=2026-10-14&adjustflag=3")
    adjusted = record(raw, "baostock://www.baostock.com:10030/query_history_k_data_plus?code=sh.600001&start=2026-06-17&end=2026-10-14&adjustflag=2")
    con.close()
    research = {"snapshotId": "local-fixture", "dataAsOf": DAY, "candidates": [{"instrumentId": IID,
        "code": "600001", "displayName": "LOCAL FIXTURE", "status": "RANKED", "board": "MAIN", "exchange": "SSE",
        "industry": "C27", "position": 1}], "provenance": []}
    research_path, retained_path = tmp_path / "research.json", tmp_path / "retained.json"
    research_path.write_text(json.dumps(research), encoding="utf-8")
    retained_path.write_text(json.dumps({"schema": "aquant.simulation_retained_instruments.v1", "trading_day": DAY,
        "instrument_ids": [], "reasons": {}, "skipped_instruments": []}), encoding="utf-8")
    item = {"instrument_id": IID, "code": "600001", "exchange": "SSE", "board": "MAIN", "listed_on": "2000-01-01",
        "identity": {"official_basic_row": basic_row, "all_stock_row": stock_row, "active_in_all_stock_on_target_day": True},
        "listed_on_source": {"source_id": basic["source_id"], "receipt_id": basic["receipt_id"], "content_hash": basic["content_hash"], "available_at": basic["first_seen_at"]},
        "history_receipt": history, "adjusted_history_receipt": adjusted,
        "simulation_eligibility": {"status": "VERIFIED", "as_of_date": DAY, "is_st": False, "is_suspended": False,
            "available_at": max(history["first_seen_at"], basic["first_seen_at"]), "observed_at": history["first_seen_at"]},
        "bars": capture.build_captured_bars(raw, {row["date"]: 1000 for row in raw}, start_day=start, target_day=DAY, receipt=history)}
    cp = {"schema": "aquant.simulation_evidence_checkpoint.v1", "instruments": {IID: item}, "source_receipts": receipts,
          "failures": [], "updated_at": history["first_seen_at"], "capture_binding": {"target_day": DAY,
            "research_path": str(research_path.resolve()), "research_hash": capture.digest(research_path), "selected_instrument_ids": [IID],
            "retained_path": str(retained_path.resolve()), "retained_hash": capture.digest(retained_path)}}
    capture.save(tmp_path / "checkpoint.json", cp)
    login_attempts = [{"status": "connected", "started_at": history["first_seen_at"]}]
    capture.save(tmp_path / "login-attempts.json", {"status": "CONNECTED", "attempts": login_attempts})
    capture.finalize_capture_package(cp=cp, research=research, selected=research["candidates"], retained_ids=[], retained_path=retained_path,
        research_path=research_path, output_dir=tmp_path, target_day=DAY, calendar_raw=calendar_raw,
        calendar_rows=[{"trading_day": row["date"], "index_close_cents": 1000} for row in index_rows],
        trade_calendar_receipt=calendar, calendar_receipt=index, calendar_available_at=calendar["first_seen_at"],
        allstock_rows=[stock_row], allstock_receipt=stocks, source_receipts=receipts, login_attempts=login_attempts, limit=120)
    original_summary = json.loads((tmp_path / "summary.json").read_text())
    (tmp_path / "summary.json").unlink()
    (tmp_path / "hash-receipt.json").unlink()
    return tmp_path, research_path, retained_path, original_summary


def recover(fixture):
    root, research, retained, _ = fixture
    return recovery.finalize_existing_capture(output_dir=root, research_path=research, retained_path=retained, target_day=DAY, limit=120)


def test_recovery_uses_normal_finalizer_preserves_originals_and_passes_normal_admission(complete_capture, monkeypatch):
    root, _, _, previous = complete_capture
    monkeypatch.setattr(capture, "BaostockClient", lambda *a, **k: pytest.fail("provider constructor must not be called"))
    before = {name: capture.digest(root / name) for name in ("checkpoint.json", "cache-120-main.json", "pool-120-main.yaml", "login-attempts.json")}
    result = recover(complete_capture)
    current = json.loads((root / "summary.json").read_text())
    assert {k: v for k, v in current.items() if k != "artifact_created_at"} == {k: v for k, v in previous.items() if k != "artifact_created_at"}
    assert datetime.fromisoformat(current["artifact_created_at"]) > datetime.fromisoformat(previous["artifact_created_at"])
    assert result["providerCalls"] == result["databaseWrites"] == 0
    assert before == {name: capture.digest(root / name) for name in before}
    proof = admit_simulation_evidence(root / "summary.json", cache_path=root / "cache-120-main.json", pool_path=root / "pool-120-main.yaml", cutoff=datetime.now(timezone.utc))
    assert proof["verifiedCount"] == 1 and proof["marketDay"] == DAY
    with pytest.raises(ValueError, match="existing final summary"):
        recover(complete_capture)


@pytest.mark.parametrize("file", ["cache-120-main.json", "pool-120-main.yaml"])
def test_recovery_rejects_changed_derived_objects_without_overwriting_them(complete_capture, file):
    root, _, _, _ = complete_capture
    doc = json.loads((root / file).read_text())
    if file.endswith("json"):
        doc["bars"][IID]["rows"][0]["close_cents"] += 1
    else:
        doc["instruments"][0]["industry"] = "WRONG"
    capture.save(root / file, doc)
    before = (root / file).read_bytes()
    with pytest.raises(ValueError, match="existing (cache|pool) differs"):
        recover(complete_capture)
    assert (root / file).read_bytes() == before
    assert not (root / "summary.json").exists()


def test_recovery_rejects_partial_checkpoint_without_creating_a_final_package(complete_capture):
    root, _, _, _ = complete_capture
    cp = json.loads((root / "checkpoint.json").read_text())
    cp["instruments"][IID]["simulation_eligibility"]["status"] = "UNKNOWN"
    capture.save(root / "checkpoint.json", cp)
    before = capture.digest(root / "checkpoint.json")
    with pytest.raises(ValueError, match="every exact selected instrument"):
        recover(complete_capture)
    assert capture.digest(root / "checkpoint.json") == before
    assert not (root / "summary.json").exists()
