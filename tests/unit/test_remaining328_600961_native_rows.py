"""Literal native rows: both periods and decimal fragments retain their columns."""
from decimal import Decimal
from dataclasses import replace

import pytest

from aquant.adapters.providers.cninfo_financial_candidate import CninfoFinancialPdfError
from aquant.adapters.providers.cninfo_statement_sections import (
    StatementRow, _merge_native_literal_rows, _native_literal_section_rows,
)


class Page:
    def __init__(self, words=()):
        self.words = list(words)

    def extract_words(self):
        return self.words


def word(text, x0, x1, top):
    return dict(text=text, x0=x0, x1=x1, top=top, bottom=top + 12)


def recover(lines, words=(), **changes):
    settings = dict(page_number=102, role="合并", statement="利润表", unit="元",
                    header=("2025年度", "2024年度"), combined=False, scope_columns=2)
    settings.update(changes)
    return _native_literal_section_rows(Page(words), list(lines), **settings)


NPC = ('五、净利润（净亏损以“－”号 1,125,504,406. 801,930,593.06', '填列） 09')
NPC_WORDS = (
    word('五、净利润（净亏损以“－”号', 90, 254, 100),
    word('1,125,504,406.', 346, 430, 100), word('801,930,593.06', 447, 531, 100),
    word('填列）', 90, 126, 115.6), word('09', 418, 430, 115.6),
)


def test_complete_prior_cell_is_never_substituted_for_split_current_profit():
    row, = recover(NPC, NPC_WORDS)
    assert row.amount == Decimal('1125504406.09')
    assert row.prior_amount == Decimal('801930593.06')
    assert row.current_cell == '1,125,504,406.09'
    assert row.prior_cell == '801,930,593.06'
    assert row.raw_line == '\n'.join(NPC)


REVENUE = ('22,246,423,224 19,759,215,060.', '其中：营业收入', '.05 69')
REVENUE_WORDS = (
    word('22,246,423,224', 346, 430, 100), word('19,759,215,060.', 447, 531, 100),
    word('其中：营业收入', 90, 174, 107.8),
    word('.05', 412, 430, 115.6), word('69', 519, 531, 115.6),
)


def test_both_revenue_decimal_tails_are_native_column_fragments_not_values():
    row, = recover(REVENUE, REVENUE_WORDS)
    assert row.amount == Decimal('22246423224.05')
    assert row.prior_amount == Decimal('19759215060.69')
    assert row.raw_line == '\n'.join(REVENUE)


@pytest.mark.parametrize('mutation', ['missing_tail', 'wrong_x', 'wrong_y', 'duplicate_tail',
                                     'duplicate_anchor', 'swapped_columns', 'other_label_overlap'])
def test_split_decimal_geometry_must_be_unique_aligned_and_ordered(mutation):
    words = [dict(w) for w in NPC_WORDS]
    if mutation == 'missing_tail': words.pop()
    elif mutation == 'wrong_x': words[-1]['x1'] += 10
    elif mutation == 'wrong_y': words[-1]['top'] += 15
    elif mutation == 'duplicate_tail': words.append(dict(words[-1]))
    elif mutation == 'duplicate_anchor': words.append(dict(words[0]))
    elif mutation == 'swapped_columns': words[1]['x1'] = 540
    elif mutation == 'other_label_overlap': words[0]['x1'] = 350
    with pytest.raises(CninfoFinancialPdfError): recover(NPC, words)


@pytest.mark.parametrize('tail', ['填列） 9', '填列） -', '填列） 09 02', '说明 09'])
def test_incomplete_current_tail_cannot_fall_back_to_complete_prior(tail):
    with pytest.raises(CninfoFinancialPdfError): recover((NPC[0], tail), NPC_WORDS)


def test_attributable_profit_preserves_two_line_label_and_decimal_fraction():
    lines = ('1.归属于母公司股东的净 1,125,228,980. 786,535,418.67',
             '利润（净亏损以“-”号填列） 31')
    words = (word('1.归属于母公司股东的净', 114, 246, 100),
             word('1,125,228,980.', 346, 430, 100), word('786,535,418.67', 447, 531, 100),
             word('利润（净亏损以“-”号填列）', 90, 252, 115.6), word('31', 418, 430, 115.6))
    row, = recover(lines, words)
    assert (row.amount, row.prior_amount) == (Decimal('1125228980.31'), Decimal('786535418.67'))
    assert row.raw_line == '\n'.join(lines)


def test_three_native_label_lines_do_not_change_the_two_complete_amount_cells():
    lines = ('1.归属于母公司股东的 1,948,072,940.84 585,449,810.33',
             '净利润（净亏损以“-”号', '填列）')
    row, = recover(lines)
    assert row.amount == Decimal('1948072940.84')
    assert row.prior_amount == Decimal('585449810.33')
    assert row.raw_line == '\n'.join(lines)
    assert recover((lines[0], '说明', *lines[1:])) == ()


@pytest.mark.parametrize('stem,suffix', [('经营活动产生的现金', '流量净额'),
                                        ('经营活动产生的现', '金流量净额')])
def test_cashflow_reads_raw_adjacent_lines_before_subtotal_suffix_glue(stem, suffix):
    lines = ('经营活动现金流出小 13,118,538,507.21 10,526,323,076.18', '计',
             stem + ' 2,272,112,198.46 1,088,756,177.91', suffix)
    row, = recover(lines, statement='现金流量表')
    assert row.amount == Decimal('2272112198.46')
    assert row.prior_amount == Decimal('1088756177.91')
    assert row.raw_line == '\n'.join(lines[2:])


