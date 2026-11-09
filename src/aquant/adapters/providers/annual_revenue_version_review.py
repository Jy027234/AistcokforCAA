"""Public-reader verifier for the finite 601069 FY24 revenue chain schema."""

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
from .annual_comparative_revenue import produce_601069_fy24_revenue_candidate
from .annual_revenue_version_chain import (
    ORIGINAL_ID, ORIGINAL_PERIOD, PRESENTING_ID, PRESENTING_PERIOD, REVIEW_PREFIX,
    SCHEMA, check_601069_current_relationship_scope,
)
from .cninfo_statement_sections import extract_section_facts
from .pdf_promotion import (
    CrossCategoryDisposition, PdfVersionReview, _archived_ok,
    _cross_category_evidence, _next_available_snapshot, _version_evidence,
)
from .s2_machine_verification import verify_machine_evidence


class _ReadOnlyArchive:
    """Small read-only view needed by the ordinary archive replay helpers."""

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
        if len(body) != row["byte_size"] or "sha256:" + hashlib.sha256(body).hexdigest() != content_hash:
            raise ValueError("archived raw bytes do not match their index")
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
        search_receipt_ids=tuple(value["searchReceiptIds"]),
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


def _cross_category(value: Mapping[str, object]):
    from .cninfo import CrossCategoryIndexEntry, CrossCategoryIndexResult, ReportIndexPage

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
        candidate_reasons=tuple(item["candidateReasons"]), page_num=int(item["pageNum"]),
        page_receipt_id=str(item["pageReceiptId"]),
        page_content_hash=str(item["pageContentHash"]),
    ) for item in value["announcements"])
    return CrossCategoryIndexResult(
        stock_code=str(value["stockCode"]), organization_id=value.get("organizationId"),
        report_period=str(value["reportPeriod"]),
        publication_window=tuple(date.fromisoformat(day) for day in value["publicationWindow"]),
        org_lookup_receipt_id=value.get("orgLookupReceiptId"),
        org_lookup_content_hash=value.get("orgLookupContentHash"), pages=pages,
        announcements=announcements,
        skipped_different_security=int(value["skippedDifferentSecurity"]),
        total_announcement=value.get("totalAnnouncement"), complete=value["complete"],
        termination=str(value["termination"]), error=value.get("error"),
    )


def _dispositions(values) -> tuple[CrossCategoryDisposition, ...]:
    return tuple(CrossCategoryDisposition(
        announcement_id=str(item["announcementId"]), relevance=str(item["relevance"]),
        reviewer_id=str(item["reviewerId"]), reviewed_at=datetime.fromisoformat(item["reviewedAt"]),
        rationale=str(item["rationale"]), document_receipt_id=item.get("documentReceiptId"),
        related_report_announcement_ids=tuple(item.get("relatedReportAnnouncementIds", ())),
        relation_receipt_id=item.get("relationReceiptId"), relation_excerpt=item.get("relationExcerpt"),
    ) for item in values)


def _checkpoint_from_reextracted(candidate) -> dict[str, object]:
    item = candidate.by_field["revenue"]
    label = item.display_row_label or (item.source_cells[0] or "")
    current = item.source_cells[item.source_current_cell_index]
    return {"report": {
        "announcementId": ORIGINAL_ID, "instrumentId": "601069", "periodEnd": "2024-12-31",
        "candidateEvidence": {"candidate": {
            "instrument_id": "601069", "period_end": "2024-12-31", "candidates": [{
                "field": "revenue", "value_yuan": str(item.value_yuan),
                "period_end": "2024-12-31", "amount_unit": item.amount_unit,
                "currency": item.currency, "pdf_page": item.pdf_page,
                "column_header": item.column_header, "display_row_label": label,
                "source_cells": [label, current],
            }],
        }},
    }}


