"""An unmarked next table may close a continuation, never create a role."""
from dataclasses import FrozenInstanceError
from datetime import date
from types import SimpleNamespace
import re

import pdfplumber
import pytest

from aquant.adapters.providers import cninfo_statement_sections as sections
from aquant.adapters.providers.cninfo_financial_candidate import _Heading
from aquant.adapters.providers.cninfo_financial_pdf import CninfoFinancialPdfError

ISSUER = '渤海租赁股份有限公司'
PERIOD = date(2025, 6, 30)
NEXT = '\n'.join([ISSUER, '资产负债表', '2025年6月30日 人民币千元',
                  '资产 附注十五 2025年6月30日 2024年12月31日',
                  '货币资金 46 39'])
START = _Heading('合并', '现金流量表', 0, 50, 60, '千元')
CONT = _Heading('合并', '现金流量表', 1, 50, 60, '千元', True)


class Page:
    height, width = 800, 595

    def __init__(self, text, positions=None):
        self.text = text
        self.positions = positions

    def extract_text(self):
        return self.text

    def search(self, pattern):
        return self.positions if self.positions is not None else [
            {'top': 50, 'bottom': 60} for line in self.text.splitlines()
            if re.search(pattern, line)]

    def crop(self, box):
        if box[3] < 100:
            return Page(ISSUER)
        if box[1] > 0:
            return Page('\n'.join(self.text.splitlines()[2:]))
        return self


def document(next_text=NEXT, positions=None):
    return SimpleNamespace(pages=[Page(ISSUER + '\n合并现金流量表'),
                                 Page(ISSUER + '\n合并现金流量表（续）'),
                                 Page(next_text, positions)])


def test_exact_source_identity_date_unit_columns_supply_terminator_only():
    end = sections._unscoped_statement_end(document(), START, CONT, PERIOD)
    assert (end.page_index, end.top) == (2, 50)
    assert not hasattr(end, 'role') and not hasattr(end, 'statement')
    with pytest.raises(FrozenInstanceError):
        end.top = 999


@pytest.mark.parametrize('text', [
    NEXT.replace(ISSUER, '其他公司股份有限公司'),
    NEXT.replace(ISSUER + '\n', ''),
    NEXT.replace('资产负债表', '现金流量表'),
    NEXT.replace('资产负债表', '利润表').replace(
        '资产 附注十五 2025年6月30日 2024年12月31日',
        '项目 附注十五 2023年6月30日 2022年6月30日'),
    NEXT.replace('资产负债表', '资产负债表有关说明'),
    NEXT.replace('资产负债表', '财务状况表'),
    NEXT.replace(ISSUER, '财务报表附注\n' + ISSUER),
    NEXT.replace(ISSUER, '会计政策说明\n' + ISSUER),
    NEXT.replace('资产负债表\n', '资产负债表\n会计政策说明\n'),
    NEXT.replace('资产负债表\n', '资产负债表\n以下为报表模板示例，不代表本公司实际报表\n'),
    NEXT.replace('2025年6月30日 人民币千元', '2024年6月30日 人民币千元'),
    NEXT.replace('2025年6月30日 人民币千元', '2025年6月30日'),
    NEXT.replace('人民币千元', '千元'),
    NEXT.replace('人民币千元', '人民币元'),
    NEXT.replace('人民币千元', '人民币千元 币种：美元'),
    NEXT.replace('人民币千元', '人民币千元 单位：人民币元'),
    NEXT.replace('资产 附注十五 2025年6月30日 2024年12月31日', '资产 附注十五 2025年6月30日'),
    NEXT.replace('资产 附注十五 2025年6月30日 2024年12月31日', '项目 附注十五 2024年12月31日 2025年6月30日'),
    NEXT.replace('2024年12月31日', '2025年6月30日'),
    NEXT.replace('2024年12月31日', '2024年13月31日'),
    NEXT.replace('资产 附注十五', '叙述两个日期'),
    NEXT.replace('资产负债表\n', '资产负债表\n利润表\n'),
    NEXT.replace('货币资金 46 39', '项目 附注十五 2025年6月30日 2024年12月31日'),
])
def test_unknown_notes_issuer_currency_period_and_columns_remain_closed(text):
    assert sections._unscoped_statement_end(document(text), START, CONT, PERIOD) is None


