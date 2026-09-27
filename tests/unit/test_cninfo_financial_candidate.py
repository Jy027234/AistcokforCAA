"""Candidate-only CNINFO parser rejects wrong identity, labels and columns."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from aquant.adapters.providers.cninfo_financial_candidate import (
    _expected_header,
    _field_matches,
    _page_headings,
    _row_amounts,
    _text_layout_rows,
    extract_cninfo_candidate_facts,
)
from aquant.adapters.providers.cninfo_financial_pdf import CninfoFinancialPdfError


def test_s2_labels_keep_required_statement_semantics() -> None:
    assert _field_matches("revenue", "其中：营业收入")
    assert _field_matches("revenue", "一、营业收入")
    assert not _field_matches("revenue", "一、营业总收入")
    assert _field_matches("net_profit_consolidated", "五、净利润（净亏损以“-”号填列）")
    assert not _field_matches("net_profit_consolidated", "持续经营净利润")
    assert _field_matches("parent_equity", "归属于母公司所有者\n权益（或股东权益）合\n计")
    assert not _field_matches("parent_equity", "所有者权益合计")


def test_current_column_and_unit_cells_are_explicit() -> None:
    assert _expected_header(
        "利润表", date(2026, 6, 30),
        ["项目", "附注", "2026 年半年度", "2025 年半年度"],
    ) == ("2026年半年度", "2025年半年度")
    assert _expected_header(
        "利润表", date(2026, 6, 30),
        ["项目", "附注", "本期发生额", "上年同期发生额"],
    ) == ("本期发生额", "上年同期发生额")
    with pytest.raises(CninfoFinancialPdfError, match="current/prior header"):
        _expected_header("利润表", date(2026, 6, 30),
                         ["项目", "2025 年半年度", "2026 年半年度"])
    label, current, prior, amount = _row_amounts(
        ["其中：营业收入", "七、61", "15,455,583,728.11", "15,761,193,628.90"],
        field="revenue",
    )
    assert (label, current, prior, amount) == (
        "其中：营业收入", 2, 3, Decimal("15455583728.11"),
    )
    with pytest.raises(CninfoFinancialPdfError, match="exactly two"):
        _row_amounts(["净利润", "1", "2", "3"], field="net_profit_consolidated")
    assert _row_amounts(
        ["经营活动产生的现金流量净额", "5,343,019,637.89", "(189,636,040.90)"],
        field="operating_cashflow",
    )[2] == 2


def test_borderless_annual_text_keeps_only_the_two_amount_cells() -> None:
    header, rows = _text_layout_rows([
        (88, "项 目 附注 2025年度 2024年度\n"
             "其中：营业收入 五、54 170,447,058,533.57 189,163,654,064.64"),
    ], "利润表", date(2025, 12, 31))
    assert header == ("2025年度", "2024年度")
    assert rows == [(88, ["其中：营业收入", "170,447,058,533.57",
                          "189,163,654,064.64"])]


def test_pdf_identity_is_required_before_parsing() -> None:
    with pytest.raises(CninfoFinancialPdfError, match="identity mismatch"):
        extract_cninfo_candidate_facts(
            b"%PDF-fake", instrument_id="600276",
            company="江苏恒瑞医药股份有限公司",
            period_end=date(2026, 6, 30), announcement_id="1225483600",
            document_url="https://static.cninfo.com.cn/finalpage/2026-08-20/other.PDF",
        )


def test_explicit_non_cny_statement_currency_fails_closed() -> None:
    class Page:
        def __init__(self, heading_lines: str) -> None:
            self.heading_lines = heading_lines

        def extract_text(self) -> str:
            return f"合并利润表\n2026年1—6月\n{self.heading_lines}\n项目"

        def search(self, _pattern: str) -> list[dict]:
            return [{"top": 10, "bottom": 20}]

    with pytest.raises(CninfoFinancialPdfError, match="non-CNY"):
        _page_headings(Page("单位：元 币种：美元"), 0)
    with pytest.raises(CninfoFinancialPdfError, match="non-CNY"):
        _page_headings(Page("单位：元\n币种：美元"), 0)
    with pytest.raises(CninfoFinancialPdfError, match="conflicting amount units"):
        _page_headings(Page("单位：元\n单位：千元"), 0)
    assert _page_headings(Page("单位：元 币种：人民币"), 0)[0].unit == "元"
