"""Read-only S2 input coverage for one published decision snapshot.

This is a diagnostic report, not a strategy registration or signal writer.
It uses only the separate, formal financial-fact repository for fundamentals;
the snapshot's optional legacy financials dataset is deliberately ignored.

Two PIT modes are reported separately, as the main specification requires:

``LIVE_OBSERVED``
    A fact may back a forward conclusion only if the system had really observed
    it by the snapshot cutoff (``first_seen_at <= cutoff``).  A fact captured
    today, however old its report period, can never be inserted into a snapshot
    that was already published.
``HISTORICAL_RECONSTRUCTED``
    A fact whose ``available_at`` was reconstructed from genuine original
    publication evidence (original announcement archive, revision relation and
    a conservative date rule) may be replayed at a historical cutoff, **while
    keeping its real first-seen time**.  That mode is therefore *not* filtered
    by ``first_seen_at``; it is filtered by ``available_at`` and by an explicit
    ``available_basis=RECONSTRUCTED`` claim.

Removing the first-seen filter globally would let a today-captured fact back a
past decision, so the filter stays for the forward mode and only the
reconstruction branch is exempted.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import replace
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from aquant.artifact_paths import resolve_artifact_path

from ..domain.data.db import connect
from ..domain.data.pit import AvailabilityBasis, PitMode, PitViolation
from ..domain.data.reader import SnapshotReader
from ..domain.data.snapshot import SnapshotStore
from ..domain.fundamentals.fact_repository import FinancialFactRepository
from ..domain.fundamentals.versioned import VersionedFinancialFactStore
from ..domain.research.s2 import (S2Template, S2ComputationError, build_s2_signals,
                                compute_s2_financial_inputs, required_s2_periods)
from .decision_market_caps import (
    DecisionMarketCapError, load_sidecar, normalize_instrument_id,
    SUPPORTED_SOURCE_IDS, _parse_aware_datetime, _validate_observation_time,
    verify_frozen_market_caps_archive,
)
from .snapshot_lifecycle import validate_snapshot_id


#: 允许用于正式读取的可用性依据。UNKNOWN 一律拒绝（§7.2）。
_FORWARD_BASES = frozenset({AvailabilityBasis.OBSERVED, AvailabilityBasis.VENDOR_PIT})
_RECONSTRUCTED_BASES = frozenset({AvailabilityBasis.RECONSTRUCTED})


class S2ReadinessError(ValueError):
    """A required frozen input is missing or inconsistent."""


def admissible_facts(*, facts, cutoff, allow_reconstructed: bool):
    """按 PIT 模式筛选可用事实，并返回逐条排除原因。

    前向分支保留 ``first_seen_at <= cutoff``；历史分支不做首见过滤，但要求
    版本显式声明 ``RECONSTRUCTED`` 依据，并仍由领域 PIT 的 ``available_at``
    门禁决定它在 cutoff 是否可见。
    """

    admitted, rejected = [], []
    for fact in facts:
        if fact.available_at > cutoff:
            rejected.append((fact, "S2_FACT_NOT_YET_AVAILABLE"))
            continue
        if fact.pit_mode is PitMode.LIVE_OBSERVED:
            if fact.first_seen_at > cutoff:
                rejected.append((fact, "S2_FACT_FIRST_SEEN_AFTER_CUTOFF"))
                continue
            if fact.availability_basis not in _FORWARD_BASES:
                rejected.append((fact, "S2_FACT_AVAILABILITY_BASIS_UNUSABLE"))
                continue
        elif fact.pit_mode is PitMode.HISTORICAL_RECONSTRUCTED:
            if not allow_reconstructed:
                rejected.append((fact, "S2_FACT_RECONSTRUCTION_NOT_REQUESTED"))
                continue
            if fact.availability_basis not in _RECONSTRUCTED_BASES:
                rejected.append((fact, "S2_FACT_RECONSTRUCTION_UNPROVEN"))
                continue
        else:
            rejected.append((fact, "S2_FACT_PIT_MODE_UNKNOWN"))
            continue
        admitted.append(fact)
    return admitted, rejected


def _market_caps(instruments: list[dict], trading_day: str, *, cutoff=None,
                 require_provenance: bool = False) -> tuple[dict[str, Decimal], int]:
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
        if require_provenance:
            if (row.get("market_cap_source_id") not in SUPPORTED_SOURCE_IDS or
                    not row.get("market_cap_receipt_id") or
                    not str(row.get("market_cap_content_hash") or "").startswith("sha256:")):
                missing += 1
                continue
            try:
                observed = _parse_aware_datetime(row.get("market_cap_observed_at"),
                                                 "market_cap_observed_at")
                _validate_observation_time(observed, date.fromisoformat(trading_day))
            except DecisionMarketCapError as exc:
                raise S2ReadinessError(f"{iid}: {exc}") from exc
            if cutoff is not None and observed > cutoff:
                missing += 1
                continue
        caps[iid] = Decimal(cents) / Decimal(100)
    return caps, missing


def map_facts_to_snapshot_ids(facts, instrument_ids):
    """Map exact six-digit CNINFO IDs in memory; retain version/review identities.

    No issuer/name aliases or suffix matching is allowed. Prefixed exchange
    conflicts are rejected by the same A-share code contract as market caps.
    The append-only repository and its evidence remain unchanged.
    """
    canonical = {}
    for iid in instrument_ids:
        normalized = normalize_instrument_id(iid)
        if normalized in canonical:
            raise S2ReadinessError("snapshot has duplicate canonical security identities")
        canonical[normalized] = iid
    mapped = []
    for fact in facts:
        try:
            iid = canonical.get(normalize_instrument_id(fact.instrument_id))
        except DecisionMarketCapError:
            continue
        if iid is not None:
            mapped.append(replace(fact, instrument_id=iid))
    return mapped


def load_s2_target_period_map(path: str | Path) -> dict:
    """Read a frozen demand/source contract; it grants no fact acceptance."""
    source = resolve_artifact_path(path)
    map_body = source.read_bytes()
    raw = json.loads(map_body)
    if not isinstance(raw, dict) or raw.get("schema") != "aquant.s2_target_periods.v1":
        raise S2ReadinessError("target-period map schema mismatch")
    canonical_scope = [normalize_instrument_id(iid) for iid in raw["source_scope_ids"]]
    if len(canonical_scope) != len(set(canonical_scope)):
        raise S2ReadinessError("target-period source scope repeats canonical instrument identities")
    records = raw.get("records", [])
    record_ids = [normalize_instrument_id(row["instrument_id"]) for row in records]
    if len(record_ids) != len(set(record_ids)) or set(record_ids) != set(canonical_scope):
        raise S2ReadinessError("target-period records differ from declared source scope")
    mapped = {}
    for iid, period in raw.get("latest_period_ends", {}).items():
        canonical = normalize_instrument_id(iid)
        if canonical in mapped or canonical not in canonical_scope:
            raise S2ReadinessError("target-period map has ambiguous or undeclared security identity")
        parsed = date.fromisoformat(str(period))
        required_s2_periods(parsed)
        mapped[canonical] = parsed.isoformat()
    for record, iid in zip(records, record_ids):
        latest = record.get("latest_period_end")
        if (latest is not None or iid in mapped) and mapped.get(iid) != latest:
            raise S2ReadinessError("target-period mapping differs from per-security record")
        if record.get("index_observed_at") is not None:
            _parse_aware_datetime(record["index_observed_at"], "index_observed_at")
        elif record.get("discovery_status") == "LATEST_DISCOVERED_INDEX_COMPLETE":
            raise S2ReadinessError("discovered latest target must retain its real index_observed_at")
    source_documents = {}
    for artifact in raw.get("source_artifacts", []):
        body = resolve_artifact_path(artifact["path"]).read_bytes()
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if digest != artifact["content_hash"]:
            raise S2ReadinessError("target-period source artifact hash changed: " + artifact["role"])
        if artifact["role"] in {"priority_official_discovery", "other100_official_discovery", "other100_official_pdfs"}:
            if artifact["role"] in source_documents:
                raise S2ReadinessError("target-period contract repeats an official source role")
            source_documents[artifact["role"]] = json.loads(body)
        if artifact["role"] == "frozen_pool_instruments":
            frozen_ids = [normalize_instrument_id(row["instrument_id"]) for row in json.loads(body)]
            if len(frozen_ids) != len(set(frozen_ids)) or set(frozen_ids) != set(canonical_scope):
                raise S2ReadinessError("target-period declared scope differs from frozen source instruments")
    _verify_target_source_records(records, source_documents)
    return {**raw, "latest_period_ends": mapped,
            "source_scope_ids": canonical_scope, "path": str(source.resolve()),
            "content_hash": "sha256:" + hashlib.sha256(map_body).hexdigest()}


def _verify_target_source_records(records, sources):
    """Rebuild derived periods and observation times from hash-bound sources."""
    official_roles = {"priority_official_discovery", "other100_official_discovery"}
    if not official_roles.intersection(sources):
        if any(row.get("scope") in {"ORIGINAL_PRIORITY_20", "OTHER_100"} for row in records):
            raise S2ReadinessError("derived official target records lack their discovery source")
        return  # Explicit manual demand is not presented as official discovery.
    priority_source = sources.get("priority_official_discovery", {})
    other_source = sources.get("other100_official_discovery", {})
    def companies(source):
        rows = source.get("companies", [])
        result = {row["code"]: row for row in rows}
        if len(result) != len(rows):
            raise S2ReadinessError("official target source repeats a company code")
        return result
    priority, others = companies(priority_source), companies(other_source)
    if set(priority) & set(others):
        raise S2ReadinessError("priority and non-priority target sources overlap")
    archive_db = sources.get("other100_official_pdfs", {}).get("archiveDbPath")
    if archive_db is None:
        archive_db = Path(__file__).resolve().parents[3] / "deploy/agentctl-q0/forward-archive/meta.sqlite"
    priority_times = {}
    if priority:
        with closing(connect(resolve_artifact_path(archive_db), read_only=True)) as con:
            for code, company in priority.items():
                stamps = []
                for page in company.get("allCategoryPages", []) + company.get("regularReportPages", []):
                    receipt = con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?",
                        (page["responseReceiptId"],)).fetchone()
                    if (receipt is None or receipt["source_id"] != "cninfo" or receipt["outcome"] != "OK" or
                            receipt["content_hash"] != page["responseContentHash"]):
                        raise S2ReadinessError("official target observation receipt does not match its source")
                    stamps.append(_parse_aware_datetime(receipt["first_seen_at"], "index receipt first_seen_at"))
                if company.get("allCategoryComplete") and not stamps:
                    raise S2ReadinessError("complete official target source lacks index receipts")
                priority_times[code] = max(stamps) if stamps else None
    for record in records:
        code = normalize_instrument_id(record["instrument_id"]).split(".")[-1]
        is_priority = code in priority
        company = (priority if is_priority else others).get(code)
        latest = company.get("latestPeriodEnd") if company and company.get("allCategoryComplete") else None
        if record.get("latest_period_end") != latest:
            raise S2ReadinessError(f"{code} target period differs from the official discovery source")
        expected_scope = "ORIGINAL_PRIORITY_20" if is_priority else "OTHER_100"
        if record.get("scope") != expected_scope:
            raise S2ReadinessError(f"{code} target scope differs from the official discovery source")
        if bool(record.get("all_category_complete")) != bool(company and company.get("allCategoryComplete")):
            raise S2ReadinessError(f"{code} target completeness differs from the official discovery source")
        if latest is None:
            if record.get("index_observed_at") is not None:
                raise S2ReadinessError(f"{code} unknown target has an unsupported observation claim")
            continue
        observed = (priority_times[code] if is_priority else _parse_aware_datetime(
            company.get("allCategoryObservedAt") or company.get("finishedAt") or other_source.get("finishedAt"),
            "official source index observed_at"))
        if _parse_aware_datetime(record.get("index_observed_at"), "target index_observed_at") != observed:
            raise S2ReadinessError(f"{code} target observation time differs from the official source")
        required = required_s2_periods(date.fromisoformat(latest))
        expected = {metric: [period.isoformat() for period in periods] for metric, periods in required.items()}
        if (record.get("required_periods") != expected or
                record.get("required_metric_period_count") != sum(map(len, required.values()))):
            raise S2ReadinessError(f"{code} target financial demand differs from its actual discovered period")


def normalize_s2_target_periods(instrument_ids, *, latest_period_end=None,
                                latest_period_ends=None, cutoff=None,
                                target_period_records=()):
    """Resolve explicit per-security frozen demand without inferring disclosures.

    Missing overrides retain an explicitly supplied baseline. When neither is
    supplied the caller may report only the periods actually admitted at its
    cutoff; that is not a claim about the latest official disclosure.
    """
    canonical = {normalize_instrument_id(iid): iid for iid in instrument_ids}
    record_ids = [normalize_instrument_id(row["instrument_id"]) for row in target_period_records]
    if len(record_ids) != len(set(record_ids)):
        raise S2ReadinessError("target-period records repeat canonical instrument identities")
    records = dict(zip(record_ids, target_period_records))
    targets = {iid: latest_period_end for iid in instrument_ids}
    overrides = {}
    seen_source_ids = set()
    for raw_iid, raw_period in (latest_period_ends or {}).items():
        normalized = normalize_instrument_id(raw_iid)
        if normalized in seen_source_ids:
            raise S2ReadinessError(f"target-period map repeats canonical instrument: {normalized}")
        seen_source_ids.add(normalized)
        period = raw_period if isinstance(raw_period, date) else date.fromisoformat(str(raw_period))
        required_s2_periods(period)  # reject unsupported report endpoints
        iid = canonical.get(normalized)
        if iid is None:
            continue  # a verified shared source may legitimately cover a larger pool
        record = records.get(normalize_instrument_id(raw_iid), {})
        observed = record.get("index_observed_at")
        if cutoff is not None and observed is not None and _parse_aware_datetime(observed, "index_observed_at") > cutoff:
            continue  # future discovery cannot reinterpret an earlier frozen decision
        overrides[iid] = period
        targets[iid] = period
    if latest_period_end is not None:
        required_s2_periods(latest_period_end)
    return targets, overrides


def _demand_rows(store, ids, cutoff, accepted_reviews, target_periods, explicit_overrides):
    rows = []
    for iid in ids:
        try:
            selected = store.select_pit(cutoff, instrument_id=iid)
            selection_error = None
        except (PitViolation, ValueError) as exc:
            selected, selection_error = [], str(exc)
        explicit = target_periods[iid]
        target = explicit or max((f.period_end for f in selected), default=None)
        pairs = []
        if target is not None:
            for metric, periods in required_s2_periods(target).items():
                for period in periods:
                    scope = ("ATTRIBUTABLE" if metric == "net_profit_attributable" else
                             "CONSOLIDATED" if metric == "net_profit_consolidated" else None)
                    matches = [f for f in selected if f.metric == metric and f.period_end == period
                               and f.statement_scope.value == "CONSOLIDATED"
                               and (f.profit_scope.value if f.profit_scope else None) == scope
                               and f.currency == "CNY" and f.raw_unit == "yuan"]
                    status = ("VERSION_CHAIN_BLOCKED" if selection_error else
                              "FACT_MISSING" if not matches else
                              "FACT_AMBIGUOUS" if len(matches) != 1 else
                              "ACCEPTED" if matches[0].version_id in accepted_reviews else
                              "REVIEW_MISSING")
                    pairs.append({"metric": metric, "period_end": period.isoformat(),
                                  "status": status, "fact_version_ids": [f.version_id for f in matches]})
        rows.append({"instrument_id": iid, "target_period_end": target.isoformat() if target else None,
                     "target_period_basis": ("EXPLICIT_SECURITY_TARGET" if iid in explicit_overrides else
                                             "EXPLICIT_FROZEN_BASELINE" if explicit is not None else
                                             "AVAILABLE_FACTS_AT_CUTOFF" if target else "TARGET_UNKNOWN"),
                     "required_metric_period_count": len(pairs),
                     "accepted_metric_period_count": sum(p["status"] == "ACCEPTED" for p in pairs),
                     "metric_periods": pairs, "selection_error": selection_error})
    return rows


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
    allow_reconstructed: bool = False,
    latest_period_end: date | None = None,
    latest_period_ends: dict[str, date] | None = None,
    target_period_basis: str | None = None,
    target_period_records=(),
    market_caps_path: str | Path | None = None,
    market_caps_archive_root: str | Path | None = None,
    include_signals: bool = False,
) -> dict[str, object]:
    """Evaluate a published snapshot through the actual accepted-fact reader."""
    report, _, _ = _build_s2_readiness_report_and_inventory(
        snapshot_dir=snapshot_dir, snapshot_id=snapshot_id, facts_db=facts_db,
        allow_reconstructed=allow_reconstructed, latest_period_end=latest_period_end,
        latest_period_ends=latest_period_ends, target_period_basis=target_period_basis,
        target_period_records=target_period_records, market_caps_path=market_caps_path,
        market_caps_archive_root=market_caps_archive_root, include_signals=include_signals,
    )
    return report


def _build_s2_readiness_report_and_inventory(
    *, snapshot_dir: str | Path, snapshot_id: str, facts_db: str | Path,
    allow_reconstructed: bool = False,
    latest_period_end: date | None = None,
    latest_period_ends: dict[str, date] | None = None,
    target_period_basis: str | None = None,
    target_period_records=(),
    market_caps_path: str | Path | None = None,
    market_caps_archive_root: str | Path | None = None,
    include_signals: bool = False,
) -> tuple[dict[str, object], VersionedFinancialFactStore, dict]:
    """Evaluate coverage without creating databases, registering S2, or writing signals.

    ``allow_reconstructed=False`` (the default) reproduces the forward-only
    behaviour: only facts really observed by the cutoff can count.  Set it to
    ``True`` to additionally admit facts that carry a proven
    ``available_basis=RECONSTRUCTED`` claim, which is what a historical replay
    needs.  The two modes are reported separately so a reconstruction can never
    be mistaken for a forward observation.
    """

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
    caps, missing_caps = _market_caps(
        instruments, snapshot.trading_day, cutoff=ref.as_of_time,
        require_provenance=ref.data_mode != "SYNTHETIC")
    cap_evidence = {"mode": "FROZEN_SNAPSHOT", "sidecar_path": None,
                    "archive_root": None, "snapshot_values_count": len(caps)}
    if ref.data_mode != "SYNTHETIC":
        if market_caps_archive_root is None and market_caps_path is None:
            caps, missing_caps = {}, len(ids)
            cap_evidence["mode"] = "ARCHIVE_VERIFICATION_REQUIRED"
        elif market_caps_archive_root is not None and caps:
            verify_frozen_market_caps_archive(
                [row for row in instruments if row["instrument_id"] in caps],
                archive_root=market_caps_archive_root, expected_as_of=snapshot.trading_day,
                cutoff=ref.as_of_time)
            cap_evidence.update(mode="RECEIPT_VERIFIED_FROZEN_SNAPSHOT",
                                archive_root=str(market_caps_archive_root))
    if market_caps_path is not None:
        if market_caps_archive_root is None:
            raise S2ReadinessError("explicit market-cap sidecar requires archive_root")
        sidecar = load_sidecar(market_caps_path, expected_as_of=snapshot.trading_day,
                               archive_root=market_caps_archive_root)
        if sidecar.observed_at_dt > ref.as_of_time:
            raise S2ReadinessError("market-cap observation is after the frozen decision cutoff")
        for iid in ids:
            item = sidecar.items.get(normalize_instrument_id(iid))
            if item is not None:
                amount = Decimal(item.market_cap_cents) / Decimal(100)
                if iid in caps and caps[iid] != amount:
                    raise S2ReadinessError(f"{iid} sidecar conflicts with frozen market cap")
                caps[iid] = amount
        missing_caps = len(ids) - len(caps)
        cap_evidence.update(mode="RECEIPT_VERIFIED_SIDECAR", sidecar_path=str(market_caps_path),
                            archive_root=str(market_caps_archive_root), source_id=sidecar.source_id,
                            receipt_id=sidecar.receipt_id, content_hash=sidecar.content_hash,
                            observed_at=sidecar.observed_at, market_cap_as_of=sidecar.market_cap_as_of)
    templates = _industry_templates(instruments)
    target_periods, explicit_overrides = normalize_s2_target_periods(
        ids, latest_period_end=latest_period_end, latest_period_ends=latest_period_ends,
        cutoff=ref.as_of_time, target_period_records=target_period_records)
    target_record_by_id = {normalize_instrument_id(row["instrument_id"]): row for row in target_period_records}
    canonical_ids = {normalize_instrument_id(iid) for iid in ids}
    source_target_ids = {normalize_instrument_id(iid) for iid in (latest_period_ends or {})}
    source_scope_ids = set(target_record_by_id) | source_target_ids

    all_store, accepted_reviews = repository.load_with_accepted_s2_pdf_reviews()
    all_facts = all_store.facts
    pool_facts = map_facts_to_snapshot_ids(all_facts, ids)
    observed_facts, rejected_facts = admissible_facts(
        facts=pool_facts, cutoff=ref.as_of_time,
        allow_reconstructed=allow_reconstructed,
    )
    reconstruction_rejections = Counter(reason for _, reason in rejected_facts)
    store = VersionedFinancialFactStore(observed_facts)
    demand = _demand_rows(store, ids, ref.as_of_time, accepted_reviews, target_periods,
                          explicit_overrides)
    instruments_by_id = {row["instrument_id"]: row for row in instruments}
    for row in demand:
        iid = row["instrument_id"]
        frozen = instruments_by_id[iid]
        discovery = target_record_by_id.get(normalize_instrument_id(iid))
        if discovery is not None:
            stamp = discovery.get("index_observed_at")
            row["target_discovery"] = {**discovery,
                "effective_at_cutoff": discovery.get("latest_period_end") is not None and
                (stamp is None or _parse_aware_datetime(stamp, "index_observed_at") <= ref.as_of_time)}
        row["market_cap"] = {
            "status": "AVAILABLE" if iid in caps else "MISSING_OR_UNVERIFIED",
            "value_yuan": str(caps[iid]) if iid in caps else None,
            "as_of": snapshot.trading_day if iid in caps else None,
            **{name: frozen.get("market_cap_" + name) for name in
               ("source_id", "receipt_id", "content_hash", "observed_at")}}
        if market_caps_path is not None and normalize_instrument_id(iid) in sidecar.items:
            row["market_cap"].update({name: getattr(sidecar, name) for name in
                                     ("source_id", "receipt_id", "content_hash", "observed_at")})
        row["financial_inputs"] = None
        try:
            bridge = compute_s2_financial_inputs(store=store, instrument_id=iid,
                cutoff=ref.as_of_time, industry_template=templates[iid])
        except (S2ComputationError, PitViolation, ValueError) as exc:
            row["financial_input_status"] = getattr(exc, "code", "S2_INPUT_INVALID")
            row["financial_input_reason"] = str(exc)
        else:
            row["financial_inputs"] = bridge.as_dict()
            row["financial_input_status"] = (
                "S2_TARGET_PERIOD_MISMATCH" if target_periods[iid] is not None and
                bridge.latest_period_end != target_periods[iid] else
                "ACCEPTED" if all(version in accepted_reviews for version in bridge.fact_version_ids)
                else "S2_REVIEW_MISSING")
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
        (target_periods[item.instrument_id] is None or
         item.factors.latest_period_end == target_periods[item.instrument_id]) and
        all(version in accepted_reviews for version in item.factors.fact_version_ids)
    ]
    # The accepted pool alone defines ranks; unreviewed formula-complete rows
    # must not influence any returned formal cross-sectional signal.
    complete = build_s2_signals(store=store, instrument_ids=[item.instrument_id for item in complete],
                                cutoff=ref.as_of_time, market_cap_by_instrument=caps,
                                industry_template_by_instrument=templates)
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
        elif target_periods[item.instrument_id] is not None and item.factors.latest_period_end != target_periods[item.instrument_id]:
            exclusions.append({"instrument_id": item.instrument_id,
                               "code": "S2_TARGET_PERIOD_MISMATCH",
                               "reason": "selected formula period differs from explicit frozen demand"})
    counts = Counter(item["code"] for item in exclusions)
    exclusion_by_id = {item["instrument_id"]: item for item in exclusions}
    for row in demand:
        excluded = exclusion_by_id.get(row["instrument_id"])
        row["factor_exclusion_code"] = excluded["code"] if excluded else None
        row["factor_exclusion_reason"] = excluded["reason"] if excluded else None
    evidence = [
        {"instrument_id": item.instrument_id,
         "factors": item.factors.as_dict(),
         "fact_version_ids": list(item.factors.fact_version_ids),
         "review_bundles": [
             {"bundle_id": bundle_id, "evidence_hash": evidence_hash}
             for bundle_id, evidence_hash in sorted({
                 accepted_reviews[version] for version in item.factors.fact_version_ids
             })
         ]}
        for item in complete
    ]
    report = {
        "schema_version": "aquant.s2_readiness.v1",
        "snapshot_dir": str(root),
        "snapshot_id": sid,
        "decision_cutoff_at": ref.as_of_time.isoformat(),
        "trading_day": snapshot.trading_day,
        "data_mode": ref.data_mode,
        "facts_db": str(Path(facts_db)),
        "strategy_family_status": "CLOSED",
        "scope_status": ("READY" if complete and len(complete) == len(ids) else
                         "PARTIAL_READY" if complete else "CLOSED"),
        "registration_gate": "SEPARATE_ACCEPTANCE_REQUIRED",
        "pool_size": len(ids),
        "complete_s2_count": len(complete),
        "coverage_ratio": len(complete) / len(ids) if ids else 0.0,
        "missing_market_cap_count": missing_caps,
        "market_cap_evidence": cap_evidence,
        "market_cap_missing_instrument_ids": [iid for iid in ids if iid not in caps],
        "target_period_end": latest_period_end.isoformat() if latest_period_end else None,
        "latest_period_ends": {iid: period.isoformat() for iid, period in sorted(explicit_overrides.items())},
        "target_period_basis": target_period_basis,
        "target_map_projection": {
            "source_target_count": len(source_target_ids),
            "source_scope_count": len(source_scope_ids),
            "unused_source_ids": sorted(source_scope_ids - canonical_ids),
            "snapshot_ids_without_source_scope": [iid for iid in ids if normalize_instrument_id(iid) not in source_scope_ids],
            "snapshot_ids_without_source_target": [iid for iid in ids if normalize_instrument_id(iid) not in source_target_ids],
            "future_observed_target_ids": [row["instrument_id"] for row in demand
                if row.get("target_discovery", {}).get("index_observed_at") and
                row.get("target_discovery", {}).get("effective_at_cutoff") is False],
            "discovery_pending_ids": [row["instrument_id"] for row in demand
                if row.get("target_discovery", {}).get("discovery_status") == "DISCOVERY_PENDING"],
        },
        "required_metric_period_count": sum(row["required_metric_period_count"] for row in demand),
        "accepted_metric_period_count": sum(row["accepted_metric_period_count"] for row in demand),
        "securities_without_target_period": sum(row["target_period_end"] is None for row in demand),
        "security_demand": demand,
        "eligible_scope": {
            "requested_pool_count": len(ids), "requested_pool_ids": list(ids),
            "formula_eligible_ids": [iid for iid in ids if templates[iid] == "ORDINARY"],
            "formula_eligible_count": sum(templates[iid] == "ORDINARY" for iid in ids),
            "unsupported_template_ids": [iid for iid in ids if templates[iid] == "FINANCIAL"],
            "unsupported_template_count": sum(templates[iid] == "FINANCIAL" for iid in ids),
            "unknown_template_ids": [iid for iid in ids if templates[iid] == "UNKNOWN"],
            "complete_financial_input_ids": [row["instrument_id"] for row in demand if row["financial_input_status"] == "ACCEPTED"],
            "complete_financial_input_count": sum(row["financial_input_status"] == "ACCEPTED" for row in demand),
            "financial_denominator_exclusions": {row["instrument_id"]: row["financial_inputs"]["non_positive_denominator_factors"]
                for row in demand if row["financial_inputs"] and row["financial_inputs"]["non_positive_denominator_factors"]},
            "full_factor_ready_ids": [item.instrument_id for item in complete],
            "full_factor_ready_count": len(complete),
            "ranking_pool_ids": [item.instrument_id for item in complete],
            "ranking_pool_count": len(complete),
            "excluded_ids": [item["instrument_id"] for item in exclusions],
            "excluded_count": len(exclusions),
            "unknown_target_ids": [row["instrument_id"] for row in demand if row["target_period_end"] is None],
            "unknown_target_count": sum(row["target_period_end"] is None for row in demand),
        },
        "exclusion_counts": dict(sorted(counts.items())),
        "exclusions": exclusions,
        "fact_versions_in_repository": len(all_facts),
        "fact_versions_in_pool": len(pool_facts),
        "fact_versions_admitted": len(observed_facts),
        "fact_versions_first_seen_after_cutoff": sum(fact.first_seen_at > ref.as_of_time for fact in pool_facts),
        "fact_versions_rejected": len(rejected_facts),
        "reconstruction_admitted": allow_reconstructed,
        "reconstruction_rejection_counts": dict(sorted(reconstruction_rejections.items())),
        "fact_versions_without_accepted_review": sum(
            fact.version_id not in accepted_reviews for fact in observed_facts),
        "engine_reported_fact_versions": len(used_facts),
        "accepted_review_bundles_used": len({
            accepted_reviews[version][0] for version in used_ids}),
        "source_documents_used": len({(fact.source_id, fact.source_document_id)
                                      for fact in used_facts}),
        "content_hashes_used": len({fact.content_hash for fact in used_facts}),
        "evidence": evidence,
        **({"signals": [item.signal.as_dict() for item in complete],
            "ranking_pool_size": len(complete)} if include_signals else {}),
    }
    # Coverage and factor bridges must use this same accepted inventory. A
    # second repository read could observe new commits during raw PDF replay.
    return report, all_store, accepted_reviews


__all__ = ["S2ReadinessError", "admissible_facts", "build_s2_readiness_report",
           "map_facts_to_snapshot_ids", "normalize_s2_target_periods", "load_s2_target_period_map"]
