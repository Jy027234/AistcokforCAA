"""Frozen M/E/H/B comparisons through the existing plan/ledger implementation.

This module never collects market data or confirms an interactive user's draft.
An engineering replay is labelled as such, and requires an explicitly supplied
human authorization subject. Results and preregistrations are immutable files;
the existing PlanService remains the single implementation of money and trades.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from ..data.reader import SnapshotReader
from ..portfolio.construction import Candidate, ConstructionParams, TargetWeight
from ..portfolio.plan import PlanService, confirmer_is_human
from ..simulation.corporate_actions import CashDividend
from ..simulation.simulator import DailySimulator
from ..strategy.s1 import build_s1_signals

MARKET_TZ = ZoneInfo("Asia/Shanghai")
COMPARISON_VERSION = "comparison-v3"


class ComparisonError(ValueError):
    pass


def _json_default(value):
    if isinstance(value, (Decimal, date, datetime, Path)):
        return str(value)
    raise TypeError(type(value).__name__)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=_json_default, allow_nan=False).encode("utf-8")


def _pct(numerator: int | Decimal, denominator: int | Decimal) -> str | None:
    return (str(Decimal(numerator) / Decimal(denominator) * 100)
            if denominator else None)


@dataclass(frozen=True)
class ReplayStep:
    trading_day: date
    decision_snapshot_id: str
    execution_snapshot_id: str


@dataclass(frozen=True)
class VerifiedInvestigation:
    event_id: str
    instrument_id: str
    available_at: datetime
    verified_at: datetime
    source_uri: str
    source_sha256: str
    verification_locator: str
    event_type: str = "MAJOR_REGULATORY_INVESTIGATION"

    def __post_init__(self):
        if (self.event_type != "MAJOR_REGULATORY_INVESTIGATION"
                or self.available_at.tzinfo is None or self.verified_at.tzinfo is None
                or not self.source_uri or not self.verification_locator
                or len(self.source_sha256.removeprefix("sha256:")) != 64):
            raise ComparisonError("E1 requires a verified, located, timezone-aware investigation")


@dataclass(frozen=True)
class ComparisonRunSpec:
    experiment_version: str
    hypothesis: str
    steps: tuple[ReplayStep, ...]
    universe: tuple[str, ...]
    human_selection: tuple[str, ...]
    human_origin: str
    initial_cash_cents: int = 100_000_000
    e1_window_trading_days: int = 10
    verified_events: tuple[VerifiedInvestigation, ...] = ()
    limitations: tuple[str, ...] = ()
    run_kind: str = "ENGINEERING_REPLAY"
    human_equity_pct: Decimal = Decimal("80")
    # Every frozen date must have explicit user decisions when claiming HUMAN.
    human_decision_refs: tuple[str, ...] = ()

    def __post_init__(self):
        if (not self.experiment_version or not self.hypothesis or not self.steps
                or not self.universe or len(set(self.universe)) != len(self.universe)
                or self.initial_cash_cents <= 0 or self.e1_window_trading_days < 1
                or self.run_kind not in {"ENGINEERING_REPLAY", "FORWARD_SIMULATION"}
                or self.human_origin not in {"ENGINEERING_REPLAY", "USER_CONFIRMED"}
                or not self.human_equity_pct.is_finite()
                or self.human_equity_pct < 0 or self.human_equity_pct > 100):
            raise ComparisonError("invalid comparison preregistration")
        days = [step.trading_day for step in self.steps]
        if days != sorted(set(days)):
            raise ComparisonError("comparison steps must be strictly chronological")
        if (len(set(self.human_selection)) != len(self.human_selection)
                or not set(self.human_selection) <= set(self.universe)):
            raise ComparisonError("H selections must be unique members of the frozen pool")
        if self.human_origin == "USER_CONFIRMED" and len(self.human_decision_refs) != len(self.steps):
            raise ComparisonError("real H decisions need a durable decision reference for every step")

    def as_dict(self) -> dict:
        return json.loads(_canonical(asdict(self)))


class ComparisonStore:
    """Append-only records: a rerun may verify an identical record, never replace it."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def write_once(self, name: str, payload: dict) -> Path:
        if Path(name).name != name or not name.endswith(".json"):
            raise ComparisonError("invalid immutable record name")
        path = self.root / name
        encoded = _canonical(payload)
        try:
            with path.open("xb") as handle:
                handle.write(encoded)
        except FileExistsError:
            if path.read_bytes() != encoded:
                raise ComparisonError("immutable comparison record already exists with different content")
        return path

    def read(self, run_id: str) -> dict:
        if not run_id.startswith("cmp-") or Path(run_id).name != run_id:
            raise ComparisonError("invalid comparison id")
        return json.loads((self.root / f"{run_id}.result.json").read_text(encoding="utf-8"))

    def list_results(self) -> list[dict]:
        return [json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(self.root.glob("cmp-*.result.json"), reverse=True)]


def active_e1_events(*, events: tuple[VerifiedInvestigation, ...], calendar: list[date],
                     decision_cutoff: datetime, execution_day: date, window: int) -> list[VerifiedInvestigation]:
    """Count actual calendar sessions; after-close availability starts next session."""
    if window < 1 or decision_cutoff.tzinfo is None:
        raise ComparisonError("E1 needs an aware cutoff and positive frozen window")
    days = sorted(set(calendar))
    if execution_day not in days:
        raise ComparisonError("execution day is absent from the trading calendar")
    active = []
    for event in events:
        usable_at = max(event.available_at, event.verified_at)
        if usable_at > decision_cutoff:
            continue
        sessions = [day for day in days
                    if datetime.combine(day, time(15), tzinfo=MARKET_TZ) > usable_at]
        if execution_day in sessions[:window]:
            active.append(event)
    return active


