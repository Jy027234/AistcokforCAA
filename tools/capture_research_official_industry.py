"""Archive and extract official CAPCO listed-company industry classifications.

CAPCO's 2026 H1 classification is kept as a distinct taxonomy from BaoStock's
CSRC industry snapshot. Matching is by exact official six-digit security code.
"""
from __future__ import annotations

import argparse
import hashlib
import html.parser
import io
import ipaddress
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.fetch_guard import (  # noqa: E402
    FetchDenied, FetchPolicy, _is_blocked_ip, check_content_length, read_bounded,
)
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402

ARCHIVE_ROOT = ROOT / "deploy/agentctl-q0/forward-archive"
OUTPUT = ROOT / "data/s2-structured/research-industry-supplement-20261009.json"
CHECKPOINT = ROOT / "data/s2-structured/research-industry-supplement-20261009.checkpoint.jsonl"
UNIVERSE = ROOT / "data/s2-structured/universe.txt"
ORG_MAP = ROOT / "data/s2-structured/universe-orgids.json"
INDUSTRY = ROOT / "data/s2-structured/research-industry-20261009.json"
QUOTES = ROOT / "data/s2-structured/research-quotes-close-20261009.json"
ARTICLE_URL = ("https://www.capco.org.cn/xhgg/hyfl/hyfljg/202609/20260930/"
               "j_2026093015101300017907523338615404.html")
GUIDE_URL = ("https://www.capco.org.cn/xhdt/tzgg/202305/20230521/"
             "j_2023052117544500016846630061707656.html")
