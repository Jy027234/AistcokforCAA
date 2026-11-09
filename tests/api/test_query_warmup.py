"""Configured query views are derived before the runtime accepts reads."""
import asyncio
import json
from pathlib import Path
import os
import sys
from datetime import datetime
from threading import Event, Lock

from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'apps/api'))
import main


def test_runtime_prepares_catalog_then_research_before_serving(tmp_path,monkeypatch):
    monkeypatch.setenv('AQUANT_DATA_DIR',str(tmp_path))
    monkeypatch.setenv('AQUANT_RESEARCH_CANDIDATES_FILE',str(tmp_path/'research.json'))
    monkeypatch.delenv('AQUANT_LIVE_FINANCIAL_FACTS_DB',raising=False)
    state=main.build_state(tmp_path)
    app=main.create_app(state)
    app.state.aquant_query_warmup_enabled=True
    seen=[]
    def catalog(**kwargs):
        assert kwargs=={'data_dir':state.data_dir,'root':state.root}
        seen.append('catalog')
        return []
    def research(path,**kwargs):
        assert path==tmp_path/'research.json' and kwargs=={'limit':1}
        seen.append('research')
        return {}
    monkeypatch.setattr(app.state.aquant_catalog,'list_published',catalog)
    monkeypatch.setattr(main,'_read_research_query',research)
    try:
        with TestClient(app) as client:
            assert seen==['catalog','research']
            assert client.get('/api/v1/health').status_code==200
    finally:
        state.con.close()


def test_warmup_failure_is_visible_and_requests_still_reject_bad_inputs(tmp_path,monkeypatch,caplog):
    monkeypatch.setenv('AQUANT_DATA_DIR',str(tmp_path))
    monkeypatch.setenv('AQUANT_RESEARCH_CANDIDATES_FILE',str(tmp_path/'research.json'))
    monkeypatch.setenv('AQUANT_IDENTITY_MODE','LOCAL_LOOPBACK_DEMO')
    monkeypatch.delenv('AQUANT_LIVE_FINANCIAL_FACTS_DB',raising=False)
    state=main.build_state(tmp_path)
    app=main.create_app(state)
    app.state.aquant_query_warmup_enabled=True
    def catalog(**kwargs):
        raise main.SnapshotError('DATA_NOT_READY','private fixture path','snapshot','repair')
    def research(path,**kwargs):
        raise main.ResearchCandidateError('private fixture source')
    monkeypatch.setattr(app.state.aquant_catalog,'list_published',catalog)
    monkeypatch.setattr(main,'_read_research_query',research)
    try:
        with TestClient(app) as client:
            assert client.get('/api/v1/health').status_code==200
            assert client.get('/api/v1/snapshots').status_code==409
            assert client.get('/api/v1/research/candidates').status_code==503
        assert 'Snapshot catalog warmup unavailable' in caplog.text
        assert 'Research query warmup unavailable' in caplog.text
        assert 'private fixture' not in caplog.text
    finally:
        state.con.close()


def _atomic_publish(path, body):
    staging = path.with_name(path.name + ".next")
    staging.write_bytes(body)
    os.replace(staging, path)


def _staged_warmup_app(tmp_path, monkeypatch, hint):
    path = tmp_path / "research.json"
    path.write_bytes(b"published")
    monkeypatch.setenv("AQUANT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AQUANT_RESEARCH_CANDIDATES_FILE", str(path))
    monkeypatch.delenv("AQUANT_LIVE_FINANCIAL_FACTS_DB", raising=False)
    state = main.build_state(tmp_path)
    app = main.create_app(state)
    app.state.aquant_query_warmup_enabled = True
    current_hint = {"value": hint}
    monkeypatch.setattr(main, "_read_research_query", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        main, "_staged_research_warmup_hint", lambda _path: current_hint["value"])
    return app, state, current_hint


async def _health_poll_and_drain(app):
    main._request_research_publication_warmup(app)
    task = app.state.aquant_research_warmup_task
    if task is not None:
        await task


def test_staged_warmup_retries_one_transient_guard_denial_after_health_cooldown(
        tmp_path, monkeypatch):
    hint = ("20261104", "attempt-one")
    app, state, _current_hint = _staged_warmup_app(tmp_path, monkeypatch, hint)
    now = {"value": 100.0}
    seen = []
    outcomes = iter((None, None))
    monkeypatch.setattr(main, "monotonic", lambda: now["value"])

    def prepare(_path, expected_hint, _metadata_db):
        seen.append(expected_hint)
        return next(outcomes)

    monkeypatch.setattr(main, "_prepare_staged_research_warmup", prepare)
    try:
        asyncio.run(_health_poll_and_drain(app))
        assert seen == [hint]
        assert app.state.aquant_staged_research_warmup_done_hint is None
        assert app.state.aquant_staged_research_warmup_retry == (hint, 110.0)

        now["value"] = 109.9
        asyncio.run(_health_poll_and_drain(app))
        assert seen == [hint]

        now["value"] = 110.0
        asyncio.run(_health_poll_and_drain(app))
        assert seen == [hint, hint]
        assert app.state.aquant_staged_research_warmup_done_hint == hint
        assert app.state.aquant_staged_research_warmup_retry is None

        now["value"] = 200.0
        asyncio.run(_health_poll_and_drain(app))
        assert seen == [hint, hint]
    finally:
        state.con.close()


