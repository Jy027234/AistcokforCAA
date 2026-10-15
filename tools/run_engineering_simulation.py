"""Execute the saved engineering draft using immutable actual market evidence; no network."""
from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.data.reader import SnapshotReader
from aquant.domain.data.snapshot import SnapshotStore
from aquant.domain.portfolio.construction import ConstructionParams
from aquant.domain.portfolio.plan import PlanService
from aquant.domain.simulation.board_rules import BOARD_RULES
from aquant.domain.simulation.verified_fees import fee_table_from_env
from aquant.operations.engineering_simulation import run_engineering_simulation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("deploy/universe-snapshot"))
    parser.add_argument("--draft-id", required=True)
    parser.add_argument("--subject", required=True, help="Original saved-draft owner; never treated as a human confirmation")
    parser.add_argument("--execution-snapshot-id")
    parser.add_argument("--results-dir", type=Path)
    args = parser.parse_args()
    data_dir = args.data_dir.resolve()
    con = connect(data_dir / "meta.sqlite")
    try:
        apply_migrations(con)
        reader = SnapshotReader(SnapshotStore(con, data_dir / "api"))
        fees, _ = fee_table_from_env()
        draft = con.execute("SELECT decision_snapshot_id FROM research_simulation_draft WHERE draft_id=? AND subject_id=?",
            (args.draft_id, args.subject)).fetchone()
        if draft is None:
            result = {"status": "DATA_NOT_READY", "error": {"code": "DATA_NOT_READY", "message": "saved draft is unavailable in this owner scope"}}
        else:
            ref = reader.ref(draft[0])
            listings = {i["instrument_id"]: (i["exchange"], i["board"])
                for i in reader.instruments(draft[0], as_of=ref.as_of_time)}
            params = ConstructionParams(initial_target_pct=Decimal("4"),
                liquidity_min_avg_amount_cents=5_000_000_000)
            result = run_engineering_simulation(PlanService(con, reader, fees, BOARD_RULES, listings, params),
                data_dir=data_dir, draft_id=args.draft_id, subject=args.subject,
                execution_snapshot_id=args.execution_snapshot_id,
                results_dir=args.results_dir.resolve() if args.results_dir else None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "COMPLETED" else 2
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
