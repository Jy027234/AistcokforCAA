"""每日流水线：采集 -> 建快照 -> 算因子 -> 留痕（§14.2）。

为什么需要它
------------
在此之前，"更新数据"是一串手工命令：先跑 collect_universe.py，
再跑一次性快照验收脚本。短跑可以接受，**每天跑会立刻暴露两个问题**：

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
import json
import os
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
from aquant.domain.data.db import apply_migrations, connect  # noqa: E402
from aquant.domain.data.reader import SnapshotReader  # noqa: E402
from aquant.domain.data.snapshot import SnapshotStore  # noqa: E402
from aquant.operations.pipeline import (  # noqa: E402
    PipelineBusy, PipelineLock, RunLog, RunRecord,
)
from aquant.operations.scheduler import market_now  # noqa: E402
from aquant.operations.snapshot_lifecycle import new_snapshot_id  # noqa: E402

PYTHON = sys.executable
CACHE = ROOT / "deploy" / "agentctl-q0" / "universe-bars.json"
FINANCIALS_CACHE = ROOT / "deploy" / "agentctl-q0" / "financials-cache.json"
ACTIONS_CACHE = ROOT / "deploy" / "agentctl-q0" / "dividend-actions.json"
LOCK = ROOT / "deploy" / "agentctl-q0" / "daily-run.lock"
RUNS = ROOT / "deploy" / "agentctl-q0" / "daily-runs.jsonl"
ALERTS = ROOT / "deploy" / "agentctl-q0" / "alerts.jsonl"
POOL = ROOT / "configs" / "real-pool-csrc.yaml"
#: 已发布快照的数据目录。因子必须落在**这份**库里，
#: 否则研究卡（它读的就是这里）永远看不到数值。
SNAPSHOT_DIR = Path(os.environ.get(
    "AQUANT_DATA_DIR", str(ROOT / "deploy" / "universe-snapshot")))
FACTORS_REPORT = ROOT / "deploy" / "agentctl-q0" / "factors-persist.json"
SNAPSHOT_REPORT = ROOT / "deploy" / "agentctl-q0" / "snapshot-publish.json"
MARKET_CAPS_CACHE = ROOT / "deploy" / "agentctl-q0" / "decision-market-caps.json"

# 快照构建的质量检查目前以人类可读名称输出。只有这两项明确属于
# 可选的财务输入；其它检查（包括行情条数、前复权价、行业和交易日历）
# 都是价格型 S1 的输入或其交易约束，不能因为启用 degraded 发布而放行。
_OPTIONAL_FINANCIAL_CHECKS = frozenset({
    "财务缓存存在",
    "财务数据覆盖池内标的 >= 90%",
    "决策日总市值覆盖率 >= 90%",
})

# 结构化报告不完整时必须失败关闭。这样即使发布工具以后新增了一个
# 检查，也不会因为 daily_run 没有认识它而把半成品切成 current。
_S1_REQUIRED_CHECKS = frozenset({
    "交易日历覆盖 >= 120 日",
    "池内标的都有行情",
    "行情条数充足",
    "成交额覆盖率 >= 99%",
    "前复权收盘价覆盖率 >= 99%",
    "窗口首行前收已从行情补齐",
    "前收缺失只可能出现在窗口首行",
    "行业齐全",
    "覆盖主板（可模拟）",
    "公司行为带来源公告",
    "公司行为的来源已登记",
})


class SnapshotQuality:
    """发布报告的分层质量结果。

    ``core_failures`` 是 S1 的阻断项；``financial_failures`` 只影响
    F10 可用性。两者分开保存，调用方不会用一个布尔值吞掉失败原因。
    """

    __slots__ = ("core_failures", "financial_failures")

    def __init__(self, core_failures: tuple[dict, ...],
                 financial_failures: tuple[dict, ...]) -> None:
        self.core_failures = core_failures
        self.financial_failures = financial_failures

    @property
    def core_ok(self) -> bool:
        return not self.core_failures

    @property
    def degraded(self) -> bool:
        return bool(self.financial_failures)


def _classify_snapshot_checks(checks: object) -> SnapshotQuality:
    """按能力边界分类快照检查，未知/畸形检查按核心失败处理。

    ``publish_universe_snapshot.py --allow-degraded`` 允许工具写出物理
    快照，但并不替 daily_run 决定哪些检查可以降级；这个函数才是 S1
    发布的显式策略边界。
    """

    if not isinstance(checks, list):
        return SnapshotQuality(({
            "name": "发布报告缺少 checks",
            "detail": "quality report must contain a list of checks",
        },), ())

    core: list[dict] = []
    financial: list[dict] = []
    for item in checks:
        if not isinstance(item, dict) or not item.get("name"):
            core.append({
                "name": "发布报告包含畸形检查",
                "detail": repr(item),
            })
            continue
        if item.get("ok"):
            continue
        failure = {
            "name": str(item["name"]),
            "detail": str(item.get("detail") or ""),
        }
        if failure["name"] in _OPTIONAL_FINANCIAL_CHECKS:
            financial.append(failure)
        else:
            core.append(failure)
    return SnapshotQuality(tuple(core), tuple(financial))


def _read_snapshot_quality(path: Path, *, snapshot_id: str) -> SnapshotQuality:
    """读取并验证本次快照报告；读不到或不完整时失败关闭。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return SnapshotQuality(({
            "name": "无法读取快照质量报告",
            "detail": str(exc),
        },), ())
    if not isinstance(payload, dict):
        return SnapshotQuality(({
            "name": "快照质量报告格式错误",
            "detail": "report must be a JSON object",
        },), ())

    quality = _classify_snapshot_checks(payload.get("checks"))
    failures = list(quality.core_failures)
    if payload.get("snapshot_id") != snapshot_id:
        failures.append({
            "name": "快照质量报告 ID 不匹配",
            "detail": (f"expected {snapshot_id}, got "
                       f"{payload.get('snapshot_id')!r}"),
        })
    checks = payload.get("checks")
    names = {
        item.get("name") for item in checks
        if isinstance(item, dict) and item.get("name")
    } if isinstance(checks, list) else set()
    for required in sorted(_S1_REQUIRED_CHECKS - names):
        failures.append({
            "name": f"缺少 S1 核心检查：{required}",
            "detail": "publish report did not record this required gate",
        })
    return SnapshotQuality(tuple(failures), quality.financial_failures)


