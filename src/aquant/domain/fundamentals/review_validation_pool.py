"""Bounded spawn workers for the ordinary immutable review predicates.

This module keeps the process boundary deliberately narrow: workers receive
plain review-row data and only the FinancialFact objects referenced by that
issuer's reviews. They run the original predicates; the caller owns ordering
and all accepted-link assembly.
"""

from __future__ import annotations

import atexit
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from multiprocessing import get_context
from threading import BoundedSemaphore, Condition, Lock
from typing import Callable, Mapping, Sequence


_REVIEW_WORKERS = 4
_MAX_IN_FLIGHT_GROUPS = 4
_MIN_PARALLEL_REVIEW_COUNT = 16
_ROW_FIELDS = ("bundle_id", "evidence_hash", "evidence_json")


@dataclass(frozen=True, slots=True)
class ReviewValidationRecord:
    """A parent-process reference; row is never sent to a child directly."""

    index: int
    row: object
    member_ids: frozenset[str]
    issuer_key: str | None


def _validate_one_review(row, member_ids, facts, s2_validator, machine_validator):
    """Keep the original S2-first short circuit for every individual review."""

    if s2_validator(row, member_ids, facts):
        return True
    return machine_validator(row, member_ids, facts)


def _worker_payload(records, facts):
    """Build a bounded, picklable payload after acquiring a worker slot."""

    wire_records = []
    required_fact_ids = set()
    for record in records:
        ids = tuple(sorted(record.member_ids))
        wire_records.append((
            record.index,
            {field: record.row[field] for field in _ROW_FIELDS},
            ids,
        ))
        required_fact_ids.update(ids)
    # Missing member IDs must stay missing so the original validators reject
    # them through their existing subset checks.
    fact_subset = {version_id: facts[version_id]
                   for version_id in required_fact_ids if version_id in facts}
    return tuple(wire_records), fact_subset


def _review_group_worker(payload):
    """Spawn-importable worker; never receives a SQLite connection or Row."""

    from .fact_repository import (
        _accepted_machine_verified_review,
        _accepted_s2_pdf_review,
    )

    wire_records, facts = payload
    results = []
    for index, row, member_ids in wire_records:
        accepted = _validate_one_review(
            row, set(member_ids), facts,
            _accepted_s2_pdf_review, _accepted_machine_verified_review,
        )
        results.append((index, accepted))
    return tuple(results)


class _SpawnReviewPool:
    """One lazy process pool per API process with a global four-job bound."""

    def __init__(self, *, executor_factory=None, context_factory=None):
        # Factories are private seams for deterministic unit tests, not runtime
        # configuration. Production always uses ProcessPoolExecutor + spawn.
        self._executor_factory = executor_factory or ProcessPoolExecutor
        self._context_factory = context_factory or get_context
        self._executor = None
        self._generation = 0
        self._lifecycle_epoch = 0
        self._lock = Lock()
        self._condition = Condition(self._lock)
        self._slots = BoundedSemaphore(_MAX_IN_FLIGHT_GROUPS)
        self._closing = False

    def submit_group(self, records, facts) -> tuple[Future, int]:
        # Acquire before materializing the serialized fact subset. Concurrent
        # requests therefore cannot queue unbounded large review payloads.
        with self._lock:
            if self._closing:
                raise RuntimeError("financial review worker pool is shutting down")
            lifecycle_epoch = self._lifecycle_epoch
        self._slots.acquire()
        slot_owned = True
        executor = None
        generation = None
        try:
            payload = _worker_payload(records, facts)
            with self._lock:
                if (self._closing or
                        lifecycle_epoch != self._lifecycle_epoch):
                    raise RuntimeError("financial review worker pool is shutting down")
                if self._executor is None:
                    self._executor = self._executor_factory(
                        max_workers=_REVIEW_WORKERS,
                        mp_context=self._context_factory("spawn"),
                    )
                    self._generation += 1
                executor = self._executor
                generation = self._generation
                future = executor.submit(_review_group_worker, payload)
            future.add_done_callback(self._release_slot)
            slot_owned = False
            return future, generation
        except BrokenProcessPool:
            if executor is not None and generation is not None:
                self.discard_broken_executor(generation)
            raise
        finally:
            if slot_owned:
                self._slots.release()

    def _release_slot(self, _future):
        self._slots.release()

    def close(self):
        """Drain the current pool; a later app lifecycle may create a new one."""

        with self._condition:
            if self._closing:
                self._condition.wait_for(lambda: not self._closing)
                return
            self._closing = True
            self._lifecycle_epoch += 1
            executor = self._executor
        try:
            if executor is not None:
                executor.shutdown(wait=True)
        finally:
            with self._lock:
                if self._executor is executor:
                    self._executor = None
                self._closing = False
                self._condition.notify_all()

    def discard_broken_executor(self, generation):
        """Discard only the failed pool generation, never a newer replacement."""

        with self._lock:
            if self._closing or generation != self._generation:
                return
            executor = self._executor
            if executor is None:
                return
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)


