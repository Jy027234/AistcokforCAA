"""CNINFO 报告期公告探针的离线合同用例。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from aquant.adapters.providers.cninfo_probe import (
    CninfoProbeError,
    CninfoReportProbe,
    HttpResponse,
    market_for_code,
    normalize_title,
    organization_id,
    parse_listing_payload,
    parse_report_period,
    title_revision_flags,
)


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)


def test_report_period_parser_maps_standard_quarters_and_rejects_arbitrary_dates() -> None:
    annual = parse_report_period("2024-12-31")
    assert annual.report_kind == "annual"
    assert annual.category == "category_ndbg_szsh"
    assert annual.title_query == "2024年年度报告"
    assert annual.publication_window[0].isoformat() == "2024-12-31"

    q1 = parse_report_period("2024Q1")
    assert q1.end_date.isoformat() == "2024-03-31"
    assert q1.title_markers == ("2024年第一季度报告", "2024年一季度报告")

    with pytest.raises(CninfoProbeError, match="standard quarter end"):
        parse_report_period("2024-05-31")


def test_stock_organization_id_is_exchange_specific() -> None:
    assert market_for_code("sz000001") == "szse"
    assert market_for_code("600519") == "sse"
    assert organization_id("000001", "szse") == "gssz0000001"
    assert organization_id("600519", "sse") == "gssh0600519"


def test_title_and_revision_flags_keep_supplement_separate_from_correction() -> None:
    title = "贵州茅台<em>2024年</em><em>年度报告</em>补充公告"
    assert normalize_title(title) == "贵州茅台2024年年度报告补充公告"
    correction, revision, supplement, markers = title_revision_flags(title)
    assert correction is False
    assert revision is False
    assert supplement is True
    assert markers == ("补充",)

    assert title_revision_flags("2024年年度报告更正公告")[0] is True
    assert title_revision_flags("2024年年度报告（修订版）")[1] is True


def test_listing_parser_strictly_filters_security_and_period_without_raw_rows() -> None:
    period = parse_report_period("2024-12-31")
    payload = {
        "totalAnnouncement": 4,
        "hasMore": False,
        "announcements": [
            {
                "announcementId": "a1",
                "secCode": "600519",
                "secName": "贵州茅台",
                "announcementTitle": "贵州茅台<em>2024年</em><em>年度报告</em>",
                "announcementTime": 1743609600000,
                "adjunctUrl": "finalpage/2025-04-03/a1.PDF",
                "amount": "do not expose",
            },
            {
                "announcementId": "a2",
                "secCode": "600519",
                "announcementTitle": "贵州茅台2024年半年度报告",
                "announcementTime": 1723132800000,
                "adjunctUrl": "finalpage/2024-08-09/a2.PDF",
            },
            {
                "announcementId": "a3",
                "secCode": "000001",
                "announcementTitle": "2024年年度报告",
                "announcementTime": 1743609600000,
                "adjunctUrl": "finalpage/2025-04-03/a3.PDF",
            },
            {
                "announcementId": "missing-time",
                "secCode": "600519",
                "announcementTitle": "贵州茅台2024年年度报告",
                "announcementTime": None,
                "adjunctUrl": "finalpage/2025-04-03/missing.PDF",
            },
        ],
    }
    rows, skipped, total, has_more = parse_listing_payload(
        payload, stock_code="600519", period=period)
    assert total == 4
    assert has_more is False
    assert len(rows) == 1
    row = rows[0]
    assert row.announcement_id == "a1"
    assert row.title == "贵州茅台2024年年度报告"
    assert row.announcement_time_ms == 1743609600000
    assert row.announcement_time_utc == "2025-04-02T16:00:00+00:00"
    assert row.announcement_date_cn == "2025-04-03"
    assert row.url == "https://static.cninfo.com.cn/finalpage/2025-04-03/a1.PDF"
    assert row.is_correction is False
    assert row.is_revision is False
    assert skipped == {
        "different_report_period": 1,
        "different_security": 1,
        "missing_announcement_time": 1,
    }
    assert "amount" not in row.as_dict()
    assert "do not expose" not in json.dumps(row.as_dict(), ensure_ascii=False)


def test_probe_is_offline_by_default_and_reports_transport_metadata_only() -> None:
    offline = CninfoReportProbe().query(stock_code="600519", report_period="2024Q4")
    assert offline.error == "CninfoProbeError"
    assert offline.request_count == 0

    calls: list[tuple[str, dict[str, str]]] = []

    def transport(url, body, headers, timeout):
        from urllib.parse import parse_qs

        params = {key: values[0] for key, values in parse_qs(
            body.decode(), keep_blank_values=True).items()}
        calls.append((url, params))
        return HttpResponse(
            200,
            json.dumps({
                "totalAnnouncement": 1,
                "hasMore": False,
                "announcements": [{
                    "announcementId": "a1",
                    "secCode": "600519",
                    "secName": "贵州茅台",
                    "announcementTitle": "贵州茅台2024年年度报告更正公告",
                    "announcementTime": 1743609600000,
                    "adjunctUrl": "finalpage/2025-04-03/a1.PDF",
                }],
            }, ensure_ascii=False).encode(),
        )

    probe = CninfoReportProbe(transport=transport, clock=lambda: utc(2026, 9, 19))
    result = probe.query(stock_code="600519", report_period="2024-12-31")
    report = result.as_dict()
    assert result.error is None
    assert result.request_count == 1
    assert calls[0][0].endswith("/new/hisAnnouncement/query")
    assert calls[0][1]["stock"] == "600519,gssh0600519"
    assert calls[0][1]["searchkey"] == ""
    assert calls[0][1]["category"] == "category_ndbg_szsh"
    assert report["probe"]["response_sha256"][0].startswith("sha256:")
    assert report["announcements"][0]["has_correction_or_revision"] is True
    assert report["probe"]["requested_at"] == "2026-09-19T00:00:00+00:00"


def test_probe_does_not_follow_cross_host_adjunct_urls() -> None:
    period = parse_report_period("2024")
    rows, skipped, _, _ = parse_listing_payload({
        "announcements": [{
            "announcementId": "bad-url",
            "secCode": "000001",
            "announcementTitle": "平安银行2024年年度报告",
            "announcementTime": 1743609600000,
            "adjunctUrl": "https://evil.example/report.pdf",
        }],
    }, stock_code="000001", period=period)
    assert len(rows) == 1
    assert rows[0].url is None
    assert skipped == {}


def test_probe_uses_resolved_org_id_and_caches_it_across_periods() -> None:
    resolutions: list[str] = []
    stocks: list[str] = []

    def resolver(code: str, timeout: float) -> str:
        resolutions.append(code)
        return "9900005965"

    def transport(url, body, headers, timeout):
        from urllib.parse import parse_qs

        stocks.append(parse_qs(body.decode())["stock"][0])
        return HttpResponse(200, b'{"announcements":[],"hasMore":false}')

    probe = CninfoReportProbe(
        transport=transport,
        organization_resolver=resolver,
        clock=lambda: utc(2026, 9, 19),
    )
    probe.query(stock_code="000333", report_period="2024Q4")
    probe.query(stock_code="000333", report_period="2025Q4")
    assert resolutions == ["000333"]
    assert stocks == ["000333,9900005965", "000333,9900005965"]


def test_probe_paginates_when_provider_uses_string_has_more() -> None:
    pages = [
        {"totalAnnouncement": 2, "hasMore": "true", "announcements": [{
            "announcementId": "a1", "secCode": "600519",
            "announcementTitle": "贵州茅台2024年年度报告",
            "announcementTime": 1743609600000,
            "adjunctUrl": "finalpage/2025-04-03/a1.PDF",
        }]},
        {"totalAnnouncement": 2, "hasMore": "false", "announcements": [{
            "announcementId": "a2", "secCode": "600519",
            "announcementTitle": "贵州茅台2024年年度报告更正公告",
            "announcementTime": 1743696000000,
            "adjunctUrl": "finalpage/2025-04-04/a2.PDF",
        }]},
    ]
    calls = []

    def transport(url, body, headers, timeout):
        from urllib.parse import parse_qs

        params = parse_qs(body.decode())
        calls.append(params["pageNum"][0])
        return HttpResponse(200, json.dumps(pages[len(calls) - 1]).encode())

    result = CninfoReportProbe(transport=transport, page_size=1).query(
        stock_code="600519", report_period="2024")
    assert calls == ["1", "2"]
    assert result.has_more is False
    assert [item.announcement_id for item in result.announcements] == ["a2", "a1"]
    assert result.announcements[0].is_correction is True
