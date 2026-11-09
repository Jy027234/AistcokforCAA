"""Isolated candidate for native annual identity spanning adjacent intro pages.

The input pages must come from one parsed PDF byte stream. This helper only
proves issuer identity; period-specific statement rows, units and values remain
the responsibility of the existing statement-section reader.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Sequence


_LEGAL_NAME = re.compile(
    r"[\u4e00-\u9fffA-Za-z（）()]{2,80}?(?:股份有限公司|股份公司|有限公司)"
)
_STOCK_SECTION = re.compile(r"^[一二三四五六七八九十]+[、.．]公司股票简况$")
_TITLE_YEAR = re.compile(r"(?<!\d)(20\d{2})年度报告")
_PROFILE_COLUMNS = ("股票种类", "股票上市交易所", "股票简称", "股票代码")
_EPSILON = 0.15
_000792_ANNUAL_FALLBACK_SOURCES = {
    date(2024, 12, 31): "ff1d94dac6d026ab61806a7a17a6f2173f45af5daee077ac176dca9217c3ad32",
    date(2025, 12, 31): "7b0a93a75d1956235137f94cee3106054524e058ebda42f5850e1a981f24518d",
}
_000792_COMPANY = "青海盐湖工业股份有限公司"


def is_source_bound_000792_annual_identity_fallback(
    pdf_bytes: bytes,
    *,
    code: str,
    company: str,
    period_end: date,
) -> bool:
    """Allow the new post-cover fallback only for the two reviewed 000792 PDFs."""
    if (
        not isinstance(pdf_bytes, bytes)
        or not pdf_bytes.startswith(b"%PDF-")
        or code != "000792"
        or company != _000792_COMPANY
        or period_end not in _000792_ANNUAL_FALLBACK_SOURCES
    ):
        return False
    return hashlib.sha256(pdf_bytes).hexdigest() == _000792_ANNUAL_FALLBACK_SOURCES[period_end]


@dataclass(frozen=True, slots=True)
class Box:
    x0: float
    top: float
    x1: float
    bottom: float


@dataclass(frozen=True, slots=True)
class PositionedText:
    text: str
    box: Box


@dataclass(frozen=True, slots=True)
class NativePage:
    """Native, positioned page text with both PDF boxes retained."""

    page_number: int
    width: float
    height: float
    media_box: tuple[float, float, float, float]
    crop_box: tuple[float, float, float, float]
    positioned_lines: tuple[PositionedText, ...]
    words: tuple[PositionedText, ...]

    @classmethod
    def from_saved_page(cls, page: dict) -> "NativePage":
        """Adapt one page record from the saved native-source JSON."""
        size = page["pageSize"]
        return cls(
            page_number=int(page["pageNumber"]),
            width=float(size["pdfplumberWidth"]),
            height=float(size["pdfplumberHeight"]),
            media_box=tuple(float(value) for value in size["pypdfMediaBox"]),
            crop_box=tuple(float(value) for value in size["pypdfCropBox"]),
            positioned_lines=tuple(
                PositionedText(
                    text=str(line["text"]),
                    box=Box(
                        float(line["bbox"]["x0"]),
                        float(line["bbox"]["top"]),
                        float(line["bbox"]["x1"]),
                        float(line["bbox"]["bottom"]),
                    ),
                )
                for line in page["positionedLines"]
            ),
            words=tuple(
                PositionedText(
                    text=str(word["text"]),
                    box=Box(
                        float(word["x0"]),
                        float(word["top"]),
                        float(word["x1"]),
                        float(word["bottom"]),
                    ),
                )
                for word in page["words"]
            ),
        )

    def supports_zero_origin_boxes(self) -> bool:
        """Fail closed outside the page geometry represented by this evidence."""
        if not math.isfinite(self.width) or not math.isfinite(self.height):
            return False
        if self.width <= 0 or self.height <= 0:
            return False
        if len(self.media_box) != 4 or len(self.crop_box) != 4:
            return False
        media_x0, media_y0, media_x1, media_y1 = self.media_box
        crop_x0, crop_y0, crop_x1, crop_y1 = self.crop_box
        values = (*self.media_box, *self.crop_box)
        if not all(math.isfinite(value) for value in values):
            return False
        if any(abs(value) > _EPSILON for value in (media_x0, media_y0, crop_x0, crop_y0)):
            return False
        if not (media_x1 > media_x0 and media_y1 > media_y0):
            return False
        if not (crop_x1 > crop_x0 and crop_y1 > crop_y0):
            return False
        if crop_x0 < media_x0 - _EPSILON or crop_y0 < media_y0 - _EPSILON:
            return False
        if crop_x1 > media_x1 + _EPSILON or crop_y1 > media_y1 + _EPSILON:
            return False
        if abs(self.width - (media_x1 - media_x0)) > _EPSILON:
            return False
        if abs(self.height - (media_y1 - media_y0)) > _EPSILON:
            return False
        if abs(self.width - (crop_x1 - crop_x0)) > _EPSILON:
            return False
        if abs(self.height - (crop_y1 - crop_y0)) > _EPSILON:
            return False
        return True

    def contains(self, box: Box) -> bool:
        """Require evidence geometry to fit inside both MediaBox and CropBox."""
        if not self.supports_zero_origin_boxes():
            return False
        if not all(math.isfinite(value) for value in (box.x0, box.top, box.x1, box.bottom)):
            return False
        if box.x0 > box.x1 or box.top > box.bottom:
            return False
        for raw_box in (self.media_box, self.crop_box):
            x0, y0, x1, y1 = raw_box
            page_top = self.height - y1
            page_bottom = self.height - y0
            if (
                box.x0 < x0 - _EPSILON
                or box.x1 > x1 + _EPSILON
                or box.top < page_top - _EPSILON
                or box.bottom > page_bottom + _EPSILON
            ):
                return False
        return True


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    role: str
    page_number: int
    text: str
    box: Box


@dataclass(frozen=True, slots=True)
class AnnualNativeIssuerIdentity:
    """Typed identity evidence, not source admission or financial eligibility."""

    company: str
    code: str
    period_end: date
    title_page: int
    company_info_page: int
    stock_profile_page: int
    evidence: tuple[EvidenceRef, ...]


@dataclass(frozen=True, slots=True)
class AnnualStatementIssuerIdentity:
    """Identity evidence joined through one issuer's signed annual statements."""

    company: str
    code: str
    period_end: date
    company_info_page: int
    consolidated_balance_sheet_page: int
    period_evidence_page: int
    evidence: tuple[EvidenceRef, ...]


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _ref(page: NativePage, role: str, item: PositionedText) -> EvidenceRef:
    return EvidenceRef(role, page.page_number, item.text, item.box)


