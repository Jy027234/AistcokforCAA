"""Replay bounded original-source evidence for unchanged S2 revision fields.

This produces an impact assessment, never a financial fact or an admission.
Unknown relationships and changed comparative amounts do not become NO_EFFECT.
"""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from importlib.metadata import PackageNotFoundError, version
import io
import json
from pathlib import Path
import re
import sqlite3
import sys
import unicodedata
from threading import RLock
from typing import Mapping, Sequence
from urllib.parse import parse_qs, urlsplit

from aquant.artifact_paths import resolve_archive_member, resolve_artifact_path
from aquant.domain.fundamentals.disclosure_link import (
    DisclosureRole, disclosure_role, is_financial_correction_title, report_period_from_title,
)
from .cninfo_probe import market_for_code, normalize_title, title_revision_flags
from .cninfo_statement_sections import (
    extract_revenue_note_decomposition, extract_statement_sections, PRIOR_NOT_ON_LINE,
)
from .pdf_page_parse_corpus import load_pages

SCHEMA = "aquant.s2-machine-revision-impact.v1"
VERIFIER = "machine:s2-revision-impact-v1"
FIELDS = frozenset({"revenue", "net_profit_attributable", "net_profit_consolidated",
                    "operating_cashflow", "parent_equity"})
STATEMENT = {"revenue": "利润表", "net_profit_attributable": "利润表",
             "net_profit_consolidated": "利润表", "operating_cashflow": "现金流量表",
             "parent_equity": "资产负债表"}
SCALE = {"元": Decimal(1), "千元": Decimal(1000), "万元": Decimal(10000), "百万元": Decimal(1000000)}
PERIODS = frozenset({"2024-06-30", "2024-12-31", "2025-06-30", "2025-12-31", "2026-06-30"})
ISSUERS = {
           "000680": "山推工程机械股份有限公司","000902": "新洋丰农业科技股份有限公司", "600338": "西藏珠峰资源股份有限公司",
           "000899": "江西赣能股份有限公司", "002602": "浙江世纪华通集团股份有限公司",
           "600578": "北京京能电力股份有限公司", "601069": "西部黄金股份有限公司",
           "000422": "湖北宜化化工股份有限公司"}
CST = timezone(timedelta(hours=8))

# Complete bytes remain in the key. The fixed 68-original source set is about
# 106 MiB, so a 32-entry text LRU could repeatedly discard valid pure parses.
# These are process-local parsing caches, never receipt or admission caches.
_PARSE_CACHE_VERSION = "revision-pure-parse-v1"
# This candidate copy may be loaded from a task overlay while the unchanged
# verifier dependencies remain in the actual image package. Fingerprint the
# candidate itself and the exact loaded dependency files in either layout.
_ACTUAL_PROVIDER_DIR = Path(extract_statement_sections.__code__.co_filename).resolve().parent
_PARSER_CODE_INPUTS = (Path(__file__).resolve(),
                       Path(__file__).resolve().with_name("pdf_page_parse_corpus.py")) + tuple(
    _ACTUAL_PROVIDER_DIR / name for name in (
        "cninfo_statement_sections.py", "cninfo_statement_geometry.py",
        "cninfo_financial_candidate.py", "cninfo_financial_pdf.py",
        "cninfo_probe.py", "cninfo_report_identity.py",
    )) + (Path(disclosure_role.__code__.co_filename).resolve(),
          Path(resolve_archive_member.__code__.co_filename).resolve())
_MISSING = object()


def _optional_library_version(name):
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _parser_code_fingerprint():
    # Re-hash actual parser/identity dependencies once per assessment. An
    # edited dependency or installed parser version cannot hit an older parse.
    return _hash(_canonical({"version": _PARSE_CACHE_VERSION,
        "code": [(str(path), _hash(path.read_bytes())) for path in _PARSER_CODE_INPUTS],
        "python": {"implementation": sys.implementation.name,
                   "version": ".".join(map(str, sys.version_info[:3]))},
        "unicodeVersion": unicodedata.unidata_version,
        "libraries": {name: version(name) for name in ("pypdf", "pdfplumber")},
        "optionalLibraries": {
            "pypdfium2": _optional_library_version("pypdfium2")}}).encode())


def _parse_memory_size(value, seen=None):
    """Bound retained bytes plus immutable parsed strings/rows, not just count."""
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if isinstance(value, tuple):
        size += sum(_parse_memory_size(item, seen) for item in value)
    elif is_dataclass(value):
        size += sum(_parse_memory_size(getattr(value, item.name), seen) for item in fields(value))
    return size


class _PureParseCache:
    def __init__(self, max_entries, max_bytes):
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.entries, self.bytes = OrderedDict(), 0
        self.hits = self.misses = self.coalesced = 0
        self.lock = RLock()
        self.inflight: dict[object, Future] = {}

    def get(self, key):
        with self.lock:
            found = self.entries.get(key, _MISSING)
            if found is _MISSING:
                self.misses += 1
                return _MISSING
            self.entries.move_to_end(key)
            self.hits += 1
            return found[0]

    def put(self, key, value):
        weight = _parse_memory_size((key, value))
        if weight > self.max_bytes:
            return value
        with self.lock:
            old = self.entries.pop(key, None)
            if old is not None:
                self.bytes -= old[1]
            while self.entries and (len(self.entries) >= self.max_entries or
                                    self.bytes + weight > self.max_bytes):
                _, (_, removed_weight) = self.entries.popitem(last=False)
                self.bytes -= removed_weight
            self.entries[key] = value, weight
            self.bytes += weight
        return value

    def get_or_compute(self, key, compute):
        """Share one pure parse among concurrent misses for the same exact key."""
        with self.lock:
            found = self.entries.get(key, _MISSING)
            if found is not _MISSING:
                self.entries.move_to_end(key)
                self.hits += 1
                return found[0]
            self.misses += 1
            future = self.inflight.get(key)
            owner = future is None
            if owner:
                future = Future()
                self.inflight[key] = future
            else:
                self.coalesced += 1

        if not owner:
            return future.result()

        try:
            value = compute()
            self.put(key, value)
            future.set_result(value)
            return value
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self.lock:
                if self.inflight.get(key) is future:
                    del self.inflight[key]

    def clear(self):
        with self.lock:
            self.entries.clear()
            self.bytes = self.hits = self.misses = self.coalesced = 0

    def info(self):
        with self.lock:
            return {"entries": len(self.entries), "bytes": self.bytes,
                    "hits": self.hits, "misses": self.misses,
                    "coalesced": self.coalesced,
                    "maxEntries": self.max_entries, "maxBytes": self.max_bytes}


# Leave room above the 106 MiB originals for their 5,373 native-text pages.
_page_parse_cache = _PureParseCache(128, 192 * 1024 * 1024)
_section_parse_cache = _PureParseCache(64, 128 * 1024 * 1024)


class RevisionImpactError(ValueError):
    pass


def _need(condition, message):
    if not condition:
        raise RevisionImpactError(message)


def _time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _need(result.tzinfo is not None, "source time must have timezone")
    return result.astimezone(timezone.utc)


def _hash(body):
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class MachineRevisionImpact:
    evidence: dict

    def as_dict(self):
        # No mutable cached result is exposed to another caller.
        result = json.loads(_canonical(self.evidence))
        result["evidenceHash"] = _hash(_canonical(self.evidence).encode())
        return result


def _raw(con, root, digest):
    row = con.execute("SELECT * FROM raw_artifact WHERE content_hash=?", (digest,)).fetchone()
    _need(row is not None, "original artifact missing")
    try:
        path = resolve_archive_member(root, row["stored_path"])
    except ValueError as exc:
        raise RevisionImpactError("original artifact path escapes archive") from exc
    body = path.read_bytes()
    _need(_hash(body) == digest and len(body) == row["byte_size"], "original SHA/byte count mismatch")
    return body


def _receipt(con, root, rid, *, expected_hash=None, expected_url=None):
    row = con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?", (rid,)).fetchone()
    _need(row is not None and row["source_id"] == "cninfo" and row["outcome"] == "OK"
          and row["http_status"] == 200, "CNINFO successful receipt required")
    if expected_hash is not None:
        _need(row["content_hash"] == expected_hash, "receipt hash mismatch")
    if expected_url is not None:
        _need(row["url"] == expected_url, "receipt URL mismatch")
    _need(_time(row["requested_at"]) <= _time(row["responded_at"])
          and _time(row["first_seen_at"]) == _time(row["responded_at"]), "receipt time mismatch")
    body = _raw(con, root, row["content_hash"])
    _need(len(body) == row["byte_size"], "receipt byte count mismatch")
    return dict(row), body


def _financial_title(title):
    # Recall includes corrected full statements with parenthesized title markers.
    return is_financial_correction_title(title) or (
        any(x in title for x in ("更正", "重述", "修订"))
        and any(x in title for x in ("年度报告", "半年度报告", "季度报告", "财务报表")))


_FUNDRAISING_SPECIAL_REPORT_MARKER = "募集资金存放与实际使用情况的专项报告"
_FUNDRAISING_SPECIAL_REPORT_TITLE_VARIANT = "募集资金存放与使用情况的专项报告"


def _is_fundraising_special_report(title, *, issuer_code=None):
    """Keep this funding-use special report related, but out of periodic full-report candidates."""
    compact = re.sub(r"\s+", "", title or "")
    return (_FUNDRAISING_SPECIAL_REPORT_MARKER in compact
            or (issuer_code == "000422" and _FUNDRAISING_SPECIAL_REPORT_TITLE_VARIANT in compact))


def _report_period(title, *, issuer_code=None):
    """Keep global legacy matching and exact aliases, excluding this named special-report class."""
    if issuer_code == "000680" and _is_fundraising_special_report(title):
        return None
    if issuer_code == "000422" and _is_fundraising_special_report(title, issuer_code=issuer_code):
        return None
    parsed = report_period_from_title(title)
    if parsed is not None:
        return parsed
    compact = re.sub(r"\s+", "", title)
    match = re.fullmatch(r"(20\d{2})年年度财务报告", compact)
    return date(int(match[1]), 12, 31) if match else None


def _index(con, root, company):
    code, org = company["code"], company["organizationId"]
    receipt, body = _receipt(con, root, company["orgLookupReceiptId"], expected_hash=company["orgLookupContentHash"])
    url = urlsplit(receipt["url"])
    _need(url.netloc == "www.cninfo.com.cn" and url.path == "/new/information/topSearch/query"
          and parse_qs(url.query) == {"keyWord": [code], "maxNum": ["10"]}, "org lookup request identity mismatch")
    matches = [x for x in json.loads(body) if str(x.get("code")) == code and str(x.get("orgId")) == org]
    _need(len(matches) == 1, "org lookup exact issuer join missing/ambiguous")
    _need(matches[0].get("zwjc") == company["name"], "official issuer name mismatch")
    start, end = company["publicationWindow"]
    _need(date.fromisoformat(start) <= date(2024, 6, 30) and date.fromisoformat(end) >= date(2026, 6, 30),
          "index relationship window does not cover this fixed assessment")
    pages = company["allCategoryPages"]
    _need(pages, "complete archived all-category pages required")
    entries, receipts, request_hashes, count, total, size = {}, [receipt], [], 0, None, None
    for number, page in enumerate(pages, 1):
        _need(page["page"] == number, "index pages not consecutive")
        rec, payload = _receipt(con, root, page["responseReceiptId"], expected_hash=page["responseContentHash"])
        parsed = urlsplit(rec["url"])
        _need(parsed.netloc == "www.cninfo.com.cn" and parsed.path == "/new/hisAnnouncement/query", "index receipt URL mismatch")
        digest = page["requestBodyHash"]
        _need(f"request={digest}" in (rec["detail"] or ""), "index request hash not bound to response")
        request = parse_qs(_raw(con, root, digest).decode(), keep_blank_values=True)
        for key, expected in {"stock": f"{code},{org}", "seDate": f"{start}~{end}", "pageNum": str(number),
                              "category": "", "searchkey": "", "column": market_for_code(code), "tabName": "fulltext"}.items():
            _need(request.get(key) == [expected], f"index request {key} mismatch")
        current_size = int(request["pageSize"][0])
        _need(current_size > 0 and (size is None or size == current_size), "index page size mismatch")
        size = current_size
        obj = json.loads(payload)
        rows = obj["announcements"]
        _need(isinstance(rows, list), "index rows invalid")
        more = obj.get("hasMore")
        if isinstance(more, str):
            more = {"true": True, "false": False}.get(more.lower())
        observed_total = obj.get("totalAnnouncement")
        _need(type(observed_total) is int and observed_total >= 0 and (total is None or total == observed_total), "index total mismatch")
        total = observed_total
        for key, expected in {"httpStatus": rec["http_status"], "rawAnnouncementCount": len(rows),
                              "totalAnnouncement": total, "hasMore": more, "firstSeenAt": rec["first_seen_at"]}.items():
            _need(page.get(key) == expected, f"index page {key} metadata mismatch")
        count += len(rows)
        _need((number == len(pages) and more is False and count == total)
              or (number < len(pages) and more is True and count < total), "index pagination incomplete")
        for item in rows:
            _need(str(item.get("secCode")) == code and str(item.get("orgId")) == org, "index issuer identity mismatch")
            aid = str(item.get("announcementId") or "")
            path = str(item.get("adjunctUrl") or "")
            _need(re.fullmatch(r"\d+", aid) and aid not in entries and not urlsplit(path).scheme and not path.startswith("//"), "index document identity/duplicate mismatch")
            day = datetime.fromtimestamp(int(item["announcementTime"]) / 1000, CST).date().isoformat()
            _need(start <= day <= end, "index row outside requested window")
            _need(re.fullmatch(r"finalpage/" + re.escape(day) + "/" + re.escape(aid) + r"\.PDF", path, re.I),
                  "index PDF URL announcement/date mismatch")
            entry = {"announcementId": aid, "title": normalize_title(item["announcementTitle"]),
                "documentUrl": "https://static.cninfo.com.cn/" + path.lstrip("/"), "publishedDate": day,
                "indexReceiptId": rec["receipt_id"], "indexResponseHash": rec["content_hash"]}
            if code == "000422":
                entry.update({"secCode": str(item["secCode"]), "orgId": str(item["orgId"])})
            entries[aid] = entry
        receipts.append(rec)
        request_hashes.append(digest)
    _need(company["allCategoryTermination"] == "has_more_false" and company["allCategoryTotalAnnouncement"] == total,
          "index declared termination/total mismatch")
    return entries, receipts, request_hashes


def _pages(body, *, code_fingerprint=None):
    key = (code_fingerprint or _parser_code_fingerprint(), body)

    def parse():
        cached_pages = load_pages(body, parser_fingerprint=key[0])
        if cached_pages is not None:
            return cached_pages
        from pypdf import PdfReader
        return tuple(re.sub(r"\s+", " ", page.extract_text() or "").strip()
                     for page in PdfReader(io.BytesIO(body), strict=False).pages)

    return _page_parse_cache.get_or_compute(key, parse)


def _sections(body, period, *, code_fingerprint=None):
    key = (code_fingerprint or _parser_code_fingerprint(), body, period)

    def parse():
        return extract_statement_sections(body, period_end=date.fromisoformat(period), roles=("合并",))

    return _section_parse_cache.get_or_compute(key, parse)


def _cells(body, period, metrics, *, code_fingerprint=None):
    sections, result = _sections(body, period, code_fingerprint=code_fingerprint), {}
    for metric in metrics:
        section = sections.get("合并", STATEMENT[metric])
        row = section.find(metric) if section else None
        if row is None or row.nil_amounts or row.unit not in SCALE:
            if metric == "revenue" and row is None:
                # Some layouts start the consolidated income table on the
                # parent balance sheet's last page. Use explicit table/column
                # headers, never a keyword or report summary alone.
                fallback = _revenue_columns(_pages(body, code_fingerprint=code_fingerprint), period)
                if fallback:
                    result[metric] = fallback
            continue
        prior_period = date(date.fromisoformat(period).year - 1, 12, 31).isoformat() if metric == "parent_equity" else (
            str(date.fromisoformat(period).year - 1) + period[4:])
        result[metric] = {"metric": metric, "statementScope": "CONSOLIDATED", "statement": STATEMENT[metric],
            "factPeriodEnd": period, "columnRole": "CURRENT", "currentHeader": section.current_header,
            "priorHeader": section.prior_header, "priorFactPeriodEnd": prior_period, "unitRaw": row.unit,
            "amountYuan": str(row.amount * SCALE[row.unit]),
            "priorAmountYuan": str(row.prior_amount * SCALE[row.unit]) if row.prior_cell != PRIOR_NOT_ON_LINE else None,
            "currentCellRaw": row.current_cell, "priorCellRaw": row.prior_cell, "rowLabel": row.label,
            "page": row.page, "sourceLine": row.raw_line, "tablePages": [section.start_page, section.end_page]}
    return result


_NATIVE_1654_AMOUNT = re.compile(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}")
_NATIVE_1654_REPORT = {
    "announcementId": "1222991654",
    "contentHash": "sha256:e3dcde5c91deb5ca53eb1936ae5c330f0e2d5846ac4417eb634d8a6e240af05b",
    "documentUrl": "https://static.cninfo.com.cn/finalpage/2025-04-03/1222991654.PDF",
    "receiptId": "rcpt_7b8ef8cf6f6b965a119a697b",
    "title": "2024年年度财务报告",
}


def _native_word_lines(words):
    lines = []
    for word in sorted(words, key=lambda item: (float(item["top"]), float(item["x0"]))):
        top = float(word["top"])
        if not lines or abs(top - lines[-1]["top"]) > 2.5:
            lines.append({"top": top, "words": []})
        lines[-1]["words"].append({
            "text": str(word["text"]), "x0": round(float(word["x0"]), 3),
            "x1": round(float(word["x1"]), 3), "top": round(top, 3),
            "bottom": round(float(word["bottom"]), 3),
        })
    for line in lines:
        line["words"].sort(key=lambda item: item["x0"])
        line["text"] = " ".join(item["text"] for item in line["words"])
    return lines


def _native_word_spans(line, label):
    target = re.sub(r"\s+", "", label)
    words = line["words"]
    found = []
    for start in range(len(words)):
        joined = ""
        for end in range(start, min(len(words), start + 5)):
            joined += re.sub(r"\s+", "", words[end]["text"])
            if joined == target:
                found.append({"x0": words[start]["x0"], "x1": words[end]["x1"],
                              "top": min(word["top"] for word in words[start:end + 1]),
                              "bottom": max(word["bottom"] for word in words[start:end + 1]),
                              "words": [word["text"] for word in words[start:end + 1]]})
                break
            if not target.startswith(joined):
                break
    return found


def _bind_native_1654_current_prior_columns(header_line, revenue_line):
    """Pair cells by native labels and x-coordinate, never by target values."""
    current_headers = _native_word_spans(header_line, "本期金额")
    prior_headers = _native_word_spans(header_line, "上期金额")
    amount_words = [word for word in revenue_line["words"]
                    if _NATIVE_1654_AMOUNT.fullmatch(re.sub(r"\s+", "", word["text"]))]
    amount_words.sort(key=lambda item: item["x0"])
    _need(len(current_headers) == 1 and len(prior_headers) == 1 and len(amount_words) == 2,
          "1654 native geometry must contain one current header, one prior header, and exactly two revenue amounts")
    current_header, prior_header = current_headers[0], prior_headers[0]
    _need(current_header["x0"] < prior_header["x0"] and amount_words[0]["x0"] < amount_words[1]["x0"]
          and abs(current_header["x0"] - amount_words[0]["x0"]) <= 5.0
          and abs(prior_header["x0"] - amount_words[1]["x0"]) <= 5.0,
          "1654 native current/prior labels do not uniquely align with the two revenue cell columns")
    return current_header, prior_header, amount_words


def _native_000422_fy24_1654_revenue_cell(
    body, *, code, target_id, period, metric, announcement_id, document, entry, receipt, pages,
    existing_parser_result=None,
):
    """Resolve only the exact 000422 FY24 revenue report's native CURRENT cell.

    The prior column is retained as an unassigned comparative observation:
    this document prints ``上期金额`` but does not print a 2023 date header.
    """
    _need((code, target_id, period, metric, announcement_id) ==
          ("000422", "1222991648", "2024-12-31", "revenue", "1222991654"),
          "1654 native support is outside its one approved issuer/target/period/metric tuple")
    metadata = document
    _need(metadata.get("code") == "000422" and metadata.get("instrumentId") == "SZ.000422"
          and metadata.get("periodEnd") == "2024-12-31"
          and metadata.get("contentHash") == _NATIVE_1654_REPORT["contentHash"]
          and metadata.get("documentUrl") == _NATIVE_1654_REPORT["documentUrl"]
          and metadata.get("title") == _NATIVE_1654_REPORT["title"]
          and metadata.get("receipt", {}).get("receipt_id") == _NATIVE_1654_REPORT["receiptId"],
          "1654 metadata is not bound to the exact index/receipt/report pin")
    _need(entry.get("announcementId") == _NATIVE_1654_REPORT["announcementId"]
          and entry.get("title") == _NATIVE_1654_REPORT["title"]
          and entry.get("publishedDate") == "2025-04-03"
          and entry.get("documentUrl") == _NATIVE_1654_REPORT["documentUrl"]
          and entry.get("secCode") == "000422" and entry.get("orgId") == "gssz0000422"
          and receipt.get("receipt_id") == _NATIVE_1654_REPORT["receiptId"]
          and receipt.get("content_hash") == _NATIVE_1654_REPORT["contentHash"]
          and receipt.get("url") == _NATIVE_1654_REPORT["documentUrl"]
          and receipt.get("source_id") == "cninfo" and receipt.get("outcome") == "OK"
          and receipt.get("http_status") == 200,
          "1654 current issuer index/receipt identity is not exact")
    _need(_hash(body) == _NATIVE_1654_REPORT["contentHash"] and body.startswith(b"%PDF-"),
          "1654 current native support bytes differ from the pinned original")
    compact_cover = "".join(re.sub(r"\s+", "", page) for page in pages[:5])
    _need("湖北宜化化工股份有限公司" in compact_cover and "二〇二四年度财务报告" in compact_cover
          and "2024年01月01日至2024年12月31日止" in compact_cover,
          "1654 native cover does not establish the exact issuer and FY24 report interval")
    candidate_pages = [index for index, text in enumerate(pages[:20])
                       if "合并利润表" in re.sub(r"\s+", "", text)
                       and "其中：营业收入" in re.sub(r"\s+", "", text)]
    _need(len(candidate_pages) == 1,
          "1654 native text does not locate exactly one consolidated income statement revenue page")
    page_number = candidate_pages[0] + 1
    page_text = pages[candidate_pages[0]]
    compact_page = re.sub(r"\s+", "", page_text)
    _need("湖北宜化化工股份有限公司" in compact_page and "合并利润表" in compact_page
          and "2024年度" in compact_page and "金额单位均为人民币元" in compact_page,
          "1654 native statement page lacks exact issuer/title/FY24 caption/yuan unit")

    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(body)) as pdf:
            page = pdf.pages[candidate_pages[0]]
            lines = _native_word_lines(page.extract_words())
            header_lines = [line for line in lines
                            if "项目" in re.sub(r"\s+", "", line["text"])
                            and _native_word_spans(line, "本期金额")
                            and _native_word_spans(line, "上期金额")]
            revenue_lines = [line for line in lines
                             if "其中：营业收入" in re.sub(r"\s+", "", line["text"])]
            _need(len(header_lines) == 1 and len(revenue_lines) == 1,
                  "1654 native geometry does not isolate one labeled header and revenue row")
            header_line, revenue_line = header_lines[0], revenue_lines[0]
            current_header, prior_header, amount_words = _bind_native_1654_current_prior_columns(
                header_line, revenue_line)
            current_raw = re.sub(r"\s+", "", amount_words[0]["text"])
            prior_raw = re.sub(r"\s+", "", amount_words[1]["text"])
            current_amount = Decimal(current_raw.replace(",", ""))
            prior_amount = Decimal(prior_raw.replace(",", ""))
            geometry = {
                "pageSizePoints": [round(float(page.width), 3), round(float(page.height), 3)],
                "headingLine": next(line for line in lines if "合并利润表" in line["text"]),
                "periodLine": next(line for line in lines if "2024年度" in line["text"]),
                "unitLine": next(line for line in lines if "人民币元" in line["text"]),
                "headerLine": header_line,
                "revenueLine": revenue_line,
                "currentHeaderBox": current_header,
                "priorHeaderBox": prior_header,
                "currentAmountBox": amount_words[0],
                "priorAmountBox": amount_words[1],
            }
    except RevisionImpactError:
        raise
    except Exception as exc:  # noqa: BLE001 - unknown geometry failure must keep the source unresolved
        raise RevisionImpactError(f"1654 native geometry diagnostic failed: {type(exc).__name__}: {exc}") from exc

    note = extract_revenue_note_decomposition(body, period_end=date(2024, 12, 31), default_unit="元")
    note_evidence = note.as_evidence()
    note_pages = {note.heading_page, note.main_business.page, note.other_business.page, note.total.page}
    note_total = note.total.amount * SCALE[note.unit]
    _need(note.pdf_sha256 == _NATIVE_1654_REPORT["contentHash"] and note.unit == "元"
          and note.current_revenue_yuan == current_amount and note_total == current_amount
          and page_number not in note_pages,
          "1654 current revenue does not pass the existing independent note decomposition arithmetic")
    native_support = {
        "schema": "aquant.000422.fy24-report1654-native-current-cell.v1",
        "announcementId": _NATIVE_1654_REPORT["announcementId"],
        "contentHash": _NATIVE_1654_REPORT["contentHash"],
        "documentUrl": _NATIVE_1654_REPORT["documentUrl"],
        "receiptId": _NATIVE_1654_REPORT["receiptId"],
        "issuerName": "湖北宜化化工股份有限公司", "instrumentId": "SZ.000422",
        "reportPeriodEnd": "2024-12-31", "statement": "合并利润表",
        "statementScope": "CONSOLIDATED", "unitRaw": "元",
        "page": page_number, "currentHeader": "2024年度",
        "currentRoleHeader": "本期金额", "currentCellRaw": current_raw,
        "currentAmountYuan": str(current_amount),
        "priorRoleHeader": "上期金额", "priorCellRawUnassigned": prior_raw,
        "priorAmountYuanUnassigned": str(prior_amount), "priorFactPeriodEnd": None,
        "priorPeriodResolution": "UNRESOLVED_NO_EXPLICIT_2023_DATE_HEADER",
        "rowLabel": "其中：营业收入", "geometry": geometry,
        "statementArithmetic": {
            "check": "revenue_note_decomposition", "matches": True,
            "noteEvidence": note_evidence,
        },
        "existingSectionParser": existing_parser_result,
        "columnAssignmentBasis": "native 本期金额/上期金额 labels and x-coordinate alignment; no expected amount used to select a column",
    }
    return {
        "metric": "revenue", "statementScope": "CONSOLIDATED", "statement": "利润表",
        "factPeriodEnd": "2024-12-31", "columnRole": "CURRENT",
        "currentHeader": "2024年度", "priorHeader": "上期金额",
        "priorFactPeriodEnd": None, "priorPeriodResolution": "UNRESOLVED",
        "priorPeriodUnresolvedReason": "1654 prints 上期金额 but no explicit 2023 period header; this column is not treated as a 2023 fact.",
        "unitRaw": "元", "amountYuan": str(current_amount),
        "priorAmountYuan": None, "currentCellRaw": current_raw, "priorCellRaw": None,
        "unassignedPriorColumn": {"header": "上期金额", "rawCell": prior_raw,
                                  "amountYuan": str(prior_amount), "factPeriodEnd": None},
        "rowLabel": "其中：营业收入", "page": page_number,
        "sourceLine": revenue_line["text"], "tablePages": [page_number, page_number],
        "sourceNativeEvidence": native_support,
    }


