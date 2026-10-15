"""Capture Eastmoney's explicitly distinct f100 industry label for BaoStock gaps."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import argparse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.eastmoney import EastmoneyClient  # noqa: E402
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402

ARCHIVE_ROOT = ROOT / "deploy/agentctl-q0/forward-archive"
INDUSTRY_PATH = ROOT / "data/s2-structured/research-industry-20261009.json"
QUOTES_PATH = ROOT / "data/s2-structured/research-quotes-close-20261009.json"
UNIVERSE = ROOT / "data/s2-structured/universe.txt"
ORG_MAP = ROOT / "data/s2-structured/universe-orgids.json"
CHECKPOINT = ROOT / "data/s2-structured/research-industry-eastmoney-20261009.checkpoint.jsonl"
OUTPUT = ROOT / "data/s2-structured/research-industry-eastmoney-20261009.json"


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_page(payload: bytes) -> tuple[list[dict], int]:
    doc = json.loads(payload)
    data = doc.get("data") or {}
    rows = data.get("diff") or []
    return rows, int(data.get("total") or 0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--offline", action="store_true", help="use only same-day hash-verified archived pages")
    args = ap.parse_args()
    started = utc()
    codes = UNIVERSE.read_text(encoding="utf-8-sig").split()
    org_doc = json.loads(ORG_MAP.read_text(encoding="utf-8-sig"))
    if set(codes) != set(org_doc["organization_ids"]):
        raise SystemExit("official universe/orgId mapping mismatch")
    codes_by_org: dict[str, list[str]] = {}
    for official_code, organization_id in org_doc["organization_ids"].items():
        codes_by_org.setdefault(organization_id, []).append(official_code)
    industry = json.loads(INDUSTRY_PATH.read_text(encoding="utf-8"))
    classified = {r["code"] for r in industry["rows"]
                  if r.get("classification_status") == "classified"}
    quotes = json.loads(QUOTES_PATH.read_text(encoding="utf-8"))
    targets = {
        r["official_code"] for r in quotes["rows"]
        if r["official_code"] not in classified and r.get("close_time_eligible")
        and r.get("volume_raw", "").strip() not in {"", "0", "0.0"}
        and r.get("turnover_amount_raw", "").strip() not in {"", "0", "0.0"}
    }
    with connect(ARCHIVE_ROOT / "meta.sqlite") as con:
        archive = ForwardArchive(con, ARCHIVE_ROOT)
        client = EastmoneyClient(archive)
        saved: dict[int, dict] = {}
        if CHECKPOINT.exists():
            for line in CHECKPOINT.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("status") != "ok":
                    continue
                rec = con.execute(
                    "SELECT source_id,url,outcome,http_status,content_hash,byte_size,requested_at,responded_at "
                    "FROM fetch_receipt WHERE receipt_id=?", (entry.get("receipt_id"),)).fetchone()
                expected_url = ("https://push2.eastmoney.com/api/qt/clist/get?"
                    f"pn={entry['page']}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12"
                    "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
                    "&fields=f12,f13,f14,f100,f26,f20")
                if (rec and rec["source_id"] == "eastmoney-direct" and rec["url"] == expected_url
                        and rec["outcome"] == "OK" and rec["http_status"] == 200
                        and rec["content_hash"] == entry.get("content_hash")
                        and rec["byte_size"] == entry.get("byte_size")
                        and archive.verify(rec["content_hash"])):
                    saved[entry["page"]] = {**entry, "requested_at": rec["requested_at"],
                                              "responded_at": rec["responded_at"],
                                              "reused_from_verified_checkpoint": True}
        # Reuse any same-day verified page-1 probe, even if its request preceded
        # this collector checkpoint. This preserves evidence without re-requesting it.
        if 1 not in saved:
            expected_url = ("https://push2.eastmoney.com/api/qt/clist/get?"
                "pn=1&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12"
                "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
                "&fields=f12,f13,f14,f100,f26,f20")
            rec = con.execute(
                "SELECT receipt_id,url,outcome,http_status,content_hash,byte_size,requested_at,responded_at "
                "FROM fetch_receipt WHERE source_id='eastmoney-direct' AND outcome='OK' "
                "AND url=? AND requested_at LIKE '2026-10-09%' ORDER BY requested_at DESC LIMIT 1",
                (expected_url,)).fetchone()
            if rec and rec["http_status"] == 200 and archive.verify(rec["content_hash"]):
                payload = archive.load_bytes(rec["content_hash"])
                page_rows, page_total = parse_page(payload)
                saved[1] = {"page": 1, "status": "ok", "receipt_id": rec["receipt_id"],
                    "url": rec["url"], "requested_at": rec["requested_at"],
                    "responded_at": rec["responded_at"], "content_hash": rec["content_hash"],
                    "byte_size": rec["byte_size"], "http_status": rec["http_status"],
                    "total": page_total, "row_count": len(page_rows),
                    "reused_from_verified_same_day_receipt": True}
        pages: list[dict] = []
        total = None
        # The first query determines the provider-declared active catalog size. Continue
        # only through those pages; every successful response remains in ForwardArchive.
        first_page, last_page = 1, max(saved) if saved else 1
        breaker_streak = 0
        CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
        with CHECKPOINT.open("a", encoding="utf-8", newline="\n") as cp:
            page = first_page
            while True:
                if page in saved:
                    entry = saved[page]
                    payload = archive.load_bytes(entry["content_hash"])
                    page_rows, page_total = parse_page(payload)
                    pages.append({**entry, "rows": page_rows})
                    total = page_total
                else:
                    if args.offline:
                        break
                    outcome, page_rows, page_total = client.universe_page(page=page, page_size=100)
                    receipt = con.execute(
                        "SELECT url,requested_at,responded_at,outcome,http_status,content_hash,byte_size "
                        "FROM fetch_receipt WHERE receipt_id=?", (outcome.receipt_id,)).fetchone()
                    expected_url = ("https://push2.eastmoney.com/api/qt/clist/get?"
                        f"pn={page}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12"
                        "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
                        "&fields=f12,f13,f14,f100,f26,f20")
                    ok = bool(outcome.ok and outcome.payload and receipt
                        and receipt["url"] == expected_url and receipt["outcome"] == "OK"
                        and receipt["http_status"] == 200 and receipt["content_hash"] == outcome.content_hash
                        and receipt["byte_size"] == len(outcome.payload)
                        and archive.verify(outcome.content_hash))
                    entry = {"page": page, "status": "ok" if ok else "failed",
                        "receipt_id": outcome.receipt_id, "url": expected_url,
                        "requested_at": receipt["requested_at"] if receipt else None,
                        "responded_at": receipt["responded_at"] if receipt else None,
                        "content_hash": receipt["content_hash"] if receipt else outcome.content_hash,
                        "byte_size": receipt["byte_size"] if receipt else None,
                        "http_status": receipt["http_status"] if receipt else None,
                        "total": page_total, "row_count": len(page_rows)}
                    cp.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    cp.flush()
                    if not ok:
                        breaker_streak += 1
                        print(f"page {page} failed, streak={breaker_streak}", flush=True)
                        if breaker_streak >= 3:
                            break
                        page += 1
                        continue
                    breaker_streak = 0
                    entry["rows"] = page_rows
                    pages.append(entry)
                    total = page_total
                    print(f"page {page}: {len(page_rows)} rows, total={total}", flush=True)
                if total is None or page * 100 >= total:
                    break
                page += 1

        observed: dict[str, list[dict]] = {}
        for item in pages:
            for raw in item["rows"]:
                code = raw.get("f12")
                if code in targets:
                    observed.setdefault(code, []).append({
                        "official_code": code,
                        "org_id": org_doc["organization_ids"][code],
                        "same_org_id_official_codes": sorted(codes_by_org[org_doc["organization_ids"][code]]),
                        "exchange_from_verified_official_code_pattern": (
                            "BJ" if code.startswith("92") or code[0] in "48" else
                            "SH" if code[0] in "69" else "SZ"),
                        "exchange_mapping_basis": "verified official universe code pattern; codes remain separate securities",
                        "exchange_id_raw": raw.get("f13"),
                        "display_name_raw": raw.get("f14"),
                        "industry_raw_f100": raw.get("f100"),
                        "ipo_date_raw_f26": raw.get("f26"),
                        "market_cap_raw_f20_yuan": raw.get("f20"),
                        "page": item["page"],
                        "receipt_id": item["receipt_id"],
                        "content_hash": item["content_hash"],
                        "requested_at": item["requested_at"],
                        "responded_at": item["responded_at"],
                        "source_url": item["url"],
                    })
        records = [row for code in sorted(observed) for row in observed[code]]
        ended = utc()
        artifact = {
            "schema": "aquant.eastmoney_research_industry_supplement.v1",
            "captured_from": min((p["requested_at"] for p in pages if p.get("requested_at")), default=started),
            "captured_to": max((p["responded_at"] for p in pages if p.get("responded_at")), default=ended),
            "generated_at": ended,
            "source": "Eastmoney direct quote universe endpoint",
            "field_map": {"f12": "security code", "f13": "provider exchange id", "f14": "provider name",
                          "f100": "provider industry label; taxonomy/version not specified by endpoint",
                          "f26": "provider IPO date", "f20": "provider market cap in yuan"},
            "classification_system": "Eastmoney f100 provider taxonomy (not claimed to be CSRC or equal to BaoStock)",
            "official_universe_count": len(codes), "target_unknown_active_code_count": len(targets),
            "provider_catalog_total": total, "pages_captured": len(pages),
            "collection_complete": bool(total is not None and len(pages) * 100 >= total),
            "offline_mode": args.offline,
            "matched_target_code_count": len(observed), "missing_target_codes": sorted(targets - set(observed)),
            "identity_policy": "exact six-digit official code match only; orgId is metadata and never causes substitution or merge",
            "source_pages": [{k: v for k, v in p.items() if k != "rows"} for p in pages],
            "rows": records,
        }
        OUTPUT.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"output": str(OUTPUT), "target_unknown_active_code_count": len(targets),
            "provider_catalog_total": total, "pages_captured": len(pages),
            "matched_target_code_count": len(observed), "missing_count": len(targets - set(observed)),
            "rows_by_verified_code_pattern": dict(Counter("BJ" if x.startswith("92") or x[0] in "48" else "SH" if x[0] in "69" else "SZ" for x in observed))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
