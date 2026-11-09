"""Finite forward-root writer candidate for 601069 FY24 revenue.

This candidate atomically stores the original FY24 CURRENT and the FY25
report's FY24 PRIOR_ADJUSTED as two FinancialFact versions. It is not an
admission or production integration.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from ...domain.data.pit import AvailabilityBasis, PitMode, TimestampPrecision, date_only_available_at
from ...domain.data.rights import REGISTER_VERSION, RightsRegistry, default_rights
from ...domain.fundamentals.fact_repository import FactReviewBundle, FinancialFactRepository
from ...domain.fundamentals.versioned import FinancialFact, StatementScope
from . import s2_revision_impact as impact
from .annual_comparative_revenue import (
    existing_current_from_source19, produce_601069_fy24_revenue_candidate,
)
from .cninfo import CrossCategoryIndexResult
from .cninfo_statement_sections import extract_section_facts
from .pdf_promotion import (
    CrossCategoryDisposition, PdfVersionReview, _archived_ok,
    _cross_category_evidence, _next_available_snapshot, _version_evidence,
)
from .s2_machine_verification import verify_machine_evidence


SCHEMA = "aquant.cninfo.601069-fy24-revenue-forward-root-v1"
REVIEW_PREFIX = "cninfo_machine_revenue_chain_"
ORIGINAL_ID, PRESENTING_ID = "1222983253", "1225082554"
ORIGINAL_PERIOD, PRESENTING_PERIOD = date(2024, 12, 31), date(2025, 12, 31)
COMPANY = "西部黄金股份有限公司"


class RevenueChainContractError(ValueError):
    """The finite original-to-adjusted source relation is incomplete."""


@dataclass(frozen=True, slots=True)
class ReportReviewContext:
    pdf_receipt_id: str
    version_review: PdfVersionReview
    cross_category_index: CrossCategoryIndexResult
    dispositions: tuple[CrossCategoryDisposition, ...]
    reviewed_by: str
    reviewed_at: datetime


def _canonical(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
        "stockCode": index.stock_code, "organizationId": index.organization_id,
        "reportPeriod": index.report_period,
        "publicationWindow": [day.isoformat() for day in index.publication_window],
        "orgLookupReceiptId": index.org_lookup_receipt_id,
        "orgLookupContentHash": index.org_lookup_content_hash,
        "pages": [{"pageNum": page.page_num, "requestBodyHash": page.request_body_hash,
                   "receiptId": page.receipt_id, "contentHash": page.content_hash,
                   "httpStatus": page.http_status,
                   "rawAnnouncementCount": page.raw_announcement_count,
                   "totalAnnouncement": page.total_announcement,
                   "hasMore": page.has_more} for page in index.pages],
        "announcements": [{"announcementId": item.announcement_id,
                           "secCode": item.sec_code, "title": item.title,
                           "announcementTimeMs": item.announcement_time_ms,
                           "documentUrl": item.document_url,
                           "candidateReasons": list(item.candidate_reasons),
                           "pageNum": item.page_num,
                           "pageReceiptId": item.page_receipt_id,
                           "pageContentHash": item.page_content_hash}
                          for item in index.announcements],
        "skippedDifferentSecurity": index.skipped_different_security,
        "totalAnnouncement": index.total_announcement, "complete": index.complete,
        "termination": index.termination, "error": index.error,
    }


def _disposition_inputs(items: tuple[CrossCategoryDisposition, ...]) -> list[dict[str, object]]:
    return [{"announcementId": item.announcement_id, "relevance": item.relevance,
             "reviewerId": item.reviewer_id, "reviewedAt": item.reviewed_at.isoformat(),
             "rationale": item.rationale, "documentReceiptId": item.document_receipt_id,
             "relatedReportAnnouncementIds": list(item.related_report_announcement_ids),
             "relationReceiptId": item.relation_receipt_id,
             "relationExcerpt": item.relation_excerpt} for item in items]


def _accepted_annual_dependency(repository: FinancialFactRepository,
                                adjusted_prior: Decimal) -> tuple[FinancialFact, dict]:
    """Resolve one report cohort through the ordinary reader's validators."""
    from ...domain.fundamentals.fact_repository import (
        _accepted_machine_verified_review, _accepted_review_group_links,
        _accepted_s2_pdf_review, _decode,
    )

    con = sqlite3.connect(Path(repository.db_path).resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        fact_rows = con.execute(
            "SELECT * FROM financial_fact WHERE instrument_id='601069' "
            "AND period_end='2025-12-31' AND source_id='cninfo' AND source_document_id=?",
            (PRESENTING_ID,),
        ).fetchall()
        facts = {fact.version_id: fact for fact in (_decode(item) for item in fact_rows)}
        review_rows = con.execute(
            "SELECT DISTINCT r.* FROM financial_fact_review r "
            "JOIN financial_fact_review_member m ON m.bundle_id=r.bundle_id "
            "JOIN financial_fact f ON f.version_id=m.version_id "
            "WHERE f.instrument_id='601069' AND f.period_end='2025-12-31' "
            "AND f.source_id='cninfo' AND f.source_document_id=?",
            (PRESENTING_ID,),
        ).fetchall()
        valid_reviews = []
        for review_row in review_rows:
            members = {item[0] for item in con.execute(
                "SELECT version_id FROM financial_fact_review_member WHERE bundle_id=?",
                (review_row["bundle_id"],),
            )}
            if (_accepted_s2_pdf_review(review_row, members, facts) or
                    _accepted_machine_verified_review(review_row, members, facts)):
                valid_reviews.append((review_row, members))
        accepted = _accepted_review_group_links(
            ("601069", PRESENTING_PERIOD.isoformat(), PRESENTING_ID), valid_reviews, facts)
        if accepted is None:
            raise RevenueChainContractError("FY25 annual cohort has no ordinary accepted review links")
        matches = [fact for fact in facts.values() if fact.metric == "revenue" and
                   fact.period_end == PRESENTING_PERIOD and fact.version_id in accepted]
        if len(matches) != 1:
            raise RevenueChainContractError("accepted FY25 annual revenue is not unique")
        fact = matches[0]
        link = accepted[fact.version_id]
        if (not link[0].startswith("cninfo_machine_fields_") or
                fact.source_id != "cninfo" or fact.currency != "CNY" or fact.raw_unit != "yuan" or
                fact.statement_scope.value != "CONSOLIDATED" or fact.profit_scope is not None):
            raise RevenueChainContractError("FY25 CURRENT lacks its ordinary accepted machine link")
        row = next((item for item in review_rows if item["bundle_id"] == link[0]), None)
        if con.total_changes:
            raise RevenueChainContractError("annual dependency lookup changed the warehouse")
    finally:
        if con.in_transaction:
            con.execute("ROLLBACK")
        con.close()
    if row is None or row["evidence_hash"] != link[1]:
        raise RevenueChainContractError("reader link does not match the immutable review row")
    evidence = json.loads(row["evidence_json"])
    digest = "sha256:" + hashlib.sha256(row["evidence_json"].encode()).hexdigest()
    if (not isinstance(evidence, dict) or
            json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")) != row["evidence_json"] or
            digest != link[1] or evidence.get("schema") != "cninfo-s2-machine-verified-v1" or
            evidence.get("instrument_id") != "601069" or
            evidence.get("announcement_id") != PRESENTING_ID or
            evidence.get("period_end") != PRESENTING_PERIOD.isoformat() or
            fact.version_id not in evidence.get("version_ids", ())):
        raise RevenueChainContractError("accepted annual row is not the linked immutable review")
    proof = evidence.get("revision_impact_proof", {})
    outcomes = [item for item in proof.get("outcomes", ())
                if isinstance(item, Mapping) and item.get("metric") == "revenue"]
    scope = proof.get("relationshipScope", {})
    if len(outcomes) != 1:
        raise RevenueChainContractError("annual dependency lacks one revenue impact proof")
    outcome, current = outcomes[0], outcomes[0].get("currentSource", {})
    if (outcome.get("status") != "NO_EFFECT" or
            outcome.get("admissionStatus") != "NOT_ADMITTED" or
            current.get("announcementId") != PRESENTING_ID or
            current.get("factPeriodEnd") != PRESENTING_PERIOD.isoformat() or
            current.get("sourceReportPeriodEnd") != PRESENTING_PERIOD.isoformat() or
            current.get("columnRole") != "CURRENT" or
            Decimal(str(current.get("priorAmountYuan"))) != adjusted_prior or
            Decimal(str(current.get("amountYuan"))) != Decimal(str(fact.value)) or
            not scope.get("completeForArchivedIndexResponse") or
            scope.get("missingReportSourceIds") != []):
        raise RevenueChainContractError("annual proof does not bind FY25 CURRENT and FY24 adjusted prior")
    return fact, {
        "versionId": fact.version_id, "bundleId": link[0], "evidenceHash": link[1],
        "valueYuan": str(fact.value), "availableAt": fact.available_at.isoformat(),
        "sourceDocumentId": fact.source_document_id,
        "revisionImpactProof": proof,
        "revisionImpactArchive": evidence.get("revision_impact_archive"),
    }


def check_601069_current_relationship_scope(archive, company: Mapping[str, object],
                                             annual_proof: Mapping[str, object], *,
                                             normal_indexes: tuple[CrossCategoryIndexResult, ...]) -> dict:
    """Re-dispatch old proof scope and bind fresh captures to both normal indexes."""
    from ...domain.fundamentals.disclosure_link import DisclosureRole, disclosure_role
    from .cninfo_probe import normalize_title, title_revision_flags

    if company.get("code") != "601069" or company.get("instrumentId") != "SH.601069":
        raise RevenueChainContractError("relationship descriptor issuer differs")
    entries, receipts, request_hashes = impact._index(archive.con, archive.root, company)
    old = annual_proof["relationshipScope"]
    periods = {
        aid: parsed.isoformat() for aid, entry in entries.items()
        if (parsed := impact._report_period(entry["title"])) is not None and
        parsed.isoformat() in impact.PERIODS and
        disclosure_role(entry["title"]) in (DisclosureRole.ORIGINAL_REPORT, DisclosureRole.REVISED_REPORT) and
        not impact._601069_is_annual_reporting_policy(entry["title"]) and
        not impact._601069_is_delayed_disclosure_notice(entry["title"])
    }
    causes = {aid for aid, entry in entries.items() if impact._financial_title(entry["title"]) and
              not impact._601069_is_annual_reporting_policy(entry["title"]) and
              not impact._601069_is_delayed_disclosure_notice(entry["title"])}
    recalled = {aid for aid, entry in entries.items()
                if aid in causes or any(title_revision_flags(entry["title"])[:3])}
    if (periods != old.get("reportCandidatePeriods") or
            causes != set(old.get("financialCauseIds", ())) or
            set(periods) | causes != set(old.get("financialCandidateIds", ())) or
            recalled != set(old.get("allRecalledRevisionIds", ())) or
            old.get("missingReportSourceIds") != [] or
            not old.get("completeForArchivedIndexResponse")):
        raise RevenueChainContractError("fresh index has an extra, missing, or conflicting financial item")
    documents = [*company.get("selectedReportRecords", ()), *company.get("relatedDocumentRecords", ())]
    by_id = {str(item["announcementId"]): item for item in documents}
    originals = {str(item["announcementId"]): item for item in annual_proof["sources"]}
    if (len(by_id) != len(documents) or not recalled <= by_id.keys() or
            not set(periods) <= by_id.keys() or not by_id.keys() <= originals.keys() or
            {str(item["announcementId"]) for item in company["selectedReportRecords"]} !=
            set(old.get("selectedReportIds", ()))):
        raise RevenueChainContractError("fresh original-source records are incomplete")
    bindings = []
    for aid, document in sorted(by_id.items()):
        entry, prior = entries.get(aid), originals[aid]
        if (entry is None or document.get("code") != "601069" or
                document.get("instrumentId") != "SH.601069" or
                document.get("documentUrl") != entry["documentUrl"] or
                normalize_title(document["title"]) != entry["title"] or
                entry["documentUrl"] != prior["index"]["documentUrl"] or
                entry["publishedDate"] != prior["index"]["publishedDate"] or
                entry["title"] != prior["index"]["title"]):
            raise RevenueChainContractError("fresh source identity differs from annual proof")
        receipt, body = _archived_ok(archive, document["receipt"]["receipt_id"],
                                     expected_url=entry["documentUrl"],
                                     expected_host="static.cninfo.com.cn")
        if (receipt != prior["receipt"] or document.get("contentHash") != prior["originalHash"] or
                receipt["content_hash"] != prior["originalHash"] or
                len(body) != prior["originalBytes"] or not body.startswith(b"%PDF-")):
            raise RevenueChainContractError("fresh source bytes differ from annual proof")
        bindings.append({"announcementId": aid, "receiptId": receipt["receipt_id"],
                         "contentHash": receipt["content_hash"],
                         "firstSeenAt": receipt["first_seen_at"]})
    fresh_scope = {"reportCandidatePeriods": periods, "financialCauseIds": sorted(causes),
                   "allRecalledRevisionIds": sorted(recalled),
                   "requestBodyHashes": request_hashes,
                   "indexReceiptIds": [row["receipt_id"] for row in receipts],
                   "indexReceiptFirstSeenAt": [row["first_seen_at"] for row in receipts],
                   "publicationWindow": list(company.get("publicationWindow", ())),
                   "originalSources": bindings}
    if len(normal_indexes) != 2:
        raise RevenueChainContractError("both current normal report indexes are required for fresh-scope binding")
    for index in normal_indexes:
        expected_receipts = [index.org_lookup_receipt_id, *(page.receipt_id for page in index.pages)]
        expected_hashes = [page.request_body_hash for page in index.pages]
        expected_window = [day.isoformat() for day in index.publication_window]
        if (fresh_scope["indexReceiptIds"] != expected_receipts or
                fresh_scope["requestBodyHashes"] != expected_hashes or
                fresh_scope["publicationWindow"] != expected_window):
            raise RevenueChainContractError(
                "fresh relationship descriptor differs from a current normal cross-category index")
    return fresh_scope


def write_601069_fy24_revenue_forward_root(
    *, repository: FinancialFactRepository, archive,
    source19_checkpoint: Mapping[str, object], original: ReportReviewContext,
    annual: ReportReviewContext, current_company_descriptor: Mapping[str, object],
    calendar_evidence: Mapping[str, object],
    rights: RightsRegistry | None = None,
) -> tuple[tuple[FinancialFact, FinancialFact], int]:
    """Re-derive, then atomically append the CURRENT root and adjusted version."""
    from .pdf_promotion import _next_available_snapshot

    started = datetime.now(timezone.utc)
    rights_entry = (rights or default_rights()).get("cninfo")
    if not (rights_entry.open_for("research_use") and rights_entry.open_for("local_storage")):
        raise RevenueChainContractError("CNINFO research/storage rights are not open")
    if (original.version_review.announcement_id != ORIGINAL_ID or
            annual.version_review.announcement_id != PRESENTING_ID or
            original.version_review.query_stock_code != "601069" or
            annual.version_review.query_stock_code != "601069" or
            original.version_review.predecessor_announcement_id is not None or
            annual.version_review.predecessor_announcement_id is not None):
        raise RevenueChainContractError("ordinary report-version reviews have the wrong identity")
    for context in (original, annual):
        if (not context.version_review.reviewer_id.startswith("machine:") or
                not context.reviewed_by.startswith("machine:") or
                any(not item.reviewer_id.startswith("machine:") for item in context.dispositions)):
            raise RevenueChainContractError("machine review evidence cannot claim a human identity")
    original_receipt, original_body = _archived_ok(
        archive, original.pdf_receipt_id, expected_host="static.cninfo.com.cn")
    annual_receipt, annual_body = _archived_ok(
        archive, annual.pdf_receipt_id, expected_host="static.cninfo.com.cn")
    report19 = source19_checkpoint.get("report", {})
    if (report19.get("announcementId") != ORIGINAL_ID or
            report19.get("contentHash") != original_receipt["content_hash"]):
        raise RevenueChainContractError("qualified source19 checkpoint differs from the original receipt")
    old = existing_current_from_source19(source19_checkpoint)
    original_candidate = extract_section_facts(
        original_body, instrument_id="601069", company=COMPANY, period_end=ORIGINAL_PERIOD,
        announcement_id=ORIGINAL_ID, document_url=original_receipt["url"],
        required_fields=("revenue",))
    original_verification = verify_machine_evidence(
        original_body, candidate=original_candidate, expected_fields=("revenue",))
    original_cell = original_candidate.by_field["revenue"]
    checkpoint_candidate = report19["candidateEvidence"]["candidate"]
    if (not original_verification.verified or
            original_candidate.pdf_sha256 != checkpoint_candidate["pdf_sha256"] or
            original_cell.value_yuan != old.amount_yuan or original_cell.pdf_page != 99 or
            original_cell.amount_unit != "元" or original_cell.currency != "CNY"):
        raise RevenueChainContractError("normal FY24 CURRENT re-extraction/strict verification differs")
    annual_document = {"metadata": {"announcementId": PRESENTING_ID, "code": "601069",
                                     "periodEnd": PRESENTING_PERIOD.isoformat(),
                                     "title": COMPANY + "2025年年度报告"}, "body": annual_body}
    candidate = produce_601069_fy24_revenue_candidate(source19_checkpoint, annual_document)
    annual_candidate = extract_section_facts(
        annual_body, instrument_id="601069", company=COMPANY, period_end=PRESENTING_PERIOD,
        announcement_id=PRESENTING_ID, document_url=annual_receipt["url"],
        required_fields=("revenue",))
    annual_verification = verify_machine_evidence(
        annual_body, candidate=annual_candidate, expected_fields=("revenue",))
    if (not annual_verification.verified or candidate.delta_yuan == 0 or
            annual_candidate.by_field["revenue"].value_yuan != candidate.current_report_yuan):
        raise RevenueChainContractError("FY25 CURRENT or FY24 adjusted comparative failed normal recheck")
    annual_fact, dependency = _accepted_annual_dependency(repository, candidate.adjusted_prior_yuan)
    proof = dependency["revisionImpactProof"]
    if not isinstance(dependency["revisionImpactArchive"], Mapping):
        raise RevenueChainContractError("accepted annual review lacks its original impact archive")
    fresh_scope = check_601069_current_relationship_scope(
        archive, current_company_descriptor, proof,
        normal_indexes=(original.cross_category_index, annual.cross_category_index))
    original_pub, original_role, original_indexes, original_index_hashes = _version_evidence(
        archive, original_candidate, original.version_review, now=started)
    annual_pub, annual_role, annual_indexes, annual_index_hashes = _version_evidence(
        archive, annual_candidate, annual.version_review, now=started)
    if original_role.value != "ORIGINAL_REPORT" or annual_role.value != "ORIGINAL_REPORT":
        raise RevenueChainContractError("forward-root sources must be original reports")
    original_cross = _cross_category_evidence(
        archive, original_candidate, original.cross_category_index, original.dispositions,
        review=original.version_review, reviewed_by=original.reviewed_by,
        reviewed_at=original.reviewed_at, pdf_receipt_id=original.pdf_receipt_id, now=started)
    annual_cross = _cross_category_evidence(
        archive, annual_candidate, annual.cross_category_index, annual.dispositions,
        review=annual.version_review, reviewed_by=annual.reviewed_by,
        reviewed_at=annual.reviewed_at, pdf_receipt_id=annual.pdf_receipt_id,
        now=started, revision_impact=proof)
    o_cross_receipts, o_cross_hashes, o_dispositions = original_cross
    a_cross_receipts, a_cross_hashes, a_dispositions = annual_cross
    from .s2_calendar_evidence import verify_calendar_evidence
    calendar_checked_at = datetime.now(timezone.utc)
    trading_calendar, verified_calendar = verify_calendar_evidence(
        dict(calendar_evidence), archive, now=calendar_checked_at,
        published_on=min(original_pub, annual_pub))
    completed = datetime.now(timezone.utc)
    observations = [calendar_checked_at,
                    datetime.fromisoformat(original_receipt["first_seen_at"]),
                    datetime.fromisoformat(annual_receipt["first_seen_at"]),
                    original.version_review.reviewed_at, annual.version_review.reviewed_at,
                    original.reviewed_at, annual.reviewed_at, annual_fact.ingested_at,
                    datetime.fromisoformat(proof["completedAt"])]
    observations.extend(datetime.fromisoformat(row["first_seen_at"])
                        for row in (*original_indexes, *annual_indexes, *o_cross_receipts,
                                    *a_cross_receipts))
    observations.extend(datetime.fromisoformat(value)
                        for value in fresh_scope["indexReceiptFirstSeenAt"])
    observations.extend(datetime.fromisoformat(source["firstSeenAt"])
                        for source in fresh_scope["originalSources"])
    def calendar_times(value):
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key == "first_seen_at" and isinstance(child, str):
                    yield datetime.fromisoformat(child)
                else:
                    yield from calendar_times(child)
        elif isinstance(value, list):
            for child in value:
                yield from calendar_times(child)
    observations.extend(calendar_times(verified_calendar))
    if any(item.tzinfo is None or item > completed for item in observations):
        raise RevenueChainContractError("a receipt or review is later than proof completion")
    original_floor, _ = date_only_available_at(original_pub, trading_calendar)
    annual_floor, _ = date_only_available_at(annual_pub, trading_calendar)
    available = _next_available_snapshot(max(
        original_floor, annual_floor, annual_fact.available_at, completed, *observations,
    ), trading_calendar)
    original_hash, annual_hash = original_candidate.pdf_sha256, annual_candidate.pdf_sha256
    root_id = _version_id("cninfo", "601069", "2024-12-31", "revenue", ORIGINAL_ID, original_hash)
    adjusted_id = _version_id("cninfo", "601069", "2024-12-31", "revenue", PRESENTING_ID,
                              annual_hash, "PRIOR_ADJUSTED", root_id)
    common = dict(
        instrument_id="601069", metric="revenue", period_end=ORIGINAL_PERIOD,
        statement_scope=StatementScope.CONSOLIDATED, profit_scope=None,
        currency="CNY", raw_unit="yuan", source_id="cninfo", source_published_at=None,
        timestamp_precision=TimestampPrecision.DATE, ingested_at=completed,
        available_at=available, availability_basis=AvailabilityBasis.OBSERVED,
        pit_mode=PitMode.LIVE_OBSERVED,
    )
    root = FinancialFact(
        **common, value=original_cell.value_yuan, source_document_id=ORIGINAL_ID,
        source_published_date=original_pub,
        first_seen_at=datetime.fromisoformat(original_receipt["first_seen_at"]),
        content_hash=original_hash, version_id=root_id,
    )
    adjusted = FinancialFact(
        **common, value=candidate.adjusted_prior_yuan, source_document_id=PRESENTING_ID,
        source_published_date=annual_pub,
        first_seen_at=datetime.fromisoformat(annual_receipt["first_seen_at"]),
        content_hash=annual_hash, version_id=adjusted_id, supersedes_id=root_id,
    )
    review = {
        "schema": SCHEMA, "source_id": "cninfo", "instrument_id": "601069",
        "period_end": "2024-12-31", "announcement_id": PRESENTING_ID,
        "rights_register_version": REGISTER_VERSION,
        "verifier_id": "machine:cninfo-601069-fy24-revenue-chain-v1",
        "verifier_version": "1", "human_review_claimed": None,
        "completed_at": completed.isoformat(), "available_at": available.isoformat(),
        "fact_version_ids": [root_id, adjusted_id],
        "candidate": candidate.as_dict(), "candidate_disposition": "AFFECTS_VERSION_CHAIN",
        "delta_adjusted_prior_minus_original_current_yuan": str(candidate.delta_yuan),
        "original_current": {"versionId": root_id, "valueYuan": str(root.value),
            "contentHash": original_hash, "receiptId": original_receipt["receipt_id"],
            "reportTitle": report19.get("title"), "reportPeriodEnd": "2024-12-31",
            "sourcePublishedDate": original_pub.isoformat(),
            "firstSeenAt": root.first_seen_at.isoformat(),
            "normalVerification": original_verification.as_evidence(),
            "versionReview": _review_fields(original.version_review),
            "indexReceiptIds": [row["receipt_id"] for row in original_indexes],
            "indexRequestBodyHashes": original_index_hashes,
            "crossCategoryReceiptIds": [row["receipt_id"] for row in o_cross_receipts],
            "crossCategoryRequestBodyHashes": o_cross_hashes,
            "crossCategoryDispositions": o_dispositions,
            "crossCategoryIndex": _cross_index_fields(original.cross_category_index),
            "crossCategoryDispositionInputs": _disposition_inputs(original.dispositions),
            "crossCategoryReviewedBy": original.reviewed_by,
            "crossCategoryReviewedAt": original.reviewed_at.isoformat()},
        "adjusted_prior": {"versionId": adjusted_id, "valueYuan": str(adjusted.value),
            "contentHash": annual_hash, "receiptId": annual_receipt["receipt_id"],
            "reportTitle": COMPANY + "2025年年度报告", "reportPeriodEnd": "2025-12-31",
            "sourceReportPeriodEnd": "2025-12-31", "columnRole": "PRIOR_ADJUSTED",
            "sourcePublishedDate": annual_pub.isoformat(),
            "firstSeenAt": adjusted.first_seen_at.isoformat(),
            "normalVerification": annual_verification.as_evidence(),
            "versionReview": _review_fields(annual.version_review),
            "indexReceiptIds": [row["receipt_id"] for row in annual_indexes],
            "indexRequestBodyHashes": annual_index_hashes,
            "crossCategoryReceiptIds": [row["receipt_id"] for row in a_cross_receipts],
            "crossCategoryRequestBodyHashes": a_cross_hashes,
            "crossCategoryDispositions": a_dispositions,
            "crossCategoryIndex": _cross_index_fields(annual.cross_category_index),
            "crossCategoryDispositionInputs": _disposition_inputs(annual.dispositions),
            "crossCategoryReviewedBy": annual.reviewed_by,
            "crossCategoryReviewedAt": annual.reviewed_at.isoformat()},
        "accepted_annual_dependency": dependency,
        "fresh_relationship_scope": fresh_scope,
        "company_descriptor": current_company_descriptor,
        "calendar_evidence": verified_calendar,
        "calendar_checked_at": calendar_checked_at.isoformat(),
        "trading_calendar": [day.isoformat() for day in trading_calendar],
        "formal_admission": "NOT_ADMITTED",
    }
    bundle_id = REVIEW_PREFIX + hashlib.sha256(
        f"{SCHEMA}|601069|2024-12-31|{root_id}|{adjusted_id}".encode()).hexdigest()[:32]
    bundle = FactReviewBundle(bundle_id, _canonical(review))
    added = repository.append_many((root, adjusted), review_bundle=bundle)
    return (root, adjusted), added


__all__ = ["SCHEMA", "RevenueChainContractError", "ReportReviewContext",
           "check_601069_current_relationship_scope", "write_601069_fy24_revenue_forward_root"]

