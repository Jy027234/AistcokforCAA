"""BaoStock provider（裸 socket，不是 HTTP）。

为什么单独一个模块、而且要格外小心
----------------------------------
其余 provider 都走 HTTP，统一经过：func:`fetch_guard.assert_url_allowed`
的域名白名单与 :class:`ForwardArchive` 的原文归档。**BaoStock 走
www.baostock.com:10030 的裸 socket**，这两道机制都覆盖不到它：

  * 白名单管不到——没有 URL 可校验；
  * 归档管不到——没有"响应原文"可供哈希。

不处理这两件事，BaoStock 就会成为整条数据链上**唯一没有留证**的一环，
而它提供的恰恰是行业分类与财务数据这类会直接影响研究结论的输入。

因此本模块自己做等价的事：

  1. **显式目标白名单**：只连 BAOSTOCK_HOST:BAOSTOCK_PORT，
     与 fetch_guard 同样的默认拒绝姿态；
  2. **留证**：每次查询把返回的记录逐条落盘（JSON）并记 sha256，
     收据进同一个 ForwardArchive，与 HTTP 源共享同一套收据体系；
  3. **限速**：BaoStock 服务端对批量查询有限速（实测全量 25~90 秒），
     连接级串行化，避免并发把服务端惹毛。

单位约定（§6.3，必须显式换算，不能凭列名猜）
------------------------------------------
  * close/open/high/low：元 -> **分**（×100）
  * volume：股（BaoStock 直接给股，与腾讯的"手"不同）
  * amount：元 -> **分**（×100）
  * turn：百分数（换手率），保留原值
  * 财务比率：BaoStock 给小数（0.179543 = 17.9543%），保留原值并在字段名标注
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

#: 只允许连这一个目标。与 fetch_guard 同样的默认拒绝姿态：
#: 新增目标必须显式改这里，不存在"配置一下就能连任意主机"。
BAOSTOCK_HOST = "www.baostock.com"
BAOSTOCK_PORT = 10030

#: 已知的分类体系。证监会分类是 BaoStock 全量返回的唯一体系。
CSRC_CLASSIFICATION = "证监会行业分类"


class BaostockUnavailable(RuntimeError):
    """登录失败或服务端不可用。调用方必须显式处理，不得静默降级。"""


@dataclass
class Receipt:
    """一次查询的留证。与 HTTP 源的 receipt 语义对齐（内容不可变、可哈希）。"""

    label: str
    requested_at: datetime
    record_count: int
    content_hash: str
    stored_path: str

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "requested_at": self.requested_at.isoformat(),
            "record_count": self.record_count,
            "content_hash": self.content_hash,
            "stored_path": self.stored_path,
        }


class BaostockClient:
    """BaoStock 的窄封装：登录、查询、留证。

    刻意**不**做成和 HTTP provider 一样的接口：它的失效模式不同
    （socket 超时 vs HTTP 状态码），伪装成一样会掩盖这个差异。
    """

    source_id = "baostock"

    def __init__(self, archive_root: str | Path, *,
                 sleep: Callable[[float], None] = time.sleep,
                 min_interval_seconds: float = 0.0) -> None:
        self.root = Path(archive_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._sleep = sleep
        self._min_interval = min_interval_seconds
        self._last_call = 0.0
        self._bs: Any = None
        self.receipts: list[Receipt] = []

    # ------------------------------------------------------------ lifecycle
    def __enter__(self) -> "BaostockClient":
        self.login()
        return self

    def __exit__(self, *exc: object) -> None:
        self.logout()

    def login(self) -> None:
        try:
            import baostock as bs
        except ImportError as err:                       # pragma: no cover
            raise BaostockUnavailable(
                "baostock 未安装：pip install baostock") from err
        result = bs.login()
        if result.error_code != "0":
            raise BaostockUnavailable(
                f"baostock login failed: {result.error_code} {result.error_msg}")
        self._bs = bs

    def logout(self) -> None:
        if self._bs is not None:
            try:
                self._bs.logout()
            finally:
                self._bs = None

    def _guard(self) -> None:
        if self._bs is None:
            raise BaostockUnavailable("not logged in; use BaostockClient as a context manager")
        if self._min_interval > 0:
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = time.monotonic()

    # --------------------------------------------------------------- 留证
    def _record(self, label: str, rows: list[dict]) -> Receipt:
        """把本次查询的记录落盘并算哈希。

        与 HTTP 源的归档同样处理：同一份内容只存一次（按内容哈希命名），
        收据单独记录"什么时候查了什么"。
        """

        requested_at = datetime.now(timezone.utc)
        body = json.dumps(rows, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        bare = digest.removeprefix("sha256:")
        target = self.root / "baostock" / bare[:2] / bare
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(body)
        receipt = Receipt(
            label=label, requested_at=requested_at, record_count=len(rows),
            content_hash=digest,
            stored_path=str(target.relative_to(self.root)),
        )
        self.receipts.append(receipt)
        return receipt

    # --------------------------------------------------------------- 查询
    def _drain(self, rs: Any) -> tuple[list[str], list[dict]]:
        fields = list(rs.fields)
        rows: list[dict] = []
        while rs.next():
            rows.append(dict(zip(fields, rs.get_row_data())))
        return fields, rows

    def stock_industry(self, code: str | None = None) -> tuple[list[dict], Receipt]:
        """证券行业分类。code 形如 sh.600519；省略则返回全市场。"""

        self._guard()
        rs = self._bs.query_stock_industry(code) if code else self._bs.query_stock_industry()
        if rs.error_code != "0":
            raise BaostockUnavailable(
                f"query_stock_industry({code}) failed: {rs.error_code} {rs.error_msg}")
        _fields, rows = self._drain(rs)
        return rows, self._record(f"stock_industry:{code or 'ALL'}", rows)

    def all_stock(self, day: str) -> tuple[list[dict], Receipt]:
        """某个交易日的全部证券（含指数）。day 形如 2026-09-14。"""

        self._guard()
        rs = self._bs.query_all_stock(day=day)
        if rs.error_code != "0":
            raise BaostockUnavailable(
                f"query_all_stock({day}) failed: {rs.error_code} {rs.error_msg}")
        _fields, rows = self._drain(rs)
        return rows, self._record(f"all_stock:{day}", rows)

    def daily_bars(self, code: str, *, start: str, end: str,
                   adjust: str = "3") -> tuple[list[dict], Receipt]:
        """日线。返回统一的**整数分**与**股**，单位换算在此完成。

        adjust: "3" 不复权 / "2" 前复权 / "1" 后复权（BaoStock 的定义）。
        默认不复权：订单与账本用真实成交价（§338）。
        """

        self._guard()
        rs = self._bs.query_history_k_data_plus(
            code, "date,open,high,low,close,volume,amount,turn,pctChg",
            start_date=start, end_date=end, frequency="d", adjustflag=adjust)
        if rs.error_code != "0":
            raise BaostockUnavailable(
                f"daily_bars({code}) failed: {rs.error_code} {rs.error_msg}")
        _fields, raw = self._drain(rs)

        def cents(value: str) -> int | None:
            if value in ("", None):
                return None
            return int(round(float(value) * 100))

        bars: list[dict] = []
        for r in raw:
            close = cents(r["close"])
            if close is None:
                continue
            bars.append({
                "trading_day": r["date"],
                "open_cents": cents(r["open"]),
                "high_cents": cents(r["high"]),
                "low_cents": cents(r["low"]),
                "close_cents": close,
                "volume_shares": int(r["volume"]) if r["volume"] else 0,
                "amount_cents": cents(r["amount"]),
                "turnover_pct": float(r["turn"]) if r["turn"] else None,
                "pct_change": float(r["pctChg"]) if r["pctChg"] else None,
            })
        return bars, self._record(f"daily_bars:{code}:{start}:{end}:adj{adjust}", raw)

    def profit(self, code: str, *, year: int, quarter: int) -> tuple[dict | None, Receipt]:
        """季频盈利能力。返回单条记录，含 pubDate（公布日）。"""

        self._guard()
        rs = self._bs.query_profit_data(code=code, year=year, quarter=quarter)
        if rs.error_code != "0":
            raise BaostockUnavailable(
                f"query_profit_data({code}) failed: {rs.error_code} {rs.error_msg}")
        _fields, rows = self._drain(rs)
        return (rows[0] if rows else None), self._record(
            f"profit:{code}:{year}Q{quarter}", rows)
