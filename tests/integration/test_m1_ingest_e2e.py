"""M1 数据与证据底座端到端测试。

链路：源清单 → 单位归一入库 → 快照数据集落盘（真实哈希）→ 发布（不可变）
      → 研究读取（PIT 门禁）。

这些测试同时覆盖主文档 §18.1 的 D01–D08 在**数据库与快照层**的落点，
与 tests/pit 的纯语义用例互补：那边测时点规则，这边测规则是否真的被接线。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.data.ingest import (
    IngestError,
    SnapshotBuilder,
    convert_amount,
    convert_volume,
)
from aquant.domain.data.snapshot import (
    DataMode,
    DatasetRef,
    SnapshotDraft,
    SnapshotError,
    SnapshotStore,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "snap-syn-001.yaml"


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


@pytest.fixture()
def env(tmp_path):
    con = connect(tmp_path / "meta.sqlite")
    apply_migrations(con)
    api_root = tmp_path / "api"
    api_root.mkdir()
    store = SnapshotStore(con, api_root)
    builder = SnapshotBuilder(con, api_root / "datasets")
    yield con, builder, store, api_root
    con.close()


def build_snapshot(con, builder, store, *, snapshot_id="snap-syn-001",
                   as_of_time: str | None = None, cutoff: str | None = None):
    doc = builder.load_manifest(EXAMPLE)
    builder.ensure_source(
        "synthetic-fixture",
        display_name="合成数据集（资料包自带）",
        domains=["DAILY_QUOTES", "CALENDAR_IDENTITY", "CORPORATE_ACTIONS", "ANNOUNCEMENTS"],
        integration_state="TEST_PASSED",
        pit_available="NO",
        pit_basis="RECONSTRUCTED",
    )
    report = builder.ingest(doc, source_id="synthetic-fixture", data_version="syn-2026-09-11")
    refs = builder.write_datasets(doc, snapshot_id=snapshot_id)

    def parse(ts: str) -> datetime:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))

    draft = SnapshotDraft(
        snapshot_id=snapshot_id,
        kind="EOD",
        data_mode=DataMode.SYNTHETIC,
        input_cutoff_at=parse(cutoff or doc["input_cutoff_at"]),
        as_of_time=parse(as_of_time or doc["as_of_time"]),
        created_at=utc(2026, 9, 11, 12, 30),
        published_at=parse(doc["published_at"]),
        code_version="0.1.0",
        data_version="syn-2026-09-11",
        watermark=doc["watermark"],
        pool_hash="sha256:" + "a" * 64,
        datasets=[
            DatasetRef(
                name=r["name"], path=r["path"], sha256=r["sha256"],
                record_count=r["record_count"],
                as_of_upper_bound=parse(r["as_of_upper_bound"]),
            )
            for r in refs
        ],
    )
    store.publish(draft)
    return doc, report


# ------------------------------------------------------------------ 单位归一
def test_volume_lot_converts_to_shares():
    """§6.3 "手"必须显式转换，不得靠列名猜测。"""

    assert convert_volume(32664, "LOT", object_id="x") == 3266400
    assert convert_volume(100, "SHARE", object_id="x") == 100


def test_amount_wan_yuan_converts_to_cents():
    # 424244.0861 万元 = 4,242,440,861 元 = 424,244,086,100 分
    assert convert_amount(424244.0861, "CNY_10000", object_id="x") == 424244086100
    # 资料包中 600519 在 09-07 的成交额即以此口径存储
    assert convert_amount(424244.0861, "CNY_10000", object_id="x") \
        == 42424408610 * 10


def test_unknown_units_are_rejected_not_guessed():
    with pytest.raises(IngestError) as exc:
        convert_volume(1, "BOARD_LOT", object_id="x")
    assert "never guess" in exc.value.repair_action
    with pytest.raises(IngestError):
        convert_amount(1, "DOLLARS", object_id="x")


# ------------------------------------------------------------------ 入库
def test_ingest_populates_transactional_tables(env):
    con, builder, store, _ = env
    doc, report = build_snapshot(con, builder, store)

    assert report.instruments == 5
    assert report.trading_days == 5
    assert report.events == 2

    n_inst = con.execute("SELECT COUNT(*) FROM instrument").fetchone()[0]
    assert n_inst == 5
    # 状态版本按有效期保存：更名不产生新证券（§15.2）
    n_ver = con.execute(
        "SELECT COUNT(*) FROM instrument_status_version WHERE instrument_id='SYN.A.600519'"
    ).fetchone()[0]
    assert n_ver == 2
    n_days = con.execute("SELECT COUNT(*) FROM trading_calendar").fetchone()[0]
    assert n_days == 5
    n_cit = con.execute("SELECT COUNT(*) FROM citation WHERE located=1").fetchone()[0]
    assert n_cit == 2


def test_ingest_is_idempotent(env):
    con, builder, store, _ = env
    doc = builder.load_manifest(EXAMPLE)
    builder.ensure_source("synthetic-fixture", display_name="合成", domains=["DAILY_QUOTES"])
    builder.ingest(doc, source_id="synthetic-fixture", data_version="v1")
    builder.ingest(doc, source_id="synthetic-fixture", data_version="v1")
    assert con.execute("SELECT COUNT(*) FROM instrument").fetchone()[0] == 5
    # 每个证券都必须有状态历史，否则历史时点查询会返回空（D05）。
    # 资料包：600519(2) + 000001(1) + 300001(1) + 600002(2) + 600003(1) = 7 条
    assert con.execute("SELECT COUNT(*) FROM instrument_status_version").fetchone()[0] == 7
    # 无状态历史的证券数量必须为 0
    orphan = con.execute(
        "SELECT COUNT(*) FROM instrument i WHERE NOT EXISTS ("
        "  SELECT 1 FROM instrument_status_version v WHERE v.instrument_id=i.instrument_id)"
    ).fetchone()[0]
    assert orphan == 0, f"{orphan} instrument(s) have no status history"


def test_source_rights_default_to_unknown(env):
    """§17.2 未确认权利默认不开放。"""

    con, builder, _, _ = env
    builder.ensure_source("src-a", display_name="A", domains=["DAILY_QUOTES"])
    row = con.execute(
        "SELECT research_use, model_processing FROM source_registry WHERE source_id='src-a'"
    ).fetchone()
    assert row["research_uses" if False else "research_use"] == "UNKNOWN"
    assert row["model_processing"] == "UNKNOWN"


def test_explicit_rights_are_recorded(env):
    con, builder, _, _ = env
    builder.ensure_source("src-b", display_name="B", domains=["NEWS"],
                          rights={"research_use": "ALLOWED", "model_processing": "PROHIBITED"})
    row = con.execute(
        "SELECT research_use, model_processing FROM source_registry WHERE source_id='src-b'"
    ).fetchone()
    assert row["research_use"] == "ALLOWED"
    assert row["model_processing"] == "PROHIBITED"


# ------------------------------------------------------------------ 快照
def test_datasets_written_with_real_hashes(env):
    con, builder, store, api_root = env
    build_snapshot(con, builder, store)
    datasets = store.datasets("snap-syn-001")
    names = {d["name"] for d in datasets}
    assert {"daily_quotes", "instruments", "trading_calendar",
            "corporate_actions", "events"} <= names
    for d in datasets:
        # 每个数据集都能通过哈希校验
        store.verify_dataset("snap-syn-001", d["name"])
        assert (api_root / d["path"]).exists()


def test_snapshot_records_watermark_and_mode(env):
    con, builder, store, _ = env
    doc, _ = build_snapshot(con, builder, store)
    snap = store.require_published("snap-syn-001")
    assert snap["data_mode"] == "SYNTHETIC"
    assert snap["watermark"] == doc["watermark"]


def test_dataset_upper_bound_cannot_exceed_cutoff(env):
    """§7/§8.3 数据集时点上界不得晚于输入截止。"""

    con, builder, store, api_root = env
    doc = builder.load_manifest(EXAMPLE)
    refs = builder.write_datasets(doc, snapshot_id="snap-late")
    refs[0]["as_of_upper_bound"] = "2026-09-11T13:00:00Z"   # 晚于 cutoff 12:30Z
    draft = SnapshotDraft(
        snapshot_id="snap-late", kind="EOD", data_mode=DataMode.SYNTHETIC,
        input_cutoff_at=utc(2026, 9, 11, 12, 30), created_at=utc(2026, 9, 11, 12, 0),
        code_version="c", data_version="d", watermark="W",
        datasets=[DatasetRef(
            name=r["name"], path=r["path"], sha256=r["sha256"],
            record_count=r["record_count"],
            as_of_upper_bound=datetime.fromisoformat(r["as_of_upper_bound"].replace("Z", "+00:00")),
        ) for r in refs],
    )
    with pytest.raises(SnapshotError) as exc:
        store.publish(draft)
    assert "later than input_cutoff_at" in exc.value.message


# ------------------------------------------------------------------ 读取 + PIT 门禁
def test_quote_reader_sees_the_expected_series(env):
    con, builder, store, api_root = env
    build_snapshot(con, builder, store)
    payload = json.loads(
        (api_root / "datasets" / "snap-syn-001" / "daily_quotes.json").read_text("utf-8")
    )
    moutai = [q for q in payload if q["instrument_id"] == "SYN.A.600519"]
    assert len(moutai) == 5
    assert moutai[0]["trading_day"] == "2026-09-07"
    # 价格以分为单位（§12.6 定点）
    assert moutai[0]["close_cents"] == 129956


def test_suspended_instrument_has_no_quote_after_suspension(env):
    """D-S08：停牌后无行情记录，缺失即缺失，不得用前收填充。"""

    con, builder, store, api_root = env
    build_snapshot(con, builder, store)
    payload = json.loads(
        (api_root / "datasets" / "snap-syn-001" / "daily_quotes.json").read_text("utf-8")
    )
    days = [q["trading_day"] for q in payload if q["instrument_id"] == "SYN.A.600002"]
    assert days == ["2026-09-07", "2026-09-08", "2026-09-09"]
    assert "2026-09-10" not in days and "2026-09-11" not in days


def test_limit_up_row_is_preserved_for_s02(env):
    con, builder, store, api_root = env
    build_snapshot(con, builder, store)
    payload = json.loads(
        (api_root / "datasets" / "snap-syn-001" / "daily_quotes.json").read_text("utf-8")
    )
    row = next(q for q in payload
               if q["instrument_id"] == "SYN.A.300001" and q["trading_day"] == "2026-09-09")
    assert row["board_limit_up"] is True
    # 开盘即涨停：四个价格相同
    assert row["open_cents"] == row["high_cents"] == row["low_cents"] == row["close_cents"]


def test_dividend_action_ex_and_pay_dates_differ_for_s07(env):
    con, builder, store, api_root = env
    build_snapshot(con, builder, store)
    payload = json.loads(
        (api_root / "datasets" / "snap-syn-001" / "corporate_actions.json").read_text("utf-8")
    )
    div = next(a for a in payload if a["action_type"] == "CASH_DIVIDEND")
    assert div["record_date"] < div["ex_date"] < div["pay_date"]
    assert div["cash_per_share_cents"] == 50

    row = con.execute(
        "SELECT supported FROM corporate_action WHERE action_type='RIGHTS_ISSUE'"
    ).fetchone()
    assert row["supported"] == 0, "无法核验的复杂公司行为必须标记未支持（§12.7）"


def test_events_land_with_citations_and_available_at(env):
    """D04：只有日期的公告，available_at 保守顺延到次一交易日盘前。"""

    con, builder, store, _ = env
    build_snapshot(con, builder, store)
    row = con.execute(
        "SELECT available_at, available_basis, source_published_date, verification_status "
        "FROM event WHERE event_id='evt-syn-001'"
    ).fetchone()
    assert row["source_published_date"] == "2026-09-04"
    assert row["available_at"] == "2026-09-07T01:30:00Z"
    assert row["available_basis"] == "RECONSTRUCTED"
    assert row["verification_status"] == "VERIFIED"


def test_malicious_sample_is_archived_but_unverified(env):
    """A05：恶意材料照常归档，但状态为 UNVERIFIED，且不产生任何写权限。"""

    con, builder, store, _ = env
    build_snapshot(con, builder, store)
    row = con.execute(
        "SELECT verification_status, market_direction FROM event "
        "WHERE event_id='evt-syn-malicious-001'"
    ).fetchone()
    assert row["verification_status"] == "UNVERIFIED"
    assert row["market_direction"] == "UNKNOWN"


def test_published_snapshot_blocks_further_writes(env):
    """§15.4 发布后不可变。"""

    import sqlite3

    con, builder, store, _ = env
    build_snapshot(con, builder, store)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE snapshot SET watermark='tampered' WHERE snapshot_id='snap-syn-001'")
