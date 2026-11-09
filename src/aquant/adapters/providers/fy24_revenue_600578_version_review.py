"""Independent reader validation for the finite 600578 FY24 revenue chain."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from ...artifact_paths import resolve_archive_member
from ...domain.data.db import connect
from ...domain.data.pit import date_only_available_at
from ...domain.data.rights import REGISTER_VERSION
from ...domain.fundamentals.versioned import FinancialFact
from . import s2_revision_impact as impact
from .cninfo import CrossCategoryIndexEntry, CrossCategoryIndexResult, ReportIndexPage
from .cninfo_statement_sections import extract_section_facts
from .fy24_revenue_600578_version_chain import (
    ADJUSTED_YUAN, COMPANY, DELTA_YUAN, FY25_CURRENT_YUAN, ORIGINAL_ID,
    ORIGINAL_PERIOD, ORIGINAL_RECEIPT_ID, ORIGINAL_YUAN, PRESENTING_ID,
    PRESENTING_PERIOD, PRESENTING_RECEIPT_ID, REVIEW_PREFIX,
    SCHEMA, RevenueChainContractError, ReportReviewContext, _adjusted_prior_row,
    _bind_annual_current, _bind_changed_pair, _canonical,
    _candidate_evidence_matches,
    _company_scope, _cross_index_fields, _current_candidate, _disposition_inputs,
    _match_impact_indexes, _outcome, _report_record, _require_current_cell,
    _review_fields, _scope_archive, _validate_company_report, _version_id,
)
from .pdf_promotion import (
    CrossCategoryDisposition, PdfVersionReview, _archived_ok,
    _cross_category_evidence, _next_available_snapshot, _version_evidence,
)
from .s2_machine_verification import verify_machine_evidence


class _ReadOnlyArchive:
    """Read-only archive handle used to replay exact receipt-bound originals."""

    def __init__(self, database_path: str, root: str) -> None:
        self.root = Path(root).resolve()
        if Path(database_path).resolve() != self.root / "meta.sqlite":
            raise ValueError("archive database is outside the declared archive root")
        self.con = connect(database_path, read_only=True)
        try:
            self.con.execute("PRAGMA query_only=ON")
            self.con.execute("BEGIN")
        except BaseException:
            self.con.close()
            raise

    def close(self) -> None:
        try:
            if self.con.in_transaction:
                self.con.execute("ROLLBACK")
        finally:
            self.con.close()

    def load_bytes(self, content_hash: str) -> bytes:
        row = self.con.execute(
            "SELECT stored_path,byte_size FROM raw_artifact WHERE content_hash=?",
            (content_hash,),
        ).fetchone()
        if row is None:
            raise KeyError(content_hash)
        path = resolve_archive_member(self.root, row["stored_path"])
        body = path.read_bytes()
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if len(body) != row["byte_size"] or digest != content_hash:
            raise ValueError("archived bytes do not match their receipt hash")
        return body

    def verify(self, content_hash: str) -> bool:
        try:
            self.load_bytes(content_hash)
        except (OSError, KeyError, ValueError):
            return False
        return True


def _review(value: Mapping[str, object]) -> PdfVersionReview:
    return PdfVersionReview(
        announcement_id=str(value["announcementId"]),
        org_lookup_receipt_id=str(value["orgLookupReceiptId"]),
        index_receipt_id=str(value["indexReceiptId"]),
        search_receipt_ids=tuple(str(item) for item in value["searchReceiptIds"]),
        search_page_numbers=tuple(int(item) for item in value["searchPageNumbers"]),
        query_stock_code=str(value["queryStockCode"]),
        query_organization_id=str(value["queryOrganizationId"]),
        query_period_start=date.fromisoformat(str(value["queryPeriodStart"])),
        query_period_end=date.fromisoformat(str(value["queryPeriodEnd"])),
        query_digest=str(value["queryDigest"]), reviewer_id=str(value["reviewerId"]),
        reviewed_at=datetime.fromisoformat(str(value["reviewedAt"])),
        complete_search_attested=value["completeSearchAttested"],
        predecessor_announcement_id=value.get("predecessorAnnouncementId"),
        correction_receipt_id=value.get("correctionReceiptId"),
        correction_excerpt=value.get("correctionExcerpt"),
    )


def _cross_category(value: Mapping[str, object]) -> CrossCategoryIndexResult:
    pages = tuple(ReportIndexPage(
        page_num=int(item["pageNum"]), request_body_hash=str(item["requestBodyHash"]),
        receipt_id=str(item["receiptId"]), content_hash=item.get("contentHash"),
        http_status=item.get("httpStatus"), raw_announcement_count=item.get("rawAnnouncementCount"),
        total_announcement=item.get("totalAnnouncement"), has_more=item.get("hasMore"),
    ) for item in value["pages"])
    announcements = tuple(CrossCategoryIndexEntry(
        announcement_id=str(item["announcementId"]), sec_code=str(item["secCode"]),
        title=str(item["title"]), announcement_time_ms=int(item["announcementTimeMs"]),
        document_url=str(item["documentUrl"]),
        candidate_reasons=tuple(str(reason) for reason in item["candidateReasons"]),
        page_num=int(item["pageNum"]), page_receipt_id=str(item["pageReceiptId"]),
        page_content_hash=str(item["pageContentHash"]),
    ) for item in value["announcements"])
    return CrossCategoryIndexResult(
        stock_code=str(value["stockCode"]), organization_id=value.get("organizationId"),
        report_period=str(value["reportPeriod"]),
        publication_window=tuple(date.fromisoformat(str(day)) for day in value["publicationWindow"]),
        org_lookup_receipt_id=value.get("orgLookupReceiptId"),
        org_lookup_content_hash=value.get("orgLookupContentHash"), pages=pages,
        announcements=announcements, skipped_different_security=int(value["skippedDifferentSecurity"]),
        total_announcement=value.get("totalAnnouncement"), complete=value["complete"],
        termination=str(value["termination"]), error=value.get("error"),
    )


def _dispositions(values) -> tuple[CrossCategoryDisposition, ...]:
    return tuple(CrossCategoryDisposition(
        announcement_id=str(item["announcementId"]), relevance=str(item["relevance"]),
        reviewer_id=str(item["reviewerId"]), reviewed_at=datetime.fromisoformat(str(item["reviewedAt"])),
        rationale=str(item["rationale"]), document_receipt_id=item.get("documentReceiptId"),
        related_report_announcement_ids=tuple(item.get("relatedReportAnnouncementIds", ())),
        relation_receipt_id=item.get("relationReceiptId"), relation_excerpt=item.get("relationExcerpt"),
    ) for item in values)


def _context(source: Mapping[str, object], proof: Mapping[str, object],
             scope_archive: Mapping[str, object]) -> ReportReviewContext:
    return ReportReviewContext(
        pdf_receipt_id=str(source["receiptId"]),
        version_review=_review(source["versionReview"]),
        cross_category_index=_cross_category(source["crossCategoryIndex"]),
        dispositions=_dispositions(source["crossCategoryDispositionInputs"]),
        reviewed_by=str(source["crossCategoryReviewedBy"]),
        reviewed_at=datetime.fromisoformat(str(source["crossCategoryReviewedAt"])),
        revision_impact_proof=proof,
        revision_impact_archive=scope_archive,
    )


def _database_path(archive) -> str:
    actual = next((row[2] for row in archive.con.execute("PRAGMA database_list")
                   if row[1] == "main"), None)
    if not actual:
        raise ValueError("archive database path is unavailable")
    return str(Path(actual).resolve())


def _verify_context(source: Mapping[str, object], fact: FinancialFact,
                    candidate, verification, *, archive, completed: datetime,
                    revision_proof: Mapping[str, object]) -> None:
    if (source.get("versionId") != fact.version_id or
            source.get("valueYuan") != str(fact.value) or
            source.get("contentHash") != fact.content_hash or
            source.get("sourcePublishedDate") != fact.source_published_date.isoformat() or
            source.get("firstSeenAt") != fact.first_seen_at.isoformat() or
            not _candidate_evidence_matches(source.get("normalCandidate"), candidate) or
            source.get("normalVerification") != verification.as_evidence()):
        raise ValueError("stored normal current candidate/verification differs from PDF replay")
    if (not verification.verified or fact.source_document_id != source["versionReview"]["announcementId"] or
            fact.period_end.isoformat() != source.get("factPeriodEnd") or
            source.get("sourceReportPeriodEnd") != source["versionReview"]["queryPeriodStart"]):
        raise ValueError("source report and fact periods/identity are not separately bound")
    if source.get("revisionImpactProof") != revision_proof:
        raise ValueError("stored revision impact proof differs from current replay input")
    if datetime.fromisoformat(str(revision_proof["completedAt"])) > completed:
        raise ValueError("full-scope impact proof completed after the chain")


def _validate_fact(fact: FinancialFact, *, version_id: str, announcement_id: str,
                   content_hash: str, value: Decimal, period_end: date,
                   supersedes_id: str | None) -> bool:
    return (
        fact.version_id == version_id and fact.instrument_id == "600578" and
        fact.metric == "revenue" and fact.period_end == period_end and
        fact.statement_scope.value == "CONSOLIDATED" and fact.profit_scope is None and
        fact.currency == "CNY" and fact.raw_unit == "yuan" and fact.source_id == "cninfo" and
        fact.source_document_id == announcement_id and fact.content_hash == content_hash and
        fact.value == value and fact.supersedes_id == supersedes_id and
        fact.timestamp_precision.value == "DATE" and fact.source_published_date is not None and
        fact.source_published_at is None and fact.availability_basis.value == "OBSERVED" and
        fact.pit_mode.value == "LIVE_OBSERVED"
    )


def accepts_600578_fy24_revenue_chain(
    row: sqlite3.Row, member_ids: set[str], facts: Mapping[str, FinancialFact], *,
    accepted_links: Mapping[str, tuple[str, str]],
) -> bool:
    """Reopen both report originals and replay every exact chain boundary."""
    try:
        raw = row["evidence_json"]
        evidence = json.loads(raw)
        if not isinstance(evidence, dict):
            return False
        digest = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if (json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")) != raw or
                row["evidence_hash"] != digest or evidence.get("schema") != SCHEMA or
                evidence.get("source_id") != "cninfo" or evidence.get("instrument_id") != "600578" or
                evidence.get("period_end") != ORIGINAL_PERIOD.isoformat() or
                evidence.get("announcement_id") != PRESENTING_ID or
                evidence.get("rights_register_version") != REGISTER_VERSION or
                evidence.get("human_review_claimed") is not None or
                not str(evidence.get("verifier_id", "")).startswith("machine:") or
                evidence.get("candidate_disposition") != "AFFECTS_VERSION_CHAIN" or
                evidence.get("formal_admission") != "NOT_ADMITTED" or
                not row["bundle_id"].startswith(REVIEW_PREFIX)):
            return False
        original, adjusted_data = evidence["original_current"], evidence["adjusted_prior"]
        root_id, adjusted_id = original["versionId"], adjusted_data["versionId"]
        expected_bundle = REVIEW_PREFIX + hashlib.sha256(
            f"{SCHEMA}|600578|2024-12-31|{root_id}|{adjusted_id}".encode()
        ).hexdigest()[:32]
        if (row["bundle_id"] != expected_bundle or
                set(evidence.get("fact_version_ids", ())) != {root_id, adjusted_id} or
                member_ids != {root_id, adjusted_id} or
                not {root_id, adjusted_id} <= facts.keys() or
                root_id in accepted_links or adjusted_id in accepted_links):
            return False
        root, adjusted = facts[root_id], facts[adjusted_id]
        root_hash, adjusted_hash = original["contentHash"], adjusted_data["contentHash"]
        expected_root = _version_id("cninfo", "600578", "2024-12-31", "revenue", ORIGINAL_ID, root_hash)
        expected_adjusted = _version_id(
            "cninfo", "600578", "2024-12-31", "revenue", PRESENTING_ID,
            adjusted_hash, "PRIOR_ADJUSTED", root_id,
        )
        if (root_id != expected_root or adjusted_id != expected_adjusted or
                not _validate_fact(root, version_id=root_id, announcement_id=ORIGINAL_ID,
                                   content_hash=root_hash, value=ORIGINAL_YUAN,
                                   period_end=ORIGINAL_PERIOD, supersedes_id=None) or
                not _validate_fact(adjusted, version_id=adjusted_id, announcement_id=PRESENTING_ID,
                                   content_hash=adjusted_hash, value=ADJUSTED_YUAN,
                                   period_end=ORIGINAL_PERIOD, supersedes_id=root_id) or
                original.get("sourceReportPeriodEnd") != "2024-12-31" or
                original.get("factPeriodEnd") != "2024-12-31" or
                original.get("columnRole") != "CURRENT" or
                adjusted_data.get("sourceReportPeriodEnd") != "2025-12-31" or
                adjusted_data.get("factPeriodEnd") != "2024-12-31" or
                adjusted_data.get("columnRole") != "PRIOR_ADJUSTED" or
                Decimal(str(adjusted_data.get("unadjustedPriorYuan"))) != ORIGINAL_YUAN or
                Decimal(str(adjusted_data.get("currentValueYuan"))) != FY25_CURRENT_YUAN or
                Decimal(str(evidence.get("delta_adjusted_prior_minus_original_current_yuan"))) != DELTA_YUAN or
                DELTA_YUAN == 0):
            return False
        cohort = [fact for fact in facts.values() if fact.logical_key == root.logical_key]
        if {fact.version_id for fact in cohort} != {root_id, adjusted_id}:
            return False

        scope_archive = evidence["scope_archive"]
        company = _company_scope(evidence["current_issuer_scope"])
        if (scope_archive.get("company") != company or
                not str(scope_archive.get("root", "")).strip() or
                Path(str(scope_archive.get("database_path", ""))).name != "meta.sqlite"):
            return False
        with closing(_ReadOnlyArchive(
            str(scope_archive["database_path"]), str(scope_archive["root"]))) as archive:
            if _database_path(archive) != str(Path(str(scope_archive["database_path"])).resolve()):
                return False
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
                    original_record["receipt"]["receipt_id"] != original.get("receiptId") or
                    annual_record["receipt"]["receipt_id"] != adjusted_data.get("receiptId") or
                    original_record["contentHash"] !=
                    "sha256:8681e1bd23a5e5d91d8ed07ca7acbc7da7be14ebcfea6f0860bed43d623adfe3" or
                    annual_record["contentHash"] !=
                    "sha256:e25638741dfaef06a80844a646e6329945040d6fbd758fc157a429a6920a6a7d"):
                return False
            original_receipt, original_body = _archived_ok(
                archive, str(original.get("receiptId")), expected_url=original_record["documentUrl"],
                expected_host="static.cninfo.com.cn",
            )
            annual_receipt, annual_body = _archived_ok(
                archive, str(adjusted_data.get("receiptId")), expected_url=annual_record["documentUrl"],
                expected_host="static.cninfo.com.cn",
            )
            if (original_receipt["content_hash"] != root_hash or
                    annual_receipt["content_hash"] != adjusted_hash or
                    original_receipt["first_seen_at"] != original.get("firstSeenAt") or
                    annual_receipt["first_seen_at"] != adjusted_data.get("firstSeenAt")):
                return False
            root_candidate, root_verification = _current_candidate(
                original_body, original_receipt, announcement_id=ORIGINAL_ID,
                period_end=ORIGINAL_PERIOD,
            )
            root_cell = _require_current_cell(
                root_candidate, announcement_id=ORIGINAL_ID, period_end=ORIGINAL_PERIOD,
                expected_value=ORIGINAL_YUAN, expected_page=99, expected_header="2024年度",
            )
            adjusted_candidate, adjusted_verification = _current_candidate(
                annual_body, annual_receipt, announcement_id=PRESENTING_ID,
                period_end=PRESENTING_PERIOD,
            )
            annual_cell = _require_current_cell(
                adjusted_candidate, announcement_id=PRESENTING_ID, period_end=PRESENTING_PERIOD,
                expected_value=FY25_CURRENT_YUAN, expected_page=101, expected_header="2025年度",
            )
            annual_section, annual_row = _adjusted_prior_row(annual_body)
            if (annual_row.amount != annual_cell.value_yuan or
                    annual_row.prior_amount != adjusted.value or
                    annual_row.page != adjusted_data.get("page") or
                    annual_row.label != adjusted_data.get("rowLabel") or
                    annual_row.current_cell != adjusted_data.get("currentCell") or
                    annual_row.prior_cell != adjusted_data.get("priorCell") or
                    annual_row.raw_line != adjusted_data.get("rawLine") or
                    annual_section.current_header != adjusted_data.get("currentHeader") or
                    annual_section.prior_header != adjusted_data.get("priorHeader") or
                    annual_section.unit != adjusted_data.get("unit")):
                return False

            scope = _scope_archive(archive, company)
            original_context = _context(original, original["revisionImpactProof"], scope)
            annual_context = _context(adjusted_data, adjusted_data["revisionImpactProof"], scope)
            for context, period in ((original_context, ORIGINAL_PERIOD),
                                    (annual_context, PRESENTING_PERIOD)):
                replay = dict(context.revision_impact_proof)
                impact.replay_revision_impact(
                    replay, archive_root=archive.root, company=company,
                    documents=context.revision_impact_archive.get("documents"),
                    target_period_end=period, target_metrics=("revenue",), issuer_name=COMPANY,
                )
                scope_row = replay["relationshipScope"]
                if (scope_row.get("completeForArchivedIndexResponse") is not True or
                        scope_row.get("missingReportSourceIds") != [] or
                        replay.get("instrumentId") != "SH.600578" or
                        replay.get("issuerName") != COMPANY):
                    return False
                _match_impact_indexes(replay, context)
            original_proof = original_context.revision_impact_proof
            annual_proof = annual_context.revision_impact_proof
            _bind_changed_pair(original_proof, root_cell, annual_row)
            _bind_annual_current(annual_proof, annual_cell)
            _verify_context(original, root, root_candidate, root_verification,
                            archive=archive, completed=datetime.fromisoformat(evidence["completed_at"]),
                            revision_proof=original_proof)
            _verify_context(adjusted_data, adjusted, adjusted_candidate, adjusted_verification,
                            archive=archive, completed=datetime.fromisoformat(evidence["completed_at"]),
                            revision_proof=annual_proof)

            completed = datetime.fromisoformat(evidence["completed_at"])
            if completed.tzinfo is None or completed > datetime.now(timezone.utc):
                return False
            replayed_index_times = []
            for context, candidate, fact, source in (
                (original_context, root_candidate, root, original),
                (annual_context, adjusted_candidate, adjusted, adjusted_data),
            ):
                review_obj = _review(source["versionReview"])
                published, role, index_rows, hashes = _version_evidence(
                    archive, candidate, review_obj, now=completed,
                )
                expected_receipts = source["indexReceiptIds"]
                if (role.value != "ORIGINAL_REPORT" or
                        published.isoformat() != source["sourcePublishedDate"] or
                        [item["receipt_id"] for item in index_rows] != expected_receipts or
                        hashes != source["indexRequestBodyHashes"]):
                    return False
                replayed_times = [datetime.fromisoformat(item["first_seen_at"])
                                  for item in index_rows]
                if (not str(source["crossCategoryReviewedBy"]).startswith("machine:") or
                        any(not str(item["reviewerId"]).startswith("machine:")
                            for item in source["crossCategoryDispositionInputs"])):
                    return False
                cross_receipts, cross_hashes, dispositions = _cross_category_evidence(
                    archive, candidate, context.cross_category_index, context.dispositions,
                    review=review_obj, reviewed_by=source["crossCategoryReviewedBy"],
                    reviewed_at=context.reviewed_at, pdf_receipt_id=source["receiptId"],
                    now=completed, revision_impact=dict(context.revision_impact_proof),
                )
                if ([item["receipt_id"] for item in cross_receipts] != source["crossCategoryReceiptIds"] or
                        cross_hashes != source["crossCategoryRequestBodyHashes"] or
                        dispositions != source["crossCategoryDispositions"] or
                        _review_fields(review_obj) != source["versionReview"] or
                        _cross_index_fields(context.cross_category_index) != source["crossCategoryIndex"] or
                        _disposition_inputs(context.dispositions) != source["crossCategoryDispositionInputs"]):
                    return False
                replayed_times.extend(datetime.fromisoformat(item["first_seen_at"])
                                      for item in cross_receipts)
                replayed_index_times.extend(replayed_times)

            from .s2_calendar_evidence import verify_calendar_evidence
            calendar_checked_at = datetime.fromisoformat(evidence["calendar_checked_at"])
            if calendar_checked_at > completed:
                return False
            verified_days, verified_packet = verify_calendar_evidence(
                evidence["calendar_evidence"], archive, now=calendar_checked_at,
                published_on=min(root.source_published_date, adjusted.source_published_date),
            )
            if ([day.isoformat() for day in verified_days] != evidence["trading_calendar"] or
                    verified_packet != evidence["calendar_evidence"]):
                return False
            proof_times = []
            for proof in (original_proof, annual_proof):
                proof_times.append(datetime.fromisoformat(proof["completedAt"]))
                proof_times.extend(datetime.fromisoformat(item["first_seen_at"])
                                   for item in proof["indexReceipts"])
                proof_times.extend(datetime.fromisoformat(item["receipt"]["first_seen_at"])
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
            calendar_observations = list(calendar_times(verified_packet))
            chain_times = [calendar_checked_at, root.first_seen_at, adjusted.first_seen_at,
                           root.ingested_at, adjusted.ingested_at,
                           original_context.version_review.reviewed_at,
                           annual_context.version_review.reviewed_at,
                           original_context.reviewed_at, annual_context.reviewed_at,
                           *proof_times, *calendar_observations,
                           *replayed_index_times]
            if any(value.tzinfo is None or value > completed for value in chain_times):
                return False
            calendar_floor, _ = date_only_available_at(
                root.source_published_date, verified_days,
            )
            annual_floor, _ = date_only_available_at(
                adjusted.source_published_date, verified_days,
            )
            expected_available = _next_available_snapshot(
                max(calendar_floor, annual_floor, completed, *chain_times), verified_days,
            )
            if (root.ingested_at != completed or adjusted.ingested_at != completed or
                    root.available_at != adjusted.available_at or
                    root.available_at.isoformat() != evidence["available_at"] or
                    expected_available != root.available_at or
                    root.first_seen_at.isoformat() != original["firstSeenAt"] or
                    adjusted.first_seen_at.isoformat() != adjusted_data["firstSeenAt"]):
                return False
        return True
    except (OSError, sqlite3.Error, KeyError, AttributeError, TypeError,
            ValueError, ArithmeticError) as exc:
        # The public reader rejects incomplete or malformed evidence.
        return False


__all__ = ["accepts_600578_fy24_revenue_chain"]
