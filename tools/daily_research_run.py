"""Daily full-scope research refresh with a separately verified simulation pool.

Provider calls are delegated to the Luna-authored capture CLIs. The calendar
gate and publication coordination themselves are entirely offline. A closed
market day records SKIPPED before starting any provider subprocess.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from aquant.domain.data.forward_archive import ForwardArchive
from aquant.domain.data.db import connect, write_tx
from aquant.operations.alerting import Alert, AlertLog, LEVEL_ERROR, LEVEL_WARNING
from aquant.operations.pipeline import PipelineBusy, PipelineLock, RunLog, RunRecord
from aquant.operations.scheduler import market_now
from aquant.operations.research_scan import load_research_scan
from aquant.operations.snapshot_lifecycle import new_snapshot_id
from aquant.operations.universe_snapshot import promote_universe_snapshot

DEFAULT_RESEARCH = ROOT / "data/s2-structured/research-candidates.json"
DEFAULT_CALENDAR = ROOT / "data/s2-structured/simulation-evidence-20261011/summary.json"
DEFAULT_RUNS = ROOT / "deploy/agentctl-q0/daily-runs.jsonl"
DEFAULT_LOCK = ROOT / "deploy/agentctl-q0/daily-run.lock"
DEFAULT_ARTIFACT_ROOT = ROOT / "data/s2-structured/daily"
DEFAULT_ARCHIVE = ROOT / "deploy/agentctl-q0/forward-archive"
DEFAULT_ALERTS = ROOT / "deploy/agentctl-q0/alerts.jsonl"
MAIN_ID = re.compile(r"^(?:SH\.60\d{4}|SZ\.00\d{4})$")


def file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def verified_calendar_day(summary_path: Path, target: date, *, now: datetime) -> dict:
    """Read the full-year calendar from original provider bytes and its receipt."""
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("schema") != "aquant.simulation_research_evidence.v1":
        raise ValueError("unsupported calendar evidence schema")
    calendar = summary["trading_calendar"]
    receipt = calendar["calendar_receipt"]
    con = sqlite3.connect(summary_path.parent / "archive.sqlite")
    con.row_factory = sqlite3.Row
    try:
        saved = con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?", (receipt["receipt_id"],)).fetchone()
        fields = ("source_id", "url", "content_hash", "requested_at", "responded_at", "first_seen_at")
        if saved is None or saved["outcome"] != "OK" or any(saved[k] != receipt[k] for k in fields):
            raise ValueError("calendar receipt differs from original archive")
        seen = datetime.fromisoformat(saved["first_seen_at"])
        if now.tzinfo is None or seen.tzinfo is None or seen > now:
            raise ValueError("calendar evidence is not yet available")
        archive = ForwardArchive(con, summary_path.parent / "archive")
        if not archive.verify(saved["content_hash"]):
            raise ValueError("calendar raw response hash mismatch")
        body = archive.load_bytes(saved["content_hash"])
        if len(body) != saved["byte_size"]:
            raise ValueError("calendar raw response byte count mismatch")
        rows = json.loads(body)
    finally:
        con.close()
    if rows != calendar["official_raw_rows"]:
        raise ValueError("calendar summary differs from original provider rows")
    first, last = date(target.year, 1, 1), date(target.year, 12, 31)
    expected = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    actual = [row.get("calendar_date") for row in rows]
    if actual != expected or any(row.get("is_trading_day") not in {"0", "1"} for row in rows):
        raise ValueError(f"calendar does not cover the complete {target.year} year")
    required_url = f"query_trade_dates?start_date={first}&end_date={last}"
    if required_url not in saved["url"] or saved["source_id"] != "baostock-socket":
        raise ValueError("calendar receipt is not the expected annual provider query")
    is_open = next(row["is_trading_day"] == "1" for row in rows if row["calendar_date"] == target.isoformat())
    return {"tradingDay": target.isoformat(), "isTradingDay": is_open,
            "receiptId": receipt["receipt_id"], "contentHash": receipt["content_hash"],
            "availableAt": receipt["first_seen_at"], "providerCalls": 0}


def run_step(args: list[str], *, label: str) -> dict:
    started = time.monotonic()
    proc = subprocess.run([sys.executable, *args], cwd=ROOT, text=True, encoding="utf-8",
                          errors="replace", capture_output=True)
    step = {"step": label, "ok": proc.returncode == 0,
            "seconds": round(time.monotonic() - started, 1), "exitCode": proc.returncode,
            "tail": (proc.stdout or "").strip().splitlines()[-4:]}
    if proc.stderr:
        step["stderrTail"] = proc.stderr.strip().splitlines()[-2:]
    print(json.dumps(step, ensure_ascii=False), flush=True)
    return step


def publication_files(summary_path: Path, *, research: Path, day: str) -> tuple[Path, Path]:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("evidence_market_date") != day:
        raise ValueError("simulation evidence market day is not the requested day")
    source = summary["source_inputs"]["research_candidates"]
    if file_hash(research) != source["sha256"] or Path(source["path"]).resolve() != research.resolve():
        raise ValueError("simulation evidence was not captured from this staged research publication")
    files = summary["ready_to_publish_files"]
    cache, pool = Path(files["cache"]), Path(files["pool"])
    if not cache.is_file() or not pool.is_file():
        raise ValueError("simulation publication files are missing")
    return cache, pool


def reuse_complete_simulation_evidence(summary: Path, *, research: Path, day: str,
                                      limit: int, retained: Path) -> bool:
    """Resume an already captured full package without opening a provider session."""
    if not summary.exists():
        return False
    packet = json.loads(summary.read_text(encoding="utf-8"))
    if packet.get("failures"):
        return False
    cache, pool = publication_files(summary, research=research, day=day)
    from capture_research_simulation_evidence import select_candidates
    from aquant.operations.simulation_evidence import admit_simulation_evidence
    retained_packet = json.loads(retained.read_text(encoding="utf-8"))
    selected = select_candidates(json.loads(research.read_text(encoding="utf-8")),
                                 limit=limit, retained_ids=retained_packet["instrument_ids"])
    if {c["instrumentId"] for c in selected} != {c["instrument_id"] for c in packet["instruments"]}:
        raise ValueError("complete simulation evidence differs from current retained selection")
    admit_simulation_evidence(summary, cache_path=cache, pool_path=pool,
                              cutoff=datetime.now(timezone.utc))
    return True


def announcement_targets(research: Path, retained: dict, *, limit: int = 20) -> list[dict]:
    """Budget official queries for retained selections, then current candidates."""
    packet = json.loads(research.read_text(encoding="utf-8"))
    cards = packet["candidates"]
    by_id = {c["instrumentId"]: c for c in cards}
    ranked = sorted((c for c in cards if c.get("status") == "RANKED"),
                    key=lambda c: c["position"])
    priority = [by_id[iid] for iid in retained["instrument_ids"] if iid in by_id]
    targets, seen = [], set()
    for card in [*priority, *ranked]:
        if card["code"] not in seen:
            targets.append({"code": card["code"], "name": card["displayName"]})
            seen.add(card["code"])
        if len(targets) == limit:
            break
    if not targets:
        raise ValueError("no current or retained candidates for announcement research")
    return targets


def refresh_daily_announcements(*, attempt: Path, ranking: Path, staged: Path,
                                retained: dict, target: date, archive_root: Path,
                                alerts: Path, record: RunRecord, extra: dict) -> None:
    """Optional official evidence refresh; a provider failure never blocks S1."""
    targets = attempt / "announcement-targets.json"
    evidence = attempt / "announcements.json"
    manifest = attempt / "announcements-manifest.json"
    files = {"ranking": ranking, "targets": targets, "research": staged}
    if manifest.exists():
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        if saved.get("refreshed"):
            files["announcements"] = evidence
        verify_stage_checkpoint(manifest, stage="ANNOUNCEMENTS", files=files)
        extra["announcementRefresh"] = saved["result"]
        record.steps.append({"step": "恢复已核验公告发布阶段", "ok": True,
                             "seconds": 0, "reusedVerifiedCheckpoint": True})
        return
    start = target - timedelta(days=30)
    result = {"refreshed": False, "window": [start.isoformat(), target.isoformat()]}
    try:
        selected = announcement_targets(ranking, retained)
        write_json(targets, selected)
        result["targetCount"] = len(selected)
        extra["providerSubprocessesStarted"] += 1
        step = run_step(["tools/capture_research_announcements.py", "--targets", str(targets),
                         "--archive-root", str(archive_root), "--output", str(evidence),
                         "--checkpoint", str(attempt / "announcement-checkpoint.jsonl"),
                         "--start", start.isoformat(), "--end", target.isoformat()],
                        label="Luna采集工具刷新优先候选官方公告")
        record.steps.append(step)
        if not step["ok"]:
            raise ValueError("official announcement capture did not complete")
        packet = json.loads(evidence.read_text(encoding="utf-8"))
        if (packet["target_codes"] != [t["code"] for t in selected]
                or packet["window"] != {"start": start.isoformat(), "end": target.isoformat()}
                or packet["queryCompleteCount"] != len(selected)):
            raise ValueError("announcement capture differs from this day's query targets/window")
        step = run_step(["tools/refresh_research_publication.py", "--baseline", str(ranking),
                         "--announcements", str(evidence), "--archive-root", str(archive_root),
                         "--output", str(staged)], label="离线核验并冻结新公告研究输入")
        record.steps.append(step)
        if not step["ok"]:
            raise ValueError("official announcement receipt/PDF admission failed")
        load_research_scan(staged, limit=1)
        result.update(refreshed=True, completeQueries=packet["queryCompleteCount"],
                      documentCount=packet["capturedDocumentCount"],
                      pdfFailureCount=sum(len(q.get("pdfFailures", [])) for q in packet["per_code"]))
        files["announcements"] = evidence
    except Exception as exc:
        result["reason"] = str(exc)
        # Retain the real old disclosure source dates; failed queries are not fresh evidence.
        staged.write_bytes(ranking.read_bytes())
        if not targets.exists():
            write_json(targets, [])
        AlertLog(alerts).write(Alert(level=LEVEL_WARNING, source="daily_research_run",
                                    message="公告刷新降级；保留既有公告日期，继续行情模拟",
                                    detail={"tradingDay": target.isoformat(), **result}))
    extra["announcementRefresh"] = result
    write_json(manifest, {"schema": "aquant.daily_stage_checkpoint.v1", "stage": "ANNOUNCEMENTS",
                          "files": stage_binding(files), "refreshed": result["refreshed"], "result": result,
                          "completed_at": datetime.now(timezone.utc).isoformat()})


def verify_quote_capture(coverage_path: Path, *, baseline: Path, day: str) -> dict:
    """Require full-universe requests, while preserving genuine empty quotes.

    The capture CLI returns zero once it has any rows. That is insufficient
    after its connection breaker stops midway: partial requests must not
    replace the previously admitted full-scope research publication.
    """
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    previous = json.loads(baseline.read_text(encoding="utf-8"))
    total = int(previous["poolSize"])
    batches = coverage.get("batches") or []
    requested = {symbol for batch in batches for symbol in batch.get("requested_symbols", [])}
    if (coverage.get("market_date") != day
            or coverage.get("quote_time_kind") != "after_close_research_snapshot"
            or coverage.get("official_universe_count") != total
            or coverage.get("official_supported_count") != total
            or coverage.get("selected_count") != total
            or len(requested) != total
            or coverage.get("stopped_on_breaker")
            or not batches
            or any(batch.get("status") != "ok" for batch in batches)
            or any(row.get("reason") == "request_failed"
                   for row in coverage.get("excluded_quote_rows", []))):
        raise ValueError("full-universe quote requests are incomplete or failed; keep prior publication")
    return {"officialUniverseCount": total, "requestedCount": len(requested),
            "capturedCount": coverage.get("captured_count"),
            "missingResponseCount": sum(coverage.get("missing_by_exchange", {}).values())}


def retained_instruments(data_dir: Path, *, target: date) -> dict:
    """Retain saved forward selections and active ledger positions offline."""
    reasons: dict[str, list[dict]] = {}
    skipped: list[dict] = []

    def retain(iid: str, reason: dict) -> None:
        if MAIN_ID.fullmatch(iid):
            reasons.setdefault(iid, []).append(reason)
        else:
            skipped.append({"instrument_id": iid, "reason": "OUTSIDE_VERIFIED_MAIN_SIMULATION_SCOPE",
                            "origin": reason})

    con = connect(data_dir / "meta.sqlite", read_only=True)
    try:
        rows = con.execute("SELECT draft_id,portfolio_id,trading_day,research_content_hash,draft_json "
                           "FROM research_simulation_draft WHERE trading_day>=? ORDER BY draft_id",
                           (target.isoformat(),)).fetchall()
        for row in rows:
            draft = json.loads(row["draft_json"])
            selected = draft["selection"]["selectedInstrumentIds"]
            if (draft["selectedInstrumentIds"] != selected
                    or draft["portfolioId"] != row["portfolio_id"]
                    or draft["tradingDay"] != row["trading_day"]
                    or draft["researchContentHash"] != row["research_content_hash"]):
                raise ValueError("retained draft binding differs from its immutable record")
            for iid in selected:
                retain(iid, {"kind": "PENDING_DRAFT", "draft_id": row["draft_id"],
                             "portfolio_id": row["portfolio_id"], "expected_execution_day": row["trading_day"],
                             "research_content_hash": row["research_content_hash"]})
        for row in con.execute("SELECT l.portfolio_id,l.instrument_id,SUM(l.quantity_remaining) AS quantity "
            "FROM position_lot l JOIN portfolio p ON p.portfolio_id=l.portfolio_id "
            "WHERE p.status='ACTIVE' AND l.quantity_remaining>0 AND p.portfolio_id NOT LIKE 'pf-cmp-%' "
            "GROUP BY l.portfolio_id,l.instrument_id ORDER BY l.portfolio_id,l.instrument_id"):
            retain(row["instrument_id"], {"kind": "ACTIVE_HOLDING", "portfolio_id": row["portfolio_id"],
                                          "quantity_remaining": row["quantity"]})
    finally:
        con.close()
    # B's frozen full common pool must survive new research rankings before
    # its first fills exist. Afterwards active lots provide the retention.
    from aquant.operations.forward_comparison import ForwardComparisonStore
    forward_store = ForwardComparisonStore(data_dir / "forward-comparison")
    for series in forward_store.list_series():
        if series["observedSessionCount"] or date.fromisoformat(series["firstTradingDay"]) > target:
            continue
        spec = forward_store.spec(series["seriesId"])
        spec_path = forward_store.root / (spec["seriesId"] + ".spec.json")
        for iid in spec["commonPool"]:
            retain(iid, {"kind": "FORWARD_COMPARISON_COMMON_POOL", "series_id": spec["seriesId"],
                         "first_trading_day": spec["firstTradingDay"], "spec_content_hash": file_hash(spec_path)})
    return {"schema": "aquant.simulation_retained_instruments.v1", "trading_day": target.isoformat(),
            "instrument_ids": sorted(reasons), "reasons": reasons, "skipped_instruments": skipped}


def advance_forward_after_publication(*, data_dir: Path, snapshot_id: str,
                                     attempt: Path, record: RunRecord, extra: dict,
                                     alerts: Path) -> None:
    """Account tracking is separate from the successful market publication."""
    report = attempt / "forward-observation.json"
    try:
        step = run_step(["tools/run_forward_comparison.py", "--advance", "--data-dir", str(data_dir),
                         "--snapshot-id", snapshot_id, "--json-out", str(report)],
                        label="推进已登记工程前向对照并核对日终账本")
        result = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {"results": []}
        if not isinstance(result.get("results"), list):
            raise ValueError("forward observation output is malformed")
    except Exception as exc:
        step = {"step": "推进已登记工程前向对照并核对日终账本", "ok": False,
                "seconds": 0, "detail": str(exc)}
        result = {"results": [], "error": str(exc)}
    record.steps.append(step)
    extra["forwardComparison"] = {"ok": step["ok"], "report": str(report.resolve()),
                                   "results": result["results"]}
    if not step["ok"]:
        AlertLog(alerts).write(Alert(level=LEVEL_WARNING, source="forward_comparison",
            message="行情已发布；工程前向账本推进失败，原计划和已有账本保留",
            detail={"snapshotId": snapshot_id, "results": result["results"], "step": step}))


def stage_binding(paths: dict[str, Path]) -> dict:
    return {role: {"path": str(path.resolve()), "sha256": file_hash(path)}
            for role, path in paths.items()}


def verify_stage_checkpoint(path: Path, *, stage: str, files: dict[str, Path]) -> bool:
    if not path.exists():
        return False
    saved = json.loads(path.read_text(encoding="utf-8"))
    if (saved.get("schema") != "aquant.daily_stage_checkpoint.v1"
            or saved.get("stage") != stage or saved.get("files") != stage_binding(files)):
        raise ValueError(f"successful {stage} stage inputs changed; checkpoint reuse refused")
    return True


def refresh_daily_market_caps(*, attempt: Path, pool: Path, target: date,
                              archive_root: Path, alerts: Path,
                              record: RunRecord, extra: dict) -> Path:
    """Run Luna's same-close capture; missing optional inputs do not stop S1."""
    from aquant.operations.decision_market_caps import load_sidecar
    sidecar = attempt / "decision-market-caps.json"
    coverage = attempt / "decision-market-caps.coverage.json"
    calendar = attempt / "market-cap-calendar.json"
    manifest = attempt / "market-cap-manifest.json"
    missing = attempt / "no-verified-market-cap-sidecar.json"
    files = {"pool": pool}
    if manifest.exists():
        try:
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            if saved.get("admitted"):
                files.update(sidecar=sidecar, coverage=coverage, calendar=calendar)
            verify_stage_checkpoint(manifest, stage="MARKET_CAPS", files=files)
            result = saved["result"]
            if saved.get("admitted"):
                load_sidecar(sidecar, expected_as_of=target.isoformat(), archive_root=archive_root)
            extra["marketCapRefresh"] = result
            record.steps.append({"step": "恢复已核验当日市值阶段", "ok": True,
                                 "seconds": 0, "reusedVerifiedCheckpoint": True})
            return sidecar if saved.get("admitted") else missing
        except Exception as exc:
            result = {"admitted": False, "tradingDay": target.isoformat(),
                      "reason": "market-cap checkpoint could not be verified: " + str(exc)}
            extra["marketCapRefresh"] = result
            record.steps.append({"step": "恢复已核验当日市值阶段", "ok": False,
                                 "seconds": 0, "detail": result["reason"]})
            AlertLog(alerts).write(Alert(level=LEVEL_WARNING, source="daily_market_caps",
                message="市值检查点未通过；保留原件并继续行情与M/E/B模拟", detail=result))
            return missing
    result = {"admitted": False, "tradingDay": target.isoformat()}
    try:
        import yaml
        ids = [row["instrument_id"] for row in
               yaml.safe_load(pool.read_text(encoding="utf-8"))["instruments"]]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("market-cap pool must contain unique explicit instrument IDs")
        result["poolCount"] = len(ids)
        extra["providerSubprocessesStarted"] += 1
        step = run_step(["tools/capture_daily_decision_market_caps.py", "--pool", str(pool),
                         "--day", target.isoformat(), "--output", str(sidecar),
                         "--coverage-output", str(coverage), "--calendar", str(calendar),
                         "--archive-root", str(archive_root)],
                        label="Luna采集工具更新同日收盘总市值")
        record.steps.append(step)
        if not step["ok"]:
            raise ValueError("same-close market-cap capture did not complete")
        packet = json.loads(coverage.read_text(encoding="utf-8"))
        cap = load_sidecar(sidecar, expected_as_of=target.isoformat(), archive_root=archive_root)
        if (packet.get("market_day") != target.isoformat()
                or packet.get("status") not in {"complete", "reused"}
                or not packet.get("provider_report", {}).get("published")
                or packet.get("pool_sha256") != file_hash(pool)
                or packet.get("requested_count") != len(ids)
                or set(cap.items) - set(ids)):
            raise ValueError("market-cap capture differs from this exact simulation pool/day")
        missing_ids = sorted(set(ids) - set(cap.items))
        result.update(admitted=True, matchedCount=len(cap.items), missingInstrumentIds=missing_ids,
                      receiptId=cap.receipt_id, contentHash=cap.content_hash,
                      observedAt=cap.observed_at, sidecar=str(sidecar.resolve()))
        files.update(sidecar=sidecar, coverage=coverage, calendar=calendar)
    except Exception as exc:
        result["reason"] = str(exc)
        AlertLog(alerts).write(Alert(level=LEVEL_WARNING, source="daily_market_caps",
            message="同日市值未准入；保留具体缺项并继续行情与M/E/B模拟",
            detail=result))
    extra["marketCapRefresh"] = result
    write_json(manifest, {"schema": "aquant.daily_stage_checkpoint.v1", "stage": "MARKET_CAPS",
        "files": stage_binding(files), "admitted": result["admitted"], "result": result,
        "completed_at": datetime.now(timezone.utc).isoformat()})
    return sidecar if result["admitted"] else missing


