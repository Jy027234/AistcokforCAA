"""已发布快照目录 API 的安全边界。"""

from __future__ import annotations

import sys
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
