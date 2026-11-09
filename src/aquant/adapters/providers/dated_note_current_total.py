"""Typed reading of a consolidated note's own dated revenue total.

This is deliberately separate from the decomposition path: it reads the
literal current-period total from note 61, without inferring a missing
other-business amount or consulting the primary candidate amount.
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from .cninfo_statement_geometry import _frontmatter
from .cninfo_statement_sections import (
    ROLE_CONSOLIDATED, _NOTE_SCOPE_HEADING, _REVENUE_NOTE_HEADING,
    _compact as _section_compact,
)

EXTRACTOR_VERSION = "dated-note-current-total-v1"
_NOTE_61 = re.compile(r"^61[、．.]营业收入(?:和|、|及)营业成本(?:情况)?$")
_NEXT_NUMBERED_NOTE = re.compile(r"^(\d+)[、．.].+$")
_DECOMPOSITION_SUBTABLE = "营业收入、营业成本的分解信息"
_ANNUAL_TITLE = re.compile(r"(?<!\d)(20\d{2})(?:年年度|年度)报告(?:全文)?")
_UNIT = re.compile(r"^单位(?:为)?[:：]?(?:人民币)?(百万元|千元|万元|元)$")
_AMOUNT = re.compile(
    r"-?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d{1,2})?"
    r"|\((?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d{1,2})?\)"
)
_UNIT_SCALE = {"元": Decimal(1), "千元": Decimal(1000),
               "万元": Decimal(10000), "百万元": Decimal(1000000)}


def _compact(text: str | None) -> str:
    # Real merged header cells contain None. It denotes an absent label, never
    # a numeric zero; leave the production text normalizer unchanged.
    return _section_compact("" if text is None else text)


class DatedNoteCurrentTotalNotLocatedError(ValueError):
    """Note 61's own dated current revenue total is absent or ambiguous."""


@dataclass(frozen=True, slots=True)
class NativeGlyph:
    text: str
    bbox: tuple[float, float, float, float]

    def as_evidence(self) -> dict[str, object]:
        return {"text": self.text, "bbox": list(self.bbox)}


@dataclass(frozen=True, slots=True)
class NativeTextEvidence:
    text: str
    page: int
    bbox: tuple[float, float, float, float]
    glyphs: tuple[NativeGlyph, ...]

    def as_evidence(self) -> dict[str, object]:
        return {"text": self.text, "page": self.page, "bbox": list(self.bbox),
                "native_glyphs": [glyph.as_evidence() for glyph in self.glyphs]}


@dataclass(frozen=True, slots=True)
class NoteTableCellEvidence:
    role: str
    text: str
    page: int
    table_index: int
    row_index: int
    cell_index: int
    bbox: tuple[float, float, float, float]
    glyphs: tuple[NativeGlyph, ...]
    display_amount: Decimal | None = None

    def as_evidence(self) -> dict[str, object]:
        return {
            "role": self.role, "text": self.text, "page": self.page,
            "table_index": self.table_index, "row_index": self.row_index,
            "cell_index": self.cell_index, "cell_bbox": list(self.bbox),
            "native_glyphs": [glyph.as_evidence() for glyph in self.glyphs],
            "display_amount": (None if self.display_amount is None
                               else str(self.display_amount)),
        }


