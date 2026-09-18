"""采集研究池的上市日期（BaoStock query_stock_basic 的 ipoDate）。

为什么单独一个脚本
------------------
上市日期是**静态参考事实**，不是每天滚动的行情。把它塞进
`universe-bars.json` 会带来两个问题：该缓存有「已补过就跳过」的续跑判据，
老缓存永远不会被补上（实测：5219 条全部没有该字段）；而且它属于
「为所有证券都要重抓一遍」的形态，而真正需要的只有研究池里的那些。

因此这里只取研究池标的（几百只），逐只查询 0.01~0.02 秒。
结果写回研究池文件，并由 `t10_universe_snapshot.py` 带进快照。

用法：
    python tools/collect_listing_dates.py            # 预览
    python tools/collect_listing_dates.py --write    # 写入研究池

退出码：0 全部命中；1 有标的不存在于 BaoStock；2 数据源不可用。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.baostock import BaostockClient  # noqa: E402

POOL = ROOT / "configs" / "real-pool-csrc.yaml"
ARCHIVE = ROOT / "deploy" / "agentctl-q0" / "baostock-listing-archive"
CACHE = ROOT / "deploy" / "agentctl-q0" / "listing-dates.json"
ATTEMPTS = 3
BACKOFF = 2.0


def baostock_code(internal: str) -> str:
    """SH.600519 -> sh.600519。与研究池里的 code 字段同源。"""

    return internal[:2].lower() + "." + internal[3:]


def sane(ipo: str, *, today: date) -> str | None:
    """只接受形状正确且不在未来的日期。

    一个明显不可能的值（比如 1900-01-01 或未来的日期）如果被直接写进
    快照，会让「上市未满 120 个交易日」静默地判错——而错误方向取决于
    值是偏大还是偏小，事后很难发现。这里宁可不写。
    """

    text = (ipo or "").strip()
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    if parsed > today:
        return None
    if parsed.year < 1990:      # 沪深交易所 1990-11-26 开市
        return None
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="写回研究池；默认只预览")
    ap.add_argument("--pool", default=str(POOL))
    args = ap.parse_args()

    pool_path = Path(args.pool)
    pool = json.loads(pool_path.read_text(encoding="utf-8"))
    instruments = pool["instruments"]
    print(f"研究池 {pool['pool_id']}：{len(instruments)} 只")

    if CACHE.exists():
        cached = json.loads(CACHE.read_text(encoding="utf-8"))
    else:
        cached = {"by_instrument": {}, "failed": {}}
    by_inst: dict[str, str] = cached.setdefault("by_instrument", {})
    failed: dict[str, str] = cached.setdefault("failed", {})

    today = date.today()
    todo = [i for i in instruments if i["instrument_id"] not in by_inst]
    print(f"已有 {len(instruments) - len(todo)} 只，待取 {len(todo)} 只")

    if todo:
        with BaostockClient(ARCHIVE) as bs:
            t0 = time.time()
            for n, entry in enumerate(todo, 1):
                iid = entry["instrument_id"]
                code = baostock_code(iid)
                got = None
                for attempt in range(1, ATTEMPTS + 1):
                    try:
                        rows, _ = bs.stock_basic(code)
                        hit = next((r for r in rows if r.get("code") == code), None)
                        if hit is None:
                            failed[iid] = "not present in stock_basic"
                            break
                        value = sane(hit.get("ipoDate") or "", today=today)
                        if value is None:
                            failed[iid] = f"unusable ipoDate: {hit.get('ipoDate')!r}"
                            break
                        got = value
                        failed.pop(iid, None)
                        break
                    except Exception as exc:            # noqa: BLE001
                        if attempt < ATTEMPTS:
                            time.sleep(BACKOFF * attempt)
                        else:
                            failed[iid] = f"{type(exc).__name__}: {str(exc)[:70]}"
                if got:
                    by_inst[iid] = got
                if n % 50 == 0 or n == len(todo):
                    rate = (time.time() - t0) / n
                    print(f"  {n}/{len(todo)}  {rate:.2f}s/只  "
                          f"预计剩余 {(len(todo) - n) * rate / 60:.1f} 分钟")
                    CACHE.write_bytes(json.dumps(cached, ensure_ascii=False,
                                                 indent=2).encode("utf-8"))

    CACHE.write_bytes(json.dumps(cached, ensure_ascii=False,
                                 indent=2).encode("utf-8"))

    missing = [i["instrument_id"] for i in instruments
               if i["instrument_id"] not in by_inst]
    print(f"\n命中 {len(by_inst)} 只；未命中 {len(missing)} 只")
    for iid in missing[:8]:
        print(f"  {iid}: {failed.get(iid, 'unknown')}")
    print(f"缓存：{CACHE}")

    if not args.write:
        print("\n（预览模式，未写入。加 --write 生效）")
        return 1 if missing else 0

    changed = 0
    for entry in instruments:
        value = by_inst.get(entry["instrument_id"])
        if value and entry.get("listed_on") != value:
            entry["listed_on"] = value
            changed += 1
        elif value:
            entry.setdefault("listed_on", value)
    payload = dict(pool)
    payload["listing_date_source"] = (
        "BaoStock query_stock_basic ipoDate（逐只查询；"
        f"{len(by_inst)}/{len(instruments)} 命中）")
    pool_path.write_bytes(json.dumps(payload, ensure_ascii=False,
                                     indent=2).encode("utf-8"))
    print(f"\n已写回 {pool_path}（更新 {changed} 只）")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