@pytest.mark.parametrize('positions', [[], [{'top': 50}, {'top': 90}], [{'top': 250}]])
def test_ambiguous_or_nonheader_title_position_is_not_a_boundary(positions):
    assert sections._unscoped_statement_end(document(positions=positions), START, CONT, PERIOD) is None


def test_missing_adjacent_next_page_cannot_guess_an_end():
    doc = document()
    doc.pages.pop()
    assert sections._unscoped_statement_end(doc, START, CONT, PERIOD) is None


@pytest.mark.parametrize('role,statement', [('母公司', '现金流量表'), ('合并', '利润表')])
def test_only_actual_consolidated_cash_to_unmarked_balance_combination_is_supported(role, statement):
    start = _Heading(role, statement, 0, 50, 60, '千元')
    assert sections._unscoped_statement_end(document(), start, CONT, PERIOD) is None


def install_extract(monkeypatch, next_text=NEXT, known_end=False):
    first = '\n'.join([ISSUER, '合并现金流量表', '2025年6月30日 人民币千元',
        '附注五 2025年1-6月 2024年1-6月',
        '经营活动产生的现金流量净额 11,177,978 11,717,371',
        '投资活动产生的现金流量净额 (2,195,803) (12,376,541)'])
    continuation = '\n'.join([ISSUER, '合并现金流量表（续）',
        '2025年6月30日 人民币千元', '附注五 2025年1-6月 2024年1-6月',
        '筹资活动产生的现金流量净额 (28,560,020) (1,628,823)',
        '四、汇率变动对现金及现金等价物的影响 7,936 32,764',
        '五、现金及现金等价物净减少额 (19,569,909) (2,255,229)'])

    class Document:
        pages = [Page(first), Page(continuation), Page(next_text)]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    headings = (START, CONT, _Heading('母公司', '资产负债表', 2, 50, 60, '千元')) if known_end else (START, CONT)
    monkeypatch.setattr(pdfplumber, 'open', lambda _: Document())
    monkeypatch.setattr(sections, '_pdf_statement_headings', lambda _: headings)


def test_actual_continuation_components_survive_and_unknown_table_is_not_extracted(monkeypatch):
    install_extract(monkeypatch)
    result = sections.extract_statement_sections(b'%PDF-fixture', period_end=PERIOD,
                                                 statements=('现金流量表',))
    table = result.require('合并', '现金流量表')
    assert len(result.sections) == 1 and table.end_page == 3
    assert result.get('母公司', '资产负债表') is None
    assert result.get('合并', '资产负债表') is None
    components = {key: table.component(key).amount for key in (
        'operating_cashflow', 'investing_cashflow', 'financing_cashflow',
        'fx_effect', 'net_increase_cash')}
    assert components['net_increase_cash'] - components['investing_cashflow'] - components['financing_cashflow'] - components['fx_effect'] == components['operating_cashflow']


def test_unproven_boundary_does_not_change_legacy_parse_or_admit_continuation_cells(monkeypatch):
    install_extract(monkeypatch, NEXT.replace('人民币千元', '千元'))
    result = sections.extract_statement_sections(b'%PDF-fixture', period_end=PERIOD,
                                                 statements=('现金流量表',))
    table = result.require('合并', '现金流量表')
    assert table.end_page == 2
    assert table.component('operating_cashflow') is not None
    assert table.component('financing_cashflow') is None
    assert table.component('net_increase_cash') is None
    assert result.get('母公司', '资产负债表') is None


def test_existing_role_bearing_end_never_invokes_unscoped_fallback(monkeypatch):
    install_extract(monkeypatch, known_end=True)
    def no_fallback(*args):
        raise AssertionError('existing explicit boundary must be unchanged')
    monkeypatch.setattr(sections, '_unscoped_statement_end', no_fallback)
    result = sections.extract_statement_sections(b'%PDF-fixture', period_end=PERIOD,
                                                 statements=('现金流量表',))
    assert result.require('合并', '现金流量表').end_page == 3
