"""已发布快照目录 API 的安全边界。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture()
def snapshot_api(tmp_path):
    from main import build_state, create_app

    state = build_state(tmp_path / "data")
    with TestClient(create_app(state=state)) as client:
        yield client, state
    state.con.close()


def _insert_nonpublished(con, snapshot_id: str, status: str) -> None:
    con.execute(
        "INSERT INTO snapshot (snapshot_id,kind,data_mode,status,input_cutoff_at,"
        "as_of_time,published_at,created_at,code_version,data_version,quality_status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (snapshot_id, "EOD", "PRODUCTION", status,
         "2026-09-12T12:30:00+00:00", "2026-09-12T12:30:00+00:00", None,
         "2026-09-12T12:00:00+00:00", "test", "test", "BLOCKING"),
    )


def _publish_same_data_snapshot(state, snapshot_id: str) -> None:
    """增加一份真实 PUBLISHED 元数据项，共用不可变 fixture 数据文件。"""

    from aquant.domain.data.snapshot import DataMode, DatasetRef, SnapshotDraft

    source = state.store.published_snapshot("snap-syn-001")
    datasets = [
        DatasetRef(
            name=row["name"], path=row["path"], sha256=row["sha256"],
            record_count=row["record_count"],
            as_of_upper_bound=datetime.fromisoformat(row["as_of_upper_bound"]),
            coverage_ratio=row["coverage_ratio"],
        )
        for row in state.store.datasets("snap-syn-001")
    ]
    state.store.publish(SnapshotDraft(
        snapshot_id=snapshot_id,
        kind=source["kind"], data_mode=DataMode(source["data_mode"]),
        input_cutoff_at=datetime.fromisoformat(source["input_cutoff_at"]),
        as_of_time=(datetime.fromisoformat(source["as_of_time"])
                    if source["as_of_time"] else None),
        created_at=datetime.fromisoformat(source["created_at"]),
        published_at=datetime.fromisoformat(source["published_at"]),
        code_version=source["code_version"], data_version=source["data_version"],
        watermark=source["watermark"], strategy_version=source["strategy_version"],
        feature_version=source["feature_version"], rule_version=source["rule_version"],
        fee_version=source["fee_version"], pool_hash=source["pool_hash"],
        quality_status=source["quality_status"], datasets=datasets,
    ))


def test_catalog_returns_only_published_summaries(snapshot_api):
    client, state = snapshot_api
    _insert_nonpublished(state.con, "snap-draft-hidden", "DRAFT")
    _insert_nonpublished(state.con, "snap-rejected-hidden", "REJECTED")

    response = client.get("/api/v1/snapshots")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 1
    item = body["snapshots"][0]
    assert item["snapshotId"] == "snap-syn-001"
    assert item["dataMode"] == "SYNTHETIC"
    assert item["asOfTime"] == "2026-09-11T12:30:00+00:00"
    assert item["inputCutoffAt"] == "2026-09-11T12:30:00+00:00"
    assert item["publishedAt"] == "2026-09-11T12:31:00+00:00"
    assert item["qualityStatus"] == "OK"
    assert item["tradingDay"] == "2026-09-11"
    assert item["datasetSummary"]
    assert {d["name"] for d in item["datasetSummary"]} >= {
        "daily_quotes", "trading_calendar"
    }
    assert item["capabilities"]["s1Decision"]["available"] is False
    assert item["capabilities"]["s1Decision"]["code"] == "S1_WINDOW_INSUFFICIENT"
    assert item["capabilities"]["s1Decision"]["repairAction"]
    assert "status" not in item
    assert "path" not in item
    assert "DRAFT" not in response.text
    assert "REJECTED" not in response.text


def test_catalog_scans_raw_quotes_once_without_building_quote_rows(snapshot_api, monkeypatch):
    client, state = snapshot_api
    import main

    _publish_same_data_snapshot(state, "snap-syn-002")
    original = main.SnapshotReader._catalog_quote_summary
    scans: list[str] = []

    def count_scans(reader, snapshot_id, **kwargs):
        scans.append(snapshot_id)
        return original(reader, snapshot_id, **kwargs)

    def reject_quote_rows(*_args, **_kwargs):
        pytest.fail("catalog must not build public QuoteRow objects")

    monkeypatch.setattr(main.SnapshotReader, "_catalog_quote_summary", count_scans)
    monkeypatch.setattr(main.SnapshotReader, "daily_quotes", reject_quote_rows)
    response = client.get("/api/v1/snapshots")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 2
    assert {item["snapshotId"] for item in body["snapshots"]} == {
        "snap-syn-001", "snap-syn-002",
    }
    assert sorted(scans) == ["snap-syn-001", "snap-syn-002"]


def test_detail_does_not_expose_nonpublished_status(snapshot_api):
    client, state = snapshot_api
    _insert_nonpublished(state.con, "snap-draft-hidden", "DRAFT")
    _insert_nonpublished(state.con, "snap-rejected-hidden", "REJECTED")

    for snapshot_id in ("snap-draft-hidden", "snap-rejected-hidden", "missing"):
        response = client.get(f"/api/v1/snapshots/{snapshot_id}")
        assert response.status_code == 409, response.text
        assert "unknown published snapshot" in response.json()["error"]["message"]
        assert "DRAFT" not in response.text
        assert "REJECTED" not in response.text


def test_detail_matches_catalog_shape(snapshot_api):
    client, _state = snapshot_api

    listed = client.get("/api/v1/snapshots").json()["snapshots"][0]
    detail = client.get("/api/v1/snapshots/snap-syn-001")

    assert detail.status_code == 200, detail.text
    assert detail.json() == listed


def test_future_annual_calendar_does_not_change_observed_quote_market_day(snapshot_api, monkeypatch):
    client, state = snapshot_api
    monkeypatch.setattr(state.reader, "trading_calendar", lambda *a, **kw: ["2026-12-31"])
    item = client.get("/api/v1/snapshots/snap-syn-001").json()
    assert item["tradingDay"] == "2026-09-11"
