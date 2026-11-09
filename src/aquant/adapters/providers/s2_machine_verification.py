"""有版本的机器核验协议：为官方原文金额生成可复核的独立证据。

背景：现有 v1/v2 准入协议要求 ``HUMAN_VISUAL`` 逐字段签署。主规格要求的是
**必要核验**，不是每个金额永远人工签字。本模块实现机器核验协议
``cninfo-s2-machine-verified-v1``，它做三件被明确要求、且可以被第三方复核的事：

1. **身份与期间**由官方索引与 PDF 封面共同确定（不是本模块重新猜）。
2. **列与表节**由表节定位器确定：合并/母公司表节分别定位，单位、当期列与
   表节边界都留证；母公司与合并是两个不同页区间，互为独立来源。
3. **独立路径核对**：每条正式金额至少要有一条**与主抽取路径不同**的路径
   证实，且必须是"数值相等"或"恒等式成立"，而不是同一个函数跑两遍。

被承认的独立路径：

``parent_statement_mirror``
    同一金额在**母公司表节**中的对应行。仅适用于合并与母公司必然相等的
    字段（归母权益、营业收入）；归母净利润、合并净利润与经营现金流在母
    公司口径下含义不同，**不允许**用这条路径。
``equity_identity``
    归母权益 = 所有者权益合计 - 少数股东权益。
``profit_identity``
    合并净利润 = 归母净利润 + 少数股东损益。
``cashflow_identity``
    经营现金流 = 现金及现金等价物净增加额 - 投资净额 - 筹资净额 - 汇率影响。
``cash_rollforward``
    期末现金及现金等价物余额 = 期初余额 + 现金及现金等价物净增加额。
``revenue_note_decomposition``
    合并附注『营业收入和营业成本情况』：主营业务 + 其他业务 = 合计 = 候选营业
    收入。附注与利润表既不是同一张表、也不在同一页，因此是另一处原文；只有在
    这个算术**真的成立**时才通过，附注定位不到就报告不成立，绝不回退到利润表
    数值（601012 2024 年报、300750 2025 年报、688981 2026 半年报的主表收入
    恒等式自相矛盾，002415 2024 半年报的主表没有『营业总收入』行）。

**不承认**的伪独立路径：把同一抽取函数跑两遍、把两站同哈希当作数值独立、
用比率反推金额、按"置信度"放行。

本模块只产出证据与结论；把事实写入版本仓由 ``pdf_promotion`` 的机器核验
入口完成。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .cninfo_financial_candidate import CninfoFinancialPdfError
from .cninfo_financial_pdf import CninfoS2CandidateFacts, PdfCandidateFact
from .cninfo_statement_geometry import extract_grid_statement_cells
from .dated_note_current_total import (
    DatedNoteCurrentTotalNotLocatedError,
    extract_dated_note_current_total,
)
from .profit_before_tax_less_income_tax import (
    ProfitTaxNotLocatedError,
    extract_profit_before_tax_less_income_tax_expense,
)
from .operating_cashflow_subtotals import (
    OperatingCashflowSubtotalsNotLocatedError,
    extract_operating_cashflow_subtotals,
)
from .parent_equity_native_closing import extract_parent_equity_native_closing_cell
from .parent_equity_comprehensive_income import (
    ParentComprehensiveIncomeNotLocated,
    extract_parent_comprehensive_income_minus_oci,
)
from .cninfo_statement_sections import (
    PARENT_MATCHING_FIELDS,
    ROLE_CONSOLIDATED,
    ROLE_PARENT,
    StatementRow,
    StatementSection,
    StatementSections,
    extract_revenue_note_decomposition,
    extract_statement_sections,
)


MACHINE_VERIFICATION_SCHEMA = "cninfo-s2-machine-verified-v1"
VERIFIER_VERSION = "s2-machine-verifier-v1"
#: 机器核验者身份。与人类 ``reviewer_id`` 分列存放，机器不得冒充人类签署。
VERIFIER_ID = "machine:s2-machine-verifier-v1"

_UNIT_SCALE = {"元": Decimal(1), "千元": Decimal(1000), "万元": Decimal(10000), "百万元": Decimal(1000000)}

#: 标准字段到承载它的合并表节。缺表时按此报告缺失，而不是静默跳过。
_STATEMENT_FOR_FIELD = {
    "parent_equity": "资产负债表",
    "revenue": "利润表",
    "net_profit_consolidated": "利润表",
    "net_profit_attributable": "利润表",
    "operating_cashflow": "现金流量表",
}

#: 每条独立路径的固定说明，写入证据供第三方复核方法本身。
PATH_DESCRIPTIONS = {
    "parent_statement_mirror": (
        "母公司表节同一行名承载同一实数值；两个表节位于不同页面区间"
    ),
    "equity_identity": "归母权益 = 所有者权益合计 - 少数股东权益",
    "profit_identity": "合并净利润 = 归母净利润 + 少数股东损益",
    "revenue_identity": (
        "营业收入 = 营业总收入 - 利息收入 - 已赚保费 - 手续费及佣金收入，"
        "且同一份原文的母公司营业收入不高于合并营业收入"
    ),
    "cashflow_identity": (
        "经营现金流 = 现金及现金等价物净增加额 - 投资净额 - 筹资净额 - 汇率影响"
    ),
    "cash_rollforward": (
        "期末现金及现金等价物余额 = 期初余额 + 净增加额"
        "（只核对现金余额滚动，不为经营现金流记功）"
    ),
    "revenue_note_decomposition": (
        "合并附注『营业收入和营业成本情况』：主营业务 + 其他业务 = 合计 = 候选"
        "营业收入；附注与利润表既不是同一张表、也不在同一页"
    ),
    "ruled_grid_cell_reextraction": (
        "独立按原始PDF格线、单元格几何及当期列头重新定位金额；不输入主抽取金额或列索引"
    ),
    "dated_note_current_total": (
        "独立按合并附注61自身标题、单位、本期/上期收入成本列、合计行及原生单元格定位"
    ),
    "profit_before_tax_less_income_tax": (
        "独立按合并利润表自身标题、单位、本期/上期列头及利润总额、所得税费用原生单元格，"
        "精确核对净利润 = 利润总额 - 所得税费用"
    ),
    "operating_cashflow_inflow_less_outflow_subtotals": (
        "独立按合并现金流量表本期/上期列头及经营活动现金流入、流出小计原生单元格，"
        "核对经营现金流 = 流入小计 - 流出小计"
    ),
    "native_parent_equity_change_table_closing_cell": (
        "独立按合并所有者权益变动表本期表头的归母所有者权益/小计列，"
        "读取本期期末余额行与该列对齐的原生单元格"
    ),
    "native_full_report_note_crosscheck": (
        "同一官方全文中从原生合并附注按当期列提取主值，再由不同页的原生附注或主要会计数据"
        "独立核对；不把扫描主表称为原生文本"
    ),
    "parent_equity_comprehensive_income_minus_oci": (
        "独立按合并所有者权益变动表本期表头，在同一“（一）综合收益总额”行读取"
        "归母所有者权益/小计与同组其他综合收益原生格，并核对归母净利润恒等式"
    ),
}


class MachineVerificationError(ValueError):
    """机器核验无法成立；相关字段必须进入异常队列，不得静默放行。"""


@dataclass(frozen=True, slots=True)
class IndependentCheck:
    """一条独立核对路径的结果。"""

    path: str
    holds: bool
    detail: str
    evidence: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "description": PATH_DESCRIPTIONS[self.path],
            "holds": self.holds,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class FieldVerification:
    """单个标准字段的核验结论。"""

    field: str
    value_yuan: Decimal
    checks: tuple[IndependentCheck, ...]

    @property
    def verified(self) -> bool:
        return any(check.holds for check in self.checks)

    @property
    def satisfied_paths(self) -> tuple[str, ...]:
        return tuple(check.path for check in self.checks if check.holds)

    def as_dict(self) -> dict[str, object]:
        return {
            "field": self.field,
            "value_yuan": str(self.value_yuan),
            "verified": self.verified,
            "satisfied_paths": list(self.satisfied_paths),
            "checks": [check.as_dict() for check in self.checks],
        }


@dataclass(frozen=True, slots=True)
class MachineVerification:
    """一份报告的机器核验结论。"""

    instrument_id: str
    period_end: date
    announcement_id: str
    pdf_sha256: str
    fields: tuple[FieldVerification, ...]
    sections: tuple[dict[str, object], ...]
    identities: tuple[IndependentCheck, ...]

    @property
    def verified(self) -> bool:
        return bool(self.fields) and all(field.verified for field in self.fields)

    @property
    def unverified_fields(self) -> tuple[str, ...]:
        return tuple(field.field for field in self.fields if not field.verified)

    def as_evidence(self) -> dict[str, object]:
        """可写入版本仓的规范证据载荷（JSON 可序列化）。"""

        return {
            "schema": MACHINE_VERIFICATION_SCHEMA,
            "verifier_version": VERIFIER_VERSION,
            "verifier_id": VERIFIER_ID,
            "instrument_id": self.instrument_id,
            "period_end": self.period_end.isoformat(),
            "announcement_id": self.announcement_id,
            "content_hash": self.pdf_sha256,
            "verified": self.verified,
            "unverified_fields": list(self.unverified_fields),
            "fields": [field.as_dict() for field in self.fields],
            "sections": list(self.sections),
            "identities": [check.as_dict() for check in self.identities],
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.as_evidence(),
            "verified_field_count": sum(1 for field in self.fields if field.verified),
            "field_count": len(self.fields),
        }


def _scaled(row: StatementRow) -> Decimal:
    if row.unit not in _UNIT_SCALE:
        raise MachineVerificationError(f"unsupported amount unit {row.unit!r}")
    return row.amount * _UNIT_SCALE[row.unit]


def _row_evidence(row: StatementRow | None) -> dict[str, object] | None:
    return None if row is None else row.as_evidence()


def _check_identity(
    path: str, terms: list[tuple[Decimal | None, int]], *, expected: Decimal,
    evidence: dict[str, object],
) -> IndependentCheck:
    """核对 ``sum(sign * value) == expected``；任一分量缺失即不成立。"""

    if any(value is None for value, _ in terms):
        return IndependentCheck(path=path, holds=False,
                                detail="one or more identity components are missing",
                                evidence=evidence)
    total = sum((value * sign for value, sign in terms if value is not None), Decimal(0))
    return IndependentCheck(
        path=path,
        holds=total == expected,
        detail=f"recomputed {total} vs extracted {expected}",
        evidence=evidence,
    )


def _section_evidence(sections: StatementSections) -> tuple[dict[str, object], ...]:
    rows = []
    for section in sections.sections:
        rows.append({
            "role": section.role,
            "statement": section.statement,
            "start_page": section.start_page,
            "end_page": section.end_page,
            "amount_unit": section.unit,
            "current_column": section.current_header,
            "prior_column": section.prior_header,
            "located_row_count": len(section.rows),
        })
    return tuple(rows)


def _safe_find(section: StatementSection, field: str) -> StatementRow | None:
    try:
        return section.find(field)
    except CninfoFinancialPdfError as exc:
        raise MachineVerificationError(f"{field}: ambiguous row: {exc}") from exc


def _revenue_identity(
    *, totals: dict[str, Decimal | None], value: Decimal,
    parent_row: StatementRow | None, evidence: dict[str, object],
) -> IndependentCheck:
    """收入恒等式 + 母公司口径上界，双重约束。

    * 合并``其中：营业收入``必须等于``营业总收入``减去声明存在的金融收入项。
      一般工商企业没有这些行，恒等式退化为"收入 = 营业总收入"。
    * 同一份原文的母公司营业收入不得高于合并营业收入。母公司是合并范围的
      子集，这条上界能挡住把母公司口径或比较列当成合并当期列的错误。

    至少两处不同原文单元格和精确扣减必须成立；单独的总额行不构成独立证明。
    """

    if totals["total_revenue"] is None:
        return IndependentCheck(
            path="revenue_identity", holds=False,
            detail=("合并利润表本身没有列示『营业总收入』行（收入以『营业收入』"
                    "为首行，实测 002415），扣减恒等式因此无法建立；这一项需要"
                    "附注的收入分解或交易所结构化数值作为第三方路径"),
            evidence=evidence)
    source_rows = [row for row in evidence.values() if isinstance(row, dict)
                   and isinstance(row.get("page"), int) and row.get("current_cell")]
    positions = {(row["page"], row["current_cell"]) for row in source_rows}
    if len(source_rows) < 2 or len(positions) < 2:
        return IndependentCheck(
            path="revenue_identity", holds=False,
            detail="only one independent source cell: total revenue alone cannot prove the candidate revenue",
            evidence=evidence)
    deductions = [
        totals[key] for key in
        ("interest_income", "earned_premium", "fee_commission_income")
    ]
    present = [item for item in deductions if item is not None]
    deduction_total = sum(present, Decimal(0))
    if totals["total_revenue"] - deduction_total != value:
        # Missing components cannot justify a tolerance or an inferred amount.
        return IndependentCheck(
            path="revenue_identity", holds=False,
            detail=(f"营业总收入 {totals['total_revenue']} 减去金融收入项 "
                    f"{deduction_total} = {totals['total_revenue'] - deduction_total}，"
                    f"与候选 {value} 不符"),
            evidence=evidence)
    if parent_row is None:
        # 母公司表节缺失时无法施加口径上界。这在合并层面只披露合并报表的
        # 架构下是正常情形（实测 688981 半年报），因此不因此判负，而是在
        # detail 里说明本次仅有扣减恒等式支撑。
        return IndependentCheck(
            path="revenue_identity", holds=True,
            detail=f"扣减后等于 {value}；母公司利润表未列示，本次无口径上界",
            evidence=evidence)
    parent_value = _scaled(parent_row)
    return IndependentCheck(
        path="revenue_identity", holds=parent_value <= value,
        detail=(f"营业总收入扣减后等于 {value}；母公司营业收入 "
                f"{parent_value} {'<=' if parent_value <= value else '>'} {value}"),
        evidence=evidence)


def _revenue_note_decomposition_check(
    *, pdf_bytes: bytes, period_end: date, value: Decimal,
    item: PdfCandidateFact, income_unit: str | None,
) -> IndependentCheck:
    """合并附注收入分解：主营业务 + 其他业务 = 合计 = 候选营业收入。

    为什么需要这条路径（实测官方原文，主表都已自相矛盾）：

    * 601012 2024 年报：``营业总收入 = 82,582,273,118.72`` 与``其中：营业收入``
      完全相等，而``利息收入 = 950,045,457.25``；
    * 300750 2025 年报（千元）：``营业总收入 = 423,701,834`` 亦然，扣减项 10,594,657；
    * 688981 2026 半年报（千元）：``营业总收入 = 38,635,115`` 亦然，扣减项 912,210；
    * 002415 2024 半年报：合并利润表没有``营业总收入``行，收入以``营业收入``为首行。

    合并附注的收入分解表是同一份原文里另一张表、另一个页面，因此是真正的第二处
    原文。通过条件只有两条：分解算术等于候选，且附注页**不是**候选利润表所在页。
    附注定位不到时报告不成立并说明原因——**绝不**回退到利润表数值，那正是待核对
    的候选本身。
    """

    evidence: dict[str, object] = {
        "candidate": {
            "field": "revenue", "value_yuan": str(value),
            "pdf_page": item.pdf_page, "amount_unit": item.amount_unit,
            "display_row_label": item.display_row_label,
        },
    }
    try:
        note = extract_revenue_note_decomposition(
            pdf_bytes, period_end=period_end, default_unit=income_unit)
    except CninfoFinancialPdfError as exc:
        return IndependentCheck(
            path="revenue_note_decomposition", holds=False,
            detail=f"合并附注收入分解未定位（不回退到利润表数值）：{exc}",
            evidence=evidence)
    evidence.update(note.as_evidence())
    evidence["income_statement_page"] = item.pdf_page
    note_pages = {note.heading_page, note.main_business.page,
                  note.other_business.page, note.total.page}
    if item.pdf_page in note_pages:
        return IndependentCheck(
            path="revenue_note_decomposition", holds=False,
            detail=(f"附注分解与利润表候选同在 {item.pdf_page} 页，"
                    "不构成另一处原文"),
            evidence=evidence)
    recomputed = note.current_revenue_yuan
    holds = recomputed == value
    return IndependentCheck(
        path="revenue_note_decomposition", holds=holds,
        detail=(f"附注第 {note.heading_page} 页『{note.heading}』"
                f"（单位：{note.unit}，来源：{note.unit_source}）：主营业务 "
                f"{note.main_business.current_cell} + 其他业务 "
                f"{note.other_business.current_cell} = 合计 {note.total.current_cell}，"
                f"折合 {recomputed} 元，与候选 {value} 元"
                f"{'相符' if holds else '不符'}"),
        evidence=evidence)


def _dated_note_current_total_check(
    *, pdf_bytes: bytes, period_end: date, value: Decimal,
    item: PdfCandidateFact, instrument_id: str,
) -> IndependentCheck:
    """Read the note's own unique dated total; compare only after selection."""

    evidence: dict[str, object] = {
        "candidate": {
            "field": "revenue", "value_yuan": str(value),
            "pdf_page": item.pdf_page, "amount_unit": item.amount_unit,
            "display_row_label": item.display_row_label,
        },
    }
    try:
        note = extract_dated_note_current_total(
            pdf_bytes, instrument_id=instrument_id, period_end=period_end)
    except DatedNoteCurrentTotalNotLocatedError as exc:
        return IndependentCheck(
            path="dated_note_current_total", holds=False,
            detail=f"合并附注本期收入合计未唯一定位：{exc}",
            evidence=evidence)
    evidence.update(note.as_evidence())
    evidence["primary_comparison"] = {
        "primary_candidate_yuan": str(value),
        "note_current_total_yuan": str(note.current_revenue_yuan),
        "comparison": "performed only after the note cell was independently selected",
    }
    if note.current_revenue_yuan != value:
        raise MachineVerificationError(
            f"revenue: independent dated note total {note.current_revenue_yuan} "
            f"differs from primary candidate {value}")
    if note.current_revenue_cell.page == item.pdf_page:
        return IndependentCheck(
            path="dated_note_current_total", holds=False,
            detail=("附注合计与主候选同在 "
                    f"{item.pdf_page} 页，不构成独立原文位置"),
            evidence=evidence)
    return IndependentCheck(
        path="dated_note_current_total", holds=True,
        detail=(f"合并附注第 {note.note_heading.page} 页自身表格的"
                f"本期发生额收入合计 = {note.current_revenue_yuan} 元"),
        evidence=evidence)