@dataclass(frozen=True, slots=True)
class DatedNoteCurrentTotalEvidence:
    pdf_sha256: str
    instrument_id: str
    period_end: date
    issuer_identity: tuple[tuple[int, str], ...]
    report_period_identity: tuple[tuple[int, str], ...]
    consolidated_scope_page: int
    consolidated_scope_heading: str
    note_heading: NativeTextEvidence
    note_boundary_kind: str
    note_boundary: NativeTextEvidence
    unit_declaration: NativeTextEvidence
    unit: str
    current_header: NoteTableCellEvidence
    prior_header: NoteTableCellEvidence
    current_revenue_header: NoteTableCellEvidence
    current_cost_header: NoteTableCellEvidence
    prior_revenue_header: NoteTableCellEvidence
    prior_cost_header: NoteTableCellEvidence
    total_row_label: NoteTableCellEvidence
    current_revenue_cell: NoteTableCellEvidence
    current_cost_cell: NoteTableCellEvidence
    prior_revenue_cell: NoteTableCellEvidence
    prior_cost_cell: NoteTableCellEvidence
    table_index: int
    table_bbox: tuple[float, float, float, float]
    row_count: int
    header_row_index: int
    subheader_row_index: int
    total_row_index: int
    current_revenue_yuan: Decimal

    def as_evidence(self) -> dict[str, object]:
        return {
            "extractor_version": EXTRACTOR_VERSION,
            "source_identity": {
                "instrument_id": self.instrument_id,
                "period_end": self.period_end.isoformat(),
                "pdf_sha256": self.pdf_sha256,
                "issuer_front_matter": [
                    {"page": page, "text": text}
                    for page, text in self.issuer_identity],
                "report_period_front_matter": [
                    {"page": page, "text": text}
                    for page, text in self.report_period_identity],
            },
            "note_scope": {
                "role": ROLE_CONSOLIDATED,
                "scope_heading_page": self.consolidated_scope_page,
                "scope_heading": self.consolidated_scope_heading,
                "note_heading": self.note_heading.as_evidence(),
                "note_boundary": {
                    "kind": self.note_boundary_kind,
                    "evidence": self.note_boundary.as_evidence(),
                    "bounded_region": {
                        "top_exclusive": self.note_heading.bbox[3],
                        "bottom_exclusive": self.note_boundary.bbox[1],
                    },
                },
                "unit_declaration": self.unit_declaration.as_evidence(),
                "amount_unit": self.unit,
            },
            "period_binding": {
                "report_period_end": self.period_end.isoformat(),
                "report_year": self.period_end.year,
                "current_header": self.current_header.as_evidence(),
                "prior_header": self.prior_header.as_evidence(),
                "relationship": (
                    "The note itself labels 本期发生额 before 上期发生额; "
                    "the same original PDF front matter identifies the annual "
                    "report period_end."
                ),
            },
            "table": {
                "reader": "pdfplumber.Page.find_tables() default settings",
                "table_index": self.table_index,
                "default_table_bbox": list(self.table_bbox),
                "row_count": self.row_count,
                "header_row_index": self.header_row_index,
                "subheader_row_index": self.subheader_row_index,
                "total_row_index": self.total_row_index,
                "column_binding": (
                    "Native period/subheader centers map into actual default-table "
                    "total-row cell intervals, at least 1pt inside a unique cell; "
                    "this is not the primary ruled-grid path."
                ),
            },
            "headers": {
                "current_period": self.current_header.as_evidence(),
                "prior_period": self.prior_header.as_evidence(),
                "current_revenue": self.current_revenue_header.as_evidence(),
                "current_cost": self.current_cost_header.as_evidence(),
                "prior_revenue": self.prior_revenue_header.as_evidence(),
                "prior_cost": self.prior_cost_header.as_evidence(),
            },
            "total_row": {
                "label": self.total_row_label.as_evidence(),
                "current_revenue": self.current_revenue_cell.as_evidence(),
                "current_cost": self.current_cost_cell.as_evidence(),
                "prior_revenue": self.prior_revenue_cell.as_evidence(),
                "prior_cost": self.prior_cost_cell.as_evidence(),
                "current_revenue_yuan": str(self.current_revenue_yuan),
            },
        }


def _bbox(raw: Any) -> tuple[float, float, float, float] | None:
    if raw is None or len(raw) != 4:
        return None
    try:
        return tuple(float(value) for value in raw)
    except (TypeError, ValueError):
        return None


