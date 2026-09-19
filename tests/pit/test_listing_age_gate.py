"""§3.1 上市天数门槛：`exclude_listing_days` 必须**真的生效**。

为什么这条用例存在
-----------------
`ConstructionParams.exclude_listing_days = 120` 写在配置里很久了，
但全仓库没有任何一行读它。两种情形叠加，让它长期不可见：

  1. 免费源没有采集上市日期，`listed_on` 恒为 None——即使有人去读这个
     参数，也没有数据可判；
  2. 参数"存在"本身就会让读者以为规则已实现。

这与 `is_simulatable` 曾经那条 `if self.listed_on is not None: return False`
是同一类缺陷的两个面：那里把**信息**当成排除条件，这里把**配置**当成实现。

因此本用例固定的不是"函数能算"，而是三条口径：
  * 按**交易日**算，不是自然日（120 自然日 ≈ 80 交易日，差三分之一）；
  * 缺上市日期时**保留但留痕**——"不知道"既不是合格也不是不合格；
  * 日历缺失时**拒绝**，不退回自然日近似。
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.data.db import apply_migrations, connect  # noqa: E402
from aquant.domain.data.ingest import SnapshotBuilder  # noqa: E402
from aquant.domain.data.reader import SnapshotReader  # noqa: E402
from aquant.domain.data.snapshot import SnapshotStore  # noqa: E402
from aquant.domain.portfolio.construction import (  # noqa: E402
    Candidate,
    ConstructionParams,
    listed_trading_days,
)
from aquant.domain.portfolio.plan import PlanError, PlanService  # noqa: E402
from aquant.domain.simulation.fees import synthetic_fee_table  # noqa: E402
from aquant.domain.simulation.simulator import BoardRule  # noqa: E402

from tests.integration.test_m1_ingest_e2e import (  # noqa: E402
    EXAMPLE,
    build_snapshot,
)

#: 合成快照的交易日（examples/snap-syn-001.yaml）。
SYN_CALENDAR = [date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9),
                date(2026, 9, 10), date(2026, 9, 11)]
TRADING_DAY = date(2026, 9, 11)
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
}


# ====================================================================== 口径
def test_counts_trading_days_not_calendar_days():
    """闭区间 [listed_on, trading_day]，且只数日历里的日子。"""

    calendar = SYN_CALENDAR
    # 上市当日算第 1 天（与 is_simulatable「上市当日起可模拟」一致）
    assert listed_trading_days(listed_on=date(2026, 9, 8),
                               trading_day=date(2026, 9, 11), calendar=calendar) == 4
    assert listed_trading_days(listed_on=date(2026, 9, 11),
                               trading_day=date(2026, 9, 11), calendar=calendar) == 1
    # 上市日晚于决策日：算 0，不是负数
    assert listed_trading_days(listed_on=date(2026, 9, 12),
                               trading_day=date(2026, 9, 11), calendar=calendar) == 0


def test_natural_days_would_overstate_the_window():
    """自然日近似会把门槛放宽——这条用例的作用是让那种改法失败。

    2026-09-07 到 2026-09-11 是 5 个自然日、5 个交易日，两者在这里恰好相近；
    真正拉开差距的是窗口外的日期：2026-06-01 到 09-11 是 102 个自然日，
    而快照日历里只有 5 个交易日。若有人把实现改成
    `(trading_day - listed_on).days`，本断言立刻失败。
    """

    age = listed_trading_days(listed_on=date(2026, 6, 1),
                              trading_day=date(2026, 9, 11), calendar=SYN_CALENDAR)
    assert age == 5
    assert age != (date(2026, 9, 11) - date(2026, 6, 1)).days


# ====================================================================== 接线
@pytest.fixture()
def world(tmp_path):
    """合成快照 + 人为写入上市日期的研究池。

    刻意**不**改 examples/snap-syn-001.yaml：那份示例被十几处引用，
    给它加上市日期会让所有既有用例的池子按新规则被清空。
    """

    con = connect(tmp_path / "meta.sqlite")
    apply_migrations(con)
    root = tmp_path / "api"
    root.mkdir()
    store = SnapshotStore(con, root)
    builder = SnapshotBuilder(con, root / "datasets")
    doc, _report = build_snapshot(con, builder, store)
    reader = SnapshotReader(store)

    def patch(overrides: dict[str, str | None]) -> dict:
        """把上市日期写进 instruments 数据集并**重新发布**。

        `listed_on` 会随 instruments.json 一起改，因此必须重新写数据集、
        重新发布——只改内存里的 dict 会让"读得到"变成一句空话。
        """

        import json

        instruments = json.loads(json.dumps(doc["instruments"]))
        for inst in instruments:
            if inst["instrument_id"] in overrides:
                inst["listed_on"] = overrides[inst["instrument_id"]]
        path = (root / "datasets" / "snap-syn-001" / "instruments.json")
        body = json.dumps(instruments, ensure_ascii=False, sort_keys=True,
                          indent=2).encode("utf-8")
        path.write_bytes(body)
        import hashlib

        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        con.execute("UPDATE snapshot_dataset SET sha256=? WHERE snapshot_id=? AND name=?",
                    (digest, "snap-syn-001", "instruments"))
        con.commit()
        return {i["instrument_id"]: i.get("listed_on") for i in instruments}

    yield con, reader, patch

    con.close()


def _service(con, reader, *, exclude_listing_days: int = 120) -> PlanService:
    return PlanService(
        con, reader, synthetic_fee_table(), RULES, LISTINGS,
        ConstructionParams(max_holdings=3, max_single_name_pct=Decimal("20"),
                           max_single_industry_pct=Decimal("50"),
                           exclude_listing_days=exclude_listing_days),
    )


CANDIDATES = [
    Candidate("SYN.A.600519", "IND_FOOD", 0.90),
    Candidate("SYN.A.000001", "IND_BANK", 0.70),
]


def _preview(svc):
    return svc.preview(
        portfolio_id="pf-listing-age", snapshot_id="snap-syn-001",
        trading_day=TRADING_DAY, as_of=AS_OF, candidates=CANDIDATES,
        cash_available_cents=100_000_000, lots=[], confirm_subject="user:alice",
    )


def test_recent_listing_is_excluded_from_targets(world):
    """上市日落在快照窗口**之内** -> 窗口内天数不足 -> 排除，理由可读。"""

    con, reader, patch = world
    patch({"SYN.A.600519": "2026-09-08", "SYN.A.000001": "2001-08-27"})
    svc = _service(con, reader)
    pv = _preview(svc)

    excluded = {e["instrument_id"]: e for e in pv.excluded}
    assert "SYN.A.600519" in excluded, "刚上市的标的必须被排除"
    assert excluded["SYN.A.600519"]["reason"] == "LISTED_TOO_RECENTLY"
    assert "2026-09-08" in excluded["SYN.A.600519"]["detail"]
    assert "4 个交易日" in excluded["SYN.A.600519"]["detail"]
    assert "SYN.A.000001" not in excluded
    assert [t.instrument_id for t in pv.targets] == ["SYN.A.000001"]


def test_old_listing_is_not_excluded_but_its_window_is_disclosed(world):
    """窗口外上市：不排除，但必须说出"门槛判不了"。

    门槛是 120 个交易日，而快照日历只有 5 天。用快照日历去数一只 2001 年
    上市的股票只能数出 5，若据此排除，整池会被清空——这正是本用例要
    钉住的边界：判不了就说不判，并且把这个不确定性放在预览里。
    """

    con, reader, patch = world
    patch({"SYN.A.600519": "2001-08-27", "SYN.A.000001": "2001-08-27"})
    svc = _service(con, reader)
    pv = _preview(svc)

    assert not pv.excluded, "两只都是窗口外上市的老股，不该有任何排除"
    assert len(pv.targets) == 2
    note = next((n for n in pv.notes if "不足以判定" in n), None)
    assert note, f"窗口太短必须留痕，实际 notes={pv.notes}"
    assert "2 只标的" in note


def test_listing_before_calendar_start_is_proven_old_when_coverage_is_enough(
    world, monkeypatch: pytest.MonkeyPatch,
):
    """日历覆盖已达到门槛时，起点前上市可由单调性证明达标。"""

    con, reader, patch = world
    patch({"SYN.A.600519": "2001-08-27", "SYN.A.000001": "2001-08-27"})
    monkeypatch.setattr(
        reader,
        "trading_calendar",
        lambda *_args, **_kwargs: [d.isoformat() for d in SYN_CALENDAR],
    )
    svc = _service(con, reader, exclude_listing_days=len(SYN_CALENDAR))
    pv = _preview(svc)

    assert not pv.excluded
    assert not [n for n in pv.notes if "不足以判定" in n]


def test_future_listing_date_is_excluded_as_not_listed_yet(world):
    """上市日晚于决策日：那不是"新"，是"还没有"。"""

    con, reader, patch = world
    patch({"SYN.A.600519": "2026-12-01", "SYN.A.000001": "2001-08-27"})
    svc = _service(con, reader)
    pv = _preview(svc)

    excluded = {e["instrument_id"]: e for e in pv.excluded}
    assert excluded["SYN.A.600519"]["reason"] == "NOT_LISTED_YET"


def test_unknown_listing_date_is_kept_and_disclosed(world):
    """缺上市日期：保留，但必须在 notes 里说出来。

    把"不知道"当成"不合格"会清空整池（真实数据上 900 只全为 NULL 时
    就是这样）；当成"合格"而不留痕，则是拿未知做准入判断。
    """

    con, reader, patch = world
    patch({"SYN.A.600519": None, "SYN.A.000001": "2001-08-27"})
    svc = _service(con, reader)
    pv = _preview(svc)

    assert not [e for e in pv.excluded if e["reason"] == "LISTED_TOO_RECENTLY"]
    assert len(pv.targets) == 2
    note = next((n for n in pv.notes if "缺少上市日期" in n), None)
    assert note, f"缺上市日期必须留痕，实际 notes={pv.notes}"
    assert "SYN.A.600519" in note


def test_threshold_zero_disables_the_gate_explicitly(world):
    """门槛设 0 = 显式声明"不按上市天数筛"，不是"实现没了"。"""

    con, reader, patch = world
    patch({"SYN.A.600519": "2026-09-08", "SYN.A.000001": "2001-08-27"})
    svc = _service(con, reader, exclude_listing_days=0)
    pv = _preview(svc)

    assert not [e for e in pv.excluded if e["reason"] == "LISTED_TOO_RECENTLY"]
    assert len(pv.targets) == 2


def test_missing_calendar_refuses_instead_of_approximating(world):
    """没有交易日历就必须拒绝，不得退回自然日近似。"""

    con, reader, patch = world
    patch({"SYN.A.600519": "2026-09-08", "SYN.A.000001": "2001-08-27"})
    # 把交易日历数据集指向一个不存在的文件：读取必然失败。
    con.execute("UPDATE snapshot_dataset SET path='datasets/snap-syn-001/missing.json' "
                "WHERE snapshot_id='snap-syn-001' AND name='trading_calendar'")
    con.commit()
    svc = _service(con, reader)

    with pytest.raises(PlanError) as exc:
        _preview(svc)
    assert exc.value.code == "DATA_NOT_READY"
    assert "trading calendar" in exc.value.message
    assert "exclude_listing_days" in exc.value.repair_action