def _profit_before_tax_less_income_tax_check(
    *, pdf_bytes: bytes, period_end: date, value: Decimal,
    item: PdfCandidateFact, instrument_id: str, section: StatementSection,
) -> IndependentCheck:
    """Use the consolidated P&L's own native PBT and tax cells as a last path."""

    evidence: dict[str, object] = {
        "candidate": {
            "field": "net_profit_consolidated", "value_yuan": str(value),
            "pdf_page": item.pdf_page, "amount_unit": item.amount_unit,
            "display_row_label": item.display_row_label,
        },
    }
    try:
        proof = extract_profit_before_tax_less_income_tax_expense(
            pdf_bytes, instrument_id=instrument_id, period_end=period_end,
            section=section,
        )
    except ProfitTaxNotLocatedError as exc:
        return IndependentCheck(
            path="profit_before_tax_less_income_tax", holds=False,
            detail=f"合并利润表的利润总额/所得税费用原生单元格未唯一定位：{exc}",
            evidence=evidence)
    evidence.update(proof.evidence)
    evidence["primary_comparison"] = {
        "primary_candidate_yuan": str(value),
        "independent_current_recomputed_yuan": str(proof.current_recomputed_yuan),
        "comparison": "performed only after native PBT and tax cells were independently selected",
    }
    if proof.current_recomputed_yuan != value:
        raise MachineVerificationError(
            "net_profit_consolidated: independent profit-before-tax less income-tax "
            f"amount {proof.current_recomputed_yuan} differs from primary candidate {value}")
    return IndependentCheck(
        path="profit_before_tax_less_income_tax", holds=True,
        detail=("合并利润表本期利润总额 - 所得税费用 = "
                f"{proof.current_recomputed_yuan} 元，与候选相符；"
                f"上期同式 = {proof.prior_recomputed_yuan} 元"),
        evidence=evidence)



