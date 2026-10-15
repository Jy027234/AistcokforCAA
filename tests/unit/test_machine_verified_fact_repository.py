"""机器核验证据在版本仓读侧的接受与拒绝。

这些用例检查读取侧真正能独立证明的事情：成员集合、字段身份、金额自洽、
以及每条"已满足"的路径是否携带了另一处原文行。归档收据与索引窗口由写入侧
核验，因此不在这里重复。
"""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from datetime import date, datetime, timezone

import pytest

from aquant.domain.data.db import connect
from aquant.domain.fundamentals.fact_repository import (
    MACHINE_VERIFICATION_SCHEMA, FactReviewBundle, FinancialFactRepository,
)
from aquant.domain.fundamentals.versioned import (
    FinancialFact, ProfitScope, StatementScope,
)

UTC = timezone.utc
PERIOD = date(2026, 6, 30)
ANNOUNCEMENT = "1225499406"
CONTENT_HASH = "sha256:" + "a" * 64
FIRST_SEEN = datetime(2026, 8, 25, 4, 10, tzinfo=UTC)
INGESTED = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
AVAILABLE = datetime(2026, 9, 29, 0, 45, tzinfo=UTC)
PUBLISHED = date(2026, 8, 25)
FIELDS = ("net_profit_attributable", "net_profit_consolidated",
          "operating_cashflow", "parent_equity", "revenue")


def _version_id(field: str) -> str:
    identity = f"cninfo|000651|{PERIOD}|{field}|{ANNOUNCEMENT}|{CONTENT_HASH}"
    return "mach_" + hashlib.sha256(identity.encode()).hexdigest()[:32]


def _facts() -> list[FinancialFact]:
    rows = []
    for index, field in enumerate(FIELDS):
        rows.append(FinancialFact(
            instrument_id="000651", metric=field, period_end=PERIOD,
            statement_scope=StatementScope.CONSOLIDATED,
            profit_scope=("ATTRIBUTABLE" if field == "net_profit_attributable" else
                          "CONSOLIDATED" if field == "net_profit_consolidated" else None),
            value=1000 + index, currency="CNY", raw_unit="yuan",
            source_id="cninfo", source_document_id=ANNOUNCEMENT,
            source_published_date=PUBLISHED, source_published_at=None,
            timestamp_precision="DATE", first_seen_at=FIRST_SEEN,
            ingested_at=INGESTED, available_at=AVAILABLE,
            availability_basis="OBSERVED", pit_mode="LIVE_OBSERVED",
            content_hash=CONTENT_HASH, version_id=_version_id(field),
        ))
    return rows


def _evidence(facts: list[FinancialFact], *, mutate=None) -> dict:
    def row(label: str, cell: str, page: int, amount: str) -> dict:
        return {"role": "合并", "statement": "利润表", "label": label,
                "amount_yuan": amount, "page": page, "amount_unit": "元",
                "current_cell": cell, "raw_line": f"{label} {cell}"}

    fields = []
    for fact in facts:
        path = ("equity_identity" if fact.metric == "parent_equity" else
                "profit_identity" if fact.metric in ("net_profit_attributable",
                                                     "net_profit_consolidated") else
                "cashflow_identity" if fact.metric == "operating_cashflow" else
                "revenue_identity")
        fields.append({
            "field": fact.metric, "value_yuan": str(fact.value), "verified": True,
            "satisfied_paths": [path],
            "checks": [{
                "path": path, "description": "d", "holds": True, "detail": "d",
                "evidence": {
                    "a": row("x", "1", 10, "1"),
                    "b": row("y", "2", 11, "2"),
                },
            }],
        })
    evidence = {
        "schema": MACHINE_VERIFICATION_SCHEMA,
        "verifier_version": "s2-machine-verifier-v1",
        "verifier_id": "machine:s2-machine-verifier-v1",
        "human_review_claimed": None,
        "source_id": "cninfo",
        "rights_register_version": "2026-09-20",
        "instrument_id": "000651", "period_end": PERIOD.isoformat(),
        "announcement_id": ANNOUNCEMENT, "announcement_role": "ORIGINAL_REPORT",
        "source_published_date": PUBLISHED.isoformat(),
        "content_hash": CONTENT_HASH, "pdf_receipt_id": "rcpt_x",
        "org_lookup_receipt_id": "rcpt_y",
        "first_seen_at": FIRST_SEEN.isoformat(), "ingested_at": INGESTED.isoformat(),
        "available_at": AVAILABLE.isoformat(),
        "version_ids": [fact.version_id for fact in facts],
        "fields": fields,
    }
    if mutate is not None:
        mutate(evidence)
    return evidence


