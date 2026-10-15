"""Reconstruction must preserve observed time and ingest complete five-field bundles."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import ingest_reconstructed_facts as ingestion
from aquant.domain.fundamentals.fact_repository import FinancialFactRepository


def _record():
    return {"status": "RECONSTRUCTED_READY", "instrument_id": "600519",
        "period_end": "2026-06-30", "available_at": "2026-08-17T09:00:00+08:00",
        "observed_at": "2026-09-02T00:00:00+00:00",
        "values": {key: "1" for key in ingestion.REQUIRED},
        "announcement": {"announcement_id": "fixture", "published_date": "2026-08-15",
            "index_page_hash": "sha256:" + "a" * 64}}


def _run(tmp_path, record):
    source = tmp_path / "bound.json"
    source.write_text(json.dumps({"records": [record],
        "generated_at": "2026-10-07T00:00:00+00:00"}), encoding="utf-8")
    target = tmp_path / "facts.sqlite"
    assert ingestion.main(["--bound", str(source), "--repository", str(target), "--commit"]) == 0
    return target


def test_missing_record_observation_does_not_use_generation_time(tmp_path):
    row = _record()
    row.pop("observed_at")
    assert not _run(tmp_path, row).exists()


def test_bad_last_field_does_not_ingest_partial_bundle(tmp_path):
    row = _record()
    row["values"][ingestion.REQUIRED[-1]] = "invalid decimal"
    assert not _run(tmp_path, row).exists()


def test_valid_bundle_keeps_source_observation_and_actual_ingestion_time(tmp_path):
    row = _record()
    target = _run(tmp_path, row)
    store, _ = FinancialFactRepository.open_existing(target).load_with_accepted_s2_pdf_reviews()
    assert len(store.facts) == 5
    assert all(fact.first_seen_at.isoformat() == row["observed_at"] for fact in store.facts)
    assert all(fact.ingested_at > fact.first_seen_at for fact in store.facts)


def test_old_and_new_security_codes_can_link_the_same_announcement(tmp_path):
    rows = [dict(_record(), instrument_id=code) for code in ("832982", "920982")]
    source = tmp_path / "bound.json"
    source.write_text(json.dumps({"records": rows}), encoding="utf-8")
    target = tmp_path / "facts.sqlite"
    assert ingestion.main(["--bound", str(source), "--repository", str(target), "--commit"]) == 0
    store, _ = FinancialFactRepository.open_existing(target).load_with_accepted_s2_pdf_reviews()
    assert len(store.facts) == 10
    assert len({fact.version_id for fact in store.facts}) == 10
    assert {fact.source_document_id for fact in store.facts} == {"fixture"}
