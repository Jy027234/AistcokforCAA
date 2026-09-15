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

# ======================================================== 只读查询端点
def test_ledger_exposes_entries_lots_fills_and_fees(client):
    """账本端点返回逐条事实，与 reconcile 的"对不对"分工不同。"""

    _run_plan(client)   # 先产生一些账本事实
    r = client.get("/api/v1/portfolios/pf-syn-m/ledger")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["portfolio_id"] == "pf-syn-m"
    assert body["account_type"] == "SIMULATED"

    # 金额一律整数分 + 展示串
    assert isinstance(body["cash"]["cents"], int)
    assert body["cash"]["display"].endswith("元")
    assert body["cash"]["entry_count"] == len(body["cash"]["entries"])
    assert body["cash"]["entries"], "执行后应至少有一条现金分录"

    # 每条分录的金额形状一致
    for entry in body["cash"]["entries"]:
        assert isinstance(entry["amount"]["cents"], int)

    # 现金合计必须等于分录之和——账本自洽
    total = sum(e["amount"]["cents"] for e in body["cash"]["entries"])
    assert total == body["cash"]["cents"]

    assert body["fills"], "执行后应有成交记录"
    for fill in body["fills"]:
        assert fill["price"]["cents"] > 0
        assert fill["side"] in ("BUY", "SELL")

    assert body["lots"], "买入后应有持仓批次"


def test_ledger_is_read_only(client):
    """账本端点是只读的：调两次结果必须一致。"""

    _run_plan(client)
    first = client.get("/api/v1/portfolios/pf-syn-m/ledger").json()
    second = client.get("/api/v1/portfolios/pf-syn-m/ledger").json()
    assert first == second


def test_unknown_portfolio_ledger_is_404(client):
    r = client.get("/api/v1/portfolios/pf-does-not-exist/ledger")
    assert r.status_code == 404


def test_events_are_filtered_by_decision_time(client):
    """事件必须按 available_at 门禁过滤，而不是"库里有就返回"。"""

    r = client.get("/api/v1/events")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["snapshotId"]
    assert body["asOfTime"]
    # 每个返回的事件都必须在决策时点之前可用
    for event in body["events"]:
        assert event["available_at"] <= body["asOfTime"], event["event_id"]


def test_events_carry_citation_locatability(client):
    """引用是否可定位必须显式给出（§15.3）。"""

    body = client.get("/api/v1/events").json()
    for event in body["events"]:
        assert "has_located_citation" in event
        assert isinstance(event["citations"], list)


def test_readiness_reports_dimensions_separately(client):
    r = client.get("/api/v1/readiness")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "data" in body and "jobs" in body
    assert body["data"]["snapshotId"]
    assert isinstance(body["jobs"], dict)


def test_job_endpoints_are_read_only_shapes(client):
    listing = client.get("/api/v1/jobs")
    assert listing.status_code == 200
    assert "counts" in listing.json()

    missing = client.get("/api/v1/jobs/job-does-not-exist")
    assert missing.status_code == 404


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


def _seed_position(client, instrument_id: str, shares: int) -> None:
    """直接给账户建一笔底仓。

    分红用例需要的持仓，不能靠"跑一轮组合构建碰巧买到"来获得：
    构建结果取决于持仓上限与权重上限，任何一次参数调整都会让它换标的，
    于是分红用例会以一种与分红毫无关系的方式失败。
    """

    from datetime import date as _date, datetime as _dt, timezone as _tz

    from aquant.domain.simulation.simulator import Lot

    service = client.app.state.aquant.service
    lot = Lot(
        lot_id=f"lot-seed-{instrument_id}", instrument_id=instrument_id,
        acquired_trading_day=_date(2026, 9, 7), earliest_sellable_day=_date(2026, 9, 7),
        quantity_original=shares, quantity_remaining=shares,
        cost_basis_cents_per_share=1000,
    )
    service._ensure_account("pf-syn-m", initial_cash_cents=100_000_000,
                            initial_lots=[lot], now=_dt(2026, 9, 8, tzinfo=_tz.utc))


def test_execute_recognises_dividend_receivable_on_ex_date(client):
    """除权日执行时必须落一条应收，且**现金不得因此增加**。

    这里验的是"执行链路把分红落库了"，因此底仓用显式建仓获得，
    不依赖组合构建买到哪只。权利算法本身由
    tests/golden/test_dividend_persistence.py 覆盖。
    """

    _seed_position(client, "SYN.A.600519", 100)

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


def test_freezing_the_same_plan_twice_is_idempotent(client):
    """重复冻结同一份内容必须幂等，而不是 500。

    真实的网页交互会走到这里：用户确认冻结后，冻结结果只存在于前端内存里；
    一旦刷新页面（或再点一次），前端会重新预览、重新取令牌、再冻结一次。
    此时幂等键（组合 + 计划版本 + 账户版本）完全相同，而计划已经存在，
    于是 INSERT 撞上唯一约束——原先直接让 sqlite3.IntegrityError 冒到
    API 层返回 500 Internal Server Error，一次无害的重复点击看起来
    像服务器崩了。
    """

    pid = preview(client).json()["planId"]
    first = freeze(client, pid, confirm(client, pid).json()["confirmationToken"])
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "FROZEN"
    assert not first.json().get("idempotent_replay")

    # 同一份计划、同一个账户状态，再冻结一次
    again = freeze(client, pid, confirm(client, pid).json()["confirmationToken"])
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["idempotent_replay"] is True, body
    assert body["plan_id"] == pid

    # 幂等返回不等于又冻了一份：计划必须只有一条
    plans = client.app.state.aquant.service
    count = plans.con.execute(
        "SELECT COUNT(*) AS n FROM simulation_plan WHERE idempotency_key=?",
        (body["idempotency_key"],)).fetchone()["n"]
    assert count == 1, "重复冻结不得产生第二条计划"


