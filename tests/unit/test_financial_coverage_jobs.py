from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.fundamentals.fact_repository import FinancialFactRepository
from aquant.domain.fundamentals.proof_supplement import STORAGE_SCHEMA
from aquant.operations import financial_coverage_jobs as financial_jobs
from aquant.operations import research_jobs
from aquant.operations.jobs import JobStatus, JobStore


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _hash(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _append_fact(path: Path, version_id: str) -> None:
    con = sqlite3.connect(path)
    try:
        _insert_fact(con, version_id)
        con.commit()
    finally:
        con.close()


def _insert_fact(con: sqlite3.Connection, version_id: str) -> None:
    period_end = "2025-12-31" if version_id.endswith("-a") else "2024-12-31"
    con.execute(
        "INSERT INTO financial_fact (version_id,instrument_id,metric,period_end,"
        "statement_scope,profit_scope,value_kind,value_text,currency,raw_unit,"
        "source_id,source_document_id,source_published_date,source_published_at,"
        "timestamp_precision,first_seen_at,ingested_at,available_at,"
        "availability_basis,pit_mode,content_hash,supersedes_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (version_id, "600000", "revenue", period_end, "CONSOLIDATED", None,
         "VALUE", "100", "CNY", "yuan", "test", "doc-" + version_id,
         "2026-01-01", None, "DATE", "2026-01-01T00:00:00+00:00",
         "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
         "OBSERVED", "LIVE_OBSERVED", "sha256:" + version_id.ljust(64, "0")[:64], None),
    )


def _append_proof(con: sqlite3.Connection, bundle_id: str, version_id: str) -> None:
    con.execute(
        "INSERT INTO financial_fact_review(bundle_id,evidence_hash,evidence_json) "
        "VALUES (?,?,?)",
        (bundle_id, "sha256:" + bundle_id.ljust(64, "0")[:64],
         json.dumps({"bundle": bundle_id}, separators=(",", ":"))),
    )
    con.execute(
        "INSERT INTO financial_fact_review_member(bundle_id,version_id) VALUES (?,?)",
        (bundle_id, version_id),
    )
    con.execute(
        "INSERT INTO financial_fact_proof_supplement "
        "(supplement_id,original_bundle_id,evidence_hash,evidence_json) VALUES (?,?,?,?)",
        ("supp-" + bundle_id, bundle_id,
         "sha256:" + ("s" + bundle_id).ljust(64, "0")[:64],
         json.dumps({"supplement": bundle_id}, separators=(",", ":"))),
    )


@pytest.fixture(autouse=True)
def _close_repository_version_watchers():
    financial_jobs.shutdown_repository_version_cache()
    yield
    financial_jobs.shutdown_repository_version_cache()


def _direct_repository_versions(path: Path, *, include_proof: bool):
    con = connect(path, read_only=True)
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        facts = financial_jobs._table_digest(
            con, "financial_fact", order_by="sequence", required=True)
        proof = None
        if include_proof:
            reviews = financial_jobs._table_digest(
                con, "financial_fact_review", order_by="bundle_id", required=True)
            members = financial_jobs._table_digest(
                con, "financial_fact_review_member",
                order_by="bundle_id,version_id", required=True)
            supplements = financial_jobs._table_digest(
                con, "financial_fact_proof_supplement",
                order_by="supplement_id", required=False)
            proof = _hash(_canonical([reviews, members, supplements]))
        con.execute("COMMIT")
        return {"factsVersion": facts, "proofVersion": proof}
    finally:
        con.close()


