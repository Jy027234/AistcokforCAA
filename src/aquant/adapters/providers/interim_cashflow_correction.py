"""Native CFO evidence for the corrected 601069 2025 H1 statements.

This is intentionally pinned to announcement 1225082560.  It reads only the
correction cover and its physical page 13, where the consolidated H1 cash-flow
statement and its two displayed period columns appear.  It is a source proof,
not a promotion or admission decision.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Mapping, Sequence

from . import operating_cashflow_subtotals as _native


TARGET_INSTRUMENT_ID = "601069"
TARGET_ANNOUNCEMENT_ID = "1225082560"
TARGET_DOCUMENT_URL = (
    "https://static.cninfo.com.cn/finalpage/2026-04-08/1225082560.PDF"
)
TARGET_PDF_SHA256 = "ad2fb88d66457c794f32d15ed80e274815ed1d7d48fc3f87932c0d7063e760e6"
TARGET_PERIOD_END = date(2025, 6, 30)
CORRECTION_COVER_PAGE = 1
CONSOLIDATED_H1_CASHFLOW_PAGE = 13
PROOF_SCHEMA = "aquant.interim-cashflow-correction-native-proof.v1"

_CFO_LABEL = "经营活动产生的现金流量净额"
_OPERATING_SECTION = re.compile(
    r"^[一二三四五六七八九十百]+[、.．]经营活动产生的现金流量[:：]?$"
)
_H1_DURATION = re.compile(r"^2025年1[-－—–]6月$")


class InterimCashflowCorrectionError(ValueError):
    """A source page cannot establish the pinned corrected H1 CFO contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CorrectedInterimCashflowProof:
    """A two-period native-row proof, with no formal-admission semantics."""

    current_amount_yuan: Decimal
    prior_amount_yuan: Decimal
    evidence: Mapping[str, object]

    def as_evidence(self) -> dict[str, object]:
        return {
            **dict(self.evidence),
            "current_amount_yuan": str(self.current_amount_yuan),
            "prior_amount_yuan": str(self.prior_amount_yuan),
        }


def _require_identity(
    *, instrument_id: str, announcement_id: str, document_url: str,
    pdf_sha256: str, period_end: date,
) -> None:
    if instrument_id != TARGET_INSTRUMENT_ID:
        raise InterimCashflowCorrectionError(
            "wrong_document", "the correction proof is pinned to issuer 601069")
    if announcement_id != TARGET_ANNOUNCEMENT_ID or document_url != TARGET_DOCUMENT_URL:
        raise InterimCashflowCorrectionError(
            "wrong_document", "announcement id and official PDF URL do not match")
    if pdf_sha256 != TARGET_PDF_SHA256:
        raise InterimCashflowCorrectionError(
            "wrong_document", "PDF bytes do not match the saved correction identity")
    if period_end != TARGET_PERIOD_END:
        raise InterimCashflowCorrectionError(
            "wrong_period", "this contract covers only 2025-06-30 H1")


def _native_value_cell(
    page: Mapping[str, object], *, table_index: int, row_index: int,
    cell_index: int, cell_text: str, bbox: object,
) -> dict[str, object]:
    try:
        return _native._native_cell(
            page, table_index=table_index, row_index=row_index,
            cell_index=cell_index, cell_text=cell_text, bbox=bbox,
        )
    except _native.OperatingCashflowSubtotalsNotLocatedError as exc:
        raise InterimCashflowCorrectionError(
            "native_cell_invalid", str(exc)) from exc


