"""采集财务数据到增量缓存（可中断续跑）。

为什么单独采集
--------------
财务数据与行情是两类事实，采集节奏也不同：行情按交易日，财务按季度。
分开采集让两者都可以独立重建与复核，而不是把 5400 次查询绑在一次运行里。

采集范围是 T9 审计确认**字段可用**的部分：
  * netProfit / totalShare / epsTTM（profit 表）；
  * CFOToNP（cash_flow 表，F08 用）。
  * MBRevenue **不采集**：T9 实测有整季空值，采了也不能用于 TTM。

每个标的取 2025Q1 起的季度：TTM 需要"上年年报 + 上年同期 + 当期"，
因此至少要覆盖前一年全年。

用法：
    python tools/collect_financials.py --limit 20      # 试跑
    python tools/collect_financials.py                 # 全池（可续跑）
    python tools/collect_financials.py --retry-failed

输出：deploy/agentctl-q0/financials-cache.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.baostock import (  # noqa: E402
    BaostockClient, BaostockUnavailable,
)

OUT = ROOT / "deploy" / "agentctl-q0" / "financials-cache.json"
POOL = ROOT / "configs" / "real-pool-csrc.yaml"
ARCHIVE = ROOT / "deploy" / "agentctl-q0" / "baostock-archive"

#: (年, 季)。TTM 需要上年年报与上年同期，故从 2025Q1 起。
PERIODS = [(2025, 1), (2025, 2), (2025, 3), (2025, 4), (2026, 1), (2026, 2)]


def load() -> dict:
    if OUT.exists():
        return json.loads(OUT.read_text(encoding="utf-8"))
    return {"created_at": None, "statements": {}, "failed": {}}


def save(doc: dict) -> None:
    doc["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = OUT.with_suffix(".tmp")
    tmp.write_bytes(json.dumps(doc, ensure_ascii=False).encode("utf-8"))
    tmp.replace(OUT)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    pool = json.loads(POOL.read_text(encoding="utf-8"))
    targets = [i["instrument_id"] for i in pool["instruments"]]

    doc = {"created_at": None, "statements": {}, "failed": {}} if args.refresh \
        else load()
    statements: dict = doc.setdefault("statements", {})
    failed: dict = doc.setdefault("failed", {})

    if args.retry_failed:
        targets = [t for t in targets if t in failed]
        for t in targets:
            failed.pop(t, None)
        print(f"只重试失败标的：{len(targets)} 只")
    if args.limit:
        targets = targets[: args.limit]

    print(f"池内 {len(pool['instruments'])} 只，本次处理 {len(targets)} 只，"
          f"每只 {len(PERIODS)} 个季度")

    def bs_code(iid: str) -> str:
        return iid[:2].lower() + "." + iid[3:]

    with BaostockClient(ARCHIVE) as bs:
        doc["created_at"] = doc.get("created_at") or datetime.now(timezone.utc).isoformat()
        doc["periods"] = [f"{y}Q{q}" for y, q in PERIODS]
        done = skipped = 0
        for i, iid in enumerate(targets, 1):
            if iid in statements and not args.refresh and statements[iid]:
                skipped += 1
                continue
            code = bs_code(iid)
            try:
                rows: dict[str, dict] = {}
                for year, quarter in PERIODS:
                    key = f"{year}Q{quarter}"
                    raw = bs._bs.query_profit_data(code=code, year=year, quarter=quarter)
                    if raw.error_code != "0":
                        continue
                    fields = list(raw.fields)
                    got = []
                    while raw.next():
                        got.append(dict(zip(fields, raw.get_row_data())))
                    if got:
                        rows[key] = got[0]
                    # 现金流只在有利润记录时取，减少无效查询
                    if got:
                        cfo = bs._bs.query_cash_flow_data(code=code, year=year,
                                                          quarter=quarter)
                        if cfo.error_code == "0":
                            cfields = list(cfo.fields)
                            cgot = []
                            while cfo.next():
                                cgot.append(dict(zip(cfields, cfo.get_row_data())))
                            if cgot:
                                rows[key]["CFOToNP"] = cgot[0].get("CFOToNP")
                statements[iid] = rows
                done += 1
            except BaostockUnavailable as exc:
                failed[iid] = f"{type(exc).__name__}: {str(exc)[:80]}"

            if i % 25 == 0 or i == len(targets):
                print(f"  {i}/{len(targets)}  已采 {done}  跳过 {skipped}  "
                      f"失败 {len(failed)}")
                save(doc)
        save(doc)

    have = sum(1 for v in statements.values() if v)
    print()
    print("=" * 62)
    print(f"财务采集完成：{have} 只有数据，失败 {len(failed)} 只")
    print(f"缓存：{OUT}（{OUT.stat().st_size / 1024:.0f} KB）")
    for k, v in list(failed.items())[:5]:
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())