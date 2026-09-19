"""Verify that the local agentctl checkout matches A-Quant Lab's lock.

This command is deliberately read-only. A mismatch is not upgraded or hidden:
it exits 1 and reports the changed-file summary so Q5 evidence cannot silently
claim compatibility with a different base revision.

Exit codes: 0 = exact match, 1 = drift, 2 = lock/repository unavailable.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = ROOT / "src" / "aquant" / "adapters" / "agentctl" / "LOCKED_BASE"


def _git(source_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(source_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip()[:500])
    return completed.stdout.strip()


def inspect_base(lock_path: Path, source_root: Path) -> dict[str, Any]:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    locked = str(lock.get("locked_commit") or "").strip()
    if len(locked) != 40:
        raise ValueError("LOCKED_BASE does not contain a full commit SHA")
    current = _git(source_root, "rev-parse", "HEAD")
    _git(source_root, "cat-file", "-e", f"{locked}^{{commit}}")
    matched = current == locked
    changed_files: list[str] = []
    shortstat = ""
    if not matched:
        shortstat = _git(
            source_root,
            "diff",
            "--shortstat",
            f"{locked}..{current}",
            "--",
            "src/agentctl",
        )
        names = _git(
            source_root,
            "diff",
            "--name-only",
            f"{locked}..{current}",
            "--",
            "src/agentctl",
        )
        changed_files = [item for item in names.splitlines() if item]
    return {
        "schema_version": "aquant.agentctl_base_status.v1",
        "matched": matched,
        "locked_commit": locked,
        "current_commit": current,
        "changed_file_count": len(changed_files),
        "diff_summary": shortstat,
        "changed_files": changed_files,
        "upgrade_required": not matched,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(
            os.environ.get("AGENTCTL_SOURCE_ROOT", str(ROOT.parent / "Agent"))
        ),
    )
    parser.add_argument("--json-out", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = inspect_base(args.lock.resolve(), args.source_root.resolve())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"agentctl base check unavailable: {type(exc).__name__}: {exc}")
        return 2
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.json_out is not None:
        out = args.json_out if args.json_out.is_absolute() else ROOT / args.json_out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["matched"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
