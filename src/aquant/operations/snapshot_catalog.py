"""Fast, integrity-checked summaries for the published snapshot catalog.

The catalog derives a small amount of UI metadata from immutable snapshot
datasets. Reusing that derivation is safe only after checking the current
published row, its point-in-time binding, and the current bytes against the
published hashes. This cache stores only the derived domain summary; it never
stores quote or instrument rows and never treats file metadata as an integrity
check.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from aquant.domain.data.db import connect
from aquant.domain.data.reader import SnapshotReader
from aquant.domain.data.snapshot import DatasetRef, PublishedSnapshot, SnapshotStore


# Bump this whenever the catalog's derived trading-day or S1 capability rules
# change. The in-process cache is short-lived, but this keeps the cache key
# explicit about which derivation produced each summary.
_SUMMARY_ALGORITHM_VERSION = 1
_HASHED_CATALOG_DATASETS = ("daily_quotes", "instruments")
_HASH_WORKERS = 2


@dataclass(frozen=True, slots=True)
class _CacheKey:
    scope: tuple[str, str]
    snapshot_id: str
    algorithm_version: int
    snapshot_metadata: tuple[tuple[str, Any], ...]
    dataset_metadata: tuple[tuple[tuple[str, Any], ...], ...]


@dataclass(slots=True)
class _CacheHitPlan:
    row: dict
    key: _CacheKey
    cached: PublishedSnapshot
    reader: SnapshotReader
    refs: dict[str, DatasetRef] | None = None
    futures: dict[str, Future[str]] | None = None
    error: Exception | None = None
    error_dataset: str | None = None


class SnapshotCatalogCache:
    """Bounded cache of small published-snapshot summaries.

    On a cache hit this class still checks that the snapshot is currently
    published, rechecks the exact snapshot ``as_of_time``, and recomputes the
    SHA-256 of each dataset used for the derived summary. Only the JSON parsing
    and capability/trading-day derivation are reused when the complete
    publication metadata and dataset hash set are unchanged.
    """

    def __init__(self, maxsize: int = 64) -> None:
        if maxsize < 0:
            raise ValueError("maxsize must be non-negative")
        self.maxsize = maxsize
        self._items: OrderedDict[_CacheKey, PublishedSnapshot] = OrderedDict()
        self._lock = threading.RLock()

    def list_published(self, *, data_dir: str | Path,
                       root: str | Path) -> list[PublishedSnapshot]:
        """Return the stable published catalog from an independent RO SQLite connection.

        The read transaction keeps status and dataset metadata consistent for
        the whole list. A fresh ``SnapshotReader`` is used per cache miss so
        its quote cache can retain at most one snapshot's rows at a time.
        """

        # One app instance serves its own catalog calls under this lock. That
        # makes a cold request single-flight: a concurrent request waits for
        # the first request to derive and cache each summary, then still does
        # its own publication/PIT/hash checks on the hot path.
        with self._lock:
            return self._list_published(data_dir=data_dir, root=root)

    def _list_published(self, *, data_dir: str | Path,
                        root: str | Path) -> list[PublishedSnapshot]:
        """Read one catalog while the public method holds the single-flight lock."""

        data_path = Path(data_dir).resolve()
        root_path = Path(root).resolve()
        db_path = data_path / "meta.sqlite"
        scope = (str(db_path), str(root_path))

        with closing(connect(db_path, read_only=True)) as con, \
                ThreadPoolExecutor(max_workers=_HASH_WORKERS) as hash_pool:
            con.execute("BEGIN")
            try:
                store = SnapshotStore(con, root_path)
                rows = store.list_published()
                prepared: list[tuple[dict, list[dict], _CacheKey]] = []
                active_keys: set[_CacheKey] = set()
                for row in rows:
                    datasets = store.datasets(row["snapshot_id"])
                    key = self._key(scope, row, datasets)
                    prepared.append((row, datasets, key))
                    active_keys.add(key)

                # Drop entries whose snapshot stopped being published or whose
                # publication metadata changed, including a superseded row
                # later restored under the same ID.
                self._retain_current(scope, active_keys)

                output: list[PublishedSnapshot] = []
                index = 0
                while index < len(prepared):
                    row, datasets, key = prepared[index]
                    reader = SnapshotReader(store)
                    cached = self._get(key)
                    if cached is not None:
                        try:
                            first = self._plan_cache_hit(
                                row, datasets, key, cached, reader, hash_pool)
                        except Exception:
                            self._discard(key)
                            raise

                        # A bounded one-row lookahead overlaps the large hash
                        # inputs of adjacent warm snapshots without allowing
                        # the executor queue to grow with catalog size.
                        next_entry = (
                            prepared[index + 1]
                            if index + 1 < len(prepared) else None
                        )
                        next_cached: PublishedSnapshot | None = None
                        if next_entry is not None:
                            next_cached = self._peek(next_entry[2])

                        plans = [first]
                        if (next_entry is not None and next_cached is not None
                                and first.futures):
                            next_row, next_datasets, next_key = next_entry
                            next_reader = SnapshotReader(store)
                            plans.append(self._plan_cache_hit(
                                next_row, next_datasets, next_key, next_cached,
                                next_reader, hash_pool))

                        for plan_index, plan in enumerate(plans):
                            if plan_index == 1:
                                # The serial walk would move this cache hit to
                                # the LRU tail only after row 1 passed.
                                self._get(plan.key)
                            try:
                                self._check_cache_hit_hashes(plan)
                            except Exception:
                                self._discard(plan.key)
                                raise
                            output.append(plan.cached)

                        index += len(plans)
                        continue

                    summary = reader.published_snapshot(row["snapshot_id"])
                    self._put(key, summary)
                    output.append(summary)
                    index += 1
                return output
            finally:
                if con.in_transaction:
                    con.execute("ROLLBACK")

    @staticmethod
    def _key(scope: tuple[str, str], snapshot: dict,
             datasets: list[dict]) -> _CacheKey:
        # Include every stored field, not just the fields currently serialized
        # to the UI. That makes any publication metadata change invalidate the
        # derived summary, while dataset SHA values bind it to exact content.
        snapshot_metadata = tuple(sorted(snapshot.items()))
        dataset_metadata = tuple(
            tuple(sorted(dataset.items()))
            for dataset in sorted(datasets, key=lambda item: item["name"])
        )
        return _CacheKey(
            scope=scope,
            snapshot_id=str(snapshot["snapshot_id"]),
            algorithm_version=_SUMMARY_ALGORITHM_VERSION,
            snapshot_metadata=snapshot_metadata,
            dataset_metadata=dataset_metadata,
        )

    def _get(self, key: _CacheKey) -> PublishedSnapshot | None:
        with self._lock:
            item = self._items.get(key)
            if item is not None:
                self._items.move_to_end(key)
            return item

    def _put(self, key: _CacheKey, item: PublishedSnapshot) -> None:
        if self.maxsize == 0:
            return
        with self._lock:
            self._items[key] = item
            self._items.move_to_end(key)
            while len(self._items) > self.maxsize:
                self._items.popitem(last=False)

    def _discard(self, key: _CacheKey) -> None:
        with self._lock:
            self._items.pop(key, None)

    def _peek(self, key: _CacheKey) -> PublishedSnapshot | None:
        """Read a lookahead item without changing its LRU position."""

        with self._lock:
            return self._items.get(key)

    @classmethod
    def _plan_cache_hit(cls, row: dict, datasets: list[dict], key: _CacheKey,
                        cached: PublishedSnapshot, reader: SnapshotReader,
                        hash_pool: ThreadPoolExecutor) -> _CacheHitPlan:
        plan = _CacheHitPlan(row=row, key=key, cached=cached, reader=reader)
        try:
            dataset_names = cls._prepare_cache_hit(reader, row, datasets)
        except Exception as exc:
            plan.error = exc
            return plan

        plan.refs = {}
        plan.futures = {}
        for name in _HASHED_CATALOG_DATASETS:
            if name not in dataset_names:
                continue
            try:
                ref = reader.store._dataset_ref(row["snapshot_id"], name)
                future = hash_pool.submit(ref.recompute_sha256, reader.store.root)
            except Exception as exc:
                plan.error = exc
                plan.error_dataset = name
                return plan
            plan.refs[name] = ref
            plan.futures[name] = future
        return plan

    def _retain_current(self, scope: tuple[str, str],
                        active_keys: set[_CacheKey]) -> None:
        with self._lock:
            for key in tuple(self._items):
                if key.scope == scope and key not in active_keys:
                    del self._items[key]

    @staticmethod
    def _prepare_cache_hit(reader: SnapshotReader, snapshot: dict,
                           datasets: list[dict]) -> set[str]:
        snapshot_id = snapshot["snapshot_id"]
        current = reader.store.require_published(snapshot_id)
        point_text = current["as_of_time"] or current["input_cutoff_at"]
        # Reuse the same exact-as-of guard used by the ordinary data reader.
        reader._assert_as_of_within_snapshot(
            snapshot_id, datetime.fromisoformat(point_text))

        dataset_names = {dataset["name"] for dataset in datasets}
        return dataset_names

    @staticmethod
    def _check_cache_hit_hashes(plan: _CacheHitPlan) -> None:
        # Collect and check in the historical dataset order even when a later
        # worker finishes first. This preserves which integrity failure callers
        # observe if more than one dataset is damaged.
        refs = plan.refs or {}
        futures = plan.futures or {}
        for name in _HASHED_CATALOG_DATASETS:
            if plan.error is not None and plan.error_dataset is None:
                raise plan.error
            if plan.error is not None and plan.error_dataset == name:
                raise plan.error
            if name not in refs:
                continue
            actual = futures[name].result()
            plan.reader.store._check_dataset_hash(
                plan.row["snapshot_id"], refs[name], actual)
        if plan.error is not None:
            raise plan.error

