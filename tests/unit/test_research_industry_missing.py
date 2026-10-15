"""Bounded disclosure evidence stays tied to requests, indexes and original PDFs."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlencode

import pytest

from aquant.domain.data.forward_archive import ForwardArchive


@pytest.fixture
def disclosure(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "tools"))
    import verify_research_industry_missing as verify
    import capture_research_industry_missing as collector
    # Exercise the real field/parser contract, replacing only PDF transport.
    pages = ["公司全称 测试股份有限公司 证券简称 测试 证券代码 920001\n"
             "所属行业名称 专用设备制造业 所属行业代码 C35\n国民经济行业分类 GB/T4754-2017"]
    monkeypatch.setattr(collector, "text_pages", lambda body: pages if body == b"%PDF-fixture" else [])
    con = sqlite3.connect(tmp_path / "meta.sqlite")
    archive = ForwardArchive(con, tmp_path)
    def source(body, url, number, detail=None):
        digest, _ = archive.store_bytes(body)
        requested, responded = ("2026-10-02T12:00:00+00:00", "2026-10-02T12:00:01+00:00") if number == 0 else (f"2026-10-09T13:0{number}:00+00:00", f"2026-10-09T13:0{number}:01+00:00")
        receipt = archive.record(source_id="cninfo", url=url, outcome="OK", http_status=200,
            requested_at=datetime.fromisoformat(requested), responded_at=datetime.fromisoformat(responded),
            content_hash=digest, byte_size=len(body), detail=detail)
        return {"receipt_id": receipt.receipt_id, "source_id": "cninfo", "url": url, "outcome": "OK",
            "requested_at": requested, "responded_at": responded, "http_status": 200,
            "content_hash": digest, "byte_size": len(body), "detail": detail}
    org = source(json.dumps({"stockList": [{"code": "920001", "orgId": "org1"}]}).encode(),
                 "http://www.cninfo.com.cn/new/data/szse_stock.json", 0)
    # The historical identity map is deliberately outside this capture window.
    identity = {"receipt_id": org["receipt_id"], "source_url": org["url"], "content_hash": org["content_hash"], "verified": True}
    request = {"stock": "920001,org1", "seDate": "2025-12-31~2026-10-09", "column": "bj",
               "tabName": "fulltext", "category": "", "pageNum": "1", "pageSize": "30", "searchkey": "发行公告"}
    request_hash, _ = archive.store_bytes(urlencode(request).encode())
    raw = {"secCode": "920001", "orgId": "org1", "secName": "测试", "announcementId": "123456",
           "announcementTitle": "上市发行公告", "announcementTime": 1789056000000,
           "adjunctUrl": "finalpage/2026-09-11/123456.PDF"}
    index = source(json.dumps({"announcements": [raw], "totalAnnouncement": 1, "hasMore": False}).encode(),
                   verify.REPORT_QUERY_URL, 1, f"research-industry-missing-keyword:920001:发行公告:request={request_hash}")
    index["request_body_hash"] = request_hash
    pdf = source(b"%PDF-fixture", "https://static.cninfo.com.cn/" + raw["adjunctUrl"], 2)
    con.close()
    doc = {"announcement_id": "123456", "security_code": "920001", "response_org_id": "org1",
           "response_security_name_raw": "测试", "title": "上市发行公告", "published_at_ms": raw["announcementTime"],
           "published_date": datetime.fromtimestamp(raw["announcementTime"] / 1000, verify.BEIJING).date().isoformat(),
           "document_url": pdf["url"], "index_page": 1, "index_receipt_id": index["receipt_id"], "index_content_hash": index["content_hash"]}
    ref = {"keyword": "发行公告", **index, "total_announcement": 1, "raw_row_count": 1, "has_more": False,
           "termination": "hasMore_false_or_total_reached", "identity_errors": [], "error": None}
    parsed = collector.parse_industry_pdf(b"%PDF-fixture", code="920001", expected_name="测试")
    row = collector.make_classification_row(code="920001", org_id="org1", quote_name="测试", doc=doc,
        pdf_receipt=pdf, parsed=parsed, query_complete=True, index_count=1, index_receipts=[ref], index_reused=False)
    packet = {"schema": "aquant.research_industry_missing.v1", "requested_codes": ["920001"],
        "scope": {"base_missing_count": 1, "query_window": {"from": "2025-12-31", "to": "2026-10-09"},
                  "index_page_size": 30, "index_max_pages_per_code": 1, "max_pdfs_per_code": 2,
                  "qualified_count": 1, "unresolved_count": 0},
        "sources": {"official_universe_org_map": identity, "index_920001_发行公告": index, "pdf_920001_123456": pdf},
        "rows": [row], "unresolved": [], "captured_from": index["requested_at"], "captured_to": pdf["responded_at"],
        "generated_at": "2026-10-09T13:03:00+00:00"}
    return verify, packet, {"920001": "org1"}, tmp_path, {"scope": {"missing_target_codes": ["920001", "920002"]}}, {"rows": [{"official_code": "920001", "display_name": "测试"}]}, pages


def admit(fixture, packet=None):
    verify, original, universe, root, base, quotes, _ = fixture
    verify.verify_industry_missing(packet or original, universe, root, base_supplement=base, target_quotes=quotes)


def test_exact_subset_document_is_admitted_and_old_identity_time_is_separate(disclosure):
    admit(disclosure)


@pytest.mark.parametrize("fault", ["target", "org", "name", "count", "coverage", "duplicate", "effective_date", "standard", "field", "page", "excerpt", "issuer", "quote_match", "pubdate", "document_url", "index_org", "index_name", "index_hash", "pdf_hash", "time", "bytes", "url", "source", "request_hash", "capture_from", "capture_to", "completeness", "partial", "ann_count", "original"])
def test_forged_disclosure_claim_is_rejected(disclosure, fault):
    packet = deepcopy(disclosure[1])
    row = packet["rows"][0]
    if fault == "target":
        packet["requested_codes"] = ["920003"]
    elif fault == "count":
        packet["scope"]["qualified_count"] = 0
    elif fault == "coverage":
        packet["rows"] = []
    elif fault == "duplicate":
        packet["rows"].append(deepcopy(row))
    elif fault in ("org", "name", "effective_date", "standard", "field", "page", "excerpt", "issuer", "quote_match", "pubdate", "document_url", "index_org", "index_name", "index_hash", "pdf_hash", "completeness", "partial", "ann_count"):
        key, value = {"org": ("org_id", "bad"), "name": ("display_name_from_quote_packet", "bad"),
            "effective_date": ("classification_asof_date", "2026-09-11"), "standard": ("standard_id", "CSRC"),
            "field": ("industry_code_raw", "J66"), "page": ("industry_field_page", 2), "excerpt": ("evidence_excerpts", []),
            "issuer": ("issuer_name_raw", "wrong issuer"), "quote_match": ("quote_name_exact_substring_match", False),
            "pubdate": ("published_date", "2026-10-08"), "document_url": ("document_url", "https://bad/"),
            "index_org": ("index_response_org_id", "wrong"), "index_name": ("index_response_security_name_raw", "wrong"),
            "index_hash": ("index_content_hash", "wrong"), "pdf_hash": ("pdf_content_hash", "wrong"),
            "completeness": ("query_complete", False), "partial": ("index_partial_page_only", True),
            "ann_count": ("index_announcement_count", 2)}[fault]
        row[key] = value
    elif fault in ("time", "bytes", "url", "source"):
        key, value = {"time": ("responded_at", "2026-10-09T13:02:02+00:00"), "bytes": ("byte_size", 1),
                      "url": ("url", "https://bad/"), "source": ("source_id", "other")}[fault]
        packet["sources"]["pdf_920001_123456"][key] = value
    elif fault == "request_hash":
        packet["sources"]["index_920001_发行公告"]["request_body_hash"] = packet["sources"]["pdf_920001_123456"]["content_hash"]
    elif fault in ("capture_from", "capture_to"):
        packet["captured_from" if fault == "capture_from" else "captured_to"] = "2026-10-02T12:00:00+00:00"
    else:
        digest = packet["sources"]["pdf_920001_123456"]["content_hash"][7:]
        (disclosure[3] / "raw" / digest[:2] / digest).write_bytes(b"changed")
    with pytest.raises((ValueError, RuntimeError)):
        admit(disclosure, packet)


def replace_index(disclosure, packet, raw_update=None, request_update=None, *, more=False, total=1):
    verify, _, _, root, _, _, _ = disclosure
    source = packet["sources"]["index_920001_发行公告"]
    with sqlite3.connect(root / "meta.sqlite") as con:
        con.row_factory = sqlite3.Row
        archive = ForwardArchive(con, root)
        body = archive.load_bytes(source["content_hash"])
        payload = json.loads(body)
        if raw_update:
            payload["announcements"][0].update(raw_update)
        payload.update(hasMore=more, totalAnnouncement=total)
        body = json.dumps(payload).encode()
        digest, _ = archive.store_bytes(body)
        source.update(content_hash=digest, byte_size=len(body))
        if request_update:
            from urllib.parse import parse_qs
            request = {k: v[0] for k, v in parse_qs(archive.load_bytes(source["request_body_hash"]).decode(), keep_blank_values=True).items()}
            request.update(request_update)
            request_hash, _ = archive.store_bytes(urlencode(request).encode())
            source["request_body_hash"] = request_hash
            source["detail"] = "fixture:request=" + request_hash
        source.update(requested_at="2026-10-09T13:01:01.500000+00:00", responded_at="2026-10-09T13:01:01.750000+00:00")
        receipt = archive.record(source_id="cninfo", url=source["url"], outcome="OK", http_status=200,
            requested_at=datetime.fromisoformat(source["requested_at"]), responded_at=datetime.fromisoformat(source["responded_at"]),
            content_hash=digest, byte_size=len(body), detail=source["detail"])
        source["receipt_id"] = receipt.receipt_id
    ref = packet["rows"][0]["index_receipts"][0]
    ref.update(source, total_announcement=total, has_more=more,
               termination="single_page_only" if more and total > 1 else "hasMore_false_or_total_reached")
    packet["rows"][0]["index_content_hash"] = source["content_hash"]
    packet["rows"][0]["index_receipt_id"] = source["receipt_id"]
    packet["captured_from"] = source["requested_at"]


def test_incomplete_query_can_admit_proven_single_document_without_complete_claim(disclosure):
    packet = deepcopy(disclosure[1])
    replace_index(disclosure, packet, more=True, total=60)
    packet["rows"][0].update(query_complete=False, index_partial_page_only=True)
    admit(disclosure, packet)
    packet["rows"][0].update(query_complete=True, index_partial_page_only=False)
    with pytest.raises(ValueError, match="completeness"):
        admit(disclosure, packet)


@pytest.mark.parametrize("change", [{"stock": "920002,org1"}, {"stock": "920001,other"}, {"seDate": "2026-10-09~2026-10-09"}])
def test_hash_valid_request_with_wrong_security_org_or_window_is_rejected(disclosure, change):
    packet = deepcopy(disclosure[1])
    replace_index(disclosure, packet, request_update=change)
    with pytest.raises(ValueError, match="identity/window"):
        admit(disclosure, packet)


@pytest.mark.parametrize("change", [{"secCode": "920002"}, {"orgId": "other"}, {"adjunctUrl": "finalpage/2026-09-11/other.PDF"}, {"announcementTime": 0}])
def test_hash_valid_index_cannot_substitute_document_identity(disclosure, change):
    packet = deepcopy(disclosure[1])
    replace_index(disclosure, packet, raw_update=change)
    with pytest.raises(ValueError):
        admit(disclosure, packet)


def test_unresolved_partition_is_bound_to_unqualified_original(disclosure):
    verify, original, _, _, _, _, pages = disclosure
    packet = deepcopy(original)
    pages[0] = pages[0].replace("GB/T4754-2017", "")
    import capture_research_industry_missing as collector
    parsed = collector.parse_industry_pdf(b"%PDF-fixture", code="920001", expected_name="测试")
    row = packet["rows"][0]
    doc = {"announcement_id": row["announcement_id"], "security_code": "920001", "response_org_id": "org1",
           "response_security_name_raw": "测试", "title": row["announcement_title"], "published_at_ms": 1789056000000,
           "published_date": row["published_date"], "document_url": row["document_url"], "index_page": 1,
           "index_receipt_id": row["index_receipt_id"], "index_content_hash": row["index_content_hash"]}
    packet["unresolved"] = [{"official_code": "920001", "org_id": "org1", "display_name": "测试",
        "query_complete": True, "index_partial_page_only": False, "index_announcement_count": 1,
        "index_receipts": row["index_receipts"], "documents_examined": [{"announcement": doc,
        "pdf_receipt": packet["sources"]["pdf_920001_123456"], "extraction_status": "unqualified", **parsed}]}]
    packet["rows"] = []
    packet["scope"].update(qualified_count=0, unresolved_count=1)
    admit(disclosure, packet)
    packet["unresolved"][0]["documents_examined"][0]["industry"]["page"] = 9
    with pytest.raises(ValueError, match="original industry"):
        admit(disclosure, packet)


@pytest.mark.parametrize("fault", ["pdf_role", "query_role", "missing_reference_time"])
def test_source_roles_and_referenced_receipt_metadata_are_exact(disclosure, fault):
    packet = deepcopy(disclosure[1])
    if fault == "pdf_role":
        packet["sources"]["pdf_920001_654321"] = packet["sources"].pop("pdf_920001_123456")
    elif fault == "query_role":
        packet["sources"]["index_920001_招股说明书"] = packet["sources"].pop("index_920001_发行公告")
    else:
        del packet["rows"][0]["index_receipts"][0]["responded_at"]
    with pytest.raises(ValueError):
        admit(disclosure, packet)