def _revenue_columns(pages, period):
    if not period.endswith("12-31"):
        return None
    year, hits = int(period[:4]), []
    number = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d+"
    for page, text in enumerate(pages, 1):
        pattern = (r"合并利润表\s+单位\s*[:：]\s*(百万元|万元|千元|元)\s+项目\s+"
                   + str(year) + r"\s*年度\s+" + str(year - 1)
                   + r"\s*年度(?P<scope>.{0,1000}?)其中\s*[:：]\s*营业收入\s+"
                   + "(?P<current>" + number + ")\\s+(?P<prior>" + number + ")")
        for match in re.finditer(pattern, text):
            unit, current, prior = match[1], match["current"], match["prior"]
            hits.append({"metric": "revenue", "statementScope": "CONSOLIDATED", "statement": "利润表",
                "factPeriodEnd": period, "columnRole": "CURRENT", "currentHeader": f"{year}年度",
                "priorHeader": f"{year-1}年度", "priorFactPeriodEnd": f"{year-1}-12-31", "unitRaw": unit,
                "amountYuan": str(Decimal(current.replace(",", "")) * SCALE[unit]),
                "priorAmountYuan": str(Decimal(prior.replace(",", "")) * SCALE[unit]),
                "currentCellRaw": current, "priorCellRaw": prior, "rowLabel": "其中：营业收入",
                "page": page, "sourceLine": match[0], "tablePages": [page, page]})
    return hits[0] if len(hits) == 1 else None


def _quote(pages, pattern):
    for n, text in enumerate(pages, 1):
        match = re.search(pattern, text)
        if match:
            return {"page": n, "quote": match.group(0)}
    return None


def _security_code(pages, code, short_name):
    for page in pages:
        if re.search(r"(?:证券|股票)代码\s*[:：]?\s*" + code + r"\b", page):
            return True
        # SSE reports arrange the stock identity as a horizontal table. Keep
        # the label, issuer short name and exact code within that actual block.
        match = re.search(r"公司股票简况.{0,150}?股票代码(.{0,160})", page)
        if match and short_name in match[1] and re.search(r"\b" + code + r"\b", match[1]):
            return True
    return False


_AUXILIARY_000422_REPORTS = {
    "1220963854": {
        "title": "2024年半年度财务报告", "period": "2024-06-30", "publishedDate": "2024-08-26",
        "documentUrl": "https://static.cninfo.com.cn/finalpage/2024-08-26/1220963854.PDF",
        "contentHash": "sha256:0427462845d7828ed393fab366508a48ec6c38b88e0bdcc11b5e3119018a4a5e",
    },
    "1222991654": {
        "title": "2024年年度财务报告", "period": "2024-12-31", "publishedDate": "2025-04-03",
        "documentUrl": "https://static.cninfo.com.cn/finalpage/2025-04-03/1222991654.PDF",
        "contentHash": "sha256:e3dcde5c91deb5ca53eb1936ae5c330f0e2d5846ac4417eb634d8a6e240af05b",
    },
    "1224559959": {
        "title": "2025年半年度财务报告", "period": "2025-06-30", "publishedDate": "2025-08-23",
        "documentUrl": "https://static.cninfo.com.cn/finalpage/2025-08-23/1224559959.PDF",
        "contentHash": "sha256:c9d3c7164431ebb69592dd76f9520195b6d8194ec60fbaa69295078f872ed8c3",
    },
}


def _index_bound_000422_auxiliary_identity(pages, title, period, issuer_name, code, source_binding):
    """Accept only the three reviewed financial statements through exact index binding."""
    if not isinstance(source_binding, Mapping):
        return False
    aid = str(source_binding.get("announcementId") or "")
    expected = _AUXILIARY_000422_REPORTS.get(aid)
    if not expected:
        return False
    if (code != "000422" or period.isoformat() != expected["period"]
            or re.sub(r"\s+", "", title) != expected["title"]
            or source_binding.get("metadataCode") != "000422"
            or source_binding.get("metadataInstrumentId") != "SZ.000422"
            or source_binding.get("periodEnd") != expected["period"]
            or source_binding.get("contentHash") != expected["contentHash"]
            or source_binding.get("documentUrl") != expected["documentUrl"]
            or source_binding.get("indexDocumentUrl") != expected["documentUrl"]
            or source_binding.get("publishedDate") != expected["publishedDate"]
            or source_binding.get("indexSecCode") != "000422"
            or source_binding.get("indexOrgId") != "gssz0000422"
            or not source_binding.get("indexReceiptId")
            or source_binding.get("metadataIndexReceiptId") != source_binding.get("indexReceiptId")
            or source_binding.get("metadataIndexResponseHash") != source_binding.get("indexResponseHash")
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(source_binding.get("indexResponseHash") or ""))
            or source_binding.get("selectedCanonicalReport") is not False):
        return False

    compact_pages = [re.sub(r"\s+", "", page) for page in pages]
    if not compact_pages or not any(issuer_name in page for page in compact_pages[:8]):
        return False
    start = period.replace(month=1, day=1)
    end_text = rf"{period.year}年0?{period.month}月0?{period.day}日"
    range_text = (rf"{start.year}年0?{start.month}月0?{start.day}日"
                  rf"(?:至|到){end_text}止")
    if not any(re.search(range_text, page) for page in compact_pages[:8]):
        return False
    if not any(issuer_name in page and "合并资产负债表" in page
               and re.search(end_text, page) for page in compact_pages[:12]):
        return False
    if any(re.search(r"(?:证券|股票|公司)代码\s*[:：]?\s*(?!000422\b)\d{6}", page)
           for page in pages):
        return False
    return True


def _financial_source_binding(metadata, entry, *, selected_canonical_report):
    return {
        "announcementId": str(metadata.get("announcementId") or ""),
        "metadataTitle": metadata.get("title"),
        "indexTitle": entry.get("title"),
        "metadataCode": metadata.get("code"),
        "metadataInstrumentId": metadata.get("instrumentId"),
        "periodEnd": metadata.get("periodEnd"),
        "contentHash": metadata.get("contentHash"),
        "pdfReceiptId": metadata.get("receipt", {}).get("receipt_id"),
        "documentUrl": metadata.get("documentUrl"),
        "indexDocumentUrl": entry.get("documentUrl"),
        "publishedDate": entry.get("publishedDate"),
        "indexSecCode": entry.get("secCode"),
        "indexOrgId": entry.get("orgId"),
        "metadataIndexReceiptId": metadata.get("indexReceiptId"),
        "indexReceiptId": entry.get("indexReceiptId"),
        "metadataIndexResponseHash": metadata.get("indexResponseHash"),
        "indexResponseHash": entry.get("indexResponseHash"),
        "selectedCanonicalReport": selected_canonical_report,
    }


_OPAQUE_000422_CAUSE = {
    "announcementId": "1225183650",
    "title": "关于湖北宜化化工股份有限公司同一控制下企业合并追溯调整前期财务报表数据的专项说明 信会师报字[2026]第ZE10202号",
    "publishedDate": "2026-04-25",
    "documentUrl": "https://static.cninfo.com.cn/finalpage/2026-04-25/1225183650.PDF",
    "contentHash": "sha256:f46e817c54a818eae59a42579b4a0f5d8bef85707473d441c9a339e735168cb0",
    "indexReceiptId": "rcpt_36762ae8d867019960befa9f",
    "indexResponseHash": "sha256:f45bf4b1b147274f56338d42ea8e8351359db4e72e55f1aff6a390dbff31e8bf",
    "pdfReceiptId": "rcpt_7c0b10d269258db87c56c118",
}


def _index_bound_000422_opaque_cause_identity(pages, title, issuer_name, code, source_binding):
    """Narrow fallback for the independently audited, unreadable glyph-coded CPA notice."""
    expected = _OPAQUE_000422_CAUSE
    if not isinstance(source_binding, Mapping):
        return None
    compact_title = re.sub(r"\s+", "", title or "")
    binding_title = re.sub(r"\s+", "", str(source_binding.get("indexTitle") or ""))
    metadata_title = re.sub(r"\s+", "", str(source_binding.get("metadataTitle") or ""))
    if (code != "000422" or issuer_name != ISSUERS.get("000422")
            or source_binding.get("announcementId") != expected["announcementId"]
            or compact_title != re.sub(r"\s+", "", expected["title"])
            or binding_title != re.sub(r"\s+", "", expected["title"])
            or metadata_title != re.sub(r"\s+", "", expected["title"])
            or source_binding.get("publishedDate") != expected["publishedDate"]
            or source_binding.get("documentUrl") != expected["documentUrl"]
            or source_binding.get("indexDocumentUrl") != expected["documentUrl"]
            or source_binding.get("contentHash") != expected["contentHash"]
            or source_binding.get("metadataCode") != "000422"
            or source_binding.get("metadataInstrumentId") != "SZ.000422"
            or source_binding.get("indexSecCode") != "000422"
            or source_binding.get("indexOrgId") != "gssz0000422"
            or source_binding.get("indexReceiptId") != expected["indexReceiptId"]
            or source_binding.get("metadataIndexReceiptId") != expected["indexReceiptId"]
            or source_binding.get("indexResponseHash") != expected["indexResponseHash"]
            or source_binding.get("metadataIndexResponseHash") != expected["indexResponseHash"]
            or source_binding.get("pdfReceiptId") != expected["pdfReceiptId"]
            or source_binding.get("selectedCanonicalReport") is not False):
        return None
    extracted = "".join("".join(page.split()) for page in pages)
    native_text_present = any(bool(page) for page in pages)
    # A readable legal-entity name is contradictory native identity evidence;
    # only this audited, glyph-coded source may use the exact index binding.
    readable_company_name = re.search(
        r"[\u4e00-\u9fff]{2,}(?:股份有限公司|有限责任公司|有限公司)", extracted)
    if not native_text_present or issuer_name in extracted or readable_company_name:
        return None
    return {"identityBasis": "INDEX_BOUND_OPAQUE_RELATED_CAUSE",
        "announcementId": expected["announcementId"], "pdfSha256": expected["contentHash"],
        "pdfReceiptId": expected["pdfReceiptId"], "indexReceiptId": expected["indexReceiptId"],
        "indexResponseHash": expected["indexResponseHash"],
        "indexSecCode": "000422", "indexOrgId": "gssz0000422",
        "publishedDate": expected["publishedDate"], "documentUrl": expected["documentUrl"],
        "nativeTextPresent": True, "usableNativeIssuerText": False,
        "visibleIdentityAudit": "remaining43/000422-cpa-native-identity-diagnostic/report.md",
        "bodyUsedForH125EffectOrAmountEvidence": False}


def _auxiliary_financial_identity(pages, title, period, issuer_name, code, *, source_binding=None):
    """Financial-only annexes bind issuer/period in cover and prepared statement.

    These official annexes omit a stock-identity cover. Their index/SQL joins
    remain mandatory at the caller; canonical listed-company reports never
    use this branch. An explicit contradictory security identity closes it.
    """
    if code == "000422":
        return _index_bound_000422_auxiliary_identity(
            pages, title, period, issuer_name, code, source_binding)
    compact_title = re.sub(r"\s+", "", title)
    kind = "半年度" if period.month == 6 else "年度"
    if code not in {"000899", "002602"} or compact_title != f"{period.year}年{kind}财务报告":
        return False
    compact = [re.sub(r"\s+", "", page) for page in pages]
    if not compact or not re.match(r"(?:1)?" + re.escape(issuer_name) + str(period.year) +
            r"年" + kind + r"财务(?:会计)?报告", compact[0]):
        return False
    identities = re.findall(r"(?:证券|股票|公司)代码\s*[:：]?\s*(\d{6})(?!\d)", " ".join(pages))
    if any(identity != code for identity in identities):
        return False
    report_date = rf"{period.year}年0?{period.month}月0?{period.day}日"
    # The prepared-by issuer and reporting date must occupy the original
    # consolidated statement heading, independently of the cover/title.
    heading = r"合并资产负债表编制单位[:：]" + re.escape(issuer_name) + report_date + r"单位[:：]元"
    return any(re.search(heading, page) for page in compact[1:])


def _601069_is_annual_reporting_policy(title):
    # A dated revision to this governance制度 is not an annual financial
    # report. Keep legacy period matching unchanged for all old issuers.
    return re.sub(r"\s+", "", title) == (
        "《西部黄金股份有限公司年报报告制度》（2025年7月修订）")

def _601069_is_delayed_disclosure_notice(title):
    # A notice delaying the annual report is not the report it announces.
    return bool(re.fullmatch(
        r"西部黄金股份有限公司关于延期披露20\d{2}年(?:年度|半年度)报告的公告",
        re.sub(r"\s+", "", title)))

def _601069_native_pages(document, physical_pages, *, allow_fixture=False):
    """Read just the fixed native pages from an archived PDF or saved capture fixture."""
    fixture_pages = document.get("_nativeFixturePages")
    if allow_fixture and fixture_pages is not None:
        by_page = {int(page["physicalPage"]): page for page in fixture_pages}
        missing = set(physical_pages) - set(by_page)
        if missing:
            raise ValueError(f"saved 601069 native pages missing: {sorted(missing)}")
        return {number: by_page[number] for number in physical_pages}

    body = document.get("body")
    if not isinstance(body, (bytes, bytearray)) or not body.startswith(b"%PDF-"):
        raise ValueError("601069 native table proof requires receipt-bound original PDF bytes")
    import pdfplumber
    result = {}
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        for number in physical_pages:
            if number < 1 or number > len(pdf.pages):
                raise ValueError(f"601069 native physical page {number} is absent")
            page = pdf.pages[number - 1]
            tables = []
            for table in page.find_tables():
                rows = list(table.rows)
                tables.append({
                    "bbox": list(table.bbox),
                    "rows": table.extract(),
                    "rowCells": [[None if cell is None else list(cell) for cell in row.cells]
                                 for row in rows],
                })
            result[number] = {
                "physicalPage": number, "width": page.width, "height": page.height,
                "nativeText": page.extract_text() or "",
                "words": page.extract_words(),
                "chars": list(page.chars), "defaultTables": tables,
            }
    return result

def _601069_native_quote(pages, physical_pages, pattern):
    """Return an exact native-text quote and its original physical page."""
    for number in physical_pages:
        page = pages.get(number)
        if page is None:
            continue
        match = re.search(pattern, str(page.get("nativeText", "")))
        if match:
            return {"page": number, "quote": match.group(0)}
    return None

def _601069_compact_native(value):
    return re.sub(r"\s+", "", str(value or ""))

def _601069_cell_native_text(page, raw_box):
    if not isinstance(raw_box, (list, tuple)) or len(raw_box) != 4:
        raise ValueError("601069 native table cell has no physical rectangle")
    x0, top, x1, bottom = (float(value) for value in raw_box)
    if not (0 <= x0 < x1 <= float(page["width"]) and
            0 <= top < bottom <= float(page["height"])):
        raise ValueError("601069 native table cell rectangle is outside its physical page")
    physical_page = int(page["physicalPage"])
    selected_chars = []
    for char in page.get("chars", ()):
        try:
            char_page = char.get("page_number")
            box = tuple(float(char[key]) for key in ("x0", "top", "x1", "bottom"))
            text = str(char["text"])
        except (KeyError, TypeError, ValueError):
            continue
        if char_page is not None and int(char_page) != physical_page:
            continue
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        if x0 <= cx <= x1 and top <= cy <= bottom:
            selected_chars.append((box, text))

    selected_words = []
    for word in page.get("words", ()):
        try:
            word_box = tuple(float(word[key]) for key in ("x0", "top", "x1", "bottom"))
            word_text = str(word["text"])
        except (KeyError, TypeError, ValueError):
            continue
        cx, cy = (word_box[0] + word_box[2]) / 2, (word_box[1] + word_box[3]) / 2
        if not (x0 <= cx <= x1 and top <= cy <= bottom):
            continue
        glyphs = [(box[0], text) for box, text in selected_chars
                  if (word_box[0] <= (box[0] + box[2]) / 2 <= word_box[2] and
                      word_box[1] <= (box[1] + box[3]) / 2 <= word_box[3])]
        native_word = "".join(text for _, text in sorted(glyphs))
        if not glyphs or _601069_compact_native(native_word) != _601069_compact_native(word_text):
            raise ValueError("601069 native word does not match its own glyph characters")
        selected_words.append((round(word_box[1], 1), word_box[0], word_text))
    selected_words.sort(key=lambda item: (item[0], item[1]))
    return "".join(item[2] for item in selected_words), len(selected_chars)

def _601069_cell(page, table, row_index, column_index):
    rows, row_cells = table.get("rows", ()), table.get("rowCells", ())
    if (row_index >= len(rows) or row_index >= len(row_cells) or row_index < 0 or
            column_index >= len(rows[row_index]) or column_index >= len(row_cells[row_index])):
        raise ValueError("601069 native table row/cell contract is incomplete")
    expected = str(rows[row_index][column_index] or "")
    if not expected:
        raise ValueError("601069 required native table cell is blank")
    actual, char_count = _601069_cell_native_text(page, row_cells[row_index][column_index])
    if not char_count or _601069_compact_native(actual) != _601069_compact_native(expected):
        raise ValueError("601069 table cell is not reproduced by its own native characters")
    return {"text": expected, "nativeText": actual,
            "bbox": [float(value) for value in row_cells[row_index][column_index]],
            "nativeCharacterCount": char_count}

def _601069_word(page, exact_text, *, before_top=None):
    target = _601069_compact_native(exact_text)
    matches = []
    for word in page.get("words", ()):
        if _601069_compact_native(word.get("text")) != target:
            continue
        box = [float(word[key]) for key in ("x0", "top", "x1", "bottom")]
        if before_top is not None and box[3] >= before_top:
            continue
        actual, count = _601069_cell_native_text(page, box)
        if count and _601069_compact_native(actual) == target:
            matches.append({"text": str(word["text"]), "bbox": box,
                            "nativeCharacterCount": count})
    if not matches:
        raise ValueError(f"601069 native page is missing exact text: {exact_text}")
    if before_top is None and len(matches) != 1:
        raise ValueError(f"601069 native page has ambiguous exact text: {exact_text}")
    if before_top is not None:
        nearest_bottom = max(match["bbox"][3] for match in matches)
        nearest = [match for match in matches if match["bbox"][3] == nearest_bottom]
        if len(nearest) != 1:
            raise ValueError(f"601069 native page has ambiguous preceding text: {exact_text}")
        return nearest[0]
    return matches[0]

def _601069_table(pages, physical_page, table_index):
    page = pages.get(physical_page)
    if page is None:
        raise ValueError(f"601069 native physical page {physical_page} is absent")
    tables = page.get("defaultTables", ())
    if table_index >= len(tables):
        raise ValueError(f"601069 native table {table_index} is absent on page {physical_page}")
    return page, tables[table_index]

def _601069_table_header(page, table, expected):
    if not table.get("rows") or len(table["rows"][0]) != len(expected):
        raise ValueError("601069 native table header column count differs")
    cells = []
    for index, expected_text in enumerate(expected):
        actual_text = str(table["rows"][0][index] or "")
        if _601069_compact_native(actual_text) != _601069_compact_native(expected_text):
            raise ValueError("601069 native table literal column roles differ")
        box = table["rowCells"][0][index]
        if not _601069_compact_native(expected_text):
            if box is not None:
                actual, count = _601069_cell_native_text(page, box)
                if count or actual:
                    raise ValueError("601069 blank merged/header cell contains native text")
            cells.append({"text": "", "nativeText": "", "bbox": box,
                          "nativeCharacterCount": 0})
        else:
            cells.append(_601069_cell(page, table, 0, index))
    boxes = [table["rowCells"][0][index] for index in range(len(expected))]
    for left, right in zip(boxes, boxes[1:]):
        if left is not None and right is not None and float(left[2]) > float(right[0]):
            raise ValueError("601069 native table header columns overlap or reverse")
    return cells

def _601069_row_index(table, label):
    target = _601069_compact_native(label)
    hits = [index for index, row in enumerate(table.get("rows", ()))
            if row and _601069_compact_native(row[0]) == target]
    if len(hits) != 1:
        raise ValueError(f"601069 native table label is not unique: {label}")
    return hits[0]

def _601069_same_columns(table, header_row, data_row, columns):
    boxes = table.get("rowCells", ())
    if max(header_row, data_row) >= len(boxes):
        raise ValueError("601069 native table geometry row is absent")
    for column in columns:
        header_box, data_box = boxes[header_row][column], boxes[data_row][column]
        if (header_box is None or data_box is None or
                float(header_box[0]) != float(data_box[0]) or
                float(header_box[2]) != float(data_box[2]) or
                float(data_box[1]) < float(header_box[3])):
            raise ValueError("601069 native data cell is outside its literal header column")

def _601069_unit_before(page, table):
    table_top = float(table["bbox"][1])
    return _601069_word(page, "单位：元", before_top=table_top)

def _601069_amount(cell):
    raw = _601069_compact_native(cell["text"])
    if not re.fullmatch(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}", raw):
        raise ValueError(f"601069 native money cell is not a two-decimal amount: {raw}")
    return Decimal(raw.replace(",", ""))

def _601069_native_row(page, table, row_index, label, columns):
    if len(table.get("rows", ())[row_index]) != columns:
        raise ValueError("601069 native table data column count differs from its role header")
    label_cell = _601069_cell(page, table, row_index, 0)
    if _601069_compact_native(label_cell["text"]) != _601069_compact_native(label):
        raise ValueError("601069 native table row label differs")
    cells = [_601069_cell(page, table, row_index, index) for index in range(1, columns)]
    row_boxes = table["rowCells"][row_index]
    for left, right in zip(row_boxes, row_boxes[1:]):
        if left is None or right is None or float(left[2]) > float(right[0]):
            raise ValueError("601069 native table data columns overlap or reverse")
    return label_cell, cells

def _601069_correction_row(correction_pages, period_end, header_page_number, header_table_index,
                           data_page_number, data_table_index, label):
    header_page, header_table = _601069_table(
        correction_pages, header_page_number, header_table_index)
    header_cells = _601069_table_header(
        header_page, header_table, ["项目", "调整前金额", "调整后金额", "调整金额"])
    unit = _601069_unit_before(header_page, header_table)
    data_page, data_table = _601069_table(correction_pages, data_page_number, data_table_index)
    row_index = _601069_row_index(data_table, label)
    if data_page_number == header_page_number and data_table_index == header_table_index:
        _601069_same_columns(header_table, 0, row_index, (0, 1, 2, 3))
    label_cell, amount_cells = _601069_native_row(data_page, data_table, row_index, label, 4)
    amounts = [_601069_amount(cell) for cell in amount_cells]
    if amounts[0] + amounts[2] != amounts[1]:
        raise ValueError("601069 native correction before/change/after arithmetic conflicts")
    return {
        "periodEnd": period_end,
        "announcementId": "1225082555", "page": data_page_number,
        "tableIndex": data_table_index, "rowIndex": row_index,
        "rowLabel": label_cell["text"], "columnRoles": ["BEFORE", "AFTER", "CHANGE"],
        "beforeYuan": str(amounts[0]), "afterYuan": str(amounts[1]),
        "changeYuan": str(amounts[2]), "unit": "元",
        "headerPage": header_page_number, "headerTableIndex": header_table_index,
        "headerCells": header_cells, "unitCell": unit,
        "cells": [label_cell, *amount_cells],
    }

def _601069_annual_reconciliation_unit_context(annual_pages, section_title, section_unit,
                                               quarter_table, q1_table_index, h1_table_index):
    """Bind adjacent-page continuation to the same unit-explicit annual section."""
    try:
        page = annual_pages[10]
        section = _601069_native_quote(annual_pages, (9,), r"九、\s*2025\s*年分季度主要财务数据")
        continuation = _601069_word(page, "季度数据与已披露定期报告数据差异说明")
        next_section = _601069_word(page, "十、")
        tables = [page["defaultTables"][index] for index in (q1_table_index, h1_table_index)]
        if (section is None or section_title["text"] != "2025年分季度主要财务数据" or
                section_unit["text"] != "单位：元" or
                not (float(section_title["bbox"][3]) < float(section_unit["bbox"][1]) <
                     float(quarter_table["bbox"][1])) or
                not all(float(continuation["bbox"][3]) < float(table["bbox"][1]) <
                        float(table["bbox"][3]) < float(next_section["bbox"][1]) for table in tables)):
            return {"status": "UNKNOWN", "unit": None, "basis": None}
    except (KeyError, IndexError, TypeError, ValueError):
        return {"status": "UNKNOWN", "unit": None, "basis": None}
    return {"status": "BOUND_FROM_SAME_SECTION", "unit": "元",
            "basis": "page9_explicit_unit_carried_into_section9_page10_continuation",
            "sectionHeading": section, "sectionTitle": section_title, "unitCell": section_unit,
            "continuationHeading": {"page": 10, **continuation},
            "nextSectionHeading": {"page": 10, **next_section}}


def _601069_annual_reconciliation(annual_pages, metric, period, table_index, unit_context):
    page, table = _601069_table(annual_pages, 10, table_index)
    label = {"revenue": "营业收入", "net_profit_attributable": "归属于上市公司股东的净利润",
             "operating_cashflow": "经营活动产生的现金流量净额"}[metric]
    if period == "2025-06-30":
        expected_header = ["项目名称", "已披露定期报告2025年1-6月数据", "2025年1-6月调整后", "调整数"]
    else:
        expected_header = ["项目名称", "已披露定期报告2025年1-3月数据", "2025年1-3月调整后", "调整数"]
    header_cells = _601069_table_header(page, table, expected_header)
    row_index = _601069_row_index(table, label)
    _601069_same_columns(table, 0, row_index, (0, 1, 2, 3))
    label_cell, amount_cells = _601069_native_row(page, table, row_index, label, 4)
    amounts = [_601069_amount(cell) for cell in amount_cells]
    return {"announcementId": "1225082554", "page": 10, "tableIndex": table_index,
            "rowIndex": row_index, "periodEnd": period,
            "columnRoles": ["DISCLOSED_PERIODIC_REPORT", "ANNUAL_REPORT_ADJUSTED", "DELTA"],
            "rowLabel": label_cell["text"],
            "beforeRaw": str(amounts[0]), "annualAdjustedRaw": str(amounts[1]),
            "changeRaw": str(amounts[2]),
            "unitPrintedOnThisTable": None,
            "beforeYuan": str(amounts[0]) if unit_context["status"] == "BOUND_FROM_SAME_SECTION" else None,
            "annualAdjustedYuan": str(amounts[1]) if unit_context["status"] == "BOUND_FROM_SAME_SECTION" else None,
            "changeYuan": str(amounts[2]) if unit_context["status"] == "BOUND_FROM_SAME_SECTION" else None,
            "unitStatus": unit_context["status"],
            "unitBasis": unit_context["basis"], "unitContext": unit_context,
            "printedDeltaVerifiedArithmetically": False,
            "headerCells": header_cells,
            "cells": [label_cell, *amount_cells]}


