from types import SimpleNamespace
import pytest
from aquant.adapters.providers.cninfo_financial_candidate import (
    _page_headings, CninfoFinancialPdfError,
)


def test_explicit_three_adjustment_columns_are_not_current_statement():
    for separator in ("", "负债：\n"):
        text="上述变更对2023年合并资产负债表的影响\n合并资产负债表\n"+separator+"调整前 调整金额 调整后\n短期借款 10 1 11"
        def forbidden(*args): raise AssertionError("Historical adjustment heading must not be searched as a primary statement")
        assert _page_headings(SimpleNamespace(extract_text=lambda:text,search=forbidden),0)==[]


def test_real_current_statement_ambiguity_still_fails():
    page=SimpleNamespace(extract_text=lambda:"合并利润表\n单位：人民币 元\n项目 2024年半年度 2023年半年度",
        search=lambda _:[{"top":1,"bottom":2},{"top":3,"bottom":4}])
    with pytest.raises(CninfoFinancialPdfError,match="ambiguous statement heading position"):
        _page_headings(page,0)


def test_unlabelled_adjustment_word_does_not_skip_actual_statement():
    page=SimpleNamespace(extract_text=lambda:"合并利润表\n单位：人民币 元\n项目 2024年半年度 2023年半年度\n会计调整说明",
        search=lambda _:[{"top":1,"bottom":2}])
    headings=_page_headings(page,0)
    assert len(headings)==1 and headings[0].statement=="利润表" and headings[0].unit=="元"
