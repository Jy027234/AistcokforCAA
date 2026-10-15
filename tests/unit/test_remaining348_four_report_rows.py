"""Closed native layout recoveries; independently verified amounts stay separate."""
from dataclasses import replace
from decimal import Decimal

import pytest

from aquant.adapters.providers.cninfo_statement_sections import (
    StatementRow, StatementSection, _literal_continuation_report_header,
)


HEADER = "上海锦江航运(集团)股份有限公司2025年半年度报告"


def section(original_lines, **changes):
    base = StatementSection(role="合并", statement="现金流量表", start_page=51,
        end_page=53, unit="元", current_header="2025年半年度", prior_header="2024年半年度",
        rows=(), lines=tuple(original_lines))
    return replace(base, **changes)


@pytest.mark.parametrize("raw,current,prior", [
    ("经营活动产生的现金流 1,088,756,177.91 739,465,663.03", "1088756177.91", "739465663.03"),
    ("经营活动产生的现金流 1,894,001,180.60 367,757,571.73", "1894001180.60", "367757571.73"),
])
def test_two_literal_adjacent_cfo_lines_preserve_both_native_cells(raw, current, prior):
    original = section(((51, raw), (51, "量净额")))
    row = original.find("operating_cashflow")
    assert row.amount == Decimal(current) and row.prior_amount == Decimal(prior)
    assert row.raw_line == raw + "\n量净额"
    assert original.component("operating_cashflow") == row


@pytest.mark.parametrize("lines", [
    ((51, "经营活动产生的现金流 100.00"), (51, "量净额")),
    ((51, "经营活动产生的现金流 100.00 -"), (51, "量净额")),
    ((51, "经营活动产生的现金流 100.00 200.00 300.00"), (51, "量净额")),
    ((51, "经营活动产生的现金流 100.00 说明 200.00"), (51, "量净额")),
    ((51, "投资活动产生的现金流 100.00 200.00"), (51, "量净额")),
    ((51, "经营活动产生的现金流 100.00 200.00"), (51, "量净额说明")),
    ((51, "经营活动产生的现金流 100.00 200.00"), (51, "说明"), (51, "量净额")),
    ((51, "经营活动产生的现金流 100.00 200.00"), (52, "量净额")),
])
def test_literal_cfo_does_not_guess_missing_cells_or_skip_body(lines):
    assert section(lines).find("operating_cashflow") is None


@pytest.mark.parametrize("changes", [
    {"role": "母公司"}, {"statement": "利润表"}, {"combined": True},
    {"scope_columns": 4}, {"unit": "美元"}, {"current_header": ""}, {"prior_header": ""},
])
def test_closed_layouts_are_only_in_already_validated_consolidated_two_column_cf(changes):
    assert section(((51, "经营活动产生的现金流 100.00 200.00"),
                    (51, "量净额")), **changes).find("operating_cashflow") is None


def cross_lines():
    return ((51, "经营活动产生的现金流 1,256,890,943.92 755,131,959.05"),
            (51, "51/175"), (52, HEADER), (52, "量净额"))


def test_cross_page_needs_real_counter_and_exact_same_report_cover_header():
    original = section(cross_lines(), pdf_page_count=175, continuation_report_header=HEADER)
    row = original.find("operating_cashflow")
    assert row.amount == Decimal("1256890943.92")
    assert row.prior_amount == Decimal("755131959.05")
    assert row.page == 51 and row.raw_line == "\n".join(line for _, line in cross_lines())


@pytest.mark.parametrize("changes", [
    {"pdf_page_count": 176}, {"pdf_page_count": 0}, {"continuation_report_header": ""},
    {"continuation_report_header": "另一家公司2025年半年度报告"},
    {"end_page": 51}, {"start_page": 52},
    {"lines": (cross_lines()[0], (51, "51/176"), *cross_lines()[2:])},
    {"lines": (cross_lines()[0], (51, "50/175"), *cross_lines()[2:])},
    {"lines": (cross_lines()[0], (51, "51/175说明"), *cross_lines()[2:])},
    {"lines": (cross_lines()[0], cross_lines()[1], (52, "另一家公司2025年半年度报告"), cross_lines()[3])},
    {"lines": (cross_lines()[0], cross_lines()[1], (52, HEADER.replace("2025", "2024")), cross_lines()[3])},
    {"lines": (cross_lines()[0], cross_lines()[1], (53, HEADER), (53, "量净额"))},
    {"lines": (*cross_lines()[:2], (52, HEADER), (52, "会计政策说明"), (52, "量净额"))},
    {"lines": (*cross_lines()[:2], (52, "母公司现金流量表"), (52, "量净额"))},
])
def test_cross_page_does_not_skip_unknown_footer_header_other_role_or_intervening_text(changes):
    options = {"pdf_page_count": 175, "continuation_report_header": HEADER}
    options.update(changes)
    assert section(cross_lines(), **options).find("operating_cashflow") is None


@pytest.mark.parametrize("note", ["七、（79）", "七、(79)", "七、79"])
def test_only_complete_observed_note_tokens_in_the_existing_three_line_cfo_rule(note):
    lines = ((51, "经营活动产生的现金流"),
             (51, note + " 8,375,244,370.14 7,890,175,064.67"), (51, "量净额"))
    row = section(lines).find("operating_cashflow")
    assert row.amount == Decimal("8375244370.14")
    assert row.raw_line == "\n".join(text for _, text in lines)


