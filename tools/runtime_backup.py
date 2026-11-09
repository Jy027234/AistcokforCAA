"""Create, verify, and selectively restore a manifest-backed runtime archive.

The tool consumes a pre-collected file inventory. It never walks a source tree,
follows reparse points, creates a SQLite snapshot, or prints file contents.
Live SQLite copies must be produced separately with SQLite's backup API and
passed with ``--live-db``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping


SCHEMA = "aquant.runtime_backup.v1"
CHUNK_SIZE = 1024 * 1024
MIN_FREE_SPACE_BYTES = 6 * 1024 ** 3
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GROUP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class BackupError(ValueError):
    """An inventory, archive, or target violates the backup contract."""


def _check_free_space(path: Path, operation: str) -> None:
    free = shutil.disk_usage(path).free
    if free < MIN_FREE_SPACE_BYTES:
        raise BackupError(
            f"{operation} stopped: free space is {free} bytes; "
            f"at least 6 GiB must remain. Any partial output is retained."
        )


def _is_reparse_info(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & reparse_flag)


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _assert_no_reparse_chain(path: Path) -> None:
    """Reject a reparse point in an absolute path's existing components."""

    absolute = path.absolute()
    if not absolute.is_absolute():
        raise BackupError(f"path must be absolute: {path}")
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            raise BackupError(f"path component does not exist: {current}") from None
        if _is_reparse_info(info):
            raise BackupError(f"reparse point is not allowed: {current}")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _relative_name(raw: object) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise BackupError("inventory path must be a non-empty relative path")
    # Treat either separator as a separator, regardless of the host OS.
    windows = PureWindowsPath(raw)
    portable = raw.replace("\\", "/")
    posix = PurePosixPath(portable)
    if windows.drive or windows.root or posix.is_absolute():
        raise BackupError(f"absolute inventory path is not allowed: {raw!r}")
    parts = portable.split("/")
    if any(part in {"", ".", ".."} or ":" in part for part in parts):
        raise BackupError(f"unsafe inventory path: {raw!r}")
    return "/".join(parts)


def _group_name(raw: object) -> str:
    if not isinstance(raw, str) or not GROUP_RE.fullmatch(raw):
        raise BackupError("group names must be short path-safe identifiers")
    return raw


def _is_environment_file(relative: str) -> bool:
    return any(part.lower() == ".env" or part.lower().startswith(".env.")
               for part in relative.split("/"))