def _glyphs(chars: list[dict[str, Any]], bbox: tuple[float, ...]) -> tuple[NativeGlyph, ...]:
    x0, top, x1, bottom = bbox
    selected = []
    for char in chars:
        try:
            box = tuple(float(char[key]) for key in ("x0", "top", "x1", "bottom"))
        except (KeyError, TypeError, ValueError):
            continue
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        if x0 <= cx <= x1 and top <= cy <= bottom:
            selected.append((box[1], box[0], NativeGlyph(str(char.get("text", "")), box)))
    selected.sort(key=lambda item: (round(item[0], 1), item[1]))
    return tuple(item[2] for item in selected)


def _native_text(text: str, page_number: int, bbox: tuple[float, ...],
                 chars: list[dict[str, Any]]) -> NativeTextEvidence:
    glyphs = _glyphs(chars, bbox)
    if _compact("".join(glyph.text for glyph in glyphs)) != _compact(text):
        raise DatedNoteCurrentTotalNotLocatedError(
            f"native glyphs do not reproduce {text!r}")
    return NativeTextEvidence(text, page_number, bbox, glyphs)


def _native_literal_position(page: Any, page_number: int, text: str) -> NativeTextEvidence:
    compact = _compact(text)
    if not compact:
        raise DatedNoteCurrentTotalNotLocatedError("empty native boundary text")
    pattern = r"\s*".join(re.escape(char) for char in compact)
    hits = page.search(pattern)
    if len(hits) != 1:
        raise DatedNoteCurrentTotalNotLocatedError(
            f"native boundary text is not unique ({len(hits)} hits): {text!r}")
    box = tuple(float(hits[0][key]) for key in ("x0", "top", "x1", "bottom"))
    return _native_text(text, page_number, box, page.chars)


def _next_note61_boundary(
    page: Any, page_number: int, lines: list[tuple[int, str]],
    title_line_index: int, title_box: tuple[float, ...],
) -> tuple[str, NativeTextEvidence]:
    """Find the earliest same-page literal boundary after note 61's title."""
    candidates: list[tuple[float, float, str, NativeTextEvidence]] = []
    for candidate_page, raw_line in lines[title_line_index + 1:]:
        if candidate_page != page_number:
            continue
        compact = _compact(raw_line)
        if not compact:
            continue
        kinds: list[str] = []
        scope_match = _NOTE_SCOPE_HEADING.fullmatch(compact)
        numbered_match = _NEXT_NUMBERED_NOTE.fullmatch(compact)
        if scope_match is not None:
            kinds.append("new_note_scope")
        if numbered_match is not None and int(numbered_match.group(1)) > 61:
            kinds.append("next_numbered_note")
        if _DECOMPOSITION_SUBTABLE in compact:
            kinds.append("revenue_cost_decomposition_subtable")
        if not kinds:
            continue

        # Keep original line text for note/scope headings; for an embedded
        # subtable label retain the exact literal title as the boundary span.
        boundary_text = (raw_line if "new_note_scope" in kinds
                         or "next_numbered_note" in kinds
                         else _DECOMPOSITION_SUBTABLE)
        evidence = _native_literal_position(page, page_number, boundary_text)
        if evidence.bbox[1] <= title_box[3]:
            continue
        kind = "+".join(kinds)
        candidates.append((evidence.bbox[1], evidence.bbox[0], kind, evidence))

    if not candidates:
        raise DatedNoteCurrentTotalNotLocatedError(
            "no native same-page boundary after consolidated note 61 title")
    candidates.sort(key=lambda item: (item[0], item[1]))
    if len(candidates) > 1 and abs(candidates[1][0] - candidates[0][0]) < 0.1:
        raise DatedNoteCurrentTotalNotLocatedError(
            "same-page note 61 boundary has multiple first-position candidates")
    _, _, kind, evidence = candidates[0]
    return kind, evidence


def _amount(text: Any) -> Decimal | None:
    value = _compact(text).replace("−", "-")
    if not value or not _AMOUNT.fullmatch(value):
        return None
    if value.startswith("(") and value.endswith(")"):
        value = "-" + value[1:-1]
    try:
        result = Decimal(value.replace(",", ""))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _row_span(cells: list[Any]) -> tuple[float, float] | None:
    boxes = [box for cell in cells if (box := _bbox(cell)) is not None]
    if not boxes:
        return None
    return min(box[1] for box in boxes), max(box[3] for box in boxes)


