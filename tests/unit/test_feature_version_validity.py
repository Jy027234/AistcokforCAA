from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.research.runs import factor_values_for_snapshot, research_run_metadata
from aquant.operations.jobs import JobError
from aquant.operations.research_jobs import JOB_FACTOR_COMPUTE, submit_research_job


ROOT = Path(__file__).resolve().parents[2]


def _snapshot_and_instrument(con: sqlite3.Connection) -> None:
    now = "2026-09-11T20:30:00+00:00"
    con.execute(
        "INSERT INTO instrument "
        "(instrument_id,exchange,board,created_at,updated_at) "
        "VALUES ('SYN.A.600519','SSE','MAIN',?,?)", (now, now))
    con.execute(
        "INSERT INTO snapshot "
        "(snapshot_id,kind,data_mode,status,input_cutoff_at,as_of_time,created_at,"
        " code_version,data_version,quality_status) "
        "VALUES ('snap-test','EOD','SYNTHETIC','PUBLISHED',?,?,?,?,?,?)",
        (now, now, now, "test", "test", "OK"),
    )


def _run(con: sqlite3.Connection, run_id: str, version: str, status: str) -> None:
    now = "2026-09-11T20:30:00+00:00"
    con.execute(
        "INSERT INTO research_run "
        "(research_run_id,snapshot_id,as_of_time,code_version,feature_version,"
        "started_at,status,output_hash) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, "snap-test", now, "test", version, now, status, "sha256:old"),
    )
    con.execute(
        "INSERT INTO feature_value "
        "(research_run_id,instrument_id,factor_id,raw_value,coverage_ratio) "
        "VALUES (?,?,?,?,?)", (run_id, "SYN.A.600519", "F10", 0.123, 1.0))


def test_migration_withdraws_old_f10_without_rewriting_values(tmp_path):
    con = connect(tmp_path / "meta.sqlite")
    con.executescript((ROOT / "schema" / "001_metadata.sql").read_text(encoding="utf-8"))
    _snapshot_and_instrument(con)
    _run(con, "rr-old", "f10-v1", "SUCCEEDED")

    con.executescript((ROOT / "schema" / "009_feature_version_validity.sql").read_text(encoding="utf-8"))

    old = con.execute(
        "SELECT status, output_hash FROM research_run WHERE research_run_id='rr-old'"
    ).fetchone()
    value = con.execute(
        "SELECT raw_value FROM feature_value WHERE research_run_id='rr-old'"
    ).fetchone()
    validity = con.execute(
        "SELECT validity_status, withdrawal_reason "
        "FROM research_run_feature_validity WHERE research_run_id='rr-old'"
    ).fetchone()
    current = con.execute(
        "SELECT status, validity_status FROM feature_version "
        "WHERE feature_version='f10-v2'"
    ).fetchone()
    assert tuple(old) == ("SUCCEEDED", "sha256:old")
    assert value[0] == 0.123
    assert validity[0] == "WITHDRAWN"
    assert "TTM" in validity[1]
    assert tuple(current) == ("ACTIVE", "VALID")

    con.close()


def test_default_snapshot_read_requires_active_valid_run(tmp_path):
    con = connect(tmp_path / "meta.sqlite")
    apply_migrations(con)
    _snapshot_and_instrument(con)
    _run(con, "rr-old", "f10-v1", "SUCCEEDED")
    _run(con, "rr-new", "f10-v2", "SUCCEEDED")
    con.executescript((ROOT / "schema" / "009_feature_version_validity.sql").read_text(encoding="utf-8"))

    rows, note = factor_values_for_snapshot(con, snapshot_id="snap-test")
    assert note is None
    assert rows[0]["research_run_id"] == "rr-new"
    assert rows[0]["feature_version"] == "f10-v2"
    assert rows[0]["validity_status"] == "VALID"
    assert research_run_metadata(con, "rr-old")["validity_status"] == "WITHDRAWN"
    con.close()


def test_reapplying_migrations_does_not_reactivate_withdrawn_f10_v2(tmp_path):
    con = connect(tmp_path / "meta.sqlite")
    apply_migrations(con)

    initial = con.execute(
        "SELECT status, validity_status, withdrawal_reason "
        "FROM feature_version WHERE feature_version='f10-v2'"
    ).fetchone()
    assert tuple(initial) == ("ACTIVE", "VALID", None)

    con.execute(
        "UPDATE feature_version SET status='WITHDRAWN', "
        "validity_status='WITHDRAWN', withdrawal_reason=? "
        "WHERE feature_version='f10-v2'",
        ("运营撤回：回归测试",),
    )

    apply_migrations(con)

    withdrawn = con.execute(
        "SELECT status, validity_status, withdrawal_reason "
        "FROM feature_version WHERE feature_version='f10-v2'"
    ).fetchone()
    assert tuple(withdrawn) == ("WITHDRAWN", "WITHDRAWN", "运营撤回：回归测试")
    con.close()


def test_factor_job_rejects_withdrawn_and_unknown_configs(tmp_path):
    con = connect(tmp_path / "meta.sqlite")
    apply_migrations(con)
    for config in ("f10-v1", "f10-v9"):
        with pytest.raises(JobError, match="unsupported|withdrawn"):
            submit_research_job(
                con, job_type=JOB_FACTOR_COMPUTE,
                trading_day="2026-09-11", snapshot_id="snap-test",
                config_version=config)
    assert con.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 0
    con.close()
