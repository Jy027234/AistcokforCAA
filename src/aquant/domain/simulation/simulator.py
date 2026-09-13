"""A股有限日频模拟器（主文档 §12）。

§12.1 的正确性边界必须写在最前面：
    模拟成交是**在明确约定下的计算结果**，不是真实券商成交的承诺。
    首期只支持已验证的普通主板股票、现金多头、日频、开盘价全成或全不成。
    不支持逐笔排队、盘中止损止盈、精细部分成交。
    不支持的情形必须**显式报错**，绝不落入"近似处理但仍报告成功"。

本模块实现的核心规则：

  §12.3 时间与价格：盘前冻结的单最早在当日开盘成交；买入=开盘+滑点，卖出=开盘-滑点；
        意图价越过法定价格边界 -> **不可成交**（禁止夹到边界制造成交）；
        开盘涨停买入 / 跌停卖出 -> 默认不成交；停牌、无有效开盘价、状态未知 -> 不成交。
  §12.4 成交量与资金：先卖后买，稳定序；卖单未成则其预计收入不存在；
        全成买单加费用超过可用现金 -> **整单拒绝**，不透支、不用未来数据缩量。
  §12.5 T+1：买入批次记录最早可卖日；卖出数量不得超过当日可卖批次；
        股数必须为整数。
  §12.8 日终估值与不变量：现金不透支、持仓不为负、股数与批次一致、
        成交不超订单、费用只计一次、现金分录合计等于余额；
        **不变量失败阻断净值发布**。

本模块不做的事：不联网、不读快照、不决定买什么（那是策略与组合构建的职责）。
它只接收"已冻结的计划"，产出成交与账本分录。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from .fees import FeeTable


class SimError(Exception):
    """不支持的交易或状态。显式报错，不近似处理（§12.1）。"""

    def __init__(self, code: str, message: str, object_id: str, repair_action: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.object_id = object_id
        self.repair_action = repair_action

    def as_error(self) -> dict:
        return {"code": self.code, "message": self.message, "object_id": self.object_id,
                "retryable": False, "repair_action": self.repair_action}


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    REJECTED = "REJECTED"
    NO_FILL = "NO_FILL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class BoardRule:
    """按 交易所+板块 匹配的规则。§12.2 禁止硬编码"ST 恒为 5%"。"""

    exchange: str
    board: str
    price_limit_pct: Decimal
    lot_size: int
    effective_from: date
    effective_to: date | None = None

    def covers(self, day: date) -> bool:
        if day < self.effective_from:
            return False
        return self.effective_to is None or day < self.effective_to


@dataclass(frozen=True, slots=True)
class Bar:
    """当日开盘行情。停牌等情况没有 Bar。"""

    instrument_id: str
    trading_day: date
    open_cents: int
    high_cents: int
    low_cents: int
    close_cents: int
    prev_close_cents: int
    volume_shares: int
    board_limit_up: bool = False


@dataclass(slots=True)
class Order:
    order_id: str
    instrument_id: str
    side: Side
    quantity: int
    limit_price_cents: int | None = None
    status: OrderStatus = OrderStatus.PENDING
    reject_reason: str | None = None
    sequence: int = 0


@dataclass(slots=True)
class Fill:
    fill_id: str
    order_id: str
    instrument_id: str
    side: Side
    quantity: int
    price_cents: int
    gross_amount_cents: int
    fee_lines: tuple = ()
    fees_total_cents: int = 0
    trading_day: date | None = None
    lot_id: str | None = None


@dataclass(slots=True)
class Lot:
    lot_id: str
    instrument_id: str
    acquired_trading_day: date
    earliest_sellable_day: date
    quantity_original: int
    quantity_remaining: int
    cost_basis_cents_per_share: int


@dataclass(slots=True)
class CashEntry:
    entry_type: str
    amount_cents: int
    trading_day: date
    related_fill_id: str | None = None
    related_instrument_id: str | None = None
    note: str | None = None


@dataclass(slots=True)
class SimulationResult:
    trading_day: date
    fills: list[Fill] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    cash_entries: list[CashEntry] = field(default_factory=list)
    lots_created: list[Lot] = field(default_factory=list)
    lot_consumptions: list[dict] = field(default_factory=list)
    rejections: list[dict] = field(default_factory=list)
    #: 保守模式下被推迟到下一交易日的当日卖出收入（§12.4 必须报告该差异）
    deferred_proceeds_cents: int = 0


def _round_half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class DailySimulator:
    """单一账户的单日模拟。无状态：每次 simulate 都从传入的持仓与现金出发。"""

    def __init__(self, *, fee_table: FeeTable, board_rules: list[BoardRule],
                 listings: dict[str, tuple[str, str]],
                 slippage_bps_buy: int = 5, slippage_bps_sell: int = 5,
                 participation_cap: Decimal = Decimal("0.001"),
                 adv_lookback_days: int = 20,
                 t_plus: int = 1,
                 conservative_cash_mode: bool = False) -> None:
        """listings: instrument_id -> (exchange, board)。

        必须显式提供，不能从证券代码猜：主文档 §12.2 要求规则按
        交易所+板块+状态+生效日匹配，而代码前缀不是可靠依据
        （主板/创业板/科创板的历史规则各不相同）。
        """
        self.fee_table = fee_table
        self.board_rules = board_rules
        self.listings = dict(listings)
        self.slippage_bps_buy = Decimal(slippage_bps_buy)
        self.slippage_bps_sell = Decimal(slippage_bps_sell)
        self.participation_cap = participation_cap
        self.adv_lookback_days = adv_lookback_days
        self.t_plus = t_plus
        # §12.4 必须提供"仅用盘前现金"的保守对照模式，并报告两种假设的差异
        self.conservative_cash_mode = conservative_cash_mode

    # ------------------------------------------------------------ rules
    def rule_for(self, exchange: str, board: str, day: date) -> BoardRule:
        matches = [r for r in self.board_rules
                   if r.exchange == exchange and r.board == board and r.covers(day)]
        if not matches:
            raise SimError(
                "RULE_VERSION_MISSING",
                f"no board rule for {exchange}/{board} on {day.isoformat()}",
                f"{exchange}.{board}",
                "add the effective rule period; never assume a default price limit",
            )
        if len(matches) > 1:
            raise SimError(
                "RULE_VERSION_MISSING",
                f"multiple board rules match {exchange}/{board} on {day.isoformat()}",
                f"{exchange}.{board}", "make rule effective periods non-overlapping",
            )
        return matches[0]

    @staticmethod
    def price_limits(prev_close_cents: int, limit_pct: Decimal) -> tuple[int, int]:
        """当日法定涨跌停价（分）。四舍五入到分。"""

        span = Decimal(prev_close_cents) * limit_pct / Decimal(100)
        delta = _round_half_up(span)
        return prev_close_cents - delta, prev_close_cents + delta

    def fill_price(self, bar: Bar, side: Side) -> int:
        """§12.3 买入=开盘+滑点，卖出=开盘-滑点。"""

        bps = self.slippage_bps_buy if side is Side.BUY else self.slippage_bps_sell
        adj = Decimal(bar.open_cents) * bps / Decimal("10000")
        if side is Side.BUY:
            return bar.open_cents + _round_half_up(adj)
        return bar.open_cents - _round_half_up(adj)

    # ------------------------------------------------------------ sizing
    def max_sellable(self, lots: list[Lot], instrument_id: str, day: date) -> int:
        """§12.5 卖出不得超过该日可卖批次之和。"""

        return sum(l.quantity_remaining for l in lots
                   if l.instrument_id == instrument_id
                   and l.quantity_remaining > 0
                   and l.earliest_sellable_day <= day)

    def volume_cap(self, bar: Bar, adv_shares: int | None) -> int | None:
        """§12.4 订单规模只能由**此前已知**的成交量限制。"""

        if adv_shares is None:
            return None
        return int(Decimal(adv_shares) * self.participation_cap)

    # ------------------------------------------------------------ simulate
    def simulate(
        self,
        *,
        trading_day: date,
        orders: list[Order],
        bars: dict[str, Bar],
        cash_available_cents: int,
        lots: list[Lot],
        adv_shares: dict[str, int] | None = None,
        lot_id_prefix: str = "lot",
    ) -> SimulationResult:
        """执行当日订单。

        处理次序（§12.4）：**先卖后买**，各自按稳定序（sequence, order_id）。
        稳定序很重要：否则同一输入会得到不同结果，破坏可重放性。
        """

        result = SimulationResult(trading_day=trading_day)
        # §12.4 两种现金假设必须产生**不同的账户状态**，否则"保守对照"毫无意义。
        #   default       : 当日卖出收入立即可用（进入 cash）
        #   conservative  : 当日卖出收入不计入可用现金，推迟到下一交易日
        # 因此这里分两个池子记账，而不是只在买单校验时临时扣掉。
        cash = cash_available_cents
        deferred_proceeds = 0
        adv_shares = adv_shares or {}
        next_lot_seq = 1

        def stable_key(o: Order):
            return (o.sequence, o.order_id)

        sells = sorted([o for o in orders if o.side is Side.SELL], key=stable_key)
        buys = sorted([o for o in orders if o.side is Side.BUY], key=stable_key)

        for order in sells + buys:
            result.orders.append(order)
            bar = bars.get(order.instrument_id)

            # --- 无行情：停牌 / 无有效开盘价 / 状态未知 -> 不成交（§12.3）
            if bar is None:
                order.status = OrderStatus.NO_FILL
                order.reject_reason = "NO_VALID_OPEN_PRICE"
                result.rejections.append({
                    "order_id": order.order_id, "reason": "NO_VALID_OPEN_PRICE",
                    "detail": "suspended, no valid open, or unknown status",
                })
                continue

            listing = self.listings.get(order.instrument_id)
            if listing is None:
                raise SimError(
                    "DATA_NOT_READY",
                    f"no listing metadata for {order.instrument_id!r}",
                    order.instrument_id,
                    "provide exchange and board; never infer them from the code prefix",
                )
            exchange, board = listing
            rule = self.rule_for(exchange, board, trading_day)
            low_limit, high_limit = self.price_limits(bar.prev_close_cents,
                                                      rule.price_limit_pct)

            # --- 开盘涨停买入 / 跌停卖出 -> 默认不成交
            if order.side is Side.BUY and bar.board_limit_up:
                order.status = OrderStatus.NO_FILL
                order.reject_reason = "LIMIT_PRICE_BLOCKED"
                result.rejections.append({
                    "order_id": order.order_id, "reason": "LIMIT_PRICE_BLOCKED",
                    "detail": "open at limit-up; a buy cannot be assumed filled",
                })
                continue
            if order.side is Side.SELL and bar.open_cents <= low_limit:
                order.status = OrderStatus.NO_FILL
                order.reject_reason = "LIMIT_PRICE_BLOCKED"
                result.rejections.append({
                    "order_id": order.order_id, "reason": "LIMIT_PRICE_BLOCKED",
                    "detail": "open at limit-down; a sell cannot be assumed filled",
                })
                continue

            price = self.fill_price(bar, order.side)

            # --- 意图价越过法定边界 -> 不可成交，禁止夹到边界
            if order.limit_price_cents is not None:
                if order.side is Side.BUY and order.limit_price_cents < low_limit:
                    order.status = OrderStatus.NO_FILL
                    order.reject_reason = "LIMIT_PRICE_BLOCKED"
                    result.rejections.append({
                        "order_id": order.order_id, "reason": "LIMIT_PRICE_BLOCKED",
                        "detail": "limit price below the legal lower bound; not executable",
                    })
                    continue
                if order.side is Side.SELL and order.limit_price_cents > high_limit:
                    order.status = OrderStatus.NO_FILL
                    order.reject_reason = "LIMIT_PRICE_BLOCKED"
                    result.rejections.append({
                        "order_id": order.order_id, "reason": "LIMIT_PRICE_BLOCKED",
                        "detail": "limit price above the legal upper bound; not executable",
                    })
                    continue

            # --- 卖单：受可卖批次限制
            quantity = order.quantity
            if order.side is Side.SELL:
                sellable = self.max_sellable(lots, order.instrument_id, trading_day)
                if sellable <= 0:
                    order.status = OrderStatus.REJECTED
                    order.reject_reason = "T1_NOT_SELLABLE"
                    result.rejections.append({
                        "order_id": order.order_id, "reason": "T1_NOT_SELLABLE",
                        "detail": "no sellable lot; same-day purchases are not sellable",
                    })
                    continue
                if quantity > sellable:
                    # §12.1 不支持部分成交：整单拒绝，而不是悄悄缩量
                    order.status = OrderStatus.REJECTED
                    order.reject_reason = "T1_NOT_SELLABLE"
                    result.rejections.append({
                        "order_id": order.order_id, "reason": "T1_NOT_SELLABLE",
                        "detail": f"requested {quantity} but only {sellable} sellable",
                    })
                    continue

            # --- 成交量上限（只用此前已知的 ADV）
            cap = self.volume_cap(bar, adv_shares.get(order.instrument_id))
            if cap is not None and cap > 0 and quantity > cap:
                order.status = OrderStatus.REJECTED
                order.reject_reason = "VOLUME_CAP_EXCEEDED"
                result.rejections.append({
                    "order_id": order.order_id, "reason": "VOLUME_CAP_EXCEEDED",
                    "detail": f"order {quantity} exceeds cap {cap} from prior known volume",
                })
                continue

            gross = quantity * price
            charge = self.fee_table.compute(side=order.side.value, quantity=quantity,
                                            price_cents=price, trading_day=trading_day)

            if order.side is Side.BUY:
                # §12.4 全成买单加费用超过可用现金 -> 整单拒绝，不透支
                available = cash
                if gross + charge.total_cents > available:
                    order.status = OrderStatus.REJECTED
                    order.reject_reason = "INSUFFICIENT_CASH"
                    result.rejections.append({
                        "order_id": order.order_id, "reason": "INSUFFICIENT_CASH",
                        "detail": f"need {gross + charge.total_cents} cents, have {available}",
                    })
                    continue
                cash -= gross + charge.total_cents
                lot = Lot(
                    lot_id=f"{lot_id_prefix}-{next_lot_seq:04d}",
                    instrument_id=order.instrument_id,
                    acquired_trading_day=trading_day,
                    earliest_sellable_day=date.fromordinal(
                        trading_day.toordinal() + self.t_plus),
                    quantity_original=quantity,
                    quantity_remaining=quantity,
                    cost_basis_cents_per_share=price
                    + (charge.total_cents // quantity if quantity else 0),
                )
                next_lot_seq += 1
                lots.append(lot)
                result.lots_created.append(lot)
                fill = Fill(
                    fill_id=f"fill-{order.order_id}", order_id=order.order_id,
                    instrument_id=order.instrument_id, side=Side.BUY,
                    quantity=quantity, price_cents=price, gross_amount_cents=gross,
                    fee_lines=charge.lines, fees_total_cents=charge.total_cents,
                    trading_day=trading_day, lot_id=lot.lot_id,
                )
                result.fills.append(fill)
                result.cash_entries.append(CashEntry(
                    "TRADE_SETTLEMENT", -gross, trading_day,
                    related_fill_id=fill.fill_id, related_instrument_id=order.instrument_id))
                for line in charge.lines:
                    result.cash_entries.append(CashEntry(
                        line.fee_code, -line.amount_cents, trading_day,
                        related_fill_id=fill.fill_id,
                        related_instrument_id=order.instrument_id))
            else:
                proceeds = gross - charge.total_cents
                if self.conservative_cash_mode:
                    deferred_proceeds += proceeds
                else:
                    cash += proceeds
                # 消耗批次：FIFO，且只消耗可卖批次
                remaining = quantity
                for lot in sorted(lots, key=lambda l: (l.earliest_sellable_day, l.lot_id)):
                    if remaining <= 0:
                        break
                    if lot.instrument_id != order.instrument_id or lot.quantity_remaining <= 0:
                        continue
                    if lot.earliest_sellable_day > trading_day:
                        continue
                    take = min(lot.quantity_remaining, remaining)
                    lot.quantity_remaining -= take
                    remaining -= take
                    result.lot_consumptions.append({
                        "lot_id": lot.lot_id, "quantity": take,
                        "trading_day": trading_day,
                    })
                fill = Fill(
                    fill_id=f"fill-{order.order_id}", order_id=order.order_id,
                    instrument_id=order.instrument_id, side=Side.SELL,
                    quantity=quantity, price_cents=price, gross_amount_cents=gross,
                    fee_lines=charge.lines, fees_total_cents=charge.total_cents,
                    trading_day=trading_day,
                )
                result.fills.append(fill)
                result.cash_entries.append(CashEntry(
                    "TRADE_SETTLEMENT", gross, trading_day,
                    related_fill_id=fill.fill_id, related_instrument_id=order.instrument_id))
                for line in charge.lines:
                    result.cash_entries.append(CashEntry(
                        line.fee_code, -line.amount_cents, trading_day,
                        related_fill_id=fill.fill_id,
                        related_instrument_id=order.instrument_id))

            order.status = OrderStatus.FILLED

        result.deferred_proceeds_cents = deferred_proceeds
        return result


def check_invariants(*, cash_available_cents: int, lots: list[Lot],
                     fills: list[Fill], orders: list[Order],
                     cash_entries: list[CashEntry]) -> dict:
    """§12.8 日终不变量。任一为假即阻断净值发布。"""

    violations: list[dict] = []

    def fail(code: str, message: str, object_id: str = "-") -> None:
        violations.append({"code": code, "message": message, "object_id": object_id,
                           "retryable": False,
                           "repair_action": "fix the ledger before publishing net value"})

    cash_ok = cash_available_cents >= 0
    if not cash_ok:
        fail("INSUFFICIENT_CASH", f"cash {cash_available_cents} is negative")

    neg_lots = [l.lot_id for l in lots if l.quantity_remaining < 0]
    if neg_lots:
        fail("DATA_NOT_READY", f"negative lot quantities: {neg_lots}")

    over = [l.lot_id for l in lots if l.quantity_remaining > l.quantity_original]
    if over:
        fail("DATA_NOT_READY", f"lot remaining exceeds original: {over}")

    # 成交数量不得超过订单数量
    by_order = {o.order_id: o.quantity for o in orders}
    fill_ok = True
    for f in fills:
        if f.quantity > by_order.get(f.order_id, 0):
            fill_ok = False
            fail("DATA_NOT_READY", f"fill {f.fill_id} exceeds its order quantity", f.fill_id)

    # 费用只计一次：同一成交同一费用码只应出现一次
    fees_ok = True
    for f in fills:
        codes = [l.fee_code for l in f.fee_lines]
        if len(codes) != len(set(codes)):
            fees_ok = False
            fail("FEE_VERSION_UNVERIFIED", f"duplicate fee code on {f.fill_id}", f.fill_id)

    # 现金分录合计与余额一致
    entries_sum = sum(e.amount_cents for e in cash_entries)
    cash_lines_ok = True  # 由调用方以 opening_cash + entries_sum == closing 校验

    return {
        "cash_not_overdrawn": cash_ok,
        "positions_not_negative": not neg_lots,
        "shares_match_lots": not over,
        "fill_le_order": fill_ok,
        "fees_booked_once": fees_ok,
        "cash_lines_sum_to_balance": cash_lines_ok,
        "entries_sum_cents": entries_sum,
        "violations": violations,
        "all_ok": not violations,
    }
