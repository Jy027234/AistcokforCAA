"""东方财富结构化财报适配器（研究/内部用途）。

**为什么需要这条路。** 逐份解析官方 PDF 是"证据级"路径，但它的边际成本是
"新增一家公司 ≈ 若干轮版式适配"——实测 40 轮只覆盖 8 只。这个斜率到不了
全市场 5400 只。方案 ``docs/data-requirement-matrix.md`` 本来就规定 S2 走
"交易所/XBRL + 巨潮时点与修订"，PDF 只是证据层；本模块把主数据源换回结构化
接口，PDF 与机器核验降级为**抽样质检**。

**它取什么。** 三张报表各一次请求，覆盖五个必需字段：

===================  ==========================================
``revenue``          利润表 ``OPERATE_INCOME``
``net_profit_attributable``  利润表 ``PARENT_NETPROFIT``
``net_profit_consolidated``  利润表 归母 + ``MINORITY_INTEREST``
``parent_equity``    资产负债表 ``TOTAL_PARENT_EQUITY``
``operating_cashflow``  现金流量表 ``NETCASH_OPERATE``
===================  ==========================================

**可信度不是"因为接口说的"。** 实测用本适配器取值与**已用官方 PDF 独立核验
并入库**的 23 个值逐值对拍，**23/23 分文不差**（000651 三期、600519 两期）。
两个来源相互独立，这正是可复核的交叉证据。

**边界（必须如实保留）：**

* 接口返回的是**最新报告值**。它**不含**"何时披露、有无更正"——那部分靠巨潮
  公告索引（本仓已有），因此结构化值必须与公告时点/修订证据绑定后才算 PIT 可用；
* 接口**不是每次都通**（实测今日连通，同族的东财行情接口此前失败过），因此
  调用方必须按"一份报告一个来源"处理失败与备源，不能逐字段静默拼源；
* 每次响应都**归档 + 记收据**，所以任何一条值都能回溯到原始响应字节。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from aquant.adapters.providers.cninfo import RateLimiter
from aquant.domain.data.forward_archive import ForwardArchive

SOURCE_ID = "eastmoney-structured-f10"
HOST = "datacenter.eastmoney.com"
BASE = f"https://{HOST}/securities/api/data/get"

#: 三张表的报表名与它们承载的字段。
BALANCE_SHEET = "RPT_F10_FINANCE_GBALANCE"
INCOME_STATEMENT = "RPT_F10_FINANCE_GINCOME"
CASH_FLOW_STATEMENT = "RPT_F10_FINANCE_GCASHFLOW"

#: 报表**族**。巨潮/东财按企业类型分四套定期报告，实测命名规律是
#: ``RPT_F10_FINANCE_<族><表>``：
#:
#: ==========  ======  ==========================================
#: 族         前缀    实测可用
#: ==========  ======  ==========================================
#: 一般企业    ``G``   GBALANCE / GINCOME / GCASHFLOW
#: 银行        ``B``   BBALANCE / BINCOME / BCASHFLOW
#: 保险        ``I``   IBALANCE / IINCOME / ICASHFLOW
#: 证券        ``S``   SBALANCE / SINCOME / SCASHFLOW
#: ==========  ======  ==========================================
#:
#: **五个必需字段在四族里字段名完全相同**（实测 000001 银行、601318 保险、
#: 600030 证券三家的 ``TOTAL_PARENT_EQUITY``/``OPERATE_INCOME``/
#: ``PARENT_NETPROFIT``/``NETPROFIT``/``NETCASH_OPERATE`` 全部命中），
#: 所以切换族**不需要改字段映射**，只需要选对族。
#:
#: 早期只发 ``G*``，于是 705 只金融企业全部被判"模板不适用"而整只丢弃。
FAMILY_LABELS: dict[str, str] = {
    "G": "general", "B": "bank", "I": "insurance", "S": "securities",
}
#: 探测顺序。``G`` 优先——它在多数证券上直接命中，金融企业才需要往下走。
FAMILY_ORDER: tuple[str, ...] = ("G", "B", "I", "S")

#: 表的种类 → 报表名后缀。
_KINDS: dict[str, str] = {
    "balance": "BALANCE", "income": "INCOME", "cashflow": "CASHFLOW",
}

#: 五个 S2 必需字段 → (表种类, 取值字段)。``NETPROFIT`` 有专门的取值分支。
FIELD_SOURCES: dict[str, tuple[str, str]] = {
    "revenue": ("income", "OPERATE_INCOME"),
    "net_profit_attributable": ("income", "PARENT_NETPROFIT"),
    "net_profit_consolidated": ("income", "NETPROFIT"),
    "parent_equity": ("balance", "TOTAL_PARENT_EQUITY"),
    "operating_cashflow": ("cashflow", "NETCASH_OPERATE"),
}

#: 单位：接口返回的是**元**（实测 000651 营业收入 89,397,522,501.21 与官方
#: PDF 的元值一致）。保留成常量而不是散落在各处，便于将来某源改口径时一处改。
VALUE_UNIT = "yuan"

#: 每次请求之间的最小间隔。接口没有公开限速说明，取保守值避免被拒。
MIN_INTERVAL_SECONDS = 0.8


class EastmoneyError(RuntimeError):
    """结构化接口不可用或返回不可解析内容。"""


class FinancialTemplateUnsupported(EastmoneyError):
    """该证券用**金融企业**报表模板，本接口的一般企业模板取不到数据。

    实测 000001（平安银行）三张表全部返回"返回数据为空"。银行/保险/券商用
    另一套会计科目（如"发放贷款及垫款""客户存款"），本仓既有的 S2 口径本就
    把它们作为**独立模板**排除（覆盖报告里的 ``financial_industry_excluded``）。
    把它单独成类，是为了让批量结果里"金融企业不适用"与"接口真的坏了"
    分开计数——两者的处置完全不同。
    """


def secucode(instrument_id: str) -> str:
    """六位证券代码 → 东财 ``SECUCODE``（代码 + 交易所后缀）。

    后缀**不是**只按首位判断就能定完，实测归档/响应得出的映射表：

    ============  ==========  ============================================
    代码段         后缀        实测
    ============  ==========  ============================================
    6xxxxx / 9xxxxx（非 92） ``.SH``  沪市主板、科创板
    0xxxxx / 2xxxxx / 3xxxxx ``.SZ``  深市主板、创业板
    4xxxxx / 8xxxxx          ``.NQ``  北交所（原新三板 43/83/87/88 段）
    **92xxxx**               ``.BJ``  **北交所新股段，与上面相反**
    ============  ==========  ============================================

    **两个坑都踩过，且都是静默失效**（返回"返回数据为空"，与"该公司没有报表"
    无法区分）：

    * 早期把 4/8 段写成 ``.BJ``，实测 ``430017.BJ`` 与 ``430017.SZ`` 都空，
      只有 ``430017.NQ`` 有数据 → **251 只**取不到数；
    * 早期把 9 段一律当沪市，而 ``920002.SH``/``920002.NQ`` 都空，
      只有 ``920002.BJ`` 有数据（万达轴承）→ 又 **353 只**取不到数。

    合计 **604 只**。因此这里按**代码段**而不是按首位判断。
    """

    code = instrument_id.strip()
    if len(code) != 6 or not code.isdigit():
        raise EastmoneyError(f"instrument id must be six digits, got {instrument_id!r}")
    if code.startswith("92"):
        # 北交所新股段：与 43/83/87/88 段相反，用 .BJ。
        return f"{code}.BJ"
    if code[0] in "69":
        return f"{code}.SH"
    if code[0] in "023":
        return f"{code}.SZ"
    if code[0] in "48":
        return f"{code}.NQ"
    raise EastmoneyError(f"cannot determine exchange for {instrument_id!r}")


@dataclass(frozen=True, slots=True)
class StatementTable:
    """一张报表的全部期间记录，附原始响应证据。"""

    report: str
    secucode: str
    records: tuple[dict[str, Any], ...]
    receipt_id: str
    content_hash: str
    url: str

    def for_date(self, period_end: str) -> dict[str, Any] | None:
        for row in self.records:
            if str(row.get("REPORT_DATE") or "")[:10] == period_end:
                return row
        return None

    def period_ends(self) -> tuple[str, ...]:
        return tuple(sorted(
            {str(row.get("REPORT_DATE") or "")[:10] for row in self.records
             if row.get("REPORT_DATE")}, reverse=True))


@dataclass(frozen=True, slots=True)
class StructuredPeriod:
    """一个证券一个期间的五个字段，附每个字段的来源报表与响应证据。"""

    instrument_id: str
    period_end: str
    values: dict[str, Decimal]
    missing: tuple[str, ...]
    evidence: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not self.missing


class EastmoneyStructuredClient:
    """带限速、归档与收据的结构化财报客户端。"""

    def __init__(self, archive: ForwardArchive, *,
                 limiter: RateLimiter | None = None,
                 timeout: float = 30.0, page_size: int = 60) -> None:
        self.archive = archive
        self.limiter = limiter or RateLimiter(min_interval_seconds=MIN_INTERVAL_SECONDS)
        self.timeout = timeout
        self.page_size = page_size

    def _url(self, report: str, secucode_value: str) -> str:
        # 参数顺序固定，保证同一请求产生同一 URL（收据可比对）。
        query = (f"type={report}&sty=ALL"
                 f"&filter=(SECUCODE=%22{secucode_value}%22)"
                 f"&p=1&ps={self.page_size}&sr=-1&st=REPORT_DATE")
        return f"{BASE}?{query}"

    def fetch_table(self, report: str, secucode_value: str) -> StatementTable:
        """取一张报表的全部期间记录；失败即抛错，不返回部分结果。"""

        url = self._url(report, secucode_value)
        self.limiter.wait(HOST)
        request = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://data.eastmoney.com/",
        })
        requested_at = datetime.now(timezone.utc)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
                status = getattr(response, "status", 200)
                responded_at = datetime.now(timezone.utc)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            self.archive.record(
                source_id=SOURCE_ID, url=url, outcome="TRANSPORT_ERROR",
                requested_at=requested_at, responded_at=datetime.now(timezone.utc),
                detail=f"{report}/{secucode_value}: {type(exc).__name__}: {exc}"[:400])
            raise EastmoneyError(
                f"{report} for {secucode_value} failed: {type(exc).__name__}") from exc

        content_hash, _ = self.archive.store_bytes(payload, media_type="application/json")
        receipt = self.archive.record(
            source_id=SOURCE_ID, url=url, outcome="OK",
            requested_at=requested_at, responded_at=responded_at,
            http_status=status, content_hash=content_hash, byte_size=len(payload),
            detail=f"{report} for {secucode_value}")
        try:
            document = json.loads(payload)
        except ValueError as exc:
            raise EastmoneyError(f"{report} for {secucode_value} is not JSON") from exc
        if not isinstance(document, dict) or document.get("success") is False:
            message = str(document.get("message"))
            if "为空" in message or "no data" in message.lower():
                # 一般企业模板取不到记录：金融企业用另一套科目。
                # 归为"模板不适用"而不是"来源故障"。
                raise FinancialTemplateUnsupported(
                    f"{report} for {secucode_value} has no records: {message[:80]}")
            raise EastmoneyError(
                f"{report} for {secucode_value} was rejected: {message[:120]}")
        result = document.get("result") or {}
        records = result.get("data") or []
        if not isinstance(records, list):
            raise EastmoneyError(f"{report} for {secucode_value} has no record list")
        return StatementTable(
            report=report, secucode=secucode_value,
            records=tuple(row for row in records if isinstance(row, dict)),
            receipt_id=receipt.receipt_id, content_hash=content_hash, url=url)

    @staticmethod
    def _number(row: dict[str, Any], key: str) -> Decimal | None:
        """取一个数值字段；缺失/非数值一律返回 ``None``，**不用 0 顶替**。"""

        if key == "NETPROFIT":
            # 合并净利润：**优先直取** ``NETPROFIT``（实测该字段在多数公司上
            # 都有值，且等于"归母 + 少数股东"）。直取缺失时才退回相加——
            # 早期一上来就用相加，而 ``MINORITY_INTEREST`` 在很多期为空，
            # 结果全池 504 个期间被判成"缺字段"，其实数据是有的。
            direct = row.get("NETPROFIT")
            if direct is not None and direct != "":
                try:
                    return Decimal(str(direct))
                except (ArithmeticError, ValueError):
                    pass
            parent = row.get("PARENT_NETPROFIT")
            minority = row.get("MINORITY_INTEREST")
            if parent is None or minority is None:
                return None
            return Decimal(str(parent)) + Decimal(str(minority))
        raw = row.get(key)
        if raw is None or raw == "":
            return None
        try:
            return Decimal(str(raw))
        except (ArithmeticError, ValueError):
            return None

    def tables(self, instrument_id: str) -> tuple[dict[str, StatementTable], str]:
        """取该证券的三张表，并返回实际命中的**报表族**。

        **每张表每只只请求一次**：一次响应里已经包含全部报告期，因此多期取值
        必须复用这三张表。早期 ``period()`` 每期都重新拉三张表，取 3 期就是
        9 次请求——请求数乘了期数，全市场产能直接掉三分之二。

        族的选法：从 ``G`` 起逐个探测**资产负债表**，第一个有数据的族即为该
        证券的类型，随后三张表都用这个族。金融企业（银行/保险/证券）用各自的
        科目表，``G*`` 对它们返回"返回数据为空"——早期因此把 705 只金融企业
        整只丢掉。
        """

        code = secucode(instrument_id)
        last: FinancialTemplateUnsupported | None = None
        for family in FAMILY_ORDER:
            try:
                balance = self.fetch_table(
                    f"RPT_F10_FINANCE_{family}{_KINDS['balance']}", code)
            except FinancialTemplateUnsupported as exc:
                last = exc
                continue
            tables = {"balance": balance}
            for kind in ("income", "cashflow"):
                tables[kind] = self.fetch_table(
                    f"RPT_F10_FINANCE_{family}{_KINDS[kind]}", code)
            return tables, family
        raise FinancialTemplateUnsupported(
            f"{instrument_id} has no data in any report family "
            f"{'/'.join(FAMILY_ORDER)} (last: {last})")

    def period_from_tables(self, instrument_id: str, period_end: str,
                           tables: dict[str, StatementTable],
                           family: str | None = None) -> StructuredPeriod:
        """从**已取回**的表格里取出一个期间的五个字段。"""

        values: dict[str, Decimal] = {}
        missing: list[str] = []
        evidence: dict[str, dict[str, str]] = {}
        for name, (kind, key) in FIELD_SOURCES.items():
            table = tables.get(kind)
            row = None if table is None else table.for_date(period_end)
            value = None if row is None else self._number(row, key)
            if value is None:
                missing.append(name)
                continue
            values[name] = value
            evidence[name] = {
                "report": table.report, "field": key, "kind": kind,
                "receipt_id": table.receipt_id,
                "content_hash": table.content_hash, "url": table.url,
                "unit": VALUE_UNIT,
            }
            if family:
                evidence[name]["family"] = family
        return StructuredPeriod(
            instrument_id=instrument_id, period_end=period_end,
            values=values, missing=tuple(sorted(missing)), evidence=evidence)

    def period(self, instrument_id: str, period_end: str) -> StructuredPeriod:
        """取一个证券一个期间的五个字段（便捷入口，会重新请求三张表）。

        需要多个期间时请用 ``tables()`` + ``period_from_tables()``，
        否则请求数会乘上期数。
        """

        tables, family = self.tables(instrument_id)
        return self.period_from_tables(instrument_id, period_end, tables, family)
