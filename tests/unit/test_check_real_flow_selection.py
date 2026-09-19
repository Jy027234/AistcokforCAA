"""check_real_flow 的双快照选择逻辑（不启动 API、不联网）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

from check_real_flow import select_snapshot_pair  # noqa: E402
from aquant.domain.data.snapshot import (  # noqa: E402
    PublishedSnapshot,
    SnapshotCapability,
)


def _snapshot(snapshot_id: str, day: str, *, ready: bool = True,
              mode: str = "PRODUCTION", kind: str = "EOD") -> PublishedSnapshot:
    stamp = f"{day}T07:00:00+00:00"
    return PublishedSnapshot(
        snapshot_id=snapshot_id,
        kind=kind,
        data_mode=mode,
        as_of_time=stamp,
        input_cutoff_at=stamp,
        published_at=f"{day}T08:00:00+00:00",
        quality_status="OK",
        trading_day=day,
        dataset_summary=(),
        s1_decision=SnapshotCapability(
            available=ready,
            code="READY" if ready else "ADJUSTED_CLOSE_INCOMPLETE",
            message="ready" if ready else "missing adjusted close",
        ),
    )


def test_current_pointer_is_execution_and_nearest_ready_prior_is_decision():
    snapshots = [
        _snapshot("s-old", "2026-09-10"),
        _snapshot("s-near-but-broken", "2026-09-14", ready=False),
        _snapshot("s-current", "2026-09-18"),
        _snapshot("s-later", "2026-09-19"),
    ]

    pair = select_snapshot_pair(snapshots, current_snapshot_id="s-current")

    assert pair.execution.snapshot_id == "s-current"
    assert pair.decision.snapshot_id == "s-old"


def test_stale_current_pointer_falls_back_to_latest_published():
    snapshots = [
        _snapshot("s-old", "2026-09-17"),
        _snapshot("s-latest", "2026-09-18"),
    ]

    pair = select_snapshot_pair(snapshots, current_snapshot_id="s-gone")

    assert pair.execution.snapshot_id == "s-latest"
    assert pair.decision.snapshot_id == "s-old"


def test_without_current_uses_latest_and_requires_same_data_mode():
    snapshots = [
        _snapshot("s-production-old", "2026-09-17"),
        _snapshot("s-production-new", "2026-09-18"),
        _snapshot("s-synthetic-old", "2026-09-19", mode="SYNTHETIC"),
        _snapshot("s-synthetic-later", "2026-09-20", mode="SYNTHETIC"),
    ]

    pair = select_snapshot_pair(snapshots)

    assert pair.execution.snapshot_id == "s-synthetic-later"
    assert pair.decision.snapshot_id == "s-synthetic-old"
    with pytest.raises(ValueError, match="数据模式"):
        select_snapshot_pair(
            snapshots,
            execution_snapshot_id="s-production-new",
            decision_snapshot_id="s-synthetic-later",
        )


def test_explicit_decision_must_be_earlier_and_s1_ready():
    snapshots = [
        _snapshot("s-decision", "2026-09-17"),
        _snapshot("s-broken", "2026-09-18", ready=False),
        _snapshot("s-execution", "2026-09-19"),
    ]

    with pytest.raises(ValueError, match="S1"):
        select_snapshot_pair(
            snapshots,
            execution_snapshot_id="s-execution",
            decision_snapshot_id="s-broken",
        )
    with pytest.raises(ValueError, match="早于执行快照"):
        select_snapshot_pair(
            snapshots,
            execution_snapshot_id="s-decision",
            decision_snapshot_id="s-execution",
        )