def verify_machine_evidence(
    pdf_bytes: bytes, *,
    candidate: CninfoS2CandidateFacts,
    expected_fields: tuple[str, ...],
) -> MachineVerification:
    """对一份已抽取候选做独立核对，返回带版本的机器核验证据。

    任何定位歧义都失败关闭：宁可让该字段进入异常队列，也不放行一条无法
    被第三方用同样方法复核的结论。
    """

    if candidate.pdf_sha256 != "sha256:" + hashlib.sha256(pdf_bytes).hexdigest():
        raise MachineVerificationError("candidate hash differs from the supplied PDF bytes")
    fields = tuple(sorted(expected_fields))
    if len(fields) != len(set(fields)) or not set(fields) <= set(candidate.by_field):
        raise MachineVerificationError("expected fields must be unique candidate fields")
    if candidate.version_label == "spring_native_consolidated_note_candidate_v1":
        from .spring_native_notes import verify_native_note_evidence
        return verify_native_note_evidence(
            pdf_bytes, candidate=candidate, expected_fields=fields,
        )
    if candidate.version_label == "native_consolidated_notes_v1":
        from .chifeng_native_notes import verify_native_note_evidence
        return verify_native_note_evidence(
            pdf_bytes, candidate=candidate, expected_fields=fields,
        )
    all_statement_scope = ("资产负债表", "利润表", "现金流量表")
    try:
        # Keep the established full-section parse as the primary path. Existing
        # complete proofs are unchanged whenever this succeeds.
        sections = extract_statement_sections(pdf_bytes, period_end=candidate.period_end)
    except CninfoFinancialPdfError as exc:
        required_statements = {_STATEMENT_FOR_FIELD[field] for field in fields}
        if not required_statements or required_statements == set(all_statement_scope):
            raise MachineVerificationError(f"statement sections are not locatable: {exc}") from exc
        scoped_statements = tuple(
            statement for statement in all_statement_scope
            if statement in required_statements
        )
        try:
            # Retry only the tables required by the caller's real field subset.
            # Each selected section still parses its complete rows and all
            # identity components used below; a required ambiguous table fails.
            sections = extract_statement_sections(
                pdf_bytes, period_end=candidate.period_end,
                statements=scoped_statements,
            )
        except CninfoFinancialPdfError as scoped_exc:
            raise MachineVerificationError(
                f"required statement sections are not locatable: {scoped_exc}"
            ) from scoped_exc

    # 恒等式分量与"本报告需要哪些字段"**无关**：它们是被核对值的另一处原文
    # 证据，只按 expected_fields 去定位会让必需字段变少时核对整批消失
    # （例如某期只需要 revenue 时，营业总收入/利息收入就永远定位不到）。
    # 因此这里无条件定位全部恒等式分量，缺哪张表就在需要时报告缺失。
    consolidated = {}
    for statement in ("资产负债表", "利润表", "现金流量表"):
        section = sections.get(ROLE_CONSOLIDATED, statement)
        if section is not None:
            consolidated[statement] = section
    missing_statements = sorted(
        {_STATEMENT_FOR_FIELD[field] for field in fields} - set(consolidated))
    if missing_statements:
        raise MachineVerificationError(
            "consolidated statement sections are not locatable: "
            + ", ".join(missing_statements))
    # "合并及公司…"表的列分组由行解析负责（``scope_columns=4``，只取最左两列），
    # 因此这里不再因为 combined 而拒绝；但必须确认**行解析确实按四列处理**，
    # 否则就可能又把公司列当成合并列。这一层是对"静默取错口径"的兜底。
    misread = sorted(
        statement for statement, section in consolidated.items()
        if getattr(section, "combined", False)
        and getattr(section, "scope_columns", 2) != 4
        and statement in {_STATEMENT_FOR_FIELD[field] for field in fields})
    if misread:
        raise MachineVerificationError(
            "consolidated statements are disclosed together with the company scope "
            "(\"合并及公司…\") but their rows were read as two-column tables: "
            + ", ".join(misread))
    balance = consolidated.get("资产负债表")
    income = consolidated.get("利润表")
    cashflow = consolidated.get("现金流量表")

    def component(section, key: str) -> Decimal | None:
        if section is None:
            return None
        try:
            row = section.component(key)
        except CninfoFinancialPdfError as exc:
            raise MachineVerificationError(
                f"{section.role}{section.statement}/{key}: ambiguous: {exc}") from exc
        return None if row is None else _scaled(row)

    def component_row(section, key: str) -> StatementRow | None:
        if section is None:
            return None
        try:
            return section.component(key)
        except CninfoFinancialPdfError as exc:
            raise MachineVerificationError(
                f"{section.role}{section.statement}/{key}: ambiguous: {exc}") from exc

    # 恒等式分量一次性定位，四条恒等式共享，避免重复解析同一表节。
    totals = {
        "total_equity": component(balance, "total_equity"),
        "minority_equity": component(balance, "minority_equity"),
        "net_profit": component(income, "net_profit"),
        "minority_profit": component(income, "minority_profit"),
        "total_revenue": component(income, "total_revenue"),
        "interest_income": component(income, "interest_income"),
        "earned_premium": component(income, "earned_premium"),
        "fee_commission_income": component(income, "fee_commission_income"),
        "operating_cashflow": component(cashflow, "operating_cashflow"),
        "investing_cashflow": component(cashflow, "investing_cashflow"),
        "financing_cashflow": component(cashflow, "financing_cashflow"),
        "fx_effect": component(cashflow, "fx_effect"),
        "net_increase_cash": component(cashflow, "net_increase_cash"),
        "ending_cash": component(cashflow, "ending_cash"),
        "beginning_cash": component(cashflow, "beginning_cash"),
    }

    def consolidated_row(field: str) -> StatementRow | None:
        try:
            if field == "parent_equity":
                return None if balance is None else balance.find(field)
            if field in ("revenue", "net_profit_attributable", "net_profit_consolidated"):
                return None if income is None else income.find(field)
            return None if cashflow is None else cashflow.find(field)
        except CninfoFinancialPdfError as exc:
            raise MachineVerificationError(f"{field}: ambiguous row: {exc}") from exc

    attributable_row = consolidated_row("net_profit_attributable")
    attributable = None if attributable_row is None else _scaled(attributable_row)

    identity_evidence = {
        "equity": {
            "total_equity": _row_evidence(component_row(balance, "total_equity")),
            "minority_equity": _row_evidence(component_row(balance, "minority_equity")),
        },
        "profit": {
            "net_profit": _row_evidence(component_row(income, "net_profit")),
            "minority_profit": _row_evidence(component_row(income, "minority_profit")),
            "attributable_profit": _row_evidence(attributable_row),
        },
        "cashflow": {
            key: _row_evidence(component_row(cashflow, key))
            for key in ("operating_cashflow", "investing_cashflow", "financing_cashflow",
                        "fx_effect", "net_increase_cash")
        },
        "cash_rollforward": {
            key: _row_evidence(component_row(cashflow, key))
            for key in ("beginning_cash", "net_increase_cash", "ending_cash")
        },
        "revenue": {
            key: _row_evidence(component_row(income, key))
            for key in ("total_revenue", "interest_income", "earned_premium",
                        "fee_commission_income")
        },
    }
    parent_income = sections.get(ROLE_PARENT, "利润表")
    identity_evidence["revenue"]["parent_revenue"] = _row_evidence(
        None if parent_income is None else parent_income.find("revenue"))

    identities: list[IndependentCheck] = []
    equity_identity_expected: Decimal | None = None
    if totals["total_equity"] is not None and totals["minority_equity"] is not None:
        equity_identity_expected = totals["total_equity"] - totals["minority_equity"]

    results: list[FieldVerification] = []
    for field in fields:
        item: PdfCandidateFact = candidate.by_field[field]
        value = Decimal(str(item.value_yuan))
        checks: list[IndependentCheck] = []

        if field in PARENT_MATCHING_FIELDS:
            statement = "资产负债表" if field == "parent_equity" else "利润表"
            parent = sections.get(ROLE_PARENT, statement)
            parent_row = None
            if parent is not None:
                try:
                    parent_row = parent.find(field)
                except CninfoFinancialPdfError as exc:
                    raise MachineVerificationError(
                        f"parent {statement}/{field}: ambiguous: {exc}") from exc
            holds = parent_row is not None and _scaled(parent_row) == value
            checks.append(IndependentCheck(
                path="parent_statement_mirror",
                holds=holds,
                detail=("母公司表节未定位到同一行" if parent_row is None else
                        f"母公司表节 {_scaled(parent_row)} vs 合并候选 {value}"),
                evidence={
                    "candidate": {
                        "field": field, "value_yuan": str(value),
                        "pdf_page": item.pdf_page, "amount_unit": item.amount_unit,
                        "display_row_label": item.display_row_label,
                    },
                    "parent_row": _row_evidence(parent_row),
                },
            ))

        if field == "parent_equity":
            check = _check_identity(
                "equity_identity",
                [(totals["total_equity"], 1), (totals["minority_equity"], -1)],
                expected=value, evidence=identity_evidence["equity"])
            checks.append(check)
        elif field == "net_profit_attributable":
            check = _check_identity(
                "profit_identity",
                [(totals["net_profit"], 1), (totals["minority_profit"], -1)],
                expected=value, evidence=identity_evidence["profit"])
            checks.append(check)
        elif field == "net_profit_consolidated":
            if attributable is not None and totals["minority_profit"] is not None:
                recomputed = attributable + totals["minority_profit"]
                checks.append(IndependentCheck(
                    path="profit_identity", holds=recomputed == value,
                    detail=f"归母净利润 + 少数股东损益 = {recomputed} vs {value}",
                    evidence=identity_evidence["profit"]))
            else:
                checks.append(IndependentCheck(
                    path="profit_identity", holds=False,
                    detail="归母净利润或少数股东损益缺失",
                    evidence=identity_evidence["profit"]))
        elif field == "operating_cashflow":
            terms = [
                (totals["net_increase_cash"], 1),
                (totals["investing_cashflow"], -1),
                (totals["financing_cashflow"], -1),
            ]
            # A missing/blank FX cell is not an observed zero. Keep it absent
            # so this identity fails and an actual independent grid path can
            # be tried. An explicit source 0 remains a real Decimal(0).
            terms.append((totals["fx_effect"], -1))
            checks.append(_check_identity(
                "cashflow_identity", terms, expected=value,
                evidence=identity_evidence["cashflow"]))
            # 注意：``cash_rollforward`` **不能**为 operating_cashflow 记功。
            # 它核对的是"期初 + 净增加 = 期末"，与经营现金流不相等，把它算作
            # 经营现金流的独立路径会制造假通过（实测 600519 2025H1：
            # 现金净增加额减投资净额再减筹资净额 = 13,122,337,994.31，
            # 而经营现金流为 13,119,061,031.33，两者并不相等）。
            # 它作为独立的现金余额滚动核对保留在 identities 里。
        elif field == "revenue":
            checks.append(_revenue_identity(
                totals=totals, value=value,
                parent_row=(None if parent_income is None
                            else _safe_find(parent_income, "revenue")),
                evidence=identity_evidence["revenue"]))
            # 主表恒等式在部分公司自相矛盾（见 ``_revenue_note_decomposition_check``
            # 的实测清单），因此无条件再走一次附注分解；定位不到只是这一条不成立，
            # 不会影响其它路径已经给出的结论。
            checks.append(_revenue_note_decomposition_check(
                pdf_bytes=pdf_bytes, period_end=candidate.period_end, value=value,
                item=item, income_unit=(income.unit or None)))
        results.append(FieldVerification(field=field, value_yuan=value,
                                         checks=tuple(checks)))

    if any(not result.verified for result in results):
        # Only reports needing another path incur this independent extraction.
        # A cell is found by geometry and semantics, never by matching the
        # candidate amount. A conflicting independently found cell is fatal.
        try:
            grid_cells = extract_grid_statement_cells(
                pdf_bytes, instrument_id=candidate.instrument_id,
                period_end=candidate.period_end)
        except Exception as exc:
            grid_cells = ()
            grid_failure = f"independent ruled-grid extraction unavailable: {type(exc).__name__}"
        else:
            grid_failure = "no unique, dated, consolidated ruled-grid cell"
        cells = {cell.field: cell for cell in grid_cells}
        updated = []
        for result in results:
            cell = cells.get(result.field)
            if cell is not None and cell.amount_yuan != result.value_yuan:
                raise MachineVerificationError(
                    f"{result.field}: independent ruled-grid amount {cell.amount_yuan} "
                    f"differs from primary candidate {result.value_yuan}")
            if result.verified:
                updated.append(result)
                continue
            check = IndependentCheck(
                path="ruled_grid_cell_reextraction", holds=cell is not None,
                detail=(grid_failure if cell is None else
                        f"independent current cell = {cell.amount_yuan} yuan"),
                evidence={} if cell is None else cell.as_evidence())
            updated.append(FieldVerification(result.field, result.value_yuan,
                                             (*result.checks, check)))
        results = updated

    # The dated note total is a separate typed path and runs only after the
    # legacy identity/decomposition and primary ruled-grid paths leave revenue
    # unresolved. Existing successful revenue fields retain their exact checks.
    updated = []
    for result in results:
        if result.field != "revenue" or result.verified:
            updated.append(result)
            continue
        item = candidate.by_field["revenue"]
        check = _dated_note_current_total_check(
            pdf_bytes=pdf_bytes, period_end=candidate.period_end,
            value=result.value_yuan, item=item,
            instrument_id=candidate.instrument_id)
        updated.append(FieldVerification(result.field, result.value_yuan,
                                         (*result.checks, check)))
    results = updated

    # This arithmetic path is last for consolidated net profit. It runs only
    # after both legacy identity and ruled-grid checks have failed, and does
    # not change either proof or its meaning.
    updated = []
    for result in results:
        if result.field != "net_profit_consolidated" or result.verified:
            updated.append(result)
            continue
        check = _profit_before_tax_less_income_tax_check(
            pdf_bytes=pdf_bytes, period_end=candidate.period_end,
            value=result.value_yuan,
            item=candidate.by_field["net_profit_consolidated"],
            instrument_id=candidate.instrument_id, section=income,
        )
        updated.append(FieldVerification(result.field, result.value_yuan,
                                         (*result.checks, check)))
    results = updated

    # This native closing-cell path is limited to unresolved parent equity.
    # The helper derives the table, header, cell geometry, and amount from the
    # same supplied PDF bytes; the legacy identity and grid evidence stay intact.
    updated = []
    for result in results:
        if result.field != "parent_equity" or result.verified:
            updated.append(result)
            continue
        try:
            proof = extract_parent_equity_native_closing_cell(
                pdf_bytes, period_end=candidate.period_end)
        except Exception as exc:
            check = IndependentCheck(
                path="native_parent_equity_change_table_closing_cell",
                holds=False,
                detail=("合并所有者权益变动表的归母/小计期末原格不可唯一定位："
                        f"{type(exc).__name__}: {exc}"),
                evidence={},
            )
        else:
            if proof.amount_yuan != result.value_yuan:
                raise MachineVerificationError(
                    "parent_equity: independent native closing subtotal "
                    f"{proof.amount_yuan} differs from primary candidate {result.value_yuan}")
            check = IndependentCheck(
                path="native_parent_equity_change_table_closing_cell",
                holds=True,
                detail=("合并所有者权益变动表归属于母公司所有者权益/小计列，"
                        f"四、本期期末余额原生单元格 = {proof.amount_yuan} 元"),
                evidence=proof.as_evidence(),
            )
        updated.append(FieldVerification(result.field, result.value_yuan,
                                         (*result.checks, check)))
    results = updated

    # This typed native-cell identity is the final fallback for unresolved
    # attributable net profit. It derives both components from the supplied
    # PDF bytes and period; the primary amount is compared only after selection.
    updated = []
    for result in results:
        if result.field != "net_profit_attributable" or result.verified:
            updated.append(result)
            continue
        try:
            proof = extract_parent_comprehensive_income_minus_oci(
                pdf_bytes, period_end=candidate.period_end)
        except ParentComprehensiveIncomeNotLocated as exc:
            check = IndependentCheck(
                path="parent_equity_comprehensive_income_minus_oci",
                holds=False,
                detail=("合并所有者权益变动表归母综合收益总额减其他综合收益的原格"
                        f"不可唯一定位：{exc}"),
                evidence={},
            )
        else:
            evidence = proof.as_evidence()
            evidence["primary_comparison"] = {
                "primary_candidate_yuan": str(result.value_yuan),
                "independent_current_recomputed_yuan": str(
                    proof.net_profit_attributable_yuan),
                "comparison": "performed only after both native cells were independently selected",
            }
            if proof.net_profit_attributable_yuan != result.value_yuan:
                raise MachineVerificationError(
                    "net_profit_attributable: independent parent comprehensive-income "
                    f"minus OCI amount {proof.net_profit_attributable_yuan} differs from "
                    f"primary candidate {result.value_yuan}")
            check = IndependentCheck(
                path="parent_equity_comprehensive_income_minus_oci",
                holds=True,
                detail=("合并所有者权益变动表同一综合收益行归母小计减同组其他综合收益"
                        f" = {proof.net_profit_attributable_yuan} 元，与候选相符"),
                evidence=evidence,
            )
        updated.append(FieldVerification(result.field, result.value_yuan,
                                         (*result.checks, check)))
    results = updated

    # The native subtotal pair is a final path for an unresolved operating
    # cash-flow candidate. Its helper receives no candidate amount or row; the
    # candidate is compared only after both source cells have been selected.
    updated = []
    for result in results:
        if result.field != "operating_cashflow" or result.verified:
            updated.append(result)
            continue
        try:
            proof = extract_operating_cashflow_subtotals(
                pdf_bytes,
                instrument_id=candidate.instrument_id,
                period_end=candidate.period_end,
                section=cashflow,
            )
        except OperatingCashflowSubtotalsNotLocatedError as exc:
            check = IndependentCheck(
                path="operating_cashflow_inflow_less_outflow_subtotals",
                holds=False,
                detail=("合并现金流量表经营活动流入/流出小计的本期原生格不可唯一定位："
                        f"{exc}"),
                evidence={},
            )
        else:
            evidence = dict(proof.evidence)
            evidence["primary_comparison"] = {
                "primary_candidate_yuan": str(result.value_yuan),
                "independent_current_recomputed_yuan":
                    str(proof.current_recomputed_yuan),
                "comparison": (
                    "performed only after both native subtotal cells were independently selected"
                ),
            }
            if proof.current_recomputed_yuan != result.value_yuan:
                raise MachineVerificationError(
                    "operating_cashflow: independent inflow-subtotal less "
                    f"outflow-subtotal amount {proof.current_recomputed_yuan} differs "
                    f"from primary candidate {result.value_yuan}")
            check = IndependentCheck(
                path="operating_cashflow_inflow_less_outflow_subtotals",
                holds=True,
                detail=("合并现金流量表本期经营活动现金流入小计 - "
                        f"现金流出小计 = {proof.current_recomputed_yuan} 元，与候选相符"),
                evidence=evidence,
            )
        updated.append(FieldVerification(result.field, result.value_yuan,
                                         (*result.checks, check)))
    results = updated

    identities.append(_check_identity(
        "equity_identity",
        [(totals["total_equity"], 1), (totals["minority_equity"], -1)],
        expected=(equity_identity_expected if equity_identity_expected is not None
                  else Decimal(0)),
        evidence=identity_evidence["equity"]))
    if attributable is not None:
        identities.append(_check_identity(
            "profit_identity",
            [(totals["net_profit"], 1), (totals["minority_profit"], -1)],
            expected=attributable, evidence=identity_evidence["profit"]))
    if totals["net_increase_cash"] is not None:
        identities.append(_check_identity(
            "cashflow_identity",
            [(totals["net_increase_cash"], 1), (totals["investing_cashflow"], -1),
             (totals["financing_cashflow"], -1),
             (totals["fx_effect"], -1)],
            expected=totals["operating_cashflow"] if totals["operating_cashflow"]
            is not None else Decimal(0),
            evidence=identity_evidence["cashflow"]))
    if totals["ending_cash"] is not None:
        identities.append(_check_identity(
            "cash_rollforward",
            [(totals["beginning_cash"], 1), (totals["net_increase_cash"], 1)],
            expected=totals["ending_cash"],
            evidence=identity_evidence["cash_rollforward"]))

    return MachineVerification(
        instrument_id=candidate.instrument_id, period_end=candidate.period_end,
        announcement_id=item_announcement(candidate), pdf_sha256=candidate.pdf_sha256,
        fields=tuple(results), sections=_section_evidence(sections),
        identities=tuple(identities),
    )


def item_announcement(candidate: CninfoS2CandidateFacts) -> str:
    """从候选文档 URL 末尾取回公告 ID，保持与索引身份一致。"""

    tail = candidate.document_url.rsplit("/", 1)[-1]
    if not tail.lower().endswith(".pdf"):
        raise MachineVerificationError("candidate document URL is not a PDF announcement")
    return tail[:-4]


__all__ = [
    "MACHINE_VERIFICATION_SCHEMA",
    "PATH_DESCRIPTIONS",
    "VERIFIER_ID",
    "VERIFIER_VERSION",
    "FieldVerification",
    "IndependentCheck",
    "MachineVerification",
    "MachineVerificationError",
    "verify_machine_evidence",
]
