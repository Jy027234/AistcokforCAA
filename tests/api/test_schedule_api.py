"""每日任务的接口与 worker（§14.2）。

这一层要钉住的性质只有一条，但它有几种失效方式：
**界面上的"立刻运行"必须真的会跑，且只跑一次。**

  * API 只登记请求，不自己执行——否则一次采集会挂在请求里，
    而 API 一重启当天那次就没了；
  * 请求到执行的这一段要有 worker 负责，且 worker 不在时状态必须**说出来**；
  * 缺解释器时失败的要说清是配置问题（预检），不是数据源问题。
"""

from __future__ import annotations

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
def _worker(tmp_path, monkeypatch, *, enabled: bool, interpreter: str = INTERPRETER):
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


def _write_record(json_out: Path, *, outcome: str) -> None:
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps({
        "trading_day": "2026-09-18", "snapshot_id": "snap-universe",
        "outcome": outcome, "reason": None,
        "started_at": "2026-09-18T12:30:00+00:00",
        "finished_at": "2026-09-18T12:30:09+00:00",
        "duration_seconds": 9.0,
        "steps": [{"step": "采集行情", "ok": True, "seconds": 4.0},
                  {"step": "发布快照", "ok": True, "seconds": 1.4}],
    }, ensure_ascii=False), encoding="utf-8")


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
    """失败的请求必须落到终态。

    留在 PENDING/CLAIMED 会让下一次 tick 反复重跑同一件事——
    而"数据源坏了"会因此变成"每 20 秒重试一次"，把告警淹没。
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
