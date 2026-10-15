"""Durable, append-only storage for :class:`FinancialFact` versions.

This repository uses its own SQLite file.  It deliberately does not run the
application's ``meta.sqlite`` migrations: a caller can populate it from a
verified announcement extractor and replay the exact facts into the existing
PIT selector after a restart.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Mapping

from ...artifact_paths import resolve_archive_member, resolve_artifact_path
from ..data.db import connect, write_tx
from .s2_pdf_review_policy import (
    S2_PDF_PILOT_COMPANIES, S2_PDF_REQUIRED_FIELDS, S2_PDF_SUBSET_POLICY,
)
from .versioned import FinancialFact, VersionedFinancialFactStore
from .proof_supplement import STORAGE_SCHEMA as _PROOF_SUPPLEMENT_SCHEMA


_SCHEMA = """
CREATE TABLE IF NOT EXISTS financial_fact (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id TEXT NOT NULL UNIQUE,
    instrument_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    period_end TEXT NOT NULL,
    statement_scope TEXT NOT NULL,
    profit_scope TEXT,
    value_kind TEXT NOT NULL,
    value_text TEXT,
    currency TEXT NOT NULL,
    raw_unit TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_document_id TEXT NOT NULL,
    source_published_date TEXT,
    source_published_at TEXT,
    timestamp_precision TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    availability_basis TEXT NOT NULL,
    pit_mode TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    supersedes_id TEXT UNIQUE REFERENCES financial_fact(version_id)
);
CREATE INDEX IF NOT EXISTS idx_financial_fact_logical
    ON financial_fact (instrument_id, metric, period_end);
CREATE UNIQUE INDEX IF NOT EXISTS idx_financial_fact_root
    ON financial_fact (instrument_id, metric, period_end, statement_scope,
                       coalesce(profit_scope, ''), currency, raw_unit)
    WHERE supersedes_id IS NULL;
