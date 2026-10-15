"""Bind current research to reviewable manual simulation without backdating it.

Pending drafts contain projections made with decision-time closes.  They are
deliberately not PlanPreview objects: the confirmation service can never turn a
missing future execution snapshot into a frozen or filled order.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from aquant.artifact_paths import resolve_artifact_path
from aquant.domain.data.db import write_tx
from aquant.domain.portfolio.construction import (
    Candidate, construct_targets, listed_trading_days, weights_to_orders,
)
from aquant.domain.portfolio.plan import PlanError, PlanService
from aquant.operations.research_candidates import ResearchCandidateError, file_hash
from aquant.operations.research_scan import load_research_scan

LABEL = "研究候选人工模拟"
MARKET_TZ = ZoneInfo("Asia/Shanghai")
HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


def _stamp(value: object, name: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("timezone missing")
        return stamp
    except (TypeError, ValueError) as exc:
        raise ResearchCandidateError(f"{name} needs a timezone-aware timestamp") from exc


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _issue(code: str, message: str, repair: str) -> dict:
    return {"code": code, "message": message, "repairAction": repair}


def _verified_status(inst: dict, day: date, cutoff: datetime) -> bool:
    status = inst.get("simulation_eligibility") or {}
    if not isinstance(status, dict):
        return False
    valid = (status.get("status") == "VERIFIED" and status.get("as_of_date") == day.isoformat()
             and status.get("is_st") is False and status.get("is_suspended") is False)
    try:
        valid = valid and all(_stamp(status[k], k) <= cutoff for k in ("available_at", "observed_at"))
    except (KeyError, ResearchCandidateError):
        valid = False
    sources = status.get("sources") or []
    return bool(valid and sources and all(
        isinstance(s, dict) and s.get("source_id") and s.get("receipt_id")
        and HASH.fullmatch(str(s.get("content_hash", ""))) for s in sources))


def assert_execution_eligibility(reader, *, decision_snapshot_id: str,
                                execution_snapshot_id: str, trading_day: date,
                                instrument_ids: list[str]) -> None:
    """A past normal state cannot authorize a different execution-day state."""
    decision_ref = reader.ref(decision_snapshot_id)
    execution_ref = reader.ref(execution_snapshot_id)
    before = {i["instrument_id"]: i for i in reader.instruments(decision_snapshot_id, as_of=decision_ref.as_of_time)}
    after = {i["instrument_id"]: i for i in reader.instruments(execution_snapshot_id, as_of=execution_ref.as_of_time)}
    for iid in instrument_ids:
        inst = after.get(iid)
        old = before.get(iid) or {}
        if (inst is None or not _verified_status(inst, trading_day, execution_ref.as_of_time)
                or (old.get("exchange"), old.get("board")) != (inst.get("exchange"), inst.get("board"))):
            raise PlanError("DATA_NOT_READY", "execution-day security status or board identity is unverified", iid,
                            "publish the actual execution-day normal/ST/suspension status; prior-day status cannot replace it")


def load_simulation_research(path: Path) -> tuple[dict, dict]:
    """Verify existing research explanations, then bind the exact full artifact.

    The ordinary endpoint paginates its output, so recover the whole artifact
    only after its existing source/hash/formula verifier has accepted it.  A
    second file hash rejects replacement during the verification/read interval.
    """
    path = resolve_artifact_path(path)
    digest = file_hash(path)
    load_research_scan(path, view="all", limit=1)
    packet = json.loads(path.read_text(encoding="utf-8"))
    if file_hash(path) != digest:
        raise ResearchCandidateError("research artifact changed during verification")
    observed = [_stamp(packet["capturedTo"], "capturedTo")]
    available = [_stamp(packet["generatedAt"], "generatedAt"),
                 _stamp(packet["asOfTime"], "asOfTime")]
    for card in packet["candidates"]:
        for item in [card.get("quote") or {}, card.get("industryClassification") or {},
                     *card.get("evidence", [])]:
            if item.get("observedAt"):
                observed.append(_stamp(item["observedAt"], "observedAt"))
            if item.get("availableAt"):
                available.append(_stamp(item["availableAt"], "availableAt"))
    available.extend(observed)
    binding = {"researchSnapshotId": packet["snapshotId"],
               "researchContentHash": digest,
               "researchObservedAt": max(observed).isoformat(),
               "researchAvailableAt": max(available).isoformat(),
               "researchDataAsOf": packet["dataAsOf"],
               "researchOriginPath": str(path.resolve())}
    return packet, binding


class IndexedDecisionReader:
    """Index one immutable quote dataset instead of scanning it for every card."""
    def __init__(self, reader):
        self.reader = reader
        self.quotes: dict[str, dict[str, list]] = {}

    def __getattr__(self, name):
        return getattr(self.reader, name)

    def daily_quotes(self, snapshot_id, *, as_of, instrument_id=None, start=None, end=None):
        # Preserve the underlying reader's exact PIT cutoff even on cached data.
        if as_of != self.reader.ref(snapshot_id).as_of_time:
            raise PlanError("PIT_UNVERIFIED", "quote cutoff differs from snapshot cutoff",
                            snapshot_id, "use the immutable snapshot cutoff")
        if snapshot_id not in self.quotes:
            indexed = defaultdict(list)
            for row in self.reader.daily_quotes(snapshot_id, as_of=as_of):
                indexed[row.instrument_id].append(row)
            self.quotes[snapshot_id] = dict(indexed)
        if isinstance(start, str):
            start = date.fromisoformat(start)
        if isinstance(end, str):
            end = date.fromisoformat(end)
        groups = self.quotes[snapshot_id]
        rows = groups.get(instrument_id, []) if instrument_id else [r for group in groups.values() for r in group]
        return [r for r in rows if (start is None or r.trading_day >= start)
                and (end is None or r.trading_day <= end)]


def simulation_context(packet: dict, binding: dict, *, reader, snapshot_id: str,
                       params, limit: int = 100) -> tuple[dict, list[Candidate], IndexedDecisionReader]:
    """Current source and instrument eligibility; no synthetic or old-pool fallback."""
    indexed = reader if isinstance(reader, IndexedDecisionReader) else IndexedDecisionReader(reader)
    ref = indexed.ref(snapshot_id)
    available = _stamp(binding["researchAvailableAt"], "researchAvailableAt")
    priceday = date.fromisoformat(binding["researchDataAsOf"])
    issues = []
    if ref.data_mode != "PRODUCTION":
        issues.append(_issue("REAL_SNAPSHOT_REQUIRED", "研究候选必须绑定已验证的真实正式快照。", "发布当前真实决策快照。"))
    if ref.as_of_time < available:
        issues.append(_issue("RESEARCH_NOT_AVAILABLE", "决策快照截止早于研究实际生成或观察时点，不能倒填研究排名。", "发布截止覆盖本次研究实际可用时点的新决策快照。"))
    calendar = sorted({date.fromisoformat(d) for d in indexed.trading_calendar(snapshot_id, as_of=ref.as_of_time)})
    next_day = next((d for d in calendar if d > priceday
                     and datetime.combine(d, time(9, 30), MARKET_TZ) > ref.as_of_time), None)
    if next_day is None:
        issues.append(_issue("CALENDAR_NOT_COVERED", "正式交易日历尚未覆盖下一执行交易日。", "补齐已发布的交易日历；不按星期猜测。"))
    instruments = {i["instrument_id"]: i for i in indexed.instruments(snapshot_id, as_of=ref.as_of_time)}
    selected = []
    rows = []
    for card in packet["candidates"]:
        if card.get("status") != "RANKED":
            continue
        iid = card["instrumentId"]
        inst = instruments.get(iid)
        reasons = []
        if inst is None:
            reasons.append(_issue("INSTRUMENT_NOT_IN_DECISION_SNAPSHOT", "证券不在这份正式决策快照中。", "用真实证券身份与行情发布扩展快照。"))
        else:
            if (inst.get("exchange"), inst.get("board")) != (card.get("exchange"), card.get("board")):
                reasons.append(_issue("IDENTITY_MISMATCH", "研究与正式快照的交易所或板块身份不一致。", "核对精确证券身份，不能替换成旧代码。"))
            if inst.get("board") != "MAIN" or inst.get("exchange") not in {"SSE", "SZSE"}:
                reasons.append(_issue("BOARD_NOT_SIMULATABLE_IN_PHASE_1", "首期模拟限沪深主板。", "当前标的可研究，尚不进入首期可执行池。"))
            if not inst.get("industry_code"):
                reasons.append(_issue("INDUSTRY_UNVERIFIED", "正式快照没有已绑定的行业分类。", "补齐正式行业证据。"))
            if not _verified_status(inst, priceday, ref.as_of_time):
                reasons.append(_issue("SECURITY_STATUS_UNVERIFIED", "当前非ST、非停牌状态未由可用的归档来源完整核验。", "补齐状态观察、来源回执及哈希；未知资格不能放行。"))
            if "ST" in str((card.get("quote") or {}).get("providerName", "")).upper():
                reasons.append(_issue("RISK_STATUS_CONFLICT", "研究报价名称含ST风险线索。", "先核对风险状态，不用正常主板规则估算成交。"))
            raw_listed = inst.get("listed_on")
            try:
                listed = date.fromisoformat(str(raw_listed))
                age = listed_trading_days(listed_on=listed, trading_day=priceday, calendar=calendar)
            except (TypeError, ValueError):
                age = 0
            if age < params.exclude_listing_days:
                reasons.append(_issue("LISTING_AGE_UNVERIFIED", "上市交易日长度未证明达到当前模拟门槛。", "补齐上市日期与足够交易日历；不按自然日替代。"))
            history = indexed.daily_quotes(snapshot_id, as_of=ref.as_of_time, instrument_id=iid, end=priceday)
            if not history or history[-1].trading_day != priceday:
                reasons.append(_issue("CURRENT_REFERENCE_PRICE_MISSING", "正式参考日线没有覆盖本次研究日期。", "补齐当前真实日线，不借用旧快照价格。"))
            window = history[-params.liquidity_lookback_days:]
            known_window = [d for d in calendar if d <= priceday][-params.liquidity_lookback_days:]
            if (len(window) < params.liquidity_lookback_days or any(r.amount_cents is None for r in window)
                    or [r.trading_day for r in window] != known_window):
                reasons.append(_issue("LIQUIDITY_WINDOW_INCOMPLETE", "最近成交额窗口不完整，无法验证流动性。", "补齐完整的已归档成交额窗口。"))
            elif params.liquidity_min_avg_amount_cents is not None and sum(r.amount_cents for r in window) // len(window) < params.liquidity_min_avg_amount_cents:
                reasons.append(_issue("LIQUIDITY_BELOW_MINIMUM", "最近完整窗口的平均成交额低于配置门槛。", "保留观察，按既定门槛排除。"))
        eligible = not reasons and not issues
        if eligible:
            selected.append(Candidate(iid, inst["industry_code"], float(card["signalRank"]), simulatable=True))
        rows.append({"instrumentId": iid, "displayName": card["displayName"],
                     "position": card.get("position"), "signalRank": card["signalRank"],
                     "eligible": eligible, "blockingIssues": reasons, "reasons": reasons})
    out = {"schema": "aquant.research-simulation-context.v1", "label": LABEL,
           "formalStrategy": False, **binding, "decisionSnapshotId": snapshot_id,
           "decisionCutoffAt": ref.as_of_time.isoformat(),
           "decisionPublishedAt": ref.published_at.isoformat() if ref.published_at else None,
           "nextTradingDay": next_day.isoformat() if next_day else None,
           "eligibleCandidateCount": len(selected), "researchCandidateCount": len(rows),
           "candidates": rows[:limit], "blockingIssues": issues,
           "simulationParameters": {"maxHoldings": params.max_holdings,
               "initialTargetPct": str(params.initial_target_pct if params.initial_target_pct is not None else params.max_single_name_pct),
               "maxSingleNamePct": str(params.max_single_name_pct),
               "maxSingleIndustryPct": str(params.max_single_industry_pct),
               "maxTotalEquityPct": str(params.max_total_equity_pct)},
           "note": "当前财务研究排名用于人工选择，不开放正式S2，不回填历史决策。"}
    return out, selected, indexed


def assert_draft_binding(context: dict, *, research_snapshot_id: str, research_content_hash: str,
                         decision_cutoff_at: datetime, trading_day: date) -> None:
    if (research_snapshot_id, research_content_hash) != (context["researchSnapshotId"], context["researchContentHash"]):
        raise PlanError("STALE_SNAPSHOT", "research snapshot or content hash changed", research_snapshot_id,
                        "reload the research context and review your selection again")
    if decision_cutoff_at.tzinfo is None or decision_cutoff_at != _stamp(context["decisionCutoffAt"], "decisionCutoffAt"):
        raise PlanError("PIT_UNVERIFIED", "decision cutoff does not match the formal snapshot", context["decisionSnapshotId"],
                        "pass the exact server-resolved decision cutoff")
    if any(i["code"] == "RESEARCH_NOT_AVAILABLE" for i in context["blockingIssues"]):
        raise PlanError("PIT_UNVERIFIED", "research was not available at the decision cutoff", research_snapshot_id,
                        "publish a new decision snapshot instead of backdating the ranking")
    if context["nextTradingDay"] and trading_day.isoformat() != context["nextTradingDay"]:
        raise PlanError("PIT_UNVERIFIED", "manual research simulation must use the next known execution session", trading_day.isoformat(),
                        "use nextTradingDay from the verified formal trading calendar")


def assert_user_selection_timing(*, source: str, trading_day: date,
                                 saved_at: str | datetime | None = None) -> None:
    """Forward user choices must actually reach the server before execution open.

    Re-preview checks the immutable original saved timestamp, never its current
    review time. Engineering replay remains visibly non-confirmable.
    """
    if source != "USER_SELECTION":
        return
    try:
        stamp = _stamp(saved_at, "createdAt") if saved_at is not None else datetime.now(timezone.utc)
    except ResearchCandidateError as exc:
        raise PlanError("PIT_UNVERIFIED", "user selection has no verified timezone-aware saved timestamp",
                        trading_day.isoformat(), "restore the original saved-time evidence; do not backfill it") from exc
    execution_open = datetime.combine(trading_day, time(9, 30), MARKET_TZ)
    if stamp >= execution_open:
        raise PlanError("PIT_UNVERIFIED", "user selection was not saved before execution open",
                        trading_day.isoformat(),
                        "choose the next session before its open; a post-open choice is engineering replay, not a forward user decision")


def project_pending_draft(service: PlanService, *, portfolio_id: str, snapshot_id: str,
                          trading_day: date, candidates: list[Candidate], cash_cents: int,
                          lots: list) -> dict:
    """Call the same domain construction/order/fee algorithms as executable plans."""
    ref = service.reader.ref(snapshot_id)
    service._assert_fee_table_allowed(snapshot_id, trading_day)
    ids = sorted({c.instrument_id for c in candidates} | {l.instrument_id for l in lots if l.quantity_remaining > 0})
    bars = service._reference_bars(snapshot_id, trading_day, ref.as_of_time, ids)
    held = defaultdict(int)
    for lot in lots:
        if lot.quantity_remaining > 0:
            held[lot.instrument_id] += lot.quantity_remaining
    missing = sorted(set(held) - set(bars))
    if missing:
        raise PlanError("DATA_NOT_READY", "held securities have no verified reference price: " + ", ".join(missing),
                        portfolio_id, "publish verified holding prices before projecting risk")
    equity = cash_cents + sum(qty * bars[iid].close_cents for iid, qty in held.items())
    inst = {i["instrument_id"]: i for i in service.reader.instruments(snapshot_id, as_of=ref.as_of_time)}
    industry_value = defaultdict(int)
    for iid, qty in held.items():
        code = (inst.get(iid) or {}).get("industry_code")
        if not code:
            raise PlanError("DATA_NOT_READY", "held industry is unknown", iid, "verify held industry before projecting concentration")
        industry_value[code] += qty * bars[iid].close_cents
    liquid, liquid_excluded = service._filter_by_liquidity(snapshot_id=snapshot_id, trading_day=trading_day,
                                                         as_of=ref.as_of_time, candidates=candidates)
    aged, age_excluded, notes = service._filter_by_listing_age(snapshot_id=snapshot_id, trading_day=trading_day,
                                                            as_of=ref.as_of_time, candidates=liquid)
    result = construct_targets(candidates=aged, params=service.params, held=dict(held),
                               held_industry_value=dict(industry_value), equity_value_cents=equity,
                               bar_by_instrument=bars)
    orders = weights_to_orders(targets=result.targets, params=service.params,
                               price_by_instrument={iid: b.close_cents for iid, b in bars.items()},
                               lot_size_by_instrument={iid: service._lot_size(iid, trading_day) for iid in bars},
                               held_quantity=dict(held), sellable_quantity=service._sellable(lots, trading_day),
                               equity_value_cents=equity)
    fees = 0
    cash_after = cash_cents
    for order in orders:
        iid = order["instrument_id"]
        listing = service.listings.get(iid)
        rules = [r for r in service.board_rules if listing and (r.exchange, r.board) == listing and r.covers(trading_day)]
        if len(rules) != 1:
            raise PlanError("RULE_VERSION_MISSING", "no unique dated board rule for projected order", iid,
                            "verify the formal board and effective rule before preparing a draft")
        charge = service.fee_table.compute(side=order["side"], quantity=order["quantity"],
                                           price_cents=order["price_cents"], trading_day=trading_day).total_cents
        fees += charge
        gross = order["quantity"] * order["price_cents"]
        cash_after += (gross if order["side"] == "SELL" else -gross) - charge
        order.update(reference_price_day=bars[iid].trading_day.isoformat(), gross_cents=gross,
                     priceBasis="DECISION_REFERENCE_CLOSE_NOT_FILL")
    if cash_after < 0:
        raise PlanError("INSUFFICIENT_CASH", "projected total orders and fees exceed available cash", portfolio_id,
                        "reduce the selection and preview again")
    return {**result.as_preview(), "orders": orders, "estimatedFeesCents": fees,
            "cashAfterCents": cash_after, "cashBeforeCents": cash_cents,
            "excluded": [*result.excluded, *liquid_excluded, *age_excluded],
            "notes": [*result.notes, *notes], "referencePriceDay": max((b.trading_day.isoformat() for b in bars.values()), default=None),
            "priceBasis": "DECISION_REFERENCE_CLOSE_NOT_FILL",
            "feeVersion": service.fee_table.schedule_for(trading_day).fee_version,
            "commissionSource": service.fee_table.commission_source,
            "executionVerified": False}


def save_research_draft(con: sqlite3.Connection, *, data_dir: Path, research_path: Path,
                        subject: str, payload: dict) -> dict:
    """Archive the exact input once and persist an immutable, idempotent draft."""
    digest = payload["researchContentHash"]
    origins = data_dir / "research_simulation_origins"
    origins.mkdir(parents=True, exist_ok=True)
    target = origins / (digest.removeprefix("sha256:") + ".json")
    if target.exists():
        if file_hash(target) != digest:
            raise ResearchCandidateError("archived research origin differs from its hash")
    else:
        body = resolve_artifact_path(research_path).read_bytes()
        if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
            raise ResearchCandidateError("research changed before draft persistence")
        try:
            with target.open("xb") as stream:
                stream.write(body)
        except FileExistsError:
            if file_hash(target) != digest:
                raise ResearchCandidateError("concurrent research archive differs from its hash")
    document = dict(payload, researchOriginPath=str(target.resolve()))
    key = "research-draft-" + hashlib.sha256(_canonical({"subject": subject, "draft": document}).encode()).hexdigest()
    prior = con.execute("SELECT draft_json FROM research_simulation_draft WHERE idempotency_key=?", (key,)).fetchone()
    if prior:
        return json.loads(prior["draft_json"])
    document["draftId"] = key
    document["createdAt"] = datetime.now(timezone.utc).isoformat()
    assert_user_selection_timing(source=document["source"],
        trading_day=date.fromisoformat(document["tradingDay"]), saved_at=document["createdAt"])
    with write_tx(con):
        con.execute("INSERT INTO research_simulation_draft (draft_id,subject_id,source,portfolio_id,"
                    "research_snapshot_id,research_content_hash,decision_snapshot_id,trading_day,status,draft_json,created_at,idempotency_key) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, subject, document["source"], document["portfolioId"], document["researchSnapshotId"], digest,
                     document["decisionSnapshotId"], document["tradingDay"], document["status"], _canonical(document), document["createdAt"], key))
    return document


def research_drafts(con: sqlite3.Connection, *, subject: str, portfolio_id: str | None = None,
                    limit: int = 20) -> list[dict]:
    sql = "SELECT draft_json FROM research_simulation_draft WHERE subject_id=?"
    params: list = [subject]
    if portfolio_id:
        sql += " AND portfolio_id=?"
        params.append(portfolio_id)
    sql += " ORDER BY created_at DESC,draft_id DESC LIMIT ?"
    params.append(limit)
    return [dict(json.loads(row["draft_json"]), confirmationAvailable=False, requiresRepreview=True)
            for row in con.execute(sql, params).fetchall()]


def research_draft_origin(con: sqlite3.Connection, *, data_dir: Path, subject: str,
                          draft_id: str) -> tuple[dict, Path]:
    """Resolve a saved selection's hash-addressed origin within its owner scope.

    Never use the mutable latest-research path or a path supplied by the caller.
    A preview is ephemeral; this read leaves the original saved draft unchanged.
    """
    row = con.execute("SELECT * FROM research_simulation_draft WHERE draft_id=? AND subject_id=?",
                      (draft_id, subject)).fetchone()
    if row is None:
        raise KeyError(draft_id)
    document = json.loads(row["draft_json"])
    pairs = {"draftId": "draft_id", "portfolioId": "portfolio_id", "source": "source",
             "researchSnapshotId": "research_snapshot_id", "researchContentHash": "research_content_hash",
             "decisionSnapshotId": "decision_snapshot_id", "tradingDay": "trading_day", "status": "status"}
    if any(document.get(key) != row[column] for key, column in pairs.items()):
        raise ResearchCandidateError("saved research draft binding differs from its persisted record")
    digest = document["researchContentHash"]
    if not HASH.fullmatch(digest):
        raise ResearchCandidateError("saved research draft has an invalid origin hash")
    origin = data_dir / "research_simulation_origins" / (digest.removeprefix("sha256:") + ".json")
    if file_hash(origin) != digest:
        raise ResearchCandidateError("saved research origin differs from its immutable content hash")
    return document, origin
