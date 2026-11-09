"""Research pool suitability, explanations and whole-list query semantics."""
import json
import hashlib
import os
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from test_s2_formula_engine import _facts
from aquant.domain.data.pit import AvailabilityBasis, PitMode
from aquant.operations.research_candidates import ResearchCandidateError, file_hash
from aquant.operations import research_scan
from aquant.operations.research_scan import build_research_scan, load_research_scan, _refresh_history


def inputs():
    codes = ["600001", "600002", "600003", "920001"]
    stamp = datetime(2026, 8, 17, tzinfo=timezone.utc)
    facts = [replace(f, value=Decimal(f.value), content_hash="sha256:" + "a" * 64,
        available_at=stamp, first_seen_at=stamp, ingested_at=stamp,
        availability_basis=AvailabilityBasis.RECONSTRUCTED, pit_mode=PitMode.HISTORICAL_RECONSTRUCTED)
        for code in codes[:3] for f in _facts(code)]
    bound = {}
    for fact in facts:
        key = (fact.instrument_id, fact.period_end.isoformat())
        row = bound.setdefault(key, {"instrument_id": key[0], "period_end": key[1],
            "status": "RECONSTRUCTED_READY", "values": {}, "announcement": {
                "announcement_id": fact.source_document_id, "title": "fixture report", "published_date": "2026-08-15",
                "url": "https://static.cninfo.com.cn/fixture.PDF", "index_receipt_id": "fixture-receipt",
                "index_page_hash": fact.content_hash}})
        row["values"][fact.metric] = str(fact.value)
    rows = [{"code": c, "org_id": c, "display_name": "fixture", "quote_time_raw": "20261009114530",
        "volume_raw": "100", "last_price_raw": "10", "total_market_cap_raw": "0.00001",
        "receipt_id": "receipt-" + c, "content_hash": "sha256:" + "b" * 64,
        "responded_at": "2026-10-09T03:46:00+00:00", "source_url": "https://qt.gtimg.cn/q=sh" + c}
        for c in codes]
    quotes = {"universe_codes": codes, "rows": rows,
        "captured_from": "2026-10-09T03:45:30+00:00", "captured_to": "2026-10-09T03:46:00+00:00"}
    industries = {"records": [{"code": c, "industry": industry, "updateDate": "2026-09-14"}
        for c, industry in zip(codes, ("C39工业", "C39工业", "J66金融", ""))]}
    return quotes, dict(facts=facts, bound={"records": list(bound.values())}, industries=industries,
                        history={"bars": {}}, names={c: "fixture-" + c for c in codes}, provenance=[])


def test_full_observation_list_and_financial_template_are_separate():
    quotes, kwargs = inputs()
    packet = build_research_scan(quotes, **kwargs)
    assert packet["poolSize"] == 4 and packet["rankedCount"] == 2
    financial = next(r for r in packet["candidates"] if r["code"] == "600003")
    assert financial["status"] == "FINANCIAL" and financial["signalRank"] is None
    assert financial["financialBridge"] and not financial["factors"]
    assert not financial["counterEvidence"][0]["noneFound"]
    assert packet["scope"]["unknownIndustryCount"] == 1
    assert not packet["simulationAvailable"]


def test_later_disclosure_refresh_keeps_quote_market_day_and_ranked_candidates():
    quotes, kwargs = inputs()
    prior = build_research_scan(quotes, **kwargs)
    later = build_research_scan(quotes, **kwargs, announcements={
        "captured_from": "2026-10-11T15:25:43+00:00", "captured_to": "2026-10-11T15:26:31+00:00",
        "window": {"start": "2026-09-11", "end": "2026-10-11"}, "per_code": [], "documents": []})
    assert later["dataAsOf"] == prior["dataAsOf"] == "2026-10-09"
    assert later["asOfTime"].startswith("2026-10-11")
    assert later["rankedCount"] == prior["rankedCount"] == 2
    assert [(c["code"], c["signalRank"]) for c in later["candidates"]] == [
        (c["code"], c["signalRank"]) for c in prior["candidates"]]


def test_complete_ttm_bridge_formula_contribution_and_sourced_counter_evidence():
    quotes, kwargs = inputs()
    card = build_research_scan(quotes, **kwargs)["candidates"][0]
    assert Decimal(card["factors"]["F08"]) == Decimal("0.5")
    assert next(x for x in card["financialBridge"] if x["label"] == "归母净利润 TTM")["value"] == "110"
    assert sum(x["contribution"] for x in card["rankBreakdown"]) == pytest.approx(card["signalRank"])
    assert [x["weight"] for x in card["rankBreakdown"]] == [1/6, 1/6, 1/6, 1/2]
    assert len(card["basisEvidence"]) <= 3
    assert any("现金生成低于账面利润" in c["statement"] and c["sourceIds"] for c in card["counterEvidence"])


@pytest.mark.parametrize("change,expected", [("inactive", "QUOTE_INACTIVE"), ("cap_missing", "MARKET_CAP_INVALID"),
    ("stale", "QUOTE_STALE_IN_BATCH"), ("wrong_day", "QUOTE_WRONG_DAY"), ("alias", "ACTIVE_ALIAS_AMBIGUOUS")])
def test_bad_current_quotes_remain_observable_without_ranking(change, expected):
    quotes, kwargs = inputs()
    row = quotes["rows"][0]
    if change == "inactive": row["volume_raw"] = "0"
    if change == "cap_missing": row["total_market_cap_raw"] = ""
    if change == "stale": row["batch_lag_seconds"] = 301
    if change == "wrong_day": row["quote_time_raw"] = "20261008114530"
    if change == "alias": quotes["rows"][1]["org_id"] = row["org_id"]
    packet = build_research_scan(quotes, **kwargs)
    card = next(c for c in packet["candidates"] if c["code"] == row["code"])
    assert card["signalRank"] is None and card["exclusionCode"] == expected
    assert packet["poolSize"] == 4


def test_unavailable_facts_are_not_replaced_by_zero_or_another_report():
    quotes, kwargs = inputs()
    kwargs["facts"] = [f for f in kwargs["facts"] if not (f.instrument_id == "600001" and f.metric == "revenue" and f.period_end.year == 2024)]
    card = next(c for c in build_research_scan(quotes, **kwargs)["candidates"] if c["code"] == "600001")
    assert card["signalRank"] is None and card["exclusionCode"] == "S2_TTM_INCOMPLETE"


def test_query_searches_entire_frozen_list_and_tampered_explanations_are_rejected(tmp_path, monkeypatch):
    quotes, kwargs = inputs()
    packet = build_research_scan(quotes, **kwargs)
    source = tmp_path / "frozen.json"
    source.write_text("{}", encoding="utf-8")
    packet["provenance"] = [{"role": "fixture", "path": str(source), "contentHash": file_hash(source)}]
    expected = dict(packet)
    monkeypatch.setattr("aquant.operations.research_scan.rebuild_scan", lambda s: expected)
    path = tmp_path / "scan.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    decoded = json.loads(path.read_text(encoding="utf-8"))
    read_text = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *args, **kwargs: pytest.fail("packet was decoded twice")
                        if self == path else read_text(self, *args, **kwargs))
    assert load_research_scan(path, limit=1, offset=1, packet=decoded)["candidates"][0]["position"] == 2
    monkeypatch.setattr(Path, "read_text", read_text)
    result = load_research_scan(path, view="all", q="920001")
    assert result["filteredCount"] == 1 and result["candidates"][0]["signalRank"] is None
    assert load_research_scan(path, view="financial")["candidates"][0]["code"] == "600003"
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["candidates"][0]["basisEvidence"][0]["statement"] = "invented claim"
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ResearchCandidateError, match="explanations"):
        load_research_scan(path)