def equal_weight_targets(candidates: list[Candidate], *, equity_pct: Decimal,
                         params: ConstructionParams, label: str) -> tuple[list[TargetWeight], dict]:
    """All pool members receive the same weight, with explicit uniform risk scaling.

    No top-N truncation. Industry concentration may require reducing the entire
    basket's equity exposure. The exact budget is frozen before any outcome.
    """
    if not candidates:
        return [], {"requested_equity_pct": str(equity_pct), "allocated_equity_pct": "0"}
    if len({c.instrument_id for c in candidates}) != len(candidates):
        raise ComparisonError("equal-weight pool contains duplicates")
    industries: dict[str, int] = {}
    for candidate in candidates:
        if not candidate.simulatable:
            raise ComparisonError("equal-weight pool contains a non-simulatable security")
        industries[candidate.industry_code] = industries.get(candidate.industry_code, 0) + 1
    n = Decimal(len(candidates))
    budget = min(equity_pct, params.max_total_equity_pct,
                 params.max_single_name_pct * n,
                 params.max_single_industry_pct * n / max(industries.values()))
    # Rounding down keeps repeating decimals from summing one ulp above a cap.
    weight = (budget / n).quantize(Decimal("0.000000000001"), rounding=ROUND_DOWN)
    targets = [TargetWeight(c.instrument_id, c.industry_code, weight, label)
               for c in sorted(candidates, key=lambda candidate: candidate.instrument_id)]
    return targets, {
        "requested_equity_pct": str(equity_pct), "allocated_equity_pct": str(weight * n),
        "name_count": len(candidates), "equal_weight_pct": str(weight),
        "risk_scaled": budget != equity_pct,
        "constraint_note": "all frozen pool members; uniform industry/name risk scaling; integer lots may leave cash",
    }


