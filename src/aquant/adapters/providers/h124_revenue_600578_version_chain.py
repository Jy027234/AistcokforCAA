"""Finite 600578 H124 revenue CURRENT -> H125 PRIOR presented-version chain.

This private profile replays only 600578 revenue for 2024-06-30.  The later
H125 prior-column value is recorded as a presented version of the same fact.
Revision impact remains AFFECTS / NOT_ADMITTED with null causal attribution.
"""
from __future__ import annotations

import copy
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
from urllib.parse import parse_qs

from ...artifact_paths import resolve_archive_member
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
    h125_four_field_version_chain_600578 as h125,
    s2_calendar_evidence,
    s2_machine_verification,
    s2_revision_impact as impact,
)
from .cninfo import parse_report_period, title_matches_period
from .pdf_promotion import _next_available_snapshot, _utc_now


SCHEMA = "aquant.cninfo.600578-h124-revenue-presented-prior-chain.v1"
VERIFIER = "machine:cninfo-600578-h124-revenue-presented-prior-chain-v1"
REVIEW_PREFIX = "cninfo_machine_600578_h124_revenue_chain_"
STOCK_CODE = "600578"
INSTRUMENT_ID = "600578"
ISSUER = "SH.600578"
COMPANY = "北京京能电力股份有限公司"
ORGANIZATION_ID = "gssh0600578"
FACT_PERIOD = date(2024, 6, 30)
PRESENTING_PERIOD = date(2025, 6, 30)
METRIC = "revenue"
REVISION_IDS = tuple(h125.REVISION_IDS)
ORIGINAL_ROLE = "ORIGINAL_CURRENT"
PRESENTED_ROLE = "PRESENTED_PRIOR"
REPORT_BINDINGS = {
    ORIGINAL_ROLE: {
        "announcementId": "1220983220",
        "receiptId": "rcpt_22f5a9ebb923009e65b69d9b",
        "contentHash": "sha256:1cfa0c66dffe4af50accdc2d188133f5d493fdfb54831e825d06c37f39668c4e",
        "documentUrl": "https://static.cninfo.com.cn/finalpage/2024-08-27/1220983220.PDF",
        "sourceReportPeriodEnd": "2024-06-30",
        "factPeriodEnd": "2024-06-30",
        "columnRole": "CURRENT",
    },
    PRESENTED_ROLE: {
        "announcementId": "1224562024",
        "receiptId": "rcpt_378f23a6ee80979231ce462d",
        "contentHash": "sha256:3364f3e7e9408d18b337834e011eaa5b9a0dd8301470ceca61710e4f9184101a",
        "documentUrl": "https://static.cninfo.com.cn/finalpage/2025-08-23/1224562024.PDF",
        "sourceReportPeriodEnd": "2025-06-30",
        "factPeriodEnd": "2024-06-30",
        "columnRole": "PRIOR",
    },
}
_EXPECTED_PUBLICATION_DATES = {
    ORIGINAL_ROLE: "2024-08-27",
    PRESENTED_ROLE: "2025-08-23",
}
_EXPECTED_PRIOR_SOURCE = {
    "currentHeader": "2025年半年度",
    "priorHeader": "2024年半年度",
    "unit": "元",
    "role": "合并",
    "statement": "利润表",
}


class H124RevenueChainError(ValueError):
    """The fixed H124 revenue source or presented-prior lineage failed replay."""


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _version_id(role: str, *, predecessor_id: str | None = None) -> str:
    binding = REPORT_BINDINGS.get(role)
    if binding is None:
        raise H124RevenueChainError("unsupported H124 revenue version role")
    if role == ORIGINAL_ROLE:
        if predecessor_id is not None:
            raise H124RevenueChainError("original H124 CURRENT cannot supersede another version")
        identity = (f"cninfo|{STOCK_CODE}|{FACT_PERIOD.isoformat()}|{METRIC}|"
                    f"{binding['announcementId']}|{binding['contentHash']}")
        prefix = "mach_"
    else:
        if not predecessor_id:
            raise H124RevenueChainError("H125 presented PRIOR must supersede H124 CURRENT")
        identity = (f"cninfo|{STOCK_CODE}|{PRESENTING_PERIOD.isoformat()}|{FACT_PERIOD.isoformat()}|"
                    f"PRIOR|{METRIC}|{binding['announcementId']}|{binding['contentHash']}")
        prefix = "mach_cmp_"
    return prefix + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _event_for(candidate: Mapping, role: str) -> Mapping:
    rows = candidate.get("versionsByMetric", {}).get(METRIC)
    if not isinstance(rows, list):
        raise H124RevenueChainError("the exact revenue source events are absent")
    matches = [row for row in rows if isinstance(row, Mapping) and row.get("role") == role]
    if len(matches) != 1:
        raise H124RevenueChainError("revenue source event role is absent or ambiguous")
    return matches[0]


def _report(candidate: Mapping, role: str) -> Mapping:
    report = candidate.get("reports", {}).get(role)
    expected = REPORT_BINDINGS.get(role)
    if not isinstance(report, Mapping) or expected is None or any(
            report.get(key) != value for key, value in expected.items()):
        raise H124RevenueChainError("fixed H124/H125 report binding differs: " + role)
    return report


