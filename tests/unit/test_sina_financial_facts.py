from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from aquant.adapters.providers.sina_financial import (
    merge_s2_pages,
    parse_statement_page,
)
from aquant.adapters.providers.sina_facts import (
    METRIC_NET_PROFIT_ATTRIBUTABLE,
    METRIC_NET_PROFIT_CONSOLIDATED,
    METRIC_OPERATING_CASHFLOW,
    METRIC_PARENT_EQUITY,
    METRIC_REVENUE,
    normalize_sina_instrument_id,
    sina_s2_row_to_facts,
)
from aquant.domain.data.pit import AvailabilityBasis, PitMode, PitViolation
from aquant.domain.fundamentals.disclosure_link import (
    DisclosureCandidate,
    link_statement_to_disclosures,
)
from aquant.domain.fundamentals.versioned import VersionedFinancialFactStore


UTC = timezone.utc
OBSERVED = datetime(2026, 9, 19, 12, tzinfo=UTC)


def _page(rows: list[tuple[str, str]]) -> str:
    body = "".join(f"<tr><td>{label}</td><td>{value}</td></tr>" for label, value in rows)
    return (
        "<table><tr><th>单位：万元</th></tr>"
        "<tr><td>报表日期</td><td>2025-12-31</td></tr>"
        f"{body}</table>"
    )


def _row(*, revenue: str = "100", cashflow: str = "12"):
    profit = parse_statement_page(_page([
        ("营业收入", revenue),
        ("净利润", "10"),
        ("归属于母公司所有者的净利润", "9"),
    ]), stock_code="600519", statement="profit", retrieved_at=OBSERVED)
    balance = parse_statement_page(_page([
        ("归属于母公司股东权益合计", "50"),
    ]), stock_code="600519", statement="balance", retrieved_at=OBSERVED)
    cash = parse_statement_page(_page([
        ("经营活动产生的现金流量净额", cashflow),
    ]), stock_code="600519", statement="cashflow", retrieved_at=OBSERVED)
    return merge_s2_pages(profit=profit, balance=balance, cashflow=cash)[0]


def _link(row):
    return link_statement_to_disclosures(
        stock_code=row.stock_code,
        period_end=row.end_date,
        value_source_id=row.source_id,
        value_content_hashes=row.content_hashes,
        announcements=[DisclosureCandidate(
            announcement_id="cninfo-2025-annual",
            sec_code=row.stock_code,
            title="2025年年度报告",
            announced_on=date(2026, 3, 20),
            document_url="https://static.cninfo.com.cn/report.pdf",
        )],
    )


def test_unlinked_sina_row_maps_all_four_metrics_but_is_not_formal_pit():
    row = _row()
    facts = sina_s2_row_to_facts(row)

    assert {fact.metric for fact in facts} == {
        METRIC_NET_PROFIT_ATTRIBUTABLE,
        METRIC_NET_PROFIT_CONSOLIDATED,
        METRIC_PARENT_EQUITY,
        METRIC_OPERATING_CASHFLOW,
        METRIC_REVENUE,
    }
    assert all(fact.source_id == "sina-financial" for fact in facts)
    assert all(fact.instrument_id == "SH.600519" for fact in facts)
    assert all(fact.source_document_id.startswith("sina-observation:") for fact in facts)
    assert all(fact.content_hash.startswith("sha256:") for fact in facts)
    assert {fact.content_hash for fact in facts} == {row.content_hash}
    assert all(fact.raw_unit == "yuan" for fact in facts)
    assert all(fact.statement_scope.value == "CONSOLIDATED" for fact in facts)
    assert all(fact.availability_basis is AvailabilityBasis.UNKNOWN for fact in facts)
    assert all(fact.pit_mode is PitMode.HISTORICAL_RECONSTRUCTED for fact in facts)
    assert facts[0].available_at == OBSERVED

    with pytest.raises(PitViolation, match="UNKNOWN"):
        VersionedFinancialFactStore(facts).select_pit(OBSERVED)


def test_cninfo_link_preserves_announcement_id_and_date_but_unverified_stays_unknown():
    row = _row()
    facts = sina_s2_row_to_facts(
        row,
        disclosure=_link(row),
        trading_calendar=[date(2026, 3, 23), date(2026, 3, 24)],
    )

    assert {fact.source_document_id for fact in facts} == {"cninfo-2025-annual"}
    assert {fact.source_published_date for fact in facts} == {date(2026, 3, 20)}
    assert {fact.availability_basis for fact in facts} == {AvailabilityBasis.UNKNOWN}
    assert facts[0].available_at == datetime(2026, 3, 23, 0, 45, tzinfo=UTC)


def test_verified_cninfo_link_can_enter_formal_pit_without_filling_missing_values():
    row = _row(revenue="--")
    linked = replace(_link(row), pit_eligible=True, pit_blocker="")
    facts = sina_s2_row_to_facts(
        row,
        disclosure=linked,
        trading_calendar=[date(2026, 3, 23), date(2026, 3, 24)],
    )

    by_metric = {fact.metric: fact for fact in facts}
    assert by_metric[METRIC_REVENUE].value is None
    assert by_metric[METRIC_NET_PROFIT_ATTRIBUTABLE].value == Decimal("90000")
    assert by_metric[METRIC_NET_PROFIT_CONSOLIDATED].value == Decimal("100000")
    assert {fact.availability_basis for fact in facts} == {AvailabilityBasis.RECONSTRUCTED}
    selected = VersionedFinancialFactStore(facts).select_pit(
        datetime(2026, 3, 23, 1, tzinfo=UTC)
    )
    assert len(selected) == 5


@pytest.mark.parametrize(("raw", "expected"), [
    ("600519", "SH.600519"),
    ("sh.688001", "SH.688001"),
    ("900901", "SH.900901"),
    ("000001", "SZ.000001"),
    ("sz.300750", "SZ.300750"),
    ("200011", "SZ.200011"),
])
def test_sina_code_normalization_covers_supported_market_prefixes(raw, expected):
    assert normalize_sina_instrument_id(raw) == expected