@pytest.mark.parametrize('amounts', ['100.00', '- 200.00', '100.00 200.00 300.00',
                                    '100.00 说明 200.00'])
def test_raw_cashflow_does_not_guess_or_fill_missing_cells(amounts):
    assert recover(('经营活动产生的现金 ' + amounts, '流量净额'), statement='现金流量表') == ()


@pytest.mark.parametrize('suffix', ['流量净额说明', '金流量净额', '流量净额 100.00'])
def test_cashflow_split_pair_is_literal(suffix):
    assert recover(('经营活动产生的现金 100.00 200.00', suffix), statement='现金流量表') == ()


@pytest.mark.parametrize('minority', ['134,126,450.69 132,957,391.86', '100.00 200.00',
                                    '999,999,999,999.99 999,999,999,999.99'])
def test_parent_equity_word_split_never_uses_following_minority_cells(minority):
    lines = ('归属于母公司所有者权 5,860,454,961.43 4,714,167,079.85',
             '益（或股东权益）合计', '少数股东权益 ' + minority)
    row, = recover(lines, statement='资产负债表')
    assert row.amount == Decimal('5860454961.43')
    assert row.prior_amount == Decimal('4714167079.85')
    assert row.raw_line == '\n'.join(lines)


@pytest.mark.parametrize('changes', [{'role': '母公司'}, {'combined': True}, {'scope_columns': 4},
                                    {'unit': '美元'}, {'header': None}, {'header': ('2025年度', '')}])
def test_native_recovery_requires_existing_consolidated_two_period_cny_scope(changes):
    assert recover(NPC, NPC_WORDS, **changes) == ()


def test_duplicate_literal_rows_cannot_create_an_arbitrary_first_match():
    lines = ('经营活动产生的现 100.00 200.00', '金流量净额')
    with pytest.raises(CninfoFinancialPdfError): recover(lines + lines, statement='现金流量表')


def test_successful_direct_row_keeps_its_original_object_and_canonical_evidence():
    native, = recover(('经营活动产生的现金 100.00 200.00', '流量净额'), statement='现金流量表')
    old = StatementRow(role='合并', statement='现金流量表', label='经营活动产生的现金流量净额',
        amount=Decimal('100.00'), prior_amount=Decimal('200.00'), page=102, unit='元',
        current_cell='100.00', prior_cell='200.00', raw_line='original direct citation')
    assert _merge_native_literal_rows([old], (native,), '现金流量表') == [old]
    assert _merge_native_literal_rows([old], (native,), '现金流量表')[0] is old


def test_only_validated_decimal_fragment_repair_replaces_the_known_wrong_direct_value():
    native, = recover(NPC, NPC_WORDS)
    old = StatementRow(role='合并', statement='利润表', label='五、净利润（净亏损以“－”号 1,125,504,406.',
        amount=Decimal('801930593.06'), prior_amount=Decimal(0), page=102, unit='元',
        current_cell='801,930,593.06', prior_cell='not-on-line', raw_line=NPC[0])
    assert _merge_native_literal_rows([old], (native,), '利润表') == [native]


def test_decimal_recovery_cannot_replace_a_separate_successful_direct_observation():
    native, = recover(NPC, NPC_WORDS)
    old = StatementRow(role='合并', statement='利润表', label='五、净利润（净亏损以“－”号填列）',
        amount=Decimal('500.00'), prior_amount=Decimal('400.00'), page=102, unit='元',
        current_cell='500.00', prior_cell='400.00', raw_line='五、净利润（净亏损以“－”号填列） 500.00 400.00')
    assert _merge_native_literal_rows([old], (native,), '利润表')[0] is old


@pytest.mark.parametrize('changes', [{'raw_line': 'different source line'}, {'page': 103},
    {'current_cell': '500.00', 'amount': Decimal('500.00')}, {'prior_cell': '400.00'},
    {'unit': '千元'}, {'nil_amounts': True}, {'label': '五、净利润完整不同标签'}])
def test_decimal_replacement_requires_the_exact_same_source_head_and_proven_prior_misread(changes):
    native, = recover(NPC, NPC_WORDS)
    old = StatementRow(role='合并', statement='利润表', label='五、净利润（净亏损以“－”号 1,125,504,406.',
        amount=Decimal('801930593.06'), prior_amount=Decimal(0), page=102, unit='元',
        current_cell='801,930,593.06', prior_cell='not-on-line', raw_line=NPC[0])
    old = replace(old, **changes)
    assert _merge_native_literal_rows([old], (native,), '利润表')[0] is old


def test_known_wrong_head_plus_another_direct_row_is_an_explicit_conflict():
    native, = recover(NPC, NPC_WORDS)
    wrong = StatementRow(role='合并', statement='利润表', label='五、净利润（净亏损以“－”号 1,125,504,406.',
        amount=Decimal('801930593.06'), prior_amount=Decimal(0), page=102, unit='元',
        current_cell='801,930,593.06', prior_cell='not-on-line', raw_line=NPC[0])
    other = replace(wrong, label='五、净利润（净亏损以“－”号填列）', current_cell='500.00',
                    amount=Decimal('500.00'), prior_cell='400.00', raw_line='another complete source row')
    with pytest.raises(CninfoFinancialPdfError):
        _merge_native_literal_rows([wrong, other], (native,), '利润表')