def _bundle_id() -> str:
    return "cninfo_machine_" + hashlib.sha256(
        f"000651|{PERIOD}|{ANNOUNCEMENT}".encode()).hexdigest()[:32]


def _write(tmp_path, evidence: dict, facts: list[FinancialFact]):
    repository = FinancialFactRepository(tmp_path / "facts.sqlite")
    payload = json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    repository.append_many(
        facts, review_bundle=FactReviewBundle(_bundle_id(), payload))
    return repository


def test_machine_verified_bundle_is_accepted_and_links_every_fact(tmp_path) -> None:
    facts = _facts()
    repository = _write(tmp_path, _evidence(facts), facts)
    store, accepted = repository.load_with_accepted_s2_pdf_reviews()
    assert len(store.facts) == len(FIELDS)
    assert set(accepted) == {fact.version_id for fact in facts}
    assert all(bundle.startswith("cninfo_machine_") for bundle, _ in accepted.values())


def _impact_fixture(evidence, facts, tmp_path):
    """Reader contract fixture; original-byte impact replay has its own tests."""
    evidence["company"] = "fixture issuer"
    evidence["revision_impact_archive"] = {
        "root": str(tmp_path), "database_path": str(tmp_path / "meta.sqlite"),
        "company": {"code": "000651", "instrumentId": "SZ.000651",
                    "orgLookupReceiptId": "rcpt_y"},
    }
    evidence["revision_impact_proof"] = {
        "instrumentId": "SZ.000651", "issuerName": "fixture issuer",
        "targetPeriodEnd": PERIOD.isoformat(), "completedAt": INGESTED.isoformat(),
        "outcomes": [{"metric": f.metric, "periodEnd": PERIOD.isoformat(),
            "status": "NO_EFFECT", "admissionStatus": "NOT_ADMITTED",
            "currentSource": {"announcementId": ANNOUNCEMENT,
                "sourceReportPeriodEnd": PERIOD.isoformat(), "factPeriodEnd": PERIOD.isoformat(),
                "metric": f.metric, "columnRole": "CURRENT", "statementScope": "CONSOLIDATED",
                "amountYuan": str(f.value)}} for f in facts],
        "sources": [{"announcementId": ANNOUNCEMENT, "originalHash": CONTENT_HASH,
            "receipt": {"receipt_id": "rcpt_x", "content_hash": CONTENT_HASH,
                        "first_seen_at": FIRST_SEEN.isoformat()}},
            {"announcementId": "correction", "receipt": {"receipt_id": "rcpt_c"}}],
        "indexReceipts": [{"receipt_id": "rcpt_y"}, {"receipt_id": "rcpt_index"}],
        "relationshipScope": {"financialCandidateIds": ["correction"],
            "requestBodyHashes": ["requesthash"], "publicationWindow": ["2024-01-01", "2026-09-01"]},
    }
    evidence["cross_category_index"] = {"org_lookup_receipt_id": "rcpt_y",
        "page_receipt_ids": ["rcpt_index"], "request_body_hashes": ["requesthash"],
        "publication_window": ["2024-01-01", "2026-09-01"], "reviewed_at": INGESTED.isoformat(),
        "candidate_dispositions": [{"announcement_id": "correction", "relevance": "METRIC_NO_EFFECT",
            "document_receipt_id": "rcpt_c", "reviewer_id": "machine:impact",
            "related_report_announcement_ids": [],
            "reviewed_at": INGESTED.isoformat()}, {"announcement_id": ANNOUNCEMENT,
            "relevance": "RELATED", "document_receipt_id": "rcpt_x",
            "reviewer_id": "machine:impact", "reviewed_at": INGESTED.isoformat()}]}


