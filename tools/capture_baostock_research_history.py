"""Refresh ranked research daily bars through BaoStock with bounded checkpoints."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.baostock import BaostockClient  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402

BASE = ROOT / "data/s2-structured/research-daily-checkpoint-20261009"
FIXED_INPUT = BASE / "input-research-candidates.json"
INPUT = FIXED_INPUT
BS_CHECKPOINT = BASE / "baostock-checkpoint.json"
OUTPUT = ROOT / "data/s2-structured/research-history-20261009.json"
START = "2026-06-22"
END = "2026-10-09"


def now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def atomic_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def verify_rows(value: object) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("BaoStock parsed rows are not a list")
    rows = {}
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("BaoStock parsed source contains a non-object row")
        raw_day = row.get("trading_day")
        try:
            day = date.fromisoformat(raw_day)
        except (TypeError, ValueError):
            raise ValueError(f"invalid BaoStock parsed trading date: {raw_day!r}")
        if not START <= day.isoformat() <= END:
            raise ValueError(f"BaoStock parsed date outside requested window: {day.isoformat()}")
        if day.isoformat() in rows:
            raise ValueError(f"duplicate BaoStock parsed date: {day.isoformat()}")
        fixed = {key: row.get(key) for key in (
            "trading_day", "open_cents", "high_cents", "low_cents", "close_cents",
            "volume_shares", "amount_cents")}
        # BaoStock returns amount_cents and share volume in its adapter. Preserve
        # a null amount explicitly where that provider genuinely omitted it.
        fixed["amount_status"] = (
            "source_returned_value" if fixed["amount_cents"] is not None
            else "source_returned_null")
        rows[day.isoformat()] = fixed
    return [rows[day] for day in sorted(rows)]


def decimal_integer(value: object, scale: int = 100) -> int | None:
    if value in (None, ""):
        return None
    return int((Decimal(str(value)) * scale).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def rows_from_raw(raw: object, provider: str) -> list[dict]:
    """Reconstruct amounts/prices from provider strings with explicit units."""
    if provider == "baostock-socket":
        if not isinstance(raw, list):
            raise ValueError("BaoStock archived JSON is not a row list")
        by_day = {}
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("BaoStock source contains a non-object row")
            day_s = item.get("date")
            try:
                day = date.fromisoformat(day_s)
            except (TypeError, ValueError):
                raise ValueError(f"invalid BaoStock trading date: {day_s!r}")
            if not START <= day.isoformat() <= END:
                raise ValueError(f"BaoStock date outside requested window: {day.isoformat()}")
            key = day.isoformat()
            if key in by_day:
                raise ValueError(f"duplicate BaoStock date in source response: {key}")
            close = decimal_integer(item.get("close"))
            amount = decimal_integer(item.get("amount"))
            volume_value = item.get("volume")
            volume = None if volume_value in (None, "") else int(Decimal(str(volume_value)).quantize(
                Decimal("1"), rounding=ROUND_HALF_EVEN))
            by_day[key] = {
                "trading_day": key,
                "open_cents": decimal_integer(item.get("open")),
                "high_cents": decimal_integer(item.get("high")),
                "low_cents": decimal_integer(item.get("low")),
                "close_cents": close,
                "volume_shares": volume,
                "amount_cents": amount,
                "amount_status": "source_returned_value" if amount is not None else "source_returned_null",
            }
        return [by_day[k] for k in sorted(by_day)]
    if provider == "tencent-ifzq":
        if not isinstance(raw, dict):
            raise ValueError("Tencent archived JSON is not an object")
        node = (raw.get("data") or {})
        if not isinstance(node, dict) or len(node) != 1:
            raise ValueError("Tencent archived response does not identify exactly one symbol")
        payload = next(iter(node.values()))
        daily = payload.get("day") if isinstance(payload, dict) else None
        if not isinstance(daily, list):
            raise ValueError("Tencent archived response has no daily rows")
        by_day = {}
        for item in daily:
            if not isinstance(item, list) or len(item) < 6:
                raise ValueError("Tencent source contains a malformed daily row")
            try:
                day = date.fromisoformat(item[0])
            except (TypeError, ValueError):
                raise ValueError(f"invalid Tencent trading date: {item[0]!r}")
            if not START <= day.isoformat() <= END:
                raise ValueError(f"Tencent date outside requested window: {day.isoformat()}")
            key = day.isoformat()
            if key in by_day:
                raise ValueError(f"duplicate Tencent date in source response: {key}")
            close = decimal_integer(item[2])
            lots = item[5]
            volume = None if lots in (None, "") else decimal_integer(lots, scale=100)
            by_day[key] = {
                "trading_day": key,
                "open_cents": decimal_integer(item[1]),
                "high_cents": decimal_integer(item[3]),
                "low_cents": decimal_integer(item[4]),
                "close_cents": close,
                "volume_shares": volume,
                "amount_cents": None,
                "amount_status": "provider_not_offered",
            }
        return [by_day[k] for k in sorted(by_day)]
    raise ValueError(f"unknown provider: {provider}")


def baostock_entry(client: BaostockClient, archive: ForwardArchive, instrument_id: str) -> dict:
    code = instrument_id.lower()
    requested = now()
    source_url = f"baostock://www.baostock.com:10030/query_history_k_data_plus?code={code}&start={START}&end={END}&adjustflag=3"
    try:
        raw_rows, source_receipt = client.daily_bars(
            code, start=START, end=END, adjust="3")
        responded = now()
        rows = verify_rows(raw_rows)
        if not rows:
            raise ValueError("successful BaoStock response had no valid rows in requested date range")
        raw_path = BASE / "baostock-archive" / source_receipt.stored_path
        body = raw_path.read_bytes()
        raw_hash = sha256(body)
        if raw_hash != source_receipt.content_hash:
            raise ValueError("BaoStock raw archive bytes failed SHA-256 verification")
        stored_hash, _ = archive.store_bytes(body, media_type="application/json")
        if stored_hash != raw_hash:
            raise ValueError("ForwardArchive hash disagrees with BaoStock source hash")
        wrapped = archive.record(
            source_id="baostock-socket", url=source_url, outcome="OK",
            requested_at=requested, responded_at=responded,
            content_hash=stored_hash, byte_size=len(body),
            detail=f"{source_receipt.label}; raw={source_receipt.stored_path}",
        )
        return {
            "rows": rows, "source": "baostock-socket", "receipt_id": wrapped.receipt_id,
            "requested_at": stamp(requested), "responded_at": stamp(responded),
            "observed_at": stamp(wrapped.first_seen_at), "source_url": source_url,
            "actual_hash": raw_hash, "content_hash": raw_hash,
            "raw_archive_path": source_receipt.stored_path,
            "raw_archive_root": str(BASE / "baostock-archive"),
            "amount_status": "returned_when_available",
        }
    except Exception as exc:
        responded = now()
        archive.record(
            source_id="baostock-socket", url=source_url, outcome="TRANSPORT_ERROR",
            requested_at=requested, responded_at=responded,
            detail=f"{type(exc).__name__}: {str(exc)[:240]}",
        )
        raise


def baostock_end_day(client: BaostockClient, archive: ForwardArchive,
                     instrument_id: str) -> dict:
    code = instrument_id.lower()
    requested = now()
    source_url = f"baostock://www.baostock.com:10030/query_history_k_data_plus?code={code}&start={END}&end={END}&adjustflag=3"
    try:
        _parsed, source_receipt = client.daily_bars(
            code, start=END, end=END, adjust="3")
        responded = now()
        raw_path = BASE / "baostock-archive" / source_receipt.stored_path
        body = raw_path.read_bytes()
        raw_hash = sha256(body)
        if raw_hash != source_receipt.content_hash:
            raise ValueError("BaoStock raw archive bytes failed SHA-256 verification")
        stored_hash, _ = archive.store_bytes(body, media_type="application/json")
        if stored_hash != raw_hash:
            raise ValueError("ForwardArchive hash disagrees with BaoStock source hash")
        wrapped = archive.record(
            source_id="baostock-socket", url=source_url, outcome="OK",
            requested_at=requested, responded_at=responded,
            content_hash=stored_hash, byte_size=len(body),
            detail=f"{source_receipt.label}; raw={source_receipt.stored_path}",
        )
        raw_doc = json.loads(body.decode("utf-8"))
        rows = rows_from_raw(raw_doc, "baostock-socket")
        if any(row["trading_day"] != END for row in rows):
            raise ValueError("end-day refresh response contains a date other than requested END")
        receipt = {
            "receipt_id": wrapped.receipt_id, "source_id": "baostock-socket",
            "source_url": source_url, "requested_at": stamp(requested),
            "responded_at": stamp(responded), "observed_at": stamp(wrapped.first_seen_at),
            "actual_hash": raw_hash, "outcome": "OK", "http_status": None,
        }
        return {"receipt": receipt, "rows": rows,
                "raw_archive_path": source_receipt.stored_path,
                "raw_archive_root": str(BASE / "baostock-archive")}
    except Exception as exc:
        responded = now()
        wrapped = archive.record(
            source_id="baostock-socket", url=source_url, outcome="TRANSPORT_ERROR",
            requested_at=requested, responded_at=responded,
            detail=f"{type(exc).__name__}: {str(exc)[:240]}",
        )
        return {"receipt": {"receipt_id": wrapped.receipt_id,
                             "source_id": "baostock-socket", "source_url": source_url,
                             "requested_at": stamp(requested), "responded_at": stamp(responded),
                             "observed_at": stamp(wrapped.first_seen_at),
                             "actual_hash": None, "outcome": "TRANSPORT_ERROR",
                             "detail": f"{type(exc).__name__}: {str(exc)[:240]}"},
                "rows": [], "error": f"{type(exc).__name__}: {str(exc)[:240]}"}


def refresh_old_end_day(doc: dict, ranked: list[dict]) -> int:
    output_doc = json.loads(OUTPUT.read_text(encoding="utf-8"))
    if output_doc.get("input", {}).get("content_hash") != doc.get("input_hash"):
        raise SystemExit("deployed history input hash does not match fixed original candidate snapshot")
    if Path(output_doc["input"]["path"]) != FIXED_INPUT:
        raise SystemExit("existing history does not point to the immutable input snapshot")
    old_bars = output_doc.get("bars", {})
    stale = [c["instrumentId"] for c in ranked
             if c["instrumentId"] in old_bars and old_bars[c["instrumentId"]].get("last_trading_day") < END]
    if not stale:
        print(json.dumps({"refreshed": 0, "reason": "no codes have a last bar before requested end"}))
        return 0
    probe_code = "SZ.002039"
    if probe_code not in stale:
        raise SystemExit("fixed-input probe code SZ.002039 is not in the old-date refresh set")
    refreshes = doc.setdefault("end_day_refreshes", {})
    probe_path = BASE / "end-day-probe-002039.json"
    if probe_code in refreshes:
        probe = refreshes[probe_code]
    elif probe_path.exists():
        probe = json.loads(probe_path.read_text(encoding="utf-8"))
        refreshes[probe_code] = probe
    else:
        con = sqlite3.connect(BASE / "archive.sqlite", timeout=30)
        archive = ForwardArchive(con, BASE / "archive")
        with BaostockClient(BASE / "baostock-archive", min_interval_seconds=0.05,
                            query_timeout=35.0) as client:
            probe = baostock_end_day(client, archive, probe_code)
        con.close()
        atomic_json(probe_path, probe)
        refreshes[probe_code] = probe
        atomic_json(BS_CHECKPOINT, doc)
    if not probe.get("rows") or not any(r.get("trading_day") == END for r in probe["rows"]):
        receipt = probe.get("receipt") or {}
        receipt_map = {r.get("receipt_id"): r for r in output_doc.get("source_receipts", [])}
        if receipt.get("receipt_id"):
            receipt_map[receipt["receipt_id"]] = receipt
        output_doc["source_receipts"] = list(receipt_map.values())
        ok_req = [r["requested_at"] for r in receipt_map.values() if r.get("outcome") == "OK"]
        ok_resp = [r["responded_at"] for r in receipt_map.values() if r.get("outcome") == "OK"]
        if ok_req:
            output_doc["captured_from"] = min(ok_req)
        if ok_resp:
            output_doc["captured_to"] = max(ok_resp)
        output_doc["end_day_refresh"] = {
            "status": "PROBE_NO_BAR_ON_REQUESTED_END",
            "probe_code": probe_code, "requested_end": END,
            "probe_receipt": probe.get("receipt"),
            "remaining_stale_count": len(stale),
            "note": "Stopped after the single probe; no other stale security was requested.",
        }
        atomic_json(OUTPUT, output_doc)
        output_hash = sha256(OUTPUT.read_bytes())
        atomic_json(BASE / "hash-receipt.json", {
            "artifact_path": str(OUTPUT), "content_hash": output_hash,
            "archived_at": stamp(now()), "source_receipt_count": len(receipt_map),
            "archive_sqlite": str(BASE / "archive.sqlite")})
        print(json.dumps(output_doc["end_day_refresh"], ensure_ascii=False))
        return 0

    # A successful probe is the one permitted request for 002039. Each other
    # stale code is queried once for the requested end date, with checkpointing.
    remaining = [iid for iid in stale if iid not in refreshes]
    con = sqlite3.connect(BASE / "archive.sqlite", timeout=30)
    archive = ForwardArchive(con, BASE / "archive")
    started = now()
    try:
        with BaostockClient(BASE / "baostock-archive", min_interval_seconds=0.05,
                            query_timeout=35.0) as client:
            for ix, iid in enumerate(remaining, 1):
                refreshes[iid] = baostock_end_day(client, archive, iid)
                if ix % 25 == 0:
                    doc["end_day_refresh_status"] = {
                        "requested_end": END, "stale_target_count": len(stale),
                        "completed": len(refreshes), "elapsed_seconds":
                        round((now() - started).total_seconds(), 2)}
                    atomic_json(BS_CHECKPOINT, doc)
                    print(json.dumps(doc["end_day_refresh_status"], ensure_ascii=False), flush=True)
    except Exception as exc:
        doc["end_day_refresh_stop_reason"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    con.close()

    receipt_map = {r.get("receipt_id"): r for r in output_doc.get("source_receipts", [])}
    for iid, refresh in refreshes.items():
        receipt = refresh.get("receipt") or {}
        if receipt.get("receipt_id"):
            receipt_map[receipt["receipt_id"]] = receipt
        entry = old_bars[iid]
        entry["history_capture"] = entry.get("history_capture") or {
            k: entry.get(k) for k in ("receipt_id", "source_url", "requested_at", "responded_at",
                                      "observed_at", "actual_hash", "raw_archive_path") if entry.get(k) is not None}
        entry["latest_refresh"] = {
            "requested_end": END, "receipt": receipt,
            "raw_archive_path": refresh.get("raw_archive_path"),
            "raw_archive_root": refresh.get("raw_archive_root"),
            "status": "BAR_RETURNED" if refresh.get("rows") else "NO_BAR_RETURNED",
        }
        by_day = {r["trading_day"]: r for r in entry.get("rows", [])}
        for row in refresh.get("rows", []):
            row.update({"source_receipt_id": receipt.get("receipt_id"),
                        "source_actual_hash": receipt.get("actual_hash"),
                        "source_observed_at": receipt.get("observed_at"),
                        "source_url": receipt.get("source_url")})
            by_day[row["trading_day"]] = row
        entry["rows"] = [by_day[d] for d in sorted(by_day)]
        entry["first_trading_day"] = entry["rows"][0]["trading_day"]
        entry["last_trading_day"] = entry["rows"][-1]["trading_day"]
        entry["latest_day_status"] = (
            "BAR_RETURNED_ON_REQUESTED_END" if entry["last_trading_day"] == END
            else "NO_BAR_RETURNED_ON_REQUESTED_END")

    receipts = list(receipt_map.values())
    times_req = [r["requested_at"] for r in receipts if r.get("outcome") == "OK"]
    times_resp = [r["responded_at"] for r in receipts if r.get("outcome") == "OK"]
    output_doc["source_receipts"] = receipts
    output_doc["captured_from"] = min(times_req) if times_req else output_doc.get("captured_from")
    output_doc["captured_to"] = max(times_resp) if times_resp else output_doc.get("captured_to")
    output_doc["latest_day_coverage"] = {
        "requested_end": END,
        "bar_returned_on_requested_end_count": sum(
            1 for e in old_bars.values() if e.get("last_trading_day") == END),
        "no_bar_returned_on_requested_end": [
            iid for iid, e in old_bars.items() if e.get("last_trading_day") != END],
    }
    output_doc["end_day_refresh"] = {
        "status": "COMPLETED", "probe_code": probe_code,
        "requested_end": END, "stale_target_count": len(stale),
        "request_count": len(refreshes),
        "new_bars_count": sum(1 for r in refreshes.values() if r.get("rows")),
        "no_bar_count": sum(1 for r in refreshes.values() if not r.get("rows")),
        "stop_reason": doc.get("end_day_refresh_stop_reason"),
    }
    output_doc["coverage"]["bar_count"] = sum(len(e["rows"]) for e in old_bars.values())
    atomic_json(OUTPUT, output_doc)
    output_hash = sha256(OUTPUT.read_bytes())
    atomic_json(BASE / "hash-receipt.json", {
        "artifact_path": str(OUTPUT), "content_hash": output_hash,
        "archived_at": stamp(now()), "source_receipt_count": len(receipts),
        "archive_sqlite": str(BASE / "archive.sqlite")})
    doc["end_day_refresh_status"] = output_doc["end_day_refresh"]
    doc["bars"] = old_bars
    atomic_json(BS_CHECKPOINT, doc)
    print(json.dumps({"refresh": output_doc["end_day_refresh"],
                      "coverage": output_doc["latest_day_coverage"],
                      "output_hash": output_hash}, ensure_ascii=False))
    return 0


def finalize(doc: dict, ranked: list[dict], source_hash: str) -> None:
    tencent_doc = json.loads((BASE / "checkpoint.json").read_text(encoding="utf-8"))
    baostock_bars = doc.get("bars", {})
    tencent_bars = tencent_doc.get("bars", {})
    merged: dict[str, dict] = {}
    for candidate in ranked:
        iid = candidate["instrumentId"]
        if iid in baostock_bars:
            merged[iid] = baostock_bars[iid]
        elif iid in tencent_bars:
            merged[iid] = dict(tencent_bars[iid])
    con = sqlite3.connect(BASE / "archive.sqlite")
    all_receipts = {}
    for iid, entry in merged.items():
        rid = entry.get("receipt_id")
        if not rid:
            continue
        row = con.execute(
            "SELECT receipt_id,source_id,url,requested_at,responded_at,first_seen_at,content_hash,outcome,http_status "
            "FROM fetch_receipt WHERE receipt_id=?", (rid,)).fetchone()
        if not row:
            continue
        receipt = dict(zip(("receipt_id", "source_id", "source_url", "requested_at",
                            "responded_at", "observed_at", "actual_hash", "outcome", "http_status"), row))
        if receipt["outcome"] == "OK":
            if receipt["actual_hash"] != entry.get("actual_hash", entry.get("content_hash")):
                raise ValueError(f"raw hash mismatch for {iid}: receipt {rid}")
            if entry.get("source") == "baostock-socket":
                raw_path = BASE / "baostock-archive" / entry["raw_archive_path"]
                body = raw_path.read_bytes()
            else:
                stored = con.execute("SELECT stored_path FROM raw_artifact WHERE content_hash=?",
                                     (receipt["actual_hash"],)).fetchone()
                if not stored:
                    raise ValueError(f"Tencent raw artifact missing for {iid}: {rid}")
                body = (BASE / "archive" / stored[0]).read_bytes()
            if "sha256:" + hashlib.sha256(body).hexdigest() != receipt["actual_hash"]:
                raise ValueError(f"source bytes failed SHA-256 verification for {iid}")
            raw_doc = json.loads(body.decode("utf-8"))
            entry["rows"] = rows_from_raw(raw_doc, entry["source"])
            if not entry["rows"]:
                raise ValueError(f"source response has no valid rows for {iid}")
            refresh = doc.get("end_day_refreshes", {}).get(iid)
            if refresh:
                entry["history_capture"] = entry.get("history_capture") or {
                    k: entry.get(k) for k in ("receipt_id", "source_url", "requested_at", "responded_at",
                                              "observed_at", "actual_hash", "raw_archive_path") if entry.get(k) is not None}
                refresh_receipt = refresh.get("receipt") or {}
                by_day = {r["trading_day"]: r for r in entry["rows"]}
                for refreshed_row in refresh.get("rows", []):
                    refreshed_row.update({"source_receipt_id": refresh_receipt.get("receipt_id"),
                                          "source_actual_hash": refresh_receipt.get("actual_hash"),
                                          "source_observed_at": refresh_receipt.get("observed_at"),
                                          "source_url": refresh_receipt.get("source_url")})
                    by_day[refreshed_row["trading_day"]] = refreshed_row
                entry["rows"] = [by_day[d] for d in sorted(by_day)]
                entry["latest_refresh"] = {
                    "requested_end": END, "receipt": refresh_receipt,
                    "raw_archive_path": refresh.get("raw_archive_path"),
                    "raw_archive_root": refresh.get("raw_archive_root"),
                    "status": "BAR_RETURNED" if refresh.get("rows") else "NO_BAR_RETURNED",
                }
                if refresh_receipt.get("receipt_id"):
                    all_receipts[refresh_receipt["receipt_id"]] = refresh_receipt
            entry["first_trading_day"] = entry["rows"][0]["trading_day"]
            entry["last_trading_day"] = entry["rows"][-1]["trading_day"]
            entry["requested_window"] = {"start": START, "end": END}
            entry["latest_day_status"] = (
                "BAR_RETURNED_ON_REQUESTED_END" if entry["last_trading_day"] == END
                else "NO_BAR_RETURNED_ON_REQUESTED_END")
            entry.update({"source_url": receipt["source_url"],
                          "requested_at": receipt["requested_at"],
                          "responded_at": receipt["responded_at"],
                          "observed_at": receipt["observed_at"],
                          "actual_hash": receipt["actual_hash"]})
            all_receipts[rid] = receipt
    con.close()
    timestamps_requested = [v["requested_at"] for v in all_receipts.values()]
    timestamps_responded = [v["responded_at"] for v in all_receipts.values()]
    days = [r["trading_day"] for entry in merged.values() for r in entry.get("rows", [])]
    result = {
        "schema": "aquant.research_daily_history.v1",
        "prepared_at": stamp(now()),
        "captured_from": min(timestamps_requested) if timestamps_requested else None,
        "captured_to": max(timestamps_responded) if timestamps_responded else None,
        "data_asof": max(days) if days else None,
        "requested_window": {"start": START, "end": END, "adjustment": "unadjusted"},
        "scope": "all RANKED entries from research-candidates.v2; no 900-code cache restriction",
        "expected_ranked_count": len(ranked),
        "bars": merged,
        "failed": doc.get("failed", {}),
        "coverage": {"ranked_count": len(ranked), "success_count": len(merged),
                     "missing_count": len(ranked) - len(merged),
                     "baostock_count": sum(1 for e in merged.values() if e.get("source") == "baostock-socket"),
                     "tencent_fallback_count": sum(1 for e in merged.values() if e.get("source") == "tencent-ifzq")},
        "units": {"price": "integer cents", "volume": "shares", "amount": "integer cents when returned; null when absent"},
        "source_receipts": list(all_receipts.values()),
        "end_day_refresh": doc.get("end_day_refresh_status"),
        "latest_day_coverage": {
            "requested_end": END,
            "bar_returned_on_requested_end_count": sum(
                1 for e in merged.values() if e.get("last_trading_day") == END),
            "no_bar_returned_on_requested_end": [
                iid for iid, e in merged.items() if e.get("last_trading_day") != END],
        },
        "source_archive": {"sqlite": str(BASE / "archive.sqlite"),
                           "baostock_raw_root": str(BASE / "baostock-archive"),
                           "tencent_raw_root": str(BASE / "archive/raw")},
        "input": {"path": str(FIXED_INPUT), "content_hash": source_hash},
        "limitations": [
            "Historical rows are current provider observations, not historical point-in-time revisions.",
            "Tencent fallback rows explicitly retain amount_cents=null because Tencent daily bars do not offer amount.",
        ],
    }
    atomic_json(OUTPUT, result)
    content_hash = sha256(OUTPUT.read_bytes())
    receipt = {"artifact_path": str(OUTPUT), "content_hash": content_hash,
               "archived_at": stamp(now()), "source_receipt_count": len(all_receipts),
               "archive_sqlite": str(BASE / "archive.sqlite")}
    atomic_json(BASE / "hash-receipt.json", receipt)
    print(json.dumps({"output": str(OUTPUT), "coverage": result["coverage"],
                      "data_asof": result["data_asof"], "captured_from": result["captured_from"],
                      "captured_to": result["captured_to"], "content_hash": content_hash}, ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--finalize-only", action="store_true")
    ap.add_argument("--refresh-missing-end", action="store_true",
                    help="probe SZ.002039 once; if it has END data refresh only old-end securities")
    ap.add_argument("--checkpoint-every", type=int, default=25)
    args = ap.parse_args()
    BASE.mkdir(parents=True, exist_ok=True)
    if not INPUT.exists():
        raise SystemExit("fixed original candidates snapshot is missing")
    input_bytes = INPUT.read_bytes()
    source_hash = sha256(input_bytes)
    source_doc = json.loads(input_bytes.decode("utf-8"))
    ranked = [c for c in source_doc.get("candidates", []) if c.get("status") == "RANKED"]
    if len(ranked) != 3101:
        raise SystemExit(f"expected 3101 ranked entries; found {len(ranked)}")
    doc = json.loads(BS_CHECKPOINT.read_text(encoding="utf-8")) if args.resume and BS_CHECKPOINT.exists() else {
        "schema": "aquant.research_daily_baostock_checkpoint.v1", "created_at": stamp(now()),
        "input_hash": source_hash, "requested_window": {"start": START, "end": END},
        "bars": {}, "failed": {}, "attempted_count": 0,
    }
    if doc.get("input_hash") != source_hash:
        raise SystemExit("candidate source hash changed since checkpoint creation")
    if args.finalize_only:
        finalize(doc, ranked, source_hash)
        return 0
    if args.refresh_missing_end:
        return refresh_old_end_day(doc, ranked)

    bars = doc.setdefault("bars", {})
    failed = doc.setdefault("failed", {})
    targets = [c for c in ranked if c["instrumentId"] not in bars]
    if args.limit:
        targets = targets[:args.limit]
    con = sqlite3.connect(BASE / "archive.sqlite", timeout=30)
    archive = ForwardArchive(con, BASE / "archive")
    started = now()
    attempts = 0
    try:
        with BaostockClient(BASE / "baostock-archive", min_interval_seconds=0.05, query_timeout=35.0) as client:
            for ix, candidate in enumerate(targets, 1):
                iid = candidate["instrumentId"]
                attempts += 1
                try:
                    bars[iid] = baostock_entry(client, archive, iid)
                    failed.pop(iid, None)
                except Exception as exc:
                    failed[iid] = {"source": "baostock-socket", "observed_at": stamp(now()),
                                   "detail": f"{type(exc).__name__}: {str(exc)[:240]}"}
                doc["attempted_count"] = int(doc.get("attempted_count", 0)) + 1
                if ix % args.checkpoint_every == 0:
                    doc["last_checkpoint_at"] = stamp(now())
                    doc["last_batch"] = {"attempted": ix, "succeeded": len(bars),
                                          "failed": len(failed),
                                          "elapsed_seconds": round((now() - started).total_seconds(), 2)}
                    atomic_json(BS_CHECKPOINT, doc)
                    print(json.dumps(doc["last_batch"], ensure_ascii=False), flush=True)
    except Exception as exc:
        doc["stop_reason"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    doc["last_checkpoint_at"] = stamp(now())
    doc["last_batch"] = {"attempted": attempts, "succeeded": len(bars), "failed": len(failed),
                          "elapsed_seconds": round((now() - started).total_seconds(), 2)}
    atomic_json(BS_CHECKPOINT, doc)
    con.close()
    finalize(doc, ranked, source_hash)
    print(json.dumps({"checkpoint": str(BS_CHECKPOINT), "last_batch": doc["last_batch"],
                      "stop_reason": doc.get("stop_reason")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
