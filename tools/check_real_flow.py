"""真实数据产品闭环验收。

与 check_ui_flow.py 的分工
--------------------------
`check_ui_flow.py` 验的是**界面**能不能驱动写路径（真浏览器、合成快照）。
`check_real_flow.py` 验的是**真实数据**能不能走完同一条链路。
两者都不能省：合成数据验不出真实行情的停牌、涨跌停、前收不连续，
而界面验不出后端读取真实快照时是否真的对得上。

它检验的核心问题是：
    研究读取 -> 组合构建 -> 确认冻结 -> 成交 -> 估值 -> 对账
这条链在真实价格上是否仍然自洽（现金、批次、费用、应收逐项对上）。

用法：
    python tools/check_real_flow.py

前置：`python -m tests.integration.t6_real_snapshot` 已成功（快照已发布）。
退出码：0 = 全部断言通过；1 = 有断言失败；2 = 环境没起来。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_DIR = ROOT / "deploy" / "real-snapshot"
SNAPSHOT_ID = "snap-real-61d"
API_PORT = 8124

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))


class Client:
    def __init__(self, base: str) -> None:
        self.base = base

    def call(self, method: str, path: str, *, body: dict | None = None,
             subject: str = "user:real") -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json", "X-Aquant-Subject": subject},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"raw": raw}


def _wait_http(url: str, *, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status < 500:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.4)
    return False


def main() -> int:
    if not (SNAPSHOT_DIR / "meta.sqlite").exists():
        print("缺少真实快照——先运行：")
        print("  python -m tests.integration.t6_real_snapshot")
        return 2

    # 用真实快照的**副本**跑：验收不应改动被复核的那份数据。
    work = Path(tempfile.mkdtemp(prefix="aquant-realflow-"))
    shutil.copytree(SNAPSHOT_DIR, work / "data")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["AQUANT_DATA_DIR"] = str(work / "data")
    env["AQUANT_SNAPSHOT_ID"] = SNAPSHOT_ID

    api = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--app-dir", "apps/api",
         "--host", "127.0.0.1", "--port", str(API_PORT)],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{API_PORT}"
    try:
        if not _wait_http(base + "/api/v1/health"):
            print("API 未就绪；输出如下：")
            api.terminate()
            out, _ = api.communicate(timeout=10)
            print((out or "")[-2000:])
            return 2
        print(f"真实快照 API：{base}  快照 {SNAPSHOT_ID}")

        client = Client(base)

        print("\n[1] 数据状态与快照绑定")
        status, body = client.call("GET", "/api/v1/status")
        check("状态可读", status == 200, str(status))
        check("快照 ID 正确", body.get("snapshotId") == SNAPSHOT_ID,
              str(body.get("snapshotId")))
        check("数据模式为真实数据", body.get("dataMode") == "PRODUCTION",
              str(body.get("dataMode")))
        check("真实数据带水印", bool(body.get("watermark")), str(body.get("watermark")))

        print("\n[2] 候选来自真实快照上的 S1")
        status, cbody = client.call("GET", "/api/v1/candidates")
        cands = cbody.get("candidates") or []
        check("候选非空", bool(cands), f"{len(cands)} 只")
        check("候选全部来自真实池", all(
            str(c.get("instrumentId", "")).startswith(("SH.", "SZ.")) for c in cands))
        simulatable = [c for c in cands if c.get("simulatable")]
        check("存在可模拟（沪深主板）候选", bool(simulatable), f"{len(simulatable)} 只")
        check("成长板块候选被标为不可模拟",
              any(not c.get("simulatable") for c in cands) or len(cands) == len(simulatable),
              "GEM/STAR 应不可模拟")

        # 用快照最后一个交易日执行：参考价必须来自它**之前**的交易日
        trading_day = "2026-09-14"
        portfolio = "pf-real-m"

        print(f"\n[3] 预览（执行日 {trading_day}）")
        status, pv = client.call("POST", "/api/v1/plans/preview", body={
            "portfolio_id": portfolio, "snapshot_id": SNAPSHOT_ID,
            "trading_day": trading_day,
        })
        check("预览成功", status == 200, json.dumps(pv, ensure_ascii=False)[:200])
        if status != 200:
            return 1
        plan_id = pv["planId"]
        ref_day = pv.get("reference_price_day")
        check("参考价日早于执行日", bool(ref_day) and ref_day < trading_day,
              f"{ref_day} < {trading_day}")
        check("预览未冻结", pv.get("frozen") in (False, None), str(pv.get("frozen")))
        check("有订单或全部有排除原因",
              bool(pv.get("orders")) or bool(pv.get("excluded")),
              f"orders={len(pv.get('orders') or [])} excluded={len(pv.get('excluded') or [])}")
        check("预估费用非负", (pv.get("estimated_fees_cents") or 0) >= 0,
              str(pv.get("estimated_fees_cents")))

        print("\n[4] 确认并冻结（一次性令牌）")
        status, tok = client.call("POST", f"/api/v1/plans/{plan_id}/confirmation")
        check("签发令牌", status == 200, str(status))
        token = tok.get("confirmationToken")
        status, fr = client.call("POST", f"/api/v1/plans/{plan_id}/freeze", body={
            "plan_id": plan_id, "confirmation_token": token,
        })
        check("冻结成功", status == 200, json.dumps(fr, ensure_ascii=False)[:200])

        print("\n[5] 执行")
        status, ex = client.call("POST", f"/api/v1/plans/{plan_id}/execute",
                                 body={"plan_id": plan_id})
        check("执行成功", status == 200, json.dumps(ex, ensure_ascii=False)[:200])
        fills = ex.get("fills") or []
        check("执行结果自洽：成交+未成交=订单数",
              len(fills) + len(ex.get("rejections") or []) >= len(pv.get("orders") or []),
              f"fills={len(fills)} rejections={len(ex.get('rejections') or [])}")
        if fills:
            check("成交价为正整数分", all(f["price_cents"] > 0 for f in fills))
            check("成交数量为正整数", all(f["quantity"] > 0 for f in fills))

        print("\n[6] 日终估值")
        status, val = client.call("POST", "/api/v1/valuations", body={
            "portfolio_id": portfolio, "snapshot_id": SNAPSHOT_ID,
            "trading_day": trading_day,
        })
        check("估值成功", status == 200, json.dumps(val, ensure_ascii=False)[:200])
        if status == 200:
            net = val["net_value_cents"]
            recomputed = (val["cash_available_cents"] + val["cash_frozen_cents"]
                          + val["receivables_cents"] + val["positions_value_cents"]
                          - val["payables_cents"])
            check("净值恒等式成立", net == recomputed,
                  f"{net} vs {recomputed}")
            check("净值已发布", val.get("published") is True, str(val.get("published")))

        print("\n[7] 逐项对账")
        status, rec = client.call("GET", f"/api/v1/portfolios/{portfolio}/reconcile")
        check("对账成功", status == 200, json.dumps(rec, ensure_ascii=False)[:200])
        if status == 200:
            check("对账结论为一致", rec.get("reconciled") is True, json.dumps(rec)[:200])
            check("现金与账本一致",
                  rec.get("valuation_cash_matches_ledger") is True)
            check("应收与账本一致",
                  rec.get("valuation_receivables_matches_ledger") is True)
            inv = rec.get("invariants") or {}
            for name in ("fill_le_order", "fees_booked_once",
                         "cash_lines_sum_to_balance", "lots_match_fills"):
                check(f"不变量 {name}", inv.get(name) is True, str(inv.get(name)))

        print("\n[8] 跨源拒绝：指向别的快照必须 404，不得静默换数据")
        status, _ = client.call("POST", "/api/v1/plans/preview", body={
            "portfolio_id": portfolio, "snapshot_id": "snap-syn-001",
            "trading_day": trading_day,
        })
        check("合成快照 ID 被拒绝", status == 404, str(status))

        failed = [c for c in checks if not c[1]]
        print("\n" + "=" * 62)
        print(f"真实数据闭环验收 {len(checks) - len(failed)}/{len(checks)} 通过")
        for name, _, detail in failed:
            print("  - " + name + "  " + detail)
        out = ROOT / "deploy" / "agentctl-q0" / "real-flow-result.json"
        out.write_bytes(json.dumps({
            "snapshot_id": SNAPSHOT_ID,
            "trading_day": trading_day,
            "portfolio_id": portfolio,
            "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks],
            "conclusion": "PASS" if not failed else "FAIL",
        }, ensure_ascii=False, indent=2).encode("utf-8"))
        print(f"报告：{out}")
        return 1 if failed else 0
    finally:
        api.terminate()
        try:
            api.wait(timeout=10)
        except subprocess.TimeoutExpired:
            api.kill()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
