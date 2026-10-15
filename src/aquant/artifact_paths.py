"""Locate immutable artifacts after an explicitly configured host relocation.

AQUANT_ARTIFACT_PATH_MAP is a JSON object mapping absolute source roots to
absolute roots on this runtime. Only file access uses the mapping: archived
documents, database fields, provenance, and content hashes remain unchanged.
"""
from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath


def _source_path(value: str):
    windows = PureWindowsPath(value)
    return windows if windows.is_absolute() else PurePosixPath(value)


@lru_cache(maxsize=4)
def _mappings(raw: str):
    if not raw:
        return ()
    try:
        pairs = json.loads(raw, object_pairs_hook=lambda items: items)
    except (TypeError, ValueError) as exc:
        raise ValueError("AQUANT_ARTIFACT_PATH_MAP must be a JSON object") from exc
    # object_pairs_hook lets duplicate source roots fail instead of silently
    # selecting whichever destination happened to appear last.
    if not raw.lstrip().startswith("{") or not isinstance(pairs, list):
        raise ValueError("AQUANT_ARTIFACT_PATH_MAP must be a JSON object")
    mappings = []
    seen = set()
    for source, destination in pairs:
        if not isinstance(source, str) or not isinstance(destination, str):
            raise ValueError("artifact path map roots must be strings")
        source_root = _source_path(source)
        target_root = Path(destination)
        if (not source_root.is_absolute() or ".." in source_root.parts
                or not target_root.is_absolute() or ".." in target_root.parts):
            raise ValueError("artifact path map requires absolute roots without traversal")
        if source_root in seen:
            raise ValueError("artifact path map repeats a source root")
        seen.add(source_root)
        mappings.append((source_root, target_root))
    return tuple(sorted(mappings, key=lambda entry: len(entry[0].parts), reverse=True))


def resolve_artifact_path(value: str | os.PathLike[str]) -> Path:
    """Resolve only a configured source root, rejecting mapped-root escapes.

    Without configuration this returns the ordinary native Path unchanged.
    Native runtime paths (for example /data) remain usable alongside mapped
    Windows archive paths. Unmapped foreign Windows absolute paths fail closed.
    """
    raw = os.environ.get("AQUANT_ARTIFACT_PATH_MAP", "").strip()
    mappings = _mappings(raw)
    original = os.fspath(value)
    if not mappings:
        return Path(original)
    source = _source_path(original)
    for source_root, target_root in mappings:
        if type(source) is not type(source_root):
            continue
        try:
            suffix = source.relative_to(source_root)
        except ValueError:
            continue
        if ".." in suffix.parts:
            raise ValueError("artifact path escapes its configured source root")
        root = target_root.resolve()
        target = root.joinpath(*suffix.parts).resolve()
        if not target.is_relative_to(root):
            raise ValueError("artifact path escapes its configured destination root")
        return target
    if isinstance(source, PureWindowsPath) and os.name != "nt":
        raise ValueError("Windows artifact path has no configured source root")
    return Path(original)


def resolve_archive_member(root: str | os.PathLike[str], stored_path: str) -> Path:
    """Resolve a portable archive member and retain its original root boundary."""
    archive_root = resolve_artifact_path(root).resolve()
    windows = PureWindowsPath(stored_path)
    if windows.is_absolute() or PurePosixPath(stored_path).is_absolute():
        target = resolve_artifact_path(stored_path).resolve()
    else:
        if windows.drive or windows.root:
            raise ValueError("archive member must not use a drive-relative path")
        target = archive_root.joinpath(*windows.parts).resolve()
    if not target.is_relative_to(archive_root):
        raise ValueError("archive member path is outside its archive")
    return target
