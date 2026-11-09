"""Independent native-cell check for consolidated operating cash flow.

The proof selects the current-period inflow and outflow subtotal cells from
the cash-flow statement itself, then compares their difference with the
candidate only after both cells have been selected.
"""

from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence


EXTRACTOR_VERSION = "cninfo-operating-cashflow-subtotals-native-v2"
_GEOMETRY_TOLERANCE = 1.0
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
_REPORT_PERIOD = re.compile(r"(20\d{2})年?(半年度报告|年度报告)")
_TARGET_LABELS = (
    "经营活动现金流入小计",
    "经营活动现金流出小计",
)
_PRIMARY_CFO_LABEL = "经营活动产生的现金流量净额"
_STATEMENT_NUMBERING = re.compile(
    r"^(?:[0-9]+|[一二三四五六七八九十百]+)[、.．]"
)
_STATEMENT_NAMES = ("资产负债表", "利润表", "现金流量表")


class OperatingCashflowSubtotalsNotLocatedError(ValueError):
    """The native statement does not expose one unambiguous subtotal pair."""


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
            "chars": [_char_evidence(char) for char in self.chars],
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


@dataclass(frozen=True, slots=True)
class OperatingCashflowSubtotals:
    evidence: dict[str, object]
    current_recomputed_yuan: Decimal
    prior_recomputed_yuan: Decimal


def _char_evidence(char: Mapping[str, object]) -> dict[str, object]:
    return {
        key: char[key]
        for key in ("text", "x0", "top", "x1", "bottom")
    }


def _get(value: object, name: str, alternate: str | None = None) -> object:
    if isinstance(value, Mapping):
        if name in value:
            return value[name]
        if alternate and alternate in value:
            return value[alternate]
        raise OperatingCashflowSubtotalsNotLocatedError(
            f"cash-flow section lacks {name}")
    if hasattr(value, name):
        return getattr(value, name)
    raise OperatingCashflowSubtotalsNotLocatedError(
        f"cash-flow section lacks {name}")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).replace("−", "-").replace("–", "-")


