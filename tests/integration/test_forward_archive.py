"""前向归档与抓取韧性测试。

归档的用途是产生**可证明的时点数据**，因此测试重点不是"能存能取"，
而是三条性质：去重但保留观察历史、失败也留证、哈希可验证。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from aquant.adapters.providers.resilience import (
    CircuitBreaker,
    CircuitOpen,
    RateLimiter,
    RetryPolicy,
)
from aquant.domain.data.forward_archive import ForwardArchive


def utc(y=2026, m=9, d=11, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


@pytest.fixture()
def archive(tmp_path):
    con = sqlite3.connect(tmp_path / "meta.sqlite", isolation_level=None)
    con.row_factory = sqlite3.Row
    yield ForwardArchive(con, tmp_path)
    con.close()


# ------------------------------------------------------------------ 去重与观察史
def test_same_content_stored_once(archive):
    a, new_a = archive.store_bytes(b'{"x":1}')
    b, new_b = archive.store_bytes(b'{"x":1}')
    assert a == b
    assert new_a is True
    assert new_b is False
    assert archive.verify(a)


def test_identical_content_records_a_new_observation_each_time(archive):
    """同一内容在不同时刻被观察到 -> 这是 PIT 可证明性的来源。"""

    payload = b'{"k":1}'
    digest, _ = archive.store_bytes(payload)

    t1, t2 = utc(hh=1), utc(hh=5)
    archive.record(source_id="em", url="https://x/a", outcome="OK",
                   requested_at=t1, responded_at=t1, http_status=200,
                   content_hash=digest, byte_size=len(payload))
    archive.record(source_id="em", url="https://x/a", outcome="OK",
                   requested_at=t2, responded_at=t2, http_status=200,
                   content_hash=digest, byte_size=len(payload))

    history = archive.observation_history("https://x/a")
    assert [h["first_seen_at"] for h in history] == [t1.isoformat(), t2.isoformat()]
    # 字节只存一份
    assert archive.distinct_content_count("em") == 1
    assert len(archive.receipts_for("em")) == 2


def test_receipt_collision_cannot_rewrite_first_seen_or_content(archive):
    first_hash, _ = archive.store_bytes(b'{"version":1}')
    later_hash, _ = archive.store_bytes(b'{"version":2}')
    requested = utc(hh=1)
    first = archive.record(
        source_id="em", url="https://x/a", outcome="OK",
        requested_at=requested, responded_at=utc(hh=2),
        http_status=200, content_hash=first_hash, byte_size=13,
    )
    with pytest.raises(ValueError, match="duplicate fetch receipt_id"):
        archive.record(
            source_id="em", url="https://x/a", outcome="OK",
            requested_at=requested, responded_at=utc(hh=3),
            http_status=200, content_hash=later_hash, byte_size=13,
        )
    stored = archive.receipts_for("em")
    assert len(stored) == 1
    assert stored[0]["receipt_id"] == first.receipt_id
    assert stored[0]["first_seen_at"] == utc(hh=2).isoformat()
    assert stored[0]["content_hash"] == first_hash


def test_receipts_and_artifact_index_are_append_only(archive):
    digest, _ = archive.store_bytes(b'{"version":1}')
    receipt = archive.record(
        source_id="em", url="https://x/a", outcome="OK",
        requested_at=utc(hh=1), responded_at=utc(hh=2),
        http_status=200, content_hash=digest, byte_size=13,
    )
    for statement, value in (
        ("UPDATE fetch_receipt SET first_seen_at=? WHERE receipt_id=?",
         (utc(hh=3).isoformat(), receipt.receipt_id)),
        ("DELETE FROM fetch_receipt WHERE receipt_id=?", (receipt.receipt_id,)),
        ("UPDATE raw_artifact SET stored_path=? WHERE content_hash=?",
         ("other", digest)),
        ("DELETE FROM raw_artifact WHERE content_hash=?", (digest,)),
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            archive.con.execute(statement, value)
    assert archive.verify(digest)
    assert archive.receipts_for("em")[0]["first_seen_at"] == utc(hh=2).isoformat()


def test_content_changed_is_a_new_artifact(archive):
    d1, _ = archive.store_bytes(b'{"v":1}')
    d2, _ = archive.store_bytes(b'{"v":2}')
    assert d1 != d2
    assert archive.distinct_content_count("em") == 0
    assert archive.verify(d1) and archive.verify(d2)


# ------------------------------------------------------------------ 失败也留证
@pytest.mark.parametrize("outcome", [
    "HTTP_ERROR", "DENIED", "TIMEOUT", "TRANSPORT_ERROR", "TOO_LARGE",
])
def test_failures_are_recorded_too(archive, outcome):
    """主文档 §6.1：公告保留索引与失败原因；拒绝本身也是证据。"""

    r = archive.record(source_id="em", url="https://x/f", outcome=outcome,
                       requested_at=utc(), detail="connection reset")
    assert r.outcome == outcome
    assert r.detail == "connection reset"
    stored = archive.receipts_for("em")
    assert len(stored) == 1
    assert stored[0]["outcome"] == outcome


def test_denied_receipt_has_no_content_hash(archive):
    r = archive.record(source_id="em", url="https://127.0.0.1/x", outcome="DENIED",
                       requested_at=utc(), detail="blocked-address")
    assert r.content_hash is None
    assert r.byte_size is None


# ------------------------------------------------------------------ 完整性
def test_tampering_with_archived_bytes_is_detected(archive, tmp_path):
    digest, _ = archive.store_bytes(b'{"trust":true}')
    row = archive.con.execute(
        "SELECT stored_path FROM raw_artifact WHERE content_hash=?", (digest,)
    ).fetchone()
    (tmp_path / row["stored_path"]).write_bytes(b'{"trust":false}')
    assert archive.verify(digest) is False


def test_load_unknown_hash_raises(archive):
    with pytest.raises(KeyError):
        archive.load_bytes("sha256:" + "0" * 64)


def test_naive_timestamp_rejected(archive):
    """§7.1 禁止朴素时间：归档时刻必须带时区。"""

    with pytest.raises(ValueError, match="timezone-aware"):
        archive.record(source_id="em", url="https://x/a", outcome="OK",
                       requested_at=datetime(2026, 9, 11))  # naive


def test_first_seen_is_never_backdated(archive):
    """§7.2 first_seen_at 是归档时刻，不得回填为过去。"""

    r = archive.record(source_id="em", url="https://x/a", outcome="OK",
                       requested_at=utc(2020, 1, 1), responded_at=utc(2026, 9, 11))
    assert r.first_seen_at == utc(2026, 9, 11)
    assert r.first_seen_at > r.requested_at


def test_response_time_cannot_precede_request(archive):
    with pytest.raises(ValueError, match="response time cannot precede request"):
        archive.record(source_id="em", url="https://x/a", outcome="OK",
                       requested_at=utc(hh=3), responded_at=utc(hh=2))
    assert archive.receipts_for("em") == []


# ------------------------------------------------------------------ 熔断
def test_circuit_opens_after_threshold():
    cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
    cb.assert_closed()
    cb.record_failure()
    cb.record_failure()
    cb.assert_closed()          # 未达阈值仍放行
    cb.record_failure()
    assert cb.is_open
    with pytest.raises(CircuitOpen) as exc:
        cb.assert_closed()
    assert exc.value.retry_after_seconds > 0


def test_circuit_half_opens_after_cooldown():
    cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=0.05)
    cb.record_failure()
    assert cb.is_open
    import time
    time.sleep(0.08)
    cb.assert_closed()          # 冷却结束，放行一次试探
    assert cb.consecutive_failures == 0


def test_success_resets_circuit():
    cb = CircuitBreaker(failure_threshold=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    assert not cb.is_open
    assert cb.consecutive_failures == 1


def test_circuit_state_is_serializable():
    cb = CircuitBreaker(failure_threshold=2)
    cb.record_failure()
    cb.record_failure()
    st = cb.state()
    assert set(st) == {"open", "consecutive_failures", "retry_after_seconds"}
    assert st["open"] is True


# ------------------------------------------------------------------ 限速
def test_rate_limiter_enforces_minimum_interval():
    slept: list[float] = []
    rl = RateLimiter(min_interval_seconds=1.5)
    rl.wait("host-a", sleep=slept.append)
    rl.wait("host-a", sleep=slept.append)
    assert slept, "second call on the same host must wait"
    assert 0 < slept[0] <= 1.5


def test_rate_limiter_is_per_host():
    slept: list[float] = []
    rl = RateLimiter(min_interval_seconds=1.5)
    rl.wait("host-a", sleep=slept.append)
    rl.wait("host-b", sleep=slept.append)
    assert slept == [], "different hosts must not block each other"


def test_retry_backoff_is_exponential_and_capped():
    rp = RetryPolicy(max_attempts=5, base_delay_seconds=2.0, max_delay_seconds=10.0)
    delays = [rp.delay_for(i) for i in range(1, 6)]
    assert delays == [2.0, 4.0, 8.0, 10.0, 10.0]
