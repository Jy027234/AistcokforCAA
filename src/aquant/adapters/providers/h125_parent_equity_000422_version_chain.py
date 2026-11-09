"""Narrow 000422 H125 parent-equity CURRENT -> H126 presented-prior chain.

The value comparison is limited to one exact consolidated H125 parent-equity
cell. The complete existing four-flow qualification is retained as source
closure evidence, but its conclusions are not transferred to parent equity.
Neither source version is formally admitted and no correction is assigned
causality.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from aquant.artifact_paths import resolve_archive_member
from aquant.adapters.providers.pdf_promotion import _next_available_snapshot, _utc_now
from aquant.domain.data.pit import (
    AvailabilityBasis, PitMode, TimestampPrecision, date_only_available_at,
)
from aquant.domain.data.rights import REGISTER_VERSION, default_rights
from aquant.domain.fundamentals.fact_repository import FactReviewBundle, FinancialFactRepository
from aquant.domain.fundamentals.versioned import FinancialFact, StatementScope


SCHEMA = "aquant.cninfo.000422-h125-parent-equity-no-effect-value-only-chain.v1"
VERIFIER_ID = "machine:cninfo-000422-h125-parent-equity-chain-v1"
INSTRUMENT_ID = "000422"
STOCK_CODE = "000422"
ISSUER_ID = "SZ.000422"
ISSUER_NAME = "湖北宜化化工股份有限公司"
H125 = date(2025, 6, 30)
H126 = date(2026, 6, 30)
METRIC = "parent_equity"
VALUE_YUAN = Decimal("5454477244.67")
CAUSE_IDS = ("1222991641", "1222991666", "1225183650", "1225183652")
FLOW_METRICS = (
    "net_profit_attributable", "net_profit_consolidated", "operating_cashflow", "revenue",
)
FLOW_CURRENT_YUAN = {
    "net_profit_attributable": "398880278.09",
    "net_profit_consolidated": "753547510.48",
    "operating_cashflow": "1287155858.35",
    "revenue": "12004585375.22",
}
PREFIX = "cninfo_machine_000422_h125_parent_equity_"
ARCHIVE_ROOT = Path(os.environ.get(
    "AQUANT_000422_ARCHIVE_ROOT", "/app/deploy/agentctl-q0/forward-archive",
))
ARCHIVE_DATABASE = Path(os.environ.get(
    "AQUANT_000422_ARCHIVE_DATABASE", "/app/deploy/agentctl-q0/forward-archive/meta.sqlite",
))

REPORTS = {
    "ORIGINAL_CURRENT": {
        "announcementId": "1224559960", "sourceReportPeriodEnd": "2025-06-30",
        "publishedDate": "2025-08-23", "title": "2025年半年度报告",
        "documentUrl": "https://static.cninfo.com.cn/finalpage/2025-08-23/1224559960.PDF",
        "contentHash": "sha256:9d110cdb52096cc04fb358f886c447ebe58022813bcdeb8a3f7c2de237046f78",
        "receiptId": "rcpt_e30a9e2d8c7ff6f996fd14bd", "columnRole": "CURRENT",
    },
    "PRESENTED_PRIOR": {
        "announcementId": "1225468007", "sourceReportPeriodEnd": "2026-06-30",
        "publishedDate": "2026-08-12", "title": "2026年半年度报告",
        "documentUrl": "https://static.cninfo.com.cn/finalpage/2026-08-12/1225468007.PDF",
        "contentHash": "sha256:6963a7b5ca322c8fe53d59eafe383bb882689ad9e2a9b78aa669373f58de4e12",
        "receiptId": "rcpt_1ba19887efeac0892b1f4a71", "columnRole": "PRESENTED_PRIOR",
    },
}


class H125ParentEquity000422ContractError(ValueError):
    """A source, native-cell, PIT, or lineage contract failed."""


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _aware(value: str | datetime, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise H125ParentEquity000422ContractError(f"{label} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise H125ParentEquity000422ContractError(f"{label} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal("".join(str(value).replace(",", "").split()))
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise H125ParentEquity000422ContractError(f"{label} is not a decimal CNY amount") from exc
    if not parsed.is_finite():
        raise H125ParentEquity000422ContractError(f"{label} is not finite")
    return parsed


def _bbox(value: object, label: str) -> tuple[float, float, float, float]:
    if (not isinstance(value, (list, tuple)) or len(value) != 4 or
            any(type(part) not in (int, float) for part in value)):
        raise H125ParentEquity000422ContractError(f"{label} is not a native four-coordinate box")
    points = tuple(float(part) for part in value)
    if (not all(point == point and abs(point) != float("inf") for point in points) or
            points[2] <= points[0] or points[3] <= points[1]):
        raise H125ParentEquity000422ContractError(f"{label} is not a finite positive box")
    return points


def _validate_native_table_proof(
    proof: Mapping, *, source_period: date, presentation: str, label: str,
) -> None:
    expected = {
        "period_end": H125.isoformat(), "source_report_period_end": source_period.isoformat(),
        "presentation": presentation, "unit": "元", "closing_row_label": "四、本期期末余额",
        "subtotal_header": "小计",
    }
    if any(proof.get(key) != value for key, value in expected.items()):
        raise H125ParentEquity000422ContractError(f"{label} is not the H125 parent-equity closing subtotal")
    if proof.get("parent_group_header") not in {"归属于母公司股东权益", "归属于母公司所有者权益"}:
        raise H125ParentEquity000422ContractError(f"{label} is not the parent-equity subtotal column")
    if (_decimal(proof.get("amount_yuan"), f"{label} amount") != VALUE_YUAN or
            _decimal(proof.get("native_cell_text"), f"{label} native cell") != VALUE_YUAN or
            proof.get("bbox_tolerance_points") != 1.0):
        raise H125ParentEquity000422ContractError(f"{label} native amount/geometry differs")
    header = _bbox(proof.get("header_subtotal_bbox"), f"{label} subtotal header")
    value_cell = _bbox(proof.get("closing_cell_bbox"), f"{label} value cell")
    row = _bbox(proof.get("closing_row_bbox"), f"{label} closing row")
    if (abs(header[0] - value_cell[0]) > 1.0 or abs(header[2] - value_cell[2]) > 1.0 or
            value_cell[1] > row[1] + 1.0 or value_cell[3] < row[3] - 1.0):
        raise H125ParentEquity000422ContractError(f"{label} native geometry does not bind the subtotal cell")
    if (type(proof.get("report_table_page")) is not int or proof["report_table_page"] < 1 or
            type(proof.get("closing_row_page")) is not int or proof["closing_row_page"] < 1 or
            "合并所有者权益变动表" not in str(proof.get("report_table_title", "")) or
            "2025" not in str(proof.get("period_header", ""))):
        raise H125ParentEquity000422ContractError(f"{label} native table identity/page/period differs")
    native_counts = proof.get("native_chars_by_page")
    if (not isinstance(native_counts, Mapping) or
            type(native_counts.get(str(proof["closing_row_page"]))) is not int or
            native_counts[str(proof["closing_row_page"])] < 1):
        raise H125ParentEquity000422ContractError(f"{label} native cell has no physical-page text")
    # The shared parser may retain a partial native opening-change excerpt.
    # Auxiliary opening rows and an opening-balance calculation are not part
    # of this exact June 30 closing-cell acceptance gate.


def _validate_current_bs_proof(cell: Mapping) -> None:
    proof = cell.get("proof")
    if not isinstance(proof, Mapping):
        raise H125ParentEquity000422ContractError("H125 CURRENT lacks native balance-sheet proof")
    if (proof.get("path_kind") != "native_consolidated_balance_sheet_current_parent_equity_row" or
            proof.get("fact_period_end") != H125.isoformat() or
            proof.get("statement") != "合并资产负债表" or
            proof.get("statement_scope") != "CONSOLIDATED" or
            proof.get("column_role") != "CURRENT" or proof.get("current_header") != "期末余额" or
            proof.get("unit") != "元" or proof.get("amount_yuan") != str(VALUE_YUAN) or
            proof.get("page") != cell.get("page") or proof.get("row_label") != cell.get("rowLabel") or
            proof.get("source_line") != cell.get("sourceLine")):
        raise H125ParentEquity000422ContractError("H125 CURRENT balance-sheet native proof differs")
    if not isinstance(cell.get("sameReportEquityChangeTable"), Mapping):
        raise H125ParentEquity000422ContractError("H125 CURRENT lacks its same-report roll-forward check")
    _validate_native_table_proof(
        cell["sameReportEquityChangeTable"], source_period=H125,
        presentation="CURRENT", label="H125 same-report equity-change cross-check",
    )


def _validate_native_cell(cell: object, *, role: str) -> Mapping:
    if not isinstance(cell, Mapping):
        raise H125ParentEquity000422ContractError(f"{role} native parser cell is missing")
    binding = REPORTS[role]
    expected = {
        "announcementId": binding["announcementId"],
        "sourceReportPeriodEnd": binding["sourceReportPeriodEnd"],
        "factPeriodEnd": H125.isoformat(), "columnRole": binding["columnRole"],
        "statement": "合并资产负债表" if role == "ORIGINAL_CURRENT" else "合并所有者权益变动表",
        "statementScope": "CONSOLIDATED", "currency": "CNY", "amountUnit": "元", "rawUnit": "元",
        "amountYuan": str(VALUE_YUAN),
    }
    if any(cell.get(key) != value for key, value in expected.items()):
        raise H125ParentEquity000422ContractError(f"{role} source/date/role/scope/value identity differs")
    for name in ("page", "headerPage", "nativeChars"):
        if type(cell.get(name)) is not int or cell[name] < 1:
            raise H125ParentEquity000422ContractError(f"{role} lacks native {name}")
    if (not isinstance(cell.get("rowLabel"), str) or not cell["rowLabel"].strip() or
            not isinstance(cell.get("columnHeader"), str) or not cell["columnHeader"].strip() or
            not isinstance(cell.get("sourceLine"), str) or not cell["sourceLine"].strip() or
            not isinstance(cell.get("proof"), Mapping)):
        raise H125ParentEquity000422ContractError(f"{role} lacks a native source line or proof")
    if role == "ORIGINAL_CURRENT":
        if "归属于母公司" not in cell["rowLabel"] or "期末余额" not in cell["columnHeader"]:
            raise H125ParentEquity000422ContractError("H125 CURRENT row/column does not identify parent equity")
        _validate_current_bs_proof(cell)
    else:
        if (cell["rowLabel"] != "四、本期期末余额" or "小计" not in cell["columnHeader"] or
                cell["proof"].get("path_kind") != "native_parent_equity_change_table_closing_subtotal_cell"):
            raise H125ParentEquity000422ContractError("H126 PRIOR is not the closing subtotal cell")
        _validate_native_table_proof(
            cell["proof"], source_period=H126, presentation="PRESENTED_PRIOR",
            label="H126 presented H125 PRIOR",
        )
    return cell


def _validate_native_comparison(pair: object) -> tuple[Mapping, Mapping]:
    if not isinstance(pair, Mapping):
        raise H125ParentEquity000422ContractError("shared native comparator returned no pair")
    if (pair.get("schema") != "aquant.parent-equity.successor-comparator.v1" or
            pair.get("metric") != METRIC or pair.get("period_end") != H125.isoformat()):
        raise H125ParentEquity000422ContractError("shared native comparator identity differs")
    current = _validate_native_cell(pair.get("current"), role="ORIGINAL_CURRENT")
    prior = _validate_native_cell(pair.get("presentedPrior"), role="PRESENTED_PRIOR")
    if (pair.get("same_value") is not True or _decimal(pair.get("delta_yuan"), "native pair delta") != 0 or
            pair.get("candidate") != "NO_EFFECT_VALUE_ONLY" or
            pair.get("causal_attribution") != "UNRESOLVED" or pair.get("admission") != "NOT_ADMITTED"):
        raise H125ParentEquity000422ContractError("same-value-only or admission boundary differs")
    if (current.get("pdfSha256") != REPORTS["ORIGINAL_CURRENT"]["contentHash"] or
            prior.get("pdfSha256") != REPORTS["PRESENTED_PRIOR"]["contentHash"]):
        raise H125ParentEquity000422ContractError("native pair is detached from pinned report bytes")
    if current.get("sameReportEquityChangeTable", {}).get("amount_yuan") != str(VALUE_YUAN):
        raise H125ParentEquity000422ContractError("H125 same-report cross-check value differs")
    return current, prior


def _version_id(role: str) -> str:
    binding = REPORTS[role]
    identity = "|".join((INSTRUMENT_ID, H125.isoformat(), METRIC, role,
                         binding["announcementId"], binding["contentHash"]))
    return "mach_000422_h125_parent_equity_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _profile_inputs() -> dict:
    from aquant.adapters.providers.h125_parent_equity_000422_review_inputs import (
        PACKET_SHA256, qualification_inputs,
    )
    if PACKET_SHA256 != "sha256:ca9b875539978d7889b97881a7a837ddbb2cf5316089c26e8244e4bcce8e5371":
        raise H125ParentEquity000422ContractError("pinned four-flow packet hash differs")
    inputs = qualification_inputs()
    company = inputs.get("company")
    if (inputs.get("targetPeriodEnd") != H125.isoformat() or
            set(inputs.get("targetMetrics", ())) != set(FLOW_METRICS) or
            inputs.get("issuerName") != ISSUER_NAME or not isinstance(company, Mapping) or
            company.get("code") != STOCK_CODE or company.get("instrumentId") != ISSUER_ID or
            company.get("allCategoryTotalAnnouncement") != 573 or
            company.get("allCategoryTermination") != "has_more_false" or
            len(company.get("allCategoryPages", ())) != 20 or
            len(company.get("selectedReportRecords", ())) != 5 or
            len(inputs.get("documents", ())) != 43):
        raise H125ParentEquity000422ContractError("pinned complete 000422 H125 four-flow source profile differs")
    for role, binding in REPORTS.items():
        matches = [row for row in company["selectedReportRecords"]
                   if str(row.get("announcementId")) == binding["announcementId"]]
        docs = [row for row in inputs["documents"]
                if str(row.get("announcementId")) == binding["announcementId"]]
        if len(matches) != 1 or len(docs) != 1:
            raise H125ParentEquity000422ContractError(f"source profile lacks unique report {binding['announcementId']}")
        record, doc = matches[0], docs[0]
        receipt = record.get("receipt")
        if (record.get("code") != STOCK_CODE or record.get("instrumentId") != ISSUER_ID or
                record.get("periodEnd") != binding["sourceReportPeriodEnd"] or
                record.get("title") != binding["title"] or record.get("documentUrl") != binding["documentUrl"] or
                record.get("contentHash") != binding["contentHash"] or not isinstance(receipt, Mapping) or
                receipt.get("receipt_id") != binding["receiptId"] or
                receipt.get("content_hash") != binding["contentHash"] or
                receipt.get("url") != binding["documentUrl"] or receipt.get("source_id") != "cninfo" or
                receipt.get("outcome") != "OK" or receipt.get("http_status") != 200 or doc.get("receipt") != receipt):
            raise H125ParentEquity000422ContractError(f"source report receipt/index binding differs for {role}")
    return inputs


def _assessor_arguments(archive_root: Path, inputs: Mapping) -> dict:
    """Translate the pinned packet's camelCase JSON to the runtime API contract."""
    return {
        "archive_root": archive_root,
        "company": inputs["company"],
        "target_period_end": date.fromisoformat(inputs["targetPeriodEnd"]),
        "target_metrics": tuple(inputs["targetMetrics"]),
        "issuer_name": inputs["issuerName"],
        "documents": inputs["documents"],
    }