def _center(word: dict[str, Any]) -> tuple[float, float]:
    return ((float(word["x0"]) + float(word["x1"])) / 2,
            (float(word["top"]) + float(word["bottom"])) / 2)


def _words_in_row(words: list[dict[str, Any]], span: tuple[float, float]) -> list[dict[str, Any]]:
    return [word for word in words
            if span[0] - 1 <= _center(word)[1] <= span[1] + 1]


def _cell_evidence(page: Any, table_index: int, row_index: int, cell_index: int,
                   role: str, text: str, raw_bbox: Any, chars: list[dict[str, Any]],
                   amount: Decimal | None = None) -> NoteTableCellEvidence:
    box = _bbox(raw_bbox)
    if box is None:
        raise DatedNoteCurrentTotalNotLocatedError(f"{role}: default-table bbox missing")
    glyphs = _glyphs(chars, box)
    if _compact("".join(glyph.text for glyph in glyphs)) != _compact(text):
        raise DatedNoteCurrentTotalNotLocatedError(
            f"{role}: native glyphs do not reproduce {text!r}")
    return NoteTableCellEvidence(role, text, page.page_number, table_index,
                                 row_index, cell_index, box, glyphs, amount)


def _row_cell(rows: list[list[Any]], boxes: list[list[Any]], row_index: int,
              label: str, role: str, page: Any, table_index: int,
              chars: list[dict[str, Any]]) -> NoteTableCellEvidence:
    hits = [index for index, text in enumerate(rows[row_index])
            if _compact(text) == label]
    if len(hits) != 1:
        raise DatedNoteCurrentTotalNotLocatedError(f"{role} header is not unique")
    index = hits[0]
    return _cell_evidence(page, table_index, row_index, index, role,
                          str(rows[row_index][index] or ""), boxes[row_index][index], chars)


