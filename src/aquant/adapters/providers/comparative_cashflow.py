"""Typed bindings for the 601069 H1 comparative cash-flow columns.

The source report period and the fact period are distinct for a comparative
PRIOR column.  This module binds each role to the original parser's native
cell evidence; it does not read the PDF or assign PIT timestamps itself.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Mapping


COMPARATIVE_CASHFLOW_SCHEMA = "aquant.cninfo.comparative-operating-cashflow-v1"
_UNIT_SCALE = {
    "元": Decimal(1),
    "千元": Decimal(1000),
    "万元": Decimal(10000),
    "百万元": Decimal(1000000),
}
_H1_HEADER = re.compile(r"(20\d{2})年?(?:半年度|上半年|1[-－—–]6月)")


class ComparativeCashflowContractError(ValueError):
    """A report's explicit comparative columns cannot be typed safely."""


@dataclass(frozen=True, slots=True)
class ComparativeCashflowFact:
    source_report_period_end: date
    fact_period_end: date
    column_role: str
    amount_yuan: Decimal
    inflow_subtotal_yuan: Decimal
    outflow_subtotal_yuan: Decimal
    inflow_cell: Mapping[str, object]
    outflow_cell: Mapping[str, object]
    primary_net_cell: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "sourceReportPeriodEnd": self.source_report_period_end.isoformat(),
            "factPeriodEnd": self.fact_period_end.isoformat(),
            "columnRole": self.column_role,
            "metric": "operating_cashflow",
            "statementScope": "CONSOLIDATED",
            "currency": "CNY",
            "rawUnit": "yuan",
            "amountYuan": str(self.amount_yuan),
            "inflowSubtotalYuan": str(self.inflow_subtotal_yuan),
            "outflowSubtotalYuan": str(self.outflow_subtotal_yuan),
            "derivation": "native_operating_cashflow_row_rechecked_by_inflow_less_outflow_subtotals",
            "inflowCell": copy.deepcopy(dict(self.inflow_cell)),
            "outflowCell": copy.deepcopy(dict(self.outflow_cell)),
            "primaryNetCfoCell": copy.deepcopy(dict(self.primary_net_cell)),
        }


@dataclass(frozen=True, slots=True)
class ComparativeCashflowContract:
    instrument_id: str
    source_report_period_end: date
    amount_unit: str
    facts: tuple[ComparativeCashflowFact, ...]
    source_proof: Mapping[str, object]

    def by_role(self, role: str) -> ComparativeCashflowFact:
        matches = [fact for fact in self.facts if fact.column_role == role]
        if len(matches) != 1:
            raise ComparativeCashflowContractError(
                f"expected exactly one {role} operating-cash-flow column")
        return matches[0]

    def as_evidence(self) -> dict[str, object]:
        return {
            "schema": COMPARATIVE_CASHFLOW_SCHEMA,
            "instrumentId": self.instrument_id,
            "sourceReportPeriodEnd": self.source_report_period_end.isoformat(),
            "metric": "operating_cashflow",
            "statement": "现金流量表",
            "statementScope": "CONSOLIDATED",
            "currency": "CNY",
            "rawUnit": "yuan",
            "sourceUnit": self.amount_unit,
            "sourceProof": copy.deepcopy(dict(self.source_proof)),
            "facts": [fact.as_dict() for fact in self.facts],
        }


def _compact(value: object) -> str:
    return re.sub(r"\s+", "", str(value)).replace("−", "-").replace("–", "-")


