"""Capture a resumable intraday Tencent quote research archive.

This is deliberately separate from the closing-market-cap sidecar path. It
records only values actually present in Tencent's quote payload and keeps the
raw response, URL and fetch receipt in ForwardArchive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.resilience import CircuitBreaker, RateLimiter  # noqa: E402
from aquant.adapters.providers.tencent import TencentClient  # noqa: E402
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402

UNIVERSE = ROOT / "data" / "s2-structured" / "universe.txt"
ORG_MAP = ROOT / "data" / "s2-structured" / "universe-orgids.json"
ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"
OUTPUT = ROOT / "data" / "s2-structured" / "research-quotes-20261009.json"
CHECKPOINT = ROOT / "data" / "s2-structured" / "research-quotes-20261009.checkpoint.jsonl"
COVERAGE = ROOT / "data" / "s2-structured" / "research-quotes-20261009.coverage.json"
QUOTE_URL = "https://qt.gtimg.cn/q="
ROW_RE = re.compile(r'v_((?:sh|sz|bj)\d{6})="([^\"]*)";')
BATCH_SIZE = 60


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def map_code(code: str) -> tuple[str, str, str, str]:
    """Map the official six-digit code using documented exchange code families."""
    if code.startswith("60"):
        return "SH", "sh" + code, "SH_MAIN", "SH.60 code pattern"
    if code.startswith("68"):
        return "SH", "sh" + code, "STAR", "SH.68 code pattern"
    if code.startswith(("00", "30")):
        board = "SZ_MAIN" if code.startswith("00") else "GEM"
        return "SZ", "sz" + code, board, f"SZ.{code[:2]} code pattern"
    if code.startswith(("4", "8", "92")):
        return "BJ", "bj" + code, "BSE", "BJ.4/8/92 code pattern"
    raise ValueError("unrecognized exchange code pattern")


def prepare() -> tuple[list[dict], list[dict], dict]:
    codes = UNIVERSE.read_text(encoding="utf-8-sig").split()
    org_doc = json.loads(ORG_MAP.read_text(encoding="utf-8-sig"))
    org_ids = org_doc["organization_ids"]
    if len(codes) != len(set(codes)) or set(codes) != set(org_ids):
        raise ValueError("universe.txt does not exactly match verified official organization-map codes")
    supported: list[dict] = []
    excluded: list[dict] = []
    seen: set[str] = set()
    for code in codes:
        if code in seen:
            excluded.append({"official_code": code, "reason": "duplicate_official_code"})
            continue
        seen.add(code)
        try:
            if not re.fullmatch(r"[0-9]{6}", code):
                raise ValueError("not_six_digit_ascii")
            exchange, symbol, board, mapping_basis = map_code(code)
        except ValueError:
            excluded.append({"official_code": code, "reason": "unrecognized_exchange_code_pattern"})
            continue
        supported.append({
            "official_code": code,
            "instrument_id": f"{exchange}.{code}",
            "exchange": exchange,
            "board": board,
            "exchange_mapping_basis": mapping_basis,
            "organization_id": org_ids.get(code),
            "symbol": symbol,
        })
    return supported, excluded, org_doc


def parse_payload(payload: bytes, batch: list[dict]) -> tuple[list[dict], list[dict]]:
    expected = {x["symbol"]: x for x in batch}
    seen: set[str] = set()
    observed: list[dict] = []
    excluded: list[dict] = []
    for match in ROW_RE.finditer(payload.decode("gbk", "replace")):
        symbol, body = match.groups()
        fields = body.split("~")
        if symbol not in expected:
            excluded.append({"symbol": symbol, "reason": "unexpected_symbol_in_response"})
            continue
        if symbol in seen:
            observed[:] = [row for row in observed if row["symbol"] != symbol]
            excluded.append({"symbol": symbol, "reason": "duplicate_symbol_in_response"})
            continue
        seen.add(symbol)
        identity = expected[symbol]
        if len(fields) <= 45:
            excluded.append({**identity, "reason": "response_missing_required_fields"})
            continue
        if fields[2].strip() != identity["official_code"]:
            excluded.append({**identity, "response_code": fields[2].strip(), "reason": "response_identity_mismatch"})
            continue
        row = {
            **identity,
            "quote_name": fields[1],
            "quote_time_raw": fields[30],
            "last_price_raw": fields[3],
            "previous_close_raw": fields[4],
            "volume_raw": fields[6],
            "turnover_amount_raw": fields[37],
            "total_market_cap_raw": fields[45],
        }
        observed.append(row)
    for symbol, identity in expected.items():
        if symbol not in seen:
            excluded.append({**identity, "reason": "missing_response"})
    observed.sort(key=lambda x: x["official_code"])
    return observed, excluded


def annotate_batch_freshness(rows: list[dict]) -> None:
    """Compare quote timestamps within their actual response batch."""
    parsed: dict[str, datetime] = {}
    for row in rows:
        try:
            parsed[row["symbol"]] = datetime.strptime(row["quote_time_raw"], "%Y%m%d%H%M%S")
        except ValueError:
            continue
    latest = max(parsed.values()) if parsed else None
    for row in rows:
        stamp = parsed.get(row["symbol"])
        lag = int((latest - stamp).total_seconds()) if latest and stamp else None
        row["quote_time_lag_seconds_from_batch_latest"] = lag
        row["stale_against_batch_latest"] = lag is None or lag > 300


def annotate_close_validation(rows: list[dict], market_day: str) -> None:
    """Keep each observed row and tag strict same-day >=15:00 evidence."""
    for row in rows:
        raw = row.get("quote_time_raw", "")
        try:
            stamp = datetime.strptime(raw, "%Y%m%d%H%M%S")
            day = stamp.strftime("%Y-%m-%d")
            reason = None if day == market_day and stamp.strftime("%H:%M:%S") >= "15:00:00" else (
                "wrong_market_day" if day != market_day else "before_market_close")
            row["quote_time_parsed"] = stamp.isoformat()
        except (TypeError, ValueError):
            reason = "invalid_quote_time"
            row["quote_time_parsed"] = None
        row["close_time_eligible"] = reason is None
        row["close_time_exclusion_reason"] = reason


def load_checkpoint(path: Path, supported_by_symbol: dict[str, dict], con,
                    archive: ForwardArchive) -> tuple[dict[str, dict], list[dict]]:
    valid: dict[str, dict] = {}
    entries: list[dict] = []
    if not path.exists():
        return valid, entries
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        symbols = entry.get("requested_symbols", [])
        if (entry.get("status") != "ok" or not symbols
                or any(symbol not in supported_by_symbol for symbol in symbols)):
            continue
        receipt = con.execute(
            "SELECT source_id,url,outcome,http_status,content_hash,byte_size FROM fetch_receipt WHERE receipt_id=?",
            (entry.get("receipt_id"),),
        ).fetchone()
        expected_url = QUOTE_URL + ",".join(symbols)
        if (receipt and receipt["source_id"] == "tencent-qt" and receipt["url"] == expected_url
                and receipt["outcome"] == "OK" and receipt["http_status"] == 200
                and receipt["content_hash"] == entry.get("content_hash")
                and receipt["byte_size"] == entry.get("byte_size")
                and archive.verify(receipt["content_hash"])):
            for symbol in symbols:
                valid[symbol] = entry
            entries.append(entry)
    # A later success line for the same symbol supersedes an earlier checkpoint.
    dedup: dict[str, dict] = {}
    for entry in entries:
        for symbol in entry["requested_symbols"]:
            dedup[symbol] = entry
    unique_entries = list({id(e): e for e in dedup.values()}.values())
    return valid, unique_entries


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    ap.add_argument("--output", type=Path, default=OUTPUT)
    ap.add_argument("--checkpoint", type=Path, default=CHECKPOINT)
    ap.add_argument("--coverage", type=Path, default=COVERAGE)
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    ap.add_argument("--limit", type=int, help="small initial sample; omitted means full supported scope")
    ap.add_argument("--exchange", choices=("SH", "SZ", "BJ"), help="limit a sample to one exchange")
    ap.add_argument("--trusted-proxy-network", action="append", default=[])
    ap.add_argument("--close-date", help="tag strict after-close evidence for Beijing market date YYYY-MM-DD")
    args = ap.parse_args()
    if not 1 <= args.batch_size <= 60:
        raise SystemExit("--batch-size must be 1..60")
    if args.close_date:
        try:
            datetime.strptime(args.close_date, "%Y-%m-%d")
        except ValueError as exc:
            raise SystemExit("--close-date must be YYYY-MM-DD") from exc
    prepared_at = now_utc()
    capture_started_at = prepared_at
    supported, out_of_scope, org_doc = prepare()
    if args.exchange:
        supported = [x for x in supported if x["exchange"] == args.exchange]
    selected = supported[:args.limit] if args.limit else supported
    batches: list[list[dict]] = []
    if args.trusted_proxy_network:
        os.environ["AQUANT_TRUSTED_PROXY_NETWORKS"] = ",".join(args.trusted_proxy_network)
    args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}
    receipts: list[dict] = []
    failures_in_a_row = 0
    stopped_on_breaker = False
    with closing(connect(args.archive_root / "meta.sqlite")) as con:
        archive = ForwardArchive(con, args.archive_root)
        map_receipt = con.execute(
            "SELECT source_id,url,outcome,http_status,content_hash FROM fetch_receipt WHERE receipt_id=?",
            (org_doc.get("receipt_id"),),
        ).fetchone()
        if not (map_receipt and map_receipt["source_id"] == "cninfo"
                and map_receipt["url"] == org_doc.get("source_url")
                and map_receipt["outcome"] == "OK" and map_receipt["http_status"] == 200
                and map_receipt["content_hash"] == org_doc.get("content_hash")
                and archive.verify(map_receipt["content_hash"])):
            raise SystemExit("official universe organization map receipt/hash is not verifiable")
        if any(not x.get("organization_id") for x in supported):
            raise SystemExit("official universe organization map is missing an organization ID")
        selected_by_symbol = {x["symbol"]: x for x in selected}
        valid_by_symbol, reusable_entries = load_checkpoint(args.checkpoint, selected_by_symbol, con, archive)
        reusable_entries = [e for e in reusable_entries
                            if all(s in selected_by_symbol for s in e["requested_symbols"])]
        pending = [x for x in selected if x["symbol"] not in valid_by_symbol]
        batches = [pending[i:i + args.batch_size] for i in range(0, len(pending), args.batch_size)]
        receipts = [{**e, "reused_from_verified_checkpoint": True} for e in reusable_entries]
        for entry in receipts:
            payload = archive.load_bytes(entry["content_hash"])
            batch = [selected_by_symbol[s] for s in entry["requested_symbols"]]
            rows, excluded = parse_payload(payload, batch)
            annotate_batch_freshness(rows)
            if args.close_date:
                annotate_close_validation(rows, args.close_date)
            results[entry["batch_key"]] = {"rows": rows, "exclusions": excluded}
        client = TencentClient(
            archive,
            limiter=RateLimiter(min_interval_seconds=1.5),
            breaker=CircuitBreaker(failure_threshold=1000, cooldown_seconds=120),
        )
        with args.checkpoint.open("a", encoding="utf-8", newline="\n") as cp:
            for ix, batch in enumerate(batches, 1):
                batch_key = hashlib.sha256(",".join(x["symbol"] for x in batch).encode()).hexdigest()
                url = QUOTE_URL + ",".join(x["symbol"] for x in batch)
                label = "after-close-research-quotes" if args.close_date else "intraday-research-quotes"
                out = client.fetch(url, label=f"{label}:{len(batch)}")
                receipt = con.execute(
                    "SELECT receipt_id,url,requested_at,responded_at,first_seen_at,outcome,http_status,"
                    "content_hash,byte_size,detail FROM fetch_receipt WHERE receipt_id=?", (out.receipt_id,),
                ).fetchone() if out.receipt_id else None
                status = "ok" if (out.ok and out.payload is not None and out.content_hash and receipt) else "failed"
                # Verify every successful response against the actual receipt and archived bytes.
                if status == "ok":
                    status = "ok" if (receipt["url"] == url and receipt["outcome"] == "OK"
                        and receipt["http_status"] == 200 and receipt["content_hash"] == out.content_hash
                        and receipt["byte_size"] == len(out.payload) and archive.verify(out.content_hash)) else "failed"
                payload = out.payload if status == "ok" else b""
                entry = {
                    "batch_key": batch_key, "batch": ix, "requested_symbols": [x["symbol"] for x in batch],
                    "requested_count": len(batch), "status": status, "capture_mode": "network_request",
                    "receipt_id": out.receipt_id, "url": url,
                    "requested_at": receipt["requested_at"] if receipt else None,
                    "responded_at": receipt["responded_at"] if receipt else None,
                    "first_seen_at": receipt["first_seen_at"] if receipt else None,
                    "content_hash": receipt["content_hash"] if receipt else None,
                    "byte_size": receipt["byte_size"] if receipt else None,
                    "http_status": receipt["http_status"] if receipt else out.http_status,
                    "outcome": receipt["outcome"] if receipt else None,
                    "detail": receipt["detail"] if receipt else out.detail,
                }
                cp.write(json.dumps(entry, ensure_ascii=False) + "\n")
                cp.flush()
                if status == "ok":
                    rows, excluded = parse_payload(payload, batch)
                    annotate_batch_freshness(rows)
                    if args.close_date:
                        annotate_close_validation(rows, args.close_date)
                    results[batch_key] = {"rows": rows, "exclusions": excluded}
                    failures_in_a_row = 0
                    print(f"{ix}/{len(batches)} OK {len(rows)}/{len(batch)} rows", flush=True)
                else:
                    failures_in_a_row += 1
                    entry["failure_streak"] = failures_in_a_row
                    cp.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    cp.flush()
                    results[batch_key] = {"rows": [], "exclusions": [{**x, "reason": "request_failed"} for x in batch]}
                    print(f"{ix}/{len(batches)} FAILED streak={failures_in_a_row}", flush=True)
                receipts.append(entry)
                if failures_in_a_row >= 4:
                    stopped_on_breaker = True
                    break
    actual_starts = [x["requested_at"] for x in receipts if x.get("requested_at")]
    actual_finishes = [x["responded_at"] for x in receipts if x.get("responded_at")]
    if actual_starts:
        capture_started_at = min(actual_starts)
    capture_finished_at = max(actual_finishes) if actual_finishes else now_utc()
    quote_rows: list[dict] = []
    exclusions = list(out_of_scope)
    for entry in receipts:
        block = results.get(entry["batch_key"], {"rows": [], "exclusions": []})
        for row in block["rows"]:
            quote_rows.append({**row, "batch": entry["batch"], "batch_key": entry["batch_key"], "receipt_id": entry["receipt_id"],
                               "request_url": entry["url"], "content_hash": entry["content_hash"],
                               "requested_at": entry["requested_at"], "responded_at": entry["responded_at"],
                               "first_seen_at": entry["first_seen_at"]})
        exclusions.extend(block["exclusions"])
    quote_rows.sort(key=lambda x: x["official_code"])
    covered = len(quote_rows)
    org_codes: dict[str, list[str]] = {}
    for item in supported:
        if item.get("organization_id"):
            org_codes.setdefault(item["organization_id"], []).append(item["official_code"])
    shared_org_groups = [
        {"organization_id": org_id, "official_codes": sorted(codes)}
        for org_id, codes in sorted(org_codes.items()) if len(codes) > 1
    ]
    requested_by_exchange = Counter(x["exchange"] for x in selected)
    observed_by_exchange = Counter(x["exchange"] for x in quote_rows)
    missing_by_exchange = Counter(
        x["exchange"] for x in exclusions if x.get("reason") == "missing_response"
    )
    capture_kind = "after_close_research_snapshot" if args.close_date else "intraday_research_snapshot"
    no_active = [x for x in quote_rows if x.get("volume_raw", "").strip() in {"", "0", "0.0"}
                 or x.get("turnover_amount_raw", "").strip() in {"", "0", "0.0"}]
    missing_cap = [x for x in quote_rows if x.get("total_market_cap_raw", "").strip() in {"", "0", "0.0"}]
    close_ineligible = [x for x in quote_rows if args.close_date and not x.get("close_time_eligible")]
    coverage = {
        "schema": "aquant.tencent_research_quote_coverage.v1",
        "generated_at": now_utc(), "capture_started_at": capture_started_at,
        "capture_finished_at": capture_finished_at, "quote_time_kind": capture_kind,
        "market_date": args.close_date,
        "does_not_represent_closing_sidecar": True,
        "does_not_represent_complete_market_close": True,
        "organization_identity_policy": "preserve each official code as a separate quote row; shared orgId is metadata only and never triggers code substitution or merging",
        "shared_org_id_code_group_count": len(shared_org_groups),
        "shared_org_id_code_groups": shared_org_groups,
        "supported_scope": "official universe entries mapped by documented code patterns: SH 60/68, SZ 00/30, BJ 4/8/92",
        "official_universe_count": len(supported) + len(out_of_scope),
        "official_supported_count": len(supported), "selected_count": len(selected),
        "captured_count": covered, "excluded_count": len(exclusions),
        "requested_by_exchange": dict(sorted(requested_by_exchange.items())),
        "observed_by_exchange": dict(sorted(observed_by_exchange.items())),
        "missing_by_exchange": dict(sorted(missing_by_exchange.items())),
        "quote_time_older_than_300s_from_batch_latest_count": sum(1 for x in quote_rows
                                                                    if x.get("quote_time_lag_seconds_from_batch_latest") is not None
                                                                    and x["quote_time_lag_seconds_from_batch_latest"] > 300),
        "invalid_quote_time_count": sum(1 for x in quote_rows if x.get("quote_time_lag_seconds_from_batch_latest") is None),
        "close_time_eligible_count": sum(1 for x in quote_rows if x.get("close_time_eligible")) if args.close_date else None,
        "close_time_ineligible_count": len(close_ineligible) if args.close_date else None,
        "close_time_ineligible_codes": [{"code": x["official_code"], "quote_time_raw": x.get("quote_time_raw"),
                                         "reason": x.get("close_time_exclusion_reason")} for x in close_ineligible],
        "zero_or_missing_volume_or_amount_count": len(no_active),
        "zero_or_missing_volume_or_amount_codes": [x["official_code"] for x in no_active],
        "missing_or_zero_market_cap_count": len(missing_cap),
        "missing_or_zero_market_cap_codes": [x["official_code"] for x in missing_cap],
        "stale_quote_rows_over_300s": [
            {"instrument_id": x["instrument_id"], "official_code": x["official_code"],
             "quote_name": x["quote_name"], "quote_time_raw": x["quote_time_raw"],
             "quote_time_lag_seconds_from_batch_latest": x["quote_time_lag_seconds_from_batch_latest"]}
            for x in quote_rows if x.get("quote_time_lag_seconds_from_batch_latest") is not None
            and x["quote_time_lag_seconds_from_batch_latest"] > 300
        ],
        "network_batch_count": sum(1 for x in receipts if x["capture_mode"] == "network_request"),
        "batch_size": args.batch_size, "consecutive_failure_breaker_limit": 4,
        "stopped_on_breaker": stopped_on_breaker,
        "unmapped_count": len(out_of_scope), "unmapped_codes": out_of_scope,
        "batches": receipts,
        "excluded_quote_rows": exclusions,
        "provider_risk_status_field": "no dedicated risk-status field captured; exact provider quote name retained for visible ST/PT/退 name markers",
        "official_org_map_receipt_id": org_doc.get("receipt_id"),
        "official_org_map_content_hash": org_doc.get("content_hash"),
    }
    artifact = {
        "schema": "aquant.tencent_research_quotes.v1",
        "quote_time_kind": capture_kind,
        "market_date": args.close_date,
        "generated_at": now_utc(), "prepared_at": prepared_at,
        "capture_started_at": capture_started_at,
        "capture_finished_at": capture_finished_at, "coverage_report": str(args.coverage.resolve()),
        "does_not_represent_closing_sidecar": True,
        "does_not_represent_complete_market_close": True,
        "field_map": {"quote_time_raw": 30, "last_price_raw": 3, "previous_close_raw": 4,
                      "volume_raw": 6, "turnover_amount_raw": 37, "total_market_cap_raw": 45,
                      "raw_values_only": True},
        "official_universe_org_map_receipt_id": org_doc.get("receipt_id"),
        "official_universe_org_map_content_hash": org_doc.get("content_hash"),
        "rows": [{
            **row,
            "code": row["official_code"],
            "display_name": row["quote_name"],
            "org_id": row["organization_id"],
            "source_url": row["request_url"],
        } for row in quote_rows],
    }
    json_write(args.output, artifact)
    json_write(args.coverage, coverage)
    print(f"captured {covered}/{len(selected)} selected; excluded scope={len(out_of_scope)}; "
          f"output={args.output}; coverage={args.coverage}", flush=True)
    return 0 if covered else 2


if __name__ == "__main__":
    raise SystemExit(main())
