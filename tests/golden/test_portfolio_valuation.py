"""组合构建与日终估值测试（主文档 §11.1、§10.3、§12.8）。"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from aquant.domain.portfolio.construction import (
    Candidate,
    ConstructionParams,
    compute_valuation,
    construct_targets,
    value_positions,
    weights_to_orders,
)
from aquant.domain.simulation.simulator import Bar, Lot, SimError

DAY = date(2026, 9, 11)


def bar(iid, close=10000, prev=9900):
    return Bar(instrument_id=iid, trading_day=DAY, open_cents=close, high_cents=close,
               low_cents=close, close_cents=close, prev_close_cents=prev,
               volume_shares=1_000_000)


def cand(iid, industry="IND_A", rank=1.0):
    return Candidate(instrument_id=iid, industry_code=industry, signal_rank=rank)


# ------------------------------------------------------------------ 排序确定性
def test_ties_are_broken_by_instrument_id():
    """§10.3 同分时用证券 ID 稳定打破平局，结果必须可复现。"""

    a = cand("SYN.A.000002", rank=1.0)
    b = cand("SYN.A.000001", rank=1.0)
    first = construct_targets(candidates=[a, b], params=ConstructionParams(
        max_holdings=5, max_single_name_pct=Decimal("10"),
        max_single_industry_pct=Decimal("30")),
        bar_by_instrument={a.instrument_id: bar(a.instrument_id),
                           b.instrument_id: bar(b.instrument_id)})
    second = construct_targets(candidates=[b, a], params=ConstructionParams(
        max_holdings=5, max_single_name_pct=Decimal("10"),
        max_single_industry_pct=Decimal("30")),
        bar_by_instrument={a.instrument_id: bar(a.instrument_id),
                           b.instrument_id: bar(b.instrument_id)})
    assert [t.instrument_id for t in first.targets] == [t.instrument_id for t in second.targets]
    assert first.targets[0].instrument_id == "SYN.A.000001"


# ------------------------------------------------------------------ 上限
def test_total_equity_cap_is_respected():
    params = ConstructionParams(max_holdings=20, max_total_equity_pct=Decimal("80"),
                               max_single_name_pct=Decimal("10"),
                               max_single_industry_pct=Decimal("100"))
    cands = [cand(f"SYN.A.{i:06d}", rank=float(100 - i)) for i in range(1, 21)]
    bars = {c.instrument_id: bar(c.instrument_id) for c in cands}
    res = construct_targets(candidates=cands, params=params, bar_by_instrument=bars)
    total = sum(t.weight_pct for t in res.targets)
    assert total <= Decimal("80")
    assert res.cash_weight_pct >= Decimal("20")


def test_single_name_cap_is_respected():
    params = ConstructionParams(max_holdings=3, max_total_equity_pct=Decimal("80"),
                               max_single_name_pct=Decimal("10"),
                               max_single_industry_pct=Decimal("100"))
    cands = [cand(f"SYN.A.{i:06d}", rank=float(10 - i)) for i in range(1, 4)]
    bars = {c.instrument_id: bar(c.instrument_id) for c in cands}
    res = construct_targets(candidates=cands, params=params, bar_by_instrument=bars)
    assert all(t.weight_pct <= Decimal("10") for t in res.targets)


def test_existing_industry_exposure_consumes_the_budget_first():
    """§11.1 第 2 步：某行业已占 25%、上限 30% -> 其他股票只能用剩下的 5%。

    这是"既有风险不因过滤而消失"的直接检验。
    """

    params = ConstructionParams(max_holdings=10, max_total_equity_pct=Decimal("80"),
                               max_single_name_pct=Decimal("10"),
                               max_single_industry_pct=Decimal("30"))
    cands = [cand("SYN.A.000001", industry="IND_A", rank=2.0),
             cand("SYN.A.000002", industry="IND_A", rank=1.0)]
    bars = {c.instrument_id: bar(c.instrument_id) for c in cands}
    equity = 100_000_000
    # 既有持仓已在 IND_A 占 25%
    res = construct_targets(candidates=cands, params=params, bar_by_instrument=bars,
                            held_industry_value={"IND_A": equity * 25 // 100},
                            equity_value_cents=equity)
    total_ind_a = sum(t.weight_pct for t in res.targets)
    assert total_ind_a <= Decimal("5"), f"industry headroom is only 5%, got {total_ind_a}"
    assert res.excluded, "被行业上限挡住的标必须留下原因"


def test_excluded_candidates_carry_a_reason():
    params = ConstructionParams(max_holdings=1, max_total_equity_pct=Decimal("80"),
                               max_single_name_pct=Decimal("10"),
                               max_single_industry_pct=Decimal("100"))
    cands = [cand("SYN.A.000001", rank=2.0), cand("SYN.A.000002", rank=1.0)]
    bars = {c.instrument_id: bar(c.instrument_id) for c in cands}
    res = construct_targets(candidates=cands, params=params, bar_by_instrument=bars)
    assert len(res.targets) == 1
    assert res.excluded and res.excluded[0]["reason"] == "MAX_HOLDINGS_REACHED"


def test_illiquid_or_priceless_candidate_is_excluded_not_priced():
    params = ConstructionParams(max_holdings=5, max_single_name_pct=Decimal("10"),
                               max_single_industry_pct=Decimal("100"))
    cands = [cand("SYN.A.000001")]
    res = construct_targets(candidates=cands, params=params, bar_by_instrument={})
    assert not res.targets
    assert res.excluded[0]["reason"] == "NO_VALID_OPEN_PRICE"


def test_retention_buffer_prefers_existing_holdings():
    """§10.3 仍在 top N 的既有持仓优先保留。"""

    params = ConstructionParams(max_holdings=2, max_total_equity_pct=Decimal("80"),
                               max_single_name_pct=Decimal("10"),
                               max_single_industry_pct=Decimal("100"),
                               retention_buffer_top_n=30)
    cands = [cand("SYN.A.000001", rank=3.0), cand("SYN.A.000002", rank=2.0),
             cand("SYN.A.000003", rank=1.0)]
    bars = {c.instrument_id: bar(c.instrument_id) for c in cands}
    res = construct_targets(candidates=cands, params=params, bar_by_instrument=bars,
                            held={"SYN.A.000002": 100})
    ids = [t.instrument_id for t in res.targets]
    assert "SYN.A.000002" in ids, "缓冲内的既有持仓必须保留"
    assert any("retained" in t.rationale for t in res.targets)


def test_self_contradictory_caps_are_rejected():
    with pytest.raises(SimError):
        ConstructionParams(max_single_name_pct=Decimal("40"),
                           max_single_industry_pct=Decimal("30"))


# ------------------------------------------------------------------ 订单换算
def test_weights_to_orders_rounds_down_to_whole_lots():
    params = ConstructionParams()
    from aquant.domain.portfolio.construction import TargetWeight
    t = TargetWeight("SYN.A.000001", "IND_A", Decimal("10"), "test")
    orders = weights_to_orders(targets=[t], params=params,
                               price_by_instrument={"SYN.A.000001": 3333},
                               equity_value_cents=100_000_000)
    qty = orders[0]["quantity"]
    assert qty % 100 == 0, "必须向下取整到整手"
    # 10% * 1,000,000 元 = 100,000 元 = 10,000,000 分；/3333 -> 3000 股
    assert qty == 3000


def test_sell_order_is_capped_by_sellable_quantity():
    params = ConstructionParams()
    from aquant.domain.portfolio.construction import TargetWeight
    t = TargetWeight("SYN.A.000001", "IND_A", Decimal("0"), "exit")
    orders = weights_to_orders(targets=[t], params=params,
                               price_by_instrument={"SYN.A.000001": 10000},
                               held_quantity={"SYN.A.000001": 1000},
                               sellable_quantity={"SYN.A.000001": 400})
    assert orders[0]["side"] == "SELL"
    assert orders[0]["quantity"] == 400, "卖出不得超过可卖数量"


def test_no_order_when_already_at_target():
    params = ConstructionParams()
    from aquant.domain.portfolio.construction import TargetWeight
    t = TargetWeight("SYN.A.000001", "IND_A", Decimal("10"), "hold")
    orders = weights_to_orders(targets=[t], params=params,
                               price_by_instrument={"SYN.A.000001": 10000},
                               held_quantity={"SYN.A.000001": 1000},
                               equity_value_cents=100_000_000)
    assert orders == []


# ------------------------------------------------------------------ 估值
def lot(iid, qty, acquired=DAY - timedelta(days=5)):
    return Lot(lot_id=f"l-{iid}", instrument_id=iid, acquired_trading_day=acquired,
               earliest_sellable_day=acquired + timedelta(days=1),
               quantity_original=qty, quantity_remaining=qty,
               cost_basis_cents_per_share=10000)


def test_net_value_formula_matches_the_spec():
    """§12.8 净值 = 可用及冻结现金 + 应收 + 持仓估值 - 应付。"""

    poses, issues = value_positions(lots=[lot("SYN.A.000001", 1000)],
                                    bars={"SYN.A.000001": bar("SYN.A.000001", close=10000)},
                                    last_valid_price={}, trading_day=DAY)
    assert not issues
    v = compute_valuation(trading_day=DAY, cash_available_cents=5_000_000,
                          cash_frozen_cents=100_000, receivables_cents=50_000,
                          payables_cents=10_000, positions=poses,
                          lots=[lot("SYN.A.000001", 1000)])
    expected = 5_000_000 + 100_000 + 50_000 + 10_000_000 - 10_000
    assert v.net_value_cents == expected
    assert v.published is True


def test_suspended_position_uses_recorded_last_valid_price_with_staleness():
    """§12.8 停牌用显式记录的最近有效价并标注停牌天数，不编造当日行情。"""

    last_valid = DAY - timedelta(days=3)
    poses, issues = value_positions(lots=[lot("SYN.A.600002", 1000)],
                                    bars={},   # 当日无行情
                                    last_valid_price={"SYN.A.600002": (8880, last_valid)},
                                    trading_day=DAY)
    assert not issues
    p = poses[0]
    assert p.price_basis == "SUSPENDED_LAST_VALID"
    assert p.price_cents == 8880
    assert p.staleness_days == 3
    assert p.value_cents == 8_880_000


def test_position_without_any_price_basis_blocks_publication():
    poses, issues = value_positions(lots=[lot("SYN.A.999999", 100)],
                                    bars={}, last_valid_price={}, trading_day=DAY)
    assert issues and issues[0]["code"] == "DATA_NOT_READY"
    v = compute_valuation(trading_day=DAY, cash_available_cents=0, positions=poses,
                          extra_issues=issues)
    assert v.published is False, "无估值依据时不得发布净值"
    assert v.invariants["all_ok"] is False


def test_lot_position_mismatch_blocks_publication():
    """§12.8 股数必须与批次一致。"""

    poses = [__import__("aquant.domain.portfolio.construction", fromlist=["PositionValue"])
             .PositionValue("SYN.A.000001", 1000, 10000, "CLOSE", 0, 10_000_000)]
    v = compute_valuation(trading_day=DAY, cash_available_cents=0, positions=poses,
                          lots=[lot("SYN.A.000001", 400)])
    assert v.invariants["shares_match_lots"] is False
    assert v.published is False


def test_negative_cash_blocks_publication():
    v = compute_valuation(trading_day=DAY, cash_available_cents=-1, positions=[])
    assert v.invariants["cash_not_overdrawn"] is False
    assert v.published is False


def test_valuation_is_serializable_for_the_ledger():
    poses, _ = value_positions(lots=[lot("SYN.A.000001", 100)],
                               bars={"SYN.A.000001": bar("SYN.A.000001")},
                               last_valid_price={}, trading_day=DAY)
    v = compute_valuation(trading_day=DAY, cash_available_cents=1000, positions=poses,
                          lots=[lot("SYN.A.000001", 100)])
    d = v.as_dict()
    assert {"net_value_cents", "positions", "invariants", "published"} <= set(d)
    assert d["published"] is True
