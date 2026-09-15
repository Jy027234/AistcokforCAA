"""每日流水线的护栏：并发锁与运行留痕（§14.2）。

流水线本身只是按顺序调已有脚本。真正会出事的是两件更朴素的事：
**两次运行重叠**、**失败无人知**。这两件都必须能用机器验，
否则"有定时任务"只是一种感觉。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.operations.pipeline import (  # noqa: E402
    PipelineBusy, PipelineLock, RunLog, RunRecord,
)


# ------------------------------------------------------------------ 锁
def test_lock_acquires_and_releases(tmp_path):
    lock = tmp_path / "daily.lock"
    with PipelineLock(lock):
        assert lock.exists()
        assert "pid=" in lock.read_text(encoding="utf-8")
    assert not lock.exists(), "退出后锁文件必须清掉，否则下次永远进不去"


def test_second_run_is_refused_not_queued(tmp_path):
    """第二次运行必须**立刻被拒**，不是排队等待。

    等下去会让两次运行都变慢，而且等待期间锁文件一直在——
    定时任务的下一次触发又会叠上来。
    """

    lock = tmp_path / "daily.lock"
    with PipelineLock(lock):
        with pytest.raises(PipelineBusy) as exc:
            with PipelineLock(lock):
                pass
        assert str(lock) in str(exc.value)
        assert "pid=" in exc.value.holder, "拒绝时要能看出是谁持有"


def test_lock_is_released_even_when_the_body_raises(tmp_path):
    lock = tmp_path / "daily.lock"
    with pytest.raises(RuntimeError):
        with PipelineLock(lock):
            raise RuntimeError("模拟流水线中途失败")
    assert not lock.exists(), "异常路径也必须释放锁"


def test_stale_lock_is_not_stolen_automatically(tmp_path):
    """崩了留下的锁**不由程序自动夺取**。

    无法区分"上一次还在跑"与"上一次崩了"；自动夺取会在长任务上
    制造两次并发写。崩了留下的锁由人清掉，而清除动作本身留下记录。
    """

    lock = tmp_path / "daily.lock"
    lock.write_text("pid=999999 at=很久以前", encoding="utf-8")   # 陈旧锁
    with pytest.raises(PipelineBusy) as exc:
        with PipelineLock(lock):
            pass
    assert "pid=999999" in exc.value.holder


# ------------------------------------------------------------------ 日志
def test_run_log_appends_and_reads_back(tmp_path):
    log = RunLog(tmp_path / "runs.jsonl")
    log.append(RunRecord(trading_day="2026-09-11", snapshot_id="snap-eod-2026-09-11",
                         outcome="PUBLISHED"))
    log.append(RunRecord(trading_day="2026-09-12", snapshot_id="-",
                         outcome="SKIPPED", reason="休市"))
    entries = log.entries()
    assert [e["trading_day"] for e in entries] == ["2026-09-11", "2026-09-12"]
    assert entries[1]["reason"] == "休市"


def test_missing_run_log_reads_as_empty_not_error(tmp_path):
    log = RunLog(tmp_path / "never-written.jsonl")
    assert log.entries() == []
    assert log.last_successful_day() is None


def test_last_successful_day_ignores_skips_and_failures(tmp_path):
    """判断"今天跑过没有"只能看**成功发布**，不能看"跑过"。"""

    log = RunLog(tmp_path / "runs.jsonl")
    log.append(RunRecord(trading_day="2026-09-11", snapshot_id="s1",
                         outcome="PUBLISHED"))
    log.append(RunRecord(trading_day="2026-09-14", snapshot_id="-",
                         outcome="SKIPPED"))
    log.append(RunRecord(trading_day="2026-09-15", snapshot_id="-",
                         outcome="FAILED", reason="采集失败"))
    assert log.last_successful_day() == "2026-09-11"


# --------------------------------------------------- 每日脚本的判定逻辑
def _daily_run_module(cache_path):
    """载入 daily_run 并把它的缓存路径指到指定文件。

    **不能**用它默认的 deploy/ 路径做断言：那是产物目录，
    干净导出里没有——第一版就是这样，被可复现闸门抓到
    （导出目录里测试红了，而本机全绿）。
    """

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "daily_run_probe", ROOT / "tools" / "daily_run.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.CACHE = cache_path
    return module


def test_cache_last_day_reads_the_actual_data_window(tmp_path):
    """快照 ID 必须由**实际拿到的末日**决定（见 tools/daily_run.py）。

    目标日休市或数据尚未更新时，采集不会报错（只是没有新行）。
    若 ID 仍用目标日，就会出现"ID 说 A、内容是 B"——事后极难察觉。
    """

    import json

    cache = tmp_path / "bars.json"
    cache.write_text(json.dumps({"bars": {
        "SH.600000": {"rows": [{"trading_day": "2026-09-11", "close_cents": 100},
                               {"trading_day": "2026-09-14", "close_cents": 101}]},
        "SZ.000001": {"rows": [{"trading_day": "2026-09-14", "close_cents": 200}]},
    }}, ensure_ascii=False), encoding="utf-8")
    assert _daily_run_module(cache)._cache_last_day() == "2026-09-14"


def test_cache_last_day_is_none_for_an_empty_cache(tmp_path):
    import json

    cache = tmp_path / "empty.json"
    cache.write_text(json.dumps({"bars": {}}), encoding="utf-8")
    assert _daily_run_module(cache)._cache_last_day() is None


def test_cache_last_day_survives_a_corrupt_cache(tmp_path):
    """缓存损坏时必须返回 None 让调用方失败，而不是抛异常糊掉整个流程。"""

    cache = tmp_path / "broken.json"
    cache.write_text("{not json", encoding="utf-8")
    assert _daily_run_module(cache)._cache_last_day() is None