def portfolio_metrics(con, *, portfolio_id: str, initial_cash_cents: int,
                      steps: tuple[ReplayStep, ...], industries_by_day: dict[str, dict[str, str]],
                      continuous_eod: bool = True, capacity_proxy: dict | None = None) -> dict:
    """Read authoritative ledger/valuation facts, including initial capital in drawdown."""
    rows = con.execute("SELECT * FROM valuation WHERE portfolio_id=? ORDER BY trading_day",
                       (portfolio_id,)).fetchall()
    if len(rows) != len(steps) or any(not row["published"] for row in rows):
        raise ComparisonError("comparison requires one published valuation per frozen day")
    if [row["trading_day"] for row in rows] != [s.trading_day.isoformat() for s in steps]:
        raise ComparisonError("valuation dates differ from frozen comparison period")
    values = [initial_cash_cents] + [int(row["net_value_cents"]) for row in rows]
    peak = initial_cash_cents
    max_drawdown = Decimal(0)
    series = []
    for row in rows:
        value = int(row["net_value_cents"])
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, Decimal(peak - value) / peak if peak else Decimal(0))
        positions = con.execute("SELECT instrument_id,quantity,value_cents FROM valuation_position WHERE valuation_id=? ORDER BY instrument_id",
                                (row["valuation_id"],)).fetchall()
        industry_values: dict[str, int] = {}
        known_industries = industries_by_day.get(row["trading_day"], {})
        for position in positions:
            industry = known_industries.get(position["instrument_id"], "UNKNOWN")
            industry_values[industry] = industry_values.get(industry, 0) + int(position["value_cents"])
        equity = int(row["positions_value_cents"])
        largest_industry = max(industry_values, key=industry_values.get) if industry_values else None
        series.append({
            "trading_day": row["trading_day"], "valuation_id": row["valuation_id"],
            "net_value_cents": value, "cash_cents": int(row["cash_available_cents"]),
            "cash_pct": _pct(int(row["cash_available_cents"]), value),
            "equity_pct": _pct(int(row["positions_value_cents"]), value),
            "receivables_cents": int(row["receivables_cents"]),
            "industry_exposure_pct": {industry: _pct(amount, value)
                                      for industry, amount in sorted(industry_values.items())},
            "largest_industry_code": largest_industry,
            "largest_industry_pct_net_assets": _pct(max(industry_values.values(), default=0), value),
            "largest_industry_pct_equity": _pct(max(industry_values.values(), default=0), equity),
            "industry_hhi_equity": (str(sum((Decimal(amount) / equity) ** 2
                                            for amount in industry_values.values())) if equity else None),
            "unknown_industry_pct_equity": _pct(industry_values.get("UNKNOWN", 0), equity),
            "positions": [dict(position) for position in positions],
        })
    fees = int(con.execute("SELECT COALESCE(SUM(fc.amount_cents),0) FROM fee_charge fc JOIN fill f ON fc.fill_id=f.fill_id WHERE f.portfolio_id=?",
                           (portfolio_id,)).fetchone()[0])
    fill_count = int(con.execute("SELECT COUNT(*) FROM fill WHERE portfolio_id=?", (portfolio_id,)).fetchone()[0])
    orders = con.execute('SELECT o.order_id,o.quantity,o.status,o.reject_reason,COALESCE(SUM(f.quantity),0) AS filled_quantity '
                         'FROM "order" o LEFT JOIN fill f ON f.order_id=o.order_id '
                         'WHERE o.portfolio_id=? GROUP BY o.order_id ORDER BY o.order_id', (portfolio_id,)).fetchall()
    turnover_cents = int(con.execute("SELECT COALESCE(SUM(quantity * price_cents),0) FROM fill WHERE portfolio_id=?",
                                    (portfolio_id,)).fetchone()[0])
    returns = [values[index] / values[index - 1] - 1 for index in range(1, len(values))]
    gross_by_side = {row["side"]: int(row["gross"]) for row in con.execute(
        "SELECT side,SUM(quantity * price_cents) AS gross FROM fill WHERE portfolio_id=? GROUP BY side", (portfolio_id,))}
    requested_quantity = sum(int(row["quantity"]) for row in orders)
    filled_quantity = sum(int(row["filled_quantity"]) for row in orders)
    unfilled_count = sum(int(row["filled_quantity"]) < int(row["quantity"]) for row in orders)
    reasons: dict[str, int] = {}
    for row in orders:
        if int(row["filled_quantity"]) < int(row["quantity"]):
            reason = row["reject_reason"] or row["status"]
            reasons[reason] = reasons.get(reason, 0) + 1
    average_value = Decimal(sum(values[1:])) / len(rows)
    last = series[-1]
    interval_vol = str(statistics.stdev(returns) * 100) if len(returns) >= 2 else None
    # Twenty marks is a display policy for risk estimates, never a trading or
    # strategy-promotion gate. Irregular observation intervals are not days.
    annualized = (str(statistics.stdev(returns) * math.sqrt(252) * 100)
                  if continuous_eod and len(returns) >= 20 else None)
    volatility_reason = ("EOD 观测缺少交易日；区间收益不能作为日收益年化" if not continuous_eod else
                         "少于 20 个连续 EOD 观测，仅显示区间样本标准差，不发布年化风险估计" if len(returns) < 20 else None)
    accounting = accounting_contributions(con, portfolio_id=portfolio_id,
        initial_cash_cents=initial_cash_cents, final_valuation=series[-1],
        industries=industries_by_day.get(series[-1]["trading_day"], {}))
    return {
        "portfolio_id": portfolio_id, "initial_cash_cents": initial_cash_cents,
        "final_net_value_cents": values[-1], "net_return_pct": _pct(values[-1] - initial_cash_cents, initial_cash_cents),
        "max_drawdown_pct": str(max_drawdown * 100),
        "annualized_volatility_pct": annualized,
        "volatility_basis": volatility_reason or "连续日净收益样本标准差 × sqrt(252)，252 交易日年化",
        "fees_total_cents": fees, "fill_count": fill_count, "order_count": len(orders),
        "unfilled_order_count": unfilled_count,
        "turnover_gross_pct_initial_capital": _pct(turnover_cents, initial_cash_cents),
        "series": series,
        "accounting_contributions": accounting,
        "evaluation": {
            "market_exposure": {"final_equity_pct": last["equity_pct"],
                "mean_observed_equity_pct": str(sum(Decimal(day["equity_pct"]) for day in series) / len(series)),
                "min_observed_equity_pct": str(min(Decimal(day["equity_pct"]) for day in series)),
                "max_observed_equity_pct": str(max(Decimal(day["equity_pct"]) for day in series)),
                "basis": "日终股票市值 / 日终净资产；观测日等权均值，不是 beta 或交易日加权平均"},
            "industry_concentration": {"final_largest_industry_code": last["largest_industry_code"],
                "final_largest_industry_pct_net_assets": last["largest_industry_pct_net_assets"],
                "final_largest_industry_pct_equity": last["largest_industry_pct_equity"],
                "final_hhi_equity": last["industry_hhi_equity"],
                "unknown_industry_pct_equity": last["unknown_industry_pct_equity"],
                "basis": "行业权重按股票市值归一，HHI=sum(行业权重²)，范围 0–1；UNKNOWN 单列，现金不作行业"},
            "turnover": {"gross_traded_cents": turnover_cents,
                "buy_traded_cents": gross_by_side.get("BUY", 0), "sell_traded_cents": gross_by_side.get("SELL", 0),
                "mean_observed_net_assets_cents": str(average_value),
                "gross_pct_mean_observed_assets": _pct(turnover_cents, average_value),
                "basis": "累计买入+卖出成交额 / 观测 EOD 净资产均值；双边口径，含建仓，不年化"},
            "unfilled": {"order_count": len(orders), "unfilled_order_count": unfilled_count,
                "unfilled_order_pct": _pct(unfilled_count, len(orders)),
                "requested_quantity_shares": requested_quantity, "filled_quantity_shares": filled_quantity,
                "unfilled_quantity_pct": _pct(requested_quantity - filled_quantity, requested_quantity),
                "reasons": dict(sorted(reasons.items())),
                "basis": "已提交订单中成交量低于请求量的比例；数量口径按股数汇总；无订单为 unavailable，整手不足未生成订单不计未成交"},
            "capacity_proxy": capacity_proxy or {"status": "UNAVAILABLE", "reason": "缺少冻结 ADV 与订单证据"},
            "volatility": {"observed_interval_sample_std_pct": interval_vol,
                "return_interval_count": len(returns), "daily_intervals": continuous_eod,
                "annualized_pct": annualized, "annualized_unavailable_reason": volatility_reason,
                "basis": "初始现金至首个 EOD，随后相邻观测 EOD 的净收益；样本标准差 ddof=1；不等长区间不能称日波动",
                "sharpe": None, "sharpe_unavailable_reason": "未冻结无风险利率；短样本和缺失 EOD 不支持 Sharpe 推断"},
        },
    }


