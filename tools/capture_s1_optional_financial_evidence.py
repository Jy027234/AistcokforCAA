"""Capture bounded BaoStock S1 profit rows for an explicit missing-target list.

The output is a dated optional-financial cache plus raw BaoStock response
artifacts and SQL receipts. It does not write a published snapshot or the
legacy financial cache.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.baostock import BaostockClient, BaostockUnavailable  # noqa: E402
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
from capture_research_simulation_evidence import login_with_single_retry, receipt_summary  # noqa: E402

SCHEMA = "aquant.simulation-financial-evidence.v1"
EXPECTED_PERIODS = {"2025Q2": (2025, 2), "2025Q4": (2025, 4), "2026Q2": (2026, 2)}
REQUIRED_ROW_FIELDS = ("code", "netProfit", "statDate", "pubDate")
_IID = re.compile(r"(SH|SZ)\.(\d{6})\Z")


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def load_targets(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    demand = json.loads(path.read_text(encoding="utf-8"))
    if demand.get("schema") != "aquant.financial-optional-demand.v1":
        raise ValueError("unsupported financial optional demand schema")
    records = demand.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("demand records must be a non-empty list")
    seen: set[tuple[str, str]] = set()
    for item in records:
        iid = item.get("instrumentId")
        match = _IID.fullmatch(str(iid or ""))
        period_key = str(item.get("periodKey") or "")
        if not match or period_key not in EXPECTED_PERIODS:
            raise ValueError(f"invalid target identity or period: {iid}/{period_key}")
        expected_year, expected_quarter = EXPECTED_PERIODS[period_key]
        if item.get("year") != expected_year or item.get("quarter") != expected_quarter:
            raise ValueError(f"period fields do not match {period_key}: {iid}")
        provider_code = ("sh." if match.group(1) == "SH" else "sz.") + match.group(2)
        if item.get("providerCode") != provider_code:
            raise ValueError(f"provider code does not match instrument id: {iid}")
        key = iid, period_key
        if key in seen:
            raise ValueError(f"duplicate target: {iid} {period_key}")
        seen.add(key)
    return demand, records


def expected_stat_date(year: int, quarter: int) -> str:
    return {1: f"{year}-03-31", 2: f"{year}-06-30",
            3: f"{year}-09-30", 4: f"{year}-12-31"}[quarter]


def classify_profit_row(instrument_id: str, period_key: str, row: dict[str, Any] | None,
                        observed_date: date) -> tuple[str, str | None]:
    if row is None:
        return "NO_RECORD", "BaoStock returned no profit row for this security-period"
    missing = [field for field in REQUIRED_ROW_FIELDS if row.get(field) in (None, "")]
    if missing:
        return "FAILED", "profit row missing required fields: " + ", ".join(missing)
    market, code = instrument_id.split(".", 1)
    provider_code = ("sh." if market == "SH" else "sz.") + code
    if row.get("code") != provider_code:
        return "FAILED", f"provider identity mismatch: expected {provider_code}, got {row.get('code')}"
    year, quarter = EXPECTED_PERIODS[period_key]
    stat_date = expected_stat_date(year, quarter)
    if row.get("statDate") != stat_date:
        return "FAILED", f"statDate mismatch: expected {stat_date}, got {row.get('statDate')}"
    try:
        publication_date = date.fromisoformat(str(row["pubDate"]))
    except ValueError:
        return "FAILED", f"invalid pubDate: {row.get('pubDate')}"
    if publication_date > observed_date:
        return "FAILED", f"pubDate is after actual capture date {observed_date.isoformat()}"
    try:
        float(row["netProfit"])
    except (TypeError, ValueError):
        return "FAILED", f"netProfit is not numeric: {row.get('netProfit')}"
    return "COLLECTED", None


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def _load_checkpoint(path: Path, binding: dict[str, Any], *, resume: bool) -> dict[tuple[str, str], dict[str, Any]]:
    if not resume or not path.is_file():
        return {}
    saved = json.loads(path.read_text(encoding="utf-8"))
    if saved.get("captureBinding") != binding:
        raise ValueError("resume checkpoint is bound to a different demand or base cache")
    return {(row["instrumentId"], row["periodKey"]): row for row in saved.get("records", [])}


def _next_attempt_path(path: Path) -> Path:
    if not path.exists():
        return path
    index = 2
    while True:
        candidate = path.with_name(f"{path.stem}-resume-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def capture(*, demand_path: Path, base_cache_path: Path, output_cache_path: Path,
            output_evidence_path: Path, archive_root: Path, archive_db_path: Path,
            checkpoint_path: Path, login_attempts_path: Path, resume: bool = False,
            min_interval_seconds: float = 0.25) -> dict[str, Any]:
    if output_cache_path.exists() or output_evidence_path.exists():
        raise FileExistsError("dated output already exists; refusing to overwrite evidence")
    if checkpoint_path.exists() and not resume:
        raise FileExistsError("checkpoint already exists; use --resume to preserve and continue it")
    demand, targets = load_targets(demand_path)
    demand_hash = sha256_file(demand_path)
    base_hash = sha256_file(base_cache_path)
    base = json.loads(base_cache_path.read_text(encoding="utf-8"))
    if not isinstance(base.get("statements"), dict):
        raise ValueError("base financial cache has no statements mapping")
    binding = {"demandPath": str(demand_path.resolve()), "demandHash": demand_hash,
               "baseCachePath": str(base_cache_path.resolve()), "baseCacheHash": base_hash,
               "scopeInstrumentIds": sorted({item["instrumentId"] for item in targets}),
               "periods": sorted({item["periodKey"] for item in targets})}
    records = _load_checkpoint(checkpoint_path, binding, resume=resume)
    if resume:
        login_attempts_path = _next_attempt_path(login_attempts_path)
    started = datetime.now(timezone.utc)
    archive_root.mkdir(parents=True, exist_ok=True)
    archive_db_path.parent.mkdir(parents=True, exist_ok=True)
    raw_root = archive_root / "baostock-raw"
    with connect(archive_db_path) as con:
        archive = ForwardArchive(con, archive_root / "raw")
        client = BaostockClient(raw_root, min_interval_seconds=min_interval_seconds)
        login_with_single_retry(client, login_attempts_path)
        stop_reason: str | None = None
        try:
            for target in targets:
                key = (target["instrumentId"], target["periodKey"])
                if key in records:
                    continue
                iid, period_key = key
                year, quarter = target["year"], target["quarter"]
                existing = base["statements"].get(iid, {}).get(period_key)
                if existing:
                    records[key] = {"instrumentId": iid, "periodKey": period_key,
                                    "status": "FAILED", "profitRow": None, "receipt": None,
                                    "reason": "base cache already contains this period; refusing to overwrite its row"}
                elif stop_reason:
                    records[key] = {"instrumentId": iid, "periodKey": period_key,
                                    "status": "FAILED", "profitRow": None, "receipt": None,
                                    "reason": "capture stopped after prior provider failure: " + stop_reason}
                else:
                    code = target["providerCode"]
                    try:
                        row, raw_receipt = client.profit(code, year=year, quarter=quarter)
                        url = (f"baostock://www.baostock.com:10030/query_profit_data?"
                               f"code={code}&year={year}&quarter={quarter}")
                        receipt = receipt_summary(client, raw_receipt, url, archive)
                        status, reason = classify_profit_row(
                            iid, period_key, row,
                            datetime.now(timezone(timedelta(hours=8))).date())
                        receipt["summary"] = raw_receipt.as_dict()
                        receipt["detail"] = f"{raw_receipt.label}; raw={raw_receipt.stored_path}"
                        records[key] = {"instrumentId": iid, "periodKey": period_key,
                                        "status": status, "profitRow": row,
                                        "receipt": receipt, "reason": reason}
                    except BaostockUnavailable as exc:
                        stop_reason = f"{type(exc).__name__}: {str(exc)[:240]}"
                        records[key] = {"instrumentId": iid, "periodKey": period_key,
                                        "status": "FAILED", "profitRow": None, "receipt": None,
                                        "reason": stop_reason}
                    except Exception as exc:  # one malformed response must not lose later records
                        records[key] = {"instrumentId": iid, "periodKey": period_key,
                                        "status": "FAILED", "profitRow": None, "receipt": None,
                                        "reason": f"{type(exc).__name__}: {str(exc)[:240]}"}
                _atomic_json(checkpoint_path, {"schema": SCHEMA + ".checkpoint",
                                               "captureBinding": binding,
                                               "records": list(records.values())})
        finally:
            client.logout()

    finished = datetime.now(timezone.utc)
    observed_date = finished.astimezone(timezone(timedelta(hours=8))).date()
    output = copy.deepcopy(base)
    statements = output.setdefault("statements", {})
    failed = output.setdefault("failed", {})
    output.setdefault("created_at", base.get("created_at") or started.isoformat())
    periods = set(output.get("periods") or [])
    periods.update(binding["periods"])
    output["periods"] = sorted(periods)
    for target in targets:
        item = records[(target["instrumentId"], target["periodKey"])]
        iid, period_key = target["instrumentId"], target["periodKey"]
        if item["status"] == "COLLECTED":
            statements.setdefault(iid, {})[period_key] = copy.deepcopy(item["profitRow"])
            if iid in failed and isinstance(failed[iid], dict):
                failed[iid].pop(period_key, None)
                if not failed[iid]:
                    failed.pop(iid)
        elif item["status"] != "NO_RECORD":
            failed.setdefault(iid, {})[period_key] = item["reason"]
    output["updated_at"] = finished.isoformat()
    _atomic_json(output_cache_path, output)
    receipt_times = [row["receipt"].get("requested_at") for row in records.values()
                     if isinstance(row.get("receipt"), dict)
                     and row["receipt"].get("requested_at")]
    captured_from = min(receipt_times) if receipt_times else started.isoformat()
    evidence = {
        "schema": SCHEMA,
        "cachePath": str(output_cache_path.resolve()),
        "cacheHash": sha256_file(output_cache_path),
        "baseCachePath": str(base_cache_path.resolve()),
        "baseCacheHash": base_hash,
        "demandPath": str(demand_path.resolve()),
        "demandHash": demand_hash,
        "archiveRoot": str((archive_root / "raw").resolve()),
        "archiveDbPath": str(archive_db_path.resolve()),
        "rawBaoStockRoot": str(raw_root.resolve()),
        "capturedFrom": captured_from,
        "captureResumedAt": started.isoformat() if resume else None,
        "capturedTo": finished.isoformat(),
        "observedDate": observed_date.isoformat(),
        "scopeInstrumentIds": binding["scopeInstrumentIds"],
        "periods": binding["periods"],
        "targets": targets,
        "records": [records[(x["instrumentId"], x["periodKey"])] for x in targets],
        "counts": {status: sum(1 for row in records.values() if row["status"] == status)
                   for status in ("COLLECTED", "NO_RECORD", "FAILED")},
        "loginAttemptsPath": str(login_attempts_path.resolve()),
        "loginAttemptsPaths": [str(path.resolve()) for path in sorted(
            login_attempts_path.parent.glob("login-attempts*.json"))],
        "captureStoppedReason": stop_reason,
        "boundary": "Optional BaoStock S1 candidate financial data with current observation receipts. It is not CNINFO official PDF evidence, not an S2 formal fact, and cannot be used before pubDate/first_seen under PIT admission.",
    }
    _atomic_json(output_evidence_path, evidence)
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demand", type=Path, default=ROOT / "data/s2-structured/financial-optional-missing-20261013.json")
    parser.add_argument("--base-cache", type=Path, default=ROOT / "deploy/agentctl-q0/financials-cache.json")
    parser.add_argument("--output-cache", type=Path, default=ROOT / "data/s2-structured/financials-cache-20261013.json")
    parser.add_argument("--output-evidence", type=Path, default=ROOT / "data/s2-structured/financial-evidence-20261013.json")
    parser.add_argument("--archive-root", type=Path, default=ROOT / "data/s2-structured/financial-evidence-20261013/archive")
    parser.add_argument("--archive-db", type=Path, default=ROOT / "data/s2-structured/financial-evidence-20261013/archive.sqlite")
    parser.add_argument("--checkpoint", type=Path, default=ROOT / "data/s2-structured/financial-evidence-20261013/checkpoint.json")
    parser.add_argument("--login-attempts", type=Path, default=ROOT / "data/s2-structured/financial-evidence-20261013/login-attempts.json")
    parser.add_argument("--min-interval-seconds", type=float, default=0.25)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    evidence = capture(demand_path=args.demand.resolve(), base_cache_path=args.base_cache.resolve(),
                       output_cache_path=args.output_cache.resolve(),
                       output_evidence_path=args.output_evidence.resolve(),
                       archive_root=args.archive_root.resolve(), archive_db_path=args.archive_db.resolve(),
                       checkpoint_path=args.checkpoint.resolve(), login_attempts_path=args.login_attempts.resolve(),
                       resume=args.resume, min_interval_seconds=args.min_interval_seconds)
    print(f"{args.output_evidence.resolve()}: {evidence['counts']} cache={evidence['cacheHash']}")
    return 0 if not evidence["counts"]["FAILED"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