def _read_s1_capability(data_root: Path, *, snapshot_id: str) -> dict:
    """复用产品读取器确认候选快照真的能作为 S1 输入。"""

    con = None
    try:
        con = connect(data_root / "meta.sqlite")
        apply_migrations(con)
        reader = SnapshotReader(SnapshotStore(con, data_root / "api"))
        capability = reader.published_snapshot(snapshot_id).s1_decision
        return {
            "ok": capability.available,
            "name": capability.code,
            "detail": capability.message,
        }
    except Exception as exc:  # fail closed; the candidate remains unpromoted
        return {
            "ok": False,
            "name": "无法验证 S1 决策能力",
            "detail": str(exc),
        }
    finally:
        if con is not None:
            con.close()


def _factor_report_problem(path: Path, *, snapshot_id: str) -> str | None:
    """确认可选 F10 作业真的产出当前有效版本，而非仅写入空行。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"无法读取 F10 落库报告：{exc}"
    if not isinstance(payload, dict):
        return "F10 落库报告格式错误"
    if payload.get("snapshot_id") != snapshot_id:
        return ("F10 落库报告快照不匹配："
                f"expected {snapshot_id}, got {payload.get('snapshot_id')!r}")
    if payload.get("feature_version") != "f10-v2":
        return f"F10 特征版本无效：{payload.get('feature_version')!r}"
    if payload.get("validity_status") != "VALID":
        return f"F10 科学有效性不是 VALID：{payload.get('validity_status')!r}"
    if not str(payload.get("output_hash") or "").startswith("sha256:"):
        return "F10 输出哈希缺失"
    try:
        feature_rows = int(payload.get("feature_value_rows") or 0)
        valued_rows = int(payload.get("valued_rows") or 0)
    except (TypeError, ValueError):
        return "F10 落库报告的行数不是整数"
    if feature_rows <= 0:
        return "F10 未写入任何因子行"
    if valued_rows <= 0:
        return "F10 没有任何可用数值（检查决策日总市值与财务输入）"
    return None

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
    days = [
        row.get("trading_day")
        for entry in (doc.get("bars") or {}).values()
        for row in (entry.get("rows") or [])
        if isinstance(row, dict) and row.get("trading_day")
    ]
    return max(days) if days else None


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
    ap.add_argument("--data-dir", default=None,
                    help="统一数据根目录（默认 AQUANT_DATA_DIR 或 deploy/universe-snapshot）")
    ap.add_argument("--source", default="manual", choices=["manual", "scheduler"],
                    help="谁发起的一次运行。写进留痕，用来回答"
                         "「这次是到点跑的，还是有人点了按钮」——"
                         "两者都合法，但排查时的下一步不一样。")
    ap.add_argument("--snapshot-id", default=None,
                    help="显式物理快照 ID；省略时每次发布生成唯一 ID，"
                         "当前快照由 current_snapshot.json 指针解析。")
    ap.add_argument("--json-out", default=None,
                    help="把本次留痕记录写成 JSON（供调度 worker 落库成运行摘要）。"
                         "留痕文件仍是权威，这份只是同一份事实的机器可读副本。")
    args = ap.parse_args()

    global SNAPSHOT_DIR
    if args.data_dir:
        SNAPSHOT_DIR = Path(args.data_dir).expanduser()
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
              else market_now().date())
    day = target.isoformat()
    # 生产默认值等采集确认实际末日后再生成，避免休市跳过也消耗一个
    # 看似已发布的物理 ID。显式 ID 只用于受控回放/迁移，仍禁止覆盖。
    snapshot_id = args.snapshot_id or "pending"
    run_log = RunLog(RUNS)
    record = RunRecord(trading_day=day, snapshot_id=snapshot_id, outcome="FAILED",
                       started_at=datetime.now(timezone.utc).isoformat(),
                       source=args.source)

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
        print("  2. 收盘后采集决策日总市值（失败时仅降级 F10）")
        print("  3. 发布一个新的唯一物理快照，并更新 current_snapshot.json")
        print("  4. 在该快照上计算 F10 因子")
        print("  5. 追加运行日志 " + str(RUNS))
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
                return _finish(run_log, record, started, 1, json_out=args.json_out)

            # 采集之后**核对实际拿到了哪一天**，再决定 ID。
            actual_last = _cache_last_day()
            record.data_last_day = actual_last
            if actual_last is None:
                record.reason = "缓存的行情窗口为空"
                alert("采集缓存里没有任何行情", tradingDay=day)
                return _finish(run_log, record, started, 1, json_out=args.json_out)
            if actual_last != day:
                record.outcome = "SKIPPED"
                record.reason = (f"目标日 {day} 没有新行情（休市或数据未更新），"
                                 f"缓存实际末日 {actual_last}")
                # 按计划跳过而不是失败：休市不是错误。
                # 报失败会让定时任务重试到天亮，而结果不会变。
                return _finish(run_log, record, started, 0, json_out=args.json_out)
            # 2. 建快照。窗口收窄到固定的起点。生产服务为每次运行生成
            # 唯一物理 ID，保留所有旧目录与 meta.sqlite，最后原子替换当前指针。
            snapshot_id = args.snapshot_id or new_snapshot_id(actual_last)
            record.snapshot_id = snapshot_id

            # F10 的分母必须是与行情末日一致的总市值。该能力对价格型 S1
            # 是可选的：采集失败仍发布 S1，但显式传入一个不存在的 sidecar
            # 路径，避免发布器误读上一交易日遗留的旧文件。
            cap_step = run_step([
                "tools/collect_market_caps.py", "--as-of", actual_last,
                "--output", str(MARKET_CAPS_CACHE),
            ], label="采集决策日总市值")
            record.steps.append(cap_step)
            market_caps_path = MARKET_CAPS_CACHE
            if not cap_step["ok"]:
                market_caps_path = MARKET_CAPS_CACHE.with_name(
                    f"decision-market-caps-missing-{actual_last}.json")
                alert("决策日总市值采集失败，按价格型 S1 降级继续",
                      tradingDay=day, snapshotId=snapshot_id,
                      exitCode=cap_step["exitCode"], tail=cap_step["tail"])

            step = run_step([
                "tools/publish_universe_snapshot.py",
                "--data-dir", str(SNAPSHOT_DIR),
                "--cache", str(CACHE), "--pool", str(POOL),
                "--financials", str(FINANCIALS_CACHE),
                "--actions", str(ACTIONS_CACHE),
                "--market-caps", str(market_caps_path),
                "--check-market-caps",
                "--snapshot-id", snapshot_id,
                "--window-start", args.window_start,
                "--window-end", actual_last,
                # 财务覆盖是可选能力；质量报告回读后只把明确的财务
                # 检查降级，行情/价格检查仍由 daily_run 逐项阻断。
                "--allow-degraded",
                "--defer-promotion",
                "--json-out", str(SNAPSHOT_REPORT),
            ], label="发布快照")
            record.steps.append(step)
            if not step["ok"]:
                # 快照构建里的"行情条数充足 / 窗口首行前收"两条校验
                # 就是休市与数据缺口的判据：它们失败说明这一天没有
                # 可发布的内容，而不是脚本坏了。
                record.reason = "快照构建未通过校验（可能是休市或数据未更新）"
                alert("快照构建未通过校验", tradingDay=day,
                      exitCode=step["exitCode"], tail=step["tail"])
                return _finish(run_log, record, started, 1, json_out=args.json_out)

            quality = _read_snapshot_quality(
                SNAPSHOT_REPORT, snapshot_id=snapshot_id)
            s1_capability = _read_s1_capability(
                SNAPSHOT_DIR, snapshot_id=snapshot_id)
            core_failures = list(quality.core_failures)
            if not s1_capability["ok"]:
                core_failures.append({
                    "name": "S1 决策能力",
                    "detail": (f"{s1_capability['name']}: "
                               f"{s1_capability['detail']}"),
                })
            price_step = {
                "step": "S1 行情/价格质量闸门",
                "ok": not core_failures,
                "seconds": 0.0,
                "tail": [
                    ("通过" if not core_failures else
                     "失败：" + "; ".join(
                         f"{item['name']} {item['detail']}"
                         for item in core_failures))
                ],
                "exitCode": 0 if not core_failures else 1,
            }
            record.steps.append(price_step)
            if core_failures:
                record.reason = "S1 行情/价格质量闸门未通过；current 保持不变"
                alert("S1 行情/价格质量闸门未通过，未切换 current",
                      tradingDay=day, snapshotId=snapshot_id,
                      failures=core_failures)
                return _finish(run_log, record, started, 1,
                               json_out=args.json_out)

            degraded_reasons = [
                f"{item['name']}: {item['detail']}"
                for item in quality.financial_failures
            ]
            if degraded_reasons:
                alert("财务因子输入质量未通过，按价格型 S1 降级继续",
                      tradingDay=day, snapshotId=snapshot_id,
                      failures=list(quality.financial_failures))

            # 3. F10 是可选的财务增强因子。它仍然执行并落库/验收，
            #    但失败只形成降级状态，不能阻断已通过 S1 价格闸门的快照。
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
                degraded_reasons.append("F10 因子未落库")
                alert("F10 因子未落库，按价格型 S1 降级继续",
                      tradingDay=day, snapshotId=snapshot_id,
                      exitCode=step["exitCode"], tail=step["tail"])
            else:
                report_problem = _factor_report_problem(
                    FACTORS_REPORT, snapshot_id=snapshot_id)
                if report_problem:
                    degraded_reasons.append(report_problem)
                    alert("F10 落库结果不可用于当前研究，按价格型 S1 降级继续",
                          tradingDay=day, snapshotId=snapshot_id,
                          problem=report_problem)

            # 4. 因子质量闸门。失败不影响"快照已发布"与"因子已落库"这两个事实，
            #    但它说明数值本身有问题（单位、值域、亏损股被截断为 0……），
            #    必须留下痕迹。
            step = run_step(["-m", "tests.integration.t12_f10_real"],
                            label="F10 质量闸门")
            record.steps.append(step)
            if not step["ok"]:
                degraded_reasons.append("F10 质量闸门未通过")
                alert("F10 质量闸门未通过，按价格型 S1 降级继续",
                      tradingDay=day, snapshotId=snapshot_id,
                      exitCode=step["exitCode"], tail=step["tail"])

            # 5. 价格型 S1 闸门通过后即可原子切换 current。F10 不再使用
            #    --require-factors：它属于可选增强能力，而非 S1 的完成条件。
            step = run_step([
                "tools/promote_snapshot.py",
                "--data-dir", str(SNAPSHOT_DIR),
                "--snapshot-id", snapshot_id,
            ], label="切换当前快照")
            record.steps.append(step)
            if not step["ok"]:
                record.reason = "快照提升失败；current 保持不变"
                alert("快照提升失败，current 保持不变",
                      tradingDay=day, snapshotId=snapshot_id,
                      exitCode=step["exitCode"], tail=step["tail"])
                return _finish(run_log, record, started, 1, json_out=args.json_out)

            record.outcome = "PUBLISHED"
            if degraded_reasons:
                record.reason = ("价格型 S1 已发布；F10 财务因子降级："
                                 + "；".join(degraded_reasons))
            return _finish(run_log, record, started, 0, json_out=args.json_out)

    except PipelineBusy as exc:
        record.reason = str(exc)
        record.outcome = "SKIPPED"
        return _finish(run_log, record, started, 0, json_out=args.json_out,
                       message="上一次运行还在进行，本次跳过（不算失败）")


def _finish(run_log: RunLog, record: RunRecord, started: float,
            code: int, *, message: str | None = None,
            json_out: str | None = None) -> int:
    record.finished_at = datetime.now(timezone.utc).isoformat()
    record.duration_seconds = round(time.time() - started, 1)
    run_log.append(record)
    if json_out:
        # 先落盘再说话：工人进程靠这个文件回填运行摘要，
        # 而"留痕写成功了、摘要没拿到"是可以接受的降级（摘要能重建）。
        import json as _json

        path = Path(json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_json.dumps(record.as_dict(), ensure_ascii=False,
                                     indent=2).encode("utf-8"))
    log(message or ("结果 " + record.outcome
                    + ("：" + record.reason if record.reason else "")))
    log("留痕 " + str(RUNS))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