def _same_visual_line(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    overlap = min(float(left["bottom"]), float(right["bottom"])) - max(
        float(left["top"]), float(right["top"]),
    )
    left_height = float(left["bottom"]) - float(left["top"])
    right_height = float(right["bottom"]) - float(right["top"])
    return overlap > 0 and overlap >= 0.6 * min(left_height, right_height)


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
            same_line = _same_visual_line(previous, char)
            if same_line and gap <= 3.2:
                groups[-1].append(char)
                continue
        groups.append([char])
    return tuple(_Token(
        text="".join(str(char.get("text", "")) for char in group),
        chars=tuple(group),
        x0=min(float(char["x0"]) for char in group),
        x1=max(float(char["x1"]) for char in group),
        top=min(float(char["top"]) for char in group),
        bottom=max(float(char["bottom"]) for char in group),
    ) for group in groups)


def _native_lines(page: Mapping[str, object]) -> tuple[_Line, ...]:
    page_number = page.get("physicalPage")
    raw_chars = page.get("chars")
    if (not isinstance(page_number, int) or
            not isinstance(raw_chars, (list, tuple)) or not raw_chars):
        raise OperatingCashflowSubtotalsNotLocatedError(
            "native page characters are missing")
    chars = [char for char in raw_chars if isinstance(char, Mapping) and
             all(key in char for key in ("text", "x0", "x1", "top", "bottom"))]
    if not chars:
        raise OperatingCashflowSubtotalsNotLocatedError(
            "native page characters have no usable coordinates")
    ordered = sorted(chars, key=lambda char: (float(char["top"]), float(char["x0"])))
    rows: list[list[Mapping[str, object]]] = []
    for char in ordered:
        if rows and _same_visual_line(rows[-1][0], char):
            rows[-1].append(char)
        else:
            rows.append([char])
    lines = []
    for row in rows:
        row.sort(key=lambda char: float(char["x0"]))
        tokens = _tokenize(row)
        if tokens:
            lines.append(_Line(
                page=page_number,
                top=min(float(char["top"]) for char in row),
                text=" ".join(token.text for token in tokens),
                tokens=tokens,
                chars=tuple(row),
            ))
    return tuple(lines)


def _amount(text: object) -> Decimal | None:
    compact = _compact(str(text))
    if not _AMOUNT.fullmatch(compact):
        return None
    negative = compact.startswith("(") and compact.endswith(")")
    if negative:
        compact = compact[1:-1]
    compact = compact.replace(",", "").replace("−", "-").replace("–", "-")
    try:
        value = Decimal(compact)
    except InvalidOperation:
        return None
    if negative and value > 0:
        value = -value
    return value if value.is_finite() else None


def _expected_period_label(period_end: date) -> tuple[int, str]:
    if period_end.month == 6 and period_end.day == 30:
        return period_end.year, "H1"
    if period_end.month == 12 and period_end.day == 31:
        return period_end.year, "FY"
    raise OperatingCashflowSubtotalsNotLocatedError(
        "only half-year and full-year statements are supported")


def _period_token(token: _Token) -> tuple[int, str] | None:
    match = _PERIOD.fullmatch(_compact(token.text))
    if match is None:
        return None
    kind = "H1" if match.group(2) in ("半年度", "上半年", "1-6月") else "FY"
    return int(match.group(1)), kind


def _line_evidence(line: _Line) -> dict[str, object]:
    return {
        "page": line.page,
        "text": line.text,
        "bbox": [line.x0, line.top, line.x1, line.bottom],
        "chars": [_char_evidence(char) for char in line.chars],
    }


def _page_dimensions(page: Mapping[str, object]) -> tuple[float, float]:
    dimensions = page.get("pageDimensions")
    if isinstance(dimensions, Mapping):
        width, height = dimensions.get("width"), dimensions.get("height")
    else:
        width, height = page.get("width"), page.get("height")
    if isinstance(width, (int, float)) and isinstance(height, (int, float)):
        return float(width), float(height)
    bbox = page.get("pageBbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        return float(bbox[2]), float(bbox[3])
    raise OperatingCashflowSubtotalsNotLocatedError(
        "native page dimensions are missing")


def _native_cell(
    page: Mapping[str, object], *, table_index: int, row_index: int,
    cell_index: int, cell_text: str, bbox: object,
) -> dict[str, object]:
    if (not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or
            not all(isinstance(value, (int, float)) for value in bbox)):
        raise OperatingCashflowSubtotalsNotLocatedError(
            "subtotal amount cell has no native bounding box")
    x0, top, x1, bottom = (float(value) for value in bbox)
    if not x0 < x1 or not top < bottom:
        raise OperatingCashflowSubtotalsNotLocatedError(
            "subtotal amount cell geometry is invalid")
    width, height = _page_dimensions(page)
    if (x0 < -_GEOMETRY_TOLERANCE or top < -_GEOMETRY_TOLERANCE or
            x1 > width + _GEOMETRY_TOLERANCE or
            bottom > height + _GEOMETRY_TOLERANCE):
        raise OperatingCashflowSubtotalsNotLocatedError(
            "subtotal amount cell extends outside the page")
    raw_chars = page.get("chars")
    if not isinstance(raw_chars, (list, tuple)):
        raise OperatingCashflowSubtotalsNotLocatedError(
            "native amount cell characters are missing")
    chars = []
    for char in raw_chars:
        if not isinstance(char, Mapping) or not all(
                key in char for key in ("text", "x0", "x1", "top", "bottom")):
            continue
        cx = (float(char["x0"]) + float(char["x1"])) / 2
        cy = (float(char["top"]) + float(char["bottom"])) / 2
        if x0 <= cx <= x1 and top <= cy <= bottom:
            if (float(char["x0"]) < x0 - _GEOMETRY_TOLERANCE or
                    float(char["x1"]) > x1 + _GEOMETRY_TOLERANCE or
                    float(char["top"]) < top - _GEOMETRY_TOLERANCE or
                    float(char["bottom"]) > bottom + _GEOMETRY_TOLERANCE):
                raise OperatingCashflowSubtotalsNotLocatedError(
                    "native glyph exceeds its subtotal cell by more than 1 point")
            chars.append(char)
    chars.sort(key=lambda char: (float(char["top"]), float(char["x0"])))
    native_text = "".join(str(char.get("text", "")) for char in chars)
    if not chars or _compact(native_text) != _compact(cell_text):
        raise OperatingCashflowSubtotalsNotLocatedError(
            "native amount glyphs do not reconstruct their table cell")
    value = _amount(native_text)
    if value is None:
        raise OperatingCashflowSubtotalsNotLocatedError(
            "subtotal amount cell is blank or non-numeric")
    return {
        "table_index": table_index,
        "row_index": row_index,
        "cell_index": cell_index,
        "text": native_text,
        "amount": value,
        "bbox": [x0, top, x1, bottom],
        "chars": [_char_evidence(char) for char in chars],
    }


def _statement_heading_kind(line: _Line) -> tuple[str, str] | None:
    """Return the entity and statement for an explicit standalone title line."""

    text = _STATEMENT_NUMBERING.sub("", _compact(line.text), count=1)
    for statement in _STATEMENT_NAMES:
        if not text.endswith(statement):
            continue
        entity = text[:-len(statement)]
        if entity in ("", "合并", "母公司", "公司"):
            return entity, statement
    return None


def _statement_page_scopes(
    pages: Sequence[Mapping[str, object]],
    lines_by_page: Mapping[int, Sequence[_Line]],
    *, heading: _Line,
) -> dict[int, dict[str, object]]:
    """Bound each continuation page by native standalone statement titles."""

    scopes: dict[int, dict[str, object]] = {}
    statement_open = True
    for page in pages:
        page_number = page["physicalPage"]
        if page_number < heading.page:
            continue
        if not statement_open:
            break
        page_titles = [
            (line, kind)
            for line in lines_by_page[page_number]
            if (kind := _statement_heading_kind(line)) is not None
        ]
        if page_number == heading.page:
            starts = [line for line, _ in page_titles if line == heading]
            if len(starts) != 1:
                raise OperatingCashflowSubtotalsNotLocatedError(
                    "the selected consolidated heading is not a unique native title line")
            start_heading = starts[0]
            following_titles = [
                line for line, _ in page_titles
                if line != start_heading and line.top >= start_heading.bottom
            ]
        else:
            start_heading = None
            following_titles = [line for line, _ in page_titles]
        end_heading = min(following_titles, key=lambda line: line.top) if following_titles else None
        scopes[page_number] = {
            "page": page_number,
            "start_after_y": start_heading.bottom if start_heading is not None else None,
            "start_heading": (_line_evidence(start_heading)
                              if start_heading is not None else None),
            "end_before_y": end_heading.top if end_heading is not None else None,
            "end_heading": (_line_evidence(end_heading)
                            if end_heading is not None else None),
        }
        if end_heading is not None:
            statement_open = False
    return scopes


def _row_is_within_statement_scope(
    cell_bbox: object, scope: Mapping[str, object],
) -> bool:
    if (not isinstance(cell_bbox, (list, tuple)) or len(cell_bbox) != 4 or
            not all(isinstance(value, (int, float)) and math.isfinite(float(value))
                    for value in cell_bbox)):
        raise OperatingCashflowSubtotalsNotLocatedError(
            "subtotal label cell has no usable native row geometry")
    _, top, _, bottom = (float(value) for value in cell_bbox)
    if not top < bottom:
        raise OperatingCashflowSubtotalsNotLocatedError(
            "subtotal label row geometry is invalid")
    start = scope["start_after_y"]
    end = scope["end_before_y"]
    if start is not None:
        start = float(start)
        if bottom <= start:
            return False
        if top < start:
            raise OperatingCashflowSubtotalsNotLocatedError(
                "subtotal label row overlaps its statement heading boundary")
    if end is not None:
        end = float(end)
        if top >= end:
            return False
        if bottom > end:
            raise OperatingCashflowSubtotalsNotLocatedError(
                "subtotal label row overlaps the next statement heading boundary")
    return True


def _find_subtotal_row(
    pages: Sequence[Mapping[str, object]], *, label: str,
    column_separator: float,
    page_scopes: Mapping[int, Mapping[str, object]],
) -> dict[str, object]:
    matches = []
    for page in pages:
        page_number = page.get("physicalPage")
        page_scope = page_scopes.get(page_number) if isinstance(page_number, int) else None
        if page_scope is None:
            continue
        raw_tables = page.get("tables")
        if not isinstance(raw_tables, (list, tuple)):
            continue
        for table in raw_tables:
            if not isinstance(table, Mapping):
                continue
            table_index = table.get("tableIndex")
            rows = table.get("rows")
            if not isinstance(table_index, int) or not isinstance(rows, (list, tuple)):
                continue
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                row_index = row.get("rowIndex")
                cell_texts = row.get("nativeCells")
                cell_boxes = row.get("cellBboxes")
                if (not isinstance(row_index, int) or
                        not isinstance(cell_texts, (list, tuple)) or
                        not isinstance(cell_boxes, (list, tuple)) or
                        len(cell_texts) != len(cell_boxes)):
                    continue
                label_cells = [index for index, text in enumerate(cell_texts)
                               if text is not None and _compact(str(text)) == label]
                if not label_cells:
                    continue
                if len(label_cells) != 1:
                    raise OperatingCashflowSubtotalsNotLocatedError(
                        f"{label} label cell is ambiguous")
                label_index = label_cells[0]
                label_box = cell_boxes[label_index]
                if not _row_is_within_statement_scope(label_box, page_scope):
                    continue
                numeric_cells = []
                for index, text in enumerate(cell_texts):
                    if index == label_index or text is None:
                        continue
                    if _amount(text) is not None:
                        numeric_cells.append(index)
                if len(numeric_cells) != 2:
                    raise OperatingCashflowSubtotalsNotLocatedError(
                        f"{label} must have exactly two native period amount cells")
                native_amounts = []
                for index in numeric_cells:
                    native_amounts.append(_native_cell(
                        page, table_index=table_index, row_index=row_index,
                        cell_index=index, cell_text=str(cell_texts[index]),
                        bbox=cell_boxes[index],
                    ))
                current_cells = [cell for cell in native_amounts
                                 if (cell["bbox"][0] + cell["bbox"][2]) / 2 <
                                 column_separator]
                prior_cells = [cell for cell in native_amounts
                               if (cell["bbox"][0] + cell["bbox"][2]) / 2 >
                               column_separator]
                if len(current_cells) != 1 or len(prior_cells) != 1:
                    raise OperatingCashflowSubtotalsNotLocatedError(
                        f"{label} amount cells do not fall uniquely under the current/prior period headers")
                matches.append({
                    "page": page["physicalPage"],
                    "table_index": table_index,
                    "row_index": row_index,
                    "label": label,
                    "label_bbox": label_box,
                    "current_cell": current_cells[0],
                    "prior_cell": prior_cells[0],
                })
    if len(matches) != 1:
        raise OperatingCashflowSubtotalsNotLocatedError(
            f"{label} native subtotal row is missing or ambiguous")
    return matches[0]


def _public_cell(cell: Mapping[str, object]) -> dict[str, object]:
    return {
        key: (str(cell[key]) if key == "amount" else cell[key])
        for key in ("table_index", "row_index", "cell_index", "text", "amount", "bbox", "chars")
    }


def extract_operating_cashflow_subtotals_from_pages(
    pages: Sequence[Mapping[str, object]], *, instrument_id: str,
    period_end: date, section: object, include_primary_row: bool = False,
) -> OperatingCashflowSubtotals:
    """Select both cash-flow subtotal rows from saved native page observations."""

    if not re.fullmatch(r"\d{6}", instrument_id):
        raise OperatingCashflowSubtotalsNotLocatedError(
            "instrument id is not a six-digit CNINFO code")
    if (_get(section, "role") != "合并" or
            _get(section, "statement") != "现金流量表"):
        raise OperatingCashflowSubtotalsNotLocatedError(
            "fallback requires the consolidated cash-flow statement section")
    start_page = _get(section, "start_page", "startPage")
    end_page = _get(section, "end_page", "endPage")
    if (not isinstance(start_page, int) or not isinstance(end_page, int) or
            start_page < 1 or end_page < start_page):
        raise OperatingCashflowSubtotalsNotLocatedError(
            "cash-flow statement section page range is invalid")
    selected_pages = sorted(
        (page for page in pages
         if isinstance(page.get("physicalPage"), int) and
         start_page <= page["physicalPage"] <= end_page),
        key=lambda page: page["physicalPage"],
    )
    if (not selected_pages or
            len({page["physicalPage"] for page in selected_pages}) != len(selected_pages)):
        raise OperatingCashflowSubtotalsNotLocatedError(
            "native pages do not uniquely cover the cash-flow section")
    lines_by_page = {
        page["physicalPage"]: _native_lines(page)
        for page in selected_pages
    }
    lines = tuple(line for page in selected_pages
                  for line in lines_by_page[page["physicalPage"]])
    headings = [
        line for line in lines
        if _statement_heading_kind(line) == ("合并", "现金流量表")
    ]
    if len(headings) != 1:
        raise OperatingCashflowSubtotalsNotLocatedError(
            "no unique standalone consolidated cash-flow statement heading")
    heading = headings[0]
    page_scopes = _statement_page_scopes(
        selected_pages, lines_by_page, heading=heading,
    )
    if not page_scopes:
        raise OperatingCashflowSubtotalsNotLocatedError(
            "native statement scope does not include the consolidated heading page")
    expected_current = _expected_period_label(period_end)
    expected_prior = (expected_current[0] - 1, expected_current[1])
    header_candidates = []
    for line in lines:
        if line.page != heading.page or line.top <= heading.top:
            continue
        dated = [(token, _period_token(token)) for token in line.tokens]
        current_tokens = [token for token, period in dated if period == expected_current]
        prior_tokens = [token for token, period in dated if period == expected_prior]
        if ("项目" in _compact(line.text) and len(current_tokens) == 1 and
                len(prior_tokens) == 1 and current_tokens[0].center < prior_tokens[0].center):
            header_candidates.append((line, current_tokens[0], prior_tokens[0]))
    if len(header_candidates) != 1:
        raise OperatingCashflowSubtotalsNotLocatedError(
            "explicit current and prior period headers are not uniquely located")
    header_line, current_header, prior_header = header_candidates[0]
    column_separator = (current_header.center + prior_header.center) / 2
    units = [(line, match.group(1)) for line in lines
             if line.page == heading.page and heading.top < line.top < header_line.top
             for match in [_UNIT.search(_compact(line.text))] if match is not None]
    if len(units) != 1:
        raise OperatingCashflowSubtotalsNotLocatedError(
            "cash-flow statement unit is not uniquely declared beside its header")
    unit_line, unit = units[0]
    section_unit = _get(section, "unit", "amount_unit")
    if section_unit != unit or unit not in _UNIT_SCALE:
        raise OperatingCashflowSubtotalsNotLocatedError(
            "native unit declaration differs from the consolidated section unit")

    report_period = (expected_current[0], "半年度报告" if expected_current[1] == "H1"
                     else "年度报告")
    report_titles = {}
    for line in lines:
        title = _compact(line.text)
        match = _REPORT_PERIOD.search(title)
        if match is not None:
            report_titles.setdefault(line.page, []).append((title, int(match.group(1)), match.group(2)))
    heading_titles = [item for item in report_titles.get(heading.page, ())
                      if (item[1], item[2]) == report_period]
    if len(heading_titles) != 1:
        raise OperatingCashflowSubtotalsNotLocatedError(
            "report title does not uniquely match the requested statement period")
    report_title = heading_titles[0][0]

    inflow = _find_subtotal_row(
        selected_pages, label=_TARGET_LABELS[0],
        column_separator=column_separator, page_scopes=page_scopes,
    )
    outflow = _find_subtotal_row(
        selected_pages, label=_TARGET_LABELS[1],
        column_separator=column_separator, page_scopes=page_scopes,
    )
    primary = (_find_subtotal_row(
        selected_pages, label=_PRIMARY_CFO_LABEL,
        column_separator=column_separator, page_scopes=page_scopes,
    ) if include_primary_row else None)
    if (inflow["page"] < heading.page or outflow["page"] < heading.page or
            inflow["page"] > outflow["page"] or outflow["page"] - heading.page > 1 or
            outflow["page"] - inflow["page"] > 1):
        raise OperatingCashflowSubtotalsNotLocatedError(
            "operating subtotals are outside the current statement continuation")
    if primary is not None and (primary["page"] < heading.page or
                                primary["page"] - heading.page > 1):
        raise OperatingCashflowSubtotalsNotLocatedError(
            "primary net-CFO row is outside the current statement continuation")
    required_pages = {inflow["page"], outflow["page"]}
    if primary is not None:
        required_pages.add(primary["page"])
    for page_number in required_pages:
        titles = [item for item in report_titles.get(page_number, ())
                  if (item[1], item[2]) == report_period]
        if len(titles) != 1 or titles[0][0] != report_title:
            raise OperatingCashflowSubtotalsNotLocatedError(
                "subtotal row page does not share the statement report title")

    scale = _UNIT_SCALE[unit]
    inflow_current = inflow["current_cell"]["amount"]
    outflow_current = outflow["current_cell"]["amount"]
    inflow_prior = inflow["prior_cell"]["amount"]
    outflow_prior = outflow["prior_cell"]["amount"]
    current_yuan = (inflow_current - outflow_current) * scale
    prior_yuan = (inflow_prior - outflow_prior) * scale
    evidence = {
        "extractor_version": EXTRACTOR_VERSION,
        "instrument_id": instrument_id,
        "period_end": period_end.isoformat(),
        "role": "合并",
        "statement": "现金流量表",
        "section": {
            "start_page": start_page,
            "end_page": end_page,
            "amount_unit": section_unit,
        },
        "statement_heading": _line_evidence(heading),
        "statement_page_scopes": list(page_scopes.values()),
        "report_title": report_title,
        "amount_unit": unit,
        "unit_declaration": _line_evidence(unit_line),
        "current_header": current_header.as_evidence(),
        "prior_header": prior_header.as_evidence(),
        "header_line": _line_evidence(header_line),
        "operating_cash_inflow_subtotal": {
            "label": inflow["label"],
            "page": inflow["page"],
            "table_index": inflow["table_index"],
            "row_index": inflow["row_index"],
            "label_cell_bbox": inflow["label_bbox"],
            "statement_page_scope": page_scopes[inflow["page"]],
            "current_cell": _public_cell(inflow["current_cell"]),
            "prior_cell": _public_cell(inflow["prior_cell"]),
        },
        "operating_cash_outflow_subtotal": {
            "label": outflow["label"],
            "page": outflow["page"],
            "table_index": outflow["table_index"],
            "row_index": outflow["row_index"],
            "label_cell_bbox": outflow["label_bbox"],
            "statement_page_scope": page_scopes[outflow["page"]],
            "current_cell": _public_cell(outflow["current_cell"]),
            "prior_cell": _public_cell(outflow["prior_cell"]),
        },
        "current_recomputed_yuan": str(current_yuan),
        "prior_recomputed_yuan": str(prior_yuan),
    }
    if primary is not None:
        evidence["primary_net_cfo_row"] = {
            "label": primary["label"],
            "page": primary["page"],
            "table_index": primary["table_index"],
            "row_index": primary["row_index"],
            "label_cell_bbox": primary["label_bbox"],
            "statement_page_scope": page_scopes[primary["page"]],
            "current_cell": _public_cell(primary["current_cell"]),
            "prior_cell": _public_cell(primary["prior_cell"]),
        }
    return OperatingCashflowSubtotals(evidence, current_yuan, prior_yuan)


def _pdf_page_observation(page: object, *, physical_page: int) -> dict[str, object]:
    try:
        width, height = float(page.width), float(page.height)
        chars = tuple(page.chars)
        tables = []
        for table_index, table in enumerate(page.find_tables()):
            extracted_rows = table.extract()
            rows = []
            for row_index, row in enumerate(table.rows):
                cell_boxes = [None if box is None else [float(value) for value in box]
                              for box in row.cells]
                native_cells = (extracted_rows[row_index]
                                if row_index < len(extracted_rows) else [])
                present = [box for box in cell_boxes if box is not None]
                row_bbox = ([min(box[0] for box in present),
                             min(box[1] for box in present),
                             max(box[2] for box in present),
                             max(box[3] for box in present)] if present else None)
                rows.append({
                    "rowIndex": row_index,
                    "bbox": row_bbox,
                    "cellBboxes": cell_boxes,
                    "nativeCells": native_cells,
                })
            cells = [None if box is None else [float(value) for value in box]
                     for box in table.cells]
            present = [box for box in cells if box is not None]
            bbox = ([min(box[0] for box in present), min(box[1] for box in present),
                     max(box[2] for box in present), max(box[3] for box in present)]
                    if present else None)
            tables.append({
                "tableIndex": table_index,
                "bbox": bbox,
                "rowCount": len(rows),
                "columnCount": max((len(row["cellBboxes"]) for row in rows), default=0),
                "cells": cells,
                "rows": rows,
            })
        return {
            "physicalPage": physical_page,
            "width": width,
            "height": height,
            "chars": chars,
            "tables": tables,
        }
    except Exception as exc:
        raise OperatingCashflowSubtotalsNotLocatedError(
            f"native page reading failed: {type(exc).__name__}") from exc


def extract_operating_cashflow_subtotals(
    pdf_bytes: bytes, *, instrument_id: str, period_end: date, section: object,
    include_primary_row: bool = False,
) -> OperatingCashflowSubtotals:
    """Read only the consolidated cash-flow section from supplied PDF bytes."""

    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise OperatingCashflowSubtotalsNotLocatedError("input must be PDF bytes")
    start_page = _get(section, "start_page")
    end_page = _get(section, "end_page")
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - deployment dependency guard
        raise OperatingCashflowSubtotalsNotLocatedError(
            "pdfplumber is required for native character and cell reading") from exc
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if start_page < 1 or end_page > len(pdf.pages) or end_page < start_page:
                raise OperatingCashflowSubtotalsNotLocatedError(
                    "cash-flow statement page range is outside the PDF")
            pages = tuple(_pdf_page_observation(pdf.pages[index], physical_page=index + 1)
                          for index in range(start_page - 1, end_page))
    except OperatingCashflowSubtotalsNotLocatedError:
        raise
    except Exception as exc:
        raise OperatingCashflowSubtotalsNotLocatedError(
            f"native cash-flow section reading failed: {type(exc).__name__}") from exc
    return extract_operating_cashflow_subtotals_from_pages(
        pages, instrument_id=instrument_id, period_end=period_end, section=section,
        include_primary_row=include_primary_row,
    )


__all__ = [
    "EXTRACTOR_VERSION", "OperatingCashflowSubtotals",
    "OperatingCashflowSubtotalsNotLocatedError",
    "extract_operating_cashflow_subtotals",
    "extract_operating_cashflow_subtotals_from_pages",
]