def _valid_lines(page: NativePage) -> tuple[PositionedText, ...]:
    return tuple(line for line in page.positioned_lines if page.contains(line.box))


def _valid_words(page: NativePage) -> tuple[PositionedText, ...]:
    return tuple(word for word in page.words if page.contains(word.box))


def _period_from_report_title(text: str) -> date | None:
    from aquant.domain.fundamentals.disclosure_link import report_period_from_title

    return report_period_from_title(text)


def _has_legacy_front_conflict(
    pages: Sequence[NativePage], *, company: str, period_end: date
) -> bool:
    """Keep explicit early-page issuer and period conflicts fail-closed."""
    for page in pages:
        if page.page_number > 3:
            continue
        for line in _valid_lines(page):
            compact = _compact(line.text)
            if any(name != company for name in _LEGAL_NAME.findall(compact)):
                return True
            declared = _period_from_report_title(compact)
            if declared is not None and declared != period_end:
                return True
    return False


def _annual_header(
    page: NativePage, expected_year: int
) -> PositionedText | None:
    for line in _valid_lines(page):
        compact = _compact(line.text)
        match = _TITLE_YEAR.fullmatch(compact)
        if match is None or int(match.group(1)) != expected_year:
            continue
        if line.box.top > page.height * 0.12:
            continue
        if line.box.x0 < page.width * 0.70:
            continue
        return line
    return None


def _company_info_evidence(
    page: NativePage, company: str
) -> tuple[EvidenceRef, ...] | None:
    section_lines = [
        line
        for line in _valid_lines(page)
        if re.fullmatch(r"一[、.．]公司信息", _compact(line.text))
    ]
    if len(section_lines) != 1:
        return None
    section = section_lines[0]
    label_words = [
        word for word in _valid_words(page) if _compact(word.text) == "公司的中文名称"
    ]
    name_words = [
        word for word in _valid_words(page) if _compact(word.text) == _compact(company)
    ]
    pairs = [
        (label, name)
        for label in label_words
        for name in name_words
        if name.box.top >= section.box.bottom
        and name.box.x0 >= label.box.x1
        and abs(label.box.top - name.box.top) <= 1.5
        and abs(label.box.bottom - name.box.bottom) <= 1.5
    ]
    if len(pairs) != 1:
        return None
    label, name = pairs[0]
    return (
        _ref(page, "company-information-section", section),
        _ref(page, "company-chinese-name-label", label),
        _ref(page, "company-chinese-legal-name", name),
    )


