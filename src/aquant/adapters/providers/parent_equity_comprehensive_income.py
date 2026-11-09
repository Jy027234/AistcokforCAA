"""Narrow native-cell proof for attributable net profit from the equity table."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
import re
from typing import Sequence

try:
    from .parent_equity_native_closing import (
        ParentEquityNativeCellNotLocated,
        _AMOUNT,
        _BBOX_TOLERANCE,
        _NativeCell,
        _NativePage,
        _NativeTable,
        _PARENT_GROUP,
        _SUBTOTAL,
        _UNIT_SCALE,
        _cell_inside_page,
        _char_bbox,
        _compact,
        _header_subtotal_cells,
        _native_page_from_pdf,
        _page_cell_text,
        _scope_boundary_precedes_closing,
        _valid_chars,
    )
except ImportError:  # direct module import in the isolated fixture tests
    from parent_equity_native_closing import (
        ParentEquityNativeCellNotLocated,
        _AMOUNT,
        _BBOX_TOLERANCE,
        _NativeCell,
        _NativePage,
        _NativeTable,
        _PARENT_GROUP,
        _SUBTOTAL,
        _UNIT_SCALE,
        _cell_inside_page,
        _char_bbox,
        _compact,
        _header_subtotal_cells,
        _native_page_from_pdf,
        _page_cell_text,
        _scope_boundary_precedes_closing,
        _valid_chars,
    )


_OTHER_COMPREHENSIVE = "其他综合收益"
_COMPREHENSIVE_ROW = "（一）综合收益总额"
_PARENT_COMPANY = "少数股东权益"
_GEOMETRY_EPSILON = 0.05


class ParentComprehensiveIncomeNotLocated(ValueError):
    """The current parent-attributable comprehensive-income row is not unique."""


@dataclass(frozen=True, slots=True)
class ParentComprehensiveIncomeProof:
    period_end: date
    report_table_page: int
    row_page: int
    report_table_title: str
    current_amount_header: str
    period_header: str
    unit_declaration: str
    unit: str
    parent_group_header: str
    comprehensive_income_row: str
    subtotal_header: str
    other_comprehensive_header: str
    parent_group_bbox: tuple[float, float, float, float]
    parent_projection_bbox: tuple[float, float, float, float]
    subtotal_header_cell_bbox: tuple[float, float, float, float]
    subtotal_header_glyph_bbox: tuple[float, float, float, float]
    other_comprehensive_header_cell_bbox: tuple[float, float, float, float]
    other_comprehensive_header_glyph_bbox: tuple[float, float, float, float]
    row_label_bbox: tuple[float, float, float, float]
    subtotal_cell_bbox: tuple[float, float, float, float]
    other_comprehensive_cell_bbox: tuple[float, float, float, float]
    subtotal_cell_text: str
    other_comprehensive_cell_text: str
    subtotal_amount_yuan: Decimal
    other_comprehensive_amount_yuan: Decimal
    net_profit_attributable_yuan: Decimal
    subtotal_display_amount: str
    other_comprehensive_display_amount: str

    def as_evidence(self) -> dict[str, object]:
        return {
            "path_kind": "native_parent_equity_comprehensive_income_minus_oci",
            "accounting_relation": (
                "归母净利润 = 归母综合收益总额 - 归母其他综合收益（税后净额）"
            ),
            "accounting_basis": (
                "财政部《企业会计准则第30号——财务报表列报》：综合收益总额由净利润与"
                "扣除所得税影响后的其他综合收益构成，合并报表区分母公司所有者与少数股东份额。"
            ),
            "period_end": self.period_end.isoformat(),
            "report_table_page": self.report_table_page,
            "row_page": self.row_page,
            "report_table_title": self.report_table_title,
            "current_amount_header": self.current_amount_header,
            "period_header": self.period_header,
            "unit_declaration": self.unit_declaration,
            "unit": self.unit,
            "parent_group_header": self.parent_group_header,
            "comprehensive_income_row": self.comprehensive_income_row,
            "subtotal_header": self.subtotal_header,
            "other_comprehensive_header": self.other_comprehensive_header,
            "parent_group_bbox": list(self.parent_group_bbox),
            "parent_projection_bbox": list(self.parent_projection_bbox),
            "subtotal_header_cell_bbox": list(self.subtotal_header_cell_bbox),
            "subtotal_header_glyph_bbox": list(self.subtotal_header_glyph_bbox),
            "other_comprehensive_header_cell_bbox": list(self.other_comprehensive_header_cell_bbox),
            "other_comprehensive_header_glyph_bbox": list(self.other_comprehensive_header_glyph_bbox),
            "row_label_bbox": list(self.row_label_bbox),
            "subtotal_cell_bbox": list(self.subtotal_cell_bbox),
            "other_comprehensive_cell_bbox": list(self.other_comprehensive_cell_bbox),
            "subtotal_cell_text": self.subtotal_cell_text,
            "other_comprehensive_cell_text": self.other_comprehensive_cell_text,
            "subtotal_display_amount": self.subtotal_display_amount,
            "other_comprehensive_display_amount": self.other_comprehensive_display_amount,
            "subtotal_amount_yuan": str(self.subtotal_amount_yuan),
            "other_comprehensive_amount_yuan": str(self.other_comprehensive_amount_yuan),
            "net_profit_attributable_yuan": str(self.net_profit_attributable_yuan),
            "bbox_tolerance_points": _BBOX_TOLERANCE,
        }


def _text_boxes(
    page: _NativePage,
    table: _NativeTable,
    expected: str,
    *,
    top_min: float | None = None,
    top_max: float | None = None,
    x_min: float | None = None,
    x_max: float | None = None,
) -> tuple[tuple[float, float, float, float], ...]:
    """Find exact text in native cells or vertically contiguous same-column cells."""
    groups: dict[tuple[float, float], list[_NativeCell]] = {}
    for cell in table.cells:
        if not _cell_inside_page(cell, page):
            continue
        x0, top, x1, bottom = cell.bbox
        if top_min is not None and bottom < top_min - _BBOX_TOLERANCE:
            continue
        if top_max is not None and top > top_max + _BBOX_TOLERANCE:
            continue
        if x_min is not None and x0 < x_min - _BBOX_TOLERANCE:
            continue
        if x_max is not None and x1 > x_max + _BBOX_TOLERANCE:
            continue
        groups.setdefault((round(x0, 2), round(x1, 2)), []).append(cell)

    found: set[tuple[float, float, float, float]] = set()
    for (x0, x1), cells in groups.items():
        cells.sort(key=lambda cell: (cell.bbox[1], cell.bbox[3]))
        for start in range(len(cells)):
            for end in range(start, len(cells)):
                if end > start and abs(cells[end - 1].bbox[3] - cells[end].bbox[1]) > _GEOMETRY_EPSILON:
                    break
                bbox = (x0, cells[start].bbox[1], x1, cells[end].bbox[3])
                if top_max is not None and bbox[3] > top_max + _BBOX_TOLERANCE:
                    continue
                if _compact(_page_cell_text(page, bbox) or "") == expected:
                    found.add(bbox)

    # Different extraction tables may expose a text row and a taller value-row
    # rectangle with the same x-range and top. Keep the tight native text box.
    canonical: dict[tuple[float, float, float], tuple[float, float, float, float]] = {}
    for bbox in found:
        key = (round(bbox[0], 2), round(bbox[2], 2), round(bbox[1], 2))
        previous = canonical.get(key)
        if previous is None or bbox[3] < previous[3]:
            canonical[key] = bbox
    return tuple(sorted(canonical.values()))


def _glyph_bbox(page: _NativePage, cell_bbox: tuple[float, float, float, float], expected: str) -> tuple[float, float, float, float]:
    if _compact(_page_cell_text(page, cell_bbox) or "") != expected:
        raise ParentComprehensiveIncomeNotLocated(f"native header cell does not read {expected!r}")
    selected = []
    for char in _valid_chars(page):
        char_bbox = _char_bbox(char)
        if char_bbox is None:
            continue
        cx = (char_bbox[0] + char_bbox[2]) / 2
        cy = (char_bbox[1] + char_bbox[3]) / 2
        x0, top, x1, bottom = cell_bbox
        if x0 <= cx <= x1 and top <= cy <= bottom and str(char.get("text", "")).strip():
            selected.append(char_bbox)
    if not selected:
        raise ParentComprehensiveIncomeNotLocated(f"native glyphs for {expected!r} are missing")
    return (min(box[0] for box in selected), min(box[1] for box in selected),
            max(box[2] for box in selected), max(box[3] for box in selected))


def _amount_in_cell(page: _NativePage, cell: _NativeCell) -> tuple[str, Decimal] | None:
    native_text = _page_cell_text(page, cell.bbox)
    if native_text is None:
        return None
    display = _compact(native_text)
    if not _AMOUNT.fullmatch(display):
        return None
    try:
        if display.startswith("(") and display.endswith(")"):
            amount = -Decimal(display[1:-1].replace(",", ""))
        else:
            amount = Decimal(display.replace(",", ""))
    except InvalidOperation:
        return None
    return (display, amount) if amount.is_finite() else None


def _table_for_header(page: _NativePage, header_cell: _NativeCell) -> _NativeTable:
    matches = [table for table in page.tables
               if any(cell.bbox == header_cell.bbox for cell in table.cells)]
    if len(matches) != 1:
        raise ParentComprehensiveIncomeNotLocated("current table/header grid is missing or duplicated")
    return matches[0]


def _parent_projection(page: _NativePage, table: _NativeTable) -> tuple[_NativeCell, _NativeCell, float, float]:
    parents = [cell for cell in table.cells
               if _compact(_page_cell_text(page, cell.bbox) or "") == _PARENT_GROUP]
    minorities = [cell for cell in table.cells
                  if _compact(_page_cell_text(page, cell.bbox) or "") == _PARENT_COMPANY]
    if len(parents) != 1 or len(minorities) != 1:
        raise ParentComprehensiveIncomeNotLocated("parent/minority group headers are missing or ambiguous")
    parent, minority = parents[0], minorities[0]
    left, _, group_right, _ = parent.bbox
    projection_right = minority.bbox[0]
    if (projection_right <= left
            or projection_right < group_right - _BBOX_TOLERANCE):
        raise ParentComprehensiveIncomeNotLocated("parent group has no unique adjacent minority boundary")
    return parent, minority, left, projection_right


def _unique_text_box(boxes: Sequence[tuple[float, float, float, float]], label: str) -> tuple[float, float, float, float]:
    if len(boxes) != 1:
        raise ParentComprehensiveIncomeNotLocated(f"{label} is missing or ambiguous")
    return boxes[0]


def derive_parent_comprehensive_income_minus_oci(
    pages: Sequence[_NativePage], *, period_end: date,
) -> ParentComprehensiveIncomeProof:
    """Derive the attributable-net-profit relationship from one current table row."""
    headers = [(page, cell, title, period, unit, unit_declaration)
               for page in pages
               for cell, title, period, unit, unit_declaration
               in _header_subtotal_cells(page, period_end)]
    if len(headers) != 1:
        raise ParentComprehensiveIncomeNotLocated("current consolidated equity-table header is missing or ambiguous")
    page, subtotal_header_cell, title, period, unit, unit_declaration = headers[0]
    table = _table_for_header(page, subtotal_header_cell)
    parent, _, projection_left, projection_right = _parent_projection(page, table)

    subtotal_header_text = _compact(_page_cell_text(page, subtotal_header_cell.bbox) or "")
    if subtotal_header_text != _SUBTOTAL:
        raise ParentComprehensiveIncomeNotLocated("parent subtotal header is not unique")
    subtotal_header_glyph = _glyph_bbox(page, subtotal_header_cell.bbox, _SUBTOTAL)

    other_header_box = _unique_text_box(
        _text_boxes(
            page, table, _OTHER_COMPREHENSIVE,
            top_min=parent.bbox[1], top_max=subtotal_header_cell.bbox[3],
            x_min=projection_left, x_max=projection_right),
        "parent other-comprehensive-income header")
    other_header_glyph = _glyph_bbox(page, other_header_box, _OTHER_COMPREHENSIVE)
    for glyph_bbox in (subtotal_header_glyph, other_header_glyph):
        if (glyph_bbox[0] < projection_left - _BBOX_TOLERANCE
                or glyph_bbox[2] > projection_right + _BBOX_TOLERANCE):
            raise ParentComprehensiveIncomeNotLocated("column header glyphs lie outside the parent group")

    row_label = _unique_text_box(
        _text_boxes(page, table, _COMPREHENSIVE_ROW),
        "same-table comprehensive-income row")
    if row_label[1] <= subtotal_header_cell.bbox[3] + _BBOX_TOLERANCE:
        raise ParentComprehensiveIncomeNotLocated("comprehensive-income row does not follow the current column header")
    if _scope_boundary_precedes_closing(
            page, row_label, subtotal_header_cell, page, (page,)):
        raise ParentComprehensiveIncomeNotLocated("a prior-period or parent-only table boundary precedes the row")

    row_cells: list[tuple[_NativeCell, str, Decimal]] = []
    for cell in table.cells:
        if not _cell_inside_page(cell, page):
            continue
        if abs(cell.bbox[1] - row_label[1]) > _BBOX_TOLERANCE:
            continue
        if cell.bbox[0] <= row_label[2] - _BBOX_TOLERANCE:
            continue
        if (cell.bbox[0] < projection_left - _BBOX_TOLERANCE
                or cell.bbox[2] > projection_right + _BBOX_TOLERANCE):
            continue
        parsed = _amount_in_cell(page, cell)
        if parsed is not None:
            row_cells.append((cell, parsed[0], parsed[1]))

    def column_amount(label_glyph_bbox: tuple[float, float, float, float], label: str):
        matches = [(cell, display, amount) for cell, display, amount in row_cells
                   if (label_glyph_bbox[0] >= cell.bbox[0] - _BBOX_TOLERANCE
                       and label_glyph_bbox[2] <= cell.bbox[2] + _BBOX_TOLERANCE)]
        unique = {(cell.bbox, display): (cell, display, amount)
                  for cell, display, amount in matches}
        if len(unique) != 1:
            raise ParentComprehensiveIncomeNotLocated(f"{label} row amount column is missing or ambiguous")
        return next(iter(unique.values()))

    subtotal_cell, subtotal_display, subtotal_raw = column_amount(subtotal_header_glyph, "parent subtotal")
    other_cell, other_display, other_raw = column_amount(other_header_glyph, "parent other comprehensive income")
    if subtotal_cell.bbox == other_cell.bbox:
        raise ParentComprehensiveIncomeNotLocated("the two parent components resolve to the same native cell")
    if not (subtotal_cell.bbox[2] < other_cell.bbox[0] + _BBOX_TOLERANCE
            or other_cell.bbox[2] < subtotal_cell.bbox[0] + _BBOX_TOLERANCE):
        raise ParentComprehensiveIncomeNotLocated("the two parent component columns overlap")

    scale = _UNIT_SCALE.get(unit)
    if scale is None:
        raise ParentComprehensiveIncomeNotLocated(f"unsupported amount unit {unit!r}")
    subtotal_yuan = subtotal_raw * scale
    other_yuan = other_raw * scale
    return ParentComprehensiveIncomeProof(
        period_end=period_end,
        report_table_page=page.number,
        row_page=page.number,
        report_table_title=title,
        current_amount_header="本期金额",
        period_header=period,
        unit_declaration=unit_declaration,
        unit=unit,
        parent_group_header=_PARENT_GROUP,
        comprehensive_income_row=_COMPREHENSIVE_ROW,
        subtotal_header=_SUBTOTAL,
        other_comprehensive_header=_OTHER_COMPREHENSIVE,
        parent_group_bbox=parent.bbox,
        parent_projection_bbox=(projection_left, parent.bbox[1], projection_right, parent.bbox[3]),
        subtotal_header_cell_bbox=subtotal_header_cell.bbox,
        subtotal_header_glyph_bbox=subtotal_header_glyph,
        other_comprehensive_header_cell_bbox=other_header_box,
        other_comprehensive_header_glyph_bbox=other_header_glyph,
        row_label_bbox=row_label,
        subtotal_cell_bbox=subtotal_cell.bbox,
        other_comprehensive_cell_bbox=other_cell.bbox,
        subtotal_cell_text=_page_cell_text(page, subtotal_cell.bbox) or "",
        other_comprehensive_cell_text=_page_cell_text(page, other_cell.bbox) or "",
        subtotal_amount_yuan=subtotal_yuan,
        other_comprehensive_amount_yuan=other_yuan,
        net_profit_attributable_yuan=subtotal_yuan - other_yuan,
        subtotal_display_amount=subtotal_display,
        other_comprehensive_display_amount=other_display,
    )


def extract_parent_comprehensive_income_minus_oci(
    pdf_bytes: bytes, *, period_end: date,
) -> ParentComprehensiveIncomeProof:
    """Independently derive the typed proof from the exact supplied PDF bytes."""
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise ParentComprehensiveIncomeNotLocated("input must be PDF bytes")
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - environment guard
        raise ParentComprehensiveIncomeNotLocated("pdfplumber is unavailable") from exc
    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            pages = tuple(_native_page_from_pdf(index + 1, source_page)
                          for index, source_page in enumerate(pdf.pages))
    except ParentComprehensiveIncomeNotLocated:
        raise
    except Exception as exc:
        raise ParentComprehensiveIncomeNotLocated(
            f"native PDF read failed: {type(exc).__name__}") from exc
    return derive_parent_comprehensive_income_minus_oci(pages, period_end=period_end)


__all__ = [
    "ParentComprehensiveIncomeNotLocated",
    "ParentComprehensiveIncomeProof",
    "derive_parent_comprehensive_income_minus_oci",
    "extract_parent_comprehensive_income_minus_oci",
]
