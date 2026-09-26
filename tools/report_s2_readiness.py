"""Print read-only S2 input coverage for one explicit published snapshot."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.data.snapshot import SnapshotError  # noqa: E402
from aquant.domain.data.pit import PitViolation  # noqa: E402
from aquant.operations.s2_readiness import (  # noqa: E402
    S2ReadinessError,
    build_s2_readiness_report,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", required=True,
                        help="已发布快照根目录，须含现有 meta.sqlite 与 api/")
    parser.add_argument("--snapshot-id", required=True, help="已发布物理快照 ID")
    parser.add_argument("--facts-db", required=True,
                        help="现有 FinancialFactRepository SQLite 文件；缺失时拒绝运行")
    args = parser.parse_args(argv)
    try:
        report = build_s2_readiness_report(
            snapshot_dir=args.snapshot_dir, snapshot_id=args.snapshot_id,
            facts_db=args.facts_db,
        )
    except (S2ReadinessError, SnapshotError, PitViolation, FileNotFoundError,
            ValueError, TypeError, sqlite3.Error, OSError) as exc:
        print(f"S2 readiness failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
