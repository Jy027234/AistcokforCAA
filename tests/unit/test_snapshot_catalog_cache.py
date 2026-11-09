"""Integrity and bounded-cache behavior for published snapshot summaries."""

from __future__ import annotations

import hashlib
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.data.db import apply_migrations, connect  # noqa: E402
from aquant.domain.data.reader import SnapshotReader  # noqa: E402
from aquant.domain.data.snapshot import (  # noqa: E402
    DataMode, DatasetRef, SnapshotDraft, SnapshotError, SnapshotStore, json_bytes,
)
from aquant.operations.snapshot_catalog import SnapshotCatalogCache  # noqa: E402


AS_OF = datetime(2026, 3, 2, 15, 0, tzinfo=timezone.utc)
SNAPSHOT_ID = "snap-catalog-fixture"


def _quote_rows() -> list[dict]:
    return [
        {
            "instrument_id": "CN:600000",
            "trading_day": (date(2026, 1, 1) + timedelta(days=index)).isoformat(),
            "open_cents": 1000 + index,
            "high_cents": 1010 + index,
            "low_cents": 990 + index,
            "close_cents": 1005 + index,
            "adjusted_close_cents": 1000 + index,
            "volume_shares": 100_000,
        }
        for index in range(61)
    ]


def _write_dataset(root: Path, snapshot_id: str, name: str, payload: list[dict]) -> DatasetRef:
    relpath = f"datasets/{snapshot_id}/{name}.json"
    body = json_bytes(payload)
    target = root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return DatasetRef(
        name=name,
        path=relpath,
        sha256="sha256:" + hashlib.sha256(body).hexdigest(),
        record_count=len(payload),
        as_of_upper_bound=AS_OF,
    )


def _publish(data_dir: Path, *, snapshot_id: str = SNAPSHOT_ID,
             source_id: str | None = None,
             parent_snapshot_id: str | None = None) -> Path:
    root = data_dir / "api"
    root.mkdir(parents=True, exist_ok=True)
    con = connect(data_dir / "meta.sqlite")
    try:
        apply_migrations(con)
        store = SnapshotStore(con, root)
        if source_id is None:
            datasets = [
                _write_dataset(root, snapshot_id, "daily_quotes", _quote_rows()),
                _write_dataset(root, snapshot_id, "instruments", [{
                    "instrument_id": "CN:600000",
                    "name": "fixture bank",
                    "industry_code": "BANK",
                }]),
            ]
            source = None
        else:
            source = store.get(source_id)
            datasets = [
                DatasetRef(
                    name=row["name"], path=row["path"], sha256=row["sha256"],
                    record_count=row["record_count"],
                    as_of_upper_bound=datetime.fromisoformat(row["as_of_upper_bound"]),
                    coverage_ratio=row["coverage_ratio"],
                )
                for row in con.execute(
                    "SELECT * FROM snapshot_dataset WHERE snapshot_id=? ORDER BY name",
                    (source_id,),
                ).fetchall()
            ]

        store.publish(SnapshotDraft(
            snapshot_id=snapshot_id,
            kind=source["kind"] if source else "EOD",
            data_mode=DataMode(source["data_mode"] if source else "PRODUCTION"),
            input_cutoff_at=(datetime.fromisoformat(source["input_cutoff_at"])
                             if source else AS_OF),
            as_of_time=(datetime.fromisoformat(source["as_of_time"])
                        if source else AS_OF),
            created_at=(datetime.fromisoformat(source["created_at"])
                        if source else AS_OF - timedelta(minutes=2)),
            published_at=(datetime.fromisoformat(source["published_at"])
                          if source else AS_OF - timedelta(minutes=1)),
            code_version=source["code_version"] if source else "test",
            data_version=source["data_version"] if source else "test-v1",
            watermark=source["watermark"] if source else None,
            parent_snapshot_id=parent_snapshot_id,
            quality_status=source["quality_status"] if source else "OK",
            datasets=datasets,
        ))
    finally:
        con.close()
    return root


@pytest.fixture()
def catalog_paths(tmp_path):
    data_dir = tmp_path / "data"
    root = _publish(data_dir)
    return data_dir, root