def _validate_four_flow_qualification(proof: Mapping, inputs: Mapping) -> None:
    without_hash = dict(proof)
    evidence_hash = without_hash.pop("evidenceHash", None)
    if (evidence_hash != _sha(_canonical(without_hash).encode("utf-8")) or
            proof.get("schema") != "aquant.s2-machine-revision-impact.v1" or
            proof.get("verifierId") != "machine:s2-revision-impact-v1" or
            proof.get("instrumentId") != ISSUER_ID or proof.get("issuerName") != ISSUER_NAME or
            proof.get("targetPeriodEnd") != H125.isoformat() or proof.get("writesFinancialWarehouse") is not False):
        raise H125ParentEquity000422ContractError("four-flow source qualification header/hash differs")
    scope = proof.get("relationshipScope")
    if (not isinstance(scope, Mapping) or scope.get("completeForArchivedIndexResponse") is not True or
            scope.get("missingReportSourceIds") != [] or scope.get("missingFundraisingSpecialReportIds") != [] or
            scope.get("financialCauseIds") != list(CAUSE_IDS) or
            not set(CAUSE_IDS) <= set(scope.get("allRecalledRevisionIds", ()))):
        raise H125ParentEquity000422ContractError("complete four-cause relationship closure is absent")
    if len(proof.get("sources", ())) != len(inputs.get("documents", ())):
        raise H125ParentEquity000422ContractError("four-flow source proof omitted a retained original")
    outcomes = proof.get("outcomes", ())
    if not isinstance(outcomes, list) or {item.get("metric") for item in outcomes} != set(FLOW_METRICS):
        raise H125ParentEquity000422ContractError("four-flow qualification outcome set differs")
    for outcome in outcomes:
        metric = outcome["metric"]
        current = outcome.get("currentSource")
        impact_proof = outcome.get("impactProof")
        if (outcome.get("status") != "NO_EFFECT" or outcome.get("admissionStatus") != "NOT_ADMITTED" or
                not isinstance(impact_proof, Mapping) or
                impact_proof.get("basis") != "EXPLICIT_2023_AND_2024_RESTATEMENT_SCOPE_WITH_UNCHANGED_H125" or
                outcome.get("unresolvedSuccessorIds") != [] or outcome.get("missingReportSourceIds") != [] or
                not isinstance(current, Mapping) or
                current.get("announcementId") != REPORTS["ORIGINAL_CURRENT"]["announcementId"] or
                current.get("sourceReportPeriodEnd") != H125.isoformat() or
                current.get("amountYuan") != FLOW_CURRENT_YUAN[metric]):
            raise H125ParentEquity000422ContractError(f"strict H125 flow outcome differs for {metric}")


