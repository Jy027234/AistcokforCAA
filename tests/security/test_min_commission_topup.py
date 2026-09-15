"""最低佣金补足必须能记进账本（主文档 §12.6、S01/S10）。

这条用例来自一次**只有跨日才暴露**的真实事故。

现象（L1）
----------
多日模拟跑到第二天，`POST /plans/{id}/execute` 返回 500，而且不是业务错误码——
是数据库 CHECK 约束在写账那一刻炸的：

    sqlite3.IntegrityError: CHECK constraint failed: entry_type IN (...)

原因（L3）
----------
`DailySimulator` 把每一条费用行**原样当作分录类型**写入 cash_entry：

    result.cash_entries.append(CashEntry(line.fee_code, -line.amount_cents, ...))

而费用码 MIN_COMMISSION_TOPUP（§12.6 最低佣金补足）不在
cash_entry.entry_type 的 CHECK 白名单里。于是：

  * 成交金额大、佣金超过最低值 -> 只写 COMMISSION -> 一切正常；
  * 成交金额小、需要补足最低佣金 -> 多出 MIN_COMMISSION_TOPUP -> 写账失败。

第一天恰好每笔都超过最低佣金，单日验收因此全绿；第二天出现一笔小额成交，
账就记不下来了。**"成交已经算出"与"账记得下来"之间那道缝，正是这里。**

本用例锁住的不变量
------------------
1. 费用表产出的**每一个** fee_code 都能作为 entry_type 落库（枚举包含关系）；
   `fees.py` 里写出的费用码也必须在白名单内——两处枚举不许各自漂移；
2. 一笔真实触发最低佣金补足的成交，能完整走完 preview -> freeze -> execute；
3. 账本里每条 fee_charge 都有一条等额现金分录，不能只对总额。
"""

from __future__ import annotations

import inspect
import re
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.data.ingest import SnapshotBuilder
from aquant.domain.data.reader import SnapshotReader
from aquant.domain.data.snapshot import SnapshotStore
from aquant.domain.portfolio.construction import Candidate, ConstructionParams
from aquant.domain.portfolio.plan import PlanService
from aquant.domain.simulation import fees as fees_module
from aquant.domain.simulation.fees import synthetic_fee_table
from aquant.domain.simulation.simulator import BoardRule

from tests.integration.test_m1_ingest_e2e import build_snapshot

AS_OF = datetime(2026, 9, 11, 12, 30, tzinfo=timezone.utc)
TRADING_DAY = date(2026, 9, 8)
SNAPSHOT_ID = "snap-syn-001"

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
CANDIDATES = [
    Candidate("SYN.A.600519", "IND_FOOD", 0.90),
    Candidate("SYN.A.000001", "IND_BANK", 0.70),
]

# 1,000,000 分 = 10,000 元。这个资金下组合构建器会给 SYN.A.000001
# （开盘 11.60 元）下 100 股的单：成交额约 117,200 分，比例佣金仅 29 分，
# 远低于 500 分最低值——这是触发最低佣金补足的最小确定场景。
OPENING_CASH = 1_000_000
PORTFOLIO = "pf-mincomm-M"

FEE_ENTRY_TYPES = (
    "COMMISSION", "MIN_COMMISSION_TOPUP", "STAMP_DUTY", "TRANSFER_FEE", "OTHER",
)


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


def _schema_entry_types(con) -> set[str]:
    """从真实 schema 里取 cash_entry.entry_type 的白名单。

    刻意不在这里再写一份常量：写第二份就又多了一个漂移点，
    而本用例要防的恰恰是这种漂移。
    """

    ddl = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='cash_entry'"
    ).fetchone()[0]
    m = re.search(r"entry_type\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(\s*"
                  r"entry_type\s+IN\s*\((.*?)\)\s*\)", ddl, re.S)
    assert m, "cash_entry.entry_type 的 CHECK 白名单解析失败：" + ddl[:200]
    return set(re.findall(r"'([A-Z_]+)'", m.group(1)))


def _do_preview(svc):
    return svc.preview(
        portfolio_id=PORTFOLIO, snapshot_id=SNAPSHOT_ID, trading_day=TRADING_DAY,
        as_of=AS_OF, candidates=CANDIDATES, cash_available_cents=OPENING_CASH,
        lots=[], confirm_subject="user:alice",
    )


