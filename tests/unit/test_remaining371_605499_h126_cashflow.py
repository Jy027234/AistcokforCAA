"""Exact three-line CFO source recovery; no scope or missing-cell inference."""
from dataclasses import replace
from decimal import Decimal

import pytest

from aquant.adapters.providers.cninfo_statement_sections import (
    StatementSection, _dewrapped,
)

REAL = (
    (73, "经营活动产生的现金"),
    (73, "七、79 2,492,600,974.20 1,740,460,562.79"),
    (73, "流量净额"),
)


def section(lines=REAL, **kwargs):
    result = StatementSection(role="合并", statement="现金流量表",
        start_page=72, end_page=74, unit="元", current_header="2026年半年度",
        prior_header="2025年半年度", rows=(), lines=tuple(lines))
    return replace(result, **kwargs)


def test_exact_native_three_lines_preserve_both_cells_and_all_source_lines():
    result = section().find("operating_cashflow")
    assert result is not None
    assert result.amount == Decimal("2492600974.20")
    assert result.prior_amount == Decimal("1740460562.79")
    assert result.current_cell == "2,492,600,974.20"
    assert result.prior_cell == "1,740,460,562.79"
    assert result.raw_line == "\n".join(line for _, line in REAL)
    assert result.page == 73 and result.unit == "元"
    assert section().component("operating_cashflow") == result
    # Global/source lines are unchanged: only the scoped row lookup recovers.
    assert _dewrapped(list(REAL)) == list(REAL)


@pytest.mark.parametrize("changes", [
    {"role": "母公司"}, {"statement": "利润表"},
    {"statement": "现金流量表附注"}, {"combined": True},
    {"scope_columns": 4}, {"unit": ""}, {"unit": "美元"},
    {"current_header": ""}, {"prior_header": ""},
])
def test_recovery_requires_existing_consolidated_two_column_section(changes):
    assert section(**changes).find("operating_cashflow") is None


@pytest.mark.parametrize("middle", [
    "七、 2,492,600,974.20 1,740,460,562.79",
    "79 2,492,600,974.20 1,740,460,562.79",
    "七、79", "七、0 2,492,600,974.20 1,740,460,562.79",
    "七、79 2,492,600,974.20", "七、79 - 1,740,460,562.79",
    "七、79 2,492,600,974.20 -", "七、79 2,492,600,974.20 1,740,460,562.79 1.00",
    "七、79 2,492,600,974.20 说明 1,740,460,562.79",
    "七、79 2,492,600,974.20 1,740,460,562.79 说明",
    "附注七、79 2,492,600,974.20 1,740,460,562.79",
])
def test_incomplete_note_cells_or_intervening_prose_do_not_become_cfo(middle):
    assert section((REAL[0], (73, middle), REAL[2])).find("operating_cashflow") is None


@pytest.mark.parametrize("lines", [
    ((72, REAL[0][1]), REAL[1], REAL[2]),
    (REAL[0], REAL[1], (74, REAL[2][1])),
    (REAL[0], (73, "本期发生额 上期发生额"), REAL[1], REAL[2]),
    (REAL[0], REAL[1], (73, "见下表"), REAL[2]),
    (REAL[0], REAL[1], (73, "流量净额说明")),
    ((73, "投资活动产生的现金"), REAL[1], REAL[2]),
    ((73, "经营活动产生的现金说明"), REAL[1], REAL[2]),
    REAL + REAL,
])
def test_exact_adjacent_same_page_label_chain_is_required(lines):
    assert section(lines).find("operating_cashflow") is None


def test_unrelated_field_cannot_consume_the_recovered_cashflow():
    assert section().find("revenue") is None
