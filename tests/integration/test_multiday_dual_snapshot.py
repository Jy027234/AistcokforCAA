"""Deterministic P0 acceptance for two-day dual-snapshot recovery.

The test uses the same runner as the command-line acceptance tool.  It is
intentionally local and synthetic so a normal test run never depends on a
network, a running API process, or real-time market waiting.
"""

from __future__ import annotations

from tools.check_multiday_dual_snapshot import run_acceptance


def test_two_consecutive_days_reopen_and_keep_snapshot_identity(tmp_path):
    report = run_acceptance(tmp_path / "dual-snapshot-work")

    assert report["conclusion"] == "PASS", report
    assert report["evidence_scope"] == "SYNTHETIC_DETERMINISTIC_DOMAIN_TEST"
    assert report["production_evidence"] is False
    assert report["network_used"] is False
    assert report["wall_clock_market_wait"] is False
    assert report["process_reopens"] >= 4
    assert [day["trading_day"] for day in report["days"]] == [
        "2026-09-09", "2026-09-10",
    ]

    first, second = report["days"]
    assert first["decision_snapshot_id"] != first["execution_snapshot_id"]
    assert second["decision_snapshot_id"] == first["execution_snapshot_id"]
    assert second["execution_snapshot_id"] != second["decision_snapshot_id"]

    for day in report["days"]:
        assert day["frozen"]["decision_snapshot_id"] == day["decision_snapshot_id"]
        assert day["frozen"]["execution_snapshot_id"] == day["execution_snapshot_id"]
        assert day["execute"]["execution_snapshot_id"] == day["execution_snapshot_id"]
        assert day["valuation"]["published"] is True
        assert day["reconcile"]["reconciled"] is True
        identity = day["valuation"]["identity"]
        assert identity["requested_snapshot_id"] == day["execution_snapshot_id"]
        assert identity["requested_execution_plan_id"] == day["plan_id"]
        assert identity["persisted_snapshot_id"] == day["execution_snapshot_id"]
        assert identity["persisted_execution_plan_id"] == day["plan_id"]
        assert identity["persisted_as_of"] == day["execution_cutoff_at"]
        assert identity["mode"] == "persisted_sidecar"
        assert identity["ok"] is True
