"""从全市场采集结果生成研究池：消除选样偏差。

为什么必须换掉手工池
--------------------
S1 的横截面排名是**百分位**。在 24 只手工挑选的标的上算百分位，
得到的是"这 24 只里的排名"，不是"全市场排名"——选样偏差直接进了
排名，而排名决定买什么。这是当前最影响结论质量的问题。

选池规则（可复核，不按我的偏好）
--------------------------------
  1. 剔除无行情、行情过少、长期停牌的标的；
  2. 剔除 ST / 退市整理（名称含 ST / 退）；
  3. 按**近 20 个交易日成交额中位数**排序取前 N；
  4. 分板块保留：沪深主板进可模拟池，创业板/科创板仅展示。

用中位数而不是均值：个别天的天量成交会把均值抬高，
而"能不能进出"取决于平常的日子，不是最活跃的那天。

用法：
    python tools/build_pool_from_universe.py --size 300
    python tools/build_pool_from_universe.py --size 300 --write
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "deploy" / "agentctl-q0" / "universe-bars.json"
OUT = ROOT / "configs" / "real-pool-csrc.yaml"

#: 需要的最少行情条数。61 个交易日的窗口里，少于这个数说明长期停牌，
#: 把它放进池子会让"近 20 日中位数成交额"建立在不完整的数据上。
MIN_ROWS = 40
LOOKBACK = 20


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=300, help="每个板块最多取多少只")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    if not CACHE.exists():
        print(f"缺少采集缓存：{CACHE}")
        print("先运行：python tools/collect_universe.py")
        return 2

    doc = json.loads(CACHE.read_text(encoding="utf-8"))
    window = doc["window"]
    bars = doc["bars"]
    print(f"缓存：{len(bars)} 只，窗口 {window['first_day']} .. {window['last_day']}"
          f"（{window['trading_days']} 个交易日）")

    # 简称：从行业缓存里取（含 code_name），避免再造一次请求
    names: dict[str, str] = {}
    ind_cache = ROOT / "deploy" / "agentctl-q0" / "baostock-industry-cache.json"
    industries: dict[str, dict] = {}
    if ind_cache.exists():
        payload = json.loads(ind_cache.read_text(encoding="utf-8"))
        for r in payload["records"]:
            market, _, number = r["code"].partition(".")
            iid = market.upper() + "." + number
            names[iid] = r.get("code_name") or ""
            industries[iid] = r

    rows: list[dict] = []
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for iid, info in bars.items():
        series = info.get("rows") or []
        if len(series) < MIN_ROWS:
            skip("行情过少（长期停牌或新上市）")
            continue
        name = (names.get(iid) or "").strip()
        # ST / 退市整理：规范 §4.1 明确要剔除风险警示与退市整理
        if "ST" in name.upper() or "退" in name:
            skip("风险警示或退市整理")
            continue
        recent = series[-LOOKBACK:]
        amounts = [b["amount_cents"] for b in recent if b.get("amount_cents")]
        if not amounts:
            skip("近 20 日无成交额")
            continue
        industry = industries.get(iid) or {}
        rows.append({
            "instrument_id": iid,
            "code": iid[:2].lower() + iid[3:],
            "exchange": info["exchange"],
            "board": info["board"],
            "name": name,
            "rows": len(series),
            "median_amount_cents_20d": int(statistics.median(amounts)),
            "industry": (industry.get("industry") or "").strip(),
            "last_close_cents": series[-1]["close_cents"],
        })

    print(f"\n过滤：保留 {len(rows)} 只，剔除 {sum(skipped.values())} 只")
    for reason, n in sorted(skipped.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>5}  {reason}")

    # 行业缺失的不进池：S1 要求"同行业可比"，缺行业会污染整个横截面
    no_industry = [r for r in rows if not r["industry"]]
    rows = [r for r in rows if r["industry"]]
    if no_industry:
        print(f"  {len(no_industry):>5}  无权威行业分类")

    by_board: dict[str, list[dict]] = {}
    for r in rows:
        by_board.setdefault(r["board"], []).append(r)

    pool: list[dict] = []
    for board, items in sorted(by_board.items()):
        items.sort(key=lambda r: -r["median_amount_cents_20d"])
        picked = items[: args.size]
        pool.extend(picked)
        if picked:
            print(f"  {board:<5} 候选 {len(items):>4} 只，取前 {len(picked):>3}；"
                  f"成交额门槛 {picked[-1]['median_amount_cents_20d'] / 1e10:.2f} 亿元")

    simulatable = [r for r in pool if r["board"] == "MAIN"]
    print(f"\n选池结果：{len(pool)} 只，其中可模拟（沪深主板）{len(simulatable)} 只")
    print(f"  成交额区间：{min(r['median_amount_cents_20d'] for r in pool) / 1e10:.2f}"
          f" ~ {max(r['median_amount_cents_20d'] for r in pool) / 1e10:.2f} 亿元（近 20 日中位数）")

    print(f"  覆盖证监会行业大类：{len({r['industry'][:3] for r in pool})} 个")

    if not args.write:
        print("\n（预览模式，未写入。加 --write 生效）")
        return 0

    payload = {
        "pool_id": f"real-pool-universe-{date.today().isoformat()}",
        "as_of": window["last_day"],
        "source": "deploy/agentctl-q0/universe-bars.json（BaoStock 全市场采集）",
        "selection_rule": (f"剔除无行情/长期停牌/风险警示/无行业分类后，按近 {LOOKBACK}"
                           f" 个交易日成交额**中位数**排序，各板块取前 {args.size} 只"),
        "classification_version": "CSRC-2012",
        "window": window,
        "instruments": pool,
    }
    OUT.write_bytes(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    print(f"\n已写入 {OUT}")
    return 0


def model_ok(pool: list[dict]) -> list[dict]:
    return pool


if __name__ == "__main__":
    raise SystemExit(main())