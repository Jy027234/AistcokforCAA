"""研究卡上的因子数值必须**真的来自落库的研究运行**。

为什么这条用例存在
-----------------
`build_research_card` 一直有一个 `factor_values` 关键字参数，默认 None，
而**没有任何调用方传它**。于是：

  * 因子计算（F10）在真实数据上算得出 832 个值，验收报告 PASS；
  * 快照库里的 `research_run` / `feature_value` 是 0 行——因为流水线跑的是
    "在内存里算一遍并写报告"的验收脚本，不是落库路径；
  * 研究卡接口自然也没有数值。

三件事各自都"正常"，合起来是"产品上没有因子"。两侧的测试都是绿的：
卡片层把 None 当成"这只没有因子值"，而那与"根本没算过"看起来完全一样。

因此这里断言的不是"接口 200"，而是**数值能从库里走到响应体上**，
以及**没有数值时必须说明原因**。

关于夹具
--------
夹具**自建快照库**而不是复用 `apps/api` 的自举：`publish` 之后快照 ID
不可变（`snapshot` 表上有触发器，连 DELETE 都会被拒绝），而本用例需要
"库里一开始就带着 financials 数据集"。绕开那条不变量的做法是手工拼一份
损坏的库——那会让用例验的是一个不存在的世界。因此这里用真实的
`SnapshotBuilder` + `SnapshotStore.publish` 从零建一份。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.data.db import apply_migrations, connect  # noqa: E402
from aquant.domain.data.ingest import SnapshotBuilder  # noqa: E402
from aquant.domain.data.snapshot import (  # noqa: E402
    DataMode,
    DatasetRef,
    SnapshotDraft,
    SnapshotStore,
)
from aquant.domain.research.f10 import compute_f10_for_snapshot  # noqa: E402
from main import SNAPSHOT_ID, build_state, create_app  # noqa: E402

#: 沿用产品默认的合成快照 ID。刻意不另造一个 ID：`resolve_fee_table`
#: 对"非默认 ID"的快照要求真实券商费率（§12.6 的费率闸门），
#: 而本用例要验的是因子接线，不是费率闸门——另造 ID 只会顺带把闸门
#: 也考一遍，失败信息还会指向费率，掩盖真正的断言。
SNAPSHOT = SNAPSHOT_ID
INSTRUMENT = "SYN.A.600519"
TRADING_DAY = "2026-09-11"
CUTOFF = datetime(2026, 9, 11, 12, 30, tzinfo=timezone.utc)
AS_OF = datetime(2026, 9, 11, 20, 30, tzinfo=timezone.utc)

TRADING_DAYS = ["2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"]


def _statement(stat: str, pub: str, profit: str) -> dict:
    return {"statDate": stat, "pubDate": pub, "netProfit": profit,
            "totalShare": "1256197800", "roeAvg": "0.15",
            "epsTTM": "2.0", "CFOToNP": "1.1", "MBRevenue": ""}


def _periods(this_year_q1: str) -> dict:
    """跨年的六个报告期。TTM 需要"本期 + 上年同期"，缺一个就只能被排除。"""

    return {
        "2025Q1": _statement("2025-03-31", "2025-04-20", "10000000000"),
        "2025Q2": _statement("2025-06-30", "2025-07-20", "20000000000"),
        "2025Q3": _statement("2025-09-30", "2025-10-20", "30000000000"),
        "2025Q4": _statement("2025-12-31", "2026-03-20", "40000000000"),
        "2026Q1": _statement("2026-03-31", "2026-04-20", this_year_q1),
        "2026Q2": _statement("2026-06-30", "2026-07-20", "22000000000"),
    }


def _bars(close_cents: int = 150000) -> list[dict]:
    return [{
        "trading_day": day, "open_cents": close_cents, "high_cents": close_cents,
        "low_cents": close_cents, "close_cents": close_cents,
        "volume_shares": 1000000, "amount_cents": close_cents * 1000000,
        "prev_close_cents": close_cents,
    } for day in TRADING_DAYS]


def _manifest() -> dict:
    """两份行情（一只带财务、一只不带）+ 五个交易日。"""

    instruments = []
    quotes = []
    for iid, name in ((INSTRUMENT, "合成贵州茅台"), ("SYN.A.600003", "合成无财报")):
        instruments.append({
            "instrument_id": iid, "exchange": "SSE", "board": "MAIN",
            "security_class": "EQUITY", "short_name": name,
            "listed_on": "2001-08-27",
            "industry_code": "C15", "industry_name": "酒、饮料和精制茶制造业",
            "classification_version": "CSRC-2012",
            "status_history": [{
                "valid_from": TRADING_DAYS[0], "valid_to": None, "name": name,
                "status": "LISTED", "industry_code": "C15",
                "industry_name": "酒、饮料和精制茶制造业",
                "classification_version": "CSRC-2012",
            }],
        })
        for bar in _bars():
            quotes.append({"instrument_id": iid, **bar})
    return {
        "schema_version": "aquant.real_dataset.v1",
        "data_mode": "SYNTHETIC",
        "watermark": "SYNTHETIC FIXTURE — 本快照的行情是构造出来的",
        "disclaimer": "构造数据，仅用于验证接口接线。",
        "as_of_time": AS_OF.isoformat(),
        "input_cutoff_at": CUTOFF.isoformat(),
        "published_at": AS_OF.isoformat(),
        "trading_days": TRADING_DAYS,
        "instruments": instruments,
        "daily_quotes": quotes,
        "corporate_actions": [],
        "events": [],
        "financials": {
            "created_at": "2026-09-11T00:00:00+00:00",
            "source_id": "synthetic-fixture",
            "statements": {INSTRUMENT: _periods("12000000000")},
        },
    }


def _publish(data_dir: Path) -> tuple:
    """建一份已发布的快照。返回 (con, store)。"""

    con = connect(data_dir / "meta.sqlite", allow_thread_sharing=True)
    apply_migrations(con)
    root = data_dir / "api"
    root.mkdir(parents=True, exist_ok=True)
    store = SnapshotStore(con, root)
    builder = SnapshotBuilder(con, root / "datasets")
    builder.ensure_source(
        "synthetic-fixture", display_name="合成夹具",
        domains=["DAILY_QUOTES", "CALENDAR_IDENTITY", "CORPORATE_ACTIONS",
                 "ANNOUNCEMENTS"],
        integration_state="TEST_PASSED", pit_available="NO",
        pit_basis="RECONSTRUCTED",
    )
    doc = _manifest()
    builder.ingest(doc, source_id="synthetic-fixture", data_version="syn-factors")
    refs = builder.write_datasets(doc, snapshot_id=SNAPSHOT)
    store.publish(SnapshotDraft(
        snapshot_id=SNAPSHOT, kind="EOD", data_mode=DataMode.SYNTHETIC,
        input_cutoff_at=CUTOFF, as_of_time=AS_OF,
        created_at=AS_OF, published_at=AS_OF,
        code_version="0.1.0", data_version="syn-factors",
        watermark=doc["watermark"], pool_hash="sha256:" + "d" * 64,
        datasets=[DatasetRef(name=r["name"], path=r["path"], sha256=r["sha256"],
                             record_count=r["record_count"],
                             # 数据集上界必须 <= input_cutoff_at（§15.4）：
                             # 晚于截止时点的数据不得进入快照。
                             as_of_upper_bound=CUTOFF) for r in refs],
    ))
    return con, store


@pytest.fixture()
def client_only(tmp_path, monkeypatch):
    """有快照、没有研究运行。"""

    monkeypatch.setenv("AQUANT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AQUANT_SNAPSHOT_ID", SNAPSHOT)
    con, _store = _publish(tmp_path)
    state = build_state(tmp_path)
    app = create_app(state=state)
    with TestClient(app) as client:
        yield client, state, con


@pytest.fixture()
def loaded(client_only):
    """有快照，且 F10 已落库。"""

    client, state, con = client_only
    con.execute("DELETE FROM research_run")
    con.execute("DELETE FROM feature_value")
    con.commit()
    summary = compute_f10_for_snapshot(
        con=con, reader=state.reader, snapshot_id=SNAPSHOT, as_of=AS_OF)
    con.commit()
    return client, state, con, summary


def test_fixture_is_sane(loaded):
    """前置条件：这份夹具必须真的算得出值，否则后面的断言没有意义。"""

    _client, _state, con, summary = loaded
    assert summary["valued"] > 0, summary
    assert con.execute("SELECT COUNT(*) FROM feature_value "
                       "WHERE raw_value IS NOT NULL").fetchone()[0] > 0


def test_card_carries_persisted_factor_values(loaded):
    client, _state, _con, summary = loaded

    r = client.get(f"/api/v1/instruments/{INSTRUMENT}/research",
                   params={"trading_day": TRADING_DAY})
    assert r.status_code == 200, r.text
    body = r.json()

    rows = body["rankBreakdown"]
    assert rows, ("研究卡没有因子数值——库里有 feature_value，"
                  "但接口没有把它传进卡片")
    f10 = rows[0]
    assert f10["factorId"] == "F10"
    assert f10["valueRaw"] is not None
    assert f10["value"] not in (None, "", "—"), f10
    # 排名标签由视图层算好（§14.1：前端不做数值格式化）
    assert f10["rankLabel"].endswith("%"), f10
    assert f10["rankPct"] is not None


def test_card_explains_why_a_value_is_missing(loaded):
    """没有数值时**必须给原因**，而不是让界面显示成"值为空"（§10.2）。"""

    client, _state, _con, _summary = loaded
    # 该标的在快照里没有财务记录 -> 只能被质量门排除
    r = client.get("/api/v1/instruments/SYN.A.600003/research",
                   params={"trading_day": TRADING_DAY})
    assert r.status_code == 200, r.text
    body = r.json()

    rows = body["rankBreakdown"]
    assert len(rows) == 1, rows
    assert rows[0]["factorId"] == "F10"
    # 算不出时**值必须为空而不是 0**——0 是一个具体且错误的数值
    assert rows[0]["valueRaw"] is None
    assert rows[0]["value"] == "—", rows[0]
    # 而"空"必须带原因，否则界面上"被排除"与"值为空"长得一样
    assert rows[0]["exclusionReason"], rows[0]
    assert rows[0]["exclusionLabel"], rows[0]
    assert rows[0]["exclusionLabel"] != rows[0]["exclusionReason"], (
        "原因必须有中文说明；取值表缺这一条时会把英文码原样显示给使用者")


def test_card_says_so_when_no_run_exists_at_all(client_only):
    """完全没有研究运行时，说明必须指向**能关掉这条链**的那一步。"""

    client, _state, con = client_only
    assert con.execute("SELECT COUNT(*) FROM research_run").fetchone()[0] == 0

    r = client.get(f"/api/v1/instruments/{INSTRUMENT}/research",
                   params={"trading_day": TRADING_DAY})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rankBreakdown"] == []
    joined = " ".join(body["uncertainties"])
    assert "尚未计算任何因子" in joined, body["uncertainties"]
    # 文案不得再指向一个关不掉这条链的工具：流水线在因子落库之前跑的是
    # 验收脚本，照着它做，卡片依然是空的。
    assert "tools/compute_factors.py" in joined, joined


def test_the_endpoint_passes_factor_values_to_the_card():
    """守卫：调用 `build_research_card` 时**必须**把因子值传进去。

    这是本文件针对的那个缺陷的最小形状——`factor_values` 有默认值 None，
    于是"忘了传"不会报错，只会让卡片静默地永远没有数值。参数有默认值
    的接口都可能这样烂掉，因此这里直接对着**真实调用点**断言，
    而不是再跑一次接口（接口已经在上面的用例里验过了）。
    """

    import inspect

    import main as api_main

    source = inspect.getsource(api_main.create_app)
    call = source.split("def research(")[1].split("def research_card_history")[0]
    assert "factor_values=factor_values" in call, (
        "研究卡接口没有把因子值传进 build_research_card；"
        "参数默认 None，因此这个疏漏不会报错，只会让卡片永远没有数值")
    assert "factor_values_for_snapshot(" in call, (
        "因子值必须来自落库的研究运行（factor_values_for_snapshot），"
        "不在这里现算")
