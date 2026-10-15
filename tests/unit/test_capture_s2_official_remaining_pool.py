from datetime import date
import hashlib
import json

import pytest

from tools.capture_s2_official_remaining_pool import (
    ArchiveFirstCninfoClient,
    _candidate_periods,
    load_targets,
)
from aquant.adapters.providers.cninfo import CrossCategoryIndexEntry


def test_remaining_scope_is_exact_frozen_pool_minus_priority(tmp_path):
    pool_path = tmp_path / "pool.yaml"
    priority_path = tmp_path / "priority.json"
    followup_path = tmp_path / "followup.json"
    pool_path.write_text("""pool_id: synthetic-research
as_of: '2026-10-13'
instruments:
  - {instrument_id: SH.600001, code: sh600001}
  - {instrument_id: SZ.002468, code: sz002468}
  - {instrument_id: SZ.000001, code: sz000001}
""", encoding="utf-8")
    priority_path.write_text(json.dumps({"priorityInstrumentIds": ["SH.600001"]}),
                             encoding="utf-8")
    pool_hash = "sha256:" + hashlib.sha256(pool_path.read_bytes()).hexdigest()
    manifest = {
        "pool_sha256": pool_hash,
        "priority_manifest_sha256": "sha256:" + hashlib.sha256(priority_path.read_bytes()).hexdigest(),
        "remaining_in_pool_order": [
            {"code": "002468", "instrument_id": "SZ.002468"},
            {"code": "000001", "instrument_id": "SZ.000001"},
        ],
    }
    followup_path.write_text(json.dumps(manifest), encoding="utf-8")

    targets, binding = load_targets(pool_path, followup_path, priority_path)

    assert [row["instrumentId"] for row in targets] == ["SZ.002468", "SZ.000001"]
    assert targets[0]["code"] == "002468"
    assert binding["poolSha256"] == pool_hash

    manifest["remaining_in_pool_order"].pop()
    followup_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="pool minus priority scope"):
        load_targets(pool_path, followup_path, priority_path)


def test_period_discovery_records_actual_q3_when_official_title_exists():
    announcements = [
        CrossCategoryIndexEntry(
            announcement_id="q3", sec_code="002468", title="2026年第三季度报告",
            announcement_time_ms=1, document_url="https://static.cninfo.com.cn/q3.pdf",
            candidate_reasons=(), page_num=1, page_receipt_id="r1", page_content_hash="h1",
        ),
        CrossCategoryIndexEntry(
            announcement_id="h1", sec_code="002468", title="2026年半年度报告",
            announcement_time_ms=1, document_url="https://static.cninfo.com.cn/h1.pdf",
            candidate_reasons=(), page_num=1, page_receipt_id="r1", page_content_hash="h1",
        ),
    ]

    assert _candidate_periods(announcements, through=date(2026, 10, 13)) == [
        date(2026, 6, 30), date(2026, 9, 30)
    ]


@pytest.mark.parametrize("title", [
    "2026年度半年报",
    "2026年度半年报（修订版）",
])
def test_period_discovery_accepts_complete_issuer_h1_alias(title):
    announcement = CrossCategoryIndexEntry(
        announcement_id="h1-alias", sec_code="603365", title=title,
        announcement_time_ms=1, document_url="https://static.cninfo.com.cn/h1.pdf",
        candidate_reasons=(), page_num=1, page_receipt_id="r1", page_content_hash="h1",
    )

    assert _candidate_periods([announcement], through=date(2026, 10, 13)) == [
        date(2026, 6, 30)
    ]


@pytest.mark.parametrize("title", [
    "2026年度半年报业绩预告",
    "2026年度半年报摘要",
    "2026年度半年报英文版",
    "2026年度半年报提示性公告",
])
def test_period_discovery_rejects_non_report_h1_alias_titles(title):
    announcement = CrossCategoryIndexEntry(
        announcement_id="not-a-report", sec_code="603365", title=title,
        announcement_time_ms=1, document_url="https://static.cninfo.com.cn/not-report.pdf",
        candidate_reasons=(), page_num=1, page_receipt_id="r1", page_content_hash="h1",
    )

    assert _candidate_periods([announcement], through=date(2026, 10, 13)) == []


def test_period_discovery_keeps_q1_q3_and_fy_periods_distinct():
    titles = [
        "2025年年度报告",
        "2026年第一季度报告",
        "2026年第三季度报告",
    ]
    announcements = [CrossCategoryIndexEntry(
        announcement_id=str(i), sec_code="603365", title=title,
        announcement_time_ms=1, document_url=f"https://static.cninfo.com.cn/{i}.pdf",
        candidate_reasons=(), page_num=1, page_receipt_id="r1", page_content_hash="h1",
    ) for i, title in enumerate(titles)]

    assert _candidate_periods(announcements, through=date(2026, 10, 13)) == [
        date(2025, 12, 31), date(2026, 3, 31), date(2026, 9, 30)
    ]


def test_archived_open_index_window_is_reused_only_on_observation_day():
    client = object.__new__(ArchiveFirstCninfoClient)
    client.observed_day = date(2026, 10, 13)
    row = {"first_seen_at": "2026-10-12T09:00:00+08:00"}
    body = b"seDate=2025-06-30%7E2027-06-30"

    assert not client._cached_receipt_is_current(
        row, url="http://www.cninfo.com.cn/new/hisAnnouncement/query", data=body
    )
    assert client._cached_receipt_is_current(
        row, url="https://www.cninfo.com.cn/new/information/topSearch/query", data=b""
    )
