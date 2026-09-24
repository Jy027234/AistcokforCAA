"""受限的巨潮定期报告 PDF 五字段候选提取器。

这是 601012 与 600519 各五个报告期的已审阅原文，不是通用 PDF 解析器，
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
_AMOUNT = re.compile(r"-?(?:0|[1-9]\d{0,2}(?:,\d{3})*)(?:\.\d{2})\Z")


@dataclass(frozen=True, slots=True)
class PdfCandidateFact:
    field: str
    value_yuan: Decimal
    period_end: date
    report_period_text: str
    statement: str
    pdf_page: int  # PDF page number, one-based; printed page matches in these reports.
    column_header: str
    amount_unit: str
    currency: str
    source_row: str  # Original extracted cells separated by tabs; embedded newlines kept.
    source_cells: tuple[str | None, ...]
    source_current_cell_index: int
    source_prior_cell_index: int
    pdf_sha256: str


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


def extract_s2_candidate_facts(
    pdf_bytes: bytes, *, instrument_id: str, period_end: date,
) -> CninfoS2CandidateFacts:
    """提取已审阅 601012／600519 定期报告的五个当期列候选。

    复核者按 ``pdf_page`` 在各自原文的合并三表，核对
    ``source_cells``、列标题、币种和单位。只返回当期列；比较列可能因会计政策
    追溯调整，不能替代历史公告的当期值。公告日期／正式替代关系须用公告
    原文另行核验，再决定是否转换成版本化正式事实；本接口不执行转换。
    返回元数据不等于公告日、正式修订关系或 PIT 准入。报告字节不在此处落盘。
    若需扩展其他证券／期间／版本，须先单独审阅并建立新的版式契约。
    """

    if instrument_id not in (_INSTRUMENT_ID, "600519") or period_end not in (
        date(2024, 6, 30), date(2024, 12, 31), date(2025, 6, 30),
        date(2025, 12, 31), date(2026, 6, 30),
    ):
        raise CninfoFinancialPdfError("outside reviewed instruments or report periods")
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise CninfoFinancialPdfError("input must be PDF bytes")
    digest = hashlib.sha256(pdf_bytes).hexdigest()
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
