from decimal import Decimal
from aquant.adapters.providers.s2_machine_verification import _revenue_identity


def _row(page, cell):
    return {"page":page,"current_cell":cell,"amount_yuan":cell,"role":"合并"}


def _check(evidence, value="1000", interest=None):
    totals={"total_revenue":Decimal("1000"),"interest_income":interest,
            "earned_premium":None,"fee_commission_income":None}
    return _revenue_identity(totals=totals,value=Decimal(value),parent_row=None,evidence=evidence)


def test_single_total_revenue_row_cannot_claim_consumer_independence():
    assert not _check({"total_revenue":_row(10,"1000"),"interest_income":None,
                       "earned_premium":None,"fee_commission_income":None,"parent_revenue":None}).holds


def test_two_claims_of_same_source_cell_remain_one_path():
    assert not _check({"total_revenue":_row(10,"1000"),"parent_revenue":_row(10,"1000")}).holds


def test_explicit_independent_component_and_exact_amount_pass():
    assert _check({"total_revenue":_row(10,"1000"),"interest_income":_row(10,"10")},
                  value="990",interest=Decimal("10")).holds
    assert _check({"total_revenue":_row(10,"1000"),"interest_income":_row(10,"0")},
                  interest=Decimal("0")).holds


def test_missing_component_cannot_make_near_equal_amount_true():
    assert not _check({"total_revenue":_row(10,"1000"),"parent_revenue":_row(11,"900")},value="996").holds
