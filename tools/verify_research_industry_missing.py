"""Admit bounded CNINFO industry disclosures against their original local evidence."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, urlparse

from capture_research_industry_missing import (
    BEIJING, REPORT_QUERY_URL, STATIC_HOST, parse_industry_pdf,
)
from aquant.adapters.providers.cninfo_probe import normalize_title
from verify_research_market_inputs import _file, _hash, _require, _time

QUALIFIED = "exact_code_company_industry_and_GB_T_4754_2017_present"


def _original(con, root, digest):
    artifact = con.execute("SELECT * FROM raw_artifact WHERE content_hash=?", (digest,)).fetchone()
    _require(artifact is not None, "disclosure original artifact missing")
    body = _file(root, artifact["stored_path"]).read_bytes()
    _require(_hash(body) == digest and len(body) == artifact["byte_size"], "disclosure original hash/bytes mismatch")
    return body


def _receipt(con, root, metadata, *, identity=False):
    row = con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?", (metadata.get("receipt_id"),)).fetchone()
    _require(row is not None and row["source_id"] == "cninfo", "disclosure source receipt absent/wrong source")
    # Older cached receipts intentionally carry fewer fields. Every carried field
    # must still agree with SQL; SQL remains authoritative for omitted metadata.
    for key in ("source_id", "url", "outcome", "requested_at", "responded_at", "first_seen_at", "http_status", "content_hash", "byte_size", "detail"):
        if key in metadata:
            _require(metadata[key] == row[key], f"disclosure receipt {key} mismatch")
    for key in ("content_hash",) if identity else ("content_hash", "requested_at", "responded_at", "http_status"):
        _require(key in metadata, f"disclosure receipt missing {key}")
    req, resp = _time(row["requested_at"]), _time(row["responded_at"])
    _require(req <= resp and _time(row["first_seen_at"]) == resp, "disclosure receipt timestamps invalid")
    body = None
    if row["outcome"] == "OK":
        _require(row["http_status"] == 200, "disclosure successful receipt HTTP mismatch")
        body = _original(con, root, row["content_hash"])
        _require(len(body) == row["byte_size"], "disclosure receipt byte count mismatch")
    return dict(row), body


def _more(value):
    if isinstance(value, str):
        return {"true": True, "false": False}.get(value.strip().lower())
    return value


def _query(con, root, metadata, receipt, body, code, org, window):
    _require(receipt["url"] == REPORT_QUERY_URL, "disclosure index URL mismatch")
    digest = metadata.get("request_body_hash")
    _require(digest and f"request={digest}" in (receipt["detail"] or ""), "disclosure index request hash not bound to receipt")
    request = parse_qs(_original(con, root, digest).decode("utf-8"), keep_blank_values=True)
    for key, value in {"stock": f"{code},{org}", "seDate": f"{window['from']}~{window['to']}",
                       "column": "bj", "tabName": "fulltext", "category": ""}.items():
        _require(request.get(key) == [value], f"disclosure query {key} identity/window mismatch")
    page = int(request.get("pageNum", ["0"])[0])
    _require(page >= 1 and int(request.get("pageSize", ["0"])[0]) in (30, 100), "disclosure query page invalid")
    if "page" in metadata:
        _require(metadata["page"] == page and request.get("searchkey") == [""], "disclosure cached page identity mismatch")
    else:
        _require(request.get("searchkey") in (["发行公告"], ["招股说明书"]), "disclosure query keyword mismatch")
    if receipt["outcome"] != "OK":
        return {"entries": [], "complete": False, "total": None, "raw_count": None,
                "has_more": None, "termination": "request_failed", "page": page}
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return {"entries": [], "complete": False, "total": None, "raw_count": None,
                "has_more": None, "termination": "invalid_response", "page": page}
    raw = payload.get("announcements") or []
    _require(isinstance(raw, list), "disclosure query rows invalid")
    total = payload.get("totalAnnouncement")
    total = int(total) if total is not None else None
    more = _more(payload.get("hasMore"))
    for key, expected in {"rows": raw, "total": payload.get("totalAnnouncement"), "has_more": payload.get("hasMore")}.items():
        if key in metadata:
            _require(metadata[key] == expected, f"disclosure cached {key} differs from original")
    entries, errors = [], []
    for item in raw:
        if not isinstance(item, dict):
            errors.append("non-object row")
            continue
        sec, response_org = str(item.get("secCode") or "").strip(), str(item.get("orgId") or "").strip()
        # Code-filtered cached queries can omit orgId; exact archived stock binds
        # that code to the official org map. Keyword responses require both.
        if sec != code or (response_org != org and (response_org or "page" not in metadata)):
            errors.append(f"response identity mismatch code={sec!r} org={response_org!r}")
            continue
        aid, adjunct = str(item.get("announcementId") or "").strip(), str(item.get("adjunctUrl") or "").strip()
        _require(aid and adjunct and not urlparse(adjunct).scheme and not adjunct.startswith("//"), "disclosure index document identity invalid")
        millis = int(item.get("announcementTime"))
        day = datetime.fromtimestamp(millis / 1000, BEIJING).date().isoformat()
        _require(window["from"] <= day <= window["to"], "disclosure publication outside query window")
        entries.append({"announcement_id": aid, "security_code": sec,
            "response_org_id": response_org, "response_security_name_raw": str(item.get("secName") or ""),
            "title": str(item.get("announcementTitle") or "").strip() if "page" in metadata else normalize_title(item.get("announcementTitle")),
            "published_at_ms": millis, "published_date": day,
            "document_url": f"https://{STATIC_HOST}/{adjunct.lstrip('/')}", "index_page": page,
            "index_receipt_id": receipt["receipt_id"], "index_content_hash": receipt["content_hash"]})
    complete = more is False or (total is not None and len(raw) >= total)
    return {"entries": entries, "complete": complete, "total": total, "raw_count": len(raw), "has_more": more,
            "termination": "hasMore_false_or_total_reached" if complete else "single_page_only", "page": page,
            "identity_errors": errors}


def verify_industry_missing(packet, universe, archive_root, *, base_supplement, target_quotes):
    """Verify exact targets and document observations, without claiming exhaustive queries."""
    _require(packet.get("schema") == "aquant.research_industry_missing.v1", "disclosure schema mismatch")
    requested = packet.get("requested_codes", [])
    missing = base_supplement["scope"]["missing_target_codes"]
    _require(requested and len(requested) == len(set(requested)) and set(requested) <= set(missing), "disclosure targets not exact CAPCO missing subset")
    names = {r["official_code"]: r.get("display_name") or r.get("quote_name") or "" for r in target_quotes["rows"]}
    _require(all(re.fullmatch(r"\d{6}", code) and code in universe and names.get(code) for code in requested), "disclosure target identity/name absent")
    scope = packet["scope"]
    # Collector sample packets count the requested subset, rather than all31.
    _require(scope.get("base_missing_count") == len(requested), "disclosure target count mismatch")
    window = scope["query_window"]
    _require(window == {"from": "2025-12-31", "to": "2026-10-09"}, "disclosure frozen query window mismatch")
    _require(scope.get("index_page_size") == 30 and scope.get("index_max_pages_per_code") == 1
             and scope.get("max_pdfs_per_code") == 2, "disclosure bounded query scope mismatch")
    rows, unresolved = packet.get("rows", []), packet.get("unresolved", [])
    codes = [r.get("official_code") for r in rows + unresolved]
    _require(len(codes) == len(set(codes)) and set(codes) == set(requested), "disclosure coverage partition mismatch")
    _require(scope.get("qualified_count") == len(rows) and scope.get("unresolved_count") == len(unresolved), "disclosure coverage counts mismatch")
    root, sources = Path(archive_root), packet.get("sources", {})
    _require("official_universe_org_map" in sources, "disclosure official identity receipt missing")
    receipts, bodies, queries, pdfs, starts, ends = {}, {}, {}, {}, [], []
    with sqlite3.connect((root / "meta.sqlite").resolve().as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        for role, metadata in sources.items():
            identity = role == "official_universe_org_map"
            receipt, body = _receipt(con, root, metadata, identity=identity)
            rid = receipt["receipt_id"]
            _require(rid not in receipts, "disclosure source receipt duplicated")
            receipts[rid], bodies[rid] = receipt, body
            if identity:
                _require(receipt["outcome"] == "OK" and metadata.get("verified") is True
                         and receipt["url"] == "http://www.cninfo.com.cn/new/data/szse_stock.json"
                         and metadata.get("source_url") == receipt["url"], "disclosure org map source mismatch")
                original = {str(r["code"]): str(r["orgId"]) for r in json.loads(body)["stockList"]}
                _require(all(original.get(code) == org for code, org in universe.items()), "disclosure org map identity mismatch")
                # This old identity receipt has no capture timestamps in packet.
                # It is evidence for the join, not this run's observation window.
                _require("requested_at" not in metadata and "responded_at" not in metadata, "disclosure org map incorrectly included in observation window")
                continue
            starts.append(_time(receipt["requested_at"]))
            ends.append(_time(receipt["responded_at"]))
            match = re.fullmatch(r"(index|pdf|cninfo_org_lookup)_(\d{6})(?:_(.+))?", role)
            _require(match is not None and match[2] in requested, "disclosure source role/code mismatch")
            kind, code, suffix = match.groups()
            if kind == "index":
                query = _query(con, root, metadata, receipt, body, code, universe[code], window)
                if "page" in metadata:
                    _require(suffix == f"page_{query['page']}", "disclosure index role page mismatch")
                else:
                    request = parse_qs(_original(con, root, metadata["request_body_hash"]).decode("utf-8"), keep_blank_values=True)
                    _require(request.get("searchkey") == [suffix], "disclosure index role keyword mismatch")
                queries[rid] = (code, query, metadata)
            elif kind == "pdf":
                _require(receipt["outcome"] == "OK" and body and body.startswith(b"%PDF"), "disclosure PDF outcome/original invalid")
                _require(suffix and re.fullmatch(r"\d+", suffix), "disclosure PDF announcement role invalid")
                pdfs[rid] = (code, suffix)
            else:
                parsed = urlparse(receipt["url"])
                _require(receipt["outcome"] == "OK" and parsed.scheme == "https" and parsed.netloc == "www.cninfo.com.cn"
                    and parsed.path == "/new/information/topSearch/query"
                    and parse_qs(parsed.query) == {"keyWord": [code], "maxNum": ["10"]}, "disclosure org lookup request identity mismatch")
                payload = json.loads(body)
                _require(any(str(r.get("code")) == code and str(r.get("orgId")) == universe[code] for r in payload), "disclosure org lookup response identity mismatch")
    _require(starts and _time(packet.get("captured_from")) == min(starts)
        and _time(packet.get("captured_to")) == max(ends), "disclosure capture bounds differ from used receipts")
    _require(_time(packet.get("generated_at")) >= max(ends), "disclosure generation precedes evidence")
    for rid, (code, aid) in pdfs.items():
        _require(any(e["announcement_id"] == aid and e["document_url"] == receipts[rid]["url"]
            for qcode, query, _ in queries.values() if qcode == code for e in query["entries"]),
            "disclosure PDF source role not bound to this security's original index")

    referenced_queries = set()
    for observation in rows + unresolved:
        code = observation["official_code"]
        _require(observation.get("org_id") == universe[code], "disclosure observation org identity mismatch")
        if observation in rows:
            _require(observation.get("display_name_from_quote_packet") == names[code], "disclosure quote name mismatch")
        elif "display_name" in observation:
            _require(observation["display_name"] == names[code], "disclosure unresolved quote name mismatch")
        refs = observation.get("index_receipts", [])
        _require(refs and len({r.get("receipt_id") for r in refs}) == len(refs), "disclosure index receipt references missing/duplicated")
        entries, complete = [], []
        for ref in refs:
            rid = ref.get("receipt_id")
            _require(rid in queries and queries[rid][0] == code, "disclosure index receipt wrong security")
            referenced_queries.add(rid)
            query, metadata = queries[rid][1:]
            _require(all(k in ref for k in ("content_hash", "requested_at", "responded_at", "request_body_hash")),
                     "disclosure referenced index metadata missing")
            for key in ("content_hash", "requested_at", "responded_at", "http_status", "source_id", "outcome", "url", "byte_size", "detail"):
                if key in ref:
                    _require(ref[key] == receipts[rid][key], f"disclosure referenced index {key} mismatch")
            _require(ref.get("request_body_hash") == metadata.get("request_body_hash"), "disclosure referenced query hash mismatch")
            if "keyword" in ref:
                _require(f"index_{code}_{ref['keyword']}" in sources
                         and sources[f"index_{code}_{ref['keyword']}"]["receipt_id"] == rid,
                         "disclosure referenced query keyword mismatch")
                for key, expected in {"total_announcement": query["total"], "raw_row_count": query["raw_count"],
                        "has_more": query["has_more"], "termination": query["termination"], "identity_errors": query.get("identity_errors", [])}.items():
                    _require(ref.get(key) == expected, f"disclosure query {key} differs from original")
            entries.extend(query["entries"])
            complete.append(query["complete"])
        _require(observation.get("query_complete") is all(complete), "disclosure query completeness misrepresented")
        if "index_partial_page_only" in observation:
            _require(observation["index_partial_page_only"] is (not all(complete)), "disclosure partial query flag mismatch")
        if "index_announcement_count" in observation:
            _require(observation["index_announcement_count"] == len(entries), "disclosure announcement count mismatch")
        if observation in rows:
            matching = [e for e in entries if e["announcement_id"] == observation.get("announcement_id")
                and e["index_receipt_id"] == observation.get("index_receipt_id")]
            _require(len(matching) == 1, "disclosure selected document not in exact original index")
            doc = matching[0]
            for key, source_key in {"announcement_title": "title", "published_date": "published_date", "document_url": "document_url",
                 "index_content_hash": "index_content_hash", "index_response_security_code": "security_code",
                 "index_response_org_id": "response_org_id", "index_response_security_name_raw": "response_security_name_raw"}.items():
                _require(observation.get(key) == doc[source_key], f"disclosure selected {key} differs from index")
            rid = observation.get("pdf_receipt_id")
            _require(pdfs.get(rid) == (code, doc["announcement_id"]) and receipts[rid]["url"] == doc["document_url"]
                     and observation.get("pdf_content_hash") == receipts[rid]["content_hash"], "disclosure PDF receipt document/security mismatch")
            parsed = parse_industry_pdf(bodies[rid], code=code, expected_name=names[code])
            _require(parsed["qualification"] == QUALIFIED and parsed["identity_verified_in_pdf"], "disclosure PDF identity/explicit standard/field not qualified")
            expected = {"classification_system": parsed["classification_standard"]["system_name"],
                "standard_id": parsed["classification_standard"]["standard_id_normalized"], "standard_id_raw": parsed["classification_standard"]["standard_id_raw"],
                "industry_code_raw": parsed["industry"]["code"], "industry_label_raw": parsed["industry"]["label"],
                "issuer_name_raw": parsed["issuer_name_raw"], "pdf_page_count": parsed["page_count"],
                "pdf_exact_code_pages": parsed["pdf_exact_code_pages"], "industry_field_page": parsed["industry"]["page"],
                "evidence_excerpts": parsed["evidence_excerpts"], "quote_name_exact_substring_match": parsed["quote_name_exact_substring_match"]}
            for key, value in expected.items():
                _require(observation.get(key) == value, f"disclosure PDF original {key} mismatch")
            _require("classification_asof_date" in observation and observation["classification_asof_date"] is None,
                     "disclosure must not invent historical classification effective date")
            _require(re.fullmatch(r"[A-S]\d{2}", observation["industry_code_raw"]), "disclosure category/major code invalid")
        else:
            for examined in observation.get("documents_examined", []):
                doc = examined["announcement"]
                _require(doc in entries, "disclosure unresolved document differs from index")
                meta = examined.get("pdf_receipt", {})
                rid = meta.get("receipt_id")
                _require(pdfs.get(rid) == (code, doc["announcement_id"]), "disclosure unresolved PDF receipt mismatch")
                _require(receipts[rid]["url"] == doc["document_url"], "disclosure unresolved PDF URL mismatch")
                for key in meta.keys() & receipts[rid].keys():
                    _require(meta[key] == receipts[rid][key], "disclosure unresolved PDF metadata mismatch")
                parsed = parse_industry_pdf(bodies[rid], code=code, expected_name=names[code])
                _require(parsed["qualification"] != QUALIFIED and examined.get("extraction_status") == parsed["qualification"], "disclosure qualified document mislabelled unresolved")
                for key, value in parsed.items():
                    _require(examined.get(key) == value, f"disclosure unresolved original {key} mismatch")
    _require(referenced_queries == set(queries), "disclosure query source omitted from observations")
