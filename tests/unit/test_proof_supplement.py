"""Proof replacement, immutable cohorts and post-verification PIT boundaries."""
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aquant.domain.data.db import connect
from aquant.domain.data.forward_archive import ForwardArchive
from aquant.domain.fundamentals import proof_supplement as supplement
from aquant.domain.fundamentals.fact_repository import FinancialFactRepository, FactReviewBundle

spec = importlib.util.spec_from_file_location("machine_protocol_fixture",
    Path(__file__).with_name("test_machine_verified_fact_repository.py"))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
UTC = timezone.utc
NOW = datetime(2026, 10, 13, 11, tzinfo=UTC)
DAYS = (date(2026,10,13), date(2026,10,14), date(2026,10,15))


@pytest.fixture
def case(tmp_path, monkeypatch):
    archive_root = tmp_path / "archive"
    archive_db = archive_root / "meta.sqlite"
    raw = b"%PDF-1.7\nSynthetic protocol fixture; numerical readers are mocked."
    with closing(connect(archive_db)) as con:
        archive = ForwardArchive(con, archive_root)
        sha, _ = archive.store_bytes(raw)
        url = f"https://static.cninfo.com.cn/finalpage/2026-08-25/{fixture.ANNOUNCEMENT}.PDF"
        receipt = archive.record(source_id="cninfo", url=url, outcome="OK", http_status=200,
            requested_at=fixture.FIRST_SEEN, responded_at=fixture.FIRST_SEEN,
            content_hash=sha, byte_size=len(raw))
    facts = []
    for old in fixture._facts():
        identity = f"cninfo|{old.instrument_id}|{old.period_end}|{old.metric}|{old.source_document_id}|{sha}"
        facts.append(replace(old, content_hash=sha,
            version_id="mach_" + hashlib.sha256(identity.encode()).hexdigest()[:32]))
    good = fixture._evidence(facts)
    good.update(content_hash=sha, pdf_receipt_id=receipt.receipt_id, company="Synthetic issuer",
                document_url=url, sections=[], identities=[])
    bad = json.loads(supplement.canonical(good))
    bad["fields"][0]["checks"][0]["evidence"].pop("b")
    repository = FinancialFactRepository(tmp_path / "facts.sqlite")
    repository.append_many(facts, review_bundle=FactReviewBundle(fixture._bundle_id(), supplement.canonical(bad)))
    descriptor = {"database_path":str(archive_db), "root":str(archive_root),
                  "pdf_receipt_id":receipt.receipt_id, "document_url":url}
    calendar_path = tmp_path / "calendar.json"
    calendar_path.write_text("{}", encoding="utf-8")
    replay = {key:good[key] for key in ("fields","sections","identities")}
    monkeypatch.setattr(supplement, "_rederive", lambda *args: replay)
    monkeypatch.setattr(supplement, "_now", lambda: NOW)
    from aquant.adapters.providers import s2_calendar_evidence
    monkeypatch.setattr(s2_calendar_evidence, "verify_calendar_evidence", lambda *args, **kwargs: (DAYS, {}))
    args = {"original_bundle_id":fixture._bundle_id(), "replacement_evidence":good,
            "archive":descriptor, "calendar_path":calendar_path}
    return repository, args, facts


def old_rows(repository):
    with closing(connect(repository.db_path, read_only=True)) as con:
        return {table:[tuple(row) for row in con.execute(f"SELECT * FROM {table} ORDER BY 1")]
                for table in ("financial_fact", "financial_fact_review", "financial_fact_review_member")}


def test_supplement_preserves_old_objects_and_cannot_backfill(case):
    repository, args, facts = case
    before = old_rows(repository)
    assert repository.load_with_accepted_s2_pdf_reviews()[1] == {}
    row = repository.append_machine_proof_supplement(**args)
    store, accepted = repository.load_with_accepted_s2_pdf_reviews()
    assert set(accepted) == {fact.version_id for fact in facts}
    assert {fact.available_at for fact in store.facts} == {datetime(2026,10,14,0,45,tzinfo=UTC)}
    assert {fact.available_at for fact in repository.load().facts} == {fixture.AVAILABLE}
    assert old_rows(repository) == before
    assert all(link[0] == row["supplement_id"] for link in accepted.values())
    with closing(connect(repository.db_path)) as con:
        with pytest.raises(Exception, match="append-only"):
            con.execute("UPDATE financial_fact_proof_supplement SET evidence_json='{}'")
        with pytest.raises(Exception, match="append-only"):
            con.execute("DELETE FROM financial_fact_proof_supplement")


def test_identical_retry_reuses_original_completion_and_row(case, monkeypatch):
    repository, args, _ = case
    first = repository.append_machine_proof_supplement(**args)
    monkeypatch.setattr(supplement, "_now", lambda: datetime(2026,10,13,12,tzinfo=UTC))
    assert repository.append_machine_proof_supplement(**args) == first
    with closing(connect(repository.db_path, read_only=True)) as con:
        assert con.execute("SELECT count(*) FROM financial_fact_proof_supplement").fetchone()[0] == 1


