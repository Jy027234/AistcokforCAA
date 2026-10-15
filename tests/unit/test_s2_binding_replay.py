"""中断恢复及修订版本的真实失败场景：无网络、坏哈希、通知混入、数值未证实。"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import bind_structured_to_announcements as binding
from replay_s2_bindings import ArchivedCninfoClient
from aquant.adapters.providers.cninfo import CninfoClient, FetchOutcome
from aquant.domain.data.forward_archive import ForwardArchive


def _announcement(aid, title, day=15):
    return {"announcementId": aid, "secCode": "600519", "secName": "贵州茅台",
            "announcementTitle": title,
            "announcementTime": int(datetime(2026, 8, day, tzinfo=timezone.utc).timestamp() * 1000),
            "adjunctUrl": f"finalpage/2026-08-{day:02}/{aid}.PDF"}


@pytest.fixture
def archive(tmp_path):
    con = sqlite3.connect(tmp_path / "meta.sqlite")
    con.row_factory = sqlite3.Row
    archive = ForwardArchive(con, tmp_path)
    yield archive
    con.close()


def _capture(archive, monkeypatch, pages):
    client = CninfoClient(archive)

    def fetch(url, *, label, data=None, **kwargs):
        from urllib.parse import parse_qs
        page = int(parse_qs(data.decode())["pageNum"][0])
        payload = json.dumps(pages[page - 1]).encode()
        digest, _ = archive.store_bytes(payload)
        receipt = archive.record(source_id="cninfo", url=url, outcome="OK",
                                 requested_at=datetime.now(timezone.utc), http_status=200,
                                 content_hash=digest, byte_size=len(payload), detail=label)
        return FetchOutcome(True, payload, receipt.receipt_id, digest, None, 200)

    monkeypatch.setattr(client, "_fetch", fetch)
    return client.report_index(stock_code="600519", report_period="2026-06-30",
                               organization_id="gssh0600519", page_size=2)


def _row():
    return {"instrument_id": "600519", "period_end": "2026-06-30",
            "values": {key: "1" for key in ("revenue", "net_profit_attributable",
                      "net_profit_consolidated", "parent_equity", "operating_cashflow")},
            "evidence": {}}


def test_archived_replay_reuses_exact_receipts_without_network(archive, monkeypatch):
    expected = _capture(archive, monkeypatch, [{"announcements": [
        _announcement("original", "2026年半年度报告")],
        "totalAnnouncement": 1, "hasMore": False}])
    before = len(archive.receipts_for("cninfo"))
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: pytest.fail("network attempted"))
    actual = ArchivedCninfoClient(archive).report_index(
        stock_code="600519", report_period="2026-06-30",
        organization_id="gssh0600519", page_size=2)
    assert actual == expected
    assert len(archive.receipts_for("cninfo")) == before
    entry = binding.bind_record(_row(), actual, [date(2026, 8, 17)], archive)
    assert entry["status"] == "RECONSTRUCTED_READY"
    assert entry["observed_at"] == archive.receipts_for("cninfo")[0]["first_seen_at"]


def test_missing_archive_page_does_not_fall_back_to_network(archive, monkeypatch):
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: pytest.fail("network attempted"))
    result = ArchivedCninfoClient(archive).report_index(
        stock_code="600519", report_period="2026-06-30", organization_id="gssh0600519")
    assert not result.complete and not result.matches
    assert result.error == "ARCHIVED_INDEX_PAGE_MISSING"
    assert archive.receipts_for("cninfo") == []


def test_corrupt_response_fails_closed(archive, monkeypatch):
    result = _capture(archive, monkeypatch, [{"announcements": [
        _announcement("original", "2026年半年度报告")],
        "totalAnnouncement": 1, "hasMore": False}])
    digest = result.pages[0].content_hash
    stored = archive.con.execute("SELECT stored_path FROM raw_artifact WHERE content_hash=?",
                                 (digest,)).fetchone()[0]
    (archive.root / stored).write_bytes(b"tampered")
    replay = ArchivedCninfoClient(archive).report_index(
        stock_code="600519", report_period="2026-06-30",
        organization_id="gssh0600519", page_size=2)
    assert not replay.complete and replay.error == "ARCHIVED_RESPONSE_HASH_INVALID"


def test_correction_notice_is_not_a_full_report_or_a_false_timestamp_tie(archive, monkeypatch):
    result = _capture(archive, monkeypatch, [{"announcements": [
        _announcement("original", "2026年半年度报告", 14),
        _announcement("new-report", "2026年半年度报告（更新后）"),
        _announcement("notice", "关于更正《2026年半年度报告》的公告")],
        "totalAnnouncement": 3, "hasMore": False}])
    entry = binding.bind_record(_row(), result, [date(2026, 8, 17)], archive)
    assert entry["announcement"]["announcement_id"] == "new-report"
    assert {v["announcement_id"] for v in entry["report_versions"]} == {"original", "new-report"}
    assert entry["status"] == "REVISION_REVIEW_REQUIRED"
    assert entry["revision_value_verification"]["status"] == "PDF_NOT_ARCHIVED"
    assert "available_at" not in entry


def test_selected_report_uses_its_own_page_receipt(archive, monkeypatch):
    result = _capture(archive, monkeypatch, [
        {"announcements": [_announcement("notice", "关于更正《2026年半年度报告》的公告")],
         "totalAnnouncement": 2, "hasMore": True},
        {"announcements": [_announcement("actual", "2026年半年度报告（更正后）")],
         "totalAnnouncement": 2, "hasMore": False}])
    entry = binding.bind_record(_row(), result, [date(2026, 8, 17)], archive)
    assert entry["announcement"]["index_receipt_id"] == result.pages[1].receipt_id
    assert entry["announcement"]["index_page_hash"] == result.pages[1].content_hash


def test_unmatched_revision_values_are_never_ready(archive, monkeypatch):
    result = _capture(archive, monkeypatch, [{"announcements": [
        _announcement("new", "2026年半年度报告（更正后）")],
        "totalAnnouncement": 1, "hasMore": False}])
    monkeypatch.setattr(binding, "verify_revision_values", lambda *args: {"status": "MISMATCH"})
    entry = binding.bind_record(_row(), result, [date(2026, 8, 17)], archive)
    assert entry["status"] == "REVISION_REVIEW_REQUIRED"
    assert "available_at" not in entry


def test_verified_revision_uses_latest_date_pdf_hash_and_real_observation(archive, monkeypatch):
    result = _capture(archive, monkeypatch, [{"announcements": [
        _announcement("old", "2026年半年度报告", 14),
        _announcement("new", "2026年半年度报告（更正后）", 15)],
        "totalAnnouncement": 2, "hasMore": False}])
    pdf_digest, _ = archive.store_bytes(b"verified fixture PDF")
    pdf_receipt = archive.record(source_id="cninfo", url=result.matches[0].announcement.url,
        outcome="OK", requested_at=datetime.now(timezone.utc), http_status=200,
        content_hash=pdf_digest, byte_size=20, detail="document")
    monkeypatch.setattr(binding, "verify_revision_values", lambda *args: {
        "status": "MATCH", "content_hash": pdf_digest,
        "receipt_id": pdf_receipt.receipt_id})
    entry = binding.bind_record(_row(), result, [date(2026, 8, 17)], archive)
    assert entry["status"] == "RECONSTRUCTED_READY"
    assert entry["announcement"]["announcement_id"] == "new"
    assert entry["available_at"] == "2026-08-17T09:00:00+08:00"
    assert entry["report_content_hash"] == pdf_digest
    assert entry["observed_at"] == pdf_receipt.first_seen_at.isoformat()


def test_organization_alias_map_requires_exact_archived_official_bytes(archive, tmp_path):
    source = {"stockList": [
        {"code": "832982", "orgId": "gfbj0832982", "category": "A股"},
        {"code": "920982", "orgId": "gfbj0832982", "category": "A股"}]}
    raw = json.dumps(source).encode()
    digest, _ = archive.store_bytes(raw)
    url = "http://www.cninfo.com.cn/new/data/szse_stock.json"
    receipt = archive.record(source_id="cninfo", url=url, outcome="OK",
        requested_at=datetime.now(timezone.utc), http_status=200,
        content_hash=digest, byte_size=len(raw), detail="universe")
    packet = {"category": "A股", "source_url": url, "content_hash": digest,
        "receipt_id": receipt.receipt_id,
        "organization_ids": {"832982": "gfbj0832982", "920982": "gfbj0832982"}}
    path = tmp_path / "orgs.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    orgs, groups, provenance = binding.verified_organization_map(path, archive)
    assert groups[orgs["920982"]] == ("832982", "920982")
    assert provenance["content_hash"] == digest
    packet["organization_ids"]["920982"] = "different-company"
    path.write_text(json.dumps(packet), encoding="utf-8")
    with pytest.raises(ValueError, match="differs from official source bytes"):
        binding.verified_organization_map(path, archive)


def test_observation_refresh_uses_source_receipts_and_refuses_missing_receipt(archive):
    receipt = archive.record(source_id="cninfo", url="http://example.test/index",
        outcome="OK", requested_at=datetime.now(timezone.utc), http_status=200,
        content_hash=None, byte_size=0, detail="fixture")
    row = {"announcement": {"index_receipt_id": receipt.receipt_id},
           "generated_at": "2099-01-01T00:00:00+00:00"}
    assert binding.receipt_observed_at(row, archive) == receipt.first_seen_at.isoformat()
    row["evidence"] = {"revenue": {"receipt_id": "missing"}}
    assert binding.receipt_observed_at(row, archive) is None
