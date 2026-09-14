"""交易日历来源：多源降级，不把日历押在单一免费源上。

为什么单独成模块
----------------
指数日线是目前唯一能证明"某天确实是交易日"的免费依据（§7.3 要求保守顺延，
而保守顺延需要知道下一个交易日是哪天）。

但实测（2026-09-14）：东方财富会按出口 IP 长时段封锁——同一次会话里
先成功、后失败、过一会儿又能用。把一个必须"可复现"的归档脚本押在它上面，
脚本就会时好时坏。

因此这里按优先级依次尝试多个源，并且：

  * 每个源都经过既有的守卫/限速/熔断/归档链路，原始响应照常留证；
  * 记录**日历来自哪个源**，写入运行摘要，便于事后核对；
  * 全部失败时明确报错，不退回工作日近似（那会把"证明"降级成"猜测"）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from aquant.adapters.providers.eastmoney import EastmoneyClient
from aquant.adapters.providers.fetch_guard import FetchPolicy
from aquant.adapters.providers.tencent import TencentClient
from aquant.domain.data.forward_archive import ForwardArchive

#: 沪深指数日线都不需要登录，且与交易日一一对应。
INDEX_SYMBOLS = ("sh000001", "sz399001")


@dataclass(frozen=True, slots=True)
class CalendarResult:
    trading_days: list[date]
    source_id: str
    symbol: str
    attempted: list[dict]

    def summary(self) -> dict:
        return {
            "source_id": self.source_id,
            "symbol": self.symbol,
            "trading_days": len(self.trading_days),
            "first_day": self.trading_days[0].isoformat() if self.trading_days else None,
            "last_day": self.trading_days[-1].isoformat() if self.trading_days else None,
            "attempted": self.attempted,
        }


def _parse_days(bars: list) -> list[date]:
    """从日线序列里取出交易日。

    两个提供方的返回形状不同，必须都接受：
      * 腾讯 daily_quotes 返回已切分的行：["2026-09-09", "1", "2", ...]
      * 东财 daily_quotes 返回**已切分**的行，但它原始形态是逗号串，
        早期调用方可能直接传 ["2026-09-09,1,2"] 这种整串。
    这里统一处理：先取首元素，若仍是含逗号的串则再切一次。
    """

    out: list[date] = []
    for row in bars:
        if not row:
            continue
        head = row[0] if isinstance(row, (list, tuple)) else row
        if isinstance(head, str) and "," in head:
            head = head.split(",")[0]
        try:
            out.append(date.fromisoformat(str(head).strip()))
        except ValueError:
            continue
    return sorted(set(out))


def load_trading_calendar(
    archive: ForwardArchive,
    *,
    begin: date,
    end: date,
    tencent: TencentClient | None = None,
    eastmoney: EastmoneyClient | None = None,
    require_after: date | None = None,
) -> CalendarResult:
    """按优先级取交易日历。

    require_after: 若提供，则要求日历中存在**严格晚于**该日期的交易日。
    指数日线只能证明已经发生的交易日，因此当日或未来日期必然无法满足——
    这正是 CALENDAR_NOT_READY 的由来，属于正确行为而非故障。
    """

    attempted: list[dict] = []
    b, e = begin.isoformat(), end.isoformat()

    def acceptable(days: list[date]) -> bool:
        if not days:
            return False
        if require_after is None:
            return True
        return any(d > require_after for d in days)

    # 1. 腾讯（实测最稳）
    tc = tencent or TencentClient(archive)
    for symbol in INDEX_SYMBOLS:
        out, bars = tc.daily_quotes(symbol, b, e, adjust=0)
        days = _parse_days(bars)
        attempted.append({"source_id": "tencent-ifzq", "symbol": symbol,
                          "ok": out.ok and bool(days), "days": len(days),
                          "detail": out.detail})
        if out.ok and acceptable(days):
            return CalendarResult(days, "tencent-ifzq", symbol, attempted)

    # 2. 东财（字段更全，但会按 IP 封锁）
    em = eastmoney or EastmoneyClient(archive)
    for secid, symbol in (("1.000001", "sh000001"), ("0.399001", "sz399001")):
        out, klines = em.daily_quotes(secid, b.replace("-", ""), e.replace("-", ""),
                                      adjust=0)
        days = _parse_days(klines)
        attempted.append({"source_id": "eastmoney-direct", "symbol": symbol,
                          "ok": out.ok and bool(days), "days": len(days),
                          "detail": out.detail})
        if out.ok and acceptable(days):
            return CalendarResult(days, "eastmoney-direct", symbol, attempted)

    raise RuntimeError(
        "no calendar source could prove the requested trading days; "
        "refusing to fall back to a weekday approximation. attempted="
        + repr(attempted)
    )
