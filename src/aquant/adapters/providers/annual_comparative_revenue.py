"""Narrow candidate contract for 601069's FY24 revenue comparative.

This binds an existing FY24 CURRENT reference to the FY24 PRIOR_ADJUSTED column
in 601069's FY25 annual report. It is a relation candidate only: it does not
qualify a source, resolve the complete version set, create a FinancialFact, or
change the original CURRENT evidence/as_evidence object.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Mapping


SCHEMA = "aquant.cninfo.601069-fy24-revenue-comparative-candidate-v1"
_SPACE = re.compile(r"\s+")


class ComparativeRevenueContractError(ValueError):
    """Saved or freshly parsed source cells do not support this relation."""


def _compact(value: object) -> str:
    return _SPACE.sub("", str(value or "")).replace("：", ":")


def _money(value: object, label: str) -> Decimal:
    text = _compact(value)
    try:
        if text.startswith("(") and text.endswith(")"):
            amount = -Decimal(text[1:-1].replace(",", ""))
        else:
            amount = Decimal(text.replace(",", ""))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ComparativeRevenueContractError(f"{label} is not a money value") from exc
    if not amount.is_finite():
        raise ComparativeRevenueContractError(f"{label} must be finite")
    return amount


@dataclass(frozen=True, slots=True)
class ExistingCurrent:
    instrument_id: str
    announcement_id: str
    fact_period_end: date
    amount_yuan: Decimal
    source_page: int
    source_row_label: str
    source_raw_cell: str
    source_pointer: str

    def reference(self) -> dict[str, object]:
        """A reference only; the existing current evidence stays untouched."""
        return {
            "instrumentId": self.instrument_id,
            "announcementId": self.announcement_id,
            "factPeriodEnd": self.fact_period_end.isoformat(),
            "amountYuan": str(self.amount_yuan),
            "sourcePointer": self.source_pointer,
        }


@dataclass(frozen=True, slots=True)
class AnnualIncomeRow:
    announcement_id: str
    instrument_id: str
    source_report_period_end: date
    page: int
    role: str
    statement: str
    row_label: str
    unit: str
    current_header: str
    prior_header: str
    current_raw: str
    prior_raw: str
    current_amount_yuan: Decimal
    prior_amount_yuan: Decimal
    raw_line: str


@dataclass(frozen=True, slots=True)
class NativeCell:
    text: str
    native_text: str
    bbox: tuple[float, float, float, float]
    native_character_count: int

    @classmethod
    def from_helper(cls, value: Mapping[str, object]) -> "NativeCell":
        bbox = value.get("bbox")
        if (not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or
                not isinstance(value.get("text"), str) or
                not isinstance(value.get("nativeText"), str) or
                not isinstance(value.get("nativeCharacterCount"), int) or
                value["nativeCharacterCount"] <= 0 or
                _compact(value["text"]) != _compact(value["nativeText"])):
            raise ComparativeRevenueContractError("native p7 cell evidence is incomplete")
        return cls(
            text=str(value["text"]), native_text=str(value["nativeText"]),
            bbox=tuple(float(item) for item in bbox),
            native_character_count=int(value["nativeCharacterCount"]),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "nativeText": self.native_text,
            "bbox": list(self.bbox),
            "nativeCharacterCount": self.native_character_count,
        }


@dataclass(frozen=True, slots=True)
class AnnualAdjustmentRow:
    announcement_id: str
    instrument_id: str
    source_report_period_end: date
    page: int
    table_index: int
    row_index: int
    row_label: str
    unit: str
    header_2024: NativeCell
    role_2024_adjusted: NativeCell
    role_2024_unadjusted: NativeCell
    current_2025: NativeCell
    adjusted_2024: NativeCell
    unadjusted_2024: NativeCell

    def amounts(self) -> tuple[Decimal, Decimal, Decimal]:
        return (
            _money(self.current_2025.text, "FY25 annual summary CURRENT"),
            _money(self.adjusted_2024.text, "FY24 annual summary adjusted PRIOR"),
            _money(self.unadjusted_2024.text, "FY24 annual summary unadjusted PRIOR"),
        )


@dataclass(frozen=True, slots=True)
class RevenueComparativeCandidate:
    original_current: ExistingCurrent
    annual_row: AnnualIncomeRow
    adjustment_row: AnnualAdjustmentRow
    adjusted_prior_yuan: Decimal
    unadjusted_prior_yuan: Decimal
    current_report_yuan: Decimal
    delta_yuan: Decimal

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "instrumentId": "601069",
            "metric": "revenue",
            "statementScope": "CONSOLIDATED",
            "originalCurrentReference": self.original_current.reference(),
            "comparativePriorAdjusted": {
                "sourceAnnouncementId": self.annual_row.announcement_id,
                "sourceReportPeriodEnd": self.annual_row.source_report_period_end.isoformat(),
                "factPeriodEnd": "2024-12-31",
                "columnRole": "PRIOR_ADJUSTED",
                "page": self.annual_row.page,
                "statement": self.annual_row.statement,
                "rowLabel": self.annual_row.row_label,
                "unit": self.annual_row.unit,
                "header": self.annual_row.prior_header,
                "rawCell": self.annual_row.prior_raw,
                "amountYuan": str(self.adjusted_prior_yuan),
                "rawLine": self.annual_row.raw_line,
            },
            "p93AnnualCurrentContext": {
                "sourceAnnouncementId": self.annual_row.announcement_id,
                "sourceReportPeriodEnd": self.annual_row.source_report_period_end.isoformat(),
                "factPeriodEnd": "2025-12-31",
                "columnRole": "CURRENT",
                "page": self.annual_row.page,
                "header": self.annual_row.current_header,
                "rawCell": self.annual_row.current_raw,
                "amountYuan": str(self.annual_row.current_amount_yuan),
            },
            "annualReportCrossCheck": {
                "page": self.adjustment_row.page,
                "tableIndex": self.adjustment_row.table_index,
                "rowLabel": self.adjustment_row.row_label,
                "unit": self.adjustment_row.unit,
                "current2025AmountYuan": str(self.current_report_yuan),
                "adjustedPrior2024AmountYuan": str(self.adjusted_prior_yuan),
                "unadjustedPrior2024AmountYuan": str(self.unadjusted_prior_yuan),
                "header2024": self.adjustment_row.header_2024.as_dict(),
                "role2024Adjusted": self.adjustment_row.role_2024_adjusted.as_dict(),
                "role2024Unadjusted": self.adjustment_row.role_2024_unadjusted.as_dict(),
                "current2025Cell": self.adjustment_row.current_2025.as_dict(),
                "adjustedPrior2024Cell": self.adjustment_row.adjusted_2024.as_dict(),
                "unadjustedPrior2024Cell": self.adjustment_row.unadjusted_2024.as_dict(),
            },
            "deltaAdjustedPriorMinusOriginalCurrentYuan": str(self.delta_yuan),
            "candidateDisposition": "AFFECTS_VERSION_CHAIN",
            "qualificationBoundary": "SOURCE_QUALIFICATION_AND_COMPLETE_VERSION_REVIEW_REQUIRED",
        }


def existing_current_from_source19(checkpoint: Mapping[str, object]) -> ExistingCurrent:
    """Read the previously qualified CURRENT candidate without rewriting it."""
    try:
        report = checkpoint["report"]
        evidence = report["candidateEvidence"]["candidate"]
        candidates = evidence["candidates"]
        if not isinstance(report, Mapping) or not isinstance(evidence, Mapping):
            raise TypeError
        if not isinstance(candidates, list):
            raise TypeError
        matches = [item for item in candidates if isinstance(item, Mapping) and
                   item.get("field") == "revenue"]
        if len(matches) != 1:
            raise ComparativeRevenueContractError(
                "source19 must contain exactly one existing revenue CURRENT cell")
        item = matches[0]
        report_identity = (report.get("announcementId"), report.get("instrumentId"),
                           report.get("periodEnd"))
        evidence_identity = (evidence.get("instrument_id"), evidence.get("period_end"))
        if (report_identity != ("1222983253", "601069", "2024-12-31") or
                evidence_identity != ("601069", "2024-12-31") or
                item.get("period_end") != "2024-12-31" or
                item.get("amount_unit") != "元" or item.get("currency") != "CNY" or
                item.get("pdf_page") != 99 or
                _compact(item.get("column_header")) != "2024年度" or
                _compact(item.get("display_row_label")) != "其中:营业收入"):
            raise ComparativeRevenueContractError(
                "saved source19 CURRENT identity, period, row, unit, or column differs")
        raw_cells = item.get("source_cells")
        if (not isinstance(raw_cells, list) or len(raw_cells) < 2 or
                _compact(raw_cells[0]) != "其中:营业收入"):
            raise ComparativeRevenueContractError("saved source19 CURRENT raw cell is absent")
        raw = str(raw_cells[1])
        amount = _money(item.get("value_yuan"), "source19 CURRENT amount")
        if _money(raw, "source19 CURRENT raw cell") != amount:
            raise ComparativeRevenueContractError(
                "saved source19 CURRENT raw cell differs from its recorded amount")
        return ExistingCurrent(
            instrument_id="601069", announcement_id="1222983253",
            fact_period_end=date(2024, 12, 31), amount_yuan=amount,
            source_page=99, source_row_label=str(item["display_row_label"]),
            source_raw_cell=raw,
            source_pointer="report.candidateEvidence.candidate.candidates[field=revenue]",
        )
    except ComparativeRevenueContractError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ComparativeRevenueContractError(
            "saved source19 CURRENT evidence shape is incomplete") from exc


def extract_annual_income_row(annual_document: Mapping[str, object]) -> AnnualIncomeRow:
    """Re-extract the annual consolidated income-statement row from archived bytes."""
    try:
        metadata = annual_document["metadata"]
        body = annual_document["body"]
        if not isinstance(metadata, Mapping) or not isinstance(body, bytes):
            raise TypeError
        if (metadata.get("announcementId") != "1225082554" or
                metadata.get("code") != "601069" or
                metadata.get("periodEnd") != "2025-12-31" or
                _compact(metadata.get("title")) != "西部黄金股份有限公司2025年年度报告"):
            raise ComparativeRevenueContractError("FY25 annual source identity differs")
        if not body.startswith(b"%PDF-"):
            raise ComparativeRevenueContractError("annual source body is not PDF bytes")
        from aquant.adapters.providers.cninfo_statement_sections import (
            ROLE_CONSOLIDATED,
            extract_statement_sections,
        )

        sections = extract_statement_sections(
            body, period_end=date(2025, 12, 31), roles=(ROLE_CONSOLIDATED,),
            statements=("利润表",),
        )
        section = sections.require(ROLE_CONSOLIDATED, "利润表")
        matches = [row for row in section.rows
                   if _compact(row.label) == "其中:营业收入"]
        if len(matches) != 1:
            raise ComparativeRevenueContractError(
                "FY25 consolidated income statement must have one revenue row")
        row = matches[0]
        if (row.page != 93 or section.unit != "元" or
                _compact(section.current_header) != "2025年度" or
                _compact(section.prior_header) != "2024年度" or
                not (section.start_page <= row.page <= section.end_page)):
            raise ComparativeRevenueContractError(
                "FY25 annual statement page, unit, or comparative headers differ")
        current = _money(row.current_cell, "FY25 annual CURRENT row cell")
        prior = _money(row.prior_cell, "FY24 annual PRIOR row cell")
        if current != row.amount or prior != row.prior_amount:
            raise ComparativeRevenueContractError(
                "annual parsed cell text differs from its statement row amounts")
        return AnnualIncomeRow(
            announcement_id="1225082554", instrument_id="601069",
            source_report_period_end=date(2025, 12, 31), page=row.page,
            role=section.role, statement=section.statement, row_label=row.label,
            unit=section.unit, current_header=section.current_header,
            prior_header=section.prior_header, current_raw=row.current_cell,
            prior_raw=row.prior_cell, current_amount_yuan=current,
            prior_amount_yuan=prior, raw_line=row.raw_line,
        )
    except ComparativeRevenueContractError:
        raise
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise ComparativeRevenueContractError(
            "FY25 annual income-statement extraction failed") from exc


def extract_annual_adjustment_row(annual_document: Mapping[str, object]) -> AnnualAdjustmentRow:
    """Read FY25 p7's literal FY24 adjusted/unadjusted cells and native geometry."""
    try:
        metadata = annual_document["metadata"]
        body = annual_document["body"]
        if not isinstance(metadata, Mapping) or not isinstance(body, bytes):
            raise TypeError
        if (metadata.get("announcementId") != "1225082554" or
                metadata.get("code") != "601069" or
                metadata.get("periodEnd") != "2025-12-31" or
                _compact(metadata.get("title")) != "西部黄金股份有限公司2025年年度报告"):
            raise ComparativeRevenueContractError("FY25 annual source identity differs")
        if not body.startswith(b"%PDF-"):
            raise ComparativeRevenueContractError("annual source body is not PDF bytes")
        from aquant.adapters.providers.s2_revision_impact import _601069_native_pages

        pages = _601069_native_pages({"body": body}, (7,))
        return annual_adjustment_from_native_page(pages[7])
    except ComparativeRevenueContractError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, ArithmeticError) as exc:
        raise ComparativeRevenueContractError(
            "FY25 annual native summary extraction failed") from exc


