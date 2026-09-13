"""费用模型（主文档 §12.6）。

三条硬要求，每条都有对应测试：

1. **按日期生效的版本**：费用表属于某个 fee_version，规则随生效日变化
   （S09 要求"规则生效日前后使用相应日期的配置"）。
2. **最低佣金不能漏，也不能重复计**：小额订单触发最低佣金补足；
   同一成交同一费用码只能计一次（数据库唯一约束 + 本模块的构造方式共同保证）。
3. **金额用整数分与 Decimal，禁止二进制浮点**：
   "永远不要依赖二进制浮点来保持现金平衡"。

本配置中的费率是**合成测试费率**，明确禁止用于真实数据的正式研究（§12.6）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal


class FeeError(Exception):
    def __init__(self, code: str, message: str, repair_action: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.repair_action = repair_action


def to_cents(value: Decimal) -> int:
    """Decimal -> 整数分。四舍五入到分，且必须是有限值。"""

    if not value.is_finite():
        raise FeeError("FEE_VERSION_UNVERIFIED", f"non-finite amount {value!r}",
                       "compute fees from finite decimals only")
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """一个日期生效的费用版本。所有费率用 Decimal 表示。"""

    fee_version: str
    effective_from: date
    effective_to: date | None
    commission_rate: Decimal
    commission_min_cents: int
    stamp_duty_rate_sell: Decimal
    transfer_fee_rate: Decimal
    #: 合成测试费率标记。为 True 时禁止用于正式研究（§12.6）
    synthetic_test_rate: bool = False

    def __post_init__(self) -> None:
        for name in ("commission_rate", "stamp_duty_rate_sell", "transfer_fee_rate"):
            rate = getattr(self, name)
            if not isinstance(rate, Decimal):
                raise FeeError("FEE_VERSION_UNVERIFIED",
                               f"{name} must be a Decimal, got {type(rate).__name__}",
                               "never express fee rates as binary floats")
            if rate < 0:
                raise FeeError("FEE_VERSION_UNVERIFIED", f"{name} is negative",
                               "fix the fee schedule")
        if self.commission_min_cents < 0:
            raise FeeError("FEE_VERSION_UNVERIFIED", "negative minimum commission",
                           "fix the fee schedule")

    def covers(self, day: date) -> bool:
        if day < self.effective_from:
            return False
        return self.effective_to is None or day < self.effective_to

    def assert_usable_for_formal_research(self) -> None:
        """§12.6 合成费率不得用于真实数据的正式研究。"""

        if self.synthetic_test_rate:
            raise FeeError(
                "FEE_VERSION_UNVERIFIED",
                f"fee version {self.fee_version!r} is a synthetic test rate",
                "build a verified fee table during M0 before producing formal research",
            )


@dataclass(frozen=True, slots=True)
class FeeLine:
    fee_code: str
    amount_cents: int
    fee_version: str
    rate_basis: str | None = None


@dataclass(frozen=True, slots=True)
class FeeCharge:
    lines: tuple[FeeLine, ...]
    total_cents: int

    def breakdown(self) -> list[dict]:
        return [
            {"fee_code": l.fee_code, "amount_cents": l.amount_cents,
             "fee_version": l.fee_version, "rate_basis": l.rate_basis}
            for l in self.lines
        ]


class FeeTable:
    """按交易日解析生效版本的费率表。"""

    def __init__(self, schedules: list[FeeSchedule]) -> None:
        if not schedules:
            raise FeeError("FEE_VERSION_UNVERIFIED", "fee table is empty",
                           "provide at least one fee schedule")
        self._schedules = sorted(schedules, key=lambda s: s.effective_from)

    def schedule_for(self, day: date, *, fee_version: str | None = None) -> FeeSchedule:
        candidates = [s for s in self._schedules if s.covers(day)]
        if fee_version:
            candidates = [s for s in candidates if s.fee_version == fee_version]
        if not candidates:
            raise FeeError(
                "FEE_VERSION_UNVERIFIED",
                f"no fee schedule covers {day.isoformat()}"
                + (f" for version {fee_version!r}" if fee_version else ""),
                "add the missing effective period; do not reuse an unrelated rate",
            )
        # 同一日多个候选意味着规则冲突，宁可报错也不猜
        if len(candidates) > 1:
            raise FeeError(
                "FEE_VERSION_UNVERIFIED",
                f"multiple fee schedules cover {day.isoformat()}: "
                f"{[c.fee_version for c in candidates]}",
                "make effective periods non-overlapping",
            )
        return candidates[0]

    # ------------------------------------------------------------ compute
    def compute(self, *, side: str, quantity: int, price_cents: int,
                trading_day: date, fee_version: str | None = None) -> FeeCharge:
        """计算一笔成交的全部费用。

        side: BUY / SELL。§12.6 印花税仅卖出方计收。
        """

        if side not in {"BUY", "SELL"}:
            raise FeeError("FEE_VERSION_UNVERIFIED", f"unknown side {side!r}",
                           "use BUY or SELL")
        if quantity <= 0 or price_cents <= 0:
            raise FeeError("FEE_VERSION_UNVERIFIED",
                           "quantity and price must be positive",
                           "compute fees only for a real fill")

        sched = self.schedule_for(trading_day, fee_version=fee_version)
        gross = Decimal(quantity) * Decimal(price_cents)

        lines: list[FeeLine] = []

        # 佣金：按成交额比例，不足最低值则补足
        commission = gross * sched.commission_rate
        commission_cents = to_cents(commission)
        if commission_cents < sched.commission_min_cents:
            # 拆成两行，让"比例部分"与"补足部分"都可审计（§12.6 最低佣金不得漏计）
            lines.append(FeeLine("COMMISSION", commission_cents, sched.fee_version,
                                 f"rate={sched.commission_rate}"))
            topup = sched.commission_min_cents - commission_cents
            lines.append(FeeLine("MIN_COMMISSION_TOPUP", topup, sched.fee_version,
                                 f"minimum={sched.commission_min_cents}"))
        else:
            lines.append(FeeLine("COMMISSION", commission_cents, sched.fee_version,
                                 f"rate={sched.commission_rate}"))

        # 印花税：仅卖出
        if side == "SELL":
            duty = to_cents(gross * sched.stamp_duty_rate_sell)
            if duty:
                lines.append(FeeLine("STAMP_DUTY", duty, sched.fee_version,
                                     f"rate={sched.stamp_duty_rate_sell}"))

        # 过户费：双向
        transfer = to_cents(gross * sched.transfer_fee_rate)
        if transfer:
            lines.append(FeeLine("TRANSFER_FEE", transfer, sched.fee_version,
                                 f"rate={sched.transfer_fee_rate}"))

        return FeeCharge(lines=tuple(lines), total_cents=sum(l.amount_cents for l in lines))


def synthetic_fee_table() -> FeeTable:
    """资料包自带的合成测试费率表。

    对应 configs/research.example.yaml 的 simulation.fees，
    并体现 §12.6 提到的印花税减半（2023-08-28 起）这一**日期生效**变化：
    表中保留新旧两个版本，正是为了让 S09 有可测的对象。
    """

    return FeeTable([
        # 减半之前的版本：仅用于验证"按生效日取用相应配置"
        FeeSchedule(
            fee_version="fee-syn-v1-pre2023",
            effective_from=date(2020, 1, 1),
            effective_to=date(2023, 8, 28),
            commission_rate=Decimal("0.00025"),
            commission_min_cents=500,
            stamp_duty_rate_sell=Decimal("0.001"),
            transfer_fee_rate=Decimal("0.00001"),
            synthetic_test_rate=True,
        ),
        # 2023-08-28 起印花税减半
        FeeSchedule(
            fee_version="fee-syn-v1",
            effective_from=date(2023, 8, 28),
            effective_to=None,
            commission_rate=Decimal("0.00025"),
            commission_min_cents=500,
            stamp_duty_rate_sell=Decimal("0.0005"),
            transfer_fee_rate=Decimal("0.00001"),
            synthetic_test_rate=True,
        ),
    ])
