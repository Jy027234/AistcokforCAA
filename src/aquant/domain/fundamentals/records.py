"""把 BaoStock 财务记录转成领域对象（单位归一 + 口径处理）。

为什么单独一层
--------------
转换里有两类容易出错的东西，集中在一处比散落在调用点安全：

  1. **单位**：BaoStock 给"元"与小数比率，我方用**整数微元**与 Decimal。
     元 -> 微元是 ×10^6，比率必须走 Decimal（float 会在 F10 上留下尾差）。
  2. **口径**：T9 实测确认 netProfit 是**年内累计**（不是单季），
     这个事实必须写在解析处，否则调用方很容易按单季处理。

空值一律转 None，**不填 0**：0 是一个具体且错误的数值，
而 None 会被 PIT 层显式拒绝（§10.2 缺失值不得用 0 填充）。
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal, InvalidOperation

from ..fundamentals.pit import FinancialStatement

MICROS_PER_YUAN = 1_000_000

#: netProfit 的单位是元还是万元，必须确认而不是猜。
#: 茅台 2026Q2 netProfit = 46033330566.78，若为万元则是 4.6 万亿，
#: 与市值量级不符；若为元则是 460 亿，与公开事实一致。**结论：元**。
#: 记录在这里，是因为"凭列名猜单位"正是 §6.3 明令禁止的。
NET_PROFIT_UNIT = "yuan"


def _yuan_to_micros(value: str | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int((Decimal(value) * MICROS_PER_YUAN).to_integral_value())
    except (InvalidOperation, ValueError):
        return None


def _decimal(value: str | None) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(value)
        return parsed if parsed.is_finite() else None
    except InvalidOperation:
        return None


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def statement_from_record(instrument_id: str, record: dict,
                          source_id: str = "baostock") -> FinancialStatement | None:
    """一条 profit 记录 -> FinancialStatement。缺关键日期时返回 None。"""

    stat = _date(record.get("statDate"))
    pub = _date(record.get("pubDate"))
    if stat is None or pub is None:
        # 没有报告期或公布日就无法建立时点，**不得**用别的日期顶替
        return None

    return FinancialStatement(
        instrument_id=instrument_id,
        stat_date=stat,
        pub_date=pub,
        net_profit_micros=_yuan_to_micros(record.get("netProfit")),
        # MBRevenue 不采信：T9 实测有整季空值，采了也不能用于 TTM。
        # 保留字段以便将来换源，但这里一律 None。
        revenue_micros=None,
        roe_avg=_decimal(record.get("roeAvg")),
        eps_ttm_micros=_yuan_to_micros(record.get("epsTTM")),
        cfo_to_np=_decimal(record.get("CFOToNP")),
        total_share=_decimal(record.get("totalShare")),
        source_id=source_id,
    )


def consistency_violations(statements: list[FinancialStatement]) -> list[dict]:
    """检查能由单条记录可靠判定的数值异常。

    为什么需要这个：单位错了 10 倍时，所有数字都"看起来正常"——
    单看一个字段永远发现不了。我自己就在核对时把科学计数法读错一次，
    误以为单位错了。机器化检查比人眼可靠。

    年内累计利润并不满足单调性：后续季度发生亏损时，半年或前三季度
    累计利润可以低于前一期，甚至跨过 0。这里不能把经营结果当成会计
    恒等式，也不能据此判断供应商给的是累计值还是单季值。

    当前对象只有一条利润值和股本，没有同口径独立字段可组成真正的
    跨字段恒等式。因此这里只拒绝非有限或非正股本；利润为 0 是合法事实。
    口径、单位与报告版本由来源映射和原文抽查验证，而不是靠走势猜测。
    """

    problems: list[dict] = []
    for s in statements:
        shares = s.total_share
        if shares is not None and (not shares.is_finite() or shares <= 0):
            problems.append({
                "instrument_id": s.instrument_id,
                "stat_date": s.stat_date.isoformat(),
                "rule": "总股本有效",
                "detail": f"总股本必须是有限正数，实际为 {shares}",
            })
    return problems


def build_statements(financials_cache: dict) -> tuple[list[FinancialStatement], list[dict]]:
    """把整个缓存转成领域对象，同时返回被跳过的记录（供复核）。"""

    out: list[FinancialStatement] = []
    skipped: list[dict] = []
    for iid, periods in (financials_cache.get("statements") or {}).items():
        for period, record in sorted(periods.items()):
            st = statement_from_record(iid, record)
            if st is None:
                skipped.append({"instrument_id": iid, "period": period,
                                "reason": "缺少 statDate 或 pubDate"})
                continue
            out.append(st)
    return out, skipped
