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


def test_preview_works_again_after_the_account_has_traded(client):
    """回归：执行过一轮之后，再次预览必须仍然可用。

    预览是**只读入口**，它只是要求账户存在，并不声明账户状态；
    若拿一个占位开户金额去和已经变化的账本比对，第二次预览会永远 409，
    而用户其实只是"再看看"。真正的比对属于冻结与执行。
    """

    pv = preview(client).json()
    pid = pv["planId"]
    tok = confirm(client, pid).json()["confirmationToken"]
    assert freeze(client, pid, tok).status_code == 200
    assert client.post(f"/api/v1/plans/{pid}/execute",
                       json={"plan_id": pid}, headers=USER).status_code == 200

    again = preview(client)
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["frozen"] is False
    # 账户已建仓，订单应反映当前持仓而不是重新建仓
    assert "planId" in body


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

# ============================================================== 分红落库
# 登记日与除权日都取执行日：执行日的买入发生在登记日收盘之前，因此
# 当日买入的批次享有本次分红，这是合法且常见的情形（除权除息日当天买入）。
# 到账日刻意推后，才能把"确认应收"和"转入现金"分成两天观察。
DIVIDEND = {
    "action_id": "ca-api-1",
    "instrument_id": "SYN.A.600519",
    "record_date": TRADING_DAY,
    "ex_date": TRADING_DAY,
    "pay_date": "2026-09-15",
    "cash_per_share_cents": 50,
}


def _run_plan(client, **extra):
    """完整走一遍 预览 -> 确认 -> 冻结 -> 执行，返回执行结果。"""

    pid = preview(client).json()["planId"]
    token = confirm(client, pid).json()["confirmationToken"]
    assert freeze(client, pid, token).status_code == 200
    body = {"plan_id": pid}
    body.update(extra)
    return client.post(f"/api/v1/plans/{pid}/execute", json=body, headers=USER)


def test_execute_recognises_dividend_receivable_on_ex_date(client):
    """除权日执行时必须落一条应收，且**现金不得因此增加**。

    先跑一轮建立底仓，再用第二轮带上分红。权利取**登记日收盘**持仓，
    所以第二轮当日的成交也算数——这也是为什么必须先有一轮：
    这里要验的是"执行链路把分红落库了"，而不是权利算法本身
    （后者由 tests/golden/test_dividend_persistence.py 覆盖）。
    """

    first = _run_plan(client)
    assert first.status_code == 200 and first.json()["fills"]

    ex = _run_plan(client, corporate_actions=[DIVIDEND])
    assert ex.status_code == 200, ex.text
    out = ex.json()
    assert out["corporate_actions"], "执行结果里必须能看到当日公司行为"
    stage = out["corporate_actions"][0]
    assert stage["stage"] == "EX_DATE"
    assert stage["cash_delta_cents"] == 0, "除权日不得直接进现金"

    # 应收必须体现在估值里，且由服务端从账本读取，而不是由请求传入
    val = client.post("/api/v1/valuations",
                      json={"portfolio_id": "pf-syn-m", "snapshot_id": SNAPSHOT_ID,
                            "trading_day": TRADING_DAY}).json()
    assert val["published"] is True
    assert val["receivables_cents"] == stage["receivable_cents"] > 0
    assert val["net_value_cents"] == (
        val["cash_available_cents"] + val["cash_frozen_cents"]
        + val["receivables_cents"] + val["positions_value_cents"]
        - val["payables_cents"]
    )

    # 对账必须把应收也算进去，否则"已确认未到账"的钱在账面上凭空消失：
    # 它既不在现金里，也不在持仓里。
    rec = client.get("/api/v1/portfolios/pf-syn-m/reconcile").json()
    assert rec["receivables_cents"] == val["receivables_cents"]
    assert rec["valuation_receivables_matches_ledger"] is True, rec
    assert rec["reconciled"] is True, rec


def test_dividend_cannot_carry_impossible_dates(client):
    """到账日早于除权日必须被拒，不得静默接受。"""

    pid = preview(client).json()["planId"]
    token = confirm(client, pid).json()["confirmationToken"]
    assert freeze(client, pid, token).status_code == 200

    bad = dict(DIVIDEND, pay_date="2026-09-01")   # 早于 ex_date
    r = client.post(f"/api/v1/plans/{pid}/execute",
                    json={"plan_id": pid, "corporate_actions": [bad]}, headers=USER)
    assert r.status_code == 409, r.text
    envelope = r.json()["error"]
    assert envelope["code"] == "CORPORATE_ACTION_UNSUPPORTED"
    assert "out of order" in envelope["message"]
    assert envelope["repair_action"], "错误必须给出修复动作"


def test_dividend_amount_must_be_positive(client):
    """每股股利必须是正整数分；0 或负数不得进入账本。"""

    pid = preview(client).json()["planId"]
    bad = dict(DIVIDEND, cash_per_share_cents=0)
    r = client.post(f"/api/v1/plans/{pid}/execute",
                    json={"plan_id": pid, "corporate_actions": [bad]}, headers=USER)
    assert r.status_code == 422, r.text


def test_client_cannot_dictate_dividend_entitlement(client):
    """调用方只能描述公司行为，不能指定股数或金额。

    权利由服务端按**登记日**持仓计算；请求里出现 entitlement/shares
    之类的字段会被忽略，而不是被采信。
    """

    pv = preview(client).json()
    pid = pv["planId"]
    token = confirm(client, pid).json()["confirmationToken"]
    assert freeze(client, pid, token).status_code == 200

    forged = dict(DIVIDEND, entitlement_shares=999_999, receivable_cents=1)
    ex = client.post(f"/api/v1/plans/{pid}/execute",
                     json={"plan_id": pid, "corporate_actions": [forged]}, headers=USER)
    assert ex.status_code == 200, ex.text
    stage = ex.json()["corporate_actions"][0]
    assert stage["entitlement_shares"] != 999_999, "调用方不得指定权利股数"
    assert stage["receivable_cents"] != 1

