"""在当前快照上计算 F10 并落库（§10.2、ADR-005）。

这个编排连接三样东西
------------------
  * 快照里的财报数据（含 pubDate）；
  * 快照 instrument 中与最后行情交易日匹配的决策日总市值；
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
from decimal import Decimal, InvalidOperation
from typing import Callable

from ..data.reader import SnapshotReader
from ..fundamentals.pit import FinancialsStore
from ..fundamentals.records import build_statements
from .runs import (FactorValue, create_research_run, research_run_metadata,
                   store_factor_values)

FACTOR_ID = "F10"
FACTOR_NAME = "盈利收益率"
# v1 used an invalid TTM formula (current YTD - prior-year YTD) and is retained
# only as an auditable historical version.  Never reuse its run id or results.
FEATURE_VERSION = "f10-v2"


def _decision_market_cap(instrument: dict, *,
                         last_trading_day: date) -> tuple[Decimal | None, str | None]:
    """读取并校验快照内与最后行情日对应的决策日总市值。

    F10 的分母必须是决策日市值。财报里的 ``total_share`` 描述的是报告期
    口径，不能和快照最后一个交易日的价格拼接成生产市值；生产编排因此只
    接受 instrument 明确冻结的 ``market_cap_cents``。
    """

    raw_cap = instrument.get("market_cap_cents")
    if raw_cap is None or (isinstance(raw_cap, str) and not raw_cap.strip()):
        return None, "缺决策日总市值"
    try:
        market_cap_cents = Decimal(str(raw_cap))
    except (InvalidOperation, TypeError, ValueError):
        return None, "决策日总市值无效"
    if not market_cap_cents.is_finite() or market_cap_cents <= 0:
        return None, "决策日总市值无效"

    raw_as_of = instrument.get("market_cap_as_of")
    if raw_as_of is None or not str(raw_as_of).strip():
        return None, "缺决策日总市值日期"
    try:
        market_cap_as_of = date.fromisoformat(str(raw_as_of))
    except (TypeError, ValueError):
        return None, "决策日总市值日期无效"
    if market_cap_as_of != last_trading_day:
        return None, "决策日总市值日期与最后行情日不一致"
    return market_cap_cents, None


def compute_f10_for_snapshot(*, con: sqlite3.Connection,
                             reader: SnapshotReader, snapshot_id: str,
                             as_of: datetime,
                             limit: int = 0,
                             write_guard: Callable[[], None] | None = None) -> dict:
    """在整个快照的证券上计算 F10，落库并返回摘要。"""

    instruments = reader.instruments(snapshot_id, as_of=as_of)
    ids = [i["instrument_id"] for i in instruments]
    instrument_by_id = {i["instrument_id"]: i for i in instruments}
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

        market_cap_cents, market_cap_reason = _decision_market_cap(
            instrument_by_id.get(instrument_id, {}),
            last_trading_day=last.trading_day,
        )
        if market_cap_reason is not None:
            exclude(instrument_id, market_cap_reason)
            continue

        ttm = store.trailing_twelve_months(instrument_id, as_of=as_of)
        if ttm is None:
            if not statements:
                exclude(instrument_id, "快照未包含财务数据")
            else:
                exclude(instrument_id, "TTM 不可得（缺上年同期或口径不成立）")
            continue
        # 生产路径只使用快照中已冻结、且日期匹配的总市值。保留
        # ``earning_yield_f10`` 供明确同一时点输入的纯函数调用方使用，
        # 但这里不能再用财报期 total_share × 最后收盘价代替市值。
        market_cap_micros = market_cap_cents * Decimal(10_000)
        value = (Decimal(ttm["ttm_net_profit_micros"]) /
                 market_cap_micros).quantize(Decimal("0.000001"))
        values.append(FactorValue(
            instrument_id=instrument_id, factor_id=FACTOR_ID,
            raw_value=float(value), coverage_ratio=1.0))

    run_id = create_research_run(
        con, snapshot_id=snapshot_id, as_of_time=as_of,
        code_version="0.1.0", feature_version=FEATURE_VERSION,
        notes=("F10 盈利收益率；财报可用性按 ADR-005 的保守规则推导；"
               "分母使用快照 instrument 的决策日总市值，且日期匹配最后行情日"),
        write_guard=write_guard)
    summary = store_factor_values(
        con, research_run_id=run_id, values=values,
        write_guard=write_guard)
    metadata = research_run_metadata(con, run_id) or {}
    validity_status = metadata.get("validity_status", "UNVERIFIED")
    withdrawal_reason = metadata.get("withdrawal_reason")

    return {
        **summary,
        # 同时给两种命名。项目的响应体一直是 snake_case（字段名与数据库列
        # 对齐，便于对照），但既有接口对新加的 id 也给了 camelCase 别名
        # （见 PreviewResponse 的 planId / plan_id）。这里保持一致，
        # 避免调用方需要记住"哪个接口是哪种风格"。
        "researchRunId": summary["research_run_id"],
        "feature_version": FEATURE_VERSION,
        "featureVersion": FEATURE_VERSION,
        "validity_status": validity_status,
        "validityStatus": validity_status,
        "withdrawal_reason": withdrawal_reason,
        "withdrawalReason": withdrawal_reason,
        "output_hash": summary["output_hash"],
        "outputHash": summary["output_hash"],
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