def test_terminal_staged_warmup_is_skipped_and_next_hint_runs_independently(
        tmp_path, monkeypatch):
    hint_one = ("20261104", "attempt-one")
    hint_two = ("20261104", "attempt-two")
    app, state, current_hint = _staged_warmup_app(tmp_path, monkeypatch, hint_one)
    seen = []
    outcomes = iter((False, True))

    def prepare(_path, expected_hint, _metadata_db):
        seen.append(expected_hint)
        return next(outcomes)

    monkeypatch.setattr(main, "_prepare_staged_research_warmup", prepare)
    try:
        asyncio.run(_health_poll_and_drain(app))
        assert seen == [hint_one]
        assert app.state.aquant_staged_research_warmup_done_hint == hint_one

        asyncio.run(_health_poll_and_drain(app))
        assert seen == [hint_one]

        current_hint["value"] = hint_two
        asyncio.run(_health_poll_and_drain(app))
        assert seen == [hint_one, hint_two]
        assert app.state.aquant_staged_research_warmup_done_hint == hint_two
    finally:
        state.con.close()


def test_changed_staged_hint_discards_deferred_retry_and_runs_new_hint(
        tmp_path, monkeypatch):
    hint_one = ("20261104", "attempt-one")
    hint_two = ("20261104", "attempt-two")
    app, state, current_hint = _staged_warmup_app(tmp_path, monkeypatch, hint_one)
    now = {"value": 100.0}
    seen = []
    outcomes = iter((None, True))
    monkeypatch.setattr(main, "monotonic", lambda: now["value"])

    def prepare(_path, expected_hint, _metadata_db):
        seen.append(expected_hint)
        return next(outcomes)

    monkeypatch.setattr(main, "_prepare_staged_research_warmup", prepare)
    try:
        asyncio.run(_health_poll_and_drain(app))
        assert seen == [hint_one]
        assert app.state.aquant_staged_research_warmup_retry == (hint_one, 110.0)

        current_hint["value"] = hint_two
        asyncio.run(_health_poll_and_drain(app))
        assert seen == [hint_one, hint_two]
        assert app.state.aquant_staged_research_warmup_done_hint == hint_two
        assert app.state.aquant_staged_research_warmup_retry is None
    finally:
        state.con.close()


def _daily_checkpoint(tmp_path, day, *, state="COMMITTED", manifest_schema="aquant.daily_stage_checkpoint.v1",
                      snapshot_id="snap-eod-2026-11-03-0123456789abcdef"):
    day_root = tmp_path / "s2-structured" / "daily" / day
    attempt = day_root / "attempt-0123456789ab"
    attempt.mkdir(parents=True)
    staged = attempt / "research-candidates.json"
    staged.write_text("staged", encoding="utf-8")
    digest = "sha256:" + "a" * 64
    (day_root / "attempt.json").write_text(json.dumps({
        "state": state, "path": str(attempt), "snapshotId": snapshot_id,
    }), encoding="utf-8")
    (attempt / "announcements-manifest.json").write_text(json.dumps({
        "schema": manifest_schema,
        "stage": "ANNOUNCEMENTS",
        "files": {"research": {"path": str(staged), "sha256": digest}},
    }), encoding="utf-8")
    if state == "COMMITTED":
        (attempt / "publication-commit.json").write_text(json.dumps({
            "state": "COMMITTED", "snapshotId": snapshot_id, "researchHash": digest,
        }), encoding="utf-8")
    return staged, digest


def _configure_daily_checkpoint(tmp_path, monkeypatch):
    published = tmp_path / "s2-structured" / "research-candidates.json"
    published.parent.mkdir(parents=True, exist_ok=True)
    published.write_text("published", encoding="utf-8")
    monkeypatch.setenv("AQUANT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AQUANT_SNAPSHOT_ID", "snap-eod-2026-11-03-0123456789abcdef")
    monkeypatch.setattr(main, "market_now", lambda: datetime(2026, 11, 4, 13, 0))
    return published


def test_staged_warmup_selects_bounded_prior_day_checkpoint(tmp_path, monkeypatch):
    published = _configure_daily_checkpoint(tmp_path, monkeypatch)
    _daily_checkpoint(tmp_path, "20261102")  # Older than the current published snapshot.
    _daily_checkpoint(tmp_path, "20261104", manifest_schema="invalid")
    _daily_checkpoint(tmp_path, "20261105")  # Future relative to market time.
    _daily_checkpoint(tmp_path, "20261103", state="IN_PROGRESS")

    hint = main._staged_research_warmup_hint(published)

    assert hint is not None
    assert hint[0] == "20261103"
    assert "20261103" in hint[1]


def test_staged_warmup_prefers_valid_current_day_checkpoint(tmp_path, monkeypatch):
    published = _configure_daily_checkpoint(tmp_path, monkeypatch)
    _daily_checkpoint(tmp_path, "20261103")
    _daily_checkpoint(tmp_path, "20261104", state="IN_PROGRESS")

    hint = main._staged_research_warmup_hint(published)

    assert hint is not None
    assert hint[0] == "20261104"