def test_another_subject_cannot_claim_the_frozen_plan(client):
    """幂等只在**同一主体**内成立。

    换个人来冻结同一份内容不能拿到别人的冻结结果——那等于把属于
    另一个用户的计划回给调用方。
    """

    pid = preview(client).json()["planId"]
    assert freeze(client, pid, confirm(client, pid).json()["confirmationToken"])         .status_code == 200

    bob = {"X-Aquant-Subject": "user:bob"}
    token = client.post(f"/api/v1/plans/{pid}/confirmation", headers=bob)         .json()["confirmationToken"]
    r = freeze(client, pid, token, headers=bob)
    assert r.status_code == 409, r.text
    assert "another subject" in r.json()["error"]["message"]


# ============================================================== 流动性筛选
def test_liquidity_threshold_is_actually_enforced(client):
    """配置里写着流动性下限，就必须真的筛。

    此前 `liquidity_min_avg_amount_cents` 只出现在参数类与示例配置里，
    没有任何代码读取它——使用者会以为组合已经按流动性筛过，
    实际上门槛完全没生效。这类"配置存在但不生效"比缺功能更危险，
    因为它看起来是打开的。
    """

    import dataclasses

    service = client.app.state.aquant.service
    original = service.params

    # 阈值低于任何合理成交额：不应排除任何人
    service.params = dataclasses.replace(original, liquidity_min_avg_amount_cents=1)
    r = preview(client)
    assert r.status_code == 200, r.text
    assert r.json()["excluded"] == [], r.json()["excluded"]

    # 阈值高到不可能达到：所有候选都应被排除，且给出可读原因
    service.params = dataclasses.replace(original, liquidity_min_avg_amount_cents=10 ** 15)
    r2 = preview(client)
    assert r2.status_code == 200, r2.text
    excluded = r2.json()["excluded"]
    assert excluded, "阈值极高时必须筛掉候选"
    assert all(e["reason"] == "LIQUIDITY_BELOW_MINIMUM" for e in excluded), excluded
    assert all("均成交额" in e["detail"] for e in excluded), excluded

    service.params = original


def test_missing_turnover_does_not_pass_the_liquidity_gate(client):
    """没有成交额时必须排除并说明，不得当作"通过"。

    免费源的腾讯日线只提供成交量、没有成交额。若把"没有数据"当成
    "没超限"，门槛就形同虚设；若用 价格×成交量 估算，则是拿一个
    未观测的数字做准入判断。
    """

    import dataclasses

    service = client.app.state.aquant.service
    original = service.params
    # 参数是不可变的实验版本（§11.2），改阈值要换成新版本而不是原地改
    service.params = dataclasses.replace(original, liquidity_min_avg_amount_cents=1)

    # 抹掉候选的成交额，模拟只有量的数据源
    real_reader = service.reader
    original_daily = real_reader.daily_quotes

    def without_amount(*args, **kwargs):  # noqa: ANN001, ANN202
        rows = original_daily(*args, **kwargs)
        return [type(r)(**{**{f: getattr(r, f) for f in r.__slots__},
                           "amount_cents": None}) for r in rows]

    real_reader.daily_quotes = without_amount
    try:
        r = preview(client)
        assert r.status_code == 200, r.text
        # 候选被筛掉之后可能没有订单，但排除原因必须出现
        excluded = r.json()["excluded"]
        assert excluded, "无成交额时不得静默放行"
        assert all(e["reason"] == "LIQUIDITY_UNVERIFIED" for e in excluded), excluded
        assert all("估算" in e["detail"] for e in excluded), excluded
    finally:
        real_reader.daily_quotes = original_daily
        service.params = original


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
    """每股股利必须为正；0 或负数不得进入账本。

    两道防线，任一拦下都算正确，因此两种信封都接受：
      * 请求模型校验（pydantic）-> 422；
      * 领域构造 CashDividend 时校验 -> 409 + CORPORATE_ACTION_UNSUPPORTED。
    断言写成"必须被拒绝并给出结论"，而不是钉死某个状态码——
    钉死状态码会让一道防线的存在把另一道防线的测试变成假失败。
    """

    pid = preview(client).json()["planId"]
    # 注意键名是**对外契约** cash_per_share_cents（单位分），
    # 不要跟着领域层的过渡参数名一起改。
    bad = dict(DIVIDEND, cash_per_share_cents=0)
    r = client.post(f"/api/v1/plans/{pid}/execute",
                    json={"plan_id": pid, "corporate_actions": [bad]}, headers=USER)
    assert r.status_code in (409, 422), r.text
    if r.status_code == 409:
        assert r.json()["error"]["code"] == "CORPORATE_ACTION_UNSUPPORTED"
        assert "positive" in r.json()["error"]["message"], r.json()


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

