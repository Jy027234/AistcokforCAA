"""Local temporal/ledger fixtures, not real-provider evidence."""
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.data.reader import QuoteRow
from aquant.domain.portfolio.construction import Candidate, ConstructionParams
from aquant.domain.portfolio.plan import PlanError, PlanService
from aquant.domain.simulation.board_rules import BOARD_RULES
from aquant.domain.simulation.verified_fees import verified_fee_table
from aquant.operations.engineering_simulation import _run_lock, list_engineering_runs, run_engineering_simulation
from aquant.operations.research_candidates import file_hash
from aquant.operations.research_simulation import project_pending_draft

DAY = date(2026, 10, 12)
DECISION = datetime.fromisoformat("2026-10-11T23:30:00+08:00")
OBSERVED = datetime.fromisoformat("2026-10-13T14:00:00+08:00")
IIDS = ["SH.600001", "SZ.000001"]


class Reader:
    def __init__(self, con):
        self.con = con
        self.store = SimpleNamespace(require_published=lambda sid: dict(con.execute("SELECT * FROM snapshot WHERE snapshot_id=?", (sid,)).fetchone()))
        self.execution_day = DAY
        self.suspension = False
        self.unknown = False
        self.actions = []
        self.rows = [QuoteRow(instrument_id=iid, trading_day=date(2026, 10, 9) - timedelta(days=i),
            open_cents=1000, high_cents=1020, low_cents=980, close_cents=1000,
            adjusted_close_cents=1000, volume_shares=10_000_000,
            amount_cents=20_000_000_000, prev_close_cents=1000) for iid in IIDS for i in range(30)]

    def ref(self, sid):
        row = self.con.execute("SELECT * FROM snapshot WHERE snapshot_id=?", (sid,)).fetchone()
        return SimpleNamespace(as_of_time=datetime.fromisoformat(row["as_of_time"]),
            published_at=datetime.fromisoformat(row["published_at"]), data_mode=row["data_mode"])

    def instruments(self, sid, *, as_of):
        day = self.execution_day if sid == "execution" else date(2026, 10, 9)
        return [{"instrument_id": iid, "exchange": "SSE" if iid.startswith("SH") else "SZSE",
            "board": "MAIN", "industry_code": "C39", "listed_on": "2000-01-01",
            "simulation_eligibility": {"status": "UNKNOWN" if self.unknown and sid == "execution" else "VERIFIED",
                "as_of_date": day.isoformat(), "is_st": False,
                "is_suspended": self.suspension and iid == IIDS[0] and sid == "execution",
                "available_at": as_of.isoformat(), "observed_at": as_of.isoformat(),
                "sources": [{"source_id": "local-test", "receipt_id": "test-1", "content_hash": "sha256:" + "b" * 64}]}}
            for iid in IIDS]

    def trading_calendar(self, sid, *, as_of):
        return [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(282)] + [DAY.isoformat()]

    def daily_quotes(self, sid, *, as_of, instrument_id=None, start=None, end=None):
        rows = list(self.rows)
        if sid == "execution":
            rows += [QuoteRow(instrument_id=iid, trading_day=self.execution_day,
                open_cents=1010, high_cents=1040, low_cents=990, close_cents=1030,
                adjusted_close_cents=1030, volume_shares=10_000_000,
                amount_cents=20_000_000_000, prev_close_cents=1000)
                for iid in IIDS if not (self.suspension and iid == IIDS[0])]
        return sorted([r for r in rows if (instrument_id is None or r.instrument_id == instrument_id)
                       and (start is None or r.trading_day >= start) and (end is None or r.trading_day <= end)],
                      key=lambda r: (r.instrument_id, r.trading_day))

    def corporate_actions(self, sid, *, as_of):
        return self.actions


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    con = connect(tmp_path / "meta.sqlite")
    apply_migrations(con)
    for sid, stamp in (("decision", DECISION), ("execution", OBSERVED)):
        con.execute("INSERT INTO snapshot (snapshot_id,kind,data_mode,status,input_cutoff_at,as_of_time,published_at,created_at,code_version,data_version,quality_status) VALUES (?,'EOD','PRODUCTION','PUBLISHED',?,?,?,?,'test','test','OK')",
            (sid, stamp.isoformat(), stamp.isoformat(), stamp.isoformat(), stamp.isoformat()))
    for i, iid in enumerate(IIDS):
        con.execute("INSERT INTO instrument (instrument_id,exchange,board,created_at,updated_at) VALUES (?,?,'MAIN',?,?)",
            (iid, "SSE" if i == 0 else "SZSE", DECISION.isoformat(), DECISION.isoformat()))
    reader = Reader(con)
    fees = verified_fee_table(commission_rate=Decimal("0.00025"), commission_min_cents=500,
        commission_source="USER_APPROVED_ASSUMPTION")
    params = ConstructionParams(initial_target_pct=Decimal("4"), liquidity_min_avg_amount_cents=5_000_000_000)
    svc = PlanService(con, reader, fees, BOARD_RULES, {IIDS[0]: ("SSE", "MAIN"), IIDS[1]: ("SZSE", "MAIN")}, params)
    packet = {"snapshotId": "research-test", "dataAsOf": "2026-10-09", "generatedAt": "2026-10-09T21:30:00+08:00",
        "asOfTime": "2026-10-09T17:00:00+08:00", "capturedTo": "2026-10-09T17:00:00+08:00",
        "candidates": [{"instrumentId": iid, "displayName": "local fixture", "position": i+1,
            "exchange": "SSE" if i == 0 else "SZSE", "board": "MAIN", "status": "RANKED",
            "signalRank": 1-i/2, "quote": {}, "evidence": []} for i, iid in enumerate(IIDS)]}
    source = tmp_path / "source.json"
    source.write_text(json.dumps(packet), encoding="utf-8")
    digest = file_hash(source)
    folder = tmp_path / "research_simulation_origins"
    folder.mkdir()
    (folder / (digest.removeprefix("sha256:") + ".json")).write_bytes(source.read_bytes())
    projection = project_pending_draft(svc, portfolio_id="original-trial", snapshot_id="decision", trading_day=DAY,
        candidates=[Candidate(iid, "C39", 1-i/2) for i, iid in enumerate(IIDS)], cash_cents=100_000_000, lots=[])
    doc = {"draftId": "research-draft-" + "a" * 64, "source": "ENGINEERING_TRIAL", "portfolioId": "original-trial",
        "researchSnapshotId": "research-test", "researchContentHash": digest, "decisionSnapshotId": "decision",
        "decisionCutoffAt": DECISION.isoformat(), "tradingDay": DAY.isoformat(), "status": "WAITING_EXECUTION_SNAPSHOT",
        "selectedInstrumentIds": IIDS, "selection": {"selectedInstrumentIds": IIDS}, "blockingIssues": [],
        "cashBasis": "NEW_SIMULATION_ACCOUNT_SETTING", "initialCashCents": 100_000_000,
        "projection": projection, "createdAt": "2026-10-11T23:32:00+08:00"}
    con.execute("INSERT INTO research_simulation_draft VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (doc["draftId"], "user:test", doc["source"], doc["portfolioId"], doc["researchSnapshotId"], digest,
         "decision", DAY.isoformat(), doc["status"], json.dumps(doc), doc["createdAt"], doc["draftId"]))
    monkeypatch.setattr("aquant.operations.research_simulation.load_research_scan", lambda *a, **k: {})
    yield svc, tmp_path, doc
    con.close()


