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

#: 补窗口首行前收时向前多取的自然日天数。
#: 挑 10 天是为了跨过停牌：停牌三个交易日就要往前多找几天才碰得到真实收盘价。
LOOKBACK_CALENDAR_DAYS = 10

#: 单只标的的抓取尝试次数与退避基数（秒）。
#: 代理下服务端偶发不响应；不重试会让一次抖动变成永久缺口。
FETCH_ATTEMPTS = 3
FETCH_BACKOFF_SECONDS = 2.0

#: 缓存里"行情行是怎么构造的"版本号。**改动行的构造口径就必须 +1**，
#: 否则续跑判据会把用旧口径写出来的行当成已完成，那些行永远修不回来
#: （本工具真的这样错过一次：补了前收却没重写首行的 prev_close_cents）。
#:   v1 -> v2：窗口首行的 prev_close_cents 由"当日开盘价占位"改为
#:             "窗口前最后一个真实收盘价"，并在窗口前多取一小段。
ROWS_FORMAT = 2

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


def baostock_code(internal: str) -> str:
    """SH.600519 -> sh.600519（internal_id 的逆变换）。"""

    market, _, number = internal.partition(".")
    return market.lower() + "." + number


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
    ap.add_argument("--prev-close-only", action="store_true",
                    help="补齐窗口首行前收，并按 --end 重写窗口内的行情行（可续跑）")
    ap.add_argument("--pool", default=None,
                    help="只处理该研究池配置里的标的（配合 --prev-close-only 用）。"
                         "全市场逐只补前收要数小时，而快照只用到池内标的——"
                         "把范围收窄到真正需要的那些。")
    ap.add_argument("--start", default=None,
                    help="窗口第一天（YYYY-MM-DD）。**增量更新必须指定**，"
                         "否则窗口跟着当天日期滑动，两次运行的日期范围不同。")
    ap.add_argument("--end", default=None,
                    help="窗口最后一天（YYYY-MM-DD）。默认今天。"
                         "**建快照必须钉住它**：否则窗口跟着当天日期滑动，"
                         "同一个脚本今天和明天跑出来的快照不是同一份数据。")
    args = ap.parse_args()
    prev_close_only = args.prev_close_only

    wanted_boards = {b.strip().upper() for b in args.boards.split(",") if b.strip()}

    doc = load_cache() if not args.refresh else {
        "created_at": None, "window": None, "bars": {}, "failed": {}}
    bars: dict[str, list] = doc.setdefault("bars", {})
    failed: dict[str, str] = doc.setdefault("failed", {})

    with BaostockClient(ROOT / "deploy" / "agentctl-q0" / "baostock-archive") as bs:
        print("[1] 取交易日历与证券清单")
        # 用指数日线确定交易日（与 T6 一致，不用工作日近似）
        end_day = date.fromisoformat(args.end) if args.end else date.today()
        idx, _ = bs.daily_bars("sh.000001", start=str(end_day - timedelta(days=800)),
                               end=str(end_day))
        all_days = [b["trading_day"] for b in idx]
        if args.start:
            # 显式钉住窗口起点。日常增量必须用它：不指定时窗口是
            # "最后 N 个交易日"，**跟着当天日期滑动**——同一个脚本今天和
            # 明天跑出来覆盖的日期范围不同，快照之间就没法比较。
            days = [d for d in all_days if args.start <= d <= str(end_day)]
            if not days:
                print(f"  指定的起点 {args.start} 之后没有交易日")
                return 2
        else:
            days = all_days[-args.window:]
            if len(days) < args.window:
                print(f"  日历不足：只有 {len(days)} 天")
                return 2
        print(f"  窗口 {days[0]} .. {days[-1]}（{len(days)} 天）")

        targets: list[tuple[str, str, str]] = []
        if args.pool:
            # 池文件本身就是标的清单，用它就不必再调 all_stock——
            # 那个批量接口在代理下偶发超时，而它的结果只用得上"代码+板块"，
            # 池文件里本来就有，且更权威（池是研究范围的唯一来源）。
            pool_doc = json.loads(Path(args.pool).read_text(encoding="utf-8"))
            for i in pool_doc["instruments"]:
                exchange, board = i["exchange"], i["board"]
                if board not in wanted_boards:
                    continue
                targets.append((baostock_code(i["instrument_id"]), exchange, board))
            print(f"  按研究池 {pool_doc['pool_id']}：{len(targets)} 只")
        else:
            allstock, receipt = bs.all_stock(days[-1])
            print(f"  证券清单 {len(allstock)} 条（收据 {receipt.content_hash[:23]}...）")
            for r in allstock:
                code = r["code"]
                listing = board_of(code)
                if listing is None:
                    continue      # 指数、基金、北交所等：不在本期范围
                exchange, board = listing
                if board not in wanted_boards:
                    continue
                targets.append((code, exchange, board))

        if not targets:
            # 空结果必须报错：静默地"采集 0 只"看起来像成功，
            # 但下游会拿到一份空快照，而错误信息里什么线索都没有。
            print("  待采集 0 只——代码形态与板块前缀表不匹配，或池过滤没匹配上。")
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
            cached = bars.get(key) or {}
            already = bool(cached.get("rows"))
            # 续跑标记与"值是否为 None"分开：新股补出来就是 None，
            # 若拿值当标记，每次续跑都会把它们重抓一遍。
            # 续跑判据必须同时带上"窗口是什么"和"行是怎么构造的"：
            # 只记"补过了"会让用错误窗口或旧口径跑出来的结果被永久当成已完成
            # （本工具真的这样错过一次）。
            target_window = f"{days[0]}..{days[-1]}"
            seeded = (cached.get("prev_close_attempted")
                      and cached.get("window") == target_window
                      and cached.get("rows_format") == ROWS_FORMAT)
            if prev_close_only:
                if seeded:
                    skipped += 1
                    continue
            elif already and not args.refresh:
                skipped += 1
                continue
            # 多取窗口起始日**之前**的一小段：窗口首行需要一个真实的前收
            # 才能判定"开盘是否即涨停"。原先首行用当日开盘价当占位，于是
            # 首行永远算不出涨停——把"未知"当成了"不是"。多取几天是为了
            # 跨过停牌，取其中最后一根真实收盘价。
            fetch_start = str(date.fromisoformat(days[0])
                              - timedelta(days=LOOKBACK_CALENDAR_DAYS))
            # 瞬时故障必须重试，否则一次抖动就变成永久数据缺口。
            # 实测：代理下服务端偶发不响应，baostock 把 socket 超时吞成 None，
            # 调用方随后崩在 rs.fields 上；重连后同一只往往一次就成功。
            got = None
            last_error = ""
            for attempt in range(1, FETCH_ATTEMPTS + 1):
                try:
                    got, _ = bs.daily_bars(code, start=fetch_start, end=days[-1],
                                           adjust="3")
                    break
                except Exception as exc:                # noqa: BLE001
                    last_error = f"{type(exc).__name__}: {str(exc)[:80]}"
                    if attempt < FETCH_ATTEMPTS:
                        # 重连是全局动作：会话/连接坏了，单靠重发同一条没用
                        try:
                            bs._reconnect()
                        except Exception:               # noqa: BLE001
                            pass
                        time.sleep(FETCH_BACKOFF_SECONDS * attempt)
            if got is None:
                failed[key] = last_error or "unknown failure"
                continue
            before = [b for b in got if b["trading_day"] < days[0]
                      and b.get("close_cents") is not None]
            by_day = {b["trading_day"]: b for b in got}
            rows = []
            for day in days:
                bar = by_day.get(day)
                if bar is None or bar["close_cents"] is None:
                    continue            # 停牌/无成交：缺失即缺失
                rows.append({
                    "trading_day": day,
                    "open_cents": bar["open_cents"],
                    "high_cents": bar["high_cents"],
                    "low_cents": bar["low_cents"],
                    "close_cents": bar["close_cents"],
                    "volume_shares": bar["volume_shares"],
                    "amount_cents": bar["amount_cents"],
                    "prev_close_cents": None,   # 下面统一回填，避免两处口径
                })
            # 首行的前收取自窗口之前那一小段（真实收盘价）；确实没有就是 None。
            # 用真实前收而不是当日开盘价：否则首行的"开盘是否即涨停"
            # 永远算不出 True——把"未知"当成了"不是"。
            seed_prev = before[-1]["close_cents"] if before else None
            prev = seed_prev
            for row in rows:
                row["prev_close_cents"] = prev
                prev = row["close_cents"]
            bars[key] = {
                "exchange": exchange, "board": board, "rows": rows,
                # 窗口首行的前收只能来自窗口之前那一小段。
                # 之前确实没有行情（新股）就写 None：未知就是未知，不能拿
                # 开盘价冒充前收——那会让"是否涨停"这个判断永远偏向否。
                "prev_close_before_window": seed_prev,
                "prev_close_attempted": True,
                "window": target_window,
                "rows_format": ROWS_FORMAT,
            }
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
