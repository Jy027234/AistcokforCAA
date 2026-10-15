"""Offline admission of Luna's receipted daily simulation input package."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path

from aquant.artifact_paths import resolve_artifact_path
from aquant.domain.data.forward_archive import ForwardArchive


def _hash(path: Path) -> str:
    with resolve_artifact_path(path).open("rb") as handle:
        return "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest()


def freeze_simulation_inputs(summary_path: Path, *, cache_path: Path,
                             pool_path: Path, data_root: Path) -> dict:
    """Retain exact inputs when the public research JSON changes tomorrow.

    Provider receipts and raw bodies stay in their dated append-only archive;
    this content-addressed copy retains the otherwise mutable source pointers.
    """
    summary_path = resolve_artifact_path(summary_path)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    sources = dict(summary["source_inputs"])
    sources.update({key: {"path": str(path), "sha256": _hash(path)}
                    for key, path in (("simulation_summary", summary_path),
                                      ("simulation_cache", cache_path),
                                      ("simulation_pool", pool_path))})
    root = data_root / "simulation_input_origins"
    root.mkdir(parents=True, exist_ok=True)
    frozen = {}
    for role, source in sources.items():
        original = resolve_artifact_path(source["path"])
        digest = source["sha256"]
        target = root / (digest.removeprefix("sha256:") + original.suffix)
        if _hash(original) != digest:
            raise ValueError("simulation research input changed before archival")
        if not target.exists():
            temp = root / ("." + target.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                shutil.copyfile(original, temp)
                if _hash(temp) != digest:
                    raise ValueError("simulation source changed while archiving")
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
        if _hash(target) != digest:
            raise ValueError("immutable simulation source archive hash mismatch")
        frozen[role] = {"originalPath": str(original.resolve()),
                        "path": str(target.resolve()), "contentHash": digest}
    return frozen


def _cents(value) -> int | None:
    return None if value in (None, "") else int((Decimal(str(value)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def admit_simulation_evidence(summary_path: Path, *, cache_path: Path, pool_path: Path,
                              cutoff: datetime) -> dict:
    """Rebuild identity, daily amounts, status and calendar from archived bytes.

    The future calendar is already published information. Future price rows
    remain forbidden. Neither an edited eligibility flag nor a JSON hash alone
    is a substitute for the original provider receipt.
    """
    if cutoff.tzinfo is None:
        raise ValueError("simulation decision cutoff must include a timezone")
    summary_path = resolve_artifact_path(summary_path)
    cache_path = resolve_artifact_path(cache_path)
    pool_path = resolve_artifact_path(pool_path)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    pool = json.loads(pool_path.read_text(encoding="utf-8"))
    if (summary.get("schema") != "aquant.simulation_research_evidence.v1"
            or cache.get("schema") != "aquant.simulation_universe_cache.v1"):
        raise ValueError("unsupported simulation evidence package")
    for source in summary["source_inputs"].values():
        if _hash(Path(source["path"])) != source["sha256"]:
            raise ValueError("simulation research input changed after capture")
    research = json.loads(resolve_artifact_path(summary["source_inputs"]["research_candidates"]["path"]).read_text(encoding="utf-8"))
    research_rows = {r["instrumentId"]: r for r in research["candidates"]}
    day = summary["evidence_market_date"]
    records = {item["instrument_id"]: item for item in summary["instruments"]}
    members = {item["instrument_id"]: item for item in pool["instruments"]}
    if len(records) != len(summary["instruments"]) or len(members) != len(pool["instruments"]) or set(members) != set(records) or set(cache["bars"]) != set(records):
        raise ValueError("simulation identity coverage differs across package files")
    con = sqlite3.connect(summary_path.parent / "archive.sqlite")
    con.row_factory = sqlite3.Row
    archive = ForwardArchive(con, summary_path.parent / "archive")
    loaded: dict[str, list[dict]] = {}
    receipt_map = {r["receipt_id"]: r for r in summary["source_receipts"]}

    def raw(receipt: dict) -> list[dict]:
        rid = receipt["receipt_id"]
        complete = receipt_map.get(rid)
        if complete is None or any(receipt.get(k) != complete.get(k) for k in ("source_id", "content_hash")):
            raise ValueError("simulation source reference has no matching complete receipt")
        receipt = complete
        if rid in loaded:
            return loaded[rid]
        saved = con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?", (rid,)).fetchone()
        if saved is None or saved["outcome"] != "OK" or any(saved[k] != receipt[k] for k in ("source_id", "url", "content_hash", "requested_at", "responded_at", "first_seen_at")):
            raise ValueError("simulation receipt differs from original archive")
        observed = datetime.fromisoformat(saved["first_seen_at"])
        if observed.tzinfo is None or observed > cutoff:
            raise ValueError("simulation evidence was unavailable at decision cutoff")
        if not archive.verify(saved["content_hash"]):
            raise ValueError("simulation raw response hash mismatch")
        body = archive.load_bytes(saved["content_hash"])
        if len(body) != saved["byte_size"]:
            raise ValueError("simulation raw response byte count mismatch")
        value = json.loads(body)
        if not isinstance(value, list):
            raise ValueError("simulation provider response is not a row list")
        loaded[rid] = value
        return value

    try:
        for receipt in summary["source_receipts"]:
            raw(receipt)
        calendar_receipt = cache["calendar_receipt"]
        calendar_rows = raw(calendar_receipt)
        calendar = sorted(row["calendar_date"] for row in calendar_rows if row["is_trading_day"] == "1")
        if [d for d in calendar if d <= day] != cache["trading_calendar"] or cache["calendar_available_at"] != calendar_receipt["first_seen_at"] or day not in calendar:
            raise ValueError("simulation calendar differs from published provider rows")
        for iid, item in records.items():
            cached = cache["bars"][iid]
            member = members[iid]
            research_row = research_rows.get(iid)
            exchange = {"SH": "SSE", "SZ": "SZSE"}.get(iid[:2])
            if (research_row is None or research_row["code"] != iid[3:]
                    or research_row["board"] != "MAIN" or not exchange
                    or any(part.get("exchange") != exchange or part.get("board") != "MAIN"
                           for part in (item, member, cached))
                    or member.get("industry") != research_row.get("industry")):
                raise ValueError("simulation exchange/board/industry differs from bound research identity")
            eligibility = item["simulation_eligibility"]
            if cached["simulation_eligibility"] != eligibility or cached["listed_on"] != item["listed_on"] or member["listed_on"] != item["listed_on"]:
                raise ValueError("simulation cache identity/eligibility was edited")
            history_receipt, adjusted_receipt = item["history_receipt"], item["adjusted_history_receipt"]
            history = {r["date"]: r for r in raw(history_receipt)}
            adjusted = {r["date"]: r for r in raw(adjusted_receipt)}
            for receipt in (history_receipt, adjusted_receipt):
                if f"code={iid.lower()}&" not in receipt["url"]:
                    raise ValueError("simulation price receipt belongs to another security")
            basic = next((r for r in raw(item["listed_on_source"]) if r.get("code") == iid.lower()), None)
            if basic is None or basic.get("ipoDate") != item["listed_on"] or member["board"] != "MAIN" or item["code"] != iid[3:]:
                raise ValueError("simulation listing identity differs from official basic row")
            identity = item["identity"]
            if (identity.get("official_basic_row") != basic or basic.get("type") != "1"
                    or basic.get("status") != "1" or (basic.get("outDate") and basic["outDate"] <= day)):
                raise ValueError("simulation basic identity is not an active listed equity")
            active_receipts = []
            for receipt in summary["source_receipts"]:
                if f"query_all_stock?day={day}" not in receipt["url"]:
                    continue
                stock = next((r for r in raw(receipt) if r.get("code") == iid.lower()), None)
                if stock == identity.get("all_stock_row") and stock is not None:
                    active_receipts.append(receipt)
            if not active_receipts or identity["all_stock_row"].get("tradeStatus") not in {"0", "1"}:
                raise ValueError("simulation active identity has no matching dated official stock receipt")
            active_available = min(r["first_seen_at"] for r in active_receipts)
            bars = cached["rows"]
            if not bars or len({bar["trading_day"] for bar in bars}) != len(bars):
                raise ValueError("simulation daily window empty or duplicated")
            for bar in bars:
                bar_day = bar["trading_day"]
                source = history.get(bar_day)
                adjusted_source = adjusted.get(bar_day)
                if bar_day > day or source is None or adjusted_source is None:
                    raise ValueError("simulation bar lacks original dated source")
                expected = {key + "_cents": _cents(source.get(key)) for key in ("open", "high", "low", "close")}
                expected.update(prev_close_cents=_cents(source.get("preclose")),
                    adjusted_close_cents=_cents(adjusted_source.get("close")),
                    volume_shares=int(source["volume"]) if source.get("volume") else None,
                    amount_cents=_cents(source.get("amount")),
                    tradestatus=source.get("tradestatus"), isST=source.get("isST"))
                if any(bar.get(k) != v for k, v in expected.items()):
                    raise ValueError(f"simulation bar values differ from receipt: {iid} {bar_day}")
            last = history.get(day)
            if (last is None or bars[-1]["trading_day"] != day
                    or eligibility["as_of_date"] != day or eligibility["status"] != "VERIFIED"
                    or eligibility["is_st"] != (last.get("isST") == "1")
                    or eligibility["is_suspended"] != (last.get("tradestatus") != "1")
                    or eligibility["observed_at"] != history_receipt["first_seen_at"]
                    or eligibility["available_at"] != max(history_receipt["first_seen_at"], receipt_map[item["listed_on_source"]["receipt_id"]]["first_seen_at"], active_available)):
                raise ValueError("simulation status differs from source observation")
        return {"summaryHash": _hash(summary_path), "cacheHash": _hash(cache_path),
                "poolHash": _hash(pool_path), "marketDay": day, "verifiedCount": len(records),
                "calendar": calendar, "decisionCutoffAt": cutoff.astimezone(timezone.utc).isoformat()}
    finally:
        con.close()
