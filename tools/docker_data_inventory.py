"""Back up SQLite consistently and compare preserved deployment facts."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
FACT_TABLES = (
    "portfolio", "simulation_plan", "plan_confirmation", "plan_confirmation_preview",
    "plan_fee_binding", "plan_snapshot_binding", "order", "fill", "fee_charge",
    "position_lot", "lot_consumption", "cash_entry", "receivable", "valuation",
    "valuation_position", "valuation_provenance", "decision_log", "research_simulation_draft",
    "experiment", "engineering_simulation_run", "engineering_plan_provenance", "snapshot",
    "snapshot_dataset", "snapshot_pointer", "instrument", "instrument_status_version",
    "trading_calendar", "run_schedule", "pipeline_run_request", "pipeline_last_run",
)


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory(con: sqlite3.Connection, data: Path) -> dict:
    tables = {}
    existing = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for name in FACT_TABLES:
        if name not in existing:
            continue
        rows = [json.dumps(list(row), ensure_ascii=False, separators=(",", ":"),
                           default=lambda value: {"bytes": value.hex()})
                for row in con.execute(f'SELECT * FROM "{name}"')]
        encoded = "\n".join(sorted(rows)).encode("utf-8")
        tables[name] = {"count": len(rows), "sha256": hashlib.sha256(encoded).hexdigest()}
    # Content addressed snapshots and frozen input originals must stay byte-identical.
    files = {}
    for folder in ("api", "simulation_input_origins", "financial_input_origins",
                   "research_simulation_origins", "comparisons", "engineering-simulation",
                   "forward-comparison", "simulation-attribution"):
        for path in sorted((data / folder).rglob("*")):
            if path.is_file() and not path.name.endswith(("-wal", "-shm")):
                files[path.relative_to(data).as_posix()] = checksum(path)
    pointer = data / "current_snapshot.json"
    if pointer.is_file():
        files[pointer.name] = checksum(pointer)
    return {"tables": tables, "files": files}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "deploy/universe-snapshot")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    data = args.data_dir.resolve(strict=True)
    target = args.output.resolve()
    container_data = ROOT == Path("/app") and data == Path("/data")
    if (not data.is_relative_to(ROOT) and not container_data) or not target.is_relative_to(ROOT):
        raise ValueError("Deployment data and evidence must stay inside the project")
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect((data / "meta.sqlite").as_uri() + "?mode=ro", uri=True, timeout=10) as live:
        if args.backup:
            backup = args.backup.resolve()
            if not backup.is_relative_to(ROOT) or backup.exists():
                raise ValueError("Backup must be a new file inside the project")
            backup.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(backup) as destination:
                live.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Backup failed SQLite integrity check")
            print(f"Consistent SQLite backup: {backup}")
        # Only use after quiescing the old processes or through docker exec on Linux.
        result = inventory(live, data)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.compare:
        prior = json.loads(args.compare.read_text(encoding="utf-8"))
        if result != prior:
            changed = [name for name in prior["tables"] if prior["tables"][name] != result["tables"].get(name)]
            changed_files = [name for name, digest in prior["files"].items() if result["files"].get(name) != digest]
            raise ValueError(f"Preserved facts changed: tables={changed}, files={changed_files[:10]}")
        print(f"PASS preserved {len(result['tables'])} tables and {len(result['files'])} files")
    else:
        print(f"Recorded {len(result['tables'])} tables and {len(result['files'])} files")


if __name__ == "__main__":
    main()
