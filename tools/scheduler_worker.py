"""每日流水线的调度 worker（独立进程，长驻）。

它为什么不是 API 里的一个线程
============================
见 `src/aquant/operations/scheduler.py` 的模块说明：API 会重启、采集会与
请求争用连接、多实例会各自触发。这里只强调一条与本文件直接相关的：
**调度器必须能回答"这一次跑了没有"**，而这件事只有在 worker 进程
自己的生命周期里才说得清。

它做什么
========
循环三件事，每一轮都很短：

  1. 到点了吗（`is_due`）？到点就登记一条 scheduler 请求；
  2. 有 PENDING 请求（可能来自界面按钮）吗？有就领取并执行；
  3. 执行 = 起一个子进程跑 `tools/daily_run.py`，**与手工运行同一条路径**
     （包括并发锁、休市判断、留痕、告警），然后把结果写回摘要表。

刻意不做的事
============
  * **不 import 领域计算**：worker 是运维件，不是产品逻辑。它对流水线的
    全部知识就是"怎么起它、怎么看退出码"。
  * **不用线程池并发跑多次**：同一时刻只允许一次运行。`daily_run` 自己
    有锁，但让 worker 也串行可以省掉一类"两次运行交错"的排查。
  * **不自动重试失败的运行**：失败已经告警并留痕，自动重试会把
    "数据源坏了"变成"每 30 秒重试一次"，把告警淹没。

用法
====
    python tools/scheduler_worker.py                     # 长驻，按配置到点运行
    python tools/scheduler_worker.py --once              # 只处理一轮（给测试与 cron 用）
    python tools/scheduler_worker.py --now               # 立刻执行一次当前待处理请求
    python tools/scheduler_worker.py --data-dir <dir>    # 指定快照数据目录

退出码：0 正常结束；2 数据目录不可用；1 本轮执行失败（`--once`/`--now`）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.data.db import apply_migrations, connect  # noqa: E402
from aquant.operations import scheduler  # noqa: E402

#: 仓库里默认的快照数据目录。与 `daily_run` 的输出目录、API 的
#: `AQUANT_DATA_DIR` 指向同一个地方——三者不一致的话，界面读的是
#: 一份库、调度写的是另一份，而两边都"正常"。
DEFAULT_DATA_DIR = ROOT / "deploy" / "universe-snapshot"

#: 单次运行的超时。**必须有**：采集卡住时 worker 不能跟着一起卡死——
#: 那会让"下一次运行"永远等不到，而且没有任何东西会报错。
RUN_TIMEOUT_SECONDS = 30 * 60
# `tick` keeps the historical `_run_pipeline` call shape so callers/tests that
# replace it remain compatible; the real subprocess still receives the exact
# data root through this short-lived worker-local context.
_PIPELINE_DATA_DIR: Path | None = None


def log(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def _worker_id() -> str:
    return f"scheduler-{uuid.uuid4().hex[:8]}"


def _resolve_interpreter(configured: str) -> Path:
    """用哪个解释器跑流水线。

    配置优先；没配就用 `sys.executable`。**默认值可能是错的**——
    流水线依赖 baostock 与 pytest，而启动 worker 的那个 python 未必装了它们。
    因此这里把选中的解释器打出来，让"用错了"至少是可见的。
    """

    if configured.strip():
        return Path(configured.strip())
    return Path(sys.executable)


def _preflight(interpreter: Path, data_dir: Path) -> list[str]:
    """跑之前能查出来的问题，就不要等跑了一半才发现。

    返回问题列表（空 = 没问题）。**不阻止运行**——流水线自己会报错并告警，
    这里只是把"解释器不存在"这种确定性失败提前说出来。
    """

    problems: list[str] = []
    if not interpreter.exists():
        problems.append(f"解释器不存在：{interpreter}")
    if not (ROOT / "tools" / "daily_run.py").exists():
        problems.append(f"找不到流水线脚本：{ROOT / 'tools' / 'daily_run.py'}")
    if not (ROOT / "deploy" / "agentctl-q0" / "universe-bars.json").exists():
        problems.append("缺少采集缓存 universe-bars.json（先跑 tools/collect_universe.py）")
    if not data_dir.exists():
        problems.append(f"数据目录不存在：{data_dir}")
    return problems


def _run_pipeline(*, interpreter: Path, window_start: str, source: str,
                  json_out: Path, data_dir: Path | None = None) -> tuple[int, str]:
    """起子进程跑一次流水线。返回 (exit_code, 输出尾巴)。"""

    resolved_data_dir = data_dir or _PIPELINE_DATA_DIR
    args = [str(interpreter), str(ROOT / "tools" / "daily_run.py"),
            "--source", source, "--window-start", window_start,
            "--json-out", str(json_out)]
    if resolved_data_dir is not None:
        args[4:4] = ["--data-dir", str(resolved_data_dir)]
    log("执行：" + " ".join(args))
    started = time.time()
    try:
        proc = subprocess.run(args, cwd=ROOT, text=True, capture_output=True,
                              timeout=RUN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return 1, f"超时（>{RUN_TIMEOUT_SECONDS}s）后被终止"
    seconds = time.time() - started
    tail = "\n".join((proc.stdout or "").strip().splitlines()[-6:])
    log(f"退出码 {proc.returncode}（{seconds:.1f}s）")
    for line in (proc.stdout or "").strip().splitlines()[-6:]:
        print("      " + line, flush=True)
    if proc.stderr:
        print("      stderr: " + proc.stderr.strip().splitlines()[-1], flush=True)
    return proc.returncode, tail


def _load_record(json_out: Path) -> dict:
    if not json_out.exists():
        return {}
    try:
        return json.loads(json_out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def tick(con, *, worker_id: str, data_dir: Path, schedule: scheduler.RunSchedule,
         now: datetime, dry_run: bool = False) -> str:
    """处理一轮。返回这一步做了什么（给日志与测试断言用）。

    返回值为下面之一：`idle` / `fired` / `busy` / `ran:OK` / `ran:FAILED`。
    """

    # 到点判断之外还要看一件事：**已经有未完成的请求时不再登记**。
    #
    # 少了这一条，worker 会在"已到点、今天还没跑完"的每一轮里重复登记
    # （request_run 幂等所以只是同一条），现象是日志里刷满"到点：登记运行请求"，
    # 而真正该做的事（领取并执行）永远排在它后面。
    pending = scheduler.pending_request(con)
    if pending is None and scheduler.is_due(schedule, now=now,
                                            last_fired_for=scheduler.last_run_day(con)):
        if dry_run:
            return "fired"
        request_id = scheduler.request_run(
            con, source="scheduler", requested_by="scheduler",
            reason=f"到点触发 {schedule.run_at_local}", now=now)
        log(f"到点：登记运行请求 {request_id}")
        return "fired"

    claimed = scheduler.claim_next(con, worker_id=worker_id, now=now)
    if claimed is None:
        return "idle"

    interpreter = _resolve_interpreter(schedule.interpreter)
    problems = _preflight(interpreter, data_dir)
    if problems:
        detail = "；".join(problems)
        log("预检未通过：" + detail)
        scheduler.finish_request(con, request_id=claimed["request_id"],
                                 status="FAILED", exit_code=2, detail=detail, now=now)
        scheduler.record_last_run(con, request_id=claimed["request_id"],
                                  source=claimed["source"],
                                  record={"outcome": "FAILED", "reason": detail,
                                          "finished_at": now.isoformat()},
                                  exit_code=2, now=now)
        return "ran:FAILED"

    json_out = ROOT / "deploy" / "agentctl-q0" / "last-run.json"
    if json_out.exists():
        json_out.unlink()
    global _PIPELINE_DATA_DIR
    previous_data_dir = _PIPELINE_DATA_DIR
    _PIPELINE_DATA_DIR = data_dir
    try:
        code, tail = _run_pipeline(
            interpreter=interpreter,
            window_start=schedule.window_start or "2026-06-22",
            source=claimed["source"], json_out=json_out)
    finally:
        _PIPELINE_DATA_DIR = previous_data_dir
    record = _load_record(json_out)
    status = "DONE" if code == 0 else "FAILED"
    scheduler.finish_request(con, request_id=claimed["request_id"], status=status,
                             exit_code=code, detail=tail or record.get("reason") or "",
                             now=now)
    scheduler.record_last_run(con, request_id=claimed["request_id"],
                              source=claimed["source"], record=record,
                              exit_code=code, now=now)
    return f"ran:{status}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=None,
                    help="快照数据目录（含 meta.sqlite）。默认取配置，"
                         "再取 AQUANT_DATA_DIR，最后用仓库默认。")
    ap.add_argument("--once", action="store_true",
                    help="只处理一轮就退出（cron / 测试用）")
    ap.add_argument("--now", action="store_true",
                    help="不等配置的时间：登记一条运行请求并立刻处理（"
                         "与界面上的「立刻运行一次」是同一条通道）")
    ap.add_argument("--interval", type=float, default=20.0,
                    help="轮询间隔秒数（长驻模式）")
    ap.add_argument("--interpreter", default=None,
                    help="覆盖数据库中的流水线解释器（容器内使用）。"
                         "宿主常驻任务通常不传，继续使用设置页保存的解释器。")
    ap.add_argument("--dry-run", action="store_true",
                    help="只说会做什么，不登记请求、不执行")
    args = ap.parse_args()

    data_dir = Path(args.data_dir) if args.data_dir else None
    if data_dir is None:
        configured = None
        # 先读一次配置来拿 data_dir：数据目录本身存在配置里，
        # 而配置又在数据目录的库里——鸡生蛋。因此用一个**引导目录**：
        # 环境变量优先，其次是仓库默认，读了配置之后再切换。
        boot = Path(DEFAULT_DATA_DIR)
        env_dir = None

        if os.environ.get("AQUANT_DATA_DIR"):
            env_dir = Path(os.environ["AQUANT_DATA_DIR"])
            boot = env_dir
        if not (boot / "meta.sqlite").is_file():
            print(f"数据目录不可用（没有 meta.sqlite）：{boot}")
            print("用 --data-dir 指定，或先跑 python tools/daily_run.py 建一份快照")
            return 2
        con0 = connect(boot / "meta.sqlite")
        apply_migrations(con0)
        schedule0 = scheduler.load_schedule(con0)
        con0.close()
        if schedule0.data_dir.strip():
            data_dir = Path(schedule0.data_dir.strip())
        else:
            data_dir = boot
    if not (data_dir / "meta.sqlite").is_file():
        print(f"数据目录不可用（没有 meta.sqlite）：{data_dir}")
        return 2

    con = connect(data_dir / "meta.sqlite")
    apply_migrations(con)

    # 启动时把上次崩在 CLAIMED 的请求放回 PENDING。**只在启动时做**：
    # 循环里做会把另一个正常运行的 worker 正在跑的活抢回来。
    recovered = scheduler.startup_recovery(con)
    if recovered:
        log(f"启动恢复：{recovered} 条 CLAIMED 请求放回 PENDING")

    worker_id = _worker_id()
    schedule = scheduler.load_schedule(con)
    interpreter = _resolve_interpreter(args.interpreter or schedule.interpreter)
    log(f"worker {worker_id}｜数据目录 {data_dir}")
    log(f"调度：{'启用' if schedule.enabled else '停用'}"
        f"｜{schedule.run_at_local}"
        f"｜{'周一至周五' if schedule.weekdays_only else '每天'}"
        f"｜解释器 {interpreter}")

    if args.now:
        request_id = scheduler.request_run(
            con, source="manual", requested_by="cli",
            reason="--now 立刻执行一次")
        log(f"登记运行请求 {request_id}")

    def one_round() -> str:
        schedule_now = scheduler.load_schedule(con)
        if args.interpreter:
            from dataclasses import replace
            schedule_now = replace(schedule_now, interpreter=args.interpreter)
        return tick(con, worker_id=worker_id, data_dir=data_dir,
                    schedule=schedule_now, now=scheduler.market_now(),
                    dry_run=args.dry_run)

    if args.once or args.now:
        result = one_round()
        log("本轮：" + result)
        con.close()
        # idle/fired 都算正常结束；ran:DONE 也正常；只有失败才非零。
        return 0 if result in ("idle", "fired") or result.endswith("DONE") else 1

    started_at = scheduler.market_now()
    heartbeat_stop = threading.Event()

    def write_heartbeat(status: str = "RUNNING") -> None:
        try:
            scheduler.write_worker_heartbeat(
                data_dir,
                worker_id=worker_id,
                pid=os.getpid(),
                started_at=started_at,
                interval_seconds=args.interval,
                status=status,
            )
        except OSError as exc:
            # 心跳写失败不应杀死真正的数据流水线，但控制台必须留下原因。
            log(f"worker 心跳写入失败：{exc}")

    def heartbeat_loop() -> None:
        write_heartbeat()
        while not heartbeat_stop.wait(max(1.0, min(args.interval, 20.0))):
            write_heartbeat()

    heartbeat_thread = threading.Thread(
        target=heartbeat_loop, name="aquant-scheduler-heartbeat", daemon=True)
    heartbeat_thread.start()
    try:
        while True:
            result = one_round()
            if result not in ("idle",):
                log("本轮：" + result)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log("收到中断，退出")
        con.close()
        return 0
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2.0)
        write_heartbeat("STOPPED")


if __name__ == "__main__":
    raise SystemExit(main())