def _accepted_annual_dependency(evidence: Mapping[str, object], facts: Mapping[str, FinancialFact],
                                accepted_links: Mapping[str, tuple[str, str]],
                                review_rows: Mapping[str, sqlite3.Row]) -> tuple[FinancialFact, dict]:
    dep = evidence["accepted_annual_dependency"]
    version_id = dep["versionId"]
    fact = facts[version_id]
    link = accepted_links.get(version_id)
    if link != (dep["bundleId"], dep["evidenceHash"]):
        raise ValueError("the cited FY25 annual dependency is not currently accepted by the public reader")
    row = review_rows.get(link[0])
    if row is None or row["evidence_hash"] != link[1]:
        raise ValueError("accepted annual immutable review row is absent or changed")
    annual_review = json.loads(row["evidence_json"])
    if (annual_review.get("schema") != "cninfo-s2-machine-verified-v1" or
            annual_review.get("instrument_id") != "601069" or
            annual_review.get("announcement_id") != PRESENTING_ID or
            annual_review.get("period_end") != PRESENTING_PERIOD.isoformat() or
            version_id not in annual_review.get("version_ids", ())):
        raise ValueError("accepted annual link points to another review cohort")
    outcomes = [item for item in annual_review["revision_impact_proof"]["outcomes"]
                if item.get("metric") == "revenue"]
    if len(outcomes) != 1:
        raise ValueError("accepted annual review does not preserve its revenue impact proof")
    current = outcomes[0]["currentSource"]
    adjusted = evidence["adjusted_prior"]
    if (outcomes[0].get("status") != "NO_EFFECT" or
            current.get("columnRole") != "CURRENT" or
            current.get("announcementId") != PRESENTING_ID or
            current.get("sourceReportPeriodEnd") != "2025-12-31" or
            Decimal(str(current.get("priorAmountYuan"))) != Decimal(str(adjusted["valueYuan"])) or
            Decimal(str(fact.value)) != Decimal(str(current["amountYuan"]))):
        raise ValueError("annual dependency does not bind its current and adjusted comparative cells")
    return fact, annual_review


