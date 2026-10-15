"""S2 读取的 PIT 模式分支：前向保留首见约束，历史重建不被首见一刀切。

主规格 §7.2 允许 ``HISTORICAL_RECONSTRUCTED``。原实现无条件要求
``first_seen_at <= cutoff``，会把合法的历史重建一起排除；修好后必须同时满足
两件事：前向事实仍被首见约束挡住，历史重建只在显式要求且依据可证时进入。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from aquant.domain.data.pit import AvailabilityBasis, PitMode
from aquant.domain.fundamentals.versioned import (
    FinancialFact, ProfitScope, StatementScope,
)
from aquant.operations.s2_readiness import admissible_facts

UTC = timezone.utc
CUTOFF = datetime(2026, 9, 24, 7, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 28, 4, 10, tzinfo=UTC)


def _fact(*, first_seen: datetime, available_at: datetime, basis: str,
          mode: str, ingested_at: datetime | None = None) -> FinancialFact:
    return FinancialFact(
        instrument_id="601012", metric="revenue", period_end=date(2025, 12, 31),
        statement_scope=StatementScope.CONSOLIDATED, profit_scope=None,
        value=Decimal("1000"), currency="CNY", raw_unit="yuan",
        source_id="cninfo", source_document_id="1223421477",
        source_published_date=date(2025, 4, 30), source_published_at=None,
        timestamp_precision="DATE", first_seen_at=first_seen,
        ingested_at=ingested_at or first_seen, available_at=available_at,
        availability_basis=basis, pit_mode=mode,
        content_hash="sha256:" + "b" * 64, version_id=f"v-{mode}-{basis}",
    )


def test_forward_mode_still_refuses_a_fact_first_seen_after_the_cutoff() -> None:
    # 可用时点声明在 cutoff 之后：这条事实在 cutoff 处本来就看不见，
    # 首见门禁与可用时间门禁同时成立。
    fact = _fact(first_seen=LATER, available_at=LATER, basis="OBSERVED",
                 mode="LIVE_OBSERVED")
    admitted, rejected = admissible_facts(
        facts=[fact], cutoff=CUTOFF, allow_reconstructed=False)
    assert admitted == []
    assert [reason for _, reason in rejected] == ["S2_FACT_NOT_YET_AVAILABLE"]


def test_first_seen_gate_is_kept_for_forward_mode() -> None:
    """首见门禁必须保留：声称在 cutoff 前可用、但系统当时还没见过的记录不允许。

    合法的 ``FinancialFact`` 无法表达这种组合（``LIVE_OBSERVED`` 要求
    ``available_at >= first_seen_at``），所以这里直接构造它要防的那个输入：
    一份被改写过可用时点的持久化记录。原实现是无条件过滤，修好后必须仍然
    **只在前向分支**拒绝它。
    """

    forged = SimpleNamespace(
        version_id="forged-forward", metric="revenue",
        first_seen_at=datetime(2026, 9, 26, 7, 0, tzinfo=UTC),
        available_at=datetime(2026, 9, 20, 7, 0, tzinfo=UTC),
        availability_basis=AvailabilityBasis.OBSERVED,
        pit_mode=PitMode.LIVE_OBSERVED,
    )
    admitted, rejected = admissible_facts(
        facts=[forged], cutoff=CUTOFF, allow_reconstructed=False)
    assert admitted == []
    assert [reason for _, reason in rejected] == ["S2_FACT_FIRST_SEEN_AFTER_CUTOFF"]

    # 声明为已证明的历史重建时，同一份时间戳不再被首见门禁拦下。
    reconstructed = SimpleNamespace(
        version_id="forged-reconstructed", metric="revenue",
        first_seen_at=datetime(2026, 9, 26, 7, 0, tzinfo=UTC),
        available_at=datetime(2026, 9, 20, 7, 0, tzinfo=UTC),
        availability_basis=AvailabilityBasis.RECONSTRUCTED,
        pit_mode=PitMode.HISTORICAL_RECONSTRUCTED,
    )
    admitted, rejected = admissible_facts(
        facts=[reconstructed], cutoff=CUTOFF, allow_reconstructed=True)
    assert admitted == [reconstructed] and rejected == []


def test_reconstruction_is_exempt_from_first_seen_but_not_from_basis() -> None:
    # 历史重建保留今天的真实首见时间，但把可用时点重建到历史 cutoff；
    # 这正是前向分支会拒绝、而重建分支必须接受的那条记录。
    proven = _fact(first_seen=LATER, available_at=CUTOFF, basis="RECONSTRUCTED",
                   mode="HISTORICAL_RECONSTRUCTED")
    admitted, rejected = admissible_facts(
        facts=[proven], cutoff=CUTOFF, allow_reconstructed=True)
    assert admitted == [proven] and rejected == []

    unproven = _fact(first_seen=LATER, available_at=CUTOFF, basis="UNKNOWN",
                     mode="HISTORICAL_RECONSTRUCTED")
    admitted, rejected = admissible_facts(
        facts=[unproven], cutoff=CUTOFF, allow_reconstructed=True)
    assert admitted == []
    assert [reason for _, reason in rejected] == ["S2_FACT_RECONSTRUCTION_UNPROVEN"]


def test_reconstruction_is_not_admitted_unless_explicitly_requested() -> None:
    fact = _fact(first_seen=LATER, available_at=CUTOFF, basis="RECONSTRUCTED",
                 mode="HISTORICAL_RECONSTRUCTED")
    admitted, rejected = admissible_facts(
        facts=[fact], cutoff=CUTOFF, allow_reconstructed=False)
    assert admitted == []
    assert [reason for _, reason in rejected] == ["S2_FACT_RECONSTRUCTION_NOT_REQUESTED"]


def test_future_availability_is_refused_in_both_modes() -> None:
    future = datetime(2026, 10, 1, tzinfo=UTC)
    for mode, basis in (("LIVE_OBSERVED", "OBSERVED"),
                        ("HISTORICAL_RECONSTRUCTED", "RECONSTRUCTED")):
        fact = _fact(first_seen=CUTOFF, available_at=future, basis=basis, mode=mode)
        admitted, rejected = admissible_facts(
            facts=[fact], cutoff=CUTOFF, allow_reconstructed=True)
        assert admitted == []
        assert [reason for _, reason in rejected] == ["S2_FACT_NOT_YET_AVAILABLE"]