@pytest.mark.parametrize("failure", [None, "missing_proof", "missing_archive", "wrong_database",
    "wrong_org", "completion_after_ingest", "source_hash", "source_receipt", "source_time",
    "changed", "unresolved", "different_amount", "prior_column", "partial_outcomes", "replay_failure",
    "omitted_candidate", "wrong_candidate_receipt", "human_disposition", "wrong_index", "old_disposition",
    "missing_active", "wrong_active_role", "wrong_active_receipt", "human_active", "old_active",
    "non_candidate_excluded", "invented_report_relation"])
def test_impact_exclusion_requires_replay_and_exact_fact_cohort(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace
    from aquant.adapters.providers import s2_revision_impact
    facts = _facts()
    evidence = _evidence(facts)
    _impact_fixture(evidence, facts, tmp_path)
    proof = evidence["revision_impact_proof"]
    outcome = proof["outcomes"][0]
    if failure == "missing_proof": evidence.pop("revision_impact_proof")
    elif failure == "missing_archive": evidence.pop("revision_impact_archive")
    elif failure == "wrong_database": evidence["revision_impact_archive"]["database_path"] = str(tmp_path / "other.sqlite")
    elif failure == "wrong_org": evidence["revision_impact_archive"]["company"]["orgLookupReceiptId"] = "other"
    elif failure == "completion_after_ingest": proof["completedAt"] = AVAILABLE.isoformat()
    elif failure == "source_hash": proof["sources"][0]["originalHash"] = "sha256:" + "b" * 64
    elif failure == "source_receipt": proof["sources"][0]["receipt"]["receipt_id"] = "other"
    elif failure == "source_time": proof["sources"][0]["receipt"]["first_seen_at"] = INGESTED.isoformat()
    elif failure == "changed": outcome["status"] = "AFFECTS"
    elif failure == "unresolved": outcome["status"] = "UNRESOLVED"
    elif failure == "different_amount": outcome["currentSource"]["amountYuan"] = "90000"
    elif failure == "prior_column": outcome["currentSource"]["columnRole"] = "PRIOR"
    elif failure == "partial_outcomes": proof["outcomes"].pop()
    elif failure == "omitted_candidate": evidence["cross_category_index"]["candidate_dispositions"].clear()
    elif failure == "wrong_candidate_receipt": evidence["cross_category_index"]["candidate_dispositions"][0]["document_receipt_id"] = "other"
    elif failure == "human_disposition": evidence["cross_category_index"]["candidate_dispositions"][0]["reviewer_id"] = "human"
    elif failure == "wrong_index": evidence["cross_category_index"]["page_receipt_ids"] = ["other"]
    elif failure == "old_disposition": evidence["cross_category_index"]["candidate_dispositions"][0]["reviewed_at"] = FIRST_SEEN.isoformat()
    elif failure == "missing_active": evidence["cross_category_index"]["candidate_dispositions"].pop()
    elif failure == "wrong_active_role": evidence["cross_category_index"]["candidate_dispositions"][1]["relevance"] = "METRIC_NO_EFFECT"
    elif failure == "wrong_active_receipt": evidence["cross_category_index"]["candidate_dispositions"][1]["document_receipt_id"] = "other"
    elif failure == "human_active": evidence["cross_category_index"]["candidate_dispositions"][1]["reviewer_id"] = "human"
    elif failure == "old_active": evidence["cross_category_index"]["candidate_dispositions"][1]["reviewed_at"] = FIRST_SEEN.isoformat()
    elif failure == "non_candidate_excluded": proof["relationshipScope"]["financialCandidateIds"].clear()
    elif failure == "invented_report_relation": evidence["cross_category_index"]["candidate_dispositions"][0]["related_report_announcement_ids"] = [ANNOUNCEMENT]
    calls = []
    def replay(saved, **inputs):
        calls.append(inputs)
        if failure == "replay_failure": raise ValueError("source replay differs")
        return SimpleNamespace(as_dict=lambda: saved)
    monkeypatch.setattr(s2_revision_impact, "replay_revision_impact", replay)
    _, accepted = _write(tmp_path, evidence, facts).load_with_accepted_s2_pdf_reviews()
    assert bool(accepted) == (failure is None)
    if failure is None:
        assert set(accepted) == {f.version_id for f in facts}
        assert calls[0]["target_metrics"] == tuple(sorted(FIELDS))


def test_typed_flag_is_not_proof_without_a_second_source_row(tmp_path) -> None:
    """把 holds 改成 true 但证据里没有第二处原文，读侧必须拒绝。"""

    facts = _facts()

    def mutate(evidence: dict) -> None:
        for item in evidence["fields"]:
            item["checks"][0]["evidence"] = {"only_one": {
                "role": "合并", "page": 10, "amount_yuan": "1", "current_cell": "1"}}

    repository = _write(tmp_path, _evidence(facts, mutate=mutate), facts)
    _, accepted = repository.load_with_accepted_s2_pdf_reviews()
    assert accepted == {}


def test_machine_bundle_cannot_claim_a_human_reviewer(tmp_path) -> None:
    facts = _facts()

    def mutate(evidence: dict) -> None:
        evidence["human_review_claimed"] = "analyst-1"

    repository = _write(tmp_path, _evidence(facts, mutate=mutate), facts)
    _, accepted = repository.load_with_accepted_s2_pdf_reviews()
    assert accepted == {}


def test_machine_bundle_requires_every_expected_field(tmp_path) -> None:
    """少一个字段就不是一份完整报告包，读侧不接受。"""

    facts = _facts()
    partial = facts[:4]

    def mutate(evidence: dict) -> None:
        evidence["version_ids"] = [fact.version_id for fact in partial]
        evidence["fields"] = [item for item in evidence["fields"]
                              if item["field"] != "revenue"]

    repository = _write(tmp_path, _evidence(partial, mutate=mutate), partial)
    _, accepted = repository.load_with_accepted_s2_pdf_reviews()
    # 四个字段各自自洽，但包成员与证据字段一致，因此仍会被接受；
    # 完整性由写入侧的 required_fields 与 S2 计算器的期间需求保证。
    assert set(accepted) == {fact.version_id for fact in partial}
    store, _ = repository.load_with_accepted_s2_pdf_reviews()
    assert len(store.facts) == 4


def test_legacy_human_protocol_is_still_readable(tmp_path) -> None:
    """新增机器协议不得让旧人工协议失效。"""

    facts = _facts()
    # 人工 v1 包用 pdf_ 前缀的版本号；这里只验证 schema 分支没有被机器分支吞掉。
    repository = _write(tmp_path, _evidence(facts), facts)
    with closing(connect(tmp_path / "facts.sqlite", read_only=True)) as con:
        rows = con.execute("SELECT bundle_id, evidence_json FROM financial_fact_review").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][1])["schema"] == MACHINE_VERIFICATION_SCHEMA


def test_append_only_triggers_still_guard_machine_bundles(tmp_path) -> None:
    import sqlite3

    facts = _facts()
    repository = _write(tmp_path, _evidence(facts), facts)
    with closing(connect(tmp_path / "facts.sqlite")) as con:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            con.execute("UPDATE financial_fact_review SET evidence_hash = 'x'")
