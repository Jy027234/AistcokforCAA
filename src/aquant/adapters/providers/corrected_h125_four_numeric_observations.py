"""Source-only native observations for the corrected 601069 H125 statements.

Announcement 1225082560 contains multiple corrected periods and is an
attachment of corrected financial statements, not an H125 full report.  This
module reads only physical pages 8 through 12 and returns the four values with
their native row, glyph, header, unit, and adjacent-page context.  It does not
decide source admission or normal eligibility.
"""

from __future__ import annotations

import copy
import hashlib
import io
import math
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
TARGET_PDF_BYTE_SIZE = 247783
TARGET_PERIOD_END = date(2025, 6, 30)
SOURCE_SECTION_PERIOD_END = date(2025, 6, 30)
SOURCE_PHYSICAL_PAGES = (8, 9, 10, 11, 12)
PROOF_SCHEMA = "aquant.corrected-h125-four-numeric-native-source-proof.v1"

_BALANCE_TITLE = "合并资产负债表"
_INCOME_TITLE = "合并利润表"
_PARENT_EQUITY_LABEL = "归属于母公司所有者权益"
_PARENT_EQUITY_SUFFIX = "（或股东权益）合计"
_NET_PROFIT_LABEL = "五、净利润（净亏损以“－”号填列）"
_ATTRIBUTABLE_LABEL = "1.归属于母公司股东的净利润"
_ATTRIBUTABLE_SUFFIX = "（净亏损以“-”号填列）"
_REVENUE_LABEL = "其中：营业收入"
_H1_DURATION = re.compile(r"^2025年1[-－—–]6月$")
_BALANCE_DATE = re.compile(r"^(20\d{2})年(\d{1,2})月(\d{1,2})日$")
_RMB = re.compile(r"币种[:：]人民币")
_GEOMETRY_TOLERANCE = 1.0


