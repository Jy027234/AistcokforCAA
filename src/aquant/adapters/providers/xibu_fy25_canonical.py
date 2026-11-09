"""Bounded proof for 西部黄金 601069 FY2025 consolidated net profit.

This module reads the archived official PDFs only. It does not decide a general
revision policy, write facts, or admit a value on its own. The caller must pass
the complete, already-validated official-index closure and the actual board
resolution PDF before ``caseClassification`` can become canonical.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import re
from typing import Mapping, Sequence


class SourceEvidenceError(ValueError):
    """The supplied file or native evidence does not match the fixed source role."""


@dataclass(frozen=True)
class ValidatedIndexEvidence:
    """Typed result emitted by the caller's trusted all-category index verifier.

    The object must be constructed only after the caller validates each raw POST,
    page receipt, response hash, company identity, period window, and document
    receipt. An editable JSON flag is not an acceptable substitute.
    """

    schema: str
    instrument_id: str
    fact_period_end: str
    source_report_period_end: str
    canonical_full_report_ids: tuple[str, ...]
    financial_correction_ids: tuple[str, ...]
    regular: Mapping[str, object]
    all_category: Mapping[str, object]
    announcement_map: Mapping[str, Mapping[str, object]]
    evidence_reference: str


_ANNUAL = {
    "announcement_id": "1225082554",
    "title": "西部黄金股份有限公司2025年年度报告",
    "url": "https://static.cninfo.com.cn/finalpage/2026-04-08/1225082554.PDF",
    "published_date": "2026-04-08",
    "sha256": "dd14bb62f8d912618a937251f875853aea1368f21fe1cce8462d183e886cb518",
    "byte_size": 1_570_978,
}
_CORRECTION_NOTICE = {
    "announcement_id": "1225082606",
    "title": "西部黄金股份有限公司关于前期会计差错更正的公告",
    "url": "https://static.cninfo.com.cn/finalpage/2026-04-08/1225082606.PDF",
    "published_date": "2026-04-08",
    "sha256": "9dc75e2b7697cb2a40118036e73de66b4ad795b76488e1422ec332863ead097a",
    "byte_size": 132_707,
}
_BOARD_EXPLANATION = {
    "announcement_id": "1225082555",
    "title": "西部黄金股份有限公司董事会关于前期会计差错更正的说明",
    "url": "https://static.cninfo.com.cn/finalpage/2026-04-08/1225082555.PDF",
    "published_date": "2026-04-08",
    "sha256": "b963f3f01e773dedfdedf7e9fab6e79259be45dc029b6d00ec7a195b9ebffeef",
    "byte_size": 103_121,
}
_CORRECTED_FINANCIALS = {
    "announcement_id": "1225082560",
    "title": "西部黄金股份有限公司关于前期会计差错更正后的财务报表及附注",
    "url": "https://static.cninfo.com.cn/finalpage/2026-04-08/1225082560.PDF",
    "published_date": "2026-04-08",
    "sha256": "ad2fb88d66457c794f32d15ed80e274815ed1d7d48fc3f87932c0d7063e760e6",
    "byte_size": 247_783,
}
_ANNUAL_BOARD_RESOLUTION = {
    "announcement_id": "1225082568",
    "title": "西部黄金股份有限公司第五届董事会第二十四次会议决议公告",
    "url": "https://static.cninfo.com.cn/finalpage/2026-04-08/1225082568.PDF",
    "published_date": "2026-04-08",
    "sha256": "12b0b7362275c7ab76f763a98fac036f0b75bc9d13b4f3ad4946bb56478126d6",
    "byte_size": 159_614,
}
_EXPECTED_CAUSE_IDS = ("1225082555", "1225082560", "1225082606")
_FULL_TEXT_HASH = re.compile(r"^[0-9a-f]{64}$")
_MONEY = re.compile(r"(?<![\d])[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}")
_RELEVANT_REPLACEMENT = str.maketrans({"−": "-", "﹣": "-", "－": "-", " ": " "})


@dataclass(frozen=True)
class _PdfInput:
    role: str
    spec: Mapping[str, object]
    path: Path | None
    source_location: str
    data: bytes
    digest: str
    native_pages: tuple[str, ...]


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value.translate(_RELEVANT_REPLACEMENT))


def _decimal_text(value: str) -> str:
    return value.translate(_RELEVANT_REPLACEMENT).replace(",", "")


def _money_values(text: str) -> list[str]:
    return [_decimal_text(match.group(0)) for match in _MONEY.finditer(text.translate(_RELEVANT_REPLACEMENT))]


def _native_pdf_pages(data: bytes, role: str) -> tuple[str, ...]:
    try:
        import pypdfium2

        with pypdfium2.PdfDocument(data) as document:
            result: list[str] = []
            for page_index in range(len(document)):
                page = document[page_index]
                try:
                    text_page = page.get_textpage()
                    try:
                        text = text_page.get_text_range(errors="strict")
                    finally:
                        text_page.close()
                finally:
                    page.close()
                result.append(text or "")
            return tuple(result)
    except Exception as exc:  # fail closed; OCR or another reader is not a fallback
        raise SourceEvidenceError(f"{role}: native PDFium text read failed") from exc


def _load_pdf(role: str, source_value: bytes | str | Path, spec: Mapping[str, object]) -> _PdfInput:
    if isinstance(source_value, bytes):
        data = source_value
        path = None
        source_location = "in_memory_archive_bytes"
    else:
        path = Path(source_value)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise SourceEvidenceError(f"{role}: PDF cannot be read") from exc
        source_location = str(path.resolve())
    digest = sha256(data).hexdigest()
    expected_digest = str(spec["sha256"])
    expected_size = int(spec["byte_size"])
    if digest != expected_digest or len(data) != expected_size:
        raise SourceEvidenceError(f"{role}: PDF hash or byte size does not match the official source role")
    pages = _native_pdf_pages(data, role)
    if not pages or any("\ufffd" in page for page in pages):
        raise SourceEvidenceError(f"{role}: native PDF text is missing or contains replacement characters")
    return _PdfInput(role, spec, path, source_location, data, digest, pages)


def _line_record(line: Mapping[str, object], physical_page: int) -> dict[str, object]:
    return {
        "physicalPage": physical_page,
        "quote": str(line.get("text", "")).strip(),
        "bboxPointsTopLeftOrigin": [
            round(float(line.get("x0", 0.0)), 2),
            round(float(line.get("top", 0.0)), 2),
            round(float(line.get("x1", 0.0)), 2),
            round(float(line.get("bottom", 0.0)), 2),
        ],
    }


def _page_lines(pdf: _PdfInput, zero_based_page: int) -> list[dict[str, object]]:
    if zero_based_page < 0 or zero_based_page >= len(pdf.native_pages):
        raise SourceEvidenceError(f"{pdf.role}: located page is outside the PDF")
    import pdfplumber

    with pdfplumber.open(BytesIO(pdf.data)) as document:
        page = document.pages[zero_based_page]
        lines = page.extract_text_lines(layout=False, strip=True) or []
        # Both independent readers must see the relevant page text. pdfplumber
        # is used only on PDFium-located pages, not as an all-page fallback.
        compact_pdfium = _compact(pdf.native_pages[zero_based_page])
        compact_pdfplumber = _compact(" ".join(str(line.get("text", "")) for line in lines))
        if not compact_pdfplumber or not compact_pdfium:
            raise SourceEvidenceError(f"{pdf.role}: relevant page is not native searchable text")
        shared = sum(
            1 for token in ("西部黄金", "2025年", "净利润", "人民币", "董事会", "前三季度", "会计差错")
            if token in compact_pdfplumber and token in compact_pdfium
        )
        if shared == 0:
            raise SourceEvidenceError(f"{pdf.role}: native extractors disagree on relevant page content")
        return lines


def _require_line(
    lines: Sequence[Mapping[str, object]],
    predicate,
    description: str,
    physical_page: int,
) -> dict[str, object]:
    hits = [line for line in lines if predicate(str(line.get("text", "")))]
    if len(hits) != 1:
        raise SourceEvidenceError(f"expected one {description} line on physical page {physical_page}; found {len(hits)}")
    return _line_record(hits[0], physical_page)


def _first_matching_page(pages: Sequence[str], predicate, description: str) -> int:
    hits = [index for index, text in enumerate(pages) if predicate(_compact(text))]
    if len(hits) != 1:
        raise SourceEvidenceError(f"expected one {description} page; found {len(hits)}")
    return hits[0]


def _line_money(line: Mapping[str, object], count: int, description: str, physical_page: int) -> list[str]:
    values = _money_values(str(line.get("text", "")))
    if len(values) != count:
        raise SourceEvidenceError(f"{description} expected {count} monetary cells on physical page {physical_page}; found {len(values)}")
    return values


def _find_prefix_line(
    lines: Sequence[Mapping[str, object]], prefix: str, description: str, physical_page: int
) -> Mapping[str, object]:
    compact_prefix = _compact(prefix)
    hits = [line for line in lines if _compact(str(line.get("text", ""))).startswith(compact_prefix)]
    if len(hits) != 1:
        raise SourceEvidenceError(f"expected one {description} line on physical page {physical_page}; found {len(hits)}")
    return hits[0]


def _nearby_money_line(
    lines: Sequence[Mapping[str, object]],
    label_line: Mapping[str, object],
    description: str,
    physical_page: int,
) -> Mapping[str, object]:
    label_top = float(label_line.get("top", 0.0))
    candidates = [
        line for line in lines
        if 0 <= float(line.get("top", 0.0)) - label_top <= 14
        and len(_money_values(str(line.get("text", "")))) >= 2
    ]
    if len(candidates) != 1:
        raise SourceEvidenceError(f"expected one value line near {description} on physical page {physical_page}; found {len(candidates)}")
    return candidates[0]


def _validated_index_closure(
    evidence: ValidatedIndexEvidence | None,
    instrument_id: str,
    target_period_end: str,
) -> tuple[bool, list[str], dict[str, object] | None]:
    if evidence is None:
        return False, ["validated complete all-category index closure was not supplied"], None
    if not isinstance(evidence, ValidatedIndexEvidence):
        return False, ["index evidence was not supplied by the trusted typed closure path"], None
    reasons: list[str] = []
    if evidence.schema != "aquant.cninfo.canonical-source-index-closure.v1":
        reasons.append("index closure schema is not the trusted CNINFO closure type")
    if evidence.instrument_id != instrument_id or evidence.fact_period_end != target_period_end:
        reasons.append("index closure targets a different security or fact period")
    if evidence.source_report_period_end != target_period_end:
        reasons.append("index closure targets a different source-report period")
    if tuple(evidence.canonical_full_report_ids) != (_ANNUAL["announcement_id"],):
        reasons.append("index closure does not identify exactly one FY2025 full annual report")
    if tuple(sorted(evidence.financial_correction_ids)) != tuple(sorted(_EXPECTED_CAUSE_IDS)):
        reasons.append("index closure contains missing or unexpected financial-correction candidates")
    windows: dict[str, dict[str, object]] = {}
    for name, index in (("regular", evidence.regular), ("all_category", evidence.all_category)):
        publication_window = tuple(index.get("publication_window", ()))
        page_receipt_ids = tuple(index.get("page_receipt_ids", ()))
        request_hashes = tuple(index.get("request_body_hashes", ()))
        if publication_window != ("2025-12-31", "2026-11-07"):
            reasons.append(f"{name} index window does not cover FY2025 through 2026-11-07")
        if not page_receipt_ids or len(page_receipt_ids) != len(request_hashes):
            reasons.append(f"{name} index receipts and POST request hashes are missing or mismatched")
        if name == "regular" and len(page_receipt_ids) != 1:
            reasons.append("regular FY2025 annual index is not the single-page complete response")
        if name == "all_category" and (index.get("complete") is not True or len(page_receipt_ids) != 5):
            reasons.append("all-category index is incomplete or does not match the captured five-page window")
        windows[name] = {
            "publicationWindow": list(publication_window),
            "pageReceiptIds": list(page_receipt_ids),
            "requestBodyHashes": list(request_hashes),
            "complete": index.get("complete", name == "regular"),
        }

    expected_sources = {
        str(spec["announcement_id"]): spec
        for spec in (_ANNUAL, _BOARD_EXPLANATION, _CORRECTED_FINANCIALS, _CORRECTION_NOTICE)
    }
    expected_sources[str(_ANNUAL_BOARD_RESOLUTION["announcement_id"])] = _ANNUAL_BOARD_RESOLUTION
    source_records: dict[str, dict[str, object]] = {}
    for announcement_id, expected in expected_sources.items():
        raw_record = evidence.announcement_map.get(announcement_id)
        if not isinstance(raw_record, Mapping):
            reasons.append(f"validated announcement map is missing source {announcement_id}")
            continue
        record = dict(raw_record)
        for field, expected_value in (("announcement_id", announcement_id),
                                      ("url", expected.get("url")),
                                      ("title", expected.get("title")),
                                      ("published_date", expected.get("published_date"))):
            if record.get(field) != expected_value:
                reasons.append(f"announcement {announcement_id} has wrong {field}")
        if expected.get("sha256") and record.get("content_hash") != f"sha256:{expected['sha256']}":
            reasons.append(f"announcement {announcement_id} has wrong source hash")
        if expected.get("byte_size") and record.get("byte_size") != expected["byte_size"]:
            reasons.append(f"announcement {announcement_id} has wrong source byte size")
        if not record.get("receipt_id"):
            reasons.append(f"announcement {announcement_id} has no official PDF receipt")
        digest = str(record.get("content_hash", "")).removeprefix("sha256:")
        if not _FULL_TEXT_HASH.fullmatch(digest):
            reasons.append(f"announcement {announcement_id} has no valid content hash")
        try:
            if int(record.get("byte_size", 0)) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            reasons.append(f"announcement {announcement_id} has no valid byte size")
        source_records[announcement_id] = record
    if not evidence.evidence_reference:
        reasons.append("index closure has no verifiable evidence reference")
    return not reasons, reasons, {
        "schema": evidence.schema,
        "instrumentId": evidence.instrument_id,
        "factPeriodEnd": evidence.fact_period_end,
        "sourceReportPeriodEnd": evidence.source_report_period_end,
        "canonicalFullReportIds": list(evidence.canonical_full_report_ids),
        "financialCorrectionIds": list(evidence.financial_correction_ids),
        "indexWindows": windows,
        "sourceRecords": source_records,
        "evidenceReference": evidence.evidence_reference,
    }


def _fixed_source_record(pdf: _PdfInput) -> dict[str, object]:
    return {
        "role": pdf.role,
        "announcementId": pdf.spec["announcement_id"],
        "title": pdf.spec["title"],
        "url": pdf.spec["url"],
        "publishedDate": pdf.spec["published_date"],
        "sha256": f"sha256:{pdf.digest}",
        "byteSize": len(pdf.data),
        "sourceLocation": pdf.source_location,
    }


def _validate_board_resolution(
    path_value: bytes | str | Path | None,
    evidence: ValidatedIndexEvidence | None,
) -> tuple[_PdfInput | None, dict[str, object] | None, list[str]]:
    if path_value is None:
        return None, None, ["actual annual-board resolution PDF was not supplied"]
    if evidence is None or not isinstance(evidence, ValidatedIndexEvidence):
        return None, None, ["annual-board PDF cannot be tied to a trusted official index receipt"]
    record = dict(evidence.announcement_map.get(str(_ANNUAL_BOARD_RESOLUTION["announcement_id"]), {}))
    digest = str(record.get("content_hash", "")).removeprefix("sha256:")
    if not _FULL_TEXT_HASH.fullmatch(digest):
        return None, None, ["annual-board PDF source record has no valid hash"]
    spec = dict(_ANNUAL_BOARD_RESOLUTION)
    try:
        row_size = int(record["byte_size"])
    except (KeyError, TypeError, ValueError):
        return None, None, ["annual-board PDF source record has no valid byte size"]
    if digest != spec["sha256"] or row_size != spec["byte_size"]:
        return None, None, ["annual-board PDF source record hash or byte size differs from the reviewed official source"]
    try:
        pdf = _load_pdf("annual_board_resolution", path_value, spec)
    except SourceEvidenceError as exc:
        return None, None, [str(exc)]
    return pdf, record, []


def _extract_native_financial_evidence(annual: _PdfInput) -> dict[str, object]:
    pages = annual.native_pages
    cover_page = _first_matching_page(
        pages,
        lambda text: "公司代码：601069" in text and "西部黄金股份有限公司" in text and "2025年年度报告" in text,
        "601069 FY2025 annual-report cover",
    )
    statement_page = _first_matching_page(
        pages,
        lambda text: (
            "合并利润表" in text
            and "2025年1" in text
            and "12月" in text
            and "单位：元" in text
            and "币种：人民币" in text
            and "2025年度" in text
            and "2024年度" in text
        ),
        "FY2025 consolidated income-statement header",
    )
    candidate_rows = []
    for index in range(statement_page, min(statement_page + 3, len(pages))):
        compact = _compact(pages[index])
        if "五、净利润" in compact:
            candidate_rows.append(index)
    if len(candidate_rows) != 1:
        raise SourceEvidenceError(f"annual: expected one consolidated net-profit row after statement header; found {len(candidate_rows)}")
    row_page = candidate_rows[0]
    annual_cover_lines = _page_lines(annual, cover_page)
    statement_lines = _page_lines(annual, statement_page)
    row_lines = _page_lines(annual, row_page)
    cover_title = _require_line(
        annual_cover_lines,
        lambda line: "西部黄金股份有限公司" in line and "2025年年度报告" in _compact(line),
        "annual-report title",
        cover_page + 1,
    )
    company_code = _require_line(
        annual_cover_lines,
        lambda line: "公司代码：601069" in _compact(line),
        "annual-report security code",
        cover_page + 1,
    )
    statement_title = _require_line(statement_lines, lambda line: _compact(line) == "合并利润表", "consolidated income-statement title", statement_page + 1)
    statement_period = _require_line(
        statement_lines,
        lambda line: "2025年1" in _compact(line) and "12月" in _compact(line),
        "full-year statement period",
        statement_page + 1,
    )
    unit_currency = _require_line(
        statement_lines,
        lambda line: "单位：元" in _compact(line) and "币种：人民币" in _compact(line),
        "CNY yuan unit",
        statement_page + 1,
    )
    column_header = _require_line(
        statement_lines,
        lambda line: "2025年度" in _compact(line) and "2024年度" in _compact(line),
        "current and comparative annual columns",
        statement_page + 1,
    )
    compact_header = _compact(str(column_header["quote"]))
    if compact_header.count("2025年度") != 1 or compact_header.count("2024年度") != 1:
        raise SourceEvidenceError("annual: current/prior annual columns are ambiguous")
    target_line = _find_prefix_line(row_lines, "五、净利润", "consolidated net-profit row", row_page + 1)
    target_values = _line_money(target_line, 2, "consolidated net-profit row", row_page + 1)
    attributable_label = _find_prefix_line(
        row_lines, "1.归属于母公司股东的净利润", "attributable net-profit label", row_page + 1
    )
    attributable_value_line = _nearby_money_line(row_lines, attributable_label, "attributable net-profit", row_page + 1)
    attributable_values = _line_money(attributable_value_line, 2, "attributable net-profit", row_page + 1)
    minority_label = _find_prefix_line(row_lines, "2.少数股东损益", "minority-profit label", row_page + 1)
    minority_value_line = _nearby_money_line(row_lines, minority_label, "minority-profit", row_page + 1)
    minority_values = _line_money(minority_value_line, 2, "minority-profit", row_page + 1)
    try:
        consolidated = Decimal(target_values[0])
        attributable = Decimal(attributable_values[0])
        minority = Decimal(minority_values[0])
    except InvalidOperation as exc:
        raise SourceEvidenceError("annual: monetary cell failed exact decimal parsing") from exc
    identity_holds = attributable + minority == consolidated
    if not identity_holds:
        raise SourceEvidenceError("annual: consolidated profit does not reconcile to attributable plus minority profit")

    opinion_page = _first_matching_page(
        pages,
        lambda text: "标准无保留意见" in text and "大信会计师事务所" in text,
        "annual unqualified audit-opinion statement",
    )
    opinion_lines = _page_lines(annual, opinion_page)
    opinion = _require_line(
        opinion_lines,
        lambda line: "标准无保留意见" in line and "大信会计师事务所" in line,
        "annual auditor opinion",
        opinion_page + 1,
    )
    signature_pages = [
        index for index, page_text in enumerate(pages)
        if "大信会计师事务所" in page_text and "中国注册会计师" in page_text and "二○二六年四月七日" in page_text
    ]
    if len(signature_pages) != 1:
        raise SourceEvidenceError(f"annual: expected one auditor signature/date page; found {len(signature_pages)}")
    signature_lines = _page_lines(annual, signature_pages[0])
    signature_date = _require_line(
        signature_lines,
        lambda line: "二○二六年四月七日" in line,
        "annual audit-report date",
        signature_pages[0] + 1,
    )
    board_lines = _page_lines(annual, 1)
    board_attestation = _require_line(
        board_lines,
        lambda line: "董事会及董事" in line and "真实性" in line,
        "annual board responsibility statement",
        2,
    )
    directors_attended = _require_line(
        board_lines,
        lambda line: "全体董事出席董事会会议" in _compact(line),
        "annual all-directors attendance statement",
        2,
    )
    return {
        "sourceIdentity": {
            "announcementId": _ANNUAL["announcement_id"],
            "title": _ANNUAL["title"],
            "reportKind": "FULL_ANNUAL_REPORT",
            "instrumentId": "601069",
            "periodStart": "2025-01-01",
            "periodEnd": "2025-12-31",
            "publishedDate": _ANNUAL["published_date"],
            "sha256": f"sha256:{annual.digest}",
            "byteSize": len(annual.data),
        },
        "annualCurrentCell": {
            "metric": "net_profit_consolidated",
            "scope": "CONSOLIDATED",
            "statement": "合并利润表",
            "periodKind": "FULL_YEAR",
            "periodHeader": statement_period,
            "currentColumnHeader": "2025年度",
            "unit": "CNY yuan",
            "valueYuan": target_values[0],
            "sourceRow": _line_record(target_line, row_page + 1),
            "sourceContext": {
                "reportTitle": cover_title,
                "companyCode": company_code,
                "statementTitle": statement_title,
                "unitAndCurrency": unit_currency,
                "currentAndPriorColumns": column_header,
            },
        },
        "independentProfitIdentity": {
            "method": "same_statement_distinct_rows_decimal_identity",
            "equationYuan": f"{attributable_values[0]} + ({minority_values[0]}) = {target_values[0]}",
            "holds": identity_holds,
            "attributableProfit": _line_record(attributable_value_line, row_page + 1),
            "minorityProfitLabel": _line_record(minority_label, row_page + 1),
            "minorityProfit": _line_record(minority_value_line, row_page + 1),
        },
        "auditEvidence": {
            "auditor": "大信会计师事务所（特殊普通合伙）",
            "opinion": opinion,
            "signatureDate": signature_date,
        },
        "annualBoardStatements": {
            "boardResponsibility": board_attestation,
            "allDirectorsAttended": directors_attended,
        },
    }


def _extract_correction_evidence(
    correction_notice: _PdfInput,
    board_notice: _PdfInput,
    corrected_financials: _PdfInput,
) -> dict[str, object]:
    notice_pages = correction_notice.native_pages
    cause_pages = board_notice.native_pages
    corrected_pages = corrected_financials.native_pages
    if len(notice_pages) != 5 or len(cause_pages) != 5 or len(corrected_pages) != 34:
        raise SourceEvidenceError("correction package PDF page counts do not match the captured source versions")
    notice_first = _page_lines(correction_notice, 0)
    correction_title = _require_line(
        notice_first,
        lambda line: "关于前期会计差错更正的公告" in _compact(line),
        "correction announcement title",
        1,
    )
    scope_line = _require_line(
        notice_first,
        lambda line: "前三季度已披露的合并财务报表" in _compact(line),
        "Q1-Q3 disclosed-consolidated-statements scope",
        1,
    )
    correction_notice_compact = _compact(notice_pages[0])
    if "审计委员会2026年第一次会议" not in correction_notice_compact or "第五届董事会第二十四次会议审议通过" not in correction_notice_compact:
        raise SourceEvidenceError("correction notice lacks the direct committee/board approval chain")
    correction_approval_lines = [
        line for line in notice_first
        if "审计委员会" in str(line.get("text", ""))
        or "第五届董事会第二十四次会议" in _compact(str(line.get("text", "")))
    ]
    if len(correction_approval_lines) < 2:
        raise SourceEvidenceError("correction notice approval statement could not be localized")
    board_first = _page_lines(board_notice, 0)
    cause_page_compact = _compact(cause_pages[0])
    required_cause_phrases = (
        "阿克陶科邦锰业制造有限公司",
        "宁夏天元锰业集团有限公司",
        "金属锰销售（包装代交货）合同",
        "企业会计准则第14号",
        "2025年前三季度确认了相关收益",
    )
    if not all(phrase in cause_page_compact for phrase in required_cause_phrases):
        raise SourceEvidenceError("board correction explanation does not bind the stated subsidiary, counterparty, contract, and Q1-Q3 cause")
    cause_contract_lines = [
        line for line in board_first
        if "金属锰销售" in str(line.get("text", "")) or "包装代交货" in str(line.get("text", ""))
    ]
    cause_period_lines = [
        line for line in board_first
        if "2025年前三" in _compact(str(line.get("text", "")))
        or "季度确认了相关收益" in _compact(str(line.get("text", "")))
        or "准则第14号" in _compact(str(line.get("text", "")))
    ]
    if not cause_contract_lines or not cause_period_lines:
        raise SourceEvidenceError("board correction explanation lacks direct transaction/cause text")
    cause_quote_refs = [
        _line_record(line, 1) for line in (*cause_contract_lines, *cause_period_lines)
    ]
    period_labels = ("2025-03-31", "2025-06-30", "2025-09-30")
    correction_pages = (1, 2, 3)
    quarter_rows: list[dict[str, object]] = []
    for period_end, zero_page in zip(period_labels, correction_pages, strict=True):
        lines = _page_lines(board_notice, zero_page)
        row = _find_prefix_line(lines, "净利润", "consolidated net-profit correction row", zero_page + 1)
        before, after, delta = _line_money(row, 3, "consolidated net-profit correction row", zero_page + 1)
        if Decimal(before) + Decimal(delta) != Decimal(after):
            raise SourceEvidenceError(f"correction: before/after/delta arithmetic fails for {period_end}")
        quarter_rows.append({
            "periodEnd": period_end,
            "periodKind": {"2025-03-31": "Q1_CUMULATIVE", "2025-06-30": "H1_CUMULATIVE", "2025-09-30": "Q3_CUMULATIVE"}[period_end],
            "metric": "net_profit_consolidated",
            "scope": "CONSOLIDATED",
            "beforeYuan": before,
            "afterYuan": after,
            "deltaYuan": delta,
            "sourceRow": _line_record(row, zero_page + 1),
        })
    corrected_title_page = _first_matching_page(
        corrected_pages,
        lambda text: "前期会计差错更正后的财务报表及附注" in text and "2025年第一季度报告" in text,
        "corrected financial statements title and covered periods",
    )
    corrected_first = _page_lines(corrected_financials, corrected_title_page)
    corrected_title = _require_line(
        corrected_first,
        lambda line: "前期会计差错更正后的财务报表及附注" in _compact(line),
        "corrected-financials title",
        corrected_title_page + 1,
    )
    approval_lines = [
        line for line in corrected_first
        if "2026年3月26日" in _compact(str(line.get("text", "")))
        or "2026年4月7日" in _compact(str(line.get("text", "")))
    ]
    if len(approval_lines) < 2:
        raise SourceEvidenceError("corrected-financials cover lacks the actual committee/board meeting dates")
    correction_auditor_page = _first_matching_page(
        notice_pages,
        lambda text: "会计师事务所意见" in text and "大信会计师事务所" in text,
        "correction auditor opinion",
    )
    correction_auditor_lines = _page_lines(correction_notice, correction_auditor_page)
    correction_auditor_compact = _compact(notice_pages[correction_auditor_page])
    if "大信会计师事务所（特殊普通合伙）认为" not in correction_auditor_compact or "如实反映了公司的前期差错更正情况" not in correction_auditor_compact:
        raise SourceEvidenceError("correction notice does not contain a direct matching-auditor conclusion")
    auditor_name_line = _require_line(
        correction_auditor_lines,
        lambda line: "大信会计师事务所" in line and "认为" in line,
        "correction auditor identity/opinion line",
        correction_auditor_page + 1,
    )
    auditor_conclusion_line = _require_line(
        correction_auditor_lines,
        lambda line: "公司的前期差错更正情况" in _compact(line),
        "correction auditor conclusion line",
        correction_auditor_page + 1,
    )
    unaudited_periods_line = _require_line(
        correction_auditor_lines,
        lambda line: "未经审计" in line,
        "correction notice unaudited-quarterly-period note",
        correction_auditor_page + 1,
    )
    h1_rows = []
    for index, text in enumerate(corrected_pages):
        compact = _compact(text)
        if "合并利润表" in compact and "2025年1" in compact and "6月" in compact and "单位：元" in compact and "币种：人民币" in compact:
            h1_rows.append(index)
    if len(h1_rows) != 1:
        raise SourceEvidenceError(f"corrected financials: expected one H1 consolidated income-statement page; found {len(h1_rows)}")
    h1_statement_page = h1_rows[0]
    h1_candidates = []
    for index in range(h1_statement_page, min(h1_statement_page + 3, len(corrected_pages))):
        if "五、净利润" in _compact(corrected_pages[index]):
            h1_candidates.append(index)
    if len(h1_candidates) != 1:
        raise SourceEvidenceError(f"corrected financials: expected one H1 consolidated net-profit row; found {len(h1_candidates)}")
    h1_row_page = h1_candidates[0]
    h1_lines = _page_lines(corrected_financials, h1_row_page)
    h1_row = _find_prefix_line(h1_lines, "五、净利润", "corrected H1 consolidated net-profit row", h1_row_page + 1)
    h1_values = _line_money(h1_row, 2, "corrected H1 consolidated net-profit row", h1_row_page + 1)
    if h1_values[0] != quarter_rows[1]["afterYuan"]:
        raise SourceEvidenceError("corrected H1 financial statement does not match the board-disclosed corrected H1 net profit")
    if not any("2025年半年度报告" in _compact(text) for text in corrected_pages):
        raise SourceEvidenceError("corrected financial statements do not explicitly identify the H1 report")

    return {
        "classification": "Q1_Q3_INTERIM_CORRECTION_SCOPE",
        "correctionNotice": {
            "sourceAnnouncementId": _CORRECTION_NOTICE["announcement_id"],
            "scope": "2025 Q1-Q3 previously disclosed consolidated financial statements",
            "scopeQuote": scope_line,
            "approvalQuotes": [_line_record(line, 1) for line in correction_approval_lines],
        },
        "causalScope": {
            "summary": "A subsidiary correction under PRC Accounting Standard No. 14 (revenue) concerning 2025 first-three-quarter consolidated statements.",
            "directQuotes": cause_quote_refs,
            "targetMetricRows": quarter_rows,
            "annualAmountOrEffect": "NOT_ASSERTED",
        },
        "correctedFinancials": {
            "sourceAnnouncementId": _CORRECTED_FINANCIALS["announcement_id"],
            "title": corrected_title,
            "approvalMeetingQuotes": [_line_record(line, corrected_title_page + 1) for line in approval_lines],
            "h1ConsolidatedNetProfit": _line_record(h1_row, h1_row_page + 1),
            "h1ValueYuan": h1_values[0],
            "matchesBoardDisclosedAfterValue": True,
        },
        "auditorEvidence": {
            "auditor": "大信会计师事务所（特殊普通合伙）",
            "scope": "review of the disclosed prior-period correction statement; quarterly financial statements noted as unaudited",
            "auditorIdentityAndOpinion": auditor_name_line,
            "conclusion": auditor_conclusion_line,
            "unauditedQuarterlyReports": unaudited_periods_line,
        },
        "boardExplanation": {
            "sourceAnnouncementId": _BOARD_EXPLANATION["announcement_id"],
            "quarterlyCorrectionRows": quarter_rows,
        },
    }


def _extract_annual_board_approval(
    board_pdf: _PdfInput | None,
    source_record: Mapping[str, object] | None,
) -> tuple[dict[str, object] | None, list[str]]:
    if board_pdf is None or source_record is None:
        return None, ["actual annual-board resolution PDF is required to prove both annual-report and correction motions"]
    pages = board_pdf.native_pages
    flat = _compact("".join(pages))
    if "第五届董事会第二十四次会议决议公告" not in flat:
        return None, ["annual-board PDF content does not match the official board-resolution title"]
    if "2025年年度报告" not in flat or "前期会计差错更正" not in flat:
        return None, ["annual-board resolution does not show both the annual-report and correction motions"]

    def approval_present(text: str) -> bool:
        compact = _compact(text)
        return "审议通过" in compact or "审议并通过" in compact

    annual_motion_pages = [index for index, text in enumerate(pages) if "2025年年度报告" in _compact(text) and approval_present(text)]
    correction_motion_pages = [index for index, text in enumerate(pages) if "前期会计差错更正" in _compact(text) and approval_present(text)]
    if not annual_motion_pages or not correction_motion_pages:
        return None, ["annual-board resolution lacks direct approval language for both motions"]
    if "公告编号：2026-018" not in flat or "西部黄金股份有限公司关于前期会计差错更正的公告" not in flat:
        return None, ["annual-board correction motion is not explicitly linked to correction announcement 2026-018"]
    meeting_date_pages = [index for index, text in enumerate(pages) if "2026年4月7日" in _compact(text)]
    if not meeting_date_pages:
        return None, ["annual-board resolution does not show the meeting date needed to bind the correction approval"]
    title_lines = _page_lines(board_pdf, 0)
    meeting_title = _require_line(
        title_lines,
        lambda line: "第五届董事会第二十四次会议决议公告" in _compact(line),
        "actual board meeting title",
        1,
    )
    records: list[dict[str, object]] = []
    for index in sorted(set(annual_motion_pages + correction_motion_pages + meeting_date_pages)):
        lines = _page_lines(board_pdf, index)
        for line in lines:
            compact = _compact(str(line.get("text", "")))
            if ("2025年年度报告" in compact and ("审议通过" in compact or "审议并通过" in compact)) or ("前期会计差错更正" in compact and ("审议通过" in compact or "审议并通过" in compact)) or "2026年4月7日" in compact:
                records.append(_line_record(line, index + 1))
            elif "公告编号：2026-018" in compact:
                records.append(_line_record(line, index + 1))
    records.insert(0, meeting_title)
    if not records:
        return None, ["annual-board resolution evidence could not be localized"]
    return {
        "announcementId": _ANNUAL_BOARD_RESOLUTION["announcement_id"],
        "title": _ANNUAL_BOARD_RESOLUTION["title"],
        "url": _ANNUAL_BOARD_RESOLUTION["url"],
        "publishedDate": _ANNUAL_BOARD_RESOLUTION["published_date"],
        "sha256": f"sha256:{board_pdf.digest}",
        "byteSize": len(board_pdf.data),
        "indexSourceRecord": dict(source_record),
        "sameBoardMeetingApprovedAnnualAndCorrection": True,
        "approvalQuotes": records,
    }, []


def extract_canonical_annual_evidence(
    annual_pdf: bytes | str | Path,
    correction_notice: bytes | str | Path,
    board_notice: bytes | str | Path,
    corrected_financials: bytes | str | Path,
    *,
    instrument_id: str,
    target_period_end: str,
    annual_board_resolution_pdf: bytes | str | Path | None = None,
    index_evidence: ValidatedIndexEvidence | None = None,
) -> dict[str, object]:
    """Extract a bounded, source-native evidence packet for the 601069 FY25 fact.

    The annual amount is read from the PDF and reconciled from two distinct rows.
    The three interim corrections are preserved as a separate scoped case. The
    helper returns ``NOT_ADMITTED`` unless the trusted all-category closure and
    the actual 2026-04-07 board resolution PDF prove the annual and correction
    motions were both approved by that meeting.
    """
    if instrument_id != "601069" or target_period_end != "2025-12-31":
        raise SourceEvidenceError("this bounded source proof supports only 601069 FY2025")
    annual = _load_pdf("annual", annual_pdf, _ANNUAL)
    correction = _load_pdf("correction_notice", correction_notice, _CORRECTION_NOTICE)
    board = _load_pdf("board_notice", board_notice, _BOARD_EXPLANATION)
    corrected = _load_pdf("corrected_financials", corrected_financials, _CORRECTED_FINANCIALS)
    annual_evidence = _extract_native_financial_evidence(annual)
    correction_evidence = _extract_correction_evidence(correction, board, corrected)
    closure_ok, closure_reasons, closure_record = _validated_index_closure(
        index_evidence, instrument_id, target_period_end
    )
    board_pdf, board_source_record, board_pdf_reasons = _validate_board_resolution(
        annual_board_resolution_pdf, index_evidence
    )
    board_approval, board_content_reasons = _extract_annual_board_approval(board_pdf, board_source_record)
    same_auditor = (
        annual_evidence["auditEvidence"]["auditor"]
        == correction_evidence["auditorEvidence"]["auditor"]
    )
    auditor_reasons = [] if same_auditor else ["annual and correction auditor identities do not match"]
    admitted = closure_ok and board_pdf is not None and board_approval is not None and not board_pdf_reasons and not board_content_reasons and same_auditor
    reasons = [*closure_reasons, *board_pdf_reasons, *board_content_reasons, *auditor_reasons]
    source_records = [
        _fixed_source_record(annual),
        _fixed_source_record(correction),
        _fixed_source_record(board),
        _fixed_source_record(corrected),
    ]
    if board_pdf is not None:
        source_records.append({
            "role": "annual_board_resolution",
            "announcementId": _ANNUAL_BOARD_RESOLUTION["announcement_id"],
            "title": _ANNUAL_BOARD_RESOLUTION["title"],
            "url": _ANNUAL_BOARD_RESOLUTION["url"],
            "publishedDate": _ANNUAL_BOARD_RESOLUTION["published_date"],
            "sha256": f"sha256:{board_pdf.digest}",
            "byteSize": len(board_pdf.data),
            "sourceLocation": board_pdf.source_location,
        })
    return {
        "schema": "aquant.xibu-fy25-canonical-source-evidence.v1",
        "target": {
            "instrumentId": instrument_id,
            "metric": "net_profit_consolidated",
            "periodEnd": target_period_end,
        },
        "caseClassification": "CONCURRENT_FULL_YEAR_AND_INTERIM_CORRECTION_PACKAGE" if admitted else "NOT_ADMITTED",
        "formalAdmission": "NOT_ADMITTED",
        "canonicalSourceProof": "PASS" if admitted else "INCOMPLETE",
        "notAdmittedReasons": reasons,
        "sourceRecords": source_records,
        "annualSourceEvidence": annual_evidence,
        "interimCorrectionEvidence": correction_evidence,
        "annualBoardApprovalEvidence": board_approval,
        "auditorContinuity": {
            "sameAuditorForAnnualAndCorrectionEvidence": same_auditor,
            "annualAuditEvidence": annual_evidence["auditEvidence"],
            "correctionAuditEvidence": correction_evidence["auditorEvidence"],
        },
        "validatedIndexClosure": closure_record,
        "revisionDisposition": {
            "state": "CURRENT_FULL_YEAR_DIRECT_SOURCE_WITH_Q1_Q3_CORRECTION_SCOPE" if admitted else "UNRESOLVED",
            "q1Q3CorrectionScope": True,
            "annualCorrectionAmount": "NOT_ASSERTED",
            "annualNoEffect": "NOT_ASSERTED",
            "q4ArithmeticBridge": "NOT_REQUIRED_FOR_THIS_SOURCE_CLASSIFICATION_AND_NOT_INFERRED",
            "metricSpecificAnnualImpactDisposition": "NOT_ASSERTED",
        },
    }
