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
_GRID_TABLE_READER_VERSION = "ruled-grid-local-shading-continuation-v3"
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

    def as_evidence(self):
        return {"extractor_version": GRID_EXTRACTOR_VERSION, "field": self.field,
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


def _frontmatter(pdf, instrument_id, period_end):
    issuer = []
    periods = []
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
    if not issuer or not periods or {row[2] for row in periods} != {period_end}: return None
    return tuple(issuer), tuple((p, text) for p, text, _ in periods)


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


def _native_title_pages(payload: bytes):
    """Recall every possible statement page; never read values or choose scope.

    PDFium is only a locator. Broad statement names include narrative/contents
    pages and all consolidated/parent/continued headings. Unreadable pages are
    retained for canonical reading, and any native failure uses the old scan.
    """
    try:
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
                if uncertain or any(name in compact for name in _FIELD_STATEMENT.values()):
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
        identity = _frontmatter(pdf, instrument_id, period_end)
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
                                context['labels'], column, identity[0], identity[1]))
    return tuple(found[field][0] for field in sorted(found) if len(found[field]) == 1)