def _source_evidence(archive, *, saved_four_flow_impact: Mapping | None = None) -> dict:
    from aquant.adapters.providers import s2_revision_impact as impact
    from aquant.adapters.providers.parent_equity_successor_compare import compare_parent_equity_h125_pair

    if archive.con.execute("PRAGMA query_only").fetchone()[0] != 1:
        raise H125ParentEquity000422ContractError("source archive must be opened query-only")
    inputs = _profile_inputs()
    assessor_arguments = _assessor_arguments(archive.root, inputs)
    if saved_four_flow_impact is None:
        four_flow = impact.assess_revision_impact(**assessor_arguments).as_dict()
    else:
        impact.replay_revision_impact(saved_four_flow_impact, **assessor_arguments)
        four_flow = dict(saved_four_flow_impact)
    _validate_four_flow_qualification(four_flow, inputs)

    records, bodies = {}, {}
    for role, binding in REPORTS.items():
        record = next(row for row in inputs["company"]["selectedReportRecords"]
                      if str(row["announcementId"]) == binding["announcementId"])
        receipt = record["receipt"]
        if (receipt.get("receipt_id") != binding["receiptId"] or
                receipt.get("content_hash") != binding["contentHash"] or
                receipt.get("url") != binding["documentUrl"]):
            raise H125ParentEquity000422ContractError(f"PDF receipt differs for {role}")
        body = archive.load_bytes(binding["contentHash"])
        if len(body) != receipt.get("byte_size") or _sha(body) != binding["contentHash"]:
            raise H125ParentEquity000422ContractError(f"receipt-bound PDF bytes differ for {role}")
        records[role] = {**binding, "receipt": dict(receipt)}
        bodies[role] = body

    pair = compare_parent_equity_h125_pair(
        bodies["ORIGINAL_CURRENT"], bodies["PRESENTED_PRIOR"], period_end=H125,
        current_announcement_id=REPORTS["ORIGINAL_CURRENT"]["announcementId"],
        current_source_report_period_end=H125,
        prior_announcement_id=REPORTS["PRESENTED_PRIOR"]["announcementId"],
        prior_source_report_period_end=H126,
    )
    current, prior = _validate_native_comparison(pair)
    return {
        "schema": "aquant.cninfo.000422-h125-parent-equity-source-pair.v1",
        "sourceId": "cninfo", "instrumentId": ISSUER_ID,
        "targetFactPeriodEnd": H125.isoformat(), "metric": METRIC,
        "reports": records, "current": dict(current), "presentedPrior": dict(prior),
        "nativeComparison": dict(pair), "fourFlowImpact": four_flow,
        "fourFlowInputPacketSha256": "sha256:ca9b875539978d7889b97881a7a837ddbb2cf5316089c26e8244e4bcce8e5371",
        "fourFlowInputSha256": _sha(_canonical(inputs).encode("utf-8")),
        "scopeBoundary": (
            "NO_EFFECT_VALUE_ONLY compares only the original H125 CURRENT parent-equity cell with the "
            "H126 report's presented-prior cell for that same date. The separate four-flow qualification "
            "is retained for complete source closure only; it does not establish that any correction has "
            "no economic effect on parent equity or other metrics. Causal attribution is unresolved and "
            "neither version is formally admitted."
        ),
    }


