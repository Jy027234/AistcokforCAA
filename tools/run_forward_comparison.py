"""Register a real future M/E/B spec or advance its actual ledger observations.

No network access. H stays unavailable and existing user portfolios are untouched.
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.data.reader import SnapshotReader
from aquant.domain.data.snapshot import SnapshotStore
from aquant.domain.portfolio.construction import ConstructionParams
from aquant.domain.portfolio.plan import PlanService
from aquant.domain.simulation.board_rules import BOARD_RULES
from aquant.domain.simulation.verified_fees import fee_table_from_env
from aquant.operations.forward_comparison import ForwardComparisonStore, prepare_forward_comparison, track_active_forward
from aquant.operations.snapshot_lifecycle import resolve_current_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--advance", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "deploy/universe-snapshot")
    parser.add_argument("--research", type=Path, default=ROOT / "data/s2-structured/research-candidates.json")
    parser.add_argument("--snapshot-id")
    parser.add_argument("--authorization-subject", help="Existing task authorization subject, required for prepare")
    parser.add_argument("--experiment-version", default="S1-E1-MEB-forward-v1")
    parser.add_argument("--output", "--json-out", dest="output", type=Path)
    args = parser.parse_args()
    con = connect(args.data_dir / "meta.sqlite")
    try:
        apply_migrations(con)
        sid = args.snapshot_id or resolve_current_snapshot(args.data_dir, con)
        reader = SnapshotReader(SnapshotStore(con, args.data_dir / "api"))
        ref = reader.ref(sid)
        listings = {row["instrument_id"]: (row["exchange"], row["board"])
                    for row in reader.instruments(sid, as_of=ref.as_of_time)}
        fees, _note = fee_table_from_env()
        service = PlanService(con, reader, fees, BOARD_RULES, listings,
            ConstructionParams(initial_target_pct=Decimal("4"), liquidity_min_avg_amount_cents=5_000_000_000))
        store = ForwardComparisonStore(args.data_dir / "forward-comparison")
        if args.prepare:
            if not args.authorization_subject:
                parser.error("--prepare requires --authorization-subject")
            result = prepare_forward_comparison(service, store=store, research_path=args.research,
                snapshot_id=sid, authorization_subject=args.authorization_subject,
                experiment_version=args.experiment_version)
        else:
            result = track_active_forward(service, store=store, snapshot_id=sid)
        output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output, encoding="utf-8")
        print(output)
        return 1 if any(item["status"] == "DATA_NOT_READY" for item in result.get("results", [])) else 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
