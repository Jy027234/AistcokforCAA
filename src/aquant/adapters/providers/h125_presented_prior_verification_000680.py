"""Independently verify H125 profit values shown in the H126 report.

This finite helper reads the prior column of three separately labelled cells
from the H126 consolidated income statement, then applies the published profit
identity.  It does not create or admit financial facts.
"""
from __future__ import annotations

import hashlib
import io
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Mapping

from .cninfo_statement_geometry import _ruled_page_tables, extract_grid_statement_cells


INSTRUMENT_ID = "000680"
SOURCE_PERIOD = date(2026, 6, 30)
FACT_PERIOD = date(2025, 6, 30)
_CURRENT_HEADER = "本期发生额"
_PRIOR_HEADER = "上期发生额"
_CURRENT_REPORT_PERIOD = "2026年半年度"
_SCALE = {"元": Decimal(1), "千元": Decimal(1000), "万元": Decimal(10000)}
_METRICS = ("net_profit_attributable", "net_profit_consolidated")
_LABELS = {
    "net_profit_attributable": re.compile(
        r"^归属于母公司(?:股东|所有者)的?净利润(?:\(净亏损以.*号填列\))?$"),
    "net_profit_consolidated": re.compile(
        r"^净利润(?:\(净亏损以.*号填列\))?$"),
    "minority_profit": re.compile(
        r"^少数股东损益(?:\(净亏损以.*号填列\))?$"),
}
_NUMBER = re.compile(r"^-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")


class H125PriorVerificationError(ValueError):
    """Invalid invocation or contradictory H126/H125 native-cell evidence."""


def _compact(value: object) -> str:
    text = re.sub(r"\s+", "", str(value or ""))
    text = text.replace("（", "(").replace("）", ")")
    text = text.replace("－", "-").replace("−", "-")
    text = re.sub(r"^(?:[一二三四五六七八九十]+、|\d+[.、．])", "", text)
    return text


