"""T8：BaoStock provider 的接入验证。

验的是**接入质量**，不是"能不能取到数"：
  1. 单位换算正确（元->分、股保持股、成交额->分）；
  2. 留证可用（内容寻址、哈希可复核、同一内容只存一份）；
  3. 未登录时显式报错，不静默返回空；
  4. 与腾讯源交叉一致（同一天同一只，收盘价逐分相同）；
  5. 财务数据带 pubDate（PIT 的前提）。

用法：
    python -m tests.integration.t8_baostock_provider
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.baostock import (  # noqa: E402
    BaostockClient, BaostockUnavailable,
)

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="aquant-t8-"))

    print("=== [1] 未登录时必须显式报错 ===")
    client = BaostockClient(work)
    try:
        client.stock_industry("sh.600519")
        check("未登录报错", False, "竟然返回了结果")
    except BaostockUnavailable as exc:
        check("未登录报错", True, str(exc)[:60])

    print()
    print("=== [2] 登录与单位换算 ===")
    with BaostockClient(work) as bs:
        bars, receipt = bs.daily_bars("sh.600519", start="2026-09-01", end="2026-09-01")
        check("取到日线", len(bars) == 1, f"{len(bars)} 条")
        if bars:
            b = bars[0]
            check("收盘价换算为整数分", isinstance(b["close_cents"], int) and b["close_cents"] > 0,
                  f"{b['close_cents']} 分")
            check("收盘价与已知值一致（1299.56 元）", b["close_cents"] == 129956,
                  str(b["close_cents"]))
            check("成交量单位是股（非手）", b["volume_shares"] > 1_000_000,
                  f"{b['volume_shares']} 股")
            check("成交额换算为整数分", b["amount_cents"] is not None and b["amount_cents"] > 0,
                  f"{b['amount_cents']} 分")
            # 成交额应约等于 收盘价×成交量（按均价口径会有小幅差异）
            approx = b["close_cents"] * b["volume_shares"]
            ratio = b["amount_cents"] / approx
            check("成交额与量价自洽（比值 0.9~1.1）", 0.9 < ratio < 1.1, f"比值 {ratio:.4f}")
            check("换手率为百分数", b["turnover_pct"] is None or 0 < b["turnover_pct"] < 100,
                  str(b["turnover_pct"]))

        print()
        print("=== [3] 留证：内容寻址与可复核 ===")
        check("生成了收据", receipt.record_count > 0, f"{receipt.record_count} 条")
        stored = work / receipt.stored_path
        check("收据指向的文件存在", stored.exists(), receipt.stored_path)
        if stored.exists():
            import hashlib
            actual = "sha256:" + hashlib.sha256(stored.read_bytes()).hexdigest()
            check("内容哈希可复核", actual == receipt.content_hash,
                  f"{actual[:23]}...")
        # 相同查询应复用同一份内容（内容寻址）
        bars2, receipt2 = bs.daily_bars("sh.600519", start="2026-09-01", end="2026-09-01")
        check("同一内容只存一份", receipt.content_hash == receipt2.content_hash,
              receipt2.stored_path)
        check("两条收据都留档", len(bs.receipts) == 2, str(len(bs.receipts)))

        print()
        print("=== [4] 与腾讯源交叉一致 ===")
        from aquant.adapters.providers.tencent import TencentClient
        from aquant.domain.data.db import apply_migrations, connect
        from aquant.domain.data.forward_archive import ForwardArchive

        con = connect(work / "x.sqlite")
        apply_migrations(con)
        archive = ForwardArchive(con, work / "archive")
        tc = TencentClient(archive)
        out, tbars = tc.daily_quotes("sh600519", "2026-09-01", "2026-09-01", adjust=0)
        con.close()
        if out.ok and tbars:
            tclose = int(round(float(tbars[0][2]) * 100))
            bclose = bars[0]["close_cents"]
            check("收盘价与腾讯逐分一致", tclose == bclose,
                  f"腾讯 {tclose} vs BaoStock {bclose}")
            tvol = int(float(tbars[0][5])) * 100
            check("成交量差异在舍入内", abs(tvol - bars[0]["volume_shares"]) <= 100,
                  f"腾讯 {tvol} vs BaoStock {bars[0]['volume_shares']}")

        print()
        print("=== [5] 会话过期自动重连（实测约 100 次查询后过期）===")
        # 这个坑很隐蔽：它不是一开始就失败，而是跑了一百次之后突然
        # 全线报"用户未登录"。不测这一条，长任务会在中途静默地失败一半。
        from aquant.adapters.providers.baostock import BaostockClient as _BC
        ok = 0
        failures = 0
        for i in range(30):
            try:
                bs.daily_bars("sh.600519", start="2026-09-01", end="2026-09-01")
                ok += 1
            except BaostockUnavailable as exc:
                failures += 1
                print(f"    第 {i+1} 次失败：{exc}")
        check("连续查询不因会话过期中断", failures == 0,
              f"{ok}/30 成功（若服务端提前踢会话，重连逻辑应已兜住）")
        # 显式模拟：把 session 标记改成过期码，看是否触发重连
        original = _BC.SESSION_EXPIRED_CODES
        try:
            _BC.SESSION_EXPIRED_CODES = ("0",)      # 让任何"成功"都被当成过期
            bars3, _r3 = bs.daily_bars("sh.600519", start="2026-09-01",
                                       end="2026-09-01")
            check("会话失效时会自动重连并重试", len(bars3) == 1,
                  f"重连后仍取到 {len(bars3)} 条")
        finally:
            _BC.SESSION_EXPIRED_CODES = original

        print()
        print("=== [6] 财务数据带 pubDate（PIT 前提）===")
        profit, preceipt = bs.profit("sh.600519", year=2026, quarter=2)
        check("取到季频财务", profit is not None, str(profit is not None))
        if profit:
            check("含 pubDate 公布日", bool(profit.get("pubDate")), str(profit.get("pubDate")))
            pub = profit.get("pubDate")
            if pub:
                try:
                    date.fromisoformat(pub)
                    check("pubDate 可解析为日期", True, pub)
                except ValueError:
                    check("pubDate 可解析为日期", False, pub)
            check("含 statDate 报告期", bool(profit.get("statDate")), str(profit.get("statDate")))
            check("报告期早于公布日",
                  (profit.get("statDate") or "9999") < (pub or "0000"),
                  f"{profit.get('statDate')} < {pub}")

    failed = [c for c in checks if not c[1]]
    print()
    print("=" * 62)
    print(f"T8 BaoStock provider 验证 {len(checks) - len(failed)}/{len(checks)} 通过")
    for name, _, detail in failed:
        print("  - " + name + "  " + detail)
    out = ROOT / "deploy" / "agentctl-q0" / "t8-baostock-provider.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(json.dumps({
        "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks],
        "conclusion": "PASS" if not failed else "FAIL",
    }, ensure_ascii=False, indent=2).encode("utf-8"))
    print(f"报告：{out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
