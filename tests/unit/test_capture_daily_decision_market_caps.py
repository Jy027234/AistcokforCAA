from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tools import capture_daily_decision_market_caps as capture_tool


BEIJING = ZoneInfo("Asia/Shanghai")


def _pool(path: Path, count: int = 120) -> list[str]:
    instruments = []
    codes = []
    for number in range(count):
        code = f"{number + 1:06d}"
        exchange = "SH" if code.startswith("6") else "SZ"
        if exchange == "SH":
            code = f"60{number:04d}"
        else:
            code = f"00{number:04d}"
        instruments.append({"instrument_id": f"{exchange}.{code}",
                            "code": ("sh" if exchange == "SH" else "sz") + code})
        codes.append(code)
    path.write_text(json.dumps({"pool_id": "pool-120", "instruments": instruments}),
                    encoding="utf-8")
    return codes


def test_load_pool_uses_nonempty_input_scope_and_rejects_duplicate_codes(tmp_path):
    path = tmp_path / "pool.json"
    codes = _pool(path, count=119)

    pool, raw, extracted = capture_tool._load_pool(path)

    assert pool["pool_id"] == "pool-120"
    assert len(raw) > 0
    assert len(extracted) == 119
    assert extracted == codes

    value = json.loads(path.read_text(encoding="utf-8"))
    value["instruments"].append(value["instruments"][0])
    path.write_text(json.dumps(value), encoding="utf-8")
    try:
        capture_tool._load_pool(path)
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("duplicate code was accepted")


def test_capture_reuses_receipted_sidecar_without_rewriting_capture_report(tmp_path, monkeypatch):
    pool_path = tmp_path / "pool.json"
    _pool(pool_path, count=5)
    env_file = tmp_path / "real-trial.env"
    env_file.write_text("AQUANT_TRUSTED_PROXY_NETWORKS=198.18.0.0/15\n", encoding="utf-8")
    attempt = tmp_path / "attempt-20261013"
    output = attempt / "caps.json"
    calendar = attempt / "calendar.json"
    coverage = attempt / "coverage.json"
    pool_hash = capture_tool._sha256(pool_path.read_bytes())
    original = {
        "schema": "aquant.daily_decision_market_cap_capture.v1",
        "status": "complete", "market_day": "2026-10-13",
        "pool_sha256": pool_hash, "started_at": "2026-10-13T15:10:00+08:00",
        "finished_at": "2026-10-13T15:12:00+08:00", "requested_count": 5,
        "accepted_count": 5, "provider_report": {"published": True,
            "receipt_id": "rcpt-original", "content_hash": "sha256:original"},
    }
    coverage.parent.mkdir(parents=True)
    original_bytes = (json.dumps(original, ensure_ascii=False, indent=2) + "\n").encode()
    coverage.write_bytes(original_bytes)
    monkeypatch.setattr(capture_tool, "_now", lambda: datetime(
        2026, 10, 13, 20, 30, tzinfo=BEIJING))
    monkeypatch.setattr(capture_tool, "_reusable_sidecar", lambda *args, **kwargs:
                        {"sidecar": object(), "coverage": original})
    monkeypatch.setattr(capture_tool.subprocess, "run",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network called")))

    code, report = capture_tool.capture(
        pool_path=pool_path, day=date(2026, 10, 13), output=output,
        archive_root=tmp_path / "archive", calendar_path=calendar,
        coverage_output=coverage, env_file=env_file,
    )

    assert code == 0
    assert report["status"] == "reused"
    assert report["started_at"] == original["started_at"]
    assert report["finished_at"] == original["finished_at"]
    assert report["reuse_checked_at"] == "2026-10-13T20:30:00+08:00"
    assert report["provider_report"]["published"] is True
    assert coverage.read_bytes() == original_bytes


def test_capture_runs_calendar_before_provider_and_records_partial_failure(tmp_path, monkeypatch):
    pool_path = tmp_path / "pool.json"
    _pool(pool_path)
    env_file = tmp_path / "real-trial.env"
    env_file.write_text("AQUANT_TRUSTED_PROXY_NETWORKS=198.18.0.0/15,2001:2::/48\n"
                        "UNRELATED_SECRET=must-not-be-read-or-printed\n", encoding="utf-8")
    output = tmp_path / "attempt" / "market-caps.json"
    archive = tmp_path / "archive"
    calendar = tmp_path / "attempt" / "calendar.json"
    coverage = tmp_path / "attempt" / "coverage.json"
    monkeypatch.setattr(capture_tool, "_now", lambda: datetime(
        2026, 10, 13, 20, 30, tzinfo=BEIJING))
    calls = []

    def fake_run(command, *, cwd, env, capture_output, text, encoding, errors, check):
        calls.append(command)
        assert env["AQUANT_TRUSTED_PROXY_NETWORKS"] == "198.18.0.0/15,2001:2::/48"
        assert "UNRELATED_SECRET" not in env  # only the explicitly saved proxy setting is loaded
        if "admit_s2_industrial_pdf_review.py" in command[1]:
            calendar.parent.mkdir(parents=True, exist_ok=True)
            calendar.write_text("{}", encoding="utf-8")
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        provider_report = {"market_day": "2026-10-13", "accepted_count": 119,
                           "complete_coverage": False,
                           "batches": [{"accepted_ids": ["SH.600001"]}],
                           "exclusions": [{"instrument_id": "SZ.000119",
                                           "reason": "wrong_market_day"}]}
        coverage.write_text(json.dumps(provider_report), encoding="utf-8")
        return type("Result", (), {"returncode": 2, "stdout": "", "stderr": "provider failed"})()

    monkeypatch.setattr(capture_tool.subprocess, "run", fake_run)

    code, report = capture_tool.capture(
        pool_path=pool_path, day=date(2026, 10, 13), output=output,
        archive_root=archive, calendar_path=calendar, coverage_output=coverage,
        env_file=env_file,
    )

    assert code == 2, report.get("failure")
    assert ["admit_s2_industrial_pdf_review.py" in item[1] for item in calls] == [True, False]
    assert report["status"] == "failed"
    assert report["accepted_count"] == 119
    assert report["requested_count"] == 120
    assert report["failed_step"] == "market_caps"
    saved = coverage.read_text(encoding="utf-8")
    assert "must-not-be-read-or-printed" not in saved
    assert "198.18.0.0/15" not in saved


def test_capture_refuses_preclose_before_any_provider_call(tmp_path, monkeypatch):
    pool_path = tmp_path / "pool.json"
    _pool(pool_path)
    env_file = tmp_path / "real-trial.env"
    env_file.write_text("AQUANT_TRUSTED_PROXY_NETWORKS=198.18.0.0/15\n", encoding="utf-8")
    monkeypatch.setattr(capture_tool, "_now", lambda: datetime(
        2026, 10, 13, 8, 17, tzinfo=BEIJING))
    monkeypatch.setattr(capture_tool.subprocess, "run",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network called")))
    attempt = tmp_path / "attempt-20261013"
    coverage = attempt / "coverage.json"

    code, report = capture_tool.capture(
        pool_path=pool_path, day=date(2026, 10, 13), output=attempt / "caps.json",
        archive_root=tmp_path / "archive", calendar_path=attempt / "calendar.json",
        coverage_output=coverage, env_file=env_file,
    )

    assert code != 0
    assert report["status"] == "failed"
    assert report["requested_count"] == 120
    assert "15:00" in report["failure"]
    assert json.loads(coverage.read_text(encoding="utf-8"))["accepted_count"] == 0
