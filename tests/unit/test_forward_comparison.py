"""Local labelled fixtures for preregistration/append-only forward contracts.

These tests exercise production gates with in-memory fixtures. They are not
external-provider observations and cannot count as actual forward performance.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from aquant.domain.experiments.comparison import ComparisonError, VerifiedInvestigation
from aquant.domain.data.snapshot import DataMode, DatasetRef, SnapshotDraft
from aquant.domain.portfolio.construction import ConstructionParams
from aquant.domain.portfolio.plan import PlanError, PlanService
from aquant.domain.simulation.verified_fees import verified_fee_table
from aquant.operations.forward_comparison import (
    ForwardComparisonStore, execute_forward_initial, prepare_forward_comparison,
    record_forward_observation, track_active_forward,
)
from tests.golden.test_four_portfolio_comparison import IDS, world

UTC = timezone.utc
REGISTERED = datetime(2026, 9, 7, 13, tzinfo=UTC)
EXECUTED = datetime(2026, 9, 8, 13, tzinfo=UTC)


@pytest.fixture()
def forward_world(world, tmp_path, monkeypatch):
    con, service, _ = world
    original_ref = service.reader.ref
    monkeypatch.setattr(service.reader, "ref", lambda sid: replace(original_ref(sid), data_mode="PRODUCTION"))
    original_instruments = service.reader.instruments
    def instruments(sid, *, as_of):
        result = deepcopy(original_instruments(sid, as_of=as_of))
        day = sid.removeprefix("comparison-")
        for row in result:
            row["listed_on"] = "2000-01-01"
            row["simulation_eligibility"] = {"status": "VERIFIED", "as_of_date": day,
                "available_at": as_of.isoformat(), "observed_at": as_of.isoformat(),
                "is_st": False, "is_suspended": False,
                "sources": [{"source_id": "local-test-only", "receipt_id": "not-provider-evidence",
                             "content_hash": "sha256:" + "a" * 64}]}
        return result
    monkeypatch.setattr(service.reader, "instruments", instruments)
    original_calendar = service.reader.trading_calendar
    monkeypatch.setattr(service.reader, "trading_calendar", lambda sid, **kwargs:
        sorted(set(original_calendar(sid, **kwargs) + ["2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"])))
    service.params = ConstructionParams(max_holdings=2, initial_target_pct=Decimal("4"),
        exclude_listing_days=0, liquidity_min_avg_amount_cents=1)
    service.fee_table = verified_fee_table(commission_rate=Decimal("0.00025"),
        commission_min_cents=500, commission_source="USER_CONFIGURED")
    source = tmp_path / "research-local-fixture.json"
    source.write_text(json.dumps({"snapshotId": "local-forward-research", "dataAsOf": "2026-09-07",
        "generatedAt": "2026-09-07T12:25:00+00:00", "asOfTime": "2026-09-07T12:20:00+00:00",
        "capturedTo": "2026-09-07T12:20:00+00:00", "candidates": [
            {"instrumentId": iid, "displayName": "local fixture", "status": "RANKED",
             "exchange": "SSE", "board": "MAIN", "position": index + 1, "signalRank": 1 - index / 3,
             "quote": {"providerName": "local fixture"}, "evidence": []}
            for index, iid in enumerate(IDS)]}), encoding="utf-8")
    monkeypatch.setattr("aquant.operations.research_simulation.load_research_scan", lambda *a, **k: {})
    yield con, service, ForwardComparisonStore(tmp_path / "forward"), source


def prepare(forward_world, **kwargs):
    _, service, store, source = forward_world
    return prepare_forward_comparison(service, store=store, research_path=source,
        snapshot_id="comparison-2026-09-07", authorization_subject="user:authorized-test",
        now=kwargs.pop("now", REGISTERED), **kwargs)


def test_actual_preopen_spec_freezes_common_pool_s1_and_full_equal_b_without_user_h(forward_world):
    con, _, store, _ = forward_world
    spec = prepare(forward_world)
    assert spec["createdAt"] == REGISTERED.isoformat()
    assert spec["firstTradingDay"] == "2026-09-08"
    assert spec["commonPoolCount"] == 3
    assert len(spec["modelTargets"]) == 2
    assert spec["modelTargets"] == spec["eventTargets"]
    assert len(spec["benchmarkTargets"]) == 3
    assert len({row["weight_pct"] for row in spec["benchmarkTargets"]}) == 1
    assert spec["human"]["status"] == "UNAVAILABLE"
    assert spec["portfolios"]["H"]["portfolioId"] is None
    assert con.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM fill").fetchone()[0] == 0
    assert store.list_series(subject="user:different") == []
    assert store.list_series()[0]["observedSessionCount"] == 0


def test_registration_after_open_uses_next_actual_calendar_session_not_past_date(forward_world):
    spec = prepare(forward_world, now=datetime(2026, 9, 8, 2, tzinfo=UTC))
    assert spec["firstTradingDay"] == "2026-09-09"
    assert spec["createdAt"] == "2026-09-08T02:00:00+00:00"


def test_unpublished_future_inputs_are_rejected(forward_world):
    with pytest.raises(ComparisonError, match="published"):
        prepare(forward_world, now=datetime(2026, 9, 7, 12, tzinfo=UTC))


def test_preopen_verified_e1_blocks_only_new_buy_targets_and_keeps_same_pool(forward_world):
    base = prepare(forward_world)
    iid = base["modelTargets"][0]["instrument_id"]
    event = VerifiedInvestigation("local-event", iid, datetime(2026, 9, 7, 12, tzinfo=UTC),
        datetime(2026, 9, 7, 12, tzinfo=UTC), "https://example.invalid/local-fixture", "a" * 64,
        "local fixture paragraph 1")
    spec = prepare(forward_world, verified_events=(event,))
    assert spec["eventBuyBlockedInstrumentIds"] == [iid]
    assert iid not in {target["instrument_id"] for target in spec["eventTargets"]}
    assert iid in spec["commonPool"]
    assert spec["modelTargets"] == base["modelTargets"]


def test_spec_and_observations_are_append_only_and_original_research_is_frozen(forward_world):
    _, _, store, source = forward_world
    spec = prepare(forward_world)
    assert store.write_spec(spec) == spec
    original = source.read_bytes()
    source.write_text("changed", encoding="utf-8")
    assert open(spec["researchOriginPath"], "rb").read() == original
    with pytest.raises(ComparisonError):
        store.write_spec(dict(spec, createdAt="2026-09-08T13:00:00+00:00"))
    observation = {"seriesId": spec["seriesId"], "tradingDay": "2026-09-08", "snapshotId": "actual-local-fixture"}
    assert store.observe(observation) == store.observe(observation)
    with pytest.raises(ComparisonError, match="different content"):
        store.observe(dict(observation, snapshotId="other-local-fixture"))


def test_real_ledger_execution_has_no_human_tokens_and_daily_marks_do_not_rebalance(forward_world):
    con, service, store, _ = forward_world
    spec = prepare(forward_world)
    first = execute_forward_initial(service, store=store, series_id=spec["seriesId"],
        execution_snapshot_id="comparison-2026-09-08", authorization_subject="user:authorized-test", now=EXECUTED)
    assert set(first["portfolios"]) == set("MEHB")
    assert first["portfolios"]["H"]["status"] == "UNAVAILABLE"
    assert all(first["portfolios"][kind]["reconciled"] for kind in "MEB")
    for kind in "MEB":
        pid = spec["portfolios"][kind]["portfolioId"]
        assert pid.endswith("-" + kind.lower())
        assert con.execute("SELECT kind FROM portfolio WHERE portfolio_id=?", (pid,)).fetchone()[0] == kind
    assert first["portfolios"]["M"]["netValueCents"] == first["portfolios"]["E"]["netValueCents"]
    for kind in "MEB":
        execution = first["portfolios"][kind]["dayExecution"]
        assert execution["tradingDay"] == first["tradingDay"]
        assert execution["fillCount"] == first["portfolios"][kind]["fillCount"]
        assert execution["feesTotalCents"] == first["portfolios"][kind]["feesTotalCents"]
        assert execution["fills"]
        for fill in execution["fills"]:
            saved = dict(con.execute("SELECT * FROM fill WHERE fill_id=?", (fill["fill_id"],)).fetchone())
            assert (fill["quantity"], fill["price_cents"], fill["gross_amount_cents"], fill["filled_at"]) == (
                saved["quantity"], saved["price_cents"], saved["gross_amount_cents"], saved["filled_at"])
            assert sum(fee["amount_cents"] for fee in fill["feeCharges"]) == saved["fees_total_cents"]
    assert con.execute("SELECT COUNT(*) FROM plan_confirmation").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0] == 0
    assert all(row[0] is None for row in con.execute("SELECT confirmed_by FROM simulation_plan"))
    assert all(row[0] == "FORWARD_ENGINEERING_SIMULATION" for row in con.execute("SELECT source FROM engineering_plan_provenance"))
    fills = con.execute("SELECT COUNT(*) FROM fill").fetchone()[0]
    assert execute_forward_initial(service, store=store, series_id=spec["seriesId"],
        execution_snapshot_id="comparison-2026-09-08", authorization_subject="user:authorized-test",
        now=EXECUTED + timedelta(hours=1)) == first
    second = record_forward_observation(service, store=store, series_id=spec["seriesId"],
        snapshot_id="comparison-2026-09-09", now=datetime(2026, 9, 9, 13, tzinfo=UTC))
    assert second["tradingDay"] == "2026-09-09" and not second["missingEodDays"]
    assert len(store.list_series()[0]["observations"]) == 2
    assert con.execute("SELECT COUNT(*) FROM fill").fetchone()[0] == fills
    assert con.execute("SELECT COUNT(*) FROM simulation_plan").fetchone()[0] == 3
    for kind in "MEB":
        daily = second["portfolios"][kind]["dayExecution"]
        assert daily["orders"] == daily["fills"] == []
        assert daily["orderCount"] == daily["fillCount"] == daily["feesTotalCents"] == 0
        assert second["portfolios"][kind]["fillCount"] == first["portfolios"][kind]["fillCount"]
    assert store.observations(spec["seriesId"])[0] == first


def test_future_advance_does_not_fabricate_zero_returns_or_accounts(forward_world):
    con, service, store, _ = forward_world
    prepare(forward_world)
    result = track_active_forward(service, store=store, snapshot_id="comparison-2026-09-07", now=REGISTERED)
    assert result["results"][0]["status"] == "WAITING_EXECUTION_SNAPSHOT"
    assert con.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0] == 0
    assert store.list_series()[0]["observedSessionCount"] == 0


def test_advance_executes_frozen_initial_and_then_is_idempotent(forward_world):
    con, service, store, _ = forward_world
    prepare(forward_world)
    result = track_active_forward(service, store=store, snapshot_id="comparison-2026-09-08", now=EXECUTED)
    assert result["results"][0]["status"] == "OBSERVED", result
    fees = con.execute("SELECT SUM(amount_cents) FROM fee_charge").fetchone()[0]
    assert track_active_forward(service, store=store, snapshot_id="comparison-2026-09-08", now=EXECUTED) == result
    assert con.execute("SELECT SUM(amount_cents) FROM fee_charge").fetchone()[0] == fees


def test_prearrival_cannot_execute_and_source_change_cannot_rewrite_spec(forward_world):
    _, service, store, _ = forward_world
    spec = prepare(forward_world)
    with pytest.raises(ComparisonError, match="arrived"):
        execute_forward_initial(service, store=store, series_id=spec["seriesId"],
            execution_snapshot_id="comparison-2026-09-08", authorization_subject="user:authorized-test", now=REGISTERED)
    with open(spec["researchOriginPath"], "wb") as stream:
        stream.write(b"replaced")
    with pytest.raises(ComparisonError, match="origin hash"):
        execute_forward_initial(service, store=store, series_id=spec["seriesId"],
            execution_snapshot_id="comparison-2026-09-08", authorization_subject="user:authorized-test", now=EXECUTED)


def test_holding_missing_from_new_eod_blocks_mark_instead_of_fake_exit(forward_world, monkeypatch):
    con, service, store, _ = forward_world
    spec = prepare(forward_world)
    execute_forward_initial(service, store=store, series_id=spec["seriesId"],
        execution_snapshot_id="comparison-2026-09-08", authorization_subject="user:authorized-test", now=EXECUTED)
    original = service.reader.instruments
    monkeypatch.setattr(service.reader, "instruments", lambda sid, **kwargs:
        [] if sid == "comparison-2026-09-09" else original(sid, **kwargs))
    with pytest.raises(ComparisonError, match="missing an active"):
        record_forward_observation(service, store=store, series_id=spec["seriesId"],
            snapshot_id="comparison-2026-09-09", now=datetime(2026, 9, 9, 13, tzinfo=UTC))
    assert len(store.observations(spec["seriesId"])) == 1
    assert con.execute("SELECT COUNT(*) FROM valuation").fetchone()[0] == 3


@pytest.mark.parametrize("interrupted_after", [1, 2])
def test_interruption_after_executed_account_recovers_without_repeating_trades(forward_world, monkeypatch, interrupted_after):
    con, service, store, _ = forward_world
    spec = prepare(forward_world)
    original = PlanService.execute
    executions = 0
    def crash_after_commit(self, **kwargs):
        nonlocal executions
        result = original(self, **kwargs)
        executions += 1
        if executions == interrupted_after:
            raise RuntimeError("local simulated interruption after executed account")
        return result
    monkeypatch.setattr(PlanService, "execute", crash_after_commit)
    with pytest.raises(RuntimeError, match="interruption"):
        execute_forward_initial(service, store=store, series_id=spec["seriesId"],
            execution_snapshot_id="comparison-2026-09-08", authorization_subject="user:authorized-test", now=EXECUTED)
    assert con.execute("SELECT COUNT(*) FROM simulation_plan WHERE status='EXECUTED'").fetchone()[0] == interrupted_after
    assert not store.observations(spec["seriesId"])
    old_fills = {row["fill_id"]: dict(row) for row in con.execute("SELECT * FROM fill")}
    monkeypatch.setattr(PlanService, "execute", original)
    recovered = execute_forward_initial(service, store=store, series_id=spec["seriesId"],
        execution_snapshot_id="comparison-2026-09-08", authorization_subject="user:authorized-test",
        now=EXECUTED + timedelta(minutes=2))
    assert all(recovered["portfolios"][kind]["reconciled"] for kind in "MEB")
    assert con.execute("SELECT COUNT(*) FROM simulation_plan WHERE status='EXECUTED'").fetchone()[0] == 3
    assert con.execute("SELECT COUNT(*) FROM fill").fetchone()[0] == 7
    assert all(dict(con.execute("SELECT * FROM fill WHERE fill_id=?", (key,)).fetchone()) == row for key, row in old_fills.items())
    count = con.execute("SELECT COUNT(*) FROM cash_entry").fetchone()[0]
    assert execute_forward_initial(service, store=store, series_id=spec["seriesId"],
        execution_snapshot_id="comparison-2026-09-08", authorization_subject="user:authorized-test", now=EXECUTED + timedelta(minutes=3)) == recovered
    assert con.execute("SELECT COUNT(*) FROM cash_entry").fetchone()[0] == count


def test_same_day_engineering_execution_uses_frozen_decision_adv_not_recaptured_history(forward_world, monkeypatch):
    _, service, store, _ = forward_world
    spec = prepare(forward_world)
    original = service.reader.daily_quotes
    def revised_volume(sid, **kwargs):
        rows = original(sid, **kwargs)
        return [replace(row, volume_shares=1) if sid == "comparison-2026-09-08" and row.trading_day < date(2026, 9, 8)
                else row for row in rows]
    monkeypatch.setattr(service.reader, "daily_quotes", revised_volume)
    result = execute_forward_initial(service, store=store, series_id=spec["seriesId"],
        execution_snapshot_id="comparison-2026-09-08", authorization_subject="user:authorized-test", now=EXECUTED)
    assert result["delayedEngineeringCalculation"] is False
    assert sum(result["portfolios"][kind]["fillCount"] for kind in "MEB") == 7


def test_later_day_mark_with_next_midnight_cutoff_needs_no_new_plan_or_orders(forward_world):
    con, service, store, _ = forward_world
    spec = prepare(forward_world)
    execute_forward_initial(service, store=store, series_id=spec["seriesId"],
        execution_snapshot_id="comparison-2026-09-08", authorization_subject="user:authorized-test", now=EXECUTED)
    snapshot_store = service.reader.store
    old = snapshot_store.datasets("comparison-2026-09-09")
    cutoff = datetime(2026, 9, 10, 0, 15, tzinfo=UTC)
    snapshot_store.publish(SnapshotDraft(snapshot_id="local-late-market-sep09", kind="EOD", data_mode=DataMode.SYNTHETIC,
        input_cutoff_at=cutoff, as_of_time=cutoff, created_at=cutoff,
        published_at=cutoff + timedelta(minutes=1), code_version="local-contract-fixture",
        data_version="local-contract-fixture", watermark="LOCAL SYNTHETIC CONTRACT TEST ONLY",
        pool_hash="sha256:" + "a" * 64,
        datasets=[DatasetRef(name=row["name"], path=row["path"], sha256=row["sha256"], record_count=row["record_count"],
            as_of_upper_bound=datetime.fromisoformat(row["as_of_upper_bound"].replace("Z", "+00:00"))) for row in old]))
    orders = con.execute('SELECT COUNT(*) FROM "order"').fetchone()[0]
    fees = con.execute("SELECT SUM(amount_cents) FROM fee_charge").fetchone()[0]
    observed = record_forward_observation(service, store=store, series_id=spec["seriesId"],
        snapshot_id="local-late-market-sep09", now=cutoff + timedelta(minutes=2))
    assert observed["tradingDay"] == "2026-09-09"
    assert observed["cutoffAt"] == cutoff.isoformat()
    assert observed["delayedEngineeringCalculation"] is True
    assert all(observed["portfolios"][kind]["reconciled"] for kind in "MEB")
    assert all(observed["portfolios"][kind]["executionPlanId"] is None for kind in "MEB")
    assert con.execute('SELECT COUNT(*) FROM "order"').fetchone()[0] == orders
    assert con.execute("SELECT SUM(amount_cents) FROM fee_charge").fetchone()[0] == fees
    assert con.execute("SELECT COUNT(*) FROM simulation_plan").fetchone()[0] == 3
