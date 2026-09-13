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

from dataclasses import dataclass
from datetime import date

from .simulator import Lot, SimError


@dataclass(frozen=True, slots=True)
class CashDividend:
    action_id: str
    instrument_id: str
    record_date: date
    ex_date: date
    pay_date: date
    cash_per_share_cents: int
    #: §12.6 未实现完整红利税处理前必须标注 PRE_TAX 或 CONSERVATIVE
    tax_treatment: str = "PRE_TAX"

    def __post_init__(self) -> None:
        if self.cash_per_share_cents < 0:
            raise SimError("CORPORATE_ACTION_UNSUPPORTED", "negative dividend per share",
                           self.action_id, "fix the corporate action record")
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


def entitled_shares(lots: list[Lot], instrument_id: str, record_date: date) -> int:
    """登记日收盘时持有的股数。

    只有**在登记日当日或之前买入**的批次才有分红权利；
    登记日之后买入的批次不享有（S07 的负例）。
    """

    return sum(l.quantity_original for l in lots
               if l.instrument_id == instrument_id
               and l.acquired_trading_day <= record_date)


def apply_cash_dividend(
    action: CashDividend,
    *,
    lots: list[Lot],
    trading_day: date,
    entitlements_recorded_on: date,
) -> DividendOutcome:
    """在给定交易日推进现金分红。

    行为取决于 trading_day 落在哪个阶段：
      trading_day == ex_date   -> 确认应收，现金不变
      trading_day == pay_date  -> 应收转现金
      其他                     -> 无动作

    权利由 entitlements_recorded_on（登记日）的持仓决定。
    """

    shares = entitled_shares(lots, action.instrument_id, entitlements_recorded_on)
    if shares == 0:
        return DividendOutcome(0, 0, 0, False,
                               "no entitlement: position was not held on the record date")

    total = shares * action.cash_per_share_cents

    if trading_day == action.ex_date:
        return DividendOutcome(
            receivable_cents=total, cash_delta_cents=0, entitlement_shares=shares,
            recognised=True,
            note=("receivable recognised on the ex-date; cash is unchanged until the pay date "
                  f"(tax treatment: {action.tax_treatment})"),
        )
    if trading_day == action.pay_date:
        return DividendOutcome(
            receivable_cents=0, cash_delta_cents=total, entitlement_shares=shares,
            recognised=True,
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