def _annual_601069_fy25_native_policy(metric, documents, financial, origin, *, allow_fixture_pages=False):
    """Native-cell proof that named FY25 totals were not replaced by interim corrections."""
    required = {"1225082555", "1225082560", "1225082606"}
    if financial != required:
        return None, "captured 601069 correction relationship set differs"
    correction_titles = {
        "1225082555": "西部黄金股份有限公司董事会关于前期会计差错更正的说明",
        "1225082560": "西部黄金股份有限公司关于前期会计差错更正后的财务报表及附注",
        "1225082606": "西部黄金股份有限公司关于前期会计差错更正的公告",
    }
    for aid, title in correction_titles.items():
        metadata = documents.get(aid, {}).get("metadata", {})
        if (metadata.get("announcementId") != aid or metadata.get("code") != "601069" or
                normalize_title(metadata.get("title", "")) != normalize_title(title)):
            return None, "captured 601069 correction announcement identity differs"
    if metric == "net_profit_consolidated":
        return None, "annual consolidated net profit has no captured quarterly bridge; do not infer from attributable profit"
    if metric not in {"revenue", "net_profit_attributable", "operating_cashflow"}:
        return None, "601069 FY25 profile does not cover this metric"
    annual = documents.get("1225082554")
    annual_meta = annual.get("metadata", {}) if annual else {}
    if (annual is None or annual_meta.get("announcementId") != "1225082554" or
            annual_meta.get("code") != "601069" or annual_meta.get("periodEnd") != "2025-12-31" or
            normalize_title(annual_meta.get("title", "")) != normalize_title("西部黄金股份有限公司2025年年度报告")):
        return None, "FY25 annual report identity/period differs from the supported original"
    if origin is None:
        return None, "annual CURRENT and applicable PRIOR source cells are required"

    if (origin.get("metric") != metric or origin.get("factPeriodEnd") != "2025-12-31" or
            origin.get("columnRole") != "CURRENT" or origin.get("statementScope") != "CONSOLIDATED" or
            origin.get("unitRaw") != "元" or origin.get("priorFactPeriodEnd") != "2024-12-31" or
            "2025" not in re.sub(r"\s+", "", origin.get("currentHeader", "")) or
            "2024" not in re.sub(r"\s+", "", origin.get("priorHeader", ""))):
        return None, "annual CURRENT/2024 adjusted PRIOR cell does not match the saved annual source"
    try:
        current, prior = Decimal(origin["amountYuan"]), Decimal(origin["priorAmountYuan"])
        current_raw = Decimal(re.sub(r"[，,\s]", "", origin["currentCellRaw"])) * SCALE[origin["unitRaw"]]
        prior_raw = Decimal(re.sub(r"[，,\s]", "", origin["priorCellRaw"])) * SCALE[origin["unitRaw"]]
    except (KeyError, ArithmeticError, TypeError, ValueError):
        return None, "annual CURRENT/PRIOR source cells are not parseable under their original unit"
    if current_raw != current or prior_raw != prior:
        return None, "annual CURRENT/PRIOR raw cells conflict with their yuan amounts"

    try:
        native_context = {
            "1225082554": _601069_native_pages(
                annual, (7, 9, 10), allow_fixture=allow_fixture_pages),
            "1225082555": _601069_native_pages(
                documents["1225082555"], (1, 2, 3, 4),
                allow_fixture=allow_fixture_pages),
            "1225082560": _601069_native_pages(
                documents["1225082560"], (1, 4, 6, 8),
                allow_fixture=allow_fixture_pages),
            "1225082606": _601069_native_pages(
                documents["1225082606"], (1,), allow_fixture=allow_fixture_pages),
        }
    except (ValueError, KeyError, TypeError) as exc:
        return None, f"601069 native context extraction failed: {exc}"

    quote_specs = (
        ("scope", "1225082606", (1,),
         r"本次会计差错更正涉及\s*2025\s*年前三季度已披露的合并财务报表"),
        ("overview", "1225082555", (1, 2, 3, 4),
         r"在\s*2025\s*年前三\s*季度确认了相关收益"),
        ("q1_heading", "1225082555", (1, 2, 3, 4),
         r"（一）对\s*2025\s*年第一季度合并财务报表更正的情况"),
        ("h1_heading", "1225082555", (1, 2, 3, 4),
         r"（二）对\s*2025\s*年半年度合并财务报表更正的情况"),
        ("q3_heading", "1225082555", (1, 2, 3, 4),
         r"（三）对\s*2025\s*年第三季度合并财务报表更正的情况"),
        ("q1_doc_heading", "1225082560", (1, 4, 6, 8),
         r"《\s*2025\s*年第一季度报告\s*》财务报表更正内容"),
        ("h1_doc_heading", "1225082560", (1, 4, 6, 8),
         r"《\s*2025\s*年半年度报告\s*》财务报表及相关附注更正内容"),
        ("annual_adjusted_prior", "1225082554", (7, 9, 10),
         r"按照同一控制下企业合并原则对上年同期数进行了追溯调整.{0,80}上年同期调整后数据按照追溯调整后的数据列示"),
        ("annual_quarter_adjustment", "1225082554", (7, 9, 10),
         r"公司自控股股东处购买新疆美盛100\.00%股权，形成同一控制下企业合并[\s\S]{0,600}?对相关财务报表数据进行追溯调整。具体调整情况如下[:：]?"),
    )
    context_quotes = {}
    for key, aid, physical_pages, pattern in quote_specs:
        quote = _601069_native_quote(native_context[aid], physical_pages, pattern)
        if quote is None:
            return None, (f"missing native context quote: {key} in {aid} "
                          f"physical page(s) {list(physical_pages)}")
        context_quotes[key] = quote
    scope = context_quotes["scope"]
    overview = context_quotes["overview"]
    q1_heading = context_quotes["q1_heading"]
    h1_heading = context_quotes["h1_heading"]
    q3_heading = context_quotes["q3_heading"]
    q1_doc_heading = context_quotes["q1_doc_heading"]
    h1_doc_heading = context_quotes["h1_doc_heading"]
    annual_adjusted_prior = context_quotes["annual_adjusted_prior"]
    annual_quarter_adjustment = context_quotes["annual_quarter_adjustment"]
    if (q1_heading["page"] != 2 or h1_heading["page"] != 2 or q3_heading["page"] != 3):
        return None, "correction period headings do not occupy the saved physical page sequence"

    try:
        annual_pages = native_context["1225082554"]
        correction_pages = native_context["1225082555"]

        # Bind annual summary columns by literal year/subheading, then read the
        # actual 2025 source row and adjusted 2024 comparative from their cells.
        summary_page, summary_table = _601069_table(annual_pages, 7, 0)
        summary_headers = _601069_table_header(
            summary_page, summary_table, ["主要会计数据", "2025年", "2024年", "", "本期比上年同期增减(%)", "2023年", ""])
        if (_601069_compact_native(summary_table["rows"][1][2]) != "调整后" or
                _601069_compact_native(summary_table["rows"][1][3]) != "调整前"):
            raise ValueError("601069 annual prior columns do not distinguish adjusted from unadjusted")
        summary_prior_role_cells = [_601069_cell(summary_page, summary_table, 1, index)
                                    for index in (2, 3)]
        summary_unit = _601069_unit_before(summary_page, summary_table)
        summary_label = {"revenue": "营业收入",
                         "net_profit_attributable": "归属于上市公司股东的净利润",
                         "operating_cashflow": "经营活动产生的现金流量净额"}[metric]
        summary_row = _601069_row_index(summary_table, summary_label)
        _601069_same_columns(summary_table, 0, summary_row, (1,))
        _601069_same_columns(summary_table, 1, summary_row, (2, 3))
        parent_prior, adjusted_prior, unadjusted_prior = (
            summary_table["rowCells"][0][2], summary_table["rowCells"][1][2],
            summary_table["rowCells"][1][3])
        if (parent_prior is None or adjusted_prior is None or unadjusted_prior is None or
                float(parent_prior[0]) > float(adjusted_prior[0]) or
                float(adjusted_prior[2]) > float(parent_prior[2]) or
                float(parent_prior[0]) > float(unadjusted_prior[0]) or
                float(unadjusted_prior[2]) > float(parent_prior[2])):
            raise ValueError("2024 adjusted/unadjusted subcolumns do not lie under the 2024 year header")
        summary_cells = [_601069_cell(summary_page, summary_table, summary_row, index)
                         for index in (0, 1, 2)]
        summary_current, summary_prior = (_601069_amount(summary_cells[1]),
                                           _601069_amount(summary_cells[2]))
        if (summary_current != current or summary_prior != prior or
                _601069_amount(summary_cells[1]) != Decimal(origin["amountYuan"]) or
                _601069_amount(summary_cells[2]) != Decimal(origin["priorAmountYuan"])):
            raise ValueError("annual summary CURRENT/adjusted PRIOR cells conflict with the statement source")

        # Date-bearing annual columns are located from p9 header text and own
        # chars. Values are accepted only from the same physical columns below.
        quarter_page, quarter_table = _601069_table(annual_pages, 9, 1)
        quarter_headers = ["", "第一季度（1-3月份）", "第二季度（4-6月份）",
                           "第三季度（7-9月份）", "第四季度（10-12月份）"]
        quarter_header_cells = _601069_table_header(quarter_page, quarter_table, quarter_headers)
        quarter_title = _601069_word(quarter_page, "2025年分季度主要财务数据")
        quarter_unit = _601069_word(quarter_page, "单位：元", before_top=float(quarter_table["bbox"][1]))
        quarter_currency = _601069_word(quarter_page, "币种：人民币", before_top=float(quarter_table["bbox"][1]))
        header_top = float(quarter_table["rowCells"][0][1][1])
        if not (quarter_title["bbox"][3] < quarter_unit["bbox"][1] < header_top and
                quarter_currency["bbox"][3] < header_top):
            raise ValueError("annual quarterly title/unit/date header physical order differs")
        quarter_label = {"revenue": "营业收入",
                         "net_profit_attributable": "归属于上市公司股东的净利润",
                         "operating_cashflow": "经营活动产生的现金流量净额"}[metric]
        quarter_row = _601069_row_index(quarter_table, quarter_label)
        _601069_same_columns(quarter_table, 0, quarter_row, (1, 2, 3, 4))
        quarter_label_cell, quarter_value_cells = _601069_native_row(
            quarter_page, quarter_table, quarter_row, quarter_label, 5)
        quarter_values = [_601069_amount(cell) for cell in quarter_value_cells]
        quarter_periods = ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]
        annual_q1 = quarter_values[0]
        annual_h1 = sum(quarter_values[:2], Decimal("0"))
        annual_q3 = sum(quarter_values[:3], Decimal("0"))
        annual_sum = sum(quarter_values, Decimal("0"))
        if annual_sum != current:
            raise ValueError("FY25 quarter columns do not sum to the actual annual CURRENT source")

        # Literal column roles and the unit-explicit section are separate proofs.
        reconciliation_unit_context = _601069_annual_reconciliation_unit_context(
            annual_pages, quarter_title, quarter_unit, quarter_table, 1, 2)
        q1_reconciliation = _601069_annual_reconciliation(
            annual_pages, metric, "2025-03-31", 1, reconciliation_unit_context)
        h1_reconciliation = _601069_annual_reconciliation(
            annual_pages, metric, "2025-06-30", 2, reconciliation_unit_context)
        if reconciliation_unit_context["status"] != "BOUND_FROM_SAME_SECTION":
            return None, (
                "UNRESOLVED: annual report p10 Q1/H1 reconciliation unit is UNKNOWN; "
                "no explicit same-section unit context, raw numeric matches are insufficient")
        p10_adjusted_matches_p9 = {
            "2025-03-31": Decimal(q1_reconciliation["annualAdjustedRaw"]) == annual_q1,
            "2025-06-30": Decimal(h1_reconciliation["annualAdjustedRaw"]) == annual_h1,
        }
        if not all(p10_adjusted_matches_p9.values()):
            raise ValueError("annual p10 adjusted Q1/H1 cells do not match p9 quarter columns")
        # Extract correction rows only from the report period's literal table
        # header. Q2/Q3 continuation rows retain their local native cell boxes.
        locations = {
            "2025-03-31": (2, 0, 2, 0),
            "2025-06-30": (2, 1, 3, 0),
            "2025-09-30": (3, 1, 4, 0),
        }
        metric_correction_label = {"revenue": "营业收入",
                                   "net_profit_attributable": "归属于母公司股东的净利润"}
        correction_after = {}
        correction_rows = {}
        if metric in metric_correction_label:
            label = metric_correction_label[metric]
            for period_end, (header_page, header_table, data_page, data_table) in locations.items():
                if period_end == "2025-09-30" and metric == "revenue":
                    data_page, data_table = header_page, header_table
                record = _601069_correction_row(correction_pages, period_end, header_page,
                                                 header_table, data_page, data_table, label)
                correction_rows[period_end] = [record]
                correction_after[period_end] = Decimal(record["afterYuan"])
        else:
            line_labels = ["销售商品、提供劳务收到的现金", "收到其他与经营活动有关的现金",
                           "购买商品、接受劳务支付的现金", "支付其他与经营活动有关的现金"]
            q1_rows = [_601069_correction_row(correction_pages, "2025-03-31", 2, 0, 2, 0, label)
                       for label in line_labels]
            q1_in_before = sum((Decimal(row["beforeYuan"]) for row in q1_rows[:2]), Decimal("0"))
            q1_in_after = sum((Decimal(row["afterYuan"]) for row in q1_rows[:2]), Decimal("0"))
            q1_out_before = sum((Decimal(row["beforeYuan"]) for row in q1_rows[2:]), Decimal("0"))
            q1_out_after = sum((Decimal(row["afterYuan"]) for row in q1_rows[2:]), Decimal("0"))
            if q1_in_before != q1_in_after or q1_out_before != q1_out_after:
                raise ValueError("Q1 listed correction rows do not offset within receipts and payments")
            correction_rows["2025-03-31"] = q1_rows
            correction_after["2025-03-31"] = None
            for period_end, (header_page, header_table, data_page, data_table) in (
                    ("2025-06-30", locations["2025-06-30"]),
                    ("2025-09-30", locations["2025-09-30"])):
                inflow = _601069_correction_row(correction_pages, period_end, header_page,
                                                header_table, data_page, data_table,
                                                "经营活动现金流入小计")
                outflow = _601069_correction_row(correction_pages, period_end, header_page,
                                                 header_table, data_page, data_table,
                                                 "经营活动现金流出小计")
                before_net = Decimal(inflow["beforeYuan"]) - Decimal(outflow["beforeYuan"])
                after_net = Decimal(inflow["afterYuan"]) - Decimal(outflow["afterYuan"])
                if before_net != after_net:
                    raise ValueError("interim correction changed a reported operating cashflow net total")
                correction_rows[period_end] = [inflow, outflow]
                correction_after[period_end] = after_net

        # The annual report's disclosed-periodic columns link Q1/H1 correction
        # after values to its later, separately adjusted annual quarter series.
        # These comparisons check consistency only after explicit section unit
        # binding; they never supply a unit or replace literal column roles.
        p10_disclosed_matches_correction = {}
        for period_end, reconciliation in (("2025-03-31", q1_reconciliation),
                                             ("2025-06-30", h1_reconciliation)):
            if metric in metric_correction_label:
                matched = Decimal(reconciliation["beforeRaw"]) == correction_after[period_end]
                p10_disclosed_matches_correction[period_end] = matched
                if not matched:
                    raise ValueError("annual p10 disclosed Q1/H1 cells conflict with correction AFTER")
            elif period_end == "2025-06-30":
                p10_disclosed_matches_correction[period_end] = (
                    Decimal(reconciliation["beforeRaw"]) == correction_after[period_end])
                if not p10_disclosed_matches_correction[period_end]:
                    raise ValueError("annual p10 disclosed H1 OCF conflicts with correction net")

        if metric == "operating_cashflow":
            q3_in = _601069_correction_row(correction_pages, "2025-09-30", 3, 1, 4, 0,
                                            "经营活动现金流入小计")
            q3_out = _601069_correction_row(correction_pages, "2025-09-30", 3, 1, 4, 0,
                                             "经营活动现金流出小计")
            correction_rows["2025-09-30"] = [q3_in, q3_out]
            correction_after["2025-09-30"] = Decimal(q3_in["afterYuan"]) - Decimal(q3_out["afterYuan"])
        if correction_after["2025-09-30"] != annual_q3:
            raise ValueError("annual Q1-Q3 columns do not match the correction's Q3 AFTER")

    except (ValueError, ArithmeticError, KeyError, IndexError, TypeError) as exc:
        return None, str(exc)

    correction_period_quotes = [
        {"announcementId": "1225082555", **q1_heading},
        {"announcementId": "1225082555", **h1_heading},
        {"announcementId": "1225082555", **q3_heading},
    ]
    source_quotes = [
        {"announcementId": "1225082606", **scope},
        {"announcementId": "1225082555", **overview},
        *correction_period_quotes,
        {"announcementId": "1225082560", **q1_doc_heading},
        {"announcementId": "1225082560", **h1_doc_heading},
        {"announcementId": "1225082554", **annual_adjusted_prior},
        {"announcementId": "1225082554", **annual_quarter_adjustment},
        {"announcementId": "1225082554", "page": 7, "native": {
            "tableIndex": 0, "headerCells": summary_headers,
            "priorRoleCells": summary_prior_role_cells,
            "unitCell": summary_unit, "rowCells": summary_cells}},
        {"announcementId": "1225082554", "page": 9, "native": {
            "tableIndex": 1, "sectionTitle": quarter_title, "unitCell": quarter_unit,
            "currencyCell": quarter_currency, "dateHeaderCells": quarter_header_cells,
            "rowLabel": quarter_label_cell, "quarterCells": quarter_value_cells}},
        {"announcementId": "1225082554", "page": 10, "native": q1_reconciliation},
        {"announcementId": "1225082554", "page": 10, "native": h1_reconciliation},
        *[{"announcementId": "1225082555", "page": row["page"], "native": row}
          for period_rows in correction_rows.values() for row in period_rows],
    ]
    quarter_chain = {
        "2025-03-31": {"annualQuarterYuan": str(annual_q1),
                        "disclosedPeriodicRaw": q1_reconciliation["beforeRaw"],
                        "annualReportAdjustedRaw": q1_reconciliation["annualAdjustedRaw"],
                        "annualAdjustmentRaw": q1_reconciliation["changeRaw"],
                        "rawValueEqualityDiagnostics": {
                            "annualQuarter": p10_adjusted_matches_p9["2025-03-31"],
                            "correctionAfter": p10_disclosed_matches_correction.get("2025-03-31"),
                        },
                        "p10UnitStatus": reconciliation_unit_context["status"]},
        "2025-06-30": {"annualQuarterCumulativeYuan": str(annual_h1),
                        "disclosedPeriodicRaw": h1_reconciliation["beforeRaw"],
                        "annualReportAdjustedRaw": h1_reconciliation["annualAdjustedRaw"],
                        "annualAdjustmentRaw": h1_reconciliation["changeRaw"],
                        "rawValueEqualityDiagnostics": {
                            "annualQuarter": p10_adjusted_matches_p9["2025-06-30"],
                            "correctionAfter": p10_disclosed_matches_correction.get("2025-06-30"),
                        },
                        "p10UnitStatus": reconciliation_unit_context["status"]},
        "2025-09-30": {"annualQuarterCumulativeYuan": str(annual_q3),
                        "correctionAfterYuan": str(correction_after["2025-09-30"])},
        "2025-12-31": {"annualQuarterSumYuan": str(annual_sum),
                        "annualCurrentSourceYuan": str(current)},
    }
    scope_data = {
        "meaning": "NO_EFFECT applies only to the selected FY25 annual CURRENT and applicable 2024 adjusted PRIOR: the captured Q1/H1/Q3 correction set does not replace those annual source cells. It does not mean interim periods, annual-quarter adjustments, cashflow classification, or economic effects are absent.",
        "interimImpactDisposition": "AFFECTS_OR_RECLASSIFIES_INTERIM_REPORTS_AS_SHOWN; annual report p10 separately records disclosed-to-adjusted Q1/H1 changes.",
        "correctionReportPeriods": ["2025-03-31", "2025-06-30", "2025-09-30"],
        "currentColumnRole": "CURRENT", "currentPeriodEnd": "2025-12-31",
        "currentAmountYuan": str(current), "priorColumnRole": "PRIOR_ADJUSTED",
        "priorPeriodEnd": "2024-12-31", "priorAmountYuan": str(prior),
        "annualQuarterValuesYuan": [str(value) for value in quarter_values],
        "annualFirstThreeQuartersSumYuan": str(annual_q3),
        "annualQuarterSumYuan": str(annual_sum),
        "annualQuarterRoleChain": quarter_chain,
        "correctionNativeRows": correction_rows,
        "annualReconciliationUnitBoundary": (
            "p10 prints no local unit. Its explicit quarterly continuation and both tables remain within section 九 before section 十, carrying p9's printed 元 unit. Literal table headers and geometry bind column roles; numeric matches are consistency checks only. Missing structural unit context is UNRESOLVED."
        ),
    }
    if metric == "operating_cashflow":
        q1_rows = correction_rows["2025-03-31"]
        q1_in_before = sum((Decimal(row["beforeYuan"]) for row in q1_rows[:2]), Decimal("0"))
        q1_in_after = sum((Decimal(row["afterYuan"]) for row in q1_rows[:2]), Decimal("0"))
        q1_out_before = sum((Decimal(row["beforeYuan"]) for row in q1_rows[2:]), Decimal("0"))
        q1_out_after = sum((Decimal(row["afterYuan"]) for row in q1_rows[2:]), Decimal("0"))
        h1_in, h1_out = correction_rows["2025-06-30"]
        q3_in, q3_out = correction_rows["2025-09-30"]
        scope_data["operatingCashflowBeforeAfter"] = {
            "2025-03-31": {
                "listedReceiptRowsBeforeSumYuan": str(q1_in_before),
                "listedReceiptRowsAfterSumYuan": str(q1_in_after),
                "listedPaymentRowsBeforeSumYuan": str(q1_out_before),
                "listedPaymentRowsAfterSumYuan": str(q1_out_after),
                "correctionTableNetSubtotal": "NOT_PRINTED",
                "disclosedPeriodicNetRaw": q1_reconciliation["beforeRaw"],
                "annualReportAdjustedNetRaw": q1_reconciliation["annualAdjustedRaw"],
                "annualNetAdjustmentRaw": q1_reconciliation["changeRaw"],
            },
            "2025-06-30": {
                "correctionInflowBeforeYuan": h1_in["beforeYuan"],
                "correctionInflowAfterYuan": h1_in["afterYuan"],
                "correctionOutflowBeforeYuan": h1_out["beforeYuan"],
                "correctionOutflowAfterYuan": h1_out["afterYuan"],
                "correctionNetBeforeYuan": str(Decimal(h1_in["beforeYuan"]) - Decimal(h1_out["beforeYuan"])),
                "correctionNetAfterYuan": str(Decimal(h1_in["afterYuan"]) - Decimal(h1_out["afterYuan"])),
                "annualReportAdjustedNetRaw": h1_reconciliation["annualAdjustedRaw"],
                "annualNetAdjustmentRaw": h1_reconciliation["changeRaw"],
            },
            "2025-09-30": {
                "correctionInflowBeforeYuan": q3_in["beforeYuan"],
                "correctionInflowAfterYuan": q3_in["afterYuan"],
                "correctionOutflowBeforeYuan": q3_out["beforeYuan"],
                "correctionOutflowAfterYuan": q3_out["afterYuan"],
                "correctionNetBeforeYuan": str(Decimal(q3_in["beforeYuan"]) - Decimal(q3_out["beforeYuan"])),
                "correctionNetAfterYuan": str(Decimal(q3_in["afterYuan"]) - Decimal(q3_out["afterYuan"])),
            },
            "boundary": "Q1 correction rows show offsetting receipt/payment reclassifications but omit their inflow/outflow subtotal cells; the Q1 net shown above comes separately from annual-report p10 disclosed/adjusted columns. H1/Q3 correction subtotals permit direct before/after net calculation.",
        }
    return {"basis": "FY25_ANNUAL_CURRENT_AND_2024_ADJUSTED_PRIOR_NOT_REPLACED_BY_INTERIM_CORRECTION_SET",
            "sourceQuotes": source_quotes, "scope": scope_data}, None


def _h124_601069_native_prior_policy(metric, documents, financial, origin, columns,
                                      *, allow_fixture_pages=False):
    """Bind H124 CURRENT to H124 PRIOR in both H125 report versions.

    The ordinary assessment still supplies the complete archived index
    relationship set and compares every canonical report candidate. This
    exact issuer/period helper adds the corrected H125 report's native PRIOR
    cell, which is not a canonical current report record.
    """
    required = {"1225082555", "1225082560", "1225082606"}
    if metric != "revenue":
        return None, "601069 H124 profile only covers revenue", None
    if financial != required:
        return None, "captured 601069 H125 correction relationship set differs", None
    correction_titles = {
        "1225082555": "西部黄金股份有限公司董事会关于前期会计差错更正的说明",
        "1225082560": "西部黄金股份有限公司关于前期会计差错更正后的财务报表及附注",
        "1225082606": "西部黄金股份有限公司关于前期会计差错更正的公告",
    }
    for aid, expected_title in correction_titles.items():
        metadata = documents.get(aid, {}).get("metadata", {})
        if (metadata.get("announcementId") != aid or metadata.get("code") != "601069" or
                normalize_title(metadata.get("title", "")) != normalize_title(expected_title)):
            return None, f"601069 correction announcement identity differs: {aid}", None
    corrected = documents.get("1225082560")
    metadata = corrected.get("metadata", {}) if corrected else {}
    if corrected is None or metadata.get("instrumentId") != "SH.601069":
        return None, "corrected H125 financial-statement document is absent or issuer differs", None
    if (origin is None or origin.get("metric") != "revenue" or
            origin.get("factPeriodEnd") != "2024-06-30" or
            origin.get("columnRole") != "CURRENT" or
            origin.get("statementScope") != "CONSOLIDATED" or
            origin.get("unitRaw") != "元" or
            "2024" not in re.sub(r"\s+", "", origin.get("currentHeader", ""))):
        return None, "601069 H124 CURRENT source cell is missing or not period/unit bound", None
    original_h125 = columns.get("1224590046", {}).get("revenue")
    if (not original_h125 or original_h125.get("factPeriodEnd") != "2025-06-30" or
            original_h125.get("columnRole") != "CURRENT" or
            original_h125.get("priorFactPeriodEnd") != "2024-06-30" or
            original_h125.get("unitRaw") != "元" or
            "2025" not in re.sub(r"\s+", "", original_h125.get("currentHeader", "")) or
            "2024" not in re.sub(r"\s+", "", original_h125.get("priorHeader", "")) or
            not original_h125.get("priorAmountYuan") or
            not origin.get("amountYuan")):
        return None, "original H125 report does not bind the same H124 PRIOR fact", None
    try:
        if Decimal(original_h125["priorAmountYuan"]) != Decimal(origin["amountYuan"]):
            return None, "original H125 PRIOR does not equal the selected H124 CURRENT", None
        notice_pages = _601069_native_pages(
            documents["1225082606"], (1,), allow_fixture=allow_fixture_pages)
        notice_scope = _601069_native_quote(
            notice_pages, (1,),
            r"本次会计差错更正涉及\s*2025\s*年前三季度已披露的合并财务报表")
        if notice_scope is None:
            raise ValueError("correction notice does not establish the explicit 2025 interim scope")
        native = _601069_native_pages(corrected, (10, 11), allow_fixture=allow_fixture_pages)
        p10, p11 = native[10], native[11]
        context = _601069_native_quote(
            native, (10,), r"合并利润表\s+2025年1\s*[—–\-]\s*6月\s+单位\s*[:：]\s*元")
        if context is None:
            raise ValueError("corrected H125 p10 has no same-page H1 income-statement/unit context")
        unit_word = _601069_word(p10, "单位：元")
        current_header = _601069_word(p11, "2025年半年度")
        prior_header = _601069_word(p11, "2024年半年度")
        label = _601069_word(p11, "其中：营业收入")
        amount = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}"
        row_pattern = re.compile(r"其中：营业收入\s+(?P<current>" + amount +
                                 r")\s+(?P<prior>" + amount + r")")
        row_matches = list(row_pattern.finditer(str(p11.get("nativeText", ""))))
        if len(row_matches) != 1:
            raise ValueError("corrected H125 p11 revenue row is absent or ambiguous")
        row = row_matches[0]

        def row_word(raw):
            wanted = _601069_compact_native(raw)
            label_box = label["bbox"]
            matches = []
            for word in p11.get("words", ()):
                if _601069_compact_native(word.get("text")) != wanted:
                    continue
                box = [float(word[key]) for key in ("x0", "top", "x1", "bottom")]
                if (abs(box[1] - label_box[1]) > 1.0 or
                        abs(box[3] - label_box[3]) > 1.0):
                    continue
                actual, char_count = _601069_cell_native_text(p11, box)
                if char_count and _601069_compact_native(actual) == wanted:
                    matches.append({"text": str(word["text"]), "bbox": box,
                                    "nativeCharacterCount": char_count})
            if len(matches) != 1:
                raise ValueError("corrected H125 p11 revenue row cell is absent or ambiguous")
            return matches[0]

        current_cell = row_word(row["current"])
        prior_cell = row_word(row["prior"])

        def center(box):
            return (float(box[0]) + float(box[2])) / 2

        if (abs(center(current_cell["bbox"]) - center(current_header["bbox"])) > 8.0 or
                abs(center(prior_cell["bbox"]) - center(prior_header["bbox"])) > 8.0 or
                center(current_header["bbox"]) >= center(prior_header["bbox"])):
            raise ValueError("corrected H125 p11 revenue cells do not align under their literal year headers")
        current_yuan = _601069_amount({"text": row["current"]})
        prior_yuan = _601069_amount({"text": row["prior"]})
        original_h125_yuan = Decimal(original_h125["amountYuan"])
        if current_yuan == original_h125_yuan:
            raise ValueError("saved H125 CURRENT versions do not expose the known genuine revenue change")
        h125_delta = current_yuan - original_h125_yuan
    except (ValueError, ArithmeticError, KeyError, TypeError, IndexError) as exc:
        return None, f"601069 native H125 comparative extraction failed: {exc}", None

    comparison = {
        "announcementId": "1225082560", "sourceReportPeriodEnd": "2025-06-30",
        "factPeriodEnd": "2024-06-30", "columnRole": "PRIOR",
        "amountYuan": str(prior_yuan), "page": 11,
        "sourceLine": row.group(0), "rowLabel": "其中：营业收入", "unitRaw": "元",
        "columnHeader": "2024年半年度", "changed": prior_yuan != Decimal(origin["amountYuan"]),
    }
    proof = {
        "basis": "EXPLICIT_601069_H124_CURRENT_AND_ORIGINAL_CORRECTED_H125_PRIOR_CELLS",
        "sourceQuotes": [
            {"announcementId": "1225082606", **notice_scope},
            {"announcementId": "1225082560", "page": 10,
             "quote": context["quote"], "unitCell": unit_word},
            {"announcementId": "1225082560", "page": 11,
             "native": {"currentHeader": current_header, "priorHeader": prior_header,
                        "rowLabel": label, "currentCell": current_cell,
                        "priorCell": prior_cell, "sourceLine": row.group(0)}},
        ],
        "scope": {
            "targetFactPeriodEnd": "2024-06-30", "targetMetric": "revenue",
            "currentAnnouncementId": "1221007391", "currentAmountYuan": origin["amountYuan"],
            "originalH125AnnouncementId": "1224590046",
            "originalH125PriorAmountYuan": original_h125["priorAmountYuan"],
            "correctedH125AnnouncementId": "1225082560",
            "correctedH125PriorAmountYuan": str(prior_yuan),
            "h125OriginalCurrentYuan": str(original_h125_yuan),
            "h125CorrectedCurrentYuan": str(current_yuan),
            "h125CurrentDeltaYuan": str(h125_delta),
            "meaning": "H124 CURRENT and both H125 PRIOR observations are separately period/header/row bound. A changed corrected PRIOR is AFFECTS, never NO_EFFECT. H125 CURRENT has its own genuine version change. The native correction notice explicitly covers 2025 interim statements; this comparison is neither admission nor a company-wide no-impact claim.",
        },
    }
    return proof, None, comparison


