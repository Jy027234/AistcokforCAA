"""Configurable date scope for official announcement capture."""
import json
from types import SimpleNamespace
from datetime import date
from urllib.parse import parse_qs

import pytest


@pytest.fixture
def capture(monkeypatch):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "tools"))
    import capture_research_announcements as module
    return module


def test_request_body_uses_explicit_inclusive_window(capture):
    body = capture.request_body("600001", "org-1", "sh", 2,
                                start=date(2026, 9, 11), end=date(2026, 10, 11))
    query = parse_qs(body.decode("utf-8"))
    assert query["seDate"] == ["2026-09-11~2026-10-11"]
    assert query["pageNum"] == ["2"]


def test_parse_pages_accepts_zero_announcements_for_requested_window(capture):
    start, end = date(2026, 9, 11), date(2026, 10, 11)
    body = capture.request_body("600001", "org-1", "sh", 1, start=start, end=end)
    payload = json.dumps({"announcements": [], "totalAnnouncement": 0, "hasMore": False}).encode()
    class Archive:
        def __init__(self):
            self.data = {"request-hash": body, "response-hash": payload}
            self.con = self
        def execute(self, sql, args):
            assert args == ("receipt-1",)
            return self
        def fetchone(self):
            return {"source_id": "cninfo", "url": capture.REPORT_QUERY_URL, "outcome": "OK",
                    "http_status": 200, "content_hash": "response-hash", "byte_size": len(payload)}
        def verify(self, digest):
            return digest in self.data
        def load_bytes(self, digest):
            return self.data[digest]
    page = {"page": 1, "receipt_id": "receipt-1", "content_hash": "response-hash",
            "request_body_hash": "request-hash"}

    result = capture.parse_pages("600001", "org-1", [page], Archive(), start=start, end=end)

    assert result["complete"] is True
    assert result["announcements"] == []
    assert result["raw_seen"] == 0


def test_parse_pages_rejects_checkpoint_for_another_window(capture):
    start, end = date(2026, 9, 11), date(2026, 10, 11)
    body = capture.request_body("600001", "org-1", "sh", 1)
    class Archive:
        con = None
        def verify(self, digest):
            return True
        def load_bytes(self, digest):
            if digest == "request-hash":
                return body
            return b"{}"
    class Connection:
        def execute(self, *args):
            class Cursor:
                def fetchone(self):
                    return {"source_id": "cninfo", "url": capture.REPORT_QUERY_URL, "outcome": "OK",
                            "http_status": 200, "content_hash": "response-hash", "byte_size": 2}
            return Cursor()
    archive = Archive()
    archive.con = Connection()
    result = capture.parse_pages("600001", "org-1", [{"page": 1, "receipt_id": "r",
        "content_hash": "response-hash", "request_body_hash": "request-hash"}], archive,
        start=start, end=end)
    assert result["complete"] is False
    assert result["termination"] == "query_identity_invalid"


def test_index_checkpoint_window_must_match(capture):
    class Archive:
        pass
    item = {"code": "600001", "org_id": "org-1", "query_window": ["2026-09-09", "2026-10-09"]}
    result = capture.verify_index_checkpoint(item,
        {"code": "600001", "org_id": "org-1"}, Archive(),
        start=date(2026, 9, 11), end=date(2026, 10, 11))
    assert result is None


def test_run_index_preserves_explicit_fetch_denial(capture):
    detail = "blocked-address: www.cninfo.com.cn resolves to 2001:2::199 (private address)"
    class Cursor:
        def fetchone(self):
            return {"receipt_id": "receipt-denied", "url": capture.REPORT_QUERY_URL,
                    "requested_at": "2026-10-11T15:20:00+00:00",
                    "responded_at": "2026-10-11T15:20:00+00:00", "first_seen_at": None,
                    "outcome": "DENIED", "http_status": None, "content_hash": None,
                    "byte_size": None, "detail": detail}
    class Archive:
        con = SimpleNamespace(execute=lambda *args: Cursor())
        def store_bytes(self, *args, **kwargs):
            return "sha256:request", 10
    class Client:
        breaker = SimpleNamespace(is_open=False)
        def _fetch(self, *args, **kwargs):
            return SimpleNamespace(ok=False, payload=None, content_hash=None,
                                   receipt_id="receipt-denied", http_status=None, detail=detail)
    result = capture.run_index({"code": "600001", "org_id": "org-1", "column": "sh"},
                               Client(), Archive(), start=date(2026, 9, 11), end=date(2026, 10, 11))
    assert result["complete"] is False
    assert result["termination"] == "fetch_failed"
    assert result["error"] == detail
    assert result["pages"][0]["failure_detail"] == detail