def _native_label_cell(
    page: Mapping[str, object], *, cell_text: str, bbox: object,
) -> dict[str, object]:
    if (not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or
            not all(isinstance(value, (int, float)) for value in bbox)):
        raise InterimCashflowCorrectionError(
            "native_cell_invalid", "the CFO label cell has no native bounding box")
    x0, top, x1, bottom = (float(value) for value in bbox)
    raw_chars = page.get("chars")
    if not isinstance(raw_chars, (list, tuple)):
        raise InterimCashflowCorrectionError(
            "native_cell_invalid", "page 13 native characters are missing")
    chars = []
    for char in raw_chars:
        if not isinstance(char, Mapping) or not all(
                key in char for key in ("text", "x0", "x1", "top", "bottom")):
            continue
        cx = (float(char["x0"]) + float(char["x1"])) / 2
        cy = (float(char["top"]) + float(char["bottom"])) / 2
        if x0 <= cx <= x1 and top <= cy <= bottom:
            if (float(char["x0"]) < x0 - 1.0 or float(char["x1"]) > x1 + 1.0 or
                    float(char["top"]) < top - 1.0 or float(char["bottom"]) > bottom + 1.0):
                raise InterimCashflowCorrectionError(
                    "native_cell_invalid", "the CFO label glyph exceeds its cell boundary")
            chars.append(char)
    chars.sort(key=lambda char: (float(char["top"]), float(char["x0"])))
    native_text = "".join(str(char.get("text", "")) for char in chars)
    if not chars or _native._compact(native_text) != _native._compact(cell_text):
        raise InterimCashflowCorrectionError(
            "native_cell_conflict", "native glyphs do not reconstruct the CFO label cell")
    return {
        "text": native_text,
        "bbox": [x0, top, x1, bottom],
        "chars": [_native._char_evidence(char) for char in chars],
    }