def annual_adjustment_from_native_page(page: Mapping[str, object]) -> AnnualAdjustmentRow:
    """Bind a saved/native p7 page using the existing 601069 cell geometry helpers."""
    try:
        from aquant.adapters.providers.s2_revision_impact import (
            _601069_cell,
            _601069_row_index,
            _601069_same_columns,
            _601069_table,
            _601069_table_header,
            _601069_unit_before,
        )

        if page.get("physicalPage") != 7:
            raise ComparativeRevenueContractError("annual summary page is not physical p7")
        page_number_map = {7: page}
        native_page, table = _601069_table(page_number_map, 7, 0)
        _601069_table_header(
            native_page, table,
            ["主要会计数据", "2025年", "2024年", "", "本期比上年同期增减(%)", "2023年", ""],
        )
        if (len(table.get("rows", ())) < 2 or
                _compact(table["rows"][1][2]) != "调整后" or
                _compact(table["rows"][1][3]) != "调整前"):
            raise ComparativeRevenueContractError(
                "FY24 summary roles are not literally adjusted/unadjusted")
        row_index = _601069_row_index(table, "营业收入")
        if len(table["rows"][row_index]) != 7:
            raise ComparativeRevenueContractError("annual summary revenue row has wrong width")
        _601069_same_columns(table, 0, row_index, (1,))
        _601069_same_columns(table, 1, row_index, (2, 3))
        unit = _601069_unit_before(native_page, table)
        if _compact(unit.get("text")) != "单位:元":
            raise ComparativeRevenueContractError("annual summary unit is not yuan")
        header_2024 = NativeCell.from_helper(_601069_cell(native_page, table, 0, 2))
        role_adjusted = NativeCell.from_helper(_601069_cell(native_page, table, 1, 2))
        role_unadjusted = NativeCell.from_helper(_601069_cell(native_page, table, 1, 3))
        cells = [NativeCell.from_helper(_601069_cell(native_page, table, row_index, column))
                 for column in (1, 2, 3)]
        if (_compact(header_2024.text) != "2024年" or
                _compact(role_adjusted.text) != "调整后" or
                _compact(role_unadjusted.text) != "调整前"):
            raise ComparativeRevenueContractError("annual summary role geometry differs")
        return AnnualAdjustmentRow(
            announcement_id="1225082554", instrument_id="601069",
            source_report_period_end=date(2025, 12, 31), page=7,
            table_index=0, row_index=row_index, row_label="营业收入", unit="元",
            header_2024=header_2024, role_2024_adjusted=role_adjusted,
            role_2024_unadjusted=role_unadjusted, current_2025=cells[0],
            adjusted_2024=cells[1], unadjusted_2024=cells[2],
        )
    except ComparativeRevenueContractError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, ArithmeticError) as exc:
        raise ComparativeRevenueContractError(
            "FY25 annual native summary page parsing failed") from exc


