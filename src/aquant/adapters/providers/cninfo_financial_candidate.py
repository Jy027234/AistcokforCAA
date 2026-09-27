"""Candidate-only extraction of five S2 fields from a CNINFO full report.

This parser deliberately has no promotion or PIT interface. It accepts the
announcement identity supplied by an archived index, checks the PDF cover,
statement scope, period columns and currency unit, and returns source cells
for visual review. Unsupported layouts fail closed.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from .cninfo_financial_pdf import (
    CninfoFinancialPdfError,
    CninfoS2CandidateFacts,
    PdfCandidateFact,
)


_HEADING = re.compile(
    r"^(?:\d+[.、])?(合并|母公司|公司)"
    r"(资产负债表|利润表|现金流量表|所有者权益变动表|股东权益变动表)$"
)
_URL = re.compile(
    r"https://static\.cninfo\.com\.cn/finalpage/\d{4}-\d{2}-\d{2}/(\d+)\.PDF\Z",
    re.IGNORECASE,
)
_UNIT = re.compile(r"单位\s*[:：]\s*(人民币)?(千元|万元|元)(?![\u4e00-\u9fff])")
_CURRENCY = re.compile(r"币种\s*[:：]\s*([A-Za-z\u4e00-\u9fff]+)")
_NUMBER = re.compile(
    r"(?:-?(?:0|[1-9]\d{0,2}(?:,\d{3})*)(?:\.\d{1,2})?"
    r"|\((?:0|[1-9]\d{0,2}(?:,\d{3})*)(?:\.\d{1,2})?\))\Z"
)
_TEXT_AMOUNT = re.compile(
    r"(?<![\d,])(?:-?[1-9]\d{0,2}(?:,\d{3})+(?:\.\d{1,2})?"
    r"|\([1-9]\d{0,2}(?:,\d{3})+(?:\.\d{1,2})?\))(?![\d,])"
)
_TEXT_HEADER = re.compile(
    r"\d{4}(?:年半年度|年度)|本期发生额|上年同期发生额|本年发生额|上年发生额"
)
_NOTE_SUFFIX = re.compile(
    r"(?:[一二三四五六七八九十]+[、.]|\([一二三四五六七八九十]+\))\d+(?:\(\d+\))?\s*\Z"
)
_UNIT_MULTIPLIER = {"元": Decimal(1), "千元": Decimal(1000), "万元": Decimal(10000)}
_NOTE = re.compile(r"^(?:[一二三四五六七八九十]+[、.]|\([一二三四五六七八九十]+\))\d+(?:\(\d+\))?$", re.ASCII)
_FIELD_STATEMENT = {
    "parent_equity": "资产负债表",
    "revenue": "利润表",
    "net_profit_consolidated": "利润表",
    "net_profit_attributable": "利润表",
    "operating_cashflow": "现金流量表",
}
INDUSTRIAL_PILOT_COMPANIES = {
    "000651": "珠海格力电器股份有限公司",
    "600276": "江苏恒瑞医药股份有限公司",
    "002415": "杭州海康威视数字技术股份有限公司",
    "300750": "宁德时代新能源科技股份有限公司",
    "688981": "中芯国际集成电路制造有限公司",
}
REQUIRED_PILOT_FIELDS_BY_PERIOD = {
    "2024-06-30": ("revenue",),
    "2024-12-31": ("revenue",),
    "2025-06-30": ("parent_equity", "revenue", "net_profit_consolidated",
                   "net_profit_attributable", "operating_cashflow"),
    "2025-12-31": ("revenue", "net_profit_consolidated",
                   "net_profit_attributable", "operating_cashflow"),
    "2026-06-30": ("parent_equity", "revenue", "net_profit_consolidated",
                   "net_profit_attributable", "operating_cashflow"),
}


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("（", "(").replace("）", ")")


@dataclass(frozen=True, slots=True)
class _Heading:
    role: str
    statement: str
    page_index: int
    top: float
    bottom: float
    unit: str | None


def _page_headings(page: Any, page_index: int) -> list[_Heading]:
    text = page.extract_text() or ""
    lines = text.splitlines()
    result = []
    for index, line in enumerate(lines):
        match = _HEADING.fullmatch(_compact(line))
        if match is None:
            continue
        # A continuation heading is not a new statement. The precise line
        # match also avoids contents pages and narrative references.
        occurrences = page.search(re.escape(line.strip()))
        if len(occurrences) != 1:
            raise CninfoFinancialPdfError("ambiguous statement heading position")
        units = []
        for following in lines[index + 1:index + 5]:
            currency = _CURRENCY.search(following)
            if currency is not None and currency.group(1).upper() not in ("人民币", "CNY"):
                raise CninfoFinancialPdfError("statement explicitly uses a non-CNY currency")
            candidate = _UNIT.search(following)
            if candidate is not None:
                units.append(candidate.group(2))
        if len(set(units)) > 1:
            raise CninfoFinancialPdfError("statement has conflicting amount units")
        unit = units[0] if units else None
        result.append(_Heading(
            match.group(1), match.group(2), page_index,
            float(occurrences[0]["top"]), float(occurrences[0]["bottom"]), unit,
        ))
    return result


def _expected_header(statement: str, period: date, labels: list[str]) -> tuple[str, str]:
    header = [_compact(label) for label in labels if label]
    if "项目" not in header:
        raise CninfoFinancialPdfError(f"{statement}: table has no project header")
    joined = "|".join(header)
    if statement == "资产负债表":
        current = next((label for label in header if re.search(
            rf"{period.year}年0?{period.month}月0?{period.day}日", label,
        )), None)
        prior = next((label for label in header if re.search(
            rf"{period.year - 1}年12月31日", label,
        )), None)
        if current and prior and header.index(current) < header.index(prior):
            return current, prior
        if "期末余额" in header and "期初余额" in header and header.index("期末余额") < header.index("期初余额"):
            return "期末余额", "期初余额"
    else:
        current = next((label for label in header if str(period.year) in label and
                        ("半年度" in label if period.month == 6 else "年度" in label)), None)
        prior = next((label for label in header if str(period.year - 1) in label and
                      ("半年度" in label if period.month == 6 else "年度" in label)), None)
        if current and prior and header.index(current) < header.index(prior):
            return current, prior
        if ("本期发生额" in header and "上年同期发生额" in header and
                header.index("本期发生额") < header.index("上年同期发生额")):
            return "本期发生额", "上年同期发生额"
        annual = next((label for label in header if label.startswith("本年发生额")), None)
        prior_annual = next((label for label in header if label.startswith("上年发生额")), None)
        if annual and prior_annual and header.index(annual) < header.index(prior_annual):
            return annual, prior_annual
    raise CninfoFinancialPdfError(f"{statement}: current/prior header is ambiguous: {joined[:160]}")


def _field_matches(field: str, label: str) -> bool:
    clean = _compact(label)
    clean = re.sub(r"^[一二三四五六七八九十]+[、.]", "", clean)
    clean = re.sub(r"^\d+[.、]", "", clean)
    if field == "parent_equity":
        return bool(re.match(r"^归属于母公司(?:所有者|股东)(?:权益|股东权益)", clean)) and "合计" in clean
    if field == "revenue":
        return clean in ("营业收入", "其中:营业收入", "其中：营业收入")
    if field == "net_profit_consolidated":
        return clean.startswith("净利润") and not any(
            word in clean for word in ("持续经营", "终止经营", "归属于")
        )
    if field == "net_profit_attributable":
        return bool(re.match(r"^归属于母公司(?:股东|所有者)的净利润", clean))
    if field == "operating_cashflow":
        return bool(re.match(r"^经营活动产生(?:/\(使用\))?的现金流量净额", clean))
    return False


def _visible_label(row: list[str | None]) -> str:
    first_numeric = next((index for index, cell in enumerate(row)
                          if _NUMBER.fullmatch((cell or "").strip())), len(row))
    return "".join(
        cell or "" for cell in row[:first_numeric]
        if cell and not _NOTE.fullmatch(_compact(cell))
    )


def _row_amounts(row: list[str | None], *, field: str) -> tuple[str, int, int, Decimal]:
    numeric = []
    for index, cell in enumerate(row):
        value = (cell or "").strip()
        if _NUMBER.fullmatch(value):
            numeric.append((index, value))
    if len(numeric) != 2:
        raise CninfoFinancialPdfError(f"{field}: expected exactly two numeric period cells")
    current_index, current = numeric[0]
    prior_index, _ = numeric[1]
    label = _visible_label(row)
    try:
        amount = Decimal(("-" + current[1:-1] if current.startswith("(")
                          else current).replace(",", ""))
    except InvalidOperation as exc:
        raise CninfoFinancialPdfError(f"{field}: invalid amount") from exc
    return label, current_index, prior_index, amount


def _text_layout_rows(
    segments: list[tuple[int, str]], statement: str, period: date,
) -> tuple[tuple[str, str], list[tuple[int, list[str | None]]]]:
    """Fallback for reports whose borderless cells lose row labels in pdfplumber."""
    headers = []
    rows = []
    for page_number, text in segments:
        for line in text.splitlines():
            compact = _compact(line)
            if "项目" in compact and (str(period.year) in compact or "本期发生额" in compact):
                labels = ["项目", *_TEXT_HEADER.findall(compact)]
                try:
                    headers.append(_expected_header(statement, period, labels))
                except CninfoFinancialPdfError:
                    pass
            amounts = list(_TEXT_AMOUNT.finditer(line))
            if len(amounts) != 2:
                continue
            label = _NOTE_SUFFIX.sub("", line[:amounts[0].start()]).strip()
            rows.append((page_number, [label, amounts[0].group(), amounts[1].group()]))
    if not headers or len(set(headers)) != 1:
        raise CninfoFinancialPdfError(f"{statement}: borderless text header is ambiguous")
    return headers[0], rows


def extract_cninfo_candidate_facts(
    pdf_bytes: bytes, *, instrument_id: str, company: str,
    period_end: date, announcement_id: str, document_url: str,
    required_fields: tuple[str, ...] | None = None,
) -> CninfoS2CandidateFacts:
    """Extract unreviewed current-column values; never confer PIT eligibility."""
    if (not re.fullmatch(r"\d{6}", instrument_id) or not company.strip() or
            period_end.month not in (6, 12) or period_end.day not in (30, 31) or
            (period_end.month, period_end.day) not in ((6, 30), (12, 31))):
        raise CninfoFinancialPdfError("unsupported security or report period")
    url_match = _URL.fullmatch(document_url)
    if url_match is None or url_match.group(1) != announcement_id:
        raise CninfoFinancialPdfError("official announcement identity mismatch")
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise CninfoFinancialPdfError("input must be PDF bytes")
    required = required_fields or tuple(_FIELD_STATEMENT)
    if len(required) != len(set(required)) or not set(required) <= set(_FIELD_STATEMENT):
        raise CninfoFinancialPdfError("required S2 fields are invalid")
    try:
        import pdfplumber
    except ImportError as exc:
        raise CninfoFinancialPdfError("pdfplumber is required for PDF candidate extraction") from exc
    digest = "sha256:" + hashlib.sha256(pdf_bytes).hexdigest()
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            cover = _compact(pdf.pages[0].extract_text() or "")
            report_kind = "半年度报告" if period_end.month == 6 else "年度报告"
            if (_compact(company) not in cover or report_kind not in cover or
                    not re.search(rf"(?<!\d){period_end.year}(?!\d)", cover)):
                raise CninfoFinancialPdfError("PDF cover company or period mismatch")
            headings = [heading for index, page in enumerate(pdf.pages)
                        for heading in _page_headings(page, index)]
            starts = {}
            needed_statements = { _FIELD_STATEMENT[field] for field in required }
            for statement in ("资产负债表", "利润表", "现金流量表"):
                if statement not in needed_statements:
                    continue
                matches = [item for item in headings if item.role == "合并" and
                           item.statement == statement and item.unit is not None]
                if len(matches) != 1:
                    raise CninfoFinancialPdfError(f"{statement}: consolidated heading/unit is ambiguous")
                starts[statement] = matches[0]
            if [starts[item].page_index for item in ("资产负债表", "利润表", "现金流量表")
                if item in starts] != sorted(item.page_index for item in starts.values()):
                raise CninfoFinancialPdfError("consolidated statement order is invalid")

            evidence: dict[str, tuple[str, str, str, list[tuple[int, list[str | None]]]]] = {}
            for statement, start in starts.items():
                following = [item for item in headings if
                             (item.page_index, item.top) > (start.page_index, start.top)]
                end = min(following, key=lambda item: (item.page_index, item.top)) if following else None
                if end is None or end.page_index - start.page_index > 10:
                    raise CninfoFinancialPdfError(f"{statement}: no nearby statement boundary")
                if end.role == "合并" and end.statement == statement:
                    raise CninfoFinancialPdfError(f"{statement}: duplicate consolidated section")
                rows = []
                segments = []
                header = None
                for page_index in range(start.page_index, end.page_index + 1):
                    page = pdf.pages[page_index]
                    top = start.bottom if page_index == start.page_index else 0
                    bottom = end.top if page_index == end.page_index else page.height
                    if bottom > top:
                        segments.append((page_index + 1, page.crop((0, top, page.width, bottom)).extract_text() or ""))
                    for table in page.find_tables():
                        if (page_index == start.page_index and table.bbox[1] <= start.bottom) or (page_index == end.page_index and table.bbox[3] >= end.top):
                            continue
                        extracted = table.extract()
                        if not extracted:
                            continue
                        if header is None:
                            try:
                                header = _expected_header(statement, period_end, extracted[0])
                            except CninfoFinancialPdfError:
                                continue
                        for row in extracted:
                            if not isinstance(row, list):
                                continue
                            rows.append((page_index + 1, row))
                if header is None:
                    header, rows = _text_layout_rows(segments, statement, period_end)
                evidence[statement] = (start.unit or "", header[0], header[1], rows)

            facts = []
            for field in required:
                statement = _FIELD_STATEMENT[field]
                unit, current_header, _, rows = evidence[statement]
                matches = []
                for page_number, row in rows:
                    label = _visible_label(row)
                    if _field_matches(field, label):
                        matches.append((page_number, row))
                if len(matches) != 1:
                    raise CninfoFinancialPdfError(f"{field}: expected unique consolidated row, found {len(matches)}")
                page_number, row = matches[0]
                label, current_index, prior_index, amount = _row_amounts(row, field=field)
                page_text = pdf.pages[page_number - 1].extract_text() or ""
                if (row[current_index] not in page_text or row[prior_index] not in page_text):
                    raise CninfoFinancialPdfError(f"{field}: table/text evidence mismatch")
                facts.append(PdfCandidateFact(
                    field=field, value_yuan=amount * _UNIT_MULTIPLIER[unit],
                    period_end=period_end, report_period_text=current_header,
                    statement={"资产负债表": "balance", "利润表": "profit",
                               "现金流量表": "cashflow"}[statement],
                    pdf_page=page_number, column_header=current_header,
                    amount_unit=unit, currency="CNY", source_row="\t".join(
                        cell or "" for cell in row), source_cells=tuple(row),
                    source_current_cell_index=current_index,
                    source_prior_cell_index=prior_index,
                    pdf_sha256=digest, display_row_label=label,
                ))
    except CninfoFinancialPdfError:
        raise
    except Exception as exc:  # noqa: BLE001 - unknown parser behavior cannot be promoted
        raise CninfoFinancialPdfError(f"PDF candidate parsing failed: {type(exc).__name__}") from exc
    return CninfoS2CandidateFacts(
        instrument_id=instrument_id, period_end=period_end,
        version_label="indexed_full_report_unreviewed", document_url=document_url,
        pdf_sha256=digest, candidates=tuple(facts),
    )


__all__ = [
    "INDUSTRIAL_PILOT_COMPANIES", "REQUIRED_PILOT_FIELDS_BY_PERIOD",
    "extract_cninfo_candidate_facts",
]
