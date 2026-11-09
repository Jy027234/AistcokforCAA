from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from aquant.domain.fundamentals.versioned import (
    AvailabilityBasis,
    FinancialFact,
    PitMode,
    ProfitScope,
    StatementScope,
    TimestampPrecision,
    VersionedFinancialFactStore,
)
from aquant.domain.research.s2 import (
    S2ComputationError,
    build_s2_signals,
    compute_s2_factors,
    compute_s2_financial_inputs,
)


UTC = timezone.utc
CURRENT_CUTOFF = datetime(2026, 11, 6, 12, 44, 44, tzinfo=UTC)
NEXT_CUTOFF = datetime(2026, 11, 9, 0, 45, tzinfo=UTC)
GANNENG_H125_AVAILABLE = NEXT_CUTOFF
ANNUAL_HASH = "sha256:1063fd11f45f1190db6fbfb0db47c59088417410047b8676ccc38a11d96c2db4"
H126_HASH = "sha256:0908a0080bdd676f3fbddcd3180cf4b3a91c32c980fd3b7643b313972f7587b8"


def _fact(
    instrument_id: str,
    metric: str,
    period_end: date,
    value: str,
    *,
    source_document_id: str,
    content_hash: str,
    available_at: datetime,
    profit_scope: ProfitScope | None = None,
) -> FinancialFact:
    published_at = {
        "1225121246": datetime(2026, 4, 27, 8, 0, tzinfo=UTC),
        "1225490300": datetime(2026, 8, 22, 8, 0, tzinfo=UTC),
        "1225491235": datetime(2026, 8, 22, 8, 0, tzinfo=UTC),
        "1225466047": datetime(2026, 8, 22, 8, 0, tzinfo=UTC),
        "1224562024": datetime(2025, 8, 22, 8, 0, tzinfo=UTC),
        "1224561048": datetime(2025, 8, 22, 8, 0, tzinfo=UTC),
        "doc-2024-h1": datetime(2024, 8, 1, 8, 0, tzinfo=UTC),
        "doc-2024-fy": datetime(2025, 4, 1, 8, 0, tzinfo=UTC),
    }.get(source_document_id, datetime(2026, 4, 27, 8, 0, tzinfo=UTC))
    first_seen_at = available_at - timedelta(hours=1)
    return FinancialFact(
        instrument_id=instrument_id,
        metric=metric,
        period_end=period_end,
        statement_scope=StatementScope.CONSOLIDATED,
        profit_scope=profit_scope,
        value=value,
        currency="CNY",
        raw_unit="yuan",
        source_id="cninfo",
        source_document_id=source_document_id,
        source_published_date=published_at.date(),
        source_published_at=published_at,
        timestamp_precision=TimestampPrecision.SECOND,
        first_seen_at=first_seen_at,
        ingested_at=first_seen_at,
        available_at=available_at,
        availability_basis=AvailabilityBasis.OBSERVED,
        pit_mode=PitMode.LIVE_OBSERVED,
        content_hash=content_hash,
        version_id=f"{instrument_id}-{metric}-{period_end.isoformat()}",
    )


def _facts(
    instrument_id: str,
    *,
    exact_ganneng_sources: bool = False,
    split_h125_bundle: bool = False,
) -> list[FinancialFact]:
    h125 = date(2025, 6, 30)
    fy25 = date(2025, 12, 31)
    h126 = date(2026, 6, 30)
    h124 = date(2024, 6, 30)
    fy24 = date(2024, 12, 31)
    rows: list[FinancialFact] = []

    values = {
        "net_profit_attributable": {
            h125: ("425363785.32", ProfitScope.ATTRIBUTABLE),
            fy25: ("1006611278.98", ProfitScope.ATTRIBUTABLE),
            h126: ("250829035.99", ProfitScope.ATTRIBUTABLE),
        },
        "net_profit_consolidated": {
            h125: ("416090369.57", ProfitScope.CONSOLIDATED),
            fy25: ("1062682551.71", ProfitScope.CONSOLIDATED),
            h126: ("356717010.94", ProfitScope.CONSOLIDATED),
        },
        "operating_cashflow": {
            h125: ("1172977139.83", None),
            fy25: ("2181535233.61", None),
            h126: ("1579671572.90", None),
        },
        "revenue": {
            h124: ("3077522689.66", None),
            fy24: ("6920945654.47", None),
            h125: ("3040821229.13", None),
            fy25: ("7398196551.95", None),
            h126: ("4084197900.76", None),
        },
        "parent_equity": {
            h125: ("5946446666.41", None),
            h126: ("6077823765.66", None),
        },
    }

    for metric, periods in values.items():
        for period_end, (value, profit_scope) in periods.items():
            if exact_ganneng_sources and period_end == fy25:
                source_document_id, content_hash = "1225121246", ANNUAL_HASH
            elif exact_ganneng_sources and period_end in (h125, h126):
                source_document_id, content_hash = "1225490300", H126_HASH
            elif split_h125_bundle and period_end == h125 and instrument_id == "SH.600578":
                source_document_id, content_hash = (
                    "1225491235",
                    "sha256:c61ffc6f3816d5b523059ae78f9e6206c2aee35e9d4b605c7bfaaf9fe0bac660",
                )
                if metric == "parent_equity":
                    source_document_id, content_hash = (
                        "1224562024",
                        "sha256:3364f3e7e9408d18b337834e011eaa5b9a0dd8301470ceca61710e4f9184101a",
                    )
            elif split_h125_bundle and period_end == h125 and instrument_id == "SZ.000680":
                source_document_id, content_hash = (
                    "1225466047",
                    "sha256:c7dddb4574fc5705eee1dc09b2733b091492537c0448c3bd1bd2f7ee9f849c90",
                )
                if metric == "operating_cashflow":
                    source_document_id, content_hash = (
                        "1224561048",
                        "sha256:d4dca5dcec17316a8094b65585454ed4e2e7fdc2af1e6e51f9935e075e39a39b",
                    )
            elif period_end == h124:
                source_document_id, content_hash = "doc-2024-h1", "sha256:2024h1"
            elif period_end == fy24:
                source_document_id, content_hash = "doc-2024-fy", "sha256:2024fy"
            else:
                source_document_id = f"doc-{period_end.isoformat()}"
                content_hash = f"sha256:{period_end.isoformat()}"

            available_at = CURRENT_CUTOFF - timedelta(days=1)
            if exact_ganneng_sources and period_end == h125:
                available_at = GANNENG_H125_AVAILABLE
            rows.append(_fact(
                instrument_id,
                metric,
                period_end,
                value,
                source_document_id=source_document_id,
                content_hash=content_hash,
                available_at=available_at,
                profit_scope=profit_scope,
            ))
    return rows