def test_cache_hit_reuses_only_summary_and_reverifies_hashes_and_pit(
        catalog_paths, monkeypatch):
    data_dir, root = catalog_paths
    cache = SnapshotCatalogCache()
    verify_calls: list[tuple[str, str]] = []
    load_calls: list[tuple[str, str]] = []
    as_of_checks: list[str] = []
    original_verify = SnapshotStore._check_dataset_hash
    original_load = SnapshotReader._load_dataset
    original_as_of = SnapshotReader._assert_as_of_within_snapshot

    def count_verify(snapshot_id, ref, actual):
        verify_calls.append((snapshot_id, ref.name))
        return original_verify(snapshot_id, ref, actual)

    def count_load(reader, snapshot_id, name):
        load_calls.append((snapshot_id, name))
        return original_load(reader, snapshot_id, name)

    def count_as_of(reader, snapshot_id, point):
        as_of_checks.append(snapshot_id)
        return original_as_of(reader, snapshot_id, point)

    monkeypatch.setattr(SnapshotStore, "_check_dataset_hash", staticmethod(count_verify))
    monkeypatch.setattr(SnapshotReader, "_load_dataset", count_load)
    monkeypatch.setattr(SnapshotReader, "_assert_as_of_within_snapshot", count_as_of)

    first = cache.list_published(data_dir=data_dir, root=root)
    second = cache.list_published(data_dir=data_dir, root=root)

    assert second == first
    assert len(first) == 1
    assert first[0].snapshot_id == SNAPSHOT_ID
    assert first[0].trading_day == "2026-03-02"
    assert first[0].s1_decision.available is True
    assert load_calls == [
        (SNAPSHOT_ID, "daily_quotes"), (SNAPSHOT_ID, "instruments"),
    ]
    assert verify_calls == [
        (SNAPSHOT_ID, "daily_quotes"), (SNAPSHOT_ID, "instruments"),
        (SNAPSHOT_ID, "daily_quotes"), (SNAPSHOT_ID, "instruments"),
    ]
    assert as_of_checks == [
        SNAPSHOT_ID, SNAPSHOT_ID, SNAPSHOT_ID,
    ]


def test_cache_hit_lookahead_hashes_adjacent_rows_on_two_bounded_workers(
        catalog_paths, monkeypatch):
    data_dir, root = catalog_paths
    _publish(data_dir, snapshot_id="snap-catalog-fixture-2")
    cache = SnapshotCatalogCache()
    expected = cache.list_published(data_dir=data_dir, root=root)
    first_id, second_id = (item.snapshot_id for item in expected)
    first_daily_started = threading.Event()
    second_daily_started = threading.Event()
    second_daily_ready = threading.Event()
    release_hashes = threading.Event()
    adjacent_overlap = threading.Event()
    daily_hashes: list[tuple[str, int]] = []
    workers_lock = threading.Lock()
    original = DatasetRef.recompute_sha256

    def overlap_hash(ref, hash_root):
        snapshot_id = Path(ref.path).parent.name
        if ref.name == "daily_quotes":
            with workers_lock:
                daily_hashes.append((snapshot_id, threading.get_ident()))
            if snapshot_id == first_id:
                first_daily_started.set()
                assert second_daily_ready.wait(timeout=5)
                adjacent_overlap.set()
                release_hashes.set()
            elif snapshot_id == second_id:
                second_daily_started.set()
                assert first_daily_started.wait(timeout=5)
                second_daily_ready.set()
                assert release_hashes.wait(timeout=5)
        return original(ref, hash_root)

    monkeypatch.setattr(DatasetRef, "recompute_sha256", overlap_hash)

    actual = cache.list_published(data_dir=data_dir, root=root)

    assert actual == expected
    assert adjacent_overlap.is_set()
    assert second_daily_started.is_set()
    assert {snapshot_id for snapshot_id, _worker in daily_hashes} == {
        first_id, second_id,
    }
    assert len({worker for _snapshot_id, worker in daily_hashes}) == 2
    assert threading.get_ident() not in {
        worker for _snapshot_id, worker in daily_hashes
    }


def test_lookahead_defers_later_preflight_error_until_earlier_hash_check(
        catalog_paths, monkeypatch):
    data_dir, root = catalog_paths
    _publish(data_dir, snapshot_id="snap-catalog-fixture-2")
    cache = SnapshotCatalogCache()
    expected = cache.list_published(data_dir=data_dir, root=root)
    first_id, second_id = (item.snapshot_id for item in expected)
    first_quotes = root / "datasets" / first_id / "daily_quotes.json"
    first_quotes.write_bytes(first_quotes.read_bytes() + b" ")

    original = SnapshotReader._assert_as_of_within_snapshot

    def reject_later_preflight(reader, snapshot_id, point):
        if snapshot_id == second_id:
            raise SnapshotError(
                "STALE_SNAPSHOT", "later PIT preflight failed", snapshot_id,
                "repair the test fixture",
            )
        return original(reader, snapshot_id, point)

    monkeypatch.setattr(
        SnapshotReader, "_assert_as_of_within_snapshot", reject_later_preflight)

    with pytest.raises(SnapshotError, match="dataset 'daily_quotes'.*failed hash"):
        cache.list_published(data_dir=data_dir, root=root)

    assert all(key.snapshot_id != first_id for key in cache._items)
    assert any(key.snapshot_id == second_id for key in cache._items)