def _verify_nov3_regular_indexes(archive, entries: Mapping, index_snapshot: Mapping) -> dict:
    """Verify the exact same-day H124/H125 and newest regular index set."""
    if index_snapshot.get("through") != "2026-11-03":
        raise H124RevenueChainError("Nov3 regular-index verifier received another declared day")
    dated_periods = [impact._report_period(entry.get("title", ""))
                     for entry in entries.values()]
    dated_periods = [period for period in dated_periods if period is not None]
    if not dated_periods:
        raise H124RevenueChainError("Nov3 broad index has no report period for latest-index binding")
    latest_period = max(dated_periods).isoformat()
    regular_bindings = (
        ("regular_h124", REPORT_BINDINGS[ORIGINAL_ROLE]["announcementId"], "2024-06-30"),
        ("regular_h125", REPORT_BINDINGS[PRESENTED_ROLE]["announcementId"], "2025-06-30"),
    )
    proof = {}
    for key, required_id, period_text in regular_bindings:
        source = index_snapshot.get(key)
        if not isinstance(source, Mapping):
            raise H124RevenueChainError("Nov3 regular index is absent: " + key)
        parsed = parse_report_period(period_text)
        expected_window = [
            parsed.publication_window[0].isoformat(),
            min(date.fromisoformat("2026-11-03"), parsed.publication_window[1]).isoformat(),
        ]
        if source.get("publication_window") != expected_window:
            raise H124RevenueChainError("Nov3 regular index does not use the parser-bounded request window: " + key)
        proof[key] = {"publicationWindow": expected_window,
                      **h125._verify_regular_index(archive, source, required_id)}

    latest = index_snapshot.get("regular_latest")
    if not isinstance(latest, Mapping) or latest.get("report_period") != latest_period:
        raise H124RevenueChainError("Nov3 latest regular index does not match the broad-index latest period")
    parsed_latest = parse_report_period(latest_period)
    expected_latest_window = [
        parsed_latest.publication_window[0].isoformat(),
        min(date.fromisoformat("2026-11-03"), parsed_latest.publication_window[1]).isoformat(),
    ]
    if latest.get("publication_window") != expected_latest_window:
        raise H124RevenueChainError("Nov3 latest regular index does not use the parser-bounded request window")
    latest_matches = [
        str(row.get("announcement", {}).get("announcement_id"))
        for row in latest.get("matches", ())
        if row.get("announcement", {}).get("report_period") == latest_period
    ]
    if len(latest_matches) != 1:
        raise H124RevenueChainError("Nov3 latest regular index has no unique latest full report")
    proof["regular_latest"] = {"publicationWindow": expected_latest_window,
        **h125._verify_regular_index(archive, latest, latest_matches[0])}

    for key, _, _ in (*regular_bindings, ("regular_latest", None, None)):
        source = index_snapshot[key]
        for page in source.get("pages", ()):
            rec = archive.con.execute(
                "SELECT first_seen_at FROM fetch_receipt WHERE receipt_id=?",
                (page.get("receipt_id"),),
            ).fetchone()
            if rec is None:
                raise H124RevenueChainError("Nov3 regular index page receipt is missing: " + key)
            first_seen = datetime.fromisoformat(str(rec["first_seen_at"]).replace("Z", "+00:00"))
            if (first_seen.tzinfo is None or
                    first_seen.astimezone(impact.CST).date().isoformat() != "2026-11-03"):
                raise H124RevenueChainError("Nov3 regular index receipt was not first-seen on Nov3 Beijing time: " + key)
    return {"observationDateBeijing": "2026-11-03", "latestPeriod": latest_period,
            "latestReportIds": latest_matches, "indexes": proof}


def _index_context(archive, impact_input: Mapping, index_snapshot: Mapping) -> tuple[dict, dict, dict, dict]:
    current_company, current_index = h125._verify_current_company(
        archive, impact_input, index_snapshot,
    )
    entries, _, _ = impact._index(archive.con, archive.root, current_company)
    source_entries = {}
    for role, binding in REPORT_BINDINGS.items():
        aid = binding["announcementId"]
        entry = entries.get(aid)
        if (not isinstance(entry, Mapping) or entry.get("documentUrl") != binding["documentUrl"] or
                entry.get("publishedDate") != _EXPECTED_PUBLICATION_DATES[role] or
                not title_matches_period(entry.get("title", ""),
                                         parse_report_period(binding["sourceReportPeriodEnd"]))):
            raise H124RevenueChainError("H124/H125 report is not bound to the complete current index: " + role)
        source_entries[role] = dict(entry)
    if index_snapshot.get("through") == "2026-11-03":
        regular_proof = _verify_nov3_regular_indexes(archive, entries, index_snapshot)
        current_index["nov3RegularIndexes"] = regular_proof
        regular = regular_proof["indexes"]["regular_h125"]
    else:
        regular = h125._verify_regular_index(
            archive, index_snapshot.get("regular_h125", {}),
            REPORT_BINDINGS[PRESENTED_ROLE]["announcementId"],
        )
    return current_company, current_index, source_entries, regular


def _extract_prior_revenue_grid_cell(body: bytes) -> dict:
    """Independently re-extract the H125 2024H1 comparative cell from PDF geometry."""
    import pdfplumber

    digest = _digest(body)
    if digest != REPORT_BINDINGS[PRESENTED_ROLE]["contentHash"] or not body.startswith(b"%PDF-"):
        raise H124RevenueChainError("H125 prior-grid input hash or PDF signature mismatch")
    found = []
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        if geometry._frontmatter(pdf, STOCK_CODE, PRESENTING_PERIOD) is None:
            raise H124RevenueChainError("H125 issuer and report period failed the grid identity check")
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
        for position, (start, top, bottom, heading, role, statement) in enumerate(located):
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
    if len(found) != 1:
        raise H124RevenueChainError("H125 H124H1 prior revenue grid cell is missing or ambiguous")
    item = found[0]
    if (item["currentHeader"] != _EXPECTED_PRIOR_SOURCE["currentHeader"] or
            item["priorHeader"] != _EXPECTED_PRIOR_SOURCE["priorHeader"] or
            item["unit"] != _EXPECTED_PRIOR_SOURCE["unit"] or
            item["role"] != _EXPECTED_PRIOR_SOURCE["role"] or
            item["statement"] != _EXPECTED_PRIOR_SOURCE["statement"]):
        raise H124RevenueChainError("H125 comparative grid header, unit, or scope differs")
    return {"cell": item, "sourceContentHash": digest}


