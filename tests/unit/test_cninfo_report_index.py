"""巨潮财报索引的请求范围、响应留证与分页完整性。"""

from __future__ import annotations

import io
import json
import sqlite3
import urllib.parse
from datetime import datetime, timezone

import pytest

from aquant.adapters.providers import cninfo
from aquant.adapters.providers.fetch_guard import FetchPolicy
from aquant.adapters.providers.resilience import RateLimiter, RetryPolicy
from aquant.domain.data.forward_archive import ForwardArchive


def _announcement(announcement_id: str | None, *, code: str = "600519",
                  title: str = "贵州茅台2026年半年度报告") -> dict:
    return {
        "announcementId": announcement_id,
        "secCode": code,
        "secName": "贵州茅台",
        "announcementTitle": title,
        "announcementTime": int(datetime(2026, 8, 14, 16,
                                         tzinfo=timezone.utc).timestamp() * 1000),
        "adjunctUrl": f"finalpage/2026-08-15/{announcement_id or 'missing'}.PDF",
    }


class _Response:
    def __init__(self, document: object) -> None:
        self._body = io.BytesIO(json.dumps(document, ensure_ascii=False).encode("utf-8"))
        self.headers = {"Content-Length": str(len(self._body.getvalue()))}
        self.status = 200

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        self._body.close()


class _Opener:
    def __init__(self, pages: list[dict], *, code: str = "600519",
                 org_id: str = "gssh0600519") -> None:
        self.pages = pages
        self.code = code
        self.org_id = org_id
        self.requests: list[tuple[str, dict[str, list[str]]]] = []

    def open(self, request, *, timeout: float) -> _Response:  # noqa: ANN001, ARG002
        url = request.full_url
        if "topSearch/query" in url:
            assert urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["keyWord"] == [
                self.code]
            return _Response([{"code": self.code, "orgId": self.org_id}])
        assert url == cninfo.REPORT_QUERY_URL
        params = urllib.parse.parse_qs(request.data.decode("utf-8"),
                                       keep_blank_values=True)
        self.requests.append((url, params))
        page = int(params["pageNum"][0])
        return _Response(self.pages[page - 1])


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path):  # noqa: ANN001
    con = sqlite3.connect(tmp_path / "archive.sqlite")
    con.row_factory = sqlite3.Row
    archive = ForwardArchive(con, tmp_path / "archive")
    client = cninfo.CninfoClient(
        archive,
        policy=FetchPolicy(resolve_dns=False,
                           allowed_hosts=frozenset({cninfo.QUERY_HOST,
                                                    cninfo.STATIC_HOST})),
        limiter=RateLimiter(min_interval_seconds=0),
        retry=RetryPolicy(max_attempts=1),
        sleep=lambda _seconds: None,
    )

    def install(pages: list[dict], *, code: str = "600519",
                org_id: str = "gssh0600519") -> _Opener:
        opener = _Opener(pages, code=code, org_id=org_id)
        monkeypatch.setattr(cninfo.urllib.request, "build_opener", lambda *_handlers: opener)
        return opener

    yield client, archive, install
    con.close()


def test_archived_index_exhausts_pages_and_binds_matches_to_receipts(harness) -> None:  # noqa: ANN001
    client, archive, install = harness
    opener = install([
        {"announcements": [_announcement("first"),
                           _announcement("foreign", code="601012")],
         "totalAnnouncement": 3, "hasMore": "true"},
        {"announcements": [_announcement("revised", title="贵州茅台2026年半年度报告（修订版）")],
         "totalAnnouncement": 3, "hasMore": "false"},
    ])

    result = client.report_index(stock_code="sh600519", report_period="2026-06-30",
                                 page_size=2, max_pages=2)

    assert result.complete and result.termination == "has_more_false"
    assert result.organization_id == "gssh0600519"
    assert result.skipped == {"different_security": 1}
    assert len(result.pages) == 2
    assert {item.announcement.announcement_id for item in result.matches} == {
        "first", "revised"}
    assert result.matches[0].announcement.is_revision
    assert result.matches[0].announcement.evidence_basis == "title_only"
    assert {item.page_num for item in result.matches} == {1, 2}
    assert archive.verify(result.org_lookup_content_hash)
    for page in result.pages:
        assert archive.verify(page.request_body_hash)
        assert archive.verify(page.content_hash)
        request = urllib.parse.parse_qs(
            archive.load_bytes(page.request_body_hash).decode("utf-8"))
        assert request["stock"] == ["600519,gssh0600519"]
        assert request["category"] == ["category_bndbg_szsh"]
        assert request["seDate"] == ["2026-06-30~2028-06-29"]
        assert request["pageNum"] == [str(page.page_num)]
        receipt = next(row for row in archive.receipts_for("cninfo")
                       if row["receipt_id"] == page.receipt_id)
        assert page.request_body_hash in receipt["detail"]
        assert receipt["content_hash"] == page.content_hash
    assert [request[1]["pageNum"] for request in opener.requests] == [["1"], ["2"]]


def test_max_pages_truncation_does_not_expose_candidates(harness) -> None:  # noqa: ANN001
    client, archive, install = harness
    install([{"announcements": [_announcement("first")],
             "totalAnnouncement": 2, "hasMore": True}])

    result = client.report_index(stock_code="600519", report_period="2026Q2",
                                 max_pages=1)

    assert not result.complete and result.termination == "max_pages_truncated"
    assert result.matches == result.announcements == ()
    assert len(result.pages) == 1
    assert archive.verify(result.pages[0].content_hash)


