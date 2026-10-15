from pathlib import Path
import json

import pytest

from tools.docker_runtime_env import runtime_settings


def saved_settings(root: Path):
    settings = {"AQUANT_HOST_DATA_DIR": str(root / "deploy/universe-snapshot"),
                "AQUANT_HOST_PIPELINE_DIR": str(root / "deploy/agentctl-q0"),
                "AQUANT_COMMISSION_SOURCE": "original saved fee assumption"}
    for value in settings.values():
        if value.startswith(str(root)):
            Path(value).mkdir(parents=True)
    for folder in ("data/s2-structured", "data/s2-pdf-review"):
        (root / folder).mkdir(parents=True)
    for key, folder in (("LIVE_FINANCIAL_FACTS_DB", "data/s2-pdf-review"),
                        ("SIMULATION_FINANCIAL_CACHE", "data/s2-structured"),
                        ("SIMULATION_FINANCIAL_EVIDENCE", "data/s2-structured"),
                        ("S2_TARGET_PERIOD_MAP", "data/s2-structured")):
        path = root / folder / key
        path.write_text("original bytes", encoding="utf-8")
        settings["AQUANT_" + key] = str(path)
    return settings


def test_runtime_env_maps_only_existing_project_inputs_and_preserves_settings(tmp_path):
    settings = saved_settings(tmp_path)
    before = dict(settings)
    result = runtime_settings(settings, tmp_path)
    assert settings == before
    assert result["AQUANT_COMMISSION_SOURCE"] == before["AQUANT_COMMISSION_SOURCE"]
    assert json.loads(result["AQUANT_ARTIFACT_PATH_MAP"]) == {
        str(tmp_path / "deploy/universe-snapshot"): "/data", str(tmp_path.resolve()): "/app",
    }
    assert result["AQUANT_CONTAINER_LIVE_FINANCIAL_FACTS_DB"] == "/app/data/s2-pdf-review/LIVE_FINANCIAL_FACTS_DB"
    assert result["AQUANT_HOST_DATA_DIR"] == (tmp_path / "deploy/universe-snapshot").as_posix()
    assert Path(settings["AQUANT_SIMULATION_FINANCIAL_CACHE"]).read_text() == "original bytes"


def test_runtime_env_rejects_inputs_outside_the_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    settings = saved_settings(project)
    external = tmp_path / "unrelated.sqlite"
    external.write_text("unrelated data")
    settings["AQUANT_LIVE_FINANCIAL_FACTS_DB"] = str(external)
    with pytest.raises(ValueError, match="inside this project"):
        runtime_settings(settings, project)
