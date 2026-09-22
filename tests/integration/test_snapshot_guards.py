"""快照守卫测试：不可变、哈希可验证、时点上界、模式不混用。

对应主文档 §15.4、§8.3、§20 ADR-009。这些不是"文档里写了"，
而是**执行时会拒绝**的行为。
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.data.snapshot import (
    DataMode,
    DatasetRef,
    SnapshotDraft,
    SnapshotError,
    SnapshotStore,
)


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path):
    con = connect(tmp_path / "meta.sqlite")
    apply_migrations(con)
    yield SnapshotStore(con, tmp_path)
    con.close()


def make_dataset(root, name: str, payload: bytes, upper: datetime) -> DatasetRef:
    rel = f"datasets/{name}.json"
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return DatasetRef(
        name=name,
        path=rel,
        sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
        record_count=3,
        as_of_upper_bound=upper,
    )


def draft(root, *, sid="snap-test-001", mode=DataMode.SYNTHETIC, cutoff=None,
          watermark="SYNTHETIC DATA -- NOT VALID FOR RESEARCH CONCLUSIONS",
          upper=None, supersedes=None, payload=b'{"a":1}\n'):
    cutoff = cutoff or utc(2026, 9, 11, 12, 30)
    upper = upper or utc(2026, 9, 11, 12, 0)
    return SnapshotDraft(
        snapshot_id=sid,
        kind="EOD",
        data_mode=mode,
        input_cutoff_at=cutoff,
        created_at=utc(2026, 9, 11, 12, 0),
        code_version="c1",
        data_version="d1",
        watermark=watermark,
        supersedes=supersedes,
        # Each snapshot owns a distinct dataset file. Reusing one path would mean
        # publishing a correction overwrites the earlier snapshot's verified
        # artefact, and the hash guard would (correctly) abort the supersede.
        datasets=[make_dataset(root, "dq_" + sid, payload, upper)],
    )


# ------------------------------------------------------------------ happy path
def test_publish_and_read_back(store, tmp_path):
    sid = store.publish(draft(tmp_path))
    snap = store.require_published(sid)
    assert snap["data_mode"] == "SYNTHETIC"
    assert snap["quality_status"] == "OK"
    assert store.datasets(sid)[0]["name"] == "dq_" + sid
    store.verify_dataset(sid, "dq_" + sid)


def test_synthetic_requires_watermark(store, tmp_path):
    """§15.4 SYNTHETIC 强制水印。"""

    with pytest.raises(SnapshotError) as exc:
        store.publish(draft(tmp_path, watermark="   "))
    assert "watermark" in exc.value.message


def test_production_needs_no_watermark(store, tmp_path):
    sid = store.publish(draft(tmp_path, mode=DataMode.PRODUCTION, watermark=None))
    assert store.get(sid)["data_mode"] == "PRODUCTION"


# ------------------------------------------------------------------ hash
def test_hash_mismatch_rejected(store, tmp_path):
    """声明的哈希与实际文件不符 -> 拒绝发布。"""

    d = draft(tmp_path)
    bad = DatasetRef(
        name=d.datasets[0].name, path=d.datasets[0].path,
        sha256="sha256:" + "0" * 64,
        record_count=1, as_of_upper_bound=d.datasets[0].as_of_upper_bound,
    )
    d.datasets = [bad]
    with pytest.raises(SnapshotError) as exc:
        store.publish(d)
    assert "hash mismatch" in exc.value.message


def test_tampering_after_publication_detected_on_read(store, tmp_path):
    """发布后内容被替换 -> 读取时校验失败（§15.4 消费者再次验证）。"""

    sid = store.publish(draft(tmp_path))
    owned = store.datasets(sid)[0]
    (tmp_path / owned["path"]).write_bytes(b'{"a":999}\n')
    with pytest.raises(SnapshotError) as exc:
        store.verify_dataset(sid, "dq_" + sid)
    assert "hash verification" in exc.value.message


# ------------------------------------------------------------------ cutoff
def test_dataset_after_cutoff_rejected(store, tmp_path):
    """§7/§8.3：数据集时点上界不得晚于输入截止。"""

    with pytest.raises(SnapshotError) as exc:
        store.publish(draft(tmp_path, cutoff=utc(2026, 9, 11, 12, 0),
                            upper=utc(2026, 9, 11, 12, 1)))
    assert "later than input_cutoff_at" in exc.value.message


# ------------------------------------------------------------------ immutability
def test_published_snapshot_cannot_be_updated_or_deleted(store, tmp_path):
    """§15.4 已发布快照不可变，由数据库触发器强制。"""

    sid = store.publish(draft(tmp_path))
    with pytest.raises(sqlite3.IntegrityError):
        store.con.execute("UPDATE snapshot SET data_mode='PRODUCTION' WHERE snapshot_id=?", (sid,))
    with pytest.raises(sqlite3.IntegrityError):
        store.con.execute("DELETE FROM snapshot WHERE snapshot_id=?", (sid,))


def test_duplicate_snapshot_id_rejected(store, tmp_path):
    store.publish(draft(tmp_path))
    with pytest.raises(SnapshotError) as exc:
        store.publish(draft(tmp_path))
    assert "immutable" in exc.value.message


def test_correction_uses_new_id_and_supersedes(store, tmp_path):
    """§15.4 修正通过新 ID + supersedes，旧快照转为 SUPERSEDED 但仍可回看。"""

    old = store.publish(draft(tmp_path, sid="snap-old"))
    new = store.publish(draft(tmp_path, sid="snap-new", supersedes=old,
                              payload=b'{"a":2}\n'))
    assert store.get(old)["status"] == "SUPERSEDED"
    assert store.get(new)["supersedes"] == old
    # 旧快照不再可作研究输入
    with pytest.raises(SnapshotError):
        store.require_published(old)


def test_cannot_supersede_unknown_snapshot(store, tmp_path):
    """supersedes 指向不存在的快照 -> 拒绝。"""

    with pytest.raises(SnapshotError) as exc:
        store.publish(draft(tmp_path, sid="snap-x", supersedes="snap-does-not-exist"))
    assert "unknown snapshot" in exc.value.message


# ------------------------------------------------------------------ mode mixing
def test_synthetic_and_production_cannot_mix(store, tmp_path):
    """§15.4 禁止把合成数据与真实行情混成单一收益曲线。"""

    a = store.publish(draft(tmp_path, sid="snap-syn", mode=DataMode.SYNTHETIC))
    b = store.publish(draft(tmp_path, sid="snap-prod", mode=DataMode.PRODUCTION,
                            watermark=None, payload=b'{"a":3}\n'))
    with pytest.raises(SnapshotError) as exc:
        store.assert_same_mode([a, b])
    assert "mix data modes" in exc.value.message


def test_same_mode_is_allowed(store, tmp_path):
    a = store.publish(draft(tmp_path, sid="snap-s1", payload=b'{"a":4}\n'))
    b = store.publish(draft(tmp_path, sid="snap-s2", payload=b'{"a":5}\n'))
    assert store.assert_same_mode([a, b]) is DataMode.SYNTHETIC


# ------------------------------------------------------------------ blocking
def test_blocking_issues_mark_quality_and_are_recorded(store, tmp_path):
    """§20 ADR-009 数据不完整时阻断，但问题本身要可诊断（§16.4）。"""

    d = draft(tmp_path)
    d.blocking_issues = [{
        "code": "DATA_NOT_READY",
        "message": "daily_quotes coverage 0.42 below threshold",
        "object_id": "daily_quotes",
        "retryable": True,
        "repair_action": "re-fetch the missing trading days before publishing",
    }]
    sid = store.publish(d)
    assert store.get(sid)["quality_status"] == "BLOCKING"
    issues = store.blocking_issues(sid)
    assert len(issues) == 1
    assert issues[0]["error_code"] == "DATA_NOT_READY"
    assert issues[0]["retryable"] == 1


def test_explicit_degraded_quality_is_persisted_without_blocking_issue(store, tmp_path):
    d = draft(tmp_path)
    d.quality_status = "DEGRADED"
    sid = store.publish(d)

    assert store.get(sid)["quality_status"] == "DEGRADED"
    assert store.blocking_issues(sid) == []


def test_empty_dataset_list_rejected(store, tmp_path):
    d = draft(tmp_path)
    d.datasets = []
    with pytest.raises(SnapshotError) as exc:
        store.publish(d)
    assert "no datasets" in exc.value.message


def test_unknown_snapshot_read_raises_typed_error(store):
    with pytest.raises(SnapshotError) as exc:
        store.require_published("snap-nope")
    assert exc.value.code == "DATA_NOT_READY"
    assert exc.value.as_error()["repair_action"]


def test_naive_timestamp_rejected(store, tmp_path):
    """§7.1 禁止朴素时间。"""

    with pytest.raises(ValueError, match="timezone-aware"):
        store.publish(draft(tmp_path, cutoff=datetime(2026, 9, 11, 12, 0)))
