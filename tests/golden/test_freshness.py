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


def test_weekend_gap_is_not_stale(tmp_path):
    """跨周末本来就有 2 天间隔，把正常状态做成提醒会让真提醒没人看。"""

    log = _write(tmp_path / "runs.jsonl", [
        {"trading_day": "2026-09-10", "outcome": "PUBLISHED",
         "data_last_day": "2026-09-10"},
        {"trading_day": "2026-09-12", "outcome": "SKIPPED",
         "data_last_day": "2026-09-12"},
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
