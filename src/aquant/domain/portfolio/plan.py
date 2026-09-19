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
import hmac
import json
import secrets
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..data.db import write_tx
from ..data.reader import SnapshotReader, QuoteRow
from ..data.snapshot import SnapshotError
from ..portfolio.construction import (
    Candidate,
    ConstructionParams,
    TargetWeight,
    construct_targets,
    listed_trading_days,
    weights_to_orders,
)
from ..simulation.corporate_actions import (
    CashDividend,
    apply_cash_dividend,
    record_dividend_entitlement,
)
from ..simulation.fees import FeeSchedule, FeeTable
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

_MARKET_TZ = ZoneInfo("Asia/Shanghai")
_A_SHARE_OPEN = time(9, 30)
_A_SHARE_CLOSE = time(15, 0)


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


def _market_datetime(day: date, at: time) -> datetime:
    """Return a timezone-aware A-share market boundary for ``day``."""

    return datetime.combine(day, at, tzinfo=_MARKET_TZ)


def _is_before_execution_open(cutoff: datetime, trading_day: date) -> bool:
    """Whether a decision cutoff is before the execution day's open.

    The comparison is deliberately made in Asia/Shanghai: the trading day is a
    local market date, while snapshot timestamps are stored as aware datetimes.
    """

    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        return False
    return cutoff.astimezone(_MARKET_TZ) < _market_datetime(trading_day, _A_SHARE_OPEN)


def _is_execution_day_close(cutoff: datetime, trading_day: date) -> bool:
    """执行快照必须属于交易日本日且不早于收盘，不能用未来快照回填。"""

    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        return False
    local = cutoff.astimezone(_MARKET_TZ)
    return local.date() == trading_day and local.time() >= _A_SHARE_CLOSE


def _account_version(lots: list[Lot], cash_cents: int) -> str:
    """账户状态版本：由持仓批次与可用现金决定。

    冻结时记录它、执行前再校验它——账户在预览与确认之间发生变化就必须重来（A09）。
    """

    payload = json.dumps(
        {
            "cash": cash_cents,
            "lots": sorted(
                [
                    l.lot_id, l.instrument_id, l.acquired_trading_day.isoformat(),
                    l.earliest_sellable_day.isoformat(), l.quantity_original,
                    l.quantity_remaining, l.cost_basis_cents_per_share,
                ]
                for l in lots
            ),
        },
        ensure_ascii=False, sort_keys=True,
    )
    return "acct-" + hashlib.sha256(payload.encode()).hexdigest()[:16]


def _plan_version(
    targets: list[TargetWeight], snapshot_id: str, trading_day: date, orders: list[dict],
    *, decision_cutoff_at: datetime | None = None,
    execution_snapshot_id: str | None = None,
    execution_cutoff_at: datetime | None = None,
) -> str:
    payload = json.dumps(
        {
            "snapshot": snapshot_id,
            "decision_cutoff_at": (_iso(decision_cutoff_at)
                                   if decision_cutoff_at is not None else None),
            "execution_snapshot": execution_snapshot_id,
            "execution_cutoff_at": (_iso(execution_cutoff_at)
                                    if execution_cutoff_at is not None else None),
            "trading_day": trading_day.isoformat(),
            "targets": sorted([t.instrument_id, str(t.weight_pct)] for t in targets),
            "orders": sorted(
                [
                    o["instrument_id"], o["side"], int(o["quantity"]),
                    int(o["price_cents"]), o.get("reference_price_day"),
                ]
                for o in orders
            ),
        },
        ensure_ascii=False, sort_keys=True,
    )
    return "plan-" + hashlib.sha256(payload.encode()).hexdigest()[:16]


# ======================================================================
@dataclass(slots=True)
class PlanPreview:
    plan_id: str
    portfolio_id: str
    # ``snapshot_id`` remains the public/backward-compatible name for the
    # decision snapshot.  The explicit fields below are persisted at freeze so
    # execution can never silently switch to the current pointer.
    snapshot_id: str
    plan_version: str
    account_version: str
    trading_day: date
    reference_price_day: date | None
    targets: list[TargetWeight]
    orders: list[dict]
    estimated_fees_cents: int
    rule_checks: list[dict]
    excluded: list[dict]
    cash_weight_pct: Decimal
    #: 按预览订单执行后的可用现金（分）。**服务端算**，不让界面自行推算：
    #: 前端算第二遍就会出现"界面上的数字与账本不一样"这类最难查的问题。
    cash_after_cents: int = 0
    #: 明确告知调用方：预览不冻结、不产生成交（A08）
    frozen: bool = False
    notes: list[str] = field(default_factory=list)
    decision_snapshot_id: str | None = None
    decision_cutoff_at: datetime | None = None
    execution_snapshot_id: str | None = None
    execution_cutoff_at: datetime | None = None

    def __post_init__(self) -> None:
        """Normalize the old ``snapshot_id`` call shape.

        Existing callers construct previews with one snapshot.  Treat that as
        an explicit decision/execution binding for synthetic and legacy flows;
        new callers can provide separate IDs and cutoffs.  Freeze still writes
        the normalized binding, so a later execute never consults process-local
        preview state.
        """

        if self.decision_snapshot_id is None:
            self.decision_snapshot_id = self.snapshot_id
        if self.execution_snapshot_id is None:
            self.execution_snapshot_id = self.snapshot_id

    def as_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "portfolio_id": self.portfolio_id,
            "snapshot_id": self.snapshot_id,
            "decision_snapshot_id": self.decision_snapshot_id,
            "decision_cutoff_at": (_iso(self.decision_cutoff_at)
                                    if self.decision_cutoff_at is not None else None),
            "execution_snapshot_id": self.execution_snapshot_id,
            "execution_cutoff_at": (_iso(self.execution_cutoff_at)
                                     if self.execution_cutoff_at is not None else None),
            "plan_version": self.plan_version,
            "account_version": self.account_version,
            "trading_day": self.trading_day.isoformat(),
            "reference_price_day": (self.reference_price_day.isoformat()
                                    if self.reference_price_day else None),
            "frozen": self.frozen,
            "orders": self.orders,
            "estimated_fees_cents": self.estimated_fees_cents,
            "rule_checks": self.rule_checks,
            "excluded": self.excluded,
            "cash_weight_pct": str(self.cash_weight_pct),
            "cash_after_cents": self.cash_after_cents,
            "notes": self.notes,
        }


