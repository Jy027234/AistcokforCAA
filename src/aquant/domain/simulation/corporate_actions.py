"""公司行为处理（主文档 §12.7）。

核心要求：
  * 现金分红的**除权日与到账日必须分开**：除权日确认应收并体现价格影响，
    到账日应收转现金，**不得重复计入收益**（S07）。
  * 权利由**登记日**的持仓决定，不是除权日或到账日。
  * 无法正确核验的复杂公司行为（配股、换股、分立）必须**显式标记未支持**，
    而不是近似处理后报告成功（S08）。
  * 退市股票保留历史与退出事件，用**有依据的终值或明确的保守情形**，
    不得永久按最后收盘价当作可清算（S08）。

税的处理：§12.6 明确在未实现完整红利税处理前，必须输出**税前或保守**口径，
并标注清楚，不得称为精确的个人税后收益。本模块的 tax_treatment 字段即为此。
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from datetime import date

from .simulator import Lot, SimError


#: 一元 = 10^6 微元。分红金额按**整数微元**记账。
MICROS_PER_YUAN = 1_000_000
#: 一元 = 100 分
CENTS_PER_YUAN = 100


@dataclass(frozen=True, slots=True)
class CashDividend:
    action_id: str
    instrument_id: str
    record_date: date
    ex_date: date
    pay_date: date
    #: 每股现金红利，单位**整数微元**（10^-6 元）。这是权威单位。
    #:
    #: 真实分红往往不是整数分：贵州茅台 2025 年度每股 28.02423 元
    #: = 2,802.423 分。若只按整数分存储，误差会随股数线性放大，
    #: 而账面看起来仍然"对得上"——这是最危险的一类错误。
    #: 交易所披露要求精确到厘（10^-3 元），微元足以精确表示。
    cash_per_share_micros: int = 0
    #: §12.6 未实现完整红利税处理前必须标注 PRE_TAX 或 CONSERVATIVE
    tax_treatment: str = "PRE_TAX"
    #: 过渡构造参数：按"分"传入每股红利（内部换算为微元）。
    #:
    #: 存在的唯一理由是让既有调用点（合成样例、测试）不必立刻改写；
    #: 真实公告解析一律走 `cash_per_share_micros`，因为真实分红
    #: 常常不是整数分。**新增代码请用微元。**
    cash_per_share_cents_input: InitVar[int | None] = None

    @property
    def cash_per_share_cents(self) -> int:
        """每股红利的分值（四舍五入到分）。

        仅用于展示与"以分为单位"的旧接口。**计算一律用微元**：
        用它去乘股数会把舍入误差放大，这不是"精度小问题"，
        而是账面金额与实际派发金额不符。
        """

        return (self.cash_per_share_micros + MICROS_PER_YUAN // CENTS_PER_YUAN // 2) \
            // (MICROS_PER_YUAN // CENTS_PER_YUAN)

    def total_micros(self, shares: int) -> int:
        """给定股数下的应收总额（微元），精确无舍入。"""

        return shares * self.cash_per_share_micros

    def total_cents_exact(self, shares: int) -> int | None:
        """应收总额的分值；不能整除到分时返回 None，**不四舍五入**。

        资金账本以分为最小单位。若总额不是整数分，说明还有不足一分的
        尾差需要单独处理——返回 None 让调用方显式决定，而不是悄悄抹掉。
        """

        micros = self.total_micros(shares)
        if micros % (MICROS_PER_YUAN // CENTS_PER_YUAN) != 0:
            return None
        return micros // (MICROS_PER_YUAN // CENTS_PER_YUAN)

    def __post_init__(self, cash_per_share_cents_input: int | None) -> None:
        if cash_per_share_cents_input is not None:
            if self.cash_per_share_micros:
                raise SimError(
                    "CORPORATE_ACTION_UNSUPPORTED",
                    "deliver the per-share dividend either in micros or in cents, not both",
                    self.action_id,
                    "use cash_per_share_micros; the cents argument is transitional",
                )
            object.__setattr__(self, "cash_per_share_micros",
                               cash_per_share_cents_input
                               * (MICROS_PER_YUAN // CENTS_PER_YUAN))
        if self.cash_per_share_micros <= 0:
            raise SimError(
                "CORPORATE_ACTION_UNSUPPORTED",
                f"per-share dividend must be positive, got {self.cash_per_share_micros} micros",
                self.action_id,
                "record the per-share cash dividend in micros (10^-6 yuan)",
            )
        if not (self.record_date <= self.ex_date <= self.pay_date):
            raise SimError(
                "CORPORATE_ACTION_UNSUPPORTED",
                f"dates out of order: record={self.record_date} ex={self.ex_date} "
                f"pay={self.pay_date}",
                self.action_id,
                "correct record/ex/pay dates; they must be non-decreasing",
            )
        if self.tax_treatment not in {"PRE_TAX", "CONSERVATIVE", "VERIFIED"}:
            raise SimError("CORPORATE_ACTION_UNSUPPORTED",
                           f"unknown tax treatment {self.tax_treatment!r}", self.action_id,
                           "declare PRE_TAX, CONSERVATIVE or VERIFIED")


@dataclass(frozen=True, slots=True)
class UnsupportedAction:
    """无法核验的复杂公司行为。必须显式阻塞，不近似。"""

    action_id: str
    instrument_id: str
    action_type: str
    reason: str


@dataclass(frozen=True, slots=True)
class DividendOutcome:
    receivable_cents: int
    cash_delta_cents: int
    entitlement_shares: int
    recognised: bool
    note: str
    #: 本次推进发生在除权日还是到账日；未发生推进时为 None。
    #:
    #: 落库方需要据此区分"新增应收"与"应收结清"两件事：只凭
    #: receivable_cents / cash_delta_cents 的正负去猜，会把
    #: 一次到账误判成新增应收，从而让同一笔分红在净值里计两次。
    stage: str | None = None
    #: 确认应收时应约定的到账日（除权日推进时才有值）
    expected_settlement_on: date | None = None


@dataclass(frozen=True, slots=True)
class DividendEntitlement:
    """Immutable record-date share count reused on ex-date and pay-date."""

    action_id: str
    instrument_id: str
    record_date: date
    shares: int


def entitled_shares(lots: list[Lot], instrument_id: str, record_date: date) -> int:
    """登记日收盘时持有的股数。

    只有**在登记日当日或之前买入**的批次才有分红权利；
    登记日之后买入的批次不享有（S07 的负例）。
    """

    return sum(l.quantity_remaining for l in lots
               if l.instrument_id == instrument_id
               and l.acquired_trading_day <= record_date)


def record_dividend_entitlement(
    action: CashDividend, *, lots: list[Lot], recorded_on: date
) -> DividendEntitlement:
    if recorded_on != action.record_date:
        raise SimError(
            "CORPORATE_ACTION_UNSUPPORTED",
            f"entitlement must be recorded on {action.record_date}, got {recorded_on}",
            action.action_id,
            "record the position at the official record-date close",
        )
    return DividendEntitlement(
        action_id=action.action_id,
        instrument_id=action.instrument_id,
        record_date=recorded_on,
        shares=entitled_shares(lots, action.instrument_id, recorded_on),
    )


def apply_cash_dividend(
    action: CashDividend,
    *,
    entitlement: DividendEntitlement,
    trading_day: date,
) -> DividendOutcome:
    """在给定交易日推进现金分红。

    行为取决于 trading_day 落在哪个阶段：
      trading_day == ex_date   -> 确认应收，现金不变
      trading_day == pay_date  -> 应收转现金
      其他                     -> 无动作

    权利使用登记日已经固化的 entitlement；之后卖出不会抹掉该权利。
    """

    if (entitlement.action_id != action.action_id
            or entitlement.instrument_id != action.instrument_id
            or entitlement.record_date != action.record_date):
        raise SimError("CORPORATE_ACTION_UNSUPPORTED",
                       "dividend entitlement does not match the action",
                       action.action_id, "load the entitlement recorded for this action")
    shares = entitlement.shares
    if shares == 0:
        return DividendOutcome(0, 0, 0, False,
                               "no entitlement: position was not held on the record date")

    # 精确保留尾差：资金账本以分为最小单位，若按分四舍五入，
    # 每股的舍入误差会随股数放大。这里按微元计算总额，
    # 只有在**确实**能整除到分时才落分；否则显式报未支持，
    # 而不是悄悄抹掉不足一分的部分。
    micros = action.total_micros(shares)
    total = action.total_cents_exact(shares)
    if total is None:
        raise SimError(
            "CORPORATE_ACTION_UNSUPPORTED",
            (f"dividend total {micros} micros ({micros / MICROS_PER_YUAN:.6f} yuan) "
             f"is not a whole number of cents for {shares} shares; the cash ledger "
             "cannot represent the remainder exactly"),
            action.action_id,
            ("record the per-share amount at cent precision, or add explicit "
             "rounding-remainder handling before booking this dividend"),
        )

    if trading_day == action.ex_date and action.ex_date == action.pay_date:
        # 除权日与到账日同日：这是 A 股的常见情形（登记日次一交易日除息、
        # 同日发放）。此时应收在同一天确认并结清，现金直接增加。
        #
        # 不能只走 EX_DATE 分支了事：那一天账面上会留下一条永不结清的应收，
        # 现金永远少一笔，而对账"应收 + 现金"的总和又是对的，
        # 于是这个错误可以长期不被发现。
        return DividendOutcome(
            receivable_cents=0, cash_delta_cents=total, entitlement_shares=shares,
            recognised=True,
            stage="EX_AND_PAY_DATE",
            expected_settlement_on=action.pay_date,
            note=("ex-date and pay-date coincide: the receivable is recognised and settled "
                  f"on the same day, cash increases immediately (tax treatment: "
                  f"{action.tax_treatment})"),
        )
    if trading_day == action.ex_date:
        return DividendOutcome(
            receivable_cents=total, cash_delta_cents=0, entitlement_shares=shares,
            recognised=True,
            stage="EX_DATE",
            expected_settlement_on=action.pay_date,
            note=("receivable recognised on the ex-date; cash is unchanged until the pay date "
                  f"({action.pay_date.isoformat()}, tax treatment: {action.tax_treatment})"),
        )
    if trading_day == action.pay_date:
        return DividendOutcome(
            receivable_cents=0, cash_delta_cents=total, entitlement_shares=shares,
            recognised=True,
            stage="PAY_DATE",
            note=("receivable settled into cash on the pay date; the ex-date already recognised "
                  "it, so income is not counted twice"),
        )
    return DividendOutcome(0, 0, shares, False,
                           f"no dividend event on {trading_day.isoformat()}")


def assert_action_supported(action: UnsupportedAction | CashDividend) -> None:
    """不支持的复杂公司行为必须显式阻塞（§12.7、S08）。"""

    if isinstance(action, UnsupportedAction):
        raise SimError(
            "CORPORATE_ACTION_UNSUPPORTED",
            f"{action.action_type} on {action.instrument_id} is not supported: {action.reason}",
            action.action_id,
            "stop producing new performance conclusions for this instrument and mark it pending",
        )


def terminal_value_cents(*, last_close_cents: int, delisted: bool) -> dict:
    """退市股票的终值处理。

    §12.7：保留历史与退出事件，用有依据的终值或明确的保守情形；
    **不得**永久按最后收盘价当作可清算。因此退市时返回 UNSUPPORTED 且不给数值，
    由上层显式选择一个保守情形并记录依据。
    """

    if delisted:
        return {
            "basis": "UNSUPPORTED",
            "value_cents": None,
            "note": ("delisted: the last close is not a liquidation value; choose an explicit "
                     "conservative scenario and record its basis"),
        }
    return {"basis": "CLOSE", "value_cents": last_close_cents, "note": "still listed"}
