"""S2 质量—估值挑战者的领域计算核心（开发文档 §10.1/§10.2/§10.4）。

本模块只做财务因子的选择、计算和横截面合成。它故意不负责网络接入、
数据适配、日更编排或因子结果落库；调用方必须先把来源记录规范化为
``FinancialFact``，并通过 ``VersionedFinancialFactStore`` 提供时点读取。

S2 的输入契约是普通工商企业的合并、累计口径：

* F07 = 归母净利润 TTM / ((期初 + 期末) / 2) 的归母权益；
* F08 = 经营现金流净额 TTM / 合并净利润 TTM；
* F09 = 营业收入 TTM / 去年同期 TTM - 1；
* F10 = 归母净利润 TTM / 决策日总市值。

流量事实按“上一完整年度 + 当年累计 - 上年同期累计”衔接，Q4
直接使用年报值。任何关键事实缺失、单位/公告版本不一致、非正分母、
PIT 未通过或金融业模板都会被拒绝；没有任何隐式的零填充或跨源补齐。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
import math
from typing import Iterable, Mapping

from ..fundamentals.versioned import (
    FinancialFact,
    PitViolation,
    ProfitScope,
    StatementScope,
    VersionedFinancialFactStore,
)


F07 = "F07"
F08 = "F08"
F09 = "F09"
F10 = "F10"


class S2Template(str, Enum):
    """行业模板。金融企业必须走独立模板，当前模块不实现该模板。"""

    ORDINARY = "ORDINARY"
    FINANCIAL = "FINANCIAL"


# 语义别名，便于调用方把“行业模板”与领域配置名称对应起来。
IndustryTemplate = S2Template


@dataclass(frozen=True, slots=True)
class S2MetricNames:
    """版本化财务事实中的标准指标名。

    默认名称是适配层输出的语义规范名，而不是某一家供应商的原始列名。
    例如 Tushare 的 ``n_income_attr_p`` 必须在适配层归一化为
    ``net_profit_attributable``。适配器若使用其它语义名，必须显式传入
    完整映射；不能在这里按字段相似度猜测。
    流量字段（净利润、经营现金流、营业收入）必须已经被来源验证为
    年内累计值，单位和币种由 ``FinancialFact`` 保留并在计算时校验。
    """

    attributable_net_profit: str = "net_profit_attributable"
    consolidated_net_profit: str = "net_profit_consolidated"
    operating_cash_flow: str = "operating_cashflow"
    revenue: str = "revenue"
    parent_equity: str = "parent_equity"

    def __post_init__(self) -> None:
        values = (
            self.attributable_net_profit,
            self.consolidated_net_profit,
            self.operating_cash_flow,
            self.revenue,
            self.parent_equity,
        )
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("all S2 metric names must be non-empty strings")
        if len(set(values)) != len(values):
            raise ValueError("S2 metric names must be distinct")


DEFAULT_S2_METRICS = S2MetricNames()


class S2ComputationError(ValueError):
    """一个标的无法形成完整、可解释的 S2 输入。"""

    def __init__(self, code: str, message: str, *, instrument_id: str,
                 repair_action: str = "补齐并验证同口径、可用时间明确的财务事实") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.instrument_id = instrument_id
        self.repair_action = repair_action

    def as_error(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "object_id": self.instrument_id,
            "retryable": False,
            "repair_action": self.repair_action,
        }


class UnsupportedS2Industry(S2ComputationError):
    """当前普通工商模板拒绝金融企业。"""

    def __init__(self, instrument_id: str, template: object) -> None:
        super().__init__(
            "S2_INDUSTRY_TEMPLATE_UNSUPPORTED",
            f"S2 ordinary-business template does not support industry template {template!r}",
            instrument_id=instrument_id,
            repair_action="provide and validate a separate bank/insurance/securities template",
        )


@dataclass(frozen=True, slots=True)
class S2Factors:
    """一个标的的四个原始 S2 因子及其 PIT 证据索引。"""

    instrument_id: str
    f07_roe_ttm: Decimal
    f08_cash_quality: Decimal
    f09_revenue_ttm_yoy: Decimal
    f10_earning_yield: Decimal
    latest_period_end: date
    ttm_start_period_end: date
    feature_template: S2Template
    fact_version_ids: tuple[str, ...]

    @property
    def values(self) -> dict[str, Decimal]:
        return {
            F07: self.f07_roe_ttm,
            F08: self.f08_cash_quality,
            F09: self.f09_revenue_ttm_yoy,
            F10: self.f10_earning_yield,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "F07": str(self.f07_roe_ttm),
            "F08": str(self.f08_cash_quality),
            "F09": str(self.f09_revenue_ttm_yoy),
            "F10": str(self.f10_earning_yield),
            "latest_period_end": self.latest_period_end.isoformat(),
            "ttm_start_period_end": self.ttm_start_period_end.isoformat(),
            "feature_template": self.feature_template.value,
            "fact_version_ids": list(self.fact_version_ids),
        }


@dataclass(frozen=True, slots=True)
class S2Signal:
    """完整 S2 排名结果；排名只来自同一时点的完整合格证券池。"""

    instrument_id: str
    factors: S2Factors
    f07_rank: float
    f08_rank: float
    f09_rank: float
    f10_rank: float
    quality_rank: float
    value_rank: float
    signal_rank: float

    def as_dict(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "factors": self.factors.as_dict(),
            "F07_rank": self.f07_rank,
            "F08_rank": self.f08_rank,
            "F09_rank": self.f09_rank,
            "F10_rank": self.f10_rank,
            "quality_rank": self.quality_rank,
            "value_rank": self.value_rank,
            "signal_rank": self.signal_rank,
        }


@dataclass(frozen=True, slots=True)
class S2Evaluation:
    """构建证券池时的成功或拒绝结果。

    被拒绝的证券保留明确原因，``factors`` 和 ``signal`` 均为 ``None``；
    它不会进入任一因子的排名，也不会被当作数值零。
    """

    instrument_id: str
    factors: S2Factors | None
    signal: S2Signal | None
    exclusion_code: str | None = None
    exclusion_reason: str | None = None

    @property
    def eligible(self) -> bool:
        return self.signal is not None


@dataclass(frozen=True, slots=True)
class _TTMValue:
    value: Decimal
    latest_period_end: date
    start_period_end: date
    fact_version_ids: tuple[str, ...]


def _decimal(value: object, *, field: str, instrument_id: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise S2ComputationError(
            "S2_FINANCIAL_FACT_MISSING",
            f"{field} is missing; S2 never fills missing values with zero",
            instrument_id=instrument_id,
        )
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise S2ComputationError(
            "S2_FINANCIAL_FACT_INVALID",
            f"{field} is not a valid numeric fact",
            instrument_id=instrument_id,
        ) from exc
    if not parsed.is_finite():
        raise S2ComputationError(
            "S2_FINANCIAL_FACT_INVALID",
            f"{field} is not finite",
            instrument_id=instrument_id,
        )
    return parsed


def _template(value: S2Template | str | None, *, instrument_id: str) -> S2Template:
    if value is None:
        return S2Template.ORDINARY
    if isinstance(value, S2Template):
        return value
    try:
        return S2Template(str(value).strip().upper())
    except ValueError as exc:
        raise S2ComputationError(
            "S2_INDUSTRY_TEMPLATE_UNKNOWN",
            f"unknown S2 industry template {value!r}",
            instrument_id=instrument_id,
            repair_action="classify the instrument into an explicitly validated S2 template",
        ) from exc


def _quarter_end(period_end: date, *, instrument_id: str) -> tuple[int, int]:
    expected = {3: 31, 6: 30, 9: 30, 12: 31}
    quarter = {3: 1, 6: 2, 9: 3, 12: 4}.get(period_end.month)
    if quarter is None or expected[period_end.month] != period_end.day:
        raise S2ComputationError(
            "S2_PERIOD_UNSUPPORTED",
            f"{period_end.isoformat()} is not a supported quarter-end financial period",
            instrument_id=instrument_id,
            repair_action="normalize the report period to a verified quarter-end date",
        )
    return period_end.year, quarter


def _period(year: int, quarter: int) -> date:
    return date(year, {1: 3, 2: 6, 3: 9, 4: 12}[quarter],
                {1: 31, 2: 30, 3: 30, 4: 31}[quarter])


def _same_basis(rows: Iterable[FinancialFact], *, label: str,
                instrument_id: str) -> None:
    rows = tuple(rows)
    if not rows:
        return
    units = {(row.currency, row.raw_unit) for row in rows}
    if len(units) != 1:
        raise S2ComputationError(
            "S2_UNIT_MISMATCH",
            f"{label} mixes currencies or raw units: {sorted(units)!r}",
            instrument_id=instrument_id,
            repair_action="normalize every input to one declared currency and unit",
        )


def _same_bundle(rows: Iterable[FinancialFact], *, period_end: date,
                 label: str, instrument_id: str) -> None:
    """同一报告期的多张表必须来自同一来源公告内容版本。"""

    rows = tuple(rows)
    if not rows:
        return
    if any(row.period_end != period_end for row in rows):
        raise S2ComputationError(
            "S2_PERIOD_ALIGNMENT_MISMATCH",
            f"{label} combines different report periods",
            instrument_id=instrument_id,
        )
    bundle_keys = {
        (row.source_id, row.source_document_id, row.content_hash,
         row.statement_scope, row.currency, row.raw_unit)
        for row in rows
    }
    if len(bundle_keys) != 1:
        raise S2ComputationError(
            "S2_REPORT_VERSION_MISMATCH",
            f"{label} mixes source, announcement, or content versions at "
            f"{period_end.isoformat()}",
            instrument_id=instrument_id,
            repair_action="link all statements to one verified announcement revision",
        )


def _index_metric(
    selected: Iterable[FinancialFact],
    *,
    metric: str,
    statement_scope: StatementScope,
    profit_scope: ProfitScope | None,
    instrument_id: str,
) -> dict[date, FinancialFact]:
    rows = [
        row for row in selected
        if row.metric == metric
        and row.statement_scope is statement_scope
        and row.profit_scope is profit_scope
    ]
    if not rows:
        raise S2ComputationError(
            "S2_FINANCIAL_FACT_MISSING",
            f"missing required metric {metric!r} with the requested statement scope",
            instrument_id=instrument_id,
        )
    out: dict[date, FinancialFact] = {}
    for row in rows:
        if row.period_end in out:
            raise S2ComputationError(
                "S2_FINANCIAL_FACT_AMBIGUOUS",
                f"more than one selected {metric!r} fact exists for {row.period_end}",
                instrument_id=instrument_id,
                repair_action="retain one verified PIT revision head per logical fact",
            )
        out[row.period_end] = row
    _same_basis(out.values(), label=metric, instrument_id=instrument_id)
    return out


def _ttm(
    rows: Mapping[date, FinancialFact],
    *,
    latest_period_end: date,
    instrument_id: str,
    label: str,
) -> _TTMValue:
    year, quarter = _quarter_end(latest_period_end, instrument_id=instrument_id)
    required: list[date]
    if quarter == 4:
        required = [latest_period_end]
    else:
        required = [
            _period(year - 1, 4),
            latest_period_end,
            _period(year - 1, quarter),
        ]
    missing = [period for period in required if period not in rows]
    if missing:
        raise S2ComputationError(
            "S2_TTM_INCOMPLETE",
            f"{label} TTM is missing {', '.join(p.isoformat() for p in missing)}; "
            "zero filling is forbidden",
            instrument_id=instrument_id,
            repair_action="ingest the complete same-scope annual/YTD and prior-period facts",
        )
    values = {
        period: _decimal(rows[period].value, field=f"{label}@{period}",
                         instrument_id=instrument_id)
        for period in required
    }
    if quarter == 4:
        value = values[latest_period_end]
        start_period_end = _period(year - 1, 4)
    else:
        value = (values[_period(year - 1, 4)]
                 + values[latest_period_end]
                 - values[_period(year - 1, quarter)])
        start_period_end = _period(year - 1, quarter)
    if not value.is_finite():
        raise S2ComputationError(
            "S2_FINANCIAL_FACT_INVALID",
            f"{label} TTM is non-finite",
            instrument_id=instrument_id,
        )
    versions = tuple(sorted({rows[period].version_id for period in required}))
    return _TTMValue(value, latest_period_end, start_period_end, versions)


def _latest_period(rows: Mapping[date, FinancialFact], *, instrument_id: str,
                   label: str) -> date:
    if not rows:
        raise S2ComputationError(
            "S2_FINANCIAL_FACT_MISSING",
            f"no selected facts for {label}",
            instrument_id=instrument_id,
        )
    latest = max(rows)
    _quarter_end(latest, instrument_id=instrument_id)
    return latest


def _collect_selected(
    store: VersionedFinancialFactStore,
    *,
    instrument_id: str,
    cutoff: datetime,
) -> list[FinancialFact]:
    try:
        return store.select_pit(cutoff, instrument_id=instrument_id)
    except PitViolation as exc:
        raise S2ComputationError(
            "PIT_UNVERIFIED",
            f"financial facts cannot be used at cutoff: {exc.message}",
            instrument_id=instrument_id,
            repair_action="repair publication time, availability basis or revision chain",
        ) from exc


def compute_s2_factors(
    *,
    store: VersionedFinancialFactStore,
    instrument_id: str,
    cutoff: datetime,
    market_cap: Decimal | int | float | str | None,
    industry_template: S2Template | str | None = S2Template.ORDINARY,
    metrics: S2MetricNames = DEFAULT_S2_METRICS,
) -> S2Factors:
    """按 PIT 选择并计算单只普通工商企业的 F07—F10。

    ``market_cap`` 必须与财务事实使用同一声明金额单位（默认映射通常是
    CNY 元），且必须是决策日冻结的正市值。市值的日期/来源留痕由快照层
    负责；本函数只拒绝缺失或非正输入，不会用股本和别的日期价格替代。
    """

    template = _template(industry_template, instrument_id=instrument_id)
    if template is not S2Template.ORDINARY:
        raise UnsupportedS2Industry(instrument_id, template.value)

    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise S2ComputationError(
            "PIT_UNVERIFIED",
            "cutoff must be timezone-aware",
            instrument_id=instrument_id,
            repair_action="pass an aware UTC cutoff to the PIT store",
        )
    selected = _collect_selected(store, instrument_id=instrument_id, cutoff=cutoff)
    attr_np = _index_metric(
        selected, metric=metrics.attributable_net_profit,
        statement_scope=StatementScope.CONSOLIDATED,
        profit_scope=ProfitScope.ATTRIBUTABLE, instrument_id=instrument_id)
    consolidated_np = _index_metric(
        selected, metric=metrics.consolidated_net_profit,
        statement_scope=StatementScope.CONSOLIDATED,
        profit_scope=ProfitScope.CONSOLIDATED, instrument_id=instrument_id)
    cfo = _index_metric(
        selected, metric=metrics.operating_cash_flow,
        statement_scope=StatementScope.CONSOLIDATED,
        profit_scope=None, instrument_id=instrument_id)
    revenue = _index_metric(
        selected, metric=metrics.revenue,
        statement_scope=StatementScope.CONSOLIDATED,
        profit_scope=None, instrument_id=instrument_id)
    equity = _index_metric(
        selected, metric=metrics.parent_equity,
        statement_scope=StatementScope.CONSOLIDATED,
        profit_scope=None, instrument_id=instrument_id)

    latest_period_end = _latest_period(attr_np, instrument_id=instrument_id,
                                       label=metrics.attributable_net_profit)
    for rows, label in (
        (consolidated_np, metrics.consolidated_net_profit),
        (cfo, metrics.operating_cash_flow),
        (revenue, metrics.revenue),
        (equity, metrics.parent_equity),
    ):
        if _latest_period(rows, instrument_id=instrument_id, label=label) != latest_period_end:
            raise S2ComputationError(
                "S2_PERIOD_ALIGNMENT_MISMATCH",
                f"{label} latest period does not match {latest_period_end.isoformat()}",
                instrument_id=instrument_id,
                repair_action="provide all S2 metrics for one common latest report period",
            )

    # Current-period S2 facts must be one report bundle. For prior periods,
    # each formula below checks its own multi-table bundle as well.
    _same_bundle(
        (attr_np[latest_period_end], consolidated_np[latest_period_end],
         cfo[latest_period_end], revenue[latest_period_end], equity[latest_period_end]),
        period_end=latest_period_end, label="current S2 bundle",
        instrument_id=instrument_id)

    attr_ttm = _ttm(attr_np, latest_period_end=latest_period_end,
                    instrument_id=instrument_id, label=metrics.attributable_net_profit)
    np_ttm = _ttm(consolidated_np, latest_period_end=latest_period_end,
                  instrument_id=instrument_id, label=metrics.consolidated_net_profit)
    cfo_ttm = _ttm(cfo, latest_period_end=latest_period_end,
                   instrument_id=instrument_id, label=metrics.operating_cash_flow)
    revenue_ttm = _ttm(revenue, latest_period_end=latest_period_end,
                       instrument_id=instrument_id, label=metrics.revenue)

    # Validate same-announcement pairing for every multi-table component that
    # enters the formulas. This prevents silently combining a revised income
    # value with an unrevised balance/cashflow value.
    year, quarter = _quarter_end(latest_period_end, instrument_id=instrument_id)
    ttm_periods = ([latest_period_end] if quarter == 4 else [
        latest_period_end, _period(year - 1, 4), _period(year - 1, quarter)])
    # F07 needs equity only at the rolling window's two endpoints. The
    # annual bridge used by the TTM formula is a flow fact, so it does not
    # require a balance-sheet value at that intermediate report period.
    for period in (latest_period_end, attr_ttm.start_period_end):
        _same_bundle((attr_np[period], equity[period]), period_end=period,
                     label="F07 report bundle", instrument_id=instrument_id)
    for period in ttm_periods:
        _same_bundle((consolidated_np[period], cfo[period]), period_end=period,
                     label="F08 report bundle", instrument_id=instrument_id)

    start_equity = _decimal(equity[attr_ttm.start_period_end].value,
                            field=f"{metrics.parent_equity}@{attr_ttm.start_period_end}",
                            instrument_id=instrument_id)
    end_equity = _decimal(equity[latest_period_end].value,
                          field=f"{metrics.parent_equity}@{latest_period_end}",
                          instrument_id=instrument_id)
    average_equity = (start_equity + end_equity) / Decimal(2)
    if average_equity <= 0:
        raise S2ComputationError(
            "S2_NON_POSITIVE_DENOMINATOR",
            "F07 average attributable equity must be positive",
            instrument_id=instrument_id,
            repair_action="exclude the object or provide a valid positive-equity ordinary-business template",
        )
    f07 = attr_ttm.value / average_equity

    if np_ttm.value <= 0:
        raise S2ComputationError(
            "S2_NON_POSITIVE_DENOMINATOR",
            "F08 requires positive consolidated net profit",
            instrument_id=instrument_id,
            repair_action="exclude non-positive-profit objects from the ordinary S2 template",
        )
    f08 = cfo_ttm.value / np_ttm.value

    prior_endpoint = _period(year - 1, quarter)
    prior_revenue_ttm = _ttm(
        revenue, latest_period_end=prior_endpoint,
        instrument_id=instrument_id, label=f"{metrics.revenue} prior-year")
    if prior_revenue_ttm.value <= 0:
        raise S2ComputationError(
            "S2_NON_POSITIVE_DENOMINATOR",
            "F09 requires positive prior-year revenue TTM",
            instrument_id=instrument_id,
            repair_action="exclude the object until a positive same-scope prior-year TTM exists",
        )
    f09 = revenue_ttm.value / prior_revenue_ttm.value - Decimal(1)

    market_cap_decimal = _decimal(market_cap, field="decision-date market cap",
                                   instrument_id=instrument_id)
    if market_cap_decimal <= 0:
        raise S2ComputationError(
            "S2_NON_POSITIVE_DENOMINATOR",
            "F10 decision-date market cap must be positive",
            instrument_id=instrument_id,
            repair_action="capture a positive market cap on the same decision date as the snapshot",
        )
    f10 = attr_ttm.value / market_cap_decimal

    all_versions = set(attr_ttm.fact_version_ids)
    all_versions.update(np_ttm.fact_version_ids)
    all_versions.update(cfo_ttm.fact_version_ids)
    all_versions.update(revenue_ttm.fact_version_ids)
    all_versions.update(prior_revenue_ttm.fact_version_ids)
    all_versions.update((equity[attr_ttm.start_period_end].version_id,
                         equity[latest_period_end].version_id))
    return S2Factors(
        instrument_id=instrument_id,
        f07_roe_ttm=f07,
        f08_cash_quality=f08,
        f09_revenue_ttm_yoy=f09,
        f10_earning_yield=f10,
        latest_period_end=latest_period_end,
        ttm_start_period_end=attr_ttm.start_period_end,
        feature_template=template,
        fact_version_ids=tuple(sorted(all_versions)),
    )


def cross_sectional_ranks(values: Mapping[str, Decimal | int | float | str]) -> dict[str, float]:
    """按值降序给出平均同分百分位排名。

    只有显式传入的有限值才参与排名。全等（包括单个对象）按规范记为
    ``0.5``；这与缺失不同，缺失对象根本不出现在 ``values`` 中。
    """

    if not values:
        return {}
    parsed: dict[str, Decimal] = {}
    for instrument_id, value in values.items():
        try:
            decimal = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"rank value for {instrument_id!r} is invalid") from exc
        if not decimal.is_finite():
            raise ValueError(f"rank value for {instrument_id!r} is non-finite")
        parsed[instrument_id] = decimal
    ordered = sorted(parsed.items(), key=lambda item: (-item[1], item[0]))
    n = len(ordered)
    if len({value for _, value in ordered}) == 1:
        return {instrument_id: 0.5 for instrument_id, _ in ordered}
    result: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        average_ordinal = (i + j) / 2
        percentile = 1.0 - average_ordinal / (n - 1)
        for index in range(i, j + 1):
            result[ordered[index][0]] = round(percentile, 6)
        i = j + 1
    return result


def build_s2_signals(
    *,
    store: VersionedFinancialFactStore,
    instrument_ids: Iterable[str],
    cutoff: datetime,
    market_cap_by_instrument: Mapping[str, Decimal | int | float | str | None],
    industry_template_by_instrument: Mapping[str, S2Template | str | None] | None = None,
    metrics: S2MetricNames = DEFAULT_S2_METRICS,
) -> list[S2Evaluation]:
    """计算证券池上的 S2，并只用完整对象做四个横截面排名。

    返回结果按可用信号降序、再按证券 ID 稳定排序；被拒绝对象排在末尾，
    且保留 ``exclusion_code``/``exclusion_reason``。这使 UI 或落库层可以
    显式展示覆盖率，而不会把缺失值混入信号。
    """

    ids = sorted(set(instrument_ids))
    templates = industry_template_by_instrument or {}
    evaluations: list[S2Evaluation] = []
    complete: list[S2Factors] = []
    for instrument_id in ids:
        try:
            factors = compute_s2_factors(
                store=store,
                instrument_id=instrument_id,
                cutoff=cutoff,
                market_cap=market_cap_by_instrument.get(instrument_id),
                industry_template=templates.get(instrument_id),
                metrics=metrics,
            )
        except S2ComputationError as exc:
            evaluations.append(S2Evaluation(
                instrument_id=instrument_id, factors=None, signal=None,
                exclusion_code=exc.code, exclusion_reason=exc.message))
        except (KeyError, TypeError, ValueError) as exc:
            evaluations.append(S2Evaluation(
                instrument_id=instrument_id, factors=None, signal=None,
                exclusion_code="S2_INPUT_INVALID", exclusion_reason=str(exc)))
        else:
            complete.append(factors)

    rank_by_factor = {
        F07: cross_sectional_ranks({f.instrument_id: f.f07_roe_ttm for f in complete}),
        F08: cross_sectional_ranks({f.instrument_id: f.f08_cash_quality for f in complete}),
        F09: cross_sectional_ranks({f.instrument_id: f.f09_revenue_ttm_yoy for f in complete}),
        F10: cross_sectional_ranks({f.instrument_id: f.f10_earning_yield for f in complete}),
    }
    by_id = {evaluation.instrument_id: evaluation for evaluation in evaluations}
    for factors in complete:
        f07_rank = rank_by_factor[F07][factors.instrument_id]
        f08_rank = rank_by_factor[F08][factors.instrument_id]
        f09_rank = rank_by_factor[F09][factors.instrument_id]
        f10_rank = rank_by_factor[F10][factors.instrument_id]
        quality_rank = (f07_rank + f08_rank + f09_rank) / 3.0
        signal = S2Signal(
            instrument_id=factors.instrument_id, factors=factors,
            f07_rank=f07_rank, f08_rank=f08_rank, f09_rank=f09_rank,
            f10_rank=f10_rank, quality_rank=quality_rank,
            value_rank=f10_rank,
            signal_rank=0.5 * quality_rank + 0.5 * f10_rank,
        )
        by_id[factors.instrument_id] = S2Evaluation(
            instrument_id=factors.instrument_id, factors=factors, signal=signal)
    return sorted(
        by_id.values(),
        key=lambda evaluation: (
            0 if evaluation.signal is not None else 1,
            -(evaluation.signal.signal_rank if evaluation.signal is not None else 0.0),
            evaluation.instrument_id,
        ),
    )


__all__ = [
    "DEFAULT_S2_METRICS",
    "F07",
    "F08",
    "F09",
    "F10",
    "IndustryTemplate",
    "S2ComputationError",
    "S2Evaluation",
    "S2Factors",
    "S2MetricNames",
    "S2Signal",
    "S2Template",
    "UnsupportedS2Industry",
    "build_s2_signals",
    "compute_s2_factors",
    "cross_sectional_ranks",
]