def accounting_contributions(con, *, portfolio_id: str, initial_cash_cents: int,
                             final_valuation: dict, industries: dict[str, str]) -> dict:
    """Security/industry P&L from authoritative cash flows and terminal marks.

    All comparison accounts start entirely in cash. Security price P&L is
    terminal market value plus sell receipts minus buy consideration. Dividends
    include both received cash and still-recognized receivables, exactly once.
    These are accounting contributions, not counterfactual causal skill claims.
    """
    rows: dict[str, dict] = {}

    def security(iid: str) -> dict:
        if iid not in rows:
            rows[iid] = {"instrument_id": iid, "industry_code": industries.get(iid, "UNKNOWN"),
                "final_market_value_cents": 0, "trade_cash_flow_cents": 0,
                "dividend_cash_flow_cents": 0, "open_receivables_cents": 0,
                "other_cash_flow_cents": 0, "fees_cents": 0}
        return rows[iid]

    for position in final_valuation["positions"]:
        security(position["instrument_id"])["final_market_value_cents"] += int(position["value_cents"])
    fee_rows = con.execute("SELECT f.instrument_id,fc.fill_id,fc.fee_code,fc.amount_cents FROM fee_charge fc JOIN fill f ON f.fill_id=fc.fill_id WHERE f.portfolio_id=?",
                           (portfolio_id,)).fetchall()
    fee_keys = {(row["fill_id"], row["fee_code"]) for row in fee_rows}
    for row in fee_rows:
        security(row["instrument_id"])["fees_cents"] += int(row["amount_cents"])
    unidentified_cash = 0
    cash_rows = con.execute("SELECT entry_type,amount_cents,related_instrument_id,related_fill_id FROM cash_entry WHERE portfolio_id=?",
                            (portfolio_id,)).fetchall()
    deposits = sum(int(row["amount_cents"]) for row in cash_rows if row["entry_type"] == "INITIAL_DEPOSIT")
    if deposits != initial_cash_cents:
        raise ComparisonError("accounting attribution requires the frozen cash-only initial account")
    for row in cash_rows:
        kind = row["entry_type"]
        if kind == "INITIAL_DEPOSIT" or (row["related_fill_id"], kind) in fee_keys:
            continue
        iid = row["related_instrument_id"]
        if not iid:
            unidentified_cash += int(row["amount_cents"])
            continue
        item = security(iid)
        field = ("trade_cash_flow_cents" if kind == "TRADE_SETTLEMENT" else
                 "dividend_cash_flow_cents" if kind in {"DIVIDEND_RECEIVABLE_SETTLED", "DIVIDEND_TAX"}
                 else "other_cash_flow_cents")
        item[field] += int(row["amount_cents"])
    for row in con.execute("SELECT instrument_id,amount_cents FROM receivable WHERE portfolio_id=? AND status='RECOGNIZED'", (portfolio_id,)):
        security(row["instrument_id"])["open_receivables_cents"] += int(row["amount_cents"])
    industry_rows: dict[str, dict] = {}
    for item in rows.values():
        item["price_pnl_cents"] = item["final_market_value_cents"] + item["trade_cash_flow_cents"]
        item["corporate_action_pnl_cents"] = item["dividend_cash_flow_cents"] + item["open_receivables_cents"]
        item["gross_pnl_cents"] = item["price_pnl_cents"] + item["corporate_action_pnl_cents"] + item["other_cash_flow_cents"]
        item["net_pnl_cents"] = item["gross_pnl_cents"] - item["fees_cents"]
        item["net_contribution_pct_initial_capital"] = _pct(item["net_pnl_cents"], initial_cash_cents)
        industry = item["industry_code"]
        aggregate = industry_rows.setdefault(industry, {"industry_code": industry,
            "price_pnl_cents": 0, "corporate_action_pnl_cents": 0,
            "other_cash_flow_cents": 0, "fees_cents": 0, "net_pnl_cents": 0})
        for key in ("price_pnl_cents", "corporate_action_pnl_cents", "other_cash_flow_cents", "fees_cents", "net_pnl_cents"):
            aggregate[key] += item[key]
    total = int(final_valuation["net_value_cents"]) - initial_cash_cents
    allocated = sum(item["net_pnl_cents"] for item in rows.values())
    residual = total - allocated
    if residual != unidentified_cash:
        raise ComparisonError("security accounting contributions do not reconcile with net asset change")
    return {"basis": "会计损益贡献：期末市值 + 交易现金流 + 分红现金/未结应收 − 已入账费用；不等于因果选股能力",
            "securities": [rows[iid] for iid in sorted(rows)],
            "industries": [industry_rows[iid] for iid in sorted(industry_rows)],
            "total_net_pnl_cents": total, "allocated_net_pnl_cents": allocated,
            "unallocated_residual_cents": residual,
            "unallocated_reason": "UNKNOWN: cash flows lack a security binding" if residual else None,
            "reconciled": allocated + residual == total}


