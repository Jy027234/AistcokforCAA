"""在当前快照上计算 F10 并落库（§10.2、ADR-005）。

这个编排连接三样东西
------------------
  * 快照里的财报数据（含 pubDate）；
  * PIT 闸门（`FinancialsStore`）——只使用决策时点前已公布的财报；
  * 研究运行与因子落库（`research_run` / `feature_value`）。

任一环缺失都会让结果无法解释：没有 PIT 闸门就是未来信息，
没有研究运行就不知道排名属于哪个时点。

财报数据的来源
--------------
快照的 `financials` 数据集如果不存在，说明这份快照没有采过财务数据。
此时**不报错、也不猜**，而是把每个标的标成"快照未包含财务数据"——
那是一个可读的缺失原因，而不是一个算错的数值。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from typing import Callable

from ..data.reader import SnapshotReader
from ..fundamentals.pit import FinancialsStore, earning_yield_f10
from ..fundamentals.records import build_statements
from .runs import FactorValue, create_research_run, store_factor_values

FACTOR_ID = "F10"
FACTOR_NAME = "盈利收益率"
FEATURE_VERSION = "f10-v1"


def compute_f10_for_snapshot(*, con: sqlite3.Connection,
                             reader: SnapshotReader, snapshot_id: str,
                             as_of: datetime,
                             limit: int = 0,
                             write_guard: Callable[[], None] | None = None) -> dict:
    """在整个快照的证券上计算 F10，落库并返回摘要。"""

    instruments = reader.instruments(snapshot_id, as_of=as_of)
    ids = [i["instrument_id"] for i in instruments]
    if limit:
        ids = ids[:limit]

    # 交易日历：PIT 的可得时点依赖它，必须来自已证实的交易日
    calendar_days = [date.fromisoformat(d)
                     for d in reader.trading_calendar(snapshot_id, as_of=as_of)]

    financial_cache = reader.financials(snapshot_id, as_of=as_of)
    statements, skipped = build_statements(financial_cache or {})

    store = FinancialsStore(statements=statements, trading_days=calendar_days)

    values: list[FactorValue] = []
    reasons: dict[str, int] = {}

    def exclude(instrument_id: str, reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1
        values.append(FactorValue(instrument_id=instrument_id,
                                  factor_id=FACTOR_ID, raw_value=None,
                                  exclusion_reason=reason))

    for instrument_id in ids:
        bars = reader.daily_quotes(snapshot_id, as_of=as_of,
                                   instrument_id=instrument_id)
        if not bars:
            exclude(instrument_id, "快照内无行情")
            continue
        last = bars[-1]

        ttm = store.trailing_twelve_months(instrument_id, as_of=as_of)
        if ttm is None:
            if not statements:
                exclude(instrument_id, "快照未包含财务数据")
            else:
                exclude(instrument_id, "TTM 不可得（缺上年同期或口径不成立）")
            continue
        shares = ttm.get("total_share")
        value = earning_yield_f10(
            ttm_net_profit_micros=ttm["ttm_net_profit_micros"],
            total_share=Decimal(shares) if shares else None,
            close_cents=last.close_cents)
        if value is None:
            exclude(instrument_id, "缺总股本或价格无效")
            continue
        values.append(FactorValue(
            instrument_id=instrument_id, factor_id=FACTOR_ID,
            raw_value=float(value), coverage_ratio=1.0))

    run_id = create_research_run(
        con, snapshot_id=snapshot_id, as_of_time=as_of,
        code_version="0.1.0", feature_version=FEATURE_VERSION,
        notes="F10 盈利收益率；财报可用性按 ADR-005 的保守规则推导",
        write_guard=write_guard)
    summary = store_factor_values(
        con, research_run_id=run_id, values=values,
        write_guard=write_guard)

    return {
        **summary,
        # 同时给两种命名。项目的响应体一直是 snake_case（字段名与数据库列
        # 对齐，便于对照），但既有接口对新加的 id 也给了 camelCase 别名
        # （见 PreviewResponse 的 planId / plan_id）。这里保持一致，
        # 避免调用方需要记住"哪个接口是哪种风格"。
        "researchRunId": summary["research_run_id"],
        "snapshotId": snapshot_id,
        "asOfTime": as_of.isoformat(),
        "factorId": FACTOR_ID,
        "factorName": FACTOR_NAME,
        "financialStatements": len(statements),
        "skippedStatements": len(skipped),
        "calendarDays": len(calendar_days),
        "exclusionBreakdown": reasons,
        "note": ("排名在每个因子内部、只对有值的标的计算；"
                 "算不出的标的带可读原因，不参与排名"),
    }
