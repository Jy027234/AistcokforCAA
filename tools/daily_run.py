"""每日流水线：采集 -> 建快照 -> 算因子 -> 留痕（§14.2）。

为什么需要它
------------
在此之前，"更新数据"是一串手工命令：先跑 collect_universe.py，
再跑 t10_universe_snapshot.py。短跑可以接受，**每天跑会立刻暴露两个问题**：

  * 没人记得跑，或者跑了但没看结果；
  * 两次运行重叠时会同时写同一份缓存与同一个快照目录。

这个脚本把它们串起来，并补上三件手工流程没有的东西：
**休市判断、并发锁、运行留痕**。

关于"时点"的一件事实（必须说清）
--------------------------------
每日快照的 as_of_time 是当天**收盘**，而数据是当晚抓的。
也就是说：这一天的快照在当天是"重建"的，不是当时观察到的——
available_basis 记为 RECONSTRUCTED 是准确的。
真正的 LIVE_OBSERVED 要从"每天真的在收盘后抓一次"开始累积，
而这件事**从这个脚本第一次被定时执行时才算开始**。

用法：
    python tools/daily_run.py                      # 跑到今天
    python tools/daily_run.py --trading-day 2026-09-14
    python tools/daily_run.py --skip-if-done       # 定时任务推荐
    python tools/daily_run.py --dry-run            # 只说会做什么
退出码：0 = 已发布或按计划跳过；1 = 失败；2 = 环境缺失。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.operations.alerting import (  # noqa: E402
    LEVEL_ERROR, AlertLog, raise_alert,
)
from aquant.operations.pipeline import (  # noqa: E402
    PipelineBusy, PipelineLock, RunLog, RunRecord,
)

PYTHON = sys.executable
CACHE = ROOT / "deploy" / "agentctl-q0" / "universe-bars.json"
LOCK = ROOT / "deploy" / "agentctl-q0" / "daily-run.lock"
RUNS = ROOT / "deploy" / "agentctl-q0" / "daily-runs.jsonl"
ALERTS = ROOT / "deploy" / "agentctl-q0" / "alerts.jsonl"
POOL = ROOT / "configs" / "real-pool-csrc.yaml"
#: 已发布快照的数据目录。因子必须落在**这份**库里，
#: 否则研究卡（它读的就是这里）永远看不到数值。
SNAPSHOT_DIR = ROOT / "deploy" / "universe-snapshot"
FACTORS_REPORT = ROOT / "deploy" / "agentctl-q0" / "factors-persist.json"

#: 快照窗口的第一天。**固定不动**：窗口跟着当天滑动的话，
#: 两次运行覆盖的日期范围不同，快照之间就没法比较。
#: 需要更长的历史时显式改它并重新采集。
WINDOW_START = "2026-06-22"


def _cache_last_day() -> str | None:
    """采集缓存里实际有的最后一个交易日。

    **不能**用"目标的交易日"代替它：目标日休市或数据尚未更新时，
    采集不会报错（只是没有新行），而快照 ID 会照样叫那个日期——
    于是 ID 说 A、内容是 B。这类不一致事后极难察觉。
    """

    import json

    try:
        doc = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for entry in (doc.get("bars") or {}).values():
        rows = entry.get("rows") or []
        if rows:
            return rows[-1]["trading_day"]
    return None


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print("[" + stamp + "] " + message, flush=True)


def run_step(args: list[str], *, label: str) -> dict:
    """跑一步，返回 step/ok/seconds/tail。**不抛异常**——
    每一步的结果都要进留痕，抛出去就丢了"哪一步失败"。"""

    started = time.time()
    proc = subprocess.run([PYTHON, *args], cwd=ROOT, text=True,
                          capture_output=True)
    seconds = time.time() - started
    tail = (proc.stdout or "").strip().splitlines()[-3:]
    ok = proc.returncode == 0
    log(("OK   " if ok else "FAIL ") + label + f"（{seconds:.1f}s）")
    for line in tail:
        print("      " + line)
    if not ok and proc.stderr:
        print("      stderr: " + proc.stderr.strip().splitlines()[-1])
    return {"step": label, "ok": ok, "seconds": round(seconds, 1),
            "tail": tail, "exitCode": proc.returncode}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trading-day", default=None,
                    help="要发布的交易日（YYYY-MM-DD）；默认今天")
    ap.add_argument("--skip-if-done", action="store_true",
                    help="该交易日已成功发布过就直接退出（定时任务推荐）")
    ap.add_argument("--dry-run", action="store_true", help="只说会做什么")
    ap.add_argument("--window-start", default=WINDOW_START)
    ap.add_argument("--snapshot-id", default="snap-universe",
                    help="快照 ID。默认稳定不变——每天重建的是同一个逻辑"
                         "对象的新版本，由 supersedes 串链；换 ID 会让所有"
                         "引用固定 ID 的工具同时失效。")
    args = ap.parse_args()

    alerts = AlertLog(ALERTS)

    def alert(message: str, **detail) -> None:
        """失败时告警。**先落盘再外发**（见 operations/alerting.py）。"""

        result = raise_alert(alerts, level=LEVEL_ERROR, source="daily_run",
                             message=message, detail=detail)
        log("已告警：" + message
            + ("（已外发）" if result["delivered"] else
               "（仅落盘：" + str(result["deliveryError"]) + "）"))

    if not CACHE.exists():
        message = "缺少采集缓存，流水线无法运行"
        print(message + "：" + str(CACHE))
        print("先跑一次 python tools/collect_universe.py")
        alert(message, cache=str(CACHE))
        return 2
    if not POOL.exists():
        message = "缺少研究池，流水线无法运行"
        print(message + "：" + str(POOL))
        alert(message, pool=str(POOL))
        return 2

    target = (date.fromisoformat(args.trading_day) if args.trading_day
              else date.today())
    day = target.isoformat()
    # 快照 ID **稳定**：内容随交易日推进，ID 不变。
    #
    # 第一版每天换 ID（snap-eod-<日期>），结果是所有钉在固定 ID 上的
    # 工具与验收脚本同时失效——而这恰恰是我自己在文档里警告过的
    # "两次运行覆盖范围不同就没法比较"的翻版。
    #
    # 稳定的 ID 与"快照不可变"并不冲突：不可变指的是**已发布的内容**
    # 不得原地修改；每天重建的是同一个逻辑对象（"当前快照"）的新版本，
    # 由 supersedes 串成链。这比"每天一个新 ID、没人知道哪个是当前"
    # 更接近使用者的心智模型。
    snapshot_id = args.snapshot_id
    run_log = RunLog(RUNS)
    record = RunRecord(trading_day=day, snapshot_id=snapshot_id, outcome="FAILED",
                       started_at=datetime.now(timezone.utc).isoformat())

    if args.skip_if_done and run_log.last_successful_day() == day:
        record.outcome = "SKIPPED"
        record.reason = "该交易日已成功发布过"
        record.finished_at = datetime.now(timezone.utc).isoformat()
        run_log.append(record)
        log(day + " 已发布过，跳过")
        return 0

    if args.dry_run:
        print("会做的事（dry-run）：")
        print("  1. 采集 " + args.window_start + " .. " + day + " 的研究池行情")
        print("  2. 发布快照 " + snapshot_id)
        print("  3. 在该快照上计算 F10 因子")
        print("  4. 追加运行日志 " + str(RUNS))
        return 0

    started = time.time()
    try:
        with PipelineLock(LOCK):
            log("发布 " + day + " 的快照 " + snapshot_id)

            # 1. 采集：窗口起点钉死，末端推到目标日。
            #    休市日采集不会报错（只是没有新行），因此"休市判断"
            #    落在第 2 步的行情条数校验上。
            step = run_step([
                "tools/collect_universe.py",
                "--pool", str(POOL), "--start", args.window_start,
                "--end", day,
            ], label="采集行情")
            record.steps.append(step)
            if not step["ok"]:
                record.reason = "采集失败"
                alert("采集失败", tradingDay=day, step="采集行情",
                      exitCode=step["exitCode"], tail=step["tail"])
                return _finish(run_log, record, started, 1)

            # 采集之后**核对实际拿到了哪一天**，再决定 ID。
            actual_last = _cache_last_day()
            record.data_last_day = actual_last
            if actual_last is None:
                record.reason = "缓存的行情窗口为空"
                alert("采集缓存里没有任何行情", tradingDay=day)
                return _finish(run_log, record, started, 1)
            if actual_last != day:
                record.outcome = "SKIPPED"
                record.reason = (f"目标日 {day} 没有新行情（休市或数据未更新），"
                                 f"缓存实际末日 {actual_last}")
                # 按计划跳过而不是失败：休市不是错误。
                # 报失败会让定时任务重试到天亮，而结果不会变。
                return _finish(run_log, record, started, 0)
            record.snapshot_id = snapshot_id

            # 2. 建快照。窗口收窄到固定的起点。
            #
            # 刻意**不做 supersedes 链**：那条链要求被替代的快照仍然存在
            # 且为 PUBLISHED，而稳定 ID 下它会被同 ID 的新版本覆盖，
            # 两者不能同时成立。今天选择的取舍是：
            #
            #   * ID 稳定（"当前快照"），内容随交易日推进；
            #   * **不用** supersedes，因此"这一刻的当前快照长什么样"
            #     只由运行留痕与快照自身的 as_of_time 回答。
            #
            # 代价要写清楚：严格意义上"快照不可变"被放宽了——
            # 每天都在重建同一个 ID。对"给工作台提供当前视图"这个用途
            # 这是可接受的；但如果将来要拿历史快照做逐日回放，
            # 必须先改成每次发布新 ID 并保留旧目录，
            # 否则回放看到的是被覆盖后的内容。
            step = run_step([
                "-m", "tests.integration.t10_universe_snapshot",
                "--pool", str(POOL), "--snapshot-id", snapshot_id,
                "--window-start", args.window_start,
            ], label="发布快照")
            record.steps.append(step)
            if not step["ok"]:
                # 快照构建里的"行情条数充足 / 窗口首行前收"两条校验
                # 就是休市与数据缺口的判据：它们失败说明这一天没有
                # 可发布的内容，而不是脚本坏了。
                record.reason = "快照构建未通过校验（可能是休市或数据未更新）"
                alert("快照构建未通过校验", tradingDay=day,
                      exitCode=step["exitCode"], tail=step["tail"])
                return _finish(run_log, record, started, 1)

            # 3. 算因子并**落库**（在新快照上）。
            #
            # 这一步以前跑的是 `tests.integration.t12_f10_real`：它读采集缓存、
            # 在内存里算一遍、写一份验收报告。于是"验收报告 PASS"与
            # "快照库里 research_run 0 行"同时成立——流水线每天成功，
            # 研究卡每天没有数值。落库与验收是两件事，必须分成两步。
            step = run_step([
                "tools/compute_factors.py",
                "--snapshot-dir", str(SNAPSHOT_DIR), "--snapshot-id", snapshot_id,
                "--json-out", str(FACTORS_REPORT),
            ], label="因子落库")
            record.steps.append(step)
            if not step["ok"]:
                # 快照已发布是事实，因子没落库是另一个状态：结果仍是
                # PUBLISHED，但这条必须告警——研究卡上没有数值。
                alert("快照已发布，但因子未落库（研究卡将无数值）",
                      tradingDay=day, snapshotId=snapshot_id,
                      exitCode=step["exitCode"], tail=step["tail"])

            # 4. 因子质量闸门。失败不影响"快照已发布"与"因子已落库"这两个事实，
            #    但它说明数值本身有问题（单位、值域、亏损股被截断为 0……），
            #    必须留下痕迹。
            step = run_step(["-m", "tests.integration.t12_f10_real"],
                            label="F10 质量闸门")
            record.steps.append(step)
            if not step["ok"]:
                alert("F10 质量闸门未通过（数值可能不可信）",
                      tradingDay=day, snapshotId=snapshot_id,
                      exitCode=step["exitCode"], tail=step["tail"])

            record.outcome = "PUBLISHED"
            return _finish(run_log, record, started, 0)

    except PipelineBusy as exc:
        record.reason = str(exc)
        record.outcome = "SKIPPED"
        return _finish(run_log, record, started, 0,
                       message="上一次运行还在进行，本次跳过（不算失败）")


def _finish(run_log: RunLog, record: RunRecord, started: float,
            code: int, *, message: str | None = None) -> int:
    record.finished_at = datetime.now(timezone.utc).isoformat()
    record.duration_seconds = round(time.time() - started, 1)
    run_log.append(record)
    log(message or ("结果 " + record.outcome
                    + ("：" + record.reason if record.reason else "")))
    log("留痕 " + str(RUNS))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