def _read_source_pair(archive, source_entries: Mapping) -> dict:
    parsed_by_role = {}
    section_by_role = {}
    report_meta = {}
    for role, binding in REPORT_BINDINGS.items():
        index_entry = source_entries.get(role)
        if not isinstance(index_entry, Mapping):
            raise H124RevenueChainError("source report is missing from the complete issuer index")
        source_period = date.fromisoformat(binding["sourceReportPeriodEnd"])
        receipt, body = h125._receipt(
            archive, binding["receiptId"], binding["contentHash"], binding["documentUrl"],
        )
        if (receipt["receipt_id"] != binding["receiptId"] or
                receipt["url"] != binding["documentUrl"] or
                receipt["byte_size"] != len(body) or not body.startswith(b"%PDF-") or
                index_entry.get("publishedDate") != _EXPECTED_PUBLICATION_DATES[role]):
            raise H124RevenueChainError("archived source receipt/index publication binding differs: " + role)
        parsed = sections.extract_section_facts(
            body, instrument_id=STOCK_CODE, company=COMPANY, period_end=source_period,
            announcement_id=binding["announcementId"], document_url=binding["documentUrl"],
            required_fields=(METRIC,),
        )
        verified = s2_machine_verification.verify_machine_evidence(
            body, candidate=parsed, expected_fields=(METRIC,),
        )
        field_verifications = [field for field in verified.fields if field.field == METRIC]
        if (not verified.verified or len(field_verifications) != 1 or
                not field_verifications[0].verified or
                "revenue_note_decomposition" not in field_verifications[0].satisfied_paths or
                parsed.pdf_sha256 != binding["contentHash"] or METRIC not in parsed.by_field):
            raise H124RevenueChainError("native revenue verifier failed for " + role)
        statement_set = sections.extract_statement_sections(
            body, period_end=source_period, roles=(sections.ROLE_CONSOLIDATED,),
            statements=("利润表",),
        )
        statement = statement_set.require(sections.ROLE_CONSOLIDATED, "利润表")
        expected_current = "2024年半年度" if role == ORIGINAL_ROLE else "2025年半年度"
        if statement.unit != "元" or statement.current_header != expected_current:
            raise H124RevenueChainError("native profit-statement header or unit differs: " + role)
        native = parsed.by_field[METRIC]
        if (native.currency != "CNY" or native.amount_unit != "元" or
                native.period_end != source_period):
            raise H124RevenueChainError("native report currency/unit/period differs: " + role)
        statement_row = statement.find(METRIC)
        if statement_row is None or Decimal(str(native.value_yuan)) != (
                statement_row.amount * sections._UNIT_MULTIPLIER[statement.unit]):
            raise H124RevenueChainError("native parser and profit statement disagree: " + role)
        report_meta[role] = {
            **binding,
            "sourcePublishedDate": index_entry["publishedDate"],
            "firstSeenAt": receipt["first_seen_at"],
            "byteSize": len(body),
        }
        parsed_by_role[role] = {
            "receipt": receipt, "body": body, "parsed": parsed,
            "verification": verified, "fieldVerification": field_verifications[0],
        }
        section_by_role[role] = statement

    original_native = parsed_by_role[ORIGINAL_ROLE]["parsed"].by_field[METRIC]
    original_section = section_by_role[ORIGINAL_ROLE]
    presented_section = section_by_role[PRESENTED_ROLE]
    prior_row = presented_section.find(METRIC)
    if (prior_row is None or presented_section.prior_header != "2024年半年度" or
            prior_row.prior_amount is None):
        raise H124RevenueChainError("H125 2024H1 comparative revenue row is absent")
    grid = _extract_prior_revenue_grid_cell(parsed_by_role[PRESENTED_ROLE]["body"])
    original_value = Decimal(str(original_native.value_yuan))
    presented_value = prior_row.prior_amount * sections._UNIT_MULTIPLIER[presented_section.unit]
    grid_cell = grid["cell"]
    grid_label = geometry._compact(grid_cell["rowLabel"])
    native_label = geometry._compact(prior_row.label)
    note_reference = re.search(r"([一二三四五六七八九十]+[、.][0-9]+)$", native_label)
    if note_reference:
        native_label = native_label[:note_reference.start()]
    cell_checks = {
        "h125CurrentNativeMatchesStatement": (
            Decimal(str(prior_row.amount * sections._UNIT_MULTIPLIER[presented_section.unit])) ==
            Decimal(str(parsed_by_role[PRESENTED_ROLE]["parsed"].by_field[METRIC].value_yuan))),
        "independentPriorGridMatchesStatement": Decimal(grid_cell["amountYuan"]) == presented_value,
        "independentCurrentCellMatchesStatement": geometry._amount(grid_cell["currentCell"]) == prior_row.amount,
        "independentPriorCellMatchesStatement": geometry._amount(grid_cell["priorCell"]) == prior_row.prior_amount,
        "gridLabelMatchesRevenueRowWithoutNoteReference": grid_label == native_label,
    }
    if not all(cell_checks.values()):
        failed = [key for key, holds in cell_checks.items() if not holds]
        raise H124RevenueChainError(
            "H125 current/prior native and independent grid cells disagree: " + ",".join(failed)
        )
    if original_value == presented_value:
        raise H124RevenueChainError("H125 prior presentation does not change H124 revenue")

    original_id = _version_id(ORIGINAL_ROLE)
    presented_id = _version_id(PRESENTED_ROLE, predecessor_id=original_id)
    original_event = {
        "role": ORIGINAL_ROLE, "metric": METRIC, "valueYuan": str(original_value),
        "currency": "CNY", "rawUnit": "yuan", "unit": original_native.amount_unit,
        "statement": "利润表", "statementScope": "CONSOLIDATED",
        "announcementId": REPORT_BINDINGS[ORIGINAL_ROLE]["announcementId"],
        "receiptId": REPORT_BINDINGS[ORIGINAL_ROLE]["receiptId"],
        "contentHash": REPORT_BINDINGS[ORIGINAL_ROLE]["contentHash"],
        "sourceReportPeriodEnd": REPORT_BINDINGS[ORIGINAL_ROLE]["sourceReportPeriodEnd"],
        "factPeriodEnd": FACT_PERIOD.isoformat(), "columnRole": "CURRENT",
        "columnHeader": original_native.column_header,
        "sourcePublishedDate": report_meta[ORIGINAL_ROLE]["sourcePublishedDate"],
        "firstSeenAt": report_meta[ORIGINAL_ROLE]["firstSeenAt"],
        "page": original_native.pdf_page,
        "displayRowLabel": original_native.display_row_label,
        "sourceRow": original_native.source_row,
        "sourceCells": list(original_native.source_cells),
        "versionId": original_id, "supersedesId": None,
    }
    presented_event = {
        "role": PRESENTED_ROLE, "metric": METRIC, "valueYuan": str(presented_value),
        "currency": "CNY", "rawUnit": "yuan", "unit": presented_section.unit,
        "statement": "利润表", "statementScope": "CONSOLIDATED",
        "announcementId": REPORT_BINDINGS[PRESENTED_ROLE]["announcementId"],
        "receiptId": REPORT_BINDINGS[PRESENTED_ROLE]["receiptId"],
        "contentHash": REPORT_BINDINGS[PRESENTED_ROLE]["contentHash"],
        "sourceReportPeriodEnd": REPORT_BINDINGS[PRESENTED_ROLE]["sourceReportPeriodEnd"],
        "factPeriodEnd": FACT_PERIOD.isoformat(), "columnRole": "PRIOR",
        "currentColumnHeader": presented_section.current_header,
        "columnHeader": presented_section.prior_header,
        "sourcePublishedDate": report_meta[PRESENTED_ROLE]["sourcePublishedDate"],
        "firstSeenAt": report_meta[PRESENTED_ROLE]["firstSeenAt"],
        "page": prior_row.page, "displayRowLabel": prior_row.label,
        "sourceRow": prior_row.raw_line,
        "sourceCells": [prior_row.label, prior_row.current_cell, prior_row.prior_cell],
        "gridCell": grid_cell,
        "versionId": presented_id, "supersedesId": original_id,
    }
    return {
        "reports": report_meta,
        "events": [original_event, presented_event],
        "sourceVerification": {
            "nativeMachinePaths": {
                ORIGINAL_ROLE: list(parsed_by_role[ORIGINAL_ROLE]["fieldVerification"].satisfied_paths),
                PRESENTED_ROLE: list(parsed_by_role[PRESENTED_ROLE]["fieldVerification"].satisfied_paths),
            },
            "originalCurrent": {
                "announcementId": REPORT_BINDINGS[ORIGINAL_ROLE]["announcementId"],
                "periodEnd": FACT_PERIOD.isoformat(), "columnHeader": original_native.column_header,
                "unit": original_native.amount_unit, "page": original_native.pdf_page,
                "rowLabel": original_native.display_row_label, "sourceRow": original_native.source_row,
                "sourceCells": list(original_native.source_cells), "valueYuan": str(original_value),
            },
            "presentedPrior": {
                "announcementId": REPORT_BINDINGS[PRESENTED_ROLE]["announcementId"],
                "sourceReportPeriodEnd": PRESENTING_PERIOD.isoformat(),
                "factPeriodEnd": FACT_PERIOD.isoformat(), "columnRole": "PRIOR",
                "currentHeader": presented_section.current_header,
                "priorHeader": presented_section.prior_header, "unit": presented_section.unit,
                "page": prior_row.page, "rowLabel": prior_row.label,
                "sourceRow": prior_row.raw_line,
                "currentCell": prior_row.current_cell, "priorCell": prior_row.prior_cell,
                "valueYuan": str(presented_value),
                "gridCell": grid_cell,
            },
            "deltaYuan": str(presented_value - original_value),
            "presentingReportCurrentValueIsNotAChainMember": True,
        },
    }


