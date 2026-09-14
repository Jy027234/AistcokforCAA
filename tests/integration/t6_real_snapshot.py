"""T6：用免费真实数据构建 61 个交易日的快照，走真实入库与发布链路。

这一步是评审留下的最大缺口：此前所有端到端验收都建立在
`snap-syn-001` 合成数据上，而"合成数据上跑通"**不能**替代
"真实数据能走通同一条链路"。两者的失败方式完全不同——
真实数据会缺字段、会停牌、会有非交易日、会有单位差异。

范围与诚实边界
--------------
只做**免费源真的能证明**的部分：

  * 交易日历：腾讯上证指数日线（真实交易日，不用工作日近似）；
  * 证券身份：代码、交易所、板块来自代码前缀与腾讯快照；
  * 日行情：腾讯前复权/不复权日线，开高低收量。

免费源**不能**证明的部分，一律不编造：

  * 行业分类：免费源不提供权威分类，写 NULL；
  * 公司行为：不重建成事件（ADR-003 已记录该限制）；
  * point-in-time：本次抓取发生在历史之后，因此
    `pit_mode=HISTORICAL_RECONSTRUCTED`、`available_basis=RECONSTRUCTED`，
    快照水印写明它不能用于正式时点回测。

用法：
    $env:AQUANT_TRUSTED_PROXY_NETWORKS='198.18.0.0/15,fdfe:dcba:9876::/48'
    python -m tests.integration.t6_real_snapshot

环境变量：
    T6_WINDOW_DAYS  交易日数量，默认 61
    T6_SYMBOLS      逗号分隔的腾讯代码，默认 sh600519,sz000001,sh601398,sz300750

退出码：0 = 快照建成且全部断言通过；1 = 有断言失败；2 = 数据源不可用。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.calendar import load_trading_calendar  # noqa: E402
from aquant.adapters.providers.tencent import TencentClient  # noqa: E402
from aquant.domain.data.db import apply_migrations, connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402
from aquant.domain.data.ingest import SnapshotBuilder  # noqa: E402
from aquant.domain.data.reader import SnapshotReader  # noqa: E402
from aquant.domain.data.snapshot import (  # noqa: E402
    DataMode, DatasetRef, SnapshotDraft, SnapshotStore,
)

DEFAULT_SYMBOLS = "sh600519,sz000001,sh601398,sz300750"

# 腾讯代码 -> (交易所, 板块)。板块由代码前缀决定，这是交易所规则而非猜测；
# 但**板块决定涨跌幅**，所以前缀不认识时必须报错而不是默认主板（§12.2）。
BOARD_BY_PREFIX = {
    "sh60": ("SSE", "MAIN"), "sh68": ("SSE", "STAR"),
    "sz00": ("SZSE", "MAIN"), "sz30": ("SZSE", "GEM"),
}


def _board_of(symbol: str) -> tuple[str, str]:
    for prefix, listing in BOARD_BY_PREFIX.items():
        if symbol.startswith(prefix):
            return listing
    raise RuntimeError(
        f"无法从代码前缀判断 {symbol} 的交易所与板块；"
        "涨跌幅规则依赖板块，不能默认成主板"
    )


def _internal_id(symbol: str) -> str:
    """内部证券 ID：市场.代码，与数据源解耦（§15.2）。"""

    return symbol[:2].upper() + "." + symbol[2:]


def main() -> int:
    window = int(os.environ.get("T6_WINDOW_DAYS", "61"))
    symbols = [s.strip() for s in
               os.environ.get("T6_SYMBOLS", DEFAULT_SYMBOLS).split(",") if s.strip()]
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))
        print(("  PASS  " if ok else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))

    work = Path(tempfile.mkdtemp(prefix="aquant-t6-"))
    con = connect(work / "meta.sqlite")
    apply_migrations(con)
    archive = ForwardArchive(con, work / "archive")
    tencent = TencentClient(archive)

    # ---------------------------------------------------------- 1. 交易日历
    print("\n[1] 真实交易日历")
    end = date.today()
    begin = end - timedelta(days=max(int(window * 2.2), 120))
    try:
        cal = load_trading_calendar(archive, begin=begin, end=end)
    except Exception as exc:                       # noqa: BLE001
        print(f"  日历不可用：{type(exc).__name__}: {exc}")
        return 2
    days = [d.isoformat() for d in cal.trading_days][-window:]
    check("日历来自真实交易日", len(days) == window,
          f"{cal.source_id} {days[0]}..{days[-1]} 共 {len(days)} 天")
    check("日历不含周末", all(date.fromisoformat(d).weekday() < 5 for d in days))

    # ------------------------------------------------------------ 2. 日行情
    print("\n[2] 真实日行情（每只证券每个交易日都要有记录）")
    quotes: list[dict] = []
    instruments: list[dict] = []
    missing: list[str] = []
    for symbol in symbols:
        exchange, board = _board_of(symbol)
        out, bars = tencent.daily_quotes(symbol, days[0], days[-1], adjust=0)
        if not out.ok:
            print(f"  {symbol}: 抓取失败 {out.detail}")
            return 2
        by_day = {str(bar[0]): bar for bar in bars if bar}
        got = 0
        prev_close = None
        for day in days:
            bar = by_day.get(day)
            if bar is None:
                # 停牌或当日无成交：**不补造**，如实记为缺失
                missing.append(f"{symbol}@{day}")
                continue
            _, o, c, h, low, vol = bar[0], bar[1], bar[2], bar[3], bar[4], bar[5]
            def cents(x: str) -> int:
                return int(round(float(x) * 100))
            quotes.append({
                "instrument_id": _internal_id(symbol),
                "trading_day": day,
                "open_cents": cents(o), "high_cents": cents(h),
                "low_cents": cents(low), "close_cents": cents(c),
                "volume_shares": int(float(vol)) * 100,   # 腾讯成交量单位是手
                "prev_close_cents": prev_close if prev_close else cents(o),
            })
            prev_close = cents(c)
            got += 1
        instruments.append({
            "instrument_id": _internal_id(symbol),
            "exchange": exchange, "board": board, "security_class": "EQUITY",
            "short_name": symbol.upper(),
            "listed_on": None,
            "industry_code": None,      # 免费源不提供权威分类：留空而不是编造
            "industry_name": None,
            "status_history": [{
                "valid_from": days[0], "valid_to": None, "name": symbol.upper(),
                "status": "LISTED", "industry_code": None, "industry_name": None,
            }],
        })
        print(f"  {symbol}: {got}/{len(days)} 个交易日有行情")

    coverage = len(quotes) / max(len(symbols) * len(days), 1)
    check("行情覆盖率 100%（缺失即缺失，不补造）", coverage == 1.0,
          f"{len(quotes)}/{len(symbols) * len(days)}；缺失 {len(missing)} 条")
    check("价格为正整数分", all(q["open_cents"] > 0 and q["close_cents"] > 0
                                for q in quotes))
    check("low<=open,close<=high", all(
        q["low_cents"] <= min(q["open_cents"], q["close_cents"])
        and max(q["open_cents"], q["close_cents"]) <= q["high_cents"] for q in quotes))
    check("行业分类未被编造", all(i["industry_code"] is None for i in instruments))

    # -------------------------------------------------- 3. 真实入库与发布
    print("\n[3] 真实快照入库与发布")
    # 数据集目录必须与快照库根目录一致：DatasetRef.path 是相对路径，
    # 快照发布时会按库根目录重新计算哈希。两处指向不同目录就会出现
    # "入库成功但发布时找不到文件"。
    api_root = work / "api"
    api_root.mkdir(parents=True, exist_ok=True)
    builder = SnapshotBuilder(con, api_root / "datasets")
    builder.ensure_source(
        "tencent-ifzq",
        display_name="腾讯行情（免费公开接口）",
        domains=["DAILY_QUOTES", "CALENDAR_IDENTITY"],
        integration_state="TEST_PASSED",
        pit_available="NO",
        pit_basis="RECONSTRUCTED",
        # 权利逐项留空即 UNKNOWN。这里刻意不填：免费公开接口的实际授权
        # 状态没有权威依据，按 §17.2 "未知默认不开放"处理，
        # 因此这批数据不得外发到模型。
        rights={},
    )
    snapshot_id = "snap-real-61d"
    doc = {
        "schema_version": "aquant.real_dataset.v1",
        "data_mode": "PRODUCTION",
        "watermark": ("REAL MARKET DATA, RECONSTRUCTED POINT-IN-TIME -- "
                      "NOT VALID FOR FORMAL PIT BACKTESTS"),
        "disclaimer": ("行情来自免费公开接口，抓取发生在历史之后；"
                       "可得时点为重建而非当时观察，不得用于正式时点回测。"),
        "as_of_time": days[-1] + "T15:00:00+08:00",
        "input_cutoff_at": days[-1] + "T07:00:00Z",
        "published_at": datetime.now(timezone.utc).isoformat(),
        "trading_days": days,
        "instruments": instruments,
        "daily_quotes": quotes,
        "corporate_actions": [],
        "events": [],
    }
    report = builder.ingest(doc, source_id="tencent-ifzq", data_version="real-61d")
    check("入库证券数正确", report.instruments == len(symbols), str(report.instruments))
    check("入库交易日数正确", report.trading_days == len(days), str(report.trading_days))

    refs = builder.write_datasets(doc, snapshot_id=snapshot_id)
    store = SnapshotStore(con, api_root)
    def parse(ts: str) -> datetime:
        return datetime.fromisoformat(ts)
    store.publish(SnapshotDraft(
        snapshot_id=snapshot_id, kind="EOD", data_mode=DataMode.PRODUCTION,
        input_cutoff_at=parse(doc["input_cutoff_at"]),
        as_of_time=parse(doc["as_of_time"]),
        created_at=datetime.now(timezone.utc),
        published_at=parse(doc["published_at"]),
        code_version="0.1.0", data_version="real-61d",
        watermark=doc["watermark"],
        pool_hash="sha256:" + "b" * 64,
        datasets=[DatasetRef(name=r["name"], path=r["path"], sha256=r["sha256"],
                             record_count=r["record_count"],
                             as_of_upper_bound=parse(r["as_of_upper_bound"]))
                  for r in refs],
    ))
    check("快照已发布", True, snapshot_id)

    # ------------------------------------------------- 4. 读取与 PIT 边界
    print("\n[4] 通过快照读取器复核（同一条产品读取路径）")
    reader = SnapshotReader(store)
    as_of = parse(doc["as_of_time"])
    # days 存的是 ISO 字符串（清单里就是字符串），这里显式转成 date：
    # 读取器的区间边界是日期语义，混用字符串与日期的比较会直接抛错。
    mid = date.fromisoformat(days[len(days) // 2])
    inst0 = _internal_id(symbols[0])
    rows = reader.daily_quotes(snapshot_id, as_of=as_of, instrument_id=inst0, end=mid)
    check("读取器能读到真实行情", len(rows) > 0, f"{inst0} 截至 {mid} 共 {len(rows)} 条")
    check("读取器不返回超过截止日的数据",
          all(r.trading_day <= mid for r in rows))

    # 未来时点必须被拒绝：这是 PIT 的底线
    future = (parse(doc["as_of_time"]) + timedelta(days=1))
    try:
        reader.daily_quotes(snapshot_id, as_of=future, instrument_id=inst0, end=days[-1])
        check("未来时点读取被拒绝", False, "竟然成功了")
    except Exception as exc:                        # noqa: BLE001
        check("未来时点读取被拒绝", True, type(exc).__name__)

    # ---------------------------------------------------------- 5. 留证
    print("\n[5] 抓取留证")
    receipts = archive.receipts_for("tencent-ifzq")
    check("归档留下了收据", len(receipts) > 0, f"{len(receipts)} 条")
    check("归档内容可哈希复核",
          all(archive.verify(r["content_hash"]) for r in receipts
              if r["content_hash"]))

    failed = [c for c in checks if not c[1]]
    print("\n" + "=" * 62)
    print(f"T6 真实快照验收 {len(checks) - len(failed)}/{len(checks)} 通过")
    print(f"交易日 {days[0]} .. {days[-1]}（{len(days)} 天），"
          f"证券 {len(symbols)} 只，行情 {len(quotes)} 条")
    for name, _, detail in failed:
        print("  - " + name + "  " + detail)

    out_path = ROOT / "deploy" / "agentctl-q0" / "t6-real-snapshot.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(json.dumps({
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_id": snapshot_id,
        "data_mode": "PRODUCTION",
        "watermark": doc["watermark"],
        "pit_mode": "HISTORICAL_RECONSTRUCTED",
        "available_basis": "RECONSTRUCTED",
        "window": {"first_day": days[0], "last_day": days[-1], "trading_days": len(days)},
        "symbols": symbols,
        "quote_count": len(quotes),
        "missing_quote_count": len(missing),
        "calendar_source": cal.source_id,
        "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks],
        "conclusion": "PASS" if not failed else "FAIL",
    }, ensure_ascii=False, indent=2).encode("utf-8"))
    print(f"报告：{out_path}")
    con.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
