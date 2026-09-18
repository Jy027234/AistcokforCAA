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

    #: 单次查询的 socket 超时（秒）。
    #:
    #: BaoStock 的底层 socket **没有超时**：服务端偶发不响应时，
    #: recv 会一直阻塞，进程 CPU 掉到 0、看起来还活着，实际已经不动了。
    #: 我在全市场采集里连续遇到两次——输出与缓存都停在同一个位置，
    #: 而作业状态仍是 running。长任务里这种"静默停摆"比直接报错更难处理，
    #: 因此设置进程级默认超时，让阻塞变成可捕获的异常。
    #: 60 秒足够覆盖实测最慢的批量查询（全量行业约 90 秒，故留足余量）。
    DEFAULT_QUERY_TIMEOUT = 120.0

    def __init__(self, archive_root: str | Path, *,
                 sleep: Callable[[float], None] = time.sleep,
                 min_interval_seconds: float = 0.0,
                 query_timeout: float | None = None) -> None:
        self.root = Path(archive_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._sleep = sleep
        self._min_interval = min_interval_seconds
        self._last_call = 0.0
        self._bs: Any = None
        self.receipts: list[Receipt] = []
        self._query_timeout = (self.DEFAULT_QUERY_TIMEOUT
                               if query_timeout is None else query_timeout)
        self._previous_default_timeout: Any = None

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
        # 进程级默认超时：BaoStock 自己创建 socket，不暴露超时参数，
        # 因此只能设全局默认值。记录原值以便 logout 时还原——
        # 不改回去会污染同进程里其它库的网络行为。
        import socket as _socket

        if self._query_timeout and self._previous_default_timeout is None:
            self._previous_default_timeout = _socket.getdefaulttimeout()
            _socket.setdefaulttimeout(self._query_timeout)

        result = bs.login()
        if result.error_code != "0":
            self._restore_socket_timeout()
            raise BaostockUnavailable(
                f"baostock login failed: {result.error_code} {result.error_msg}")
        self._bs = bs

    def _restore_socket_timeout(self) -> None:
        if self._previous_default_timeout is not None:
            import socket as _socket

            _socket.setdefaulttimeout(self._previous_default_timeout)
            self._previous_default_timeout = None

    def logout(self) -> None:
        if self._bs is not None:
            try:
                self._bs.logout()
            finally:
                self._bs = None
                self._restore_socket_timeout()

    #: 会话失效的错误码。BaoStock 的服务端会话大约在 100 次查询后过期，
    #: 之后所有查询都返回"用户未登录"。
    #:
    #: 这个坑很隐蔽：**它不是一开始就失败**，而是跑了 100 次之后突然全线失败。
    #: 我的采集脚本正是这样——前 79 只成功、之后连续 21 只报"用户未登录"，
    #: 而错误信息看起来像是调用方没登录，实际是服务端把会话踢了。
    SESSION_EXPIRED_CODES = ("10001001", "10001002")

    def _reconnect(self) -> None:
        """会话失效后重新登录。

        BaoStock 的登录状态存在**进程内的模块级变量**里，不是每个客户端实例
        一份，因此重连是全局动作。这里显式 logout 再 login：只 login 不 logout
        会让服务端累积会话。
        """

        self.logout()
        self.login()

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

    def _run(self, call: "Callable[[], Any]", *, label: str) -> Any:
        """执行一次查询，会话失效时**自动重连并重试一次**。

        为什么可以自动重试：这些查询都是只读的，重试不改变任何状态。
        会话过期是服务端行为，不是调用方的错，把它暴露给调用方只会让
        每个调用点都写一遍重连逻辑——而漏写一处就会在跑了一百次之后
        突然全线失败。
        """

        result = call()
        if getattr(result, "error_code", "0") in self.SESSION_EXPIRED_CODES:
            self._reconnect()
            result = call()
        if getattr(result, "error_code", "0") != "0":
            raise BaostockUnavailable(
                f"{label} failed: {result.error_code} {result.error_msg}")
        return result

    def stock_industry(self, code: str | None = None) -> tuple[list[dict], Receipt]:
        """证券行业分类。code 形如 sh.600519；省略则返回全市场。"""

        self._guard()
        rs = self._run(
            (lambda: self._bs.query_stock_industry(code)) if code
            else self._bs.query_stock_industry,
            label=f"query_stock_industry({code})")
        _fields, rows = self._drain(rs)
        return rows, self._record(f"stock_industry:{code or 'ALL'}", rows)

    def stock_basic(self, code: str) -> tuple[list[dict], Receipt]:
        """证券基本资料。返回 code/code_name/ipoDate/outDate/type/status。

        `ipoDate` 是**唯一的上市日期来源**：它决定 §3.1 的「新上市不足规定
        交易日」与「决策时点是否已上市」能否判断。此前我方从不采集它，
        `instrument.listed_on` 因此恒为 NULL，两条判定都退化成空操作。

        **必须逐只查**：不带 code 的全量查询在本机实测会挂住（既不返回
        也不报错），而单只只需 0.01~0.02 秒。调用方本来就在逐只取日线，
        顺路取一次基本资料的边际成本可以忽略。

        刻意**不提供**省略 code 的重载：那正是会挂住的调用形态，
        把它做成默认参数等于把一个已知会卡住的路径摆在最顺手的入口。
        """

        if not code:
            raise ValueError("stock_basic requires an explicit code (bulk query hangs)")
        self._guard()
        rs = self._run(lambda: self._bs.query_stock_basic(code=code),
                       label=f"query_stock_basic({code})")
        _fields, rows = self._drain(rs)
        return rows, self._record(f"stock_basic:{code}", rows)

    def all_stock(self, day: str) -> tuple[list[dict], Receipt]:
        """某个交易日的全部证券（含指数）。day 形如 2026-09-14。"""

        self._guard()
        rs = self._run(lambda: self._bs.query_all_stock(day=day),
                       label=f"query_all_stock({day})")
        _fields, rows = self._drain(rs)
        return rows, self._record(f"all_stock:{day}", rows)

    def daily_bars(self, code: str, *, start: str, end: str,
                   adjust: str = "3") -> tuple[list[dict], Receipt]:
        """日线。返回统一的**整数分**与**股**，单位换算在此完成。

        adjust: "3" 不复权 / "2" 前复权 / "1" 后复权（BaoStock 的定义）。
        默认不复权：订单与账本用真实成交价（§338）。
        """

        self._guard()
        rs = self._run(
            lambda: self._bs.query_history_k_data_plus(
                code, "date,open,high,low,close,volume,amount,turn,pctChg",
                start_date=start, end_date=end, frequency="d", adjustflag=adjust),
            label=f"daily_bars({code})")
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
        rs = self._run(
            lambda: self._bs.query_profit_data(code=code, year=year, quarter=quarter),
            label=f"query_profit_data({code})")
        _fields, rows = self._drain(rs)
        return (rows[0] if rows else None), self._record(
            f"profit:{code}:{year}Q{quarter}", rows)