def report_daily_formal_s2(*, data_dir: Path, snapshot_id: str, facts_db: Path | None,
                           archive_root: Path, attempt: Path, record: RunRecord,
                           extra: dict, alerts: Path, target_period: date = date(2026, 6, 30),
                           target_period_map: Path | None = None) -> None:
    """Read formal formula inputs from the newly published immutable snapshot."""
    if facts_db is None:
        extra["formalS2"] = {"ok": False, "scopeStatus": "CLOSED", "reason": "formal facts DB not configured"}
        return
    output = attempt / "formal-s2-readiness.json"
    try:
        command = ["tools/report_s2_readiness.py", "--snapshot-dir", str(data_dir),
                         "--snapshot-id", snapshot_id, "--facts-db", str(facts_db),
                         "--market-caps-archive-root", str(archive_root),
                         "--latest-period-end", target_period.isoformat(),
                         "--include-signals", "--json-out", str(output)]
        if target_period_map is not None:
            # Freeze the exact metadata version consumed by this observation.
            from aquant.operations.s2_readiness import load_s2_target_period_map
            frozen_map = attempt / "s2-target-period-map.json"
            if frozen_map.exists():
                load_s2_target_period_map(frozen_map)
            else:
                raw_map = load_s2_target_period_map(target_period_map)
                write_json(frozen_map, {k: v for k, v in raw_map.items() if k not in ("path", "content_hash")})
            command += ["--target-period-map", str(frozen_map)]
        step = run_step(command,
                        label="离线核对正式S2四因子、市值与计算分母")
        record.steps.append(step)
        if not step["ok"]:
            raise ValueError("formal S2 input report failed")
        report = json.loads(output.read_text(encoding="utf-8"))
        if report.get("snapshot_id") != snapshot_id:
            raise ValueError("formal S2 report has a different snapshot")
        extra["formalS2"] = {"ok": True, "report": str(output.resolve()),
            "scopeStatus": report.get("scope_status", "CLOSED"),
            "strategyFamilyStatus": report.get("strategy_family_status", "CLOSED"),
            "poolCount": report.get("pool_size"),
            "completeSecurities": report.get("complete_s2_count"),
            "rankingPoolSize": report.get("ranking_pool_size", 0)}
    except Exception as exc:
        extra["formalS2"] = {"ok": False, "report": str(output.resolve()),
                             "scopeStatus": "CLOSED", "reason": str(exc)}
        AlertLog(alerts).write(Alert(level=LEVEL_WARNING, source="daily_formal_s2",
            message="行情与前向账本已保留；正式S2输入检查未完成", detail=extra["formalS2"]))


