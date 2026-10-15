"""Research time/scope claims must remain tied to original collection receipts."""
from copy import deepcopy
from pathlib import Path

import pytest


@pytest.fixture
def evidence(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "tools"))
    import capture_research_announcements as capture
    from publish_research_scan import verify_announcements

    start, middle, end = "2026-10-09T04:01:00+00:00", "2026-10-09T04:02:00+00:00", "2026-10-09T04:03:00+00:00"
    page = {"page": 1, "indexReceiptId": "index", "indexContentHash": "indexhash",
            "requestBodyHash": "requesthash", "indexUrl": capture.REPORT_QUERY_URL,
            "requestedAt": start, "respondedAt": middle}
    doc = {"code": "600001", "announcementId": "ann", "title": "公告", "publishedDate": "2026-09-22",
           "url": "https://static.cninfo.com.cn/f.PDF", "indexReceiptId": "index", "indexContentHash": "indexhash",
           "pdfReceiptId": "pdf", "pdfContentHash": "pdfhash", "pdfRequestedAt": middle,
           "pdfRespondedAt": end, "pdfFirstSeenAt": end, "extractedText": "公告事实", "pages": [], "pageCount": 1}
    packet = {"schema": "aquant.cninfo_research_announcements.v1", "target_codes": ["600001"],
              "targetCount": 1, "queryCompleteCount": 1, "capturedDocumentCount": 1,
              "window": {"start": "2026-09-09", "end": "2026-10-09"}, "captured_from": start, "captured_to": end,
              "per_code": [{"code": "600001", "orgId": "org", "window": ["2026-09-09", "2026-10-09"],
                            "queryComplete": True, "rawAnnouncementCount": 1,
                            "totalAnnouncement": 1, "pages": [page]}], "documents": [doc]}
    receipts = {"index": {"url": capture.REPORT_QUERY_URL, "requested_at": start, "responded_at": middle},
                "pdf": {"source_id": "cninfo", "url": doc["url"], "outcome": "OK", "http_status": 200,
                        "content_hash": "pdfhash", "byte_size": 5, "requested_at": middle,
                        "responded_at": end, "first_seen_at": end}}
    class Connection:
        def execute(self, sql, args):
            class Cursor:
                def fetchone(self):
                    return receipts.get(args[0])
            return Cursor()
    class Archive:
        def verify(self, digest):
            return digest == "pdfhash"
        def load_bytes(self, digest):
            return b"%PDF-"
    monkeypatch.setattr(capture, "parse_pages", lambda *a, **kw: {"complete": True,
        "raw_seen": 1, "total_announcement": 1, "announcements": [{
        "announcement_id": "ann", "url": doc["url"], "title": doc["title"], "published_date": doc["publishedDate"],
        "index_receipt_id": "index", "index_content_hash": "indexhash"}]})
    monkeypatch.setattr(capture, "extract_pdf_text", lambda b: ("公告事实", [], 1))
    return packet, lambda value: verify_announcements(value, {"600001": "org"}, Archive(), Connection())


def test_admitted_scope_and_time_are_from_real_receipts(evidence):
    packet, verify = evidence
    verify(packet)


@pytest.mark.parametrize("field,value", [("target_codes", ["600002"]),
    ("queryCompleteCount", 0), ("capturedDocumentCount", 0), ("stopOnBreaker", True)])
def test_incomplete_or_misdeclared_announcement_packet_is_not_admitted(evidence, field, value):
    packet, verify = evidence
    changed = deepcopy(packet)
    changed[field] = value
    with pytest.raises(ValueError):
        verify(changed)


@pytest.mark.parametrize("target,key", [("packet", "captured_from"), ("packet", "captured_to"),
    ("page", "requestedAt"), ("page", "respondedAt"), ("document", "pdfRequestedAt"),
    ("document", "pdfRespondedAt"), ("document", "pdfFirstSeenAt"), ("window", "start"), ("query", "window")])
def test_time_or_scope_cannot_be_extended_without_collection_evidence(evidence, target, key):
    packet, verify = evidence
    changed = deepcopy(packet)
    row = {"packet": changed, "page": changed["per_code"][0]["pages"][0],
           "document": changed["documents"][0], "window": changed["window"], "query": changed["per_code"][0]}[target]
    row[key] = ["2026-08-09", "2026-10-09"] if key == "window" else "2026-10-10T00:00:00+00:00"
    with pytest.raises(ValueError):
        verify(changed)
