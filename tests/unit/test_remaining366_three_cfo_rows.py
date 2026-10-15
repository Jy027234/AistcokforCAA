"""Two additional literal splits, without broad prefix or empty-cell recovery."""
from dataclasses import replace
from decimal import Decimal

import pytest

from aquant.adapters.providers.cninfo_statement_sections import StatementSection


def section(head, amounts, tail, **changes):
    lines=((120, head), (120, amounts), (120, tail))
    original=StatementSection(role="合并", statement="现金流量表", start_page=120,
        end_page=122, unit="元", current_header="2025年度", prior_header="2024年度",
        rows=(), lines=lines)
    return replace(original, **changes)


@pytest.mark.parametrize("head,middle,tail,current,prior", [
    ("经营活动产生的现金流量", "七、79 6,174,239,867.75 5,789,408,508.54",
     "净额", "6174239867.75", "5789408508.54"),
    ("经营活动产生的现金流", "七、78 3,802,916,896.40 2,907,217,929.40",
     "量净额", "3802916896.40", "2907217929.40"),
    ("经营活动产生的现金流", "七、78 2,907,217,929.40 2,881,736,311.41",
     "量净额", "2907217929.40", "2881736311.41"),
])
def test_source_proven_exact_splits_keep_both_original_cells_and_three_lines(
        head,middle,tail,current,prior):
    report=section(head,middle,tail)
    row=report.find("operating_cashflow")
    assert row is not None and row.amount==Decimal(current)
    assert row.prior_amount==Decimal(prior)
    assert row.raw_line=="\n".join((head,middle,tail))
    assert report.component("operating_cashflow")==row


@pytest.mark.parametrize("head,tail", [
    ("经营活动产生的现金流", "净额"),
    ("经营活动产生的现金流量", "量净额"),
    ("经营活动产生的现金流量净", "额"),
    ("经营活动产生的现金流量说明", "净额"),
    ("投资活动产生的现金流量", "净额"),
    ("经营活动使用的现金流量", "净额"),
])
def test_only_the_observed_whole_cfo_label_pairs_are_supported(head,tail):
    assert section(head,"七、79 6,174,239,867.75 5,789,408,508.54",tail).find(
        "operating_cashflow") is None


@pytest.mark.parametrize("changes", [
    {"role":"母公司"}, {"statement":"利润表"}, {"combined":True},
    {"scope_columns":4},
    {"lines":((120,"经营活动产生的现金流量"),
              (121,"七、79 6,174,239,867.75 5,789,408,508.54"),(121,"净额"))},
    {"lines":((120,"经营活动产生的现金流量"),
              (120,"七、79 6,174,239,867.75 5,789,408,508.54"),
              (120,"说明"),(120,"净额"))},
])
def test_existing_scope_page_and_adjacency_boundaries_also_apply_to_new_pairs(changes):
    assert section("经营活动产生的现金流量",
        "七、79 6,174,239,867.75 5,789,408,508.54","净额",**changes).find(
            "operating_cashflow") is None


@pytest.mark.parametrize("middle", [
    "七、79 6,174,239,867.75", "七、79 6,174,239,867.75 -",
    "七、79 - 5,789,408,508.54", "79 6,174,239,867.75 5,789,408,508.54",
    "七、79 6,174,239,867.75 说明 5,789,408,508.54",
])
def test_new_pairs_do_not_guess_missing_or_nonadjacent_amount_cells(middle):
    assert section("经营活动产生的现金流量",middle,"净额").find(
        "operating_cashflow") is None
