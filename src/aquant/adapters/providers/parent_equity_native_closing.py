"""Native closing-cell path for consolidated parent equity.

This is a deliberately narrow independent path. It reads only the supplied PDF
bytes, locates the current-period consolidated owners' equity-change table,
binds its parent-equity ``小计`` header to the literal closing row, and parses
the amount from that cell's native characters. It never receives a candidate
amount, field column, document code, or source-page list.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
import re
from typing import Mapping, Sequence


_BBOX_TOLERANCE = 1.0
_PAGE_EPSILON = 1e-6
_MAX_PAGE_GAP = 2
_UNIT_SCALE = {
    "元": Decimal(1),
    "千元": Decimal(1000),
    "万元": Decimal(10000),
    "百万元": Decimal(1000000),
}
_AMOUNT = re.compile(
    r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?|"
    r"\(\d{1,3}(?:,\d{3})+\.\d{1,2}\)"
)
_REPORT_TABLE_TITLE = re.compile(r"(?:[一二三四五六七八九十\d]+[、.．])?合并所有者权益变动表")
_PARENT_COMPANY_TABLE_TITLE = re.compile(r"(?:[一二三四五六七八九十\d]+[、.．])?母公司所有者权益变动表")
_PRIOR_AMOUNT_BOUNDARIES = frozenset(("上年金额", "上期金额", "上年同期金额", "上期同期金额"))
_CLOSING_LABEL = "四、本期期末余额"
_PARENT_GROUP = "归属于母公司所有者权益"
_SUBTOTAL = "小计"


class ParentEquityNativeCellNotLocated(ValueError):
    """The native table/cell contract is missing or ambiguous."""


@dataclass(frozen=True, slots=True)
class _NativeCell:
    bbox: tuple[float, float, float, float]
    text: str


@dataclass(frozen=True, slots=True)
class _NativeTable:
    bbox: tuple[float, float, float, float]
    cells: tuple[_NativeCell, ...]


@dataclass(frozen=True, slots=True)
class _NativePage:
    number: int
    bbox: tuple[float, float, float, float]
    chars: tuple[Mapping[str, object], ...]
    tables: tuple[_NativeTable, ...]


@dataclass(frozen=True, slots=True)
class ParentEquityNativeClosingCellProof:
    amount_yuan: Decimal
    display_amount: str
    unit: str
    period_end: date
    report_table_page: int
    closing_row_page: int
    report_table_title: str
    current_amount_header: str
    period_header: str
    unit_declaration: str
    parent_group_header: str
    subtotal_header: str
    closing_row_label: str
    header_subtotal_bbox: tuple[float, float, float, float]
    closing_cell_bbox: tuple[float, float, float, float]
    closing_row_bbox: tuple[float, float, float, float]
    native_cell_text: str

    def as_evidence(self) -> dict[str, object]:
        return {
            "path_kind": "native_parent_equity_change_table_closing_subtotal_cell",
            "period_end": self.period_end.isoformat(),
            "report_table_page": self.report_table_page,
            "closing_row_page": self.closing_row_page,
            "report_table_title": self.report_table_title,
            "current_amount_header": self.current_amount_header,
            "period_header": self.period_header,
            "unit_declaration": self.unit_declaration,
            "unit": self.unit,
            "parent_group_header": self.parent_group_header,
            "subtotal_header": self.subtotal_header,
            "closing_row_label": self.closing_row_label,
            "header_subtotal_bbox": list(self.header_subtotal_bbox),
            "closing_cell_bbox": list(self.closing_cell_bbox),
            "closing_row_bbox": list(self.closing_row_bbox),
            "native_cell_text": self.native_cell_text,
            "display_amount": self.display_amount,
            "amount_yuan": str(self.amount_yuan),
            "bbox_tolerance_points": _BBOX_TOLERANCE,
        }


def _compact(text: object) -> str:
    return re.sub(r"\s+", "", str(text or "")).replace("　", "")


def _tuple_bbox(value: object) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    try:
        values = tuple(float(part) for part in value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if len(values) != 4 or not all(map(lambda part: part == part and abs(part) != float("inf"), values)):
        return None
    x0, top, x1, bottom = values
    if x1 <= x0 or bottom <= top:
        return None
    return values  # type: ignore[return-value]


def _char_bbox(char: Mapping[str, object]) -> tuple[float, float, float, float] | None:
    try:
        return _tuple_bbox((char["x0"], char["top"], char["x1"], char["bottom"]))
    except KeyError:
        return None


def _inside_page(bbox: tuple[float, float, float, float], page_bbox: tuple[float, float, float, float]) -> bool:
    x0, top, x1, bottom = bbox
    left, page_top, right, page_bottom = page_bbox
    return (x0 >= left - _PAGE_EPSILON and top >= page_top - _PAGE_EPSILON
            and x1 <= right + _PAGE_EPSILON and bottom <= page_bottom + _PAGE_EPSILON)


def _valid_chars(page: _NativePage) -> tuple[Mapping[str, object], ...]:
    return tuple(char for char in page.chars
                 if _char_bbox(char) is not None and _inside_page(_char_bbox(char), page.bbox))  # type: ignore[arg-type]


def _char_lines(chars: Sequence[Mapping[str, object]]) -> tuple[tuple[float, str], ...]:
    ordered = sorted((char for char in chars if _char_bbox(char) is not None),
                     key=lambda char: (float(char["top"]), float(char["x0"])))
    lines: list[list[Mapping[str, object]]] = []
    for char in ordered:
        top = float(char["top"])
        if not lines or abs(top - float(lines[-1][0]["top"])) > 1.0:
            lines.append([char])
        else:
            lines[-1].append(char)
    return tuple((min(float(char["top"]) for char in line),
                  "".join(str(char.get("text", "")) for char in sorted(line, key=lambda item: float(item["x0"]))))
                 for line in lines)


def _cell_text(chars: Sequence[Mapping[str, object]], bbox: tuple[float, float, float, float]) -> str:
    x0, top, x1, bottom = bbox
    inside = []
    for char in chars:
        char_bbox = _char_bbox(char)
        if char_bbox is None:
            continue
        cx = (char_bbox[0] + char_bbox[2]) / 2
        cy = (char_bbox[1] + char_bbox[3]) / 2
        if x0 - _PAGE_EPSILON <= cx <= x1 + _PAGE_EPSILON and top - _PAGE_EPSILON <= cy <= bottom + _PAGE_EPSILON:
            inside.append(char)
    return "\n".join(text for _, text in _char_lines(inside))


def _cell_chars_in_bounds(page: _NativePage, bbox: tuple[float, float, float, float]) -> tuple[Mapping[str, object], ...] | None:
    """Return cell-local native chars only when every used glyph is within 1 pt."""
    x0, top, x1, bottom = bbox
    selected = []
    for char in _valid_chars(page):
        char_bbox = _char_bbox(char)
        if char_bbox is None:
            continue
        cx = (char_bbox[0] + char_bbox[2]) / 2
        cy = (char_bbox[1] + char_bbox[3]) / 2
        if not (x0 <= cx <= x1 and top <= cy <= bottom):
            continue
        if (char_bbox[0] < x0 - _BBOX_TOLERANCE
                or char_bbox[1] < top - _BBOX_TOLERANCE
                or char_bbox[2] > x1 + _BBOX_TOLERANCE
                or char_bbox[3] > bottom + _BBOX_TOLERANCE):
            if not str(char.get("text", "")).strip():
                continue
            return None
        selected.append(char)
    return tuple(selected)


def _page_cell_text(page: _NativePage, bbox: tuple[float, float, float, float]) -> str | None:
    chars = _cell_chars_in_bounds(page, bbox)
    return None if chars is None else "\n".join(text for _, text in _char_lines(chars))


def _native_table_from_pdf(table: object, chars: Sequence[Mapping[str, object]],
                           page_bbox: tuple[float, float, float, float]) -> _NativeTable | None:
    raw_bbox = _tuple_bbox(getattr(table, "bbox", None))
    if raw_bbox is None or not _inside_page(raw_bbox, page_bbox):
        return None
    native_cells: dict[tuple[float, float, float, float], _NativeCell] = {}
    for row in getattr(table, "rows", ()):
        for raw_cell_bbox in getattr(row, "cells", ()):
            bbox = _tuple_bbox(raw_cell_bbox)
            if bbox is None or not _inside_page(bbox, page_bbox):
                continue
            native_cells.setdefault(bbox, _NativeCell(bbox, _cell_text(chars, bbox)))
    if not native_cells:
        return None
    return _NativeTable(raw_bbox, tuple(native_cells.values()))


def _native_page_from_pdf(number: int, page: object) -> _NativePage:
    bounds = [_tuple_bbox(getattr(page, attr, None)) for attr in ("bbox", "mediabox", "cropbox")]
    bounds = [bbox for bbox in bounds if bbox is not None]
    if not bounds:
        raise ParentEquityNativeCellNotLocated("PDF page has no usable MediaBox/CropBox bounds")
    # pdfplumber exposes these boxes in the same top-down coordinate system as
    # its native chars. Intersecting them makes every used char/row page-local.
    legal_bbox = (max(box[0] for box in bounds), max(box[1] for box in bounds),
                  min(box[2] for box in bounds), min(box[3] for box in bounds))
    if legal_bbox[2] <= legal_bbox[0] or legal_bbox[3] <= legal_bbox[1]:
        raise ParentEquityNativeCellNotLocated("PDF page has an empty MediaBox/CropBox intersection")
    chars = tuple(dict(char) for char in getattr(page, "chars", ()))
    page_text = "".join(text for _, text in _char_lines(
        tuple(char for char in chars if _char_bbox(char) and _inside_page(_char_bbox(char), legal_bbox))))  # type: ignore[arg-type]
    compact_text = _compact(page_text)
    tables: tuple[_NativeTable, ...] = ()
    if any(token in compact_text for token in ("合并所有者权益变动表", "归属于母公司所有者权益", "四、本期期末")):
        try:
            found = page.find_tables({"vertical_strategy": "lines", "horizontal_strategy": "lines"})
        except Exception as exc:
            raise ParentEquityNativeCellNotLocated(
                f"native ruled-table locator failed on page {number}: {type(exc).__name__}") from exc
        tables = tuple(native for table in found
                       if (native := _native_table_from_pdf(table, chars, legal_bbox)) is not None)
    return _NativePage(number, legal_bbox, chars, tables)


def _period_header_matches(text: str, period_end: date) -> bool:
    compact = _compact(text)
    year = period_end.year
    if (period_end.month, period_end.day) == (6, 30):
        return compact in {f"{year}年半年度", f"{year}年上半年", f"{year}年01-06月", f"{year}年1-6月"}
    if (period_end.month, period_end.day) == (12, 31):
        return compact in {f"{year}年年度", f"{year}年度"}
    return compact in {f"{year}年{period_end.month}月{period_end.day}日"}


def _unit_from_lines(lines: Sequence[tuple[float, str]]) -> tuple[str, str] | None:
    found: list[tuple[str, str]] = []
    for _, line in lines:
        compact = _compact(line)
        match = re.fullmatch(r"单位(?:为)?[:：]?(?:人民币)?(百万元|千元|万元|元)", compact)
        if match:
            found.append((compact, match.group(1)))
    if len(found) != 1:
        return None
    return found[0]


def _cell_inside_page(cell: _NativeCell, page: _NativePage) -> bool:
    return _inside_page(cell.bbox, page.bbox)


def _header_subtotal_cells(page: _NativePage, period_end: date) -> tuple[tuple[_NativeCell, str, str, str, str], ...]:
    chars = _valid_chars(page)
    lines = _char_lines(chars)
    title_lines = [(top, _compact(line)) for top, line in lines if _REPORT_TABLE_TITLE.fullmatch(_compact(line))]
    current_lines = [(top, _compact(line)) for top, line in lines if _compact(line) == "本期金额"]
    period_lines = [(top, _compact(line)) for top, line in lines if _period_header_matches(line, period_end)]
    unit = _unit_from_lines(lines)
    if len(title_lines) != 1 or len(current_lines) != 1 or len(period_lines) != 1 or unit is None:
        return ()
    title_top, title_text = title_lines[0]
    current_top, current_text = current_lines[0]
    period_top, period_text = period_lines[0]
    unit_text, unit_name = unit
    if not (title_top < current_top < period_top):
        return ()
    if not any(current_top < top < period_top and _compact(line) == unit_text for top, line in lines):
        return ()

    matches: dict[tuple[float, float, float, float], tuple[_NativeCell, str, str, str, str]] = {}
    for table in page.tables:
        cells = [cell for cell in table.cells if _cell_inside_page(cell, page)]
        parents = [cell for cell in cells
                   if _compact(_page_cell_text(page, cell.bbox) or "") == _PARENT_GROUP]
        minority_groups = [cell for cell in cells
                           if _compact(_page_cell_text(page, cell.bbox) or "") == "少数股东权益"]
        subtotals = [cell for cell in cells
                     if _compact(_page_cell_text(page, cell.bbox) or "") == _SUBTOTAL]
        for parent in parents:
            parent_left, parent_top, parent_right, parent_bottom = parent.bbox
            projection_rights = {
                minority.bbox[0]
                for minority in minority_groups
                if (minority.bbox[0] > parent_left
                    and minority.bbox[0] >= parent_right - _BBOX_TOLERANCE
                    and minority.bbox[1] <= parent_bottom + 50
                    and minority.bbox[3] >= parent_top - _BBOX_TOLERANCE)
            }
            if len(projection_rights) != 1:
                continue
            projection_right = next(iter(projection_rights))
            for subtotal in subtotals:
                sub_left, sub_top, sub_right, _ = subtotal.bbox
                if (sub_left < parent_left - _BBOX_TOLERANCE
                        or parent_right < sub_left - _BBOX_TOLERANCE
                        or sub_right > projection_right + _BBOX_TOLERANCE):
                    continue
                if sub_top < parent_top - _BBOX_TOLERANCE or sub_top > parent_bottom + 50:
                    continue
                matches.setdefault(subtotal.bbox, (subtotal, title_text, period_text, unit_name, unit_text))
    return tuple(matches.values())


def _label_bbox_candidates(table: _NativeTable, subtotal_bbox: tuple[float, float, float, float]) -> tuple[tuple[float, float, float, float], ...]:
    cells = [cell for cell in table.cells
             if cell.bbox[2] <= subtotal_bbox[0] + _BBOX_TOLERANCE]
    candidates: set[tuple[float, float, float, float]] = set()
    for cell in cells:
        text = _compact(cell.text)
        if text == _CLOSING_LABEL:
            candidates.add(cell.bbox)
        if text not in ("四、本期期末", "余额"):
            continue
        if text == "四、本期期末":
            for following in cells:
                following_text = _compact(following.text)
                if following_text != "余额":
                    continue
                if (abs(cell.bbox[0] - following.bbox[0]) <= _BBOX_TOLERANCE
                        and abs(cell.bbox[2] - following.bbox[2]) <= _BBOX_TOLERANCE
                        and abs(cell.bbox[3] - following.bbox[1]) <= _BBOX_TOLERANCE):
                    candidates.add((cell.bbox[0], cell.bbox[1], cell.bbox[2], following.bbox[3]))
    return tuple(candidates)


def _closing_amount_candidates(page: _NativePage, header_cell: _NativeCell,
                               header_page: _NativePage,
                               scope_pages: Sequence[_NativePage]) -> tuple[tuple[_NativeCell, tuple[float, float, float, float], str, Decimal], ...]:
    candidates: dict[tuple[tuple[float, float, float, float], str],
                     tuple[_NativeCell, tuple[float, float, float, float], str, Decimal]] = {}
    for table in page.tables:
        for label_bbox in _label_bbox_candidates(table, header_cell.bbox):
            if _scope_boundary_precedes_closing(
                    page, label_bbox, header_cell, header_page, scope_pages):
                continue
            for cell in table.cells:
                if not _cell_inside_page(cell, page):
                    continue
                if any(abs(cell.bbox[index] - header_cell.bbox[index]) > _BBOX_TOLERANCE
                       for index in (0, 2)):
                    continue
                if cell.bbox[0] <= label_bbox[2] - _BBOX_TOLERANCE:
                    continue
                if (cell.bbox[1] > label_bbox[1] + _BBOX_TOLERANCE
                        or cell.bbox[3] < label_bbox[3] - _BBOX_TOLERANCE):
                    continue
                native_text = _page_cell_text(page, cell.bbox)
                if native_text is None:
                    continue
                display = _compact(native_text)
                if not _AMOUNT.fullmatch(display):
                    continue
                try:
                    parsed = Decimal(display.replace(",", ""))
                except InvalidOperation:
                    continue
                if not parsed.is_finite():
                    continue
                key = (cell.bbox, display)
                candidates.setdefault(key, (_NativeCell(cell.bbox, native_text), label_bbox, display, parsed))
    return tuple(candidates.values())


def _scope_boundary_precedes_closing(
    closing_page: _NativePage,
    label_bbox: tuple[float, float, float, float],
    header_cell: _NativeCell,
    header_page: _NativePage,
    scope_pages: Sequence[_NativePage],
) -> bool:
    """Stop a current-table search at a prior-period or parent-only table boundary."""
    header_title_tops = [top for top, line in _char_lines(_valid_chars(header_page))
                         if _REPORT_TABLE_TITLE.fullmatch(_compact(line))]
    if len(header_title_tops) != 1:
        return True
    for scoped_page in scope_pages:
        if not (header_page.number <= scoped_page.number <= closing_page.number):
            continue
        for top, line in _char_lines(_valid_chars(scoped_page)):
            compact = _compact(line)
            is_boundary = (compact in _PRIOR_AMOUNT_BOUNDARIES
                           or _PARENT_COMPANY_TABLE_TITLE.fullmatch(compact) is not None)
            if not is_boundary:
                continue
            if scoped_page.number == header_page.number:
                # Ignore earlier content on the same page; scope begins at this table title/header.
                if top <= max(header_title_tops[0], header_cell.bbox[1]):
                    continue
            if (scoped_page.number < closing_page.number
                    or top < label_bbox[1] - _BBOX_TOLERANCE):
                return True
    return False


def derive_parent_equity_native_closing_cell(
    pages: Sequence[_NativePage], *, period_end: date,
) -> ParentEquityNativeClosingCellProof:
    """Derive the current-period parent subtotal from its literal native cell."""
    header_options: list[tuple[_NativePage, _NativeCell, str, str, str, str]] = []
    for page in pages:
        for cell, title, period, unit, unit_declaration in _header_subtotal_cells(page, period_end):
            header_options.append((page, cell, title, period, unit, unit_declaration))
    if len(header_options) != 1:
        raise ParentEquityNativeCellNotLocated(
            "current consolidated equity-change table/header is missing or ambiguous")

    header_page, header_cell, title, period_header, unit, unit_declaration = header_options[0]
    unit_scale = _UNIT_SCALE[unit]
    closing_matches: list[tuple[_NativePage, _NativeCell, tuple[float, float, float, float], str, Decimal]] = []
    for page in pages:
        if not (header_page.number <= page.number <= header_page.number + _MAX_PAGE_GAP):
            continue
        scope_pages = tuple(scoped for scoped in pages
                            if header_page.number <= scoped.number <= page.number)
        for cell, label_bbox, display, parsed in _closing_amount_candidates(
                page, header_cell, header_page, scope_pages):
            closing_matches.append((page, cell, label_bbox, display, parsed * unit_scale))
    if len(closing_matches) != 1:
        raise ParentEquityNativeCellNotLocated(
            "the current table has no unique in-bounds closing subtotal cell within two pages")

    closing_page, closing_cell, label_bbox, display, amount_yuan = closing_matches[0]
    return ParentEquityNativeClosingCellProof(
        amount_yuan=amount_yuan,
        display_amount=display,
        unit=unit,
        period_end=period_end,
        report_table_page=header_page.number,
        closing_row_page=closing_page.number,
        report_table_title=title,
        current_amount_header="本期金额",
        period_header=period_header,
        unit_declaration=unit_declaration,
        parent_group_header=_PARENT_GROUP,
        subtotal_header=_SUBTOTAL,
        closing_row_label=_CLOSING_LABEL,
        header_subtotal_bbox=header_cell.bbox,
        closing_cell_bbox=closing_cell.bbox,
        closing_row_bbox=label_bbox,
        native_cell_text=closing_cell.text,
    )


def extract_parent_equity_native_closing_cell(
    pdf_bytes: bytes, *, period_end: date,
) -> ParentEquityNativeClosingCellProof:
    """Read the proof from the exact supplied PDF bytes, without caller metadata."""
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise ParentEquityNativeCellNotLocated("input must be PDF bytes")
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - environment guard
        raise ParentEquityNativeCellNotLocated("pdfplumber is unavailable") from exc
    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            pages = tuple(_native_page_from_pdf(index + 1, page)
                          for index, page in enumerate(pdf.pages))
    except ParentEquityNativeCellNotLocated:
        raise
    except Exception as exc:
        raise ParentEquityNativeCellNotLocated(
            f"native PDF read failed: {type(exc).__name__}") from exc
    return derive_parent_equity_native_closing_cell(pages, period_end=period_end)


__all__ = [
    "ParentEquityNativeCellNotLocated",
    "ParentEquityNativeClosingCellProof",
    "derive_parent_equity_native_closing_cell",
    "extract_parent_equity_native_closing_cell",
]
