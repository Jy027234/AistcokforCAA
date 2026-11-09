from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import zipfile
from types import SimpleNamespace
from pathlib import Path

import pytest

from tools import runtime_backup

_REAL_CHECK_FREE_SPACE = runtime_backup._check_free_space


@pytest.fixture(autouse=True)
def allow_small_temporary_test_disks(monkeypatch):
    monkeypatch.setattr(runtime_backup, "_check_free_space", lambda _path, _operation: None)


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, Path], Path]:
    roots = {group: tmp_path / "source" / group
             for group in ("runtime", "artifacts", "pipeline")}
    for root in roots.values():
        root.mkdir(parents=True)

    (roots["runtime"] / "state.json").write_text('{"snapshot":"test"}', encoding="utf-8")
    (roots["runtime"] / ".env.local").write_text("TOKEN=do-not-archive", encoding="utf-8")
    (roots["artifacts"] / "receipt.json").write_text('{"receipt":1}', encoding="utf-8")
    (roots["pipeline"] / "forward.txt").write_text("pipeline record", encoding="utf-8")

    source_db = roots["runtime"] / "meta.sqlite"
    with sqlite3.connect(source_db) as con:
        con.execute("CREATE TABLE source_state (value TEXT)")
        con.execute("INSERT INTO source_state VALUES ('live-file')")
    staged_db = tmp_path / "sqlite-backup-copy.sqlite"
    with sqlite3.connect(source_db) as source, sqlite3.connect(staged_db) as target:
        source.backup(target)
    with sqlite3.connect(staged_db) as con:
        con.execute("INSERT INTO source_state VALUES ('consistent-copy')")

    entries = []
    for group, root in roots.items():
        for path in root.rglob("*"):
            if path.is_file():
                info = path.stat()
                entries.append({"group": group, "path": path.relative_to(root).as_posix(),
                                "bytes": info.st_size, "mtime_ns": info.st_mtime_ns,
                                "ctime_ns": info.st_ctime_ns, "dev": info.st_dev,
                                "ino": info.st_ino})
    inventory = {
        "roots": {group: str(path) for group, path in roots.items()},
        "entries": entries,
        "links": [{"group": "runtime", "path": "history/archive", "target": None}],
        "errors": [],
    }
    inventory["entries"].extend([
        {"group": "runtime", "path": "meta.sqlite-wal", "bytes": 14},
        {"group": "runtime", "path": "meta.sqlite-shm", "bytes": 13},
    ])
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    return inventory_path, roots, staged_db


def _make_archive(tmp_path: Path) -> tuple[Path, dict[str, Path], Path]:
    inventory_path, roots, staged_db = _fixture(tmp_path)
    archive = tmp_path / "backup.zip"
    runtime_backup.create_backup(
        inventory_path, archive,
        live_db={("runtime", "meta.sqlite"): staged_db},
    )
    return archive, roots, staged_db


def test_backup_substitutes_sqlite_copy_and_records_manifest(tmp_path):
    archive, roots, staged_db = _make_archive(tmp_path)

    result = runtime_backup.verify_backup(archive)
    assert result["verified_files"] == 4
    assert result["groups"] == ["artifacts", "pipeline", "runtime"]

    with zipfile.ZipFile(archive) as zipped:
        manifest = json.loads(zipped.read("manifest.json"))
        by_path = {(row["group"], row["path"]): row for row in manifest["entries"]}
        sqlite_member = "files/runtime/meta.sqlite"
        assert zipped.read(sqlite_member) == staged_db.read_bytes()
        assert by_path[("runtime", "meta.sqlite")]["source"] == "sqlite-backup-copy"
        assert hashlib.sha256(zipped.read(sqlite_member)).hexdigest() == by_path[("runtime", "meta.sqlite")]["sha256"]
        assert "files/runtime/meta.sqlite-wal" not in zipped.namelist()
        assert "files/runtime/meta.sqlite-shm" not in zipped.namelist()
        assert "files/runtime/.env.local" not in zipped.namelist()
        assert manifest["links"] == [{"group": "runtime", "path": "history/archive",
                                      "target": None, "restored": False}]
        assert {(row["group"], row["path"], row["reason"])
                for row in manifest["skipped_sidecars"]} == {
            ("runtime", "meta.sqlite-wal", "covered-by-sqlite-backup"),
            ("runtime", "meta.sqlite-shm", "covered-by-sqlite-backup"),
        }
    assert (roots["runtime"] / ".env.local").exists()


