from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from aquant.domain.data.db import connect
from aquant.domain.data.forward_archive import ForwardArchive
from aquant.operations.simulation_financial_evidence import (
    FinancialEvidenceError, admit_simulation_financial_evidence, freeze_financial_inputs,
)

START = datetime(2026, 10, 12, 17, 50, tzinfo=timezone.utc)
END = START + timedelta(seconds=3)


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def package(tmp_path, *, profit="123.45", pub="2026-08-21", status="COLLECTED"):
    base_path = tmp_path / "base.json"
    cache_path = tmp_path / "cache.json"
    evidence_path = tmp_path / "evidence.json"
    base = {"created_at": "2026-09-15T00:00:00+00:00", "updated_at": "2026-09-24T00:00:00+00:00",
            "statements": {"SH.600938": {"2026Q2": {"old": "unchanged"}}}}
    row = {"code": "sh.600323", "netProfit": profit, "statDate": "2026-06-30", "pubDate": pub}
    cache = json.loads(json.dumps(base))
    cache["updated_at"] = END.isoformat()
    if status == "COLLECTED":
        cache["statements"]["SH.600323"] = {"2026Q2": row}
    archive_root = tmp_path / "archive"
    db = tmp_path / "archive.sqlite"
    with closing(connect(db)) as con:
        archive = ForwardArchive(con, archive_root)
        body = json.dumps([row] if status != "NO_RECORD" else []).encode()
        digest, _ = archive.store_bytes(body)
        receipt = archive.record(source_id="baostock-socket",
            url="baostock://www.baostock.com:10030/query_profit_data?code=sh.600323&year=2026&quarter=2",
            outcome="OK", requested_at=START, responded_at=END - timedelta(seconds=1),
            content_hash=digest, byte_size=len(body), detail="profit:sh.600323:2026Q2; raw=original")
        declared = {key: getattr(receipt, key) for key in
                    ("receipt_id", "source_id", "url", "content_hash", "byte_size")}
        for key in ("requested_at", "responded_at", "first_seen_at"):
            declared[key] = getattr(receipt, key).isoformat()
        declared["row_count"] = 0 if status == "NO_RECORD" else 1
    manifest = {"schema": "aquant.simulation-financial-evidence.v1", "cachePath": str(cache_path),
        "baseCachePath": str(base_path), "baseCacheHash": dump(base_path, base), "cacheHash": dump(cache_path, cache),
        "capturedFrom": START.isoformat(), "capturedTo": END.isoformat(),
        "archiveDbPath": str(db), "archiveRoot": str(archive_root), "targets": ["SH.600323"],
        "periods": ["2026Q2"], "records": [{"instrumentId": "SH.600323", "periodKey": "2026Q2",
            "status": status, "receipt": declared, "profitRow": row if status != "NO_RECORD" else None,
            "reason": "provider did not return a usable period" if status != "COLLECTED" else None}]}
    dump(evidence_path, manifest)
    return evidence_path, manifest


def test_exact_response_identity_values_receipts_and_inherited_rows_admitted(tmp_path):
    path, manifest = package(tmp_path, profit="-12.50")
    result = admit_simulation_financial_evidence(path, financials_path=manifest["cachePath"],
        cutoff=END, expected_instrument_ids=["SH.600323"])
    assert result["cache"]["statements"]["SH.600323"]["2026Q2"]["netProfit"] == "-12.50"
    assert result["cache"]["statements"]["SH.600938"] == {"2026Q2": {"old": "unchanged"}}
    assert result["provenance"]["collectedRecordCount"] == 1
    frozen = freeze_financial_inputs(result["provenance"], data_root=tmp_path / "snapshot")
    assert set(frozen) == {"cache", "evidence", "inherited_base"}
    assert freeze_financial_inputs(result["provenance"], data_root=tmp_path / "snapshot") == frozen


def test_actual_collector_shape_has_pair_targets_and_separate_identity_scope(tmp_path):
    path, manifest = package(tmp_path)
    target = {"instrumentId": "SH.600323", "providerCode": "sh.600323",
              "periodKey": "2026Q2", "year": 2026, "quarter": 2}
    demand_path = tmp_path / "demand.json"
    manifest["targets"] = [target]
    manifest["scopeInstrumentIds"] = ["SH.600323"]
    manifest["demandPath"] = str(demand_path)
    manifest["demandHash"] = dump(demand_path, {"schema": "aquant.financial-optional-demand.v1", "records": [target]})
    dump(path, manifest)
    actual = admit_simulation_financial_evidence(path, cutoff=END)
    assert actual["provenance"]["scopeInstrumentIds"] == ["SH.600323"]
    assert actual["provenance"]["collectedRecordCount"] == 1
    manifest["targets"][0]["quarter"] = 1
    dump(path, manifest)
    with pytest.raises(FinancialEvidenceError, match="year/quarter"):
        admit_simulation_financial_evidence(path, cutoff=END)


