"""Source-bound FY2025 native-note candidate for Tianshan Aluminium.

Only the exact reviewed CNINFO audit attachment is supported.  The PDF itself
does not print the A-share code; the caller supplies that identity from the
official index/audit association.  This module creates evidence candidates only.
"""
from __future__ import annotations

import hashlib
import io
import re
from datetime import date
from decimal import Decimal

from .cninfo_financial_pdf import CninfoS2CandidateFacts, PdfCandidateFact
from .s2_machine_verification import (
    FieldVerification, IndependentCheck, MachineVerification, MachineVerificationError,
)

VERSION_LABEL = "audited_consolidated_notes_v1"
VERIFIER_VERSION = "tianshan-audit-native-notes-v1"
_COMPANY = "天山铝业集团股份有限公司"
_CODE = "002532"
_ANNOUNCEMENT = "1225041401"
_URL = "https://static.cninfo.com.cn/finalpage/2026-03-30/1225041401.PDF"
_PERIOD = date(2025, 12, 31)
_HASH = "03b73777aaa647123ae629aa3ae1307b098290f271c016fb6966d9ccf77e7082"
_PAGE_COUNT = 133
_FIELDS = frozenset({"revenue", "net_profit_attributable", "net_profit_consolidated", "operating_cashflow"})
_AMOUNT = re.compile(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{2})?\Z")
_FRAGMENT = re.compile(r"[-\d,.]+\Z")


class TianshanAuditNativeError(MachineVerificationError):
    """The supplied report does not satisfy the reviewed source contract."""


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).translate(str.maketrans("", "", "“”‘’\"'"))


def _open(pdf_bytes: bytes):
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover
        raise TianshanAuditNativeError("pdfplumber is required for native PDF extraction") from exc
    try:
        return pdfplumber.open(io.BytesIO(pdf_bytes))
    except Exception as exc:  # noqa: BLE001
        raise TianshanAuditNativeError(f"cannot open PDF: {type(exc).__name__}") from exc


def _supported(pdf_bytes: bytes) -> bool:
    return (isinstance(pdf_bytes, bytes) and pdf_bytes.startswith(b"%PDF-") and
            hashlib.sha256(pdf_bytes).hexdigest() == _HASH)


def is_supported_pdf(pdf_bytes: bytes) -> bool:
    return _supported(pdf_bytes)


def _bind(pdf_bytes: bytes, *, instrument_id: str, company: str, period_end: date,
          announcement_id: str, document_url: str, required_fields: tuple[str, ...]) -> None:
    if not _supported(pdf_bytes):
        raise TianshanAuditNativeError("PDF bytes do not match the reviewed official audit attachment")
    if (instrument_id != _CODE or company != _COMPANY or period_end != _PERIOD or
            announcement_id != _ANNOUNCEMENT or document_url != _URL):
        raise TianshanAuditNativeError("source identity, period, announcement, or URL differs")
    if not required_fields or len(required_fields) != len(set(required_fields)) or not set(required_fields) <= _FIELDS:
        raise TianshanAuditNativeError("required fields are empty, duplicated, or unsupported")


def _lines(page) -> list[dict[str, object]]:
    words = sorted(page.extract_words(x_tolerance=1, y_tolerance=2),
                   key=lambda w: (float(w["top"]), float(w["x0"])))
    groups: list[list[dict[str, object]]] = []
    for word in words:
        if not groups or abs(float(word["top"]) - float(groups[-1][0]["top"])) > 2.5:
            groups.append([word])
        else:
            groups[-1].append(word)
    result = []
    for group in groups:
        group.sort(key=lambda w: float(w["x0"]))
        result.append({
            "text": " ".join(str(w["text"]) for w in group), "words": group,
            "bbox": {"x0": round(min(float(w["x0"]) for w in group), 2),
                     "x1": round(max(float(w["x1"]) for w in group), 2),
                     "top": round(min(float(w["top"]) for w in group), 2),
                     "bottom": round(max(float(w["bottom"]) for w in group), 2)},
        })
    return result


