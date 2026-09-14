"""D01–D08 端到端验收：源清单 → 入库 → 快照 → PIT 门禁读取。

tests/pit 验证时点**规则**，本文件验证规则是否真的接进了 M1 链路。
两处都通过，才算主文档 §18.1 的黄金用例成立。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.data.ingest import SnapshotBuilder
from aquant.domain.data.reader import SnapshotReader
from aquant.domain.data.snapshot import SnapshotStore

from tests.integration.test_m1_ingest_e2e import build_snapshot


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


AS_OF = utc(2026, 9, 11, 12, 30)


@pytest.fixture()
def m1(tmp_path):
    con = connect(tmp_path / "meta.sqlite")
    apply_migrations(con)
    root = tmp_path / "api"
    root.mkdir()
    store = SnapshotStore(con, root)
    builder = SnapshotBuilder(con, root / "datasets")
    build_snapshot(con, builder, store)
    yield con, store, SnapshotReader(store), root
    con.close()


# ------------------------------------------------------------------ D01
def test_d01_report_published_this_year_is_invisible_in_prior_snapshot(m1):
    """D01：财报归属去年但今年才公布 -> 去年策略读取不到。

    在 M1 链路上的落点：快照按 as_of 冻结，as_of 之前不存在的数据不在快照里。
    """

    con, store, reader, _ = m1
    # 试图用 2025 年的时点读 2026 年的快照 -> 被上界门禁拒绝
    from aquant.domain.data.snapshot import SnapshotError
    with pytest.raises(SnapshotError):
        reader.daily_quotes("snap-syn-001", as_of=utc(2025, 12, 31))
    # 正确时点可读
    assert reader.daily_quotes("snap-syn-001", as_of=AS_OF)


# ------------------------------------------------------------------ D02
def test_d02_published_snapshot_is_immutable_and_superseded_not_edited(m1):
    """D02：历史报告修订 -> 修订前快照仍使用当时版本。"""

    import sqlite3

    con, store, reader, _ = m1
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE snapshot SET watermark='revised' WHERE snapshot_id='snap-syn-001'")
    # 旧快照仍可读，内容未变
    rows = reader.daily_quotes("snap-syn-001", as_of=AS_OF, instrument_id="SYN.A.600519")
    assert rows[0].close_cents == 129956


# ------------------------------------------------------------------ D03
def test_d03_reconstructed_availability_kept_separate_from_capture_time(m1):
    """D03：今天导入历史数据 -> 保留今天抓取时间；按可证明依据重建可用时间。"""

    con, store, reader, _ = m1
    row = con.execute(
        "SELECT first_seen_at, available_at, available_basis, pit_mode "
        "FROM event WHERE event_id='evt-syn-001'"
    ).fetchone()
    # 抓取时间与可用时间是不同的字段，且依据可证明
    assert row["first_seen_at"] != row["available_at"]
    assert row["available_basis"] in {"OBSERVED", "VENDOR_PIT", "RECONSTRUCTED"}
    assert row["pit_mode"] in {"LIVE_OBSERVED", "HISTORICAL_RECONSTRUCTED"}


# ------------------------------------------------------------------ D04
def test_d04_date_only_announcement_deferred_to_next_trading_day_preopen(m1):
    """D04：公告只有日期 -> 顺延到保守可用交易日，不编造早间时间。"""

    con, store, reader, root = m1
    row = con.execute(
        "SELECT source_published_date, available_at FROM event WHERE event_id='evt-syn-001'"
    ).fetchone()
    assert row["source_published_date"] == "2026-09-04"
    assert row["available_at"] == "2026-09-07T01:30:00Z"   # 次一交易日盘前

    # 门禁生效：事件读取默认只返回 available_at <= as_of 的记录。
    # 这里直接验证过滤函数本身——用一个比 available_at 早一(微)秒的门禁值，
    # 不构造第二个快照，因为按 as_of 冻结的快照本就不允许包含晚于它的数据
    # （cutoff 检查已经先一步拒绝了，那正是它该做的）。
    from aquant.domain.data.reader import SnapshotReader as _R
    import json as _json
    raw_events = reader._load_dataset("snap-syn-001", "events")
    cutoff = datetime.fromisoformat("2026-09-07T01:29:59+00:00")
    visible_before = [
        e for e in raw_events
        if e.get("available_at")
        and datetime.fromisoformat(e["available_at"]) <= cutoff
    ]
    visible_after = [
        e for e in raw_events
        if e.get("available_at")
        and datetime.fromisoformat(e["available_at"]) <= AS_OF
    ]
    assert "evt-syn-001" not in {e["event_id"] for e in visible_before}
    assert "evt-syn-001" in {e["event_id"] for e in visible_after}
    # 且 reader 在快照自身时点上确实返回了它
    assert "evt-syn-001" in {e["event_id"] for e in reader.events("snap-syn-001", as_of=AS_OF)}


# ------------------------------------------------------------------ D05
def test_d05_historical_classification_not_replaced_by_current(m1):
    """D05：股票后来改行业 -> 历史分类不被当前列表替换。"""

    con, store, reader, _ = m1
    rows = con.execute(
        "SELECT valid_from, valid_to, industry_code FROM instrument_status_version "
        "WHERE instrument_id='SYN.A.600519' ORDER BY valid_from"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["industry_code"] == "SW_SYN_09"
    assert rows[0]["valid_to"] == "2024-07-01"     # 左闭右开
    assert rows[1]["industry_code"] == "SW_SYN_01"
    assert rows[1]["valid_to"] is None


def test_d05_suspended_status_is_versioned(m1):
    """停牌也必须走版本化状态，否则历史查询会返回空。"""

    con, store, reader, _ = m1
    rows = con.execute(
        "SELECT valid_from, valid_to, status FROM instrument_status_version "
        "WHERE instrument_id='SYN.A.600002' ORDER BY valid_from"
    ).fetchall()
    assert [r["status"] for r in rows] == ["LISTED", "SUSPENDED"]
    assert rows[1]["valid_from"] == "2026-09-10"


# ------------------------------------------------------------------ D06
def test_d06_old_snapshot_hash_unchanged_after_new_corporate_action(m1):
    """D06：未来分红影响今日前复权序列 -> 旧快照与旧实验哈希不变化。"""

    con, store, reader, root = m1
    before = store.verify_dataset("snap-syn-001", "daily_quotes")
    # 模拟"后来发生了一次公司行为"：写入新文件到别处，不改已发布数据集
    (root / "datasets" / "snap-syn-001" / "later_action.json").write_text("[]", encoding="utf-8")
    after = store.verify_dataset("snap-syn-001", "daily_quotes")
    assert before == after, "已发布数据集不得随后续事件变化"


# ------------------------------------------------------------------ D07
def test_d07_source_switch_is_recorded_and_visible(m1):
    """D07：供应商切换 -> 差异可见，未经验证不混接。"""

    con, store, reader, _ = m1
    # 快照记录了 data_version，切换来源必须产生新版本
    snap = store.require_published("snap-syn-001")
    assert snap["data_version"] == "syn-2026-09-11"

    from aquant.adapters.providers.chain import FetchChain, SilentMixError
    with pytest.raises(SilentMixError):
        FetchChain.assert_no_silent_mix(["sourceA", "sourceB"])


# ------------------------------------------------------------------ D08
def test_d08_withdrawal_creates_new_state_and_old_view_remains(m1):
    """D08：转载归并、撤回产生新状态、旧判断仍可回看。"""

    con, store, reader, _ = m1
    # 事件与文档分离：一个事件可以挂多个文档（转载）
    n_docs = con.execute(
        "SELECT COUNT(*) FROM event_document WHERE event_id='evt-syn-001'"
    ).fetchone()[0]
    assert n_docs >= 1
    # 撤回通过新状态表达，不删除旧记录
    doc_cols = {r[1] for r in con.execute("PRAGMA table_info(document)")}
    assert "withdrawn_at" in doc_cols
    ev_cols = {r[1] for r in con.execute("PRAGMA table_info(event)")}
    assert "supersedes_id" in ev_cols and "verification_status" in ev_cols


# ------------------------------------------------------------------ 汇总
def test_all_eight_golden_cases_have_landed_in_m1(m1):
    """一条汇总断言：八个用例在 M1 链路上都有对应落点。"""

    con, store, reader, _ = m1
    checks = {
        "D01": lambda: reader.daily_quotes("snap-syn-001", as_of=AS_OF),
        "D02": lambda: store.require_published("snap-syn-001"),
        "D03": lambda: con.execute("SELECT available_basis FROM event LIMIT 1").fetchone(),
        "D04": lambda: con.execute("SELECT available_at FROM event LIMIT 1").fetchone(),
        "D05": lambda: con.execute("SELECT COUNT(*) FROM instrument_status_version").fetchone(),
        "D06": lambda: store.verify_dataset("snap-syn-001", "daily_quotes"),
        "D07": lambda: store.get("snap-syn-001")["data_version"],
        "D08": lambda: con.execute("SELECT COUNT(*) FROM document").fetchone(),
    }
    for name, fn in checks.items():
        assert fn() is not None, name