def accepts_601069_fy24_revenue_chain(
    row: sqlite3.Row, member_ids: set[str], facts: Mapping[str, FinancialFact], *,
    accepted_links: Mapping[str, tuple[str, str]],
    review_rows: Mapping[str, sqlite3.Row],
) -> bool:
    """Re-read source/index proofs and accept only this exact two-version cohort."""
    try:
        raw = row["evidence_json"]
        evidence = json.loads(raw)
        if not isinstance(evidence, dict):
            return False
        canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
        if (canonical != raw or row["evidence_hash"] != digest or evidence.get("schema") != SCHEMA or
                evidence.get("source_id") != "cninfo" or evidence.get("instrument_id") != "601069" or
                evidence.get("period_end") != "2024-12-31" or
                evidence.get("announcement_id") != PRESENTING_ID or
                evidence.get("rights_register_version") != REGISTER_VERSION or
                evidence.get("human_review_claimed") is not None or
                not str(evidence.get("verifier_id", "")).startswith("machine:") or
                evidence.get("candidate_disposition") != "AFFECTS_VERSION_CHAIN" or
                evidence.get("formal_admission") != "NOT_ADMITTED"):
            return False
        for bundle_id, prior_row in review_rows.items():
            if bundle_id == row["bundle_id"]:
                continue
            try:
                prior_evidence = json.loads(prior_row["evidence_json"])
            except (TypeError, ValueError):
                continue
            if (not isinstance(prior_evidence, Mapping) or
                    prior_evidence.get("instrument_id") not in ("601069", "SH.601069") or
                    prior_evidence.get("period_end") != "2024-12-31" or
                    prior_evidence.get("announcement_id") != ORIGINAL_ID):
                continue
            prior_fields = set(prior_evidence.get("required_fields", ()))
            prior_fields.update(item.get("field") for item in prior_evidence.get("fields", ())
                                if isinstance(item, Mapping))
            prior_fields.update(item.get("field") for item in prior_evidence.get("field_reviews", ())
                                if isinstance(item, Mapping))
            if "revenue" in prior_fields:
                return False
        if not row["bundle_id"].startswith(REVIEW_PREFIX):
            return False
        expected_bundle = REVIEW_PREFIX + hashlib.sha256(
            f"{SCHEMA}|601069|2024-12-31|{evidence['original_current']['versionId']}|"
            f"{evidence['adjusted_prior']['versionId']}".encode()
        ).hexdigest()[:32]
        if row["bundle_id"] != expected_bundle:
            return False
        root_data, adjusted_data = evidence["original_current"], evidence["adjusted_prior"]
        root_id, adjusted_id = root_data["versionId"], adjusted_data["versionId"]
        if (set(evidence.get("fact_version_ids", ())) != {root_id, adjusted_id} or
                member_ids != {root_id, adjusted_id} or
                not {root_id, adjusted_id} <= facts.keys()):
            return False
        root, adjusted = facts[root_id], facts[adjusted_id]
        if (root.metric != "revenue" or adjusted.metric != "revenue" or
                root.instrument_id != "601069" or adjusted.instrument_id != "601069" or
                root.period_end != ORIGINAL_PERIOD or adjusted.period_end != ORIGINAL_PERIOD or
                root.statement_scope.value != "CONSOLIDATED" or
                adjusted.statement_scope.value != "CONSOLIDATED" or
                root.profit_scope is not None or adjusted.profit_scope is not None or
                root.currency != "CNY" or adjusted.currency != "CNY" or
                root.raw_unit != "yuan" or adjusted.raw_unit != "yuan" or
                root.source_id != "cninfo" or adjusted.source_id != "cninfo" or
                root.timestamp_precision.value != "DATE" or
                adjusted.timestamp_precision.value != "DATE" or
                root.availability_basis.value != "OBSERVED" or
                adjusted.availability_basis.value != "OBSERVED" or
                root.pit_mode.value != "LIVE_OBSERVED" or
                adjusted.pit_mode.value != "LIVE_OBSERVED" or
                root.source_document_id != ORIGINAL_ID or adjusted.source_document_id != PRESENTING_ID or
                root_data.get("reportPeriodEnd") != "2024-12-31" or
                adjusted_data.get("reportPeriodEnd") != "2025-12-31" or
                adjusted_data.get("sourceReportPeriodEnd") != "2025-12-31" or
                adjusted_data.get("columnRole") != "PRIOR_ADJUSTED" or
                root.content_hash != root_data["contentHash"] or
                adjusted.content_hash != adjusted_data["contentHash"] or
                root.supersedes_id is not None or adjusted.supersedes_id != root_id):
            return False
        root_expected = "mach_" + hashlib.sha256(
            f"cninfo|601069|2024-12-31|revenue|{ORIGINAL_ID}|{root.content_hash}".encode()
        ).hexdigest()[:32]
        adjusted_expected = "mach_" + hashlib.sha256(
            f"cninfo|601069|2024-12-31|revenue|{PRESENTING_ID}|{adjusted.content_hash}|PRIOR_ADJUSTED|{root_id}".encode()
        ).hexdigest()[:32]
        if root_id != root_expected or adjusted_id != adjusted_expected:
            return False
        delta = Decimal(str(adjusted.value)) - Decimal(str(root.value))
        if (delta == 0 or delta != Decimal(str(evidence["delta_adjusted_prior_minus_original_current_yuan"])) or
                Decimal(str(root.value)) != Decimal(str(root_data["valueYuan"])) or
                Decimal(str(adjusted.value)) != Decimal(str(adjusted_data["valueYuan"]))):
            return False
        # No aliased or additional logical version may be hidden from the root check.
        cohort = [fact for fact in facts.values() if fact.instrument_id in ("601069", "SH.601069") and
                  fact.metric == "revenue" and fact.period_end == ORIGINAL_PERIOD and
                  fact.statement_scope.value == "CONSOLIDATED" and fact.profit_scope is None and
                  fact.currency == "CNY" and fact.raw_unit == "yuan"]
        if {fact.version_id for fact in cohort} != {root_id, adjusted_id}:
            return False
        annual_fact, annual_review = _accepted_annual_dependency(
            evidence, facts, accepted_links, review_rows)
        annual_proof_completed = datetime.fromisoformat(
            annual_review["revision_impact_proof"]["completedAt"])
        if (root.available_at != adjusted.available_at or
                root.ingested_at != adjusted.ingested_at or
                root.available_at.isoformat() != evidence["available_at"] or
                root.ingested_at.isoformat() != evidence["completed_at"] or
                annual_fact.ingested_at > root.ingested_at or
                annual_proof_completed > root.ingested_at or
                annual_fact.available_at > root.available_at or
                root.first_seen_at.isoformat() != root_data["firstSeenAt"] or
                adjusted.first_seen_at.isoformat() != adjusted_data["firstSeenAt"] or
                root.source_published_date.isoformat() != root_data["sourcePublishedDate"] or
                adjusted.source_published_date.isoformat() != adjusted_data["sourcePublishedDate"]):
            return False
        if (not evidence.get("trading_calendar") or not evidence.get("calendar_evidence") or
                not evidence.get("calendar_checked_at")):
            return False
        calendar = tuple(date.fromisoformat(item) for item in evidence["trading_calendar"])
        if calendar != tuple(sorted(set(calendar))):
            return False
        floors = [date_only_available_at(root.source_published_date, calendar)[0],
                  date_only_available_at(adjusted.source_published_date, calendar)[0],
                  annual_fact.available_at, root.ingested_at]
        floors.extend(datetime.fromisoformat(value) for value in (
            root_data["firstSeenAt"], adjusted_data["firstSeenAt"],
            root_data["versionReview"]["reviewedAt"], adjusted_data["versionReview"]["reviewedAt"]))
        expected_available = _next_available_snapshot(max(floors), calendar)
        if expected_available != root.available_at:
            return False
        archive_spec = annual_review.get("revision_impact_archive")
        if not isinstance(archive_spec, Mapping):
            return False
        with closing(_ReadOnlyArchive(str(archive_spec["database_path"]),
                                      str(archive_spec["root"]))) as archive:
            from .s2_calendar_evidence import verify_calendar_evidence
            calendar_checked_at = datetime.fromisoformat(evidence["calendar_checked_at"])
            if calendar_checked_at > root.ingested_at:
                return False
            verified_days, verified_packet = verify_calendar_evidence(
                evidence["calendar_evidence"], archive, now=calendar_checked_at,
                published_on=min(root.source_published_date, adjusted.source_published_date))
            if (verified_packet != evidence["calendar_evidence"] or
                    [day.isoformat() for day in verified_days] != evidence["trading_calendar"]):
                return False
            def calendar_first_seen(value):
                if isinstance(value, Mapping):
                    for key, child in value.items():
                        if key == "first_seen_at" and isinstance(child, str):
                            yield datetime.fromisoformat(child)
                        else:
                            yield from calendar_first_seen(child)
                elif isinstance(value, list):
                    for child in value:
                        yield from calendar_first_seen(child)

            replayed_times = [calendar_checked_at, root.first_seen_at, adjusted.first_seen_at,
                              annual_fact.ingested_at, annual_proof_completed]
            replayed_times.extend(calendar_first_seen(verified_packet))
            original_context, annual_context = root_data, adjusted_data
            original_receipt, original_body = _archived_ok(
                archive, str(original_context["receiptId"]), expected_host="static.cninfo.com.cn")
            annual_receipt, annual_body = _archived_ok(
                archive, str(annual_context["receiptId"]), expected_host="static.cninfo.com.cn")
            if (original_receipt["content_hash"] != root.content_hash or
                    annual_receipt["content_hash"] != adjusted.content_hash or
                    original_receipt["first_seen_at"] != root_data["firstSeenAt"] or
                    annual_receipt["first_seen_at"] != adjusted_data["firstSeenAt"]):
                return False
            company = str(original_context.get("company", "西部黄金股份有限公司"))
            original_candidate = extract_section_facts(
                original_body, instrument_id="601069", company=company,
                period_end=ORIGINAL_PERIOD, announcement_id=ORIGINAL_ID,
                document_url=original_receipt["url"], required_fields=("revenue",))
            original_verification = verify_machine_evidence(
                original_body, candidate=original_candidate, expected_fields=("revenue",))
            annual_candidate = extract_section_facts(
                annual_body, instrument_id="601069", company=company,
                period_end=PRESENTING_PERIOD, announcement_id=PRESENTING_ID,
                document_url=annual_receipt["url"], required_fields=("revenue",))
            annual_verification = verify_machine_evidence(
                annual_body, candidate=annual_candidate, expected_fields=("revenue",))
            if (not original_verification.verified or not annual_verification.verified or
                    original_verification.as_evidence() != original_context["normalVerification"] or
                    annual_verification.as_evidence() != annual_context["normalVerification"]):
                return False
            annual_fact_hash = annual_review.get("content_hash")
            if (annual_fact_hash != adjusted.content_hash or
                    annual_fact.content_hash != adjusted.content_hash or
                    annual_review.get("company") != "西部黄金股份有限公司"):
                return False
            annual_document = {"metadata": {"announcementId": PRESENTING_ID, "code": "601069",
                                             "periodEnd": "2025-12-31",
                                             "title": annual_context["reportTitle"]},
                               "body": annual_body}
            comparative = produce_601069_fy24_revenue_candidate(
                _checkpoint_from_reextracted(original_candidate), annual_document)
            if comparative.as_dict() != evidence.get("candidate"):
                return False
            for context, candidate, impact_proof in (
                    (original_context, original_candidate, None),
                    (annual_context, annual_candidate,
                     annual_review["revision_impact_proof"])):
                if (not str(context["crossCategoryReviewedBy"]).startswith("machine:") or
                        any(not str(item["reviewerId"]).startswith("machine:")
                            for item in context["crossCategoryDispositionInputs"])):
                    return False
                cross_receipts, cross_hashes, dispositions = _cross_category_evidence(
                    archive, candidate, _cross_category(context["crossCategoryIndex"]),
                    _dispositions(context["crossCategoryDispositionInputs"]),
                    review=_review(context["versionReview"]),
                    reviewed_by=context["crossCategoryReviewedBy"],
                    reviewed_at=datetime.fromisoformat(context["crossCategoryReviewedAt"]),
                    pdf_receipt_id=context["receiptId"],
                    now=datetime.now(timezone.utc), revision_impact=impact_proof,
                )
                if ([item["receipt_id"] for item in cross_receipts] !=
                        context["crossCategoryReceiptIds"] or
                        cross_hashes != context["crossCategoryRequestBodyHashes"] or
                        dispositions != context["crossCategoryDispositions"]):
                    return False
                replayed_times.append(datetime.fromisoformat(context["crossCategoryReviewedAt"]))
                replayed_times.extend(datetime.fromisoformat(item["first_seen_at"])
                                      for item in cross_receipts)
                replayed_times.extend(datetime.fromisoformat(item["reviewedAt"])
                                      for item in context["crossCategoryDispositionInputs"])
            for fact, context, candidate in (
                    (root, original_context, original_candidate),
                    (adjusted, annual_context, annual_candidate)):
                review = _review(context["versionReview"])
                published, role, indexes, hashes = _version_evidence(
                    archive, candidate, review, now=datetime.now(timezone.utc))
                replayed_times.extend(datetime.fromisoformat(item["first_seen_at"])
                                      for item in indexes)
                replayed_times.append(review.reviewed_at)
                if (published.isoformat() != context["sourcePublishedDate"] or
                        role.value != "ORIGINAL_REPORT" or
                        [item["receipt_id"] for item in indexes] != context["indexReceiptIds"] or
                        hashes != context["indexRequestBodyHashes"] or
                        review.announcement_id != fact.source_document_id):
                    return False
            if any(value > root.ingested_at for value in replayed_times):
                return False
            annual_proof = annual_review["revision_impact_proof"]
            replayed_scope = check_601069_current_relationship_scope(
                archive, evidence["company_descriptor"], annual_proof,
                normal_indexes=(
                    _cross_category(original_context["crossCategoryIndex"]),
                    _cross_category(annual_context["crossCategoryIndex"]),
                ))
            if replayed_scope != evidence["fresh_relationship_scope"]:
                return False
            scope_times = [datetime.fromisoformat(value)
                           for value in replayed_scope["indexReceiptFirstSeenAt"]]
            scope_times.extend(datetime.fromisoformat(item["firstSeenAt"])
                               for item in replayed_scope["originalSources"])
            if any(value > root.ingested_at for value in scope_times):
                return False
        return True
    except (OSError, sqlite3.Error, KeyError, AttributeError, TypeError, ValueError, ArithmeticError):
        return False


__all__ = ["accepts_601069_fy24_revenue_chain"]
