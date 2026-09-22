"""版本化财务事实与正式 PIT 选择。

这个模块只保存和选择财务事实，不负责网络接入、供应商字段映射或 TTM。
同一报告期的修订必须追加为新版本；``supersedes_id`` 把新版本连接到被
替代版本，旧版本永远保留，因而可以在任意 cutoff 重放当时可见的事实。

PIT 的枚举和基础校验全部复用 :mod:`aquant.domain.data.pit`。这里使用
``period_end`` 表示报告期截止日，使用 ``source_document_id`` 表示原始
报告/公告标识；这两个字段都不代表可用时间。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Iterable, Iterator

from ..data.pit import (
    AvailabilityBasis,
    PitMode,
    PitRecord,
    PitViolation,
    TimestampPrecision,
    assert_available_at_not_before_publication,
    assert_backtestable,
    assert_mode_consistent,
    is_usable_at,
)


class StatementScope(str, Enum):
    """报表主体口径。普通工商 S2 至少要区分合并和母公司。"""

    CONSOLIDATED = "CONSOLIDATED"
    PARENT = "PARENT"


class ProfitScope(str, Enum):
    """净利润归属口径。非净利润事实可将其设为 ``None``。"""

    ATTRIBUTABLE = "ATTRIBUTABLE"
    CONSOLIDATED = "CONSOLIDATED"


# 常见调用方名称的同一枚举别名，避免为同一个口径再造枚举。
NetProfitScope = ProfitScope


def _as_utc_aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


@dataclass(frozen=True, slots=True)
class FinancialFact:
    """一条可审计的财务事实版本。

    ``value`` 是按 ``raw_unit`` 记录的原始数值；本模块不做单位换算。这样
    Sina、mootdx、CNINFO 的适配器可以分别保存其原始单位，并在上层完成
    明确的单位映射与核对。
    """

    instrument_id: str
    metric: str
    period_end: date
    statement_scope: StatementScope
    profit_scope: ProfitScope | None
    value: Decimal | int | float | str | None
    currency: str
    raw_unit: str
    source_id: str
    source_document_id: str
    source_published_date: date | None
    source_published_at: datetime | None
    timestamp_precision: TimestampPrecision
    first_seen_at: datetime
    ingested_at: datetime
    available_at: datetime
    availability_basis: AvailabilityBasis
    pit_mode: PitMode
    content_hash: str
    version_id: str
    supersedes_id: str | None = None

    def __post_init__(self) -> None:
        if not self.instrument_id:
            raise ValueError("instrument_id is required")
        if not self.metric:
            raise ValueError("metric is required")
        if not isinstance(self.period_end, date):
            raise TypeError("period_end must be a date")
        if not self.currency:
            raise ValueError("currency is required")
        if not self.raw_unit:
            raise ValueError("raw_unit is required")
        if not self.source_id:
            raise ValueError("source_id is required")
        if not self.source_document_id:
            raise ValueError("source_document_id is required")
        if not self.content_hash:
            raise ValueError("content_hash is required")
        if not self.version_id:
            raise ValueError("version_id is required")
        if self.supersedes_id == self.version_id:
            raise ValueError("a version cannot supersede itself")

        # Accept serialized enum values at the boundary, but keep one canonical
        # enum type in memory.  The PIT enums themselves are imported above.
        object.__setattr__(self, "statement_scope", StatementScope(self.statement_scope))
        if self.profit_scope is not None:
            object.__setattr__(self, "profit_scope", ProfitScope(self.profit_scope))
        object.__setattr__(self, "timestamp_precision", TimestampPrecision(self.timestamp_precision))
        object.__setattr__(self, "availability_basis", AvailabilityBasis(self.availability_basis))
        object.__setattr__(self, "pit_mode", PitMode(self.pit_mode))

        _as_utc_aware(self.first_seen_at, "first_seen_at")
        _as_utc_aware(self.ingested_at, "ingested_at")
        _as_utc_aware(self.available_at, "available_at")
        if self.source_published_at is not None:
            _as_utc_aware(self.source_published_at, "source_published_at")
            if (self.source_published_date is not None
                    and self.source_published_date != self.source_published_at.date()):
                raise ValueError("source_published_date must match source_published_at.date()")
        if self.timestamp_precision in (TimestampPrecision.SECOND, TimestampPrecision.MINUTE):
            if self.source_published_at is None:
                raise ValueError(
                    "SECOND/MINUTE timestamp_precision requires source_published_at")
        elif self.timestamp_precision is TimestampPrecision.DATE:
            if self.source_published_date is None:
                raise ValueError("DATE timestamp_precision requires source_published_date")

        # Build the shared PIT view so publication ordering and timezone rules
        # remain exactly those used by ordinary PitRecord consumers.
        pit = self.as_pit_record()
        assert_available_at_not_before_publication(pit)
        # A live record cannot be observed after it is ingested.  Historical
        # reconstruction intentionally keeps the looser shared PIT rule.
        from ..data.pit import assert_live_observed_first_seen
        assert_live_observed_first_seen(pit, ingested_at=self.ingested_at)

    @property
    def record_id(self) -> str:
        """Compatibility view for the common PIT record identity name."""

        return self.version_id

    @property
    def raw_value(self) -> Decimal | int | float | str | None:
        """The value in the source's declared ``raw_unit``."""

        return self.value

    @property
    def announcement_id(self) -> str:
        """Alias used by announcement-oriented adapters."""

        return self.source_document_id

    @property
    def available_basis(self) -> AvailabilityBasis:
        """Alias matching the persisted event field spelling."""

        return self.availability_basis

    @property
    def logical_key(self) -> tuple[str, str, date, StatementScope, ProfitScope | None, str, str]:
        """Identity of the fact being revised, excluding version metadata."""

        return (
            self.instrument_id,
            self.metric,
            self.period_end,
            self.statement_scope,
            self.profit_scope,
            self.currency,
            self.raw_unit,
        )

    def as_pit_record(self) -> PitRecord:
        """Return the shared PIT view used by the common gate functions."""

        return PitRecord(
            record_id=self.version_id,
            source_published_at=self.source_published_at,
            timestamp_precision=self.timestamp_precision,
            first_seen_at=self.first_seen_at,
            available_at=self.available_at,
            availability_basis=self.availability_basis,
            pit_mode=self.pit_mode,
            supersedes_id=self.supersedes_id,
        )

    def is_usable_at(self, cutoff: datetime) -> bool:
        return is_usable_at(self.as_pit_record(), cutoff)


