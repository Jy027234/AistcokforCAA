"""数据新鲜度：快照落后了没有。

为什么值得单独一条用例
----------------------
这是一个**没人会自己注意到**的问题：界面一切正常、数字自洽、对账通过，
而它们基于三天前的数据。

而"上次跑成功是什么时候"不足以发现它：天天跑成功但数据源没更新，
快照照样停在几天前。因此判据是
**快照覆盖的末日 vs 采集实际拿到的末日**，与运行是否成功无关。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from datetime import date  # noqa: E402

from aquant.operations.freshness import (  # noqa: E402
    STALE_AFTER_DAYS, age_from_snapshot, freshness,
)


def _write(path: Path, entries: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries),
                    encoding="utf-8")
    return path


def test_no_log_falls_back_to_the_snapshot_own_age(tmp_path):
    """没有留痕时不能声称"数据是新的"，但**仍要能说过期了没有**。

    快照自己带着时点，因此"这数据有多旧"不依赖任何文件。
    第一版只说"没有留痕、无法判断"，于是在手工建快照或换过数据目录时
    永远不提醒——而那恰恰是最需要提醒的时候。
    """

    result = freshness(tmp_path / "never.jsonl", snapshot_day="2026-09-14")
    assert "没有运行留痕" in result.detail
    # 与今天有关，因此断言的是"关系"而不是具体天数
    age = age_from_snapshot("2026-09-14")
    assert result.staleness_days == age
    assert result.data_last_day is None
    assert result.stale == (age is not None and age >= STALE_AFTER_DAYS)


def test_age_from_snapshot_is_pure_and_deterministic():
    """兜底判据必须可测且不依赖机器时钟。"""

    assert age_from_snapshot("2026-09-10", today=date(2026, 9, 14)) == 4
    assert age_from_snapshot("2026-09-14", today=date(2026, 9, 14)) == 0
    assert age_from_snapshot(None, today=date(2026, 9, 14)) is None
    assert age_from_snapshot("乱七八糟", today=date(2026, 9, 14)) is None


def test_very_old_snapshot_without_log_is_flagged_stale(tmp_path):
    """手工建的旧快照也必须会被标出来。"""

    result = freshness(tmp_path / "never.jsonl", snapshot_day="2026-01-01")
    assert result.stale is True
    assert "过期" in result.detail


def test_snapshot_behind_collection_is_stale(tmp_path):
    log = _write(tmp_path / "runs.jsonl", [
        {"trading_day": "2026-09-14", "outcome": "PUBLISHED",
         "data_last_day": "2026-09-14"},
        {"trading_day": "2026-09-18", "outcome": "SKIPPED",
         "data_last_day": "2026-09-18", "reason": "目标日无新行情"},
    ])
    result = freshness(log, snapshot_day="2026-09-14")
    assert result.stale is True, result.detail
    assert result.staleness_days == 4
    assert result.data_last_day == "2026-09-18"
    assert "过期" in result.detail


def test_every_day_succeeding_but_data_not_updating_is_still_stale(tmp_path):
    """这是关键情形：**天天跑成功**，但采集没有新数据。

    只看"上次成功"会认为一切正常，而快照已经落后了。
    """

    log = _write(tmp_path / "runs.jsonl", [
        {"trading_day": "2026-09-14", "outcome": "PUBLISHED",
         "data_last_day": "2026-09-14"},
        {"trading_day": "2026-09-15", "outcome": "SKIPPED",
         "data_last_day": "2026-09-14"},
        {"trading_day": "2026-09-16", "outcome": "SKIPPED",
         "data_last_day": "2026-09-14"},
        {"trading_day": "2026-09-17", "outcome": "SKIPPED",
         "data_last_day": "2026-09-14"},
        {"trading_day": "2026-09-18", "outcome": "SKIPPED",
         "data_last_day": "2026-09-18"},
    ])
    result = freshness(log, snapshot_day="2026-09-14")
    assert result.stale is True
    assert result.last_published_day == "2026-09-14"


def test_consecutive_failed_runs_do_not_reuse_an_older_data_day(tmp_path):
    """失败记录显式没有 data_last_day 时，不能回退到旧记录并报成新鲜。"""

    log = _write(tmp_path / "runs.jsonl", [
        {"trading_day": "2026-10-20", "outcome": "PUBLISHED",
         "data_last_day": "2026-10-20"},
        {"trading_day": "2026-10-21", "outcome": "FAILED",
         "data_last_day": None, "reason": "BaoStock 登录超时"},
        {"trading_day": "2026-10-22", "outcome": "FAILED",
         "data_last_day": None, "reason": "BaoStock 登录超时"},
        {"trading_day": "2026-10-23", "outcome": "FAILED",
         "data_last_day": None, "reason": "BaoStock 登录超时"},
    ])

    result = freshness(log, snapshot_day="2026-10-20")
    assert result.data_last_day is None
    assert result.stale is True
    assert result.staleness_days == 3
    assert result.last_attempt["outcome"] == "FAILED"
    assert "2026-10-23" in result.detail
    assert "2026-10-20" in result.detail
    assert "BaoStock 登录超时" in result.detail


def test_failed_attempt_on_snapshot_day_is_reported_without_inventing_a_gap(tmp_path):
    log = _write(tmp_path / "runs.jsonl", [
        {"trading_day": "2026-09-18", "outcome": "FAILED",
         "data_last_day": None, "reason": "上游连接超时"},
    ])

    result = freshness(log, snapshot_day="2026-09-18")
    assert result.stale is False
    assert result.staleness_days is None
    assert "最近一次日更" in result.detail
    assert "上游连接超时" in result.detail


def test_closed_day_skip_keeps_latest_failure_until_successful_publish(tmp_path):
    entries = [
        {"trading_day": "2026-10-20", "outcome": "PUBLISHED",
         "data_last_day": "2026-10-20"},
        {"trading_day": "2026-10-23", "outcome": "FAILED",
         "data_last_day": None, "reason": "BaoStock 登录超时"},
        {"trading_day": "2026-10-25", "outcome": "SKIPPED",
         "data_last_day": None, "calendarGate": {"isTradingDay": False}},
    ]
    log = _write(tmp_path / "runs.jsonl", entries)

    after_closed_day = freshness(log, snapshot_day="2026-10-20")
    assert after_closed_day.last_attempt["outcome"] == "FAILED"
    assert after_closed_day.last_attempt["tradingDay"] == "2026-10-23"
    assert "已核验 2026-10-25 为休市日" in after_closed_day.detail
    assert after_closed_day.stale is True
    assert "BaoStock 登录超时" in after_closed_day.detail

    entries.append({"trading_day": "2026-10-26", "outcome": "PUBLISHED",
                    "data_last_day": "2026-10-26"})
    _write(log, entries)
    after_publish = freshness(log, snapshot_day="2026-10-26")
    assert after_publish.last_attempt["outcome"] == "PUBLISHED"
    assert after_publish.stale is False
    assert after_publish.data_last_day == "2026-10-26"


def test_older_backfill_does_not_resolve_a_newer_failed_target(tmp_path):
    entries = [
        {"trading_day": "2026-10-30", "outcome": "FAILED",
         "data_last_day": None, "reason": "日更目标日 30 失败"},
        {"trading_day": "2026-10-29", "outcome": "PUBLISHED",
         "data_last_day": "2026-10-29"},
    ]
    log = _write(tmp_path / "runs.jsonl", entries)

    after_backfill = freshness(log, snapshot_day="2026-10-29")
    assert after_backfill.last_attempt["outcome"] == "PUBLISHED"
    assert after_backfill.last_attempt["tradingDay"] == "2026-10-29"
    assert after_backfill.last_published_day == "2026-10-29"
    assert after_backfill.data_last_day == "2026-10-29"
    assert after_backfill.stale is True
    assert after_backfill.staleness_days == 1
    assert "此前日更（目标日 2026-10-30）失败" in after_backfill.detail
    assert "尚未由后续成功发布恢复" in after_backfill.detail

    entries.append({"trading_day": "2026-10-30", "outcome": "PUBLISHED",
                    "data_last_day": "2026-10-30"})
    _write(log, entries)
    after_covering_publish = freshness(log, snapshot_day="2026-10-30")
    assert after_covering_publish.last_attempt["outcome"] == "PUBLISHED"
    assert after_covering_publish.last_published_day == "2026-10-30"
    assert after_covering_publish.stale is False
    assert "尚未由后续成功发布恢复" not in after_covering_publish.detail
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[0])["outcome"] == "FAILED"


def test_lock_skip_does_not_report_an_old_snapshot_as_fresh(tmp_path):
    log = _write(tmp_path / "runs.jsonl", [
        {"trading_day": "2026-10-28", "outcome": "PUBLISHED",
         "data_last_day": "2026-10-28"},
        {"trading_day": "2026-10-30", "outcome": "SKIPPED",
         "data_last_day": None, "reason": "流水线正在运行中（锁文件 daily-run.lock）"},
    ])

    result = freshness(log, snapshot_day="2026-10-28")
    assert result.last_attempt["outcome"] == "SKIPPED"
    assert result.stale is True
    assert result.staleness_days == 2
    assert result.data_last_day == "2026-10-28"
    assert "异常跳过" in result.detail
    assert "锁文件" in result.detail
    assert "仍使用覆盖到 2026-10-28 的快照" in result.detail


def test_later_lock_skip_keeps_an_unresolved_failure_visible(tmp_path):
    log = _write(tmp_path / "runs.jsonl", [
        {"trading_day": "2026-10-28", "outcome": "PUBLISHED",
         "data_last_day": "2026-10-28"},
        {"trading_day": "2026-10-29", "outcome": "FAILED",
         "data_last_day": None, "reason": "BaoStock 登录超时"},
        {"trading_day": "2026-10-30", "outcome": "SKIPPED",
         "data_last_day": None, "reason": "流水线正在运行中（锁文件 daily-run.lock）"},
    ])

    result = freshness(log, snapshot_day="2026-10-28")
    assert result.last_attempt["outcome"] == "SKIPPED"
    assert result.stale is True
    assert result.staleness_days == 2
    assert result.data_last_day is None
    assert "锁文件" in result.detail
    assert "此前日更（目标日 2026-10-29）失败" in result.detail
    assert "BaoStock 登录超时" in result.detail
    assert "尚未由后续成功发布恢复" in result.detail


def test_only_verified_closed_day_skip_is_not_an_update_warning(tmp_path):
    log = _write(tmp_path / "runs.jsonl", [
        {"trading_day": "2026-10-30", "outcome": "SKIPPED",
         "data_last_day": None, "reason": "已验证交易日历标记休市；未启动任何数据源采集",
         "calendarGate": {"isTradingDay": False}},
    ])

    result = freshness(log, snapshot_day="2026-10-28")
    assert result.last_attempt["outcome"] == "SKIPPED"
    assert result.last_attempt["calendarGate"] == {"isTradingDay": False}
    assert result.stale is False
    assert "已核验 2026-10-30 为休市日" in result.detail
    assert "异常跳过" not in result.detail


def test_weekend_gap_is_not_stale(tmp_path):
    """跨周末本来就有 2 天间隔，把正常状态做成提醒会让真提醒没人看。"""

    log = _write(tmp_path / "runs.jsonl", [
        {"trading_day": "2026-09-10", "outcome": "PUBLISHED",
         "data_last_day": "2026-09-10"},
        {"trading_day": "2026-09-12", "outcome": "SKIPPED",
         "data_last_day": "2026-09-12",
         "calendarGate": {"isTradingDay": False}},
    ])
    result = freshness(log, snapshot_day="2026-09-10")
    assert result.staleness_days == 2
    assert result.stale is False
    assert STALE_AFTER_DAYS == 4, "阈值改动会改变上一条断言的结论"


def test_threshold_boundary_is_exact(tmp_path):
    log = _write(tmp_path / "runs.jsonl", [
        {"trading_day": "2026-09-18", "outcome": "PUBLISHED",
         "data_last_day": "2026-09-18"},
    ])
    # 判据是 gap >= STALE_AFTER_DAYS（即"4 天及以上算落后"）
    assert freshness(log, snapshot_day="2026-09-15").stale is False      # 3 天
    assert freshness(log, snapshot_day="2026-09-14").stale is True       # 4 天


def test_corrupt_log_lines_are_skipped_not_fatal(tmp_path):
    """进程被杀时最后一行可能写了一半——那时恰恰最需要这个文件。"""

    path = tmp_path / "runs.jsonl"
    path.write_text(
        json.dumps({"trading_day": "2026-09-14", "outcome": "PUBLISHED",
                    "data_last_day": "2026-09-14"}) + "\n"
        + '{"trading_day": "2026-09-1', encoding="utf-8")
    result = freshness(path, snapshot_day="2026-09-14")
    assert result.last_published_day == "2026-09-14"
    assert result.stale is False


def test_missing_dates_are_reported_not_guessed(tmp_path):
    log = _write(tmp_path / "runs.jsonl", [
        {"trading_day": "2026-09-14", "outcome": "PUBLISHED"},
    ])
    result = freshness(log, snapshot_day="2026-09-14")
    assert result.staleness_days is None
    assert "不判断" in result.detail


def test_attempt_payload_is_trimmed_for_the_api(tmp_path):
    """运行留痕里带着每步的输出尾巴，整个塞进 /status 会让状态接口变成日志。"""

    log = _write(tmp_path / "runs.jsonl", [
        {"trading_day": "2026-09-14", "outcome": "PUBLISHED",
         "data_last_day": "2026-09-14", "finished_at": "2026-09-15T00:00:00Z",
         "steps": [{"step": "采集行情", "ok": True,
                    "tail": ["x" * 5000], "exitCode": 0}]},
    ])
    result = freshness(log, snapshot_day="2026-09-14")
    attempt = result.as_dict()["lastAttempt"]
    assert attempt["steps"] == [{"step": "采集行情", "ok": True}]
    assert "tail" not in json.dumps(attempt)