def _finite_000680_fy24_current_policy(code, metric, period, documents, financial):
    """Only the FY24 CURRENT already including the disclosed common-control merger."""
    if (code, metric, period) != ("000680", "revenue", "2024-12-31"):
        return None, "outside the finite 000680 FY24 CURRENT revenue profile"
    notices = {
        "1223217192": "sha256:af2d75f47e7d71bc3c8ad58fc39e8c4ff5812f0c92ff4679f41da2714c9ec503",
        "1223217224": "sha256:55a461763ababb4cbb016e2ab11102167e38a7d662424b820fe0ee62c5f1826d",
        "1223217226": "sha256:3b0cb88fb75ef32444efa4742545b28b20d17833f7a7378649ed155308c4450e",
    }
    if financial != set(notices):
        return None, "complete captured financial-cause set differs from the three merger notices"
    for aid, digest in notices.items():
        doc = documents.get(aid, {})
        metadata, entry = doc.get("metadata", {}), doc.get("entry", {})
        if (metadata.get("code") != code or metadata.get("contentHash") != digest or
                entry.get("publishedDate") != "2025-04-23"):
            return None, "captured merger-notice identity/hash/publication differs"
    reports = {
        "1223217214": ("2024-12-31", "2025-04-23", "sha256:766e63a23043f4a5dcbed14856ea99cede324fff37d79d9a1a96e3f3b87981be"),
        "1225008978": ("2025-12-31", "2026-03-16", "sha256:69512971443ccbaaa7b5a7e986da6478a4252997f8e132c689eef3a88aa2b4de"),
    }
    for aid, (report_period, published, digest) in reports.items():
        doc = documents.get(aid, {})
        metadata, entry = doc.get("metadata", {}), doc.get("entry", {})
        if (metadata.get("code") != code or metadata.get("periodEnd") != report_period or
                metadata.get("contentHash") != digest or entry.get("publishedDate") != published):
            return None, "exact FY24 CURRENT/FY25 successor report binding differs"
    quotes = []
    for aid in ("1223217192", "1223217226"):
        pages = documents[aid]["pages"]
        patterns = (
            r"公司对\s*2024\s*年期初及\s*2023\s*年度相关财务报表数据进行追溯调整",
            r"山重建机被合并前受公司控股股东山东重工集团有限公司控制.{0,45}同一控制下企业合并",
            r"对\s*2023\s*年度合并利润表追溯调整如下",
            r"对\s*2023\s*年度合并现金流量表追溯调整如下",
        )
        for pattern in patterns:
            quote = _quote(pages, pattern)
            if not quote:
                return None, "explicit complete FY2023 flow/2024 opening scope missing"
            quotes.append({"announcementId": aid, **quote})
    included = _quote(documents["1223217214"]["pages"][240:241],
        r"本期发生同一控制下企业合并的.{0,70}被合并方在合并前实现的净利润为.{0,8}103,720,639\.73")
    if not included:
        return None, "FY24 report does not independently declare the pre-merger profit already included"
    # _quote numbers relative to its supplied page sequence; restore the
    # physical page so persisted evidence cannot mislabel p241 as p1.
    included["page"] += 240
    quotes.append({"announcementId": "1223217214", **included})
    return {
        "basis": "FINITE_000680_FY24_CURRENT_ALREADY_CONSOLIDATED_AND_UNCHANGED_SUCCESSOR",
        "sourceQuotes": quotes,
        "scope": {
            "targetFactPeriodEnd": period, "targetMetric": metric, "targetColumnRole": "CURRENT",
            "currentAnnouncementId": "1223217214", "successorAnnouncementId": "1225008978",
            "noticeFlowPeriodEnd": "2023-12-31", "noticeBalanceRole": "2024 opening",
            "capturedFinancialCauseIds": sorted(notices),
            "accountantNoticeNativeTextReliedOn": False,
            "meaning": "Only the selected FY24 CURRENT revenue is outside the separate FY2023-flow/2024-opening restatement. The FY24 report already includes the merger; the ordinary complete-set comparisons must still prove the same FY24 fact in FY25 PRIOR unchanged. This is not a no-economic-effect claim and does not cover H124, H125, other metrics or an uncaptured notice.",
        },
    }, None


_H124_REVENUE_CONCORDANCE_ROWS = {
    "2024-06-30": {
        "total_revenue": ("一、营业总收入", ("9,061,263,220.34", "9,096,122,125.03")),
        "revenue": ("其中：营业收入", ("9,061,263,220.34", "9,096,122,125.03")),
        "total_cost": ("二、营业总成本", ("8,609,611,445.82", "8,915,622,870.73")),
        "cost": ("其中：营业成本", ("7,789,950,638.63", "8,046,357,096.96")),
        "tax": ("税金及附加", ("30,862,469.19", "40,366,035.81")),
        "selling": ("销售费用", ("61,587,397.88", "54,382,019.69")),
        "administrative": ("管理费用", ("299,882,321.77", "342,946,554.44")),
        "research": ("研发费用", ("356,872,167.29", "345,246,696.04")),
        "finance": ("财务费用", ("70,456,451.06", "86,324,467.79")),
    },
    "2025-06-30": {
        "total_revenue": ("一、营业总收入", ("12,004,585,375.22", "13,189,293,125.38")),
        "revenue": ("其中：营业收入", ("12,004,585,375.22", "13,189,293,125.38")),
        "total_cost": ("二、营业总成本", ("11,066,238,710.02", "11,648,964,895.26")),
        "cost": ("其中：营业成本", ("9,749,816,134.73", "10,244,682,447.12")),
        "tax": ("税金及附加", ("226,584,269.15", "213,089,403.45")),
        "selling": ("销售费用", ("69,203,805.69", "76,336,562.48")),
        "administrative": ("管理费用", ("441,536,506.21", "421,574,625.74")),
        "research": ("研发费用", ("347,785,448.25", "458,935,745.90")),
        "finance": ("财务费用", ("231,312,545.99", "234,346,110.57")),
    },
}
_H124_AUXILIARY_PDF_RECEIPTS = {
    "1220963854": "rcpt_e28a1a6d3cd6259c51634841",
    "1224559959": "rcpt_355b862a304411e74bf45b81",
}
_H124_CANONICAL_PDF_RECEIPTS = {
    "1220963862": "rcpt_d01d1847d6b162d3a7c34a69",
    "1224559960": "rcpt_e30a9e2d8c7ff6f996fd14bd",
}
_H124_AUXILIARY_CANONICAL = {
    "1220963854": {"period": "2024-06-30", "canonical": "1220963862",
        "sourceRole": "SAME_PERIOD_CURRENT"},
    "1224559959": {"period": "2025-06-30", "canonical": "1224559960",
        "sourceRole": "SUCCESSOR_PRESENTED_PRIOR"},
}
_H124_NATIVE_AMOUNT = re.compile(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}")


def _native_000422_h124_income_statement_rows(page_text):
    """Read nine exact consolidated income-statement rows from one native page."""
    lines = [re.sub(r"\s+", " ", line).strip()
             for line in str(page_text or "").splitlines() if line.strip()]
    rows = {}
    for key, (label, _) in _H124_REVENUE_CONCORDANCE_ROWS["2024-06-30"].items():
        compact_label = re.sub(r"\s+", "", label)
        matches = [line for line in lines
                   if compact_label in re.sub(r"\s+", "", line)]
        if len(matches) != 1:
            return None, f"native consolidated row {label} is absent or duplicated"
        amounts = tuple(_H124_NATIVE_AMOUNT.findall(matches[0]))
        if len(amounts) != 2:
            return None, f"native consolidated row {label} does not have exactly two amount cells"
        rows[key] = {"amounts": amounts, "sourceLine": matches[0]}
    return rows, None


def _h124_native_rows_match_period(rows, period):
    expected = _H124_REVENUE_CONCORDANCE_ROWS.get(period)
    return bool(expected and rows and set(rows) == set(expected) and all(
        rows[key]["amounts"] == expected_pair for key, (_, expected_pair) in expected.items()))


def _h124_explicit_canonical_prior_binding(cell):
    if (not cell or cell.get("currentHeader") != "2025年半年度"
            or cell.get("priorHeader") != "2024年半年度"
            or cell.get("priorFactPeriodEnd") != "2024-06-30"
            or cell.get("priorAmountYuan") != "13189293125.38"):
        return None
    return {
        "basis": "CANONICAL_H125_P66_EXPLICIT_H124_PRIOR_COLUMN",
        "canonicalAnnouncementId": "1224559960", "canonicalPage": 66,
        "canonicalPriorHeader": cell["priorHeader"],
        "canonicalPriorFactPeriodEnd": cell["priorFactPeriodEnd"],
        "canonicalPriorAmountYuan": cell["priorAmountYuan"],
        "periodInferredFromAuxiliaryHeader": False,
    }


def _native_000422_h124_pdf_rows(body, physical_page):
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(body), strict=False)
        if len(reader.pages) < physical_page:
            return None, f"native PDF has no physical page {physical_page}"
        text = reader.pages[physical_page - 1].extract_text() or ""
    except Exception as exc:
        return None, f"native PDF page {physical_page} extraction failed: {type(exc).__name__}: {exc}"
    return _native_000422_h124_income_statement_rows(text)


def _h124_auxiliary_duplicate_concordance(by_id, report_periods, columns, selected_report_ids):
    """Bind two exact same-day auxiliary statements to their canonical p66 reports."""
    if not {"1220963862", "1224559960"} <= set(selected_report_ids):
        return None, None, "canonical H124/H125 reports are not both selected for auxiliary concordance"
    canonical_cells = {
        "2024-06-30": columns.get("1220963862", {}).get("revenue"),
        "2025-06-30": columns.get("1224559960", {}).get("revenue"),
    }
    h124, h125 = canonical_cells["2024-06-30"], canonical_cells["2025-06-30"]
    explicit_prior_binding = _h124_explicit_canonical_prior_binding(h125)
    if (not h124 or h124.get("currentHeader") != "2024年半年度"
            or h124.get("amountYuan") != "9061263220.34"
            or not h125 or h125.get("currentHeader") != "2025年半年度"
            or h125.get("amountYuan") != "12004585375.22"
            or explicit_prior_binding is None):
        return None, None, "canonical p66 CURRENT/H125 explicit H124 PRIOR cells are not bound"

    for aid, exact in _H124_AUXILIARY_CANONICAL.items():
        item = by_id.get(aid)
        if not item or report_periods.get(aid) != exact["period"]:
            return None, None, f"indexed H124 auxiliary report {aid} is absent or has a different period"
        expected = _AUXILIARY_000422_REPORTS[aid]
        metadata, entry, receipt = item["metadata"], item["entry"], item["receipt"]
        binding = _financial_source_binding(metadata, entry, selected_canonical_report=False)
        if (receipt.get("receipt_id") != _H124_AUXILIARY_PDF_RECEIPTS[aid]
                or receipt.get("source_id") != "cninfo" or receipt.get("outcome") != "OK"
                or receipt.get("http_status") != 200
                or receipt.get("content_hash") != expected["contentHash"]
                or receipt.get("url") != expected["documentUrl"]
                or binding.get("pdfReceiptId") != _H124_AUXILIARY_PDF_RECEIPTS[aid]
                or not _index_bound_000422_auxiliary_identity(
                    item.get("pages", ()), entry["title"], date.fromisoformat(exact["period"]),
                    ISSUERS["000422"], "000422", binding)):
            return None, None, f"H124 auxiliary source {aid} PDF receipt/SHA/index/period identity differs"

    corroborations, auxiliary_cells = {}, {}
    for aid, exact in _H124_AUXILIARY_CANONICAL.items():
        canonical_id, period = exact["canonical"], exact["period"]
        expected_rows = _H124_REVENUE_CONCORDANCE_ROWS[period]
        canonical, auxiliary = by_id[canonical_id], by_id[aid]
        canonical_rows, error = _native_000422_h124_pdf_rows(canonical["body"], 66)
        if error:
            return None, None, f"canonical report {canonical_id} p66 concordance parse failed: {error}"
        auxiliary_rows, error = _native_000422_h124_pdf_rows(auxiliary["body"], 7)
        if error:
            return None, None, f"auxiliary report {aid} p7 concordance parse failed: {error}"
        if (not _h124_native_rows_match_period(canonical_rows, period)
                or not _h124_native_rows_match_period(auxiliary_rows, period)):
            return None, None, f"native nine-row amount pair mismatch for {aid}"
        row_evidence = []
        for key, (_, expected_pair) in expected_rows.items():
            canonical_pair, auxiliary_pair = canonical_rows[key]["amounts"], auxiliary_rows[key]["amounts"]
            row_evidence.append({"rowKey": key, "canonicalAmounts": list(canonical_pair),
                "auxiliaryAmounts": list(auxiliary_pair), "exactMatch": True,
                "canonicalLine": canonical_rows[key]["sourceLine"],
                "auxiliaryLine": auxiliary_rows[key]["sourceLine"]})

        metadata, entry, receipt = auxiliary["metadata"], auxiliary["entry"], auxiliary["receipt"]
        canonical_meta, canonical_entry = canonical["metadata"], canonical["entry"]
        canonical_receipt = canonical_meta.get("receipt", {})
        canonical_pdf_receipt = _H124_CANONICAL_PDF_RECEIPTS[canonical_id]
        if (canonical["receipt"].get("receipt_id") != canonical_pdf_receipt
                or canonical_receipt.get("receipt_id") != canonical_pdf_receipt
                or canonical["receipt"].get("content_hash") != canonical_meta.get("contentHash")
                or canonical["receipt"].get("url") != canonical_entry.get("documentUrl")):
            return None, None, f"canonical report {canonical_id} exact PDF receipt/SHA/URL binding differs"
        period_binding = ({
            "basis": "AUXILIARY_REPORT_EXPLICIT_2024H1_CURRENT_PERIOD",
            "auxiliaryPeriodEnd": "2024-06-30", "columnRole": "CURRENT",
            "priorColumnHeader": "上期金额", "priorPeriodEnd": None,
            "priorPeriodResolution": "UNRESOLVED_AND_NOT_USED",
        } if aid == "1220963854" else {
            **explicit_prior_binding,
            "auxiliaryPriorColumnHeader": "上期金额",
        })
        evidence = {
            "basis": "EXACT_SAME_DAY_AUXILIARY_TO_CANONICAL_NINE_ROW_CONCORDANCE",
            "sourceRole": exact["sourceRole"], "periodEnd": period,
            "auxiliaryReport": {"announcementId": aid, "title": entry["title"],
                "pdfSha256": metadata["contentHash"], "pdfReceiptId": receipt["receipt_id"],
                "documentUrl": entry["documentUrl"], "publishedDate": entry["publishedDate"],
                "page": 7, "pageLabel": "reported PDF p7",
                "indexSecCode": entry.get("secCode"), "indexOrgId": entry.get("orgId"),
                "indexReceiptId": entry.get("indexReceiptId"),
                "indexResponseHash": entry.get("indexResponseHash"),
                "columnHeaders": ["本期金额", "上期金额"]},
            "canonicalReport": {"announcementId": canonical_id, "title": canonical_entry["title"],
                "pdfSha256": canonical_meta["contentHash"],
                "pdfReceiptId": canonical_receipt.get("receipt_id"),
                "documentUrl": canonical_entry["documentUrl"],
                "publishedDate": canonical_entry["publishedDate"],
                "page": 66, "pageLabel": "reported PDF p66",
                "currentHeader": ("2024年半年度" if period == "2024-06-30" else "2025年半年度")},
            "periodBinding": period_binding,
            "matchedRowCount": len(row_evidence), "rows": row_evidence,
        }
        revenue = auxiliary_rows["revenue"]
        current_amount, comparative_amount = (amount.replace(",", "") for amount in revenue["amounts"])
        auxiliary_cells[aid] = {
            "metric": "revenue", "statementScope": "CONSOLIDATED",
            "statement": STATEMENT["revenue"], "factPeriodEnd": period,
            "columnRole": "CURRENT", "currentHeader": "本期金额",
            "priorHeader": "上期金额", "unitRaw": "元", "amountYuan": current_amount,
            "priorAmountYuan": None if aid == "1220963854" else comparative_amount,
            "priorFactPeriodEnd": None if aid == "1220963854" else "2024-06-30",
            "priorPeriodResolution": ("UNRESOLVED_AND_NOT_USED" if aid == "1220963854"
                else "BOUND_TO_CANONICAL_H125_P66_EXPLICIT_PRIOR"),
            "page": 7, "sourceLine": revenue["sourceLine"],
            "rowLabel": "其中：营业收入", "tablePages": [7, 7],
            "sourceNativeEvidence": evidence,
        }
        corroborations[aid] = evidence
    return {aid: {"revenue": cell} for aid, cell in auxiliary_cells.items()}, corroborations, None


def _finite_000422_h124_revenue_policy(code, metric, period, target_id, documents, financial,
                                     current_cell, requested_metrics, report_periods, columns,
                                     selected_report_ids=()):
    """Bind only the 000422 H124 revenue CURRENT to its H125 PRIOR restatement.

    The generic status calculation still decides AFFECTS from the parsed
    same-fact comparison. This finite profile adds native p8/p66 and merger
    identity evidence without attributing the change to one notice.
    """
    if (code, metric, period, target_id) != (
            "000422", "revenue", "2024-06-30", "1220963862"):
        return None, "outside the exact 000422 H124 revenue CURRENT report profile"
    if set(requested_metrics) != {"revenue"}:
        return None, "profile only covers the exact H124 revenue field"
    expected_causes = {
        "1222991641": ("2025-04-03",
            "关于湖北宜化化工股份有限公司2024年度追溯调整前期财务报表数据的专项说明-信会师报字[2025]第ZE10074号",
            "sha256:9378ef0cd160da7ead1881ca1e4db5e50b00d5c45db6c0f773eab2a12406743a"),
        "1222991666": ("2025-04-03", "董事会关于2024年度追溯调整前期财务报表数据的说明",
            "sha256:18a7701e0d52d83f6c5d86eb4dab7c55c4be3af2283c30bf8a3ae1c9f135c207"),
        "1225183650": ("2026-04-25",
            "关于湖北宜化化工股份有限公司同一控制下企业合并追溯调整前期财务报表数据的专项说明 信会师报字[2026]第ZE10202号",
            "sha256:f46e817c54a818eae59a42579b4a0f5d8bef85707473d441c9a339e735168cb0"),
        "1225183652": ("2026-04-25", "关于同一控制下企业合并追溯调整前期财务报表数据的公告",
            "sha256:b090c18e1fae1a1335140676251959070a262d880d18cd10e1d15b639163fac1"),
    }
    if financial != set(expected_causes):
        return None, "complete captured 000422 financial-correction cause set differs"
    if not {"1220963862", "1224559960"} <= set(selected_report_ids):
        return None, "H124/H125 canonical report sources are not both selected"

    expected_reports = {
        "1220963862": ("2024年半年度报告", "2024-08-26",
            "sha256:e8136a903b0c3f75affd1cabb18db9052bf1662d00b84fd3b99f2d9bfc8ff8f0",
            "2024-06-30"),
        "1224559960": ("2025年半年度报告", "2025-08-23",
            "sha256:9d110cdb52096cc04fb358f886c447ebe58022813bcdeb8a3f7c2de237046f78",
            "2025-06-30"),
    }
    for aid, (title, published, digest, report_period) in expected_reports.items():
        item = documents.get(aid)
        if not item:
            return None, "fixed H124/H125 report source is absent: " + aid
        metadata, entry = item["metadata"], item["entry"]
        expected_url = f"https://static.cninfo.com.cn/finalpage/{published}/{aid}.PDF"
        if (metadata.get("announcementId") != aid or metadata.get("code") != code or
                metadata.get("instrumentId") != "SZ.000422" or
                metadata.get("periodEnd") != report_period or metadata.get("contentHash") != digest or
                metadata.get("documentUrl") != expected_url or entry.get("title") != title or
                entry.get("publishedDate") != published or entry.get("documentUrl") != expected_url or
                report_periods.get(aid) != report_period):
            return None, "fixed H124/H125 report identity/date/SHA/period differs: " + aid
    for aid, (published, title, digest) in expected_causes.items():
        item = documents.get(aid)
        if not item:
            return None, "required captured financial cause is absent: " + aid
        entry, metadata = item["entry"], item["metadata"]
        expected_url = f"https://static.cninfo.com.cn/finalpage/{published}/{aid}.PDF"
        if (entry.get("title") != title or entry.get("publishedDate") != published or
                entry.get("documentUrl") != expected_url or metadata.get("contentHash") != digest or
                metadata.get("code") != code or metadata.get("instrumentId") != "SZ.000422"):
            return None, "captured financial cause identity differs: " + aid

    exact = {
        "1220963862": ("2024-06-30", "CURRENT", "2024年半年度",
            "9061263220.34", 66),
        "1224559960": ("2025-06-30", "CURRENT", "2025年半年度",
            "12004585375.22", 66),
    }
    for aid, (fact_period, role, header, amount, page) in exact.items():
        cell = current_cell if aid == "1220963862" else (columns or {}).get(aid, {}).get("revenue")
        expected_amount = amount if aid == "1220963862" else "12004585375.22"
        if (not cell or cell.get("metric") != "revenue" or cell.get("factPeriodEnd") != fact_period or
                cell.get("columnRole") != role or cell.get("statementScope") != "CONSOLIDATED" or
                cell.get("statement") != "利润表" or cell.get("unitRaw") != "元" or
                cell.get("currentHeader") != header or cell.get("amountYuan") != expected_amount or
                cell.get("page") != page or not cell.get("sourceLine")):
            return None, "native H124/H125 consolidated revenue CURRENT cell is not bound: " + aid
    current = current_cell
    prior = (columns or {}).get("1224559960", {}).get("revenue")
    if (not prior or prior.get("priorFactPeriodEnd") != period or
            prior.get("priorHeader") != "2024年半年度" or
            prior.get("priorAmountYuan") != "13189293125.38" or
            prior.get("statementScope") != "CONSOLIDATED" or prior.get("statement") != "利润表" or
            prior.get("unitRaw") != "元" or prior.get("page") != 66 or
            not prior.get("sourceLine") or Decimal(prior.get("priorAmountYuan")) ==
            Decimal(current.get("amountYuan"))):
        return None, "H125 original report does not provide the changed H124 revenue PRIOR cell"

    quotes = []
    def require_quote(aid, pattern, expected_page):
        item = documents.get(aid)
        found = _quote(item.get("pages", ()) if item else (), pattern)
        if not found or found.get("page") != expected_page:
            return False
        quotes.append({"announcementId": aid, **found})
        return True

    checks = [
        require_quote("1220963862",
            r"其中\s*[:：]?\s*营业收入\s+9,061,263,220\.34\s+9,096,122,125\.03", 66),
        require_quote("1224559960", r"追溯调整或重述原因\s+同一控制下企业合并", 8),
        require_quote("1224559960",
            r"营业收入（元）\s+12,004,585,375\.22\s+9,061,263,220\.34\s+13,189,293,125\.38", 8),
        require_quote("1224559960",
            r"其中\s*[:：]?\s*营业收入\s+12,004,585,375\.22\s+13,189,293,125\.38", 66),
        require_quote("1224559960",
            r"宜昌新发[\s\S]{0,120}?100\.00%[\s\S]{0,180}?同受湖北宜化集\s*团有限责任公司"
            r"[\s\S]{0,100}?控制[\s\S]{0,40}?非暂时性[\s\S]{0,160}?2025\s*年\s*05\s*月\s*30\s*日", 172),
    ]
    if not all(checks):
        return None, "native H124/H125 p66, H125 p8 before/after, or p172 merger identity quote is missing"
    quotes.extend([
        {"announcementId": "1220963862", "page": current["page"], "quote": current["sourceLine"]},
        {"announcementId": "1224559960", "page": prior["page"], "quote": prior["sourceLine"]},
    ])
    delta = Decimal(prior["priorAmountYuan"]) - Decimal(current["amountYuan"])
    return {
        "basis": "FINITE_000422_H124_REVENUE_CURRENT_TO_H125_PRESENTED_PRIOR",
        "sourceQuotes": quotes,
        "causalAttribution": None,
        "scope": {
            "targetFactPeriodEnd": period, "targetMetric": metric,
            "currentAnnouncementId": "1220963862", "currentPdfSha256": expected_reports["1220963862"][2],
            "currentPage": 66, "currentColumnHeader": "2024年半年度",
            "currentAmountYuan": current["amountYuan"],
            "presentedPriorAnnouncementId": "1224559960",
            "presentedPriorSourceReportPeriodEnd": "2025-06-30",
            "presentedPriorFactPeriodEnd": period, "presentedPriorColumnRole": "PRIOR",
            "presentedPriorPdfSha256": expected_reports["1224559960"][2],
            "presentedPriorPage": 66, "presentedPriorColumnHeader": prior["priorHeader"],
            "presentedPriorAmountYuan": prior["priorAmountYuan"],
            "deltaLaterMinusOriginalYuan": str(delta),
            "h125RestatementReason": "同一控制下企业合并",
            "mergerIdentity": {"announcementId": "1224559960", "page": 172,
                "entity": "宜昌新发产业投资有限公司", "ownership": "100.00%",
                "controller": "湖北宜化集团有限责任公司", "controlNonTemporary": True,
                "mergerDate": "2025-05-30"},
            "capturedFinancialCauseIds": sorted(expected_causes),
            "meaning": ("The official H125 report labels retrospective adjustment as a same-control merger, "
                "identifies 宜昌新发 as a 100% same-control merger dated 2025-05-30, and presents a "
                "different 2024H1 revenue value. This proves AFFECTS for this captured same-fact pair only; "
                "it does not assign the change to one of the four captured notices or admit either version."),
        },
    }, None