def _restore_current(data_dir: Path, saved: dict) -> None:
    """Restore the exact pointer pair if promotion failed after a partial write."""
    con = connect(data_dir / "meta.sqlite")
    try:
        with write_tx(con):
            con.execute("DELETE FROM snapshot_pointer WHERE pointer_name='current'")
            if saved["database"] is not None:
                con.execute("INSERT INTO snapshot_pointer(pointer_name,snapshot_id,updated_at) VALUES ('current',?,?)",
                            tuple(saved["database"]))
    finally:
        con.close()
    path = data_dir / "current_snapshot.json"
    if saved["file"] is None:
        path.unlink(missing_ok=True)
    else:
        pending = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            pending.write_bytes(saved["file"].encode("utf-8"))
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)


def recover_publication(attempt: Path, *, research: Path, data_dir: Path) -> str | None:
    """Recover a commit interrupted between its two atomic replacements."""
    journal = attempt / "publication-commit.json"
    if not journal.exists():
        return None
    saved = json.loads(journal.read_text(encoding="utf-8"))
    if saved["state"] == "COMMITTED":
        return saved["snapshotId"]
    if saved["state"] != "PREPARED":
        return None
    backup = attempt / "previous-research.json"
    if file_hash(backup) != saved["previousResearchHash"]:
        raise ValueError("interrupted publication backup changed; automatic recovery refused")
    if file_hash(research) not in {saved["previousResearchHash"], saved["stagedResearchHash"]}:
        raise ValueError("research changed outside interrupted publication; automatic recovery refused")
    pointer = data_dir / "current_snapshot.json"
    if pointer.exists():
        current_id = json.loads(pointer.read_text(encoding="utf-8"))["snapshot_id"]
        previous = saved["previousCurrent"]["database"]
        allowed = {saved["snapshotId"], previous[0] if previous else None}
        if current_id not in allowed:
            raise ValueError("current changed outside interrupted publication; automatic recovery refused")
    pending = research.with_name(f".{research.name}.{os.getpid()}.tmp")
    try:
        pending.write_bytes(backup.read_bytes())
        os.replace(pending, research)
    finally:
        pending.unlink(missing_ok=True)
    _restore_current(data_dir, saved["previousCurrent"])
    write_json(journal, dict(saved, state="ROLLED_BACK",
                             reason="recovered an interrupted publication before retry"))
    return None