def _validate_report(pdf) -> None:
    if len(pdf.pages) != _PAGE_COUNT:
        raise TianshanAuditNativeError("audit PDF page count differs from reviewed source")
    p1 = _compact(pdf.pages[0].extract_text() or "")
    if _COMPANY not in p1 or _compact("众环审字(2026)1100042号") not in p1:
        raise TianshanAuditNativeError("page 1 lacks exact issuer and auditor report number")
    p2 = _compact(pdf.pages[1].extract_text() or "")
    scope = (_COMPANY + "全体股东", "审计了", "2025年12月31日的合并及公司资产负债表",
             "2025年度的合并及公司利润表", "合并及公司现金流量表",
             "合并及公司股东权益变动表", "相关财务报表附注", "企业会计准则")
    if not all(_compact(item) in p2 for item in scope):
        raise TianshanAuditNativeError("page 2 does not establish full FY2025 audited scope")
    p19 = _compact(pdf.pages[18].extract_text() or "")
    if not all(_compact(item) in p19 for item in (
            _COMPANY, "2025年度财务报表附注", "金额单位为人民币元",
            "截至2025年12月31日", "2026年3月27日", "决议批准报出")):
        raise TianshanAuditNativeError("page 19 lacks issuer, period, unit, or board approval context")
    p55 = _compact(pdf.pages[54].extract_text() or "")
    if not all(_compact(item) in p55 for item in (
            "六、合并财务报表项目注释", "年初指2025年1月1日", "年末指2025年12月31日",
            "上年年末指2024年12月31日", "本年指2025年度", "上年指2024年度")):
        raise TianshanAuditNativeError("page 55 does not bind period vocabulary to the consolidated notes")
    p123 = _compact(pdf.pages[122].extract_text() or "")
    if _compact("十七、公司财务报表主要项目注释") not in p123:
        raise TianshanAuditNativeError("page 123 does not establish consolidated-note boundary")


def _context(pdf, page_number: int, heading: str, *, continuation: bool = False) -> dict[str, object]:
    page = pdf.pages[page_number - 1]
    text = _compact(page.extract_text() or "")
    if not 55 <= page_number < 123 or _COMPANY not in text or "2025年度财务报表附注" not in text:
        raise TianshanAuditNativeError(f"page {page_number} lacks native identity or is outside consolidated notes")
    if _compact(heading) not in text:
        # The exact unit is declared on p19 and applies to the report's notes;
        # continuation pages retain their local native note/table heading.
        if not (continuation and (("补充资料" in text and "本年金额" in text and "上年金额" in text) or
                                  ("本年发生额" in text and "上年发生额" in text))):
            raise TianshanAuditNativeError(f"page {page_number} lacks the expected note heading")
    return {"page": page_number, "role": "audited_consolidated_note", "issuer": _COMPANY,
            "period_end": _PERIOD.isoformat(), "note_heading": heading,
            "amount_unit": "人民币元", "currency": "CNY", "scale": "yuan",
            "current_prior_binding": "page 55 defines 本年=2025年度 and 上年=2024年度",
            "text_layer": "native searchable PDF text; no OCR"}


def _line(page, page_number: int, label: str, *, min_top: float = 0,
          max_top: float = 784, prefix: bool = True) -> dict[str, object]:
    target = _compact(label)
    matches = [line for line in _lines(page)
               if min_top <= float(line["bbox"]["top"]) <= max_top and
               (( _compact(str(line["text"])).startswith(target)) if prefix else
                (target in _compact(str(line["text"]))))]
    if len(matches) != 1:
        raise TianshanAuditNativeError(f"page {page_number} row {label!r} is not unique: {len(matches)}")
    return matches[0]


