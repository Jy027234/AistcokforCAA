"""API contract/restart tests using local fixtures, never provider evidence."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/api"))
from main import SNAPSHOT_ID, build_state, create_app
from aquant.domain.portfolio.construction import ConstructionParams
from aquant.domain.simulation.fees import FeeSchedule, FeeTable
from aquant.operations.research_candidates import file_hash
from aquant.operations import research_simulation as research_operations

USER = {"X-Aquant-Subject": "user:demo"}
IIDS = ["SYN.A.600519", "SYN.A.000001"]


def set_selection_clock(monkeypatch, stamp):
    instant = datetime.fromisoformat(stamp)
    class SelectionClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)
    monkeypatch.setattr(research_operations, "datetime", SelectionClock)


def test_api_defaults_follow_original_twenty_name_four_percent_design(tmp_path):
    state = build_state(tmp_path)
    assert state.params.max_holdings == 20
    assert state.params.initial_target_pct == Decimal("4")
    assert state.params.max_single_name_pct == Decimal("10")
    assert state.params.max_single_industry_pct == Decimal("30")
    state.con.close()


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("AQUANT_IDENTITY_MODE", "LOCAL_LOOPBACK_DEMO")
    monkeypatch.setenv("AQUANT_TRIAL_SUBJECT", "user:demo")
    artifact = tmp_path / "research-fixture.json"
    packet = {"snapshotId": "research-api-fixture", "candidates": [
        {"instrumentId": iid, "displayName": "local synthetic fixture", "exchange": "SSE" if i == 0 else "SZSE",
         "board": "MAIN", "status": "RANKED", "position": i + 1, "signalRank": 1 - i / 2}
        for i, iid in enumerate(IIDS)]}
    artifact.write_text(json.dumps(packet), encoding="utf-8")
    monkeypatch.setenv("AQUANT_RESEARCH_CANDIDATES_FILE", str(artifact))
    binding = {"researchSnapshotId": packet["snapshotId"], "researchContentHash": file_hash(artifact),
        "researchObservedAt": "2026-09-11T11:00:00+00:00", "researchAvailableAt": "2026-09-11T12:00:00+00:00",
        "researchDataAsOf": "2026-09-11", "researchOriginPath": str(artifact)}
    def load_local_fixture(path):
        source = json.loads(path.read_text(encoding="utf-8"))
        return source, dict(binding, researchSnapshotId=source["snapshotId"],
                            researchContentHash=file_hash(path), researchOriginPath=str(path))
    monkeypatch.setattr("main.load_simulation_research", load_local_fixture)
    state = build_state(tmp_path / "app")
    original_ref = state.reader.ref
    monkeypatch.setattr(state.reader, "ref", lambda sid: replace(original_ref(sid), data_mode="PRODUCTION", watermark=None))
    original_insts = state.reader.instruments
    def instruments(sid, *, as_of):
        rows = deepcopy(original_insts(SNAPSHOT_ID, as_of=original_ref(SNAPSHOT_ID).as_of_time))
        execution = sid == "local-execution-fixture"
        for row in rows:
            status_stamp = "2026-09-14T07:00:00+00:00" if execution else binding["researchObservedAt"]
            row["simulation_eligibility"] = {"status": "VERIFIED", "as_of_date": "2026-09-14" if execution else "2026-09-11",
                "available_at": status_stamp, "observed_at": status_stamp,
                "is_st": False, "is_suspended": False,
                "sources": [{"source_id": "local-fixture", "receipt_id": "test-receipt", "content_hash": "sha256:" + "b" * 64}]}
        return rows
    monkeypatch.setattr(state.reader, "instruments", instruments)
    original_calendar = state.reader.trading_calendar
    monkeypatch.setattr(state.reader, "trading_calendar", lambda sid, **kw: [*original_calendar(sid, **kw), "2026-09-14"])
    state.params = ConstructionParams(max_holdings=4, exclude_listing_days=1,
                                      liquidity_lookback_days=3, liquidity_min_avg_amount_cents=None)
    fees = FeeTable([FeeSchedule("local-test-real-fee", date(2023, 8, 28), None, Decimal("0.00025"),
        500, Decimal("0.0005"), Decimal("0.00001"))], commission_source="USER_APPROVED_ASSUMPTION")
    state.fees = state.service.fee_table = fees
    # The user choice is actually saved before this local fixture's next open.
    # Provider and client dates remain independently represented in the fixture.
    set_selection_clock(monkeypatch, "2026-09-11T13:00:00+00:00")
    # A second snapshot's timing is a local fixture for preview/confirmation,
    # not a claim that future production prices exist.
    state.con.execute("INSERT INTO snapshot (snapshot_id,kind,data_mode,status,input_cutoff_at,as_of_time,"
        "published_at,created_at,code_version,data_version,quality_status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("local-execution-fixture", "EOD", "PRODUCTION", "PUBLISHED", "2026-09-14T15:00:00+08:00",
         "2026-09-14T15:00:00+08:00", "2026-09-14T16:00:00+08:00", "2026-09-14T16:00:00+08:00", "test", "test", "OK"))
    app = create_app(state=state)
    with TestClient(app) as client:
        ctx = client.get("/api/v1/research/simulation/context").json()
        assert ctx["eligibleCandidateCount"] == 2, ctx
        body = {"portfolioId": "pf-api-user-research", "researchSnapshotId": ctx["researchSnapshotId"],
            "researchContentHash": ctx["researchContentHash"], "decisionSnapshotId": ctx["decisionSnapshotId"],
            "decisionCutoffAt": ctx["decisionCutoffAt"], "tradingDay": ctx["nextTradingDay"],
            "selectedInstrumentIds": [IIDS[1]], "selectionReason": "local test selection",
            "excludedReasons": {IIDS[0]: "local test exclusion"}, "initialCashCents": 10_000_000}
        yield client, state, body


def test_pending_draft_survives_refresh_without_plan_or_account(setup):
    client, state, body = setup
    first = client.post("/api/v1/research/simulation/draft", json=body, headers=USER)
    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["status"] == "WAITING_EXECUTION_SNAPSHOT"
    assert payload["planId"] is None and not payload["confirmationAvailable"]
    assert payload["selectedInstrumentIds"] == body["selectedInstrumentIds"]
    assert payload["projection"]["orders"] and not payload["projection"]["executionVerified"]
    assert client.post("/api/v1/research/simulation/draft", json=body, headers=USER).json() == payload
    with TestClient(create_app(state=state)) as reopened:
        saved = reopened.get("/api/v1/research/simulation/drafts", headers=USER).json()["drafts"]
        assert saved[0]["draftId"] == payload["draftId"]
        assert not saved[0]["confirmationAvailable"]
    assert state.con.execute("SELECT COUNT(*) FROM simulation_plan").fetchone()[0] == 0
    assert state.con.execute("SELECT COUNT(*) FROM portfolio WHERE portfolio_id=?", (body["portfolioId"],)).fetchone()[0] == 0


def test_context_limit_exposes_whole_verified_pool_without_changing_candidate_counts(setup):
    client, _, _ = setup
    first = client.get("/api/v1/research/simulation/context?limit=1").json()
    complete = client.get("/api/v1/research/simulation/context?limit=5000").json()
    assert len(first["candidates"]) == 1 and len(complete["candidates"]) == 2
    assert first["eligibleCandidateCount"] == complete["eligibleCandidateCount"] == 2
    assert client.get("/api/v1/research/simulation/context?limit=5001").status_code == 422


def test_ready_preview_uses_existing_single_use_confirmation_and_records_origin(setup):
    client, state, body = setup
    body["executionSnapshotId"] = "local-execution-fixture"
    response = client.post("/api/v1/research/simulation/draft", json=body, headers=USER)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "READY_FOR_REVIEW" and result["confirmationAvailable"]
    plan_id = result["planId"]
    assert client.post(f"/api/v1/plans/{plan_id}/confirmation", headers={"X-Aquant-Subject": "model:test"}).status_code == 403
    token = client.post(f"/api/v1/plans/{plan_id}/confirmation", headers=USER).json()["confirmationToken"]
    frozen = client.post(f"/api/v1/plans/{plan_id}/freeze", headers=USER,
        json={"plan_id": plan_id, "confirmation_token": token})
    assert frozen.status_code == 200, frozen.text
    saved = state.con.execute("SELECT model_proposed_json,human_final_json,reason_note FROM decision_log WHERE plan_id=?", (plan_id,)).fetchone()
    assert json.loads(saved["human_final_json"])["selection"]["excludedReasons"] == body["excludedReasons"]
    assert json.loads(saved["model_proposed_json"])["research_origin"]["researchContentHash"] == body["researchContentHash"]
    assert saved["reason_note"] == body["selectionReason"]
    assert state.con.execute("SELECT COUNT(*) FROM fill").fetchone()[0] == 0
    repeat = client.post(f"/api/v1/plans/{plan_id}/freeze", headers=USER,
        json={"plan_id": plan_id, "confirmation_token": token})
    assert repeat.status_code == 409


def test_engineering_source_does_not_masquerade_as_user_confirmation(setup):
    client, state, body = setup
    body.update(source="ENGINEERING_TRIAL", portfolioId="pf-api-engineering", executionSnapshotId="local-execution-fixture")
    result = client.post("/api/v1/research/simulation/draft", json=body, headers=USER)
    assert result.status_code == 200, result.text
    payload = result.json()
    assert payload["source"] == "ENGINEERING_TRIAL" and not payload["confirmationAvailable"]
    assert client.post(f"/api/v1/plans/{payload['planId']}/confirmation", headers=USER).status_code == 404
    assert state.con.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0] == 0
    assert client.get("/api/v1/research/simulation/drafts", headers=USER).json()["drafts"][0]["source"] == "ENGINEERING_TRIAL"


@pytest.mark.parametrize("mutation,status", [("hash", 409), ("outside", 409), ("duplicate", 422), ("reason", 422), ("cutoff", 422)])
def test_invalid_or_stale_request_cannot_create_account_or_draft(setup, mutation, status):
    client, state, body = setup
    if mutation == "hash": body["researchContentHash"] = "sha256:" + "0" * 64
    if mutation == "outside": body["selectedInstrumentIds"] = ["SH.999999"]
    if mutation == "duplicate": body["selectedInstrumentIds"] *= 2
    if mutation == "reason": body["selectionReason"] = "  "
    if mutation == "cutoff": body["decisionCutoffAt"] = "2026-09-11T12:29:00+00:00"
    response = client.post("/api/v1/research/simulation/draft", json=body, headers=USER)
    assert response.status_code == status, response.text
    assert state.con.execute("SELECT COUNT(*) FROM research_simulation_draft").fetchone()[0] == 0
    assert state.con.execute("SELECT COUNT(*) FROM portfolio WHERE portfolio_id=?", (body["portfolioId"],)).fetchone()[0] == 0


def test_existing_account_cash_cannot_be_replaced_by_initial_setting(setup):
    client, state, body = setup
    state.service._ensure_account(body["portfolioId"], initial_cash_cents=2_000_000,
                                  initial_lots=[], now=datetime.now().astimezone())
    response = client.post("/api/v1/research/simulation/draft", json=body, headers=USER)
    assert response.status_code == 409
    assert state.service._ledger_cash(body["portfolioId"]) == 2_000_000


def test_saved_selection_repreviews_archived_origin_after_daily_replacement_and_restart(setup, monkeypatch):
    client, state, body = setup
    saved = client.post("/api/v1/research/simulation/draft", json=body, headers=USER).json()
    original = state.con.execute("SELECT draft_json FROM research_simulation_draft WHERE draft_id=?",
                                 (saved["draftId"],)).fetchone()[0]
    # Daily publishing may change or even temporarily make the current file
    # unreadable. A saved decision must depend only on its exact old origin.
    latest = state.data_dir.parent / "replacement.json"
    latest.write_text('{"snapshotId":"different-new-research","candidates":[]}', encoding="utf-8")
    monkeypatch.setenv("AQUANT_RESEARCH_CANDIDATES_FILE", str(latest))
    state.previews.clear()
    state.preview_decisions.clear()
    with TestClient(create_app(state=state)) as reopened:
        result = reopened.post(f"/api/v1/research/simulation/drafts/{saved['draftId']}/preview",
            json={"executionSnapshotId": "local-execution-fixture"}, headers=USER)
        assert result.status_code == 200, result.text
        live = result.json()
        assert live["status"] == "READY_FOR_REVIEW" and live["confirmationAvailable"]
        assert live["draftId"] == live["sourceDraftId"] == saved["draftId"]
        assert not live["requiresRepreview"]
        for key in ("researchSnapshotId", "researchContentHash", "decisionSnapshotId",
                    "decisionCutoffAt", "tradingDay", "selection", "selectedInstrumentIds",
                    "originalRanking", "createdAt", "researchOriginPath"):
            assert live[key] == saved[key], key
        assert live["originalInitialCashCents"] == saved["initialCashCents"]
        history = reopened.get("/api/v1/research/simulation/drafts", headers=USER).json()["drafts"]
        assert len(history) == 1 and history[0]["requiresRepreview"]
        assert not history[0]["confirmationAvailable"] and history[0]["planId"] is None
        token = reopened.post(f"/api/v1/plans/{live['planId']}/confirmation", headers=USER).json()["confirmationToken"]
        frozen = reopened.post(f"/api/v1/plans/{live['planId']}/freeze", headers=USER,
            json={"plan_id": live["planId"], "confirmation_token": token})
        assert frozen.status_code == 200, frozen.text
    assert state.con.execute("SELECT draft_json FROM research_simulation_draft WHERE draft_id=?",
                              (saved["draftId"],)).fetchone()[0] == original
    decision = json.loads(state.con.execute("SELECT human_final_json FROM decision_log WHERE plan_id=?",
                                            (live["planId"],)).fetchone()[0])
    assert decision["research_origin"]["researchContentHash"] == saved["researchContentHash"]
    assert decision["research_origin"]["sourceDraftId"] == saved["draftId"]
    assert decision["selection"] == saved["selection"]
    assert state.con.execute("SELECT COUNT(*) FROM fill").fetchone()[0] == 0


@pytest.mark.parametrize("mutation,expected", [("missing", 503), ("hash", 503), ("other_owner", 404),
                                               ("unknown_id", 404), ("caller_selection", 422)])
def test_saved_preview_refuses_missing_or_tampered_origin_and_selection_overrides(setup, mutation, expected):
    client, state, body = setup
    saved = client.post("/api/v1/research/simulation/draft", json=body, headers=USER).json()
    draft_id = saved["draftId"]
    request = {"executionSnapshotId": "local-execution-fixture"}
    if mutation == "missing": Path(saved["researchOriginPath"]).unlink()
    if mutation == "hash": Path(saved["researchOriginPath"]).write_text("{}", encoding="utf-8")
    if mutation == "unknown_id": draft_id = "missing-draft-id"
    if mutation == "caller_selection": request["selectedInstrumentIds"] = [IIDS[0]]
    if mutation == "other_owner":
        from aquant.operations.research_simulation import save_research_draft
        others = save_research_draft(state.con, data_dir=state.data_dir,
            research_path=Path(saved["researchOriginPath"]), subject="user:bob", payload=saved)
        draft_id = others["draftId"]
    response = client.post(f"/api/v1/research/simulation/drafts/{draft_id}/preview", json=request, headers=USER)
    assert response.status_code == expected, response.text
    assert not state.previews
    assert state.con.execute("SELECT COUNT(*) FROM portfolio WHERE portfolio_id=?", (body["portfolioId"],)).fetchone()[0] == 0


@pytest.mark.parametrize("mutation", ["prior_day_status", "future_status_observation", "wrong_execution_day"])
def test_saved_preview_requires_execution_day_status_and_exact_execution_day(setup, monkeypatch, mutation):
    client, state, body = setup
    saved = client.post("/api/v1/research/simulation/draft", json=body, headers=USER).json()
    original = state.reader.instruments
    if mutation == "wrong_execution_day":
        original_ref = state.reader.ref
        monkeypatch.setattr(state.reader, "ref", lambda sid: replace(original_ref(sid),
            as_of_time=datetime.fromisoformat("2026-09-15T15:00:00+08:00"))
            if sid == "local-execution-fixture" else original_ref(sid))
    else:
        def instruments(sid, *, as_of):
            rows = original(sid, as_of=as_of)
            if sid == "local-execution-fixture":
                for row in rows:
                    status = row["simulation_eligibility"]
                    if mutation == "prior_day_status": status["as_of_date"] = "2026-09-11"
                    else: status["observed_at"] = "2026-09-15T07:00:00+00:00"
            return rows
        monkeypatch.setattr(state.reader, "instruments", instruments)
    response = client.post(f"/api/v1/research/simulation/drafts/{saved['draftId']}/preview",
        json={"executionSnapshotId": "local-execution-fixture"}, headers=USER)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "DATA_NOT_READY"
    assert not state.previews
    assert state.con.execute("SELECT COUNT(*) FROM portfolio WHERE portfolio_id=?", (body["portfolioId"],)).fetchone()[0] == 0


def test_engineering_saved_preview_never_receives_human_confirmation(setup, monkeypatch):
    client, state, body = setup
    set_selection_clock(monkeypatch, "2026-09-14T20:30:00+08:00")
    body.update(source="ENGINEERING_TRIAL", portfolioId="pf-saved-engineering")
    saved = client.post("/api/v1/research/simulation/draft", json=body, headers=USER).json()
    response = client.post(f"/api/v1/research/simulation/drafts/{saved['draftId']}/preview",
        json={"executionSnapshotId": "local-execution-fixture"}, headers=USER)
    assert response.status_code == 200, response.text
    live = response.json()
    assert live["status"] == "READY_FOR_REVIEW" and not live["confirmationAvailable"]
    assert client.post(f"/api/v1/plans/{live['planId']}/confirmation", headers=USER).status_code == 404
    assert state.con.execute("SELECT COUNT(*) FROM simulation_plan").fetchone()[0] == 0
    assert state.con.execute("SELECT COUNT(*) FROM fill").fetchone()[0] == 0


def test_saved_new_account_setting_cannot_replace_a_different_existing_deposit(setup):
    client, state, body = setup
    saved = client.post("/api/v1/research/simulation/draft", json=body, headers=USER).json()
    state.service._ensure_account(body["portfolioId"], initial_cash_cents=2_000_000,
                                  initial_lots=[], now=datetime.now().astimezone())
    response = client.post(f"/api/v1/research/simulation/drafts/{saved['draftId']}/preview",
        json={"executionSnapshotId": "local-execution-fixture"}, headers=USER)
    assert response.status_code == 409, response.text
    assert state.service._ledger_cash(body["portfolioId"]) == 2_000_000
    assert not state.previews


@pytest.mark.parametrize("stamp,expected", [("2026-09-14T09:29:59.999999+08:00", 200),
                                           ("2026-09-14T09:30:00+08:00", 422)])
def test_first_user_selection_requires_save_before_execution_open(setup, monkeypatch, stamp, expected):
    client, state, body = setup
    set_selection_clock(monkeypatch, stamp)
    response = client.post("/api/v1/research/simulation/draft", json=body, headers=USER)
    assert response.status_code == expected, response.text
    if expected == 200:
        assert datetime.fromisoformat(response.json()["createdAt"]) == datetime.fromisoformat(stamp)
    else:
        assert response.json()["error"]["code"] == "PIT_UNVERIFIED"
        assert state.con.execute("SELECT COUNT(*) FROM research_simulation_draft").fetchone()[0] == 0
    assert state.con.execute("SELECT COUNT(*) FROM portfolio WHERE portfolio_id=?", (body["portfolioId"],)).fetchone()[0] == 0
    assert not state.previews


def test_saved_preopen_choice_can_be_reviewed_after_execution_close(setup, monkeypatch):
    client, state, body = setup
    set_selection_clock(monkeypatch, "2026-09-14T09:29:59.999999+08:00")
    saved = client.post("/api/v1/research/simulation/draft", json=body, headers=USER).json()
    set_selection_clock(monkeypatch, "2026-09-14T20:30:00+08:00")
    response = client.post(f"/api/v1/research/simulation/drafts/{saved['draftId']}/preview",
        json={"executionSnapshotId": "local-execution-fixture"}, headers=USER)
    assert response.status_code == 200, response.text
    live = response.json()
    assert live["confirmationAvailable"] and live["createdAt"] == saved["createdAt"]
    persisted = state.con.execute("SELECT created_at FROM research_simulation_draft WHERE draft_id=?",
                                  (saved["draftId"],)).fetchone()[0]
    assert persisted == saved["createdAt"]
    assert state.con.execute("SELECT COUNT(*) FROM fill").fetchone()[0] == 0


def test_selection_crossing_open_while_checked_cannot_leave_account_or_live_preview(setup, monkeypatch):
    client, state, body = setup
    body["executionSnapshotId"] = "local-execution-fixture"
    set_selection_clock(monkeypatch, "2026-09-14T09:29:59.999999+08:00")
    import main
    original = main.load_simulation_research
    def checks_finish_after_open(path):
        result = original(path)
        set_selection_clock(monkeypatch, "2026-09-14T09:30:00+08:00")
        return result
    monkeypatch.setattr(main, "load_simulation_research", checks_finish_after_open)
    response = client.post("/api/v1/research/simulation/draft", json=body, headers=USER)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "PIT_UNVERIFIED"
    assert state.con.execute("SELECT COUNT(*) FROM research_simulation_draft").fetchone()[0] == 0
    assert state.con.execute("SELECT COUNT(*) FROM portfolio WHERE portfolio_id=?", (body["portfolioId"],)).fetchone()[0] == 0
    assert not state.previews and not state.preview_decisions


def test_repeated_saved_preview_freeze_reuses_one_persisted_plan_and_decision(setup):
    client, state, body = setup
    saved = client.post("/api/v1/research/simulation/draft", json=body, headers=USER).json()
    freezes = []
    previews = []
    for _ in range(2):
        response = client.post(f"/api/v1/research/simulation/drafts/{saved['draftId']}/preview",
            json={"executionSnapshotId": "local-execution-fixture"}, headers=USER)
        assert response.status_code == 200, response.text
        preview_id = response.json()["planId"]
        previews.append(preview_id)
        token = client.post(f"/api/v1/plans/{preview_id}/confirmation", headers=USER).json()["confirmationToken"]
        frozen = client.post(f"/api/v1/plans/{preview_id}/freeze", headers=USER,
            json={"plan_id": preview_id, "confirmation_token": token})
        assert frozen.status_code == 200, frozen.text
        freezes.append(frozen.json())
    assert previews[0] != previews[1]
    assert freezes[0]["plan_id"] == freezes[1]["plan_id"] == previews[0]
    assert freezes[1]["idempotent_replay"]
    assert freezes[0]["decision"]["decision_id"] == freezes[1]["decision"]["decision_id"]
    assert state.con.execute("SELECT COUNT(*) FROM simulation_plan").fetchone()[0] == 1
    assert state.con.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0] == 1
    assert state.con.execute("SELECT COUNT(*) FROM research_simulation_draft").fetchone()[0] == 1
    assert state.con.execute("SELECT COUNT(*) FROM fill").fetchone()[0] == 0


@pytest.mark.parametrize("case", ["ST_BUY", "UNKNOWN_BUY", "ST_SELL"])
def test_normal_s1_preview_never_authorizes_main_rules_for_new_unverified_execution_status(setup, monkeypatch, case):
    client, state, body = setup
    from aquant.domain.simulation.simulator import Lot
    original = state.reader.instruments
    def instruments(sid, *, as_of):
        rows = original(sid, as_of=as_of)
        if sid == "local-execution-fixture":
            for row in rows:
                if row["instrument_id"] == IIDS[1]:
                    if case == "UNKNOWN_BUY": row["simulation_eligibility"] = None
                    else: row["simulation_eligibility"]["is_st"] = True
        return rows
    monkeypatch.setattr(state.reader, "instruments", instruments)
    selected = [IIDS[1]]
    if case == "ST_SELL":
        selected = [IIDS[0]]
        lot = Lot("local-retained-st", IIDS[1], date(2026, 9, 7), date(2026, 9, 8), 100, 100, 1000)
        state.service._ensure_account(body["portfolioId"], initial_cash_cents=body["initialCashCents"],
            initial_lots=[lot], now=datetime.fromisoformat("2026-09-11T13:00:00+00:00"))
    response = client.post("/api/v1/plans/preview", json={
        "portfolio_id": body["portfolioId"], "snapshot_id": body["decisionSnapshotId"],
        "decision_snapshot_id": body["decisionSnapshotId"], "decision_cutoff_at": body["decisionCutoffAt"],
        "execution_snapshot_id": "local-execution-fixture", "trading_day": body["tradingDay"],
        "selected_instrument_ids": selected})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "DATA_NOT_READY"
    assert not state.previews and not state.preview_decisions
    assert state.con.execute("SELECT COUNT(*) FROM simulation_plan").fetchone()[0] == 0
    assert state.con.execute("SELECT COUNT(*) FROM fill").fetchone()[0] == 0


def test_normal_s1_preview_preserves_legacy_snapshot_without_status_field(setup, monkeypatch):
    client, state, body = setup
    original = state.reader.instruments
    def instruments(sid, *, as_of):
        rows = original(sid, as_of=as_of)
        for row in rows:
            row.pop("simulation_eligibility", None)
        return rows
    monkeypatch.setattr(state.reader, "instruments", instruments)
    response = client.post("/api/v1/plans/preview", json={
        "portfolio_id": body["portfolioId"], "snapshot_id": body["decisionSnapshotId"],
        "decision_snapshot_id": body["decisionSnapshotId"], "decision_cutoff_at": body["decisionCutoffAt"],
        "execution_snapshot_id": "local-execution-fixture", "trading_day": body["tradingDay"],
        "selected_instrument_ids": [IIDS[1]]})
    assert response.status_code == 200, response.text
    assert response.json()["orders"] and response.json()["planId"] in state.previews
