"""Private, receipt-byte parser for H125 parent-equity successor comparisons.

The CURRENT fact is the H125 consolidated balance-sheet row.  A same-report
consolidated equity-change closing cell is retained as a native geometry
cross-check.  The successor source is the later H126 report's presented H125
consolidated equity-change closing subtotal.  This module classifies only the
value at that period; it does not assign a cause or admit a financial fact.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import re
from typing import Literal, Mapping, Sequence

import pdfplumber

from aquant.adapters.providers import parent_equity_native_closing as _base
from aquant.adapters.providers import s2_revision_impact as _impact


SCHEMA = "aquant.parent-equity.successor-comparator.v1"
_PARENT_GROUPS = frozenset({"归属于母公司所有者权益", "归属于母公司股东权益"})
_CONSOLIDATED_TITLE = re.compile(r"(?:[一二三四五六七八九十\d]+[、.．])?合并所有者权益变动表")
_MAX_TITLE_GAP = 5
_EPSILON = _base._BBOX_TOLERANCE


class ParentEquitySuccessorCompareError(ValueError):
    """Raised when the supplied PDF does not prove one unique native cell."""


def parse_parent_equity_closing(
    pdf_bytes: bytes,
    *,
    period_end: date,
    source_report_period_end: date,
    presentation: Literal["current", "presented_prior"],
    announcement_id: str,
) -> dict[str, object]:
    """Parse a target H125 CURRENT row or a successor's presented-prior row.

    ``current`` is taken from the consolidated balance sheet's ``期末余额``
    column.  Its same-report consolidated equity-change table is independently
    extracted with native table geometry and must equal the balance-sheet cell.
    ``presented_prior`` is taken from the consolidated equity-change table in a
    later report and must have the exact target period and ``小计`` column.

    The caller supplies report identity/period from its bound source packet;
    this parser binds the supplied bytes with a computed SHA-256 but does not
    replace receipt, index, or issuer-identity checks.
    """
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise ParentEquitySuccessorCompareError("input must be the bound PDF bytes")
    if not isinstance(period_end, date) or not isinstance(source_report_period_end, date):
        raise ParentEquitySuccessorCompareError("period dates must be date values")
    if presentation not in {"current", "presented_prior"}:
        raise ParentEquitySuccessorCompareError("unsupported presentation role")
    if not announcement_id:
        raise ParentEquitySuccessorCompareError("announcement id is required")

    if presentation == "current":
        if source_report_period_end != period_end:
            raise ParentEquitySuccessorCompareError("CURRENT report date must equal the fact date")
        return _parse_current_balance_sheet(
            pdf_bytes, period_end=period_end,
            source_report_period_end=source_report_period_end,
            announcement_id=announcement_id,
        )
    if source_report_period_end <= period_end:
        raise ParentEquitySuccessorCompareError("presented-prior report must be later than the fact date")
    if (source_report_period_end.month, source_report_period_end.day) != (period_end.month, period_end.day):
        raise ParentEquitySuccessorCompareError("presented-prior report must have the matching half-year boundary")
    return _parse_presented_prior_change_table(
        pdf_bytes, period_end=period_end,
        source_report_period_end=source_report_period_end,
        announcement_id=announcement_id,
    )


def compare_parent_equity_h125_pair(
    current_pdf_bytes: bytes,
    prior_pdf_bytes: bytes,
    *,
    period_end: date,
    current_announcement_id: str,
    current_source_report_period_end: date,
    prior_announcement_id: str,
    prior_source_report_period_end: date,
) -> dict[str, object]:
    """Build the narrow same-date comparison candidate; formal admission stays closed."""
    current = parse_parent_equity_closing(
        current_pdf_bytes, period_end=period_end,
        source_report_period_end=current_source_report_period_end,
        presentation="current", announcement_id=current_announcement_id,
    )
    prior = parse_parent_equity_closing(
        prior_pdf_bytes, period_end=period_end,
        source_report_period_end=prior_source_report_period_end,
        presentation="presented_prior", announcement_id=prior_announcement_id,
    )
    current_amount = Decimal(str(current["amountYuan"]))
    prior_amount = Decimal(str(prior["amountYuan"]))
    delta = prior_amount - current_amount
    return {
        "schema": SCHEMA,
        "period_end": period_end.isoformat(),
        "metric": "parent_equity",
        "current": current,
        "presentedPrior": prior,
        "same_value": delta == 0,
        "delta_yuan": str(delta),
        "candidate": "NO_EFFECT_VALUE_ONLY" if delta == 0 else "AFFECTS_VALUE_CHANGE",
        "causal_attribution": "UNRESOLVED",
        "admission": "NOT_ADMITTED",
    }


def replay_parent_equity_closing_proof(
    pdf_bytes: bytes,
    *,
    period_end: date,
    source_report_period_end: date,
    presentation: Literal["current", "presented_prior"],
    announcement_id: str,
    saved: Mapping[str, object],
    expected_amount_yuan: str,
) -> bool:
    """Reparse the same bytes and fail closed on any semantic or geometry change."""
    try:
        actual = parse_parent_equity_closing(
            pdf_bytes, period_end=period_end,
            source_report_period_end=source_report_period_end,
            presentation=presentation, announcement_id=announcement_id,
        )
        return (
            dict(saved) == actual
            and str(actual.get("amountYuan")) == str(Decimal(expected_amount_yuan))
            and _proof_has_parent_subtotal(actual)
        )
    except (ParentEquitySuccessorCompareError, InvalidOperation, TypeError, ValueError,
            ArithmeticError, KeyError, AttributeError, OSError):
        return False


def _parse_current_balance_sheet(
    pdf_bytes: bytes, *, period_end: date,
    source_report_period_end: date, announcement_id: str,
) -> dict[str, object]:
    cells = _impact._cells(pdf_bytes, period_end.isoformat(), ("parent_equity",))
    row = cells.get("parent_equity")
    if row is None:
        raise ParentEquitySuccessorCompareError("CURRENT balance-sheet parent-equity row is missing or ambiguous")
    if (row.get("statementScope") != "CONSOLIDATED"
            or row.get("metric") != "parent_equity"
            or row.get("factPeriodEnd") != period_end.isoformat()
            or row.get("columnRole") != "CURRENT"
            or row.get("currentHeader") != "期末余额"
            or row.get("unitRaw") != "元"):
        raise ParentEquitySuccessorCompareError("CURRENT row identity/period/column/unit failed")
    if not _is_parent_equity_label(str(row.get("rowLabel") or "")):
        raise ParentEquitySuccessorCompareError("CURRENT row label is not the consolidated parent-equity total")

    rollforward = _parse_native_change_table_cell(
        pdf_bytes, period_end=period_end,
        source_report_period_end=source_report_period_end,
        presentation="current",
    )
    if str(rollforward["amount_yuan"]) != str(row.get("amountYuan")):
        raise ParentEquitySuccessorCompareError(
            "CURRENT balance-sheet value disagrees with same-report native equity-change subtotal")
    native_chars = _native_char_count(pdf_bytes, int(row["page"]))
    digest = _sha256(pdf_bytes)
    amount = str(Decimal(str(row["amountYuan"])))
    return {
        "announcementId": announcement_id,
        "sourceReportPeriodEnd": source_report_period_end.isoformat(),
        "factPeriodEnd": period_end.isoformat(),
        "columnRole": "CURRENT",
        "statement": "合并资产负债表",
        "statementScope": "CONSOLIDATED",
        "currency": "CNY",
        "amountUnit": "元",
        "rawUnit": str(row["unitRaw"]),
        "amountYuan": amount,
        "page": int(row["page"]),
        "headerPage": int(row["tablePages"][0]),
        "rowLabel": str(row["rowLabel"]),
        "columnHeader": str(row["currentHeader"]),
        "sourceLine": str(row["sourceLine"]),
        "pdfSha256": digest,
        "nativeChars": native_chars,
        "proof": {
            "path_kind": "native_consolidated_balance_sheet_current_parent_equity_row",
            "fact_period_end": period_end.isoformat(),
            "statement": "合并资产负债表",
            "statement_scope": "CONSOLIDATED",
            "column_role": "CURRENT",
            "current_header": str(row["currentHeader"]),
            "comparison_header": str(row["priorHeader"]),
            "comparison_period_end": str(row["priorFactPeriodEnd"]),
            "unit": str(row["unitRaw"]),
            "row_label": str(row["rowLabel"]),
            "page": int(row["page"]),
            "table_pages": list(row["tablePages"]),
            "source_line": str(row["sourceLine"]),
            "current_cell_raw": str(row["currentCellRaw"]),
            "amount_yuan": amount,
            "native_chars": native_chars,
        },
        "sameReportEquityChangeTable": rollforward,
    }


def _parse_presented_prior_change_table(
    pdf_bytes: bytes, *, period_end: date,
    source_report_period_end: date, announcement_id: str,
) -> dict[str, object]:
    proof = _parse_native_change_table_cell(
        pdf_bytes, period_end=period_end,
        source_report_period_end=source_report_period_end,
        presentation="presented_prior",
    )
    return {
        "announcementId": announcement_id,
        "sourceReportPeriodEnd": source_report_period_end.isoformat(),
        "factPeriodEnd": period_end.isoformat(),
        "columnRole": "PRESENTED_PRIOR",
        "statement": "合并所有者权益变动表",
        "statementScope": "CONSOLIDATED",
        "currency": "CNY",
        "amountUnit": str(proof["unit"]),
        "rawUnit": str(proof["unit"]),
        "amountYuan": str(proof["amount_yuan"]),
        "page": int(proof["closing_row_page"]),
        "headerPage": int(proof["report_table_page"]),
        "rowLabel": str(proof["closing_row_label"]),
        "columnHeader": f"{proof['parent_group_header']} / {proof['subtotal_header']}",
        "sourceLine": f"{proof['closing_row_label']} | {proof['native_cell_text']}",
        "pdfSha256": _sha256(pdf_bytes),
        "nativeChars": proof["native_chars_by_page"][str(proof["closing_row_page"])],
        "proof": proof,
    }


def _parse_native_change_table_cell(
    pdf_bytes: bytes, *, period_end: date,
    source_report_period_end: date,
    presentation: Literal["current", "presented_prior"],
) -> dict[str, object]:
    """Locate one table using native characters and ruled-cell geometry only."""
    page_candidates: list[tuple[int, tuple[int, str, float], str, tuple[str, str], str | None]] = []
    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            for number, raw_page in enumerate(pdf.pages, 1):
                chars = tuple(dict(char) for char in getattr(raw_page, "chars", ()))
                lines = _base._char_lines(chars)
                compact_lines = [(_base._compact(line), top) for top, line in lines]
                title = _nearest_consolidated_title(pdf.pages, number)
                if title is None:
                    continue
                title_page_number, title_text, title_top = title
                period_matches = [(line, top) for line, top in compact_lines
                                  if _base._period_header_matches(line, period_end)]
                unit = _base._unit_from_lines(lines)
                parent_group_tops = [top for line, top in compact_lines
                                     if any(group in line for group in _PARENT_GROUPS)]
                if not parent_group_tops:
                    continue
                if len(period_matches) == 1:
                    period_text, period_top = period_matches[0]
                elif not period_matches:
                    native_period = _native_period_header(
                        chars, period_end, after_top=title_top,
                        before_top=min(parent_group_tops),
                    )
                    if native_period is None:
                        continue
                    period_text, period_top = native_period
                else:
                    continue
                if unit is None:
                    unit = _native_unit_header(
                        chars, after_top=title_top, before_top=period_top,
                    )
                if unit is None:
                    continue
                unit_text, unit_name = unit
                anchors = [(line, top) for line, top in compact_lines
                           if line == ("本期金额" if presentation == "current" else "上年金额")]
                if presentation == "current" and len(anchors) > 1:
                    continue
                if presentation == "presented_prior" and len(anchors) > 1:
                    continue
                if anchors and not anchors[0][1] < period_top:
                    continue
                if presentation == "current" and source_report_period_end != period_end:
                    continue
                if presentation == "presented_prior" and source_report_period_end <= period_end:
                    continue
                if not any(
                    unit_top < period_top and (
                        _base._compact(line) == _base._compact(unit_text)
                        or (unit_name == "元" and re.search(r"单位[：:]元", _base._compact(line)))
                    )
                    for unit_top, line in lines
                ):
                    continue
                page_candidates.append((number, title, period_text, unit, anchors[0][0] if anchors else None))

            if len(page_candidates) != 1:
                raise ParentEquitySuccessorCompareError(
                    f"equity-change table header is missing or ambiguous ({len(page_candidates)} candidates)")
            header_number, title, period_text, unit, role_anchor = page_candidates[0]
            header_page = _base._native_page_from_pdf(header_number, pdf.pages[header_number - 1])
            header_cells = _parent_subtotal_cells(header_page)
            if len(header_cells) != 1:
                raise ParentEquitySuccessorCompareError(
                    f"归母权益小计列header is missing or ambiguous ({len(header_cells)} candidates)")
            header_cell, parent_header, subtotal_header = header_cells[0]
            title_page_number, title_text, title_top = title
            closing_matches = []
            last_page = min(len(pdf.pages), header_number + _base._MAX_PAGE_GAP)
            for number in range(header_number, last_page + 1):
                raw_page = pdf.pages[number - 1]
                native_page = _base._native_page_from_pdf(number, raw_page)
                scope_pages = tuple(
                    _base._native_page_from_pdf(scoped_number, pdf.pages[scoped_number - 1])
                    for scoped_number in range(header_number, number + 1)
                )
                for table in native_page.tables:
                    for label_bbox in _base._label_bbox_candidates(table, header_cell.bbox):
                        if _prior_boundary_precedes(
                                native_page, label_bbox, header_cell,
                                header_number=header_number, role_anchor=role_anchor,
                                scope_pages=scope_pages,
                                title_page_number=title_page_number,
                                title_text=title_text,
                                title_top=title_top):
                            continue
                        for cell in table.cells:
                            if not _base._cell_inside_page(cell, native_page):
                                continue
                            if any(abs(cell.bbox[index] - header_cell.bbox[index]) > _EPSILON
                                   for index in (0, 2)):
                                continue
                            if cell.bbox[0] <= label_bbox[2] - _EPSILON:
                                continue
                            if (cell.bbox[1] > label_bbox[1] + _EPSILON
                                    or cell.bbox[3] < label_bbox[3] - _EPSILON):
                                continue
                            native_text = _base._page_cell_text(native_page, cell.bbox)
                            if native_text is None:
                                continue
                            display = _base._compact(native_text)
                            if not _base._AMOUNT.fullmatch(display):
                                continue
                            try:
                                amount = Decimal(display.replace(",", "")) * _base._UNIT_SCALE[unit[1]]
                            except (InvalidOperation, KeyError):
                                continue
                            if not amount.is_finite():
                                continue
                            closing_matches.append((native_page, cell, label_bbox, native_text, display, amount,
                                                   "ruled_native_table_cell"))
                if not native_page.tables:
                    projected = _unruled_closing_candidates(native_page, header_cell)
                    for cell, label_bbox, native_text, display, amount in projected:
                        if _prior_boundary_precedes(
                                native_page, label_bbox, header_cell,
                                header_number=header_number, role_anchor=role_anchor,
                                scope_pages=scope_pages,
                                title_page_number=title_page_number,
                                title_text=title_text,
                                title_top=title_top):
                            continue
                        closing_matches.append((native_page, cell, label_bbox, native_text, display, amount,
                                               "native_chars_projected_to_header_subtotal_column"))
            if len(closing_matches) != 1:
                raise ParentEquitySuccessorCompareError(
                    f"equity-change closing subtotal is missing or ambiguous ({len(closing_matches)} matches)")

            (closing_page, closing_cell, label_bbox, native_cell_text, display,
             amount_yuan, closing_cell_method) = closing_matches[0]
            native_chars = {
                str(n): len(pdf.pages[n - 1].extract_text() or "")
                for n in sorted({header_number, closing_page.number})
            }
            opening_bridge: list[dict[str, object]] = []
            opening_bridge_status = "NOT_APPLICABLE_CURRENT_SOURCE"
            opening_bridge_note: str | None = None
            if presentation == "presented_prior":
                opening_bridge_status = "PARTIAL_CONTEXT_ONLY"
                try:
                    opening_bridge = _opening_bridge_rows(
                        pdf.pages[header_number - 1], header_cell,
                    )
                    if opening_bridge:
                        opening_bridge_note = "best-effort rows through opening balance; native page excerpt remains primary"
                    else:
                        opening_bridge_note = "no structured opening rows; native page excerpt retained"
                except Exception as exc:
                    opening_bridge_note = f"structured rows unavailable: {type(exc).__name__}; native page excerpt retained"
            return {
                "path_kind": "native_parent_equity_change_table_closing_subtotal_cell",
                "period_end": period_end.isoformat(),
                "source_report_period_end": source_report_period_end.isoformat(),
                "presentation": "CURRENT" if presentation == "current" else "PRESENTED_PRIOR",
                "report_table_page": header_number,
                "closing_row_page": closing_page.number,
                "report_table_title": title_text,
                "period_header": period_text,
                "current_amount_header": role_anchor if presentation == "current" else None,
                "current_role_evidence": (
                    "explicit_current_period_header" if presentation == "current" and role_anchor
                    else "report_period_matches_fact_period" if presentation == "current"
                    else None
                ),
                "prior_period_anchor": role_anchor,
                "unit_declaration": unit[0],
                "unit": unit[1],
                "parent_group_header": parent_header,
                "subtotal_header": subtotal_header,
                "closing_row_label": _base._CLOSING_LABEL,
                "header_subtotal_bbox": list(header_cell.bbox),
                "closing_cell_bbox": list(closing_cell.bbox),
                "closing_cell_method": closing_cell_method,
                "closing_row_bbox": list(label_bbox),
                "native_cell_text": native_cell_text,
                "display_amount": display,
                "amount_yuan": str(amount_yuan),
                "bbox_tolerance_points": _EPSILON,
                "native_chars_by_page": native_chars,
                "opening_bridge": opening_bridge,
                "opening_bridge_status": opening_bridge_status,
                "opening_bridge_note": opening_bridge_note,
                "opening_bridge_native_excerpt_pages": [header_number] if presentation == "presented_prior" else [],
            }
    except ParentEquitySuccessorCompareError:
        raise
    except Exception as exc:
        raise ParentEquitySuccessorCompareError(
            f"native equity-change table replay failed: {type(exc).__name__}") from exc


def _nearest_consolidated_title(pages: Sequence[object], page_number: int) -> tuple[int, str, float] | None:
    floor = max(1, page_number - _MAX_TITLE_GAP)
    candidates: list[tuple[int, float, str]] = []
    for number in range(floor, page_number + 1):
        lines = _base._char_lines(tuple(dict(char) for char in getattr(pages[number - 1], "chars", ())))
        for top, line in lines:
            compact = _base._compact(line)
            if _CONSOLIDATED_TITLE.fullmatch(compact):
                candidates.append((number, top, compact))
    if not candidates:
        return None
    candidate = max(candidates, key=lambda item: (item[0], item[1]))
    title_page, title_top, title_text = candidate
    for number in range(title_page, page_number + 1):
        lines = _base._char_lines(tuple(dict(char) for char in getattr(pages[number - 1], "chars", ())))
        for top, line in lines:
            compact = _base._compact(line)
            if _base._PARENT_COMPANY_TABLE_TITLE.fullmatch(compact):
                return None
            if _CONSOLIDATED_TITLE.fullmatch(compact) and (number, top, compact) != candidate:
                return None
            if number == title_page and top < title_top:
                continue
    return title_page, title_text, title_top


def _native_period_header(
    chars: Sequence[Mapping[str, object]], period_end: date, *,
    after_top: float, before_top: float,
) -> tuple[str, float] | None:
    """Join native glyphs with a small baseline tolerance for the table period."""
    if (period_end.month, period_end.day) != (6, 30):
        return None
    expected = f"{period_end.year}年半年度"
    bands = _native_glyph_bands(chars)
    matches: list[tuple[str, float]] = []
    for band in bands:
        top = min(float(char.get("top", 0.0)) for char in band)
        if not after_top < top < before_top:
            continue
        text = _base._compact("".join(
            str(char.get("text", "")) for char in sorted(band, key=lambda item: float(item.get("x0", 0.0)))
        ))
        if text == expected:
            matches.append((expected, top))
    return matches[0] if len(matches) == 1 else None


def _native_unit_header(
    chars: Sequence[Mapping[str, object]], *, after_top: float, before_top: float,
) -> tuple[str, str] | None:
    """Read one CNY yuan declaration from the native band before the period row."""
    matches: list[tuple[str, str]] = []
    for band in _native_glyph_bands(chars):
        top = min(float(char.get("top", 0.0)) for char in band)
        if not after_top < top < before_top:
            continue
        text = _base._compact("".join(
            str(char.get("text", "")) for char in sorted(band, key=lambda item: float(item.get("x0", 0.0)))
        ))
        if re.search(r"单位[：:]元", text):
            matches.append(("单位：元", "元"))
    return matches[0] if len(matches) == 1 else None


def _native_glyph_bands(
    chars: Sequence[Mapping[str, object]], *, baseline_tolerance: float = 2.0,
) -> list[list[Mapping[str, object]]]:
    bands: list[list[Mapping[str, object]]] = []
    for char in sorted(chars, key=lambda item: (float(item.get("top", 0.0)),
                                                 float(item.get("x0", 0.0)))):
        top = float(char.get("top", 0.0))
        if not bands or top - min(float(item.get("top", 0.0)) for item in bands[-1]) > baseline_tolerance:
            bands.append([char])
        else:
            bands[-1].append(char)
    return bands


def _parent_subtotal_cells(page: object) -> tuple[tuple[object, str, str], ...]:
    cells = [cell for table in page.tables for cell in table.cells
             if _base._cell_inside_page(cell, page)]
    parents = [cell for cell in cells
               if _base._compact(_base._page_cell_text(page, cell.bbox) or "") in _PARENT_GROUPS]
    minorities = [cell for cell in cells
                  if _base._compact(_base._page_cell_text(page, cell.bbox) or "") == "少数股东权益"]
    subtotals = [cell for cell in cells
                 if _base._compact(_base._page_cell_text(page, cell.bbox) or "") == "小计"]
    found: dict[tuple[float, float, float, float], tuple[object, str, str]] = {}
    for parent in parents:
        left, top, right, bottom = parent.bbox
        projection_rights = {
            minority.bbox[0] for minority in minorities
            if (minority.bbox[0] > left
                and minority.bbox[0] >= right - _EPSILON
                and minority.bbox[1] <= bottom + 50
                and minority.bbox[3] >= top - _EPSILON)
        }
        if len(projection_rights) != 1:
            continue
        projection_right = next(iter(projection_rights))
        parent_text = _base._compact(_base._page_cell_text(page, parent.bbox) or "")
        for subtotal in subtotals:
            sub_left, sub_top, sub_right, _ = subtotal.bbox
            if (sub_left < left - _EPSILON or right < sub_left - _EPSILON
                    or sub_right > projection_right + _EPSILON
                    or sub_top < top - _EPSILON or sub_top > bottom + 50):
                continue
            found.setdefault(subtotal.bbox, (subtotal, parent_text, "小计"))
    return tuple(found.values())


def _unruled_closing_candidates(
    page: object, header_cell: object,
) -> tuple[tuple[object, tuple[float, float, float, float], str, str, Decimal], ...]:
    """Project a native text-only continuation onto the ruled subtotal column.

    This path is available only when the continuation page has no detected
    table geometry.  The column x-range comes from the native header cell on
    the exact consolidated table header page; the closing-row y-range comes
    from native glyph bounds for the exact row label on the continuation.
    """
    valid_chars = _base._valid_chars(page)
    label_glyphs = []
    for char in valid_chars:
        text = str(char.get("text", ""))
        x0 = float(char.get("x0", 0.0))
        if x0 >= header_cell.bbox[0]:
            continue
        if any("\u3400" <= glyph <= "\u9fff" or glyph in {"、", "（", "）", "．"}
               for glyph in text):
            label_glyphs.append(char)
    lines: list[list[object]] = []
    for char in sorted(label_glyphs, key=lambda item: (float(item.get("top", 0.0)),
                                                        float(item.get("x0", 0.0)))):
        top = float(char.get("top", 0.0))
        if not lines or abs(top - float(lines[-1][0].get("top", 0.0))) > _EPSILON:
            lines.append([char])
        else:
            lines[-1].append(char)
    lines = [sorted(line, key=lambda item: float(item.get("x0", 0.0))) for line in lines]
    matching_label_chars: list[object] | None = None
    for start in range(len(lines)):
        collected: list[object] = []
        text_parts: list[str] = []
        previous_top: float | None = None
        for line in lines[start:]:
            top = min(float(char.get("top", 0.0)) for char in line)
            if previous_top is not None and top - previous_top > 14.0:
                break
            collected.extend(line)
            text_parts.append("".join(str(char.get("text", "")) for char in line))
            if _base._compact("".join(text_parts)) == _base._CLOSING_LABEL:
                matching_label_chars = collected
                break
            if not _base._CLOSING_LABEL.startswith(_base._compact("".join(text_parts))):
                break
            previous_top = top
        if matching_label_chars is not None:
            break
    if not matching_label_chars:
        return ()

    label_bbox = (
        min(float(char["x0"]) for char in matching_label_chars),
        min(float(char["top"]) for char in matching_label_chars),
        max(float(char["x1"]) for char in matching_label_chars),
        max(float(char["bottom"]) for char in matching_label_chars),
    )
    glyph_heights = [float(char["bottom"]) - float(char["top"]) for char in matching_label_chars]
    vertical_margin = max(glyph_heights) + _EPSILON
    cell_bbox = (
        float(header_cell.bbox[0]),
        label_bbox[1] - vertical_margin,
        float(header_cell.bbox[2]),
        label_bbox[3] + vertical_margin,
    )
    native_text = _base._page_cell_text(page, cell_bbox)
    if native_text is None:
        return ()
    display = _base._compact(native_text)
    if not _base._AMOUNT.fullmatch(display):
        return ()
    try:
        amount = Decimal(display.replace(",", ""))
    except InvalidOperation:
        return ()
    if not amount.is_finite():
        return ()
    cell = _base._NativeCell(cell_bbox, native_text)
    return ((cell, label_bbox, native_text, display, amount),)


def _prior_boundary_precedes(
    page: object, label_bbox: tuple[float, float, float, float],
    header_cell: object, *, header_number: int, role_anchor: str | None,
    scope_pages: Sequence[object],
    title_page_number: int, title_text: str, title_top: float,
) -> bool:
    if (not any(scoped.number == header_number for scoped in scope_pages)
            or page.number < header_number):
        return True
    for scoped_page in scope_pages:
        if not header_number <= scoped_page.number <= page.number:
            continue
        for top, line in _base._char_lines(_base._valid_chars(scoped_page)):
            compact = _base._compact(line)
            is_boundary = (
                compact in _base._PRIOR_AMOUNT_BOUNDARIES
                or _base._PARENT_COMPANY_TABLE_TITLE.fullmatch(compact) is not None
                or _CONSOLIDATED_TITLE.fullmatch(compact) is not None
            )
            if not is_boundary:
                continue
            if (scoped_page.number == title_page_number
                    and compact == title_text
                    and abs(top - title_top) <= _EPSILON):
                continue
            if (scoped_page.number == header_number
                    and role_anchor == "上年金额" and compact == "上年金额"
                    and top <= header_cell.bbox[1] + _EPSILON):
                continue
            if (scoped_page.number < page.number
                    or top < label_bbox[1] - _EPSILON):
                return True
    return False


def _opening_bridge_rows(raw_page: object, header_cell: object) -> list[dict[str, object]]:
    """Read only the native opening bridge through the following opening balance."""
    wanted: list[tuple[str, re.Pattern[str]]] = [
        ("opening_before", re.compile(r"(?:一、)?上年(?:期末|年末)余额")),
        ("accounting_policy_change", re.compile(r"(?:加)?会计政策变更")),
        ("prior_error_correction", re.compile(r"前期差错更正")),
        ("other_adjustment", re.compile(r"其他")),
        ("opening_after", re.compile(r"二、本年(?:期初|年初)余额")),
    ]
    candidates: list[tuple[float, str, str, tuple[float, float, float, float]]] = []
    try:
        tables = raw_page.find_tables({"vertical_strategy": "lines", "horizontal_strategy": "lines"})
    except Exception as exc:
        raise ParentEquitySuccessorCompareError(
            f"opening bridge table-cell locator failed: {type(exc).__name__}") from exc
    for table in tables:
        for row in getattr(table, "rows", ()):
            row_bboxes = [_base._tuple_bbox(box) for box in getattr(row, "cells", ())]
            row_bboxes = [bbox for bbox in row_bboxes if bbox is not None]
            if not row_bboxes:
                continue
            first_x = min(bbox[0] for bbox in row_bboxes)
            label_cells = [bbox for bbox in row_bboxes
                           if abs(bbox[0] - first_x) <= _EPSILON
                           and bbox[2] < header_cell.bbox[0] - _EPSILON]
            if len(label_cells) != 1:
                continue
            label_text = _base._compact(_base._cell_text(raw_page.chars, label_cells[0]))
            label_text = re.sub(r"[\s:：]+", "", label_text)
            kind = next((key for key, pattern in wanted if pattern.fullmatch(label_text)), None)
            if kind is None:
                continue
            aligned = [bbox for bbox in row_bboxes
                       if abs(bbox[0] - header_cell.bbox[0]) <= _EPSILON
                       and abs(bbox[2] - header_cell.bbox[2]) <= _EPSILON]
            if len(aligned) != 1:
                raise ParentEquitySuccessorCompareError(
                    f"opening bridge {kind} row has no unique subtotal cell")
            row_top = min(bbox[1] for bbox in row_bboxes)
            candidates.append((row_top, kind, label_text, aligned[0]))
    opening_after_tops = [top for top, kind, _label, _bbox in candidates if kind == "opening_after"]
    if not opening_after_tops:
        raise ParentEquitySuccessorCompareError("presented-prior opening-after row was not located")
    boundary_top = min(opening_after_tops)
    result: list[dict[str, object]] = []
    for _top, kind, label_text, bbox in sorted(candidates, key=lambda item: (item[0], item[1])):
        if _top > boundary_top + _EPSILON:
            continue
        raw_text = _base._cell_text(raw_page.chars, bbox)
        compact = _base._compact(raw_text)
        amount: str | None = None
        if compact:
            if _base._AMOUNT.fullmatch(compact):
                amount = str(Decimal(compact.replace(",", "")) * _base._UNIT_SCALE["元"])
            elif compact not in {"-", "—", "–", "－"}:
                raise ParentEquitySuccessorCompareError(
                    f"opening bridge {kind} subtotal is neither a native amount nor a dash")
        result.append({
            "kind": kind,
            "rowLabel": label_text,
            "nativeCellText": raw_text,
            "amountYuan": amount,
            "cellBbox": list(bbox),
        })
    kinds = [str(row["kind"]) for row in result]
    if len(kinds) != len(set(kinds)):
        raise ParentEquitySuccessorCompareError("opening bridge contains duplicate native row roles")
    if "opening_before" not in kinds or "opening_after" not in kinds:
        raise ParentEquitySuccessorCompareError("opening bridge does not contain both opening balance rows")
    return result


def _native_char_count(pdf_bytes: bytes, physical_page: int) -> int:
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        if physical_page < 1 or physical_page > len(pdf.pages):
            raise ParentEquitySuccessorCompareError("physical page is outside the PDF")
        return len(pdf.pages[physical_page - 1].extract_text() or "")


def _is_parent_equity_label(label: str) -> bool:
    compact = _base._compact(label)
    return compact in {"归属于母公司所有者权益合计", "归属于母公司股东权益合计"}


def _proof_has_parent_subtotal(value: Mapping[str, object]) -> bool:
    role = value.get("columnRole")
    proof: Mapping[str, object] | None
    if role == "CURRENT":
        nested = value.get("sameReportEquityChangeTable")
        proof = nested if isinstance(nested, Mapping) else None
    else:
        nested = value.get("proof")
        proof = nested if isinstance(nested, Mapping) else None
    if proof is None:
        return False
    parent = proof.get("parent_group_header")
    bbox_values = [proof.get("header_subtotal_bbox"), proof.get("closing_cell_bbox"), proof.get("closing_row_bbox")]
    if not (parent in _PARENT_GROUPS and proof.get("subtotal_header") == "小计"
            and proof.get("closing_row_label") == _base._CLOSING_LABEL
            and proof.get("unit") == "元" and proof.get("period_end") == value.get("factPeriodEnd")):
        return False
    try:
        return all(len(bbox) == 4 and all(float(v) == float(v) for v in bbox) for bbox in bbox_values)
    except (TypeError, ValueError):
        return False


def _sha256(data: bytes) -> str:
    return "sha256:" + sha256(data).hexdigest()