def test_scan_rebuild_decodes_verified_bytes_and_warm_cache_rechecks_content(tmp_path, monkeypatch):
    bodies = {
        "quotes": (b'{"rows": []}', ".json"),
        "bound_records": (b'{"records": []}', ".json"),
        "facts_db": (b"fixture database bytes; not opened by this test", ".sqlite"),
        "industry_snapshot": (b'{"records": []}', ".json"),
        "price_history": (b'{"bars": {}}', ".json"),
        "company_names": (b'{"stockList": []}', ".json"),
    }
    sources = []
    paths = {}
    for role, (body, suffix) in bodies.items():
        path = tmp_path / (role + suffix)
        path.write_bytes(body)
        paths[role] = path
        sources.append((role, str(path), file_hash(path)))
    sources = tuple(sorted(sources))

    research_scan.rebuild_scan.cache_clear()
    snapshot_dir = tmp_path / "verified-inputs"
    snapshot_dir.mkdir()
    resolved, decoded = research_scan._read_verified_scan_inputs(
        sources, facts_snapshot_dir=snapshot_dir)
    assert resolved["quotes"] == paths["quotes"]
    assert resolved["facts_db"] == snapshot_dir / "facts_db.sqlite"
    assert resolved["facts_db"].read_bytes() == paths["facts_db"].read_bytes()
    assert decoded["quotes"] == {"rows": []}
    assert decoded["industry_snapshot"] == {"records": []}

    monkeypatch.setattr(research_scan, "_build_research_scan",
                        lambda sources, paths, inputs: {"verified": inputs["quotes"]})
    assert research_scan.rebuild_scan(sources) == {"verified": {"rows": []}}

    original_stat = paths["quotes"].stat()
    paths["quotes"].write_bytes(b'{"rows": {}}')
    os.utime(paths["quotes"], ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    with pytest.raises(ResearchCandidateError, match="input has changed: quotes"):
        research_scan.rebuild_scan(sources)
    research_scan.rebuild_scan.cache_clear()


def test_warm_scan_rehashes_all_sources_with_at_most_two_workers(tmp_path, monkeypatch, request):
    bodies = {
        "quotes": (b'{"rows": []}', ".json"),
        "bound_records": (b'{"records": []}', ".json"),
        "facts_db": (b"fixture database bytes; not opened by this test", ".sqlite"),
        "industry_snapshot": (b'{"records": []}', ".json"),
        "price_history": (b'{"bars": {}}', ".json"),
        "company_names": (b'{"stockList": []}', ".json"),
        **{f"archive_{index}": (f"archive-{index}".encode(), ".bin") for index in range(4)},
    }
    sources = []
    for role, (body, suffix) in bodies.items():
        path = tmp_path / (role + suffix)
        path.write_bytes(body)
        sources.append((role, str(path), file_hash(path)))
    sources = tuple(sorted(sources))

    research_scan.rebuild_scan.cache_clear()
    request.addfinalizer(research_scan.rebuild_scan.cache_clear)
    monkeypatch.setattr(research_scan, "_build_research_scan",
                        lambda sources, paths, inputs: {"verified": True})
    assert research_scan.rebuild_scan(sources) == {"verified": True}

    original_verify = research_scan._verify_source_hash
    paired_checks = threading.Barrier(2)
    active = 0
    max_active = 0
    verified_roles = []
    state_lock = threading.Lock()

    def observe_verify(path, expected_hash, role):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
            verified_roles.append(role)
        try:
            paired_checks.wait(timeout=3)
            return original_verify(path, expected_hash, role)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(research_scan, "_verify_source_hash", observe_verify)
    assert research_scan.rebuild_scan(sources) == {"verified": True}
    assert sorted(verified_roles) == sorted(role for role, _path, _digest in sources)
    assert max_active == 2


def _seed_verified_context_projection(tmp_path):
    source = tmp_path / "frozen-source-00.json"
    source_paths = []
    for index in range(10):
        current = tmp_path / f"frozen-source-{index:02d}.json"
        current.write_bytes(f"source-{index}".encode("ascii"))
        source_paths.append(current)
    packet = {
        "schema": research_scan.SCHEMA,
        "researchOnly": True,
        "simulationAvailable": False,
        "snapshotId": "research-projection-fixture",
        "dataAsOf": "2026-10-09",
        "capturedTo": "2026-10-09T17:00:00+08:00",
        "generatedAt": "2026-10-09T21:30:00+08:00",
        "asOfTime": "2026-10-09T17:00:00+08:00",
        "provenance": [{"role": f"fixture-{index}", "path": str(current),
                        "contentHash": file_hash(current)}
                       for index, current in enumerate(source_paths)],
        "candidates": [
            {"instrumentId": "SH.600001", "displayName": "ranked fixture", "position": 1,
             "exchange": "SSE", "board": "MAIN", "status": "RANKED", "signalRank": 0.8,
             "quote": {"providerName": "fixture", "observedAt": "2026-10-09T17:00:00+08:00",
                       "availableAt": "2026-10-09T17:00:00+08:00", "discard": "quote payload"},
             "industryClassification": {"observedAt": "2026-10-09T18:00:00+08:00",
                                        "availableAt": "2026-10-09T18:00:00+08:00", "discard": "industry payload"},
             "evidence": [{"observedAt": "2026-10-10T07:00:00+08:00",
                           "availableAt": "2026-10-10T08:00:00+08:00", "discard": "evidence payload"}],
             "discard": "large financial bridge"},
            {"instrumentId": "SZ.000001", "status": "EXCLUDED", "position": None,
             "quote": {}, "industryClassification": {},
             "evidence": [{"observedAt": "2026-10-10T09:00:00+08:00",
                           "availableAt": "2026-10-10T10:00:00+08:00"}]},
        ],
    }
    body = json.dumps(packet, ensure_ascii=False).encode("utf-8")
    artifact = tmp_path / "research-scan.json"
    artifact.write_bytes(body)
    sources = tuple(sorted((item["role"], item["path"], item["contentHash"])
                           for item in packet["provenance"]))
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    key = (str(artifact.resolve()), digest, sources, research_scan._RESEARCH_SCAN_CACHE_VERSION)
    research_scan.rebuild_scan.cache_clear()
    research_scan._SCAN_CACHE[sources] = {"verified": True}
    research_scan._VERIFIED_SCAN_CACHE[key] = {
        "sources": sources, "packet": packet, "body": body,
    }
    return artifact, source, packet, sources, key, digest


def test_verified_context_projection_is_small_immutable_and_exact_sha_cached(
        tmp_path, monkeypatch, request):
    artifact, _source, packet, sources, key, digest = _seed_verified_context_projection(tmp_path)
    request.addfinalizer(research_scan.rebuild_scan.cache_clear)
    verified_roles = []
    original_verify = research_scan._verify_source_hash
    original_hash = research_scan._hash_resolved_file
    artifact_hash_reads = []

    def counted_verify(path, expected_hash, role):
        verified_roles.append(role)
        return original_verify(path, expected_hash, role)

    def counted_hash(path):
        if Path(path).resolve() == artifact.resolve():
            artifact_hash_reads.append(str(path))
        return original_hash(path)

    monkeypatch.setattr(research_scan, "_verify_source_hash", counted_verify)
    monkeypatch.setattr(research_scan, "_hash_resolved_file", counted_hash)
    projection, actual_digest = research_scan.load_verified_research_scan_context_projection(artifact)
    assert actual_digest == digest
    assert len(projection["candidates"]) == len(packet["candidates"]) == 2
    assert projection["candidates"][1]["status"] == "EXCLUDED"
    assert projection["candidates"][0]["quote"]["providerName"] == "fixture"
    assert projection["candidates"][0]["evidence"][0]["availableAt"] == "2026-10-10T08:00:00+08:00"
    assert "discard" not in projection["candidates"][0]
    assert "discard" not in projection["candidates"][0]["quote"]
    assert isinstance(projection["candidates"], tuple)
    with pytest.raises(TypeError):
        projection["candidates"][0]["quote"]["providerName"] = "caller mutation"
    with pytest.raises(TypeError):
        projection["candidates"][0]["evidence"][0]["observedAt"] = "1900-01-01"

    second, second_digest = research_scan.load_verified_research_scan_context_projection(artifact)
    assert second is projection and second_digest == digest
    assert research_scan._VERIFIED_SCAN_CACHE[key]["context_projection"] is projection
    assert verified_roles == [f"fixture-{index}" for index in range(10)] * 2
    assert len(artifact_hash_reads) == 4  # Full artifact hash before and after each request.
    assert projection["candidates"][0]["quote"]["providerName"] == "fixture"


def test_verified_context_projection_rejects_source_replacement_with_restored_mtime(
        tmp_path, request):
    artifact, source, _packet, _sources, _key, _digest = _seed_verified_context_projection(tmp_path)
    request.addfinalizer(research_scan.rebuild_scan.cache_clear)
    research_scan.load_verified_research_scan_context_projection(artifact)

    original_stat = source.stat()
    source.write_bytes(b"source-B")
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    with pytest.raises(ResearchCandidateError, match="input has changed: fixture-0"):
        research_scan.load_verified_research_scan_context_projection(artifact)


def test_verified_context_projection_rejects_artifact_replacement_during_projection(
        tmp_path, monkeypatch, request):
    artifact, _source, packet, _sources, _key, _digest = _seed_verified_context_projection(tmp_path)
    request.addfinalizer(research_scan.rebuild_scan.cache_clear)
    original_project = research_scan._project_research_scan_for_simulation_context

    def replace_after_projection(current):
        projection = original_project(current)
        replacement = dict(packet, generatedAt="replacement after projection")
        artifact.write_text(json.dumps(replacement), encoding="utf-8")
        return projection

    monkeypatch.setattr(research_scan, "_project_research_scan_for_simulation_context",
                        replace_after_projection)
    with pytest.raises(ResearchCandidateError, match="artifact changed during verification"):
        research_scan.load_verified_research_scan_context_projection(artifact)


def test_verified_scan_query_rehashes_every_request_and_skips_warm_parse_and_semantic_compare(
        tmp_path, monkeypatch, request):
    quotes, kwargs = inputs()
    packet = build_research_scan(quotes, **kwargs)
    source = tmp_path / "frozen-source.json"
    source.write_bytes(b"source-A")
    packet["provenance"] = [{"role": "fixture", "path": str(source),
                             "contentHash": file_hash(source)}]
    artifact = tmp_path / "research-scan.json"
    artifact.write_text(json.dumps(packet), encoding="utf-8")
    valid_artifact_bytes = artifact.read_bytes()

    expected = dict(packet, generatedAt="2026-10-25T00:00:00+00:00", provenance=[])
    verified_source_reads = []

    def exact_rebuild(sources):
        for role, path, digest in sources:
            verified_source_reads.append(path)
            research_scan._verify_source_hash(path, digest, role)
        return expected

    clear_cache = research_scan.rebuild_scan.cache_clear
    clear_cache()
    request.addfinalizer(clear_cache)
    monkeypatch.setattr(research_scan, "rebuild_scan", exact_rebuild)

    artifact_hash_reads = []
    original_hash = research_scan._hash_resolved_file

    def counted_hash(path):
        if Path(path).resolve() == artifact.resolve():
            artifact_hash_reads.append(str(path))
        return original_hash(path)

    monkeypatch.setattr(research_scan, "_hash_resolved_file", counted_hash)
    original_loads = json.loads
    decode_count = 0

    def counted_loads(value, *args, **kwargs):
        nonlocal decode_count
        decode_count += 1
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(research_scan.json, "loads", counted_loads)

    first = research_scan.load_verified_research_scan(artifact, limit=1)
    assert first["returnedCount"] == 1
    first["candidates"][0]["basis"].append("caller mutation")
    first["provenance"][0]["contentHash"] = "tampered"
    first["source"]["marketCapAsOf"] = "1900-01-01"
    first["limitations"].append("caller mutation")

    original_validate = research_scan._validated_scan_sources
    monkeypatch.setattr(research_scan, "_validated_scan_sources",
                        lambda packet: pytest.fail("warm verified packet was semantically compared again"))
    second = research_scan.load_verified_research_scan(artifact, limit=1)
    assert "caller mutation" not in second["candidates"][0]["basis"]
    assert second["provenance"][0]["contentHash"] == file_hash(source)
    assert second["source"]["marketCapAsOf"] != "1900-01-01"
    assert "caller mutation" not in second["limitations"]
    assert decode_count == 1
    assert len(artifact_hash_reads) == 2
    assert len(verified_source_reads) == 2

    full, full_digest = research_scan.load_verified_research_scan_packet(artifact)
    assert full["candidates"] == expected["candidates"]
    assert full_digest == "sha256:" + hashlib.sha256(valid_artifact_bytes).hexdigest()
    assert decode_count == 2  # Full callers receive a private decode of verified bytes.
    assert len(artifact_hash_reads) == 4  # Full packet reads are hash-clamped around source checks.
    assert len(verified_source_reads) == 3
    full["candidates"][0]["basis"].append("simulation caller mutation")
    isolated = research_scan.load_verified_research_scan(artifact, limit=1)
    assert "simulation caller mutation" not in isolated["candidates"][0]["basis"]
    assert decode_count == 2
    assert len(artifact_hash_reads) == 5
    assert len(verified_source_reads) == 4

    monkeypatch.setattr(research_scan, "_validated_scan_sources", original_validate)
    tampered = dict(packet)
    tampered["candidates"] = [dict(row) for row in packet["candidates"]]
    tampered["candidates"][0]["basis"] = ["invented explanation"]
    artifact.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ResearchCandidateError, match="explanations differ"):
        research_scan.load_verified_research_scan(artifact, limit=1)
    artifact.write_bytes(valid_artifact_bytes)
    assert decode_count == 3
    assert len(artifact_hash_reads) == 6
    assert len(verified_source_reads) == 5

    # Restore the timestamp after changing bytes: the warm path must still hash
    # the full source, rather than treating file timestamps as an integrity proof.
    source_stat = source.stat()
    source.write_bytes(b"source-B")
    os.utime(source, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
    with pytest.raises(ResearchCandidateError, match="input has changed: fixture"):
        research_scan.load_verified_research_scan(artifact, limit=1)
    assert decode_count == 3
    assert len(artifact_hash_reads) == 7
    assert len(verified_source_reads) == 6
    research_scan.load_verified_research_scan.cache_clear()
    assert not research_scan._SCAN_CACHE
    assert not research_scan._VERIFIED_SCAN_CACHE


@pytest.mark.parametrize("phase", ["source_check", "private_decode"])
def test_verified_full_packet_rejects_artifact_replacement_during_read(tmp_path, monkeypatch, phase):
    quotes, kwargs = inputs()
    packet = build_research_scan(quotes, **kwargs)
    source = tmp_path / "frozen-source.json"
    source.write_bytes(b"source-A")
    packet["provenance"] = [{"role": "fixture", "path": str(source),
                             "contentHash": file_hash(source)}]
    artifact = tmp_path / "research-scan.json"
    artifact.write_text(json.dumps(packet), encoding="utf-8")
    original_bytes = artifact.read_bytes()
    expected = dict(packet, generatedAt="2026-10-25T00:00:00+00:00", provenance=[])
    original_rebuild = research_scan.rebuild_scan

    def replace_during_source_check(sources):
        for role, path, digest in sources:
            research_scan._verify_source_hash(path, digest, role)
        if phase == "source_check":
            artifact.write_text(json.dumps(dict(packet, generatedAt="replacement")), encoding="utf-8")
        return expected

    artifact_hash_reads = []
    original_hash = research_scan._hash_resolved_file

    def counted_hash(path):
        if Path(path).resolve() == artifact.resolve():
            artifact_hash_reads.append(str(path))
        return original_hash(path)

    research_scan.rebuild_scan.cache_clear()
    monkeypatch.setattr(research_scan, "rebuild_scan", replace_during_source_check)
    if phase == "private_decode":
        research_scan.load_verified_research_scan(artifact)
        original_loads = json.loads

        def replace_during_decode(value, *args, **kwargs):
            result = original_loads(value, *args, **kwargs)
            artifact.write_text(json.dumps(dict(packet, generatedAt="replacement")), encoding="utf-8")
            return result

        monkeypatch.setattr(research_scan.json, "loads", replace_during_decode)
    monkeypatch.setattr(research_scan, "_hash_resolved_file", counted_hash)
    with pytest.raises(ResearchCandidateError, match="artifact changed during verification"):
        research_scan.load_verified_research_scan_packet(artifact)
    assert len(artifact_hash_reads) == 2
    assert artifact.read_bytes() != original_bytes
    # The alternate test implementation leaves the production pure rebuild untouched.
    monkeypatch.setattr(research_scan, "rebuild_scan", original_rebuild)
    research_scan.rebuild_scan.cache_clear()


def test_verified_scan_query_rejects_legacy_schema_without_treating_bad_v2_as_legacy(tmp_path):
    artifact = tmp_path / "research-candidates.json"
    artifact.write_text(json.dumps({"schema": "aquant.research-candidates.v1"}), encoding="utf-8")
    research_scan.rebuild_scan.cache_clear()
    with pytest.raises(research_scan.ResearchScanSchemaMismatch):
        research_scan.load_verified_research_scan(artifact)

    artifact.write_text(json.dumps({"schema": research_scan.SCHEMA,
                                    "researchOnly": True, "simulationAvailable": False}), encoding="utf-8")
    with pytest.raises((ResearchCandidateError, KeyError, TypeError)) as exc:
        research_scan.load_verified_research_scan(artifact)
    assert not isinstance(exc.value, research_scan.ResearchScanSchemaMismatch)


def test_verified_query_does_not_replace_source_derived_rebuild_packet_with_artifact_metadata(tmp_path, monkeypatch):
    quotes, kwargs = inputs()
    packet = build_research_scan(quotes, **kwargs)
    source = tmp_path / "frozen-source.json"
    source.write_bytes(b"source")
    packet["provenance"] = [{"role": "fixture", "path": str(source),
                             "contentHash": file_hash(source)}]
    artifact = tmp_path / "research-scan.json"
    artifact.write_text(json.dumps(packet), encoding="utf-8")

    sources = tuple(sorted((item["role"], item["path"], item["contentHash"])
                           for item in packet["provenance"]))
    expected = dict(packet, generatedAt="source-derived-time", provenance=[])
    research_scan.rebuild_scan.cache_clear()
    research_scan._SCAN_CACHE[sources] = expected
    monkeypatch.setattr(research_scan, "rebuild_scan", lambda current: research_scan._SCAN_CACHE[current])

    research_scan.load_verified_research_scan(artifact, limit=1)
    assert research_scan._SCAN_CACHE[sources] is expected

    alternate = dict(packet, generatedAt="another-artifact-time",
                     extraMetadata={"producerNote": "allowed nonsemantic extension"})
    artifact.write_text(json.dumps(alternate), encoding="utf-8")
    result = research_scan.load_verified_research_scan(artifact, limit=1)
    assert result["extraMetadata"] == alternate["extraMetadata"]
    assert research_scan._SCAN_CACHE[sources] is expected

    # The legacy loader still compares against the pure derived packet and
    # accepts a packet that omits the newer artifact's optional extension.
    old_result = research_scan.load_research_scan(artifact, packet=packet, limit=1)
    assert "extraMetadata" not in old_result
    assert research_scan._SCAN_CACHE[sources] is expected
    research_scan.load_verified_research_scan.cache_clear()


def _scan_source_bindings(tmp_path, facts_db):
    bodies = {
        "quotes": (b'{"rows": []}', ".json"),
        "bound_records": (b'{"records": []}', ".json"),
        "industry_snapshot": (b'{"records": []}', ".json"),
        "price_history": (b'{"bars": {}}', ".json"),
        "company_names": (b'{"stockList": []}', ".json"),
    }
    sources = [("facts_db", str(facts_db), file_hash(facts_db))]
    for role, (body, suffix) in bodies.items():
        path = tmp_path / (role + suffix)
        path.write_bytes(body)
        sources.append((role, str(path), file_hash(path)))
    return tuple(sorted(sources))


@pytest.mark.parametrize("warm_cache", [False, True])
def test_scan_rejects_nonempty_facts_wal_even_when_main_hash_is_unchanged(
        tmp_path, monkeypatch, warm_cache):
    from aquant.domain.fundamentals.fact_repository import FinancialFactRepository

    facts_db = tmp_path / "facts.sqlite"
    FinancialFactRepository(facts_db)
    sources = _scan_source_bindings(tmp_path, facts_db)
    expected_hash = dict((role, digest) for role, _path, digest in sources)["facts_db"]
    research_scan.rebuild_scan.cache_clear()
    monkeypatch.setattr(research_scan, "_build_research_scan",
                        lambda sources, paths, inputs: {"cached": True})
    artifact_packet = {"schema": research_scan.SCHEMA, "researchOnly": True,
        "simulationAvailable": False, "candidates": [], "provenance": [
            {"role": role, "path": path, "contentHash": digest}
            for role, path, digest in sources]}
    artifact = tmp_path / "research-scan.json"
    artifact.write_text(json.dumps(artifact_packet), encoding="utf-8")
    def validate_with_real_source_gate(packet):
        current = tuple(sorted((item["role"], item["path"], item["contentHash"])
                               for item in packet["provenance"]))
        research_scan.rebuild_scan(current)
        return current, packet
    monkeypatch.setattr(research_scan, "_validated_scan_sources", validate_with_real_source_gate)
    try:
        if warm_cache:
            assert research_scan.rebuild_scan(sources) == {"cached": True}

        writer = sqlite3.connect(str(facts_db))
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE wal_probe (value TEXT)")
            writer.execute("INSERT INTO wal_probe VALUES ('not in the hashed main file')")
            writer.commit()
            wal_path = Path(str(facts_db) + "-wal")
            assert wal_path.stat().st_size > 0
            assert file_hash(facts_db) == expected_hash
            with pytest.raises(ResearchCandidateError, match="nonempty SQLite WAL"):
                research_scan.load_verified_research_scan_packet(artifact)
            with pytest.raises(ResearchCandidateError, match="nonempty SQLite WAL"):
                research_scan.load_verified_research_scan_context_projection(artifact)
            assert file_hash(facts_db) == expected_hash
        finally:
            writer.close()
    finally:
        research_scan.rebuild_scan.cache_clear()


def test_scan_queries_the_verified_facts_bytes_after_source_changes(tmp_path, monkeypatch):
    from aquant.domain.fundamentals.fact_repository import FinancialFactRepository

    facts_db = tmp_path / "facts.sqlite"
    FinancialFactRepository(facts_db)
    sources = _scan_source_bindings(tmp_path, facts_db)
    expected_hash = dict((role, digest) for role, _path, digest in sources)["facts_db"]
    original_snapshot = research_scan._snapshot_verified_facts_db
    original_open_existing = FinancialFactRepository.open_existing
    opened_paths = []

    def snapshot_then_change_source(path, content_hash, destination):
        snapshot = original_snapshot(path, content_hash, destination)
        facts_db.write_bytes(b"source changed after the verified copy was made")
        return snapshot

    def open_verified_copy(path):
        snapshot = Path(path)
        assert snapshot.resolve() != facts_db.resolve()
        assert file_hash(snapshot) == expected_hash
        opened_paths.append(snapshot)
        return original_open_existing(snapshot)

    research_scan.rebuild_scan.cache_clear()
    monkeypatch.setattr(research_scan, "_snapshot_verified_facts_db",
                        snapshot_then_change_source)
    monkeypatch.setattr(FinancialFactRepository, "open_existing",
                        staticmethod(open_verified_copy))
    monkeypatch.setattr(research_scan, "build_research_scan",
                        lambda *args, **kwargs: {
                            "asOfTime": "2026-10-25T00:00:00+00:00",
                            "quoteMode": "CLOSING_RESEARCH",
                        })
    try:
        packet = research_scan.rebuild_scan(sources)
        assert packet["asOfTime"] == "2026-10-25T00:00:00+00:00"
        assert len(opened_paths) == 1
        assert not opened_paths[0].exists()  # The verified snapshot is removed after rebuilding.
    finally:
        research_scan.rebuild_scan.cache_clear()


def _with_alternate_quote_source(sources, tmp_path):
    alternate = tmp_path / "quotes-alternate.json"
    original = next(path for role, path, _digest in sources if role == "quotes")
    alternate.write_bytes(Path(original).read_bytes())
    return tuple(sorted((role, str(alternate), file_hash(alternate)) if role == "quotes"
                        else (role, path, digest) for role, path, digest in sources))


def _drop_empty_proof_supplement_table(facts_db):
    writer = sqlite3.connect(str(facts_db))
    try:
        writer.execute("DROP TABLE financial_fact_proof_supplement")
        writer.commit()
    finally:
        writer.close()


def _rebind_scan_source_hash(sources, role):
    return tuple(sorted((name, path, file_hash(path) if name == role else digest)
                        for name, path, digest in sources))


def _prepared_scan_publication_fixture(tmp_path, monkeypatch, request):
    from aquant.domain.fundamentals.fact_repository import FinancialFactRepository

    facts_db = tmp_path / "facts.sqlite"
    FinancialFactRepository(facts_db)
    _drop_empty_proof_supplement_table(facts_db)
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    active_sources = _scan_source_bindings(source_dir, facts_db)
    active_quote = Path(next(path for role, path, _digest in active_sources if role == "quotes"))
    active_quote.write_text(json.dumps({"rows": [{"code": "600001"}]}), encoding="utf-8")
    active_sources = _rebind_scan_source_hash(active_sources, "quotes")

    staged_sources = _with_alternate_quote_source(active_sources, tmp_path)
    staged_quote = Path(next(path for role, path, _digest in staged_sources if role == "quotes"))
    staged_quote.write_text(json.dumps({"rows": [{"code": "600002"}]}), encoding="utf-8")
    staged_sources = _rebind_scan_source_hash(staged_sources, "quotes")

    def build_fixture_packet(quotes, **_kwargs):
        code = quotes["rows"][0]["code"]
        return {
            "schema": research_scan.SCHEMA,
            "researchOnly": True,
            "simulationAvailable": False,
            "generatedAt": "2026-10-25T00:00:00+00:00",
            "asOfTime": "2026-10-25T00:00:00+00:00",
            "capturedTo": "2026-10-25T00:00:00+00:00",
            "dataAsOf": "2026-10-24",
            "quoteMode": "CLOSING_RESEARCH",
            "candidates": [{"code": code, "instrumentId": "SH." + code,
                "displayName": "fixture " + code, "status": "RANKED",
                "board": "MAIN", "industry": "fixture"}],
        }

    research_scan.rebuild_scan.cache_clear()
    request.addfinalizer(research_scan.rebuild_scan.cache_clear)
    monkeypatch.setattr(research_scan, "build_research_scan", build_fixture_packet)

    def verified_staged_scan(path, expected_hash, metadata_db, version):
        packet = json.loads(Path(path).read_text(encoding="utf-8"))
        sources = research_scan._scan_sources(packet)
        facts_hash = next(digest for role, _source_path, digest in sources if role == "facts_db")
        receipt = {
            "artifactHash": expected_hash,
            "sourcesHash": research_scan.provenance_hash(sources),
            "factsHash": facts_hash,
            "version": version,
        }
        return receipt, "verified"

    monkeypatch.setattr(research_scan, "verify_staged_scan", verified_staged_scan)

    published = tmp_path / "published-research-scan.json"
    active_expected = research_scan.rebuild_scan(active_sources)
    active_packet = dict(active_expected, provenance=[
        {"role": role, "path": path, "contentHash": digest}
        for role, path, digest in active_sources])
    published.write_text(json.dumps(active_packet), encoding="utf-8")
    assert research_scan.load_verified_research_scan(published)["candidates"][0]["code"] == "600001"

    with TemporaryDirectory(prefix="staged-scan-fixture-") as snapshot_dir:
        paths, decoded = research_scan._read_verified_scan_inputs(
            staged_sources, facts_snapshot_dir=snapshot_dir)
        staged_expected = research_scan._build_research_scan(staged_sources, paths, decoded)

    attempt = tmp_path / "attempt"
    attempt.mkdir()
    staged_path = attempt / "research-scan.json"
    staged_packet = dict(staged_expected, generatedAt="2026-10-25T00:01:00+00:00",
        provenance=[{"role": role, "path": path, "contentHash": digest}
                    for role, path, digest in staged_sources])
    staged_body = json.dumps(staged_packet).encode("utf-8")
    staged_path.write_bytes(staged_body)
    staged_hash = "sha256:" + hashlib.sha256(staged_body).hexdigest()
    (attempt / "publication-commit.json").write_text(json.dumps({
        "state": "COMMITTED", "snapshotId": staged_packet["snapshotId"],
        "researchHash": staged_hash,
    }), encoding="utf-8")
    metadata_db = tmp_path / "meta.sqlite"
    return published, staged_path, staged_body, staged_hash, staged_quote, active_sources, metadata_db


def test_prepared_scan_stays_private_until_exact_bytes_reach_published_alias(
        tmp_path, monkeypatch, request):
    published, staged_path, staged_body, staged_hash, _staged_quote, active_sources, metadata_db = \
        _prepared_scan_publication_fixture(tmp_path, monkeypatch, request)
    active_entry = research_scan._SCAN_CACHE[active_sources]

    assert research_scan.prepare_research_scan(
        staged_path, published_path=published, expected_hash=staged_hash, metadata_db=metadata_db)
    assert research_scan._SCAN_CACHE[active_sources] is active_entry
    assert research_scan.load_verified_research_scan(published)["candidates"][0]["code"] == "600001"

    monkeypatch.setattr(research_scan, "_validated_scan_sources",
                        lambda packet: pytest.fail("committed prepared scan used cold semantic fallback"))
    published.write_bytes(staged_body)
    adopted = research_scan.load_verified_research_scan(published)
    assert adopted["candidates"][0]["code"] == "600002"
    assert adopted["generatedAt"] == "2026-10-25T00:01:00+00:00"
    assert research_scan._PREPARED_SCAN is None


@pytest.mark.parametrize("journal_state", [None, "PREPARED"])
def test_prepared_scan_without_committed_journal_uses_full_verifier_fallback(
        tmp_path, monkeypatch, request, journal_state):
    published, staged_path, staged_body, staged_hash, _staged_quote, _active_sources, metadata_db = \
        _prepared_scan_publication_fixture(tmp_path, monkeypatch, request)
    assert research_scan.prepare_research_scan(
        staged_path, published_path=published, expected_hash=staged_hash, metadata_db=metadata_db)

    journal = staged_path.parent / "publication-commit.json"
    if journal_state is None:
        journal.unlink()
    else:
        commit = json.loads(journal.read_text(encoding="utf-8"))
        commit["state"] = journal_state
        journal.write_text(json.dumps(commit), encoding="utf-8")
    published.write_bytes(staged_body)

    semantic_fallbacks = []
    original_validate = research_scan._validated_scan_sources

    def record_semantic_fallback(packet):
        semantic_fallbacks.append(packet)
        return original_validate(packet)

    monkeypatch.setattr(research_scan, "_validated_scan_sources", record_semantic_fallback)
    adopted = research_scan.load_verified_research_scan(published)
    assert adopted["candidates"][0]["code"] == "600002"
    assert len(semantic_fallbacks) == 1


def test_prepared_scan_rechecks_sources_when_exact_bytes_are_adopted(
        tmp_path, monkeypatch, request):
    published, staged_path, staged_body, staged_hash, staged_quote, _active_sources, metadata_db = \
        _prepared_scan_publication_fixture(tmp_path, monkeypatch, request)
    assert research_scan.prepare_research_scan(
        staged_path, published_path=published, expected_hash=staged_hash, metadata_db=metadata_db)

    published.write_bytes(staged_body)
    assert research_scan.load_verified_research_scan(published)["candidates"][0]["code"] == "600002"
    monkeypatch.setattr(research_scan, "rebuild_scan",
                        lambda _sources: pytest.fail("receipt-derived warm read called rebuild_scan"))

    original_stat = staged_quote.stat()
    original_quote = staged_quote.read_bytes()
    staged_quote.write_text(json.dumps({"rows": [{"code": "600003"}]}), encoding="utf-8")
    os.utime(staged_quote, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    with pytest.raises(ResearchCandidateError, match="input has changed: quotes"):
        research_scan.load_verified_research_scan(published)

    staged_quote.write_bytes(original_quote)
    os.utime(staged_quote, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    facts_db = Path(next(path for role, path, _digest in
                         research_scan._scan_sources(json.loads(staged_body)) if role == "facts_db"))
    expected_facts_hash = next(digest for role, _path, digest in
                               research_scan._scan_sources(json.loads(staged_body)) if role == "facts_db")
    writer = sqlite3.connect(str(facts_db))
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE adopted_wal_probe (value TEXT)")
        writer.execute("INSERT INTO adopted_wal_probe VALUES ('not in the hashed main file')")
        writer.commit()
        assert Path(str(facts_db) + "-wal").stat().st_size > 0
        assert file_hash(facts_db) == expected_facts_hash
        with pytest.raises(ResearchCandidateError, match="nonempty SQLite WAL"):
            research_scan.load_verified_research_scan(published)
    finally:
        writer.close()


@pytest.mark.parametrize(("outcome", "expected"), [
    ("candidate rejected", False),
    ("timeout", False),
    ("verifier_failed", False),
    ("financial_busy", None),
    ("insufficient_memory", None),
])
def test_staged_scan_guard_denial_leaves_no_pending_receipt(
        tmp_path, monkeypatch, request, outcome, expected):
    published, staged_path, _staged_body, staged_hash, _staged_quote, _active_sources, metadata_db = \
        _prepared_scan_publication_fixture(tmp_path, monkeypatch, request)
    monkeypatch.setattr(research_scan, "verify_staged_scan",
                        lambda path, expected_hash, metadata_db, version: (None, outcome))

    assert research_scan.prepare_research_scan(
        staged_path, published_path=published, expected_hash=staged_hash,
        metadata_db=metadata_db) is expected
    assert research_scan._PREPARED_SCAN is None


def _count_scan_fact_loads(monkeypatch):
    from aquant.domain.fundamentals.fact_repository import FinancialFactRepository

    calls = []
    original_load = FinancialFactRepository.load_with_accepted_s2_pdf_reviews

    def counted_load(repository):
        calls.append(repository.db_path)
        return original_load(repository)

    monkeypatch.setattr(FinancialFactRepository, "load_with_accepted_s2_pdf_reviews", counted_load)
    monkeypatch.setattr(research_scan, "build_research_scan",
                        lambda *args, **kwargs: {
                            "asOfTime": "2026-10-25T00:00:00+00:00",
                            "quoteMode": "CLOSING_RESEARCH",
                        })
    return calls


def test_scan_fact_cache_reuses_only_matching_verified_facts_db_hash(tmp_path, monkeypatch, request):
    from aquant.domain.fundamentals.fact_repository import FinancialFactRepository

    facts_db = tmp_path / "facts.sqlite"
    FinancialFactRepository(facts_db)
    _drop_empty_proof_supplement_table(facts_db)
    sources = _scan_source_bindings(tmp_path, facts_db)
    alternate_sources = _with_alternate_quote_source(sources, tmp_path)
    assert alternate_sources != sources

    calls = _count_scan_fact_loads(monkeypatch)
    research_scan.rebuild_scan.cache_clear()
    clear_cache = research_scan.rebuild_scan.cache_clear
    request.addfinalizer(clear_cache)

    first = research_scan.rebuild_scan(sources)
    second = research_scan.rebuild_scan(alternate_sources)
    assert first["asOfTime"] == second["asOfTime"]
    assert len(calls) == 1  # Different source tuples share the same verified facts bytes.

    # A distinct facts database hash invalidates the one-entry prepared-facts cache.
    changed_db = tmp_path / "facts-with-extra-table.sqlite"
    FinancialFactRepository(changed_db)
    _drop_empty_proof_supplement_table(changed_db)
    writer = sqlite3.connect(str(changed_db))
    try:
        writer.execute("CREATE TABLE cache_miss_probe (value TEXT)")
        writer.commit()
    finally:
        writer.close()
    changed_sources = _scan_source_bindings(tmp_path, changed_db)
    assert dict((role, digest) for role, _path, digest in changed_sources)["facts_db"] != \
        dict((role, digest) for role, _path, digest in sources)["facts_db"]
    research_scan.rebuild_scan(changed_sources)
    assert len(calls) == 2

    clear_cache()
    assert research_scan._SCAN_FACTS_CACHE is None


def test_scan_fact_cache_never_skips_reverification_of_a_new_source_tuple(tmp_path, monkeypatch, request):
    from aquant.domain.fundamentals.fact_repository import FinancialFactRepository

    facts_db = tmp_path / "facts.sqlite"
    FinancialFactRepository(facts_db)
    _drop_empty_proof_supplement_table(facts_db)
    sources = _scan_source_bindings(tmp_path, facts_db)
    alternate_sources = _with_alternate_quote_source(sources, tmp_path)
    original_hash = dict((role, digest) for role, _path, digest in sources)["facts_db"]
    calls = _count_scan_fact_loads(monkeypatch)
    research_scan.rebuild_scan.cache_clear()
    request.addfinalizer(research_scan.rebuild_scan.cache_clear)

    research_scan.rebuild_scan(sources)
    assert len(calls) == 1
    facts_db.write_bytes(b"tampered after the verified rebuild")
    with pytest.raises(ResearchCandidateError, match="frozen research input has changed: facts_db"):
        research_scan.rebuild_scan(alternate_sources)
    assert len(calls) == 1  # Hash verification rejects before the prepared facts cache is consulted.
    assert original_hash != file_hash(facts_db)


def test_scan_fact_cache_bypasses_databases_with_review_proof_rows(tmp_path, monkeypatch, request):
    from aquant.domain.fundamentals.fact_repository import FinancialFactRepository

    facts_db = tmp_path / "facts.sqlite"
    FinancialFactRepository(facts_db)
    writer = sqlite3.connect(str(facts_db))
    try:
        writer.execute(
            "INSERT INTO financial_fact_review (bundle_id, evidence_hash, evidence_json) "
            "VALUES (?, ?, ?)",
            ("invalid-proof-fixture", "sha256:" + "0" * 64, "{}"),
        )
        writer.commit()
    finally:
        writer.close()
    _drop_empty_proof_supplement_table(facts_db)
    sources = _scan_source_bindings(tmp_path, facts_db)
    alternate_sources = _with_alternate_quote_source(sources, tmp_path)
    calls = _count_scan_fact_loads(monkeypatch)
    research_scan.rebuild_scan.cache_clear()
    request.addfinalizer(research_scan.rebuild_scan.cache_clear)

    research_scan.rebuild_scan(sources)
    research_scan.rebuild_scan(alternate_sources)
    assert len(calls) == 2
    assert research_scan._SCAN_FACTS_CACHE is None


def test_real_event_excerpt_is_separate_from_hypothesis_and_unsearched_is_not_no_events():
    quotes, kwargs = inputs()
    text = "本次权益分派方案：每10股派发现金1元；具体股权登记日请查阅完整公告。"
    kwargs["announcements"] = {"captured_from": "2026-10-09T04:01:00+00:00", "captured_to": "2026-10-09T04:03:00+00:00",
        "window": {"start": "2026-09-09", "end": "2026-10-09"},
        "per_code": [{"code": "600001", "queryComplete": True, "pdfFailures": []}],
        "documents": [{"code": "600001", "announcementId": "eventdoc", "title": "权益分派实施公告",
            "publishedDate": "2026-09-22", "url": "https://static.cninfo.com.cn/event.PDF", "extractedText": text,
            "pdfReceiptId": "pdfr", "pdfContentHash": "sha256:" + "c" * 64,
            "pages": [{"page": 1, "start_char": 0, "end_char": len(text)}]}]}
    packet = build_research_scan(quotes, **kwargs)
    one = next(r for r in packet["candidates"] if r["code"] == "600001")
    assert one["eventContext"]["queryComplete"] and one["events"][0]["page"] == 1
    ev = one["events"][0]
    assert ev["quote"] == text[ev["startChar"]:ev["endChar"]]
    assert "不能" in one["impactPaths"][-1]["hypothesis"]
    two = next(r for r in packet["candidates"] if r["code"] == "600002")
    assert not two["eventContext"]["queryComplete"] and "不表示无公告" in two["eventContext"]["missing"]
    assert packet["asOfTime"] == "2026-10-09T04:03:00+00:00"
    assert packet["quoteCapturedTo"] == "2026-10-09T03:46:00+00:00"
    quotes.update(captured_from="2026-10-09T09:11:00+00:00", captured_to="2026-10-09T09:14:00+00:00")
    closing_capture = build_research_scan(quotes, **kwargs)
    assert closing_capture["capturedFrom"] == "2026-10-09T04:01:00+00:00"
    assert closing_capture["quoteCapturedFrom"] == "2026-10-09T09:11:00+00:00"
    assert closing_capture["capturedTo"] == closing_capture["asOfTime"] == "2026-10-09T09:14:00+00:00"


def test_event_quote_locator_never_labels_cross_page_text_as_one_page():
    from aquant.operations.research_scan import _event_context
    text = "每10股派发现金1元。" + "第2页内容" * 20
    boundary = 15
    announcements = {"window": {"start": "2026-09-09", "end": "2026-10-09"}, "captured_to": "2026-10-09T04:03:00+00:00",
        "per_code": [{"code": "600001", "queryComplete": True}], "documents": [{"code": "600001",
            "announcementId": "split", "title": "权益分派", "publishedDate": "2026-09-22", "url": "https://static.cninfo.com.cn/f.PDF",
            "extractedText": text, "pdfReceiptId": "r", "pdfContentHash": "sha256:" + "c" * 64,
            "pages": [{"page": 1, "start_char": 0, "end_char": boundary},
                      {"page": 2, "start_char": boundary, "end_char": len(text)}]}]}
    event = _event_context("600001", announcements)[1][0]
    assert event["page"] == 1 and event["endChar"] == boundary
    assert event["quote"] == text[:boundary]


def test_closing_research_rejects_early_quotes_and_uses_completed_day_context():
    quotes, kwargs = inputs()
    quotes.update(research_quote_mode="CLOSING_RESEARCH", captured_to="2026-10-09T09:15:00+00:00")
    quotes["rows"][0]["quote_time_raw"] = "20261009161500"
    kwargs["history"] = {"bars": {"SH.600001": {"rows": [
        {"trading_day": "2026-10-08", "close_cents": 1000, "amount_cents": 500},
        {"trading_day": "2026-10-09", "close_cents": 1100, "amount_cents": 600},
        {"trading_day": "2026-10-12", "close_cents": 9999, "amount_cents": 9999}]}}}
    packet = build_research_scan(quotes, **kwargs)
    cards = {r["code"]: r for r in packet["candidates"]}
    assert packet["quoteMode"] == "CLOSING_RESEARCH" and not packet["simulationAvailable"]
    assert packet["rankedCount"] == 1
    assert cards["600002"]["exclusionCode"] == "QUOTE_BEFORE_CLOSE"
    context = cards["600001"]["marketContext"]
    assert context["historyAsOf"] == context["expectedAsOf"] == "2026-10-09"
    assert context["tradingDayCount"] == 2
    assert context["freshness"] == "已覆盖本次研究日"
    assert packet["historyCoverage"]["rankedCurrentCount"] == 1
    # During trading, a provider's provisional same-day bar is not a full-day bar.
    quotes["research_quote_mode"] = "INTRADAY_RESEARCH"
    intraday = build_research_scan(quotes, **kwargs)
    old = next(r for r in intraday["candidates"] if r["code"] == "600001")
    assert old["marketContext"]["historyAsOf"] == "2026-10-08"


def test_history_refresh_replaces_selected_windows_preserves_others_and_does_not_fill_missing_amount():
    from aquant.operations.research_scan import _history
    base = {"bars": {"SH.600001": {"rows": [{"trading_day": "2026-09-01"}]},
                      "SH.600002": {"rows": [{"trading_day": "2026-09-14"}]}}}
    dates = [f"2026-09-{day:02d}" for day in range(1, 21)]
    refresh = {"bars": {"SH.600001": {"source": "tencent-ifzq", "receipt_id": "raw-r",
        "content_hash": "hash", "observed_at": "2026-10-09T09:21:00+00:00",
        "rows": [{"trading_day": d, "close_cents": 1000, "amount_cents": None} for d in dates]}},
        "captured_from": "2026-10-09T09:20:00+00:00", "captured_to": "2026-10-09T09:21:00+00:00",
        "requested_window": {"end": "2026-10-09"}}
    merged = _refresh_history(base, refresh)
    assert merged["bars"]["SH.600002"] == base["bars"]["SH.600002"]
    assert len(merged["bars"]["SH.600001"]["rows"]) == 20 and merged["refreshed_count"] == 1
    context = _history(merged, "SH.600001", datetime(2026, 10, 9, 9, tzinfo=timezone.utc),
                       include_cutoff_day=True, expected_as_of=merged["expected_as_of"])
    assert context["historyAsOf"] == "2026-09-20" and context["freshness"] == "截至较早交易日"
    assert context["metrics"] == [] and context["source"]["receipt_id"] == "raw-r"
    assert any("未填零" in text for text in context["limitations"])


def test_current_industry_preserves_provider_classification_date_and_latest_input_cutoff():
    quotes, kwargs = inputs()
    records = kwargs["industries"]["records"]
    kwargs["industries"] = {"schema": "aquant.baostock_research_industry.v1",
        "captured_to": "2026-10-09T09:14:39+00:00", "rows": [
            {"baostock_code_raw": "sh." + r["code"], "industry_raw": r["industry"],
             "classification_asof_raw": "2026-10-05"} for r in records]}
    kwargs["history"].update(captured_to="2026-10-09T09:30:00+00:00")
    packet = build_research_scan(quotes, **kwargs)
    assert packet["asOfTime"] == "2026-10-09T09:30:00+00:00"
    assert all(r["industryAsOf"] == "2026-10-05" for r in packet["candidates"])
    assert packet["scope"]["financialCount"] == 1


def test_incremental_day_has_its_own_source_proof_and_intraday_does_not_show_unused_segment():
    from aquant.operations.research_scan import _history
    receipt = {"source_id": "baostock-socket", "receipt_id": "today-r", "actual_hash": "today-h",
               "observed_at": "2026-10-09T09:44:00+00:00", "source_url": "baostock://today"}
    history = {"bars": {"SH.600001": {"source": "baostock-socket", "receipt_id": "history-r",
        "content_hash": "history-h", "observed_at": "2026-10-09T09:16:00+00:00",
        "source_url": "baostock://history", "latest_refresh": {"receipt": receipt}, "rows": [
            {"trading_day": "2026-10-08", "close_cents": 1000},
            {"trading_day": "2026-10-09", "close_cents": 1100, "source_receipt_id": "today-r"}]}}}
    cutoff = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)
    closed = _history(history, "SH.600001", cutoff, include_cutoff_day=True)
    proofs = closed["source"]["receipts"]
    assert [p["receipt_id"] for p in proofs] == ["history-r", "today-r"]
    assert [p["window"] for p in proofs] == ["2026-10-08 至 2026-10-08", "2026-10-09 至 2026-10-09"]
    assert proofs[1]["content_hash"] == "today-h" and proofs[1]["rowCount"] == 1
    intraday = _history(history, "SH.600001", cutoff)
    assert len(intraday["source"]["receipts"]) == 1
    assert intraday["source"]["receipts"][0]["receipt_id"] == "history-r"


def supplement_fixture(code="920001", category="C"):
    label = "金融业" if category == "J" else "制造业"
    return {"schema": "aquant.capco_research_industry_supplement.v1",
        "captured_from": "2026-10-09T12:57:00+00:00", "captured_to": "2026-10-09T13:00:00+00:00",
        "taxonomy": {"classification_asof_date": None, "reference_period": "2026年上半年",
            "result_publication_date": "2026-09-30", "financial_category": {"code": "J", "label": "金融业"},
            "category_pairs_in_pdf": [{"code": category, "label": label}]},
        "sources": {"classification_pdf": {"url": "https://sp.capco.org.cn:82/file/fixture.pdf",
            "receipt_id": "capco-r", "content_hash": "sha256:" + "c" * 64, "responded_at": "2026-10-09T12:58:00+00:00"}},
        "rows": [{"official_code": code, "industry_category_code": category,
            "industry_category_label": label, "industry_subcategory_code": "CG" if category == "C" else None,
            "industry_subcategory_label": "专用、通用及交通运输设备" if category == "C" else None,
            "industry_major_code": "35" if category == "C" else "66", "industry_major_label": "fixture分类",
            "source_page": 151, "source_row_raw": "fixture原表行", "parse_status": "parsed"}]}


def test_official_supplement_adds_exact_bse_to_existing_ranking_with_separate_taxonomy():
    quotes, kwargs = inputs()
    original = [f for f in kwargs["facts"] if f.instrument_id == "600001"]
    kwargs["facts"] += [replace(f, instrument_id="920001", version_id="bj-" + f.version_id) for f in original]
    kwargs["bound"]["records"] += [dict(r, instrument_id="920001") for r in list(kwargs["bound"]["records"])
                                 if r["instrument_id"] == "600001"]
    kwargs["industry_supplement"] = supplement_fixture()
    packet = build_research_scan(quotes, **kwargs)
    card = next(r for r in packet["candidates"] if r["code"] == "920001")
    assert card["status"] == "RANKED" and card["board"] == "BSE" and card["factors"]
    classification = card["industryClassification"]
    assert card["industry"].startswith("CAPCO C / CG / 35")
    assert classification["system"] == "CAPCO 上市公司行业统计分类"
    assert classification["classificationAsOf"] is None and card["industryAsOf"] is None
    assert classification["publicationDate"] == "2026-09-30" and classification["source"]["page"] == 151
    assert packet["rankedCount"] == 3 and packet["industryCoverage"]["activeUnknownCount"] == 0
    assert packet["industryCoverage"]["supplementCount"] == 1
    assert packet["asOfTime"] == "2026-10-09T13:00:00+00:00"
    assert packet["historyCoverage"]["rankedCurrentCount"] == 0 and not packet["simulationAvailable"]


def test_capco_financial_category_never_uses_ordinary_f08_and_primary_is_not_overwritten():
    quotes, kwargs = inputs()
    kwargs["industry_supplement"] = supplement_fixture(category="J")
    card = next(r for r in build_research_scan(quotes, **kwargs)["candidates"] if r["code"] == "920001")
    assert card["status"] == "FINANCIAL" and card["signalRank"] is None and card["factors"] == {}
    assert card["financialObservation"]["status"] == "MISSING"
    assert all(v["value"] is None for v in card["financialObservation"]["values"])
    kwargs["industry_supplement"] = supplement_fixture(code="600001", category="J")
    primary = next(r for r in build_research_scan(quotes, **kwargs)["candidates"] if r["code"] == "600001")
    assert primary["status"] == "RANKED" and primary["industry"] == "C39工业"
    assert primary["industryClassification"]["selection"] == "PRIMARY"
    assert primary["industryClassification"]["alternatives"][0]["system"].startswith("CAPCO")


def test_supplement_invented_effective_date_and_duplicate_rows_are_rejected():
    quotes, kwargs = inputs()
    supplement = supplement_fixture()
    kwargs["industry_supplement"] = supplement
    supplement["taxonomy"]["classification_asof_date"] = "2026-06-30"
    with pytest.raises(ResearchCandidateError, match="invented classification date"):
        build_research_scan(quotes, **kwargs)
    supplement["taxonomy"]["classification_asof_date"] = None
    supplement["rows"].append(dict(supplement["rows"][0]))
    with pytest.raises(ResearchCandidateError, match="duplicate supplement"):
        build_research_scan(quotes, **kwargs)


def test_financial_observation_requires_same_period_group_family_and_keeps_missing_null():
    quotes, kwargs = inputs()
    for binding in kwargs["bound"]["records"]:
        binding["evidence"] = {metric: {"family": "B"} for metric in binding["values"]}
    packet = build_research_scan(quotes, **kwargs)
    card = next(r for r in packet["candidates"] if r["code"] == "600003")
    observation = card["financialObservation"]
    assert observation["status"] == "COMPLETE" and observation["reportFamily"] == "B"
    assert observation["comparisonKey"] == "证监会行业分类|66|2026-06-30|B"
    assert {r["metric"] for r in observation["values"]} == {r["metric"] for r in card["financialBridge"]}
    assert all(v["evidenceIds"] for v in observation["values"])
    latest = next(r for r in kwargs["bound"]["records"] if r["instrument_id"] == "600003" and r["period_end"] == "2026-06-30")
    latest["evidence"]["operating_cashflow"]["family"] = "I"
    mixed = next(r for r in build_research_scan(quotes, **kwargs)["candidates"] if r["code"] == "600003")
    assert mixed["financialObservation"]["comparisonKey"] is None
    kwargs["facts"] = [f for f in kwargs["facts"] if not (f.instrument_id == "600003" and f.period_end.isoformat() == "2026-06-30" and f.metric == "revenue")]
    partial = next(r for r in build_research_scan(quotes, **kwargs)["candidates"] if r["code"] == "600003")
    assert partial["financialObservation"]["status"] == "PARTIAL"
    assert partial["financialObservation"]["missingMetrics"] == ["revenue"]
    assert next(v for v in partial["financialObservation"]["values"] if v["metric"] == "revenue")["value"] is None


def test_financial_report_family_can_use_exact_legacy_report_names_but_conflicts_disable_comparison():
    quotes, kwargs = inputs()
    kinds = {"operating_cashflow": "CASHFLOW", "parent_equity": "BALANCE"}
    for binding in kwargs["bound"]["records"]:
        binding["evidence"] = {metric: {"report": "RPT_F10_FINANCE_G" + kinds.get(metric, "INCOME")}
                               for metric in binding["values"]}
    card = next(r for r in build_research_scan(quotes, **kwargs)["candidates"] if r["code"] == "600003")
    assert card["financialObservation"]["reportFamily"] == "G"
    assert card["financialObservation"]["comparisonKey"] == "证监会行业分类|66|2026-06-30|G"
    latest = next(r for r in kwargs["bound"]["records"] if r["instrument_id"] == "600003" and r["period_end"] == "2026-06-30")
    latest["evidence"]["parent_equity"]["family"] = "B"
    conflict = next(r for r in build_research_scan(quotes, **kwargs)["candidates"] if r["code"] == "600003")
    assert conflict["financialObservation"]["reportFamily"] is None
    assert conflict["financialObservation"]["comparisonKey"] is None


def disclosure_fixture():
    return {"schema": "aquant.research_industry_missing.v1",
        "captured_from": "2026-10-09T13:01:00+00:00", "captured_to": "2026-10-09T13:02:00+00:00",
        "sources": {"pdf": {"receipt_id": "company-r", "responded_at": "2026-10-09T13:02:00+00:00"}},
        "rows": [{"official_code": "920001", "industry_code_raw": "C35", "industry_label_raw": "专用设备制造业",
            "classification_system": "国民经济行业分类", "standard_id": "GB/T 4754-2017", "classification_asof_date": None,
            "published_date": "2026-09-10", "pdf_receipt_id": "company-r", "document_url": "https://static.cninfo.com.cn/fixture.PDF",
            "pdf_content_hash": "sha256:" + "d" * 64, "industry_field_page": 18,
            "evidence_excerpts": [{"field": "所属行业名称", "page": 18, "excerpt": "证券代码 920001 所属行业名称 专用设备制造业 所属行业代码 C35"}],
            "announcement_id": "company-ann", "index_receipt_id": "company-index-r", "index_content_hash": "sha256:" + "e" * 64,
            "query_complete": False}]}


def test_exact_company_disclosure_is_separate_from_capco_and_partial_index_is_not_complete():
    quotes, kwargs = inputs()
    kwargs["industry_disclosures"] = disclosure_fixture()
    scan = build_research_scan(quotes, **kwargs)
    card = next(r for r in scan["candidates"] if r["code"] == "920001")
    assert card["industry"] == "GB/T 4754-2017 C35 专用设备制造业"
    assert card["industryClassification"]["system"] == "国民经济行业分类 / 公司披露"
    assert card["industryClassification"]["classificationAsOf"] is None
    assert card["industryClassification"]["publicationDate"] == "2026-09-10"
    assert card["industryClassification"]["source"]["queryComplete"] is False
    assert card["exclusionCode"] != "INDUSTRY_UNKNOWN"
    assert card["signalRank"] is None  # Classification cannot fill missing facts.
    assert scan["industryCoverage"]["supplementCount"] == 1 and scan["industryCoverage"]["activeUnknownCount"] == 0
    assert scan["asOfTime"] == "2026-10-09T13:02:00+00:00" and not scan["simulationAvailable"]
    kwargs["industry_supplement"] = supplement_fixture()
    kept = next(r for r in build_research_scan(quotes, **kwargs)["candidates"] if r["code"] == "920001")
    assert kept["industry"].startswith("CAPCO ")
    assert kept["industryClassification"]["alternatives"][0]["system"] == "国民经济行业分类 / 公司披露"


@pytest.mark.parametrize("change", ["unknown_standard", "invented_date", "ambiguous_receipt"])
def test_unresolved_company_classification_is_not_admitted(change):
    quotes, kwargs = inputs()
    packet = disclosure_fixture()
    kwargs["industry_disclosures"] = packet
    if change == "unknown_standard": packet["rows"][0]["standard_id"] = "provider label"
    if change == "invented_date": packet["rows"][0]["classification_asof_date"] = "2026-09-10"
    if change == "ambiguous_receipt": packet["sources"]["duplicate"] = dict(packet["sources"]["pdf"])
    with pytest.raises(ResearchCandidateError):
        build_research_scan(quotes, **kwargs)
