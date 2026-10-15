"""Strictly finish a failed package from its original complete capture archive.

No provider session, receipt creation, publication or business-ledger operation.
The ordinary collector's finalizer owns every package field and rule. Existing
checkpoint/cache/pool/login bytes and original source observations are retained.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from aquant.artifact_paths import resolve_archive_member
import capture_research_simulation_evidence as capture


def _hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def finalize_existing_capture(*, output_dir: Path, research_path: Path,
                              retained_path: Path, target_day: str, limit: int) -> dict:
    target = date.fromisoformat(target_day)
    output_dir, research_path, retained_path = output_dir.resolve(), research_path.resolve(), retained_path.resolve()
    frozen_paths = [output_dir / name for name in ("checkpoint.json", "cache-120-main.json", "pool-120-main.yaml", "login-attempts.json")]
    if not all(path.is_file() for path in frozen_paths):
        raise ValueError("recovery requires the original complete checkpoint/cache/pool/login files")
    if any((output_dir / name).exists() for name in ("summary.json", "hash-receipt.json")):
        raise ValueError("existing final summary/hash receipt must be preserved, not recovered again")
    before = {str(path): _hash(path) for path in frozen_paths}
    cp = json.loads((output_dir / "checkpoint.json").read_text(encoding="utf-8"))
    research = json.loads(research_path.read_text(encoding="utf-8"))
    retained_ids = capture.validate_retained_document(json.loads(retained_path.read_text(encoding="utf-8")), target_day)
    selected = capture.select_candidates(research, limit=limit, retained_ids=retained_ids)
    selected_ids = [row["instrumentId"] for row in selected]
    capture.checkpoint_for_resume(cp, target=target_day, research_path=research_path,
        research_hash=_hash(research_path), selected_ids=selected_ids,
        retained_path=retained_path, retained_hash=_hash(retained_path))
    if cp.get("failures") or set(cp["instruments"]) != set(selected_ids) or any(
            not capture.checkpoint_item_is_complete(cp["instruments"].get(iid), target_day) for iid in selected_ids):
        raise ValueError("offline recovery requires every exact selected instrument to be complete")
    capture.research_source_inputs(research, research_path, retained_path)
    receipts = cp["source_receipts"]
    if len({row["receipt_id"] for row in receipts}) != len(receipts):
        raise ValueError("duplicate capture receipt identities are ambiguous for offline recovery")
    con = sqlite3.connect("file:" + (output_dir / "archive.sqlite").as_posix() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("BEGIN")
    loaded = {}
    try:
        for receipt in receipts:
            row = con.execute("SELECT f.*,a.stored_path,a.byte_size AS artifact_size FROM fetch_receipt f JOIN raw_artifact a ON a.content_hash=f.content_hash WHERE f.receipt_id=?", (receipt["receipt_id"],)).fetchone()
            if row is None or row["outcome"] != "OK" or any(row[key] != receipt[key] for key in (
                    "source_id", "url", "requested_at", "responded_at", "first_seen_at", "content_hash", "byte_size")):
                raise ValueError("original receipt metadata differs from checkpoint")
            body = resolve_archive_member(output_dir / "archive", row["stored_path"]).read_bytes()
            if len(body) != row["artifact_size"] or len(body) != receipt["byte_size"] or "sha256:" + hashlib.sha256(body).hexdigest() != receipt["content_hash"]:
                raise ValueError("original raw receipt bytes differ from checkpoint")
            data = json.loads(body)
            if not isinstance(data, list) or len(data) != receipt["row_count"]:
                raise ValueError("original provider row list differs from receipt")
            loaded[receipt["receipt_id"]] = data
    finally:
        con.close()

    def original(url: str):
        candidates = [row for row in receipts if row["url"] == url]
        if len(candidates) != 1:
            raise ValueError("required original calendar/index/all-stock receipt is missing or ambiguous")
        receipt = candidates[0]
        return receipt, loaded[receipt["receipt_id"]]

    year_start, year_end = capture.calendar_range(target)
    trade_receipt, calendar_raw = original(f"baostock://www.baostock.com:10030/query_trade_dates?start_date={year_start}&end_date={year_end}")
    index_receipt, index_raw = original(f"baostock://www.baostock.com:10030/query_history_k_data_plus?code=sh.000001&start={year_start}&end={target_day}&adjustflag=3")
    allstock_receipt, allstock_raw = original(f"baostock://www.baostock.com:10030/query_all_stock?day={target_day}")
    # The original BaostockClient.daily_bars normalizer uses this exact cents
    # conversion. Recovery retains its result rather than changing rounding.
    calendar_rows = [{"trading_day": row["date"], "index_close_cents": int(round(float(row["close"]) * 100))}
                     for row in index_raw if row.get("close") not in (None, "")]
    codes = {capture.iid_to_bs(iid) for iid in selected_ids}
    allstock_rows = [row for row in allstock_raw if row.get("code") in codes]
    login = json.loads((output_dir / "login-attempts.json").read_text(encoding="utf-8"))
    if login.get("status") != "CONNECTED" or not login.get("attempts"):
        raise ValueError("original successful provider login evidence is missing")
    capture.finalize_capture_package(cp=cp, research=research, selected=selected,
        retained_ids=retained_ids, retained_path=retained_path, research_path=research_path,
        output_dir=output_dir, target_day=target_day, calendar_raw=calendar_raw,
        calendar_rows=calendar_rows, trade_calendar_receipt=trade_receipt, calendar_receipt=index_receipt,
        calendar_available_at=trade_receipt["first_seen_at"], allstock_rows=allstock_rows,
        allstock_receipt=allstock_receipt, source_receipts=receipts,
        login_attempts=login["attempts"], limit=limit, preserve_existing=True)
    after = {str(path): _hash(path) for path in frozen_paths}
    if before != after:
        raise ValueError("offline packaging changed an original capture/checkpoint file")
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    if summary["source_receipts"] != receipts:
        raise ValueError("offline packaging changed the original source receipts")
    return {"schema": "aquant.offline-capture-finalization.v1", "marketDay": target_day,
        "completedAt": datetime.now(timezone.utc).isoformat(), "summaryCreatedAt": summary["artifact_created_at"],
        "summaryPath": str(output_dir / "summary.json"), "summaryHash": _hash(output_dir / "summary.json"),
        "verifiedInstrumentCount": len(selected_ids), "verifiedReceiptCount": len(receipts),
        "providerCalls": 0, "databaseWrites": 0, "originalFileHashes": before,
        "originalFileHashesAfter": after, "originalReceiptsUnchanged": True,
        "publication": "NOT_PERFORMED", "ledgerAdvance": "NOT_PERFORMED"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--research", type=Path, required=True)
    parser.add_argument("--retain-instruments", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    result = finalize_existing_capture(output_dir=args.output_dir, research_path=args.research,
        retained_path=args.retain_instruments, target_day=args.day, limit=args.limit)
    if args.json_out:
        with args.json_out.open("x", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