def test_repository_versions_reuses_exact_digest_across_threads_and_closes(tmp_path, monkeypatch):
    path = tmp_path / "facts.sqlite"
    FinancialFactRepository(path)
    _append_fact(path, "base-a")
    with sqlite3.connect(path) as con:
        _append_proof(con, "bundle-a", "base-a")
    expected = _direct_repository_versions(path, include_proof=True)

    digest_calls = []
    original_digest = financial_jobs._table_digest

    def counted_digest(con, table, **kwargs):
        digest_calls.append(table)
        return original_digest(con, table, **kwargs)

    monkeypatch.setattr(financial_jobs, "_table_digest", counted_digest)
    first = financial_jobs.repository_versions(path, include_proof=True)
    assert first == expected
    assert digest_calls == [
        "financial_fact", "financial_fact_review",
        "financial_fact_review_member", "financial_fact_proof_supplement",
    ]
    watcher = financial_jobs._REPOSITORY_VERSION_WATCHERS[str(path.resolve())]
    assert watcher.connection.execute("PRAGMA query_only").fetchone()[0] == 1
    assert watcher.connection.in_transaction is False

    with ThreadPoolExecutor(max_workers=6) as pool:
        repeated = list(pool.map(
            lambda _index: financial_jobs.repository_versions(path, include_proof=True),
            range(12),
        ))
    assert repeated == [first] * len(repeated)
    assert digest_calls == [
        "financial_fact", "financial_fact_review",
        "financial_fact_review_member", "financial_fact_proof_supplement",
    ]
    assert watcher.connection.in_transaction is False

    watcher_connection = watcher.connection
    financial_jobs.shutdown_repository_version_cache()
    assert not financial_jobs._REPOSITORY_VERSION_WATCHERS
    with pytest.raises(sqlite3.ProgrammingError):
        watcher_connection.execute("PRAGMA data_version")


def test_repository_versions_invalidates_after_wal_fact_and_proof_commits(tmp_path):
    path = tmp_path / "wal-facts.sqlite"
    FinancialFactRepository(path)
    _append_fact(path, "base-a")
    with sqlite3.connect(path) as con:
        _append_proof(con, "bundle-a", "base-a")
    original = financial_jobs.repository_versions(path, include_proof=True)
    watcher = financial_jobs._REPOSITORY_VERSION_WATCHERS[str(path.resolve())]
    original_data_version = watcher.data_version

    writer = sqlite3.connect(path)
    try:
        assert writer.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        _insert_fact(writer, "base-b")
        writer.commit()
        assert Path(str(path) + "-wal").is_file()
        fact_updated = financial_jobs.repository_versions(path, include_proof=True)
        assert fact_updated["factsVersion"] != original["factsVersion"]
        assert fact_updated["proofVersion"] == original["proofVersion"]
        assert watcher.data_version != original_data_version
        assert watcher.connection.in_transaction is False

        current_data_version = watcher.data_version
        _append_proof(writer, "bundle-b", "base-b")
        writer.commit()
        proof_updated = financial_jobs.repository_versions(path, include_proof=True)
        assert proof_updated["factsVersion"] == fact_updated["factsVersion"]
        assert proof_updated["proofVersion"] != fact_updated["proofVersion"]
        assert watcher.data_version != current_data_version
        assert watcher.connection.in_transaction is False
    finally:
        writer.close()


def test_repository_versions_invalidates_if_wal_commit_lands_between_cache_checks(
    tmp_path, monkeypatch,
):
    path = tmp_path / "facts.sqlite"
    FinancialFactRepository(path)
    _append_fact(path, "base-a")
    original = financial_jobs.repository_versions(path, include_proof=False)
    real_read_data_version = financial_jobs._read_data_version
    inserted = False

    def commit_after_cache_check(connection):
        nonlocal inserted
        value = real_read_data_version(connection)
        if not inserted:
            inserted = True
            _append_fact(path, "cache-race-b")
        return value

    monkeypatch.setattr(financial_jobs, "_read_data_version", commit_after_cache_check)
    updated = financial_jobs.repository_versions(path, include_proof=False)

    assert inserted
    assert updated["factsVersion"] != original["factsVersion"]
    assert updated == _direct_repository_versions(path, include_proof=False)


def test_repository_versions_rejects_wal_commit_during_fingerprint(tmp_path, monkeypatch):
    path = tmp_path / "facts.sqlite"
    FinancialFactRepository(path)
    _append_fact(path, "base-a")
    original = financial_jobs.repository_versions(path, include_proof=False)
    # Force the next request through a real scan before injecting the writer.
    # A warm cache correctly skips _table_digest and therefore cannot exercise
    # the mid-scan guard by itself.
    financial_jobs.shutdown_repository_version_cache()
    real_digest = financial_jobs._table_digest
    inserted = False

    def commit_during_scan(con, table, **kwargs):
        nonlocal inserted
        digest = real_digest(con, table, **kwargs)
        if table == "financial_fact" and not inserted:
            inserted = True
            _append_fact(path, "midscan-b")
        return digest

    monkeypatch.setattr(financial_jobs, "_table_digest", commit_during_scan)
    with pytest.raises(financial_jobs.FinancialCheckError) as failure:
        financial_jobs.repository_versions(path, include_proof=False)

    assert inserted
    assert failure.value.code == "FINANCIAL_CHECK_INPUT_NOT_READY"
    assert str(path.resolve()) not in financial_jobs._REPOSITORY_VERSION_WATCHERS
    assert financial_jobs.repository_versions(path, include_proof=False) == (
        _direct_repository_versions(path, include_proof=False))