def _finite_000422_h125_four_policy(code, metric, period, target_id, documents, financial,
                                   current_cell, requested_metrics, report_periods, columns,
                                   selected_report_ids=()):
    """Bound the four H125 facts to the two explicit historic notice scopes and H126 successor."""
    exact_metrics = {"net_profit_attributable", "net_profit_consolidated",
                     "operating_cashflow", "revenue"}
    expected_causes = {
        "1222991641": ("2025-04-03",
            "关于湖北宜化化工股份有限公司2024年度追溯调整前期财务报表数据的专项说明-信会师报字[2025]第ZE10074号",
            "sha256:9378ef0cd160da7ead1881ca1e4db5e50b00d5c45db6c0f773eab2a12406743a"),
        "1222991666": ("2025-04-03", "董事会关于2024年度追溯调整前期财务报表数据的说明",
            "sha256:18a7701e0d52d83f6c5d86eb4dab7c55c4be3af2283c30bf8a3ae1c9f135c207"),
        "1225183650": ("2026-04-25",
            "关于湖北宜化化工股份有限公司同一控制下企业合并追溯调整前期财务报表数据的专项说明 信会师报字[2026]第ZE10202号",
            "sha256:f46e817c54a818eae59a42579b4a0f5d8bef85707473d441c9a339e735168cb0"),
        "1225183652": ("2026-04-25", "关于同一控制下企业合并追溯调整前期财务报表数据的公告",
            "sha256:b090c18e1fae1a1335140676251959070a262d880d18cd10e1d15b639163fac1"),
    }
    if (code, period, target_id) != ("000422", "2025-06-30", "1224559960"):
        return None, "outside the exact 000422 H125 CURRENT report profile"
    if not requested_metrics or not set(requested_metrics) <= exact_metrics or metric not in exact_metrics:
        return None, "profile only covers the four H125 income/cashflow fields"
    if financial != set(expected_causes):
        return None, "complete captured 000422 financial-correction cause set differs"

    quotes = []
    def require_quote(aid, pattern):
        item = documents.get(aid)
        quote = _quote(item.get("pages", ()) if item else (), pattern)
        if quote:
            quotes.append({"announcementId": aid, **quote})
        return quote is not None

    for aid, (published, title, digest) in expected_causes.items():
        item = documents.get(aid)
        if not item:
            return None, f"required official financial notice {aid} is absent"
        entry, metadata = item["entry"], item["metadata"]
        expected_url = f"https://static.cninfo.com.cn/finalpage/{published}/{aid}.PDF"
        if (entry.get("title") != title or entry.get("publishedDate") != published
                or entry.get("documentUrl") != expected_url
                or metadata.get("contentHash") != digest):
            return None, f"official financial notice {aid} title/date/URL/SHA differs"

    opaque_cause = documents.get("1225183650")
    if not opaque_cause:
        return None, "opaque CPA cause 1225183650 is absent from the complete captured cause set"
    opaque_binding = _financial_source_binding(opaque_cause["metadata"], opaque_cause["entry"],
        selected_canonical_report="1225183650" in set(selected_report_ids))
    opaque_identity = _index_bound_000422_opaque_cause_identity(
        opaque_cause.get("pages", ()), opaque_cause["entry"]["title"],
        ISSUERS["000422"], code, opaque_binding)
    if not opaque_identity:
        return None, "exact opaque CPA cause identity/index binding differs"

    auxiliary_index_bindings = []
    for aid, expected in _AUXILIARY_000422_REPORTS.items():
        item = documents.get(aid)
        if not item:
            return None, f"required index-bound auxiliary financial report {aid} is absent"
        binding = _financial_source_binding(item["metadata"], item["entry"],
            selected_canonical_report=aid in set(selected_report_ids))
        period_date = date.fromisoformat(expected["period"])
        if not _index_bound_000422_auxiliary_identity(
                item.get("pages", ()), item["entry"]["title"],
                period_date, ISSUERS["000422"], code, binding):
            return None, f"index-bound auxiliary financial report {aid} identity/source binding differs"
        start_date = period_date.replace(month=1, day=1)
        period_pattern = (rf"{start_date.year}\s*年\s*0?1\s*月\s*0?1\s*日\s*(?:至|到)"
            rf"{period_date.year}\s*年\s*0?{period_date.month}\s*月\s*0?{period_date.day}\s*日\s*止")
        statement_pattern = (re.escape(ISSUERS["000422"])
            + r"[\s\S]{0,100}?合并\s*资产负债表[\s\S]{0,35}?"
            + rf"{period_date.year}\s*年\s*0?{period_date.month}\s*月\s*0?{period_date.day}\s*日")
        issuer_quote = _quote(item["pages"][:8], re.escape(ISSUERS["000422"]))
        compact_pages = [re.sub(r"\s+", "", page) for page in item["pages"]]
        period_quote = _quote(compact_pages[:8], period_pattern)
        statement_quote = _quote(compact_pages[:12], statement_pattern)
        if not issuer_quote or not period_quote or not statement_quote:
            return None, (f"native identity/report-period/statement quote missing for auxiliary source {aid} "
                f"(issuer={bool(issuer_quote)}, period={bool(period_quote)}, statement={bool(statement_quote)})")
        quotes.extend([{"announcementId": aid, **issuer_quote},
                       {"announcementId": aid, **period_quote},
                       {"announcementId": aid, **statement_quote}])
        auxiliary_index_bindings.append({"announcementId": aid,
            "pdfSha256": binding["contentHash"], "periodEnd": expected["period"],
            "identityBasis": "INDEX_BOUND_ONLY",
            "nativeCodePresent": any("000422" in page for page in item["pages"]),
            "nativeIssuerQuote": issuer_quote, "nativePeriodQuote": period_quote,
            "consolidatedStatementQuote": statement_quote,
            "currentIndex": {"secCode": binding["indexSecCode"], "orgId": binding["indexOrgId"],
                "documentUrl": binding["documentUrl"], "publishedDate": binding["publishedDate"],
                "receiptId": binding["indexReceiptId"], "responseHash": binding["indexResponseHash"]}})

    special = documents.get("1220963855")
    if not special:
        return None, "indexed fundraising special report was not retained as a related source"
    special_meta, special_entry = special["metadata"], special["entry"]
    if (special_entry.get("title") != "董事会关于2024年半年度募集资金存放与使用情况的专项报告"
            or special_entry.get("publishedDate") != "2024-08-26"
            or special_meta.get("contentHash") != "sha256:e21b05cb82095d9a4ec6c77f897ad68274b8b1159d8a001db0fc4aebd2bee70f"
            or special_meta.get("periodEnd") is not None
            or special_meta.get("declaredPeriodEnd") != "2024-06-30"
            or special_meta.get("documentKind") != "RELATED_SUPPORTING_SPECIAL_REPORT"
            or special_meta.get("periodicReportClassification") != "RELATED_SUPPORTING_SPECIAL_REPORT"):
        return None, "fundraising special report identity/classification/declared period differs"
    special_pages = special.get("pages", ())
    special_issuer = _quote(special_pages, re.escape("湖北宜化化工股份有限公司"))
    special_code = _quote(special_pages, r"(?:证券|股票)代码\s*[:：]?\s*000422\b")
    if (not special_issuer or special_meta.get("code") != code
            or special_meta.get("instrumentId") != "SZ.000422"
            or special_entry.get("secCode") != code
            or special_entry.get("orgId") != "gssz0000422"
            or special_meta.get("indexReceiptId") != special_entry.get("indexReceiptId")
            or special_meta.get("indexResponseHash") != special_entry.get("indexResponseHash")):
        return None, "fundraising special report native issuer or current-index-bound identity differs"
    quotes.append({"announcementId": "1220963855", **special_issuer})

    exact_reports = {
        "1224559959": ("2025年半年度财务报告", "2025-08-23",
            "sha256:c9d3c7164431ebb69592dd76f9520195b6d8194ec60fbaa69295078f872ed8c3", "2025-06-30"),
        "1224559960": ("2025年半年度报告", "2025-08-23",
            "sha256:9d110cdb52096cc04fb358f886c447ebe58022813bcdeb8a3f7c2de237046f78", "2025-06-30"),
        "1225468007": ("2026年半年度报告", "2026-08-12",
            "sha256:6963a7b5ca322c8fe53d59eafe383bb882689ad9e2a9b78aa669373f58de4e12", "2026-06-30"),
    }
    for aid, (title, published, digest, report_period) in exact_reports.items():
        item = documents.get(aid)
        if not item:
            return None, f"required H125/H126 report {aid} is absent"
        metadata, entry = item["metadata"], item["entry"]
        if (metadata.get("periodEnd") != report_period or metadata.get("contentHash") != digest
                or entry.get("title") != title or entry.get("publishedDate") != published
                or report_periods.get(aid) != report_period):
            return None, f"H125/H126 report {aid} identity/date/SHA/period differs"

    scope_checks = [
        ("1222991641", r"对\s*2023\s*年\s*12\s*月\s*31\s*日合并资产负债表项目的影响如下"),
        ("1222991641", r"对\s*2023\s*年度合并利润表的影响如下"),
        ("1222991641", r"对\s*2023\s*年度合并现金流量表的影响如下"),
        ("1222991666", r"对\s*2023\s*年\s*12\s*月\s*31\s*日合并资产负债表项目的影响如下"),
        ("1222991666", r"对\s*2023\s*年度合并利润表的影响如下"),
        ("1222991666", r"对\s*2023\s*年度合并现金流量表的影响如下"),
        ("1225183652", r"对合并资产负债表\s*2024\s*年\s*12\s*月\s*31\s*日余额追溯调整如下"),
        ("1225183652", r"对合并利润表\s*2024\s*年度金额追溯调整如下"),
        ("1225183652", r"对合并现金流量表\s*2024\s*年度金额追溯调整如下"),
        ("1225183652", r"合并日为\s*2025\s*年\s*5\s*月\s*30\s*日"),
        ("1225183652", r"编制\s*2025\s*年度合并财务报表时对公司前期财务数据进行\s*追溯调整"),
        ("1222991666", r"单位\s*[:：]\s*元\s*项目\s*重塑前\s*同控合并影响\s*重塑后"),
        ("1222991641", r"一、营业总收入\s*17,042,037,265\.76\s*3,764,077\.44\s*17,045,801,343\.20"),
        ("1222991641", r"五、净利润.{0,60}805,529,781\.98\s*-1,006,001\.24\s*804,523,780\.74"),
        ("1222991641", r"经营活动产生的现金流量净额\s*1,626,449,159\.93\s*3,137,777\.09\s*1,629,586,937\.02"),
        ("1225183652", r"营业总收入\s*16,963,593,887\.39\s*25,394,172,300\.46\s*8,430,578,413\.07"),
        ("1225183652", r"五、净利润.{0,80}893,723,240\.87\s*2,143,573,106\.10\s*1,249,849,865\.23"),
        ("1225183652", r"归属于母公司股东的.{0,80}652,544,554\.42\s*1,067,484,892\.73\s*414,940,338\.31"),
        ("1225183652", r"经营活动产生的现金流量净额\s*768,224,617\.68\s*4,415,405,597\.01\s*3,647,180,979\.33"),
    ]
    for aid, pattern in scope_checks:
        if not require_quote(aid, pattern):
            return None, f"explicit original-body correction scope/affected-row evidence missing for {aid}"

    expected_cells = {
        "net_profit_attributable": ("398880278.09", 67, "归属于母公司股东的净利润"),
        "net_profit_consolidated": ("753547510.48", 67, "净利润"),
        "operating_cashflow": ("1287155858.35", 70, "经营活动产生的现金流量净额"),
        "revenue": ("12004585375.22", 66, "营业收入"),
    }
    same_day_pages = {"net_profit_attributable": 7, "net_profit_consolidated": 7,
                      "operating_cashflow": 9, "revenue": 7}
    current = columns.get("1224559960", {}).get(metric)
    same_day = columns.get("1224559959", {}).get(metric)
    successor = columns.get("1225468007", {}).get(metric)
    amount, page, label = expected_cells[metric]
    compact = lambda value: re.sub(r"\s+", "", str(value or ""))
    if (not current or current_cell != current or not same_day or not successor
            or current.get("factPeriodEnd") != period or current.get("columnRole") != "CURRENT"
            or current.get("statementScope") != "CONSOLIDATED" or current.get("unitRaw") != "元"
            or current.get("currentHeader") != "2025年半年度" or current.get("amountYuan") != amount
            or current.get("page") != page or label not in compact(current.get("rowLabel"))
            or same_day.get("amountYuan") != amount or same_day.get("factPeriodEnd") != period
            or same_day.get("unitRaw") != "元" or label not in compact(same_day.get("rowLabel"))
            or same_day.get("page") != same_day_pages[metric]
            or same_day.get("currentHeader") != "本期金额" or same_day.get("priorHeader") != "上期金额"
            or same_day.get("priorFactPeriodEnd") is not None or same_day.get("priorAmountYuan") is not None
            or same_day.get("priorPeriodResolution") != "UNRESOLVED"
            or same_day.get("priorPeriodUnresolvedReason") !=
                "原文列头仅为“上期金额”，没有明示比较期间；不由2025-06-30反推期间。"
            or not same_day.get("priorCellRaw")
            or successor.get("factPeriodEnd") != "2026-06-30"
            or successor.get("priorFactPeriodEnd") != period
            or successor.get("priorHeader") != "2025年半年度"
            or successor.get("priorAmountYuan") != amount or successor.get("unitRaw") != "元"):
        return None, f"original H125/current, same-day full-statement, or H126 PRIOR cell differs for {metric}"
    quotes.extend([
        {"announcementId": "1224559960", "page": current["page"], "quote": current["sourceLine"]},
        {"announcementId": "1224559959", "page": same_day["page"], "quote": same_day["sourceLine"]},
        {"announcementId": "1225468007", "page": successor["page"], "quote": successor["sourceLine"]},
    ])
    return {"basis": "EXPLICIT_2023_AND_2024_RESTATEMENT_SCOPE_WITH_UNCHANGED_H125",
        "sourceQuotes": quotes,
        "scope": {"targetFactPeriodEnd": period, "targetMetric": metric,
            "currentAnnouncementId": "1224559960", "currentPublishedDate": "2025-08-23",
            "currentPdfSha256": exact_reports["1224559960"][2], "currentPage": current["page"],
            "currentRowLabel": current["rowLabel"], "currentUnit": current["unitRaw"],
            "currentAmountYuan": amount,
            "auxiliaryIdentityBindings": auxiliary_index_bindings,
            "opaqueCauseIdentity": opaque_identity,
            "sameDayStatementAnnouncementId": "1224559959",
            "sameDayStatementPdfSha256": exact_reports["1224559959"][2],
            "sameDayIdentityMode": "INDEX_BOUND_ONLY",
            "sameDayAmountYuan": same_day["amountYuan"],
            "sameDayCurrentHeader": same_day["currentHeader"],
            "sameDayPriorHeader": same_day["priorHeader"],
            "sameDayPriorCellRaw": same_day["priorCellRaw"],
            "sameDayPriorFactPeriodEnd": same_day["priorFactPeriodEnd"],
            "sameDayPriorAmountYuan": same_day["priorAmountYuan"],
            "sameDayPriorPeriodResolution": same_day["priorPeriodResolution"],
            "sameDayPriorPeriodUnresolvedReason": same_day["priorPeriodUnresolvedReason"],
            "successorAnnouncementId": "1225468007", "successorPublishedDate": "2026-08-12",
            "successorPdfSha256": exact_reports["1225468007"][2],
            "successorPriorFactPeriodEnd": period, "successorPriorHeader": successor["priorHeader"],
            "successorPriorAmountYuan": successor["priorAmountYuan"],
            "specialReportIdentityMode": "INDEX_BOUND_ONLY",
            "specialReportNativeIssuerQuote": special_issuer,
            "specialReportNativeCodePresent": bool(special_code),
            "reportedRestatementScopes": ["2023-12-31 balance sheet", "2023 annual income/cashflow",
                                          "2024-12-31 balance sheet", "2024 annual income/cashflow"],
            "capturedFinancialCauseIds": sorted(expected_causes),
            "fundraisingSpecialReport": {"announcementId": "1220963855", "periodEnd": None,
                "declaredPeriodEnd": "2024-06-30", "relatedSourceRetained": True},
            "meaning": "This is a selected H125 source-version finding only: both current same-day H125 statement sources and H126's H125 PRIOR show the same amount, while the four captured notices' explicit tables identify 2023 and FY2024 periods. This does not claim no business/economic effect, does not infer from omitted rows, and does not cover parent equity, FY25, H126, or other H125 fields."}}, None




def _policy(code, metric, period, documents, financial):
    """Only original-body-supported narrow profiles; unknown additions stay open."""
    if (code, metric, period) == ("000680", "revenue", "2024-12-31"):
        return _finite_000680_fy24_current_policy(code, metric, period, documents, financial)
    def text(aid):
        return documents.get(aid, {}).get("pages", ())
    if code == "000902":
        allowed = {"1225198545", "1225198546", "1225198547"}
        if financial != allowed:
            return None, "financial relationship set differs from supported three-document cause"
        q1 = _quote(text("1225198545"), r"本次差错更正为追溯调整.{0,90}2021-2024.{0,95}资本公积.{0,55}85,161\.12.{0,55}未分配利润.{0,55}85,161\.12.{0,80}上述调整不影响公司各期净利润.{0,30}仅对所有者权益内部结构产生影响")
        q2 = _quote(text("1225198546"), r"公司本次会计差错更正涉及\s*2018\s*年至\s*2020\s*年度财务报表及附注.{0,35}追溯调整.{0,20}2021-2024\s*年度财务报表及附注")
        if not q1 or not q2:
            return None, "explicit complete period/zero total-equity transfer proof missing"
        # Equal internal transfers leave the total unchanged; no flow change is
        # inferred merely because a row label does not appear in the PDF.
        return {"basis": "EXPLICIT_PERIOD_AND_ZERO_TOTAL_EQUITY_TRANSFER", "sourceQuotes": [
            {"announcementId": "1225198545", **q1}, {"announcementId": "1225198546", **q2}],
            "scope": {"changedFlowYears": [2018, 2019, 2020], "laterEffect": "equity internal reclassification",
                      "capitalReserveDeltaYuan": "85161.12", "retainedEarningsDeltaYuan": "-85161.12",
                      "totalParentEquityDeltaYuan": "0.00"}}, None
    if metric != "revenue":
        return None, "this issuer profile only proves revenue impact"
    if code == "600338" and period == "2024-06-30":
        allowed = {"1220732174", "1220732167", "1220732169", "1220732175", "1220732166"}
        if financial != allowed:
            return None, "complete captured five-document correction group differs"
        q = _quote(text("1220732174"), r"本次更正事项对公司\s*2023\s*年度财务报告的合并资产负债表.{0,85}具体如下")
        q2 = _quote(text("1220732167"), r"对公司\s*2023\s*年年度报告的合并财务\s*报表及相关附注进行更正")
        q3 = _quote(text("1220732174"), r"根据上述更正，公司已同步调整\s*2023\s*年年度报告全文第二节、第三节、第\s*十节以及摘要第二节中的对应内容或表述。.{0,80}更正后\s*2023\s*年年度报告全文与摘要")
        republications = []
        for aid in ("1220732175", "1220732166"):
            cover = _quote(text(aid)[:1], r"公司代码\s*[:：]\s*600338.{0,40}西藏珠峰资源股份有限公司\s*2023\s*年年度报告(?:摘要)?")
            if cover:
                republications.append({"announcementId": aid, **cover})
        # Rebuild the complete closed impact table, rather than interpreting a
        # missing revenue keyword as an exclusion.
        amount = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d+"
        rows = "".join(re.escape(label) + r"\s+" + (amount + r"\s+") * 3
                       for label in ("管理费用", "营业利润", "利润总额", "所得税费用", "净利润"))
        income = _quote(text("1220732174"), r"（二）对\s*2023\s*年度合并利润表报表项目的影响金额\s*[:：]\s*单位\s*[:：]\s*元\s+报表项目\s+更正前金额\s+更正金额\s+更正后金额\s+" + rows + r"（三）对\s*2023\s*年度合并现金流量表项目的影响金额")
        if q and q2 and q3 and income and len(republications) == 2:
            return {"basis": "EXPLICIT_OTHER_PERIOD_WITH_UNCHANGED_SUCCESSOR", "sourceQuotes": [
                {"announcementId": "1220732174", **q}, {"announcementId": "1220732167", **q2},
                {"announcementId": "1220732174", **q3}, {"announcementId": "1220732174", **income},
                *republications], "scope": {"reportPeriodEnd": "2023-12-31", "targetMetric": "revenue",
                    "completeIncomeAffectedRows": ["管理费用", "营业利润", "利润总额", "所得税费用", "净利润"]}}, None
    if code == "002602" and period == "2024-06-30":
        allowed = {"1223382757", "1223382727", "1223382692", "1223382691"}
        if financial != allowed:
            return None, "complete captured 2024 correction group differs"
        current = documents.get("1221081842")
        successor = documents.get("1224628793")
        if (not current or current.get("metadata", {}).get("periodEnd") != period or
                current.get("entry", {}).get("title") != "2024年半年度报告" or
                current.get("entry", {}).get("publishedDate") != "2024-08-31" or
                not successor or successor.get("metadata", {}).get("periodEnd") != "2025-06-30" or
                successor.get("entry", {}).get("title") != "2025年半年度报告" or
                successor.get("entry", {}).get("publishedDate") != "2025-08-30"):
            return None, "captured H1 current/successor report chronology differs"
        if any(documents.get(aid, {}).get("entry", {}).get("publishedDate") != "2025-04-29"
               for aid in allowed):
            return None, "captured 2024 correction publication dates differ"
        q = _quote(text("1223382691"), r"对\s*2024\s*年第三季度合并财务报表的影响.{0,2000}?对合并年初到报告期末利润表的影响.{0,850}?将重分类进损益的其他综合收益.{0,500}?归属于母公司所有者的综合收益总.{0,180}?本次会计差错更正对\s*2024\s*年合并年初到报告期末现金流量表无影响")
        if q:
            return {"basis": "EXPLICIT_2024_Q3_OCI_SCOPE_WITH_H1_SUCCESSOR_COMPARISON",
                    "sourceQuotes": [{"announcementId": "1223382691", **q}],
                    "scope": {"targetFactPeriodEnd": period,
                        "targetMetric": "revenue",
                        "currentAnnouncementId": "1221081842",
                        "successorAnnouncementId": "1224628793",
                        "correctionFlowPeriodEnd": "2024-09-30",
                        "correctionIncomeEffect": "other comprehensive income only",
                        "meaning": "The captured correction table is for 2024 Q3 year-to-date statements and identifies OCI effects. The selected H1 CURRENT and successor H1 PRIOR remain subject to ordinary original-cell parsing and equality checks across the complete captured report set."}}, None
    if code == "000899" and period == "2024-12-31":
        if financial != {"1223101826", "1225490303"}:
            return None, "financial relationship set differs from both captured merger notices"
        q1 = _quote(text("1223101826"), r"拟对\s*2023\s*年度相关财务数据进行追溯调整")
        q2 = _quote(text("1225490303"), r"拟对\s*2026\s*年度财务报表期初\s*数据及\s*2025\s*年半年度财务报表数据进行追溯调整")
        if q1 and q2:
            return {"basis": "EXPLICIT_OTHER_PERIODS_WITH_UNCHANGED_SUCCESSOR", "sourceQuotes": [
                {"announcementId": "1223101826", **q1}, {"announcementId": "1225490303", **q2}],
                "scope": {"flowPeriods": ["2023-12-31", "2025-06-30"], "balancePeriod": "2025-12-31"}}, None
    if code == "002602" and period == "2024-12-31":
        allowed = {"1223382757", "1223382727", "1223382692", "1223382691"}
        if financial != allowed:
            return None, "financial relationship set differs from complete correction group"
        q = _quote(text("1223382691"), r"对\s*2024\s*年第三季度合并财务报表的影响.{0,2000}?对合并年初到报告期末利润表的影响.{0,850}?将重分类进损益的其他综合收益.{0,500}?归属于母公司所有者的综合收益总.{0,180}?本次会计差错更正对\s*2024\s*年合并年初到报告期末现金流量表无影响")
        if q:
            return {"basis": "COMPLETE_TARGET_YEAR_FLOW_IMPACT_TABLE_AND_UNCHANGED_SUCCESSOR", "sourceQuotes": [
                {"announcementId": "1223382691", **q}], "scope": {"targetYearIncomeEffect": "other comprehensive income only"}}, None
    return None, "original-body period/field impact profile is unresolved"


