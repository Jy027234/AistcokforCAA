"""S2 增量日更：把已有入口串成可恢复的每日作业。

实施方案要求"沿用既有收盘后和盘前作业位置接入增量任务，不另建重复调度系统"，
并且每只证券有状态、可恢复、有水位、失败隔离。本工具就是那个入口——它**不**
新建调度器，只把已经实现的步骤按顺序跑一遍，并把每一步的退出码与产物写进
一份运行记录：

1. ``calendar``：刷新交易日历证据（该证据是**当日件**，跨日失效）；
2. ``collect_s2_company_index``：公司级共享索引 + 增量水位；
3. ``promote_s2_machine_facts``：按水位与冻结需求做机器核验升仓；
4. ``report_s2_coverage``：产出覆盖报告。

设计约束：

* **失败隔离**：某只证券失败不终止整批，但步骤级失败会终止后续依赖步骤
  （例如日历证据拿不到就不做升仓，因为 ``available_at`` 需要它）。
* **有限重试**：每一步只重试有限次，且重试之间有退避；不做无限循环。
* ``--dry-run`` 只做索引与核验，不写事实库。
* 运行数据（事实库、归档、快照）由调用方显式给出，工具不猜路径。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = timezone(timedelta(hours=8))
DEFAULT_DIR = ROOT / "data" / "s2-pdf-review"
DEFAULT_ARCHIVE = ROOT / "deploy" / "agentctl-q0" / "forward-archive"
DEFAULT_FACTS = DEFAULT_DIR / "s2-machine-verified-facts.sqlite"
SCHEMA = "aquant.s2-daily-run.v1"

#: 每步允许的尝试次数与退避秒数。有限、可预期，不无限重试。
STEP_ATTEMPTS = 2
STEP_BACKOFF_SECONDS = 20.0


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_step(name: str, argv: list[str], *, env: dict[str, str],
             attempts: int = STEP_ATTEMPTS) -> dict[str, object]:
    """跑一个步骤，返回退出码、耗时与输出尾部；有限重试。"""

    record: dict[str, object] = {"step": name, "argv": argv, "attempts": []}
    for attempt in range(1, max(1, attempts) + 1):
        started = time.monotonic()
        completed = subprocess.run(  # noqa: S603 - argv is built here, not user shell
            argv, cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        elapsed = round(time.monotonic() - started, 2)
        tail = "\n".join(
            (completed.stdout or "").strip().splitlines()[-6:]
            + (completed.stderr or "").strip().splitlines()[-4:]
        )
        record["attempts"].append({  # type: ignore[union-attr]
            "attempt": attempt, "exit_code": completed.returncode,
            "elapsed_seconds": elapsed, "tail": tail,
        })
        if completed.returncode == 0:
            record["exit_code"] = 0
            record["elapsed_seconds"] = elapsed
            record["tail"] = tail
            return record
        if attempt < max(1, attempts):
            time.sleep(STEP_BACKOFF_SECONDS)
    record["exit_code"] = record["attempts"][-1]["exit_code"]  # type: ignore[index]
    record["elapsed_seconds"] = record["attempts"][-1]["elapsed_seconds"]  # type: ignore[index]
    record["tail"] = record["attempts"][-1]["tail"]  # type: ignore[index]
    return record


#: 失败状态词。退出码 0 **不等于**每只证券都成功：官方索引可能整批失败而命令
#: 仍以 0 结束（实测：不带代理跑一遍，四步全部 exit 0，而 5 只证券全是
#: INDEX_INCOMPLETE）。因此运行结论必须从产物里的状态分布判定，不能只看退出码。
FAILURE_STATUS_MARKERS = ("FAILED", "INCOMPLETE", "BLOCKED", "NOT_UNIQUE",
                          "REVIEW_REQUIRED", "NO_LATEST_PERIOD", "NO_REPORT_ATTEMPTED")


def _artifact(path: Path, key: str) -> dict | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = payload.get(key)
    return value if isinstance(value, dict) else None


def _step_outcome(name: str, counts: dict[str, int] | None) -> tuple[str, dict]:
    """把某一步的状态分布折算成 (结论, 失败明细)。"""

    if counts is None:
        return "NO_ARTIFACT", {}
    failures = {
        status: number for status, number in counts.items()
        if any(marker in status for marker in FAILURE_STATUS_MARKERS)
    }
    if not counts:
        return "EMPTY", {}
    if failures and sum(failures.values()) >= sum(counts.values()):
        return "ALL_FAILED", failures
    if failures:
        return "PARTIAL", failures
    return "OK", {}


def _overall(steps: list[dict], outcomes: dict[str, tuple[str, dict]]) -> str:
    """运行总评。按**根因优先**排列：索引全失败是更靠前的故障，

    不能只报"没有证券升仓"——那会让读者去查升仓，而真正坏掉的是索引。
    """

    if any(step.get("exit_code") not in (0, None) for step in steps):
        return "STEP_FAILED"
    if outcomes.get("company_index", ("", {}))[0] == "ALL_FAILED":
        return "INDEX_ALL_FAILED"
    if outcomes.get("promote", ("", {}))[0] == "ALL_FAILED":
        return "NO_SECURITY_PROMOTED"
    if any(value[0] in ("NO_ARTIFACT", "EMPTY") for value in outcomes.values()):
        return "ARTIFACT_MISSING"
    if any(value[0] in ("PARTIAL", "ALL_FAILED") for value in outcomes.values()):
        return "PARTIAL"
    return "OK"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--facts-db", type=Path, default=DEFAULT_FACTS)
    parser.add_argument("--through", type=date.fromisoformat, default=None,
                        help="北京时间截止日，默认今天")
    parser.add_argument("--stock", action="append", default=[],
                        help="repeatable; 默认取 manifest 里全部 DEMAND_FROZEN 证券")
    parser.add_argument("--trusted-proxy-network", action="append", default=[],
                        help="repeatable; 进程级放行的透明代理伪 IP 网段")
    parser.add_argument("--dry-run", action="store_true",
                        help="只做索引与核验，不写事实库")
    parser.add_argument("--skip-calendar", action="store_true",
                        help="复用已存在的当日日历证据文件")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--snapshot-dir", type=Path,
                        help="可选：产出正式readiness的已发布冻结快照目录")
    parser.add_argument("--snapshot-id", help="须与--snapshot-dir同时提供")
    parser.add_argument("--latest-period-end", type=date.fromisoformat,
                        help="正式readiness固定需求期，不更改既有manifest")
    parser.add_argument("--readiness-output", type=Path)
    parsed = parser.parse_args(argv)
    if bool(parsed.snapshot_dir) != bool(parsed.snapshot_id):
        parser.error("--snapshot-dir 与 --snapshot-id 必须同时提供")

    through = parsed.through or datetime.now(SHANGHAI).date()
    directory = parsed.dir.resolve()
    archive_root = parsed.archive_root.resolve()
    facts_db = parsed.facts_db.resolve()
    directory.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    if parsed.trusted_proxy_network:
        env["AQUANT_TRUSTED_PROXY_NETWORKS"] = ",".join(parsed.trusted_proxy_network)
    py = sys.executable
    stamp = _stamp()
    calendar_path = directory / f"calendar-evidence-{stamp}.json"
    index_path = directory / "s2-company-index.json"
    run_path = directory / "s2-machine-promotion" / f"daily-{stamp}.json"
    coverage_path = directory / "s2-coverage-report.json"
    (directory / "s2-machine-promotion").mkdir(parents=True, exist_ok=True)

    steps: list[dict[str, object]] = []
    started_at = datetime.now(timezone.utc)

    common_proxy: list[str] = []
    for network in parsed.trusted_proxy_network:
        common_proxy += ["--trusted-proxy-network", network]
    stock_args: list[str] = []
    for code in parsed.stock:
        stock_args += ["--stock", code]

    # 1) 日历证据是当日件：跨日即失效，因此每天先刷新。
    if parsed.skip_calendar:
        existing = sorted(directory.glob("calendar-evidence-*.json"))
        calendar_path = existing[-1] if existing else calendar_path
        steps.append({
            "step": "calendar", "skipped": True, "reused": str(calendar_path),
            "exit_code": 0 if calendar_path.is_file() else 4,
        })
    else:
        steps.append(run_step("calendar", [
            py, "tools/admit_s2_industrial_pdf_review.py", "calendar",
            "--archive-root", str(archive_root), "--output", str(calendar_path),
        ], env=env))
    if steps[-1].get("exit_code") != 0:
        report = _report(started_at, through, directory, archive_root, facts_db,
                         parsed, steps, calendar_path, index_path, run_path,
                         coverage_path, aborted="calendar evidence unavailable")
        _write(report, parsed.output or directory / f"s2-daily-{stamp}.json")
        return 2

    # 2) 公司级共享索引 + 增量水位。
    steps.append(run_step("company_index", [
        py, "tools/collect_s2_company_index.py",
        "--manifest", str(directory / "s2-demand-manifest.json"),
        "--archive-root", str(archive_root), "--output", str(index_path),
        "--through", through.isoformat(), *stock_args, *common_proxy,
    ], env=env))

    # 3) 机器核验升仓。索引步骤失败时仍尝试升仓（升仓自带索引校验）。
    promote: list[str] = [
        py, "tools/promote_s2_machine_facts.py",
        "--manifest", str(directory / "s2-demand-manifest.json"),
        "--archive-root", str(archive_root), "--fact-db", str(facts_db),
        "--calendar-evidence", str(calendar_path),
        "--cross-through", through.isoformat(), "--output", str(run_path),
        *stock_args,
    ]
    if not parsed.dry_run:
        promote.append("--commit")
    steps.append(run_step("promote", promote, env=env))

    # 4) 覆盖报告总是产出：它是本次运行的可读结论。
    steps.append(run_step("coverage", [
        py, "tools/report_s2_coverage.py", "--dir", str(directory),
        "--facts-db", str(facts_db), "--output", str(coverage_path),
    ], env=env))
    readiness_path = parsed.readiness_output or directory / f"s2-readiness-{stamp}.json"
    if parsed.snapshot_id:
        readiness = [py, "tools/report_s2_readiness.py", "--snapshot-dir", str(parsed.snapshot_dir),
                     "--snapshot-id", parsed.snapshot_id, "--facts-db", str(facts_db),
                     "--market-caps-archive-root", str(archive_root),
                     "--include-signals", "--json-out", str(readiness_path)]
        if parsed.latest_period_end:
            readiness += ["--latest-period-end", parsed.latest_period_end.isoformat()]
        steps.append(run_step("readiness", readiness, env=env))

    report = _report(started_at, through, directory, archive_root, facts_db, parsed,
                     steps, calendar_path, index_path, run_path, coverage_path)
    outcomes = {
        "company_index": _step_outcome(
            "company_index", _artifact(index_path, "counts")),
        "promote": _step_outcome(
            "promote", _artifact(run_path, "counts")),
        "coverage": _step_outcome(
            "coverage", _artifact(coverage_path, "coverage_status_counts")),
    }
    report["step_outcomes"] = {
        name: {"outcome": value[0], "failures": value[1]}
        for name, value in outcomes.items()
    }
    report["overall_status"] = _overall(steps, outcomes)
    if parsed.snapshot_id:
        report["artifacts"]["readiness_report"] = str(readiness_path) if readiness_path.is_file() else None
        if readiness_path.is_file():
            try:
                readiness_result = json.loads(readiness_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                readiness_result = {}
            report["readiness_scope_status"] = readiness_result.get("scope_status")
            report["readiness_complete_s2_count"] = readiness_result.get("complete_s2_count")
    output = parsed.output or directory / f"s2-daily-{stamp}.json"
    _write(report, output)
    print(f"{output}: " + " ".join(
        f"{step['step']}={step.get('exit_code')}" for step in steps))
    print(f"  overall_status={report['overall_status']} "
          + " ".join(f"{name}={value[0]}" for name, value in outcomes.items()))
    for name, (outcome, failures) in outcomes.items():
        if outcome not in ("OK", "NO_ARTIFACT", "EMPTY"):
            print(f"  {name} not clean: {failures}")
    for step in steps:
        if step.get("exit_code") not in (0, None):
            print(f"  {step['step']} failed: {str(step.get('tail',''))[:200]}")
    return 0 if report["overall_status"] == "OK" else 1


def _report(started_at: datetime, through, directory: Path, archive_root: Path,
            facts_db: Path, parsed, steps: list[dict], calendar_path: Path,
            index_path: Path, run_path: Path, coverage_path: Path,
            aborted: str | None = None) -> dict[str, object]:
    def _counts(path: Path, key: str) -> dict | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload.get(key)

    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "started_at": started_at.isoformat(),
        "observed_through_date": through.isoformat(),
        "dry_run": bool(parsed.dry_run),
        "aborted": aborted,
        "inputs": {
            "directory": str(directory),
            "archive_root": str(archive_root),
            "facts_db": str(facts_db) if facts_db.is_file() else None,
            "selected_stocks": list(parsed.stock),
        },
        "artifacts": {
            "calendar_evidence": str(calendar_path) if calendar_path.is_file() else None,
            "company_index": str(index_path) if index_path.is_file() else None,
            "promotion_run": str(run_path) if run_path.is_file() else None,
            "coverage_report": str(coverage_path) if coverage_path.is_file() else None,
        },
        "steps": steps,
        "company_index_counts": _counts(index_path, "counts"),
        "promotion_counts": _counts(run_path, "counts"),
        "coverage_status_counts": _counts(coverage_path, "coverage_status_counts"),
        "boundary": (
            "This is the incremental entry point, not a scheduler: it runs the "
            "existing steps in order and records their exit codes. A step failure "
            "is isolated per security, but the calendar step gates promotion "
            "because available_at depends on it."
        ),
    }


def _write(report: dict, output: Path) -> None:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=1, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
