from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from aquant.domain.data.pit import AvailabilityBasis, PitMode, TimestampPrecision
from aquant.domain.fundamentals.fact_repository import FinancialFactRepository
from aquant.domain.fundamentals.versioned import FinancialFact, StatementScope


UTC = timezone.utc


def _at(day: int, hour: int) -> datetime:
    return datetime(2026, 1, day, hour, tzinfo=UTC)


def _fact(version: str, day: int, value: Decimal, *,
          supersedes: str | None = None) -> FinancialFact:
    return FinancialFact(
        instrument_id="SYN.SSE.600519", metric="net_profit",
        period_end=date(2025, 12, 31),
        statement_scope=StatementScope.CONSOLIDATED,
        profit_scope="ATTRIBUTABLE", value=value, currency="CNY",
        raw_unit="yuan", source_id="official-pdf",
        source_document_id=f"announcement-{version}",
        source_published_date=date(2026, 1, day),
        source_published_at=datetime(2026, 1, day, 15, tzinfo=timezone(timedelta(hours=8))),
        timestamp_precision=TimestampPrecision.MINUTE,
        first_seen_at=_at(day, 8), ingested_at=_at(day, 9),
        available_at=_at(day, 8),
        availability_basis=AvailabilityBasis.RECONSTRUCTED,
        pit_mode=PitMode.HISTORICAL_RECONSTRUCTED,
        content_hash=f"sha256:{version}", version_id=version,
        supersedes_id=supersedes,
    )


def test_reopen_replays_every_field_and_pit_cutoff(tmp_path):
    path = tmp_path / "financial-facts.sqlite"
    original = _fact("v1", 10, Decimal("100.00"))
    revised = _fact("v2", 20, Decimal("120.000"), supersedes="v1")
    repository = FinancialFactRepository(path)

    # A source may hand over the revisions in reverse order in one import.
    assert repository.append_many([revised, original]) == 2
    reopened = FinancialFactRepository(path)
    store = reopened.load()

    assert store.facts == (original, revised)
    assert store.facts[0].value.as_tuple() == original.value.as_tuple()
    assert store.facts[0].source_published_at.isoformat() == original.source_published_at.isoformat()
    assert [fact.version_id for fact in store.select_pit(_at(19, 12))] == ["v1"]
    assert [fact.version_id for fact in store.select_pit(_at(20, 12))] == ["v2"]


def test_same_version_retry_is_idempotent_and_conflict_keeps_original(tmp_path):
    repository = FinancialFactRepository(tmp_path / "facts.sqlite")
    original = _fact("v1", 10, Decimal("100"))

    assert repository.append(original)
    assert repository.append_many([original, original]) == 0
    with pytest.raises(ValueError, match="conflicting financial fact version_id"):
        repository.append(replace(original, value=Decimal("101")))
    assert repository.load().facts == (original,)


def test_invalid_revision_batch_rolls_back_and_rejects_forks_and_cycles(tmp_path):
    repository = FinancialFactRepository(tmp_path / "facts.sqlite")
    root = _fact("v1", 10, Decimal("100"))
    repository.append(root)

    successor = _fact("v2", 20, Decimal("120"), supersedes="v1")
    bad = replace(_fact("v3", 21, Decimal("130"), supersedes="v2"), metric="revenue")
    with pytest.raises(ValueError, match="different logical fact"):
        repository.append_many([successor, bad])
    assert repository.load().facts == (root,)

    fork = _fact("v3", 21, Decimal("130"), supersedes="v1")
    with pytest.raises(ValueError, match="two successors"):
        repository.append_many([successor, fork])
    with pytest.raises(ValueError, match="two roots"):
        repository.append(_fact("other-root", 20, Decimal("120")))
    cycle_a = _fact("a", 20, Decimal("120"), supersedes="b")
    cycle_b = _fact("b", 20, Decimal("130"), supersedes="a")
    with pytest.raises(ValueError, match="cycle"):
        repository.append_many([cycle_a, cycle_b])
    assert repository.load().facts == (root,)


def test_persisted_versions_cannot_be_updated_or_deleted(tmp_path):
    path = tmp_path / "facts.sqlite"
    repository = FinancialFactRepository(path)
    repository.append(_fact("v1", 10, Decimal("100")))

    with sqlite3.connect(path) as con:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            con.execute("UPDATE financial_fact SET value_text = '0' WHERE version_id = 'v1'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            con.execute("DELETE FROM financial_fact WHERE version_id = 'v1'")
