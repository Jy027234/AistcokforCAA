"""Pipeline lock lifecycle and crash recovery, using isolated temporary files."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.operations import pipeline  # noqa: E402


PIDNS = "pid:[4026533001]"
OTHER_PIDNS = "pid:[4026533002]"
BOOT = "01234567-89ab-cdef-0123-456789abcdef"
CONTAINER = "a" * 64
PROCESS_START = "12345"
REUSED_START = "67890"


def _identity(monkeypatch, *, pidns=PIDNS, boot=BOOT,
              container=CONTAINER, start=PROCESS_START):
    monkeypatch.setattr(pipeline, "_pid_namespace_id", lambda: pidns)
    monkeypatch.setattr(pipeline, "_boot_id", lambda: boot)
    monkeypatch.setattr(pipeline, "_container_id", lambda: container)
    monkeypatch.setattr(pipeline, "_process_start_ticks", lambda _pid: start)


def _write_v2_lock(path: Path, monkeypatch, *, pid=987654, pidns=PIDNS,
                   boot=BOOT, container=CONTAINER, start=PROCESS_START) -> bytes:
    _identity(monkeypatch, pidns=pidns, boot=boot, container=container, start=start)
    record = pipeline._owner_line(
        path, pid=pid, at=datetime(2026, 10, 29, 13, 9, tzinfo=timezone.utc))
    body = (record + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return body


def test_live_writer_keeps_lock_and_legacy_pid_is_not_guessed(tmp_path, monkeypatch):
    _identity(monkeypatch)
    path = tmp_path / "daily-run.lock"

    with pipeline.PipelineLock(path):
        result = pipeline.recover_orphaned_pipeline_lock(path)
        assert result.status == "ACTIVE"
        assert "仍存活" in result.detail
        assert path.is_file()
    assert not path.exists()


def test_dead_owner_is_archived_before_pipeline_lock_is_reacquired(tmp_path, monkeypatch):
    _identity(monkeypatch)
    monkeypatch.setattr(pipeline, "_pid_exists", lambda _pid: False)
    path = tmp_path / "daily-run.lock"
    original = _write_v2_lock(path, monkeypatch)

    with pipeline.PipelineLock(path):
        assert path.is_file()
        archived = list(tmp_path.glob("daily-run.lock.orphaned-*"))
        assert len(archived) == 1
        assert archived[0].read_bytes() == original
    assert not path.exists()
    assert archived[0].read_bytes() == original


def test_legacy_pid_at_lock_is_preserved_when_pid_is_absent_locally(tmp_path, monkeypatch):
    path = tmp_path / "daily-run.lock"
    original = b"pid=18544 at=2026-10-29T13:09:13.346410+00:00\n"
    path.write_bytes(original)
    monkeypatch.setattr(pipeline, "_pid_exists", lambda _pid: False)

    result = pipeline.recover_orphaned_pipeline_lock(path)

    assert result.status == "UNVERIFIED"
    assert "旧格式" in result.detail
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("daily-run.lock.orphaned-*"))


def test_pid_reuse_in_same_namespace_does_not_keep_orphan_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "_pid_exists", lambda _pid: True)
    path = tmp_path / "daily-run.lock"
    original = _write_v2_lock(path, monkeypatch, start=PROCESS_START)
    _identity(monkeypatch, start=REUSED_START)

    result = pipeline.recover_orphaned_pipeline_lock(path)

    assert result.status == "RECOVERED"
    assert result.archive_path is not None
    assert result.archive_path.read_bytes() == original
    assert not path.exists()


def test_container_restart_uses_owner_identity_without_cross_namespace_pid_check(
    tmp_path, monkeypatch,
):
    path = tmp_path / "daily-run.lock"
    original = _write_v2_lock(path, monkeypatch, pidns=OTHER_PIDNS)
    _identity(monkeypatch, pidns=PIDNS)
    path.with_name(path.name + ".lease").write_bytes(b"\0")

    def wrong_namespace_pid_check(_pid):
        raise AssertionError("must not query a PID from another namespace")

    monkeypatch.setattr(pipeline, "_pid_exists", wrong_namespace_pid_check)
    result = pipeline.recover_orphaned_pipeline_lock(path)

    assert result.status == "RECOVERED"
    assert result.archive_path is not None
    assert result.archive_path.read_bytes() == original


def test_different_runtime_owner_is_preserved(tmp_path, monkeypatch):
    path = tmp_path / "daily-run.lock"
    original = _write_v2_lock(path, monkeypatch, pidns=OTHER_PIDNS)
    _identity(monkeypatch, pidns=PIDNS, container="b" * 64)

    result = pipeline.recover_orphaned_pipeline_lock(path)

    assert result.status == "UNVERIFIED"
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("daily-run.lock.orphaned-*"))


@pytest.mark.skipif(os.name == "nt", reason="Windows byte-range locks are process-scoped")
def test_competing_recovery_calls_preserve_only_one_lock_history(tmp_path, monkeypatch):
    _identity(monkeypatch)
    monkeypatch.setattr(pipeline, "_pid_exists", lambda _pid: False)
    path = tmp_path / "daily-run.lock"
    original = _write_v2_lock(path, monkeypatch)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _unused: pipeline.recover_orphaned_pipeline_lock(path), range(2)))

    assert any(result.status == "RECOVERED" for result in results)
    assert not path.exists()
    archived = list(tmp_path.glob("daily-run.lock.orphaned-*"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == original


@pytest.mark.parametrize("text", ["", "pid=0 at=bad", "schema=aquant.pipeline_lock.v2 pid=x"])
def test_malformed_lock_is_left_for_diagnosis(tmp_path, monkeypatch, text):
    path = tmp_path / "daily-run.lock"
    original = (text + "\n").encode("utf-8")
    path.write_bytes(original)
    monkeypatch.setattr(pipeline, "_pid_exists", lambda _pid: False)

    result = pipeline.recover_orphaned_pipeline_lock(path)

    assert result.status == "UNVERIFIED"
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("daily-run.lock.orphaned-*"))