def build_600578_h124_revenue_source_candidate(
    *, archive, impact_input: Mapping, index_snapshot: Mapping,
) -> dict:
    """Create the finite claim from archived source; the reader independently replays it."""
    _, current_index, source_entries, _ = _index_context(archive, impact_input, index_snapshot)
    extracted = _read_source_pair(archive, source_entries)
    return {
        "schema": "aquant.cninfo.600578-h124-revenue-source-candidate.v1",
        "instrumentId": INSTRUMENT_ID,
        "factPeriodEnd": FACT_PERIOD.isoformat(),
        "sourceIndexThrough": index_snapshot["through"],
        "sourceIndexSha256": _digest(_canonical(index_snapshot).encode("utf-8")),
        "currentIndex": current_index,
        "reports": extracted["reports"],
        "versionsByMetric": {METRIC: extracted["events"]},
        "sourceVerification": extracted["sourceVerification"],
    }


def _verify_h124_impact(
    archive, impact_input: Mapping, original_impact_input: Mapping,
    impact_input_provenance: Mapping, impact_proof: Mapping,
) -> dict:
    binding = h125._verify_impact_input_binding(
        archive, impact_input, original_impact_input, impact_input_provenance,
    )
    company = impact_input.get("sourceOnlyCompany")
    if (not isinstance(company, Mapping) or company.get("code") != STOCK_CODE or
            company.get("instrumentId") != ISSUER or company.get("name") != "京能电力" or
            company.get("publicationWindow") != ["2024-06-30", "2026-10-27"]):
        raise H124RevenueChainError("H124 impact source input is outside the fixed issuer/window")
    try:
        impact.replay_revision_impact(
            impact_proof, archive_root=archive.root, company=company,
            target_period_end=FACT_PERIOD, target_metrics=(METRIC,), issuer_name=COMPANY,
        )
    except (OSError, sqlite3.Error, KeyError, AttributeError, TypeError,
            ValueError, ArithmeticError) as exc:
        raise H124RevenueChainError("H124 revenue impact source replay failed: " + str(exc)) from exc
    outcomes = {row.get("metric"): row for row in impact_proof.get("outcomes", ())}
    scope = impact_proof.get("relationshipScope", {})
    if (set(outcomes) != {METRIC} or outcomes[METRIC].get("periodEnd") != FACT_PERIOD.isoformat() or
            outcomes[METRIC].get("status") != "AFFECTS" or
            outcomes[METRIC].get("admissionStatus") != "NOT_ADMITTED" or
            outcomes[METRIC].get("impactProof") is not None or
            scope.get("completeForArchivedIndexResponse") is not True or
            set(scope.get("allRecalledRevisionIds", ())) != set(REVISION_IDS)):
        raise H124RevenueChainError("H124 AFFECTS/NOT_ADMITTED source disposition changed")
    sources = impact_proof.get("sources", ())
    source_ids = [str(row.get("announcementId")) for row in sources]
    if (len(source_ids) != 16 or len(set(source_ids)) != 16 or
            any(not str(row.get("originalHash", "")).startswith("sha256:") for row in sources)):
        raise H124RevenueChainError("H124 impact proof does not bind all 16 official PDF source hashes")
    for role, binding_row in REPORT_BINDINGS.items():
        matches = [row for row in sources if str(row.get("announcementId")) == binding_row["announcementId"]]
        if len(matches) != 1 or matches[0].get("originalHash") != binding_row["contentHash"]:
            raise H124RevenueChainError("H124/H125 report is missing from the impact source proof: " + role)
    proof_body = dict(impact_proof)
    declared_hash = proof_body.pop("evidenceHash", None)
    if declared_hash != _digest(_canonical(proof_body).encode("utf-8")):
        raise H124RevenueChainError("H124 impact proof hash is not canonical")
    if archive.con.total_changes != 0:
        raise H124RevenueChainError("H124 impact replay changed the source archive")
    return {
        "outcome": outcomes[METRIC],
        "relationshipScope": scope,
        "sourceCount": len(source_ids),
        "sourceAnnouncementIds": source_ids,
        "evidenceHash": declared_hash,
        "impactInputBinding": binding,
    }