def _word_groups_by_row(
    words: Iterable[PositionedText], tolerance: float = 1.5
) -> tuple[tuple[PositionedText, ...], ...]:
    groups: list[list[PositionedText]] = []
    for word in sorted(words, key=lambda item: (item.box.top, item.box.x0)):
        if not groups or abs(groups[-1][0].box.top - word.box.top) > tolerance:
            groups.append([word])
        else:
            groups[-1].append(word)
    return tuple(tuple(group) for group in groups)


def _center(item: PositionedText) -> float:
    return (item.box.x0 + item.box.x1) / 2


def _profile_evidence(
    page: NativePage, code: str
) -> tuple[EvidenceRef, ...] | None:
    lines = _valid_lines(page)
    sections = [
        line for line in lines if _STOCK_SECTION.fullmatch(_compact(line.text))
    ]
    captions = [
        line for line in lines if _compact(line.text) == "公司股票简况"
    ]
    if len(sections) != 1 or len(captions) != 1:
        return None
    section, caption = sections[0], captions[0]
    if caption.box.top <= section.box.bottom:
        return None

    rows = _word_groups_by_row(_valid_words(page))
    matching_profiles: list[tuple[PositionedText, ...]] = []
    chosen_header: tuple[PositionedText, ...] | None = None
    chosen_row: tuple[PositionedText, ...] | None = None
    for header_row in rows:
        if not (caption.box.bottom < header_row[0].box.top <= caption.box.bottom + 45):
            continue
        by_label = {
            label: [word for word in header_row if _compact(word.text) == label]
            for label in _PROFILE_COLUMNS
        }
        if any(len(by_label[label]) != 1 for label in _PROFILE_COLUMNS):
            continue
        headers = tuple(by_label[label][0] for label in _PROFILE_COLUMNS)
        centers = tuple(_center(header) for header in headers)
        if not all(left < right for left, right in zip(centers, centers[1:])):
            continue

        for data_row in rows:
            row_top = data_row[0].box.top
            if not (header_row[0].box.top + 5 <= row_top <= header_row[0].box.top + 40):
                continue
            candidates: list[PositionedText] = []
            expected_by_column = (
                lambda text: _compact(text) == "A股",
                lambda text: bool(re.fullmatch(r"[\u4e00-\u9fff]{2,20}证券交易所", _compact(text))),
                lambda text: bool(re.fullmatch(r"[\u4e00-\u9fff]{2,12}", _compact(text))),
                lambda text: _compact(text) == code and bool(re.fullmatch(r"\d{6}", _compact(text))),
            )
            for header, predicate in zip(headers, expected_by_column):
                aligned = [
                    word
                    for word in data_row
                    if predicate(word.text) and abs(_center(word) - _center(header)) <= 12
                ]
                if len(aligned) != 1:
                    break
                candidates.append(aligned[0])
            if len(candidates) != 4:
                continue
            if len({id(item) for item in candidates}) != 4:
                continue
            if max(item.box.top for item in candidates) - min(
                item.box.top for item in candidates
            ) > 1.5:
                continue
            if max(item.box.bottom for item in candidates) - min(
                item.box.bottom for item in candidates
            ) > 1.5:
                continue
            matching_profiles.append(tuple(candidates))
            chosen_header = headers
            chosen_row = tuple(candidates)

    if len(matching_profiles) != 1 or chosen_header is None or chosen_row is None:
        return None
    evidence = [
        _ref(page, "company-stock-profile-section", section),
        _ref(page, "company-stock-profile-caption", caption),
    ]
    evidence.extend(
        _ref(page, f"stock-table-column-{index + 1}", word)
        for index, word in enumerate(chosen_header)
    )
    evidence.extend(
        _ref(page, f"a-share-row-cell-{index + 1}", word)
        for index, word in enumerate(chosen_row)
    )
    return tuple(evidence)


