"""行名**末字折到下一行行首**时的复原用例。

这个形态来自真实原文：600276 2026 半年报合并资产负债表第 76 页把
``归属于母公司所有者权益（或股东权益）合计``排成三行，收尾的``计``独占一行：

.. code-block:: text

    归属于母公司所有者
    权益（或股东权益）合 64,466,430,205.71 61,272,068,641.41
    计
    少数股东权益 523,310,211.35 524,754,772.84

这个``计``既接得上上一行（``…（或股东权益）合``），也接得上下一行
（``计少数股东权益``）——**两个方向都能拼出一个"像行名"的东西**，所以方向不能
靠形状猜，只能靠"拼出来是不是一个已知术语"来验证。

本文件盯住的两件事：

1. 真实形态必须被复原：权益行行名完整、下一行不带``计``前缀，且两行都能被
   ``StatementSection.find`` / ``component`` 定位到；
2. 规则在**本来完整的行**、**两个不同科目**、**不是术语的拼接**上都不许开火。

第 2 条是重点。这条判定被写坏过一次：它复用了 ``_field_matches``，而归母权益
那一支只判断"以``归属于母公司所有者/股东权益``开头"且"行内出现``合计``"，于是
``…（或股东权益）合计``再被接一个字**仍然算命中术语**。标准资产负债表里
``少数股东权益``恰好紧跟归母权益行，结果两条完好的行被同时切坏成
``…合计少`` / ``数股东权益``（实测 000651 2024-12-31 因此由全通过变成
``parent_equity`` 未通过）。因此下面的反例不是假想，是本机归档报告的版式。
"""

from __future__ import annotations

from decimal import Decimal

from aquant.adapters.providers.cninfo_statement_sections import (
    StatementRow, StatementSection, _dewrapped, _matches_component_label,
    _normalize_label, _resplit_wrapped_tail, _row_amounts,
)

#: 600276 2026-06-30 合并资产负债表第 76 页的**逐字原文行**（pdfplumber 抽取）。
REAL_600276_P76 = [
    "未分配利润 七、60 43,312,733,571.33 40,173,354,828.03",
    "归属于母公司所有者",
    "权益（或股东权益）合 64,466,430,205.71 61,272,068,641.41",
    "计",
    "少数股东权益 523,310,211.35 524,754,772.84",
    "所有者权益（或股",
    "64,989,740,417.06 61,796,823,414.25",
    "东权益）合计",
]

#: 对照形态（行名取自 600519 实测原文）：归母权益行**本来就完整**，紧随其后的是
#: 另一行数据行。这正是"多切一个字"会同时毁掉两条行的版式。
COMPLETE_EQUITY_THEN_NEXT_ROW = [
    "归属于母公司所有者权益（或股东权益）合计 238,646,536,390.48 233,105,984,399.47",
    "少数股东权益 2,000,000.00 1,000,000.00",
    "所有者权益（或股东权益）合计 240,646,536,390.48 234,105,984,399.47",
]

#: 对照形态（000651 2024-06-30 第 127 页附注原文）：上一行行名停在``险组``，
#: 孤立的``合``后面才是**自带完整行名**的``合计``行。这里``合``确实属于上一行，
#: 但``险组合``不是本模块认识的术语，因此规则必须保持沉默——宁可不动，也不能
#: 为了让一行"看起来更完整"去切掉下一行的首字。
REAL_000651_P127 = [
    (127, "险组 294,059,546.00 62,013,123.60 232,046,422.40 "
          "317,305,464.00 62,404,031.17 254,901,432.83"),
    (127, "合计 1,279,398,852.30 487,641,110.85 791,757,741.45 "
          "1,331,845,547.38 493,033,413.73 838,812,133.65"),
]


def _page_lines(lines: list[str]) -> list[str]:
    """按抽取器的方式复原换行（页码固定为第 76 页）。"""

    return [line for _, line in _dewrapped([(76, item) for item in lines])]


def _labels(lines: list[str]) -> list[str]:
    """复原后每一条数据行的行名。"""

    parsed = (_row_amounts(line) for line in _page_lines(lines))
    return [item[0] for item in parsed if item is not None]


def _section(lines: list[str]) -> StatementSection:
    """按抽取器的方式把原文行变成表节（复原换行 → 解析金额 → 建行）。"""

    rows = []
    for line in _page_lines(lines):
        parsed = _row_amounts(line)
        if parsed is None:
            continue
        label, cell, value, prior = parsed
        rows.append(StatementRow(
            role="合并", statement="资产负债表", label=label, amount=value,
            prior_amount=(Decimal(0) if prior is None else prior), page=76,
            unit="元", current_cell=cell,
            prior_cell=("not-on-line" if prior is None else str(prior)),
            raw_line=line))
    return StatementSection(
        role="合并", statement="资产负债表", start_page=76, end_page=76,
        unit="元", current_header="2026年6月30日", prior_header="2025年12月31日",
        rows=tuple(rows), lines=tuple(_dewrapped([(76, item) for item in lines])))


