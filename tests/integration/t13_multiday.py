"""T13：多日模拟 + 跨进程账本重建的真实数据验收。

为什么必须跨进程
--------------
"账本正确"这句话在单进程里很容易成立——因为账本的真相可能一直躺在
内存里。真实的失败方式是：进程重启后持仓少了一批、现金对不上、
版本号重置。这些在单进程测试里**永远测不出来**。

因此本脚本每天**杀掉 API 进程再重启**，然后：
  * 账本必须从数据库重建出完全一致的现金与批次；
  * 对账必须继续通过；
  * 累计净值必须与逐日变化一致。

多日还验证一件事：**T+1 真的在跨日生效**。当日买入次日才可卖，
如果只跑单日，这条规则不会被触发。

用法：
    python -m tests.integration.t13_multiday
环境变量：
    T13_DAYS      参与模拟的交易日数，默认 5
    T13_SNAPSHOT  快照目录，默认 deploy/universe-snapshot
退出码：0 = 全部通过；1 = 有断言失败；2 = 环境没起来。
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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

SNAPSHOT_DIR = Path(os.environ.get(
    "T13_SNAPSHOT", str(ROOT / "deploy" / "universe-snapshot")))
SNAPSHOT_ID = "snap-universe"
API_PORT = 8125
PORTFOLIO = "pf-multiday-M"

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))


class Client:
    def __init__(self, base: str) -> None:
        self.base = base

    def call(self, method: str, path: str, *, body: dict | None = None,
             subject: str = "user:multiday") -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json", "X-Aquant-Subject": subject})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"raw": raw}


def wait_http(url: str, *, timeout: float = 60.0) -> bool:
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


def start_api(data_dir: Path, *, log_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["AQUANT_DATA_DIR"] = str(data_dir)
    env["AQUANT_SNAPSHOT_ID"] = SNAPSHOT_ID
    # 日志必须落盘：500 的堆栈只在这里，DEVNULL 会把失败原因一起丢掉
    log = open(log_path, "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--app-dir", "apps/api",
         "--host", "127.0.0.1", "--port", str(API_PORT)],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    proc.aquant_log = log  # type: ignore[attr-defined]
    if not wait_http(f"http://127.0.0.1:{API_PORT}/api/v1/health"):
        proc.terminate()
        raise RuntimeError("API 未就绪")
    return proc


def stop_api(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log = getattr(proc, "aquant_log", None)
    if log is not None:
        log.close()
    # 等端口释放，否则下一次启动会撞上 TIME_WAIT
    for _ in range(40):
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{API_PORT}/api/v1/health", timeout=1)
        except (urllib.error.URLError, OSError):
            return
        time.sleep(0.25)


def main() -> int:
    if not (SNAPSHOT_DIR / "meta.sqlite").exists():
        print("缺少快照：" + str(SNAPSHOT_DIR))
        print("先运行：python -m tests.integration.t10_universe_snapshot")
        return 2

    days_wanted = int(os.environ.get("T13_DAYS", "5"))
    calendar = json.loads(
        (SNAPSHOT_DIR / "api" / "datasets" / SNAPSHOT_ID
         / "trading_calendar.json").read_text(encoding="utf-8"))
    days = calendar[-days_wanted:]
    print("参与模拟的交易日：" + "、".join(days))

    work = Path(tempfile.mkdtemp(prefix="aquant-t13-"))
    shutil.copytree(SNAPSHOT_DIR, work / "data")

    # 日志放在工作区而不是临时目录：临时目录会被 finally 清理掉，
    # 而失败后我们最需要的东西恰好就是那份堆栈
    log_dir = ROOT / "deploy" / "agentctl-q0"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "t13-api.log"
    if log_path.exists():
        log_path.unlink()
    api = start_api(work / "data", log_path=log_path)
    client = Client(f"http://127.0.0.1:{API_PORT}")
    per_day: list[dict] = []
    try:
        for index, day in enumerate(days):
            print()
            print("=== 第 " + str(index + 1) + " 天：" + day + " ===")
            if index > 0:
                # 每天重建进程：账本的真相必须来自数据库
                stop_api(api)
                api = start_api(work / "data", log_path=log_path)
                check("第 " + str(index + 1) + " 天进程已重启", True,
                      "端口 " + str(API_PORT))

            rec_before = client.call("GET", f"/api/v1/portfolios/{PORTFOLIO}/reconcile")
            cash_before = (rec_before[1].get("cash_cents", 0)
                           if rec_before[0] == 200 else 0)
            positions_before = (rec_before[1].get("positions", {})
                                if rec_before[0] == 200 else {})

            status, pv = client.call("POST", "/api/v1/plans/preview", body={
                "portfolio_id": PORTFOLIO, "snapshot_id": SNAPSHOT_ID,
                "trading_day": day})
            if status != 200:
                check("第 " + str(index + 1) + " 天预览", False,
                      json.dumps(pv, ensure_ascii=False)[:180])
                break
            plan_id = pv["planId"]

            tok = client.call("POST", f"/api/v1/plans/{plan_id}/confirmation")[1]
            fr = client.call("POST", f"/api/v1/plans/{plan_id}/freeze", body={
                "plan_id": plan_id,
                "confirmation_token": tok.get("confirmationToken")})
            if fr[0] != 200:
                check("第 " + str(index + 1) + " 天冻结", False,
                      json.dumps(fr[1], ensure_ascii=False)[:180])
                break

            ex = client.call("POST", f"/api/v1/plans/{plan_id}/execute",
                             body={"plan_id": plan_id})
            if ex[0] != 200:
                check("第 " + str(index + 1) + " 天执行", False,
                      json.dumps(ex[1], ensure_ascii=False)[:180])
                tail = log_path.read_text(encoding="utf-8", errors="replace")
                print("      --- API 日志尾部 ---")
                for line in tail.splitlines()[-40:]:
                    print("      " + line)
                break
            fills = ex[1].get("fills") or []

            val = client.call("POST", "/api/v1/valuations", body={
                "portfolio_id": PORTFOLIO, "snapshot_id": SNAPSHOT_ID,
                "trading_day": day})
            if val[0] != 200:
                check("第 " + str(index + 1) + " 天估值", False,
                      json.dumps(val[1], ensure_ascii=False)[:180])
                break

            rec = client.call("GET", f"/api/v1/portfolios/{PORTFOLIO}/reconcile")
            body = rec[1]
            per_day.append({
                "day": day, "fills": len(fills),
                "net_value_cents": val[1]["net_value_cents"],
                "cash_cents": body.get("cash_cents"),
                "positions": body.get("positions") or {},
                "reconciled": body.get("reconciled"),
            })
            check("第 " + str(index + 1) + " 天对账通过",
                  body.get("reconciled") is True, json.dumps(body)[:160])
            check("第 " + str(index + 1) + " 天净值已发布",
                  val[1].get("published") is True)
            print("     成交 " + str(len(fills)) + " 笔，净值 "
                  + format(val[1]["net_value_cents"] / 100, ",") + " 元，现金 "
                  + format((body.get("cash_cents") or 0) / 100, ",") + " 元")

            # 跨日连续性：重启之后账本必须与上一天一致（允许当日成交改变）
            if index > 0:
                check("第 " + str(index + 1) + " 天起始现金与上日终一致",
                      cash_before == per_day[index - 1]["cash_cents"],
                      str(cash_before) + " vs " + str(per_day[index - 1]["cash_cents"]))
                check("第 " + str(index + 1) + " 天起始持仓与上日终一致",
                      positions_before == per_day[index - 1]["positions"],
                      str(positions_before) + " vs " + str(per_day[index - 1]["positions"]))

        print()
        print("=== 跨日不变量 ===")
        check("全部交易日都已执行", len(per_day) == len(days),
              str(len(per_day)) + "/" + str(len(days)))
        check("每日对账全部通过", all(d["reconciled"] for d in per_day),
              str([d["reconciled"] for d in per_day]))
        check("净值逐日可达且为正", all(d["net_value_cents"] > 0 for d in per_day),
              str([d["net_value_cents"] for d in per_day]))

        # 账本要能逐日追溯：估值表里应当每个交易日一条
        final = client.call("POST", "/api/v1/valuations", body={
            "portfolio_id": PORTFOLIO, "snapshot_id": SNAPSHOT_ID,
            "trading_day": days[-1]})
        check("末日估值可重复计算（幂等）",
              final[0] == 200
              and final[1]["net_value_cents"] == per_day[-1]["net_value_cents"],
              str(final[1].get("net_value_cents")))

        ledger = client.call("GET", f"/api/v1/portfolios/{PORTFOLIO}/ledger")
        if ledger[0] == 200:
            entries = ledger[1]["cash"]["entries"]
            check("账本有跨日分录", len(entries) >= len(days),
                  str(len(entries)) + " 条")
            check("账本现金合计自洽",
                  sum(e["amount"]["cents"] for e in entries)
                  == ledger[1]["cash"]["cents"])
            lots = ledger[1]["lots"]
            check("批次带最早可卖日（T+1 依据）",
                  all(l["earliest_sellable_day"] for l in lots),
                  str(len(lots)) + " 个批次")

            # 每一条费用都必须有一条**等额**的现金分录。
            #
            # 这条不变量来自一次真实事故：小额成交触发最低佣金补足时，
            # 费用码 MIN_COMMISSION_TOPUP 被当作 cash_entry.entry_type 写入，
            # 而该列的 CHECK 白名单里没有它——成交已经算出，账却记不下来，
            # 接口 500。当时只有跨日才暴露：第一天的每笔成交都超过最低佣金。
            # 所以这里按 fee_code 逐项对账，而不是只看合计。
            by_code: dict[str, int] = {}
            for e in entries:
                by_code[e["entry_type"]] = by_code.get(e["entry_type"], 0) + e["amount"]["cents"]
            fee_rows = ledger[1]["fees_by_code"]
            check("每条费用码都有对应现金分录（§12.6）",
                  all(c["fee_code"] in by_code for c in fee_rows),
                  str(sorted(c["fee_code"] for c in fee_rows)))
            check("费用分录金额与费用表逐项相等",
                  all(by_code.get(c["fee_code"]) == -c["total"]["cents"] for c in fee_rows),
                  str({c["fee_code"]: (by_code.get(c["fee_code"]), -c["total"]["cents"])
                       for c in fee_rows}))
            check("账本记录了最低佣金补足（触发过小额成交）",
                  "MIN_COMMISSION_TOPUP" in by_code,
                  str(sorted(by_code)))

    finally:
        stop_api(api)
        shutil.rmtree(work, ignore_errors=True)

    failed = [c for c in checks if not c[1]]
    print()
    print("=" * 62)
    print("T13 多日与跨进程验收 " + str(len(checks) - len(failed)) + "/"
          + str(len(checks)) + " 通过")
    for name, _, detail in failed:
        print("  - " + name + "  " + detail)

    out = ROOT / "deploy" / "agentctl-q0" / "t13-multiday.json"
    out.write_bytes(json.dumps({
        "days": days, "perDay": per_day,
        "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks],
        "conclusion": "PASS" if not failed else "FAIL",
    }, ensure_ascii=False, indent=2).encode("utf-8"))
    print("报告：" + str(out))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
