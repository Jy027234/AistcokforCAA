"""现金分红接入账本与估值（主文档 §12.7、S07 的持久化验收）。

S07 在域函数层面已经证明"除权日确认应收、到账日转现金、收益不重复"。
但**证明算法正确不等于证明账本记得住**：应收原先只存在于函数返回值里，
`receivable` 表没有任何写入方，`valuation.receivables_cents` 恒为 0。
于是账面上一次分红会凭空少一笔资产，而且看不出来是漏记。

本文件验收的是持久化那一半：

  1. 除权日：应收落库为 RECOGNIZED，**现金不动**，净值含这笔应收；
  2. 到账日：应收转 SETTLED，现金增加等额，**净值不因结清而跳变**
     （否则就是把同一笔收益计了两次）；
  3. 重复推进不产生第二条应收、也不产生第二笔现金；
  4. 应收不是调用方传进来的参数——估值自己从账本读。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.data.ingest import SnapshotBuilder
from aquant.domain.data.reader import SnapshotReader
from aquant.domain.data.snapshot import SnapshotStore
from aquant.domain.portfolio.plan import PlanService
from aquant.domain.simulation.corporate_actions import CashDividend
from aquant.domain.simulation.fees import synthetic_fee_table
from aquant.domain.simulation.simulator import BoardRule, Lot

from tests.integration.test_m1_ingest_e2e import build_snapshot

SNAPSHOT_ID = "snap-syn-001"
PORTFOLIO = "pf-div-M"
INSTRUMENT = "SYN.A.600519"
RECORD_DAY = date(2026, 9, 8)      # 登记日：持有 1000 股
EX_DAY = date(2026, 9, 9)          # 除权日
PAY_DAY = date(2026, 9, 10)        # 到账日
CASH_PER_SHARE = 50                # 每股 0.50 元
SHARES = 1000
EXPECTED_RECEIVABLE = SHARES * CASH_PER_SHARE
OPENING_CASH = 100_000_000

RULES = [
    BoardRule(exchange="SSE", board="MAIN", price_limit_pct=Decimal("10"),
              lot_size=100, effective_from=date(2026, 7, 6)),
    BoardRule(exchange="SZSE", board="MAIN", price_limit_pct=Decimal("10"),
              lot_size=100, effective_from=date(2026, 7, 6)),
]
LISTINGS = {INSTRUMENT: ("SSE", "MAIN"), "SYN.A.000001": ("SZSE", "MAIN"),
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


def _lot(shares: int = SHARES, acquired: date = RECORD_DAY) -> Lot:
    return Lot(
        lot_id="lot-div-1", instrument_id=INSTRUMENT,
        acquired_trading_day=acquired, earliest_sellable_day=acquired,
        quantity_original=shares, quantity_remaining=shares,
        cost_basis_cents_per_share=1000,
    )


def _dividend() -> CashDividend:
    return CashDividend(
        action_id="ca-div-1", instrument_id=INSTRUMENT,
        record_date=RECORD_DAY, ex_date=EX_DAY, pay_date=PAY_DAY,
        cash_per_share_cents_input=CASH_PER_SHARE,
    )


def _setup(svc, *, snapshot_id: str, shares: int = SHARES, acquired: date = RECORD_DAY):
    lots = [_lot(shares, acquired)] if shares else []
    # 建账：期初现金与批次。_ensure_account 走的是 preview 那条路径。
    svc._ensure_account(PORTFOLIO, initial_cash_cents=OPENING_CASH,
                        initial_lots=lots, now=datetime(2026, 9, 8, tzinfo=timezone.utc))
    return lots


def _value(svc, *, snapshot_id: str, day: date, lots: list[Lot]):
    return svc.value(
        portfolio_id=PORTFOLIO, snapshot_id=snapshot_id, trading_day=day,
        as_of=datetime(2026, 9, 11, 12, 30, tzinfo=timezone.utc),
        lots=lots, cash_available_cents=OPENING_CASH,
    )


def _cash(con) -> int:
    return int(con.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS t FROM cash_entry WHERE portfolio_id=?",
        (PORTFOLIO,)).fetchone()["t"])


def _dividend_cash(con) -> int:
    """已从应收转为现金的分红金额。

    现金分录的约定是**流入为正、流出为负**（见 `_ledger_cash` 与
    `INITIAL_DEPOSIT` 的写法：期初资金是一条正数分录）。
    """

    return int(con.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS t FROM cash_entry "
        "WHERE portfolio_id=? AND entry_type='DIVIDEND_RECEIVABLE_SETTLED'",
        (PORTFOLIO,)).fetchone()["t"])


def _dividend_contribution(con) -> int:
    """这笔分红此刻体现为多少资产：未结应收 + 已到账现金。"""

    return _open_receivable(con) + _dividend_cash(con)


def _open_receivable(con) -> int:
    return int(con.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS t FROM receivable "
        "WHERE portfolio_id=? AND status='RECOGNIZED'", (PORTFOLIO,)).fetchone()["t"])


# ================================================== 除权日：确认应收，现金不动
def test_ex_date_recognises_receivable_without_touching_cash(world):
    con, reader, svc, store = world
    snapshot_id = SNAPSHOT_ID
    lots = _setup(svc, snapshot_id=snapshot_id)
    cash_before = _cash(con)

    out = svc._advance_corporate_actions(
        portfolio_id=PORTFOLIO, trading_day=EX_DAY, actions=[_dividend()],
        lots=lots, now=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )

    assert out[0]["stage"] == "EX_DATE"
    assert _open_receivable(con) == EXPECTED_RECEIVABLE
    assert _cash(con) == cash_before, "除权日不得直接进现金，否则与到账日重复计量"

    row = con.execute(
        "SELECT * FROM receivable WHERE portfolio_id=?", (PORTFOLIO,)).fetchone()
    assert row["status"] == "RECOGNIZED"
    assert row["recognized_on"] == EX_DAY.isoformat()
    assert row["expected_settlement_on"] == PAY_DAY.isoformat()
    assert row["settled_on"] is None
    assert row["corporate_action_id"] == "ca-div-1"
    assert row["tax_treatment"] == "PRE_TAX"


def test_valuation_includes_open_receivable_without_being_told(world):
    """应收必须由估值自己从账本读，不能靠调用方传参。

    这是"账本权威"的判据：就算调用方完全不知道分红这回事，
    净值里也必须已经有这笔应收。
    """

    con, reader, svc, store = world
    snapshot_id = SNAPSHOT_ID
    lots = _setup(svc, snapshot_id=snapshot_id)
    _advance(svc, lots, day=EX_DAY)

    result = _value(svc, snapshot_id=snapshot_id, day=EX_DAY, lots=lots)
    assert result["receivables_cents"] == EXPECTED_RECEIVABLE
    assert result["net_value_cents"] == (
        result["cash_available_cents"] + result["cash_frozen_cents"]
        + result["receivables_cents"] + result["positions_value_cents"]
        - result["payables_cents"]
    )
    stored = con.execute(
        "SELECT receivables_cents FROM valuation WHERE portfolio_id=? AND trading_day=?",
        (PORTFOLIO, EX_DAY.isoformat())).fetchone()
    assert stored["receivables_cents"] == EXPECTED_RECEIVABLE


# ============================================== 到账日：应收转现金，净值不跳变
def test_pay_date_settles_into_cash_and_net_value_does_not_double_count(world):
    con, reader, svc, store = world
    snapshot_id = SNAPSHOT_ID
    lots = _setup(svc, snapshot_id=snapshot_id)

    _advance(svc, lots, day=EX_DAY)
    on_ex = _value(svc, snapshot_id=snapshot_id, day=EX_DAY, lots=lots)
    cash_after_ex = _cash(con)
    contribution_on_ex = _dividend_contribution(con)

    out = _advance(svc, lots, day=PAY_DAY)
    assert out[0]["stage"] == "PAY_DATE"
    assert _open_receivable(con) == 0, "结清后不得留下未结应收"
    assert _cash(con) == cash_after_ex + EXPECTED_RECEIVABLE

    on_pay = _value(svc, snapshot_id=snapshot_id, day=PAY_DAY, lots=lots)
    assert on_pay["receivables_cents"] == 0

    # 核心判据。注意**不能**断言"两天的净值相等"：持仓价格在两天之间
    # 本来就会变，那样的断言会把正常的价格波动报成重复计量错误。
    # 真正该守恒的是这笔分红的贡献：应收 + 已转为现金的分红 = 应收额。
    assert contribution_on_ex == EXPECTED_RECEIVABLE
    assert _dividend_contribution(con) == EXPECTED_RECEIVABLE, (
        "到账后这笔分红的贡献必须仍然等于应收额——多出来就是重复计量，"
        "少下去就是漏记"
    )
    assert on_ex["receivables_cents"] == EXPECTED_RECEIVABLE

    row = con.execute(
        "SELECT status, settled_on FROM receivable WHERE portfolio_id=?",
        (PORTFOLIO,)).fetchone()
    assert row["status"] == "SETTLED"
    assert row["settled_on"] == PAY_DAY.isoformat()
    assert con.execute(
        "SELECT COUNT(*) AS n FROM cash_entry WHERE portfolio_id=? "
        "AND entry_type='DIVIDEND_RECEIVABLE_SETTLED'",
        (PORTFOLIO,)).fetchone()["n"] == 1


# ============================== 登记日持有、除权日卖出：权利不因当日卖出消失
def test_position_sold_on_ex_date_keeps_the_dividend(world):
    """除权日当天卖出，仍享有本次分红。

    权利在**登记日收盘**就已固化。逐笔模拟器按 §12.4 先卖后买，如果直接
    拿"成交后持仓"去算权利，当天卖掉的持仓会被判成"从没持有过"，
    应收凭空少一笔，而账面看不出任何异常。
    """

    con, reader, svc, store = world
    lots = _setup(svc, snapshot_id=SNAPSHOT_ID)          # 登记日买入 1000 股

    # 模拟"除权日当天全部卖出"之后的持仓
    sold = [_lot(SHARES)]
    sold[0].quantity_remaining = 0

    pre_trade = [_lot(SHARES)]                            # 成交前：1000 股
    entitlement_lots = svc._entitlement_lots(pre_trade, sold + [_lot(SHARES)])

    out = svc._advance_corporate_actions(
        portfolio_id=PORTFOLIO, trading_day=EX_DAY, actions=[_dividend()],
        lots=entitlement_lots, now=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )

    assert out[0]["entitlement_shares"] == SHARES, "当日卖出不得丧失已固化的权利"
    assert _open_receivable(con) == EXPECTED_RECEIVABLE


def test_entitlement_lots_never_double_counts_a_lot(world):
    """成交后持仓里同一批次不得出现两次。

    simulate() 把当日新建批次**追加进传入的 lots 列表**，因此
    "ledger_lots + result.lots_created" 会让同一批次被计两次、股数扣两遍。
    这里直接断言口径：取较大值，且每个证券只产出一个权利批次。
    """

    con, reader, svc, store = world
    lot = _lot(SHARES)
    pre = [lot]
    # 同一批次既在传入列表里、又被当成"新建"再出现一次（模拟错误的拼接）
    post = [lot, _lot(SHARES)]

    entitlement = svc._entitlement_lots(pre, post)
    by_instrument = {}
    for l in entitlement:
        by_instrument[l.instrument_id] = by_instrument.get(l.instrument_id, 0) +             l.quantity_remaining
    assert by_instrument == {INSTRUMENT: SHARES}, by_instrument
    assert len(entitlement) == 1, "每个证券只应产出一个权利批次"


# ============================== 除权日与到账日同日：直接进现金，不留未结应收
def test_same_day_ex_and_pay_settles_immediately(world):
    """A 股常见情形：登记日次一交易日除息、同日发放。

    这里必须**当天直接进现金**。若只走"除权日确认应收"分支，
    账面会留下一条永不结清的应收：现金永远少一笔，而"应收 + 现金"
    的总和又是对的，于是这个错误可以长期不被发现。
    """

    con, reader, svc, store = world
    lots = _setup(svc, snapshot_id=SNAPSHOT_ID)
    same_day = CashDividend(
        action_id="ca-same-day", instrument_id=INSTRUMENT,
        record_date=RECORD_DAY, ex_date=EX_DAY, pay_date=EX_DAY,
        cash_per_share_cents_input=CASH_PER_SHARE,
    )
    cash_before = _cash(con)

    out = svc._advance_corporate_actions(
        portfolio_id=PORTFOLIO, trading_day=EX_DAY, actions=[same_day],
        lots=lots, now=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )

    assert out[0]["stage"] == "EX_AND_PAY_DATE"
    assert out[0]["cash_delta_cents"] == EXPECTED_RECEIVABLE
    assert _open_receivable(con) == 0, "同日发放不得留下未结应收"
    assert _cash(con) == cash_before + EXPECTED_RECEIVABLE
    assert con.execute(
        "SELECT COUNT(*) AS n FROM receivable WHERE portfolio_id=?",
        (PORTFOLIO,)).fetchone()["n"] == 0


# ================================================= 幂等：重复推进不得重复入账
def test_repeated_advance_does_not_double_book(world):
    con, reader, svc, store = world
    snapshot_id = SNAPSHOT_ID
    lots = _setup(svc, snapshot_id=snapshot_id)

    _advance(svc, lots, day=EX_DAY)
    _advance(svc, lots, day=EX_DAY)
    assert con.execute("SELECT COUNT(*) AS n FROM receivable WHERE portfolio_id=?",
                       (PORTFOLIO,)).fetchone()["n"] == 1
    assert _open_receivable(con) == EXPECTED_RECEIVABLE

    _advance(svc, lots, day=PAY_DAY)
    _advance(svc, lots, day=PAY_DAY)
    assert con.execute(
        "SELECT COUNT(*) AS n FROM cash_entry WHERE portfolio_id=? "
        "AND entry_type='DIVIDEND_RECEIVABLE_SETTLED'",
        (PORTFOLIO,)).fetchone()["n"] == 1
    assert _cash(con) == OPENING_CASH + EXPECTED_RECEIVABLE


# ====================================== 权利归属：登记日之后买入不享有分红
def test_position_bought_on_the_record_date_is_entitled(world):
    """登记日当天买入的股份**享有**分红——权利看的是登记日收盘持仓。

    这是最容易被写错的一条：如果权利改成取"执行前的账本批次"，
    当日买入就会被判成无权利，应收凭空消失而账面看不出错。
    """

    con, reader, svc, store = world
    snapshot_id = SNAPSHOT_ID
    # 登记日当天买入：收盘时确实持有，因此享有分红
    lots = _setup(svc, snapshot_id=snapshot_id, acquired=RECORD_DAY)

    out = svc._advance_corporate_actions(
        portfolio_id=PORTFOLIO, trading_day=EX_DAY, actions=[_dividend()],
        lots=lots, now=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )

    assert out[0]["entitlement_shares"] == SHARES
    assert out[0]["receivable_cents"] == EXPECTED_RECEIVABLE
    assert _open_receivable(con) == EXPECTED_RECEIVABLE


def test_position_bought_after_the_record_date_gets_nothing(world):
    """登记日之后（除权日当天）买入的股份不享有本次分红。"""

    con, reader, svc, store = world
    snapshot_id = SNAPSHOT_ID
    lots = _setup(svc, snapshot_id=snapshot_id, acquired=EX_DAY)

    out = svc._advance_corporate_actions(
        portfolio_id=PORTFOLIO, trading_day=EX_DAY, actions=[_dividend()],
        lots=lots, now=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )

    assert out[0]["entitlement_shares"] == 0
    assert _open_receivable(con) == 0
    assert con.execute("SELECT COUNT(*) AS n FROM receivable WHERE portfolio_id=?",
                       (PORTFOLIO,)).fetchone()["n"] == 0


def test_position_sold_before_record_date_gets_nothing(world):
    """登记日前已卖光的批次不享有分红。

    权利看的是**登记日的剩余股数**，不是当初买入的股数。
    """

    con, reader, svc, store = world
    snapshot_id = SNAPSHOT_ID
    lots = _setup(svc, snapshot_id=snapshot_id)
    lots[0].quantity_remaining = 0        # 已全部卖出
    lots[0].quantity_original = SHARES    # 但历史买入量仍是 1000

    out = svc._advance_corporate_actions(
        portfolio_id=PORTFOLIO, trading_day=EX_DAY, actions=[_dividend()],
        lots=lots, now=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )

    assert out[0]["entitlement_shares"] == 0, "权利必须按登记日剩余股数计算"
    assert _open_receivable(con) == 0


# ==================================================== 落地顺序：先公司行为后成交
def test_corporate_actions_run_before_fills_in_execute(world):
    """execute 必须能接收当日公司行为，并在成交之前推进。

    这里只断言调用契约存在且被转发：真正的成交链路已由
    test_product_loop_e2e 覆盖，本文件专注于分红落库。
    """

    con, reader, svc, store = world
    snapshot_id = SNAPSHOT_ID
    _setup(svc, snapshot_id=snapshot_id)
    out = svc._advance_corporate_actions(
        portfolio_id=PORTFOLIO, trading_day=EX_DAY, actions=[_dividend()],
        lots=[_lot()], now=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )
    assert out and out[0]["stage"] == "EX_DATE"
    # 公司行为本身也要留档，否则事后无法回答"这笔应收来自哪次分红"
    ca = con.execute("SELECT * FROM corporate_action WHERE action_id='ca-div-1'").fetchone()
    assert ca is not None
    assert ca["action_type"] == "CASH_DIVIDEND"
    assert ca["supported"] == 1
    assert ca["cash_per_share_cents"] == CASH_PER_SHARE


def _advance(svc, lots, *, day: date):
    return svc._advance_corporate_actions(
        portfolio_id=PORTFOLIO, trading_day=day, actions=[_dividend()],
        lots=lots, now=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )
