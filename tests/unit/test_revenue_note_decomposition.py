"""附注收入分解路径（``revenue_note_decomposition``）的针对性用例。

覆盖的新行为：

* 合并附注的『主营业务 + 其他业务 = 合计』真的等于候选收入时才成立；
* 分解明细不等于候选、或附注与利润表在同一页时**不成立**；
* 附注缺失、只有母公司附注、『其他业务』行缺失时报告"不成立"并说明原因，
  绝不回退到利润表数值；
* 版本仓读侧接受这条新路径，同时拒绝把三行写成同一处原文、让附注落在利润表
  页上、或携带算不平的金额。

用例刻意不依赖真实 PDF：解析函数作用在 ``(页码, 原文行)`` 列表上；真实原文的
端到端核对由 ``tmp/probe_targets.py`` 的实跑证据承担。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from aquant.adapters.providers import s2_machine_verification as verification
from aquant.adapters.providers.cninfo_financial_pdf import PdfCandidateFact
from aquant.adapters.providers.cninfo_statement_sections import (
    RevenueNoteNotLocatedError,
    _select_revenue_note_decomposition,
    extract_revenue_note_decomposition,
)
from aquant.adapters.providers.s2_machine_verification import (
    MACHINE_VERIFICATION_SCHEMA,
    _revenue_note_decomposition_check,
)
from aquant.domain.fundamentals.fact_repository import (
    _MACHINE_VERIFICATION_PATHS, _machine_path_is_independent,
    FactReviewBundle, FinancialFactRepository,
)
from aquant.domain.fundamentals.versioned import (
    FinancialFact, StatementScope,
)

PERIOD = date(2024, 6, 30)
DIGEST = "sha256:" + "0" * 64
ANNOUNCEMENT = "1221015306"
CONTENT_HASH = "sha256:" + "a" * 64
UTC = timezone.utc
FIRST_SEEN = datetime(2024, 8, 17, 4, 10, tzinfo=UTC)
INGESTED = datetime(2024, 8, 18, 12, 0, tzinfo=UTC)
AVAILABLE = datetime(2024, 8, 19, 0, 45, tzinfo=UTC)
PUBLISHED = date(2024, 8, 17)
#: 合成数据里的合并收入（元）与它所在页；利润表候选刻意放在另一页。
CONSOLIDATED_REVENUE = Decimal("1000.00")
NOTE_PAGE = 100
INCOME_PAGE = 43


def _synthetic_lines(
    *, unit: str = "元", columns: str = "本期发生额 上年同期发生额(已重述)",
    main: str = "900.00 400.00 800.00 300.00",
    other: str | None = "100.00 50.00 90.00 40.00",
    total: str = "1,000.00 450.00 890.00 340.00",
    extras: tuple[str, ...] = (), next_note: tuple[str, ...] = (),
    consolidated: bool = True, parent: bool = True,
) -> list[tuple[int, str]]:
    """002415 版式的合成原文：合并段与母公司段各有一张收入分解表。

    母公司合计刻意取 250.00，与合并 1,000.00 相差四倍——正是 002415 2024H1
    两张表的关系（10,889,673,181.86 对 41,209,096,206.36）。
    """

    lines: list[tuple[int, str]] = []
    if consolidated:
        lines += [
            (NOTE_PAGE, "杭州海康威视数字技术股份有限公司2024年半年度报告"),
            (NOTE_PAGE, "(五) 合并财务报表项目注释 - 续"),
            (NOTE_PAGE, "46、 营业收入和营业成本"),
            (NOTE_PAGE, "(1) 营业收入和营业成本情况"),
            (NOTE_PAGE, f"单位：{unit}"),
            (NOTE_PAGE, columns),
            (NOTE_PAGE, "项目"),
            (NOTE_PAGE, "收入 成本 收入 成本"),
            (NOTE_PAGE, f"主营业务 {main}"),
        ]
        if other is not None:
            lines.append((NOTE_PAGE, f"其他业务 {other}"))
        for extra in extras:
            lines.append((NOTE_PAGE, extra))
        lines.append((NOTE_PAGE, f"合计 {total}"))
        lines += [(NOTE_PAGE + 1, line) for line in next_note]
    if parent:
        lines += [
            (130, "(十六) 母公司财务报表主要项目注释"),
            (130, "4、 营业收入、营业成本"),
            (130, f"单位：{unit}"),
            (130, columns),
            (130, "项目"),
            (130, "收入 成本 收入 成本"),
            (130, "主营业务 200.00 100.00 190.00 90.00"),
            (130, "其他业务 50.00 20.00 40.00 10.00"),
            (130, "合计 250.00 120.00 230.00 100.00"),
        ]
    return lines


def _note_from_lines(lines: list[tuple[int, str]], *,
                     default_unit: str | None = "元"):
    return _select_revenue_note_decomposition(
        lines, pdf_sha256=DIGEST, period_end=PERIOD, default_unit=default_unit)


def _item(value: str, *, page: int = INCOME_PAGE) -> PdfCandidateFact:
    return PdfCandidateFact(
        field="revenue", value_yuan=Decimal(value), period_end=PERIOD,
        report_period_text="2024年半年度", statement="profit", pdf_page=page,
        column_header="2024年半年度", amount_unit="元", currency="CNY",
        source_row=f"其中：营业收入 {value}", source_cells=("其中：营业收入", value),
        source_current_cell_index=1, source_prior_cell_index=1,
        pdf_sha256=DIGEST, display_row_label="其中：营业收入",
    )


def test_consolidated_note_decomposition_is_located_with_evidence() -> None:
    """合并附注被选中，母公司附注（合计 250.00）绝不参与。"""

    note = _note_from_lines(_synthetic_lines())
    assert note.role == "合并"
    assert note.heading == "46、营业收入和营业成本"
    assert note.heading_page == NOTE_PAGE
    assert note.unit == "元" and note.unit_source == "note"
    assert (note.current_header, note.prior_header) == ("本期发生额", "上年同期发生额")
    # 列序是"当期收入 当期成本 上期收入 上期成本"：amount 取当期收入。
    assert note.main_business.amount == Decimal("900.00")
    assert note.other_business.amount == Decimal("100.00")
    assert note.total.amount == CONSOLIDATED_REVENUE
    assert note.current_revenue_yuan == CONSOLIDATED_REVENUE
    assert note.main_business.amount + note.other_business.amount == note.total.amount
    # 三行都带页码与原始单元格，可供读侧复核。
    assert note.main_business.page == note.total.page == NOTE_PAGE
    assert {note.main_business.label, note.other_business.label, note.total.label} == {
        "主营业务", "其他业务", "合计"}


def test_company_note_heading_is_parent_scope_without_amount_selection() -> None:
    # Actual 002011 H126 calls the parent-only chapter 公司财务报表主要项目注释.
    lines = _synthetic_lines()
    lines = [(page, "十八、公司财务报表主要项目注释" if "母公司财务报表" in line else line)
             for page, line in lines]
    note = _note_from_lines(lines)
    assert note.role == "合并"
    assert note.current_revenue_yuan == CONSOLIDATED_REVENUE
    assert note.heading_page == NOTE_PAGE


def test_company_note_heading_requires_an_exact_chapter_title() -> None:
    from aquant.adapters.providers.cninfo_statement_sections import _consolidated_revenue_note_headings
    lines = [(1, "七、合并财务报表项目注释"),
             (2, "见十八、公司财务报表主要项目注释的相关说明"),
             (3, "46、营业收入和营业成本")]
    assert _consolidated_revenue_note_headings(lines) == [(2, "46、营业收入和营业成本")]


def test_later_split_rows_do_not_precede_the_first_complete_table() -> None:
    """实采000589H1：后续分解表拆行不得重排第一张完整收入表。"""
    lines = _synthetic_lines(parent=False)
    lines += [(NOTE_PAGE + 1, line) for line in (
        "营业收入、营业成本的分解信息：", "其他业务", "100.00 50.00")]
    note = _note_from_lines(lines)
    assert note.current_revenue_yuan == CONSOLIDATED_REVENUE
    assert note.other_business.page == NOTE_PAGE


def test_mixed_direct_and_split_rows_preserve_source_order() -> None:
    lines = _synthetic_lines(parent=False)
    position = next(i for i, (_, line) in enumerate(lines) if line.startswith("其他业务"))
    lines[position:position + 1] = [(NOTE_PAGE, "其他业务"),
                                  (NOTE_PAGE, "100.00 50.00 90.00 40.00")]
    # 四列拆行不属于已支持的两列配对，必须继续失败关闭。
    with pytest.raises(RevenueNoteNotLocatedError):
        _note_from_lines(lines)


def test_thousand_yuan_note_is_scaled_and_recorded() -> None:
    """千元附注按 1000 折算，并记录单位来源。

    金额写成带千分位的真实形状（实测 300750 ``406,785,222``）：不带分隔符的
    短整数会被金额词法当成附注编号排除，那不是本条要验证的行为。
    """

    note = _note_from_lines(_synthetic_lines(
        unit="千元", main="900,000 400,000 800,000 300,000",
        other="100,000 50,000 90,000 40,000",
        total="1,000,000 450,000 890,000 340,000"))
    assert note.unit == "千元" and note.unit_source == "note"
    assert note.current_revenue_yuan == Decimal("1000000000")


def test_unit_falls_back_to_statement_section_and_is_recorded() -> None:
    """附注整节不重印单位（实测 000651）时回退到报表单位，但必须记录来源。"""

    lines = [line for line in _synthetic_lines() if not line[1].startswith("单位：")]
    note = _note_from_lines(lines, default_unit="元")
    assert note.unit == "元" and note.unit_source == "statement_section"
    assert note.current_revenue_yuan == CONSOLIDATED_REVENUE
    # 两侧都没有单位声明时失败关闭，不假定"元"。
    with pytest.raises(RevenueNoteNotLocatedError, match="单位"):
        _note_from_lines(lines, default_unit=None)


def test_arithmetic_holds_only_when_components_match_the_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成立与否只看算术：真等于候选才 True，差一分钱也 False。"""

    note = _note_from_lines(_synthetic_lines())
    monkeypatch.setattr(
        verification, "extract_revenue_note_decomposition", lambda *a, **k: note)
    holds = _revenue_note_decomposition_check(
        pdf_bytes=b"%PDF-1.4", period_end=PERIOD, value=CONSOLIDATED_REVENUE,
        item=_item("1000.00"), income_unit="元")
    assert holds.holds is True
    assert holds.evidence["income_statement_page"] == INCOME_PAGE
    assert holds.evidence["main_business"]["label"] == "主营业务"
    assert holds.evidence["main_business"]["amount_yuan"] == "900.00"
    assert holds.evidence["total"]["current_cell"] == "1,000.00"

    misses = _revenue_note_decomposition_check(
        pdf_bytes=b"%PDF-1.4", period_end=PERIOD, value=Decimal("999.99"),
        item=_item("999.99"), income_unit="元")
    assert misses.holds is False
    assert "不符" in misses.detail


