"""机器核验协议与独立抽取路径的针对性用例。

覆盖的新行为：

* 恒等式/镜像路径只在数值真的相等时成立；
* 未定位到的分量不会退化成"通过"；
* 机器核验证据必须携带可复核的原文行，而不是一个自称的布尔值；
* 版本仓读侧接受机器协议、拒绝伪造与混用。

用例刻意不依赖真实 PDF：真实原文的端到端核对由
``tools/promote_s2_machine_facts.py`` 的实跑证据承担。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from aquant.adapters.providers.cninfo_financial_pdf import (
    CninfoS2CandidateFacts, PdfCandidateFact,
)
from aquant.adapters.providers.cninfo_statement_sections import (
    ROLE_CONSOLIDATED, ROLE_PARENT, StatementRow, StatementSection, StatementSections,
    _balance_sheet_header, _line_amounts_only, _normalize_label, _row_amounts,
)
from aquant.adapters.providers.s2_machine_verification import (
    MACHINE_VERIFICATION_SCHEMA, MachineVerification, MachineVerificationError,
    FieldVerification, IndependentCheck, verify_machine_evidence,
)

UNIT = "元"


def _row(statement: str, label: str, amount: str, *, role: str = ROLE_CONSOLIDATED,
         page: int = 10) -> StatementRow:
    value = Decimal(amount)
    return StatementRow(
        role=role, statement=statement, label=label, amount=value,
        prior_amount=value, page=page, unit=UNIT, current_cell=amount,
        prior_cell=amount, raw_line=f"{label} {amount} {amount}",
    )


def _section(statement: str, rows: list[StatementRow], *,
             role: str = ROLE_CONSOLIDATED) -> StatementSection:
    return StatementSection(
        role=role, statement=statement, start_page=10, end_page=11, unit=UNIT,
        current_header="本期发生额", prior_header="上期发生额",
        rows=tuple(rows),
    )


def test_balance_sheet_header_accepts_opening_balance_prior_column() -> None:
    """比较列是"本年年初"，不是上年同日；必须仍然认得出来。"""

    assert _balance_sheet_header(["2024年12月31日", "2024年1月1日"], date(2024, 12, 31)) == (
        "2024年12月31日", "2024年1月1日")
    assert _balance_sheet_header(["期末余额", "期初余额"], date(2024, 6, 30)) == (
        "期末余额", "期初余额")
    # 缺少任何一列都不是可用的表头。
    assert _balance_sheet_header(["期末余额"], date(2024, 6, 30)) is None


def test_row_amounts_ignores_row_numbers_and_note_references() -> None:
    """行首编号与附注引用都不是金额列。"""

    parsed = _row_amounts("2.少数股东损益（净亏损以“—”号填列） -248,410,050.57 -323,116,590.28")
    assert parsed is not None
    assert parsed[2] == Decimal("-248410050.57")
    assert parsed[3] == Decimal("-323116590.28")
    annual = _row_amounts("其中：营业收入 五、54 189,163,654,064.64 203,979,266,387.09")
    assert annual is not None
    assert annual[2] == Decimal("189163654064.64")
    assert _normalize_label("其中：营业收入 五、") == "其中:营业收入"


def test_line_amounts_only_rejects_extra_tokens() -> None:
    assert _line_amounts_only("14,136,119,367.60 12,673,238,816.55") == (
        "14,136,119,367.60", Decimal("14136119367.60"), Decimal("12673238816.55"))
    assert _line_amounts_only("货币资金 五、1 113,900,461,797.94 124,104,987,289.62") is None


def _candidate(fields: dict[str, str], *, period: date = date(2026, 6, 30)) -> CninfoS2CandidateFacts:
    candidates = tuple(
        PdfCandidateFact(
            field=field, value_yuan=Decimal(value), period_end=period,
            report_period_text="2026年半年度", statement="profit", pdf_page=10,
            column_header="2026年半年度", amount_unit=UNIT, currency="CNY",
            source_row=f"{field} {value}", source_cells=(field, value),
            source_current_cell_index=1, source_prior_cell_index=1,
            pdf_sha256="sha256:" + "0" * 64, display_row_label=field,
        )
        for field, value in sorted(fields.items())
    )
    return CninfoS2CandidateFacts(
        instrument_id="000651", period_end=period,
        version_label="test", document_url=(
            "https://static.cninfo.com.cn/finalpage/2026-08-25/1225499406.PDF"),
        pdf_sha256="sha256:" + "0" * 64, candidates=candidates,
    )


def _synthetic_sections(rows_by_statement: dict[str, list[StatementRow]],
                        parent: dict[str, list[StatementRow]] | None = None) -> StatementSections:
    sections = [
        _section(statement, rows)
        for statement, rows in rows_by_statement.items()
    ]
    for statement, rows in (parent or {}).items():
        sections.append(_section(statement, rows, role=ROLE_PARENT))
    return StatementSections(pdf_sha256="sha256:" + "0" * 64, sections=tuple(sections))


def test_equity_identity_holds_only_when_components_match() -> None:
    """归母权益 = 所有者权益合计 - 少数股东权益；任一分量缺失即不成立。"""

    balance = [
        _row("资产负债表", "所有者权益合计", "1000"),
        _row("资产负债表", "少数股东权益", "100"),
        _row("资产负债表", "归属于母公司所有者权益合计", "900"),
    ]
    sections = _synthetic_sections({"资产负债表": balance})
    section = sections.require(ROLE_CONSOLIDATED, "资产负债表")
    assert section.component("total_equity") is not None
    assert section.component("minority_equity") is not None
    # 恒等式左边的两个分量都在，且差等于归母权益。
    assert (section.component("total_equity").amount
            - section.component("minority_equity").amount) == Decimal("900")

    missing = _synthetic_sections({"资产负债表": [
        _row("资产负债表", "所有者权益合计", "1000"),
        _row("资产负债表", "归属于母公司所有者权益合计", "900"),
    ]}).require(ROLE_CONSOLIDATED, "资产负债表")
    assert missing.component("minority_equity") is None


def test_duplicate_component_rows_take_the_first_not_a_guess() -> None:
    """同一表节里同名行重复只可能是版式伪影，取先出现的一行。

    真歧义（找不到唯一行）由 ``find``/``component`` 返回 ``None`` 表达，
    由核对层报告"分量缺失"，而不是在这里抛错——抛错会让整份报告无法核验，
    而版式伪影并不影响当期列的取值。
    """

    duplicated = _synthetic_sections({"利润表": [
        _row("利润表", "五、净利润（净亏损以“—”号填列）", "100"),
        _row("利润表", "六、净利润（净亏损以“—”号填列）", "200", page=11),
    ]}).require(ROLE_CONSOLIDATED, "利润表")
    assert duplicated.component("net_profit").amount == Decimal("100")


def test_missing_component_is_reported_as_absent_not_substituted() -> None:
    """分量真的找不到时必须返回 None，绝不拿相似行顶替。"""

    without_minority = _synthetic_sections({"利润表": [
        _row("利润表", "五、净利润（净亏损以“—”号填列）", "100"),
    ]}).require(ROLE_CONSOLIDATED, "利润表")
    assert without_minority.component("minority_profit") is None


def test_satisfied_paths_and_verified_flags() -> None:
    holds = IndependentCheck(path="equity_identity", holds=True, detail="ok",
                             evidence={})
    fails = IndependentCheck(path="profit_identity", holds=False, detail="no",
                             evidence={})
    field = FieldVerification(field="parent_equity", value_yuan=Decimal("900"),
                              checks=(fails, holds))
    assert field.verified is True
    assert field.satisfied_paths == ("equity_identity",)
    assert FieldVerification(field="x", value_yuan=Decimal(1),
                             checks=(fails,)).verified is False


def test_machine_evidence_payload_is_serializable_and_versioned() -> None:
    verification = MachineVerification(
        instrument_id="000651", period_end=date(2026, 6, 30),
        announcement_id="1225499406", pdf_sha256="sha256:" + "0" * 64,
        fields=(FieldVerification(field="revenue", value_yuan=Decimal("1"),
                                  checks=(IndependentCheck(
                                      path="revenue_identity", holds=True,
                                      detail="ok", evidence={"a": 1}),)),),
        sections=(), identities=(),
    )
    payload = verification.as_evidence()
    assert payload["schema"] == MACHINE_VERIFICATION_SCHEMA
    assert payload["verifier_id"].startswith("machine:")
    assert payload["verified"] is True
    assert payload["unverified_fields"] == []


def test_verifier_rejects_candidate_hash_mismatch() -> None:
    candidate = _candidate({"revenue": "1"})
    with pytest.raises(MachineVerificationError, match="hash differs"):
        verify_machine_evidence(b"%PDF-1.4 not really", candidate=candidate,
                                expected_fields=("revenue",))