def test_false_replacement_cannot_be_accepted_by_self_claim(case):
    repository, args, _ = case
    wrong = json.loads(supplement.canonical(args["replacement_evidence"]))
    wrong["fields"][0]["value_yuan"] = "9999"
    with pytest.raises(ValueError, match="current original-byte"):
        repository.append_machine_proof_supplement(**{**args,"replacement_evidence":wrong})
    assert repository.load_with_accepted_s2_pdf_reviews()[1] == {}


@pytest.mark.parametrize("tamper", ["member", "original_hash", "completion", "floor"])
def test_rehashed_outer_claims_still_require_exact_source_and_cohort(case, tamper):
    repository, args, facts = case
    row = repository.append_machine_proof_supplement(**args)
    value = json.loads(row["evidence_json"])
    if tamper == "member": value["version_ids"].pop()
    if tamper == "original_hash": value["original_evidence_hash"] = "sha256:" + "0" * 64
    if tamper == "completion": value["completed_at"] = "2027-01-01T00:00:00+00:00"
    if tamper == "floor": value["available_at"] = fixture.AVAILABLE.isoformat()
    payload = supplement.canonical(value); sha = supplement.digest(payload.encode())
    row.update(evidence_json=payload, evidence_hash=sha, supplement_id="cninfo_proof_supplement_"+sha[7:])
    with closing(connect(repository.db_path, read_only=True)) as con:
        original = con.execute("SELECT * FROM financial_fact_review").fetchone()
    with pytest.raises(ValueError):
        supplement.validate(row, {fixture._bundle_id():original},
            {fixture._bundle_id():{fact.version_id for fact in facts}}, {fact.version_id:fact for fact in facts})


def test_changed_raw_bytes_fail_closed_after_successful_supplement(case):
    repository, args, _ = case
    repository.append_machine_proof_supplement(**args)
    with closing(connect(args["archive"]["database_path"], read_only=True)) as con:
        path = con.execute("SELECT stored_path FROM raw_artifact").fetchone()[0]
    (Path(args["archive"]["root"]) / path).write_bytes(b"%PDF-1.7\nDifferent amounts.")
    assert repository.load_with_accepted_s2_pdf_reviews()[1] == {}


def test_final_calendar_check_crossing_preopen_moves_proof_to_next_day(case, monkeypatch):
    repository, args, _ = case
    clock = [datetime(2026,10,14,0,44,59,tzinfo=UTC)]
    monkeypatch.setattr(supplement, "_now", lambda: clock[0])
    original_calendar = supplement._calendar
    calls = [0]
    def calendar(*values):
        result = original_calendar(*values)
        calls[0] += 1
        if calls[0] == 3:
            clock[0] = datetime(2026,10,14,0,45,1,tzinfo=UTC)
        return result
    monkeypatch.setattr(supplement, "_calendar", calendar)
    row = repository.append_machine_proof_supplement(**args)
    assert json.loads(row["evidence_json"])["available_at"] == "2026-10-15T00:45:00+00:00"
    assert {fact.available_at for fact in repository.load_with_accepted_s2_pdf_reviews()[0].facts} == {
        datetime(2026,10,15,0,45,tzinfo=UTC)}


def test_replay_cache_binds_actual_bytes_scope_and_amounts_without_mutable_proofs(monkeypatch):
    from aquant.adapters.providers import cninfo_statement_sections, s2_machine_verification
    facts={fact.version_id:fact for fact in fixture._facts()}
    original={**fixture._evidence(list(facts.values())), 'company':'Synthetic issuer'}
    original.setdefault('document_url','https://static.cninfo.com.cn/example.pdf')
    calls=[]
    def candidate(body, **kwargs):
        calls.append((body,kwargs))
        return SimpleNamespace(by_field={fact.metric:SimpleNamespace(value_yuan=fact.value) for fact in facts.values()})
    monkeypatch.setattr(cninfo_statement_sections,'extract_section_facts',candidate)
    monkeypatch.setattr(s2_machine_verification,'verify_machine_evidence',
        lambda *args,**kwargs:SimpleNamespace(verified=True,as_evidence=lambda:{'fields':[],'sections':[],'identities':[]}))
    supplement._rederive_source.cache_clear()
    body=b'%PDF-1.7 first exact bytes'
    assert supplement._rederive(body,original,facts,set(facts))=={'fields':[],'sections':[],'identities':[]}
    mutated=supplement._rederive(body,original,facts,set(facts)); mutated['fields'].append('forged')
    assert supplement._rederive(body,original,facts,set(facts))['fields']==[]
    assert len(calls)==1
    first=next(iter(facts)); wrong={**facts,first:replace(facts[first],value=facts[first].value+1)}
    with pytest.raises(ValueError,match='cannot change a financial amount'):
        supplement._rederive(body,original,wrong,set(wrong))
    assert len(calls)==1
    supplement._rederive(b'%PDF-1.7 different actual bytes',original,facts,set(facts))
    supplement._rederive(body,original,facts,{first})
    supplement._rederive(body,{**original,'company':'Different issuer'},facts,set(facts))
    assert len(calls)==4
    supplement._rederive_source.cache_clear()