def test_note_on_the_income_statement_page_cannot_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """附注与利润表同页就不是"另一处原文"，即使算术相等也不成立。"""

    note = _note_from_lines(_synthetic_lines())
    monkeypatch.setattr(
        verification, "extract_revenue_note_decomposition", lambda *a, **k: note)
    check = _revenue_note_decomposition_check(
        pdf_bytes=b"%PDF-1.4", period_end=PERIOD, value=CONSOLIDATED_REVENUE,
        item=_item("1000.00", page=NOTE_PAGE), income_unit="元")
    assert check.holds is False
    assert "另一处原文" in check.detail


def test_absent_note_is_reported_without_falling_back_to_the_income_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """定位不到附注时报告不成立，且证据里不出现被核对的利润表数值。"""

    def _missing(*args, **kwargs):
        raise RevenueNoteNotLocatedError(
            "未在合并附注区间定位到『营业收入和营业成本』标题")

    monkeypatch.setattr(
        verification, "extract_revenue_note_decomposition", _missing)
    check = _revenue_note_decomposition_check(
        pdf_bytes=b"%PDF-1.4", period_end=PERIOD, value=CONSOLIDATED_REVENUE,
        item=_item("1000.00"), income_unit="元")
    assert check.holds is False
    assert "不回退到利润表数值" in check.detail
    # 只有候选自身被记录；没有制造任何"另一处原文"来顶替缺失的附注。
    assert set(check.evidence) == {"candidate"}


