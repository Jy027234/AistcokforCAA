"""证据读取接口（§9、§15.3）。

这条例用锁住一个容易做反的默认值：接口**默认返回全部引用**，
包括定位不到的。不可定位的引用是"模型引用了原文里没有的话"
这一事实，默认藏起来它就永远不会被看到。
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.evidence.store import record_evidence  # noqa: E402
from main import build_state, create_app  # noqa: E402

SOURCE = "重要内容提示：A股每股现金红利28.02423元，股权登记日2026/6/25"


@pytest.fixture()
def ctx(tmp_path):
    state = build_state(tmp_path)
    app = create_app(state=state)
    with TestClient(app) as client:
        yield client, state.con


def _seed(con) -> None:
    # 快照里已有这些证券；来源登记也已由快照构建完成
    # 合成快照只登记了 synthetic-fixture 这一个来源，所以这里用它。
    # 证据的来源必须**已登记**——未登记会被外键拦下，这是刻意的：
    # 一份来源不明的证据不该悄悄进库。（真实快照那边登记的是 cninfo。）
    record_evidence(
        con, instrument_id="SYN.A.600519", source_id="synthetic-fixture",
        source_url="http://example.invalid/x.PDF", source_title="合成示例公告",
        source_text=SOURCE,
        available_at=datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc),
        fact_summary="合成证据", verification_status="VERIFIED",
        extra={"announced_on": "2026-09-07",
               "structured": [{"name": "per_share_amount", "value_text": "0.5"}],
               "citations": ["A股每股现金红利28.02423元", "原文里没有的句子"]})


def _mine(client, **params):
    """只看**本用例播的证据**。

    合成夹具里本来就带一个事件（用于验证不可定位引用会被标出来），
    所以断言必须针对自己播的那一条，否则数出来的条数会随夹具变化。
    """

    r = client.get("/api/v1/instruments/SYN.A.600519/evidence", params=params)
    assert r.status_code == 200, r.text
    return [row for row in r.json()["evidence"]
            if row["factSummary"] == "合成证据"]


def test_evidence_default_includes_unlocatable_citations(ctx):
    client, con = ctx
    _seed(con)
    rows = _mine(client)
    assert len(rows) == 2, rows
    located = {row["located"] for row in rows}
    assert located == {True, False}, (
        "默认视图必须同时包含可定位与不可定位的引用")


def test_located_only_filters_out_the_unlocatable(ctx):
    client, con = ctx
    _seed(con)
    rows = _mine(client, located_only=True)
    assert len(rows) == 1, rows
    assert rows[0]["located"] is True
    assert rows[0]["locatorStart"] is not None


def test_unknown_instrument_returns_empty_not_error(ctx):
    client, _con = ctx
    r = client.get("/api/v1/instruments/SH.999999/evidence")
    assert r.status_code == 200
    assert r.json()["evidence"] == []


def test_unlocatable_citation_is_visible_in_the_default_view(ctx):
    """不可定位的引用必须能在默认视图里被看到。

    这就是这条接口默认值的理由：如果 located_only 是默认，
    "模型引用了原文里没有的话"这件事永远不会有人发现。
    """

    client, con = ctx
    _seed(con)
    unlocatable = [r for r in _mine(client) if r["located"] is False]
    assert unlocatable, "默认视图里看不到不可定位的引用——它被静默过滤了"
    assert unlocatable[0]["quote"] == "原文里没有的句子"
