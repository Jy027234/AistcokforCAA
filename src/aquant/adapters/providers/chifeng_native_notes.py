"""Fail-closed native-note candidate reader for one registered CNINFO PDF.

The report's first-page title glyphs are damaged in the source PDF text layer.
Identity is therefore established from the same PDF's exact legal-name row on
page 8, its explicitly labelled A-share row in the page 9 stock table, and the
repeated legal-entity / annual-period / RMB-unit headings on the consolidated
notes pages.  No OCR, cover-title substitution, or amount lookup by expected
value is used.  This module only returns non-PIT candidates and machine proof;
it does not create or promote financial facts.
"""

from __future__ import annotations

import hashlib
import io
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Iterable, Sequence

from .cninfo_financial_pdf import CninfoS2CandidateFacts, CninfoFinancialPdfError, PdfCandidateFact
from .s2_machine_verification import FieldVerification, IndependentCheck, MachineVerification, MachineVerificationError


class ChifengNativeNoteError(CninfoFinancialPdfError):
    """The one reviewed original or one of its native note layouts did not match."""


_PDF_SHA256 = "d53382f3f9e7989347b91524d63140dca89cd56d8ade23a07d3e9a33dc1a043e"
_PDF_PAGE_COUNT = 219
_INSTRUMENT_ID = "600988"
_COMPANY = "赤峰吉隆黄金矿业股份有限公司"
_PERIOD_END = date(2025, 12, 31)
_ANNOUNCEMENT_ID = "1225021883"
_DOCUMENT_URL = "https://static.cninfo.com.cn/finalpage/2026-03-21/1225021883.PDF"
_FIELDS = (
    "revenue",
    "net_profit_attributable",
    "net_profit_consolidated",
    "operating_cashflow",
)
_FIELD_LABELS = {
    "revenue": "合计",
    "net_profit_attributable": "加：本年归属于母公司股东的净利润",
    "net_profit_consolidated": "净利润",
    "operating_cashflow": "经营活动产生的现金流量净额",
}
_NOTE_PAGE = {
    "net_profit_attributable": 165,
    "revenue": 166,
    "net_profit_consolidated": 178,
    "operating_cashflow": 178,
}
_NOTE_HEADINGS = {
    165: "41、 未分配利润",
    166: "42、 营业收入及成本",
    175: "56、 所得税费用",
    178: "58、 现金流量表补充资料",
    183: "61、 每股收益",
}
_IDENTITY_NOTE_PAGES = tuple(_NOTE_HEADINGS)
_PROOF_SCHEMA = "cninfo-native-full-report-note-crosscheck-v1"
_EXTRACTOR_VERSION = "chifeng-native-notes-v1"
_CANDIDATE_VERSION = "native_consolidated_notes_v1"
_NATIVE_PATH = "native_full_report_note_crosscheck"
_AMOUNT = re.compile(r"(?:0|[1-9]\d{0,2}(?:,\d{3})*)(?:\.\d{2})\Z")
_NEGATIVE_PAREN = re.compile(r"\(\s*((?:0|[1-9]\d{0,2}(?:,\d{3})*)(?:\.\d{2}))\s*\)\Z")
_NEGATIVE_SIGN = re.compile(r"-(?:0|[1-9]\d{0,2}(?:,\d{3})*)(?:\.\d{2})\Z")


def is_supported_pdf(pdf_bytes: bytes) -> bool:
    """Return true only for the exact previously registered 2025 CNINFO original."""

    return (isinstance(pdf_bytes, bytes) and pdf_bytes.startswith(b"%PDF-")
            and hashlib.sha256(pdf_bytes).hexdigest() == _PDF_SHA256)


def _fail(message: str) -> None:
    raise ChifengNativeNoteError(message)


def _compact(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "")


def _bbox(value: Sequence[float] | None) -> list[float] | None:
    return None if value is None else [float(part) for part in value]


def _char_record(char: dict[str, object]) -> dict[str, object]:
    return {
        "text": str(char.get("text", "")),
        "bbox": [float(char[key]) for key in ("x0", "top", "x1", "bottom")],
    }


def _line_record(page_number: int, line: dict[str, object]) -> dict[str, object]:
    chars = line.get("chars") or []
    return {
        "page": page_number,
        "text": str(line.get("text", "")),
        "bbox": [float(line[key]) for key in ("x0", "top", "x1", "bottom")],
        "native_glyphs": [_char_record(char) for char in chars],
    }


def _unique_line(lines: list[dict[str, object]], *, page_number: int,
                 predicate, description: str) -> dict[str, object]:
    matches = [line for line in lines if predicate(str(line.get("text", "")))]
    if len(matches) != 1:
        _fail(f"page {page_number}: {description} is missing or ambiguous")
    return _line_record(page_number, matches[0])