def test_missing_matching_announcement_id_fails_closed(harness) -> None:  # noqa: ANN001
    client, archive, install = harness
    install([{"announcements": [_announcement(None)],
             "totalAnnouncement": 1, "hasMore": False}])

    result = client.report_index(stock_code="600519", report_period="2026Q2")

    assert not result.complete and result.termination == "identity_incomplete"
    assert result.skipped == {"missing_announcement_id": 1}
    assert result.matches == result.announcements == ()
    assert archive.verify(result.pages[0].content_hash)


def test_total_without_has_more_can_prove_exhaustion(harness) -> None:  # noqa: ANN001
    client, _, install = harness
    install([{"announcements": [_announcement("first")],
             "totalAnnouncement": 1}])

    result = client.report_index(stock_code="600519", report_period="2026Q2")

    assert result.complete and result.termination == "total_reached"
    assert len(result.announcements) == 1


def test_conflicting_pagination_metadata_fails_closed(harness) -> None:  # noqa: ANN001
    client, _, install = harness
    install([{"announcements": [_announcement("first")],
             "totalAnnouncement": 2, "hasMore": "false"}])

    result = client.report_index(stock_code="600519", report_period="2026Q2")

    assert not result.complete and result.termination == "pagination_inconsistent"
    assert result.matches == result.announcements == ()


def test_numeric_cninfo_organization_id_is_accepted(harness) -> None:  # noqa: ANN001
    client, _, install = harness
    opener = install([{"announcements": [_announcement("first")],
                      "totalAnnouncement": 1, "hasMore": False}],
                     org_id="9900005965")

    result = client.report_index(stock_code="600519", report_period="2026Q2")

    assert result.complete
    assert result.organization_id == "9900005965"
    assert opener.requests[0][1]["stock"] == ["600519,9900005965"]


def test_duplicate_announcement_in_one_page_fails_closed(harness) -> None:  # noqa: ANN001
    client, _, install = harness
    install([{"announcements": [_announcement("first"), _announcement("first")],
             "totalAnnouncement": 2, "hasMore": False}])

    result = client.report_index(stock_code="600519", report_period="2026Q2")

    assert not result.complete and result.termination == "identity_incomplete"
    assert result.matches == result.announcements == ()


def test_cross_category_index_recalls_bare_correction_with_numeric_org_id(
        harness) -> None:  # noqa: ANN001
    client, archive, install = harness
    revised = _announcement("1223477802", code="601012",
                            title="2024年年度报告（修订版）")
    correction = _announcement("1223477804", code="601012", title="更正公告")
    unrelated = _announcement("other", code="601012", title="关于提供担保的公告")
    for row in (revised, correction, unrelated):
        row["announcementTime"] = int(datetime(2025, 5, 6, 16,
                                                tzinfo=timezone.utc).timestamp() * 1000)
    opener = install([
        {"announcements": [unrelated, correction], "totalAnnouncement": 3,
         "hasMore": "true"},
        {"announcements": [revised], "totalAnnouncement": 3,
         "hasMore": "false"},
    ], code="601012", org_id="9900022338")

    result = client.cross_category_index(
        stock_code="601012", report_period="2024-12-31",
        through=datetime(2025, 5, 10).date(), page_size=2, max_pages=2)

    assert result.complete and result.termination == "has_more_false"
    assert result.organization_id == "9900022338"
    assert len(result.announcements) == 3
    assert {item.announcement_id for item in result.candidates} == {
        "1223477802", "1223477804"}
    correction_hit = next(item for item in result.candidates
                          if item.announcement_id == "1223477804")
    assert correction_hit.candidate_reasons == ("correction_title",)
    assert correction_hit.page_receipt_id == result.pages[0].receipt_id
    assert archive.verify(correction_hit.page_content_hash)
    for page in result.pages:
        assert archive.verify(page.request_body_hash)
        body = urllib.parse.parse_qs(
            archive.load_bytes(page.request_body_hash).decode("utf-8"),
            keep_blank_values=True)
        assert body["stock"] == ["601012,9900022338"]
        assert body["category"] == [""]
        assert body["searchkey"] == [""]
        assert body["seDate"] == ["2024-12-31~2025-05-10"]
    assert len(opener.requests) == 2


def test_cross_category_max_pages_truncation_hides_all_titles(harness) -> None:  # noqa: ANN001
    client, archive, install = harness
    correction = _announcement("1223477804", code="601012", title="更正公告")
    correction["announcementTime"] = int(datetime(
        2025, 5, 6, 16, tzinfo=timezone.utc).timestamp() * 1000)
    install([{"announcements": [correction], "totalAnnouncement": 2,
             "hasMore": True}], code="601012", org_id="9900022338")

    result = client.cross_category_index(
        stock_code="601012", report_period="2024Q4",
        through=datetime(2025, 5, 10).date(), max_pages=1)

    assert not result.complete and result.termination == "max_pages_truncated"
    assert result.announcements == result.candidates == ()
    assert archive.verify(result.pages[0].content_hash)


def test_cross_category_missing_announcement_id_fails_closed(harness) -> None:  # noqa: ANN001
    client, _, install = harness
    missing = _announcement(None, code="601012", title="更正公告")
    missing["announcementTime"] = int(datetime(
        2025, 5, 6, 16, tzinfo=timezone.utc).timestamp() * 1000)
    install([{"announcements": [missing], "totalAnnouncement": 1,
             "hasMore": False}], code="601012", org_id="9900022338")

    result = client.cross_category_index(
        stock_code="601012", report_period="2024Q4",
        through=datetime(2025, 5, 10).date())

    assert not result.complete and result.termination == "identity_incomplete"
    assert result.announcements == result.candidates == ()
