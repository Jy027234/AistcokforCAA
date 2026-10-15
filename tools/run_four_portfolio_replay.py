"""Run the frozen September real-data M/E/H/B engineering replay, no network.

The archive's reconstructed PIT watermark is preserved. This is an engineering
replay using real prices, not a formal backtest or the user's actual decisions.
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from aquant.domain.data.db import connect
from aquant.domain.data.reader import SnapshotReader
from aquant.domain.data.snapshot import SnapshotStore
from aquant.domain.experiments.comparison import ComparisonRunner, ComparisonRunSpec, ComparisonStore, ReplayStep
from aquant.domain.portfolio.construction import Candidate, ConstructionParams
from aquant.domain.portfolio.plan import PlanService
from aquant.domain.simulation.board_rules import BOARD_RULES
from aquant.domain.simulation.verified_fees import fee_table_from_env

DEFAULT_STEPS = (
    ReplayStep(date(2026, 9, 23), "snap-eod-2026-09-22-ee07b71a6aa24604", "snap-eod-2026-09-23-29e87f7d1f1441c5"),
    ReplayStep(date(2026, 9, 24), "snap-eod-2026-09-23-29e87f7d1f1441c5", "snap-eod-2026-09-24-875b96bba5bf4344"),
    ReplayStep(date(2026, 9, 29), "snap-eod-2026-09-28-9e419b38ed084700", "snap-eod-2026-09-29-ddd1b2b71cc54fa1"),
    ReplayStep(date(2026, 9, 30), "snap-eod-2026-09-29-ddd1b2b71cc54fa1", "snap-eod-2026-09-30-b423483dc6fe4cdc"),
)


def run_archive(*, data_dir: Path, results_dir: Path, experiment_version: str,
                authorization_subject: str, max_single_name_pct: Decimal = Decimal("4")) -> dict:
    con = connect(data_dir / "meta.sqlite")
    reader = SnapshotReader(SnapshotStore(con, data_dir / "api"))
    fees, fee_note = fee_table_from_env()
    params = ConstructionParams(max_single_name_pct=max_single_name_pct)
    common = None
    pool_counts = []
    for step in DEFAULT_STEPS:
        ref = reader.ref(step.decision_snapshot_id)
        instruments = {row["instrument_id"]: row for row in reader.instruments(step.decision_snapshot_id, as_of=ref.as_of_time)}
        history_counts: dict[str, int] = {}
        incomplete = set()
        for quote in reader.daily_quotes(step.decision_snapshot_id, as_of=ref.as_of_time):
            if quote.adjusted_close_cents is not None:
                history_counts[quote.instrument_id] = history_counts.get(quote.instrument_id, 0) + 1
            else:
                incomplete.add(quote.instrument_id)
        candidates = [Candidate(iid, row["industry_code"], 0) for iid, row in instruments.items()
                      if row.get("board") == "MAIN" and row.get("industry_code")
                      and history_counts.get(iid, 0) >= 61 and iid not in incomplete]
        listings = {iid: (row["exchange"], row["board"]) for iid, row in instruments.items()}
        svc = PlanService(con, reader, fees, BOARD_RULES, listings, params)
        liquid, liquidity_exclusions = svc._filter_by_liquidity(snapshot_id=step.decision_snapshot_id,
            trading_day=step.trading_day, as_of=ref.as_of_time, candidates=candidates)
        eligible, age_exclusions, notes = svc._filter_by_listing_age(snapshot_id=step.decision_snapshot_id,
            trading_day=step.trading_day, as_of=ref.as_of_time, candidates=liquid)
        ids = {candidate.instrument_id for candidate in eligible}
        common = ids if common is None else common & ids
        pool_counts.append({"decision_snapshot_id": step.decision_snapshot_id,
            "instrument_count": len(instruments), "s1_history_main_pool_count": len(candidates),
            "eligible_count": len(ids), "liquidity_exclusions": len(liquidity_exclusions),
            "listing_exclusions": len(age_exclusions), "notes": notes})
    pool = tuple(sorted(common or set()))
    # Fixed, explicit engineering allocation; never claim this is user stock selection.
    human = tuple(iid for iid in ("SH.600519", "SH.600900", "SH.601318", "SH.600036", "SH.601398", "SZ.000001") if iid in pool)
    if not human:
        raise ValueError("declared engineering H selection is absent from the common pool")
    spec = ComparisonRunSpec(experiment_version=experiment_version,
        hypothesis="检验相同真实归档信息、初始资金与成交费用下四组合账本和会计复盘；不检验盈利能力",
        steps=DEFAULT_STEPS, universe=pool, human_selection=human, human_origin="ENGINEERING_REPLAY",
        limitations=("REAL MARKET DATA, RECONSTRUCTED POINT-IN-TIME -- NOT VALID FOR FORMAL PIT BACKTESTS",
            "仅使用已归档的原900代码池共同合格子集；不是10月9日最新研究候选的历史结果",
            "H为明确六证券工程回放配置，未声称这些选择来自用户",
            "公司行为档案仅含已核验记录，覆盖有限；未核验全证券池所有分红与复杂行为",
            "无核验重大监管调查事件样本，本轮不检验E1效用",
            "首次记录前已存在这段价格资料，本次登记是工程重放，不是事前经济假设检验"))
    first_ref = reader.ref(DEFAULT_STEPS[0].decision_snapshot_id)
    listings = {row["instrument_id"]: (row["exchange"], row["board"])
                for row in reader.instruments(DEFAULT_STEPS[0].decision_snapshot_id, as_of=first_ref.as_of_time)}
    runner = ComparisonRunner(PlanService(con, reader, fees, BOARD_RULES, listings, params), ComparisonStore(results_dir))
    result = runner.run(spec, authorization_subject=authorization_subject)
    print(json.dumps({"run_id": result["run_id"], "status": result["status"],
        "pool_count": len(pool), "human_selection": list(human), "fee_note": fee_note,
        "pool_by_step": pool_counts, "error": result.get("error"),
        "metrics": {kind: {key: value for key, value in metrics.items() if key not in {"series", "accounting_contributions"}}
                    for kind, metrics in result.get("portfolios", {}).items()},
        "result_file": str(results_dir / f"{result['run_id']}.result.json")}, ensure_ascii=False, indent=2))
    con.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("deploy/universe-snapshot"))
    parser.add_argument("--results-dir", type=Path, default=Path("deploy/universe-snapshot/comparisons"))
    parser.add_argument("--experiment-version", required=True)
    parser.add_argument("--authorization-subject", required=True)
    parser.add_argument("--max-single-name-pct", type=Decimal, default=Decimal("4"))
    args = parser.parse_args()
    result = run_archive(data_dir=args.data_dir.resolve(), results_dir=args.results_dir.resolve(),
        experiment_version=args.experiment_version, authorization_subject=args.authorization_subject,
        max_single_name_pct=args.max_single_name_pct)
    raise SystemExit(0 if result["status"] == "COMPLETED" else 1)


if __name__ == "__main__":
    main()