def human_difference(model: dict, human: dict, *, origin: str, decision_refs: tuple[str, ...]) -> dict:
    """Accounting differences are facts; causal selection/cash effects stay unknown."""
    initial = model["initial_cash_cents"]
    if human["initial_cash_cents"] != initial:
        raise ComparisonError("H and M initial capital differ")
    final_m, final_h = model["series"][-1], human["series"][-1]
    model_names = {row["instrument_id"] for row in final_m["positions"]}
    human_names = {row["instrument_id"] for row in final_h["positions"]}
    wealth_difference = human["final_net_value_cents"] - model["final_net_value_cents"]
    fee_difference = human["fees_total_cents"] - model["fees_total_cents"]
    industries = set(final_h["industry_exposure_pct"]) | set(final_m["industry_exposure_pct"])
    accounting_m = model["accounting_contributions"]
    accounting_h = human["accounting_contributions"]
    security_m = {row["instrument_id"]: row for row in accounting_m["securities"]}
    security_h = {row["instrument_id"]: row for row in accounting_h["securities"]}
    contribution_differences = []
    industry_differences: dict[str, dict] = {}
    for iid in sorted(set(security_m) | set(security_h)):
        row_m, row_h = security_m.get(iid, {}), security_h.get(iid, {})
        industry = row_h.get("industry_code") or row_m.get("industry_code") or "UNKNOWN"
        item = {"instrument_id": iid, "industry_code": industry}
        for field in ("price_pnl_cents", "corporate_action_pnl_cents", "fees_cents", "net_pnl_cents"):
            item[f"{field}_difference"] = row_h.get(field, 0) - row_m.get(field, 0)
        contribution_differences.append(item)
        aggregate = industry_differences.setdefault(industry, {"industry_code": industry, "net_pnl_cents_difference": 0})
        aggregate["net_pnl_cents_difference"] += item["net_pnl_cents_difference"]
    unallocated_difference = accounting_h["unallocated_residual_cents"] - accounting_m["unallocated_residual_cents"]
    if sum(row["net_pnl_cents_difference"] for row in contribution_differences) + unallocated_difference != wealth_difference:
        raise ComparisonError("H-M security contribution differences do not reconcile")
    return {
        "human_origin": origin, "human_decision_refs": list(decision_refs),
        "interpretation": ("工程回放操作，不能代表用户判断或人工能力" if origin == "ENGINEERING_REPLAY"
                           else "记录明确人工操作；样本与暴露不同，尚不足以归因人工能力"),
        "net_wealth_difference_cents": wealth_difference,
        "net_return_difference_pct_points": _pct(wealth_difference, initial),
        "direct_fee_debit_difference_cents": -fee_difference,
        "direct_fee_debit_difference_pct_points": _pct(-fee_difference, initial),
        "remaining_wealth_difference_cents": wealth_difference + fee_difference,
        "security_accounting_contribution_differences": contribution_differences,
        "industry_accounting_contribution_differences": [industry_differences[key] for key in sorted(industry_differences)],
        "unallocated_contribution_difference_cents": unallocated_difference,
        "accounting_contribution_differences_reconciled": True,
        "accounting_identity": "net wealth difference = direct fee debit difference + remaining wealth difference; residual is not causal selection alpha",
        "added_final_holdings": sorted(human_names - model_names),
        "removed_final_holdings": sorted(model_names - human_names),
        "cash_pct_difference_points": str(Decimal(final_h["cash_pct"]) - Decimal(final_m["cash_pct"])),
        "industry_exposure_difference_pct_points": {
            industry: str(Decimal(final_h["industry_exposure_pct"].get(industry) or "0")
                          - Decimal(final_m["industry_exposure_pct"].get(industry) or "0"))
            for industry in sorted(industries)
        },
        "causal_selection_effect": None, "causal_cash_effect": None,
        "timing_effect": None, "holding_period_effect": None,
        "external_information_effect": None,
        "unknown_reason": "需要可核验操作、同条件反事实与更长前向记录；收益差不能直接归因选股或人工能力",
    }


def capacity_metrics(samples: list[dict], *, lookback_days: int, participation_cap: Decimal) -> dict:
    """Order/known-volume evidence is a proxy, never a money capacity estimate."""
    usable = [row for row in samples if row["prior_adv_shares"] and row["prior_adv_shares"] > 0]
    complete = [row for row in usable if row["prior_quote_count"] == lookback_days]
    max_requested = max((Decimal(row["requested_participation_pct"]) for row in usable), default=None)
    max_filled = max((Decimal(row["filled_participation_pct"]) for row in usable), default=None)
    return {
        "status": "AVAILABLE" if samples and len(complete) == len(samples) else "PARTIAL" if usable else "UNAVAILABLE",
        "lookback_trading_days": lookback_days, "simulator_participation_cap_pct": str(participation_cap * 100),
        "order_count": len(samples), "usable_adv_order_count": len(usable), "full_lookback_order_count": len(complete),
        "max_requested_participation_pct": str(max_requested) if max_requested is not None else None,
        "max_filled_participation_pct": str(max_filled) if max_filled is not None else None,
        "volume_cap_rejection_count": sum(row["reject_reason"] == "VOLUME_CAP_EXCEEDED" for row in samples),
        "samples": samples,
        "basis": "每单请求/成交股数 ÷ 冻结执行快照中执行日前 N 个已知行情日平均成交股数；与模拟器 ADV/0.1% 全成全拒限制一致；不用当日成交量",
        "capital_capacity_cents": None,
        "capital_capacity_unavailable_reason": "缺少订单簿、冲击模型、参与率压力实验与连续成交证据；参与率代理不能换算可投资资金容量",
        "reason": None if samples and len(complete) == len(samples) else "无订单或部分订单缺少完整的此前 ADV 证据",
    }


