"""Publish a complete current research scan from real frozen quote archives."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from aquant.domain.data.db import connect
from aquant.domain.data.forward_archive import ForwardArchive
from aquant.operations.research_candidates import file_hash
from aquant.operations.research_scan import rebuild_scan
from bind_structured_to_announcements import verified_organization_map
from verify_research_market_inputs import verify_industry, verify_history_refresh

ROW = re.compile(r'v_([a-z]{2}[0-9]{6})="([^"]*)";')
FIELDS = {"display_name": 1, "code": 2, "last_price_raw": 3, "previous_close_raw": 4,
          "volume_raw": 6, "quote_time_raw": 30, "turnover_amount_raw": 37, "total_market_cap_raw": 45}


def write_json(path, packet):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(packet, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def freeze(path, folder):
    digest = file_hash(path)[7:23]
    destination = folder / f"{path.stem}-{digest}{path.suffix}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(path.read_bytes())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
    if file_hash(destination) != file_hash(path):
        raise ValueError("frozen source differs from its admitted input")
    return destination


def verify_quotes(packet, universe, archive, con):
    """Validate display rows against the actual archived provider fields/receipts."""
    parsed = {}
    batches = {}
    for row in packet["rows"]:
        rid = row["receipt_id"]
        if rid not in batches:
            receipt = con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?", (rid,)).fetchone()
            if (receipt is None or receipt["source_id"] != "tencent-qt" or receipt["outcome"] != "OK"
                    or receipt["http_status"] != 200 or not archive.verify(receipt["content_hash"])):
                raise ValueError("current quote receipt is not verified")
            body = archive.load_bytes(receipt["content_hash"])
            if len(body) != receipt["byte_size"]:
                raise ValueError("quote byte count differs from receipt")
            response = {}
            for symbol, value in ROW.findall(body.decode("gbk", "replace")):
                if symbol in response:
                    raise ValueError("duplicate quote identity in provider response")
                response[symbol] = value.split("~")
            batches[rid] = receipt
            parsed[rid] = response
        receipt = batches[rid]
        fields = parsed[rid].get(row["symbol"])
        expected_iid = row["symbol"][:2].upper() + "." + row["symbol"][2:]
        if (row["code"] not in universe or row["org_id"] != universe[row["code"]]
                or row["instrument_id"] != expected_iid or row["symbol"][2:] != row["code"]
                or row["symbol"] not in receipt["url"].partition("q=")[2].split(",")
                or row["source_url"] != receipt["url"] or row["content_hash"] != receipt["content_hash"]
                or row["requested_at"] != receipt["requested_at"] or row["responded_at"] != receipt["responded_at"]
                or fields is None or len(fields) <= 45
                or any(str(row[key]) != fields[index].strip() for key, index in FIELDS.items())):
            raise ValueError("research quote row differs from original provider response")
    starts = [r["requested_at"] for r in batches.values()]
    ends = [r["responded_at"] for r in batches.values()]
    if packet["capture_started_at"] != min(starts) or packet["capture_finished_at"] != max(ends):
        raise ValueError("capture window differs from real receipt timestamps")
    for rid, response in parsed.items():
        stamps = [datetime.strptime(fields[30], "%Y%m%d%H%M%S") for fields in response.values() if len(fields) > 45]
        latest = max(stamps)
        for row in (r for r in packet["rows"] if r["receipt_id"] == rid):
            actual_lag = int((latest - datetime.strptime(row["quote_time_raw"], "%Y%m%d%H%M%S")).total_seconds())
            if row["quote_time_lag_seconds_from_batch_latest"] != actual_lag:
                raise ValueError("quote freshness differs from original response timestamps")
    print(f"verified original quote rows={len(packet['rows'])}, receipt batches={len(batches)}")


def verify_announcements(packet, universe, archive, con):
    from datetime import date
    from capture_research_announcements import extract_pdf_text, parse_pages
    if packet.get("schema") != "aquant.cninfo_research_announcements.v1":
        raise ValueError("unsupported announcement evidence schema")
    window = packet["window"]
    start, end = date.fromisoformat(window["start"]), date.fromisoformat(window["end"])
    if start > end or end > datetime.fromisoformat(packet["captured_to"]).astimezone(
            timezone(timedelta(hours=8))).date():
        raise ValueError("announcement window is invalid or not yet observable")
    codes = packet["target_codes"]
    if (not 1 <= len(codes) <= 20 or len(set(codes)) != len(codes)
            or set(codes) != {q["code"] for q in packet["per_code"]}
            or packet["targetCount"] != len(codes)):
        raise ValueError("announcement coverage differs from the declared targets")
    admitted = {}
    receipts = {}
    for query in packet["per_code"]:
        code = query["code"]
        if universe.get(code) != query["orgId"] or query["window"] != list(window.values()):
            raise ValueError("event query differs from official identity")
        for page in query["pages"]:
            receipt = con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?", (page["indexReceiptId"],)).fetchone()
            if (not receipt or page["requestedAt"] != receipt["requested_at"]
                    or page["respondedAt"] != receipt["responded_at"] or page["indexUrl"] != receipt["url"]):
                raise ValueError("announcement query timestamps differ from the real receipt")
            receipts[page["indexReceiptId"]] = receipt
        pages = [{"page": p["page"], "receipt_id": p["indexReceiptId"],
                  "content_hash": p["indexContentHash"], "request_body_hash": p["requestBodyHash"]}
                 for p in query["pages"]]
        parsed = parse_pages(code, query["orgId"], pages, archive, start=start, end=end)
        if not parsed["complete"] or query["queryComplete"] is not True:
            raise ValueError("announcement coverage does not match original pages")
        if (query["rawAnnouncementCount"] != parsed["raw_seen"]
                or query["totalAnnouncement"] != parsed["total_announcement"]):
            raise ValueError("announcement counts differ from the original index")
        for item in parsed["announcements"]:
            admitted[(code, item["announcement_id"])] = item
    if len({q["code"] for q in packet["per_code"]}) != len(packet["per_code"]):
        raise ValueError("duplicate announcement research target")
    if packet["queryCompleteCount"] != len(codes) or packet.get("stopOnBreaker"):
        raise ValueError("announcement index queries are incomplete")
    for document in packet["documents"]:
        source = admitted.get((document["code"], document["announcementId"]))
        receipt = con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?", (document["pdfReceiptId"],)).fetchone()
        if (not source or source["url"] != document["url"] or source["title"] != document["title"]
                or source["published_date"] != document["publishedDate"]
                or source["index_receipt_id"] != document["indexReceiptId"]
                or source["index_content_hash"] != document["indexContentHash"]
                or not receipt or receipt["source_id"] != "cninfo" or receipt["url"] != document["url"]
                or receipt["outcome"] != "OK" or receipt["http_status"] != 200
                or receipt["content_hash"] != document["pdfContentHash"] or not archive.verify(receipt["content_hash"])
                or document["pdfRequestedAt"] != receipt["requested_at"]
                or document["pdfRespondedAt"] != receipt["responded_at"]
                or document["pdfFirstSeenAt"] != receipt["first_seen_at"]):
            raise ValueError("event PDF differs from official index or receipt")
        receipts[document["pdfReceiptId"]] = receipt
        body = archive.load_bytes(receipt["content_hash"])
        if len(body) != receipt["byte_size"] or not body.startswith(b"%PDF-"):
            raise ValueError("event PDF bytes not verified")
        text, pages, count = extract_pdf_text(body)
        if text != document["extractedText"] or pages != document["pages"] or count != document["pageCount"]:
            raise ValueError("event quotation differs from the original PDF")
    if len({(d["code"], d["announcementId"]) for d in packet["documents"]}) != len(packet["documents"]):
        raise ValueError("duplicate announcement document")
    if packet["capturedDocumentCount"] != len(packet["documents"]):
        raise ValueError("announcement PDF count differs from documents")
    if (not receipts or packet["captured_from"] != min(r["requested_at"] for r in receipts.values())
            or packet["captured_to"] != max(r["responded_at"] for r in receipts.values())):
        raise ValueError("announcement capture window differs from real receipt timestamps")
    print(f"verified event queries={len(packet['per_code'])}, original PDF texts={len(packet['documents'])}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quotes", type=Path, default=ROOT / "data/s2-structured/research-quotes-20261009.json")
    ap.add_argument("--bound", type=Path, default=ROOT / "data/s2-structured/bound-v6-20261009.json")
    ap.add_argument("--facts-db", type=Path, default=ROOT / "data/s2-structured/recon-v7-20261009.sqlite")
    ap.add_argument("--org-map", type=Path, default=ROOT / "data/s2-structured/universe-orgids.json")
    ap.add_argument("--industry", type=Path, default=ROOT / "deploy/agentctl-q0/baostock-industry-cache.json")
    ap.add_argument("--industry-supplement", type=Path)
    ap.add_argument("--industry-disclosures", type=Path)
    ap.add_argument("--history", type=Path, default=ROOT / "deploy/agentctl-q0/universe-bars.json")
    ap.add_argument("--history-refresh", type=Path)
    ap.add_argument("--quote-mode", choices=("intraday", "closing"), default="intraday")
    ap.add_argument("--archive-root", type=Path, default=ROOT / "deploy/agentctl-q0/forward-archive")
    ap.add_argument("--output", type=Path, default=ROOT / "data/s2-structured/research-candidates.json")
    ap.add_argument("--announcements", type=Path)
    args = ap.parse_args()
    quotes = json.loads(args.quotes.read_text(encoding="utf-8"))
    with closing(connect(args.archive_root / "meta.sqlite")) as con:
        archive = ForwardArchive(con, args.archive_root)
        universe, _, orgproof = verified_organization_map(args.org_map, archive)
        verify_quotes(quotes, universe, archive, con)
        industries = json.loads(args.industry.read_text(encoding="utf-8"))
        if industries.get("schema") == "aquant.baostock_research_industry.v1":
            verify_industry(industries, universe, args.archive_root)
        if args.industry_supplement:
            from verify_research_industry_supplement import verify_industry_supplement
            verify_industry_supplement(json.loads(args.industry_supplement.read_text(encoding="utf-8")),
                universe, args.archive_root, target_quotes=quotes, target_industry=industries)
        if args.industry_disclosures:
            if not args.industry_supplement:
                raise ValueError("company disclosure supplement requires the admitted CAPCO base gap list")
            from verify_research_industry_missing import verify_industry_missing
            verify_industry_missing(json.loads(args.industry_disclosures.read_text(encoding="utf-8")), universe,
                args.archive_root, base_supplement=json.loads(args.industry_supplement.read_text(encoding="utf-8")), target_quotes=quotes)
        if args.history_refresh:
            verify_history_refresh(json.loads(args.history_refresh.read_text(encoding="utf-8")), universe)
        if args.announcements:
            verify_announcements(json.loads(args.announcements.read_text(encoding="utf-8")), universe, archive, con)
        name_path = args.archive_root / con.execute("SELECT stored_path FROM raw_artifact WHERE content_hash=?",
                                                  (orgproof["content_hash"],)).fetchone()[0]
    quotes = dict(quotes, captured_from=quotes["capture_started_at"], captured_to=quotes["capture_finished_at"],
                  universe_codes=sorted(universe),
                  research_quote_mode="CLOSING_RESEARCH" if args.quote_mode == "closing" else "INTRADAY_RESEARCH")
    if args.quote_mode == "closing":
        local_start = datetime.fromisoformat(quotes["captured_from"]).astimezone(timezone(timedelta(hours=8)))
        local_end = datetime.fromisoformat(quotes["captured_to"]).astimezone(timezone(timedelta(hours=8)))
        if (quotes.get("quote_time_kind") != "after_close_research_snapshot"
                or local_start.hour < 15 or local_start.date() != local_end.date()):
            raise ValueError("closing research requires a verified same-day after-close capture")
    for row in quotes["rows"]:
        row["batch_lag_seconds"] = row["quote_time_lag_seconds_from_batch_latest"]
    frozen_dir = args.output.parent / "frozen-inputs"
    normalized = args.output.parent / "research-quote-input-20261009.json"
    write_json(normalized, quotes)
    sources = [("quotes", normalized), ("bound_records", args.bound), ("facts_db", args.facts_db),
               ("industry_snapshot", args.industry), ("price_history", args.history), ("company_names", name_path)]
    if args.announcements:
        sources.append(("announcements", args.announcements))
    if args.history_refresh:
        sources.append(("price_history_refresh", args.history_refresh))
    if args.industry_supplement:
        sources.append(("industry_supplement", args.industry_supplement))
    if args.industry_disclosures:
        sources.append(("industry_disclosures", args.industry_disclosures))
    provenance = []
    for role, path in sources:
        frozen = freeze(path, frozen_dir) if role in {"quotes", "industry_snapshot", "industry_supplement", "industry_disclosures", "price_history", "price_history_refresh", "announcements"} else path
        provenance.append({"role": role, "path": str(frozen.resolve()), "contentHash": file_hash(frozen)})
    packet = rebuild_scan(tuple(sorted((s["role"], s["path"], s["contentHash"]) for s in provenance)))
    packet["provenance"] = provenance
    write_json(args.output, packet)
    print(json.dumps({"output": str(args.output), "poolSize": packet["poolSize"], "rankedCount": packet["rankedCount"],
                      "excludedCount": packet["excludedCount"], "scope": packet["scope"],
                      "quoteMode": packet["quoteMode"], "historyCoverage": packet["historyCoverage"],
                      "industryCoverage": packet["industryCoverage"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
