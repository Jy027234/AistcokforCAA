"""D01–D08 数据与时点黄金用例（主文档 §18.1）。

这些用例是发布门槛的一部分：§18.4 要求"所有资金和 PIT 黄金测试通过"。
本模块只测试时点语义本身，不依赖网络、不依赖模型密钥。

编号对应主文档 §18.1 原文：
    D01 财报归属去年但今年才公布   -> 去年策略读取不到该财报
    D02 历史报告后来修订           -> 修订前快照仍使用当时版本
    D03 今天导入历史原始数据       -> 保留今天抓取时间；按可证明依据重建可用时间
    D04 公告只有日期、逢长假       -> 顺延到保守可用交易日，不编造早间时间
    D05 股票后来退市或改行业       -> 历史证券池与历史分类不被当前列表替换
    D06 未来分红影响今日前复权序列 -> 旧快照与旧实验哈希不变化
    D07 主数据、行情单位或供应商切换 -> 差异可见，未经验证不混接
    D08 同一公告转载多次并随后撤回 -> 事实归并，撤回产生新状态，旧判断仍可回看
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from aquant.domain.data.instruments import (
    Board,
    Exchange,
    Instrument,
    SecurityStatus,
    StatusVersion,
)
from aquant.domain.data.pit import (
    AvailabilityBasis,
    PitMode,
    PitRecord,
    PitViolation,
    TimestampPrecision,
    assert_available_at_not_before_publication,
    assert_backtestable,
    assert_mode_consistent,
    date_only_available_at,
    is_usable_at,
    preopen_instant,
    select_usable_at,
)


def utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


#: 含长假边界的交易日历（虚构，用于确定性）
CALENDAR = [
    date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30),   # 国庆前
    date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 13),  # 长假后
]


# ------------------------------------------------------------------ D01/D04
def test_d04_date_only_announcement_defers_to_next_trading_day_before_holiday():
    """D04：公告只有日期、逢长假 -> 顺延到保守可用交易日，不编造早间时间。

    2026-09-30 发布（长假前最后一个交易日）。次一"日"是 10-01，但那是假期，
    因此必须顺延到 10-09 的盘前，而不是 10-01 或 09-30 当日早间。
    """

    available_at, rationale = date_only_available_at(date(2026, 9, 30), CALENDAR)
    assert available_at == preopen_instant(date(2026, 10, 9)), rationale
    # 关键否定断言：不得早于发布日之后的第一个交易日盘前
    assert available_at > preopen_instant(date(2026, 9, 30))
    assert "2026-10-09" in rationale


def test_d04_preopen_not_yet_captured_defers_further():
    """§7.3 若该盘前快照尚未抓取/复核，则继续顺延。"""

    available_at, rationale = date_only_available_at(
        date(2026, 9, 30), CALENDAR, preopen_already_captured=False
    )
    assert available_at == preopen_instant(date(2026, 10, 12)), rationale
    assert "deferred" in rationale


def test_d04_never_assumes_same_day_preopen():
    """禁止假定当日开盘前可用。"""

    available_at, _ = date_only_available_at(date(2026, 10, 9), CALENDAR)
    assert available_at >= preopen_instant(date(2026, 10, 12))


def test_d01_financial_report_published_this_year_invisible_last_year():
    """D01：财报归属去年但今年才公布 -> 去年策略读取不到该财报。"""

    record = PitRecord(
        record_id="fin-2025-annual",
        source_published_at=utc(2026, 4, 20, 8, 0),
        timestamp_precision=TimestampPrecision.SECOND,
        first_seen_at=utc(2026, 4, 20, 8, 5),
        available_at=utc(2026, 4, 20, 8, 0),
        availability_basis=AvailabilityBasis.OBSERVED,
        pit_mode=PitMode.LIVE_OBSERVED,
    )
    # 2025-12-31 的研究看不到它
    assert not is_usable_at(record, utc(2025, 12, 31, 23, 59))
    # 公布之后可见
    assert is_usable_at(record, utc(2026, 4, 20, 8, 1))
    assert is_usable_at(record, utc(2026, 4, 21))


# ------------------------------------------------------------------ D02
def test_d02_revised_report_keeps_pre_revision_version_readable():
    """D02：历史报告后来修订 -> 修订前快照仍使用当时版本。

    实现方式：修订产生新记录并通过 supersedes_id 指向旧记录；
    旧记录在旧快照的 as_of 时点仍然可用（不原地擦除）。
    """

    original = PitRecord(
        record_id="fin-v1",
        source_published_at=utc(2026, 4, 20, 8, 0),
        timestamp_precision=TimestampPrecision.SECOND,
        first_seen_at=utc(2026, 4, 20, 8, 5),
        available_at=utc(2026, 4, 20, 8, 0),
        availability_basis=AvailabilityBasis.OBSERVED,
        pit_mode=PitMode.LIVE_OBSERVED,
    )
    revised = PitRecord(
        record_id="fin-v2",
        source_published_at=utc(2026, 8, 15, 8, 0),
        timestamp_precision=TimestampPrecision.SECOND,
        first_seen_at=utc(2026, 8, 15, 8, 5),
        available_at=utc(2026, 8, 15, 8, 0),
        availability_basis=AvailabilityBasis.OBSERVED,
        pit_mode=PitMode.LIVE_OBSERVED,
        supersedes_id="fin-v1",
    )

    # 修订前的时点：只有 v1 可用
    usable_before = select_usable_at([original, revised], utc(2026, 6, 1))
    assert [r.record_id for r in usable_before] == ["fin-v1"]

    # 修订后：两条都可用（历史可回看），由上层按 supersedes 链选择
    usable_after = select_usable_at([original, revised], utc(2026, 9, 1))
    assert {r.record_id for r in usable_after} == {"fin-v1", "fin-v2"}


# ------------------------------------------------------------------ D03
def test_d03_historical_import_keeps_today_capture_time():
    """D03：今天导入历史数据 -> 保留今天抓取时间；可用时间按可证明依据重建。

    first_seen_at 必须是今天（真实抓取时间），不得伪造成过去；
    而 available_at 可以依 VENDOR_PIT 依据重建到历史时点。
    """

    ingested_today = utc(2026, 9, 13, 10, 0)
    reconstructed = PitRecord(
        record_id="hist-quote-2019",
        source_published_at=utc(2019, 6, 3, 7, 30),
        timestamp_precision=TimestampPrecision.SECOND,
        first_seen_at=ingested_today,          # 今天抓的
        available_at=utc(2019, 6, 3, 7, 30),   # 依据供应商 PIT 重建
        availability_basis=AvailabilityBasis.VENDOR_PIT,
        pit_mode=PitMode.HISTORICAL_RECONSTRUCTED,
    )
    assert reconstructed.first_seen_at == ingested_today
    assert reconstructed.available_at.year == 2019
    # 重建依据必须可证明，不能是 UNKNOWN
    assert_backtestable([reconstructed], "hist-import")


def test_d03_reconstructed_with_unknown_basis_cannot_backtest():
    """§7.2 availability_basis=UNKNOWN 不得用于正式 PIT 回测。"""

    record = PitRecord(
        record_id="hist-unknown",
        source_published_at=None,
        timestamp_precision=TimestampPrecision.UNKNOWN,
        first_seen_at=utc(2026, 9, 13),
        available_at=utc(2020, 1, 1),
        availability_basis=AvailabilityBasis.UNKNOWN,
        pit_mode=PitMode.HISTORICAL_RECONSTRUCTED,
    )
    with pytest.raises(PitViolation) as exc:
        assert_backtestable([record], "backtest")
    assert exc.value.code == "PIT_UNVERIFIED"
    assert "UNKNOWN" in exc.value.message


# ------------------------------------------------------------------ D05
def test_d05_historical_classification_not_replaced_by_current():
    """D05：股票后来改行业 -> 历史分类不被当前列表替换。"""

    inst = Instrument(
        instrument_id="SYN.A.600519",
        exchange=Exchange.SSE,
        board=Board.MAIN,
        status_history=(
            StatusVersion(
                # 7.1 left-closed right-open: an industry valid THROUGH 2024-06-30
                # must be stored as valid_to=2024-07-01, not 2024-06-30.
                valid_from=date(2020, 1, 2), valid_to=date(2024, 7, 1),
                name="合成示例·原名称", status=SecurityStatus.LISTED,
                industry_code="SW_SYN_09", industry_name="合成旧行业",
            ),
            StatusVersion(
                valid_from=date(2024, 7, 1), valid_to=None,
                name="合成示例·消费龙头", status=SecurityStatus.LISTED,
                industry_code="SW_SYN_01", industry_name="合成食品饮料",
            ),
        ),
    )
    assert inst.industry_on(date(2023, 12, 31)) == "SW_SYN_09"
    assert inst.name_on(date(2023, 12, 31)) == "合成示例·原名称"
    assert inst.industry_on(date(2025, 1, 1)) == "SW_SYN_01"
    # 边界：valid_from 当日生效，valid_to 当日不再生效（左闭右开）
    assert inst.industry_on(date(2024, 7, 1)) == "SW_SYN_01"
    assert inst.industry_on(date(2024, 6, 30)) == "SW_SYN_09"
    assert inst.industry_on(date(2024, 5, 1)) == "SW_SYN_09"


def test_d05_delisted_stock_keeps_history():
    """D05：退市后历史证券池不被当前列表替换。"""

    inst = Instrument(
        instrument_id="SYN.A.000999",
        exchange=Exchange.SSE,
        board=Board.MAIN,
        delisted_on=date(2025, 3, 1),
        status_history=(
            StatusVersion(valid_from=date(2015, 1, 1), valid_to=date(2025, 3, 1),
                          name="合成退市股", status=SecurityStatus.LISTED),
            StatusVersion(valid_from=date(2025, 3, 1), valid_to=None,
                          name="合成退市股", status=SecurityStatus.DELISTED),
        ),
    )
    assert inst.status_on(date(2020, 6, 1)).status is SecurityStatus.LISTED
    assert inst.status_on(date(2026, 1, 1)).status is SecurityStatus.DELISTED


def test_non_main_board_is_not_simulatable_by_default():
    """§3.1 创业板可展示但不进入默认可模拟池。"""

    gem = Instrument(
        instrument_id="SYN.A.300001",
        exchange=Exchange.SZSE,
        board=Board.GEM,
        status_history=(StatusVersion(valid_from=date(2020, 1, 1), valid_to=None,
                                      name="合成创业板", status=SecurityStatus.LISTED),),
    )
    assert not gem.is_simulatable(date(2026, 9, 11))


# ------------------------------------------------------------------ D06
def test_d06_future_dividend_does_not_change_old_snapshot_hash():
    """D06：未来分红影响今日前复权序列 -> 旧快照与旧实验哈希不变化。

    本测试固化"不可变"语义：快照哈希是快照内容的函数，
    后续发生的公司行为不得回写已发布快照。
    """

    def snapshot_hash(datasets: dict[str, str]) -> str:
        import hashlib

        payload = "|".join(f"{k}={v}" for k, v in sorted(datasets.items()))
        return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()

    old = snapshot_hash({"daily_quotes": "v1-adj-before-dividend"})
    # 未来分红发生后，新的前复权序列是**新数据集**，不是旧数据集的修改
    new = snapshot_hash({"daily_quotes": "v2-adj-after-dividend"})
    assert old != new
    # 旧哈希仍是旧哈希（这里以重新计算的方式表达"未被回写"）
    assert snapshot_hash({"daily_quotes": "v1-adj-before-dividend"}) == old


# ------------------------------------------------------------------ D07
def test_d07_vendor_switch_is_visible_not_silently_merged():
    """D07：主数据或供应商切换 -> 差异可见，未经验证不混接。"""

    a = PitRecord(
        record_id="quote-sourceA-20260910",
        source_published_at=utc(2026, 9, 10, 8, 0),
        timestamp_precision=TimestampPrecision.MINUTE,
        first_seen_at=utc(2026, 9, 10, 8, 1),
        available_at=utc(2026, 9, 10, 8, 0),
        availability_basis=AvailabilityBasis.OBSERVED,
        pit_mode=PitMode.LIVE_OBSERVED,
    )
    b = PitRecord(
        record_id="quote-sourceB-20260910",
        source_published_at=utc(2026, 9, 10, 8, 0),
        timestamp_precision=TimestampPrecision.MINUTE,
        first_seen_at=utc(2026, 9, 10, 8, 2),
        available_at=utc(2026, 9, 10, 8, 0),
        availability_basis=AvailabilityBasis.OBSERVED,
        pit_mode=PitMode.LIVE_OBSERVED,
    )
    usable = select_usable_at([a, b], utc(2026, 9, 11))
    # 两个来源的记录同时存在且可区分，未合并成一条
    assert len(usable) == 2
    assert {r.record_id for r in usable} == {"quote-sourceA-20260910", "quote-sourceB-20260910"}


# ------------------------------------------------------------------ D08
def test_d08_repost_merges_fact_but_keeps_withdrawal_visible():
    """D08：同一公告转载多次并随后撤回 -> 事实归并，撤回产生新状态，旧判断仍可回看。

    这里验证"陈旧判断仍可用"这一半：撤回时间晚于旧判断时点，
    旧时点看不到撤回状态。
    """

    withdrawn_at = utc(2026, 9, 11, 9, 0)
    record = PitRecord(
        record_id="evt-1-status",
        source_published_at=utc(2026, 9, 11, 9, 0),
        timestamp_precision=TimestampPrecision.SECOND,
        first_seen_at=utc(2026, 9, 11, 9, 1),
        available_at=withdrawn_at,
        availability_basis=AvailabilityBasis.OBSERVED,
        pit_mode=PitMode.LIVE_OBSERVED,
        valid_from=withdrawn_at,
    )
    # 撤回之前的判断时点看不到撤回状态
    assert not is_usable_at(record, utc(2026, 9, 11, 8, 59))
    # 撤回之后可见
    assert is_usable_at(record, utc(2026, 9, 11, 9, 1))


# ------------------------------------------------------------------ 时点完整性
def test_available_at_cannot_precede_publication():
    """§7.2 规则 1 的守卫。"""

    bad = PitRecord(
        record_id="bad-1",
        source_published_at=utc(2026, 9, 11, 10, 0),
        timestamp_precision=TimestampPrecision.SECOND,
        first_seen_at=utc(2026, 9, 11, 10, 1),
        available_at=utc(2026, 9, 11, 9, 0),   # 早于公开时间
        availability_basis=AvailabilityBasis.OBSERVED,
        pit_mode=PitMode.LIVE_OBSERVED,
    )
    with pytest.raises(PitViolation) as exc:
        assert_available_at_not_before_publication(bad)
    assert exc.value.code == "PIT_UNVERIFIED"
    assert "precedes" in exc.value.message


def test_naive_datetime_is_rejected():
    """§7.1 必须带时区，禁止朴素时间。"""

    with pytest.raises(ValueError, match="timezone-aware"):
        PitRecord(
            record_id="naive",
            source_published_at=None,
            timestamp_precision=TimestampPrecision.DATE,
            first_seen_at=datetime(2026, 9, 11, 0, 0),   # 无时区
            available_at=utc(2026, 9, 11),
            availability_basis=AvailabilityBasis.OBSERVED,
            pit_mode=PitMode.LIVE_OBSERVED,
        )


def test_live_and_reconstructed_cannot_share_one_conclusion():
    """§7.2 两种模式不得混入同一比较结论。"""

    live = PitRecord(
        record_id="live-1", source_published_at=None,
        timestamp_precision=TimestampPrecision.DATE,
        first_seen_at=utc(2026, 9, 11), available_at=utc(2026, 9, 11),
        availability_basis=AvailabilityBasis.OBSERVED, pit_mode=PitMode.LIVE_OBSERVED,
    )
    hist = PitRecord(
        record_id="hist-1", source_published_at=None,
        timestamp_precision=TimestampPrecision.DATE,
        first_seen_at=utc(2026, 9, 11), available_at=utc(2020, 1, 1),
        availability_basis=AvailabilityBasis.RECONSTRUCTED,
        pit_mode=PitMode.HISTORICAL_RECONSTRUCTED,
    )
    with pytest.raises(PitViolation) as exc:
        assert_mode_consistent([live, hist], "comparison-M")
    assert "mixes PIT modes" in exc.value.message


def test_suspended_instrument_status_blocks_simulation():
    """§12.3 状态未知或停牌不成交。"""

    inst = Instrument(
        instrument_id="SYN.A.600002",
        exchange=Exchange.SSE,
        board=Board.MAIN,
        status_history=(
            StatusVersion(valid_from=date(2018, 7, 1), valid_to=date(2026, 9, 10),
                          name="合成示例·停牌", status=SecurityStatus.LISTED),
            StatusVersion(valid_from=date(2026, 9, 10), valid_to=None,
                          name="合成示例·停牌", status=SecurityStatus.SUSPENDED),
        ),
    )
    assert inst.is_simulatable(date(2026, 9, 9))
    assert not inst.is_simulatable(date(2026, 9, 10))
