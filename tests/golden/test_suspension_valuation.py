"""停牌估值的持久化不变量（主文档 §12.8、S08）。

为什么单独写这一条
------------------
`value_positions` 的停牌分支（沿用最近有效收盘价 + 标注停牌天数）在
S08 的域函数用例里已经验过。但**算得对不等于记得住**：估值结果要落到
`valuation` 与 `valuation_position` 两张表里，而"价格基准"与"停牌天数"
正是在那里被界面读出来给用户看的。如果落库时把 price_basis 写成 CLOSE、
或把 staleness_days 丢掉，界面上就会显示成一个当天的正常价格——
账面看起来毫无异常，这正是最危险的一类错误。

真实数据里没有中途停牌的样本（池内 900 只全部覆盖窗口 61 天），
所以这条不变量只能用受控数据来锁。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.data.ingest import SnapshotBuilder
from aquant.domain.data.reader import SnapshotReader
from aquant.domain.data.snapshot import SnapshotStore
from aquant.domain.portfolio.construction import value_positions
from aquant.domain.portfolio.plan import PlanService
from aquant.domain.simulation.fees import synthetic_fee_table
from aquant.domain.simulation.simulator import BoardRule, Lot

from tests.integration.test_m1_ingest_e2e import build_snapshot

SNAPSHOT_ID = "snap-syn-001"
PORTFOLIO = "pf-susp-M"
AS_OF = datetime(2026, 9, 11, 12, 30, tzinfo=timezone.utc)
OPENING_CASH = 100_000_000

RULES = [
    BoardRule(exchange="SSE", board="MAIN", price_limit_pct=Decimal("10"),
              lot_size=100, effective_from=date(2026, 7, 6)),
    BoardRule(exchange="SZSE", board="MAIN", price_limit_pct=Decimal("10"),
              lot_size=100, effective_from=date(2026, 7, 6)),
]
LISTINGS = {"SYN.A.600519": ("SSE", "MAIN"), "SYN.A.000001": ("SZSE", "MAIN"),
            "SYN.A.600003": ("SSE", "MAIN")}


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
    svc = PlanService(con, reader, synthetic_fee_table(), RULES, LISTINGS)
    yield con, reader, svc, store
    con.close()


# 2026-09-10、09-11 是交易日，但快照里 SYN.A.600002 从 09-10 起就停牌
SUSPENDED_DAY = date(2026, 9, 10)
LAST_VALID_DAY = date(2026, 9, 9)
LAST_VALID_PRICE = 8850        # 与 examples 里最后一条行情一致
SHARES = 1000


def _lot() -> Lot:
    return Lot(lot_id="lot-susp-1", instrument_id="SYN.A.600002",
               acquired_trading_day=date(2026, 9, 1),
               earliest_sellable_day=date(2026, 9, 2),
               quantity_original=SHARES, quantity_remaining=SHARES,
               cost_basis_cents_per_share=8800)


def test_suspended_position_is_valued_at_last_valid_close(world):
    """停牌日：没有 Bar，必须用此前最后一个有效收盘价，并标注停牌天数。"""

    _con, _reader, _svc, _store = world
    positions, issues = value_positions(
        lots=[_lot()],
        bars={},                     # 停牌：当日无行情
        last_valid_price={"SYN.A.600002": (LAST_VALID_PRICE, LAST_VALID_DAY)},
        trading_day=SUSPENDED_DAY,
    )
    assert not issues, issues
    assert len(positions) == 1
    p = positions[0]
    assert p.price_cents == LAST_VALID_PRICE, "不得编造当日价格"
    assert p.price_basis == "SUSPENDED_LAST_VALID", (
        "价格基准必须是「停牌沿用」，否则界面会把它显示成当日正常收盘价")
    assert p.staleness_days == (SUSPENDED_DAY - LAST_VALID_DAY).days
    assert p.value_cents == SHARES * LAST_VALID_PRICE


def test_no_price_at_all_is_reported_not_silently_zeroed(world):
    """既无当日行情、又无此前有效价：必须有明确问题说明，而不是静默 0。"""

    _con, _reader, _svc, _store = world
    positions, issues = value_positions(
        lots=[_lot()], bars={}, last_valid_price={}, trading_day=SUSPENDED_DAY,
    )
    assert positions and positions[0].price_basis == "UNSUPPORTED"
    assert issues, "无价可用却没有给出任何说明"
    assert issues[0]["code"] == "DATA_NOT_READY"


def test_suspension_basis_and_staleness_are_persisted(world):
    """落到 valuation_position 的价格基准与停牌天数必须与域函数一致。

    这条针对的是"算得对但记不住"：界面读的是表里的值，
    表里丢了 basis，用户看到的就只是一个当天的价格。
    """

    con, reader, svc, _store = world

    # 期望值从**快照本身**读出来，不在这里抄一个数字：
    # 抄数字的话，夹具一改这条用例就会以与不变量无关的方式失败，
    # 而它真正要证明的是"落库的值与快照里最后一条有效收盘价一致"。
    history = [h for h in reader.daily_quotes(
        SNAPSHOT_ID, as_of=AS_OF, instrument_id="SYN.A.600002")
        if h.trading_day <= SUSPENDED_DAY]
    assert history, "夹具里应当有停牌前的行情"
    expected_price = history[-1].close_cents
    expected_stale = (SUSPENDED_DAY - history[-1].trading_day).days
    assert expected_stale >= 1, "该用例需要一天以上的停牌"

    svc._ensure_account(PORTFOLIO, initial_cash_cents=OPENING_CASH,
                        initial_lots=[_lot()], now=AS_OF)
    out = svc.value(portfolio_id=PORTFOLIO, snapshot_id=SNAPSHOT_ID,
                    trading_day=SUSPENDED_DAY, as_of=AS_OF,
                    lots=[_lot()], cash_available_cents=OPENING_CASH)
    assert out["published"] is True

    row = con.execute(
        "SELECT instrument_id,quantity,price_cents,price_basis,staleness_days,"
        "value_cents FROM valuation_position WHERE valuation_id=?",
        (f"val-{PORTFOLIO}-{SUSPENDED_DAY.isoformat()}",)).fetchone()
    assert row is not None, "停牌持仓没有落库到 valuation_position"
    assert row["price_basis"] == "SUSPENDED_LAST_VALID"
    assert row["price_cents"] == expected_price
    assert row["quantity"] == SHARES
    assert row["staleness_days"] == expected_stale
    assert row["value_cents"] == SHARES * expected_price

    # 净值里必须包含这笔持仓，不能因为停牌就把它算没
    val = con.execute(
        "SELECT positions_value_cents,net_value_cents FROM valuation WHERE valuation_id=?",
        (f"val-{PORTFOLIO}-{SUSPENDED_DAY.isoformat()}",)).fetchone()
    assert val["positions_value_cents"] == SHARES * expected_price
    assert val["net_value_cents"] == (OPENING_CASH + SHARES * expected_price)
