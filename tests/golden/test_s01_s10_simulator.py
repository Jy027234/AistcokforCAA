"""S01–S10 模拟与账本黄金用例（主文档 §18.2）。

这些用例是发布门槛的一部分：§18.4 要求"所有资金和 PIT 黄金测试通过"。
每条断言都对应主文档 §18.2 表中"必须结果"一列。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from aquant.domain.simulation.fees import synthetic_fee_table
from aquant.domain.simulation.simulator import (
    Bar,
    BoardRule,
    CashEntry,
    DailySimulator,
    Lot,
    Order,
    OrderStatus,
    Side,
    SimError,
    check_invariants,
)

DAY = date(2026, 9, 11)
NEXT_DAY = date(2026, 9, 14)   # 次一交易日

#: 显式的上市信息。绝不从代码前缀猜。
LISTINGS = {
    "SYN.A.600519": ("SSE", "MAIN"),
    "SYN.A.000001": ("SZSE", "MAIN"),
    "SYN.A.600003": ("SSE", "MAIN"),
}

RULES = [
    BoardRule(exchange="SSE", board="MAIN", price_limit_pct=Decimal("10"),
              lot_size=100, effective_from=date(2026, 7, 6)),
    BoardRule(exchange="SZSE", board="MAIN", price_limit_pct=Decimal("10"),
              lot_size=100, effective_from=date(2026, 7, 6)),
]


def sim(**kw):
    base = dict(fee_table=synthetic_fee_table(), board_rules=RULES, listings=LISTINGS)
    base.update(kw)
    return DailySimulator(**base)


def bar(iid="SYN.A.600519", *, open_cents=10000, prev_close=9900,
        limit_up=False, day=DAY, volume=10_000_000):
    return Bar(instrument_id=iid, trading_day=day, open_cents=open_cents,
               high_cents=open_cents, low_cents=open_cents, close_cents=open_cents,
               prev_close_cents=prev_close, volume_shares=volume,
               board_limit_up=limit_up)


def lot(iid="SYN.A.600519", qty=1000, acquired=None, sellable=None, cost=10000):
    # 默认产出**已可卖**的批次（昨日买入），因为多数用例验证的是别的规则；
    # T+1 本身由 S01 的专门用例用 acquired=DAY 验证。
    acquired = acquired or (DAY - timedelta(days=10))
    return Lot(lot_id=f"l-{iid}-{acquired}", instrument_id=iid,
               acquired_trading_day=acquired,
               earliest_sellable_day=sellable or (acquired + timedelta(days=1)),
               quantity_original=qty, quantity_remaining=qty,
               cost_basis_cents_per_share=cost)


# ================================================================== S01
def test_s01_same_day_purchase_is_not_sellable():
    """S01：当天买入又尝试全部卖出 -> 新买批次不可售；旧可售批次单独处理。"""

    s = sim()
    bars = {"SYN.A.600519": bar()}
    # 先买（生成当日批次）
    r1 = s.simulate(trading_day=DAY, orders=[
        Order(order_id="b1", instrument_id="SYN.A.600519", side=Side.BUY, quantity=1000),
    ], bars=bars, cash_available_cents=20_000_000, lots=[])
    assert r1.orders[0].status is OrderStatus.FILLED
    assert len(r1.lots_created) == 1
    new_lot = r1.lots_created[0]
    assert new_lot.earliest_sellable_day > DAY, "T+1：当日买入不得当日可卖"

    # 同一天尝试卖出 -> 拒绝
    r2 = s.simulate(trading_day=DAY, orders=[
        Order(order_id="s1", instrument_id="SYN.A.600519", side=Side.SELL, quantity=1000),
    ], bars=bars, cash_available_cents=1_000_000, lots=list(r1.lots_created))
    assert r2.orders[0].status is OrderStatus.REJECTED
    assert r2.orders[0].reject_reason == "T1_NOT_SELLABLE"
    assert not r2.fills


def test_s01_old_sellable_lot_is_handled_separately():
    """S01 后半：旧可售批次可正常卖出，新批次不受影响。"""

    s = sim()
    old = lot(qty=1000, acquired=DAY - timedelta(days=10),
              sellable=DAY - timedelta(days=7))
    new = Lot(lot_id="new", instrument_id="SYN.A.600519", acquired_trading_day=DAY,
              earliest_sellable_day=NEXT_DAY, quantity_original=500,
              quantity_remaining=500, cost_basis_cents_per_share=130000)
    r = s.simulate(trading_day=DAY, orders=[
        Order(order_id="s1", instrument_id="SYN.A.600519", side=Side.SELL, quantity=1000),
    ], bars={"SYN.A.600519": bar()}, cash_available_cents=0, lots=[old, new])
    assert r.orders[0].status is OrderStatus.FILLED
    # 只消耗旧批次；新批次原封不动
    assert [c["lot_id"] for c in r.lot_consumptions] == [old.lot_id]
    assert new.quantity_remaining == 500


# ================================================================== S02
def test_s02_open_limit_up_blocks_buy_without_fabricating_cash():
    """S02：买入遇开盘涨停 -> 保守规则不成交，无虚构资金。"""

    s = sim()
    r = s.simulate(trading_day=DAY, orders=[
        Order(order_id="b1", instrument_id="SYN.A.600519", side=Side.BUY, quantity=1000),
    ], bars={"SYN.A.600519": bar(limit_up=True)}, cash_available_cents=5_000_000, lots=[])
    assert r.orders[0].status is OrderStatus.NO_FILL
    assert r.orders[0].reject_reason == "LIMIT_PRICE_BLOCKED"
    assert not r.fills and not r.cash_entries
    assert not r.lots_created, "不成交就不得凭空产生持仓"


def test_s02_open_limit_down_blocks_sell():
    """S02：卖出遇开盘跌停 -> 不成交。"""

    s = sim()
    held = lot()
    # 跌停：开盘 = 前收 - 10%
    r = s.simulate(trading_day=DAY, orders=[
        Order(order_id="s1", instrument_id="SYN.A.600519", side=Side.SELL, quantity=1000),
    ], bars={"SYN.A.600519": bar(open_cents=8910, prev_close=9900)},
        cash_available_cents=0, lots=[held])
    assert r.orders[0].status is OrderStatus.NO_FILL
    assert held.quantity_remaining == 1000


# ================================================================== S03
def test_s03_unfilled_sell_does_not_create_proceeds_for_buy():
    """S03：卖单未成导致买单资金不足 -> 买单拒绝，现金不透支。"""

    s = sim()
    held = lot(qty=1000)
    # 卖单因跌停不成交；买单金额超过现有现金
    r = s.simulate(trading_day=DAY, orders=[
        Order(order_id="s1", instrument_id="SYN.A.600519", side=Side.SELL, quantity=1000),
        Order(order_id="b1", instrument_id="SYN.A.600519", side=Side.BUY, quantity=1000),
    ], bars={"SYN.A.600519": bar(open_cents=8910, prev_close=9900)},
        cash_available_cents=100_000, lots=[held])
    statuses = {o.order_id: o.status for o in r.orders}
    assert statuses["s1"] is OrderStatus.NO_FILL
    assert statuses["b1"] is OrderStatus.REJECTED
    assert r.orders[1].reject_reason == "INSUFFICIENT_CASH"
    assert not any(e.entry_type == "TRADE_SETTLEMENT" and e.amount_cents > 0
                   for e in r.cash_entries), "未成交的卖出不得产生收入"


def test_s03_sell_proceeds_are_usable_when_default_mode():
    """默认假设当日卖出收入可用于买入（并在报告中说明该简化）。"""

    s = sim()
    held = lot(qty=1000)
    r = s.simulate(trading_day=DAY, orders=[
        Order(order_id="s1", instrument_id="SYN.A.600519", side=Side.SELL, quantity=1000, sequence=0),
        Order(order_id="b1", instrument_id="SYN.A.600519", side=Side.BUY, quantity=1000, sequence=1),
    ], bars={"SYN.A.600519": bar()}, cash_available_cents=100_000, lots=[held])
    statuses = {o.order_id: o.status for o in r.orders}
    assert statuses["s1"] is OrderStatus.FILLED
    assert statuses["b1"] is OrderStatus.FILLED


def test_s03_conservative_mode_blocks_buy_that_needs_sell_proceeds():
    """§12.4 保守对照模式：买单只能用盘前现金。

    盘前现金刻意设为不足以买入，但当日卖出收入足够——两种模式的差别
    才会真正显现。若现金本就够买，这个用例什么也证明不了。
    """

    held = lot(qty=1000)
    bars = {"SYN.A.600519": bar()}
    orders = [
        Order(order_id="s1", instrument_id="SYN.A.600519", side=Side.SELL, quantity=1000, sequence=0),
        Order(order_id="b1", instrument_id="SYN.A.600519", side=Side.BUY, quantity=1000, sequence=1),
    ]
    opening_cash = 5_000_000   # 少于 1000 股所需（约 10,000,000 分）

    # 默认模式：可以动用当日卖出收入
    default_run = sim().simulate(trading_day=DAY, orders=list(orders), bars=bars,
                                 cash_available_cents=opening_cash, lots=[held])
    default_statuses = {o.order_id: o.status for o in default_run.orders}
    assert default_statuses["s1"] is OrderStatus.FILLED
    assert default_statuses["b1"] is OrderStatus.FILLED, default_run.rejections

    # 保守模式：只能动用盘前现金 -> 买单被拒
    conservative_run = sim(conservative_cash_mode=True).simulate(
        trading_day=DAY, orders=list(orders), bars=bars,
        cash_available_cents=opening_cash, lots=[lot(qty=1000)])  # 独立的批次副本
    cons_statuses = {o.order_id: o.status for o in conservative_run.orders}
    assert cons_statuses["s1"] is OrderStatus.FILLED
    assert cons_statuses["b1"] is OrderStatus.REJECTED, (
        "保守模式下不得动用当日卖出收入")


# ================================================================== S04
def test_s04_gap_above_budget_rejects_instead_of_resizing():
    """S04：买入开盘跳空超过资金预算 -> 不用未来价格事后重定最佳股数。"""

    s = sim()
    r = s.simulate(trading_day=DAY, orders=[
        Order(order_id="b1", instrument_id="SYN.A.600519", side=Side.BUY, quantity=1000),
    ], bars={"SYN.A.600519": bar(open_cents=150000, prev_close=135000)},
        cash_available_cents=1_400_000, lots=[])
    assert r.orders[0].status is OrderStatus.REJECTED
    assert r.orders[0].reject_reason == "INSUFFICIENT_CASH"
    # 计划数量必须原样保留，不得被静默缩小
    assert r.orders[0].quantity == 1000
    assert not r.fills


# ================================================================== S05
def test_s05_retry_of_frozen_plan_records_once():
    """S05：重试同一冻结计划 -> 订单、费用、成交仅记录一次。

    模拟器本身是无状态的；去重由 (plan_id, order_id) 唯一性与
    任务幂等键在调用层保证。这里验证**同一计划重复执行**不会产生额外分录。
    """

    from aquant.operations.jobs import JobStore
    from aquant.domain.data.db import apply_migrations, connect
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    con = connect(tmp / "m.sqlite")
    apply_migrations(con)
    store = JobStore(con)

    first_id, created1 = store.submit(job_type="execute_plan", trading_day="2026-09-11",
                                      config_version="cfg-v1", input_snapshot_id="snap-1")
    second_id, created2 = store.submit(job_type="execute_plan", trading_day="2026-09-11",
                                       config_version="cfg-v1", input_snapshot_id="snap-1")
    assert first_id == second_id
    assert created1 is True and created2 is False
    assert store.counts_by_status() == {"PENDING": 1}
    con.close()

    # 且模拟器对同一输入产生相同结果（可重放）
    s = sim()
    args = dict(trading_day=DAY, orders=[
        Order(order_id="b1", instrument_id="SYN.A.600519", side=Side.BUY, quantity=100),
    ], bars={"SYN.A.600519": bar()}, cash_available_cents=20_000_000, lots=[])
    r1 = s.simulate(**args)
    r2 = s.simulate(**args)
    assert [(f.fill_id, f.price_cents, f.fees_total_cents) for f in r1.fills] == \
           [(f.fill_id, f.price_cents, f.fees_total_cents) for f in r2.fills]


# ================================================================== S06
def test_s06_minimum_commission_reconciles_exactly():
    """S06：小额订单最低佣金 -> 费用与现金精确对账。"""

    s = sim()
    r = s.simulate(trading_day=DAY, orders=[
        Order(order_id="b1", instrument_id="SYN.A.600519", side=Side.BUY, quantity=100),
    ], bars={"SYN.A.600519": bar(open_cents=100, prev_close=100)},
        cash_available_cents=20_000_000, lots=[])
    fill = r.fills[0]
    codes = {l.fee_code: l.amount_cents for l in fill.fee_lines}
    assert codes["COMMISSION"] + codes["MIN_COMMISSION_TOPUP"] == 500
    assert codes["MIN_COMMISSION_TOPUP"] > 0

    # 现金对账：期初 - 成交额 - 费用 == 期末
    opening = 20_000_000
    closing = opening + sum(e.amount_cents for e in r.cash_entries)
    expected = opening - fill.gross_amount_cents - fill.fees_total_cents
    assert closing == expected
    fees_paid = sum(e.amount_cents for e in r.cash_entries
                    if e.entry_type not in {"TRADE_SETTLEMENT"})
    assert -fees_paid == fill.fees_total_cents, "费用分录合计必须等于成交费用总额"


# ================================================================== S07
def test_s07_dividend_receivable_and_cash_move_on_different_days():
    """S07：分红除息与到账不同日 -> 应收和现金正确迁移，不重复收益。"""

    from aquant.domain.simulation.corporate_actions import (
        CashDividend, apply_cash_dividend, record_dividend_entitlement,
    )

    div = CashDividend(
        action_id="ca-1", instrument_id="SYN.A.600003",
        record_date=date(2026, 9, 9), ex_date=date(2026, 9, 10),
        pay_date=date(2026, 9, 14), cash_per_share_cents=50,
    )
    # 必须在登记日（2026-09-09）当日或之前买入才有分红权利
    held = lot(iid="SYN.A.600003", qty=1000, acquired=date(2026, 9, 8))
    entitlement = record_dividend_entitlement(
        div, lots=[held], recorded_on=date(2026, 9, 9))

    # 除权日：确认应收，现金不变
    r_ex = apply_cash_dividend(div, entitlement=entitlement,
                               trading_day=date(2026, 9, 10))
    assert r_ex.receivable_cents == 1000 * 50
    assert r_ex.cash_delta_cents == 0, "除权日不得直接计入现金"

    # 到账日：应收转现金
    held.quantity_remaining = 0  # 登记日后卖出不影响已固化的权利
    r_pay = apply_cash_dividend(div, entitlement=entitlement,
                                trading_day=date(2026, 9, 14))
    assert r_pay.receivable_cents == 0
    assert r_pay.cash_delta_cents == 1000 * 50

    # 两阶段是一次收益的两个环节，不是两笔收益：
    # 除权日先记为应收，到账日应收清零并转入现金；因此
    # 除权日的应收额 == 到账日的现金额，且到账日不再新增应收。
    assert r_ex.receivable_cents == 1000 * 50
    assert r_pay.cash_delta_cents == 1000 * 50
    assert r_pay.receivable_cents == 0, "到账日应收必须清零，否则会重复计量"


def test_s07_no_entitlement_when_not_held_on_record_date():
    """登记日未持有 -> 无分红权利。"""

    from aquant.domain.simulation.corporate_actions import (
        CashDividend, apply_cash_dividend, record_dividend_entitlement,
    )

    div = CashDividend(action_id="ca-2", instrument_id="SYN.A.600003",
                       record_date=date(2026, 9, 9), ex_date=date(2026, 9, 10),
                       pay_date=date(2026, 9, 14), cash_per_share_cents=50)
    # 批次在登记日之后才买入
    bought_late = lot(iid="SYN.A.600003", qty=1000, acquired=date(2026, 9, 10))
    entitlement = record_dividend_entitlement(
        div, lots=[bought_late], recorded_on=date(2026, 9, 9))
    r = apply_cash_dividend(div, entitlement=entitlement,
                            trading_day=date(2026, 9, 14))
    assert r.cash_delta_cents == 0
    assert r.receivable_cents == 0


def test_s07_lot_sold_before_record_date_has_no_entitlement():
    from aquant.domain.simulation.corporate_actions import (
        CashDividend, record_dividend_entitlement,
    )

    div = CashDividend(action_id="ca-3", instrument_id="SYN.A.600003",
                       record_date=date(2026, 9, 9), ex_date=date(2026, 9, 10),
                       pay_date=date(2026, 9, 14), cash_per_share_cents=50)
    sold = lot(iid="SYN.A.600003", qty=1000, acquired=date(2026, 9, 8))
    sold.quantity_remaining = 0
    entitlement = record_dividend_entitlement(
        div, lots=[sold], recorded_on=date(2026, 9, 9))
    assert entitlement.shares == 0


# ================================================================== S08
def test_s08_suspension_produces_no_fill_and_no_removal():
    """S08：停牌 -> 不制造成交，不移除亏损样本，明确估值与未支持状态。"""

    s = sim()
    held = lot(qty=1000)
    r = s.simulate(trading_day=DAY, orders=[
        Order(order_id="s1", instrument_id="SYN.A.600519", side=Side.SELL, quantity=1000),
    ], bars={}, cash_available_cents=0, lots=[held])   # 无 Bar = 停牌
    assert r.orders[0].status is OrderStatus.NO_FILL
    assert r.orders[0].reject_reason == "NO_VALID_OPEN_PRICE"
    assert held.quantity_remaining == 1000, "停牌不得移除持仓"


def test_s08_unsupported_corporate_action_must_be_flagged_not_approximated():
    """S08：无法核验的复杂公司行为 -> 明确标记未支持，不近似处理。"""

    from aquant.domain.simulation.corporate_actions import (
        UnsupportedAction, assert_action_supported,
    )

    action = UnsupportedAction(action_id="ca-3", instrument_id="SYN.A.600002",
                               action_type="RIGHTS_ISSUE",
                               reason="配股细则不完整")
    with pytest.raises(SimError) as exc:
        assert_action_supported(action)
    assert exc.value.code == "CORPORATE_ACTION_UNSUPPORTED"


def test_s08_delisting_keeps_history_with_explicit_terminal_state():
    """S08：退市保留历史与退出事件，不得永久用最后收盘价当作可清算。"""

    from aquant.domain.simulation.corporate_actions import terminal_value_cents

    normal = terminal_value_cents(last_close_cents=8850, delisted=True)
    assert normal["basis"] == "UNSUPPORTED"
    assert normal["value_cents"] is None, "不得默认按最后收盘价清算"
    still_listed = terminal_value_cents(last_close_cents=8850, delisted=False)
    assert still_listed["basis"] == "CLOSE"
    assert still_listed["value_cents"] == 8850


# ================================================================== S09
def test_s09_rule_selected_by_effective_date():
    """S09：规则生效日前后、板块不同 -> 使用相应日期和板块的规则配置。"""

    old_rules = [
        BoardRule(exchange="SSE", board="MAIN", price_limit_pct=Decimal("10"),
                  lot_size=100, effective_from=date(2020, 1, 1),
                  effective_to=date(2026, 7, 6)),
        BoardRule(exchange="SSE", board="MAIN", price_limit_pct=Decimal("10"),
                  lot_size=100, effective_from=date(2026, 7, 6)),
    ]
    s = sim(board_rules=old_rules)
    before = s.rule_for("SSE", "MAIN", date(2026, 7, 3))
    after = s.rule_for("SSE", "MAIN", date(2026, 7, 6))
    assert before.effective_to == date(2026, 7, 6)
    assert after.effective_to is None


def test_s09_missing_rule_is_an_error_not_a_default():
    """没有匹配规则时必须报错，绝不假定默认涨跌幅。"""

    s = sim()
    with pytest.raises(SimError) as exc:
        s.rule_for("SSE", "STAR", DAY)
    assert exc.value.code == "RULE_VERSION_MISSING"
    assert "never assume a default price limit" in exc.value.repair_action


def test_s09_price_limits_computed_per_board():
    s = sim()
    rule = s.rule_for("SSE", "MAIN", DAY)
    low, high = s.price_limits(129000, rule.price_limit_pct)
    assert high == 129000 + 12900
    assert low == 129000 - 12900


# ================================================================== S10
def test_s10_no_intraday_sequence_inference():
    """S10：仅日线且同时穿过止损止盈价 -> 首期不推断盘中先后，不生成此类成交。

    实现方式：模拟器只使用开盘价成交，且**没有**任何止损/止盈逻辑。
    因此不存在"因为当天最高价先触及止损"这类推断。
    """

    import inspect

    from aquant.domain.simulation import simulator as mod

    source = inspect.getsource(mod)
    for forbidden in ("stop_loss", "take_profit", "intraday", "bar.high_cents >"):
        assert forbidden not in source, f"simulator must not infer intraday ordering: {forbidden}"


def test_s10_fill_uses_open_price_only():
    """成交价只由开盘价与滑点决定，不参考当日高低收。"""

    s = sim()
    r = s.simulate(trading_day=DAY, orders=[
        Order(order_id="b1", instrument_id="SYN.A.600519", side=Side.BUY, quantity=100),
    ], bars={"SYN.A.600519": Bar(
        instrument_id="SYN.A.600519", trading_day=DAY, open_cents=10000,
        high_cents=200000, low_cents=100000, close_cents=190000,
        prev_close_cents=9900, volume_shares=1_000_000,
    )}, cash_available_cents=20_000_000, lots=[])
    fill = r.fills[0]
    # 开盘 10000 + 5bp 滑点 = 10005
    assert fill.price_cents == 10005
    assert fill.price_cents != 190000 and fill.price_cents != 100000


# ================================================================== 不变量
def test_invariants_pass_on_a_clean_run():
    s = sim()
    opening = 20_000_000
    r = s.simulate(trading_day=DAY, orders=[
        Order(order_id="b1", instrument_id="SYN.A.600519", side=Side.BUY, quantity=100),
    ], bars={"SYN.A.600519": bar()}, cash_available_cents=opening, lots=[])
    closing = opening + sum(e.amount_cents for e in r.cash_entries)
    inv = check_invariants(cash_available_cents=closing, lots=r.lots_created,
                           fills=r.fills, orders=r.orders, cash_entries=r.cash_entries,
                           opening_cash_cents=opening)
    assert inv["all_ok"], inv["violations"]


def test_invariants_detect_overdraft():
    inv = check_invariants(cash_available_cents=-1, lots=[], fills=[], orders=[],
                           cash_entries=[], opening_cash_cents=-1)
    assert inv["cash_not_overdrawn"] is False
    assert inv["all_ok"] is False


def test_invariants_detect_negative_lot():
    bad = lot(qty=100)
    bad.quantity_remaining = -1
    inv = check_invariants(cash_available_cents=0, lots=[bad], fills=[], orders=[],
                           cash_entries=[], opening_cash_cents=0)
    assert inv["positions_not_negative"] is False


def test_invariants_detect_cash_line_mismatch():
    inv = check_invariants(cash_available_cents=100, opening_cash_cents=100,
                           lots=[], fills=[], orders=[],
                           cash_entries=[CashEntry("OTHER", -1, DAY)])
    assert inv["cash_lines_sum_to_balance"] is False


# ================================================================== 稳定序
def test_sell_orders_are_processed_before_buys():
    """§12.4 先处理卖单，再处理买单。"""

    s = sim()
    held = lot(qty=1000)
    r = s.simulate(trading_day=DAY, orders=[
        Order(order_id="b1", instrument_id="SYN.A.600519", side=Side.BUY, quantity=100, sequence=0),
        Order(order_id="s1", instrument_id="SYN.A.600519", side=Side.SELL, quantity=1000, sequence=1),
    ], bars={"SYN.A.600519": bar()}, cash_available_cents=0, lots=[held])
    executed = [f.order_id for f in r.fills]
    assert executed == ["s1", "b1"], "卖单必须先处理，买单才能用到其收入"


def test_unknown_listing_is_rejected_not_guessed():
    """缺少上市信息时必须报错，绝不从代码前缀推断板块。"""

    s = sim()
    with pytest.raises(SimError) as exc:
        s.simulate(trading_day=DAY, orders=[
            Order(order_id="b1", instrument_id="SYN.A.999999", side=Side.BUY, quantity=100),
        ], bars={"SYN.A.999999": bar(iid="SYN.A.999999")},
            cash_available_cents=20_000_000, lots=[])
    assert "never infer them from the code prefix" in exc.value.repair_action