def _validate_request(*, instrument_id: str, company: str, period_end: date,
                      announcement_id: str, document_url: str,
                      required_fields: Sequence[str]) -> tuple[str, ...]:
    if (instrument_id != _INSTRUMENT_ID or company != _COMPANY
            or period_end != _PERIOD_END or announcement_id != _ANNOUNCEMENT_ID
            or document_url != _DOCUMENT_URL):
        _fail("requested issuer, instrument, period, announcement, or URL mismatches the registered PDF")
    if isinstance(required_fields, (str, bytes)):
        _fail("required_fields must be a non-empty unique field sequence")
    try:
        fields = tuple(required_fields)
    except TypeError as exc:
        raise ChifengNativeNoteError("required_fields must be a non-empty unique field sequence") from exc
    if not fields or len(fields) != len(set(fields)) or not set(fields) <= set(_FIELDS):
        _fail("required_fields must be a non-empty subset of the four reviewed native-note fields")
    return tuple(field for field in _FIELDS if field in fields)


def _native_lines(page, page_number: int) -> list[dict[str, object]]:
    lines = page.extract_text_lines(layout=False, return_chars=True) or []
    if not lines:
        _fail(f"page {page_number}: native text lines are unavailable")
    return lines


def _cell_glyphs(page, bbox: Sequence[float]) -> list[dict[str, object]]:
    x0, top, x1, bottom = (float(value) for value in bbox)
    selected = [char for char in page.chars
                if x0 - 0.35 <= (float(char["x0"]) + float(char["x1"])) / 2 <= x1 + 0.35
                and top - 0.35 <= (float(char["top"]) + float(char["bottom"])) / 2 <= bottom + 0.35]
    # Keep the PDF's native character-stream order. Some compact annual-column
    # headers are slightly staggered vertically (the 年 glyph sits a fraction
    # above the year numerals), so geometry-sorting that row would rewrite
    # ``2025年`` as ``年2025`` despite an exact native glyph stream.
    return [_char_record(char) for char in selected]


def _cell_record(page, text: str | None, bbox_value: Sequence[float] | None,
                 *, page_number: int, table_index: int, row_index: int,
                 cell_index: int) -> dict[str, object]:
    raw = text or ""
    if bbox_value is None:
        if raw.strip():
            _fail(f"page {page_number}: non-empty table cell has no native geometry")
        return {"index": cell_index, "text": raw, "bbox": None, "native_glyphs": []}
    glyphs = _cell_glyphs(page, bbox_value)
    glyph_text = "".join(str(char["text"]) for char in glyphs)
    if _compact(glyph_text) != _compact(raw):
        _fail(f"page {page_number}: table cell text differs from glyphs at row {row_index}, cell {cell_index}")
    return {
        "index": cell_index,
        "text": raw,
        "bbox": _bbox(bbox_value),
        "native_glyphs": glyphs,
    }


def _row_record(page, table, rows: list[list[str | None]], *, page_number: int,
                table_index: int, row_index: int) -> dict[str, object]:
    row = rows[row_index]
    native_cells = table.rows[row_index].cells
    if len(native_cells) != len(row):
        _fail(f"page {page_number}: table cell geometry count mismatch")
    cells = [
        _cell_record(page, text, cell_bbox, page_number=page_number,
                     table_index=table_index, row_index=row_index, cell_index=index)
        for index, (text, cell_bbox) in enumerate(zip(row, native_cells))
    ]
    boxes = [cell["bbox"] for cell in cells if cell["bbox"] is not None]
    row_bbox = None if not boxes else [
        min(box[0] for box in boxes), min(box[1] for box in boxes),
        max(box[2] for box in boxes), max(box[3] for box in boxes),
    ]
    return {
        "page": page_number,
        "table_index": table_index,
        "table_bbox": _bbox(table.bbox),
        "row_index": row_index,
        "row_bbox": row_bbox,
        "raw_cells": row,
        "cells": cells,
    }


def _tables(page) -> list[tuple[int, object, list[list[str | None]]]]:
    found = []
    for index, table in enumerate(page.find_tables()):
        rows = table.extract()
        found.append((index, table, rows))
    return found


def _one_table(tables, predicate, *, page_number: int, description: str):
    matches = [item for item in tables if predicate(item[2])]
    if len(matches) != 1:
        _fail(f"page {page_number}: {description} table is missing or ambiguous")
    return matches[0]


def _header_columns(row: Sequence[str | None], *, page_number: int) -> tuple[int, int]:
    current = [i for i, value in enumerate(row) if _compact(value) == "2025年"]
    prior = [i for i, value in enumerate(row) if _compact(value) == "2024年"]
    if len(current) != 1 or len(prior) != 1:
        _fail(f"page {page_number}: 2025/2024 native column headers are missing or ambiguous")
    return current[0], prior[0]


def _money(value: str | None, *, page_number: int, description: str) -> Decimal:
    raw = (value or "").strip()
    if _AMOUNT.fullmatch(raw):
        numeric = raw.replace(",", "")
    elif _NEGATIVE_SIGN.fullmatch(raw):
        numeric = raw.replace(",", "")
    else:
        negative = _NEGATIVE_PAREN.fullmatch(raw)
        if negative is None:
            _fail(f"page {page_number}: {description} is not an explicit yuan amount")
        numeric = "-" + negative.group(1).replace(",", "")
    try:
        return Decimal(numeric)
    except InvalidOperation as exc:
        raise ChifengNativeNoteError(f"page {page_number}: invalid amount in {description}") from exc


