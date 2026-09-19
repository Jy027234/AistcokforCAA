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
from aquant.operations.pipeline import PipelineBusy, PipelineLock  # noqa: E402

OUT = ROOT / "deploy" / "agentctl-q0" / "universe-bars.json"

#: 补窗口首行前收时向前多取的自然日天数。
#: 挑 10 天是为了跨过停牌：停牌三个交易日就要往前多找几天才碰得到真实收盘价。
LOOKBACK_CALENDAR_DAYS = 10

#: 单只标的的抓取尝试次数与退避基数（秒）。
#: 代理下服务端偶发不响应；不重试会让一次抖动变成永久缺口。
FETCH_ATTEMPTS = 3
FETCH_BACKOFF_SECONDS = 2.0

#: 上市日期的单独尝试次数。它比行情轻得多（实测 0.01~0.02s/只），
#: 因此不跟行情共用重试预算：行情失败会把整只标的记为失败，
#: 而上市日期失败只是「这次没取到」，下次续跑再试。
BASIC_ATTEMPTS = 2

#: 缓存里"行情行是怎么构造的"版本号。**改动行的构造口径就必须 +1**，
#: 否则续跑判据会把用旧口径写出来的行当成已完成，那些行永远修不回来
#: （本工具真的这样错过一次：补了前收却没重写首行的 prev_close_cents）。
#:   v1 -> v2：窗口首行的 prev_close_cents 由"当日开盘价占位"改为
#:             "窗口前最后一个真实收盘价"，并在窗口前多取一小段。
ROWS_FORMAT = 2

# 行里目前只保存成交价、成交量和成交额等不复权日线字段。这个集合单独
# 放出来是为了让增量合并不依赖供应商返回的额外字段（turn/pctChg 等）。
BAR_FIELDS = (
    "trading_day", "open_cents", "high_cents", "low_cents", "close_cents",
    "volume_shares", "amount_cents", "prev_close_cents",
)

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


def _valid_trading_day(value: object) -> bool:
    """判断缓存行是否带有可排序的 ISO 日期。"""

    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _canonical_row(row: object) -> dict | None:
    """清理一条缓存/供应商行情行，但保留未知字段以兼容旧缓存。"""

    if not isinstance(row, dict):
        return None
    day = row.get("trading_day")
    if not _valid_trading_day(day) or row.get("close_cents") is None:
        # BaoStock 的适配层已经过滤无收盘价的行；这里再过滤一次，避免
        # 手工缓存或旧版本缓存把"无成交"伪装成已覆盖日期。
        return None
    return dict(row)


def normalize_rows(rows: object) -> list[dict]:
    """按交易日去重并排序，后出现的同日行覆盖先出现的行。"""

    if not isinstance(rows, list):
        return []
    by_day: dict[str, dict] = {}
    for raw in rows:
        row = _canonical_row(raw)
        if row is not None:
            by_day[row["trading_day"]] = row
    return [by_day[day] for day in sorted(by_day)]


def bar_to_row(bar: object, *, target_days: set[str]) -> dict | None:
    """把供应商日线转换成缓存行，并只保留本次请求的目标交易日。"""

    if not isinstance(bar, dict):
        return None
    day = bar.get("trading_day")
    if day not in target_days:
        return None
    row = {field: bar.get(field) for field in BAR_FIELDS}
    return _canonical_row(row)


def merge_rows(existing: object, incoming: object) -> list[dict]:
    """合并两组行情，供应商本次返回的同日数据优先。"""

    merged = normalize_rows(existing)
    updates = normalize_rows(incoming)
    by_day = {row["trading_day"]: row for row in merged}
    by_day.update({row["trading_day"]: row for row in updates})
    return [by_day[day] for day in sorted(by_day)]


def fetch_ranges_for_entry(
    rows: object,
    first_day: str,
    last_day: str,
    *,
    force_rebuild: bool = False,
) -> list[tuple[str, str]]:
    """返回需要向供应商请求的日期区间。

    正常续跑只请求缓存最后一天之后的尾部；若窗口向前扩展，则只请求
    缓存第一天之前的前缀。中间因停牌/无成交而没有行的日期不在这里重复
    追抓，由上层根据交易日历和供应商状态决定是否需要复核。
    """

    if first_day > last_day:
        return []
    cached_rows = normalize_rows(rows)
    if force_rebuild or not cached_rows:
        return [(
            str(date.fromisoformat(first_day) - timedelta(days=LOOKBACK_CALENDAR_DAYS)),
            last_day,
        )]

    existing_first = cached_rows[0]["trading_day"]
    existing_last = cached_rows[-1]["trading_day"]
    ranges: list[tuple[str, str]] = []

    # 缓存没有覆盖目标窗口的左侧时，向前取一小段来重新建立首行前收。
    if existing_first > first_day:
        prefix_end = min(
            date.fromisoformat(existing_first) - timedelta(days=1),
            date.fromisoformat(last_day),
        )
        if date.fromisoformat(first_day) <= prefix_end:
            ranges.append((
                str(date.fromisoformat(first_day)
                    - timedelta(days=LOOKBACK_CALENDAR_DAYS)),
                str(prefix_end),
            ))

    # 正常日常增量只会走这个尾部区间。起点是已有最后一天之后的自然日，
    # BaoStock 会自行按交易日返回；这样不会无条件重新抓整个历史窗口。
    if existing_last < last_day:
        suffix_start = max(
            date.fromisoformat(existing_last) + timedelta(days=1),
            date.fromisoformat(first_day),
        )
        if suffix_start <= date.fromisoformat(last_day):
            ranges.append((str(suffix_start), last_day))
    return ranges