class CorrectedH125SourceError(ValueError):
    """The pinned correction pages do not support the requested observation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CorrectedH125FourNumericProof:
    """Immutable source-only candidate; formal admission is always withheld."""

    evidence: Mapping[str, object]

    def as_evidence(self) -> dict[str, object]:
        return copy.deepcopy(dict(self.evidence))


def _require_identity(
    *, instrument_id: str, announcement_id: str, document_url: str,
    pdf_sha256: str, pdf_byte_size: int, period_end: date,
) -> None:
    if instrument_id != TARGET_INSTRUMENT_ID:
        raise CorrectedH125SourceError(
            "wrong_document", "the correction observation is pinned to issuer 601069")
    if announcement_id != TARGET_ANNOUNCEMENT_ID or document_url != TARGET_DOCUMENT_URL:
        raise CorrectedH125SourceError(
            "wrong_document", "announcement id and official PDF URL do not match")
    if pdf_sha256.removeprefix("sha256:") != TARGET_PDF_SHA256:
        raise CorrectedH125SourceError(
            "wrong_document", "PDF bytes do not match the saved correction identity")
    if pdf_byte_size != TARGET_PDF_BYTE_SIZE:
        raise CorrectedH125SourceError(
            "wrong_document", "PDF byte size does not match the saved correction identity")
    if period_end != TARGET_PERIOD_END:
        raise CorrectedH125SourceError(
            "wrong_period", "this source section covers 2025-06-30 H1")


def _compact(text: str) -> str:
    return _native._compact(text)


def _page_lines(page: Mapping[str, object], expected_page: int) -> tuple[object, ...]:
    if page.get("physicalPage") != expected_page:
        raise CorrectedH125SourceError(
            "wrong_page", f"expected native physical page {expected_page}")
    width, height = page.get("width"), page.get("height")
    raw_chars = page.get("chars")
    native_text = page.get("nativeText")
    if (not isinstance(width, (int, float)) or not isinstance(height, (int, float)) or
            not math.isfinite(float(width)) or not math.isfinite(float(height)) or
            float(width) <= 0 or float(height) <= 0):
        raise CorrectedH125SourceError(
            "native_page_invalid", f"page {expected_page} dimensions are missing")
    if not isinstance(raw_chars, (list, tuple)) or not raw_chars:
        raise CorrectedH125SourceError(
            "native_page_invalid", f"page {expected_page} native characters are missing")
    if not isinstance(native_text, str) or not native_text.strip():
        raise CorrectedH125SourceError(
            "native_page_invalid", f"page {expected_page} native text is missing")
    required = ("text", "x0", "x1", "top", "bottom")
    for char in raw_chars:
        if not isinstance(char, Mapping) or not all(key in char for key in required):
            raise CorrectedH125SourceError(
                "native_page_invalid", f"page {expected_page} has incomplete glyph geometry")
        if not isinstance(char["text"], str):
            raise CorrectedH125SourceError(
                "native_page_invalid", f"page {expected_page} has non-text glyph data")
        try:
            x0, x1, top, bottom = (float(char[key]) for key in ("x0", "x1", "top", "bottom"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise CorrectedH125SourceError(
                "native_page_invalid", f"page {expected_page} has invalid glyph coordinates") from exc
        if (not all(math.isfinite(value) for value in (x0, x1, top, bottom)) or
                x0 >= x1 or top >= bottom or x0 < -_GEOMETRY_TOLERANCE or
                top < -_GEOMETRY_TOLERANCE or x1 > float(width) + _GEOMETRY_TOLERANCE or
                bottom > float(height) + _GEOMETRY_TOLERANCE):
            raise CorrectedH125SourceError(
                "native_page_invalid", f"page {expected_page} has out-of-page glyph geometry")
    try:
        lines = _native._native_lines(page)
    except _native.OperatingCashflowSubtotalsNotLocatedError as exc:
        raise CorrectedH125SourceError("native_page_invalid", str(exc)) from exc
    reconstructed = _compact(" ".join(line.text for line in lines))
    if reconstructed != _compact(native_text):
        raise CorrectedH125SourceError(
            "native_text_geometry_conflict",
            f"page {expected_page} text does not reconstruct from its native glyphs",
        )
    return lines


def _line_evidence(line: object) -> dict[str, object]:
    return _native._line_evidence(line)


def _token_evidence(token: object, *, unit_scale: Decimal) -> dict[str, object]:
    evidence = token.as_evidence()
    amount = _native._amount(token.text)
    if amount is None:
        raise CorrectedH125SourceError("native_cell_invalid", "a selected amount is not numeric")
    # The page has no native table-cell object.  This box is the exact bounding
    # rectangle of the source glyphs composing the native amount token.
    return {
        **evidence,
        "amountYuan": str(amount * unit_scale),
        "geometryBasis": "EXACT_NATIVE_GLYPH_EXTENTS",
        "cellRepresentation": "NATIVE_AMOUNT_TOKEN",
    }


def _balance_date(token: object) -> date | None:
    match = _BALANCE_DATE.fullmatch(_compact(token.text))
    if match is None:
        return None
    try:
        return date(*(int(value) for value in match.groups()))
    except ValueError:
        return None


def _period_for_header(token: object, *, statement: str) -> date | None:
    if statement == "资产负债表":
        return _balance_date(token)
    period = _native._period_token(token)
    if period is None or period[1] != "H1":
        return None
    return date(period[0], 6, 30)


def _expected_prior(period_end: date) -> date:
    try:
        return period_end.replace(year=period_end.year - 1)
    except ValueError as exc:
        raise CorrectedH125SourceError("wrong_period", "invalid prior comparison date") from exc


def _period_headers(
    lines: Sequence[object], *, statement: str, period_end: date,
) -> tuple[object, object, object]:
    expected_prior = _expected_prior(period_end)
    candidates = []
    for line in lines:
        if "项目" not in _compact(line.text):
            continue
        current = [token for token in line.tokens
                   if _period_for_header(token, statement=statement) == period_end]
        prior = [token for token in line.tokens
                 if _period_for_header(token, statement=statement) == expected_prior]
        if len(current) == 1 and len(prior) == 1 and current[0].center < prior[0].center:
            candidates.append((line, current[0], prior[0]))
    if len(candidates) != 1:
        raise CorrectedH125SourceError(
            "wrong_period", f"{statement} current/prior headers are absent, duplicated, or reversed")
    return candidates[0]


def _unit_context(
    lines: Sequence[object], *, lower_y: float, upper_y: float, physical_page: int,
) -> tuple[object, str, Decimal]:
    found = []
    for line in lines:
        if not lower_y < line.top < upper_y:
            continue
        match = _native._UNIT.search(_compact(line.text))
        if match is not None:
            found.append((line, match.group(1)))
    if len(found) != 1:
        raise CorrectedH125SourceError(
            "unit_missing", f"page {physical_page} must declare one statement amount unit")
    line, unit = found[0]
    if _RMB.search(_compact(line.text)) is None or unit not in _native._UNIT_SCALE:
        raise CorrectedH125SourceError(
            "unit_conflict", f"page {physical_page} must declare RMB and a supported unit")
    return line, unit, _native._UNIT_SCALE[unit]


def _statement_heading(lines: Sequence[object], *, entity: str, statement: str) -> object:
    matches = [line for line in lines
               if _native._statement_heading_kind(line) == (entity, statement)]
    if len(matches) != 1:
        raise CorrectedH125SourceError(
            "wrong_scope", f"page lacks one unique {entity}{statement} title")
    return matches[0]


def _numeric_tokens(line: object) -> list[object]:
    return [token for token in line.tokens if _native._amount(token.text) is not None]


def _select_pair(
    amount_line: object, *, current_header: object, prior_header: object,
    unit_scale: Decimal,
) -> tuple[dict[str, object], dict[str, object]]:
    numbers = _numeric_tokens(amount_line)
    if len(numbers) != 2:
        raise CorrectedH125SourceError(
            "wrong_period", "the selected native row must contain exactly two amount cells")
    separator = (current_header.center + prior_header.center) / 2
    current = [token for token in numbers if token.center < separator]
    prior = [token for token in numbers if token.center > separator]
    if len(current) != 1 or len(prior) != 1:
        raise CorrectedH125SourceError(
            "wrong_period", "native amounts do not align uniquely with current/prior columns")
    return (
        _token_evidence(current[0], unit_scale=unit_scale),
        _token_evidence(prior[0], unit_scale=unit_scale),
    )


def _direct_row_pair(
    lines: Sequence[object], *, label: str, current_header: object,
    prior_header: object, unit_scale: Decimal,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    expected = _compact(label)
    matches = [line for line in lines if line.tokens and _compact(line.tokens[0].text) == expected]
    if len(matches) != 1:
        raise CorrectedH125SourceError(
            "native_row_ambiguous", f"expected one native row labelled {label}")
    line = matches[0]
    current, prior = _select_pair(
        line, current_header=current_header, prior_header=prior_header,
        unit_scale=unit_scale,
    )
    return current, prior, _line_evidence(line)


def _wrapped_row_pair(
    lines: Sequence[object], *, label: str, suffix: str,
    current_header: object, prior_header: object, unit_scale: Decimal,
) -> tuple[dict[str, object], dict[str, object], list[dict[str, object]]]:
    expected_label, expected_suffix = _compact(label), _compact(suffix)
    label_indexes = [index for index, line in enumerate(lines)
                     if line.tokens and _compact(line.tokens[0].text) == expected_label]
    if len(label_indexes) != 1:
        raise CorrectedH125SourceError(
            "native_row_ambiguous", f"expected one native row label {label}")
    index = label_indexes[0]
    if index + 2 >= len(lines):
        raise CorrectedH125SourceError(
            "native_row_incomplete", f"native row {label} is missing adjacent cell or suffix")
    amount_line, suffix_line = lines[index + 1], lines[index + 2]
    if _compact(suffix_line.text) != expected_suffix:
        raise CorrectedH125SourceError(
            "native_row_incomplete", f"native row {label} lacks its adjacent label continuation")
    if len(amount_line.tokens) != 2 or len(_numeric_tokens(amount_line)) != 2:
        raise CorrectedH125SourceError(
            "native_cell_invalid", f"native row {label} has an ambiguous amount line")
    current, prior = _select_pair(
        amount_line, current_header=current_header, prior_header=prior_header,
        unit_scale=unit_scale,
    )
    return current, prior, [
        _line_evidence(lines[index]), _line_evidence(amount_line), _line_evidence(suffix_line),
    ]


def _field_cell(
    *, value: dict[str, object], header: object, role: str,
    period_end: date, period_kind: str, physical_pages: Sequence[int],
) -> dict[str, object]:
    return {
        "columnRole": role,
        "factPeriodEnd": period_end.isoformat(),
        "periodKind": period_kind,
        "amountYuan": value["amountYuan"],
        "amountUnit": "元",
        "currency": "CNY",
        "sourceColumnHeader": header.as_evidence(),
        "sourcePhysicalPages": list(physical_pages),
        "nativeCell": value,
    }


def _native_observation_pair(
    *, current: dict[str, object], prior: dict[str, object],
    current_header: object, prior_header: object, period_kind: str,
    physical_pages: Sequence[int],
) -> dict[str, object]:
    return {
        "current": _field_cell(
            value=current, header=current_header, role="CURRENT",
            period_end=TARGET_PERIOD_END, period_kind=period_kind,
            physical_pages=physical_pages,
        ),
        "prior": _field_cell(
            value=prior, header=prior_header, role="PRIOR",
            period_end=_expected_prior(TARGET_PERIOD_END), period_kind=period_kind,
            physical_pages=physical_pages,
        ),
    }


def _proof_from_observations(
    *, instrument_id: str, announcement_id: str, document_url: str,
    pdf_sha256: str, pdf_byte_size: int, period_end: date,
    page_observations: Mapping[int, Mapping[str, object]],
) -> CorrectedH125FourNumericProof:
    """Build a source-only candidate from saved native page JSON, without a PDF."""

    _require_identity(
        instrument_id=instrument_id, announcement_id=announcement_id,
        document_url=document_url, pdf_sha256=pdf_sha256,
        pdf_byte_size=pdf_byte_size, period_end=period_end,
    )
    lines_by_page: dict[int, tuple[object, ...]] = {}
    page_errors: dict[int, dict[str, str]] = {}
    for page_number in SOURCE_PHYSICAL_PAGES:
        page = page_observations.get(page_number)
        if page is None:
            page_errors[page_number] = {
                "code": "missing_native_page",
                "message": f"native observation for physical page {page_number} is absent",
            }
            continue
        lines_by_page[page_number] = _page_lines(page, page_number)

    fields: dict[str, object] = {}
    incomplete: dict[str, object] = {}

    # Income statement context begins at the bottom of p10, continues through
    # the p11 column header, and reaches the H125 profit rows on p12.
    if all(number in lines_by_page for number in (10, 11, 12)):
        p10, p11, p12 = (lines_by_page[number] for number in (10, 11, 12))
        income_heading = _statement_heading(p10, entity="合并", statement="利润表")
        durations = [line for line in p10 if _H1_DURATION.fullmatch(_compact(line.text))]
        if len(durations) != 1 or not income_heading.top < durations[0].top:
            raise CorrectedH125SourceError(
                "wrong_period", "p10 does not uniquely declare the 2025 H1 income statement")
        header_line, income_current, income_prior = _period_headers(
            p11, statement="利润表", period_end=period_end,
        )
        if header_line.top <= 0 or 11 != 10 + 1 or 12 != 11 + 1:
            raise CorrectedH125SourceError(
                "wrong_scope", "income statement continuation pages are not consecutive")
        unit_line, income_unit, income_scale = _unit_context(
            p10, lower_y=durations[0].top, upper_y=float("inf"), physical_page=10,
        )
        if income_unit != "元":
            raise CorrectedH125SourceError(
                "unit_conflict", "the saved H125 correction rows are not stated in yuan")
        if any(_native._statement_heading_kind(line) for line in p11):
            raise CorrectedH125SourceError(
                "wrong_scope", "p11 contains a new statement title inside the income continuation")

        revenue_current, revenue_prior, revenue_line = _direct_row_pair(
            p11, label=_REVENUE_LABEL, current_header=income_current,
            prior_header=income_prior, unit_scale=income_scale,
        )
        net_current, net_prior, net_line = _direct_row_pair(
            p12, label=_NET_PROFIT_LABEL, current_header=income_current,
            prior_header=income_prior, unit_scale=income_scale,
        )
        attr_current, attr_prior, attr_lines = _wrapped_row_pair(
            p12, label=_ATTRIBUTABLE_LABEL, suffix=_ATTRIBUTABLE_SUFFIX,
            current_header=income_current, prior_header=income_prior,
            unit_scale=income_scale,
        )
        first_profit_row = min(
            line.top for line in p12
            if line.tokens and _compact(line.tokens[0].text) in {
                _compact(_NET_PROFIT_LABEL), _compact(_ATTRIBUTABLE_LABEL),
            }
        )
        if any(_native._statement_heading_kind(line) and line.top < first_profit_row
               for line in p12):
            raise CorrectedH125SourceError(
                "wrong_scope", "p12 begins a new statement before the selected income rows")
        income_context = {
            "documentRole": "CORRECTED_FINANCIAL_STATEMENTS",
            "statementScope": "CONSOLIDATED",
            "statement": "利润表",
            "sourceSectionPeriodEnd": SOURCE_SECTION_PERIOD_END.isoformat(),
            "h1Duration": _line_evidence(durations[0]),
            "statementHeading": _line_evidence(income_heading),
            "unitDeclaration": _line_evidence(unit_line),
            "unit": income_unit,
            "currency": "CNY",
            "headerLine": _line_evidence(header_line),
        }
        field_values = (
            ("revenue", revenue_current, revenue_prior, revenue_line, (11,)),
            ("net_profit_consolidated", net_current, net_prior, net_line, (12,)),
            ("net_profit_attributable", attr_current, attr_prior, attr_lines, (12,)),
        )
        for metric, current, prior, row_evidence, physical_pages in field_values:
            fields[metric] = {
                "status": "CANDIDATE_SOURCE_PROOF_NOT_ADMITTED",
                **_native_observation_pair(
                    current=current, prior=prior, current_header=income_current,
                    prior_header=income_prior, period_kind="H1",
                    physical_pages=physical_pages,
                ),
                "rowEvidence": row_evidence,
                "statementContext": income_context,
                "pageContinuity": {
                    "contextPage": 10,
                    "headerAndRevenuePage": 11,
                    "profitRowsPage": 12,
                    "consecutiveNativePages": True,
                },
            }
    else:
        missing = [n for n in (10, 11, 12) if n not in lines_by_page]
        for metric in ("revenue", "net_profit_consolidated", "net_profit_attributable"):
            incomplete[metric] = {
                "status": "MISSING_NATIVE_CONTEXT",
                "missingPhysicalPages": missing,
                "reason": "the H1 income statement context, headers, or target row is absent",
            }

    # The balance sheet header and unit are on p8.  P9 is part of the native
    # continuation to the closing parent-equity row on p10; a missing p9 is an
    # explicit context gap, even when the p10 amount glyphs are readable.
    if 8 in lines_by_page and 10 in lines_by_page:
        p8, p10 = lines_by_page[8], lines_by_page[10]
        balance_heading = _statement_heading(p8, entity="合并", statement="资产负债表")
        balance_header_line, balance_current, balance_prior = _period_headers(
            p8, statement="资产负债表", period_end=period_end,
        )
        balance_unit_line, balance_unit, balance_scale = _unit_context(
            p8, lower_y=balance_heading.top, upper_y=balance_header_line.top,
            physical_page=8,
        )
        if balance_unit != "元":
            raise CorrectedH125SourceError(
                "unit_conflict", "the corrected H125 balance sheet is not stated in yuan")
        eq_current, eq_prior, eq_rows = _wrapped_row_pair(
            p10, label=_PARENT_EQUITY_LABEL, suffix=_PARENT_EQUITY_SUFFIX,
            current_header=balance_current, prior_header=balance_prior,
            unit_scale=balance_scale,
        )
        parent_row_top = min(float(row["bbox"][1]) for row in eq_rows)
        later_income_titles = [line for line in p10
                               if _native._statement_heading_kind(line) == ("合并", "利润表")
                               and line.top > parent_row_top]
        if len(later_income_titles) != 1:
            raise CorrectedH125SourceError(
                "wrong_scope", "the p10 parent-equity row is not bounded before the next statement")
        if 9 not in lines_by_page:
            incomplete["parent_equity"] = {
                "status": "MISSING_CONTEXT_PHYSICAL_PAGE9",
                "missingPhysicalPages": [9],
                "reason": "p8 headers and p10 row are observed, but p9-to-p10 statement continuity is not saved",
                "observedBalanceHeader": {
                    "title": _line_evidence(balance_heading),
                    "headerLine": _line_evidence(balance_header_line),
                    "currentHeader": balance_current.as_evidence(),
                    "priorHeader": balance_prior.as_evidence(),
                    "unitDeclaration": _line_evidence(balance_unit_line),
                    "unit": balance_unit,
                    "currency": "CNY",
                },
                "observedNativeRowWithoutPeriodProof": {
                    "labelAndCells": eq_rows,
                    "observedCurrentAmountYuan": eq_current["amountYuan"],
                    "observedPriorAmountYuan": eq_prior["amountYuan"],
                    "sourcePhysicalPage": 10,
                },
            }
        else:
            p9 = lines_by_page[9]
            if any(_native._statement_heading_kind(line) for line in p9):
                raise CorrectedH125SourceError(
                    "wrong_scope", "p9 contains a new financial statement title before the p10 closing row")
            fields["parent_equity"] = {
                "status": "CANDIDATE_SOURCE_PROOF_NOT_ADMITTED",
                **_native_observation_pair(
                    current=eq_current, prior=eq_prior,
                    current_header=balance_current, prior_header=balance_prior,
                    period_kind="BALANCE_SHEET",
                    physical_pages=(8, 9, 10),
                ),
                "rowEvidence": eq_rows,
                "statementContext": {
                    "documentRole": "CORRECTED_FINANCIAL_STATEMENTS",
                    "statementScope": "CONSOLIDATED",
                    "statement": "资产负债表",
                    "sourceSectionPeriodEnd": SOURCE_SECTION_PERIOD_END.isoformat(),
                    "statementHeading": _line_evidence(balance_heading),
                    "currentAndPriorHeaderLine": _line_evidence(balance_header_line),
                    "unitDeclaration": _line_evidence(balance_unit_line),
                    "unit": balance_unit,
                    "currency": "CNY",
                },
                "pageContinuity": {
                    "headerPage": 8,
                    "continuationPage": 9,
                    "closingRowPage": 10,
                    "consecutiveNativePages": True,
                    "nextStatementHeadingAfterRow": _line_evidence(later_income_titles[0]),
                },
            }
    else:
        missing = [n for n in (8, 9, 10) if n not in lines_by_page]
        incomplete["parent_equity"] = {
            "status": "MISSING_NATIVE_CONTEXT",
            "missingPhysicalPages": missing,
            "reason": "the balance-sheet title/header, continuation, or parent-equity row is absent",
        }

    available_pages = sorted(lines_by_page)
    source_identity = {
        "instrumentId": instrument_id,
        "announcementId": announcement_id,
        "documentUrl": document_url,
        "documentSha256": f"sha256:{TARGET_PDF_SHA256}",
        "documentByteSize": TARGET_PDF_BYTE_SIZE,
        "documentTitle": "西部黄金股份有限公司关于前期会计差错更正后的财务报表及附注",
        "documentRole": "CORRECTED_FINANCIAL_STATEMENTS",
        "identityBasis": "INDEX_BOUND_ONLY",
        "literalSourceReportTypeCode": None,
        "documentContainsMultipleCorrectedSections": True,
        "sourceSectionPeriodEnd": SOURCE_SECTION_PERIOD_END.isoformat(),
    }
    evidence = {
        "schema": PROOF_SCHEMA,
        "status": ("CANDIDATE_SOURCE_PROOF_NOT_ADMITTED" if not incomplete
                   else "PARTIAL_NATIVE_OBSERVATION_NOT_ADMITTED"),
        "sourceOnly": True,
        "formalAdmission": "NOT_ADMITTED",
        "qualificationDecision": False,
        "document": source_identity,
        "requestedPhysicalPages": list(SOURCE_PHYSICAL_PAGES),
        "observedPhysicalPages": available_pages,
        "fields": fields,
        "incompleteFields": incomplete,
        "pageReadErrors": page_errors,
    }
    return CorrectedH125FourNumericProof(evidence)


def extract_corrected_h125_four_numeric_observations(
    pdf_bytes: bytes,
    *,
    instrument_id: str,
    announcement_id: str,
    document_url: str,
    period_end: date,
) -> CorrectedH125FourNumericProof:
    """Read only corrected-report pages 8–12 and return native source evidence.

    The function never treats the attachment as a full report or makes an
    admission decision.  Callers cannot supply booleans or value overrides for
    page identity, statement scope, current/prior roles, or cell geometry.
    """

    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise CorrectedH125SourceError("invalid_pdf", "input must be PDF bytes")
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    _require_identity(
        instrument_id=instrument_id, announcement_id=announcement_id,
        document_url=document_url, pdf_sha256=digest,
        pdf_byte_size=len(pdf_bytes), period_end=period_end,
    )
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - runtime dependency guard
        raise CorrectedH125SourceError(
            "pdfplumber_missing", "pdfplumber is required for native page extraction") from exc
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if len(pdf.pages) < max(SOURCE_PHYSICAL_PAGES):
                raise CorrectedH125SourceError(
                    "invalid_pdf", "the pinned H125 statement page range is incomplete")
            observations = {}
            for physical_page in SOURCE_PHYSICAL_PAGES:
                page = pdf.pages[physical_page - 1]
                observations[physical_page] = {
                    "physicalPage": physical_page,
                    "width": float(page.width),
                    "height": float(page.height),
                    "nativeText": page.extract_text() or "",
                    "chars": tuple(page.chars),
                }
    except CorrectedH125SourceError:
        raise
    except Exception as exc:
        raise CorrectedH125SourceError(
            "native_page_read_failed", f"native page reading failed: {type(exc).__name__}") from exc
    return _proof_from_observations(
        instrument_id=instrument_id, announcement_id=announcement_id,
        document_url=document_url, pdf_sha256=digest,
        pdf_byte_size=len(pdf_bytes), period_end=period_end,
        page_observations=observations,
    )


__all__ = [
    "CorrectedH125FourNumericProof",
    "CorrectedH125SourceError",
    "extract_corrected_h125_four_numeric_observations",
]