def _proof_from_observation(
    *, instrument_id: str, announcement_id: str, document_url: str,
    pdf_sha256: str, period_end: date, cover_text: str,
    statement_page: Mapping[str, object],
) -> CorrectedInterimCashflowProof:
    """Validate one saved/native page observation without opening a PDF.

    This narrow seam is also used by the finite saved-JSON contract checks.
    Runtime callers reach it only after extracting physical pages 1 and 13 from
    the supplied PDF bytes.
    """

    _require_identity(
        instrument_id=instrument_id, announcement_id=announcement_id,
        document_url=document_url, pdf_sha256=pdf_sha256, period_end=period_end,
    )
    cover = _native._compact(cover_text)
    if ("西部黄金股份有限公司" not in cover or
            "关于前期会计差错更正后的财务报表及附注" not in cover or
            "更正后的财务报表及附注如下" not in cover):
        raise InterimCashflowCorrectionError(
            "wrong_scope", "page 1 does not identify the issuer's corrected statements")

    if statement_page.get("physicalPage") != CONSOLIDATED_H1_CASHFLOW_PAGE:
        raise InterimCashflowCorrectionError(
            "wrong_scope", "the consolidated H1 statement must come from physical page 13")
    try:
        lines = _native._native_lines(statement_page)
    except _native.OperatingCashflowSubtotalsNotLocatedError as exc:
        raise InterimCashflowCorrectionError("native_page_invalid", str(exc)) from exc
    headings = [
        line for line in lines
        if _native._statement_heading_kind(line) == ("合并", "现金流量表")
    ]
    if len(headings) != 1:
        raise InterimCashflowCorrectionError(
            "wrong_scope", "page 13 lacks one unique consolidated cash-flow title")
    heading = headings[0]

    duration_lines = [
        line for line in lines
        if line.top > heading.top and _H1_DURATION.fullmatch(_native._compact(line.text))
    ]
    if len(duration_lines) != 1:
        raise InterimCashflowCorrectionError(
            "wrong_period", "page 13 does not uniquely state 2025年1—6月")

    expected_current = _native._expected_period_label(period_end)
    expected_prior = (expected_current[0] - 1, expected_current[1])
    header_candidates = []
    for line in lines:
        if line.top <= heading.top or "项目" not in _native._compact(line.text):
            continue
        dated = [(token, _native._period_token(token)) for token in line.tokens]
        current = [token for token, period in dated if period == expected_current]
        prior = [token for token, period in dated if period == expected_prior]
        if len(current) == 1 and len(prior) == 1 and current[0].center < prior[0].center:
            header_candidates.append((line, current[0], prior[0]))
    if len(header_candidates) != 1:
        raise InterimCashflowCorrectionError(
            "wrong_period", "current and prior H1 headers are not unique and ordered")
    header_line, current_header, prior_header = header_candidates[0]
    if not heading.top < duration_lines[0].top < header_line.top:
        raise InterimCashflowCorrectionError(
            "wrong_period", "the H1 duration is outside the statement header")

    unit_lines = []
    for line in lines:
        if not heading.top < line.top < header_line.top:
            continue
        match = _native._UNIT.search(_native._compact(line.text))
        if match is not None:
            unit_lines.append((line, match.group(1)))
    if len(unit_lines) != 1:
        raise InterimCashflowCorrectionError(
            "unit_missing", "page 13 must declare one statement amount unit")
    unit_line, unit = unit_lines[0]
    currency_text = _native._compact(unit_line.text).replace("：", ":")
    if "币种:人民币" not in currency_text or unit not in _native._UNIT_SCALE:
        raise InterimCashflowCorrectionError(
            "unit_conflict", "page 13 must declare RMB and a supported amount unit")

    physical_page = CONSOLIDATED_H1_CASHFLOW_PAGE
    native_scope = _native._statement_page_scopes(
        [statement_page], {physical_page: lines}, heading=heading,
    ).get(physical_page)
    if native_scope is None:
        raise InterimCashflowCorrectionError(
            "wrong_scope", "the native statement scope does not include its title")
    width, height = _native._page_dimensions(statement_page)
    # The source observation has no next statement title on page 13.  Its native
    # page edge is therefore the explicit after-bound; do not extend the scan to
    # a continuation page or borrow text from another report section.
    scoped_page = {
        **native_scope,
        "end_before_y": (height if native_scope.get("end_before_y") is None
                         else native_scope["end_before_y"]),
    }
    page_scopes = {physical_page: scoped_page}

    operating_headings = [
        line for line in lines
        if _OPERATING_SECTION.fullmatch(_native._compact(line.text))
        and line.top > header_line.top
    ]
    if len(operating_headings) != 1:
        raise InterimCashflowCorrectionError(
            "wrong_scope", "page 13 does not uniquely bound the operating-activities section")
    operating_heading = operating_headings[0]

    matches: list[tuple[int, Mapping[str, object], int, int, object, object]] = []
    tables = statement_page.get("tables")
    if not isinstance(tables, (list, tuple)):
        raise InterimCashflowCorrectionError(
            "native_table_missing", "page 13 has no native table observations")
    for table in tables:
        if not isinstance(table, Mapping):
            continue
        table_index, rows = table.get("tableIndex"), table.get("rows")
        if not isinstance(table_index, int) or not isinstance(rows, (list, tuple)):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            row_index = row.get("rowIndex")
            cells, boxes = row.get("nativeCells"), row.get("cellBboxes")
            if (not isinstance(row_index, int) or not isinstance(cells, (list, tuple)) or
                    not isinstance(boxes, (list, tuple)) or len(cells) != len(boxes)):
                continue
            label_cells = [
                index for index, value in enumerate(cells)
                if value is not None and _native._compact(str(value)) == _CFO_LABEL
            ]
            if not label_cells:
                continue
            if len(label_cells) != 1:
                raise InterimCashflowCorrectionError(
                    "native_row_ambiguous", "the CFO label cell is duplicated in one row")
            label_index = label_cells[0]
            label_box = boxes[label_index]
            if not _native._row_is_within_statement_scope(label_box, scoped_page):
                continue
            if not operating_heading.bottom < float(label_box[1]):
                raise InterimCashflowCorrectionError(
                    "wrong_scope", "the CFO row precedes the operating-activities heading")
            numeric = [
                index for index, value in enumerate(cells)
                if index != label_index and value is not None and
                _native._amount(value) is not None
            ]
            if len(numeric) != 2:
                raise InterimCashflowCorrectionError(
                    "wrong_period", "the CFO row must contain exactly two period cells")
            matches.append((table_index, table, row, row_index, label_index, (cells, boxes, numeric)))
    if len(matches) != 1:
        raise InterimCashflowCorrectionError(
            "native_row_missing", "one scoped consolidated CFO row was not located")

    table_index, cfo_table, cfo_row, row_index, label_index, row_data = matches[0]
    cells, boxes, numeric_indexes = row_data
    label_box = boxes[label_index]
    label_cell = _native_label_cell(
        statement_page, cell_text=str(cells[label_index]), bbox=label_box,
    )
    table_rows = cfo_table.get("rows")
    if not isinstance(table_rows, (list, tuple)):
        raise InterimCashflowCorrectionError(
            "native_table_invalid", "the selected CFO table has no row sequence")
    later_rows = [
        row for row in table_rows
        if isinstance(row, Mapping) and isinstance(row.get("rowIndex"), int)
        and row["rowIndex"] > row_index
        and any(value is not None and str(value).strip()
                for value in (row.get("nativeCells") or ()))
    ]
    if later_rows:
        raise InterimCashflowCorrectionError(
            "wrong_scope", "the CFO row is not the final populated row in its native table")
    trailing_lines = [line for line in lines if line.top >= float(label_box[3])]
    if trailing_lines:
        raise InterimCashflowCorrectionError(
            "wrong_scope", "native text continues below the CFO closing row on page 13")
    column_separator = (current_header.center + prior_header.center) / 2
    current_indexes = []
    prior_indexes = []
    cfo_cells = []
    for index in numeric_indexes:
        native_cell = _native_value_cell(
            statement_page, table_index=table_index, row_index=row_index,
            cell_index=index, cell_text=str(cells[index]), bbox=boxes[index],
        )
        center = (native_cell["bbox"][0] + native_cell["bbox"][2]) / 2
        if center < column_separator:
            current_indexes.append(index)
            cfo_cells.append(("current", native_cell))
        elif center > column_separator:
            prior_indexes.append(index)
            cfo_cells.append(("prior", native_cell))
        else:
            raise InterimCashflowCorrectionError(
                "wrong_period", "a CFO cell lies on the period-column separator")
    if len(current_indexes) != 1 or len(prior_indexes) != 1:
        raise InterimCashflowCorrectionError(
            "wrong_period", "CFO current and prior cells are not uniquely aligned")
    current_cell = next(cell for key, cell in cfo_cells if key == "current")
    prior_cell = next(cell for key, cell in cfo_cells if key == "prior")

    try:
        inflow = _native._find_subtotal_row(
            [statement_page], label="经营活动现金流入小计",
            column_separator=column_separator, page_scopes=page_scopes,
        )
        outflow = _native._find_subtotal_row(
            [statement_page], label="经营活动现金流出小计",
            column_separator=column_separator, page_scopes=page_scopes,
        )
    except _native.OperatingCashflowSubtotalsNotLocatedError as exc:
        raise InterimCashflowCorrectionError(
            "subtotal_missing", str(exc)) from exc

    scale = _native._UNIT_SCALE[unit]
    current_yuan = current_cell["amount"] * scale
    prior_yuan = prior_cell["amount"] * scale
    current_recomputed = (
        inflow["current_cell"]["amount"] - outflow["current_cell"]["amount"]
    ) * scale
    prior_recomputed = (
        inflow["prior_cell"]["amount"] - outflow["prior_cell"]["amount"]
    ) * scale
    if current_yuan != current_recomputed or prior_yuan != prior_recomputed:
        raise InterimCashflowCorrectionError(
            "amount_conflict",
            "reported CFO differs from native inflow less outflow in at least one period",
        )

    scope_evidence = {
        "physical_page": physical_page,
        "start_after_y": scoped_page["start_after_y"],
        "start_heading": scoped_page["start_heading"],
        "end_before_y": scoped_page["end_before_y"],
        "end_heading": scoped_page["end_heading"],
        "end_boundary": ("next_statement_heading" if native_scope.get("end_before_y") is not None
                         else "physical_page_bottom"),
        "operating_section_heading": _native._line_evidence(operating_heading),
        "cfo_label_bbox": label_cell["bbox"],
        "cfo_row_within_scope": True,
        "cfo_row_closes_native_table": True,
        "last_native_line": _native._line_evidence(lines[-1]),
    }
    evidence: dict[str, object] = {
        "schema": PROOF_SCHEMA,
        "status": "CANDIDATE_SOURCE_PROOF_NOT_ADMITTED",
        "formal_admission": "NOT_ADMITTED",
        "document": {
            "instrument_id": instrument_id,
            "announcement_id": announcement_id,
            "document_url": document_url,
            "pdf_sha256": pdf_sha256,
        },
        "period_end": period_end.isoformat(),
        "pages_read": [CORRECTION_COVER_PAGE, CONSOLIDATED_H1_CASHFLOW_PAGE],
        "correction_cover": {
            "physical_page": CORRECTION_COVER_PAGE,
            "issuer_title": "西部黄金股份有限公司",
            "document_title": "关于前期会计差错更正后的财务报表及附注",
            "corrected_statements_declared": True,
        },
        "statement": {
            "physical_page": physical_page,
            "role": "合并",
            "statement": "现金流量表",
            "heading": _native._line_evidence(heading),
            "h1_duration": _native._line_evidence(duration_lines[0]),
            "current_header": current_header.as_evidence(),
            "prior_header": prior_header.as_evidence(),
            "header_line": _native._line_evidence(header_line),
            "unit": unit,
            "currency": "人民币",
            "unit_declaration": _native._line_evidence(unit_line),
            "scope": scope_evidence,
        },
        "reported_cfo": {
            "label": _CFO_LABEL,
            "table_index": table_index,
            "row_index": row_index,
            "label_cell": label_cell,
            "current_cell": _native._public_cell(current_cell),
            "prior_cell": _native._public_cell(prior_cell),
            "current_amount_yuan": str(current_yuan),
            "prior_amount_yuan": str(prior_yuan),
        },
        "operating_inflow_outflow_check": {
            "current": {
                "inflow_cell": _native._public_cell(inflow["current_cell"]),
                "outflow_cell": _native._public_cell(outflow["current_cell"]),
                "recomputed_yuan": str(current_recomputed),
                "reported_yuan": str(current_yuan),
                "holds": True,
            },
            "prior": {
                "inflow_cell": _native._public_cell(inflow["prior_cell"]),
                "outflow_cell": _native._public_cell(outflow["prior_cell"]),
                "recomputed_yuan": str(prior_recomputed),
                "reported_yuan": str(prior_yuan),
                "holds": True,
            },
            "amount_unit_scale": str(scale),
        },
    }
    return CorrectedInterimCashflowProof(current_yuan, prior_yuan, evidence)