def test_parent_note_alone_is_never_used_as_the_consolidated_one() -> None:
    """只有母公司附注时失败关闭：宁可报告未证实，也不拿母公司数当合并数。"""

    with pytest.raises(RevenueNoteNotLocatedError, match="未在合并附注区间定位"):
        _note_from_lines(_synthetic_lines(consolidated=False))


def test_missing_other_business_row_fails_closed() -> None:
    """实测 601012 2024 年报版式：只有主营业务与合计，缺一列即失败关闭。"""

    with pytest.raises(RevenueNoteNotLocatedError, match="其他业务"):
        _note_from_lines(_synthetic_lines(other=None))


def test_duplicate_component_row_is_rejected() -> None:
    """分解行重复（主营业务→其他业务→其他业务→合计）时不得拼出一张假表。"""

    with pytest.raises(RevenueNoteNotLocatedError, match="主营业务→其他业务→合计"):
        _note_from_lines(_synthetic_lines(
            extras=("其他业务 10.00 5.00 9.00 4.00",)))


def test_rows_from_the_next_note_are_not_stitched_in() -> None:
    """下一条附注的同名行不得被接到本表上：标题行是硬边界。

    实测 000651 的 ``56、 利息收入、利息支出`` 小节里同样有金额行；越过标题
    继续扫描就可能拼出一张原文里并不存在的分解表。
    """

    with pytest.raises(RevenueNoteNotLocatedError, match="其他业务"):
        _note_from_lines(_synthetic_lines(
            other=None,
            next_note=("47、 其他业务明细", "其他业务 100.00 50.00 90.00 40.00",
                       "合计 100.00 50.00 90.00 40.00")))


