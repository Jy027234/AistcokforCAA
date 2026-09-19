"""M1+M2 产品闭环端到端验收。

链路（主文档 §22 建议的第一条开发主线）：

    固定快照 -> 研究读取 -> 组合草稿 -> 用户确认冻结 -> 模拟成交 -> 日终估值 -> 对账

这条链路的意义不是"跑通"，而是证明四件事同时成立：
  1. 所有数值绑定同一个快照，可重放；
  2. 冻结是用户动作，模型无权确认；
  3. 账户在预览与确认之间变化时必须拒绝（A09）；
  4. 重复执行不产生第二次入账（A07 / S05）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.data.ingest import SnapshotBuilder
from aquant.domain.data.reader import SnapshotReader
from aquant.domain.data.snapshot import SnapshotStore
from aquant.domain.portfolio.construction import Candidate, ConstructionParams
from aquant.domain.portfolio.plan import PlanError, PlanService, confirmer_is_human
from aquant.domain.simulation.fees import synthetic_fee_table
from aquant.domain.simulation.simulator import BoardRule, Lot, SimError

from tests.integration.test_m1_ingest_e2e import build_snapshot

TRADING_DAY = date(2026, 9, 8)
AS_OF = datetime(2026, 9, 11, 12, 30, tzinfo=timezone.utc)

RULES = [
    BoardRule(exchange="SSE", board="MAIN", price_limit_pct=Decimal("10"),
              lot_size=100, effective_from=date(2026, 7, 6)),
    BoardRule(exchange="SZSE", board="MAIN", price_limit_pct=Decimal("10"),
              lot_size=100, effective_from=date(2026, 7, 6)),
]
LISTINGS = {
    "SYN.A.600519": ("SSE", "MAIN"),
    "SYN.A.000001": ("SZSE", "MAIN"),
    "SYN.A.600003": ("SSE", "MAIN"),
}


@pytest.fixture()
def world(tmp_path):
    con = connect(tmp_path / "meta.sqlite")
    apply_migrations(con)
    root = tmp_path / "api"
    root.mkdir()
    store = SnapshotStore(con, root)
    builder = SnapshotBuilder(con, root / "datasets")
    build_snapshot(con, builder, store)
    reader = SnapshotReader(store)
    svc = PlanService(con, reader, synthetic_fee_table(), RULES, LISTINGS,
                      ConstructionParams(max_holdings=3,
                                         max_single_name_pct=Decimal("20"),
                                         max_single_industry_pct=Decimal("50")))
    yield con, reader, svc, store
    con.close()


CANDIDATES = [
    Candidate("SYN.A.600519", "IND_FOOD", 0.90),
    Candidate("SYN.A.000001", "IND_BANK", 0.70),
]


def preview(svc, *, cash=100_000_000, lots=None, subject="user:alice", **over):
    return svc.preview(
        portfolio_id="pf-syn-m", snapshot_id="snap-syn-001", trading_day=TRADING_DAY,
        as_of=AS_OF, candidates=CANDIDATES, cash_available_cents=cash,
        lots=lots or [], confirm_subject=subject, **over,
    )


def freeze_preview(svc, pv, *, lots=None, cash=100_000_000, subject="user:alice",
                   ttl=timedelta(hours=12), now=None):
    lots = lots or []
    token = svc.issue_confirmation(preview=pv, subject=subject, current_lots=lots,
                                   current_cash_cents=cash, now=now)
    return svc.freeze(preview=pv, confirm_subject=subject, confirmation_token=token,
                      expected_account_version=pv.account_version, current_lots=lots,
                      current_cash_cents=cash, ttl=ttl, now=now)


# ================================================================== 闭环
def test_full_loop_from_snapshot_to_reconciliation(world):
    con, reader, svc, store = world

    # 1. 研究读取绑定固定快照
    ref = reader.ref("snap-syn-001")
    assert ref.data_mode == "SYNTHETIC"
    quotes = reader.daily_quotes("snap-syn-001", as_of=AS_OF,
                                 instrument_id="SYN.A.600519", end=TRADING_DAY)
    assert quotes, "快照必须能回答研究读取"

    # 2. 预览（只算不冻）
    pv = preview(svc)
    assert pv.frozen is False
    assert pv.orders, "应当产生建仓订单"
    assert pv.estimated_fees_cents > 0
    assert pv.snapshot_id == "snap-syn-001"

    # 3. 用户确认并冻结
    frozen = freeze_preview(svc, pv)
    assert frozen["status"] == "FROZEN"
    assert frozen["confirmed_by"] == "user:alice"

    # 4. 执行
    lots: list[Lot] = []
    ex = svc.execute(plan_id=pv.plan_id, lots=lots, cash_available_cents=100_000_000)
    assert ex["status"] == "EXECUTED"
    assert ex["fills"], "冻结的计划应当成交"
    assert lots, "成交应产生批次"

    # 5. 日终估值（落库；不变量全过才允许发布）
    cash_after = 100_000_000 + sum(e["amount_cents"] for e in ex["cash_entries"])
    val = svc.value(portfolio_id="pf-syn-m", snapshot_id="snap-syn-001",
                    trading_day=TRADING_DAY, as_of=AS_OF, lots=lots,
                    cash_available_cents=cash_after)
    assert val["published"] is True, val["invariants"]["violations"]
    assert val["net_value_cents"] > 0
    stored = con.execute(
        "SELECT published, net_value_cents FROM valuation WHERE portfolio_id=?",
        ("pf-syn-m",)).fetchone()
    assert stored["published"] == 1, "不变量全过时净值应当已发布"
    assert stored["net_value_cents"] == val["net_value_cents"]

    # 6. 对账
    rec = svc.reconcile(portfolio_id="pf-syn-m")
    assert rec["fill_count"] == len(ex["fills"])
    assert rec["invariants"]["fees_booked_once"] is True
    assert rec["reconciled"] is True
    assert rec["cash_cents"] >= 0, "现金不得透支"


def test_freeze_persists_separate_decision_and_execution_snapshot_binding(world):
    """冻结产物必须把决策时点与成交行情来源一起固化。"""

    con, _reader, svc, _store = world
    pv = preview(svc)
    freeze_preview(svc, pv)
    binding = con.execute(
        "SELECT decision_snapshot_id,decision_cutoff_at,"
        "execution_snapshot_id,execution_cutoff_at "
        "FROM plan_snapshot_binding WHERE plan_id=?",
        (pv.plan_id,),
    ).fetchone()
    assert binding is not None
    assert binding["decision_snapshot_id"] == pv.snapshot_id
    assert binding["execution_snapshot_id"] == pv.execution_snapshot_id
    assert binding["decision_cutoff_at"]
    assert binding["execution_cutoff_at"]


def test_explicit_decision_cutoff_after_execution_open_is_rejected(world):
    """显式时点链路不得把执行日收盘之后的数据当成盘前输入。"""

    _con, _reader, svc, _store = world
    with pytest.raises(PlanError, match="not before"):
        preview(
            svc,
            # The fixture is after 2026-09-08's open.  Supplying the explicit
            # fields opts into strict decision/execution timing validation.
            decision_snapshot_id="snap-syn-001",
            decision_cutoff_at=AS_OF,
            execution_snapshot_id="snap-syn-001",
        )


def test_production_preview_rejects_legacy_single_snapshot(world, monkeypatch):
    """真实快照不能沿用 decision=execution 的旧兼容路径。"""

    _con, reader, svc, _store = world
    from dataclasses import replace

    original_ref = reader.ref
    monkeypatch.setattr(reader, "ref", lambda sid: replace(
        original_ref(sid), data_mode="PRODUCTION"))
    with pytest.raises(PlanError, match="distinct decision and execution"):
        preview(svc)


def test_snapshot_is_the_single_source_of_prices(world):
    """所有成交价都来自快照，不来自任何"最新价"。"""

    con, reader, svc, store = world
    pv = preview(svc)
    freeze_preview(svc, pv)
    lots: list[Lot] = []
    ex = svc.execute(plan_id=pv.plan_id, lots=lots, cash_available_cents=100_000_000)

    rows = reader.daily_quotes("snap-syn-001", as_of=AS_OF,
                                instrument_id="SYN.A.600519", end=TRADING_DAY)
    bar = next(r for r in rows if r.trading_day == TRADING_DAY)
    fills_600519 = [f for f in ex["fills"] if f["instrument_id"] == "SYN.A.600519"]
    assert fills_600519
    # 成交价来自**计划交易日**的快照开盘价加滑点，买入方向必然高于开盘价
    assert fills_600519[0]["price_cents"] > bar.open_cents


# ================================================================== A08
def test_preview_writes_nothing(world):
    """A08：预览不产生订单成交或现金变化。"""

    con, reader, svc, store = world
    before_plans = con.execute("SELECT COUNT(*) FROM simulation_plan").fetchone()[0]
    before_fills = con.execute("SELECT COUNT(*) FROM fill").fetchone()[0]
    before_cash = con.execute("SELECT COUNT(*) FROM cash_entry").fetchone()[0]

    preview(svc)

    assert con.execute("SELECT COUNT(*) FROM simulation_plan").fetchone()[0] == before_plans
    assert con.execute("SELECT COUNT(*) FROM fill").fetchone()[0] == before_fills
    assert con.execute("SELECT COUNT(*) FROM cash_entry").fetchone()[0] == before_cash


# ================================================================== A09
def test_stale_account_blocks_freeze(world):
    """A09：确认之后账户已变 -> 拒绝陈旧提交，要求重新核验。"""

    con, reader, svc, store = world
    pv = preview(svc)

    # 账户在预览之后发生了变化（多了一笔批次）
    changed_lots = [Lot(lot_id="l-new", instrument_id="SYN.A.600519",
                        acquired_trading_day=TRADING_DAY - timedelta(days=5),
                        earliest_sellable_day=TRADING_DAY - timedelta(days=4),
                        quantity_original=100, quantity_remaining=100,
                        cost_basis_cents_per_share=10000)]
    token = svc.issue_confirmation(preview=pv, subject="user:alice", current_lots=[],
                                   current_cash_cents=100_000_000)
    with pytest.raises(PlanError) as exc:
        svc.freeze(preview=pv, confirm_subject="user:alice",
                   confirmation_token=token,
                   expected_account_version=pv.account_version,
                   current_lots=changed_lots, current_cash_cents=100_000_000)
    assert exc.value.code == "STALE_SNAPSHOT"
    assert "re-preview" in exc.value.repair_action


def test_mismatched_expected_version_blocks_freeze(world):
    con, reader, svc, store = world
    pv = preview(svc)
    token = svc.issue_confirmation(preview=pv, subject="user:alice", current_lots=[],
                                   current_cash_cents=100_000_000)
    with pytest.raises(PlanError):
        svc.freeze(preview=pv, confirm_subject="user:alice",
                   confirmation_token=token,
                   expected_account_version="acct-somethingelse",
                   current_lots=[], current_cash_cents=100_000_000)


# ================================================================== 模型不得确认
def test_model_cannot_confirm_a_plan(world):
    """§16.3 冻结模拟计划通过用户界面确认的服务执行，不把确认权限交给模型。"""

    con, reader, svc, store = world
    pv = svc.preview(portfolio_id="pf-syn-m", snapshot_id="snap-syn-001",
                     trading_day=TRADING_DAY, as_of=AS_OF, candidates=CANDIDATES,
                     cash_available_cents=100_000_000, lots=[],
                     confirm_subject="model:assistant")
    # 预览本身允许（生成草稿），但服务端不得向模型主体签发确认。
    with pytest.raises(PlanError) as exc:
        svc.issue_confirmation(preview=pv, subject="model:assistant", current_lots=[],
                               current_cash_cents=100_000_000)
    assert "not a human principal" in exc.value.message
    assert "models never confirm plans" in exc.value.repair_action


@pytest.mark.parametrize("subject", ["model:x", "assistant", "agent-1", "llm", "bot",
                                     "gpt-4", "claude", "AI:helper", ""])
def test_non_human_confirmers_are_rejected(subject):
    assert confirmer_is_human(subject) is False


@pytest.mark.parametrize("subject", ["user:alice", "alice", "trader_01"])
def test_human_confirmers_are_accepted(subject):
    assert confirmer_is_human(subject) is True


def test_short_confirmation_token_is_rejected(world):
    con, reader, svc, store = world
    pv = preview(svc)
    with pytest.raises(PlanError) as exc:
        svc.freeze(preview=pv, confirm_subject="user:alice", confirmation_token="x",
                   expected_account_version=pv.account_version,
                   current_lots=[], current_cash_cents=100_000_000)
    assert "token" in exc.value.message


def test_confirmation_is_bound_to_immutable_preview_and_is_single_use(world):
    con, reader, svc, store = world
    pv = preview(svc)
    token = svc.issue_confirmation(preview=pv, subject="user:alice", current_lots=[],
                                   current_cash_cents=100_000_000)
    pv.orders[0]["quantity"] += 100
    with pytest.raises(PlanError) as exc:
        svc.freeze(preview=pv, confirm_subject="user:alice", confirmation_token=token,
                   expected_account_version=pv.account_version, current_lots=[],
                   current_cash_cents=100_000_000)
    assert exc.value.code == "STALE_SNAPSHOT"

    pv2 = preview(svc)
    token2 = svc.issue_confirmation(preview=pv2, subject="user:alice", current_lots=[],
                                    current_cash_cents=100_000_000)
    svc.freeze(preview=pv2, confirm_subject="user:alice", confirmation_token=token2,
               expected_account_version=pv2.account_version, current_lots=[],
               current_cash_cents=100_000_000)
    with pytest.raises(PlanError) as replay:
        svc.freeze(preview=pv2, confirm_subject="user:alice", confirmation_token=token2,
                   expected_account_version=pv2.account_version, current_lots=[],
                   current_cash_cents=100_000_000)
    assert "consumed" in replay.value.message


# ================================================================== A07 / S05
def test_executing_a_plan_twice_is_refused(world):
    """计划只能执行一次；重复执行被状态机挡住。"""

    con, reader, svc, store = world
    pv = preview(svc)
    freeze_preview(svc, pv)
    svc.execute(plan_id=pv.plan_id, lots=[], cash_available_cents=100_000_000)
    with pytest.raises(PlanError) as exc:
        svc.execute(plan_id=pv.plan_id, lots=[], cash_available_cents=100_000_000)
    assert "not FROZEN" in exc.value.message


def test_expired_plan_is_refused_and_marked(world):
    con, reader, svc, store = world
    pv = preview(svc)
    past = datetime.now(timezone.utc) - timedelta(days=2)
    freeze_preview(svc, pv, ttl=timedelta(seconds=1), now=past)
    with pytest.raises(PlanError) as exc:
        svc.execute(plan_id=pv.plan_id, lots=[], cash_available_cents=100_000_000)
    assert exc.value.code == "DECISION_CUTOFF_PASSED"
    status = con.execute("SELECT status FROM simulation_plan WHERE plan_id=?",
                         (pv.plan_id,)).fetchone()["status"]
    assert status == "EXPIRED"


def test_unknown_plan_is_rejected(world):
    con, reader, svc, store = world
    with pytest.raises(PlanError):
        svc.execute(plan_id="plan_nope", lots=[], cash_available_cents=0)


# ================================================================== 规则检查
def test_rule_check_failure_prevents_preview(world):
    """规则检查不通过时不得预览成功——更不得冻结在猜测之上。"""

    con, reader, svc, store = world
    # 去掉全部板块规则
    svc.board_rules = []
    with pytest.raises(PlanError) as exc:
        preview(svc)
    assert exc.value.code == "RULE_VERSION_MISSING"


def test_suspended_instrument_is_sized_from_prior_close_then_does_not_fill(world):
    """盘前计划不能偷看当日是否停牌；执行时无开盘价则明确不成交。"""

    con, reader, svc, store = world
    suspended = Candidate("SYN.A.600002", "IND_PHARMA", 0.99)
    svc2 = PlanService(con, reader, synthetic_fee_table(), RULES,
                       {**LISTINGS, "SYN.A.600002": ("SSE", "MAIN")},
                       ConstructionParams(max_holdings=2,
                                          max_single_name_pct=Decimal("20"),
                                          max_single_industry_pct=Decimal("50")))
    # 计划仅使用 9 月 10 日之前可知的收盘价，因此可以产生订单。
    pv = svc2.preview(portfolio_id="pf-syn-m", snapshot_id="snap-syn-001",
                     trading_day=date(2026, 9, 10), as_of=AS_OF,
                     candidates=[suspended], cash_available_cents=100_000_000,
                     lots=[], confirm_subject="user:alice")
    assert pv.orders
    freeze_preview(svc2, pv)
    executed = svc2.execute(plan_id=pv.plan_id, lots=[],
                            cash_available_cents=100_000_000)
    assert executed["fills"] == []
    assert executed["rejections"][0]["reason"] == "NO_VALID_OPEN_PRICE"


# ================================================================== 冻结不可变
def test_frozen_plan_is_immutable_in_the_database(world):
    con, reader, svc, store = world
    pv = preview(svc)
    freeze_preview(svc, pv)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE simulation_plan SET status='DRAFT' WHERE plan_id=?",
                    (pv.plan_id,))


def test_ledger_tables_receive_the_expected_rows(world):
    con, reader, svc, store = world
    pv = preview(svc)
    freeze_preview(svc, pv)
    lots: list[Lot] = []
    ex = svc.execute(plan_id=pv.plan_id, lots=lots, cash_available_cents=100_000_000)

    assert con.execute("SELECT COUNT(*) FROM fill").fetchone()[0] == len(ex["fills"])
    assert con.execute("SELECT COUNT(*) FROM fee_charge").fetchone()[0] > 0
    assert con.execute("SELECT COUNT(*) FROM position_lot").fetchone()[0] == len(lots)
    # 期初资金也是一条分录（§15.1 要求从期初对账），因此是执行分录 + 1
    assert con.execute("SELECT COUNT(*) FROM cash_entry").fetchone()[0] == len(ex["cash_entries"]) + 1


def test_sell_consumption_updates_the_authoritative_lot_ledger(world):
    con, reader, svc, store = world
    held = Lot(lot_id="opening-lot", instrument_id="SYN.A.600519",
               acquired_trading_day=TRADING_DAY - timedelta(days=5),
               earliest_sellable_day=TRADING_DAY - timedelta(days=4),
               quantity_original=1000, quantity_remaining=1000,
               cost_basis_cents_per_share=9000)
    pv = svc.preview(portfolio_id="pf-syn-m", snapshot_id="snap-syn-001",
                     trading_day=TRADING_DAY, as_of=AS_OF, candidates=[CANDIDATES[0]],
                     cash_available_cents=0, lots=[held], confirm_subject="user:alice")
    assert any(o["side"] == "SELL" for o in pv.orders)
    freeze_preview(svc, pv, cash=0, lots=[held])
    account_lots = [held]
    ex = svc.execute(plan_id=pv.plan_id, lots=account_lots, cash_available_cents=0)
    sell_fill = next(f for f in ex["fills"] if f["side"] == "SELL")
    remaining = con.execute(
        "SELECT quantity_remaining FROM position_lot WHERE lot_id='opening-lot'"
    ).fetchone()[0]
    consumed = con.execute(
        "SELECT COALESCE(SUM(quantity),0) FROM lot_consumption WHERE fill_id=?",
        (sell_fill["fill_id"],),
    ).fetchone()[0]
    assert remaining == 1000 - sell_fill["quantity"]
    assert consumed == sell_fill["quantity"]
    assert svc.reconcile(portfolio_id="pf-syn-m")["reconciled"] is True


def test_wrong_caller_cash_cannot_publish_a_valuation(world):
    con, reader, svc, store = world
    pv = preview(svc)
    known_rows = reader.daily_quotes("snap-syn-001", as_of=AS_OF,
                                     instrument_id="SYN.A.600519", end=TRADING_DAY)
    prior = [r for r in known_rows if r.trading_day < TRADING_DAY][-1]
    planned = next(o for o in pv.orders if o["instrument_id"] == "SYN.A.600519")
    assert planned["price_cents"] == prior.close_cents
    assert planned["reference_price_day"] == prior.trading_day.isoformat()
    freeze_preview(svc, pv)
    lots: list[Lot] = []
    ex = svc.execute(plan_id=pv.plan_id, lots=lots, cash_available_cents=100_000_000)
    actual_cash = 100_000_000 + sum(e["amount_cents"] for e in ex["cash_entries"])
    val = svc.value(portfolio_id="pf-syn-m", snapshot_id="snap-syn-001",
                    trading_day=TRADING_DAY, as_of=AS_OF, lots=lots,
                    cash_available_cents=actual_cash + 1)
    assert val["cash_available_cents"] == actual_cash
    assert val["published"] is False
    assert any(v["code"] == "STALE_SNAPSHOT" for v in val["invariants"]["violations"])


def test_plan_records_rule_and_fee_versions(world):
    """§7.1 计划必须保存规则与费用版本，否则研究不可复现。"""

    con, reader, svc, store = world
    pv = preview(svc)
    freeze_preview(svc, pv)
    row = con.execute("SELECT rule_version, fee_version, idempotency_key, "
                      "confirmation_token_hash FROM simulation_plan WHERE plan_id=?",
                      (pv.plan_id,)).fetchone()
    assert row["rule_version"] and row["rule_version"] != "unknown"
    assert row["fee_version"] == "fee-syn-v1"
    assert row["idempotency_key"].startswith("freeze|")
    # 令牌只存哈希，不存明文
    assert row["confirmation_token_hash"].startswith("sha256:")
    assert "confirm-token" not in row["confirmation_token_hash"]
