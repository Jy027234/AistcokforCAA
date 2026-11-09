"""Atomic reservation and usage settlement for governed assistant egress."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from aquant.application.assistant_budget import (
    AssistantBudgetConfig,
    BudgetExceeded,
    usage_cost_upper_bound,
    daily_budget_status,
    estimate_reservation,
    reserve_budget,
)
from aquant.domain.data.db import apply_migrations, connect

TEST_NOW = datetime(2026, 11, 2, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def config(*, cap: int = 1_000) -> AssistantBudgetConfig:
    # Synthetic unit-test prices; no production config or provider is used.
    return AssistantBudgetConfig(
        model="test-model-v1",
        price_version="unit-fixture-only",
        price_source="https://www.deepseek.com/pricing",
        effective_from=date(2026, 1, 1),
        input_rate=1_000_000,
        output_rate=1_000_000,
        daily_cap=cap,
    )


@pytest.fixture()
def budget_db(tmp_path):
    path = tmp_path / "meta.sqlite"
    con = connect(path)
    apply_migrations(con)
    yield con, path
    con.close()


def test_reservations_are_atomic_across_connections_and_reject_over_cap(budget_db):
    _con, path = budget_db
    cfg = config(cap=1_000)
    connections = [connect(path, allow_thread_sharing=True) for _ in range(8)]

    def reserve(index: int) -> str:
        try:
            reserve_budget(
                connections[index], config=cfg, provider="deepseek",
                subject_id=f"user:{index}", input_tokens=100, output_tokens=100,
                now=TEST_NOW)
            return "reserved"
        except BudgetExceeded:
            return "rejected"

    try:
        with ThreadPoolExecutor(max_workers=len(connections)) as pool:
            outcomes = list(pool.map(reserve, range(len(connections))))
        assert outcomes.count("reserved") == 5
        assert outcomes.count("rejected") == 3
        status = daily_budget_status(connections[0], cfg, now=TEST_NOW)
        assert status["usageCostUpperBoundMicroCny"] == cfg.daily_cap
        assert status["remainingBudgetMicroCny"] == 0
        assert status["costBasis"] == "REGISTERED_PRICE_UPPER_BOUND"
    finally:
        for con in connections:
            con.close()


def test_usage_budget_upper_bound_rounds_up_by_each_registered_rate():
    cfg = config()
    public_config = cfg.public_dict()
    assert public_config["costBasis"] == "REGISTERED_PRICE_UPPER_BOUND"
    assert "不代表供应商账单实扣金额" in public_config["accountingNote"]
    assert "inputPriceUpperBoundMicroCnyPerMillionTokens" in public_config
    assert estimate_reservation(cfg, input_tokens=257, output_tokens=8) == 265
    assert usage_cost_upper_bound(cfg, input_tokens=11, output_tokens=3) == 14
    # Each direction is billed independently; a fractional micro-yuan rounds up.
    fractional = AssistantBudgetConfig(
        model=cfg.model, price_version=cfg.price_version,
        price_source=cfg.price_source, effective_from=cfg.effective_from,
        input_rate=1, output_rate=1, daily_cap=cfg.daily_cap)
    assert usage_cost_upper_bound(fractional, input_tokens=1, output_tokens=1) == 2


def test_unpriced_legacy_call_blocks_new_reservation(budget_db):
    con, _path = budget_db
    con.execute(
        "INSERT INTO model_call (model_call_id,provider,model,prompt_version,"
        "outcome,error_code,called_at) VALUES (?,?,?,?,?,?,?)",
        ("mc-legacy-unknown-cost", "deepseek", "old-model", "legacy", "OK",
         None, "2026-11-02T00:00:00+00:00"),
    )
    status = daily_budget_status(con, config(), now=TEST_NOW)
    assert status["status"] == "UNKNOWN_USAGE"
    assert status["unaccountedLegacyCalls"] == 1

    from aquant.application.assistant_budget import BudgetUnavailable

    with pytest.raises(BudgetUnavailable) as raised:
        reserve_budget(con, config=config(), provider="deepseek",
                       subject_id="user:alice", input_tokens=100, output_tokens=100,
                       now=TEST_NOW)
    assert raised.value.code == "ASSISTANT_BUDGET_USAGE_UNKNOWN"
    assert con.execute(
        "SELECT count(*) FROM assistant_budget_reservation"
    ).fetchone()[0] == 0