def candidate_annual_native_profile_identity(
    pages: Sequence[NativePage],
    *,
    code: str,
    company: str,
    period_end: date,
) -> AnnualNativeIssuerIdentity | None:
    """Find an in-bounds annual header/name page and its adjacent A-share row.

    Production integration must supply pages parsed from one PDF byte stream,
    including pages 1-3 so explicit legacy cover conflicts remain rejecting.
    The saved source fixture contains only pages 7-8, so its focused tests
    exercise the new chain itself; this helper does not replace the existing
    cover path, reader version, serializer, or financial-section checks.
    """
    if period_end.month != 12 or period_end.day != 31:
        return None
    if not re.fullmatch(r"\d{6}", code):
        return None
    if not _LEGAL_NAME.fullmatch(company):
        return None
    page_numbers = [page.page_number for page in pages]
    if len(page_numbers) != len(set(page_numbers)):
        return None
    if _has_legacy_front_conflict(pages, company=company, period_end=period_end):
        return None

    by_number = {page.page_number: page for page in pages}
    candidates: list[AnnualNativeIssuerIdentity] = []
    for company_page in pages:
        if not company_page.supports_zero_origin_boxes():
            continue
        title = _annual_header(company_page, period_end.year)
        if title is None:
            continue
        company_evidence = _company_info_evidence(company_page, company)
        if company_evidence is None:
            continue
        profile_page = by_number.get(company_page.page_number + 1)
        if profile_page is None or not profile_page.supports_zero_origin_boxes():
            continue
        profile_evidence = _profile_evidence(profile_page, code)
        if profile_evidence is None:
            continue
        evidence = (_ref(company_page, "annual-report-page-header", title), *company_evidence, *profile_evidence)
        if any(
            not (company_page if item.page_number == company_page.page_number else profile_page).contains(item.box)
            for item in evidence
        ):
            continue
        candidates.append(
            AnnualNativeIssuerIdentity(
                company=company,
                code=code,
                period_end=period_end,
                title_page=company_page.page_number,
                company_info_page=company_page.page_number,
                stock_profile_page=profile_page.page_number,
                evidence=evidence,
            )
        )
    return candidates[0] if len(candidates) == 1 else None


def annual_native_profile_identity_from_pdf_bytes(
    pdf_bytes: bytes,
    *,
    pypdf_reader,
    code: str,
    company: str,
    period_end: date,
) -> AnnualNativeIssuerIdentity | None:
    """Build the narrow annual identity candidate from the current byte stream.

    ``pypdf_reader`` must be the local ``PdfReader(BytesIO(pdf_bytes))`` already
    used by ``check_report_cover``. Raw first-three-page conflicts are checked
    against its untouched extracted lines before positioned native words are
    considered. Later candidate recognition uses only word coordinates that
    fit both boxes from those same reader pages.
    """
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        return None
    stream = getattr(pypdf_reader, "stream", None)
    if not hasattr(stream, "getvalue") or stream.getvalue() != pdf_bytes:
        return None

    # Keep the existing deployed identity-name guard and period parser as the
    # authority for the old front-matter conflict contract.
    from aquant.adapters.providers.cninfo_report_identity import (
        IDENTITY_PAGE_LIMIT,
        explicit_issuer_company_names,
    )
    from aquant.domain.fundamentals.disclosure_link import report_period_from_title

    reader_pages = list(pypdf_reader.pages[:IDENTITY_PAGE_LIMIT])
    if len(reader_pages) < 3:
        return None
    raw_page_lines = [
        (index + 1, (page.extract_text() or "").splitlines())
        for index, page in enumerate(reader_pages)
    ]
    if {row[2] for row in explicit_issuer_company_names(raw_page_lines)} != {company}:
        return None

    for page_number, lines in raw_page_lines:
        if page_number > 3:
            continue
        for line in lines:
            compact = re.sub(r"\s+", "", line)
            if (
                re.search(r"(?:股份有限公司|股份公司|有限公司)", compact)
                and company not in compact
            ):
                return None
            key = re.sub(
                r"\s+\d+(?:\s*/\s*\d+)?\s*$",
                "",
                line.split("|", 1)[0],
            ).strip()
            declared_period = report_period_from_title(key)
            if declared_period is not None and declared_period != period_end:
                return None

    import pdfplumber

    native_pages: list[NativePage] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        plumber_pages = list(pdf.pages[:IDENTITY_PAGE_LIMIT])
        if len(plumber_pages) != len(reader_pages):
            return None
        for index, (plumber_page, reader_page) in enumerate(
            zip(plumber_pages, reader_pages), start=1
        ):
            words = tuple(
                PositionedText(
                    text=str(word["text"]),
                    box=Box(
                        float(word["x0"]),
                        float(word["top"]),
                        float(word["x1"]),
                        float(word["bottom"]),
                    ),
                )
                for word in plumber_page.extract_words()
            )
            lines = tuple(
                PositionedText(
                    text=" ".join(item.text for item in sorted(row, key=lambda word: word.box.x0)),
                    box=Box(
                        min(item.box.x0 for item in row),
                        min(item.box.top for item in row),
                        max(item.box.x1 for item in row),
                        max(item.box.bottom for item in row),
                    ),
                )
                for row in _word_groups_by_row(words)
            )
            native_pages.append(
                NativePage(
                    page_number=index,
                    width=float(plumber_page.width),
                    height=float(plumber_page.height),
                    media_box=tuple(float(value) for value in reader_page.mediabox),
                    crop_box=tuple(float(value) for value in reader_page.cropbox),
                    positioned_lines=lines,
                    words=words,
                )
            )

    # The raw first-three-page guard above uses report_period_from_title. Do
    # not run the earlier candidate's recognition-only parser on those pages.
    # The profile/header chain is bounded to the same first-20-page window.
    identity_pages = tuple(page for page in native_pages if page.page_number > 3)
    return candidate_annual_native_profile_identity(
        identity_pages,
        code=code,
        company=company,
        period_end=period_end,
    )


