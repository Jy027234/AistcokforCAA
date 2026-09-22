from datetime import date
from decimal import Decimal

from aquant.domain.fundamentals.pit import FinancialStatement
from aquant.domain.fundamentals.records import consistency_violations


def _statement(period: str, profit: int, shares: str = "1000") -> FinancialStatement:
    return FinancialStatement(
        instrument_id="SYN.TEST",
        stat_date=date.fromisoformat(period),
        pub_date=date(2026, 9, 1),
        net_profit_micros=profit,
        revenue_micros=None,
        roe_avg=None,
        eps_ttm_micros=None,
        cfo_to_np=None,
        total_share=Decimal(shares),
        source_id="synthetic",
    )


def test_cumulative_profit_may_decline_when_a_later_quarter_loses_money():
    rows = [
        _statement("2026-03-31", 100),
        _statement("2026-06-30", 80),
    ]

    assert consistency_violations(rows) == []


def test_zero_profit_is_a_valid_financial_fact():
    assert consistency_violations([_statement("2026-06-30", 0)]) == []
