"""API 身份模式的最小边界测试。

``X-Aquant-Subject`` 当前仍是开发接线，不能被误报为认证。Compose 的
``LOCAL_LOOPBACK_DEMO`` 通过宿主机 loopback 端口绑定和固定主体标签支持
本机单用户试运行；容器内 TCP 对端不参与判定。
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "src"))

from main import build_state, create_app


def _client(tmp_path, monkeypatch, mode: str | None, subject: str | None = None,
            *, client_address: tuple[str, int] = ("testclient", 50000)) -> TestClient:
    if mode is None:
        monkeypatch.delenv("AQUANT_IDENTITY_MODE", raising=False)
    else:
        monkeypatch.setenv("AQUANT_IDENTITY_MODE", mode)
    if subject is None:
        monkeypatch.delenv("AQUANT_TRIAL_SUBJECT", raising=False)
    else:
        monkeypatch.setenv("AQUANT_TRIAL_SUBJECT", subject)
    return TestClient(
        create_app(state=build_state(tmp_path)),
        client=client_address,
    )


def test_undeclared_self_report_is_visible_and_blocks_trial(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, None) as client:
        body = client.get("/api/v1/readiness").json()

    identity = body["identity"]
    assert identity["mode"] == "UNDECLARED_SELF_REPORTED"
    assert identity["assurance"] == "SELF_REPORTED_HEADER_UNDECLARED"
    assert identity["productionAuthentication"] is False
    assert "IDENTITY_MODE_UNDECLARED" in {
        item["code"] for item in body["trial"]["blockingIssues"]
    }
    assert body["trial"]["ready"] is False


def test_development_header_remains_usable_but_is_not_trial_assurance(
    tmp_path, monkeypatch,
):
    with _client(tmp_path, monkeypatch, "DEVELOPMENT_SELF_REPORTED") as client:
        response = client.get(
            "/api/v1/watchlist", headers={"X-Aquant-Subject": "user:alice"}
        )
        body = client.get("/api/v1/readiness").json()

    assert response.status_code == 200
    assert body["identity"]["mode"] == "DEVELOPMENT_SELF_REPORTED"
    assert body["identity"]["subjectSource"] == "X-Aquant-Subject"
    assert "IDENTITY_PRODUCTION_AUTH_REQUIRED" in {
        item["code"] for item in body["trial"]["blockingIssues"]
    }


def test_local_loopback_demo_binds_one_subject_without_bearer(
    tmp_path, monkeypatch,
):
    with _client(
        tmp_path,
        monkeypatch,
        "LOCAL_LOOPBACK_DEMO",
        "user:demo",
        client_address=("172.18.0.2", 50000),
    ) as client:
        accepted = client.get(
            "/api/v1/watchlist", headers={"X-Aquant-Subject": "user:demo"}
        )
        rejected = client.get(
            "/api/v1/watchlist", headers={"X-Aquant-Subject": "user:alice"}
        )
        bearer = client.get(
            "/api/v1/watchlist",
            headers={"Authorization": "Bearer user:demo"},
        )
        body = client.get("/api/v1/readiness").json()

    assert accepted.status_code == 200
    assert rejected.status_code == 403
    assert bearer.status_code == 401
    assert body["identity"]["mode"] == "LOCAL_LOOPBACK_DEMO"
    assert body["identity"]["localOnly"] is True
    assert body["identity"]["singleUser"] is True
    assert body["identity"]["productionAuthentication"] is False
    assert not any(
        item["code"].startswith("IDENTITY_")
        for item in body["trial"]["blockingIssues"]
    )