def recompute_prev_closes(
    rows: object,
    seed_prev: int | None,
) -> tuple[list[dict], int | None]:
    """按排序后的完整序列重建前收，返回序列及其首行前收。"""

    normalized = normalize_rows(rows)
    if not normalized:
        return [], seed_prev
    if seed_prev is None:
        # 旧缓存可能没有单独的元数据，但行本身仍保存了首行前收。
        seed_prev = normalized[0].get("prev_close_cents")
    previous = seed_prev
    for row in normalized:
        row["prev_close_cents"] = previous
        previous = row.get("close_cents")
    return normalized, seed_prev


def seed_from_fetched(
    rows: list[dict], fetched: object, current_seed: int | None,
) -> int | None:
    """若本次请求包含序列最早行之前的收盘，优先用它作为首行前收。"""

    if not rows or not isinstance(fetched, list):
        return current_seed
    first_day = rows[0]["trading_day"]
    before = sorted(
        (
            bar["trading_day"], bar.get("close_cents")
        ) for bar in fetched
        if isinstance(bar, dict)
        and isinstance(bar.get("trading_day"), str)
        and bar["trading_day"] < first_day
        and bar.get("close_cents") is not None
    )
    return before[-1][1] if before else current_seed


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


def _main_unlocked() -> int:
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
        all_days = sorted({
            b["trading_day"] for b in idx
            if isinstance(b, dict) and _valid_trading_day(b.get("trading_day"))
        })
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
        allstock: list[dict] = []
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
        target_window = f"{days[0]}..{days[-1]}"
        target_days = set(days)

        print()
        print("[2] 逐只采集（可中断续跑）")
        t0 = time.time()
        done = skipped = 0
        for i, (code, exchange, board) in enumerate(targets, 1):
            key = internal_id(code)
            cached = bars.get(key) or {}
            cached_rows = normalize_rows(cached.get("rows"))
            # 窗口变化本身不代表要重抓：缓存保存的是逐只的历史序列，
            # 本次只补左侧前缀或右侧尾部。真正改变行构造口径时，旧行不能
            # 与新行混用，才按本次窗口重建。缺失 rows_format 的旧缓存按兼容
            # 路径处理，并在成功写回时补上当前版本。
            format_changed = (
                cached.get("rows_format") is not None
                and cached.get("rows_format") != ROWS_FORMAT
            )
            seeded = (cached.get("prev_close_attempted")
                      and cached.get("window") == target_window
                      and cached.get("rows_format") == ROWS_FORMAT)

            if prev_close_only and seeded:
                skipped += 1
                continue

            force_rebuild = bool(args.refresh or prev_close_only or format_changed)
            base_rows = [] if force_rebuild else cached_rows
            fetch_ranges = fetch_ranges_for_entry(
                base_rows, days[0], days[-1], force_rebuild=force_rebuild)

            if not fetch_ranges:
                # 已覆盖目标尾部时可以跳过网络请求，但仍要规范化旧缓存，
                # 防止重复/乱序行持续污染下游。窗口变化只更新元数据。
                seed_prev = cached.get("prev_close_before_window")
                canonical_rows, seed_prev = recompute_prev_closes(
                    cached_rows, seed_prev)
                entry = dict(cached)
                entry.update({
                    "exchange": exchange,
                    "board": board,
                    "rows": canonical_rows,
                    "window": target_window,
                    "rows_format": ROWS_FORMAT,
                })
                if canonical_rows:
                    entry["prev_close_before_window"] = seed_prev
                bars[key] = entry
                skipped += 1
                continue

            # 一个证券最多会有左前缀和右尾部两个请求；每个区间独立重试，
            # 这样窗口扩展时不会因为重建整段历史而放大供应商压力。
            fetched: list[dict] = []
            fetch_error = ""
            for fetch_start, fetch_end in fetch_ranges:
                got = None
                last_error = ""
                for attempt in range(1, FETCH_ATTEMPTS + 1):
                    try:
                        got, _ = bs.daily_bars(
                            code, start=fetch_start, end=fetch_end, adjust="3")
                        break
                    except Exception as exc:            # noqa: BLE001
                        last_error = f"{type(exc).__name__}: {str(exc)[:80]}"
                        if attempt < FETCH_ATTEMPTS:
                            # 重连是全局动作：会话/连接坏了，单靠重发同一条没用
                            try:
                                bs._reconnect()
                            except Exception:             # noqa: BLE001
                                pass
                            time.sleep(FETCH_BACKOFF_SECONDS * attempt)
                if got is None:
                    fetch_error = last_error or "unknown failure"
                else:
                    fetched.extend(got)

            fetched_rows = [
                row for row in (
                    bar_to_row(bar, target_days=target_days) for bar in fetched
                ) if row is not None
            ]

            if fetch_error:
                # 已完成的区间仍可安全合并到旧缓存；失败区间留在 failed，
                # 下一次运行会按现有最后日期继续补尾部。口径变更时保留旧行，
                # 避免部分请求失败导致已有数据丢失。
                partial_rows = merge_rows(cached_rows, fetched_rows)
                if partial_rows != cached_rows:
                    seed_prev = cached.get("prev_close_before_window")
                    seed_prev = seed_from_fetched(partial_rows, fetched, seed_prev)
                    partial_rows, seed_prev = recompute_prev_closes(
                        partial_rows, seed_prev)
                    entry = dict(cached)
                    entry.update({
                        "exchange": exchange,
                        "board": board,
                        "rows": partial_rows,
                        "window": target_window,
                        "rows_format": (ROWS_FORMAT
                                        if not format_changed else
                                        cached.get("rows_format")),
                        "prev_close_before_window": seed_prev,
                    })
                    bars[key] = entry
                failed[key] = fetch_error
                continue

            # --- 上市日期（只取一次，失败不影响行情） ---
            # `ipoDate` 是判断「上市未满规定交易日」与「决策时点是否已上市」
            # 的唯一依据。此前从不采集，`instrument.listed_on` 恒为 NULL，
            # 于是两条判定都退化成空操作——而账面看不出来。
            #
            # 单独记在 listed_on 字段而不是塞进 rows：它是**逐只**属性，
            # 且必须能在续跑时判定「这只补过了」。
            #
            # **取不到时不要写这个键**。写 None 与"没采过"在缓存里长得一样，
            # 而续跑判据是"键在不在"——写成 None 会让这次失败变成永久结果，
            # 下次续跑直接跳过。这一层 5219 只是逐只查的，代价不小，
            # 因此正常情况下用 `tools/collect_listing_dates.py`（只查研究池，
            # 并且把失败与缺失分开记）。
            listed_on = cached.get("listed_on")
            if listed_on is None:
                for attempt in range(1, BASIC_ATTEMPTS + 1):
                    try:
                        basic, _ = bs.stock_basic(code)
                        hit = next((r for r in basic if r.get("code") == code), None)
                        raw_ipo = (hit or {}).get("ipoDate") or ""
                        # 空串是「没有」而不是一个日期：写成 "" 会让
                        # 下游把它当成有值的字符串。
                        listed_on = raw_ipo.strip() or None
                        break
                    except Exception:                   # noqa: BLE001
                        if attempt < BASIC_ATTEMPTS:
                            time.sleep(FETCH_BACKOFF_SECONDS * attempt)

            rows = merge_rows(base_rows, fetched_rows)
            seed_prev = None if force_rebuild else cached.get(
                "prev_close_before_window")
            seed_prev = seed_from_fetched(rows, fetched, seed_prev)
            rows, seed_prev = recompute_prev_closes(rows, seed_prev)
            entry = dict(cached)
            entry.update({
                "exchange": exchange,
                "board": board,
                "rows": rows,
                # 这个字段描述缓存中最早保存的那一行之前的真实收盘，
                # 保留历史序列后它不再随滚动窗口起点改变。
                "prev_close_before_window": seed_prev,
                "prev_close_attempted": True,
                "window": target_window,
                "rows_format": ROWS_FORMAT,
            })
            if listed_on is not None:
                # 只有真的取到才写（见上：写成 None 会让续跑把它当成已完成）。
                # 下游一律把 None / 缺键当作「未知」，而不是「未上市」。
                entry["listed_on"] = listed_on
            else:
                # 兼容曾经把查询失败写成 None 的旧缓存；缺键才表示下次
                # 仍可重试，不把一次临时失败固化成永久结果。
                entry.pop("listed_on", None)
            bars[key] = entry
            failed.pop(key, None)
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


def main() -> int:
    """跨进程串行更新同一份整体 JSON，避免后写进程覆盖先写结果。"""

    lock_path = OUT.with_suffix(".lock")
    try:
        with PipelineLock(lock_path):
            return _main_unlocked()
    except PipelineBusy as exc:
        print(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
