"""Relocated research/financial evidence still requires exact original bytes."""
import json
from pathlib import Path, PureWindowsPath

import pytest

from aquant.domain.fundamentals.fact_repository import FinancialFactRepository, _replay_grid_cell
from aquant.operations.research_candidates import ResearchCandidateError, file_hash, load_research_candidates
from aquant.operations.research_scan import load_research_scan, rebuild_scan
from aquant.operations.research_simulation import load_simulation_research
from aquant.operations.simulation_financial_evidence import (
    FinancialEvidenceError, admit_simulation_financial_evidence, freeze_financial_inputs,
)
from test_artifact_paths import SOURCE, mapping
from test_simulation_evidence_admission import package as simulation_package
from test_proof_supplement import case as proof_case


def foreign(path, root):
    return str(PureWindowsPath(SOURCE).joinpath(*path.relative_to(root).parts))


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_relocated_v1_rebuild_preserves_original_source_text_and_rejects_tampering(tmp_path, monkeypatch):
    from test_research_candidates import fixture_inputs
    from aquant.operations.research_candidates import build_research_candidates
    report, bound, facts, provenance = fixture_inputs(tmp_path)
    for item in provenance:
        item["path"] = foreign(Path(item["path"]), tmp_path)
    report["source_path"] = next(p["path"] for p in provenance if p["role"] == "factor_report")
    packet = build_research_candidates(report, bound, facts=facts, names={"600519": "贵州茅台"},
                                      industries={}, provenance=provenance)
    path = tmp_path / "candidates.json"
    dump(path, packet)
    before = {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    mapping(monkeypatch, tmp_path)
    actual = load_research_candidates(foreign(path, tmp_path))
    assert actual["source"] == packet["source"]
    assert actual["provenance"] == provenance
    assert all(p.read_bytes() == body for p, body in before.items())
    (tmp_path / "names.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ResearchCandidateError, match="input has changed"):
        load_research_candidates(foreign(path, tmp_path))


def test_relocated_scan_and_simulation_context_share_full_rebuild_and_exact_origin(tmp_path, monkeypatch):
    from test_research_scan import inputs
    quotes, inputs = inputs()
    files = {"quotes": quotes, "bound_records": inputs["bound"], "industry_snapshot": inputs["industries"],
             "price_history": inputs["history"], "company_names": {"stockList": [
                 {"code": code, "zwjc": name} for code, name in inputs["names"].items()]}}
    provenance = []
    for role, body in files.items():
        path = tmp_path / (role + ".json")
        dump(path, body)
        provenance.append({"role": role, "path": foreign(path, tmp_path), "contentHash": file_hash(path)})
    facts = tmp_path / "facts.sqlite"
    FinancialFactRepository(facts).append_many(inputs["facts"])
    provenance.append({"role": "facts_db", "path": foreign(facts, tmp_path), "contentHash": file_hash(facts)})
    mapping(monkeypatch, tmp_path)
    packet = dict(rebuild_scan(tuple(sorted((p["role"], p["path"], p["contentHash"]) for p in provenance))))
    packet["provenance"] = provenance
    path = tmp_path / "scan.json"
    dump(path, packet)
    before = {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    actual = load_research_scan(foreign(path, tmp_path), view="all")
    assert actual["poolSize"] == 4 and actual["snapshotId"] == packet["snapshotId"]
    full, binding = load_simulation_research(foreign(path, tmp_path))
    assert full == packet and binding["researchContentHash"] == file_hash(path)
    assert all(p.read_bytes() == body for p, body in before.items())
    (tmp_path / "quotes.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ResearchCandidateError, match="input has changed"):
        load_simulation_research(foreign(path, tmp_path))


def test_relocated_financial_package_replays_nested_demand_cache_receipts_and_body(tmp_path, monkeypatch):
    from test_simulation_financial_evidence import END, package
    path, manifest = package(tmp_path)
    target = {"instrumentId": "SH.600323", "providerCode": "sh.600323", "periodKey": "2026Q2", "year": 2026, "quarter": 2}
    demand = tmp_path / "demand.json"
    dump(demand, {"records": [target]})
    manifest.update(targets=[target], scopeInstrumentIds=["SH.600323"],
                    demandPath=str(demand), demandHash=file_hash(demand))
    for key in ("cachePath", "baseCachePath", "archiveDbPath", "archiveRoot", "demandPath"):
        manifest[key] = foreign(Path(manifest[key]), tmp_path)
    dump(path, manifest)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    mapping(monkeypatch, tmp_path)
    result = admit_simulation_financial_evidence(foreign(path, tmp_path),
        financials_path=foreign(tmp_path / "cache.json", tmp_path), cutoff=END)
    assert result["provenance"]["verifiedReceiptCount"] == 1
    frozen = freeze_financial_inputs(result["provenance"], data_root=tmp_path / "snapshots")
    assert Path(frozen["evidence"]["path"]).read_bytes() == before[path]
    assert all(p.read_bytes() == body for p, body in before.items())
    raw = next(p for p in (tmp_path / "archive/raw").rglob("*") if p.is_file())
    raw.write_bytes(b"changed original")
    with pytest.raises(FinancialEvidenceError, match="receipt hash/size"):
        admit_simulation_financial_evidence(foreign(path, tmp_path), cutoff=END)


def test_relocated_grid_evidence_keeps_pdf_hash_and_exact_geometry_checks(tmp_path, monkeypatch):
    from test_statement_grid_verification import _archive_and_fact, _cell
    from aquant.adapters.providers import cninfo_statement_geometry as grid
    payload, fact, bundle, pdf = _archive_and_fact(tmp_path)
    for key in ("database_path", "root"):
        bundle["geometry_archive"][key] = foreign(Path(bundle["geometry_archive"][key]), tmp_path)
    mapping(monkeypatch, tmp_path)
    seen = []
    def extract(body, **kwargs):
        seen.append(body)
        return (_cell(),)
    monkeypatch.setattr(grid, "extract_grid_statement_cells", extract)
    assert _replay_grid_cell(_cell().as_evidence(), fact, bundle)
    assert seen == [payload]
    pdf.write_bytes(b"%PDF-changed-original")
    assert not _replay_grid_cell(_cell().as_evidence(), fact, bundle)
    assert seen == [payload]


def test_relocated_daily_simulation_package_keeps_frozen_source_and_receipt_validation(
        tmp_path, monkeypatch, simulation_package):
    from test_simulation_evidence_admission import CUTOFF
    from aquant.operations.simulation_evidence import admit_simulation_evidence, freeze_simulation_inputs
    paths, values = simulation_package
    source = values["summary"]["source_inputs"]["research_candidates"]
    source["path"] = foreign(tmp_path / "research.json", tmp_path)
    dump(paths["summary"], values["summary"])
    before = paths["summary"].read_bytes()
    mapping(monkeypatch, tmp_path)
    proof = admit_simulation_evidence(foreign(paths["summary"], tmp_path),
        cache_path=foreign(paths["cache"], tmp_path), pool_path=foreign(paths["pool"], tmp_path), cutoff=CUTOFF)
    assert proof["marketDay"] == "2026-10-09"
    frozen = freeze_simulation_inputs(foreign(paths["summary"], tmp_path),
        cache_path=paths["cache"], pool_path=paths["pool"], data_root=tmp_path / "snapshots")
    assert Path(frozen["simulation_summary"]["path"]).read_bytes() == before
    assert paths["summary"].read_bytes() == before
    (tmp_path / "research.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="research input changed"):
        admit_simulation_evidence(paths["summary"], cache_path=paths["cache"], pool_path=paths["pool"], cutoff=CUTOFF)


def test_relative_windows_archive_members_are_read_without_rewriting_sql(tmp_path):
    import hashlib
    import sqlite3
    from aquant.domain.data.forward_archive import ForwardArchive
    from aquant.adapters.providers.s2_revision_impact import RevisionImpactError, _raw
    body = b"original response"
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    path = tmp_path / "raw/ab/original"
    path.parent.mkdir(parents=True)
    path.write_bytes(body)
    with sqlite3.connect(":memory:") as con:
        con.row_factory = sqlite3.Row
        archive = ForwardArchive(con, tmp_path)
        con.execute("INSERT INTO raw_artifact VALUES (?,?,?,?,?)", (
            digest, len(body), "application/json", r"raw\ab\original", "2026-10-13T00:00:00+00:00"))
        assert archive.load_bytes(digest) == body and archive.verify(digest)
        assert _raw(con, tmp_path, digest) == body
        assert con.execute("SELECT stored_path FROM raw_artifact").fetchone()[0] == r"raw\ab\original"
        path.write_bytes(b"modified original")
        assert not archive.verify(digest)
        with pytest.raises(RevisionImpactError, match="SHA/byte count"):
            _raw(con, tmp_path, digest)


def test_relocated_coverage_target_map_verifies_nested_source_hash(tmp_path, monkeypatch):
    from aquant.operations.s2_readiness import S2ReadinessError, load_s2_target_period_map
    source = tmp_path / "pool.json"
    dump(source, [{"instrument_id": "SH.600000"}])
    target = tmp_path / "target.json"
    manifest = {"schema": "aquant.s2_target_periods.v1", "source_scope_ids": ["SH.600000"],
        "latest_period_ends": {"SH.600000": "2026-06-30"},
        "records": [{"instrument_id": "SH.600000", "latest_period_end": "2026-06-30"}],
        "source_artifacts": [{"role": "frozen_pool_instruments", "path": foreign(source, tmp_path),
                              "content_hash": file_hash(source)}]}
    dump(target, manifest)
    before = target.read_bytes()
    mapping(monkeypatch, tmp_path)
    actual = load_s2_target_period_map(foreign(target, tmp_path))
    assert actual["latest_period_ends"] == {"SH.600000": "2026-06-30"}
    assert target.read_bytes() == before
    dump(source, [])
    with pytest.raises(S2ReadinessError, match="hash changed"):
        load_s2_target_period_map(foreign(target, tmp_path))


@pytest.mark.parametrize("tamper", ["pdf", "calendar"])
def test_relocated_proof_supplement_replays_originals_calendar_and_availability(
        tmp_path, monkeypatch, proof_case, tamper):
    from contextlib import closing
    from aquant.domain.data.db import connect
    from aquant.domain.fundamentals import proof_supplement as supplement
    repository, args, facts = proof_case
    row = repository.append_machine_proof_supplement(**args)
    # Build a synthetic frozen Windows descriptor. No real or fixture stored
    # supplement is overwritten; validate must replay this exact JSON as-is.
    value = json.loads(row["evidence_json"])
    for key in ("database_path", "root"):
        value["archive"][key] = foreign(Path(value["archive"][key]), tmp_path)
    value["calendar_path"] = foreign(Path(value["calendar_path"]), tmp_path)
    payload = supplement.canonical(value)
    digest = supplement.digest(payload.encode())
    row = {**row, "evidence_json": payload, "evidence_hash": digest,
           "supplement_id": "cninfo_proof_supplement_" + digest[7:]}
    with closing(connect(repository.db_path, read_only=True)) as con:
        original = con.execute("SELECT * FROM financial_fact_review").fetchone()
    original_id = args["original_bundle_id"]
    inputs = ({original_id: original}, {original_id: {f.version_id for f in facts}},
              {f.version_id: f for f in facts})
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    mapping(monkeypatch, tmp_path)
    _, cohort, available = supplement.validate(row, *inputs)
    assert cohort == {f.version_id for f in facts}
    assert available.isoformat() == value["available_at"]
    assert row["evidence_json"] == payload
    assert all(p.read_bytes() == body for p, body in before.items())
    if tamper == "pdf":
        raw = next(p for p in (tmp_path / "archive/raw").rglob("*") if p.is_file())
        raw.write_bytes(b"%PDF-modified-original")
        expected = "original bytes"
    else:
        (tmp_path / "calendar.json").write_text("[]", encoding="utf-8")
        expected = "calendar bytes"
    with pytest.raises(ValueError, match=expected):
        supplement.validate(row, *inputs)
