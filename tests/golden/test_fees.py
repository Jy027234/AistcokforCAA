"""费用模型测试（主文档 §12.6、S06、S09）。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from aquant.domain.simulation.fees import (
    FeeError,
    FeeSchedule,
    FeeTable,
    synthetic_fee_table,
    to_cents,
)


def sched(**kw):
    base = dict(
        fee_version="f1", effective_from=date(2024, 1, 1), effective_to=None,
        commission_rate=Decimal("0.00025"), commission_min_cents=500,
        stamp_duty_rate_sell=Decimal("0.0005"), transfer_fee_rate=Decimal("0.00001"),
    )
    base.update(kw)
    return FeeSchedule(**base)


# ------------------------------------------------------------------ 基本性质
def test_cents_rounding_is_half_up_on_decimal():
    assert to_cents(Decimal("1.4")) == 1
    assert to_cents(Decimal("1.5")) == 2
    assert to_cents(Decimal("2.5")) == 3


def test_non_finite_amount_is_rejected():
    with pytest.raises(FeeError):
        to_cents(Decimal("NaN"))


def test_rates_must_be_decimal_not_float():
    """§12.6 禁止用二进制浮点表达费率。"""

    with pytest.raises(FeeError) as exc:
        sched(commission_rate=0.00025)  # float
    assert "binary floats" in exc.value.repair_action


def test_negative_rate_is_rejected():
    with pytest.raises(FeeError):
        sched(commission_rate=Decimal("-0.001"))


# ------------------------------------------------------------------ S06 最低佣金
def test_small_order_triggers_minimum_commission_topup():
    """S06：小额订单最低佣金，费用与现金精确对账。"""

    table = FeeTable([sched()])
    # 100 股 * 1.00 元 = 10000 分 = 100 元；佣金 0.025% -> 2.5 分，远低于最低 500 分
    charge = table.compute(side="BUY", quantity=100, price_cents=100,
                           trading_day=date(2024, 5, 6))
    codes = {l.fee_code: l.amount_cents for l in charge.lines}
    assert codes["COMMISSION"] == 3            # 2.5 分四舍五入
    assert codes["MIN_COMMISSION_TOPUP"] == 497
    assert codes["COMMISSION"] + codes["MIN_COMMISSION_TOPUP"] == 500
    assert charge.total_cents == sum(codes.values())


def test_large_order_pays_proportional_commission_without_topup():
    table = FeeTable([sched()])
    # 10000 股 * 100.00 元 = 1,000,000 元；佣金 0.025% = 250 元 = 25000 分
    charge = table.compute(side="BUY", quantity=10000, price_cents=10000,
                           trading_day=date(2024, 5, 6))
    codes = {l.fee_code: l.amount_cents for l in charge.lines}
    assert "MIN_COMMISSION_TOPUP" not in codes
    assert codes["COMMISSION"] == 25000


def test_commission_exactly_at_minimum_has_no_topup():
    table = FeeTable([sched()])
    # 需要 gross * 0.00025 == 500 分 -> gross = 2,000,000 分
    charge = table.compute(side="BUY", quantity=20000, price_cents=100,
                           trading_day=date(2024, 5, 6))
    codes = {l.fee_code: l.amount_cents for l in charge.lines}
    assert codes["COMMISSION"] == 500
    assert "MIN_COMMISSION_TOPUP" not in codes


# ------------------------------------------------------------------ 印花税
def test_stamp_duty_charged_on_sell_only():
    """§12.6 印花税仅卖出方计收。"""

    table = FeeTable([sched()])
    buy = table.compute(side="BUY", quantity=10000, price_cents=10000,
                        trading_day=date(2024, 5, 6))
    sell = table.compute(side="SELL", quantity=10000, price_cents=10000,
                         trading_day=date(2024, 5, 6))
    assert "STAMP_DUTY" not in {l.fee_code for l in buy.lines}
    assert "STAMP_DUTY" in {l.fee_code for l in sell.lines}


def test_transfer_fee_charged_both_directions():
    table = FeeTable([sched()])
    buy = table.compute(side="BUY", quantity=10000, price_cents=10000,
                        trading_day=date(2024, 5, 6))
    sell = table.compute(side="SELL", quantity=10000, price_cents=10000,
                         trading_day=date(2024, 5, 6))
    assert "TRANSFER_FEE" in {l.fee_code for l in buy.lines}
    assert "TRANSFER_FEE" in {l.fee_code for l in sell.lines}


def test_sell_costs_more_than_buy_for_same_notional():
    table = FeeTable([sched()])
    buy = table.compute(side="BUY", quantity=10000, price_cents=10000,
                        trading_day=date(2024, 5, 6))
    sell = table.compute(side="SELL", quantity=10000, price_cents=10000,
                         trading_day=date(2024, 5, 6))
    assert sell.total_cents > buy.total_cents


# ------------------------------------------------------------------ S09 生效日
def test_fee_version_selected_by_effective_date():
    """S09：规则生效日前后使用相应日期的配置。"""

    table = synthetic_fee_table()
    before = table.schedule_for(date(2023, 8, 25))
    after = table.schedule_for(date(2023, 8, 28))
    assert before.fee_version == "fee-syn-v1-pre2023"
    assert after.fee_version == "fee-syn-v1"
    # 印花税减半确实生效
    assert after.stamp_duty_rate_sell * 2 == before.stamp_duty_rate_sell


def test_boundary_day_uses_the_new_schedule():
    """生效日当天即适用新版本（左闭右开）。"""

    table = synthetic_fee_table()
    assert table.schedule_for(date(2023, 8, 28)).fee_version == "fee-syn-v1"
    # 前一天仍用旧版本
    assert table.schedule_for(date(2023, 8, 27)).fee_version == "fee-syn-v1-pre2023"


def test_gap_in_coverage_is_an_error_not_a_guess():
    """没有覆盖该日期的费率时必须报错，绝不回退到无关费率。"""

    table = FeeTable([sched(effective_from=date(2025, 1, 1))])
    with pytest.raises(FeeError) as exc:
        table.compute(side="BUY", quantity=100, price_cents=1000,
                      trading_day=date(2024, 5, 6))
    assert exc.value.code == "FEE_VERSION_UNVERIFIED"


def test_overlapping_schedules_are_rejected():
    """同一日两个生效版本意味着规则冲突，宁可报错也不猜。"""

    table = FeeTable([
        sched(fee_version="a", effective_from=date(2024, 1, 1), effective_to=date(2024, 7, 1)),
        sched(fee_version="b", effective_from=date(2024, 6, 1), effective_to=None),
    ])
    with pytest.raises(FeeError) as exc:
        table.schedule_for(date(2024, 6, 15))
    assert "multiple fee schedules" in exc.value.message


# ------------------------------------------------------------------ 合成费率守卫
def test_synthetic_rate_is_blocked_for_formal_research():
    """§12.6 合成费率明确禁止用于正式研究。"""

    sched_ = synthetic_fee_table().schedule_for(date(2024, 5, 6))
    assert sched_.synthetic_test_rate is True
    with pytest.raises(FeeError) as exc:
        sched_.assert_usable_for_formal_research()
    assert exc.value.code == "FEE_VERSION_UNVERIFIED"


def test_empty_fee_table_is_rejected():
    with pytest.raises(FeeError):
        FeeTable([])


def test_negative_quantity_or_price_rejected():
    table = FeeTable([sched()])
    with pytest.raises(FeeError):
        table.compute(side="BUY", quantity=0, price_cents=100, trading_day=date(2024, 5, 6))
    with pytest.raises(FeeError):
        table.compute(side="BUY", quantity=100, price_cents=0, trading_day=date(2024, 5, 6))


def test_breakdown_is_serializable_for_the_ledger():
    """费用明细要能直接写入 fee_charge 表（§12.6 可审计）。"""

    table = FeeTable([sched()])
    charge = table.compute(side="SELL", quantity=10000, price_cents=10000,
                           trading_day=date(2024, 5, 6))
    rows = charge.breakdown()
    assert all({"fee_code", "amount_cents", "fee_version"} <= set(r) for r in rows)
    assert len({(r["fee_code"], r["fee_version"]) for r in rows}) == len(rows), \
        "同一成交同一费用码不得出现两行（§12.6 不得重复计费）"