def _replay_all(
    archive, *, candidate: Mapping, impact_input: Mapping,
    original_impact_input: Mapping, impact_input_provenance: Mapping,
    impact_proof: Mapping, h125_impact_proof: Mapping,
    index_snapshot: Mapping, revision_source_audit: Mapping,
    completed_at: datetime,
) -> dict:
    if (candidate.get("schema") != "aquant.cninfo.600578-h124-revenue-source-candidate.v1" or
            candidate.get("instrumentId") != INSTRUMENT_ID or
            candidate.get("factPeriodEnd") != FACT_PERIOD.isoformat() or
            index_snapshot.get("through") != "2026-11-03" or
            candidate.get("sourceIndexThrough") != index_snapshot.get("through") or
            candidate.get("sourceIndexSha256") != _digest(_canonical(index_snapshot).encode("utf-8"))):
        raise H124RevenueChainError("H124 source candidate/index binding differs")
    _, current_index, source_entries, regular_h125 = _index_context(
        archive, impact_input, index_snapshot,
    )
    source_data = _read_source_pair(archive, source_entries)
    if (candidate.get("reports") != source_data["reports"] or
            candidate.get("versionsByMetric") != {METRIC: source_data["events"]} or
            candidate.get("sourceVerification") != source_data["sourceVerification"] or
            candidate.get("currentIndex") != current_index):
        raise H124RevenueChainError("source candidate does not exactly match fresh native PDF/index replay")
    h124_impact = _verify_h124_impact(
        archive, impact_input, original_impact_input, impact_input_provenance, impact_proof,
    )
    # Reuse the strict H125 audit, replaying its historical Nov2 receipts and
    # binding all 11 dispositions to the exact current Nov3 cross-category set.
    h125_outcomes = {row.get("metric"): row for row in h125_impact_proof.get("outcomes", ())}
    if (set(h125_outcomes) != set(h125.METRICS) or any(
            row.get("status") != "AFFECTS" or row.get("admissionStatus") != "NOT_ADMITTED" or
            row.get("impactProof") is not None for row in h125_outcomes.values())):
        raise H124RevenueChainError("preserved H125 four-field AFFECTS statuses are not exact")
    revision_dispositions = h125._verify_revision_source_audit(
        archive, revision_source_audit, index_snapshot, h125_impact_proof,
        impact_input=impact_input,
    )
    if (len(revision_dispositions) != 11 or
            {row["announcementId"] for row in revision_dispositions} != set(REVISION_IDS) or
            any(row.get("causalAttribution") is not None for row in revision_dispositions)):
        raise H124RevenueChainError("complete 11-candidate disposition audit differs")
    if not isinstance(completed_at, datetime) or completed_at.tzinfo is None or completed_at.utcoffset() is None:
        raise H124RevenueChainError("writer completion time must be timezone-aware")
    observation_times = h125._collect_source_times(
        (candidate, impact_input, impact_proof, h125_impact_proof,
         index_snapshot, revision_source_audit),
        completed=completed_at,
    )
    if archive.con.total_changes != 0:
        raise H124RevenueChainError("source replay changed the read-only archive")
    return {
        "h124Impact": h124_impact,
        "currentIndex": current_index,
        "regularH125": regular_h125,
        "revisionDispositions": revision_dispositions,
        "sourceVerification": source_data["sourceVerification"],
        "observationTimes": tuple(observation_times),
    }