def _enriched_event(*, parsed: Mapping, report: Mapping, source_period: date) -> dict:
    amount = _decimal(parsed.get("amountYuan"), "native pair amount")
    receipt = report["receipt"]
    if (amount != VALUE_YUAN or parsed.get("announcementId") != report["announcementId"] or
            parsed.get("pdfSha256") != report["contentHash"] or
            report.get("documentUrl") != receipt.get("url") or
            report.get("contentHash") != receipt.get("content_hash")):
        raise H125ParentEquity000422ContractError("native cell detaches from its source receipt")
    return {
        **dict(parsed), "receiptId": report["receiptId"], "contentHash": report["contentHash"],
        "documentUrl": report["documentUrl"], "title": report["title"],
        "publishedDate": report["publishedDate"], "firstSeenAt": receipt["first_seen_at"],
        "sourceReportPeriodEnd": source_period.isoformat(), "factPeriodEnd": H125.isoformat(),
        "amountYuan": str(amount), "factRawUnit": "yuan", "nativeProof": dict(parsed.get("proof", {})),
    }


def _collect_first_seen(value: object) -> list[datetime]:
    found: list[datetime] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in {"first_seen_at", "firstseenat", "indexfirstseenat"} and isinstance(child, str):
                found.append(_aware(child, "nested source first_seen_at"))
            else:
                found.extend(_collect_first_seen(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.extend(_collect_first_seen(child))
    return found


def _events_and_facts(source: Mapping, *, completed: datetime,
                      available_at: datetime) -> tuple[tuple[FinancialFact, FinancialFact], list[dict]]:
    events = [
        _enriched_event(parsed=source["current"], report=source["reports"]["ORIGINAL_CURRENT"], source_period=H125),
        _enriched_event(parsed=source["presentedPrior"], report=source["reports"]["PRESENTED_PRIOR"], source_period=H126),
    ]
    facts: list[FinancialFact] = []
    version_events: list[dict] = []
    predecessor = None
    for role, event in zip(("ORIGINAL_CURRENT", "PRESENTED_PRIOR"), events, strict=True):
        fact = FinancialFact(
            instrument_id=INSTRUMENT_ID, metric=METRIC, period_end=H125,
            statement_scope=StatementScope.CONSOLIDATED, profit_scope=None,
            value=VALUE_YUAN, currency="CNY", raw_unit="yuan", source_id="cninfo",
            source_document_id=event["announcementId"],
            source_published_date=date.fromisoformat(event["publishedDate"]),
            source_published_at=None, timestamp_precision=TimestampPrecision.DATE,
            first_seen_at=_aware(event["firstSeenAt"], f"{role} first_seen_at"),
            ingested_at=completed, available_at=available_at,
            availability_basis=AvailabilityBasis.OBSERVED, pit_mode=PitMode.LIVE_OBSERVED,
            content_hash=event["contentHash"], version_id=_version_id(role), supersedes_id=predecessor,
        )
        facts.append(fact)
        version_events.append({
            **event, "role": role, "versionId": fact.version_id,
            "supersedesId": predecessor, "ingestedAt": completed.isoformat(),
            "availableAt": available_at.isoformat(),
        })
        predecessor = fact.version_id
    return (facts[0], facts[1]), version_events


def _assemble_bundle(source: Mapping, *, calendar_evidence: Mapping,
                     calendar_checked_at: datetime, completed: datetime,
                     trading_calendar: list[date]) -> tuple[tuple[FinancialFact, FinancialFact], FactReviewBundle, dict]:
    if completed < calendar_checked_at:
        raise H125ParentEquity000422ContractError("source completion precedes calendar verification")
    first_seen = _collect_first_seen(source)
    if not first_seen or any(stamp > completed for stamp in first_seen):
        raise H125ParentEquity000422ContractError("source first-seen clocks are missing or after completion")
    date_floors = [
        date_only_available_at(date.fromisoformat(REPORTS[role]["publishedDate"]), trading_calendar)[0]
        for role in ("ORIGINAL_CURRENT", "PRESENTED_PRIOR")
    ]
    available_at = _next_available_snapshot(
        max([completed, calendar_checked_at, *first_seen, *date_floors]), trading_calendar,
    )
    facts, version_events = _events_and_facts(source, completed=completed, available_at=available_at)
    fact_ids = [fact.version_id for fact in facts]
    evidence = {
        "schema": SCHEMA, "verifierId": VERIFIER_ID, "verifierVersion": "1",
        "humanReviewClaimed": None, "rightsRegisterVersion": REGISTER_VERSION,
        "sourceId": "cninfo", "instrumentId": ISSUER_ID, "factPeriodEnd": H125.isoformat(),
        "metricSet": [METRIC], "versionIdsByMetric": {METRIC: fact_ids},
        "factVersionIds": fact_ids, "versionSourcesByMetric": {METRIC: version_events},
        "sourceEvidence": dict(source),
        "relationshipSourceClosure": source["fourFlowImpact"]["relationshipScope"],
        "relationshipSourceReplaySha256": source["fourFlowImpact"]["evidenceHash"],
        "sourceQualificationSha256": source["fourFlowInputPacketSha256"],
        "impactClassification": {METRIC: {
            "status": "NO_EFFECT_VALUE_ONLY", "admissionStatus": "NOT_ADMITTED",
            "impactProof": None, "causalAttribution": None,
            "current": source["current"], "presentedPrior": source["presentedPrior"],
            "deltaYuan": "0.00", "candidate": "NO_EFFECT_VALUE_ONLY",
            "nativeValueComparisonOnly": True,
        }},
        "candidateDisposition": "SAME_DATE_EQUAL_VALUE_NO_EFFECT_VALUE_ONLY",
        "causalAttribution": None,
        "classificationBoundary": {
            "formalAdmission": "NOT_ADMITTED", "impactStatus": "NO_EFFECT_VALUE_ONLY",
            "impactProof": None, "causalAttribution": None,
            "priorStrictStatus": "NOT_VERIFIED_AS_PRIOR_FACTS",
            "nativePriorStatus": "PASS_EXACT_H125_PRESENTED_PRIOR_CELL",
            "economicEffectClaim": None,
        },
        "calendarEvidence": dict(calendar_evidence),
        "calendarCheckedAt": calendar_checked_at.isoformat(),
        "tradingCalendar": [day.isoformat() for day in trading_calendar],
        "completedAt": completed.isoformat(), "availableAt": available_at.isoformat(),
        "formalAdmissionStatus": "NOT_ADMITTED",
    }
    bundle_id = PREFIX + hashlib.sha256(
        f"{SCHEMA}|{INSTRUMENT_ID}|{H125}|{'|'.join(sorted(fact_ids))}".encode("utf-8")
    ).hexdigest()[:32]
    return facts, FactReviewBundle(bundle_id, _canonical(evidence)), evidence


def prepare_000422_h125_parent_equity_version_chain(
    *, archive, calendar_evidence: Mapping,
) -> tuple[tuple[FinancialFact, FinancialFact], FactReviewBundle, dict]:
    """Replay the pinned source packet and prepare, without persisting, the pair."""
    from aquant.adapters.providers import s2_calendar_evidence
    rights = default_rights().get("cninfo")
    if rights is None or not (rights.open_for("research_use") and rights.open_for("local_storage")):
        raise H125ParentEquity000422ContractError("CNINFO research/storage rights are closed")
    if not isinstance(calendar_evidence, Mapping) or not calendar_evidence:
        raise H125ParentEquity000422ContractError("verified calendar evidence is required")
    if archive.con.execute("PRAGMA query_only").fetchone()[0] != 1:
        raise H125ParentEquity000422ContractError("source archive must be query-only")
    source = _source_evidence(archive)
    started = _utc_now()
    calendar_checked_at = _utc_now()
    trading_calendar, verified_calendar = s2_calendar_evidence.verify_calendar_evidence(
        dict(calendar_evidence), archive, now=calendar_checked_at,
        published_on=date.fromisoformat(REPORTS["ORIGINAL_CURRENT"]["publishedDate"]),
    )
    if verified_calendar != calendar_evidence:
        raise H125ParentEquity000422ContractError("calendar evidence differs from read-only replay")
    completed = _utc_now()
    if completed < started:
        raise H125ParentEquity000422ContractError("writer clock moved backwards")
    return _assemble_bundle(
        source, calendar_evidence=verified_calendar,
        calendar_checked_at=calendar_checked_at, completed=completed,
        trading_calendar=list(trading_calendar),
    )


def promote_000422_h125_parent_equity_version_chain(
    *, archive, repository: FinancialFactRepository, calendar_evidence: Mapping,
) -> tuple[FinancialFact, FinancialFact]:
    """Append the two source versions through the normal repository writer."""
    rights = default_rights().get("cninfo")
    if rights is None or not (rights.open_for("research_use") and rights.open_for("local_storage")):
        raise H125ParentEquity000422ContractError("CNINFO research/storage rights are closed")
    if any(fact.instrument_id == INSTRUMENT_ID and fact.period_end == H125 and fact.metric == METRIC
           for fact in repository.load().facts):
        raise H125ParentEquity000422ContractError("target parent-equity fact versions already exist")
    facts, bundle, _ = prepare_000422_h125_parent_equity_version_chain(
        archive=archive, calendar_evidence=calendar_evidence,
    )
    repository.append_many(facts, review_bundle=bundle)
    return facts


class _ReadOnlyArchive:
    def __init__(self, con: sqlite3.Connection, root: Path) -> None:
        self.con, self.root = con, root.resolve()

    def verify(self, content_hash: str) -> bool:
        """Match ForwardArchive.verify using the checked, read-only byte loader."""
        try:
            self.load_bytes(content_hash)
        except (KeyError, OSError, ValueError):
            return False
        return True

    def load_bytes(self, content_hash: str) -> bytes:
        row = self.con.execute(
            "SELECT stored_path,byte_size FROM raw_artifact WHERE content_hash=?", (content_hash,),
        ).fetchone()
        if row is None:
            raise KeyError(f"raw archive object is missing: {content_hash}")
        path = resolve_archive_member(self.root, str(row["stored_path"]))
        body = path.read_bytes()
        if len(body) != int(row["byte_size"]) or _sha(body) != content_hash:
            raise ValueError(f"raw archive object hash/size differs: {content_hash}")
        return body


def _open_readonly_archive() -> tuple[_ReadOnlyArchive, sqlite3.Connection]:
    root = ARCHIVE_ROOT.resolve(strict=True)
    database = ARCHIVE_DATABASE.resolve(strict=True)
    if database != (root / "meta.sqlite").resolve(strict=True):
        raise H125ParentEquity000422ContractError("configured source DB is outside the pinned archive root")
    con = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    if con.execute("PRAGMA query_only").fetchone()[0] != 1:
        con.close()
        raise H125ParentEquity000422ContractError("source archive reader is not query-only")
    con.execute("BEGIN")
    return _ReadOnlyArchive(con, root), con


def accepts_000422_h125_parent_equity_version_chain(
    row: Mapping, member_ids: set[str], facts: Mapping[str, FinancialFact], *,
    accepted_links: Mapping[str, tuple[str, str]],
) -> bool:
    """Fail-closed reader replay of the pinned source closure, pair, and PIT lineage."""
    source_connection = None
    try:
        from aquant.adapters.providers import s2_calendar_evidence
        evidence_json = row["evidence_json"]
        evidence = json.loads(evidence_json)
        if (_canonical(evidence) != evidence_json or row["evidence_hash"] != _sha(evidence_json.encode("utf-8")) or
                evidence.get("schema") != SCHEMA or evidence.get("verifierId") != VERIFIER_ID or
                evidence.get("humanReviewClaimed") is not None or
                evidence.get("rightsRegisterVersion") != REGISTER_VERSION or
                evidence.get("sourceId") != "cninfo" or evidence.get("instrumentId") != ISSUER_ID or
                evidence.get("factPeriodEnd") != H125.isoformat() or evidence.get("metricSet") != [METRIC] or
                evidence.get("formalAdmissionStatus") != "NOT_ADMITTED" or
                evidence.get("candidateDisposition") != "SAME_DATE_EQUAL_VALUE_NO_EFFECT_VALUE_ONLY" or
                evidence.get("causalAttribution") is not None):
            return False
        version_map = evidence.get("versionIdsByMetric", {})
        version_ids = version_map.get(METRIC, ()) if isinstance(version_map, Mapping) else ()
        if (set(version_map) != {METRIC} or len(version_ids) != 2 or len(set(version_ids)) != 2 or
                member_ids != set(version_ids) or member_ids != set(evidence.get("factVersionIds", ())) or
                not member_ids <= facts.keys() or member_ids & accepted_links.keys()):
            return False
        expected_bundle_id = PREFIX + hashlib.sha256(
            f"{SCHEMA}|{INSTRUMENT_ID}|{H125}|{'|'.join(sorted(member_ids))}".encode("utf-8")
        ).hexdigest()[:32]
        if (row["bundle_id"] != expected_bundle_id or evidence.get("sourceQualificationSha256") !=
                "sha256:ca9b875539978d7889b97881a7a837ddbb2cf5316089c26e8244e4bcce8e5371"):
            return False

        archive, source_connection = _open_readonly_archive()
        source = _source_evidence(
            archive, saved_four_flow_impact=evidence["sourceEvidence"]["fourFlowImpact"],
        )
        if source != evidence.get("sourceEvidence") or archive.con.total_changes != 0:
            return False
        completed = _aware(evidence["completedAt"], "completedAt")
        calendar_checked_at = _aware(evidence["calendarCheckedAt"], "calendarCheckedAt")
        if calendar_checked_at > completed or completed > datetime.now(timezone.utc):
            return False
        trading_calendar, verified_calendar = s2_calendar_evidence.verify_calendar_evidence(
            evidence.get("calendarEvidence"), archive, now=calendar_checked_at,
            published_on=date.fromisoformat(REPORTS["ORIGINAL_CURRENT"]["publishedDate"]),
        )
        if ([day.isoformat() for day in trading_calendar] != evidence.get("tradingCalendar") or
                verified_calendar != evidence.get("calendarEvidence")):
            return False
        rebuilt, rebuilt_bundle, rebuilt_evidence = _assemble_bundle(
            source, calendar_evidence=verified_calendar,
            calendar_checked_at=calendar_checked_at, completed=completed,
            trading_calendar=list(trading_calendar),
        )
        if _canonical(rebuilt_evidence) != evidence_json or rebuilt_bundle.bundle_id != row["bundle_id"]:
            return False
        source_connection.execute("ROLLBACK")
        if source_connection.total_changes != 0:
            return False
        source_connection.close()
        source_connection = None

        by_version = {str(fact.version_id): fact for fact in facts.values()}
        expected_facts = {fact.version_id: fact for fact in rebuilt}
        if set(expected_facts) != member_ids:
            return False
        for version_id, expected_fact in expected_facts.items():
            saved = by_version.get(version_id)
            if (saved is None or saved != expected_fact or saved.instrument_id != INSTRUMENT_ID or
                    saved.metric != METRIC or saved.period_end != H125 or
                    saved.statement_scope is not StatementScope.CONSOLIDATED or saved.profit_scope is not None or
                    saved.currency != "CNY" or saved.raw_unit != "yuan" or saved.value != VALUE_YUAN or
                    saved.source_id != "cninfo" or saved.availability_basis is not AvailabilityBasis.OBSERVED or
                    saved.pit_mode is not PitMode.LIVE_OBSERVED):
                return False
        return True
    except (OSError, sqlite3.Error, KeyError, IndexError, AttributeError, TypeError, ValueError, ArithmeticError):
        return False
    finally:
        if source_connection is not None:
            try:
                source_connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            source_connection.close()


__all__ = [
    "SCHEMA", "VERIFIER_ID", "PREFIX", "H125ParentEquity000422ContractError",
    "prepare_000422_h125_parent_equity_version_chain",
    "promote_000422_h125_parent_equity_version_chain",
    "accepts_000422_h125_parent_equity_version_chain",
]