@pytest.mark.parametrize("note", ["七、（79", "七、79）", "七、(79）", "七、（79)",
                                 "七、()", "七、（0）", "七、（79）说明", "79"])
def test_unclosed_or_prose_note_tokens_do_not_become_cfo_amounts(note):
    assert section(((51, "经营活动产生的现金流"),
        (51, note + " 100.00 200.00"), (51, "量净额"))).find("operating_cashflow") is None


PARENT_LINES = (
    ((49, "归属于母公司所有者权益 15,082,767,775.77 14,321,719,535.57"),
     (49, "（或股东权益）合计少数股东权益 625,411,338.35 199,157,313.77")),
    ((42, "归属于母公司所有者权益（或股 4,708,750,908.56"),
     (42, "4,354,464,746.68"),
     (42, "东权益）合计少数股东权益 140,171,839.19 136,284,413.93")),
)


@pytest.mark.parametrize("lines,current,prior", [
    (PARENT_LINES[0], "15082767775.77", "14321719535.57"),
    (PARENT_LINES[1], "4708750908.56", "4354464746.68"),
])
def test_parent_suffix_glue_does_not_move_minority_amounts_to_parent(lines, current, prior):
    original = section(lines, statement="资产负债表")
    row = original.find("parent_equity")
    assert row.amount == Decimal(current) and row.prior_amount == Decimal(prior)
    assert row.raw_line == "\n".join(line for _, line in lines)
    assert original.component("minority_equity") is None


@pytest.mark.parametrize("lines", [
    ((49, "归属于母公司所有者权益 100.00"), (49, "（或股东权益）合计少数股东权益 1.00 2.00")),
    ((49, "归属于母公司所有者权益 100.00 200.00"), (49, "（或股东权益）合计其他权益 1.00 2.00")),
    ((49, "归属于母公司所有者权益 100.00 200.00"), (49, "（或股东权益）合计少数股东权益 - 2.00")),
    ((49, "归属于母公司所有者权益 100.00 200.00"), (50, "（或股东权益）合计少数股东权益 1.00 2.00")),
    ((42, "归属于母公司所有者权益（或股 100.00"), (42, "200.00 300.00"),
     (42, "东权益）合计少数股东权益 1.00 2.00")),
])
def test_parent_suffix_rejects_wrong_boundary_cells_and_labels(lines):
    assert section(lines, statement="资产负债表").find("parent_equity") is None


@pytest.mark.parametrize("minority", ["999,999,999,999.99 888,888,888,888.88", "100.00 200.00"])
def test_following_minority_values_cannot_change_recovered_parent_values(minority):
    lines = ((49, "归属于母公司所有者权益 100.00 200.00"),
             (49, "（或股东权益）合计少数股东权益 " + minority))
    row = section(lines, statement="资产负债表").find("parent_equity")
    assert (row.amount, row.prior_amount) == (Decimal("100.00"), Decimal("200.00"))
    assert section(lines + lines, statement="资产负债表").find("parent_equity") is None


def test_duplicate_literal_recoveries_are_rejected_and_existing_direct_row_wins():
    lines = ((51, "经营活动产生的现金流 100.00 200.00"), (51, "量净额"))
    assert section(lines + lines).find("operating_cashflow") is None
    direct = StatementRow(role="合并", statement="现金流量表", label="经营活动产生的现金流量净额",
        amount=Decimal("300.00"), prior_amount=Decimal("400.00"), page=51, unit="元",
        current_cell="300.00", prior_cell="400.00", raw_line="original direct row")
    assert section(lines, rows=(direct,)).find("operating_cashflow") is direct


def test_revenue_note_suffix_is_scoped_and_cannot_substitute_total_revenue():
    original = section((), statement="利润表")
    assert original.normalize("其中：营业收入 七、（61）") == "其中:营业收入"
    assert original.normalize("营业收入 七、(61)") == "营业收入"
    assert original.normalize("其中：营业收入 七、（61）说明") != "其中:营业收入"
    assert replace(original, role="母公司").normalize("其中：营业收入 七、（61）") != "其中:营业收入"
    assert original.normalize("一、营业总收入 七、（61）") != "营业收入"


def test_continuation_header_must_be_a_unique_whole_cover_issuer_period_line():
    class Page:
        def __init__(self, text): self.text = text
        def extract_text(self): return self.text
    class Pdf:
        def __init__(self, *texts): self.pages = [Page(t) for t in texts]
    from datetime import date
    assert _literal_continuation_report_header(Pdf(HEADER, HEADER), date(2025, 6, 30)) == HEADER
    assert _literal_continuation_report_header(Pdf(HEADER, HEADER.replace("锦江", "外部")), date(2025, 6, 30)) == ""
    assert _literal_continuation_report_header(Pdf(HEADER.replace("2025", "2024")), date(2025, 6, 30)) == ""
    assert _literal_continuation_report_header(Pdf("说明：" + HEADER), date(2025, 6, 30)) == ""
