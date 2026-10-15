"""M/E/H/B integration: real domain ledgers on labelled synthetic snapshots."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.data.ingest import SnapshotBuilder
from aquant.domain.data.reader import SnapshotReader
from aquant.domain.data.snapshot import DataMode, DatasetRef, SnapshotDraft, SnapshotStore
from aquant.domain.experiments.comparison import (
    ComparisonError, ComparisonRunner, ComparisonRunSpec, ComparisonStore, ReplayStep,
    VerifiedInvestigation, active_e1_events, equal_weight_targets,
)
from aquant.domain.portfolio.construction import Candidate, ConstructionParams, TargetWeight
from aquant.domain.portfolio.plan import PlanError, PlanService
from aquant.domain.simulation.fees import synthetic_fee_table
from aquant.domain.simulation.simulator import BoardRule, Lot
from tests.integration.test_m1_ingest_e2e import EXAMPLE

IDS = ("SYN.C.001", "SYN.C.002", "SYN.C.003")
UTC = timezone.utc
NOW = datetime(2026, 9, 10, 14, tzinfo=UTC)


@pytest.fixture()
def world(tmp_path, request):
    con = connect(tmp_path / "meta.sqlite")
    apply_migrations(con)
    root = tmp_path / "api"
    root.mkdir()
    store = SnapshotStore(con, root)
    builder = SnapshotBuilder(con, root / "datasets")
    original = builder.load_manifest(EXAMPLE)
    builder.ensure_source("comparison-synthetic", display_name="comparison labelled synthetic",
                          domains=["DAILY_QUOTES", "CALENDAR_IDENTITY"])
    days = [date(2026, 5, 1) + timedelta(days=index) for index in range(134)]
    days = [day for day in days if day.weekday() < 5]
    snapshot_days = (date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9))
    if getattr(request, "param", None) == "gap":
        snapshot_days += (date(2026, 9, 10),)
    for last_day in snapshot_days:
        sid = f"comparison-{last_day}"
        point = datetime.combine(last_day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12, minutes=30)
        doc = deepcopy(original)
        doc.update(snapshot_id=sid, as_of_time=point.isoformat(), input_cutoff_at=point.isoformat(),
                   published_at=(point + timedelta(minutes=1)).isoformat())
        doc["trading_days"] = [day.isoformat() for day in days if day <= last_day]
        doc["instruments"] = []
        for index, iid in enumerate(IDS):
            row = deepcopy(original["instruments"][0])
            row.update(instrument_id=iid, industry_code=f"IND{index}", status="LISTED", status_history=[])
            doc["instruments"].append(row)
        doc["daily_quotes"] = []
        for index, iid in enumerate(IDS):
            for pos, day in enumerate(days):
                if day > last_day:
                    continue
                price = (10000 + pos * 10 if index == 0 else
                         11000 - pos * 5 if index == 1 else 8000 + pos * 15)
                volume = 200_000_000 if day == date(2026, 9, 9) else 10_000_000
                doc["daily_quotes"].append({"instrument_id": iid, "trading_day": day.isoformat(),
                    "open_cents": price, "high_cents": price + 30, "low_cents": price - 30,
                    "close_cents": price + 5, "adjusted_close_cents": price + 5,
                    "prev_close_cents": price - 5, "volume_shares": volume,
                    "amount_cents": price * volume})
        doc["events"] = []
        doc["corporate_actions"] = []
        if getattr(request, "param", None) in {"receivable", "settled"}:
            doc["corporate_actions"] = [{"action_id": "comparison-test-dividend",
                "instrument_id": IDS[0], "action_type": "CASH_DIVIDEND", "supported": True,
                "record_date": "2026-09-08", "ex_date": "2026-09-09",
                "pay_date": "2026-09-10" if request.param == "receivable" else "2026-09-09",
                "cash_per_share_micros": 1_000_000, "tax_treatment": "PRE_TAX"}]
        builder.ingest(doc, source_id="comparison-synthetic", data_version="comparison-fixture-v1")
        datasets = builder.write_datasets(doc, snapshot_id=sid)
        store.publish(SnapshotDraft(snapshot_id=sid, kind="EOD", data_mode=DataMode.SYNTHETIC,
            input_cutoff_at=point, as_of_time=point, created_at=point,
            published_at=point + timedelta(minutes=1), code_version="comparison-test",
            data_version="comparison-fixture-v1", watermark="SYNTHETIC - NOT RESEARCH EVIDENCE",
            pool_hash="sha256:" + "a" * 64,
            datasets=[DatasetRef(name=row["name"], path=row["path"], sha256=row["sha256"],
                record_count=row["record_count"], as_of_upper_bound=datetime.fromisoformat(row["as_of_upper_bound"].replace("Z", "+00:00")))
                for row in datasets]))
    params = ConstructionParams(max_holdings=2, max_single_name_pct=Decimal("40"),
                                max_single_industry_pct=Decimal("60"), exclude_listing_days=0)
    service = PlanService(con, SnapshotReader(store), synthetic_fee_table(),
        [BoardRule("SSE", "MAIN", Decimal("10"), 100, date(2020, 1, 1))],
        {iid: ("SSE", "MAIN") for iid in IDS}, params)
    yield con, service, ComparisonStore(tmp_path / "results")
    con.close()


def spec(**changes):
    base = ComparisonRunSpec(experiment_version="comparison-test-v1", hypothesis="test deterministic comparison",
        steps=(ReplayStep(date(2026, 9, 8), "comparison-2026-09-07", "comparison-2026-09-08"),
               ReplayStep(date(2026, 9, 9), "comparison-2026-09-08", "comparison-2026-09-09")),
        universe=IDS, human_selection=(IDS[1],), human_origin="ENGINEERING_REPLAY")
    return replace(base, **changes)


def test_four_accounts_reconcile_and_replay_preserves_identical_result(world):
    con, service, store = world
    runner = ComparisonRunner(service, store)
    result = runner.run(spec(), authorization_subject="user:authorized-test", now=NOW)
    assert result["status"] == "COMPLETED", result.get("error", result)
    assert set(result["portfolios"]) == set("MEHB")
    assert result["portfolios"]["M"]["final_net_value_cents"] == result["portfolios"]["E"]["final_net_value_cents"]
    assert result["event_shadow"]["rule_triggered"] is False
    # Benchmark is the entire pool, even though model max_holdings is two.
    assert result["portfolio_runs"]["B"]["days"][0]["target_count"] == 3
    assert len(result["portfolios"]["B"]["series"][0]["positions"]) == 3
    for run in result["portfolio_runs"].values():
        assert all(day["reconciled"] for day in run["days"])
    difference = result["human_difference"]
    assert difference["causal_selection_effect"] is None
    assert difference["causal_cash_effect"] is None
    assert difference["accounting_contribution_differences_reconciled"] is True
    assert sum(row["net_pnl_cents_difference"] for row in difference["security_accounting_contribution_differences"]) == difference["net_wealth_difference_cents"]
    assert difference["net_wealth_difference_cents"] == (difference["direct_fee_debit_difference_cents"]
                                                        + difference["remaining_wealth_difference_cents"])
    count = con.execute("SELECT COUNT(*) FROM fill").fetchone()[0]
    assert runner.run(spec(), authorization_subject="user:authorized-test", now=NOW + timedelta(hours=1)) == result
    assert con.execute("SELECT COUNT(*) FROM fill").fetchone()[0] == count
    assert "SYNTHETIC" in result["preregistration"]["snapshots"][0]["decision"]["watermark"]


def investigation(iid=IDS[2], at=datetime(2026, 9, 7, 8, tzinfo=UTC)):
    return VerifiedInvestigation("event-test", iid, at, at, "https://example.invalid/test",
                                 "a" * 64, "synthetic test paragraph 1")


def test_event_shadow_removes_only_buys_and_keeps_cash(world):
    _con, service, store = world
    events = tuple(replace(investigation(iid), event_id=f"event-{iid}") for iid in IDS)
    result = ComparisonRunner(service, store).run(spec(verified_events=events),
        authorization_subject="user:authorized-test", now=NOW)
    assert result["status"] == "COMPLETED", result.get("error", result)
    assert result["event_shadow"]["rule_triggered"] is True
    assert all(not day["positions"] for day in result["portfolios"]["E"]["series"])
    assert Decimal(result["portfolios"]["E"]["series"][0]["cash_pct"]) > Decimal(result["portfolios"]["M"]["series"][0]["cash_pct"])


def test_e1_counts_sessions_and_ignores_unavailable_verification():
    days = [date(2026, 9, 7) + timedelta(days=i) for i in range(19)]
    days = [day for day in days if day.weekday() < 5]
    event = investigation(at=datetime(2026, 9, 7, 9, tzinfo=UTC))  # after local close
    assert active_e1_events(events=(event,), calendar=days, decision_cutoff=NOW,
                            execution_day=date(2026, 9, 8), window=10) == [event]
    assert active_e1_events(events=(event,), calendar=days, decision_cutoff=NOW,
                            execution_day=date(2026, 9, 22), window=10) == []
    late = replace(event, verified_at=NOW + timedelta(days=1))
    assert active_e1_events(events=(late,), calendar=days, decision_cutoff=NOW,
                            execution_day=date(2026, 9, 9), window=10) == []


def test_equal_weight_risk_scaling_is_uniform_and_never_drops_members():
    candidates = [Candidate(str(i), "I" if i < 3 else "J", 1) for i in range(4)]
    params = ConstructionParams(max_single_name_pct=Decimal("20"), max_single_industry_pct=Decimal("30"))
    targets, budget = equal_weight_targets(candidates, equity_pct=Decimal("80"), params=params, label="test")
    assert len(targets) == 4 and {t.weight_pct for t in targets} == {Decimal("10")}
    assert Decimal(budget["allocated_equity_pct"]) == Decimal("40")
    assert budget["risk_scaled"] is True


@pytest.mark.parametrize("target", [
    TargetWeight(IDS[0], "SPOOFED", Decimal("20"), "test"),
    TargetWeight(IDS[0], "IND0", Decimal("41"), "test"),
    TargetWeight("ABSENT", "IND0", Decimal("20"), "test"),
    TargetWeight(IDS[0], "IND0", Decimal("NaN"), "test"),
])
def test_custom_targets_cannot_evade_pool_industry_or_risk(world, target):
    _con, service, _store = world
    with pytest.raises(PlanError, match="explicit target"):
        service.preview(portfolio_id="test", snapshot_id="comparison-2026-09-07",
            trading_day=date(2026, 9, 8), as_of=datetime(2026, 9, 7, 12, 30, tzinfo=UTC),
            decision_snapshot_id="comparison-2026-09-07",
            execution_snapshot_id="comparison-2026-09-08", candidates=[Candidate(IDS[0], "IND0", 1)],
            cash_available_cents=100_000_000, lots=[], confirm_subject="user:test", target_weights=[target])


def test_store_cannot_overwrite_old_results_and_real_h_needs_decision_refs(tmp_path):
    store = ComparisonStore(tmp_path)
    store.write_once("cmp-test.result.json", {"net_return": "-1"})
    with pytest.raises(ComparisonError, match="immutable"):
        store.write_once("cmp-test.result.json", {"net_return": "2"})
    with pytest.raises(ComparisonError, match="decision reference"):
        spec(human_origin="USER_CONFIRMED")


def test_failed_runs_are_saved_without_return_metrics(world):
    _con, service, store = world
    service.params = replace(service.params, liquidity_min_avg_amount_cents=10**30)
    result = ComparisonRunner(service, store).run(spec(), authorization_subject="user:test", now=NOW)
    assert result["status"] == "FAILED"
    assert "portfolios" not in result
    assert store.read(result["run_id"]) == result


@pytest.mark.parametrize("world", ["receivable", "settled"], indirect=True)
def test_security_contributions_include_dividend_cash_or_receivable_exactly_once(world):
    _con, service, store = world
    result = ComparisonRunner(service, store).run(spec(), authorization_subject="user:test", now=NOW)
    assert result["status"] == "COMPLETED", result.get("error")
    model = result["portfolios"]["M"]
    accounting = model["accounting_contributions"]
    contribution = next(row for row in accounting["securities"] if row["instrument_id"] == IDS[0])
    assert contribution["corporate_action_pnl_cents"] > 0
    assert contribution["corporate_action_pnl_cents"] == (contribution["dividend_cash_flow_cents"]
                                                         + contribution["open_receivables_cents"])
    assert accounting["unallocated_residual_cents"] == 0
    assert accounting["allocated_net_pnl_cents"] == model["final_net_value_cents"] - model["initial_cash_cents"]


def test_event_pause_never_adds_a_forced_sell(world):
    _con, service, _store = world
    lots = [Lot("old-position", IDS[2], date(2026, 9, 7), date(2026, 9, 8), 1000, 1000, 8000)]
    kwargs = dict(portfolio_id="test", snapshot_id="comparison-2026-09-09", trading_day=date(2026, 9, 9),
        as_of=datetime(2026, 9, 9, 12, 30, tzinfo=UTC), candidates=[Candidate(IDS[0], "IND0", 1), Candidate(IDS[1], "IND1", 0)],
        cash_available_cents=100_000_000, lots=lots, confirm_subject="user:test")
    model = service.preview(**kwargs)
    event = service.preview(**kwargs, buy_blocked_instruments={IDS[0], IDS[2]})
    assert [order for order in event.orders if order["side"] == "SELL"] == [order for order in model.orders if order["side"] == "SELL"]
    assert any(order["instrument_id"] == IDS[2] and order["side"] == "SELL" for order in model.orders)
    assert not any(order["instrument_id"] == IDS[0] and order["side"] == "BUY" for order in event.orders)


def test_small_capital_reports_targets_below_integer_lots_without_fake_fills(world):
    _con, service, store = world
    result = ComparisonRunner(service, store).run(spec(initial_cash_cents=100_000), authorization_subject="user:test", now=NOW)
    assert result["status"] == "COMPLETED", result.get("error")
    assert result["portfolio_runs"]["B"]["days"][0]["below_minimum_lot_target_count"] == 3
    assert result["portfolios"]["B"]["fill_count"] == 0
    assert result["portfolios"]["B"]["final_net_value_cents"] == 100_000


def test_historical_replay_cannot_be_relabeled_as_forward_record(world):
    _con, service, store = world
    result = ComparisonRunner(service, store).run(spec(run_kind="FORWARD_SIMULATION"), authorization_subject="user:test", now=NOW)
    assert result["status"] == "FAILED"
    assert "relabelling" in result["error"]["message"]
    assert "portfolios" not in result


def test_engineering_replay_cannot_claim_real_human_choices_from_reference_strings(world):
    con, service, store = world
    result = ComparisonRunner(service, store).run(spec(human_origin="USER_CONFIRMED",
        human_decision_refs=("unverified-decision-one", "unverified-decision-two")),
        authorization_subject="user:test", now=NOW)
    assert result["status"] == "FAILED"
    assert "actual H decisions" in result["error"]["message"]
    assert con.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0] == 0


def test_evaluation_metrics_use_ledger_capital_and_equity_denominators(world):
    con, service, store = world
    result = ComparisonRunner(service, store).run(spec(), authorization_subject="user:test", now=NOW)
    assert result["status"] == "COMPLETED", result.get("error")
    for portfolio in result["portfolios"].values():
        evaluation = portfolio["evaluation"]
        last = portfolio["series"][-1]
        equity = sum(row["value_cents"] for row in last["positions"])
        assert Decimal(evaluation["market_exposure"]["final_equity_pct"]) == Decimal(equity) / last["net_value_cents"] * 100
        by_industry = {}
        for position in last["positions"]:
            industry = f"IND{IDS.index(position['instrument_id'])}"
            by_industry[industry] = by_industry.get(industry, 0) + position["value_cents"]
        concentration = evaluation["industry_concentration"]
        assert Decimal(concentration["final_largest_industry_pct_equity"]) == Decimal(max(by_industry.values())) / equity * 100
        assert Decimal(concentration["final_hhi_equity"]) == sum((Decimal(value) / equity) ** 2 for value in by_industry.values())
        actual_gross = con.execute("SELECT SUM(quantity*price_cents) FROM fill WHERE portfolio_id=?", (portfolio["portfolio_id"],)).fetchone()[0]
        assert evaluation["turnover"]["gross_traded_cents"] == actual_gross
        mean_assets = Decimal(sum(day["net_value_cents"] for day in portfolio["series"])) / len(portfolio["series"])
        assert Decimal(evaluation["turnover"]["gross_pct_mean_observed_assets"]) == Decimal(actual_gross) / mean_assets * 100
        difference = portfolio["benchmark_difference"]
        assert difference["net_wealth_difference_cents"] == portfolio["final_net_value_cents"] - result["portfolios"]["B"]["final_net_value_cents"]
        assert difference["broad_market_difference_pct_points"] is None
        assert evaluation["volatility"]["annualized_pct"] is None
        assert "20" in evaluation["volatility"]["annualized_unavailable_reason"]
        assert evaluation["volatility"]["sharpe"] is None


def test_unfilled_and_capacity_proxies_report_rejections_without_future_volume(world):
    _con, service, store = world
    result = ComparisonRunner(service, store).run(spec(initial_cash_cents=1_000_000_000), authorization_subject="user:test", now=NOW)
    assert result["status"] == "COMPLETED", result.get("error")
    benchmark = result["portfolios"]["B"]
    evaluation = benchmark["evaluation"]
    assert evaluation["unfilled"]["unfilled_order_pct"] == "100"
    assert evaluation["unfilled"]["unfilled_quantity_pct"] == "100"
    assert evaluation["unfilled"]["reasons"]["VOLUME_CAP_EXCEEDED"] == benchmark["order_count"]
    capacity = evaluation["capacity_proxy"]
    assert capacity["status"] == "AVAILABLE"
    assert all(row["prior_adv_shares"] == 10_000_000 for row in capacity["samples"])
    assert Decimal(capacity["max_requested_participation_pct"]) > Decimal("0.1")
    assert capacity["max_filled_participation_pct"] == "0"
    assert capacity["capital_capacity_cents"] is None


def test_no_orders_or_equity_yields_unavailable_rates_not_zero_fills(world):
    _con, service, store = world
    events = tuple(replace(investigation(iid), event_id=f"event-{iid}") for iid in IDS)
    result = ComparisonRunner(service, store).run(spec(verified_events=events), authorization_subject="user:test", now=NOW)
    assert result["status"] == "COMPLETED", result.get("error")
    evaluation = result["portfolios"]["E"]["evaluation"]
    assert evaluation["unfilled"]["order_count"] == 0
    assert evaluation["unfilled"]["unfilled_order_pct"] is None
    assert evaluation["unfilled"]["unfilled_quantity_pct"] is None
    assert evaluation["industry_concentration"]["final_hhi_equity"] is None
    assert evaluation["capacity_proxy"]["status"] == "UNAVAILABLE"


@pytest.mark.parametrize("world", ["gap"], indirect=True)
def test_missing_eod_only_has_interval_volatility_and_no_daily_annualization(world):
    _con, service, store = world
    gap_spec = spec(steps=(ReplayStep(date(2026, 9, 8), "comparison-2026-09-07", "comparison-2026-09-08"),
                          ReplayStep(date(2026, 9, 10), "comparison-2026-09-09", "comparison-2026-09-10")))
    result = ComparisonRunner(service, store).run(gap_spec, authorization_subject="user:test", now=NOW)
    assert result["status"] == "COMPLETED", result.get("error")
    assert result["observation_basis"]["missing_eod_days"] == ["2026-09-09"]
    for portfolio in result["portfolios"].values():
        volatility = portfolio["evaluation"]["volatility"]
        assert volatility["daily_intervals"] is False
        assert volatility["observed_interval_sample_std_pct"] is not None
        assert volatility["annualized_pct"] is None
        assert "缺少交易日" in volatility["annualized_unavailable_reason"]
