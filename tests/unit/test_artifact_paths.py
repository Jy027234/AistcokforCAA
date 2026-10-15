"""Explicit archive relocation is bounded and never rewrites source values."""
import json
import os
from pathlib import Path

import pytest

from aquant.artifact_paths import resolve_archive_member, resolve_artifact_path


SOURCE = r"Z:\frozen-project"


def mapping(monkeypatch, tmp_path):
    monkeypatch.setenv("AQUANT_ARTIFACT_PATH_MAP", json.dumps({SOURCE: str(tmp_path)}))


def test_no_mapping_keeps_native_path_behavior(monkeypatch):
    monkeypatch.delenv("AQUANT_ARTIFACT_PATH_MAP", raising=False)
    for value in ("relative/file.json", SOURCE + r"\data\facts.sqlite"):
        assert resolve_artifact_path(value) == Path(value)


def test_maps_only_complete_configured_root_and_preserves_native_runtime_paths(tmp_path, monkeypatch):
    mapping(monkeypatch, tmp_path)
    assert resolve_artifact_path(SOURCE + r"\data\facts.sqlite") == tmp_path / "data/facts.sqlite"
    assert resolve_artifact_path("z:/frozen-project/data/facts.sqlite") == tmp_path / "data/facts.sqlite"
    assert resolve_artifact_path(tmp_path / "local.json") == tmp_path / "local.json"
    sibling = SOURCE + r"-other\secret.json"
    if os.name == "nt":
        assert resolve_artifact_path(sibling) == Path(sibling)
    else:
        with pytest.raises(ValueError, match="no configured source root"):
            resolve_artifact_path(sibling)


def test_most_specific_configured_root_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("AQUANT_ARTIFACT_PATH_MAP", json.dumps({
        SOURCE: str(tmp_path / "general"), SOURCE + r"\data": str(tmp_path / "specific")}))
    assert resolve_artifact_path(SOURCE + r"\data\facts.sqlite") == tmp_path / "specific/facts.sqlite"


@pytest.mark.parametrize("suffix", [r"\..\outside.json", r"\data\..\inside.json", "/../outside"])
def test_mapped_paths_reject_source_traversal(tmp_path, monkeypatch, suffix):
    mapping(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        resolve_artifact_path(SOURCE + suffix)


@pytest.mark.parametrize("raw", ["not json", "[]", "null", '{"relative":"/app"}',
    '{"/root":"relative"}', '{"/root":null}', '{"/root":{}}', '{"/root":"/app/../other"}',
    '{"/root":"/app","/root":"/other"}'])
def test_invalid_mapping_fails_closed(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("AQUANT_ARTIFACT_PATH_MAP", raw)
    with pytest.raises(ValueError):
        resolve_artifact_path(tmp_path / "any.json")


def test_archive_members_handle_windows_separators_and_retain_boundary(tmp_path, monkeypatch):
    mapping(monkeypatch, tmp_path)
    root = SOURCE + r"\archive"
    expected = tmp_path / "archive/raw/ab/hash"
    assert resolve_archive_member(root, r"raw\ab\hash") == expected
    assert resolve_archive_member(root, SOURCE + r"\archive\raw\ab\hash") == expected
    for member in (r"..\outside", SOURCE + r"\other\outside", r"Z:drive-relative", r"\rooted"):
        with pytest.raises(ValueError):
            resolve_archive_member(root, member)


def test_symlink_cannot_escape_mapped_or_archive_root(tmp_path, monkeypatch):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("this host cannot create symlinks")
    mapping(monkeypatch, root)
    with pytest.raises(ValueError, match="destination root"):
        resolve_artifact_path(SOURCE + r"\link\file")
    with pytest.raises(ValueError, match="outside its archive"):
        resolve_archive_member(root, "link/file")