CREATE TRIGGER IF NOT EXISTS financial_fact_no_update
BEFORE UPDATE ON financial_fact BEGIN
    SELECT RAISE(ABORT, 'financial facts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS financial_fact_no_delete
BEFORE DELETE ON financial_fact BEGIN
    SELECT RAISE(ABORT, 'financial facts are append-only');
END;
CREATE TABLE IF NOT EXISTS financial_fact_review (
    bundle_id TEXT PRIMARY KEY,
    evidence_hash TEXT NOT NULL UNIQUE,
    evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS financial_fact_review_member (
    bundle_id TEXT NOT NULL REFERENCES financial_fact_review(bundle_id),
    version_id TEXT NOT NULL UNIQUE REFERENCES financial_fact(version_id),
    PRIMARY KEY (bundle_id, version_id)
);
CREATE TRIGGER IF NOT EXISTS financial_fact_review_no_update
BEFORE UPDATE ON financial_fact_review BEGIN
    SELECT RAISE(ABORT, 'financial fact reviews are append-only');
END;
CREATE TRIGGER IF NOT EXISTS financial_fact_review_no_delete
BEFORE DELETE ON financial_fact_review BEGIN
    SELECT RAISE(ABORT, 'financial fact reviews are append-only');
END;
CREATE TRIGGER IF NOT EXISTS financial_fact_review_member_no_update
BEFORE UPDATE ON financial_fact_review_member BEGIN
    SELECT RAISE(ABORT, 'financial fact reviews are append-only');
END;
CREATE TRIGGER IF NOT EXISTS financial_fact_review_member_no_delete
BEFORE DELETE ON financial_fact_review_member BEGIN
    SELECT RAISE(ABORT, 'financial fact reviews are append-only');
END;
"""

_COLUMNS = (
    "version_id", "instrument_id", "metric", "period_end", "statement_scope",
    "profit_scope", "value_kind", "value_text", "currency", "raw_unit",
    "source_id", "source_document_id", "source_published_date",
    "source_published_at", "timestamp_precision", "first_seen_at",
    "ingested_at", "available_at", "availability_basis", "pit_mode",
    "content_hash", "supersedes_id",
)
_SCHEMA += _PROOF_SUPPLEMENT_SCHEMA
_SELECT = "SELECT " + ", ".join(_COLUMNS) + " FROM financial_fact"
_INSERT = ("INSERT INTO financial_fact (" + ", ".join(_COLUMNS) + ") VALUES ("
           + ", ".join("?" for _ in _COLUMNS) + ")")


def _value_to_text(value: object) -> tuple[str, str | None]:
    if value is None:
        return "none", None
    if isinstance(value, Decimal):
        return "decimal", str(value)
    if isinstance(value, bool):
        return "bool", "1" if value else "0"
    if isinstance(value, int):
        return "int", str(value)
    if isinstance(value, float):
        return "float", repr(value)
    if isinstance(value, str):
        return "str", value
    raise TypeError(f"unsupported financial fact value type: {type(value).__name__}")


def _value_from_text(kind: str, text: str | None) -> Decimal | int | float | str | None:
    if kind == "none" and text is None:
        return None
    if text is None:
        raise ValueError(f"financial fact {kind!r} value has no text")
    if kind == "decimal":
        return Decimal(text)
    if kind == "bool":
        if text not in ("0", "1"):
            raise ValueError("invalid financial fact bool value")
        return bool(int(text))
    if kind == "int":
        return int(text)
    if kind == "float":
        return float(text)
    if kind == "str":
        return text
    raise ValueError(f"unknown financial fact value kind {kind!r}")


def _encode(fact: FinancialFact) -> tuple[object, ...]:
    kind, value = _value_to_text(fact.value)
    return (
        fact.version_id, fact.instrument_id, fact.metric, fact.period_end.isoformat(),
        fact.statement_scope.value,
        fact.profit_scope.value if fact.profit_scope is not None else None,
        kind, value, fact.currency, fact.raw_unit, fact.source_id,
        fact.source_document_id,
        fact.source_published_date.isoformat() if fact.source_published_date else None,
        fact.source_published_at.isoformat() if fact.source_published_at else None,
        fact.timestamp_precision.value, fact.first_seen_at.isoformat(),
        fact.ingested_at.isoformat(), fact.available_at.isoformat(),
        fact.availability_basis.value, fact.pit_mode.value, fact.content_hash,
        fact.supersedes_id,
    )


def _decode(row: sqlite3.Row) -> FinancialFact:
    return FinancialFact(
        version_id=row["version_id"], instrument_id=row["instrument_id"],
        metric=row["metric"], period_end=date.fromisoformat(row["period_end"]),
        statement_scope=row["statement_scope"], profit_scope=row["profit_scope"],
        value=_value_from_text(row["value_kind"], row["value_text"]),
        currency=row["currency"], raw_unit=row["raw_unit"],
        source_id=row["source_id"], source_document_id=row["source_document_id"],
        source_published_date=(date.fromisoformat(row["source_published_date"])
                               if row["source_published_date"] else None),
        source_published_at=(datetime.fromisoformat(row["source_published_at"])
                             if row["source_published_at"] else None),
        timestamp_precision=row["timestamp_precision"],
        first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
        ingested_at=datetime.fromisoformat(row["ingested_at"]),
        available_at=datetime.fromisoformat(row["available_at"]),
        availability_basis=row["availability_basis"], pit_mode=row["pit_mode"],
        content_hash=row["content_hash"], supersedes_id=row["supersedes_id"],
    )


@dataclass(frozen=True, slots=True)
class FactReviewBundle:
    """Canonical, immutable evidence for one atomic reviewed report bundle."""

    bundle_id: str
    evidence_json: str

    @property
    def evidence_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.evidence_json.encode("utf-8")).hexdigest()


_S2_PDF_FIELDS = frozenset({
    "net_profit_attributable", "net_profit_consolidated", "operating_cashflow",
    "revenue", "parent_equity",
})
#: 机器核验证据协议。与 v1/v2 人工协议**并列**，不混用、不互相替代。
MACHINE_VERIFICATION_SCHEMA = "cninfo-s2-machine-verified-v1"
#: 机器核验者身份前缀。人工``reviewer_id``不得以此开头，机器核验也不得
#: 冒用人类身份，读写两侧都按前缀分开。
MACHINE_VERIFIER_PREFIX = "machine:"
_IMMUTABLE_REVIEW_TRIGGERS = frozenset({
    "financial_fact_no_update", "financial_fact_no_delete",
    "financial_fact_review_no_update", "financial_fact_review_no_delete",
    "financial_fact_review_member_no_update",
    "financial_fact_review_member_no_delete",
})


def _accepted_s2_pdf_review(
    row: sqlite3.Row, member_ids: set[str], facts: Mapping[str, FinancialFact],
) -> bool:
    """Recognize immutable v1 five-field or v2 fixed-cohort PDF proof."""

    try:
        evidence_json = row["evidence_json"]
        evidence = json.loads(evidence_json)
        if not isinstance(evidence, dict):
            return False
        canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
        if canonical != evidence_json or row["evidence_hash"] != (
            "sha256:" + hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
        ):
            return False
        schema = evidence.get("schema")
        if schema not in ("cninfo-s2-pdf-review-v1", "cninfo-s2-pdf-review-v2"):
            return False
        iid = evidence["instrument_id"]
        period = date.fromisoformat(evidence["period_end"])
        if schema == "cninfo-s2-pdf-review-v2":
            expected_fields = S2_PDF_REQUIRED_FIELDS.get(period)
            if (iid not in S2_PDF_PILOT_COMPANIES or
                    evidence.get("company") != S2_PDF_PILOT_COMPANIES[iid] or
                    evidence.get("review_policy") != S2_PDF_SUBSET_POLICY or
                    expected_fields is None or
                    evidence.get("required_fields") != sorted(expected_fields)):
                return False
            allowed_units = ("元", "千元", "万元")
        else:
            expected_fields = _S2_PDF_FIELDS
            allowed_units = ("元", "千元")
        if (evidence.get("source_id") != "cninfo" or
                evidence.get("complete_search_attested") is not True or
                evidence.get("index_query", {}).get("request_body_verified") is not True):
            return False
        version_ids = evidence.get("version_ids")
        if (not isinstance(version_ids, list) or
                len(version_ids) != len(expected_fields) or
                len(set(version_ids)) != len(expected_fields) or
                set(version_ids) != member_ids or
                not member_ids <= facts.keys()):
            return False
        announcement = evidence["announcement_id"]
        content_hash = evidence["content_hash"]
        bundle_prefix = ("cninfo_pdf_v2_" if schema == "cninfo-s2-pdf-review-v2"
                         else "cninfo_pdf_")
        if (row["bundle_id"] != bundle_prefix + hashlib.sha256(
            f"{iid}|{period}|{announcement}".encode()
        ).hexdigest()[:32] or evidence.get("announcement_role") not in
                ("ORIGINAL_REPORT", "REVISED_REPORT") or
                not all(evidence.get(key) for key in (
                    "rights_register_version", "pdf_receipt_id",
                    "org_lookup_receipt_id", "version_reviewer_id",
                    "version_reviewed_at", "available_at", "first_seen_at",
                    "ingested_at", "source_published_date"))):
            return False
        cross = evidence.get("cross_category_index")
        if (not isinstance(cross, dict) or not cross.get("reviewer_id") or
                not cross.get("reviewed_at") or
                not isinstance(cross.get("candidate_dispositions"), list)):
            return False
        reviews = evidence.get("field_reviews")
        if (not isinstance(reviews, list) or len(reviews) != len(expected_fields) or
                {review.get("field") for review in reviews if isinstance(review, dict)}
                != expected_fields):
            return False
        by_metric = {facts[version_id].metric: facts[version_id]
                     for version_id in member_ids}
        if set(by_metric) != expected_fields:
            return False
        for field, fact in by_metric.items():
            identity = (f"cninfo|{iid}|{period}|{field}|{announcement}|{content_hash}")
            expected_id = "pdf_" + hashlib.sha256(identity.encode()).hexdigest()[:32]
            expected_scope = ("ATTRIBUTABLE" if field == "net_profit_attributable" else
                              "CONSOLIDATED" if field == "net_profit_consolidated" else None)
            if (fact.version_id != expected_id or fact.instrument_id != iid or
                    fact.period_end != period or fact.source_id != "cninfo" or
                    fact.source_document_id != announcement or
                    fact.content_hash != content_hash or fact.currency != "CNY" or
                    fact.raw_unit != "yuan" or fact.statement_scope.value != "CONSOLIDATED" or
                    (fact.profit_scope.value if fact.profit_scope else None) != expected_scope or
                    fact.source_published_date.isoformat() != evidence["source_published_date"] or
                    fact.source_published_at is not None or
                    fact.timestamp_precision.value != "DATE" or
                    fact.first_seen_at.isoformat() != evidence["first_seen_at"] or
                    fact.ingested_at.isoformat() != evidence["ingested_at"] or
                    fact.available_at.isoformat() != evidence["available_at"] or
                    fact.availability_basis.value != "OBSERVED" or
                    fact.pit_mode.value != "LIVE_OBSERVED"):
                return False
        for review in reviews:
            fact = by_metric[review["field"]]
            if (review.get("method") != "HUMAN_VISUAL" or
                    not review.get("reviewer_id") or not review.get("reviewed_at") or
                    not isinstance(review.get("pdf_page"), int) or
                    review["pdf_page"] < 1 or
                    review.get("source_amount_unit") not in allowed_units or
                    not review.get("current_cell") or not review.get("row_label") or
                    Decimal(review["value_yuan"]) != Decimal(str(fact.value))):
                return False
            if schema == "cninfo-s2-pdf-review-v2":
                cell = review["current_cell"].strip().replace(",", "")
                cell = cell.replace("（", "(").replace("）", ")")
                if cell.startswith("(") and cell.endswith(")"):
                    cell = "-" + cell[1:-1]
                raw_amount = Decimal(cell)
                scale = {"元": Decimal(1), "千元": Decimal(1000),
                         "万元": Decimal(10000)}[review["source_amount_unit"]]
                if (not raw_amount.is_finite() or
                        raw_amount * scale != Decimal(review["value_yuan"])):
                    return False
        return True
    except (KeyError, AttributeError, TypeError, ValueError, ArithmeticError):
        return False


def _accepted_machine_verified_review(
    row: sqlite3.Row, member_ids: set[str], facts: Mapping[str, FinancialFact],
) -> bool:
    """Recognize an immutable machine-verification bundle.

    A bundle is admitted only when every one of its S2 facts is internally
    consistent **and** the stored evidence proves at least one independent
    verification path per field.  The evidence must not claim a human review,
    and every satisfied path must be one of the named non-human paths: a
    ``verified: true`` flag that a caller typed into JSON is not proof, because
    the field-level ``holds`` flags and their quoted source rows are checked
    against the persisted fact values.

    Archived receipts, index windows and revision relations are verified by the
    promotion writer at write time; this reader re-checks what a standalone
    fact database can still prove on its own.
    """

    try:
        evidence_json = row["evidence_json"]
        evidence = json.loads(evidence_json)
        if not isinstance(evidence, dict):
            return False
        canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
        if canonical != evidence_json or row["evidence_hash"] != (
            "sha256:" + hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
        ):
            return False
        if evidence.get("schema") != MACHINE_VERIFICATION_SCHEMA:
            return False
        if evidence.get("source_id") != "cninfo":
            return False
        verifier_id = evidence.get("verifier_id")
        if (not isinstance(verifier_id, str) or
                not verifier_id.startswith(MACHINE_VERIFIER_PREFIX) or
                not evidence.get("verifier_version")):
            return False
        if evidence.get("human_review_claimed") is not None:
            return False
        iid = evidence["instrument_id"]
        period = date.fromisoformat(evidence["period_end"])
        announcement = evidence["announcement_id"]
        content_hash = evidence["content_hash"]
        if (not isinstance(announcement, str) or not announcement or
                not isinstance(content_hash, str) or
                not content_hash.startswith("sha256:")):
            return False
        if row["bundle_id"] != "cninfo_machine_" + hashlib.sha256(
            f"{iid}|{period}|{announcement}".encode()
        ).hexdigest()[:32]:
            return False
        if evidence.get("announcement_role") not in (
                "ORIGINAL_REPORT", "REVISED_REPORT"):
            return False
        if not all(evidence.get(key) for key in (
                "rights_register_version", "pdf_receipt_id",
                "org_lookup_receipt_id", "available_at", "first_seen_at",
                "ingested_at", "source_published_date")):
            return False
        version_ids = evidence.get("version_ids")
        if (not isinstance(version_ids, list) or not version_ids or
                len(set(version_ids)) != len(version_ids) or
                set(version_ids) != member_ids or
                not member_ids <= facts.keys()):
            return False
        fields = evidence.get("fields")
        if not isinstance(fields, list) or not fields:
            return False
        by_metric = {facts[version_id].metric: facts[version_id]
                     for version_id in member_ids}
        if {item.get("field") for item in fields if isinstance(item, dict)} != set(by_metric):
            return False
        dispositions = evidence.get("cross_category_index", {}).get("candidate_dispositions", ())
        has_impact_disposition = any(isinstance(item, Mapping) and
                                    item.get("relevance") == "METRIC_NO_EFFECT"
                                    for item in dispositions)
        if ("revision_impact_proof" in evidence or "revision_impact_archive" in evidence or
                has_impact_disposition):
            if not _replay_revision_impact(evidence, by_metric):
                return False
        for item in fields:
            field = item["field"]
            fact = by_metric[field]
            identity = (f"cninfo|{iid}|{period}|{field}|{announcement}|{content_hash}")
            expected_id = "mach_" + hashlib.sha256(identity.encode()).hexdigest()[:32]
            expected_scope = ("ATTRIBUTABLE" if field == "net_profit_attributable" else
                              "CONSOLIDATED" if field == "net_profit_consolidated" else None)
            if (fact.version_id != expected_id or fact.instrument_id != iid or
                    fact.period_end != period or fact.source_id != "cninfo" or
                    fact.source_document_id != announcement or
                    fact.content_hash != content_hash or fact.currency != "CNY" or
                    fact.raw_unit != "yuan" or
                    fact.statement_scope.value != "CONSOLIDATED" or
                    (fact.profit_scope.value if fact.profit_scope else None) != expected_scope or
                    fact.source_published_date is None or
                    fact.source_published_date.isoformat() !=
                    evidence["source_published_date"] or
                    fact.source_published_at is not None or
                    fact.timestamp_precision.value != "DATE" or
                    fact.first_seen_at.isoformat() != evidence["first_seen_at"] or
                    fact.ingested_at.isoformat() != evidence["ingested_at"] or
                    fact.available_at.isoformat() != evidence["available_at"]):
                return False
            if Decimal(item["value_yuan"]) != Decimal(str(fact.value)):
                return False
            checks = item.get("checks")
            if not isinstance(checks, list) or not checks:
                return False
            satisfied = [
                check for check in checks
                if isinstance(check, dict) and check.get("holds") is True
                and check.get("path") in _MACHINE_VERIFICATION_PATHS
                and isinstance(check.get("evidence"), dict)
            ]
            if not satisfied or item.get("verified") is not True:
                return False
            if any(not _machine_path_is_independent(check, fact, bundle_evidence=evidence)
                   for check in satisfied):
                return False
        return True
    except (KeyError, AttributeError, TypeError, ValueError, ArithmeticError):
        return False


def _replay_revision_impact(bundle: Mapping[str, object],
                            facts: Mapping[str, FinancialFact]) -> bool:
    """A field exclusion must replay the complete receipt-bound relationship set.

    This does not turn a changed amount into an original version, or provide an
    independent numerical check. Those gates still run for every member below.
    Legacy bundles have no impact exclusion and keep their existing protocol.
    """
    from ...adapters.providers.s2_revision_impact import replay_revision_impact
    try:
        proof, archive = bundle["revision_impact_proof"], bundle["revision_impact_archive"]
        if not isinstance(proof, Mapping) or not isinstance(archive, Mapping) or not facts:
            return False
        company = archive["company"]
        if not isinstance(company, Mapping):
            return False
        root = resolve_artifact_path(archive["root"]).resolve()
        if resolve_artifact_path(archive["database_path"]).resolve() != root / "meta.sqlite":
            return False
        first = next(iter(facts.values()))
        if (company.get("code") != first.instrument_id or
                company.get("orgLookupReceiptId") != bundle["org_lookup_receipt_id"] or
                proof.get("instrumentId") != company.get("instrumentId") or
                proof.get("issuerName") != bundle["company"] or
                proof.get("targetPeriodEnd") != first.period_end.isoformat()):
            return False
        completed = datetime.fromisoformat(proof["completedAt"])
        if completed.tzinfo is None or any(
                not fact.first_seen_at <= completed <= fact.ingested_at <= fact.available_at
                for fact in facts.values()):
            return False
        fresh = replay_revision_impact(
            proof, archive_root=root, company=company,
            target_period_end=first.period_end, target_metrics=tuple(sorted(facts)),
            issuer_name=bundle["company"],
            documents=archive.get("documents"),
        ).as_dict()
        cross = bundle["cross_category_index"]
        index_receipts = fresh["indexReceipts"]
        if (cross["org_lookup_receipt_id"] != bundle["org_lookup_receipt_id"] or
                cross["page_receipt_ids"] != [r["receipt_id"] for r in index_receipts[1:]] or
                cross["request_body_hashes"] != fresh["relationshipScope"]["requestBodyHashes"] or
                cross["publication_window"] != fresh["relationshipScope"]["publicationWindow"] or
                not completed <= datetime.fromisoformat(cross["reviewed_at"]) <= first.ingested_at):
            return False
        dispositions = cross["candidate_dispositions"]
        active = [item for item in dispositions
                  if item["announcement_id"] == first.source_document_id]
        if (len(active) != 1 or active[0]["relevance"] != "RELATED" or
                active[0]["document_receipt_id"] != bundle["pdf_receipt_id"] or
                not active[0]["reviewer_id"].startswith(MACHINE_VERIFIER_PREFIX) or
                not completed <= datetime.fromisoformat(active[0]["reviewed_at"]) <= first.ingested_at):
            return False
        exclusions = [item for item in dispositions if item["relevance"] == "METRIC_NO_EFFECT"]
        excluded_ids = {item["announcement_id"] for item in exclusions}
        if (len(exclusions) != len(excluded_ids) or
                not set(fresh["relationshipScope"]["financialCandidateIds"]) -
                {first.source_document_id} <= excluded_ids):
            return False
        all_sources = {item["announcementId"]: item for item in fresh["sources"]}
        for item in exclusions:
            excluded_source = all_sources[item["announcement_id"]]
            if (item["announcement_id"] == first.source_document_id or
                    item["announcement_id"] not in fresh["relationshipScope"]["financialCandidateIds"] or
                    item["related_report_announcement_ids"] != [] or
                    item["document_receipt_id"] != excluded_source["receipt"]["receipt_id"] or
                    not item["reviewer_id"].startswith(MACHINE_VERIFIER_PREFIX) or
                    not completed <= datetime.fromisoformat(item["reviewed_at"]) <= first.ingested_at):
                return False
        outcomes = fresh["outcomes"]
        if (len(outcomes) != len(facts) or
                {item["metric"] for item in outcomes} != set(facts)):
            return False
        sources = [source for source in fresh["sources"]
                   if source["announcementId"] == first.source_document_id]
        if len(sources) != 1:
            return False
        source = sources[0]
        receipt = source["receipt"]
        if (source["originalHash"] != first.content_hash or
                receipt["receipt_id"] != bundle["pdf_receipt_id"] or
                receipt["content_hash"] != first.content_hash or
                datetime.fromisoformat(receipt["first_seen_at"]) != first.first_seen_at):
            return False
        for item in outcomes:
            fact = facts[item["metric"]]
            current = item["currentSource"]
            if (item["status"] != "NO_EFFECT" or item["admissionStatus"] != "NOT_ADMITTED" or
                    item["periodEnd"] != fact.period_end.isoformat() or
                    current["announcementId"] != fact.source_document_id or
                    current["sourceReportPeriodEnd"] != fact.period_end.isoformat() or
                    current["factPeriodEnd"] != fact.period_end.isoformat() or
                    current["metric"] != fact.metric or current["columnRole"] != "CURRENT" or
                    current["statementScope"] != "CONSOLIDATED" or
                    Decimal(current["amountYuan"]) != Decimal(str(fact.value))):
                return False
        return True
    except (OSError, sqlite3.Error, KeyError, AttributeError, TypeError, ValueError, ArithmeticError):
        return False


#: 被承认的独立路径。新增路径必须同时实现核对逻辑并在此登记，不能只写字符串。
_MACHINE_VERIFICATION_PATHS = frozenset({
    "parent_statement_mirror", "equity_identity", "profit_identity",
    "revenue_identity", "revenue_note_decomposition", "cashflow_identity",
    "cash_rollforward",
    "ruled_grid_cell_reextraction",
})


def _machine_path_is_independent(check: Mapping[str, object],
                                 fact: FinancialFact, *,
                                 bundle_evidence: Mapping[str, object] | None = None) -> bool:
    """该路径是否真的携带了另一处原文，而不是一个自称的布尔值。"""

    evidence = check["evidence"]
    assert isinstance(evidence, Mapping)
    if check["path"] == "ruled_grid_cell_reextraction":
        return _replay_grid_cell(evidence, fact, bundle_evidence)
    if check["path"] == "parent_statement_mirror":
        parent_row = evidence.get("parent_row")
        return (isinstance(parent_row, Mapping) and
                parent_row.get("role") == "母公司" and
                isinstance(parent_row.get("page"), int) and
                bool(parent_row.get("current_cell")) and
                Decimal(str(parent_row.get("amount_yuan"))) == Decimal(str(fact.value)))
    if check["path"] == "revenue_note_decomposition":
        # 附注收入分解：必须同时携带主营业务、其他业务与合计三处原文单元格，
        # 三者算术自洽且等于该事实的值，并且附注页**不是**利润表所在页。
        # 只带一处"合计"行、把三行写成同一处单元格、或让附注落在利润表页上，
        # 都说明这不是另一处原文，一律拒绝。
        note = evidence.get("note_section")
        rows = (evidence.get("main_business"), evidence.get("other_business"),
                evidence.get("total"))
        if not isinstance(note, Mapping) or note.get("role") != "合并":
            return False
        if not isinstance(note.get("heading_page"), int):
            return False
        if not all(isinstance(row, Mapping) and isinstance(row.get("page"), int)
                   and row.get("current_cell") for row in rows):
            return False
        if [row.get("label") for row in rows] != ["主营业务", "其他业务", "合计"]:
            return False
        # 三行必须落在**至少两处**不同单元格上：同一处单元格不可能同时承载
        # 主营业务、其他业务与合计三个数，只可能是把一行改个名字抄了三遍。
        # 只要求"≥2 处"而不是"3 处"，是因为主营业务与其他业务的数值真的可能
        # 相等（那时两行的原文单元格文本相同），此时合计行仍提供第二处原文。
        positions = {(row["page"], row["current_cell"]) for row in rows}
        if len(positions) < 2:
            return False
        income_page = evidence.get("income_statement_page")
        if (not isinstance(income_page, int)
                or income_page == note["heading_page"]
                or any(row["page"] == income_page for row in rows)):
            return False
        try:
            components = [Decimal(str(row.get("amount_yuan"))) for row in rows]
        except (ArithmeticError, TypeError, ValueError):
            return False
        return (components[0] + components[1] == components[2]
                and components[2] == Decimal(str(fact.value)))
    # 恒等式路径：至少要引用三处不同的原文行（一个被核对值加两个分量），
    # 且每个分量都带页码与原始单元格。
    rows = [value for value in evidence.values()
            if isinstance(value, Mapping) and "page" in value]
    if len(rows) < 2:
        return False
    positions = {(row.get("page"), row.get("current_cell")) for row in rows}
    return (len(positions) >= 2 and
            all(isinstance(row.get("page"), int) and row.get("current_cell")
                for row in rows))


def _replay_grid_cell(cell_evidence: Mapping[str, object], fact: FinancialFact,
                      bundle: Mapping[str, object] | None) -> bool:
    """Replay geometry against the original receipt-bound PDF, not JSON flags."""
    from urllib.parse import urlsplit
    from ...adapters.providers.cninfo_statement_geometry import extract_grid_statement_cells
    try:
        if bundle is None or not isinstance(bundle.get("geometry_archive"), Mapping):
            return False
        archive = bundle["geometry_archive"]
        receipt_id = archive["pdf_receipt_id"]
        if receipt_id != bundle["pdf_receipt_id"]:
            return False
        with closing(connect(resolve_artifact_path(archive["database_path"]), read_only=True)) as con:
            receipt = con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?", (receipt_id,)).fetchone()
            raw = con.execute("SELECT * FROM raw_artifact WHERE content_hash=?", (fact.content_hash,)).fetchone()
        if receipt is None or raw is None:
            return False
        url = urlsplit(receipt["url"])
        if (receipt["source_id"] != "cninfo" or receipt["outcome"] != "OK" or
                receipt["http_status"] != 200 or receipt["content_hash"] != fact.content_hash or
                receipt["url"] != archive["document_url"] or
                url.scheme not in ("https", "http") or url.hostname != "static.cninfo.com.cn" or
                url.path.rsplit("/", 1)[-1].lower() != f"{fact.source_document_id}.pdf" or
                datetime.fromisoformat(receipt["first_seen_at"]) != fact.first_seen_at):
            return False
        root = resolve_artifact_path(archive["root"]).resolve()
        path = resolve_archive_member(root, raw["stored_path"])
        payload = path.read_bytes()
        if (not payload.startswith(b"%PDF-") or len(payload) != receipt["byte_size"] or
                len(payload) != raw["byte_size"] or
                "sha256:" + hashlib.sha256(payload).hexdigest() != fact.content_hash):
            return False
        cells = extract_grid_statement_cells(payload, instrument_id=fact.instrument_id,
                                             period_end=fact.period_end)
        matches = [cell for cell in cells if cell.field == fact.metric]
        return (len(matches) == 1 and matches[0].amount_yuan == Decimal(str(fact.value)) and
                dict(cell_evidence) == matches[0].as_evidence())
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError, ArithmeticError):
        return False


class FinancialFactRepository:
    """Own an independent SQLite fact file and replay it into the PIT store.
    ``append_many`` is atomic and returns the number of newly inserted
    versions. An identical ``version_id`` retry returns zero; a changed retry
    raises ``ValueError``. Revisions may be supplied out of order within one
    batch, provided the complete chain is present and has one successor per
    version.
    """

    def __init__(self, db_path: str | Path, *, _create_schema: bool = True) -> None:
        if str(db_path) == ":memory:":
            raise ValueError("financial fact repository requires a persistent file")
        self.db_path = resolve_artifact_path(db_path)
        self._read_only = not _create_schema
        if not _create_schema:
            if not self.db_path.is_file():
                raise FileNotFoundError(f"financial fact repository does not exist: {self.db_path}")
            try:
                with closing(connect(self.db_path, read_only=True)) as con:
                    con.execute(_SELECT + " LIMIT 0")
            except sqlite3.Error as exc:
                raise ValueError(
                    f"invalid financial fact repository schema: {self.db_path}: {exc}"
                ) from exc
            return
        with closing(connect(self.db_path)) as con:
            # executescript commits implicitly, so schema setup stays outside
            # the short append transaction. DDL is idempotent on restart.
            con.executescript(_SCHEMA)

    @classmethod
    def open_existing(cls, db_path: str | Path) -> FinancialFactRepository:
        """Open a validated fact file without creating a file or running DDL."""

        return cls(db_path, _create_schema=False)

    def append(self, fact: FinancialFact) -> bool:
        """Append one version; return whether it was newly inserted."""

        return bool(self.append_many((fact,)))

    def append_many(self, facts: Iterable[FinancialFact], *,
                    review_bundle: FactReviewBundle | None = None) -> int:
        if self._read_only:
            raise RuntimeError("financial fact repository was opened read-only")
        incoming: dict[str, FinancialFact] = {}
        encoded: dict[str, tuple[object, ...]] = {}
        for fact in facts:
            if not isinstance(fact, FinancialFact):
                raise TypeError("append_many requires FinancialFact instances")
            payload = _encode(fact)
            previous = encoded.get(fact.version_id)
            if previous is not None and previous != payload:
                raise ValueError(f"conflicting financial fact version_id {fact.version_id!r}")
            incoming[fact.version_id] = fact
            encoded[fact.version_id] = payload
        if not incoming:
            if review_bundle is not None:
                raise ValueError("review bundle requires financial facts")
            return 0
        if review_bundle is not None:
            if not review_bundle.bundle_id or not review_bundle.evidence_json:
                raise ValueError("review bundle id and evidence are required")
            # Reject non-canonical payloads so an identical logical review has
            # one stable hash and one idempotent retry representation.
            try:
                decoded_review = json.loads(review_bundle.evidence_json)
            except (TypeError, ValueError) as exc:
                raise ValueError("review evidence must be JSON") from exc
            canonical = json.dumps(decoded_review, ensure_ascii=False,
                                   sort_keys=True, separators=(",", ":"))
            if canonical != review_bundle.evidence_json:
                raise ValueError("review evidence must use canonical JSON")

        with closing(connect(self.db_path)) as con, write_tx(con):
            existing: dict[str, FinancialFact] = {}
            new: dict[str, FinancialFact] = {}
            for version_id, fact in incoming.items():
                row = con.execute(_SELECT + " WHERE version_id = ?", (version_id,)).fetchone()
                if row is None:
                    new[version_id] = fact
                elif tuple(row[column] for column in _COLUMNS) != encoded[version_id]:
                    raise ValueError(f"conflicting financial fact version_id {version_id!r}")
                else:
                    existing[version_id] = _decode(row)

            roots: set[tuple[object, ...]] = set()
            successors: dict[str, str] = {}
            for fact in new.values():
                predecessor_id = fact.supersedes_id
                if predecessor_id is None:
                    if fact.logical_key in roots:
                        raise ValueError("financial fact has two roots for one logical fact")
                    roots.add(fact.logical_key)
                    existing_root = con.execute(
                        "SELECT version_id FROM financial_fact WHERE "
                        "instrument_id = ? AND metric = ? AND period_end = ? "
                        "AND statement_scope = ? AND profit_scope IS ? "
                        "AND currency = ? AND raw_unit = ? AND supersedes_id IS NULL",
                        (fact.instrument_id, fact.metric, fact.period_end.isoformat(),
                         fact.statement_scope.value,
                         fact.profit_scope.value if fact.profit_scope else None,
                         fact.currency, fact.raw_unit),
                    ).fetchone()
                    if existing_root is not None:
                        raise ValueError("financial fact has two roots for one logical fact")
                    continue
                earlier = successors.setdefault(predecessor_id, fact.version_id)
                if earlier != fact.version_id:
                    raise ValueError(f"financial fact {predecessor_id!r} has two successors")
                row = con.execute(
                    "SELECT version_id FROM financial_fact WHERE supersedes_id = ?",
                    (predecessor_id,),
                ).fetchone()
                if row is not None:
                    raise ValueError(f"financial fact {predecessor_id!r} already has a successor")
                prior = new.get(predecessor_id) or existing.get(predecessor_id)
                if prior is None:
                    prior_row = con.execute(
                        _SELECT + " WHERE version_id = ?", (predecessor_id,)
                    ).fetchone()
                    if prior_row is None:
                        raise ValueError(f"supersedes_id {predecessor_id!r} is not present")
                    prior = _decode(prior_row)
                if fact.logical_key != prior.logical_key:
                    raise ValueError(
                        f"financial fact {fact.version_id!r} supersedes a different logical fact")
                if fact.available_at < prior.available_at:
                    raise ValueError(
                        f"financial fact {fact.version_id!r} is available before its superseded version")

            # Sort dependencies before INSERT, satisfying the immediate FK and
            # rejecting cycles even when a whole cycle arrives in one batch.
            ordered: list[FinancialFact] = []
            visiting: set[str] = set()
            visited: set[str] = set()

            def visit(version_id: str) -> None:
                if version_id in visiting:
                    raise ValueError("financial fact revision chain contains a cycle")
                if version_id in visited:
                    return
                visiting.add(version_id)
                predecessor_id = new[version_id].supersedes_id
                if predecessor_id in new:
                    visit(predecessor_id)
                visiting.remove(version_id)
                visited.add(version_id)
                ordered.append(new[version_id])

            for version_id in new:
                visit(version_id)
            for fact in ordered:
                con.execute(_INSERT, encoded[fact.version_id])
            if review_bundle is not None:
                row = con.execute(
                    "SELECT evidence_hash, evidence_json FROM financial_fact_review "
                    "WHERE bundle_id = ?", (review_bundle.bundle_id,),
                ).fetchone()
                if row is None:
                    con.execute(
                        "INSERT INTO financial_fact_review "
                        "(bundle_id, evidence_hash, evidence_json) VALUES (?, ?, ?)",
                        (review_bundle.bundle_id, review_bundle.evidence_hash,
                         review_bundle.evidence_json),
                    )
                elif (row["evidence_hash"] != review_bundle.evidence_hash or
                      row["evidence_json"] != review_bundle.evidence_json):
                    raise ValueError("conflicting financial fact review bundle")
                existing_members = {
                    row["version_id"] for row in con.execute(
                        "SELECT version_id FROM financial_fact_review_member WHERE bundle_id = ?",
                        (review_bundle.bundle_id,),
                    )
                }
                if existing_members and existing_members != set(incoming):
                    raise ValueError("conflicting financial fact review members")
                if not existing_members:
                    con.executemany(
                        "INSERT INTO financial_fact_review_member (bundle_id, version_id) "
                        "VALUES (?, ?)",
                        ((review_bundle.bundle_id, version_id) for version_id in incoming),
                    )
            return len(ordered)

    def load(self) -> VersionedFinancialFactStore:
        """Rebuild the existing in-memory store, including every old version."""

        with closing(connect(self.db_path, read_only=True)) as con:
            rows = con.execute(_SELECT + " ORDER BY sequence").fetchall()
        return VersionedFinancialFactStore(_decode(row) for row in rows)

    def append_machine_proof_supplement(self, **kwargs):
        """Append a whole-cohort raw-replayed proof; preserve every old row."""
        from .proof_supplement import append
        return append(self, **kwargs)

    def load_with_accepted_s2_pdf_reviews(
        self,
    ) -> tuple[VersionedFinancialFactStore, dict[str, tuple[str, str]]]:
        """Read facts and accepted CNINFO S2 PDF review links in one read snapshot.

        A review is admitted only when its canonical evidence, hash, exact DB
        members, and actual fact versions agree. Invalid or incomplete reviews
        yield no accepted links; they never manufacture PIT facts. Archived
        receipts and human attestations are checked by the promotion writer,
        not reverified from this standalone fact database at read time.
        """

        with closing(connect(self.db_path, read_only=True)) as con:
            con.execute("BEGIN")
            triggers = {row["name"] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )}
            if not _IMMUTABLE_REVIEW_TRIGGERS <= triggers:
                raise ValueError("financial fact review append-only triggers are missing")
            fact_rows = con.execute(_SELECT + " ORDER BY sequence").fetchall()
            reviews = con.execute(
                "SELECT bundle_id,evidence_hash,evidence_json "
                "FROM financial_fact_review ORDER BY bundle_id"
            ).fetchall()
            memberships = con.execute(
                "SELECT bundle_id,version_id FROM financial_fact_review_member "
                "ORDER BY bundle_id,version_id"
            ).fetchall()
            has_supplements = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                          "AND name='financial_fact_proof_supplement'").fetchone()
            supplements = []
            if has_supplements:
                from .proof_supplement import TRIGGERS
                if not TRIGGERS <= triggers:
                    raise ValueError("financial proof supplement append-only triggers are missing")
                supplements = con.execute("SELECT * FROM financial_fact_proof_supplement "
                                          "ORDER BY supplement_id").fetchall()
            con.execute("COMMIT")
        store = VersionedFinancialFactStore(_decode(row) for row in fact_rows)
        by_id = {fact.version_id: fact for fact in store.facts}
        members: dict[str, set[str]] = {}
        for row in memberships:
            members.setdefault(row["bundle_id"], set()).add(row["version_id"])
        valid_bundles: dict[tuple[str, str, str], list[tuple[sqlite3.Row, set[str]]]] = {}
        for row in reviews:
            member_ids = members.get(row["bundle_id"], set())
            if _accepted_s2_pdf_review(row, member_ids, by_id):
                evidence = json.loads(row["evidence_json"])
                report_key = (evidence["instrument_id"], evidence["period_end"],
                              evidence["announcement_id"])
                valid_bundles.setdefault(report_key, []).append((row, member_ids))
                continue
            if _accepted_machine_verified_review(row, member_ids, by_id):
                evidence = json.loads(row["evidence_json"])
                report_key = (evidence["instrument_id"], evidence["period_end"],
                              evidence["announcement_id"])
                valid_bundles.setdefault(report_key, []).append((row, member_ids))
        supplement_floors = {}
        if supplements:
            from .proof_supplement import validate
            originals = {row["bundle_id"]: row for row in reviews}
            for supplement in supplements:
                try:
                    base_id, cohort, available = validate(supplement, originals, members, by_id)
                except (OSError, sqlite3.Error, KeyError, TypeError, ValueError, ArithmeticError):
                    continue
                base = json.loads(originals[base_id]["evidence_json"])
                key = (base["instrument_id"], base["period_end"], base["announcement_id"])
                existing = valid_bundles.get(key, [])
                # Explicitly replaces proof for the same exact cohort, never
                # merges fields from two report or human/machine namespaces.
                existing = [(row, ids) for row, ids in existing if row["bundle_id"] != base_id]
                link = {"bundle_id": supplement["supplement_id"], "evidence_hash": supplement["evidence_hash"]}
                valid_bundles[key] = existing + [(link, cohort)]
                supplement_floors.update({version:available for version in cohort})
        accepted: dict[str, tuple[str, str]] = {}
        for bundles in valid_bundles.values():
            # A report has one immutable review cohort. Two independent proofs
            # of the same announcement would permit a silent field-set mix, and
            # the human and machine protocols must never be mixed either.
            if len(bundles) != 1:
                continue
            row, member_ids = bundles[0]
            for version_id in member_ids:
                accepted[version_id] = (row["bundle_id"], row["evidence_hash"])
        if supplement_floors:
            store = VersionedFinancialFactStore(
                replace(fact, available_at=max(fact.available_at, supplement_floors[fact.version_id]))
                if fact.version_id in accepted and fact.version_id in supplement_floors else fact
                for fact in store.facts)
        return store, accepted


__all__ = ["FactReviewBundle", "FinancialFactRepository"]
