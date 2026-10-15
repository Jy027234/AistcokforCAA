"""Replay bounded original-source evidence for unchanged S2 revision fields.

This produces an impact assessment, never a financial fact or an admission.
Unknown relationships and changed comparative amounts do not become NO_EFFECT.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from importlib.metadata import version
import io
import json
from pathlib import Path
import re
import sqlite3
import sys
from threading import RLock
from typing import Mapping, Sequence
from urllib.parse import parse_qs, urlsplit

from aquant.artifact_paths import resolve_archive_member, resolve_artifact_path
from aquant.domain.fundamentals.disclosure_link import (
    DisclosureRole, disclosure_role, is_financial_correction_title, report_period_from_title,
)
from .cninfo_probe import market_for_code, normalize_title, title_revision_flags
from .cninfo_statement_sections import extract_statement_sections, PRIOR_NOT_ON_LINE

SCHEMA = "aquant.s2-machine-revision-impact.v1"
VERIFIER = "machine:s2-revision-impact-v1"
FIELDS = frozenset({"revenue", "net_profit_attributable", "net_profit_consolidated",
                    "operating_cashflow", "parent_equity"})
STATEMENT = {"revenue": "利润表", "net_profit_attributable": "利润表",
             "net_profit_consolidated": "利润表", "operating_cashflow": "现金流量表",
             "parent_equity": "资产负债表"}
SCALE = {"元": Decimal(1), "千元": Decimal(1000), "万元": Decimal(10000), "百万元": Decimal(1000000)}
PERIODS = frozenset({"2024-06-30", "2024-12-31", "2025-06-30", "2025-12-31", "2026-06-30"})
ISSUERS = {"000902": "新洋丰农业科技股份有限公司", "600338": "西藏珠峰资源股份有限公司",
           "000899": "江西赣能股份有限公司", "002602": "浙江世纪华通集团股份有限公司"}
CST = timezone(timedelta(hours=8))

# Complete bytes remain in the key. The fixed 68-original source set is about
# 106 MiB, so a 32-entry text LRU could repeatedly discard valid pure parses.
# These are process-local parsing caches, never receipt or admission caches.
_PARSE_CACHE_VERSION = "revision-pure-parse-v1"
_PARSER_CODE_INPUTS = tuple(Path(__file__).with_name(name) for name in (
    "s2_revision_impact.py", "cninfo_statement_sections.py",
    "cninfo_financial_candidate.py", "cninfo_financial_pdf.py",
    "cninfo_probe.py", "cninfo_report_identity.py",
)) + (Path(__file__).parents[2] / "domain/fundamentals/disclosure_link.py",
      Path(__file__).parents[2] / "artifact_paths.py")
_MISSING = object()


def _parser_code_fingerprint():
    # Re-hash actual parser/identity dependencies once per assessment. An
    # edited dependency or installed parser version cannot hit an older parse.
    return _hash(_canonical({"version": _PARSE_CACHE_VERSION,
        "code": [(str(path), _hash(path.read_bytes())) for path in _PARSER_CODE_INPUTS],
        "libraries": {name: version(name) for name in ("pypdf", "pdfplumber")}}).encode())


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
        self.hits = self.misses = 0
        self.lock = RLock()

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

    def clear(self):
        with self.lock:
            self.entries.clear()
            self.bytes = self.hits = self.misses = 0

    def info(self):
        with self.lock:
            return {"entries": len(self.entries), "bytes": self.bytes,
                    "hits": self.hits, "misses": self.misses,
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


def _report_period(title):
    """Keep global legacy matching; recognize this exact financial-only alias."""
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
            entries[aid] = {"announcementId": aid, "title": normalize_title(item["announcementTitle"]),
                "documentUrl": "https://static.cninfo.com.cn/" + path.lstrip("/"), "publishedDate": day,
                "indexReceiptId": rec["receipt_id"], "indexResponseHash": rec["content_hash"]}
        receipts.append(rec)
        request_hashes.append(digest)
    _need(company["allCategoryTermination"] == "has_more_false" and company["allCategoryTotalAnnouncement"] == total,
          "index declared termination/total mismatch")
    return entries, receipts, request_hashes


def _pages(body, *, code_fingerprint=None):
    key = (code_fingerprint or _parser_code_fingerprint(), body)
    cached = _page_parse_cache.get(key)
    if cached is not _MISSING:
        return cached
    from pypdf import PdfReader
    parsed = tuple(re.sub(r"\s+", " ", page.extract_text() or "").strip()
                   for page in PdfReader(io.BytesIO(body), strict=False).pages)
    return _page_parse_cache.put(key, parsed)


def _sections(body, period, *, code_fingerprint=None):
    key = (code_fingerprint or _parser_code_fingerprint(), body, period)
    cached = _section_parse_cache.get(key)
    if cached is not _MISSING:
        return cached
    parsed = extract_statement_sections(body, period_end=date.fromisoformat(period), roles=("合并",))
    return _section_parse_cache.put(key, parsed)


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


def _auxiliary_financial_identity(pages, title, period, issuer_name, code):
    """Financial-only annexes bind issuer/period in cover and prepared statement.

    These official annexes omit a stock-identity cover. Their index/SQL joins
    remain mandatory at the caller; canonical listed-company reports never
    use this branch. An explicit contradictory security identity closes it.
    """
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


def _policy(code, metric, period, documents, financial):
    """Only original-body-supported narrow profiles; unknown additions stay open."""
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
        report_periods = {
            aid: parsed.isoformat() for aid, entry in entries.items()
            if (parsed := _report_period(entry["title"])) is not None
            and parsed.isoformat() in PERIODS
            and disclosure_role(entry["title"]) in (
                DisclosureRole.ORIGINAL_REPORT, DisclosureRole.REVISED_REPORT)
        }
        financial = {aid for aid, row in entries.items() if _financial_title(row["title"])}
        recalled = {aid for aid, row in entries.items() if _financial_title(row["title"])
                    or any(title_revision_flags(row["title"])[:3])}
        supplied = [str(d["announcementId"]) for d in input_docs]
        _need(len(supplied) == len(set(supplied)), "document relationship records duplicated")
        bound_relationships = {str(d["announcementId"]) for d in
                               [*company["selectedReportRecords"], *company["relatedDocumentRecords"]]}
        _need(bound_relationships <= set(supplied), "current archived issuer relationship set omitted")
        _need(recalled <= set(supplied), "complete captured revision relationships not supplied")
        selected = company["selectedReportRecords"]
        _need(len(selected) == len(PERIODS) and {d["periodEnd"] for d in selected} == PERIODS,
              "one canonical current report for each of the five periods required")
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
                _need(issuer_name in "".join("".join(p.split()) for p in text), "financial relation exact issuer mismatch")
            if doc.get("periodEnd") is not None:
                _need(_report_period(entry["title"]) == date.fromisoformat(doc["periodEnd"]), "report index period mismatch")
                _need(issuer_name in "".join("".join(p.split()) for p in text[:8]), "report cover exact issuer mismatch")
                auxiliary_identity = (aid not in {str(d["announcementId"]) for d in selected}
                    and _auxiliary_financial_identity(text, entry["title"],
                        date.fromisoformat(doc["periodEnd"]), issuer_name, code))
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
        try:
            columns[aid] = _cells(by_id[aid]["body"], report_period, relevant,
                                  code_fingerprint=code_fingerprint)
        except (ValueError, ArithmeticError) as exc:
            failures[aid] = str(exc)
    results = []
    for metric in metrics:
        origin = columns.get(target_id, {}).get(metric)
        proof, error = _policy(code, metric, period, by_id, financial)
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
                comparisons.append({"announcementId": aid, "sourceReportPeriodEnd": candidate_period,
                    "factPeriodEnd": period, "columnRole": "CURRENT" if same_period else "PRIOR", "amountYuan": amount,
                    "page": cell["page"], "sourceLine": cell["sourceLine"], "rowLabel": cell["rowLabel"],
                    "unitRaw": cell["unitRaw"], "columnHeader": cell["currentHeader"] if same_period else cell["priorHeader"], "changed": changed})
            ambiguous = any(len(amounts) > 1 for amounts in amounts_by_day.values())
        status = "UNRESOLVED" if ambiguous or missing_report_sources else "AFFECTS" if different else "NO_EFFECT" if proof and origin and not missing_comparisons else "UNRESOLVED"
        if missing_comparisons:
            error = "existing same-fact successor original cell could not be reconstructed"
        if missing_report_sources:
            error = "complete indexed report candidate originals have not been supplied"
        # The three auxiliary profiles also require an actual successor equality;
        # issuer000902 has an explicit complete zero-impact scope for current facts
        # that legitimately have no later annual/midyear comparison yet.
        if status == "NO_EFFECT" and code != "000902" and not comparisons:
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
            "financialCandidateIds": sorted(financial | set(report_periods)),
            "financialCauseIds": sorted(financial),
            "selectedReportIds": sorted(d["announcementId"] for d in selected),
            "reportCandidatePeriods": dict(sorted(report_periods.items())),
            "missingReportSourceIds": missing_report_sources,
            "allRecalledRevisionIds": sorted(recalled),
            "completeForArchivedIndexResponse": not missing_report_sources, "excludesLaterUnobservedAnnouncements": False,
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
