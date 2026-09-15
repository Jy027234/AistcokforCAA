"""助手问答的外发闸门（§17.2、§826、ADR-012）。

这个接口是**唯一**一条"把材料发给模型"的路径，因此它必须证明：

  1. 已授权来源的材料能发出去，并且**留档**（成功与失败都留）；
  2. 未登记来源被拒，且**材料没有被发出去**——不是"发出去了再报错"；
  3. 被拒与失败同样留档：否则预算与责任都无从核对；
  4. 真实数据语境里混入合成材料会被拦下——这类错误会让结论
     看起来完全正常，只是基于一份示例数据。

测试不联网、不需要密钥：用的是确定性替身（ADR-012 的离线路径）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.ai.model import ModelResponse  # noqa: E402
from main import build_state, create_app  # noqa: E402

USER = {"X-Aquant-Subject": "user:alice"}


class StubProvider:
    """确定性替身。**记录它收到了什么**——这是"材料到底发出去没有"的证据。"""

    provider_name = "stub"
    model_name = "stub-model"

    def __init__(self) -> None:
        self.calls: list = []

    def complete(self, request) -> ModelResponse:
        self.calls.append(request)
        return ModelResponse(text="依据材料，结论如下。", provider=self.provider_name,
                             model=self.model_name, input_tokens=123, output_tokens=45,
                             content_hash="sha256:" + "a" * 64)


class FailingProvider(StubProvider):
    def complete(self, request):
        from aquant.domain.ai.model import ModelUnavailable
        self.calls.append(request)
        raise ModelUnavailable("上游 503", repair_action="稍后重试")


@pytest.fixture()
def ctx(tmp_path):
    state = build_state(tmp_path)
    stub = StubProvider()
    state.model_provider = stub
    app = create_app(state=state)
    with TestClient(app) as client:
        yield client, state.con, stub


def ask(client, materials, *, purpose="测试用途"):
    return client.post("/api/v1/assistant/messages",
                       json={"purpose": purpose, "materials": materials},
                       headers=USER)


def mat(source="cninfo", text="某公司公告正文"):
    return {"source_id": source, "text": text, "contains_personal_data": False}


# ============================================================ 放行与留档
def test_authorised_material_is_sent_and_recorded(ctx):
    client, con, stub = ctx
    r = ask(client, [mat(), mat(source="baostock", text="日线数据")])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"], body
    assert body["sourcesUsed"] == ["cninfo", "baostock"]
    assert body["modelCallId"]

    # 材料真的到了模型手上（不是"通过了检查但没发"）
    assert len(stub.calls) == 1
    assert "某公司公告正文" in stub.calls[0].context

    row = con.execute("SELECT * FROM model_call WHERE model_call_id=?",
                      (body["modelCallId"],)).fetchone()
    assert row is not None, "成功的调用也必须留档"
    assert row["outcome"] == "OK"
    assert row["input_tokens"] == 123 and row["output_tokens"] == 45
    assert row["content_hash"] == body["contentHash"]


def test_answer_is_bound_to_a_published_snapshot(ctx):
    """§5.5：助手回答系统状态前必须先读快照，回答里要能看出依据哪个时点。"""

    client, _con, stub = ctx
    body = ask(client, [mat()]).json()
    based = body.get("basedOn")
    assert based, "回答没有标注所依据的快照与时点"
    assert based["snapshotId"], based
    assert based["asOfTime"], based
    assert based["dataMode"], based
    # 系统状态注记确实进入了模型上下文（不是只在响应里回显）
    assert "系统状态" in stub.calls[-1].context, stub.calls[-1].context[:200]
    assert based["snapshotId"] in stub.calls[-1].context


def test_state_note_uses_a_registered_source(ctx):
    """注记的来源必须已登记，否则会被闸门拦下（而原因看起来像别的问题）。"""

    client, con, stub = ctx
    body = ask(client, [mat()]).json()
    registered = {r[0] for r in con.execute("SELECT source_id FROM source_registry")}
    assert body["basedOn"]["stateSourceId"] in registered, (
        body["basedOn"]["stateSourceId"] + " 不在已登记来源里")


def test_instructions_forbid_probability_outputs(ctx):
    """§5.3 的要求在提示词层面也要落一次。"""

    client, _con, stub = ctx
    ask(client, [mat()])
    instructions = stub.calls[0].instructions
    for forbidden in ("概率", "预期收益", "目标价"):
        assert forbidden in instructions, f"指令里没有禁止 {forbidden}"


# ============================================================ 拒绝路径
def test_unregistered_source_is_rejected_and_never_sent(ctx):
    """未登记来源必须**发不出去**，而不是"发了再报错"。"""

    client, con, stub = ctx
    r = ask(client, [mat(source="some-unknown-site")])
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "SOURCE_PERMISSION_MISSING"
    assert detail["repair_action"], "被拒时必须给出可操作的修复动作"
    assert detail["blockers"][0]["source_id"] == "some-unknown-site"

    assert stub.calls == [], "材料在闸门拦下之后仍然被发出去了"

    # 被拒同样留档：否则"试过哪些来源、被拦了几次"无从核对
    row = con.execute("SELECT outcome,error_code FROM model_call").fetchone()
    assert row is not None, "被拒的尝试没有留档"
    assert row["outcome"] == "REJECTED"
    assert row["error_code"] == "SOURCE_PERMISSION_MISSING"


def test_one_bad_source_rejects_the_whole_batch(ctx):
    """整批拒绝，不静默过滤：过滤会让调用方以为材料都用上了。"""

    client, _con, stub = ctx
    r = ask(client, [mat(), mat(source="some-unknown-site")])
    assert r.status_code == 403
    assert stub.calls == []


def test_personal_data_is_blocked_by_default(ctx):
    """§826：未声明等同于含个人信息，默认不进模型上下文。"""

    client, _con, stub = ctx
    r = client.post("/api/v1/assistant/messages",
                    json={"purpose": "测试", "materials": [
                        {"source_id": "cninfo", "text": "含姓名的材料"}]},
                    headers=USER)
    assert r.status_code == 403
    assert r.json()["detail"]["blockers"][0]["right"] == "personal_data"
    assert stub.calls == []


def test_model_failure_is_503_and_recorded(ctx):
    client, con, _stub = ctx
    # 直接替换 state 上的 provider。生产路径会构造 DeepSeekProvider，
    # 但测试必须离线，因此换成一个**明确失败**的替身来验证失败留档。
    state = client.app.state.aquant
    state.model_provider = FailingProvider()
    r = ask(client, [mat()])
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["code"] == "DATA_NOT_READY"

    row = con.execute("SELECT outcome,error_code FROM model_call").fetchone()
    assert row["outcome"] == "ERROR", "失败的调用没有留档"


def test_calls_endpoint_exposes_the_audit_trail(ctx):
    client, _con, _stub = ctx
    ask(client, [mat()])
    ask(client, [mat(source="some-unknown-site")])

    r = client.get("/api/v1/assistant/calls")
    assert r.status_code == 200, r.text
    outcomes = {c["outcome"] for c in r.json()["calls"]}
    assert outcomes == {"OK", "REJECTED"}, outcomes


# ==================================================== §5.5 写操作只给草稿
DRAFT = {"portfolio_id": "pf-syn-m", "trading_day": "2026-09-08"}


def _open_account(state, *, cash: int = 100_000_000) -> None:
    """先给模拟账户建一笔期初资金。

    不建账时预览会以 INSUFFICIENT_CASH 拒绝（现金 0）——助手会如实转达
    这个错误，但那样用例测的就不是草稿而是报错了。
    """

    from datetime import datetime, timezone

    state.service._ensure_account("pf-syn-m", initial_cash_cents=cash,
                                  initial_lots=[],
                                  now=datetime(2026, 9, 8, tzinfo=timezone.utc))


def test_draft_request_returns_preview_without_writing(ctx):
    """涉及写操作时只生成草稿与差异预览，绝不落库（§5.5、A08）。"""

    client, con, _stub = ctx
    _open_account(client.app.state.aquant)

    def counts():
        return {t: con.execute(f'SELECT COUNT(*) AS n FROM "{t}"').fetchone()["n"]
                for t in ("simulation_plan", "order", "fill", "cash_entry",
                          "position_lot")}

    before = counts()
    r = client.post("/api/v1/assistant/messages",
                    json={"purpose": "去掉某行业后组合如何变化",
                          "materials": [mat()], "draft": DRAFT},
                    headers=USER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("draft"), body
    assert body["draft"]["portfolioId"] == "pf-syn-m"
    assert "orders" in body["draft"]
    assert body["draft"]["frozen"] is False, "草稿不得是已冻结状态"
    assert "显式确认" in body["draftNote"], body["draftNote"]

    after = counts()
    assert after == before, (
        "草稿请求改动了账本：" + str({k: (before[k], after[k])
                                       for k in before if before[k] != after[k]}))


def test_two_drafts_are_independent_previews(ctx):
    """两次草稿各自独立：预览不入库，所以不应该互相覆盖或复用。"""

    client, con, _stub = ctx
    _open_account(client.app.state.aquant)
    ids = []
    for _ in range(2):
        body = client.post("/api/v1/assistant/messages",
                           json={"purpose": "草稿", "materials": [mat()],
                                 "draft": DRAFT}, headers=USER).json()
        ids.append(body["draft"]["planId"])
    assert all(ids), ids
    assert con.execute("SELECT COUNT(*) AS n FROM simulation_plan").fetchone()["n"] == 0, (
        "草稿落库了——那就不再是「只算不写」")


def test_answer_without_draft_has_no_draft_field(ctx):
    client, _con, _stub = ctx
    body = ask(client, [mat()]).json()
    assert "draft" not in body


def test_empty_materials_is_rejected(ctx):
    client, _con, stub = ctx
    r = client.post("/api/v1/assistant/messages",
                    json={"purpose": "测试", "materials": []}, headers=USER)
    assert r.status_code == 422
    assert stub.calls == []
