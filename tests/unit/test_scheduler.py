"""每日任务的调度：到点判断、至多一次、以及"请求与执行分离"。

为什么这些用例重要
-----------------
调度是那种**错了也不会立刻显形**的东西：配置写错一个字段，任务就永远
不跑，而界面上一切正常（数据只是慢慢变旧）。因此这里钉住的不是"函数能算"，
而是三条容易静默失效的性质：

1. **每天至多一次**：worker 重启后不能把当天已经跑过的再跑一次
   （那会重复采集、重复发布、重复告警）；
2. **不补跑也不漏跑**：错过时刻之后当天仍应触发一次；昨天没跑不等于今天要跑两次；
3. **缺解释器就不许启用**：用错解释器时每天的失败信息是
   "baostock 未安装"——看起来像数据源坏了，而不像配置写错了。
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.data.db import apply_migrations, connect  # noqa: E402
from aquant.operations import scheduler  # noqa: E402
from aquant.operations.scheduler import (  # noqa: E402
    RunSchedule, ScheduleError, is_due, market_now, next_fire, validate_time_of_day,
)

CST = timezone(timedelta(hours=8))
INTERPRETER = r"E:\IT\Agent\.venv\Scripts\python.exe"


@pytest.fixture()
def con(tmp_path):
    connection = connect(tmp_path / "meta.sqlite")
    apply_migrations(connection)
    yield connection
    connection.close()


# ====================================================================== 校验
@pytest.mark.parametrize("value", ["20:30", "00:00", "23:59", "09:05"])
def test_accepts_hh_mm(value):
    assert validate_time_of_day(value) == value


@pytest.mark.parametrize("value", ["8:30", "20:30:00", "24:00", "2030", "", "20-30"])
def test_rejects_other_shapes_instead_of_guessing(value):
    """不接受别的形状并**顺手解析**：静默接受多种格式，最后总有一处按错的位数理解它。"""

    with pytest.raises(ScheduleError) as exc:
        validate_time_of_day(value)
    assert "HH:MM" in exc.value.repair


# ================================================================== 到点判断
def _schedule(**kw) -> RunSchedule:
    base = dict(enabled=True, run_at_local="20:30", weekdays_only=True,
                interpreter=INTERPRETER)
    base.update(kw)
    return RunSchedule(**base)


def test_next_fire_is_today_when_not_yet_passed():
    now = datetime(2026, 9, 18, 9, 0, tzinfo=CST)      # 周五上午
    assert next_fire(_schedule(), now=now) == datetime(2026, 9, 18, 20, 30, tzinfo=CST)


def test_default_scheduler_clock_is_china_market_time():
    now = market_now()
    assert now.utcoffset() == timedelta(hours=8)
    assert now.tzname() == "Asia/Shanghai"


def test_next_fire_rolls_to_next_weekday_over_the_weekend():
    """周五晚过了之后，下一次是**周一**——不是周六。"""

    now = datetime(2026, 9, 18, 21, 0, tzinfo=CST)     # 周五 21:00
    assert next_fire(_schedule(), now=now) == datetime(2026, 9, 21, 20, 30, tzinfo=CST)


def test_next_fire_can_land_on_the_weekend_when_asked():
    now = datetime(2026, 9, 18, 21, 0, tzinfo=CST)
    assert next_fire(_schedule(weekdays_only=False), now=now) \
        == datetime(2026, 9, 19, 20, 30, tzinfo=CST)


def test_disabled_schedule_has_no_next_fire():
    """停用时**不返回**一个"下一次"：显示一个不会发生的时刻比不显示更糟。"""

    assert next_fire(_schedule(enabled=False), now=datetime(2026, 9, 18, 9, tzinfo=CST)) is None


def test_due_once_the_time_has_passed():
    friday = date(2026, 9, 18)
    now = datetime(2026, 9, 18, 20, 31, tzinfo=CST)
    assert is_due(_schedule(), now=now, last_fired_for=None)
    assert is_due(_schedule(), now=now, last_fired_for=friday - timedelta(days=1))
    # **今天已经跑过就不再跑**：这是 worker 反复重启时的关键性质
    assert not is_due(_schedule(), now=now, last_fired_for=friday)


def test_not_due_before_the_time():
    assert not is_due(_schedule(), now=datetime(2026, 9, 18, 20, 29, tzinfo=CST),
                      last_fired_for=None)


def test_not_due_on_the_weekend_when_weekdays_only():
    assert not is_due(_schedule(), now=datetime(2026, 9, 19, 21, 0, tzinfo=CST),
                      last_fired_for=None)
    # 但"每天"就照跑
    assert is_due(_schedule(weekdays_only=False),
                  now=datetime(2026, 9, 19, 21, 0, tzinfo=CST), last_fired_for=None)


def test_late_start_still_fires_once_the_same_day():
    """worker 晚上 23:00 才起来：当天仍应触发一次（错过时刻不等于跳过当天）。"""

    assert is_due(_schedule(), now=datetime(2026, 9, 18, 23, 0, tzinfo=CST),
                  last_fired_for=None)


def test_disabled_is_never_due():
    assert not is_due(_schedule(enabled=False),
                      now=datetime(2026, 9, 18, 23, 0, tzinfo=CST), last_fired_for=None)


# ====================================================================== 存储
def test_default_is_disabled_and_not_written(con):
    """默认停用，且**不自动建行**：装上就每天自动抓数据是使用者没做过的决定。"""

    schedule = scheduler.load_schedule(con)
    assert schedule.enabled is False
    assert con.execute("SELECT COUNT(*) FROM run_schedule").fetchone()[0] == 0


def test_enabling_without_an_interpreter_is_refused(con):
    with pytest.raises(ScheduleError) as exc:
        scheduler.save_schedule(con, enabled=True, run_at_local="20:30",
                                weekdays_only=True, interpreter="   ",
                                data_dir="", window_start="2026-06-22",
                                actor="user:test")
    assert "解释器" in exc.value.message
    assert "baostock" in exc.value.repair


def test_disabling_without_an_interpreter_is_allowed(con):
    """停用时不需要解释器：那正是"先关掉"这条路径。"""

    schedule = scheduler.save_schedule(
        con, enabled=False, run_at_local="20:30", weekdays_only=True,
        interpreter="", data_dir="", window_start="2026-06-22", actor="user:test")
    assert schedule.enabled is False
    assert schedule.updated_by == "user:test"


def test_saving_twice_updates_the_same_row(con):
    for hour in ("20:30", "21:00"):
        scheduler.save_schedule(con, enabled=True, run_at_local=hour,
                                weekdays_only=True, interpreter=INTERPRETER,
                                data_dir="", window_start="2026-06-22",
                                actor="user:test")
    rows = con.execute("SELECT run_at_local FROM run_schedule").fetchall()
    assert len(rows) == 1, "调度是单行配置，第二次保存必须更新而不是插入"
    assert scheduler.load_schedule(con).run_at_local == "21:00"


# ================================================================ 运行请求
def test_manual_requests_do_not_queue_up(con):
    """重复点按钮不排队成一串运行：同一时刻至多一条未完成的请求。"""

    first = scheduler.request_run(con, source="manual", requested_by="user:a")
    second = scheduler.request_run(con, source="manual", requested_by="user:a")
    assert first == second
    assert con.execute("SELECT COUNT(*) FROM pipeline_run_request").fetchone()[0] == 1


def test_claim_moves_to_claimed_and_only_once(con):
    request_id = scheduler.request_run(con, source="manual", requested_by="user:a")
    claimed = scheduler.claim_next(con, worker_id="w1")
    assert claimed is not None and claimed["request_id"] == request_id
    assert scheduler.claim_next(con, worker_id="w2") is None, "同一条不能被领两次"


def test_startup_recovery_returns_claimed_to_pending(con):
    """worker 崩在 CLAIMED 之后，那条请求必须能被下一次启动捡回来。

    残留一条永远 CLAIMED 的请求，表现就是"点了按钮没反应"——
    而日志里什么错都没有。
    """

    scheduler.request_run(con, source="manual", requested_by="user:a")
    scheduler.claim_next(con, worker_id="w1")
    assert scheduler.startup_recovery(con) == 1
    assert scheduler.claim_next(con, worker_id="w2") is not None


def test_finish_and_last_run_round_trip(con):
    request_id = scheduler.request_run(con, source="manual", requested_by="user:a")
    scheduler.finish_request(con, request_id=request_id, status="DONE", exit_code=0,
                             detail="ok")
    scheduler.record_last_run(
        con, request_id=request_id, source="manual", exit_code=0,
        record={"trading_day": "2026-09-18", "snapshot_id": "snap-universe",
                "outcome": "PUBLISHED", "started_at": "2026-09-18T12:30:00+00:00",
                "finished_at": "2026-09-18T12:30:09+00:00", "duration_seconds": 9.0,
                "steps": [{"step": "采集行情", "ok": True, "seconds": 4.0}]})
    state = scheduler.status(con, now=datetime(2026, 9, 18, 9, 0, tzinfo=CST))
    assert state["lastRun"]["outcome"] == "PUBLISHED"
    assert state["lastRun"]["steps"][0]["step"] == "采集行情"
    assert scheduler.last_run_day(con) == date(2026, 9, 18)
    # 已完成的请求不再算"待处理"
    assert state["pending"] is None


def test_status_reports_no_next_fire_when_disabled(con):
    state = scheduler.status(con, now=datetime(2026, 9, 18, 9, 0, tzinfo=CST))
    assert state["schedule"]["enabled"] is False
    assert state["nextFireAt"] is None
    assert state["lastRun"] is None          # 没有运行记录 ≠ 跑了但结果为空


# ================================================================ worker 心跳
def test_worker_liveness_distinguishes_running_stale_and_stopped(tmp_path):
    started = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    heartbeat = datetime(2026, 9, 19, 12, 1, tzinfo=timezone.utc)
    scheduler.write_worker_heartbeat(
        tmp_path, worker_id="scheduler-test", pid=1234,
        started_at=started, interval_seconds=20, now=heartbeat,
    )

    live = scheduler.worker_liveness(
        tmp_path, now=heartbeat + timedelta(seconds=30))
    assert live["running"] is True
    assert live["status"] == "RUNNING"

    stale = scheduler.worker_liveness(
        tmp_path, now=heartbeat + timedelta(seconds=91))
    assert stale["running"] is False
    assert stale["status"] == "STALE"

    scheduler.write_worker_heartbeat(
        tmp_path, worker_id="scheduler-test", pid=1234,
        started_at=started, interval_seconds=20, status="STOPPED",
        now=heartbeat + timedelta(seconds=100),
    )
    stopped = scheduler.worker_liveness(
        tmp_path, now=heartbeat + timedelta(seconds=101))
    assert stopped["running"] is False
    assert stopped["status"] == "STOPPED"


def test_worker_liveness_reports_missing_heartbeat(tmp_path):
    state = scheduler.worker_liveness(tmp_path)
    assert state["running"] is False
    assert state["status"] == "MISSING"