def _number_groups(words: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: list[list[dict[str, object]]] = []
    for word in sorted(words, key=lambda w: float(w["x0"])):
        raw = str(word["text"]).replace(" ", "")
        if not _FRAGMENT.fullmatch(raw):
            continue
        prev = "".join(str(w["text"]).replace(" ", "") for w in groups[-1]) if groups else ""
        if groups and float(word["x0"]) - float(groups[-1][-1]["x1"]) <= 2.5 and _FRAGMENT.fullmatch(prev + raw):
            groups[-1].append(word)
        else:
            groups.append([word])
    out = []
    for group in groups:
        text = "".join(str(w["text"]).replace(" ", "") for w in group)
        if _AMOUNT.fullmatch(text):
            out.append({"text": text, "value": str(Decimal(text.replace(",", ""))),
                        "bbox": {"x0": round(min(float(w["x0"]) for w in group), 2),
                                 "x1": round(max(float(w["x1"]) for w in group), 2),
                                 "top": round(min(float(w["top"]) for w in group), 2),
                                 "bottom": round(max(float(w["bottom"]) for w in group), 2)}})
    return out


def _cell(line: dict[str, object], left: float, right: float, label: str) -> dict[str, object]:
    words = [w for w in line["words"] if left <= (float(w["x0"]) + float(w["x1"])) / 2 < right]
    groups = _number_groups(words)
    if len(groups) > 1:
        raise TianshanAuditNativeError(f"row {label!r} has multiple numbers in one column")
    if groups:
        return {"status": "numeric", "text": groups[0]["text"],
                "value_yuan": groups[0]["value"], "bbox": groups[0]["bbox"]}
    return {"status": "literal_blank" if not words else "literal_non_numeric",
            "text": " ".join(str(w["text"]) for w in words), "value_yuan": None,
            "bbox": None if not words else {"x0": round(min(float(w["x0"]) for w in words), 2),
                                             "x1": round(max(float(w["x1"]) for w in words), 2),
                                             "top": round(min(float(w["top"]) for w in words), 2),
                                             "bottom": round(max(float(w["bottom"]) for w in words), 2)}}


def _v(cell: dict[str, object], label: str) -> Decimal:
    if cell["status"] != "numeric" or cell["value_yuan"] is None:
        raise TianshanAuditNativeError(f"required amount {label!r} is blank or nonnumeric")
    return Decimal(str(cell["value_yuan"]))


def _row(line: dict[str, object], page: int, label: str,
         columns: tuple[tuple[float, float], ...]) -> dict[str, object]:
    return {"label": label, "page": page, "raw_text": str(line["text"]),
            "line_bbox": line["bbox"], "cells": [_cell(line, a, b, label) for a, b in columns]}


def _headers(page, page_number: int, current: str, prior: str) -> dict[str, object]:
    lines = _lines(page)
    c = [line for line in lines if current in _compact(str(line["text"]))]
    p = [line for line in lines if prior in _compact(str(line["text"]))]
    if not c or not p:
        raise TianshanAuditNativeError(f"page {page_number} lacks current/prior column labels")
    return {"current_header": str(c[0]["text"]), "current_bbox": c[0]["bbox"],
            "prior_header": str(p[0]["text"]), "prior_bbox": p[0]["bbox"],
            "current_period": "2025年度", "prior_period": "2024年度"}


def _profit_attributable(pdf):
    pn, page = 93, pdf.pages[92]
    ctx = _context(pdf, pn, "38、 未分配利润")
    headers = _headers(page, pn, "本年", "上年")
    cols = ((300, 415), (415, 520))
    labels = {
        "opening": "调整后年初未分配利润", "profit": "加：本年归属于母公司股东的净利润",
        "reserve": "减：提取法定盈余公积", "voluntary": "提取任意盈余公积",
        "risk": "提取一般风险准备", "dividend": "应付普通股股利",
        "stock_dividend": "转作股本的普通股股利", "closing": "年末未分配利润",
    }
    rows = {key: _row(_line(page, pn, label), pn, label, cols) for key, label in labels.items()}
    adjustment = _row(_line(page, pn, "调整年初未分配利润合计数"), pn,
                      "调整年初未分配利润合计数（调增+，调减-）", cols)
    for key in ("voluntary", "risk", "stock_dividend"):
        if rows[key]["cells"][0]["status"] != "literal_blank":
            raise TianshanAuditNativeError(f"optional row {labels[key]!r} is no longer a native blank")
    opening = _v(rows["opening"]["cells"][0], labels["opening"])
    profit = _v(rows["profit"]["cells"][0], labels["profit"])
    reserve = _v(rows["reserve"]["cells"][0], labels["reserve"])
    dividend = _v(rows["dividend"]["cells"][0], labels["dividend"])
    closing = _v(rows["closing"]["cells"][0], labels["closing"])
    computed = opening + profit - reserve - dividend
    if computed != closing:
        raise TianshanAuditNativeError("printed retained-profit numeric movements do not bridge to closing balance")
    return profit, {
        "page_context": ctx, "column_headers": headers, "primary_row": rows["profit"],
        "independent_bridge": {"opening_row": rows["opening"], "reserve_row": rows["reserve"],
            "dividend_row": rows["dividend"], "closing_row": rows["closing"],
            "computed_closing_yuan": str(computed),
            "numeric_terms_yuan": [str(opening), str(profit), str(-reserve), str(-dividend)],
            "literal_blanks_not_coerced": {"opening_adjustment": adjustment,
                "voluntary_reserve": rows["voluntary"], "general_risk_reserve": rows["risk"],
                "stock_dividend": rows["stock_dividend"]}},
    }, {"retained_profit_bridge_holds": True}


def _revenue(pdf):
    pn, page = 94, pdf.pages[93]
    ctx = _context(pdf, pn, "39、 营业收入和营业成本", continuation=True)
    headers = _headers(page, pn, "本年发生额", "上年发生额")
    cols = ((140, 230), (230, 320), (320, 420), (420, 520))
    main = _row(_line(page, pn, "主营业务"), pn, "主营业务", cols)
    other = _row(_line(page, pn, "其他业务"), pn, "其他业务", cols)
    totals = [line for line in _lines(page) if _compact(str(line["text"])).startswith("合计")]
    # The primary total is the first subtotal before the product breakdown.
    total_line = next((line for line in totals if 160 < float(line["bbox"]["top"]) < 202), None)
    product_total_line = next((line for line in totals if 490 < float(line["bbox"]["top"]) < 525), None)
    other_total_line = next((line for line in totals if 635 < float(line["bbox"]["top"]) < 665), None)
    if not all((total_line, product_total_line, other_total_line)):
        raise TianshanAuditNativeError("revenue table primary or product subtotal is not located")
    total = _row(total_line, pn, "合计（营业收入和营业成本）", cols)
    product_total = _row(product_total_line, pn, "主营业务分产品合计", cols)
    other_total = _row(other_total_line, pn, "其他业务分产品合计", cols)
    main_value = _v(main["cells"][0], "主营业务收入")
    other_value = _v(other["cells"][0], "其他业务收入")
    total_value = _v(total["cells"][0], "营业收入合计")
    if main_value + other_value != total_value:
        raise TianshanAuditNativeError("main plus other revenue does not equal total")
    if _v(product_total["cells"][0], "主营业务分产品合计") != main_value:
        raise TianshanAuditNativeError("main product-table subtotal differs from main-business revenue")
    if _v(other_total["cells"][0], "其他业务分产品合计") != other_value:
        raise TianshanAuditNativeError("other product-table subtotal differs from other-business revenue")
    def components(after: float, before: float) -> list[dict[str, object]]:
        found = []
        for line in _lines(page):
            top = float(line["bbox"]["top"])
            if after < top < before:
                cell = _cell(line, *cols[0], "product revenue detail")
                if cell["status"] == "numeric":
                    found.append({"raw_text": str(line["text"]), "line_bbox": line["bbox"], "cell": cell})
        return found
    lines = _lines(page)
    product_heading = next(line for line in lines if "（2）主营业务分产品" in _compact(str(line["text"])))
    other_heading = next(line for line in lines if "（3）其他业务（分产品）" in _compact(str(line["text"])))
    main_parts = components(float(product_heading["bbox"]["top"]), float(product_total_line["bbox"]["top"]))
    other_parts = components(float(other_heading["bbox"]["top"]), float(other_total_line["bbox"]["top"]))
    main_sum = sum((Decimal(str(item["cell"]["value_yuan"])) for item in main_parts), Decimal(0))
    other_sum = sum((Decimal(str(item["cell"]["value_yuan"])) for item in other_parts), Decimal(0))
    if main_sum != main_value or other_sum != other_value:
        raise TianshanAuditNativeError("product-level revenue amounts do not sum to printed subtotals")
    return total_value, {
        "page_context": ctx, "column_headers": headers,
        "primary_rows": {"main_business": main, "other_business": other, "total": total},
        "independent_rows": {"main_product_subtotal": product_total,
            "other_product_subtotal": other_total, "main_product_amounts": main_parts,
            "other_product_amounts": other_parts, "product_sums_yuan":
                {"main": str(main_sum), "other": str(other_sum)}},
    }, {"main_plus_other_equals_total": True, "product_breakdowns_equal_subtotals": True}


def _net_profit(pdf):
    p101n, p99n, p100n = 101, 99, 100
    p101, p99, p100 = pdf.pages[100], pdf.pages[98], pdf.pages[99]
    ctx101 = _context(pdf, p101n, "54、 现金流量表补充资料")
    ctx99 = _context(pdf, p99n, "51、 所得税费用")
    ctx100 = _context(pdf, p100n, "所得税费用", continuation=True)
    heads101 = _headers(p101, p101n, "本年金额", "上年金额")
    heads99 = _headers(p99, p99n, "本年发生额", "上年发生额")
    primary = _row(_line(p101, p101n, "净利润"), p101n, "净利润", ((300, 410), (410, 520)))
    net = _v(primary["cells"][0], "合并净利润")
    tax_cols = ((300, 410), (410, 520))
    current_tax = _row(_line(p99, p99n, "当期所得税费用"), p99n, "当期所得税费用", tax_cols)
    deferred_tax = _row(_line(p99, p99n, "递延所得税费用"), p99n, "递延所得税费用", tax_cols)
    tax_total_line = _line(p99, p99n, "合 计", min_top=630, max_top=655)
    tax_total = _row(tax_total_line, p99n, "所得税费用合计", tax_cols)
    tax = _v(tax_total["cells"][0], "所得税费用合计")
    tax_parts = _v(current_tax["cells"][0], "当期所得税费用") + _v(deferred_tax["cells"][0], "递延所得税费用")
    pbt = _row(_line(p99, p99n, "利润总额"), p99n, "利润总额", ((400, 520),))
    pbt_value = _v(pbt["cells"][0], "利润总额")
    repeated = _row(_line(p100, p100n, "所得税费用"), p100n,
                    "所得税费用（调整过程末行）", ((400, 520),))
    repeated_value = _v(repeated["cells"][0], "所得税费用调整过程末行")
    if tax_parts != tax or repeated_value != tax or pbt_value - tax != net:
        raise TianshanAuditNativeError("profit-before-tax/tax evidence does not reproduce net profit")
    return net, {"primary_page_context": ctx101, "primary_column_headers": heads101,
        "primary_row": primary, "independent": {"tax_page_context": ctx99,
            "tax_column_headers": heads99, "current_tax_row": current_tax,
            "deferred_tax_row": deferred_tax, "tax_total_row": tax_total,
            "tax_parts_sum_yuan": str(tax_parts), "profit_before_tax_row": pbt,
            "profit_before_tax_header": "p99 本年发生额", "tax_reconciliation_context": ctx100,
            "tax_reconciliation_row": repeated,
            "recomputed_net_profit_yuan": str(pbt_value-tax)}}, {
                "tax_components_equal_tax_total": True,
                "profit_before_tax_less_tax_equals_net_profit": True,
                "tax_total_repeats_in_reconciliation": True}


_CFO_LABELS = (
    ("net_profit", "净利润", True), ("asset_impairment", "加：资产减值准备", True),
    ("credit_impairment", "信用减值损失", True),
    ("fixed_depreciation", "固定资产折旧、油气资产折耗、生产", True),
    ("right_use_depreciation", "使用权资产折旧", True), ("intangibles", "无形资产摊销", True),
    ("long_prepaid", "长期待摊费用摊销", False),
    ("disposal", "处置固定资产、无形资产和其他长期", True),
    ("scrap", "固定资产报废损失（收益以“－”号填", True),
    ("fair_value", "公允价值变动损失（收益以“－”号填", False),
    ("finance", "财务费用（收益以“－”号填列）", True),
    ("investment", "投资损失（收益以“－”号填列）", False),
    ("deferred_asset", "递延所得税资产减少（增加以“－”号", True),
    ("deferred_liability", "递延所得税负债增加（减少以“－”号", True),
    ("inventory", "存货的减少（增加以“－”号填列）", True),
    ("receivables", "经营性应收项目的减少（增加以“－”", True),
    ("payables", "经营性应付项目的增加（减少以“－”", True),
    ("other", "其他", True),
)


def _label_lines(page, page_number: int, patterns) -> list[dict[str, object]]:
    lines = _lines(page)
    result = []
    for pattern in patterns:
        matches = [line for line in lines if _compact(str(line["text"])).startswith(_compact(pattern))]
        if len(matches) != 1:
            raise TianshanAuditNativeError(f"page {page_number} CFO row label {pattern!r} is not unique")
        result.append(matches[0])
    if any(float(a["bbox"]["top"]) >= float(b["bbox"]["top"]) for a,b in zip(result,result[1:])):
        raise TianshanAuditNativeError("CFO row labels are out of printed order")
    return result


def _numeric_current(page, lower: float, upper: float):
    result=[]
    for line in _lines(page):
        top=float(line["bbox"]["top"])
        if lower <= top <= upper:
            cell=_cell(line,300,410,"CFO current-year cell")
            if cell["status"]=="numeric": result.append((top,line,cell))
    return result


def _operating_cashflow(pdf):
    n1,n2=101,102
    p1,p2=pdf.pages[100],pdf.pages[101]
    c1=_context(pdf,n1,"54、 现金流量表补充资料")
    c2=_context(pdf,n2,"现金流量表补充资料",continuation=True)
    h1=_headers(p1,n1,"本年金额","上年金额"); h2=_headers(p2,n2,"本年金额","上年金额")
    labels1=_label_lines(p1,n1,[x[1] for x in _CFO_LABELS[:3]])
    labels2=_label_lines(p2,n2,[x[1] for x in _CFO_LABELS[3:]])
    rows=_numeric_current(p1,float(labels1[0]["bbox"]["top"]),780)
    rows += _numeric_current(p2,82,650) # excludes the separately reported total row
    events=[]
    events += [(x[0],x[1],float(line["bbox"]["top"]),n1) for x,line in zip(_CFO_LABELS[:3],labels1)]
    events += [(x[0],x[1],float(line["bbox"]["top"]),n2) for x,line in zip(_CFO_LABELS[3:],labels2)]
    numeric_expected=[key for key,_,has_number in _CFO_LABELS if has_number]
    if len(rows)!=len(numeric_expected):
        raise TianshanAuditNativeError("CFO schedule does not contain one current amount for each populated row")
    mapped=[]
    for top,line,cell in rows:
        page_no=n1 if top>=float(labels1[0]["bbox"]["top"]) else n2
        prior=[e for e in events if e[3]==page_no and e[2]<=top]
        if not prior: raise TianshanAuditNativeError("CFO amount has no preceding native label")
        key,label,label_top,_=prior[-1]
        mapped.append({"key":key,"label":label,"page":page_no,"row_label_top":label_top,
                       "raw_text":str(line["text"]),"line_bbox":line["bbox"],"cell":cell})
    if [item["key"] for item in mapped]!=numeric_expected:
        raise TianshanAuditNativeError("CFO numeric cells do not align with native row-label order")
    total_line=_line(p2,n2,"经营活动产生的现金流量净额")
    total=_row(total_line,n2,"经营活动产生的现金流量净额",((300,410),(410,520)))
    reported=_v(total["cells"][0],"经营活动产生的现金流量净额")
    computed=sum((Decimal(str(item["cell"]["value_yuan"])) for item in mapped),Decimal(0))
    if computed!=reported: raise TianshanAuditNativeError("printed CFO numeric adjustments do not sum to total")
    proof={"page_contexts":[c1,c2],"column_headers":[h1,h2],"adjustment_rows":mapped,
           "row_count":len(mapped),"reported_total_row":total,"computed_total_yuan":str(computed),
           "literal_blank_rows_not_added_as_zero":[label for _,label,has in _CFO_LABELS if not has],
           "blank_cells_added_as_zero":False}
    return reported,proof,{"all_printed_numeric_rows_sum_to_total":True}


def _field(pdf, field: str):
    if field=="revenue":
        value,proof,checks=_revenue(pdf); page=94; statement="revenue_note"; label="合计（营业收入和营业成本）"
        row=proof["primary_rows"]["total"]; cells=row["cells"]
        source_cells=(label,str(cells[0]["text"]),str(cells[1]["text"]),str(cells[2]["text"]),str(cells[3]["text"]))
        current_index,prior_index,header=1,3,"本年发生额/收入"
    elif field=="net_profit_attributable":
        value,proof,checks=_profit_attributable(pdf); page=93; statement="retained_profit_note"
        label="加：本年归属于母公司股东的净利润"; row=proof["primary_row"]; cells=row["cells"]
        source_cells=(label,str(cells[0]["text"]),str(cells[1]["text"]))
        current_index,prior_index,header=1,2,"本年"
    elif field=="net_profit_consolidated":
        value,proof,checks=_net_profit(pdf); page=101; statement="cashflow_note"; label="净利润"
        row=proof["primary_row"]; cells=row["cells"]
        source_cells=(label,str(cells[0]["text"]),str(cells[1]["text"]))
        current_index,prior_index,header=1,2,"本年金额"
    elif field=="operating_cashflow":
        value,proof,checks=_operating_cashflow(pdf); page=102; statement="cashflow_note"
        label="经营活动产生的现金流量净额"; row=proof["reported_total_row"]; cells=row["cells"]
        source_cells=(label,str(cells[0]["text"]),str(cells[1]["text"]))
        current_index,prior_index,header=1,2,"本年金额"
    else: raise TianshanAuditNativeError(f"unsupported field: {field}")
    candidate=PdfCandidateFact(field=field,value_yuan=value,period_end=_PERIOD,
        report_period_text="2025年度（page 55: 本年）",statement=statement,pdf_page=page,
        column_header=header,amount_unit="元",currency="CNY",source_row=str(row["raw_text"]),
        source_cells=source_cells,source_current_cell_index=current_index,
        source_prior_cell_index=prior_index,pdf_sha256="sha256:"+_HASH,display_row_label=label)
    return value,proof,checks,candidate


def extract_native_note_facts(pdf_bytes: bytes, *, instrument_id: str, company: str,
        period_end: date, announcement_id: str, document_url: str,
        required_fields: tuple[str,...]) -> CninfoS2CandidateFacts:
    """Create non-admitted candidate cells from the exact audited source."""
    _bind(pdf_bytes,instrument_id=instrument_id,company=company,period_end=period_end,
          announcement_id=announcement_id,document_url=document_url,required_fields=required_fields)
    try:
        with _open(pdf_bytes) as pdf:
            _validate_report(pdf)
            candidates=tuple(_field(pdf,field)[3] for field in sorted(required_fields))
    except TianshanAuditNativeError: raise
    except Exception as exc:  # noqa: BLE001
        raise TianshanAuditNativeError(f"native-note extraction failed: {type(exc).__name__}: {exc}") from exc
    return CninfoS2CandidateFacts(instrument_id=_CODE,period_end=_PERIOD,
        version_label=VERSION_LABEL,document_url=_URL,pdf_sha256="sha256:"+_HASH,candidates=candidates)


def verify_native_note_evidence(pdf_bytes: bytes, *, candidate: CninfoS2CandidateFacts,
                                expected_fields: tuple[str,...]) -> MachineVerification:
    """Freshly re-extract and cross-check every selected note amount."""
    if not _supported(pdf_bytes) or candidate.version_label!=VERSION_LABEL:
        raise TianshanAuditNativeError("candidate is not bound to this exact audited-note protocol")
    if (candidate.instrument_id!=_CODE or candidate.period_end!=_PERIOD or
            candidate.document_url!=_URL or candidate.pdf_sha256!="sha256:"+_HASH):
        raise TianshanAuditNativeError("candidate identity, period, URL, or hash differs")
    fields=tuple(sorted(expected_fields))
    fresh=extract_native_note_facts(pdf_bytes,instrument_id=_CODE,company=_COMPANY,
        period_end=_PERIOD,announcement_id=_ANNOUNCEMENT,document_url=_URL,required_fields=fields)
    if candidate!=fresh: raise TianshanAuditNativeError("candidate cells differ from fresh source extraction")
    field_results=[]; sections=[]
    with _open(pdf_bytes) as pdf:
        for field in fields:
            value,proof,checks,_=_field(pdf,field)
            ic=IndependentCheck(path="native_full_report_note_crosscheck",
                holds=bool(checks) and all(checks.values()),
                detail="; ".join(f"{key}={value}" for key,value in checks.items()),
                evidence={"method":VERIFIER_VERSION,"instrument_id":_CODE,
                    "instrument_id_source":"official CNINFO index/audit association supplied by caller; code is not printed in this PDF",
                    "company":_COMPANY,"period_end":_PERIOD.isoformat(),"announcement_id":_ANNOUNCEMENT,
                    "document_url":_URL,"content_hash":"sha256:"+_HASH,
                    "audit_report_number":"众环审字(2026)1100042号",
                    "audited_scope":"FY2025 consolidated/company statements and notes under CAS",
                    "identity_pages":{"p1":"issuer and report number","p2":"audit scope/opinion",
                        "p19":"issuer, CNY yuan, period, board approval 2026-03-27",
                        "p55":"consolidated notes and 本年/上年 mapping","p123":"company-note boundary"},
                    "main_statement_claim":"not used; primary candidate is from native audited consolidated notes",
                    **proof})
            field_results.append(FieldVerification(field=field,value_yuan=value,checks=(ic,)))
            page_numbers={93 if field=="net_profit_attributable" else 0,
                94 if field=="revenue" else 0,99 if field=="net_profit_consolidated" else 0,
                100 if field=="net_profit_consolidated" else 0,
                101 if field in {"net_profit_consolidated","operating_cashflow"} else 0,
                102 if field=="operating_cashflow" else 0}
            for page_number in sorted(page_numbers-{0}):
                if page_number not in {s["page"] for s in sections}:
                    sections.append({"role":"audited_consolidated_note","page":page_number,
                        "issuer":_COMPANY,"period_end":_PERIOD.isoformat(),"amount_unit":"人民币元",
                        "current_column_binding":"2025年度 as defined on p55",
                        "text_layer":"native searchable PDF text; no OCR",
                        "native_text_char_count":len(pdf.pages[page_number-1].extract_text() or "")})
    return MachineVerification(instrument_id=_CODE,period_end=_PERIOD,
        announcement_id=_ANNOUNCEMENT,pdf_sha256="sha256:"+_HASH,
        fields=tuple(field_results),sections=tuple(sections),identities=())
