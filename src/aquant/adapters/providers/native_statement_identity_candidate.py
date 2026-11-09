"""Additive native statement identity contract for CNINFO PDF bytes.

This candidate proves only report identity and period. It never parses or
returns financial amounts and never admits a formal fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from io import BytesIO
import re
from typing import Iterable


LINE_Y_TOLERANCE_PT = 1.0
PROFILE_PAGE_LIMIT = 20
IDENTITY_SCHEMA = "aquant.native-statement-identity-candidate.v1"

_LEGAL_NAME = re.compile(r"[\u4e00-\u9fffA-Za-z（）()·]+(?:股份有限公司|股份公司|有限公司)\Z")
_NAME_LABEL = re.compile(r"(?:公司的中文名称|公司中文名称|公司名称)(?:[（(]如有[）)])?[:：]?\Z")
_REPORT_TITLE = re.compile(
    r"(?P<prefix>[\u4e00-\u9fffA-Za-z（）()·]{0,40})?"
    r"(?P<year>20\d{2})年(?P<kind>半年度|年度)报告(?:全文)?\Z"
)
_DATE = re.compile(r"(20\d{2})年0?(\d{1,2})月0?(\d{1,2})日")
_A_SHARE_ROW = re.compile(r"^A股(?:上海|深圳|北京)")
_PROFILE_HEADING = re.compile(
    r"(?:第[一二三四五六七八九十\d]+[节章])?公司简介"
)


class NativeIdentityRejected(ValueError):
    """The native PDF does not contain an unambiguous matching identity chain."""


@dataclass(frozen=True, slots=True)
class NativeWord:
    text: str
    x0: float
    top: float
    x1: float
    bottom: float

    @property
    def x_center(self) -> float:
        return (self.x0 + self.x1) / 2.0


@dataclass(frozen=True, slots=True)
class NativeLine:
    text: str
    bbox: tuple[float, float, float, float] | None = None
    words: tuple[NativeWord, ...] = ()

    @property
    def top(self) -> float | None:
        return self.bbox[1] if self.bbox is not None else None

    @property
    def bottom(self) -> float | None:
        return self.bbox[3] if self.bbox is not None else None


@dataclass(frozen=True, slots=True)
class NativePage:
    physical_page: int
    width: float
    height: float
    lines: tuple[NativeLine, ...]


@dataclass(frozen=True, slots=True)
class IdentityRow:
    page: int
    line: NativeLine
    source_lines: tuple[NativeLine, ...] = ()


@dataclass(frozen=True, slots=True)
class NativeStatementIdentity:
    pdf_sha256: str
    instrument_id: str
    period_end: date
    legal_issuer: str
    local_stock_name: str
    company_name_row: IdentityRow
    a_share_code_row: IdentityRow
    statement_issuer_row: IdentityRow
    statement_heading_row: IdentityRow
    statement_period_row: IdentityRow
    statement_current_columns_row: IdentityRow
    statement_unit_row: IdentityRow | None = None

    def as_evidence(self) -> dict[str, object]:
        def row(item: IdentityRow) -> dict[str, object]:
            result: dict[str, object] = {
                "physicalPage": item.page,
                "text": item.line.text,
                "bboxPoints": list(item.line.bbox) if item.line.bbox else None,
            }
            if item.source_lines:
                result["sourceLines"] = [
                    {
                        "text": source.text,
                        "bboxPoints": list(source.bbox) if source.bbox else None,
                    }
                    for source in item.source_lines
                ]
            return result

        result = {
            "schema": IDENTITY_SCHEMA,
            "status": "CANDIDATE_ONLY_NOT_ADMITTED",
            "formalAdmission": "NOT_ADMITTED",
            "source": "native PDF text from supplied bytes",
            "ocrUsed": False,
            "renderingUsed": False,
            "financialAmountsRead": False,
            "pdfSha256": self.pdf_sha256,
            "instrumentId": self.instrument_id,
            "periodEnd": self.period_end.isoformat(),
            "legalIssuer": self.legal_issuer,
            "localStockName": self.local_stock_name,
            "companyNameRow": row(self.company_name_row),
            "aShareCodeRow": row(self.a_share_code_row),
            "statementIssuerRow": row(self.statement_issuer_row),
            "statementHeadingRow": row(self.statement_heading_row),
            "statementPeriodRow": row(self.statement_period_row),
            "statementCurrentColumnsRow": row(self.statement_current_columns_row),
        }
        if self.statement_unit_row is not None:
            result["statementUnitRow"] = row(self.statement_unit_row)
        return result


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


_SOURCE_CASES = {
    ("600988", date(2025, 6, 30)): (
        "赤峰吉隆黄金矿业股份有限公司",
        "327812b211cd6f665d81d38915125ffc9836eacc8898a498802306be1cacd284",
    ),
    ("600988", date(2026, 6, 30)): (
        "赤峰吉隆黄金矿业集团股份有限公司",
        "c86d474d3cd66c7a1aff9bf1de6372072ce54acf1edfe6e52635772394f172ed",
    ),
}

# These two 2024 reports were visually matched to Chifeng Gold and their exact
# archived bytes. The native identity fallback below remains limited to these
# four-part source bindings; it does not recognize other 600988 reports.
_SOURCE_BOUND_600988_2024_REVENUE = {
    ("600988", date(2024, 6, 30)): {
        "company": "赤峰吉隆黄金矿业股份有限公司",
        "stock_name": "赤峰黄金",
        "sha256": "923b7df94b43eda5e5951d1265c1470b2950f9b8e46e2fdf4a7e69bb2326d901",
        "announcement_id": "1221090296",
        "document_url": "https://static.cninfo.com.cn/finalpage/2024-08-31/1221090296.PDF",
        "prior_balance_date": date(2023, 12, 31),
    },
    ("600988", date(2024, 12, 31)): {
        "company": "赤峰吉隆黄金矿业股份有限公司",
        "stock_name": "赤峰黄金",
        "sha256": "f91218c102c6fddbcdbf2ce0e67ad3ba372a976ea94f43f9797a7b220af07edd",
        "announcement_id": "1222946558",
        "document_url": "https://static.cninfo.com.cn/finalpage/2025-03-29/1222946558.PDF",
        "prior_balance_date": date(2023, 12, 31),
    },
}

# This single corrected-statements attachment has no six-digit security code
# on its cover.  Its issuer/code identity is therefore usable only when the
# already-audited CNINFO receipt/index contract and these exact source bytes
# agree; the geometry reader separately requires the full native issuer name
# and the corrected H125 section/date headers in the supplied PDF.
_SOURCE_BOUND_601069_CORRECTED_H125 = {
    "instrument_id": "601069",
    "company": "西部黄金股份有限公司",
    "announcement_id": "1225082560",
    "document_title": "西部黄金股份有限公司关于前期会计差错更正后的财务报表及附注",
    "document_url": "https://static.cninfo.com.cn/finalpage/2026-04-08/1225082560.PDF",
    "sha256": "ad2fb88d66457c794f32d15ed80e274815ed1d7d48fc3f87932c0d7063e760e6",
    "byte_size": 247783,
    "source_role": "CORRECTED_FINANCIAL_STATEMENTS",
    "source_report_period_end": date(2025, 6, 30),
    "source_report_period_end_meaning": (
        "selected-section-only; this corrected-statements document contains multiple periods"
    ),
    "source_section_period_end": date(2025, 6, 30),
    "pdf_receipt_id": "rcpt_782b9a0c9ba7dca61e7d2a23",
    "index_receipt_id": "rcpt_b5074399ad32e45ce7d922f0",
    "index_sha256": "464423a919b3c5efb7053d532711b1c173a3f7122d316bcba1a471b277658b46",
    "index_request_sha256": "afeb0346b8f6191c4d615724d0e484ab45d449d1cffec600088fbbeef788ba5c",
}


def bind_601069_corrected_h125_source_identity(
    instrument_id: str,
    period_end: date,
    *,
    pdf_bytes: bytes,
    expected_company: str,
    announcement_id: str,
    document_title: str,
    document_url: str,
    source_role: str,
    source_section_period_end: date,
) -> dict[str, object] | None:
    """Return the receipt/index-bound identity only for the exact corrected H125 PDF."""
    case = _SOURCE_BOUND_601069_CORRECTED_H125
    if (
        instrument_id != case["instrument_id"]
        or period_end != case["source_section_period_end"]
        or not isinstance(pdf_bytes, bytes)
        or not pdf_bytes.startswith(b"%PDF-")
        or len(pdf_bytes) != case["byte_size"]
        or sha256(pdf_bytes).hexdigest() != case["sha256"]
        or _compact(expected_company) != _compact(str(case["company"]))
        or announcement_id != case["announcement_id"]
        or document_title != case["document_title"]
        or document_url != case["document_url"]
        or source_role != case["source_role"]
        or source_section_period_end != case["source_section_period_end"]
    ):
        return None
    return {
        "identityBasis": "INDEX_BOUND_ONLY",
        "instrumentId": str(case["instrument_id"]),
        "company": str(case["company"]),
        "announcementId": str(case["announcement_id"]),
        "documentTitle": str(case["document_title"]),
        "documentUrl": str(case["document_url"]),
        "pdfSha256": "sha256:" + str(case["sha256"]),
        "pdfByteSize": int(case["byte_size"]),
        "sourceRole": str(case["source_role"]),
        "sourceReportPeriodEnd": case["source_report_period_end"].isoformat(),
        "sourceReportPeriodEndMeaning": str(case["source_report_period_end_meaning"]),
        "sourceSectionPeriodEnd": case["source_section_period_end"].isoformat(),
        "pdfReceiptId": str(case["pdf_receipt_id"]),
        "indexReceiptId": str(case["index_receipt_id"]),
        "indexSha256": "sha256:" + str(case["index_sha256"]),
        "indexRequestSha256": "sha256:" + str(case["index_request_sha256"]),
    }


def supports_600988_h1_source_candidate(
    instrument_id: str, period_end: date, *, pdf_bytes: bytes,
    expected_company: str | None = None,
) -> bool:
    """Limit the additive route to the two archived, receipt-bound PDF byte strings."""
    case = _SOURCE_CASES.get((instrument_id, period_end))
    if case is None or not isinstance(pdf_bytes, bytes):
        return False
    expected_name, expected_hash = case
    return (
        sha256(pdf_bytes).hexdigest() == expected_hash
        and (expected_company is None or _compact(expected_company) == _compact(expected_name))
    )


def is_600988_2024_revenue_source_tuple(
    instrument_id: str, period_end: date,
) -> bool:
    return (instrument_id, period_end) in _SOURCE_BOUND_600988_2024_REVENUE


def supports_600988_2024_revenue_source_candidate(
    instrument_id: str,
    period_end: date,
    *,
    pdf_bytes: bytes,
    expected_company: str | None = None,
    announcement_id: str | None = None,
    document_url: str | None = None,
) -> bool:
    """Bind only the two reviewed 2024 source byte strings and tuple metadata."""
    case = _SOURCE_BOUND_600988_2024_REVENUE.get((instrument_id, period_end))
    if case is None or not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        return False
    return (
        sha256(pdf_bytes).hexdigest() == case["sha256"]
        and (expected_company is None or _compact(expected_company) == _compact(case["company"]))
        and (announcement_id is None or announcement_id == case["announcement_id"])
        and (document_url is None or document_url == case["document_url"])
    )


def _line_bbox(words: Iterable[NativeWord]) -> tuple[float, float, float, float] | None:
    items = tuple(words)
    if not items:
        return None
    return (
        min(word.x0 for word in items),
        min(word.top for word in items),
        max(word.x1 for word in items),
        max(word.bottom for word in items),
    )


def _group_words(words: Iterable[dict[str, object]]) -> tuple[NativeLine, ...]:
    """Group native PDF words using the existing 1 pt line tolerance."""
    parsed: list[NativeWord] = []
    for word in words:
        try:
            parsed.append(NativeWord(
                text=str(word["text"]),
                x0=float(word["x0"]),
                top=float(word["top"]),
                x1=float(word["x1"]),
                bottom=float(word["bottom"]),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    parsed.sort(key=lambda item: (item.top, item.x0))
    groups: list[list[NativeWord]] = []
    group_top: float | None = None
    for word in parsed:
        if group_top is None or abs(word.top - group_top) > LINE_Y_TOLERANCE_PT:
            groups.append([word])
            group_top = word.top
        else:
            groups[-1].append(word)
    lines = []
    for group in groups:
        ordered = tuple(sorted(group, key=lambda item: item.x0))
        lines.append(NativeLine(
            text=" ".join(item.text for item in ordered),
            bbox=_line_bbox(ordered),
            words=ordered,
        ))
    return tuple(lines)


def _identity_page_indices(pdf: object, statement_titles: Iterable[tuple]) -> tuple[int, ...]:
    """Bound word extraction to frontmatter roles and recalled statement titles."""
    pdf_pages = getattr(pdf, "pages", None)
    if pdf_pages is None:
        raise NativeIdentityRejected("opened PDF has no pages")
    page_count = len(pdf_pages)
    selected = set(range(min(3, page_count)))  # preserve first-three-page conflict checks

    for index, page in enumerate(pdf_pages[:PROFILE_PAGE_LIMIT]):
        text_lines = (page.extract_text() or "").splitlines()
        compact_lines = tuple(_compact(line) for line in text_lines)
        profile_page = any(_PROFILE_HEADING.match(line) or line == "公司信息"
                           for line in compact_lines)
        a_share_or_table_page = any(_A_SHARE_ROW.match(line) for line in compact_lines)
        a_share_or_table_page |= any(
            all(label in line for label in ("股票种类", "股票简称", "股票代码"))
            for line in compact_lines
        )
        if profile_page or a_share_or_table_page:
            selected.add(index)

    statement_pages: set[int] = set()
    try:
        for index, _line, role, statement in statement_titles:
            if role != "合并" or statement != "资产负债表":
                continue
            if type(index) is not int or index < 0 or index >= page_count:
                raise NativeIdentityRejected("native statement title locator returned an invalid page")
            statement_pages.add(index)
    except NativeIdentityRejected:
        raise
    except Exception as exc:
        raise NativeIdentityRejected("native statement title locator returned invalid evidence") from exc
    if not statement_pages:
        raise NativeIdentityRejected("native consolidated balance-sheet title was not recalled")
    selected.update(statement_pages)
    return tuple(sorted(selected))


def _native_pages_from_open_pdf(
    pdf: object, *, statement_titles: Iterable[tuple],
) -> tuple[NativePage, ...]:
    indices = _identity_page_indices(pdf, statement_titles)
    pdf_pages = getattr(pdf, "pages")
    result = []
    for index in indices:
        page = pdf_pages[index]
        words = page.extract_words(
            x_tolerance=LINE_Y_TOLERANCE_PT,
            y_tolerance=LINE_Y_TOLERANCE_PT,
            keep_blank_chars=False,
        )
        result.append(NativePage(
            physical_page=index + 1,
            width=float(page.width),
            height=float(page.height),
            lines=_group_words(words),
        ))
    return tuple(result)


def _pdf_native_pages(pdf_bytes: bytes) -> tuple[NativePage, ...]:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - runtime dependency guard
        raise NativeIdentityRejected("pdfplumber is required for native identity") from exc

    try:
        # Reuse the release-v1 native/canonical title locator. Its returned
        # page set includes every exact matching title (including competing
        # matches); only those pages plus the bounded identity pages below get
        # the comparatively expensive word/geometry extraction.
        from .cninfo_statement_geometry import _canonical_statement_titles
        statement_titles = _canonical_statement_titles(pdf_bytes)
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            return _native_pages_from_open_pdf(pdf, statement_titles=statement_titles)
    except NativeIdentityRejected:
        raise
    except Exception as exc:
        raise NativeIdentityRejected("native PDF text extraction failed") from exc


def _labeled_legal_name(text: str) -> str | None:
    compact = _compact(text)
    for label in ("公司的中文名称", "公司中文名称", "公司名称", "公司的中文名称如有"):
        if compact.startswith(label):
            candidate = compact[len(label):].lstrip(":：")
            if _LEGAL_NAME.fullmatch(candidate):
                return candidate
    return None


def _company_profile(pages: tuple[NativePage, ...]) -> tuple[IdentityRow, str]:
    evidence: list[tuple[IdentityRow, str]] = []
    for page in pages:
        if page.physical_page > PROFILE_PAGE_LIMIT:
            continue
        compact_lines = tuple(_compact(line.text) for line in page.lines)
        if not any(_PROFILE_HEADING.match(text) or text == "公司信息"
                   for text in compact_lines):
            continue
        for index, line in enumerate(page.lines):
            name = _labeled_legal_name(line.text)
            if name is None and _NAME_LABEL.fullmatch(_compact(line.text)) and index + 1 < len(page.lines):
                next_line = _compact(page.lines[index + 1].text)
                if _LEGAL_NAME.fullmatch(next_line):
                    name = next_line
                    line = NativeLine(
                        text=f"{line.text}\n{page.lines[index + 1].text}",
                        bbox=(
                            min(line.bbox[0], page.lines[index + 1].bbox[0]),
                            min(line.bbox[1], page.lines[index + 1].bbox[1]),
                            max(line.bbox[2], page.lines[index + 1].bbox[2]),
                            max(line.bbox[3], page.lines[index + 1].bbox[3]),
                        ) if line.bbox and page.lines[index + 1].bbox else None,
                    )
            if name is not None:
                evidence.append((IdentityRow(page.physical_page, line), name))

    names = {name for _, name in evidence}
    if len(names) != 1 or not evidence:
        raise NativeIdentityRejected("company-info legal name is missing or conflicting")
    evidence.sort(key=lambda item: (item[0].page, item[0].line.top or 0.0))
    return evidence[0][0], next(iter(names))


def _column_center(line: NativeLine, label: str) -> float | None:
    matches = [word for word in line.words if _compact(word.text) == label]
    if len(matches) != 1:
        return None
    return matches[0].x_center


def _word_in_column(line: NativeLine, center: float, expected: str | None = None) -> NativeWord | None:
    matches = [word for word in line.words
               if abs(word.x_center - center) <= LINE_Y_TOLERANCE_PT
               and (expected is None or _compact(word.text) == expected)]
    return matches[0] if len(matches) == 1 else None


def _a_share_row(
    pages: tuple[NativePage, ...], instrument_id: str,
) -> tuple[IdentityRow, str]:
    matches: list[tuple[IdentityRow, str]] = []
    wrong_codes: list[IdentityRow] = []
    for page in pages:
        if page.physical_page > PROFILE_PAGE_LIMIT:
            continue
        for index, line in enumerate(page.lines):
            compact = _compact(line.text)
            if not _A_SHARE_ROW.match(compact):
                continue
            headers = [candidate for candidate in page.lines[max(0, index - 2):index]
                       if all(label in _compact(candidate.text)
                              for label in ("股票种类", "股票简称", "股票代码"))]
            if len(headers) != 1:
                continue
            header = headers[0]
            name_center = _column_center(header, "股票简称")
            code_center = _column_center(header, "股票代码")
            if name_center is None or code_center is None:
                continue
            codes = [word for word in line.words if re.fullmatch(r"\d{6}", _compact(word.text))]
            code_word = _word_in_column(line, code_center)
            name_word = _word_in_column(line, name_center)
            if len(codes) != 1 or code_word is None or name_word is None:
                continue
            if _compact(code_word.text) != instrument_id:
                wrong_codes.append(IdentityRow(page.physical_page, line))
                continue
            matches.append((IdentityRow(page.physical_page, line), _compact(name_word.text)))

    if wrong_codes:
        raise NativeIdentityRejected("A-share stock table contains a conflicting code row")
    if len(matches) != 1:
        raise NativeIdentityRejected("unique A-share stock-table code row is missing or ambiguous")
    return matches[0]


def _source_bound_600988_2024_a_share_row(
    pages: tuple[NativePage, ...], instrument_id: str, expected_stock_name: str,
) -> tuple[IdentityRow, str]:
    """Read the reviewed profile row by header-cell bounds, not equal centers.

    The 2024 PDFs align each value within its headed column but do not center
    the code value on the printed "股票代码" label. The existing general reader
    requires equal centers and therefore misses this otherwise native row.
    This fallback is reachable only after the exact source hash is bound.
    """
    labels = (
        "股票种类", "股票上市交易所", "股票简称", "股票代码", "变更前股票简称",
    )
    candidates: list[tuple[NativePage, NativeLine, NativeLine]] = []
    for page in pages:
        if page.physical_page > PROFILE_PAGE_LIMIT:
            continue
        compact_lines = tuple(_compact(line.text) for line in page.lines)
        has_stock_heading = any(
            text == "公司股票简况"
            or re.fullmatch(r"[一二三四五六七八九十\d]+[、.．]公司股票简况", text)
            for text in compact_lines
        )
        if not has_stock_heading:
            continue
        for index, line in enumerate(page.lines):
            compact = compact_lines[index]
            if not _A_SHARE_ROW.match(compact):
                continue
            headers = [
                prior for prior in page.lines[max(0, index - 2):index]
                if all(label in _compact(prior.text) for label in labels[:4])
            ]
            if len(headers) != 1:
                raise NativeIdentityRejected(
                    "source-bound A-share row lacks one preceding stock-table header")
            candidates.append((page, headers[0], line))

    if len(candidates) != 1:
        raise NativeIdentityRejected(
            "source-bound A-share stock row is missing or ambiguous")
    page, header, row = candidates[0]
    code_values = [
        word for word in row.words
        if re.fullmatch(r"\d{6}", _compact(word.text))
    ]
    if len(code_values) != 1 or _compact(code_values[0].text) != instrument_id:
        raise NativeIdentityRejected("source-bound A-share row contains a conflicting code")

    centers: list[float] = []
    for label in labels:
        center = _column_center(header, label)
        if center is None:
            raise NativeIdentityRejected(
                "source-bound A-share header has missing or duplicate columns")
        centers.append(center)
    if any(left >= right for left, right in zip(centers, centers[1:])):
        raise NativeIdentityRejected("source-bound A-share header columns are unordered")
    boundaries = [float("-inf")]
    boundaries.extend((left + right) / 2.0 for left, right in zip(centers, centers[1:]))
    boundaries.append(float("inf"))

    expected_cells = (
        (lambda text: _compact(text) == "A股", 0, "A-share class"),
        (lambda text: bool(re.fullmatch(r"[\u4e00-\u9fff]{2,20}证券交易所", _compact(text))),
         1, "A-share exchange"),
        (lambda text: _compact(text) == expected_stock_name, 2, "A-share stock name"),
        (lambda text: _compact(text) == instrument_id, 3, "A-share code"),
    )
    for predicate, column_index, description in expected_cells:
        lower, upper = boundaries[column_index], boundaries[column_index + 1]
        matches = [
            word for word in row.words
            if predicate(word.text)
            and lower <= word.x_center < upper
        ]
        if len(matches) != 1:
            raise NativeIdentityRejected(
                f"source-bound {description} does not occupy its headed column")

    data_words = [item for predicate, _index, _label in expected_cells
                  for item in row.words if predicate(item.text)]
    if (
        len(data_words) != 4
        or max(item.top for item in data_words) - min(item.top for item in data_words) > 1.5
        or max(item.bottom for item in data_words) - min(item.bottom for item in data_words) > 1.5
    ):
        raise NativeIdentityRejected("source-bound A-share row is not one native row")
    return IdentityRow(page.physical_page, row), expected_stock_name


def _parse_native_dates(text: str) -> tuple[date, ...]:
    dates: list[date] = []
    for year, month, day in _DATE.findall(_compact(text)):
        try:
            dates.append(date(int(year), int(month), int(day)))
        except ValueError:
            continue
    return tuple(dates)


def _frontmatter_conflict(
    pages: tuple[NativePage, ...], *, legal_issuer: str, local_stock_name: str,
    period_end: date,
) -> None:
    for page in pages:
        if page.physical_page > 3:
            continue
        lines = page.lines
        is_contents_page = any(_compact(line.text) in {"目录", "目錄"} for line in lines[:3])
        in_reference_list = False
        for line_index, line in enumerate(lines):
            compact = _compact(line.text)
            if "备查文件目录" in compact or "备查文件清单" in compact:
                in_reference_list = True
                continue
            if in_reference_list:
                continue
            explicit_name = _labeled_legal_name(line.text)
            if explicit_name is not None and explicit_name != legal_issuer:
                raise NativeIdentityRejected("first-three-page issuer field conflicts with profile")
            if is_contents_page:
                continue
            title = _REPORT_TITLE.fullmatch(compact)
            if title is None:
                continue
            declared = date(
                int(title.group("year")),
                6 if title.group("kind") == "半年度" else 12,
                30 if title.group("kind") == "半年度" else 31,
            )
            if declared != period_end:
                raise NativeIdentityRejected("first-three-page report title has a different period")
            prefix = title.group("prefix") or ""
            if not prefix:
                # Covers sometimes print the issuer and report title on
                # adjacent lines instead of one concatenated title line. Only
                # accept this as issuer-role evidence when the report title is
                # an exact standalone line on a non-contents, non-reference
                # first-three page; don't scan nearby disclosure prose.
                adjacent = (
                    lines[line_index - 1:line_index]
                    + lines[line_index + 1:line_index + 2]
                )
                for adjacent_line in adjacent:
                    adjacent_name = _compact(adjacent_line.text)
                    if (_LEGAL_NAME.fullmatch(adjacent_name)
                            and adjacent_name not in {legal_issuer, local_stock_name}):
                        raise NativeIdentityRejected(
                            "first-three-page title is adjacent to a conflicting legal issuer")
            if prefix and (_LEGAL_NAME.fullmatch(prefix) or prefix in {legal_issuer, local_stock_name}):
                if prefix not in {legal_issuer, local_stock_name}:
                    raise NativeIdentityRejected("first-three-page report title names another issuer")
            elif prefix:
                # An unrecognized title prefix is positive title-role evidence,
                # but cannot be resolved from this report's own profile/table.
                raise NativeIdentityRejected("first-three-page report title issuer is unresolved")


def _source_bound_first_three_conflict(
    pages: tuple[NativePage, ...], *, legal_issuer: str,
    local_stock_name: str, period_end: date,
) -> None:
    """Fail closed on any native legal-name or report-period conflict up front."""
    allowed_names = {_compact(legal_issuer), _compact(local_stock_name)}
    for page in pages:
        if page.physical_page > 3:
            continue
        for line in page.lines:
            compact = _compact(line.text)
            if any(_compact(name) not in allowed_names for name in _LEGAL_NAME.findall(compact)):
                raise NativeIdentityRejected(
                    "first-three-page visible legal-name text conflicts with the bound issuer")
            explicit_name = _labeled_legal_name(line.text)
            if explicit_name is not None and _compact(explicit_name) not in allowed_names:
                raise NativeIdentityRejected(
                    "first-three-page issuer field conflicts with the bound issuer")
            title = _REPORT_TITLE.fullmatch(compact)
            if title is None:
                continue
            declared = date(
                int(title.group("year")),
                6 if title.group("kind") == "半年度" else 12,
                30 if title.group("kind") == "半年度" else 31,
            )
            if declared != period_end:
                raise NativeIdentityRejected(
                    "first-three-page report title has a different period")
            prefix = _compact(title.group("prefix") or "")
            if prefix and prefix not in allowed_names:
                raise NativeIdentityRejected(
                    "first-three-page title issuer conflicts with the bound issuer")


def _ordered_geometry(*lines: NativeLine) -> bool:
    if any(line.bbox is None for line in lines):
        return True
    for left, right in zip(lines, lines[1:]):
        assert left.bottom is not None and right.top is not None
        if left.bottom > right.top + LINE_Y_TOLERANCE_PT:
            return False
    return True


def _dates_from_native_row_pair(
    value_line: NativeLine, label_line: NativeLine,
) -> tuple[date, ...]:
    """Pair printed date numerals with adjacent 年/月/日 markers by 1 pt geometry."""
    labels = sorted(
        (word for word in label_line.words if _compact(word.text) in {"年", "月", "日"}),
        key=lambda word: word.x0,
    )
    values = sorted(
        (word for word in value_line.words if re.fullmatch(r"\d+", _compact(word.text))),
        key=lambda word: word.x0,
    )
    if len(labels) not in (3, 6) or len(values) != len(labels):
        return ()
    label_names = [_compact(word.text) for word in labels]
    if label_names != ["年", "月", "日"] * (len(labels) // 3):
        return ()
    if value_line.top is not None and label_line.top is not None:
        if value_line.bottom is None or label_line.bottom is None:
            return ()
        if (value_line.top > label_line.bottom + LINE_Y_TOLERANCE_PT
                or label_line.top > value_line.bottom + LINE_Y_TOLERANCE_PT):
            return ()
    for value, label in zip(values, labels):
        if value.x0 >= label.x0 or abs(value.x1 - label.x0) > LINE_Y_TOLERANCE_PT:
            return ()

    dates = []
    for offset in range(0, len(values), 3):
        year_text, month_text, day_text = (
            _compact(word.text) for word in values[offset:offset + 3]
        )
        if len(year_text) != 4 or len(month_text) > 2 or len(day_text) > 2:
            return ()
        try:
            dates.append(date(int(year_text), int(month_text), int(day_text)))
        except ValueError:
            return ()
    return tuple(dates)


def _date_observations(
    page: NativePage, start: int, stop: int,
) -> tuple[tuple[tuple[date, ...], tuple[int, ...]], ...]:
    result = []
    for index in range(start, stop):
        direct = _parse_native_dates(page.lines[index].text)
        if direct:
            result.append((direct, (index,)))
        if index + 1 < stop:
            paired = _dates_from_native_row_pair(page.lines[index], page.lines[index + 1])
            if paired:
                result.append((paired, (index, index + 1)))
    return tuple(result)


def _column_date_observations(
    page: NativePage, start: int, stop: int,
) -> tuple[tuple[tuple[date, ...], tuple[int, ...]], ...]:
    result = []
    for index in range(start, stop):
        line = page.lines[index]
        if "项目" not in _compact(line.text):
            continue
        direct = _parse_native_dates(line.text)
        if len(direct) == 2:
            result.append((direct, (index,)))
        if index > start:
            paired = _dates_from_native_row_pair(page.lines[index - 1], line)
            if len(paired) == 2:
                result.append((paired, (index - 1, index)))
    return tuple(result)


def _source_bound_2024_consolidated_balance_sheet(
    pages: tuple[NativePage, ...], *, legal_issuer: str, period_end: date,
    prior_date: date,
) -> tuple[IdentityRow, IdentityRow, IdentityRow, IdentityRow, IdentityRow]:
    """Bind the source-specific statement signature and explicit RMB-yuan unit."""
    expected_name = _compact(legal_issuer)
    candidates = []
    conflict = False
    for page in pages:
        compact_lines = tuple(_compact(line.text) for line in page.lines)
        for heading_index, text in enumerate(compact_lines):
            if text != "合并资产负债表":
                continue
            start = max(0, heading_index - 4)
            stop = min(len(page.lines), heading_index + 10)
            issuer_indexes = []
            labeled_issuer_indexes = []
            for index in range(start, stop):
                compact = compact_lines[index]
                names = {_compact(name) for name in _LEGAL_NAME.findall(compact)}
                if names and names != {expected_name}:
                    conflict = True
                if (
                    compact.startswith("编制单位") and expected_name in compact
                    and names in (set(), {expected_name})
                ):
                    labeled_issuer_indexes.append(index)
                elif compact == expected_name:
                    issuer_indexes.append(index)

            # Annual reports may repeat the issuer as a running header and as
            # the statement's explicit preparer. Prefer the labeled role row
            # when present; the standalone legal name remains a bounded fallback.
            issuer_indexes = labeled_issuer_indexes or issuer_indexes

            period_rows = [
                indexes for values, indexes in _date_observations(
                    page, heading_index + 1, stop,
                ) if values == (period_end,)
            ]
            column_rows = [
                indexes for values, indexes in _column_date_observations(
                    page, heading_index + 1, stop,
                ) if values == (period_end, prior_date)
            ]
            unit_rows = [
                index for index in range(start, stop)
                if re.search(r"单位[:：]?元", compact_lines[index])
                and re.search(r"币种[:：]?人民币", compact_lines[index])
            ]
            if (
                len(issuer_indexes) != 1
                or len(period_rows) != 1
                or len(column_rows) != 1
                or len(unit_rows) != 1
            ):
                continue

            rows = (
                IdentityRow(page.physical_page, page.lines[issuer_indexes[0]]),
                IdentityRow(page.physical_page, page.lines[heading_index]),
                _identity_row_from_lines(page, period_rows[0]),
                _identity_row_from_lines(page, column_rows[0]),
                IdentityRow(page.physical_page, page.lines[unit_rows[0]]),
            )
            positioned = sorted(
                (row.line for row in rows if row.line.bbox is not None),
                key=lambda line: line.top or 0.0,
            )
            if len(positioned) != len(rows) or not _ordered_geometry(*positioned):
                continue
            candidates.append(rows)

    if conflict:
        raise NativeIdentityRejected(
            "consolidated balance-sheet header contains a conflicting legal issuer")
    if len(candidates) != 1:
        raise NativeIdentityRejected(
            "source-bound consolidated balance-sheet signature is missing or ambiguous")
    return candidates[0]


def _identity_row_from_lines(page: NativePage, indexes: tuple[int, ...]) -> IdentityRow:
    source_lines = tuple(page.lines[index] for index in indexes)
    if len(source_lines) == 1:
        return IdentityRow(page.physical_page, source_lines[0])
    boxes = [line.bbox for line in source_lines if line.bbox is not None]
    bbox = None
    if len(boxes) == len(source_lines):
        bbox = (
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )
    combined = NativeLine("\n".join(line.text for line in source_lines), bbox)
    return IdentityRow(page.physical_page, combined, source_lines)


def _statement_header(
    pages: tuple[NativePage, ...], legal_issuer: str, period_end: date,
) -> tuple[IdentityRow, IdentityRow, IdentityRow, IdentityRow]:
    expected_compact = _compact(legal_issuer)
    candidates = []
    conflicting = False
    for page in pages:
        compact_lines = tuple(_compact(line.text) for line in page.lines)
        for heading_index, heading_text in enumerate(compact_lines):
            if heading_text != "合并资产负债表":
                continue
            issuer_indexes = [i for i in range(max(0, heading_index - 2), heading_index)
                              if compact_lines[i] == expected_compact]
            stop = min(len(page.lines), heading_index + 9)
            dates = _date_observations(page, heading_index + 1, stop)
            period_rows = [(row_indexes, values) for values, row_indexes in dates
                           if values == (period_end,)]
            column_rows = [(row_indexes, values)
                           for values, row_indexes in _column_date_observations(
                               page, heading_index + 1, stop)
                           if values[0] == period_end and values[1] != period_end]
            if issuer_indexes and len(period_rows) == 1 and len(column_rows) == 1:
                period_indexes, _ = period_rows[0]
                column_indexes, _ = column_rows[0]
                issuer_index = max(issuer_indexes)
                if (issuer_index < heading_index
                        and heading_index < min(period_indexes)
                        and max(period_indexes) < min(column_indexes)):
                    rows = (
                        IdentityRow(page.physical_page, page.lines[issuer_index]),
                        IdentityRow(page.physical_page, page.lines[heading_index]),
                        _identity_row_from_lines(page, period_indexes),
                        _identity_row_from_lines(page, column_indexes),
                    )
                    if _ordered_geometry(*(item.line for item in rows)):
                        candidates.append(rows)

            # An exact target-date consolidated heading with an explicit but
            # different legal-name header is a positive conflicting issuer.
            if any(period_end in observed_dates for observed_dates, _ in dates):
                nearby_names = [line for line in page.lines[max(0, heading_index - 2):heading_index]
                                if re.search(r"(?:股份有限公司|股份公司|有限公司)", _compact(line.text))]
                if nearby_names and not any(_compact(line.text) == expected_compact for line in nearby_names):
                    conflicting = True

    if conflicting:
        raise NativeIdentityRejected("consolidated statement header has a conflicting legal issuer")
    if len(candidates) != 1:
        raise NativeIdentityRejected("unique native current-date consolidated statement header is missing")
    return candidates[0]


def resolve_native_statement_identity(
    pages: tuple[NativePage, ...], *, instrument_id: str, period_end: date,
    pdf_sha256: str, expected_company: str | None = None,
) -> NativeStatementIdentity:
    """Resolve native observations; the production entry point supplies PDF bytes.

    This lower-level function is also the saved-JSON fixture seam. Fixture
    results are test evidence only and cannot substitute for source bytes.
    """
    if not re.fullmatch(r"\d{6}", instrument_id):
        raise NativeIdentityRejected("instrument id must be a six-digit code")
    if (period_end.month, period_end.day) not in {(6, 30), (12, 31)}:
        raise NativeIdentityRejected("unsupported report period")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", pdf_sha256):
        raise NativeIdentityRejected("a full PDF SHA-256 is required")

    pages = tuple(sorted(pages, key=lambda page: page.physical_page))
    if not pages or len({page.physical_page for page in pages}) != len(pages):
        raise NativeIdentityRejected("native PDF pages are missing or duplicated")

    company_row, legal_issuer = _company_profile(pages)
    a_share_row, local_stock_name = _a_share_row(pages, instrument_id)
    if expected_company:
        expected = _compact(expected_company)
        if expected not in {legal_issuer, local_stock_name}:
            raise NativeIdentityRejected("requested company is not report-local profile/table identity")

    _frontmatter_conflict(
        pages, legal_issuer=legal_issuer, local_stock_name=local_stock_name,
        period_end=period_end,
    )
    statement_rows = _statement_header(pages, legal_issuer, period_end)
    return NativeStatementIdentity(
        pdf_sha256=pdf_sha256,
        instrument_id=instrument_id,
        period_end=period_end,
        legal_issuer=legal_issuer,
        local_stock_name=local_stock_name,
        company_name_row=company_row,
        a_share_code_row=a_share_row,
        statement_issuer_row=statement_rows[0],
        statement_heading_row=statement_rows[1],
        statement_period_row=statement_rows[2],
        statement_current_columns_row=statement_rows[3],
    )


def _resolve_source_bound_600988_2024_identity(
    pages: tuple[NativePage, ...], *, instrument_id: str, period_end: date,
    pdf_sha256: str, expected_company: str | None,
) -> NativeStatementIdentity:
    case = _SOURCE_BOUND_600988_2024_REVENUE[(instrument_id, period_end)]
    if expected_company is not None and _compact(expected_company) != _compact(case["company"]):
        raise NativeIdentityRejected("requested company differs from the source-bound issuer")

    company_row, legal_issuer = _company_profile(pages)
    if _compact(legal_issuer) != _compact(case["company"]):
        raise NativeIdentityRejected("native company profile conflicts with the source binding")
    a_share_row, local_stock_name = _source_bound_600988_2024_a_share_row(
        pages, instrument_id, str(case["stock_name"]),
    )
    if a_share_row.page != company_row.page + 1:
        raise NativeIdentityRejected(
            "native A-share stock profile is not adjacent to the issuer profile")

    _source_bound_first_three_conflict(
        pages,
        legal_issuer=legal_issuer,
        local_stock_name=local_stock_name,
        period_end=period_end,
    )
    _frontmatter_conflict(
        pages, legal_issuer=legal_issuer, local_stock_name=local_stock_name,
        period_end=period_end,
    )
    statement_rows = _source_bound_2024_consolidated_balance_sheet(
        pages,
        legal_issuer=legal_issuer,
        period_end=period_end,
        prior_date=case["prior_balance_date"],
    )

    return NativeStatementIdentity(
        pdf_sha256=pdf_sha256,
        instrument_id=instrument_id,
        period_end=period_end,
        legal_issuer=legal_issuer,
        local_stock_name=local_stock_name,
        company_name_row=company_row,
        a_share_code_row=a_share_row,
        statement_issuer_row=statement_rows[0],
        statement_heading_row=statement_rows[1],
        statement_period_row=statement_rows[2],
        statement_current_columns_row=statement_rows[3],
        statement_unit_row=statement_rows[4],
    )


def native_statement_identity_from_pdf_bytes(
    pdf_bytes: bytes, *, instrument_id: str, period_end: date,
    expected_company: str | None = None,
) -> NativeStatementIdentity:
    """Build a candidate identity chain from the supplied PDF bytes only."""
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise NativeIdentityRejected("input must be original PDF bytes")
    pages = _pdf_native_pages(pdf_bytes)
    if is_600988_2024_revenue_source_tuple(instrument_id, period_end):
        case = _SOURCE_BOUND_600988_2024_REVENUE[(instrument_id, period_end)]
        _source_bound_first_three_conflict(
            pages,
            legal_issuer=str(case["company"]),
            local_stock_name=str(case["stock_name"]),
            period_end=period_end,
        )
        if not supports_600988_2024_revenue_source_candidate(
            instrument_id, period_end, pdf_bytes=pdf_bytes,
            expected_company=expected_company,
        ):
            raise NativeIdentityRejected(
                "PDF bytes do not match the exact source-bound 600988 2024 report")
        return _resolve_source_bound_600988_2024_identity(
            pages,
            instrument_id=instrument_id,
            period_end=period_end,
            pdf_sha256="sha256:" + sha256(pdf_bytes).hexdigest(),
            expected_company=expected_company,
        )
    return resolve_native_statement_identity(
        pages,
        instrument_id=instrument_id,
        period_end=period_end,
        pdf_sha256="sha256:" + sha256(pdf_bytes).hexdigest(),
        expected_company=expected_company,
    )
