"""Capture exact missing CNINFO report-index retries from the existing bound set.

The command only calls ``CninfoClient.report_index``. Raw requests and responses
remain owned by the existing ``ForwardArchive``; this file writes resumable
metadata checkpoints for the caller's offline processing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.cninfo import (  # noqa: E402
    CircuitBreaker, CninfoClient, RateLimiter, default_policy,
)
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402

ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"
BOUND_PATH = ROOT / "data" / "s2-structured" / "bound-v2.json"
ORG_MAP_PATH = ROOT / "data" / "s2-structured" / "universe-orgids.json"
PERIOD = "2025-06-30"
RECEIPT_DAY = "2026-10-06"
EXPECTED_MISSING_COUNT = 37
CHECKPOINT_PATH = ROOT / "data" / "s2-structured" / "index-retry-20261007.checkpoint.jsonl"
OUTPUT_PATH = ROOT / "data" / "s2-structured" / "index-retry-20261007.json"


def exact_pairs(bound_path: Path, archive_root: Path) -> list[dict[str, str]]:
    """Return 2025-06-30 incomplete pairs with no index receipt on 2026-10-06."""
    bound = json.loads(bound_path.read_text(encoding="utf-8"))
    records = bound["records"]
    incomplete = [row for row in records
                  if row.get("status") == "INDEX_INCOMPLETE"
                  and str(row.get("period_end", ""))[:10] == PERIOD]
    with closing(connect(archive_root / "meta.sqlite", read_only=True)) as con:
        receipt_rows = con.execute(
            "SELECT detail FROM fetch_receipt WHERE source_id='cninfo' "
            "AND outcome='OK' AND http_status=200 AND content_hash IS NOT NULL "
            "AND first_seen_at >= ? AND first_seen_at < ? "
            "AND detail LIKE 'report-index:%'",
            (f"{RECEIPT_DAY}T00:00:00+00:00", "2026-10-07T00:00:00+00:00"),
        ).fetchall()
    captured: set[tuple[str, str]] = set()
    pattern = re.compile(r"^report-index:(\d{6}):(\d{4}-\d{2}-\d{2}):")
    for row in receipt_rows:
        match = pattern.match(row["detail"] or "")
        if match:
            captured.add((match.group(1), match.group(2)))
    result = [
        {"instrument_id": str(row["instrument_id"]).zfill(6), "period_end": PERIOD}
        for row in incomplete
        if (str(row["instrument_id"]).zfill(6), PERIOD) not in captured
    ]
    result.sort(key=lambda pair: pair["instrument_id"])
    return result


def read_checkpoint(path: Path) -> dict[tuple[str, str], dict]:
    entries: dict[tuple[str, str], dict] = {}
    if not path.exists():
        return entries
    with path.open("r", encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            entry = json.loads(line)
            key = (str(entry["instrument_id"]).zfill(6), str(entry["period_end"])[:10])
            entries[key] = entry
    # A checkpoint only closes a pair when it records a complete result. Failed
    # calls and truncated indexes remain eligible for a later attempt.
    return {key: entry for key, entry in entries.items() if entry.get("complete") is True}


def atomic_write_json(path: Path, data: dict) -> None:
    """Atomically replace the summary so interrupted retries can refresh it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as output:
            temp_path = Path(output.name)
            json.dump(data, output, ensure_ascii=False, indent=2, default=str)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    pairs_cmd = subparsers.add_parser("exact-pairs", help="list currently missing exact pairs")
    pairs_cmd.add_argument("--bound", type=Path, default=BOUND_PATH)
    pairs_cmd.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    collect_cmd = subparsers.add_parser("collect", help="capture indexes and append checkpoints")
    collect_cmd.add_argument("--bound", type=Path, default=BOUND_PATH)
    collect_cmd.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    collect_cmd.add_argument("--org-map", type=Path, default=ORG_MAP_PATH)
    collect_cmd.add_argument("--checkpoint", type=Path, default=CHECKPOINT_PATH)
    collect_cmd.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()

    pairs = exact_pairs(args.bound, args.archive_root)
    if args.command == "exact-pairs":
        print(json.dumps(pairs, ensure_ascii=False, indent=2))
        print(f"exact_pairs={len(pairs)}")
        return 0
    if len(pairs) != EXPECTED_MISSING_COUNT:
        raise SystemExit(f"expected {EXPECTED_MISSING_COUNT} missing pairs, found {len(pairs)}")
    org_ids = json.loads(args.org_map.read_text(encoding="utf-8"))["organization_ids"]
    completed = read_checkpoint(args.checkpoint)
    # Only reuse checkpointed keys that are still in the exact current target set.
    target_keys = {(p["instrument_id"], p["period_end"]) for p in pairs}
    completed = {key: value for key, value in completed.items() if key in target_keys}
    started = time.monotonic()
    attempts = 0
    stopped_on_breaker = False
    args.checkpoint.parent.mkdir(parents=True, exist_ok=True)

    with closing(connect(args.archive_root / "meta.sqlite")) as con:
        archive = ForwardArchive(con, args.archive_root)
        client = CninfoClient(
            archive,
            policy=default_policy(),
            limiter=RateLimiter(min_interval_seconds=4.5),
            breaker=CircuitBreaker(failure_threshold=15, cooldown_seconds=120),
        )
        with args.checkpoint.open("a", encoding="utf-8", newline="\n") as checkpoint:
            for index, pair in enumerate(pairs, 1):
                key = (pair["instrument_id"], pair["period_end"])
                if key in completed:
                    continue
                began = datetime.now(timezone.utc)
                attempts += 1
                try:
                    result = client.report_index(
                        stock_code=pair["instrument_id"],
                        report_period=pair["period_end"].replace("-", ""),
                        organization_id=org_ids.get(pair["instrument_id"]),
                    )
                    entry = {
                        **pair,
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                        "started_at": began.isoformat(),
                        "complete": result.complete,
                        "termination": result.termination,
                        "error": result.error,
                        "result": asdict(result),
                    }
                except Exception as exc:  # noqa: BLE001 - checkpoint every attempted pair
                    entry = {
                        **pair,
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                        "started_at": began.isoformat(),
                        "exception": f"{type(exc).__name__}: {exc}",
                    }
                checkpoint.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
                checkpoint.flush()
                completed[key] = entry
                print(f"{index}/{len(pairs)} {key[0]} {key[1]} "
                      f"{entry.get('termination', entry.get('exception', 'captured'))}", flush=True)
                if client.breaker.is_open:
                    stopped_on_breaker = True
                    break

    ordered = [completed[(p["instrument_id"], p["period_end"])]
               for p in pairs if (p["instrument_id"], p["period_end"]) in completed]
    summary = {
        "schema": "aquant.s2-index-retry-metadata.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "requested_pairs": pairs,
        # successful_count counts calls returning a result, including an
        # incomplete pagination outcome; completed_count counts complete=true.
        "successful_count": sum("result" in item for item in ordered),
        "attempted_count": attempts,
        "completed_count": sum(item.get("complete") is True for item in ordered),
        "remaining_count": len(pairs) - sum(item.get("complete") is True for item in ordered),
        "stopped_on_breaker": stopped_on_breaker,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "checkpoint": str(args.checkpoint),
        "results": ordered,
    }
    atomic_write_json(args.output, summary)
    print(json.dumps({key: summary[key] for key in (
        "successful_count", "attempted_count", "completed_count", "remaining_count",
        "stopped_on_breaker", "elapsed_seconds",
        "checkpoint",
    )}, ensure_ascii=False))
    return 0 if not stopped_on_breaker and summary["completed_count"] == len(pairs) else 2


if __name__ == "__main__":
    raise SystemExit(main())
