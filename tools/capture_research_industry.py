"""Capture one full BaoStock industry snapshot for independent research."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.baostock import BaostockClient, CSRC_CLASSIFICATION  # noqa: E402

UNIVERSE = ROOT / "data/s2-structured/universe.txt"
ORG_MAP = ROOT / "data/s2-structured/universe-orgids.json"
ARCHIVE_ROOT = ROOT / "deploy/agentctl-q0/forward-archive"
OUTPUT = ROOT / "data/s2-structured/research-industry-20261009.json"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def code_for_baostock(raw: str) -> str | None:
    if not isinstance(raw, str) or len(raw) != 9 or raw[2] != ".":
        return None
    exchange, code = raw[:2].upper(), raw[3:]
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return None
    if exchange in {"SH", "SZ"}:
        return code
    return None


def main() -> int:
    started = now_utc()
    codes = UNIVERSE.read_text(encoding="utf-8-sig").split()
    org_doc = json.loads(ORG_MAP.read_text(encoding="utf-8-sig"))
    if set(codes) != set(org_doc["organization_ids"]) or len(codes) != len(set(codes)):
        raise SystemExit("official code map does not exactly match the universe")
    print("Starting one BaoStock full stock_industry query (45s socket timeout).", flush=True)
    with BaostockClient(ARCHIVE_ROOT, min_interval_seconds=0.0, query_timeout=45.0) as client:
        print("BaoStock login succeeded; querying full snapshot.", flush=True)
        source_rows, receipt = client.stock_industry()
        print(f"BaoStock returned {len(source_rows)} rows.", flush=True)
    code_rows: dict[str, list[dict]] = {}
    unjoined: list[dict] = []
    for item in source_rows:
        code = code_for_baostock(item.get("code"))
        if code is None:
            unjoined.append(item)
            continue
        code_rows.setdefault(code, []).append(item)
    quote_doc = json.loads((ROOT / "data/s2-structured/research-quotes-20261009.json").read_text(encoding="utf-8"))
    quote_by_code = {r["official_code"]: r for r in quote_doc.get("rows", [])}
    rows = []
    for code in codes:
        matches = code_rows.get(code, [])
        for ix, raw in enumerate(matches):
            quote = quote_by_code.get(code)
            industry = raw.get("industry")
            if code.startswith(("60", "68")):
                exchange, iid = "SH", "SH." + code
            elif code.startswith(("00", "30")):
                exchange, iid = "SZ", "SZ." + code
            else:
                exchange, iid = "BJ", None
            rows.append({
                "code": code, "instrument_id": iid,
                "org_id": org_doc["organization_ids"][code], "exchange": exchange,
                "quote_observed_intraday": quote is not None,
                "quote_time_intraday_raw": quote.get("quote_time_raw") if quote else None,
                "baostock_code_raw": raw.get("code"),
                "code_name_raw": raw.get("code_name"),
                "industry_raw": industry,
                "industry_classification_raw": raw.get("industryClassification"),
                "classification_system": CSRC_CLASSIFICATION,
                "classification_asof_raw": raw.get("updateDate"),
                "source_row_index_for_duplicate_code": ix,
                "duplicate_source_code_row_count": len(matches),
                "classification_status": "classified" if isinstance(industry, str) and industry.strip() else "empty_industry_value",
            })
    covered_codes = {r["code"] for r in rows}
    missing = [code for code in codes if code not in covered_codes]
    counts = Counter(r["exchange"] for r in rows)
    receipt_dict = receipt.as_dict()
    archived_bytes = (ARCHIVE_ROOT / receipt.stored_path).read_bytes()
    if "sha256:" + hashlib.sha256(archived_bytes).hexdigest() != receipt.content_hash:
        raise SystemExit("BaoStock persisted source bytes failed SHA-256 verification")
    if json.loads(archived_bytes.decode("utf-8")) != source_rows:
        raise SystemExit("BaoStock persisted source rows differ from returned rows")
    ended = now_utc()
    artifact = {
        "schema": "aquant.baostock_research_industry.v1",
        "prepared_at": started,
        "capture_started_at": started,
        "capture_finished_at": receipt_dict["requested_at"],
        "captured_from": started,
        "captured_to": receipt_dict["requested_at"],
        "generated_at": ended,
        "capture_time_semantics": (
            "captured_from/capture_started_at mark this collector's start before login/query; "
            "captured_to/capture_finished_at are the BaoStock receipt.requested_at timestamp, "
            "which BaostockClient assigns while persisting returned rows; this is the collector "
            "scope and not a precise network request interval. The original source_receipt value is preserved."
        ),
        "source_receipt_timestamp_semantics": (
            "requested_at is assigned by BaostockClient._record after query rows have returned, "
            "at persisted-row receipt creation; it is not the network request start."
        ),
        "classification_system": CSRC_CLASSIFICATION,
        "classification_asof_basis": "source updateDate on each raw BaoStock row; no host-date substitution",
        "source": "BaoStock query_stock_industry full-market query",
        "source_receipt": receipt_dict, "source_content_hash_verified": True,
        "official_universe_count": len(codes), "source_record_count": len(source_rows),
        "matched_official_code_count": len(covered_codes),
        "matched_rows_by_exchange": dict(sorted(counts.items())),
        "missing_official_codes": missing, "unjoined_source_rows": unjoined,
        "identity_policy": "only exact SH/SZ code identity joins; no orgId substitution, no cross-classification inference; BJ stays unmatched when exact code is absent",
        "rows": rows,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "source_record_count": len(source_rows),
                      "matched_official_code_count": len(covered_codes),
                      "matched_rows_by_exchange": dict(sorted(counts.items())),
                      "missing_count": len(missing), "receipt": receipt_dict}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