def _require_note_header(lines_by_page: dict[int, list[dict[str, object]]],
                         page_number: int) -> dict[str, object]:
    lines = lines_by_page[page_number]
    issuer = _unique_line(
        lines, page_number=page_number,
        predicate=lambda text: _compact(text) == _COMPANY,
        description="exact legal-entity header",
    )
    annual_unit = _unique_line(
        lines, page_number=page_number,
        predicate=lambda text: _compact(text) == "2025年度人民币元",
        description="2025 annual period and RMB yuan header",
    )
    scope = _unique_line(
        lines, page_number=page_number,
        predicate=lambda text: _compact(text) == "五、合并财务报表主要项目注释（续）",
        description="consolidated notes scope header",
    )
    note = _unique_line(
        lines, page_number=page_number,
        predicate=lambda text, expected=_NOTE_HEADINGS[page_number]: _compact(text) == _compact(expected),
        description="bounded note number and title",
    )
    return {"page": page_number, "issuer": issuer, "annual_period_and_unit": annual_unit,
            "consolidated_scope": scope, "note_heading": note}


def _identity_proof(pdf, lines_by_page: dict[int, list[dict[str, object]]],
                    page_tables: dict[int, list[tuple[int, object, list[list[str | None]]]]]) -> dict[str, object]:
    company_line = _unique_line(
        lines_by_page[8], page_number=8,
        predicate=lambda text: _compact(text) == _compact("公司的中文名称 赤峰吉隆黄金矿业股份有限公司"),
        description="page 8 legal Chinese company-name row",
    )
    stock_heading = _unique_line(
        lines_by_page[9], page_number=9,
        predicate=lambda text: _compact(text) == "五、公司股票简况",
        description="page 9 issuer stock-brief heading",
    )
    a_line = _unique_line(
        lines_by_page[9], page_number=9,
        predicate=lambda text: re.match(r"^\s*A股\s+", text) is not None,
        description="page 9 labelled A-share stock row",
    )
    h_line = _unique_line(
        lines_by_page[9], page_number=9,
        predicate=lambda text: re.match(r"^\s*H股\s+", text) is not None,
        description="page 9 distinct H-share stock row",
    )
    a_text = str(a_line["text"])
    h_text = str(h_line["text"])
    a_codes = re.findall(r"(?<!\d)\d{6}(?!\d)", a_text)
    h_codes = re.findall(r"(?<!\d)\d{5}(?!\d)", h_text)
    if a_codes != [_INSTRUMENT_ID] or h_codes != ["06693"]:
        _fail("page 9: A-share code or distinct H-share code does not match the registered instrument")
    a_match = re.fullmatch(r"\s*A股\s+(\S+)\s+(\S+)\s+600988\s+(.+)\s*", a_text)
    h_match = re.fullmatch(r"\s*H股\s+(\S+)\s+(\S+)\s+06693\s+(.+)\s*", h_text)
    if not a_match or not h_match or a_match.group(2) != h_match.group(2):
        _fail("page 9: A/H-share exchange and short-name row structure mismatch")

    table_index, table, rows = _one_table(
        page_tables[9],
        lambda rows: len(rows) >= 3 and len(rows[0]) >= 3
        and "股票上市" in _compact(rows[0][0])
        and _compact(rows[0][1]) == "股票简称"
        and _compact(rows[0][2]) == "股票代码"
        and _compact(rows[1][2]) == _INSTRUMENT_ID
        and _compact(rows[2][2]) == "06693",
        page_number=9, description="explicit stock-listing",
    )
    if (_compact(rows[1][0]) != _compact(a_match.group(1))
            or _compact(rows[1][1]) != _compact(a_match.group(2))
            or _compact(rows[2][0]) != _compact(h_match.group(1))
            or _compact(rows[2][1]) != _compact(h_match.group(2))):
        _fail("page 9: stock-table A/H rows disagree with their native A股/H股 labels")
    a_row = _row_record(pdf.pages[8], table, rows, page_number=9,
                        table_index=table_index, row_index=1)
    h_row = _row_record(pdf.pages[8], table, rows, page_number=9,
                        table_index=table_index, row_index=2)
    for line, row, tag in ((a_line, a_row, "A股"), (h_line, h_row, "H股")):
        line_bbox = line["bbox"]
        row_bbox = row["row_bbox"]
        if (row_bbox is None or line_bbox[1] < row_bbox[1] - 0.5
                or line_bbox[3] > row_bbox[3] + 0.5):
            _fail(f"page 9: {tag} label does not align with its exchange/name/code table row")
    stock_headers = _row_record(pdf.pages[8], table, rows, page_number=9,
                                table_index=table_index, row_index=0)
    repeated_notes = [_require_note_header(lines_by_page, page) for page in _IDENTITY_NOTE_PAGES]
    return {
        "identity_method": "page8_legal_name_plus_page9_labelled_a_share_row_plus_repeated_native_consolidated_note_headers_v1",
        "legacy_cover_check": "not_claimed; original cover title glyphs are not used as identity evidence",
        "page8_legal_name": company_line,
        "page9_stock_brief_heading": stock_heading,
        "page9_stock_table_headers": stock_headers,
        "page9_a_share_label": a_line,
        "page9_a_share_table_row": a_row,
        "page9_h_share_label": h_line,
        "page9_h_share_table_row": h_row,
        "repeated_note_identity_headers": repeated_notes,
        "a_share_instrument_id": _INSTRUMENT_ID,
        "company": _COMPANY,
        "period_end": _PERIOD_END.isoformat(),
        "amount_unit": "元",
        "currency": "CNY",
        "scope": "合并财务报表附注",
    }


