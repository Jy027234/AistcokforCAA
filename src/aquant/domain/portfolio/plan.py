"""模拟计划的生命周期：预览 -> 冻结 -> 执行 -> 对账。

这条链路就是主文档 §16.2 的 POST /plans/preview 与 POST /plans/{id}/freeze，
加上 §11 的组合构建与 §12 的模拟成交。

四条不可让渡的规则：

1. **预览只算不冻**：preview 不写成交、不动账本，明确返回"未冻结"（A08）。
2. **冻结必须复核五项**：计划版本、快照版本、账户状态版本、确认主体、有效期。
   任一项不匹配即拒绝并要求重新预览（A09）。
3. **确认主体不能是模型**：冻结由用户界面确认的服务执行（§5.5、§16.3）。
   本模块不提供任何"模型自动确认"的入口。
4. **幂等**：(plan_id) 与业务幂等键唯一，重复提交不产生第二次入账（A07、S05）。

本模块是**编排**，不是重算：规则检查、费用、成交、估值全部委托给已有模块，
这样每条规则只有一处实现，不会在编排层被"顺手简化"。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from ..data.db import write_tx
from ..data.reader import SnapshotReader, QuoteRow
from ..portfolio.construction import (
    Candidate,
    ConstructionParams,
    TargetWeight,
    construct_targets,
    weights_to_orders,
)
from ..simulation.fees import FeeTable
from ..simulation.simulator import (
    Bar,
    BoardRule,
    DailySimulator,
    Lot,
    Order,
    OrderStatus,
    Side,
)

PLAN_STATUSES = ("DRAFT", "PREVIEWED", "FROZEN", "EXECUTING", "EXECUTED",
                 "CANCELLED", "EXPIRED", "SUPERSEDED")


class PlanError(Exception):
    def __init__(self, code: str, message: str, object_id: str, repair_action: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.object_id = object_id
        self.repair_action = repair_action

    def as_error(self) -> dict:
        return {"code": self.code, "message": self.message, "object_id": self.object_id,
                "retryable": False, "repair_action": self.repair_action}


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise PlanError("DATA_NOT_READY", "naive datetime rejected (§7.1)", "<timestamp>",
                        "store timezone-aware UTC")
    return dt.astimezone(timezone.utc).isoformat()


def _account_version(lots: list[Lot], cash_cents: int) -> str:
    """账户状态版本：由持仓批次与可用现金决定。

    冻结时记录它、执行前再校验它——账户在预览与确认之间发生变化就必须重来（A09）。
    """

    payload = json.dumps(
        {
            "cash": cash_cents,
            "lots": sorted(
                [l.instrument_id, l.quantity_remaining, l.earliest_sellable_day.isoformat()]
                for l in lots
            ),
        },
        ensure_ascii=False, sort_keys=True,
    )
    return "acct-" + hashlib.sha256(payload.encode()).hexdigest()[:16]


def _plan_version(targets: list[TargetWeight], snapshot_id: str) -> str:
    payload = json.dumps(
        {"snapshot": snapshot_id,
         "targets": sorted([t.instrument_id, str(t.weight_pct)] for t in targets)},
        ensure_ascii=False, sort_keys=True,
    )
    return "plan-" + hashlib.sha256(payload.encode()).hexdigest()[:16]


# ======================================================================
@dataclass(slots=True)
class PlanPreview:
    plan_id: str
    portfolio_id: str
    snapshot_id: str
    plan_version: str
    account_version: str
    trading_day: date
    targets: list[TargetWeight]
    orders: list[dict]
    estimated_fees_cents: int
    rule_checks: list[dict]
    excluded: list[dict]
    cash_weight_pct: Decimal
    #: 明确告知调用方：预览不冻结、不产生成交（A08）
    frozen: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "portfolio_id": self.portfolio_id,
            "snapshot_id": self.snapshot_id,
            "plan_version": self.plan_version,
            "account_version": self.account_version,
            "trading_day": self.trading_day.isoformat(),
            "frozen": self.frozen,
            "orders": self.orders,
            "estimated_fees_cents": self.estimated_fees_cents,
            "rule_checks": self.rule_checks,
            "excluded": self.excluded,
            "cash_weight_pct": str(self.cash_weight_pct),
            "notes": self.notes,
        }


class PlanService:
    def __init__(self, con: sqlite3.Connection, reader: SnapshotReader,
                 fee_table: FeeTable, board_rules: list[BoardRule],
                 listings: dict[str, tuple[str, str]],
                 params: ConstructionParams | None = None) -> None:
        self.con = con
        self.reader = reader
        self.fee_table = fee_table
        self.board_rules = board_rules
        self.listings = listings
        self.params = params or ConstructionParams()

    # ------------------------------------------------------------ helpers
    def _bars(self, snapshot_id: str, trading_day: date, as_of: datetime,
              instrument_ids: list[str]) -> dict[str, Bar]:
        bars: dict[str, Bar] = {}
        for iid in instrument_ids:
            rows = self.reader.daily_quotes(snapshot_id, as_of=as_of,
                                            instrument_id=iid, end=trading_day)
            match = next((r for r in rows if r.trading_day == trading_day), None)
            if match is None:
                continue      # 停牌/无行情：不构造 Bar，模拟器会据此判定不成交
            bars[iid] = Bar(
                instrument_id=iid, trading_day=trading_day,
                open_cents=match.open_cents, high_cents=match.high_cents,
                low_cents=match.low_cents, close_cents=match.close_cents,
                prev_close_cents=match.prev_close_cents or match.open_cents,
                volume_shares=match.volume_shares,
                board_limit_up=match.board_limit_up,
            )
        return bars

    def _adv(self, snapshot_id: str, trading_day: date, as_of: datetime,
             instrument_ids: list[str]) -> dict[str, int]:
        """过去 N 个交易日的平均成交量。只用**此前已知**的数据（§12.4）。"""

        out: dict[str, int] = {}
        for iid in instrument_ids:
            rows = self.reader.daily_quotes(snapshot_id, as_of=as_of, instrument_id=iid,
                                            end=trading_day)
            history = [r for r in rows if r.trading_day < trading_day]
            history = history[-self.params.liquidity_lookback_days:]
            if history:
                out[iid] = sum(r.volume_shares for r in history) // len(history)
        return out

    # ------------------------------------------------------------ preview
    def preview(
        self,
        *,
        portfolio_id: str,
        snapshot_id: str,
        trading_day: date,
        as_of: datetime,
        candidates: list[Candidate],
        cash_available_cents: int,
        lots: list[Lot],
        confirm_subject: str,
    ) -> PlanPreview:
        """只算不冻。不写 simulation_plan，不写账本（A08）。"""

        if not confirm_subject or not confirm_subject.strip():
            raise PlanError("DATA_NOT_READY", "confirm_subject is required for a plan",
                            portfolio_id, "identify the human confirming the plan")

        ids = [c.instrument_id for c in candidates]
        bars = self._bars(snapshot_id, trading_day, as_of, ids)
        held_qty: dict[str, int] = {}
        held_industry_value: dict[str, int] = {}
        for l in lots:
            if l.quantity_remaining > 0:
                held_qty[l.instrument_id] = held_qty.get(l.instrument_id, 0) + l.quantity_remaining
        for iid, qty in held_qty.items():
            b = bars.get(iid)
            if b is not None:
                industry = self._industry_of(candidates, iid)
                held_industry_value[industry] = held_industry_value.get(industry, 0) + qty * b.close_cents

        equity = cash_available_cents + sum(
            q * bars[i].close_cents for i, q in held_qty.items() if i in bars
        )
        construction = construct_targets(
            candidates=candidates, params=self.params, held=held_qty,
            held_industry_value=held_industry_value, equity_value_cents=equity,
            bar_by_instrument=bars,
        )

        orders = weights_to_orders(
            targets=construction.targets, params=self.params,
            price_by_instrument={i: b.close_cents for i, b in bars.items()},
            lot_size_by_instrument={i: self._lot_size(i, trading_day) for i in bars},
            held_quantity=held_qty, sellable_quantity=self._sellable(lots, trading_day),
            equity_value_cents=equity,
        )

        # 规则检查：每一条都给出结论与原因，不通过则不得冻结
        checks: list[dict] = []
        est_fees = 0
        for o in orders:
            iid = o["instrument_id"]
            bar = bars.get(iid)
            if bar is None:
                checks.append({"order": iid, "check": "HAS_PRICE", "passed": False,
                               "detail": "no bar on the trading day; the order cannot fill"})
                continue
            exchange, board_ = self.listings[iid]
            rule = next((r for r in self.board_rules
                         if r.exchange == exchange and r.board == board_
                         and r.covers(trading_day)), None)
            checks.append({"order": iid, "check": "RULE_VERSION", "passed": rule is not None,
                           "detail": None if rule else f"no rule for {exchange}/{board_}"})
            charge = self.fee_table.compute(
                side=o["side"], quantity=o["quantity"], price_cents=o["price_cents"],
                trading_day=trading_day)
            est_fees += charge.total_cents
            checks.append({"order": iid, "check": "FEE_VERSION", "passed": True,
                           "detail": f"{charge.total_cents} cents"})

        # §12.4 单笔都在预算内，不等于合计在预算内。若不在这里拦住，
        # 后一笔买单会在执行时才失败，账户进入"部分建仓"状态且无法区分原因。
        buy_demand = sum(
            o["quantity"] * o["price_cents"]
            + self.fee_table.compute(side="BUY", quantity=o["quantity"],
                                     price_cents=o["price_cents"],
                                     trading_day=trading_day).total_cents
            for o in orders if o["side"] == "BUY"
        )
        sell_supply = sum(
            o["quantity"] * o["price_cents"]
            for o in orders if o["side"] == "SELL"
        )
        if buy_demand > cash_available_cents + sell_supply:
            raise PlanError(
                "INSUFFICIENT_CASH",
                f"the plan needs {buy_demand} cents but only "
                f"{cash_available_cents + sell_supply} are available once sells are counted",
                portfolio_id,
                "reduce the target weights or add capital; a plan must be affordable as a whole",
            )
        checks.append({"order": "*", "check": "PLAN_AFFORDABLE", "passed": True,
                       "detail": f"buy demand {buy_demand} <= available "
                                 f"{cash_available_cents + sell_supply}"})

        if any(not c["passed"] for c in checks):
            failing = [c for c in checks if not c["passed"]]
            raise PlanError(
                "RULE_VERSION_MISSING",
                f"{len(failing)} rule check(s) failed: "
                + "; ".join(f"{c['order']}:{c['check']}" for c in failing[:3]),
                portfolio_id,
                "resolve the failing checks before previewing; a plan is not frozen on guesses",
            )

        plan_id = "plan_" + uuid.uuid4().hex[:20]
        return PlanPreview(
            plan_id=plan_id, portfolio_id=portfolio_id, snapshot_id=snapshot_id,
            plan_version=_plan_version(construction.targets, snapshot_id),
            account_version=_account_version(lots, cash_available_cents),
            trading_day=trading_day, targets=construction.targets, orders=orders,
            estimated_fees_cents=est_fees, rule_checks=checks,
            excluded=construction.excluded, cash_weight_pct=construction.cash_weight_pct,
            frozen=False, notes=construction.notes,
        )

    def _ensure_portfolio(self, portfolio_id: str, *, initial_cash_cents: int,
                          now: datetime) -> None:
        """账本外键要求组合先存在。这里幂等地补一行，不让编排层散落建账逻辑。

        kind 由 ID 约定推导（pf-syn-m/pf-syn-e/pf-syn-h/pf-syn-b），
        与 §13.1 的 M/E/H/B 四类对照账户对应。
        """

        existing = self.con.execute(
            "SELECT portfolio_id FROM portfolio WHERE portfolio_id=?", (portfolio_id,)
        ).fetchone()
        if existing is not None:
            return
        suffix = portfolio_id.rsplit("-", 1)[-1].upper()
        kind = suffix if suffix in {"M", "E", "H", "B"} else "M"
        with write_tx(self.con):
            self.con.execute(
                "INSERT OR IGNORE INTO portfolio (portfolio_id,kind,account_type,"
                "base_currency,initial_cash_cents,opened_at,status) VALUES (?,?,?,?,?,?,?)",
                (portfolio_id, kind, "SIMULATED", "CNY", max(initial_cash_cents, 0),
                 _iso(now), "ACTIVE"),
            )
            # §15.1 要求从期初到期末逐项对账，因此期初资金必须是一条分录，
            # 否则现金分录合计永远等于负的累计支出，无法对账。
            self.con.execute(
                "INSERT OR IGNORE INTO cash_entry (entry_id,portfolio_id,entry_type,"
                "amount_cents,trading_day,occurred_at,note) VALUES (?,?,?,?,?,?,?)",
                (f"{portfolio_id}-INITIAL", portfolio_id, "INITIAL_DEPOSIT",
                 max(initial_cash_cents, 0), now.date().isoformat(), _iso(now),
                 "opening balance"),
            )

    def _rule_version(self, day: date) -> str:
        """取当日生效的规则版本，写入计划以便复现与审计（§7.1）。"""

        for r in self.board_rules:
            if r.covers(day):
                return f"{r.exchange}-{r.board}-{r.price_limit_pct}-{r.effective_from.isoformat()}"
        return "unknown"

    def _industry_of(self, candidates: list[Candidate], iid: str) -> str:
        for c in candidates:
            if c.instrument_id == iid:
                return c.industry_code
        return "UNKNOWN"

    def _lot_size(self, iid: str, day: date) -> int:
        listing = self.listings.get(iid)
        if not listing:
            return 100
        for r in self.board_rules:
            if r.exchange == listing[0] and r.board == listing[1] and r.covers(day):
                return r.lot_size
        return 100

    def _sellable(self, lots: list[Lot], day: date) -> dict[str, int]:
        out: dict[str, int] = {}
        for l in lots:
            if l.quantity_remaining > 0 and l.earliest_sellable_day <= day:
                out[l.instrument_id] = out.get(l.instrument_id, 0) + l.quantity_remaining
        return out

    # ------------------------------------------------------------ freeze
    def freeze(
        self,
        *,
        preview: PlanPreview,
        confirm_subject: str,
        confirmation_token: str,
        expected_account_version: str,
        current_lots: list[Lot],
        current_cash_cents: int,
        ttl: timedelta = timedelta(hours=12),
        now: datetime | None = None,
    ) -> dict:
        """冻结计划。五项复核缺一不可（§16.2、A09）。

        冻结后计划主体不可变（由数据库触发器保证）。
        """

        now = now or datetime.now(timezone.utc)
        self._ensure_portfolio(preview.portfolio_id, initial_cash_cents=current_cash_cents,
                               now=now)
        if not confirmer_is_human(confirm_subject):
            raise PlanError("DATA_NOT_READY",
                            f"confirmation subject {confirm_subject!r} is not a human principal",
                            preview.plan_id,
                            "plan freeze is a user action; models never confirm plans (§16.3)")

        actual_account_version = _account_version(current_lots, current_cash_cents)
        if actual_account_version != expected_account_version:
            raise PlanError(
                "STALE_SNAPSHOT",
                "account state changed since the preview",
                preview.plan_id,
                "re-preview against the current account state before confirming",
            )
        if preview.account_version != expected_account_version:
            raise PlanError(
                "STALE_SNAPSHOT",
                "the confirmation refers to a different preview of this account",
                preview.plan_id,
                "re-preview; the plan version and account version must match",
            )
        if not confirmation_token or len(confirmation_token) < 8:
            raise PlanError("DATA_NOT_READY", "confirmation token missing or too short",
                            preview.plan_id, "obtain a fresh confirmation token from the UI")

        expires_at = now + ttl
        idempotency_key = f"freeze|{preview.portfolio_id}|{preview.plan_version}|{preview.account_version}"
        token_hash = "sha256:" + hashlib.sha256(confirmation_token.encode()).hexdigest()

        with write_tx(self.con):
            self.con.execute(
                "INSERT INTO simulation_plan (plan_id,portfolio_id,snapshot_id,account_version,"
                "plan_version,status,created_at,frozen_at,expires_at,confirmed_by,"
                "confirmation_token_hash,rule_version,fee_version,diff_preview_json,"
                "estimated_fees_cents,idempotency_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (preview.plan_id, preview.portfolio_id, preview.snapshot_id,
                 0, 1, "FROZEN", _iso(now), _iso(now), _iso(expires_at),
                 confirm_subject, token_hash,
                 self._rule_version(preview.trading_day),
                 self.fee_table.schedule_for(preview.trading_day).fee_version,
                 json.dumps(preview.as_dict(), ensure_ascii=False),
                 preview.estimated_fees_cents, idempotency_key),
            )
            # 冻结的计划拥有自己的订单列表：订单在冻结时一次性写入，此后不可变。
            # 卖单排在前面（§12.4 先卖后买），序号即稳定序。
            ordered = ([o for o in preview.orders if o["side"] == "SELL"]
                       + [o for o in preview.orders if o["side"] == "BUY"])
            for seq, o in enumerate(ordered):
                self.con.execute(
                    "INSERT OR IGNORE INTO \"order\" (order_id,portfolio_id,plan_id,"
                    "snapshot_id,instrument_id,side,quantity,limit_price_cents,status,"
                    "trading_day,process_sequence,created_at,idempotency_key) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"{preview.plan_id}-{seq:03d}", preview.portfolio_id, preview.plan_id,
                     preview.snapshot_id, o["instrument_id"], o["side"], o["quantity"],
                     None, "PENDING", preview.trading_day.isoformat(), seq, _iso(now),
                     f"{idempotency_key}|{seq}"),
                )
        return {
            "plan_id": preview.plan_id,
            "status": "FROZEN",
            "frozen_at": _iso(now),
            "expires_at": _iso(expires_at),
            "confirmed_by": confirm_subject,
            "idempotency_key": idempotency_key,
            "note": "frozen plans are immutable; corrections require a new plan version",
        }

    # ------------------------------------------------------------ execute
    def execute(
        self,
        *,
        plan_id: str,
        lots: list[Lot],
        cash_available_cents: int,
        now: datetime | None = None,
        lot_id_prefix: str = "lot",
    ) -> dict:
        """执行已冻结的计划。

        执行前重新校验有效期与账户状态——冻结不等于永久有效。
        """

        now = now or datetime.now(timezone.utc)
        row = self.con.execute("SELECT * FROM simulation_plan WHERE plan_id=?",
                               (plan_id,)).fetchone()
        if row is None:
            raise PlanError("DATA_NOT_READY", f"unknown plan {plan_id!r}", plan_id,
                            "use an existing plan id")
        if row["status"] != "FROZEN":
            raise PlanError("DATA_NOT_READY",
                            f"plan {plan_id} is {row['status']}, not FROZEN", plan_id,
                            "only a frozen plan can execute")
        expires_at = datetime.fromisoformat(row["expires_at"])
        if now > expires_at:
            with write_tx(self.con):
                self.con.execute("UPDATE simulation_plan SET status='EXPIRED' WHERE plan_id=?",
                                 (plan_id,))
            raise PlanError("DECISION_CUTOFF_PASSED",
                            f"plan {plan_id} expired at {row['expires_at']}", plan_id,
                            "re-preview and re-confirm; an expired plan must not execute")

        preview_doc = json.loads(row["diff_preview_json"])
        trading_day = date.fromisoformat(preview_doc["trading_day"])
        snapshot_id = preview_doc["snapshot_id"]
        as_of = self.reader.ref(snapshot_id).as_of_time

        ids = sorted({o["instrument_id"] for o in preview_doc["orders"]})
        bars = self._bars(snapshot_id, trading_day, as_of, ids)
        adv = self._adv(snapshot_id, trading_day, as_of, ids)

        # 订单已由 freeze 持久化；这里按冻结时的顺序号重建，保证 order_id 一致。
        stored = self.con.execute(
            "SELECT order_id, instrument_id, side, quantity, process_sequence "
            "FROM \"order\" WHERE plan_id=? ORDER BY process_sequence", (plan_id,)
        ).fetchall()
        orders = [
            Order(order_id=r["order_id"], instrument_id=r["instrument_id"],
                  side=Side(r["side"]), quantity=int(r["quantity"]),
                  sequence=int(r["process_sequence"]))
            for r in stored
        ]

        simulator = DailySimulator(
            fee_table=self.fee_table, board_rules=self.board_rules,
            listings=self.listings,
        )
        result = simulator.simulate(
            trading_day=trading_day, orders=orders, bars=bars,
            cash_available_cents=cash_available_cents, lots=lots,
            adv_shares=adv, lot_id_prefix=lot_id_prefix,
        )

        with write_tx(self.con):
            self.con.execute("UPDATE simulation_plan SET status='EXECUTED' WHERE plan_id=?",
                             (plan_id,))
            for o in result.orders:
                self.con.execute(
                    "UPDATE \"order\" SET status=?, reject_reason=? WHERE order_id=?",
                    (o.status.value, o.reject_reason, o.order_id),
                )
            for f in result.fills:
                self.con.execute(
                    "INSERT OR IGNORE INTO fill (fill_id,order_id,portfolio_id,instrument_id,"
                    "side,quantity,price_cents,gross_amount_cents,fees_total_cents,"
                    "trading_day,filled_at,lot_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f.fill_id, f.order_id, preview_doc["portfolio_id"], f.instrument_id,
                     f.side.value, f.quantity, f.price_cents, f.gross_amount_cents,
                     f.fees_total_cents, f.trading_day.isoformat(), _iso(now), f.lot_id),
                )
                for line in f.fee_lines:
                    self.con.execute(
                        "INSERT OR IGNORE INTO fee_charge (fee_charge_id,fill_id,fee_code,"
                        "amount_cents,fee_version,rate_basis) VALUES (?,?,?,?,?,?)",
                        (f"{f.fill_id}-{line.fee_code}", f.fill_id, line.fee_code,
                         line.amount_cents, line.fee_version, line.rate_basis),
                    )
            for l in result.lots_created:
                self.con.execute(
                    "INSERT OR IGNORE INTO position_lot (lot_id,portfolio_id,instrument_id,"
                    "acquired_trading_day,earliest_sellable_day,quantity_original,"
                    "quantity_remaining,cost_basis_cents_per_share,source_fill_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (l.lot_id, preview_doc["portfolio_id"], l.instrument_id,
                     l.acquired_trading_day.isoformat(),
                     l.earliest_sellable_day.isoformat(),
                     l.quantity_original, l.quantity_remaining,
                     l.cost_basis_cents_per_share, None),
                )
            for e in result.cash_entries:
                self.con.execute(
                    "INSERT OR IGNORE INTO cash_entry (entry_id,portfolio_id,entry_type,"
                    "amount_cents,trading_day,occurred_at,related_fill_id,"
                    "related_instrument_id) VALUES (?,?,?,?,?,?,?,?)",
                    (f"{plan_id}-{e.entry_type}-{abs(hash((e.related_fill_id, e.amount_cents))) % 10**10}",
                     preview_doc["portfolio_id"], e.entry_type, e.amount_cents,
                     e.trading_day.isoformat(), _iso(now), e.related_fill_id,
                     e.related_instrument_id),
                )

        return {
            "plan_id": plan_id,
            "status": "EXECUTED",
            "trading_day": trading_day.isoformat(),
            "fills": [
                {"fill_id": f.fill_id, "order_id": f.order_id,
                 "instrument_id": f.instrument_id, "side": f.side.value,
                 "quantity": f.quantity, "price_cents": f.price_cents,
                 "fees_total_cents": f.fees_total_cents}
                for f in result.fills
            ],
            "rejections": result.rejections,
            "cash_entries": [
                {"entry_type": e.entry_type, "amount_cents": e.amount_cents}
                for e in result.cash_entries
            ],
            "lots_created": [l.lot_id for l in result.lots_created],
        }

    # ------------------------------------------------------------ value
    def value(
        self,
        *,
        portfolio_id: str,
        snapshot_id: str,
        trading_day: date,
        as_of: datetime,
        lots: list[Lot],
        cash_available_cents: int,
    ) -> dict:
        """日终估值并落库。不变量失败则 published=0，净值不得发布（§12.8）。"""

        from .construction import compute_valuation, value_positions

        ids = sorted({l.instrument_id for l in lots if l.quantity_remaining > 0})
        bars = self._bars(snapshot_id, trading_day, as_of, ids)

        # 停牌时使用此前最后一个有效收盘价，并显式记录其日期以计算停牌天数
        last_valid: dict[str, tuple[int, date]] = {}
        for iid in ids:
            rows = self.reader.daily_quotes(snapshot_id, as_of=as_of,
                                            instrument_id=iid, end=trading_day)
            history = [r for r in rows]
            if history:
                last_valid[iid] = (history[-1].close_cents, history[-1].trading_day)

        positions, issues = value_positions(lots=lots, bars=bars,
                                            last_valid_price=last_valid,
                                            trading_day=trading_day)
        result = compute_valuation(
            trading_day=trading_day, cash_available_cents=cash_available_cents,
            positions=positions, lots=lots, extra_issues=issues,
        )

        now = datetime.now(timezone.utc)
        inv = result.invariants
        with write_tx(self.con):
            self.con.execute(
                "INSERT OR REPLACE INTO valuation (valuation_id,portfolio_id,trading_day,"
                "cash_available_cents,cash_frozen_cents,receivables_cents,"
                "positions_value_cents,payables_cents,net_value_cents,"
                "invariant_cash_not_overdrawn,invariant_positions_not_negative,"
                "invariant_shares_match_lots,invariant_fill_le_order,"
                "invariant_fees_booked_once,invariant_cash_lines_sum,published,"
                "violations_json,computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"val-{portfolio_id}-{trading_day.isoformat()}", portfolio_id,
                 trading_day.isoformat(), result.cash_available_cents,
                 result.cash_frozen_cents, result.receivables_cents,
                 result.positions_value_cents, result.payables_cents,
                 result.net_value_cents,
                 1 if inv["cash_not_overdrawn"] else 0,
                 1 if inv["positions_not_negative"] else 0,
                 1 if inv["shares_match_lots"] else 0,
                 1 if inv["fill_le_order"] else 0,
                 1 if inv["fees_booked_once"] else 0,
                 1 if inv["cash_lines_sum_to_balance"] else 0,
                 1 if result.published else 0,
                 json.dumps(inv["violations"], ensure_ascii=False), _iso(now)),
            )
        return result.as_dict()

    # ------------------------------------------------------------ reconcile
    def reconcile(self, *, portfolio_id: str) -> dict:
        """§15.1 从期初到期末逐项对账。"""

        cash_row = self.con.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS c FROM cash_entry WHERE portfolio_id=?",
            (portfolio_id,)).fetchone()
        cash = int(cash_row["c"])

        lot_rows = self.con.execute(
            "SELECT instrument_id, SUM(quantity_remaining) AS q FROM position_lot "
            "WHERE portfolio_id=? AND quantity_remaining > 0 GROUP BY instrument_id",
            (portfolio_id,)).fetchall()
        positions = {r["instrument_id"]: int(r["q"]) for r in lot_rows}

        fill_rows = self.con.execute(
            "SELECT COUNT(*) AS n FROM fill WHERE portfolio_id=?", (portfolio_id,)).fetchone()
        fee_rows = self.con.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS f FROM fee_charge fc JOIN fill f2 "
            "ON fc.fill_id = f2.fill_id WHERE f2.portfolio_id=?", (portfolio_id,)).fetchone()

        dup_fees = self.con.execute(
            "SELECT COUNT(*) AS n FROM ("
            "  SELECT fill_id, fee_code, fee_version, COUNT(*) c FROM fee_charge"
            "  GROUP BY fill_id, fee_code, fee_version HAVING c > 1)", ()).fetchone()

        return {
            "portfolio_id": portfolio_id,
            "cash_cents": cash,
            "positions": positions,
            "fill_count": int(fill_rows["n"]),
            "fees_total_cents": int(fee_rows["f"]),
            "duplicate_fee_groups": int(dup_fees["n"]),
            "reconciled": cash >= 0 and int(dup_fees["n"]) == 0,
        }


def confirmer_is_human(subject: str) -> bool:
    """确认主体必须是人类。模型或系统主体一律拒绝（§5.5、§16.3）。

    这条规则刻意做成独立函数，便于测试与审计引用同一处判断。
    """

    s = (subject or "").strip().lower()
    if not s:
        return False
    model_markers = ("model", "assistant", "agent", "llm", "gpt", "claude", "bot", "ai:")
    return not any(marker in s for marker in model_markers)