def run(setup):
    svc, root, doc = setup
    return run_engineering_simulation(svc, data_dir=root, draft_id=doc["draftId"], subject="user:test",
        execution_snapshot_id="execution", now=OBSERVED + timedelta(minutes=1))


def test_default_rejects_delayed_execution_cutoff(setup):
    svc, root, doc = setup
    with pytest.raises(PlanError, match="end-of-day snapshot"):
        svc.preview(portfolio_id="pf-user-test", snapshot_id="decision", trading_day=DAY, as_of=DECISION,
            candidates=[Candidate(iid, "C39", 1) for iid in IIDS], cash_available_cents=100_000_000, lots=[],
            confirm_subject="server:preview", decision_snapshot_id="decision", decision_cutoff_at=DECISION,
            execution_snapshot_id="execution")


def test_delayed_engineering_uses_actual_day_bars_and_preserves_observed_time(setup):
    svc, root, doc = setup
    result = run(setup)
    assert result["status"] == "COMPLETED", result
    assert result["calculationMode"] == "DELAYED_ENGINEERING_CALCULATION"
    assert result["executionMarketDay"] == DAY.isoformat()
    assert result["providerObservedCutoffAt"] == OBSERVED.isoformat()
    assert result["fillCount"] == 2 and all(f["trading_day"] == DAY.isoformat() for f in result["fills"])
    assert all(f["price_cents"] == 1011 for f in result["fills"])
    assert result["reconciliation"]["reconciled"] and result["valuation"]["published"]
    assert not result["humanConfirmed"] and not result["forwardPerformance"]
    assert result["initialCashCents"] == 100_000_000
    assert svc.con.execute("SELECT kind FROM portfolio WHERE portfolio_id=?", (result["portfolioId"],)).fetchone()[0] == "M"
    for table in ("plan_confirmation", "decision_log"):
        assert svc.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    plan = svc.load_persisted_plan(result["planId"])
    assert plan["confirmed_by"] is None and plan["confirmation_token_hash"] is None
    with pytest.raises(PlanError, match="no durable confirmation"):
        svc.execute(plan_id=plan["plan_id"], subject="user:test")
    before = {t: svc.con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in ("fill", "fee_charge", "cash_entry", "valuation", "portfolio")}
    assert run(setup) == result
    assert list_engineering_runs(root)[0] == result
    assert before == {t: svc.con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in before}