def _decimal(value: object, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ComparativeCashflowContractError(f"{label} is not a decimal amount") from exc
    if not result.is_finite():
        raise ComparativeCashflowContractError(f"{label} must be finite")
    return result


def _cell(row: object, name: str) -> Mapping[str, object]:
    if not isinstance(row, Mapping):
        raise ComparativeCashflowContractError("subtotal row evidence is missing")
    cell = row.get(name)
    if (not isinstance(cell, Mapping) or not isinstance(cell.get("amount"), str) or
            not isinstance(cell.get("text"), str) or
            not cell.get("text", "").strip() or
            not isinstance(cell.get("bbox"), list) or len(cell["bbox"]) != 4 or
            not all(isinstance(cell.get(key), int) for key in
                    ("table_index", "row_index", "cell_index"))):
        raise ComparativeCashflowContractError(f"native {name} subtotal cell is incomplete")
    return cell


def _header_year(value: object, expected_year: int, role: str) -> None:
    text = _compact(value)
    match = _H1_HEADER.search(text)
    if match is None or int(match.group(1)) != expected_year:
        raise ComparativeCashflowContractError(
            f"{role} header does not explicitly identify H1 {expected_year}")


def _source_amount(value: object, label: str) -> Decimal:
    text = _compact(value)
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = text.replace(",", "")
    amount = _decimal(text, label)
    return -amount if negative and amount > 0 else amount


def _primary_row(proof_evidence: Mapping[str, object], role: str,
                 expected_period: date) -> tuple[Mapping[str, object], Decimal]:
    row = proof_evidence.get("primary_operating_cashflow_row")
    if not isinstance(row, Mapping):
        raise ComparativeCashflowContractError(
            "native net operating cash-flow primary row is missing")
    if (row.get("role") != "合并" or row.get("statement") != "现金流量表" or
            row.get("label") != "经营活动产生的现金流量净额" or
            row.get("amount_unit") != proof_evidence.get("amount_unit") or
            row.get("current_header") != proof_evidence.get("current_header", {}).get("text") or
            row.get("prior_header") != proof_evidence.get("prior_header", {}).get("text")):
        raise ComparativeCashflowContractError(
            "native primary row identity, scope, unit, or columns differ")
    cell_key = "current_cell" if role == "CURRENT" else "prior_cell"
    amount_key = "amount" if role == "CURRENT" else "prior_amount"
    native_row = proof_evidence.get("primary_net_cfo_row")
    if (not isinstance(row.get("page"), int) or row["page"] < 1 or
            not isinstance(row.get("raw_line"), str) or
            not isinstance(row.get(cell_key), str) or not row[cell_key].strip() or
            not isinstance(native_row, Mapping) or
            native_row.get("label") != "经营活动产生的现金流量净额" or
            native_row.get("page") != row.get("page") or
            not isinstance(native_row.get("table_index"), int) or
            not isinstance(native_row.get("row_index"), int) or
            not isinstance(native_row.get("statement_page_scope"), Mapping) or
            native_row["statement_page_scope"].get("page") != row.get("page")):
        raise ComparativeCashflowContractError("native primary net-CFO cell is incomplete")
    native_cell = _cell(native_row, cell_key)
    raw_amount = _decimal(row.get(amount_key), f"{role} primary row amount")
    if (_source_amount(row[cell_key], "located primary net-CFO row cell") != raw_amount or
            _source_amount(native_cell["text"], "native primary net-CFO cell") != raw_amount or
            _decimal(native_cell["amount"], "native primary net-CFO amount") != raw_amount):
        raise ComparativeCashflowContractError(
            "native primary net-CFO cell differs from the located statement row")
    return ({
        "role": role,
        "statementScope": "CONSOLIDATED",
        "statement": "现金流量表",
        "label": row["label"],
        "page": row["page"],
        "sourceUnit": row["amount_unit"],
        "sourceReportCurrentHeader": row["current_header"],
        "sourceReportPriorHeader": row["prior_header"],
        "factPeriodEnd": expected_period.isoformat(),
        "cellText": native_cell["text"],
        "locatedRowCell": row[cell_key],
        "rawLine": row["raw_line"],
        "nativeCellEvidence": copy.deepcopy(dict(native_cell)),
        "amountYuan": str(raw_amount * _UNIT_SCALE[str(row["amount_unit"])]),
    }, raw_amount * _UNIT_SCALE[str(row["amount_unit"])])


def derive_comparative_cashflow_contract(
    proof_evidence: Mapping[str, object], *, instrument_id: str,
    source_report_period_end: date,
) -> ComparativeCashflowContract:
    """Bind native subtotal proof to CURRENT and PRIOR fact periods.

    This candidate implementation is intentionally limited to 601069's
    2026-06-30 H1 report, whose explicit PRIOR column describes 2025-06-30.
    The parser proof remains embedded verbatim for a reader-side raw replay.
    """

    if instrument_id != "601069" or source_report_period_end != date(2026, 6, 30):
        raise ComparativeCashflowContractError(
            "comparative CFO contract is scoped to 601069's 2026-06-30 H1 report")
    if (proof_evidence.get("instrument_id") != instrument_id or
            proof_evidence.get("period_end") != source_report_period_end.isoformat() or
            proof_evidence.get("role") != "合并" or
            proof_evidence.get("statement") != "现金流量表"):
        raise ComparativeCashflowContractError(
            "native subtotal proof identity, report period, or statement scope differs")
    unit = proof_evidence.get("amount_unit")
    section = proof_evidence.get("section")
    unit_line = proof_evidence.get("unit_declaration")
    heading = proof_evidence.get("statement_heading")
    report_title = _compact(proof_evidence.get("report_title", ""))
    if (unit not in _UNIT_SCALE or not isinstance(section, Mapping) or
            section.get("amount_unit") != unit or not isinstance(unit_line, Mapping) or
            "单位：" + str(unit) not in _compact(unit_line.get("text", "")) or
            "人民币" not in _compact(unit_line.get("text", "")) or
            not isinstance(heading, Mapping) or
            "合并现金流量表" not in _compact(heading.get("text", "")) or
            "西部黄金股份有限公司" not in report_title or
            "2026年半年度报告" not in report_title):
        raise ComparativeCashflowContractError(
            "native unit, RMB, consolidated heading, or source-report title is not bound")

    current_header = proof_evidence.get("current_header")
    prior_header = proof_evidence.get("prior_header")
    if not isinstance(current_header, Mapping) or not isinstance(prior_header, Mapping):
        raise ComparativeCashflowContractError("explicit CURRENT and PRIOR headers are required")
    _header_year(current_header.get("text"), 2026, "CURRENT")
    _header_year(prior_header.get("text"), 2025, "PRIOR")

    inflow_row = proof_evidence.get("operating_cash_inflow_subtotal")
    outflow_row = proof_evidence.get("operating_cash_outflow_subtotal")
    if (not isinstance(inflow_row, Mapping) or
            inflow_row.get("label") != "经营活动现金流入小计" or
            not isinstance(outflow_row, Mapping) or
            outflow_row.get("label") != "经营活动现金流出小计"):
        raise ComparativeCashflowContractError("native operating inflow/outflow subtotal rows differ")

    inflow_current = _cell(inflow_row, "current_cell")
    inflow_prior = _cell(inflow_row, "prior_cell")
    outflow_current = _cell(outflow_row, "current_cell")
    outflow_prior = _cell(outflow_row, "prior_cell")
    scale = _UNIT_SCALE[str(unit)]
    current_inflow = _decimal(inflow_current["amount"], "CURRENT inflow subtotal") * scale
    current_outflow = _decimal(outflow_current["amount"], "CURRENT outflow subtotal") * scale
    prior_inflow = _decimal(inflow_prior["amount"], "PRIOR inflow subtotal") * scale
    prior_outflow = _decimal(outflow_prior["amount"], "PRIOR outflow subtotal") * scale
    current_subtotal_amount = current_inflow - current_outflow
    prior_subtotal_amount = prior_inflow - prior_outflow
    if (current_subtotal_amount != _decimal(proof_evidence.get("current_recomputed_yuan"),
                                   "CURRENT recomputation") or
            prior_subtotal_amount != _decimal(proof_evidence.get("prior_recomputed_yuan"),
                                     "PRIOR recomputation")):
        raise ComparativeCashflowContractError(
            "subtotal cells do not reproduce both report-column amounts")

    current_primary_cell, current_amount = _primary_row(
        proof_evidence, "CURRENT", source_report_period_end)
    prior_primary_cell, prior_amount = _primary_row(
        proof_evidence, "PRIOR", date(2025, 6, 30))
    if (current_amount != current_subtotal_amount or
            prior_amount != prior_subtotal_amount):
        raise ComparativeCashflowContractError(
            "native primary CFO cells do not match the independent subtotal arithmetic")

    current_fact = ComparativeCashflowFact(
        source_report_period_end=source_report_period_end,
        fact_period_end=source_report_period_end,
        column_role="CURRENT",
        amount_yuan=current_amount,
        inflow_subtotal_yuan=current_inflow,
        outflow_subtotal_yuan=current_outflow,
        inflow_cell=inflow_current,
        outflow_cell=outflow_current,
        primary_net_cell=current_primary_cell,
    )
    prior_fact = ComparativeCashflowFact(
        source_report_period_end=source_report_period_end,
        fact_period_end=date(2025, 6, 30),
        column_role="PRIOR",
        amount_yuan=prior_amount,
        inflow_subtotal_yuan=prior_inflow,
        outflow_subtotal_yuan=prior_outflow,
        inflow_cell=inflow_prior,
        outflow_cell=outflow_prior,
        primary_net_cell=prior_primary_cell,
    )
    return ComparativeCashflowContract(
        instrument_id=instrument_id,
        source_report_period_end=source_report_period_end,
        amount_unit=str(unit),
        facts=(prior_fact, current_fact),
        source_proof=copy.deepcopy(dict(proof_evidence)),
    )


def derive_h125_cashflow_version_relation(
    annual_impact: Mapping[str, object], *, original_h125_amount: Decimal,
    h126_prior_amount: Decimal,
) -> dict[str, object]:
    """Bind the real H125 change to the annual report's native H1 bridge.

    The existing annual impact reader must first replay ``annual_impact`` from
    its receipt-bound archive. This function checks the *different* H1 version
    relationship; it does not turn the annual CURRENT NO_EFFECT result into an
    H1 NO_EFFECT verdict and does not authorize a write. The annual report's
    adjusted H1 reconciliation is a version bridge, not a replacement for the
    H126 report's literal PRIOR label or either report's primary CFO cell.
    """
    if (annual_impact.get("instrumentId") != "SH.601069" or
            annual_impact.get("issuerName") != "西部黄金股份有限公司" or
            annual_impact.get("targetPeriodEnd") != "2025-12-31"):
        raise ComparativeCashflowContractError("annual CFO bridge issuer or period differs")
    relationship = annual_impact.get("relationshipScope")
    outcomes = annual_impact.get("outcomes")
    if (not isinstance(relationship, Mapping) or
            relationship.get("completeForArchivedIndexResponse") is not True or
            relationship.get("missingReportSourceIds") or
            set(relationship.get("financialCauseIds", ())) !=
            {"1225082555", "1225082560", "1225082606"} or
            not isinstance(outcomes, list)):
        raise ComparativeCashflowContractError("annual bridge correction relationship set is incomplete")
    matching = [item for item in outcomes if isinstance(item, Mapping) and
                item.get("metric") == "operating_cashflow"]
    if len(matching) != 1:
        raise ComparativeCashflowContractError("one annual operating cashflow outcome is required")
    outcome = matching[0]
    source = outcome.get("currentSource")
    policy = outcome.get("impactProof")
    if (outcome.get("status") != "NO_EFFECT" or
            outcome.get("periodEnd") != "2025-12-31" or
            not isinstance(source, Mapping) or
            source.get("announcementId") != "1225082554" or
            source.get("sourceReportPeriodEnd") != "2025-12-31" or
            source.get("factPeriodEnd") != "2025-12-31" or
            source.get("columnRole") != "CURRENT" or
            not isinstance(policy, Mapping) or policy.get("basis") !=
            "FY25_ANNUAL_CURRENT_AND_2024_ADJUSTED_PRIOR_NOT_REPLACED_BY_INTERIM_CORRECTION_SET"):
        raise ComparativeCashflowContractError("the existing annual CURRENT proof is not the expected bridge source")
    scope = policy.get("scope")
    quotes = policy.get("sourceQuotes")
    if not isinstance(scope, Mapping) or not isinstance(quotes, list):
        raise ComparativeCashflowContractError("annual native version context is missing")
    h1_quotes = [item for item in quotes if isinstance(item, Mapping) and
                 item.get("announcementId") == "1225082554" and
                 item.get("page") == 10 and isinstance(item.get("native"), Mapping) and
                 item["native"].get("periodEnd") == "2025-06-30" and
                 item["native"].get("rowLabel") == "经营活动产生的现金流量净额"]
    if len(h1_quotes) != 1:
        raise ComparativeCashflowContractError("one literal annual H1 CFO reconciliation row is required")
    h1_native = h1_quotes[0]["native"]
    chain = scope.get("annualQuarterRoleChain")
    correction = scope.get("operatingCashflowBeforeAfter")
    if (not isinstance(chain, Mapping) or not isinstance(correction, Mapping) or
            not isinstance(chain.get("2025-06-30"), Mapping) or
            not isinstance(correction.get("2025-06-30"), Mapping)):
        raise ComparativeCashflowContractError("the annual H1 quarterly/correction link is missing")
    h1_chain, h1_correction = chain["2025-06-30"], correction["2025-06-30"]
    if (h1_native.get("unitStatus") != "BOUND_FROM_SAME_SECTION" or
            h1_chain.get("p10UnitStatus") != "BOUND_FROM_SAME_SECTION"):
        raise ComparativeCashflowContractError("annual H1 unit is not bound by its native section")
    before = _decimal(h1_native.get("beforeYuan"), "annual H1 disclosed CFO")
    after = _decimal(h1_native.get("annualAdjustedYuan"), "annual H1 adjusted CFO")
    delta = _decimal(h1_native.get("changeYuan"), "annual H1 CFO adjustment")
    original = _decimal(original_h125_amount, "original H125 primary CFO")
    presented_prior = _decimal(h126_prior_amount, "H126 native PRIOR CFO")
    if before == after or after - before != delta:
        raise ComparativeCashflowContractError("annual H1 before/change/after does not prove a real version change")
    if original != before or presented_prior != after:
        raise ComparativeCashflowContractError("report primary columns do not match the explicit annual H1 version bridge")
    if (any(_decimal(h1_chain.get(key), key) != expected for key, expected in (
            ("disclosedPeriodicRaw", before), ("annualReportAdjustedRaw", after),
            ("annualAdjustmentRaw", delta), ("annualQuarterCumulativeYuan", after))) or
            any(_decimal(h1_correction.get(key), key) != before for key in (
                "correctionNetBeforeYuan", "correctionNetAfterYuan"))):
        raise ComparativeCashflowContractError("annual H1 bridge conflicts with the separately bound correction/quarter amounts")
    inflow_before = _decimal(h1_correction.get("correctionInflowBeforeYuan"), "H1 correction inflow before")
    inflow_after = _decimal(h1_correction.get("correctionInflowAfterYuan"), "H1 correction inflow after")
    outflow_before = _decimal(h1_correction.get("correctionOutflowBeforeYuan"), "H1 correction outflow before")
    outflow_after = _decimal(h1_correction.get("correctionOutflowAfterYuan"), "H1 correction outflow after")
    if inflow_before - outflow_before != before or inflow_after - outflow_after != before:
        raise ComparativeCashflowContractError("the original correction subtotals do not preserve the disclosed H1 net")
    return {
        "instrumentId": "SH.601069", "metric": "operating_cashflow",
        "factPeriodEnd": "2025-06-30", "status": "AFFECTS_VERSION_CHAIN",
        "original": {"announcementId": "1224590046", "sourceReportPeriodEnd": "2025-06-30",
                     "columnRole": "CURRENT", "amountYuan": str(before)},
        "annualBridge": {"announcementId": "1225082554", "sourceReportPeriodEnd": "2025-12-31",
                         "columnRole": "EXPLICIT_H1_RECONCILIATION", "amountYuan": str(after),
                         "nativeReconciliation": copy.deepcopy(dict(h1_quotes[0]))},
        "laterPresentation": {"announcementId": "1225494335", "sourceReportPeriodEnd": "2026-06-30",
                              "columnRole": "PRIOR", "amountYuan": str(after)},
        "changeYuan": str(delta),
        "correctionH1Subtotals": copy.deepcopy(dict(h1_correction)),
        "annualQuarterH1Binding": copy.deepcopy(dict(h1_chain)),
        "boundary": "A native numerical version relationship only. Raw replay, normal current/version/calendar gates, actual predecessor lookup, writer/reader and PIT acceptance are still required.",
    }


def extract_comparative_cashflow_contract(
    pdf_bytes: bytes, *, instrument_id: str, source_report_period_end: date,
) -> ComparativeCashflowContract:
    """Rebuild both primary cells and independent checks from original bytes.

    Writer and reader share this complete derivation, including the section
    parser's primary row and the independent native geometric row. Neither side
    may omit the primary row and replay only its subtotal proof.
    """
    if instrument_id != "601069" or source_report_period_end != date(2026, 6, 30):
        raise ComparativeCashflowContractError("comparative extraction is outside the bounded report")
    from .cninfo_statement_sections import ROLE_CONSOLIDATED, extract_statement_sections
    from .operating_cashflow_subtotals import extract_operating_cashflow_subtotals

    sections = extract_statement_sections(
        pdf_bytes, period_end=source_report_period_end,
        roles=(ROLE_CONSOLIDATED,), statements=("现金流量表",),
    )
    section = sections.get(ROLE_CONSOLIDATED, "现金流量表")
    if section is None:
        raise ComparativeCashflowContractError("consolidated cash-flow section is missing")
    primary_rows = [row for row in section.rows if
                    re.sub(r"\s+", "", section.normalize(row.label)) ==
                    "经营活动产生的现金流量净额"]
    if len(primary_rows) != 1:
        raise ComparativeCashflowContractError("exactly one located primary CFO row is required")
    primary = primary_rows[0]
    independent = extract_operating_cashflow_subtotals(
        pdf_bytes, instrument_id=instrument_id,
        period_end=source_report_period_end, section=section,
        include_primary_row=True,
    )
    evidence = dict(independent.evidence)
    evidence["primary_operating_cashflow_row"] = {
        "role": primary.role, "statement": primary.statement,
        "label": re.sub(r"\s+", "", section.normalize(primary.label)),
        "page": primary.page, "amount_unit": primary.unit,
        "amount": str(primary.amount), "prior_amount": str(primary.prior_amount),
        "current_cell": primary.current_cell, "prior_cell": primary.prior_cell,
        "raw_line": primary.raw_line, "current_header": section.current_header,
        "prior_header": section.prior_header,
    }
    return derive_comparative_cashflow_contract(
        evidence, instrument_id=instrument_id,
        source_report_period_end=source_report_period_end,
    )


__all__ = [
    "COMPARATIVE_CASHFLOW_SCHEMA",
    "ComparativeCashflowContract",
    "ComparativeCashflowContractError",
    "ComparativeCashflowFact",
    "derive_comparative_cashflow_contract",
    "derive_h125_cashflow_version_relation",
    "extract_comparative_cashflow_contract",
]
