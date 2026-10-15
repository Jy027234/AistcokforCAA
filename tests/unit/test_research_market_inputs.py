"""Offline market admission must reconstruct identities, units and scope."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from aquant.domain.data.forward_archive import ForwardArchive


def digest(body):
    return "sha256:" + hashlib.sha256(body).hexdigest()


@pytest.fixture
def verifier(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "tools"))
    import verify_research_market_inputs
    return verify_research_market_inputs


@pytest.fixture
def industry(tmp_path, verifier):
    raw = [{"code": "sz.000001", "code_name": "银行", "industry": "J66货币金融服务",
            "industryClassification": verifier.CSRC, "updateDate": "2026-10-05"}]
    raw += [dict(raw[0]), {**raw[0], "code": "sh.689009"}, {**raw[0], "code": "bj.920001"}]
    body = json.dumps(raw, ensure_ascii=False).encode()
    (tmp_path / "raw.json").write_bytes(body)
    start, end = "2026-10-09T09:14:32+00:00", "2026-10-09T09:14:39+00:00"
    rows = [{"code": "000001", "instrument_id": "SZ.000001", "org_id": "org1", "exchange": "SZ",
             "baostock_code_raw": r["code"], "code_name_raw": r["code_name"], "industry_raw": r["industry"],
             "industry_classification_raw": r["industryClassification"], "classification_system": verifier.CSRC,
             "classification_asof_raw": r["updateDate"], "source_row_index_for_duplicate_code": ix,
             "duplicate_source_code_row_count": 2, "classification_status": "classified"}
            for ix, r in enumerate(raw[:2])]
    packet = {"schema": "aquant.baostock_research_industry.v1", "classification_system": verifier.CSRC,
              "source_receipt": {"label": "stock_industry:ALL", "stored_path": "raw.json", "content_hash": digest(body),
                                 "requested_at": end, "record_count": 4},
              "source_content_hash_verified": True, "source_record_count": 4, "official_universe_count": 2,
              "matched_official_code_count": 1, "matched_rows_by_exchange": {"SZ": 2}, "rows": rows,
              "missing_official_codes": ["920001"], "unjoined_source_rows": raw[3:],
              "source_rows_outside_official_universe": raw[2:3],
              "captured_from": start, "capture_started_at": start, "prepared_at": start,
              "captured_to": end, "capture_finished_at": end, "generated_at": end,
              "capture_time_semantics": "collector start before login; returned rows persisted at finish",
              "source_receipt_timestamp_semantics": "persistence time, not network request start"}
    return packet, {"000001": "org1", "920001": "org2"}, tmp_path


def test_industry_preserves_duplicates_missing_outside_and_persistence_time(industry, verifier):
    verifier.verify_industry(*industry)


@pytest.mark.parametrize("mutation", ["org", "exchange", "classification", "asof", "duplicate", "omission",
    "missing", "outside", "unjoined", "hash", "count", "time", "escape"])
def test_industry_rejects_identity_scope_source_or_time_forgery(industry, verifier, mutation):
    packet, universe, root = industry
    packet = deepcopy(packet)
    if mutation in ("org", "exchange", "classification", "asof"):
        field = {"org": "org_id", "exchange": "instrument_id", "classification": "industry_raw", "asof": "classification_asof_raw"}[mutation]
        packet["rows"][0][field] = "forged"
    elif mutation == "duplicate":
        packet["rows"][1] = deepcopy(packet["rows"][0])
    elif mutation == "omission":
        packet["rows"].pop()
    elif mutation in ("missing", "outside", "unjoined"):
        packet[{"missing": "missing_official_codes", "outside": "source_rows_outside_official_universe", "unjoined": "unjoined_source_rows"}[mutation]] = []
    elif mutation == "hash":
        (root / "raw.json").write_bytes(b"[]")
    elif mutation == "count":
        packet["source_receipt"]["record_count"] = 3
    elif mutation == "time":
        packet["captured_to"] = packet["captured_from"]
    else:
        packet["source_receipt"]["stored_path"] = "../raw.json"
    with pytest.raises(ValueError):
        verifier.verify_industry(packet, universe, root)


def build_history(tmp_path, verifier, provider, raw=None, url=None):
    iid = "SZ.002039"
    start, end = "2026-06-22", "2026-10-09"
    if provider == "baostock-socket":
        raw = raw if raw is not None else [{"date": start, "open": "1.005", "close": "1.015", "high": "2.345",
                  "low": "0.995", "volume": "9158497", "amount": "182611888.455"},
                  {"date": end, "open": "", "close": "1.235", "high": "", "low": None,
                   "volume": "", "amount": ""}]
        url = url or f"baostock://www.baostock.com:10030/query_history_k_data_plus?code=sz.002039&start={start}&end={end}&adjustflag=3"
    else:
        raw = raw if raw is not None else {"code": 0, "data": {"sz002039": {"day": [
            [start, "1.005", "1.015", "2.345", "0.995", "98.335"],
            [end, "", "1.235", "", None, ""]]}}}
        url = url or f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sz002039,day,{start},{end},640,"
    body = json.dumps(raw).encode()
    con = sqlite3.connect(tmp_path / "archive.sqlite")
    archive = ForwardArchive(con, tmp_path / "archive")
    content_hash, _ = archive.store_bytes(body)
    requested, responded = "2026-10-09T09:10:03+00:00", "2026-10-09T09:10:04+00:00"
    receipt = archive.record(source_id=provider, url=url, outcome="OK", requested_at=datetime.fromisoformat(requested),
                             responded_at=datetime.fromisoformat(responded), content_hash=content_hash, byte_size=len(body),
                             http_status=200 if provider == "tencent-ifzq" else None)
    con.close()
    # Independent expected values expose half-even precision and provider volume units.
    rows = [{"trading_day": start, "open_cents": 100, "close_cents": 102, "high_cents": 234, "low_cents": 100,
             "volume_shares": 9158497 if provider == "baostock-socket" else 9834,
             "amount_cents": 18261188846 if provider == "baostock-socket" else None,
             "amount_status": "source_returned_value" if provider == "baostock-socket" else "provider_not_offered"},
            {"trading_day": end, "open_cents": None, "close_cents": 124, "high_cents": None, "low_cents": None,
             "volume_shares": None, "amount_cents": None,
             "amount_status": "source_returned_null" if provider == "baostock-socket" else "provider_not_offered"}]
    metadata = {"receipt_id": receipt.receipt_id, "source_id": provider, "source_url": url, "requested_at": requested,
                "responded_at": responded, "observed_at": responded, "actual_hash": content_hash, "outcome": "OK",
                "http_status": 200 if provider == "tencent-ifzq" else None}
    frozen = json.dumps({"schema": "aquant.research-candidates.v2", "researchOnly": True, "simulationAvailable": False,
                        "candidates": [{"instrumentId": iid, "status": "RANKED"}, {"instrumentId": "SH.600001", "status": "RANKED"}]}).encode()
    (tmp_path / "input-research-candidates.json").write_bytes(frozen)
    packet = {"schema": "aquant.research_daily_history.v1", "requested_window": {"start": start, "end": end, "adjustment": "unadjusted"},
              "bars": {iid: {**{k: v for k, v in metadata.items() if k not in ("source_id", "outcome", "http_status")},
                              "source": provider, "content_hash": content_hash, "rows": rows}},
              "source_receipts": [metadata], "captured_from": requested, "captured_to": responded, "data_asof": end,
              "expected_ranked_count": 2, "coverage": {"ranked_count": 2, "success_count": 1, "missing_count": 1,
                  "baostock_count": int(provider == "baostock-socket"), "tencent_fallback_count": int(provider == "tencent-ifzq")},
              "source_archive": {"sqlite": str(tmp_path / "archive.sqlite"), "tencent_raw_root": str(tmp_path / "archive/raw")},
              "input": {"path": str(tmp_path / "input-research-candidates.json"), "content_hash": digest(frozen)}}
    return packet, {"002039": "org1", "600001": "org2"}


@pytest.mark.parametrize("provider", ["baostock-socket", "tencent-ifzq"])
def test_history_rebuilds_integer_cents_volume_and_nulls_with_partial_coverage(tmp_path, verifier, provider):
    verifier.verify_history_refresh(*build_history(tmp_path, verifier, provider))


@pytest.mark.parametrize("mutation", ["identity", "amount", "volume", "null", "fraction", "bool", "time", "receipt_time",
    "duplicate", "omission", "extra_receipt", "coverage", "asof", "hash", "input_hash", "input_targets"])
def test_history_rejects_field_receipt_or_scope_forgery(tmp_path, verifier, mutation):
    packet, universe = build_history(tmp_path, verifier, "tencent-ifzq")
    entry = next(iter(packet["bars"].values()))
    if mutation in ("amount", "volume", "null", "fraction", "bool"):
        field, value = {"amount": ("amount_cents", 0), "volume": ("volume_shares", 98), "null": ("open_cents", 0),
                        "fraction": ("open_cents", 100.0), "bool": ("open_cents", True)}[mutation]
        entry["rows"][1 if mutation == "null" else 0][field] = value
    elif mutation == "identity":
        packet["bars"]["SH.600001"] = packet["bars"].pop("SZ.002039")
    elif mutation == "time":
        packet["captured_to"] = "2026-10-10T00:00:00+00:00"
    elif mutation == "receipt_time":
        entry["observed_at"] = "2026-10-10T00:00:00+00:00"
    elif mutation == "duplicate":
        entry["rows"][1] = deepcopy(entry["rows"][0])
    elif mutation == "omission":
        entry["rows"].pop()
    elif mutation == "extra_receipt":
        packet["source_receipts"].append({**packet["source_receipts"][0], "receipt_id": "unused"})
    elif mutation == "coverage":
        packet["coverage"]["missing_count"] = 0
    elif mutation == "asof":
        packet["data_asof"] = "2026-10-10"
    elif mutation == "hash":
        next((tmp_path / "archive/raw").glob("*/*")).write_bytes(b"{}")
    elif mutation == "input_hash":
        packet["input"]["content_hash"] = "forged"
    else:
        packet["expected_ranked_count"] = 1
    with pytest.raises(ValueError):
        verifier.verify_history_refresh(packet, universe)


@pytest.mark.parametrize("fault", ["identity", "duplicate", "date", "window", "adjusted", "nonfinite"])
def test_history_rejects_invalid_original_responses_even_with_valid_hash(tmp_path, verifier, fault):
    row = ["2026-06-22", "1.005", "1.015", "2.345", "0.995", "98.335"]
    raw = {"data": {"sz002039": {"day": [row]}}}
    url = None
    if fault == "identity":
        raw["data"]["sh600001"] = raw["data"].pop("sz002039")
    elif fault == "duplicate":
        raw["data"]["sz002039"]["day"].append(list(row))
    elif fault == "date":
        row[0] = "bad-date"
    elif fault == "window":
        row[0] = "2026-06-21"
    elif fault == "nonfinite":
        row[2] = "NaN"
    else:
        url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sz002039,qfqday,2026-06-22,2026-10-09,640,qfq"
    packet, universe = build_history(tmp_path, verifier, "tencent-ifzq", raw, url)
    with pytest.raises(ValueError):
        verifier.verify_history_refresh(packet, universe)


@pytest.mark.parametrize("provider", ["baostock-socket", "tencent-ifzq"])
def test_history_retains_missing_close_without_silently_dropping_row(tmp_path, verifier, provider):
    if provider == "baostock-socket":
        raw = [{"date": "2026-06-22", "open": "1.005", "close": "1.015", "high": "2.345",
                "low": "0.995", "volume": "9158497", "amount": "182611888.455"},
               {"date": "2026-10-09", "open": "", "close": "", "high": "", "low": None,
                "volume": "", "amount": ""}]
    else:
        raw = {"data": {"sz002039": {"day": [
            ["2026-06-22", "1.005", "1.015", "2.345", "0.995", "98.335"],
            ["2026-10-09", "", "", "", None, ""]]}}}
    packet, universe = build_history(tmp_path, verifier, provider, raw)
    entry = next(iter(packet["bars"].values()))
    entry["rows"][1]["close_cents"] = None
    verifier.verify_history_refresh(packet, universe)
    entry["rows"].pop()
    with pytest.raises(ValueError, match="omitted"):
        verifier.verify_history_refresh(packet, universe)


def add_latest_refresh(tmp_path, packet, *, raw=None, symbol="sz.002039"):
    entry = packet["bars"]["SZ.002039"]
    end = packet["requested_window"]["end"]
    raw = raw if raw is not None else [{"date": end, "open": "2.005", "close": "", "high": "2.025",
                                        "low": "", "volume": "1.5", "amount": ""}]
    body = json.dumps(raw).encode()
    con = sqlite3.connect(tmp_path / "archive.sqlite")
    archive = ForwardArchive(con, tmp_path / "archive")
    content_hash, _ = archive.store_bytes(body)
    url = f"baostock://www.baostock.com:10030/query_history_k_data_plus?code={symbol}&start={end}&end={end}&adjustflag=3"
    requested, responded = "2026-10-09T09:43:03+00:00", "2026-10-09T09:43:04+00:00"
    receipt = archive.record(source_id="baostock-socket", url=url, outcome="OK",
                             requested_at=datetime.fromisoformat(requested), responded_at=datetime.fromisoformat(responded),
                             content_hash=content_hash, byte_size=len(body))
    con.close()
    metadata = {"receipt_id": receipt.receipt_id, "source_id": "baostock-socket", "source_url": url,
                "requested_at": requested, "responded_at": responded, "observed_at": responded,
                "actual_hash": content_hash, "outcome": "OK", "http_status": None}
    packet["source_receipts"].append(metadata)
    packet["captured_to"] = responded
    packet["data_asof"] = end
    entry["history_capture"] = {key: entry[key] for key in (
        "receipt_id", "source_url", "requested_at", "responded_at", "observed_at", "actual_hash")}
    entry["latest_refresh"] = {"requested_end": end, "receipt": metadata, "status": "BAR_RETURNED"}
    row = {"trading_day": end, "open_cents": 200, "close_cents": None, "high_cents": 202,
           "low_cents": None, "volume_shares": 2, "amount_cents": None, "amount_status": "source_returned_null",
           "source_receipt_id": receipt.receipt_id, "source_actual_hash": content_hash,
           "source_observed_at": responded, "source_url": url}
    entry["rows"] = [r for r in entry["rows"] if r["trading_day"] != end] + [row]
    packet["coverage"]["bar_count"] = len(entry["rows"])


@pytest.mark.parametrize("replace", [False, True])
def test_incremental_day_adds_or_replaces_only_end_day_and_preserves_both_receipts(tmp_path, verifier, replace):
    raw = None if replace else [{"date": "2026-06-22", "open": "1.005", "close": "1.015", "high": "2.345",
                                "low": "0.995", "volume": "9158497", "amount": "182611888.455"}]
    packet, universe = build_history(tmp_path, verifier, "baostock-socket", raw)
    entry = packet["bars"]["SZ.002039"]
    if not replace:
        entry["rows"] = entry["rows"][:1]
    original_hash = entry["actual_hash"]
    add_latest_refresh(tmp_path, packet)
    verifier.verify_history_refresh(packet, universe)
    assert entry["actual_hash"] == original_hash != entry["rows"][-1]["source_actual_hash"]
    assert len(packet["source_receipts"]) == 2
    assert entry["rows"][-1]["close_cents"] is None


@pytest.mark.parametrize("fault", ["security", "raw_security", "date", "raw_bytes", "missing_receipt", "row_receipt",
    "row_hash", "row_time", "row_url", "row_amount", "history_capture", "requested_end", "bar_count", "capture_end", "base_row_source"])
def test_incremental_history_rejects_forged_binding_rows_receipts_and_bounds(tmp_path, verifier, fault):
    packet, universe = build_history(tmp_path, verifier, "baostock-socket")
    raw = None
    if fault in ("date", "raw_security"):
        raw = [{"date": "2026-10-08" if fault == "date" else "2026-10-09", "code": "sh.600001",
                "open": "2.005", "close": "", "high": "2.025", "low": "", "volume": "1.5", "amount": ""}]
        if fault == "date":
            raw[0].pop("code")
    add_latest_refresh(tmp_path, packet, raw=raw, symbol="sh.600001" if fault == "security" else "sz.002039")
    entry = packet["bars"]["SZ.002039"]
    latest, last = entry["latest_refresh"], entry["rows"][-1]
    if fault == "raw_bytes":
        bare = latest["receipt"]["actual_hash"][7:]
        (tmp_path / "archive/raw" / bare[:2] / bare).write_bytes(b"[]")
    elif fault == "missing_receipt":
        packet["source_receipts"].pop()
    elif fault in ("row_receipt", "row_hash", "row_time", "row_url", "row_amount"):
        field = {"row_receipt": "source_receipt_id", "row_hash": "source_actual_hash", "row_time": "source_observed_at",
                 "row_url": "source_url", "row_amount": "amount_cents"}[fault]
        last[field] = 0 if fault == "row_amount" else "forged"
    elif fault == "history_capture":
        entry["history_capture"]["actual_hash"] = latest["receipt"]["actual_hash"]
    elif fault == "requested_end":
        latest["requested_end"] = "2026-10-08"
    elif fault == "bar_count":
        packet["coverage"]["bar_count"] = 3
    elif fault == "capture_end":
        packet["captured_to"] = entry["responded_at"]
    elif fault == "base_row_source":
        entry["rows"][0]["source_receipt_id"] = latest["receipt"]["receipt_id"]
    with pytest.raises(ValueError):
        verifier.verify_history_refresh(packet, universe)
