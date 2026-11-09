"""Reader for the bounded combined H125-root/H126-PRIOR CFO review."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from ...domain.data.forward_archive import ForwardArchive
from ...domain.data.pit import date_only_available_at
from ...domain.fundamentals.versioned import FinancialFact
from .cashflow_version_chain import (
    ANNUAL_ID, CORRECTIONS, ORIGINAL_ID, PRESENTING_ID, SCHEMA, VERIFIER,
    _check_current_relationship_scope, _corrected_h1_source_proof, _digest,
    _next_available_snapshot, canonical,
)
from .comparative_cashflow import (
    derive_h125_cashflow_version_relation, extract_comparative_cashflow_contract,
)
from .cninfo_statement_sections import extract_section_facts
from .s2_machine_verification import verify_machine_evidence


class _ExistingArchive(ForwardArchive):
    """Read an existing receipt archive without constructor schema writes."""
    def __init__(self, con: sqlite3.Connection, root: Path):
        self.con, self.root, self.blob_dir = con, root, root / "raw"
        if not root.is_dir() or not self.blob_dir.is_dir():
            raise ValueError("existing archive is missing")


def accepted_cashflow_version_chain(
    row: Mapping, member_ids: set[str], facts: Mapping[str, FinancialFact],
    accepted_ordinary: Mapping[str, tuple[str, str]], reviews_by_id: Mapping[str, Mapping],
) -> bool:
    """Validate all three real members, dependent annual proof and raw columns.

    ``accepted_ordinary`` comes from the same public reader's already replayed
    legacy reviews. It is not a persisted PASS flag. A missing or invalid annual
    review never supplies the H1 version bridge.
    """
    from ...domain.fundamentals.fact_repository import (
        _accepted_machine_verified_review, _load_native_parent_equity_payload,
    )
    try:
        evidence = json.loads(row["evidence_json"])
        if (canonical(evidence) != row["evidence_json"] or
                _digest(row["evidence_json"]) != row["evidence_hash"] or
                evidence.get("schema") != SCHEMA or evidence.get("verifierId") != VERIFIER or
                evidence.get("humanReviewClaimed") is not None or
                evidence.get("instrumentId") != "601069" or
                evidence.get("metric") != "operating_cashflow" or
                not evidence.get("rightsRegisterVersion")):
            return False
        ids = evidence.get("versionIds")
        if (not isinstance(ids, dict) or
                set(ids) != {"H125_ORIGINAL", "H125_PRESENTED_PRIOR", "H126_CURRENT"} or
                len(set(ids.values())) != 3 or set(ids.values()) != member_ids or
                not member_ids <= facts.keys()):
            return False
        root, prior, current = (facts[ids[key]] for key in
                                ("H125_ORIGINAL", "H125_PRESENTED_PRIOR", "H126_CURRENT"))
        expected_bundle = "cninfo_machine_cfo_chain_" + hashlib.sha256(
            f"601069|{ORIGINAL_ID}|{PRESENTING_ID}|{root.version_id}|{prior.version_id}|{current.version_id}".encode()).hexdigest()[:32]
        if row["bundle_id"] != expected_bundle:
            return False
        if (any(fact.availability_basis.value != "OBSERVED" or fact.pit_mode.value != "LIVE_OBSERVED"
                for fact in (root, prior, current)) or
                root.supersedes_id is not None or current.supersedes_id is not None or
                prior.supersedes_id != root.version_id or prior.logical_key != root.logical_key or
                root.period_end != date(2025, 6, 30) or prior.period_end != date(2025, 6, 30) or
                current.period_end != date(2026, 6, 30) or
                root.source_document_id != ORIGINAL_ID or
                prior.source_document_id != PRESENTING_ID or current.source_document_id != PRESENTING_ID or
                any(fact.version_id != prior.version_id and fact.supersedes_id == root.version_id
                    for fact in facts.values())):
            return False
        dependency = evidence.get("annualDependency")
        if not isinstance(dependency, dict):
            return False
        annual_version = dependency.get("factVersionId")
        annual_fact = facts.get(annual_version)
        annual_link = (dependency.get("reviewBundleId"), dependency.get("reviewEvidenceHash"))
        annual_row = reviews_by_id.get(annual_link[0])
        if (annual_fact is None or annual_row is None or
                not isinstance(annual_link[0], str) or not annual_link[0].startswith("cninfo_machine_") or
                accepted_ordinary.get(annual_version) != annual_link or
                annual_row["evidence_hash"] != annual_link[1] or
                annual_fact.instrument_id != "601069" or annual_fact.metric != "operating_cashflow" or
                annual_fact.period_end != date(2025, 12, 31) or annual_fact.source_document_id != ANNUAL_ID or
                dependency.get("acceptedFactIngestedAt") != annual_fact.ingested_at.isoformat() or
                dependency.get("acceptedFactAvailableAt") != annual_fact.available_at.isoformat() or
                dependency.get("acceptedFactPublishedOn") != annual_fact.source_published_date.isoformat() or
                not root.source_published_date < annual_fact.source_published_date < current.source_published_date):
            return False
        annual_evidence = json.loads(annual_row["evidence_json"])
        if (annual_evidence.get("instrument_id") != "601069" or
                annual_evidence.get("period_end") != "2025-12-31" or
                annual_evidence.get("announcement_id") != ANNUAL_ID):
            return False
        annual_impact = annual_evidence.get("revision_impact_proof")
        if not isinstance(annual_impact, dict):
            return False
        reports = evidence.get("reports")
        if (not isinstance(reports, list) or len(reports) != 2 or
                {item.get("announcement_id") for item in reports if isinstance(item, dict)} !=
                {ORIGINAL_ID, PRESENTING_ID}):
            return False
        by_report = {item["announcement_id"]: item for item in reports}
        raw_payloads = {}
        for fact in (root, current):
            component = by_report[fact.source_document_id]
            # Reuse the ordinary protocol's exact source/value/independent-path
            # validation. This local representation is not a persisted old
            # review and is never returned as an accepted review link.
            component_text = canonical(component)
            component_id = "cninfo_machine_fields_" + hashlib.sha256(
                f"601069|{fact.period_end}|{fact.source_document_id}|operating_cashflow".encode()).hexdigest()[:32]
            local = {"bundle_id": component_id, "evidence_hash": _digest(component_text),
                     "evidence_json": component_text}
            if (component.get("announcement_role") != "ORIGINAL_REPORT" or
                    "revision_impact_proof" in component or "revision_impact_archive" in component or
                    not _accepted_machine_verified_review(local, {fact.version_id}, facts)):
                return False
            raw = _load_native_parent_equity_payload(
                fact, component, archive_key="cashflow_chain_source_archive")
            if raw is None:
                return False
            candidate = extract_section_facts(
                raw, instrument_id="601069", company="西部黄金股份有限公司",
                period_end=fact.period_end, announcement_id=fact.source_document_id,
                document_url=component["document_url"], required_fields=("operating_cashflow",))
            verification = verify_machine_evidence(
                raw, candidate=candidate, expected_fields=("operating_cashflow",))
            if (not verification.verified or
                    candidate.by_field["operating_cashflow"].value_yuan != Decimal(str(fact.value)) or
                    [item.as_dict() for item in verification.fields] != component["fields"] or
                    list(verification.sections) != component["sections"] or
                    [item.as_dict() for item in verification.identities] != component["identities"]):
                return False
            raw_payloads[fact.source_document_id] = raw
            dispositions = component.get("cross_category_index", {}).get("candidate_dispositions")
            if not isinstance(dispositions, list):
                return False
            correction_items = {item["announcement_id"]: item for item in dispositions
                                if isinstance(item, dict) and item.get("announcement_id") in CORRECTIONS}
            if (set(correction_items) != CORRECTIONS or
                    any(item.get("relevance") != "RELATED" or
                        not str(item.get("reviewer_id", "")).startswith("machine:")
                        for item in correction_items.values())):
                return False
        newer = by_report[PRESENTING_ID]
        payload = raw_payloads[PRESENTING_ID]
        comparative = extract_comparative_cashflow_contract(
            payload, instrument_id="601069", source_report_period_end=date(2026, 6, 30))
        if comparative.as_evidence() != evidence.get("comparative"):
            return False
        relation = derive_h125_cashflow_version_relation(
            annual_impact, original_h125_amount=Decimal(str(root.value)),
            h126_prior_amount=comparative.by_role("PRIOR").amount_yuan)
        prior_identity = f"cninfo|601069|2026-06-30|2025-06-30|PRIOR|operating_cashflow|{PRESENTING_ID}|{current.content_hash}"
        if (prior.version_id != "mach_cmp_" + hashlib.sha256(prior_identity.encode()).hexdigest()[:32] or
                Decimal(str(prior.value)) != comparative.by_role("PRIOR").amount_yuan or
                Decimal(str(current.value)) != comparative.by_role("CURRENT").amount_yuan or
                prior.instrument_id != "601069" or prior.metric != "operating_cashflow" or
                prior.currency != "CNY" or prior.raw_unit != "yuan" or
                prior.statement_scope.value != "CONSOLIDATED" or prior.profit_scope is not None or
                prior.source_id != "cninfo" or prior.content_hash != current.content_hash or
                prior.source_published_date != current.source_published_date or
                prior.source_published_at is not None or prior.timestamp_precision.value != "DATE" or
                prior.first_seen_at != current.first_seen_at or
                prior.availability_basis.value != "OBSERVED" or prior.pit_mode.value != "LIVE_OBSERVED"):
            return False
        ingested, available = datetime.fromisoformat(evidence["ingestedAt"]), datetime.fromisoformat(evidence["availableAt"])
        calendar = tuple(date.fromisoformat(day) for day in evidence["tradingCalendar"])
        floors = [date_only_available_at(fact.source_published_date, calendar)[0] for fact in (root, current)]
        observed = [fact.first_seen_at for fact in (root, current)]
        review_times = [datetime.fromisoformat(item["cross_category_index"]["reviewed_at"]) for item in reports]
        if (ingested.tzinfo is None or available.tzinfo is None or
                ingested > datetime.now(timezone.utc) or
                any(fact.ingested_at != ingested or fact.available_at != available for fact in (root, prior, current)) or
                max(*observed, *review_times) > ingested or
                available != _next_available_snapshot(
                    max(ingested, annual_fact.available_at, *floors, *observed, *review_times), calendar)):
            return False
        descriptor = newer.get("cashflow_chain_source_archive")
        older_descriptor = by_report[ORIGINAL_ID].get("cashflow_chain_source_archive")
        if (not isinstance(descriptor, dict) or not isinstance(older_descriptor, dict) or
                descriptor.get("root") != older_descriptor.get("root") or
                descriptor.get("database_path") != older_descriptor.get("database_path")):
            return False
        archive_root = Path(descriptor["root"]).resolve()
        database = Path(descriptor["database_path"]).resolve()
        if database != archive_root / "meta.sqlite":
            return False
        con = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA query_only=ON")
            con.execute("BEGIN")
            scope = _check_current_relationship_scope(
                _ExistingArchive(con, archive_root), evidence["currentRelationshipCompany"], annual_impact)
            if scope != evidence.get("currentRelationshipScope") or con.total_changes:
                return False
            corrected = _corrected_h1_source_proof(_ExistingArchive(con, archive_root), scope)
            if Decimal(corrected["nativeProof"]["current_amount_yuan"]) != Decimal(str(root.value)):
                return False
            relation["correctedH1FullStatement"] = corrected
            if relation != evidence.get("versionRelation"):
                return False
            source_bindings = {item["announcementId"]: item for item in scope["originalSources"]}
            for fact in (root, current):
                binding = source_bindings.get(fact.source_document_id)
                if (binding is None or binding["contentHash"] != fact.content_hash or
                        binding["receiptId"] != by_report[fact.source_document_id]["pdf_receipt_id"]):
                    return False
            for component in reports:
                if (scope["indexReceiptIds"] !=
                        [component["org_lookup_receipt_id"], *component["cross_category_page_receipt_ids"]] or
                        scope["requestBodyHashes"] != component["cross_category_page_request_hashes"] or
                        evidence["currentRelationshipCompany"]["publicationWindow"] !=
                        component["cross_category_publication_window"]):
                    return False
        finally:
            if con.in_transaction:
                con.execute("ROLLBACK")
            con.close()
        return True
    except (KeyError, TypeError, ValueError, AttributeError, OSError, sqlite3.Error, ArithmeticError):
        return False
