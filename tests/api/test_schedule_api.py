"""每日任务的接口与 worker（§14.2）。

这一层要钉住的性质只有一条，但它有几种失效方式：
**界面上的"立刻运行"必须真的会跑，且只跑一次。**

  * API 只登记请求，不自己执行——否则一次采集会挂在请求里，
    而 API 一重启当天那次就没了；
  * 请求到执行的这一段要有 worker 负责，且 worker 不在时状态必须**说出来**；
  * 缺解释器时失败的要说清是配置问题（预检），不是数据源问题。
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from aquant.operations import scheduler  # noqa: E402
from main import build_state, create_app  # noqa: E402

CST = timezone(timedelta(hours=8))
INTERPRETER = r"E:\IT\Agent\.venv\Scripts\python.exe"


@pytest.fixture()
def client(tmp_path):
    state = build_state(tmp_path)
    app = create_app(state=state)
    with TestClient(app) as c:
        yield c, state


def _get(client):
    r = client.get("/api/v1/schedule")
    assert r.status_code == 200, r.text
    return r.json()


def test_default_schedule_is_disabled(client):
    """装上就自动每天抓数据，是使用者没做过的决定——因此默认停用。"""

    c, _state = client
    body = _get(c)
    assert body["schedule"]["enabled"] is False
    assert body["nextFireAt"] is None
    assert body["lastRun"] is None


def test_readiness_exposes_unfinished_lock_skip_without_blocking_manual_data(client, tmp_path, monkeypatch):
    c, state = client
    run_log = tmp_path / "daily-runs.jsonl"
    monkeypatch.setenv("AQUANT_DAILY_RUN_LOG", str(run_log))
    monkeypatch.setattr(sys.modules["main"], "_snapshot_last_day", lambda _s: "2026-10-28")
    before = c.get("/api/v1/readiness").json()
    entries = [
        {"trading_day": "2026-10-28", "outcome": "PUBLISHED", "data_last_day": "2026-10-28"},
        {"trading_day": "2026-10-29", "outcome": "FAILED", "data_last_day": None,
         "reason": "BaoStock 登录超时"},
        {"trading_day": "2026-10-30", "outcome": "SKIPPED", "data_last_day": None,
         "reason": "流水线正在运行中（锁文件 daily-run.lock）",
         "calendarGate": {"isTradingDay": True}},
    ]
    run_log.write_text("\n".join(json.dumps(row) for row in entries), encoding="utf-8")
    body = c.get("/api/v1/readiness").json()
    assert body["ready"] == before["ready"]
    assert body["trial"]["ready"] == before["trial"]["ready"]
    assert body["dailyOperationReady"] is False
    assert body["data"]["freshness"]["stale"] is True
    assert body["data"]["freshness"]["lastAttempt"]["tradingDay"] == "2026-10-30"
    warning = next(x for x in body["trial"]["operationalWarnings"]
                   if x["code"] == "DAILY_REFRESH_SKIPPED")
    assert "锁" in warning["message"]


def test_readiness_does_not_warn_about_verified_closed_day(client, tmp_path, monkeypatch):
    c, _state = client
    run_log = tmp_path / "daily-runs.jsonl"
    monkeypatch.setenv("AQUANT_DAILY_RUN_LOG", str(run_log))
    monkeypatch.setattr(sys.modules["main"], "_snapshot_last_day", lambda _s: "2026-10-30")
    entries = [
        {"trading_day": "2026-10-30", "outcome": "PUBLISHED", "data_last_day": "2026-10-30"},
        {"trading_day": "2026-11-01", "outcome": "SKIPPED", "data_last_day": None,
         "calendarGate": {"isTradingDay": False}},
    ]
    run_log.write_text("\n".join(json.dumps(row) for row in entries), encoding="utf-8")
    body = c.get("/api/v1/readiness").json()
    codes = {x["code"] for x in body["trial"]["operationalWarnings"]}
    assert "DAILY_REFRESH_SKIPPED" not in codes
    assert body["data"]["freshness"]["stale"] is False


def test_saving_requires_an_authenticated_subject(client):
    c, _state = client
    r = c.post("/api/v1/schedule", json={
        "enabled": False, "run_at_local": "20:30", "weekdays_only": True,
        "interpreter": "", "data_dir": "", "window_start": "2026-06-22"})
    # TestClient 默认不带 X-Aquant-Subject：配置是用户动作，必须拒绝
    assert r.status_code == 401, r.text


def test_enabling_without_interpreter_is_rejected_with_repair(client):
    c, _state = client
    r = c.post("/api/v1/schedule",
               headers={"X-Aquant-Subject": "user:alice"},
               json={"enabled": True, "run_at_local": "20:30", "weekdays_only": True,
                     "interpreter": "", "data_dir": "", "window_start": "2026-06-22"})
    assert r.status_code == 422, r.text
    err = r.json()["detail"]["error"]
    assert err["code"] == "SCHEDULE_INVALID"
    assert "baostock" in err["repair_action"], err


def test_saving_and_reading_back(client):
    c, state = client
    r = c.post("/api/v1/schedule",
               headers={"X-Aquant-Subject": "user:alice"},
               json={"enabled": True, "run_at_local": "20:30", "weekdays_only": True,
                     "interpreter": INTERPRETER, "data_dir": "",
                     "window_start": "2026-06-22"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schedule"]["enabled"] is True
    assert body["schedule"]["updatedBy"] == "user:alice"
    # 下一次触发必须算出来，且是**本机时区**的一个未来时刻
    nxt = datetime.fromisoformat(body["nextFireAt"])
    assert nxt > datetime.now(nxt.tzinfo)
    # 读回来与写进去一致（同一个库、同一份事实）
    assert _get(c)["schedule"]["interpreter"] == INTERPRETER


def test_readiness_warns_when_enabled_worker_has_no_fresh_heartbeat(client):
    c, state = client
    scheduler.save_schedule(
        state.con, enabled=True, run_at_local="20:30", weekdays_only=True,
        interpreter=INTERPRETER, data_dir=str(state.data_dir),
        window_start="2026-06-22", actor="user:test",
    )

    missing = c.get("/api/v1/readiness").json()["trial"]
    assert missing["schedulerWorker"]["status"] == "MISSING"
    assert "SCHEDULER_WORKER_NOT_RUNNING" in {
        item["code"] for item in missing["operationalWarnings"]
    }

    now = datetime.now(timezone.utc)
    scheduler.write_worker_heartbeat(
        state.data_dir, worker_id="scheduler-test", pid=1234,
        started_at=now, interval_seconds=20, now=now,
    )
    running = c.get("/api/v1/readiness").json()["trial"]
    assert running["schedulerWorker"]["running"] is True
    assert "SCHEDULER_WORKER_NOT_RUNNING" not in {
        item["code"] for item in running["operationalWarnings"]
    }


def test_readiness_reports_daily_failure_without_blocking_manual_trial(
    client, tmp_path, monkeypatch,
):
    c, _state = client
    run_log = tmp_path / "daily-runs.jsonl"
    monkeypatch.setenv("AQUANT_DAILY_RUN_LOG", str(run_log))

    before = c.get("/api/v1/readiness").json()["trial"]
    entries = [
        {"trading_day": "2026-10-20", "outcome": "PUBLISHED",
         "data_last_day": "2026-10-20"},
        {"trading_day": "2026-10-21", "outcome": "FAILED",
         "data_last_day": None, "reason": "BaoStock 登录超时"},
        {"trading_day": "2026-10-22", "outcome": "FAILED",
         "data_last_day": None, "reason": "BaoStock 登录超时"},
        {"trading_day": "2026-10-23", "outcome": "FAILED",
         "data_last_day": None, "reason": "BaoStock 登录超时"},
        {"trading_day": "2026-10-25", "outcome": "SKIPPED",
         "data_last_day": None, "calendarGate": {"isTradingDay": False}},
    ]
    run_log.write_text("\n".join(json.dumps(item) for item in entries), encoding="utf-8")

    after = c.get("/api/v1/readiness").json()["trial"]
    warning = next(
        item for item in after["operationalWarnings"]
        if item["code"] == "DAILY_REFRESH_FAILED"
    )
    assert after["ready"] == before["ready"]
    assert "DATA_NOT_READY" not in {
        item["code"] for item in after["blockingIssues"]
    }
    assert "BaoStock 登录超时" in warning["message"]
    assert "2026-10-23" in warning["message"]

    entries.append({"trading_day": "2026-10-26", "outcome": "PUBLISHED",
                    "data_last_day": "2026-10-26"})
    run_log.write_text("\n".join(json.dumps(item) for item in entries), encoding="utf-8")
    after_publish = c.get("/api/v1/readiness").json()["trial"]
    assert "DAILY_REFRESH_FAILED" not in {
        item["code"] for item in after_publish["operationalWarnings"]
    }


def test_run_now_registers_a_request_but_does_not_execute(client):
    """「立刻运行」只登记请求：执行是 worker 的事。

    若这个接口自己执行，一次采集会挂在请求里几十秒，
    而客户端超时后会重试——重试又登记一条，看起来像"点了没反应"。
    """

    c, state = client
    r = c.post("/api/v1/schedule/run", headers={"X-Aquant-Subject": "user:alice"},
               json={"reason": "界面点了按钮"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requestId"].startswith("run_")
    assert body["pending"]["requestId"] == body["requestId"]
    assert body["pending"]["source"] == "manual"
    # 只是登记：没有任何运行记录被写出来
    assert body["lastRun"] is None

    row = state.con.execute(
        "SELECT status, requested_by FROM pipeline_run_request WHERE request_id=?",
        (body["requestId"],)).fetchone()
    assert row["status"] == "PENDING"
    assert row["requested_by"] == "user:alice"


def test_run_now_is_idempotent_while_pending(client):
    c, _state = client
    headers = {"X-Aquant-Subject": "user:alice"}
    first = c.post("/api/v1/schedule/run", headers=headers, json={}).json()
    second = c.post("/api/v1/schedule/run", headers=headers, json={}).json()
    assert first["requestId"] == second["requestId"], \
        "连点两次不该排成两次采集"


# ====================================================================== worker
def _worker(tmp_path, monkeypatch, *, enabled: bool, interpreter: str = sys.executable):
    """一个可测的 worker 环境：真实的（空）快照库 + 真实的数据目录。"""

    import scheduler_worker as worker

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    from aquant.domain.data.db import apply_migrations, connect

    con = connect(data_dir / "meta.sqlite")
    apply_migrations(con)
    if enabled:
        scheduler.save_schedule(con, enabled=True, run_at_local="20:30",
                                weekdays_only=True, interpreter=interpreter,
                                data_dir="", window_start="2026-06-22",
                                actor="user:test")
    monkeypatch.setattr(worker, "ROOT", tmp_path)
    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "tools" / "daily_run.py").write_text("# stub\n", encoding="utf-8")
    return worker, con, data_dir


def _write_record(json_out: Path, *, outcome: str, source: str | None = None) -> None:
    json_out.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "trading_day": "2026-09-18", "snapshot_id": "snap-universe",
        "outcome": outcome, "reason": None,
        "started_at": "2026-09-18T12:30:00+00:00",
        "finished_at": "2026-09-18T12:30:09+00:00",
        "duration_seconds": 9.0,
        "steps": [{"step": "采集行情", "ok": True, "seconds": 4.0},
                  {"step": "发布快照", "ok": True, "seconds": 1.4}],
    }
    if source is not None:
        record["source"] = source
    json_out.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


def _seed_timeout_failure(worker, con, *, root: Path,
                          finished_at: datetime | None = None,
                          source: str = "scheduler",
                          outcome: str = "FAILED",
                          retry_kind: str = "BAOSTOCK_LOGIN_TIMEOUT") -> tuple[str, Path, dict]:
    finished = finished_at or datetime(2026, 9, 18, 20, 30, tzinfo=CST)
    day = finished.date().isoformat()
    started = finished - timedelta(minutes=1)
    request_id = scheduler.request_run(
        con, source=source, requested_by=("scheduler" if source == "scheduler" else "user:alice"),
        now=started)
    status = "FAILED" if outcome == "FAILED" else "DONE"
    code = 1 if outcome == "FAILED" else 0
    scheduler.finish_request(con, request_id=request_id, status=status,
                             exit_code=code, detail="test daily result", now=finished)
    record = {
        "trading_day": day, "snapshot_id": "pending", "outcome": outcome,
        "source": source, "mode": "RESEARCH_SIMULATION", "reason": "test daily result",
        "started_at": started.isoformat(), "finished_at": finished.isoformat(),
        "retryableFailure": {"kind": retry_kind},
    }
    if retry_kind == "BAOSTOCK_CAPTURE_TRANSPORT_FAILURE":
        attempt_dir = root / "artifacts" / "transport-failure"
        checkpoint_path = attempt_dir / "simulation-evidence" / "checkpoint.json"
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_path.write_text('{"checkpoint":"current"}', encoding="utf-8")
        record["artifactDirectory"] = str(attempt_dir)
        record["retryableFailure"].update({
            "checkpointPath": str(checkpoint_path.resolve()),
            "checkpointSha256": "sha256:" + hashlib.sha256(checkpoint_path.read_bytes()).hexdigest(),
            "missingInstrumentIds": ["SH.600001"],
        })
    scheduler.record_last_run(con, request_id=request_id, source=source,
                              record=record, exit_code=code, now=finished)
    last_run_path = root / "deploy" / "agentctl-q0" / "last-run.json"
    last_run_path.parent.mkdir(parents=True, exist_ok=True)
    last_run_path.write_text(json.dumps(record), encoding="utf-8")
    return request_id, last_run_path, record


@pytest.mark.parametrize(("delay", "expected"), [
    (timedelta(minutes=14, seconds=59), "idle"),
    (timedelta(minutes=15), "fired"),
])
def test_tick_registers_same_day_timeout_retry_only_after_fifteen_minutes(
    tmp_path, monkeypatch, delay, expected,
):
    worker, con, data_dir = _worker(tmp_path, monkeypatch, enabled=True)
    monkeypatch.setenv("AQUANT_DAILY_MODE", "RESEARCH_SIMULATION")
    _original, _last_run_path, _record = _seed_timeout_failure(worker, con, root=tmp_path)
    initial_count = con.execute("SELECT COUNT(*) FROM pipeline_run_request").fetchone()[0]
    failed_at = datetime.fromisoformat(_record["finished_at"])
    result = worker.tick(con, worker_id="w1", data_dir=data_dir,
                         schedule=scheduler.load_schedule(con), now=failed_at + delay)
    assert result == expected
    rows = con.execute("SELECT requested_by,status FROM pipeline_run_request ORDER BY requested_at").fetchall()
    assert con.execute("SELECT COUNT(*) FROM pipeline_run_request").fetchone()[0] == (
        initial_count + (1 if expected == "fired" else 0))
    if expected == "fired":
        assert rows[0]["status"] == "FAILED"
        assert rows[1]["requested_by"] == worker._retry_marker_for_day("2026-09-18")
        assert rows[1]["status"] == "PENDING"
    con.close()


def test_timeout_retry_is_one_request_across_restart_and_next_tick(tmp_path, monkeypatch):
    worker, con, data_dir = _worker(tmp_path, monkeypatch, enabled=True)
    monkeypatch.setenv("AQUANT_DAILY_MODE", "RESEARCH_SIMULATION")
    original_id, _path, record = _seed_timeout_failure(worker, con, root=tmp_path)
    failed_at = datetime.fromisoformat(record["finished_at"])
    first_tick = failed_at + timedelta(minutes=15)
    assert worker.tick(con, worker_id="w1", data_dir=data_dir,
                       schedule=scheduler.load_schedule(con), now=first_tick) == "fired"
    assert con.execute("SELECT COUNT(*) FROM pipeline_run_request").fetchone()[0] == 2
    # A restarted worker sees and reuses the same pending request.
    assert scheduler.startup_recovery(con, now=first_tick + timedelta(minutes=1)) == 0
    monkeypatch.setattr(worker, "_preflight", lambda *a, **kw: [])

    def successful_run(*, interpreter, window_start, source, json_out):
        _write_record(json_out, outcome="PUBLISHED", source=source)
        return 0, "published"

    monkeypatch.setattr(worker, "_run_pipeline", successful_run)
    assert worker.tick(con, worker_id="w2", data_dir=data_dir,
                       schedule=scheduler.load_schedule(con),
                       now=first_tick + timedelta(minutes=1)) == "ran:DONE"
    assert worker.tick(con, worker_id="w2", data_dir=data_dir,
                       schedule=scheduler.load_schedule(con),
                       now=first_tick + timedelta(minutes=2)) == "idle"
    assert con.execute("SELECT COUNT(*) FROM pipeline_run_request").fetchone()[0] == 2
    original = con.execute("SELECT status FROM pipeline_run_request WHERE request_id=?",
                           (original_id,)).fetchone()
    assert original["status"] == "FAILED"
    retries = con.execute("SELECT requested_by,status FROM pipeline_run_request "
                          "WHERE requested_by=?", (worker._retry_marker_for_day("2026-09-18"),)).fetchall()
    assert len(retries) == 1 and retries[0]["status"] == "DONE"
    con.close()


@pytest.mark.parametrize("problem", [
    "disabled", "manual", "success", "wrong_kind", "different_day",
    "finished_mismatch", "cross_day", "existing_retry_marker", "daily_lock",
    "not_research_mode", "wrong_mode", "malformed_record", "pending_request", "claimed_request",
    "capture_checkpoint_changed", "naive_clock",
])
def test_tick_does_not_register_retry_without_all_required_conditions(
    tmp_path, monkeypatch, problem,
):
    worker, con, data_dir = _worker(tmp_path, monkeypatch, enabled=problem != "disabled")
    monkeypatch.setenv("AQUANT_DAILY_MODE", "RESEARCH_SIMULATION")
    finished = (datetime(2026, 9, 18, 23, 50, tzinfo=CST)
                if problem == "cross_day" else datetime(2026, 9, 18, 20, 30, tzinfo=CST))
    source = "manual" if problem == "manual" else "scheduler"
    outcome = "PUBLISHED" if problem == "success" else "FAILED"
    _request_id, path, record = _seed_timeout_failure(
        worker, con, root=tmp_path, finished_at=finished, source=source, outcome=outcome,
        retry_kind=("BAOSTOCK_CAPTURE_TRANSPORT_FAILURE"
                    if problem == "capture_checkpoint_changed" else "BAOSTOCK_LOGIN_TIMEOUT"))
    if problem == "wrong_kind":
        record["retryableFailure"]["kind"] = "OTHER"
        path.write_text(json.dumps(record), encoding="utf-8")
    elif problem == "different_day":
        record["trading_day"] = "2026-09-17"
        path.write_text(json.dumps(record), encoding="utf-8")
    elif problem == "finished_mismatch":
        record["finished_at"] = (finished + timedelta(seconds=1)).isoformat()
        path.write_text(json.dumps(record), encoding="utf-8")
    elif problem == "wrong_mode":
        record["mode"] = "PRICE_ONLY"
        path.write_text(json.dumps(record), encoding="utf-8")
    elif problem == "malformed_record":
        path.write_text("{", encoding="utf-8")
    elif problem == "existing_retry_marker":
        for attempt in (1, 2):
            retry_id = scheduler.request_run(
                con, source="scheduler",
                requested_by=worker._retry_marker_for_day("2026-09-18", attempt), now=finished)
            scheduler.finish_request(con, request_id=retry_id, status="FAILED",
                                     exit_code=1, detail="already attempted", now=finished)
    elif problem == "daily_lock":
        lock = tmp_path / "deploy" / "agentctl-q0" / "daily-run.lock"
        lock.write_text("pid=another-writer", encoding="utf-8")
    elif problem == "capture_checkpoint_changed":
        Path(record["retryableFailure"]["checkpointPath"]).write_text(
            '{"checkpoint":"changed after failure"}', encoding="utf-8")
    elif problem in {"pending_request", "claimed_request"}:
        scheduler.request_run(con, source="manual", requested_by="user:alice", now=finished)
        if problem == "claimed_request":
            scheduler.claim_next(con, worker_id="other-worker", now=finished)
    elif problem == "not_research_mode":
        monkeypatch.delenv("AQUANT_DAILY_MODE")
    elif problem == "manual":
        # A manual result cannot authorize a scheduler retry.
        pass
    now = (datetime(2026, 9, 19, 0, 10, tzinfo=CST)
           if problem == "cross_day" else finished + timedelta(minutes=15))
    if problem == "naive_clock":
        now = now.replace(tzinfo=None)
    before = con.execute("SELECT COUNT(*) FROM pipeline_run_request").fetchone()[0]
    result = worker.tick(con, worker_id="w1", data_dir=data_dir,
                         schedule=scheduler.load_schedule(con), now=now)
    assert result == ("ran:FAILED" if problem == "pending_request" else "idle")
    assert con.execute("SELECT COUNT(*) FROM pipeline_run_request").fetchone()[0] == before
    con.close()


def test_timeout_retry_dry_run_does_not_register_request(tmp_path, monkeypatch):
    worker, con, data_dir = _worker(tmp_path, monkeypatch, enabled=True)
    monkeypatch.setenv("AQUANT_DAILY_MODE", "RESEARCH_SIMULATION")
    _request_id, _path, record = _seed_timeout_failure(worker, con, root=tmp_path)
    failed_at = datetime.fromisoformat(record["finished_at"])
    before = con.execute("SELECT COUNT(*) FROM pipeline_run_request").fetchone()[0]
    result = worker.tick(con, worker_id="w1", data_dir=data_dir,
                         schedule=scheduler.load_schedule(con),
                         now=failed_at + timedelta(minutes=15), dry_run=True)
    assert result == "fired"
    assert con.execute("SELECT COUNT(*) FROM pipeline_run_request").fetchone()[0] == before
    con.close()


def test_timeout_retry_stops_after_two_additional_requests_in_one_day(tmp_path, monkeypatch):
    worker, con, data_dir = _worker(tmp_path, monkeypatch, enabled=True)
    monkeypatch.setenv("AQUANT_DAILY_MODE", "RESEARCH_SIMULATION")
    original_id, _path, original = _seed_timeout_failure(worker, con, root=tmp_path)
    finish = datetime.fromisoformat(original["finished_at"])

    assert worker.tick(con, worker_id="w1", data_dir=data_dir,
                       schedule=scheduler.load_schedule(con),
                       now=finish + timedelta(minutes=15)) == "fired"
    retry1 = con.execute("SELECT request_id FROM pipeline_run_request WHERE requested_by=?",
                         (worker._retry_marker_for_day("2026-09-18", 1),)).fetchone()[0]
    failure1 = datetime(2026, 9, 18, 20, 55, tzinfo=CST)
    _record_retry_failure(worker, con, root=tmp_path, request_id=retry1,
                          finished_at=failure1,
                          retry_kind="BAOSTOCK_CAPTURE_TRANSPORT_FAILURE")
    assert worker.tick(con, worker_id="w2", data_dir=data_dir,
                       schedule=scheduler.load_schedule(con),
                       now=failure1 + timedelta(minutes=15)) == "fired"

    retry2 = con.execute("SELECT request_id FROM pipeline_run_request WHERE requested_by=?",
                         (worker._retry_marker_for_day("2026-09-18", 2),)).fetchone()[0]
    failure2 = datetime(2026, 9, 18, 21, 20, tzinfo=CST)
    _record_retry_failure(worker, con, root=tmp_path, request_id=retry2,
                          finished_at=failure2,
                          retry_kind="BAOSTOCK_LOGIN_TIMEOUT")
    before_third_tick = con.execute("SELECT COUNT(*) FROM pipeline_run_request").fetchone()[0]
    assert worker.tick(con, worker_id="w3", data_dir=data_dir,
                       schedule=scheduler.load_schedule(con),
                       now=failure2 + timedelta(minutes=15)) == "idle"
    assert con.execute("SELECT COUNT(*) FROM pipeline_run_request").fetchone()[0] == before_third_tick == 3
    assert con.execute("SELECT status FROM pipeline_run_request WHERE request_id=?",
                       (original_id,)).fetchone()[0] == "FAILED"
    markers = con.execute("SELECT requested_by,status FROM pipeline_run_request "
                          "WHERE requested_by LIKE ? ORDER BY requested_by",
                          (worker.DAILY_TIMEOUT_RETRY_REQUESTED_BY + ":2026-09-18:%",)).fetchall()
    assert [row["requested_by"] for row in markers] == [
        worker._retry_marker_for_day("2026-09-18", 1),
        worker._retry_marker_for_day("2026-09-18", 2),
    ]
    assert [row["status"] for row in markers] == ["FAILED", "FAILED"]
    con.close()


def _record_retry_failure(worker, con, *, root: Path, request_id: str,
                          finished_at: datetime, retry_kind: str) -> None:
    started = finished_at - timedelta(minutes=2)
    record = {
        "trading_day": finished_at.date().isoformat(), "snapshot_id": "pending",
        "outcome": "FAILED", "source": "scheduler", "mode": "RESEARCH_SIMULATION",
        "reason": "test retry failure",
        "started_at": started.isoformat(), "finished_at": finished_at.isoformat(),
        "retryableFailure": {"kind": retry_kind},
    }
    if retry_kind == "BAOSTOCK_CAPTURE_TRANSPORT_FAILURE":
        attempt_dir = root / "artifacts" / "retry-transport-failure"
        checkpoint_path = attempt_dir / "simulation-evidence" / "checkpoint.json"
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_path.write_text('{"checkpoint":"second failure"}', encoding="utf-8")
        record["artifactDirectory"] = str(attempt_dir)
        record["retryableFailure"].update({
            "checkpointPath": str(checkpoint_path.resolve()),
            "checkpointSha256": "sha256:" + hashlib.sha256(checkpoint_path.read_bytes()).hexdigest(),
            "missingInstrumentIds": ["SH.600001"],
        })
    scheduler.finish_request(con, request_id=request_id, status="FAILED",
                             exit_code=1, detail=record["reason"], now=finished_at)
    scheduler.record_last_run(con, request_id=request_id, source="scheduler",
                              record=record, exit_code=1, now=finished_at)
    path = root / "deploy" / "agentctl-q0" / "last-run.json"
    path.write_text(json.dumps(record), encoding="utf-8")


def test_tick_is_idle_when_nothing_is_requested_and_not_due(tmp_path, monkeypatch):
    worker, con, data_dir = _worker(tmp_path, monkeypatch, enabled=False)
    result = worker.tick(con, worker_id="w1", data_dir=data_dir,
                         schedule=scheduler.load_schedule(con),
                         now=datetime(2026, 9, 18, 9, 0, tzinfo=CST))
    assert result == "idle"
    con.close()


def test_tick_registers_a_request_when_due(tmp_path, monkeypatch):
    worker, con, data_dir = _worker(tmp_path, monkeypatch, enabled=True)
    result = worker.tick(con, worker_id="w1", data_dir=data_dir,
                         schedule=scheduler.load_schedule(con),
                         now=datetime(2026, 9, 18, 20, 31, tzinfo=CST))
    assert result == "fired"
    row = con.execute("SELECT source, status FROM pipeline_run_request").fetchone()
    assert row["source"] == "scheduler" and row["status"] == "PENDING"
    con.close()


def test_tick_runs_the_pipeline_and_records_the_result(tmp_path, monkeypatch):
    """完整一轮：领取请求 -> 起子进程 -> 回填请求状态与运行摘要。"""

    worker, con, data_dir = _worker(tmp_path, monkeypatch, enabled=False)
    seen: dict = {}

    def fake_run(*, interpreter, window_start, source, json_out):
        seen.update(interpreter=interpreter, window_start=window_start, source=source)
        _write_record(json_out, outcome="PUBLISHED")
        return 0, "结果 PUBLISHED"

    monkeypatch.setattr(worker, "_run_pipeline", fake_run)
    # 预检要能过：缓存文件按 worker 的 ROOT 解析
    (tmp_path / "deploy" / "agentctl-q0").mkdir(parents=True, exist_ok=True)
    (tmp_path / "deploy" / "agentctl-q0" / "universe-bars.json").write_text(
        "{}", encoding="utf-8")

    request_id = scheduler.request_run(con, source="manual", requested_by="user:alice")
    result = worker.tick(con, worker_id="w1", data_dir=data_dir,
                         schedule=scheduler.load_schedule(con),
                         now=datetime(2026, 9, 18, 9, 0, tzinfo=CST))
    assert result == "ran:DONE"
    assert seen["source"] == "manual"

    row = con.execute("SELECT status, exit_code FROM pipeline_run_request "
                      "WHERE request_id=?", (request_id,)).fetchone()
    assert row["status"] == "DONE" and row["exit_code"] == 0

    last = scheduler.last_run(con)
    assert last["outcome"] == "PUBLISHED"
    assert last["source"] == "manual"
    assert [s["step"] for s in last["steps"]] == ["采集行情", "发布快照"]
    con.close()


def test_tick_fails_the_request_when_preflight_fails(tmp_path, monkeypatch):
    """解释器不存在这种确定性失败，不该等到跑了一半才发现。

    而且失败必须**写清是配置问题**：真实运行的失败信息会是
    "baostock 未安装"，那看起来像数据源坏了。
    """

    worker, con, data_dir = _worker(tmp_path, monkeypatch, enabled=False)
    request_id = scheduler.request_run(con, source="manual", requested_by="user:alice")
    result = worker.tick(
        con, worker_id="w1", data_dir=data_dir,
        schedule=scheduler.RunSchedule(enabled=False,
                                       interpreter=str(tmp_path / "nope" / "python.exe")),
        now=datetime(2026, 9, 18, 9, 0, tzinfo=CST))
    assert result == "ran:FAILED"

    row = con.execute("SELECT status, detail FROM pipeline_run_request "
                      "WHERE request_id=?", (request_id,)).fetchone()
    assert row["status"] == "FAILED"
    assert "解释器不存在" in row["detail"]
    assert scheduler.last_run(con)["outcome"] == "FAILED"
    con.close()


def test_failed_run_does_not_leave_the_request_pending(tmp_path, monkeypatch):
    """普通手动失败必须落到终态。

    留在 PENDING/CLAIMED 会让下一次 tick 反复重跑同一件事；受限恢复只
    对另有严格结构证据的定时登录接收超时失败开放。
    """

    worker, con, data_dir = _worker(tmp_path, monkeypatch, enabled=False)
    monkeypatch.setattr(worker, "_run_pipeline",
                        lambda **kw: (1, "采集失败"))
    request_id = scheduler.request_run(con, source="manual", requested_by="user:alice")
    assert worker.tick(con, worker_id="w1", data_dir=data_dir,
                       schedule=scheduler.load_schedule(con),
                       now=datetime(2026, 9, 18, 9, 0, tzinfo=CST)) == "ran:FAILED"
    assert scheduler.pending_request(con) is None
    assert worker.tick(con, worker_id="w1", data_dir=data_dir,
                       schedule=scheduler.load_schedule(con),
                       now=datetime(2026, 9, 18, 9, 1, tzinfo=CST)) == "idle"
    con.close()


def test_tick_fires_only_once_per_day(tmp_path, monkeypatch):
    """同一交易日的第二次 tick 只登记/执行一次——这条挡的是 worker 反复重启。"""

    worker, con, data_dir = _worker(tmp_path, monkeypatch, enabled=True)
    (tmp_path / "deploy" / "agentctl-q0").mkdir(parents=True, exist_ok=True)
    (tmp_path / "deploy" / "agentctl-q0" / "universe-bars.json").write_text(
        "{}", encoding="utf-8")
    monkeypatch.setattr(worker, "_run_pipeline",
                        lambda **kw: (_write_record(kw["json_out"], outcome="PUBLISHED"), 0, "ok")[1:])
    moment = datetime(2026, 9, 18, 20, 31, tzinfo=CST)
    assert worker.tick(con, worker_id="w1", data_dir=data_dir,
                       schedule=scheduler.load_schedule(con), now=moment) == "fired"
    assert worker.tick(con, worker_id="w1", data_dir=data_dir,
                       schedule=scheduler.load_schedule(con), now=moment) == "ran:DONE"
    # 第三次：今天的触发已经发生过（last_run_day = 2026-09-18），不再触发
    assert worker.tick(con, worker_id="w1", data_dir=data_dir,
                       schedule=scheduler.load_schedule(con),
                       now=moment + timedelta(minutes=1)) == "idle"
    con.close()
