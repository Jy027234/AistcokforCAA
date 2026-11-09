"""Finite 000422 H124 revenue CURRENT -> H125 presented-prior chain.

This reader preserves the original H124 observation and the later H125
comparative as two versions of one revenue fact. The comparison is AFFECTS /
NOT_ADMITTED; the H125 restatement label and merger identity are evidence, not
single-notice causal attribution.
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

from ...domain.data.pit import (
    AvailabilityBasis, PitMode, TimestampPrecision, date_only_available_at,
)
from ...domain.data.rights import REGISTER_VERSION, default_rights
from ...domain.fundamentals.fact_repository import FactReviewBundle, FinancialFactRepository
from ...domain.fundamentals.versioned import FinancialFact, StatementScope
from . import (
    cninfo_statement_sections as sections,
    fy24_revenue_000422_version_chain as fy24,
    s2_calendar_evidence,
    s2_machine_verification,
    s2_revision_impact as impact,
)
from .cninfo import parse_announcements, parse_report_period, title_matches_period
from .pdf_promotion import _archived_ok, _next_available_snapshot, _utc_now


SCHEMA = "aquant.cninfo.000422-h124-revenue-presented-prior-chain.v1"
VERIFIER = "machine:cninfo-000422-h124-revenue-presented-prior-chain-v1"
REVIEW_PREFIX = "cninfo_machine_000422_h124_revenue_chain_"
STOCK_CODE = "000422"
INSTRUMENT_ID = "000422"
ISSUER = "SZ.000422"
COMPANY = "湖北宜化化工股份有限公司"
SHORT_NAME = "湖北宜化"
ORGANIZATION_ID = "gssz0000422"
FACT_PERIOD = date(2024, 6, 30)
PRESENTING_PERIOD = date(2025, 6, 30)
METRIC = "revenue"
ORIGINAL_ROLE = "ORIGINAL_CURRENT"
PRESENTED_ROLE = "PRESENTED_PRIOR"
CAUSE_IDS = ("1222991641", "1222991666", "1225183650", "1225183652")
CAPTURE_SHA256 = "sha256:0b4669952ca537fb1eb4f0a32cffac15281bbd9535153028d4154e5d9c05eaf9"
CAPTURE_CANONICAL_SHA256 = "sha256:8b8fbe6c0b130a6b482d915681be85944476ef1a5bafee7323aab751f43a1e42"

REPORT_BINDINGS = {
    ORIGINAL_ROLE: {
        "announcementId": "1220963862",
        "receiptId": "rcpt_d01d1847d6b162d3a7c34a69",
        "contentHash": "sha256:e8136a903b0c3f75affd1cabb18db9052bf1662d00b84fd3b99f2d9bfc8ff8f0",
        "documentUrl": "https://static.cninfo.com.cn/finalpage/2024-08-26/1220963862.PDF",
        "title": "2024年半年度报告", "sourceReportPeriodEnd": "2024-06-30",
        "factPeriodEnd": "2024-06-30", "columnRole": "CURRENT",
        "sourcePublishedDate": "2024-08-26", "valueYuan": "9061263220.34",
        "page": 66, "columnHeader": "2024年半年度",
    },
    PRESENTED_ROLE: {
        "announcementId": "1224559960",
        "receiptId": "rcpt_e30a9e2d8c7ff6f996fd14bd",
        "contentHash": "sha256:9d110cdb52096cc04fb358f886c447ebe58022813bcdeb8a3f7c2de237046f78",
        "documentUrl": "https://static.cninfo.com.cn/finalpage/2025-08-23/1224559960.PDF",
        "title": "2025年半年度报告", "sourceReportPeriodEnd": "2025-06-30",
        "factPeriodEnd": "2024-06-30", "columnRole": "PRIOR",
        "sourcePublishedDate": "2025-08-23", "valueYuan": "13189293125.38",
        "page": 66, "columnHeader": "2024年半年度",
        "currentHeader": "2025年半年度", "currentValueYuan": "12004585375.22",
    },
}

EXPECTED_CAUSES = {
    "1222991641": ("2025-04-03",
        "关于湖北宜化化工股份有限公司2024年度追溯调整前期财务报表数据的专项说明-信会师报字[2025]第ZE10074号",
        "sha256:9378ef0cd160da7ead1881ca1e4db5e50b00d5c45db6c0f773eab2a12406743a"),
    "1222991666": ("2025-04-03", "董事会关于2024年度追溯调整前期财务报表数据的说明",
        "sha256:18a7701e0d52d83f6c5d86eb4dab7c55c4be3af2283c30bf8a3ae1c9f135c207"),
    "1225183650": ("2026-04-25",
        "关于湖北宜化化工股份有限公司同一控制下企业合并追溯调整前期财务报表数据的专项说明 信会师报字[2026]第ZE10202号",
        "sha256:f46e817c54a818eae59a42579b4a0f5d8bef85707473d441c9a339e735168cb0"),
    "1225183652": ("2026-04-25", "关于同一控制下企业合并追溯调整前期财务报表数据的公告",
        "sha256:b090c18e1fae1a1335140676251959070a262d880d18cd10e1d15b639163fac1"),
}


class H124RevenueChainError(ValueError):
    """A fixed H124 revenue source or presented-prior lineage failed replay."""


_ReadOnlyArchive = fy24._ReadOnlyArchive
_canonical = fy24._canonical
_digest = fy24._digest


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise H124RevenueChainError(message)


def _version_id(role: str, predecessor_id: str | None = None) -> str:
    binding = REPORT_BINDINGS.get(role)
    _need(binding is not None, "unsupported H124 revenue version role")
    if role == ORIGINAL_ROLE:
        _need(predecessor_id is None, "H124 CURRENT cannot supersede another version")
        identity = (f"cninfo|{STOCK_CODE}|{FACT_PERIOD.isoformat()}|{METRIC}|"
                    f"{binding['announcementId']}|{binding['contentHash']}")
        return "mach_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    _need(bool(predecessor_id), "H125 presented PRIOR must supersede H124 CURRENT")
    identity = (f"cninfo|{STOCK_CODE}|{PRESENTING_PERIOD.isoformat()}|{FACT_PERIOD.isoformat()}|"
                f"PRIOR|{METRIC}|{binding['announcementId']}|{binding['contentHash']}")
    return "mach_cmp_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


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
          receipt.get("content_hash") == binding["contentHash"] and
          receipt.get("url") == binding["documentUrl"] and receipt.get("source_id") == "cninfo" and
          receipt.get("outcome") == "OK" and receipt.get("http_status") == 200,
          "selected report identity or archived PDF receipt differs for " + binding["announcementId"])
    return row


def _packet_document(source: Mapping[str, object], role: str) -> Mapping[str, object]:
    binding = REPORT_BINDINGS[role]
    docs = source.get("documentEvidenceById")
    _need(isinstance(docs, Mapping), "source packet lacks native report page evidence")
    item = docs.get(binding["announcementId"])
    _need(isinstance(item, Mapping), "source packet lacks report evidence " + binding["announcementId"])
    entry, metadata = item.get("entry"), item.get("metadata")
    receipts = item.get("receipts")
    pdf_receipt = receipts.get("pdf") if isinstance(receipts, Mapping) else None
    _need(isinstance(entry, Mapping) and isinstance(metadata, Mapping) and isinstance(pdf_receipt, Mapping) and
          entry.get("announcementId") == binding["announcementId"] and
          entry.get("secCode") == STOCK_CODE and entry.get("title") == binding["title"] and
          entry.get("announcedOn") == binding["sourcePublishedDate"] and
          entry.get("url") == binding["documentUrl"] and
          metadata.get("announcementId") == binding["announcementId"] and
          metadata.get("code") == STOCK_CODE and metadata.get("instrumentId") == ISSUER and
          metadata.get("periodEnd") == binding["sourceReportPeriodEnd"] and
          metadata.get("title") == binding["title"] and metadata.get("contentHash") == binding["contentHash"] and
          metadata.get("documentUrl") == binding["documentUrl"] and
          pdf_receipt.get("receipt_id") == binding["receiptId"] and
          pdf_receipt.get("content_hash") == binding["contentHash"] and
          pdf_receipt.get("source_id") == "cninfo" and pdf_receipt.get("outcome") == "OK" and
          pdf_receipt.get("http_status") == 200 and pdf_receipt.get("url") == binding["documentUrl"],
          "native page packet binding differs for " + binding["announcementId"])
    return item


def _report_bytes(archive, source: Mapping[str, object], role: str) -> tuple[Mapping, bytes, Mapping]:
    binding = REPORT_BINDINGS[role]
    packet_doc = _packet_document(source, role)
    receipt, body = _archived_ok(archive, binding["receiptId"], expected_url=binding["documentUrl"],
                                 expected_host="static.cninfo.com.cn")
    _need(receipt.get("content_hash") == binding["contentHash"] and
          len(body) == packet_doc["metadata"].get("byteSize"),
          "archived report bytes differ from receipt-bound packet " + binding["announcementId"])
    return receipt, body, packet_doc


def _compact(text: object) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _native_pages(body: bytes, packet_doc: Mapping, required: set[int]) -> dict[int, str]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(body))
    _need(not reader.is_encrypted and max(required) <= len(reader.pages),
          "required native physical PDF page is absent")
    packet_pages = packet_doc.get("pages", ())
    _need(isinstance(packet_pages, (list, tuple)), "native evidence pages are not a list")
    saved = {int(item.get("physicalPage")): item for item in packet_pages
             if isinstance(item, Mapping) and item.get("physicalPage") is not None}
    result: dict[int, str] = {}
    for physical_page in required:
        text = reader.pages[physical_page - 1].extract_text() or ""
        row = saved.get(physical_page)
        _need(isinstance(row, Mapping) and row.get("extraction") ==
              "pypdf native PDF text layer; no OCR" and
              re.sub(r"\s+", " ", str(row.get("text", ""))).strip() ==
              re.sub(r"\s+", " ", text).strip(),
              f"native page {physical_page} does not match its archived-text evidence")
        result[physical_page] = text
    return result


def _current_cell(body: bytes, binding: Mapping[str, object]) -> tuple[object, object, dict]:
    report_period = date.fromisoformat(str(binding["sourceReportPeriodEnd"]))
    candidate = sections.extract_section_facts(
        body, instrument_id=STOCK_CODE, company=COMPANY, period_end=report_period,
        announcement_id=str(binding["announcementId"]), document_url=str(binding["documentUrl"]),
        required_fields=(METRIC,),
    )
    verification = s2_machine_verification.verify_machine_evidence(
        body, candidate=candidate, expected_fields=(METRIC,),
    )
    item = candidate.by_field[METRIC]
    expected_header = binding.get("currentHeader", binding.get("columnHeader"))
    expected_amount = binding.get("currentValueYuan", binding["valueYuan"])
    good_paths = {check.path for field in verification.fields for check in field.checks
                  if field.field == METRIC and check.holds and
                  check.path in {"revenue_identity", "revenue_note_decomposition"}}
    _need(verification.verified and "revenue_note_decomposition" in good_paths and
          item.currency == "CNY" and item.amount_unit == "元" and
          item.pdf_page == REPORT_BINDINGS[ORIGINAL_ROLE]["page"] and
          item.column_header == expected_header and
          item.period_end == report_period and str(item.value_yuan) == expected_amount and
          (item.display_row_label or "").replace(" ", "").replace("：", ":") == "其中:营业收入",
          "strict machine CURRENT candidate differs for " + str(binding["announcementId"]))
    return candidate, verification, {
        "announcementId": binding["announcementId"], "valueYuan": str(item.value_yuan),
        "page": item.pdf_page, "columnHeader": item.column_header, "unit": item.amount_unit,
        "rowLabel": item.display_row_label, "currency": item.currency,
        "sourceRow": item.source_row, "sourceCells": list(item.source_cells),
        "successfulRevenuePaths": sorted(good_paths), "verification": verification.as_evidence(),
    }


def _presented_prior(body: bytes, current_candidate, current_verification) -> tuple[dict, dict]:
    statement_set = sections.extract_statement_sections(
        body, period_end=PRESENTING_PERIOD, roles=(sections.ROLE_CONSOLIDATED,),
        statements=("利润表",),
    )
    statement = statement_set.require(sections.ROLE_CONSOLIDATED, "利润表")
    row = statement.find(METRIC)
    _need(row is not None and statement.unit == "元" and row.unit == "元" and row.page == 66 and
          statement.current_header == "2025年半年度" and statement.prior_header == "2024年半年度" and
          (row.label or "").replace(" ", "").replace("：", ":") == "其中:营业收入" and
          str(row.amount) == REPORT_BINDINGS[PRESENTED_ROLE]["currentValueYuan"] and
          str(row.prior_amount) == REPORT_BINDINGS[PRESENTED_ROLE]["valueYuan"] and
          row.current_cell and row.prior_cell and current_verification.verified and
          str(current_candidate.by_field[METRIC].value_yuan) == str(row.amount),
          "H125 consolidated CURRENT/PRIOR roles or cells differ")

    note = sections.extract_revenue_note_decomposition(
        body, period_end=PRESENTING_PERIOD, default_unit=statement.unit,
    )
    _need(note.pdf_sha256 == _digest(body) and note.role == "合并" and note.unit == "元" and
          note.main_business.prior_cell and note.other_business.prior_cell and note.total.prior_cell and
          note.main_business.page == 157 and note.other_business.page == 157 and note.total.page == 157 and
          str(note.main_business.prior_amount) == "12628119533.00" and
          str(note.other_business.prior_amount) == "561173592.38" and
          str(note.total.prior_amount) == REPORT_BINDINGS[PRESENTED_ROLE]["valueYuan"] and
          str(note.current_revenue_yuan) == REPORT_BINDINGS[PRESENTED_ROLE]["currentValueYuan"] and
          note.main_business.prior_amount + note.other_business.prior_amount == note.total.prior_amount and
          note.total.prior_amount == row.prior_amount,
          "H125 revenue note does not independently decompose the 2024H1 PRIOR cell")
    prior = {
        "role": PRESENTED_ROLE, "metric": METRIC,
        "sourceReportPeriodEnd": PRESENTING_PERIOD.isoformat(),
        "factPeriodEnd": FACT_PERIOD.isoformat(), "columnRole": "PRIOR",
        "currentHeader": statement.current_header, "columnHeader": statement.prior_header,
        "page": row.page, "rowLabel": row.label, "unit": statement.unit,
        "sourceRow": row.raw_line, "currentCell": row.current_cell, "priorCell": row.prior_cell,
        "currentValueYuan": str(row.amount), "valueYuan": str(row.prior_amount),
        "priorIsNotOriginalStrictCurrent": True,
        "currentMachineVerification": current_verification.as_evidence(),
        "priorNoteDecomposition": {
            "page": note.total.page, "unit": note.unit, "pdfSha256": note.pdf_sha256,
            "mainPriorYuan": str(note.main_business.prior_amount),
            "otherPriorYuan": str(note.other_business.prior_amount),
            "totalPriorYuan": str(note.total.prior_amount),
            "currentTotalYuan": str(note.current_revenue_yuan),
            "rawLines": list(note.raw_lines),
        },
    }
    current = {
        "announcementId": REPORT_BINDINGS[PRESENTED_ROLE]["announcementId"],
        "valueYuan": str(row.amount), "page": row.page,
        "columnHeader": statement.current_header, "unit": statement.unit,
        "rowLabel": row.label, "sourceRow": row.raw_line,
        "verification": current_verification.as_evidence(),
    }
    return prior, current


def _revision_identity(pages: Mapping[int, str]) -> dict:
    p8 = _compact(pages[8])
    p66 = _compact(pages[66])
    p172 = _compact(pages[172])
    reason = "同一控制下企业合并"
    before_after = "营业收入（元）12,004,585,375.229,061,263,220.3413,189,293,125.38"
    identity = ("宜昌新发产业投资有限公司100.00%同受湖北宜化集团有限责任公司"
                "控制，且该控制非暂时性2025年05月30日")
    _need(reason in p8 and before_after in p8,
          "H125 p8 lacks the exact same-control cause and 2024H1 revenue before/after row")
    _need("其中：营业收入12,004,585,375.2213,189,293,125.38" in p66,
          "H125 p66 lacks the exact 2024H1 consolidated revenue PRIOR row")
    _need(identity in p172,
          "H125 p172 does not bind the same-control 100% 宜昌新发 merger and date")
    return {
        "announcementId": REPORT_BINDINGS[PRESENTED_ROLE]["announcementId"],
        "p8": {"page": 8, "reason": reason,
               "beforeYuan": "9061263220.34", "afterYuan": "13189293125.38",
               "currentH125Yuan": "12004585375.22"},
        "p66": {"page": 66, "currentHeader": "2025年半年度",
                "priorHeader": "2024年半年度", "priorYuan": "13189293125.38"},
        "mergerIdentity": {"page": 172, "entity": "宜昌新发产业投资有限公司",
            "ownership": "100.00%", "controller": "湖北宜化集团有限责任公司",
            "controlNonTemporary": True, "mergerDate": "2025-05-30"},
        "p8RevenueRow": pages[8], "p172MergerRow": pages[172],
    }


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
    _need(source.get("currentIndexCaptureSha256") == CAPTURE_SHA256 and
          _digest(_canonical(capture).encode("utf-8")) == CAPTURE_CANONICAL_SHA256 and
          source.get("currentIndexCaptureCanonicalSha256") == CAPTURE_CANONICAL_SHA256,
          "Nov3 current index capture digest differs")
    index = capture.get("currentCrossIndex", {})
    pages = index.get("pages", ())
    _need(len(index.get("allEntries", ())) == 573 and len(pages) == 20 and
          len(index.get("financialRevisionCandidates", ())) == 4 and
          sorted(str(item.get("announcementId")) for item in index["financialRevisionCandidates"]) ==
          list(CAUSE_IDS), "Nov3 all-category/cause closure is not exact")
    regular_rows = list(capture.get("regularIndexes", ()))
    expected_periods = {"2024-06-30", "2024-12-31", "2025-06-30",
                        "2025-12-31", "2026-06-30", "2026-09-30"}
    _need({str(item.get("periodEnd")) for item in regular_rows} == expected_periods,
          "Nov3 regular-index period set differs")
    for item in regular_rows:
        period = str(item["periodEnd"])
        expected_through = "2026-06-30" if period == "2024-06-30" else "2026-11-03"
        _need(item.get("complete") is True and item.get("termination") == "has_more_false" and
              item.get("queryThrough") == expected_through and item.get("fullReportCandidateCount") == 1 and
              len(item.get("pages", ())) == 1 and item["pages"][0].get("hasMore") is False,
              "one of the six Nov3 regular indexes is incomplete: " + period)
    page_links = [(item.get("page"), item.get("responseReceiptId"), item.get("responseContentHash"),
                   item.get("requestBodyHash")) for item in pages]
    expected_links = [(item.get("page"), item.get("responseReceiptId"), item.get("responseContentHash"),
                       item.get("requestBodyHash")) for item in company.get("allCategoryPages", ())]
    _need(page_links == expected_links, "captured all-category pages differ from source company receipts")

    verified_regular = {}
    for role in (ORIGINAL_ROLE, PRESENTED_ROLE):
        binding = REPORT_BINDINGS[role]
        period = binding["sourceReportPeriodEnd"]
        matches = [item for item in regular_rows if item.get("periodEnd") == period]
        _need(len(matches) == 1, "Nov3 regular index is not unique for " + period)
        captured = matches[0]
        matches_full = captured.get("fullReportMatches", ())
        expected_through = "2026-06-30" if role == ORIGINAL_ROLE else "2026-11-03"
        _need(captured.get("queryThrough") == expected_through and captured.get("complete") is True and
              captured.get("termination") == "has_more_false" and
              captured.get("expectedAnnouncementId") == binding["announcementId"] and
              captured.get("expectedAnnouncementMatchCount") == 1 and
              captured.get("fullReportCandidateCount") == 1 and len(matches_full) == 1 and
              matches_full[0].get("announcementId") == binding["announcementId"] and
              matches_full[0].get("secCode") == STOCK_CODE and
              matches_full[0].get("documentUrl") == binding["documentUrl"] and
              matches_full[0].get("title") == binding["title"],
              "regular index does not uniquely bind " + binding["announcementId"])
        report_record = _record(company, role)
        selected_receipt_id = report_record.get("indexReceiptId")
        selected_page_rows = [item for item in pages if item.get("responseReceiptId") == selected_receipt_id]
        _need(len(selected_page_rows) == 1 and
              selected_page_rows[0].get("responseContentHash") == report_record.get("indexResponseHash") and
              selected_page_rows[0].get("firstSeenAt") == report_record.get("indexFirstSeenAt"),
              "selected report is detached from its unique current all-category receipt")
        selected_page = selected_page_rows[0]
        selected_verified = fy24._receipt_page(
            archive, selected_page,
            expected_detail_prefix=("cross-category:000422:2024-06-30~2026-11-03:page=" +
                                    str(selected_page.get("page")) + ":request="),
        )
        _need(selected_verified["receiptId"] == selected_receipt_id and
              selected_verified["contentHash"] == report_record["indexResponseHash"] and
              selected_verified["firstSeenAt"] == report_record["indexFirstSeenAt"],
              "selected all-category receipt metadata differs")
        selected_announcements = parse_announcements(selected_verified["body"])
        selected_matches = [item for item in selected_announcements
                            if item.announcement_id == binding["announcementId"] and item.sec_code == STOCK_CODE]
        selected_url = ((selected_matches[0].detail_url() or "").replace("http://", "https://")
                        if len(selected_matches) == 1 else "")
        _need(len(selected_matches) == 1 and selected_url == binding["documentUrl"] and
              selected_matches[0].title == binding["title"],
              "all-category page lacks the unique selected report row")

        page_rows = captured.get("pages", ())
        _need(len(page_rows) == 1 and page_rows[0].get("page") == 1 and
              page_rows[0].get("hasMore") is False and
              matches_full[0].get("pageReceiptId") == page_rows[0].get("responseReceiptId") and
              matches_full[0].get("pageContentHash") == page_rows[0].get("responseContentHash"),
              "current regular-index match is detached from its page response")
        verified = fy24._receipt_page(
            archive, page_rows[0],
            expected_detail_prefix="report-index:000422:" + period + ":page=1:",
        )
        parsed = parse_announcements(verified["body"])
        day = parse_report_period(period)
        actual = [item for item in parsed if item.sec_code == STOCK_CODE and
                  title_matches_period(item.title, day)]
        actual_url = ((actual[0].detail_url() or "").replace("http://", "https://")
                      if len(actual) == 1 else "")
        _need(len(actual) == 1 and actual[0].announcement_id == binding["announcementId"] and
              actual_url == binding["documentUrl"] and actual[0].title == binding["title"],
              "archived regular index response does not contain the unique report")
        verified_regular[role] = {"periodEnd": period, "announcementId": binding["announcementId"],
            "receiptId": verified["receiptId"], "contentHash": verified["contentHash"],
            "firstSeenAt": verified["firstSeenAt"], "requestBodyHash": verified["requestBodyHash"]}
    return {"status": "PASS", "captureSha256": CAPTURE_SHA256,
            "allCategoryEntryCount": 573, "allCategoryPageCount": 20,
            "financialCauseIds": list(CAUSE_IDS), "regularIndexes": verified_regular}


def _check_packet_cells(source: Mapping[str, object], original: Mapping, prior: Mapping) -> None:
    current = source.get("current_cell")
    _need(isinstance(current, Mapping) and current.get("announcementId") ==
          REPORT_BINDINGS[ORIGINAL_ROLE]["announcementId"] and
          current.get("periodEnd") == FACT_PERIOD.isoformat() and current.get("metric") == METRIC and
          current.get("columnRole") == "CURRENT" and current.get("columnHeader") == "2024年半年度" and
          current.get("rowLabel") == "其中：营业收入" and current.get("valueYuan") == "9061263220.34" and
          current.get("unit") == "元" and current.get("physicalPage") == 66 and
          current.get("contentHash") == REPORT_BINDINGS[ORIGINAL_ROLE]["contentHash"] and
          current.get("pdfReceiptId") == REPORT_BINDINGS[ORIGINAL_ROLE]["receiptId"],
          "source packet H124 CURRENT cell differs")
    columns = source.get("columns")
    h125 = columns.get(REPORT_BINDINGS[PRESENTED_ROLE]["announcementId"]) if isinstance(columns, Mapping) else None
    _need(isinstance(h125, Mapping) and h125.get("factPeriodEnd") == PRESENTING_PERIOD.isoformat() and
          h125.get("priorFactPeriodEnd") == FACT_PERIOD.isoformat() and
          h125.get("priorColumnRole") == PRESENTED_ROLE and
          h125.get("currentHeader") == "2025年半年度" and h125.get("priorHeader") == "2024年半年度" and
          h125.get("currentRevenueYuan") == REPORT_BINDINGS[PRESENTED_ROLE]["currentValueYuan"] and
          h125.get("priorRevenueYuan") == REPORT_BINDINGS[PRESENTED_ROLE]["valueYuan"] and
          h125.get("physicalPage") == 66 and h125.get("reasonPhysicalPage") == 8 and
          h125.get("reason") == "同一控制下企业合并" and
          h125.get("currentReportContentHash") == REPORT_BINDINGS[PRESENTED_ROLE]["contentHash"] and
          h125.get("pdfReceiptId") == REPORT_BINDINGS[PRESENTED_ROLE]["receiptId"],
          "source packet H125 prior/reason boundary differs")
    _need(original.get("valueYuan") == current.get("valueYuan") and
          prior.get("valueYuan") == h125.get("priorRevenueYuan") and
          prior.get("currentValueYuan") == h125.get("currentRevenueYuan"),
          "independently parsed native cells differ from the source packet")


def _check_cause_set(source: Mapping[str, object]) -> None:
    _need(sorted(str(aid) for aid in source.get("financialCauseIds", ())) == list(CAUSE_IDS),
          "complete H124 captured financial-cause set differs")
    for aid, (published, title, content_hash) in EXPECTED_CAUSES.items():
        records = [row for row in source.get("documents", ())
                   if isinstance(row, Mapping) and str(row.get("announcementId")) == aid]
        _need(len(records) == 1, "captured financial cause is absent or duplicated: " + aid)
        row = records[0]
        if (row.get("title") != title or row.get("periodEnd") is not None or
                row.get("documentUrl") != f"https://static.cninfo.com.cn/finalpage/{published}/{aid}.PDF" or
                row.get("contentHash") != content_hash or row.get("code") != STOCK_CODE or
                row.get("instrumentId") != ISSUER):
            raise H124RevenueChainError("captured financial cause identity differs: " + aid)


def _impact_replay(archive, source: Mapping[str, object]) -> dict:
    target_scope = source.get("targetScope")
    _need(isinstance(target_scope, (list, tuple)) and
          tuple(target_scope) == ("000422|revenue|2024-06-30",) and
          source.get("targetRevisionImpactProof") is None and
          source.get("revisionImpactProofScope", {}).get("demandKey") ==
          "000422|revenue|2024-12-31" and
          source.get("revisionImpactProofScope", {}).get("notForH124Replay") is True,
          "FY24 annual impact proof is not segregated from the H124 target")
    fresh = impact.assess_revision_impact(
        archive_root=archive.root, company=source["company"],
        target_period_end=FACT_PERIOD, target_metrics=(METRIC,),
        issuer_name=COMPANY, documents=source["documents"],
    )
    proof = fresh.as_dict()
    outcomes = [row for row in proof.get("outcomes", ())
                if isinstance(row, Mapping) and row.get("metric") == METRIC]
    _need(len(outcomes) == 1, "fresh H124 impact has no unique revenue outcome")
    outcome = outcomes[0]
    impact_proof = outcome.get("impactProof")
    _need(isinstance(impact_proof, Mapping),
          "fresh H124 impact has no finite revenue proof: " + str(outcome.get("reason")))
    comparisons = [row for row in outcome.get("comparisons", ())
                   if isinstance(row, Mapping) and
                   row.get("announcementId") == REPORT_BINDINGS[PRESENTED_ROLE]["announcementId"] and
                   row.get("factPeriodEnd") == FACT_PERIOD.isoformat() and row.get("columnRole") == "PRIOR"]
    _need(outcome.get("status") == "AFFECTS" and outcome.get("admissionStatus") == "NOT_ADMITTED" and
          impact_proof.get("basis") ==
          "FINITE_000422_H124_REVENUE_CURRENT_TO_H125_PRESENTED_PRIOR" and
          outcome.get("currentSource", {}).get("announcementId") ==
          REPORT_BINDINGS[ORIGINAL_ROLE]["announcementId"] and
          outcome.get("currentSource", {}).get("amountYuan") == REPORT_BINDINGS[ORIGINAL_ROLE]["valueYuan"] and
          outcome.get("unresolvedSuccessorIds") == [] and outcome.get("missingReportSourceIds") == [] and
          len(comparisons) == 1 and comparisons[0].get("amountYuan") ==
          REPORT_BINDINGS[PRESENTED_ROLE]["valueYuan"] and comparisons[0].get("changed") is True and
          impact_proof.get("causalAttribution") is None,
          "fresh source impact does not prove the exact H124-to-H125 difference conservatively")
    return {"proof": proof, "outcome": outcome,
            "presentedPriorComparison": comparisons[0]}


def _replay_sources(archive, source: Mapping[str, object], saved_impact: Mapping | None = None) -> dict:
    company = fy24._company(source)
    # The 000422 H124 packet is a separate financial profile; never reuse the
    # unchanged annual proof carried in that source packet.
    _check_cause_set(source)
    h124_record = _record(company, ORIGINAL_ROLE)
    h125_record = _record(company, PRESENTED_ROLE)
    h124_receipt, h124_body, h124_packet = _report_bytes(archive, source, ORIGINAL_ROLE)
    h125_receipt, h125_body, h125_packet = _report_bytes(archive, source, PRESENTED_ROLE)

    h124_candidate, h124_machine, h124_current = _current_cell(
        h124_body, REPORT_BINDINGS[ORIGINAL_ROLE])
    h125_candidate, h125_machine, h125_current = _current_cell(
        h125_body, REPORT_BINDINGS[PRESENTED_ROLE])
    h125_prior, h125_current_cell = _presented_prior(h125_body, h125_candidate, h125_machine)
    native_h124 = _native_pages(h124_body, h124_packet, {66})
    native_h125 = _native_pages(h125_body, h125_packet, {8, 66, 172})
    restatement = _revision_identity(native_h125)
    _need(h124_current["valueYuan"] == REPORT_BINDINGS[ORIGINAL_ROLE]["valueYuan"] and
          h125_prior["valueYuan"] == REPORT_BINDINGS[PRESENTED_ROLE]["valueYuan"] and
          h125_current_cell["valueYuan"] == REPORT_BINDINGS[PRESENTED_ROLE]["currentValueYuan"],
          "native H124/H125 current and presented-prior values differ")
    _check_packet_cells(source, h124_current, h125_prior)

    closure = _verify_index_capture(archive, source, company)
    impact_data = (_impact_replay(archive, source) if saved_impact is None else None)
    if saved_impact is not None:
        fresh = impact.replay_revision_impact(
            saved_impact, archive_root=archive.root, company=company,
            target_period_end=FACT_PERIOD, target_metrics=(METRIC,),
            issuer_name=COMPANY, documents=source["documents"],
        )
        impact_data = {"proof": dict(saved_impact),
                       "outcome": next(row for row in fresh.as_dict()["outcomes"]
                                       if row.get("metric") == METRIC)}
        compared = [row for row in impact_data["outcome"].get("comparisons", ())
                    if row.get("announcementId") == REPORT_BINDINGS[PRESENTED_ROLE]["announcementId"] and
                    row.get("factPeriodEnd") == FACT_PERIOD.isoformat() and row.get("columnRole") == "PRIOR"]
        _need(len(compared) == 1, "replayed H124 impact lost the H125 PRIOR comparison")
        impact_data["presentedPriorComparison"] = compared[0]
        _need(impact_data["outcome"].get("status") == "AFFECTS" and
              impact_data["outcome"].get("admissionStatus") == "NOT_ADMITTED" and
              impact_data["outcome"].get("impactProof", {}).get("basis") ==
              "FINITE_000422_H124_REVENUE_CURRENT_TO_H125_PRESENTED_PRIOR" and
              compared[0].get("amountYuan") == REPORT_BINDINGS[PRESENTED_ROLE]["valueYuan"] and
              compared[0].get("changed") is True and
              impact_data["outcome"].get("unresolvedSuccessorIds") == [] and
              impact_data["outcome"].get("missingReportSourceIds") == [],
              "replayed H124 impact differs from AFFECTS / NOT_ADMITTED")

    proof = impact_data["proof"]
    outcome = impact_data["outcome"]
    comparison = impact_data["presentedPriorComparison"]
    _need(str(comparison.get("amountYuan")) == h125_prior["valueYuan"] and
          str(outcome.get("currentSource", {}).get("amountYuan")) == h124_current["valueYuan"],
          "impact outcome values differ from independently parsed native cells")
    return {
        "originalCurrent": h124_current, "presentedPrior": h125_prior,
        "presentingReportCurrent": h125_current_cell, "h125Restatement": restatement,
        "impactProof": proof,
        "impactSummary": {"status": outcome["status"], "admissionStatus": outcome["admissionStatus"],
            "currentAnnouncementId": outcome["currentSource"]["announcementId"],
            "currentValueYuan": outcome["currentSource"]["amountYuan"],
            "priorAnnouncementId": comparison["announcementId"],
            "priorValueYuan": comparison["amountYuan"], "changed": comparison["changed"],
            "causalAttribution": None},
        "indexClosure": closure,
        "sourceReceipts": {
            ORIGINAL_ROLE: {"receiptId": h124_receipt["receipt_id"],
                "contentHash": h124_receipt["content_hash"],
                "firstSeenAt": h124_receipt["first_seen_at"]},
            PRESENTED_ROLE: {"receiptId": h125_receipt["receipt_id"],
                "contentHash": h125_receipt["content_hash"],
                "firstSeenAt": h125_receipt["first_seen_at"]},
        },
    }


def _source_times(*values: object, completed: datetime) -> list[datetime]:
    found: list[datetime] = []

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key in {"first_seen_at", "firstSeenAt", "indexFirstSeenAt"} and isinstance(child, str):
                    stamp = datetime.fromisoformat(child.replace("Z", "+00:00"))
                    _need(stamp.tzinfo is not None and stamp.utcoffset() is not None and stamp <= completed,
                          "source observation clock is invalid or follows completion")
                    found.append(stamp)
                else:
                    visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    for value in values:
        visit(value)
    return found


def _calendar_and_pit_replay(evidence: Mapping[str, object], archive, replay: Mapping[str, object],
                             completed: datetime) -> datetime:
    checked = datetime.fromisoformat(str(evidence["calendar_checked_at"]))
    _need(checked.tzinfo is not None and checked.utcoffset() is not None and checked <= completed,
          "calendar review timestamp is invalid")
    calendar = evidence["calendar_evidence"]
    days, verified = s2_calendar_evidence.verify_calendar_evidence(
        calendar, archive, now=checked,
        published_on=date.fromisoformat(REPORT_BINDINGS[ORIGINAL_ROLE]["sourcePublishedDate"]),
    )
    _need(verified == calendar and [day.isoformat() for day in days] == evidence["trading_calendar"],
          "saved calendar evidence differs from read-only replay")
    source_times = _source_times(evidence["source_input"], evidence["impact_proof"], calendar,
                                 completed=completed)
    source_times.extend(datetime.fromisoformat(item["firstSeenAt"])
                        for item in replay["sourceReceipts"].values())
    source_times.extend(datetime.fromisoformat(item["firstSeenAt"])
                        for item in replay["indexClosure"]["regularIndexes"].values())
    date_floors = [date_only_available_at(
        date.fromisoformat(REPORT_BINDINGS[role]["sourcePublishedDate"]), days)[0]
        for role in (ORIGINAL_ROLE, PRESENTED_ROLE)]
    available = _next_available_snapshot(max([completed, *date_floors, *source_times]), days)
    recorded = datetime.fromisoformat(str(evidence["available_at"]))
    _need(recorded.tzinfo is not None and recorded.utcoffset() is not None and
          recorded > completed and recorded == available,
          "PIT availability does not follow the captured report/index/calendar clocks")
    return recorded


def _target_exists(repository: FinancialFactRepository) -> bool:
    with closing(fy24.connect(repository.db_path, read_only=True)) as con:
        count = con.execute("""SELECT count(*) FROM financial_fact
            WHERE instrument_id=? AND metric=? AND period_end=? AND statement_scope=?
              AND profit_scope IS NULL AND currency=? AND raw_unit=?""",
            (INSTRUMENT_ID, METRIC, FACT_PERIOD.isoformat(), StatementScope.CONSOLIDATED.value,
             "CNY", "yuan")).fetchone()[0]
    return count > 0


def promote_000422_h124_revenue_version_chain(
    *, repository: FinancialFactRepository, archive, source: Mapping[str, object],
    calendar_evidence: Mapping[str, object],
) -> tuple[FinancialFact, FinancialFact]:
    """Append the fixed CURRENT and H125 PRESENTED_PRIOR to a private clone."""
    rights = default_rights().get("cninfo")
    _need(rights is not None and rights.open_for("research_use") and rights.open_for("local_storage"),
          "CNINFO research/local-storage rights are closed")
    archive.con.execute("PRAGMA query_only=ON")
    started = _utc_now()
    replay = _replay_sources(archive, source)
    days, verified_calendar = s2_calendar_evidence.verify_calendar_evidence(
        dict(calendar_evidence), archive, now=started,
        published_on=date.fromisoformat(REPORT_BINDINGS[ORIGINAL_ROLE]["sourcePublishedDate"]),
    )
    _need(verified_calendar == calendar_evidence, "calendar source evidence changed on replay")
    _need(not _target_exists(repository), "H124 revenue already has a stored root; refusing a duplicate chain")

    completed = _utc_now()
    receipts = replay["sourceReceipts"]
    proof_times = [datetime.fromisoformat(item["firstSeenAt"]) for item in receipts.values()]
    proof_times.extend(datetime.fromisoformat(item["firstSeenAt"])
                       for item in replay["indexClosure"]["regularIndexes"].values())
    date_floors = [date_only_available_at(
        date.fromisoformat(REPORT_BINDINGS[role]["sourcePublishedDate"]), days)[0]
        for role in (ORIGINAL_ROLE, PRESENTED_ROLE)]
    observed = _source_times(source, replay["impactProof"], verified_calendar, completed=completed)
    available = _next_available_snapshot(max([completed, *date_floors, *proof_times, *observed]), days)

    original, prior = replay["originalCurrent"], replay["presentedPrior"]
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
        "formal_admission": "NOT_ADMITTED",
        "candidate_disposition": "PRESENTED_PRIOR_AFFECTS_H124_REVENUE_VERSION_CHAIN",
        "causal_attribution": None, "fact_version_ids": [root_id, prior_id],
        "version_ids_by_metric": {METRIC: [root_id, prior_id]},
        "review_member_version_ids": [root_id, prior_id],
        "completed_at": completed.isoformat(), "available_at": available.isoformat(),
        "source_input": dict(source), "source_replay": {
            key: value for key, value in replay.items() if key != "impactProof"},
        "impact_proof": replay["impactProof"],
        "calendar_evidence": verified_calendar, "calendar_checked_at": started.isoformat(),
        "trading_calendar": [day.isoformat() for day in days],
        "archive": {"root": str(Path(archive.root).resolve()),
                    "database_path": str(Path(archive.root).resolve() / "meta.sqlite")},
        "versions": [
            {"role": ORIGINAL_ROLE, "versionId": root_id, "supersedesId": None,
             "announcementId": REPORT_BINDINGS[ORIGINAL_ROLE]["announcementId"],
             "receiptId": REPORT_BINDINGS[ORIGINAL_ROLE]["receiptId"],
             "contentHash": REPORT_BINDINGS[ORIGINAL_ROLE]["contentHash"],
             "sourceReportPeriodEnd": FACT_PERIOD.isoformat(), "factPeriodEnd": FACT_PERIOD.isoformat(),
             "columnRole": "CURRENT", "columnHeader": original["columnHeader"],
             "page": original["page"], "rowLabel": original["rowLabel"], "unit": original["unit"],
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


def accepts_000422_h124_revenue_version_chain(
    row: Mapping[str, object], member_ids: set[str], facts: Mapping[str, FinancialFact], *,
    accepted_links: Mapping[str, tuple[str, str]],
) -> bool:
    """Fail-closed public-reader replay for this fixed two-version H124 revenue chain."""
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
                evidence.get("candidate_disposition") != "PRESENTED_PRIOR_AFFECTS_H124_REVENUE_VERSION_CHAIN" or
                evidence.get("causal_attribution") is not None or
                not row["bundle_id"].startswith(REVIEW_PREFIX)):
            return False
        ids = evidence.get("version_ids_by_metric", {}).get(METRIC, ())
        if (set(evidence.get("version_ids_by_metric", {})) != {METRIC} or len(ids) != 2 or
                ids != evidence.get("fact_version_ids") or ids != evidence.get("review_member_version_ids") or
                member_ids != set(ids) or member_ids & accepted_links.keys() or
                set(ids) - facts.keys()):
            return False
        expected_bundle = REVIEW_PREFIX + hashlib.sha256(
            f"{SCHEMA}|{INSTRUMENT_ID}|{FACT_PERIOD.isoformat()}|{ids[0]}|{ids[1]}".encode("utf-8")
        ).hexdigest()[:32]
        if row["bundle_id"] != expected_bundle:
            return False
        archive_data = evidence["archive"]
        archive = _ReadOnlyArchive(str(archive_data["database_path"]), str(archive_data["root"]))
        completed = datetime.fromisoformat(evidence["completed_at"])
        if (completed.tzinfo is None or completed.utcoffset() is None or
                completed > datetime.now(timezone.utc)):
            return False
        replay = _replay_sources(archive, evidence["source_input"], evidence["impact_proof"])
        if (replay.pop("impactProof") != evidence["impact_proof"] or
                replay != evidence["source_replay"] or archive.con.total_changes != 0):
            return False
        available = _calendar_and_pit_replay(evidence, archive, replay, completed)
        versions = evidence["versions"]
        if len(versions) != 2 or [item.get("role") for item in versions] != [ORIGINAL_ROLE, PRESENTED_ROLE]:
            return False
        predecessor = None
        for item, role in zip(versions, (ORIGINAL_ROLE, PRESENTED_ROLE), strict=True):
            fact = facts.get(item.get("versionId"))
            binding = REPORT_BINDINGS[role]
            expected_id = _version_id(role, predecessor)
            amount = Decimal(binding["valueYuan"])
            if (fact is None or item.get("versionId") != expected_id or
                    item.get("supersedesId") != predecessor or fact.version_id != expected_id or
                    fact.instrument_id != INSTRUMENT_ID or fact.metric != METRIC or fact.period_end != FACT_PERIOD or
                    fact.statement_scope is not StatementScope.CONSOLIDATED or fact.profit_scope is not None or
                    fact.currency != "CNY" or fact.raw_unit != "yuan" or fact.value != amount or
                    fact.source_id != "cninfo" or fact.source_document_id != binding["announcementId"] or
                    fact.content_hash != binding["contentHash"] or fact.supersedes_id != predecessor or
                    fact.source_published_date != date.fromisoformat(binding["sourcePublishedDate"]) or
                    fact.source_published_at is not None or fact.timestamp_precision is not TimestampPrecision.DATE or
                    fact.availability_basis is not AvailabilityBasis.OBSERVED or
                    fact.pit_mode is not PitMode.LIVE_OBSERVED or
                    fact.first_seen_at.isoformat() != replay["sourceReceipts"][role]["firstSeenAt"] or
                    fact.ingested_at.isoformat() != evidence["completed_at"] or fact.available_at != available or
                    item.get("announcementId") != binding["announcementId"] or
                    item.get("receiptId") != binding["receiptId"] or
                    item.get("contentHash") != binding["contentHash"] or
                    item.get("sourceReportPeriodEnd") != binding["sourceReportPeriodEnd"] or
                    item.get("factPeriodEnd") != FACT_PERIOD.isoformat() or
                    item.get("columnRole") != binding["columnRole"] or item.get("valueYuan") != str(amount)):
                return False
            source_cell = replay["originalCurrent"] if role == ORIGINAL_ROLE else replay["presentedPrior"]
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
    "SCHEMA", "VERIFIER", "REVIEW_PREFIX", "H124RevenueChainError",
    "promote_000422_h124_revenue_version_chain", "accepts_000422_h124_revenue_version_chain",
]