def publish_pair(*, staged_research: Path, research: Path, data_dir: Path,
                 snapshot_id: str, attempt: Path) -> None:
    """Replace each public file atomically; restore research on promotion failure.

    The journal keeps the previous research bytes and the intended snapshot.
    Both public objects have already passed their own admission before this
    short commit window. There is no claim of a filesystem-wide transaction.
    """
    previous = research.read_bytes()
    pointer = data_dir / "current_snapshot.json"
    con = connect(data_dir / "meta.sqlite")
    try:
        row = con.execute("SELECT snapshot_id,updated_at FROM snapshot_pointer WHERE pointer_name='current'").fetchone()
        saved_current = {"database": list(row) if row is not None else None,
                         "file": pointer.read_text(encoding="utf-8") if pointer.exists() else None}
    finally:
        con.close()
    backup = attempt / "previous-research.json"
    backup.write_bytes(previous)
    journal = attempt / "publication-commit.json"
    write_json(journal, {"state": "PREPARED", "snapshotId": snapshot_id,
                        "previousCurrent": saved_current,
                        "previousResearchHash": file_hash(backup),
                        "stagedResearchHash": file_hash(staged_research)})
    pending = research.with_name(f".{research.name}.{os.getpid()}.tmp")
    try:
        pending.write_bytes(staged_research.read_bytes())
        os.replace(pending, research)
        promote_universe_snapshot(data_root=data_dir, snapshot_id=snapshot_id)
    except BaseException:
        pending.write_bytes(previous)
        os.replace(pending, research)
        _restore_current(data_dir, saved_current)
        write_json(journal, {"state": "ROLLED_BACK", "snapshotId": snapshot_id,
                            "reason": "snapshot promotion failed; previous research restored"})
        raise
    finally:
        pending.unlink(missing_ok=True)
    write_json(journal, {"state": "COMMITTED", "snapshotId": snapshot_id,
                        "researchHash": file_hash(research)})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trading-day")
    ap.add_argument("--skip-if-done", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--window-start", default="2026-06-22", help="legacy CLI compatibility; simulation uses verified rolling 120-day inputs")
    ap.add_argument("--data-dir", type=Path, default=Path(os.environ.get("AQUANT_DATA_DIR", str(ROOT / "deploy/universe-snapshot"))))
    ap.add_argument("--source", choices=("manual", "scheduler"), default="manual")
    ap.add_argument("--snapshot-id")
    ap.add_argument("--json-out", type=Path)
    ap.add_argument("--research-file", type=Path, default=DEFAULT_RESEARCH)
    ap.add_argument("--calendar-evidence", type=Path, default=Path(os.environ.get("AQUANT_CALENDAR_EVIDENCE", str(DEFAULT_CALENDAR))))
    ap.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    ap.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE)
    ap.add_argument("--simulation-limit", type=int, default=120)
    ap.add_argument("--financials", type=Path, default=os.environ.get("AQUANT_SIMULATION_FINANCIAL_CACHE"),
                    help="已归档的可选利润缓存；配合新收据时离线准入，不改原发布")
    ap.add_argument("--financial-evidence", type=Path,
                    default=os.environ.get("AQUANT_SIMULATION_FINANCIAL_EVIDENCE"),
                    help="Luna实际财务采集收据；与--financials精确绑定")
    ap.add_argument("--s2-facts-db", type=Path, default=os.environ.get("AQUANT_LIVE_FINANCIAL_FACTS_DB"),
                    help="正式LIVE原报事实仓；仅对本次发布快照输出四因子及缺项报告")
    ap.add_argument("--s2-latest-period-end", type=date.fromisoformat,
                    default=os.environ.get("AQUANT_S2_TARGET_PERIOD_END", "2026-06-30"),
                    help="本批公式需求截止期；按真实最新报告索引更新，不从失败样本缩小需求")
    ap.add_argument("--s2-target-period-map", type=Path,
                    default=os.environ.get("AQUANT_S2_TARGET_PERIOD_MAP"),
                    help="逐证券实际索引目标与待发现状态；本次日更离线冻结元数据版本")
    ap.add_argument("--run-log", type=Path, default=DEFAULT_RUNS)
    ap.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    ap.add_argument("--alerts", type=Path, default=DEFAULT_ALERTS)
    args = ap.parse_args(argv)
    if args.financial_evidence and not args.financials:
        ap.error("--financial-evidence requires its exact --financials cache")
    now = market_now()
    target = date.fromisoformat(args.trading_day) if args.trading_day else now.date()
    day = target.isoformat()
    started = time.monotonic()
    record = RunRecord(trading_day=day, snapshot_id=args.snapshot_id or "pending",
                       outcome="FAILED", source=args.source,
                       started_at=datetime.now(timezone.utc).isoformat())
    run_log = RunLog(args.run_log)
    extra = {"mode": "RESEARCH_SIMULATION", "providerSubprocessesStarted": 0}

    def finish(code: int) -> int:
        record.finished_at = datetime.now(timezone.utc).isoformat()
        record.duration_seconds = round(time.monotonic() - started, 1)
        payload = dict(record.as_dict(), **extra)
        args.run_log.parent.mkdir(parents=True, exist_ok=True)
        with args.run_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        if args.json_out:
            write_json(args.json_out, payload)
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        return code

    try:
        if args.simulation_limit <= 0:
            raise ValueError("simulation limit must be positive")
        gate = verified_calendar_day(args.calendar_evidence, target, now=now)
        extra["calendarGate"] = gate
        record.steps.append({"step": "核对已归档年度交易日历", "ok": True, "seconds": 0})
        if not gate["isTradingDay"]:
            record.outcome, record.reason = "SKIPPED", "已验证交易日历标记休市；未启动任何数据源采集"
            return finish(0)
        if args.skip_if_done and any(e.get("outcome") == "PUBLISHED" and e.get("trading_day") == day
                                     and e.get("mode") == "RESEARCH_SIMULATION" for e in run_log.entries(200)):
            record.outcome, record.reason = "SKIPPED", "该日研究与模拟快照已经联合发布；保留不可变来源"
            return finish(0)
        if args.dry_run:
            record.outcome, record.reason = "SKIPPED", "DRY_RUN：全范围行情刷新、候选重算、限定模拟池证据、质量检查、联合发布；未启动采集"
            return finish(0)
        if target > now.date() or (target == now.date() and now.hour < 15):
            raise ValueError("requested market close has not occurred; no provider capture started")
        with PipelineLock(args.lock):
            day_root = args.artifact_root / day.replace("-", "")
            state_path = day_root / "attempt.json"
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            if state.get("state") == "IN_PROGRESS":
                attempt = Path(state["path"])
            else:
                attempt = day_root / ("attempt-" + uuid.uuid4().hex[:12])
                attempt.mkdir(parents=True, exist_ok=True)
                write_json(state_path, {"state": "IN_PROGRESS", "path": str(attempt.resolve())})
            extra["artifactDirectory"] = str(attempt.resolve())
            committed = recover_publication(attempt, research=args.research_file, data_dir=args.data_dir)
            if committed is not None:
                write_json(state_path, {"state": "COMMITTED", "path": str(attempt.resolve()),
                                        "snapshotId": committed})
                record.snapshot_id, record.outcome, record.data_last_day = committed, "PUBLISHED", day
                record.reason = "恢复已完成的联合发布记录；未重复采集或覆盖来源"
                advance_forward_after_publication(data_dir=args.data_dir, snapshot_id=committed,
                    attempt=attempt, record=record, extra=extra, alerts=args.alerts)
                report_daily_formal_s2(data_dir=args.data_dir, snapshot_id=committed,
                    facts_db=args.s2_facts_db, archive_root=args.archive_root, attempt=attempt,
                    record=record, extra=extra, alerts=args.alerts, target_period=args.s2_latest_period_end,
                    target_period_map=args.s2_target_period_map)
                return finish(0)
            retained = retained_instruments(args.data_dir, target=target)
            retained_path = attempt / "retained-instruments.json"
            if retained_path.exists():
                prior_retained = json.loads(retained_path.read_text(encoding="utf-8"))
                if prior_retained.get("trading_day") != day:
                    raise ValueError("retained instruments checkpoint has a different market day")
                if not set(retained["instrument_ids"]) <= set(prior_retained["instrument_ids"]):
                    # A new saved selection needs a new source binding. Keep the
                    # old partial archive intact rather than mutating its pool.
                    attempt = day_root / ("attempt-" + uuid.uuid4().hex[:12])
                    attempt.mkdir(parents=True, exist_ok=True)
                    write_json(state_path, {"state": "IN_PROGRESS", "path": str(attempt.resolve())})
                    extra["artifactDirectory"] = str(attempt.resolve())
                    retained_path = attempt / "retained-instruments.json"
            if not retained_path.exists():
                write_json(retained_path, retained)
            extra["retainedInstrumentCount"] = len(json.loads(retained_path.read_text(encoding="utf-8"))["instrument_ids"])
            staged = attempt / "research-candidates.json"
            ranking = attempt / "research-ranking.json"
            baseline = attempt / "baseline-research.json"
            if not baseline.exists():
                baseline.write_bytes(args.research_file.read_bytes())
            quotes, evidence = attempt / "closing-quotes.json", attempt / "simulation-evidence"
            plan = [(["tools/capture_research_quotes.py", "--close-date", day,
                      "--archive-root", str(args.archive_root), "--output", str(quotes),
                      "--checkpoint", str(attempt / "quote-checkpoint.jsonl"),
                      "--coverage", str(attempt / "quote-coverage.json")], "Luna采集工具更新全范围收盘后行情", True),
                    (["tools/refresh_research_publication.py", "--baseline", str(baseline),
                      "--quotes", str(quotes), "--archive-root", str(args.archive_root),
                      "--output", str(ranking), "--trading-day", day], "离线重算全范围研究候选", False),
                    (["tools/capture_research_simulation_evidence.py", "--day", day,
                      "--research", str(staged), "--output-dir", str(evidence),
                      "--limit", str(args.simulation_limit), "--retain-instruments", str(retained_path),
                      "--resume"], "Luna采集工具更新限定模拟池证据", True)]
            for index, (command, label, provider) in enumerate(plan):
                manifest = attempt / ("quote-capture-manifest.json" if index == 0 else "refresh-manifest.json")
                stage = "QUOTES" if index == 0 else "RESEARCH_REFRESH"
                files = {"baseline": baseline, "quotes": quotes,
                         "coverage": attempt / "quote-coverage.json"}
                if index == 1:
                    files["research"] = ranking
                reused = (verify_stage_checkpoint(manifest, stage=stage, files=files) if index < 2 else
                          reuse_complete_simulation_evidence(evidence / "summary.json", research=staged,
                              day=day, limit=args.simulation_limit, retained=retained_path))
                if reused:
                    step = {"step": label, "ok": True, "seconds": 0, "reusedVerifiedCheckpoint": True}
                else:
                    if provider:
                        extra["providerSubprocessesStarted"] += 1
                    step = run_step(command, label=label)
                record.steps.append(step)
                if not step["ok"]:
                    raise RuntimeError(label + "失败；保留旧的研究发布和 current")
                if index == 0:
                    extra["quoteCoverage"] = verify_quote_capture(
                        attempt / "quote-coverage.json", baseline=baseline, day=day)
                elif index == 1:
                    load_research_scan(ranking, limit=1)
                if index < 2 and not reused:
                    write_json(manifest, {"schema": "aquant.daily_stage_checkpoint.v1", "stage": stage,
                                          "files": stage_binding(files),
                                          "completed_at": datetime.now(timezone.utc).isoformat()})
                if index == 1:
                    refresh_daily_announcements(attempt=attempt, ranking=ranking, staged=staged,
                        retained=json.loads(retained_path.read_text(encoding="utf-8")), target=target,
                        archive_root=args.archive_root, alerts=args.alerts, record=record, extra=extra)
            summary = evidence / "summary.json"
            cache, pool = publication_files(summary, research=staged, day=day)
            market_caps = refresh_daily_market_caps(attempt=attempt, pool=pool, target=target,
                archive_root=args.archive_root, alerts=args.alerts, record=record, extra=extra)
            record.snapshot_id = args.snapshot_id or new_snapshot_id(day)
            report = attempt / "snapshot-publish.json"
            financial_args = (["--financials", str(args.financials)] if args.financials else [])
            if args.financial_evidence:
                financial_args += ["--financial-evidence", str(args.financial_evidence)]
                extra["optionalFinancialEvidence"] = str(args.financial_evidence.resolve())
            step = run_step(["tools/publish_universe_snapshot.py", "--simulation-evidence", str(summary),
                            "--cache", str(cache), "--pool", str(pool), "--data-dir", str(args.data_dir),
                            "--snapshot-id", record.snapshot_id, "--window-end", day,
                            "--market-caps", str(market_caps),
                            "--market-caps-archive-root", str(args.archive_root),
                            "--allow-degraded", "--defer-promotion", "--json-out", str(report), *financial_args],
                           label="离线准入并发布待提升模拟快照")
            record.steps.append(step)
            if not step["ok"]:
                raise RuntimeError("模拟快照发布失败；保留旧的研究发布和 current")
            from daily_run import _read_s1_capability, _read_snapshot_quality
            quality = _read_snapshot_quality(report, snapshot_id=record.snapshot_id)
            capability = _read_s1_capability(args.data_dir, snapshot_id=record.snapshot_id)
            extra["snapshotQuality"] = {"coreOk": quality.core_ok, "s1Available": capability["ok"],
                                        "financialDegraded": quality.degraded,
                                        "financialFailures": list(quality.financial_failures)}
            if not quality.core_ok or not capability["ok"]:
                raise RuntimeError("模拟核心质量不通过：" + json.dumps([*quality.core_failures, capability], ensure_ascii=False))
            publish_pair(staged_research=staged, research=args.research_file, data_dir=args.data_dir,
                         snapshot_id=record.snapshot_id, attempt=attempt)
            write_json(state_path, {"state": "COMMITTED", "path": str(attempt.resolve()),
                                    "snapshotId": record.snapshot_id})
            record.outcome, record.data_last_day = "PUBLISHED", day
            record.reason = "全范围研究刷新已发布；模拟为已核验的主板限定池，财务及行业来源日期保留"
            extra["simulationLimit"] = args.simulation_limit
            extra["simulationEvidence"] = str(summary.resolve())
            advance_forward_after_publication(data_dir=args.data_dir, snapshot_id=record.snapshot_id,
                attempt=attempt, record=record, extra=extra, alerts=args.alerts)
            report_daily_formal_s2(data_dir=args.data_dir, snapshot_id=record.snapshot_id,
                facts_db=args.s2_facts_db, archive_root=args.archive_root, attempt=attempt,
                record=record, extra=extra, alerts=args.alerts, target_period=args.s2_latest_period_end,
                target_period_map=args.s2_target_period_map)
            return finish(0)
    except PipelineBusy as exc:
        record.outcome, record.reason = "SKIPPED", str(exc)
        return finish(0)
    except Exception as exc:
        record.reason = str(exc)
        AlertLog(args.alerts).write(Alert(level=LEVEL_ERROR, source="daily_research_run",
                                        message="每日研究/模拟更新失败；保留旧发布",
                                        detail={"tradingDay": day, "reason": record.reason}))
        return finish(1)


if __name__ == "__main__":
    raise SystemExit(main())