def _target_key_present(repository: FinancialFactRepository) -> bool:
    with closing(connect(repository.db_path, read_only=True)) as con:
        con.execute("PRAGMA query_only=ON")
        if con.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise H124RevenueChainError("target-key lookup is not query-only")
        con.execute("BEGIN")
        before = con.total_changes
        rows = con.execute(
            "SELECT version_id FROM financial_fact WHERE instrument_id IN (?,?) "
            "AND metric=? AND period_end=? AND statement_scope=? AND profit_scope IS NULL "
            "AND currency=? AND raw_unit=?",
            (STOCK_CODE, ISSUER, METRIC, FACT_PERIOD.isoformat(), "CONSOLIDATED", "CNY", "yuan"),
        ).fetchall()
        if con.total_changes != before:
            raise H124RevenueChainError("target-key read changed the private fact clone")
        con.execute("ROLLBACK")
    return bool(rows)


def promote_600578_h124_revenue_version_chain(
    *, candidate: Mapping, impact_input: Mapping, original_impact_input: Mapping,
    impact_input_provenance: Mapping, impact_proof: Mapping,
    h125_impact_proof: Mapping, index_snapshot: Mapping,
    revision_source_audit: Mapping, calendar_packet: Mapping,
    archive, repository: FinancialFactRepository,
) -> tuple[FinancialFact, ...]:
    """Append the two source versions to a private clone using append_many."""
    rights = default_rights().get("cninfo")
    if not (rights.open_for("research_use") and rights.open_for("local_storage")):
        raise H124RevenueChainError("CNINFO research/local-storage rights are closed")
    started = _utc_now()
    replay = _replay_all(
        archive, candidate=candidate, impact_input=impact_input,
        original_impact_input=original_impact_input,
        impact_input_provenance=impact_input_provenance,
        impact_proof=impact_proof, h125_impact_proof=h125_impact_proof,
        index_snapshot=index_snapshot, revision_source_audit=revision_source_audit,
        completed_at=started,
    )
    completed = _utc_now()
    if any(value > completed for value in replay["observationTimes"]):
        raise H124RevenueChainError("an archived source observation follows the actual append clock")
    trading_days, rechecked_calendar = s2_calendar_evidence.verify_calendar_evidence(
        calendar_packet, archive, now=completed,
    )
    if rechecked_calendar != calendar_packet:
        raise H124RevenueChainError("calendar source evidence changed during private writer replay")
    if _target_key_present(repository):
        raise H124RevenueChainError("H124 revenue target already has a root; writer will not duplicate it")

    report_times = []
    date_floors = []
    for role in (ORIGINAL_ROLE, PRESENTED_ROLE):
        report = candidate["reports"][role]
        source_date = date.fromisoformat(report["sourcePublishedDate"])
        first_seen = datetime.fromisoformat(report["firstSeenAt"])
        if (source_date.isoformat() != report["sourcePublishedDate"] or first_seen.tzinfo is None or
                first_seen.utcoffset() is None or first_seen > completed):
            raise H124RevenueChainError("report publication/first-seen clock is invalid")
        report_times.append(first_seen)
        date_floors.append(date_only_available_at(source_date, trading_days)[0])
    calendar_times = []

    def visit_calendar(value):
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key == "first_seen_at" and isinstance(child, str):
                    stamp = datetime.fromisoformat(child)
                    if stamp.tzinfo is None or stamp.utcoffset() is None:
                        raise H124RevenueChainError("calendar receipt clock lacks timezone")
                    calendar_times.append(stamp)
                else:
                    visit_calendar(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit_calendar(child)

    visit_calendar(rechecked_calendar)
    floors = [completed, *date_floors, *report_times, *calendar_times,
              *replay["observationTimes"]]
    available = _next_available_snapshot(max(floors), trading_days)
    events = candidate["versionsByMetric"][METRIC]
    facts = []
    predecessor = None
    fact_rows = []
    for role, event in zip((ORIGINAL_ROLE, PRESENTED_ROLE), events, strict=True):
        report = candidate["reports"][role]
        amount = Decimal(event["valueYuan"])
        version_id = _version_id(role, predecessor_id=predecessor)
        if (event.get("role") != role or event.get("versionId") != version_id or
                event.get("supersedesId") != predecessor or event.get("metric") != METRIC or
                event.get("factPeriodEnd") != FACT_PERIOD.isoformat() or
                event.get("valueYuan") != str(amount)):
            raise H124RevenueChainError("source event/version lineage differs before append")
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
        fact_rows.append({
            "role": role, "versionId": version_id, "supersedesId": predecessor,
            "columnHeader": event["columnHeader"],
            **({"currentColumnHeader": event["currentColumnHeader"]}
               if "currentColumnHeader" in event else {}),
            "sourceRow": event["sourceRow"],
            "announcementId": report["announcementId"], "receiptId": report["receiptId"],
            "statementScope": event["statementScope"],
            "contentHash": report["contentHash"], "valueYuan": str(amount),
            "sourceReportPeriodEnd": report["sourceReportPeriodEnd"],
            "factPeriodEnd": FACT_PERIOD.isoformat(), "columnRole": report["columnRole"],
            "sourcePublishedDate": report["sourcePublishedDate"],
            "firstSeenAt": report["firstSeenAt"],
            "ingestedAt": completed.isoformat(), "availableAt": available.isoformat(),
        })
        predecessor = version_id

    ids = [fact.version_id for fact in facts]
    bundle_identity = f"{SCHEMA}|{INSTRUMENT_ID}|{FACT_PERIOD.isoformat()}|{'|'.join(sorted(ids))}"
    bundle_id = REVIEW_PREFIX + hashlib.sha256(bundle_identity.encode("utf-8")).hexdigest()[:32]
    impact_outcome = replay["h124Impact"]["outcome"]
    evidence = {
        "schema": SCHEMA, "source_id": "cninfo", "instrument_id": INSTRUMENT_ID,
        "issuer_id": ISSUER, "issuer_name": COMPANY, "period_end": FACT_PERIOD.isoformat(),
        "announcement_id": REPORT_BINDINGS[PRESENTED_ROLE]["announcementId"],
        "rights_register_version": REGISTER_VERSION, "verifier_id": VERIFIER,
        "verifier_version": "1", "human_review_claimed": None,
        "formal_admission": "NORMAL_READER_REPLAY_REQUIRED",
        "candidate_disposition": "PRESENTED_PRIOR_CHANGED_VALUE_AFFECTS_VERSION_CHAIN",
        "causal_attribution": None, "fact_version_ids": ids,
        "version_ids_by_metric": {METRIC: ids},
        "review_member_version_ids": ids,
        "version_sources_by_metric": {METRIC: fact_rows},
        "source_candidate": candidate,
        "impact_input": impact_input,
        "impact_input_original": original_impact_input,
        "impact_input_provenance": impact_input_provenance,
        "impact_proof": impact_proof,
        "h125_four_field_impact_proof": h125_impact_proof,
        "current_index_snapshot": index_snapshot,
        "revision_source_audit": revision_source_audit,
        "calendar_evidence": rechecked_calendar,
        "archive": {
            "root": str(Path(archive.root).resolve()),
            "database_path": str(Path(archive.root).resolve() / "meta.sqlite"),
        },
        "replay_receipt": {
            "started_at": started.isoformat(),
            "completed_at": completed.isoformat(),
            "available_at": available.isoformat(),
            "source_replay": {
                "impactOutcome": {
                    "periodEnd": impact_outcome.get("periodEnd"),
                    "status": impact_outcome.get("status"),
                    "admissionStatus": impact_outcome.get("admissionStatus"),
                    "impactProof": impact_outcome.get("impactProof"),
                },
                "impactInputBinding": replay["h124Impact"]["impactInputBinding"],
                "impactSourceCount": replay["h124Impact"]["sourceCount"],
                "impactEvidenceHash": replay["h124Impact"]["evidenceHash"],
                "currentIndex": replay["currentIndex"],
                "regularH125": replay["regularH125"],
                "revisionDispositions": replay["revisionDispositions"],
                "sourceVerification": replay["sourceVerification"],
                "writerStartedAt": started.isoformat(),
                "calendarCheckedAt": completed.isoformat(),
            },
        },
    }
    repository.append_many(facts, review_bundle=FactReviewBundle(bundle_id, _canonical(evidence)))
    return tuple(facts)


def accepts_600578_h124_revenue_version_chain(
    row: Mapping, member_ids: set[str], facts: Mapping[str, FinancialFact], *,
    accepted_links: Mapping[str, tuple[str, str]],
) -> bool:
    """Targeted reader replay for only the fixed two-version H124 revenue cohort."""
    archive = None
    try:
        raw = row["evidence_json"]
        evidence = json.loads(raw)
        if not isinstance(evidence, dict) or _canonical(evidence) != raw:
            return False
        if (row["evidence_hash"] != _digest(raw.encode("utf-8")) or
                evidence.get("schema") != SCHEMA or evidence.get("source_id") != "cninfo" or
                evidence.get("instrument_id") != INSTRUMENT_ID or evidence.get("issuer_id") != ISSUER or
                evidence.get("issuer_name") != COMPANY or evidence.get("period_end") != FACT_PERIOD.isoformat() or
                evidence.get("announcement_id") != REPORT_BINDINGS[PRESENTED_ROLE]["announcementId"] or
                evidence.get("rights_register_version") != REGISTER_VERSION or
                evidence.get("verifier_id") != VERIFIER or evidence.get("human_review_claimed") is not None or
                evidence.get("formal_admission") != "NORMAL_READER_REPLAY_REQUIRED" or
                evidence.get("candidate_disposition") != "PRESENTED_PRIOR_CHANGED_VALUE_AFFECTS_VERSION_CHAIN" or
                evidence.get("causal_attribution") is not None or
                not row["bundle_id"].startswith(REVIEW_PREFIX)):
            return False
        ids_by_metric = evidence["version_ids_by_metric"]
        source_by_metric = evidence["version_sources_by_metric"]
        chain_ids = ids_by_metric.get(METRIC, [])
        if (set(ids_by_metric) != {METRIC} or set(source_by_metric) != {METRIC} or
                len(chain_ids) != 2 or len(set(chain_ids)) != 2 or
                chain_ids != evidence.get("fact_version_ids") or
                member_ids != set(evidence.get("review_member_version_ids", ())) or
                member_ids != set(chain_ids) or member_ids & accepted_links.keys() or
                set(chain_ids) - facts.keys()):
            return False
        expected_bundle = REVIEW_PREFIX + hashlib.sha256(
            f"{SCHEMA}|{INSTRUMENT_ID}|{FACT_PERIOD.isoformat()}|{'|'.join(sorted(chain_ids))}".encode("utf-8")
        ).hexdigest()[:32]
        if row["bundle_id"] != expected_bundle:
            return False
        archive_data = evidence["archive"]
        archive = h125._read_only_archive(archive_data["database_path"], archive_data["root"])
        completed = datetime.fromisoformat(evidence["replay_receipt"]["completed_at"])
        if (completed.tzinfo is None or completed.utcoffset() is None or
                completed > datetime.now(timezone.utc)):
            return False
        replay = _replay_all(
            archive, candidate=evidence["source_candidate"],
            impact_input=evidence["impact_input"],
            original_impact_input=evidence["impact_input_original"],
            impact_input_provenance=evidence["impact_input_provenance"],
            impact_proof=evidence["impact_proof"],
            h125_impact_proof=evidence["h125_four_field_impact_proof"],
            index_snapshot=evidence["current_index_snapshot"],
            revision_source_audit=evidence["revision_source_audit"],
            completed_at=completed,
        )
        if archive.con.total_changes != 0:
            return False
        source_replay = evidence["replay_receipt"]["source_replay"]
        impact_outcome = replay["h124Impact"]["outcome"]
        expected_replay = {
            "impactOutcome": {
                "periodEnd": impact_outcome.get("periodEnd"),
                "status": impact_outcome.get("status"),
                "admissionStatus": impact_outcome.get("admissionStatus"),
                "impactProof": impact_outcome.get("impactProof"),
            },
            "impactInputBinding": replay["h124Impact"]["impactInputBinding"],
            "impactSourceCount": replay["h124Impact"]["sourceCount"],
            "impactEvidenceHash": replay["h124Impact"]["evidenceHash"],
            "currentIndex": replay["currentIndex"],
            "regularH125": replay["regularH125"],
            "revisionDispositions": replay["revisionDispositions"],
            "sourceVerification": replay["sourceVerification"],
            "writerStartedAt": evidence["replay_receipt"]["started_at"],
            "calendarCheckedAt": completed.isoformat(),
        }
        if source_replay != expected_replay:
            return False
        completed_time = datetime.fromisoformat(evidence["replay_receipt"]["completed_at"])
        available = datetime.fromisoformat(evidence["replay_receipt"]["available_at"])
        trading_days, calendar_replayed = s2_calendar_evidence.verify_calendar_evidence(
            evidence["calendar_evidence"], archive, now=completed_time,
        )
        if calendar_replayed != evidence["calendar_evidence"]:
            return False
        source_times = h125._collect_source_times(
            (evidence["source_candidate"], evidence["impact_input"], evidence["impact_proof"],
             evidence["h125_four_field_impact_proof"], evidence["current_index_snapshot"],
             evidence["revision_source_audit"]),
            completed=completed_time,
        )
        report_times = []
        date_floors = []
        for role in (ORIGINAL_ROLE, PRESENTED_ROLE):
            report = _report(evidence["source_candidate"], role)
            report_times.append(datetime.fromisoformat(report["firstSeenAt"]))
            date_floors.append(date_only_available_at(
                date.fromisoformat(report["sourcePublishedDate"]), trading_days,
            )[0])
        calendar_times = []

        def visit_calendar(value):
            if isinstance(value, Mapping):
                for key, child in value.items():
                    if key == "first_seen_at" and isinstance(child, str):
                        calendar_times.append(datetime.fromisoformat(child))
                    else:
                        visit_calendar(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    visit_calendar(child)

        visit_calendar(calendar_replayed)
        expected_available = _next_available_snapshot(
            max([completed_time, *date_floors, *report_times, *calendar_times, *source_times]),
            trading_days,
        )
        if (available.tzinfo is None or available.utcoffset() is None or
                available <= completed_time or expected_available != available or
                available.isoformat() != evidence["replay_receipt"]["available_at"]):
            return False
        events = source_by_metric[METRIC]
        if len(events) != 2 or [event.get("role") for event in events] != [ORIGINAL_ROLE, PRESENTED_ROLE]:
            return False
        predecessor = None
        for role, event in zip((ORIGINAL_ROLE, PRESENTED_ROLE), events, strict=True):
            report = _report(evidence["source_candidate"], role)
            fact = facts.get(event.get("versionId"))
            amount = Decimal(event["valueYuan"])
            expected_id = _version_id(role, predecessor_id=predecessor)
            if (fact is None or event.get("versionId") != expected_id or
                    event.get("supersedesId") != predecessor or fact.supersedes_id != predecessor or
                    fact.version_id != expected_id or fact.instrument_id != INSTRUMENT_ID or
                    fact.metric != METRIC or fact.period_end != FACT_PERIOD or
                    fact.statement_scope is not StatementScope.CONSOLIDATED or fact.profit_scope is not None or
                    fact.value != amount or fact.currency != "CNY" or fact.raw_unit != "yuan" or
                    fact.source_id != "cninfo" or fact.source_document_id != report["announcementId"] or
                    fact.content_hash != report["contentHash"] or
                    fact.source_published_date != date.fromisoformat(report["sourcePublishedDate"]) or
                    fact.source_published_at is not None or fact.timestamp_precision is not TimestampPrecision.DATE or
                    fact.first_seen_at != datetime.fromisoformat(report["firstSeenAt"]) or
                    fact.ingested_at != completed_time or fact.available_at != available or
                    fact.availability_basis is not AvailabilityBasis.OBSERVED or
                    fact.pit_mode is not PitMode.LIVE_OBSERVED or
                    event.get("announcementId") != report["announcementId"] or
                    event.get("receiptId") != report["receiptId"] or
                    event.get("contentHash") != report["contentHash"] or
                    event.get("sourceReportPeriodEnd") != report["sourceReportPeriodEnd"] or
                    event.get("factPeriodEnd") != FACT_PERIOD.isoformat() or
                    event.get("columnRole") != report["columnRole"] or
                    event.get("valueYuan") != str(amount) or
                    event.get("sourcePublishedDate") != report["sourcePublishedDate"] or
                    event.get("firstSeenAt") != report["firstSeenAt"] or
                    event.get("statementScope") != "CONSOLIDATED"):
                return False
            if role == ORIGINAL_ROLE and (event.get("columnHeader") != "2024年半年度" or
                    event.get("sourceRow") != evidence["source_candidate"]["sourceVerification"]["originalCurrent"]["sourceRow"]):
                return False
            if role == PRESENTED_ROLE and (event.get("columnHeader") != "2024年半年度" or
                    event.get("currentColumnHeader") != "2025年半年度" or
                    Decimal(replay["sourceVerification"]["presentedPrior"]["valueYuan"]) != amount):
                return False
            predecessor = expected_id
        return archive_data["root"] == str(Path(archive_data["root"]).resolve())
    except (OSError, sqlite3.Error, KeyError, AttributeError, TypeError,
            ValueError, ArithmeticError):
        return False
    finally:
        if archive is not None:
            archive.close()


__all__ = [
    "SCHEMA", "VERIFIER", "REVIEW_PREFIX", "H124RevenueChainError",
    "build_600578_h124_revenue_source_candidate",
    "promote_600578_h124_revenue_version_chain",
    "accepts_600578_h124_revenue_version_chain",
]