def _finite_000422_h126_four_flow_policy(code, metric, period, target_id, documents,
                                           financial, current_cell, requested_metrics,
                                           selected_report_ids=(), company=None):
    """Bound four H126 CURRENT flow facts to readable FY23/FY24 correction scope.

    NO_EFFECT means this captured correction group does not replace the selected
    H126 CURRENT cell. It does not claim the acquisitions had no amount effect.
    """
    exact_metrics = {"net_profit_attributable", "net_profit_consolidated",
                     "operating_cashflow", "revenue"}
    expected_causes = {
        "1222991641": ("2025-04-03",
            "关于湖北宜化化工股份有限公司2024年度追溯调整前期财务报表数据的专项说明-信会师报字[2025]第ZE10074号",
            "sha256:9378ef0cd160da7ead1881ca1e4db5e50b00d5c45db6c0f773eab2a12406743a"),
        "1222991666": ("2025-04-03", "董事会关于2024年度追溯调整前期财务报表数据的说明",
            "sha256:18a7701e0d52d83f6c5d86eb4dab7c55c4be3af2283c30bf8a3ae1c9f135c207"),
        "1225183650": ("2026-04-25",
            "关于湖北宜化化工股份有限公司同一控制下企业合并追溯调整前期财务报表数据的专项说明 信会师报字[2026]第ZE10202号",
            "sha256:f46e817c54a818eae59a42579b4a0f5d8bef85707473d441c9a339e735168cb0"),
        "1225183652": ("2026-04-25", "关于同一控制下企业合并追溯调整前期财务报表数据的公告",
            "sha256:b090c18e1fae1a1335140676251959070a262d880d18cd10e1d15b639163fac1"),
    }
    if (code, period, target_id) != ("000422", "2026-06-30", "1225468007"):
        return None, "outside the exact 000422 H126 CURRENT report profile"
    if set(requested_metrics) != exact_metrics or metric not in exact_metrics:
        return None, "profile supports exactly four H126 CURRENT flow fields; parent_equity is excluded"
    if financial != set(expected_causes):
        return None, "complete captured 000422 financial-correction cause set differs"
    if target_id not in set(selected_report_ids):
        return None, "H126 half-year report is not the selected canonical CURRENT source"
    if not isinstance(company, Mapping):
        return None, "complete H126 issuer/index context is required"

    # Generic assessor receipt joins remain mandatory; additionally pin the
    # current all-category closure and the separately captured H126 regular index.
    index_pages = company.get("allCategoryPages", ())
    if (company.get("code") != code or company.get("instrumentId") != "SZ.000422"
            or company.get("organizationId") != "gssz0000422"
            or company.get("allCategoryTotalAnnouncement") != 573
            or company.get("allCategoryTermination") != "has_more_false"
            or company.get("publicationWindow") != ["2024-06-30", "2026-11-03"]
            or len(index_pages) != 20
            or [item.get("page") for item in index_pages] != list(range(1, 21))
            or sum(item.get("rawAnnouncementCount", 0) for item in index_pages) != 573
            or any(item.get("hasMore") is not True for item in index_pages[:-1])
            or index_pages[-1].get("hasMore") is not False):
        return None, "complete 573-entry/20-page Nov 3 current all-category index closure differs"
    regular = company.get("h126CurrentRegularIndexBinding")
    expected_regular = {
        "periodEnd": "2026-06-30", "queryThrough": "2026-11-03",
        "requestBodyHash": "sha256:9b21d7f9ad77111645c9ba36b36fda33095ff5cddebe4593a625200dc900039d",
        "requestReceiptDetail": "report-index:000422:2026-06-30:page=1:request=sha256:9b21d7f9ad77111645c9ba36b36fda33095ff5cddebe4593a625200dc900039d",
        "responseContentHash": "sha256:7badb0b62b0fc10e27eaa92502d6cbc978ec92b0003e2bca84d1b40c8170e6ba",
        "responseReceiptId": "rcpt_6771bcac3d5decdc2df9de06",
    }
    if regular != expected_regular:
        return None, "H126 regular current-period index capture/receipt binding differs"

    quotes = []
    def require_quote(aid, pattern, expected_page=None):
        item = documents.get(aid)
        quote = _quote(item.get("pages", ()) if item else (), pattern)
        if not quote or (expected_page is not None and quote["page"] != expected_page):
            return False
        quotes.append({"announcementId": aid, **quote})
        return True

    for aid, (published, title, digest) in expected_causes.items():
        item = documents.get(aid)
        if not item:
            return None, f"required official financial notice {aid} is absent"
        entry, metadata = item["entry"], item["metadata"]
        expected_url = f"https://static.cninfo.com.cn/finalpage/{published}/{aid}.PDF"
        if (entry.get("title") != title or entry.get("publishedDate") != published
                or entry.get("documentUrl") != expected_url
                or metadata.get("announcementId") != aid or metadata.get("code") != code
                or metadata.get("instrumentId") != "SZ.000422"
                or metadata.get("contentHash") != digest
                or metadata.get("documentUrl") != expected_url):
            return None, f"official financial notice {aid} title/date/URL/SHA/index identity differs"

    # CPA 1225183650 is an opaque related identity only. Its body is never
    # quoted or used as scope, metric, or amount evidence.
    opaque = documents.get("1225183650")
    if not opaque:
        return None, "opaque CPA related identity is absent from the complete cause set"
    opaque_binding = _financial_source_binding(opaque["metadata"], opaque["entry"],
        selected_canonical_report=False)
    opaque_identity = _index_bound_000422_opaque_cause_identity(
        opaque.get("pages", ()), opaque["entry"]["title"], ISSUERS["000422"], code,
        opaque_binding)
    if not opaque_identity:
        return None, "opaque CPA related-document exact index/source identity binding differs"
    opaque_related = {key: opaque_identity[key] for key in (
        "identityBasis", "announcementId", "pdfSha256", "pdfReceiptId", "indexReceiptId",
        "indexResponseHash", "indexSecCode", "indexOrgId", "publishedDate", "documentUrl",
        "nativeTextPresent", "usableNativeIssuerText")}

    # Readable original correction tables explicitly name FY2023 and FY2024.
    scope_checks = (
        ("1222991641", r"对\s*2023\s*年\s*12\s*月\s*31\s*日合并资产负债表项目的影响如下"),
        ("1222991641", r"对\s*2023\s*年度合并利润表的影响如下"),
        ("1222991641", r"对\s*2023\s*年度合并现金流量表的影响如下"),
        ("1222991666", r"对\s*2023\s*年\s*12\s*月\s*31\s*日合并资产负债表项目的影响如下"),
        ("1222991666", r"对\s*2023\s*年度合并利润表的影响如下"),
        ("1222991666", r"对\s*2023\s*年度合并现金流量表的影响如下"),
        ("1225183652", r"对合并资产负债表\s*2024\s*年\s*12\s*月\s*31\s*日余额追溯调整如下"),
        ("1225183652", r"对合并利润表\s*2024\s*年度金额追溯调整如下"),
        ("1225183652", r"对合并现金流量表\s*2024\s*年度金额追溯调整如下"),
        ("1225183652", r"合并日为\s*2025\s*年\s*5\s*月\s*30\s*日"),
        ("1225183652", r"编制\s*2025\s*年度合并财务报表时对公司前期财务数据进行\s*追溯调整"),
    )
    for aid, pattern in scope_checks:
        if not require_quote(aid, pattern):
            return None, f"readable issuer disclosure for explicit FY2023/FY2024 correction scope missing in {aid}"

    annual = documents.get(target_id)
    if not annual:
        return None, "selected H126 annual original is absent"
    annual_meta, annual_entry = annual["metadata"], annual["entry"]
    expected_hash = "sha256:6963a7b5ca322c8fe53d59eafe383bb882689ad9e2a9b78aa669373f58de4e12"
    expected_url = "https://static.cninfo.com.cn/finalpage/2026-08-12/1225468007.PDF"
    if (annual_meta.get("periodEnd") != period or annual_meta.get("contentHash") != expected_hash
            or annual_entry.get("publishedDate") != "2026-08-12"
            or annual_entry.get("title") != "2026年半年度报告"
            or annual_entry.get("documentUrl") != expected_url
            or annual_meta.get("documentUrl") != expected_url):
        return None, "selected H126 original identity/date/SHA/period differs"
    if not current_cell:
        return None, "H126 selected CURRENT and 2025H1 PRIOR source cells are required"

    expected_cells = {
        "net_profit_attributable": ("319334194.62", "398880278.09", 59,
            "归属于母公司股东的净利润", r"归属于母公司股东的\s*净利润[\s\S]{0,80}319,334,194\.62\s+398,880,278\.09"),
        "net_profit_consolidated": ("575273085.20", "753547510.48", 59,
            "五、净利润", r"五、净利润[\s\S]{0,80}575,273,085\.20\s+753,547,510\.48"),
        "operating_cashflow": ("1557413240.48", "1287155858.35", 62,
            "经营活动产生的现金流量净额", r"经营活动产生的现金流量净额\s+1,557,413,240\.48\s+1,287,155,858\.35"),
        "revenue": ("13391915759.47", "12004585375.22", 58,
            "其中：营业收入", r"其中\s*[:：]\s*营业收入\s+13,391,915,759\.47\s+12,004,585,375\.22"),
    }
    current_amount, prior_amount, page, label, row_pattern = expected_cells[metric]
    compact = lambda value: re.sub(r"\s+", "", str(value or ""))
    if (current_cell.get("metric") != metric or current_cell.get("factPeriodEnd") != period
            or current_cell.get("columnRole") != "CURRENT"
            or current_cell.get("statementScope") != "CONSOLIDATED"
            or current_cell.get("statement") != STATEMENT[metric]
            or current_cell.get("currentHeader") != "2026年半年度"
            or current_cell.get("priorHeader") != "2025年半年度"
            or current_cell.get("priorFactPeriodEnd") != "2025-06-30"
            or current_cell.get("unitRaw") != "元"
            or current_cell.get("amountYuan") != current_amount
            or current_cell.get("priorAmountYuan") != prior_amount
            or current_cell.get("page") != page
            or label not in compact(current_cell.get("rowLabel"))
            or not re.search(row_pattern, current_cell.get("sourceLine", ""))):
        return None, f"H126 CURRENT/2025H1 PRIOR consolidated cell, period, role, row, or amount differs for {metric}"
    try:
        current_raw = Decimal(re.sub(r"[，,\s]", "", current_cell["currentCellRaw"])) * SCALE[current_cell["unitRaw"]]
        prior_raw = Decimal(re.sub(r"[，,\s]", "", current_cell["priorCellRaw"])) * SCALE[current_cell["unitRaw"]]
    except (KeyError, ArithmeticError, TypeError, ValueError):
        return None, f"H126 CURRENT/2025H1 PRIOR raw cells are not parseable for {metric}"
    if current_raw != Decimal(current_amount) or prior_raw != Decimal(prior_amount):
        return None, f"H126 CURRENT/2025H1 PRIOR raw cell arithmetic differs for {metric}"

    fy24_rows = {
        "net_profit_attributable": r"归属于母公司股东的[\s\S]{0,80}652,544,554\.42\s+1,067,484,892\.73\s+414,940,338\.31",
        "net_profit_consolidated": r"五、净利润[\s\S]{0,80}893,723,240\.87\s+2,143,573,106\.10\s+1,249,849,865\.23",
        "operating_cashflow": r"经营活动产生的现金流量净额\s+768,224,617\.68\s+4,415,405,597\.01\s+3,647,180,979\.33",
        "revenue": r"其中\s*[:：]\s*营业收入\s+16,963,593,887\.39\s+25,394,172,300\.46\s+8,430,578,413\.07",
    }
    if not require_quote("1225183652", fy24_rows[metric]):
        return None, f"readable FY2024 adjusted-before/after/delta row missing for {metric}"

    # The H126 report itself explicitly says prior-year data are not restated.
    if not require_quote(target_id,
            r"公司是否需追溯调整或重述以前年度会计数据\s*□\s*是\s*[\uf052☑]\s*否", 9):
        return None, "H126 p9 does not positively record the prior-year restatement answer as NO"
    if not require_quote(target_id, r"项目\s+2026年半年度\s+2025年半年度", 58):
        return None, "H126 consolidated income statement header/periods missing"
    if not require_quote(target_id,
            r"五、净利润[\s\S]{0,100}575,273,085\.20\s+753,547,510\.48", 59):
        return None, "H126 consolidated net-profit row missing"
    if not require_quote(target_id,
            r"经营活动产生的现金流量净额\s+1,557,413,240\.48\s+1,287,155,858\.35", 62):
        return None, "H126 consolidated cash-flow row missing"
    if not require_quote(target_id,
            r"其中\s*[:：]\s*营业收入\s+13,391,915,759\.47\s+12,004,585,375\.22", 58):
        return None, "H126 consolidated revenue row missing"
    if not require_quote(target_id,
            r"归属于母公司股东的\s*净利润[\s\S]{0,80}319,334,194\.62\s+398,880,278\.09", 59):
        return None, "H126 attributable-profit row missing"

    quotes.append({"announcementId": target_id, "page": page,
        "quote": current_cell["sourceLine"], "columnRole": "CURRENT",
        "currentHeader": current_cell["currentHeader"], "priorHeader": current_cell["priorHeader"],
        "priorFactPeriodEnd": current_cell["priorFactPeriodEnd"],
        "currentAmountYuan": current_amount, "priorAmountYuan": prior_amount,
        "unitRaw": current_cell["unitRaw"], "statementScope": current_cell["statementScope"],
        "statement": current_cell["statement"]})
    return {"basis": "FINITE_000422_H126_CURRENT_FLOW_WITH_NO_PRIOR_YEAR_RESTATEMENT_AND_COMPLETE_FY23_FY24_SCOPE",
        "sourceQuotes": quotes,
        "scope": {"targetFactPeriodEnd": period, "targetMetric": metric,
            "targetColumnRole": "CURRENT", "currentAnnouncementId": target_id,
            "currentPublishedDate": "2026-08-12", "currentPdfSha256": annual_meta["contentHash"],
            "currentPage": page, "currentRowLabel": current_cell["rowLabel"],
            "currentUnit": current_cell["unitRaw"], "currentStatementScope": current_cell["statementScope"],
            "currentStatement": current_cell["statement"], "currentAmountYuan": current_amount,
            "priorFactPeriodEnd": "2025-06-30", "priorHeader": "2025年半年度",
            "priorAmountYuan": prior_amount,
            "capturedFinancialCorrectionIds": sorted(financial),
            "fy23CorrectionPeriodEnd": "2023-12-31",
            "fy24CorrectionPeriodEnd": "2024-12-31",
            "fy24RestatementAnnouncementId": "1225183652",
            "fy24RestatementRowBeforeAfterDeltaYuan": {
                "net_profit_attributable": ["652544554.42", "1067484892.73", "414940338.31"],
                "net_profit_consolidated": ["893723240.87", "2143573106.10", "1249849865.23"],
                "operating_cashflow": ["768224617.68", "4415405597.01", "3647180979.33"],
                "revenue": ["16963593887.39", "25394172300.46", "8430578413.07"],
            }[metric],
            "completeAllCategoryIndexClosure": {"entryCount": 573, "pageCount": 20,
                "publicationWindow": ["2024-06-30", "2026-11-03"], "termination": "has_more_false"},
            "h126RegularIndexBinding": regular,
            "opaqueRelatedDocumentIdentity": opaque_related,
            "opaqueDocumentBodyUsedForScopeOrAmountEvidence": False,
            "scopeExcludesParentEquity": True,
            "meaning": ("This finite NO_EFFECT applies only to this selected H126 2026-H1 CURRENT flow cell: "
                "the complete captured notices document FY2023/FY2024 corrections, and the H126 report explicitly "
                "marks NO for retrospective adjustment/restatement of previous-year accounting data. It means "
                "those notices do not replace this selected H126 CURRENT cell. It does not mean same-control "
                "combinations or historical corrections had no amount/business effect, does not extend to "
                "parent_equity, and does not establish formal admission.")}}, None


def _finite_h126_policy(code, metric, period, target_id, documents, financial):
    """Two captured CURRENT versions, not a claim of zero business impact.

    Existing comparisons and relationship closure still run at the caller.
    The 000422 cause with unreadable native text has no supported profile.
    """
    active = {"000899": "1225490300", "600338": "1225479925"}
    if (period != "2026-06-30" or metric not in FIELDS or
            active.get(code) != target_id):
        return None, "finite H126 current report identity is unsupported"
    def text(aid):
        return documents.get(aid, {}).get("pages", ())
    quotes = []
    def required(aid, pattern):
        quote = _quote(text(aid), pattern)
        if quote:
            quotes.append({"announcementId": aid, **quote})
        return quote is not None
    if code == "000899":
        if financial != {"1223101826", "1225490303"}:
            return None, "financial relationship set differs from both captured merger notices"
        checks = [
            required("1223101826", r"拟对\s*2023\s*年度相关财务数据进行追溯调整"),
            required("1225490303", r"东电公司纳入公司合并报表\s*范围.{0,160}拟对\s*2026\s*年度财务报表期初\s*数据及\s*2025\s*年半年度财务报表数据进行追溯调整"),
            required("1225490303", r"（一）对\s*2026\s*年度合并资产负债表期初数的影响"),
            required("1225490303", r"（二）对\s*2025\s*年半年度合并利润表的影响"),
            required("1225490303", r"（三）对\s*2025\s*年半年度合并现金流量表的影响"),
            required(target_id, r"追溯调整或重述原因\s*同一控制下企业合并\s*本报告期\s*上年同期"),
            required(target_id, r"本期发生同一控制下企业合并的，被合并方在合并前实现的净利润为：\s*10,255,443\.11\s*元，上期被合并方实现的净利润\s*为：\s*-12,893,441\.07\s*元"),
            required(target_id, r"对于同一控制下企业合并取得的子公司.{0,70}从合并当期的\s*期初起将其资产、负债、经营成果和现金流量纳入合并财务报表"),
        ]
        scope = {"historicalFlowPeriodEnd": "2023-12-31", "adjustedOpeningPeriodEnd": "2025-12-31",
                 "adjustedComparativePeriodEnd": "2025-06-30",
                 "currentReportIncludesCombination": True}
    else:
        if financial != {"1220732174", "1220732167", "1220732169", "1220732175", "1220732166"}:
            return None, "complete captured five-document correction group differs"
        checks = [
            required("1220732174", r"本次更正事项对公司\s*2023\s*年度财务报告的合并资产负债表.{0,85}具体如下"),
            required("1220732167", r"对公司\s*2023\s*年年度报告的合并财务\s*报表及相关附注进行更正"),
            required("1220732174", r"根据上述更正，公司已同步调整\s*2023\s*年年度报告全文第二节、第三节、第\s*十节以及摘要第二节中的对应内容或表述。.{0,80}更正后\s*2023\s*年年度报告全文与摘要"),
            required("1220732169", r"中审亚太审字\(2024\)007377\s*号.{0,180}《前期会计差错更正专项说明》"),
            required("1220732175", r"公司代码\s*[:：]\s*600338.{0,40}西藏珠峰资源股份有限公司\s*2023\s*年年度报告"),
            required("1220732166", r"公司代码\s*[:：]\s*600338.{0,40}西藏珠峰资源股份有限公司\s*2023\s*年年度报告摘要"),
            required(target_id, r"合并资产负债表\s*2026\s*年\s*6\s*月\s*30\s*日\s*编制单位：\s*西藏珠峰资源股份有限公司"),
            required(target_id, r"合并利润表\s*2026\s*年\s*1[—－-]6\s*月"),
            required(target_id, r"合并现金流量表\s*2026\s*年\s*1[—－-]6\s*月"),
        ]
        scope = {"correctedReportPeriodEnd": "2023-12-31"}
    if not all(checks):
        return None, "complete literal historical/comparative scope and active CURRENT relation missing"
    return {"basis": "FINITE_H126_ACTIVE_CURRENT_VERSION_AND_EXPLICIT_OTHER_FACT_SCOPE",
            "sourceQuotes": quotes, "scope": {**scope, "targetMetric": metric,
                "currentAnnouncementId": target_id, "currentFactPeriodEnd": period,
                "meaning": "No captured relationship replaces this selected CURRENT fact; historical adjustments and business effects remain."}}, None


def _finite_000422_fy25_four_current_policy(code, metric, period, target_id, documents,
                                             financial, current_cell, requested_metrics,
                                             selected_report_ids=()):
    """Bind FY25 CURRENT to annual cells and explicit FY23/FY24 correction scopes."""
    exact_metrics = {"net_profit_attributable", "net_profit_consolidated",
                     "operating_cashflow", "revenue"}
    expected_causes = {
        "1222991641": ("2025-04-03",
            "关于湖北宜化化工股份有限公司2024年度追溯调整前期财务报表数据的专项说明-信会师报字[2025]第ZE10074号",
            "sha256:9378ef0cd160da7ead1881ca1e4db5e50b00d5c45db6c0f773eab2a12406743a"),
        "1222991666": ("2025-04-03", "董事会关于2024年度追溯调整前期财务报表数据的说明",
            "sha256:18a7701e0d52d83f6c5d86eb4dab7c55c4be3af2283c30bf8a3ae1c9f135c207"),
        "1225183650": ("2026-04-25",
            "关于湖北宜化化工股份有限公司同一控制下企业合并追溯调整前期财务报表数据的专项说明 信会师报字[2026]第ZE10202号",
            "sha256:f46e817c54a818eae59a42579b4a0f5d8bef85707473d441c9a339e735168cb0"),
        "1225183652": ("2026-04-25", "关于同一控制下企业合并追溯调整前期财务报表数据的公告",
            "sha256:b090c18e1fae1a1335140676251959070a262d880d18cd10e1d15b639163fac1"),
    }
    if (code, period, target_id) != ("000422", "2025-12-31", "1225183616"):
        return None, "outside the exact 000422 FY25 annual CURRENT report profile"
    if set(requested_metrics) != exact_metrics or metric not in exact_metrics:
        return None, "profile requires exactly the four FY25 income/cashflow fields"
    if financial != set(expected_causes):
        return None, "complete captured 000422 financial-correction cause set differs"
    if target_id not in set(selected_report_ids):
        return None, "FY25 annual report is not the selected canonical CURRENT source"

    quotes = []
    def require_quote(aid, pattern, expected_page=None):
        item = documents.get(aid)
        quote = _quote(item.get("pages", ()) if item else (), pattern)
        if not quote or (expected_page is not None and quote["page"] != expected_page):
            return False
        quotes.append({"announcementId": aid, **quote})
        return True

    for aid, (published, title, digest) in expected_causes.items():
        item = documents.get(aid)
        if not item:
            return None, f"required official financial notice {aid} is absent"
        entry, metadata = item["entry"], item["metadata"]
        expected_url = f"https://static.cninfo.com.cn/finalpage/{published}/{aid}.PDF"
        if (entry.get("title") != title or entry.get("publishedDate") != published
                or entry.get("documentUrl") != expected_url
                or metadata.get("announcementId") != aid or metadata.get("code") != code
                or metadata.get("instrumentId") != "SZ.000422"
                or metadata.get("contentHash") != digest
                or metadata.get("documentUrl") != expected_url):
            return None, f"official financial notice {aid} title/date/URL/SHA/index identity differs"

    opaque = documents.get("1225183650")
    if not opaque:
        return None, "opaque CPA related document 1225183650 is absent from the complete captured cause set"
    opaque_binding = _financial_source_binding(opaque["metadata"], opaque["entry"],
        selected_canonical_report=False)
    opaque_identity = _index_bound_000422_opaque_cause_identity(
        opaque.get("pages", ()), opaque["entry"]["title"], ISSUERS["000422"], code,
        opaque_binding)
    if not opaque_identity:
        return None, "opaque CPA related-document exact index/source identity binding differs"
    # Retain exact related identity only; do not persist the old visual-audit pointer.
    opaque_related = {key: opaque_identity[key] for key in (
        "identityBasis", "announcementId", "pdfSha256", "pdfReceiptId", "indexReceiptId",
        "indexResponseHash", "indexSecCode", "indexOrgId", "publishedDate", "documentUrl",
        "nativeTextPresent", "usableNativeIssuerText")}

    scope_checks = (
        ("1222991641", r"对\s*2023\s*年\s*12\s*月\s*31\s*日合并资产负债表项目的影响如下"),
        ("1222991641", r"对\s*2023\s*年度合并利润表的影响如下"),
        ("1222991641", r"对\s*2023\s*年度合并现金流量表的影响如下"),
        ("1222991666", r"对\s*2023\s*年\s*12\s*月\s*31\s*日合并资产负债表项目的影响如下"),
        ("1222991666", r"对\s*2023\s*年度合并利润表的影响如下"),
        ("1222991666", r"对\s*2023\s*年度合并现金流量表的影响如下"),
        ("1225183652", r"对合并资产负债表\s*2024\s*年\s*12\s*月\s*31\s*日余额追溯调整如下"),
        ("1225183652", r"对合并利润表\s*2024\s*年度金额追溯调整如下"),
        ("1225183652", r"对合并现金流量表\s*2024\s*年度金额追溯调整如下"),
        ("1225183652", r"合并日为\s*2025\s*年\s*5\s*月\s*30\s*日"),
        ("1225183652", r"编制\s*2025\s*年度合并财务报表时对公司前期财务数据进行\s*追溯调整"),
    )
    for aid, pattern in scope_checks:
        if not require_quote(aid, pattern):
            return None, f"readable issuer disclosure for explicit FY2023/FY2024 correction scope missing in {aid}"

    annual = documents.get(target_id)
    if not annual:
        return None, "selected FY25 annual original is absent"
    annual_meta, annual_entry = annual["metadata"], annual["entry"]
    if (annual_meta.get("periodEnd") != period
            or annual_meta.get("contentHash") != "sha256:13067dd7ca378659a5d01a6600c911dda9335afc970ac10f554dfd79a945987f"
            or annual_entry.get("publishedDate") != "2026-04-25"
            or annual_entry.get("title") != "2025年年度报告"):
        return None, "selected FY25 annual identity/date/SHA/period differs"
    if not current_cell:
        return None, "FY25 annual CURRENT and FY24 PRIOR source cells are required"

    expected_cells = {
        "net_profit_attributable": ("808587873.83", "1067484892.73", 117,
            "归属于母公司股东的净利润", r"归属于母公司股东的净利润"),
        "net_profit_consolidated": ("1444539489.18", "2143573106.10", 117,
            "净利润", r"五、净利润"),
        "operating_cashflow": ("3606060151.55", "4415405597.01", 120,
            "经营活动产生的现金流量净额", r"经营活动产生的现金流量净额"),
        "revenue": ("25652882508.52", "25394172300.46", 116,
            "其中：营业收入", r"其中\s*[:：]\s*营业收入"),
    }
    current_amount, prior_amount, page, label, row_pattern = expected_cells[metric]
    compact = lambda value: re.sub(r"\s+", "", str(value or ""))
    if (current_cell.get("metric") != metric or current_cell.get("factPeriodEnd") != period
            or current_cell.get("columnRole") != "CURRENT"
            or current_cell.get("statementScope") != "CONSOLIDATED"
            or current_cell.get("statement") != STATEMENT[metric]
            or current_cell.get("currentHeader") != "2025年度"
            or current_cell.get("priorHeader") != "2024年度"
            or current_cell.get("priorFactPeriodEnd") != "2024-12-31"
            or current_cell.get("unitRaw") != "元"
            or current_cell.get("amountYuan") != current_amount
            or current_cell.get("priorAmountYuan") != prior_amount
            or current_cell.get("page") != page
            or label not in compact(current_cell.get("rowLabel"))
            or not re.search(row_pattern, current_cell.get("sourceLine", ""))):
        return None, f"FY25 CURRENT/FY24 PRIOR consolidated cell, period, role, row, or amount differs for {metric}"
    try:
        current_raw = Decimal(re.sub(r"[，,\s]", "", current_cell["currentCellRaw"])) * SCALE[current_cell["unitRaw"]]
        prior_raw = Decimal(re.sub(r"[，,\s]", "", current_cell["priorCellRaw"])) * SCALE[current_cell["unitRaw"]]
    except (KeyError, ArithmeticError, TypeError, ValueError):
        return None, f"FY25 CURRENT/FY24 PRIOR raw cells are not parseable for {metric}"
    if current_raw != Decimal(current_amount) or prior_raw != Decimal(prior_amount):
        return None, f"FY25 CURRENT/FY24 PRIOR raw cell arithmetic differs for {metric}"

    prior_rows = {
        "net_profit_attributable": r"归属于母公司股东的[\s\S]{0,80}652,544,554\.42\s+1,067,484,892\.73\s+414,940,338\.31",
        "net_profit_consolidated": r"五、净利润[\s\S]{0,80}893,723,240\.87\s+2,143,573,106\.10\s+1,249,849,865\.23",
        "operating_cashflow": r"经营活动产生的现金流量净额\s+768,224,617\.68\s+4,415,405,597\.01\s+3,647,180,979\.33",
        "revenue": r"其中\s*[:：]\s*营业收入\s+16,963,593,887\.39\s+25,394,172,300\.46\s+8,430,578,413\.07",
    }
    if not require_quote("1225183652", prior_rows[metric]):
        return None, f"readable FY2024 adjusted-after/before/delta row missing for {metric}"
    if (not require_quote(target_id, r"追溯调整或重述原因\s*同一控制下企业合并", 8)
            or not require_quote(target_id,
                r"本期发生同一控制下企业合并的，被合并方在合并前实现的净利润为[：:]?\s*538,176,288\.80\s*元", 118)
            or not require_quote(target_id,
                r"宜昌新发\s*产业投资\s*有限公司[\s\S]{0,500}2025\s*年\s*05\s*月\s*30\s*日[\s\S]{0,700}3,054,144\s*,?\s*144\.89[\s\S]{0,200}538,176,2\s*88\.80", 220)):
        return None, "FY25 annual source does not positively bind the current-year common-control consolidation scope"

    quotes.append({"announcementId": target_id, "page": page,
        "quote": current_cell["sourceLine"], "columnRole": "CURRENT",
        "currentHeader": current_cell["currentHeader"], "priorHeader": current_cell["priorHeader"],
        "priorFactPeriodEnd": current_cell["priorFactPeriodEnd"],
        "currentAmountYuan": current_amount, "priorAmountYuan": prior_amount})
    return {"basis": "EXPLICIT_FY23_FY24_CORRECTION_SCOPE_WITH_FY25_CURRENT_CONSOLIDATION",
        "sourceQuotes": quotes,
        "scope": {"targetFactPeriodEnd": period, "targetMetric": metric,
            "targetColumnRole": "CURRENT", "currentAnnouncementId": target_id,
            "currentPublishedDate": "2026-04-25", "currentPdfSha256": annual_meta["contentHash"],
            "currentPage": page, "currentRowLabel": current_cell["rowLabel"],
            "currentUnit": current_cell["unitRaw"], "currentAmountYuan": current_amount,
            "applicablePriorFactPeriodEnd": "2024-12-31", "applicablePriorHeader": "2024年度",
            "applicablePriorAmountYuan": prior_amount,
            "fy24RestatementAnnouncementId": "1225183652",
            "fy24RestatementRowPriorAfterDeltaYuan": {
                "net_profit_attributable": ["652544554.42", "1067484892.73", "414940338.31"],
                "net_profit_consolidated": ["893723240.87", "2143573106.10", "1249849865.23"],
                "operating_cashflow": ["768224617.68", "4415405597.01", "3647180979.33"],
                "revenue": ["16963593887.39", "25394172300.46", "8430578413.07"],
            }[metric],
            "opaqueRelatedDocumentIdentity": opaque_related,
            "opaqueDocumentBodyUsedForScopeOrAmountEvidence": False,
            "meaning": ("Only this selected FY25 annual CURRENT fact is outside the explicit FY2023 and FY2024 prior-period correction tables. The readable board notice's FY2024 adjusted-after value matches this annual report's FY24 PRIOR cell; the annual report separately binds FY25 CURRENT to its consolidated 2025 columns and discloses the same-control acquisition's pre-merger current-year results. NO_EFFECT means these captured notices do not replace this FY25 CURRENT cell. It does not mean the same-control acquisition had no FY25 amount effect. This does not infer consolidated profit from attributable profit, cover other periods/metrics, or establish admission.")}}, None