def test_cli_uses_explicit_sources_and_keeps_environment_values_out_of_output(tmp_path, capsys):
    inventory_path, roots, staged_db = _fixture(tmp_path)
    archive = tmp_path / "cli-backup.zip"
    backup_args = ["backup", "--inventory", str(inventory_path), "--output", str(archive)]
    for group, root in roots.items():
        backup_args.extend(["--root", f"{group}={root}"])
    backup_args.extend(["--live-db", f"runtime:meta.sqlite={staged_db}"])
    backup_args.extend(["--exclude", "pipeline:forward.txt=temporary diagnostic fixture"])

    assert runtime_backup.main(backup_args) == 0
    backup_output = capsys.readouterr().out
    assert "do-not-archive" not in backup_output
    assert runtime_backup.main(["verify", "--archive", str(archive)]) == 0
    assert runtime_backup.main([
        "restore", "--archive", str(archive), "--target", str(tmp_path / "cli-restore"),
        "--group", "runtime",
    ]) == 0
    assert (tmp_path / "cli-restore" / "runtime" / "meta.sqlite").read_bytes() == staged_db.read_bytes()


def test_explicit_exclusion_is_manifested_with_reason(tmp_path):
    inventory_path, _roots, staged_db = _fixture(tmp_path)
    archive = tmp_path / "excluded.zip"
    reason = "short-lived worker heartbeat"
    runtime_backup.create_backup(
        inventory_path, archive,
        live_db={("runtime", "meta.sqlite"): staged_db},
        excludes={("artifacts", "receipt.json"): reason},
    )

    verified = runtime_backup.verify_backup(archive)
    with zipfile.ZipFile(archive) as zipped:
        manifest = json.loads(zipped.read("manifest.json"))
        assert {row["group"]: row["reason"] for row in manifest["excluded"]
                if row["reason"] != "environment-file"}["artifacts"] == reason
        assert manifest["excluded_transient"] == [{
            "group": "artifacts", "path": "receipt.json", "reason": reason,
        }]
        assert "files/artifacts/receipt.json" not in zipped.namelist()
    assert verified["verified_files"] == 3


def test_live_database_snapshot_provenance_is_embedded(tmp_path):
    inventory_path, _roots, staged_db = _fixture(tmp_path)
    digest = hashlib.sha256(staged_db.read_bytes()).hexdigest()
    report = {
        "schema": "aquant.live_sqlite_snapshots.v1",
        "items": [{
            "group": "runtime", "path": "meta.sqlite",
            "source_path": "/source/runtime/meta.sqlite",
            "copy_name": "runtime-meta.sqlite",
            "started_at": "2026-11-01T10:00:00+00:00",
            "completed_at": "2026-11-01T10:00:01+00:00",
            "bytes": staged_db.stat().st_size, "sha256": digest,
            "quick_check": "ok", "sqlite_version": "3.x",
            "application_id": 0, "user_version": 1, "schema_version": 1,
            "tables": [{"name": "source_state", "rows": 2}],
        }],
    }
    archive = tmp_path / "with-live-db-provenance.zip"
    runtime_backup.create_backup(
        inventory_path, archive,
        live_db={("runtime", "meta.sqlite"): staged_db},
        live_db_report=report,
    )

    runtime_backup.verify_backup(archive)
    with zipfile.ZipFile(archive) as zipped:
        manifest = json.loads(zipped.read("manifest.json"))
    assert manifest["live_database_snapshots"] == report["items"]


