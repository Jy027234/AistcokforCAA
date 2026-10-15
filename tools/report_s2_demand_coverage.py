"""按**冻结需求**逐项核对：112 个"指标×期间"值里有多少已通过机器核验。

与 ``report_s2_coverage.py`` 的分工不同：那个报告看的是"每只证券的状态与阻断
分类"（面向流程），这个工具看的是"需求清单里的每一个值到底有没有被证实"
（面向验收）。方案要求"不能再以候选数据采齐代替功能完成"，因此验收口径必须
落在**逐值**上，而不是"报告是否抽到"。

输出含三类信息：

* ``verified`` / ``total``：冻结需求里已通过机器核验的"指标×期间"值；
* ``missing``：未通过的值，附**具体原因**（抽取失败 / 逐字段未通过 / 无报告）；
* ``promotable``：已通过但尚未入库的值——这是"还差一步"的清单。

用法::

    python tools/report_s2_demand_coverage.py --sweep tmp/sweep-latest.json
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
SCHEMA = "aquant.s2-demand-coverage.v1"
DEFAULT_MANIFEST = ROOT / "data" / "s2-pdf-review" / "s2-demand-manifest.json"
DEFAULT_SWEEP = ROOT / "tmp" / "sweep-r19c.json"
DEFAULT_FACTS = ROOT / "data" / "s2-pdf-review" / "s2-machine-verified-facts.sqlite"
DEFAULT_OUTPUT = ROOT / "data" / "s2-pdf-review" / "s2-demand-coverage.json"


def _sweep_index(sweep: dict) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for report in sweep.get("reports", []):
        code, period = report.get("instrument"), report.get("period_end")
        if code and period:
            out[(code, period)] = report
    return out


def _promoted_pairs(facts_db: Path) -> set[tuple[str, str]]:
    """已入库事实覆盖的 (证券, 期间)。只读，不写库。"""

    if not facts_db.is_file():
        return set()
    sys.path.insert(0, str(ROOT / "src"))
    from aquant.domain.fundamentals.fact_repository import (  # noqa: PLC0415
        FinancialFactRepository,
    )

    repository = FinancialFactRepository.open_existing(str(facts_db))
    store, accepted = repository.load_with_accepted_s2_pdf_reviews()
    return {
        (fact.instrument_id, fact.period_end.isoformat())
        for fact in store.facts if fact.version_id in accepted
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--sweep", type=Path, default=DEFAULT_SWEEP)
    parser.add_argument("--facts-db", type=Path, default=DEFAULT_FACTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    sweep = json.loads(args.sweep.read_text(encoding="utf-8"))
    reports = _sweep_index(sweep)
    promoted = _promoted_pairs(args.facts_db)

    total = 0
    verified = 0
    missing: list[dict[str, object]] = []
    promotable: list[dict[str, object]] = []
    per_security: dict[str, dict[str, int]] = {}
    for security in manifest.get("securities", []):
        code = security.get("code")
        if security.get("status") != "DEMAND_FROZEN" or not code:
            continue
        counts = per_security.setdefault(code, {"verified": 0, "total": 0})
        for metric, days in sorted((security.get("required_periods") or {}).items()):
            for day in days:
                total += 1
                counts["total"] += 1
                report = reports.get((code, day))
                fields = set(report.get("verified_fields", [])) if report else set()
                # **必须逐字段判定，不能因为整份报告是 VERIFIED 就把每个需求
                # 都记成已完成**：``verified_fields`` 是"这份报告里哪些字段
                # 通过了"，而需求清单问的是"这一个指标这一个期间通过了没有"。
                # 早期写成 ``status == "VERIFIED" or metric in fields``，于是
                # 像 300750 2025-12-31 这种 ``revenue`` 通过、``operating_cashflow``
                # 未通过的期间，会被错误地把未通过的那一项也算成已完成——**覆盖率
                # 被高估**。宁可少算，也不能把未证实的值报成已证实。
                field_ok = report is not None and metric in fields
                if field_ok:
                    verified += 1
                    counts["verified"] += 1
                    if (code, day) not in promoted:
                        promotable.append({
                            "code": code, "period_end": day, "metric": metric,
                            "note": "verified but this period has no formal fact yet",
                        })
                    continue
                if report is None:
                    reason, detail = "NO_REPORT", "no archived report for this period"
                elif report.get("error"):
                    reason = "EXTRACT_FAILED"
                    detail = str(report["error"])[:160]
                else:
                    reason = "FIELD_UNVERIFIED"
                    detail = "unverified fields: " + ", ".join(
                        report.get("unverified_fields", []))
                missing.append({
                    "code": code, "period_end": day, "metric": metric,
                    "reason": reason, "detail": detail,
                })

    report_out = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest": str(args.manifest),
        "sweep": str(args.sweep),
        "verified_metric_periods": verified,
        "total_metric_periods": total,
        "coverage_ratio": round(verified / total, 4) if total else None,
        "per_security": dict(sorted(per_security.items())),
        "missing_reason_counts": dict(sorted(Counter(
            item["reason"] for item in missing).items())),
        "missing": missing,
        "verified_but_not_promoted": promotable,
        "boundary": (
            "Counts per metric-period VALUE, not per report: the plan requires the "
            "formal pipeline to compute the factors, so 'candidate data collected' "
            "is not completion."
        ),
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(report_out, stream, ensure_ascii=False, indent=1, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    print(f"{output}: {verified}/{total} frozen metric-periods verified "
          f"({report_out['coverage_ratio']})")
    for code, counts in report_out["per_security"].items():
        print(f"   {code}: {counts['verified']}/{counts['total']}")
    if missing:
        print("   missing by reason:", report_out["missing_reason_counts"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