def _finite_later_period_policy(code, metric, period, target_id, documents, financial,
                                current_cell=None):
    """Narrow later-period scope evidence; a missing same-fact comparator stays unresolved."""
    profiles = {
        ("000899", "2025-12-31", "1225121246"): {
            "metrics": {"net_profit_attributable", "net_profit_consolidated",
                        "operating_cashflow", "revenue"},
            "financial": {"1223101826", "1225490303"},
        },
        ("600338", "2025-06-30", "1224568683"): {
            "metrics": set(FIELDS),
            "financial": {"1220732174", "1220732167", "1220732169",
                          "1220732175", "1220732166"},
        },
        ("600338", "2024-12-31", "1223383463"): {
            "metrics": {"revenue"},
            "financial": {"1220732174", "1220732167", "1220732169",
                          "1220732175", "1220732166"},
        },
        ("600338", "2025-12-31", "1225153721"): {
            "metrics": {"net_profit_attributable", "net_profit_consolidated",
                        "operating_cashflow", "revenue"},
            "financial": {"1220732174", "1220732167", "1220732169",
                          "1220732175", "1220732166"},
        },
    }
    profile = profiles.get((code, period, target_id))
    if not profile or metric not in profile["metrics"]:
        return None, "later-period finite profile does not include this exact report/field"
    if financial != profile["financial"]:
        return None, "complete captured financial-correction relationship set differs"

    def text(aid):
        return documents.get(aid, {}).get("pages", ())

    quotes = []
    def required(aid, pattern):
        quote = _quote(text(aid), pattern)
        if quote:
            quotes.append({"announcementId": aid, **quote})
        return quote is not None

    if code == "000899":
        if (not current_cell or current_cell.get("metric") != metric or
                current_cell.get("factPeriodEnd") != period or
                current_cell.get("columnRole") != "CURRENT" or
                current_cell.get("statementScope") != "CONSOLIDATED" or
                current_cell.get("statement") != STATEMENT[metric] or
                current_cell.get("currentHeader") != "2025年度" or
                current_cell.get("priorHeader") != "2024年度" or
                current_cell.get("priorFactPeriodEnd") != "2024-12-31" or
                current_cell.get("unitRaw") != "元" or
                current_cell.get("priorAmountYuan") is None or
                not current_cell.get("sourceLine")):
            return None, "selected FY25 annual CURRENT and its 2024 annual PRIOR cell are not bound"
        current = documents.get(target_id)
        fy23 = documents.get("1223101826")
        h1 = documents.get("1225490303")
        publication_dates = {
            "1223101826": fy23.get("entry", {}).get("publishedDate") if fy23 else None,
            target_id: current.get("entry", {}).get("publishedDate") if current else None,
            "1225490303": h1.get("entry", {}).get("publishedDate") if h1 else None,
        }
        if publication_dates != {
                "1223101826": "2025-04-16", target_id: "2026-04-18",
                "1225490303": "2026-08-22"}:
            return None, "captured FY23/FY25/H1 correction chronology differs from the bounded profile"
        quotes.append({"announcementId": target_id, "page": current_cell.get("page"),
                       "quote": current_cell["sourceLine"]})
        h1_flow_rows = {
            "revenue": r"营业收入\s+3,030,507,313\.49\s+3,040,821,229\.13\s+10,313,915\.64",
            "net_profit_attributable": r"归属于母公司股东的\s*净利润\s+438,257,226\.39\s+425,363,785\.32\s+-12,893,441\.07",
            "net_profit_consolidated": r"净利润（净亏损以.{0,6}号填列）\s+428,983,810\.64\s+416,090,369\.57\s+-12,893,441\.07",
            "operating_cashflow": r"经营活动产生的现金流\s*量\s*净额\s+1,174,697,131\.92\s+1,172,977,139\.83\s+-1,719,992\.09",
        }
        checks = [
            required("1223101826", r"拟对\s*2023\s*年度相关财务数据进行追溯调整"),
            required("1223101826", r"合并资产负债表\s+单位\s*[:：]\s*元\s+项目\s+2023\s*年\s*12\s*月\s*31\s*日\s*余额"),
            required("1223101826", r"合并利润表\s+单位\s*[:：]\s*元\s+项目\s+2023\s*年度\s*（追溯调整前）\s+2023\s*年度\s*（追溯调整后）"),
            required("1223101826", r"合并现金流量表\s+单位\s*[:：]\s*元\s+项目\s+2023\s*年度\s*（追溯调整前）\s+2023\s*年度\s*（追溯调整后）"),
            required("1225490303", r"拟对\s*2026\s*年度财务报表期初\s*数据及\s*2025\s*年半年度财务报表数据进行追溯\s*调整"),
            required("1225490303", r"拟对本年期初数据及\s*2025\s*年\s*半年度财务报表数据进行追溯\s*调整"),
            required("1225490303", r"（一）对\s*2026\s*年度合并资产负债表期初数的影响"),
            required("1225490303", r"（二）对\s*2025\s*年半年度合并利润表的影响\s+单位\s*[:：]\s*元\s+项目\s+2025\s*年\s*1\s*[-－—]\s*6\s*月\s*（追溯调整前）\s+2025\s*年\s*1\s*[-－—]\s*6\s*月\s*（追溯调整后）"),
            required("1225490303", r"（三）对\s*2025\s*年半年度合并现金流量表的影响\s+单位\s*[:：]\s*元\s+项目\s+2025\s*年\s*1\s*[-－—]\s*6\s*月\s*（追溯调整前）\s+2025\s*年\s*1\s*[-－—]\s*6\s*月\s*（追溯调整后）"),
            required("1225490303", h1_flow_rows[metric]),
        ]
        if not all(checks):
            return None, "full original FY23/H1-2025/2026-opening scope and requested H1 row are not present"
        return {"basis": "FINITE_FY25_CURRENT_NOT_REPLACED_BY_EXPLICIT_OTHER_PERIOD_CORRECTIONS",
                "sourceQuotes": quotes,
                "scope": {"capturedFinancialCorrectionIds": sorted(financial),
                    "historicalCorrectionPeriodEnd": "2023-12-31",
                    "laterCorrection": {"announcementId": "1225490303",
                        "publicationDate": publication_dates["1225490303"],
                        "adjustedOpeningBalancePeriodEnd": "2026-01-01",
                        "adjustedFlowPeriodEnd": "2025-06-30",
                        "targetMetricH1ValuesBeforeAfterAndDeltaYuan": {
                            "revenue": ["3030507313.49", "3040821229.13", "10313915.64"],
                            "net_profit_attributable": ["438257226.39", "425363785.32", "-12893441.07"],
                            "net_profit_consolidated": ["428983810.64", "416090369.57", "-12893441.07"],
                            "operating_cashflow": ["1174697131.92", "1172977139.83", "-1719992.09"],
                        }[metric]},
                    "currentAnnouncementId": target_id,
                    "currentPublishedDate": publication_dates[target_id],
                    "currentFactPeriodEnd": period,
                    "applicablePriorFactPeriodEnd": current_cell["priorFactPeriodEnd"],
                    "applicablePriorHeader": current_cell["priorHeader"],
                    "applicablePriorAmountYuan": current_cell["priorAmountYuan"],
                    "targetMetric": metric,
                    "meaning": "The complete captured correction set states a FY2023 correction and a FY2025H1-flow/2026-opening correction. The latter records this metric's actual H1 before/after values but does not provide a replacement value for the selected FY2025 annual CURRENT cell. This finite scope does not assert no economic effect or that H1 is unchanged."}}, None

    if code == "600338" and period == "2024-12-31":
        if (not current_cell or current_cell.get("metric") != metric or
                current_cell.get("factPeriodEnd") != period or
                current_cell.get("columnRole") != "CURRENT" or
                current_cell.get("statementScope") != "CONSOLIDATED" or
                current_cell.get("statement") != STATEMENT[metric] or
                current_cell.get("currentHeader") != "2024年度" or
                current_cell.get("priorHeader") != "2023年度" or
                current_cell.get("priorFactPeriodEnd") != "2023-12-31" or
                current_cell.get("unitRaw") != "元" or
                current_cell.get("priorAmountYuan") is None or
                not current_cell.get("sourceLine")):
            return None, "selected FY24 annual CURRENT and its FY23 PRIOR cell are not bound"
        current = documents.get(target_id)
        if (not current or current.get("metadata", {}).get("periodEnd") != period or
                current.get("entry", {}).get("title") != "2024年年度报告" or
                current.get("entry", {}).get("publishedDate") != "2025-04-29"):
            return None, "selected FY24 annual report identity or publication date differs"
    # The full five-document 2023 group is required. The correction tables are
    # tied to their stated FY2023 period and all three consolidated statements;
    # a missing keyword in one table is never treated as an exclusion.
    income_amount = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d+"
    income_rows = "".join(
        re.escape(label) + r"\s+" + (income_amount + r"\s+") * 3
        for label in ("管理费用", "营业利润", "利润总额", "所得税费用", "净利润")
    )
    checks = [
        required("1220732174", r"本次会计差错更正事项对公司\s*2023\s*年度财务报告的合并资产负债表.{0,180}?合\s*并利润表和合并现金流量表"),
        required("1220732174", r"（一）对\s*2023\s*年度合并资产负债表项目的影响金额\s*[:：]?\s*单位\s*[:：]元\s*报表项目\s*更正前金额\s*更正金额\s*更正后金额.{0,700}?未分配利润.{0,160}?负债与所有者权益总计"),
        required("1220732174", r"（二）对\s*2023\s*年度合并利润表报表项目的影响金额\s*[:：]?\s*单位\s*[:：]元\s*报表项目\s*更正前金额\s*更正金额\s*更正后金额\s*" + income_rows + r"（三）对\s*2023\s*年度合并现金流量表项目的影响金额"),
        required("1220732174", r"报表项目\s*更正前金额\s*更正金额\s*更正后金额.{0,900}?经营活动产生的现金流量净额.{0,250}?分配股利、利润或偿付利息支付\s*的现金"),
        required("1220732167", r"对公司\s*2023\s*年年度报告的合并财务\s*报表及相关附注进行更正.{0,180}?黑色加粗字体数字为本次差\s*错更正受影响数据"),
        required("1220732169", r"中审亚太审字\(2024\)007377\s*号.{0,180}《前期会计差错更正专项说明》"),
        required("1220732175", r"公司代码\s*[:：]\s*600338.{0,40}西藏珠峰资源股份有限公司\s*2023\s*年年度报告"),
        required("1220732166", r"公司代码\s*[:：]\s*600338.{0,40}西藏珠峰资源股份有限公司\s*2023\s*年年度报告摘要"),
    ]
    if not all(checks):
        return None, "complete original five-document FY2023 correction scope is not present"
    current = documents.get(target_id)
    current_published = current.get("entry", {}).get("publishedDate") if current else None
    try:
        current_day = date.fromisoformat(current_published)
    except (TypeError, ValueError):
        return None, "selected CURRENT report has no valid index publication date"
    correction_publications = []
    for aid in sorted(profile["financial"]):
        correction = documents.get(aid)
        published = correction.get("entry", {}).get("publishedDate") if correction else None
        try:
            correction_day = date.fromisoformat(published)
        except (TypeError, ValueError):
            return None, f"correction {aid} has no valid index publication date"
        if correction_day >= current_day:
            return None, f"correction {aid} was not published before the selected CURRENT report"
        correction_publications.append({"announcementId": aid, "publishedDate": published,
                                        "strictlyBeforeCurrent": True})
    return {"basis": "EXPLICIT_COMPLETE_FY2023_CORRECTION_SCOPE_POSTDISCLOSURE_CURRENT",
            "sourceQuotes": quotes,
            "scope": {"correctionReportPeriodEnd": "2023-12-31",
                      "correctedStatements": ["consolidated balance sheet",
                          "consolidated income statement", "consolidated cash-flow statement"],
                      "currentAnnouncementId": target_id,
                      "currentFactPeriodEnd": period,
                      "currentPublishedDate": current_published,
                      "correctionPublications": correction_publications,
                      "targetMetric": metric,
                      "meaning": "The complete captured correction group is explicitly scoped to FY2023 and every correction document predates this selected CURRENT report. "
                          "This finite post-disclosure scope may support the current fact when no applicable same-fact comparison exists; any observed or unparsed comparison remains governed by the ordinary path."}}, None


def _finite_000899_h124_current27_notice_scope(document):
    """Bind the exact 2026-10-24 000899 notice to its stated periods and row."""
    aid = "1225619745"
    expected_title = "关于同一控制下企业合并追溯调整财务数据的公告"
    expected_hash = "sha256:eb7fc614c808bd26667baad8f963c5baebddd3e1e48a9c74225f066927fee040"
    expected_url = "https://static.cninfo.com.cn/finalpage/2026-10-24/1225619745.PDF"
    metadata = document.get("metadata", {}) if isinstance(document, Mapping) else {}
    entry = document.get("entry", {}) if isinstance(document, Mapping) else {}
    if (not isinstance(metadata, Mapping) or not isinstance(entry, Mapping) or
            metadata.get("announcementId") != aid or entry.get("announcementId") != aid):
        return None, "the additional 000899 notice is not the exact indexed document"
    metadata_title = metadata.get("title")
    title = entry.get("title")
    if (not isinstance(metadata_title, str) or normalize_title(metadata_title) != expected_title or
            title != expected_title or not _financial_title(title)):
        return None, "the additional 000899 notice title or financial-correction role differs"
    if entry.get("publishedDate") != "2026-10-24":
        return None, "the additional 000899 notice publication date differs"
    receipt = metadata.get("receipt", {})
    if (metadata.get("code") != "000899" or metadata.get("instrumentId") != "SZ.000899" or
            metadata.get("documentUrl") != expected_url or metadata.get("contentHash") != expected_hash or
            metadata.get("byteSize") != 171848 or not isinstance(receipt, Mapping) or
            receipt.get("url") != expected_url or receipt.get("content_hash") != expected_hash or
            receipt.get("byte_size") != 171848):
        return None, "the additional 000899 notice identity, original hash, or byte size differs"

    pages = document.get("pages", ())
    if not isinstance(pages, (tuple, list)) or len(pages) != 11 or any(
            not isinstance(page, str) or not page.strip() for page in pages):
        return None, "all eleven native pages of the additional 000899 notice are required"

    quotes = []

    def required(page_number, pattern):
        match = re.search(pattern, pages[page_number - 1])
        if match:
            quotes.append({"announcementId": aid, "page": page_number,
                           "quote": match.group(0)})
        return match

    checks = [
        required(1, r"江西赣能股份有限公司\s+关于同一控制下企业合并追溯调整财务数据的公告"),
        required(1, r"证券代码\s*[:：]\s*000899\s+证券简称\s*[:：]\s*赣能股份\s+公告编号\s*[:：]\s*2026-53"),
        required(1, r"根据《企业会计准则》相关要求，对\s*2026\s*年度财务报表期初数\s*据及\s*2025\s*年\s*1\s*[-－—]\s*9\s*月财务报表数\s*据进行追溯调整"),
        required(2, r"根据上述规定，对\s*2026\s*年度财务报表期初数\s*据及\s*2025\s*年\s*1\s*[-－—]\s*9\s*月财务报表数\s*据进行追溯调整"),
        required(2, r"（一）对\s*2026\s*年度合并资产负债表期初数的影响\s+单位\s*[:：]\s*元\s+项目\s+2026\s*年期初数\s*（追溯调整前）\s+2026\s*年期初数\s*（追溯调整后）\s+影响数"),
        required(5, r"（二）对\s*2025\s*年\s*1\s*[-－—]\s*9\s*月合并利润表的影响\s+单位\s*[:：]\s*元\s+项目\s+2025\s*年\s*1\s*[-－—]\s*9\s*月\s*（追溯调整前）\s+2025\s*年\s*1\s*[-－—]\s*9\s*月\s*（追溯调整后）\s+影响数"),
    ]
    if not all(checks):
        return None, "native notice period scope or complete statement headings are missing"

    number = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}"
    revenue = required(6, r"其中\s*[:：]\s*营业收入\s+(?P<before>" + number +
                       r")\s+(?P<after>" + number + r")\s+(?P<delta>" + number + r")")
    if revenue is None:
        return None, "native 2025 Jan-Sep consolidated revenue before/after/delta row is missing"
    values = {key: revenue.group(key).replace(",", "") for key in ("before", "after", "delta")}
    expected_values = {"before": "5169205999.04", "after": "5188696419.80",
                       "delta": "19490420.76"}
    try:
        arithmetic_matches = Decimal(values["after"]) - Decimal(values["before"]) == Decimal(values["delta"])
    except (ArithmeticError, ValueError):
        arithmetic_matches = False
    if values != expected_values or not arithmetic_matches:
        return None, "native 2025 Jan-Sep consolidated revenue amounts differ from the captured adjustment"

    return {
        "announcementId": aid,
        "title": expected_title,
        "publicationDate": "2026-10-24",
        "sourceOriginalHash": expected_hash,
        "sourceOriginalBytes": 171848,
        "adjustedOpeningPeriodEnd": "2026-01-01",
        "adjustedFlowPeriodStart": "2025-01-01",
        "adjustedFlowPeriodEnd": "2025-09-30",
        "targetMetric": "revenue",
        "targetMetricFlowValuesBeforeAfterAndDeltaYuan": [
            values["before"], values["after"], values["delta"]],
        "sourceQuotes": quotes,
        "meaning": (
            "The additional notice explicitly scopes restatement to 2026 opening data and 2025 Jan-Sep "
            "statements, and reports the quoted revenue change for that flow period. This proof does not "
            "infer a 2024H1 non-effect from the absence of 2024 text; the H124 relation still depends on "
            "the unchanged H125 original PRIOR equality and observed same-fact comparisons."
        ),
    }, None


def _finite_000899_h124_period_scope_policy(code, metric, period, target_id, documents,
                                           financial, *, current_cell=None,
                                           report_periods=None, columns=None):
    """Bind the 2024H1 CURRENT to its 2025H1 original PRIOR and explicit later scopes."""
    if (code, period, target_id) != ("000899", "2024-06-30", "1220893776"):
        return None, "finite 000899 H124 profile does not include this exact report"
    if metric != "revenue":
        return None, "finite 000899 H124 profile only supports revenue"
    legacy_financial = {"1223101826", "1225490303"}
    current27_financial = legacy_financial | {"1225619745"}
    if financial not in (legacy_financial, current27_financial):
        return None, "complete captured financial correction set differs from both bounded notices"

    expected_publication = {
        "1220893776": "2024-08-19",
        "1223101826": "2025-04-16",
        "1224536426": "2025-08-22",
        "1225490303": "2026-08-22",
    }
    for aid, expected_day in expected_publication.items():
        document = documents.get(aid)
        if not document or document.get("entry", {}).get("publishedDate") != expected_day:
            return None, f"required original source or chronology is missing for {aid}"

    current = documents["1220893776"]
    successor = documents["1224536426"]
    if (current.get("metadata", {}).get("periodEnd") != period or
            successor.get("metadata", {}).get("periodEnd") != "2025-06-30" or
            not report_periods or report_periods.get("1224536426") != "2025-06-30"):
        return None, "H124 CURRENT and original H125 successor are not bound to their report periods"
    if (not current_cell or current_cell.get("metric") != "revenue" or
            current_cell.get("factPeriodEnd") != period or
            current_cell.get("columnRole") != "CURRENT" or
            current_cell.get("statementScope") != "CONSOLIDATED" or
            current_cell.get("statement") != STATEMENT["revenue"] or
            current_cell.get("unitRaw") != "元" or
            current_cell.get("amountYuan") is None or
            "2024" not in str(current_cell.get("currentHeader", "")) or
            not current_cell.get("sourceLine")):
        return None, "selected H124 consolidated revenue CURRENT cell is not bound"

    successor_cell = (columns or {}).get("1224536426", {}).get("revenue")
    if (not successor_cell or successor_cell.get("factPeriodEnd") != "2025-06-30" or
            successor_cell.get("columnRole") != "CURRENT" or
            successor_cell.get("statementScope") != "CONSOLIDATED" or
            successor_cell.get("statement") != STATEMENT["revenue"] or
            successor_cell.get("unitRaw") != "元" or
            "2025" not in str(successor_cell.get("currentHeader", "")) or
            successor_cell.get("priorFactPeriodEnd") != period or
            "2024" not in str(successor_cell.get("priorHeader", "")) or
            successor_cell.get("priorAmountYuan") is None or
            not successor_cell.get("sourceLine")):
        return None, "original H125 report does not provide a parsed 2024H1 revenue PRIOR cell"
    if Decimal(current_cell["amountYuan"]) != Decimal(successor_cell["priorAmountYuan"]):
        return None, "original H125 revenue PRIOR differs from the selected H124 CURRENT"

    def text(aid):
        return documents.get(aid, {}).get("pages", ())

    quotes = []

    def required(aid, pattern):
        quote = _quote(text(aid), pattern)
        if quote:
            quotes.append({"announcementId": aid, **quote})
        return quote is not None

    h125_revenue_rows = {
        "revenue": r"营业收入\s+3,030,507,313\.49\s+3,040,821,229\.13\s+10,313,915\.64",
    }
    checks = [
        required("1223101826", r"拟对\s*2023\s*年度相关财务数据进行追溯调整"),
        required("1223101826", r"合并资产负债表\s+单位\s*[:：]\s*元\s+项目\s+2023\s*年\s*12\s*月\s*31\s*日\s*余额"),
        required("1223101826", r"合并利润表\s+单位\s*[:：]\s*元\s+项目\s+2023\s*年度\s*（追溯调整前）\s+2023\s*年度\s*（追溯调整后）"),
        required("1223101826", r"合并现金流量表\s+单位\s*[:：]\s*元\s+项目\s+2023\s*年度\s*（追溯调整前）\s+2023\s*年度\s*（追溯调整后）"),
        required("1225490303", r"拟对\s*2026\s*年度财务报表期初\s*数据及\s*2025\s*年半年度财务报表数据进行追溯\s*调整"),
        required("1225490303", r"拟对本年期初数据及\s*2025\s*年\s*半年度财务报表数据进行追溯\s*调整"),
        required("1225490303", r"（一）对\s*2026\s*年度合并资产负债表期初数的影响"),
        required("1225490303", r"（二）对\s*2025\s*年半年度合并利润表的影响\s+单位\s*[:：]\s*元\s+项目\s+2025\s*年\s*1\s*[-－—]\s*6\s*月\s*（追溯调整前）\s+2025\s*年\s*1\s*[-－—]\s*6\s*月\s*（追溯调整后）"),
        required("1225490303", r"（三）对\s*2025\s*年半年度合并现金流量表的影响\s+单位\s*[:：]\s*元\s+项目\s+2025\s*年\s*1\s*[-－—]\s*6\s*月\s*（追溯调整前）\s+2025\s*年\s*1\s*[-－—]\s*6\s*月\s*（追溯调整后）"),
        required("1225490303", h125_revenue_rows[metric]),
    ]
    if not all(checks):
        return None, "complete FY2023 and H125/H126-opening correction scopes are not explicit in captured source text"

    quotes.extend([
        {"announcementId": "1220893776", "page": current_cell["page"],
         "quote": current_cell["sourceLine"]},
        {"announcementId": "1224536426", "page": successor_cell["page"],
         "quote": successor_cell["sourceLine"]},
    ])
    legacy_proof = {"basis": "000899_H124_CURRENT_OUTSIDE_COMPLETE_FY23_AND_H125_CORRECTION_SCOPES",
            "sourceQuotes": quotes,
            "scope": {"capturedFinancialCorrectionIds": sorted(financial),
                "historicalCorrectionPeriodEnd": "2023-12-31",
                "laterCorrection": {"announcementId": "1225490303",
                    "publicationDate": expected_publication["1225490303"],
                    "adjustedOpeningPeriodEnd": "2026-01-01",
                    "adjustedFlowPeriodEnd": "2025-06-30",
                    "targetMetricH1ValuesBeforeAfterAndDeltaYuan": [
                        "3030507313.49", "3040821229.13", "10313915.64"]},
                "currentAnnouncementId": target_id,
                "currentPublishedDate": expected_publication[target_id],
                "currentFactPeriodEnd": period,
                "currentAmountYuan": current_cell["amountYuan"],
                "applicableSuccessorPrior": {"announcementId": "1224536426",
                    "sourceReportPeriodEnd": "2025-06-30", "factPeriodEnd": period,
                    "columnRole": "PRIOR", "columnHeader": successor_cell["priorHeader"],
                    "amountYuan": successor_cell["priorAmountYuan"]},
                "meaning": "The complete captured corrections explicitly address FY2023 and 2025H1 flows/2026 opening. The original H125 report itself records the same H124 amount in its 2024H1 PRIOR column. No additional H124 PRIOR cell is required in the later correction notice because its literal scope is bounded to different periods. This does not assert no economic effect outside the captured, expressly bounded reports."}}
    if financial == legacy_financial:
        return legacy_proof, None

    extra_notice, error = _finite_000899_h124_current27_notice_scope(documents.get("1225619745"))
    if error:
        return None, error
    proof = {
        **legacy_proof,
        "basis": "000899_H124_CURRENT_WITH_EXPLICIT_FY23_H125_AND_CURRENT27_NOTICE_SCOPES",
        "sourceQuotes": [*legacy_proof["sourceQuotes"], *extra_notice["sourceQuotes"]],
        "scope": {
            **legacy_proof["scope"],
            "capturedFinancialCorrectionIds": sorted(financial),
            "additionalLaterCorrection": {
                key: value for key, value in extra_notice.items() if key != "sourceQuotes"
            },
            "meaning": (
                legacy_proof["scope"]["meaning"] + " The additional 2026-10-24 notice explicitly "
                "covers 2026 opening data and 2025 Jan-Sep flows, with its own consolidated revenue "
                "before/after/delta quoted from the original PDF. This additional scope proof does not "
                "infer a 2024H1 non-effect from omitted 2024 wording; the H125 original PRIOR equality "
                "and observed same-fact comparisons remain required."
            ),
        },
    }
    return proof, None