def test_backup_fails_if_inventory_metadata_changed(tmp_path):
    inventory_path, roots, staged_db = _fixture(tmp_path)
    state = roots["runtime"] / "state.json"
    state.write_text('{"snapshot":"best"}', encoding="utf-8")
    info = state.stat()
    os.utime(state, ns=(info.st_atime_ns, info.st_mtime_ns + 10_000_000_000))

    with pytest.raises(runtime_backup.BackupError, match="metadata changed since inventory"):
        runtime_backup.create_backup(
            inventory_path, tmp_path / "changed.zip",
            live_db={("runtime", "meta.sqlite"): staged_db},
        )
    assert not (tmp_path / "changed.zip").exists()


def test_group_restore_checks_hashes_and_never_recreates_links(tmp_path):
    archive, roots, staged_db = _make_archive(tmp_path)
    target = tmp_path / "restored-runtime"

    result = runtime_backup.restore_backup(archive, target, group="runtime")

    assert result["group"] == "runtime"
    assert result["restored_files"] == 2
    assert result["links_not_recreated"] == 1
    assert (target / "runtime" / "meta.sqlite").read_bytes() == staged_db.read_bytes()
    assert (target / "runtime" / "state.json").read_text(encoding="utf-8") == '{"snapshot":"test"}'
    assert not (target / "runtime" / "meta.sqlite-wal").exists()
    assert not (target / "runtime" / ".env.local").exists()
    assert not (target / "runtime" / "history" / "archive").exists()
    assert not (target / "artifacts").exists()
    with pytest.raises(runtime_backup.BackupError, match="overlap an archived source root"):
        runtime_backup.restore_backup(archive, roots["runtime"] / "recovered")
    assert not (roots["runtime"] / "recovered").exists()

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    marker = occupied / "keep.txt"
    marker.write_text("preserve", encoding="utf-8")
    with pytest.raises(runtime_backup.BackupError, match="must not exist"):
        runtime_backup.restore_backup(archive, occupied, group="runtime")
    assert marker.read_text(encoding="utf-8") == "preserve"


def test_verification_and_restore_detect_member_tampering(tmp_path):
    archive, _roots, _staged_db = _make_archive(tmp_path)
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(archive, "r") as source, zipfile.ZipFile(tampered, "w") as target:
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == "files/runtime/state.json":
                payload = b'{"snapshot":"xest"}'
            target.writestr(info, payload)

    with pytest.raises(runtime_backup.BackupError, match="SHA-256"):
        runtime_backup.verify_backup(tampered)
    target = tmp_path / "tampered-restore"
    with pytest.raises(runtime_backup.BackupError, match="SHA-256"):
        runtime_backup.restore_backup(tampered, target)
    assert not target.exists()
    partial_stages = list(tmp_path.glob(".tampered-restore.restore-*"))
    assert len(partial_stages) == 1 and partial_stages[0].is_dir()


def test_backup_and_restore_reject_path_escape_and_bad_archive_path(tmp_path):
    inventory_path, roots, _staged_db = _fixture(tmp_path)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["entries"].append({"group": "runtime", "path": "../outside.txt", "bytes": 1})
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    escaped_archive = tmp_path / "escaped.zip"
    with pytest.raises(runtime_backup.BackupError, match="unsafe inventory path"):
        runtime_backup.create_backup(inventory_path, escaped_archive)
    assert not escaped_archive.exists()

    malicious = tmp_path / "malicious.zip"
    manifest = {
        "schema": runtime_backup.SCHEMA,
        "created_at": "2026-11-01T00:00:00+00:00",
        "inventory_sha256": "0" * 64,
        "groups": ["runtime"],
        "entries": [{"group": "runtime", "path": "../outside.txt",
                     "archive_name": "files/runtime/../outside.txt",
                     "bytes": 1, "sha256": hashlib.sha256(b"x").hexdigest(),
                     "source": "inventory"}],
        "excluded": [], "skipped_sidecars": [], "links": [],
    }
    with zipfile.ZipFile(malicious, "w") as zipped:
        zipped.writestr("files/runtime/../outside.txt", b"x")
        zipped.writestr("manifest.json", json.dumps(manifest))
    target = tmp_path / "malicious-restore"
    with pytest.raises(runtime_backup.BackupError, match="unsafe member path"):
        runtime_backup.restore_backup(malicious, target, group="runtime")
    assert not target.exists()
    assert roots["runtime"].is_dir()