def test_real_600276_tail_character_returns_to_its_own_row() -> None:
    """实测 600276：收尾的``计``必须回到权益行，且不得粘到下一行行首。"""

    labels = _labels(REAL_600276_P76)
    assert "归属于母公司所有者权益（或股东权益）合计" in labels
    assert "少数股东权益" in labels
    assert not [label for label in labels if label.startswith("计")]
    assert not [label for label in labels if label.endswith("合计计")]


def test_real_600276_section_locates_equity_and_minority_rows() -> None:
    """修复后归母权益与少数股东权益都能被表节定位到（恒等式的两个分量）。"""

    section = _section(REAL_600276_P76)
    equity = section.find("parent_equity")
    assert equity is not None, "归母权益行未定位到"
    assert _normalize_label(equity.label) == (
        "归属于母公司所有者权益(或股东权益)合计")
    assert equity.amount == Decimal("64466430205.71")
    minority = section.component("minority_equity")
    assert minority is not None, "少数股东权益行未定位到"
    assert _normalize_label(minority.label) == "少数股东权益"
    assert minority.amount == Decimal("523310211.35")
    total = section.component("total_equity")
    assert total is not None and total.amount == Decimal("64989740417.06")


def test_complete_equity_row_is_never_extended() -> None:
    """本来就完整的行不许被接字：这是上一版判定切坏两条行的地方。"""

    assert _labels(COMPLETE_EQUITY_THEN_NEXT_ROW) == [
        "归属于母公司所有者权益（或股东权益）合计",
        "少数股东权益",
        "所有者权益（或股东权益）合计",
    ]


def test_two_different_adjacent_rows_are_left_alone() -> None:
    """两行本来就是不同科目：没有任何一条规则可以把它们粘起来。"""

    assert _labels([
        "未分配利润 七、60 43,312,733,571.33 40,173,354,828.03",
        "少数股东权益 523,310,211.35 524,754,772.84",
    ]) == ["未分配利润 七、60", "少数股东权益"]


def test_rule_does_not_steal_a_character_from_a_real_row_label() -> None:
    """``险组``/``合计``是两条真实附注行：拼接结果不是术语，规则必须沉默。"""

    assert _resplit_wrapped_tail(REAL_000651_P127) == REAL_000651_P127


def test_known_label_check_is_whole_label_not_prefix() -> None:
    """判定必须是**整词**：前缀命中会让完整的行被多切一个字。"""

    for exact in (
        "归属于母公司所有者权益（或股东权益）合计",
        "归属于母公司股东权益合计",
        "归属于母公司所有者权益合计",
        "所有者权益（或股东权益）合计",
        "少数股东权益",
        "净利润",
        "五、净利润（净亏损以“—”号填列）",
        "归属于母公司所有者的净利润（净亏损以“—”号填列）",
        "其中：营业收入",
        "经营活动产生的现金流量净额",
    ):
        assert _matches_component_label(exact), f"应判为已知术语: {exact!r}"
    for extended in (
        # 上一版判定的漏洞：这些都被 ``_field_matches`` 的前缀判断放行了。
        "归属于母公司所有者权益（或股东权益）合计少",
        "净利润少数股东损益",
        "其中：营业收入营业总收入",
        # 不是术语的拼接。
        "险组合",
        "递延所得税负债计",
        "未分配利润 七、60",
    ):
        assert not _matches_component_label(extended), f"不该判为术语: {extended!r}"


def test_complete_attributable_profit_row_is_never_extended() -> None:
    """归母净利润行同样不许被接字。

    ``_COMPONENT_LABELS`` 里这一支结尾带 ``.*$``（为容纳被截断的括号部分，
    见该常量说明），因此整词性由"本行行名还不是术语才动字"这条守卫承担。
    """

    assert _labels([
        "归属于母公司所有者的净利润（净亏损以“—”号填列） 3,428,132,358.04 "
        "3,000,000,000.00",
        "少数股东损益（净亏损以“—”号填列） 186,498,915.07 -1,298,364,690.59",
    ]) == [
        "归属于母公司所有者的净利润（净亏损以“—”号填列）",
        "少数股东损益（净亏损以“—”号填列）",
    ]


def test_recovery_is_idempotent_on_the_real_page() -> None:
    """复原结果再跑一遍不许变：否则"补上的字"会被下一次判定再偷走。"""

    once = _dewrapped([(76, item) for item in REAL_600276_P76])
    assert _dewrapped(once) == once


def test_pages_are_never_crossed() -> None:
    """跨页的相邻两行不许拼接：换页处没有版式证据。"""

    lines = [
        (75, "权益（或股东权益）合 64,466,430,205.71 61,272,068,641.41"),
        (76, "计少数股东权益 523,310,211.35 524,754,772.84"),
    ]
    assert _resplit_wrapped_tail(lines) == lines
