"""Explicit current classification selection; no historical effective-date inference."""
from __future__ import annotations

import re
from collections import defaultdict

from aquant.operations.research_candidates import ResearchCandidateError

CAPCO = "CAPCO 上市公司行业统计分类"
CAPCO_VERSION = "2023指引 / 2026年上半年结果"


def _template(category):
    return "FINANCIAL" if category == "J" else "ORDINARY" if category in set("ABCDEFGHIJKLMNOPQRS") else "UNKNOWN"


def resolve_industries(primary, supplement=None):
    """Keep a nonempty primary classification; supplement only exact-code gaps.

    The two taxonomies remain distinct records. The publisher verifies the
    original bytes and identities before freezing these inputs.
    """
    result, grouped = {}, defaultdict(list)
    modern = primary.get("schema") == "aquant.baostock_research_industry.v1"
    for row in primary.get("rows", []) if modern else primary["records"]:
        code = (row.get("baostock_code_raw", "") if modern else row["code"])[-6:]
        label = (row.get("industry_raw") if modern else row.get("industry")) or ""
        asof = row.get("classification_asof_raw") if modern else row.get("updateDate")
        match = re.match(r"^([A-S])(\d{2})", label)
        category, major = (match[1], match[2]) if match else (None, None)
        record = {"system": "证监会行业分类", "version": f"BaoStock / updateDate {asof or '未知'}",
            "categoryCode": category, "categoryName": "金融业" if category == "J" else None,
            "majorCode": major, "majorName": label[3:] if match else None,
            "classificationAsOf": asof, "referencePeriod": None, "publicationDate": None,
            "observedAt": primary.get("captured_to"), "selection": "PRIMARY" if label else "MISSING",
            "template": _template(category) if category else "UNKNOWN", "label": label}
        receipt = primary.get("source_receipt")
        if modern and receipt:
            record["source"] = {"url": "baostock://query_stock_industry", "receiptId": None,
                "contentHash": receipt["content_hash"], "sourceRow":
                f"{row['baostock_code_raw']} | {row.get('code_name_raw', '')} | {label} | {asof}",
                "receiptLabel": receipt["label"], "storedPath": receipt["stored_path"]}
        grouped[code].append(record)
    for code, records in grouped.items():
        if len({(r["label"], r["classificationAsOf"]) for r in records}) != 1:
            raise ResearchCandidateError(f"conflicting primary industry rows: {code}")
        result[code] = records[0]
    if supplement is None:
        return result
    if supplement.get("schema") != "aquant.capco_research_industry_supplement.v1":
        raise ResearchCandidateError("unsupported industry supplement")
    taxonomy = supplement["taxonomy"]
    if (taxonomy["classification_asof_date"] is not None
            or taxonomy["financial_category"]["code"] != "J"
            or taxonomy["financial_category"]["label"] != "金融业"
            or taxonomy["reference_period"] != "2026年上半年"):
        raise ResearchCandidateError("unsupported CAPCO taxonomy or invented classification date")
    pairs = {r["code"]: r["label"] for r in taxonomy["category_pairs_in_pdf"]}
    pdf = supplement["sources"]["classification_pdf"]
    seen = set()
    for row in supplement["rows"]:
        code, category = row["official_code"], row["industry_category_code"]
        if code in seen:
            raise ResearchCandidateError("duplicate supplement industry identity")
        seen.add(code)
        if (row["parse_status"] != "parsed" or not category or _template(category) == "UNKNOWN"
                or pairs.get(category) != row["industry_category_label"]):
            raise ResearchCandidateError("unresolved supplement classification")
        parts = [category, row["industry_subcategory_code"], row["industry_major_code"]]
        record = {"system": CAPCO, "version": CAPCO_VERSION,
            "categoryCode": category, "categoryName": row["industry_category_label"],
            "subcategoryCode": row["industry_subcategory_code"], "subcategoryName": row["industry_subcategory_label"],
            "majorCode": row["industry_major_code"], "majorName": row["industry_major_label"],
            "classificationAsOf": None, "referencePeriod": taxonomy["reference_period"],
            "publicationDate": taxonomy["result_publication_date"], "observedAt": pdf["responded_at"],
            "selection": "OFFICIAL_SUPPLEMENT_FOR_PRIMARY_GAP", "template": _template(category),
            "label": "CAPCO " + " / ".join(p for p in parts if p) + " " + row["industry_major_label"],
            "source": {"url": pdf["url"], "receiptId": pdf["receipt_id"], "contentHash": pdf["content_hash"],
                "page": row["source_page"], "sourceRow": f"{code} {row['source_row_raw']}"}}
        if result.get(code, {}).get("label"):
            # A different taxonomy is an alternative observation, not an
            # undeclared conversion or an automatic conflict.
            result[code] = dict(result[code], alternatives=[record])
        else:
            result[code] = record
    return result


def resolve_disclosures(classifications, packet):
    """Admitted exact-code company disclosures retain their stated GB/T taxonomy."""
    if packet is None:
        return classifications
    if packet.get("schema") != "aquant.research_industry_missing.v1":
        raise ResearchCandidateError("unsupported company industry disclosure schema")
    result, seen = dict(classifications), set()
    for row in packet["rows"]:
        code, label = row["official_code"], row["industry_code_raw"]
        if (code in seen or not re.fullmatch(r"[A-S]\d{2}", label)
                or row["classification_system"] != "国民经济行业分类"
                or row["standard_id"] != "GB/T 4754-2017" or row["classification_asof_date"] is not None):
            raise ResearchCandidateError("unresolved or duplicate company industry classification")
        seen.add(code)
        receipts = [s for s in packet["sources"].values() if s.get("receipt_id") == row["pdf_receipt_id"]]
        if len(receipts) != 1:
            raise ResearchCandidateError("company industry PDF receipt is missing or ambiguous")
        evidence = next((r["excerpt"] for r in row["evidence_excerpts"] if r["field"] in {"所属行业名称", "上市公司行业分类"}
                         and r["page"] == row["industry_field_page"]), None)
        if evidence is None:
            raise ResearchCandidateError("company industry field locator is missing")
        record = {"system": "国民经济行业分类 / 公司披露", "version": "GB/T 4754-2017",
            "categoryCode": label[0], "categoryName": "金融业" if label[0] == "J" else None,
            "majorCode": label[1:], "majorName": row["industry_label_raw"],
            "classificationAsOf": None, "referencePeriod": None, "publicationDate": row["published_date"],
            "observedAt": receipts[0]["responded_at"], "selection": "OFFICIAL_DISCLOSURE_FOR_PRIMARY_GAP",
            "template": _template(label[0]), "label": f"GB/T 4754-2017 {label} {row['industry_label_raw']}",
            "source": {"url": row["document_url"], "receiptId": row["pdf_receipt_id"],
                "contentHash": row["pdf_content_hash"], "page": row["industry_field_page"], "sourceRow": evidence,
                "announcementId": row["announcement_id"], "indexReceiptId": row["index_receipt_id"],
                "indexContentHash": row["index_content_hash"], "queryComplete": row["query_complete"],
                "queryScope": "2025-12-31 至 2026-10-09 的有界公告检索，仅保留所列公司披露。检索完整性只涉及本次限定查询，不证明此后行业未变更。"}}
        if result.get(code, {}).get("label"):
            result[code] = dict(result[code], alternatives=result[code].get("alternatives", []) + [record])
        else:
            result[code] = record
    return result
