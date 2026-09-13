"""研究读取的 PIT 门禁测试。

覆盖主文档 §16.2、§15.4、§7.2 与研究卡要求：
读取必须绑定固定快照、时点不得越界、当时不可见的事件不得参与判断。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.data.reader import SnapshotReader
from aquant.domain.data.snapshot import SnapshotError, SnapshotStore

from test_m1_ingest_e2e import EXAMPLE, build_snapshot


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


@pytest.fixture()
def reader_env(tmp_path):
    con = connect(tmp_path / "meta.sqlite")
    apply_migrations(con)
    api_root = tmp_path / "api"
    api_root.mkdir()
    store = SnapshotStore(con, api_root)
    from aquant.domain.data.ingest import SnapshotBuilder
    builder = SnapshotBuilder(con, api_root / "datasets")
    build_snapshot(con, builder, store)
    yield SnapshotReader(store), store, api_root, con
    con.close()


AS_OF = utc(2026, 9, 11, 12, 30)   # = 快照 as_of_time (20:30 +08:00)


# ------------------------------------------------------------------ 基本读取
def test_reader_returns_series_for_instrument(reader_env):
    reader, *_ = reader_env
    rows = reader.daily_quotes("snap-syn-001", as_of=AS_OF, instrument_id="SYN.A.600519")
    assert len(rows) == 5
    assert rows[0].trading_day == date(2026, 9, 7)
    assert rows[0].close_cents == 129956


def test_latest_quote_returns_last_available_day(reader_env):
    reader, *_ = reader_env
    row = reader.latest_quote("snap-syn-001", "SYN.A.600519", as_of=AS_OF)
    assert row is not None and row.trading_day == date(2026, 9, 11)


def test_result_carries_snapshot_identity(reader_env):
    """§16.2 结果必须返回快照 ID，供研究卡关联。"""

    reader, *_ = reader_env
    ref = reader.ref("snap-syn-001")
    assert ref.snapshot_id == "snap-syn-001"
    assert ref.data_mode == "SYNTHETIC"
    assert ref.watermark
    assert ref.as_dict()["as_of_time"].startswith("2026-09-11")


# ------------------------------------------------------------------ as_of 门禁
def test_as_of_later_than_snapshot_is_rejected(reader_env):
    """A11：不能用更晚的时点去读较早的快照。"""

    reader, *_ = reader_env
    with pytest.raises(SnapshotError) as exc:
        reader.daily_quotes("snap-syn-001", as_of=utc(2026, 9, 20))
    assert exc.value.code == "PIT_UNVERIFIED"
    assert "later than snapshot" in exc.value.message


def test_naive_as_of_is_rejected(reader_env):
    reader, *_ = reader_env
    with pytest.raises(SnapshotError) as exc:
        reader.daily_quotes("snap-syn-001", as_of=datetime(2026, 9, 11, 12, 30))
    assert "timezone-aware" in exc.value.message


def test_as_of_exactly_at_upper_bound_is_allowed(reader_env):
    reader, *_ = reader_env
    rows = reader.daily_quotes("snap-syn-001", as_of=AS_OF)
    assert rows


# ------------------------------------------------------------------ 停牌
def test_suspended_day_has_no_row_and_is_reported_suspended(reader_env):
    """S08：停牌缺失即缺失，读者可判定为停牌，但不得用前收填充。"""

    reader, *_ = reader_env
    rows = reader.daily_quotes("snap-syn-001", as_of=AS_OF, instrument_id="SYN.A.600002")
    assert [r.trading_day for r in rows] == [date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9)]
    assert reader.is_suspended_on("snap-syn-001", "SYN.A.600002",
                                  date(2026, 9, 10), as_of=AS_OF) is True
    assert reader.is_suspended_on("snap-syn-001", "SYN.A.600002",
                                  date(2026, 9, 9), as_of=AS_OF) is False


# ------------------------------------------------------------------ 事件门禁
def test_event_selection_filters_by_available_at(reader_env):
    """D04：只有日期的公告，其可用时点是次一交易日盘前。

    2026-09-06（周日的语义位置）看不到 09-04 的公告；
    2026-09-07 盘前（01:30Z）之后才看得到。
    """

    reader, *_ = reader_env
    before = reader.events("snap-syn-001", as_of=AS_OF,
                           instrument_id="SYN.A.600003")  # 无 instrument_id 过滤时需注意
    assert before  # 事件本身存在

    all_events = reader.events("snap-syn-001", as_of=AS_OF)
    ids = {e["event_id"] for e in all_events}
    assert "evt-syn-001" in ids

    # 用一个早于 available_at 的 as_of 不可行（早于快照上界会通过，
    # 但会命中 available_at 过滤）——这里直接验证过滤逻辑本身。
    early = reader.events("snap-syn-001", as_of=datetime.fromisoformat(
        "2026-09-07T01:29:59+00:00"))
    assert "evt-syn-001" not in {e["event_id"] for e in early}


def test_event_visible_after_available_at(reader_env):
    reader, *_ = reader_env
    at = datetime.fromisoformat("2026-09-07T01:30:00+00:00")
    ids = {e["event_id"] for e in reader.events("snap-syn-001", as_of=at)}
    assert "evt-syn-001" in ids


def test_events_can_be_read_unfiltered_for_audit(reader_env):
    """审计场景可读取全部事件，但默认必须是有门禁的。"""

    reader, *_ = reader_env
    gated = reader.events("snap-syn-001", as_of=datetime.fromisoformat(
        "2026-09-07T01:29:59+00:00"))
    ungated = reader.events("snap-syn-001", as_of=datetime.fromisoformat(
        "2026-09-07T01:29:59+00:00"), only_available=False)
    assert len(ungated) >= len(gated)


def test_malicious_event_is_returned_as_data_not_instruction(reader_env):
    """A05：恶意材料照常作为**数据**返回，系统不因其内容获得任何权限。"""

    reader, *_ = reader_env
    events = reader.events("snap-syn-001", as_of=AS_OF)
    mal = next(e for e in events if e["event_id"] == "evt-syn-malicious-001")
    assert mal["verification_status"] == "UNVERIFIED"
    assert mal["market_direction"] == "UNKNOWN"


# ------------------------------------------------------------------ 其他数据集
def test_corporate_actions_are_readable(reader_env):
    reader, *_ = reader_env
    actions = reader.corporate_actions("snap-syn-001", as_of=AS_OF,
                                       instrument_id="SYN.A.600003")
    assert len(actions) == 1
    assert actions[0]["action_type"] == "CASH_DIVIDEND"


def test_trading_calendar_is_readable(reader_env):
    reader, *_ = reader_env
    days = reader.trading_calendar("snap-syn-001", as_of=AS_OF)
    assert days[0] == "2026-09-07" and len(days) == 5


# ------------------------------------------------------------------ 完整性
def test_tampered_dataset_is_detected_on_read(reader_env):
    """读取时校验哈希，替换已发布内容必须被发现。"""

    reader, store, api_root, _ = reader_env
    target = api_root / "datasets" / "snap-syn-001" / "daily_quotes.json"
    target.write_text("[{}]", encoding="utf-8")
    fresh = SnapshotReader(store)      # 绕过缓存
    with pytest.raises(SnapshotError) as exc:
        fresh.daily_quotes("snap-syn-001", as_of=AS_OF)
    assert "hash verification" in exc.value.message


def test_unknown_snapshot_is_rejected(reader_env):
    reader, *_ = reader_env
    with pytest.raises(SnapshotError):
        reader.daily_quotes("snap-nope", as_of=AS_OF)
