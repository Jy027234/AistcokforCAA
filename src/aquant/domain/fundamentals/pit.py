"""财务数据的时点（PIT）可用性规则（§7.2、§7.4，ADR-005）。

这一层存在的唯一理由
--------------------
财报有**两个日期**，混淆它们会直接产生未来信息泄漏：

  * statDate（报告期）：数据描述的那段时间的截止日；
  * pubDate（公布日）：市场**实际能看到**这条数据的日期。

用 statDate 做决策就是以未来信息交易——2026-06-30 的财报要到
2026-08-15 才公布，中间一个半月市场并不知道它。
而这类错误在回测里只会表现为"策略很赚钱"，不会报错。

可用性规则（ADR-005）
--------------------
pubDate 精确到**日**、不提供时刻，因此无法证明当日盘中已知。
按 D04 已确立的保守规则：

    可得时点 = 公布日之后**第一个交易日**的盘前（09:00 CST）

取"严格之后"而不是"当日"：把 D 日公布的财报算作 D 日盘前已知，
在公告于盘中或盘后发布时会变成未来信息。宁可晚一天，不可早一天。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

#: 盘前时刻（Asia/Shanghai）。可得时点定在这一刻，
#: 意味着"开盘前就已经知道"，因此当日交易可以使用它。
PREOPEN_HOUR_CST = 9
CST = timezone(timedelta(hours=8))

#: 依据类型。与 §7.2 的 available_basis 枚举一致。
BASIS_RECONSTRUCTED = "RECONSTRUCTED"


class FinancialsUnavailable(RuntimeError):
    """财报数据缺失或不可用于该时点。调用方必须显式处理。"""


@dataclass(frozen=True, slots=True)
class FinancialStatement:
    """一条季频财务记录。**金额单位是整数微元**（与分红一致）。"""

    instrument_id: str
    stat_date: date            # 报告期截止日
    pub_date: date             # 公布日（市场能看到的最早日期）
    net_profit_micros: int | None
    revenue_micros: int | None
    roe_avg: Decimal | None
    eps_ttm_micros: int | None
    cfo_to_np: Decimal | None
    total_share: Decimal | None
    source_id: str

    @property
    def period_key(self) -> tuple[int, int]:
        """(年, 季)。用于按报告期索引与 TTM 滚动。"""

        return (self.stat_date.year, (self.stat_date.month - 1) // 3 + 1)


def available_at(pub_date: date, trading_days: list[date]) -> datetime:
    """由公布日推导可得时点：公布日之后第一个交易日的盘前。

    trading_days 必须是**已证实的交易日**（来自权威日历），
    不能是工作日近似——用工作日近似会在长假前后算出错误的可得时点，
    而错误方向恰好是"提前可用"。
    """

    for day in sorted(set(trading_days)):
        if day > pub_date:
            return datetime.combine(day, time(PREOPEN_HOUR_CST), tzinfo=CST)
    raise FinancialsUnavailable(
        f"no trading day after {pub_date} in the provided calendar; "
        "extend the calendar rather than guessing the next trading day")


class FinancialsStore:
    """某个快照内的财务数据，并强制 PIT 可用性。

    **所有读取都必须经由 available_statements / trailing_twelve_months**，
    它们会按时点过滤。刻意不提供"取最新一条"这样的接口：
    那种接口的默认语义就是"用最新的"，也就是未来信息。
    """

    def __init__(self, *, statements: list[FinancialStatement],
                 trading_days: list[date]) -> None:
        if not trading_days:
            raise FinancialsUnavailable(
                "trading calendar is empty; PIT availability cannot be derived")
        self._days = sorted(set(trading_days))
        self._available: dict[tuple[str, str, str], datetime] = {}
        self._by_instrument: dict[str, list[FinancialStatement]] = {}

        for st in statements:
            self._by_instrument.setdefault(st.instrument_id, []).append(st)
            self._available[self._key(st)] = available_at(st.pub_date, self._days)

        for rows in self._by_instrument.values():
            rows.sort(key=lambda s: (s.stat_date, s.pub_date))

    @staticmethod
    def _key(st: FinancialStatement) -> tuple[str, str, str]:
        return (st.instrument_id, st.stat_date.isoformat(), st.pub_date.isoformat())

    def availability_of(self, st: FinancialStatement) -> datetime:
        return self._available[self._key(st)]

    def available_statements(self, instrument_id: str, *,
                             as_of: datetime) -> list[FinancialStatement]:
        """截至 as_of **已经可见**的财报，按报告期升序。"""

        rows = self._by_instrument.get(instrument_id, [])
        return [s for s in rows if self.availability_of(s) <= as_of]

    def trailing_twelve_months(self, instrument_id: str, *,
                               as_of: datetime) -> dict | None:
        """按 TTM 规则取最近四个季度，返回推导过程供复核。

        口径（T9 实测）：BaoStock 的 netProfit 是**年内累计**，
        因此某期累计值 = 该年期初到该期末。TTM 用规范 §10.1 的规则：

            最近完整年度值 + 当年累计值 − 上年同期累计值

        Q1 的"上年同期累计区间"起点是年初，故该项为 0。
        """

        rows = self.available_statements(instrument_id, as_of=as_of)
        if not rows:
            return None
        latest = rows[-1]
        year, quarter = latest.period_key

        by_period: dict[tuple[int, int], FinancialStatement] = {}
        for s in rows:
            by_period[s.period_key] = s

        if quarter == 4:
            ttm = latest.net_profit_micros
            basis = f"{year}Q4 即全年"
        else:
            prev_annual = by_period.get((year - 1, 4))
            if prev_annual is None or latest.net_profit_micros is None \
                    or prev_annual.net_profit_micros is None:
                return None
            if quarter == 1:
                prev_same_value = 0
                same_note = "Q1 的同期区间起点为年初，取 0"
            else:
                prev_same = by_period.get((year - 1, quarter))
                if prev_same is None or prev_same.net_profit_micros is None:
                    return None
                prev_same_value = prev_same.net_profit_micros
                same_note = f"扣除 {year - 1}Q{quarter} 累计"
            ttm = (latest.net_profit_micros + prev_annual.net_profit_micros
                   - prev_same_value)
            basis = (f"{year}Q{quarter} 累计 + {year - 1} 年报 − "
                     f"{year - 1}Q{quarter} 累计（{same_note}）")

        return {
            "instrument_id": instrument_id,
            "as_of": as_of.isoformat(),
            "latest_stat_date": latest.stat_date.isoformat(),
            "latest_pub_date": latest.pub_date.isoformat(),
            "latest_available_at": self.availability_of(latest).isoformat(),
            "ttm_net_profit_micros": ttm,
            "ttm_basis": basis,
            "total_share": (str(latest.total_share)
                            if latest.total_share is not None else None),
            "eps_ttm_micros": latest.eps_ttm_micros,
        }


def earning_yield_f10(*, ttm_net_profit_micros: int, total_share: Decimal | None,
                      close_cents: int) -> Decimal | None:
    """F10 盈利收益率 = 归母净利润TTM / 时点总市值。

    市值 = 总股本 × 时点收盘价。两个输入都必须来自**同一时点**：
    用后来才知道的股本去除以前的利润，会得到一个当时算不出来的收益率。

    负利润**保留符号**，不取绝对值也不截断为 0（§10.1 F10 的启用条件），
    否则"亏损股"会被伪装成"低估值"。
    """

    if total_share is None or total_share <= 0 or close_cents <= 0:
        return None
    # 市值（微元）= 总股本 × 收盘价(分) × 10^4（1 分 = 10^4 微元）
    market_cap_micros = total_share * Decimal(close_cents) * Decimal(10_000)
    if market_cap_micros <= 0:
        return None
    return (Decimal(ttm_net_profit_micros) / market_cap_micros).quantize(
        Decimal("0.000001"))