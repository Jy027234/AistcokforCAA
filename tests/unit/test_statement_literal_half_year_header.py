"""January-June is an explicit H1 duration, not a generic date guess."""
from datetime import date
import pytest
from aquant.adapters.providers.cninfo_financial_pdf import CninfoFinancialPdfError
from aquant.adapters.providers.cninfo_statement_sections import _column_header_labels,_header_for_statement
from aquant.adapters.providers.cninfo_statement_geometry import _dated_label


def test_actual_tcl_and_bohai_header_is_literal_two_year_half_year():
    labels=_column_header_labels('附注五 2025年1-6月 2024年1-6月')
    assert labels==['2025年1-6月','2024年1-6月']
    assert _header_for_statement('利润表',labels,date(2025,6,30))==tuple(labels)
    assert _dated_label(labels[0])==date(2025,6,30)


def test_actual_linglong_relative_comparison_keeps_raw_labels():
    labels=_column_header_labels('项目 附注 本期发生额 上期发生额')
    assert _header_for_statement('利润表',labels,date(2026,6,30))==('本期发生额','上期发生额')


@pytest.mark.parametrize('labels,period',[
    (['2025年1-6月','2024年1-6月'],date(2025,12,31)),
    (['2026年1-6月','2024年1-6月'],date(2025,6,30)),
    (['2025年1-3月','2024年1-3月'],date(2025,6,30)),
    (['2025年1-6月','2025年1-6月'],date(2025,6,30)),
    (['2024年1-6月','2025年1-6月'],date(2025,6,30)),
    (['上期发生额','本期发生额'],date(2025,6,30)),
])
def test_wrong_year_type_or_duplicate_period_is_not_h1(labels,period):
    with pytest.raises(CninfoFinancialPdfError):
        _header_for_statement('利润表',labels,period)


def test_unmarked_narrative_half_year_comparison_is_not_a_header():
    assert _column_header_labels('2025年1-6月收入比2024年1-6月减少') is None
    assert _dated_label('2025年1-3月') is None
    assert _dated_label('2025年1-6月营业收入') is None
