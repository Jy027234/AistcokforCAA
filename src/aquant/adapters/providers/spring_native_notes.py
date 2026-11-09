"""Source-bound native consolidated-note candidates for Spring Airlines.

The annual-report statement pages for this source are image-only. This path
reads only the original PDF's searchable consolidated notes and the report's
native summary table; it never converts an image or claims a native main-table
extraction.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Mapping

from .cninfo_financial_pdf import CninfoS2CandidateFacts, PdfCandidateFact
from .cninfo_statement_sections import (
    check_report_cover,
)
from .s2_machine_verification import (
    FieldVerification,
    IndependentCheck,
    MachineVerification,
    MachineVerificationError,
)


VERSION_LABEL = "spring_native_consolidated_note_candidate_v1"
VERIFIER_VERSION = "spring-native-note-crosscheck-v1"
_COMPANY = "春秋航空股份有限公司"
_CNINFO_URL = "https://static.cninfo.com.cn/finalpage/{date}/{announcement}.PDF"
_NOTE_UNIT = "(除特别注明外，金额单位为人民币元)"
_AMOUNT = re.compile(r"(?<!\d)-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class _Profile:
    digest: str
    period_end: date
    announcement_id: str
    url_date: str
    page_count: int
    revenue_page: int
    report_summary_page: int = 8

    @property
    def url(self) -> str:
        return _CNINFO_URL.format(date=self.url_date, announcement=self.announcement_id)

    @property
    def fields(self) -> frozenset[str]:
        if self.period_end == date(2025, 12, 31):
            return frozenset({
                "revenue", "net_profit_attributable", "net_profit_consolidated",
                "operating_cashflow",
            })
        return frozenset({"revenue"})


_PROFILES: Mapping[str, _Profile] = {
    "d865b40eeccba86f465d98282989d8aacc410806f021c126cfc56de3a4f176d3": _Profile(
        digest="sha256:d865b40eeccba86f465d98282989d8aacc410806f021c126cfc56de3a4f176d3",
        period_end=date(2025, 12, 31), announcement_id="1225093115",
        url_date="2026-04-11", page_count=203, revenue_page=171,
    ),
    "b9c6908049acecc529701a420b70881fb0484ce61cd20cf6f0334d0c26acba18": _Profile(
        digest="sha256:b9c6908049acecc529701a420b70881fb0484ce61cd20cf6f0334d0c26acba18",
        period_end=date(2024, 12, 31), announcement_id="1223415192",
        url_date="2025-04-30", page_count=208, revenue_page=177,
    ),
}


class SpringNativeNoteError(MachineVerificationError):
    """The exact original report does not satisfy the native-note contract."""


def _profile_for_bytes(pdf_bytes: bytes) -> _Profile | None:
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        return None
    key = hashlib.sha256(pdf_bytes).hexdigest()
    return _PROFILES.get(key)


def is_supported_pdf(pdf_bytes: bytes) -> bool:
    """Return true only for the two source PDFs reviewed for this candidate."""

    return _profile_for_bytes(pdf_bytes) is not None


def _validate_binding(
    pdf_bytes: bytes, *, instrument_id: str, company: str, period_end: date,
    announcement_id: str, document_url: str, required_fields: tuple[str, ...],
) -> _Profile:
    profile = _profile_for_bytes(pdf_bytes)
    if profile is None:
        raise SpringNativeNoteError("PDF hash is not one of the reviewed Spring Airlines reports")
    if (instrument_id != "601021" or company != _COMPANY or
            period_end != profile.period_end or
            announcement_id != profile.announcement_id or document_url != profile.url):
        raise SpringNativeNoteError("Spring Airlines PDF identity does not match its reviewed source")
    fields = tuple(sorted(required_fields))
    if (len(fields) != len(set(fields)) or not fields or
            not set(fields) <= profile.fields):
        raise SpringNativeNoteError("required fields do not match this report profile")
    return profile


def _compact(value: str) -> str:
    return _SPACE.sub("", value)


def _pdf_pages(pdf_bytes: bytes):
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - runtime dependency guard
        raise SpringNativeNoteError("pdfplumber is required for native PDF text extraction") from exc
    try:
        pdf = pdfplumber.open(io.BytesIO(pdf_bytes))
    except Exception as exc:  # noqa: BLE001
        raise SpringNativeNoteError(f"cannot open source PDF: {type(exc).__name__}") from exc
    return pdf


def _page_context(page, *, page_number: int, profile: _Profile,
                  note_heading: str | None = None) -> dict[str, object]:
    text = page.extract_text(layout=False) or ""
    compact = _compact(text)
    year = profile.period_end.year
    if _COMPANY not in compact:
        raise SpringNativeNoteError(f"page {page_number} lacks the exact legal issuer name")
    period_phrase = f"{year}年12月31日止年度"
    if _compact(period_phrase) not in compact:
        raise SpringNativeNoteError(f"page {page_number} lacks the exact report period")
    if _compact(_NOTE_UNIT) not in compact:
        raise SpringNativeNoteError(f"page {page_number} lacks the exact consolidated-note unit")
    if "四、合并财务报表项目附注" not in compact:
        raise SpringNativeNoteError(f"page {page_number} is not inside the consolidated-note section")
    if note_heading and _compact(note_heading) not in compact:
        raise SpringNativeNoteError(f"page {page_number} lacks note heading {note_heading!r}")
    current = f"{year}年度"
    prior = f"{year - 1}年度"
    if current not in compact or prior not in compact:
        raise SpringNativeNoteError(f"page {page_number} lacks paired current/prior year headers")
    lines = _lines_with_geometry(page)
    header_lines = [line for line in lines
                    if current in _compact(line["text"]) and prior in _compact(line["text"])]
    if not header_lines:
        raise SpringNativeNoteError(f"page {page_number} has no single current/prior column-header row")
    return {
        "page": page_number,
        "role": "consolidated_note",
        "issuer": _COMPANY,
        "period_end": profile.period_end.isoformat(),
        "consolidated_note_section": "四、合并财务报表项目附注",
        "amount_unit": _NOTE_UNIT,
        "currency": "CNY",
        "scale": "yuan",
        "current_column": current,
        "prior_column": prior,
        "header_text": header_lines[0]["text"],
        "header_bbox": header_lines[0]["bbox"],
    }


def _lines_with_geometry(page) -> list[dict[str, object]]:
    words = sorted(page.extract_words(x_tolerance=2, y_tolerance=2),
                   key=lambda word: (float(word["top"]), float(word["x0"])))
    groups: list[list[dict[str, object]]] = []
    for word in words:
        top = float(word["top"])
        if not groups or abs(top - float(groups[-1][0]["top"])) > 2.5:
            groups.append([word])
        else:
            groups[-1].append(word)
    result = []
    for group in groups:
        group.sort(key=lambda word: float(word["x0"]))
        text = " ".join(str(word["text"]) for word in group)
        result.append({
            "text": text,
            "bbox": {
                "x0": round(min(float(word["x0"]) for word in group), 2),
                "x1": round(max(float(word["x1"]) for word in group), 2),
                "top": round(min(float(word["top"]) for word in group), 2),
                "bottom": round(max(float(word["bottom"]) for word in group), 2),
            },
            "words": group,
        })
    return result


def _find_line(page, *, page_number: int, label: str,
               current_cell: str | None = None, prior_cell: str | None = None,
               profile: _Profile, allow_unlabelled: bool = False) -> dict[str, object]:
    matches = []
    compact_label = _compact(label)
    for line in _lines_with_geometry(page):
        compact_text = _compact(str(line["text"]))
        number_strings = [match.group(0) for match in _AMOUNT.finditer(str(line["text"]))]
        if len(number_strings) < 2:
            continue
        has_target_pair = (
            current_cell is not None and current_cell in number_strings and
            prior_cell is not None and prior_cell in number_strings
        )
        if label and not compact_text.startswith(compact_label) and not (allow_unlabelled and has_target_pair):
            continue
        if current_cell is not None and current_cell not in number_strings:
            continue
        if prior_cell is not None and prior_cell not in number_strings:
            continue
        if not label and not allow_unlabelled:
            continue
        matches.append((line, number_strings))
    if len(matches) != 1:
        raise SpringNativeNoteError(
            f"page {page_number} row {label!r} is not unique (found {len(matches)})")
    line, numbers = matches[0]
    if len(numbers) < 2:
        raise SpringNativeNoteError(f"page {page_number} row {label!r} lacks two amount cells")
    current_text = current_cell or numbers[0]
    prior_text = prior_cell or numbers[1]
    cells = []
    for target in (current_text, prior_text):
        cell_words = [word for word in line["words"] if str(word["text"]) == target]
        if len(cell_words) != 1:
            raise SpringNativeNoteError(
                f"page {page_number} row {label!r} amount {target!r} has no unique native text box")
        word = cell_words[0]
        cells.append({
            "text": target,
            "bbox": {key: round(float(word[key]), 2)
                     for key in ("x0", "x1", "top", "bottom")},
        })
    return {
        "label": label,
        "page": page_number,
        "raw_text": line["text"],
        "line_bbox": line["bbox"],
        "current_cell": cells[0],
        "prior_cell": cells[1],
        "current_value_yuan": str(Decimal(current_text.replace(",", ""))),
        "prior_value_yuan": str(Decimal(prior_text.replace(",", ""))),
        "current_column": f"{profile.period_end.year}年度",
        "prior_column": f"{profile.period_end.year - 1}年度",
        "amount_unit": _NOTE_UNIT,
        "currency": "CNY",
        "scale": "yuan",
    }


def _note_row_geometry(pdf, *, profile: _Profile, row, page_number: int,
                       page_context: dict[str, object]) -> dict[str, object]:
    page = pdf.pages[page_number - 1]
    return {
        **_find_line(
            page, page_number=page_number, label=row.label,
            current_cell=row.current_cell, prior_cell=row.prior_cell,
            profile=profile, allow_unlabelled=(row.label == "合计"),
        ),
        "page_context": page_context,
    }


def _amount_row(page, *, page_number: int, label: str, profile: _Profile,
                context: dict[str, object]) -> tuple[Decimal, dict[str, object]]:
    row = _find_line(page, page_number=page_number, label=label, profile=profile)
    value = Decimal(str(row["current_value_yuan"]))
    return value, {**row, "page_context": context}


def _report_summary(pdf, *, profile: _Profile, field_label: str) -> tuple[Decimal, dict[str, object]]:
    page_number = profile.report_summary_page
    page = pdf.pages[page_number - 1]
    text = _compact(page.extract_text(layout=False) or "")
    year = profile.period_end.year
    if (_COMPANY not in text or f"{year}年年度报告" not in text or
            "七、近三年主要会计数据和财务指标" not in text or
            "单位：元币种：人民币" not in text):
        raise SpringNativeNoteError(f"page {page_number} lacks exact report-summary identity/unit")
    current_header = f"{year}年"
    prior_header = f"{year - 1}年"
    if current_header not in text or prior_header not in text:
        raise SpringNativeNoteError(f"page {page_number} lacks the summary current/prior columns")
    context = {
        "page": page_number,
        "role": "report_summary",
        "issuer": _COMPANY,
        "period_end": profile.period_end.isoformat(),
        "report_title": f"{year}年年度报告",
        "amount_unit": "单位：元，币种：人民币",
        "currency": "CNY",
        "scale": "yuan",
        "current_column": current_header,
        "prior_column": prior_header,
    }
    value, row = _amount_row(page, page_number=page_number, label=field_label,
                             profile=profile, context=context)
    row["current_column"] = current_header
    row["prior_column"] = prior_header
    return value, row


def _revenue_proof(pdf_bytes: bytes, pdf, *, profile: _Profile) -> tuple[Decimal, dict[str, object], dict[str, object]]:
    page = pdf.pages[profile.revenue_page - 1]
    note_heading = ("38. 营业收入和营业成本" if profile.period_end.year == 2025
                    else "37. 营业收入和营业成本")
    context = _page_context(
        page, page_number=profile.revenue_page, profile=profile,
        note_heading=note_heading,
    )
    main_value, main = _amount_row(
        page, page_number=profile.revenue_page, label="主营业务收入",
        profile=profile, context=context,
    )
    other_value, other = _amount_row(
        page, page_number=profile.revenue_page, label="其他业务收入",
        profile=profile, context=context,
    )
    primary_value = main_value + other_value
    prior_total = Decimal(str(main["prior_value_yuan"])) + Decimal(str(other["prior_value_yuan"]))
    current_cell = f"{primary_value:,.0f}"
    prior_cell = f"{prior_total:,.0f}"
    total = _find_line(
        page, page_number=profile.revenue_page, label="合计",
        current_cell=current_cell, prior_cell=prior_cell, profile=profile,
        allow_unlabelled=True,
    )
    total["page_context"] = context
    total_value = Decimal(total["current_value_yuan"])
    summary_value, summary = _report_summary(pdf, profile=profile, field_label="营业收入")
    if primary_value != total_value or primary_value != summary_value:
        raise SpringNativeNoteError("revenue note components, total, and report summary do not agree")
    primary = {
        "basis": "native_consolidated_note_revenue_decomposition",
        "note_heading": note_heading,
        "note_page": profile.revenue_page,
        "page_context": context,
        "main_business": main,
        "other_business": other,
        "total": total,
        "current_revenue_yuan": str(primary_value),
    }
    independent = {
        "basis": "same_report_summary_on_different_page",
        "summary_row": summary,
        "different_page_from_primary": summary["page"] != profile.revenue_page,
        "components_sum_to_note_total": str(
            Decimal(main["current_value_yuan"]) + Decimal(other["current_value_yuan"])
        ) == str(total_value),
        "summary_equals_note_total": summary_value == total_value,
    }
    return primary_value, primary, independent


def _field_proof(pdf_bytes: bytes, pdf, *, profile: _Profile, field: str) -> tuple[Decimal, dict[str, object]]:
    year = profile.period_end.year
    if field == "revenue":
        value, primary, independent = _revenue_proof(pdf_bytes, pdf, profile=profile)
        return value, {
            "field": field, "primary": primary, "independent": independent,
            "comparison": "note main-business + other-business = note total = report-summary current cell",
        }

    pages = {
        "net_profit_attributable": (171, "37. 未分配利润",
                                    "加：本年归属于母公司股东的净利润"),
        "net_profit_consolidated": (179, "54. 现金流量表补充资料",
                                    "净利润"),
        "operating_cashflow": (179, "54. 现金流量表补充资料",
                               "经营活动产生的现金流量净额"),
    }
    page_number, note_heading, label = pages[field]
    page = pdf.pages[page_number - 1]
    context = _page_context(page, page_number=page_number, profile=profile,
                            note_heading=note_heading)
    primary_value, primary_row = _amount_row(
        page, page_number=page_number, label=label, profile=profile, context=context)
    if field == "net_profit_attributable":
        eps_page_number = 177
        eps_page = pdf.pages[eps_page_number - 1]
        eps_context = _page_context(
            eps_page, page_number=eps_page_number, profile=profile,
            note_heading="52. 每股收益",
        )
        direct_value, direct_row = _amount_row(
            eps_page, page_number=eps_page_number,
            label="归属于母公司普通股股东的合并净利润",
            profile=profile, context=eps_context,
        )
        summary_value, summary = _report_summary(
            pdf, profile=profile, field_label="归属于上市公司股东的净利润")
        if direct_value != primary_value or summary_value != primary_value:
            raise SpringNativeNoteError("attributable-profit rows do not agree")
        independent = {
            "basis": "direct_eps_numerator_and_report_summary",
            "direct_profit_numerator": direct_row,
            "report_summary_row": summary,
            "ratio_back_calculation_used": False,
            "different_page_from_primary": eps_page_number != page_number,
        }
        comparison = "note37 attributable profit = direct note52 numerator = report-summary current cell"
    elif field == "net_profit_consolidated":
        tax_page_number = 176
        tax_page = pdf.pages[tax_page_number - 1]
        tax_context = _page_context(
            tax_page, page_number=tax_page_number, profile=profile,
            note_heading="51. 所得税费用",
        )
        pbt, pbt_row = _amount_row(
            tax_page, page_number=tax_page_number, label="利润总额",
            profile=profile, context=tax_context,
        )
        tax_expense, tax_row = _amount_row(
            tax_page, page_number=tax_page_number, label="所得税费用",
            profile=profile, context=tax_context,
        )
        if "基于合并利润表的利润总额" not in _compact(tax_page.extract_text() or ""):
            raise SpringNativeNoteError("tax note does not identify consolidated income-statement profit before tax")
        recomputed = pbt - tax_expense
        if recomputed != primary_value:
            raise SpringNativeNoteError("consolidated net profit differs from profit-before-tax less income tax")
        independent = {
            "basis": "tax_note_consolidated_profit_before_tax_less_income_tax",
            "explanation": "将基于合并利润表的利润总额采用适用税率计算的所得税调节为所得税费用",
            "profit_before_tax": pbt_row,
            "income_tax_expense": tax_row,
            "recomputed_net_profit_yuan": str(recomputed),
            "different_page_from_primary": tax_page_number != page_number,
        }
        comparison = "note54 net profit = note51 consolidated profit before tax - income-tax expense"
    else:
        summary_value, summary = _report_summary(
            pdf, profile=profile, field_label="经营活动产生的现金流量净额")
        if summary_value != primary_value:
            raise SpringNativeNoteError("operating-cashflow note and report-summary rows do not agree")
        independent = {
            "basis": "same_report_summary_on_different_page",
            "summary_row": summary,
            "different_page_from_primary": summary["page"] != page_number,
        }
        comparison = "note54 operating-cashflow amount = report-summary current cell"
    primary = {
        "basis": "native_consolidated_note_current_cell",
        "note_heading": note_heading,
        "source_row": primary_row,
    }
    return primary_value, {
        "field": field, "primary": primary, "independent": independent,
        "comparison": comparison,
    }


def extract_native_note_facts(
    pdf_bytes: bytes, *, instrument_id: str, company: str, period_end: date,
    announcement_id: str, document_url: str, required_fields: tuple[str, ...],
) -> CninfoS2CandidateFacts:
    """Extract only the reviewed native consolidated-note cells from an original PDF."""

    profile = _validate_binding(
        pdf_bytes, instrument_id=instrument_id, company=company, period_end=period_end,
        announcement_id=announcement_id, document_url=document_url,
        required_fields=required_fields,
    )
    try:
        check_report_cover(pdf_bytes, company=company, period_end=period_end,
                           instrument_id=instrument_id)
    except Exception as exc:  # fail closed on the existing normal cover identity gate
        raise SpringNativeNoteError(f"normal report-cover identity check failed: {exc}") from exc
    digest = "sha256:" + hashlib.sha256(pdf_bytes).hexdigest()
    candidates = []
    try:
        with _pdf_pages(pdf_bytes) as pdf:
            if len(pdf.pages) != profile.page_count:
                raise SpringNativeNoteError("source PDF page count differs from its reviewed profile")
            for field in sorted(required_fields):
                value, _ = _field_proof(pdf_bytes, pdf, profile=profile, field=field)
                if field == "revenue":
                    note_page = profile.revenue_page
                    page = pdf.pages[note_page - 1]
                    context = _page_context(
                        page, page_number=note_page, profile=profile,
                        note_heading=("38. 营业收入和营业成本" if period_end.year == 2025
                                     else "37. 营业收入和营业成本"),
                    )
                    main_value, main_row = _amount_row(
                        page, page_number=note_page, label="主营业务收入",
                        profile=profile, context=context,
                    )
                    other_value, other_row = _amount_row(
                        page, page_number=note_page, label="其他业务收入",
                        profile=profile, context=context,
                    )
                    total_row = _find_line(
                        page, page_number=note_page, label="合计",
                        current_cell=f"{main_value + other_value:,.0f}",
                        prior_cell=f"{Decimal(str(main_row['prior_value_yuan'])) + Decimal(str(other_row['prior_value_yuan'])):,.0f}",
                        profile=profile, allow_unlabelled=True,
                    )
                    source_row = str(total_row["raw_text"])
                    source_cells = ("合计", str(total_row["current_cell"]["text"]),
                                    str(total_row["prior_cell"]["text"]))
                    display_label = "合计（营业收入和营业成本）"
                else:
                    note_page = {"net_profit_attributable": 171,
                                 "net_profit_consolidated": 179,
                                 "operating_cashflow": 179}[field]
                    label = {
                        "net_profit_attributable": "加：本年归属于母公司股东的净利润",
                        "net_profit_consolidated": "净利润",
                        "operating_cashflow": "经营活动产生的现金流量净额",
                    }[field]
                    row_line = _find_line(pdf.pages[note_page - 1],
                                          page_number=note_page, label=label,
                                          profile=profile)
                    source_row = str(row_line["raw_text"])
                    source_cells = (
                        label, str(row_line["current_cell"]["text"]),
                        str(row_line["prior_cell"]["text"]),
                    )
                    display_label = label
                candidates.append(PdfCandidateFact(
                    field=field, value_yuan=value, period_end=period_end,
                    report_period_text=f"{period_end.year}年度", statement=(
                        "cashflow" if field == "operating_cashflow" else "profit"),
                    pdf_page=note_page, column_header=f"{period_end.year}年度",
                    amount_unit="元", currency="CNY", source_row=source_row,
                    source_cells=source_cells, source_current_cell_index=1,
                    source_prior_cell_index=2, pdf_sha256=digest,
                    display_row_label=display_label,
                ))
    except SpringNativeNoteError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SpringNativeNoteError(f"native note extraction failed: {type(exc).__name__}: {exc}") from exc
    return CninfoS2CandidateFacts(
        instrument_id=instrument_id, period_end=period_end,
        version_label=VERSION_LABEL, document_url=document_url,
        pdf_sha256=digest, candidates=tuple(candidates),
    )


def verify_native_note_evidence(
    pdf_bytes: bytes, *, candidate: CninfoS2CandidateFacts,
    expected_fields: tuple[str, ...],
) -> MachineVerification:
    """Re-derive every selected row and independent check from the supplied PDF bytes."""

    profile = _profile_for_bytes(pdf_bytes)
    if profile is None or candidate.version_label != VERSION_LABEL:
        raise SpringNativeNoteError("candidate is not bound to this Spring native-note protocol")
    if candidate.instrument_id != "601021" or candidate.period_end != profile.period_end:
        raise SpringNativeNoteError("candidate source code or period differs from the exact report")
    fresh = extract_native_note_facts(
        pdf_bytes, instrument_id="601021", company=_COMPANY,
        period_end=profile.period_end, announcement_id=profile.announcement_id,
        document_url=profile.url, required_fields=tuple(sorted(expected_fields)),
    )
    if candidate != fresh:
        raise SpringNativeNoteError("candidate fields or source cells differ from fresh native extraction")
    fields = []
    sections = []
    with _pdf_pages(pdf_bytes) as pdf:
        for field in sorted(expected_fields):
            value, proof = _field_proof(pdf_bytes, pdf, profile=profile, field=field)
            check = IndependentCheck(
                path="native_full_report_note_crosscheck", holds=True,
                detail=proof["comparison"], evidence={
                    "method": VERIFIER_VERSION,
                    "instrument_id": "601021",
                    "company": _COMPANY,
                    "period_end": profile.period_end.isoformat(),
                    "announcement_id": profile.announcement_id,
                    "document_url": profile.url,
                    "content_hash": profile.digest,
                    "source_identity": {
                        "issuer": _COMPANY,
                        "official_report_title": f"{profile.period_end.year}年年度报告",
                        "pdf_page_count": profile.page_count,
                    },
                    "main_statement_claim": "not_used; primary values come from native consolidated-note text",
                    **proof,
                },
            )
            fields.append(FieldVerification(field=field, value_yuan=value, checks=(check,)))
            for page_number in sorted({
                int(proof["primary"].get("note_page", proof["primary"].get("source_row", {}).get("page", 0))),
                int(proof["independent"].get("summary_row", {}).get("page",
                    proof["independent"].get("report_summary_row", {}).get("page",
                    proof["independent"].get("direct_profit_numerator", {}).get("page",
                    proof["independent"].get("profit_before_tax", {}).get("page", 0))))),
            } - {0}):
                page = pdf.pages[page_number - 1]
                if page_number == profile.report_summary_page:
                    sections.append({
                        "role": "report_summary", "page": page_number,
                        "issuer": _COMPANY, "period_end": profile.period_end.isoformat(),
                        "amount_unit": "CNY yuan", "current_column": f"{profile.period_end.year}年",
                        "prior_column": f"{profile.period_end.year - 1}年",
                        "native_text_char_count": len(page.extract_text() or ""),
                    })
                else:
                    note_context = _page_context(page, page_number=page_number, profile=profile)
                    sections.append({**note_context,
                                     "native_text_char_count": len(page.extract_text() or "")})
    return MachineVerification(
        instrument_id="601021", period_end=profile.period_end,
        announcement_id=profile.announcement_id, pdf_sha256=profile.digest,
        fields=tuple(fields), sections=tuple(sections), identities=(),
    )