def bind_revenue_comparative_candidate(
    original_current: ExistingCurrent,
    annual_row: AnnualIncomeRow,
    adjustment_row: AnnualAdjustmentRow,
) -> RevenueComparativeCandidate:
    """Bind only the explicit FY24 PRIOR_ADJUSTED comparison and its p7 cross-check."""
    if (original_current.instrument_id != "601069" or
            original_current.announcement_id != "1222983253" or
            original_current.fact_period_end != date(2024, 12, 31) or
            original_current.source_page != 99 or
            _compact(original_current.source_row_label) != "其中:营业收入"):
        raise ComparativeRevenueContractError("existing FY24 CURRENT reference differs")
    if _money(original_current.source_raw_cell, "existing FY24 CURRENT raw cell") != original_current.amount_yuan:
        raise ComparativeRevenueContractError("existing FY24 CURRENT raw cell differs from its amount")
    if (annual_row.announcement_id != "1225082554" or
            annual_row.instrument_id != "601069" or
            annual_row.source_report_period_end != date(2025, 12, 31) or
            annual_row.page != 93 or annual_row.role != "合并" or
            annual_row.statement != "利润表" or annual_row.unit != "元" or
            _compact(annual_row.row_label) != "其中:营业收入" or
            _compact(annual_row.current_header) != "2025年度" or
            _compact(annual_row.prior_header) != "2024年度"):
        raise ComparativeRevenueContractError("annual statement CURRENT/PRIOR contract differs")
    if (_money(annual_row.current_raw, "annual p93 CURRENT raw cell") !=
            annual_row.current_amount_yuan or
            _money(annual_row.prior_raw, "annual p93 PRIOR raw cell") !=
            annual_row.prior_amount_yuan):
        raise ComparativeRevenueContractError("annual p93 raw cells differ from their amounts")
    compact_line = _compact(annual_row.raw_line).replace(",", "")
    if ("其中:营业收入" not in compact_line or
            _compact(annual_row.current_raw).replace(",", "") not in compact_line or
            _compact(annual_row.prior_raw).replace(",", "") not in compact_line):
        raise ComparativeRevenueContractError("annual p93 raw line does not contain both cells")
    if (adjustment_row.announcement_id != annual_row.announcement_id or
            adjustment_row.instrument_id != annual_row.instrument_id or
            adjustment_row.source_report_period_end != annual_row.source_report_period_end or
            adjustment_row.page != 7 or adjustment_row.table_index != 0 or
            adjustment_row.unit != "元" or
            _compact(adjustment_row.row_label) != "营业收入" or
            _compact(adjustment_row.header_2024.text) != "2024年" or
            _compact(adjustment_row.role_2024_adjusted.text) != "调整后" or
            _compact(adjustment_row.role_2024_unadjusted.text) != "调整前"):
        raise ComparativeRevenueContractError("annual p7 adjustment row identity differs")
    current_2025, adjusted_2024, unadjusted_2024 = adjustment_row.amounts()
    if (annual_row.current_amount_yuan != current_2025 or
            annual_row.prior_amount_yuan != adjusted_2024 or
            original_current.amount_yuan != unadjusted_2024):
        raise ComparativeRevenueContractError(
            "annual p93 cells do not bind to p7's explicit CURRENT/adjusted/unadjusted cells")
    delta = adjusted_2024 - original_current.amount_yuan
    if delta == 0:
        raise ComparativeRevenueContractError(
            "equal cells require separate full-version disposition; do not infer NO_EFFECT")
    return RevenueComparativeCandidate(
        original_current=original_current, annual_row=annual_row,
        adjustment_row=adjustment_row, adjusted_prior_yuan=adjusted_2024,
        unadjusted_prior_yuan=unadjusted_2024, current_report_yuan=current_2025,
        delta_yuan=delta,
    )


def produce_601069_fy24_revenue_candidate(
    source19_checkpoint: Mapping[str, object],
    annual_document: Mapping[str, object],
) -> RevenueComparativeCandidate:
    """Build a candidate from the existing CURRENT reference and report source bytes."""
    original_current = existing_current_from_source19(source19_checkpoint)
    annual_row = extract_annual_income_row(annual_document)
    adjustment_row = extract_annual_adjustment_row(annual_document)
    return bind_revenue_comparative_candidate(
        original_current, annual_row, adjustment_row,
    )