def test_note_table_that_contradicts_itself_is_rejected() -> None:
    """合计不等于明细之和的表本身不可信，不得据此记功。"""

    with pytest.raises(RevenueNoteNotLocatedError, match="自身不成立"):
        _note_from_lines(_synthetic_lines(total="1,010.00 450.00 890.00 340.00"))


def test_reversed_period_columns_fail_closed() -> None:
    """比较列排在当期列之前时取到的会是上期数，必须失败关闭。"""

    with pytest.raises(RevenueNoteNotLocatedError, match="比较列排在当期列之前"):
        _note_from_lines(_synthetic_lines(columns="上期发生额 本期发生额"))


def test_three_column_row_is_not_guessed() -> None:
    """行内金额列数不是四列时失败关闭，不按"前两个大概是收入成本"去猜。"""

    with pytest.raises(RevenueNoteNotLocatedError, match="不是四列"):
        _note_from_lines(_synthetic_lines(main="900.00 400.00 800.00"))


def test_extractor_requires_pdf_bytes() -> None:
    """非 PDF 字节立即失败关闭，不静默返回空分解。"""

    with pytest.raises(Exception, match="PDF bytes"):
        extract_revenue_note_decomposition(
            b"not a pdf", period_end=PERIOD, default_unit="元")


# ---------------------------------------------------------------------------
# 版本仓读侧：新路径必须被接受，同时不能被伪造
# ---------------------------------------------------------------------------

def _row(label: str, cell: str, amount: str, *, page: int = NOTE_PAGE) -> dict:
    return {"role": "合并", "statement": "利润表附注", "label": label,
            "amount_yuan": amount, "page": page, "amount_unit": "元",
            "current_cell": cell, "raw_line": f"{label} {cell}"}


def _note_check() -> dict:
    return {
        "path": "revenue_note_decomposition", "description": "d", "holds": True,
        "detail": "d",
        "evidence": {
            "candidate": {"field": "revenue", "value_yuan": "1000.00",
                          "pdf_page": INCOME_PAGE, "amount_unit": "元",
                          "display_row_label": "其中：营业收入"},
            "note_section": {"role": "合并", "statement": "利润表附注",
                             "heading": "46、营业收入和营业成本",
                             "heading_page": NOTE_PAGE, "amount_unit": "元",
                             "unit_source": "note", "current_column": "本期发生额",
                             "prior_column": "上年同期发生额", "pdf_sha256": DIGEST,
                             "raw_lines": []},
            "main_business": _row("主营业务", "900.00", "900.00"),
            "other_business": _row("其他业务", "100.00", "100.00"),
            "total": _row("合计", "1,000.00", "1000.00"),
            "current_revenue_yuan": "1000.00",
            "income_statement_page": INCOME_PAGE,
        },
    }


def _fact() -> FinancialFact:
    identity = f"cninfo|002415|{PERIOD}|revenue|{ANNOUNCEMENT}|{CONTENT_HASH}"
    return FinancialFact(
        instrument_id="002415", metric="revenue", period_end=PERIOD,
        statement_scope=StatementScope.CONSOLIDATED, profit_scope=None,
        value=CONSOLIDATED_REVENUE, currency="CNY", raw_unit="yuan",
        source_id="cninfo", source_document_id=ANNOUNCEMENT,
        source_published_date=PUBLISHED, source_published_at=None,
        timestamp_precision="DATE", first_seen_at=FIRST_SEEN,
        ingested_at=INGESTED, available_at=AVAILABLE,
        availability_basis="OBSERVED", pit_mode="LIVE_OBSERVED",
        content_hash=CONTENT_HASH,
        version_id="mach_" + hashlib.sha256(identity.encode()).hexdigest()[:32],
    )


