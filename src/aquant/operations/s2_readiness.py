"""Read-only S2 input coverage for one published decision snapshot.

This is a diagnostic report, not a strategy registration or signal writer.
It uses only the separate, formal financial-fact repository for fundamentals;
the snapshot's optional legacy financials dataset is deliberately ignored.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from ..domain.data.db import connect
from ..domain.data.reader import SnapshotReader
from ..domain.data.snapshot import SnapshotStore
from ..domain.fundamentals.fact_repository import FinancialFactRepository
from ..domain.fundamentals.versioned import VersionedFinancialFactStore
from ..domain.research.s2 import S2Template, build_s2_signals
from .snapshot_lifecycle import validate_snapshot_id


class S2ReadinessError(ValueError):
    """A required frozen input is missing or inconsistent."""


def _market_caps(instruments: list[dict], trading_day: str) -> tuple[dict[str, Decimal], int]:
    caps: dict[str, Decimal] = {}
    missing = 0
    for row in instruments:
        iid = row["instrument_id"]
        cents = row.get("market_cap_cents")
        cap_day = row.get("market_cap_as_of")
        if cap_day is not None and cap_day != trading_day:
            raise S2ReadinessError(
                f"{iid} market_cap_as_of={cap_day!r} differs from frozen trading day {trading_day}"
            )
        if cents is None:
            missing += 1
            continue
        if cap_day is None:
            raise S2ReadinessError(f"{iid} has market_cap_cents without market_cap_as_of")
        if isinstance(cents, bool) or not isinstance(cents, int) or cents <= 0:
            raise S2ReadinessError(f"{iid} market_cap_cents must be a positive integer")
        caps[iid] = Decimal(cents) / Decimal(100)
    return caps, missing


def _industry_templates(instruments: list[dict]) -> dict[str, str]:
    templates = {}
    for row in instruments:
        code = str(row.get("industry_code") or "").strip().upper()
        # CSRC J sector (J66–J69) comprises banking, securities, insurance,
        # and other finance. The ordinary-business formula is not valid there.
        templates[row["instrument_id"]] = (
            S2Template.FINANCIAL.value if code.startswith("J") else
            S2Template.ORDINARY.value if code else "UNKNOWN"
        )
    return templates


def build_s2_readiness_report(
    *, snapshot_dir: str | Path, snapshot_id: str, facts_db: str | Path,
) -> dict[str, object]:
    """Evaluate coverage without creating databases, registering S2, or writing signals."""

    root = Path(snapshot_dir)
    metadata = root / "meta.sqlite"
    if not metadata.is_file():
        raise S2ReadinessError(f"published snapshot database does not exist: {metadata}")
    if not (root / "api").is_dir():
        raise S2ReadinessError(f"published snapshot dataset directory does not exist: {root / 'api'}")
    sid = validate_snapshot_id(snapshot_id)
    repository = FinancialFactRepository.open_existing(facts_db)

    with closing(connect(metadata, read_only=True)) as con:
        reader = SnapshotReader(SnapshotStore(con, root / "api"))
        snapshot = reader.published_snapshot(sid)
        ref = reader.ref(sid)
        if snapshot.trading_day is None:
            raise S2ReadinessError(f"snapshot {sid} has no frozen trading day")
        instruments = reader.instruments(sid, as_of=ref.as_of_time)
        quotes = reader.daily_quotes(sid, as_of=ref.as_of_time)
        if not quotes:
            raise S2ReadinessError(f"snapshot {sid} has no frozen daily quotes")
        latest_quote_day = max(quote.trading_day for quote in quotes).isoformat()
        if latest_quote_day != snapshot.trading_day:
            raise S2ReadinessError(
                f"snapshot trading day {snapshot.trading_day} differs from latest quote day "
                f"{latest_quote_day}"
            )

    ids = [row.get("instrument_id") for row in instruments]
    if any(not isinstance(iid, str) or not iid for iid in ids) or len(set(ids)) != len(ids):
        raise S2ReadinessError("snapshot instruments must have unique non-empty IDs")
    caps, missing_caps = _market_caps(instruments, snapshot.trading_day)
    templates = _industry_templates(instruments)

    all_store, accepted_reviews = repository.load_with_accepted_s2_pdf_reviews()
    all_facts = all_store.facts
    pool_ids = set(ids)
    pool_facts = [fact for fact in all_facts if fact.instrument_id in pool_ids]
    # A reconstructed fact first observed after this snapshot cannot be used to
    # claim historical readiness, even if its recorded available_at is earlier.
    observed_facts = [fact for fact in pool_facts if fact.first_seen_at <= ref.as_of_time]
    store = VersionedFinancialFactStore(observed_facts)
    evaluated = build_s2_signals(
        store=store, instrument_ids=ids, cutoff=ref.as_of_time,
        market_cap_by_instrument=caps,
        industry_template_by_instrument=templates,
    )
    # PIT selection must see every version. Removing an unreviewed successor
    # before selection would resurrect an older reviewed version incorrectly.
    complete = [
        item for item in evaluated
        if item.signal is not None and
        all(version in accepted_reviews for version in item.factors.fact_version_ids)
    ]
    used_ids = sorted({version for item in complete for version in item.factors.fact_version_ids})
    facts_by_id = {fact.version_id: fact for fact in observed_facts}
    used_facts = [facts_by_id[version] for version in used_ids]
    if any(fact.currency != "CNY" or fact.raw_unit != "yuan" for fact in used_facts):
        raise S2ReadinessError("complete S2 facts must use CNY yuan like frozen market caps")

    exclusions = []
    for item in evaluated:
        if item.signal is None:
            exclusions.append({"instrument_id": item.instrument_id,
                               "code": item.exclusion_code,
                               "reason": item.exclusion_reason})
        elif any(version not in accepted_reviews
                 for version in item.factors.fact_version_ids):
            exclusions.append({
                "instrument_id": item.instrument_id,
                "code": "S2_REVIEW_MISSING",
                "reason": "formula inputs lack accepted CNINFO S2 PDF review evidence",
            })
    counts = Counter(item["code"] for item in exclusions)
    evidence = [
        {"instrument_id": item.instrument_id,
         "fact_version_ids": list(item.factors.fact_version_ids),
         "review_bundles": [
             {"bundle_id": bundle_id, "evidence_hash": evidence_hash}
             for bundle_id, evidence_hash in sorted({
                 accepted_reviews[version] for version in item.factors.fact_version_ids
             })
         ]}
        for item in complete
    ]
    return {
        "schema_version": "aquant.s2_readiness.v1",
        "snapshot_dir": str(root),
        "snapshot_id": sid,
        "decision_cutoff_at": ref.as_of_time.isoformat(),
        "trading_day": snapshot.trading_day,
        "data_mode": ref.data_mode,
        "facts_db": str(Path(facts_db)),
        "strategy_family_status": "CLOSED",
        "registration_gate": "SEPARATE_ACCEPTANCE_REQUIRED",
        "pool_size": len(ids),
        "complete_s2_count": len(complete),
        "coverage_ratio": len(complete) / len(ids) if ids else 0.0,
        "missing_market_cap_count": missing_caps,
        "exclusion_counts": dict(sorted(counts.items())),
        "exclusions": exclusions,
        "fact_versions_in_repository": len(all_facts),
        "fact_versions_in_pool": len(pool_facts),
        "fact_versions_first_seen_after_cutoff": len(pool_facts) - len(observed_facts),
        "fact_versions_without_accepted_review": sum(
            fact.version_id not in accepted_reviews for fact in observed_facts),
        "engine_reported_fact_versions": len(used_facts),
        "accepted_review_bundles_used": len({
            accepted_reviews[version][0] for version in used_ids}),
        "source_documents_used": len({(fact.source_id, fact.source_document_id)
                                      for fact in used_facts}),
        "content_hashes_used": len({fact.content_hash for fact in used_facts}),
        "evidence": evidence,
    }


__all__ = ["S2ReadinessError", "build_s2_readiness_report"]
