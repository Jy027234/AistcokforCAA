"""工作台写操作 API 测试。

重点不在"接口能通"，而在四件事必须成立（主文档 §16.2、§16.3、A08、A09）：

  1. 预览不写任何东西；
  2. 确认主体来自服务端信任边界，模型主体被拒；
  3. 令牌绑定该次预览且只能用一次；
  4. 现金与批次由服务端从账本读取，调用方无法谎报。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "src"))

from main import SNAPSHOT_ID, create_app  # noqa: E402

TRADING_DAY = "2026-09-08"
USER = {"X-Aquant-Subject": "user:alice"}


@pytest.fixture()
def client(tmp_path):
    app = create_app(state=__import__("main").build_state(tmp_path))
    with TestClient(app) as c:
        yield c


def preview(client, **over):
    body = {"portfolio_id": "pf-syn-m", "snapshot_id": SNAPSHOT_ID,
            "trading_day": TRADING_DAY}
    body.update(over)
    return client.post("/api/v1/plans/preview", json=body)


def confirm(client, plan_id, headers=USER):
    return client.post(f"/api/v1/plans/{plan_id}/confirmation", headers=headers)


def freeze(client, plan_id, token, headers=USER):
    return client.post(f"/api/v1/plans/{plan_id}/freeze",
                       json={"plan_id": plan_id, "confirmation_token": token},
                       headers=headers)


# ================================================================== 读
def test_health(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_status_exposes_snapshot_and_watermark(client):
    r = client.get("/api/v1/status")
    assert r.status_code == 200
    body = r.json()
    assert body["snapshotId"] == SNAPSHOT_ID
    assert body["dataMode"] == "SYNTHETIC"
    assert body["watermark"], "合成数据必须带水印"


def test_research_card_carries_no_probability_field(client):
    """§5.3：不得把排名改写成概率。契约层面断言字段不存在。"""

    r = client.get("/api/v1/instruments/SYN.A.600519/research",
                   params={"trading_day": TRADING_DAY})
    assert r.status_code == 200
    blob = r.text.lower()
    for forbidden in ("probability", "expectedreturn", "totalscore", "confidence"):
        assert forbidden not in blob, f"API 暴露了 {forbidden}"


def test_unknown_instrument_is_rejected(client):
    r = client.get("/api/v1/instruments/SYN.A.999999/research",
                   params={"trading_day": TRADING_DAY})
    assert r.status_code >= 400


# ================================================================== A08 预览不写
def test_preview_writes_nothing(client):
    before = client.get("/api/v1/portfolios/pf-syn-m/reconcile").json()
    r = preview(client)
    assert r.status_code == 200
    after = client.get("/api/v1/portfolios/pf-syn-m/reconcile").json()
    assert before["fill_count"] == after["fill_count"] == 0
    assert before["positions"] == after["positions"] == {}


def test_preview_is_marked_unfrozen(client):
    body = preview(client).json()
    assert body["frozen"] is False
    assert "未冻结" in body["frozenLabel"]


def test_preview_produces_orders_and_costs(client):
    body = preview(client).json()
    assert body["orders"], "应当产生订单"
    assert body["estimatedFeesCents"] > 0


def test_unknown_snapshot_is_404(client):
    r = preview(client, snapshot_id="snap-nope")
    assert r.status_code == 404


# ================================================================== 身份
def test_missing_subject_is_401(client):
    pid = preview(client).json()["planId"]
    r = client.post(f"/api/v1/plans/{pid}/confirmation")   # 无头部
    assert r.status_code == 401


@pytest.mark.parametrize("subject", ["model:assistant", "assistant", "agent-1", "llm", "bot"])
def test_model_principals_cannot_confirm(client, subject):
    """§16.3 冻结模拟计划是用户动作，模型主体一律拒绝。"""

    pid = preview(client).json()["planId"]
    r = confirm(client, pid, headers={"X-Aquant-Subject": subject})
    assert r.status_code == 403
    assert "user action" in r.json()["detail"]


# ================================================================== 令牌
def test_happy_path_preview_confirm_freeze_execute_value(client):
    pv = preview(client).json()
    pid = pv["planId"]

    tok = confirm(client, pid)
    assert tok.status_code == 200
    token = tok.json()["confirmationToken"]

    fr = freeze(client, pid, token)
    assert fr.status_code == 200, fr.text
    assert fr.json()["status"] == "FROZEN"

    ex = client.post(f"/api/v1/plans/{pid}/execute",
                     json={"plan_id": pid}, headers=USER)
    assert ex.status_code == 200, ex.text
    assert ex.json()["fills"], "冻结的计划应当成交"

    val = client.post("/api/v1/valuations",
                      json={"portfolio_id": "pf-syn-m", "snapshot_id": SNAPSHOT_ID,
                            "trading_day": TRADING_DAY})
    assert val.status_code == 200
    assert val.json()["published"] is True

    rec = client.get("/api/v1/portfolios/pf-syn-m/reconcile").json()
    assert rec["reconciled"] is True, rec
    assert rec["fill_count"] >= 1
    # 对账不只看一个布尔值，逐项不变量都要成立
    inv = rec["invariants"]
    for name in ("fill_le_order", "fees_booked_once", "cash_lines_sum_to_balance",
                 "lots_match_fills"):
        assert inv[name] is True, (name, inv)
    assert inv["violations"] in ([], None), inv["violations"]
    assert rec["valuation_cash_matches_ledger"] is True


def test_forged_token_is_refused(client):
    pid = preview(client).json()["planId"]
    r = freeze(client, pid, "forged-token-000000")
    assert r.status_code == 409
    assert "invalid" in r.json()["error"]["message"]


def test_token_is_single_use(client):
    pid = preview(client).json()["planId"]
    token = confirm(client, pid).json()["confirmationToken"]
    assert freeze(client, pid, token).status_code == 200
    again = freeze(client, pid, token)
    assert again.status_code == 409
    assert "consumed" in again.json()["error"]["message"]


def test_token_cannot_be_used_by_another_subject(client):
    pid = preview(client).json()["planId"]
    token = confirm(client, pid).json()["confirmationToken"]
    r = freeze(client, pid, token, headers={"X-Aquant-Subject": "user:bob"})
    assert r.status_code == 409
    assert "another subject" in r.json()["error"]["message"]


def test_confirmation_requires_a_live_preview(client):
    """没有预览就没有令牌——服务端不签发针对未知计划的令牌。"""

    r = confirm(client, "plan_does_not_exist")
    assert r.status_code == 404


# ================================================================== A09 / 谎报
def test_supplied_cash_must_match_ledger(client):
    """调用方提供现金时，与账本不符即拒绝。"""

    pid = preview(client).json()["planId"]
    token = confirm(client, pid).json()["confirmationToken"]
    assert freeze(client, pid, token).status_code == 200

    # 执行后账本有余额；此时再报一个假数字应当被拒
    client.post(f"/api/v1/plans/{pid}/execute", json={"plan_id": pid}, headers=USER)
    r = preview(client, cash_available_cents=5)
    assert r.status_code == 409
    # 领域校验会先命中，错误信封可能来自领域处理器或 API 层；两者都要能读到消息
    payload = r.json()
    envelope = payload.get("error") or payload.get("detail", {}).get("error", {})
    assert "ledger" in envelope["message"], payload


def test_execute_requires_subject(client):
    pid = preview(client).json()["planId"]
    r = client.post(f"/api/v1/plans/{pid}/execute", json={"plan_id": pid})
    assert r.status_code == 401


def test_double_execute_is_refused(client):
    pid = preview(client).json()["planId"]
    token = confirm(client, pid).json()["confirmationToken"]
    freeze(client, pid, token)
    assert client.post(f"/api/v1/plans/{pid}/execute",
                       json={"plan_id": pid}, headers=USER).status_code == 200
    second = client.post(f"/api/v1/plans/{pid}/execute",
                         json={"plan_id": pid}, headers=USER)
    assert second.status_code == 409


def test_path_and_body_plan_id_must_agree(client):
    pid = preview(client).json()["planId"]
    token = confirm(client, pid).json()["confirmationToken"]
    r = client.post(f"/api/v1/plans/{pid}/freeze",
                    json={"plan_id": "plan_other", "confirmation_token": token},
                    headers=USER)
    assert r.status_code == 400


# ================================================================== 收窄
def test_api_exposes_no_order_or_ledger_write_paths(client):
    """§16.3：不得出现下单或直接写账本的接口，也不得出现任意抓取。"""

    paths = {getattr(r, "path", "") for r in client.app.routes}
    blob = " ".join(sorted(paths)).lower()
    for forbidden in ("order", "trade", "ledger/write", "shell", "sql", "fetch"):
        assert forbidden not in blob, f"API 暴露了 {forbidden} 路径: {sorted(paths)}"
