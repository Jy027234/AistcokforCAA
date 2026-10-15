"""Actual reports print explicit RMB unit without the word 单位."""
import pytest
from aquant.adapters.providers import cninfo_financial_candidate as primary
from aquant.adapters.providers import cninfo_statement_geometry as independent


@pytest.mark.parametrize('source,unit,scale',[
    ('人民币元','元',1),
    ('人民币千元','千元',1000),
    ('2025年6月30日 人民币千元','千元',1000),
    ('人民币万元','万元',10000),
    ('人民币百万元','百万元',1000000),
])
def test_explicit_standalone_unit_has_identical_exact_scale(source,unit,scale):
    main=primary._UNIT.search(source);grid=independent._UNIT.search(source)
    assert main is not None and main[1]=='人民币' and main[2]==unit
    assert grid is not None and grid[1]==unit
    assert primary._UNIT_MULTIPLIER[unit]==independent._SCALE[unit]==scale


@pytest.mark.parametrize('source', ['千元','美元千元','人民币元化','人民币金额', '人民币百万元化'])
def test_no_currency_or_nonunit_words_do_not_supply_a_default(source):
    assert primary._UNIT.search(source) is None
    assert independent._UNIT.search(source) is None