def _parse_table(page: Any, table: Any, table_index: int, *, page_number: int,
                 title: NativeTextEvidence, boundary_kind: str,
                 boundary: NativeTextEvidence, words: list[dict[str, Any]],
                 period_end: date, pdf_sha256: str, instrument_id: str,
                 identity: tuple[tuple[tuple[int, str], ...],
                                 tuple[tuple[int, str], ...]],
                 scope: tuple[int, str]) -> DatedNoteCurrentTotalEvidence:
    table_box = _bbox(table.bbox)
    if (table_box is None or table_box[1] <= title.bbox[3]
            or table_box[3] >= boundary.bbox[1]):
        raise DatedNoteCurrentTotalNotLocatedError(
            "table is not wholly between note 61 title and its same-page boundary")
    rows = table.extract()
    physical = [list(row.cells) for row in table.rows]
    if not rows or len(rows) != len(physical) or any(
            len(row) != len(cells) for row, cells in zip(rows, physical)):
        raise DatedNoteCurrentTotalNotLocatedError("table text/cell geometry is incomplete")

    totals = [i for i, row in enumerate(rows)
              if sum(_compact(text) == "合计" for text in row) == 1]
    if len(totals) != 1:
        raise DatedNoteCurrentTotalNotLocatedError("no unique literal 合计 row")
    total_index = totals[0]

    period_rows = []
    for index, row in enumerate(rows):
        labels = [_compact(text) for text in row]
        now, prior = labels.count("本期发生额"), labels.count("上期发生额")
        if now or prior:
            if now != 1 or prior != 1:
                raise DatedNoteCurrentTotalNotLocatedError(
                    "current/prior header is missing or duplicated")
            period_rows.append(index)
    if len(period_rows) != 1 or period_rows[0] >= total_index:
        raise DatedNoteCurrentTotalNotLocatedError("no unique dated header before 合计")
    header_index = period_rows[0]

    subheader_rows = []
    for index in range(header_index + 1, total_index):
        labels = [_compact(text) for text in rows[index]]
        found = [label for label in labels if label in ("收入", "成本")]
        if found == ["收入", "成本", "收入", "成本"]:
            subheader_rows.append(index)
        elif found:
            raise DatedNoteCurrentTotalNotLocatedError(
                "revenue/cost subheaders are duplicate or incomplete")
    if len(subheader_rows) != 1:
        raise DatedNoteCurrentTotalNotLocatedError("no unique four-column subheader row")
    subheader_index = subheader_rows[0]

    header_span = _row_span(physical[header_index])
    subheader_span = _row_span(physical[subheader_index])
    total_span = _row_span(physical[total_index])
    if header_span is None or subheader_span is None or total_span is None:
        raise DatedNoteCurrentTotalNotLocatedError("period/header/total row bbox missing")
    header_words = [word for word in _words_in_row(words, header_span)
                    if _compact(word.get("text")) in ("本期发生额", "上期发生额")]
    current_words = [word for word in header_words
                     if _compact(word["text"]) == "本期发生额"]
    prior_words = [word for word in header_words
                   if _compact(word["text"]) == "上期发生额"]
    if len(current_words) != 1 or len(prior_words) != 1:
        raise DatedNoteCurrentTotalNotLocatedError("native period headers are ambiguous")

    subheader_words = [word for word in _words_in_row(words, subheader_span)
                       if _compact(word.get("text")) in ("收入", "成本")]
    subheader_words.sort(key=lambda word: _center(word)[0])
    subheader_labels = [_compact(word["text"]) for word in subheader_words]
    if subheader_labels != ["收入", "成本", "收入", "成本"]:
        raise DatedNoteCurrentTotalNotLocatedError(
            "native subheaders do not show current/prior revenue-cost pairs")
    now_header = _row_cell(rows, physical, header_index, "本期发生额",
                           "current-period-header", page, table_index, page.chars)
    prior_header = _row_cell(rows, physical, header_index, "上期发生额",
                             "prior-period-header", page, table_index, page.chars)
    if _center(current_words[0])[0] >= _center(prior_words[0])[0]:
        raise DatedNoteCurrentTotalNotLocatedError("prior period precedes current period")

    subheaders = []
    for occurrence, (label, word) in enumerate(zip(subheader_labels, subheader_words), 1):
        candidates = [index for index, text in enumerate(rows[subheader_index])
                      if _compact(text) == label
                      and (box := _bbox(physical[subheader_index][index])) is not None
                      and box[0] - 1 <= _center(word)[0] <= box[2] + 1]
        if len(candidates) != 1:
            raise DatedNoteCurrentTotalNotLocatedError(
                f"native subheader {occurrence} has no unique default-table cell")
        index = candidates[0]
        subheaders.append(_cell_evidence(
            page, table_index, subheader_index, index,
            f"subheader-{occurrence}-{label}", str(rows[subheader_index][index] or ""),
            physical[subheader_index][index], page.chars))

    label_hits = [i for i, text in enumerate(rows[total_index])
                  if _compact(text) == "合计"]
    if len(label_hits) != 1:
        raise DatedNoteCurrentTotalNotLocatedError("合计 label cell is ambiguous")
    label_index = label_hits[0]
    total_label = _cell_evidence(
        page, table_index, total_index, label_index, "total-row-label",
        str(rows[total_index][label_index] or ""),
        physical[total_index][label_index], page.chars)

    amounts = []
    for index, text in enumerate(rows[total_index]):
        value = _amount(text)
        if value is None:
            continue
        box = _bbox(physical[total_index][index])
        if box is None:
            raise DatedNoteCurrentTotalNotLocatedError("numeric total cell bbox missing")
        amounts.append((index, str(text or ""), box, value))
    if len(amounts) != 4:
        raise DatedNoteCurrentTotalNotLocatedError(
            "合计 row must have four nonblank revenue/cost current/prior cells")
    mapped = []
    for word in subheader_words:
        center = _center(word)[0]
        hits = [cell for cell in amounts if cell[2][0] + 1 < center < cell[2][2] - 1]
        if len(hits) != 1:
            raise DatedNoteCurrentTotalNotLocatedError(
                "header center does not map at least 1pt inside a unique total cell")
        mapped.append(hits[0])
    if (len({item[0] for item in mapped}) != 4
            or [item[0] for item in mapped]
            != [item[0] for item in sorted(amounts, key=lambda item: item[2][0])]):
        raise DatedNoteCurrentTotalNotLocatedError(
            "subheader order and physical total-cell order disagree")
    groups = ((mapped[0][2][0], mapped[1][2][2]),
              (mapped[2][2][0], mapped[3][2][2]))
    for word, span in zip((current_words[0], prior_words[0]), groups):
        x = _center(word)[0]
        if not span[0] - 1 <= x <= span[1] + 1:
            raise DatedNoteCurrentTotalNotLocatedError(
                "period header is not above its own pair of total cells")

    roles = ("current-revenue", "current-cost", "prior-revenue", "prior-cost")
    total_cells = tuple(_cell_evidence(
        page, table_index, total_index, index, roles[position], text,
        physical[total_index][index], page.chars, value)
        for position, (index, text, _, value) in enumerate(mapped))

    title_bottom, table_top = title.bbox[3], table_box[1]
    unit_words = [word for word in words
                  if _UNIT.fullmatch(_compact(word.get("text")))
                  and title_bottom < float(word["top"])
                  and float(word["bottom"]) < table_top]
    if len(unit_words) != 1:
        raise DatedNoteCurrentTotalNotLocatedError(
            f"note table has no unique own unit in its title/table boundary ({len(unit_words)})")
    unit_word = unit_words[0]
    unit_match = _UNIT.fullmatch(_compact(unit_word["text"]))
    assert unit_match is not None
    unit = unit_match.group(1)
    unit_box = tuple(float(unit_word[key]) for key in ("x0", "top", "x1", "bottom"))
    unit_evidence = _native_text(str(unit_word["text"]), page_number, unit_box, page.chars)
    unit_amount = total_cells[0].display_amount
    if unit_amount is None or unit not in _UNIT_SCALE:
        raise DatedNoteCurrentTotalNotLocatedError("current revenue total/unit is unusable")

    return DatedNoteCurrentTotalEvidence(
        pdf_sha256=pdf_sha256, instrument_id=instrument_id, period_end=period_end,
        issuer_identity=identity[0], report_period_identity=identity[1],
        consolidated_scope_page=scope[0], consolidated_scope_heading=scope[1],
        note_heading=title, note_boundary_kind=boundary_kind,
        note_boundary=boundary,
        unit_declaration=unit_evidence, unit=unit,
        current_header=now_header, prior_header=prior_header,
        current_revenue_header=subheaders[0], current_cost_header=subheaders[1],
        prior_revenue_header=subheaders[2], prior_cost_header=subheaders[3],
        total_row_label=total_label, current_revenue_cell=total_cells[0],
        current_cost_cell=total_cells[1], prior_revenue_cell=total_cells[2],
        prior_cost_cell=total_cells[3], table_index=table_index, table_bbox=table_box,
        row_count=len(rows), header_row_index=header_index,
        subheader_row_index=subheader_index, total_row_index=total_index,
        current_revenue_yuan=unit_amount * _UNIT_SCALE[unit])


