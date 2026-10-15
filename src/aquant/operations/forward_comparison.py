"""Pre-open M/E/B engineering observations on the existing domain ledger.

The actual creation timestamp is part of the frozen specification.  H is
unavailable until a separate user-confirmed series is explicitly registered;
neither an engineering selection nor an old replay is substituted for it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, replace
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from aquant.domain.experiments.comparison import (
    ComparisonError, VerifiedInvestigation, active_e1_events, equal_weight_targets,
)
from aquant.domain.portfolio.construction import Candidate, ConstructionParams, TargetWeight
from aquant.domain.portfolio.plan import PlanService, confirmer_is_human
from aquant.domain.strategy.s1 import build_s1_signals
from aquant.operations.research_candidates import file_hash
from aquant.operations.research_simulation import (
    assert_execution_eligibility, load_simulation_research, project_pending_draft, simulation_context,
)

MARKET_TZ = ZoneInfo("Asia/Shanghai")
SERIES_ID = re.compile(r"^fwd-[0-9a-f]{24}$")
VERSION = "aquant.forward-comparison.v1"


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=lambda item: str(item) if isinstance(item, (Decimal, date, datetime, Path))
                      else (_ for _ in ()).throw(TypeError(type(item).__name__)), allow_nan=False).encode()


def _hash(value) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _stamp(value: str | datetime) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ComparisonError("forward observation requires a timezone-aware timestamp")
    return result


def _validate_id(series_id: str) -> str:
    if not SERIES_ID.fullmatch(series_id):
        raise ComparisonError("invalid forward series id")
    return series_id


def _market_day(reader, snapshot_id: str) -> date:
    """Future published calendar sessions are not observed market prices."""
    ref = reader.ref(snapshot_id)
    quotes = reader.daily_quotes(snapshot_id, as_of=ref.as_of_time)
    if not quotes:
        raise ComparisonError("snapshot has no actual market-price date")
    return max(row.trading_day for row in quotes)


def _seal(document: dict) -> dict:
    return dict(document, contentHash=_hash(document))


def _unseal(document: dict) -> dict:
    body = {key: value for key, value in document.items() if key != "contentHash"}
    if document.get("contentHash") != _hash(body):
        raise ComparisonError("immutable forward record content hash mismatch")
    return body


def _assert_portfolio_bindings(spec: dict) -> None:
    sid = spec["seriesId"]
    if (set(spec["portfolios"]) != set("MEHB")
            or any(spec["portfolios"][kind]["portfolioId"] != f"pf-engineering-series-{sid}-{kind.lower()}" for kind in "MEB")
            or spec["portfolios"]["H"]["portfolioId"] is not None
            or spec["portfolios"]["H"]["status"] != "UNAVAILABLE"):
        raise ComparisonError("forward account bindings differ from the isolated immutable series")


class ForwardComparisonStore:
    """One frozen spec and append-only daily observations per actual series."""
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _write_once(self, name: str, document: dict) -> dict:
        target = self.root / name
        encoded = _canonical(_seal(document))
        temporary = self.root / ("." + name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_bytes(encoded)
        try:
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.read_bytes() != encoded:
                    raise ComparisonError("immutable forward record already exists with different content")
        finally:
            temporary.unlink(missing_ok=True)
        return _unseal(json.loads(target.read_text(encoding="utf-8")))

    def write_spec(self, document: dict) -> dict:
        sid = _validate_id(document["seriesId"])
        core = {key: value for key, value in document.items() if key not in {"seriesId", "portfolios"}}
        if sid != "fwd-" + hashlib.sha256(_canonical(core)).hexdigest()[:24]:
            raise ComparisonError("forward id differs from the frozen actual specification")
        _assert_portfolio_bindings(document)
        return self._write_once(sid + ".spec.json", document)

    def spec(self, series_id: str, *, subject: str | None = None) -> dict:
        sid = _validate_id(series_id)
        document = _unseal(json.loads((self.root / (sid + ".spec.json")).read_text(encoding="utf-8")))
        core = {key: value for key, value in document.items() if key not in {"seriesId", "portfolios"}}
        if document["seriesId"] != sid or sid != "fwd-" + hashlib.sha256(_canonical(core)).hexdigest()[:24]:
            raise ComparisonError("forward series identity does not match frozen content")
        _assert_portfolio_bindings(document)
        if subject is not None and document["authorizationSubject"] != subject:
            raise ComparisonError("forward series is owned by a different authorization subject")
        return document

    def observations(self, series_id: str) -> list[dict]:
        sid = _validate_id(series_id)
        rows = [_unseal(json.loads(path.read_text(encoding="utf-8")))
                for path in sorted(self.root.glob(sid + ".*.observation.json"))]
        if any(row["seriesId"] != sid for row in rows):
            raise ComparisonError("observation belongs to a different forward series")
        return rows

    def observe(self, document: dict) -> dict:
        sid = _validate_id(document["seriesId"])
        day = date.fromisoformat(document["tradingDay"]).isoformat()
        return self._write_once(f"{sid}.{day}.observation.json", document)

    def list_series(self, *, subject: str | None = None) -> list[dict]:
        results = []
        for path in sorted(self.root.glob("fwd-*.spec.json"), reverse=True):
            spec = self.spec(path.name.removesuffix(".spec.json"))
            if subject is not None and spec["authorizationSubject"] != subject:
                continue
            observations = self.observations(spec["seriesId"])
            from aquant.operations.simulation_attribution import AttributionStore
            attribution_store = AttributionStore(self.root.parent / "simulation-attribution")
            display_observations = [dict(row, attribution=attribution_store.read(spec["seriesId"], row["tradingDay"])) for row in observations]
            results.append(dict(spec, observations=display_observations,
                observedSessionCount=len(observations),
                status="OBSERVING" if observations else "WAITING_EXECUTION_SNAPSHOT"))
        return sorted(results, key=lambda row: row["createdAt"], reverse=True)


def prepare_forward_comparison(service: PlanService, *, store: ForwardComparisonStore,
                               research_path: Path, snapshot_id: str,
                               authorization_subject: str, now: datetime | None = None,
                               experiment_version: str = "S1-E1-MEB-forward-v1",
                               verified_events: tuple[VerifiedInvestigation, ...] = (),
                               observation_target_sessions: int = 5) -> dict:
    """Freeze actual future input/targets. No account, fill or future return is created."""
    if not confirmer_is_human(authorization_subject):
        raise ComparisonError("forward preparation requires the existing human authorization")
    now = _stamp(now or datetime.now(timezone.utc))
    if observation_target_sessions < 1 or not experiment_version:
        raise ComparisonError("forward observation needs an explicit version and positive session target")
    packet, binding = load_simulation_research(Path(research_path))
    context, eligible, indexed = simulation_context(packet, binding, reader=service.reader,
        snapshot_id=snapshot_id, params=service.params, limit=5000)
    if context["blockingIssues"] or not eligible:
        raise ComparisonError("current verified research/simulation inputs are not ready for a forward series")
    ref = indexed.ref(snapshot_id)
    if (ref.as_of_time > now or ref.published_at is None or ref.published_at > now
            or ref.data_mode != "PRODUCTION"):
        raise ComparisonError("forward inputs must actually have been published before registration")
    calendar = sorted({date.fromisoformat(day) for day in indexed.trading_calendar(snapshot_id, as_of=ref.as_of_time)})
    first_day = next((day for day in calendar
                      if datetime.combine(day, time(9, 30), MARKET_TZ) > max(now, ref.as_of_time)), None)
    if first_day is None:
        raise ComparisonError("verified calendar does not contain a future execution session")
    histories = {candidate.instrument_id: [] for candidate in eligible}
    for quote in indexed.daily_quotes(snapshot_id, as_of=ref.as_of_time,
                                     end=date.fromisoformat(binding["researchDataAsOf"])):
        if quote.instrument_id in histories:
            histories[quote.instrument_id].append(quote.adjusted_close_cents)
    complete = {iid: prices for iid, prices in histories.items()
                if len(prices) >= 61 and all(value is not None and value > 0 for value in prices)}
    excluded_history = sorted(set(histories) - set(complete))
    if not complete:
        raise ComparisonError("no common pool member has 61 verified adjusted closes for S1")
    industries = {candidate.instrument_id: candidate.industry_code for candidate in eligible}
    signals = build_s1_signals(adjusted_closes_by_instrument=complete, industry_by_instrument=industries)
    candidates = [signal.as_candidate() for signal in signals]
    projection = project_pending_draft(service, portfolio_id="forward-spec-projection",
        snapshot_id=snapshot_id, trading_day=first_day, candidates=candidates,
        cash_cents=service.params.initial_capital_cents, lots=[])
    active = active_e1_events(events=verified_events, calendar=calendar,
        decision_cutoff=ref.as_of_time, execution_day=first_day, window=10)
    blocked = {event.instrument_id for event in active}
    baseline_targets, baseline_budget = equal_weight_targets(candidates,
        equity_pct=service.params.max_total_equity_pct, params=service.params,
        label="B frozen future common-pool equal weight")
    originals = store.root / "research-origins"
    originals.mkdir(exist_ok=True)
    origin = originals / (binding["researchContentHash"].removeprefix("sha256:") + ".json")
    body = Path(research_path).read_bytes()
    if "sha256:" + hashlib.sha256(body).hexdigest() != binding["researchContentHash"]:
        raise ComparisonError("research changed while registering the forward series")
    if not origin.exists():
        try:
            with origin.open("xb") as stream:
                stream.write(body)
        except FileExistsError:
            pass
    if file_hash(origin) != binding["researchContentHash"]:
        raise ComparisonError("forward original research content hash mismatch")
    params = json.loads(_canonical(asdict(service.params)))
    first_fee = json.loads(_canonical(asdict(service.fee_table.schedule_for(first_day))))
    core = {
        "schema": VERSION, "experimentVersion": experiment_version,
        "runKind": "PREOPEN_ENGINEERING_FORWARD", "formalStrategy": False,
        "humanConfirmed": False, "authorizationSubject": authorization_subject,
        "createdAt": now.isoformat(), "firstTradingDay": first_day.isoformat(),
        "decisionSnapshotId": snapshot_id, "decisionCutoffAt": ref.as_of_time.isoformat(),
        "decisionPublishedAt": ref.published_at.isoformat(), **binding,
        "researchOriginPath": str(origin.resolve()),
        "commonPool": sorted(complete), "commonPoolCount": len(complete),
        "s1ExcludedHistory": excluded_history, "s1Signals": [asdict(signal) for signal in signals],
        "initialCashCents": service.params.initial_capital_cents,
        "constructionParams": params,
        "boardRules": json.loads(_canonical([asdict(rule) for rule in service.board_rules])),
        "fees": {"commissionSource": service.fee_table.commission_source, "firstDaySchedule": first_fee},
        "executionModel": "existing PlanService / DailySimulator; actual archived EOD; integer lots and volume cap",
        "rebalancingPolicy": "INITIAL_ALLOCATION_THEN_HOLD; daily EOD marks; no automatic user orders",
        "modelProjection": projection,
        "modelTargets": projection["targets"],
        "eventTargets": [target for target in projection["targets"] if target["instrument_id"] not in blocked],
        "verifiedEvents": [asdict(event) for event in verified_events],
        "eventBuyBlockedInstrumentIds": sorted(blocked),
        "eventInterpretation": "仅核验且截止前可得的 E1 阻断新增买入；无触发时 E=M。",
        "benchmarkTargets": [asdict(target) for target in baseline_targets],
        "benchmarkBudget": baseline_budget,
        "broadMarketReference": {"status": "UNAVAILABLE", "reason": "尚无冻结广义市场指数及价格/全收益口径"},
        "human": {"status": "UNAVAILABLE", "reason": "尚无本次相同信息时点及约束下的用户明确确认，不能以工程选择替代 H"},
        "observationTargetSessions": observation_target_sessions,
        "hypothesis": "验证原时点 S1/E1/同池等权的前向执行与每日账本完整性；不以盈利判定通过",
        "primaryMetric": "ledger reconciliation and actual EOD completeness",
        "failureConditions": ["immutable source hash mismatch", "missing execution-day eligibility",
            "ledger or valuation invariants fail", "unsupported corporate action"],
        "limitations": ["本次登记时刻真实保留，不追认为 10 月 12 日预登记。",
            "M/E/B 属获授权工程观察，H 尚不可用；工程选择账户另列。",
            "证券池限当前真实完整证据池，不代表全部研究代码均可模拟。",
            "公司行为仅处理已核验支持记录，复杂行为和覆盖缺口不得假装完成。",
            "五交易日是运行观察目标，不能据此判定盈利能力、正式 S2 或策略升级。"],
    }
    # Convert Decimal/dataclass leaf values before hashing or persisting.
    core = json.loads(_canonical(core))
    sid = "fwd-" + hashlib.sha256(_canonical(core)).hexdigest()[:24]
    portfolios = {kind: {"portfolioId": f"pf-engineering-series-{sid}-{kind.lower()}", "status": "PENDING"}
                  for kind in "MEB"}
    portfolios["H"] = {"portfolioId": None, **core["human"]}
    return store.write_spec(dict(core, seriesId=sid, portfolios=portfolios))


def _service_for_spec(service: PlanService, spec: dict, kind: str, *, delayed: bool = False) -> PlanService:
    frozen = dict(spec["constructionParams"])
    for key in ("max_total_equity_pct", "max_single_name_pct", "max_single_industry_pct", "initial_target_pct"):
        if frozen.get(key) is not None:
            frozen[key] = Decimal(frozen[key])
    params = ConstructionParams(**frozen)
    if kind == "B":
        params = replace(params, max_holdings=len(spec["commonPool"]))
    ref = service.reader.ref(spec["decisionSnapshotId"])
    listings = {row["instrument_id"]: (row["exchange"], row["board"])
                for row in service.reader.instruments(spec["decisionSnapshotId"], as_of=ref.as_of_time)}
    first_day = date.fromisoformat(spec["firstTradingDay"])
    schedule = json.loads(_canonical(asdict(service.fee_table.schedule_for(first_day))))
    if (schedule != spec["fees"]["firstDaySchedule"]
            or service.fee_table.commission_source != spec["fees"]["commissionSource"]):
        raise ComparisonError("fee configuration differs from frozen forward specification")
    if json.loads(_canonical([asdict(rule) for rule in service.board_rules])) != spec["boardRules"]:
        raise ComparisonError("board rules differ from frozen forward specification")
    # All isolated engineering executions use the frozen decision ADV window.
    # The public PlanService path stays unchanged; the displayed delayed flag
    # still describes the actual provider cutoff rather than this internal mode.
    return PlanService(service.con, service.reader, service.fee_table, service.board_rules, listings, params,
                       delayed_engineering_calculation=True)


def execute_forward_initial(service: PlanService, *, store: ForwardComparisonStore,
                            series_id: str, execution_snapshot_id: str,
                            authorization_subject: str, now: datetime | None = None) -> dict:
    """Execute only this actually pre-open spec, never a reconstructed past choice."""
    if not confirmer_is_human(authorization_subject):
        raise ComparisonError("forward execution requires the saved human authorization")
    now = _stamp(now or datetime.now(timezone.utc))
    spec = store.spec(series_id, subject=authorization_subject)
    day = date.fromisoformat(spec["firstTradingDay"])
    execution_ref = service.reader.ref(execution_snapshot_id)
    if _market_day(service.reader, execution_snapshot_id) != day:
        raise ComparisonError("initial execution must use the actual registered day's prices")
    if _stamp(spec["createdAt"]) >= datetime.combine(day, time(9, 30), MARKET_TZ):
        raise ComparisonError("the series was not actually registered before execution open")
    if execution_ref.as_of_time > now or execution_ref.published_at is None or execution_ref.published_at > now:
        raise ComparisonError("execution snapshot has not actually arrived")
    if file_hash(Path(spec["researchOriginPath"])) != spec["researchContentHash"]:
        raise ComparisonError("frozen forward research origin hash mismatch")
    prior = next((row for row in store.observations(series_id) if row["tradingDay"] == day.isoformat()), None)
    if prior:
        if prior["snapshotId"] != execution_snapshot_id:
            raise ComparisonError("initial execution already bound a different immutable snapshot")
        return record_forward_observation(service, store=store, series_id=series_id,
                                          snapshot_id=execution_snapshot_id, now=now)
    assert_execution_eligibility(service.reader, decision_snapshot_id=spec["decisionSnapshotId"],
        execution_snapshot_id=execution_snapshot_id, trading_day=day, instrument_ids=spec["commonPool"])
    # The existing domain service checks action support and owns entitlement accounting.
    from aquant.domain.experiments.comparison import ComparisonRunner
    actions = ComparisonRunner._cash_dividends(service.reader.corporate_actions(
        execution_snapshot_id, as_of=execution_ref.as_of_time))
    from aquant.operations.engineering_simulation import persist_engineering_plan
    candidates = [Candidate(row["instrument_id"], row["industry_code"], row["signal_rank"])
                  for row in spec["s1Signals"]]
    plans = {}
    for kind in "MEB":
        delayed = execution_ref.as_of_time.astimezone(MARKET_TZ).date() != day
        svc = _service_for_spec(service, spec, kind, delayed=delayed)
        pid = spec["portfolios"][kind]["portfolioId"]
        # A retry after a partially completed batch reuses the actually executed plan.
        existing = svc.con.execute("SELECT p.plan_id,p.status,p.confirmed_by,p.confirmation_token_hash,"
            "b.execution_snapshot_id,e.source,e.origin_ref,e.original_registered_at FROM simulation_plan p "
            "JOIN plan_snapshot_binding b ON b.plan_id=p.plan_id LEFT JOIN engineering_plan_provenance e ON e.plan_id=p.plan_id "
            "WHERE p.portfolio_id=? ORDER BY p.frozen_at LIMIT 1", (pid,)).fetchone()
        if existing:
            if (existing["execution_snapshot_id"] != execution_snapshot_id
                    or existing["source"] != "FORWARD_ENGINEERING_SIMULATION"
                    or existing["origin_ref"] != series_id + ":" + kind
                    or existing["original_registered_at"] != spec["createdAt"]
                    or existing["confirmed_by"] is not None or existing["confirmation_token_hash"] is not None):
                raise ComparisonError("partial forward account differs from the frozen engineering provenance")
            plan_id = existing["plan_id"]
            plan_status = existing["status"]
        else:
            key = {"M": "modelTargets", "E": "eventTargets", "B": "benchmarkTargets"}[kind]
            targets = [TargetWeight(row["instrument_id"], row["industry_code"], Decimal(row["weight_pct"]), row["rationale"])
                       for row in spec[key]]
            preview = svc.preview(portfolio_id=pid, snapshot_id=spec["decisionSnapshotId"],
                decision_snapshot_id=spec["decisionSnapshotId"], decision_cutoff_at=_stamp(spec["decisionCutoffAt"]),
                execution_snapshot_id=execution_snapshot_id, trading_day=day, as_of=_stamp(spec["decisionCutoffAt"]),
                candidates=candidates, lots=[], cash_available_cents=spec["initialCashCents"],
                confirm_subject=authorization_subject, target_weights=targets)
            frozen = persist_engineering_plan(svc, preview=preview,
                source="FORWARD_ENGINEERING_SIMULATION", origin_ref=series_id + ":" + kind,
                original_registered_at=_stamp(spec["createdAt"]), initial_cash_cents=spec["initialCashCents"], now=now)
            plan_id = frozen["plan_id"]
            plan_status = frozen["status"]
        if plan_status == "FROZEN":
            svc.execute(plan_id=plan_id, subject=None, now=now, corporate_actions=actions)
        elif plan_status == "EXECUTED":
            # execute commits fills before advancing cash-dividend accounting.
            # A process interruption between those stages must never replay buys.
            svc._advance_corporate_actions(portfolio_id=pid, trading_day=day,
                actions=actions, lots=svc._load_lots(pid), now=now)
        else:
            raise ComparisonError("forward initial plan is neither executable nor already executed: " + plan_status)
        plans[kind] = plan_id
    return record_forward_observation(service, store=store, series_id=series_id,
        snapshot_id=execution_snapshot_id, now=now, initial_plan_ids=plans)


def _day_execution(con, *, portfolio_id: str, trading_day: str, observed_at: datetime) -> dict:
    """Freeze booked day-specific execution evidence with the new EOD record."""
    orders = [dict(row) for row in con.execute(
        'SELECT order_id,plan_id,snapshot_id,instrument_id,side,quantity,status,reject_reason,'
        'trading_day,created_at FROM "order" WHERE portfolio_id=? AND trading_day=? '
        'ORDER BY process_sequence,order_id', (portfolio_id, trading_day))
        if _stamp(row["created_at"]) <= observed_at]
    fills = [dict(row) for row in con.execute(
        'SELECT fill_id,order_id,instrument_id,side,quantity,price_cents,gross_amount_cents,'
        'fees_total_cents,trading_day,filled_at FROM fill '
        'WHERE portfolio_id=? AND trading_day=? AND is_reversed=0 '
        'ORDER BY filled_at,fill_id', (portfolio_id, trading_day))
        if _stamp(row["filled_at"]) <= observed_at]
    order_ids = {row["order_id"] for row in orders}
    if any(row["order_id"] not in order_ids for row in fills):
        raise ComparisonError("day-specific fills lack their actual dated orders")
    for fill in fills:
        fill["feeCharges"] = [dict(row) for row in con.execute(
            'SELECT fee_code,amount_cents,fee_version FROM fee_charge '
            'WHERE fill_id=? ORDER BY fee_code,fee_version', (fill["fill_id"],))]
        if sum(charge["amount_cents"] for charge in fill["feeCharges"]) != fill["fees_total_cents"]:
            raise ComparisonError("day-specific fee charges do not reconcile to the booked fill")
    return {"tradingDay": trading_day, "recordedAt": observed_at.isoformat(),
            "orders": orders, "fills": fills, "orderCount": len(orders), "fillCount": len(fills),
            "unfilledOrderCount": sum(row["status"] != "FILLED" for row in orders),
            "feesTotalCents": sum(row["fees_total_cents"] for row in fills),
            "grossTurnoverCents": sum(row["gross_amount_cents"] for row in fills)}


def record_forward_observation(service: PlanService, *, store: ForwardComparisonStore,
                               series_id: str, snapshot_id: str, now: datetime | None = None,
                               initial_plan_ids: dict[str, str] | None = None) -> dict:
    """Actual EOD value/reconciliation only. It never places daily rebalance orders."""
    now = _stamp(now or datetime.now(timezone.utc))
    spec = store.spec(series_id)
    ref = service.reader.ref(snapshot_id)
    day = _market_day(service.reader, snapshot_id)
    if (day < date.fromisoformat(spec["firstTradingDay"]) or ref.as_of_time > now
            or ref.published_at is None or ref.published_at > now or ref.data_mode != "PRODUCTION"
            or ref.as_of_time < datetime.combine(day, time(15), MARKET_TZ)):
        raise ComparisonError("forward observation must use an arrived production EOD at or after initial execution")
    prior = store.observations(series_id)
    same = next((row for row in prior if row["tradingDay"] == day.isoformat()), None)
    if same:
        if same["snapshotId"] != snapshot_id:
            raise ComparisonError("an immutable observation already bound a different snapshot for this day")
        from aquant.operations.simulation_attribution import AttributionStore, save_forward_attribution
        save_forward_attribution(service, store=AttributionStore(store.root.parent / "simulation-attribution"), spec=spec, observation=same)
        return same
    if prior and day <= date.fromisoformat(prior[-1]["tradingDay"]):
        raise ComparisonError("daily forward observations must progress chronologically")
    if not prior and day.isoformat() != spec["firstTradingDay"]:
        raise ComparisonError("initial execution-day EOD must be recorded before a later observation")
    instruments = {row["instrument_id"]: row for row in service.reader.instruments(snapshot_id, as_of=ref.as_of_time)}
    # Never manufacture missing days; list them against the actual published calendar.
    calendar = {date.fromisoformat(value) for value in service.reader.trading_calendar(snapshot_id, as_of=ref.as_of_time)}
    seen = {date.fromisoformat(row["tradingDay"]) for row in prior} | {day}
    missing_days = sorted(value.isoformat() for value in calendar
                          if date.fromisoformat(spec["firstTradingDay"]) <= value <= day and value not in seen)
    portfolios = {"H": dict(spec["human"], portfolioId=None)}
    for kind in "MEB":
        svc = _service_for_spec(service, spec, kind, delayed=ref.as_of_time.astimezone(MARKET_TZ).date() != day)
        pid = spec["portfolios"][kind]["portfolioId"]
        if not svc.con.execute("SELECT 1 FROM portfolio WHERE portfolio_id=?", (pid,)).fetchone():
            raise ComparisonError("forward account has no actual initial execution; no cash-only return is fabricated")
        lots = svc._load_lots(pid)
        held = {lot.instrument_id for lot in lots if lot.quantity_remaining > 0}
        if not held <= set(instruments):
            raise ComparisonError("actual EOD snapshot is missing an active forward holding")
        raw_actions = service.reader.corporate_actions(snapshot_id, as_of=ref.as_of_time)
        affected = [action for action in raw_actions if action.get("instrument_id") in held]
        if affected and initial_plan_ids is None:
            from aquant.domain.experiments.comparison import ComparisonRunner
            actions = ComparisonRunner._cash_dividends(affected)
            previous_day = date.fromisoformat(prior[-1]["tradingDay"])
            if any(previous_day < boundary < day for action in actions
                   for boundary in (action.record_date, action.ex_date, action.pay_date)):
                raise ComparisonError("missing an actual observation across a known corporate-action boundary; restore that day's EOD first")
            svc._advance_corporate_actions(portfolio_id=pid, trading_day=day, actions=actions, lots=lots, now=now)
        valuation = svc.value(portfolio_id=pid, snapshot_id=snapshot_id, trading_day=day,
            as_of=ref.as_of_time, execution_plan_id=(initial_plan_ids or {}).get(kind),
            lots=lots, cash_available_cents=svc._ledger_cash(pid))
        reconciliation = svc.reconcile(portfolio_id=pid)
        if not valuation["published"] or not reconciliation["reconciled"]:
            raise ComparisonError("forward daily ledger or valuation invariant failed")
        row = svc.con.execute("SELECT * FROM valuation WHERE portfolio_id=? AND trading_day=?", (pid, day.isoformat())).fetchone()
        positions = [dict(position) for position in svc.con.execute("SELECT * FROM valuation_position WHERE valuation_id=? ORDER BY instrument_id", (row["valuation_id"],))]
        total = int(row["net_value_cents"])
        equity = int(row["positions_value_cents"])
        industries = {}
        for position in positions:
            industry = instruments[position["instrument_id"]].get("industry_code") or "UNKNOWN"
            industries[industry] = industries.get(industry, 0) + int(position["value_cents"])
        percentage = lambda value: str(Decimal(value) / total * 100) if total else None
        portfolios[kind] = {"portfolioId": pid, "status": "OBSERVED", "valuationId": row["valuation_id"],
            "netValueCents": total, "cashCents": int(row["cash_available_cents"]),
            "positionsValueCents": equity, "cashPct": percentage(int(row["cash_available_cents"])),
            "equityPct": percentage(equity),
            "netReturnPct": str(Decimal(total - spec["initialCashCents"]) / spec["initialCashCents"] * 100),
            "industryExposurePct": {key: percentage(value) for key, value in sorted(industries.items())},
            "feesTotalCents": reconciliation["fees_total_cents"], "fillCount": reconciliation["fill_count"],
            "positions": positions, "reconciled": True, "valuation": valuation,
            "dayExecution": _day_execution(svc.con, portfolio_id=pid,
                                           trading_day=day.isoformat(), observed_at=now),
            "executionPlanId": (initial_plan_ids or {}).get(kind)}
    record = {"schema": VERSION + ".observation", "seriesId": series_id,
        "tradingDay": day.isoformat(), "snapshotId": snapshot_id, "cutoffAt": ref.as_of_time.isoformat(),
        "observedAt": now.isoformat(), "portfolios": portfolios, "missingEodDays": missing_days,
        "delayedEngineeringCalculation": ref.as_of_time.astimezone(MARKET_TZ).date() != day,
        "formalStrategy": False, "humanConfirmed": False,
        "observationTargetReached": len(prior) + 1 >= spec["observationTargetSessions"],
        "interpretation": "来自实际已保存账本和当日真实快照；H不可用；观察天数不是盈利或上线门槛"}
    saved = store.observe(record)
    from aquant.operations.simulation_attribution import AttributionStore, save_forward_attribution
    save_forward_attribution(service, store=AttributionStore(store.root.parent / "simulation-attribution"), spec=spec, observation=saved)
    return saved


def track_active_forward(service: PlanService, *, store: ForwardComparisonStore,
                         snapshot_id: str, now: datetime | None = None) -> dict:
    """Execute the frozen initial basket once, then mark actual daily holdings."""
    now = _stamp(now or datetime.now(timezone.utc))
    results = []
    current_day = _market_day(service.reader, snapshot_id).isoformat()
    for spec in store.list_series():
        try:
            # A later market update may be the next retry after an EOD was
            # saved but its independent review failed. Restore every missing
            # earlier review from its own snapshot and dated ledger first.
            from aquant.operations.simulation_attribution import AttributionStore, save_forward_attribution
            attribution_store = AttributionStore(store.root.parent / "simulation-attribution")
            for prior_observation in store.observations(spec["seriesId"]):
                if attribution_store.read(spec["seriesId"], prior_observation["tradingDay"]) is None:
                    save_forward_attribution(service, store=attribution_store,
                        spec=spec, observation=prior_observation)
            if not spec["observations"]:
                if not current_day or current_day < spec["firstTradingDay"]:
                    results.append({"seriesId": spec["seriesId"], "status": "WAITING_EXECUTION_SNAPSHOT",
                                    "firstTradingDay": spec["firstTradingDay"]})
                    continue
                initial_snapshot = snapshot_id
                if current_day != spec["firstTradingDay"]:
                    available = [item for item in service.reader.published_snapshot_catalog()
                                 if _market_day(service.reader, item.snapshot_id).isoformat() == spec["firstTradingDay"]
                                 and _stamp(item.as_of_time) <= now]
                    if not available:
                        raise ComparisonError("the actual initial execution-day snapshot is still missing; later prices cannot replace it")
                    initial_snapshot = max(available, key=lambda item: item.published_at or "").snapshot_id
                initial = execute_forward_initial(service, store=store, series_id=spec["seriesId"],
                    execution_snapshot_id=initial_snapshot,
                    authorization_subject=spec["authorizationSubject"], now=now)
                if current_day == initial["tradingDay"]:
                    results.append({"seriesId": spec["seriesId"], "status": "OBSERVED", "tradingDay": initial["tradingDay"]})
                    continue
            observation = record_forward_observation(service, store=store, series_id=spec["seriesId"],
                snapshot_id=snapshot_id, now=now)
            results.append({"seriesId": spec["seriesId"], "status": "OBSERVED", "tradingDay": observation["tradingDay"]})
        except Exception as exc:
            results.append({"seriesId": spec["seriesId"], "status": "DATA_NOT_READY", "reason": str(exc)})
    return {"schema": VERSION + ".daily", "results": results}