def test_failed_backup_retains_its_partial_archive_for_review(tmp_path):
    inventory_path, _roots, _staged_db = _fixture(tmp_path)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["entries"].append({"group": "runtime", "path": "missing.json", "bytes": 3})
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")

    with pytest.raises(runtime_backup.BackupError, match="temporary file retained"):
        runtime_backup.create_backup(inventory_path, tmp_path / "failed.zip")

    retained = list(tmp_path.glob(".failed.zip.tmp-*"))
    assert len(retained) == 1
    assert zipfile.is_zipfile(retained[0])
    with zipfile.ZipFile(retained[0]) as zipped:
        assert "manifest.json" not in zipped.namelist()


def test_six_gib_guard_checks_chunks_and_retains_partial_outputs(tmp_path, monkeypatch):
    archive, _roots, staged_db = _make_archive(tmp_path)
    inventory = tmp_path / "inventory.json"
    low_disk = SimpleNamespace(total=20 * 1024**3, used=14 * 1024**3,
                               free=runtime_backup.MIN_FREE_SPACE_BYTES - 1)
    monkeypatch.setattr(runtime_backup.shutil, "disk_usage", lambda _path: low_disk)
    with pytest.raises(runtime_backup.BackupError, match="at least 6 GiB"):
        _REAL_CHECK_FREE_SPACE(tmp_path, "test")

    checks = 0

    def allow_preflight_then_check_chunk(path, operation):
        nonlocal checks
        checks += 1
        if checks > 1:
            _REAL_CHECK_FREE_SPACE(path, operation)

    monkeypatch.setattr(runtime_backup, "_check_free_space", allow_preflight_then_check_chunk)
    with pytest.raises(runtime_backup.BackupError, match="temporary file retained"):
        runtime_backup.create_backup(
            inventory, tmp_path / "low-space.zip",
            live_db={("runtime", "meta.sqlite"): staged_db},
        )
    partial_archives = list(tmp_path.glob(".low-space.zip.tmp-*"))
    assert len(partial_archives) == 1

    checks = 0
    target = tmp_path / "low-space-restore"
    with pytest.raises(runtime_backup.BackupError, match="partial staging retained"):
        runtime_backup.restore_backup(archive, target, group="runtime")
    assert not target.exists()
    partial_restores = list(tmp_path.glob(".low-space-restore.restore-*"))
    assert len(partial_restores) == 1 and partial_restores[0].is_dir()


def test_backup_refuses_reparse_source_and_output_inside_source(tmp_path):
    inventory_path, roots, staged_db = _fixture(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    linked = roots["runtime"] / "linked.txt"
    try:
        linked.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("host does not allow creating a test symlink")

    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["entries"].append({"group": "runtime", "path": "linked.txt", "bytes": 7})
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    with pytest.raises(runtime_backup.BackupError, match="reparse point"):
        runtime_backup.create_backup(
            inventory_path, tmp_path / "symlink.zip",
            live_db={("runtime", "meta.sqlite"): staged_db},
        )

    linked.unlink()
    with pytest.raises(runtime_backup.BackupError, match="inside a source root"):
        runtime_backup.create_backup(
            inventory_path, roots["runtime"] / "inside.zip",
            live_db={("runtime", "meta.sqlite"): staged_db},
        )