# ------------------------------------------------------ 1. 枚举包含关系
def test_fees_table_codes_are_valid_cash_entry_types(world):
    """费率表实际算出来的费用码，必须都能作为分录类型落库。

    这条断言如果在事故之前就存在，事故根本不会发生：
    它不依赖跑出某笔特定成交，成本接近于零。
    """

    con, _reader, _svc, _store = world
    entry_types = _schema_entry_types(con)

    produced: set[str] = set()
    table = synthetic_fee_table()
    for sched in table._schedules:
        for side in ("BUY", "SELL"):
            # 每档费率都用"佣金不足最低值"的价格，确保补足分支被走到
            charge = table.compute(side=side, quantity=100, price_cents=1160,
                                   trading_day=sched.effective_from)
            produced.update(line.fee_code for line in charge.lines)
    assert produced, "费率表没有产出任何费用码"

    missing = produced - entry_types
    assert not missing, (
        "这些费用码无法作为 cash_entry.entry_type 落库，写账时会抛 CHECK 失败："
        + repr(sorted(missing)))


def test_fees_module_literals_are_valid_cash_entry_types(world):
    """fees.py 是本产品唯一产出费用码的地方，它写的字面量必须在白名单内。"""

    con, _reader, _svc, _store = world
    entry_types = _schema_entry_types(con)
    codes = set(re.findall(r'FeeLine\("([A-Z_]+)"', inspect.getsource(fees_module)))
    assert codes, "没有从 fees.py 解析出任何费用码"
    assert codes <= entry_types, (
        "fees.py 产出的费用码不在 schema 白名单里：" + repr(sorted(codes - entry_types)))


# ------------------------------------------- 2. 真实成交走完整链路并逐项入账
def test_minimum_commission_topup_is_booked_and_persisted(world):
    """触发最低佣金补足的成交必须能走完全链路，并逐项记入账本。"""

    con, _reader, svc, _store = world

    pv = _do_preview(svc)
    assert pv.orders, "该资金下应当产生建仓订单"
    assert pv.estimated_fees_cents > 0

    token = svc.issue_confirmation(preview=pv, subject="user:alice",
                                   current_lots=[], current_cash_cents=OPENING_CASH)
    svc.freeze(preview=pv, confirm_subject="user:alice", confirmation_token=token,
               expected_account_version=pv.account_version, current_lots=[],
               current_cash_cents=OPENING_CASH)

    ex = svc.execute(plan_id=pv.plan_id, lots=[], cash_available_cents=OPENING_CASH)
    assert ex["status"] == "EXECUTED"

    by_code = {
        r["fee_code"]: int(r["total"]) for r in con.execute(
            "SELECT fc.fee_code, SUM(fc.amount_cents) AS total FROM fee_charge fc "
            "JOIN fill f ON f.fill_id=fc.fill_id WHERE f.portfolio_id=? "
            "GROUP BY fc.fee_code", (PORTFOLIO,))
    }
    assert "MIN_COMMISSION_TOPUP" in by_code, (
        "本用例的场景必须真的触发最低佣金补足，否则它没有覆盖到事故路径；"
        "实际费用码：" + repr(sorted(by_code)))

    # 逐项对账：每条费用都要有一条**等额**的现金分录（流出为负）
    for code, total in by_code.items():
        booked = int(con.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS t FROM cash_entry "
            "WHERE portfolio_id=? AND entry_type=?", (PORTFOLIO, code)).fetchone()["t"])
        assert booked == -total, (
            f"费用码 {code} 的费用额 {total} 与现金分录 {booked} 不等额")

    # 汇总也不能漏
    fee_total = sum(by_code.values())
    booked_fees = sum(
        int(r["t"]) for r in con.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS t FROM cash_entry "
            "WHERE portfolio_id=? AND entry_type IN "
            "('COMMISSION','MIN_COMMISSION_TOPUP','STAMP_DUTY','TRANSFER_FEE','OTHER') "
            "GROUP BY entry_type", (PORTFOLIO,)))
    assert booked_fees == -fee_total, (
        f"账本费用合计 {booked_fees} 与费用表 {fee_total} 不一致")
