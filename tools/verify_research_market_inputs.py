"""Read-only admission checks against the original research market archives.

BaoStock industry receipts timestamp persisted returned rows, not request start.
Daily bars are current observations of historical data, not historical PIT data.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

CSRC = "证监会行业分类"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _hash(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _time(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid archive timestamp: {value!r}") from exc
    _require(parsed.tzinfo is not None and parsed.utcoffset() is not None,
             "archive timestamp must include timezone")
    return parsed


def _day(value: object) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid source trading date: {value!r}") from exc
    _require(parsed.isoformat() == value, "source dates must use YYYY-MM-DD")
    return parsed.isoformat()


def _file(root: Path, stored: object) -> Path:
    _require(isinstance(stored, str) and bool(stored), "missing stored archive path")
    # BaoStock/ForwardArchive paths were recorded on Windows.
    rel = Path(stored.replace("\\", "/"))
    resolved = (root / rel).resolve()
    _require(not rel.is_absolute() and resolved.is_relative_to(root.resolve()),
             "stored archive path escapes archive root")
    return resolved


def _iid(code: str) -> str | None:
    _require(isinstance(code, str) and re.fullmatch(r"[0-9]{6}", code) is not None,
             "official code must be six ASCII digits")
    if code.startswith(("60", "68")):
        return "SH." + code
    if code.startswith(("00", "30")):
        return "SZ." + code
    return None


def verify_industry(packet: dict, universe: dict[str, str], archive_root: Path) -> None:
    """Bind every classified/missing/duplicate row to the full archived response."""
    _require(packet.get("schema") == "aquant.baostock_research_industry.v1", "industry schema mismatch")
    receipt = packet.get("source_receipt", {})
    _require(receipt.get("label") == "stock_industry:ALL", "industry receipt is not full-market")
    body = _file(Path(archive_root), receipt.get("stored_path")).read_bytes()
    _require(_hash(body) == receipt.get("content_hash"), "industry original SHA256 mismatch")
    raw = json.loads(body)
    _require(isinstance(raw, list) and all(isinstance(r, dict) for r in raw), "industry original is not a row list")
    _require(type(receipt.get("record_count")) is int and receipt["record_count"] == len(raw), "industry receipt record count mismatch")
    _require(packet.get("source_record_count") == len(raw), "industry source record count mismatch")
    _require(packet.get("source_content_hash_verified") is True, "industry hash verification claim missing")
    _require(packet.get("classification_system") == CSRC, "industry classification must be CSRC")
    start, finish = _time(packet.get("captured_from")), _time(receipt.get("requested_at"))
    _require(start <= finish, "industry collector interval reversed")
    for key in ("capture_started_at", "prepared_at"):
        _require(packet.get(key) == packet.get("captured_from"), f"industry {key} differs from collector start")
    for key in ("capture_finished_at", "captured_to"):
        _require(packet.get(key) == receipt.get("requested_at"), f"industry {key} differs from persistence receipt")
    _require(_time(packet.get("generated_at")) >= finish, "industry generation precedes persistence")
    _require(bool(packet.get("capture_time_semantics")) and bool(packet.get("source_receipt_timestamp_semantics")),
             "industry collector/persistence time semantics missing")
    grouped: dict[str, list[dict]] = defaultdict(list)
    unjoined, outside = [], []
    for item in raw:
        source_code = item.get("code")
        match = re.fullmatch(r"(sh|sz)\.([0-9]{6})", source_code) if isinstance(source_code, str) else None
        if not match:
            unjoined.append(item)
            continue
        code = match[2]
        if code not in universe:
            outside.append(item)
            continue
        _require(_iid(code) == source_code.upper(), f"industry raw exchange/code mismatch: {source_code}")
        grouped[code].append(item)
    _require(packet.get("unjoined_source_rows") == unjoined, "industry unjoined rows omitted or altered")
    _require(packet.get("source_rows_outside_official_universe", []) == outside, "industry outside-universe rows omitted or altered")
    rows = packet.get("rows")
    _require(isinstance(rows, list), "industry rows missing")
    seen = set()
    counts = Counter()
    for row in rows:
        _require(isinstance(row, dict), "invalid industry joined row")
        code, ix = row.get("code"), row.get("source_row_index_for_duplicate_code")
        _require(code in universe and code in grouped and type(ix) is int and 0 <= ix < len(grouped[code]),
                 "industry joined identity/index has no original row")
        _require((code, ix) not in seen, "industry duplicate joined row")
        seen.add((code, ix))
        original = grouped[code][ix]
        iid = _iid(code)
        expected = {"instrument_id": iid, "exchange": iid[:2], "org_id": universe[code],
                    "baostock_code_raw": original.get("code"), "code_name_raw": original.get("code_name"),
                    "industry_raw": original.get("industry"), "industry_classification_raw": original.get("industryClassification"),
                    "classification_system": CSRC, "classification_asof_raw": original.get("updateDate"),
                    "duplicate_source_code_row_count": len(grouped[code]),
                    "classification_status": "classified" if isinstance(original.get("industry"), str) and original["industry"].strip() else "empty_industry_value"}
        _require(original.get("industryClassification") == CSRC, "raw industry classification is not CSRC")
        _day(original.get("updateDate"))
        for key, value in expected.items():
            _require(row.get(key) == value, f"industry {code}[{ix}] {key} differs from original identity/field")
        counts[iid[:2]] += 1
    _require(len(seen) == sum(map(len, grouped.values())), "industry original matched rows omitted")
    missing = packet.get("missing_official_codes")
    _require(isinstance(missing, list) and len(missing) == len(set(missing)) and set(missing) == set(universe) - set(grouped),
             "industry missing official codes mismatch")
    _require(packet.get("official_universe_count") == len(universe), "industry universe count mismatch")
    _require(packet.get("matched_official_code_count") == len(grouped), "industry matched code count mismatch")
    _require(packet.get("matched_rows_by_exchange") == dict(counts), "industry exchange coverage mismatch")


def _integer(value: object, scale: int = 100) -> int | None:
    if value is None or value == "":
        return None
    _require(not isinstance(value, bool), "boolean is not a market numeric value")
    try:
        number = Decimal(str(value))
        _require(number.is_finite(), "non-finite source number")
        return int((number * scale).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid source number: {value!r}") from exc


def _history_url(url: str, provider: str, iid: str, start: str, end: str) -> str:
    split = urlsplit(url)
    query = parse_qs(split.query, keep_blank_values=True)
    _require(not split.fragment and not split.username and not split.password, "unexpected history URL components")
    if provider == "baostock-socket":
        _require(split.scheme == "baostock" and split.netloc == "www.baostock.com:10030"
                 and split.path == "/query_history_k_data_plus"
                 and query == {"code": [iid.lower()], "start": [start], "end": [end], "adjustflag": ["3"]},
                 "BaoStock receipt URL identity/window/adjustment mismatch")
        return iid.lower()
    _require(provider == "tencent-ifzq", "unsupported history source")
    symbol = iid.lower().replace(".", "")
    _require(split.scheme == "https" and split.netloc == "web.ifzq.gtimg.cn"
             and split.path == "/appstock/app/fqkline/get"
             and query == {"param": [f"{symbol},day,{start},{end},640,"]},
             "Tencent receipt URL identity/window/adjustment mismatch")
    return symbol


def _history_rows(raw: object, provider: str, symbol: str, start: str, end: str) -> list[dict]:
    if provider == "baostock-socket":
        _require(isinstance(raw, list), "BaoStock raw history is not a row list")
        items = raw
    else:
        _require(isinstance(raw, dict) and isinstance(raw.get("data"), dict)
                 and list(raw["data"]) == [symbol], "Tencent raw security identity mismatch")
        node = raw["data"][symbol]
        _require(isinstance(node, dict) and isinstance(node.get("day"), list), "Tencent unadjusted daily rows missing")
        items = node["day"]
    rows, days = [], set()
    for item in items:
        if provider == "baostock-socket":
            _require(isinstance(item, dict), "malformed BaoStock raw daily row")
            if "code" in item:
                _require(item["code"] == symbol, "BaoStock raw security identity mismatch")
            values = [item.get(k) for k in ("date", "open", "close", "high", "low", "volume", "amount")]
        else:
            _require(isinstance(item, list) and len(item) >= 6, "malformed Tencent raw daily row")
            values = item[:6] + [None]
        day = _day(values[0])
        _require(start <= day <= end, "raw history date outside requested window")
        _require(day not in days, "duplicate raw history trading date")
        days.add(day)
        amount = _integer(values[6])
        row = {"trading_day": day, "open_cents": _integer(values[1]), "close_cents": _integer(values[2]),
               "high_cents": _integer(values[3]), "low_cents": _integer(values[4]),
               "volume_shares": _integer(values[5], 1 if provider == "baostock-socket" else 100),
               "amount_cents": amount, "amount_status": ("source_returned_value" if amount is not None else "source_returned_null")
               if provider == "baostock-socket" else "provider_not_offered"}
        rows.append(row)
    _require(bool(rows), "used history response contains no daily rows")
    return sorted(rows, key=lambda row: row["trading_day"])


def verify_history_refresh(packet: dict, universe: dict[str, str]) -> None:
    """Rebuild every used daily response from read-only ForwardArchive evidence."""
    _require(packet.get("schema") == "aquant.research_daily_history.v1", "history schema mismatch")
    window = packet.get("requested_window", {})
    start, end = _day(window.get("start")), _day(window.get("end"))
    _require(start <= end and window.get("adjustment") == "unadjusted", "history window/adjustment invalid")
    bars = packet.get("bars")
    _require(isinstance(bars, dict) and bool(bars), "history bars missing")
    supplied = packet.get("source_receipts")
    _require(isinstance(supplied, list) and all(isinstance(r, dict) for r in supplied), "history receipts missing")
    by_receipt = {r.get("receipt_id"): r for r in supplied}
    _require(len(by_receipt) == len(supplied), "duplicate history receipt metadata")
    archive = packet.get("source_archive", {})
    db = Path(archive["sqlite"])
    source_input = packet.get("input", {})
    input_path = Path(source_input["path"])
    _require(input_path.resolve() == (db.parent / "input-research-candidates.json").resolve(),
             "history target input must be the fixed archive copy")
    input_bytes = input_path.read_bytes()
    _require(_hash(input_bytes) == source_input.get("content_hash"), "history target input SHA256 mismatch")
    input_doc = json.loads(input_bytes)
    _require(input_doc.get("schema") == "aquant.research-candidates.v2"
             and input_doc.get("researchOnly") is True and input_doc.get("simulationAvailable") is False,
             "history target input schema/read-only scope mismatch")
    candidates = input_doc.get("candidates")
    _require(isinstance(candidates, list) and all(isinstance(c, dict) for c in candidates), "history original target candidates missing")
    target_ids = [c.get("instrumentId") for c in candidates if c.get("status") == "RANKED"]
    _require(all(isinstance(iid, str) for iid in target_ids) and len(set(target_ids)) == len(target_ids),
             "history duplicate/invalid original ranked targets")
    _require(set(bars).issubset(target_ids) and packet.get("expected_ranked_count") == len(target_ids),
             "history bars/coverage differ from original ranked targets")
    # ForwardArchive.stored_path is relative to archive root and includes raw/.
    raw_root = db.parent / "archive"
    _require(Path(archive["tencent_raw_root"]).resolve() == (raw_root / "raw").resolve(), "history raw root differs from ForwardArchive root")
    used, requested, responded, all_days = set(), [], [], []
    counts = Counter()
    with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row

        def load_segment(metadata, provider, iid, segment_start, segment_end, *, full_receipt=False):
            """One response remains one receipt, including a same-day refresh."""
            _require(isinstance(metadata, dict), "history segment receipt metadata missing")
            rid = metadata.get("receipt_id")
            _require(rid not in used and rid in by_receipt, "history receipt missing or reused across securities")
            used.add(rid)
            found = con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?", (rid,)).fetchone()
            _require(found is not None, "used history receipt absent from archive")
            r = dict(found)
            _require(r["outcome"] == "OK" and r["source_id"] == provider, "history receipt outcome/source mismatch")
            _require(r["http_status"] == (200 if provider == "tencent-ifzq" else None), "history receipt HTTP status mismatch")
            symbol = _history_url(r["url"], provider, iid, segment_start, segment_end)
            mapping = {"source_id": "source_id", "source_url": "url", "requested_at": "requested_at",
                       "responded_at": "responded_at", "observed_at": "first_seen_at", "actual_hash": "content_hash",
                       "outcome": "outcome", "http_status": "http_status", "receipt_id": "receipt_id"}
            for key, original_key in mapping.items():
                _require(by_receipt[rid].get(key) == r[original_key], f"history receipt {key} differs from archive")
                if full_receipt or key in ("receipt_id", "source_url", "requested_at", "responded_at", "observed_at", "actual_hash"):
                    _require(metadata.get(key) == r[original_key], f"history segment {key} differs from receipt")
            if "content_hash" in metadata:
                _require(metadata["content_hash"] == r["content_hash"], "history entry content_hash differs from receipt")
            req, resp, seen = (_time(r[k]) for k in ("requested_at", "responded_at", "first_seen_at"))
            _require(req <= resp and seen == resp, "history receipt timestamps invalid")
            artifact = con.execute("SELECT * FROM raw_artifact WHERE content_hash=?", (r["content_hash"],)).fetchone()
            _require(artifact is not None, "history original artifact missing")
            body = _file(raw_root, artifact["stored_path"]).read_bytes()
            _require(_hash(body) == r["content_hash"], "history original SHA256 mismatch")
            _require(type(r["byte_size"]) is int and r["byte_size"] == len(body) == artifact["byte_size"], "history original byte count mismatch")
            rows = _history_rows(json.loads(body), provider, symbol, segment_start, segment_end)
            requested.append(req)
            responded.append(resp)
            return rows

        for iid, entry in bars.items():
            _require(isinstance(iid, str) and re.fullmatch(r"(?:SH|SZ)\.[0-9]{6}", iid) is not None
                     and iid[3:] in universe and _iid(iid[3:]) == iid, "history official security identity mismatch")
            _require(isinstance(entry, dict), "invalid history entry")
            provider = entry.get("source")
            rows = load_segment(entry, provider, iid, start, end)
            capture = entry.get("history_capture")
            if capture is not None or "latest_refresh" in entry:
                _require(isinstance(capture, dict), "incremental history requires original history_capture")
                for key in ("receipt_id", "source_url", "requested_at", "responded_at", "observed_at", "actual_hash"):
                    _require(capture.get(key) == entry.get(key), f"history_capture {key} differs from original receipt")
                if "raw_archive_path" in capture:
                    _require(capture["raw_archive_path"] == entry.get("raw_archive_path"), "history_capture raw path differs from original")
            if "latest_refresh" in entry:
                latest = entry["latest_refresh"]
                _require(isinstance(latest, dict) and latest.get("requested_end") == end
                         and latest.get("status") == "BAR_RETURNED", "history latest refresh day/status mismatch")
                metadata = latest.get("receipt")
                delta = load_segment(metadata, provider, iid, end, end, full_receipt=True)
                _require(len(delta) == 1 and delta[0]["trading_day"] == end,
                         "history latest refresh must contain exactly the requested end day")
                _require(_time(metadata["requested_at"]) >= _time(entry["responded_at"]), "latest refresh precedes original capture")
                delta[0].update(source_receipt_id=metadata["receipt_id"], source_actual_hash=metadata["actual_hash"],
                                source_observed_at=metadata["observed_at"], source_url=metadata["source_url"])
                combined = {row["trading_day"]: row for row in rows}
                combined[end] = delta[0]
                rows = [combined[day] for day in sorted(combined)]
            actual_rows = entry.get("rows")
            _require(isinstance(actual_rows, list) and len(actual_rows) == len(rows), "history rows omitted or added")
            for actual, expected in zip(actual_rows, rows):
                _require(isinstance(actual, dict), "invalid normalized history row")
                for key, value in expected.items():
                    _require(key in actual and actual[key] == value and (value is None or not isinstance(value, int) or type(actual[key]) is int),
                             f"history {iid} {expected['trading_day']} {key} differs from original units/value")
                # A base row cannot claim that it came from the incremental receipt.
                for key in ("source_receipt_id", "source_actual_hash", "source_observed_at", "source_url"):
                    _require(key not in actual or key in expected,
                             f"history {iid} base row has unsupported incremental source metadata")
            all_days.extend(row["trading_day"] for row in rows)
            counts[provider] += 1
    _require(used == set(by_receipt), "history unused receipt alters observation interval")
    _require(_time(packet.get("captured_from")) == min(requested) and _time(packet.get("captured_to")) == max(responded), "history capture bounds differ from used receipts")
    _require(packet.get("data_asof") == max(all_days), "history data_asof differs from original rows")
    expected = packet.get("expected_ranked_count")
    _require(type(expected) is int and len(bars) <= expected <= len(universe), "history expected ranked count invalid")
    coverage = {"ranked_count": expected, "success_count": len(bars), "missing_count": expected - len(bars),
                "baostock_count": counts["baostock-socket"], "tencent_fallback_count": counts["tencent-ifzq"]}
    if "bar_count" in packet.get("coverage", {}):
        coverage["bar_count"] = len(all_days)
    _require(packet.get("coverage") == coverage, "history coverage differs from used responses")
