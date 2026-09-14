"""工作台视图模型：把领域数据投影成前端可直接渲染的形状。

设计约束（主文档 §14.1）：前端不复制计算逻辑。因此所有数值、分级与限制说明
都在这里算好，前端只负责呈现，不得自行推导排名、费用或可模拟性。

三条来自 §5.3 的硬要求，在本模块落实为**数据形状**而非文案约定：

1. "排名前 10%"不得改写为"上涨概率 90%"：数值一律以 rankPct + 明确语义标签给出，
   视图模型**不提供任何概率字段**。
2. 禁止"综合投资价值 87.36 分"：因此这里**不存在** totalScore 之类的字段。
   综合排名一律可展开为公式与各项贡献（rankBreakdown）。
3. 限制要说清规则、适用日期与修复方法，不允许只显示"失败"：
   每个不可交易项都带 rule / effectiveFrom / repair。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from ..domain.data.reader import SnapshotReader
from ..domain.simulation.fees import FeeTable
from ..domain.simulation.simulator import Bar, BoardRule


def money(cents: int | None) -> str:
    """分 -> 人民币展示串。前端不做金额换算。"""

    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    yuan, fen = divmod(abs(cents), 100)
    return f"{sign}{yuan:,}.{fen:02d}"


def pct(value: Decimal | float, digits: int = 2) -> str:
    return f"{float(value):.{digits}f}%"


#: 因子值的展示精度，按单位语义决定。
#: 目的是消除浮点尾巴（0.09899999999999998 -> 0.099），
#: 而不是改变数值本身——原始值仍保留给下游与审计使用。
_VALUE_DIGITS = {
    "ratio": 4,          # 收益率/比值：万分位足够
    "annualized": 4,     # 年化波动率
    "percent": 2,
    "cny": 2,
    "shares": 0,
}


def factor_value(value: float | None, unit: str | None) -> str:
    """把因子数值格式化成展示串。"""

    if value is None:
        return "—"
    digits = _VALUE_DIGITS.get((unit or "").lower(), 4)
    return f"{float(value):.{digits}f}"


# ======================================================================
# 数据状态（§5.4 顶栏）
# ======================================================================
@dataclass(slots=True)
class DataStatus:
    snapshot_id: str
    kind: str
    as_of_time: str
    published_at: str | None
    data_mode: str
    watermark: str | None
    quality_status: str
    readiness: str
    readiness_label: str
    blocking_issues: list[dict]
    dataset_summary: list[dict]
    time_label: str
    account_label: str = "模拟账户"

    def as_dict(self) -> dict:
        return {
            "snapshotId": self.snapshot_id, "kind": self.kind,
            "asOfTime": self.as_of_time, "publishedAt": self.published_at,
            "dataMode": self.data_mode, "watermark": self.watermark,
            "qualityStatus": self.quality_status, "readiness": self.readiness,
            "readinessLabel": self.readiness_label,
            "blockingIssues": self.blocking_issues,
            "datasetSummary": self.dataset_summary,
            "timeLabel": self.time_label, "accountLabel": self.account_label,
        }


def build_data_status(reader: SnapshotReader, snapshot_id: str) -> DataStatus:
    snap = reader.store.require_published(snapshot_id)
    issues = reader.store.blocking_issues(snapshot_id)
    datasets = reader.store.datasets(snapshot_id)

    if issues:
        readiness, label = "BLOCKING", "数据不完整，已阻断正式研究"
    elif snap["quality_status"] == "DEGRADED":
        readiness, label = "PARTIAL", "数据部分缺失"
    else:
        readiness, label = "READY", "数据已就绪"

    time_label = {
        "SYNTHETIC": "虚构示例 · 不可用于收益结论",
        "PRODUCTION": "生产快照",
    }.get(snap["data_mode"], "未知数据模式")

    return DataStatus(
        snapshot_id=snapshot_id,
        kind=snap["kind"],
        as_of_time=snap["as_of_time"] or snap["input_cutoff_at"],
        published_at=snap["published_at"],
        data_mode=snap["data_mode"],
        watermark=snap["watermark"],
        quality_status=snap["quality_status"],
        readiness=readiness,
        readiness_label=label,
        blocking_issues=[
            {"code": i["error_code"], "message": i["message"],
             "objectId": i["object_id"], "retryable": bool(i["retryable"]),
             "repair": i["repair_action"]}
            for i in issues
        ],
        dataset_summary=[
            {"name": d["name"], "recordCount": d["record_count"],
             "coverage": d["coverage_ratio"],
             "asOfUpperBound": d["as_of_upper_bound"]}
            for d in datasets
        ],
        time_label=time_label,
    )


# ======================================================================
# 研究卡片（§5.3）
# ======================================================================
@dataclass(slots=True)
class ResearchCardVM:
    instrument_id: str
    display_name: str
    exchange: str
    board: str
    snapshot_id: str
    as_of_time: str
    generated_at: str
    data_completeness: str
    time_label: str
    rank_semantics: str
    rank_breakdown: list[dict]
    comparison_scope: str
    evidence: list[dict]
    counter_evidence: list[dict]
    uncertainties: list[str]
    actions: list[dict]
    tradability: dict
    limitations: list[str]

    def as_dict(self) -> dict:
        return {
            "instrumentId": self.instrument_id, "displayName": self.display_name,
            "exchange": self.exchange, "board": self.board,
            "snapshotId": self.snapshot_id, "asOfTime": self.as_of_time,
            "generatedAt": self.generated_at,
            "dataCompleteness": self.data_completeness, "timeLabel": self.time_label,
            "rankSemantics": self.rank_semantics,
            "rankBreakdown": self.rank_breakdown,
            "comparisonScope": self.comparison_scope,
            "evidence": self.evidence, "counterEvidence": self.counter_evidence,
            "uncertainties": self.uncertainties, "actions": self.actions,
            "tradability": self.tradability, "limitations": self.limitations,
        }


def _tradability(
    *, exchange: str, board: str, trading_day: date,
    board_rules: list[BoardRule], bar: Bar | None,
) -> tuple[dict, list[str]]:
    """可模拟性判定。不可模拟时必须给出具体规则、适用日期与修复方法。"""

    if bar is None:
        return ({
            "simulatable": False,
            "reason": "NO_VALID_OPEN_PRICE",
            "reasonLabel": "当日无有效开盘价",
            "detail": "停牌、无行情或状态未知时不得模拟成交，也不得用最近价替代当日开盘价",
            "rule": f"{exchange}/{board}", "effectiveFrom": None,
            "repair": "确认该证券当日状态；若为停牌则等待复牌后再纳入草稿",
        }, ["当日不可模拟：无有效开盘价"])

    rule = next((r for r in board_rules
                 if r.exchange == exchange and r.board == board and r.covers(trading_day)),
                None)
    if rule is None:
        return ({
            "simulatable": False,
            "reason": "RULE_VERSION_MISSING",
            "reasonLabel": "缺少该板块的规则版本",
            "detail": f"未找到 {exchange}/{board} 在 {trading_day.isoformat()} 生效的交易规则",
            "rule": f"{exchange}/{board}", "effectiveFrom": None,
            "repair": "补充该板块对应生效日的规则配置；不得假定默认涨跌幅",
        }, ["当日不可模拟：规则版本缺失"])

    if exchange not in {"SSE", "SZSE"} or board != "MAIN":
        return ({
            "simulatable": False,
            "reason": "OUTSIDE_DEFAULT_POOL",
            "reasonLabel": "不在首期默认可模拟池",
            "detail": f"{exchange}/{board} 可观察，但不进入可执行模拟池",
            "rule": f"{exchange}/{board}",
            "effectiveFrom": rule.effective_from.isoformat(),
            "repair": "按板块补齐交易规则、公司行为与测试后再纳入；不得只改一个过滤条件",
        }, ["当日不可模拟：不在默认可模拟池（可观察）"])

    return ({
        "simulatable": True, "reason": None, "reasonLabel": "可模拟",
        "detail": f"按 {exchange}/{board} 规则模拟，涨跌停 {rule.price_limit_pct}%，"
                  f"整手 {rule.lot_size} 股",
        "rule": f"{exchange}/{board}",
        "effectiveFrom": rule.effective_from.isoformat(), "repair": None,
    }, [])


def build_research_card(
    reader: SnapshotReader,
    *,
    snapshot_id: str,
    as_of: datetime,
    instrument_id: str,
    trading_day: date,
    board_rules: list[BoardRule],
    listings: dict[str, tuple[str, str]],
    bar: Bar | None,
    factor_values: list[dict] | None = None,
    evidence: list[dict] | None = None,
    counter_evidence: list[dict] | None = None,
) -> ResearchCardVM:
    """组装研究卡片。factor_values 由策略层算好后传入，视图层不计算因子。"""

    instruments = {i["instrument_id"]: i
                   for i in reader.instruments(snapshot_id, as_of=as_of)}
    inst = instruments.get(instrument_id)
    if inst is None:
        raise KeyError(f"instrument {instrument_id!r} is not covered by {snapshot_id}")

    status = build_data_status(reader, snapshot_id)
    exchange, board = listings.get(
        instrument_id, (inst.get("exchange", "OTHER"), inst.get("board", "OTHER")))
    tradability, limitations = _tradability(
        exchange=exchange, board=board, trading_day=trading_day,
        board_rules=board_rules, bar=bar)

    breakdown = [
        {"factorId": f.get("factor_id"), "name": f.get("name", f.get("factor_id")),
         # 展示串在视图模型算好，前端不做数值格式化（§14.1）
         "value": factor_value(f.get("value"), f.get("unit")),
         "valueRaw": f.get("value"),
         "unit": f.get("unit"),
         "rankPct": f.get("rank_pct"),
         "rankLabel": (f"{float(f['rank_pct']) * 100:.0f}%" if f.get("rank_pct") is not None else "—"),
         "coverage": f.get("coverage"),
         "coverageLabel": (f"{float(f['coverage']):.2f}" if f.get("coverage") is not None else "—"),
         "contribution": f.get("contribution")}
        for f in (factor_values or [])
    ]

    ev = list(evidence or [])
    ce = list(counter_evidence or [])
    if not ce:
        # §5.3 要求"至少一个有效反证或明确未找到反证"——显式表达，不留空
        ce = [{"statement": "未找到反证", "noneFound": True,
               "note": "本次检索范围内未找到有效反证；这不等于不存在反证"}]

    return ResearchCardVM(
        instrument_id=instrument_id,
        display_name=inst.get("short_name") or instrument_id,
        exchange=exchange, board=board,
        snapshot_id=snapshot_id,
        as_of_time=status.as_of_time,
        generated_at=datetime.now(timezone.utc).isoformat(),
        data_completeness=status.readiness_label,
        time_label=status.time_label,
        rank_semantics="横截面排名百分位（越大越靠前）。这是排名，不是上涨概率，也不表示预期收益",
        rank_breakdown=breakdown,
        comparison_scope=f"比较范围：{snapshot_id} 快照内可模拟池",
        evidence=ev,
        counter_evidence=ce,
        uncertainties=[
            "预期是否已被价格消化：首期不作判断",
            "缺少的数据：见数据状态抽屉中的数据集覆盖",
            "观察窗口：日频研究，初始策略每周调仓",
        ],
        actions=[
            {"id": "watchlist.add", "label": "加入自选",
             "sideEffect": "自选写入", "note": "不产生订单"},
            {"id": "compare", "label": "加入比较", "sideEffect": "无", "note": None},
            {"id": "draft.create", "label": "创建模拟草稿", "sideEffect": "草稿",
             "note": "需在界面中显式确认后才会冻结"},
        ],
        tradability=tradability,
        limitations=limitations,
    )


# ======================================================================
# 草稿与差异预览（§5.4 底部、§5.5）
# ======================================================================
def build_draft_vm(
    *,
    plan_id: str,
    portfolio_id: str,
    trading_day: date,
    cash_before_cents: int,
    orders: list[dict],
    targets: list[dict],
    excluded: list[dict],
    fee_table: FeeTable,
    industry_cap_pct: Decimal,
    equity_value_cents: int = 0,
) -> dict:
    """草稿视图：预计资金、行业分布、规则检查、确认入口。

    明确标注**未冻结**——预览不产生任何成交（A08）。
    """

    est_fees = 0
    buy_total = 0
    sell_total = 0
    rows: list[dict] = []
    industry_value: dict[str, int] = {}
    weight_by_id = {t["instrument_id"]: t.get("weight_pct") for t in targets}

    for o in orders:
        charge = fee_table.compute(side=o["side"], quantity=o["quantity"],
                                   price_cents=o["price_cents"],
                                   trading_day=trading_day)
        est_fees += charge.total_cents
        gross = o["quantity"] * o["price_cents"]
        if o["side"] == "BUY":
            buy_total += gross
        else:
            sell_total += gross
        rows.append({
            "instrumentId": o["instrument_id"], "side": o["side"],
            "quantity": o["quantity"], "price": money(o["price_cents"]),
            "gross": money(gross), "estimatedFee": money(charge.total_cents),
            "rationale": o.get("rationale"),
            "targetWeightPct": weight_by_id.get(o["instrument_id"]),
        })

    for t in targets:
        if equity_value_cents:
            value = int(Decimal(equity_value_cents) * Decimal(str(t["weight_pct"]))
                        / Decimal(100))
            code = t.get("industry_code") or "UNKNOWN"
            industry_value[code] = industry_value.get(code, 0) + value

    industry_rows = [
        {"industryCode": k, "value": money(v),
         "sharePct": pct(Decimal(v) * 100 / equity_value_cents) if equity_value_cents else "—",
         "overCap": (Decimal(v) * 100 / equity_value_cents) > industry_cap_pct
                    if equity_value_cents else False}
        for k, v in sorted(industry_value.items())
    ]

    cash_after = cash_before_cents - buy_total - est_fees + sell_total
    rule_failures = [e for e in excluded if e.get("reason") in
                     {"RULE_VERSION_MISSING", "NO_VALID_OPEN_PRICE"}]

    return {
        "planId": plan_id,
        "portfolioId": portfolio_id,
        "tradingDay": trading_day.isoformat(),
        "frozen": False,
        "frozenLabel": "未冻结 · 预览不产生成交",
        "orders": rows,
        "estimatedFees": money(est_fees),
        "cashBefore": money(cash_before_cents),
        "cashAfter": money(cash_after),
        "cashAfterCents": cash_after,
        "buyTotal": money(buy_total),
        "sellTotal": money(sell_total),
        "industryCapPct": pct(industry_cap_pct),
        "industry": industry_rows,
        "excluded": excluded,
        "ruleChecks": [
            {"name": "预计现金不为负", "passed": cash_after >= 0},
            {"name": "全部订单有当日行情", "passed": not any(
                e.get("reason") == "NO_VALID_OPEN_PRICE" for e in excluded)},
            {"name": "全部订单有规则版本", "passed": not any(
                e.get("reason") == "RULE_VERSION_MISSING" for e in excluded)},
            {"name": "行业上限", "passed": not any(r["overCap"] for r in industry_rows)},
        ],
        "confirmAction": {
            "id": "plan.freeze",
            "label": "确认并冻结计划",
            "requirement": "需要人类用户显式确认；确认主体不能是模型",
            "revalidate": ["计划版本", "快照版本", "账户状态版本", "确认主体", "有效期"],
        },
    }
