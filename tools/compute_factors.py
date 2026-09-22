"""在**已发布快照**上计算 F10 并落库（§10.2、§13）。

为什么需要这个工具
-----------------
在这之前，"算因子"这一步实际上跑的是 `tests.integration.t12_f10_real`：
它读采集缓存里的财务与行情，**在内存里算一遍**，只写一份验收报告 JSON。
于是出现了一个谁都没注意到的事实：

    验收报告写着 "computed: 832/900, conclusion: PASS"，
    而快照库里的 research_run 与 feature_value **都是 0 行**。

因子从来没有进过产品。研究卡接口读的是 `research_run` / `feature_value`
（见 `src/aquant/adapters/agentctl/snapshot_card_reader.py:145`），
所以卡片上永远是"该快照上尚未计算任何因子"。流水线每天"成功"，
产品每天没有数值——两者都自洽，只是互不相干。

本工具走的是**产品自己的落库路径**（`compute_f10_for_snapshot`），
与 `POST /api/v1/research/jobs` 的因子作业是同一条代码路径：

    快照（含 financials 数据集） -> PIT 闸门 -> research_run -> feature_value

因此它产出的东西，研究卡读得到。

用法：
    python tools/compute_factors.py --snapshot-dir deploy/universe-snapshot \
        --snapshot-id snap-universe

退出码：0 已落库；2 环境缺失（目录/快照/财务数据集）；1 落库为 0 行。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.data.db import apply_migrations, connect  # noqa: E402
from aquant.domain.data.reader import SnapshotReader  # noqa: E402
from aquant.domain.data.snapshot import SnapshotError, SnapshotStore  # noqa: E402
from aquant.domain.research.f10 import compute_f10_for_snapshot  # noqa: E402
from aquant.operations.snapshot_lifecycle import resolve_current_snapshot  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot-dir", default=str(ROOT / "deploy" / "universe-snapshot"),
                    help="已发布快照的数据目录（含 meta.sqlite 与 api/）")
    ap.add_argument("--snapshot-id", default=None,
                    help="物理快照 ID；省略时解析 current_snapshot.json")
    ap.add_argument("--limit", type=int, default=0, help="只算前 N 只（调试用）")
    ap.add_argument("--json-out", default=None,
                    help="把摘要写成 JSON（供流水线留痕）")
    args = ap.parse_args()

    data_dir = Path(args.snapshot_dir)
    if not (data_dir / "meta.sqlite").is_file():
        print(f"缺少快照库：{data_dir / 'meta.sqlite'}")
        print("先跑 python -m tests.integration.t10_universe_snapshot")
        return 2

    con = connect(data_dir / "meta.sqlite")
    # 已发布快照的库里也需要表结构；apply_migrations 幂等（IF NOT EXISTS），
    # 但**改不了已有表的列**——这一点记在 docs/implementation-baseline.md §8.3。
    apply_migrations(con)
    store = SnapshotStore(con, data_dir / "api")
    reader = SnapshotReader(store)
    snapshot_id = resolve_current_snapshot(
        data_dir, explicit_snapshot_id=args.snapshot_id, connection=con)

    try:
        ref = reader.ref(snapshot_id)
    except SnapshotError as exc:
        print(f"快照 {snapshot_id!r} 不可读：{exc.message}")
        print("修复：" + exc.repair_action)
        con.close()
        return 2

    try:
        financials = reader.financials(snapshot_id, as_of=ref.as_of_time)
    except SnapshotError as exc:
        # 只吞"这份快照没有财务数据集"。数据集存在但哈希不对是另一回事，
        # 掩盖它会让"快照被改过"看起来像"没有财务数据"（同 reader 的口径）。
        if "has no dataset" not in str(exc.message):
            raise
        financials = {}
    if not financials.get("statements"):
        print("快照里没有 financials 数据集 —— 因子只能全部标为「快照未包含财务数据」。")
        print("快照必须带上财务数据，研究卡才可能有数值。见 t10_universe_snapshot.py。")
        con.close()
        return 2

    summary = compute_f10_for_snapshot(
        con=con, reader=reader, snapshot_id=snapshot_id,
        as_of=ref.as_of_time, limit=args.limit,
    )
    con.commit()

    print(f"快照 {summary['snapshotId']} @ {summary['asOfTime']}")
    print(f"  研究运行 {summary['researchRunId']}")
    print(f"  特征版本 {summary['featureVersion']}（{summary['validityStatus']}）")
    print(f"  输出哈希 {summary['outputHash']}")
    print(f"  财务记录 {summary['financialStatements']} 条"
          f"（跳过 {summary['skippedStatements']} 条），交易日 {summary['calendarDays']} 天")
    for key, value in sorted(summary.get("exclusionBreakdown", {}).items()):
        print(f"  未进排名：{value} 只 —— {key}")

    # 落库后**回读一遍**：写进去了不等于读得出来。
    # 研究卡走的是另一条读取路径，因此这里必须用读取器验证，而不是相信返回值。
    rows = con.execute(
        "SELECT COUNT(*) FROM feature_value WHERE research_run_id=?",
        (summary["research_run_id"],)).fetchone()[0]
    with_value = con.execute(
        "SELECT COUNT(*) FROM feature_value WHERE research_run_id=? AND raw_value IS NOT NULL",
        (summary["research_run_id"],)).fetchone()[0]
    print(f"  回读：feature_value {rows} 行，其中 {with_value} 行有值")

    out = {
        "ran_at": datetime.now().astimezone().isoformat(),
        "snapshot_id": summary["snapshotId"],
        "as_of_time": summary["asOfTime"],
        "research_run_id": summary["researchRunId"],
        "factor_id": summary["factorId"],
        "feature_version": summary["feature_version"],
        "validity_status": summary["validity_status"],
        "withdrawal_reason": summary["withdrawal_reason"],
        "output_hash": summary["output_hash"],
        "feature_value_rows": rows,
        "valued_rows": with_value,
        "exclusion_breakdown": summary.get("exclusionBreakdown", {}),
        "financial_statements": summary["financialStatements"],
        "skipped_statements": summary["skippedStatements"],
    }
    if args.json_out:
        Path(args.json_out).write_bytes(
            json.dumps(out, ensure_ascii=False, indent=2).encode("utf-8"))
        print(f"报告：{args.json_out}")

    con.close()
    if rows == 0:
        print("落库为 0 行：因子没有进库，研究卡不会有数值。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
