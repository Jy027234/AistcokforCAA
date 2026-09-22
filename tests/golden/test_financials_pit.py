"""T11：财务 PIT 可用性与 F10 的验收（ADR-005）。

重点是**拒绝路径**，不是计算路径。
一个能算出收益率的 F10 并不难写；难的是保证它在任何时点都只用
"当时已经可见"的财报。后者错了不会报错，只会让回测看起来更赚钱。
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.fundamentals.pit import (  # noqa: E402
    CST, FinancialStatement, FinancialsStore, FinancialsUnavailable,
    available_at, earning_yield_f10,
)

#: 一段真实交易日历（含周末与一个长假）。
#: 用真实日历而不是工作日近似：长假前后"下一个交易日"会差好几天，
#: 而错误方向恰好是"提前可用"。
TRADING_DAYS = [
    date(2026, 4, 24), date(2026, 4, 27), date(2026, 4, 28),
    date(2026, 4, 29), date(2026, 4, 30),
    # 五一假期：5-01 ~ 5-05 休市，5-06 复市
    date(2026, 5, 6), date(2026, 5, 7), date(2026, 5, 8),
    date(2026, 8, 13), date(2026, 8, 14),
    date(2026, 8, 17), date(2026, 8, 18),   # 8-15 是周六
    date(2026, 10, 29), date(2026, 10, 30), date(2026, 11, 2),
]

MICROS = 1_000_000


def st(stat: str, pub: str, profit_yuan: float, *, shares: str = "1000000000",
       instrument: str = "SH.600519") -> FinancialStatement:
    return FinancialStatement(
        instrument_id=instrument,
        stat_date=date.fromisoformat(stat),
        pub_date=date.fromisoformat(pub),
        net_profit_micros=int(profit_yuan * MICROS),
        revenue_micros=None,
        roe_avg=Decimal("0.1"),
        eps_ttm_micros=None,
        cfo_to_np=None,
        total_share=Decimal(shares),
        source_id="baostock",
    )


def store(statements: list[FinancialStatement]) -> FinancialsStore:
    return FinancialsStore(statements=statements, trading_days=TRADING_DAYS)


# ==================================================== 可用时点推导
def test_available_at_is_the_next_trading_day_preopen():
    # 2026-08-13 是交易日，公布日 08-13 -> 下一交易日 08-14 盘前
    got = available_at(date(2026, 8, 13), TRADING_DAYS)
    assert got.date() == date(2026, 8, 14)
    assert got.hour == 9
    assert str(got.tzinfo) == "UTC+08:00"


def test_published_on_a_trading_day_is_not_known_same_day():
    """D 日公布的财报，不得算作 D 日已知。

    这是整个规则的核心：公告可能在盘中或盘后发布，
    pubDate 不提供时刻，因此当日已知无法证明。
    """

    pub = date(2026, 8, 14)          # 交易日
    got = available_at(pub, TRADING_DAYS)
    assert got.date() > pub, f"可得日 {got.date()} 不得早于等于公布日 {pub}"
    assert got.date() == date(2026, 8, 17)   # 8-15 周六、8-16 周日


def test_weekend_publication_rolls_to_monday():
    # 2026-08-15 是周六 -> 下一交易日 08-17（周一）
    assert available_at(date(2026, 8, 15), TRADING_DAYS).date() == date(2026, 8, 17)


def test_long_holiday_publication_skips_the_whole_break():
    """五一休市：4-30 公布的财报要到 5-06 才可用。

    用工作日近似会算成 5-01（周五），凭空提前 5 天。
    """

    got = available_at(date(2026, 4, 30), TRADING_DAYS)
    assert got.date() == date(2026, 5, 6), got


def test_calendar_without_a_later_trading_day_is_rejected():
    """日历覆盖不到时**报错**，不得猜一个日期。"""

    with pytest.raises(FinancialsUnavailable) as exc:
        available_at(date(2026, 12, 1), TRADING_DAYS)
    assert "extend the calendar" in str(exc.value)


def test_empty_calendar_is_rejected():
    with pytest.raises(FinancialsUnavailable):
        FinancialsStore(statements=[], trading_days=[])


# ============================================ 拒绝路径：未来财报不可见
def test_statement_published_after_as_of_is_invisible():
    """用未来才公布的财报回答过去的决策，必须看不到它。"""

    s = store([st("2026-06-30", "2026-08-13", 4.6e10)])
    before = datetime(2026, 8, 13, 10, 0, tzinfo=CST)   # 公布当日的盘中
    after = datetime(2026, 8, 14, 9, 30, tzinfo=CST)    # 次一交易日盘前之后

    assert s.available_statements("SH.600519", as_of=before) == []
    assert len(s.available_statements("SH.600519", as_of=after)) == 1
    assert s.trailing_twelve_months("SH.600519", as_of=before) is None


def test_no_statement_is_visible_at_the_preopen_of_its_own_publication_day():
    """公布日当天盘前**看不到**当天要公布的财报。"""

    s = store([st("2026-06-30", "2026-08-13", 4.6e10)])
    same_day_preopen = datetime(2026, 8, 13, 9, 0, tzinfo=CST)
    assert s.available_statements("SH.600519", as_of=same_day_preopen) == []


# ================================================ TTM 滚动与口径
def test_ttm_is_the_rolling_twelve_months():
    """TTM = 上一年完整年度 + 当期累计 − 上年同期累计。

    两个常见错法都会在这里失败：
      * "四个季累加"把年内累计值当单季值，得 1.0+2.0+3.0+4.0 = 10e10；
      * "当期累计 − 上年同期"漏掉上一完整年度，得 2.4 − 2.0 = 0.4e10。
    """

    rows = [
        st("2025-03-31", "2025-04-30", 1.0e10),
        st("2025-06-30", "2025-08-13", 2.0e10),
        st("2025-09-30", "2025-10-30", 3.0e10),
        st("2025-12-31", "2026-04-17", 4.0e10),
        st("2026-03-31", "2026-04-25", 1.2e10),
        st("2026-06-30", "2026-08-13", 2.4e10),
    ]
    s = store(rows)
    as_of = datetime(2026, 8, 14, 9, 30, tzinfo=CST)

    ttm = s.trailing_twelve_months("SH.600519", as_of=as_of)
    assert ttm is not None
    # 4.0e10 + 2.4e10 − 2.0e10 = 4.4e10
    assert ttm["ttm_net_profit_micros"] == int(4.4e10 * MICROS), ttm
    assert ttm["ttm_net_profit_micros"] != int(10.0e10 * MICROS), "不得四季累加"
    assert ttm["ttm_net_profit_micros"] != int(0.4e10 * MICROS), "不得漏掉上年年报"
    assert "12 个月" in ttm["ttm_basis"], ttm["ttm_basis"]


def test_ttm_for_q1_needs_the_prior_q1():
    """Q1 的 TTM 需要**上年 Q1 累计**，不能假定为 0。

    我第一版按"上年同期区间起点是年初，故取 0"处理，理由是错的：
    当期累计覆盖"年初到 3-31"，上年同期累计覆盖"上年年初到上年 3-31"，
    还要接上上一完整年度。取 0 等于把上年一季度也算进来，
    同时漏掉上一完整年度，结果不是 TTM。
    """

    rows = [
        st("2025-03-31", "2025-04-30", 1.0e10),
        st("2025-12-31", "2026-04-17", 4.0e10),
        st("2026-03-31", "2026-04-25", 1.2e10),
    ]
    s = store(rows)
    ttm = s.trailing_twelve_months(
        "SH.600519", as_of=datetime(2026, 4, 27, 9, 30, tzinfo=CST))
    assert ttm is not None
    # 4.0e10 + 1.2e10 − 1.0e10 = 4.2e10
    assert ttm["ttm_net_profit_micros"] == int(4.2e10 * MICROS), ttm

    # 缺上年 Q1 时必须拒绝，不得假定为 0
    s2 = store([st("2025-12-31", "2026-04-17", 4.0e10),
                st("2026-03-31", "2026-04-25", 1.2e10)])
    assert s2.trailing_twelve_months(
        "SH.600519", as_of=datetime(2026, 4, 27, 9, 30, tzinfo=CST)) is None


def test_ttm_is_none_when_prior_annual_is_invisible():
    """缺上年年报时返回 None，**不得**退回季累加凑一个数。"""

    s = store([
        st("2025-06-30", "2025-08-13", 2.0e10),
        # 年报即使存在，若在 as_of 之后公布也不能用于 TTM。
        st("2025-12-31", "2026-10-29", 4.0e10),
        st("2026-06-30", "2026-08-13", 2.4e10),
    ])
    ttm = s.trailing_twelve_months(
        "SH.600519", as_of=datetime(2026, 8, 14, 9, 30, tzinfo=CST))
    assert ttm is None


def test_ttm_allows_later_quarter_loss_to_reduce_cumulative_value():
    """后续季度亏损造成累计下降时，仍按完整 TTM 公式计算。"""

    rows = [
        st("2025-06-30", "2025-08-13", 2.0e10),
        st("2025-12-31", "2026-04-17", 4.0e10),
        st("2026-03-31", "2026-04-25", 10.0e10),
        # Q2 单季亏损 2.0e10，年内累计从 10.0e10 降到 8.0e10。
        st("2026-06-30", "2026-08-13", 8.0e10),
    ]
    s = store(rows)
    ttm = s.trailing_twelve_months(
        "SH.600519", as_of=datetime(2026, 8, 14, 9, 30, tzinfo=CST))
    assert ttm is not None
    # 4.0e10 + 8.0e10 − 2.0e10 = 10.0e10
    assert ttm["ttm_net_profit_micros"] == int(10.0e10 * MICROS), ttm


def test_q4_is_the_full_year_not_a_single_quarter():
    rows = [st("2025-12-31", "2026-04-17", 8.5e10)]
    s = store(rows)
    ttm = s.trailing_twelve_months(
        "SH.600519", as_of=datetime(2026, 4, 24, 9, 30, tzinfo=CST))
    assert ttm is not None
    assert ttm["ttm_net_profit_micros"] == int(8.5e10 * MICROS)
    assert "完整 12 个月" in ttm["ttm_basis"], ttm["ttm_basis"]


# ======================================================== F10 计算
def test_f10_is_profit_over_market_cap():
    # 净利润 4.4 亿元，股本 10 亿股，收盘 44 元 -> 市值 440 亿 -> 1%
    value = earning_yield_f10(ttm_net_profit_micros=int(4.4e8 * MICROS),
                              total_share=Decimal("1000000000"),
                              close_cents=4400)
    assert value == Decimal("0.010000"), value


def test_f10_keeps_the_sign_for_losses():
    """亏损必须保留负号，不得截断为 0。

    截断会把"亏损股"显示成"收益率 0"，与"低估值"无法区分，
    而 §10.1 的 F10 启用条件明确要求"负利润保留符号"。
    """

    value = earning_yield_f10(ttm_net_profit_micros=int(-4.4e8 * MICROS),
                              total_share=Decimal("1000000000"),
                              close_cents=4400)
    assert value is not None and value < 0, value
    assert value == Decimal("-0.010000")


def test_f10_is_none_without_shares_or_price():
    assert earning_yield_f10(ttm_net_profit_micros=1, total_share=None,
                             close_cents=100) is None
    assert earning_yield_f10(ttm_net_profit_micros=1,
                             total_share=Decimal("0"), close_cents=100) is None
    assert earning_yield_f10(ttm_net_profit_micros=1,
                             total_share=Decimal("1000"), close_cents=0) is None
