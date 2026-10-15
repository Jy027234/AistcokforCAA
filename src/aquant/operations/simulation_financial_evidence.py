"""Offline admission of receipt-backed optional BaoStock profit records."""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from ..artifact_paths import resolve_archive_member, resolve_artifact_path
from ..domain.data.db import connect

SCHEMA = "aquant.simulation-financial-evidence.v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")


class FinancialEvidenceError(ValueError):
    pass


def _hash(path):
    return "sha256:" + hashlib.sha256(resolve_artifact_path(path).read_bytes()).hexdigest()


def _time(raw, field):
    value = datetime.fromisoformat(str(raw))
    if value.tzinfo is None or value.utcoffset() is None:
        raise FinancialEvidenceError(f"{field} must contain an actual timezone-aware timestamp")
    return value


def _load(path):
    value = json.loads(resolve_artifact_path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FinancialEvidenceError("financial evidence/cache must be a JSON object")
    return value


def admit_simulation_financial_evidence(evidence_path: str | Path, *,
                                      financials_path: str | Path | None = None,
                                      cutoff: datetime | None = None,
                                      expected_instrument_ids=None) -> dict:
    path = resolve_artifact_path(evidence_path).resolve()
    package = _load(path)
    if package.get("schema") != SCHEMA:
        raise FinancialEvidenceError("financial evidence schema differs from the supported contract")
    cache_path = resolve_artifact_path(package["cachePath"]).resolve()
    base_path = resolve_artifact_path(package["baseCachePath"]).resolve()
    if cache_path == base_path:
        raise FinancialEvidenceError("new financial cache must preserve the separate inherited base cache")
    if financials_path is not None and resolve_artifact_path(financials_path).resolve() != cache_path:
        raise FinancialEvidenceError("financials path differs from its bound evidence cache")
    if _hash(cache_path) != package["cacheHash"] or _hash(base_path) != package["baseCacheHash"]:
        raise FinancialEvidenceError("financial cache/base content hash differs from its evidence binding")
    start = _time(package["capturedFrom"], "capturedFrom")
    end = _time(package["capturedTo"], "capturedTo")
    if start > end or (cutoff is not None and end > cutoff):
        raise FinancialEvidenceError("financial capture window exceeds publication cutoff or is reversed")
    raw_targets = package.get("targets")
    targets = package.get("scopeInstrumentIds", raw_targets)
    periods = package.get("periods")
    if (not isinstance(targets, list) or not targets or len(set(targets)) != len(targets) or
            any(not re.fullmatch(r"(?:SH|SZ)\.\d{6}", iid) for iid in targets) or
            not isinstance(periods, list) or not periods or len(set(periods)) != len(periods) or
            any(not re.fullmatch(r"\d{4}Q[1-4]", period) for period in periods)):
        raise FinancialEvidenceError("financial demand needs exact unique SH/SZ IDs and YYYYQ periods")
    if raw_targets and isinstance(raw_targets[0], dict):
        target_pairs = []
        for target in raw_targets:
            iid, period = target.get("instrumentId"), target.get("periodKey")
            if iid not in targets or period not in periods:
                raise FinancialEvidenceError("financial request target differs from declared scope/periods")
            if (target.get("providerCode") != iid.lower() or target.get("year") != int(period[:4]) or
                    target.get("quarter") != int(period[-1])):
                raise FinancialEvidenceError("financial request target identity/year/quarter is inconsistent")
            target_pairs.append((iid, period))
        if len(set(target_pairs)) != len(target_pairs) or set(target_pairs) != {
                (iid, period) for iid in targets for period in periods}:
            raise FinancialEvidenceError("financial serialized request demand is incomplete or repeated")
        demand_path = resolve_artifact_path(package["demandPath"])
        if (_hash(demand_path) != package["demandHash"] or
                _load(demand_path).get("records") != raw_targets):
            raise FinancialEvidenceError("financial serialized demand differs from its frozen original hash")
    records = package.get("records")
    expected = {(iid, period) for iid in targets for period in periods}
    if not isinstance(records, list) or len(records) != len(expected):
        raise FinancialEvidenceError("financial evidence does not cover its exact security-period demand")
    cache = _load(cache_path)
    base = _load(base_path)
    if not isinstance(base.get("statements"), dict) or not isinstance(cache.get("statements"), dict):
        raise FinancialEvidenceError("financial cache statements must be a JSON object")
    derived = json.loads(json.dumps(base["statements"]))
    seen = set()
    counts = Counter()
    receipts = set()
    archive_db = resolve_artifact_path(package["archiveDbPath"]).resolve()
    archive_root = resolve_artifact_path(package["archiveRoot"]).resolve()
    with closing(connect(archive_db, read_only=True)) as con:
        for record in records:
            pair = (record.get("instrumentId"), record.get("periodKey"))
            if pair not in expected or pair in seen:
                raise FinancialEvidenceError("financial evidence repeats or invents a security-period pair")
            seen.add(pair)
            status = record.get("status")
            if status not in {"COLLECTED", "NO_RECORD", "FAILED"}:
                raise FinancialEvidenceError("financial evidence status is unsupported")
            counts[status] += 1
            if status == "FAILED":
                if not record.get("reason"):
                    raise FinancialEvidenceError("failed financial query must disclose its reason")
                # Candidate raw data may remain in the failed record for audit;
                # it is never used to construct the admitted cache below.
                continue
            declared = record.get("receipt") or {}
            receipt = con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?",
                                  (declared.get("receipt_id"),)).fetchone()
            if (receipt is None or receipt["source_id"] != "baostock-socket" or
                    receipt["outcome"] != "OK" or receipt["http_status"] is not None or
                    any(declared.get(key) != receipt[key] for key in
                        ("source_id", "url", "content_hash", "byte_size", "requested_at", "responded_at", "first_seen_at"))):
                raise FinancialEvidenceError("financial query receipt differs from its persisted original")
            iid, period = pair
            year, quarter = int(period[:4]), int(period[-1])
            provider_code = iid.lower()
            label = f"profit:{provider_code}:{period}"
            url = urlsplit(receipt["url"])
            query = parse_qs(url.query)
            if (url.scheme != "baostock" or url.hostname != "www.baostock.com" or url.port != 10030 or
                    url.path != "/query_profit_data" or query != {
                        "code": [provider_code], "year": [str(year)], "quarter": [str(quarter)]} or
                    not str(receipt["detail"] or "").startswith(label + "; raw=")):
                raise FinancialEvidenceError("financial receipt does not prove the exact profit query identity")
            requested = _time(receipt["requested_at"], "requested_at")
            responded = _time(receipt["responded_at"], "responded_at")
            observed = _time(receipt["first_seen_at"], "first_seen_at")
            if not start <= requested <= responded == observed <= end:
                raise FinancialEvidenceError("financial receipt actual observation is outside the capture window")
            digest = receipt["content_hash"]
            artifact = con.execute("SELECT * FROM raw_artifact WHERE content_hash=?", (digest,)).fetchone()
            if artifact is None:
                raise FinancialEvidenceError("financial raw response is not archived")
            try:
                stored = resolve_archive_member(archive_root, artifact["stored_path"])
            except ValueError as exc:
                raise FinancialEvidenceError("financial raw response path is outside its archive") from exc
            body = stored.read_bytes()
            if ("sha256:" + hashlib.sha256(body).hexdigest() != digest or
                    len(body) != receipt["byte_size"] or len(body) != artifact["byte_size"]):
                raise FinancialEvidenceError("financial raw response bytes differ from receipt hash/size")
            raw_rows = json.loads(body)
            if not isinstance(raw_rows, list) or len(raw_rows) != declared.get("row_count"):
                raise FinancialEvidenceError("financial raw response row count differs from the receipt")
            receipts.add(receipt["receipt_id"])
            if status == "NO_RECORD":
                if raw_rows or record.get("profitRow"):
                    raise FinancialEvidenceError("NO_RECORD financial query contains values")
                continue
            row = record.get("profitRow")
            if len(raw_rows) != 1 or row != raw_rows[0] or not isinstance(row, dict):
                raise FinancialEvidenceError("financial profit value differs from the exact original response row")
            stat_date = date(year, quarter * 3, {3: 31, 6: 30, 9: 30, 12: 31}[quarter * 3])
            try:
                published = date.fromisoformat(row["pubDate"])
                profit = Decimal(row["netProfit"])
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise FinancialEvidenceError("financial row lacks valid actual publication/profit") from exc
            if (row.get("code") != provider_code or row.get("statDate") != stat_date.isoformat() or
                    not stat_date <= published <= observed.astimezone(SHANGHAI).date() or not profit.is_finite()):
                raise FinancialEvidenceError("financial row identity/period/publication/profit is inconsistent")
            original = derived.setdefault(iid, {}).get(period)
            if original is not None and original != row:
                raise FinancialEvidenceError("new optional cache cannot overwrite an inherited period without a versioned revision path")
            derived[iid][period] = row
    if seen != expected or cache["statements"] != derived:
        raise FinancialEvidenceError("financial cache differs from inherited rows plus verified new profit rows")
    if cache.get("created_at") != base.get("created_at") or _time(cache.get("updated_at"), "updated_at") != end:
        raise FinancialEvidenceError("financial cache inherited creation or actual update timestamp changed")
    provenance = {"schema": SCHEMA, "evidencePath": str(path), "evidenceHash": _hash(path),
                  "cachePath": str(cache_path), "cacheHash": package["cacheHash"],
                  "baseCachePath": str(base_path), "baseCacheHash": package["baseCacheHash"],
                  "archiveDbPath": str(archive_db), "archiveRoot": str(archive_root),
                  "capturedFrom": package["capturedFrom"], "capturedTo": package["capturedTo"],
                  "verifiedReceiptCount": len(receipts), "collectedRecordCount": counts["COLLECTED"],
                  "noRecordCount": counts["NO_RECORD"], "failedRecordCount": counts["FAILED"],
                  "partial": bool(counts["NO_RECORD"] or counts["FAILED"]),
                  "scopeInstrumentIds": targets, "periods": periods,
                  "poolProjectedInstrumentIds": (sorted(set(targets) & set(expected_instrument_ids))
                                                 if expected_instrument_ids is not None else targets),
                  "outsidePublicationPoolCount": (len(set(targets) - set(expected_instrument_ids))
                                                  if expected_instrument_ids is not None else 0),
                  "note": "Verified new optional profit rows; inherited legacy rows keep their original weaker evidence status; not official S2 facts."}
    return {"cache": cache, "provenance": provenance, "receipt_count": len(receipts)}


def freeze_financial_inputs(provenance: dict, *, data_root: str | Path) -> dict:
    """Keep exact package, new cache and inherited base bytes with this snapshot."""
    target_dir = Path(data_root) / "financial_input_origins"
    target_dir.mkdir(parents=True, exist_ok=True)
    frozen = {}
    for role, path_key, hash_key in (("evidence", "evidencePath", "evidenceHash"),
                                    ("cache", "cachePath", "cacheHash"),
                                    ("inherited_base", "baseCachePath", "baseCacheHash")):
        source = resolve_artifact_path(provenance[path_key])
        digest = provenance[hash_key]
        body = source.read_bytes()
        if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
            raise FinancialEvidenceError(f"financial {role} changed after admission")
        destination = target_dir / (digest.removeprefix("sha256:") + ".json")
        if destination.exists():
            if destination.read_bytes() != body:
                raise FinancialEvidenceError(f"frozen financial {role} content conflicts")
        else:
            with destination.open("xb") as stream:
                stream.write(body)
        frozen[role] = {"path": str(destination.resolve()), "contentHash": digest}
    return frozen
