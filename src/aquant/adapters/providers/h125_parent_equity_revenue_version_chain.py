"""Finite normal version chain for 601069 H125 parent equity and revenue.

This candidate keeps three distinct source roles: the original H125 report,
the H125 section in the multi-period corrected-statements attachment, and the
H125 PRIOR cells in the later H126 report. The attachment is never treated as
a full report. The public reader must register ``accepts_601069_h125_parent_equity_revenue``
as a review-bundle validator before this writer can be used for normal reads.
At integration, place the unchanged ``corrected_h125_four_numeric_observations``
producer module beside this file; this candidate imports that source producer.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence

from ...domain.data.forward_archive import ForwardArchive
from ...domain.data.pit import AvailabilityBasis, PitMode, TimestampPrecision, date_only_available_at
from ...domain.data.rights import REGISTER_VERSION, default_rights
from ...domain.fundamentals.disclosure_link import DisclosureRole, disclosure_role
from ...domain.fundamentals.fact_repository import FactReviewBundle, FinancialFactRepository
from ...domain.fundamentals.versioned import FinancialFact, ProfitScope, StatementScope
from .cninfo import CrossCategoryIndexResult
from .cninfo_financial_pdf import CninfoS2CandidateFacts, PdfCandidateFact
from .cninfo_probe import normalize_title, title_revision_flags
from .cninfo_statement_sections import (
    ROLE_CONSOLIDATED, extract_section_facts, extract_statement_sections,
)
from .pdf_promotion import (
    CrossCategoryDisposition, PdfPromotionError, PdfVersionReview,
    _archived_ok, _cross_category_evidence, _next_available_snapshot,
    _utc_now, _version_evidence,
)
from .s2_machine_verification import verify_machine_evidence


SCHEMA = "aquant.cninfo.601069-h125-parent-equity-revenue-version-chain.v1"
VERIFIER = "machine:cninfo-601069-h125-parent-equity-revenue-chain-v1"
COMPANY = "西部黄金股份有限公司"
INSTRUMENT_ID = "601069"
H125 = date(2025, 6, 30)
H126 = date(2026, 6, 30)
ORIGINAL_ID = "1224590046"
CORRECTED_ID = "1225082560"
PRESENTING_ID = "1225494335"
ANNUAL_ID = "1225082554"
METRICS = ("parent_equity", "revenue")
H126_PRIOR_METRICS = frozenset({"revenue"})
CORRECTIONS = frozenset({"1225082555", CORRECTED_ID, "1225082606"})
CURRENT_SCOPE_SUPPLEMENT_ID = "1225642214"
CURRENT_SCOPE_SUPPLEMENT_TITLE = (
    "西部黄金股份有限公司关于制定及修订于H股发行上市后适用的公司章程、"
    "相关议事规则及公司内部治理制度的公告"
)
CURRENT_SCOPE_SUPPLEMENT_URL = (
    "https://static.cninfo.com.cn/finalpage/2026-10-30/1225642214.PDF"
)
CURRENT_SCOPE_SUPPLEMENT_DATE = date(2026, 10, 30)
CURRENT_SCOPE_BROAD_END = date(2026, 11, 3)
_STATEMENT_FOR = {
    "parent_equity": "资产负债表",
    "revenue": "利润表",
}
_UNIT_SCALE = {"元": Decimal(1), "千元": Decimal(1000), "万元": Decimal(10000)}


@dataclass(frozen=True, slots=True)
class ReportReviewContext:
    period_end: date
    pdf_receipt_id: str
    version_review: PdfVersionReview
    cross_category_index: CrossCategoryIndexResult
    dispositions: tuple[CrossCategoryDisposition, ...]
    reviewed_by: str
    reviewed_at: datetime


class H125ParentEquityRevenueContractError(PdfPromotionError):
    """A finite H125 parent-equity/revenue source, relation, or lineage contract failed."""


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _nested_first_seen_times(value: object) -> tuple[datetime, ...]:
    """Read archived first-seen clocks from a verified calendar packet."""
    found = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "first_seen_at" and isinstance(child, str):
                parsed = datetime.fromisoformat(child)
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise H125ParentEquityRevenueContractError("calendar receipt time lacks a timezone")
                found.append(parsed)
            else:
                found.extend(_nested_first_seen_times(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_nested_first_seen_times(child))
    return tuple(found)


def _period_header(period: date, *, balance_sheet: bool = False) -> str:
    if balance_sheet:
        return f"{period.year}年{period.month}月{period.day}日"
    if period.month == 6 and period.day == 30:
        return f"{period.year}年半年度"
    raise H125ParentEquityRevenueContractError(f"unsupported H125 target statement period: {period}")


def _source_cell_binding(label: str, current_cell: str, prior_cell: str) -> tuple[tuple[str, str, str], int, int]:
    """Keep native CURRENT and PRIOR cell text at distinct source indexes."""
    if not label or not current_cell or not prior_cell:
        raise H125ParentEquityRevenueContractError("statement row lacks a native label, current cell, or prior cell")
    return (label, current_cell, prior_cell), 1, 2


def _candidate_from_sections(
    body: bytes, *, period_end: date, announcement_id: str, document_url: str,
    fields: Sequence[str] = METRICS,
) -> tuple[CninfoS2CandidateFacts, object]:
    """Build ordinary candidate rows from the existing statement-section reader.

    This is used only for the corrected-statements attachment, whose source role
    is not a report cover. It still requires the ordinary native table parser,
    then the independent machine verifier re-parses the same actual PDF bytes.
    """
    if not isinstance(body, bytes) or not body.startswith(b"%PDF-"):
        raise H125ParentEquityRevenueContractError("statement-section input is not PDF bytes")
    sections = extract_statement_sections(body, period_end=period_end,
                                          roles=(ROLE_CONSOLIDATED,),
                                          statements=tuple(sorted({_STATEMENT_FOR[f] for f in fields})))
    digest = _digest_bytes(body)
    rows = []
    for field in fields:
        section = sections.require(ROLE_CONSOLIDATED, _STATEMENT_FOR[field])
        row = section.find(field)
        if row is None or row.nil_amounts or row.unit not in _UNIT_SCALE:
            raise H125ParentEquityRevenueContractError(f"{field}: H125 consolidated row is not complete")
        source_cells, current_index, prior_index = _source_cell_binding(
            row.label, row.current_cell, row.prior_cell,
        )
        rows.append(PdfCandidateFact(
            field=field, value_yuan=row.amount * _UNIT_SCALE[row.unit],
            period_end=period_end, report_period_text=section.current_header,
            statement={"资产负债表": "balance", "利润表": "profit"}[_STATEMENT_FOR[field]],
            pdf_page=row.page, column_header=section.current_header,
            amount_unit=row.unit, currency="CNY", source_row=row.raw_line.replace("\t", " "),
            source_cells=source_cells, source_current_cell_index=current_index,
            source_prior_cell_index=prior_index, pdf_sha256=digest, display_row_label=row.label,
        ))
    candidate = CninfoS2CandidateFacts(
        instrument_id=INSTRUMENT_ID, period_end=period_end,
        version_label="h125_corrected_section_extracted", document_url=document_url,
        pdf_sha256=digest, candidates=tuple(rows),
    )
    if announcement_id != CORRECTED_ID:
        raise H125ParentEquityRevenueContractError("corrected section announcement identity differs")
    return candidate, sections


def _read_full_report(archive: ForwardArchive, context: ReportReviewContext,
                      *, expected_period: date, expected_id: str, now: datetime) -> dict:
    review = context.version_review
    if (context.period_end != expected_period or review.announcement_id != expected_id or
            review.query_stock_code != INSTRUMENT_ID or review.predecessor_announcement_id is not None or
            not review.reviewer_id.startswith("machine:") or
            not context.reviewed_by.startswith("machine:") or
            any(not item.reviewer_id.startswith("machine:") for item in context.dispositions)):
        raise H125ParentEquityRevenueContractError("full-report context has the wrong issuer, period, or source role")
    receipt, body = _archived_ok(archive, context.pdf_receipt_id,
                                 expected_host="static.cninfo.com.cn")
    candidate = extract_section_facts(
        body, instrument_id=INSTRUMENT_ID, company=COMPANY, period_end=expected_period,
        announcement_id=expected_id, document_url=receipt["url"], required_fields=METRICS,
    )
    verification = verify_machine_evidence(body, candidate=candidate, expected_fields=METRICS)
    if not verification.verified:
        raise H125ParentEquityRevenueContractError(f"{expected_id}: normal two-field strict verification failed")
    published, role, index_receipts, index_hashes = _version_evidence(
        archive, candidate, review, now=now)
    if role is not DisclosureRole.ORIGINAL_REPORT:
        raise H125ParentEquityRevenueContractError(f"{expected_id}: expected an original full report")
    cross_receipts, cross_hashes, dispositions = _cross_category_evidence(
        archive, candidate, context.cross_category_index, context.dispositions,
        review=review, reviewed_by=context.reviewed_by, reviewed_at=context.reviewed_at,
        pdf_receipt_id=context.pdf_receipt_id, now=now,
    )
    first_seen = datetime.fromisoformat(receipt["first_seen_at"]).astimezone(timezone.utc)
    times = [first_seen, review.reviewed_at, context.reviewed_at]
    times.extend(datetime.fromisoformat(item["first_seen_at"])
                 for item in (*index_receipts, *cross_receipts))
    if any(item.tzinfo is None or item > now for item in times):
        raise H125ParentEquityRevenueContractError("source or review time is after validation")
    return {
        "context": context, "candidate": candidate, "verification": verification,
        "receipt": receipt, "body": body, "published": published, "role": role,
        "firstSeen": first_seen, "indexReceipts": index_receipts,
        "indexHashes": index_hashes, "crossReceipts": cross_receipts,
        "crossHashes": cross_hashes, "dispositions": dispositions, "times": times,
    }


def _read_corrected_h125(archive: ForwardArchive, binding: Mapping[str, object], *,
                         source_section_period_end: date) -> dict:
    from .corrected_h125_four_numeric_observations import (
        extract_corrected_h125_four_numeric_observations,
    )

    if source_section_period_end != H125:
        raise H125ParentEquityRevenueContractError("corrected attachment section must be H125")
    if (binding.get("announcementId") != CORRECTED_ID or
            not isinstance(binding.get("receiptId"), str) or
            not isinstance(binding.get("documentUrl"), str) or
            not isinstance(binding.get("publishedDate"), str)):
        raise H125ParentEquityRevenueContractError("current source scope lacks the corrected H125 attachment")
    receipt, body = _archived_ok(
        archive, str(binding["receiptId"]), expected_url=str(binding["documentUrl"]),
        expected_host="static.cninfo.com.cn")
    if (receipt["content_hash"] != binding.get("contentHash") or
            normalize_title(str(binding.get("title", ""))) !=
            "西部黄金股份有限公司关于前期会计差错更正后的财务报表及附注"):
        raise H125ParentEquityRevenueContractError("corrected attachment index/receipt identity differs")
    proof = extract_corrected_h125_four_numeric_observations(
        body, instrument_id=INSTRUMENT_ID, announcement_id=CORRECTED_ID,
        document_url=receipt["url"], period_end=H125)
    native = proof.as_evidence()
    document = native.get("document", {})
    if (native.get("sourceOnly") is not True or native.get("qualificationDecision") is not False or
            native.get("formalAdmission") != "NOT_ADMITTED" or
            document.get("documentRole") != "CORRECTED_FINANCIAL_STATEMENTS" or
            document.get("identityBasis") != "INDEX_BOUND_ONLY" or
            document.get("literalSourceReportTypeCode") is not None or
            document.get("documentContainsMultipleCorrectedSections") is not True or
            document.get("sourceSectionPeriodEnd") != H125.isoformat()):
        raise H125ParentEquityRevenueContractError("corrected attachment cannot be represented as a full report")
    cells = native.get("fields")
    if not isinstance(cells, Mapping) or not set(METRICS) <= set(cells):
        raise H125ParentEquityRevenueContractError("corrected attachment lacks target native fields")
    for metric in METRICS:
        item = cells[metric]
        current = item.get("current")
        context = item.get("statementContext")
        expected_statement = _STATEMENT_FOR[metric]
        if (not isinstance(current, Mapping) or current.get("columnRole") != "CURRENT" or
                current.get("factPeriodEnd") != H125.isoformat() or
                current.get("currency") != "CNY" or current.get("amountUnit") != "元" or
                not isinstance(context, Mapping) or
                context.get("documentRole") != "CORRECTED_FINANCIAL_STATEMENTS" or
                context.get("statementScope") != "CONSOLIDATED" or
                context.get("statement") != expected_statement or
                context.get("sourceSectionPeriodEnd") != H125.isoformat() or
                context.get("unit") != "元" or context.get("currency") != "CNY"):
            raise H125ParentEquityRevenueContractError(f"{metric}: corrected H125 current role/unit is invalid")
        native_cell = current.get("nativeCell")
        if (not isinstance(native_cell, Mapping) or
                native_cell.get("amountYuan") != current.get("amountYuan") or
                native_cell.get("geometryBasis") != "EXACT_NATIVE_GLYPH_EXTENTS" or
                native_cell.get("cellRepresentation") != "NATIVE_AMOUNT_TOKEN"):
            raise H125ParentEquityRevenueContractError(f"{metric}: corrected H125 amount lacks complete native-cell evidence")
    native = {**native, "fields": {metric: cells[metric] for metric in METRICS}}
    candidate, sections = _candidate_from_sections(
        body, period_end=H125, announcement_id=CORRECTED_ID, document_url=receipt["url"],
        fields=METRICS)
    verification = verify_machine_evidence(body, candidate=candidate, expected_fields=METRICS)
    if not verification.verified:
        raise H125ParentEquityRevenueContractError("corrected H125 section did not pass independent two-field strict verification")
    for metric in METRICS:
        proof_field = cells[metric]
        current = proof_field["current"]
        section_context = proof_field["statementContext"]
        item = candidate.by_field[metric]
        if (item.value_yuan != Decimal(str(current["amountYuan"])) or
                item.value_yuan != Decimal(str(proof_field["current"]["nativeCell"]["amountYuan"]))):
            raise H125ParentEquityRevenueContractError(f"{metric}: native source and strict H125 section disagree")
        if (item.pdf_page not in current["sourcePhysicalPages"] or
                item.amount_unit != section_context["unit"] or item.currency != section_context["currency"] or
                item.column_header != current["sourceColumnHeader"]["text"]):
            raise H125ParentEquityRevenueContractError(f"{metric}: native source and strict H125 section disagree")
    return {"receipt": receipt, "body": body, "proof": native, "candidate": candidate,
            "verification": verification, "published": date.fromisoformat(str(binding["publishedDate"])),
            "firstSeen": datetime.fromisoformat(receipt["first_seen_at"]).astimezone(timezone.utc),
            "binding": dict(binding), "sections": sections}


def _read_h126_h125_prior(report: Mapping[str, object]) -> tuple[dict[str, dict], dict]:
    candidate = report["candidate"]
    body = report["body"]
    sections = extract_statement_sections(
        body, period_end=H126, roles=(ROLE_CONSOLIDATED,),
        statements=("资产负债表", "利润表"),
    )
    income = sections.require(ROLE_CONSOLIDATED, "利润表")
    if (income.current_header != _period_header(H126) or
            income.prior_header != _period_header(H125)):
        raise H125ParentEquityRevenueContractError("H126 income statement columns are not CURRENT=2026H1 / PRIOR=2025H1")
    observations: dict[str, dict] = {}
    for metric in sorted(H126_PRIOR_METRICS):
        row = income.find(metric)
        if row is None or row.nil_amounts or row.unit not in _UNIT_SCALE or not row.prior_cell:
            raise H125ParentEquityRevenueContractError(f"H126 prior {metric} cell is missing")
        value = row.prior_amount * _UNIT_SCALE[row.unit]
        observations[metric] = {
            "sourceReportPeriodEnd": H126.isoformat(), "factPeriodEnd": H125.isoformat(),
            "columnRole": "PRIOR", "columnHeader": income.prior_header,
            "currentColumnHeader": income.current_header, "statement": "利润表",
            "statementScope": "CONSOLIDATED", "amountYuan": str(value),
            "amountUnit": row.unit, "currency": "CNY", "page": row.page,
            "rowLabel": row.label, "sourceLine": row.raw_line,
            "currentCell": row.current_cell, "priorCell": row.prior_cell,
        }
    balance = sections.require(ROLE_CONSOLIDATED, "资产负债表")
    row = balance.find("parent_equity")
    if row is None or balance.prior_header != _period_header(date(2025, 12, 31), balance_sheet=True):
        raise H125ParentEquityRevenueContractError("H126 parent-equity prior period cannot be positively excluded")
    excluded = {
        "sourceReportPeriodEnd": H126.isoformat(), "factPeriodEnd": "2025-12-31",
        "columnRole": "PRIOR", "columnHeader": balance.prior_header,
        "amountYuan": str(row.prior_amount * _UNIT_SCALE[row.unit]),
        "reason": "2025-12-31 differs from the H125 2025-06-30 fact period",
    }
    # The report's CURRENT strict pass was performed by _read_full_report.
    # The prior columns above remain role-specific native observations.
    return observations, excluded


def _annual_h1_bridges(annual_impact: Mapping[str, object], corrected_values: Mapping[str, Decimal],
                       prior_values: Mapping[str, Decimal]) -> dict[str, dict]:
    """Bind the accepted FY25 revenue H1 before/after bridge exactly."""
    outcomes = annual_impact.get("outcomes")
    if not isinstance(outcomes, list):
        raise H125ParentEquityRevenueContractError("accepted annual review lacks its outcome list")
    matches = [item for item in outcomes
               if isinstance(item, Mapping) and item.get("metric") == "revenue"]
    if len(matches) != 1:
        raise H125ParentEquityRevenueContractError("accepted annual review lacks the revenue H1 relationship outcome")
    proof = matches[0].get("impactProof")
    scope = proof.get("scope") if isinstance(proof, Mapping) else None
    chain = scope.get("annualQuarterRoleChain") if isinstance(scope, Mapping) else None
    h1 = chain.get(H125.isoformat()) if isinstance(chain, Mapping) else None
    if not isinstance(h1, Mapping):
        raise H125ParentEquityRevenueContractError("accepted annual review lacks the explicit revenue H1 bridge")
    before = Decimal(str(h1.get("disclosedPeriodicRaw")))
    after = Decimal(str(h1.get("annualReportAdjustedRaw")))
    cumulative = Decimal(str(h1.get("annualQuarterCumulativeYuan")))
    adjustment = Decimal(str(h1.get("annualAdjustmentRaw")))
    if (before != corrected_values["revenue"] or after != prior_values["revenue"] or
            cumulative != after or adjustment != after - before or
            h1.get("p10UnitStatus") != "BOUND_FROM_SAME_SECTION"):
        raise H125ParentEquityRevenueContractError("accepted annual revenue H1 bridge conflicts with source columns")
    return {"revenue": {
        "announcementId": ANNUAL_ID, "sourceReportPeriodEnd": "2025-12-31",
        "factPeriodEnd": H125.isoformat(), "columnRole": "EXPLICIT_H1_RECONCILIATION",
        "disclosedPeriodicYuan": str(before), "annualAdjustedYuan": str(after),
        "annualQuarterCumulativeYuan": str(cumulative), "adjustmentYuan": str(adjustment),
        "nativeUnitStatus": h1["p10UnitStatus"],
    }}


def _accepted_annual_dependency(repository: FinancialFactRepository) -> dict:
    """Resolve one FY25 revenue review through the unchanged ordinary reader gate.

    The writer needs only the exact annual dependency cohort; reading the whole
    fact store is cheap, but replaying every unrelated review is not required
    for this finite chain qualification.
    """
    from ...domain.fundamentals.fact_repository import (
        _accepted_machine_verified_review, _accepted_review_group_links, _decode,
    )
    store = repository.load()
    by_id = {item.version_id: item for item in store.facts}
    annual = [fact for fact in store.facts if
              fact.instrument_id in (INSTRUMENT_ID, "SH." + INSTRUMENT_ID) and
              fact.period_end == date(2025, 12, 31) and fact.source_id == "cninfo" and
              fact.source_document_id == ANNUAL_ID and fact.metric == "revenue"]
    aliases = {fact.instrument_id for fact in annual}
    if len(annual) != 1 or len(aliases) != 1:
        raise H125ParentEquityRevenueContractError("FY25 annual review is not accepted as one normal-reader cohort")
    fact = annual[0]
    con = sqlite3.connect(Path(repository.db_path).resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        current_fact = con.execute(
            "SELECT * FROM financial_fact WHERE version_id=?", (fact.version_id,)
        ).fetchone()
        if current_fact is None or _decode(current_fact) != fact:
            raise H125ParentEquityRevenueContractError("accepted FY25 fact changed after the ordinary reader snapshot")
        membership = con.execute(
            "SELECT bundle_id FROM financial_fact_review_member WHERE version_id=?", (fact.version_id,)
        ).fetchall()
        if len(membership) != 1:
            raise H125ParentEquityRevenueContractError("FY25 revenue fact lacks one normal review membership")
        bundle_id = str(membership[0]["bundle_id"])
        row = con.execute(
            "SELECT * FROM financial_fact_review WHERE bundle_id=?", (bundle_id,)
        ).fetchone()
        member_ids = {str(item[0]) for item in con.execute(
            "SELECT version_id FROM financial_fact_review_member WHERE bundle_id=?", (bundle_id,))}
        if row is None or not member_ids or not member_ids <= by_id.keys():
            raise H125ParentEquityRevenueContractError("accepted FY25 annual review cohort is incomplete")
        evidence_hash = str(row["evidence_hash"])
        raw = row["evidence_json"]
        evidence = json.loads(raw)
        if (_canonical(evidence) != raw or _digest_bytes(raw.encode()) != evidence_hash or
                evidence.get("announcement_id") != ANNUAL_ID or
                evidence.get("period_end") != "2025-12-31" or
                not isinstance(evidence.get("revision_impact_proof"), Mapping)):
            raise H125ParentEquityRevenueContractError("accepted annual review lacks its receipt-bound relationship proof")
        if not _accepted_machine_verified_review(row, member_ids, by_id):
            raise H125ParentEquityRevenueContractError("FY25 annual review failed the unchanged ordinary machine reader gate")
        report_key = (fact.instrument_id, fact.period_end.isoformat(), ANNUAL_ID)
        accepted = _accepted_review_group_links(report_key, [(row, member_ids)], by_id)
        if accepted is None or fact.version_id not in accepted:
            raise H125ParentEquityRevenueContractError("FY25 annual review did not produce an ordinary reader link")
        supplement = con.execute(
            "SELECT * FROM financial_fact_proof_supplement WHERE original_bundle_id=?", (bundle_id,)
        ).fetchone()
        if supplement is not None:
            # The current accepted FY25 cohort has no proof supplement. Refuse
            # to silently select an alternate dependency path if that changes.
            raise H125ParentEquityRevenueContractError("FY25 annual dependency unexpectedly requires a proof supplement")
        existing = tuple(_decode(item) for item in con.execute(
            "SELECT * FROM financial_fact WHERE instrument_id IN ('601069','SH.601069') "
            "AND metric IN ('parent_equity','revenue') "
            "AND period_end='2025-06-30'"))
        if con.total_changes:
            raise H125ParentEquityRevenueContractError("annual dependency lookup changed the fact warehouse")
    finally:
        if con.in_transaction:
            con.execute("ROLLBACK")
        con.close()
    return {
        "fact": fact, "bundleId": bundle_id, "evidenceHash": evidence_hash,
        "evidence": evidence, "acceptedLinks": accepted,
        "existingH125Facts": existing,
    }


def _check_current_relationship_scope(archive: ForwardArchive, company: Mapping,
                                      annual_impact: Mapping) -> dict:
    """Replay the original 37 sources plus one receipt-bound Nov3 scope source."""
    from . import s2_revision_impact as impact

    if (company.get("code") != INSTRUMENT_ID or company.get("instrumentId") != "SH.601069" or
            list(company.get("publicationWindow", ())) != ["2024-06-30", "2026-11-03"]):
        raise H125ParentEquityRevenueContractError("current relationship descriptor issuer differs")
    entries, receipts, request_hashes = impact._index(archive.con, archive.root, company)
    if any(datetime.fromisoformat(row["first_seen_at"]).astimezone(
            timezone(timedelta(hours=8))).date() != CURRENT_SCOPE_BROAD_END for row in receipts):
        raise H125ParentEquityRevenueContractError("current complete index includes a non-Nov3 observation")
    previous = annual_impact.get("relationshipScope", {})
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
    if (financial != CORRECTIONS or periods != previous.get("reportCandidatePeriods") or
            previous.get("missingReportSourceIds") != [] or
            not previous.get("completeForArchivedIndexResponse")):
        raise H125ParentEquityRevenueContractError("fresh complete index contains a changed or unresolved version set")
    documents = [*company.get("selectedReportRecords", ()), *company.get("relatedDocumentRecords", ())]
    by_id = {str(item["announcementId"]): item for item in documents}
    old_sources = {str(item["announcementId"]): item for item in annual_impact.get("sources", ())}
    extra_ids = set(by_id) - old_sources.keys()
    if (len(by_id) != len(documents) or extra_ids != {CURRENT_SCOPE_SUPPLEMENT_ID} or
            not recalled <= by_id.keys() or not set(periods) <= by_id.keys() or
            {str(item["announcementId"]) for item in company.get("selectedReportRecords", ())} !=
            set(previous.get("selectedReportIds", ()))):
        raise H125ParentEquityRevenueContractError("current original-source set is incomplete or lacks its exact supplement")
    bindings, supplemental = [], []
    for aid, document in sorted(by_id.items()):
        entry = entries.get(aid)
        if (entry is None or document.get("code") != INSTRUMENT_ID or
                document.get("instrumentId") != "SH.601069" or
                document.get("documentUrl") != entry["documentUrl"] or
                normalize_title(document["title"]) != entry["title"] or
                document.get("currentIndexReceiptId") != entry["indexReceiptId"] or
                document.get("currentIndexResponseHash") != entry["indexResponseHash"]):
            raise H125ParentEquityRevenueContractError("current source identity/date differs from current index")
        receipt, payload = _archived_ok(
            archive, document["receipt"]["receipt_id"], expected_url=entry["documentUrl"],
            expected_host="static.cninfo.com.cn")
        if aid in old_sources:
            old = old_sources[aid]
            if (entry["documentUrl"] != old["index"]["documentUrl"] or
                    entry["publishedDate"] != old["index"]["publishedDate"] or
                    entry["title"] != old["index"]["title"] or
                    receipt != old["receipt"] or document.get("contentHash") != old["originalHash"] or
                    receipt["content_hash"] != old["originalHash"] or
                    len(payload) != old["originalBytes"] or
                    len(payload) != int(document.get("byteSize", -1)) or not payload.startswith(b"%PDF-")):
                raise H125ParentEquityRevenueContractError("one of the original 37 source receipts or bytes changed")
            bindings.append({"announcementId": aid, "receiptId": receipt["receipt_id"],
                             "contentHash": receipt["content_hash"], "documentUrl": entry["documentUrl"],
                             "title": entry["title"], "publishedDate": entry["publishedDate"],
                             "firstSeenAt": receipt["first_seen_at"]})
        else:
            supplemental.append(_governance_source_evidence(
                archive, document, entry, company=company))
    if len(bindings) != len(old_sources) or len(supplemental) != 1:
        raise H125ParentEquityRevenueContractError("current source set does not contain 37 original and one current supplement")
    return {
        "reportCandidatePeriods": periods, "financialCauseIds": sorted(financial),
        "allRecalledRevisionIds": sorted(recalled), "requestBodyHashes": request_hashes,
        "indexReceiptIds": [row["receipt_id"] for row in receipts],
        "indexReceiptFirstSeenAt": [row["first_seen_at"] for row in receipts],
        "originalSources": bindings, "originalSourceCount": len(bindings),
        "currentSourceSupplementCount": len(supplemental),
        "currentSourceSupplements": supplemental,
        "currentSourceCount": len(bindings) + len(supplemental),
    }


def _replay_current_annual_scope(archive: ForwardArchive, company: Mapping,
                                 annual_impact: Mapping, saved: Mapping | None = None) -> dict:
    """Reassess FY25 outcomes against the new complete 38-source Nov3 set.

    The accepted 37-source annual proof remains verbatim historical evidence;
    this stores a separate current-scope replay and requires all prior outcomes
    to remain identical.
    """
    from . import s2_revision_impact as impact

    old_outcomes = annual_impact.get("outcomes")
    if not isinstance(old_outcomes, list) or not old_outcomes:
        raise H125ParentEquityRevenueContractError("accepted annual proof has no outcome list")
    metrics = tuple(sorted({str(item.get("metric")) for item in old_outcomes
                            if isinstance(item, Mapping) and item.get("metric")}))
    if not metrics or len(metrics) != len(old_outcomes):
        raise H125ParentEquityRevenueContractError("accepted annual outcome metric set is malformed")
    inputs = {"archive_root": archive.root, "company": company,
              "target_period_end": date(2025, 12, 31), "target_metrics": metrics,
              "issuer_name": COMPANY}
    if saved is None:
        current = impact.assess_revision_impact(**inputs).as_dict()
    else:
        if not isinstance(saved, Mapping):
            raise H125ParentEquityRevenueContractError("current-scope impact replay is missing")
        saved_proof = saved.get("currentScopeImpactProof")
        if not isinstance(saved_proof, Mapping):
            raise H125ParentEquityRevenueContractError(
                "current-scope impact refresh lacks its canonical nested impact proof")
        impact.replay_revision_impact(saved_proof, **inputs)
        current = dict(saved_proof)
    current_sources = {str(item.get("announcementId")) for item in current.get("sources", ())}
    old_sources = {str(item.get("announcementId")) for item in annual_impact.get("sources", ())}
    current_scope = current.get("relationshipScope")
    old_scope = annual_impact.get("relationshipScope")
    if (current_sources != old_sources | {CURRENT_SCOPE_SUPPLEMENT_ID} or
            len(current.get("sources", ())) != 38 or not isinstance(current_scope, Mapping) or
            not isinstance(old_scope, Mapping) or
            current_scope.get("reportCandidatePeriods") != old_scope.get("reportCandidatePeriods") or
            current_scope.get("selectedReportIds") != old_scope.get("selectedReportIds") or
            current_scope.get("financialCauseIds") != old_scope.get("financialCauseIds") or
            current_scope.get("missingReportSourceIds") != [] or
            current_scope.get("completeForArchivedIndexResponse") is not True):
        raise H125ParentEquityRevenueContractError(
            "replayed FY25 annual source scope is not the original 37 plus the one current supplement")
    current_outcomes = current.get("outcomes")
    old_by_metric = {str(item.get("metric")): item for item in old_outcomes}
    new_by_metric = {str(item.get("metric")): item for item in current_outcomes or ()}
    if set(new_by_metric) != set(old_by_metric):
        raise H125ParentEquityRevenueContractError("replayed FY25 outcome metric set changed")
    outcome_checks = []
    for metric in metrics:
        old_hash = _digest_bytes(_canonical(old_by_metric[metric]).encode("utf-8"))
        new_hash = _digest_bytes(_canonical(new_by_metric[metric]).encode("utf-8"))
        if old_hash != new_hash:
            raise H125ParentEquityRevenueContractError(
                f"FY25 {metric} cause/timing/outcome changed under current source replay")
        outcome_checks.append({"metric": metric, "historicalOutcomeHash": old_hash,
                               "currentScopeOutcomeHash": new_hash, "unchanged": True})
    return {
        "schema": "aquant.601069.fy25-current-38-source-impact-replay.v1",
        "historicalAnnualProofSha256": _digest_bytes(_canonical(annual_impact).encode("utf-8")),
        "historicalSourceCount": len(old_sources), "currentSourceCount": len(current_sources),
        "newCurrentSupplementId": CURRENT_SCOPE_SUPPLEMENT_ID,
        "outcomeChecks": outcome_checks,
        "currentScopeImpactProof": current,
    }


def _report_context_evidence(archive: ForwardArchive, report: Mapping) -> dict:
    context: ReportReviewContext = report["context"]
    review = context.version_review
    cross = context.cross_category_index
    database = next(item[2] for item in archive.con.execute("PRAGMA database_list") if item[1] == "main")
    return {
        "periodEnd": context.period_end.isoformat(), "pdfReceiptId": context.pdf_receipt_id,
        "documentUrl": report["candidate"].document_url,
        "contentHash": report["candidate"].pdf_sha256,
        "sourcePublishedDate": report["published"].isoformat(),
        "firstSeenAt": report["firstSeen"].isoformat(),
        "versionReview": _version_review_fields(review),
        "crossCategoryIndex": _cross_index_fields(cross),
        "crossCategoryDispositions": _disposition_inputs(context.dispositions),
        "crossCategoryReviewedBy": context.reviewed_by,
        "crossCategoryReviewedAt": context.reviewed_at.isoformat(),
        "versionIndexReceiptIds": [item["receipt_id"] for item in report["indexReceipts"]],
        "versionIndexRequestHashes": report["indexHashes"],
        "crossCategoryReceiptIds": [item["receipt_id"] for item in report["crossReceipts"]],
        "crossCategoryRequestHashes": report["crossHashes"],
        "crossCategoryPageReceiptIds": [page.receipt_id for page in cross.pages],
        "crossCategoryPageRequestHashes": [page.request_body_hash for page in cross.pages],
        "crossCategoryPublicationWindow": [day.isoformat() for day in cross.publication_window],
        "normalCandidate": _candidate_fields(report["candidate"]),
        "normalVerification": report["verification"].as_evidence(),
        "archive": {"root": str(archive.root.resolve()), "databasePath": str(Path(database).resolve())},
    }


def _candidate_fields(candidate: CninfoS2CandidateFacts) -> dict:
    return {
        "instrumentId": candidate.instrument_id, "periodEnd": candidate.period_end.isoformat(),
        "announcementId": candidate.document_url.rsplit("/", 1)[-1].split(".", 1)[0],
        "documentUrl": candidate.document_url, "contentHash": candidate.pdf_sha256,
        "fields": [{"field": item.field, "valueYuan": str(item.value_yuan),
                    "statement": item.statement, "page": item.pdf_page,
                    "header": item.column_header, "amountUnit": item.amount_unit,
                    "currency": item.currency, "sourceRow": item.source_row,
                    "sourceCells": list(item.source_cells),
                    "displayRowLabel": item.display_row_label}
                   for item in candidate.candidates],
    }


def _version_review_fields(value: PdfVersionReview) -> dict:
    return {
        "announcementId": value.announcement_id,
        "orgLookupReceiptId": value.org_lookup_receipt_id,
        "indexReceiptId": value.index_receipt_id,
        "searchReceiptIds": list(value.search_receipt_ids),
        "searchPageNumbers": list(value.search_page_numbers),
        "queryStockCode": value.query_stock_code,
        "queryOrganizationId": value.query_organization_id,
        "queryPeriodStart": value.query_period_start.isoformat(),
        "queryPeriodEnd": value.query_period_end.isoformat(),
        "queryDigest": value.query_digest, "reviewerId": value.reviewer_id,
        "reviewedAt": value.reviewed_at.isoformat(),
        "completeSearchAttested": value.complete_search_attested,
        "predecessorAnnouncementId": value.predecessor_announcement_id,
        "correctionReceiptId": value.correction_receipt_id,
        "correctionExcerpt": value.correction_excerpt,
    }


def _cross_index_fields(value: CrossCategoryIndexResult) -> dict:
    return {
        "stockCode": value.stock_code, "organizationId": value.organization_id,
        "reportPeriod": value.report_period,
        "publicationWindow": [day.isoformat() for day in value.publication_window],
        "orgLookupReceiptId": value.org_lookup_receipt_id,
        "orgLookupContentHash": value.org_lookup_content_hash,
        "pages": [{"pageNum": item.page_num, "requestBodyHash": item.request_body_hash,
                   "receiptId": item.receipt_id, "contentHash": item.content_hash,
                   "httpStatus": item.http_status,
                   "rawAnnouncementCount": item.raw_announcement_count,
                   "totalAnnouncement": item.total_announcement, "hasMore": item.has_more}
                  for item in value.pages],
        "announcements": [{"announcementId": item.announcement_id, "secCode": item.sec_code,
                           "title": item.title, "announcementTimeMs": item.announcement_time_ms,
                           "documentUrl": item.document_url,
                           "candidateReasons": list(item.candidate_reasons), "pageNum": item.page_num,
                           "pageReceiptId": item.page_receipt_id,
                           "pageContentHash": item.page_content_hash}
                          for item in value.announcements],
        "skippedDifferentSecurity": value.skipped_different_security,
        "totalAnnouncement": value.total_announcement, "complete": value.complete,
        "termination": value.termination, "error": value.error,
    }


def _disposition_inputs(values: Sequence[CrossCategoryDisposition]) -> list[dict]:
    return [{"announcementId": item.announcement_id, "relevance": item.relevance,
             "reviewerId": item.reviewer_id, "reviewedAt": item.reviewed_at.isoformat(),
             "rationale": item.rationale, "documentReceiptId": item.document_receipt_id,
             "relatedReportAnnouncementIds": list(item.related_report_announcement_ids),
             "relationReceiptId": item.relation_receipt_id,
             "relationExcerpt": item.relation_excerpt} for item in values]


def _version_id(metric: str, announcement: str, digest: str, role: str,
                predecessor: str | None = None) -> str:
    if role == "ORIGINAL_CURRENT":
        identity = f"cninfo|{INSTRUMENT_ID}|{H125}|{metric}|{announcement}|{digest}"
        prefix = "mach_"
    elif role == "CORRECTED_CURRENT":
        identity = f"cninfo|{INSTRUMENT_ID}|{H125}|{metric}|{announcement}|{digest}|CORRECTED_CURRENT|{predecessor}"
        prefix = "mach_rev_"
    elif role == "PRESENTED_PRIOR":
        identity = f"cninfo|{INSTRUMENT_ID}|{H126}|{H125}|PRIOR|{metric}|{announcement}|{digest}"
        prefix = "mach_cmp_"
    else:
        raise H125ParentEquityRevenueContractError("unsupported finite version role")
    return prefix + hashlib.sha256(identity.encode()).hexdigest()[:32]


def _scope_by_id(scope: Mapping[str, object]) -> dict[str, dict]:
    return {str(item["announcementId"]): dict(item) for item in scope.get("originalSources", ())}


def _governance_source_evidence(archive: ForwardArchive, document: Mapping[str, object],
                                entry: Mapping[str, object], *, company: Mapping[str, object]) -> dict:
    """Replay the one Nov3 current H-share governance source supplement.

    This adds source coverage to the new review only. It neither changes the
    accepted FY25 review proof nor assigns a financial effect outcome.
    """
    if (document.get("announcementId") != CURRENT_SCOPE_SUPPLEMENT_ID or
            document.get("code") != INSTRUMENT_ID or
            document.get("instrumentId") != "SH." + INSTRUMENT_ID or
            document.get("title") != CURRENT_SCOPE_SUPPLEMENT_TITLE or
            normalize_title(str(document.get("title", ""))) != entry.get("title") or
            document.get("documentUrl") != CURRENT_SCOPE_SUPPLEMENT_URL or
            entry.get("documentUrl") != CURRENT_SCOPE_SUPPLEMENT_URL or
            document.get("publishedDate") != CURRENT_SCOPE_SUPPLEMENT_DATE.isoformat() or
            entry.get("publishedDate") != CURRENT_SCOPE_SUPPLEMENT_DATE.isoformat() or
            document.get("currentIndexReceiptId") != entry.get("indexReceiptId") or
            document.get("currentIndexResponseHash") != entry.get("indexResponseHash") or
            company.get("code") != INSTRUMENT_ID or
            company.get("instrumentId") != "SH." + INSTRUMENT_ID or
            company.get("organizationId") is None):
        raise H125ParentEquityRevenueContractError(
            "current source supplement index/company/date contract differs")
    receipt, body = _archived_ok(
        archive, str(document.get("receipt", {}).get("receipt_id")),
        expected_url=CURRENT_SCOPE_SUPPLEMENT_URL,
        expected_host="static.cninfo.com.cn")
    if (not body.startswith(b"%PDF-") or len(body) != int(document.get("byteSize", -1)) or
            len(body) != int(receipt.get("byte_size", -2)) or
            receipt.get("content_hash") != document.get("contentHash") or
            receipt.get("first_seen_at") != document.get("receipt", {}).get("first_seen_at")):
        raise H125ParentEquityRevenueContractError(
            "current source supplement PDF receipt/hash/size contract differs")
    try:
        from pypdf import PdfReader
        pages = tuple((page.extract_text() or "") for page in PdfReader(io.BytesIO(body), strict=False).pages)
    except Exception as exc:
        raise H125ParentEquityRevenueContractError(
            f"current source supplement PDF text cannot be replayed: {type(exc).__name__}: {exc}") from exc
    if not pages:
        raise H125ParentEquityRevenueContractError("current source supplement PDF has no readable pages")
    compact_pages = tuple(re.sub(r"\s+", "", page) for page in pages)
    compact_text = "".join(compact_pages)
    required_terms = (COMPANY, "H股", "发行上市", "公司章程", "议事规则", "治理制度")
    missing_terms = [term for term in required_terms if term not in compact_text]
    if missing_terms:
        raise H125ParentEquityRevenueContractError(
            f"current source supplement body does not bind its issuer/governance scope: {missing_terms}")
    code_pattern = re.compile(r"(?:证券代码|股票代码|公司代码|证券代号)[:：]?([0-9]{6})")
    literal_codes = sorted({match for page in compact_pages for match in code_pattern.findall(page)})
    if any(code != INSTRUMENT_ID for code in literal_codes):
        raise H125ParentEquityRevenueContractError(
            f"current source supplement contains a contradictory literal security code: {literal_codes}")
    date_pattern = re.compile(r"20[0-9]{2}(?:年[0-9]{1,2}月[0-9]{1,2}日|[-/.][0-9]{1,2}[-/.][0-9]{1,2})")
    printed_dates = sorted({match for page in compact_pages for match in date_pattern.findall(page)})
    topics = []
    for term in ("H股", "发行上市", "公司章程", "议事规则", "治理制度"):
        for page_number, text in enumerate(compact_pages, 1):
            position = text.find(term)
            if position >= 0:
                topics.append({"term": term, "page": page_number,
                               "excerpt": text[max(0, position - 80):position + len(term) + 160]})
                break
    observed = datetime.fromisoformat(str(receipt["first_seen_at"]))
    if (observed.tzinfo is None or
            observed.astimezone(timezone(timedelta(hours=8))).date() != CURRENT_SCOPE_BROAD_END):
        raise H125ParentEquityRevenueContractError(
            "current source supplement receipt was not observed on the authorized Nov3 window")
    return {
        "schema": "aquant.601069.current-index-revision-source-supplement.v1",
        "announcementId": CURRENT_SCOPE_SUPPLEMENT_ID, "sourceRole": "CURRENT_GOVERNANCE_REVISION_CONTEXT",
        "code": INSTRUMENT_ID, "instrumentId": "SH." + INSTRUMENT_ID,
        "organizationId": company["organizationId"], "companyName": COMPANY,
        "title": CURRENT_SCOPE_SUPPLEMENT_TITLE,
        "documentUrl": CURRENT_SCOPE_SUPPLEMENT_URL,
        "publishedDateFromCurrentIndex": CURRENT_SCOPE_SUPPLEMENT_DATE.isoformat(),
        "currentIndexReceiptId": entry["indexReceiptId"],
        "currentIndexResponseHash": entry["indexResponseHash"],
        "pdfReceiptId": receipt["receipt_id"], "pdfContentHash": receipt["content_hash"],
        "pdfByteSize": len(body), "pdfFirstSeenAt": receipt["first_seen_at"],
        "pdfSha256Recomputed": _digest_bytes(body),
        "pdfPageCount": len(pages), "pdfTextSha256": _digest_bytes("\n".join(pages).encode("utf-8")),
        "literalCompanyNamePresent": True, "literalSecurityCodes": literal_codes,
        "identityBasis": "OFFICIAL_CURRENT_INDEX_CODE_ORG_AND_PDF_COMPANY_TEXT",
        "printedDateTokens": printed_dates, "governanceTopicExcerpts": topics,
        "financialEffectOutcome": None,
        "boundary": "This supplements the Nov3 current index source set; it leaves the original 37-source FY25 accepted review and its outcome proof unchanged.",
    }


def _ensure_correction_related(report: Mapping, report_id: str) -> None:
    input_context: ReportReviewContext = report["context"]
    by_id = {item.announcement_id: item for item in input_context.dispositions}
    if CORRECTED_ID not in by_id:
        raise H125ParentEquityRevenueContractError(f"{report_id}: corrected H125 statements lack a current RELATED disposition")
    item = by_id[CORRECTED_ID]
    if item.relevance != "RELATED" or report_id not in item.related_report_announcement_ids:
        raise H125ParentEquityRevenueContractError(f"{report_id}: corrected source relation is not bound to this H125 chain")


def _metric_scope(metric: str) -> ProfitScope | None:
    if metric not in METRICS:
        raise H125ParentEquityRevenueContractError("unsupported target metric scope")
    return None


def _value_for(report: Mapping, metric: str) -> Decimal:
    return Decimal(str(report["candidate"].by_field[metric].value_yuan))


def _existing_cohort(repository: FinancialFactRepository, accepted: Mapping[str, tuple[str, str]]) -> tuple[dict, str | None]:
    store = repository.load()
    rows = [fact for fact in store.facts if fact.instrument_id in (INSTRUMENT_ID, "SH." + INSTRUMENT_ID)
            and fact.period_end == H125 and fact.metric in METRICS]
    aliases = {fact.instrument_id for fact in rows}
    if len(aliases) > 1:
        raise H125ParentEquityRevenueContractError("H125 logical keys exist under both 601069 aliases")
    alias = next(iter(aliases), INSTRUMENT_ID)
    by_metric: dict[str, list[FinancialFact]] = {}
    for metric in METRICS:
        members = [fact for fact in rows if fact.metric == metric]
        for fact in members:
            if (fact.instrument_id != alias or fact.source_id != "cninfo" or
                    fact.statement_scope is not StatementScope.CONSOLIDATED or
                    fact.profit_scope is not _metric_scope(metric) or fact.currency != "CNY" or
                    fact.raw_unit != "yuan"):
                raise H125ParentEquityRevenueContractError(f"existing {metric} cohort has an incompatible logical key")
            if fact.version_id not in accepted:
                raise H125ParentEquityRevenueContractError(f"existing {metric} version has no normal accepted reader link")
        by_metric[metric] = members
    return by_metric, alias


def _chain_prefix(existing: Sequence[FinancialFact],
                  expected: Sequence[tuple[str, Decimal, str, str, date, datetime]],
                  *, metric: str) -> list[FinancialFact]:
    """Validate a stored chain as an accepted prefix of observed source events."""
    if not existing:
        return []
    by_id = {item.version_id: item for item in existing}
    children: dict[str | None, list[FinancialFact]] = {}
    for item in existing:
        children.setdefault(item.supersedes_id, []).append(item)
    if any(len(values) != 1 for values in children.values()):
        raise H125ParentEquityRevenueContractError(f"existing {metric} lineage branches")
    roots = children.get(None, [])
    if len(roots) != 1:
        raise H125ParentEquityRevenueContractError(f"existing {metric} lineage has no unique root")
    ordered, current = [], roots[0]
    while True:
        ordered.append(current)
        nxt = children.get(current.version_id, [])
        if not nxt:
            break
        current = nxt[0]
    if len(ordered) != len(existing) or len(ordered) > len(expected):
        raise H125ParentEquityRevenueContractError(f"existing {metric} lineage is disconnected or too long")
    previous_id = None
    for fact, (source_id, amount, version_id, content_hash, published, first_seen) in zip(ordered, expected):
        if (fact.source_document_id != source_id or Decimal(str(fact.value)) != amount or
                fact.version_id != version_id or fact.content_hash != content_hash or
                fact.source_published_date != published or fact.first_seen_at != first_seen or
                fact.supersedes_id != previous_id):
            raise H125ParentEquityRevenueContractError(f"existing {metric} lineage conflicts with re-read source values")
        previous_id = version_id
    return ordered


def promote_601069_h125_parent_equity_revenue(
    *, original_h125: ReportReviewContext, presenting_h126: ReportReviewContext,
    current_company: Mapping[str, object], archive: ForwardArchive,
    repository: FinancialFactRepository, calendar_evidence: Mapping[str, object],
) -> tuple[FinancialFact, ...]:
    """Re-read the finite H125 lineage, then append missing versions atomically.

    Existing roots and accepted predecessors are reused. Any unaccepted, aliased,
    branched, or unexpected source lineage closes the write; missing stage40
    demands are never interpreted as proof that the warehouse has no history.
    """
    started = _utc_now()
    rights = default_rights().get("cninfo")
    if not (rights.open_for("research_use") and rights.open_for("local_storage")):
        raise H125ParentEquityRevenueContractError("CNINFO research/storage rights are closed")
    dependency = _accepted_annual_dependency(repository)
    impact = dependency["evidence"]["revision_impact_proof"]
    if dependency["fact"].instrument_id not in (INSTRUMENT_ID, "SH." + INSTRUMENT_ID):
        raise H125ParentEquityRevenueContractError("accepted annual dependency alias is invalid")
    if dependency["fact"].available_at > started or dependency["fact"].ingested_at > started:
        raise H125ParentEquityRevenueContractError("accepted FY25 annual dependency is not available at the current writer time")
    scope = _check_current_relationship_scope(archive, current_company, impact)
    current_scope_refresh = _replay_current_annual_scope(archive, current_company, impact)
    original = _read_full_report(archive, original_h125, expected_period=H125,
                                 expected_id=ORIGINAL_ID, now=started)
    presenting = _read_full_report(archive, presenting_h126, expected_period=H126,
                                   expected_id=PRESENTING_ID, now=started)
    _ensure_correction_related(original, ORIGINAL_ID)
    _ensure_correction_related(presenting, PRESENTING_ID)
    for report in (original, presenting):
        cross = report["context"].cross_category_index
        if (scope["indexReceiptIds"] != [cross.org_lookup_receipt_id, *(page.receipt_id for page in cross.pages)] or
                scope["requestBodyHashes"] != [page.request_body_hash for page in cross.pages] or
                list(current_company.get("publicationWindow", ())) != [day.isoformat() for day in cross.publication_window] or
                report["context"].version_review.org_lookup_receipt_id != cross.org_lookup_receipt_id):
            raise H125ParentEquityRevenueContractError("complete current relationship scope differs from a normal report index")
    source_bindings = _scope_by_id(scope)
    corrected_binding = source_bindings.get(CORRECTED_ID)
    if corrected_binding is None:
        raise H125ParentEquityRevenueContractError("complete current index lacks corrected H125 source")
    corrected = _read_corrected_h125(archive, corrected_binding, source_section_period_end=H125)
    prior_observations, excluded_equity = _read_h126_h125_prior(presenting)
    corrected_values = {metric: Decimal(str(corrected["proof"]["fields"][metric]["current"]["amountYuan"]))
                        for metric in METRICS}
    prior_values = {metric: Decimal(prior_observations[metric]["amountYuan"])
                    for metric in H126_PRIOR_METRICS}
    annual_bridges = _annual_h1_bridges(impact, corrected_values, prior_values)
    for metric in METRICS:
        old_value = _value_for(original, metric)
        corrected_value = Decimal(str(corrected["proof"]["fields"][metric]["current"]["amountYuan"]))
        if corrected_value != corrected["candidate"].by_field[metric].value_yuan:
            raise H125ParentEquityRevenueContractError(f"{metric}: corrected native and ordinary strict value disagree")
        if metric in H126_PRIOR_METRICS:
            if Decimal(prior_observations[metric]["amountYuan"]) == corrected_value:
                # Keep the later PRIOR disclosure in the audit proof; do not make
                # a duplicate fact version when the value did not change.
                pass
    annual_published = dependency["fact"].source_published_date
    if (not original["published"] < corrected["published"] < presenting["published"] or
            not original["published"] < annual_published < presenting["published"]):
        raise H125ParentEquityRevenueContractError("original, accepted annual bridge, and H126 publication order is inconsistent")
    from .s2_calendar_evidence import verify_calendar_evidence
    calendar_checked_at = _utc_now()
    trading_calendar, verified_calendar = verify_calendar_evidence(
        dict(calendar_evidence), archive, now=calendar_checked_at,
        published_on=min(original["published"], presenting["published"], corrected["published"]),
    )
    completed = _utc_now()
    if completed < started:
        raise H125ParentEquityRevenueContractError("normal writer clock moved backwards")
    annual_proof_completed = datetime.fromisoformat(str(impact["completedAt"]))
    scope_times = [datetime.fromisoformat(value) for value in scope["indexReceiptFirstSeenAt"]]
    scope_times.extend(datetime.fromisoformat(item["firstSeenAt"])
                       for item in scope["originalSources"])
    scope_times.extend(datetime.fromisoformat(item["pdfFirstSeenAt"])
                       for item in scope["currentSourceSupplements"])
    if (any(value.tzinfo is None or value > completed for value in scope_times) or
            annual_proof_completed.tzinfo is None or annual_proof_completed > completed):
        raise H125ParentEquityRevenueContractError("current index or accepted annual proof time is after validation")
    available = _next_available_snapshot(max(
        completed, dependency["fact"].available_at,
        dependency["fact"].ingested_at, annual_proof_completed,
        date_only_available_at(original["published"], trading_calendar)[0],
        date_only_available_at(corrected["published"], trading_calendar)[0],
        date_only_available_at(presenting["published"], trading_calendar)[0],
        *original["times"], *presenting["times"], corrected["firstSeen"],
        *scope_times, calendar_checked_at,
    ), trading_calendar)
    existing_by_metric, alias = _existing_cohort(repository, dependency["acceptedLinks"])
    if any(fact.ingested_at > completed or fact.available_at > completed
           for rows in existing_by_metric.values() for fact in rows):
        raise H125ParentEquityRevenueContractError("an existing H125 cohort version is future-dated")
    facts_to_submit: dict[str, FinancialFact] = {}
    ids_by_metric: dict[str, list[str]] = {}
    expected_by_metric: dict[str, list[tuple[str, Decimal, str, str, date, datetime]]] = {}
    sources_by_metric: dict[str, list[dict]] = {}
    for metric in METRICS:
        events = [
            (ORIGINAL_ID, _value_for(original, metric), original["candidate"].pdf_sha256,
             "ORIGINAL_CURRENT", original["published"], original["firstSeen"]),
            (CORRECTED_ID, Decimal(str(corrected["proof"]["fields"][metric]["current"]["amountYuan"])),
             corrected["candidate"].pdf_sha256, "CORRECTED_CURRENT", corrected["published"], corrected["firstSeen"]),
        ]
        if metric in H126_PRIOR_METRICS:
            events.append((PRESENTING_ID, Decimal(prior_observations[metric]["amountYuan"]),
                           presenting["candidate"].pdf_sha256, "PRESENTED_PRIOR",
                           presenting["published"], presenting["firstSeen"]))
        expected: list[tuple[str, Decimal, str, str, date, datetime]] = []
        previous_id = None
        previous_value = None
        version_sources = []
        for source_id, value, digest, role, published, first_seen in events:
            if previous_value is not None and value == previous_value:
                version_sources.append({
                    "announcementId": source_id, "role": role, "valueYuan": str(value),
                    "contentHash": digest, "sourcePublishedDate": published.isoformat(),
                    "firstSeenAt": first_seen.isoformat(), "columnRole":
                    "PRIOR" if role == "PRESENTED_PRIOR" else "CURRENT",
                    "sourceReportPeriodEnd": H126.isoformat() if role == "PRESENTED_PRIOR" else H125.isoformat(),
                    "factPeriodEnd": H125.isoformat(), "duplicateValueNoVersion": True,
                })
                continue
            version_id = _version_id(metric, source_id, digest, role, previous_id)
            expected.append((source_id, value, version_id, digest, published, first_seen))
            fact = FinancialFact(
                instrument_id=alias, metric=metric, period_end=H125,
                statement_scope=StatementScope.CONSOLIDATED, profit_scope=_metric_scope(metric),
                value=value, currency="CNY", raw_unit="yuan", source_id="cninfo",
                source_document_id=source_id, source_published_date=published, source_published_at=None,
                timestamp_precision=TimestampPrecision.DATE, first_seen_at=first_seen,
                ingested_at=completed, available_at=available,
                availability_basis=AvailabilityBasis.OBSERVED, pit_mode=PitMode.LIVE_OBSERVED,
                content_hash=digest, version_id=version_id, supersedes_id=previous_id,
            )
            facts_to_submit[version_id] = fact
            previous_id, previous_value = version_id, value
            version_sources.append({"announcementId": source_id, "role": role,
                                    "valueYuan": str(value), "versionId": version_id,
                                    "contentHash": digest, "sourcePublishedDate": published.isoformat(),
                                    "firstSeenAt": first_seen.isoformat(), "columnRole":
                                    "PRIOR" if role == "PRESENTED_PRIOR" else "CURRENT",
                                    "sourceReportPeriodEnd": H126.isoformat() if role == "PRESENTED_PRIOR" else H125.isoformat(),
                                    "factPeriodEnd": H125.isoformat()})
        expected_by_metric[metric] = expected
        sources_by_metric[metric] = version_sources
        prior_chain = _chain_prefix(existing_by_metric[metric], expected, metric=metric)
        prior_ids = {item.version_id for item in prior_chain}
        ids_by_metric[metric] = [item[2] for item in expected]
        for item in prior_chain:
            facts_to_submit[item.version_id] = item
        # Only submit the suffix after the physical accepted head.
        if prior_chain:
            head = prior_chain[-1]
            suffix = [facts_to_submit[version_id] for _, _, version_id, *_ in expected
                      if version_id not in prior_ids]
            expected_predecessor = head.version_id
            for item in suffix:
                if item.supersedes_id != expected_predecessor:
                    raise H125ParentEquityRevenueContractError(f"{metric}: new event does not extend the accepted physical head")
                expected_predecessor = item.version_id

    version_ids = sorted(facts_to_submit)
    if not version_ids:
        return ()
    evidence = {
        "schema": SCHEMA, "verifierId": VERIFIER, "verifierVersion": "1",
        "humanReviewClaimed": None, "rightsRegisterVersion": REGISTER_VERSION,
        "sourceId": "cninfo", "instrumentId": INSTRUMENT_ID,
        "factPeriodEnd": H125.isoformat(), "metricSet": list(METRICS),
        "versionIdsByMetric": ids_by_metric, "factVersionIds": version_ids,
        "versionSourcesByMetric": sources_by_metric,
        "originalReport": _report_context_evidence(archive, original),
        "correctedH125": {
            "announcementId": CORRECTED_ID, "documentRole": "CORRECTED_FINANCIAL_STATEMENTS",
            "sourceSectionPeriodEnd": H125.isoformat(), "sourceReportPeriodEndMeaning": "selected-section-only; source contains multiple corrected periods",
            "receiptId": corrected["receipt"]["receipt_id"], "contentHash": corrected["receipt"]["content_hash"],
            "documentUrl": corrected["receipt"]["url"], "sourcePublishedDate": corrected["published"].isoformat(),
            "firstSeenAt": corrected["firstSeen"].isoformat(), "identityBasis": "INDEX_BOUND_ONLY",
            "nativeSourceProof": corrected["proof"],
            "normalCandidate": _candidate_fields(corrected["candidate"]),
            "normalVerification": corrected["verification"].as_evidence(),
        },
        "presentingH126": _report_context_evidence(archive, presenting),
        "h126H125Prior": prior_observations,
        "excludedParentEquityPrior": excluded_equity,
        "annualDependency": {"factVersionId": dependency["fact"].version_id,
            "bundleId": dependency["bundleId"], "evidenceHash": dependency["evidenceHash"],
            "acceptedFactAvailableAt": dependency["fact"].available_at.isoformat(),
            "acceptedFactIngestedAt": dependency["fact"].ingested_at.isoformat(),
            "acceptedFactPublishedDate": dependency["fact"].source_published_date.isoformat()},
        "annualRevisionImpactProof": impact,
        "currentScopeImpactRefresh": current_scope_refresh,
        "annualH1Bridges": annual_bridges,
        "currentRelationshipCompany": dict(current_company), "currentRelationshipScope": scope,
        "calendarEvidence": verified_calendar,
        "calendarCheckedAt": calendar_checked_at.isoformat(),
        "tradingCalendar": [day.isoformat() for day in trading_calendar],
        "completedAt": completed.isoformat(), "availableAt": available.isoformat(),
        "formalAdmission": "NORMAL_READER_REPLAY_REQUIRED",
    }
    for metric, source_rows in sources_by_metric.items():
        for source in source_rows:
            if source.get("versionId"):
                fact = facts_to_submit[source["versionId"]]
                source["ingestedAt"] = fact.ingested_at.isoformat()
                source["availableAt"] = fact.available_at.isoformat()
                source["currency"] = fact.currency
                source["rawUnit"] = fact.raw_unit
    if all(len(existing_by_metric[metric]) == len(expected_by_metric[metric])
           for metric in METRICS):
        # The current source rows were freshly replayed above, but every
        # applicable value is already represented by an accepted chain head.
        # Keep the PRIOR/source observation in this return path without adding
        # a duplicate financial version or review transaction.
        return tuple(item for metric in METRICS for item in existing_by_metric[metric])
    bundle_id = "cninfo_machine_h125_parent_equity_revenue_" + hashlib.sha256(
        f"{INSTRUMENT_ID}|{H125}|{'|'.join(version_ids)}".encode()).hexdigest()[:32]
    repository.append_many(tuple(facts_to_submit.values()),
                            review_bundle=FactReviewBundle(bundle_id, _canonical(evidence)))
    return tuple(facts_to_submit[item] for item in version_ids)


def accepts_601069_h125_parent_equity_revenue(
    row: Mapping, member_ids: set[str], facts: Mapping[str, FinancialFact], *,
    accepted_links: Mapping[str, tuple[str, str]], review_rows: Mapping[str, Mapping],
) -> bool:
    """Independent public-reader replay hook for this exact two-field bundle.

    The host reader must call this alongside its ordinary v1/v2 machine
    validators. The function accepts no serialized PASS flag as source truth:
    it reopens receipt-bound bytes and re-runs the report, correction, PRIOR,
    relationship, and calendar checks.
    """
    try:
        raw = row["evidence_json"]
        evidence = json.loads(raw)
        if (_canonical(evidence) != raw or row["evidence_hash"] != _digest_bytes(raw.encode()) or
                evidence.get("schema") != SCHEMA or evidence.get("verifierId") != VERIFIER or
                evidence.get("humanReviewClaimed") is not None or
                evidence.get("rightsRegisterVersion") != REGISTER_VERSION or
                evidence.get("instrumentId") != INSTRUMENT_ID or
                evidence.get("factPeriodEnd") != H125.isoformat() or
                evidence.get("metricSet") != list(METRICS) or
                evidence.get("formalAdmission") != "NORMAL_READER_REPLAY_REQUIRED"):
            return False
        version_sources = evidence.get("versionSourcesByMetric")
        ids_by_metric = evidence.get("versionIdsByMetric")
        if not isinstance(version_sources, Mapping) or not isinstance(ids_by_metric, Mapping):
            return False
        all_ids = {version for values in ids_by_metric.values() for version in values}
        if (set(version_sources) != set(METRICS) or set(ids_by_metric) != set(METRICS) or
                all_ids != member_ids or member_ids != set(evidence.get("factVersionIds", ())) or
                not member_ids <= facts.keys()):
            return False
        aliases = {facts[version].instrument_id for version in member_ids}
        if len(aliases) != 1 or not aliases <= {INSTRUMENT_ID, "SH." + INSTRUMENT_ID}:
            return False
        expected_bundle = "cninfo_machine_h125_parent_equity_revenue_" + hashlib.sha256(
            f"{INSTRUMENT_ID}|{H125}|{'|'.join(sorted(member_ids))}".encode()).hexdigest()[:32]
        if row["bundle_id"] != expected_bundle:
            return False
        for metric in METRICS:
            if not isinstance(version_sources[metric], list) or not version_sources[metric]:
                return False
            prior_id = None
            seen_ids = []
            last_value = None
            for source in version_sources[metric]:
                role = source.get("role")
                if role not in ("ORIGINAL_CURRENT", "CORRECTED_CURRENT", "PRESENTED_PRIOR"):
                    return False
                expected_column = "PRIOR" if role == "PRESENTED_PRIOR" else "CURRENT"
                expected_report_period = H126.isoformat() if role == "PRESENTED_PRIOR" else H125.isoformat()
                if (source.get("columnRole") != expected_column or
                        source.get("sourceReportPeriodEnd") != expected_report_period or
                        source.get("factPeriodEnd") != H125.isoformat()):
                    return False
                value = Decimal(str(source["valueYuan"]))
                if source.get("duplicateValueNoVersion"):
                    if last_value is None or value != last_value or source.get("versionId") is not None:
                        return False
                    continue
                version_id = source.get("versionId")
                fact = facts.get(version_id)
                expected_id = _version_id(metric, str(source.get("announcementId")),
                                          str(source.get("contentHash")), str(role), prior_id)
                if (fact is None or fact.metric != metric or fact.period_end != H125 or
                        fact.instrument_id not in (INSTRUMENT_ID, "SH." + INSTRUMENT_ID) or
                        fact.source_id != "cninfo" or fact.source_document_id != source.get("announcementId") or
                        fact.statement_scope is not StatementScope.CONSOLIDATED or
                        fact.profit_scope is not _metric_scope(metric) or fact.currency != "CNY" or
                        fact.raw_unit != "yuan" or fact.content_hash != source.get("contentHash") or
                        fact.version_id != expected_id or
                        fact.supersedes_id != prior_id or Decimal(str(fact.value)) != value or
                        source.get("columnRole") != expected_column or
                        source.get("sourceReportPeriodEnd") != expected_report_period or
                        source.get("factPeriodEnd") != H125.isoformat() or
                        fact.availability_basis is not AvailabilityBasis.OBSERVED or
                        fact.pit_mode is not PitMode.LIVE_OBSERVED or
                        fact.timestamp_precision is not TimestampPrecision.DATE or
                        fact.ingested_at.isoformat() != source.get("ingestedAt") or
                        fact.available_at.isoformat() != source.get("availableAt") or
                        fact.source_published_date.isoformat() != source.get("sourcePublishedDate") or
                        fact.first_seen_at.isoformat() != source.get("firstSeenAt")):
                    return False
                seen_ids.append(version_id)
                prior_id = version_id
                last_value = value
            if seen_ids != ids_by_metric[metric]:
                return False
        archive_data = evidence["originalReport"]["archive"]
        from ...artifact_paths import resolve_archive_member
        from ...domain.data.db import connect
        root = Path(archive_data["root"]).resolve()
        database = Path(archive_data["databasePath"]).resolve()
        if database != root / "meta.sqlite":
            return False
        archive = _ReadOnlyArchive(database, root, connect, resolve_archive_member)
        try:
            replay = _replay_evidence(archive, evidence)
            if archive.con.total_changes:
                return False
        finally:
            archive.close()
        if not replay:
            return False
        dependency = evidence["annualDependency"]
        annual_fact = facts.get(dependency["factVersionId"])
        annual_link = accepted_links.get(dependency["factVersionId"])
        annual_row = review_rows.get(dependency["bundleId"])
        if (annual_fact is None or annual_link != (dependency["bundleId"], dependency["evidenceHash"]) or
                annual_row is None or annual_row["evidence_hash"] != dependency["evidenceHash"] or
                annual_fact.metric != "revenue" or annual_fact.source_document_id != ANNUAL_ID or
                annual_fact.period_end != date(2025, 12, 31) or
                annual_fact.source_published_date.isoformat() != dependency["acceptedFactPublishedDate"] or
                annual_fact.available_at.isoformat() != dependency["acceptedFactAvailableAt"] or
                annual_fact.ingested_at.isoformat() != dependency["acceptedFactIngestedAt"]):
            return False
        annual_evidence = json.loads(annual_row["evidence_json"])
        if annual_evidence.get("revision_impact_proof") != evidence.get("annualRevisionImpactProof"):
            return False
        annual_bridges = _annual_h1_bridges(
            evidence["annualRevisionImpactProof"],
            {metric: Decimal(evidence["correctedH125"]["nativeSourceProof"]["fields"][metric]["current"]["amountYuan"])
             for metric in METRICS},
            {metric: Decimal(evidence["h126H125Prior"][metric]["amountYuan"])
             for metric in H126_PRIOR_METRICS},
        )
        if annual_bridges != evidence.get("annualH1Bridges"):
            return False
        # The replayed bytes, not the serialized review rows, control the four
        # source values and their role-specific version order.
        original, corrected, presenting = replay["original"], replay["corrected"], replay["presenting"]
        annual_published = date.fromisoformat(evidence["annualDependency"]["acceptedFactPublishedDate"])
        if (not original["published"] < corrected["published"] < presenting["published"] or
                not original["published"] < annual_published < presenting["published"]):
            return False
        completed = datetime.fromisoformat(evidence["completedAt"])
        available = datetime.fromisoformat(evidence["availableAt"])
        calendar_checked_at = datetime.fromisoformat(evidence["calendarCheckedAt"])
        calendar = tuple(replay["tradingCalendar"])
        annual_proof_completed = datetime.fromisoformat(
            evidence["annualRevisionImpactProof"]["completedAt"])
        scope_times = [datetime.fromisoformat(value)
                       for value in replay["scope"]["indexReceiptFirstSeenAt"]]
        scope_times.extend(datetime.fromisoformat(item["firstSeenAt"])
                           for item in replay["scope"]["originalSources"])
        scope_times.extend(datetime.fromisoformat(item["pdfFirstSeenAt"])
                           for item in replay["scope"]["currentSourceSupplements"])
        clock_values = (completed, available, calendar_checked_at, annual_proof_completed,
                        annual_fact.available_at, annual_fact.ingested_at,
                        original["firstSeen"], presenting["firstSeen"], corrected["firstSeen"],
                        *original["times"], *presenting["times"], *scope_times,
                        *replay["calendarFirstSeenTimes"])
        if (any(value.tzinfo is None or value.utcoffset() is None for value in clock_values) or
                completed > datetime.now(timezone.utc) or calendar_checked_at > completed or
                annual_proof_completed > completed or annual_fact.ingested_at > completed or
                annual_fact.available_at > completed or
                any(value > completed for value in (
                    *original["times"], *presenting["times"], corrected["firstSeen"],
                    *scope_times, *replay["calendarFirstSeenTimes"])) or
                calendar != tuple(sorted(set(calendar))) or not calendar):
            return False
        floors = [
            completed, annual_fact.available_at, annual_fact.ingested_at, annual_proof_completed,
            date_only_available_at(original["published"], calendar)[0],
            date_only_available_at(corrected["published"], calendar)[0],
            date_only_available_at(presenting["published"], calendar)[0],
            *original["times"], *presenting["times"], corrected["firstSeen"],
            *scope_times, calendar_checked_at, *replay["calendarFirstSeenTimes"],
        ]
        if _next_available_snapshot(max(floors), calendar) != available:
            return False
        actual_events = {}
        for metric in METRICS:
            events = [
                {"announcementId": ORIGINAL_ID, "role": "ORIGINAL_CURRENT",
                 "valueYuan": str(_value_for(original, metric)),
                 "contentHash": original["candidate"].pdf_sha256,
                 "sourcePublishedDate": original["published"].isoformat(),
                 "firstSeenAt": original["firstSeen"].isoformat()},
                {"announcementId": CORRECTED_ID, "role": "CORRECTED_CURRENT",
                 "valueYuan": str(Decimal(corrected["proof"]["fields"][metric]["current"]["amountYuan"])),
                 "contentHash": corrected["receipt"]["content_hash"],
                 "sourcePublishedDate": corrected["published"].isoformat(),
                 "firstSeenAt": corrected["firstSeen"].isoformat()},
            ]
            if metric in H126_PRIOR_METRICS:
                events.append({"announcementId": PRESENTING_ID, "role": "PRESENTED_PRIOR",
                               "valueYuan": replay["prior"][metric]["amountYuan"],
                               "contentHash": presenting["candidate"].pdf_sha256,
                               "sourcePublishedDate": presenting["published"].isoformat(),
                               "firstSeenAt": presenting["firstSeen"].isoformat()})
            collapsed = []
            last_value = None
            previous = None
            for event in events:
                value = Decimal(str(event["valueYuan"]))
                if last_value is not None and value == last_value:
                    event.update({"columnRole": "PRIOR" if event["role"] == "PRESENTED_PRIOR" else "CURRENT",
                                  "sourceReportPeriodEnd": H126.isoformat() if event["role"] == "PRESENTED_PRIOR" else H125.isoformat(),
                                  "factPeriodEnd": H125.isoformat(), "duplicateValueNoVersion": True})
                    collapsed.append(event)
                    continue
                version_id = _version_id(metric, event["announcementId"], event["contentHash"],
                                         event["role"], previous)
                event.update({"versionId": version_id,
                              "columnRole": "PRIOR" if event["role"] == "PRESENTED_PRIOR" else "CURRENT",
                              "sourceReportPeriodEnd": H126.isoformat() if event["role"] == "PRESENTED_PRIOR" else H125.isoformat(),
                              "factPeriodEnd": H125.isoformat()})
                fact = facts[version_id]
                if (Decimal(str(fact.value)) != value or fact.content_hash != event["contentHash"] or
                        fact.source_document_id != event["announcementId"] or
                        fact.first_seen_at.isoformat() != event["firstSeenAt"] or
                        fact.source_published_date.isoformat() != event["sourcePublishedDate"] or
                        fact.ingested_at > completed or fact.available_at > available or
                        (fact.ingested_at == completed and fact.available_at != available) or
                        fact.available_at < date_only_available_at(fact.source_published_date, calendar)[0]):
                    return False
                event.update({"ingestedAt": fact.ingested_at.isoformat(),
                              "availableAt": fact.available_at.isoformat(),
                              "currency": fact.currency, "rawUnit": fact.raw_unit})
                collapsed.append(event)
                previous, last_value = version_id, value
            if collapsed != version_sources[metric]:
                return False
            expected_ids = [item["versionId"] for item in collapsed if item.get("versionId")]
            if expected_ids != ids_by_metric[metric]:
                return False
        return True
    except (OSError, sqlite3.Error, KeyError, AttributeError, TypeError, ValueError, ArithmeticError):
        return False


class _ReadOnlyArchive:
    def __init__(self, database: Path, root: Path, connect_fn, resolve_member):
        self.root = root
        self.con = connect_fn(database, read_only=True)
        self.con.execute("PRAGMA query_only=ON")
        self.con.execute("BEGIN")
        self._resolve_member = resolve_member

    def load_bytes(self, digest: str) -> bytes:
        row = self.con.execute("SELECT stored_path,byte_size FROM raw_artifact WHERE content_hash=?", (digest,)).fetchone()
        if row is None:
            raise KeyError(digest)
        path = self._resolve_member(self.root, row["stored_path"])
        body = path.read_bytes()
        if len(body) != row["byte_size"] or _digest_bytes(body) != digest:
            raise ValueError("raw artifact is inconsistent")
        return body

    def verify(self, digest: str) -> bool:
        try:
            self.load_bytes(digest)
        except (OSError, KeyError, ValueError):
            return False
        return True

    def close(self) -> None:
        try:
            if self.con.in_transaction:
                self.con.execute("ROLLBACK")
        finally:
            self.con.close()


def _replay_evidence(archive: ForwardArchive, evidence: Mapping) -> dict | None:
    """Re-extract source roles and proof bytes from the immutable archive."""
    try:
        original_context = _context_from_evidence(evidence["originalReport"])
        presenting_context = _context_from_evidence(evidence["presentingH126"])
        now = datetime.fromisoformat(evidence["completedAt"])
        original = _read_full_report(archive, original_context, expected_period=H125,
                                     expected_id=ORIGINAL_ID, now=now)
        presenting = _read_full_report(archive, presenting_context, expected_period=H126,
                                       expected_id=PRESENTING_ID, now=now)
        scope = _check_current_relationship_scope(archive, evidence["currentRelationshipCompany"],
                                                  evidence["annualRevisionImpactProof"])
        if scope != evidence["currentRelationshipScope"]:
            return False
        scope_refresh = _replay_current_annual_scope(
            archive, evidence["currentRelationshipCompany"],
            evidence["annualRevisionImpactProof"], evidence.get("currentScopeImpactRefresh"))
        if scope_refresh != evidence.get("currentScopeImpactRefresh"):
            return False
        corrected_binding = _scope_by_id(scope).get(CORRECTED_ID)
        corrected = _read_corrected_h125(archive, corrected_binding,
                                          source_section_period_end=H125)
        prior, excluded = _read_h126_h125_prior(presenting)
        if (prior != evidence["h126H125Prior"] or excluded != evidence["excludedParentEquityPrior"] or
                corrected["proof"] != evidence["correctedH125"]["nativeSourceProof"] or
                _candidate_fields(corrected["candidate"]) != evidence["correctedH125"]["normalCandidate"] or
                corrected["receipt"]["receipt_id"] != evidence["correctedH125"]["receiptId"] or
                corrected["receipt"]["content_hash"] != evidence["correctedH125"]["contentHash"] or
                corrected["receipt"]["url"] != evidence["correctedH125"]["documentUrl"] or
                corrected["published"].isoformat() != evidence["correctedH125"]["sourcePublishedDate"] or
                corrected["firstSeen"].isoformat() != evidence["correctedH125"]["firstSeenAt"] or
                corrected["verification"].as_evidence() != evidence["correctedH125"]["normalVerification"] or
                original["verification"].as_evidence() != evidence["originalReport"]["normalVerification"] or
                presenting["verification"].as_evidence() != evidence["presentingH126"]["normalVerification"]):
            return False
        for report, saved in ((original, evidence["originalReport"]), (presenting, evidence["presentingH126"])):
            if _report_context_evidence(archive, report) != saved:
                return False
        for context in (original, presenting):
            _ensure_correction_related(context, context["context"].version_review.announcement_id)
            cross = context["context"].cross_category_index
            if (scope["indexReceiptIds"] !=
                    [cross.org_lookup_receipt_id, *(page.receipt_id for page in cross.pages)] or
                    scope["requestBodyHashes"] != [page.request_body_hash for page in cross.pages] or
                    [day.isoformat() for day in cross.publication_window] !=
                    evidence["currentRelationshipCompany"].get("publicationWindow") or
                    context["context"].version_review.org_lookup_receipt_id !=
                    cross.org_lookup_receipt_id):
                return False
        calendar_checked_at = datetime.fromisoformat(evidence["calendarCheckedAt"])
        if calendar_checked_at.tzinfo is None or calendar_checked_at.utcoffset() is None:
            return False
        from .s2_calendar_evidence import verify_calendar_evidence
        days, packet = verify_calendar_evidence(
            evidence["calendarEvidence"], archive, now=calendar_checked_at,
            published_on=min(original["published"], corrected["published"], presenting["published"]),
        )
        if (packet != evidence["calendarEvidence"] or
                [day.isoformat() for day in days] != evidence["tradingCalendar"]):
            return False
        return {"original": original, "corrected": corrected, "presenting": presenting,
                "scope": scope, "currentScopeImpactRefresh": scope_refresh,
                "prior": prior, "excluded": excluded,
                "tradingCalendar": days,
                "calendarFirstSeenTimes": _nested_first_seen_times(packet)}
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        return None


def _context_from_evidence(value: Mapping) -> ReportReviewContext:
    cross = _cross_category_from_evidence(value["crossCategoryIndex"])
    return ReportReviewContext(
        period_end=date.fromisoformat(value["periodEnd"]),
        pdf_receipt_id=value["pdfReceiptId"],
        version_review=_version_review_from_evidence(value["versionReview"]),
        cross_category_index=cross,
        dispositions=_dispositions_from_evidence(value["crossCategoryDispositions"]),
        reviewed_by=value["crossCategoryReviewedBy"],
        reviewed_at=datetime.fromisoformat(value["crossCategoryReviewedAt"]),
    )


def _version_review_from_evidence(value: Mapping) -> PdfVersionReview:
    return PdfVersionReview(
        announcement_id=str(value["announcementId"]),
        org_lookup_receipt_id=str(value["orgLookupReceiptId"]),
        index_receipt_id=str(value["indexReceiptId"]),
        search_receipt_ids=tuple(value["searchReceiptIds"]),
        search_page_numbers=tuple(int(item) for item in value["searchPageNumbers"]),
        query_stock_code=str(value["queryStockCode"]),
        query_organization_id=str(value["queryOrganizationId"]),
        query_period_start=date.fromisoformat(value["queryPeriodStart"]),
        query_period_end=date.fromisoformat(value["queryPeriodEnd"]),
        query_digest=str(value["queryDigest"]), reviewer_id=str(value["reviewerId"]),
        reviewed_at=datetime.fromisoformat(value["reviewedAt"]),
        complete_search_attested=value["completeSearchAttested"],
        predecessor_announcement_id=value.get("predecessorAnnouncementId"),
        correction_receipt_id=value.get("correctionReceiptId"),
        correction_excerpt=value.get("correctionExcerpt"),
    )


def _cross_category_from_evidence(value: Mapping) -> CrossCategoryIndexResult:
    from .cninfo import CrossCategoryIndexEntry, ReportIndexPage
    pages = tuple(ReportIndexPage(
        page_num=int(item["pageNum"]), request_body_hash=str(item["requestBodyHash"]),
        receipt_id=str(item["receiptId"]), content_hash=item.get("contentHash"),
        http_status=item.get("httpStatus"), raw_announcement_count=item.get("rawAnnouncementCount"),
        total_announcement=item.get("totalAnnouncement"), has_more=item.get("hasMore"),
    ) for item in value["pages"])
    entries = tuple(CrossCategoryIndexEntry(
        announcement_id=str(item["announcementId"]), sec_code=str(item["secCode"]),
        title=str(item["title"]), announcement_time_ms=int(item["announcementTimeMs"]),
        document_url=str(item["documentUrl"]), candidate_reasons=tuple(item["candidateReasons"]),
        page_num=int(item["pageNum"]), page_receipt_id=str(item["pageReceiptId"]),
        page_content_hash=str(item["pageContentHash"]),
    ) for item in value["announcements"])
    return CrossCategoryIndexResult(
        stock_code=str(value["stockCode"]), organization_id=value.get("organizationId"),
        report_period=str(value["reportPeriod"]),
        publication_window=tuple(date.fromisoformat(item) for item in value["publicationWindow"]),
        org_lookup_receipt_id=value.get("orgLookupReceiptId"),
        org_lookup_content_hash=value.get("orgLookupContentHash"), pages=pages, announcements=entries,
        skipped_different_security=int(value["skippedDifferentSecurity"]),
        total_announcement=value.get("totalAnnouncement"), complete=value["complete"],
        termination=value["termination"], error=value.get("error"),
    )


def _dispositions_from_evidence(values: Sequence[Mapping]) -> tuple[CrossCategoryDisposition, ...]:
    return tuple(CrossCategoryDisposition(
        announcement_id=str(item["announcementId"]), relevance=str(item["relevance"]),
        reviewer_id=str(item["reviewerId"]), reviewed_at=datetime.fromisoformat(item["reviewedAt"]),
        rationale=str(item["rationale"]), document_receipt_id=item.get("documentReceiptId"),
        related_report_announcement_ids=tuple(item.get("relatedReportAnnouncementIds", ())),
        relation_receipt_id=item.get("relationReceiptId"), relation_excerpt=item.get("relationExcerpt"),
    ) for item in values)


__all__ = ["SCHEMA", "H125ParentEquityRevenueContractError", "ReportReviewContext",
           "promote_601069_h125_parent_equity_revenue", "accepts_601069_h125_parent_equity_revenue"]
