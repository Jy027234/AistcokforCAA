"""601012／600519 官方 PDF 封闭样本；真实原文通过环境变量提供。"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from aquant.adapters.providers.cninfo_financial_pdf import (
    CninfoFinancialPdfError,
    _ADDITIONAL_PERIODS,
    _FieldCellLayout,
    _STATEMENTS,
    _header_from_page,
    _header_from_split_pages,
    _parse_amount,
    _unique_row,
    extract_s2_candidate_facts,
)
from aquant.domain.research.s2 import required_s2_periods


PERIOD = date(2024, 12, 31)
EXPECTED = {
    "net_profit_attributable": (Decimal("-8617528506.44"), 125, "2024年度"),
    "net_profit_consolidated": (Decimal("-8677451528.22"), 125, "2024年度"),
    "revenue": (Decimal("82582273118.72"), 124, "2024年度"),
    "parent_equity": (Decimal("60895314122.52"), 121, "2024年12月31日"),
    "operating_cashflow": (Decimal("-4724978931.84"), 128, "2024年度"),
}
HASHES = {
    "original": "3dbaf5314ab01f44b58f58e8ad75fea0dce3ced0001db516829f2624b615a3a0",
    "revised": "c9e11fc7ec92a59d7d1ccfcf8a4f1fbb35ee8ec0435df16a3e968497b5f64bb8",
}
ADDITIONAL = {
    "2024H1": (
        date(2024, 6, 30),
        "dba216e8f28538ce5fd3bf9b1cd4e428af8b52ff3dcf0795ac5237cd39543677",
        {"net_profit_attributable": ("-5243344677.95", 66, "2024年半年度"),
         "net_profit_consolidated": ("-5257395058.02", 66, "2024年半年度"),
         "revenue": ("38528702860.54", 65, "2024年半年度"),
         "parent_equity": ("64396110127.79", 62, "2024年6月30日"),
         "operating_cashflow": ("-6413098864.02", 70, "2024年半年度")},
    ),
    "2025H1": (
        date(2025, 6, 30),
        "449aaf2308a9374317604a7a7dce7eb1ef661d3c01f3e2f8a0f1cd0cb83a28c6",
        {"net_profit_attributable": ("-2569358351.05", 63, "2025年半年度"),
         "net_profit_consolidated": ("-2598193935.31", 63, "2025年半年度"),
         "revenue": ("32813146398.68", 62, "2025年半年度"),
         "parent_equity": ("58274955479.58", 59, "2025年6月30日"),
         "operating_cashflow": ("-484332299.83", 66, "2025年半年度")},
    ),
    "2025FY": (
        date(2025, 12, 31),
        "10233740d54740b89dfd772eecac5f04d8fef639489acd3b18b1a35e81bce2c1",
        {"net_profit_attributable": ("-6419556843.85", 126, "2025年度"),
         "net_profit_consolidated": ("-6509714466.50", 126, "2025年度"),
         "revenue": ("70347049950.42", 125, "2025年度"),
         "parent_equity": ("54275611054.63", 122, "2025年12月31日"),
         "operating_cashflow": ("4359382755.77", 129, "2025年度")},
    ),
    "2026H1": (
        date(2026, 6, 30),
        "c3bcfa8bcf5cd18df6c008a48382119039eac71fe4273973fa0c8bbe7128f103",
        {"net_profit_attributable": ("-3684104428.05", 70, "2026年半年度"),
         "net_profit_consolidated": ("-3843140471.53", 70, "2026年半年度"),
         "revenue": ("27045209097.55", 69, "2026年半年度"),
         "parent_equity": ("48622113127.87", 66, "2026年6月30日"),
         "operating_cashflow": ("-5817794006.91", 73, "2026年半年度")},
    ),
}

MOUTAI = {
    "2024H1": (
        date(2024, 6, 30),
        "898b37d6529d0042aa66461bae11b84a47f9337c2ff1d572155bfa71fca77c58",
        {"net_profit_attributable": ("41695610983.37", 31, "2024年半年度"),
         "net_profit_consolidated": ("43176914345.12", 31, "2024年半年度"),
         "revenue": ("81930977667.75", 30, "2024年半年度"),
         "parent_equity": ("218575608600.14", 28, "2024年6月30日"),
         "operating_cashflow": ("36621833812.63", 35, "2024年半年度")},
    ),
    "2024FY": (
        date(2024, 12, 31),
        "5299f4940e2ce4e91084b73dc457d558b9d335fa76fbfee6227e4254eb7f4a30",
        {"net_profit_attributable": ("86228146421.62", 64, "2024年度"),
         "net_profit_consolidated": ("89334728025.90", 64, "2024年度"),
         "revenue": ("170899152276.34", 63, "2024年度"),
         "parent_equity": ("233105984399.47", 61, "2024年12月31日"),
         "operating_cashflow": ("92463692168.43", 67, "2024年度")},
    ),
    "2025H1": (
        date(2025, 6, 30),
        "c80fb7180169469053c396e65315414368bcfa9f1f1d4e3bf793c8fb327e6b0c",
        {"net_profit_attributable": ("45402962298.10", 29, "2025年半年度"),
         "net_profit_consolidated": ("46986681449.24", 29, "2025年半年度"),
         "revenue": ("89389354416.84", 28, "2025年半年度"),
         "parent_equity": ("238646536390.48", 26, "2025年6月30日"),
         "operating_cashflow": ("13119061031.33", 32, "2025年半年度")},
    ),
    "2025FY": (
        date(2025, 12, 31),
        "474905deeaf0f875fc0a1b097a626c0c7852c427faadc5d7fc7816cbf45ea288",
        {"net_profit_attributable": ("82320067101.68", 62, "2025年度"),
         "net_profit_consolidated": ("85310324833.67", 62, "2025年度"),
         "revenue": ("168838102514.79", 61, "2025年度"),
         "parent_equity": ("244637811032.18", 58, "2025年12月31日"),
         "operating_cashflow": ("61522204989.35", 65, "2025年度")},
    ),
    "2026H1": (
        date(2026, 6, 30),
        "0e10aa26be46b1cf3cd03f06e834c7fb98d5dd0d661b96f8fddd4af7e846a4f6",
        {"net_profit_attributable": ("44516880421.86", 31, "2026年半年度"),
         "net_profit_consolidated": ("46033330566.78", 31, "2026年半年度"),
         "revenue": ("90703260964.48", 30, "2026年半年度"),
         "parent_equity": ("251253594419.50", 28, "2026年6月30日"),
         "operating_cashflow": ("70690750119.06", 34, "2026年半年度")},
    ),
}


class _Page:
    def __init__(self, text: str, tables: list[list[list[str]]]) -> None:
        self.text = text
        self.tables = tables

    def extract_text(self) -> str:
        return self.text

    def extract_tables(self) -> list[list[list[str]]]:
        return self.tables


def test_only_reviewed_identity_and_bytes_are_accepted() -> None:
    with pytest.raises(CninfoFinancialPdfError, match="outside reviewed"):
        extract_s2_candidate_facts(b"%PDF-other", instrument_id="000001", period_end=PERIOD)
    with pytest.raises(CninfoFinancialPdfError, match="outside reviewed"):
        extract_s2_candidate_facts(
            b"%PDF-other", instrument_id="601012", period_end=date(2023, 12, 31),
        )
    with pytest.raises(CninfoFinancialPdfError, match="approved report version"):
        extract_s2_candidate_facts(b"%PDF-other", instrument_id="601012", period_end=PERIOD)


def test_amount_rejects_unit_or_missing_cell() -> None:
    assert _parse_amount("-8,617,528,506.44", field="x") == Decimal("-8617528506.44")
    for value in ("", "--", "8617万元", "8,617.5", "1,23.00", "1e6"):
        with pytest.raises(CninfoFinancialPdfError, match="ambiguous yuan amount"):
            _parse_amount(value, field="x")


def test_section_requires_exact_consolidated_period_unit_and_columns() -> None:
    rule = _STATEMENTS["profit"]
    good_text = "\n".join((
        "隆基绿能科技股份有限公司2024年年度报告", "合并利润表",
        "2024年1—12月", "单位：元 币种：人民币",
        "项目 附注 2024年度 2023年度",
    ))
    good_table = [["项目", "附注", "2024年度", "2023年度"]]
    _header_from_page(_Page(good_text, [good_table]), rule)
    for altered in (
        good_text.replace("合并利润表", "母公司利润表"),
        good_text.replace("2024年1—12月", "2023年1—12月"),
        good_text.replace("单位：元", "单位：万元"),
    ):
        with pytest.raises(CninfoFinancialPdfError, match="heading, period or CNY/yuan unit"):
            _header_from_page(_Page(altered, [good_table]), rule)
    with pytest.raises(CninfoFinancialPdfError, match="column header"):
        _header_from_page(_Page(good_text, [["项目", "附注", "2023年度", "2024年度"]]), rule)


def test_duplicate_or_shifted_rows_are_rejected() -> None:
    row = ["其中：营业收入", "七、61", "82,582,273,118.72", "129,497,674,192.20"]
    assert _unique_row(_Page("", [[row]]), field="revenue", label="其中：营业收入") == tuple(row)
    with pytest.raises(CninfoFinancialPdfError, match="found 2"):
        _unique_row(_Page("", [[row], [row]]), field="revenue", label="其中：营业收入")
    with pytest.raises(CninfoFinancialPdfError, match="ambiguous table columns"):
        _unique_row(_Page("", [[row[:-1]]]), field="revenue", label="其中：营业收入")


def test_2025_h1_split_cashflow_heading_requires_next_page_unit_and_columns() -> None:
    rule = _ADDITIONAL_PERIODS[date(2025, 6, 30)][2]["cashflow"]
    title = _Page("\n".join(("合并现金流量表", "2025年1—6月", "65/237")), [])
    next_page = _Page(
        "\n".join(("隆基绿能科技股份有限公司2025年半年度报告",
                   "单位：元 币种：人民币", "项目 附注 2025年半年度 2024年半年度")),
        [[["项目", "附注", "2025年半年度", "2024年半年度"]]],
    )
    _header_from_split_pages(title, next_page, rule)
    with pytest.raises(CninfoFinancialPdfError, match="split CNY/yuan unit"):
        _header_from_split_pages(
            title, _Page(next_page.text.replace("单位：元", "单位：万元"), next_page.tables),
            rule,
        )
    with pytest.raises(CninfoFinancialPdfError, match="column header"):
        _header_from_split_pages(
            title, _Page(next_page.text,
                         [[["项目", "附注", "2024年半年度", "2025年半年度"]]]),
            rule,
        )


def test_known_five_cell_profit_row_requires_explicit_prior_index() -> None:
    label = "1.归属于母公司股东的净利润（净亏损以“-”号填列）"
    row = [label, "", "82,320,067,101.68", None, "86,228,146,421.62"]
    layout = _FieldCellLayout(5, 2, 4, 3)
    assert _unique_row(_Page("", [[row]]), field="net_profit_attributable",
                       label=label, layout=layout) == tuple(row)
    with pytest.raises(CninfoFinancialPdfError, match="ambiguous table columns"):
        _unique_row(_Page("", [[row]]), field="net_profit_attributable", label=label)
    with pytest.raises(CninfoFinancialPdfError, match="ambiguous table columns"):
        _unique_row(_Page("", [[[label, "", "82,320,067,101.68",
                                 "86,228,146,421.62", None]]]),
                    field="net_profit_attributable", label=label, layout=layout)


def test_two_official_versions_same_facts_distinct_hashes_when_supplied() -> None:
    paths = [os.getenv("AQUANT_CNINFO_601012_ORIGINAL_PDF"),
             os.getenv("AQUANT_CNINFO_601012_REVISED_PDF")]
    if not all(paths):
        pytest.skip("set both AQUANT_CNINFO_601012_*_PDF paths to local official reports")
    outputs = []
    for version, path in zip(("original", "revised"), paths, strict=True):
        result = extract_s2_candidate_facts(
            Path(path).read_bytes(), instrument_id="601012", period_end=PERIOD,
        )
        assert result.version_label == version
        assert result.pdf_sha256 == "sha256:" + HASHES[version]
        assert result.pit_eligible is False
        assert result.review_status == "requires_manual_verification"
        assert set(result.by_field) == set(EXPECTED)
        for field, (amount, page, column) in EXPECTED.items():
            candidate = result.by_field[field]
            assert (candidate.value_yuan, candidate.pdf_page, candidate.column_header) == (
                amount, page, column,
            )
            assert candidate.period_end == PERIOD
            assert candidate.report_period_text == (
                "2024年12月31日" if field == "parent_equity" else "2024年1—12月"
            )
            assert candidate.currency == "CNY" and candidate.amount_unit == "元"
            assert candidate.pdf_sha256 == result.pdf_sha256
            assert candidate.source_row and len(candidate.source_cells) == 4
        outputs.append(result)
    assert outputs[0].pdf_sha256 != outputs[1].pdf_sha256
    assert {key: value.value_yuan for key, value in outputs[0].by_field.items()} == {
        key: value.value_yuan for key, value in outputs[1].by_field.items()
    }


@pytest.mark.parametrize("report_key", tuple(ADDITIONAL))
def test_additional_official_full_report_current_columns_when_supplied(
    report_key: str,
) -> None:
    path = os.getenv(f"AQUANT_CNINFO_601012_{report_key}_PDF")
    if not path:
        pytest.skip(f"set AQUANT_CNINFO_601012_{report_key}_PDF to the official PDF")
    period, digest, expected = ADDITIONAL[report_key]
    result = extract_s2_candidate_facts(
        Path(path).read_bytes(), instrument_id="601012", period_end=period,
    )
    assert result.pdf_sha256 == "sha256:" + digest
    assert result.version_label == "indexed_full_report"
    assert result.pit_eligible is False
    assert result.review_status == "requires_manual_verification"
    assert set(result.by_field) == set(expected)
    for field, (amount, page, column) in expected.items():
        candidate = result.by_field[field]
        assert (candidate.value_yuan, candidate.pdf_page, candidate.column_header) == (
            Decimal(amount), page, column,
        )
        assert Decimal(candidate.source_cells[2].replace(",", "")) == candidate.value_yuan
        assert candidate.period_end == period
        assert candidate.report_period_text == (
            column if field == "parent_equity" else
            f"{period.year}年1—{'12' if period.month == 12 else '6'}月"
        )
        assert candidate.amount_unit == "元" and candidate.currency == "CNY"
        assert candidate.pdf_sha256 == result.pdf_sha256
        assert len(candidate.source_cells) == 4 and candidate.source_row
    with pytest.raises(CninfoFinancialPdfError, match="period mismatches"):
        extract_s2_candidate_facts(
            Path(path).read_bytes(), instrument_id="601012", period_end=PERIOD,
        )


def test_revenue_comparative_columns_match_earlier_current_filings_when_supplied() -> None:
    keys = ("2024H1", "2025H1", "2025FY", "2026H1")
    paths = {key: os.getenv(f"AQUANT_CNINFO_601012_{key}_PDF") for key in keys}
    revised_2024 = os.getenv("AQUANT_CNINFO_601012_REVISED_PDF")
    if not all(paths.values()) or not revised_2024:
        pytest.skip("set all five official PDF paths to audit revenue comparatives")
    reports = {
        key: extract_s2_candidate_facts(
            Path(paths[key]).read_bytes(), instrument_id="601012",
            period_end=ADDITIONAL[key][0],
        ) for key in keys
    }
    reports["2024FY"] = extract_s2_candidate_facts(
        Path(revised_2024).read_bytes(), instrument_id="601012", period_end=PERIOD,
    )
    for newer, older in (("2025H1", "2024H1"),
                         ("2025FY", "2024FY"),
                         ("2026H1", "2025H1")):
        comparative = Decimal(reports[newer].by_field["revenue"].source_cells[3].replace(",", ""))
        prior_current = reports[older].by_field["revenue"].value_yuan
        assert comparative == prior_current
        assert reports[newer].pdf_sha256 != reports[older].pdf_sha256


@pytest.mark.parametrize("report_key", tuple(MOUTAI))
def test_moutai_official_full_report_current_columns_when_supplied(report_key: str) -> None:
    path = os.getenv(f"AQUANT_CNINFO_600519_{report_key}_PDF")
    if not path:
        pytest.skip(f"set AQUANT_CNINFO_600519_{report_key}_PDF to official report")
    period, digest, expected = MOUTAI[report_key]
    payload = Path(path).read_bytes()
    result = extract_s2_candidate_facts(
        payload, instrument_id="600519", period_end=period,
    )
    assert result.pdf_sha256 == "sha256:" + digest
    assert result.instrument_id == "600519" and result.period_end == period
    assert result.pit_eligible is False
    assert result.review_status == "requires_manual_verification"
    assert set(result.by_field) == set(expected)
    for field, (amount, page, column) in expected.items():
        fact = result.by_field[field]
        assert (fact.value_yuan, fact.pdf_page, fact.column_header) == (
            Decimal(amount), page, column,
        )
        assert fact.report_period_text == (
            "2026 年6月30 日" if report_key == "2026H1" and field == "parent_equity"
            else column if field == "parent_equity"
            else f"{period.year}年1—{'12' if period.month == 12 else '6'}月"
        )
        assert fact.amount_unit == "元" and fact.currency == "CNY"
        assert fact.pdf_sha256 == result.pdf_sha256
        assert fact.source_cells[fact.source_current_cell_index] is not None
        assert Decimal(fact.source_cells[fact.source_current_cell_index].replace(",", "")) == fact.value_yuan
        if report_key == "2025FY" and field == "net_profit_attributable":
            assert len(fact.source_cells) == 5 and fact.source_prior_cell_index == 4
            assert fact.source_cells[3] is None
    with pytest.raises(CninfoFinancialPdfError, match="instrument mismatches"):
        extract_s2_candidate_facts(payload, instrument_id="601012", period_end=period)


def test_moutai_2026_h1_has_all_required_s2_periods_and_positive_net_ttm_when_supplied() -> None:
    paths = {key: os.getenv(f"AQUANT_CNINFO_600519_{key}_PDF") for key in MOUTAI}
    if not all(paths.values()):
        pytest.skip("set all five AQUANT_CNINFO_600519_*_PDF paths")
    reports = {
        MOUTAI[key][0]: extract_s2_candidate_facts(
            Path(path).read_bytes(), instrument_id="600519", period_end=MOUTAI[key][0],
        ) for key, path in paths.items()
    }
    requirements = required_s2_periods(date(2026, 6, 30))
    assert sum(len(periods) for periods in requirements.values()) == 16
    assert all(
        metric in reports[period].by_field
        for metric, periods in requirements.items()
        for period in periods
    )
    net_ttm = (
        reports[date(2025, 12, 31)].by_field["net_profit_consolidated"].value_yuan
        + reports[date(2026, 6, 30)].by_field["net_profit_consolidated"].value_yuan
        - reports[date(2025, 6, 30)].by_field["net_profit_consolidated"].value_yuan
    )
    assert net_ttm == Decimal("84356973951.21") > 0