class ComparisonRunner:
    def __init__(self, service: PlanService, store: ComparisonStore):
        self.service, self.store = service, store

    def _spec_record(self, spec: ComparisonRunSpec) -> dict:
        svc = self.service
        return {
            "comparison_version": COMPARISON_VERSION, "spec": spec.as_dict(),
            "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "construction_params": json.loads(_canonical(asdict(svc.params))),
            "fee_table": {"commission_source": svc.fee_table.commission_source,
                          "schedules_by_day": {step.trading_day.isoformat(): json.loads(_canonical(
                              asdict(svc.fee_table.schedule_for(step.trading_day)))) for step in spec.steps}},
            "board_rules": json.loads(_canonical([asdict(rule) for rule in svc.board_rules])),
            "snapshots": [{"decision": svc.reader.ref(step.decision_snapshot_id).as_dict(),
                           "execution": svc.reader.ref(step.execution_snapshot_id).as_dict()}
                          for step in spec.steps],
            "benchmark_definition": "same frozen simulatable pool, equal weight across every member, same equity budget and fees; risk scaling explicit; integer lots may leave cash",
            "broad_market_reference": {"status": "UNKNOWN", "reason": "未提供冻结广义市场指数及价格/全收益口径"},
            "human_origin": spec.human_origin,
        }

    def run(self, spec: ComparisonRunSpec, *, authorization_subject: str,
            now: datetime | None = None) -> dict:
        if not confirmer_is_human(authorization_subject):
            raise ComparisonError("comparison execution requires explicit human authorization")
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ComparisonError("comparison execution time must be aware")
        record = self._spec_record(spec)
        run_id = "cmp-" + hashlib.sha256(_canonical(record)).hexdigest()[:24]
        self.store.write_once(f"{run_id}.spec.json", record)
        result_path = self.store.root / f"{run_id}.result.json"
        if result_path.exists():
            return self.store.read(run_id)
        result = {"run_id": run_id, "comparison_version": COMPARISON_VERSION,
                  "experiment_version": spec.experiment_version, "run_kind": spec.run_kind,
                  "human_origin": spec.human_origin, "preregistration": record,
                  "created_at": now.isoformat(), "status": "RUNNING",
                  "limitations": list(spec.limitations), "portfolio_runs": {},
                  "event_shadow": {"verified_event_count": len(spec.verified_events), "active_events_by_day": {}, "blocked_buy_count": 0}}
        if any(step.trading_day > now.astimezone(MARKET_TZ).date()
               or self.service.reader.ref(step.execution_snapshot_id).as_of_time > now for step in spec.steps):
            result.update(status="PENDING", pending_reason="未来执行日或执行快照尚未到达；没有模拟成交或收益")
            self.store.write_once(f"{run_id}.result.json", result)
            return result
        try:
            if spec.run_kind == "FORWARD_SIMULATION":
                raise ComparisonError("this runner registers after execution snapshots exist; use the durable pre-open forward draft workflow instead of relabelling a replay")
            if spec.human_origin == "USER_CONFIRMED":
                raise ComparisonError("engineering replay cannot reconstruct actual H decisions from labels; compare the user's confirmed plans and ledger instead")
            self._execute(spec, result, authorization_subject, now)
            result["status"] = "COMPLETED"
        except Exception as exc:
            result.update(status="FAILED", error={"type": type(exc).__name__, "message": str(exc),
                                                    "code": getattr(exc, "code", None),
                                                    "object_id": getattr(exc, "object_id", None)})
        self.store.write_once(f"{run_id}.result.json", result)
        return result

    def _execute(self, spec: ComparisonRunSpec, result: dict, subject: str, now: datetime):
        base = self.service
        portfolio_ids = {kind: f"pf-{result['run_id']}-{kind.lower()}" for kind in "MEHB"}
        if any(base.con.execute("SELECT 1 FROM portfolio WHERE portfolio_id=?", (pid,)).fetchone()
               for pid in portfolio_ids.values()):
            raise ComparisonError("partial comparison ledger exists; preserve failed run and register a new experiment version")
        result["portfolio_runs"] = {kind: {"portfolio_id": pid, "days": []} for kind, pid in portfolio_ids.items()}
        industries_by_day: dict[str, dict[str, str]] = {}
        capacity_samples: dict[str, list[dict]] = {kind: [] for kind in "MEHB"}
        all_calendar_days: set[date] = set()
        for step in spec.steps:
            ref = base.reader.ref(step.decision_snapshot_id)
            execution_ref = base.reader.ref(step.execution_snapshot_id)
            instruments = {row["instrument_id"]: row for row in base.reader.instruments(step.decision_snapshot_id, as_of=ref.as_of_time)}
            if not set(spec.universe) <= set(instruments):
                raise ComparisonError("frozen pool is missing from a decision snapshot")
            listings = {iid: (row["exchange"], row["board"]) for iid, row in instruments.items()}
            if any(listings[iid][1] != "MAIN" for iid in spec.universe):
                raise ComparisonError("first-phase comparison pool must be MAIN-board simulatable")
            histories = {iid: [] for iid in spec.universe}
            for quote in base.reader.daily_quotes(step.decision_snapshot_id, as_of=ref.as_of_time):
                if quote.instrument_id in histories:
                    if quote.adjusted_close_cents is None:
                        raise ComparisonError("S1 history has missing adjusted close")
                    histories[quote.instrument_id].append(quote.adjusted_close_cents)
            industries = {iid: instruments[iid].get("industry_code") for iid in spec.universe}
            if any(not industry for industry in industries.values()):
                raise ComparisonError("frozen pool lacks industry classification")
            signals = build_s1_signals(adjusted_closes_by_instrument=histories, industry_by_instrument=industries)
            candidates = [signal.as_candidate() for signal in signals]
            # Publish all effective universe filters before any portfolio's order.
            probe = PlanService(base.con, base.reader, base.fee_table, base.board_rules, listings, base.params)
            liquid, liquidity_exclusions = probe._filter_by_liquidity(snapshot_id=step.decision_snapshot_id,
                trading_day=step.trading_day, as_of=ref.as_of_time, candidates=candidates)
            eligible, age_exclusions, _ = probe._filter_by_listing_age(snapshot_id=step.decision_snapshot_id,
                trading_day=step.trading_day, as_of=ref.as_of_time, candidates=liquid)
            if liquidity_exclusions or age_exclusions or {c.instrument_id for c in eligible} != set(spec.universe):
                raise ComparisonError("preregistered pool contains filtered securities; freeze the common eligible pool before replay")
            calendar = [date.fromisoformat(day) for day in base.reader.trading_calendar(step.execution_snapshot_id,
                                                                                        as_of=execution_ref.as_of_time)]
            all_calendar_days.update(calendar)
            active = active_e1_events(events=spec.verified_events, calendar=calendar,
                                     decision_cutoff=ref.as_of_time, execution_day=step.trading_day,
                                     window=spec.e1_window_trading_days)
            result["event_shadow"]["active_events_by_day"][step.trading_day.isoformat()] = [event.event_id for event in active]
            execution_instruments = base.reader.instruments(step.execution_snapshot_id, as_of=execution_ref.as_of_time)
            industries_by_day[step.trading_day.isoformat()] = {row["instrument_id"]: row.get("industry_code") or "UNKNOWN"
                                                              for row in execution_instruments}
            # The execution engine admits only quotes strictly before this day.
            # Read once for all four accounts, retaining the same N-row window.
            prior_volumes: dict[str, list[int]] = {iid: [] for iid in spec.universe}
            for quote in base.reader.daily_quotes(step.execution_snapshot_id, as_of=execution_ref.as_of_time):
                if quote.instrument_id in prior_volumes and quote.trading_day < step.trading_day:
                    prior_volumes[quote.instrument_id].append(quote.volume_shares)
            prior_volumes = {iid: values[-base.params.liquidity_lookback_days:] for iid, values in prior_volumes.items()}
            raw_actions = base.reader.corporate_actions(step.execution_snapshot_id, as_of=execution_ref.as_of_time)
            actions = self._cash_dividends(raw_actions)
            preceding = [other.trading_day for other in spec.steps if other.trading_day < step.trading_day]
            if preceding:
                prior_day = max(preceding)
                if any(prior_day < boundary < step.trading_day
                       for action in actions if action.instrument_id in spec.universe
                       for boundary in (action.record_date, action.ex_date, action.pay_date)):
                    raise ComparisonError("sampled replay skips a known corporate-action boundary; add that day's execution and EOD snapshot")
            baseline_targets, baseline_budget = equal_weight_targets(candidates, equity_pct=base.params.max_total_equity_pct,
                                                                     params=base.params, label="B frozen common-pool equal weight")
            human_candidates = [c for c in candidates if c.instrument_id in spec.human_selection]
            human_targets, human_budget = equal_weight_targets(human_candidates, equity_pct=spec.human_equity_pct,
                                                               params=base.params, label=f"H {spec.human_origin}")
            for kind in "MEHB":
                params = (replace(base.params, max_holdings=len(spec.universe)) if kind == "B" else base.params)
                svc = PlanService(base.con, base.reader, base.fee_table, base.board_rules, listings, params)
                portfolio_id = portfolio_ids[kind]
                initialized = base.con.execute("SELECT 1 FROM portfolio WHERE portfolio_id=?", (portfolio_id,)).fetchone()
                lots = svc._load_lots(portfolio_id) if initialized else []
                cash = svc._ledger_cash(portfolio_id) if initialized else spec.initial_cash_cents
                kwargs = {}
                if kind == "B":
                    kwargs["target_weights"] = baseline_targets
                elif kind == "H":
                    kwargs["target_weights"] = human_targets
                elif kind == "E":
                    kwargs["buy_blocked_instruments"] = {event.instrument_id for event in active}
                preview = svc.preview(portfolio_id=portfolio_id, snapshot_id=step.decision_snapshot_id,
                    decision_snapshot_id=step.decision_snapshot_id, decision_cutoff_at=ref.as_of_time,
                    execution_snapshot_id=step.execution_snapshot_id, trading_day=step.trading_day,
                    as_of=ref.as_of_time, candidates=candidates, lots=lots,
                    cash_available_cents=cash, confirm_subject=subject, **kwargs)
                token = svc.issue_confirmation(preview=preview, subject=subject, current_lots=lots,
                                               current_cash_cents=cash, now=now)
                svc.freeze(preview=preview, confirm_subject=subject, confirmation_token=token,
                           expected_account_version=preview.account_version,
                           current_lots=lots, current_cash_cents=cash, now=now)
                execution = svc.execute(plan_id=preview.plan_id, subject=subject, now=now,
                                        corporate_actions=actions)
                valuation = svc.value(portfolio_id=portfolio_id, snapshot_id=step.execution_snapshot_id,
                    trading_day=step.trading_day, as_of=execution_ref.as_of_time,
                    execution_plan_id=preview.plan_id, lots=svc._load_lots(portfolio_id),
                    cash_available_cents=svc._ledger_cash(portfolio_id))
                reconciliation = svc.reconcile(portfolio_id=portfolio_id)
                if not valuation["published"] or not reconciliation["reconciled"]:
                    raise ComparisonError("ledger reconciliation or valuation invariant failed")
                for order in base.con.execute(
                        'SELECT o.order_id,o.instrument_id,o.side,o.quantity,o.status,o.reject_reason,COALESCE(SUM(f.quantity),0) AS filled_quantity '
                        'FROM "order" o LEFT JOIN fill f ON f.order_id=o.order_id WHERE o.plan_id=? GROUP BY o.order_id ORDER BY o.order_id',
                        (preview.plan_id,)):
                    volumes = prior_volumes.get(order["instrument_id"], [])
                    adv = sum(volumes) // len(volumes) if volumes else None
                    capacity_samples[kind].append({
                        "trading_day": step.trading_day.isoformat(), "execution_snapshot_id": step.execution_snapshot_id,
                        "order_id": order["order_id"], "instrument_id": order["instrument_id"], "side": order["side"],
                        "requested_quantity_shares": int(order["quantity"]), "filled_quantity_shares": int(order["filled_quantity"]),
                        "status": order["status"], "reject_reason": order["reject_reason"],
                        "prior_adv_shares": adv, "prior_quote_count": len(volumes),
                        "requested_participation_pct": _pct(int(order["quantity"]), adv or 0),
                        "filled_participation_pct": _pct(int(order["filled_quantity"]), adv or 0),
                    })
                run = result["portfolio_runs"][kind]
                run["days"].append({"trading_day": step.trading_day.isoformat(), "plan_id": preview.plan_id,
                    "decision_snapshot": ref.as_dict(), "execution_snapshot": execution_ref.as_dict(),
                    "target_count": len(preview.targets), "order_count": len(preview.orders),
                    "fill_count": len(execution["fills"]), "reconciled": reconciliation["reconciled"],
                    "excluded": preview.excluded,
                    "allocation": baseline_budget if kind == "B" else human_budget if kind == "H" else None})
                if kind == "E":
                    result["event_shadow"]["blocked_buy_count"] += sum(
                        exclusion["reason"] == "E1_BUY_PAUSED" for exclusion in preview.excluded)
                before_quantity = {}
                for lot in lots:
                    before_quantity[lot.instrument_id] = before_quantity.get(lot.instrument_id, 0) + lot.quantity_remaining
                reference_bars = svc._reference_bars(step.decision_snapshot_id, step.trading_day,
                    ref.as_of_time, sorted({c.instrument_id for c in candidates} | set(before_quantity)))
                reference_equity = cash + sum(quantity * reference_bars[iid].close_cents
                    for iid, quantity in before_quantity.items() if iid in reference_bars)
                below_lot = [target.instrument_id for target in preview.targets
                    if not before_quantity.get(target.instrument_id)
                    and target.instrument_id in reference_bars
                    and Decimal(reference_equity) * target.weight_pct / 100
                        < reference_bars[target.instrument_id].close_cents * svc._lot_size(target.instrument_id, step.trading_day)]
                run["days"][-1]["below_minimum_lot_target_count"] = len(below_lot)
                run["days"][-1]["below_minimum_lot_instruments"] = sorted(below_lot)
        observed_days = {step.trading_day for step in spec.steps}
        unobserved = sorted(day.isoformat() for day in all_calendar_days
                            if spec.steps[0].trading_day <= day <= spec.steps[-1].trading_day
                            and day not in observed_days)
        cap = DailySimulator(fee_table=base.fee_table, board_rules=base.board_rules,
                             listings=base.listings).participation_cap
        result["portfolios"] = {kind: portfolio_metrics(base.con, portfolio_id=pid,
            initial_cash_cents=spec.initial_cash_cents, steps=spec.steps, industries_by_day=industries_by_day,
            continuous_eod=not unobserved, capacity_proxy=capacity_metrics(capacity_samples[kind],
                lookback_days=base.params.liquidity_lookback_days, participation_cap=cap))
            for kind, pid in portfolio_ids.items()}
        benchmark = result["portfolios"]["B"]
        for metrics in result["portfolios"].values():
            difference = metrics["final_net_value_cents"] - benchmark["final_net_value_cents"]
            metrics["benchmark_difference"] = {
                "benchmark_portfolio_id": benchmark["portfolio_id"], "net_wealth_difference_cents": difference,
                "net_return_difference_pct_points": _pct(difference, spec.initial_cash_cents),
                "basis": "同初始资金、日期、费用与冻结共同池 B 的期末净资产差和净收益百分点差；实际现金/股票暴露不同，不能称 beta 调整 alpha",
                "broad_market_difference_pct_points": None,
                "broad_market_unavailable_reason": "缺少冻结广义市场指数和价格/全收益口径",
            }
        result["observation_basis"] = {
            "sample_count": len(spec.steps), "missing_eod_days": unobserved,
            "continuous_eod": not unobserved,
            "drawdown_basis": "observed EOD marks including initial cash; unobserved days may have larger drawdowns",
        }
        if unobserved:
            for metrics in result["portfolios"].values():
                metrics["annualized_volatility_pct"] = None
                metrics["volatility_basis"] = "UNKNOWN: non-contiguous EOD samples; daily annualization is not justified"
            result["limitations"].append("非连续 EOD 采样：回撤仅为已观测时点下界，日频年化波动未计算")
        result["human_difference"] = human_difference(result["portfolios"]["M"], result["portfolios"]["H"],
                                                     origin=spec.human_origin, decision_refs=spec.human_decision_refs)
        result["event_shadow"]["rule_triggered"] = result["event_shadow"]["blocked_buy_count"] > 0
        result["event_shadow"]["interpretation"] = ("没有发生 E1 买入暂停；本轮不能检验事件规则增量"
            if not result["event_shadow"]["rule_triggered"] else "已执行核验事件买入暂停，保持 S1 自身卖出条件")
        if not spec.verified_events:
            result["limitations"].append("未提供核验重大监管调查事件；E 与 M 一致只能验证执行一致性")
        result["limitations"].extend(["短样本工程/前向记录不足以证明策略或人工能力", "广义市场指数参考缺少冻结数据，标记 UNKNOWN"])

    @staticmethod
    def _cash_dividends(rows: list[dict]) -> list[CashDividend]:
        actions = []
        for row in rows:
            kind = row.get("action_type") or row.get("type")
            if kind not in {"CASH_DIVIDEND", "cash_dividend"}:
                if row.get("supported") is False or kind:
                    raise ComparisonError("unsupported corporate action in comparison execution snapshot")
                continue
            micros = row.get("cash_per_share_micros")
            if micros is None:
                micros = int(row["cash_per_share_cents"]) * 10_000
            actions.append(CashDividend(action_id=row["action_id"], instrument_id=row["instrument_id"],
                record_date=date.fromisoformat(row["record_date"]), ex_date=date.fromisoformat(row["ex_date"]),
                pay_date=date.fromisoformat(row["pay_date"]), cash_per_share_micros=int(micros),
                tax_treatment=row.get("tax_treatment", "PRE_TAX")))
        return actions
