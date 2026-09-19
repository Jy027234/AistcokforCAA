"""把已经通过后续闸门的物理快照提升为 current。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.operations.universe_snapshot import (  # noqa: E402
    UniverseSnapshotError,
    promote_universe_snapshot,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="切换当前快照指针")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--require-factors", action="store_true")
    args = parser.parse_args()
    try:
        promote_universe_snapshot(
            data_root=Path(args.data_dir), snapshot_id=args.snapshot_id,
            require_factors=args.require_factors,
        )
    except UniverseSnapshotError as exc:
        print(f"快照提升失败：{exc}")
        return 1
    print(f"current 已切换：{args.snapshot_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
