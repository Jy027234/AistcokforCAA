"""Finite private H125 profit chain for 000680.

The two source roles are H125 CURRENT and the source-native H125 PRIOR cells
presented in the H126 report.  This candidate preserves AFFECTS /
NOT_ADMITTED with null cause/proof fields.  It never routes a PRIOR cell through
the strict CURRENT report factory and only appends through the normal
FinancialFactRepository.append_many API.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qs, urlsplit

import pdfplumber
from pypdf import PdfReader

from aquant.adapters.providers.cninfo import REPORT_QUERY_URL
from aquant.adapters.providers.cninfo_probe import parse_listing_payload, parse_report_period
from aquant.adapters.providers.cninfo_statement_geometry import extract_grid_statement_cells
from aquant.adapters.providers.cninfo_statement_sections import (
    ROLE_CONSOLIDATED, extract_section_facts, extract_statement_sections,
)
from aquant.adapters.providers.pdf_promotion import _archived_ok, _next_available_snapshot, _utc_now
from aquant.adapters.providers.s2_calendar_evidence import verify_calendar_evidence
from aquant.adapters.providers.s2_machine_verification import verify_machine_evidence
from aquant.artifact_paths import resolve_archive_member
from aquant.domain.data.pit import (
    AvailabilityBasis, PitMode, TimestampPrecision, date_only_available_at,
)
from aquant.domain.data.rights import REGISTER_VERSION, default_rights
from aquant.domain.fundamentals.fact_repository import FactReviewBundle, FinancialFactRepository
from aquant.domain.fundamentals.versioned import FinancialFact, ProfitScope, StatementScope

from .h125_presented_prior_verification_000680 import verify_h126_presented_h125_prior
from .h125_profit_pair_000680_review_inputs import (
    CALENDAR_EVIDENCE, CALENDAR_EVIDENCE_SHA256,
    QUALIFICATION_REPLAY_INPUT, RELATIONSHIP_PACKET_SOURCE_SHA256,
    RELATIONSHIP_SOURCE_REPLAY, RELATIONSHIP_SOURCE_AUDIT_SHA256,
)


SCHEMA = "aquant.cninfo.000680-h125-profit-pair-presented-prior-chain.v2"
VERIFIER_ID = "machine:cninfo-000680-h125-profit-pair-v2"
INSTRUMENT_ID = "000680"
COMPANY = "山推工程机械股份有限公司"
ORG_ID = "gssz0000680"
H125 = date(2025, 6, 30)
H126 = date(2026, 6, 30)
METRICS = ("net_profit_attributable", "net_profit_consolidated")
H125_REPORT = {
    "announcementId": "1224561048",
    "receiptId": "rcpt_7252c19331f45502f6232b6c",
    "contentHash": "sha256:d4dca5dcec17316a8094b65585454ed4e2e7fdc2af1e6e51f9935e075e39a39b",
    "documentUrl": "https://static.cninfo.com.cn/finalpage/2025-08-23/1224561048.PDF",
    "publishedDate": "2025-08-23",
    "regularIndexReceiptId": "rcpt_c3a48577837f082d5bb7c15a",
}
H126_REPORT = {
    "announcementId": "1225466047",
    "receiptId": "rcpt_f9e1e51d222c5366dc027f6d",
    "contentHash": "sha256:c7dddb4574fc5705eee1dc09b2733b091492537c0448c3bd1bd2f7ee9f849c90",
    "documentUrl": "https://static.cninfo.com.cn/finalpage/2026-08-11/1225466047.PDF",
    "publishedDate": "2026-08-11",
    "regularIndexReceiptId": "rcpt_9511b2bb947b426fe3887b99",
}
ORG_LOOKUP_RECEIPT_ID = "rcpt_5c103e2d113ba0f73c77041d"
ARCHIVE_ROOT = Path(os.environ.get(
    "AQUANT_000680_ARCHIVE_ROOT",
    "/app/deploy/agentctl-q0/forward-archive",
))
ARCHIVE_DATABASE = Path(os.environ.get(
    "AQUANT_000680_ARCHIVE_DATABASE",
    "/app/deploy/agentctl-q0/forward-archive/meta.sqlite",
))
SCALE = {"元": Decimal(1), "千元": Decimal(1000), "万元": Decimal(10000)}
_FULL_REPORT_TITLE = re.compile(r"报告(?:全文)?(?:[（(][^（）()]{1,40}[）)])?$")


class H125ProfitPairContractError(ValueError):
    """A source, PIT, or exact 000680 H125 profit-pair contract failed."""


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _aware(value: str | datetime, label: str) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise H125ProfitPairContractError(f"{label} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


class ReadOnlyArchive:
    """Archive view over a mode=ro/query_only WAL snapshot."""

    def __init__(self, con: sqlite3.Connection, root: Path) -> None:
        self.con = con
        self.root = root.resolve()

    def load_bytes(self, content_hash: str) -> bytes:
        row = self.con.execute(
            "SELECT stored_path,byte_size FROM raw_artifact WHERE content_hash=?",
            (content_hash,),
        ).fetchone()
        if row is None:
            raise KeyError(f"raw archive object is missing: {content_hash}")
        path = resolve_archive_member(self.root, str(row["stored_path"]))
        body = path.read_bytes()
        if len(body) != int(row["byte_size"]) or _sha(body) != content_hash:
            raise ValueError(f"raw archive object hash/size differs: {content_hash}")
        return body

    def verify(self, content_hash: str) -> bool:
        try:
            self.load_bytes(content_hash)
            return True
        except (OSError, KeyError, ValueError):
            return False


def _receipt(archive: ReadOnlyArchive, receipt_id: str) -> dict:
    row = archive.con.execute(
        "SELECT * FROM fetch_receipt WHERE receipt_id=?", (receipt_id,),
    ).fetchone()
    if row is None:
        raise H125ProfitPairContractError(f"required archived receipt is missing: {receipt_id}")
    return dict(row)


def _official_org_lookup(archive: ReadOnlyArchive) -> dict:
    receipt, payload = _archived_ok(
        archive, ORG_LOOKUP_RECEIPT_ID,
        expected_url="https://www.cninfo.com.cn/new/information/topSearch/query?keyWord=000680&maxNum=10",
        expected_host="www.cninfo.com.cn",
    )
    try:
        rows = json.loads(payload)
    except (UnicodeError, ValueError) as exc:
        raise H125ProfitPairContractError("reused issuer lookup response is not JSON") from exc
    matches = [row for row in rows if isinstance(row, Mapping)
               and row.get("code") == INSTRUMENT_ID and row.get("orgId") == ORG_ID]
    if len(matches) != 1:
        raise H125ProfitPairContractError("reused Nov 3 official lookup does not uniquely bind 000680/orgId")
    return {
        "receiptId": receipt["receipt_id"], "requestedAt": receipt["requested_at"],
        "firstSeenAt": receipt["first_seen_at"], "url": receipt["url"],
        "contentHash": receipt["content_hash"], "organizationId": ORG_ID,
        "issuerRow": dict(matches[0]),
    }


def _regular_index(archive: ReadOnlyArchive, *, receipt_id: str,
                   report_period: date, expected_announcement_id: str,
                   current_h126: bool) -> dict:
    receipt, payload = _archived_ok(archive, receipt_id, expected_host="www.cninfo.com.cn")
    if receipt["url"] != REPORT_QUERY_URL:
        raise H125ProfitPairContractError("period index receipt is not the exact normal report-index endpoint")
    prefix = (f"report-index:{INSTRUMENT_ID}:{report_period.isoformat()}:page=1:request=")
    detail = str(receipt.get("detail") or "")
    if not detail.startswith(prefix):
        raise H125ProfitPairContractError("report-index receipt detail does not bind the requested code/period/page")
    request_hash = detail.removeprefix(prefix)
    request_bytes = archive.load_bytes(request_hash)
    form = {key: values[0] if len(values) == 1 else values
            for key, values in parse_qs(request_bytes.decode("utf-8"), keep_blank_values=True,
                                        strict_parsing=True).items()}
    expected_form = {
        "pageNum": "1", "pageSize": "30", "column": "szse", "tabName": "fulltext",
        "plate": "", "stock": f"{INSTRUMENT_ID},{ORG_ID}", "searchkey": "", "secid": "",
        "category": "category_bndbg_szsh", "trade": "",
        "seDate": f"{report_period.isoformat()}~2026-11-03",
        "sortName": "", "sortType": "", "isHLtitle": "true",
    }
    if form != expected_form:
        raise H125ProfitPairContractError("regular index POST body differs from exact code/org/period/complete-window gate")
    found, skipped, total, has_more = parse_listing_payload(
        payload, stock_code=INSTRUMENT_ID, period=parse_report_period(report_period.isoformat()),
        organization_id=ORG_ID,
    )
    try:
        document = json.loads(payload)
    except (UnicodeError, ValueError) as exc:
        raise H125ProfitPairContractError("regular report-index response is not JSON") from exc
    raw_rows = document.get("announcements")
    if not isinstance(raw_rows, list) or len(found) + sum(skipped.values()) != len(raw_rows):
        raise H125ProfitPairContractError("regular report-index rows have incomplete identity parsing")
    matches = [item for item in found if item.announcement_id == expected_announcement_id]
    if len(matches) != 1:
        raise H125ProfitPairContractError("expected full report is not unique in the regular index")
    target = matches[0]
    expected_url = H126_REPORT["documentUrl"] if current_h126 else H125_REPORT["documentUrl"]
    if (target.sec_code != INSTRUMENT_ID or target.url != expected_url or
            target.report_period != report_period.isoformat()):
        raise H125ProfitPairContractError("regular index target code/report period/PDF URL differs")
    full_reports = [item for item in found
                    if _FULL_REPORT_TITLE.search(re.sub(r"\s+", "", item.title))
                    and "摘要" not in item.title and "英文" not in item.title]
    if current_h126 and (has_more is not False or total != len(raw_rows) or
                         len(full_reports) != 1 or full_reports[0].announcement_id != expected_announcement_id):
        raise H125ProfitPairContractError("H126 current regular index is incomplete or has another full report")
    if not current_h126 and (has_more is not False or total != len(raw_rows)):
        raise H125ProfitPairContractError("H125 regular index no longer reaches its explicit complete page")
    return {
        "receiptId": receipt["receipt_id"], "requestBodyHash": request_hash,
        "responseHash": receipt["content_hash"], "requestedAt": receipt["requested_at"],
        "firstSeenAt": receipt["first_seen_at"], "httpStatus": receipt["http_status"],
        "outcome": receipt["outcome"], "page": 1, "pageSize": form["pageSize"],
        "rawAnnouncementCount": len(raw_rows), "totalAnnouncement": total,
        "hasMore": has_more, "skipped": skipped,
        "target": {
            "announcementId": target.announcement_id, "secCode": target.sec_code,
            "title": target.title, "documentUrl": target.url,
            "reportPeriod": target.report_period,
        "publishedDate": date.fromisoformat(target.announcement_date_cn).isoformat(),
        },
        "fullReportMatches": [item.announcement_id for item in full_reports],
    }


def _source_report(archive: ReadOnlyArchive, binding: Mapping[str, str]) -> tuple[dict, bytes]:
    receipt, body = _archived_ok(
        archive, binding["receiptId"], expected_url=binding["documentUrl"],
        expected_host="static.cninfo.com.cn",
    )
    if (receipt["content_hash"] != binding["contentHash"] or
            len(body) != int(receipt["byte_size"])):
        raise H125ProfitPairContractError("bound report receipt/hash/size differs")
    try:
        published = date.fromisoformat(binding["publishedDate"])
    except (KeyError, TypeError, ValueError) as exc:
        raise H125ProfitPairContractError("report publication date is invalid") from exc
    if published.isoformat() != binding["publishedDate"]:
        raise H125ProfitPairContractError("report publication date is not canonical ISO")
    return receipt, body


def _native_prior_period_binding(body: bytes) -> dict:
    pdf = PdfReader(io.BytesIO(body))
    if len(pdf.pages) != 191:
        raise H125ProfitPairContractError("H126 source PDF page count differs from the bound report")
    pages = {n: (pdf.pages[n - 1].extract_text() or "") for n in (7, 182, 184)}
    compact = {n: re.sub(r"\s+", "", value) for n, value in pages.items()}
    required = {
        "title": "2026年半年度报告全文" in compact[7],
        "sameReportCurrentPriorComparator": "本报告期" in compact[7] and "上年同期" in compact[7],
        "attributableAdjustmentBeforeAfterRow": all(value in compact[7] for value in (
            "归属于上市公司股东的净利润", "681,076,003.83", "568,285,466.94",
            "572,191,955.66", "调整前", "调整后")),
        "datedReportStatement": "2026年6月30日" in compact[182] and "2026年半年度" in compact[184],
        "sameIncomeStatementPriorColumn": "本期发生额" in compact[184] and "上期发生额" in compact[184],
        "attributablePriorCellCrossCheck": "572,191,955.66" in compact[184],
        "consolidatedPriorCell": "573,906,897.04" in compact[184],
    }
    if not all(required.values()):
        raise H125ProfitPairContractError("H126 report-native same-period binding did not pass")
    quotes = {
        str(n): [line for line in pages[n].splitlines()
                 if any(term in line for term in (
                     "2026年半年度", "本报告期", "上年同期", "调整前", "调整后",
                     "归属于上市公司股东的净利", "2026年 6月 30 日",
                     "合并利润表", "本期发生额", "上期发生额", "572,191,955.66",
                     "573,906,897.04"))]
        for n in (7, 182, 184)
    }
    coordinates = []
    with pdfplumber.open(io.BytesIO(body)) as document:
        for physical, terms in {
            7: ("本报告期", "上年同期", "调整前", "调整后", "572,191,955.66"),
            182: ("2026年", "6月", "30", "单位：元"),
            184: ("本期发生额", "上期发生额", "572,191,955.66", "573,906,897.04"),
        }.items():
            page = document.pages[physical - 1]
            for term in terms:
                for match in page.search(re.escape(term)):
                    coordinates.append({
                        "physicalPage": physical, "searchText": term,
                        "matchedText": match.get("text", ""),
                        "bbox": [round(float(match[key]), 3)
                                 for key in ("x0", "top", "x1", "bottom")],
                    })
    return {
        "status": "PASS_SOURCE_NATIVE_SAME_REPORT_PRIOR_PERIOD_BINDING",
        "factPeriodEnd": H125.isoformat(), "sourceReportPeriodEnd": H126.isoformat(),
        "resolution": "SAME_REPORT_NATIVE_UPPER_YEAR_COMPARATOR_AND_DATED_REPORT_PERIOD",
        "notDerivedFromRequestedYearMinusOne": True,
        "pages": {str(key): value for key, value in required.items()},
        "nativeQuotesByPhysicalPage": quotes,
        "nativeTextBoundingBoxes": coordinates,
    }


def _pair_source_evidence(archive: ReadOnlyArchive, qualification: Mapping | None = None) -> dict:
    org = _official_org_lookup(archive)
    h125_index = _regular_index(
        archive, receipt_id=H125_REPORT["regularIndexReceiptId"], report_period=H125,
        expected_announcement_id=H125_REPORT["announcementId"], current_h126=False,
    )
    h126_index = _regular_index(
        archive, receipt_id=H126_REPORT["regularIndexReceiptId"], report_period=H126,
        expected_announcement_id=H126_REPORT["announcementId"], current_h126=True,
    )
    for name, index, binding, expected_period in (
        ("H125", h125_index, H125_REPORT, H125.isoformat()),
        ("H126", h126_index, H126_REPORT, H126.isoformat()),
    ):
        target = index["target"]
        if (target["announcementId"] != binding["announcementId"] or
                target["documentUrl"] != binding["documentUrl"] or
                target["publishedDate"] != binding["publishedDate"] or
                target["reportPeriod"] != expected_period):
            raise H125ProfitPairContractError(f"{name} regular index and bound report identity differ")
    h125_receipt, h125_body = _source_report(archive, H125_REPORT)
    h126_receipt, h126_body = _source_report(archive, H126_REPORT)
    current_candidate = extract_section_facts(
        h125_body, instrument_id=INSTRUMENT_ID, company=COMPANY, period_end=H125,
        announcement_id=H125_REPORT["announcementId"],
        document_url=H125_REPORT["documentUrl"], required_fields=METRICS,
    )
    current_strict = verify_machine_evidence(
        h125_body, candidate=current_candidate, expected_fields=METRICS,
    )
    if not current_strict.verified:
        raise H125ProfitPairContractError("H125 CURRENT did not pass the original strict verifier")
    current_sections = extract_statement_sections(
        h125_body, period_end=H125, roles=(ROLE_CONSOLIDATED,), statements=("利润表",),
    ).require(ROLE_CONSOLIDATED, "利润表")
    if current_sections.unit != "元" or current_sections.current_header != "2025年半年度":
        raise H125ProfitPairContractError("H125 consolidated CURRENT statement period/unit differs")
    current_grid = {
        cell.field: cell for cell in extract_grid_statement_cells(
            h125_body, instrument_id=INSTRUMENT_ID, period_end=H125,
        ) if cell.field in METRICS and cell.statement == "利润表"
    }
    if set(current_grid) != set(METRICS):
        raise H125ProfitPairContractError("H125 independent CURRENT grid did not return both profit fields")
    current_values: dict[str, Decimal] = {}
    current_events = {}
    for metric in METRICS:
        row = current_sections.find(metric)
        if row is None or row.nil_amounts or row.unit != "元":
            raise H125ProfitPairContractError(f"H125 CURRENT {metric} row/unit is incomplete")
        strict_value = current_candidate.by_field[metric].value_yuan
        grid_value = current_grid[metric].amount_yuan
        if strict_value != grid_value or grid_value != row.amount:
            raise H125ProfitPairContractError(f"H125 CURRENT strict/section/grid disagreement for {metric}")
        current_values[metric] = Decimal(str(strict_value))
        current_events[metric] = {
            "announcementId": H125_REPORT["announcementId"], "receiptId": h125_receipt["receipt_id"],
            "contentHash": H125_REPORT["contentHash"], "documentUrl": H125_REPORT["documentUrl"],
            "publishedDate": H125_REPORT["publishedDate"], "firstSeenAt": h125_receipt["first_seen_at"],
            "sourceReportPeriodEnd": H125.isoformat(), "factPeriodEnd": H125.isoformat(),
            "columnRole": "CURRENT", "columnHeader": current_sections.current_header,
            "currentColumnHeader": current_sections.current_header,
            "priorColumnHeader": current_sections.prior_header,
            "statement": "利润表", "statementScope": "CONSOLIDATED", "currency": "CNY",
            "amountUnit": row.unit, "rawUnit": "yuan", "amountYuan": str(strict_value),
            "page": row.page, "rowLabel": row.label, "sourceRow": row.raw_line,
            "currentCell": row.current_cell,
            "independentGrid": current_grid[metric].as_evidence(),
        }

    h126_sections = extract_statement_sections(
        h126_body, period_end=H126, roles=(ROLE_CONSOLIDATED,), statements=("利润表",),
    ).require(ROLE_CONSOLIDATED, "利润表")
    if h126_sections.unit != "元" or h126_sections.current_header != "本期发生额" or h126_sections.prior_header != "上期发生额":
        raise H125ProfitPairContractError("H126 CURRENT/PRIOR columns or unit differ from the source table")
    h126_prior_values = {}
    h126_prior_events = {}
    for metric in METRICS:
        row = h126_sections.find(metric)
        if row is None or row.nil_amounts or row.unit != "元" or row.prior_amount is None:
            raise H125ProfitPairContractError(f"H126 PRIOR {metric} row/cell/unit is incomplete")
        h126_prior_values[metric] = Decimal(str(row.prior_amount))
        h126_prior_events[metric] = {
            "announcementId": H126_REPORT["announcementId"], "receiptId": h126_receipt["receipt_id"],
            "contentHash": H126_REPORT["contentHash"], "documentUrl": H126_REPORT["documentUrl"],
            "publishedDate": H126_REPORT["publishedDate"], "firstSeenAt": h126_receipt["first_seen_at"],
            "sourceReportPeriodEnd": H126.isoformat(), "factPeriodEnd": H125.isoformat(),
            "columnRole": "PRIOR", "columnHeader": h126_sections.prior_header,
            "currentColumnHeader": h126_sections.current_header,
            "statement": "利润表", "statementScope": "CONSOLIDATED", "currency": "CNY",
            "amountUnit": row.unit, "rawUnit": "yuan", "amountYuan": str(row.prior_amount),
            "page": row.page, "rowLabel": row.label, "sourceRow": row.raw_line,
            "currentCell": row.current_cell, "priorCell": row.prior_cell,
        }
    prior_grid = verify_h126_presented_h125_prior(
        h126_body, source_report_period_end=H126, fact_period_end=H125,
        expected_values_yuan={metric: str(h126_prior_values[metric]) for metric in METRICS},
    )
    if prior_grid.get("status") != "VERIFIED":
        raise H125ProfitPairContractError("H126 PRIOR independent ruled-grid verification failed")
    for metric in METRICS:
        checked = prior_grid.get("nativePriorRows", {}).get(metric, {})
        try:
            checked_cell_amount = Decimal(str(checked.get("priorCell")).replace(",", "").replace(" ", ""))
            section_cell_amount = Decimal(str(h126_prior_events[metric]["priorCell"]).replace(",", "").replace(" ", ""))
        except (ArithmeticError, TypeError, ValueError):
            raise H125ProfitPairContractError(f"H126 PRIOR selected cell is not a finite amount for {metric}")
        if (Decimal(checked.get("priorAmountYuan")) != h126_prior_values[metric] or
                checked_cell_amount != section_cell_amount or
                checked_cell_amount != h126_prior_values[metric] or
                checked.get("page") != h126_prior_events[metric]["page"]):
            raise H125ProfitPairContractError(f"H126 PRIOR independent grid/section disagreement for {metric}")
        h126_prior_events[metric]["independentPriorGrid"] = {
            "schema": prior_grid["schema"], "status": prior_grid["status"],
            "sourcePdfSha256": prior_grid["sourcePdfSha256"],
            "statement": prior_grid["statement"],
            "currentHeader": prior_grid["statement"]["currentHeader"],
            "priorHeader": prior_grid["statement"]["priorHeader"],
            "headerCellBounds": prior_grid["statement"]["headerCellBounds"],
            "headerPage": prior_grid["statement"]["headerPage"],
            "tableBounds": prior_grid["statement"]["headerTableBounds"],
            "row": checked,
            "gridAnchor": prior_grid["gridAnchors"][metric],
        }
    native_binding = _native_prior_period_binding(h126_body)
    if qualification is not None:
        _verify_existing_impact_classification(qualification, current_values, h126_prior_values)
    return {
        "organizationLookup": org,
        "regularIndexes": {"H125_CURRENT": h125_index, "H126_PRIOR_REPORT": h126_index},
        "reports": {
            "H125_CURRENT": {"receipt": h125_receipt, "contentHashVerified": _sha(h125_body) == H125_REPORT["contentHash"]},
            "H126_PRIOR_REPORT": {"receipt": h126_receipt, "contentHashVerified": _sha(h126_body) == H126_REPORT["contentHash"]},
        },
        "currentStrict": {
            "called": "verify_machine_evidence(extract_section_facts(... expected_fields=METRICS))",
            "verified": True, "scope": "H125_CURRENT_FIELDS_ONLY",
            "verifiedFields": list(METRICS), "valuesYuan": {key: str(value) for key, value in current_values.items()},
            "candidateEvidence": current_strict.as_evidence(),
        },
        "currentGrid": {key: current_grid[key].as_evidence() for key in METRICS},
        "priorStrict": {"status": "NOT_VERIFIED_AS_PRIOR_FACTS",
                        "note": "H126 strict CURRENT verification is not a strict verification of H126 PRIOR facts."},
        "priorGrid": prior_grid,
        "nativePeriodBinding": native_binding,
        "values": {
            metric: {
                "H125_CURRENT": current_events[metric],
                "H126_PRIOR": h126_prior_events[metric],
                "deltaYuan": str(h126_prior_values[metric] - current_values[metric]),
            }
            for metric in METRICS
        },
    }


def _verify_existing_impact_classification(qualification: Mapping,
                                           current_values: Mapping[str, Decimal],
                                           prior_values: Mapping[str, Decimal]) -> dict:
    classifications = qualification.get("impactClassification")
    if not isinstance(classifications, Mapping):
        raise H125ProfitPairContractError("original AFFECTS classification is absent")
    result = {}
    for metric in METRICS:
        item = classifications.get(metric)
        if not isinstance(item, Mapping):
            raise H125ProfitPairContractError(f"original impact classification is missing for {metric}")
        current = item.get("current", {})
        prior = item.get("presentedPrior", {})
        if (item.get("status") != "AFFECTS" or item.get("admissionStatus") != "NOT_ADMITTED" or
                item.get("impactProof") is not None or item.get("causalAttribution") is not None or
                current.get("announcementId") != H125_REPORT["announcementId"] or
                current.get("columnRole") != "CURRENT" or
                Decimal(str(current.get("amountYuan"))) != current_values[metric] or
                prior.get("announcementId") != H126_REPORT["announcementId"] or
                prior.get("columnRole") != "PRIOR" or
                Decimal(str(prior.get("amountYuan"))) != prior_values[metric] or
                current_values[metric] == prior_values[metric]):
            raise H125ProfitPairContractError(f"{metric}: refuse any status/cause rewrite or stale value pair")
        result[metric] = {
            "status": "AFFECTS", "admissionStatus": "NOT_ADMITTED",
            "impactProof": None, "causalAttribution": None,
            "current": dict(current), "presentedPrior": dict(prior),
            "deltaYuan": str(prior_values[metric] - current_values[metric]),
        }
    return result


def _verify_relationship_source_replay(archive: ReadOnlyArchive, replay: Mapping) -> dict:
    """Replay the bounded Source1 cross-index and its 23 source documents.

    The review row carries the replay inputs. This function re-reads every
    page receipt, request body, response body, and linked PDF from the canonical
    archive; no task-local qualification file is consulted.
    """
    if _canonical(replay) != _canonical(RELATIONSHIP_SOURCE_REPLAY):
        raise H125ProfitPairContractError("persisted Source1 replay input differs from the pinned finite cohort")
    if replay.get("schema") != "aquant.000680.noeffect-four.relationship-packet.v3":
        raise H125ProfitPairContractError("relationship source replay schema differs")
    company = replay.get("company")
    if not isinstance(company, Mapping) or (
            company.get("code") != INSTRUMENT_ID or
            company.get("name") != "山推股份" or
            company.get("instrumentId") != "SZ.000680" or
            company.get("organizationId") != ORG_ID or
            company.get("orgLookupReceiptId") != ORG_LOOKUP_RECEIPT_ID or
            company.get("publicationWindow") != ["2024-06-30", "2026-11-03"] or
            company.get("allCategoryTermination") != "has_more_false" or
            company.get("allCategoryTotalAnnouncement") != 359):
        raise H125ProfitPairContractError("Source1 relationship index issuer/window/termination binding differs")

    from aquant.adapters.providers import s2_revision_impact as impact

    entries, _, _ = impact._index(archive.con, archive.root, dict(company))
    if len(entries) != 359:
        raise H125ProfitPairContractError("Source1 all-category index replay did not return 359 unique entries")

    report_periods = {
        announcement_id: parsed.isoformat()
        for announcement_id, entry in entries.items()
        if (parsed := impact._report_period(entry["title"], issuer_code=INSTRUMENT_ID)) is not None
        and parsed.isoformat() in impact.PERIODS
        and impact.disclosure_role(entry["title"]) in (
            impact.DisclosureRole.ORIGINAL_REPORT, impact.DisclosureRole.REVISED_REPORT,
        )
    }
    fundraising_ids = {
        announcement_id for announcement_id, entry in entries.items()
        if impact._is_fundraising_special_report(entry["title"])
    }
    financial_ids = {
        announcement_id for announcement_id, entry in entries.items()
        if impact._financial_title(entry["title"])
    }
    recalled_ids = {
        announcement_id for announcement_id, entry in entries.items()
        if (impact._financial_title(entry["title"]) or
            any(impact.title_revision_flags(entry["title"])[:3]))
    }
    required_ids = set(report_periods) | fundraising_ids | recalled_ids
    docs = replay.get("sourceDocuments")
    if not isinstance(docs, list) or len(docs) != 23:
        raise H125ProfitPairContractError("Source1 relationship replay must include exactly 23 source documents")
    ids = [str(item.get("announcementId")) for item in docs if isinstance(item, Mapping)]
    if len(ids) != len(docs) or len(set(ids)) != 23 or set(ids) != required_ids:
        raise H125ProfitPairContractError("Source1 replay documents do not exactly close required report/revision IDs")

    for item in docs:
        announcement_id = str(item["announcementId"])
        entry = entries.get(announcement_id)
        if entry is None or (
                item.get("title") != entry["title"] or
                item.get("documentUrl") != entry["documentUrl"] or
                item.get("publishedDate") != entry["publishedDate"] or
                item.get("matchesCurrentIndex") is not True or
                item.get("receiptAndRawVerified") is not True):
            raise H125ProfitPairContractError(f"Source1 document {announcement_id} differs from its current index row")
        receipt, body = impact._receipt(
            archive.con, archive.root, str(item.get("receiptId")),
            expected_hash=str(item.get("rawSha256")), expected_url=entry["documentUrl"],
        )
        if (not body.startswith(b"%PDF-") or
                len(body) != int(item.get("byteSize", -1)) or
                _sha(body) != item.get("rawSha256") or
                receipt.get("first_seen_at") != item.get("receiptFirstSeenAt")):
            raise H125ProfitPairContractError(f"Source1 document {announcement_id} receipt/raw replay differs")

    closure = replay.get("sourceClosure")
    expected_closure = {
        "documentCount": 23,
        "currentIndexEntryCount": 359,
        "documentsVerifiedAgainstCurrentCrossIndexAndArchiveReceipts": 23,
        "financialCandidateIds": sorted(financial_ids | set(report_periods) | fundraising_ids),
        "fundraisingSpecialReportIds": sorted(fundraising_ids),
        "missingFundraisingSpecialReportIds": [],
        "allReportCandidatePeriods": dict(sorted(report_periods.items())),
        "allRecalledRevisionIds": sorted(recalled_ids),
        "missingRecalledRevisionIds": [],
        "missingReportSourceIds": [],
        "completeForCurrentCapturedSet": True,
    }
    closure_differences = ({key: {"saved": closure.get(key), "replayed": value}
                            for key, value in expected_closure.items()
                            if closure.get(key) != value}
                           if isinstance(closure, Mapping) else {"sourceClosure": "missing_or_invalid"})
    if closure_differences:
        raise H125ProfitPairContractError(
            "Source1 report/revision closure differs from replayed canonical archive: "
            + _canonical(closure_differences)
        )
    return {
        "completeForCurrentCapturedSet": True,
        "documentsVerifiedAgainstCurrentCrossIndexAndArchiveReceipts": 23,
        "currentIndexEntryCount": 359,
        "missingReportSourceIds": [],
        "missingRecalledRevisionIds": [],
        "impactClassificationImportedFromThisPacket": False,
    }


def _version_id(metric: str, role: str, announcement_id: str, content_hash: str) -> str:
    identity = f"{INSTRUMENT_ID}|{H125.isoformat()}|{metric}|{role}|{announcement_id}|{content_hash}"
    return "mach_000680_h125_profit_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _metric_profit_scope(metric: str) -> ProfitScope:
    if metric == "net_profit_attributable":
        return ProfitScope.ATTRIBUTABLE
    if metric == "net_profit_consolidated":
        return ProfitScope.CONSOLIDATED
    raise H125ProfitPairContractError(f"unsupported profit metric: {metric}")


def _make_facts(source: Mapping, *, ingested_at: datetime,
                available_at: datetime) -> tuple[tuple[FinancialFact, ...], dict[str, list[dict]]]:
    built: list[FinancialFact] = []
    by_metric: dict[str, list[dict]] = {}
    for metric in METRICS:
        events = [source["values"][metric]["H125_CURRENT"],
                  source["values"][metric]["H126_PRIOR"]]
        previous_id = None
        source_rows = []
        for event in events:
            role = "ORIGINAL_CURRENT" if event["columnRole"] == "CURRENT" else "PRESENTED_PRIOR"
            published = date.fromisoformat(event["publishedDate"])
            first_seen = _aware(event["firstSeenAt"], "source firstSeenAt")
            version_id = _version_id(metric, role, event["announcementId"], event["contentHash"])
            fact = FinancialFact(
                instrument_id=INSTRUMENT_ID, metric=metric, period_end=H125,
                statement_scope=StatementScope.CONSOLIDATED,
                profit_scope=_metric_profit_scope(metric),
                value=Decimal(event["amountYuan"]), currency="CNY", raw_unit="yuan",
                source_id="cninfo", source_document_id=event["announcementId"],
                source_published_date=published, source_published_at=None,
                timestamp_precision=TimestampPrecision.DATE,
                first_seen_at=first_seen, ingested_at=ingested_at, available_at=available_at,
                availability_basis=AvailabilityBasis.OBSERVED, pit_mode=PitMode.LIVE_OBSERVED,
                content_hash=event["contentHash"], version_id=version_id,
                supersedes_id=previous_id,
            )
            built.append(fact)
            source_rows.append({**event, "role": role, "versionId": version_id,
                                "supersedesId": previous_id,
                                "ingestedAt": ingested_at.isoformat(),
                                "availableAt": available_at.isoformat(),
                                "currency": "CNY", "rawUnit": "yuan"})
            previous_id = version_id
        by_metric[metric] = source_rows
    return tuple(built), by_metric


def promote_000680_h125_profit_pair(
    *, archive: ReadOnlyArchive, repository: FinancialFactRepository,
    qualification: Mapping | None = None, calendar_evidence: Mapping | None = None,
) -> tuple[FinancialFact, ...]:
    """Re-read the two allowed reports, then append exactly four private facts."""
    qualification = QUALIFICATION_REPLAY_INPUT if qualification is None else qualification
    calendar_evidence = CALENDAR_EVIDENCE if calendar_evidence is None else calendar_evidence
    if _canonical(qualification) != _canonical(QUALIFICATION_REPLAY_INPUT):
        raise H125ProfitPairContractError("writer qualification input differs from the pinned AFFECTS source qualification")
    if _canonical(calendar_evidence) != _canonical(CALENDAR_EVIDENCE):
        raise H125ProfitPairContractError("writer calendar input differs from the pinned source calendar evidence")
    started = _utc_now()
    rights = default_rights().get("cninfo")
    if not (rights.open_for("research_use") and rights.open_for("local_storage")):
        raise H125ProfitPairContractError("CNINFO research/storage rights are closed")
    previous = repository.load()
    if any(fact.instrument_id == INSTRUMENT_ID and fact.period_end == H125 and
           fact.metric in METRICS for fact in previous.facts):
        raise H125ProfitPairContractError("target H125 keys already exist; finite fresh-root writer refuses an append")

    sources = _pair_source_evidence(archive, qualification)
    calendar_checked_at = _utc_now()
    trading_calendar, verified_calendar = verify_calendar_evidence(
        dict(calendar_evidence), archive, now=calendar_checked_at,
        published_on=date.fromisoformat(H125_REPORT["publishedDate"]),
    )
    completed = _utc_now()
    if completed < started:
        raise H125ProfitPairContractError("writer clock moved backwards")
    all_first_seen = [
        _aware(sources["reports"][key]["receipt"]["first_seen_at"], "PDF first_seen_at")
        for key in ("H125_CURRENT", "H126_PRIOR_REPORT")
    ]
    all_first_seen.extend(
        _aware(sources["regularIndexes"][key]["firstSeenAt"], "index first_seen_at")
        for key in ("H125_CURRENT", "H126_PRIOR_REPORT")
    )
    all_first_seen.append(_aware(sources["organizationLookup"]["firstSeenAt"], "org lookup first_seen_at"))
    if any(seen > completed for seen in all_first_seen):
        raise H125ProfitPairContractError("source observation is after writer completion")
    date_floors = [date_only_available_at(date.fromisoformat(H125_REPORT["publishedDate"]), trading_calendar)[0],
                   date_only_available_at(date.fromisoformat(H126_REPORT["publishedDate"]), trading_calendar)[0]]
    available_at = _next_available_snapshot(
        max([completed, calendar_checked_at, *all_first_seen, *date_floors]), trading_calendar,
    )
    facts, version_sources = _make_facts(sources, ingested_at=completed, available_at=available_at)
    impact = _verify_existing_impact_classification(
        qualification,
        {metric: Decimal(sources["values"][metric]["H125_CURRENT"]["amountYuan"]) for metric in METRICS},
        {metric: Decimal(sources["values"][metric]["H126_PRIOR"]["amountYuan"]) for metric in METRICS},
    )
    relationship_replay = json.loads(_canonical(RELATIONSHIP_SOURCE_REPLAY))
    relationship_closure = _verify_relationship_source_replay(archive, relationship_replay)
    ids_by_metric = {metric: [item["versionId"] for item in version_sources[metric]]
                     for metric in METRICS}
    fact_ids = sorted(version for values in ids_by_metric.values() for version in values)
    evidence = {
        "schema": SCHEMA, "verifierId": VERIFIER_ID, "verifierVersion": "1",
        "humanReviewClaimed": None, "rightsRegisterVersion": REGISTER_VERSION,
        "sourceId": "cninfo", "instrumentId": INSTRUMENT_ID,
        "factPeriodEnd": H125.isoformat(), "metricSet": list(METRICS),
        "versionIdsByMetric": ids_by_metric, "factVersionIds": fact_ids,
        "versionSourcesByMetric": version_sources,
        "sourceEvidence": sources,
        "sourceQualificationSha256": QUALIFICATION_REPLAY_INPUT["sourceQualificationSha256"],
        "qualificationReplay": json.loads(_canonical(QUALIFICATION_REPLAY_INPUT)),
        "relationshipPacketSha256": RELATIONSHIP_PACKET_SOURCE_SHA256,
        "relationshipSourceAuditSha256": RELATIONSHIP_SOURCE_AUDIT_SHA256,
        "relationshipSourceReplaySha256": _sha(_canonical(relationship_replay).encode("utf-8")),
        "relationshipSourceReplay": relationship_replay,
        "relationshipSourceClosure": relationship_closure,
        "impactClassification": impact,
        "classificationBoundary": {
            "formalAdmission": "NOT_ADMITTED", "impactStatus": "AFFECTS",
            "impactProof": None, "causalAttribution": None,
            "priorStrictStatus": "NOT_VERIFIED_AS_PRIOR_FACTS",
            "priorGridStatus": "PASS_AMOUNT_CELL_AND_PERIOD_BINDING",
            "currentStrictStatus": "PASS_H125_CURRENT_FIELDS_ONLY",
        },
        "calendarEvidence": verified_calendar,
        "calendarEvidenceSha256": CALENDAR_EVIDENCE_SHA256,
        "calendarCheckedAt": calendar_checked_at.isoformat(),
        "tradingCalendar": [day.isoformat() for day in trading_calendar],
        "completedAt": completed.isoformat(), "availableAt": available_at.isoformat(),
        "formalAdmissionStatus": "NOT_ADMITTED",
    }
    bundle_id = "cninfo_machine_000680_h125_profit_pair_" + hashlib.sha256(
        f"{INSTRUMENT_ID}|{H125}|{'|'.join(fact_ids)}".encode("utf-8")
    ).hexdigest()[:32]
    repository.append_many(facts, review_bundle=FactReviewBundle(bundle_id, _canonical(evidence)))
    return facts


def accepts_000680_h125_profit_pair(
    row: Mapping, member_ids: set[str], facts: Mapping[str, FinancialFact], *,
    accepted_links: Mapping[str, tuple[str, str]],
) -> bool:
    """Private public-reader route for this finite two-metric/two-source cohort."""
    source = None
    try:
        evidence_json = row["evidence_json"]
        evidence = json.loads(evidence_json)
        if (_canonical(evidence) != evidence_json or row["evidence_hash"] != _sha(evidence_json.encode("utf-8")) or
                evidence.get("schema") != SCHEMA or evidence.get("verifierId") != VERIFIER_ID or
                evidence.get("humanReviewClaimed") is not None or
                evidence.get("rightsRegisterVersion") != REGISTER_VERSION or
                evidence.get("instrumentId") != INSTRUMENT_ID or
                evidence.get("factPeriodEnd") != H125.isoformat() or
                evidence.get("metricSet") != list(METRICS) or
                evidence.get("formalAdmissionStatus") != "NOT_ADMITTED"):
            return False
        version_ids = {version for values in evidence.get("versionIdsByMetric", {}).values()
                       for version in values}
        if (set(evidence.get("versionIdsByMetric", {})) != set(METRICS) or
                member_ids != version_ids or member_ids != set(evidence.get("factVersionIds", ())) or
                not member_ids <= facts.keys() or any(version_id in accepted_links for version_id in member_ids)):
            return False
        expected_bundle_id = "cninfo_machine_000680_h125_profit_pair_" + hashlib.sha256(
            f"{INSTRUMENT_ID}|{H125}|{'|'.join(sorted(member_ids))}".encode("utf-8")
        ).hexdigest()[:32]
        if row["bundle_id"] != expected_bundle_id:
            return False
        qualification = evidence.get("qualificationReplay")
        if (not isinstance(qualification, Mapping) or
                _canonical(qualification) != _canonical(QUALIFICATION_REPLAY_INPUT) or
                evidence.get("sourceQualificationSha256") != QUALIFICATION_REPLAY_INPUT["sourceQualificationSha256"] or
                evidence.get("relationshipPacketSha256") != RELATIONSHIP_PACKET_SOURCE_SHA256 or
                evidence.get("relationshipSourceAuditSha256") != RELATIONSHIP_SOURCE_AUDIT_SHA256 or
                evidence.get("calendarEvidenceSha256") != CALENDAR_EVIDENCE_SHA256):
            return False
        db_path = ARCHIVE_DATABASE.resolve(strict=True)
        archive_root = ARCHIVE_ROOT.resolve(strict=True)
        source = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True,
                                 timeout=30, isolation_level=None)
        source.row_factory = sqlite3.Row
        source.execute("PRAGMA query_only=ON")
        if source.execute("PRAGMA query_only").fetchone()[0] != 1:
            source.close()
            source = None
            return False
        source.execute("BEGIN")
        archive = ReadOnlyArchive(source, archive_root)
        source_evidence = _pair_source_evidence(archive, qualification)
        if evidence.get("sourceEvidence") != source_evidence:
            source.execute("ROLLBACK")
            source.close()
            source = None
            return False
        impact = _verify_existing_impact_classification(
            qualification,
            {metric: Decimal(source_evidence["values"][metric]["H125_CURRENT"]["amountYuan"])
             for metric in METRICS},
            {metric: Decimal(source_evidence["values"][metric]["H126_PRIOR"]["amountYuan"])
             for metric in METRICS},
        )
        if evidence.get("impactClassification") != impact:
            source.execute("ROLLBACK")
            source.close()
            source = None
            return False
        relationship_replay = evidence.get("relationshipSourceReplay")
        if (not isinstance(relationship_replay, Mapping) or
                evidence.get("relationshipSourceReplaySha256") !=
                _sha(_canonical(relationship_replay).encode("utf-8"))):
            return False
        relationship_closure = _verify_relationship_source_replay(archive, relationship_replay)
        if evidence.get("relationshipSourceClosure") != relationship_closure:
            return False
        calendar_packet = evidence.get("calendarEvidence")
        ingested_at = _aware(evidence["completedAt"], "writer completedAt")
        calendar_days, rebuilt_calendar = verify_calendar_evidence(
            calendar_packet, archive, now=ingested_at,
            published_on=date.fromisoformat(H125_REPORT["publishedDate"]),
        )
        if (rebuilt_calendar != calendar_packet or
                [day.isoformat() for day in calendar_days] != evidence.get("tradingCalendar")):
            return False
        available = _aware(evidence["availableAt"], "writer availableAt")
        if available != _next_available_snapshot(
                max([ingested_at, _aware(evidence["calendarCheckedAt"], "calendarCheckedAt"),
                     *[_aware(source_evidence["reports"][key]["receipt"]["first_seen_at"], "firstSeenAt")
                       for key in ("H125_CURRENT", "H126_PRIOR_REPORT")],
                     *[_aware(source_evidence["regularIndexes"][key]["firstSeenAt"], "index firstSeenAt")
                       for key in ("H125_CURRENT", "H126_PRIOR_REPORT")],
                     _aware(source_evidence["organizationLookup"]["firstSeenAt"], "firstSeenAt"),
                     *[date_only_available_at(date.fromisoformat(published), calendar_days)[0]
                       for published in (H125_REPORT["publishedDate"], H126_REPORT["publishedDate"])]
                     ]), calendar_days):
            return False
        con_changes = source.total_changes
        source.execute("ROLLBACK")
        source.close()
        source = None
        if con_changes != 0:
            return False
        by_version_id = {str(fact.version_id): fact for fact in facts.values()}
        for metric in METRICS:
            events = evidence["versionSourcesByMetric"].get(metric)
            expected_events = [source_evidence["values"][metric]["H125_CURRENT"],
                               source_evidence["values"][metric]["H126_PRIOR"]]
            if not isinstance(events, list) or len(events) != 2:
                return False
            predecessor = None
            last_value = None
            for saved, actual in zip(events, expected_events):
                role = "ORIGINAL_CURRENT" if actual["columnRole"] == "CURRENT" else "PRESENTED_PRIOR"
                expected_id = _version_id(metric, role, actual["announcementId"], actual["contentHash"])
                fact = by_version_id.get(expected_id)
                if (fact is None or saved.get("role") != role or
                        saved.get("announcementId") != actual["announcementId"] or
                        saved.get("sourceReportPeriodEnd") != actual["sourceReportPeriodEnd"] or
                        saved.get("factPeriodEnd") != H125.isoformat() or
                        saved.get("columnRole") != actual["columnRole"] or
                        saved.get("amountYuan") != actual["amountYuan"] or
                        saved.get("versionId") != expected_id or
                        saved.get("supersedesId") != predecessor or
                        fact.version_id != expected_id or fact.instrument_id != INSTRUMENT_ID or
                        fact.metric != metric or fact.period_end != H125 or
                        fact.statement_scope is not StatementScope.CONSOLIDATED or
                        fact.profit_scope is not _metric_profit_scope(metric) or
                        fact.source_id != "cninfo" or fact.source_document_id != actual["announcementId"] or
                        fact.content_hash != actual["contentHash"] or fact.value != Decimal(actual["amountYuan"]) or
                        fact.currency != "CNY" or fact.raw_unit != "yuan" or
                        fact.timestamp_precision is not TimestampPrecision.DATE or
                        fact.availability_basis is not AvailabilityBasis.OBSERVED or
                        fact.pit_mode is not PitMode.LIVE_OBSERVED or
                        fact.source_published_date != date.fromisoformat(actual["publishedDate"]) or
                        fact.source_published_at is not None or
                        fact.first_seen_at != _aware(actual["firstSeenAt"], "firstSeenAt") or
                        fact.ingested_at != ingested_at or fact.available_at != available or
                        fact.supersedes_id != predecessor):
                    return False
                if last_value is not None and Decimal(actual["amountYuan"]) == last_value:
                    return False
                predecessor = expected_id
                last_value = Decimal(actual["amountYuan"])
        boundary = evidence.get("classificationBoundary", {})
        if (boundary.get("formalAdmission") != "NOT_ADMITTED" or
                boundary.get("impactStatus") != "AFFECTS" or
                boundary.get("impactProof") is not None or boundary.get("causalAttribution") is not None or
                boundary.get("priorStrictStatus") != "NOT_VERIFIED_AS_PRIOR_FACTS" or
                boundary.get("priorGridStatus") != "PASS_AMOUNT_CELL_AND_PERIOD_BINDING"):
            return False
        return True
    except (OSError, sqlite3.Error, KeyError, IndexError, AttributeError, TypeError,
            ValueError, ArithmeticError):
        return False
    finally:
        if source is not None:
            try:
                source.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            source.close()


__all__ = ["SCHEMA", "VERIFIER_ID", "H125ProfitPairContractError",
           "promote_000680_h125_profit_pair", "accepts_000680_h125_profit_pair"]
