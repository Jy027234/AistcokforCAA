"""生产快照发布 CLI。

它是 ``tools/daily_run.py`` 的快照步骤入口。生产路径只调用
``aquant.operations.universe_snapshot``，不会 import 或执行测试模块。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.operations.universe_snapshot import (  # noqa: E402
    UniverseSnapshotError,
    publish_universe_snapshot,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="发布一个不可变全市场快照")
    ap.add_argument("--data-dir", default=None,
                    help="统一数据根目录（含 meta.sqlite/api/；默认 deploy/universe-snapshot）")
    ap.add_argument("--cache", default=None,
                    help="collector 输出 universe-bars.json")
    ap.add_argument("--pool", default=str(ROOT / "configs" / "real-pool-csrc.yaml"))
    ap.add_argument("--snapshot-id", default=None,
                    help="显式物理 ID（默认按实际末日生成唯一 ID；禁止覆盖已有 ID）")
    ap.add_argument("--window-start", default=None)
    ap.add_argument("--window-end", default=None,
                    help="窗口末日；历史回放必须传入，禁止带入缓存中的未来行")
    ap.add_argument("--window", type=int, default=None)
    ap.add_argument("--financials", default=None)
    ap.add_argument("--actions", default=None)
    ap.add_argument("--allow-degraded", action="store_true",
                    help="允许质量检查未通过的快照发布（仍记录检查结果）")
    ap.add_argument("--defer-promotion", action="store_true",
                    help="只发布物理快照，暂不切换 current 指针")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    data_dir = Path(args.data_dir or ROOT / "deploy" / "universe-snapshot")
    cache = Path(args.cache or ROOT / "deploy" / "agentctl-q0" / "universe-bars.json")
    try:
        result = publish_universe_snapshot(
            data_root=data_dir,
            cache_path=cache,
            pool_path=Path(args.pool),
            snapshot_id=args.snapshot_id,
            window_start=args.window_start,
            window_end=args.window_end,
            window=args.window,
            financials_path=args.financials,
            actions_path=args.actions,
            strict_quality=not args.allow_degraded,
            promote=not args.defer_promotion,
        )
    except UniverseSnapshotError as exc:
        print(f"快照发布失败：{exc}")
        return 1
    payload = result.as_dict()
    payload["published_at"] = datetime.now(timezone.utc).isoformat()
    state = "已发布并设为 current" if result.promoted else "已发布，等待提升为 current"
    print(f"快照{state}：{result.snapshot_id}")
    print(f"  窗口 {result.first_day} .. {result.last_day}（{result.trading_days} 天）")
    print(f"  证券 {result.instruments} 只，行情 {result.quotes} 条")
    if result.promoted:
        print(f"  当前指针：{result.pointer_path}")
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
