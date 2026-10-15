"""Capture a bounded, resumable Tencent unadjusted daily-bar snapshot for research.

This sidecar is independent of the deployed universe cache and never changes
research publication or simulation inputs. Tencent's daily endpoint omits
turnover amount; amount_cents is therefore explicitly null.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.resilience import RateLimiter, RetryPolicy  # noqa: E402
from aquant.adapters.providers.tencent import TencentClient  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402

INPUT = ROOT / "data/s2-structured/research-candidates.json"
BASE = ROOT / "data/s2-structured/research-daily-checkpoint-20261009"
OUTPUT = ROOT / "data/s2-structured/research-history-20261009.json"
START = "2026-06-22"
END = "2026-10-09"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def cents(value: object) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_bars(raw: list) -> list[dict]:
    bars = []
    for item in raw:
        if not isinstance(item, list) or len(item) < 6:
            continue
        try:
            day = date.fromisoformat(str(item[0]))
            if not START <= day.isoformat() <= END:
                continue
            volume = (Decimal(str(item[5])) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            # Tencent order is [day, open, close, high, low, volume in lots].
            bars.append({
                "trading_day": day.isoformat(),
                "open_cents": cents(item[1]),
                "high_cents": cents(item[3]),
                "low_cents": cents(item[4]),
                "close_cents": cents(item[2]),
                "volume_shares": int(volume),
                "amount_cents": None,
                "amount_status": "provider_not_offered",
            })
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            continue
    by_day = {row["trading_day"]: row for row in bars}
    return [by_day[day] for day in sorted(by_day)]


def receipt_row(con: sqlite3.Connection, receipt_id: str) -> dict | None:
    row = con.execute(
        "SELECT receipt_id,source_id,url,requested_at,responded_at,first_seen_at,"
        "content_hash,outcome,http_status FROM fetch_receipt WHERE receipt_id=?",
        (receipt_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(zip(("receipt_id", "source_id", "source_url", "requested_at",
                     "responded_at", "observed_at", "actual_hash", "outcome", "http_status"), row))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="capture at most N not-yet-captured codes")
    ap.add_argument("--checkpoint-every", type=int, default=25)
    ap.add_argument("--resume", action="store_true", help="resume from the independent checkpoint")
    ap.add_argument("--retry-failed", action="store_true", help="retry prior failures once after a cooldown")
    ap.add_argument("--finalize-only", action="store_true", help="rebuild artifact metadata from checkpoint and receipts")
    args = ap.parse_args()
    BASE.mkdir(parents=True, exist_ok=True)
    source_bytes = INPUT.read_bytes()
    candidates = json.loads(source_bytes.decode("utf-8"))
    ranked = [c for c in candidates.get("candidates", []) if c.get("status") == "RANKED"]
    if len(ranked) != 3101:
        raise SystemExit(f"expected 3101 RANKED candidates, found {len(ranked)}")

    checkpoint_path = BASE / "checkpoint.json"
    if args.resume and checkpoint_path.exists():
        doc = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    else:
        probe_path = BASE / "probe-20.json"
        probe = json.loads(probe_path.read_text(encoding="utf-8")) if probe_path.exists() else {}
        doc = {
            "schema": "aquant.research_daily_history.v1",
            "created_at": utc_now(),
            "requested_window": {"start": START, "end": END, "adjustment": "unadjusted"},
            "source": "Tencent web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            "units": {"prices": "integer cents", "volume": "shares (Tencent lots × 100)",
                      "amount": "unavailable from source; null"},
            "input": {"path": str(INPUT), "content_hash": "sha256:" + hashlib.sha256(source_bytes).hexdigest()},
            "expected_ranked_count": len(ranked),
            "bars": probe.get("rows", {}),
            "failed": {},
            "attempted_count": len(probe.get("rows", {})) + len(probe.get("errors", [])),
            "last_checkpoint_at": utc_now(),
        }
        for err in probe.get("errors", []):
            doc["failed"][err["instrument_id"]] = err

    bars = doc.setdefault("bars", {})
    failed = doc.setdefault("failed", {})
    targets = [] if args.finalize_only else [
        c for c in ranked if c["instrumentId"] not in bars and
        (args.retry_failed or c["instrumentId"] not in failed)
    ]
    if args.limit:
        targets = targets[:args.limit]
    con = sqlite3.connect(BASE / "archive.sqlite", timeout=30)
    archive = ForwardArchive(con, BASE / "archive")
    client = TencentClient(
        archive,
        limiter=RateLimiter(min_interval_seconds=0.3),
        retry=RetryPolicy(max_attempts=2, base_delay_seconds=0.5, max_delay_seconds=2.0),
        timeout=20.0,
    )
    started = time.monotonic()
    started_at = utc_now()
    new_count = 0
    for ix, candidate in enumerate(targets, 1):
        instrument_id = candidate["instrumentId"]
        symbol = instrument_id.lower().replace(".", "")
        outcome, raw = client.daily_quotes(symbol, START, END, adjust=0)
        doc["attempted_count"] = int(doc.get("attempted_count", 0)) + 1
        if not outcome.ok:
            failed[instrument_id] = {
                "source": "tencent-ifzq", "observed_at": utc_now(),
                "receipt_id": outcome.receipt_id, "detail": outcome.detail,
            }
        else:
            converted = parse_bars(raw)
            if not converted:
                failed[instrument_id] = {
                    "source": "tencent-ifzq", "observed_at": utc_now(),
                    "receipt_id": outcome.receipt_id, "content_hash": outcome.content_hash,
                    "detail": "successful response contained no parseable daily bars",
                }
            else:
                bars[instrument_id] = {
                    "rows": converted,
                    "source": "tencent-ifzq",
                    "receipt_id": outcome.receipt_id,
                    "content_hash": outcome.content_hash,
                    "amount_status": "provider_not_offered",
                }
                failed.pop(instrument_id, None)
                new_count += 1
        if ix % args.checkpoint_every == 0:
            doc["last_checkpoint_at"] = utc_now()
            doc["last_batch"] = {"started_at": started_at, "captured_count": new_count,
                                 "elapsed_seconds": round(time.monotonic() - started, 2),
                                 "processed_in_batch": ix, "remaining_after_batch": len(targets) - ix}
            atomic_json(checkpoint_path, doc)
            print(json.dumps({"processed": ix, "batch_success": new_count,
                              "total_success": len(bars), "failed": len(failed),
                              "elapsed_seconds": round(time.monotonic() - started, 2)}, ensure_ascii=False), flush=True)

        # A provider circuit opening is concrete evidence to stop the batch.
        if not outcome.ok and getattr(client.breaker, "is_open", False):
            doc["stop_reason"] = "provider circuit opened after repeated failures"
            break

    doc["last_checkpoint_at"] = utc_now()
    doc["last_batch"] = {"started_at": started_at, "captured_count": new_count,
                         "elapsed_seconds": round(time.monotonic() - started, 2),
                         "processed_in_batch": min(len(targets), ix if targets else 0)}
    receipt_count = con.execute("SELECT COUNT(*) FROM fetch_receipt").fetchone()[0]
    atomic_json(checkpoint_path, doc)
    successes = len(bars)
    all_days = [r["trading_day"] for entry in bars.values() for r in entry.get("rows", [])]
    used_receipts = {}
    for entry in bars.values():
        rid = entry.get("receipt_id")
        receipt = receipt_row(con, rid) if rid else None
        if receipt and receipt.get("outcome") == "OK":
            used_receipts[rid] = receipt
            entry.update({
                "requested_at": receipt["requested_at"],
                "responded_at": receipt["responded_at"],
                "observed_at": receipt["observed_at"],
                "source_url": receipt["source_url"],
                "actual_hash": receipt["actual_hash"],
            })
    if len(used_receipts) != len(bars):
        missing = len(bars) - len(used_receipts)
        doc["receipt_integrity_issue_count"] = missing
    con.close()
    doc["data_asof"] = max(all_days) if all_days else None
    requested_times = [r["requested_at"] for r in used_receipts.values()]
    response_times = [r["responded_at"] for r in used_receipts.values()]
    doc["captured_from"] = min(requested_times) if requested_times else None
    doc["captured_to"] = max(response_times) if response_times else None
    doc["source_receipts"] = list(used_receipts.values())
    doc["coverage"] = {"ranked_count": len(ranked), "success_count": successes,
                       "failed_count": len(failed), "missing_count": len(ranked) - successes}
    doc["archive"] = {
        "sqlite_path": str(BASE / "archive.sqlite"),
        "raw_directory": str(BASE / "archive/raw"),
    }
    doc["checkpoint_path"] = str(checkpoint_path)
    atomic_json(OUTPUT, doc)
    doc_hash = sha256_file(OUTPUT)
    receipt = {"artifact_path": str(OUTPUT), "content_hash": doc_hash,
               "archived_at": utc_now(), "source_archive_sqlite": str(BASE / "archive.sqlite"),
               "raw_archive_directory": str(BASE / "archive/raw"),
               "source_receipt_count": receipt_count}
    # The output itself is hashed; provider response bytes and receipt IDs are
    # preserved in ForwardArchive. Keep an independent receipt alongside it.
    atomic_json(BASE / "hash-receipt.json", receipt)
    print(json.dumps({"output": str(OUTPUT), "checkpoint": str(checkpoint_path),
                      "success_count": successes, "failed_count": len(failed),
                      "data_asof": doc["data_asof"], "artifact_hash": doc_hash,
                      "elapsed_seconds": round(time.monotonic() - started, 2)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