SOURCE_ID = "capco-industry-classification"
MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_PDF_BYTES = 64 * 1024 * 1024


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def assert_capco_url_allowed(url: str, policy: FetchPolicy) -> None:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or parts.username or parts.password:
        raise FetchDenied("url-not-allowed", "only credential-free HTTPS is accepted")
    if not policy.host_allowed(host):
        raise FetchDenied("host-not-allowed", f"{host!r} is not in the CAPCO allowlist")
    if parts.port not in (None, 443):
        exact_attachment = (host == "sp.capco.org.cn" and parts.port == 82
                            and parts.path == "/file/202609/gwfile/2026shangbangupiaodaima.pdf")
        if not exact_attachment:
            raise FetchDenied("port-not-allowed", "only the exact CAPCO-hosted PDF href may use HTTPS port 82")
    try:
        infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FetchDenied("dns-failure", f"cannot resolve {host!r}: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        why = _is_blocked_ip(ip)
        if why and not any(ip.version == net.version and ip in net for net in policy.trusted_proxy_ips()):
            raise FetchDenied("blocked-address", f"{host} resolves to {ip} ({why})")


class CapcoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, policy: FetchPolicy) -> None:
        self.policy = policy
        self.hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops += 1
        if self.hops > self.policy.max_redirects:
            raise FetchDenied("too-many-redirects", "CAPCO redirect limit exceeded")
        assert_capco_url_allowed(newurl, self.policy)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class LinkParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict] = []
        self._active: dict | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() == "a":
            values = dict(attrs)
            href = values.get("href")
            if href:
                self._active = {"href": href, "text": "", "title": values.get("title")}

    def handle_data(self, data: str) -> None:
        if self._active is not None:
            self._active["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._active is not None:
            self.links.append(self._active)
            self._active = None


class TextParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = re.sub(r"\s+", " ", data).strip()
        if value:
            self.parts.append(value)


def fetch(url: str, *, label: str, archive: ForwardArchive, policy: FetchPolicy,
          timeout: float = 30.0, limit: int) -> tuple[bytes | None, dict]:
    requested = datetime.now(timezone.utc)
    try:
        assert_capco_url_allowed(url, policy)
        opener = urllib.request.build_opener(CapcoRedirectHandler(policy))
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept": "text/html,application/pdf,application/octet-stream,*/*",
            "Referer": "https://www.capco.org.cn/",
        })
        with opener.open(req, timeout=timeout) as response:
            check_content_length(response.headers.get("Content-Length"), policy, max_bytes=limit)
            body = read_bounded(response, policy, max_bytes=limit)
            status = response.status
            final_url = response.geturl()
        digest, _ = archive.store_bytes(body, media_type="application/pdf" if body.startswith(b"%PDF") else "text/html; charset=utf-8")
        receipt = archive.record(source_id=SOURCE_ID, url=url, outcome="OK",
            requested_at=requested, responded_at=datetime.now(timezone.utc), http_status=status,
            content_hash=digest, byte_size=len(body), detail=label)
        return body, {"receipt_id": receipt.receipt_id, "url": url, "final_url": final_url,
            "requested_at": receipt.requested_at.isoformat(), "responded_at": receipt.responded_at.isoformat(),
            "http_status": status, "content_hash": digest, "byte_size": len(body), "label": label}
    except Exception as exc:
        receipt = archive.record(source_id=SOURCE_ID, url=url, outcome="TRANSPORT_ERROR",
            requested_at=requested, responded_at=datetime.now(timezone.utc),
            detail=f"{label}: {type(exc).__name__}: {str(exc)[:250]}")
        return None, {"receipt_id": receipt.receipt_id, "url": url,
            "requested_at": requested.isoformat(), "responded_at": datetime.now(timezone.utc).isoformat(),
            "outcome": "TRANSPORT_ERROR", "detail": f"{type(exc).__name__}: {str(exc)[:250]}", "label": label}


def reuse_same_day(url: str, *, con, archive: ForwardArchive) -> tuple[bytes, dict] | None:
    row = con.execute(
        "SELECT receipt_id,url,requested_at,responded_at,http_status,content_hash,byte_size,detail "
        "FROM fetch_receipt WHERE source_id=? AND url=? AND outcome='OK' "
        "AND requested_at LIKE '2026-10-09%' ORDER BY requested_at DESC LIMIT 1",
        (SOURCE_ID, url)).fetchone()
    if not row or row["http_status"] != 200 or not archive.verify(row["content_hash"]):
        return None
    body = archive.load_bytes(row["content_hash"])
    if len(body) != row["byte_size"]:
        return None
    return body, {"receipt_id": row["receipt_id"], "url": url,
        "requested_at": row["requested_at"], "responded_at": row["responded_at"],
        "http_status": row["http_status"], "content_hash": row["content_hash"],
        "byte_size": row["byte_size"], "label": row["detail"],
        "reused_from_verified_same_day_receipt": True}


def target_codes() -> tuple[list[str], dict[str, str]]:
    universe = UNIVERSE.read_text(encoding="utf-8-sig").split()
    org_doc = json.loads(ORG_MAP.read_text(encoding="utf-8-sig"))
    if set(universe) != set(org_doc["organization_ids"]):
        raise RuntimeError("official universe/orgId map mismatch")
    bao = json.loads(INDUSTRY.read_text(encoding="utf-8"))
    classified = {row["code"] for row in bao["rows"] if row.get("classification_status") == "classified"}
    quotes = json.loads(QUOTES.read_text(encoding="utf-8"))
    active_unknown = sorted({row["official_code"] for row in quotes["rows"]
        if row["official_code"] not in classified and row.get("close_time_eligible")
        and row.get("volume_raw", "").strip() not in {"", "0", "0.0"}
        and row.get("turnover_amount_raw", "").strip() not in {"", "0", "0.0"}})
    return active_unknown, org_doc["organization_ids"]


def extract_rows(pdf_bytes: bytes) -> tuple[list[dict], list[str]]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("pypdf is required to extract the official classification PDF") from exc
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    extracted = [(ix + 1, (page.extract_text() or "")) for ix, page in enumerate(reader.pages)]
    result: list[dict] = []
    categories = {
        "A": "农、林、牧、渔业", "B": "采矿业", "C": "制造业",
        "D": "电力、热力、燃气及水生产和供应业", "E": "建筑业",
        "F": "批发和零售业", "G": "交通运输、仓储和邮政业",
        "H": "住宿和餐饮业", "I": "信息传输、软件和信息技术服务业",
        "J": "金融业", "K": "房地产业", "L": "租赁和商务服务业",
        "M": "科学研究和技术服务业", "N": "水利、环境和公共设施管理业",
        "O": "居民服务、修理和其他服务业", "P": "教育",
        "Q": "卫生和社会工作", "R": "文化、体育和娱乐业", "S": "综合",
    }
    category_pattern = "|".join(re.escape(value) for value in sorted(categories.values(), key=len, reverse=True))

    def finish_record(page_number: int, lines: list[str]) -> None:
        if not lines:
            return
        first = re.match(r"^\s*(\d{6})(?!\d)\s*(.*)$", lines[0])
        if not first:
            return
        code, first_tail = first.groups()
        raw_lines = [first_tail, *lines[1:]]
        raw_text = " ".join(re.sub(r"\s+", " ", line).strip() for line in raw_lines if line.strip())
        flat = re.sub(r"\s+", "", raw_text)
        cat = re.search(r"([A-S])(" + category_pattern + r")", flat)
        parsed: dict[str, str | None] = {"category_code": None, "category_name": None,
                                         "subcategory_code": None, "subcategory_name": None,
                                         "major_code": None, "major_name": None}
        if cat:
            code_for_name = {v: k for k, v in categories.items()}
            parsed["category_code"] = cat.group(1)
            parsed["category_name"] = cat.group(2)
            remainder = flat[cat.end():]
            major = re.search(r"(\d{2})(.*)$", remainder)
            if major:
                before_major = remainder[:major.start()]
                parsed["major_code"] = major.group(1)
                parsed["major_name"] = major.group(2) or None
                sub = re.match(r"([A-Z]{2})(.*)$", before_major)
                if sub:
                    parsed["subcategory_code"] = sub.group(1)
                    parsed["subcategory_name"] = sub.group(2) or None
            # Defensive sanity check for category code/name alignment.
            if code_for_name.get(parsed["category_name"]) != parsed["category_code"]:
                parsed = {key: None for key in parsed}
        result.append({"code_raw": code, "page": page_number,
                       "company_name_raw": flat[:cat.start()] if cat else None,
                       "company_and_classification_raw": raw_text,
                       "classification_code_tail_raw": flat[cat.start():] if cat else None,
                       "parse_status": "parsed" if parsed["category_code"] and parsed["major_code"] else "raw_only",
                       **parsed})

    # PDF text extraction may wrap category cells across lines. Treat each line
    # starting with an explicit six-digit code as one new row and retain all
    # continuation lines until the next code row/page boundary.
    for page_number, text in extracted:
        current: list[str] = []
        for raw_line in text.splitlines():
            line = re.sub(r"[\u00a0\t]+", " ", raw_line).strip()
            if re.match(r"^\d{6}(?!\d)", line):
                finish_record(page_number, current)
                current = [line]
            elif current and line:
                current.append(line)
        finish_record(page_number, current)
    return result, [text for _, text in extracted[:3]]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--probe-only", action="store_true", help="fetch official article/PDF and emit first-page parsing sample")
    ap.add_argument("--trusted-proxy-network", action="append", default=[])
    args = ap.parse_args()
    if args.trusted_proxy_network:
        os.environ["AQUANT_TRUSTED_PROXY_NETWORKS"] = ",".join(args.trusted_proxy_network)
    proxy_nets = frozenset(x.strip() for x in os.environ.get("AQUANT_TRUSTED_PROXY_NETWORKS", "").split(",") if x.strip())
    policy = FetchPolicy(resolve_dns=True,
        allowed_hosts=frozenset({"www.capco.org.cn", "capco.org.cn", "sp.capco.org.cn"}),
        max_redirects=3, max_response_bytes=MAX_PDF_BYTES, trusted_proxy_networks=proxy_nets)
    targets, org_ids = target_codes()
    prepared = utc()
    with connect(ARCHIVE_ROOT / "meta.sqlite") as con:
        archive = ForwardArchive(con, ARCHIVE_ROOT)
        reusable_article = reuse_same_day(ARTICLE_URL, con=con, archive=archive)
        if reusable_article:
            article_body, article_receipt = reusable_article
        else:
            article_body, article_receipt = fetch(ARTICLE_URL, label="capco:2026h1-industry-article",
                archive=archive, policy=policy, limit=MAX_PAGE_BYTES)
        if not article_body:
            raise SystemExit(f"official CAPCO article fetch failed: {article_receipt}")
        parser = LinkParser(); parser.feed(article_body.decode("utf-8", "replace"))
        candidates = [link for link in parser.links
            if "按股票代码排序" in (link.get("text", "") + " " + (link.get("title") or ""))]
        if len(candidates) != 1:
            raise SystemExit(f"expected exactly one code-sorted official PDF attachment, got {len(candidates)}")
        attachment_url = urllib.parse.urljoin(ARTICLE_URL, candidates[0]["href"])
        reusable_pdf = reuse_same_day(attachment_url, con=con, archive=archive)
        if reusable_pdf:
            attachment_body, pdf_receipt = reusable_pdf
        else:
            attachment_body, pdf_receipt = fetch(attachment_url, label="capco:2026h1-industry-code-sorted-pdf",
                archive=archive, policy=policy, limit=MAX_PDF_BYTES)
        if not attachment_body or not attachment_body.startswith(b"%PDF"):
            raise SystemExit(f"official CAPCO attachment is not a fetched PDF: {pdf_receipt}")
        time.sleep(2.0)
        reusable_guide = reuse_same_day(GUIDE_URL, con=con, archive=archive)
        if reusable_guide:
            guide_body, guide_receipt = reusable_guide
        else:
            guide_body, guide_receipt = fetch(GUIDE_URL, label="capco:industry-statistics-guideline",
                archive=archive, policy=policy, limit=MAX_PAGE_BYTES)
        guide_text = None
        if guide_body:
            text_parser = TextParser(); text_parser.feed(guide_body.decode("utf-8", "replace"))
            guide_text = " ".join(text_parser.parts)
            guide_title = "中国上市公司协会上市公司行业统计分类指引"
            # The HTML repeats its title in <title> before inline CSS/navigation;
            # keep the final occurrence, which begins the visible article body.
            title_at = guide_text.rfind(guide_title)
            if title_at >= 0:
                guide_text = guide_text[title_at:]
        raw_rows, page_samples = extract_rows(attachment_body)
        target_set = set(targets)
        targeted = [row for row in raw_rows if row["code_raw"] in target_set]
        pdf_reader = __import__("pypdf").PdfReader(io.BytesIO(attachment_body))
        all_pairs = sorted({(row.get("category_code"), row.get("category_name")) for row in raw_rows
                            if row.get("category_code") and row.get("category_name")})
        target_pairs: dict[str, int] = {}
        for row in targeted:
            if row.get("category_code") and row.get("category_name"):
                key = f'{row["category_code"]} {row["category_name"]}'
                target_pairs[key] = target_pairs.get(key, 0) + 1
        by_code: dict[str, list[dict]] = {}
        for row in targeted:
            by_code.setdefault(row["code_raw"], []).append(row)
        missing = sorted(target_set - set(by_code))
        duplicates = {code: rows for code, rows in by_code.items() if len(rows) != 1}
        conflicts = {code: rows for code, rows in by_code.items()
                     if len({(r.get("category_code"), r.get("subcategory_code"), r.get("major_code"))
                             for r in rows}) > 1}
        rows_out = []
        for row in targeted:
            code = row["code_raw"]
            rows_out.append({
                "official_code": code,
                "org_id": org_ids[code],
                "same_org_official_codes_annotation_only": sorted(
                    c for c, org in org_ids.items() if org == org_ids[code] and c != code),
                "company_name_raw": row["company_name_raw"],
                "industry_category_code": row["category_code"],
                "industry_category_label": row["category_name"],
                "industry_subcategory_code": row["subcategory_code"],
                "industry_subcategory_label": row["subcategory_name"],
                "industry_major_code": row["major_code"],
                "industry_major_label": row["major_name"],
                "classification_code_tail_raw": row["classification_code_tail_raw"],
                "source_row_raw": row["company_and_classification_raw"],
                "source_page": row["page"],
                "parse_status": row["parse_status"],
            })
        sample = {"targets": targets[:15], "target_raw_rows": targeted[:20],
                  "first_pages_text": page_samples}
        CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
        checkpoint = {"schema": "aquant.capco_research_industry_checkpoint.v1",
            "capture_started_at": prepared, "article_receipt": article_receipt,
            "pdf_receipt": pdf_receipt, "guide_receipt": guide_receipt,
            "page_count": len(pdf_reader.pages),
            "raw_extracted_code_row_count": len(raw_rows), "target_count": len(targets),
            "target_code_rows_seen": len(targeted), "target_code_sample": sample,
            "target_category_counts": target_pairs, "missing_target_codes": missing,
            "duplicate_target_source_codes": sorted(duplicates),
            "conflicting_target_source_codes": sorted(conflicts)}
        with CHECKPOINT.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(checkpoint, ensure_ascii=False) + "\n")
        if args.probe_only:
            print(json.dumps({"article_receipt": article_receipt, "pdf_receipt": pdf_receipt,
                "guide_receipt": guide_receipt, "pdf_bytes": len(attachment_body), "pdf_pages": len(pdf_reader.pages),
                "raw_extracted_code_row_count": len(raw_rows), "target_count": len(targets),
                "target_code_rows_seen": len(targeted), "first_page_text_sample": page_samples[:1],
                "all_category_pairs": all_pairs, "target_category_counts": target_pairs,
                "missing_target_codes": missing, "duplicate_target_count": len(duplicates),
                "conflicting_target_count": len(conflicts), "target_row_sample": targeted[:8]}, ensure_ascii=False))
            return 0
        try:
            pub_date = re.search(r"发布时间：\s*(2026-09-30)", article_body.decode("utf-8", "replace"))
            publication_date = pub_date.group(1) if pub_date else None
            result = {
                "schema": "aquant.capco_research_industry_supplement.v1",
                "prepared_at": prepared,
                "captured_from": min(x["requested_at"] for x in (article_receipt, pdf_receipt, guide_receipt)
                                      if x.get("requested_at")),
                "captured_to": max(x["responded_at"] for x in (article_receipt, pdf_receipt, guide_receipt)
                                    if x.get("responded_at")),
                "generated_at": utc(),
                "capture_scope_note": "采集器观测范围；不是精确网络请求区间或分类生效时点。每个资源的真实请求/响应时间见收据。",
                "taxonomy": {
                    "publisher": "中国上市公司协会",
                    "taxonomy_title": "中国上市公司协会上市公司行业统计分类指引",
                    "taxonomy_publication_date": "2023-05-21",
                    "result_title": "2026年上半年上市公司行业分类结果（按股票代码排序）",
                    "reference_period": "2026年上半年",
                    "classification_asof_date": None,
                    "result_publication_date": publication_date,
                    "category_pairs_in_pdf": [{"code": c, "label": n} for c, n in all_pairs],
                    "financial_category": {"code": "J", "label": "金融业",
                        "basis": "分类结果PDF逐行列示的门类代码/简称；未将其他体系代码映射为本分类。"},
                    "classification_structure_note": "原PDF列名为门类代码/门类简称、次类代码/次类简称、大类代码/大类简称；仅提取原表内容，不推断CSRC行业代码。",
                    "guideline_text_excerpt": (guide_text[:6000] if guide_text else None),
                },
                "scope": {"official_universe_code_count": len(org_ids), "target_count": len(targets),
                    "matching_rule": "只按官方六位证券代码精确匹配；org_id仅核验/附注，别名不替代代码。",
                    "target_category_counts": target_pairs,
                    "matched_target_code_count": len(set(by_code) & target_set),
                    "missing_target_codes": missing,
                    "duplicate_target_source_codes": sorted(duplicates),
                    "conflicting_target_source_codes": sorted(conflicts),
                    "parse_status_counts": {status: sum(1 for r in targeted if r["parse_status"] == status)
                                             for status in sorted({r["parse_status"] for r in targeted})}},
                "sources": {"result_article": article_receipt,
                    "classification_pdf": {**pdf_receipt, "origin_url": attachment_url,
                        "page_count": len(pdf_reader.pages), "raw_extracted_code_row_count": len(raw_rows),
                        "unique_pdf_category_pairs": len(all_pairs)},
                    "guideline_article": {**guide_receipt,
                        "title": "中国上市公司协会上市公司行业统计分类指引",
                        "publication_date": "2023-05-21"}},
                "rows": rows_out,
                "unmatched_target_codes": missing,
                "duplicate_source_rows": duplicates,
                "conflicting_source_rows": conflicts,
            }
            json_write(OUTPUT, result)
            checkpoint["output_path"] = str(OUTPUT)
            checkpoint["output_row_count"] = len(rows_out)
            checkpoint["output_sha256"] = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
            with CHECKPOINT.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(checkpoint, ensure_ascii=False) + "\n")
            print(json.dumps({"output": str(OUTPUT), "rows": len(rows_out),
                "matched_target_codes": result["scope"]["matched_target_code_count"],
                "missing_target_codes": missing, "all_category_pairs": all_pairs,
                "target_category_counts": target_pairs, "pdf_sha256": pdf_receipt["content_hash"],
                "article_receipt_id": article_receipt["receipt_id"],
                "pdf_receipt_id": pdf_receipt["receipt_id"],
                "guide_receipt_id": guide_receipt.get("receipt_id")}, ensure_ascii=False))
        except Exception:
            raise
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
