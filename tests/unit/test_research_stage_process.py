import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

from aquant.operations import research_stage_process as stage


def _queue(path, status=None):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE job(job_type TEXT, status TEXT)")
        if status is not None:
            db.execute("INSERT INTO job VALUES ('FINANCIAL_CHECK', ?)", (status,))


@pytest.mark.parametrize("status", ["PENDING", "RUNNING"])
def test_active_financial_job_prevents_spawn(tmp_path, monkeypatch, status):
    metadata = tmp_path / "meta.sqlite"
    _queue(metadata, status)
    monkeypatch.setattr(stage.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("spawned during financial job"))
    assert stage._run_guarded_verifier(["unused"], metadata) == (None, "financial_busy")


def test_terminated_verifier_leaves_no_scratch(tmp_path, monkeypatch):
    metadata = tmp_path / "meta.sqlite"
    _queue(metadata)
    marker = tmp_path / "child-scratch"
    headroom = iter((3 * stage._GIB, stage._GIB // 2))
    monkeypatch.setattr(stage, "_read_headroom_bytes", lambda: next(headroom))
    command = [sys.executable, "-c", (
        "import os, sys, time; from pathlib import Path; "
        "scratch=Path(os.environ['TMPDIR']); (scratch/'partial.sqlite').write_bytes(b'partial'); "
        "Path(sys.argv[1]).write_text(str(scratch)); time.sleep(10)"
    ), str(marker)]
    assert stage._run_guarded_verifier(command, metadata) == (None, "insufficient_memory")
    scratch = Path(marker.read_text())
    assert scratch.name.startswith("aquant-stage-child-") and not scratch.exists()


@pytest.mark.parametrize("fault", ["duplicate", "nonce", "sources"])
def test_invalid_child_receipt_is_rejected(tmp_path, monkeypatch, fault):
    staged = tmp_path / "research.json"
    staged.write_text("{}")
    digest = "sha256:" + "1" * 64

    def run(command, _metadata):
        path, artifact, nonce, version = command[-4:]
        receipt = {"path": path, "artifactHash": artifact, "nonce": nonce,
                   "version": version, "sourcesHash": digest, "factsHash": digest}
        if fault == "nonce":
            receipt["nonce"] = "wrong"
        if fault == "sources":
            receipt["sourcesHash"] = []
        output = json.dumps(receipt)
        if fault == "duplicate":
            output = '{"path":"duplicate",' + output[1:]
        return output.encode(), "verified"

    monkeypatch.setattr(stage, "_run_guarded_verifier", run)
    receipt, _outcome = stage.verify_staged_scan(staged, digest, tmp_path / "meta.sqlite", "test")
    assert receipt is None
