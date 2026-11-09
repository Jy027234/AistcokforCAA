"""Independent ruled-grid cell reading. No main line-parser values or indices."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from collections import OrderedDict
from threading import RLock
from .cninfo_report_identity import IDENTITY_READER_VERSION, IDENTITY_PAGE_LIMIT
import hashlib, io, re

GRID_EXTRACTOR_VERSION = "cninfo-ruled-grid-v1"
_TITLE_LOCATOR_VERSION = "native-recall-pypdf-canonical-v1"
_GRID_TABLE_READER_VERSION = "ruled-grid-local-shading-continuation-v3-black-fill-v4"
_NATIVE_TEXT_LOCK = RLock()
_SCALE = {"元": Decimal(1), "千元": Decimal(1000), "万元": Decimal(10000), "百万元": Decimal(1000000)}
_UNIT = re.compile(r"(?:单位\s*(?:为)?\s*[:：]?\s*|(?=人民币\s*(?:百万元|千元|万元|元)))(?:人民币)?\s*(百万元|千元|万元|元)(?:(?![\u4e00-\u9fff])|(?=币种\s*[:：]))")
_TITLE = re.compile(r"^(?:(?:\d{4}年(?:\d{1,2}月\d{1,2}日|上半年|半年度|年度|度)?)|(?:\d+[.、]))?(合并|母公司|公司)(?:及公司|及母公司)?(资产负债表|利润表|现金流量表)(?:[(（]续[)）]|[-—]续)?$")
_FIELD_STATEMENT = {"revenue": "利润表", "net_profit_attributable": "利润表",
    "net_profit_consolidated": "利润表", "parent_equity": "资产负债表", "operating_cashflow": "现金流量表"}


def _compact(text): return re.sub(r"\s+", "", str(text or "")).replace("（", "(").replace("）", ")")


def _field_label(field, label):
    label = _compact(label)
    label = re.sub(r"^(?:[一二三四五六七八九十]+[、.]|\d+[.、]|\([一二三四五六七八九十]+\))", "", label)
    patterns = {
        "revenue": r"^(?:其中[:：])?营业收入$",
        "net_profit_consolidated": r"^净利润(?:\(净亏损以.*号填列\))?$",
        "net_profit_attributable": r"^归属于母公司(?:股东|所有者)的?净利润(?:\(净亏损以.*号填列\))?$",
        "parent_equity": r"^归属(?:于)?母公司(?:所有者|股东)权益(?:\(或股东权益\))?合计$",
        "operating_cashflow": r"^经营活动(?:(?:产生|使用)(?:/\((?:使用|产生)\))?|\((?:使用|产生)\)/(?:产生|使用))的现金流量净额$",
    }
    return re.fullmatch(patterns[field], label) is not None


def _amount(cell):
    text = _compact(cell).replace("−", "-")
    if not text or text in ("-", "—", "–", "/"): return None
    if not re.fullmatch(r"-?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d{1,2})?|\((?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d{1,2})?\)", text): return None
    if text.startswith("(") and text.endswith(")"): text = "-" + text[1:-1]
    try: value = Decimal(text.replace(",", ""))
    except InvalidOperation: return None
    return value if value.is_finite() else None


def _dated_label(text):
    text = _compact(text)
    half_year = re.fullmatch(r"(20\d{2})年0?1[-－—–]0?6月", text)
    if half_year: return date(int(half_year[1]), 6, 30)
    match = re.fullmatch(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", text)
    if match:
        try: return date(*map(int, match.groups()))
        except ValueError: return None
    match = re.fullmatch(r"(20\d{2})(?:年半年度|年上半年|年度|年年度)", text)
    if match: return date(int(match[1]), 6 if ("半" in text) else 12, 30 if ("半" in text) else 31)
    match = re.fullmatch(r"截至(20\d{2})年6月30日止6个月期间",text)
    if match: return date(int(match[1]),6,30)
    return None


@dataclass(frozen=True, slots=True)
class GridStatementCell:
    field: str
    statement: str
    period_end: date
    page: int
    heading_page: int
    heading: str
    amount_unit: str
    unit_declaration: str
    current_header: str
    prior_header: str
    row_label: str
    current_cell: str
    amount_yuan: Decimal
    table_bbox: tuple
    cell_bbox: tuple
    header_cells: tuple
    current_column: int
    issuer_evidence: tuple
    report_period_evidence: tuple
    source_identity_binding: tuple = ()

    def as_evidence(self):
        evidence = {"extractor_version": GRID_EXTRACTOR_VERSION, "field": self.field,
            "role": "合并", "statement": self.statement, "period_end": self.period_end.isoformat(),
            "page": self.page, "heading_page": self.heading_page, "heading": self.heading,
            "amount_unit": self.amount_unit, "currency": "CNY", "unit_declaration": self.unit_declaration,
            "currency_basis": ("EXPLICIT_RMB_UNIT_DECLARATION" if "人民币" in self.unit_declaration else
                               "CNINFO_DOMESTIC_YUAN_TEMPLATE_NO_FOREIGN_DECLARATION"),
            "current_header": self.current_header, "prior_header": self.prior_header,
            "row_label": self.row_label, "current_cell": self.current_cell,
            "amount_yuan": str(self.amount_yuan), "table_bbox": list(self.table_bbox),
            "cell_bbox": list(self.cell_bbox), "header_cells": list(self.header_cells),
            "current_column": self.current_column, "issuer_evidence": [list(row) for row in self.issuer_evidence],
            "report_period_evidence": [list(row) for row in self.report_period_evidence]}
        if self.source_identity_binding:
            evidence["source_identity_binding"] = dict(self.source_identity_binding)
        return evidence


def _corrected_601069_h125_frontmatter(pdf, binding, period_end):
    """Resolve the bound H125 section without treating the attachment as a full report."""
    expected_company = _compact(binding["company"])
    pages = pdf.pages[:12]
    if len(pages) < 11:
        return None
    page_lines = [(index + 1, (page.extract_text() or "").splitlines())
                  for index, page in enumerate(pages)]

    # The cover prints the issuer's full legal name, but no security code.
    # The code/announcement binding comes from the separately audited index
    # contract; reject any conflicting legal-name text on the first pages.
    cover_issuer = [(1, line) for line in page_lines[0][1]
                    if _compact(line) == expected_company]
    cover_title = _compact(binding["documentTitle"]).replace(expected_company, "", 1)
    if (len(cover_issuer) != 1 or
            not any(_compact(line) == cover_title for line in page_lines[0][1])):
        return None
    legal_name_pattern = re.compile(
        r"[\u4e00-\u9fffA-Za-z（）()·]+(?:股份有限公司|股份公司|有限公司)")
    for _page, lines in page_lines[:3]:
        names = {_compact(match.group(0)) for line in lines
                 for match in legal_name_pattern.finditer(line)}
        if names - {expected_company}:
            return None

    section_marker = _compact("二、《2025 年半年度报告》财务报表及相关附注更正内容")
    section_rows = [(page_number, index, line)
                    for page_number, lines in page_lines
                    for index, line in enumerate(lines)
                    if _compact(line) == section_marker]
    if len(section_rows) != 1 or section_rows[0][0] != 8:
        return None
    section_page, section_index, section_line = section_rows[0]
    section_lines = [(page_number, index, line)
                     for page_number, lines in page_lines[section_page - 1:12]
                     for index, line in enumerate(lines)]
    if not any(page == section_page and index > section_index and
               _compact(line) == _compact("（一）2025 年半年度更正后的财务报表")
               for page, index, line in section_lines):
        return None

    balance_heads = [(page, index, line) for page, index, line in section_lines
                     if _compact(line) == "合并资产负债表"]
    if len(balance_heads) != 1:
        return None
    balance_page, balance_index, _ = balance_heads[0]
    current_date = f"{period_end.year}年{period_end.month}月{period_end.day}日"
    prior_date = "2024年6月30日"
    balance_headers = [
        (page, line) for page, index, line in section_lines
        if page >= balance_page and (page, index) > (balance_page, balance_index)
        and current_date in _compact(line) and prior_date in _compact(line)
    ]
    if len(balance_headers) != 1:
        return None

    profit_heads = [(page, index, line) for page, index, line in section_lines
                    if _compact(line) == "合并利润表"]
    if len(profit_heads) != 1:
        return None
    profit_page, profit_index, _ = profit_heads[0]
    durations = [(page, line) for page, index, line in section_lines
                 if (page, index) > (profit_page, profit_index)
                 and re.fullmatch(r"2025年1[-－—–]6月", _compact(line))]
    profit_headers = [
        (page, line) for page, _index, line in section_lines
        if page > profit_page and "2025年半年度" in _compact(line)
        and "2024年半年度" in _compact(line)
    ]
    if len(durations) != 1 or len(profit_headers) != 1:
        return None

    issuer = tuple(cover_issuer)
    periods = (
        (section_page, section_line, period_end),
        (balance_headers[0][0], balance_headers[0][1], period_end),
        (durations[0][0], durations[0][1], period_end),
        (profit_headers[0][0], profit_headers[0][1], period_end),
    )
    return issuer, periods


def _frontmatter(pdf, instrument_id, period_end, *, pdf_bytes=None):
    issuer = []
    periods = []
    source_identity_binding = ()
    for index, page in enumerate(pdf.pages[:12]):
        for line in (page.extract_text() or "").splitlines():
            compact = _compact(line)
            if re.search(r"(?:公司代码|股票代码|证券代码)", compact) and re.search(rf"(?<!\d){instrument_id}(?!\d)", compact):
                issuer.append((index + 1, line))
            if index < 3:
                match = re.fullmatch(r"(?:[\u4e00-\u9fffA-Za-z()]+)?(20\d{2})(年半年度|年年度|年度)报告(?:全文)?", compact)
                if match:
                    declared = date(int(match[1]), 6 if "半" in match[2] else 12,
                                    30 if "半" in match[2] else 31)
                    periods.append((index + 1, line, declared))
    if not issuer:
        from .cninfo_report_identity import extra_a_share_code_evidence, profile_security_code_evidence
        pages=[(i+1,(p.extract_text() or "").splitlines()) for i,p in enumerate(pdf.pages[:IDENTITY_PAGE_LIMIT])]
        issuer.extend(extra_a_share_code_evidence(pages,instrument_id) or
                      profile_security_code_evidence(pages,instrument_id))
    if not periods:
        from .cninfo_report_identity import compound_report_identity, explicit_issuer_company_names
        pages=[(i+1,(p.extract_text() or "").splitlines()) for i,p in enumerate(pdf.pages[:IDENTITY_PAGE_LIMIT])]
        names={r[2] for r in explicit_issuer_company_names(pages)}
        if len(names)==1:
            chain=compound_report_identity(pages,instrument_id,next(iter(names)),period_end)
            if chain:
                issuer=list(chain[0])
                periods=[(p,line,period_end) for p,line in chain[1]]
    # A report-local period mismatch is a hard rejection; the native source
    # candidate below may only fill absent observations, never override them.
    if periods and {row[2] for row in periods} != {period_end}: return None
    if not issuer or not periods:
        if pdf_bytes is None:
            return None
        from .native_statement_identity_candidate import (
            NativeIdentityRejected,
            bind_601069_corrected_h125_source_identity,
            native_statement_identity_from_pdf_bytes,
            supports_600988_h1_source_candidate,
            supports_600988_2024_revenue_source_candidate,
        )
        bound_601069 = bind_601069_corrected_h125_source_identity(
            instrument_id, period_end, pdf_bytes=pdf_bytes,
            expected_company="西部黄金股份有限公司",
            announcement_id="1225082560",
            document_title="西部黄金股份有限公司关于前期会计差错更正后的财务报表及附注",
            document_url=(
                "https://static.cninfo.com.cn/finalpage/2026-04-08/1225082560.PDF"
            ),
            source_role="CORRECTED_FINANCIAL_STATEMENTS",
            source_section_period_end=date(2025, 6, 30),
        )
        if bound_601069 is not None:
            corrected = _corrected_601069_h125_frontmatter(
                pdf, bound_601069, period_end)
            if corrected is None:
                return None
            issuer, periods = list(corrected[0]), list(corrected[1])
            source_identity_binding = tuple(sorted(bound_601069.items()))
        elif not (
            supports_600988_h1_source_candidate(
                instrument_id, period_end, pdf_bytes=pdf_bytes,
            )
            or supports_600988_2024_revenue_source_candidate(
                instrument_id, period_end, pdf_bytes=pdf_bytes,
            )
        ):
            return None
        else:
            try:
                native = native_statement_identity_from_pdf_bytes(
                    pdf_bytes, instrument_id=instrument_id, period_end=period_end,
                )
            except NativeIdentityRejected:
                return None
            issuer = [
                (row.page, line.text)
                for row in (
                    native.company_name_row,
                    native.a_share_code_row,
                    native.statement_issuer_row,
                )
                for line in (row.source_lines or (row.line,))
            ]
            periods = [
                (row.page, line.text, period_end)
                for row in (native.statement_period_row, native.statement_current_columns_row)
                for line in (row.source_lines or (row.line,))
            ]
    if not issuer or not periods or {row[2] for row in periods} != {period_end}: return None
    return (tuple(issuer), tuple((p, text) for p, text, _ in periods),
            source_identity_binding)


def _header(table, period_end, statement, preamble, report_period):
    rows = table.extract()
    if not rows: return None
    for row_index, row in enumerate(rows[:4]):
        labels = [re.sub(r"\((?:经|已)?重述\)$", "", _compact(x)) for x in row]
        if not labels or labels[0] not in ("项目", "资产", "负债及股东权益"): continue
        scope_labels = [re.fullmatch(r"(.+?)(合并|公司|母公司)[*＊]?", label) for label in labels]
        amount_scopes = [(i,match) for i,match in enumerate(scope_labels) if match]
        if amount_scopes:
            # Four explicitly scoped header cells, independently found from
            # their physical grid. Neither their indices nor dates come from
            # the primary parser. Do not silently pick the leftmost numbers.
            if [match[2] for _,match in amount_scopes] not in (
                    ["合并","合并","公司","公司"],
                    ["合并","合并","母公司","母公司"]): continue
            dates = [(i,_dated_label(match[1]),match[2]) for i,match in amount_scopes]
            if any(day is None for _,day,_ in dates): continue
            if [day for _,day,_ in dates[:2]] != [day for _,day,_ in dates[2:]]: continue
            current = [i for i,day,scope in dates if scope == "合并" and day == period_end]
            prior = [i for i,day,scope in dates if scope == "合并" and day != period_end]
        else:
            current = [i for i, label in enumerate(labels) if _dated_label(label) == period_end]
            prior = [i for i, label in enumerate(labels) if _dated_label(label) is not None and _dated_label(label) != period_end]
        if not current:
            now_tokens = ("期末余额", "年末余额") if statement == "资产负债表" else ("本期发生额", "本年发生额", "本期数")
            previous_tokens = ("期初余额", "年初余额") if statement == "资产负债表" else ("上期发生额", "上年发生额", "上年同期发生额", "上期数")
            current = [i for i, label in enumerate(labels) if label in now_tokens]
            prior = [i for i, label in enumerate(labels) if label in previous_tokens]
            if report_period != period_end: return None
        if len(current) != 1 or len(prior) != 1 or current[0] == prior[0]: return None
        cells = table.rows[row_index].cells
        if len(cells) != len(labels) or cells[current[0]] is None or cells[prior[0]] is None: return None
        bounds = tuple((None if cell is None else (round(cell[0], 3), round(cell[2], 3))) for cell in cells)
        return {"labels": tuple(str(x or "") for x in row), "column": current[0],
                "prior_column": prior[0], "bounds": bounds}
    return None


_CELL_CACHE = OrderedDict()


def _fragmented_header(table, period_end):
    """Recognize redundant narrow empty drawn cells, not arbitrary bad tables."""
    for index,row in enumerate(table.extract()[:4]):
        labels=[_compact(x) for x in row]
        if len(row)<8 or not labels or labels[0]:continue
        nonempty=[x for x in labels if x]
        if len(nonempty) not in (3,4) or nonempty[0]!='项目':continue
        if len(nonempty)==4 and nonempty[1]!='附注':continue
        current,prior=nonempty[-2:]
        dated=(_dated_label(current)==period_end and _dated_label(prior) is not None
               and _dated_label(prior)!=period_end)
        relative=(current,prior) in (('本期发生额','上期发生额'),('本年发生额','上年发生额'),
                                    ('期末余额','期初余额'),('年末余额','年初余额'))
        if not (dated or relative):continue
        cells=table.rows[index].cells
        if len(cells)!=len(row):continue
        thin=sum(not label and cell is not None and 0<float(cell[2])-float(cell[0])<=8
                 for label,cell in zip(labels,cells))
        if thin>=3:return True
    return False


def _ruled_page_tables(page,period_end,statement,preamble):
    tables=page.find_tables({'vertical_strategy':'lines','horizontal_strategy':'lines'})
    result=[];alternate=None
    for table in tables:
        if _header(table,period_end,statement,preamble,period_end) is not None or not _fragmented_header(table,period_end):
            result.append(table);continue
        if alternate is None:
            alternate=page.find_tables({'vertical_strategy':'lines','horizontal_strategy':'lines',
                                       'snap_x_tolerance':8,'join_x_tolerance':8})
        # Only the same physical outer table may be reconstructed. A different
        # table, ambiguous reconstruction, or invalid own dated header fails.
        matches=[other for other in alternate if
                 all(abs(float(a)-float(b))<=1 for a,b in zip(table.bbox,other.bbox))
                 and _header(other,period_end,statement,preamble,period_end) is not None]
        result.append(matches[0] if len(matches)==1 else table)
    return tuple(result)


def _amount_columns_match_header(bounds, context, table_bbox, *, continuation):
    """Compare the independent physical amount bands, without any amounts.

    Continued printed grids can move a line by its stroke width (the actual
    archived samples move 0.275/0.325pt). Only continuation pages of the same
    statement may use this allowance, with both outer table edges preserved.
    """
    columns = (context['column'], context['prior_column'])
    expected = context['bounds']
    if len(bounds) != len(expected) or len(set(columns)) != 2:
        return False
    if any(i <= 0 or i >= len(bounds) or bounds[i] is None or expected[i] is None for i in columns):
        return False
    if all(abs(bounds[i][edge] - expected[i][edge]) <= 0.2
           for i in columns for edge in (0, 1)):
        return True
    if not continuation or expected[0] is None or expected[-1] is None:
        return False
    if abs(table_bbox[0] - expected[0][0]) > 1 or abs(table_bbox[2] - expected[-1][1]) > 1:
        return False
    ordered = sorted(columns)
    if bounds[ordered[0]][1] > bounds[ordered[1]][0]:
        return False
    return all(bounds[i][1] - bounds[i][0] >= 10
               and expected[i][1] - expected[i][0] >= 10
               and abs((bounds[i][1] - bounds[i][0]) - (expected[i][1] - expected[i][0])) <= 1
               and all(abs(bounds[i][edge] - expected[i][edge]) <= 1 for edge in (0, 1))
               for i in columns)


@dataclass(frozen=True, slots=True)
class _BlackFillRow:
    cells: tuple


@dataclass(frozen=True, slots=True)
class _BlackFillTable:
    bbox: tuple
    rows: tuple
    texts: tuple
    one_word_amount: tuple

    def extract(self):
        return [list(row) for row in self.texts]


def _black_fill_rect_table(page, original, period_end, statement, preamble):
    """A closed lattice of actual black filled thin rectangles, not strokes.

    Grey objects are not deleted or declared decorative. Own black vertical
    and horizontal bands must independently close every row of three columns.
    No candidate amounts, primary columns or primary rows are inputs.
    """
    if (period_end.month, period_end.day) != (6, 30) or statement not in ("资产负债表", "利润表"):
        return None
    raw = original.extract()
    if not raw or len(raw[0]) < 8 or _compact(raw[0][0]):
        return None
    labels = [_compact(value) for value in raw[0] if _compact(value)]
    if len(labels) != 3 or labels[0] != "项目":
        return None
    if _header(original, period_end, statement, preamble, period_end) is not None:
        return None
    units = [match[1] for line in preamble.splitlines()
             if (match := _UNIT.search(line)) is not None]
    if len(set(units)) != 1 or units[0] not in _SCALE:
        return None
    if re.search(r"币种\s*[:：]\s*(?!人民币|CNY)[A-Za-z\u4e00-\u9fff]+", preamble):
        return None
    if page.lines:
        return None
    bbox = tuple(float(value) for value in original.bbox)
    rects = page.rects
    if not rects or any(not r.get("fill") or r.get("stroke") or r.get("linewidth") != 0 for r in rects):
        return None
    def black(r):
        return r.get("non_stroking_color") == 0
    def in_region(r):
        return (bbox[0]-1 <= r["x0"] < r["x1"] <= bbox[2]+1
                and bbox[1]-2 <= r["top"] < r["bottom"] <= bbox[3]+2)
    vertical = [r for r in rects if black(r) and in_region(r)
                and 0 < r["width"] <= .5 and r["height"] > 2]
    groups = {}
    for r in vertical:
        key = (round(r["x0"], 6), round(r["x1"], 6))
        groups.setdefault(key, []).append(r)
    bands = sorted(groups)
    if len(bands) != 4 or any(bands[n][1] >= bands[n+1][0] for n in range(3)):
        return None
    intervals = []
    for band in bands:
        own = [(round(r["top"], 6), round(r["bottom"], 6)) for r in groups[band]]
        if len(own) != len(set(own)):
            return None
        intervals.append(set(own))
    if not intervals[0] or any(own != intervals[0] for own in intervals[1:]):
        return None
    yrows = sorted(intervals[0])
    if len(yrows) < 3 or any(a[1] >= b[0] for a, b in zip(yrows, yrows[1:])):
        return None
    horizontal = {}
    for r in rects:
        if black(r) and in_region(r) and 0 < r["height"] <= .5 and r["width"] > 20:
            key = (round(r["top"], 6), round(r["bottom"], 6))
            horizontal.setdefault(key, []).append(r)
    used = set()
    def closure(y, edge):
        found = [(key, rows) for key, rows in horizontal.items() if abs(key[edge]-y) <= .01]
        if len(found) != 1 or len(found[0][1]) != 3:
            return None
        key, rows = found[0]
        rows = sorted(rows, key=lambda r:r["x0"])
        # Link actual black segments to the actual adjacent vertical bands.
        if any(abs(row["x0"]-bands[n][1]) > .03
               or abs(row["x1"]-bands[n+1][0]) > .03
               for n, row in enumerate(rows)):
            return None
        used.add(key)
        return sum(key)/2
    bounds = []
    for top, bottom in yrows:
        upper, lower = closure(top, 1), closure(bottom, 0)
        if upper is None or lower is None:
            return None
        bounds.append((upper, lower))
    if set(horizontal) != used:
        return None
    xs = [(left+right)/2 for left, right in bands]
    physical_bbox = (xs[0], bounds[0][0], xs[-1], bounds[-1][1])
    # Exactly the existing one-point same-outer safeguard, without rounding.
    if any(abs(a-b) > 1 for a, b in zip(physical_bbox, bbox)):
        return None
    words = [w for w in page.extract_words(keep_blank_chars=True, use_text_flow=True,
             extra_attrs=["fontname", "size"]) if w["text"].strip()]
    chars = [ch for ch in page.chars if ch["text"].strip()]
    def inside(obj, cell):
        return (cell[0] < obj["x0"] <= obj["x1"] < cell[2]
                and cell[1] < obj["top"] <= obj["bottom"] < cell[3])
    row_cells = [tuple((xs[c], top, xs[c+1], bottom) for c in range(3))
                 for top, bottom in bounds]
    cells = [cell for row in row_cells for cell in row]
    for obj in [*words, *chars]:
        touches = (physical_bbox[0] < obj["x1"] and obj["x0"] < physical_bbox[2]
                   and physical_bbox[1] < obj["bottom"] and obj["top"] < physical_bbox[3])
        if touches and sum(inside(obj, cell) for cell in cells) != 1:
            return None
    signatures = [(ch["text"], ch["x0"], ch["x1"], ch["top"], ch["bottom"])
                  for ch in chars if inside(ch, physical_bbox)]
    if len(signatures) != len(set(signatures)):
        return None
    texts, numeric = [], []
    for row in row_cells:
        text_row, numeric_row = [], []
        for cell in row:
            own_words = [w for w in words if inside(w, cell)]
            own_chars = [ch for ch in chars if inside(ch, cell)]
            owned = []
            for word in own_words:
                glyphs = [ch for ch in own_chars if word["x0"]-.01 <= ch["x0"]
                          and ch["x1"] <= word["x1"]+.01
                          and abs(ch["top"]-word["top"]) <= .02
                          and abs(ch["bottom"]-word["bottom"]) <= .02]
                quoted = "".join(ch["text"] for ch in sorted(glyphs, key=lambda ch:ch["x0"]))
                if _compact(quoted) != _compact(word["text"]):
                    return None
                owned.extend(id(ch) for ch in glyphs)
            if len(owned) != len(set(owned)) or set(owned) != {id(ch) for ch in own_chars}:
                return None
            text = "\n".join(word["text"].strip() for word in own_words)
            text_row.append(text)
            numeric_row.append(len(own_words) == 1 and _amount(text) is not None)
        texts.append(tuple(text_row)); numeric.append(tuple(numeric_row))
    table = _BlackFillTable(physical_bbox, tuple(_BlackFillRow(row) for row in row_cells),
                            tuple(texts), tuple(numeric))
    header = _header(table, period_end, statement, preamble, period_end)
    if (header is None or tuple(_compact(x) for x in header["labels"]) != tuple(labels)
            or header["column"] != 1 or header["prior_column"] != 2):
        return None
    return table


def _extract_black_fill_statement_cells(pdf, located, identity, fields, period_end):
    """Only old-zero fields; keep all existing title/role/unit/PIT inputs."""
    found = {field: [] for field in fields}
    for pos, (start, top, bottom, heading, role, statement) in enumerate(located):
        wanted = [f for f in fields if _FIELD_STATEMENT[f] == statement]
        if not wanted or role != "合并" or pos+1 >= len(located):
            continue
        end = located[pos+1]
        if end[0]-start > 8 or end[4] not in ("母公司", "公司") or end[5] != statement:
            continue
        first = pdf.pages[start]
        preamble = first.crop((0, bottom, first.width, min(first.height, bottom+180))).extract_text() or ""
        units = [(line, match[1]) for line in preamble.splitlines()
                 if (match := _UNIT.search(line)) is not None]
        if len({unit for _, unit in units}) != 1:
            continue
        declaration, unit = units[0]
        for index in range(start, min(end[0], len(pdf.pages)-1)+1):
            page = pdf.pages[index]
            for original in page.find_tables({'vertical_strategy':'lines','horizontal_strategy':'lines'}):
                table = _black_fill_rect_table(page, original, period_end, statement, preamble)
                if table is None or (index == start and table.bbox[1] < bottom) or (index == end[0] and table.bbox[3] > end[1]):
                    continue
                header = _header(table, period_end, statement, preamble, period_end)
                for row_index, row in enumerate(table.extract()):
                    for field in wanted:
                        if not _field_label(field, row[0]):
                            continue
                        column = header['column']
                        if not table.one_word_amount[row_index][column] or not table.one_word_amount[row_index][header['prior_column']]:
                            continue
                        amount = _amount(row[column])
                        found[field].append(GridStatementCell(field, statement, period_end, index+1,
                            start+1, heading, unit, declaration, header['labels'][column],
                            header['labels'][header['prior_column']], row[0], row[column], amount*_SCALE[unit],
                            table.bbox, table.rows[row_index].cells[column], header['labels'], column,
                            identity[0], identity[1]))
    return tuple(found[field][0] for field in sorted(found) if len(found[field]) == 1)


def _merge_black_fill_cells(found, extra):
    # Keep every unique old result unchanged and never resolve an old ambiguity.
    more = {field: [] for field in found}
    for cell in extra:
        if cell.field in more and not found[cell.field]:
            more[cell.field].append(cell)
    return tuple((found[field] if found[field] else more[field])[0]
                 for field in sorted(found)
                 if len(found[field]) == 1 or (not found[field] and len(more[field]) == 1))


def _native_title_pages(payload: bytes, *, statement_names=None):
    """Recall every possible statement page; never read values or choose scope.

    PDFium is only a locator. Broad statement names include narrative/contents
    pages and all consolidated/parent/continued headings. Unreadable pages are
    retained for canonical reading, and any native failure uses the old scan.
    """
    try:
        names = (_FIELD_STATEMENT.values() if statement_names is None
                 else tuple(statement_names))
        if not names or any(not isinstance(name, str) or not name for name in names):
            return None
        import pypdfium2
        # PDFium calls (including resource destruction) must not overlap in
        # different consumer threads. Grid parsing remains outside this lock.
        with _NATIVE_TEXT_LOCK, pypdfium2.PdfDocument(payload) as document:
            pages = []
            for index in range(len(document)):
                page = document[index]
                try:
                    text_page = page.get_textpage()
                    try:
                        text = text_page.get_text_range(errors="strict")
                    finally:
                        text_page.close()
                finally:
                    page.close()
                compact = _compact(text)
                uncertain = (not compact or not re.search(r"[\u4e00-\u9fff]", compact)
                             or re.search(r"[\ufffd\ue000-\uf8ff]", compact))
                if uncertain or any(name in compact for name in names):
                    pages.append(index)
            return len(document), tuple(pages)
    except Exception:
        # Optional native reader cannot become an admission dependency.
        return None


def _canonical_statement_titles(payload: bytes):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(payload))
    native = _native_title_pages(payload)
    indices = (native[1] if native is not None and native[0] == len(reader.pages)
               and native[1] else range(len(reader.pages)))

    def read_titles(page_indices):
        titles = []
        for index in page_indices:
            for line in (reader.pages[index].extract_text() or "").splitlines():
                match = _TITLE.fullmatch(_compact(line))
                if match:
                    titles.append((index, line, match.group(1), match.group(2)))
        return titles

    titles = read_titles(indices)
    if not titles and native is not None:
        return read_titles(range(len(reader.pages)))
    return titles


def extract_grid_statement_cells(payload: bytes, *, instrument_id: str, period_end: date):
    """Reuse immutable cells only after binding the supplied original bytes.

    Retain parsed cells, not hundreds of large PDF byte strings. Readers still
    read and verify their receipt-bound original bytes before entering here.
    """
    if not isinstance(payload, bytes): return ()
    key = (GRID_EXTRACTOR_VERSION, _TITLE_LOCATOR_VERSION, _GRID_TABLE_READER_VERSION, IDENTITY_READER_VERSION,
           hashlib.sha256(payload).hexdigest(), instrument_id, period_end)
    if key in _CELL_CACHE:
        _CELL_CACHE.move_to_end(key)
        return _CELL_CACHE[key]
    result = _extract_grid_statement_cells(payload, instrument_id=instrument_id, period_end=period_end)
    _CELL_CACHE[key] = result
    while len(_CELL_CACHE) > 1024: _CELL_CACHE.popitem(last=False)
    return result


def _extract_grid_statement_cells(payload: bytes, *, instrument_id: str, period_end: date):
    """Read all five fields from physical ruled cells, with independent headers.

    Returned tuples/cells are immutable. The key is original bytes, never a
    mutable path. No candidate amount, candidate column or main section is an
    input. Missing/ambiguous/non-ruled cells are omitted, never replaced by 0.
    """
    import pdfplumber
    if not isinstance(payload, bytes) or not payload.startswith(b"%PDF-") or not re.fullmatch(r"\d{6}", instrument_id): return ()
    # A separate title lookup only narrows pages; geometry, headers and amounts
    # are read independently from drawn cell boundaries below.
    titles = _canonical_statement_titles(payload)
    found = {field: [] for field in _FIELD_STATEMENT}
    with pdfplumber.open(io.BytesIO(payload)) as pdf:
        identity = _frontmatter(pdf, instrument_id, period_end, pdf_bytes=payload)
        if identity is None: return ()
        if not titles:
            for index, page in enumerate(pdf.pages):
                for line in (page.extract_text() or "").splitlines():
                    match = _TITLE.fullmatch(_compact(line))
                    if match: titles.append((index, line, match.group(1), match.group(2)))
        located = []
        for index, line, role, statement in titles:
            page = pdf.pages[index]
            matches = page.search(r"\s*".join(re.escape(char) for char in re.sub(r"\s+", "", line)))
            if len(matches) != 1: continue
            located.append((index, float(matches[0]["top"]), float(matches[0]["bottom"]), line, role, statement))
        located.sort()
        for position, (start, top, bottom, heading, role, statement) in enumerate(located):
            if role != "合并": continue
            end = located[position + 1][:2] if position + 1 < len(located) else (start + 8, float('inf'))
            if end[0] - start > 8: continue
            first_page = pdf.pages[start]
            preamble = first_page.crop((0, bottom, first_page.width, min(first_page.height, bottom + 180))).extract_text() or ""
            if bottom > first_page.height - 120 and start + 1 < len(pdf.pages):
                next_text = (pdf.pages[start+1].crop((0,0,pdf.pages[start+1].width,160)).extract_text() or "")
                # A title wrapping to the next page may have a unit there;
                # reject crossing any intervening financial title.
                if not any(_TITLE.fullmatch(_compact(line)) for line in next_text.splitlines()):
                    preamble += "\n" + next_text
            units = [(line, match[1]) for line in preamble.splitlines() for match in [_UNIT.search(line)] if match]
            if len({unit for _, unit in units}) != 1: continue
            unit_declaration, unit = units[0]
            if any(re.search(r"币种\s*[:：]\s*(?!人民币|CNY)[A-Za-z\u4e00-\u9fff]+", line) for line in preamble.splitlines()): continue
            context = None
            context_page = None
            for page_index in range(start, min(end[0], len(pdf.pages) - 1) + 1):
                page = pdf.pages[page_index]
                for table in _ruled_page_tables(page,period_end,statement,preamble):
                    if page_index == start and table.bbox[1] < bottom: continue
                    if page_index == end[0] and table.bbox[3] > end[1]: continue
                    current_header = _header(table, period_end, statement, preamble, period_end)
                    if current_header is not None:
                        context = current_header
                        context_page = page_index
                    if context is None: continue
                    rows = table.extract()
                    for row_index, row in enumerate(rows):
                        cells = table.rows[row_index].cells
                        if len(row) != len(context['labels']) or len(cells) != len(row): continue
                        column = context['column']
                        bounds = tuple(None if cell is None else (round(cell[0], 3), round(cell[2], 3)) for cell in cells)
                        # Label/reference cells can merge differently on a
                        # wrapped row. The two dated amount cells must retain
                        # their exact independent physical column boundaries.
                        if not _amount_columns_match_header(bounds, context, table.bbox,
                                                            continuation=page_index > context_page):
                            continue
                        amount = _amount(row[column])
                        if amount is None: continue
                        for field, target_statement in _FIELD_STATEMENT.items():
                            if target_statement != statement or not _field_label(field, row[0]): continue
                            found[field].append(GridStatementCell(field, statement, period_end, page_index + 1,
                                start + 1, heading, unit, unit_declaration, context['labels'][column],
                                context['labels'][context['prior_column']], str(row[0]), str(row[column]),
                                amount * _SCALE[unit], tuple(table.bbox), tuple(cells[column]),
                                context['labels'], column, identity[0], identity[1], identity[2]))
        needed = frozenset(field for field, values in found.items() if not values
                           and field in ("parent_equity", "net_profit_attributable", "net_profit_consolidated"))
        extra = _extract_black_fill_statement_cells(pdf, located, identity, needed, period_end) if needed else ()
    return _merge_black_fill_cells(found, extra)
