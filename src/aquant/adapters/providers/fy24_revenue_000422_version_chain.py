"""Finite 000422 FY24 revenue CURRENT -> FY25 report FY24 PRIOR chain.

This module is deliberately scoped to one metric and one fact period. The
later-column observation is recorded as PRESENTED_PRIOR, never as an original
CURRENT report. Source evidence remains NOT_ADMITTED until the ordinary public
reader replays the saved originals.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from ...artifact_paths import resolve_archive_member
from ...domain.data.db import connect
from ...domain.data.pit import (
    AvailabilityBasis, PitMode, TimestampPrecision, date_only_available_at,
)
from ...domain.data.rights import REGISTER_VERSION, default_rights
from ...domain.fundamentals.fact_repository import FactReviewBundle, FinancialFactRepository
from ...domain.fundamentals.versioned import FinancialFact, StatementScope
from . import cninfo_statement_sections as sections
from . import s2_calendar_evidence, s2_machine_verification
from . import s2_revision_impact as impact
from .cninfo import parse_announcements, parse_report_period, title_matches_period
from .pdf_promotion import _archived_ok, _next_available_snapshot, _utc_now


SCHEMA = "aquant.cninfo.000422-fy24-revenue-presented-prior-chain.v1"
VERIFIER = "machine:cninfo-000422-fy24-revenue-presented-prior-chain-v1"
REVIEW_PREFIX = "cninfo_machine_000422_fy24_revenue_chain_"
STOCK_CODE = "000422"
INSTRUMENT_ID = "000422"
ISSUER = "SZ.000422"
COMPANY = "湖北宜化化工股份有限公司"
SHORT_NAME = "湖北宜化"
ORGANIZATION_ID = "gssz0000422"
FACT_PERIOD = date(2024, 12, 31)
PRESENTING_PERIOD = date(2025, 12, 31)
METRIC = "revenue"
ORIGINAL_ROLE = "ORIGINAL_CURRENT"
PRESENTED_ROLE = "PRESENTED_PRIOR"
CAUSE_IDS = ("1222991641", "1222991666", "1225183650", "1225183652")
REPORT_BINDINGS = {
    ORIGINAL_ROLE: {
        "announcementId": "1222991648",
        "receiptId": "rcpt_dd563ad123312081de09d3ee",
        "contentHash": "sha256:621ad3da4b39889bda5e44d85ee7c76d6eda17a22648bf5e6540b0d036c1f008",
        "documentUrl": "https://static.cninfo.com.cn/finalpage/2025-04-03/1222991648.PDF",
        "title": "2024年年度报告", "sourceReportPeriodEnd": "2024-12-31",
        "factPeriodEnd": "2024-12-31", "columnRole": "CURRENT",
        "sourcePublishedDate": "2025-04-03", "valueYuan": "16963593887.39",
        "page": 127, "columnHeader": "2024年度",
    },
    PRESENTED_ROLE: {
        "announcementId": "1225183616",
        "receiptId": "rcpt_a077f219141fbc91d26d1325",
        "contentHash": "sha256:13067dd7ca378659a5d01a6600c911dda9335afc970ac10f554dfd79a945987f",
        "documentUrl": "https://static.cninfo.com.cn/finalpage/2026-04-25/1225183616.PDF",
        "title": "2025年年度报告", "sourceReportPeriodEnd": "2025-12-31",
        "factPeriodEnd": "2024-12-31", "columnRole": "PRIOR",
        "sourcePublishedDate": "2026-04-25", "valueYuan": "25394172300.46",
        "page": 116, "columnHeader": "2024年度",
        "currentValueYuan": "25652882508.52", "currentHeader": "2025年度",
    },
}
BOARD_BINDING = {
    "announcementId": "1225183652",
    "receiptId": "rcpt_7f8be2ce9e888bc0e0c5e7bc",
    "contentHash": "sha256:b090c18e1fae1a1335140676251959070a262d880d18cd10e1d15b639163fac1",
    "documentUrl": "https://static.cninfo.com.cn/finalpage/2026-04-25/1225183652.PDF",
    "beforeYuan": "16963593887.39", "afterYuan": "25394172300.46",
    "deltaYuan": "8430578413.07", "page": 5,
}
CAPTURE_SHA256 = "sha256:0b4669952ca537fb1eb4f0a32cffac15281bbd9535153028d4154e5d9c05eaf9"
CAPTURE_CANONICAL_SHA256 = "sha256:8b8fbe6c0b130a6b482d915681be85944476ef1a5bafee7323aab751f43a1e42"
PRIOR_NOTE = {
    "heading": "60、营业收入和营业成本", "page": 205,
    "mainYuan": "24357468523.69", "otherYuan": "1036703776.77",
    "totalYuan": "25394172300.46", "currentTotalYuan": "25652882508.52",
}


class RevenueChainContractError(ValueError):
    """A fixed source identity, period, cell, or replay contract failed."""


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise RevenueChainContractError(message)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _version_id(role: str, predecessor_id: str | None = None) -> str:
    if role == ORIGINAL_ROLE:
        _need(predecessor_id is None, "original CURRENT cannot supersede a prior version")
        report = REPORT_BINDINGS[ORIGINAL_ROLE]
        identity = (f"cninfo|{STOCK_CODE}|{FACT_PERIOD.isoformat()}|{METRIC}|"
                    f"{report['announcementId']}|{report['contentHash']}")
        return "mach_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    _need(role == PRESENTED_ROLE and bool(predecessor_id), "unsupported presentation role")
    report = REPORT_BINDINGS[PRESENTED_ROLE]
    identity = (f"cninfo|{STOCK_CODE}|{PRESENTING_PERIOD.isoformat()}|{FACT_PERIOD.isoformat()}|"
                f"PRIOR|{METRIC}|{report['announcementId']}|{report['contentHash']}")
    return "mach_cmp_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


class _ReadOnlyArchive:
    """SQLite read-only archive used by public review replay."""

    def __init__(self, database_path: str, root: str) -> None:
        self.root = Path(root).resolve()
        if Path(database_path).resolve() != self.root / "meta.sqlite":
            raise ValueError("archive database is outside the declared canonical root")
        self.con = connect(database_path, read_only=True)
        self.con.execute("PRAGMA query_only=ON")
        self.con.execute("BEGIN")

    def close(self) -> None:
        try:
            if self.con.in_transaction:
                self.con.execute("ROLLBACK")
        finally:
            self.con.close()

    def load_bytes(self, content_hash: str) -> bytes:
        row = self.con.execute(
            "SELECT stored_path,byte_size FROM raw_artifact WHERE content_hash=?",
            (content_hash,),
        ).fetchone()
        if row is None:
            raise KeyError(content_hash)
        path = resolve_archive_member(self.root, row["stored_path"])
        body = path.read_bytes()
        if len(body) != row["byte_size"] or _digest(body) != content_hash:
            raise ValueError("archive member hash or byte size differs")
        return body

    def verify(self, content_hash: str) -> bool:
        try:
            self.load_bytes(content_hash)
        except (OSError, KeyError, ValueError):
            return False
        return True


def _company(source: Mapping[str, object]) -> Mapping[str, object]:
    company = source.get("company")
    _need(isinstance(company, Mapping), "source input lacks the current issuer packet")
    _need(company.get("code") == STOCK_CODE and company.get("instrumentId") == ISSUER and
          company.get("name") == SHORT_NAME and company.get("organizationId") == ORGANIZATION_ID and
          company.get("publicationWindow") == ["2024-06-30", "2026-11-03"] and
          company.get("orgLookupReceiptId") == "rcpt_61dc08712f446d25cffaf990" and
          company.get("orgLookupContentHash") ==
          "sha256:379914b477016350d931c76544a0887c1a8c7c378608dda4233c0baa9e113805",
          "current 000422 issuer identity or Nov3 window differs")
    _need(source.get("issuerName") == COMPANY, "full issuer name differs from the source packet")
    _need(company.get("allCategoryTermination") == "has_more_false" and
          company.get("allCategoryTotalAnnouncement") == 573 and
          len(company.get("allCategoryPages", ())) == 20,
          "Nov3 all-category capture is incomplete")
    return company


def _record(company: Mapping[str, object], role: str) -> Mapping[str, object]:
    binding = REPORT_BINDINGS[role]
    rows = [row for row in company.get("selectedReportRecords", ())
            if isinstance(row, Mapping) and str(row.get("announcementId")) == binding["announcementId"]]
    _need(len(rows) == 1, "issuer packet lacks the unique selected report " + binding["announcementId"])
    row = rows[0]
    receipt = row.get("receipt", {})
    _need(row.get("code") == STOCK_CODE and row.get("instrumentId") == ISSUER and
          row.get("periodEnd") == binding["sourceReportPeriodEnd"] and
          row.get("title") == binding["title"] and row.get("documentUrl") == binding["documentUrl"] and
          row.get("contentHash") == binding["contentHash"] and
          receipt.get("receipt_id") == binding["receiptId"] and
          receipt.get("content_hash") == binding["contentHash"] and receipt.get("url") == binding["documentUrl"] and
          receipt.get("source_id") == "cninfo" and receipt.get("outcome") == "OK" and
          receipt.get("http_status") == 200,
          "selected report identity or archived PDF receipt differs for " + binding["announcementId"])
    return row


def _receipt_page(archive, page: Mapping[str, object], *, expected_detail_prefix: str) -> dict:
    rid = str(page.get("responseReceiptId", page.get("receipt", {}).get("receipt_id", "")))
    row = archive.con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?", (rid,)).fetchone()
    _need(row is not None, "captured index receipt is missing: " + rid)
    receipt = dict(row)
    _need(receipt["source_id"] == "cninfo" and receipt["outcome"] == "OK" and
          receipt["http_status"] == 200 and receipt["content_hash"] ==
          page.get("responseContentHash", page.get("receipt", {}).get("content_hash")) and
          receipt["detail"].startswith(expected_detail_prefix),
          "captured index receipt metadata differs: " + rid)
    url = receipt["url"]
    _, body = _archived_ok(archive, rid, expected_url=url, expected_host="www.cninfo.com.cn")
    return {"receiptId": rid, "contentHash": receipt["content_hash"],
            "firstSeenAt": receipt["first_seen_at"], "byteSize": len(body),
            "requestBodyHash": page.get("requestBodyHash"), "body": body}


def _verify_index_capture(archive, source: Mapping[str, object], company: Mapping[str, object]) -> dict:
    capture = source.get("currentIndexCapture")
    _need(isinstance(capture, Mapping) and capture.get("schema") ==
          "aquant.000422.nov3.current-source-closure.v1" and
          capture.get("status") == "COMPLETE" and capture.get("observationDateBeijing") == "2026-11-03" and
          capture.get("scope", {}).get("code") == STOCK_CODE and
          capture.get("scope", {}).get("crossCategoryCount") == 573 and
          capture.get("scope", {}).get("crossCategoryPageCount") == 20 and
          capture.get("scope", {}).get("crossCategoryTermination") == "has_more_false" and
          capture.get("scope", {}).get("crossCategoryWindow") == ["2024-06-30", "2026-11-03"],
          "Nov3 source closure capture differs or is incomplete")
    _need(source.get("currentIndexCaptureSha256") == CAPTURE_SHA256,
          "Nov3 source closure capture SHA differs")
    canonical_capture_hash = _digest(_canonical(capture).encode("utf-8"))
    _need(source.get("currentIndexCaptureCanonicalSha256") == CAPTURE_CANONICAL_SHA256 and
          canonical_capture_hash == CAPTURE_CANONICAL_SHA256,
          "Nov3 current index capture object differs from its canonical input digest")
    index = capture.get("currentCrossIndex", {})
    pages = index.get("pages", ())
    _need(len(index.get("allEntries", ())) == 573 and len(pages) == 20 and
          len(index.get("financialRevisionCandidates", ())) == 4 and
          sorted(str(item.get("announcementId")) for item in index["financialRevisionCandidates"]) ==
          list(CAUSE_IDS), "Nov3 all-category/cause set is not the exact four-source closure")
    regular_rows = list(capture.get("regularIndexes", ()))
    regular_periods = {str(item.get("periodEnd")) for item in regular_rows}
    _need(regular_periods == {"2024-06-30", "2024-12-31", "2025-06-30",
                              "2025-12-31", "2026-06-30", "2026-09-30"},
          "Nov3 regular-index period set differs")
    for item in regular_rows:
        period = str(item["periodEnd"])
        expected_through = "2026-06-30" if period == "2024-06-30" else "2026-11-03"
        _need(item.get("complete") is True and item.get("termination") == "has_more_false" and
              item.get("queryThrough") == expected_through and item.get("fullReportCandidateCount") == 1 and
              len(item.get("pages", ())) == 1 and item["pages"][0].get("hasMore") is False,
              "one of the six captured regular indexes is incomplete: " + period)
    page_links = [(item.get("page"), item.get("responseReceiptId"), item.get("responseContentHash"),
                   item.get("requestBodyHash")) for item in pages]
    expected_links = [(item.get("page"), item.get("responseReceiptId"), item.get("responseContentHash"),
                       item.get("requestBodyHash")) for item in company.get("allCategoryPages", ())]
    _need(page_links == expected_links, "all-category capture pages differ from source packet receipts")
    # replay_revision_impact reopens and validates these 20 captured pages and
    # all 573 rows. Here we also bind their receipt tuple to the packet summary.

    verified_regular = {}
    for role, binding in ((ORIGINAL_ROLE, REPORT_BINDINGS[ORIGINAL_ROLE]),
                          (PRESENTED_ROLE, REPORT_BINDINGS[PRESENTED_ROLE])):
        period = binding["sourceReportPeriodEnd"]
        matches = [item for item in capture.get("regularIndexes", ())
                   if item.get("periodEnd") == period]
        _need(len(matches) == 1, "Nov3 regular index is not unique for " + period)
        captured = matches[0]
        expected_id = binding["announcementId"]
        rows = captured.get("fullReportMatches", ())
        _need(captured.get("queryThrough") == "2026-11-03" and captured.get("complete") is True and
              captured.get("termination") == "has_more_false" and
              captured.get("expectedAnnouncementId") == expected_id and
              captured.get("expectedAnnouncementMatchCount") == 1 and
              captured.get("fullReportCandidateCount") == 1 and len(rows) == 1 and
              rows[0].get("announcementId") == expected_id and rows[0].get("secCode") == STOCK_CODE and
              rows[0].get("documentUrl") == binding["documentUrl"] and
              rows[0].get("title") == binding["title"],
              "Nov3 regular report index does not uniquely bind " + expected_id)
        report_record = _record(company, role)

        # The selected document record stores the all-category page that first
        # surfaced the announcement. Keep that provenance independent from the
        # later, period-specific regular-index closure below.
        selected_receipt_id = report_record.get("indexReceiptId")
        selected_hash = report_record.get("indexResponseHash")
        selected_first_seen = report_record.get("indexFirstSeenAt")
        selected_pages = [item for item in pages
                          if item.get("responseReceiptId") == selected_receipt_id]
        _need(len(selected_pages) == 1 and
              selected_pages[0].get("responseContentHash") == selected_hash and
              selected_pages[0].get("firstSeenAt") == selected_first_seen,
              "selected report is not bound to its unique all-category page receipt/hash/first-seen")
        selected_page = selected_pages[0]
        selected_page_number = selected_page.get("page")
        selected_verified = _receipt_page(
            archive, selected_page,
            expected_detail_prefix=("cross-category:000422:2024-06-30~2026-11-03:page=" +
                                    str(selected_page_number) + ":request="),
        )
        _need(selected_verified["receiptId"] == selected_receipt_id and
              selected_verified["contentHash"] == selected_hash and
              selected_verified["firstSeenAt"] == selected_first_seen,
              "selected report all-category receipt metadata differs from its original packet")
        selected_announcements = parse_announcements(selected_verified.pop("body"))
        selected_matches = [item for item in selected_announcements
                            if item.announcement_id == expected_id and item.sec_code == STOCK_CODE]
        selected_url = ((selected_matches[0].detail_url() or "").replace("http://", "https://")
                        if len(selected_matches) == 1 else "")
        _need(len(selected_matches) == 1 and selected_url == binding["documentUrl"] and
              selected_matches[0].title == binding["title"] and
              title_matches_period(selected_matches[0].title, parse_report_period(period)),
              "original all-category page lacks the unique selected issuer/report row")

        # Separately validate the complete current regular index. Its receipt
        # is not substituted into selectedReportRecords provenance.
        page_rows = captured.get("pages", ())
        _need(len(page_rows) == 1 and page_rows[0].get("page") == 1 and
              page_rows[0].get("hasMore") is False, "annual regular index is not a closed single page")
        page = page_rows[0]
        _need(rows[0].get("pageReceiptId") == page.get("responseReceiptId") and
              rows[0].get("pageContentHash") == page.get("responseContentHash"),
              "Nov3 regular-index match is detached from its own response page")
        verified = _receipt_page(archive, page, expected_detail_prefix=
                                 "report-index:000422:" + period + ":page=1:")
        announcements = parse_announcements(verified.pop("body"))
        day = parse_report_period(period)
        actual = [item for item in announcements
                  if item.sec_code == STOCK_CODE and title_matches_period(item.title, day)]
        actual_url = (actual[0].detail_url() or "").replace("http://", "https://") if len(actual) == 1 else ""
        _need(len(actual) == 1 and actual[0].announcement_id == expected_id and
              actual_url == binding["documentUrl"] and actual[0].title == binding["title"],
              "archived regular index response does not contain the unique report row")
        verified_regular[role] = {
            "periodEnd": period, "announcementId": expected_id,
            "receiptId": verified["receiptId"], "contentHash": verified["contentHash"],
            "firstSeenAt": verified["firstSeenAt"], "requestBodyHash": verified["requestBodyHash"],
        }
    return {"status": "PASS", "captureSha256": CAPTURE_SHA256,
            "allCategoryEntryCount": 573, "allCategoryPageCount": 20,
            "financialCauseIds": list(CAUSE_IDS), "regularIndexes": verified_regular}


def _current_cell(body: bytes, binding: Mapping[str, object]) -> tuple[object, object, dict]:
    period = date.fromisoformat(str(binding["sourceReportPeriodEnd"]))
    candidate = sections.extract_section_facts(
        body, instrument_id=STOCK_CODE, company=COMPANY, period_end=period,
        announcement_id=str(binding["announcementId"]), document_url=str(binding["documentUrl"]),
        required_fields=(METRIC,),
    )
    verification = s2_machine_verification.verify_machine_evidence(
        body, candidate=candidate, expected_fields=(METRIC,),
    )
    item = candidate.by_field[METRIC]
    good_paths = {check.path for field in verification.fields for check in field.checks
                  if field.field == METRIC and check.holds and
                  check.path in {"revenue_identity", "revenue_note_decomposition"}}
    _need(verification.verified and good_paths and item.currency == "CNY" and
          item.amount_unit == "元" and item.pdf_page == binding["page"] and
          item.column_header == ("2024年度" if binding is REPORT_BINDINGS[ORIGINAL_ROLE] else "2025年度") and
          item.period_end == period and str(item.value_yuan) ==
          ("16963593887.39" if binding is REPORT_BINDINGS[ORIGINAL_ROLE] else "25652882508.52") and
          (item.display_row_label or "").replace(" ", "").replace("：", ":") == "其中:营业收入",
          "strict machine CURRENT candidate differs for " + str(binding["announcementId"]))
    return candidate, verification, {
        "announcementId": binding["announcementId"], "valueYuan": str(item.value_yuan),
        "page": item.pdf_page, "columnHeader": item.column_header, "unit": item.amount_unit,
        "rowLabel": item.display_row_label, "currency": item.currency,
        "sourceRow": item.source_row, "sourceCells": list(item.source_cells),
        "successfulRevenuePaths": sorted(good_paths), "verification": verification.as_evidence(),
    }


def _presented_prior(body: bytes, current_candidate, current_verification) -> dict:
    statement_set = sections.extract_statement_sections(
        body, period_end=PRESENTING_PERIOD, roles=(sections.ROLE_CONSOLIDATED,),
        statements=("利润表",),
    )
    statement = statement_set.require(sections.ROLE_CONSOLIDATED, "利润表")
    row = statement.find(METRIC)
    _need(row is not None and statement.unit == "元" and row.unit == "元" and row.page == 116 and
          statement.current_header == "2025年度" and statement.prior_header == "2024年度" and
          (row.label or "").replace(" ", "").replace("：", ":") == "其中:营业收入" and
          str(row.amount) == "25652882508.52" and str(row.prior_amount) == "25394172300.46" and
          row.current_cell and row.prior_cell and current_verification.verified and
          str(current_candidate.by_field[METRIC].value_yuan) == str(row.amount),
          "FY25 report income statement CURRENT/PRIOR roles or cells differ")

    note = sections.extract_revenue_note_decomposition(
        body, period_end=PRESENTING_PERIOD, default_unit=statement.unit,
    )
    _need(note.pdf_sha256 == _digest(body) and note.role == "合并" and note.unit == "元" and
          note.heading == PRIOR_NOTE["heading"] and note.heading_page == PRIOR_NOTE["page"] and
          note.current_header == "本期发生额" and note.prior_header == "上期发生额" and
          note.main_business.page == PRIOR_NOTE["page"] and
          note.other_business.page == PRIOR_NOTE["page"] and note.total.page == PRIOR_NOTE["page"] and
          note.main_business.prior_cell and note.other_business.prior_cell and note.total.prior_cell and
          str(note.main_business.prior_amount) == PRIOR_NOTE["mainYuan"] and
          str(note.other_business.prior_amount) == PRIOR_NOTE["otherYuan"] and
          str(note.total.prior_amount) == PRIOR_NOTE["totalYuan"] and
          str(note.current_revenue_yuan) == PRIOR_NOTE["currentTotalYuan"] and
          note.main_business.prior_amount + note.other_business.prior_amount == note.total.prior_amount and
          note.total.prior_amount == row.prior_amount,
          "FY25 annual note parser does not independently decompose the FY24 PRIOR cell")
    return {
        "role": PRESENTED_ROLE, "metric": METRIC,
        "sourceReportPeriodEnd": PRESENTING_PERIOD.isoformat(),
        "factPeriodEnd": FACT_PERIOD.isoformat(), "columnRole": "PRIOR",
        "currentHeader": statement.current_header, "columnHeader": statement.prior_header,
        "page": row.page, "rowLabel": row.label, "unit": statement.unit,
        "sourceRow": row.raw_line, "currentCell": row.current_cell,
        "priorCell": row.prior_cell, "currentValueYuan": str(row.amount),
        "valueYuan": str(row.prior_amount),
        "priorIsNotOriginalStrictCurrent": True,
        "currentMachineVerification": current_verification.as_evidence(),
        "priorNoteDecomposition": {
            "heading": note.heading, "page": note.heading_page, "unit": note.unit,
            "currentHeader": note.current_header, "priorHeader": note.prior_header,
            "pdfSha256": note.pdf_sha256,
            "mainPriorYuan": str(note.main_business.prior_amount),
            "mainPriorCell": note.main_business.prior_cell,
            "otherPriorYuan": str(note.other_business.prior_amount),
            "otherPriorCell": note.other_business.prior_cell,
            "totalPriorYuan": str(note.total.prior_amount),
            "totalPriorCell": note.total.prior_cell,
            "currentTotalYuan": str(note.current_revenue_yuan),
            "rawLines": list(note.raw_lines),
        },
    }


def _board_correction(body: bytes) -> dict:
    from pypdf import PdfReader
    _need(_digest(body) == BOARD_BINDING["contentHash"] and body.startswith(b"%PDF-"),
          "board correction receipt bytes differ")
    pages = PdfReader(io.BytesIO(body)).pages
    _need(len(pages) >= BOARD_BINDING["page"], "board correction page is missing")
    text = pages[BOARD_BINDING["page"] - 1].extract_text() or ""
    compact = re.sub(r"\s+", "", text)
    _need("对合并利润表2024年度金额追溯调整如下" in compact and "单位：人民币元" in compact and
          re.search(r"其中：营业收入16,963,593,887\.3925,394,172,300\.468,430,578,413\.07", compact),
          "native pypdf p5 does not bind exact revenue before/after/delta row")
    before, after, delta = map(Decimal, (BOARD_BINDING["beforeYuan"],
                                         BOARD_BINDING["afterYuan"], BOARD_BINDING["deltaYuan"]))
    _need(after - before == delta, "board adjustment arithmetic differs")
    return {"announcementId": BOARD_BINDING["announcementId"],
            "contentHash": BOARD_BINDING["contentHash"], "page": BOARD_BINDING["page"],
            "periodEnd": FACT_PERIOD.isoformat(), "statementRole": "合并", "unit": "人民币元",
            "beforeYuan": str(before), "afterYuan": str(after), "deltaYuan": str(delta),
            "nativeExtractionBackend": "pypdf", "nativePageText": text}


def _presented_prior_comparison(outcome: Mapping[str, object]) -> Mapping[str, object]:
    matches = [row for row in outcome.get("comparisons", ())
              if isinstance(row, Mapping) and
              row.get("announcementId") == REPORT_BINDINGS[PRESENTED_ROLE]["announcementId"] and
              row.get("sourceReportPeriodEnd") == PRESENTING_PERIOD.isoformat() and
              row.get("factPeriodEnd") == FACT_PERIOD.isoformat() and
              row.get("columnRole") == "PRIOR"]
    _need(len(matches) == 1,
          "replayed impact lacks the unique FY25 report FY24 PRESENTED_PRIOR comparison")
    return matches[0]


def _impact_replay(archive, source: Mapping[str, object], company: Mapping[str, object]) -> dict:
    saved = source.get("revisionImpactProof")
    docs = source.get("documents")
    _need(isinstance(saved, Mapping) and isinstance(docs, list),
          "full captured revenue impact proof and document set are required")
    fresh = impact.replay_revision_impact(
        saved, archive_root=archive.root, company=company, documents=docs,
        target_period_end=FACT_PERIOD, target_metrics=(METRIC,), issuer_name=COMPANY,
    )
    # Keep the originally observed timestamped proof in the bundle. Replay
    # validates it against fresh output while allowing only completedAt to move.
    proof = dict(saved)
    outcomes = [row for row in proof.get("outcomes", ())
                if isinstance(row, Mapping) and row.get("metric") == METRIC]
    _need(len(outcomes) == 1, "replayed revision impact lacks the unique revenue outcome")
    outcome = outcomes[0]
    unresolved = outcome.get("unresolvedSuccessorIds")
    _need(isinstance(unresolved, list) and unresolved == [],
          "source-exhaustive revenue chain is blocked by unresolved successor report(s): "
          f"{unresolved!r}; resolve the 2024 same-period report before writing or accepting this chain")
    presented_prior = _presented_prior_comparison(outcome)
    same_period = [row for row in outcome.get("comparisons", ())
                   if isinstance(row, Mapping) and row.get("announcementId") == "1222991654" and
                   row.get("sourceReportPeriodEnd") == FACT_PERIOD.isoformat() and
                   row.get("factPeriodEnd") == FACT_PERIOD.isoformat() and row.get("columnRole") == "CURRENT"]
    native_value = same_period[0].get("sourceNativeEvidence") if len(same_period) == 1 else None
    native = native_value if isinstance(native_value, Mapping) else {}
    note_value = native.get("statementArithmetic")
    note_arithmetic = note_value if isinstance(note_value, Mapping) else {}
    scope = proof.get("relationshipScope", {})
    _need(outcome.get("status") == "AFFECTS" and outcome.get("admissionStatus") == "NOT_ADMITTED" and
          len(same_period) == 1 and len(outcome.get("comparisons", ())) == 2 and
          same_period[0].get("changed") is False and
          str(same_period[0].get("amountYuan")) == REPORT_BINDINGS[ORIGINAL_ROLE]["valueYuan"] and
          native.get("schema") == "aquant.000422.fy24-report1654-native-current-cell.v1" and
          native.get("announcementId") == "1222991654" and
          native.get("contentHash") == "sha256:e3dcde5c91deb5ca53eb1936ae5c330f0e2d5846ac4417eb634d8a6e240af05b" and
          native.get("documentUrl") == "https://static.cninfo.com.cn/finalpage/2025-04-03/1222991654.PDF" and
          native.get("receiptId") == "rcpt_7b8ef8cf6f6b965a119a697b" and
          native.get("issuerName") == COMPANY and native.get("instrumentId") == ISSUER and
          native.get("reportPeriodEnd") == FACT_PERIOD.isoformat() and
          native.get("statement") == "合并利润表" and native.get("statementScope") == "CONSOLIDATED" and
          native.get("unitRaw") == "元" and native.get("currentHeader") == "2024年度" and
          native.get("currentRoleHeader") == "本期金额" and native.get("currentAmountYuan") ==
          REPORT_BINDINGS[ORIGINAL_ROLE]["valueYuan"] and native.get("priorRoleHeader") == "上期金额" and
          native.get("priorFactPeriodEnd") is None and
          native.get("priorPeriodResolution") == "UNRESOLVED_NO_EXPLICIT_2023_DATE_HEADER" and
          note_arithmetic.get("check") == "revenue_note_decomposition" and
          note_arithmetic.get("matches") is True and
          scope.get("reportCandidatePeriods", {}).get("1222991654") == FACT_PERIOD.isoformat() and
          outcome.get("currentSource", {}).get("announcementId") ==
          REPORT_BINDINGS[ORIGINAL_ROLE]["announcementId"] and
          outcome.get("currentSource", {}).get("columnRole") == "CURRENT" and
          outcome.get("currentSource", {}).get("factPeriodEnd") == FACT_PERIOD.isoformat() and
          str(outcome.get("currentSource", {}).get("amountYuan")) ==
          REPORT_BINDINGS[ORIGINAL_ROLE]["valueYuan"] and
          str(presented_prior.get("amountYuan")) == REPORT_BINDINGS[PRESENTED_ROLE]["valueYuan"] and
          scope.get("financialCauseIds") == list(CAUSE_IDS) and
          scope.get("missingReportSourceIds") == [] and
          scope.get("completeForArchivedIndexResponse") is True and
          scope.get("publicationWindow") == ["2024-06-30", "2026-11-03"],
          "replayed impact no longer proves the exact 1654 CURRENT + FY25 PRIOR AFFECTS/NOT_ADMITTED pair and four causes")
    return {"status": outcome["status"], "admissionStatus": outcome["admissionStatus"],
            "outcome": outcome, "financialCauseIds": list(scope["financialCauseIds"]),
            "unresolvedSuccessorIds": unresolved,
            "missingReportSourceIds": list(scope.get("missingReportSourceIds", ())),
            "proof": proof}


def _replay_sources(archive, source: Mapping[str, object]) -> dict:
    with sections._revenue_note_text_cache_scope():
        return _replay_sources_with_note_text_cache(archive, source)


def _replay_sources_with_note_text_cache(archive, source: Mapping[str, object]) -> dict:
    company = _company(source)
    original_record = _record(company, ORIGINAL_ROLE)
    presented_record = _record(company, PRESENTED_ROLE)
    original_receipt, original_body = _archived_ok(
        archive, REPORT_BINDINGS[ORIGINAL_ROLE]["receiptId"],
        expected_url=REPORT_BINDINGS[ORIGINAL_ROLE]["documentUrl"], expected_host="static.cninfo.com.cn")
    presented_receipt, presented_body = _archived_ok(
        archive, REPORT_BINDINGS[PRESENTED_ROLE]["receiptId"],
        expected_url=REPORT_BINDINGS[PRESENTED_ROLE]["documentUrl"], expected_host="static.cninfo.com.cn")
    board_record = next((row for row in company.get("relatedDocumentRecords", ())
                         if isinstance(row, Mapping) and str(row.get("announcementId")) ==
                         BOARD_BINDING["announcementId"]), None)
    _need(isinstance(board_record, Mapping) and board_record.get("documentUrl") == BOARD_BINDING["documentUrl"] and
          board_record.get("contentHash") == BOARD_BINDING["contentHash"] and
          board_record.get("receipt", {}).get("receipt_id") == BOARD_BINDING["receiptId"],
          "board source is not bound to the current issuer packet")
    board_receipt, board_body = _archived_ok(
        archive, BOARD_BINDING["receiptId"], expected_url=BOARD_BINDING["documentUrl"],
        expected_host="static.cninfo.com.cn")
    _need(original_receipt["content_hash"] == original_record["contentHash"] and
          presented_receipt["content_hash"] == presented_record["contentHash"] and
          board_receipt["content_hash"] == board_record["contentHash"],
          "report or board receipt differs from source packet")
    _candidate, original_machine, original_evidence = _current_cell(
        original_body, REPORT_BINDINGS[ORIGINAL_ROLE])
    presented_candidate, presented_machine, presented_current = _current_cell(
        presented_body, REPORT_BINDINGS[PRESENTED_ROLE])
    prior = _presented_prior(presented_body, presented_candidate, presented_machine)
    board = _board_correction(board_body)
    _need(board["beforeYuan"] == original_evidence["valueYuan"] and
          board["afterYuan"] == prior["valueYuan"],
          "board before/after does not equal the two chain cells")
    impact_result = _impact_replay(archive, source, company)
    presented_prior = _presented_prior_comparison(impact_result["outcome"])
    _need(str(presented_prior["amountYuan"]) == prior["valueYuan"] and
          str(impact_result["outcome"]["currentSource"]["amountYuan"]) == original_evidence["valueYuan"],
          "AFFECTS result values differ from independently parsed source cells")
    closure = _verify_index_capture(archive, source, company)
    return {
        "company": company, "originalCurrent": original_evidence,
        "presentedPrior": prior, "presentingReportCurrentControl": presented_current,
        "boardCorrection": board, "impactReplay": impact_result,
        "indexClosure": closure,
        "sourceReceipts": {
            ORIGINAL_ROLE: {"receiptId": original_receipt["receipt_id"],
                            "contentHash": original_receipt["content_hash"],
                            "firstSeenAt": original_receipt["first_seen_at"]},
            PRESENTED_ROLE: {"receiptId": presented_receipt["receipt_id"],
                             "contentHash": presented_receipt["content_hash"],
                             "firstSeenAt": presented_receipt["first_seen_at"]},
            "BOARD_CORRECTION": {"receiptId": board_receipt["receipt_id"],
                                 "contentHash": board_receipt["content_hash"],
                                 "firstSeenAt": board_receipt["first_seen_at"]},
        },
    }


def _calendar_and_pit_replay(evidence: Mapping[str, object], archive, replay: Mapping[str, object],
                             completed: datetime) -> datetime:
    checked = datetime.fromisoformat(str(evidence["calendar_checked_at"]))
    _need(checked.tzinfo is not None and checked <= completed,
          "calendar review timestamp is invalid")
    calendar = evidence["calendar_evidence"]
    days, verified = s2_calendar_evidence.verify_calendar_evidence(
        calendar, archive, now=checked,
        published_on=date.fromisoformat(REPORT_BINDINGS[ORIGINAL_ROLE]["sourcePublishedDate"]),
    )
    _need(verified == calendar and [day.isoformat() for day in days] == evidence["trading_calendar"],
          "saved calendar evidence differs from read-only replay")
    saved_impact = replay["impactReplay"]["proof"]
    impact_completed = datetime.fromisoformat(str(saved_impact["completedAt"]))
    _need(impact_completed.tzinfo is not None and impact_completed <= completed,
          "AFFECTS proof completed after the source chain")
    observed: list[datetime] = []

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key in {"first_seen_at", "firstSeenAt", "indexFirstSeenAt"} and isinstance(child, str):
                    stamp = datetime.fromisoformat(child.replace("Z", "+00:00"))
                    _need(stamp.tzinfo is not None and stamp <= completed,
                          "an indexed/source observation follows chain completion")
                    observed.append(stamp)
                else:
                    visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    visit(evidence["source_input"])
    visit(saved_impact)
    visit(calendar)
    source_times = [datetime.fromisoformat(item["firstSeenAt"])
                    for item in replay["sourceReceipts"].values()]
    source_times.extend(datetime.fromisoformat(item["firstSeenAt"])
                        for item in replay["indexClosure"]["regularIndexes"].values())
    date_floors = [date_only_available_at(
        date.fromisoformat(REPORT_BINDINGS[role]["sourcePublishedDate"]), days)[0]
        for role in (ORIGINAL_ROLE, PRESENTED_ROLE)]
    expected = _next_available_snapshot(
        max([completed, *date_floors, *source_times, *observed]), days,
    )
    available = datetime.fromisoformat(str(evidence["available_at"]))
    _need(available.tzinfo is not None and available > completed and available == expected,
          "PIT availability does not follow the captured dates/receipts/calendar")
    return available


def promote_000422_fy24_revenue_version_chain(
    *, repository: FinancialFactRepository, archive, source: Mapping[str, object],
    calendar_evidence: Mapping[str, object],
) -> tuple[FinancialFact, FinancialFact]:
    """Append only the two fixed source versions to the supplied private clone."""
    rights = default_rights().get("cninfo")
    _need(rights is not None and rights.open_for("research_use") and rights.open_for("local_storage"),
          "CNINFO research/local-storage rights are closed")
    archive.con.execute("PRAGMA query_only=ON")
    started = _utc_now()
    replay = _replay_sources(archive, source)
    trading_days, verified_calendar = s2_calendar_evidence.verify_calendar_evidence(
        dict(calendar_evidence), archive, now=started,
        published_on=date.fromisoformat(REPORT_BINDINGS[ORIGINAL_ROLE]["sourcePublishedDate"]),
    )
    _need(verified_calendar == calendar_evidence, "calendar source evidence changed on replay")
    if any(fact.logical_key == (
            STOCK_CODE, METRIC, FACT_PERIOD, StatementScope.CONSOLIDATED, None, "CNY", "yuan")
           for fact in repository.load().facts):
        raise RevenueChainContractError("FY24 revenue already has a stored version; refusing a second root")

    completed = _utc_now()
    receipts = replay["sourceReceipts"]
    proof_times = [datetime.fromisoformat(item["firstSeenAt"])
                   for item in receipts.values()]
    proof_times.extend(datetime.fromisoformat(item["firstSeenAt"])
                       for item in replay["indexClosure"]["regularIndexes"].values())
    for value in proof_times:
        _need(value.tzinfo is not None and value <= completed, "source observation clock is invalid")
    calendar_floor, _ = date_only_available_at(
        date.fromisoformat(REPORT_BINDINGS[ORIGINAL_ROLE]["sourcePublishedDate"]), trading_days)
    presenting_floor, _ = date_only_available_at(
        date.fromisoformat(REPORT_BINDINGS[PRESENTED_ROLE]["sourcePublishedDate"]), trading_days)
    available = _next_available_snapshot(max([completed, calendar_floor, presenting_floor, *proof_times]),
                                         trading_days)

    original = replay["originalCurrent"]
    prior = replay["presentedPrior"]
    root_id = _version_id(ORIGINAL_ROLE)
    prior_id = _version_id(PRESENTED_ROLE, root_id)
    common = dict(
        instrument_id=INSTRUMENT_ID, metric=METRIC, period_end=FACT_PERIOD,
        statement_scope=StatementScope.CONSOLIDATED, profit_scope=None,
        currency="CNY", raw_unit="yuan", source_id="cninfo", source_published_at=None,
        timestamp_precision=TimestampPrecision.DATE, ingested_at=completed, available_at=available,
        availability_basis=AvailabilityBasis.OBSERVED, pit_mode=PitMode.LIVE_OBSERVED,
    )
    root = FinancialFact(
        **common, value=Decimal(original["valueYuan"]),
        source_document_id=REPORT_BINDINGS[ORIGINAL_ROLE]["announcementId"],
        source_published_date=date.fromisoformat(REPORT_BINDINGS[ORIGINAL_ROLE]["sourcePublishedDate"]),
        first_seen_at=datetime.fromisoformat(receipts[ORIGINAL_ROLE]["firstSeenAt"]),
        content_hash=REPORT_BINDINGS[ORIGINAL_ROLE]["contentHash"], version_id=root_id,
    )
    presented = FinancialFact(
        **common, value=Decimal(prior["valueYuan"]),
        source_document_id=REPORT_BINDINGS[PRESENTED_ROLE]["announcementId"],
        source_published_date=date.fromisoformat(REPORT_BINDINGS[PRESENTED_ROLE]["sourcePublishedDate"]),
        first_seen_at=datetime.fromisoformat(receipts[PRESENTED_ROLE]["firstSeenAt"]),
        content_hash=REPORT_BINDINGS[PRESENTED_ROLE]["contentHash"], version_id=prior_id,
        supersedes_id=root_id,
    )
    evidence = {
        "schema": SCHEMA, "source_id": "cninfo", "instrument_id": INSTRUMENT_ID,
        "issuer_id": ISSUER, "issuer_name": COMPANY, "period_end": FACT_PERIOD.isoformat(),
        "announcement_id": REPORT_BINDINGS[PRESENTED_ROLE]["announcementId"],
        "rights_register_version": REGISTER_VERSION, "verifier_id": VERIFIER,
        "formal_admission": "NOT_ADMITTED", "candidate_disposition":
        "PRESENTED_PRIOR_AFFECTS_VERSION_CHAIN", "causal_attribution": None,
        "fact_version_ids": [root_id, prior_id], "version_ids_by_metric": {METRIC: [root_id, prior_id]},
        "review_member_version_ids": [root_id, prior_id], "completed_at": completed.isoformat(),
        "available_at": available.isoformat(),
        "source_input": dict(source), "source_replay": replay,
        "calendar_evidence": verified_calendar,
        "calendar_checked_at": started.isoformat(),
        "trading_calendar": [day.isoformat() for day in trading_days],
        "archive": {"root": str(Path(archive.root).resolve()),
                    "database_path": str(Path(archive.root).resolve() / "meta.sqlite")},
        "versions": [
            {"role": ORIGINAL_ROLE, "versionId": root_id, "supersedesId": None,
             "announcementId": REPORT_BINDINGS[ORIGINAL_ROLE]["announcementId"],
             "receiptId": REPORT_BINDINGS[ORIGINAL_ROLE]["receiptId"],
             "contentHash": REPORT_BINDINGS[ORIGINAL_ROLE]["contentHash"],
             "sourceReportPeriodEnd": FACT_PERIOD.isoformat(), "factPeriodEnd": FACT_PERIOD.isoformat(),
             "columnRole": "CURRENT", "columnHeader": "2024年度", "page": original["page"],
             "rowLabel": original["rowLabel"], "unit": original["unit"],
             "valueYuan": str(root.value)},
            {"role": PRESENTED_ROLE, "versionId": prior_id, "supersedesId": root_id,
             "announcementId": REPORT_BINDINGS[PRESENTED_ROLE]["announcementId"],
             "receiptId": REPORT_BINDINGS[PRESENTED_ROLE]["receiptId"],
             "contentHash": REPORT_BINDINGS[PRESENTED_ROLE]["contentHash"],
             "sourceReportPeriodEnd": PRESENTING_PERIOD.isoformat(), "factPeriodEnd": FACT_PERIOD.isoformat(),
             "columnRole": "PRIOR", "currentColumnHeader": prior["currentHeader"],
             "columnHeader": prior["columnHeader"], "page": prior["page"],
             "rowLabel": prior["rowLabel"], "unit": prior["unit"],
             "valueYuan": str(presented.value)},
        ],
    }
    bundle_id = REVIEW_PREFIX + hashlib.sha256(
        f"{SCHEMA}|{INSTRUMENT_ID}|{FACT_PERIOD.isoformat()}|{root_id}|{prior_id}".encode("utf-8")
    ).hexdigest()[:32]
    repository.append_many((root, presented), review_bundle=FactReviewBundle(bundle_id, _canonical(evidence)))
    return root, presented


def accepts_000422_fy24_revenue_version_chain(
    row: Mapping[str, object], member_ids: set[str], facts: Mapping[str, FinancialFact], *,
    accepted_links: Mapping[str, tuple[str, str]],
) -> bool:
    """Fail-closed public-reader replay for the one fixed 000422 revenue tuple."""
    archive = None
    try:
        raw = row["evidence_json"]
        evidence = json.loads(raw)
        if (not isinstance(evidence, dict) or _canonical(evidence) != raw or
                row["evidence_hash"] != _digest(raw.encode("utf-8")) or
                evidence.get("schema") != SCHEMA or evidence.get("source_id") != "cninfo" or
                evidence.get("instrument_id") != INSTRUMENT_ID or evidence.get("issuer_id") != ISSUER or
                evidence.get("issuer_name") != COMPANY or evidence.get("period_end") != FACT_PERIOD.isoformat() or
                evidence.get("announcement_id") != REPORT_BINDINGS[PRESENTED_ROLE]["announcementId"] or
                evidence.get("rights_register_version") != REGISTER_VERSION or
                evidence.get("verifier_id") != VERIFIER or evidence.get("formal_admission") != "NOT_ADMITTED" or
                evidence.get("candidate_disposition") != "PRESENTED_PRIOR_AFFECTS_VERSION_CHAIN" or
                evidence.get("causal_attribution") is not None or not row["bundle_id"].startswith(REVIEW_PREFIX)):
            return False
        ids = evidence.get("version_ids_by_metric", {}).get(METRIC, ())
        if (set(evidence.get("version_ids_by_metric", {})) != {METRIC} or len(ids) != 2 or
                ids != evidence.get("fact_version_ids") or ids != evidence.get("review_member_version_ids") or
                member_ids != set(ids) or set(ids) & accepted_links.keys() or set(ids) - facts.keys()):
            return False
        expected_bundle = REVIEW_PREFIX + hashlib.sha256(
            f"{SCHEMA}|{INSTRUMENT_ID}|{FACT_PERIOD.isoformat()}|{ids[0]}|{ids[1]}".encode("utf-8")
        ).hexdigest()[:32]
        if row["bundle_id"] != expected_bundle:
            return False
        archive_data = evidence["archive"]
        archive = _ReadOnlyArchive(str(archive_data["database_path"]), str(archive_data["root"]))
        completed = datetime.fromisoformat(evidence["completed_at"])
        if completed.tzinfo is None or completed > datetime.now(timezone.utc):
            return False
        replay = _replay_sources(archive, evidence["source_input"])
        if replay != evidence["source_replay"] or archive.con.total_changes != 0:
            return False
        _calendar_and_pit_replay(evidence, archive, replay, completed)
        versions = evidence["versions"]
        if len(versions) != 2 or [item.get("role") for item in versions] != [ORIGINAL_ROLE, PRESENTED_ROLE]:
            return False
        predecessor = None
        for item, role in zip(versions, (ORIGINAL_ROLE, PRESENTED_ROLE), strict=True):
            fact = facts.get(item.get("versionId"))
            binding = REPORT_BINDINGS[role]
            expected_id = _version_id(role, predecessor)
            value = Decimal(binding["valueYuan"])
            if (fact is None or item.get("versionId") != expected_id or item.get("supersedesId") != predecessor or
                    fact.version_id != expected_id or fact.instrument_id != INSTRUMENT_ID or
                    fact.metric != METRIC or fact.period_end != FACT_PERIOD or
                    fact.statement_scope is not StatementScope.CONSOLIDATED or fact.profit_scope is not None or
                    fact.currency != "CNY" or fact.raw_unit != "yuan" or fact.value != value or
                    fact.source_id != "cninfo" or fact.source_document_id != binding["announcementId"] or
                    fact.content_hash != binding["contentHash"] or fact.supersedes_id != predecessor or
                    fact.source_published_date != date.fromisoformat(binding["sourcePublishedDate"]) or
                    fact.source_published_at is not None or fact.timestamp_precision is not TimestampPrecision.DATE or
                    fact.availability_basis is not AvailabilityBasis.OBSERVED or fact.pit_mode is not PitMode.LIVE_OBSERVED or
                    fact.first_seen_at.isoformat() != replay["sourceReceipts"][role]["firstSeenAt"] or
                    fact.ingested_at.isoformat() != evidence["completed_at"] or
                    fact.available_at.isoformat() != evidence["available_at"] or
                    item.get("announcementId") != binding["announcementId"] or
                    item.get("receiptId") != binding["receiptId"] or item.get("contentHash") != binding["contentHash"] or
                    item.get("sourceReportPeriodEnd") != binding["sourceReportPeriodEnd"] or
                    item.get("factPeriodEnd") != FACT_PERIOD.isoformat() or
                    item.get("columnRole") != binding["columnRole"] or item.get("valueYuan") != str(value)):
                return False
            source_cell = (replay["originalCurrent"] if role == ORIGINAL_ROLE
                           else replay["presentedPrior"])
            if (item.get("columnHeader") != source_cell.get("columnHeader") or
                    item.get("page") != source_cell.get("page") or
                    item.get("rowLabel") != source_cell.get("rowLabel") or
                    item.get("unit") != source_cell.get("unit")):
                return False
            if role == PRESENTED_ROLE and item.get("currentColumnHeader") != source_cell.get("currentHeader"):
                return False
            predecessor = expected_id
        cohort = [fact for fact in facts.values() if fact.logical_key == facts[ids[0]].logical_key]
        if {fact.version_id for fact in cohort} != set(ids):
            return False
        _need(archive_data["root"] == str(Path(archive_data["root"]).resolve()),
              "archive root path is not canonical")
        return True
    except (OSError, sqlite3.Error, KeyError, AttributeError, TypeError, ValueError, ArithmeticError):
        return False
    finally:
        if archive is not None:
            archive.close()


__all__ = [
    "SCHEMA", "VERIFIER", "REVIEW_PREFIX", "RevenueChainContractError",
    "promote_000422_fy24_revenue_version_chain", "accepts_000422_fy24_revenue_version_chain",
]
