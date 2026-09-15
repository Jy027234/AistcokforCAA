"""研究卡的证据串联（§5.3、§9、§15.3）。

Q4 要证明的是"作业产出的证据真的到了卡片上"，而不是"两个接口各自能用"。
因此这里断言的是**卡片内容**：

  1. 证据带引用与原文片段出现在卡片上；
  2. 不可定位的引用**不被过滤**，并在卡片上标出来；
  3. 不可核验的证据自动成为卡片的一条反证——
     "我们不掌握的事实要自己说出来"，而不是留个空栏；
  4. 卡片留档后重复打开仍是同一张（与留档语义一致）。
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
from main import SNAPSHOT_ID, build_state, create_app  # noqa: E402

INSTRUMENT = "SYN.A.600519"
DAY = "2026-09-08"
SOURCE = "重要内容提示：A股每股现金红利0.50元，股权登记日2026/9/7"


@pytest.fixture()
def ctx(tmp_path):
    state = build_state(tmp_path)
    app = create_app(state=state)
    with TestClient(app) as client:
        yield client, state.con


def _seed(con, *, extra_citation: str | None = None) -> str:
    cites = ["A股每股现金红利0.50元"]
    if extra_citation:
        cites.append(extra_citation)
    bundle = record_evidence(
        con, instrument_id=INSTRUMENT, source_id="synthetic-fixture",
        source_url="http://example.invalid/x.PDF", source_title="合成示例公告",
        source_text=SOURCE,
        available_at=datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc),
        fact_summary="合成分红公告：每股 0.50 元",
        verification_status="VERIFIED",
        extra={"announced_on": "2026-09-07",
               "structured": [{"name": "per_share_amount", "value_text": "0.5"}],
               "citations": cites})
    return bundle.event_id


def card(client):
    r = client.get(f"/api/v1/instruments/{INSTRUMENT}/research",
                   params={"trading_day": DAY})
    assert r.status_code == 200, r.text
    return r.json()


def test_seeded_evidence_appears_on_the_card(ctx):
    client, con = ctx
    _seed(con)
    body = card(client)
    quotes = [e.get("quote") for e in body["evidence"]]
    assert "A股每股现金红利0.50元" in quotes, (
        "作业产出的证据没有出现在卡片上：evidence=" + str(body["evidence"])[:200])
    item = next(e for e in body["evidence"]
                if e.get("quote") == "A股每股现金红利0.50元")
    assert item["citationId"], "卡片上的证据必须带引用标识，否则无法核对"
    assert item["documentId"], "卡片上的证据必须带文档标识"
    assert item["located"] is True
    assert not item["note"], "可定位的引用不该带无法核验的说明"


def test_unlocatable_citation_is_shown_not_filtered(ctx):
    """不可定位的引用必须在卡片上出现，并被标出来。"""

    client, con = ctx
    _seed(con, extra_citation="这句话原文里根本没有")
    body = card(client)
    bad = [e for e in body["evidence"]
           if e.get("quote") == "这句话原文里根本没有"]
    assert bad, "不可定位的引用被卡片过滤掉了"
    assert bad[0]["located"] is False
    assert bad[0]["note"], "不可定位的引用必须带说明，否则看起来像正常引用"


def test_unverifiable_evidence_becomes_counter_evidence(ctx):
    """不可核验的证据要自动成为反证，而不是留个空栏。"""

    client, con = ctx
    _seed(con, extra_citation="这句话原文里根本没有")
    body = card(client)
    statements = " ".join(c.get("statement", "") + (c.get("note") or "")
                          for c in body["counterEvidence"])
    assert "定位不到" in statements, (
        "存在不可定位引用，卡片却没有把它列为反证："
        + str(body["counterEvidence"])[:300])


def test_card_without_evidence_says_so_explicitly(ctx):
    """没有证据时也要有明确说法（§5.3 至少一个反证或明确未找到）。"""

    client, _con = ctx
    body = card(client)
    assert body["counterEvidence"], "反证栏不得为空"
    assert any(c.get("noneFound") for c in body["counterEvidence"]), (
        "没有证据时应当明确写「未找到反证」，而不是留空")


def test_card_is_archived_once_per_day(ctx):
    """留档语义：同一天重复打开是同一张卡片。"""

    client, con = ctx
    _seed(con)
    first, second = card(client), card(client)
    assert first["cardId"] == second["cardId"]
    assert first["generatedAt"] == second["generatedAt"]
    assert SNAPSHOT_ID in first["cardId"]
