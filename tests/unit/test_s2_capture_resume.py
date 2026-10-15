"""Collector checkpoint failures must leave the affected pairs retryable."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from capture_s2_retry_indices import read_checkpoint


def test_first_capture_has_no_completed_pairs(tmp_path):
    assert read_checkpoint(tmp_path / "new.checkpoint.jsonl") == {}


def test_incomplete_and_later_failed_attempts_do_not_close_pairs(tmp_path):
    entries = [
        {"instrument_id": "920982", "period_end": "2025-06-30", "complete": False},
        {"instrument_id": "920983", "period_end": "2025-06-30", "complete": True},
        {"instrument_id": "920984", "period_end": "2025-06-30", "complete": True},
        {"instrument_id": "920984", "period_end": "2025-06-30", "exception": "timeout"},
    ]
    path = tmp_path / "retry.jsonl"
    path.write_text("\n".join(json.dumps(entry) for entry in entries), encoding="utf-8")
    assert set(read_checkpoint(path)) == {("920983", "2025-06-30")}
