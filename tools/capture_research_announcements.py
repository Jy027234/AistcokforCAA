"""Capture recent official CNINFO announcements for the research candidates.

The index query is exact-range, all-category and archive-first. This produces
research evidence only: it does not infer event direction or write candidates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.parse
from contextlib import closing
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.cninfo import (  # noqa: E402
    CninfoClient, REPORT_QUERY_URL, STATIC_HOST, CST,
    RateLimiter, CircuitBreaker, RetryPolicy,
)
from aquant.adapters.providers.cninfo_probe import market_for_code, normalize_title  # noqa: E402
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402

ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"
TARGETS = ROOT / "tmp" / "research-event-targets-20261009.json"
ORG_MAP = ROOT / "data" / "s2-structured" / "universe-orgids.json"
OUTPUT = ROOT / "data" / "s2-structured" / "research-announcements-20261009.json"
CHECKPOINT = ROOT / "data" / "s2-structured" / "research-announcements-20261009.checkpoint.jsonl"
WINDOW_START = date(2026, 9, 9)
WINDOW_END = date(2026, 10, 9)
BEIJING = timezone(timedelta(hours=8))
MAX_PAGES = 100
PAGE_SIZE = 30
MAX_PDFS = 40
MAX_TEXT_CHARS = 3000
MAX_PDF_BYTES = 64 * 1024 * 1024
PERIODIC_TERMS = (
    "定期报告", "年度报告", "半年度报告", "季度报告", "第一季度报告", "第三季度报告",
    "年报", "半年报", "季报", "一季报", "三季报", "四季报",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def targets_and_orgs(target_path: Path, org_map_path: Path) -> tuple[list[dict], dict, dict]:
    targets = json.loads(target_path.read_text(encoding="utf-8-sig"))
    org_doc = json.loads(org_map_path.read_text(encoding="utf-8-sig"))
    if not isinstance(targets, list) or not 1 <= len(targets) <= 20:
        raise ValueError("expected between 1 and 20 event research targets")
    orgs = org_doc.get("organization_ids")
    if not isinstance(orgs, dict):
        raise ValueError("official organization map is malformed")
    seen: set[str] = set()
    cleaned: list[dict] = []
    for item in targets:
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or "").strip()
        if not re.fullmatch(r"\d{6}", code) or code in seen:
            raise ValueError(f"target code is invalid or duplicated: {code!r}")
        if not name or code not in orgs:
            raise ValueError(f"target lacks display name or official orgId: {code}")
        seen.add(code)
        cleaned.append({"code": code, "name": name, "org_id": str(orgs[code]),
                        "column": market_for_code(code)})
    return cleaned, orgs, org_doc


def verify_org_map(archive: ForwardArchive, org_doc: dict) -> None:
    receipt = archive.con.execute(
        "SELECT source_id,url,outcome,http_status,content_hash FROM fetch_receipt WHERE receipt_id=?",
        (org_doc.get("receipt_id"),),
    ).fetchone()
    if not (receipt and receipt["source_id"] == "cninfo" and receipt["url"] == org_doc.get("source_url")
            and receipt["outcome"] == "OK" and receipt["http_status"] == 200
            and receipt["content_hash"] == org_doc.get("content_hash")
            and archive.verify(receipt["content_hash"])):
        raise ValueError("official organization map receipt/hash does not verify")


def request_body(code: str, org_id: str, column: str, page: int, *,
                 start: date = WINDOW_START, end: date = WINDOW_END) -> bytes:
    return urllib.parse.urlencode({
        "pageNum": page, "pageSize": PAGE_SIZE, "column": column,
        "tabName": "fulltext", "plate": "", "stock": f"{code},{org_id}",
        "searchkey": "", "secid": "", "category": "", "trade": "",
        "seDate": f"{start.isoformat()}~{end.isoformat()}",
        "sortName": "", "sortType": "", "isHLtitle": "true",
    }).encode("utf-8")


def _has_more(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if value is None:
        return None
    raise ValueError("invalid hasMore")


def _record_from_raw(row: dict, code: str, org_id: str, page: dict, *,
                     start: date = WINDOW_START, end: date = WINDOW_END) -> dict:
    sec_code = str(row.get("secCode") or "").strip()
    if sec_code != code:
        raise ValueError(f"response identity mismatch: expected {code}, received {sec_code!r}")
    response_org_id = str(row.get("orgId") or "").strip()
    if response_org_id and response_org_id != org_id:
        raise ValueError(f"response orgId mismatch for {code}")
    announcement_id = str(row.get("announcementId") or "").strip()
    title = normalize_title(row.get("announcementTitle"))
    if not announcement_id or not title:
        raise ValueError("announcement ID or title is missing")
    try:
        millis = int(row.get("announcementTime"))
        if isinstance(row.get("announcementTime"), bool) or millis <= 0:
            raise ValueError("invalid announcementTime")
        published = datetime.fromtimestamp(millis / 1000, tz=timezone.utc).astimezone(CST).date()
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"invalid announcementTime: {exc}") from exc
    if not start <= published <= end:
        raise ValueError("announcement date outside requested window")
    path = str(row.get("adjunctUrl") or "").strip()
    parsed = urlsplit(path)
    if not path or parsed.scheme or parsed.netloc:
        raise ValueError("announcement document path is missing or not relative")
    url = f"https://{STATIC_HOST}/{path.lstrip('/')}"
    return {
        "code": code, "org_id": org_id, "announcement_id": announcement_id,
        "title": title, "published_date": published.isoformat(),
        "announcement_time_ms": millis, "url": url,
        "announcement_type": str(row.get("announcementTypeName") or ""),
        "index_page": page["page"], "index_receipt_id": page["receipt_id"],
        "index_content_hash": page["content_hash"],
    }


def parse_pages(code: str, org_id: str, pages: list[dict], archive: ForwardArchive,
                *, verify_query: bool = True, start: date = WINDOW_START,
                end: date = WINDOW_END) -> dict:
    """Reparse and validate complete paginated evidence from archived raw bytes."""
    if not pages:
        return {"complete": False, "termination": "no_pages", "error": "no response pages", "announcements": []}
    all_announcements: list[dict] = []
    seen_ids: set[str] = set()
    expected_total: int | None = None
    raw_seen = 0
    last_has_more: bool | None = None
    for expected_page, page in enumerate(pages, 1):
        if page.get("page") != expected_page:
            return {"complete": False, "termination": "page_sequence_invalid", "error": "page sequence gap", "announcements": []}
        receipt = archive.con.execute(
            "SELECT source_id,url,outcome,http_status,content_hash,byte_size FROM fetch_receipt WHERE receipt_id=?",
            (page.get("receipt_id"),),
        ).fetchone()
        digest = page.get("content_hash")
        if not (receipt and receipt["source_id"] == "cninfo" and receipt["url"] == REPORT_QUERY_URL
                and receipt["outcome"] == "OK" and receipt["http_status"] == 200
                and receipt["content_hash"] == digest and archive.verify(digest)):
            return {"complete": False, "termination": "receipt_invalid", "error": "response receipt/hash invalid", "announcements": []}
        request_hash = page.get("request_body_hash")
        if not archive.verify(request_hash):
            return {"complete": False, "termination": "request_body_invalid", "error": "request body hash invalid", "announcements": []}
        body = archive.load_bytes(request_hash)
        parsed_body = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
        if verify_query and (parsed_body.get("seDate") != [f"{start.isoformat()}~{end.isoformat()}"]
                or parsed_body.get("stock") != [f"{code},{org_id}"]
                or parsed_body.get("pageNum") != [str(expected_page)]
                or parsed_body.get("category") != [""]):
            return {"complete": False, "termination": "query_identity_invalid", "error": "request body scope/code/orgId mismatch", "announcements": []}
        payload = archive.load_bytes(digest)
        if len(payload) != receipt["byte_size"]:
            return {"complete": False, "termination": "response_size_invalid", "error": "response size differs from receipt", "announcements": []}
        try:
            document = json.loads(payload)
            if not isinstance(document, dict):
                raise ValueError("response is not an object")
            raw_rows = document.get("announcements") or []
            if not isinstance(raw_rows, list):
                raise ValueError("announcements is not a list")
            raw_total = document.get("totalAnnouncement")
            page_total = None if raw_total is None else int(raw_total)
            if page_total is not None and (isinstance(raw_total, bool) or page_total < 0):
                raise ValueError("invalid totalAnnouncement")
            more = _has_more(document.get("hasMore"))
        except (ValueError, TypeError, OverflowError, json.JSONDecodeError) as exc:
            return {"complete": False, "termination": "page_parse_failed", "error": str(exc), "announcements": []}
        if expected_total is not None and page_total is not None and expected_total != page_total:
            return {"complete": False, "termination": "pagination_inconsistent", "error": "totalAnnouncement changed", "announcements": []}
        if page_total is not None:
            expected_total = page_total
        raw_seen += len(raw_rows)
        for row in raw_rows:
            if not isinstance(row, dict):
                return {"complete": False, "termination": "identity_incomplete", "error": "non-object announcement", "announcements": []}
            row_code = str(row.get("secCode") or "").strip()
            if row_code != code:
                return {"complete": False, "termination": "identity_mismatch", "error": f"expected {code}, got {row_code!r}", "announcements": []}
            try:
                item = _record_from_raw(row, code, org_id, page, start=start, end=end)
            except ValueError as exc:
                return {"complete": False, "termination": "identity_incomplete", "error": str(exc), "announcements": []}
            if item["announcement_id"] in seen_ids:
                return {"complete": False, "termination": "pagination_inconsistent", "error": "duplicate announcement across pages", "announcements": []}
            seen_ids.add(item["announcement_id"])
            all_announcements.append(item)
        last_has_more = more
        if page_total is not None and raw_seen > page_total:
            return {"complete": False, "termination": "pagination_inconsistent", "error": "raw rows exceed totalAnnouncement", "announcements": []}
        if more is False:
            if page_total is not None and raw_seen < page_total:
                return {"complete": False, "termination": "pagination_inconsistent", "error": "hasMore=false before totalAnnouncement", "announcements": []}
            return {"complete": True, "termination": "has_more_false", "error": None,
                    "announcements": all_announcements, "total_announcement": expected_total, "raw_seen": raw_seen}
        if more is True and page_total is not None and raw_seen >= page_total:
            return {"complete": False, "termination": "pagination_inconsistent", "error": "hasMore=true after totalAnnouncement", "announcements": []}
        if more is None and page_total is not None and raw_seen == page_total:
            return {"complete": True, "termination": "total_reached", "error": None,
                    "announcements": all_announcements, "total_announcement": expected_total, "raw_seen": raw_seen}
    return {"complete": False, "termination": "max_pages_truncated", "error": f"pagination did not exhaust within {len(pages)} pages", "announcements": []}


def run_index(code_row: dict, client: CninfoClient, archive: ForwardArchive, *,
              start: date = WINDOW_START, end: date = WINDOW_END) -> dict:
    code, org_id, column = code_row["code"], code_row["org_id"], code_row["column"]
    pages: list[dict] = []
    for page_num in range(1, MAX_PAGES + 1):
        body = request_body(code, org_id, column, page_num, start=start, end=end)
        body_hash, _ = archive.store_bytes(body, media_type="application/x-www-form-urlencoded")
        out = client._fetch(
            REPORT_QUERY_URL,
            label=f"research-event-index:{code}:{start}~{end}:page={page_num}:request={body_hash}",
            data=body, content_type="application/x-www-form-urlencoded; charset=UTF-8",
        )
        receipt = archive.con.execute(
            "SELECT receipt_id,url,requested_at,responded_at,first_seen_at,outcome,http_status,content_hash,byte_size,detail "
            "FROM fetch_receipt WHERE receipt_id=?", (out.receipt_id,),
        ).fetchone() if out.receipt_id else None
        if not (out.ok and out.payload is not None and out.content_hash and receipt
                and receipt["url"] == REPORT_QUERY_URL and receipt["outcome"] == "OK"
                and receipt["http_status"] == 200 and receipt["content_hash"] == out.content_hash
                and receipt["byte_size"] == len(out.payload) and archive.verify(out.content_hash)):
            page = {"page": page_num, "request_body_hash": body_hash,
                    "receipt_id": out.receipt_id, "content_hash": out.content_hash,
                    "receipt_outcome": receipt["outcome"] if receipt else None,
                    "failure_detail": (receipt["detail"] if receipt and receipt["detail"]
                                       else out.detail or "response was not a verified archive receipt"),
                    "http_status": receipt["http_status"] if receipt else out.http_status,
                    "raw_announcement_count": None, "total_announcement": None, "has_more": None,
                    "requested_at": receipt["requested_at"] if receipt else None,
                    "responded_at": receipt["responded_at"] if receipt else None}
            pages.append(page)
            return {"pages": pages, "complete": False, "termination": "fetch_failed",
                    "error": page["failure_detail"], "announcements": []}
        try:
            document = json.loads(out.payload)
            rows = document.get("announcements") or []
            if not isinstance(rows, list):
                raise ValueError("announcements is not a list")
            raw_total = document.get("totalAnnouncement")
            total = None if raw_total is None else int(raw_total)
            more = _has_more(document.get("hasMore"))
        except (ValueError, TypeError, OverflowError, json.JSONDecodeError) as exc:
            pages.append({"page": page_num, "request_body_hash": body_hash,
                          "receipt_id": receipt["receipt_id"], "content_hash": receipt["content_hash"],
                          "http_status": receipt["http_status"], "raw_announcement_count": None,
                          "total_announcement": None, "has_more": None,
                          "requested_at": receipt["requested_at"], "responded_at": receipt["responded_at"]})
            return {"pages": pages, "complete": False, "termination": "page_parse_failed",
                    "error": str(exc), "announcements": []}
        pages.append({"page": page_num, "request_body_hash": body_hash,
                      "receipt_id": receipt["receipt_id"], "content_hash": receipt["content_hash"],
                      "http_status": receipt["http_status"], "raw_announcement_count": len(rows),
                      "total_announcement": total, "has_more": more,
                      "requested_at": receipt["requested_at"], "responded_at": receipt["responded_at"]})
        result = parse_pages(code, org_id, pages, archive, start=start, end=end)
        if result["complete"] or not result["error"] is None:
            # Continue only while the actual last page proves more data exists.
            if result["complete"] or result["termination"] != "max_pages_truncated":
                return {"pages": pages, **result}
        if more is False or (more is None and total is not None and sum(p["raw_announcement_count"] or 0 for p in pages) == total):
            return {"pages": pages, **parse_pages(code, org_id, pages, archive, start=start, end=end)}
        # When hasMore is unavailable, only a stable total can establish exhaustion.
        if more is None and total is None:
            return {"pages": pages, "complete": False, "termination": "pagination_unknown",
                    "error": "neither hasMore nor totalAnnouncement is available", "announcements": []}
    return {"pages": pages, **parse_pages(code, org_id, pages, archive, start=start, end=end)}


def eligible_documents(announcements: list[dict]) -> tuple[list[dict], dict[str, int]]:
    eligible: list[dict] = []
    counts: dict[str, int] = {}
    for item in announcements:
        title = item["title"]
        reason = None
        if any(term in title for term in PERIODIC_TERMS):
            reason = "periodic_report"
        elif "摘要" in title:
            reason = "summary"
        elif any(term.lower() in title.lower() for term in ("英文", "english")):
            reason = "english"
        elif not item["url"].lower().endswith(".pdf"):
            reason = "not_pdf"
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
        else:
            eligible.append(item)
    eligible.sort(key=lambda x: (x["announcement_time_ms"], x["announcement_id"]), reverse=True)
    return eligible, counts


def verified_pdf(archive: ForwardArchive, url: str) -> tuple[bytes, dict] | None:
    rows = archive.con.execute(
        "SELECT receipt_id,source_id,url,requested_at,responded_at,first_seen_at,outcome,http_status,content_hash,byte_size,detail "
        "FROM fetch_receipt WHERE source_id='cninfo' AND url=? AND outcome='OK' AND http_status=200 "
        "AND content_hash IS NOT NULL ORDER BY first_seen_at DESC", (url,),
    ).fetchall()
    for row in rows:
        if archive.verify(row["content_hash"]):
            payload = archive.load_bytes(row["content_hash"])
            if len(payload) == row["byte_size"] and payload.startswith(b"%PDF-"):
                return payload, dict(row)
    return None


def extract_pdf_text(pdf_bytes: bytes) -> tuple[str, list[dict], int]:
    import io
    import pdfplumber

    page_ranges: list[dict] = []
    chunks: list[str] = []
    char_count = 0
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page_count = len(pdf.pages)
        for index, page in enumerate(pdf.pages, 1):
            if char_count >= MAX_TEXT_CHARS:
                break
            source_text = page.extract_text() or ""
            if not source_text:
                continue
            prefix = "\n" if chunks else ""
            available = MAX_TEXT_CHARS - char_count - len(prefix)
            if available <= 0:
                break
            text = source_text[:available]
            start = char_count + len(prefix)
            if prefix:
                chunks.append(prefix)
            chunks.append(text)
            char_count = start + len(text)
            page_ranges.append({"page": index, "start_char": start, "end_char": char_count})
    return "".join(chunks), page_ranges, page_count


def read_checkpoint(path: Path) -> dict[tuple[str, str], dict]:
    latest: dict[tuple[str, str], dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            key = (item.get("kind"), item.get("code"), item.get("announcement_id", ""))
            latest[key] = item
    return latest


def verify_index_checkpoint(item: dict, code_row: dict, archive: ForwardArchive, *,
                            start: date = WINDOW_START, end: date = WINDOW_END) -> dict | None:
    if item.get("code") != code_row["code"] or item.get("org_id") != code_row["org_id"]:
        return None
    if item.get("query_window") != [start.isoformat(), end.isoformat()]:
        return None
    parsed = parse_pages(code_row["code"], code_row["org_id"], item.get("pages", []), archive,
                         start=start, end=end)
    if parsed.get("complete"):
        return {**item, **parsed, "checkpoint_reused": True}
    return None


def iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    parser.add_argument("--targets", type=Path, default=TARGETS)
    parser.add_argument("--org-map", type=Path, default=ORG_MAP)
    parser.add_argument("--checkpoint", type=Path, default=CHECKPOINT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--start", type=iso_date, default=WINDOW_START,
                        help="inclusive announcement date (YYYY-MM-DD)")
    parser.add_argument("--end", type=iso_date, default=WINDOW_END,
                        help="inclusive announcement date (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, help="initial sample limit; omitted means all 20")
    parser.add_argument("--trusted-proxy-network", action="append", default=[])
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must be on or before --end")
    if args.end > datetime.now(timezone.utc).date():
        parser.error("--end cannot be in the future relative to current UTC date")
    target_rows, _, org_doc = targets_and_orgs(args.targets, args.org_map)
    if args.limit:
        target_rows = target_rows[:args.limit]
    if args.trusted_proxy_network:
        os.environ["AQUANT_TRUSTED_PROXY_NETWORKS"] = ",".join(args.trusted_proxy_network)
    args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpointed = read_checkpoint(args.checkpoint)
    capture_started = now_iso()
    per_code: list[dict] = []
    documents: list[dict] = []
    stop_on_breaker = False
    with closing(connect(args.archive_root / "meta.sqlite")) as con:
        archive = ForwardArchive(con, args.archive_root)
        verify_org_map(archive, org_doc)
        client = CninfoClient(
            archive, limiter=RateLimiter(min_interval_seconds=2.5),
            breaker=CircuitBreaker(failure_threshold=4, cooldown_seconds=120),
            retry=RetryPolicy(max_attempts=2, base_delay_seconds=3, max_delay_seconds=6),
        )
        with args.checkpoint.open("a", encoding="utf-8", newline="\n") as checkpoint:
            for index, target in enumerate(target_rows, 1):
                code = target["code"]
                idx_key = ("index", code, "")
                saved = checkpointed.get(idx_key)
                index_result = verify_index_checkpoint(saved, target, archive, start=args.start, end=args.end) if saved else None
                if index_result is None:
                    index_result = run_index(target, client, archive, start=args.start, end=args.end)
                    index_entry = {
                        "kind": "index", "code": code, "org_id": target["org_id"],
                        "status": "complete" if index_result.get("complete") else "incomplete",
                        "query_window": [args.start.isoformat(), args.end.isoformat()],
                        **index_result,
                    }
                    checkpoint.write(json.dumps(index_entry, ensure_ascii=False) + "\n")
                    checkpoint.flush()
                    checkpointed[idx_key] = index_entry
                complete = bool(index_result.get("complete"))
                announcements = index_result.get("announcements", []) if complete else []
                eligible, filter_counts = eligible_documents(announcements)
                chosen = eligible[:2]
                code_documents: list[dict] = []
                pdf_failures: list[dict] = []
                if complete:
                    for candidate in chosen:
                        ann_id = candidate["announcement_id"]
                        pdf_key = ("pdf", code, ann_id)
                        prior = checkpointed.get(pdf_key)
                        pdf_bytes = None
                        pdf_receipt = None
                        if prior and prior.get("status") == "ok" and prior.get("url") == candidate["url"]:
                            row = con.execute(
                                "SELECT receipt_id,source_id,url,requested_at,responded_at,first_seen_at,outcome,http_status,content_hash,byte_size FROM fetch_receipt WHERE receipt_id=?",
                                (prior.get("pdf_receipt_id"),),
                            ).fetchone()
                            if (row and row["source_id"] == "cninfo" and row["url"] == candidate["url"]
                                    and row["outcome"] == "OK" and row["http_status"] == 200
                                    and row["content_hash"] == prior.get("pdf_content_hash")
                                    and archive.verify(row["content_hash"])):
                                pdf_bytes = archive.load_bytes(row["content_hash"])
                                if len(pdf_bytes) == row["byte_size"] and pdf_bytes.startswith(b"%PDF-"):
                                    pdf_receipt = dict(row)
                                else:
                                    pdf_bytes = None
                        if pdf_bytes is None:
                            existing = verified_pdf(archive, candidate["url"])
                            if existing:
                                pdf_bytes, pdf_receipt = existing
                            else:
                                out = client.document(candidate["url"],
                                    label=f"research-announcement:{code}:{ann_id}", max_bytes=MAX_PDF_BYTES)
                                row = con.execute(
                                    "SELECT receipt_id,source_id,url,requested_at,responded_at,first_seen_at,outcome,http_status,content_hash,byte_size,detail "
                                    "FROM fetch_receipt WHERE receipt_id=?", (out.receipt_id,),
                                ).fetchone() if out.receipt_id else None
                                if (out.ok and out.payload and row and row["source_id"] == "cninfo"
                                        and row["url"] == candidate["url"] and row["outcome"] == "OK"
                                        and row["http_status"] == 200 and row["content_hash"] == out.content_hash
                                        and row["byte_size"] == len(out.payload) and archive.verify(out.content_hash)
                                        and out.payload.startswith(b"%PDF-")):
                                    pdf_bytes, pdf_receipt = out.payload, dict(row)
                                else:
                                    fail = {"kind": "pdf", "code": code, "announcement_id": ann_id,
                                            "status": "failed", "url": candidate["url"],
                                            "detail": out.detail or "response was not a verified PDF",
                                            "pdf_receipt_id": out.receipt_id, "pdf_content_hash": out.content_hash}
                                    checkpoint.write(json.dumps(fail, ensure_ascii=False) + "\n")
                                    checkpoint.flush()
                                    checkpointed[pdf_key] = fail
                                    pdf_failures.append(fail)
                                    if client.breaker.is_open:
                                        stop_on_breaker = True
                                        break
                        if pdf_bytes is not None and pdf_receipt is not None:
                            try:
                                extracted, page_ranges, page_count = extract_pdf_text(pdf_bytes)
                                document = {
                                    "code": code, "announcementId": ann_id, "title": candidate["title"],
                                    "publishedDate": candidate["published_date"], "url": candidate["url"],
                                    "indexReceiptId": candidate["index_receipt_id"],
                                    "indexContentHash": candidate["index_content_hash"],
                                    "indexPage": candidate["index_page"],
                                    "pdfReceiptId": pdf_receipt["receipt_id"],
                                    "pdfContentHash": pdf_receipt["content_hash"],
                                    "pdfRequestedAt": pdf_receipt.get("requested_at"),
                                    "pdfRespondedAt": pdf_receipt.get("responded_at"),
                                    "pdfFirstSeenAt": pdf_receipt.get("first_seen_at"),
                                    "extractedText": extracted, "pages": page_ranges,
                                    "pageCount": page_count, "charRange": [0, len(extracted)],
                                    "textCharLimit": MAX_TEXT_CHARS,
                                }
                                code_documents.append(document)
                                pdf_entry = {"kind": "pdf", "code": code, "announcement_id": ann_id,
                                    "status": "ok", "url": candidate["url"],
                                    "pdf_receipt_id": pdf_receipt["receipt_id"],
                                    "pdf_content_hash": pdf_receipt["content_hash"]}
                                checkpoint.write(json.dumps(pdf_entry, ensure_ascii=False) + "\n")
                                checkpoint.flush()
                                checkpointed[pdf_key] = pdf_entry
                            except Exception as exc:
                                fail = {"kind": "pdf", "code": code, "announcement_id": ann_id,
                                    "status": "failed", "url": candidate["url"],
                                    "detail": f"pdf_text_extract_failed:{type(exc).__name__}: {exc}",
                                    "pdf_receipt_id": pdf_receipt.get("receipt_id"),
                                    "pdf_content_hash": pdf_receipt.get("content_hash")}
                                pdf_failures.append(fail)
                                checkpoint.write(json.dumps(fail, ensure_ascii=False) + "\n")
                                checkpoint.flush()
                                checkpointed[pdf_key] = fail
                documents.extend(code_documents)
                per_code.append({
                    "code": code, "targetName": target["name"], "orgId": target["org_id"],
                    "column": target["column"], "window": [args.start.isoformat(), args.end.isoformat()],
                    "queryComplete": complete, "termination": index_result.get("termination"),
                    "error": index_result.get("error"),
                    "totalAnnouncement": index_result.get("total_announcement"),
                    "rawAnnouncementCount": index_result.get("raw_seen"),
                    "pages": [{
                        "page": page["page"], "requestBodyHash": page["request_body_hash"],
                        "indexUrl": REPORT_QUERY_URL,
                        "indexReceiptId": page["receipt_id"], "indexContentHash": page["content_hash"],
                        "receiptOutcome": page.get("receipt_outcome"),
                        "failureDetail": page.get("failure_detail"),
                        "rawAnnouncementCount": page["raw_announcement_count"],
                        "totalAnnouncement": page["total_announcement"], "hasMore": page["has_more"],
                        "requestedAt": page.get("requested_at"),
                        "respondedAt": page.get("responded_at"),
                    } for page in index_result.get("pages", [])],
                    "eligibleDocumentCount": len(eligible), "filteredByTitleOrType": filter_counts,
                    "selectedAnnouncementIds": [x["announcement_id"] for x in chosen],
                    "capturedDocumentCount": len(code_documents), "pdfFailures": pdf_failures,
                    "queryFailureIsExplicit": not complete,
                })
                print(f"{index}/{len(target_rows)} {code} complete={complete} pages={len(index_result.get('pages', []))} "
                      f"docs={len(code_documents)}/{len(chosen)}", flush=True)
                if stop_on_breaker or client.breaker.is_open:
                    stop_on_breaker = True
                    break
    request_times = [page.get("requestedAt") for company in per_code for page in company.get("pages", [])
                     if page.get("requestedAt")]
    response_times = [page.get("respondedAt") for company in per_code for page in company.get("pages", [])
                      if page.get("respondedAt")]
    request_times.extend(doc.get("pdfRequestedAt") for doc in documents if doc.get("pdfRequestedAt"))
    response_times.extend(doc.get("pdfRespondedAt") for doc in documents if doc.get("pdfRespondedAt"))
    captured_from = min(request_times) if request_times else capture_started
    captured_to = max(response_times) if response_times else now_iso()
    by_code = {row["code"]: row for row in per_code}
    for target in target_rows:
        if target["code"] not in by_code:
            per_code.append({"code": target["code"], "targetName": target["name"],
                             "orgId": target["org_id"], "queryComplete": False,
                             "termination": "not_attempted", "window": [args.start.isoformat(), args.end.isoformat()],
                             "pages": [], "eligibleDocumentCount": 0,
                             "capturedDocumentCount": 0, "pdfFailures": [],
                             "queryFailureIsExplicit": True})
    output = {
        "schema": "aquant.cninfo_research_announcements.v1",
        "prepared_at": now_iso(), "captured_from": captured_from, "captured_to": captured_to,
        "window": {"start": args.start.isoformat(), "end": args.end.isoformat()},
        "target_codes": [x["code"] for x in target_rows],
        "officialOrgMapReceiptId": org_doc.get("receipt_id"),
        "officialOrgMapContentHash": org_doc.get("content_hash"),
        "queryCompleteCount": sum(x.get("queryComplete") is True for x in per_code),
        "targetCount": len(target_rows), "documentTargetLimit": MAX_PDFS,
        "capturedDocumentCount": len(documents),
        "pdfFailureCount": sum(len(x.get("pdfFailures", [])) for x in per_code),
        "captureComplete": (sum(x.get("queryComplete") is True for x in per_code) == len(target_rows)
                            and not any(x.get("pdfFailures") for x in per_code) and not stop_on_breaker),
        "stopOnBreaker": stop_on_breaker,
        "status": "RESEARCH_EVIDENCE_ONLY_TITLES_ARE_NOT_FACTS",
        "per_code": per_code, "documents": documents,
    }
    atomic_json(args.output, output)
    print(f"output={args.output} indexComplete={output['queryCompleteCount']}/{len(target_rows)} "
          f"PDFs={len(documents)}", flush=True)
    return 0 if output["captureComplete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