def _finite_600578_current_scope_policy(code, metric, period, target_id, documents, financial,
                                        current_cell=None):
    """Bound one selected CURRENT cell to the exact captured restatement notice."""
    profiles = {
        ("2025-06-30", "1224562024"): {
            "metrics": {"parent_equity"},
            "title": "京能电力：2025年半年度报告", "publishedDate": "2025-08-23",
            "currentHeader": "2025年6月30日", "priorHeader": "2024年12月31日",
            "priorFactPeriodEnd": "2024-12-31",
        },
        ("2025-12-31", "1225184640"): {
            "metrics": {"net_profit_attributable", "net_profit_consolidated",
                        "operating_cashflow", "revenue"},
            "title": "京能电力：2025年年度报告",
            "publishedDate": "2026-04-25",
            "currentHeader": "2025年度", "priorHeader": "2024年度",
            "priorFactPeriodEnd": "2024-12-31",
        },
        ("2026-06-30", "1225491235"): {
            "metrics": set(FIELDS),
            "title": "京能电力：2026年半年度报告",
            "publishedDate": "2026-08-22",
        },
    }
    profile = profiles.get((period, target_id)) if code == "600578" else None
    if not profile or metric not in profile["metrics"]:
        return None, "600578 selected-CURRENT scope does not include this exact report/field"
    if financial != {"1225429005"}:
        return None, "captured 600578 restatement relationship set differs from the one notice"

    statement = STATEMENT[metric]
    if period == "2026-06-30" and metric == "parent_equity":
        current_header, prior_header, prior_period = "2026年6月30日", "2025年12月31日", "2025-12-31"
    elif period == "2026-06-30":
        current_header, prior_header, prior_period = "2026年半年度", "2025年半年度", "2025-06-30"
    else:
        current_header = profile["currentHeader"]
        prior_header = profile["priorHeader"]
        prior_period = profile["priorFactPeriodEnd"]
    if (not current_cell or current_cell.get("metric") != metric or
            current_cell.get("factPeriodEnd") != period or
            current_cell.get("columnRole") != "CURRENT" or
            current_cell.get("statementScope") != "CONSOLIDATED" or
            current_cell.get("statement") != statement or
            current_cell.get("currentHeader") != current_header or
            current_cell.get("priorHeader") != prior_header or
            current_cell.get("priorFactPeriodEnd") != prior_period or
            current_cell.get("unitRaw") != "元" or
            not current_cell.get("amountYuan") or
            not current_cell.get("priorAmountYuan") or
            not current_cell.get("sourceLine") or
            type(current_cell.get("page")) is not int or current_cell["page"] < 1):
        return None, "selected 600578 CURRENT cell and same-report comparative column are not bound"
    try:
        current_amount = Decimal(current_cell["amountYuan"])
        prior_amount = Decimal(current_cell["priorAmountYuan"])
    except (ArithmeticError, TypeError, ValueError):
        return None, "selected 600578 CURRENT/comparative amount is not a decimal yuan value"
    if not current_amount.is_finite() or not prior_amount.is_finite():
        return None, "selected 600578 CURRENT/comparative amount is not finite"

    current_report = documents.get(target_id)
    notice = documents.get("1225429005")
    current_entry = current_report.get("entry", {}) if current_report else {}
    notice_entry = notice.get("entry", {}) if notice else {}
    if (current_entry.get("title") != profile["title"] or
            current_entry.get("publishedDate") != profile["publishedDate"] or
            notice_entry.get("title") != "京能电力：关于因同一控制下企业合并追溯调整2023年度财务报表数据的专项说明（上网）" or
            notice_entry.get("publishedDate") != "2026-07-17"):
        return None, "selected report/notice identity or publication date differs from the captured profile"
    pages = notice.get("pages", ()) if notice else ()
    if len(pages) != 7 or any(not page for page in pages):
        return None, "all seven native pages of the captured 600578 notice are required"

    quotes = []
    def required(pattern):
        quote = _quote(pages, pattern)
        if quote:
            quotes.append({"announcementId": "1225429005", **quote})
        return quote is not None

    checks = [
        required(r"关于因同一控制下企业合并追溯调整\s*2023\s*年度财务报表数\s*据的专项说明"),
        required(r"本公司于\s*2025\s*年\s*12\s*月取得被合并方的控制权.{0,60}合并日确定为\s*2025\s*年\s*12\s*月\s*31\s*日"),
        required(r"由于合并前后合并双方均受北京能源集团有限责任公司控制且该控制并非\s*暂时性"),
        required(r"追溯调\s*整\s*2023\s*年度财务报表数\s*据[\s\S]{0,100}?2024\s*年度的追溯调整已经\s*2025\s*年度审计报告披\s*露"),
        required(r"合并资产负债表项目的影响如下[\s\S]{0,180}?项目\s*2023\s*年\s*12\s*月\s*31\s*日\s*2023\s*年\s*12\s*月\s*31\s*日\s*重述影响金额"),
        required(r"资产总计\s*95,294,881,367\.51\s*95,931,192,662\.18\s*636,311,294\.67"),
        required(r"负债合计\s*61,292,030,623\.06\s*61,952,262,773\.33\s*660,232,150\.27"),
        required(r"归属于母公司股东权益合计\s*28,079,529,455\.07\s*28,055,608,599\.47\s*-23,920,855\.60"),
        required(r"合并利润表项目的影响如下[\s\S]{0,180}?项目\s*2023\s*年度\s*2023\s*年度\s*重述影响金额"),
        required(r"其中：营业收入\s*33,253,466,332\.68\s*33,302,405,558\.83\s*48,939,226\.15"),
        required(r"归属于母公司股东的净利润[\s\S]{0,80}?881,331,682\.27\s*922,917,636\.65\s*41,585,954\.38"),
        required(r"五、净利润\s*（净亏损以“－”号填\s*列）\s*1,091,390,194\.04\s*1,132,976,148\.42\s*41,585,954\.38"),
        required(r"合并现金流量表项目的影响如下[\s\S]{0,180}?项目\s*2023\s*年度\s*2023\s*年度\s*重述影响金额"),
        required(r"经营活动产生的现金流量净额\s*3,806,408,845\.51\s*3,784,841,886\.12\s*-21,566,959\.39"),
    ]
    if not all(checks):
        return None, "complete notice period scope and material 2023 statement effects are not present"
    quotes.append({"announcementId": target_id, "page": current_cell["page"],
                   "quote": current_cell["sourceLine"]})
    return {"basis": "FINITE_600578_SELECTED_CURRENT_OUTSIDE_EXPLICIT_FY2023_NOTICE",
            "sourceQuotes": quotes,
            "scope": {"noticeAnnouncementId": "1225429005",
                "noticePublishedDate": "2026-07-17", "noticeCorrectionPeriodEnd": "2023-12-31",
                "controlAcquisitionDate": "2025-12-31", "fy2024AdjustmentDisclosedIn": "2025年度审计报告",
                "reportedFy2023EffectsYuan": {"revenue": "48939226.15",
                    "netProfitAttributable": "41585954.38", "netProfitConsolidated": "41585954.38",
                    "operatingCashflow": "-21566959.39", "totalAssets": "636311294.67",
                    "totalLiabilities": "660232150.27", "parentEquity": "-23920855.60"},
                "currentAnnouncementId": target_id,
                "currentPublishedDate": profile["publishedDate"],
                "currentFactPeriodEnd": period, "targetMetric": metric,
                "currentAmountYuan": current_cell["amountYuan"],
                "currentPage": current_cell["page"],
                "applicablePriorFactPeriodEnd": prior_period,
                "applicablePriorHeader": prior_header,
                "applicablePriorAmountYuan": current_cell["priorAmountYuan"],
                "meaning": ("Only the selected H125 parent-equity CURRENT is outside this notice explicitly dated FY2023. The notice says FY2024 adjustments were separately disclosed in the FY2025 audit; it does not supply a replacement balance for 2025-06-30. H125 income/cashflow real changes remain separate, and H126 opening equity is a different fact date. All observed same-fact versions remain subject to the ordinary comparisons." if period == "2025-06-30" else "This notice is expressly scoped to the FY2023 restatement and says the FY2024 adjustment was already disclosed in the FY2025 audit report. It provides no replacement value for this exact selected FY2025/H126 CURRENT cell. Its disclosed FY2023 effects remain material; this scope does not claim no enterprise-wide economic effect. FY24/H125 version changes remain separate facts and this proof does not admit those versions.")}}, None


def assess_revision_impact(*, archive_root: Path, company: Mapping, target_period_end: date,
                           target_metrics: Sequence[str], issuer_name: str,
                           documents: Sequence[Mapping] | None = None) -> MachineRevisionImpact:
    """Re-derive impact from the entire captured issuer relationship set.

    The discovery company supplies page/receipt identities, not trusted verdicts.
    Same-day different figures retain ambiguous order and are not admitted here.
    """
    period, metrics, code = target_period_end.isoformat(), tuple(sorted(set(target_metrics))), company["code"]
    _need(period in PERIODS and metrics and set(metrics) <= FIELDS, "target period/metric outside bounded prototype")
    _need(re.fullmatch(r"\d{6}", code) and company["instrumentId"] ==
          ("SH." if market_for_code(code) == "sse" else "SZ.") + code, "target issuer identity invalid")
    _need(issuer_name == ISSUERS.get(code), "exact full issuer name required by bounded original-source profile")
    code_fingerprint = _parser_code_fingerprint()
    root = resolve_artifact_path(archive_root).resolve()
    input_docs = list(documents if documents is not None else [*company["selectedReportRecords"], *company["relatedDocumentRecords"]])
    by_id, receipts = {}, []
    with sqlite3.connect((root / "meta.sqlite").as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        entries, index_receipts, request_hashes = _index(con, root, company)
        receipts.extend(index_receipts)
        fundraising_special_ids = {
            aid for aid, entry in entries.items()
            if ((code == "000680" and _is_fundraising_special_report(entry["title"]))
                or (code == "000422" and _is_fundraising_special_report(
                    entry["title"], issuer_code=code)))
        }
        report_periods = {
            aid: parsed.isoformat() for aid, entry in entries.items()
            if (parsed := _report_period(entry["title"], issuer_code=code)) is not None
            and parsed.isoformat() in PERIODS
            and disclosure_role(entry["title"]) in (
                DisclosureRole.ORIGINAL_REPORT, DisclosureRole.REVISED_REPORT)
            and not (code == "601069" and (
                _601069_is_annual_reporting_policy(entry["title"])
                or _601069_is_delayed_disclosure_notice(entry["title"])))
        }
        financial = {aid for aid, row in entries.items() if _financial_title(row["title"])}
        fundraising_special_revisions = {
            aid for aid in fundraising_special_ids
            if any(title_revision_flags(entries[aid]["title"])[0:3])
        }
        financial |= fundraising_special_revisions
        recalled = {aid for aid, row in entries.items() if _financial_title(row["title"])
                    or any(title_revision_flags(row["title"])[:3])}
        recalled |= fundraising_special_revisions
        supplied = [str(d["announcementId"]) for d in input_docs]
        missing_fundraising_special_sources = sorted(fundraising_special_ids - set(supplied))
        _need(not missing_fundraising_special_sources,
              "complete indexed fundraising special report originals have not been supplied")
        _need(len(supplied) == len(set(supplied)), "document relationship records duplicated")
        bound_relationships = {str(d["announcementId"]) for d in
                               [*company["selectedReportRecords"], *company["relatedDocumentRecords"]]}
        _need(bound_relationships <= set(supplied), "current archived issuer relationship set omitted")
        _need(recalled <= set(supplied), "complete captured revision relationships not supplied")
        selected = company["selectedReportRecords"]
        _need(len(selected) == len(PERIODS) and {d["periodEnd"] for d in selected} == PERIODS,
              "one canonical current report for each of the five periods required")
        selected_ids = {str(d["announcementId"]) for d in selected}
        _need(all(report_periods.get(str(d["announcementId"])) == d["periodEnd"] for d in selected),
              "canonical report period differs from the actual index")
        _need({d["announcementId"] for d in selected} <= set(supplied), "current/successor report set omitted")
        missing_report_sources = sorted(set(report_periods) - set(supplied))
        for doc in input_docs:
            aid = str(doc["announcementId"])
            _need(aid in entries and doc["code"] == code and doc["instrumentId"] == company["instrumentId"], "document issuer/index identity mismatch")
            entry = entries[aid]
            _need(doc["documentUrl"] == entry["documentUrl"] and normalize_title(doc["title"]) == entry["title"], "document URL/title differs from current index")
            rec, body = _receipt(con, root, doc["receipt"]["receipt_id"], expected_hash=doc["contentHash"], expected_url=entry["documentUrl"])
            for key in ("source_id", "requested_at", "responded_at", "first_seen_at", "content_hash", "byte_size", "url", "outcome", "http_status"):
                _need(doc["receipt"].get(key) == rec[key], f"document receipt {key} differs from SQL")
            _need(doc["byteSize"] == len(body) and body.startswith(b"%PDF-"), "document original bytes/type mismatch")
            text = _pages(body, code_fingerprint=code_fingerprint)
            if aid in financial and any(text):
                native_issuer_present = issuer_name in "".join("".join(p.split()) for p in text)
                opaque_identity = None
                if code == "000422" and not native_issuer_present:
                    source_binding = _financial_source_binding(
                        doc, entry, selected_canonical_report=aid in selected_ids)
                    opaque_identity = _index_bound_000422_opaque_cause_identity(
                        text, entry["title"], issuer_name, code, source_binding)
                _need(native_issuer_present or opaque_identity is not None,
                      f"financial relation exact issuer mismatch for {aid}")
            if doc.get("periodEnd") is not None:
                _need(_report_period(entry["title"], issuer_code=code) == date.fromisoformat(doc["periodEnd"]), "report index period mismatch")
                _need(issuer_name in "".join("".join(p.split()) for p in text[:8]), "report cover exact issuer mismatch")
                source_binding = (_financial_source_binding(
                    doc, entry, selected_canonical_report=aid in selected_ids)
                    if code == "000422" else None)
                auxiliary_identity = (aid not in selected_ids
                    and _auxiliary_financial_identity(text, entry["title"],
                        date.fromisoformat(doc["periodEnd"]), issuer_name, code,
                        source_binding=source_binding))
                _need(_security_code(text[:8], code, company["name"]) or auxiliary_identity,
                      f"report {aid} cover exact security code missing")
                report_date = date.fromisoformat(doc["periodEnd"])
                report_kind = "半年度" if report_date.month == 6 else "年度"
                _need(auxiliary_identity or _quote(text[:3], str(report_date.year) + r"\s*年?\s*" + report_kind + r"\s*(?:财务\s*)?报告"),
                      "report cover exact year/interim period mismatch")
            if aid in report_periods:
                _need(doc.get("periodEnd") == report_periods[aid],
                      "complete report candidate lacks its index-bound period")
            by_id[aid] = {"metadata": dict(doc), "entry": entry, "receipt": rec, "body": body, "pages": text}
            receipts.append(rec)
    target = [d for d in selected if d["periodEnd"] == period]
    _need(len(target) == 1, "target current report is missing/ambiguous")
    target_id = target[0]["announcementId"]
    columns, failures = {}, {}
    for aid, report_period in report_periods.items():
        if aid not in by_id:
            continue
        report_date = date.fromisoformat(report_period)
        relevant = tuple(metric for metric in metrics if report_period == period or
            (report_period > period and (date(report_date.year - 1, 12, 31).isoformat()
            if metric == "parent_equity" else str(report_date.year - 1) + report_period[4:]) == period))
        if not relevant:
            continue
        native_1654_target = (
            (code, period, target_id, metrics, aid, relevant) ==
            ("000422", "2024-12-31", "1222991648", ("revenue",),
             "1222991654", ("revenue",))
        )
        try:
            if native_1654_target:
                existing_parser_result = {"status": "FAILED_CLOSED"}
                try:
                    ordinary = _cells(by_id[aid]["body"], report_period, relevant,
                                      code_fingerprint=code_fingerprint)
                    ordinary_cell = ordinary.get("revenue")
                    existing_parser_result = {
                        "status": "PARSED", "currentAmountYuan": (
                            ordinary_cell.get("amountYuan") if ordinary_cell else None),
                        "page": ordinary_cell.get("page") if ordinary_cell else None,
                        "unitRaw": ordinary_cell.get("unitRaw") if ordinary_cell else None,
                    }
                except (ValueError, ArithmeticError) as exc:
                    existing_parser_result.update({
                        "exceptionType": type(exc).__name__, "message": str(exc),
                    })
                    ordinary_cell = None
                native = _native_000422_fy24_1654_revenue_cell(
                    by_id[aid]["body"], code=code, target_id=target_id, period=period,
                    metric="revenue", announcement_id=aid, document=by_id[aid]["metadata"],
                    entry=by_id[aid]["entry"], receipt=by_id[aid]["receipt"],
                    pages=by_id[aid]["pages"], existing_parser_result=existing_parser_result)
                if ordinary_cell is not None:
                    _need(ordinary_cell.get("amountYuan") == native["amountYuan"]
                          and ordinary_cell.get("page") == native["page"]
                          and ordinary_cell.get("unitRaw") == native["unitRaw"],
                          "1654 native support conflicts with an ordinary parsed CURRENT cell")
                columns[aid] = {"revenue": native}
            else:
                columns[aid] = _cells(by_id[aid]["body"], report_period, relevant,
                                      code_fingerprint=code_fingerprint)
                if code == "000422" and aid == "1224559959":
                    for cell in columns[aid].values():
                        cell["priorFactPeriodEnd"] = None
                        cell["priorAmountYuan"] = None
                        cell["priorPeriodResolution"] = "UNRESOLVED"
                        cell["priorPeriodUnresolvedReason"] = (
                            "原文列头仅为“上期金额”，没有明示比较期间；不由2025-06-30反推期间。"
                        )
        except (ValueError, ArithmeticError) as exc:
            failures[aid] = str(exc)
    h124_auxiliary_concordance, h124_auxiliary_concordance_error = None, None
    if (code, period, target_id, metrics) == (
            "000422", "2024-06-30", "1220963862", ("revenue",)):
        auxiliary_cells, h124_auxiliary_concordance, h124_auxiliary_concordance_error = (
            _h124_auxiliary_duplicate_concordance(by_id, report_periods, columns, selected_ids))
        if h124_auxiliary_concordance_error:
            for aid in _H124_AUXILIARY_CANONICAL:
                columns.pop(aid, None)
                failures[aid] = ("H124 exact auxiliary concordance rejected: "
                                 + h124_auxiliary_concordance_error)
            h124_auxiliary_concordance = None
        else:
            columns.update(auxiliary_cells)
            for aid in auxiliary_cells:
                failures.pop(aid, None)
    results = []
    for metric in metrics:
        origin = columns.get(target_id, {}).get(metric)
        h124_scoped_comparison = None
        finite_600578 = code == "600578" and (
            (period, target_id) in {
                ("2025-12-31", "1225184640"), ("2026-06-30", "1225491235")}
            or (metric == "parent_equity" and
                (period, target_id) == ("2025-06-30", "1224562024")))
        finite_h126 = code in {"000899", "600338"} and period == "2026-06-30"
        finite_later = (code, period, target_id) in {
            ("000899", "2025-12-31", "1225121246"),
            ("600338", "2025-06-30", "1224568683"),
            ("600338", "2024-12-31", "1223383463"),
            ("600338", "2025-12-31", "1225153721"),
        }
        finite_000899_h124 = (metric == "revenue" and (code, period, target_id) == (
            "000899", "2024-06-30", "1220893776"))
        finite_000422_h124_revenue = (code, metric, period, target_id) == (
            "000422", "revenue", "2024-06-30", "1220963862")
        finite_000422_h125 = (code, period, target_id) == (
            "000422", "2025-06-30", "1224559960")
        finite_000422_fy25_four = (code, period, target_id) == (
            "000422", "2025-12-31", "1225183616")
        finite_000422_h126_four = (code, period, target_id) == (
            "000422", "2026-06-30", "1225468007")
        if finite_000422_h124_revenue:
            proof, error = _finite_000422_h124_revenue_policy(
                code, metric, period, target_id, by_id, financial, origin,
                requested_metrics=metrics, report_periods=report_periods, columns=columns,
                selected_report_ids=selected_ids)
        elif finite_000422_h126_four:
            proof, error = _finite_000422_h126_four_flow_policy(
                code, metric, period, target_id, by_id, financial, origin,
                requested_metrics=metrics, selected_report_ids=selected_ids, company=company)
        elif finite_000422_h125:
            proof, error = _finite_000422_h125_four_policy(
                code, metric, period, target_id, by_id, financial, origin,
                requested_metrics=metrics, report_periods=report_periods, columns=columns,
                selected_report_ids=selected_ids)
        elif finite_000422_fy25_four:
            proof, error = _finite_000422_fy25_four_current_policy(
                code, metric, period, target_id, by_id, financial, origin,
                requested_metrics=metrics, selected_report_ids=selected_ids)
        elif finite_000899_h124:
            proof, error = _finite_000899_h124_period_scope_policy(
                code, metric, period, target_id, by_id, financial,
                current_cell=origin, report_periods=report_periods, columns=columns)
        elif finite_600578:
            proof, error = _finite_600578_current_scope_policy(
                code, metric, period, target_id, by_id, financial, current_cell=origin)
        elif (code, period, target_id) == ("601069", "2025-12-31", "1225082554"):
            proof, error = _annual_601069_fy25_native_policy(
                metric, by_id, financial, origin)
        elif (code, period, target_id) == ("601069", "2024-06-30", "1221007391"):
            proof, error, h124_scoped_comparison = _h124_601069_native_prior_policy(
                metric, by_id, financial, origin, columns)
        elif finite_h126:
            proof, error = _finite_h126_policy(code, metric, period, target_id, by_id, financial)
        elif finite_later:
            proof, error = _finite_later_period_policy(code, metric, period, target_id, by_id, financial,
                                                       current_cell=origin)
        else:
            proof, error = _policy(code, metric, period, by_id, financial)
        if (proof is not None and
                (code, metric, period, target_id) == ("000422", "revenue", "2024-06-30", "1220963862")
                and h124_auxiliary_concordance is not None):
            proof["scope"]["auxiliaryDuplicateSourceCorroborations"] = h124_auxiliary_concordance
        finite_later_postdisclosure_scope = bool(proof and proof.get("basis") ==
            "EXPLICIT_COMPLETE_FY2023_CORRECTION_SCOPE_POSTDISCLOSURE_CURRENT")
        finite_fy25_scope = bool(proof and proof.get("basis") ==
            "FINITE_FY25_CURRENT_NOT_REPLACED_BY_EXPLICIT_OTHER_PERIOD_CORRECTIONS")
        finite_000422_fy25_scope = bool(proof and proof.get("basis") ==
            "EXPLICIT_FY23_FY24_CORRECTION_SCOPE_WITH_FY25_CURRENT_CONSOLIDATION")
        finite_000422_h126_scope = bool(proof and proof.get("basis") ==
            "FINITE_000422_H126_CURRENT_FLOW_WITH_NO_PRIOR_YEAR_RESTATEMENT_AND_COMPLETE_FY23_FY24_SCOPE")
        finite_000422_h124_scope = bool(proof and proof.get("basis") ==
            "FINITE_000422_H124_REVENUE_CURRENT_TO_H125_PRESENTED_PRIOR")
        finite_600578_scope = bool(proof and proof.get("basis") ==
            "FINITE_600578_SELECTED_CURRENT_OUTSIDE_EXPLICIT_FY2023_NOTICE")
        finite_601069_scope = bool(proof and proof.get("basis") ==
            "FY25_ANNUAL_CURRENT_AND_2024_ADJUSTED_PRIOR_NOT_REPLACED_BY_INTERIM_CORRECTION_SET")
        comparisons, different, ambiguous, missing_comparisons = [], False, False, []
        if origin:
            amounts_by_day = {by_id[target_id]["entry"]["publishedDate"]: {Decimal(origin["amountYuan"])}}
            for aid, candidate_period in report_periods.items():
                report_period = date.fromisoformat(candidate_period)
                expected_prior = date(report_period.year - 1, 12, 31).isoformat() if metric == "parent_equity" else (
                    str(report_period.year - 1) + candidate_period[4:])
                same_period = candidate_period == period and aid != target_id
                successor = candidate_period > period and expected_prior == period
                if not same_period and not successor:
                    continue
                cell = columns.get(aid, {}).get(metric)
                amount = (cell.get("amountYuan") if same_period else cell.get("priorAmountYuan")) if cell else None
                if (not cell or amount is None or
                        (successor and cell["priorFactPeriodEnd"] != period)):
                    missing_comparisons.append(aid)
                    continue
                old, new = Decimal(origin["amountYuan"]), Decimal(amount)
                changed = old != new
                different |= changed
                day = by_id[aid]["entry"]["publishedDate"]
                amounts_by_day.setdefault(day, set()).add(new)
                comparison = {"announcementId": aid, "sourceReportPeriodEnd": candidate_period,
                    "factPeriodEnd": period, "columnRole": "CURRENT" if same_period else "PRIOR", "amountYuan": amount,
                    "page": cell["page"], "sourceLine": cell["sourceLine"], "rowLabel": cell["rowLabel"],
                    "unitRaw": cell["unitRaw"], "columnHeader": cell["currentHeader"] if same_period else cell["priorHeader"],
                    "changed": changed}
                if cell.get("sourceNativeEvidence") is not None:
                    comparison["sourceNativeEvidence"] = cell["sourceNativeEvidence"]
                if cell.get("unassignedPriorColumn") is not None:
                    comparison["unassignedPriorColumn"] = cell["unassignedPriorColumn"]
                comparisons.append(comparison)
            if h124_scoped_comparison is not None:
                amount = Decimal(h124_scoped_comparison["amountYuan"])
                changed = Decimal(origin["amountYuan"]) != amount
                h124_scoped_comparison["changed"] = changed
                different |= changed
                day = by_id[h124_scoped_comparison["announcementId"]]["entry"]["publishedDate"]
                amounts_by_day.setdefault(day, set()).add(amount)
                comparisons.append(h124_scoped_comparison)
            ambiguous = any(len(amounts) > 1 for amounts in amounts_by_day.values())
        status = "UNRESOLVED" if ambiguous or missing_report_sources else "AFFECTS" if different else "NO_EFFECT" if proof and origin and not missing_comparisons else "UNRESOLVED"
        if missing_comparisons:
            error = "existing same-fact successor original cell could not be reconstructed"
        if missing_report_sources:
            error = "complete indexed report candidate originals have not been supplied"
        # Auxiliary profiles require a successor equality unless an exact
        # captured-period scope above supports the selected fact on its own.
        if (status == "NO_EFFECT" and code != "000902" and not comparisons
                and not finite_h126 and not finite_later_postdisclosure_scope
                and not finite_fy25_scope and not finite_600578_scope
                and not finite_601069_scope and not finite_000422_fy25_scope
                and not finite_000422_h126_scope and not finite_000422_h124_scope):
            status, error = "UNRESOLVED", "no same-fact successor cell supports auxiliary profile"
        results.append({"metric": metric, "periodEnd": period, "status": status,
            "admissionStatus": "NOT_ADMITTED", "currentSource": {"announcementId": target_id,
                "sourceReportPeriodEnd": period, **origin} if origin else None,
            "comparisons": comparisons, "impactProof": proof, "reason":
                "same-day differing versions have no proven order" if ambiguous else
                error if missing_report_sources else
                "a captured report version changes this exact fact" if different else error,
            "numericSourceFailures": failures, "unresolvedSuccessorIds": missing_comparisons,
            "missingReportSourceIds": missing_report_sources})
    completed = datetime.now(timezone.utc)
    _need(all(_time(r["first_seen_at"]) <= completed for r in receipts), "source evidence observed after machine completion")
    return MachineRevisionImpact({"schema": SCHEMA, "verifierId": VERIFIER,
        "instrumentId": company["instrumentId"], "issuerName": issuer_name, "targetPeriodEnd": period,
        "completedAt": completed.isoformat(), "outcomes": results,
        "relationshipScope": {"publicationWindow": list(company["publicationWindow"]),
            "capturedFrom": min(receipts, key=lambda r: _time(r["requested_at"]))["requested_at"],
            "capturedTo": max(receipts, key=lambda r: _time(r["responded_at"]))["responded_at"],
            "financialCandidateIds": sorted(financial | set(report_periods) | fundraising_special_ids),
            "financialCauseIds": sorted(financial),
            **({"fundraisingSpecialReportIds": sorted(fundraising_special_ids),
                "missingFundraisingSpecialReportIds": missing_fundraising_special_sources}
               if code in {"000680", "000422"} else {}),
            "selectedReportIds": sorted(d["announcementId"] for d in selected),
            "reportCandidatePeriods": dict(sorted(report_periods.items())),
            "missingReportSourceIds": missing_report_sources,
            "allRecalledRevisionIds": sorted(recalled),
            "completeForArchivedIndexResponse": not missing_report_sources and not missing_fundraising_special_sources,
            "excludesLaterUnobservedAnnouncements": False,
            "requestBodyHashes": request_hashes},
        "sources": [{"announcementId": aid, "index": item["entry"], "receipt": item["receipt"],
                     "originalHash": _hash(item["body"]), "originalBytes": len(item["body"]),
                     "nativeTextAvailable": any(item["pages"])} for aid, item in sorted(by_id.items())],
        "indexReceipts": index_receipts, "writesFinancialWarehouse": False,
        "boundary": "Impact applies only to the complete captured relationship set and specified fact. No financial admission, historical availability, human attestation, or comparative-column backfill is produced."})


def replay_revision_impact(saved: Mapping, **inputs) -> MachineRevisionImpact:
    """Re-derive from originals; a self-authored verdict/hash is not evidence."""
    payload = dict(saved)
    digest = payload.pop("evidenceHash", None)
    _need(digest == _hash(_canonical(payload).encode()), "saved impact canonical hash mismatch")
    fresh = assess_revision_impact(**inputs)
    before, after = dict(payload), dict(fresh.evidence)
    recorded = _time(before.pop("completedAt"))
    after.pop("completedAt")
    _need(_time(payload["relationshipScope"]["capturedTo"]) <= recorded <= datetime.now(timezone.utc), "saved completion time invalid")
    _need(before == after, "saved impact differs from original-byte/index/relationship replay")
    return fresh
