"""Independent native-text check for consolidated net profit.

The helper selects the consolidated income-statement heading, unit, current and
prior headers, and the two source rows from PDF-native character coordinates.
It deliberately accepts no candidate amount: the caller compares the result
only after these cells have been selected.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence


EXTRACTOR_VERSION = "cninfo-profit-tax-native-v1"
_UNIT_SCALE = {
    "元": Decimal(1), "千元": Decimal(1000),
    "万元": Decimal(10000), "百万元": Decimal(1000000),
}
_AMOUNT = re.compile(
    r"(?:[-−]?\d{1,3}(?:,\d{3})+|[-−]?\d+)(?:\.\d{1,4})?"
    r"|\([-−]?\d{1,3}(?:,\d{3})+(?:\.\d{1,4})?\)"
    r"|\([-−]?\d+(?:\.\d{1,4})?\)"
)
_UNIT = re.compile(r"单位(?:为)?[:：]?(?:人民币)?(百万元|千元|万元|元)")
_PERIOD = re.compile(r"(20\d{2})年?(半年度|上半年|年度|年年度|1[-－—–]6月)")


class ProfitTaxNotLocatedError(ValueError):
    """The native report does not provide one unambiguous source pair."""


@dataclass(frozen=True, slots=True)
class _Token:
    text: str
    chars: tuple[Mapping[str, object], ...]
    x0: float
    x1: float
    top: float
    bottom: float

    @property
    def center(self) -> float:
        return (self.x0 + self.x1) / 2

    def as_evidence(self) -> dict[str, object]:
        return {
            "text": self.text,
            "bbox": [self.x0, self.top, self.x1, self.bottom],
            "chars": [
                {key: char[key] for key in ("text", "x0", "top", "x1", "bottom")}
                for char in self.chars
            ],
        }


@dataclass(frozen=True, slots=True)
class _Line:
    page: int
    top: float
    text: str
    tokens: tuple[_Token, ...]
    chars: tuple[Mapping[str, object], ...]

    @property
    def x0(self) -> float:
        return min(float(char["x0"]) for char in self.chars)

    @property
    def x1(self) -> float:
        return max(float(char["x1"]) for char in self.chars)

    @property
    def bottom(self) -> float:
        return max(float(char["bottom"]) for char in self.chars)

    def as_label_evidence(self) -> dict[str, object]:
        return {
            "page": self.page,
            "text": self.text,
            "bbox": [self.x0, self.top, self.x1, self.bottom],
            "chars": [
                {key: char[key] for key in ("text", "x0", "top", "x1", "bottom")}
                for char in self.chars
            ],
        }


@dataclass(frozen=True, slots=True)
class ProfitBeforeTaxLessIncomeTax:
    evidence: dict[str, object]
    current_recomputed_yuan: Decimal
    prior_recomputed_yuan: Decimal


def _get(value: object, name: str, alternate: str | None = None) -> object:
    if isinstance(value, Mapping):
        if name in value:
            return value[name]
        if alternate and alternate in value:
            return value[alternate]
        raise ProfitTaxNotLocatedError(f"income-statement section lacks {name}")
    if hasattr(value, name):
        return getattr(value, name)
    raise ProfitTaxNotLocatedError(f"income-statement section lacks {name}")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).replace("−", "-").replace("–", "-")


def _tokenize(chars: Sequence[Mapping[str, object]]) -> tuple[_Token, ...]:
    ordered = sorted(chars, key=lambda char: (float(char["x0"]), float(char["top"])))
    groups: list[list[Mapping[str, object]]] = []
    for char in ordered:
        text = str(char.get("text", ""))
        if not text or text.isspace():
            continue
        if groups:
            previous = groups[-1][-1]
            gap = float(char["x0"]) - float(previous["x1"])
            same_line = abs(float(char["top"]) - float(previous["top"])) <= 1.5
            if same_line and gap <= 3.2:
                groups[-1].append(char)
                continue
        groups.append([char])
    tokens = []
    for group in groups:
        tokens.append(_Token(
            text="".join(str(char.get("text", "")) for char in group),
            chars=tuple(group),
            x0=min(float(char["x0"]) for char in group),
            x1=max(float(char["x1"]) for char in group),
            top=min(float(char["top"]) for char in group),
            bottom=max(float(char["bottom"]) for char in group),
        ))
    return tuple(tokens)


def _native_lines(page: Mapping[str, object]) -> tuple[_Line, ...]:
    page_number = page.get("physicalPage")
    raw_chars = page.get("chars")
    if (not isinstance(page_number, int) or
            not isinstance(raw_chars, (list, tuple)) or not raw_chars):
        raise ProfitTaxNotLocatedError("native page characters are missing")
    chars = [char for char in raw_chars if isinstance(char, Mapping) and
             all(key in char for key in ("text", "x0", "x1", "top", "bottom"))]
    if not chars:
        raise ProfitTaxNotLocatedError("native page characters have no usable coordinates")
    ordered = sorted(chars, key=lambda char: (float(char["top"]), float(char["x0"])))
    rows: list[list[Mapping[str, object]]] = []
    for char in ordered:
        if rows and abs(float(char["top"]) - float(rows[-1][0]["top"])) <= 1.5:
            rows[-1].append(char)
        else:
            rows.append([char])
    lines = []
    for row in rows:
        row.sort(key=lambda char: float(char["x0"]))
        tokens = _tokenize(row)
        if not tokens:
            continue
        lines.append(_Line(
            page=page_number,
            top=min(float(char["top"]) for char in row),
            text=" ".join(token.text for token in tokens),
            tokens=tokens,
            chars=tuple(row),
        ))
    return tuple(lines)


def _expected_period_label(period_end: date) -> tuple[int, str]:
    if period_end.month == 6 and period_end.day == 30:
        return period_end.year, "H1"
    if period_end.month == 12 and period_end.day == 31:
        return period_end.year, "FY"
    raise ProfitTaxNotLocatedError("only half-year and full-year statements are supported")


def _period_token(token: _Token) -> tuple[int, str] | None:
    compact = _compact(token.text)
    match = _PERIOD.fullmatch(compact)
    if not match:
        return None
    suffix = match.group(2)
    kind = "H1" if suffix in ("半年度", "上半年", "1-6月") else "FY"
    return int(match.group(1)), kind


def _unit(line: _Line) -> str | None:
    match = _UNIT.search(_compact(line.text))
    return None if match is None else match.group(1)


def _amount(token: _Token) -> Decimal | None:
    text = _compact(token.text)
    if not _AMOUNT.fullmatch(text):
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = text.replace(",", "").replace("−", "-").replace("–", "-")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if negative and value > 0:
        value = -value
    return value if value.is_finite() else None


def _label_parts(line: _Line) -> tuple[tuple[_Token, ...], tuple[Mapping[str, object], ...]]:
    """Return only source-native label tokens/chars, excluding inline amounts."""

    amount_index = next((index for index, token in enumerate(line.tokens)
                         if _amount(token) is not None), len(line.tokens))
    tokens = line.tokens[:amount_index]
    chars = tuple(char for token in tokens for char in token.chars)
    return tokens, chars


def _label_evidence(line: _Line) -> dict[str, object]:
    tokens, chars = _label_parts(line)
    if not chars:
        raise ProfitTaxNotLocatedError("row label has no source-native characters")
    return {
        "page": line.page,
        "text": " ".join(token.text for token in tokens),
        "bbox": [min(float(char["x0"]) for char in chars), line.top,
                 max(float(char["x1"]) for char in chars),
                 max(float(char["bottom"]) for char in chars)],
        "chars": [
            {key: char[key] for key in ("text", "x0", "top", "x1", "bottom")}
            for char in chars
        ],
    }


def _line_evidence(line: _Line) -> dict[str, object]:
    return {
        "page": line.page,
        "text": line.text,
        "bbox": [line.x0, line.top, line.x1, line.bottom],
        "chars": [
            {key: char[key] for key in ("text", "x0", "top", "x1", "bottom")}
            for char in line.chars
        ],
    }


def _find_native_rows(
    lines: Sequence[_Line], *, period_end: date, section: object,
) -> tuple[_Line, _Line, _Token, _Token, _Token, _Token,
           str, _Line, _Line, _Line, _Line, _Line]:
    role = _get(section, "role")
    statement = _get(section, "statement")
    start_page = _get(section, "start_page", "startPage")
    end_page = _get(section, "end_page", "endPage")
    if (role != "合并" or statement != "利润表" or
            not isinstance(start_page, int) or not isinstance(end_page, int)):
        raise ProfitTaxNotLocatedError(
            "fallback requires the consolidated income-statement section")
    scoped = [line for line in lines if start_page <= line.page <= end_page]
    headings = [line for line in scoped
                if "合并利润表" in _compact(line.text)
                and "母公司" not in _compact(line.text)
                and "合并及公司" not in _compact(line.text)
                and "合并及母公司" not in _compact(line.text)]
    if len(headings) != 1:
        raise ProfitTaxNotLocatedError("no unique standalone consolidated profit-statement heading")
    heading = headings[0]
    same_page = [line for line in scoped if line.page == heading.page and
                 line.top > heading.top]
    expected_year, expected_kind = _expected_period_label(period_end)
    expected_current = (expected_year, expected_kind)
    expected_prior = (expected_year - 1, expected_kind)
    headers = []
    for line in same_page:
        dated = [(token, _period_token(token)) for token in line.tokens]
        currents = [token for token, label in dated if label == expected_current]
        priors = [token for token, label in dated if label == expected_prior]
        if len(currents) == 1 and len(priors) == 1 and currents[0].x0 < priors[0].x0:
            headers.append((line, currents[0], priors[0]))
    if len(headers) != 1:
        raise ProfitTaxNotLocatedError(
            "explicit current and prior columns are not uniquely located")
    header_line, current_header, prior_header = headers[0]
    unit_lines = [(line, _unit(line)) for line in same_page
                  if line.top < header_line.top and _unit(line) is not None]
    if len(unit_lines) != 1:
        raise ProfitTaxNotLocatedError("profit-statement unit is not unique beside its header")
    unit_line, unit = unit_lines[0]
    separator = (current_header.center + prior_header.center) / 2

    table_lines = [line for line in scoped if
                   (line.page > header_line.page or
                    (line.page == header_line.page and line.top > header_line.top))]
    pbt_labels = [line for line in table_lines
                  if "利润总额" in _compact(line.text)
                  and "营业利润" not in _compact(line.text)]
    tax_labels = [line for line in table_lines
                  if "所得税费用" in _compact(line.text)
                  and "递延所得税费用" not in _compact(line.text)]
    if len(pbt_labels) != 1 or len(tax_labels) != 1:
        raise ProfitTaxNotLocatedError(
            "profit-before-tax or income-tax row is missing or ambiguous")

    def cells_for(label: _Line) -> tuple[_Line, _Token, _Token]:
        _, label_chars = _label_parts(label)
        if not label_chars:
            raise ProfitTaxNotLocatedError("row label has no source-native characters")
        label_x1 = max(float(char["x1"]) for char in label_chars)
        value_lines = [line for line in table_lines if line.page == label.page and
                       -1 <= line.top - label.top <= 12]
        candidates = []
        for line in value_lines:
            amounts = [token for token in line.tokens if _amount(token) is not None]
            current_tokens = [token for token in amounts
                              if current_header.x0 - 1 <= token.center < separator]
            prior_tokens = [token for token in amounts
                            if separator < token.center and prior_header.x0 - 1 <= token.center]
            if len(current_tokens) == 1 and len(prior_tokens) == 1:
                current = current_tokens[0]
                prior = prior_tokens[0]
                if (label_x1 >= current.x0 or current.center >= separator or
                        prior.center <= separator):
                    continue
                candidates.append((line, current, prior))
        if len(candidates) != 1:
            raise ProfitTaxNotLocatedError(
                "row does not have one own current/prior amount-cell pair")
        return candidates[0]

    pbt_line, pbt_current, pbt_prior = cells_for(pbt_labels[0])
    tax_line, tax_current, tax_prior = cells_for(tax_labels[0])
    if pbt_line.page != tax_line.page:
        raise ProfitTaxNotLocatedError("profit and tax cells do not share the same statement page")
    if unit is None:
        raise ProfitTaxNotLocatedError("income-statement unit is missing")
    return (pbt_labels[0], tax_labels[0], pbt_current, pbt_prior,
            tax_current, tax_prior,
            unit, unit_line, header_line, heading, current_header,
            prior_header)


def extract_profit_before_tax_less_income_tax_from_pages(
    pages: Sequence[Mapping[str, object]], *, instrument_id: str,
    period_end: date, section: object,
) -> ProfitBeforeTaxLessIncomeTax:
    """Parse saved native page characters without opening or extracting a PDF."""

    if not re.fullmatch(r"\d{6}", instrument_id):
        raise ProfitTaxNotLocatedError("instrument id is not a six-digit CNINFO code")
    start_page = _get(section, "start_page", "startPage")
    end_page = _get(section, "end_page", "endPage")
    selected_pages = [page for page in pages if isinstance(page.get("physicalPage"), int)
                      and start_page <= page["physicalPage"] <= end_page]
    if not selected_pages:
        raise ProfitTaxNotLocatedError("native pages do not cover the income-statement section")
    lines = tuple(line for page in selected_pages for line in _native_lines(page))
    (pbt_label, tax_label, pbt_current, pbt_prior, tax_current, tax_prior,
     unit, unit_line, header_line, heading, current_header, prior_header) = _find_native_rows(
        lines, period_end=period_end, section=section)
    scale = _UNIT_SCALE.get(unit)
    if scale is None:
        raise ProfitTaxNotLocatedError(f"unsupported income-statement unit {unit!r}")
    pbt_current_value = _amount(pbt_current)
    pbt_prior_value = _amount(pbt_prior)
    tax_current_value = _amount(tax_current)
    tax_prior_value = _amount(tax_prior)
    if None in (pbt_current_value, pbt_prior_value, tax_current_value, tax_prior_value):
        raise ProfitTaxNotLocatedError("one or more native amount cells are not numeric")
    current_yuan = (pbt_current_value - tax_current_value) * scale
    prior_yuan = (pbt_prior_value - tax_prior_value) * scale
    current_header_evidence = current_header.as_evidence()
    current_header_evidence["page"] = header_line.page
    prior_header_evidence = prior_header.as_evidence()
    prior_header_evidence["page"] = header_line.page
    pbt_current_evidence = pbt_current.as_evidence()
    pbt_current_evidence["page"] = pbt_label.page
    pbt_prior_evidence = pbt_prior.as_evidence()
    pbt_prior_evidence["page"] = pbt_label.page
    tax_current_evidence = tax_current.as_evidence()
    tax_current_evidence["page"] = tax_label.page
    tax_prior_evidence = tax_prior.as_evidence()
    tax_prior_evidence["page"] = tax_label.page
    evidence = {
        "extractor_version": EXTRACTOR_VERSION,
        "instrument_id": instrument_id,
        "period_end": period_end.isoformat(),
        "role": "合并",
        "statement": "利润表",
        "section": {
            "start_page": _get(section, "start_page", "startPage"),
            "end_page": _get(section, "end_page", "endPage"),
        },
        "statement_heading": _label_evidence(heading),
        "amount_unit": unit,
        "unit_declaration": _line_evidence(unit_line),
        "current_header": current_header_evidence,
        "prior_header": prior_header_evidence,
        "header_line": _line_evidence(header_line),
        "profit_before_tax": {
            "row_label": _label_evidence(pbt_label),
            "current_cell": pbt_current.text,
            "current_cell_yuan": str(pbt_current_value * scale),
            "current_cell_native": pbt_current_evidence,
            "prior_cell": pbt_prior.text,
            "prior_cell_yuan": str(pbt_prior_value * scale),
            "prior_cell_native": pbt_prior_evidence,
        },
        "income_tax_expense": {
            "row_label": _label_evidence(tax_label),
            "current_cell": tax_current.text,
            "current_cell_yuan": str(tax_current_value * scale),
            "current_cell_native": tax_current_evidence,
            "prior_cell": tax_prior.text,
            "prior_cell_yuan": str(tax_prior_value * scale),
            "prior_cell_native": tax_prior_evidence,
        },
        "current_recomputed_yuan": str(current_yuan),
        "prior_recomputed_yuan": str(prior_yuan),
    }
    return ProfitBeforeTaxLessIncomeTax(evidence, current_yuan, prior_yuan)


def extract_profit_before_tax_less_income_tax_expense(
    pdf_bytes: bytes, *, instrument_id: str, period_end: date, section: object,
) -> ProfitBeforeTaxLessIncomeTax:
    """Read only the selected income-statement pages from the supplied PDF bytes."""

    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise ProfitTaxNotLocatedError("input must be PDF bytes")
    start_page = _get(section, "start_page")
    end_page = _get(section, "end_page")
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - deployment dependency guard
        raise ProfitTaxNotLocatedError(
            "pdfplumber is required for native character reading") from exc
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if start_page < 1 or end_page > len(pdf.pages) or end_page < start_page:
                raise ProfitTaxNotLocatedError("income-statement page range is outside the PDF")
            pages = []
            for index in range(start_page - 1, end_page):
                page = pdf.pages[index]
                pages.append({
                    "physicalPage": index + 1,
                    "width": page.width,
                    "height": page.height,
                    "chars": tuple(page.chars),
                })
    except ProfitTaxNotLocatedError:
        raise
    except Exception as exc:
        raise ProfitTaxNotLocatedError(
            f"native character page reading failed: {type(exc).__name__}") from exc
    return extract_profit_before_tax_less_income_tax_from_pages(
        pages, instrument_id=instrument_id, period_end=period_end, section=section)


__all__ = [
    "EXTRACTOR_VERSION", "ProfitBeforeTaxLessIncomeTax",
    "ProfitTaxNotLocatedError", "extract_profit_before_tax_less_income_tax_expense",
    "extract_profit_before_tax_less_income_tax_from_pages",
]
