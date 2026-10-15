"""Read-only actual financial gap repair evidence on frozen 20/120 scopes."""
from __future__ import annotations
import argparse
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from aquant.domain.fundamentals.fact_repository import FinancialFactRepository
from aquant.domain.fundamentals.versioned import VersionedFinancialFactStore
from aquant.domain.research.s2 import compute_s2_financial_inputs, required_s2_periods, S2ComputationError


def build_report(*, baseline_path: Path, anchor_path: Path, repair_path: Path):
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
    repair = json.loads(repair_path.read_text(encoding="utf-8"))
    prior_path = Path(baseline["priorReportPath"])
    if "sha256:" + hashlib.sha256(prior_path.read_bytes()).hexdigest() != baseline["priorReportHash"]:
        raise ValueError("original fixed 100-report result has changed")
    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    db = Path(baseline["factDb"])
    with closing(sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        raw_facts = {row["version_id"]: dict(row) for row in con.execute("SELECT * FROM financial_fact")}
        raw_reviews = {row["bundle_id"]: dict(row) for row in con.execute("SELECT * FROM financial_fact_review")}
    for row in baseline["facts"]:
        if raw_facts.get(row["version_id"]) != row:
            raise ValueError("a pre-existing immutable financial fact has changed")
    for row in baseline["reviews"]:
        if raw_reviews.get(row["bundle_id"]) != row:
            raise ValueError("a pre-existing immutable financial review has changed")
    old_ids = {row["version_id"] for row in baseline["facts"]}
    new_ids = set(raw_facts) - old_ids
    store, accepted = FinancialFactRepository.open_existing(db).load_with_accepted_s2_pdf_reviews()
    accepted_facts = [fact for fact in store.facts if fact.version_id in accepted]
    if any(fact.pit_mode.value != "LIVE_OBSERVED" for fact in store.facts):
        raise ValueError("gap proof must use the live-only accepted warehouse")
    live_store = VersionedFinancialFactStore(accepted_facts)
    fact_pairs = {}
    for fact in accepted_facts:
        key = (fact.instrument_id, fact.metric, fact.period_end)
        if key in fact_pairs:
            raise ValueError("gap audit does not guess among competing fact versions")
        fact_pairs[key] = fact
    priority_codes = {row["code"] for row in prior["results"]}
    if len(priority_codes) != 20 or len(anchor["securities"]) != 120:
        raise ValueError("fixed 20/120 scope has changed")
    cutoff = datetime.fromisoformat(anchor["snapshotCutoffAt"])
    observed_now = datetime.now(timezone.utc)
    diagnostic_cutoff = max([observed_now] + [fact.available_at for fact in accepted_facts
                                            if fact.instrument_id in priority_codes])
    securities = []
    for instrument in anchor["securities"]:
        code = instrument["code"]
        target = date.fromisoformat(instrument["targetPeriodEnd"])
        demand = required_s2_periods(target)
        pairs = []
        for metric, periods in sorted(demand.items()):
            for period in sorted(periods):
                fact = fact_pairs.get((code, metric, period))
                current = bool(fact and max(fact.available_at, fact.first_seen_at, fact.ingested_at) <= cutoff)
                pairs.append({"metric": metric, "periodEnd": period.isoformat(),
                    "status": "CURRENT_CUTOFF_ACCEPTED" if current else "NEXT_CUTOFF_PENDING" if fact else "MISSING",
                    "versionId": fact.version_id if fact else None,
                    "valueYuan": str(fact.value) if fact else None})
        complete = all(pair["status"] != "MISSING" for pair in pairs)
        financial_inputs = None
        exclusions = []
        if complete:
            try:
                inputs = compute_s2_financial_inputs(store=live_store, instrument_id=code,
                    cutoff=diagnostic_cutoff, industry_template=instrument["template"])
                financial_inputs = inputs.as_dict()
                for factor, field in (("F07", "average_parent_equity"),
                                      ("F08", "consolidated_net_profit_ttm"),
                                      ("F09", "prior_revenue_ttm")):
                    value = getattr(inputs, field)
                    if value <= 0:
                        exclusions.append({"code": "S2_NON_POSITIVE_DENOMINATOR", "factor": factor,
                                           "denominator": field, "valueYuan": str(value)})
            except S2ComputationError as exc:
                exclusions.append(exc.as_error())
        securities.append({"instrumentId": instrument["instrumentId"], "code": code,
            "displayName": instrument["displayName"], "isPriority": code in priority_codes,
            "targetPeriodEnd": target.isoformat(), "requiredMetricPeriodCount": len(pairs),
            "verifiedMetricPeriodCount": sum(pair["status"] != "MISSING" for pair in pairs),
            "beforeVerifiedMetricPeriodCount": sum(pair["versionId"] in old_ids for pair in pairs),
            "currentCutoffAcceptedCount": sum(pair["status"] == "CURRENT_CUTOFF_ACCEPTED" for pair in pairs),
            "missingMetricPeriods": [pair for pair in pairs if pair["status"] == "MISSING"],
            "completeFinancialInputs": complete and financial_inputs is not None,
            "diagnosticFinancialInputs": financial_inputs, "businessExclusions": exclusions,
            "formulaStatus": "FINANCIAL_INPUTS_COMPLETE_MARKET_CAP_REQUIRED" if complete and not exclusions else
                             "BUSINESS_EXCLUDED" if exclusions else "REQUIRED_FINANCIAL_FACTS_MISSING"})
    priority = [row for row in securities if row["isPriority"]]
    def summary(rows):
        return {"securityCount": len(rows), "requiredMetricPeriodCount": sum(row["requiredMetricPeriodCount"] for row in rows),
            "beforeVerifiedMetricPeriodCount": sum(row["beforeVerifiedMetricPeriodCount"] for row in rows),
            "verifiedMetricPeriodCount": sum(row["verifiedMetricPeriodCount"] for row in rows),
            "currentCutoffAcceptedMetricPeriodCount": sum(row["currentCutoffAcceptedCount"] for row in rows),
            "completeFinancialInputSecurityCount": sum(row["completeFinancialInputs"] for row in rows),
            "beforeCompleteFactSecurityCount": sum(row["beforeVerifiedMetricPeriodCount"] == row["requiredMetricPeriodCount"] for row in rows),
            "positiveFinancialDenominatorSecurityCount": sum(row["completeFinancialInputs"] and not row["businessExclusions"] for row in rows),
            "businessExcludedSecurityCount": sum(bool(row["businessExclusions"]) for row in rows)}
    def classify(row):
        reason = row.get("reason", "")
        return "COVER_IDENTITY" if "cover company" in reason else "NO_STATEMENT_SECTIONS" if "no statement" in reason else "AMBIGUOUS_LAYOUT" if "ambiguous" in reason or "uniquely" in reason else "INDEPENDENT_CHECK_FAILED"
    return {"schema": "aquant.financial-gap-repair-evidence.v1", "generatedAt": observed_now.isoformat(),
        "snapshotId": anchor["snapshotId"], "snapshotCutoffAt": cutoff.isoformat(),
        "targetPeriodBasis": "frozen official 20-security discovery: latest available report was 2026H1",
        "diagnosticCutoffAt": diagnostic_cutoff.isoformat(),
        "diagnosticNote": "Future availability diagnostics do not create a decision or change any published snapshot. F10 requires actual same-day admitted market cap; no placeholder is used.",
        "sourceReportPath": str(prior_path), "sourceReportHash": baseline["priorReportHash"],
        "repairReportPath": str(repair_path), "repairReportHash": "sha256:" + hashlib.sha256(repair_path.read_bytes()).hexdigest(),
        "originalFailureCategories": dict(Counter(classify(row) for row in prior["results"] if row["status"] == "VERIFICATION_FAILED")),
        "processedRepairReportCount": len(repair["results"]), "repairCounts": repair["counts"],
        "originalReportCount": len(prior["results"]), "preExistingFactCount": len(old_ids),
        "preExistingReviewCount": len(baseline["reviews"]), "preExistingFactsAndReviewsUnchanged": True,
        "newFactCount": len(new_ids), "newAcceptedFactCount": len(new_ids & set(accepted)),
        "liveFactCount": len(store.facts), "acceptedLiveFactCount": len(accepted),
        "newFacts": [raw_facts[iid] for iid in sorted(new_ids)],
        "priorityScope": summary(priority), "simulationScope": summary(securities),
        "securities": securities, "providerRequestsStarted": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--coverage-anchor", type=Path, required=True)
    parser.add_argument("--repair-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = build_report(baseline_path=args.baseline, anchor_path=args.coverage_anchor, repair_path=args.repair_report)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({key: report[key] for key in ("newFactCount", "priorityScope", "simulationScope")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