def test_parallel_hash_failures_keep_dataset_error_order_and_evict_cache(
        catalog_paths):
    data_dir, root = catalog_paths
    cache = SnapshotCatalogCache()
    cache.list_published(data_dir=data_dir, root=root)
    for name in ("daily_quotes", "instruments"):
        path = root / "datasets" / SNAPSHOT_ID / f"{name}.json"
        path.write_bytes(path.read_bytes() + b" ")

    with pytest.raises(SnapshotError, match="dataset 'daily_quotes'"):
        cache.list_published(data_dir=data_dir, root=root)

    assert all(key.snapshot_id != SNAPSHOT_ID for key in cache._items)


@pytest.mark.parametrize("prime_cache", [False, True])
def test_catalog_rejects_same_mtime_content_change_on_miss_and_hit(
        catalog_paths, prime_cache):
    data_dir, root = catalog_paths
    cache = SnapshotCatalogCache()

    quotes_path = root / "datasets" / SNAPSHOT_ID / "daily_quotes.json"
    initial_stat = quotes_path.stat()
    stable_mtime_ns = initial_stat.st_mtime_ns // 1_000_000_000 * 1_000_000_000
    os.utime(quotes_path, ns=(initial_stat.st_atime_ns, stable_mtime_ns))
    assert quotes_path.stat().st_mtime_ns == stable_mtime_ns

    if prime_cache:
        cache.list_published(data_dir=data_dir, root=root)

    original = quotes_path.read_bytes()
    changed = original.replace(
        b'"adjusted_close_cents": 1000',
        b'"adjusted_close_cents": 1001',
        1,
    )
    assert changed != original
    assert len(changed) == len(original)
    stat = quotes_path.stat()
    quotes_path.write_bytes(changed)
    os.utime(quotes_path, ns=(stat.st_atime_ns, stable_mtime_ns))
    assert quotes_path.stat().st_mtime_ns == stable_mtime_ns
    assert quotes_path.stat().st_size == stat.st_size

    with pytest.raises(SnapshotError, match="failed hash verification"):
        cache.list_published(data_dir=data_dir, root=root)


def test_cold_catalog_parses_the_same_bytes_it_verified(catalog_paths, monkeypatch):
    data_dir, root = catalog_paths
    cache = SnapshotCatalogCache()
    quotes_path = root / "datasets" / SNAPSHOT_ID / "daily_quotes.json"
    read_bytes = Path.read_bytes
    reads = []

    def read_then_replace(path):
        body = read_bytes(path)
        if path == quotes_path:
            reads.append(path)
            path.write_bytes(b"[]")
        return body

    monkeypatch.setattr(Path, "read_bytes", read_then_replace)

    first = cache.list_published(data_dir=data_dir, root=root)

    assert first[0].s1_decision.available is True
    assert reads == [quotes_path]
    with pytest.raises(SnapshotError, match="failed hash verification"):
        cache.list_published(data_dir=data_dir, root=root)


def test_changed_dataset_metadata_invalidates_derived_summary(catalog_paths, monkeypatch):
    data_dir, root = catalog_paths
    cache = SnapshotCatalogCache()
    first = cache.list_published(data_dir=data_dir, root=root)
    writer = connect(data_dir / "meta.sqlite")
    try:
        writer.execute(
            "UPDATE snapshot_dataset SET coverage_ratio=? "
            "WHERE snapshot_id=? AND name=?",
            (0.5, SNAPSHOT_ID, "daily_quotes"),
        )
    finally:
        writer.close()

    rebuilds: list[str] = []
    original = SnapshotReader.published_snapshot

    def count_rebuild(reader, snapshot_id):
        rebuilds.append(snapshot_id)
        return original(reader, snapshot_id)

    monkeypatch.setattr(SnapshotReader, "published_snapshot", count_rebuild)
    second = cache.list_published(data_dir=data_dir, root=root)

    assert second[0] != first[0]
    assert second[0].dataset_summary[0].name == "daily_quotes"
    assert second[0].dataset_summary[0].coverage_ratio == 0.5
    assert rebuilds == [SNAPSHOT_ID]


