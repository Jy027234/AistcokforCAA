"""Write read-only, snapshot-scoped financial demand and evidence coverage."""
from __future__ import annotations
import argparse
from datetime import date
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from aquant.operations.financial_coverage import build_financial_coverage_report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", type=Path, default=ROOT / "deploy/universe-snapshot")
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--live-facts-db", type=Path, default=ROOT / "data/s2-pdf-review/s2-machine-verified-facts.sqlite")
    parser.add_argument("--reconstructed-facts-db", type=Path, default=ROOT / "data/s2-structured/recon-v7-20261009.sqlite")
    parser.add_argument("--priority-file", type=Path)
    parser.add_argument("--latest-period", type=date.fromisoformat, default=date(2026, 6, 30))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    priorities = json.loads(args.priority_file.read_text(encoding="utf-8")).get("priorityInstrumentIds", []) if args.priority_file else []
    report = build_financial_coverage_report(snapshot_dir=args.snapshot_dir,
        snapshot_id=args.snapshot_id, live_facts_db=args.live_facts_db,
        reconstructed_facts_db=args.reconstructed_facts_db,
        priority_instrument_ids=priorities, latest_period_end=args.latest_period)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"output": str(args.output), "s1Optional": report["s1Optional"],
                      "formalS2": report["formalS2"], "historicalReconstructed": report["historicalReconstructed"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
