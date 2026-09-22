from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from aquant.domain.data.pit import AvailabilityBasis, PitMode, PitViolation, TimestampPrecision
from aquant.domain.fundamentals.versioned import (
    FinancialFact,
    StatementScope,
    VersionedFinancialFactStore,
)


UTC = timezone.utc


def at(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 1, day, hour, tzinfo=UTC)


def fact(
    version: str,
    *,
    available_day: int,
    value: str,
    supersedes: str | None = None,
    basis: AvailabilityBasis = AvailabilityBasis.OBSERVED,
) -> FinancialFact:
    return FinancialFact(
        instrument_id="SYN.SSE.600519",
        metric="net_profit",
        period_end=date(2025, 12, 31),
        statement_scope=StatementScope.CONSOLIDATED,
        profit_scope="ATTRIBUTABLE",
        value=Decimal(value),
        currency="CNY",
        raw_unit="yuan",
        source_id="synthetic-provider",
        source_document_id=f"announcement-{version}",
        source_published_date=date(2026, 1, available_day),
        source_published_at=at(available_day, 8),
        timestamp_precision=TimestampPrecision.SECOND,
        first_seen_at=at(available_day, 9),
        ingested_at=at(available_day, 9),
        available_at=at(available_day, 9),
        availability_basis=basis,
        pit_mode=PitMode.LIVE_OBSERVED,
        content_hash=f"sha256:{version}",
        version_id=version,
        supersedes_id=supersedes,
    )


def test_financial_fact_keeps_s2_provenance_and_reuses_shared_pit_enums():
    item = fact("v1", available_day=10, value="100")

    assert item.period_end == date(2025, 12, 31)
    assert item.statement_scope is StatementScope.CONSOLIDATED
    assert item.profit_scope.value == "ATTRIBUTABLE"
    assert item.currency == "CNY"
    assert item.raw_unit == "yuan"
    assert item.source_document_id == "announcement-v1"
    assert item.content_hash == "sha256:v1"
    assert item.record_id == "v1"
    assert item.as_pit_record().pit_mode is PitMode.LIVE_OBSERVED
    assert item.as_pit_record().availability_basis is AvailabilityBasis.OBSERVED


def test_revision_is_appended_and_cutoff_selects_original_then_revision():
    original = fact("v1", available_day=10, value="100")
    revised = fact("v2", available_day=20, value="120", supersedes="v1")
    store = VersionedFinancialFactStore([original, revised])

    assert len(store) == 2
    before = store.select_pit(at(19, 12), metric="net_profit")
    after = store.select_pit(at(20, 12), metric="net_profit")
    assert [(row.version_id, row.value) for row in before] == [("v1", Decimal("100"))]
    assert [(row.version_id, row.value) for row in after] == [("v2", Decimal("120"))]
    assert [row.version_id for row in store.facts] == ["v1", "v2"]


def test_unknown_basis_cannot_enter_formal_pit():
    item = fact("unknown", available_day=10, value="100", basis=AvailabilityBasis.UNKNOWN)
    store = VersionedFinancialFactStore([item])

    with pytest.raises(PitViolation) as exc:
        store.select_pit(at(11))
    assert "UNKNOWN" in exc.value.message
    assert [row.version_id for row in store.select(at(11), formal=False)] == ["unknown"]


def test_revision_must_point_to_same_logical_fact_and_store_is_append_only():
    original = fact("v1", available_day=10, value="100")
    store = VersionedFinancialFactStore([original])

    with pytest.raises(ValueError, match="different logical fact"):
        store.append(
            replace(original, version_id="bad", metric="revenue", supersedes_id="v1")
        )
