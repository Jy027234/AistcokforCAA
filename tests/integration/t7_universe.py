"""T7 第二部分：BaoStock 能否支撑"扩池到全市场"。

为什么必须验这一条
------------------
研究池现在是**手工声明**的 24 只。要"验证非人为选取的样本"，
就必须有一个权威的全市场证券清单。BaoStock 的 query_all_stock
返回 7378 条（含指数），但能否筛出真正的 A 股、并批量取到行情，
决定了扩池是"改个配置"还是"跑一整天"。

退出码：0 = 通过；1 = 有断言失败；2 = 数据源不可用。
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))


def board_of(number: str) -> str | None:
    """按代码判断板块。这是交易所规则，不是猜测。"""

    if number.startswith(("600", "601", "603", "605")):
        return "MAIN"
    if number.startswith("688"):
        return "STAR"
    if number.startswith(("000", "001", "002", "003")):
        return "MAIN"
    if number.startswith(("300", "301")):
        return "GEM"
    if number.startswith(("8", "4", "920")):
        return "BSE"
    return None


def main() -> int:
    try:
        import baostock as bs
    except ImportError:
        print("需要 baostock")
        return 2

    bs.login()
    try:
        print("=== [1] 全市场证券清单 ===")
        t0 = time.time()
        rs = bs.query_all_stock(day="2026-09-14")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        fields = list(rs.fields)
        elapsed = time.time() - t0
        print(f"  {len(rows)} 条，{elapsed:.1f}s，字段 {fields}")
        records = [dict(zip(fields, r)) for r in rows]

        # 清单里混了指数（sh.000001 上证综合指数）。必须能区分——
        # 把指数当股票放进模拟池会得到一条永远不对的样本。
        stocks = []
        indices = []
        unknown = []
        for r in records:
            market, _, number = r["code"].partition(".")
            if market not in ("sh", "sz"):
                unknown.append(r["code"])
                continue
            if number.startswith(("000", "399")) and market == "sz":
                indices.append(r["code"])
                continue
            if market == "sh" and number.startswith("000"):
                indices.append(r["code"])
                continue
            if board_of(number):
                stocks.append(r)
            else:
                unknown.append(r["code"])

        check("能区分股票与指数", len(stocks) > 4000 and indices,
              f"股票 {len(stocks)}、指数 {len(indices)}、无法判定 {len(unknown)}")
        print(f"  无法判定样例: {unknown[:6]}")

        boards = Counter(board_of(r["code"].partition(".")[2]) for r in stocks)
        print(f"  板块分布: {dict(boards)}")
        check("覆盖沪深主板", boards.get("MAIN", 0) > 2000, str(boards.get("MAIN")))
        check("含成长板块（可展示不可模拟）",
              boards.get("GEM", 0) > 500 and boards.get("STAR", 0) > 100,
              f"GEM {boards.get('GEM')}、STAR {boards.get('STAR')}")

        print()
        print("=== [2] 批量行情延迟（决定扩池成本）===")
        sample = [r["code"] for r in stocks[:10]]
        t0 = time.time()
        got = 0
        for code in sample:
            r = bs.query_history_k_data_plus(
                code, "date,open,high,low,close,volume,amount",
                start_date="2026-06-22", end_date="2026-09-14",
                frequency="d", adjustflag="3")
            while r.next():
                r.get_row_data()
                got += 1
        per = (time.time() - t0) / len(sample)
        print(f"  10 只 × 61 交易日：{time.time()-t0:.1f}s，{got} 行，{per:.2f}s/只")
        check("每只 61 日行情延迟可接受（<3s）", per < 3.0, f"{per:.2f}s/只")
        full_minutes = per * len(stocks) / 60
        print(f"  推算全市场 {len(stocks)} 只：约 {full_minutes:.0f} 分钟")
        check("全市场扩池时间可接受（<180 分钟）", full_minutes < 180,
              f"{full_minutes:.0f} 分钟")

        print()
        print("=== [3] 与现有真实快照交叉校验（同源不同库）===")
        snap = ROOT / "deploy" / "real-snapshot" / "meta.sqlite"
        if snap.exists():
            import sqlite3
            con = sqlite3.connect(snap)
            con.row_factory = sqlite3.Row
            rows2 = con.execute(
                "SELECT instrument_id, trading_day, close_cents, volume_shares "
                "FROM snapshot_dataset d, json_each(d.record_count) WHERE 1=0").fetchall() \
                if False else []
            con.close()
            # 数据集是 JSON 文件而不是表，直接从文件比对更直接
            import glob
            files = sorted((ROOT / "deploy" / "real-snapshot" / "api"
                            / "datasets" / "snap-real-61d").glob("daily_quotes.json"))
            if files:
                quotes = json.loads(files[0].read_text(encoding="utf-8"))
                want = next(q for q in quotes
                            if q["instrument_id"] == "SH.600519"
                            and q["trading_day"] == "2026-09-01")
                r = bs.query_history_k_data_plus(
                    "sh.600519", "date,close,volume",
                    start_date="2026-09-01", end_date="2026-09-01",
                    frequency="d", adjustflag="3")
                while r.next():
                    _d, c, v = r.get_row_data()
                ours_cents = want["close_cents"]
                theirs_cents = round(float(c) * 100)
                check("收盘价与腾讯源一致（跨源校验）",
                      ours_cents == theirs_cents,
                      f"腾讯 {ours_cents} 分 vs BaoStock {theirs_cents} 分")
                vol_delta = abs(int(v) - want["volume_shares"])
                check("成交量差异在舍入范围内",
                      vol_delta <= 100,
                      f"腾讯 {want['volume_shares']} vs BaoStock {v}（差 {vol_delta}）")
            else:
                check("真实快照数据集存在", False, str(files))
        else:
            print("  （未找到真实快照，跳过交叉校验）")

    finally:
        bs.logout()

    failed = [c for c in checks if not c[1]]
    print()
    print("=" * 62)
    print(f"T7 全市场扩池验证 {len(checks) - len(failed)}/{len(checks)} 通过")
    for name, _, detail in failed:
        print("  - " + name + "  " + detail)

    out = ROOT / "deploy" / "agentctl-q0" / "t7-universe.json"
    out.write_bytes(json.dumps({
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "all_stock_rows": len(rows),
        "stocks": len(stocks), "indices": len(indices), "unclassified": len(unknown),
        "boards": dict(boards),
        "latency": {"query_all_stock_s": round(elapsed, 1), "per_instrument_s": round(per, 2)},
        "projected_full_market_minutes": round(full_minutes, 1),
        "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks],
        "conclusion": "PASS" if not failed else "FAIL",
    }, ensure_ascii=False, indent=2).encode("utf-8"))
    print(f"报告：{out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())