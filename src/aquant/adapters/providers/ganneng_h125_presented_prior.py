"""Bounded native proof for Jiangxi Ganneng's H125 values presented in H126.

The extractor accepts only the two reviewed CNINFO originals below. It reads a
small set of native-text pages and ruled table cells, emits candidate evidence
for the 2025-06-30 ``PRESENTED_PRIOR`` version, and never creates financial
facts, PIT timestamps, an admission decision, or a no-effect conclusion.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import re
from typing import Any, Callable, Mapping, Sequence


INSTRUMENT_ID = "000899"
COMPANY = "江西赣能股份有限公司"
FACT_PERIOD_END = date(2025, 6, 30)
SOURCE_REPORT_PERIOD_END = date(2026, 6, 30)
SOURCE_REPORT_ANNOUNCEMENT_ID = "1225490300"
SOURCE_REPORT_TITLE = "2026年半年度报告"
SOURCE_REPORT_PUBLISHED_DATE = "2026-08-22"
SOURCE_REPORT_SHA256 = "0908a0080bdd676f3fbddcd3180cf4b3a91c32c980fd3b7643b313972f7587b8"
SOURCE_REPORT_PAGE_COUNT = 133
CORRECTION_ANNOUNCEMENT_ID = "1225619745"
CORRECTION_TITLE = "关于同一控制下企业合并追溯调整财务数据的公告"
CORRECTION_PUBLISHED_DATE = "2026-10-24"
CORRECTION_SHA256 = "eb7fc614c808bd26667baad8f963c5baebddd3e1e48a9c74225f066927fee040"
CORRECTION_PAGE_COUNT = 11

_AMOUNT = re.compile(r"-?(?:0|[1-9]\d{0,2}(?:,\d{3})*|\d+)(?:\.\d{2})\Z")
_BBOX_TOLERANCE = 1.0


class GannengPresentedPriorError(ValueError):
    """A reviewed report identity, header, native cell, or identity check failed."""


def _compact(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).replace("　", "")


def _table_rows(table: Any) -> list[list[str | None]]:
    rows = table.extract()
    return [[None if value is None else str(value) for value in row] for row in rows]


def _cell_bbox(table: Any, row_index: int, column_index: int) -> tuple[float, float, float, float]:
    try:
        bbox = table.rows[row_index].cells[column_index]
        if bbox is None or len(bbox) != 4:
            raise ValueError
        values = tuple(float(value) for value in bbox)
    except (AttributeError, IndexError, TypeError, ValueError) as exc:
        raise GannengPresentedPriorError("native table cell has no unambiguous rectangle") from exc
    x0, top, x1, bottom = values
    if x1 <= x0 or bottom <= top or any(not (value == value and abs(value) != float("inf")) for value in values):
        raise GannengPresentedPriorError("native table cell rectangle is invalid")
    return values  # type: ignore[return-value]


def _bbox_json(bbox: Sequence[float]) -> list[float]:
    return [round(float(value), 3) for value in bbox]


def _page_text(pdf: Any, page_number: int) -> str:
    return pdf.pages[page_number - 1].extract_text(layout=False) or ""


def _require_text(pdf: Any, page_number: int, *needles: str) -> str:
    text = _page_text(pdf, page_number)
    compact = _compact(text)
    missing = [needle for needle in needles if _compact(needle) not in compact]
    if missing:
        raise GannengPresentedPriorError(
            f"physical page {page_number} does not contain required native text: {missing!r}"
        )
    return text


def _tables(pdf: Any, page_number: int) -> list[Any]:
    page = pdf.pages[page_number - 1]
    try:
        return page.find_tables({"vertical_strategy": "lines", "horizontal_strategy": "lines"})
    except Exception as exc:
        raise GannengPresentedPriorError(
            f"native ruled-table locator failed on physical page {page_number}: {type(exc).__name__}"
        ) from exc


def _find_rows(
    pdf: Any,
    page_number: int,
    predicate: Callable[[Sequence[str | None]], bool],
) -> list[tuple[Any, int, list[str | None]]]:
    found: list[tuple[Any, int, list[str | None]]] = []
    for table in _tables(pdf, page_number):
        for row_index, row in enumerate(_table_rows(table)):
            if predicate(row):
                found.append((table, row_index, row))
    return found


def _only_row(
    pdf: Any,
    page_number: int,
    predicate: Callable[[Sequence[str | None]], bool],
    label: str,
) -> tuple[Any, int, list[str | None]]:
    found = _find_rows(pdf, page_number, predicate)
    if len(found) != 1:
        raise GannengPresentedPriorError(
            f"expected one native {label} row on physical page {page_number}; found {len(found)}"
        )
    return found[0]


def _parse_amount(text: str | None, label: str) -> Decimal:
    compact = _compact(text)
    if not _AMOUNT.fullmatch(compact):
        raise GannengPresentedPriorError(f"{label} is not one unambiguous CNY amount: {text!r}")
    try:
        return Decimal(compact.replace(",", ""))
    except InvalidOperation as exc:
        raise GannengPresentedPriorError(f"{label} cannot be parsed as Decimal") from exc


def _amount_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _cell_proof(
    *,
    pdf: Any,
    page_number: int,
    table: Any,
    row_index: int,
    row: Sequence[str | None],
    column_index: int,
    field: str,
    row_label: str,
    statement: str,
    column_header: str,
    unit: str = "元",
) -> tuple[Decimal, dict[str, object]]:
    if column_index >= len(row):
        raise GannengPresentedPriorError(f"{field} row has too few native columns")
    raw = row[column_index]
    amount = _parse_amount(raw, field)
    label_bbox = _cell_bbox(table, row_index, 0)
    amount_bbox = _cell_bbox(table, row_index, column_index)
    label_text = row[0] or ""
    return amount, {
        "field": field,
        "statement": statement,
        "row_label": _compact(label_text),
        "expected_row_label": row_label,
        "physical_page": page_number,
        "column_header": column_header,
        "column_role": "PRESENTED_PRIOR",
        "unit": unit,
        "currency": "CNY",
        "native_row_label_text": label_text,
        "native_cell_text": raw,
        "value_yuan": _amount_text(amount),
        "table_bbox": _bbox_json(table.bbox),
        "row_label_bbox": _bbox_json(label_bbox),
        "amount_cell_bbox": _bbox_json(amount_bbox),
        "grid_row_index": row_index,
        "grid_column_index": column_index,
    }


def _statement_prior_cell(
    *,
    pdf: Any,
    page_number: int,
    predicate: Callable[[Sequence[str | None]], bool],
    field: str,
    row_label: str,
    statement: str,
    prior_column_index: int,
) -> tuple[Decimal, dict[str, object]]:
    table, row_index, row = _only_row(pdf, page_number, predicate, field)
    if len(row) != 3 or prior_column_index != 2:
        raise GannengPresentedPriorError(f"{field} table no longer has the reviewed current/prior grid")
    return _cell_proof(
        pdf=pdf,
        page_number=page_number,
        table=table,
        row_index=row_index,
        row=row,
        column_index=prior_column_index,
        field=field,
        row_label=row_label,
        statement=statement,
        column_header="2025年半年度",
    )


def _summary_cell(
    *, pdf: Any, predicate: Callable[[Sequence[str | None]], bool], field: str, row_label: str
) -> tuple[Decimal, dict[str, object]]:
    page_number = 7
    tables = _tables(pdf, page_number)
    qualifying: list[tuple[Any, int, list[str | None]]] = []
    for table in tables:
        rows = _table_rows(table)
        has_adjusted_prior_header = any(
            len(row) >= 4
            and _compact(row[2]) == "调整前"
            and _compact(row[3]) == "调整后"
            for row in rows[:4]
        )
        if not has_adjusted_prior_header:
            continue
        for row_index, row in enumerate(rows):
            if predicate(row):
                qualifying.append((table, row_index, row))
    if len(qualifying) != 1:
        raise GannengPresentedPriorError(
            f"expected one adjusted-prior summary row for {field}; found {len(qualifying)}"
        )
    table, row_index, row = qualifying[0]
    if len(row) != 5 or _compact(row[0]) not in ("营业收入（元）", "归属于上市公司股东的净利润（元）", "经营活动产生的现金流量净额（元）"):
        raise GannengPresentedPriorError(f"{field} summary row does not match the reviewed five-column grid")
    return _cell_proof(
        pdf=pdf,
        page_number=page_number,
        table=table,
        row_index=row_index,
        row=row,
        column_index=3,
        field=field,
        row_label=row_label,
        statement="2026 H1 report summary; 上年同期调整后",
        column_header="上年同期 / 调整后",
    )


def _equity_table_cells(pdf: Any) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    page_number = 55
    candidates: list[tuple[Any, list[list[str | None]]]] = []
    for table in _tables(pdf, page_number):
        rows = _table_rows(table)
        if len(rows) < 4 or not rows or len(rows[0]) != 16:
            continue
        period_header = any(_compact(value) == "2025年半年度" for value in rows[0])
        parent_group = any(_compact(value) == "归属于母公司所有者权益" for value in rows[1])
        subtotal = any(_compact(value) == "小计" for value in rows[2])
        minority = any(_compact(value) == "少数股东权益" for value in rows[1])
        total = any(_compact(value) == "所有者权益合计" for value in rows[1])
        if period_header and parent_group and subtotal and minority and total:
            candidates.append((table, rows))
    if len(candidates) != 1:
        raise GannengPresentedPriorError(
            f"expected one 2025 H1 consolidated equity-change table header; found {len(candidates)}"
        )
    header_table, header_rows = candidates[0]

    parent_group_columns = [i for i, value in enumerate(header_rows[1])
                            if _compact(value) == "归属于母公司所有者权益"]
    subtotal_columns = [i for i, value in enumerate(header_rows[2]) if _compact(value) == "小计"]
    minority_columns = [i for i, value in enumerate(header_rows[1])
                        if _compact(value) == "少数股东权益"]
    total_columns = [i for i, value in enumerate(header_rows[1])
                     if _compact(value) == "所有者权益合计"]
    if (len(parent_group_columns) != 1 or len(subtotal_columns) != 1
            or len(minority_columns) != 1 or len(total_columns) != 1):
        raise GannengPresentedPriorError("equity grid headers are ambiguous")
    subtotal_col = subtotal_columns[0]
    parent_bbox = _cell_bbox(header_table, 1, parent_group_columns[0])
    subtotal_header_bbox = _cell_bbox(header_table, 2, subtotal_col)
    minority_bbox = _cell_bbox(header_table, 1, minority_columns[0])
    total_bbox = _cell_bbox(header_table, 1, total_columns[0])
    if not (parent_bbox[0] <= subtotal_header_bbox[0] + _BBOX_TOLERANCE
            and subtotal_header_bbox[2] <= parent_bbox[2] + _BBOX_TOLERANCE
            and subtotal_header_bbox[2] <= minority_bbox[0] + _BBOX_TOLERANCE
            and minority_bbox[2] <= total_bbox[0] + _BBOX_TOLERANCE):
        raise GannengPresentedPriorError("parent subtotal is not geometrically under the parent-equity header")

    opening_row = _only_row(
        pdf, 56, lambda row: bool(row) and _compact(row[0]) == "二、本年期初余额",
        "2025 H1 equity opening subtotal",
    )
    movement_row = _only_row(
        pdf, 56, lambda row: bool(row) and _compact(row[0]).startswith("三、本期增减变动金额"),
        "2025 H1 equity movement subtotal",
    )
    closing_row = _only_row(
        pdf, 57, lambda row: bool(row) and _compact(row[0]) == "四、本期期末余额",
        "2025 H1 equity closing subtotal",
    )

    evidence: list[dict[str, object]] = []
    for page_number, (table, row_index, row), field, label in (
        (56, opening_row, "equity_opening", "二、本年期初余额"),
        (56, movement_row, "equity_movement", "三、本期增减变动金额"),
        (57, closing_row, "parent_equity", "四、本期期末余额"),
    ):
        if len(row) != 16:
            raise GannengPresentedPriorError(f"{field} row no longer has the reviewed 16-column equity grid")
        amount_bbox = _cell_bbox(table, row_index, subtotal_col)
        if abs(amount_bbox[0] - subtotal_header_bbox[0]) > _BBOX_TOLERANCE or abs(amount_bbox[2] - subtotal_header_bbox[2]) > _BBOX_TOLERANCE:
            raise GannengPresentedPriorError(f"{field} amount column does not align with the parent subtotal header")
        amount = _parse_amount(row[subtotal_col], field)
        evidence.append({
            "field": field,
            "statement": "合并所有者权益变动表 / 上年金额",
            "row_label": _compact(row[0]),
            "expected_row_label": label,
            "physical_page": page_number,
            "column_header": "归属于母公司所有者权益 / 小计",
            "column_role": "PRESENTED_PRIOR",
            "unit": "元",
            "currency": "CNY",
            "native_row_label_text": row[0],
            "native_cell_text": row[subtotal_col],
            "value_yuan": _amount_text(amount),
            "table_bbox": _bbox_json(table.bbox),
            "header_parent_group_bbox": _bbox_json(parent_bbox),
            "header_subtotal_bbox": _bbox_json(subtotal_header_bbox),
            "amount_cell_bbox": _bbox_json(amount_bbox),
            "grid_row_index": row_index,
            "grid_column_index": subtotal_col,
        })
    return evidence[0], evidence[1], evidence[2], {
        "physical_page": 55,
        "period_header": "2025年半年度",
        "parent_group_header": "归属于母公司所有者权益",
        "subtotal_header": "小计",
        "minority_group_header": "少数股东权益",
        "total_group_header": "所有者权益合计",
        "unit": "元",
        "parent_group_bbox": _bbox_json(parent_bbox),
        "subtotal_header_bbox": _bbox_json(subtotal_header_bbox),
        "minority_group_bbox": _bbox_json(minority_bbox),
        "total_group_bbox": _bbox_json(total_bbox),
    }


def _event_evidence(h126: Any, correction: Any) -> dict[str, object]:
    h126_p29 = _require_text(
        h126, 29, "2026年5月8日", "江西东津发电有限责任公司", "100%股权",
        "江投集团", "截至本报告期末", "工商变更登记手续已办理完毕",
    )
    # The date and control-duration text are vertically wrapped inside table
    # cells on p.109. Validate them in the native grid below, not by flattening
    # page-wide text order.
    h126_p109 = _require_text(h126, 109, "江西东津发电有限责任公司")
    h126_p49 = _require_text(
        h126, 49, "本期发生同一控制下企业合并", "上期被合并方实现的净利润",
    )
    correction_p1 = _require_text(
        correction, 1, "000899", "江西东津发电有限责任公司", "100%股权",
        "同一控制下企业合并", "2025年1-9月", "期初数据",
    )
    target_note = re.search(r"上期被合并方实现的净利润\s*为\s*[:：]?\s*(-?[\d,]+\.\d{2})\s*元", h126_p49)
    if target_note is None:
        raise GannengPresentedPriorError("H126 p.49 prior acquiree-profit note is not natively parseable")
    note_profit = Decimal(target_note.group(1).replace(",", ""))

    table, row_index, acquiree_row = _only_row(
        h126, 109,
        lambda row: len(row) == 9 and _compact(row[0]) == "江西东津发电有限责任公司",
        "Dongjin same-control merger row",
    )
    if (_compact(acquiree_row[1]) != "100.00%"
            or _compact(acquiree_row[3]) != "2026年06月30日"
            or "最终控制方控制" not in _compact(acquiree_row[2])
            or "超过一年" not in _compact(acquiree_row[2])):
        raise GannengPresentedPriorError("H126 p.109 does not bind the expected same-control merger scope")
    prior_profit = _parse_amount(acquiree_row[8], "H126 p.109 comparative acquiree net profit")
    if note_profit != prior_profit:
        raise GannengPresentedPriorError("H126 p.49 note and p.109 comparative acquiree net profit differ")

    event_cells = []
    for column_index, label in ((0, "acquiree"), (1, "ownership_percentage"),
                                (2, "same_control_basis"), (3, "merger_date"),
                                (8, "prior_acquiree_net_profit")):
        event_cells.append({
            "label": label,
            "physical_page": 109,
            "native_cell_text": acquiree_row[column_index],
            "cell_bbox": _bbox_json(_cell_bbox(table, row_index, column_index)),
            "grid_row_index": row_index,
            "grid_column_index": column_index,
        })

    return {
        "status": "PASS_SAME_ACQUISITION_EVENT_BOUND_ACROSS_H126_AND_OCT_NOTICE",
        "event_identity": {
            "acquiree": "江西东津发电有限责任公司",
            "ownership_percentage": "100%",
            "h126_board_approval_date": "2026-05-08",
            "h126_completion_date": "2026-06-30",
            "same_control": True,
            "h126_report_note_prior_acquiree_profit_yuan": _amount_text(note_profit),
            "h126_acquiree_table_prior_profit_yuan": _amount_text(prior_profit),
            "same_h125_acquiree_profit_crosscheck": True,
        },
        "h126_acquiree_table_native_cells": event_cells,
        "source_quotes": {
            "h126_physical_page_29": h126_p29,
            "h126_physical_page_49": h126_p49,
            "h126_physical_page_109": h126_p109,
            "oct_notice_physical_page_1": correction_p1,
        },
        "oct_notice_scope": {
            "source_report_period_end": SOURCE_REPORT_PERIOD_END.isoformat(),
            "restated_periods": ["2026-01-01 opening data", "2025-01-01 through 2025-09-30"],
            "same_event_as_h126_acquisition": True,
            "direct_h125_six_month_cell_or_no_change_statement": False,
            "does_not_prove_a_new_h125_six_month_effect": True,
        },
    }


def extract_presented_prior_evidence(
    pdf_bytes: bytes,
    correction_pdf_bytes: bytes,
    *,
    instrument_id: str = INSTRUMENT_ID,
    target_period_end: date = FACT_PERIOD_END,
) -> dict[str, object]:
    """Extract all five H125 values as they are presented in the reviewed H126 PDF.

    ``correction_pdf_bytes`` must be the reviewed 2026-10-24 same-control
    restatement notice. It binds the later notice to the same acquisition event
    but is not treated as a direct H125 six-month amount or no-change statement.
    Source-index receipts, first-seen times, correction-set closure, rights,
    admission and PIT selection remain the caller's responsibility.
    """
    if instrument_id != INSTRUMENT_ID or target_period_end != FACT_PERIOD_END:
        raise GannengPresentedPriorError("instrument or target period is outside the reviewed 000899 H125 sample")
    if not isinstance(pdf_bytes, bytes) or not isinstance(correction_pdf_bytes, bytes):
        raise GannengPresentedPriorError("PDF inputs must be immutable byte strings")
    report_sha = hashlib.sha256(pdf_bytes).hexdigest()
    correction_sha = hashlib.sha256(correction_pdf_bytes).hexdigest()
    if report_sha != SOURCE_REPORT_SHA256:
        raise GannengPresentedPriorError("H126 PDF SHA-256 is outside the reviewed native-text sample")
    if correction_sha != CORRECTION_SHA256:
        raise GannengPresentedPriorError("October correction PDF SHA-256 is outside the reviewed native-text sample")

    try:
        import pdfplumber
    except ImportError as exc:
        raise GannengPresentedPriorError("pdfplumber is required for bounded native text/table evidence") from exc

    from io import BytesIO

    with pdfplumber.open(BytesIO(pdf_bytes)) as h126, pdfplumber.open(BytesIO(correction_pdf_bytes)) as correction:
        if len(h126.pages) != SOURCE_REPORT_PAGE_COUNT or len(correction.pages) != CORRECTION_PAGE_COUNT:
            raise GannengPresentedPriorError("PDF physical page count differs from the bound source")
        _require_text(h126, 1, COMPANY, "2026年半年度报告全文")
        _require_text(h126, 47, "3、合并利润表", "单位：元", "2026年半年度", "2025年半年度")
        _require_text(h126, 51, "5、合并现金流量表", "单位：元", "2026年半年度", "2025年半年度")
        _require_text(h126, 55, "上年金额", "单位：元", "2025年半年度")

        # The summary has an explicit adjusted-prior column and is an independent
        # native table path for revenue, attributable profit, and operating cash flow.
        revenue_summary, revenue_summary_proof = _summary_cell(
            pdf=h126, predicate=lambda row: bool(row) and _compact(row[0]) == "营业收入（元）",
            field="revenue", row_label="营业收入（元）",
        )
        attributable_summary, attributable_summary_proof = _summary_cell(
            pdf=h126,
            predicate=lambda row: bool(row) and _compact(row[0]) == "归属于上市公司股东的净利润（元）",
            field="net_profit_attributable", row_label="归属于上市公司股东的净利润（元）",
        )
        cashflow_summary, cashflow_summary_proof = _summary_cell(
            pdf=h126,
            predicate=lambda row: bool(row) and _compact(row[0]) == "经营活动产生的现金流量净额（元）",
            field="operating_cashflow", row_label="经营活动产生的现金流量净额（元）",
        )

        revenue, revenue_proof = _statement_prior_cell(
            pdf=h126, page_number=48,
            predicate=lambda row: bool(row) and _compact(row[0]) == "其中：营业收入",
            field="revenue", row_label="其中：营业收入", statement="合并利润表",
            prior_column_index=2,
        )
        consolidated, consolidated_proof = _statement_prior_cell(
            pdf=h126, page_number=49,
            predicate=lambda row: bool(row) and "五、净利润" in _compact(row[0]),
            field="net_profit_consolidated", row_label="五、净利润", statement="合并利润表",
            prior_column_index=2,
        )
        attributable, attributable_proof = _statement_prior_cell(
            pdf=h126, page_number=49,
            predicate=lambda row: bool(row) and "归属于母公司股东的净利润" in _compact(row[0]),
            field="net_profit_attributable", row_label="归属于母公司股东的净利润",
            statement="合并利润表", prior_column_index=2,
        )
        minority, minority_proof = _statement_prior_cell(
            pdf=h126, page_number=49,
            predicate=lambda row: bool(row) and "少数股东损益" in _compact(row[0]),
            field="minority_profit", row_label="少数股东损益", statement="合并利润表",
            prior_column_index=2,
        )

        cashflow_table, cashflow_row_index, cashflow_row = _only_row(
            h126, 51,
            lambda row: bool(row) and _compact(row[0]) == "经营活动产生的现金流量净额",
            "operating cash flow",
        )
        cashflow_rows = _table_rows(cashflow_table)
        if (len(cashflow_row) != 3 or not cashflow_rows
                or not any(len(row) >= 3 and _compact(row[1]) == "2026年半年度"
                           and _compact(row[2]) == "2025年半年度" for row in cashflow_rows[:3])):
            raise GannengPresentedPriorError("cash-flow table period headers do not match the reviewed H126 grid")
        cashflow, cashflow_proof = _cell_proof(
            pdf=h126, page_number=51, table=cashflow_table, row_index=cashflow_row_index,
            row=cashflow_row, column_index=2, field="operating_cashflow",
            row_label="经营活动产生的现金流量净额", statement="合并现金流量表",
            column_header="2025年半年度",
        )
        inflow, inflow_proof = _statement_prior_cell(
            pdf=h126, page_number=51,
            predicate=lambda row: bool(row) and _compact(row[0]) == "经营活动现金流入小计",
            field="operating_cash_inflow", row_label="经营活动现金流入小计",
            statement="合并现金流量表", prior_column_index=2,
        )
        outflow, outflow_proof = _statement_prior_cell(
            pdf=h126, page_number=51,
            predicate=lambda row: bool(row) and _compact(row[0]) == "经营活动现金流出小计",
            field="operating_cash_outflow", row_label="经营活动现金流出小计",
            statement="合并现金流量表", prior_column_index=2,
        )

        equity_opening_proof, equity_movement_proof, equity_proof, equity_headers = _equity_table_cells(h126)
        equity_opening = Decimal(str(equity_opening_proof["value_yuan"]))
        equity_movement = Decimal(str(equity_movement_proof["value_yuan"]))
        equity = Decimal(str(equity_proof["value_yuan"]))

        if revenue != revenue_summary or attributable != attributable_summary or cashflow != cashflow_summary:
            raise GannengPresentedPriorError("H126 prior main-statement cells differ from its adjusted-prior summary")
        if attributable + minority != consolidated:
            raise GannengPresentedPriorError("H126 prior consolidated-profit identity did not balance")
        if inflow - outflow != cashflow:
            raise GannengPresentedPriorError("H126 prior operating-cash-flow bridge did not balance")
        if equity_opening + equity_movement != equity:
            raise GannengPresentedPriorError("H126 prior parent-equity opening/movement/closing identity did not balance")
        event = _event_evidence(h126, correction)

    values = {
        "revenue": {"value_yuan": _amount_text(revenue), "source_cell": revenue_proof,
                    "independent_paths": [{
                        "path": "H126 summary adjusted-prior revenue cell",
                        "value_yuan": _amount_text(revenue_summary),
                        "passed": True,
                        "source_cell": revenue_summary_proof,
                    }]},
        "net_profit_consolidated": {"value_yuan": _amount_text(consolidated),
                                    "source_cell": consolidated_proof,
                                    "independent_paths": [
                                        {"path": "attributable_plus_minority_profit_equals_consolidated",
                                         "attributable_profit_yuan": _amount_text(attributable),
                                         "minority_profit_yuan": _amount_text(minority),
                                         "result_yuan": _amount_text(attributable + minority),
                                         "passed": True,
                                         "source_cells": [attributable_proof, minority_proof]}]},
        "net_profit_attributable": {"value_yuan": _amount_text(attributable),
                                    "source_cell": attributable_proof,
                                    "independent_paths": [{
                                        "path": "H126 summary adjusted-prior attributable-profit cell",
                                        "value_yuan": _amount_text(attributable_summary),
                                        "passed": True,
                                        "source_cell": attributable_summary_proof,
                                    }]},
        "operating_cashflow": {"value_yuan": _amount_text(cashflow), "source_cell": cashflow_proof,
                               "independent_paths": [{
                                   "path": "H126 summary adjusted-prior operating-cashflow cell",
                                   "value_yuan": _amount_text(cashflow_summary),
                                   "passed": True,
                                   "source_cell": cashflow_summary_proof,
                               },
                                   {"path": "operating_cash_inflow_less_outflow_equals_net_cashflow",
                                    "inflow_yuan": _amount_text(inflow),
                                    "outflow_yuan": _amount_text(outflow),
                                    "result_yuan": _amount_text(inflow - outflow),
                                    "passed": True,
                                    "source_cells": [inflow_proof, outflow_proof]}]},
        "parent_equity": {"value_yuan": _amount_text(equity), "source_cell": equity_proof,
                           "independent_paths": [
                               {"path": "equity_opening_plus_movement_equals_closing",
                                "opening_yuan": _amount_text(equity_opening),
                                "movement_yuan": _amount_text(equity_movement),
                                "result_yuan": _amount_text(equity_opening + equity_movement),
                                "passed": True,
                                "source_cells": [equity_opening_proof, equity_movement_proof],
                                "header_geometry": equity_headers}]},
    }
    return {
        "schema": "aquant.cninfo.000899-h125-presented-prior-source-proof.v1",
        "status": "PASS_SOURCE_NATIVE_PRESENTED_PRIOR_CANDIDATE",
        "verification_actor": "machine:cninfo-000899-h125-presented-prior-v1",
        "instrument_id": INSTRUMENT_ID,
        "company": COMPANY,
        "fact_period_end": FACT_PERIOD_END.isoformat(),
        "source_report_period_end": SOURCE_REPORT_PERIOD_END.isoformat(),
        "column_role": "PRESENTED_PRIOR",
        "currency": "CNY",
        "unit": "元",
        "pit_eligible": False,
        "formal_admission": "NOT_DECIDED_BY_THIS_EXTRACTOR",
        "source_report": {
            "announcement_id": SOURCE_REPORT_ANNOUNCEMENT_ID,
            "title": SOURCE_REPORT_TITLE,
            "published_date": SOURCE_REPORT_PUBLISHED_DATE,
            "pdf_sha256": f"sha256:{SOURCE_REPORT_SHA256}",
            "physical_page_count": SOURCE_REPORT_PAGE_COUNT,
            "native_pdf_text_only": True,
        },
        "later_correction_source": {
            "announcement_id": CORRECTION_ANNOUNCEMENT_ID,
            "title": CORRECTION_TITLE,
            "published_date": CORRECTION_PUBLISHED_DATE,
            "pdf_sha256": f"sha256:{CORRECTION_SHA256}",
            "physical_page_count": CORRECTION_PAGE_COUNT,
            "native_pdf_text_only": True,
        },
        "values": values,
        "event_binding": event,
        "limitations": [
            "The October 24 notice reports 2026 opening data and 2025 9M comparative data for the same acquisition event; it is not a direct H125 six-month cell or an explicit H125 no-change statement.",
            "This extractor does not resolve full-index correction closure, source receipts, data rights, first-seen timestamps, PIT selection, version writing, or formal admission.",
            "The output is machine source evidence only and is not a HUMAN_VISUAL review or assessor decision.",
        ],
    }


__all__ = [
    "GannengPresentedPriorError",
    "extract_presented_prior_evidence",
]