def _visible_positioned_lines(plumber_page) -> tuple[PositionedText, ...]:
    def include_visible_char(item) -> bool:
        if item.get("object_type") != "char":
            return True
        color = item.get("non_stroking_color")
        if isinstance(color, (tuple, list)) and len(color) == 4:
            try:
                return float(color[3]) > 0
            except (TypeError, ValueError):
                return True
        return True

    words = tuple(
        PositionedText(
            text=str(word["text"]),
            box=Box(
                float(word["x0"]),
                float(word["top"]),
                float(word["x1"]),
                float(word["bottom"]),
            ),
        )
        for word in plumber_page.filter(include_visible_char).extract_words()
    )
    return tuple(
        PositionedText(
            text=" ".join(item.text for item in sorted(row, key=lambda word: word.box.x0)),
            box=Box(
                min(item.box.x0 for item in row),
                min(item.box.top for item in row),
                max(item.box.x1 for item in row),
                max(item.box.bottom for item in row),
            ),
        )
        for row in _word_groups_by_row(words)
    )


def annual_statement_identity_from_pdf_bytes(
    pdf_bytes: bytes,
    *,
    pypdf_reader,
    code: str,
    company: str,
    period_end: date,
) -> AnnualStatementIssuerIdentity | None:
    """Join company profile, consolidated statement signature, and exact date.

    The company/code comes from the report's labeled company-information page.
    The statement issuer comes from the native consolidated balance-sheet
    header. The exact December 31 date must appear in that statement header or
    in the auditor's explicit description of that same company's consolidated
    balance sheet. An annual year alone is never a period match.
    """
    if (
        not isinstance(pdf_bytes, bytes)
        or not pdf_bytes.startswith(b"%PDF-")
        or period_end.month != 12
        or period_end.day != 31
        or not re.fullmatch(r"\d{6}", code)
        or not _LEGAL_NAME.fullmatch(company)
    ):
        return None
    stream = getattr(pypdf_reader, "stream", None)
    if not hasattr(stream, "getvalue") or stream.getvalue() != pdf_bytes:
        return None
    if not is_source_bound_000792_annual_identity_fallback(
        pdf_bytes, code=code, company=company, period_end=period_end,
    ):
        return None

    from aquant.adapters.providers.cninfo_report_identity import (
        explicit_issuer_company_names,
        profile_security_code_evidence,
    )
    import pdfplumber

    page_lines = [
        (number, (page.extract_text() or "").splitlines())
        for number, page in enumerate(pypdf_reader.pages, start=1)
    ]
    info_candidates = []
    for number, lines in page_lines[:20]:
        names = explicit_issuer_company_names([(number, lines)])
        if {item[2] for item in names} != {company}:
            continue
        code_rows = profile_security_code_evidence(
            [(number, lines)], code, company=company
        )
        if not code_rows:
            continue
        info_candidates.append((number, names, code_rows))
    if len(info_candidates) != 1:
        return None

    compact_period = f"{period_end.year}年{period_end.month}月{period_end.day}日"
    statement_candidates = []
    audit_candidates = []
    for number, lines in page_lines:
        compact_lines = tuple(_compact(line) for line in lines)
        title_lines = tuple(
            line for line, compact in zip(lines, compact_lines)
            if re.fullmatch(r"\d+[、.．]?合并资产负债表", compact)
        )
        issuer_lines = tuple(
            line for line, compact in zip(lines, compact_lines)
            if "编制单位" in compact and company in compact
        )
        if len(title_lines) == 1 and len(issuer_lines) == 1:
            statement_candidates.append((number, title_lines[0], issuer_lines[0], lines))

        normalized_page = _compact("".join(lines))
        if (
            compact_period in normalized_page
            and company in normalized_page
            and "审计报告正文" in normalized_page
            and "我们审计了" in normalized_page
            and "合并及母公司资产负债表" in normalized_page
        ):
            audit_candidates.append((number, lines))
    if len(statement_candidates) != 1:
        return None

    statement_page, title_text, issuer_text, statement_lines = statement_candidates[0]
    statement_has_exact_date = any(
        compact_period in _compact(line) for line in statement_lines
    )
    period_page = statement_page
    period_line_text = next(
        (line for line in statement_lines if compact_period in _compact(line)),
        None,
    )
    if not statement_has_exact_date:
        if len(audit_candidates) != 1:
            return None
        period_page, audit_lines = audit_candidates[0]
        period_line_text = next(
            (line for line in audit_lines
             if company in _compact(line) and compact_period in _compact(line)),
            None,
        )
        if period_line_text is None:
            return None

    evidence_page_numbers = {
        info_candidates[0][0], statement_page, period_page,
    }
    visible_lines: dict[int, tuple[PositionedText, ...]] = {}
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for number in evidence_page_numbers:
            if number < 1 or number > len(pdf.pages):
                return None
            visible_lines[number] = _visible_positioned_lines(pdf.pages[number - 1])

    info_page = info_candidates[0][0]
    info_lines = visible_lines[info_page]
    info_name = [
        line for line in info_lines
        if company in _compact(line.text)
        and any(label in _compact(line.text)
                for label in ("公司的中文名称", "公司中文名称", "公司名称"))
    ]
    info_code = [
        line for line in info_lines
        if any(label in _compact(line.text) for label in ("股票代码", "证券代码"))
        and code in _compact(line.text)
    ]
    if len(info_name) != 1 or len(info_code) != 1:
        return None

    statement_lines_visible = visible_lines[statement_page]
    visible_titles = [
        line for line in statement_lines_visible
        if re.fullmatch(r"\d+[、.．]?合并资产负债表", _compact(line.text))
    ]
    visible_issuer = [
        line for line in statement_lines_visible
        if "编制单位" in _compact(line.text) and company in _compact(line.text)
    ]
    if len(visible_titles) != 1 or len(visible_issuer) != 1:
        return None

    evidence = [
        EvidenceRef("company-information-legal-name", info_page,
                    info_name[0].text, info_name[0].box),
        EvidenceRef("company-information-stock-code", info_page,
                    info_code[0].text, info_code[0].box),
        EvidenceRef("consolidated-balance-sheet-title", statement_page,
                    visible_titles[0].text, visible_titles[0].box),
        EvidenceRef("consolidated-balance-sheet-issuer", statement_page,
                    visible_issuer[0].text, visible_issuer[0].box),
    ]
    if statement_page == period_page:
        visible_period_lines = [
            line for line in statement_lines_visible
            if compact_period in _compact(line.text)
        ]
        if not visible_period_lines:
            return None
        evidence.append(EvidenceRef(
            "explicit-statement-period-end", period_page,
            visible_period_lines[0].text, visible_period_lines[0].box,
        ))
    else:
        visible_audit_lines = visible_lines[period_page]
        audit_text = _compact("".join(line.text for line in visible_audit_lines))
        if not all(term in audit_text for term in (
            company, compact_period, "审计报告正文", "我们审计了", "合并及母公司资产负债表"
        )):
            return None
        visible_period_lines = [
            line for line in visible_audit_lines
            if company in _compact(line.text) and compact_period in _compact(line.text)
        ]
        if not visible_period_lines:
            return None
        evidence.append(EvidenceRef(
            "auditor-explicit-statement-period-end", period_page,
            visible_period_lines[0].text, visible_period_lines[0].box,
        ))

    return AnnualStatementIssuerIdentity(
        company=company,
        code=code,
        period_end=period_end,
        company_info_page=info_page,
        consolidated_balance_sheet_page=statement_page,
        period_evidence_page=period_page,
        evidence=tuple(evidence),
    )
