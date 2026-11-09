"""Finite 600578 FY24 revenue CURRENT -> FY25 PRIOR_ADJUSTED chain.

This candidate is deliberately isolated from the production reader and writer.
It writes the original CURRENT root and its later adjusted comparative in one
immutable review transaction. The caller must supply ordinary report review
contexts and complete current issuer impact proofs; this module never treats a
saved REAL_CHANGE flag as admission evidence.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, fields, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence

from ...domain.data.pit import AvailabilityBasis, PitMode, TimestampPrecision, date_only_available_at
from ...domain.data.rights import REGISTER_VERSION, RightsRegistry, default_rights
from ...domain.fundamentals.fact_repository import FactReviewBundle, FinancialFactRepository
from ...domain.fundamentals.versioned import FinancialFact, StatementScope
from . import s2_revision_impact as impact
from .cninfo import CrossCategoryIndexResult
from .cninfo_statement_sections import (
    ROLE_CONSOLIDATED, extract_section_facts, extract_statement_sections,
)
from .cninfo_probe import parse_report_period, title_matches_period, title_revision_flags
from .cninfo_financial_pdf import CninfoS2CandidateFacts, PdfCandidateFact
from .pdf_promotion import (
    CrossCategoryDisposition, PdfVersionReview, _archived_ok,
    _cross_category_evidence, _next_available_snapshot, _version_evidence,
)
from .s2_machine_verification import verify_machine_evidence


SCHEMA = "aquant.cninfo.600578-fy24-revenue-adjusted-forward-root-v1"
REVIEW_PREFIX = "cninfo_machine_600578_fy24_revenue_chain_"
ORIGINAL_ID = "1223304346"
PRESENTING_ID = "1225184640"
ORIGINAL_RECEIPT_ID = "rcpt_cb751b458675a373fe8fb12e"
PRESENTING_RECEIPT_ID = "rcpt_7c0e475d6679cb6b774a4ed0"
ORIGINAL_PERIOD = date(2024, 12, 31)
PRESENTING_PERIOD = date(2025, 12, 31)
COMPANY = "北京京能电力股份有限公司"
COMPANY_SHORT_NAME = "京能电力"
STOCK_CODE = "600578"
INSTRUMENT = "SH.600578"
ORG_RECEIPT_ID = "rcpt_13773c7d5e193857ea97a0ac"
ORG_CONTENT_HASH = "sha256:8741b7934c8c465542823d78f5ab320630452c13fe629d277ea4d0745662e1c9"
CURRENT27_PACKET_SHA256 = "sha256:ecbbd52bfdd37c4913bcf99415e70ce33ac019e9dbd9428aa25adcc51b1e3149"
ISSUER_NAME_DERIVATION_SCHEMA = "aquant.600578-current27-issuer-name-derivation.v1"
ORIGINAL_YUAN = Decimal("35428028379.25")
ADJUSTED_YUAN = Decimal("35423751313.61")
FY25_CURRENT_YUAN = Decimal("35425723476.94")
DELTA_YUAN = ADJUSTED_YUAN - ORIGINAL_YUAN


class RevenueChainContractError(ValueError):
    """A required 600578 source, scope, row, or version link failed replay."""


@dataclass(frozen=True, slots=True)
class ReportReviewContext:
    """Normal report/index review and full-scope impact context for one report."""

    pdf_receipt_id: str
    version_review: PdfVersionReview
    cross_category_index: CrossCategoryIndexResult
    dispositions: tuple[CrossCategoryDisposition, ...]
    reviewed_by: str
    reviewed_at: datetime
    revision_impact_proof: Mapping[str, object]
    revision_impact_archive: Mapping[str, object]


def _canonical(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_PDF_CANDIDATE_FACT_FIELDS = (
    "field", "value_yuan", "period_end", "report_period_text", "statement",
    "pdf_page", "column_header", "amount_unit", "currency", "source_row",
    "source_cells", "source_current_cell_index", "source_prior_cell_index",
    "pdf_sha256", "display_row_label",
)
_S2_CANDIDATE_FACTS_FIELDS = (
    "instrument_id", "period_end", "version_label", "document_url", "pdf_sha256",
    "candidates", "pit_eligible", "review_status",
)


def _candidate_text(value: object, name: str) -> str:
    if type(value) is not str:
        raise RevenueChainContractError(f"native PDF candidate {name} must be text")
    return value


def _candidate_fact_evidence(candidate: PdfCandidateFact) -> dict[str, object]:
    if (type(candidate) is not PdfCandidateFact or
            tuple(item.name for item in fields(PdfCandidateFact)) != _PDF_CANDIDATE_FACT_FIELDS):
        raise RevenueChainContractError("native PDF candidate-fact schema differs from the closed serializer")
    if (not isinstance(candidate.value_yuan, Decimal) or not candidate.value_yuan.is_finite() or
            type(candidate.period_end) is not date or type(candidate.pdf_page) is not int or
            type(candidate.source_current_cell_index) is not int or
            type(candidate.source_prior_cell_index) is not int or type(candidate.source_cells) is not tuple or
            any(value is not None and type(value) is not str for value in candidate.source_cells)):
        raise RevenueChainContractError("native PDF candidate-fact value types differ from the closed serializer")
    return {
        "field": _candidate_text(candidate.field, "field"),
        "value_yuan": str(candidate.value_yuan),
        "period_end": candidate.period_end.isoformat(),
        "report_period_text": _candidate_text(candidate.report_period_text, "report_period_text"),
        "statement": _candidate_text(candidate.statement, "statement"),
        "pdf_page": candidate.pdf_page,
        "column_header": _candidate_text(candidate.column_header, "column_header"),
        "amount_unit": _candidate_text(candidate.amount_unit, "amount_unit"),
        "currency": _candidate_text(candidate.currency, "currency"),
        "source_row": _candidate_text(candidate.source_row, "source_row"),
        "source_cells": list(candidate.source_cells),
        "source_current_cell_index": candidate.source_current_cell_index,
        "source_prior_cell_index": candidate.source_prior_cell_index,
        "pdf_sha256": _candidate_text(candidate.pdf_sha256, "pdf_sha256"),
        "display_row_label": _candidate_text(candidate.display_row_label, "display_row_label"),
    }


def _candidate_evidence(candidate: CninfoS2CandidateFacts) -> dict[str, object]:
    """Encode the actual frozen native candidate dataclass as closed JSON data."""
    if (type(candidate) is not CninfoS2CandidateFacts or
            tuple(item.name for item in fields(CninfoS2CandidateFacts)) != _S2_CANDIDATE_FACTS_FIELDS):
        raise RevenueChainContractError("native S2 candidate schema differs from the closed serializer")
    if (type(candidate.period_end) is not date or type(candidate.candidates) is not tuple or
            type(candidate.pit_eligible) is not bool):
        raise RevenueChainContractError("native S2 candidate value types differ from the closed serializer")
    return {
        "instrument_id": _candidate_text(candidate.instrument_id, "instrument_id"),
        "period_end": candidate.period_end.isoformat(),
        "version_label": _candidate_text(candidate.version_label, "version_label"),
        "document_url": _candidate_text(candidate.document_url, "document_url"),
        "pdf_sha256": _candidate_text(candidate.pdf_sha256, "pdf_sha256"),
        "candidates": [_candidate_fact_evidence(item) for item in candidate.candidates],
        "pit_eligible": candidate.pit_eligible,
        "review_status": _candidate_text(candidate.review_status, "review_status"),
    }


def _candidate_evidence_matches(stored: object, candidate: CninfoS2CandidateFacts) -> bool:
    """Compare writer JSON with an independently re-extracted native candidate."""
    return isinstance(stored, Mapping) and _canonical(stored) == _canonical(_candidate_evidence(candidate))


def _version_id(*parts: str) -> str:
    return "mach_" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def _review_fields(review: PdfVersionReview) -> dict[str, object]:
    return {
        "announcementId": review.announcement_id,
        "orgLookupReceiptId": review.org_lookup_receipt_id,
        "indexReceiptId": review.index_receipt_id,
        "searchReceiptIds": list(review.search_receipt_ids),
        "searchPageNumbers": list(review.search_page_numbers),
        "queryStockCode": review.query_stock_code,
        "queryOrganizationId": review.query_organization_id,
        "queryPeriodStart": review.query_period_start.isoformat(),
        "queryPeriodEnd": review.query_period_end.isoformat(),
        "queryDigest": review.query_digest,
        "reviewerId": review.reviewer_id,
        "reviewedAt": review.reviewed_at.isoformat(),
        "completeSearchAttested": review.complete_search_attested,
        "predecessorAnnouncementId": review.predecessor_announcement_id,
        "correctionReceiptId": review.correction_receipt_id,
        "correctionExcerpt": review.correction_excerpt,
    }


def _cross_index_fields(index: CrossCategoryIndexResult) -> dict[str, object]:
    return {
        "stockCode": index.stock_code,
        "organizationId": index.organization_id,
        "reportPeriod": index.report_period,
        "publicationWindow": [day.isoformat() for day in index.publication_window],
        "orgLookupReceiptId": index.org_lookup_receipt_id,
        "orgLookupContentHash": index.org_lookup_content_hash,
        "pages": [{
            "pageNum": page.page_num, "requestBodyHash": page.request_body_hash,
            "receiptId": page.receipt_id, "contentHash": page.content_hash,
            "httpStatus": page.http_status,
            "rawAnnouncementCount": page.raw_announcement_count,
            "totalAnnouncement": page.total_announcement, "hasMore": page.has_more,
        } for page in index.pages],
        "announcements": [{
            "announcementId": item.announcement_id, "secCode": item.sec_code,
            "title": item.title, "announcementTimeMs": item.announcement_time_ms,
            "documentUrl": item.document_url, "candidateReasons": list(item.candidate_reasons),
            "pageNum": item.page_num, "pageReceiptId": item.page_receipt_id,
            "pageContentHash": item.page_content_hash,
        } for item in index.announcements],
        "skippedDifferentSecurity": index.skipped_different_security,
        "totalAnnouncement": index.total_announcement,
        "complete": index.complete, "termination": index.termination, "error": index.error,
    }


def _disposition_inputs(items: Sequence[CrossCategoryDisposition]) -> list[dict[str, object]]:
    return [{
        "announcementId": item.announcement_id, "relevance": item.relevance,
        "reviewerId": item.reviewer_id, "reviewedAt": item.reviewed_at.isoformat(),
        "rationale": item.rationale, "documentReceiptId": item.document_receipt_id,
        "relatedReportAnnouncementIds": list(item.related_report_announcement_ids),
        "relationReceiptId": item.relation_receipt_id,
        "relationExcerpt": item.relation_excerpt,
    } for item in items]


def _company_scope(value: Mapping[str, object]) -> Mapping[str, object]:
    company = value.get("company") if isinstance(value.get("company"), Mapping) else value
    if not isinstance(company, Mapping):
        raise RevenueChainContractError("derived 600578 issuer descriptor is required")
    derivation = company.get("issuerNameDerivation")
    publication_window = company.get("publicationWindow")
    expected_derivation = {
        "schema": ISSUER_NAME_DERIVATION_SCHEMA,
        "sourcePacketSha256": CURRENT27_PACKET_SHA256,
        "sourceCompanyNameField": "sourceOnlyCompany.name",
        "sourceCompanyFullName": COMPANY,
        "officialCode": STOCK_CODE,
        "officialOrganizationId": "gssh0600578",
        "officialZwjc": COMPANY_SHORT_NAME,
        "officialZwjcField": "zwjc",
        "orgLookupReceiptId": ORG_RECEIPT_ID,
        "orgLookupContentHash": ORG_CONTENT_HASH,
        "actualRequestBeijingDay": "2026-10-22",
        "reuseEvidenceDay": "2026-10-27",
        "archiveVerified": True,
    }
    if (company.get("code") != STOCK_CODE or
            company.get("instrumentId") != INSTRUMENT or
            company.get("name") != COMPANY_SHORT_NAME or
            company.get("issuerFullName") != COMPANY or
            company.get("organizationId") != "gssh0600578" or
            publication_window != ["2024-06-30", "2026-10-27"] or
            company.get("orgLookupReceiptId") != ORG_RECEIPT_ID or
            company.get("orgLookupContentHash") != ORG_CONTENT_HASH or
            derivation != expected_derivation or
            not company.get("selectedReportRecords") or
            not company.get("allCategoryPages")):
        raise RevenueChainContractError("complete 600578 current issuer descriptor is required")
    return company


def _actual_database_path(archive) -> str:
    rows = archive.con.execute("PRAGMA database_list").fetchall()
    actual = next((row[2] for row in rows if row[1] == "main"), None)
    if not actual:
        raise RevenueChainContractError("archive database path is unavailable")
    return str(Path(actual).resolve())


def _scope_archive(archive, company: Mapping[str, object]) -> dict[str, object]:
    return {"root": str(Path(archive.root).resolve()),
            "database_path": _actual_database_path(archive), "company": company}


def _report_record(company: Mapping[str, object], announcement_id: str) -> Mapping[str, object]:
    rows = [item for item in company.get("selectedReportRecords", ())
            if isinstance(item, Mapping) and str(item.get("announcementId")) == announcement_id]
    if len(rows) != 1:
        raise RevenueChainContractError(f"current issuer descriptor lacks unique report {announcement_id}")
    return rows[0]


def _validate_company_report(record: Mapping[str, object], *, announcement_id: str,
                             period_end: date, title: str, url: str) -> None:
    if (record.get("announcementId") != announcement_id or
            record.get("code") != STOCK_CODE or record.get("instrumentId") != INSTRUMENT or
            record.get("periodEnd") != period_end.isoformat() or record.get("title") != title or
            record.get("documentUrl") != url or record.get("contentHash") !=
            record.get("receipt", {}).get("content_hash") or
            record.get("receipt", {}).get("url") != url or
            record.get("receipt", {}).get("source_id") != "cninfo" or
            record.get("receipt", {}).get("outcome") != "OK" or
            record.get("receipt", {}).get("http_status") != 200):
        raise RevenueChainContractError(f"official report identity/receipt differs for {announcement_id}")


def _replay_impact(context: ReportReviewContext, *, archive, company: Mapping[str, object],
                   target_period: date) -> dict[str, object]:
    descriptor = context.revision_impact_archive
    if (not isinstance(descriptor, Mapping) or
            Path(str(descriptor.get("root", ""))).resolve() != Path(archive.root).resolve() or
            Path(str(descriptor.get("database_path", ""))).resolve() !=
            Path(_actual_database_path(archive)).resolve() or
            descriptor.get("company") != company):
        raise RevenueChainContractError("impact archive/company is not the current captured issuer scope")
    proof = dict(context.revision_impact_proof)
    try:
        impact.replay_revision_impact(
            proof, archive_root=archive.root, company=company,
            documents=descriptor.get("documents"), target_period_end=target_period,
            target_metrics=("revenue",), issuer_name=COMPANY,
        )
    except (OSError, sqlite3.Error, KeyError, AttributeError, TypeError, ValueError,
            ArithmeticError) as exc:
        raise RevenueChainContractError(f"current full-scope impact proof failed replay: {exc}") from exc
    scope = proof.get("relationshipScope")
    if (proof.get("schema") != impact.SCHEMA or proof.get("instrumentId") != INSTRUMENT or
            proof.get("issuerName") != COMPANY or proof.get("targetPeriodEnd") != target_period.isoformat() or
            not isinstance(scope, Mapping) or scope.get("completeForArchivedIndexResponse") is not True or
            scope.get("missingReportSourceIds") != [] or
            scope.get("publicationWindow") != list(company.get("publicationWindow", ()))):
        raise RevenueChainContractError("replayed current impact scope is incomplete or differently bound")
    impact_completed = datetime.fromisoformat(str(proof["completedAt"]))
    if (impact_completed > context.version_review.reviewed_at or
            impact_completed > context.reviewed_at):
        raise RevenueChainContractError("full-scope impact replay must precede report/disposition review")
    return proof


def _match_impact_indexes(proof: Mapping[str, object], context: ReportReviewContext) -> None:
    scope = proof["relationshipScope"]
    index = context.cross_category_index
    receipts = [index.org_lookup_receipt_id, *(page.receipt_id for page in index.pages)]
    hashes = [page.request_body_hash for page in index.pages]
    if (index.stock_code != STOCK_CODE or index.organization_id != "gssh0600578" or
            list(scope["publicationWindow"]) != [day.isoformat() for day in index.publication_window] or
            [item["receipt_id"] for item in proof["indexReceipts"]] != receipts or
            scope["requestBodyHashes"] != hashes):
        raise RevenueChainContractError("impact proof and normal cross-category index differ")


def _bind_current27_broad_scope(context: ReportReviewContext, *,
                                target_period: date,
                                company: Mapping[str, object]) -> ReportReviewContext:
    """Reuse the current27 all-category pages while rebinding only target metadata.

    current27's one regular index is for H125 and is never used here. The eight
    broad pages cover the full 2024-06-30..2026-10-27 issuer window, so their
    archived responses can be reused for either report after target-specific
    title reasons are recomputed from the saved titles.
    """
    index = context.cross_category_index
    start, end = tuple(date.fromisoformat(str(item)) for item in company["publicationWindow"])
    archived_pages = company.get("allCategoryPages", ())
    page_bindings = [
        (page.page_num, page.receipt_id, page.content_hash, page.request_body_hash)
        for page in index.pages
    ]
    expected_bindings = [
        (int(page["page"]), str(page["responseReceiptId"]),
         str(page["responseContentHash"]), str(page["requestBodyHash"]))
        for page in archived_pages
    ]
    if (index.stock_code != STOCK_CODE or index.organization_id != company.get("organizationId") or
            index.org_lookup_receipt_id != company.get("orgLookupReceiptId") or
            index.org_lookup_content_hash != company.get("orgLookupContentHash") or
            index.publication_window != (start, end) or
            start != date(2024, 6, 30) or end != date(2026, 10, 27) or
            index.report_period not in ("2024-06-30", target_period.isoformat()) or
            not index.complete or index.error is not None or index.termination != "has_more_false" or
            len(index.pages) != 8 or len(archived_pages) != 8 or
            page_bindings != expected_bindings or
            index.total_announcement != company.get("allCategoryTotalAnnouncement") or
            company.get("allCategoryTermination") != "has_more_false"):
        raise RevenueChainContractError("current27 complete broad scope is not bound to archived page receipts")
    period = parse_report_period(target_period.isoformat())
    announcements = tuple(replace(
        entry,
        candidate_reasons=tuple(reason for reason, active in (
            ("period_title", title_matches_period(entry.title, period)),
            ("correction_title", title_revision_flags(entry.title)[0]),
            ("revision_title", title_revision_flags(entry.title)[1]),
            ("supplement_title", title_revision_flags(entry.title)[2]),
            ("accounting_restatement_title", any(term in entry.title for term in (
                "会计差错", "追溯调整", "重述"))),
        ) if active),
    ) for entry in index.announcements)
    rebound = replace(index, report_period=target_period.isoformat(), announcements=announcements)
    return replace(context, cross_category_index=rebound)


def _current_candidate(body: bytes, receipt: Mapping[str, object], *,
                       announcement_id: str, period_end: date):
    candidate = extract_section_facts(
        body, instrument_id=STOCK_CODE, company=COMPANY, period_end=period_end,
        announcement_id=announcement_id, document_url=str(receipt["url"]),
        required_fields=("revenue",),
    )
    verification = verify_machine_evidence(
        body, candidate=candidate, expected_fields=("revenue",),
    )
    if not verification.verified:
        raise RevenueChainContractError(f"{announcement_id} revenue independent verification failed")
    return candidate, verification


def _require_current_cell(candidate, *, announcement_id: str, period_end: date,
                          expected_value: Decimal, expected_page: int, expected_header: str):
    cell = candidate.by_field["revenue"]
    label = (cell.display_row_label or "").replace("：", ":")
    if (candidate.instrument_id != STOCK_CODE or candidate.period_end != period_end or
            candidate.pdf_sha256 == "" or cell.value_yuan != expected_value or
            cell.pdf_page != expected_page or cell.amount_unit != "元" or
            cell.currency != "CNY" or cell.column_header != expected_header or
            not label.replace(" ", "").startswith("其中:营业收入")):
        raise RevenueChainContractError(f"{announcement_id} ordinary CURRENT revenue cell differs")
    return cell


def _adjusted_prior_row(body: bytes):
    sections = extract_statement_sections(
        body, period_end=PRESENTING_PERIOD, roles=(ROLE_CONSOLIDATED,),
        statements=("利润表",),
    )
    section = sections.require(ROLE_CONSOLIDATED, "利润表")
    row = section.find("revenue")
    if (row is None or section.unit != "元" or row.unit != "元" or row.page != 101 or
            section.current_header != "2025年度" or section.prior_header != "2024年度" or
            section.normalize(row.label).replace(" ", "").replace("：", ":") != "其中:营业收入" or
            row.amount != FY25_CURRENT_YUAN or row.prior_amount != ADJUSTED_YUAN or
            row.current_cell is None or row.prior_cell is None):
        raise RevenueChainContractError("FY25 p101 consolidated revenue CURRENT/PRIOR row differs")
    return section, row


def _outcome(proof: Mapping[str, object]) -> Mapping[str, object]:
    outcomes = [item for item in proof.get("outcomes", ())
                if isinstance(item, Mapping) and item.get("metric") == "revenue"]
    if len(outcomes) != 1:
        raise RevenueChainContractError("current impact proof must cover exactly revenue")
    return outcomes[0]


def _bind_changed_pair(original_proof: Mapping[str, object], root_cell, annual_row) -> None:
    outcome = _outcome(original_proof)
    current = outcome.get("currentSource", {})
    comparisons = [item for item in outcome.get("comparisons", ())
                   if isinstance(item, Mapping) and item.get("announcementId") == PRESENTING_ID and
                   item.get("sourceReportPeriodEnd") == PRESENTING_PERIOD.isoformat() and
                   item.get("factPeriodEnd") == ORIGINAL_PERIOD.isoformat() and
                   item.get("columnRole") == "PRIOR"]
    if (outcome.get("status") != "AFFECTS" or outcome.get("admissionStatus") != "NOT_ADMITTED" or
            outcome.get("unresolvedSuccessorIds") != [] or
            outcome.get("missingReportSourceIds") != [] or
            len(outcome.get("comparisons", ())) != 1 or
            current.get("announcementId") != ORIGINAL_ID or
            current.get("sourceReportPeriodEnd") != ORIGINAL_PERIOD.isoformat() or
            current.get("factPeriodEnd") != ORIGINAL_PERIOD.isoformat() or
            current.get("columnRole") != "CURRENT" or
            current.get("statementScope") != "CONSOLIDATED" or
            current.get("unitRaw") != "元" or Decimal(str(current.get("amountYuan"))) != root_cell.value_yuan or
            len(comparisons) != 1):
        raise RevenueChainContractError("FY24 current scope did not replay as the exact changed pair")
    comparison = comparisons[0]
    if (comparison.get("columnHeader") != "2024年度" or comparison.get("page") != annual_row.page or
            comparison.get("unitRaw") != "元" or
            Decimal(str(comparison.get("amountYuan"))) != annual_row.prior_amount or
            comparison.get("sourceLine") != annual_row.raw_line or
            annual_row.prior_amount - root_cell.value_yuan != DELTA_YUAN or
            Decimal(str(comparison["amountYuan"])) - root_cell.value_yuan == 0):
        raise RevenueChainContractError("replayed native FY25 PRIOR row does not prove the saved numeric delta")


def _bind_annual_current(annual_proof: Mapping[str, object], annual_cell) -> None:
    outcome = _outcome(annual_proof)
    current = outcome.get("currentSource", {})
    if (outcome.get("status") not in ("AFFECTS", "NO_EFFECT") or
            outcome.get("admissionStatus") != "NOT_ADMITTED" or
            current.get("announcementId") != PRESENTING_ID or
            current.get("sourceReportPeriodEnd") != PRESENTING_PERIOD.isoformat() or
            current.get("factPeriodEnd") != PRESENTING_PERIOD.isoformat() or
            current.get("columnRole") != "CURRENT" or
            current.get("statementScope") != "CONSOLIDATED" or current.get("unitRaw") != "元" or
            Decimal(str(current.get("amountYuan"))) != annual_cell.value_yuan):
        raise RevenueChainContractError("FY25 normal CURRENT/full-scope context differs")


def write_600578_fy24_revenue_forward_root(
    *, repository: FinancialFactRepository, archive,
    original: ReportReviewContext, annual: ReportReviewContext,
    current_company_descriptor: Mapping[str, object], calendar_evidence: Mapping[str, object],
    rights: RightsRegistry | None = None,
) -> tuple[tuple[FinancialFact, FinancialFact], int]:
    """Re-open, replay, and atomically append the FY24 CURRENT root and FY25 adjusted successor."""
    started = datetime.now(timezone.utc)
    rights_entry = (rights or default_rights()).get("cninfo")
    if not (rights_entry.open_for("research_use") and rights_entry.open_for("local_storage")):
        raise RevenueChainContractError("CNINFO research/storage rights are not open")
    if (original.version_review.announcement_id != ORIGINAL_ID or
            annual.version_review.announcement_id != PRESENTING_ID or
            original.version_review.query_stock_code != STOCK_CODE or
            annual.version_review.query_stock_code != STOCK_CODE or
            original.version_review.query_organization_id != "gssh0600578" or
            annual.version_review.query_organization_id != "gssh0600578" or
            original.version_review.predecessor_announcement_id is not None or
            annual.version_review.predecessor_announcement_id is not None):
        raise RevenueChainContractError("ordinary report review identities do not match the finite pair")
    for context in (original, annual):
        if (not context.version_review.reviewer_id.startswith("machine:") or
                not context.reviewed_by.startswith("machine:") or
                any(not item.reviewer_id.startswith("machine:") for item in context.dispositions)):
            raise RevenueChainContractError("machine context cannot claim a human reviewer")

    company = _company_scope(current_company_descriptor)
    original = _bind_current27_broad_scope(
        original, target_period=ORIGINAL_PERIOD, company=company,
    )
    annual = _bind_current27_broad_scope(
        annual, target_period=PRESENTING_PERIOD, company=company,
    )
    original_record = _report_record(company, ORIGINAL_ID)
    annual_record = _report_record(company, PRESENTING_ID)
    _validate_company_report(
        original_record, announcement_id=ORIGINAL_ID, period_end=ORIGINAL_PERIOD,
        title="京能电力：2024年年度报告",
        url="https://static.cninfo.com.cn/finalpage/2025-04-26/1223304346.PDF",
    )
    _validate_company_report(
        annual_record, announcement_id=PRESENTING_ID, period_end=PRESENTING_PERIOD,
        title="京能电力：2025年年度报告",
        url="https://static.cninfo.com.cn/finalpage/2026-04-25/1225184640.PDF",
    )
    if (original_record["receipt"]["receipt_id"] != ORIGINAL_RECEIPT_ID or
            annual_record["receipt"]["receipt_id"] != PRESENTING_RECEIPT_ID or
            original.pdf_receipt_id != original_record["receipt"]["receipt_id"] or
            annual.pdf_receipt_id != annual_record["receipt"]["receipt_id"]):
        raise RevenueChainContractError("ordinary source contexts do not use the current descriptor PDF receipts")

    original_receipt, original_body = _archived_ok(
        archive, original.pdf_receipt_id, expected_url=original_record["documentUrl"],
        expected_host="static.cninfo.com.cn",
    )
    annual_receipt, annual_body = _archived_ok(
        archive, annual.pdf_receipt_id, expected_url=annual_record["documentUrl"],
        expected_host="static.cninfo.com.cn",
    )
    if (original_receipt["content_hash"] != original_record["contentHash"] or
            original_receipt["content_hash"] != "sha256:8681e1bd23a5e5d91d8ed07ca7acbc7da7be14ebcfea6f0860bed43d623adfe3" or
            annual_receipt["content_hash"] != annual_record["contentHash"] or
            annual_receipt["content_hash"] != "sha256:e25638741dfaef06a80844a646e6329945040d6fbd758fc157a429a6920a6a7d"):
        raise RevenueChainContractError("FY24/FY25 archived PDF receipts or content hashes differ")

    original_candidate, original_verification = _current_candidate(
        original_body, original_receipt, announcement_id=ORIGINAL_ID, period_end=ORIGINAL_PERIOD,
    )
    original_cell = _require_current_cell(
        original_candidate, announcement_id=ORIGINAL_ID, period_end=ORIGINAL_PERIOD,
        expected_value=ORIGINAL_YUAN, expected_page=99, expected_header="2024年度",
    )
    annual_candidate, annual_verification = _current_candidate(
        annual_body, annual_receipt, announcement_id=PRESENTING_ID, period_end=PRESENTING_PERIOD,
    )
    annual_cell = _require_current_cell(
        annual_candidate, announcement_id=PRESENTING_ID, period_end=PRESENTING_PERIOD,
        expected_value=FY25_CURRENT_YUAN, expected_page=101, expected_header="2025年度",
    )
    annual_section, annual_row = _adjusted_prior_row(annual_body)
    if annual_row.amount != annual_cell.value_yuan:
        raise RevenueChainContractError("FY25 ordinary CURRENT and p101 row CURRENT differ")

    original_impact = _replay_impact(
        original, archive=archive, company=company, target_period=ORIGINAL_PERIOD,
    )
    annual_impact = _replay_impact(
        annual, archive=archive, company=company, target_period=PRESENTING_PERIOD,
    )
    _match_impact_indexes(original_impact, original)
    _match_impact_indexes(annual_impact, annual)
    _bind_changed_pair(original_impact, original_cell, annual_row)
    _bind_annual_current(annual_impact, annual_cell)

    original_published, original_role, original_indexes, original_hashes = _version_evidence(
        archive, original_candidate, original.version_review, now=started,
    )
    annual_published, annual_role, annual_indexes, annual_hashes = _version_evidence(
        archive, annual_candidate, annual.version_review, now=started,
    )
    if original_role.value != "ORIGINAL_REPORT" or annual_role.value != "ORIGINAL_REPORT":
        raise RevenueChainContractError("both chain sources must be original report versions")
    original_cross = _cross_category_evidence(
        archive, original_candidate, original.cross_category_index, original.dispositions,
        review=original.version_review, reviewed_by=original.reviewed_by,
        reviewed_at=original.reviewed_at, pdf_receipt_id=original.pdf_receipt_id,
        now=started, revision_impact=original_impact,
    )
    annual_cross = _cross_category_evidence(
        archive, annual_candidate, annual.cross_category_index, annual.dispositions,
        review=annual.version_review, reviewed_by=annual.reviewed_by,
        reviewed_at=annual.reviewed_at, pdf_receipt_id=annual.pdf_receipt_id,
        now=started, revision_impact=annual_impact,
    )
    o_cross_receipts, o_cross_hashes, o_dispositions = original_cross
    a_cross_receipts, a_cross_hashes, a_dispositions = annual_cross

    from .s2_calendar_evidence import verify_calendar_evidence
    calendar_checked_at = datetime.now(timezone.utc)
    trading_calendar, verified_calendar = verify_calendar_evidence(
        dict(calendar_evidence), archive, now=calendar_checked_at,
        published_on=min(original_published, annual_published),
    )
    completed = datetime.now(timezone.utc)
    evidence_times = [
        calendar_checked_at, original.version_review.reviewed_at, annual.version_review.reviewed_at,
        original.reviewed_at, annual.reviewed_at,
        datetime.fromisoformat(original_receipt["first_seen_at"]),
        datetime.fromisoformat(annual_receipt["first_seen_at"]),
        datetime.fromisoformat(original_impact["completedAt"]),
        datetime.fromisoformat(annual_impact["completedAt"]),
    ]
    evidence_times.extend(datetime.fromisoformat(row["first_seen_at"])
                          for row in (*original_indexes, *annual_indexes,
                                      *o_cross_receipts, *a_cross_receipts))
    for proof in (original_impact, annual_impact):
        evidence_times.extend(datetime.fromisoformat(item["first_seen_at"])
                              for item in proof["indexReceipts"])
        evidence_times.extend(datetime.fromisoformat(item["receipt"]["first_seen_at"])
                              for item in proof["sources"])
    def calendar_times(value):
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key == "first_seen_at" and isinstance(child, str):
                    yield datetime.fromisoformat(child)
                else:
                    yield from calendar_times(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                yield from calendar_times(child)
    evidence_times.extend(calendar_times(verified_calendar))
    if (any(item.tzinfo is None or item > completed for item in evidence_times) or
            any(item > completed for item in (original.reviewed_at, annual.reviewed_at))):
        raise RevenueChainContractError("source/review evidence is later than proof completion")

    original_floor, _ = date_only_available_at(original_published, trading_calendar)
    annual_floor, _ = date_only_available_at(annual_published, trading_calendar)
    available = _next_available_snapshot(
        max(original_floor, annual_floor, completed, *evidence_times), trading_calendar,
    )
    original_hash, annual_hash = original_candidate.pdf_sha256, annual_candidate.pdf_sha256
    root_id = _version_id("cninfo", STOCK_CODE, ORIGINAL_PERIOD.isoformat(), "revenue",
                          ORIGINAL_ID, original_hash)
    adjusted_id = _version_id("cninfo", STOCK_CODE, ORIGINAL_PERIOD.isoformat(), "revenue",
                              PRESENTING_ID, annual_hash, "PRIOR_ADJUSTED", root_id)
    common = dict(
        instrument_id=STOCK_CODE, metric="revenue", period_end=ORIGINAL_PERIOD,
        statement_scope=StatementScope.CONSOLIDATED, profit_scope=None,
        currency="CNY", raw_unit="yuan", source_id="cninfo", source_published_at=None,
        timestamp_precision=TimestampPrecision.DATE, ingested_at=completed,
        available_at=available, availability_basis=AvailabilityBasis.OBSERVED,
        pit_mode=PitMode.LIVE_OBSERVED,
    )
    root = FinancialFact(
        **common, value=original_cell.value_yuan, source_document_id=ORIGINAL_ID,
        source_published_date=original_published,
        first_seen_at=datetime.fromisoformat(original_receipt["first_seen_at"]),
        content_hash=original_hash, version_id=root_id,
    )
    adjusted = FinancialFact(
        **common, value=annual_row.prior_amount, source_document_id=PRESENTING_ID,
        source_published_date=annual_published,
        first_seen_at=datetime.fromisoformat(annual_receipt["first_seen_at"]),
        content_hash=annual_hash, version_id=adjusted_id, supersedes_id=root_id,
    )

    # A real root must be created by this atomic batch. An existing version of
    # this logical fact needs separate lineage inspection; it cannot be reused
    # or silently become a root here.
    if any(fact.logical_key == root.logical_key for fact in repository.load().facts):
        raise RevenueChainContractError("FY24 revenue already has a stored version; root creation is not safe")

    review = {
        "schema": SCHEMA, "source_id": "cninfo", "instrument_id": STOCK_CODE,
        "period_end": ORIGINAL_PERIOD.isoformat(), "announcement_id": PRESENTING_ID,
        "rights_register_version": REGISTER_VERSION,
        "verifier_id": "machine:cninfo-600578-fy24-revenue-chain-v1",
        "verifier_version": "1", "human_review_claimed": None,
        "completed_at": completed.isoformat(), "available_at": available.isoformat(),
        "fact_version_ids": [root_id, adjusted_id],
        "candidate_disposition": "AFFECTS_VERSION_CHAIN",
        "formal_admission": "NOT_ADMITTED",
        "delta_adjusted_prior_minus_original_current_yuan": str(annual_row.prior_amount - original_cell.value_yuan),
        "current_issuer_scope": company,
        "scope_archive": _scope_archive(archive, company),
        "original_current": {
            "versionId": root_id, "valueYuan": str(root.value),
            "contentHash": original_hash, "receiptId": original_receipt["receipt_id"],
            "reportTitle": original_record["title"],
            "sourceReportPeriodEnd": ORIGINAL_PERIOD.isoformat(),
            "factPeriodEnd": ORIGINAL_PERIOD.isoformat(), "columnRole": "CURRENT",
            "sourcePublishedDate": original_published.isoformat(),
            "firstSeenAt": root.first_seen_at.isoformat(),
            "normalCandidate": _candidate_evidence(original_candidate),
            "normalVerification": original_verification.as_evidence(),
            "versionReview": _review_fields(original.version_review),
            "indexReceiptIds": [row["receipt_id"] for row in original_indexes],
            "indexRequestBodyHashes": original_hashes,
            "crossCategoryReceiptIds": [row["receipt_id"] for row in o_cross_receipts],
            "crossCategoryRequestBodyHashes": o_cross_hashes,
            "crossCategoryDispositions": o_dispositions,
            "crossCategoryIndex": _cross_index_fields(original.cross_category_index),
            "crossCategoryDispositionInputs": _disposition_inputs(original.dispositions),
            "crossCategoryReviewedBy": original.reviewed_by,
            "crossCategoryReviewedAt": original.reviewed_at.isoformat(),
            "revisionImpactProof": original_impact,
        },
        "adjusted_prior": {
            "versionId": adjusted_id, "valueYuan": str(adjusted.value),
            "contentHash": annual_hash, "receiptId": annual_receipt["receipt_id"],
            "reportTitle": annual_record["title"],
            "sourceReportPeriodEnd": PRESENTING_PERIOD.isoformat(),
            "factPeriodEnd": ORIGINAL_PERIOD.isoformat(), "columnRole": "PRIOR_ADJUSTED",
            "page": annual_row.page, "statement": annual_row.statement,
            "statementScope": "CONSOLIDATED", "rowLabel": annual_row.label,
            "unit": annual_section.unit, "currentHeader": annual_section.current_header,
            "priorHeader": annual_section.prior_header,
            "currentCell": annual_row.current_cell, "priorCell": annual_row.prior_cell,
            "currentValueYuan": str(annual_row.amount), "rawLine": annual_row.raw_line,
            "unadjustedPriorYuan": str(original_cell.value_yuan),
            "sourcePublishedDate": annual_published.isoformat(),
            "firstSeenAt": adjusted.first_seen_at.isoformat(),
            "normalCandidate": _candidate_evidence(annual_candidate),
            "normalVerification": annual_verification.as_evidence(),
            "versionReview": _review_fields(annual.version_review),
            "indexReceiptIds": [row["receipt_id"] for row in annual_indexes],
            "indexRequestBodyHashes": annual_hashes,
            "crossCategoryReceiptIds": [row["receipt_id"] for row in a_cross_receipts],
            "crossCategoryRequestBodyHashes": a_cross_hashes,
            "crossCategoryDispositions": a_dispositions,
            "crossCategoryIndex": _cross_index_fields(annual.cross_category_index),
            "crossCategoryDispositionInputs": _disposition_inputs(annual.dispositions),
            "crossCategoryReviewedBy": annual.reviewed_by,
            "crossCategoryReviewedAt": annual.reviewed_at.isoformat(),
            "revisionImpactProof": annual_impact,
        },
        "calendar_evidence": verified_calendar,
        "calendar_checked_at": calendar_checked_at.isoformat(),
        "trading_calendar": [day.isoformat() for day in trading_calendar],
    }
    bundle_id = REVIEW_PREFIX + hashlib.sha256(
        f"{SCHEMA}|{STOCK_CODE}|{ORIGINAL_PERIOD.isoformat()}|{root_id}|{adjusted_id}".encode()
    ).hexdigest()[:32]
    bundle = FactReviewBundle(bundle_id, _canonical(review))
    added = repository.append_many((root, adjusted), review_bundle=bundle)
    return (root, adjusted), added


__all__ = [
    "SCHEMA", "REVIEW_PREFIX", "ORIGINAL_ID", "PRESENTING_ID", "ORIGINAL_PERIOD",
    "PRESENTING_PERIOD", "COMPANY", "ORIGINAL_RECEIPT_ID", "PRESENTING_RECEIPT_ID",
    "RevenueChainContractError", "ReportReviewContext",
    "_canonical", "_candidate_evidence", "_candidate_evidence_matches",
    "_cross_index_fields", "_disposition_inputs", "_review_fields",
    "_version_id", "write_600578_fy24_revenue_forward_root",
]
