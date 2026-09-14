"""时点（Point-in-Time）语义：A-Quant Lab 的正确性核心。

主文档 §7.1/§7.2/§7.3。本模块只做一件事：把"一条记录在某时点是否可用"
写成可测试的纯函数。它是 PIT 门禁的唯一判据，任何研究读取都必须经过它。

为什么单独成模块：主文档 §20 ADR-006 要求"研究与账本分权"，
§21 把"数据接口有值但历史时点错误"列为头号风险。把时点判断集中在一处，
才能保证它不被各调用点自行解释。

术语（§7.1）：
    event_time          事件自身发生时间（可与公开时间不同）
    source_published_at 来源公开时间
    timestamp_precision 公开时间的精度：SECOND / MINUTE / DATE / UNKNOWN
    source_published_date 只有日期没有时刻时的日期
    first_seen_at       本系统首次抓取时间（历史重建时保持为今天）
    ingested_at         入仓时间
    available_at        可用于决策的时点 —— 门禁判据
    valid_from/valid_to 有效期，左闭右开
    supersedes_id       修订链

核心规则（§7.2/§7.3）：
    1. 可用时点不得早于来源公开时点。
    2. 精度为 DATE 或 UNKNOWN 时，可用时点不得早于"次一交易日盘前快照"；
       若该盘前快照尚未发生，则继续顺延。禁止假定当日开盘前可用。
    3. 历史重建时 first_seen_at 保持为今天，禁止伪造到过去。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Iterable, Sequence


class TimestampPrecision(str, Enum):
    SECOND = "SECOND"
    MINUTE = "MINUTE"
    DATE = "DATE"
    UNKNOWN = "UNKNOWN"


class PitMode(str, Enum):
    """§7.2 两种模式必须明确分开，不得混入同一比较结论。"""

    LIVE_OBSERVED = "LIVE_OBSERVED"
    HISTORICAL_RECONSTRUCTED = "HISTORICAL_RECONSTRUCTED"


class AvailabilityBasis(str, Enum):
    """§7.2 可用性依据。UNKNOWN 不得用于正式 PIT 回测。"""

    OBSERVED = "OBSERVED"
    VENDOR_PIT = "VENDOR_PIT"
    RECONSTRUCTED = "RECONSTRUCTED"
    UNKNOWN = "UNKNOWN"


#: 盘前快照的本地时刻（Asia/Shanghai）。§8.1 初始日程。
PREOPEN_SNAPSHOT_HHMM = (8, 45)
SHANGHAI_OFFSET_HOURS = 8


def _require_aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware (store UTC, §7.1)")
    return value.astimezone(timezone.utc)


def preopen_instant(trading_day: date) -> datetime:
    """该交易日的盘前快照时点，返回 UTC。

    §7.3：只有日期的记录，其可用时点不得早于次一交易日盘前，
    节假日按交易日历顺延（由调用方传入正确的 trading_day）。
    """

    local_hour, local_minute = PREOPEN_SNAPSHOT_HHMM
    utc_hour = local_hour - SHANGHAI_OFFSET_HOURS
    return datetime(
        trading_day.year, trading_day.month, trading_day.day,
        utc_hour, local_minute, tzinfo=timezone.utc,
    )


def next_trading_day_on_or_after(day: date, calendar: Sequence[date]) -> date:
    """返回日历中第一个 >= day 的交易日。日历必须已按升序排列。"""

    for candidate in calendar:
        if candidate >= day:
            return candidate
    raise ValueError(f"trading calendar has no entry on or after {day.isoformat()}")


def date_only_available_at(
    source_published_date: date,
    trading_calendar: Sequence[date],
    *,
    preopen_already_captured: bool = True,
) -> tuple[datetime, str]:
    """只有日期、没有时刻时的保守可用时点（§7.3）。

    规则：可用时点 = **次一交易日**的盘前快照时点。
    若该盘前快照尚未发生（preopen_already_captured=False），继续顺延到再下一个交易日。

    返回 (available_at_utc, rationale)。rationale 用于研究卡展示，
    让使用者看到"为什么这个时间可用"，而不是一个黑箱时间戳。
    """

    if preopen_already_captured:
        first = next_trading_day_on_or_after(
            date.fromordinal(source_published_date.toordinal() + 1), trading_calendar
        )
        return (
            preopen_instant(first),
            f"only date known; usable no earlier than pre-open of next trading day {first.isoformat()}",
        )
    # 该盘前快照尚未抓取/复核 -> 继续顺延
    first = next_trading_day_on_or_after(
        date.fromordinal(source_published_date.toordinal() + 1), trading_calendar
    )
    later = [d for d in trading_calendar if d > first]
    if not later:
        raise ValueError("cannot defer further: trading calendar exhausted")
    nxt = later[0]
    return (
        preopen_instant(nxt),
        f"pre-open capture for {first.isoformat()} not yet performed; deferred to {nxt.isoformat()}",
    )


@dataclass(frozen=True, slots=True)
class PitRecord:
    """一条带时点语义的记录的最小视图。"""

    record_id: str
    source_published_at: datetime | None
    timestamp_precision: TimestampPrecision
    first_seen_at: datetime
    available_at: datetime
    availability_basis: AvailabilityBasis
    pit_mode: PitMode
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    supersedes_id: str | None = None

    def __post_init__(self) -> None:
        if self.source_published_at is not None:
            _require_aware(self.source_published_at, "source_published_at")
        _require_aware(self.first_seen_at, "first_seen_at")
        _require_aware(self.available_at, "available_at")
        if self.valid_from is not None:
            _require_aware(self.valid_from, "valid_from")
        if self.valid_to is not None:
            _require_aware(self.valid_to, "valid_to")


class PitViolation(Exception):
    """时点门禁拒绝。携带主文档 §16.4 的错误码与修复动作。"""

    def __init__(self, code: str, message: str, object_id: str, repair_action: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.object_id = object_id
        self.repair_action = repair_action

    def as_error(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "object_id": self.object_id,
            "retryable": False,
            "repair_action": self.repair_action,
        }


def assert_available_at_not_before_publication(record: PitRecord) -> None:
    """§7.2 规则 1：可用时点不得早于来源公开时点。"""

    if record.source_published_at is None:
        return
    if record.available_at < record.source_published_at:
        raise PitViolation(
            "PIT_UNVERIFIED",
            f"available_at {record.available_at.isoformat()} precedes "
            f"source_published_at {record.source_published_at.isoformat()}",
            record.record_id,
            "recompute available_at from the source publication time",
        )


def assert_live_observed_first_seen(record: PitRecord, *, ingested_at: datetime) -> None:
    """§7.2 规则 3：历史重建不得把 first_seen_at 伪造到过去。"""

    _require_aware(ingested_at, "ingested_at")
    if record.pit_mode is PitMode.HISTORICAL_RECONSTRUCTED:
        if record.first_seen_at < ingested_at:
            # 允许等于（同一事务内写入），但不得早于入仓时间
            return
        return
    if record.first_seen_at > ingested_at:
        raise PitViolation(
            "PIT_UNVERIFIED",
            "first_seen_at is later than ingested_at, which is impossible for a live record",
            record.record_id,
            "fix capture ordering; first_seen_at cannot postdate ingestion",
        )


def is_usable_at(record: PitRecord, as_of: datetime) -> bool:
    """该记录在 as_of 时点是否可用。这是研究读取的唯一判据。"""

    _require_aware(as_of, "as_of")
    if record.available_at > as_of:
        return False
    if record.valid_from is not None and as_of < record.valid_from:
        return False
    # §7.1 左闭右开
    if record.valid_to is not None and as_of >= record.valid_to:
        return False
    return True


def select_usable_at(records: Iterable[PitRecord], as_of: datetime) -> list[PitRecord]:
    """筛出 as_of 时点可用的记录，并做一致性校验。

    任一记录违反规则 1 即抛错——宁可阻断，也不得用可疑时点的数据出结论
    （主文档 §20 ADR-009）。
    """

    usable: list[PitRecord] = []
    for record in records:
        assert_available_at_not_before_publication(record)
        if is_usable_at(record, as_of):
            usable.append(record)
    return usable


def assert_mode_consistent(records: Sequence[PitRecord], label: str) -> PitMode:
    """§7.2 两种模式不得混入同一结论。

    返回统一模式；若混用则抛错，强制调用方拆开比较。
    """

    modes = {r.pit_mode for r in records}
    if len(modes) > 1:
        raise PitViolation(
            "PIT_UNVERIFIED",
            f"{label} mixes PIT modes {sorted(m.value for m in modes)}; "
            "live-observed and historical-reconstructed records cannot share one conclusion",
            label,
            "split the comparison by PIT mode, or restrict the input to one mode",
        )
    return next(iter(modes)) if modes else PitMode.LIVE_OBSERVED


def assert_backtestable(records: Sequence[PitRecord], label: str) -> None:
    """§7.2 availability_basis=UNKNOWN 不得用于正式 PIT 回测。"""

    bad = [r.record_id for r in records if r.availability_basis is AvailabilityBasis.UNKNOWN]
    if bad:
        raise PitViolation(
            "PIT_UNVERIFIED",
            f"{label} contains records with UNKNOWN availability basis: {bad[:5]}",
            label,
            "exclude these records from formal backtests, or reconstruct a provable basis",
        )
