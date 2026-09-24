from datetime import date

from aquant.domain.research.s2 import required_s2_periods


def test_midyear_requires_five_periods_and_only_relevant_metrics() -> None:
    requirements = required_s2_periods(date(2026, 6, 30))

    assert requirements["net_profit_attributable"] == (
        date(2025, 6, 30), date(2025, 12, 31), date(2026, 6, 30))
    assert requirements["net_profit_consolidated"] == requirements["operating_cashflow"]
    assert requirements["revenue"] == (
        date(2024, 6, 30), date(2024, 12, 31), date(2025, 6, 30),
        date(2025, 12, 31), date(2026, 6, 30))
    assert requirements["parent_equity"] == (date(2025, 6, 30), date(2026, 6, 30))
    assert [metric for metric, periods in requirements.items()
            if date(2024, 12, 31) in periods] == ["revenue"]
    assert sum(map(len, requirements.values())) == 16


def test_year_end_includes_prior_profit_for_same_report_bundle() -> None:
    requirements = required_s2_periods(date(2025, 12, 31))

    assert requirements["net_profit_attributable"] == (
        date(2024, 12, 31), date(2025, 12, 31))
    assert requirements["revenue"] == (date(2024, 12, 31), date(2025, 12, 31))
    assert requirements["parent_equity"] == (date(2024, 12, 31), date(2025, 12, 31))
    assert sum(map(len, requirements.values())) == 8