@pytest.mark.parametrize("kind", ["value", "inherited", "receipt_time", "duplicate", "cutoff", "body"])
def test_new_optional_values_cannot_hide_changed_sources_or_lookahead(tmp_path, kind):
    path, manifest = package(tmp_path)
    if kind in {"value", "inherited"}:
        cache = json.loads((tmp_path / "cache.json").read_text())
        if kind == "value":
            cache["statements"]["SH.600323"]["2026Q2"]["netProfit"] = "999"
        else:
            cache["statements"]["SH.600938"] = {"2026Q2": {"old": "modified"}}
        manifest["cacheHash"] = dump(tmp_path / "cache.json", cache)
    elif kind == "receipt_time":
        manifest["records"][0]["receipt"]["first_seen_at"] = START.isoformat()
    elif kind == "duplicate":
        manifest["records"].append(manifest["records"][0])
    elif kind == "body":
        for raw in (tmp_path / "archive/raw").rglob("*"):
            if raw.is_file():
                raw.write_bytes(b"tampered")
    dump(path, manifest)
    with pytest.raises(FinancialEvidenceError):
        admit_simulation_financial_evidence(path, cutoff=START if kind == "cutoff" else END,
            expected_instrument_ids=["SH.600323"])


def test_shared_financial_cache_is_admitted_when_the_next_research_pool_changes(tmp_path):
    path, _ = package(tmp_path)
    result = admit_simulation_financial_evidence(path, cutoff=END,
                                               expected_instrument_ids=["SH.600001"])
    assert result["provenance"]["verifiedReceiptCount"] == 1
    assert result["provenance"]["poolProjectedInstrumentIds"] == []
    assert result["provenance"]["outsidePublicationPoolCount"] == 1
    # The compiler validates the full immutable source; publication is the
    # later projection into only the snapshot's exact instrument IDs.
    assert result["cache"]["statements"]["SH.600323"]["2026Q2"]["netProfit"] == "123.45"


def test_publisher_projects_verified_shared_cache_into_changed_pool_without_external_rows(tmp_path):
    from test_universe_snapshot_financials import _write_inputs
    from aquant.operations.universe_snapshot import _build_document
    path, manifest = package(tmp_path)
    (tmp_path / "market").mkdir()
    inputs = _write_inputs(tmp_path / "market", financials="{}")
    doc, instruments, _, _ = _build_document(
        cache=json.loads(inputs["cache"].read_text()), pool=json.loads(inputs["pool"].read_text()),
        window_start=None, window_end=None, window=None, financials_path=tmp_path / "cache.json",
        actions_path=inputs["actions"], market_caps_path=inputs["market_caps"],
        check_market_caps=False, checks=[], financial_evidence_path=path, financial_cutoff=END)
    assert doc["financials"]["statements"] == {}
    assert not {"SH.600323", "SH.600938"}.intersection(i["instrument_id"] for i in instruments)
    assert doc["financials"]["evidence_status"] == "VERIFIED_RECEIPT_BACKED_OPTIONAL"
    assert doc["financials"]["financial_evidence"]["verifiedReceiptCount"] == 1


@pytest.mark.parametrize("profit,pub", [("NaN", "2026-08-21"), ("Infinity", "2026-08-21"),
                                          ("100", "2026-10-14"), ("100", "2026-06-01")])
def test_invalid_or_future_supplier_amounts_do_not_enter_snapshot(tmp_path, profit, pub):
    path, _ = package(tmp_path, profit=profit, pub=pub)
    with pytest.raises(FinancialEvidenceError):
        admit_simulation_financial_evidence(path, cutoff=END)


@pytest.mark.parametrize("status", ["NO_RECORD", "FAILED"])
def test_unavailable_records_are_reported_without_becoming_facts(tmp_path, status):
    path, manifest = package(tmp_path, status=status)
    result = admit_simulation_financial_evidence(path, cutoff=END)
    assert "SH.600323" not in result["cache"]["statements"]
    assert result["provenance"]["partial"]
    assert result["provenance"]["noRecordCount" if status == "NO_RECORD" else "failedRecordCount"] == 1