def _find_note_table(tables, *, page_number: int, heading: list[str | None],
                     labels: set[str]):
    def matches(rows):
        if not rows or not rows[0]:
            return False
        first = {_compact(value) for value in rows[0] if value}
        flattened = {_compact(value) for row in rows for value in row if value}
        return {"2025年", "2024年"} <= first and labels <= flattened
    return _one_table(tables, matches, page_number=page_number,
                      description="dated, note-bounded")


def _make_candidate(field: str, *, row: list[str | None], row_index: int,
                    page_number: int, current_index: int, prior_index: int,
                    table_index: int, table, value: Decimal) -> PdfCandidateFact:
    if current_index >= len(row) or prior_index >= len(row):
        _fail(f"page {page_number}: expected current/prior cells are outside the source row")
    prior = _money(row[prior_index], page_number=page_number,
                   description=f"{field} explicit 2024 comparison cell")
    if _money(row[current_index], page_number=page_number,
              description=f"{field} explicit current cell") != value:
        _fail(f"page {page_number}: candidate value differs from its native current cell")
    return PdfCandidateFact(
        field=field, value_yuan=value, period_end=_PERIOD_END,
        report_period_text="2025年度", statement="合并财务报表附注",
        pdf_page=page_number, column_header="2025年", amount_unit="元",
        currency="CNY", source_row="\t".join(cell or "" for cell in row),
        source_cells=tuple(row), source_current_cell_index=current_index,
        source_prior_cell_index=prior_index, pdf_sha256="sha256:" + _PDF_SHA256,
        display_row_label=_FIELD_LABELS[field],
    )


def _with_headers(page, table, rows, *, page_number: int, table_index: int,
                  indices: Iterable[int]) -> list[dict[str, object]]:
    return [_row_record(page, table, rows, page_number=page_number,
                        table_index=table_index, row_index=index)
            for index in indices]


def _fact_proof(page, table, rows, *, page_number: int, table_index: int,
                row_index: int, current_index: int, prior_index: int,
                header_rows: Iterable[int], formula: dict[str, object],
                candidate: PdfCandidateFact) -> dict[str, object]:
    return {
        "primary_current_fact": {
            "field": candidate.field,
            "value_yuan": str(candidate.value_yuan),
            "period_end": candidate.period_end.isoformat(),
            "report_period_text": candidate.report_period_text,
            "statement_scope": candidate.statement,
            "page": page_number,
            "column_header": candidate.column_header,
            "amount_unit": candidate.amount_unit,
            "currency": candidate.currency,
            "row": _row_record(page, table, rows, page_number=page_number,
                               table_index=table_index, row_index=row_index),
            "current_cell_index": current_index,
            "prior_cell_index": prior_index,
            "headers": _with_headers(page, table, rows, page_number=page_number,
                                     table_index=table_index, indices=header_rows),
        },
        "independent_native_arithmetic": formula,
    }