def test_ganneng_stays_pit_pending_then_blocks_unverified_comparability() -> None:
    instrument_id = "SZ.000899"
    store = VersionedFinancialFactStore(
        _facts(instrument_id, exact_ganneng_sources=True))

    with pytest.raises(S2ComputationError) as current_exc:
        compute_s2_financial_inputs(
            store=store, instrument_id=instrument_id, cutoff=CURRENT_CUTOFF)
    assert current_exc.value.code == "S2_TTM_INCOMPLETE"

    with pytest.raises(S2ComputationError) as future_bridge_exc:
        compute_s2_financial_inputs(
            store=store, instrument_id=instrument_id, cutoff=NEXT_CUTOFF)
    assert future_bridge_exc.value.code == "S2_COMPARABILITY_UNVERIFIED"
    assert "1225121246" in future_bridge_exc.value.message
    assert "1225490300" in future_bridge_exc.value.message

    with pytest.raises(S2ComputationError) as future_factor_exc:
        compute_s2_factors(
            store=store, instrument_id=instrument_id, cutoff=NEXT_CUTOFF,
            market_cap=Decimal("20000000000"))
    assert future_factor_exc.value.code == "S2_COMPARABILITY_UNVERIFIED"

    evaluation, = build_s2_signals(
        store=store, instrument_ids=[instrument_id], cutoff=NEXT_CUTOFF,
        market_cap_by_instrument={instrument_id: Decimal("20000000000")})
    assert evaluation.signal is None
    assert evaluation.exclusion_code == "S2_COMPARABILITY_UNVERIFIED"
    assert "1225121246" in evaluation.exclusion_reason
    assert "尚无已核实的同一合并范围证明" in evaluation.exclusion_reason


def test_clean_same_structure_bridge_and_factors_are_unchanged() -> None:
    instrument_id = "SYN.SSE.123456"
    store = VersionedFinancialFactStore(_facts(instrument_id))

    bridge = compute_s2_financial_inputs(
        store=store, instrument_id=instrument_id, cutoff=CURRENT_CUTOFF)
    assert bridge.attributable_net_profit_ttm == Decimal("832076529.65")
    assert bridge.consolidated_net_profit_ttm == Decimal("1003309193.08")
    assert bridge.operating_cashflow_ttm == Decimal("2588229666.68")
    assert bridge.revenue_ttm == Decimal("8441573223.58")
    assert bridge.prior_revenue_ttm == Decimal("6884244193.94")
    assert bridge.average_parent_equity == Decimal("6012135216.035")

    factors = compute_s2_factors(
        store=store, instrument_id=instrument_id, cutoff=CURRENT_CUTOFF,
        market_cap=Decimal("20000000000"))
    assert factors.values == {
        "F07": Decimal("832076529.65") / Decimal("6012135216.035"),
        "F08": Decimal("2588229666.68") / Decimal("1003309193.08"),
        "F09": Decimal("8441573223.58") / Decimal("6884244193.94") - Decimal("1"),
        "F10": Decimal("832076529.65") / Decimal("20000000000"),
    }


@pytest.mark.parametrize(
    ("instrument_id", "expected_bundle"),
    [("SH.600578", "F07"), ("SZ.000680", "F08")],
)
def test_existing_h125_mixed_report_bundle_rejection_remains(
    instrument_id: str, expected_bundle: str,
) -> None:
    store = VersionedFinancialFactStore(
        _facts(instrument_id, split_h125_bundle=True))

    with pytest.raises(S2ComputationError) as exc:
        compute_s2_factors(
            store=store, instrument_id=instrument_id, cutoff=CURRENT_CUTOFF,
            market_cap=Decimal("1000"))

    assert exc.value.code == "S2_REPORT_VERSION_MISMATCH"
    assert f"{expected_bundle} report bundle" in exc.value.message
