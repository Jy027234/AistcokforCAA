"""Bounded normal admission of the three real 601069 H1 CFO observations.

This protocol preserves the H125 original as a forward-observed root, then
records the H126 report's different H125 PRIOR and its H126 CURRENT. The
already accepted FY25 annual review supplies a receipt-replayed native H1
before/adjusted bridge. An annual NO_EFFECT result is never an H1 NO_EFFECT
disposition. All three new facts and their single immutable review are appended
by one repository transaction after both reports' normal source/index checks.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence

from ...domain.data.forward_archive import ForwardArchive
from ...domain.data.pit import AvailabilityBasis, PitMode, TimestampPrecision, date_only_available_at
from ...domain.data.rights import REGISTER_VERSION, default_rights
from ...domain.fundamentals.disclosure_link import DisclosureRole, disclosure_role
from ...domain.fundamentals.fact_repository import FactReviewBundle, FinancialFactRepository
from ...domain.fundamentals.versioned import FinancialFact, StatementScope
from .cninfo import CrossCategoryIndexResult
from .cninfo_probe import normalize_title, title_revision_flags
from .cninfo_statement_sections import extract_section_facts
from .comparative_cashflow import (
    derive_h125_cashflow_version_relation, extract_comparative_cashflow_contract,
)
from .pdf_promotion import (
    CrossCategoryDisposition, PdfPromotionError, PdfVersionReview,
    _archived_ok, _aware, _cross_category_evidence, _next_available_snapshot,
    _utc_now, _version_evidence,
)
from .s2_machine_verification import (
    MACHINE_VERIFICATION_SCHEMA, VERIFIER_ID, VERIFIER_VERSION, verify_machine_evidence,
)


SCHEMA = "aquant.cninfo.601069-h1-cfo-version-chain.v1"
VERIFIER = "machine:cninfo-601069-h1-cfo-version-chain-v1"
CORRECTIONS = frozenset({"1225082555", "1225082560", "1225082606"})
ORIGINAL_ID, PRESENTING_ID, ANNUAL_ID = "1224590046", "1225494335", "1225082554"
COMPANY = "西部黄金股份有限公司"


@dataclass(frozen=True, slots=True)
class CashflowReportInput:
    period_end: date
    pdf_receipt_id: str
    version_review: PdfVersionReview
    cross_category_index: CrossCategoryIndexResult
    dispositions: tuple[CrossCategoryDisposition, ...]
    reviewed_by: str
    reviewed_at: datetime


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _normal_id(period: date, announcement: str, content_hash: str) -> str:
    identity = f"cninfo|601069|{period}|operating_cashflow|{announcement}|{content_hash}"
    return "mach_" + hashlib.sha256(identity.encode()).hexdigest()[:32]


def _accepted_annual_dependency(repository: FinancialFactRepository) -> dict:
    from ...domain.fundamentals.fact_repository import (
        _accepted_machine_verified_review, _accepted_review_group_links,
        _accepted_s2_pdf_review, _decode,
    )
    # Reuse the actual reader's validators and disjoint-cohort grouping for this
    # one annual report. Do not load/replay the unrelated full warehouse merely
    # to establish the existing immutable annual dependency.
    con = sqlite3.connect(Path(repository.db_path).resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        rows = con.execute(
            "SELECT * FROM financial_fact WHERE instrument_id='601069' AND "
            "period_end='2025-12-31' AND source_id='cninfo' AND source_document_id=?",
            (ANNUAL_ID,)).fetchall()
        facts = {item["version_id"]: _decode(item) for item in rows}
        reviews = con.execute(
            "SELECT DISTINCT r.* FROM financial_fact_review r "
            "JOIN financial_fact_review_member m ON m.bundle_id=r.bundle_id "
            "JOIN financial_fact f ON f.version_id=m.version_id "
            "WHERE f.instrument_id='601069' AND f.period_end='2025-12-31' "
            "AND f.source_id='cninfo' AND f.source_document_id=?", (ANNUAL_ID,)).fetchall()
        valid = []
        for review in reviews:
            members = {item[0] for item in con.execute(
                "SELECT version_id FROM financial_fact_review_member WHERE bundle_id=?",
                (review["bundle_id"],))}
            if (_accepted_s2_pdf_review(review, members, facts) or
                    _accepted_machine_verified_review(review, members, facts)):
                valid.append((review, members))
        accepted = _accepted_review_group_links(("601069", "2025-12-31", ANNUAL_ID), valid, facts)
        annual = [fact for fact in facts.values() if fact.metric == "operating_cashflow" and
                  accepted and fact.version_id in accepted]
        if len(annual) != 1:
            raise PdfPromotionError("one actually accepted original annual CFO review is required")
        annual_fact = annual[0]
        bundle_id, evidence_hash = accepted[annual_fact.version_id]
        if not bundle_id.startswith("cninfo_machine_"):
            raise PdfPromotionError("annual version bridge must come from an accepted machine review")
        row = next((review for review in reviews if review["bundle_id"] == bundle_id), None)
        if row is None or row["evidence_hash"] != evidence_hash:
            raise PdfPromotionError("accepted annual review link differs from its immutable row")
        evidence = json.loads(row["evidence_json"])
        if canonical(evidence) != row["evidence_json"] or _digest(row["evidence_json"]) != evidence_hash:
            raise PdfPromotionError("annual review canonical bytes differ from the accepted link")
        existing = tuple(_decode(item) for item in con.execute(
            "SELECT * FROM financial_fact WHERE instrument_id IN ('601069','SH.601069') "
            "AND metric='operating_cashflow' AND period_end IN ('2025-06-30','2026-06-30')"))
        if con.total_changes:
            raise PdfPromotionError("annual dependency lookup changed the warehouse")
    finally:
        if con.in_transaction:
            con.execute("ROLLBACK")
        con.close()
    if (evidence.get("instrument_id") != "601069" or
            evidence.get("period_end") != "2025-12-31" or
            evidence.get("announcement_id") != ANNUAL_ID or
            not isinstance(evidence.get("revision_impact_proof"), Mapping)):
        raise PdfPromotionError("accepted annual review lacks its original native version proof")
    return {"factVersionId": annual_fact.version_id, "reviewBundleId": bundle_id,
            "reviewEvidenceHash": evidence_hash, "evidence": evidence,
            "acceptedFactIngestedAt": annual_fact.ingested_at.isoformat(),
            "acceptedFactAvailableAt": annual_fact.available_at.isoformat(),
            "acceptedFactPublishedOn": annual_fact.source_published_date.isoformat(),
            "existingFacts": existing}


def _check_current_relationship_scope(
    archive: ForwardArchive, company: Mapping, annual_impact: Mapping,
) -> dict:
    """Check fresh index/source completeness without re-extracting old sources.

    The immutable annual review replays its native correction policy. Here the
    normal writer must also establish that today's original-byte index has no
    additional applicable financial version or missing original source.
    """
    from . import s2_revision_impact as impact

    if company.get("code") != "601069" or company.get("instrumentId") != "SH.601069":
        raise PdfPromotionError("current relationship descriptor issuer differs")
    entries, receipts, request_hashes = impact._index(archive.con, archive.root, company)
    periods = {
        aid: parsed.isoformat() for aid, entry in entries.items()
        if (parsed := impact._report_period(entry["title"])) is not None and
        parsed.isoformat() in impact.PERIODS and
        disclosure_role(entry["title"]) in (DisclosureRole.ORIGINAL_REPORT, DisclosureRole.REVISED_REPORT) and
        not impact._601069_is_annual_reporting_policy(entry["title"]) and
        not impact._601069_is_delayed_disclosure_notice(entry["title"])
    }
    financial = {aid for aid, entry in entries.items() if impact._financial_title(entry["title"]) and
                 not impact._601069_is_annual_reporting_policy(entry["title"]) and
                 not impact._601069_is_delayed_disclosure_notice(entry["title"])}
    recalled = {aid for aid, entry in entries.items() if aid in financial or
                any(title_revision_flags(entry["title"])[:3])}
    previous = annual_impact["relationshipScope"]
    if financial != CORRECTIONS or periods != previous["reportCandidatePeriods"]:
        raise PdfPromotionError("fresh index contains an unresolved financial version or correction")
    documents = [*company["selectedReportRecords"], *company["relatedDocumentRecords"]]
    by_id = {str(item["announcementId"]): item for item in documents}
    originals = {str(item["announcementId"]): item for item in annual_impact["sources"]}
    if (len(by_id) != len(documents) or not recalled <= by_id.keys() or
            not set(periods) <= by_id.keys() or not by_id.keys() <= originals.keys() or
            {str(item["announcementId"]) for item in company["selectedReportRecords"]} !=
            set(previous["selectedReportIds"])):
        raise PdfPromotionError("current relationship original-source set is incomplete or changed")
    source_bindings = []
    for aid, document in sorted(by_id.items()):
        entry, old = entries.get(aid), originals[aid]
        if (entry is None or document.get("code") != "601069" or
                document.get("instrumentId") != "SH.601069" or
                document["documentUrl"] != entry["documentUrl"] or
                normalize_title(document["title"]) != entry["title"] or
                entry["documentUrl"] != old["index"]["documentUrl"] or
                entry["publishedDate"] != old["index"]["publishedDate"] or
                entry["title"] != old["index"]["title"]):
            raise PdfPromotionError("a current related source identity/date differs from the annual bridge")
        receipt, payload = _archived_ok(
            archive, document["receipt"]["receipt_id"], expected_url=entry["documentUrl"],
            expected_host="static.cninfo.com.cn")
        if (receipt != old["receipt"] or document["contentHash"] != old["originalHash"] or
                receipt["content_hash"] != old["originalHash"] or
                len(payload) != old["originalBytes"] or not payload.startswith(b"%PDF-")):
            raise PdfPromotionError("current related PDF bytes/SQL receipt differ from the replayed annual source")
        source_bindings.append({"announcementId": aid, "contentHash": receipt["content_hash"],
                                "receiptId": receipt["receipt_id"]})
    return {"reportCandidatePeriods": periods, "financialCauseIds": sorted(financial),
            "allRecalledRevisionIds": sorted(recalled), "requestBodyHashes": request_hashes,
            "indexReceiptIds": [row["receipt_id"] for row in receipts],
            "originalSources": source_bindings}


def _read_report(archive: ForwardArchive, report: CashflowReportInput, now: datetime) -> dict:
    expected_id = ORIGINAL_ID if report.period_end == date(2025, 6, 30) else PRESENTING_ID
    review = report.version_review
    if (report.period_end not in (date(2025, 6, 30), date(2026, 6, 30)) or
            review.announcement_id != expected_id or review.predecessor_announcement_id is not None or
            not review.reviewer_id.startswith("machine:") or not report.reviewed_by.startswith("machine:") or
            any(not item.reviewer_id.startswith("machine:") for item in report.dispositions)):
        raise PdfPromotionError("comparative source must keep its own original report and machine identities")
    receipt, body = _archived_ok(archive, report.pdf_receipt_id, expected_host="static.cninfo.com.cn")
    candidate = extract_section_facts(
        body, instrument_id="601069", company=COMPANY, period_end=report.period_end,
        announcement_id=review.announcement_id, document_url=receipt["url"],
        required_fields=("operating_cashflow",))
    verification = verify_machine_evidence(body, candidate=candidate, expected_fields=("operating_cashflow",))
    if not verification.verified:
        raise PdfPromotionError("normal independent CFO verification failed")
    published, role, index_receipts, index_hashes = _version_evidence(archive, candidate, review, now=now)
    if role is not DisclosureRole.ORIGINAL_REPORT:
        raise PdfPromotionError("a presenting H126 report is not an H125 revised full report")
    by_id = {item.announcement_id: item for item in report.dispositions}
    if any(aid not in by_id or by_id[aid].relevance != "RELATED" for aid in CORRECTIONS):
        raise PdfPromotionError("all actual correction PDFs need explicit version-chain relations, never NO_EFFECT")
    cross_receipts, cross_hashes, dispositions = _cross_category_evidence(
        archive, candidate, report.cross_category_index, report.dispositions,
        review=review, reviewed_by=report.reviewed_by, reviewed_at=report.reviewed_at,
        pdf_receipt_id=report.pdf_receipt_id, now=now)
    first_seen = _aware(datetime.fromisoformat(receipt["first_seen_at"]), "PDF first_seen_at")
    times = [first_seen, _aware(review.reviewed_at, "version reviewed_at"),
             _aware(report.reviewed_at, "cross reviewed_at")]
    times.extend(_aware(datetime.fromisoformat(row["first_seen_at"]), "index first_seen_at")
                 for row in (*index_receipts, *cross_receipts))
    if max(times) > now:
        raise PdfPromotionError("normal source or review was observed after validation")
    return {"candidate": candidate, "verification": verification, "receipt": receipt,
            "body": body, "published": published, "firstSeen": first_seen,
            "indexReceipts": index_receipts, "indexHashes": index_hashes,
            "crossReceipts": cross_receipts, "crossHashes": cross_hashes,
            "dispositions": dispositions, "review": report, "times": times}


def _corrected_h1_source_proof(archive: ForwardArchive, scope: Mapping) -> dict:
    """Replay the direct corrected H1 statement, distinct from annual adjustment."""
    from .interim_cashflow_correction import (
        TARGET_ANNOUNCEMENT_ID, TARGET_DOCUMENT_URL, extract_corrected_h1_operating_cashflow,
    )
    bindings = [item for item in scope["originalSources"]
                if item["announcementId"] == TARGET_ANNOUNCEMENT_ID]
    if len(bindings) != 1:
        raise PdfPromotionError("the complete version scope lacks its corrected H1 statement")
    receipt, payload = _archived_ok(
        archive, bindings[0]["receiptId"], expected_url=TARGET_DOCUMENT_URL,
        expected_host="static.cninfo.com.cn")
    if receipt["content_hash"] != bindings[0]["contentHash"]:
        raise PdfPromotionError("the corrected H1 source differs from its complete version binding")
    proof = extract_corrected_h1_operating_cashflow(
        payload, instrument_id="601069", announcement_id=TARGET_ANNOUNCEMENT_ID,
        document_url=TARGET_DOCUMENT_URL, period_end=date(2025, 6, 30))
    return {"receiptId": receipt["receipt_id"], "contentHash": receipt["content_hash"],
            "sourceReportPeriodEnd": "2025-06-30", "columnRole": "CURRENT",
            "nativeProof": proof.as_evidence()}


def promote_cashflow_version_chain(
    *, original_h125: CashflowReportInput, presenting_h126: CashflowReportInput,
    current_company: Mapping, archive: ForwardArchive,
    repository: FinancialFactRepository, trading_calendar: Sequence[date],
) -> tuple[FinancialFact, ...]:
    """Normal unique writer for the bounded, previously absent two-key chain.

    No database write occurs before every source/version check succeeds. Existing
    facts for either key close this initial-forward-root path; an interrupted
    retry must inspect the actual original transaction rather than add roots.
    """
    started = _utc_now()
    rights = default_rights().get("cninfo")
    if not rights.open_for("research_use") or not rights.open_for("local_storage"):
        raise PdfPromotionError("normal CNINFO source rights are unavailable")
    dependency = _accepted_annual_dependency(repository)
    if any(fact.instrument_id in {"601069", "SH.601069"} and fact.metric == "operating_cashflow" and
           fact.period_end in (date(2025, 6, 30), date(2026, 6, 30))
           for fact in dependency.pop("existingFacts")):
        raise PdfPromotionError("initial CFO version-chain keys already have physical facts; inspect the real lineage")
    impact = dependency["evidence"]["revision_impact_proof"]
    current_scope = _check_current_relationship_scope(archive, current_company, impact)
    old, new = _read_report(archive, original_h125, started), _read_report(archive, presenting_h126, started)
    if old["candidate"].period_end != date(2025, 6, 30) or new["candidate"].period_end != date(2026, 6, 30):
        raise PdfPromotionError("source reports are reversed")
    annual_published = date.fromisoformat(dependency["acceptedFactPublishedOn"])
    if not old["published"] < annual_published < new["published"]:
        raise PdfPromotionError("original, annual H1 bridge and later presentation have no proven publication order")
    for report in (original_h125, presenting_h126):
        cross = report.cross_category_index
        if (current_scope["indexReceiptIds"] !=
                [cross.org_lookup_receipt_id, *[page.receipt_id for page in cross.pages]] or
                current_scope["requestBodyHashes"] != [page.request_body_hash for page in cross.pages] or
                list(current_company["publicationWindow"]) != [day.isoformat() for day in cross.publication_window] or
                report.version_review.org_lookup_receipt_id != cross.org_lookup_receipt_id):
            raise PdfPromotionError("version relationships do not use both reports' actual fresh complete index")
    comparative = extract_comparative_cashflow_contract(
        new["body"], instrument_id="601069", source_report_period_end=date(2026, 6, 30))
    old_value = old["candidate"].by_field["operating_cashflow"].value_yuan
    current_value = new["candidate"].by_field["operating_cashflow"].value_yuan
    if comparative.by_role("CURRENT").amount_yuan != current_value:
        raise PdfPromotionError("H126 normal CURRENT differs from the typed native CURRENT")
    relation = derive_h125_cashflow_version_relation(
        impact, original_h125_amount=old_value,
        h126_prior_amount=comparative.by_role("PRIOR").amount_yuan)
    corrected = _corrected_h1_source_proof(archive, current_scope)
    if Decimal(corrected["nativeProof"]["current_amount_yuan"]) != old_value:
        raise PdfPromotionError("the directly corrected H1 net differs from the original-version bridge")
    relation["correctedH1FullStatement"] = corrected
    completed = _utc_now()
    if completed < started:
        raise PdfPromotionError("normal writer clock moved backwards")
    floors = [date_only_available_at(item["published"], trading_calendar)[0] for item in (old, new)]
    dependency_available = _aware(datetime.fromisoformat(dependency["acceptedFactAvailableAt"]),
                                  "annual dependency available_at")
    available = _next_available_snapshot(
        max(completed, dependency_available, *floors, *old["times"], *new["times"]), trading_calendar)
    root_id = _normal_id(date(2025, 6, 30), ORIGINAL_ID, old["candidate"].pdf_sha256)
    current_id = _normal_id(date(2026, 6, 30), PRESENTING_ID, new["candidate"].pdf_sha256)
    prior_identity = f"cninfo|601069|2026-06-30|2025-06-30|PRIOR|operating_cashflow|{PRESENTING_ID}|{new['candidate'].pdf_sha256}"
    prior_id = "mach_cmp_" + hashlib.sha256(prior_identity.encode()).hexdigest()[:32]

    def fact(source: dict, period: date, value: Decimal, version: str, predecessor=None):
        return FinancialFact(
            instrument_id="601069", metric="operating_cashflow", period_end=period,
            statement_scope=StatementScope.CONSOLIDATED, profit_scope=None,
            value=value, currency="CNY", raw_unit="yuan", source_id="cninfo",
            source_document_id=source["review"].version_review.announcement_id,
            source_published_date=source["published"], source_published_at=None,
            timestamp_precision=TimestampPrecision.DATE, first_seen_at=source["firstSeen"],
            ingested_at=completed, available_at=available, availability_basis=AvailabilityBasis.OBSERVED,
            pit_mode=PitMode.LIVE_OBSERVED, content_hash=source["candidate"].pdf_sha256,
            version_id=version, supersedes_id=predecessor)

    facts = (fact(old, date(2025, 6, 30), old_value, root_id),
             fact(new, date(2025, 6, 30), comparative.by_role("PRIOR").amount_yuan, prior_id, root_id),
             fact(new, date(2026, 6, 30), current_value, current_id))
    reports = [_report_evidence(archive, item, target, completed, available)
               for item, target in ((old, facts[0]), (new, facts[2]))]
    evidence = {"schema": SCHEMA, "verifierId": VERIFIER, "humanReviewClaimed": None,
                "rightsRegisterVersion": REGISTER_VERSION, "instrumentId": "601069",
                "metric": "operating_cashflow", "reports": reports,
                "annualDependency": {key: value for key, value in dependency.items() if key != "evidence"},
                "currentRelationshipCompany": dict(current_company),
                "currentRelationshipScope": current_scope,
                "versionRelation": relation, "comparative": comparative.as_evidence(),
                "versionIds": {"H125_ORIGINAL": root_id, "H125_PRESENTED_PRIOR": prior_id, "H126_CURRENT": current_id},
                "ingestedAt": completed.isoformat(), "availableAt": available.isoformat(),
                "tradingCalendar": [day.isoformat() for day in trading_calendar]}
    bundle_id = "cninfo_machine_cfo_chain_" + hashlib.sha256(
        f"601069|{ORIGINAL_ID}|{PRESENTING_ID}|{root_id}|{prior_id}|{current_id}".encode()).hexdigest()[:32]
    repository.append_many(facts, review_bundle=FactReviewBundle(bundle_id, canonical(evidence)))
    return facts


def _report_evidence(archive: ForwardArchive, source: dict, fact: FinancialFact,
                     completed: datetime, available: datetime) -> dict:
    candidate, verification, review = source["candidate"], source["verification"], source["review"]
    database = next(row[2] for row in archive.con.execute("PRAGMA database_list") if row[1] == "main")
    binding = {"root": str(archive.root.resolve()), "database_path": str(Path(database).resolve()),
               "pdf_receipt_id": review.pdf_receipt_id, "document_url": candidate.document_url,
               "content_hash": candidate.pdf_sha256}
    evidence = {"schema": MACHINE_VERIFICATION_SCHEMA, "source_id": "cninfo",
                "rights_register_version": REGISTER_VERSION, "instrument_id": "601069",
                "period_end": candidate.period_end.isoformat(), "announcement_id": review.version_review.announcement_id,
                "announcement_role": "ORIGINAL_REPORT", "company": COMPANY,
                "pdf_receipt_id": review.pdf_receipt_id, "content_hash": candidate.pdf_sha256,
                "document_url": candidate.document_url, "org_lookup_receipt_id": review.version_review.org_lookup_receipt_id,
                "source_published_date": source["published"].isoformat(),
                "first_seen_at": source["firstSeen"].isoformat(), "ingested_at": completed.isoformat(),
                "available_at": available.isoformat(), "version_ids": [fact.version_id],
                "required_fields": ["operating_cashflow"], "verifier_id": VERIFIER_ID,
                "verifier_version": VERIFIER_VERSION, "human_review_claimed": None,
                "fields": [item.as_dict() for item in verification.fields],
                "sections": list(verification.sections), "identities": [item.as_dict() for item in verification.identities],
                "cross_category_index": {"reviewer_id": review.reviewed_by, "reviewed_at": review.reviewed_at.isoformat(),
                                         "candidate_dispositions": source["dispositions"]},
                "version_index_receipt_ids": [row["receipt_id"] for row in source["indexReceipts"]],
                "version_index_request_hashes": source["indexHashes"],
                "cross_category_receipt_ids": [row["receipt_id"] for row in source["crossReceipts"]],
                "cross_category_request_hashes": source["crossHashes"],
                "cross_category_page_receipt_ids": [page.receipt_id for page in review.cross_category_index.pages],
                "cross_category_page_request_hashes": [page.request_body_hash for page in review.cross_category_index.pages],
                "cross_category_publication_window": [day.isoformat() for day in review.cross_category_index.publication_window]}
    evidence["cashflow_chain_source_archive"] = binding
    paths = {check.path for field in verification.fields for check in field.checks if check.holds}
    if "ruled_grid_cell_reextraction" in paths:
        evidence["geometry_archive"] = binding
    if "operating_cashflow_inflow_less_outflow_subtotals" in paths:
        evidence["native_operating_cashflow_archive"] = binding
    return evidence
