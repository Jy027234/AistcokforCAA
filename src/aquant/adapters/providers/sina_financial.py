"""新浪财经三表的受限 S2 探针适配器。

这个模块只解析公开 HTML 页面中的结构化表格，用来测量免费数据源是否覆盖
F07--F09 所需绝对字段。新浪页面不提供可靠的公告日期与历史修订链，因此
结果始终标为 ``pit_eligible=False``；调用方不得把它直接写成已验收的 S2
财报，也不得用巨潮的公告日期静默伪装成同源元数据。

源页面以“万元”展示金额。适配器用 Decimal 转成“元”，并保留 ``--`` 等
缺失值为 None，绝不把缺失填成 0。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Mapping
from urllib.request import Request, urlopen


SOURCE_ID = "sina-financial"
_UNIT_MULTIPLIERS = {"万元": Decimal("10000")}
_PAGE_PATHS = {
    "profit": "vFD_ProfitStatement",
    "balance": "vFD_BalanceSheet",
    "cashflow": "vFD_CashFlow",
}


class SinaFinancialError(RuntimeError):
    """请求或页面结构不满足受限探针契约。"""


class _Rows(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(_clean("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
            self._cell = None


def _clean(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("：", ":")


def _amount(value: str, multiplier: Decimal) -> Decimal | None:
    text = _clean(value).replace(",", "")
    if text in ("", "-", "--", "—", "N/A", "None"):
        return None
    try:
        return Decimal(text) * multiplier
    except InvalidOperation as exc:
        raise SinaFinancialError(f"invalid amount {value!r}") from exc


@dataclass(frozen=True, slots=True)
class SinaStatementPage:
    stock_code: str
    statement: str
    periods: tuple[date, ...]
    rows: Mapping[str, tuple[Decimal | None, ...]]
    content_hash: str
    retrieved_at: datetime
    amount_unit: str = "yuan"


@dataclass(frozen=True, slots=True)
class SinaS2Row:
    stock_code: str
    end_date: date
    revenue: Decimal | None
    net_income: Decimal | None
    net_income_attributable: Decimal | None
    parent_equity: Decimal | None
    operating_cashflow: Decimal | None
    source_id: str
    retrieved_at: datetime
    pit_eligible: bool = False
    pit_blocker: str = "source page has no verified announcement date or revision chain"
    amount_unit: str = "yuan"
    # The three source pages are one logical observation.  Keep every page
    # digest on the row so a downstream fact adapter can retain provenance
    # without inventing a document hash for a missing page.
    content_hashes: tuple[str, ...] = ()

    @property
    def content_hash(self) -> str:
        """Stable digest for the complete three-page observation."""

        if not self.content_hashes:
            return ""
        digest_input = "|".join(self.content_hashes).encode("utf-8")
        return "sha256:" + hashlib.sha256(digest_input).hexdigest()

    @property
    def required_fields_present(self) -> bool:
        return all(value is not None for value in (
            self.revenue, self.net_income, self.net_income_attributable,
            self.parent_equity, self.operating_cashflow,
        ))


def parse_statement_page(html: bytes | str, *, stock_code: str,
                         statement: str,
                         retrieved_at: datetime | None = None) -> SinaStatementPage:
    """解析一张新浪财务 HTML；缺失值保留为 None。"""

    if statement not in _PAGE_PATHS:
        raise SinaFinancialError(f"unknown statement {statement!r}")
    raw = html if isinstance(html, bytes) else html.encode("utf-8")
    text = (html.decode("gb18030", "replace") if isinstance(html, bytes)
            else html)
    unit_match = re.search(r"单位\s*[:：]\s*([^<\s]+)", text)
    unit = _clean(unit_match.group(1)) if unit_match else ""
    multiplier = _UNIT_MULTIPLIERS.get(unit)
    if multiplier is None:
        raise SinaFinancialError(f"unsupported or missing amount unit {unit!r}")

    parser = _Rows()
    parser.feed(text)
    date_row = next((row for row in parser.rows
                     if row and _clean(row[0]) == "报表日期"), None)
    if date_row is None or len(date_row) < 2:
        raise SinaFinancialError("statement page has no report-date row")
    try:
        periods = tuple(date.fromisoformat(_clean(value)) for value in date_row[1:])
    except ValueError as exc:
        raise SinaFinancialError("statement page has an invalid report date") from exc

    rows: dict[str, tuple[Decimal | None, ...]] = {}
    for row in parser.rows:
        if len(row) != len(periods) + 1:
            continue
        label = _clean(row[0])
        if not label or label == "报表日期":
            continue
        values = tuple(_amount(value, multiplier) for value in row[1:])
        rows.setdefault(label, values)
    if not rows:
        raise SinaFinancialError("statement page has no financial rows")

    observed = retrieved_at or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        raise SinaFinancialError("retrieved_at must be timezone-aware")
    return SinaStatementPage(
        stock_code=stock_code, statement=statement, periods=periods, rows=rows,
        content_hash="sha256:" + hashlib.sha256(raw).hexdigest(),
        retrieved_at=observed,
    )


def _row(page: SinaStatementPage, *aliases: str) -> tuple[Decimal | None, ...]:
    for alias in aliases:
        value = page.rows.get(_clean(alias))
        if value is not None:
            return value
    raise SinaFinancialError(
        f"{page.statement} page missing required row: {' / '.join(aliases)}")


def merge_s2_pages(*, profit: SinaStatementPage,
                   balance: SinaStatementPage,
                   cashflow: SinaStatementPage) -> tuple[SinaS2Row, ...]:
    """按共同报告期合并三张新浪页面，但保持“不可用于 PIT”的事实。"""

    pages = (profit, balance, cashflow)
    if {page.stock_code for page in pages} != {profit.stock_code}:
        raise SinaFinancialError("cross-instrument statement merge is forbidden")
    if (profit.statement, balance.statement, cashflow.statement) != (
        "profit", "balance", "cashflow",
    ):
        raise SinaFinancialError("profit/balance/cashflow pages are required")

    revenue = _row(profit, "营业收入")
    net_income = _row(profit, "净利润", "五、净利润")
    attributable = _row(profit, "归属于母公司所有者的净利润")
    equity = _row(balance, "归属于母公司股东权益合计", "归属于母公司股东权益")
    operating_cf = _row(cashflow, "经营活动产生的现金流量净额")
    lookups = [dict(zip(page.periods, values)) for page, values in (
        (profit, revenue), (profit, net_income), (profit, attributable),
        (balance, equity), (cashflow, operating_cf),
    )]
    common = set(profit.periods) & set(balance.periods) & set(cashflow.periods)
    observed = max(page.retrieved_at for page in pages)
    return tuple(
        SinaS2Row(
            stock_code=profit.stock_code, end_date=period,
            revenue=lookups[0][period], net_income=lookups[1][period],
            net_income_attributable=lookups[2][period],
            parent_equity=lookups[3][period], operating_cashflow=lookups[4][period],
            source_id=SOURCE_ID, retrieved_at=observed,
            content_hashes=tuple(page.content_hash for page in pages),
        )
        for period in sorted(common, reverse=True)
    )


class SinaFinancialClient:
    """显式联网客户端；构造与导入本模块都不会自动发请求。"""

    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        self.timeout_seconds = timeout_seconds

    def fetch_page(self, stock_code: str, statement: str) -> SinaStatementPage:
        path = _PAGE_PATHS.get(statement)
        if path is None:
            raise SinaFinancialError(f"unknown statement {statement!r}")
        url = ("https://vip.stock.finance.sina.com.cn/corp/go.php/"
               f"{path}/stockid/{stock_code}/ctrl/part/displaytype/4.phtml")
        request = Request(url, headers={
            "User-Agent": "AQuant-Lab/0.1 local provider probe",
            "Referer": "https://finance.sina.com.cn/",
        })
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read()
        except Exception as exc:  # noqa: BLE001 - preserve provider failure
            raise SinaFinancialError(
                f"failed to fetch {statement} for {stock_code}: {type(exc).__name__}") from exc
        return parse_statement_page(payload, stock_code=stock_code,
                                    statement=statement)

    def fetch_s2_rows(self, stock_code: str) -> tuple[SinaS2Row, ...]:
        return merge_s2_pages(
            profit=self.fetch_page(stock_code, "profit"),
            balance=self.fetch_page(stock_code, "balance"),
            cashflow=self.fetch_page(stock_code, "cashflow"),
        )