def test_prepare_staged_warmup_accepts_matching_committed_checkpoint(tmp_path, monkeypatch):
    published = _configure_daily_checkpoint(tmp_path, monkeypatch)
    monkeypatch.setenv("AQUANT_SNAPSHOT_ID", "snap-eod-2026-11-02-0123456789abcdef")
    staged, digest = _daily_checkpoint(tmp_path, "20261103")
    hint = main._staged_research_warmup_hint(published)
    assert hint is not None
    seen = []

    def prepare(path, **kwargs):
        seen.append((path, kwargs))
        return True

    monkeypatch.setattr(main, "prepare_research_scan", prepare)

    assert main._prepare_staged_research_warmup(published, hint, tmp_path / "meta.sqlite") is True
    assert seen == [(staged, {
        "published_path": published,
        "expected_hash": digest,
        "metadata_db": tmp_path / "meta.sqlite",
    })]


def test_staged_warmup_skips_already_active_committed_checkpoint(tmp_path, monkeypatch):
    published = _configure_daily_checkpoint(tmp_path, monkeypatch)
    _daily_checkpoint(tmp_path, "20261103")

    assert main._staged_research_warmup_hint(published) is None


def test_health_coalesces_new_publications_into_one_background_warmup(
        tmp_path, monkeypatch):
    monkeypatch.setenv("AQUANT_DATA_DIR", str(tmp_path))
    path = tmp_path / "research.json"
    path.write_bytes(b"v1")
    monkeypatch.setenv("AQUANT_RESEARCH_CANDIDATES_FILE", str(path))
    monkeypatch.delenv("AQUANT_LIVE_FINANCIAL_FACTS_DB", raising=False)
    state = main.build_state(tmp_path)
    app = main.create_app(state)
    app.state.aquant_query_warmup_enabled = True
    v2_started, release_v2 = Event(), Event()
    v3_started, v3_finished = Event(), Event()
    seen = []
    in_flight = maximum_in_flight = 0
    lock = Lock()

    def research(artifact, **kwargs):
        nonlocal in_flight, maximum_in_flight
        assert artifact == path and kwargs == {"limit": 1}
        body = artifact.read_bytes()
        with lock:
            seen.append(body)
            in_flight += 1
            maximum_in_flight = max(maximum_in_flight, in_flight)
        try:
            if body == b"v2":
                v2_started.set()
                assert release_v2.wait(5)
            if body == b"v3":
                v3_started.set()
                v3_finished.set()
            return {"version": body.decode()}
        finally:
            with lock:
                in_flight -= 1

    monkeypatch.setattr(main, "_read_research_query", research)
    before_writes = state.con.total_changes
    try:
        with TestClient(app) as client:
            assert seen == [b"v1"]
            _atomic_publish(path, b"v2")
            assert client.get("/api/v1/health").status_code == 200
            assert v2_started.wait(2)

            # A second atomic publication during the first verification is
            # coalesced; health remains responsive and starts no second worker.
            _atomic_publish(path, b"v3")
            assert client.get("/api/v1/health").status_code == 200
            assert client.get("/api/v1/health").status_code == 200
            status = client.get("/api/v1/status")
            assert status.status_code == 200, status.text
            assert seen == [b"v1", b"v2"]
            assert state.con.total_changes == before_writes

            release_v2.set()
            assert v3_started.wait(3)
            assert v3_finished.wait(2)
            with lock:
                assert seen == [b"v1", b"v2", b"v3"]
                assert maximum_in_flight == 1
                assert in_flight == 0
    finally:
        release_v2.set()
        state.con.close()


def test_failed_new_publication_warmup_preserves_get_verification_error(
        tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("AQUANT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AQUANT_IDENTITY_MODE", "LOCAL_LOOPBACK_DEMO")
    path = tmp_path / "research.json"
    path.write_bytes(b"old")
    monkeypatch.setenv("AQUANT_RESEARCH_CANDIDATES_FILE", str(path))
    monkeypatch.delenv("AQUANT_LIVE_FINANCIAL_FACTS_DB", raising=False)
    state = main.build_state(tmp_path)
    app = main.create_app(state)
    app.state.aquant_query_warmup_enabled = True
    new_attempted = Event()
    error = "frozen research input has changed: facts_db"

    def research(artifact, **kwargs):
        assert kwargs.get("limit") in {1, 20}
        if artifact.read_bytes() == b"new-corrupt-source":
            new_attempted.set()
            raise main.ResearchCandidateError(error)
        return {"snapshotId": "old"}

    monkeypatch.setattr(main, "_read_research_query", research)
    try:
        with TestClient(app) as client:
            _atomic_publish(path, b"new-corrupt-source")
            assert client.get("/api/v1/health").status_code == 200
            assert new_attempted.wait(2)
            response = client.get("/api/v1/research/candidates")
            assert response.status_code == 503
            assert error in response.json()["detail"]["message"]
        assert error in caplog.text
    finally:
        state.con.close()
