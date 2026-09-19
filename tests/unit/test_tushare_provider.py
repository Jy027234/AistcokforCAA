"""Tushare 财报适配层的离线高价值用例。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from aquant.adapters.providers.tushare import (
    BALANCE_API,
    CASHFLOW_API,
    INCOME_API,
    SOURCE_ID,
    StatementBatch,
    TushareOfflineError,
    TushareProFinancials,
    TushareValidationError,
    normalize_financial_batches,
)


KEYS = {
    "ts_code": "600519.SH",
    "end_date": "20260630",
    "ann_date": "20260829",
    "f_ann_date": "20260829",
    "report_type": "1",
    "comp_type": "1",
    "update_flag": "0",
}


def _batches(*, source_id: str = SOURCE_ID, report_type: str = "1",
             include_ann_date: bool = True) -> tuple[StatementBatch, StatementBatch, StatementBatch]:
    common = dict(KEYS, report_type=report_type)
    if not include_ann_date:
        common.pop("ann_date")
    income = dict(common, n_income_attr_p="10.25", n_income="11.00",
                  revenue="100.50")
    balance = dict(common, total_hldr_eqy_exc_min_int="80.00")
    cashflow = dict(common, n_cashflow_act="14.00")
    return tuple(StatementBatch(api, source_id, (row,), datetime(2026, 9, 19, tzinfo=timezone.utc))
                 for api, row in ((INCOME_API, income), (BALANCE_API, balance),
                                  (CASHFLOW_API, cashflow)))  # type: ignore[return-value]


def test_default_reader_is_offline_and_does_not_need_token() -> None:
    reader = TushareProFinancials()
    with pytest.raises(TushareOfflineError):
        reader.fetch_bundle("600519.SH", period="2026-06-30")


def test_injected_transport_normalizes_alias_and_retains_revision_metadata() -> None:
    batches = _batches()
    responses = {
        INCOME_API: batches[0].rows,
        BALANCE_API: batches[1].rows,
        CASHFLOW_API: batches[2].rows,
    }

    def transport(api_name: str, params: dict[str, object]):
        assert params["ts_code"] == "600519.SH"
        return responses[api_name]

    out = TushareProFinancials(
        transport=transport,
        clock=lambda: datetime(2026, 9, 19, tzinfo=timezone.utc),
    ).fetch_bundle("600519.SH", period="20260630")
    assert len(out) == 1
    row = out[0]
    assert row.revenue == Decimal("100.50")
    assert row.n_income_attr_p == Decimal("10.25")
    assert row.source_id == SOURCE_ID
    assert row.revision.update_flag == "0"
    assert row.revision.f_ann_date is not None
    assert row.as_dict()["source"]["apis"] == [INCOME_API, BALANCE_API, CASHFLOW_API]


def test_revenue_is_preferred_over_distinct_total_revenue() -> None:
    income, balance, cashflow = _batches()
    row = dict(income.rows[0], total_revenue="101.75")
    changed_income = StatementBatch(
        INCOME_API, SOURCE_ID, (row,), income.retrieved_at)
    out = normalize_financial_batches(
        income=changed_income, balancesheet=balance, cashflow=cashflow)
    assert out[0].revenue == Decimal("100.50")


@pytest.mark.parametrize("bad", [
    {"include_ann_date": False},
    {"report_type": "2"},
])
def test_missing_announcement_or_wrong_report_type_is_rejected(bad: dict[str, object]) -> None:
    income, balance, cashflow = _batches(**bad)
    with pytest.raises(TushareValidationError):
        normalize_financial_batches(
            income=income, balancesheet=balance, cashflow=cashflow)


def test_cross_source_and_mismatched_revision_are_rejected() -> None:
    income, balance, cashflow = _batches()
    foreign_balance = StatementBatch(
        BALANCE_API, "other-source", balance.rows, balance.retrieved_at)
    with pytest.raises(TushareValidationError, match="cross-source"):
        normalize_financial_batches(
            income=income, balancesheet=foreign_balance, cashflow=cashflow)

    changed = dict(cashflow.rows[0], update_flag="1")
    changed_cashflow = StatementBatch(
        CASHFLOW_API, SOURCE_ID, (changed,), cashflow.retrieved_at)
    with pytest.raises(TushareValidationError, match="do not match"):
        normalize_financial_batches(
            income=income, balancesheet=balance, cashflow=changed_cashflow)
