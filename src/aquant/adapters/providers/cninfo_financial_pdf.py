"""受限的巨潮定期报告 PDF 五字段候选提取器。

这是 601012、600519、000333 各五个报告期的已审阅原文，不是通用 PDF 解析器，
也不提供公告时间、替代关系或可用于 PIT 的 ``FinancialFact``。输入只接受
下列已核验的官方原文 SHA-256。调用方仍须独立核对公告身份、
人工复核金额与数据权利；任何版式、期间、列或单位偏差均明确失败。

依赖 ``pdfplumber`` 为可选探针依赖；导入模块不会联网或读取本地文件。
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Mapping


class CninfoFinancialPdfError(ValueError):
    """原文未满足已审阅 PDF 样本的严格抽取契约。"""


_INSTRUMENT_ID = "601012"
_COMPANY = "隆基绿能科技股份有限公司"
_MOUTAI_COMPANY = "贵州茅台酒股份有限公司"
_MIDEA_COMPANY = "美的集团股份有限公司"
_AMOUNT = re.compile(r"-?(?:0|[1-9]\d{0,2}(?:,\d{3})*)(?:\.\d{2})\Z")
_THOUSAND_AMOUNT = re.compile(r"(?:0|[1-9]\d{0,2}(?:,\d{3})*)\Z")


@dataclass(frozen=True, slots=True)
class PdfCandidateFact:
    field: str
    value_yuan: Decimal
    period_end: date
    report_period_text: str
    statement: str
    pdf_page: int  # One-based PDF page; printed page may differ.
    column_header: str
    amount_unit: str
    currency: str
    source_row: str  # Original extracted cells separated by tabs; embedded newlines kept.
    source_cells: tuple[str | None, ...]
    source_current_cell_index: int
    source_prior_cell_index: int
    pdf_sha256: str
    # The visible PDF row label. Some text-layout tables split it across rows,
    # so source_cells[0] alone can be empty or just the last character.
    display_row_label: str = ""


@dataclass(frozen=True, slots=True)
class CninfoS2CandidateFacts:
    instrument_id: str
    period_end: date
    version_label: str
    document_url: str
    pdf_sha256: str
    candidates: tuple[PdfCandidateFact, ...]
    pit_eligible: bool = False
    review_status: str = "requires_manual_verification"

    @property
    def by_field(self) -> Mapping[str, PdfCandidateFact]:
        return {item.field: item for item in self.candidates}


@dataclass(frozen=True, slots=True)
class _StatementRule:
    name: str
    start_page: int
    heading: str
    period_heading: str
    column_heading: str
    previous_column_heading: str
    parent_start_page: int
    column_page: int | None = None


@dataclass(frozen=True, slots=True)
class _FieldCellLayout:
    cell_count: int = 4
    current_index: int = 2
    prior_index: int = 3
    empty_index: int | None = None


@dataclass(frozen=True, slots=True)
class _ReviewedReport:
    instrument_id: str
    company: str
    period_end: date
    page_count: int
    cover_marker: str
    statements: Mapping[str, _StatementRule]
    fields: tuple[tuple[str, str, int, str], ...]
    version_label: str
    document_url: str
    cell_layouts: Mapping[str, _FieldCellLayout]


@dataclass(frozen=True, slots=True)
class _MideaReport:
    period_end: date
    page_count: int
    cover_marker: str
    balance_page: int
    profit_page: int
    cashflow_page: int
    document_url: str
    annual_text_layout: bool


_STATEMENTS = {
    "balance": _StatementRule(
        "balance", 118, "合并资产负债表", "2024年12月31日",
        "2024年12月31日", "2023年12月31日", 121,
    ),
    "profit": _StatementRule(
        "profit", 124, "合并利润表", "2024年1—12月", "2024年度", "2023年度", 126,
    ),
    "cashflow": _StatementRule(
        "cashflow", 127, "合并现金流量表", "2024年1—12月", "2024年度", "2023年度", 129,
    ),
}
_FIELDS = (
    ("net_profit_attributable", "profit", 125,
     "1.归属于母公司股东的净利润（净亏损以“-”号填列）"),
    ("net_profit_consolidated", "profit", 125, "五、净利润（净亏损以“－”号填列）"),
    ("revenue", "profit", 124, "其中：营业收入"),
    ("parent_equity", "balance", 121,
     "归属于母公司所有者权益（或股东权益）合计"),
    ("operating_cashflow", "cashflow", 128,
     "经营活动产生的现金流量净额"),
)

_ATTR_LABEL = "1.归属于母公司股东的净利润（净亏损以“-”号填列）"
_EQUITY_LABEL = "归属于母公司所有者权益（或股东权益）合计"
_REVENUE_LABEL = "其中：营业收入"
_CASHFLOW_LABEL = "经营活动产生的现金流量净额"
_NET_LABEL_FULLWIDTH = "五、净利润（净亏损以“－”号填列）"
_NET_LABEL_ASCII = "五、净利润（净亏损以“-”号填列）"


def _field_rules(*, equity: int, revenue: int, net_income: int,
                 cashflow: int, net_label: str,
                 equity_label: str = _EQUITY_LABEL) -> tuple[tuple[str, str, int, str], ...]:
    return (
        ("net_profit_attributable", "profit", net_income, _ATTR_LABEL),
        ("net_profit_consolidated", "profit", net_income, net_label),
        ("revenue", "profit", revenue, _REVENUE_LABEL),
        ("parent_equity", "balance", equity, equity_label),
        ("operating_cashflow", "cashflow", cashflow, _CASHFLOW_LABEL),
    )


_ADDITIONAL_PERIODS: Mapping[date, tuple[int, str, Mapping[str, _StatementRule],
                                 tuple[tuple[str, str, int, str], ...]]] = {
    date(2024, 6, 30): (
        247, "2024年半年度报告", {
            "balance": _StatementRule("balance", 59, "合并资产负债表", "2024年6月30日",
                                      "2024年6月30日", "2023年12月31日", 62),
            "profit": _StatementRule("profit", 65, "合并利润表", "2024年1—6月",
                                     "2024年半年度", "2023年半年度", 67),
            "cashflow": _StatementRule("cashflow", 69, "合并现金流量表", "2024年1—6月",
                                       "2024年半年度", "2023年半年度", 71),
        }, _field_rules(equity=62, revenue=65, net_income=66, cashflow=70,
                        net_label=_NET_LABEL_FULLWIDTH),
    ),
    date(2025, 6, 30): (
        237, "2025年半年度报告", {
            "balance": _StatementRule("balance", 56, "合并资产负债表", "2025年6月30日",
                                      "2025年6月30日", "2024年12月31日", 59),
            "profit": _StatementRule("profit", 62, "合并利润表", "2025年1—6月",
                                     "2025年半年度", "2024年半年度", 64),
            # Heading and report period finish page 65; unit and columns start page 66.
            "cashflow": _StatementRule("cashflow", 65, "合并现金流量表", "2025年1—6月",
                                       "2025年半年度", "2024年半年度", 67, 66),
        }, _field_rules(equity=59, revenue=62, net_income=63, cashflow=66,
                        net_label=_NET_LABEL_FULLWIDTH),
    ),
    date(2025, 12, 31): (
        314, "2025年年度报告", {
            "balance": _StatementRule("balance", 119, "合并资产负债表", "2025年12月31日",
                                      "2025年12月31日", "2024年12月31日", 122),
            "profit": _StatementRule("profit", 125, "合并利润表", "2025年1—12月",
                                     "2025年度", "2024年度", 127),
            "cashflow": _StatementRule("cashflow", 128, "合并现金流量表", "2025年1—12月",
                                       "2025年度", "2024年度", 130),
        }, _field_rules(equity=122, revenue=125, net_income=126, cashflow=129,
                        net_label=_NET_LABEL_ASCII),
    ),
    date(2026, 6, 30): (
        241, "2026年半年度报告", {
            "balance": _StatementRule("balance", 63, "合并资产负债表", "2026年6月30日",
                                      "2026年6月30日", "2025年12月31日", 66),
            "profit": _StatementRule("profit", 69, "合并利润表", "2026年1—6月",
                                     "2026年半年度", "2025年半年度", 71),
            "cashflow": _StatementRule("cashflow", 72, "合并现金流量表", "2026年1—6月",
                                       "2026年半年度", "2025年半年度", 74),
        }, _field_rules(equity=66, revenue=69, net_income=70, cashflow=73,
                        net_label=_NET_LABEL_ASCII),
    ),
}

_ANNUAL_2024 = (301, "2024年年度报告", _STATEMENTS, _FIELDS)

_MOUTAI_PERIODS: Mapping[date, tuple[int, str, Mapping[str, _StatementRule],
                             tuple[tuple[str, str, int, str], ...]]] = {
    date(2024, 6, 30): (
        105, "2024年半年度报告", {
            "balance": _StatementRule("balance", 26, "合并资产负债表", "2024年6月30日",
                                      "2024年6月30日", "2023年12月31日", 28),
            "profit": _StatementRule("profit", 30, "合并利润表", "2024年1—6月",
                                     "2024年半年度", "2023年半年度", 32),
            "cashflow": _StatementRule("cashflow", 34, "合并现金流量表", "2024年1—6月",
                                       "2024年半年度", "2023年半年度", 36),
        }, _field_rules(equity=28, revenue=30, net_income=31, cashflow=35,
                        net_label=_NET_LABEL_FULLWIDTH),
    ),
    date(2024, 12, 31): (
        143, "2024年年度报告", {
            "balance": _StatementRule("balance", 58, "合并资产负债表", "2024年12月31日",
                                      "2024年12月31日", "2023年12月31日", 61),
            "profit": _StatementRule("profit", 63, "合并利润表", "2024年1—12月",
                                     "2024年度", "2023年度", 65),
            "cashflow": _StatementRule("cashflow", 66, "合并现金流量表", "2024年1—12月",
                                       "2024年度", "2023年度", 68),
        }, _field_rules(equity=61, revenue=63, net_income=64, cashflow=67,
                        net_label=_NET_LABEL_FULLWIDTH),
    ),
    date(2025, 6, 30): (
        103, "2025年半年度报告", {
            "balance": _StatementRule("balance", 24, "合并资产负债表", "2025年6月30日",
                                      "2025年6月30日", "2024年12月31日", 26),
            "profit": _StatementRule("profit", 28, "合并利润表", "2025年1—6月",
                                     "2025年半年度", "2024年半年度", 30),
            "cashflow": _StatementRule("cashflow", 31, "合并现金流量表", "2025年1—6月",
                                       "2025年半年度", "2024年半年度", 33),
        }, _field_rules(equity=26, revenue=28, net_income=29, cashflow=32,
                        net_label=_NET_LABEL_FULLWIDTH),
    ),
    date(2025, 12, 31): (
        143, "2025年年度报告", {
            "balance": _StatementRule("balance", 56, "合并资产负债表", "2025年12月31日",
                                      "2025年12月31日", "2024年12月31日", 59),
            "profit": _StatementRule("profit", 61, "合并利润表", "2025年1—12月",
                                     "2025年度", "2024年度", 63),
            "cashflow": _StatementRule("cashflow", 64, "合并现金流量表", "2025年1—12月",
                                       "2025年度", "2024年度", 66),
        }, _field_rules(equity=58, revenue=61, net_income=62, cashflow=65,
                        net_label=_NET_LABEL_FULLWIDTH,
                        equity_label="归属于母公司所有者权益"),
    ),
    date(2026, 6, 30): (
        110, "2026年半年度报告", {
            "balance": _StatementRule("balance", 25, "合并资产负债表", "2026 年6月30 日",
                                      "2026年6月30日", "2025年12月31日", 28),
            "profit": _StatementRule("profit", 30, "合并利润表", "2026年1—6月",
                                     "2026年半年度", "2025年半年度", 32),
            "cashflow": _StatementRule("cashflow", 33, "合并现金流量表", "2026年1—6月",
                                       "2026年半年度", "2025年半年度", 35),
        }, _field_rules(equity=28, revenue=30, net_income=31, cashflow=34,
                        net_label=_NET_LABEL_ASCII),
    ),
}


def _reviewed_report(period_end: date, version_label: str,
                     document_url: str) -> _ReviewedReport:
    spec = (_ANNUAL_2024 if period_end == date(2024, 12, 31)
            else _ADDITIONAL_PERIODS[period_end])
    return _ReviewedReport(
        _INSTRUMENT_ID, _COMPANY, period_end, *spec, version_label,
        document_url, {},
    )


def _moutai_report(period_end: date, document_url: str) -> _ReviewedReport:
    spec = _MOUTAI_PERIODS[period_end]
    layouts = ({
        "net_profit_attributable": _FieldCellLayout(5, 2, 4, 3),
        "net_profit_consolidated": _FieldCellLayout(5, 2, 3, 4),
    } if period_end == date(2025, 12, 31) else {})
    return _ReviewedReport(
        "600519", _MOUTAI_COMPANY, period_end, *spec,
        "indexed_full_report", document_url, layouts,
    )


_VERSIONS = {
    "3dbaf5314ab01f44b58f58e8ad75fea0dce3ced0001db516829f2624b615a3a0":
        _reviewed_report(date(2024, 12, 31), "original",
                         "https://static.cninfo.com.cn/finalpage/2025-04-30/1223421477.PDF"),
    "c9e11fc7ec92a59d7d1ccfcf8a4f1fbb35ee8ec0435df16a3e968497b5f64bb8":
        _reviewed_report(date(2024, 12, 31), "revised",
                         "https://static.cninfo.com.cn/finalpage/2025-05-07/1223477802.PDF"),
    "dba216e8f28538ce5fd3bf9b1cd4e428af8b52ff3dcf0795ac5237cd39543677":
        _reviewed_report(date(2024, 6, 30), "indexed_full_report",
                         "https://static.cninfo.com.cn/finalpage/2024-08-31/1221087751.PDF"),
    "449aaf2308a9374317604a7a7dce7eb1ef661d3c01f3e2f8a0f1cd0cb83a28c6":
        _reviewed_report(date(2025, 6, 30), "indexed_full_report",
                         "https://static.cninfo.com.cn/finalpage/2025-08-23/1224562068.PDF"),
    "10233740d54740b89dfd772eecac5f04d8fef639489acd3b18b1a35e81bce2c1":
        _reviewed_report(date(2025, 12, 31), "indexed_full_report",
                         "https://static.cninfo.com.cn/finalpage/2026-04-29/1225251647.PDF"),
    "c3bcfa8bcf5cd18df6c008a48382119039eac71fe4273973fa0c8bbe7128f103":
        _reviewed_report(date(2026, 6, 30), "indexed_full_report",
                         "https://static.cninfo.com.cn/finalpage/2026-08-31/1225535067.PDF"),
    "898b37d6529d0042aa66461bae11b84a47f9337c2ff1d572155bfa71fca77c58":
        _moutai_report(date(2024, 6, 30),
                       "https://static.cninfo.com.cn/finalpage/2024-08-09/1220825189.PDF"),
    "5299f4940e2ce4e91084b73dc457d558b9d335fa76fbfee6227e4254eb7f4a30":
        _moutai_report(date(2024, 12, 31),
                       "https://static.cninfo.com.cn/finalpage/2025-04-03/1222993920.PDF"),
    "c80fb7180169469053c396e65315414368bcfa9f1f1d4e3bf793c8fb327e6b0c":
        _moutai_report(date(2025, 6, 30),
                       "https://static.cninfo.com.cn/finalpage/2025-08-13/1224462930.PDF"),
    "474905deeaf0f875fc0a1b097a626c0c7852c427faadc5d7fc7816cbf45ea288":
        _moutai_report(date(2025, 12, 31),
                       "https://static.cninfo.com.cn/finalpage/2026-04-17/1225114741.PDF"),
    "0e10aa26be46b1cf3cd03f06e834c7fb98d5dd0d661b96f8fddd4af7e846a4f6":
        _moutai_report(date(2026, 6, 30),
                       "https://static.cninfo.com.cn/finalpage/2026-08-15/1225475868.PDF"),
}


# These five documents use RMB thousands and four amount columns in a combined
# consolidated/company statement. Annual pages are borderless and require a
# separate text-table contract; they cannot reuse the yuan/two-column rules.
_MIDEA_VERSIONS: Mapping[str, _MideaReport] = {
    "29987795727aa66db08035eb9eff80eaf5622940fadcf05d44bca31cd2f47521":
        _MideaReport(date(2024, 6, 30), 212, "2024年半年度报告", 107, 108, 109,
                     "https://static.cninfo.com.cn/finalpage/2024-08-20/1220909926.PDF", False),
    "b17a9b9b84bca1d2a4e4a3cadc5dd5ba5c85e3f1fd2d758acd3315b5b040ecd9":
        _MideaReport(date(2024, 12, 31), 295, "2024年度报告", 157, 158, 160,
                     "https://static.cninfo.com.cn/finalpage/2025-03-29/1222951181.PDF", True),
    "cec88d9c6ded328ac9b467ba55254ab831b906dfc613982f6ac0504fc2055a12":
        _MideaReport(date(2025, 6, 30), 205, "2025年半年度报告", 95, 96, 97,
                     "https://static.cninfo.com.cn/finalpage/2025-08-30/1224626720.PDF", False),
    "16f95f70527db59dcf2736f276a9479cf7ee917e5f71e4f6cbbe83acbad9f4b6":
        _MideaReport(date(2025, 12, 31), 276, "2025年年度报告", 133, 135, 137,
                     "https://static.cninfo.com.cn/finalpage/2026-03-31/1225065145.PDF", True),
    "576dd80e353e53296a800b03e9889a9cbb2e8b91fa2ab3c1dace7c10159179b8":
        _MideaReport(date(2026, 6, 30), 205, "2026年半年度报告", 97, 98, 99,
                     "https://static.cninfo.com.cn/finalpage/2026-08-29/1225531404.PDF", False),
}


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _parse_amount(value: str | None, *, field: str) -> Decimal:
    if value is None or not _AMOUNT.fullmatch(value.strip()):
        raise CninfoFinancialPdfError(f"{field}: missing or ambiguous yuan amount")
    try:
        return Decimal(value.replace(",", ""))
    except InvalidOperation as exc:
        raise CninfoFinancialPdfError(f"{field}: invalid yuan amount") from exc


def _header_from_page(page, rule: _StatementRule,  # noqa: ANN001
                      *, company: str = _COMPANY) -> None:
    text = page.extract_text() or ""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line == rule.heading]
    if len(starts) != 1 or lines[starts[0] + 1:starts[0] + 2] != [rule.period_heading]:
        raise CninfoFinancialPdfError(
            f"{rule.name}: consolidated heading, period or CNY/yuan unit ambiguous"
        )
    following = lines[starts[0] + 2:starts[0] + 4]
    if following[:1] != ["单位：元 币种：人民币"] and not (
        len(following) == 2 and
        _compact(following[0]) == f"编制单位：{company}" and
        following[1] == "单位：元 币种：人民币"
    ):
        raise CninfoFinancialPdfError(
            f"{rule.name}: consolidated heading, period or CNY/yuan unit ambiguous"
        )
    _check_column_header(page, rule)


def _check_column_header(page, rule: _StatementRule) -> None:  # noqa: ANN001
    header = ["项目", "附注", rule.column_heading, rule.previous_column_heading]
    matches = [table[0] for table in page.extract_tables()
               if table and table[0] == header]
    if len(matches) != 1:
        raise CninfoFinancialPdfError(
            f"{rule.name}: expected one exact current/prior column header"
        )


def _header_from_split_pages(title_page, column_page,  # noqa: ANN001
                             rule: _StatementRule) -> None:
    title_lines = (title_page.extract_text() or "").splitlines()
    column_lines = (column_page.extract_text() or "").splitlines()
    # The reviewed 2025H1 cash-flow heading is the last content on page 65.
    if title_lines[-3:-1] != [rule.heading, rule.period_heading]:
        raise CninfoFinancialPdfError(
            f"{rule.name}: split consolidated heading or period ambiguous"
        )
    if column_lines[1:2] != ["单位：元 币种：人民币"]:
        raise CninfoFinancialPdfError(
            f"{rule.name}: split CNY/yuan unit ambiguous"
        )
    _check_column_header(column_page, rule)


def _unique_row(page, *, field: str, label: str,  # noqa: ANN001
                layout: _FieldCellLayout = _FieldCellLayout()) -> tuple[str | None, ...]:
    matches: list[tuple[str | None, ...]] = []
    for table in page.extract_tables():
        for row in table:
            if not row or not row[0] or _compact(row[0]) != _compact(label):
                continue
            if (len(row) != layout.cell_count or
                    row[layout.current_index] is None or
                    row[layout.prior_index] is None or
                    (layout.empty_index is not None and
                     row[layout.empty_index] is not None) or
                    (layout.empty_index is None and any(cell is None for cell in row))):
                raise CninfoFinancialPdfError(f"{field}: ambiguous table columns")
            matches.append(tuple(row))
    if len(matches) != 1:
        raise CninfoFinancialPdfError(
            f"{field}: expected one exact row, found {len(matches)}"
        )
    return matches[0]


_MIDEA_ANNUAL_TABLE_SETTINGS = {
    "vertical_strategy": "text",
    "horizontal_strategy": "text",
    "snap_tolerance": 3,
    "join_tolerance": 3,
    "text_tolerance": 3,
}
_MIDEA_UNIT_LINE = "(除特别注明外，金额单位为人民币千元)"
_MIDEA_ROLES = ("合并", "合并", "公司", "公司")


def _midea_thousand_amount(cell: str | None, *, field: str) -> Decimal:
    if not isinstance(cell, str):
        raise CninfoFinancialPdfError(f"{field}: missing thousand-yuan amount")
    raw = cell.strip()
    negative = raw.startswith("(") and raw.endswith(")")
    digits = raw[1:-1] if negative else raw
    if not _THOUSAND_AMOUNT.fullmatch(digits):
        raise CninfoFinancialPdfError(f"{field}: ambiguous thousand-yuan amount")
    value = Decimal(digits.replace(",", ""))
    return -value if negative else value


def _midea_period_labels(profile: _MideaReport, statement: str) -> tuple[str, str]:
    current = profile.period_end
    if statement == "balance":
        return (f"{current.year}年{current.month}月{current.day}日",
                f"{current.year - 1}年12月31日")
    suffix = "年度" if profile.annual_text_layout else "年半年度"
    return f"{current.year}{suffix}", f"{current.year - 1}{suffix}"


def _midea_header_table(page, profile: _MideaReport,  # noqa: ANN001
                        statement: str) -> tuple[list[str], list[list[str | None]]]:
    lines = (page.extract_text() or "").splitlines()
    if len(lines) < 7 or _compact(lines[0]) != _MIDEA_COMPANY:
        raise CninfoFinancialPdfError(f"{statement}: Midea company heading mismatch")
    if statement == "balance":
        heading = "合并及公司资产负债表(续)"
        date_heading = _midea_period_labels(profile, statement)[0]
        if (_compact(lines[1]) != heading or
                _compact(lines[2]) != date_heading or
                _compact(lines[3]) != _MIDEA_UNIT_LINE):
            raise CninfoFinancialPdfError(f"{statement}: Midea heading/period/unit mismatch")
    else:
        suffix = "年度" if profile.annual_text_layout else "年半年度"
        kind = "利润表" if statement == "profit" else "现金流量表"
        heading = f"{profile.period_end.year}{suffix}合并及公司{kind}"
        if (_compact(lines[1]) != heading or
                _compact(lines[2]) != _MIDEA_UNIT_LINE):
            raise CninfoFinancialPdfError(f"{statement}: Midea heading/period/unit mismatch")
    if not any(line.strip().endswith("合并 合并 公司 公司") for line in lines[:9]):
        raise CninfoFinancialPdfError(f"{statement}: Midea consolidated/company order mismatch")

    tables = page.extract_tables(
        _MIDEA_ANNUAL_TABLE_SETTINGS if profile.annual_text_layout else None,
    )
    if len(tables) != 1 or len(tables[0]) < 3:
        raise CninfoFinancialPdfError(f"{statement}: Midea table layout mismatch")
    table = tables[0]
    current, prior = _midea_period_labels(profile, statement)
    header, role = table[0], table[1]
    if statement == "balance" and profile.annual_text_layout:
        valid = (len(header) == len(role) == 6 and
                 tuple(_compact(x or "") for x in header[:2]) ==
                 ("负债和股东权益", "附注") and
                 tuple(_compact(x or "") for x in header[2:]) ==
                 (f"{profile.period_end.year}年", f"{profile.period_end.year-1}年",
                  f"{profile.period_end.year}年", f"{profile.period_end.year-1}年") and
                 tuple(_compact(x or "") for x in role[2:]) ==
                 ("12月31日",) * 4 and
                 tuple(table[2][2:]) == _MIDEA_ROLES)
    elif statement == "cashflow" and profile.annual_text_layout:
        valid = (len(header) == len(role) == 6 and
                 tuple(header[2:]) == (current, prior, current, prior) and
                 tuple(role) == ("项目", "附注", *_MIDEA_ROLES))
    elif statement == "profit" and profile.annual_text_layout:
        valid = (len(header) == len(role) == 7 and
                 tuple(header[:3]) == ("", "项目", "附注") and
                 tuple(header[3:]) == (current, prior, current, prior) and
                 tuple(role[3:]) == _MIDEA_ROLES)
    else:
        valid = (len(header) == len(role) == 6 and
                 tuple(_compact(x or "") for x in header[2:]) ==
                 (current, prior, current, prior) and
                 tuple(role[2:]) == _MIDEA_ROLES)
    if not valid:
        raise CninfoFinancialPdfError(f"{statement}: Midea current/prior columns mismatch")
    return lines, table


def _midea_corrob_line(lines: list[str], label: str,
                       amounts: tuple[str | None, ...], *, field: str) -> None:
    expected = tuple(amount or "" for amount in amounts)
    candidates = list(lines)
    if label == "归属于母公司股东的净利润":
        candidates.extend(
            lines[i] + lines[i + 1] for i in range(len(lines) - 1)
            if lines[i] == "归属于母公司股东的" and lines[i + 1].startswith("净利润 ")
        )
    matches = [line for line in candidates if _compact(line).startswith(_compact(label)) and
               tuple(re.findall(r"\(?\d[\d,]*\)?", line))[-4:] == expected]
    if len(matches) != 1:
        raise CninfoFinancialPdfError(f"{field}: Midea source text disagrees with table")


def _midea_row(table: list[list[str | None]], lines: list[str], *,
               field: str, label: str,
               annual_profit: bool) -> tuple[tuple[str | None, ...], int, str]:
    prefix = ""
    if annual_profit and field == "revenue":
        candidates = [(i, row) for i, row in enumerate(table)
                      if len(row) == 7 and row[:2] == ["：", "营业收入"]]
        if len(candidates) != 1:
            raise CninfoFinancialPdfError(f"{field}: Midea annual row ambiguous")
        i, row = candidates[0]
        prior_note = table[i - 1] if i > 0 else []
        if (len(prior_note) != 7 or prior_note[:2] != ["", ""] or
                not isinstance(prior_note[2], str) or
                not re.fullmatch(r"四\(\d+\),", prior_note[2]) or
                not isinstance(row[2], str) or
                not re.fullmatch(r"十八\(3\)", row[2]) or
                any(prior_note[3:])):
            raise CninfoFinancialPdfError(f"{field}: Midea annual note/row mismatch")
        prefix = "\t".join(part or "" for part in prior_note) + "\n"
    elif annual_profit and field == "net_profit_consolidated":
        candidates = [(i, row) for i, row in enumerate(table)
                      if len(row) == 7 and row[:3] == ["润", "", ""]]
        if len(candidates) != 1:
            raise CninfoFinancialPdfError(f"{field}: Midea annual row ambiguous")
        _, row = candidates[0]
    elif annual_profit and field == "net_profit_attributable":
        candidates = [(i, row) for i, row in enumerate(table)
                      if len(row) == 7 and row[:3] == ["", "归属于母公司股东的", ""]]
        if len(candidates) != 1:
            raise CninfoFinancialPdfError(f"{field}: Midea annual row ambiguous")
        i, prior_row = candidates[0]
        row = table[i + 1] if i + 1 < len(table) else []
        if (len(row) != 7 or row[:3] != ["", "净利润", ""] or
                any(prior_row[3:])):
            raise CninfoFinancialPdfError(f"{field}: Midea annual split row mismatch")
        prefix = "\t".join(part or "" for part in prior_row) + "\n"
        if lines.count("归属于母公司股东的") != 1:
            raise CninfoFinancialPdfError(f"{field}: Midea annual split text mismatch")
    else:
        candidates = [(i, row) for i, row in enumerate(table)
                      if len(row) == 6 and _compact(row[0] or "") == _compact(label)]
        if len(candidates) != 1:
            raise CninfoFinancialPdfError(f"{field}: Midea row ambiguous")
        _, row = candidates[0]
    current_index = 3 if annual_profit else 2
    if len(row) != current_index + 4:
        raise CninfoFinancialPdfError(f"{field}: Midea row column count mismatch")
    for cell in row[current_index:]:
        _midea_thousand_amount(cell, field=field)
    _midea_corrob_line(lines, label, tuple(row[current_index:]), field=field)
    return tuple(row), current_index, prefix + "\t".join(part or "" for part in row)


def _extract_midea_candidate_facts(pdf_bytes: bytes, *, profile: _MideaReport,
                                   digest: str) -> CninfoS2CandidateFacts:
    try:
        import pdfplumber
    except ImportError as exc:
        raise CninfoFinancialPdfError("pdfplumber is required for this PDF probe") from exc
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if len(pdf.pages) != profile.page_count:
                raise CninfoFinancialPdfError("Midea report page count mismatch")
            cover = _compact(pdf.pages[0].extract_text() or "")
            if _MIDEA_COMPANY not in cover or profile.cover_marker not in cover:
                raise CninfoFinancialPdfError("Midea company or report period mismatch")
            statement_pages = {
                "balance": profile.balance_page,
                "profit": profile.profit_page,
                "cashflow": profile.cashflow_page,
            }
            evidence = {
                key: _midea_header_table(pdf.pages[page - 1], profile, key)
                for key, page in statement_pages.items()
            }
            fields = (
                ("net_profit_attributable", "profit", "归属于母公司股东的净利润"),
                ("net_profit_consolidated", "profit", "四、净利润" if profile.annual_text_layout
                 else "五、净利润"),
                ("revenue", "profit", "其中：营业收入"),
                ("parent_equity", "balance", "归属于母公司股东权益合计"),
                ("operating_cashflow", "cashflow",
                 "经营活动产生/(使用)的现金流量净额" if profile.period_end == date(2025, 12, 31)
                 else "经营活动产生的现金流量净额"),
            )
            candidates = []
            for field, statement, label in fields:
                lines, table = evidence[statement]
                row, current_index, source_row = _midea_row(
                    table, lines, field=field, label=label,
                    annual_profit=profile.annual_text_layout and statement == "profit",
                )
                current = _midea_thousand_amount(row[current_index], field=field)
                _midea_thousand_amount(row[current_index + 1], field=f"{field} prior")
                current_header, _ = _midea_period_labels(profile, statement)
                candidates.append(PdfCandidateFact(
                    field=field, value_yuan=current * Decimal(1000),
                    period_end=profile.period_end,
                    report_period_text=current_header, statement=statement,
                    pdf_page=statement_pages[statement],
                    column_header=current_header, amount_unit="千元", currency="CNY",
                    source_row=source_row, source_cells=row,
                    source_current_cell_index=current_index,
                    source_prior_cell_index=current_index + 1,
                    pdf_sha256="sha256:" + digest,
                    display_row_label=label,
                ))
    except CninfoFinancialPdfError:
        raise
    except Exception as exc:  # noqa: BLE001 - PDF parser failures are unsafe
        raise CninfoFinancialPdfError(
            f"Midea PDF parsing failed: {type(exc).__name__}"
        ) from exc
    return CninfoS2CandidateFacts(
        instrument_id="000333", period_end=profile.period_end,
        version_label="indexed_full_report", document_url=profile.document_url,
        pdf_sha256="sha256:" + digest, candidates=tuple(candidates),
    )


def extract_s2_candidate_facts(
    pdf_bytes: bytes, *, instrument_id: str, period_end: date,
) -> CninfoS2CandidateFacts:
    """提取已审阅 601012／600519／000333 定期报告的五个当期列候选。

    复核者按 ``pdf_page`` 在各自原文的合并三表，核对
    ``source_cells``、列标题、币种和单位。只返回当期列；比较列可能因会计政策
    追溯调整，不能替代历史公告的当期值。公告日期／正式替代关系须用公告
    原文另行核验，再决定是否转换成版本化正式事实；本接口不执行转换。
    返回元数据不等于公告日、正式修订关系或 PIT 准入。报告字节不在此处落盘。
    若需扩展其他证券／期间／版本，须先单独审阅并建立新的版式契约。
    """

    if instrument_id not in (_INSTRUMENT_ID, "600519", "000333") or period_end not in (
        date(2024, 6, 30), date(2024, 12, 31), date(2025, 6, 30),
        date(2025, 12, 31), date(2026, 6, 30),
    ):
        raise CninfoFinancialPdfError("outside reviewed instruments or report periods")
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise CninfoFinancialPdfError("input must be PDF bytes")
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    midea_profile = _MIDEA_VERSIONS.get(digest)
    if midea_profile is not None:
        if instrument_id != "000333":
            raise CninfoFinancialPdfError("requested instrument mismatches approved PDF version")
        if period_end != midea_profile.period_end:
            raise CninfoFinancialPdfError("requested period mismatches approved PDF version")
        return _extract_midea_candidate_facts(
            pdf_bytes, profile=midea_profile, digest=digest,
        )
    profile = _VERSIONS.get(digest)
    if profile is None:
        raise CninfoFinancialPdfError("PDF SHA-256 is not an approved report version")
    if instrument_id != profile.instrument_id:
        raise CninfoFinancialPdfError("requested instrument mismatches approved PDF version")
    if period_end != profile.period_end:
        raise CninfoFinancialPdfError("requested period mismatches approved PDF version")

    try:
        import pdfplumber
    except ImportError as exc:
        raise CninfoFinancialPdfError("pdfplumber is required for this PDF probe") from exc

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if len(pdf.pages) != profile.page_count:
                raise CninfoFinancialPdfError("unexpected report page count")
            cover = pdf.pages[0].extract_text() or ""
            if profile.company not in _compact(cover) or profile.cover_marker not in _compact(cover):
                raise CninfoFinancialPdfError("company or report period mismatch")
            for rule in profile.statements.values():
                if rule.column_page is None:
                    _header_from_page(
                        pdf.pages[rule.start_page - 1], rule, company=profile.company,
                    )
                else:
                    _header_from_split_pages(
                        pdf.pages[rule.start_page - 1],
                        pdf.pages[rule.column_page - 1], rule,
                    )

            candidates: list[PdfCandidateFact] = []
            for field, statement, page_number, label in profile.fields:
                page = pdf.pages[page_number - 1]
                layout = profile.cell_layouts.get(field, _FieldCellLayout())
                row = _unique_row(page, field=field, label=label, layout=layout)
                current, prior = row[layout.current_index], row[layout.prior_index]
                # Both amount columns must be well formed, so table shifts are
                # rejected even if the desired cell happens to parse.
                amount = _parse_amount(current, field=field)
                _parse_amount(prior, field=f"{field} prior column")
                text = page.extract_text() or ""
                if (profile.cover_marker not in _compact(text) or
                        current not in text or prior not in text):
                    raise CninfoFinancialPdfError(f"{field}: row/page evidence mismatch")
                rule = profile.statements[statement]
                if page_number > rule.parent_start_page:
                    raise CninfoFinancialPdfError(
                        f"{field}: row outside consolidated statement"
                    )
                if page_number == rule.parent_start_page:
                    parent_heading = {
                        "balance": "母公司资产负债表",
                        "profit": "母公司利润表",
                        "cashflow": "母公司现金流量表",
                    }[statement]
                    marker = text.find(parent_heading)
                    row_start = text.find(row[0].splitlines()[0])
                    if marker < 0 or row_start < 0 or row_start >= marker:
                        raise CninfoFinancialPdfError(
                            f"{field}: row outside consolidated statement"
                        )
                candidates.append(PdfCandidateFact(
                    field=field, value_yuan=amount, period_end=period_end,
                    report_period_text=rule.period_heading,
                    statement=statement, pdf_page=page_number,
                    column_header=rule.column_heading, amount_unit="元",
                    currency="CNY", source_row="\t".join(cell or "" for cell in row),
                    source_cells=row,
                    source_current_cell_index=layout.current_index,
                    source_prior_cell_index=layout.prior_index,
                    pdf_sha256="sha256:" + digest,
                    display_row_label=label,
                ))
    except CninfoFinancialPdfError:
        raise
    except Exception as exc:  # noqa: BLE001 - PDF parser failures are unsafe
        raise CninfoFinancialPdfError(
            f"PDF parsing failed: {type(exc).__name__}"
        ) from exc

    return CninfoS2CandidateFacts(
        instrument_id=instrument_id, period_end=period_end,
        version_label=profile.version_label, document_url=profile.document_url,
        pdf_sha256="sha256:" + digest, candidates=tuple(candidates),
    )