def _extract_report(pdf_bytes: bytes, *, instrument_id: str, company: str,
                    period_end: date, announcement_id: str, document_url: str,
                    required_fields: Sequence[str]):
    fields = _validate_request(
        instrument_id=instrument_id, company=company, period_end=period_end,
        announcement_id=announcement_id, document_url=document_url,
        required_fields=required_fields,
    )
    if not is_supported_pdf(pdf_bytes):
        _fail("PDF SHA-256 is not the registered CNINFO 2025 original")
    try:
        import pdfplumber
    except ImportError as exc:
        raise ChifengNativeNoteError("pdfplumber is required to read native source cells") from exc
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if len(pdf.pages) != _PDF_PAGE_COUNT:
                _fail("registered PDF page count mismatch")
            line_pages = (8, 9, *_IDENTITY_NOTE_PAGES)
            lines_by_page = {number: _native_lines(pdf.pages[number - 1], number)
                             for number in line_pages}
            pages_needed = set(_IDENTITY_NOTE_PAGES)
            tables_by_page = {
                number: _tables(pdf.pages[number - 1])
                for number in pages_needed
            }
            tables_by_page[9] = _tables(pdf.pages[8])
            identity = _identity_proof(pdf, lines_by_page, tables_by_page)
            candidates: dict[str, PdfCandidateFact] = {}
            proof: dict[str, dict[str, object]] = {}

            if "revenue" in fields:
                page_no = 166
                lines = lines_by_page[page_no]
                if not any(_compact(line["text"]) == _compact(_NOTE_HEADINGS[page_no]) for line in lines):
                    _fail("page 166: revenue note 42 heading is absent")
                main_i, main_table, main_rows = _one_table(
                    tables_by_page[page_no],
                    lambda rows: len(rows) >= 7 and len(rows[0]) >= 8
                    and _compact(rows[0][2]) == "2025年"
                    and _compact(rows[0][7]) == "2024年"
                    and _compact(rows[2][2]) == "收入"
                    and _compact(rows[2][7]) == "收入"
                    and _compact(rows[4][0]) == "主营业务"
                    and _compact(rows[5][0]) == "其他业务"
                    and _compact(rows[6][0]) == "合计",
                    page_number=page_no, description="2025/2024 revenue and cost",
                )
                current, prior = 2, 7
                main_value = _money(main_rows[4][current], page_number=page_no,
                                    description="main-business current revenue")
                other_value = _money(main_rows[5][current], page_number=page_no,
                                     description="other-business current revenue")
                total_value = _money(main_rows[6][current], page_number=page_no,
                                     description="revenue note total")
                if main_value + other_value != total_value:
                    _fail("page 166: main plus other business does not equal the revenue note total")
                candidate = _make_candidate("revenue", row=main_rows[6], row_index=6,
                                            page_number=page_no, current_index=current,
                                            prior_index=prior, table_index=main_i,
                                            table=main_table, value=total_value)

                geo_i, geo_table, geo_rows = _one_table(
                    tables_by_page[page_no],
                    lambda rows: len(rows) >= 7 and len(rows[0]) >= 9
                    and _compact(rows[0][0]) == "报告分部"
                    and _compact(rows[0][8]) == "合计"
                    and _compact(rows[3][0]) == "中国境内"
                    and _compact(rows[4][0]) == "老挝"
                    and _compact(rows[5][0]) == "加纳"
                    and _compact(rows[6][0]) == "合计",
                    page_number=page_no, description="separate geography revenue breakdown",
                )
                geo_values = [
                    _money(geo_rows[index][8], page_number=page_no,
                           description=f"geography revenue {geo_rows[index][0]}")
                    for index in (3, 4, 5)
                ]
                geography_total = sum(geo_values, Decimal(0))
                geo_proof = {
                    "table_bbox": _bbox(geo_table.bbox),
                    "header": _row_record(pdf.pages[page_no - 1], geo_table, geo_rows,
                                          page_number=page_no, table_index=geo_i, row_index=0),
                    "component_rows": [
                        _row_record(pdf.pages[page_no - 1], geo_table, geo_rows,
                                    page_number=page_no, table_index=geo_i, row_index=index)
                        for index in (3, 4, 5)
                    ],
                    "reported_total_row": _row_record(
                        pdf.pages[page_no - 1], geo_table, geo_rows,
                        page_number=page_no, table_index=geo_i, row_index=6),
                    "component_total_yuan": str(geography_total),
                    "reported_geography_total_yuan": str(_money(
                        geo_rows[6][8], page_number=page_no,
                        description="geography breakdown total")),
                    "primary_revenue_total_yuan": str(total_value),
                    "holds": geography_total == total_value
                    and _money(geo_rows[6][8], page_number=page_no,
                               description="geography breakdown total") == total_value,
                }
                formula = {
                    "decomposition": {
                        "main_business_current_yuan": str(main_value),
                        "other_business_current_yuan": str(other_value),
                        "reported_total_yuan": str(total_value),
                        "holds": main_value + other_value == total_value,
                        "main_business_row": _row_record(
                            pdf.pages[page_no - 1], main_table, main_rows,
                            page_number=page_no, table_index=main_i, row_index=4),
                        "other_business_row": _row_record(
                            pdf.pages[page_no - 1], main_table, main_rows,
                            page_number=page_no, table_index=main_i, row_index=5),
                    },
                    "geography_breakdown": geo_proof,
                }
                candidates["revenue"] = candidate
                proof["revenue"] = _fact_proof(
                    pdf.pages[page_no - 1], main_table, main_rows,
                    page_number=page_no, table_index=main_i, row_index=6,
                    current_index=current, prior_index=prior, header_rows=(0, 2),
                    formula=formula, candidate=candidate,
                )

            if "net_profit_attributable" in fields:
                page_no = 165
                tables = tables_by_page[page_no]
                table_i, table, rows = _one_table(
                    tables,
                    lambda rows: len(rows) >= 6 and len(rows[0]) == 3
                    and _compact(rows[0][1]) == "2025年"
                    and _compact(rows[0][2]) == "2024年"
                    and _compact(rows[1][0]) == "年初未分配利润"
                    and _compact(rows[2][0]) == _compact(_FIELD_LABELS["net_profit_attributable"])
                    and _compact(rows[3][0]) == "减：提取法定盈余公积"
                    and _compact(rows[4][0]) == "减：支付普通股现金股利"
                    and _compact(rows[5][0]) == "年末未分配利润",
                    page_number=page_no, description="note 41 retained earnings",
                )
                current, prior = _header_columns(rows[0], page_number=page_no)
                opening = _money(rows[1][current], page_number=page_no,
                                 description="opening retained earnings")
                value = _money(rows[2][current], page_number=page_no,
                               description="current attributable profit")
                if rows[3][current] is None or _compact(rows[3][current]) != "-":
                    _fail("page 165: statutory reserve current dash is not preserved as a dash")
                dividend = _money(rows[4][current], page_number=page_no,
                                  description="current cash dividends")
                closing = _money(rows[5][current], page_number=page_no,
                                 description="closing retained earnings")
                rollforward = opening + value - dividend
                if rollforward != closing:
                    _fail("page 165: retained-earnings roll-forward does not reconcile")
                candidate = _make_candidate("net_profit_attributable", row=rows[2],
                                            row_index=2, page_number=page_no,
                                            current_index=current, prior_index=prior,
                                            table_index=table_i, table=table, value=value)
                eps_no = 183
                eps_i, eps_table, eps_rows = _one_table(
                    tables_by_page[eps_no],
                    lambda rows: len(rows) >= 4 and len(rows[0]) == 3
                    and _compact(rows[0][1]) == "2025年"
                    and _compact(rows[0][2]) == "2024年"
                    and _compact(rows[2][0]) == "归属于本公司普通股股东的当期净利润"
                    and _compact(rows[3][0]) == "持续经营",
                    page_number=eps_no, description="EPS direct profit numerator",
                )
                eps_current, eps_prior = _header_columns(eps_rows[0], page_number=eps_no)
                eps_value = _money(eps_rows[3][eps_current], page_number=eps_no,
                                   description="direct EPS numerator, not EPS ratio")
                eps_proof = _row_record(pdf.pages[eps_no - 1], eps_table, eps_rows,
                                        page_number=eps_no, table_index=eps_i, row_index=3)
                formula = {
                    "retained_earnings_rollforward": {
                        "opening_retained_earnings_yuan": str(opening),
                        "current_attributable_profit_yuan": str(value),
                        "current_statutory_reserve_source_cell": rows[3][current],
                        "current_dividends_yuan": str(dividend),
                        "closing_retained_earnings_yuan": str(closing),
                        "recomputed_closing_yuan": str(rollforward),
                        "holds": rollforward == closing,
                        "opening_row": _row_record(pdf.pages[page_no - 1], table, rows,
                                                   page_number=page_no, table_index=table_i, row_index=1),
                        "statutory_reserve_row": _row_record(pdf.pages[page_no - 1], table, rows,
                                                             page_number=page_no, table_index=table_i, row_index=3),
                        "dividends_row": _row_record(pdf.pages[page_no - 1], table, rows,
                                                     page_number=page_no, table_index=table_i, row_index=4),
                        "closing_row": _row_record(pdf.pages[page_no - 1], table, rows,
                                                   page_number=page_no, table_index=table_i, row_index=5),
                    },
                    "direct_eps_numerator": {
                        "row": eps_proof,
                        "value_yuan": str(eps_value),
                        "not_ratio": True,
                        "holds": eps_value == value,
                    },
                }
                candidates["net_profit_attributable"] = candidate
                proof["net_profit_attributable"] = _fact_proof(
                    pdf.pages[page_no - 1], table, rows,
                    page_number=page_no, table_index=table_i, row_index=2,
                    current_index=current, prior_index=prior, header_rows=(0,),
                    formula=formula, candidate=candidate,
                )

            if "net_profit_consolidated" in fields or "operating_cashflow" in fields:
                page_no = 178
                _, main_table, main_rows = _one_table(
                    tables_by_page[page_no],
                    lambda rows: len(rows) >= 20 and len(rows[0]) == 3
                    and _compact(rows[0][1]) == "2025年"
                    and _compact(rows[0][2]) == "2024年"
                    and _compact(rows[2][0]) == "净利润"
                    and _compact(rows[19][0]) == _FIELD_LABELS["operating_cashflow"],
                    page_number=page_no, description="note 58 dated cashflow reconciliation",
                )
                current, prior = _header_columns(main_rows[0], page_number=page_no)
                np_value = _money(main_rows[2][current], page_number=page_no,
                                  description="consolidated current net profit")
                cfo_value = _money(main_rows[19][current], page_number=page_no,
                                   description="current operating cashflow")
                if "net_profit_consolidated" in fields:
                    tax_no = 175
                    tax_i, tax_table, tax_rows = _one_table(
                        tables_by_page[tax_no],
                        lambda rows: len(rows) >= 13 and len(rows[0]) == 3
                        and _compact(rows[0][1]) == "2025年"
                        and _compact(rows[0][2]) == "2024年"
                        and _compact(rows[1][0]) == "利润总额"
                        and _compact(rows[12][0]) == "按本集团实际税率计算的所得税费用",
                        page_number=tax_no, description="note 56 income-tax reconciliation",
                    )
                    tax_current, tax_prior = _header_columns(tax_rows[0], page_number=tax_no)
                    profit_before_tax = _money(tax_rows[1][tax_current], page_number=tax_no,
                                               description="current profit before tax")
                    tax_expense = _money(tax_rows[12][tax_current], page_number=tax_no,
                                         description="current income tax expense")
                    tax_result = profit_before_tax - tax_expense
                    candidate = _make_candidate(
                        "net_profit_consolidated", row=main_rows[2], row_index=2,
                        page_number=page_no, current_index=current, prior_index=prior,
                        table_index=0, table=main_table, value=np_value,
                    )
                    formula = {
                        "profit_before_tax_less_income_tax": {
                            "profit_before_tax_yuan": str(profit_before_tax),
                            "income_tax_expense_yuan": str(tax_expense),
                            "recomputed_net_profit_yuan": str(tax_result),
                            "holds": tax_result == np_value,
                            "profit_before_tax_row": _row_record(
                                pdf.pages[tax_no - 1], tax_table, tax_rows,
                                page_number=tax_no, table_index=tax_i, row_index=1),
                            "tax_expense_row": _row_record(
                                pdf.pages[tax_no - 1], tax_table, tax_rows,
                                page_number=tax_no, table_index=tax_i, row_index=12),
                            "primary_net_profit_row": _row_record(
                                pdf.pages[page_no - 1], main_table, main_rows,
                                page_number=page_no, table_index=0, row_index=2),
                        },
                    }
                    candidates["net_profit_consolidated"] = candidate
                    proof["net_profit_consolidated"] = _fact_proof(
                        pdf.pages[page_no - 1], main_table, main_rows,
                        page_number=page_no, table_index=0, row_index=2,
                        current_index=current, prior_index=prior, header_rows=(0,),
                        formula=formula, candidate=candidate,
                    )
                if "operating_cashflow" in fields:
                    adjustment_values = [
                        _money(main_rows[index][current], page_number=page_no,
                               description=f"current cashflow reconciliation component row {index}")
                        for index in range(3, 19)
                    ]
                    recomputed_cfo = np_value + sum(adjustment_values, Decimal(0))
                    candidate = _make_candidate(
                        "operating_cashflow", row=main_rows[19], row_index=19,
                        page_number=page_no, current_index=current, prior_index=prior,
                        table_index=0, table=main_table, value=cfo_value,
                    )
                    component_rows = [
                        _row_record(pdf.pages[page_no - 1], main_table, main_rows,
                                    page_number=page_no, table_index=0, row_index=index)
                        for index in range(2, 19)
                    ]
                    formula = {
                        "cashflow_reconciliation": {
                            "net_profit_yuan": str(np_value),
                            "adjustment_rows": component_rows[1:],
                            "adjustment_total_yuan": str(sum(adjustment_values, Decimal(0))),
                            "recomputed_operating_cashflow_yuan": str(recomputed_cfo),
                            "reported_operating_cashflow_yuan": str(cfo_value),
                            "holds": recomputed_cfo == cfo_value,
                            "net_profit_row": component_rows[0],
                            "operating_cashflow_row": _row_record(
                                pdf.pages[page_no - 1], main_table, main_rows,
                                page_number=page_no, table_index=0, row_index=19),
                        },
                    }
                    candidates["operating_cashflow"] = candidate
                    proof["operating_cashflow"] = _fact_proof(
                        pdf.pages[page_no - 1], main_table, main_rows,
                        page_number=page_no, table_index=0, row_index=19,
                        current_index=current, prior_index=prior, header_rows=(0,),
                        formula=formula, candidate=candidate,
                    )
    except ChifengNativeNoteError:
        raise
    except Exception as exc:  # fail closed on PDF parser/layout errors
        raise ChifengNativeNoteError(f"native note parsing failed: {type(exc).__name__}") from exc

    candidate_bundle = CninfoS2CandidateFacts(
        instrument_id=_INSTRUMENT_ID, period_end=_PERIOD_END,
        version_label=_CANDIDATE_VERSION, document_url=_DOCUMENT_URL,
        pdf_sha256="sha256:" + _PDF_SHA256,
        candidates=tuple(candidates[field] for field in _FIELDS if field in candidates),
    )
    proof_bundle = {
        "proof_schema": _PROOF_SCHEMA,
        "extractor_version": _EXTRACTOR_VERSION,
        "candidate_version_label": _CANDIDATE_VERSION,
        "source_binding": {
            "source_id": "cninfo",
            "announcement_id": _ANNOUNCEMENT_ID,
            "document_url": _DOCUMENT_URL,
            "pdf_sha256": "sha256:" + _PDF_SHA256,
            "page_count": _PDF_PAGE_COUNT,
        },
        "identity": identity,
        "report_period": {
            "period_end": _PERIOD_END.isoformat(),
            "native_period_text": "2025年度",
            "comparison_period_text": "2024年",
        },
        "basis": {
            "scope": "合并财务报表附注",
            "amount_unit": "元",
            "currency": "CNY",
            "main_statement_scan": "not_scanned; primary cells are from bounded consolidated-note tables",
            "accounting_framework_claimed": False,
        },
        "field_evidence": proof,
        "point_in_time": {
            "pit_eligible": False,
            "notice": "This parser asserts document/version identity only; archive receipt and publication-date treatment remain in the registered CNINFO pipeline.",
        },
    }
    return candidate_bundle, proof_bundle


