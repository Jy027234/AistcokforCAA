"""Finite 000680 H125 parent-equity CURRENT -> H126 presented-prior chain.

This private candidate preserves two native observations of the same
2025-06-30 consolidated parent-equity fact. It reuses the current H125 profit
pair's issuer/index/receipt and 359-row relationship replay, while delegating
both equity-table extractions to ``parent_equity_successor_compare``. The
changed value is a version-impact finding only: it is not admitted, and no
single notice is assigned causal responsibility.

The module is intended to be copied into ``aquant.adapters.providers`` only
after the central fact-repository reader registers its schema and prefix.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from aquant.domain.data.pit import (
    AvailabilityBasis,
    PitMode,
    TimestampPrecision,
    date_only_available_at,
)
from aquant.domain.data.rights import REGISTER_VERSION, default_rights
from aquant.domain.fundamentals.fact_repository import (
    FactReviewBundle,
    FinancialFactRepository,
)
from aquant.domain.fundamentals.versioned import FinancialFact, StatementScope


SCHEMA = "aquant.cninfo.000680-h125-parent-equity-presented-prior-chain.v1"
VERIFIER_ID = "machine:cninfo-000680-h125-parent-equity-chain-v1"
INSTRUMENT_ID = "000680"
H125 = date(2025, 6, 30)
H126 = date(2026, 6, 30)
METRIC = "parent_equity"
CURRENT_VALUE_YUAN = Decimal("5676406977.01")
PRESENTED_PRIOR_VALUE_YUAN = Decimal("5679942158.29")
DELTA_YUAN = Decimal("3535181.28")
PREFIX = "cninfo_machine_000680_h125_parent_equity_"


class H125ParentEquityPairContractError(ValueError):
    """A source, native-cell, relationship, PIT, or lineage contract failed."""


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _aware(value: str | datetime, label: str) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise H125ParentEquityPairContractError(f"{label} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _decimal(value: object, label: str) -> Decimal:
    try:
        result = Decimal(str(value).replace(",", "").strip())
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise H125ParentEquityPairContractError(f"{label} is not a decimal CNY amount") from exc
    if not result.is_finite():
        raise H125ParentEquityPairContractError(f"{label} is not finite")
    return result


def _cell_amount(cell: Mapping, label: str) -> Decimal:
    value = cell.get("amountYuan")
    if value is None:
        raise H125ParentEquityPairContractError(f"{label} native cell has no yuan value")
    return _decimal(value, label)


def _cell_page(cell: Mapping, label: str) -> int:
    value = cell.get("page")
    if type(value) is not int or value < 1:
        raise H125ParentEquityPairContractError(f"{label} native cell has no physical PDF page")
    return value


def _cell_label(cell: Mapping, label: str) -> str:
    value = cell.get("rowLabel")
    if not isinstance(value, str) or not value.strip():
        raise H125ParentEquityPairContractError(f"{label} native cell has no row label")
    return value.strip()


def _bbox(value: object, label: str) -> tuple[float, float, float, float]:
    if (not isinstance(value, (list, tuple)) or len(value) != 4 or
            any(type(part) not in (int, float) for part in value)):
        raise H125ParentEquityPairContractError(f"{label} is not a native four-coordinate box")
    parts = tuple(float(part) for part in value)
    if (not all(part == part and abs(part) != float("inf") for part in parts) or
            parts[2] <= parts[0] or parts[3] <= parts[1]):
        raise H125ParentEquityPairContractError(f"{label} is not a finite positive box")
    return parts


def _validate_parent_equity_table_proof(
    proof: Mapping, amount: Decimal, *, source_period: date,
    presentation: str, label: str, outer_cell: Mapping | None = None,
) -> None:
    expected = {
        "period_end": H125.isoformat(),
        "source_report_period_end": source_period.isoformat(),
        "presentation": presentation,
        "unit": "元",
        "parent_group_header": "归属于母公司股东权益",
        "subtotal_header": "小计",
        "closing_row_label": "四、本期期末余额",
    }
    if any(proof.get(key) != value for key, value in expected.items()):
        raise H125ParentEquityPairContractError(f"{label} proof is not the H125 parent-equity closing subtotal")
    if (_decimal(proof.get("amount_yuan"), f"{label} proof amount") != amount or
            _decimal(proof.get("native_cell_text"), f"{label} native glyph amount") != amount or
            proof.get("bbox_tolerance_points") != 1.0):
        raise H125ParentEquityPairContractError(f"{label} native cell amount/geometry tolerance differs")
    header = _bbox(proof.get("header_subtotal_bbox"), f"{label} parent subtotal header")
    value_cell = _bbox(proof.get("closing_cell_bbox"), f"{label} closing amount cell")
    row = _bbox(proof.get("closing_row_bbox"), f"{label} closing row label")
    if (abs(header[0] - value_cell[0]) > 1.0 or
            abs(header[2] - value_cell[2]) > 1.0 or
            value_cell[1] > row[1] + 1.0 or
            value_cell[3] < row[3] - 1.0):
        raise H125ParentEquityPairContractError(f"{label} parent subtotal geometry is not row/column aligned")
    if (type(proof.get("closing_row_page")) is not int or proof["closing_row_page"] < 1 or
            type(proof.get("report_table_page")) is not int or proof["report_table_page"] < 1 or
            "合并所有者权益变动表" not in str(proof.get("report_table_title", "")) or
            "2025" not in str(proof.get("period_header", ""))):
        raise H125ParentEquityPairContractError(f"{label} native table page/title/period identity differs")
    if outer_cell is not None and (
            proof.get("closing_row_page") != outer_cell.get("page") or
            proof.get("report_table_page") != outer_cell.get("headerPage")):
        raise H125ParentEquityPairContractError(f"{label} native page binding differs from its outer cell")


def _validate_current_balance_sheet_proof(cell: Mapping, amount: Decimal) -> None:
    proof = cell.get("proof")
    if not isinstance(proof, Mapping):
        raise H125ParentEquityPairContractError("H125 CURRENT lacks native balance-sheet proof")
    if (proof.get("path_kind") != "native_consolidated_balance_sheet_current_parent_equity_row" or
            proof.get("fact_period_end") != H125.isoformat() or
            proof.get("statement") != "合并资产负债表" or
            proof.get("statement_scope") != "CONSOLIDATED" or
            proof.get("column_role") != "CURRENT" or
            proof.get("current_header") != "期末余额" or
            proof.get("unit") != "元" or
            proof.get("row_label") != cell.get("rowLabel") or
            proof.get("page") != cell.get("page") or
            proof.get("source_line") != cell.get("sourceLine") or
            _decimal(proof.get("amount_yuan"), "H125 balance-sheet proof amount") != amount):
        raise H125ParentEquityPairContractError("H125 CURRENT balance-sheet native proof differs")
    same_report = cell.get("sameReportEquityChangeTable")
    if not isinstance(same_report, Mapping):
        raise H125ParentEquityPairContractError("H125 CURRENT lacks its same-report equity-change cross-check")
    _validate_parent_equity_table_proof(
        same_report, amount, source_period=H125, presentation="CURRENT",
        label="H125 same-report equity-change cross-check",
    )


def _report_bindings(base) -> tuple[dict, dict]:
    original = dict(base.H125_REPORT)
    presenting = dict(base.H126_REPORT)
    if (original.get("announcementId") != "1224561048" or
            original.get("publishedDate") != "2025-08-23" or
            presenting.get("announcementId") != "1225466047" or
            presenting.get("publishedDate") != "2026-08-11"):
        raise H125ParentEquityPairContractError("pinned H125/H126 source identity differs")
    return original, presenting


def _validate_native_comparison(pair: Mapping) -> tuple[Decimal, Decimal, dict, dict]:
    """Validate the shared native parser result and return its two source cells."""
    if not isinstance(pair, Mapping):
        raise H125ParentEquityPairContractError("shared parent-equity parser returned no pair")
    current = pair.get("current")
    prior = pair.get("presentedPrior")
    if not isinstance(current, Mapping) or not isinstance(prior, Mapping):
        raise H125ParentEquityPairContractError("shared parser pair lacks CURRENT or PRESENTED_PRIOR cell")

    current_amount = _cell_amount(current, "H125 CURRENT")
    prior_amount = _cell_amount(prior, "H126 presented H125 PRIOR")
    _validate_current_balance_sheet_proof(current, current_amount)
    _validate_parent_equity_table_proof(
        prior.get("proof", {}), prior_amount,
        source_period=H126, presentation="PRESENTED_PRIOR",
        label="H126 presented H125 PRIOR", outer_cell=prior,
    )
    delta = _decimal(pair.get("delta_yuan"), "pair delta")
    same = pair.get("same_value")
    candidate = pair.get("candidate")
    cause = pair.get("causal_attribution")
    admission = pair.get("admission")
    if (current_amount != CURRENT_VALUE_YUAN or
            prior_amount != PRESENTED_PRIOR_VALUE_YUAN or
            delta != DELTA_YUAN or prior_amount - current_amount != delta or
            same is not False or candidate != "AFFECTS_VALUE_CHANGE" or
            cause not in ("UNRESOLVED", None) or admission != "NOT_ADMITTED"):
        raise H125ParentEquityPairContractError("native same-fact pair or impact boundary differs")
    if current_amount == prior_amount:
        raise H125ParentEquityPairContractError("AFFECTS candidate unexpectedly has equal values")
    return current_amount, prior_amount, dict(current), dict(prior)


def _enriched_event(*, parsed: Mapping, report: Mapping, receipt: Mapping,
                    source_period: date, fact_period: date, role: str) -> dict:
    amount = _cell_amount(parsed, role)
    page = _cell_page(parsed, role)
    row_label = _cell_label(parsed, role)
    expected_role = "CURRENT" if role == "ORIGINAL_CURRENT" else "PRESENTED_PRIOR"
    # The parser is authoritative for native geometry and cell text; this
    # wrapper binds those cells to the current official index/PDF receipt.
    dimensions = {
        "sourceReportPeriodEnd": source_period.isoformat(),
        "factPeriodEnd": fact_period.isoformat(),
        "columnRole": expected_role,
        "statementScope": "CONSOLIDATED",
        "currency": "CNY",
        "amountUnit": "元",
        "rawUnit": "元",
        "pdfSha256": report["contentHash"],
    }
    for key, expected in dimensions.items():
        actual = parsed.get(key)
        # `sourceReportPeriodEnd` and `factPeriodEnd` are part of the shared
        # parser contract; do not infer the comparator date from report year.
        if actual != expected:
            raise H125ParentEquityPairContractError(f"{role} native parser {key} differs")
    header = parsed.get("columnHeader")
    if not isinstance(header, str) or not header.strip():
        raise H125ParentEquityPairContractError(f"{role} native cell has no literal column header")
    source_line = parsed.get("sourceLine")
    native_proof = parsed.get("proof")
    header_page = parsed.get("headerPage")
    native_chars = parsed.get("nativeChars")
    expected_statement = ("合并资产负债表" if role == "ORIGINAL_CURRENT"
                          else "合并所有者权益变动表")
    if (parsed.get("announcementId") != report["announcementId"] or
            parsed.get("statement") != expected_statement or
            parsed.get("pdfSha256") != report["contentHash"] or
            type(header_page) is not int or header_page < 1 or
            type(native_chars) is not int or native_chars < 1 or
            not isinstance(source_line, str) or not source_line.strip() or
            not isinstance(native_proof, Mapping) or not native_proof):
        raise H125ParentEquityPairContractError(f"{role} native cell lacks source-line or geometry proof")
    return {
        **dict(parsed),
        "announcementId": report["announcementId"],
        "receiptId": receipt["receipt_id"],
        "contentHash": report["contentHash"],
        "documentUrl": report["documentUrl"],
        "publishedDate": report["publishedDate"],
        "firstSeenAt": receipt["first_seen_at"],
        "sourceReportPeriodEnd": source_period.isoformat(),
        "factPeriodEnd": fact_period.isoformat(),
        "columnRole": expected_role,
        "columnHeader": header.strip(),
        "statement": parsed.get("statement"),
        "statementScope": "CONSOLIDATED",
        "currency": "CNY",
        "amountUnit": "元",
        "rawUnit": "元",
        "factRawUnit": "yuan",
        "amountYuan": str(amount),
        "page": page,
        "rowLabel": row_label,
        "sourceLine": source_line,
        "nativeProof": dict(native_proof),
        "headerPage": header_page,
        "nativeChars": native_chars,
    }


def _version_id(role: str, announcement_id: str, content_hash: str) -> str:
    identity = f"{INSTRUMENT_ID}|{H125.isoformat()}|{METRIC}|{role}|{announcement_id}|{content_hash}"
    return "mach_000680_h125_parent_equity_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _events_and_facts(pair: Mapping, *, original: Mapping, presenting: Mapping,
                      original_receipt: Mapping, presenting_receipt: Mapping,
                      ingested_at: datetime, available_at: datetime) -> tuple[tuple[FinancialFact, FinancialFact], list[dict]]:
    current_amount, prior_amount, current_cell, prior_cell = _validate_native_comparison(pair)
    current = _enriched_event(parsed=current_cell, report=original, receipt=original_receipt,
                              source_period=H125, fact_period=H125, role="ORIGINAL_CURRENT")
    prior = _enriched_event(parsed=prior_cell, report=presenting, receipt=presenting_receipt,
                            source_period=H126, fact_period=H125, role="PRESENTED_PRIOR")
    if (_cell_amount(current, "bound H125 CURRENT") != current_amount or
            _cell_amount(prior, "bound H126 PRIOR") != prior_amount):
        raise H125ParentEquityPairContractError("bound event values differ from native comparison")

    facts = []
    events = []
    predecessor = None
    for role, event in (("ORIGINAL_CURRENT", current), ("PRESENTED_PRIOR", prior)):
        source_id = event["announcementId"]
        content_hash = event["contentHash"]
        version_id = _version_id(role, source_id, content_hash)
        fact = FinancialFact(
            instrument_id=INSTRUMENT_ID, metric=METRIC, period_end=H125,
            statement_scope=StatementScope.CONSOLIDATED, profit_scope=None,
            value=Decimal(event["amountYuan"]), currency="CNY", raw_unit="yuan",
            source_id="cninfo", source_document_id=source_id,
            source_published_date=date.fromisoformat(event["publishedDate"]),
            source_published_at=None, timestamp_precision=TimestampPrecision.DATE,
            first_seen_at=_aware(event["firstSeenAt"], "PDF first_seen_at"),
            ingested_at=ingested_at, available_at=available_at,
            availability_basis=AvailabilityBasis.OBSERVED, pit_mode=PitMode.LIVE_OBSERVED,
            content_hash=content_hash, version_id=version_id, supersedes_id=predecessor,
        )
        facts.append(fact)
        events.append({**event, "role": role, "versionId": version_id,
                       "supersedesId": predecessor, "ingestedAt": ingested_at.isoformat(),
                       "availableAt": available_at.isoformat()})
        predecessor = version_id
    return (facts[0], facts[1]), events


def _collect_first_seen(value: object) -> list[datetime]:
    found: list[datetime] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in {"first_seen_at", "firstseenat"} and isinstance(child, str):
                found.append(_aware(child, "nested source first_seen_at"))
            else:
                found.extend(_collect_first_seen(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.extend(_collect_first_seen(child))
    return found


def _source_evidence(archive) -> dict:
    from aquant.adapters.providers import h125_profit_pair_000680_version_chain as profit_pair
    from aquant.adapters.providers.parent_equity_successor_compare import (
        compare_parent_equity_h125_pair,
    )

    if archive.con.execute("PRAGMA query_only").fetchone()[0] != 1:
        raise H125ParentEquityPairContractError("source archive must be opened query-only")
    original, presenting = _report_bindings(profit_pair)
    existing_source = profit_pair._pair_source_evidence(
        archive, profit_pair.QUALIFICATION_REPLAY_INPUT,
    )
    relationship_replay = json.loads(_canonical(profit_pair.RELATIONSHIP_SOURCE_REPLAY))
    relationship_closure = profit_pair._verify_relationship_source_replay(
        archive, relationship_replay,
    )
    h125_receipt = existing_source["reports"]["H125_CURRENT"]["receipt"]
    h126_receipt = existing_source["reports"]["H126_PRIOR_REPORT"]["receipt"]
    h125_body = archive.load_bytes(original["contentHash"])
    h126_body = archive.load_bytes(presenting["contentHash"])
    pair = compare_parent_equity_h125_pair(
        h125_body, h126_body, period_end=H125,
        current_announcement_id=original["announcementId"],
        current_source_report_period_end=H125,
        prior_announcement_id=presenting["announcementId"],
        prior_source_report_period_end=H126,
    )
    _validate_native_comparison(pair)
    current = _enriched_event(parsed=pair["current"], report=original,
                              receipt=h125_receipt, source_period=H125,
                              fact_period=H125, role="ORIGINAL_CURRENT")
    prior = _enriched_event(parsed=pair["presentedPrior"], report=presenting,
                            receipt=h126_receipt, source_period=H126,
                            fact_period=H125, role="PRESENTED_PRIOR")
    return {
        "schema": "aquant.000680-h125-parent-equity-source-pair.v1",
        "sourceId": "cninfo", "instrumentId": INSTRUMENT_ID,
        "targetFactPeriodEnd": H125.isoformat(), "metric": METRIC,
        "sourceReportPeriodEnds": {"current": H125.isoformat(), "presentedPrior": H126.isoformat()},
        "current": current, "presentedPrior": prior,
        "nativeComparison": dict(pair),
        "existingProfitPairSourceEvidence": existing_source,
        "relationshipSourceReplay": relationship_replay,
        "relationshipSourceClosure": relationship_closure,
    }


def _assemble_bundle(source: Mapping, *, calendar_evidence: Mapping,
                     calendar_checked_at: datetime, completed: datetime,
                     trading_calendar: list[date]) -> tuple[tuple[FinancialFact, FinancialFact], FactReviewBundle, dict]:
    from aquant.adapters.providers import h125_profit_pair_000680_version_chain as profit_pair

    original, presenting = _report_bindings(profit_pair)
    if completed < calendar_checked_at:
        raise H125ParentEquityPairContractError("source completion precedes calendar verification")
    first_seen = _collect_first_seen(source)
    if not first_seen or any(item > completed for item in first_seen):
        raise H125ParentEquityPairContractError("source first-seen clocks are missing or after completion")
    date_floors = [
        date_only_available_at(date.fromisoformat(original["publishedDate"]), trading_calendar)[0],
        date_only_available_at(date.fromisoformat(presenting["publishedDate"]), trading_calendar)[0],
    ]
    available = profit_pair._next_available_snapshot(
        max([completed, calendar_checked_at, *first_seen, *date_floors]), trading_calendar,
    )
    original_receipt = source["existingProfitPairSourceEvidence"]["reports"]["H125_CURRENT"]["receipt"]
    presenting_receipt = source["existingProfitPairSourceEvidence"]["reports"]["H126_PRIOR_REPORT"]["receipt"]
    facts, version_sources = _events_and_facts(
        source["nativeComparison"], original=original, presenting=presenting,
        original_receipt=original_receipt, presenting_receipt=presenting_receipt,
        ingested_at=completed, available_at=available,
    )
    native_pair = source["nativeComparison"]
    impact = {
        METRIC: {
            "status": "AFFECTS", "admissionStatus": "NOT_ADMITTED",
            "impactProof": None, "causalAttribution": None,
            "current": source["current"], "presentedPrior": source["presentedPrior"],
            "deltaYuan": str(DELTA_YUAN),
            "candidate": native_pair["candidate"],
            "nativeValueComparisonOnly": True,
        }
    }
    version_ids_by_metric = {METRIC: [item.version_id for item in facts]}
    fact_ids = sorted(item.version_id for item in facts)
    from aquant.adapters.providers import h125_profit_pair_000680_version_chain as profit_pair
    evidence = {
        "schema": SCHEMA, "verifierId": VERIFIER_ID, "verifierVersion": "1",
        "humanReviewClaimed": None, "rightsRegisterVersion": REGISTER_VERSION,
        "sourceId": "cninfo", "instrumentId": INSTRUMENT_ID,
        "factPeriodEnd": H125.isoformat(), "metricSet": [METRIC],
        "versionIdsByMetric": version_ids_by_metric, "factVersionIds": fact_ids,
        "versionSourcesByMetric": {METRIC: version_sources},
        "sourceEvidence": source,
        "sourceQualificationSha256": profit_pair.QUALIFICATION_REPLAY_INPUT["sourceQualificationSha256"],
        "relationshipPacketSha256": profit_pair.RELATIONSHIP_PACKET_SOURCE_SHA256,
        "relationshipSourceAuditSha256": profit_pair.RELATIONSHIP_SOURCE_AUDIT_SHA256,
        "relationshipSourceReplaySha256": _sha(_canonical(source["relationshipSourceReplay"]).encode("utf-8")),
        "relationshipSourceClosure": source["relationshipSourceClosure"],
        "impactClassification": impact,
        "candidateDisposition": "PRESENTED_PRIOR_CHANGED_VALUE_AFFECTS_VERSION_CHAIN",
        "causalAttribution": None,
        "classificationBoundary": {
            "formalAdmission": "NOT_ADMITTED", "impactStatus": "AFFECTS",
            "impactProof": None, "causalAttribution": None,
            "priorStrictStatus": "NOT_VERIFIED_AS_PRIOR_FACTS",
            "nativePriorStatus": "PASS_EXACT_H125_COMPARATIVE_CELL",
        },
        "calendarEvidence": dict(calendar_evidence),
        "calendarCheckedAt": calendar_checked_at.isoformat(),
        "tradingCalendar": [day.isoformat() for day in trading_calendar],
        "completedAt": completed.isoformat(), "availableAt": available.isoformat(),
        "formalAdmissionStatus": "NOT_ADMITTED",
    }
    bundle_id = PREFIX + hashlib.sha256(
        f"{INSTRUMENT_ID}|{H125}|{'|'.join(fact_ids)}".encode("utf-8")
    ).hexdigest()[:32]
    bundle = FactReviewBundle(bundle_id, _canonical(evidence))
    return facts, bundle, evidence


def prepare_000680_h125_parent_equity_version_chain(
    *, archive, calendar_evidence: Mapping | None = None,
) -> tuple[tuple[FinancialFact, FinancialFact], FactReviewBundle, dict]:
    """Replay pinned originals and prepare the two facts plus immutable review.

    This function does not mutate the fact repository. It is suitable for a
    private backup clone, where the caller can inspect the prepared evidence
    before invoking ``repository.append_many``.
    """
    from aquant.adapters.providers import h125_profit_pair_000680_version_chain as profit_pair
    from aquant.adapters.providers.s2_calendar_evidence import verify_calendar_evidence

    rights = default_rights().get("cninfo")
    if rights is None or not (rights.open_for("research_use") and rights.open_for("local_storage")):
        raise H125ParentEquityPairContractError("CNINFO research/storage rights are closed")
    source = _source_evidence(archive)
    original, presenting = _report_bindings(profit_pair)
    started = profit_pair._utc_now()
    calendar_packet = (profit_pair.CALENDAR_EVIDENCE if calendar_evidence is None
                       else dict(calendar_evidence))
    calendar_checked_at = profit_pair._utc_now()
    trading_calendar, verified_calendar = verify_calendar_evidence(
        calendar_packet, archive, now=calendar_checked_at,
        published_on=date.fromisoformat(original["publishedDate"]),
    )
    completed = profit_pair._utc_now()
    if completed < started:
        raise H125ParentEquityPairContractError("writer clock moved backwards")
    return _assemble_bundle(
        source, calendar_evidence=verified_calendar,
        calendar_checked_at=calendar_checked_at, completed=completed,
        trading_calendar=list(trading_calendar),
    )


def promote_000680_h125_parent_equity_version_chain(
    *, archive, repository: FinancialFactRepository,
    calendar_evidence: Mapping | None = None,
) -> tuple[FinancialFact, FinancialFact]:
    """Prepare and append exactly the original and changed presented-prior facts."""
    rights = default_rights().get("cninfo")
    if rights is None or not (rights.open_for("research_use") and rights.open_for("local_storage")):
        raise H125ParentEquityPairContractError("CNINFO research/storage rights are closed")
    if any(fact.instrument_id == INSTRUMENT_ID and fact.period_end == H125 and
           fact.metric == METRIC for fact in repository.load().facts):
        raise H125ParentEquityPairContractError("target parent-equity fact versions already exist")
    facts, bundle, _ = prepare_000680_h125_parent_equity_version_chain(
        archive=archive, calendar_evidence=calendar_evidence,
    )
    repository.append_many(facts, review_bundle=bundle)
    return facts


def accepts_000680_h125_parent_equity_version_chain(
    row: Mapping, member_ids: set[str], facts: Mapping[str, FinancialFact], *,
    accepted_links: Mapping[str, tuple[str, str]],
) -> bool:
    """Fail-closed public-reader replay for this two-version parent-equity pair."""
    source_connection = None
    try:
        from aquant.adapters.providers import h125_profit_pair_000680_version_chain as profit_pair
        from aquant.adapters.providers.s2_calendar_evidence import verify_calendar_evidence
        evidence_json = row["evidence_json"]
        evidence = json.loads(evidence_json)
        if (_canonical(evidence) != evidence_json or row["evidence_hash"] != _sha(evidence_json.encode("utf-8")) or
                evidence.get("schema") != SCHEMA or evidence.get("verifierId") != VERIFIER_ID or
                evidence.get("humanReviewClaimed") is not None or
                evidence.get("rightsRegisterVersion") != REGISTER_VERSION or
                evidence.get("sourceId") != "cninfo" or evidence.get("instrumentId") != INSTRUMENT_ID or
                evidence.get("factPeriodEnd") != H125.isoformat() or
                evidence.get("metricSet") != [METRIC] or evidence.get("formalAdmissionStatus") != "NOT_ADMITTED" or
                evidence.get("candidateDisposition") != "PRESENTED_PRIOR_CHANGED_VALUE_AFFECTS_VERSION_CHAIN" or
                evidence.get("causalAttribution") is not None):
            return False
        version_map = evidence.get("versionIdsByMetric", {})
        version_ids = version_map.get(METRIC, ()) if isinstance(version_map, Mapping) else ()
        if (set(version_map) != {METRIC} or len(version_ids) != 2 or len(set(version_ids)) != 2 or
                member_ids != set(version_ids) or member_ids != set(evidence.get("factVersionIds", ())) or
                not member_ids <= facts.keys() or member_ids & accepted_links.keys()):
            return False
        expected_bundle_id = PREFIX + hashlib.sha256(
            f"{INSTRUMENT_ID}|{H125}|{'|'.join(sorted(member_ids))}".encode("utf-8")
        ).hexdigest()[:32]
        if row["bundle_id"] != expected_bundle_id:
            return False

        db_path = profit_pair.ARCHIVE_DATABASE.resolve(strict=True)
        archive_root = profit_pair.ARCHIVE_ROOT.resolve(strict=True)
        source_connection = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True,
                                            timeout=30, isolation_level=None)
        source_connection.row_factory = sqlite3.Row
        source_connection.execute("PRAGMA query_only=ON")
        if source_connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            return False
        source_connection.execute("BEGIN")
        archive = profit_pair.ReadOnlyArchive(source_connection, archive_root)
        expected = _source_evidence(archive)
        if evidence.get("sourceEvidence") != expected or evidence.get("relationshipSourceClosure") != expected["relationshipSourceClosure"]:
            return False
        if evidence.get("relationshipSourceReplaySha256") != _sha(
                _canonical(expected["relationshipSourceReplay"]).encode("utf-8")):
            return False

        ingested_at = _aware(evidence["completedAt"], "writer completedAt")
        calendar_checked_at = _aware(evidence["calendarCheckedAt"], "calendarCheckedAt")
        available_at = _aware(evidence["availableAt"], "writer availableAt")
        if (calendar_checked_at > ingested_at or ingested_at > datetime.now(timezone.utc)):
            return False
        calendar_days, rebuilt_calendar = verify_calendar_evidence(
            evidence.get("calendarEvidence"), archive, now=ingested_at,
            published_on=date.fromisoformat(profit_pair.H125_REPORT["publishedDate"]),
        )
        if ([day.isoformat() for day in calendar_days] != evidence.get("tradingCalendar") or
                rebuilt_calendar != evidence.get("calendarEvidence")):
            return False
        all_first_seen = _collect_first_seen(expected)
        if not all_first_seen or any(seen > ingested_at for seen in all_first_seen):
            return False
        original, presenting = _report_bindings(profit_pair)
        date_floors = [
            date_only_available_at(date.fromisoformat(original["publishedDate"]), calendar_days)[0],
            date_only_available_at(date.fromisoformat(presenting["publishedDate"]), calendar_days)[0],
        ]
        computed_available = profit_pair._next_available_snapshot(
            max([ingested_at, calendar_checked_at, *all_first_seen, *date_floors]), calendar_days,
        )
        if available_at != computed_available:
            return False
        rebuilt, rebuilt_bundle, rebuilt_evidence = _assemble_bundle(
            expected, calendar_evidence=rebuilt_calendar,
            calendar_checked_at=calendar_checked_at, completed=ingested_at,
            trading_calendar=list(calendar_days),
        )
        if (_canonical(rebuilt_evidence) != evidence_json or rebuilt_bundle.bundle_id != row["bundle_id"]):
            return False
        # The replay re-reads receipt-bound originals and all 23 related PDFs;
        # the H126 PRIOR remains a source role, not a strict CURRENT report.
        source_connection.execute("ROLLBACK")
        if source_connection.total_changes != 0:
            return False
        source_connection.close()
        source_connection = None
        by_version = {str(fact.version_id): fact for fact in facts.values()}
        expected_facts = {str(fact.version_id): fact for fact in rebuilt}
        if set(expected_facts) != member_ids:
            return False
        for version_id in sorted(member_ids):
            saved = by_version.get(version_id)
            expected_fact = expected_facts[version_id]
            if (saved is None or saved != expected_fact or
                    saved.instrument_id != INSTRUMENT_ID or saved.metric != METRIC or
                    saved.period_end != H125 or saved.statement_scope is not StatementScope.CONSOLIDATED or
                    saved.profit_scope is not None or saved.currency != "CNY" or saved.raw_unit != "yuan" or
                    saved.source_id != "cninfo" or saved.availability_basis is not AvailabilityBasis.OBSERVED or
                    saved.pit_mode is not PitMode.LIVE_OBSERVED):
                return False
        impact = evidence.get("impactClassification", {})
        item = impact.get(METRIC, {}) if isinstance(impact, Mapping) else {}
        boundary = evidence.get("classificationBoundary", {})
        if (item.get("status") != "AFFECTS" or item.get("admissionStatus") != "NOT_ADMITTED" or
                item.get("impactProof") is not None or item.get("causalAttribution") is not None or
                item.get("deltaYuan") != str(DELTA_YUAN) or
                boundary.get("impactStatus") != "AFFECTS" or boundary.get("impactProof") is not None or
                boundary.get("causalAttribution") is not None or
                boundary.get("priorStrictStatus") != "NOT_VERIFIED_AS_PRIOR_FACTS" or
                boundary.get("nativePriorStatus") != "PASS_EXACT_H125_COMPARATIVE_CELL"):
            return False
        return True
    except (OSError, sqlite3.Error, KeyError, IndexError, AttributeError, TypeError,
            ValueError, ArithmeticError):
        return False
    finally:
        if source_connection is not None:
            try:
                source_connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            source_connection.close()


__all__ = [
    "SCHEMA", "VERIFIER_ID", "PREFIX", "H125ParentEquityPairContractError",
    "prepare_000680_h125_parent_equity_version_chain",
    "promote_000680_h125_parent_equity_version_chain",
    "accepts_000680_h125_parent_equity_version_chain",
]