def _amount(value: object) -> Decimal | None:
    text = re.sub(r"\s+", "", str(value or ""))
    if not text or text in {"-", "—", "–", "－", "不适用"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = text.replace("−", "-").replace("－", "-")
    if not _NUMBER.fullmatch(text):
        return None
    try:
        number = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    return -number if negative else number


def _same_box(left: object, right: object, *, tolerance: float = 1.0) -> bool:
    try:
        return len(left) == 4 and len(right) == 4 and all(
            abs(float(a) - float(b)) <= tolerance for a, b in zip(left, right))
    except (TypeError, ValueError):
        return False


def _same_column(cell: object, header_cell: object) -> bool:
    try:
        if cell is None or header_cell is None or len(cell) != 4 or len(header_cell) != 4:
            return False
        return abs(float(cell[0]) - float(header_cell[0])) <= 1.0 and abs(
            float(cell[2]) - float(header_cell[2])) <= 1.0
    except (TypeError, ValueError):
        return False


def _native_cell_character_evidence(page: object, bounds: object, cell_text: object) -> dict[str, object] | None:
    """Bind extracted cell text to glyphs physically contained by that cell."""
    try:
        if bounds is None or len(bounds) != 4:
            return None
        x0, top, x1, bottom = (float(value) for value in bounds)
        if not (x0 < x1 and top < bottom):
            return None
        selected = []
        for char in page.chars:
            left, char_top = float(char["x0"]), float(char["top"])
            right, char_bottom = float(char["x1"]), float(char["bottom"])
            if (left >= x0 - 1.0 and right <= x1 + 1.0 and
                    char_top >= top - 1.0 and char_bottom <= bottom + 1.0):
                selected.append(char)
        # Use the actual table reader's text ordering on the fully contained
        # glyph subset. Sorting raw top values separates Chinese and numeric
        # glyphs with different font ascents even when they form one header.
        from pdfplumber.utils import extract_text
        glyph_text = extract_text(selected) if selected else ""
        if not selected or _compact(glyph_text) != _compact(cell_text):
            return None
        return {
            "cellText": str(cell_text or ""),
            "glyphText": glyph_text,
            "textMatchesCell": True,
            "cellBounds": [x0, top, x1, bottom],
            "containmentTolerancePoints": 1.0,
            "textReconstruction": "pdfplumber.utils.extract_text_from_fully_contained_native_glyphs",
            "characters": [
                {
                    "text": str(char.get("text") or ""),
                    "x0": float(char["x0"]), "top": float(char["top"]),
                    "x1": float(char["x1"]), "bottom": float(char["bottom"]),
                }
                for char in selected
            ],
        }
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _single_row(rows: list[list[str | None]], metric: str) -> tuple[int, list[str | None]] | None:
    pattern = _LABELS[metric]
    matches = [(index, row) for index, row in enumerate(rows)
               if row and pattern.fullmatch(_compact(row[0]))]
    return matches[0] if len(matches) == 1 else None


def _evaluate_prior_profit_values(
    *,
    source_report_period_end: date,
    fact_period_end: date,
    current_header: str,
    prior_header: str,
    amount_unit: str,
    prior_values_yuan: Mapping[str, object],
    expected_values_yuan: Mapping[str, object],
) -> dict[str, object]:
    """Pure finite contract after native rows and columns have been selected."""
    required = {*_METRICS, "minority_profit"}
    if (source_report_period_end != SOURCE_PERIOD or fact_period_end != FACT_PERIOD or
            current_header != _CURRENT_HEADER or prior_header != _PRIOR_HEADER or
            amount_unit not in _SCALE or set(prior_values_yuan) != required or
            set(expected_values_yuan) != set(_METRICS)):
        return {"status": "UNKNOWN", "reason": "period, column, unit, or component set is incomplete"}
    values = {metric: _amount(prior_values_yuan[metric]) for metric in required}
    expected = {metric: _amount(expected_values_yuan[metric]) for metric in _METRICS}
    if any(value is None for value in values.values()) or any(
            value is None for value in expected.values()):
        return {"status": "UNKNOWN", "reason": "a selected prior amount is not an exact yuan number"}
    identity = values["net_profit_attributable"] + values["minority_profit"]
    identity_holds = identity == values["net_profit_consolidated"]
    expected_holds = all(values[metric] == expected[metric] for metric in _METRICS)
    return {
        "status": "VERIFIED" if identity_holds and expected_holds else "REJECTED",
        "reason": None if identity_holds and expected_holds else
                  "selected prior amounts fail the profit identity or expected-value comparison",
        "identityHolds": identity_holds,
        "expectedValuesHold": expected_holds,
        "recomputedConsolidatedYuan": str(identity),
    }


def verify_h126_presented_h125_prior(
    pdf_bytes: bytes,
    *,
    source_report_period_end: date,
    fact_period_end: date,
    expected_values_yuan: Mapping[str, object],
) -> dict[str, object]:
    """Verify the two H125 profit values using H126's native PRIOR cells.

    The H126 table's own ruled-grid extractor binds issuer, source report,
    CURRENT/PRIOR header labels, statement unit, and physical table.  This
    helper then selects the PRIOR cells for attributed profit, consolidated
    profit, and minority profit from that table and checks their independent
    identity before comparing the two requested fact amounts.
    """
    if source_report_period_end != SOURCE_PERIOD or fact_period_end != FACT_PERIOD:
        raise H125PriorVerificationError(
            "this helper only covers H126 (2026-06-30) PRIOR for H125 (2025-06-30)")
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise H125PriorVerificationError("pdf_bytes must be the original PDF byte sequence")
    if not isinstance(expected_values_yuan, Mapping) or set(expected_values_yuan) != set(_METRICS):
        raise H125PriorVerificationError("expected_values_yuan must contain exactly the two profit metrics")
    expected: dict[str, Decimal] = {}
    for metric in _METRICS:
        value = _amount(expected_values_yuan[metric])
        if value is None:
            raise H125PriorVerificationError(f"{metric}: expected amount is not an exact yuan number")
        expected[metric] = value

    anchors = extract_grid_statement_cells(
        pdf_bytes, instrument_id=INSTRUMENT_ID, period_end=SOURCE_PERIOD)
    by_field: dict[str, list[object]] = {metric: [] for metric in _METRICS}
    for cell in anchors:
        if getattr(cell, "field", None) in by_field:
            by_field[cell.field].append(cell)
    if any(len(by_field[metric]) != 1 for metric in _METRICS):
        return _unknown("H126 ruled-grid anchors for the two profit rows are not unique")

    attributable_anchor = by_field["net_profit_attributable"][0]
    consolidated_anchor = by_field["net_profit_consolidated"][0]
    cells = (attributable_anchor, consolidated_anchor)
    for cell in cells:
        evidence = cell.as_evidence()
        if (cell.period_end != SOURCE_PERIOD or cell.statement != "利润表" or
                cell.current_header != _CURRENT_HEADER or cell.prior_header != _PRIOR_HEADER or
                cell.amount_unit not in _SCALE or
                not any(INSTRUMENT_ID in str(row[-1]) for row in cell.issuer_evidence) or
                not any(_CURRENT_REPORT_PERIOD in str(row[1]) for row in cell.report_period_evidence) or
                evidence.get("currency") != "CNY"):
            return _unknown("H126 source identity, income-statement scope, dated headers, or unit is incomplete")
    if (attributable_anchor.page != consolidated_anchor.page or
            attributable_anchor.heading_page != consolidated_anchor.heading_page or
            not _same_box(attributable_anchor.table_bbox, consolidated_anchor.table_bbox) or
            attributable_anchor.header_cells != consolidated_anchor.header_cells or
            attributable_anchor.current_column != consolidated_anchor.current_column or
            attributable_anchor.amount_unit != consolidated_anchor.amount_unit):
        return _unknown("profit rows do not bind to one H126 ruled income-statement table")

    header_cells = tuple(_compact(value) for value in attributable_anchor.header_cells)
    prior_indexes = [index for index, value in enumerate(header_cells)
                     if value == _compact(_PRIOR_HEADER)]
    current_indexes = [index for index, value in enumerate(header_cells)
                       if value == _compact(_CURRENT_HEADER)]
    if (len(prior_indexes) != 1 or len(current_indexes) != 1 or
            current_indexes[0] != attributable_anchor.current_column or
            prior_indexes[0] == current_indexes[0]):
        return _unknown("H126 table headers do not provide distinct CURRENT and PRIOR columns")
    prior_index = prior_indexes[0]
    current_index = current_indexes[0]

    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - runtime dependency guard
        raise H125PriorVerificationError("pdfplumber is required for native prior-cell selection") from exc
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        if not (1 <= attributable_anchor.page <= len(pdf.pages)):
            return _unknown("H126 income-statement page is outside the supplied PDF")
        page = pdf.pages[attributable_anchor.page - 1]
        candidates = [table for table in _ruled_page_tables(
            page, SOURCE_PERIOD, "利润表", attributable_anchor.unit_declaration)
                      if _same_box(table.bbox, attributable_anchor.table_bbox)]
        if len(candidates) != 1:
            return _unknown("original ruled-table boundary cannot be uniquely rebound within 1 point")
        table = candidates[0]
        rows = table.extract()
        data = [list(row) for row in rows]
        header_matches = [index for index, row in enumerate(data)
                          if tuple(_compact(value) for value in row) == header_cells]
        header_page = page
        header_table = table
        header_data = data
        if len(header_matches) == 0 and attributable_anchor.heading_page < attributable_anchor.page:
            # The existing grid reader carries a dated header into a continuation
            # table. Rebind that same statement's real heading-page header; never
            # infer the year from an amount or copy a CURRENT qualification.
            heading_page = pdf.pages[attributable_anchor.heading_page - 1]
            heading_matches = heading_page.search(r"\s*".join(
                re.escape(char) for char in re.sub(r"\s+", "", attributable_anchor.heading)))
            if len(heading_matches) != 1:
                return _unknown("the income-statement heading cannot bind its inherited dated header")
            inherited = []
            for possible in _ruled_page_tables(
                    heading_page, SOURCE_PERIOD, "利润表", attributable_anchor.unit_declaration):
                if (possible.bbox[1] < float(heading_matches[0]["bottom"]) or
                        abs(float(possible.bbox[0]) - float(table.bbox[0])) > 1.0 or
                        abs(float(possible.bbox[2]) - float(table.bbox[2])) > 1.0):
                    continue
                possible_data = [list(row) for row in possible.extract()]
                for index, row in enumerate(possible_data):
                    if tuple(_compact(value) for value in row) != header_cells:
                        continue
                    bounds = possible.rows[index].cells
                    # Each selected profit row is checked against these dated
                    # amount-column boundaries below; an unrelated first row
                    # may legitimately have merged cells on a continuation.
                    if (len(bounds) == len(header_cells) and
                            bounds[current_index] is not None and bounds[prior_index] is not None):
                        inherited.append((possible, possible_data, index))
            if len(inherited) != 1:
                return _unknown("same-statement dated header cannot uniquely bind the continuation columns")
            header_table, header_data, header_row_index = inherited[0]
            header_page = heading_page
        elif len(header_matches) == 1:
            header_row_index = header_matches[0]
        else:
            return _unknown("native H126 table does not contain one exact dated header row")
        header_bounds = header_table.rows[header_row_index].cells
        if (len(header_bounds) != len(header_cells) or
                header_bounds[current_index] is None or header_bounds[prior_index] is None or
                float(header_bounds[current_index][0]) >= float(header_bounds[prior_index][0])):
            return _unknown("native current/prior header cell geometry is incomplete")
        current_header_characters = _native_cell_character_evidence(
            header_page, header_bounds[current_index], header_data[header_row_index][current_index])
        prior_header_characters = _native_cell_character_evidence(
            header_page, header_bounds[prior_index], header_data[header_row_index][prior_index])
        if current_header_characters is None or prior_header_characters is None:
            return _unknown("dated header cell glyphs are not uniquely contained by their native cells")

        selected: dict[str, dict[str, object]] = {}
        for metric in (*_METRICS, "minority_profit"):
            found = _single_row(data, metric)
            if found is None:
                return _unknown(f"native H126 {metric} row is missing or ambiguous")
            row_index, row = found
            row_bounds = table.rows[row_index].cells
            if (len(row) != len(header_cells) or len(row_bounds) != len(header_cells) or
                    not _same_column(row_bounds[current_index], header_bounds[current_index]) or
                    not _same_column(row_bounds[prior_index], header_bounds[prior_index]) or
                    row_bounds[prior_index] is None):
                return _unknown(f"native H126 {metric} row is not aligned to the dated table columns")
            prior_characters = _native_cell_character_evidence(page, row_bounds[prior_index], row[prior_index])
            if prior_characters is None:
                return _unknown(f"native H126 {metric} PRIOR glyphs are not uniquely contained by their cell")
            prior_value = _amount(row[prior_index])
            if prior_value is None:
                return _unknown(f"native H126 {metric} PRIOR cell has no exact amount token")
            current_value = _amount(row[current_index])
            if metric in _METRICS and current_value is None:
                return _unknown(f"native H126 {metric} CURRENT cell is unavailable for table binding")
            selected[metric] = {
                "rowLabel": str(row[0] or ""),
                "currentCell": str(row[current_index] or ""),
                "priorCell": str(row[prior_index] or ""),
                "currentCellBounds": list(row_bounds[current_index]),
                "priorCellBounds": list(row_bounds[prior_index]),
                "priorCellCharacterEvidence": prior_characters,
                "page": attributable_anchor.page,
                "amountUnit": attributable_anchor.amount_unit,
                "priorAmountYuan": str(prior_value * _SCALE[attributable_anchor.amount_unit]),
            }
        for metric, anchor in (("net_profit_attributable", attributable_anchor),
                               ("net_profit_consolidated", consolidated_anchor)):
            found_current = _amount(selected[metric]["currentCell"])
            if found_current is None or found_current * _SCALE[anchor.amount_unit] != anchor.amount_yuan:
                return _unknown(f"native {metric} CURRENT cell does not match its unique grid anchor")

    prior_values = {metric: Decimal(str(selected[metric]["priorAmountYuan"]))
                    for metric in (*_METRICS, "minority_profit")}
    assessment = _evaluate_prior_profit_values(
        source_report_period_end=SOURCE_PERIOD, fact_period_end=FACT_PERIOD,
        current_header=_CURRENT_HEADER, prior_header=_PRIOR_HEADER,
        amount_unit=attributable_anchor.amount_unit,
        prior_values_yuan=prior_values, expected_values_yuan=expected)
    status = str(assessment["status"])
    identity_holds = bool(assessment.get("identityHolds", False))
    identity_value = assessment.get("recomputedConsolidatedYuan")
    return {
        "schema": "aquant.cninfo.h126-presented-h125-prior-verification.v1",
        "status": status,
        "instrumentId": INSTRUMENT_ID,
        "sourceReportPeriodEnd": SOURCE_PERIOD.isoformat(),
        "factPeriodEnd": FACT_PERIOD.isoformat(),
        "sourcePdfSha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "sourcePdfByteSize": len(pdf_bytes),
        "verificationScope": "PRESENTED_H125_PRIOR_PROFIT_NUMERICAL_COMPONENTS_ONLY",
        "statement": {
            "role": "合并", "name": "利润表",
            "currentHeader": _CURRENT_HEADER, "priorHeader": _PRIOR_HEADER,
            "headerCells": list(attributable_anchor.header_cells),
            "currentColumnIndex": current_index, "priorColumnIndex": prior_index,
            "headerCellBounds": [list(header_bounds[current_index]), list(header_bounds[prior_index])],
            "headerPage": header_page.page_number,
            "headerTableBounds": list(header_table.bbox),
            "currentHeaderCharacterEvidence": current_header_characters,
            "priorHeaderCharacterEvidence": prior_header_characters,
            "amountUnit": attributable_anchor.amount_unit,
            "unitDeclaration": attributable_anchor.unit_declaration,
            "tableBounds": list(attributable_anchor.table_bbox),
            "page": attributable_anchor.page,
        },
        "gridAnchors": {
            "net_profit_attributable": attributable_anchor.as_evidence(),
            "net_profit_consolidated": consolidated_anchor.as_evidence(),
        },
        "nativePriorRows": selected,
        "profitIdentity": {
            "formula": "net_profit_consolidated = net_profit_attributable + minority_profit",
            "attributableYuan": str(prior_values["net_profit_attributable"]),
            "minorityProfitYuan": str(prior_values["minority_profit"]),
            "consolidatedYuan": str(prior_values["net_profit_consolidated"]),
            "recomputedConsolidatedYuan": identity_value,
            "holds": identity_holds,
        },
        "metrics": {
            metric: {
                "status": status,
                "amountYuan": str(prior_values[metric]),
                "expectedYuan": str(expected[metric]),
                "columnRole": "PRIOR",
                "independentPath": "H126_PRIOR_PROFIT_IDENTITY",
                "identityHolds": identity_holds,
            }
            for metric in _METRICS
        },
        "admission": "NOT_ADMITTED",
    }


def _unknown(reason: str) -> dict[str, object]:
    return {
        "schema": "aquant.cninfo.h126-presented-h125-prior-verification.v1",
        "status": "UNKNOWN",
        "instrumentId": INSTRUMENT_ID,
        "sourceReportPeriodEnd": SOURCE_PERIOD.isoformat(),
        "factPeriodEnd": FACT_PERIOD.isoformat(),
        "reason": reason,
        "metrics": {metric: {"status": "UNKNOWN", "reason": reason} for metric in _METRICS},
        "admission": "NOT_ADMITTED",
    }