class VersionedFinancialFactStore:
    """内存中的 append-only 财务事实版本集。

    ``append`` 只追加新版本，绝不按报告期覆盖旧行。正式 PIT 读取通过
    ``select_pit`` 完成：先按 ``available_at <= cutoff`` 过滤，再沿
    ``supersedes_id`` 选择当时唯一的最新链头。
    """

    def __init__(self, facts: Iterable[FinancialFact] = ()) -> None:
        self._facts: list[FinancialFact] = []
        self._by_version: dict[str, FinancialFact] = {}
        self.append_many(facts)

    def __iter__(self) -> Iterator[FinancialFact]:
        return iter(self._facts)

    def __len__(self) -> int:
        return len(self._facts)

    @property
    def facts(self) -> tuple[FinancialFact, ...]:
        """Read-only insertion-order view for audit/export code."""

        return tuple(self._facts)

    def append(self, fact: FinancialFact) -> None:
        if fact.version_id in self._by_version:
            raise ValueError(f"duplicate financial fact version_id {fact.version_id!r}")
        if fact.supersedes_id is not None:
            prior = self._by_version.get(fact.supersedes_id)
            if prior is None:
                raise ValueError(
                    f"supersedes_id {fact.supersedes_id!r} is not present in the append-only store")
            self._validate_revision(fact, prior)
        self._facts.append(fact)
        self._by_version[fact.version_id] = fact

    def append_many(self, facts: Iterable[FinancialFact]) -> None:
        incoming = list(facts)
        if not incoming:
            return
        ids = [fact.version_id for fact in incoming]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate financial fact version_id in append batch")
        overlap = set(ids) & set(self._by_version)
        if overlap:
            raise ValueError(f"duplicate financial fact version_id {sorted(overlap)!r}")

        # Validate the complete batch before mutating the store, so a failed
        # import cannot leave a partial append.
        batch = dict(self._by_version)
        batch.update({fact.version_id: fact for fact in incoming})
        for fact in incoming:
            if fact.supersedes_id is not None:
                prior = batch.get(fact.supersedes_id)
                if prior is None:
                    raise ValueError(
                        f"supersedes_id {fact.supersedes_id!r} is not present in the append-only store")
                self._validate_revision(fact, prior)
        self._facts.extend(incoming)
        self._by_version.update({fact.version_id: fact for fact in incoming})

    @staticmethod
    def _validate_revision(fact: FinancialFact, prior: FinancialFact) -> None:
        if fact.logical_key != prior.logical_key:
            raise ValueError(
                f"financial fact {fact.version_id!r} supersedes a different logical fact")
        if fact.available_at < prior.available_at:
            raise ValueError(
                f"financial fact {fact.version_id!r} is available before its superseded version")

    def select_pit(
        self,
        cutoff: datetime,
        *,
        instrument_id: str | None = None,
        metric: str | None = None,
        period_end: date | None = None,
    ) -> list[FinancialFact]:
        """Select one latest legal version per logical fact at ``cutoff``.

        Formal PIT refuses a selected version with ``UNKNOWN`` availability
        basis.  Use ``select(..., formal=False)`` only for audit/diagnostic
        views that are explicitly outside formal PIT.
        """

        selected = self.select(
            cutoff,
            instrument_id=instrument_id,
            metric=metric,
            period_end=period_end,
            formal=True,
        )
        return selected

    def select(
        self,
        cutoff: datetime,
        *,
        instrument_id: str | None = None,
        metric: str | None = None,
        period_end: date | None = None,
        formal: bool = True,
    ) -> list[FinancialFact]:
        """Select facts visible at ``cutoff``.

        ``formal=True`` applies the common mode and UNKNOWN-basis gates. The
        output is deterministic, sorted by logical key, and contains no older
        version that is superseded by an eligible revision.
        """

        _as_utc_aware(cutoff, "cutoff")
        eligible = [
            fact for fact in self._facts
            if fact.available_at <= cutoff
            and (instrument_id is None or fact.instrument_id == instrument_id)
            and (metric is None or fact.metric == metric)
            and (period_end is None or fact.period_end == period_end)
        ]
        groups: dict[tuple, list[FinancialFact]] = defaultdict(list)
        for fact in eligible:
            groups[fact.logical_key].append(fact)

        selected: list[FinancialFact] = []
        for key, rows in groups.items():
            ids_replaced = {row.supersedes_id for row in rows if row.supersedes_id is not None}
            heads = [row for row in rows if row.version_id not in ids_replaced]
            if len(heads) != 1:
                raise PitViolation(
                    "PIT_UNVERIFIED",
                    f"{key!r} has {len(heads)} eligible version heads; revision chain is ambiguous",
                    f"{key[0]}:{key[1]}:{key[2].isoformat()}",
                    "verify the source version chain and keep one supersedes successor",
                )
            selected.append(heads[0])

        selected.sort(key=lambda fact: fact.logical_key)
        if formal:
            pit_records = [fact.as_pit_record() for fact in selected]
            assert_mode_consistent(pit_records, "financial-facts")
            assert_backtestable(pit_records, "financial-facts")
        return selected

    def select_at(self, cutoff: datetime, **filters: object) -> list[FinancialFact]:
        """Short alias for callers that use ``at`` terminology."""

        return self.select(cutoff, **filters)  # type: ignore[arg-type]

    def select_as_of(self, as_of: datetime, **filters: object) -> list[FinancialFact]:
        """Short alias for callers that use ``as_of`` terminology."""

        return self.select(as_of, **filters)  # type: ignore[arg-type]


# A concise name for application code and a function form for small adapters.
FinancialFactStore = VersionedFinancialFactStore


def select_financial_facts(
    facts: Iterable[FinancialFact],
    cutoff: datetime,
    **filters: object,
) -> list[FinancialFact]:
    """Select version heads without requiring callers to retain a store."""

    return VersionedFinancialFactStore(facts).select(cutoff, **filters)  # type: ignore[arg-type]


__all__ = [
    "AvailabilityBasis",
    "FinancialFact",
    "FinancialFactStore",
    "NetProfitScope",
    "PitMode",
    "PitViolation",
    "ProfitScope",
    "StatementScope",
    "TimestampPrecision",
    "VersionedFinancialFactStore",
    "select_financial_facts",
]