def extract_dated_note_current_total(
    pdf_bytes: bytes, *, instrument_id: str, period_end: date,
) -> DatedNoteCurrentTotalEvidence:
    """Read a unique consolidated note 61 total; the primary amount is not input."""
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise DatedNoteCurrentTotalNotLocatedError("input must be original PDF bytes")
    if not re.fullmatch(r"\d{6}", instrument_id) or (period_end.month, period_end.day) != (12, 31):
        raise DatedNoteCurrentTotalNotLocatedError(
            "this narrow path requires a six-digit issuer and annual period")
    digest = "sha256:" + hashlib.sha256(pdf_bytes).hexdigest()
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            identity = _frontmatter(pdf, instrument_id, period_end)
            if identity is None:
                raise DatedNoteCurrentTotalNotLocatedError(
                    "front matter does not bind issuer and report period")
            issuer_rows, report_rows = identity[:2]
            annual_titles = tuple(
                (page, text) for page, text in report_rows
                if (match := _ANNUAL_TITLE.search(_compact(text))) is not None
                and int(match.group(1)) == period_end.year)
            if not annual_titles:
                raise DatedNoteCurrentTotalNotLocatedError(
                    "front matter lacks an annual title for period_end")
            lines = [(i + 1, line) for i, page in enumerate(pdf.pages)
                     for line in (page.extract_text(layout=False) or "").splitlines()]
            role, scope = "", None
            records = []
            for line_index, (page_number, line) in enumerate(lines):
                compact = _compact(line)
                scope_match = _NOTE_SCOPE_HEADING.fullmatch(compact)
                if scope_match:
                    role = "母公司" if scope_match.group(1) == "公司" else scope_match.group(1)
                    scope = (page_number, line)
                elif (role == ROLE_CONSOLIDATED and scope is not None
                      and _REVENUE_NOTE_HEADING.fullmatch(compact)
                      and _NOTE_61.fullmatch(compact)):
                    records.append((line_index, page_number, line, scope))
            if len(records) != 1:
                raise DatedNoteCurrentTotalNotLocatedError(
                    f"consolidated note 61 heading is not unique ({len(records)} hits)")
            title_line_index, page_number, raw_title, scope = records[0]
            page = pdf.pages[page_number - 1]
            title_hits = page.search(r"61\s*[、．.]\s*营业收入(?:和|、|及)营业成本(?:情况)?")
            if len(title_hits) != 1:
                raise DatedNoteCurrentTotalNotLocatedError(
                    "note 61 has no unique native title position")
            title_box = tuple(float(title_hits[0][key])
                              for key in ("x0", "top", "x1", "bottom"))
            title_glyphs = _glyphs(page.chars, title_box)
            if _compact("".join(g.text for g in title_glyphs)) != _compact(raw_title):
                raise DatedNoteCurrentTotalNotLocatedError(
                    "native glyphs do not reproduce note 61 title")
            title = NativeTextEvidence(raw_title, page_number, title_box, title_glyphs)
            boundary_kind, boundary = _next_note61_boundary(
                page, page_number, lines, title_line_index, title_box)
            words = page.extract_words(x_tolerance=2, y_tolerance=3,
                                       keep_blank_chars=False, use_text_flow=True)
            found = []
            for table_index, table in enumerate(page.find_tables()):
                table_box = _bbox(table.bbox)
                if (table_box is None or table_box[1] <= title_box[3]
                        or table_box[3] >= boundary.bbox[1]):
                    continue
                try:
                    found.append(_parse_table(
                        page, table, table_index, page_number=page_number,
                        title=title, boundary_kind=boundary_kind,
                        boundary=boundary, words=words, period_end=period_end,
                        pdf_sha256=digest, instrument_id=instrument_id,
                        identity=(tuple(issuer_rows), tuple(report_rows)), scope=scope))
                except DatedNoteCurrentTotalNotLocatedError:
                    continue
            if len(found) != 1:
                raise DatedNoteCurrentTotalNotLocatedError(
                    f"note 61 contains {len(found)} unique default-table candidates")
            return found[0]
    except DatedNoteCurrentTotalNotLocatedError:
        raise


__all__ = [
    "DatedNoteCurrentTotalEvidence",
    "DatedNoteCurrentTotalNotLocatedError",
    "EXTRACTOR_VERSION",
    "extract_dated_note_current_total",
]