def test_status_change_evicts_cached_snapshot_summary(catalog_paths, monkeypatch):
    data_dir, root = catalog_paths
    cache = SnapshotCatalogCache()
    assert cache.list_published(data_dir=data_dir, root=root)
    writer = connect(data_dir / "meta.sqlite")
    try:
        writer.execute(
            "UPDATE snapshot SET status='SUPERSEDED' WHERE snapshot_id=?",
            (SNAPSHOT_ID,),
        )
    finally:
        writer.close()

    assert cache.list_published(data_dir=data_dir, root=root) == []
    assert all(key.snapshot_id != SNAPSHOT_ID for key in cache._items)

    successor_id = "snap-catalog-successor"
    _publish(
        data_dir, snapshot_id=successor_id, source_id=SNAPSHOT_ID,
        parent_snapshot_id=SNAPSHOT_ID,
    )

    rebuilds: list[str] = []
    original = SnapshotReader.published_snapshot

    def count_rebuild(reader, snapshot_id):
        rebuilds.append(snapshot_id)
        return original(reader, snapshot_id)

    monkeypatch.setattr(SnapshotReader, "published_snapshot", count_rebuild)
    items = cache.list_published(data_dir=data_dir, root=root)
    assert [item.snapshot_id for item in items] == [successor_id]
    assert rebuilds == [successor_id]
    assert all(key.snapshot_id != SNAPSHOT_ID for key in cache._items)

    reader = connect(data_dir / "meta.sqlite", read_only=True)
    try:
        old = reader.execute(
            "SELECT status FROM snapshot WHERE snapshot_id=?", (SNAPSHOT_ID,)
        ).fetchone()
        assert old["status"] == "SUPERSEDED"
    finally:
        reader.close()


def test_cache_capacity_is_bounded_by_summary_count(tmp_path):
    data_dir = tmp_path / "data"
    root = _publish(data_dir)
    _publish(data_dir, snapshot_id="snap-catalog-fixture-2", source_id=SNAPSHOT_ID)
    cache = SnapshotCatalogCache(maxsize=1)

    items = cache.list_published(data_dir=data_dir, root=root)

    assert len(items) == 2
    assert len(cache._items) == 1


def test_concurrent_cold_catalog_calls_share_one_derivation(catalog_paths, monkeypatch):
    data_dir, root = catalog_paths
    cache = SnapshotCatalogCache()
    entered_derivation = threading.Event()
    release_derivation = threading.Event()
    derive_calls: list[str] = []
    verify_calls: list[tuple[str, str]] = []
    original_derive = SnapshotReader.published_snapshot
    original_verify = SnapshotStore._check_dataset_hash

    class ContentionTrackingRLock:
        def __init__(self):
            self.inner = threading.RLock()
            self.contended = threading.Event()

        def acquire(self, blocking=True):
            if self.inner.acquire(blocking=False):
                return True
            if not blocking:
                return False
            self.contended.set()
            return self.inner.acquire()

        def release(self):
            self.inner.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *_exc):
            self.release()

    observed_lock = ContentionTrackingRLock()
    cache._lock = observed_lock

    def block_first_derivation(reader, snapshot_id):
        derive_calls.append(snapshot_id)
        if len(derive_calls) == 1:
            entered_derivation.set()
            assert release_derivation.wait(timeout=5)
        return original_derive(reader, snapshot_id)

    def count_verify(snapshot_id, ref, actual):
        verify_calls.append((snapshot_id, ref.name))
        return original_verify(snapshot_id, ref, actual)

    monkeypatch.setattr(SnapshotReader, "published_snapshot", block_first_derivation)
    monkeypatch.setattr(SnapshotStore, "_check_dataset_hash", staticmethod(count_verify))

    def request():
        return cache.list_published(data_dir=data_dir, root=root)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(request)
        try:
            assert entered_derivation.wait(timeout=5)
            second = pool.submit(request)
            # This event is raised by the lock only when the second request
            # reaches the serialized catalog method while the first is held.
            assert observed_lock.contended.wait(timeout=5)
            assert derive_calls == [SNAPSHOT_ID]
        finally:
            release_derivation.set()
        first_items = first.result(timeout=5)
        second_items = second.result(timeout=5)

    assert first_items == second_items
    assert derive_calls == [SNAPSHOT_ID]
    # First call validates on cold load; the waiting request validates again
    # on its cache hit rather than inheriting the first call's hash result.
    assert verify_calls == [
        (SNAPSHOT_ID, "daily_quotes"), (SNAPSHOT_ID, "instruments"),
        (SNAPSHOT_ID, "daily_quotes"), (SNAPSHOT_ID, "instruments"),
    ]

