"""Offline execution of a previously saved engineering selection.

These isolated accounts never issue or consume human confirmation tokens. The
actual source, saved-time evidence, order sizes and ledger are preserved. This
is a finite daily simulation calculation, not a broker execution or formal S2.
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from aquant.domain.data.db import connect, write_tx
from aquant.domain.data.snapshot import SnapshotError
from aquant.domain.portfolio.construction import TargetWeight
from aquant.domain.portfolio.plan import PlanError, PlanPreview, PlanService
from aquant.domain.simulation.corporate_actions import CashDividend
from aquant.domain.simulation.simulator import SimError
from aquant.operations.research_candidates import ResearchCandidateError
from aquant.operations.research_simulation import (
    HASH, MARKET_TZ, _stamp, assert_draft_binding, load_simulation_research,
    research_draft_origin, simulation_context,
)

SCHEMA = "aquant.engineering-simulation-run.v1"
LIMITATIONS = [
    "ENGINEERING_TRIAL: original engineering selection, not a user-confirmed H portfolio or formal S2",
    "Daily opening-price simulation with 5bp slippage, whole-order fills and 0.1% prior-volume cap; no opening queue or broker fills",
    "Corporate actions only include archived verified supported cash dividends; coverage is limited and taxes are PRE_TAX",
    "These snapshots preserve actual observed/publication times; historical observations are not a formal PIT backtest",
]


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _reject(message: str, object_id: str, code="DATA_NOT_READY"):
    raise PlanError(code, message, object_id, "restore the bound original evidence; do not backdate or substitute inputs")


def _archive_result(data_dir: Path, result: dict, results_dir: Path | None = None) -> dict:
    root = results_dir or data_dir / "engineering-simulation"
    root.mkdir(parents=True, exist_ok=True)
    path = root / (result["runId"] + ".result.json")
    body = _canonical(result)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(body)
    except FileExistsError:
        if path.read_text(encoding="utf-8") != body:
            _reject("saved engineering result differs from the authoritative result", result["runId"])
    return dict(result, resultPath=str(path.resolve()))


def list_engineering_runs(data_dir: Path) -> list[dict]:
    """Read immutable successful/blocked run records, never recompute a return."""
    con = connect(data_dir / "meta.sqlite", read_only=True)
    try:
        exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='engineering_simulation_run'").fetchone()
        if not exists:
            return []
        rows = con.execute("SELECT * FROM engineering_simulation_run ORDER BY created_at DESC,run_id").fetchall()
        result = []
        for row in rows:
            if row["result_json"]:
                doc = json.loads(row["result_json"])
                if _hash(doc) != row["result_hash"]:
                    _reject("engineering result hash differs from its persisted record", row["run_id"])
                result.append(dict(doc, resultPath=str((data_dir / "engineering-simulation" / (row["run_id"] + ".result.json")).resolve())))
            else:
                result.append(dict(json.loads(row["spec_json"]), status=row["status"]))
        return result
    finally:
        con.close()


def _execution_status(service, decision_id, execution_id, trading_day, ids):
    """Known suspension is a no-fill; unknown/ST/identity gaps block calculation."""
    before_ref, after_ref = service.reader.ref(decision_id), service.reader.ref(execution_id)
    before = {i["instrument_id"]: i for i in service.reader.instruments(decision_id, as_of=before_ref.as_of_time)}
    after = {i["instrument_id"]: i for i in service.reader.instruments(execution_id, as_of=after_ref.as_of_time)}
    suspended = []
    for iid in ids:
        inst = after.get(iid) or {}
        old = before.get(iid) or {}
        status = inst.get("simulation_eligibility") or {}
        sources = status.get("sources") or []
        valid = (status.get("status") == "VERIFIED" and status.get("as_of_date") == trading_day.isoformat()
                 and status.get("is_st") is False and isinstance(status.get("is_suspended"), bool)
                 and (inst.get("exchange"), inst.get("board")) == (old.get("exchange"), old.get("board"))
                 and inst.get("board") == "MAIN" and bool(sources)
                 and all(isinstance(s, dict) and s.get("source_id") and s.get("receipt_id")
                         and HASH.fullmatch(str(s.get("content_hash", ""))) for s in sources))
        try:
            valid = valid and all(_stamp(status[k], k) <= after_ref.as_of_time for k in ("available_at", "observed_at"))
        except (KeyError, ResearchCandidateError):
            valid = False
        if not valid:
            _reject("execution-day security status/board identity is not fully verified", iid)
        rows = service.reader.daily_quotes(execution_id, as_of=after_ref.as_of_time,
            instrument_id=iid, start=trading_day, end=trading_day)
        if status["is_suspended"]:
            if rows:
                _reject("suspension status conflicts with a tradable execution bar", iid)
            suspended.append(iid)
        elif not rows:
            _reject("non-suspended security has no actual execution-day bar", iid)
    return suspended


def _actions(rows: list[dict]) -> list[CashDividend]:
    result = []
    for row in rows:
        if (row.get("action_type") or row.get("type")) not in {"CASH_DIVIDEND", "cash_dividend"}:
            _reject("unsupported corporate action requires explicit handling before publishing performance", str(row.get("action_id")))
        micros = row.get("cash_per_share_micros")
        if micros is None:
            micros = int(row["cash_per_share_cents"]) * 10_000
        result.append(CashDividend(action_id=row["action_id"], instrument_id=row["instrument_id"],
            record_date=date.fromisoformat(row["record_date"]), ex_date=date.fromisoformat(row["ex_date"]),
            pay_date=date.fromisoformat(row["pay_date"]), cash_per_share_micros=int(micros),
            tax_treatment=row.get("tax_treatment", "PRE_TAX")))
    return result


def _persist_engineering_plan_tx(service, *, preview, source, origin_ref,
                                original_registered_at, initial_cash_cents, now):
    if source not in {"ENGINEERING_TRIAL", "FORWARD_ENGINEERING_SIMULATION"} or not origin_ref:
        _reject("explicit engineering source and immutable origin are required", preview.plan_id)
    if not preview.portfolio_id.startswith("pf-engineering-"):
        _reject("engineering plan requires an isolated account", preview.portfolio_id)
    if original_registered_at.tzinfo is None or original_registered_at >= datetime.combine(preview.trading_day, time(9, 30), MARKET_TZ):
        _reject("engineering source was not registered before execution open", origin_ref, "PIT_UNVERIFIED")
    service._assert_fee_table_allowed(preview.snapshot_id, preview.trading_day)
    from aquant.domain.portfolio.plan import _account_version, _plan_version
    if _plan_version(preview.targets, preview.snapshot_id, preview.trading_day, preview.orders,
            decision_cutoff_at=preview.decision_cutoff_at, execution_snapshot_id=preview.execution_snapshot_id,
            execution_cutoff_at=preview.execution_cutoff_at) != preview.plan_version:
        _reject("engineering preview changed after domain validation", preview.plan_id)
    original = service.con.execute("SELECT * FROM engineering_plan_provenance WHERE origin_ref=?", (origin_ref,)).fetchone()
    binding = service._snapshot_binding(preview)
    normalized = dict(preview.as_dict())
    normalized.pop("plan_id")
    record = {"source": source, "originRef": origin_ref, "originalRegisteredAt": original_registered_at.isoformat(),
              "initialCashCents": initial_cash_cents, "preview": normalized}
    if original:
        if json.loads(original["record_json"]) != record:
            _reject("engineering origin is already bound to a different preview", origin_ref)
        return {"plan_id": original["plan_id"], "status": service.load_persisted_plan(original["plan_id"])["status"], "idempotent_replay": True}
    if service.con.execute("SELECT 1 FROM simulation_plan WHERE plan_id=?", (preview.plan_id,)).fetchone():
        _reject("plan id belongs to another registered plan", preview.plan_id)
    stamp = now.isoformat()
    existing = service.con.execute("SELECT * FROM portfolio WHERE portfolio_id=?", (preview.portfolio_id,)).fetchone()
    if existing is None:
        kind = preview.portfolio_id.rsplit("-", 1)[-1].upper()
        service.con.execute("INSERT INTO portfolio (portfolio_id,kind,account_type,base_currency,initial_cash_cents,opened_at,status) VALUES (?,?,'SIMULATED','CNY',?,?,'ACTIVE')",
            (preview.portfolio_id, kind if kind in {"M", "E", "H", "B"} else "M", initial_cash_cents, stamp))
        service.con.execute("INSERT INTO cash_entry (entry_id,portfolio_id,entry_type,amount_cents,trading_day,occurred_at,note) VALUES (?,?,'INITIAL_DEPOSIT',?,?,?,?)",
            (preview.portfolio_id + "-INITIAL", preview.portfolio_id, initial_cash_cents,
             now.astimezone(MARKET_TZ).date().isoformat(), stamp,
             source + " opening balance; recorded at actual calculation time"))
    else:
        if existing["status"] != "ACTIVE" or existing["initial_cash_cents"] != initial_cash_cents:
            _reject("existing engineering account differs from its registered setting", preview.portfolio_id)
    if _account_version(service._load_lots(preview.portfolio_id), service._ledger_cash(preview.portfolio_id)) != preview.account_version:
        _reject("engineering preview does not match the current ledger", preview.portfolio_id)
    service.con.execute("INSERT INTO simulation_plan (plan_id,portfolio_id,snapshot_id,account_version,plan_version,status,created_at,frozen_at,expires_at,confirmed_by,confirmation_token_hash,rule_version,fee_version,diff_preview_json,estimated_fees_cents,idempotency_key) VALUES (?,?,?,?,?,'FROZEN',?,?,?,NULL,NULL,?,?,?,?,?)",
        (preview.plan_id, preview.portfolio_id, preview.snapshot_id, preview.account_version, preview.plan_version,
         stamp, stamp, (now + timedelta(days=36500)).isoformat(), service._rule_version(preview.trading_day),
         service.fee_table.schedule_for(preview.trading_day).fee_version, _canonical(preview.as_dict()),
         preview.estimated_fees_cents, "engineering|" + origin_ref))
    service._persist_plan_fee_binding(preview.plan_id, preview.trading_day)
    service.con.execute("INSERT INTO plan_snapshot_binding (plan_id,decision_snapshot_id,decision_cutoff_at,execution_snapshot_id,execution_cutoff_at,created_at) VALUES (?,?,?,?,?,?)",
        (preview.plan_id, binding["decision_snapshot_id"], binding["decision_cutoff_at"], binding["execution_snapshot_id"], binding["execution_cutoff_at"], stamp))
    ordered = [o for side in ("SELL", "BUY") for o in preview.orders if o["side"] == side]
    for seq, order in enumerate(ordered):
        service.con.execute('INSERT INTO "order" (order_id,portfolio_id,plan_id,snapshot_id,instrument_id,side,quantity,status,trading_day,process_sequence,created_at,idempotency_key) VALUES (?,?,?,?,?,?,?,\'PENDING\',?,?,?,?)',
            (f"{preview.plan_id}-{seq:03d}", preview.portfolio_id, preview.plan_id, preview.snapshot_id,
             order["instrument_id"], order["side"], order["quantity"], preview.trading_day.isoformat(), seq, stamp, f"engineering|{origin_ref}|{seq}"))
    service.con.execute("INSERT INTO engineering_plan_provenance (plan_id,source,origin_ref,original_registered_at,record_json,created_at) VALUES (?,?,?,?,?,?)",
        (preview.plan_id, source, origin_ref, original_registered_at.isoformat(), _canonical(record), stamp))
    return {"plan_id": preview.plan_id, "status": "FROZEN", "idempotent_replay": False}


def persist_engineering_plan(service: PlanService, *, preview: PlanPreview, source: str,
                             origin_ref: str, original_registered_at: datetime,
                             initial_cash_cents: int, now: datetime | None = None) -> dict:
    """Persist an explicitly registered isolated engineering plan without tokens.

    Caller must validate its immutable original source; this shared money writer
    validates exact preview/cutoff/account bindings and durable idempotency.
    """
    with write_tx(service.con):
        return _persist_engineering_plan_tx(service, preview=preview, source=source, origin_ref=origin_ref,
            original_registered_at=original_registered_at, initial_cash_cents=initial_cash_cents,
            now=now or datetime.now(timezone.utc))


def _prepare(service, data_dir, draft_id, subject, execution_id, now):
    draft, origin = research_draft_origin(service.con, data_dir=data_dir, subject=subject, draft_id=draft_id)
    if draft["source"] != "ENGINEERING_TRIAL":
        _reject("only explicitly labelled engineering drafts use this runner", draft_id)
    actual_saved = service.con.execute("SELECT created_at FROM research_simulation_draft WHERE draft_id=?", (draft_id,)).fetchone()[0]
    if draft.get("createdAt") != actual_saved:
        _reject("saved timestamp differs from the actual persisted timestamp", draft_id, "PIT_UNVERIFIED")
    day = date.fromisoformat(draft["tradingDay"])
    opening = datetime.combine(day, time(9, 30), MARKET_TZ)
    cutoff = _stamp(draft["decisionCutoffAt"], "decisionCutoffAt")
    if _stamp(actual_saved, "createdAt") >= opening or cutoff >= opening:
        _reject("engineering selection was not actually saved before execution open", draft_id, "PIT_UNVERIFIED")
    if draft.get("blockingIssues") or not draft.get("projection"):
        _reject("original engineering draft was not eligible", draft_id)
    if draft.get("cashBasis") != "NEW_SIMULATION_ACCOUNT_SETTING":
        _reject("engineering execution requires a frozen independent initial-account setting", draft_id)
    chosen = draft["selectedInstrumentIds"]
    if not chosen or len(chosen) != len(set(chosen)) or chosen != draft["selection"]["selectedInstrumentIds"]:
        _reject("original selection bindings are inconsistent", draft_id)
    packet, binding = load_simulation_research(origin)
    context, candidates, indexed = simulation_context(packet, binding, reader=service.reader,
        snapshot_id=draft["decisionSnapshotId"], params=service.params, limit=len(packet["candidates"]))
    assert_draft_binding(context, research_snapshot_id=draft["researchSnapshotId"],
        research_content_hash=draft["researchContentHash"], decision_cutoff_at=cutoff, trading_day=day)
    by_id = {c.instrument_id: c for c in candidates}
    if not set(chosen) <= by_id.keys():
        _reject("saved selected instruments are not eligible in the original decision source", draft_id)
    if execution_id is None:
        rows = service.con.execute("SELECT snapshot_id FROM snapshot WHERE kind='EOD' AND data_mode='PRODUCTION' AND status='PUBLISHED' ORDER BY published_at,snapshot_id").fetchall()
        matching = []
        for row in rows:
            ref = service.reader.ref(row[0])
            if service._execution_snapshot_time_valid(row[0], day, ref.as_of_time):
                matching.append(row[0])
        if not matching:
            _reject("actual execution-day snapshot is not published", day.isoformat(), "WAITING_EXECUTION_SNAPSHOT")
        if len(matching) != 1:
            _reject("multiple execution snapshots require an explicit immutable choice", day.isoformat())
        execution_id = matching[0]
    portfolio_id = "pf-engineering-" + draft_id.removeprefix("research-draft-")[:16]
    if service.reader.ref(execution_id).data_mode != "PRODUCTION":
        _reject("engineering results require actual production market evidence", execution_id)
    projection = draft["projection"]
    targets = [TargetWeight(row["instrument_id"], row["industry_code"], Decimal(row["weight_pct"]), row["rationale"])
               for row in projection["targets"]]
    preview = service.preview(portfolio_id=portfolio_id, snapshot_id=draft["decisionSnapshotId"],
        trading_day=day, as_of=cutoff, candidates=[by_id[i] for i in chosen],
        cash_available_cents=int(draft["initialCashCents"]), lots=[], confirm_subject="engineering:calculation",
        decision_snapshot_id=draft["decisionSnapshotId"], decision_cutoff_at=cutoff,
        execution_snapshot_id=execution_id, target_weights=targets)
    order_fields = ("instrument_id", "side", "quantity", "price_cents", "reference_price_day")
    def order_set(rows):
        return [tuple(r[k] for k in order_fields) for r in rows]
    if order_set(preview.orders) != order_set(projection["orders"]):
        _reject("recomputed orders differ from the saved ex-ante order sizes", draft_id)
    suspended = _execution_status(service, draft["decisionSnapshotId"], execution_id, day,
        sorted({o["instrument_id"] for o in preview.orders}))
    actions = _actions(service.reader.corporate_actions(execution_id, as_of=service.reader.ref(execution_id).as_of_time))
    if any(o["side"] != "BUY" for o in preview.orders):
        _reject("independent initial engineering draft unexpectedly contains a sell order", draft_id)
    run_id = "eng-" + hashlib.sha256(_canonical({"draftId": draft_id, "subject": subject}).encode()).hexdigest()[:24]
    preview.plan_id = "plan-engineering-" + run_id.removeprefix("eng-")
    preview.notes.append("ENGINEERING_TRIAL; no human confirmation; original_draft=" + draft_id)
    spec = {"schema": SCHEMA, "runId": run_id, "source": "ENGINEERING_TRIAL", "formalStrategy": False,
        "humanConfirmed": False, "draftId": draft_id, "subjectId": subject, "portfolioId": portfolio_id,
        "initialCashCents": int(draft["initialCashCents"]), "planId": preview.plan_id,
        "tradingDay": day.isoformat(), "draftCreatedAt": actual_saved, "decisionSnapshotId": draft["decisionSnapshotId"],
        "decisionCutoffAt": cutoff.isoformat(), "executionSnapshotId": execution_id,
        "executionCutoffAt": service.reader.ref(execution_id).as_of_time.isoformat(),
        "researchSnapshotId": draft["researchSnapshotId"], "researchContentHash": draft["researchContentHash"],
        "draft": draft, "preview": preview.as_dict(), "createdAt": now.isoformat(),
        "suspendedInstrumentIds": suspended, "limitations": LIMITATIONS,
        "calculationMode": "DELAYED_ENGINEERING_CALCULATION" if service.reader.ref(execution_id).as_of_time.astimezone(MARKET_TZ).date() > day else "ENGINEERING_CALCULATION",
        "executionMarketDay": day.isoformat(), "providerObservedCutoffAt": service.reader.ref(execution_id).as_of_time.isoformat(),
        "forwardPerformance": False,
        "delayedEngineeringCalculation": service.reader.ref(execution_id).as_of_time.astimezone(MARKET_TZ).date() > day,
        "simulationAssumptions": {"slippageBpsBuy": 5, "slippageBpsSell": 5, "priorVolumeParticipationCap": "0.001",
            "conservativeCashModeDifference": "NO_DIFFERENCE: independent initial account has no sell orders"}}
    # Account, unconfirmed engineering plan, provenance and run commit together.
    with write_tx(service.con):
        _persist_engineering_plan_tx(service, preview=preview, source="ENGINEERING_TRIAL",
            origin_ref=draft_id, original_registered_at=_stamp(actual_saved, "createdAt"),
            initial_cash_cents=int(draft["initialCashCents"]), now=now)
        stamp = now.isoformat()
        service.con.execute("INSERT INTO engineering_simulation_run (run_id,draft_id,subject_id,portfolio_id,plan_id,execution_snapshot_id,spec_json,status,created_at) VALUES (?,?,?,?,?,?,?,'PREPARED',?)",
            (run_id, draft_id, subject, portfolio_id, preview.plan_id, execution_id, _canonical(spec), stamp))
    return spec, actions


def _run_engineering_simulation(service: PlanService, *, data_dir: Path, draft_id: str, subject: str,
                              execution_snapshot_id: str | None = None, results_dir: Path | None = None,
                              now: datetime | None = None) -> dict:
    """Prepare/execute/value/reconcile once; retries resume from durable stages."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("actual calculation time must include a timezone")
    service = PlanService(service.con, service.reader, service.fee_table, service.board_rules,
        service.listings, service.params, delayed_engineering_calculation=True)
    row = service.con.execute("SELECT * FROM engineering_simulation_run WHERE draft_id=?", (draft_id,)).fetchone()
    try:
        if row is not None:
            if row["subject_id"] != subject:
                _reject("engineering draft belongs to another subject", draft_id)
            if execution_snapshot_id and execution_snapshot_id != row["execution_snapshot_id"]:
                _reject("engineering run is already bound to another execution snapshot", draft_id)
            if row["status"] == "COMPLETED":
                result = json.loads(row["result_json"])
                if _hash(result) != row["result_hash"]:
                    _reject("persisted result hash failed", row["run_id"])
                return _archive_result(data_dir, result, results_dir)
            spec = json.loads(row["spec_json"])
            # Re-verify the hash-addressed original even when resuming after a crash.
            research_draft_origin(service.con, data_dir=data_dir, subject=subject, draft_id=draft_id)
            preview = PlanPreview.from_dict(spec["preview"])
            _execution_status(service, spec["decisionSnapshotId"], spec["executionSnapshotId"], preview.trading_day,
                sorted({o["instrument_id"] for o in preview.orders}))
            actions = _actions(service.reader.corporate_actions(spec["executionSnapshotId"], as_of=preview.execution_cutoff_at))
        else:
            spec, actions = _prepare(service, data_dir, draft_id, subject, execution_snapshot_id, now)
            preview = PlanPreview.from_dict(spec["preview"])
        plan = service.load_persisted_plan(preview.plan_id)
        if plan["confirmed_by"] is not None or plan["confirmation_token_hash"] is not None:
            _reject("engineering plan unexpectedly contains human confirmation evidence", preview.plan_id)
        if plan["status"] == "FROZEN":
            service.execute(plan_id=preview.plan_id, now=now, corporate_actions=actions)
        elif plan["status"] != "EXECUTED":
            _reject("engineering plan is not resumable", preview.plan_id)
        # execute() commits money before corporate action advancement. Re-running
        # the idempotent action service closes that crash window without more fills.
        service._advance_corporate_actions(portfolio_id=preview.portfolio_id, trading_day=preview.trading_day,
            actions=actions, lots=service._load_lots(preview.portfolio_id), now=now)
        with write_tx(service.con):
            service.con.execute("UPDATE engineering_simulation_run SET status='EXECUTED' WHERE run_id=?", (spec["runId"],))
        valuation = service.value(portfolio_id=preview.portfolio_id, snapshot_id=spec["executionSnapshotId"],
            trading_day=preview.trading_day, as_of=preview.execution_cutoff_at, execution_plan_id=preview.plan_id,
            lots=service._load_lots(preview.portfolio_id), cash_available_cents=service._ledger_cash(preview.portfolio_id))
        recon = service.reconcile(portfolio_id=preview.portfolio_id)
        if not valuation["published"] or not recon["reconciled"]:
            _reject("ledger invariants blocked engineering valuation", spec["runId"])
        con, pid = service.con, preview.portfolio_id
        orders = [dict(r) for r in con.execute('SELECT * FROM "order" WHERE plan_id=? ORDER BY process_sequence', (preview.plan_id,))]
        fills = [dict(r) for r in con.execute("SELECT * FROM fill WHERE portfolio_id=? ORDER BY fill_id", (pid,))]
        result = dict(spec, status="COMPLETED", orders=orders, fills=fills,
            rejections=[{"order_id": o["order_id"], "instrument_id": o["instrument_id"], "reason": o["reject_reason"]}
                        for o in orders if o["status"] != "FILLED"],
            feeCharges=[dict(r) for r in con.execute("SELECT fc.* FROM fee_charge fc JOIN fill f ON f.fill_id=fc.fill_id WHERE f.portfolio_id=? ORDER BY fee_charge_id", (pid,))],
            cashEntries=[dict(r) for r in con.execute("SELECT * FROM cash_entry WHERE portfolio_id=? ORDER BY occurred_at,entry_id", (pid,))],
            cashCents=recon["cash_cents"], positions=recon["positions"], valuation=valuation, reconciliation=recon,
            completedAt=datetime.now(timezone.utc).isoformat(), fillCount=len(fills))
        with write_tx(con):
            con.execute("UPDATE engineering_simulation_run SET status='COMPLETED',result_json=?,result_hash=?,completed_at=? WHERE run_id=?",
                (_canonical(result), _hash(result), result["completedAt"], spec["runId"]))
        return _archive_result(data_dir, result, results_dir)
    except (PlanError, SnapshotError, SimError, ResearchCandidateError, KeyError, ValueError, OSError) as exc:
        error = exc.as_error() if hasattr(exc, "as_error") else {"code": "DATA_NOT_READY", "message": str(exc)}
        status = "WAITING_EXECUTION_SNAPSHOT" if error["code"] == "WAITING_EXECUTION_SNAPSHOT" else "DATA_NOT_READY"
        failed = {"schema": SCHEMA, "status": status, "source": "ENGINEERING_TRIAL", "formalStrategy": False,
            "humanConfirmed": False, "draftId": draft_id, "error": error, "valuation": None,
            "note": "No valid performance conclusion is published while evidence or reconciliation is incomplete."}
        persisted = service.con.execute("SELECT * FROM engineering_simulation_run WHERE draft_id=? AND subject_id=?", (draft_id, subject)).fetchone()
        if persisted and persisted["status"] != "COMPLETED":
            failed = dict(json.loads(persisted["spec_json"]), **failed)
            with write_tx(service.con):
                service.con.execute("UPDATE engineering_simulation_run SET status='DATA_NOT_READY',result_json=?,result_hash=? WHERE run_id=?",
                    (_canonical(failed), _hash(failed), persisted["run_id"]))
        return failed


@contextmanager
def _run_lock(data_dir: Path, draft_id: str):
    """OS-released local lock, so a duplicate CLI cannot execute concurrently."""
    root = data_dir / "engineering-simulation" / "locks"
    root.mkdir(parents=True, exist_ok=True)
    path = root / (hashlib.sha256(draft_id.encode()).hexdigest() + ".lock")
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise PlanError("RUN_IN_PROGRESS", "the same engineering draft is already being calculated", draft_id,
                            "read the saved result after the running calculation finishes") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def run_engineering_simulation(service: PlanService, *, data_dir: Path, draft_id: str, subject: str,
                              execution_snapshot_id: str | None = None, results_dir: Path | None = None,
                              now: datetime | None = None) -> dict:
    try:
        with _run_lock(data_dir, draft_id):
            return _run_engineering_simulation(service, data_dir=data_dir, draft_id=draft_id, subject=subject,
                execution_snapshot_id=execution_snapshot_id, results_dir=results_dir, now=now)
    except PlanError as exc:
        return {"schema": SCHEMA, "status": "DATA_NOT_READY", "source": "ENGINEERING_TRIAL", "draftId": draft_id,
                "humanConfirmed": False, "formalStrategy": False, "error": exc.as_error(), "valuation": None}
