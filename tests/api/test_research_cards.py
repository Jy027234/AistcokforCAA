"""研究卡片留档（主文档 §5.3、§14.1）。

卡片原先每次请求现算、算完即弃。这条用例锁住留档的几条性质：

  1. 打开研究卡片会**落库**，返回体带上 cardId；
  2. 同一 (标的, 快照, 交易日) 重复打开得到**同一张**卡片，
     生成时刻不刷新——它是"当时看到的证据"，不是每次新拍的快照；
  3. 换一个交易日就是**另一张**卡片，历史可以逐日追溯；
  4. 留档内容里带着数据模式：合成数据的卡片永远不能变成"真实数据"。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "src"))

from main import SNAPSHOT_ID, build_state, create_app  # noqa: E402

INSTRUMENT = "SYN.A.600519"
DAY_ONE = "2026-09-08"
DAY_TWO = "2026-09-09"


@pytest.fixture()
def ctx(tmp_path):
    state = build_state(tmp_path)
    app = create_app(state=state)
    with TestClient(app) as client:
        yield client, state.con


def _research(client, day: str, instrument: str = INSTRUMENT):
    return client.get(f"/api/v1/instruments/{instrument}/research",
                      params={"trading_day": day})


def test_opening_a_card_persists_it(ctx):
    client, con = ctx
    r = _research(client, DAY_ONE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("cardId"), "返回体必须带上卡片标识，否则前端无法引用它"

    row = con.execute("SELECT * FROM research_card WHERE card_id=?",
                      (body["cardId"],)).fetchone()
    assert row is not None, "研究卡片没有落库"
    assert row["instrument_id"] == INSTRUMENT
    assert row["snapshot_id"] == SNAPSHOT_ID
    assert row["data_mode"] == "SYNTHETIC", "合成数据的卡片不得标成真实数据"


def test_same_day_reopens_the_same_card(ctx):
    """同一天重复打开必须是同一张：否则历史无从对照。"""

    client, con = ctx
    first = _research(client, DAY_ONE).json()
    second = _research(client, DAY_ONE).json()

    assert first["cardId"] == second["cardId"]
    assert first["generatedAt"] == second["generatedAt"], (
        "生成时刻被刷新了，说明又在现算而不是读留档")

    n = con.execute("SELECT COUNT(*) AS n FROM research_card WHERE instrument_id=? "
                    "AND snapshot_id=?", (INSTRUMENT, SNAPSHOT_ID)).fetchone()["n"]
    assert n == 1, f"同一天同一标的留下了 {n} 张卡片"


def test_different_day_is_a_different_card(ctx):
    """换交易日就是另一张：卡片绑定生成时点，不能互相覆盖。"""

    client, con = ctx
    a = _research(client, DAY_ONE).json()
    b = _research(client, DAY_TWO).json()
    assert a["cardId"] != b["cardId"]

    ids = {r["card_id"] for r in
           con.execute("SELECT card_id FROM research_card WHERE instrument_id=?",
                       (INSTRUMENT,))}
    assert {a["cardId"], b["cardId"]} <= ids


def test_card_history_is_queryable(ctx):
    client, _con = ctx
    a = _research(client, DAY_ONE).json()
    b = _research(client, DAY_TWO).json()

    r = client.get("/api/v1/research/cards",
                   params={"instrument_id": INSTRUMENT, "snapshot_id": SNAPSHOT_ID})
    assert r.status_code == 200, r.text
    ids = [c["card_id"] for c in r.json()["cards"]]
    assert a["cardId"] in ids and b["cardId"] in ids

    # 历史列表必须返回首次展示时冻结的完整响应，而不是旧摘要表里能拼出的
    # 少数字段；否则 actions、名称和时点等证据会在历史视图中消失。
    stored = next(c for c in r.json()["cards"] if c["card_id"] == a["cardId"])
    assert stored["cardId"] == a["cardId"]
    assert stored["instrumentId"] == a["instrumentId"]
    assert stored["displayName"] == a["displayName"]
    assert stored["actions"] == a["actions"]
    assert stored["asOfTime"] == a["asOfTime"]

    # 未覆盖的标的应当返回空表，而不是报错或返回别人的卡片
    other = client.get("/api/v1/research/cards",
                       params={"instrument_id": "SYN.A.999999"})
    assert other.status_code == 200
    assert other.json()["cards"] == []


def test_stored_card_keeps_rank_semantics_and_limitations(ctx):
    """留档要能独立回答"当时看到什么"，因此排名语义与限制必须一起存。"""

    client, con = ctx
    body = _research(client, DAY_ONE).json()
    row = con.execute("SELECT quality_label,numeric_json,uncertainty_json,"
                      "limitations_json,counter_evidence_json FROM research_card "
                      "WHERE card_id=?", (body["cardId"],)).fetchone()

    import json
    numeric = json.loads(row["numeric_json"])
    assert "排名" in (numeric.get("rankSemantics") or ""), numeric.get("rankSemantics")
    assert "概率" in (numeric.get("rankSemantics") or ""), (
        "排名语义必须显式说明它不是概率")
    # §5.3 至少一个有效反证或明确未找到反证——留档里也必须有这一条
    counter = json.loads(row["counter_evidence_json"])
    assert counter, "反证栏不得为空"
