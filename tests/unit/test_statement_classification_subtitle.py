from decimal import Decimal
from aquant.adapters.providers.cninfo_statement_sections import _dewrapped,_row_amounts


def test_actual_000415_classification_is_not_a_wrapped_field_prefix():
    source=[(65,'按所有权归属分类'),
            (65,'归属于母公司股东的净利润 (2,018,780 ) 716,400'),
            (65,'少数股东损益 713,810 497,729')]
    assert _dewrapped(source)==source
    current=_row_amounts(_dewrapped(source)[1][1])
    assert current[0]=='归属于母公司股东的净利润'
    assert current[2]==Decimal('-2018780')


def test_actual_000415_two_period_header_does_not_prefix_first_revenue():
    source=[(65,'附注五 2025年1-6月 2024年1-6月'),
            (65,'营业收入 41 28,460,034 16,178,569')]
    assert _dewrapped(source)==source
    assert _row_amounts(_dewrapped(source)[1][1])[0]=='营业收入 41'


def test_field_containing_a_period_word_is_not_discarded_as_a_header():
    source=[(65,'归属于母公司所有者'),
            (65,'权益合计 53,595,106 53,167,609')]
    assert any('归属于母公司所有者权益合计' in line for _,line in _dewrapped(source))