_review_workers = _SpawnReviewPool()
atexit.register(_review_workers.close)


def shutdown_review_workers() -> None:
    """Explicit lifecycle hook for the API; atexit remains the fallback."""

    _review_workers.close()


def _issuer_groups(records: Sequence[ReviewValidationRecord]):
    groups = {}
    for record in records:
        if isinstance(record.issuer_key, str) and record.issuer_key:
            key = ("issuer", record.issuer_key)
        else:
            # Keep malformed or identity-less rows isolated for the exact same
            # validator rejection path; they are not merged under a fake issuer.
            key = ("row", record.index)
        groups.setdefault(key, []).append(record)
    return list(groups.values())


def _validate_in_groups(records, facts):
    groups = _issuer_groups(records)
    pending = {}
    outcomes = {}
    group_iter = iter(groups)

    def submit_available():
        while len(pending) < _MAX_IN_FLIGHT_GROUPS:
            try:
                group = next(group_iter)
            except StopIteration:
                return
            try:
                future, generation = _review_workers.submit_group(group, facts)
            except BrokenProcessPool as exc:
                raise _worker_failure(exc) from exc
            except Exception as exc:
                raise _worker_failure(exc) from exc
            pending[future] = generation

    submit_available()
    while pending:
        done, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
        for future in done:
            # Unexpected worker failures must fail the read; never turn them
            # into a rejected review or partial accepted-link result.
            try:
                group_results = future.result()
            except BrokenProcessPool as exc:
                _discard_broken_pool(exc, pending[future])
                raise _worker_failure(exc) from exc
            except Exception as exc:
                raise _worker_failure(exc) from exc
            for index, accepted in group_results:
                if not isinstance(index, int) or not isinstance(accepted, bool):
                    raise ValueError("financial review worker returned an invalid result")
                if index in outcomes:
                    raise ValueError("financial review worker returned a duplicate index")
                outcomes[index] = accepted
            del pending[future]
        submit_available()

    if set(outcomes) != {record.index for record in records}:
        raise ValueError("financial review worker omitted or changed a result index")
    # Results are consumed in original query order, independently of worker
    # completion order. The caller appends accepted rows in this same order.
    return [outcomes[record.index] for record in records]


def _worker_failure(exc):
    return ValueError(
        "financial review worker failed; no accepted-link result was returned "
        f"and no serial retry was attempted ({type(exc).__name__}: {exc})"
    )


def _discard_broken_pool(original_error, generation):
    try:
        _review_workers.discard_broken_executor(generation)
    except Exception as cleanup_error:
        raise _worker_failure(cleanup_error) from original_error


def validate_ordinary_reviews(
    records: Sequence[ReviewValidationRecord],
    facts: Mapping[str, object],
    *,
    s2_validator: Callable,
    machine_validator: Callable,
) -> list[bool]:
    """Run all ordinary review predicates once, with a serial small-input path."""

    if not records:
        return []
    if len({record.index for record in records}) != len(records):
        raise ValueError("ordinary review indexes must be unique")
    issuer_count = len({record.issuer_key for record in records
                        if isinstance(record.issuer_key, str) and record.issuer_key})
    if len(records) < _MIN_PARALLEL_REVIEW_COUNT or issuer_count < 2:
        return [
            _validate_one_review(
                record.row, set(record.member_ids), facts,
                s2_validator, machine_validator,
            )
            for record in records
        ]
    return _validate_in_groups(records, facts)

