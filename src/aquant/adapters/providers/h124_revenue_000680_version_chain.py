"""Finite 000680 H124 revenue CURRENT -> H125 presented-prior chain.

The ordinary S2 classifier and the existing 000680 H125 index/archive
contract remain authoritative. This module binds their AFFECTS result to the
two receipt-bound revenue observations without assigning a single cause or
admitting either version.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
from contextlib import closing
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from ...domain.data.db import connect
from ...domain.data.pit import (
    AvailabilityBasis, PitMode, TimestampPrecision, date_only_available_at,
)
from ...domain.data.rights import REGISTER_VERSION, default_rights
from ...domain.fundamentals.fact_repository import FactReviewBundle, FinancialFactRepository
from ...domain.fundamentals.versioned import FinancialFact, StatementScope
from . import (
    cninfo_statement_geometry as geometry,
    cninfo_statement_sections as sections,
    h125_profit_pair_000680_review_inputs as h125_inputs,
    h125_profit_pair_000680_version_chain as h125,
    s2_calendar_evidence,
    s2_machine_verification,
    s2_revision_impact as impact,
)
from .pdf_promotion import _archived_ok, _next_available_snapshot, _utc_now


SCHEMA = "aquant.cninfo.000680-h124-revenue-presented-prior-chain.v1"
SOURCE_SCHEMA = "aquant.cninfo.000680-h124-revenue-source-candidate.v1"
VERIFIER = "machine:cninfo-000680-h124-revenue-presented-prior-chain-v1"
REVIEW_PREFIX = "cninfo_machine_000680_h124_revenue_chain_"
STOCK_CODE = "000680"
INSTRUMENT_ID = "000680"
ISSUER = "SZ.000680"
COMPANY = "山推工程机械股份有限公司"
SHORT_NAME = "山推股份"
ORGANIZATION_ID = "gssz0000680"
FACT_PERIOD = date(2024, 6, 30)
PRESENTING_PERIOD = date(2025, 6, 30)
METRIC = "revenue"
ORIGINAL_ROLE = "ORIGINAL_CURRENT"
PRESENTED_ROLE = "PRESENTED_PRIOR"

REPORT_BINDINGS = {
    ORIGINAL_ROLE: {
        "announcementId": "1220951759",
        "receiptId": "rcpt_e0c1dba5d32d4c6de2c4a02e",
        "contentHash": "sha256:683eed50a0c341d10dff5029351318079ec376402f0133092db47c00725f5138",
        "documentUrl": "https://static.cninfo.com.cn/finalpage/2024-08-23/1220951759.PDF",
        "title": "2024年半年度报告",
        "sourceReportPeriodEnd": "2024-06-30",
        "factPeriodEnd": "2024-06-30",
        "columnRole": "CURRENT",
        "sourcePublishedDate": "2024-08-23",
        "valueYuan": "6508010963.75",
        "page": 174,
        "columnHeader": "2024年半年度",
    },
    PRESENTED_ROLE: {
        "announcementId": "1224561048",
        "receiptId": "rcpt_7252c19331f45502f6232b6c",
        "contentHash": "sha256:d4dca5dcec17316a8094b65585454ed4e2e7fdc2af1e6e51f9935e075e39a39b",
        "documentUrl": "https://static.cninfo.com.cn/finalpage/2025-08-23/1224561048.PDF",
        "title": "2025年半年度报告",
        "sourceReportPeriodEnd": "2025-06-30",
        "factPeriodEnd": "2024-06-30",
        "columnRole": "PRIOR",
        "sourcePublishedDate": "2025-08-23",
        "valueYuan": "6798178192.94",
        "page": 183,
        "columnHeader": "2024年半年度",
        "currentHeader": "2025年半年度",
        "currentValueYuan": "7003573272.33",
    },
}

ANNEX_BINDINGS = {
    "1220951752": {
        "contentHash": "sha256:b7585bd52e2ec80e2f5b355cdaa9fe87f2985efee0077595b6d1f2db020fd0c1",
        "periodEnd": "2024-06-30",
        "revenueYuan": "6508010963.75",
        "columnRole": "CURRENT",
    },
    "1224561047": {
        "contentHash": "sha256:a3bca1a8f1becdd5394a0e58e5c96dcc1f980301b8703984ea325659b5172ceb",
        "periodEnd": "2025-06-30",
        "revenueYuan": "6798178192.94",
        "columnRole": "PRIOR",
    },
}

SELECTED_REPORT_IDS = {
    "2024-06-30": "1220951759",
    "2024-12-31": "1223217214",
    "2025-06-30": "1224561048",
    "2025-12-31": "1225008978",
    "2026-06-30": "1225466047",
}
_EXPECTED_PRIOR_GRID = {
    "page": 183,
    "tableBbox": (42.5, 101.84, 552.75, 722.59),
    "priorCellBbox": (450.0, 126.19, 552.75, 138.34),
    "rowLabel": "其中：营业收入",
    "currentCell": "7,003,573,272.33",
    "priorCell": "6,798,178,192.94",
}


class H124RevenueChainError(ValueError):
    """A fixed 000680 H124 revenue source or presented-prior replay failed."""


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise H124RevenueChainError(message)


def _candidate_evidence(candidate) -> dict:
    return {
        "instrumentId": candidate.instrument_id,
        "periodEnd": candidate.period_end.isoformat(),
        "versionLabel": candidate.version_label,
        "documentUrl": candidate.document_url,
        "pdfSha256": candidate.pdf_sha256,
        "pitEligible": candidate.pit_eligible,
        "reviewStatus": candidate.review_status,
        "candidates": [
            {
                "field": item.field,
                "valueYuan": str(item.value_yuan),
                "periodEnd": item.period_end.isoformat(),
                "reportPeriodText": item.report_period_text,
                "statement": item.statement,
                "pdfPage": item.pdf_page,
                "columnHeader": item.column_header,
                "amountUnit": item.amount_unit,
                "currency": item.currency,
                "sourceRow": item.source_row,
                "sourceCells": list(item.source_cells),
                "sourceCurrentCellIndex": item.source_current_cell_index,
                "sourcePriorCellIndex": item.source_prior_cell_index,
                "pdfSha256": item.pdf_sha256,
                "displayRowLabel": item.display_row_label,
            }
            for item in candidate.candidates
        ],
    }


def _version_id(role: str, predecessor_id: str | None = None) -> str:
    binding = REPORT_BINDINGS.get(role)
    _need(binding is not None, "unsupported 000680 H124 revenue version role")
    if role == ORIGINAL_ROLE:
        _need(predecessor_id is None, "H124 CURRENT cannot supersede another version")
        identity = (f"cninfo|{STOCK_CODE}|{FACT_PERIOD.isoformat()}|{METRIC}|"
                    f"{binding['announcementId']}|{binding['contentHash']}")
        prefix = "mach_000680_h124_"
    else:
        _need(bool(predecessor_id), "H125 presented PRIOR must supersede H124 CURRENT")
        identity = (f"cninfo|{STOCK_CODE}|{PRESENTING_PERIOD.isoformat()}|{FACT_PERIOD.isoformat()}|"
                    f"PRIOR|{METRIC}|{binding['announcementId']}|{binding['contentHash']}")
        prefix = "mach_cmp_000680_h124_"
    return prefix + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _source_document_map(archive, company: Mapping) -> tuple[dict, dict, list]:
    entries, _, _ = impact._index(archive.con, archive.root, dict(company))
    source_replay = h125_inputs.RELATIONSHIP_SOURCE_REPLAY
    source_rows = source_replay.get("sourceDocuments")
    _need(isinstance(source_rows, list) and len(source_rows) == 23,
          "pinned 000680 H125 source contract no longer has its 23-document closure")
    report_periods = {}
    for aid, entry in entries.items():
        parsed = impact._report_period(entry["title"], issuer_code=STOCK_CODE)
        if (parsed is not None and parsed.isoformat() in impact.PERIODS and
                impact.disclosure_role(entry["title"]) in (
                    impact.DisclosureRole.ORIGINAL_REPORT, impact.DisclosureRole.REVISED_REPORT)):
            report_periods[aid] = parsed.isoformat()
    documents_by_id = {}
    documents = []
    for item in source_rows:
        aid = str(item.get("announcementId"))
        entry = entries.get(aid)
        _need(entry is not None and item.get("matchesCurrentIndex") is True and
              item.get("receiptAndRawVerified") is True and
              entry.get("title") == item.get("title") and
              entry.get("documentUrl") == item.get("documentUrl") and
              entry.get("publishedDate") == item.get("publishedDate"),
              "pinned relationship document differs from the current cross-index: " + aid)
        receipt, body = impact._receipt(
            archive.con, archive.root, str(item.get("receiptId")),
            expected_hash=str(item.get("rawSha256")), expected_url=entry["documentUrl"],
        )
        _need(_digest(body) == item.get("rawSha256") and len(body) == item.get("byteSize") and
              receipt.get("first_seen_at") == item.get("receiptFirstSeenAt") and
              body.startswith(b"%PDF-"),
              "pinned relationship PDF bytes/receipt differ: " + aid)
        doc = {
            "announcementId": aid,
            "code": STOCK_CODE,
            "instrumentId": ISSUER,
            "title": entry["title"],
            "documentUrl": entry["documentUrl"],
            "publishedDate": entry["publishedDate"],
            "contentHash": item["rawSha256"],
            "byteSize": len(body),
            "receipt": receipt,
        }
        if aid in report_periods:
            doc["periodEnd"] = report_periods[aid]
        documents_by_id[aid] = doc
        documents.append(doc)
    for period, aid in SELECTED_REPORT_IDS.items():
        _need(report_periods.get(aid) == period and aid in documents_by_id,
              "canonical current report selection differs for " + period)
    expected_ids = {str(item["announcementId"]) for item in source_rows}
    _need(set(documents_by_id) == expected_ids and len(documents_by_id) == 23,
          "pinned issuer source set contains a missing or duplicate document")
    return entries, documents_by_id, documents


def _classifier_inputs(archive) -> tuple[dict, list, dict]:
    source_replay = h125_inputs.RELATIONSHIP_SOURCE_REPLAY
    company = deepcopy(source_replay["company"])
    entries, by_id, documents = _source_document_map(archive, company)
    selected = [by_id[aid] for _, aid in sorted(SELECTED_REPORT_IDS.items())]
    selected_ids = {str(row["announcementId"]) for row in selected}
    company["selectedReportRecords"] = selected
    company["relatedDocumentRecords"] = [
        row for row in documents if str(row["announcementId"]) not in selected_ids
    ]
    return company, documents, entries


def _extract_prior_revenue_grid_cell(body: bytes) -> dict:
    """Re-extract the H125 H124 comparative from its unique ruled-grid cell."""
    import pdfplumber

    binding = REPORT_BINDINGS[PRESENTED_ROLE]
    digest = _digest(body)
    _need(digest == binding["contentHash"] and body.startswith(b"%PDF-"),
          "H125 prior-grid input hash or PDF signature mismatch")
    found = []
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        _need(geometry._frontmatter(pdf, STOCK_CODE, PRESENTING_PERIOD) is not None,
              "H125 issuer and report period failed the grid identity check")
        titles = geometry._canonical_statement_titles(body)
        if not titles:
            for page_index, page in enumerate(pdf.pages):
                for line in (page.extract_text() or "").splitlines():
                    match = geometry._TITLE.fullmatch(geometry._compact(line))
                    if match:
                        titles.append((page_index, line, match.group(1), match.group(2)))
        located = []
        for page_index, line, role, statement in titles:
            matches = pdf.pages[page_index].search(
                r"\s*".join(re.escape(char) for char in re.sub(r"\s+", "", line))
            )
            if len(matches) == 1:
                located.append((page_index, float(matches[0]["top"]), float(matches[0]["bottom"]),
                                line, role, statement))
        located.sort()
        for position, (start, _, bottom, heading, role, statement) in enumerate(located):
            if role != "合并" or statement != "利润表":
                continue
            next_heading = located[position + 1] if position + 1 < len(located) else None
            end_page, end_top = ((next_heading[0], next_heading[1]) if next_heading
                                 else (min(start + 8, len(pdf.pages) - 1), float("inf")))
            if end_page - start > 8:
                continue
            page = pdf.pages[start]
            preamble = page.crop((0, bottom, page.width, min(page.height, bottom + 180))).extract_text() or ""
            if bottom > page.height - 120 and start + 1 < len(pdf.pages):
                next_text = pdf.pages[start + 1].crop(
                    (0, 0, pdf.pages[start + 1].width, 160),
                ).extract_text() or ""
                if not any(geometry._TITLE.fullmatch(geometry._compact(line))
                           for line in next_text.splitlines()):
                    preamble += "\n" + next_text
            units = [(line, match[1]) for line in preamble.splitlines()
                     for match in [geometry._UNIT.search(line)] if match]
            if len({unit for _, unit in units}) != 1:
                continue
            unit_declaration, unit = units[0]
            if unit != "元" or any(
                    re.search(r"币种\s*[:：]\s*(?!人民币|CNY)[A-Za-z\u4e00-\u9fff]+", line)
                    for line in preamble.splitlines()):
                continue
            context = None
            context_page = None
            for page_index in range(start, min(end_page, len(pdf.pages) - 1) + 1):
                current_page = pdf.pages[page_index]
                for table in geometry._ruled_page_tables(
                        current_page, PRESENTING_PERIOD, "利润表", preamble):
                    if ((page_index == start and table.bbox[1] < bottom) or
                            (page_index == end_page and table.bbox[3] > end_top)):
                        continue
                    header = geometry._header(
                        table, PRESENTING_PERIOD, "利润表", preamble, PRESENTING_PERIOD,
                    )
                    if header is not None:
                        context, context_page = header, page_index
                    if context is None:
                        continue
                    for row_index, row in enumerate(table.extract()):
                        cells = table.rows[row_index].cells
                        if len(row) != len(context["labels"]) or len(cells) != len(row):
                            continue
                        bounds = tuple(
                            None if cell is None else (round(cell[0], 3), round(cell[2], 3))
                            for cell in cells
                        )
                        if not geometry._amount_columns_match_header(
                                bounds, context, table.bbox, continuation=page_index > context_page):
                            continue
                        if not geometry._field_label(METRIC, row[0]):
                            continue
                        amount = geometry._amount(row[context["prior_column"]])
                        if amount is None:
                            continue
                        found.append({
                            "field": METRIC, "role": "合并", "statement": "利润表",
                            "sourceReportPeriodEnd": PRESENTING_PERIOD.isoformat(),
                            "factPeriodEnd": FACT_PERIOD.isoformat(), "columnRole": "PRIOR",
                            "page": page_index + 1, "headingPage": start + 1,
                            "heading": heading, "unit": unit, "unitDeclaration": unit_declaration,
                            "currentHeader": str(context["labels"][context["column"]]),
                            "priorHeader": str(context["labels"][context["prior_column"]]),
                            "rowLabel": str(row[0]), "currentCell": str(row[context["column"]]),
                            "priorCell": str(row[context["prior_column"]]),
                            "amountYuan": str(amount * geometry._SCALE[unit]),
                            "tableBbox": list(table.bbox),
                            "priorCellBbox": (None if cells[context["prior_column"]] is None
                                              else list(cells[context["prior_column"]])),
                            "headerCells": [str(value or "") for value in context["labels"]],
                            "priorColumn": context["prior_column"],
                            "sourceContentHash": digest,
                        })
    _need(len(found) == 1, "H125 H124H1 prior revenue grid cell is missing or ambiguous")
    item = found[0]
    _need(item["currentHeader"] == "2025年半年度" and
          item["priorHeader"] == "2024年半年度" and item["unit"] == "元" and
          item["role"] == "合并" and item["statement"] == "利润表" and
          item["page"] == _EXPECTED_PRIOR_GRID["page"] and
          geometry._compact(item["rowLabel"]) == geometry._compact(_EXPECTED_PRIOR_GRID["rowLabel"]) and
          item["currentCell"] == _EXPECTED_PRIOR_GRID["currentCell"] and
          item["priorCell"] == _EXPECTED_PRIOR_GRID["priorCell"] and
          tuple(round(float(value), 2) for value in item["tableBbox"]) ==
          _EXPECTED_PRIOR_GRID["tableBbox"] and
          tuple(round(float(value), 2) for value in item["priorCellBbox"]) ==
          _EXPECTED_PRIOR_GRID["priorCellBbox"],
          "H125 comparative grid cell differs from the exact native p183 geometry")
    return {"cell": item, "sourceContentHash": digest}


def _report_source(archive, docs_by_id: Mapping, role: str) -> tuple[dict, bytes, Mapping]:
    binding = REPORT_BINDINGS[role]
    doc = docs_by_id.get(binding["announcementId"])
    _need(isinstance(doc, Mapping), "canonical H124/H125 report is absent from source closure")
    _need(doc.get("code") == STOCK_CODE and doc.get("instrumentId") == ISSUER and
          doc.get("periodEnd") == binding["sourceReportPeriodEnd"] and
          doc.get("title") == binding["title"] and doc.get("documentUrl") == binding["documentUrl"] and
          doc.get("contentHash") == binding["contentHash"],
          "canonical report/index identity differs: " + binding["announcementId"])
    receipt, body = _archived_ok(
        archive, binding["receiptId"], expected_url=binding["documentUrl"],
        expected_host="static.cninfo.com.cn",
    )
    _need(receipt.get("content_hash") == binding["contentHash"] and
          receipt.get("byte_size") == len(body) and doc.get("byteSize") == len(body),
          "archived canonical report bytes differ: " + binding["announcementId"])
    return receipt, body, doc


def _read_source_pair(archive, docs_by_id: Mapping) -> dict:
    parsed_by_role, statement_by_role, reports = {}, {}, {}
    for role, binding in REPORT_BINDINGS.items():
        receipt, body, doc = _report_source(archive, docs_by_id, role)
        source_period = date.fromisoformat(binding["sourceReportPeriodEnd"])
        parsed = sections.extract_section_facts(
            body, instrument_id=STOCK_CODE, company=COMPANY, period_end=source_period,
            announcement_id=binding["announcementId"], document_url=binding["documentUrl"],
            required_fields=(METRIC,),
        )
        verification = s2_machine_verification.verify_machine_evidence(
            body, candidate=parsed, expected_fields=(METRIC,),
        )
        verifications = [item for item in verification.fields if item.field == METRIC]
        _need(verification.verified and len(verifications) == 1 and
              verifications[0].verified and
              "revenue_note_decomposition" in verifications[0].satisfied_paths and
              parsed.pdf_sha256 == binding["contentHash"] and METRIC in parsed.by_field,
              "native revenue verifier failed for " + role)
        statement_set = sections.extract_statement_sections(
            body, period_end=source_period, roles=(sections.ROLE_CONSOLIDATED,),
            statements=("利润表",),
        )
        statement = statement_set.require(sections.ROLE_CONSOLIDATED, "利润表")
        native = parsed.by_field[METRIC]
        expected_current_header = "2024年半年度" if role == ORIGINAL_ROLE else "2025年半年度"
        _need(statement.unit == "元" and statement.current_header == expected_current_header and
              native.currency == "CNY" and native.amount_unit == "元" and
              native.period_end == source_period,
              "native report currency/unit/period/header differs: " + role)
        statement_row = statement.find(METRIC)
        _need(statement_row is not None and
              Decimal(str(native.value_yuan)) == statement_row.amount * sections._UNIT_MULTIPLIER[statement.unit],
              "native candidate and consolidated profit statement disagree: " + role)
        reports[role] = {
            **binding,
            "sourcePublishedDate": doc["publishedDate"],
            "firstSeenAt": receipt["first_seen_at"],
            "byteSize": len(body),
        }
        parsed_by_role[role] = {
            "receipt": receipt, "body": body, "parsed": parsed,
            "verification": verification, "fieldVerification": verifications[0],
        }
        statement_by_role[role] = statement

    original_native = parsed_by_role[ORIGINAL_ROLE]["parsed"].by_field[METRIC]
    presented_native = parsed_by_role[PRESENTED_ROLE]["parsed"].by_field[METRIC]
    presented_statement = statement_by_role[PRESENTED_ROLE]
    prior_row = presented_statement.find(METRIC)
    _need(prior_row is not None and presented_statement.prior_header == "2024年半年度" and
          prior_row.prior_amount is not None,
          "H125 H124 comparative revenue row is absent")
    grid = _extract_prior_revenue_grid_cell(parsed_by_role[PRESENTED_ROLE]["body"])
    original_value = Decimal(str(original_native.value_yuan))
    presented_value = prior_row.prior_amount * sections._UNIT_MULTIPLIER[presented_statement.unit]
    cell = grid["cell"]
    native_label = geometry._compact(prior_row.label)
    grid_label = geometry._compact(cell["rowLabel"])
    note_reference = re.search(
        r"(?:(?:\([一二三四五六七八九十]+\))|(?:[一二三四五六七八九十]+[、.]))\d+$",
        native_label,
    )
    if note_reference:
        native_label = native_label[:note_reference.start()]
    checks = {
        "h124CurrentBoundToExpectedValue": original_value == Decimal("6508010963.75"),
        "h125CurrentBoundToExpectedValue": Decimal(str(presented_native.value_yuan)) == Decimal("7003573272.33"),
        "h125PriorStatementEqualsIndependentGrid": Decimal(cell["amountYuan"]) == presented_value,
        "h125CurrentStatementEqualsIndependentGrid": geometry._amount(cell["currentCell"]) == prior_row.amount,
        "h125PriorStatementCellEqualsIndependentGrid": geometry._amount(cell["priorCell"]) == prior_row.prior_amount,
        "revenueLabelMatchesWithoutNoteReference": grid_label == native_label,
    }
    _need(all(checks.values()),
          "H124/H125 statement parser and independent grid cells disagree: " +
          ",".join(key for key, holds in checks.items() if not holds))
    _need(presented_value == Decimal("6798178192.94") and original_value != presented_value,
          "H125 does not present a changed H124 revenue value")

    original_id = _version_id(ORIGINAL_ROLE)
    presented_id = _version_id(PRESENTED_ROLE, original_id)
    original_event = {
        "role": ORIGINAL_ROLE, "metric": METRIC, "valueYuan": str(original_value),
        "currency": "CNY", "rawUnit": "yuan", "unit": original_native.amount_unit,
        "statement": "利润表", "statementScope": "CONSOLIDATED",
        "announcementId": REPORT_BINDINGS[ORIGINAL_ROLE]["announcementId"],
        "receiptId": REPORT_BINDINGS[ORIGINAL_ROLE]["receiptId"],
        "contentHash": REPORT_BINDINGS[ORIGINAL_ROLE]["contentHash"],
        "sourceReportPeriodEnd": FACT_PERIOD.isoformat(), "factPeriodEnd": FACT_PERIOD.isoformat(),
        "columnRole": "CURRENT", "columnHeader": original_native.column_header,
        "sourcePublishedDate": reports[ORIGINAL_ROLE]["sourcePublishedDate"],
        "firstSeenAt": reports[ORIGINAL_ROLE]["firstSeenAt"],
        "sourceRow": original_native.source_row, "rowLabel": original_native.display_row_label,
        "page": original_native.pdf_page, "versionId": original_id, "supersedesId": None,
    }
    presented_event = {
        "role": PRESENTED_ROLE, "metric": METRIC, "valueYuan": str(presented_value),
        "currency": "CNY", "rawUnit": "yuan", "unit": presented_statement.unit,
        "statement": "利润表", "statementScope": "CONSOLIDATED",
        "announcementId": REPORT_BINDINGS[PRESENTED_ROLE]["announcementId"],
        "receiptId": REPORT_BINDINGS[PRESENTED_ROLE]["receiptId"],
        "contentHash": REPORT_BINDINGS[PRESENTED_ROLE]["contentHash"],
        "sourceReportPeriodEnd": PRESENTING_PERIOD.isoformat(),
        "factPeriodEnd": FACT_PERIOD.isoformat(), "columnRole": "PRIOR",
        "columnHeader": presented_statement.prior_header,
        "currentColumnHeader": presented_statement.current_header,
        "sourcePublishedDate": reports[PRESENTED_ROLE]["sourcePublishedDate"],
        "firstSeenAt": reports[PRESENTED_ROLE]["firstSeenAt"],
        "sourceRow": prior_row.raw_line, "rowLabel": prior_row.label,
        "page": prior_row.page, "versionId": presented_id, "supersedesId": original_id,
    }
    return {
        "reports": reports,
        "events": [original_event, presented_event],
        "sourceVerification": {
            "h124Current": {
                "candidate": _candidate_evidence(parsed_by_role[ORIGINAL_ROLE]["parsed"]),
                "machineVerification": parsed_by_role[ORIGINAL_ROLE]["verification"].as_evidence(),
                "statementHeader": statement_by_role[ORIGINAL_ROLE].current_header,
                "statementUnit": statement_by_role[ORIGINAL_ROLE].unit,
            },
            "h125Current": {
                "candidate": _candidate_evidence(parsed_by_role[PRESENTED_ROLE]["parsed"]),
                "machineVerification": parsed_by_role[PRESENTED_ROLE]["verification"].as_evidence(),
                "statementHeader": presented_statement.current_header,
                "statementUnit": presented_statement.unit,
            },
            "h125Prior": {
                "statementAmountYuan": str(presented_value),
                "independentGrid": cell,
                "crossChecks": checks,
            },
        },
    }


def _source_candidate(archive, closure: Mapping) -> tuple[dict, dict, list, dict]:
    source_replay = h125_inputs.RELATIONSHIP_SOURCE_REPLAY
    company, documents, entries = _classifier_inputs(archive)
    docs_by_id = {str(row["announcementId"]): row for row in documents}
    source = _read_source_pair(archive, docs_by_id)
    current_index = {
        role: {
            "announcementId": binding["announcementId"],
            "title": entries[binding["announcementId"]]["title"],
            "publishedDate": entries[binding["announcementId"]]["publishedDate"],
            "documentUrl": entries[binding["announcementId"]]["documentUrl"],
            "indexReceiptId": entries[binding["announcementId"]]["indexReceiptId"],
            "indexResponseHash": entries[binding["announcementId"]]["indexResponseHash"],
        }
        for role, binding in REPORT_BINDINGS.items()
    }
    candidate = {
        "schema": SOURCE_SCHEMA,
        "instrumentId": ISSUER,
        "factPeriodEnd": FACT_PERIOD.isoformat(),
        "sourceIndexThrough": source_replay["company"]["publicationWindow"][1],
        "sourceClosure": dict(closure),
        "currentIndex": current_index,
        "reports": source["reports"],
        "versionsByMetric": {METRIC: source["events"]},
        "sourceVerification": source["sourceVerification"],
        "alternateReports": {
            aid: {
                "contentHash": ANNEX_BINDINGS[aid]["contentHash"],
                "periodEnd": ANNEX_BINDINGS[aid]["periodEnd"],
                "columnRole": ANNEX_BINDINGS[aid]["columnRole"],
                "revenueYuan": ANNEX_BINDINGS[aid]["revenueYuan"],
            }
            for aid in ANNEX_BINDINGS
        },
    }
    return candidate, company, documents, entries


def _validate_impact(proof: Mapping, documents: list) -> Mapping:
    _need(isinstance(proof, Mapping), "S2 H124 impact proof is missing")
    body = dict(proof)
    declared_hash = body.pop("evidenceHash", None)
    _need(declared_hash == _digest(_canonical(body).encode("utf-8")),
          "S2 H124 impact evidence hash is not canonical")
    outcomes = [row for row in proof.get("outcomes", ()) if row.get("metric") == METRIC]
    _need(len(outcomes) == 1 and len(proof.get("outcomes", ())) == 1,
          "S2 classifier did not return the unique H124 revenue outcome")
    outcome = outcomes[0]
    scope = proof.get("relationshipScope", {})
    _need(outcome.get("periodEnd") == FACT_PERIOD.isoformat() and
          outcome.get("status") == "AFFECTS" and
          outcome.get("admissionStatus") == "NOT_ADMITTED" and
          outcome.get("impactProof") is None and
          outcome.get("missingReportSourceIds") == [] and
          outcome.get("unresolvedSuccessorIds") == [] and
          scope.get("completeForArchivedIndexResponse") is True,
          "ordinary S2 classifier no longer proves complete AFFECTS / NOT_ADMITTED")

    expected_documents = {str(doc["announcementId"]): doc for doc in documents}
    sources = proof.get("sources", ())
    source_ids = [str(row.get("announcementId")) for row in sources]
    _need(len(source_ids) == 23 and len(set(source_ids)) == 23 and
          set(source_ids) == set(expected_documents),
          "S2 proof does not bind the exact 23-document 000680 source closure")
    for row in sources:
        expected = expected_documents[str(row["announcementId"])]
        _need(row.get("originalHash") == expected["contentHash"],
              "S2 proof source hash differs for " + str(row["announcementId"]))
    for aid, binding in REPORT_BINDINGS.items():
        matches = [row for row in sources
                   if str(row.get("announcementId")) == binding["announcementId"]]
        _need(len(matches) == 1 and matches[0].get("originalHash") == binding["contentHash"],
              "canonical H124/H125 report is missing from S2 source proof: " + aid)
    for aid, binding in ANNEX_BINDINGS.items():
        matches = [row for row in sources if str(row.get("announcementId")) == aid]
        _need(len(matches) == 1 and matches[0].get("originalHash") == binding["contentHash"],
              "alternate financial report is missing from S2 source proof: " + aid)

    comparisons = {
        str(row.get("announcementId")): row
        for row in outcome.get("comparisons", ())
    }
    expected_comparisons = {
        "1220951752": ("CURRENT", "2024-06-30", "6508010963.75", False),
        "1224561048": ("PRIOR", "2025-06-30", "6798178192.94", True),
        "1224561047": ("PRIOR", "2025-06-30", "6798178192.94", True),
    }
    _need(set(expected_comparisons) <= set(comparisons),
          "S2 H124 source comparison omits a canonical or alternate report candidate")
    for aid, expected in expected_comparisons.items():
        row = comparisons[aid]
        _need((row.get("columnRole"), row.get("sourceReportPeriodEnd"),
               row.get("amountYuan"), row.get("changed")) == expected and
              row.get("factPeriodEnd") == FACT_PERIOD.isoformat(),
              "S2 H124 canonical/alternate comparison differs: " + aid)
    current = outcome.get("currentSource", {})
    _need(current.get("announcementId") == REPORT_BINDINGS[ORIGINAL_ROLE]["announcementId"] and
          current.get("amountYuan") == REPORT_BINDINGS[ORIGINAL_ROLE]["valueYuan"],
          "S2 H124 current source differs from the canonical report")
    return outcome


def _replay_all(archive, *, candidate: Mapping | None = None,
                saved_impact: Mapping | None = None) -> dict:
    _need(archive.con.execute("PRAGMA query_only").fetchone()[0] == 1,
          "source archive must be opened query-only")
    source_replay = deepcopy(h125_inputs.RELATIONSHIP_SOURCE_REPLAY)
    closure = h125._verify_relationship_source_replay(archive, source_replay)
    actual_candidate, company, documents, _ = _source_candidate(archive, closure)
    if candidate is not None:
        _need(_canonical(candidate) == _canonical(actual_candidate),
              "stored H124 source candidate differs from fresh native/index replay")
    inputs = dict(archive_root=archive.root, company=company, target_period_end=FACT_PERIOD,
                  target_metrics=(METRIC,), issuer_name=COMPANY, documents=documents)
    if saved_impact is not None:
        impact.replay_revision_impact(saved_impact, **inputs)
        effective_impact = dict(saved_impact)
    else:
        effective_impact = impact.assess_revision_impact(**inputs).as_dict()
    outcome = _validate_impact(effective_impact, documents)
    if archive.con.total_changes != 0:
        raise H124RevenueChainError("H124 source replay modified the read-only archive")
    return {
        "candidate": actual_candidate,
        "impactProof": effective_impact,
        "outcome": outcome,
        "relationshipSourceReplay": source_replay,
        "relationshipSourceClosure": closure,
        "observationTimes": _source_times(
            (actual_candidate, effective_impact, source_replay, closure),
        ),
    }


def _source_times(value: object) -> list[datetime]:
    times = []
    timestamp_keys = {
        "first_seen_at", "firstseenat", "requested_at", "requestedat",
        "responded_at", "respondedat", "completedat", "capturedat",
    }

    def visit(item):
        if isinstance(item, Mapping):
            for key, child in item.items():
                if str(key).lower() in timestamp_keys and isinstance(child, str):
                    stamp = datetime.fromisoformat(child.replace("Z", "+00:00"))
                    _need(stamp.tzinfo is not None and stamp.utcoffset() is not None,
                          "source observation timestamp lacks timezone")
                    times.append(stamp.astimezone(timezone.utc))
                else:
                    visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return times


def _target_key_present(repository: FinancialFactRepository) -> bool:
    with closing(connect(repository.db_path, read_only=True)) as con:
        con.execute("PRAGMA query_only=ON")
        _need(con.execute("PRAGMA query_only").fetchone()[0] == 1,
              "target-key lookup is not query-only")
        con.execute("BEGIN")
        before = con.total_changes
        rows = con.execute(
            "SELECT version_id FROM financial_fact WHERE instrument_id IN (?,?) "
            "AND metric=? AND period_end=? AND statement_scope=? AND profit_scope IS NULL "
            "AND currency=? AND raw_unit=?",
            (INSTRUMENT_ID, ISSUER, METRIC, FACT_PERIOD.isoformat(),
             "CONSOLIDATED", "CNY", "yuan"),
        ).fetchall()
        _need(con.total_changes == before, "target-key read changed the private fact clone")
        con.execute("ROLLBACK")
    return bool(rows)


def _calendar_times(calendar_evidence: Mapping) -> list[datetime]:
    return _source_times(calendar_evidence)


def _expected_available_at(
    *, completed_at: datetime, replay: Mapping, calendar: Mapping,
    trading_days: tuple[date, ...],
) -> datetime:
    report_floors, report_observations = [], []
    for role in (ORIGINAL_ROLE, PRESENTED_ROLE):
        report = replay["candidate"]["reports"][role]
        source_date = date.fromisoformat(report["sourcePublishedDate"])
        first_seen = datetime.fromisoformat(report["firstSeenAt"])
        _need(first_seen.tzinfo is not None and first_seen.utcoffset() is not None and
              first_seen <= completed_at,
              "canonical report first-seen clock is invalid")
        report_observations.append(first_seen.astimezone(timezone.utc))
        report_floors.append(date_only_available_at(source_date, trading_days)[0])
    observations = [
        completed_at,
        *report_observations,
        *_calendar_times(calendar),
        *replay["observationTimes"],
    ]
    _need(all(value <= completed_at for value in observations),
          "a source observation follows the H124 writer completion time")
    return _next_available_snapshot(max([*observations, *report_floors]), trading_days)


def _version_source(event: Mapping, report: Mapping, completed: datetime, available: datetime) -> dict:
    return {
        "role": event["role"], "versionId": event["versionId"],
        "supersedesId": event["supersedesId"], "columnHeader": event["columnHeader"],
        **({"currentColumnHeader": event["currentColumnHeader"]}
           if "currentColumnHeader" in event else {}),
        "sourceRow": event["sourceRow"], "rowLabel": event["rowLabel"],
        "page": event["page"], "announcementId": report["announcementId"],
        "receiptId": report["receiptId"], "statementScope": event["statementScope"],
        "contentHash": report["contentHash"], "valueYuan": event["valueYuan"],
        "sourceReportPeriodEnd": report["sourceReportPeriodEnd"],
        "factPeriodEnd": event["factPeriodEnd"], "columnRole": report["columnRole"],
        "sourcePublishedDate": report["sourcePublishedDate"],
        "firstSeenAt": report["firstSeenAt"], "ingestedAt": completed.isoformat(),
        "availableAt": available.isoformat(),
    }


def promote_000680_h124_revenue_version_chain(
    *, archive, repository: FinancialFactRepository,
    calendar_evidence: Mapping,
) -> tuple[FinancialFact, FinancialFact]:
    """Append exactly the canonical H124 CURRENT and H125 presented PRIOR."""
    rights = default_rights().get("cninfo")
    if not (rights.open_for("research_use") and rights.open_for("local_storage")):
        raise H124RevenueChainError("CNINFO research/local-storage rights are closed")
    packet = dict(calendar_evidence)
    started = _utc_now()
    replay = _replay_all(archive)
    completed = _utc_now()
    _need(completed >= started, "writer clock moved backwards")
    trading_days, verified_calendar = s2_calendar_evidence.verify_calendar_evidence(
        packet, archive, now=completed,
        published_on=date.fromisoformat(REPORT_BINDINGS[ORIGINAL_ROLE]["sourcePublishedDate"]),
    )
    _need(verified_calendar == packet, "verified source calendar differs from the submitted archived evidence")
    if _target_key_present(repository):
        raise H124RevenueChainError("H124 revenue already has stored versions; refusing a duplicate chain")
    available = _expected_available_at(
        completed_at=completed, replay=replay, calendar=verified_calendar,
        trading_days=trading_days,
    )
    facts, version_rows = [], []
    predecessor = None
    for role, event in zip(
            (ORIGINAL_ROLE, PRESENTED_ROLE),
            replay["candidate"]["versionsByMetric"][METRIC], strict=True):
        report = replay["candidate"]["reports"][role]
        amount = Decimal(event["valueYuan"])
        version_id = _version_id(role, predecessor)
        _need(event.get("role") == role and event.get("versionId") == version_id and
              event.get("supersedesId") == predecessor and
              event.get("factPeriodEnd") == FACT_PERIOD.isoformat() and
              event.get("valueYuan") == str(amount),
              "source event/version lineage differs before append")
        fact = FinancialFact(
            version_id=version_id, instrument_id=INSTRUMENT_ID, metric=METRIC,
            period_end=FACT_PERIOD, statement_scope=StatementScope.CONSOLIDATED,
            profit_scope=None, value=amount, currency="CNY", raw_unit="yuan",
            source_id="cninfo", source_document_id=report["announcementId"],
            source_published_date=date.fromisoformat(report["sourcePublishedDate"]),
            source_published_at=None, timestamp_precision=TimestampPrecision.DATE,
            first_seen_at=datetime.fromisoformat(report["firstSeenAt"]),
            ingested_at=completed, available_at=available,
            availability_basis=AvailabilityBasis.OBSERVED, pit_mode=PitMode.LIVE_OBSERVED,
            content_hash=report["contentHash"], supersedes_id=predecessor,
        )
        facts.append(fact)
        version_rows.append(_version_source(event, report, completed, available))
        predecessor = version_id

    ids = [fact.version_id for fact in facts]
    bundle_id = REVIEW_PREFIX + hashlib.sha256(
        f"{SCHEMA}|{INSTRUMENT_ID}|{FACT_PERIOD.isoformat()}|{'|'.join(sorted(ids))}".encode("utf-8")
    ).hexdigest()[:32]
    impact_outcome = replay["outcome"]
    evidence = {
        "schema": SCHEMA, "source_id": "cninfo", "instrument_id": INSTRUMENT_ID,
        "issuer_id": ISSUER, "issuer_name": COMPANY, "period_end": FACT_PERIOD.isoformat(),
        "announcement_id": REPORT_BINDINGS[PRESENTED_ROLE]["announcementId"],
        "rights_register_version": REGISTER_VERSION, "verifier_id": VERIFIER,
        "verifier_version": "1", "human_review_claimed": None,
        "formal_admission": "NOT_ADMITTED",
        "candidate_disposition": "PRESENTED_PRIOR_AFFECTS_H124_REVENUE_VERSION_CHAIN",
        "causal_attribution": None, "fact_version_ids": ids,
        "version_ids_by_metric": {METRIC: ids},
        "review_member_version_ids": ids,
        "version_sources_by_metric": {METRIC: version_rows},
        "source_candidate": replay["candidate"],
        "impact_proof": replay["impactProof"],
        "relationship_source_replay": replay["relationshipSourceReplay"],
        "relationship_source_closure": replay["relationshipSourceClosure"],
        "calendar_evidence": verified_calendar,
        "archive": {
            "root": str(Path(archive.root).resolve()),
            "database_path": str(Path(archive.root).resolve() / "meta.sqlite"),
        },
        "replay_receipt": {
            "started_at": started.isoformat(),
            "completed_at": completed.isoformat(),
            "available_at": available.isoformat(),
            "impact_outcome": {
                "periodEnd": impact_outcome.get("periodEnd"),
                "status": impact_outcome.get("status"),
                "admissionStatus": impact_outcome.get("admissionStatus"),
                "impactProof": impact_outcome.get("impactProof"),
                "comparisons": impact_outcome.get("comparisons"),
                "currentSource": impact_outcome.get("currentSource"),
            },
        },
    }
    repository.append_many(facts, review_bundle=FactReviewBundle(bundle_id, _canonical(evidence)))
    return facts[0], facts[1]


def _open_readonly_archive(database_path: str, root_path: str):
    database = Path(database_path).resolve(strict=True)
    root = Path(root_path).resolve(strict=True)
    _need(database == (root / "meta.sqlite").resolve(strict=True),
          "persisted archive database must be the meta.sqlite within its bound root")
    con = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True,
                          timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    _need(con.execute("PRAGMA query_only").fetchone()[0] == 1,
          "source archive connection is not query-only")
    con.execute("BEGIN")
    return h125.ReadOnlyArchive(con, root)


def accepts_000680_h124_revenue_version_chain(
    row: Mapping, member_ids: set[str], facts: Mapping[str, FinancialFact], *,
    accepted_links: Mapping[str, tuple[str, str]],
) -> bool:
    """Fail-closed public-reader replay for the fixed two-version H124 chain."""
    archive = None
    try:
        raw = row["evidence_json"]
        evidence = json.loads(raw)
        if not isinstance(evidence, dict) or _canonical(evidence) != raw:
            return False
        if (row["evidence_hash"] != _digest(raw.encode("utf-8")) or
                evidence.get("schema") != SCHEMA or evidence.get("source_id") != "cninfo" or
                evidence.get("instrument_id") != INSTRUMENT_ID or
                evidence.get("issuer_id") != ISSUER or evidence.get("issuer_name") != COMPANY or
                evidence.get("period_end") != FACT_PERIOD.isoformat() or
                evidence.get("announcement_id") != REPORT_BINDINGS[PRESENTED_ROLE]["announcementId"] or
                evidence.get("rights_register_version") != REGISTER_VERSION or
                evidence.get("verifier_id") != VERIFIER or
                evidence.get("formal_admission") != "NOT_ADMITTED" or
                evidence.get("candidate_disposition") !=
                "PRESENTED_PRIOR_AFFECTS_H124_REVENUE_VERSION_CHAIN" or
                evidence.get("causal_attribution") is not None or
                not row["bundle_id"].startswith(REVIEW_PREFIX)):
            return False
        ids_by_metric = evidence.get("version_ids_by_metric", {})
        source_by_metric = evidence.get("version_sources_by_metric", {})
        ids = ids_by_metric.get(METRIC, [])
        if (set(ids_by_metric) != {METRIC} or set(source_by_metric) != {METRIC} or
                len(ids) != 2 or len(set(ids)) != 2 or
                ids != evidence.get("fact_version_ids") or
                ids != evidence.get("review_member_version_ids") or
                member_ids != set(ids) or member_ids & accepted_links.keys() or
                set(ids) - facts.keys()):
            return False
        expected_bundle = REVIEW_PREFIX + hashlib.sha256(
            f"{SCHEMA}|{INSTRUMENT_ID}|{FACT_PERIOD.isoformat()}|{'|'.join(sorted(ids))}".encode("utf-8")
        ).hexdigest()[:32]
        if row["bundle_id"] != expected_bundle:
            return False
        archive_data = evidence["archive"]
        archive = _open_readonly_archive(
            archive_data["database_path"], archive_data["root"],
        )
        completed = datetime.fromisoformat(evidence["replay_receipt"]["completed_at"])
        started = datetime.fromisoformat(evidence["replay_receipt"]["started_at"])
        available = datetime.fromisoformat(evidence["replay_receipt"]["available_at"])
        if (completed.tzinfo is None or completed.utcoffset() is None or
                started.tzinfo is None or started.utcoffset() is None or
                available.tzinfo is None or available.utcoffset() is None or
                started > completed or completed > datetime.now(timezone.utc)):
            return False
        replay = _replay_all(
            archive, candidate=evidence["source_candidate"],
            saved_impact=evidence["impact_proof"],
        )
        if (evidence.get("relationship_source_replay") != replay["relationshipSourceReplay"] or
                evidence.get("relationship_source_closure") != replay["relationshipSourceClosure"]):
            return False
        source_replay = evidence["replay_receipt"]["impact_outcome"]
        outcome = replay["outcome"]
        expected_impact = {
            "periodEnd": outcome.get("periodEnd"),
            "status": outcome.get("status"),
            "admissionStatus": outcome.get("admissionStatus"),
            "impactProof": outcome.get("impactProof"),
            "comparisons": outcome.get("comparisons"),
            "currentSource": outcome.get("currentSource"),
        }
        if source_replay != expected_impact:
            return False
        calendar_packet = evidence["calendar_evidence"]
        trading_days, rebuilt_calendar = s2_calendar_evidence.verify_calendar_evidence(
            calendar_packet, archive, now=completed,
            published_on=date.fromisoformat(REPORT_BINDINGS[ORIGINAL_ROLE]["sourcePublishedDate"]),
        )
        if rebuilt_calendar != calendar_packet:
            return False
        expected_available = _expected_available_at(
            completed_at=completed, replay=replay, calendar=calendar_packet,
            trading_days=trading_days,
        )
        if (available <= completed or available != expected_available or
                available.isoformat() != evidence["replay_receipt"]["available_at"]):
            return False

        events = replay["candidate"]["versionsByMetric"][METRIC]
        predecessor = None
        version_sources = []
        for role, event, version_id in zip((ORIGINAL_ROLE, PRESENTED_ROLE), events, ids, strict=True):
            report = replay["candidate"]["reports"][role]
            version_sources.append(_version_source(event, report, completed, available))
            fact = facts[version_id]
            amount = Decimal(event["valueYuan"])
            expected_id = _version_id(role, predecessor)
            if (fact.version_id != expected_id or fact.supersedes_id != predecessor or
                    fact.instrument_id != INSTRUMENT_ID or fact.metric != METRIC or
                    fact.period_end != FACT_PERIOD or
                    fact.statement_scope is not StatementScope.CONSOLIDATED or
                    fact.profit_scope is not None or fact.value != amount or
                    fact.currency != "CNY" or fact.raw_unit != "yuan" or
                    fact.source_id != "cninfo" or
                    fact.source_document_id != report["announcementId"] or
                    fact.content_hash != report["contentHash"] or
                    fact.source_published_date != date.fromisoformat(report["sourcePublishedDate"]) or
                    fact.source_published_at is not None or
                    fact.timestamp_precision is not TimestampPrecision.DATE or
                    fact.first_seen_at != datetime.fromisoformat(report["firstSeenAt"]) or
                    fact.ingested_at != completed or fact.available_at != available or
                    fact.availability_basis is not AvailabilityBasis.OBSERVED or
                    fact.pit_mode is not PitMode.LIVE_OBSERVED):
                return False
            predecessor = expected_id
        if (_canonical(source_by_metric[METRIC]) != _canonical(version_sources) or
                archive.con.total_changes != 0):
            return False
        return True
    except (OSError, sqlite3.Error, KeyError, IndexError, AttributeError,
            TypeError, ValueError, ArithmeticError):
        return False
    finally:
        if archive is not None:
            try:
                archive.con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            archive.con.close()


__all__ = [
    "SCHEMA", "VERIFIER", "REVIEW_PREFIX", "H124RevenueChainError",
    "REPORT_BINDINGS", "ANNEX_BINDINGS",
    "promote_000680_h124_revenue_version_chain",
    "accepts_000680_h124_revenue_version_chain",
]
