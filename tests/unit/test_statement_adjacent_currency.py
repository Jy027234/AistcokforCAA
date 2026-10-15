from types import SimpleNamespace
import pytest

from aquant.adapters.providers.cninfo_financial_candidate import _UNIT, _page_headings
from aquant.adapters.providers.cninfo_financial_pdf import CninfoFinancialPdfError


class _Page:
    def __init__(self, unit_line): self.unit_line = unit_line
    def extract_text(self):
        return '合并利润表\n2024年1—6月\n' + self.unit_line + '\n项目 附注 本期发生额 上期发生额'
    def search(self, _): return [{'top': 100, 'bottom': 120}]


def test_unit_followed_by_explicit_currency_declaration():
    headings = _page_headings(_Page('单位：元币种：人民币'), 90)
    assert len(headings) == 1 and headings[0].unit == '元'
    assert _UNIT.search('单位：万元币种：人民币').group(2) == '万元'


def test_adjacent_currency_still_rejects_non_cny():
    with pytest.raises(CninfoFinancialPdfError, match='non-CNY'):
        _page_headings(_Page('单位：元币种：美元'), 90)


@pytest.mark.parametrize('line', ['单位：元日期：2024年', '单位：元文字', '单位：元币种人民币'])
def test_undeclared_adjacent_text_is_not_a_unit(line):
    assert _UNIT.search(line) is None
