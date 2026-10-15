"""Read-only financial demand coverage for an immutable simulation snapshot.

Legacy optional profit records, accepted live official facts and historical
reconstructions remain separate. A newly admitted fact never changes an old
snapshot's availability; no result here opens the S2 registration gate.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

from ..domain.data.db import connect
from ..domain.data.pit import PitMode, PitViolation
from ..domain.data.reader import SnapshotReader
from ..domain.data.snapshot import SnapshotStore
from ..domain.fundamentals.fact_repository import FinancialFactRepository
from ..domain.fundamentals.pit import FinancialsStore
from ..domain.fundamentals.records import build_statements
from ..domain.fundamentals.versioned import ProfitScope, StatementScope, VersionedFinancialFactStore
from ..domain.research.s2 import S2ComputationError, compute_s2_financial_inputs, required_s2_periods
from .s2_readiness import admissible_facts, _build_s2_readiness_report_and_inventory
from .snapshot_lifecycle import validate_snapshot_id

SCHEMA = "aquant.financial_coverage.v1"


def security_code(instrument_id: str) -> str:
    """Use exact stored security codes; this never maps issuer aliases."""
    match = re.fullmatch(r"(?:(?:SH|SZ|BJ)\.)?(\d{6})", instrument_id)
    if not match:
        raise ValueError(f"financial coverage needs an explicit six-digit code: {instrument_id}")
    return match[1]


def _valid_fact(fact, metric: str) -> bool:
    scope = (ProfitScope.ATTRIBUTABLE if metric == "net_profit_attributable" else
             ProfitScope.CONSOLIDATED if metric == "net_profit_consolidated" else None)
    try:
        value = Decimal(str(fact.value))
    except (InvalidOperation, ValueError):
        return False
    return (fact.statement_scope is StatementScope.CONSOLIDATED and
            fact.profit_scope is scope and fact.currency == "CNY" and
            fact.raw_unit == "yuan" and value.is_finite())


def _pair_status(store, *, code, metric, period, cutoff, accepted=None):
    try:
        facts = store.select_pit(cutoff, instrument_id=code, metric=metric, period_end=period)
    except (PitViolation, ValueError) as exc:
        return {"status": "VERSION_CHAIN_BLOCKED", "reason": str(exc)}
    facts = [fact for fact in facts if _valid_fact(fact, metric)]
    if len(facts) != 1:
        return {"status": "FACT_MISSING" if not facts else "FACT_AMBIGUOUS"}
    fact = facts[0]
    review = accepted.get(fact.version_id) if accepted is not None else None
    return {"status": ("ACCEPTED" if review else "REVIEW_MISSING") if accepted is not None else
            "HISTORICAL_RECONSTRUCTED", "valueYuan": str(fact.value),
            "versionId": fact.version_id, "sourceId": fact.source_id,
            "sourceDocumentId": fact.source_document_id, "contentHash": fact.content_hash,
            "firstSeenAt": fact.first_seen_at.isoformat(),
            "availableAt": fact.available_at.isoformat(),
            "bundleId": review[0] if review else None,
            "evidenceHash": review[1] if review else None}


def build_financial_coverage_report(*, snapshot_dir: str | Path, snapshot_id: str,
                                  live_facts_db: str | Path,
                                  reconstructed_facts_db: str | Path | None = None,
                                  market_caps_archive_root: str | Path | None = None,
                                  priority_instrument_ids=(),
                                  latest_period_end: date = date(2026, 6, 30),
                                  latest_period_ends: dict[str, date] | None = None,
                                  target_period_records=(),
                                  target_period_basis: str = "Frozen 2026H1 research demand; not an assertion that no later report exists") -> dict:
    root = Path(snapshot_dir)
    sid = validate_snapshot_id(snapshot_id)
    with closing(connect(root / "meta.sqlite", read_only=True)) as con:
        reader = SnapshotReader(SnapshotStore(con, root / "api"))
        snapshot = reader.published_snapshot(sid)
        ref = reader.ref(sid)
        instruments = reader.instruments(sid, as_of=ref.as_of_time)
        calendar = [date.fromisoformat(day) for day in
                    reader.trading_calendar(sid, as_of=ref.as_of_time)]
        legacy = reader.financials(sid, as_of=ref.as_of_time)
    ids = [item["instrument_id"] for item in instruments]
    codes = [security_code(iid) for iid in ids]
    if len(set(ids)) != len(ids) or len(set(codes)) != len(codes):
        raise ValueError("financial coverage snapshot must have unique IDs and security codes")
    code_set = set(codes)
    readiness, all_store, all_accepted = _build_s2_readiness_report_and_inventory(
        snapshot_dir=root, snapshot_id=sid, facts_db=live_facts_db,
        latest_period_end=latest_period_end,
        market_caps_archive_root=market_caps_archive_root, include_signals=True,
        latest_period_ends=latest_period_ends, target_period_records=target_period_records,
        target_period_basis=target_period_basis)
    live_facts = [fact for fact in all_store.facts if fact.pit_mode is PitMode.LIVE_OBSERVED]
    live_pool = [fact for fact in live_facts if security_code(fact.instrument_id) in code_set]
    # Known CNINFO facts use bare codes. Preserve all original fact objects;
    # accepting prefixed IDs would need an explicit repository identity migration.
    if any(fact.instrument_id != security_code(fact.instrument_id) for fact in live_pool):
        raise ValueError("official fact repository must retain its explicit bare-code identity")
    admitted, rejected = admissible_facts(facts=live_pool, cutoff=ref.as_of_time,
                                         allow_reconstructed=False)
    live_store = VersionedFinancialFactStore(admitted)
    pending_store = VersionedFinancialFactStore(live_pool)
    pending_cutoff = max([datetime.now(timezone.utc), ref.as_of_time] +
                         [fact.available_at for fact in live_pool])
    historical_facts = []
    if reconstructed_facts_db is not None:
        historical = FinancialFactRepository.open_existing(reconstructed_facts_db).load()
        historical_facts = [fact for fact in historical.facts
                            if fact.pit_mode is PitMode.HISTORICAL_RECONSTRUCTED and
                            security_code(fact.instrument_id) in code_set]
        historical_facts, _ = admissible_facts(facts=historical_facts,
                                              cutoff=ref.as_of_time, allow_reconstructed=True)
    historical_store = VersionedFinancialFactStore(historical_facts)
    statements, skipped = build_statements(legacy or {})
    profit_store = FinancialsStore(statements=statements, trading_days=calendar)
    priorities = set(priority_instrument_ids)
    formal_inputs = {row["instrument_id"]: row for row in readiness["security_demand"]}
    formal_signals = {row["instrument_id"]: row for row in readiness.get("signals", [])}
    formal_exclusions = {row["instrument_id"]: row for row in readiness["exclusions"]}
    rows = []
    for instrument, code in zip(instruments, codes):
        iid = instrument["instrument_id"]
        actual_input = formal_inputs[iid]
        target = date.fromisoformat(actual_input["target_period_end"])
        required = required_s2_periods(target)
        required_count = sum(map(len, required.values()))
        q = (target.month - 1) // 3 + 1
        legacy_periods = ([f"{target.year}Q4"] if q == 4 else
                         [f"{target.year - 1}Q{q}", f"{target.year - 1}Q4", f"{target.year}Q{q}"])
        template = ("FINANCIAL" if str(instrument.get("industry_code") or "").startswith("J") else
                    "ORDINARY" if instrument.get("industry_code") else "UNKNOWN")
        ttm = profit_store.trailing_twelve_months(iid, as_of=ref.as_of_time)
        periods = (legacy.get("statements") or {}).get(iid, {})
        legacy_missing = [period for period in legacy_periods if not periods.get(period) or
                          any(periods[period].get(field) in (None, "") for field in
                              ("netProfit", "statDate", "pubDate"))]
        pairs = []
        historical_pairs = []
        for metric, metric_periods in sorted(required.items()):
            for period in metric_periods:
                pair = _pair_status(live_store, code=code, metric=metric,
                                    period=period, cutoff=ref.as_of_time, accepted=all_accepted)
                if pair["status"] != "ACCEPTED":
                    pending = _pair_status(pending_store, code=code, metric=metric,
                                           period=period, cutoff=pending_cutoff, accepted=all_accepted)
                    if pending["status"] == "ACCEPTED":
                        pair = {**pending, "status": "NEXT_CUTOFF_PENDING",
                                "reason": "accepted archive fact was observed or available after this snapshot cutoff"}
                pairs.append({"metric": metric, "periodEnd": period.isoformat(), **pair})
                historical_pairs.append({"metric": metric, "periodEnd": period.isoformat(),
                                         **_pair_status(historical_store, code=code, metric=metric,
                                                        period=period, cutoff=ref.as_of_time)})
        covered = sum(pair["status"] == "ACCEPTED" for pair in pairs)
        pending = sum(pair["status"] == "NEXT_CUTOFF_PENDING" for pair in pairs)
        reconstructed = sum(pair["status"] == "HISTORICAL_RECONSTRUCTED"
                            for pair in historical_pairs)
        financial_bridge = (actual_input["financial_inputs"]
                            if actual_input["financial_input_status"] == "ACCEPTED" and
                            actual_input["financial_inputs"]["latest_period_end"] == target.isoformat() else None)
        bridge_status = "ACCEPTED" if financial_bridge is not None else actual_input["financial_input_status"]
        bridge_cutoff = ref.as_of_time
        if financial_bridge is None and covered + pending == required_count and template == "ORDINARY":
            try:
                bridge = compute_s2_financial_inputs(store=pending_store, instrument_id=code,
                    cutoff=pending_cutoff, industry_template=template)
                if bridge.latest_period_end == target and all(version in all_accepted for version in bridge.fact_version_ids):
                    financial_bridge = bridge.as_dict()
                    bridge_status = "NEXT_CUTOFF_PENDING"
                    bridge_cutoff = pending_cutoff
            except (S2ComputationError, PitViolation, ValueError):
                pass
        factor_complete = iid in formal_signals
        exclusion = "S2_FINANCIAL_FACT_MISSING"
        reason = "accepted live official metric-period demand is incomplete"
        if factor_complete:
            exclusion, reason = None, None
        elif iid in formal_exclusions:
            exclusion, reason = formal_exclusions[iid]["code"], formal_exclusions[iid]["reason"]
        elif template != "ORDINARY":
            exclusion = "S2_FINANCIAL_INDUSTRY_TEMPLATE_UNSUPPORTED" if template == "FINANCIAL" else "S2_INDUSTRY_UNCLASSIFIED"
            reason = "ordinary-business formula does not apply to this industry template"
        rows.append({"instrumentId": iid, "code": code,
                     "displayName": instrument.get("short_name"), "template": template,
                     "targetPeriodEnd": target.isoformat(),
                     "targetPeriodBasis": actual_input["target_period_basis"],
                     "targetPeriodDiscovery": actual_input.get("target_discovery"),
                     "isPriority": iid in priorities,
                     "s1Optional": {"statementPresent": bool(periods),
                                    "ttmAvailable": ttm is not None,
                                    "ttmNetProfitMicros": ttm["ttm_net_profit_micros"] if ttm else None,
                                    "latestStatementPeriod": ttm["latest_stat_date"] if ttm else None,
                                    "missingPeriods": legacy_missing},
                     "formalS2": {"requiredMetricPeriodCount": required_count,
                                  "acceptedMetricPeriodCount": covered,
                                  "pendingMetricPeriodCount": pending,
                                  "completePendingFacts": pending > 0 and covered + pending == required_count,
                                  "completeFacts": covered == required_count,
                                  "completeFactors": factor_complete,
                                  "factorExclusionCode": exclusion,
                                  "factorExclusionReason": reason, "metricPeriods": pairs,
                                  "financialInputs": financial_bridge, "financialInputStatus": bridge_status,
                                  "financialInputCutoffAt": bridge_cutoff.isoformat(),
                                  "marketCap": actual_input["market_cap"],
                                  "signal": formal_signals.get(iid)},
                     "historicalReconstructed": {"availableMetricPeriodCount": reconstructed,
                                                 "completeFacts": reconstructed == required_count,
                                                 "metricPeriods": historical_pairs}})
    count = len(rows)
    presence = sum(row["s1Optional"]["statementPresent"] for row in rows)
    ttms = sum(row["s1Optional"]["ttmAvailable"] for row in rows)
    return {"schema": SCHEMA, "snapshotId": sid, "snapshotCutoffAt": ref.as_of_time.isoformat(),
            "tradingDay": snapshot.trading_day, "generatedAt": datetime.now(timezone.utc).isoformat(),
            "latestPeriodEnd": latest_period_end.isoformat(),
            "targetPeriodEnd": latest_period_end.isoformat(), "targetPeriodBasis": target_period_basis,
            "s1Optional": {"statementSecurityCount": presence, "ttmSecurityCount": ttms,
                           "poolCount": count, "presenceRatio": presence / count if count else 0,
                           "ttmRatio": ttms / count if count else 0,
                           "skippedStatementCount": len(skipped), "sourceId": legacy.get("source_id"),
                           "cacheUpdatedAt": legacy.get("updated_at"),
                           "note": "Optional frozen BaoStock profit data; not accepted official S2 facts."},
            "formalS2": {"factsDb": str(Path(live_facts_db)), "poolCount": count,
                         "liveFactCount": len(live_facts),
                         "acceptedFactCount": sum(fact.version_id in all_accepted for fact in live_facts),
                         "poolLiveFactCount": len(live_pool), "poolAdmittedFactCount": len(admitted),
                         "rejectionCounts": dict(Counter(reason for _, reason in rejected)),
                         "requiredMetricPeriodCount": sum(row["formalS2"]["requiredMetricPeriodCount"] for row in rows),
                         "acceptedMetricPeriodCount": sum(row["formalS2"]["acceptedMetricPeriodCount"] for row in rows),
                         "pendingMetricPeriodCount": sum(row["formalS2"]["pendingMetricPeriodCount"] for row in rows),
                         "pendingCompleteFactSecurityCount": sum(row["formalS2"]["completePendingFacts"] for row in rows),
                         "completeFactSecurityCount": sum(row["formalS2"]["completeFacts"] for row in rows),
                         "completeFactorSecurityCount": sum(row["formalS2"]["completeFactors"] for row in rows),
                         "scopeStatus": readiness["scope_status"],
                         "rankingPoolSize": readiness.get("ranking_pool_size", 0),
                         "missingMarketCapCount": readiness["missing_market_cap_count"],
                         "eligibleScope": readiness["eligible_scope"],
                         "strategyFamilyStatus": "CLOSED"},
            "historicalReconstructed": {"factsDb": str(Path(reconstructed_facts_db)) if reconstructed_facts_db else None,
                "poolCount": count, "requiredMetricPeriodCount": sum(row["formalS2"]["requiredMetricPeriodCount"] for row in rows),
                "availableMetricPeriodCount": sum(row["historicalReconstructed"]["availableMetricPeriodCount"] for row in rows),
                "completeFactSecurityCount": sum(row["historicalReconstructed"]["completeFacts"] for row in rows),
                "pitMode": "HISTORICAL_RECONSTRUCTED"}, "securities": rows,
            "note": "Coverage diagnostic; current archive admissions do not rewrite frozen inputs or enable S2."}
