"""Capture official CNINFO industry fields for the 31 active codes omitted by CAPCO H1.

This is an auxiliary, bounded research packet. It never substitutes issuer aliases
and labels each row with the classification system stated in the source PDF.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.cninfo import (  # noqa: E402
    CninfoClient, CrossCategoryIndexEntry, RateLimiter, CircuitBreaker, RetryPolicy,
    STATIC_HOST, REPORT_QUERY_URL,
)
from aquant.adapters.providers.cninfo_probe import normalize_title  # noqa: E402
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402

ARCHIVE_ROOT = ROOT / "deploy/agentctl-q0/forward-archive"
CAPCO_PACKET = ROOT / "data/s2-structured/research-industry-supplement-20261009.json"
QUOTES = ROOT / "data/s2-structured/research-quotes-close-20261009.json"
ORG_MAP = ROOT / "data/s2-structured/universe-orgids.json"
OUTPUT = ROOT / "data/s2-structured/research-industry-missing-20261009.json"
CHECKPOINT = ROOT / "data/s2-structured/research-industry-missing-20261009.checkpoint.jsonl"
WINDOW_START = date(2025, 12, 31)
WINDOW_END = date(2026, 10, 9)
BEIJING = timezone(timedelta(hours=8))
PAGE_SIZE = 30
MAX_PAGES = 1
MAX_PDFS_PER_CODE = 2
PDF_LIMIT = 64 * 1024 * 1024


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def load_targets() -> tuple[list[str], dict[str, str], dict[str, str]]:
    capco = json.loads(CAPCO_PACKET.read_text(encoding="utf-8"))
    quote_doc = json.loads(QUOTES.read_text(encoding="utf-8"))
    org_doc = json.loads(ORG_MAP.read_text(encoding="utf-8"))
    orgs = org_doc["organization_ids"]
    expected = sorted(capco["scope"]["missing_target_codes"])
    if len(expected) != 31 or len(set(expected)) != 31:
        raise RuntimeError("frozen CAPCO packet must identify exactly 31 unique missing codes")
    if not {row["official_code"] for row in quote_doc["rows"]}.issubset(orgs):
        raise RuntimeError("quote code set is not contained in the official org map")
    names = {row["official_code"]: row.get("display_name") or row.get("quote_name") or ""
             for row in quote_doc["rows"] if row["official_code"] in set(expected)}
    if any(not names.get(code) or not orgs.get(code) for code in expected):
        raise RuntimeError("one or more missing codes lack exact official name/orgId context")
    return expected, orgs, names


def text_pages(pdf_bytes: bytes) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("pypdf is required for official PDF field extraction") from exc
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    return [page.extract_text() or "" for page in reader.pages]


def _squash(value: str) -> str:
    return re.sub(r"[\s\u3000]+", " ", value).strip()


def parse_industry_pdf(pdf_bytes: bytes, *, code: str, expected_name: str) -> dict:
    pages = text_pages(pdf_bytes)
    page_texts = [_squash(x) for x in pages]
    joined = "\n".join(page_texts)
    code_pat = re.escape(code)
    issuer_match = re.search(r"公司全称\s*([^\n]+).*?证券代码\s*" + code_pat, joined)
    issuer_name = issuer_match.group(1).split("证券简称", 1)[0].strip() if issuer_match else None
    exact_code_occurrences = [i + 1 for i, text in enumerate(page_texts)
                              if re.search(r"证券代码\s*[“\"]?" + code_pat + r"\b", text)]

    industry = None
    industry_pattern = re.compile(
        r"所属行业名称\s*([\u4e00-\u9fffA-Za-z0-9（）()·、，,\-]+?)\s+所属行业代码\s*([A-Z]\d{1,4})"
    )
    for page_num, text in enumerate(page_texts, 1):
        if not re.search(r"证券代码\s*[“\"]?" + code_pat + r"\b", text):
            continue
        match = industry_pattern.search(text)
        if match:
            industry = {"label": match.group(1).strip(), "code": match.group(2), "page": page_num}
            break
    if industry is None:
        # Prospectuses may use their explicit "上市公司行业分类" table instead of
        # the issuance announcement's key-element table.
        for page_num, text in enumerate(page_texts, 1):
            if "上市公司行业分类" not in text:
                continue
            match = re.search(r"上市公司行业分类\s+.*?\b([A-Z]\d{2,4})\s+(.+?业)", text)
            if match:
                industry = {"label": match.group(2).strip(), "code": match.group(1), "page": page_num}
                break

    standard = None
    standard_code = re.search(r"GB\s*/\s*T\s*4754\s*[-－–—]\s*2017", joined, re.I)
    if standard_code and "国民经济行业分类" in joined:
        standard = {
            "system_name": "国民经济行业分类",
            "standard_id_raw": standard_code.group(0),
            "standard_id_normalized": "GB/T 4754-2017",
        }

    evidence = []
    keys = ("证券代码", "所属行业名称", "所属行业代码", "上市公司行业分类", "国民经济行业分类")
    for page_num, text in enumerate(pages, 1):
        if any(key in text for key in keys):
            for key in keys:
                pos = text.find(key)
                if pos >= 0:
                    evidence.append({"page": page_num, "field": key,
                                     "excerpt": _squash(text[max(0, pos - 140):pos + 420])})
    quote_name_match = bool(issuer_name and expected_name.replace(" ", "") in issuer_name.replace(" ", ""))
    identity_ok = bool(exact_code_occurrences and issuer_name)
    qualified = bool(identity_ok and industry and standard)
    return {
        "page_count": len(pages), "issuer_name_raw": issuer_name,
        "expected_quote_name": expected_name, "identity_verified_in_pdf": identity_ok,
        "quote_name_exact_substring_match": quote_name_match,
        "pdf_exact_code_pages": exact_code_occurrences,
        "industry": industry, "classification_standard": standard,
        "classification_asof_date": None,
        "qualification": "exact_code_company_industry_and_GB_T_4754_2017_present" if qualified else "unqualified",
        "evidence_excerpts": evidence,
    }


def make_classification_row(*, code: str, org_id: str, quote_name: str,
                            doc: dict, pdf_receipt: dict, parsed: dict,
                            query_complete: bool, index_count: int,
                            index_receipts: list[dict], index_reused: bool,
                            index_partial: bool = False) -> dict:
    return {"official_code": code, "org_id": org_id,
        "display_name_from_quote_packet": quote_name,
        "classification_system": parsed["classification_standard"]["system_name"],
        "standard_id": parsed["classification_standard"]["standard_id_normalized"],
        "standard_id_raw": parsed["classification_standard"]["standard_id_raw"],
        "classification_asof_date": None,
        "industry_code_raw": parsed["industry"]["code"],
        "industry_label_raw": parsed["industry"]["label"],
        "issuer_name_raw": parsed["issuer_name_raw"],
        "index_response_security_code": doc.get("security_code"),
        "index_response_org_id": doc.get("response_org_id"),
        "index_response_security_name_raw": doc.get("response_security_name_raw"),
        "quote_name_exact_substring_match": parsed["quote_name_exact_substring_match"],
        "announcement_id": doc["announcement_id"], "announcement_title": doc["title"],
        "published_date": doc["published_date"], "document_url": doc["document_url"],
        "index_receipt_id": doc["index_receipt_id"],
        "index_content_hash": doc["index_content_hash"],
        "pdf_receipt_id": pdf_receipt["receipt_id"],
        "pdf_content_hash": pdf_receipt["content_hash"],
        "pdf_page_count": parsed["page_count"],
        "pdf_exact_code_pages": parsed["pdf_exact_code_pages"],
        "industry_field_page": parsed["industry"]["page"],
        "evidence_excerpts": parsed["evidence_excerpts"],
        "query_complete": query_complete, "index_announcement_count": index_count,
        "index_partial_page_only": index_partial,
        "index_receipts": index_receipts, "index_reused": index_reused}


def index_entry_dict(entry: CrossCategoryIndexEntry) -> dict:
    published = datetime.fromtimestamp(entry.announcement_time_ms / 1000, tz=timezone.utc).astimezone(BEIJING)
    return {
        "announcement_id": entry.announcement_id, "security_code": entry.sec_code,
        "title": entry.title, "published_at_ms": entry.announcement_time_ms,
        "published_date": published.date().isoformat(), "document_url": entry.document_url,
        "index_page": entry.page_num, "index_receipt_id": entry.page_receipt_id,
        "index_content_hash": entry.page_content_hash,
    }


def entries_from_archived_pages(code: str, org_id: str, pages: list[dict], archive: ForwardArchive) -> list[dict]:
    """Parse only raw successful pages already received; incomplete does not mean exhaustive."""
    entries = []
    seen = set()
    for page in pages:
        digest = page.get("content_hash")
        if not digest or not archive.verify(digest):
            continue
        try:
            payload = json.loads(archive.load_bytes(digest))
        except (ValueError, TypeError):
            continue
        for item in payload.get("announcements") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("secCode") or "").strip() != code:
                continue
            response_org = str(item.get("orgId") or "").strip()
            if response_org and response_org != org_id:
                continue
            announcement_id = str(item.get("announcementId") or "").strip()
            adjunct = str(item.get("adjunctUrl") or "").strip()
            if not announcement_id or not adjunct or announcement_id in seen:
                continue
            seen.add(announcement_id)
            millis = int(item.get("announcementTime") or 0)
            published = datetime.fromtimestamp(millis / 1000, tz=timezone.utc).astimezone(BEIJING)
            entries.append({"announcement_id": announcement_id, "security_code": code,
                "response_org_id": response_org,
                "response_security_name_raw": str(item.get("secName") or ""),
                "title": str(item.get("announcementTitle") or "").strip(),
                "published_at_ms": millis, "published_date": published.date().isoformat(),
                "document_url": f"https://{STATIC_HOST}/{adjunct.lstrip('/')}",
                "index_page": page.get("page_num", page.get("page")), "index_receipt_id": page["receipt_id"],
                "index_content_hash": digest})
    return entries


def cached_pdf(url: str, archive: ForwardArchive) -> tuple[bytes, dict] | None:
    row = archive.con.execute(
        "SELECT receipt_id,url,requested_at,responded_at,http_status,content_hash,byte_size,detail "
        "FROM fetch_receipt WHERE source_id='cninfo' AND url=? AND outcome='OK' "
        "AND http_status=200 ORDER BY requested_at DESC LIMIT 1", (url,)).fetchone()
    if not row or not archive.verify(row["content_hash"]):
        return None
    raw = archive.load_bytes(row["content_hash"])
    if len(raw) != row["byte_size"] or not raw.startswith(b"%PDF"):
        return None
    return raw, {key: row[key] for key in row.keys()}


def receipt_record(archive: ForwardArchive, receipt_id: str | None) -> dict | None:
    if not receipt_id:
        return None
    row = archive.con.execute(
        "SELECT receipt_id,source_id,url,outcome,requested_at,responded_at,http_status,content_hash,byte_size,detail "
        "FROM fetch_receipt WHERE receipt_id=?", (receipt_id,)).fetchone()
    return {key: row[key] for key in row.keys()} if row else None


def cached_index(code: str, archive: ForwardArchive) -> dict | None:
    """Reuse only a verified, complete same-day cross-category response for the exact window."""
    detail_prefix = f"cross-category:{code}:{WINDOW_START.isoformat()}~{WINDOW_END.isoformat()}:page="
    rows = archive.con.execute(
        "SELECT receipt_id,url,requested_at,responded_at,http_status,content_hash,byte_size,detail "
        "FROM fetch_receipt WHERE source_id='cninfo' AND outcome='OK' AND http_status=200 "
        "AND url LIKE '%hisAnnouncement/query' AND detail LIKE ? ORDER BY requested_at",
        (detail_prefix + "%",)).fetchall()
    pages_by_number = {}
    for row in rows:
        if not archive.verify(row["content_hash"]):
            return None
        match = re.search(r"page=(\d+):request=(sha256:[0-9a-f]{64})", row["detail"] or "")
        if not match:
            return None
        req_hash = match.group(2)
        if not archive.verify(req_hash):
            return None
        payload = json.loads(archive.load_bytes(row["content_hash"]))
        page_item = {"page": int(match.group(1)), "request_body_hash": req_hash,
                      "receipt_id": row["receipt_id"], "content_hash": row["content_hash"],
                      "http_status": row["http_status"], "rows": payload.get("announcements") or [],
                      "has_more": payload.get("hasMore"), "total": payload.get("totalAnnouncement"),
                      "requested_at": row["requested_at"], "responded_at": row["responded_at"]}
        number = page_item["page"]
        previous = pages_by_number.get(number)
        if previous and (previous["content_hash"] != page_item["content_hash"]
                         or previous["request_body_hash"] != page_item["request_body_hash"]):
            return None
        pages_by_number[number] = page_item
    pages = [pages_by_number[k] for k in sorted(pages_by_number)]
    if not pages or [p["page"] for p in pages] != list(range(1, len(pages) + 1)):
        return None
    if len({p["content_hash"] for p in pages}) != len(pages):
        return None
    total = pages[0]["total"]
    if pages[-1]["has_more"] not in (False, "false", 0) and not (total is not None and sum(len(p["rows"]) for p in pages) >= int(total)):
        return None
    entries = []
    for page in pages:
        for item in page["rows"]:
            if str(item.get("secCode") or "").strip() != code:
                continue
            millis = int(item.get("announcementTime") or 0)
            entries.append({"announcement_id": str(item.get("announcementId") or ""),
                "security_code": code, "response_org_id": str(item.get("orgId") or ""),
                "response_security_name_raw": str(item.get("secName") or ""),
                "title": str(item.get("announcementTitle") or ""),
                "published_at_ms": millis,
                "published_date": datetime.fromtimestamp(millis / 1000, tz=timezone.utc).astimezone(BEIJING).date().isoformat(),
                "document_url": f"https://{STATIC_HOST}/{str(item.get('adjunctUrl') or '').lstrip('/')}",
                "index_page": page["page"], "index_receipt_id": page["receipt_id"],
                "index_content_hash": page["content_hash"]})
    return {"pages": pages, "entries": entries, "complete": True,
            "termination": "verified_archived_hasMore_false_or_total_reached", "total": total,
            "reused": True}


def keyword_search(client: CninfoClient, archive: ForwardArchive, *, code: str,
                   org_id: str, keyword: str) -> dict:
    """One bounded exact-code CNINFO title search, preserving request and response bytes."""
    body = urllib.parse.urlencode({
        "pageNum": 1, "pageSize": 30, "column": "bj", "tabName": "fulltext",
        "plate": "", "stock": f"{code},{org_id}", "searchkey": keyword,
        "secid": "", "category": "", "trade": "",
        "seDate": f"{WINDOW_START.isoformat()}~{WINDOW_END.isoformat()}",
        "sortName": "", "sortType": "", "isHLtitle": "true",
    }).encode("utf-8")
    request_hash, _ = archive.store_bytes(body, media_type="application/x-www-form-urlencoded")
    label = f"research-industry-missing-keyword:{code}:{keyword}:request={request_hash}"
    cached_receipt = archive.con.execute(
        "SELECT receipt_id,content_hash FROM fetch_receipt WHERE source_id='cninfo' AND url=? "
        "AND outcome='OK' AND http_status=200 AND detail=? ORDER BY requested_at DESC LIMIT 1",
        (REPORT_QUERY_URL, label)).fetchone()
    if cached_receipt:
        if (archive.verify(request_hash) and archive.verify(cached_receipt["content_hash"])
                and archive.load_bytes(request_hash) == body):
            response_bytes = archive.load_bytes(cached_receipt["content_hash"])
            receipt = receipt_record(archive, cached_receipt["receipt_id"]) or {}
            receipt["reused_from_verified_archive"] = True
            outcome_ok = True
            outcome_id = cached_receipt["receipt_id"]
            outcome_hash = cached_receipt["content_hash"]
            outcome_detail = None
        else:
            cached_receipt = None
    if not cached_receipt:
        outcome = client._fetch(REPORT_QUERY_URL, label=label, data=body,
            content_type="application/x-www-form-urlencoded; charset=UTF-8")
        receipt = receipt_record(archive, outcome.receipt_id) or {
            "receipt_id": outcome.receipt_id, "content_hash": outcome.content_hash,
            "url": REPORT_QUERY_URL, "http_status": outcome.http_status,
        }
        response_bytes = outcome.payload
        outcome_ok, outcome_id, outcome_hash, outcome_detail = (
            outcome.ok, outcome.receipt_id, outcome.content_hash, outcome.detail)
    receipt["request_body_hash"] = request_hash
    if not outcome_ok or response_bytes is None or not outcome_hash:
        return {"complete": False, "keyword": keyword, "entries": [], "receipt": receipt,
                "error": outcome_detail or "query failed", "termination": "request_failed"}
    try:
        payload = json.loads(response_bytes)
        raw_rows = payload.get("announcements") or []
        if not isinstance(raw_rows, list):
            raise ValueError("announcements field is not a list")
    except (ValueError, TypeError) as exc:
        return {"complete": False, "keyword": keyword, "entries": [], "receipt": receipt,
                "error": f"invalid JSON response: {exc}", "termination": "invalid_response"}
    entries = []
    identity_errors = []
    for row in raw_rows:
        if not isinstance(row, dict):
            identity_errors.append("non-object row")
            continue
        row_code = str(row.get("secCode") or "").strip()
        row_org = str(row.get("orgId") or "").strip()
        if row_code != code or row_org != org_id:
            identity_errors.append(f"response identity mismatch code={row_code!r} org={row_org!r}")
            continue
        announcement_id = str(row.get("announcementId") or "").strip()
        adjunct = str(row.get("adjunctUrl") or "").strip()
        if not announcement_id or not adjunct:
            identity_errors.append("missing announcement id or document URL")
            continue
        try:
            millis = int(row.get("announcementTime"))
            published = datetime.fromtimestamp(millis / 1000, tz=timezone.utc).astimezone(BEIJING)
        except (TypeError, ValueError, OverflowError):
            identity_errors.append("invalid announcement timestamp")
            continue
        entries.append({"announcement_id": announcement_id, "security_code": code,
            "response_org_id": row_org,
            "response_security_name_raw": str(row.get("secName") or ""),
            "title": normalize_title(row.get("announcementTitle")),
            "published_at_ms": millis, "published_date": published.date().isoformat(),
            "document_url": f"https://{STATIC_HOST}/{adjunct.lstrip('/')}",
            "index_page": 1, "index_receipt_id": outcome_id,
            "index_content_hash": outcome_hash})
    total = payload.get("totalAnnouncement")
    try:
        total = int(total) if total is not None else None
    except (TypeError, ValueError):
        total = None
    has_more = payload.get("hasMore")
    if isinstance(has_more, str):
        has_more = has_more.strip().lower() == "true" if has_more.strip().lower() in {"true", "false"} else None
    complete = (has_more is False or (total is not None and len(raw_rows) >= total))
    return {"complete": complete, "keyword": keyword, "entries": entries,
            "receipt": receipt, "total": total, "raw_row_count": len(raw_rows),
            "has_more": has_more, "identity_errors": identity_errors,
            "termination": "hasMore_false_or_total_reached" if complete else "single_page_only"}


def choose_documents(entries: list[dict]) -> list[dict]:
    desired = [x for x in entries if "发行公告" in x["title"]
               and "发行结果" not in x["title"] and "风险特别" not in x["title"]]
    desired.sort(key=lambda x: ("上市发行公告" not in x["title"],
                                -int(x["published_date"].replace("-", "")),
                                x["announcement_id"]))
    if desired:
        return desired[:MAX_PDFS_PER_CODE]
    prospectus = [x for x in entries if "招股说明书" in x["title"]]
    prospectus.sort(key=lambda x: (x["published_date"], x["announcement_id"]), reverse=True)
    return prospectus[:MAX_PDFS_PER_CODE]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=31, help="small sample limit; default all 31")
    args = parser.parse_args()
    if not 1 <= args.limit <= 31:
        raise SystemExit("--limit must be between 1 and 31")
    codes, org_ids, names = load_targets()
    codes = codes[:args.limit]
    prepared = now_iso()
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("AQUANT_TRUSTED_PROXY_NETWORKS", "198.18.0.0/15,2001:2::/64")
    rows_out: list[dict] = []
    unresolved: list[dict] = []
    source_receipts: dict[str, dict] = {}
    consecutive_failures = 0
    query_client = None
    with connect(ARCHIVE_ROOT / "meta.sqlite") as con:
        archive = ForwardArchive(con, ARCHIVE_ROOT)
        source_receipts["official_universe_org_map"] = {
            "receipt_id": json.loads(ORG_MAP.read_text(encoding="utf-8" )).get("receipt_id"),
            "source_url": json.loads(ORG_MAP.read_text(encoding="utf-8" )).get("source_url"),
            "content_hash": json.loads(ORG_MAP.read_text(encoding="utf-8" )).get("content_hash"),
        }
        org_receipt = receipt_record(archive, source_receipts["official_universe_org_map"]["receipt_id"])
        source_receipts["official_universe_org_map"]["verified"] = bool(
            org_receipt and org_receipt["outcome"] == "OK" and org_receipt["http_status"] == 200
            and org_receipt["content_hash"] == source_receipts["official_universe_org_map"]["content_hash"]
            and archive.verify(org_receipt["content_hash"]))
        if not source_receipts["official_universe_org_map"]["verified"]:
            raise RuntimeError("official universe orgId map receipt/hash does not verify")
        query_client = CninfoClient(archive,
            limiter=RateLimiter(min_interval_seconds=2.5),
            breaker=CircuitBreaker(failure_threshold=3, cooldown_seconds=120.0),
            retry=RetryPolicy(max_attempts=1, base_delay_seconds=0, max_delay_seconds=0),
            timeout=25.0)
        for ix, code in enumerate(codes, 1):
            org_id, quote_name = org_ids[code], names[code]
            cached = cached_index(code, archive)
            if cached:
                index = cached
                expected_org = org_id
                index_pages = index["pages"]
                all_entries = index["entries"]
                # A cached query has a verified code-filtered response. The pre-existing official org map
                # remains the orgId authority; the query body includes the same exact code/orgId.
                if any(urllib.parse.parse_qs(
                    archive.load_bytes(p["request_body_hash"]).decode("utf-8", "replace")
                    ).get("stock", [""])[0] != f"{code},{org_id}" for p in index_pages):
                    unresolved.append({"official_code": code, "org_id": org_id, "query_complete": False,
                        "reason": "cached_request_identity_mismatch", "index_pages": index_pages})
                    continue
                index_receipts = [{k: p[k] for k in ("receipt_id", "content_hash", "request_body_hash", "requested_at", "responded_at")} for p in index_pages]
                for p in index_pages:
                    source_receipts[f"index_{code}_page_{p['page']}"] = p
                lookup = archive.con.execute(
                    "SELECT receipt_id FROM fetch_receipt WHERE source_id='cninfo' AND outcome='OK' "
                    "AND url LIKE '%topSearch/query?keyWord=' || ? || '&maxNum=10' "
                    "ORDER BY requested_at DESC LIMIT 1", (code,)).fetchone()
                if lookup:
                    source_receipts[f"cninfo_org_lookup_{code}"] = receipt_record(archive, lookup["receipt_id"])
            else:
                keyword_results = []
                all_entries = []
                for keyword in ("发行公告", "招股说明书"):
                    result = keyword_search(query_client, archive, code=code,
                                            org_id=org_id, keyword=keyword)
                    keyword_results.append(result)
                    source_receipts[f"index_{code}_{keyword}"] = result["receipt"]
                    all_entries.extend(result["entries"])
                    if choose_documents(all_entries):
                        break
                # Exact-code title queries are intentionally bounded to one page each.
                # If the source reports more results, use a matching captured document
                # only as a partial observation and retain query_complete=false.
                index_complete = all(item["complete"] for item in keyword_results)
                index_receipts = [{"keyword": item["keyword"], **item["receipt"],
                    "total_announcement": item.get("total"),
                    "raw_row_count": item.get("raw_row_count"),
                    "has_more": item.get("has_more"),
                    "termination": item.get("termination"),
                    "identity_errors": item.get("identity_errors", []),
                    "error": item.get("error")}
                    for item in keyword_results]
                index = {"complete": index_complete,
                    "termination": ("all_keyword_queries_complete" if index_complete
                                    else "single_page_keyword_query_or_failure"),
                    "total": sum(item.get("total") or 0 for item in keyword_results),
                    "reused": any(item["receipt"].get("reused_from_verified_archive")
                                  for item in keyword_results),
                    "partial_page_only": not index_complete}
                if not all_entries and all(item.get("error") for item in keyword_results):
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        unresolved.append({"official_code": code, "org_id": org_id,
                            "query_complete": False, "reason": "bounded_CNINFO_keyword_queries_failed",
                            "index_receipts": index_receipts})
                        break
                expected_org = org_id
            consecutive_failures = 0
            documents = choose_documents(all_entries)
            parsed_documents = []
            for doc in documents:
                cached_pdf_value = cached_pdf(doc["document_url"], archive)
                if cached_pdf_value:
                    pdf_bytes, pdf_receipt = cached_pdf_value
                    pdf_receipt = {**pdf_receipt, "reused_from_verified_archive": True}
                else:
                    fetched = query_client.document(doc["document_url"],
                        label=f"research-industry-missing:{code}:{doc['announcement_id']}", max_bytes=PDF_LIMIT)
                    if not fetched.ok or fetched.payload is None:
                        parsed_documents.append({"announcement": doc, "pdf_receipt_id": fetched.receipt_id,
                            "pdf_content_hash": fetched.content_hash, "extraction_status": "fetch_failed",
                            "error": fetched.detail})
                        consecutive_failures += 1
                        break
                    pdf_bytes = fetched.payload
                    receipt_row = archive.con.execute(
                        "SELECT receipt_id,url,requested_at,responded_at,http_status,content_hash,byte_size "
                        "FROM fetch_receipt WHERE receipt_id=?", (fetched.receipt_id,)).fetchone()
                    pdf_receipt = {key: receipt_row[key] for key in receipt_row.keys()} if receipt_row else {
                        "receipt_id": fetched.receipt_id, "url": doc["document_url"],
                        "content_hash": fetched.content_hash, "http_status": fetched.http_status,
                        "byte_size": len(pdf_bytes)}
                source_receipts[f"pdf_{code}_{doc['announcement_id']}"] = pdf_receipt
                parsed = parse_industry_pdf(pdf_bytes, code=code, expected_name=quote_name)
                parsed_documents.append({"announcement": doc, "pdf_receipt": pdf_receipt,
                    "extraction_status": parsed["qualification"], **parsed})
                if parsed["qualification"] == "exact_code_company_industry_and_GB_T_4754_2017_present":
                    rows_out.append(make_classification_row(code=code, org_id=org_id,
                        quote_name=quote_name, doc=doc, pdf_receipt=pdf_receipt, parsed=parsed,
                        query_complete=index["complete"], index_count=len(all_entries),
                        index_receipts=index_receipts, index_reused=index.get("reused", False),
                        index_partial=index.get("partial_page_only", False)))
                    break
            if not any(row["official_code"] == code for row in rows_out) and not any(
                    item.get("keyword") == "招股说明书" for item in index_receipts):
                fallback = keyword_search(query_client, archive, code=code, org_id=org_id,
                                          keyword="招股说明书")
                source_receipts[f"index_{code}_招股说明书"] = fallback["receipt"]
                fallback_receipt = {"keyword": fallback["keyword"], **fallback["receipt"],
                    "total_announcement": fallback.get("total"),
                    "raw_row_count": fallback.get("raw_row_count"),
                    "has_more": fallback.get("has_more"),
                    "termination": fallback.get("termination"),
                    "identity_errors": fallback.get("identity_errors", []),
                    "error": fallback.get("error")}
                index_receipts.append(fallback_receipt)
                fallback_docs = [doc for doc in fallback["entries"] if "招股说明书" in doc["title"]][:MAX_PDFS_PER_CODE]
                for doc in fallback_docs:
                    cached_pdf_value = cached_pdf(doc["document_url"], archive)
                    if cached_pdf_value:
                        pdf_bytes, pdf_receipt = cached_pdf_value
                        pdf_receipt = {**pdf_receipt, "reused_from_verified_archive": True}
                    else:
                        fetched = query_client.document(doc["document_url"],
                            label=f"research-industry-missing:{code}:{doc['announcement_id']}",
                            max_bytes=PDF_LIMIT)
                        if not fetched.ok or fetched.payload is None:
                            parsed_documents.append({"announcement": doc,
                                "pdf_receipt_id": fetched.receipt_id,
                                "pdf_content_hash": fetched.content_hash,
                                "extraction_status": "fetch_failed", "error": fetched.detail})
                            continue
                        pdf_bytes = fetched.payload
                        receipt_row = receipt_record(archive, fetched.receipt_id)
                        pdf_receipt = receipt_row or {"receipt_id": fetched.receipt_id,
                            "url": doc["document_url"], "content_hash": fetched.content_hash,
                            "http_status": fetched.http_status, "byte_size": len(pdf_bytes)}
                    source_receipts[f"pdf_{code}_{doc['announcement_id']}"] = pdf_receipt
                    parsed = parse_industry_pdf(pdf_bytes, code=code, expected_name=quote_name)
                    parsed_documents.append({"announcement": doc, "pdf_receipt": pdf_receipt,
                        "extraction_status": parsed["qualification"], **parsed})
                    if parsed["qualification"] == "exact_code_company_industry_and_GB_T_4754_2017_present":
                        rows_out.append(make_classification_row(code=code, org_id=org_id,
                            quote_name=quote_name, doc=doc, pdf_receipt=pdf_receipt, parsed=parsed,
                            query_complete=index["complete"] and fallback["complete"],
                            index_count=len(all_entries) + len(fallback["entries"]),
                            index_receipts=index_receipts,
                            index_reused=(index.get("reused", False)
                                or bool(fallback["receipt"].get("reused_from_verified_archive"))),
                            index_partial=(index.get("partial_page_only", False) or not fallback["complete"])))
                        break
            if not any(row["official_code"] == code for row in rows_out):
                unresolved.append({"official_code": code, "org_id": org_id, "display_name": quote_name,
                    "query_complete": index["complete"], "index_termination": index.get("termination"),
                    "index_partial_page_only": index.get("partial_page_only", False),
                    "index_announcement_count": len(all_entries), "index_receipts": index_receipts,
                    "documents_examined": parsed_documents,
                    "reason": "no_selected_official_document_with_exact_identity_taxonomy_and_industry_fields"})
            CHECKPOINT.open("a", encoding="utf-8").write(json.dumps({
                "schema": "aquant.research_industry_missing_checkpoint.v1", "prepared_at": prepared,
                "code": code, "org_id": org_id, "index_complete": index["complete"],
                "index_reused": index.get("reused", False), "index_receipts": index_receipts,
                "documents_examined": parsed_documents,
                "status": "qualified" if any(row["official_code"] == code for row in rows_out) else "unresolved"
            }, ensure_ascii=False) + "\n")
            print(json.dumps({"progress": f"{ix}/{len(codes)}", "code": code,
                "matched": sum(row["official_code"] == code for row in rows_out),
                "unresolved": sum(row["official_code"] == code for row in unresolved),
                "index_count": len(all_entries), "index_reused": index.get("reused", False)}, ensure_ascii=False), flush=True)

    output = {"schema": "aquant.research_industry_missing.v1", "prepared_at": prepared,
        "generated_at": now_iso(), "requested_codes": codes,
        "scope": {"base_packet": str(CAPCO_PACKET), "base_missing_count": len(codes),
            "matching_rule": "仅按官方六位证券代码精确匹配；orgId须与官方证券清单一致。",
            "query_window": {"from": WINDOW_START.isoformat(), "to": WINDOW_END.isoformat()},
            "index_page_size": PAGE_SIZE, "index_max_pages_per_code": MAX_PAGES,
            "max_pdfs_per_code": MAX_PDFS_PER_CODE,
            "qualified_count": len({r["official_code"] for r in rows_out}),
            "unresolved_count": len({r["official_code"] for r in unresolved})},
        "classification_policy": "只记录PDF原文明确给出的分类系统、标准、代码和名称；本包GB/T 4754-2017不等同于CSRC或CAPCO分类，未推断映射。",
        "sources": source_receipts,
        "rows": rows_out, "unresolved": unresolved}
    timestamps = [value.get(key) for value in source_receipts.values() if isinstance(value, dict)
                  for key in ("requested_at", "responded_at") if value.get(key)]
    output["captured_from"] = min(timestamps) if timestamps else None
    output["captured_to"] = max(timestamps) if timestamps else None
    write_json(OUTPUT, output)
    print(json.dumps({"output": str(OUTPUT), "qualified": output["scope"]["qualified_count"],
        "unresolved": output["scope"]["unresolved_count"], "generated_at": output["generated_at"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
