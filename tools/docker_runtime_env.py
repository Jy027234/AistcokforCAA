"""Translate saved host settings into a private Docker runtime env file.

The source settings and archived records are never edited. The explicit artifact
map relocates reads while existing content hashes remain authoritative.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH_KEYS = (
    "LIVE_FINANCIAL_FACTS_DB", "SIMULATION_FINANCIAL_CACHE",
    "SIMULATION_FINANCIAL_EVIDENCE", "S2_TARGET_PERIOD_MAP",
)
SETTING_KEYS = (
    "AQUANT_IDENTITY_MODE", "AQUANT_TRIAL_SUBJECT", "AQUANT_COMMISSION_RATE",
    "AQUANT_COMMISSION_MIN_CENTS", "AQUANT_COMMISSION_SOURCE",
    "AQUANT_TRUSTED_PROXY_NETWORKS", "AQUANT_HOST_DATA_DIR", "AQUANT_HOST_PIPELINE_DIR",
    "AQUANT_SCHEDULER_IMAGE",
)


def read_settings(source: Path) -> dict[str, str]:
    settings = {}
    for raw in source.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            settings[key.strip()] = value.strip()
    return settings


def runtime_settings(settings: dict[str, str], root: Path = ROOT) -> dict[str, str]:
    root = root.resolve()
    result = {key: settings[key] for key in SETTING_KEYS if key in settings}
    for key in ("AQUANT_HOST_DATA_DIR", "AQUANT_HOST_PIPELINE_DIR"):
        path = Path(result[key]).resolve(strict=True)
        if not path.is_dir() or not path.is_relative_to(root):
            raise ValueError(f"{key} must be an existing directory inside this project")
        result[key] = path.as_posix()
    for name, folder in (("ARTIFACT", "data"),):
        path = (root / folder).resolve(strict=True)
        if not path.is_dir() or not path.is_relative_to(root):
            raise ValueError(f"{name} data directory is outside this project")
        result[f"AQUANT_HOST_{name}_DIR"] = path.as_posix()
    for name in PATH_KEYS:
        path = Path(settings[f"AQUANT_{name}"]).resolve(strict=True)
        if not path.is_file() or not path.is_relative_to(root):
            raise ValueError(f"AQUANT_{name} must be an existing file inside this project")
        result[f"AQUANT_CONTAINER_{name}"] = "/app/" + path.relative_to(root).as_posix()
    result["AQUANT_ARTIFACT_PATH_MAP"] = json.dumps({
        str(Path(result["AQUANT_HOST_DATA_DIR"])): "/data", str(root): "/app",
    }, ensure_ascii=False)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "real-trial.env")
    parser.add_argument("--output", type=Path, default=ROOT / "deploy/local-trial/docker-runtime.env")
    args = parser.parse_args()
    values = runtime_settings(read_settings(args.source))
    target = args.output.resolve()
    if not target.is_relative_to(ROOT):
        raise ValueError("Runtime settings must stay in this project")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Single-quote dotenv values so Windows backslashes, $, and spaces survive.
    if any("'" in value or "\n" in value or "\r" in value for value in values.values()):
        raise ValueError("Saved settings contain unsupported quote or newline characters")
    target.write_text("\n".join(f"{key}='{value}'" for key, value in values.items()) + "\n", encoding="utf-8")
    print(f"Docker runtime settings saved: {target}")
    print("Original settings and archived records unchanged.")


if __name__ == "__main__":
    main()