def _preview_hash(preview: PlanPreview) -> str:
    """Hash the complete user-visible preview, including order quantities and prices."""

    payload = json.dumps(preview.as_dict(), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


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
    def load_persisted_plan(self, plan_id: str) -> sqlite3.Row | None:
        """Read the durable plan header for API orchestration.

        A preview is deliberately process-local, while a frozen plan is a
        database record.  Keeping this lookup in the domain service avoids an
        API caller having to reconstruct a portfolio or snapshot from request
        data just to decide whether an execute request can be routed.
        """

        return self.con.execute(
            "SELECT * FROM simulation_plan WHERE plan_id=?", (plan_id,)
        ).fetchone()

    def _snapshot_binding(self, preview: PlanPreview) -> dict[str, str]:
        """Return the explicit snapshot/cutoff values carried by a preview.

        The old preview shape only had ``snapshot_id`` and ``as_of``.  Resolve
        missing cutoff fields from the immutable snapshot manifest so a direct
        domain caller remains compatible while a frozen plan still gets a
        complete durable binding.
        """

        decision_id = preview.decision_snapshot_id or preview.snapshot_id
        execution_id = preview.execution_snapshot_id or decision_id
        if decision_id != preview.snapshot_id:
            raise PlanError(
                "PIT_UNVERIFIED",
                "preview snapshot_id disagrees with decision_snapshot_id",
                preview.plan_id,
                "rebuild the preview with one decision snapshot ID",
            )
        decision_ref = self.reader.ref(decision_id)
        execution_ref = self.reader.ref(execution_id)
        decision_cutoff = preview.decision_cutoff_at or decision_ref.as_of_time
        execution_cutoff = preview.execution_cutoff_at or execution_ref.as_of_time
        if decision_cutoff != decision_ref.as_of_time:
            raise PlanError(
                "PIT_UNVERIFIED",
                "preview decision cutoff does not equal its snapshot as_of_time",
                decision_id,
                "use the exact cutoff recorded by the decision snapshot",
            )
        if execution_cutoff != execution_ref.as_of_time:
            raise PlanError(
                "PIT_UNVERIFIED",
                "preview execution cutoff does not equal its snapshot as_of_time",
                execution_id,
                "use the exact cutoff recorded by the execution snapshot",
            )
        return {
            "decision_snapshot_id": decision_id,
            "decision_cutoff_at": _iso(decision_cutoff),
            "execution_snapshot_id": execution_id,
            "execution_cutoff_at": _iso(execution_cutoff),
        }

    def _load_snapshot_binding(self, plan_id: str, plan_row: sqlite3.Row) -> sqlite3.Row:
        """Load and verify the immutable binding for an executable plan."""

        try:
            binding = self.con.execute(
                "SELECT * FROM plan_snapshot_binding WHERE plan_id=?", (plan_id,)
            ).fetchone()
        except sqlite3.OperationalError as exc:
            raise PlanError(
                "DATA_NOT_READY",
                "plan snapshot binding table is unavailable",
                plan_id,
                "apply the plan snapshot binding migration before executing plans",
            ) from exc
        if binding is None:
            raise PlanError(
                "DATA_NOT_READY",
                "frozen plan has no decision/execution snapshot binding",
                plan_id,
                "freeze a new plan after applying the snapshot binding migration",
            )
        if binding["decision_snapshot_id"] != plan_row["snapshot_id"]:
            raise PlanError(
                "DATA_NOT_READY",
                "plan decision snapshot does not match its durable plan snapshot",
                plan_id,
                "create a new plan with consistent persisted snapshot bindings",
            )
        try:
            decision_ref = self.reader.ref(binding["decision_snapshot_id"])
            execution_ref = self.reader.ref(binding["execution_snapshot_id"])
            decision_cutoff = datetime.fromisoformat(binding["decision_cutoff_at"])
            execution_cutoff = datetime.fromisoformat(binding["execution_cutoff_at"])
        except (SnapshotError, TypeError, ValueError) as exc:
            raise PlanError(
                "DATA_NOT_READY",
                "frozen plan references an unreadable decision or execution snapshot",
                plan_id,
                "publish both snapshots and freeze a new plan",
            ) from exc
        if (decision_cutoff != decision_ref.as_of_time or
                execution_cutoff != execution_ref.as_of_time):
            raise PlanError(
                "PIT_UNVERIFIED",
                "snapshot binding cutoff no longer matches its immutable snapshot",
                plan_id,
                "repair the binding by creating a new frozen plan",
            )
        return binding

    def _assert_fee_table_allowed(self, snapshot_id: str, trading_day: date,
                                  *, fee_table: FeeTable | None = None) -> None:
        """真实数据上不得使用合成费率（§12.6）。

        放在 preview / freeze / execute / value 的入口，而不是只做成一个
        工具方法：工具方法不会自己被执行。原先
        assert_usable_for_formal_research() 只被一个测试调用过，
        于是真实快照上的成交与盈亏一直在用合成费率计算——
        **数字算得出来，只是没有依据**。

        preview 就拦是为了让使用者在看到草稿时就发现问题，
        而不是走到冻结那一步才被拒。
        """

        ref = self.reader.ref(snapshot_id)
        (fee_table or self.fee_table).assert_usable_for_data_mode(
            ref.data_mode, trading_day=trading_day)

    def _persist_plan_fee_binding(self, plan_id: str, trading_day: date) -> None:
        """冻结时保存当天实际使用的费率输入，而不只保存版本名。"""

        schedule = self.fee_table.schedule_for(trading_day)
        self.con.execute(
            "INSERT INTO plan_fee_binding (plan_id,fee_version,effective_from,effective_to,"
            "commission_rate,commission_min_cents,stamp_duty_rate_sell,"
            "transfer_fee_rate,synthetic_test_rate,commission_source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                plan_id, schedule.fee_version, schedule.effective_from.isoformat(),
                schedule.effective_to.isoformat() if schedule.effective_to else None,
                str(schedule.commission_rate), schedule.commission_min_cents,
                str(schedule.stamp_duty_rate_sell), str(schedule.transfer_fee_rate),
                1 if schedule.synthetic_test_rate else 0,
                self.fee_table.commission_source,
            ),
        )

    def _persisted_fee_table(self, plan_id: str) -> FeeTable:
        """恢复冻结计划的精确费率，避免重启后读取新的环境配置。"""

        row = self.con.execute(
            "SELECT * FROM plan_fee_binding WHERE plan_id=?", (plan_id,),
        ).fetchone()
        if row is None:
            raise PlanError(
                "DATA_NOT_READY",
                "frozen plan has no exact persisted fee inputs",
                plan_id,
                "create and freeze a new plan so its commission inputs are recorded",
            )
        return FeeTable([
            FeeSchedule(
                fee_version=row["fee_version"],
                effective_from=date.fromisoformat(row["effective_from"]),
                effective_to=(date.fromisoformat(row["effective_to"])
                              if row["effective_to"] else None),
                commission_rate=Decimal(row["commission_rate"]),
                commission_min_cents=int(row["commission_min_cents"]),
                stamp_duty_rate_sell=Decimal(row["stamp_duty_rate_sell"]),
                transfer_fee_rate=Decimal(row["transfer_fee_rate"]),
                synthetic_test_rate=bool(row["synthetic_test_rate"]),
            )
        ], commission_source=row["commission_source"])

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

    def _reference_bars(self, snapshot_id: str, trading_day: date, as_of: datetime,
                        instrument_ids: list[str]) -> dict[str, Bar]:
        """Latest closes strictly before execution day.

        Plans are formed before the execution-day open.  Using that day's close to size an
        order would leak future information, so preview and construction use only prior bars.
        """

        bars: dict[str, Bar] = {}
        for iid in instrument_ids:
            rows = self.reader.daily_quotes(snapshot_id, as_of=as_of,
                                            instrument_id=iid, end=trading_day)
            history = [r for r in rows if r.trading_day < trading_day]
            if not history:
                continue
            row = history[-1]
            bars[iid] = Bar(
                instrument_id=iid, trading_day=row.trading_day,
                open_cents=row.open_cents, high_cents=row.high_cents,
                low_cents=row.low_cents, close_cents=row.close_cents,
                prev_close_cents=row.prev_close_cents or row.open_cents,
                volume_shares=row.volume_shares,
                board_limit_up=row.board_limit_up,
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

    def _filter_by_liquidity(self, *, snapshot_id: str, trading_day: date,
                             as_of: datetime, candidates: list[Candidate],
                             ) -> tuple[list[Candidate], list[dict]]:
        """按近 N 日均成交额筛掉流动性不足的候选。

        三条不可让渡的规则：

        1. **未配置阈值就不筛**（阈值 None）。免费源的腾讯日线只有成交量、
           没有成交额，默认给一个阈值会让筛选把所有标的排除；
        2. **只看执行日之前已知的数据**，与 §12.4 的 ADV 口径一致；
        3. **成交额缺失就明确排除并说明原因**，绝不用 价格×成交量 补造——
           那是在用一个没观测到的数字做准入判断。
        """

        threshold = self.params.liquidity_min_avg_amount_cents
        if threshold is None:
            return list(candidates), []

        lookback = self.params.liquidity_lookback_days
        kept: list[Candidate] = []
        excluded: list[dict] = []
        for candidate in candidates:
            rows = self.reader.daily_quotes(
                snapshot_id, as_of=as_of, instrument_id=candidate.instrument_id,
                end=trading_day,
            )
            history = [r for r in rows if r.trading_day < trading_day][-lookback:]
            amounts = [r.amount_cents for r in history if r.amount_cents is not None]
            if not amounts:
                excluded.append({
                    "instrument_id": candidate.instrument_id,
                    "reason": "LIQUIDITY_UNVERIFIED",
                    "detail": (f"近 {lookback} 个交易日无成交额数据，无法证明满足"
                               f"流动性下限；不使用估算值"),
                })
                continue
            average = sum(amounts) // len(amounts)
            if average < threshold:
                excluded.append({
                    "instrument_id": candidate.instrument_id,
                    "reason": "LIQUIDITY_BELOW_MINIMUM",
                    "detail": (f"近 {lookback} 日均成交额 {average} 分 < 下限 {threshold} 分"),
                })
                continue
            kept.append(candidate)
        return kept, excluded

    def _filter_by_listing_age(self, *, snapshot_id: str, trading_day: date,
                               as_of: datetime, candidates: list[Candidate],
                               ) -> tuple[list[Candidate], list[dict], list[str]]:
        """§3.1 / §11.2：上市未满 ``exclude_listing_days`` 个交易日的标的不进模拟池。

        这条规则此前从未生效过，虽然参数一直在配置里：

          * `ConstructionParams.exclude_listing_days = 120` 声明了它，但
            全仓库没有任何一行读它——配置写着却不生效，比没有这条配置更糟，
            因为使用者会以为新股已经被排除了；
          * 免费源长期没有上市日期，`listed_on` 恒为 None，所以即使有人
            去读这个参数，也没有数据可供判断。数据补齐之后（BaoStock ipoDate），
            "参数没人读"这件事才暴露出来。

        **必须说清楚的一件事：门槛判不了"精确的 120 个交易日"。**
        研究池只有 61 个交易日的历史，快照日历也只有这么多天。一只 2019 年
        上市的股票，它的上市天数远超 120，但用快照日历去数只能数出 61。
        因此这里判的不是"上市天数"，而是**可判定的那一部分**：

          1. 上市日晚于决策日 —— 那时它还不是可交易证券（§7.1）；
          2. 上市日落在快照窗口**之内** —— 用窗口内的交易日数判定，
             窗口内不够 120 天，就必然不够 120 天（单调），结论可靠；
          3. 上市日在窗口起点**之前** —— 窗口内的天数不足以证明或否证门槛。
             此时**不排除**，并把"历史长度不足、门槛无法判定"写成一条 note。

        把情形 3 当成"不合格"会清空整池；当成"合格"而不留痕，则是拿未知做
        准入判断。两者都是本项目已经踩过的错。

        另外两条不可让渡的口径：

          * **交易日，不是自然日**。120 个自然日 ≈ 80 个交易日，用自然日
            近似会把门槛静默放宽三分之一。日历取自**已发布快照**。
          * **缺上市日期不排除，但必须留痕**（与情形 3 同理）。
        """

        threshold = self.params.exclude_listing_days
        if threshold <= 0:
            return list(candidates), [], []

        try:
            calendar = [date.fromisoformat(d)
                        for d in self.reader.trading_calendar(snapshot_id, as_of=as_of)]
        except (SnapshotError, FileNotFoundError) as exc:
            # 数据集"登记了但读不到"（哈希校验失败、文件被删）也是读不到。
            # 只吞这两种：它们是数据可用性问题，不是代码缺陷，
            # 而调用方要的是一句可修复的话，不是一个栈。
            calendar = []
            read_error = f"{type(exc).__name__}: {exc}"
        else:
            read_error = None
        if not calendar:
            # 没有日历就不判。用自然日顶替正是被禁止的那种近似。
            raise PlanError(
                "DATA_NOT_READY",
                f"snapshot {snapshot_id} carries no readable trading calendar, so the "
                f"{threshold}-trading-day listing gate cannot be evaluated"
                + (f"（{read_error}）" if read_error else ""),
                "trading_calendar",
                "publish the snapshot with its trading calendar, or set "
                "exclude_listing_days to 0 to state explicitly that newly listed "
                "instruments are allowed",
            ) from None
        window_start = calendar[0]
        instruments = {i["instrument_id"]: i
                       for i in self.reader.instruments(snapshot_id, as_of=as_of)}

        kept: list[Candidate] = []
        excluded: list[dict] = []
        unknown: list[str] = []
        partial: list[str] = []
        for candidate in candidates:
            raw = (instruments.get(candidate.instrument_id) or {}).get("listed_on")
            if not raw:
                kept.append(candidate)
                unknown.append(candidate.instrument_id)
                continue
            listed_on = date.fromisoformat(str(raw))

            if listed_on > trading_day:
                # 情形 1：决策时点还没上市
                excluded.append({
                    "instrument_id": candidate.instrument_id,
                    "reason": "NOT_LISTED_YET",
                    "detail": (f"上市日 {listed_on.isoformat()} 晚于决策日 "
                               f"{trading_day.isoformat()}（§7.1）"),
                })
                continue

            age = listed_trading_days(listed_on=listed_on, trading_day=trading_day,
                                      calendar=calendar)
            if listed_on >= window_start:
                # 情形 2：窗口内上市。窗口内不够，则一定不够（单调）。
                if age < threshold:
                    excluded.append({
                        "instrument_id": candidate.instrument_id,
                        "reason": "LISTED_TOO_RECENTLY",
                        "detail": (f"上市日 {listed_on.isoformat()} 落在快照窗口内，"
                                   f"截至 {trading_day.isoformat()} 仅 {age} 个交易日 "
                                   f"< 门槛 {threshold} 个交易日（§3.1）"),
                    })
                    continue
            else:
                # 情形 3：窗口外上市。快照日历覆盖不了门槛所需的长度。
                partial.append(candidate.instrument_id)
            kept.append(candidate)

        notes: list[str] = []
        if unknown:
            notes.append(
                f"{len(unknown)} 只标的缺少上市日期，未按 {threshold} 个交易日门槛"
                f"排除（未知不等于合格，也不等于不合格）：{sorted(unknown)[:5]}"
                + ("..." if len(unknown) > 5 else ""))
        if partial:
            notes.append(
                f"{len(partial)} 只标的的上市日早于快照窗口起点 "
                f"{window_start.isoformat()}，窗口内 "
                f"{listed_trading_days(listed_on=window_start, trading_day=trading_day, calendar=calendar)} "
                f"个交易日不足以判定 {threshold} 个交易日门槛；未排除，"
                f"其历史长度也未纳入因子计算范围")
        return kept, excluded, notes

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
        decision_snapshot_id: str | None = None,
        decision_cutoff_at: datetime | None = None,
        execution_snapshot_id: str | None = None,
    ) -> PlanPreview:
        """只算不冻。不写 simulation_plan，不写账本（A08）。"""

        # ``snapshot_id`` is the old name for the decision snapshot.  Keep it
        # as an alias, but make the two roles explicit before any data read.
        decision_id = decision_snapshot_id or snapshot_id
        if decision_id != snapshot_id:
            raise PlanError(
                "PIT_UNVERIFIED",
                "snapshot_id and decision_snapshot_id refer to different snapshots",
                portfolio_id,
                "pass the same decision snapshot ID through the legacy and explicit fields",
            )
        decision_ref = self.reader.ref(decision_id)
        decision_cutoff = decision_cutoff_at or as_of
        if decision_cutoff != decision_ref.as_of_time:
            raise PlanError(
                "PIT_UNVERIFIED",
                f"decision cutoff {decision_cutoff.isoformat()} does not equal "
                f"snapshot {decision_id} as_of_time {decision_ref.as_of_time.isoformat()}",
                decision_id,
                "use the snapshot whose as_of_time is exactly the decision cutoff",
            )

        # An old one-snapshot synthetic call remains usable for existing local
        # demos.  New callers that opt into explicit timing must prove that the
        # decision was made before the execution day's open and that the
        # execution snapshot was captured after that day's close.
        legacy_single_snapshot = (
            decision_snapshot_id is None
            and decision_cutoff_at is None
            and execution_snapshot_id is None
        )
        execution_id = execution_snapshot_id or decision_id
        execution_ref = self.reader.ref(execution_id)
        if execution_ref.data_mode != decision_ref.data_mode:
            raise PlanError(
                "PIT_UNVERIFIED",
                "decision and execution snapshots use different data modes",
                execution_id,
                "bind both sides to snapshots from the same data provenance",
            )
        if decision_ref.data_mode != "SYNTHETIC" and execution_id == decision_id:
            raise PlanError(
                "DATA_NOT_READY",
                "production plans require distinct decision and execution snapshots",
                decision_id,
                "publish and pass a pre-open decision snapshot plus an execution snapshot",
            )
        if legacy_single_snapshot and decision_ref.data_mode != "SYNTHETIC":
            raise PlanError(
                "DATA_NOT_READY",
                "production plans require decision_snapshot_id, decision_cutoff_at, "
                "and execution_snapshot_id",
                decision_id,
                "pass separate decision and execution snapshots; single-snapshot mode "
                "is only available for synthetic demos",
            )
        if not legacy_single_snapshot:
            if not _is_before_execution_open(decision_cutoff, trading_day):
                raise PlanError(
                    "PIT_UNVERIFIED",
                    f"decision cutoff {decision_cutoff.isoformat()} is not before "
                    f"the {trading_day.isoformat()} execution open",
                    decision_id,
                    "use an EOD or pre-open decision snapshot",
                )
            if not _is_execution_day_close(execution_ref.as_of_time, trading_day):
                raise PlanError(
                    "DATA_NOT_READY",
                    f"execution snapshot {execution_id} is not an end-of-day snapshot "
                    f"for {trading_day.isoformat()}",
                    execution_id,
                    "publish the execution-day EOD snapshot; do not use a later snapshot",
                )

        self._assert_fee_table_allowed(decision_id, trading_day)
        if not confirm_subject or not confirm_subject.strip():
            raise PlanError("DATA_NOT_READY", "confirm_subject is required for a plan",
                            portfolio_id, "identify the human confirming the plan")

        ids = sorted({c.instrument_id for c in candidates}
                     | {l.instrument_id for l in lots if l.quantity_remaining > 0})
        bars = self._reference_bars(decision_id, trading_day, decision_cutoff, ids)
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
        # 流动性在**此处**筛，不在 construct_targets 里：那里拿不到行情。
        # 配置里写着阈值却不生效，比没有这个配置更糟——使用者会以为
        # 组合已经按流动性筛过了。
        liquid, liquidity_excluded = self._filter_by_liquidity(
            snapshot_id=decision_id, trading_day=trading_day, as_of=decision_cutoff,
            candidates=candidates,
        )
        # 上市天数门槛同理：construct_targets 拿不到交易日历，
        # 而用自然日近似会静默放宽门槛（§3.1）。
        aged, listing_excluded, listing_notes = self._filter_by_listing_age(
            snapshot_id=decision_id, trading_day=trading_day, as_of=decision_cutoff,
            candidates=liquid,
        )
        construction = construct_targets(
            candidates=aged, params=self.params, held=held_qty,
            held_industry_value=held_industry_value, equity_value_cents=equity,
            bar_by_instrument=bars,
        )
        construction.excluded.extend(liquidity_excluded)
        construction.excluded.extend(listing_excluded)
        construction.notes.extend(listing_notes)

        orders = weights_to_orders(
            targets=construction.targets, params=self.params,
            price_by_instrument={i: b.close_cents for i, b in bars.items()},
            lot_size_by_instrument={i: self._lot_size(i, trading_day) for i in bars},
            held_quantity=held_qty, sellable_quantity=self._sellable(lots, trading_day),
            equity_value_cents=equity,
        )
        for order in orders:
            order["reference_price_day"] = bars[order["instrument_id"]].trading_day.isoformat()

        # 规则检查：每一条都给出结论与原因，不通过则不得冻结
        checks: list[dict] = []
        est_fees = 0
        for o in orders:
            iid = o["instrument_id"]
            bar = bars.get(iid)
            if bar is None:
                checks.append({"order": iid, "check": "HAS_REFERENCE_PRICE", "passed": False,
                               "detail": "no price known before the execution day"})
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
        # 执行后可用现金 = 现有现金 + 卖出所得 − 买入支出（含费用）。
        # 放在服务端算：界面自行推算就会出现"界面数字与账本不一致"，
        # 而且那种不一致只会在下单之后才被发现。
        cash_after_cents = cash_available_cents + sell_supply - buy_demand

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
        reference_days = [b.trading_day for b in bars.values()]
        return PlanPreview(
            plan_id=plan_id, portfolio_id=portfolio_id, snapshot_id=decision_id,
            plan_version=_plan_version(
                construction.targets, decision_id, trading_day, orders,
                decision_cutoff_at=decision_cutoff,
                execution_snapshot_id=execution_id,
                execution_cutoff_at=execution_ref.as_of_time,
            ),
            account_version=_account_version(lots, cash_available_cents),
            trading_day=trading_day,
            reference_price_day=max(reference_days) if reference_days else None,
            targets=construction.targets, orders=orders,
            estimated_fees_cents=est_fees, rule_checks=checks,
            excluded=construction.excluded, cash_weight_pct=construction.cash_weight_pct,
            cash_after_cents=cash_after_cents,
            frozen=False, notes=construction.notes,
            decision_snapshot_id=decision_id,
            decision_cutoff_at=decision_cutoff,
            execution_snapshot_id=execution_id,
            execution_cutoff_at=execution_ref.as_of_time,
        )

    def _load_lots(self, portfolio_id: str) -> list[Lot]:
        rows = self.con.execute(
            "SELECT lot_id,instrument_id,acquired_trading_day,earliest_sellable_day,"
            "quantity_original,quantity_remaining,cost_basis_cents_per_share "
            "FROM position_lot WHERE portfolio_id=? ORDER BY lot_id", (portfolio_id,)
        ).fetchall()
        return [
            Lot(
                lot_id=r["lot_id"], instrument_id=r["instrument_id"],
                acquired_trading_day=date.fromisoformat(r["acquired_trading_day"]),
                earliest_sellable_day=date.fromisoformat(r["earliest_sellable_day"]),
                quantity_original=int(r["quantity_original"]),
                quantity_remaining=int(r["quantity_remaining"]),
                cost_basis_cents_per_share=int(r["cost_basis_cents_per_share"]),
            )
            for r in rows
        ]

    def _ledger_cash(self, portfolio_id: str) -> int:
        row = self.con.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS cash FROM cash_entry "
            "WHERE portfolio_id=?", (portfolio_id,)
        ).fetchone()
        return int(row["cash"])

    def _ensure_account(self, portfolio_id: str, *, initial_cash_cents: int,
                        initial_lots: list[Lot], now: datetime,
                        enforce_match: bool = True) -> None:
        """Create the account once, then require callers to match its authoritative ledger.

        enforce_match=False 用于**只读入口**（如预览）：调用方此刻并没有声明账户状态，
        只是要求账户存在，因此不应拿一个占位开户金额去和已变化的账本比对。
        真正的比对发生在冻结与执行——那时调用方必须与账本一致。
        """

        existing = self.con.execute(
            "SELECT portfolio_id FROM portfolio WHERE portfolio_id=?", (portfolio_id,)
        ).fetchone()
        if existing is not None:
            if not enforce_match:
                return
            db_lots = self._load_lots(portfolio_id)
            db_cash = self._ledger_cash(portfolio_id)
            if _account_version(db_lots, db_cash) != _account_version(
                    initial_lots, initial_cash_cents):
                raise PlanError(
                    "STALE_SNAPSHOT", "supplied account state differs from the ledger",
                    portfolio_id,
                    "reload cash and lots from the portfolio ledger, then re-preview",
                )
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
            for lot in initial_lots:
                self.con.execute(
                    "INSERT INTO position_lot (lot_id,portfolio_id,instrument_id,"
                    "acquired_trading_day,earliest_sellable_day,quantity_original,"
                    "quantity_remaining,cost_basis_cents_per_share,source_fill_id) "
                    "VALUES (?,?,?,?,?,?,?,?,NULL)",
                    (lot.lot_id, portfolio_id, lot.instrument_id,
                     lot.acquired_trading_day.isoformat(),
                     lot.earliest_sellable_day.isoformat(), lot.quantity_original,
                     lot.quantity_remaining, lot.cost_basis_cents_per_share),
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

    def issue_confirmation(
        self,
        *,
        preview: PlanPreview,
        subject: str,
        current_lots: list[Lot],
        current_cash_cents: int,
        ttl: timedelta = timedelta(minutes=15),
        now: datetime | None = None,
    ) -> str:
        """Issue a one-time server token bound to this exact preview and account state."""

        now = now or datetime.now(timezone.utc)
        if not confirmer_is_human(subject):
            raise PlanError(
                "DATA_NOT_READY", f"confirmation subject {subject!r} is not a human principal",
                preview.plan_id,
                "plan freeze is a user action; models never confirm plans (§16.3)",
            )
        if ttl <= timedelta(0):
            raise PlanError("DATA_NOT_READY", "confirmation TTL must be positive",
                            preview.plan_id, "request a fresh confirmation")

        self._ensure_account(preview.portfolio_id, initial_cash_cents=current_cash_cents,
                             initial_lots=current_lots, now=now)
        actual_version = _account_version(
            self._load_lots(preview.portfolio_id), self._ledger_cash(preview.portfolio_id)
        )
        if actual_version != preview.account_version:
            raise PlanError("STALE_SNAPSHOT", "account state changed since the preview",
                            preview.plan_id, "re-preview against the current account state")

        expected_plan_version = _plan_version(
            preview.targets, preview.snapshot_id, preview.trading_day, preview.orders,
            decision_cutoff_at=preview.decision_cutoff_at,
            execution_snapshot_id=preview.execution_snapshot_id,
            execution_cutoff_at=preview.execution_cutoff_at,
        )
        if expected_plan_version != preview.plan_version:
            raise PlanError("STALE_SNAPSHOT", "preview contents changed after calculation",
                            preview.plan_id, "discard the modified preview and calculate it again")

        token = secrets.token_urlsafe(32)
        token_hash = "sha256:" + hashlib.sha256(token.encode()).hexdigest()
        with write_tx(self.con):
            self.con.execute(
                "INSERT INTO plan_confirmation (confirmation_id,token_hash,plan_id,portfolio_id,"
                "snapshot_id,plan_version,account_version,preview_hash,subject,issued_at,"
                "expires_at,consumed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
                ("pc_" + uuid.uuid4().hex, token_hash, preview.plan_id,
                 preview.portfolio_id, preview.snapshot_id, preview.plan_version,
                 preview.account_version, _preview_hash(preview), subject, _iso(now),
                 _iso(now + ttl)),
            )
        return token

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

        # 冻结与执行各自再查一次：preview 到 freeze 之间调用方可能换了费率表，
        # 而"预览时合法、冻结点不合法"是必须被拒的状态。
        self._assert_fee_table_allowed(preview.snapshot_id, preview.trading_day)
        now = now or datetime.now(timezone.utc)
        if not confirmer_is_human(confirm_subject):
            raise PlanError("DATA_NOT_READY",
                            f"confirmation subject {confirm_subject!r} is not a human principal",
                            preview.plan_id,
                            "plan freeze is a user action; models never confirm plans (§16.3)")

        self._ensure_account(preview.portfolio_id, initial_cash_cents=current_cash_cents,
                             initial_lots=current_lots, now=now)
        ledger_lots = self._load_lots(preview.portfolio_id)
        ledger_cash = self._ledger_cash(preview.portfolio_id)
        actual_account_version = _account_version(ledger_lots, ledger_cash)
        supplied_account_version = _account_version(current_lots, current_cash_cents)
        if supplied_account_version != actual_account_version:
            raise PlanError(
                "STALE_SNAPSHOT", "supplied account state differs from the ledger",
                preview.plan_id, "reload the account and re-preview before confirming",
            )
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
        if not confirmation_token:
            raise PlanError("DATA_NOT_READY", "confirmation token is missing",
                            preview.plan_id, "obtain a fresh confirmation token from the UI")

        current_plan_version = _plan_version(
            preview.targets, preview.snapshot_id, preview.trading_day, preview.orders,
            decision_cutoff_at=preview.decision_cutoff_at,
            execution_snapshot_id=preview.execution_snapshot_id,
            execution_cutoff_at=preview.execution_cutoff_at,
        )
        if current_plan_version != preview.plan_version:
            raise PlanError("STALE_SNAPSHOT", "preview contents changed after confirmation",
                            preview.plan_id, "calculate and confirm a new preview")
        snapshot_binding = self._snapshot_binding(preview)

        expires_at = now + ttl
        idempotency_key = f"freeze|{preview.portfolio_id}|{preview.plan_version}|{preview.account_version}"
        token_hash = "sha256:" + hashlib.sha256(confirmation_token.encode()).hexdigest()

        confirmation = self.con.execute(
            "SELECT * FROM plan_confirmation WHERE token_hash=?", (token_hash,)
        ).fetchone()
        if confirmation is None:
            raise PlanError("DATA_NOT_READY", "confirmation token is invalid",
                            preview.plan_id, "request and use a server-issued confirmation token")
        if confirmation["consumed_at"] is not None:
            raise PlanError("DATA_NOT_READY", "confirmation token was already consumed",
                            preview.plan_id, "request a fresh confirmation token")
        if now > datetime.fromisoformat(confirmation["expires_at"]):
            raise PlanError("DECISION_CUTOFF_PASSED", "confirmation token expired",
                            preview.plan_id, "review the current preview and confirm it again")
        bound_values = {
            "plan_id": preview.plan_id,
            "portfolio_id": preview.portfolio_id,
            "snapshot_id": preview.snapshot_id,
            "plan_version": preview.plan_version,
            "account_version": preview.account_version,
            "subject": confirm_subject,
        }
        for key, expected in bound_values.items():
            if not hmac.compare_digest(str(confirmation[key]), str(expected)):
                raise PlanError("STALE_SNAPSHOT", f"confirmation is bound to another {key}",
                                preview.plan_id, "request confirmation for this exact preview")
        if not hmac.compare_digest(confirmation["preview_hash"], _preview_hash(preview)):
            raise PlanError("STALE_SNAPSHOT", "preview changed after confirmation",
                            preview.plan_id, "calculate and confirm a new preview")

        with write_tx(self.con):
            try:
                self.con.execute(
                    "INSERT INTO simulation_plan (plan_id,portfolio_id,snapshot_id,"
                    "account_version,plan_version,status,created_at,frozen_at,expires_at,"
                    "confirmed_by,confirmation_token_hash,rule_version,fee_version,"
                    "diff_preview_json,estimated_fees_cents,idempotency_key) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (preview.plan_id, preview.portfolio_id, preview.snapshot_id,
                     preview.account_version, preview.plan_version, "FROZEN",
                     _iso(now), _iso(now), _iso(expires_at),
                     confirm_subject, token_hash,
                     self._rule_version(preview.trading_day),
                     self.fee_table.schedule_for(preview.trading_day).fee_version,
                     json.dumps(preview.as_dict(), ensure_ascii=False),
                     preview.estimated_fees_cents, idempotency_key),
                )
                self._persist_plan_fee_binding(preview.plan_id, preview.trading_day)
                self.con.execute(
                    "INSERT INTO plan_snapshot_binding ("
                    "plan_id,decision_snapshot_id,decision_cutoff_at,"
                    "execution_snapshot_id,execution_cutoff_at,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (preview.plan_id, snapshot_binding["decision_snapshot_id"],
                     snapshot_binding["decision_cutoff_at"],
                     snapshot_binding["execution_snapshot_id"],
                     snapshot_binding["execution_cutoff_at"], _iso(now)),
                )
            except sqlite3.IntegrityError as exc:
                # 幂等键（组合 + 计划版本 + 账户版本）已存在。
                #
                # 这不是异常状态，而是重复提交：用户在界面上再点一次冻结，
                # 或者换了个新令牌又冻结了同一份内容。原先这里直接让
                # IntegrityError 冒到 API 层，返回 500 Internal Server Error，
                # 于是一次无害的重复点击看起来像服务器崩了。
                #
                # 但如果既有计划的确认人不是当前主体，就**不能**当幂等返回：
                # 那等于把一个属于别人的冻结结果回给了调用方。
                if "idempotency_key" not in str(exc):
                    raise
                existing = self.con.execute(
                    "SELECT plan_id,status,confirmed_by,frozen_at,expires_at "
                    "FROM simulation_plan WHERE idempotency_key=?", (idempotency_key,),
                ).fetchone()
                if existing is None:
                    raise
                if not hmac.compare_digest(str(existing["confirmed_by"]),
                                           str(confirm_subject)):
                    raise PlanError(
                        "STALE_SNAPSHOT",
                        "this plan was already frozen by another subject",
                        preview.plan_id,
                        "use the plan that was frozen, or re-preview under your own account",
                    )
                self.con.execute(
                    "UPDATE plan_confirmation SET consumed_at=? WHERE confirmation_id=? "
                    "AND consumed_at IS NULL",
                    (_iso(now), confirmation["confirmation_id"]),
                )
                return {
                    "plan_id": existing["plan_id"],
                    "status": existing["status"],
                    "decision_snapshot_id": snapshot_binding["decision_snapshot_id"],
                    "decision_cutoff_at": snapshot_binding["decision_cutoff_at"],
                    "execution_snapshot_id": snapshot_binding["execution_snapshot_id"],
                    "execution_cutoff_at": snapshot_binding["execution_cutoff_at"],
                    "frozen_at": existing["frozen_at"],
                    "expires_at": existing["expires_at"],
                    "confirmed_by": existing["confirmed_by"],
                    "idempotency_key": idempotency_key,
                    "idempotent_replay": True,
                    "note": ("freeze already applied for this plan version and account "
                             "version; returned the existing frozen plan instead of "
                             "creating a second one"),
                }
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
            self.con.execute(
                "UPDATE plan_confirmation SET consumed_at=? WHERE confirmation_id=? "
                "AND consumed_at IS NULL",
                (_iso(now), confirmation["confirmation_id"]),
            )
        return {
            "plan_id": preview.plan_id,
            "status": "FROZEN",
            "decision_snapshot_id": snapshot_binding["decision_snapshot_id"],
            "decision_cutoff_at": snapshot_binding["decision_cutoff_at"],
            "execution_snapshot_id": snapshot_binding["execution_snapshot_id"],
            "execution_cutoff_at": snapshot_binding["execution_cutoff_at"],
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
        lots: list[Lot] | None = None,
        cash_available_cents: int | None = None,
        subject: str | None = None,
        now: datetime | None = None,
        lot_id_prefix: str = "lot",
        corporate_actions: list[CashDividend] | None = None,
    ) -> dict:
        """执行已冻结的计划。

        执行前重新校验有效期与账户状态——冻结不等于永久有效。

        corporate_actions: 当日落在除权日/到账日的现金分红。权利由
        **登记日**收盘持仓决定，而登记日必然早于除权日，因此用执行前的
        账本批次计算权利是正确的——当日成交不会改变已经固化的权利。
        """

        now = now or datetime.now(timezone.utc)
        row = self.load_persisted_plan(plan_id)
        if row is None:
            raise PlanError("DATA_NOT_READY", f"unknown plan {plan_id!r}", plan_id,
                            "use an existing plan id")
        # Execute is also an authenticated API operation.  The current schema does
        # not have a portfolio-owner relation; the durable ownership fact available
        # to us is the human subject that confirmed this frozen plan.  Keep this
        # check in the domain service so callers that survive an AppState rebuild
        # cannot bypass it by supplying a different in-memory preview.
        if subject is not None:
            if not confirmer_is_human(subject):
                raise PlanError(
                    "DATA_NOT_READY",
                    f"execution subject {subject!r} is not a human principal",
                    plan_id,
                    "execute the plan as the authenticated human user",
                )
            confirmed_by = row["confirmed_by"]
            if not confirmed_by:
                raise PlanError(
                    "DATA_NOT_READY",
                    "frozen plan has no durable confirmation subject",
                    plan_id,
                    "freeze the plan again with an authenticated human user",
                )
            if not hmac.compare_digest(str(confirmed_by), str(subject)):
                raise PlanError(
                    "DATA_NOT_READY",
                    "execution subject does not match the subject that confirmed this plan",
                    plan_id,
                    "execute the plan with the authenticated subject that froze it",
                )
        # 执行前再查一次费率表：这里的每一笔费用都会真的写进账本，
        # 用合成费率记出来的盈亏没有依据。
        if row["status"] != "FROZEN":
            raise PlanError("DATA_NOT_READY",
                            f"plan {plan_id} is {row['status']}, not FROZEN", plan_id,
                            "only a frozen plan can execute")
        snapshot_binding = self._load_snapshot_binding(plan_id, row)
        try:
            preview_doc = json.loads(row["diff_preview_json"])
            trading_day = date.fromisoformat(preview_doc["trading_day"])
        except (TypeError, ValueError, json.JSONDecodeError, KeyError) as exc:
            raise PlanError(
                "DATA_NOT_READY",
                "frozen plan has no valid persisted trading day",
                plan_id,
                "create a new preview and freeze a complete plan",
            ) from exc
        decision_ref = self.reader.ref(snapshot_binding["decision_snapshot_id"])
        decision_cutoff = datetime.fromisoformat(snapshot_binding["decision_cutoff_at"])
        execution_cutoff = datetime.fromisoformat(snapshot_binding["execution_cutoff_at"])
        if decision_ref.data_mode != "SYNTHETIC":
            if not _is_before_execution_open(decision_cutoff, trading_day):
                raise PlanError(
                    "PIT_UNVERIFIED", "persisted decision cutoff is not before execution open",
                    plan_id, "freeze a new plan with a valid decision snapshot")
            if not _is_execution_day_close(execution_cutoff, trading_day):
                raise PlanError(
                    "PIT_UNVERIFIED", "persisted execution snapshot is not from execution-day close",
                    plan_id, "freeze a new plan with the execution-day EOD snapshot")
        execution_fees = self._persisted_fee_table(plan_id)
        self._assert_fee_table_allowed(
            row["snapshot_id"], trading_day, fee_table=execution_fees)
        expires_at = datetime.fromisoformat(row["expires_at"])
        if now > expires_at:
            with write_tx(self.con):
                self.con.execute("UPDATE simulation_plan SET status='EXPIRED' WHERE plan_id=?",
                                 (plan_id,))
            raise PlanError("DECISION_CUTOFF_PASSED",
                            f"plan {plan_id} expired at {row['expires_at']}", plan_id,
                            "re-preview and re-confirm; an expired plan must not execute")

        # These identity fields come from the durable plan row.  The JSON preview
        # is retained as audit evidence, but it is not an authority for which
        # portfolio or snapshot an execute request may touch.
        # ``snapshot_id`` on the legacy plan row is the decision snapshot.
        # Execution data is always read from the immutable binding instead.
        snapshot_id = snapshot_binding["decision_snapshot_id"]
        execution_snapshot_id = snapshot_binding["execution_snapshot_id"]
        portfolio_id = row["portfolio_id"]
        portfolio = self.con.execute(
            "SELECT portfolio_id,status FROM portfolio WHERE portfolio_id=?",
            (portfolio_id,),
        ).fetchone()
        if portfolio is None:
            raise PlanError(
                "DATA_NOT_READY",
                f"frozen plan references missing portfolio {portfolio_id!r}",
                plan_id,
                "restore the referenced portfolio or create a new plan",
            )
        if portfolio["status"] != "ACTIVE":
            raise PlanError(
                "DATA_NOT_READY",
                f"portfolio {portfolio_id!r} is {portfolio['status']}",
                plan_id,
                "execute only while the referenced portfolio is active",
            )
        execution_as_of = execution_cutoff

        ledger_lots = self._load_lots(portfolio_id)
        ledger_cash = self._ledger_cash(portfolio_id)
        supplied_lots = ledger_lots if lots is None else lots
        supplied_cash = ledger_cash if cash_available_cents is None else cash_available_cents
        ledger_version = _account_version(ledger_lots, ledger_cash)
        supplied_version = _account_version(supplied_lots, supplied_cash)
        if supplied_version != ledger_version:
            raise PlanError("STALE_SNAPSHOT", "supplied account state differs from the ledger",
                            plan_id, "reload the account before executing")
        if ledger_version != row["account_version"]:
            raise PlanError("STALE_SNAPSHOT", "account changed after the plan was frozen",
                            plan_id, "re-preview and confirm against the current ledger")

        # Orders are immutable children of the frozen plan.  Load them from the
        # order table, and verify their durable portfolio/snapshot bindings before
        # simulating.  This makes a restart independent of AppState.previews and
        # prevents a stale preview document from redirecting execution.
        stored = self.con.execute(
            "SELECT order_id,portfolio_id,snapshot_id,instrument_id,side,quantity,"
            "process_sequence,trading_day "
            "FROM \"order\" WHERE plan_id=? ORDER BY process_sequence",
            (plan_id,),
        ).fetchall()
        for stored_order in stored:
            if (stored_order["portfolio_id"] != portfolio_id or
                    stored_order["snapshot_id"] != snapshot_id):
                raise PlanError(
                    "DATA_NOT_READY",
                    "frozen plan contains an order bound to another portfolio or snapshot",
                    plan_id,
                    "create a new plan after repairing the persisted order bindings",
                )
            if stored_order["trading_day"] != trading_day.isoformat():
                raise PlanError(
                    "DATA_NOT_READY",
                    "frozen plan contains an order for a different trading day",
                    plan_id,
                    "create a new plan with a consistent persisted order set",
                )
        expected_order_count = len(preview_doc.get("orders", []))
        if len(stored) != expected_order_count:
            raise PlanError(
                "DATA_NOT_READY",
                "frozen plan order set is incomplete",
                plan_id,
                "restore the persisted orders or create a new plan",
            )
        ids = sorted({o["instrument_id"] for o in stored})
        bars = self._bars(execution_snapshot_id, trading_day, execution_as_of, ids)
        # A missing bar for one instrument can mean a genuine suspension; the
        # simulator must preserve that no-fill outcome.  Missing/unpublished
        # execution snapshots are rejected above by ``_load_snapshot_binding``
        # and ``reader.ref`` rather than guessed from the decision snapshot.
        adv = self._adv(execution_snapshot_id, trading_day, execution_as_of, ids)
        # 执行可能发生在进程重启或 current 指针前移之后。板块决定涨跌停与
        # 手数，必须从计划冻结的快照恢复，不能沿用 AppState 当前快照的映射。
        snapshot_listings = {
            inst["instrument_id"]: (inst["exchange"], inst["board"])
            for inst in self.reader.instruments(execution_snapshot_id, as_of=execution_as_of)
            if inst.get("instrument_id") and inst.get("exchange") and inst.get("board")
        }

        # 订单已由 freeze 持久化；这里按冻结时的顺序号重建，保证 order_id 一致。
        orders = [
            Order(order_id=r["order_id"], instrument_id=r["instrument_id"],
                  side=Side(r["side"]), quantity=int(r["quantity"]),
                  sequence=int(r["process_sequence"]))
            for r in stored
        ]

        simulator = DailySimulator(
            fee_table=execution_fees, board_rules=self.board_rules,
            listings=snapshot_listings,
        )
        # simulate() 会**就地修改**传入的批次（扣减 quantity_remaining）。
        # 因此必须先留一份成交前快照：
        #   * 分红权利按登记日收盘持仓算，而当日买入也享有权利，
        #     所以要用"成交后"的持仓 —— 那是 ledger_lots（已被就地扣减）
        #     加上 result.lots_created；
        #   * 但若把 ledger_lots + lots_created 当成两份来源再相加，
        #     同一批次会被计两次，股数被扣两遍——第一批卖出会凭空翻倍。
        # 这里的 _pre_trade_lots 只用于落库对比，不参与权利计算。
        _pre_trade_lots = [
            Lot(lot_id=l.lot_id, instrument_id=l.instrument_id,
                acquired_trading_day=l.acquired_trading_day,
                earliest_sellable_day=l.earliest_sellable_day,
                quantity_original=l.quantity_original,
                quantity_remaining=l.quantity_remaining,
                cost_basis_cents_per_share=l.cost_basis_cents_per_share)
            for l in ledger_lots
        ]
        result = simulator.simulate(
            trading_day=trading_day, orders=orders, bars=bars,
            cash_available_cents=ledger_cash, lots=ledger_lots,
            adv_shares=adv,
            lot_id_prefix=f"{lot_id_prefix}-{portfolio_id}-{plan_id}",
        )
        # 就地修改之后，ledger_lots 自身就已经是成交后的完整持仓。
        # simulate() 会把当日新建批次**追加进同一个 lots 列表**
        # （simulator.py: lots.append(lot)），因此再拼一次 result.lots_created
        # 会让同一批次出现两次、股数被扣两遍——第一批卖出会凭空翻倍。
        # 下面是这个不变量的机器化断言，防止以后重新踩进来。
        _ids = [l.lot_id for l in ledger_lots]
        assert len(_ids) == len(set(_ids)), "成交后持仓出现重复批次"
        assert all(
            l.quantity_remaining <= before.quantity_remaining
            for l, before in zip(ledger_lots, _pre_trade_lots)
        ), "simulate 必须只减不增地就地扣减批次"
        assert all(any(l.lot_id == c.lot_id for l in ledger_lots)
                   for c in result.lots_created), "新建批次必须已在成交后持仓里"

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
                    (f.fill_id, f.order_id, portfolio_id, f.instrument_id,
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
            created_ids = {l.lot_id for l in result.lots_created}
            for l in ledger_lots:
                if l.lot_id in created_ids:
                    continue
                self.con.execute(
                    "UPDATE position_lot SET quantity_remaining=? WHERE lot_id=? "
                    "AND portfolio_id=?",
                    (l.quantity_remaining, l.lot_id, portfolio_id),
                )
            source_fill = {f.lot_id: f.fill_id for f in result.fills if f.lot_id}
            for l in result.lots_created:
                self.con.execute(
                    "INSERT INTO position_lot (lot_id,portfolio_id,instrument_id,"
                    "acquired_trading_day,earliest_sellable_day,quantity_original,"
                    "quantity_remaining,cost_basis_cents_per_share,source_fill_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (l.lot_id, portfolio_id, l.instrument_id,
                     l.acquired_trading_day.isoformat(),
                     l.earliest_sellable_day.isoformat(),
                     l.quantity_original, l.quantity_remaining,
                     l.cost_basis_cents_per_share, source_fill[l.lot_id]),
                )
            for seq, consumption in enumerate(result.lot_consumptions):
                self.con.execute(
                    "INSERT INTO lot_consumption (consumption_id,lot_id,fill_id,quantity,"
                    "trading_day) VALUES (?,?,?,?,?)",
                    (f"{plan_id}-consume-{seq:04d}", consumption["lot_id"],
                     consumption["fill_id"], consumption["quantity"],
                     consumption["trading_day"].isoformat()),
                )
            for seq, e in enumerate(result.cash_entries):
                self.con.execute(
                    "INSERT INTO cash_entry (entry_id,portfolio_id,entry_type,"
                    "amount_cents,trading_day,occurred_at,related_fill_id,"
                    "related_instrument_id) VALUES (?,?,?,?,?,?,?,?)",
                    (f"{plan_id}-cash-{seq:04d}", portfolio_id, e.entry_type, e.amount_cents,
                     e.trading_day.isoformat(), _iso(now), e.related_fill_id,
                     e.related_instrument_id),
                )

        # §12.7 公司行为在成交之后推进，权利取**登记日收盘**的持仓。
        #
        # 这里必须用"已含当日成交"的批次，而不是执行前的账本：
        #   * 模拟器对传入的 ledger_lots **就地**扣减 quantity_remaining，
        #     所以 ledger_lots 此刻已经是成交后的状态；
        #   * 当日买入产生的新批次在 result.lots_created 里，必须补上，
        #     否则"登记日当天买入"会被判成无权利，凭空少算一笔应收；
        #   * 登记日前卖光的批次 quantity_remaining 已是 0，自然不享权。
        # 两个方向都不需要额外特判——错在取哪一份批次，不在权利算法。
        dividend_outcomes = self._advance_corporate_actions(
            portfolio_id=portfolio_id, trading_day=trading_day,
            actions=corporate_actions or (),
            lots=self._entitlement_lots(_pre_trade_lots, ledger_lots), now=now,
        )

        # Preserve the public API's in-memory view while the database remains authoritative.
        if lots is not None:
            lots[:] = ledger_lots

        return {
            "plan_id": plan_id,
            "status": "EXECUTED",
            "trading_day": trading_day.isoformat(),
            "decision_snapshot_id": snapshot_binding["decision_snapshot_id"],
            "decision_cutoff_at": snapshot_binding["decision_cutoff_at"],
            "execution_snapshot_id": snapshot_binding["execution_snapshot_id"],
            "execution_cutoff_at": snapshot_binding["execution_cutoff_at"],
            "corporate_actions": dividend_outcomes,
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
    def _ledger_invariants(self, portfolio_id: str) -> dict:
        violations: list[dict] = []

        bad_fills = self.con.execute(
            "SELECT COUNT(*) AS n FROM fill f JOIN \"order\" o ON o.order_id=f.order_id "
            "WHERE f.portfolio_id=? AND (f.quantity>o.quantity OR f.side<>o.side "
            "OR f.instrument_id<>o.instrument_id)", (portfolio_id,),
        ).fetchone()["n"]
        if bad_fills:
            violations.append({"invariant": "fill_le_order", "count": int(bad_fills)})

        fee_mismatches = self.con.execute(
            "SELECT COUNT(*) AS n FROM (SELECT f.fill_id FROM fill f "
            "LEFT JOIN fee_charge fc ON fc.fill_id=f.fill_id WHERE f.portfolio_id=? "
            "GROUP BY f.fill_id,f.fees_total_cents "
            "HAVING COALESCE(SUM(fc.amount_cents),0)<>f.fees_total_cents)",
            (portfolio_id,),
        ).fetchone()["n"]
        duplicate_fees = self.con.execute(
            "SELECT COUNT(*) AS n FROM (SELECT fc.fill_id,fc.fee_code,fc.fee_version "
            "FROM fee_charge fc JOIN fill f ON f.fill_id=fc.fill_id "
            "WHERE f.portfolio_id=? GROUP BY fc.fill_id,fc.fee_code,fc.fee_version "
            "HAVING COUNT(*)>1)", (portfolio_id,),
        ).fetchone()["n"]
        fees_ok = int(fee_mismatches) == 0 and int(duplicate_fees) == 0
        if not fees_ok:
            violations.append({"invariant": "fees_booked_once",
                               "mismatches": int(fee_mismatches),
                               "duplicates": int(duplicate_fees)})

        cash_mismatches = 0
        for r in self.con.execute(
            "SELECT f.fill_id,f.side,f.gross_amount_cents,f.fees_total_cents,"
            "COALESCE(SUM(CASE WHEN ce.entry_type='TRADE_SETTLEMENT' "
            "THEN ce.amount_cents ELSE 0 END),0) AS settlement,"
            "COALESCE(SUM(CASE WHEN ce.entry_type<>'TRADE_SETTLEMENT' "
            "THEN ce.amount_cents ELSE 0 END),0) AS fees "
            "FROM fill f LEFT JOIN cash_entry ce ON ce.related_fill_id=f.fill_id "
            "WHERE f.portfolio_id=? GROUP BY f.fill_id,f.side,f.gross_amount_cents,"
            "f.fees_total_cents", (portfolio_id,),
        ).fetchall():
            expected_settlement = (int(r["gross_amount_cents"])
                                   if r["side"] == "SELL"
                                   else -int(r["gross_amount_cents"]))
            if (int(r["settlement"]) != expected_settlement
                    or int(r["fees"]) != -int(r["fees_total_cents"])):
                cash_mismatches += 1
        opening = self.con.execute(
            "SELECT p.initial_cash_cents,COUNT(ce.entry_id) AS n,"
            "COALESCE(SUM(ce.amount_cents),0) AS amount FROM portfolio p "
            "LEFT JOIN cash_entry ce ON ce.portfolio_id=p.portfolio_id "
            "AND ce.entry_type='INITIAL_DEPOSIT' WHERE p.portfolio_id=? "
            "GROUP BY p.initial_cash_cents", (portfolio_id,),
        ).fetchone()
        opening_ok = (opening is not None and int(opening["n"]) == 1
                      and int(opening["amount"]) == int(opening["initial_cash_cents"]))
        cash_ok = cash_mismatches == 0 and opening_ok
        if not cash_ok:
            violations.append({"invariant": "cash_lines_sum_to_balance",
                               "fill_mismatches": cash_mismatches,
                               "opening_balance_matches": opening_ok})

        buy_lot_mismatches = self.con.execute(
            "SELECT COUNT(*) AS n FROM (SELECT f.fill_id,f.quantity,"
            "COALESCE(SUM(l.quantity_original),0) AS q FROM fill f "
            "LEFT JOIN position_lot l ON l.source_fill_id=f.fill_id "
            "WHERE f.portfolio_id=? AND f.side='BUY' "
            "GROUP BY f.fill_id,f.quantity HAVING q<>f.quantity)", (portfolio_id,),
        ).fetchone()["n"]
        sell_lot_mismatches = self.con.execute(
            "SELECT COUNT(*) AS n FROM (SELECT f.fill_id,f.quantity,"
            "COALESCE(SUM(lc.quantity),0) AS q FROM fill f "
            "LEFT JOIN lot_consumption lc ON lc.fill_id=f.fill_id "
            "WHERE f.portfolio_id=? AND f.side='SELL' "
            "GROUP BY f.fill_id,f.quantity HAVING q<>f.quantity)", (portfolio_id,),
        ).fetchone()["n"]
        lots_ok = int(buy_lot_mismatches) == 0 and int(sell_lot_mismatches) == 0
        if not lots_ok:
            violations.append({"invariant": "lots_match_fills",
                               "buy_mismatches": int(buy_lot_mismatches),
                               "sell_mismatches": int(sell_lot_mismatches)})

        return {
            "fill_le_order": int(bad_fills) == 0 and lots_ok,
            "fees_booked_once": fees_ok,
            "cash_lines_sum_to_balance": cash_ok,
            "lots_match_fills": lots_ok,
            "violations": violations,
        }

    # --------------------------------------------------- corporate actions
    @staticmethod
    def _entitlement_lots(pre_trade: list[Lot], post_trade: list[Lot]) -> list[Lot]:
        """登记日盘后的持仓口径。

        权利看**登记日收盘持仓**，因此包含当日买入；而当日卖出**不会**
        丧失已经固化的权利。逐笔模拟器先卖后买（§12.4），如果直接拿
        成交后持仓去算权利，当日卖出的持仓会被判成"没持有过"，
        凭空少算一笔应收。

        所以按证券取"成交前"与"成交后"剩余股数的**较大值**：
          * 当日买入 → 成交后更多 → 计入；
          * 当日卖出 → 成交前更多 → 仍然计入；
          * 登记日前已卖光 → 两者都是 0 → 不计入。
        只有日线数据时无法知道盘中先后，这个口径在两个方向上都不会漏记。
        """

        def by_lot_id(lots: list[Lot]) -> dict[str, Lot]:
            """按批次 ID 去重，同一批次只保留剩余股数最大的那份。

            这里必须去重而不是直接求和：simulate() 会把新建批次追加进
            传入的列表，任何调用方只要再拼一次 result.lots_created，
            同一批次就会出现两次、股数被计两遍。这个函数是不变量防线，
            应当对错误输入免疫，而不是跟着一起算错。
            """

            out: dict[str, Lot] = {}
            for lot in lots:
                current = out.get(lot.lot_id)
                if current is None or lot.quantity_remaining > current.quantity_remaining:
                    out[lot.lot_id] = lot
            return out

        before_by_id = by_lot_id(pre_trade)
        after_by_id = by_lot_id(post_trade)

        def quantity_of(lots: dict[str, Lot], instrument_id: str) -> int:
            return sum(l.quantity_remaining for l in lots.values()
                       if l.instrument_id == instrument_id)

        def entitlement_quantity(instrument_id: str) -> int:
            return max(quantity_of(before_by_id, instrument_id),
                       quantity_of(after_by_id, instrument_id))

        instruments = ({l.instrument_id for l in before_by_id.values()}
                       | {l.instrument_id for l in after_by_id.values()})
        out: list[Lot] = []
        for instrument_id in sorted(instruments):
            quantity = entitlement_quantity(instrument_id)
            if quantity <= 0:
                continue
            # 权利只关心"持有多少股"与"登记日是否已持有"。取成交前批次里
            # 最早的那一天作为 acquired_trading_day：当日买入也享有权利，
            # 因此 acquired 用执行日同样成立。
            candidates = [l for l in before_by_id.values()
                          if l.instrument_id == instrument_id]
            if not candidates:
                candidates = [l for l in after_by_id.values()
                              if l.instrument_id == instrument_id]
            first = min(candidates, key=lambda l: l.acquired_trading_day)
            out.append(Lot(
                lot_id=f"entitlement-{instrument_id}", instrument_id=instrument_id,
                acquired_trading_day=first.acquired_trading_day,
                earliest_sellable_day=first.earliest_sellable_day,
                quantity_original=quantity, quantity_remaining=quantity,
                cost_basis_cents_per_share=first.cost_basis_cents_per_share,
            ))
        return out

    def _open_receivables(self, portfolio_id: str) -> int:
        """尚未结清的应收合计（分）。

        估值里的 receivables_cents 必须来自这张表，**不能由调用方传入**：
        应收是账本事实，不是当次调用的参数。传参就意味着调用方可以选择
        不报或少报，那样净值就成了"你输入什么就是什么"。
        """

        row = self.con.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS total FROM receivable "
            "WHERE portfolio_id=? AND status='RECOGNIZED'", (portfolio_id,),
        ).fetchone()
        return int(row["total"])

    def _advance_corporate_actions(
        self,
        *,
        portfolio_id: str,
        trading_day: date,
        actions: "list[CashDividend] | tuple[CashDividend, ...]",
        lots: list[Lot],
        now: datetime,
    ) -> list[dict]:
        """把当日落在除权日/到账日的现金分红落库，返回可读的结果。

        除权日  -> 新增一条 RECOGNIZED 应收，**现金不变**
        到账日  -> 把该应收置为 SETTLED，并记一笔 DIVIDEND 现金分录

        两条路径都要求存在一条 RECOGNIZED 的应收；首次执行才补记。
        """

        outcomes: list[dict] = []
        applicable = [a for a in actions if a.instrument_id
                      and trading_day in (a.ex_date, a.pay_date)]
        if not applicable:
            return outcomes

        with write_tx(self.con):
            for action in applicable:
                entitlement = record_dividend_entitlement(
                    action, lots=lots, recorded_on=action.record_date
                )
                outcome = apply_cash_dividend(
                    action, entitlement=entitlement, trading_day=trading_day
                )
                if not outcome.recognised:
                    outcomes.append({
                        "action_id": action.action_id,
                        "instrument_id": action.instrument_id,
                        "stage": outcome.stage,
                        "receivable_cents": 0,
                        "cash_delta_cents": 0,
                        "entitlement_shares": outcome.entitlement_shares,
                        "note": outcome.note,
                    })
                    continue

                # 公司行为本身也要落库：应收要引用它，事后才能回答
                # "这笔钱是哪次分红来的"。
                self.con.execute(
                    "INSERT OR IGNORE INTO corporate_action (action_id,instrument_id,"
                    "action_type,record_date,ex_date,pay_date,cash_per_share_cents,"
                    "supported,notes) VALUES (?,?,'CASH_DIVIDEND',?,?,?,?,1,?)",
                    (action.action_id, action.instrument_id,
                     action.record_date.isoformat(), action.ex_date.isoformat(),
                     action.pay_date.isoformat(), action.cash_per_share_cents,
                     f"tax treatment: {action.tax_treatment}"),
                )

                if outcome.stage == "EX_AND_PAY_DATE":
                    # 同日除息与发放：不产生未结应收，直接记现金
                    self.con.execute(
                        "INSERT INTO cash_entry (entry_id,portfolio_id,entry_type,"
                        "amount_cents,trading_day,occurred_at,related_instrument_id) "
                        "VALUES (?,?,'DIVIDEND_RECEIVABLE_SETTLED',?,?,?,?)",
                        (f"cash-div-{portfolio_id}-{action.action_id}", portfolio_id,
                         outcome.cash_delta_cents, trading_day.isoformat(), _iso(now),
                         action.instrument_id),
                    )
                elif outcome.stage == "EX_DATE":
                    self.con.execute(
                        "INSERT OR IGNORE INTO receivable (receivable_id,portfolio_id,"
                        "instrument_id,kind,amount_cents,tax_treatment,recognized_on,"
                        "expected_settlement_on,settled_on,status,corporate_action_id) "
                        "VALUES (?,?,?,'DIVIDEND',?,?,?,?,NULL,'RECOGNIZED',?)",
                        (f"rcv-{portfolio_id}-{action.action_id}", portfolio_id,
                         action.instrument_id, outcome.receivable_cents,
                         action.tax_treatment, trading_day.isoformat(),
                         outcome.expected_settlement_on.isoformat(), action.action_id),
                    )
                elif outcome.stage == "PAY_DATE":
                    settled = self.con.execute(
                        "UPDATE receivable SET status='SETTLED', settled_on=? "
                        "WHERE portfolio_id=? AND corporate_action_id=? "
                        "AND status='RECOGNIZED'",
                        (trading_day.isoformat(), portfolio_id, action.action_id),
                    )
                    if settled.rowcount:
                        self.con.execute(
                            "INSERT INTO cash_entry (entry_id,portfolio_id,entry_type,"
                            "amount_cents,trading_day,occurred_at,related_instrument_id) "
                            "VALUES (?,?,'DIVIDEND_RECEIVABLE_SETTLED',?,?,?,?)",
                            (f"cash-div-{portfolio_id}-{action.action_id}", portfolio_id,
                             outcome.cash_delta_cents, trading_day.isoformat(), _iso(now),
                             action.instrument_id),
                        )

                outcomes.append({
                    "action_id": action.action_id,
                    "instrument_id": action.instrument_id,
                    "stage": outcome.stage,
                    "receivable_cents": outcome.receivable_cents,
                    "cash_delta_cents": outcome.cash_delta_cents,
                    "entitlement_shares": outcome.entitlement_shares,
                    "note": outcome.note,
                })
        return outcomes

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

        if self.con.execute("SELECT 1 FROM portfolio WHERE portfolio_id=?",
                            (portfolio_id,)).fetchone() is None:
            self._ensure_account(portfolio_id, initial_cash_cents=cash_available_cents,
                                 initial_lots=lots, now=datetime.now(timezone.utc))
        ledger_lots = self._load_lots(portfolio_id)
        ledger_cash = self._ledger_cash(portfolio_id)
        # 应收从账本读，不从调用方拿：否则估值会变成"调用方说多少就是多少"
        ledger_receivables = self._open_receivables(portfolio_id)
        supplied_matches = (_account_version(lots, cash_available_cents)
                            == _account_version(ledger_lots, ledger_cash))

        ids = sorted({l.instrument_id for l in ledger_lots if l.quantity_remaining > 0})
        bars = self._bars(snapshot_id, trading_day, as_of, ids)

        # 停牌时使用此前最后一个有效收盘价，并显式记录其日期以计算停牌天数
        last_valid: dict[str, tuple[int, date]] = {}
        for iid in ids:
            rows = self.reader.daily_quotes(snapshot_id, as_of=as_of,
                                            instrument_id=iid, end=trading_day)
            history = [r for r in rows]
            if history:
                last_valid[iid] = (history[-1].close_cents, history[-1].trading_day)

        positions, issues = value_positions(lots=ledger_lots, bars=bars,
                                            last_valid_price=last_valid,
                                            trading_day=trading_day)
        if not supplied_matches:
            issues.append({
                "code": "STALE_SNAPSHOT", "message": "supplied account differs from ledger",
                "object_id": portfolio_id, "retryable": False,
                "repair_action": "reload cash and lots from the portfolio ledger",
            })
        ledger_invariants = self._ledger_invariants(portfolio_id)
        result = compute_valuation(
            trading_day=trading_day, cash_available_cents=ledger_cash,
            receivables_cents=ledger_receivables,
            positions=positions, lots=ledger_lots, extra_issues=issues,
            ledger_invariants=ledger_invariants,
        )

        now = datetime.now(timezone.utc)
        inv = result.invariants
        valuation_id = f"val-{portfolio_id}-{trading_day.isoformat()}"
        with write_tx(self.con):
            self.con.execute("DELETE FROM valuation_position WHERE valuation_id=?",
                             (valuation_id,))
            self.con.execute(
                "INSERT OR REPLACE INTO valuation (valuation_id,portfolio_id,trading_day,"
                "cash_available_cents,cash_frozen_cents,receivables_cents,"
                "positions_value_cents,payables_cents,net_value_cents,"
                "invariant_cash_not_overdrawn,invariant_positions_not_negative,"
                "invariant_shares_match_lots,invariant_fill_le_order,"
                "invariant_fees_booked_once,invariant_cash_lines_sum,published,"
                "violations_json,computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (valuation_id, portfolio_id,
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
            for p in result.positions:
                self.con.execute(
                    "INSERT INTO valuation_position (valuation_id,instrument_id,quantity,"
                    "price_cents,price_basis,staleness_days,value_cents) VALUES (?,?,?,?,?,?,?)",
                    (valuation_id, p.instrument_id, p.quantity, p.price_cents,
                     p.price_basis, p.staleness_days, p.value_cents),
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

        invariants = self._ledger_invariants(portfolio_id)

        valuation = self.con.execute(
            "SELECT cash_available_cents,receivables_cents,published FROM valuation "
            "WHERE portfolio_id=? ORDER BY trading_day DESC LIMIT 1", (portfolio_id,),
        ).fetchone()
        valuation_cash_matches = (valuation is None
                                  or int(valuation["cash_available_cents"]) == cash)
        if not valuation_cash_matches:
            invariants["violations"].append({"invariant": "valuation_cash_matches_ledger"})

        # 应收也要对账。只管现金和持仓会漏掉"已确认未到账"的分红：
        # 那笔钱既不在现金里，也不在持仓里，账面上看起来凭空少了。
        receivables = self._open_receivables(portfolio_id)
        valuation_receivables_matches = (
            valuation is None or int(valuation["receivables_cents"]) == receivables)
        if not valuation_receivables_matches:
            invariants["violations"].append(
                {"invariant": "valuation_receivables_matches_ledger"})

        return {
            "portfolio_id": portfolio_id,
            "cash_cents": cash,
            "positions": positions,
            "receivables_cents": receivables,
            "fill_count": int(fill_rows["n"]),
            "fees_total_cents": int(fee_rows["f"]),
            "invariants": invariants,
            "valuation_cash_matches_ledger": valuation_cash_matches,
            "valuation_receivables_matches_ledger": valuation_receivables_matches,
            "reconciled": (cash >= 0 and valuation_cash_matches
                           and valuation_receivables_matches
                           and all(invariants[k] for k in (
                               "fill_le_order", "fees_booked_once",
                               "cash_lines_sum_to_balance", "lots_match_fills"))),
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