@pytest.mark.parametrize("defect", ["future_bar", "unknown", "late_saved", "source_changed", "unsupported_action", "quantity_changed"])
def test_unverified_or_post_open_inputs_never_publish_performance(setup, defect):
    svc, root, doc = setup
    if defect == "future_bar": svc.reader.execution_day = date(2026, 10, 13)
    if defect == "unknown": svc.reader.unknown = True
    if defect == "late_saved":
        # New local fixture record; avoid rewriting an immutable saved draft.
        svc.con.execute("DROP TRIGGER research_simulation_draft_immutable")
        doc["createdAt"] = "2026-10-12T09:30:00+08:00"
        svc.con.execute("UPDATE research_simulation_draft SET draft_json=?,created_at=?", (json.dumps(doc), doc["createdAt"]))
    if defect == "source_changed": next((root / "research_simulation_origins").glob("*.json")).write_text("changed", encoding="utf-8")
    if defect == "unsupported_action": svc.reader.actions = [{"action_id": "unsupported", "type": "RIGHTS_ISSUE"}]
    if defect == "quantity_changed":
        svc.con.execute("DROP TRIGGER research_simulation_draft_immutable")
        doc["projection"]["orders"][0]["quantity"] += 100
        svc.con.execute("UPDATE research_simulation_draft SET draft_json=?", (json.dumps(doc),))
    result = run(setup)
    assert result["status"] == "DATA_NOT_READY", result
    assert result["valuation"] is None
    assert svc.con.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0] == 0
    assert svc.con.execute("SELECT COUNT(*) FROM fill").fetchone()[0] == 0


def test_known_suspension_is_explicit_no_fill_not_fake_price(setup):
    svc, root, doc = setup
    svc.reader.suspension = True
    result = run(setup)
    assert result["status"] == "COMPLETED", result
    assert result["fillCount"] == 1 and result["rejections"]
    assert result["suspendedInstrumentIds"] == [IIDS[0]]


def test_crash_after_money_commit_resumes_without_duplicate_fees(setup, monkeypatch):
    svc, root, doc = setup
    original = PlanService.value
    def crash(*a, **k):
        raise OSError("test interrupted after execution")
    monkeypatch.setattr(PlanService, "value", crash)
    failed = run(setup)
    assert failed["status"] == "DATA_NOT_READY"
    fees = svc.con.execute("SELECT SUM(amount_cents) FROM fee_charge").fetchone()[0]
    assert svc.con.execute("SELECT COUNT(*) FROM fill").fetchone()[0] == 2
    monkeypatch.setattr(PlanService, "value", original)
    result = run(setup)
    assert result["status"] == "COMPLETED", result
    assert result["reconciliation"]["fees_total_cents"] == fees
    assert svc.con.execute("SELECT COUNT(*) FROM fill").fetchone()[0] == 2