def _evidence(fact: FinancialFact, *, mutate=None) -> dict:
    evidence = {
        "schema": MACHINE_VERIFICATION_SCHEMA,
        "verifier_version": "s2-machine-verifier-v1",
        "verifier_id": "machine:s2-machine-verifier-v1",
        "human_review_claimed": None,
        "source_id": "cninfo",
        "rights_register_version": "2026-09-20",
        "instrument_id": "002415", "period_end": PERIOD.isoformat(),
        "announcement_id": ANNOUNCEMENT, "announcement_role": "ORIGINAL_REPORT",
        "source_published_date": PUBLISHED.isoformat(),
        "content_hash": CONTENT_HASH, "pdf_receipt_id": "rcpt_x",
        "org_lookup_receipt_id": "rcpt_y",
        "first_seen_at": FIRST_SEEN.isoformat(), "ingested_at": INGESTED.isoformat(),
        "available_at": AVAILABLE.isoformat(),
        "version_ids": [fact.version_id],
        "fields": [{
            "field": "revenue", "value_yuan": str(fact.value), "verified": True,
            "satisfied_paths": ["revenue_note_decomposition"],
            "checks": [_note_check()],
        }],
    }
    if mutate is not None:
        mutate(evidence)
    return evidence


def _accepted(db_path, evidence: dict, fact: FinancialFact) -> set[str]:
    repository = FinancialFactRepository(db_path)
    repository.append_many((fact,), review_bundle=FactReviewBundle(
        "cninfo_machine_" + hashlib.sha256(
            f"002415|{PERIOD}|{ANNOUNCEMENT}".encode()).hexdigest()[:32],
        json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"))))
    _, accepted = repository.load_with_accepted_s2_pdf_reviews()
    return set(accepted)


def test_path_is_registered_and_evidence_is_independent() -> None:
    """路径名必须在白名单里，且证据要通过独立性的形状与算术复核。"""

    assert "revenue_note_decomposition" in _MACHINE_VERIFICATION_PATHS
    fact = _fact()
    assert _machine_path_is_independent(_note_check(), fact) is True
    # 三行被写成同一处单元格：不是三处原文。
    same_cell = _note_check()
    same_cell["evidence"]["other_business"] = _row("其他业务", "900.00", "100.00")
    same_cell["evidence"]["total"] = _row("合计", "900.00", "1000.00")
    assert _machine_path_is_independent(same_cell, fact) is False
    # 附注落在利润表所在页：不是另一页。
    same_page = _note_check()
    same_page["evidence"]["income_statement_page"] = NOTE_PAGE
    assert _machine_path_is_independent(same_page, fact) is False
    # 算不平：合计与明细不符。
    unbalanced = _note_check()
    unbalanced["evidence"]["main_business"] = _row("主营业务", "800.00", "800.00")
    assert _machine_path_is_independent(unbalanced, fact) is False


def test_repository_reader_accepts_the_note_path(tmp_path) -> None:
    """版本仓读侧接受附注分解路径（这是它进入正式事实仓的准入条件）。"""

    fact = _fact()
    assert _accepted(tmp_path / "facts.sqlite", _evidence(fact), fact) == {fact.version_id}


def test_repository_reader_rejects_a_self_referential_note(tmp_path) -> None:
    """三行共用一处单元格、或附注与利润表同页时，读侧必须拒绝。"""

    fact = _fact()

    def shared_cell(evidence: dict) -> None:
        check = evidence["fields"][0]["checks"][0]
        check["evidence"]["other_business"] = _row("其他业务", "900.00", "100.00")
        check["evidence"]["total"] = _row("合计", "900.00", "1000.00")

    assert _accepted(tmp_path / "shared.sqlite",
                     _evidence(fact, mutate=shared_cell), fact) == set()

    def same_page(evidence: dict) -> None:
        check = evidence["fields"][0]["checks"][0]
        check["evidence"]["income_statement_page"] = NOTE_PAGE

    assert _accepted(tmp_path / "same-page.sqlite",
                     _evidence(fact, mutate=same_page), fact) == set()


def test_repository_reader_rejects_unbalanced_note_arithmetic(tmp_path) -> None:
    """读侧自己重算 主营业务 + 其他业务 = 合计 = 事实值，算不平即拒绝。"""

    fact = _fact()

    def unbalanced(evidence: dict) -> None:
        check = evidence["fields"][0]["checks"][0]
        check["evidence"]["main_business"] = _row("主营业务", "800.00", "800.00")

    assert _accepted(tmp_path / "unbalanced.sqlite",
                     _evidence(fact, mutate=unbalanced), fact) == set()
