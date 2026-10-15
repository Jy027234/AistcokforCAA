"""行名归一化与金额词法的针对性用例。

这个区域反复出过真实缺陷，因此把**实测原文**逐条固定下来。每一行都来自本机
已归档的官方 PDF（证券代码写在用例名里），不是构造出来的理想字符串。

背景：财务报表的行名里会夹着报表附注引用、行首编号、全角冒号、换行碎片，
而 ``_field_matches`` 只认干净的语义行名。归一化漏掉任何一种，对应的字段就
"定位不到"，表现为整份报告无法核验——方向是拒绝正确数据，比放行更容易被忽略。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aquant.adapters.providers.cninfo_financial_candidate import _field_matches
from aquant.adapters.providers.cninfo_statement_sections import (
    _amounts_in, _is_note_reference, _normalize_label, _row_amounts,
)

#: (原文行, 期望字段, 期望当期金额)
REAL_LINES = [
    # 600519：附注编号前有空格，且不带章节号。
    ("其中：营业收入 41 89,389,354,416.84 81,930,977,667.75", "revenue",
     "89389354416.84"),
    # 000651 年报无边框版式：附注写成「五、54」，行名后还有列头残留。
    ("其中：营业收入 五、54 189,163,654,064.64 203,979,266,387.09", "revenue",
     "189163654064.64"),
    # 002415：附注写成「(五)46」，收入是利润表首行。
    ("一、营业收入 (五)46 41,209,096,206.36 37,571,352,435.63", "revenue",
     "41209096206.36"),
    # 601012：行名在提示符之后紧跟附注。
    ("一、营业收入 七、61 82,582,273,118.72 129,497,674,192.20", "revenue",
     "82582273118.72"),
    # 688981：千元单位，负数用括号表示。
    ("其中：营业收入 七、61 38,635,115 32,348,049", "revenue", "38635115"),
    # 归母净利润被换行拆开后的续行。
    ("1、归属于母公司股东的净利润（净亏损以“-”号填 32,184,570,372.28 "
     "29,017,387,604.18", "net_profit_attributable", "32184570372.28"),
    # 全角减号形式的净利润行。
    ("五、净利润（净亏损以“－”号填列） 46,986,681,449.24 43,176,914,345.12",
     "net_profit_consolidated", "46986681449.24"),
    # 少数股东损益：行首编号 + 括号负数。
    ("2.少数股东损益（净亏损以“—”号填列） -248,410,050.57 -323,116,590.28",
     None, "-248410050.57"),
    # 经营现金流。
    ("经营活动产生的现金流量净额 5,122,166,411.40 30,859,691,934.60",
     "operating_cashflow", "5122166411.40"),
    # 母公司表节里被右括号续行接起来的归母权益。
    ("归属于母公司所有者权益（或股东权益）合计 238,646,536,390.48 "
     "233,105,984,399.47", "parent_equity", "238646536390.48"),
    # Actual 000415 H125: a real space before the negative cell's closing glyph.
    ("归属于母公司股东的净利润 (2,018,780 ) 716,400", "net_profit_attributable", "-2018780"),
    # Actual 000100 H125 uses 归属 rather than 归属于; same stated parent scope.
    ("归属母公司股东权益合计 53,595,106 53,167,609", "parent_equity", "53595106"),
]


@pytest.mark.parametrize("line,field,amount", REAL_LINES)
def test_real_ocr_lines_parse_and_match(line: str, field: str | None,
                                        amount: str) -> None:
    parsed = _row_amounts(line)
    assert parsed is not None, f"未解析出金额: {line!r}"
    label, cell, value, _ = parsed
    assert value == Decimal(amount), f"当期金额不符: {line!r}"
    assert _amounts_in(line)[0][1] == cell
    if field is not None:
        assert _field_matches(field, _normalize_label(label)), (
            f"{field} 未匹配，归一化后为 {_normalize_label(label)!r}")


@pytest.mark.parametrize("line,field,amount", REAL_LINES)
def test_normalised_label_never_ends_in_a_bare_digit(
        line: str, field: str | None, amount: str) -> None:
    """行名不会以裸数字结尾；残留数字只可能来自被误当金额的附注编号。"""

    import re

    parsed = _row_amounts(line)
    assert parsed is not None
    normalised = _normalize_label(parsed[0])
    assert re.search(r"\d\Z", normalised) is None, (
        f"归一化后仍以数字结尾: {normalised!r}")


def test_revenue_accepts_both_revenue_row_spellings_but_not_total_revenue() -> None:
    """``营业总收入``必须继续被拒绝：它与营业收入在有利息收入的公司并不相等。"""

    for label in ("营业收入", "其中:营业收入", "其中：营业收入"):
        assert _field_matches("revenue", label)
    assert not _field_matches("revenue", "营业总收入")
    assert not _field_matches("revenue", "其中:营业总收入")


def test_parent_equity_alias_cannot_be_a_minority_or_narrative_label():
    from aquant.adapters.providers.cninfo_statement_geometry import _field_label
    assert _field_label('parent_equity','归属母公司股东权益合计')
    assert not _field_label('parent_equity','少数股东权益合计')
    assert not _field_label('parent_equity','说明归属母公司股东权益合计')
    assert _normalize_label('说明归属母公司股东权益合计')=='说明归属母公司股东权益合计'


def test_note_reference_detection_covers_all_observed_forms() -> None:
    """三种实测附注形态都要被识别为"不是金额"。"""

    def _is_note(line: str, token: str) -> bool:
        match = next(m for m in _amounts_in.__globals__["_NUMBER_TOKEN"].finditer(line)
                     if m.group() == token)
        return _is_note_reference(line, match)

    assert _is_note("其中：营业收入 41 89,389,354,416.84", "41")
    assert not _is_note("其中：营业收入 4,412,132,088.76 3,000,000.00", "4,412,132,088.76")


def test_plain_amounts_are_never_treated_as_note_references() -> None:
    """真正的金额列不能被当成附注编号丢掉（688981 曾因此整列失配）。"""

    line = "其中：营业收入 4,412,132,088.76 3,900,000,000.00"
    found = _amounts_in(line)
    assert [value for _, _, value in found] == [
        Decimal("4412132088.76"), Decimal("3900000000.00")]


def test_row_without_two_amounts_is_not_a_row() -> None:
    assert _row_amounts("利息收入 - -") is None
    assert _row_amounts("流动资产：") is None
    assert _row_amounts("") is None
