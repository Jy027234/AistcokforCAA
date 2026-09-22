"""S2 领域公式、PIT 和严格缺失门禁的最小黄金用例。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from aquant.adapters.providers.sina_financial import SinaS2Row
from aquant.adapters.providers.sina_facts import sina_s2_row_to_facts
from aquant.domain.fundamentals.disclosure_link import (
    DisclosureCandidate,
    link_statement_to_disclosures,
)
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
    DEFAULT_S2_METRICS,
    S2ComputationError,
    S2Template,
    build_s2_signals,
    compute_s2_factors,
    cross_sectional_ranks,
)


UTC = timezone.utc
CUTOFF = datetime(2026, 9, 30, 12, tzinfo=UTC)


def _fact(
    instrument_id: str,
    metric: str,
    period_end: date,
    value: str | None,
    *,
    profit_scope: ProfitScope | None = None,
    source_document_id: str | None = None,
    content_hash: str | None = None,
    available_at: datetime | None = None,
    version_id: str | None = None,
) -> FinancialFact:
    available = available_at or datetime(2026, 1, 2, 9, tzinfo=UTC)
    source_doc = source_document_id or f"doc-{period_end.isoformat()}"
    version = version_id or f"{instrument_id}-{metric}-{period_end.isoformat()}"
    return FinancialFact(
        instrument_id=instrument_id,
        metric=metric,
        period_end=period_end,
        statement_scope=StatementScope.CONSOLIDATED,
        profit_scope=profit_scope,
        value=value,
        currency="CNY",
        raw_unit="yuan",
        source_id="synthetic-sina-cninfo",
        source_document_id=source_doc,
        source_published_date=date(2026, 1, 1),
        source_published_at=datetime(2026, 1, 1, 8, tzinfo=UTC),
        timestamp_precision=TimestampPrecision.SECOND,
        first_seen_at=available,
        ingested_at=available,
        available_at=available,
        availability_basis=AvailabilityBasis.OBSERVED,
        pit_mode=PitMode.LIVE_OBSERVED,
        # A report bundle is represented by one content hash across all five
        # metric facts, as the Sina adapter does for its three-page digest.
        content_hash=content_hash or f"sha256:{source_doc}",
        version_id=version,
    )


def _facts(instrument_id: str = "SYN.SSE.000001", *, omit: set[tuple[str, date]] | None = None,
           null_value: tuple[str, date] | None = None) -> list[FinancialFact]:
    omit = omit or set()
    rows: list[FinancialFact] = []

    # Latest endpoint is 2026Q2. Flow values are cumulative YTD values;
    # current TTM = 2025 annual + 2026Q2 YTD - 2025Q2 YTD.
    periods = {
        "attr": {date(2025, 6, 30): "20", date(2025, 12, 31): "100", date(2026, 6, 30): "30"},
        "consolidated": {date(2025, 6, 30): "24", date(2025, 12, 31): "120", date(2026, 6, 30): "36"},
        "cash": {date(2025, 6, 30): "12", date(2025, 12, 31): "60", date(2026, 6, 30): "18"},
        # Extra prior-year periods are required to form the prior-year revenue TTM.
        "revenue": {
            date(2024, 6, 30): "200", date(2024, 12, 31): "400",
            date(2025, 6, 30): "220", date(2025, 12, 31): "500",
            date(2026, 6, 30): "300",
        },
        "equity": {date(2025, 6, 30): "400", date(2026, 6, 30): "500"},
    }
    metric_scope = {
        "attr": ("net_profit_attributable", ProfitScope.ATTRIBUTABLE),
        "consolidated": ("net_profit_consolidated", ProfitScope.CONSOLIDATED),
        "cash": ("operating_cashflow", None),
        "revenue": ("revenue", None),
        "equity": ("parent_equity", None),
    }
    for kind, values in periods.items():
        metric, scope = metric_scope[kind]
        for period_end, value in values.items():
            key = (kind, period_end)
            if key in omit:
                continue
            if null_value == key:
                value = None
            rows.append(_fact(
                instrument_id, metric, period_end, value,
                profit_scope=scope,
                source_document_id=f"{instrument_id}-doc-{period_end.isoformat()}",
            ))
    return rows


def test_default_metric_contract_uses_provider_neutral_semantic_names() -> None:
    assert DEFAULT_S2_METRICS.attributable_net_profit == "net_profit_attributable"
    assert DEFAULT_S2_METRICS.consolidated_net_profit == "net_profit_consolidated"
    assert DEFAULT_S2_METRICS.operating_cash_flow == "operating_cashflow"
    assert DEFAULT_S2_METRICS.revenue == "revenue"
    assert DEFAULT_S2_METRICS.parent_equity == "parent_equity"


def test_s2_computes_all_four_factors_from_pit_cumulative_facts() -> None:
    factors = compute_s2_factors(
        store=VersionedFinancialFactStore(_facts()),
        instrument_id="SYN.SSE.000001",
        cutoff=CUTOFF,
        market_cap=Decimal("1000"),
    )

    # F07: 110 / ((400 + 500) / 2) = 11/45.
    assert factors.f07_roe_ttm == Decimal(110) / Decimal(450)
    # F08: (60 + 18 - 12) / (120 + 36 - 24) = 66 / 132.
    assert factors.f08_cash_quality == Decimal("0.5")
    # F09: (500 + 300 - 220) / (400 + 220 - 200) - 1 = 580/420 - 1.
    assert factors.f09_revenue_ttm_yoy == Decimal(580) / Decimal(420) - Decimal(1)
    # F10 keeps the sign and uses the explicit decision-date market cap.
    assert factors.f10_earning_yield == Decimal("0.11")
    assert factors.latest_period_end == date(2026, 6, 30)
    assert factors.ttm_start_period_end == date(2025, 6, 30)
    assert len(factors.fact_version_ids) > 0


def test_s2_rejects_same_report_id_with_mixed_content_hashes() -> None:
    rows = _facts()
    target = next(
        index for index, row in enumerate(rows)
        if row.metric == "parent_equity" and row.period_end == date(2026, 6, 30)
    )
    rows[target] = replace(rows[target], content_hash="sha256:revised-report")

    with pytest.raises(S2ComputationError) as exc:
        compute_s2_factors(
            store=VersionedFinancialFactStore(rows),
            instrument_id="SYN.SSE.000001",
            cutoff=CUTOFF,
            market_cap=Decimal("1000"),
        )

    assert exc.value.code == "S2_REPORT_VERSION_MISMATCH"


def _verified_sina_row(period_end: date, *, revenue: str, net_income: str,
                       attributable: str, equity: str, cashflow: str) -> SinaS2Row:
    """Construct a provider-shaped row whose disclosure evidence is added below."""

    return SinaS2Row(
        stock_code="600519",
        end_date=period_end,
        revenue=Decimal(revenue),
        net_income=Decimal(net_income),
        net_income_attributable=Decimal(attributable),
        parent_equity=Decimal(equity),
        operating_cashflow=Decimal(cashflow),
        source_id="sina-financial",
        retrieved_at=datetime(2026, 9, 19, 12, tzinfo=UTC),
        content_hashes=(
            f"sha256:profit-{period_end.isoformat()}",
            f"sha256:balance-{period_end.isoformat()}",
            f"sha256:cash-{period_end.isoformat()}",
        ),
        amount_unit="yuan",
    )


def test_sina_adapter_facts_flow_into_s2_with_default_metric_contract() -> None:
    """Provider-shaped values must pass the same production PIT and scope gates."""

    rows = [
        _verified_sina_row(date(2024, 6, 30), revenue="200", net_income="30",
                           attributable="25", equity="350", cashflow="10"),
        _verified_sina_row(date(2024, 12, 31), revenue="400", net_income="40",
                           attributable="35", equity="380", cashflow="20"),
        _verified_sina_row(date(2025, 6, 30), revenue="220", net_income="24",
                           attributable="20", equity="400", cashflow="12"),
        _verified_sina_row(date(2025, 12, 31), revenue="500", net_income="120",
                           attributable="100", equity="450", cashflow="60"),
        _verified_sina_row(date(2026, 6, 30), revenue="300", net_income="36",
                           attributable="30", equity="500", cashflow="18"),
    ]
    announced_on = {
        date(2024, 6, 30): date(2024, 8, 20),
        date(2024, 12, 31): date(2025, 3, 20),
        date(2025, 6, 30): date(2025, 8, 20),
        date(2025, 12, 31): date(2026, 3, 20),
        date(2026, 6, 30): date(2026, 8, 20),
    }
    disclosures = []
    for row in rows:
        titles = {
            6: f"{row.end_date.year}年半年度报告",
            12: f"{row.end_date.year}年年度报告",
        }
        link = link_statement_to_disclosures(
            stock_code=row.stock_code,
            period_end=row.end_date,
            value_source_id=row.source_id,
            value_content_hashes=row.content_hashes,
            announcements=[DisclosureCandidate(
                announcement_id=f"cninfo-{row.end_date.isoformat()}",
                sec_code=row.stock_code,
                title=titles[row.end_date.month],
                announced_on=announced_on[row.end_date],
                document_url="https://static.cninfo.com.cn/verified-fixture.pdf",
            )],
        )
        disclosures.append(replace(link, pit_eligible=True, pit_blocker=""))

    calendar = [
        date(2024, 8, 21), date(2025, 3, 21), date(2025, 8, 21),
        date(2026, 3, 23), date(2026, 8, 21),
    ]
    facts = []
    for row, disclosure in zip(rows, disclosures):
        facts.extend(sina_s2_row_to_facts(
            row,
            disclosure=disclosure,
            trading_calendar=calendar,
            first_seen_at=datetime(2026, 9, 19, 12, tzinfo=UTC),
            ingested_at=datetime(2026, 9, 19, 12, tzinfo=UTC),
        ))

    assert len(facts) == len(rows) * 5
    assert {fact.availability_basis for fact in facts} == {AvailabilityBasis.RECONSTRUCTED}
    assert {fact.pit_mode for fact in facts} == {PitMode.HISTORICAL_RECONSTRUCTED}

    factors = compute_s2_factors(
        store=VersionedFinancialFactStore(facts),
        instrument_id="SH.600519",
        cutoff=CUTOFF,
        market_cap=Decimal("1000"),
    )
    assert factors.f07_roe_ttm == Decimal(110) / Decimal(450)
    assert factors.f08_cash_quality == Decimal("0.5")
    assert factors.f09_revenue_ttm_yoy == Decimal(580) / Decimal(420) - Decimal(1)
    assert factors.f10_earning_yield == Decimal("0.11")


def test_s2_rejects_missing_fact_instead_of_zero_filling() -> None:
    store = VersionedFinancialFactStore(
        _facts(omit={("revenue", date(2025, 6, 30))})
    )

    with pytest.raises(S2ComputationError) as exc:
        compute_s2_factors(
            store=store,
            instrument_id="SYN.SSE.000001",
            cutoff=CUTOFF,
            market_cap=Decimal("1000"),
        )
    assert exc.value.code == "S2_TTM_INCOMPLETE"
    assert "zero filling" in exc.value.message


def test_s2_does_not_use_future_revision_at_cutoff() -> None:
    rows = _facts()
    rows.append(_fact(
        "SYN.SSE.000001", "revenue", date(2026, 6, 30), "999",
        source_document_id="SYN.SSE.000001-doc-2026-06-30-revision",
        available_at=datetime(2026, 10, 2, 9, tzinfo=UTC),
        version_id="future-revenue-revision",
    ))
    # The future revision is not a PIT input. It is also not linked with
    # supersedes_id here, so the store must still refuse the ambiguous
    # current-period chain once the revision becomes available; at this cutoff
    # the original version remains the sole eligible head.
    factors = compute_s2_factors(
        store=VersionedFinancialFactStore(rows),
        instrument_id="SYN.SSE.000001",
        cutoff=CUTOFF,
        market_cap=Decimal("1000"),
    )
    assert factors.f09_revenue_ttm_yoy == Decimal(580) / Decimal(420) - Decimal(1)


def test_financial_template_is_explicitly_rejected() -> None:
    with pytest.raises(S2ComputationError) as exc:
        compute_s2_factors(
            store=VersionedFinancialFactStore(_facts()),
            instrument_id="SYN.SSE.000001",
            cutoff=CUTOFF,
            market_cap=Decimal("1000"),
            industry_template=S2Template.FINANCIAL,
        )
    assert exc.value.code == "S2_INDUSTRY_TEMPLATE_UNSUPPORTED"


def test_cross_sectional_ranks_use_average_ties_and_half_for_all_equal() -> None:
    assert cross_sectional_ranks({"A": Decimal("3"), "B": Decimal("2"), "C": Decimal("1")}) == {
        "A": 1.0, "B": 0.5, "C": 0.0,
    }
    assert cross_sectional_ranks({"A": Decimal("1"), "B": Decimal("1")}) == {
        "A": 0.5, "B": 0.5,
    }
    assert cross_sectional_ranks({"A": Decimal("1")}) == {"A": 0.5}


def test_s2_signal_ranks_only_complete_objects_and_uses_frozen_formula() -> None:
    store = VersionedFinancialFactStore(
        _facts("SYN.SSE.000001") + _facts("SYN.SSE.000002")
    )
    evaluated = build_s2_signals(
        store=store,
        instrument_ids=["SYN.SSE.000001", "SYN.SSE.000002", "SYN.SSE.MISSING"],
        cutoff=CUTOFF,
        market_cap_by_instrument={
            "SYN.SSE.000001": Decimal("1000"),
            "SYN.SSE.000002": Decimal("500"),
        },
    )
    by_id = {item.instrument_id: item for item in evaluated}
    first = by_id["SYN.SSE.000001"]
    second = by_id["SYN.SSE.000002"]
    missing = by_id["SYN.SSE.MISSING"]

    assert first.signal is not None and second.signal is not None
    assert first.signal.quality_rank == pytest.approx(0.5)
    assert second.signal.quality_rank == pytest.approx(0.5)
    assert first.signal.value_rank == pytest.approx(0.0)
    assert second.signal.value_rank == pytest.approx(1.0)
    assert first.signal.signal_rank == pytest.approx(0.25)
    assert second.signal.signal_rank == pytest.approx(0.75)
    assert missing.signal is None
    assert missing.exclusion_code == "S2_FINANCIAL_FACT_MISSING"
