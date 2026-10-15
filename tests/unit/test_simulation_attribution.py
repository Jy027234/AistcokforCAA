"""Dated accounting-review contracts using local labelled fixtures only."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from aquant.domain.experiments.comparison import ComparisonError, ComparisonRunner
from aquant.operations.forward_comparison import execute_forward_initial, record_forward_observation, track_active_forward, _service_for_spec
from aquant.operations.simulation_attribution import AttributionStore, save_engineering_attribution, save_forward_attribution
from tests.unit.test_forward_comparison import forward_world, world, prepare, EXECUTED
from tests.unit.test_engineering_simulation import setup, run


def test_engineering_accounting_bridge_matches_published_ledger_without_writing_original(setup):
    service, root, _ = setup
    original = run(setup)
    saved_row = dict(service.con.execute("SELECT * FROM engineering_simulation_run").fetchone())
    changes = service.con.total_changes
    store = AttributionStore(root / "simulation-attribution")
    report = save_engineering_attribution(service, store=store, run=original)
    portfolio = report["portfolios"]["ENGINEERING_SELECTION"]
    assert portfolio["netValueCents"] == original["valuation"]["net_value_cents"]
    assert portfolio["accounting"]["unallocated_residual_cents"] == 0
    assert sum(row["net_pnl_cents"] for row in portfolio["accounting"]["securities"]) == portfolio["accounting"]["total_net_pnl_cents"]
    assert sum(row["net_pnl_cents"] for row in portfolio["accounting"]["industries"]) == portfolio["accounting"]["total_net_pnl_cents"]
    assert portfolio["cashBridge"]["finalCashCents"] == original["cashCents"]
    assert portfolio["cashBridge"]["unallocatedResidualCents"] == 0
    assert portfolio["execution"]["orderCount"] == 2
    assert portfolio["execution"]["unfilledOrderCount"] == 0
    assert portfolio["originalProposal"]["selectionReason"] is None
    assert not report["comparisons"] and report["causalAttribution"]["selection"] is None
    assert save_engineering_attribution(service, store=store, run=original) == report
    assert service.con.total_changes == changes
    assert dict(service.con.execute("SELECT * FROM engineering_simulation_run").fetchone()) == saved_row
    assert store.list_reports(subject="user:other") == []


def test_suspended_order_is_unfilled_and_never_called_a_minimum_lot_omission(setup):
    service, root, _ = setup
    service.reader.suspension = True
    result = run(setup)
    report = save_engineering_attribution(service, store=AttributionStore(root / "reports"), run=result)
    execution = report["portfolios"]["ENGINEERING_SELECTION"]["execution"]
    assert execution["unfilledOrderCount"] == 1
    assert execution["unfilledOrderPct"] == "50.0"
    assert execution["belowMinimumLotCount"] == 0
    assert execution["noOrderTargets"] == []


def test_forward_existing_eod_saves_differences_and_future_eod_has_no_report(forward_world):
    con, service, forward_store, _ = forward_world
    spec = prepare(forward_world)
    attribution_store = AttributionStore(forward_store.root.parent / "simulation-attribution")
    assert attribution_store.list_reports() == []
    observation = execute_forward_initial(service, store=forward_store, series_id=spec["seriesId"],
        execution_snapshot_id="comparison-2026-09-08", authorization_subject="user:authorized-test", now=EXECUTED)
    report = attribution_store.read(spec["seriesId"], observation["tradingDay"])
    assert report is not None and report["human"]["status"] == "UNAVAILABLE"
    assert set(report["comparisons"]) == {"M_MINUS_B", "E_MINUS_M"}
    difference = report["comparisons"]["M_MINUS_B"]
    assert difference["net_wealth_difference_cents"] == report["portfolios"]["M"]["netValueCents"] - report["portfolios"]["B"]["netValueCents"]
    assert sum(row["net_pnl_cents_difference"] for row in difference["security_accounting_contribution_differences"]) == difference["net_wealth_difference_cents"]
    assert report["comparisons"]["E_MINUS_M"]["net_wealth_difference_cents"] == 0
    assert report["comparisons"]["E_MINUS_M"]["causal_selection_effect"] is None
    assert forward_store.list_series()[0]["observations"][0]["attribution"] == report
    assert attribution_store.read(spec["seriesId"], "2026-09-09") is None
    assert save_forward_attribution(service, store=attribution_store, spec=spec, observation=observation) == report


@pytest.mark.parametrize("world", ["receivable"], indirect=True)
def test_later_dividend_settlement_cannot_change_prior_day_open_receivable_attribution(forward_world):
    _, service, forward_store, _ = forward_world
    spec = prepare(forward_world)
    execute_forward_initial(service, store=forward_store, series_id=spec["seriesId"],
        execution_snapshot_id="comparison-2026-09-08", authorization_subject="user:authorized-test", now=EXECUTED)
    second = record_forward_observation(service, store=forward_store, series_id=spec["seriesId"],
        snapshot_id="comparison-2026-09-09", now=datetime(2026, 9, 9, 13, tzinfo=timezone.utc))
    store = AttributionStore(forward_store.root.parent / "simulation-attribution")
    old = store.read(spec["seriesId"], "2026-09-09")
    actions = ComparisonRunner._cash_dividends(service.reader.corporate_actions("comparison-2026-09-09", as_of=service.reader.ref("comparison-2026-09-09").as_of_time))
    for kind in "MEB":
        scoped = _service_for_spec(service, spec, kind)
        pid = spec["portfolios"][kind]["portfolioId"]
        scoped._advance_corporate_actions(portfolio_id=pid, trading_day=date(2026, 9, 10),
            actions=actions, lots=scoped._load_lots(pid), now=datetime(2026, 9, 10, 13, tzinfo=timezone.utc))
    assert save_forward_attribution(service, store=store, spec=spec, observation=second) == old
    assert any(row["netAssetBridge"]["corporateActionPnlCents"] > 0 for row in old["portfolios"].values())


def test_attribution_immutable_result_cannot_be_replaced_with_another_value(setup):
    service, root, _ = setup
    result = run(setup)
    store = AttributionStore(root / "reports")
    original = save_engineering_attribution(service, store=store, run=result)
    changed = {key: value for key, value in original.items() if key != "contentHash"}
    changed["snapshotId"] = "another-snapshot"
    with pytest.raises(ComparisonError, match="different dated evidence"):
        store.save(changed)
    with pytest.raises(ComparisonError, match="original result"):
        save_engineering_attribution(service, store=store, run=dict(result, initialCashCents=1))


@pytest.mark.parametrize("retry_snapshot", ["comparison-2026-09-08", "comparison-2026-09-09"])
def test_failed_sidecar_write_after_saved_eod_recovers_without_replaying_ledger_or_eod(forward_world, monkeypatch, retry_snapshot):
    con, service, forward_store, _ = forward_world
    spec = prepare(forward_world)
    from aquant.operations import simulation_attribution
    original_save = simulation_attribution.save_forward_attribution
    def failed_write(*args, **kwargs):
        raise OSError("local simulated review storage unavailable")
    monkeypatch.setattr(simulation_attribution, "save_forward_attribution", failed_write)
    failed = track_active_forward(service, store=forward_store, snapshot_id="comparison-2026-09-08", now=EXECUTED)
    assert failed["results"][0]["status"] == "DATA_NOT_READY"
    assert len(forward_store.observations(spec["seriesId"])) == 1
    original_observation = forward_store.observations(spec["seriesId"])[0]
    counts = {table: con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in ("fill", "cash_entry", "fee_charge", "valuation", "valuation_position")}
    store = AttributionStore(forward_store.root.parent / "simulation-attribution")
    assert store.list_reports() == []
    monkeypatch.setattr(simulation_attribution, "save_forward_attribution", original_save)
    retry_time = EXECUTED + (timedelta(minutes=2) if retry_snapshot.endswith("08") else timedelta(days=1))
    recovered = track_active_forward(service, store=forward_store, snapshot_id=retry_snapshot, now=retry_time)
    assert recovered["results"][0]["status"] == "OBSERVED"
    assert store.read(spec["seriesId"], "2026-09-08") is not None
    assert forward_store.observations(spec["seriesId"])[0] == original_observation
    assert {table: con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in ("fill", "cash_entry", "fee_charge")} == {
        table: counts[table] for table in ("fill", "cash_entry", "fee_charge")}
    if retry_snapshot.endswith("08"):
        assert {table: con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in counts} == counts
    else:
        assert store.read(spec["seriesId"], "2026-09-09") is not None
        assert len(forward_store.observations(spec["seriesId"])) == 2
        assert con.execute('SELECT COUNT(*) FROM valuation').fetchone()[0] == counts['valuation'] + 3
