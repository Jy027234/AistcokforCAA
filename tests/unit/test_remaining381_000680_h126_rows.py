"""Two exact 000680 source labels; amounts, scope and raw labels stay explicit."""
from decimal import Decimal

import pytest

from aquant.adapters.providers.cninfo_statement_sections import (
    ROLE_CONSOLIDATED, ROLE_PARENT, StatementRow, StatementSection,
)


def section(statement, labels, role=ROLE_CONSOLIDATED):
    rows = tuple(StatementRow(role=role, statement=statement, label=label,
        amount=Decimal('1234.56'), prior_amount=Decimal('987.65'), page=182,
        unit='元', current_cell='1,234.56', prior_cell='987.65',
        raw_line=label + ' 1,234.56 987.65') for label in labels)
    return StatementSection(role=role, statement=statement, start_page=182,
        end_page=183, unit='元', current_header='期末余额', prior_header='期初余额', rows=rows)


@pytest.mark.parametrize('label', ['其中：营业收入 五）49', '其中:营业收入 五)49',
                                  '一、营业收入 十七）4'])
def test_exact_revenue_note_suffix_preserves_original_cell_and_label(label):
    report = section('利润表', [label])
    row = report.find('revenue')
    assert row is report.rows[0]
    assert row.label == label and row.raw_line.startswith(label)
    assert row.current_cell == '1,234.56' and row.amount == Decimal('1234.56')


@pytest.mark.parametrize('label', ['营业总收入 五）49', '营业收入说明 五）49',
    '营业收入 五）', '营业收入 五', '营业收入 五）49不适用',
    '营业收入五）49', '营业收入 五）0', '营业收入 五）1234',
    '营业收入 第五）49', '其中：营业收入 五）49 上期发生额'])
def test_note_suffix_does_not_convert_other_labels_or_incomplete_references(label):
    assert section('利润表', [label]).find('revenue') is None


def test_truncated_equity_source_label_is_not_filled_or_rewritten():
    report = section('资产负债表', ['归属于母公司所有者权益合'])
    row = report.find('parent_equity')
    assert row is report.rows[0]
    assert row.label == '归属于母公司所有者权益合'
    assert '合计' not in row.raw_line
    assert row.amount == Decimal('1234.56')


def test_truncated_equity_alias_does_not_select_between_duplicate_rows():
    assert section('资产负债表', ['归属于母公司所有者权益合'] * 2).find('parent_equity') is None


@pytest.mark.parametrize('label', ['归属于母公司所有者权益', '归属于母公司所有者权益和',
                                  '归属于母公司所有者权益合说明', '少数股东权益合'])
def test_other_truncations_and_other_equity_are_not_guessed(label):
    assert section('资产负债表', [label]).find('parent_equity') is None


@pytest.mark.parametrize('role,statement', [(ROLE_PARENT, '资产负债表'),
                                          (ROLE_CONSOLIDATED, '利润表')])
def test_equity_alias_requires_actual_consolidated_balance_scope(role, statement):
    assert section(statement, ['归属于母公司所有者权益合'], role=role).find('parent_equity') is None


def test_existing_complete_equity_label_and_note_normalization_are_unchanged():
    complete = section('资产负债表', ['归属于母公司所有者权益合计'])
    assert complete.find('parent_equity') is complete.rows[0]
    income = section('利润表', ['其中：营业收入 五、49'])
    assert income.find('revenue') is income.rows[0]
    assert income.normalize('本期发生额') == '本期发生额'


def test_closed_note_alone_cannot_manufacture_a_candidate_without_a_source_amount():
    from aquant.adapters.providers.cninfo_statement_sections import _row_amounts
    assert _row_amounts('其中：营业收入 五）49') is None
