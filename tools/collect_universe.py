"""采集全市场行情到增量缓存（可中断、可续跑）。

为什么需要这一层
----------------
S1 的横截面排名必须建立在**全市场**上。现在池子只有 24 只、且是我手工挑的，
所以"排名百分位"实际是"这 24 只里的排名"——选样偏差直接进了排名，
而排名又决定买什么。要消除它，只能把池子放到全市场。

为什么不直接建快照
------------------
全市场 4805 只 × 61 个交易日要抓十几到几十分钟，中途还可能被服务端限速。
把采集与建快照分开，采集结果落成**可复核的缓存**：
  * 逐只写入，中断后续跑不必重头；
  * 缓存带抓取时间与内容哈希，可作为证据引用；
  * 建快照时想取子集（例如只取沪深主板）不必重新联网。

用法：
    python tools/collect_universe.py --limit 50        # 试跑
    python tools/collect_universe.py                   # 全市场（可重复运行续跑）
    python tools/collect_universe.py --refresh         # 忽略已有缓存全部重抓

输出：deploy/agentctl-q0/universe-bars.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.baostock import BaostockClient  # noqa: E402

OUT = ROOT / "deploy" / "agentctl-q0" / "universe-bars.json"

#: 代码前缀 -> 板块。
#:
#: 注意代码形态：BaoStock 给的是 **sh.600519**（带点），
#: 而 T6 的研究池配置用的是腾讯风格 sh600519（不带点）。
#: 两处形态不同，前缀表必须跟着形态写——我第一版照抄了不带点的表，
#: 结果 4805 只一只都没匹配上，而脚本"成功"退出、缓存里 0 条。
#: 这类"空结果却零错误"的失败最难发现，因此下面显式断言非空。
BOARD_BY_PREFIX = {
    "sh.60": ("SSE", "MAIN"), "sh.68": ("SSE", "STAR"),
    "sz.00": ("SZSE", "MAIN"), "sz.30": ("SZSE", "GEM"),
}


def board_of(code: str) -> tuple[str, str] | None:
    for prefix, listing in BOARD_BY_PREFIX.items():
        if code.startswith(prefix):
            return listing
    return None


def internal_id(code: str) -> str:
    """sh.600519 -> SH.600519"""

    market, _, number = code.partition(".")
    return market.upper() + "." + number


def load_cache() -> dict:
    if OUT.exists():
        return json.loads(OUT.read_text(encoding="utf-8"))
    return {"created_at": None, "window": None, "bars": {}, "failed": {}}


def save_cache(doc: dict) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc["updated_at"] = datetime.now(timezone.utc).isoformat()
    # 先写临时文件再原子改名：中断时不会留下半截 JSON
    tmp = OUT.with_suffix(".tmp")
    tmp.write_bytes(json.dumps(doc, ensure_ascii=False).encode("utf-8"))
    tmp.replace(OUT)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只抓前 N 只（试跑用）")
    ap.add_argument("--window", type=int, default=61, help="交易日数量")
    ap.add_argument("--refresh", action="store_true", help="忽略已有缓存")
    ap.add_argument("--retry-failed", action="store_true",
                    help="只重试之前失败的标的（会话过期等可恢复错误）")
    ap.add_argument("--boards", default="MAIN,GEM,STAR",
                    help="逗号分隔的板块；默认全部")
    args = ap.parse_args()

    wanted_boards = {b.strip().upper() for b in args.boards.split(",") if b.strip()}

    doc = load_cache() if not args.refresh else {
        "created_at": None, "window": None, "bars": {}, "failed": {}}
    bars: dict[str, list] = doc.setdefault("bars", {})
    failed: dict[str, str] = doc.setdefault("failed", {})

    with BaostockClient(ROOT / "deploy" / "agentctl-q0" / "baostock-archive") as bs:
        print("[1] 取交易日历与证券清单")
        # 用指数日线确定交易日（与 T6 一致，不用工作日近似）
        idx, _ = bs.daily_bars("sh.000001", start=str(date.today() - timedelta(days=200)),
                               end=str(date.today()))
        days = [b["trading_day"] for b in idx][-args.window:]
        if len(days) < args.window:
            print(f"  日历不足：只有 {len(days)} 天")
            return 2
        print(f"  窗口 {days[0]} .. {days[-1]}（{len(days)} 天）")

        allstock, receipt = bs.all_stock(days[-1])
        print(f"  证券清单 {len(allstock)} 条（收据 {receipt.content_hash[:23]}...）")

        targets: list[tuple[str, str, str]] = []
        for r in allstock:
            code = r["code"]
            listing = board_of(code)
            if listing is None:
                continue          # 指数、基金、北交所等：不在本期范围
            exchange, board = listing
            if board not in wanted_boards:
                continue
            targets.append((code, exchange, board))

        if not targets:
            # 空结果必须报错：静默地"采集 0 只"看起来像成功，
            # 但下游会拿到一份空快照，而错误信息里什么线索都没有。
            print("  待采集 0 只——代码形态与板块前缀表不匹配。")
            print(f"  清单代码样例: {[r['code'] for r in allstock[:5]]}")
            return 2

        if args.retry_failed:
            # 只重试之前失败的：会话过期是**可恢复**错误，让它永久留在
            # failed 里，会把一次临时故障变成永久的数据缺口。
            targets = [t for t in targets if internal_id(t[0]) in failed]
            print(f"  只重试失败标的：{len(targets)} 只")
            for t in targets:
                failed.pop(internal_id(t[0]), None)

        if args.limit:
            targets = targets[: args.limit]
        print(f"  待采集 {len(targets)} 只（板块 {sorted(wanted_boards)}）")
        if len(targets) < 100:
            print(f"  样例: {[t[0] for t in targets[:5]]}")

        doc["created_at"] = doc.get("created_at") or datetime.now(timezone.utc).isoformat()
        doc["window"] = {"first_day": days[0], "last_day": days[-1],
                         "trading_days": len(days)}
        doc["calendar_source"] = "baostock:sh.000001"

        print()
        print("[2] 逐只采集（可中断续跑）")
        t0 = time.time()
        done = skipped = 0
        for i, (code, exchange, board) in enumerate(targets, 1):
            key = internal_id(code)
            if key in bars and not args.refresh and bars[key]:
                skipped += 1
                continue
            try:
                got, _ = bs.daily_bars(code, start=days[0], end=days[-1], adjust="3")
            except Exception as exc:                    # noqa: BLE001
                failed[key] = f"{type(exc).__name__}: {str(exc)[:80]}"
                continue
            by_day = {b["trading_day"]: b for b in got}
            rows = []
            prev = None
            for day in days:
                bar = by_day.get(day)
                if bar is None or bar["close_cents"] is None:
                    continue        # 停牌/无成交：缺失即缺失
                rows.append({
                    "trading_day": day,
                    "open_cents": bar["open_cents"],
                    "high_cents": bar["high_cents"],
                    "low_cents": bar["low_cents"],
                    "close_cents": bar["close_cents"],
                    "volume_shares": bar["volume_shares"],
                    "amount_cents": bar["amount_cents"],
                    "prev_close_cents": prev if prev else bar["open_cents"],
                })
                prev = bar["close_cents"]
            bars[key] = {"exchange": exchange, "board": board, "rows": rows}
            done += 1

            # 每 25 只落盘一次：崩溃或被杀时最多丢 25 只，
            # 而 100 只的间隔在限速下可能长达一分钟，看不出进度。
            if i % 25 == 0 or i == len(targets):
                elapsed = time.time() - t0
                rate = elapsed / max(done, 1)
                remain = (len(targets) - i) * rate / 60
                print(f"  {i}/{len(targets)}  已采 {done}  跳过 {skipped}  "
                      f"失败 {len(failed)}  {rate:.2f}s/只  预计剩余 {remain:.0f} 分钟")
                save_cache(doc)

        save_cache(doc)

    total_rows = sum(len(v["rows"]) for v in bars.values())
    print()
    print("=" * 62)
    print(f"采集完成：{len(bars)} 只，{total_rows} 条行情，失败 {len(failed)} 只")
    print(f"缓存：{OUT}（{OUT.stat().st_size / 1024 / 1024:.1f} MB）")
    if failed:
        print("失败样例：")
        for k, v in list(failed.items())[:5]:
            print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
