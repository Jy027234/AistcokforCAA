"""最小 Q5 agentctl handler 接线测试。

这些用例把能力接到产品自己的 SQLite 快照、证据、账本和 JobStore；
handler 模块本身不提供合成字典或测试专用写路径。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.agentctl.snapshot_card_reader import SnapshotCardReader  # noqa: E402
from aquant.domain.data.db import apply_migrations, connect  # noqa: E402
from aquant.domain.data.ingest import SnapshotBuilder  # noqa: E402
from aquant.domain.data.reader import SnapshotReader  # noqa: E402
from aquant.domain.data.snapshot import SnapshotStore  # noqa: E402
from aquant.operations.jobs import JobStatus, JobStore  # noqa: E402
from tests.integration.test_m1_ingest_e2e import build_snapshot  # noqa: E402


def _handlers():
    path = ROOT / "capabilities" / "aquant_lab_agentctl_handlers.py"
    spec = importlib.util.spec_from_file_location("q5_handlers", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def world(tmp_path):
    con = connect(tmp_path / "meta.sqlite")
    apply_migrations(con)
    root = tmp_path / "api"
    root.mkdir()
    store = SnapshotStore(con, root)
    build_snapshot(con, SnapshotBuilder(con, root / "datasets"), store)
    reader = SnapshotReader(store)
    cards = SnapshotCardReader(con, reader)
    yield con, reader, cards
    con.close()


def _invoke(function, arguments, *, metadata=None, **kwargs):
    return asyncio.run(function(
        {
            "invocation_id": "invoke-q5-test",
            "validated_arguments": arguments,
            "metadata": metadata or {
                "tenant_id": "tenant:test",
                "actor_user_id": "user:alice",
            },
        },
        **kwargs,
    ))


def _assert_execution_evidence(out, capability_id):
    ref = out["evidence_ref"]
    assert ref["capability_id"] == capability_id
    assert ref["invocation_id"] == "invoke-q5-test"
    unsigned = {key: value for key, value in out.items() if key != "evidence_ref"}
    canonical = json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    assert ref["result_sha256"] == f"sha256:{digest}"
    assert ref["evidence_id"] == f"aquant-result://{capability_id}/{digest}"


def test_event_evidence_reads_pit_data_and_never_submits_a_job(world):
    con, reader, _cards = world
    handlers = _handlers()
    before = con.execute("SELECT COUNT(*) FROM job").fetchone()[0]

    out = _invoke(
        handlers.event_evidence_read,
        {"instrument_id": "SYN.A.600519", "snapshot_id": "snap-syn-001"},
        reader=reader,
    )

    assert out["ok"] is True, out
    assert out["snapshot_id"] == "snap-syn-001"
    assert out["pit_filter"] == "available_at <= as_of_time"
    assert out["evidence"]
    _assert_execution_evidence(out, "aquant.event_evidence.read")
    malicious = out["evidence"][0]
    assert malicious["verificationStatus"] == "UNVERIFIED"
    assert malicious["authorization_blocked"] is True
    assert malicious["quote"] is None
    assert con.execute("SELECT COUNT(*) FROM job").fetchone()[0] == before


def test_event_evidence_rejects_a_current_event_for_an_old_snapshot(world):
    """A11：点名的晚到事件不得污染旧时点读取。"""

    con, reader, _cards = world
    handlers = _handlers()
    from datetime import datetime, timezone

    from aquant.domain.evidence.store import record_evidence

    late = record_evidence(
        con,
        instrument_id="SYN.A.600519",
        source_id="synthetic-fixture",
        source_url="https://example.invalid/a11-current-event",
        source_title="A11 current event",
        source_text="A11 current event is published after the old snapshot.",
        available_at=datetime(2026, 9, 12, tzinfo=timezone.utc),
        fact_summary="A11 current event",
        verification_status="VERIFIED",
        extra={
            "announced_on": "2026-09-12",
            "citations": ["A11 current event is published after the old snapshot."],
        },
    )
    before = con.execute("SELECT COUNT(*) FROM event").fetchone()[0]

    out = _invoke(
        handlers.event_evidence_read,
        {
            "instrument_id": "SYN.A.600519",
            "snapshot_id": "snap-syn-001",
            "event_ids": [late.event_id],
        },
        reader=reader,
    )

    assert out["ok"] is False, out
    assert out["error"]["code"] == "PIT_UNVERIFIED", out
    assert out["error"]["object_id"] == late.event_id
    assert con.execute("SELECT COUNT(*) FROM event").fetchone()[0] == before


def test_portfolio_read_uses_product_ledger_and_actor_context(world):
    con, _reader, _cards = world
    handlers = _handlers()
    con.execute(
        "INSERT INTO portfolio (portfolio_id,kind,initial_cash_cents,opened_at) "
        "VALUES ('pf-agentctl','M',100000,'2026-09-11T00:00:00Z')"
    )
    con.execute(
        "INSERT INTO cash_entry (entry_id,portfolio_id,entry_type,amount_cents,"
        "trading_day,occurred_at) VALUES (?,?,?,?,?,?)",
        ("cash-agentctl", "pf-agentctl", "INITIAL_DEPOSIT", 100000,
         "2026-09-11", "2026-09-11T00:00:00Z"),
    )

    out = asyncio.run(handlers.portfolio_read(
        {"validated_arguments": {"portfolio_id": "pf-agentctl"},
         "metadata": {"actor_user_id": "user:alice"}},
        con=con,
    ))

    assert out["ok"] is True, out
    assert out["cash"]["cents"] == 100000
    assert out["positions"] == []
    assert out["actor_user_id"] == "user:alice"
    assert out["read_only"] is True
    assert out["evidence_ref"]["capability_id"] == "aquant.portfolio.read"


def test_experiment_submit_uses_durable_job_idempotency(world):
    con, _reader, _cards = world
    handlers = _handlers()
    args = {
        "job_type": "FACTOR_COMPUTE",
        "trading_day": "2026-09-11",
        "snapshot_id": "snap-syn-001",
    }

    first = _invoke(handlers.experiment_submit, args, con=con)
    second = _invoke(handlers.experiment_submit, args, con=con)

    assert first["ok"] is True, first
    assert first["created"] is True
    assert second["created"] is False
    assert first["jobId"] == second["jobId"]
    assert first["idempotencyKey"] == second["idempotencyKey"]
    assert first["job_ref"]["job_id"] == first["jobId"]
    assert first["job_ref"]["status"] == "pending"
    assert first["job_ref"]["status_capability_id"] == "aquant.job.status"
    _assert_execution_evidence(first, "aquant.experiment.submit")
    assert con.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 1

    other = _invoke(
        handlers.experiment_submit,
        args,
        con=con,
        metadata={"tenant_id": "tenant:test", "actor_user_id": "user:bob"},
    )
    assert other["ok"] is True, other
    assert other["jobId"] != first["jobId"]
    assert con.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 2


def test_job_status_reads_durable_store_and_preserves_domain_state(world):
    con, _reader, _cards = world
    handlers = _handlers()
    job_id, _created = JobStore(con).submit(
        job_type="FACTOR_COMPUTE",
        trading_day="2026-09-11",
        config_version="default",
        input_snapshot_id="snap-syn-001",
        payload={"limit": 10},
        idempotency_namespace='{"actor_user_id":"user:alice","tenant_id":"tenant:test"}',
        owner_metadata={"tenant_id": "tenant:test", "actor_user_id": "user:alice"},
    )
    store = JobStore(con)
    assert store.claim("test-worker", job_types=["FACTOR_COMPUTE"]).job_id == job_id
    store.finish(
        job_id,
        JobStatus.FAILED,
        worker_id="test-worker",
        attempt_count=1,
        error_code="DATA_NOT_READY",
        error_detail="worker could not obtain the published input",
    )

    out = _invoke(
        handlers.job_status,
        {"job_id": job_id},
        con=con,
    )

    assert out["ok"] is True, out
    assert out["jobId"] == job_id
    assert out["status"] == "FAILED"
    assert out["errorCode"] == "DATA_NOT_READY"
    assert out["errorDetail"] == "worker could not obtain the published input"
    assert out["snapshotId"] == "snap-syn-001"
    assert out["job_ref"] == {
        "job_id": job_id,
        "status": "failed",
        "owner": "aquant_lab",
        "failure_code": "DATA_NOT_READY",
    }
    _assert_execution_evidence(out, "aquant.job.status")

    denied = _invoke(
        handlers.job_status,
        {"job_id": job_id},
        con=con,
        metadata={"tenant_id": "tenant:test", "actor_user_id": "user:bob"},
    )
    assert denied["ok"] is False
    assert denied["error"]["code"] == "SOURCE_PERMISSION_MISSING"


def test_job_status_returns_structured_error_for_unknown_job(world):
    con, _reader, _cards = world
    handlers = _handlers()

    out = _invoke(handlers.job_status, {"job_id": "job_missing"}, con=con)

    assert out["ok"] is False, out
    assert out["error"]["code"] == "DATA_NOT_READY"
    assert out["error"]["object_id"] == "job_missing"


def test_simulation_preview_stops_on_missing_real_s1_inputs_without_writes(
    world, monkeypatch
):
    con, _reader, cards = world
    handlers = _handlers()
    from aquant.domain.simulation.fees import synthetic_fee_table
    from aquant.domain.simulation import verified_fees

    fee_resolution_calls = 0

    def resolve_fees():
        nonlocal fee_resolution_calls
        fee_resolution_calls += 1
        return synthetic_fee_table(), "test"

    monkeypatch.setattr(verified_fees, "fee_table_from_env", resolve_fees)
    con.execute(
        "INSERT INTO portfolio (portfolio_id,kind,initial_cash_cents,opened_at) "
        "VALUES ('pf-preview','M',100000,'2026-09-11T00:00:00Z')"
    )
    con.execute(
        "INSERT INTO cash_entry (entry_id,portfolio_id,entry_type,amount_cents,"
        "trading_day,occurred_at) VALUES (?,?,?,?,?,?)",
        ("cash-preview", "pf-preview", "INITIAL_DEPOSIT", 100000,
         "2026-09-11", "2026-09-11T00:00:00Z"),
    )
    before = {
        name: con.execute(
            f'SELECT COUNT(*) FROM "{name}"'
        ).fetchone()[0]
        for name in ("simulation_plan", "order", "fill", "cash_entry")
    }

    out = _invoke(
        handlers.simulation_plan_preview,
        {
            "portfolio_id": "pf-preview",
            "snapshot_id": "snap-syn-001",
            "trading_day": "2026-09-08",
        },
        reader=cards,
    )

    # The checked-in synthetic snapshot intentionally has only five bars, so
    # an S1 preview cannot be honestly completed. The capability reports that
    # product data blocker and leaves all durable plan/ledger tables unchanged.
    assert out["ok"] is False, out
    assert out["error"]["code"] == "DATA_NOT_READY", out
    assert fee_resolution_calls == 1
    after = {
        name: con.execute(
            f'SELECT COUNT(*) FROM "{name}"'
        ).fetchone()[0]
        for name in before
    }
    assert after == before


def test_trusted_user_confirmation_bridge_preserves_stale_error_evidence(world):
    """The agentctl bridge delegates stale checks and emits execution evidence."""

    con, _reader, _cards = world
    handlers = _handlers()
    from aquant.domain.portfolio.plan import PlanError

    class StaleService:
        def freeze_confirmation(self, **_kwargs):
            raise PlanError(
                "STALE_SNAPSHOT",
                "account state changed since the preview",
                "plan-test",
                "re-preview against the current account state",
            )

    out = _invoke(
        handlers.simulation_plan_confirm_user,
        {"plan_id": "plan-test", "confirmation_token": "opaque-token"},
        service=StaleService(),
        reader=object(),
        con=con,
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "STALE_SNAPSHOT"
    assert out["confirmation_path"] == "product_owned_trusted_user"
    assert out["agentctl_actor_verified"] is True
    assert out["actor_user_id"] == "user:alice"
    _assert_execution_evidence(out, "aquant.simulation_plan.confirm_user")


def test_trusted_user_confirmation_bridge_rejects_model_actor(world):
    con, _reader, _cards = world
    handlers = _handlers()

    out = _invoke(
        handlers.simulation_plan_confirm_user,
        {"plan_id": "plan-test", "confirmation_token": "opaque-token"},
        metadata={"tenant_id": "tenant:test", "actor_user_id": "model:assistant"},
        reader=object(),
        con=con,
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "SOURCE_PERMISSION_MISSING"
