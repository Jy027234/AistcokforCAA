from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from aquant.adapters.providers.sina_financial import (
    merge_s2_pages,
    parse_statement_page,
)


def page(title: str, rows: list[tuple[str, str, str]]) -> str:
    body = "".join(
        f"<tr><td><a>{label}</a></td><td>{a}</td><td>{b}</td></tr>"
        for label, a, b in rows
    )
    return ("<table><thead><tr><th>" + title
            + "<div>单位：万元</div></th></tr></thead><tbody>"
            + "<tr><td><strong>报表日期</strong></td>"
              "<td>2026-06-30</td><td>2026-03-31</td></tr>"
            + body + "</tbody></table>")


NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)


def test_parser_converts_wan_yuan_and_preserves_missing_values():
    parsed = parse_statement_page(
        page("利润表", [("营业收入", "1,234.50", "--")]),
        stock_code="688489", statement="profit", retrieved_at=NOW,
    )
    assert parsed.rows["营业收入"] == (Decimal("12345000.00"), None)


def test_parser_does_not_confuse_observed_zero_with_missing():
    parsed = parse_statement_page(
        page("利润表", [("营业收入", "0", "-")]),
        stock_code="688489", statement="profit", retrieved_at=NOW,
    )
    assert parsed.rows["营业收入"] == (Decimal("0"), None)


def test_three_pages_expose_s2_fields_but_remain_pit_ineligible():
    profit = parse_statement_page(page("利润表", [
        ("营业收入", "100", "80"),
        ("五、净利润", "10", "8"),
        ("归属于母公司所有者的净利润", "9", "7"),
    ]), stock_code="688489", statement="profit", retrieved_at=NOW)
    balance = parse_statement_page(page("资产负债表", [
        ("归属于母公司股东权益合计", "50", "48"),
    ]), stock_code="688489", statement="balance", retrieved_at=NOW)
    cashflow = parse_statement_page(page("现金流量表", [
        ("经营活动产生的现金流量净额", "12", "6"),
    ]), stock_code="688489", statement="cashflow", retrieved_at=NOW)

    rows = merge_s2_pages(profit=profit, balance=balance, cashflow=cashflow)
    assert len(rows) == 2
    assert rows[0].required_fields_present is True
    assert rows[0].pit_eligible is False
    assert rows[0].revenue == Decimal("1000000")
    assert rows[0].parent_equity == Decimal("500000")
