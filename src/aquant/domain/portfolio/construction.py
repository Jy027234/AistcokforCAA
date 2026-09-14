"""组合构建与日终估值（主文档 §11、§12.8）。

组合构建的确定性次序（§11.1），顺序不可颠倒：

  1. 读冻结输入、持仓批次与当前状态
  2. 标记不可卖 / 不可买 / 未解决的公司行为限制
     —— **既有风险不因过滤而消失**：某行业已占 25%、上限 30%，则其他股票
     只能用剩下的 5%，不得先忽略既有持仓再分配 30%
  3. 由策略排名与保留缓冲形成目标集合
  4. 在总仓位、单券、行业上限内生成目标权重，约束不足时保留现金
  5. 用**决策时点可知的价格**把权重差换成整数股订单
  6. 校验预计现金、费用、可卖数量与历史成交量上限
  7. 生成差异预览并冻结计划

日终估值与不变量（§12.8）：

    每日净值 = 可用及冻结现金 + 支持的应收项目 + 持仓估值 - 应付项目

停牌股票用**显式记录的最近有效价**并标注停牌天数，绝不编造当日行情。
不变量失败必须**阻断净值发布**，而不是仅打印告警。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from ..simulation.simulator import Bar, Lot, SimError


def _round(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# ======================================================================
# 组合构建
# ======================================================================
@dataclass(frozen=True, slots=True)
class ConstructionParams:
    """§11.2 初始研究参数。全部属于某个实验版本，不原地覆盖。"""

    initial_capital_cents: int = 100_000_000
    max_holdings: int = 20
    max_total_equity_pct: Decimal = Decimal("80")
    max_single_name_pct: Decimal = Decimal("10")
    max_single_industry_pct: Decimal = Decimal("30")
    exclude_listing_days: int = 120
    #: 流动性下限（按成交额，单位分）。
    #: 
    #: 为 None 表示**不做流动性筛选**，这是免费源下的正确缺省：
    #: 腾讯日线只有成交量、没有成交额，若给一个默认阈值，筛选会因为
    #: "没有成交额"而把所有标的都排除，或者更糟——用 价格×成交量 补造
    #: 成交额，然后假装那是观测到的流动性。两种情况都不该发生。
    #: 需要成交额门槛时必须显式给值，并自行确保证据里确实有成交额。
    liquidity_min_avg_amount_cents: int | None = None
    liquidity_lookback_days: int = 20
    retention_buffer_top_n: int = 30

    def __post_init__(self) -> None:
        if self.max_single_name_pct > self.max_single_industry_pct:
            raise SimError(
                "RULE_VERSION_MISSING",
                "single-name cap exceeds industry cap, which is self-contradictory",
                "params", "raise the industry cap above the single-name cap",
            )


@dataclass(frozen=True, slots=True)
class Candidate:
    instrument_id: str
    industry_code: str
    signal_rank: float
    #: 是否进入**可执行模拟池**（主文档 §4.1 默认可模拟范围）。
    #:
    #: 创业板、科创板、北交所首期**可展示但不可模拟**。这项约束如果只写在
    #: 研究池配置或调用方的心里，就随时会在某次重构里丢掉——标的会悄悄
    #: 重新进入可模拟池，而涨跌幅规则却按 20% 执行，成交结果无从复核。
    #: 因此它是候选自身的属性，由组合构建强制。
    simulatable: bool = True

    def sort_key(self) -> tuple[float, str]:
        """名次由 signal_rank 降序决定，同分用 instrument_id 稳定打破平局（§10.3）。"""

        return (-self.signal_rank, self.instrument_id)


@dataclass(frozen=True, slots=True)
class TargetWeight:
    instrument_id: str
    industry_code: str
    weight_pct: Decimal
    rationale: str


@dataclass(frozen=True, slots=True)
class ConstructionResult:
    targets: list[TargetWeight]
    cash_weight_pct: Decimal
    excluded: list[dict]
    notes: list[str] = field(default_factory=list)

    def as_preview(self) -> dict:
        return {
            "targets": [
                {"instrument_id": t.instrument_id, "industry_code": t.industry_code,
                 "weight_pct": str(t.weight_pct), "rationale": t.rationale}
                for t in self.targets
            ],
            "cash_weight_pct": str(self.cash_weight_pct),
            "excluded": self.excluded,
            "notes": self.notes,
        }


def construct_targets(
    *,
    candidates: list[Candidate],
    params: ConstructionParams,
    held: dict[str, int] | None = None,
    held_industry_value: dict[str, int] | None = None,
    equity_value_cents: int | None = None,
    bar_by_instrument: dict[str, Bar] | None = None,
    unlisted_lot_ids: list[str] | None = None,
) -> ConstructionResult:
    """确定性组合构建。

    held: 当前持仓股数（不可卖的既有持仓仍占用风险预算）。
    held_industry_value: 既有持仓按行业计的市值（分），用于行业上限。
    """

    held = held or {}
    held_industry_value = held_industry_value or {}
    bars = bar_by_instrument or {}
    equity = equity_value_cents or params.initial_capital_cents
    excluded: list[dict] = []
    notes: list[str] = []

    if unlisted_lot_ids:
        notes.append(
            f"{len(unlisted_lot_ids)} lot(s) have no current price and were kept at last valid "
            "value; they still consume risk budget"
        )

    ranked = sorted(candidates, key=lambda c: c.sort_key())

    # §10.3 保留缓冲：仍在 top N 的既有持仓优先保留
    buffer_ids = {c.instrument_id for c in ranked[: params.retention_buffer_top_n]}
    retained = [c for c in ranked if c.instrument_id in held and c.instrument_id in buffer_ids]
    fresh = [c for c in ranked if c.instrument_id not in held]
    ordered = retained + fresh

    targets: list[TargetWeight] = []
    industry_used: dict[str, Decimal] = {}
    # 既有持仓先占用行业预算——§11.1 第 2 步：既有风险不因过滤而消失
    for code, value in held_industry_value.items():
        if equity > 0:
            industry_used[code] = Decimal(value) / Decimal(equity) * Decimal(100)

    total_equity_pct = Decimal(0)

    for cand in ordered:
        if not cand.simulatable:
            # §4.1 首期只模拟沪深主板。展示与模拟是两种权限，
            # 不可模拟不等于"数据有问题"，因此给的是规则说明而不是错误。
            excluded.append({
                "instrument_id": cand.instrument_id,
                "reason": "BOARD_NOT_SIMULATABLE_IN_PHASE_1",
                "detail": ("创业板/科创板/北交所首期可展示但不进入可执行模拟池；"
                           "需先补齐该板块的交易规则版本与验收用例"),
            })
            continue

        if len(targets) >= params.max_holdings:
            excluded.append({"instrument_id": cand.instrument_id,
                             "reason": "MAX_HOLDINGS_REACHED"})
            continue

        bar = bars.get(cand.instrument_id)
        if bar is None:
            excluded.append({"instrument_id": cand.instrument_id,
                             "reason": "NO_VALID_OPEN_PRICE"})
            continue

        if total_equity_pct >= params.max_total_equity_pct:
            excluded.append({"instrument_id": cand.instrument_id,
                             "reason": "MAX_TOTAL_EQUITY_REACHED"})
            continue

        target_pct = min(params.max_single_name_pct, params.max_total_equity_pct)

        ind_used = industry_used.get(cand.industry_code, Decimal(0))
        ind_headroom = params.max_single_industry_pct - ind_used
        if ind_headroom <= 0:
            excluded.append({
                "instrument_id": cand.instrument_id,
                "reason": "INDUSTRY_CAP_REACHED",
                "detail": (f"industry {cand.industry_code} already at {ind_used:.2f}% of the "
                           f"{params.max_single_industry_pct}% cap; existing exposure consumes "
                           "the budget first"),
            })
            continue
        target_pct = min(target_pct, ind_headroom)

        headroom_total = params.max_total_equity_pct - total_equity_pct
        target_pct = min(target_pct, headroom_total)
        if target_pct <= 0:
            excluded.append({"instrument_id": cand.instrument_id,
                             "reason": "NO_HEADROOM"})
            continue

        is_retained = cand.instrument_id in held
        targets.append(TargetWeight(
            instrument_id=cand.instrument_id,
            industry_code=cand.industry_code,
            weight_pct=target_pct,
            rationale=("retained: still inside the retention buffer" if is_retained
                       else f"new entry at signal rank {cand.signal_rank:.4f}"),
        ))
        total_equity_pct += target_pct
        industry_used[cand.industry_code] = ind_used + target_pct

    cash_pct = Decimal(100) - total_equity_pct
    notes.append(f"retained={len(retained)} new={len(targets) - len(retained)} "
                 f"cash={cash_pct:.2f}%")
    return ConstructionResult(targets=targets, cash_weight_pct=cash_pct,
                              excluded=excluded, notes=notes)


def weights_to_orders(
    *,
    targets: list[TargetWeight],
    params: ConstructionParams,
    price_by_instrument: dict[str, int],
    lot_size_by_instrument: dict[str, int] | None = None,
    held_quantity: dict[str, int] | None = None,
    sellable_quantity: dict[str, int] | None = None,
    equity_value_cents: int | None = None,
) -> list[dict]:
    """§11.1 第 5 步：用决策时点可知的价格把权重差换成**整数股**订单。

    约束不足时保留现金，而不是强行凑满；数量向下取整到整手。
    """

    held_quantity = held_quantity or {}
    sellable_quantity = sellable_quantity or {}
    lot_size_by_instrument = lot_size_by_instrument or {}
    equity = equity_value_cents or params.initial_capital_cents

    orders: list[dict] = []
    target_ids = {t.instrument_id for t in targets}
    for t in targets:
        price = price_by_instrument.get(t.instrument_id)
        if not price:
            continue
        lot = lot_size_by_instrument.get(t.instrument_id, 100)
        target_value = Decimal(equity) * t.weight_pct / Decimal(100)
        target_shares = int(target_value / Decimal(price))
        target_shares -= target_shares % lot
        current = held_quantity.get(t.instrument_id, 0)
        delta = target_shares - current
        if delta > 0:
            orders.append({"instrument_id": t.instrument_id, "side": "BUY",
                           "quantity": delta, "price_cents": price,
                           "rationale": t.rationale})
        elif delta < 0:
            sellable = sellable_quantity.get(t.instrument_id, 0)
            qty = min(-delta, sellable)
            if qty > 0:
                orders.append({"instrument_id": t.instrument_id, "side": "SELL",
                               "quantity": qty, "price_cents": price,
                               "rationale": "reduce toward target weight"})
    # Holdings removed by eligibility or capacity rules need explicit exit orders.  Omitting
    # them would leave the realised portfolio different from the constructed target set.
    for instrument_id in sorted(set(held_quantity) - target_ids):
        price = price_by_instrument.get(instrument_id)
        qty = min(held_quantity[instrument_id], sellable_quantity.get(instrument_id, 0))
        if price and qty > 0:
            orders.append({"instrument_id": instrument_id, "side": "SELL",
                           "quantity": qty, "price_cents": price,
                           "rationale": "exit: instrument is outside the target set"})
    return orders


# ======================================================================
# 日终估值
# ======================================================================
@dataclass(frozen=True, slots=True)
class PositionValue:
    instrument_id: str
    quantity: int
    price_cents: int
    price_basis: str
    staleness_days: int
    value_cents: int


@dataclass(frozen=True, slots=True)
class ValuationResult:
    trading_day: date
    cash_available_cents: int
    cash_frozen_cents: int
    receivables_cents: int
    positions_value_cents: int
    payables_cents: int
    net_value_cents: int
    positions: list[PositionValue]
    invariants: dict
    published: bool

    def as_dict(self) -> dict:
        return {
            "trading_day": self.trading_day.isoformat(),
            "cash_available_cents": self.cash_available_cents,
            "cash_frozen_cents": self.cash_frozen_cents,
            "receivables_cents": self.receivables_cents,
            "positions_value_cents": self.positions_value_cents,
            "payables_cents": self.payables_cents,
            "net_value_cents": self.net_value_cents,
            "positions": [
                {"instrument_id": p.instrument_id, "quantity": p.quantity,
                 "price_cents": p.price_cents, "price_basis": p.price_basis,
                 "staleness_days": p.staleness_days, "value_cents": p.value_cents}
                for p in self.positions
            ],
            "invariants": self.invariants,
            "published": self.published,
        }


def value_positions(
    *,
    lots: list[Lot],
    bars: dict[str, Bar],
    last_valid_price: dict[str, tuple[int, date]],
    trading_day: date,
) -> tuple[list[PositionValue], list[dict]]:
    """按批次聚合持仓并按显式规则估值。

    停牌：用 last_valid_price 中显式记录的最近有效价，并标注停牌天数。
    **绝不**编造当日行情，也**绝不**永久按最后收盘价当作可清算。
    """

    by_instrument: dict[str, int] = {}
    for l in lots:
        if l.quantity_remaining > 0:
            by_instrument[l.instrument_id] = by_instrument.get(
                l.instrument_id, 0) + l.quantity_remaining

    out: list[PositionValue] = []
    issues: list[dict] = []
    for iid, qty in sorted(by_instrument.items()):
        bar = bars.get(iid)
        if bar is not None:
            out.append(PositionValue(iid, qty, bar.close_cents, "CLOSE", 0,
                                     qty * bar.close_cents))
            continue
        recorded = last_valid_price.get(iid)
        if recorded is None:
            issues.append({
                "code": "DATA_NOT_READY",
                "message": f"no price and no recorded last valid price for {iid}",
                "object_id": iid, "retryable": False,
                "repair_action": "record the last valid price or mark it unsupported",
            })
            out.append(PositionValue(iid, qty, 0, "UNSUPPORTED", 0, 0))
            continue
        price, as_of = recorded
        staleness = (trading_day - as_of).days
        out.append(PositionValue(iid, qty, price, "SUSPENDED_LAST_VALID", staleness,
                                 qty * price))
    return out, issues


def compute_valuation(
    *,
    trading_day: date,
    cash_available_cents: int,
    positions: list[PositionValue],
    receivables_cents: int = 0,
    cash_frozen_cents: int = 0,
    payables_cents: int = 0,
    lots: list[Lot] | None = None,
    extra_issues: list[dict] | None = None,
    ledger_invariants: dict[str, bool] | None = None,
) -> ValuationResult:
    """§12.8 日终估值与不变量。

    不变量任一失败 -> published=False，净值不得发布。
    """

    positions_value = sum(p.value_cents for p in positions)
    net_value = (cash_available_cents + cash_frozen_cents + receivables_cents
                 + positions_value - payables_cents)

    violations = list(extra_issues or [])

    if cash_available_cents < 0:
        violations.append({"code": "INSUFFICIENT_CASH",
                           "message": f"cash {cash_available_cents} is negative",
                           "object_id": "-", "retryable": False,
                           "repair_action": "fix the ledger before publishing net value"})
    if net_value < 0:
        violations.append({"code": "DATA_NOT_READY",
                           "message": f"net value {net_value} is negative",
                           "object_id": "-", "retryable": False,
                           "repair_action": "fix the ledger before publishing net value"})

    mismatch = False
    if lots is not None:
        lot_total: dict[str, int] = {}
        for l in lots:
            lot_total[l.instrument_id] = lot_total.get(l.instrument_id, 0) + l.quantity_remaining
        for p in positions:
            if lot_total.get(p.instrument_id, 0) != p.quantity:
                mismatch = True
                violations.append({
                    "code": "DATA_NOT_READY",
                    "message": (f"{p.instrument_id}: position {p.quantity} does not match "
                                f"lot total {lot_total.get(p.instrument_id, 0)}"),
                    "object_id": p.instrument_id, "retryable": False,
                    "repair_action": "reconcile lots against positions before valuing",
                })
        for iid, qty in lot_total.items():
            if qty < 0:
                violations.append({"code": "DATA_NOT_READY",
                                   "message": f"{iid}: negative lot quantity {qty}",
                                   "object_id": iid, "retryable": False,
                                   "repair_action": "fix the ledger"})

    for p in positions:
        if p.price_basis == "UNSUPPORTED":
            violations.append({
                "code": "DATA_NOT_READY",
                "message": f"{p.instrument_id} has no supportable valuation basis",
                "object_id": p.instrument_id, "retryable": False,
                "repair_action": "record a last valid price or exclude it explicitly",
            })

    ledger = ledger_invariants or {}
    for key, message in (
        ("fill_le_order", "one or more fills exceed or disagree with their orders"),
        ("fees_booked_once", "fee charges are duplicated or do not equal fill fees"),
        ("cash_lines_sum_to_balance", "cash entries do not reconcile to fills and opening cash"),
    ):
        if ledger.get(key, True) is False:
            violations.append({
                "code": "DATA_NOT_READY", "message": message, "object_id": "-",
                "retryable": False, "repair_action": "repair the ledger before publishing",
            })

    invariants = {
        "cash_not_overdrawn": cash_available_cents >= 0,
        "positions_not_negative": all(p.quantity >= 0 for p in positions),
        "shares_match_lots": not mismatch,
        "fill_le_order": ledger.get("fill_le_order", True),
        "fees_booked_once": ledger.get("fees_booked_once", True),
        "cash_lines_sum_to_balance": ledger.get("cash_lines_sum_to_balance", True),
        "violations": violations,
        "all_ok": not violations,
    }

    return ValuationResult(
        trading_day=trading_day,
        cash_available_cents=cash_available_cents,
        cash_frozen_cents=cash_frozen_cents,
        receivables_cents=receivables_cents,
        positions_value_cents=positions_value,
        payables_cents=payables_cents,
        net_value_cents=net_value,
        positions=positions,
        invariants=invariants,
        published=not violations,
    )
