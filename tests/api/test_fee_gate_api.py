"""§12.6 费率闸门在 API 边界的表现。

为什么单独一条：FeeError 此前**从未走到过 API 边界**——合成费率的守卫
只被一个测试调用过，生产路径根本不会抛它。把闸门接进
preview/freeze/execute 之后它立刻会走到这里，而没有处理器就会变成 500，
调用方看到的是"服务端故障"，不是"你的费率表没有依据"。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "src"))

from main import build_state, create_app  # noqa: E402

USER = {"X-Aquant-Subject": "user:alice"}


@pytest.fixture()
def client(tmp_path):
    app = create_app(state=build_state(tmp_path))
    with TestClient(app) as c:
        yield c


def test_synthetic_snapshot_still_previews(client):
    """合成快照上用合成费率是允许的（它本来就是跑通链路用的）。"""

    r = client.post("/api/v1/plans/preview", headers=USER, json={
        "portfolio_id": "pf-fee-syn-M", "snapshot_id": "snap-syn-001",
        "trading_day": "2026-09-08"})
    assert r.status_code == 200, r.text


def test_unconfigured_real_snapshot_keeps_read_only_service_available(monkeypatch):
    """未配置佣金时保留只读能力，模拟入口仍由 synthetic 标记阻断。"""

    from main import resolve_fee_table

    monkeypatch.delenv("AQUANT_COMMISSION_RATE", raising=False)
    monkeypatch.delenv("AQUANT_COMMISSION_MIN_CENTS", raising=False)
    table = resolve_fee_table("snap-eod-real")
    assert table.is_synthetic is True
    assert table.commission_source == "UNCONFIGURED_DEFAULT"


def test_user_approved_assumption_clears_fee_readiness_block(monkeypatch, tmp_path):
    """明确批准的行业假设应放行费用闸门，同时保留来源标签。"""

    monkeypatch.setenv("AQUANT_COMMISSION_RATE", "0.00025")
    monkeypatch.setenv("AQUANT_COMMISSION_MIN_CENTS", "500")
    monkeypatch.setenv("AQUANT_COMMISSION_SOURCE", "USER_APPROVED_ASSUMPTION")
    app = create_app(state=build_state(tmp_path))
    with TestClient(app) as c:
        fees = c.get("/api/v1/fees").json()
        trial = c.get("/api/v1/readiness").json()["trial"]
    assert fees["commissionSource"] == "USER_APPROVED_ASSUMPTION"
    assert fees["syntheticTestRate"] is False
    assert "FEE_VERSION_UNVERIFIED" not in {
        item["code"] for item in trial["blockingIssues"]
    }


def test_fee_error_becomes_a_422_envelope_not_a_500(client):
    """费率问题必须是 422 + §16.4 信封，不能是 500。

    FeeError 此前**从未走到过 API 边界**（合成费率的守卫只被一个测试
    调用过），所以这条必须经过真实的处理器验一次，
    而不是只断言"我能构造一个 FeeError"。
    """

    from fastapi import FastAPI
    from fastapi.testclient import TestClient as _TC

    from aquant.domain.simulation.fees import FeeError
    from main import create_app as _create

    # 刻意**不**用 create_app()：它会把前端产物挂在 "/" 上（部署用），
    # 那个 catch-all 会吃掉这里临时注册的探针路由。
    # 这条用例要验的是异常处理器，不是静态服务。
    probe = FastAPI()
    probe.state.aquant = client.app.state.aquant
    for handler in client.app.exception_handlers.items():
        probe.add_exception_handler(*handler)

    @probe.get("/api/v1/_probe_fee_error")
    def _boom():                                          # pragma: no cover
        raise FeeError("FEE_VERSION_UNVERIFIED", "合成费率不得用于真实数据",
                       "配置 AQUANT_COMMISSION_RATE")

    with _TC(probe) as c:
        r = c.get("/api/v1/_probe_fee_error")
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"]["code"] == "FEE_VERSION_UNVERIFIED"
    assert body["error"]["repair_action"], "必须给出可操作的修复动作"
    assert "detail" not in body, "多嵌一层 detail 会让调用方写两套解析逻辑"