def test_repository_versions_invalidates_when_database_path_is_replaced(tmp_path):
    path = tmp_path / "facts.sqlite"
    FinancialFactRepository(path)
    _append_fact(path, "original-a")
    original = financial_jobs.repository_versions(path, include_proof=False)
    path_key = str(path.resolve())
    old_watcher = financial_jobs._REPOSITORY_VERSION_WATCHERS[path_key]

    replacement_source = tmp_path / "replacement-source.sqlite"
    FinancialFactRepository(replacement_source)
    _append_fact(replacement_source, "replacement-a")
    replacement = tmp_path / "replacement.sqlite"
    source_connection = sqlite3.connect(replacement_source)
    replacement_writer = sqlite3.connect(replacement)
    try:
        source_connection.backup(replacement_writer)
        assert replacement_writer.execute(
            "PRAGMA journal_mode=DELETE").fetchone()[0].lower() == "delete"
    finally:
        source_connection.close()
        replacement_writer.close()
    for sidecar in (Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        sidecar.unlink(missing_ok=True)
    os.replace(replacement, path)

    updated = financial_jobs.repository_versions(path, include_proof=False)
    new_watcher = financial_jobs._REPOSITORY_VERSION_WATCHERS[path_key]
    assert updated["factsVersion"] != original["factsVersion"]
    assert new_watcher is not old_watcher
    with pytest.raises(sqlite3.ProgrammingError):
        old_watcher.connection.execute("PRAGMA data_version")


def test_repository_version_watcher_count_is_bounded_and_shutdown_closes_all(tmp_path):
    first_connection = None
    for index in range(financial_jobs._REPOSITORY_VERSION_CACHE_LIMIT + 1):
        path = tmp_path / f"facts-{index}.sqlite"
        FinancialFactRepository(path)
        _append_fact(path, f"fact-{index}-a")
        financial_jobs.repository_versions(path, include_proof=False)
        if index == 0:
            first_connection = financial_jobs._REPOSITORY_VERSION_WATCHERS[
                str(path.resolve())].connection
    assert len(financial_jobs._REPOSITORY_VERSION_WATCHERS) == (
        financial_jobs._REPOSITORY_VERSION_CACHE_LIMIT)
    assert first_connection is not None
    with pytest.raises(sqlite3.ProgrammingError):
        first_connection.execute("PRAGMA data_version")

    remaining_connections = [watcher.connection
                              for watcher in financial_jobs._REPOSITORY_VERSION_WATCHERS.values()]
    financial_jobs.shutdown_repository_version_cache()
    for connection in remaining_connections:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("PRAGMA data_version")


def _capture_env(tmp_path: Path, monkeypatch, priorities):
    research = tmp_path / "research.json"
    research.write_text(json.dumps({
        "snapshotId": "research-snapshot-1",
        "provenance": [{"role": "facts_db", "path": str(tmp_path / "reconstructed.sqlite")}],
    }), encoding="utf-8")
    live = tmp_path / "live.sqlite"
    reconstructed = tmp_path / "reconstructed.sqlite"
    FinancialFactRepository(live)
    FinancialFactRepository(reconstructed)
    target = tmp_path / "target-map.json"
    target.write_text('{"version":1}', encoding="utf-8")
    archive = tmp_path / "archive"
    archive.mkdir()
    monkeypatch.setenv("AQUANT_RESEARCH_CANDIDATES_FILE", str(research))
    monkeypatch.setenv("AQUANT_LIVE_FINANCIAL_FACTS_DB", str(live))
    monkeypatch.setenv("AQUANT_S2_TARGET_PERIOD_MAP", str(target))
    monkeypatch.setenv("AQUANT_FORWARD_ARCHIVE_ROOT", str(archive))
    monkeypatch.setattr(financial_jobs, "_priority_ids",
                        lambda _data_dir, _subject: list(priorities))
    return research, live, reconstructed, target


def test_priority_read_leaves_a_missing_store_absent_and_rejects_a_file(tmp_path):
    assert financial_jobs._priority_ids(tmp_path, "user:alice") == []
    store = tmp_path / "forward-comparison"
    assert not store.exists()
    store.write_text("not a directory", encoding="utf-8")
    with pytest.raises(financial_jobs.FinancialCheckError, match="研究优先目标暂不可读取"):
        financial_jobs._priority_ids(tmp_path, "user:alice")


def test_overlapping_priority_failure_does_not_mask_research_failure(tmp_path, monkeypatch):
    priority_started = Event()
    research_error = financial_jobs.FinancialCheckError("RESEARCH", "research failed", "restore")

    def read_priorities(_root, _subject):
        priority_started.set()
        raise financial_jobs.FinancialCheckError("PRIORITY", "priority failed", "restore")

    def read_research(_path):
        assert priority_started.wait(timeout=5)
        raise research_error

    monkeypatch.setattr(financial_jobs, "_priority_ids", read_priorities)
    monkeypatch.setattr(financial_jobs, "_research_packet_for_binding", read_research)
    with pytest.raises(financial_jobs.FinancialCheckError) as error:
        financial_jobs.capture_financial_check_inputs(
            data_dir=tmp_path, snapshot_id="snap-test", trading_day="2026-10-30",
            snapshot_cutoff_at="2026-10-30T07:00:00+00:00", subject="user:alice")
    assert error.value is research_error


def test_warm_financial_repository_versions_preloads_configured_sources(
    tmp_path, monkeypatch,
):
    _research, live, reconstructed, _target = _capture_env(
        tmp_path, monkeypatch, ["SH.600000"])
    _append_fact(live, "live-a")
    _append_fact(reconstructed, "reconstructed-a")
    digest_calls = []
    original_digest = financial_jobs._table_digest

    def counted_digest(con, table, **kwargs):
        digest_calls.append((Path(con.execute("PRAGMA database_list").fetchone()[2]).name,
                             table))
        return original_digest(con, table, **kwargs)

    monkeypatch.setattr(financial_jobs, "_table_digest", counted_digest)
    assert financial_jobs.warm_financial_repository_versions() is None

    live_watcher = financial_jobs._REPOSITORY_VERSION_WATCHERS[str(live.resolve())]
    reconstructed_watcher = financial_jobs._REPOSITORY_VERSION_WATCHERS[
        str(reconstructed.resolve())]
    assert live_watcher.proof_known is True
    assert reconstructed_watcher.proof_known is False
    assert live_watcher.connection.execute("PRAGMA query_only").fetchone()[0] == 1
    assert reconstructed_watcher.connection.execute("PRAGMA query_only").fetchone()[0] == 1
    assert digest_calls == [
        ("live.sqlite", "financial_fact"),
        ("live.sqlite", "financial_fact_review"),
        ("live.sqlite", "financial_fact_review_member"),
        ("live.sqlite", "financial_fact_proof_supplement"),
        ("reconstructed.sqlite", "financial_fact"),
    ]

    digest_calls.clear()
    assert financial_jobs.warm_financial_repository_versions() is None
    assert digest_calls == []


def test_input_binding_is_stable_and_changes_with_each_report_input(tmp_path, monkeypatch):
    research, live, reconstructed, target = _capture_env(
        tmp_path, monkeypatch, ["SH.600000"])
    _append_fact(live, "live-a")
    _append_fact(reconstructed, "reconstructed-a")
    args = {"data_dir": tmp_path, "snapshot_id": "snap-1",
            "trading_day": "2026-10-30",
            "snapshot_cutoff_at": "2026-10-30T07:00:00+00:00",
            "subject": "user:alice"}

    original = financial_jobs.capture_financial_check_inputs(**args)
    repeated = financial_jobs.capture_financial_check_inputs(**args)
    assert repeated["inputBinding"]["inputHash"] == original["inputBinding"]["inputHash"]

    previous = original["inputBinding"]["inputHash"]
    _append_fact(live, "live-b")
    updated = financial_jobs.capture_financial_check_inputs(**args)
    assert updated["inputBinding"]["inputHash"] != previous
    previous = updated["inputBinding"]["inputHash"]

    with sqlite3.connect(live) as con:
        con.execute(
            "INSERT INTO financial_fact_review(bundle_id,evidence_hash,evidence_json) "
            "VALUES (?,?,?)", ("bundle-a", "sha256:" + "a" * 64, '{"proof":1}'))
        con.execute(
            "INSERT INTO financial_fact_review_member(bundle_id,version_id) VALUES (?,?)",
            ("bundle-a", "live-a"),
        )
        con.executescript(STORAGE_SCHEMA)
        con.execute(
            "INSERT INTO financial_fact_proof_supplement "
            "(supplement_id,original_bundle_id,evidence_hash,evidence_json) VALUES (?,?,?,?)",
            ("supp-a", "bundle-a", "sha256:" + "b" * 64, '{"supplement":1}'),
        )
    updated = financial_jobs.capture_financial_check_inputs(**args)
    assert updated["inputBinding"]["inputHash"] != previous
    previous = updated["inputBinding"]["inputHash"]

    _append_fact(reconstructed, "reconstructed-b")
    updated = financial_jobs.capture_financial_check_inputs(**args)
    assert updated["inputBinding"]["inputHash"] != previous
    previous = updated["inputBinding"]["inputHash"]

    research.write_text(research.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    updated = financial_jobs.capture_financial_check_inputs(**args)
    assert updated["inputBinding"]["inputHash"] != previous
    previous = updated["inputBinding"]["inputHash"]

    target.write_text('{"version":2}', encoding="utf-8")
    updated = financial_jobs.capture_financial_check_inputs(**args)
    assert updated["inputBinding"]["inputHash"] != previous
    previous = updated["inputBinding"]["inputHash"]

    monkeypatch.setattr(financial_jobs, "_priority_ids",
                        lambda _data_dir, _subject: ["SH.600001"])
    updated = financial_jobs.capture_financial_check_inputs(**args)
    assert updated["inputBinding"]["inputHash"] != previous
    previous = updated["inputBinding"]["inputHash"]

    monkeypatch.setattr(financial_jobs, "FINANCIAL_CHECK_VERSION", "financial-check-test-next")
    updated = financial_jobs.capture_financial_check_inputs(**args)
    assert updated["inputBinding"]["inputHash"] != previous


@pytest.mark.parametrize("indent", [1, 2])
def test_research_metadata_binding_skips_large_candidate_payload(tmp_path, monkeypatch, indent):
    _research, _live, _reconstructed, _target = _capture_env(
        tmp_path, monkeypatch, ["SH.600000"])
    path = tmp_path / "research-candidates.json"
    packet = {
        "schema": "aquant.research-candidates.v2",
        "candidates": [{"instrumentId": f"SH.{i:06d}", "name": "x" * 256,
                        "snapshotId": "nested-snapshot", "provenance": []}
                       for i in range(5000)],
        "snapshotId": "research-snapshot-large",
        "provenance": [{"role": "facts_db", "path": str(_reconstructed)}],
    }
    path.write_text(json.dumps(packet, ensure_ascii=False, indent=indent),
                    encoding="utf-8")
    monkeypatch.setenv("AQUANT_RESEARCH_CANDIDATES_FILE", str(path))

    captured = financial_jobs.capture_financial_check_inputs(
        data_dir=tmp_path, snapshot_id="snap-large", trading_day="2026-10-30",
        snapshot_cutoff_at="2026-10-30T07:00:00+00:00", subject="user:alice")
    binding = captured["inputBinding"]
    assert path.stat().st_size > 1_000_000
    assert binding["researchSnapshotId"] == "research-snapshot-large"
    assert binding["researchContentHash"] == _hash(path.read_bytes())
    assert captured["inputSpec"]["reconstructedFactsDb"] == str(_reconstructed)


def test_compact_research_metadata_uses_supported_fallback(tmp_path):
    path = tmp_path / "compact-research.json"
    packet = {
        "schema": "aquant.research-candidates.v2",
        "snapshotId": "compact-snapshot",
        "provenance": [{"role": "facts_db", "path": str(tmp_path / "facts.sqlite")}],
    }
    path.write_text(json.dumps(packet, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")

    metadata, content_hash, reconstructed = financial_jobs._research_packet_for_binding(path)

    assert metadata == {"snapshotId": "compact-snapshot", "provenance": packet["provenance"]}
    assert content_hash == _hash(path.read_bytes())
    assert reconstructed == tmp_path / "facts.sqlite"


@pytest.mark.parametrize("field,value", [("snapshotId", "other"), ("provenance", [])])
def test_repeated_root_binding_fields_are_rejected(tmp_path, field, value):
    path = tmp_path / "duplicate-research.json"
    path.write_text('{\n "snapshotId": "valid",\n "provenance": [],\n "' +
                    field + '": ' + json.dumps(value) + '\n}', encoding="utf-8")
    with pytest.raises(financial_jobs.FinancialCheckError) as failure:
        financial_jobs._research_packet_for_binding(path)
    assert failure.value.code == "FINANCIAL_CHECK_INPUT_NOT_READY"


@pytest.mark.parametrize("packet", [
    {"schema": "aquant.research-candidates.v2", "snapshotId": 7, "provenance": []},
    {"schema": "aquant.research-candidates.v2", "snapshotId": "valid", "provenance": {}},
])
def test_invalid_research_metadata_is_rejected(tmp_path, packet):
    path = tmp_path / "invalid-research.json"
    path.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")

    with pytest.raises(financial_jobs.FinancialCheckError) as failure:
        financial_jobs._research_packet_for_binding(path)

    assert failure.value.code == "FINANCIAL_CHECK_INPUT_NOT_READY"


def test_financial_dispatch_persists_read_only_result_reference_and_hides_paths(tmp_path, monkeypatch):
    meta_path = tmp_path / "meta.sqlite"
    con = connect(meta_path)
    apply_migrations(con)
    research_path = tmp_path / "research.json"
    research_path.write_text("{}", encoding="utf-8")
    research_hash = _hash(research_path.read_bytes())
    live_path = tmp_path / "live.sqlite"
    binding = {
        "snapshotId": "snap-bound", "snapshotCutoffAt": "2026-10-30T07:00:00+00:00",
        "tradingDay": "2026-10-30", "researchSnapshotId": "research-snapshot-1",
        "researchContentHash": research_hash, "liveFactsVersion": "sha256:" + "1" * 64,
        "proofVersion": "sha256:" + "2" * 64,
        "reconstructedFactsVersion": None, "targetMapHash": _hash(b"no-target-period-map"),
        "priorityInstrumentIdsHash": _hash(_canonical([])),
        "codeVersion": financial_jobs.FINANCIAL_CHECK_VERSION,
    }
    binding["inputHash"] = _hash(_canonical(binding))
    payload = {
        "inputBinding": binding,
        "inputSpec": {
            "dataDir": str(tmp_path), "researchCandidatesFile": str(research_path),
            "liveFactsDb": str(live_path), "reconstructedFactsDb": None,
            "targetPeriodMapFile": None, "marketCapsArchiveRoot": str(tmp_path),
            "priorityInstrumentIds": [],
        },
    }
    job_id, _ = JobStore(con).submit(
        job_type=financial_jobs.JOB_FINANCIAL_CHECK, trading_day="2026-10-30",
        config_version=financial_jobs.FINANCIAL_CHECK_VERSION,
        input_snapshot_id="snap-bound", payload=payload,
    )
    job = JobStore(con).get(job_id)

    monkeypatch.setattr(financial_jobs, "load_simulation_research", lambda _path: (
        {"provenance": []}, {"researchContentHash": research_hash,
                             "researchSnapshotId": "research-snapshot-1"}))
    monkeypatch.setattr(financial_jobs, "repository_versions", lambda _path, include_proof: {
        "factsVersion": binding["liveFactsVersion"],
        "proofVersion": binding["proofVersion"] if include_proof else None,
    })
    report = {
        "schema": "aquant.financial_coverage.v1", "snapshotId": "snap-bound",
        "snapshotCutoffAt": binding["snapshotCutoffAt"], "tradingDay": binding["tradingDay"],
        "generatedAt": "2026-11-01T01:02:03+00:00",
        "formalS2": {"factsDb": "C:/private/live.sqlite", "strategyFamilyStatus": "CLOSED"},
        "historicalReconstructed": {"factsDb": "C:/private/reconstructed.sqlite"},
    }
    monkeypatch.setattr(financial_jobs, "build_financial_coverage_report",
                        lambda **_kwargs: report)
    guard_calls = []
    result = financial_jobs.run_financial_check_job(job, write_guard=lambda: guard_calls.append(True))
    job.result_json = json.dumps(result)
    public_result = financial_jobs.read_financial_check_result(tmp_path, job)

    assert result["resultSizeBytes"] > 0
    assert len(guard_calls) == 2
    assert public_result["report"]["formalS2"]["strategyFamilyStatus"] == "CLOSED"
    assert "factsDb" not in public_result["report"]["formalS2"]
    assert "private" not in json.dumps(public_result, ensure_ascii=False)
    monkeypatch.setattr(financial_jobs, "FINANCIAL_CHECK_VERSION", "financial-check-test-next")
    assert financial_jobs.read_financial_check_result(tmp_path, job) == public_result
    metadata = json.loads(job.result_json)
    metadata["inputBinding"]["snapshotId"] = "snap-other"
    job.result_json = json.dumps(metadata)
    with pytest.raises(financial_jobs.FinancialCheckError) as mismatch:
        financial_jobs.read_financial_check_result(tmp_path, job)
    assert mismatch.value.code == "FINANCIAL_CHECK_RESULT_UNAVAILABLE"
    con.close()


def test_financial_job_dispatch_records_stale_input_as_failed(tmp_path, monkeypatch):
    con = connect(tmp_path / "meta.sqlite")
    apply_migrations(con)
    job_id, _ = JobStore(con).submit(
        job_type=financial_jobs.JOB_FINANCIAL_CHECK, trading_day="2026-10-30",
        config_version=financial_jobs.FINANCIAL_CHECK_VERSION,
        input_snapshot_id="snap-1", payload={"inputBinding": {}, "inputSpec": {}},
    )
    from aquant.operations.financial_coverage_jobs import FinancialCheckError

    def stale(_job, *, write_guard=None):
        raise FinancialCheckError("FINANCIAL_CHECK_INPUT_CHANGED", "changed", "resubmit")

    monkeypatch.setattr(financial_jobs, "run_financial_check_job", stale)
    outcome = research_jobs.run_pending(
        con, None, limit=1, worker_id="financial-worker",
        job_types=[financial_jobs.JOB_FINANCIAL_CHECK],
    )
    assert outcome[0]["status"] == "FAILED"
    assert JobStore(con).get(job_id).error_code == "FINANCIAL_CHECK_INPUT_CHANGED"
    assert JobStore(con).get(job_id).lease_owner is None
    con.close()


def test_expired_financial_job_is_recovered_once_by_next_worker(tmp_path, monkeypatch):
    con = connect(tmp_path / "meta.sqlite")
    apply_migrations(con)
    job_id, _ = JobStore(con).submit(
        job_type=financial_jobs.JOB_FINANCIAL_CHECK, trading_day="2026-10-30",
        config_version=financial_jobs.FINANCIAL_CHECK_VERSION,
        input_snapshot_id="snap-1", payload={"safe": True},
    )
    abandoned = JobStore(con).claim(
        "crashed-financial-worker", lease_seconds=0,
        job_types=[financial_jobs.JOB_FINANCIAL_CHECK],
    )
    assert abandoned is not None and abandoned.job_id == job_id
    calls = []
    monkeypatch.setattr(financial_jobs, "run_financial_check_job",
                        lambda _job, *, write_guard=None: calls.append("run") or {"ok": True})

    outcome = research_jobs.run_pending(
        con, None, limit=1, worker_id="recovery-worker",
        job_types=[financial_jobs.JOB_FINANCIAL_CHECK],
    )
    recovered = JobStore(con).get(job_id)
    assert outcome[0]["status"] == "SUCCEEDED"
    assert recovered.attempt_count == 2
    assert recovered.status is JobStatus.SUCCEEDED
    assert calls == ["run"]
    con.close()