def test_same_ex_and_pay_day_is_idempotent_on_recovery(setup):
    svc, root, doc = setup
    svc.reader.actions = [{"type": "CASH_DIVIDEND", "action_id": "same-day-dividend", "instrument_id": IIDS[0],
        "record_date": DAY.isoformat(), "ex_date": DAY.isoformat(), "pay_date": DAY.isoformat(),
        "cash_per_share_micros": 100_000, "tax_treatment": "PRE_TAX"}]
    result = run(setup)
    assert result["status"] == "COMPLETED", result
    assert svc.con.execute("SELECT COUNT(*) FROM cash_entry WHERE entry_type='DIVIDEND_RECEIVABLE_SETTLED'").fetchone()[0] == 1
    assert result["reconciliation"]["reconciled"]


def test_default_valuation_cannot_rewrite_engineering_ledger(setup):
    svc, root, doc = setup
    result = run(setup)
    assert result["status"] == "COMPLETED"
    with pytest.raises(PlanError, match="reserved"):
        svc.value(portfolio_id=result["portfolioId"], snapshot_id="execution", trading_day=DAY,
            as_of=OBSERVED, execution_plan_id=result["planId"], lots=svc._load_lots(result["portfolioId"]),
            cash_available_cents=result["cashCents"])


def test_default_ordinary_valuation_keeps_same_day_boundary(setup):
    svc, root, doc = setup
    with pytest.raises(PlanError, match="does not match"):
        svc.value(portfolio_id="pf-user-ordinary", snapshot_id="execution", trading_day=DAY,
            as_of=OBSERVED, execution_plan_id=None, lots=[], cash_available_cents=100_000_000)
    assert svc.con.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0] == 0


def test_duplicate_running_cli_cannot_mutate_accounts(setup):
    svc, root, doc = setup
    with _run_lock(root, doc["draftId"]):
        result = run(setup)
    assert result["error"]["code"] == "RUN_IN_PROGRESS"
    assert svc.con.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0] == 0


def test_public_new_human_plan_cannot_change_existing_engineering_account(setup):
    svc, root, doc = setup
    result = run(setup)
    assert result["status"] == "COMPLETED"
    pid = result["portfolioId"]
    before = {table: [tuple(row) for row in svc.con.execute(f'SELECT * FROM "{table}"')]
              for table in ("simulation_plan", "order", "fill", "fee_charge", "cash_entry", "position_lot", "valuation")}
    with pytest.raises(PlanError) as refused:
        svc.preview(portfolio_id=pid, snapshot_id="decision", trading_day=DAY, as_of=DECISION,
            candidates=[], cash_available_cents=result["cashCents"], lots=svc._load_lots(pid),
            confirm_subject="user:test", decision_snapshot_id="decision", decision_cutoff_at=DECISION,
            execution_snapshot_id="execution")
    assert refused.value.code == "ENGINEERING_ACCOUNT_RESERVED"
    from aquant.domain.portfolio.plan import PlanPreview
    saved = PlanPreview.from_dict(result["preview"])
    with pytest.raises(PlanError) as refused:
        svc.issue_confirmation(preview=saved, subject="user:test", current_lots=svc._load_lots(pid),
                               current_cash_cents=result["cashCents"])
    assert refused.value.code == "ENGINEERING_ACCOUNT_RESERVED"
    with pytest.raises(PlanError) as refused:
        svc.freeze(preview=saved, confirm_subject="user:test", confirmation_token="caller-token",
            expected_account_version=saved.account_version, current_lots=svc._load_lots(pid),
            current_cash_cents=result["cashCents"])
    assert refused.value.code == "ENGINEERING_ACCOUNT_RESERVED"
    with pytest.raises(PlanError) as refused:
        svc._ensure_account(pid, initial_cash_cents=result["cashCents"], initial_lots=[], now=OBSERVED,
                            enforce_match=False)
    assert refused.value.code == "ENGINEERING_ACCOUNT_RESERVED"
    assert svc.con.execute("SELECT COUNT(*) FROM plan_confirmation").fetchone()[0] == 0
    assert before == {table: [tuple(row) for row in svc.con.execute(f'SELECT * FROM "{table}"')] for table in before}
    assert run(setup) == result  # Internal registered runner remains idempotent.
