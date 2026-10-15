"""Print read-only S2 input coverage for one explicit published snapshot."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from aquant.domain.data.snapshot import SnapshotError  # noqa: E402
from aquant.domain.data.pit import PitViolation  # noqa: E402
from aquant.operations.s2_readiness import (  # noqa: E402
    S2ReadinessError,
    build_s2_readiness_report,
    load_s2_target_period_map,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", required=True,
                        help="已发布快照根目录，须含现有 meta.sqlite 与 api/")
    parser.add_argument("--snapshot-id", required=True, help="已发布物理快照 ID")
    parser.add_argument("--facts-db", required=True,
                        help="现有 FinancialFactRepository SQLite 文件；缺失时拒绝运行")
    parser.add_argument("--allow-reconstructed", action="store_true",
                        help="额外接受 available_basis=RECONSTRUCTED 的历史重建事实；"
                             "前向事实仍受 first_seen_at <= cutoff 约束")
    parser.add_argument("--latest-period-end", type=date.fromisoformat,
                        help="固定需求报告期；缺字段保持在原证券/指标分母中")
    parser.add_argument("--target-period-map", type=Path,
                        help="逐证券冻结目标/发现状态及哈希绑定来源；共享源按当前快照精确投影")
    parser.add_argument("--market-caps", type=Path, help="显式同日收据支持市值sidecar；不默认猜路径")
    parser.add_argument("--market-caps-archive-root", type=Path,
                        help="核验冻结市值或显式sidecar的原始响应和收据")
    parser.add_argument("--include-signals", action="store_true",
                        help="输出只来自完整已接纳证券同池的正式S2诊断排名")
    parser.add_argument("--json-out", type=Path, help="保存只读报告；不修改快照、事实或策略")
    args = parser.parse_args(argv)
    try:
        target_map = load_s2_target_period_map(args.target_period_map) if args.target_period_map else {}
        report = build_s2_readiness_report(
            snapshot_dir=args.snapshot_dir, snapshot_id=args.snapshot_id,
            facts_db=args.facts_db, allow_reconstructed=args.allow_reconstructed,
            latest_period_end=args.latest_period_end, market_caps_path=args.market_caps,
            market_caps_archive_root=args.market_caps_archive_root,
            include_signals=args.include_signals,
            latest_period_ends=target_map.get("latest_period_ends"),
            target_period_basis=target_map.get("target_period_basis"),
            target_period_records=target_map.get("records", []),
        )
        if target_map:
            report["target_period_map"] = target_map
    except (S2ReadinessError, SnapshotError, PitViolation, FileNotFoundError,
            ValueError, TypeError, sqlite3.Error, OSError) as exc:
        print(f"S2 readiness failed: {exc}", file=sys.stderr)
        return 2
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.json_out is not None:
        from tools.run_s2_daily import _write
        _write(report, args.json_out)
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
