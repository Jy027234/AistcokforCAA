"""Admit the bounded CAPCO classification supplement from original local bytes."""
from __future__ import annotations

from collections import Counter, defaultdict
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urljoin

from capture_research_official_industry import ARTICLE_URL, GUIDE_URL, INDUSTRY, QUOTES, extract_rows
from verify_research_market_inputs import _file, _hash, _require, _time, _integer

SOURCE_ID = "capco-industry-classification"
TITLE = "中国上市公司协会上市公司行业统计分类指引"
RESULT_TITLE = "2026年上半年上市公司行业分类结果（按股票代码排序）"


class _Article(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links = [], []
        self.active = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            values = dict(attrs)
            self.active = {"href": values.get("href"), "title": values.get("title", ""), "text": ""}

    def handle_data(self, data):
        value = re.sub(r"\s+", " ", data).strip()
        if value:
            self.parts.append(value)
        if self.active is not None:
            self.active["text"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.active is not None:
            self.links.append(self.active)
            self.active = None


def _html(body):
    parsed = _Article()
    parsed.feed(body.decode("utf-8"))
    return parsed


def _publication(parsed):
    match = re.search(r"发布时间：\s*(\d{4}-\d{2}-\d{2})", " ".join(parsed.parts))
    _require(match is not None, "official article publication date missing")
    return match[1]


def _targets(quotes, industry, universe):
    classified = {r["code"] for r in industry["rows"] if r.get("classification_status") == "classified"}
    targets = set()
    for row in quotes["rows"]:
        code = row["official_code"]
        if code in classified or not row.get("close_time_eligible"):
            continue
        volume, amount = _integer(row.get("volume_raw")), _integer(row.get("turnover_amount_raw"))
        if volume is not None and volume > 0 and amount is not None and amount > 0:
            _require(code in universe, "industry target code absent from official universe")
            targets.add(code)
    return targets


def verify_industry_supplement(packet, universe, archive_root, *, target_quotes=None, target_industry=None):
    """Check source receipts, taxonomy, PDF rows and exact original target coverage.

    The optional target packets let the publisher use its already admitted inputs.
    CAPCO is its own documented taxonomy; it is not relabelled as CSRC.
    """
    _require(packet.get("schema") == "aquant.capco_research_industry_supplement.v1", "CAPCO supplement schema mismatch")
    root = Path(archive_root)
    sources = packet.get("sources", {})
    _require(set(sources) == {"result_article", "classification_pdf", "guideline_article"}, "CAPCO three required sources mismatch")
    bodies, requested, responded, receipt_ids = {}, [], [], set()
    with sqlite3.connect((root / "meta.sqlite").resolve().as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        for role, metadata in sources.items():
            rid = metadata.get("receipt_id")
            _require(rid not in receipt_ids, "CAPCO source receipt reused across resources")
            receipt_ids.add(rid)
            row = con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?", (rid,)).fetchone()
            _require(row is not None and row["source_id"] == SOURCE_ID and row["outcome"] == "OK"
                     and row["http_status"] == 200, "CAPCO source receipt outcome/source/HTTP mismatch")
            for key in ("url", "requested_at", "responded_at", "http_status", "content_hash", "byte_size"):
                _require(metadata.get(key) == row[key], f"CAPCO {role} {key} differs from archive receipt")
            req, resp, seen = (_time(row[k]) for k in ("requested_at", "responded_at", "first_seen_at"))
            _require(req <= resp and seen == resp, "CAPCO source receipt timestamps invalid")
            artifact = con.execute("SELECT * FROM raw_artifact WHERE content_hash=?", (row["content_hash"],)).fetchone()
            _require(artifact is not None, "CAPCO source original artifact missing")
            body = _file(root, artifact["stored_path"]).read_bytes()
            _require(_hash(body) == row["content_hash"] and len(body) == row["byte_size"] == artifact["byte_size"],
                     "CAPCO source original hash/byte count mismatch")
            bodies[role] = body
            requested.append(req)
            responded.append(resp)
    _require(_time(packet.get("captured_from")) == min(requested) and _time(packet.get("captured_to")) == max(responded),
             "CAPCO capture bounds differ from actual three receipts")
    _require(_time(packet.get("generated_at")) >= max(responded), "CAPCO generation precedes captured evidence")
    _require(sources["result_article"]["url"] == ARTICLE_URL and sources["guideline_article"]["url"] == GUIDE_URL,
             "CAPCO official result/guideline URL mismatch")
    article, guideline = _html(bodies["result_article"]), _html(bodies["guideline_article"])
    attachment = [link for link in article.links if "按股票代码排序" in (link["text"] + " " + (link["title"] or ""))]
    _require(len(attachment) == 1 and attachment[0]["href"], "CAPCO code-sorted attachment href missing/ambiguous")
    pdf_url = urljoin(ARTICLE_URL, attachment[0]["href"])
    _require(sources["classification_pdf"]["url"] == pdf_url
             and sources["classification_pdf"].get("origin_url") == pdf_url, "CAPCO PDF differs from actual article attachment")
    _require(_publication(article) == "2026-09-30" and _publication(guideline) == "2023-05-21", "CAPCO original publication dates mismatch")
    guide_text = " ".join(guideline.parts)
    title_at = guide_text.rfind(TITLE)
    _require(title_at >= 0 and "《国民经济行业分类》" in guide_text and "GB" in guide_text and "4754-2017" in guide_text,
             "CAPCO classification guideline identity/basis missing")
    taxonomy = packet.get("taxonomy", {})
    for key, expected in {"publisher": "中国上市公司协会", "taxonomy_title": TITLE,
                          "taxonomy_publication_date": "2023-05-21", "result_title": RESULT_TITLE,
                          "reference_period": "2026年上半年", "result_publication_date": "2026-09-30"}.items():
        _require(taxonomy.get(key) == expected, f"CAPCO taxonomy {key} differs from original documents")
    _require("classification_asof_date" in taxonomy and taxonomy["classification_asof_date"] is None,
             "CAPCO reference period/publication cannot become historical effective date")
    _require(taxonomy.get("guideline_text_excerpt") == guide_text[title_at:][:6000], "CAPCO guideline excerpt differs from original HTML")
    _require(sources["guideline_article"].get("title") == TITLE
             and sources["guideline_article"].get("publication_date") == "2023-05-21", "CAPCO guideline source metadata mismatch")
    body = bodies["classification_pdf"]
    _require(body.startswith(b"%PDF"), "CAPCO original classification attachment is not PDF")
    raw_rows, samples = extract_rows(body)
    _require("2026年上半年" in re.sub(r"\s+", "", " ".join(samples)), "CAPCO PDF reference period mismatch")
    from pypdf import PdfReader
    pdf_metadata = sources["classification_pdf"]
    pairs = sorted({(r["category_code"], r["category_name"]) for r in raw_rows if r.get("category_code") and r.get("category_name")})
    _require(pdf_metadata.get("page_count") == len(PdfReader(io.BytesIO(body)).pages)
             and pdf_metadata.get("raw_extracted_code_row_count") == len(raw_rows)
             and pdf_metadata.get("unique_pdf_category_pairs") == len(pairs), "CAPCO original PDF page/row/category statistics mismatch")
    _require(taxonomy.get("category_pairs_in_pdf") == [{"code": c, "label": n} for c, n in pairs], "CAPCO PDF category pairs altered")
    financial = taxonomy.get("financial_category", {})
    _require(financial.get("code") == "J" and financial.get("label") == "金融业" and ("J", "金融业") in pairs,
             "CAPCO exact financial J mapping not established by original PDF")
    quotes = target_quotes if target_quotes is not None else json.loads(QUOTES.read_text(encoding="utf-8"))
    industry = target_industry if target_industry is not None else json.loads(INDUSTRY.read_text(encoding="utf-8"))
    targets = _targets(quotes, industry, universe)
    targeted = [r for r in raw_rows if r["code_raw"] in targets]
    grouped = defaultdict(list)
    for r in targeted:
        grouped[r["code_raw"]].append(r)
    missing = sorted(targets - set(grouped))
    duplicates = {code: rows for code, rows in grouped.items() if len(rows) != 1}
    conflicts = {code: rows for code, rows in grouped.items()
                 if len({(r.get("category_code"), r.get("subcategory_code"), r.get("major_code")) for r in rows}) > 1}
    expected_rows = []
    for r in targeted:
        code = r["code_raw"]
        expected_rows.append({"official_code": code, "org_id": universe[code],
            "same_org_official_codes_annotation_only": sorted(c for c, org in universe.items() if org == universe[code] and c != code),
            "company_name_raw": r["company_name_raw"], "industry_category_code": r["category_code"],
            "industry_category_label": r["category_name"], "industry_subcategory_code": r["subcategory_code"],
            "industry_subcategory_label": r["subcategory_name"], "industry_major_code": r["major_code"],
            "industry_major_label": r["major_name"], "classification_code_tail_raw": r["classification_code_tail_raw"],
            "source_row_raw": r["company_and_classification_raw"], "source_page": r["page"], "parse_status": r["parse_status"]})
    _require(packet.get("rows") == expected_rows, "CAPCO target rows differ from original PDF/code/orgId/page")
    scope = packet.get("scope", {})
    category_counts = Counter(f'{r["category_code"]} {r["category_name"]}' for r in targeted if r.get("category_code") and r.get("category_name"))
    expected_scope = {"official_universe_code_count": len(universe), "target_count": len(targets),
                      "target_category_counts": dict(category_counts), "matched_target_code_count": len(grouped),
                      "missing_target_codes": missing, "duplicate_target_source_codes": sorted(duplicates),
                      "conflicting_target_source_codes": sorted(conflicts), "parse_status_counts": dict(Counter(r["parse_status"] for r in targeted))}
    for key, value in expected_scope.items():
        _require(scope.get(key) == value, f"CAPCO {key} differs from original target/PDF coverage")
    _require(packet.get("unmatched_target_codes") == missing and packet.get("duplicate_source_rows") == duplicates
             and packet.get("conflicting_source_rows") == conflicts, "CAPCO missing/duplicate/conflict partitions altered")
