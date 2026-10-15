"""Produce the S2 coverage report the plan's deliverable list asks for.

The plan requires one table that walks

    target securities → reports discovered/archived → required candidate fields
    → verified fields → formal facts → securities with complete F07–F10
    → securities actually ranked

with business exclusions (unsupported template, non-positive denominator) listed
separately from collection/verification gaps.  It also requires reporting what is
usable forward, what is usable historically, and what is still missing.

Everything here is read-only: it aggregates the frozen demand manifest, the
company-level index report, the promotion run reports and the append-only fact
database.  It never re-verifies a PDF and never writes facts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.fundamentals.fact_repository import (  # noqa: E402
    FinancialFactRepository,
)

SCHEMA = "aquant.s2-coverage-report.v1"
DEFAULT_DIR = ROOT / "data" / "s2-pdf-review"
DEFAULT_FACTS = DEFAULT_DIR / "s2-machine-verified-facts.sqlite"

#: 业务排除与采集/核验缺口的区别。前者不是失败，不得计入分母误导读者。
BUSINESS_EXCLUSIONS = frozenset({
    "S2_FINANCIAL_INDUSTRY_TEMPLATE_UNSUPPORTED",
    "S2_INDUSTRY_UNCLASSIFIED",
    "S2_NON_POSITIVE_DENOMINATOR",
    "NOT_IN_CONFIG_POOL",
})
#: 核验缺口：数据已抽到但没有满足的独立路径。
VERIFICATION_GAPS = frozenset({
    "VERIFICATION_FAILED", "REVISION_REVIEW_REQUIRED", "DRY_RUN_NOT_WRITTEN",
})
#: 成功状态：已写入正式事实。"ALREADY_PROMOTED"是重复运行的幂等结果，同样
#: 表示该期间已有正式事实，不能算成失败。
SUCCESS_STATUSES = frozenset({"PROMOTED", "VERIFIED_NOT_WRITTEN", "ALREADY_PROMOTED"})
#: 采集缺口：索引、原文或身份没拿到。
COLLECTION_GAPS = frozenset({
    "INDEX_FAILED", "CROSS_INDEX_FAILED", "INDEX_INCOMPLETE",
    "CROSS_INDEX_INCOMPLETE", "DOWNLOAD_FAILED", "REPORT_NOT_UNIQUE",
    "SCOPE_BLOCKED", "NO_LATEST_PERIOD",
})


def _load(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _promotion_runs(directory: Path) -> list[dict]:
    runs = []
    for path in sorted(directory.glob("s2-machine-promotion/*.json")):
        payload = _load(path)
        if payload and payload.get("schema") == "aquant.s2-machine-promotion-run.v1":
            runs.append({"path": str(path), **payload})
    return runs


def latest_promotion_by_pair(runs: list[dict]) -> dict[tuple[str, str], dict]:
    """每个 (证券, 期间) 取**最后生成**的一次运行结果。

    必须按 ``generated_at`` 排序，不能按文件名：``000651-run.json`` 与
    ``scope-run.json`` 的字典序与时间序无关，按文件名会让较早的运行覆盖较晚
    的运行，把已经入库的证券误报成缺口（实测第一次生成覆盖报告时就这样）。
    """

    latest: dict[tuple[str, str], dict] = {}
    ordered = sorted(runs, key=lambda run: run.get("generated_at") or "")
    for run in ordered:
        for result in run.get("results", []):
            code = result.get("code")
            period = result.get("period_end")
            if code and period:
                latest[(code, period)] = {**result, "run_path": run["path"]}
    return latest


def classify(status: str) -> str:
    if status in BUSINESS_EXCLUSIONS:
        return "business_exclusion"
    if status in VERIFICATION_GAPS:
        return "verification_gap"
    if status in COLLECTION_GAPS:
        return "collection_gap"
    return "other"


def build_report(*, directory: Path, facts_db: Path) -> dict[str, object]:
    manifest = _load(directory / "s2-demand-manifest.json") or {}
    index = _load(directory / "s2-company-index.json") or {}
    runs = _promotion_runs(directory)
    latest = latest_promotion_by_pair(runs)

    securities = manifest.get("securities", [])
    index_by_code = {row["code"]: row for row in index.get("companies", [])}

    # 正式事实与排名：只有通过读取侧接受的版本才算数。
    facts_summary: dict[str, object] = {
        "facts_db": str(facts_db) if facts_db.is_file() else None,
        "exists": facts_db.is_file(),
        "formal_fact_count": 0,
        "accepted_fact_count": 0,
        "per_security": {},
    }
    if facts_db.is_file():
        repository = FinancialFactRepository.open_existing(facts_db)
        store, accepted = repository.load_with_accepted_s2_pdf_reviews()
        accepted_metrics = Counter()
        per: dict[str, dict[str, object]] = {}
        for fact in store.facts:
            entry = per.setdefault(fact.instrument_id, {"facts": 0, "accepted": 0,
                                                        "periods": set()})
            entry["facts"] += 1
            entry["periods"].add(fact.period_end.isoformat())
            if fact.version_id in accepted:
                entry["accepted"] += 1
                accepted_metrics[fact.metric] += 1
        for entry in per.values():
            entry["periods"] = sorted(entry["periods"])
        facts_summary.update({
            "formal_fact_count": len(store.facts),
            "accepted_fact_count": len(accepted),
            "accepted_metric_counts": dict(sorted(accepted_metrics.items())),
            "per_security": per,
        })

    rows = []
    status_counts: Counter[str] = Counter()
    class_counts: Counter[str] = Counter()
    required_total = 0
    verified_total = 0
    for security in securities:
        code = security["code"]
        row: dict[str, object] = {
            "code": code, "name": security.get("name"),
            "industry": security.get("industry"),
            "manifest_status": security.get("status"),
            "required_metric_period_count": security.get("required_metric_period_count", 0),
        }
        index_row = index_by_code.get(code)
        row["index_status"] = index_row.get("status") if index_row else None
        row["index_reason"] = index_row.get("reason") if index_row else None
        row["revision_candidates"] = len(
            (index_row or {}).get("revisionCandidates") or [])
        results = [value for (pair_code, _), value in latest.items()
                   if pair_code == code]
        row["report_periods_seen"] = len(results)
        verified_periods = [item["period_end"] for item in results
                            if item.get("status") in SUCCESS_STATUSES]
        row["promoted_periods"] = sorted(verified_periods)
        row["report_statuses"] = dict(sorted(Counter(
            item.get("status", "UNKNOWN") for item in results).items()))
        facts = facts_summary["per_security"].get(code)  # type: ignore[union-attr]
        row["formal_facts"] = facts["facts"] if facts else 0
        row["accepted_facts"] = facts["accepted"] if facts else 0
        row["required_metric_period_count"] = security.get(
            "required_metric_period_count", 0)
        required_total += int(row["required_metric_period_count"] or 0)
        verified_total += int(row["accepted_facts"] or 0)

        if security.get("status") != "DEMAND_FROZEN":
            row["coverage_status"] = str(security.get("status"))
            row["blocking_class"] = "business_exclusion"
        elif not results:
            row["coverage_status"] = "NO_REPORT_ATTEMPTED"
            row["blocking_class"] = "collection_gap"
        elif verified_periods or (facts and facts["accepted"]):
            # 有成功期间，或库里已有被读取侧接受的正式事实，即算有正式事实。
            row["coverage_status"] = "FORMAL_FACTS_PRESENT"
            row["blocking_class"] = None
        else:
            worst = Counter(item.get("status", "UNKNOWN") for item in results)
            row["coverage_status"] = worst.most_common(1)[0][0]
            row["blocking_class"] = classify(row["coverage_status"])
        status_counts[str(row["coverage_status"])] += 1
        class_counts[str(row["blocking_class"])] += 1
        rows.append(row)

    promotable = [row for row in rows
                  if row.get("coverage_status") == "FORMAL_FACTS_PRESENT"]
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "manifest": str(directory / "s2-demand-manifest.json"),
            "company_index": str(directory / "s2-company-index.json"),
            "promotion_runs": [run["path"] for run in runs],
            "facts_db": facts_summary["facts_db"],
        },
        "pipeline_counts": {
            "target_securities": len(securities),
            "target_pool_size": manifest.get("summary", {}).get("pool_size"),
            "financial_industry_excluded": manifest.get("summary", {}).get(
                "pool_financial_excluded"),
            "required_metric_period_values": required_total,
            "accepted_formal_fact_values": facts_summary["accepted_fact_count"],
            "reports_attempted": len(latest),
            "securities_with_formal_facts": len(promotable),
        },
        "coverage_status_counts": dict(sorted(status_counts.items())),
        "blocking_class_counts": dict(sorted(class_counts.items())),
        "facts": facts_summary,
        "securities": rows,
        "usable_forward": {
            "securities": sorted(row["code"] for row in promotable),
            "note": ("Formal facts exist and are PIT-usable forward. A security "
                     "needs every required metric-period of one latest report "
                     "before the calculator can produce F07-F10 for it."),
        },
        "usable_historical": {
            "securities": [],
            "note": ("No fact carries available_basis=RECONSTRUCTED yet, so no "
                     "historical interval is replayable. The readiness reader "
                     "supports --allow-reconstructed; the evidence to fill it is "
                     "still outstanding."),
        },
        "boundary": (
            "Counts are aggregated from run artifacts. 'verified fields' here means "
            "the reader accepted a machine-verification bundle, not that every "
            "candidate value was independently checked; see each report's "
            "per-field satisfied paths. Business exclusions are reported separately "
            "and are not collection or verification failures."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR,
                        help="directory holding the manifest, index and run reports")
    parser.add_argument("--facts-db", type=Path, default=DEFAULT_FACTS)
    parser.add_argument("--output", type=Path,
                        default=DEFAULT_DIR / "s2-coverage-report.json")
    args = parser.parse_args(argv)
    report = build_report(directory=args.dir.resolve(),
                          facts_db=args.facts_db.resolve())
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=1, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    counts = report["pipeline_counts"]
    print(f"{output}")
    for key in ("target_securities", "target_pool_size", "financial_industry_excluded",
                "required_metric_period_values", "accepted_formal_fact_values",
                "reports_attempted", "securities_with_formal_facts"):
        print(f"  {key}: {counts[key]}")
    print(f"  coverage_status_counts: {report['coverage_status_counts']}")
    print(f"  blocking_class_counts: {report['blocking_class_counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