def _require_directory(raw: str | Path, label: str) -> Path:
    path = Path(raw).expanduser().absolute()
    _assert_no_reparse_chain(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise BackupError(f"{label} is not a regular directory: {path}")
    return path.resolve(strict=True)


def _require_regular_file(raw: str | Path, label: str) -> Path:
    path = Path(raw).expanduser().absolute()
    _assert_no_reparse_chain(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise BackupError(f"{label} is not a regular file: {path}")
    return path.resolve(strict=True)


def _safe_source_file(root: Path, relative: str) -> Path:
    root_real = root.resolve(strict=True)
    current = root
    parts = relative.split("/")
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            raise BackupError(f"inventoried source file is missing: {relative}") from None
        if _is_reparse_info(info):
            raise BackupError(f"inventoried path crosses a reparse point: {relative}")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise BackupError(f"inventory parent is not a directory: {relative}")
        if index == len(parts) - 1 and not stat.S_ISREG(info.st_mode):
            raise BackupError(f"inventoried path is not a regular file: {relative}")
    resolved = current.resolve(strict=True)
    if not _is_within(resolved, root_real):
        raise BackupError(f"inventoried path escapes its source root: {relative}")
    return current


def _assert_inventory_metadata(info: os.stat_result, row: Mapping[str, Any], label: str) -> None:
    expected = {
        "bytes": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "dev": info.st_dev,
        "ino": info.st_ino,
    }
    expected["bytes"] = row.get("inventory_bytes", row.get("bytes"))
    for field in ("bytes", "mtime_ns", "dev", "ino"):
        if field in row and row[field] != expected[field]:
            raise BackupError(f"source metadata changed since inventory: {label} ({field})")


def _load_inventory(path: str | Path) -> tuple[dict[str, Any], bytes, Path]:
    inventory_path = _require_regular_file(path, "inventory")
    raw = inventory_path.read_bytes()
    try:
        inventory = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"inventory is not valid JSON ({type(exc).__name__})") from exc
    if not isinstance(inventory, dict):
        raise BackupError("inventory must be a JSON object")
    if inventory.get("errors"):
        raise BackupError("inventory contains scan errors; refusing a partial backup")
    if not isinstance(inventory.get("roots"), dict) or not inventory["roots"]:
        raise BackupError("inventory must name one or more source roots")
    if not isinstance(inventory.get("entries"), list):
        raise BackupError("inventory entries must be a list")
    if not isinstance(inventory.get("links", []), list):
        raise BackupError("inventory links must be a list")
    if inventory.get("captured_at") is not None and not isinstance(inventory["captured_at"], str):
        raise BackupError("inventory captured_at must be text")
    return inventory, raw, inventory_path


def _resolve_roots(inventory: dict[str, Any], overrides: Mapping[str, str | Path] | None
                   ) -> dict[str, Path]:
    raw_roots = inventory["roots"]
    override_map = dict(overrides or {})
    unknown = set(override_map) - set(raw_roots)
    if unknown:
        raise BackupError("root override names an unknown inventory group")
    roots: dict[str, Path] = {}
    for group, raw in raw_roots.items():
        if not isinstance(raw, str):
            raise BackupError("inventory root names and paths must be strings")
        _group_name(group)
        root = _require_directory(override_map.get(group, raw), f"source root {group}")
        roots[group] = root
    root_paths = list(roots.items())
    for index, (left_name, left) in enumerate(root_paths):
        for right_name, right in root_paths[index + 1:]:
            if _is_within(left, right) or _is_within(right, left):
                raise BackupError(f"source roots overlap: {left_name} and {right_name}")
    return roots


def _parse_live_db_key(key: tuple[str, str] | str) -> tuple[str, str]:
    if isinstance(key, tuple) and len(key) == 2:
        group, raw_path = key
    elif isinstance(key, str) and ":" in key:
        group, raw_path = key.split(":", 1)
    else:
        raise BackupError("live database key must be (group, path) or 'group:path'")
    _group_name(group)
    relative = _relative_name(raw_path)
    if not relative.lower().endswith(".sqlite"):
        raise BackupError("live database replacement must target a .sqlite inventory file")
    return group, relative


def _parse_exclude_key(key: tuple[str, str] | str) -> tuple[str, str]:
    if isinstance(key, tuple) and len(key) == 2:
        group, raw_path = key
    elif isinstance(key, str) and ":" in key:
        group, raw_path = key.split(":", 1)
    else:
        raise BackupError("exclude key must be (group, path) or 'group:path'")
    _group_name(group)
    return group, _relative_name(raw_path)


def _validate_live_db_report(report: object,
                             replacement_keys: set[tuple[str, str]]) -> list[dict[str, Any]]:
    if report is None:
        return []
    if not isinstance(report, dict) or report.get("schema") != "aquant.live_sqlite_snapshots.v1":
        raise BackupError("live database report schema is not supported")
    rows = report.get("items")
    if not isinstance(rows, list):
        raise BackupError("live database report items must be a list")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise BackupError("live database report records must be objects")
        key = _parse_live_db_key((row.get("group"), row.get("path")))
        if key in seen:
            raise BackupError("live database report contains duplicate targets")
        seen.add(key)
        if key not in replacement_keys:
            raise BackupError("live database report target has no matching replacement copy")
        if not isinstance(row.get("source_path"), str) or not isinstance(row.get("copy_name"), str):
            raise BackupError("live database report must identify source and copy")
        if not isinstance(row.get("started_at"), str) or not isinstance(row.get("completed_at"), str):
            raise BackupError("live database report must record its snapshot time window")
        size = row.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise BackupError("live database report has invalid copy size")
        digest = row.get("sha256")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise BackupError("live database report has invalid copy hash")
        if str(row.get("quick_check", "")).lower() != "ok":
            raise BackupError("live database copy did not pass SQLite quick_check")
        tables = row.get("tables")
        if not isinstance(tables, list):
            raise BackupError("live database report tables must be a list")
        normalized.append(row)
    if seen != replacement_keys:
        raise BackupError("live database report must cover every replacement copy")
    return normalized


def _prepare_output(path: str | Path, *, inventory_path: Path,
                    source_roots: Mapping[str, Path],
                    replacement_files: Mapping[tuple[str, str], Path]) -> Path:
    output = Path(path).expanduser().absolute()
    parent = output.parent
    _assert_no_reparse_chain(parent)
    if not parent.is_dir():
        raise BackupError("archive output parent must already exist")
    output = parent.resolve(strict=True) / output.name
    if _lexists(output):
        raise BackupError("archive output already exists; refusing to overwrite")
    if output == inventory_path:
        raise BackupError("archive output cannot overwrite its inventory")
    for root in source_roots.values():
        if _is_within(output, root):
            raise BackupError("archive output cannot be inside a source root")
    for replacement in replacement_files.values():
        if output == replacement:
            raise BackupError("archive output cannot overwrite a live database copy")
    return output


def _copy_hash(source: Path, destination: Any, *, space_path: Path,
               expected_metadata: Mapping[str, Any] | None = None) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    before_path = source.lstat()
    if _is_reparse_info(before_path) or not stat.S_ISREG(before_path.st_mode):
        raise BackupError(f"source changed to a reparse point or non-file: {source.name}")
    if expected_metadata is not None:
        _assert_inventory_metadata(before_path, expected_metadata, source.name)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if _is_reparse_info(before) or not stat.S_ISREG(before.st_mode):
            raise BackupError(f"opened source is not a regular file: {source.name}")
        if not os.path.samestat(before_path, before):
            raise BackupError(f"source path changed while opening: {source.name}")
        if expected_metadata is not None:
            _assert_inventory_metadata(before, expected_metadata, source.name)
        while chunk := handle.read(CHUNK_SIZE):
            _check_free_space(space_path, "archive creation")
            destination.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(handle.fileno())
    after_path = source.lstat()
    if (_is_reparse_info(after_path) or not os.path.samestat(after, after_path)
            or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
               (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or size != after.st_size):
        raise BackupError(f"source changed while being archived: {source.name}")
    if expected_metadata is not None:
        _assert_inventory_metadata(after, expected_metadata, source.name)
    return size, digest.hexdigest()


def create_backup(inventory_path: str | Path, output_path: str | Path, *,
                  roots: Mapping[str, str | Path] | None = None,
                  live_db: Mapping[tuple[str, str] | str, str | Path] | None = None,
                  excludes: Mapping[tuple[str, str] | str, str] | None = None,
                  live_db_report: dict[str, Any] | None = None,
                  ) -> dict[str, Any]:
    """Create a compressed archive from a pre-collected inventory.

    ``live_db`` maps an inventory ``group:path`` to a separately-created
    consistent SQLite backup file. Matching ``-wal`` and ``-shm`` inventory
    entries are omitted because SQLite's backup copy already contains their
    committed state.
    """

    inventory, raw_inventory, inventory_file = _load_inventory(inventory_path)
    source_roots = _resolve_roots(inventory, roots)
    replacements: dict[tuple[str, str], Path] = {}
    for key, raw_path in (live_db or {}).items():
        normalized_key = _parse_live_db_key(key)
        if normalized_key in replacements:
            raise BackupError("duplicate live database replacement")
        replacement = _require_regular_file(raw_path, "live database copy")
        if any(_is_within(replacement, root) for root in source_roots.values()):
            raise BackupError("live database copies must be staged outside source roots")
        if any(os.path.samefile(replacement, prior) for prior in replacements.values()):
            raise BackupError("each live database target requires a distinct backup copy")
        replacements[normalized_key] = replacement

    output = _prepare_output(output_path, inventory_path=inventory_file,
                             source_roots=source_roots,
                             replacement_files=replacements)
    _check_free_space(output.parent, "archive creation")
    entry_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for row in inventory["entries"]:
        if not isinstance(row, dict):
            raise BackupError("inventory entries must be objects")
        group = row.get("group")
        if not isinstance(group, str) or group not in source_roots:
            raise BackupError("inventory entry names an unknown source group")
        relative = _relative_name(row.get("path"))
        size = row.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise BackupError(f"invalid inventoried byte size: {group}:{relative}")
        key = (group, relative)
        if key in entry_rows:
            raise BackupError(f"duplicate inventory entry: {group}:{relative}")
        stored = {"group": group, "path": relative, "inventory_bytes": size}
        for field in ("mtime_ns", "ctime_ns", "dev", "ino"):
            value = row.get(field)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise BackupError(f"invalid inventoried {field}: {group}:{relative}")
                stored[field] = value
        entry_rows[key] = stored

    for key in replacements:
        row = entry_rows.get(key)
        if row is None:
            raise BackupError(f"live database target is not inventoried: {key[0]}:{key[1]}")
        original = _safe_source_file(source_roots[key[0]], key[1])
        if os.path.samefile(original, replacements[key]):
            raise BackupError(f"live database replacement is the original source: {key[0]}:{key[1]}")
    live_db_rows = _validate_live_db_report(live_db_report, set(replacements))
    live_db_report_by_key = {
        (row["group"], _relative_name(row["path"])): row for row in live_db_rows
    }

    skipped_sidecars = []
    sidecars = {
        (group, f"{relative}{suffix}")
        for group, relative in replacements
        for suffix in ("-wal", "-shm")
    }
    explicit_excludes: dict[tuple[str, str], str] = {}
    for raw_key, raw_reason in (excludes or {}).items():
        group, relative = _parse_exclude_key(raw_key)
        key = (group, relative)
        if key not in entry_rows:
            raise BackupError(f"excluded file is not inventoried: {group}:{relative}")
        if key in replacements or key in sidecars or _is_environment_file(relative):
            raise BackupError(f"cannot separately exclude a protected entry: {group}:{relative}")
        if not isinstance(raw_reason, str):
            raise BackupError("exclude reason must be text")
        reason = raw_reason.strip()
        if not reason or len(reason) > 240 or "\n" in reason or "\r" in reason:
            raise BackupError("exclude reason must be one non-empty line of at most 240 characters")
        if key in explicit_excludes:
            raise BackupError(f"duplicate explicit exclusion: {group}:{relative}")
        explicit_excludes[key] = reason
    archived: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    excluded_transient: list[dict[str, str]] = []
    links: list[dict[str, Any]] = []
    seen_link_keys: set[tuple[str, str]] = set()
    for link in inventory.get("links", []):
        if not isinstance(link, dict):
            raise BackupError("inventory link records must be objects")
        group = link.get("group")
        if not isinstance(group, str) or group not in source_roots:
            raise BackupError("inventory link names an unknown source group")
        if link.get("target") is not None and not isinstance(link.get("target"), str):
            raise BackupError("inventory link target must be text or null")
        relative = _relative_name(link.get("path"))
        key = (group, relative)
        if key in seen_link_keys or key in entry_rows:
            raise BackupError(f"duplicate or conflicting inventory link: {group}:{relative}")
        seen_link_keys.add(key)
        # Link targets are metadata only. This tool never recreates them.
        links.append({"group": group, "path": relative,
                      "target": link.get("target"), "restored": False})

    fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.tmp-", dir=output.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=3, allowZip64=True) as archive:
            for key, row in entry_rows.items():
                group, relative = key
                if key in sidecars:
                    skipped_sidecars.append({"group": group, "path": relative,
                                             "reason": "covered-by-sqlite-backup"})
                    continue
                if key in explicit_excludes:
                    row = {"group": group, "path": relative,
                           "reason": explicit_excludes[key]}
                    excluded.append(row)
                    excluded_transient.append(row)
                    continue
                if _is_environment_file(relative):
                    excluded.append({"group": group, "path": relative,
                                     "reason": "environment-file"})
                    continue
                replacement = replacements.get(key)
                if replacement is not None:
                    source = replacement
                    origin = "sqlite-backup-copy"
                else:
                    source = _safe_source_file(source_roots[group], relative)
                    _assert_inventory_metadata(source.lstat(), row, f"{group}:{relative}")
                    origin = "inventory"
                info = zipfile.ZipInfo(f"files/{group}/{relative}")
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                with archive.open(info, "w", force_zip64=True) as destination:
                    actual_size, digest = _copy_hash(
                        source, destination, space_path=output.parent,
                        expected_metadata=row if origin == "inventory" else None)
                if origin == "inventory" and actual_size != row["inventory_bytes"]:
                    raise BackupError(f"source size changed since inventory: {group}:{relative}")
                if origin == "sqlite-backup-copy" and live_db_report is not None:
                    snapshot = live_db_report_by_key[key]
                    if actual_size != snapshot["bytes"] or digest != snapshot["sha256"]:
                        raise BackupError(f"SQLite backup copy changed after validation: {group}:{relative}")
                archived.append({"group": group, "path": relative,
                                 "archive_name": info.filename,
                                 "inventory_bytes": row["inventory_bytes"],
                                 "bytes": actual_size, "sha256": digest,
                                 "source": origin})
            manifest = {
                "schema": SCHEMA,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "inventory_captured_at": inventory.get("captured_at"),
                "inventory_sha256": hashlib.sha256(raw_inventory).hexdigest(),
                "groups": sorted(source_roots),
                "source_roots": {
                    group: {"inventory": inventory["roots"][group],
                            "resolved": str(root)}
                    for group, root in source_roots.items()
                },
                "entries": archived,
                    "live_database_snapshots": live_db_rows,
                "excluded": excluded,
                "excluded_transient": excluded_transient,
                "skipped_sidecars": skipped_sidecars,
                "links": links,
            }
            manifest_bytes = json.dumps(
                manifest, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            archive.writestr("manifest.json", manifest_bytes,
                             compress_type=zipfile.ZIP_DEFLATED, compresslevel=3)
        # A hard link publishes the complete file atomically and refuses to
        # replace an output that appeared after the initial check.
        os.link(temp_path, output)
    except Exception as exc:
        # Leave any partial/complete work archive in place for root review.
        # The backup workflow explicitly retains temporary archives.
        if _lexists(temp_path):
            raise BackupError(
                f"archive was not published; temporary file retained as {temp_path.name} "
                f"({type(exc).__name__}: {exc})"
            ) from exc
        raise
    temp_alias_retained = False
    try:
        temp_path.unlink()
    except OSError:
        # The final archive is already published as a hard link. Report the
        # extra name rather than converting a successful backup into failure.
        temp_alias_retained = True

    return {"archive": output.name, "files": len(archived),
            "bytes": sum(item["bytes"] for item in archived),
            "excluded": len(excluded), "skipped_sidecars": len(skipped_sidecars),
            "excluded_transient": len(excluded_transient),
            "links_not_restored": len(links), "live_database_snapshots": len(live_db_rows),
            "temporary_alias_retained": temp_alias_retained}


def _manifest_from_archive(archive: zipfile.ZipFile) -> tuple[dict[str, Any], dict[str, zipfile.ZipInfo]]:
    infos: dict[str, zipfile.ZipInfo] = {}
    for info in archive.infolist():
        name = info.filename
        if name in infos:
            raise BackupError(f"archive has a duplicate member: {name}")
        if "\\" in name or name.startswith("/") or "\x00" in name:
            raise BackupError(f"archive has an unsafe member path: {name!r}")
        parts = name.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise BackupError(f"archive has an unsafe member path: {name!r}")
        if info.is_dir():
            raise BackupError("archive must not contain explicit directory entries")
        mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(mode)
        if file_type not in {0, stat.S_IFREG}:
            raise BackupError("archive contains a non-regular member")
        infos[name] = info
    manifest_info = infos.get("manifest.json")
    if manifest_info is None:
        raise BackupError("archive has no manifest.json")
    try:
        import io
        with archive.open(manifest_info, "r") as source:
            manifest = json.load(io.TextIOWrapper(source, encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise BackupError(f"archive manifest is unreadable ({type(exc).__name__})") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise BackupError("archive manifest schema is not supported")
    if not SHA256_RE.fullmatch(str(manifest.get("inventory_sha256", ""))):
        raise BackupError("archive manifest has an invalid inventory hash")
    groups = manifest.get("groups")
    if (not isinstance(groups, list) or not groups or
            any(not isinstance(group, str) or not group for group in groups) or
            len(set(groups)) != len(groups)):
        raise BackupError("archive manifest groups are invalid")
    for group in groups:
        _group_name(group)
    root_records = manifest.get("source_roots")
    if not isinstance(root_records, dict) or set(root_records) != set(groups):
        raise BackupError("archive manifest source roots are invalid")
    for record in root_records.values():
        if (not isinstance(record, dict) or
                not isinstance(record.get("inventory"), str) or
                not isinstance(record.get("resolved"), str)):
            raise BackupError("archive manifest source root record is invalid")
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise BackupError("archive manifest entries must be a list")
    expected_names = {"manifest.json"}
    seen_paths: set[tuple[str, str]] = set()
    for row in entries:
        if not isinstance(row, dict):
            raise BackupError("archive manifest file records must be objects")
        group = row.get("group")
        if not isinstance(group, str) or group not in groups:
            raise BackupError("archive entry names an unknown group")
        _group_name(group)
        relative = _relative_name(row.get("path"))
        if _is_environment_file(relative):
            raise BackupError("environment files are not allowed in this archive")
        key = (group, relative)
        if key in seen_paths:
            raise BackupError(f"archive manifest repeats a file: {group}:{relative}")
        seen_paths.add(key)
        name = f"files/{group}/{relative}"
        if row.get("archive_name") != name:
            raise BackupError("archive manifest member path does not match its file path")
        size, digest = row.get("bytes"), row.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise BackupError("archive manifest has an invalid file size")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise BackupError("archive manifest has an invalid file hash")
        info = infos.get(name)
        if info is None:
            raise BackupError(f"archive is missing a manifest member: {name}")
        if info.file_size != size:
            raise BackupError(f"archive member size does not match its manifest: {name}")
        expected_names.add(name)
    if expected_names != set(infos):
        raise BackupError("archive contains members missing from or extra to its manifest")
    for section in ("excluded", "excluded_transient", "skipped_sidecars", "links"):
        rows = manifest.get(section, [])
        if not isinstance(rows, list):
            raise BackupError(f"archive manifest {section} must be a list")
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("group"), str)
                    or row.get("group") not in groups):
                raise BackupError(f"archive manifest {section} record is invalid")
            _group_name(row["group"])
            relative = _relative_name(row.get("path"))
            if section in {"excluded", "excluded_transient"}:
                reason = row.get("reason")
                if (not isinstance(reason, str) or not reason.strip() or
                        len(reason) > 240 or "\n" in reason or "\r" in reason):
                    raise BackupError("archive exclusion reason is invalid")
            if section == "links":
                if row.get("restored") is not False:
                    raise BackupError("archive link metadata must say the link was not restored")
                if row.get("target") is not None and not isinstance(row.get("target"), str):
                    raise BackupError("archive link target metadata must be text or null")
    excluded_keys = {(row["group"], row["path"], row["reason"])
                     for row in manifest.get("excluded", [])}
    transient_keys = {(row["group"], row["path"], row["reason"])
                      for row in manifest.get("excluded_transient", [])}
    if not transient_keys.issubset(excluded_keys):
        raise BackupError("transient exclusions must also appear in the compatibility exclusions list")
    live_rows = manifest.get("live_database_snapshots", [])
    if not isinstance(live_rows, list):
        raise BackupError("archive live database snapshots must be a list")
    if live_rows:
        db_entries = {(row["group"], row["path"]): row for row in entries
                      if row.get("source") == "sqlite-backup-copy"}
        _validate_live_db_report({"schema": "aquant.live_sqlite_snapshots.v1",
                                  "items": live_rows}, set(db_entries))
        for row in live_rows:
            key = (row["group"], row["path"])
            entry = db_entries[key]
            if entry["bytes"] != row["bytes"] or entry["sha256"] != row["sha256"]:
                raise BackupError("live database report does not match archived copy hash")
    return manifest, infos


def _hash_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with archive.open(info, "r") as source:
        while chunk := source.read(CHUNK_SIZE):
            size += len(chunk)
            if size > info.file_size:
                raise BackupError(f"archive member expands past its declared size: {info.filename}")
            digest.update(chunk)
    return size, digest.hexdigest()


def verify_backup(archive_path: str | Path, *, group: str | None = None) -> dict[str, Any]:
    """Stream-verify member sizes and SHA-256 hashes without extracting files."""

    archive_file = _require_regular_file(archive_path, "archive")
    with zipfile.ZipFile(archive_file, "r") as archive:
        manifest, infos = _manifest_from_archive(archive)
        groups = manifest["groups"]
        if group is not None and group not in groups:
            raise BackupError("requested group is absent from the archive")
        rows = [row for row in manifest["entries"]
                if group is None or row["group"] == group]
        total_bytes = 0
        for row in rows:
            info = infos[row["archive_name"]]
            size, digest = _hash_member(archive, info)
            if size != row["bytes"] or digest != row["sha256"]:
                raise BackupError(f"archive member failed SHA-256 verification: {info.filename}")
            total_bytes += size
    return {"archive": archive_file.name, "verified_files": len(rows),
            "verified_bytes": total_bytes,
            "groups": [group] if group else groups,
            "links_not_restored": sum(1 for link in manifest.get("links", [])
                                      if group is None or link["group"] == group)}


def _absolute_source_location(raw: str) -> Path | None:
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path.resolve(strict=False)
    if PureWindowsPath(raw).is_absolute() and os.name == "nt":
        return path.resolve(strict=False)
    return None


def _safe_archive_target(target: Path, archive_file: Path,
                         source_roots: Mapping[str, Any]) -> tuple[Path, Path]:
    target = target.expanduser().absolute()
    if _lexists(target):
        raise BackupError("restore target must not exist; refusing to overwrite any directory")
    parent = _require_directory(target.parent, "restore target parent")
    target = parent / target.name
    archive_real = archive_file.resolve(strict=True)
    target_future = target.resolve(strict=False)
    if _is_within(archive_real, target_future):
        raise BackupError("archive cannot be inside the restore target")
    if target_future == archive_real:
        raise BackupError("archive and restore target cannot be the same path")
    for record in source_roots.values():
        for key in ("inventory", "resolved"):
            source = _absolute_source_location(record[key])
            if source is not None and (_is_within(target_future, source)
                                       or _is_within(source, target_future)):
                raise BackupError("restore target cannot overlap an archived source root")
    return target, parent


def restore_backup(archive_path: str | Path, target_path: str | Path, *,
                   group: str | None = None) -> dict[str, Any]:
    """Restore selected archived files into a previously non-existent target.

    Reparse points remain metadata only and are never recreated. The extracted
    bytes are checked against the manifest before the staging directory is
    published at the requested target path.
    """

    archive_file = _require_regular_file(archive_path, "archive")
    with zipfile.ZipFile(archive_file, "r") as archive:
        manifest, _infos = _manifest_from_archive(archive)
    target, parent = _safe_archive_target(
        Path(target_path), archive_file, manifest["source_roots"])
    _check_free_space(parent, "restore")
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=parent))
    selected_group: str | None = group
    try:
        with zipfile.ZipFile(archive_file, "r") as archive:
            manifest, infos = _manifest_from_archive(archive)
            groups = manifest["groups"]
            if group is not None and group not in groups:
                raise BackupError("requested group is absent from the archive")
            selected = [row for row in manifest["entries"]
                        if group is None or row["group"] == group]
            total_bytes = 0
            for row in selected:
                relative = _relative_name(row["path"])
                base = stage / row["group"]
                destination = base.joinpath(*relative.split("/"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                current = base
                for part in relative.split("/")[:-1]:
                    current = current / part
                    if _is_reparse_info(current.lstat()):
                        raise BackupError("restore path unexpectedly contains a reparse point")
                info = infos[row["archive_name"]]
                digest = hashlib.sha256()
                size = 0
                with archive.open(info, "r") as source, destination.open("xb") as output:
                    while chunk := source.read(CHUNK_SIZE):
                        size += len(chunk)
                        if size > row["bytes"]:
                            raise BackupError(f"archive member expands past its manifest: {info.filename}")
                        _check_free_space(parent, "restore")
                        output.write(chunk)
                        digest.update(chunk)
                if size != row["bytes"] or digest.hexdigest() != row["sha256"]:
                    raise BackupError(f"archive member failed SHA-256 verification: {info.filename}")
                total_bytes += size
        # On Windows rename refuses a pre-existing target. Recheck immediately
        # before publishing to avoid replacing a target created during restore.
        if _lexists(target):
            raise BackupError("restore target appeared during extraction; refusing to overwrite")
        os.rename(stage, target)
    except Exception as exc:
        # Preserve an incomplete restore for explicit inspection and cleanup.
        raise BackupError(
            f"restore failed; partial staging retained as {stage.name} "
            f"({type(exc).__name__}: {exc})"
        ) from exc

    links = [link for link in manifest.get("links", [])
             if group is None or link["group"] == group]
    excluded = [row for row in manifest.get("excluded", [])
                if group is None or row["group"] == group]
    skipped_sidecars = [row for row in manifest.get("skipped_sidecars", [])
                        if group is None or row["group"] == group]
    return {"target": target.name, "group": selected_group,
            "restored_files": len(selected), "restored_bytes": total_bytes,
            "links_not_recreated": len(links),
            "excluded_environment_files": sum(1 for row in excluded
                                               if row["reason"] == "environment-file"),
            "explicitly_excluded_files": sum(1 for row in excluded
                                              if row["reason"] != "environment-file"),
            "transiently_excluded_files": sum(1 for row in manifest.get("excluded_transient", [])
                                                if group is None or row["group"] == group),
            "sqlite_sidecars_covered_by_backup": len(skipped_sidecars)}


def _parse_assignment(value: str, *, label: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError(f"{label} must use NAME=PATH")
    name, path = value.split("=", 1)
    if not name or not path:
        raise argparse.ArgumentTypeError(f"{label} must use NAME=PATH")
    return name, path


def _parse_live_db_assignment(value: str) -> tuple[str, str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("--live-db must use GROUP:RELATIVE.sqlite=BACKUP_COPY")
    key, path = value.split("=", 1)
    if ":" not in key or not path:
        raise argparse.ArgumentTypeError("--live-db must use GROUP:RELATIVE.sqlite=BACKUP_COPY")
    group, relative = key.split(":", 1)
    try:
        _parse_live_db_key((group, relative))
    except BackupError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return group, relative, path


def _parse_exclude_assignment(value: str) -> tuple[str, str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("--exclude must use GROUP:RELATIVE=REASON")
    key, reason = value.split("=", 1)
    if ":" not in key or not reason.strip():
        raise argparse.ArgumentTypeError("--exclude must use GROUP:RELATIVE=REASON")
    group, relative = key.split(":", 1)
    try:
        _parse_exclude_key((group, relative))
    except BackupError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return group, relative, reason.strip()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="create a compressed inventory archive")
    backup.add_argument("--inventory", required=True, help="explicit source inventory JSON")
    backup.add_argument("--output", required=True, help="new archive path; existing files are refused")
    backup.add_argument("--root", action="append", default=[], type=lambda value: _parse_assignment(
                            value, label="--root"), metavar="GROUP=PATH",
                        help="override an inventory source root (repeat for Docker mounts)")
    backup.add_argument("--live-db", action="append", default=[],
                        type=_parse_live_db_assignment,
                        metavar="GROUP:RELATIVE.sqlite=BACKUP_COPY",
                        help="substitute a separately-created consistent SQLite backup copy")
    backup.add_argument("--exclude", action="append", default=[],
                        type=_parse_exclude_assignment,
                        metavar="GROUP:RELATIVE=REASON",
                        help="omit an explicit short-lived file and record the reason")
    backup.add_argument("--live-db-report",
                        help="JSON provenance report for every --live-db copy")
    verify = commands.add_parser("verify", help="stream-check every archived file hash")
    verify.add_argument("--archive", required=True)
    restore = commands.add_parser("restore", help="restore to a new target directory")
    restore.add_argument("--archive", required=True)
    restore.add_argument("--target", required=True)
    restore.add_argument("--group", help="restore one inventory group only")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            root_overrides = dict(args.root)
            if len(root_overrides) != len(args.root):
                raise BackupError("duplicate --root group override")
            live_db: dict[tuple[str, str], str] = {}
            for group, relative, path in args.live_db:
                key = _parse_live_db_key((group, relative))
                if key in live_db:
                    raise BackupError("duplicate --live-db target")
                live_db[key] = path
            excludes: dict[tuple[str, str], str] = {}
            for group, relative, reason in args.exclude:
                key = _parse_exclude_key((group, relative))
                if key in excludes:
                    raise BackupError("duplicate --exclude path")
                excludes[key] = reason
            live_db_report = None
            if args.live_db_report:
                report_file = _require_regular_file(args.live_db_report, "live database report")
                try:
                    live_db_report = json.loads(report_file.read_bytes())
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise BackupError(f"live database report is unreadable ({type(exc).__name__})") from exc
            result = create_backup(args.inventory, args.output,
                                   roots=root_overrides, live_db=live_db,
                                   excludes=excludes, live_db_report=live_db_report)
        elif args.command == "verify":
            result = verify_backup(args.archive)
        else:
            result = restore_backup(args.archive, args.target, group=args.group)
    except (BackupError, OSError, zipfile.BadZipFile, zlib.error, EOFError) as exc:
        print(f"runtime-backup: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