def extract_corrected_h1_operating_cashflow(
    pdf_bytes: bytes,
    *,
    instrument_id: str,
    announcement_id: str,
    document_url: str,
    period_end: date,
) -> CorrectedInterimCashflowProof:
    """Extract only physical pages 1 and 13 from the pinned correction PDF.

    The saved source has a correction-statement cover and a native H1 table page;
    this announcement does not print the original annual-report title.  The
    public section parser's full-document scan and the subtotal wrapper's
    ``半年度报告`` title requirement therefore do not apply here.  Their native
    page, geometry, period-column, scope, row-cell and subtotal helpers are used
    directly without broadening the read range.
    """

    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise InterimCashflowCorrectionError("invalid_pdf", "input must be PDF bytes")
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    _require_identity(
        instrument_id=instrument_id, announcement_id=announcement_id,
        document_url=document_url, pdf_sha256=digest, period_end=period_end,
    )
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - runtime dependency guard
        raise InterimCashflowCorrectionError(
            "pdfplumber_missing", "pdfplumber is required for native page extraction") from exc
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if len(pdf.pages) < CONSOLIDATED_H1_CASHFLOW_PAGE:
                raise InterimCashflowCorrectionError(
                    "invalid_pdf", "the pinned H1 statement page is absent")
            cover_text = pdf.pages[CORRECTION_COVER_PAGE - 1].extract_text() or ""
            statement_page = _native._pdf_page_observation(
                pdf.pages[CONSOLIDATED_H1_CASHFLOW_PAGE - 1],
                physical_page=CONSOLIDATED_H1_CASHFLOW_PAGE,
            )
    except InterimCashflowCorrectionError:
        raise
    except Exception as exc:
        raise InterimCashflowCorrectionError(
            "native_page_read_failed", f"native page reading failed: {type(exc).__name__}") from exc
    return _proof_from_observation(
        instrument_id=instrument_id, announcement_id=announcement_id,
        document_url=document_url, pdf_sha256=digest, period_end=period_end,
        cover_text=cover_text, statement_page=statement_page,
    )


__all__ = [
    "CorrectedInterimCashflowProof",
    "InterimCashflowCorrectionError",
    "extract_corrected_h1_operating_cashflow",
]