def extract_native_note_facts(
    pdf_bytes: bytes, *, instrument_id: str, company: str, period_end: date,
    announcement_id: str, document_url: str, required_fields: Sequence[str],
) -> CninfoS2CandidateFacts:
    """Extract requested current FY2025 yuan facts from the one registered original."""

    candidate, _ = _extract_report(
        pdf_bytes, instrument_id=instrument_id, company=company,
        period_end=period_end, announcement_id=announcement_id,
        document_url=document_url, required_fields=required_fields,
    )
    return candidate


def verify_native_note_evidence(
    pdf_bytes: bytes, *, candidate: CninfoS2CandidateFacts,
    expected_fields: tuple[str, ...],
) -> MachineVerification:
    """Replay native extraction and arithmetic from bytes; never trust candidate values."""

    if not isinstance(candidate, CninfoS2CandidateFacts):
        raise MachineVerificationError("candidate has an unexpected type")
    if (len(expected_fields) != len(set(expected_fields))
            or not expected_fields or not set(expected_fields) <= set(_FIELDS)):
        raise MachineVerificationError("expected_fields must be a unique canonical field subset")
    # The ordinary writer supplies sorted field names. Field order carries no
    # accounting meaning; retain this provider's deterministic source order.
    expected_fields = tuple(field for field in _FIELDS if field in expected_fields)
    candidate_fields = tuple(item.field for item in candidate.candidates)
    if candidate_fields != expected_fields:
        raise MachineVerificationError("candidate fields differ from the requested field cohort")
    try:
        replayed, evidence = _extract_report(
            pdf_bytes, instrument_id=candidate.instrument_id, company=_COMPANY,
            period_end=candidate.period_end,
            announcement_id=_ANNOUNCEMENT_ID,
            document_url=candidate.document_url,
            required_fields=expected_fields,
        )
    except ChifengNativeNoteError as exc:
        raise MachineVerificationError(f"native report replay failed: {exc}") from exc
    if replayed != candidate:
        raise MachineVerificationError("candidate values or source-row evidence differ from native replay")

    verifications = []
    for field in expected_fields:
        item = candidate.by_field[field]
        field_evidence = evidence["field_evidence"][field]
        independent = field_evidence["independent_native_arithmetic"]
        if field == "revenue":
            holds = (independent["decomposition"]["holds"]
                     and independent["geography_breakdown"]["holds"])
            detail = "native main plus other business equals total; separate geography totals equal the same current revenue"
        elif field == "net_profit_attributable":
            holds = (independent["retained_earnings_rollforward"]["holds"]
                     and independent["direct_eps_numerator"]["holds"])
            detail = "retained-earnings roll-forward reconciles and a separate direct EPS numerator matches"
        elif field == "net_profit_consolidated":
            holds = independent["profit_before_tax_less_income_tax"]["holds"]
            detail = "separate tax reconciliation profit before tax less total income tax expense equals consolidated net profit"
        else:
            holds = independent["cashflow_reconciliation"]["holds"]
            detail = "net profit plus all signed native reconciliation adjustments equals operating cashflow"
        check = IndependentCheck(
            path=_NATIVE_PATH, holds=bool(holds), detail=detail,
            evidence={
                "proof_schema": _PROOF_SCHEMA,
                "extractor_version": _EXTRACTOR_VERSION,
                "field": field,
                "candidate_value_yuan": str(item.value_yuan),
                "native_proof": field_evidence,
                "source_identity": evidence["identity"],
            },
        )
        verifications.append(FieldVerification(field, item.value_yuan, (check,)))

    identity_check = IndependentCheck(
        path=_NATIVE_PATH, holds=True,
        detail="native p8 legal name, p9 labelled A-share stock row, distinct H-share row, and repeated audited consolidated-note headers identify this original/version",
        evidence={"proof_schema": _PROOF_SCHEMA, "extractor_version": _EXTRACTOR_VERSION,
                  "identity": evidence["identity"],
                  "source_binding": evidence["source_binding"]},
    )
    return MachineVerification(
        instrument_id=candidate.instrument_id, period_end=candidate.period_end,
        announcement_id=_ANNOUNCEMENT_ID, pdf_sha256=candidate.pdf_sha256,
        fields=tuple(verifications), sections=(evidence,), identities=(identity_check,),
    )


__all__ = [
    "ChifengNativeNoteError",
    "extract_native_note_facts",
    "is_supported_pdf",
    "verify_native_note_evidence",
]
