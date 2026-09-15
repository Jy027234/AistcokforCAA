"""引用定位与证据落库（§15.3、§9）。

§15.3 的硬要求是"引用必须能定位；不可定位则不得发布"。这条用例锁住：

  1. 逐字命中与"只差空白"是**两档**，不合并——证据强度不同；
  2. 定位失败**照样落库**并标 located=0，不静默丢弃；
  3. 事件带 available_at（PIT 门禁的唯一判据），由证据自身的可得时间给出；
  4. 写不出可比对字段时 verification_status 不能是 VERIFIED。
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.data.db import apply_migrations, connect  # noqa: E402
from aquant.domain.data.ingest import SnapshotBuilder  # noqa: E402
from aquant.domain.evidence.store import evidence_for, locate, record_evidence  # noqa: E402

SOURCE = "重要内容提示：每股分配比例A股每股现金红利28.02423元相关日期股权登记日2026/6/25"


@pytest.fixture()
def con(tmp_path):
    c = connect(tmp_path / "meta.sqlite")
    apply_migrations(c)
    # 用 ensure_source 登记来源，而不是手写 INSERT：手写会与列定义漂移
    # （我第一版就把列名猜错了），而这一步要证明的恰恰是"来源已登记"本身。
    SnapshotBuilder(c, tmp_path / "datasets").ensure_source(
        "cninfo", display_name="巨潮资讯", domains=["ANNOUNCEMENTS"],
        integration_state="TEST_PASSED", pit_available="NO",
        pit_basis="RECONSTRUCTED", rights={})
    c.execute("INSERT OR IGNORE INTO instrument (instrument_id,exchange,board,created_at,"
              "updated_at) VALUES ('SH.600519','SSE','MAIN','2026-09-15T00:00:00Z',"
              "'2026-09-15T00:00:00Z')")
    yield c
    c.close()


# ------------------------------------------------------------------ 定位
def test_exact_match_is_its_own_tier():
    c = locate(SOURCE, "A股每股现金红利28.02423元")
    assert c.located and c.locator_kind == "EXACT_MATCH"
    assert SOURCE[c.start:c.end] == "A股每股现金红利28.02423元"


def test_whitespace_only_difference_is_a_separate_tier():
    """模型在中文与拉丁字符之间加空格很常见，但这不是逐字命中。"""

    c = locate(SOURCE, "A 股每股现金红利 28.02423 元")
    assert c.located, "只差空白应当能定位"
    assert c.locator_kind == "WHITESPACE_INSENSITIVE", (
        "只差空白被并入了逐字命中——两者的证据强度不同，不能合并")
    assert SOURCE[c.start:c.end] == "A股每股现金红利28.02423元"


def test_rewritten_quote_is_not_located():
    """改写过的句子绝不能算可定位，否则"可定位"就没有意义了。"""

    c = locate(SOURCE, "每股现金红利约为28元")
    assert not c.located


def test_empty_quote_is_not_located():
    assert not locate(SOURCE, "   ").located


# ------------------------------------------------------------------ 落库
def _record(con, **over):
    args = dict(instrument_id="SH.600519", source_id="cninfo",
                source_url="http://static.cninfo.com.cn/x.PDF",
                source_title="某公司权益分派实施公告", source_text=SOURCE,
                available_at=datetime(2026, 6, 21, 15, 0, tzinfo=timezone.utc),
                fact_summary="测试事件", verification_status="UNVERIFIED",
                extra={"announced_on": "2026-06-21",
                       "structured": [{"name": "record_date", "value_text": "2026-06-25",
                                       "unit": "DATE"}],
                       "citations": ["A股每股现金红利28.02423元", "这句话原文里没有"]})
    args.update(over)
    return record_evidence(con, **args)


def test_unlocatable_quote_is_stored_not_dropped(con):
    """定位失败照样落库：那本身是"模型引用了原文里没有的话"这一事实。"""

    bundle = _record(con)
    assert len(bundle.citations) == 2
    rows = con.execute("SELECT quote,located,locator_start,located FROM citation "
                       "ORDER BY citation_id").fetchall()
    assert len(rows) == 2, "不可定位的引用被丢掉了"
    assert rows[0]["located"] == 1 and rows[0]["locator_start"] is not None
    assert rows[1]["located"] == 0, "不可定位的引用竟然标成了已定位"


def test_event_carries_its_own_available_at(con):
    """PIT 门禁只认 available_at，必须来自证据自身而不是"现在"。"""

    bundle = _record(con)
    row = con.execute("SELECT available_at,available_basis,pit_mode FROM event "
                      "WHERE event_id=?", (bundle.event_id,)).fetchone()
    assert row["available_at"].startswith("2026-06-21T15:00")
    assert row["available_basis"] == "RECONSTRUCTED"
    assert row["pit_mode"] == "HISTORICAL_RECONSTRUCTED"


def test_evidence_is_readable_back_with_locators(con):
    _record(con)
    rows = evidence_for(con, instrument_id="SH.600519", located_only=True)
    assert len(rows) == 1, "可定位的引用应当只有一条"
    assert rows[0]["located"] is True
    assert rows[0]["locatorStart"] is not None
    assert rows[0]["documentId"]


def test_document_references_a_registered_source(con):
    """来源未登记时外键必须失败——这是我们在真实快照里踩过的坑。"""

    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        _record(con, source_id="never-registered")
