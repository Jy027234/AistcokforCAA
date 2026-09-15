"""告警：让流水线的失败能被看见（§14.2）。

告警本身很容易做成"看起来有，实际没用"。这条用例要证明三件事：

  1. **先落盘再外发**——外发失败时告警本身不能丢；
  2. 外发失败**不影响**调用方（通道坏了不该让流水线跟着红），
     但这件事本身要留下记录；
  3. 没配通道是正常状态，且返回体如实说"未外发"，
     不能让调用方以为告警已经发出去了。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.operations.alerting import (  # noqa: E402
    LEVEL_ERROR, LEVEL_WARNING, AlertLog, raise_alert,
)


@pytest.fixture(autouse=True)
def _no_webhook(monkeypatch):
    monkeypatch.delenv("AQUANT_ALERT_WEBHOOK", raising=False)


def test_alert_is_persisted_without_any_channel(tmp_path):
    log = AlertLog(tmp_path / "alerts.jsonl")
    result = raise_alert(log, level=LEVEL_ERROR, source="daily_run",
                         message="采集失败", detail={"exitCode": 1})
    assert result["alerted"] is True
    assert result["delivered"] is False
    assert "未配置" in result["deliveryError"], (
        "没配通道时必须如实说未外发，不能让调用方以为发出去了")
    entries = log.recent()
    assert len(entries) == 1
    assert entries[0]["message"] == "采集失败"
    assert entries[0]["detail"]["exitCode"] == 1


def test_alert_is_written_before_delivery_is_attempted(tmp_path):
    """外发失败时告警必须**已经**在盘上。

    先外发再落盘的话，外发一失败告警就丢了——而那正是最需要它的时候。
    """

    log = AlertLog(tmp_path / "alerts.jsonl")
    result = raise_alert(log, level=LEVEL_ERROR, source="daily_run",
                         message="快照未通过校验",
                         webhook="http://127.0.0.1:1/nope", timeout=2)
    assert result["delivered"] is False
    entries = log.recent()
    original = [e for e in entries if e["message"] == "快照未通过校验"]
    assert original, "外发失败后原始告警不见了"
    assert original[0]["level"] == LEVEL_ERROR


def test_delivery_failure_is_recorded_as_a_warning(tmp_path):
    """通知渠道坏了要留痕，但级别是 WARNING——流水线本身没坏。"""

    log = AlertLog(tmp_path / "alerts.jsonl")
    raise_alert(log, level=LEVEL_ERROR, source="daily_run", message="原始告警",
                webhook="http://127.0.0.1:1/nope", timeout=2)
    warnings = [e for e in log.recent() if e["level"] == LEVEL_WARNING]
    assert warnings, "外发失败没有留痕，下次会被误以为告警已发出"
    assert warnings[0]["source"] == "alerting"
    assert "原始告警" in warnings[0]["detail"]["original"]


def test_delivery_failure_does_not_raise(tmp_path):
    """通道坏了不该让流水线跟着红。"""

    log = AlertLog(tmp_path / "alerts.jsonl")
    # 不抛异常即为通过；返回体里如实标 delivered=False
    result = raise_alert(log, level=LEVEL_ERROR, source="daily_run",
                         message="x", webhook="http://127.0.0.1:1/nope", timeout=2)
    assert result["alerted"] is True and result["delivered"] is False


def test_unknown_level_is_rejected(tmp_path):
    log = AlertLog(tmp_path / "alerts.jsonl")
    with pytest.raises(ValueError):
        raise_alert(log, level="URGENT", source="x", message="y")
    assert log.recent() == [], "级别非法时不该写入任何东西"


def test_missing_log_reads_as_empty(tmp_path):
    log = AlertLog(tmp_path / "never.jsonl")
    assert log.recent() == []
    assert log.errors_since("2000-01-01") == []


def test_errors_since_filters_by_day_and_level(tmp_path):
    log = AlertLog(tmp_path / "alerts.jsonl")
    log.write(_alert("ERROR", "2026-09-10T00:00:00+00:00"))
    log.write(_alert("WARNING", "2026-09-14T00:00:00+00:00"))
    log.write(_alert("ERROR", "2026-09-14T00:00:00+00:00"))
    assert len(log.errors_since("2026-09-14")) == 1
    assert len(log.errors_since("2026-09-01")) == 2


def _alert(level: str, raised_at: str):
    from aquant.operations.alerting import Alert

    return Alert(level=level, source="test", message="m", raised_at=raised_at)


def test_log_is_jsonl_and_readable_line_by_line(tmp_path):
    """JSONL 的价值在于可以直接 grep / 逐行读，不用先解析整个文件。"""

    log = AlertLog(tmp_path / "alerts.jsonl")
    raise_alert(log, level=LEVEL_ERROR, source="daily_run", message="第一条")
    raise_alert(log, level=LEVEL_ERROR, source="daily_run", message="第二条")
    lines = (tmp_path / "alerts.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["message"] == "第一条"
    assert json.loads(lines[1])["message"] == "第二条"
