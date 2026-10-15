from datetime import date
from aquant.adapters.providers.cninfo_financial_candidate import _HEADING, _UNIT, _UNIT_MULTIPLIER, _field_matches
from aquant.adapters.providers.cninfo_statement_sections import (
    _combined_statement_header, _combined_scope_column_count, _split_six_month_header)
from aquant.adapters.providers.cninfo_statement_geometry import _dated_label
from aquant.adapters.providers.cninfo_statement_geometry import _header
from types import SimpleNamespace


def test_explicit_halfyear_combined_four_scope_columns():
    assert _HEADING.fullmatch('2025年6月30日合并及公司资产负债表')
    assert _HEADING.fullmatch('2025年上半年合并及公司利润表')
    lines=['2025年上半年 2024年上半年 2025年上半年 2024年上半年',
           '项目 附注','合并* 合并* 公司* 公司*']
    assert _combined_scope_column_count(lines) == 4
    assert _combined_statement_header(lines,date(2025,6,30)) == ('2025年06月30日','2024年06月30日')
    assert _combined_scope_column_count(['合并* 公司* 合并* 公司*']) is None
    assert _combined_scope_column_count(['合并* 合并* 公司* 公司* 附加说明']) is None


def test_geometry_independently_selects_explicit_consolidated_half_of_four_cells():
    labels=['项目','附注','2025年上半年\n合并*','2024年上半年\n合并*','2025年上半年\n公司*','2024年上半年\n公司*']
    table=SimpleNamespace(extract=lambda:[labels],rows=[SimpleNamespace(cells=[(i*10,0,(i+1)*10,10) for i in range(6)])])
    found=_header(table,date(2025,6,30),'利润表','',date(2025,6,30))
    assert found['column'] == 2 and found['prior_column'] == 3
    labels[3] += '\n(经重述)'
    labels[5] += '\n(经重述)'
    assert _header(table,date(2025,6,30),'利润表','',date(2025,6,30))['column'] == 2
    labels[3]='2024年上半年\n公司*'
    assert _header(table,date(2025,6,30),'利润表','',date(2025,6,30)) is None


def test_explicit_million_is_normalized_only_when_original_declares_it():
    assert _UNIT.search('人民币百万元')[2] == '百万元'
    assert _UNIT_MULTIPLIER['百万元'] == 1000000
    assert _UNIT.search('美元百万元') is None
    assert _UNIT.search('人民币百万元日期') is None
    assert _field_matches('operating_cashflow','经营活动(使用)/产生的现金流量净额')
    assert not _field_matches('operating_cashflow','投资活动(使用)/产生的现金流量净额')


def test_split_six_month_header_requires_year_date_duration_alignment():
    lines=['人民币百万元','截至2026年 截至2025年','6月30日止 6月30日止','项目 附注 6个月期间 6个月期间']
    expected=('截至2026年6月30日止6个月期间','截至2025年6月30日止6个月期间')
    assert _split_six_month_header(lines,date(2026,6,30)) == expected
    assert _dated_label(expected[0]) == date(2026,6,30)
    assert _split_six_month_header(lines,date(2025,6,30)) is None
    assert _split_six_month_header(lines,date(2026,12,31)) is None
    assert _split_six_month_header([*lines[:-1],'项目 附注 3个月期间 3个月期间'],date(2026,6,30)) is None
    assert _split_six_month_header([lines[0],lines[1],'6月30日止 3月31日止',lines[3]],date(2026,6,30)) is None
