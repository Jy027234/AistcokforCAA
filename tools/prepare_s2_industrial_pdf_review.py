#!/usr/bin/env python3
"""Prepare an unreviewed, local S2 review worklist for industrial CNINFO PDFs.

The source worksheet and output stay under ignored ``data/s2-pdf-review``.
Every selected PDF is checked against its archive receipt and re-extracted;
neither this tool nor its output creates FinancialFact or PIT eligibility.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.parse
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.cninfo_financial_candidate import (  # noqa: E402
    INDUSTRIAL_PILOT_COMPANIES,
    REQUIRED_PILOT_FIELDS_BY_PERIOD,
    extract_cninfo_candidate_facts,
)
from aquant.adapters.providers.cninfo_probe import normalize_title  # noqa: E402
from aquant.domain.data.db import connect  # noqa: E402


ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"
INPUT_PATH = ROOT / "data" / "s2-pdf-review" / "cninfo-industrial-candidates.json"
OUTPUT_ROOT = ROOT / "data" / "s2-pdf-review"
_DIGEST = re.compile(r"sha256:([0-9a-f]{64})\Z")
_REQUEST = re.compile(r"request=(sha256:[0-9a-f]{64})")


def _verified_blob(archive_root: Path, digest: str) -> bytes:
    match = _DIGEST.fullmatch(digest)
    if match is None:
        raise ValueError("invalid archived SHA-256")
    hexdigest = match.group(1)
    payload = (archive_root / "raw" / hexdigest[:2] / hexdigest).read_bytes()
    if hashlib.sha256(payload).hexdigest() != hexdigest:
        raise ValueError(f"archived bytes differ from {digest}")
    return payload


def _verified_receipt(con, archive_root: Path, receipt_id: str, digest: str,
                      *, expected_url: str | None = None,
                      expected_size: int | None = None,
                      expected_seen: str | None = None) -> tuple[bytes, dict]:
    receipt = con.execute(
        "SELECT source_id,url,outcome,http_status,content_hash,byte_size,"
        "first_seen_at,detail FROM fetch_receipt WHERE receipt_id=?",
        (receipt_id,),
    ).fetchone()
    if (receipt is None or receipt["source_id"] != "cninfo" or
            receipt["outcome"] != "OK" or receipt["http_status"] != 200 or
            receipt["content_hash"] != digest or
            (expected_url is not None and receipt["url"] != expected_url) or
            (expected_size is not None and receipt["byte_size"] != expected_size) or
            (expected_seen is not None and receipt["first_seen_at"] != expected_seen)):
        raise ValueError(f"archive receipt differs from worksheet: {receipt_id}")
    payload = _verified_blob(archive_root, digest)
    if len(payload) != receipt["byte_size"]:
        raise ValueError(f"archive byte size differs from receipt: {receipt_id}")
    return payload, dict(receipt)


def _index_scope(row: dict, con, archive_root: Path) -> dict:
    scope = row.get("versionCountScope")
    count = row.get("versionCount")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ValueError("version count is missing or invalid")
    if scope == "linked_period_candidates":
        if row.get("indexReceiptId") or row.get("indexPublicationWindow"):
            raise ValueError("linked candidate scope has contradictory index evidence")
        return {
            "versionCount": count,
            "versionCountScope": scope,
            "regularIndexEvidenceStatus": "LINKED_PERIOD_CANDIDATES_ONLY",
            "publicationWindow": None,
            "completeWithinQueryScope": None,
            "indexReceiptId": None,
            "indexContentHash": None,
        }
    if scope != "regular_index_query_window":
        raise ValueError("unknown version count scope")
    if not row.get("indexReceiptId") or not row.get("indexContentHash"):
        raise ValueError("regular index scope requires archived page evidence")
    _, receipt = _verified_receipt(
        con, archive_root, row["indexReceiptId"], row["indexContentHash"],
    )
    request = _REQUEST.search(receipt["detail"] or "")
    if request is None:
        raise ValueError("regular index request body is not archived")
    body = urllib.parse.parse_qs(
        _verified_blob(archive_root, request.group(1)).decode("utf-8"),
    )
    windows = body.get("seDate", [])
    if len(windows) != 1 or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}~\d{4}-\d{2}-\d{2}", windows[0]):
        raise ValueError("regular index query window is ambiguous")
    window = windows[0].split("~")
    if row.get("indexPublicationWindow") != window:
        raise ValueError("regular index query window differs from worksheet")
    announced = date.fromisoformat(row["announcedOn"])
    if not (date.fromisoformat(window[0]) <= announced <= date.fromisoformat(window[1])):
        raise ValueError("announcement date falls outside archived index query")
    complete = row.get("indexCompleteWithinQueryScope")
    if complete not in (True, False, None):
        raise ValueError("invalid regular index completeness claim")
    return {
        "versionCount": count,
        "versionCountScope": scope,
        "regularIndexEvidenceStatus": "ARCHIVED_PAGE_AND_QUERY_VERIFIED",
        "publicationWindow": window,
        "completeWithinQueryScope": complete,
        "indexPageNum": row.get("indexPageNum"),
        "indexReceiptId": row["indexReceiptId"],
        "indexContentHash": row["indexContentHash"],
    }


def _verified_disclosure_pages(index: dict, *, stock: str, organization: str,
                               category: str, con, archive_root: Path) -> tuple[list[str], dict]:
    pages = index.get("pages")
    if not pages or not isinstance(pages, list):
        raise ValueError("complete disclosure index has no pages")
    windows = set()
    announcements = {}
    raw_seen = 0
    for page_num, page in enumerate(pages, start=1):
        if page.get("page") != page_num:
            raise ValueError("disclosure index pages are not consecutive")
        request = urllib.parse.parse_qs(
            _verified_blob(archive_root, page["requestBodyHash"]).decode("utf-8"),
            keep_blank_values=True,
        )
        if (request.get("stock") != [f"{stock},{organization}"] or
                request.get("pageNum") != [str(page_num)] or
                request.get("category") != [category] or
                len(request.get("seDate", [])) != 1):
            raise ValueError("disclosure index request identity or scope differs")
        window = request["seDate"][0]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}~\d{4}-\d{2}-\d{2}", window):
            raise ValueError("disclosure index query window is invalid")
        windows.add(window)
        payload, receipt = _verified_receipt(
            con, archive_root, page["responseReceiptId"], page["responseContentHash"],
        )
        if _REQUEST.search(receipt["detail"] or "") is None or \
                _REQUEST.search(receipt["detail"] or "").group(1) != page["requestBodyHash"]:
            raise ValueError("disclosure response receipt does not identify request")
        response = json.loads(payload)
        rows = response.get("announcements")
        if (not isinstance(rows, list) or len(rows) != page.get("rawAnnouncementCount") or
                response.get("totalAnnouncement") != page.get("totalAnnouncement") or
                response.get("hasMore") != page.get("hasMore") or
                (page_num == len(pages)) == bool(page.get("hasMore"))):
            raise ValueError("disclosure pagination differs from archived response")
        raw_seen += len(rows)
        for item in rows:
            if item.get("secCode") != stock:
                continue
            announcement_id = str(item.get("announcementId") or "")
            if not announcement_id or announcement_id in announcements:
                raise ValueError("disclosure announcement identity is missing or duplicated")
            announcements[announcement_id] = {
                "title": normalize_title(item.get("announcementTitle")),
                "documentUrl": ("https://static.cninfo.com.cn/" +
                                str(item.get("adjunctUrl") or "").lstrip("/")),
                "page": page_num,
                "pageReceiptId": page["responseReceiptId"],
            }
    if (len(windows) != 1 or raw_seen != pages[-1]["totalAnnouncement"] or
            pages[-1]["hasMore"] is not False):
        raise ValueError("disclosure index is not complete within one query window")
    if "totalAnnouncement" in index and index["totalAnnouncement"] != raw_seen:
        raise ValueError("disclosure index total differs from archived pages")
    return next(iter(windows)).split("~"), announcements


def _cross_category_review(index: dict, report: dict, con,
                           archive_root: Path) -> dict:
    stock, period = report["instrumentId"], report["periodEnd"]
    if (index.get("schema") != "cninfo-s2-disclosure-index-v1" or
            index.get("sourceId") != "cninfo" or
            index.get("instrumentId") != stock or
            index.get("reportPeriod") != period or
            index.get("status") != "TITLE_CANDIDATES_ONLY_NOT_PIT_ELIGIBLE"):
        raise ValueError("cross-category index identity or status differs")
    regular, all_category = index["regularIndex"], index["allCategoryIndex"]
    organization = regular.get("organizationId")
    if (regular.get("complete") is not True or
            all_category.get("complete") is not True or
            not organization or organization != all_category.get("organizationId")):
        raise ValueError("cross-category and regular indexes must both be complete")
    regular_window, regular_rows = _verified_disclosure_pages(
        regular, stock=stock, organization=organization,
        category="category_bndbg_szsh", con=con, archive_root=archive_root,
    )
    all_window, all_rows = _verified_disclosure_pages(
        all_category, stock=stock, organization=organization,
        category="", con=con, archive_root=archive_root,
    )
    observed = index["observedThroughDate"]
    if (date.fromisoformat(observed) < date.fromisoformat(period) or
            date.fromisoformat(all_window[1]) != date.fromisoformat(observed)):
        raise ValueError("cross-category observation date differs from query window")
    selected = report["announcementId"]
    if (selected not in regular_rows or selected not in all_rows or
            all_rows[selected]["documentUrl"] != report["documentUrl"]):
        raise ValueError("selected PDF is absent from complete disclosure indexes")
    for listing, rows in ((regular.get("matchedReportAnnouncements", []), regular_rows),
                          (all_category.get("candidateAnnouncements", []), all_rows)):
        for item in listing:
            observed_item = rows.get(item["announcementId"])
            if (observed_item is None or any(
                    item.get(key) != observed_item[key]
                    for key in ("title", "page", "pageReceiptId"))
            ) or ("documentUrl" in item and
                  item["documentUrl"] != observed_item["documentUrl"]):
                raise ValueError("disclosure title candidate differs from archived response")
    return {
        "observedThroughBeijingDate": observed,
        "regularPublicationWindow": regular_window,
        "allCategoryPublicationWindow": all_window,
        "regularIndexPageReceipts": [item["responseReceiptId"] for item in regular["pages"]],
        "allCategoryPageReceipts": [item["responseReceiptId"] for item in all_category["pages"]],
        "indexCompleteWithinRecordedQueryScope": True,
        "mustRefreshIfHumanReviewOccursOnLaterBeijingDate": True,
        "totalAnnouncementsInAllCategoryWindow": all_category["totalAnnouncement"],
        "candidateAnnouncements": [
            {**item, "reviewStatus": "PENDING_HUMAN_DISPOSITION"}
            for item in all_category["candidateAnnouncements"]
        ],
    }


def build_review_worklist(worksheet: dict, con, archive_root: Path,
                          *, stocks: tuple[str, ...] | None = None,
                          periods: tuple[str, ...] | None = None,
                          cross_indexes: tuple[dict, ...] = ()) -> dict:
    if (worksheet.get("schema") != "cninfo-industrial-s2-candidates-v1" or
            worksheet.get("sourceId") != "cninfo" or
            worksheet.get("status") != "UNREVIEWED_NOT_PIT_ELIGIBLE" or
            worksheet.get("pitEligible") is not False or
            worksheet.get("formalFactCount") != 0):
        raise ValueError("input is not an unreviewed CNINFO candidate worksheet")
    selected_stocks = tuple(stocks or INDUSTRIAL_PILOT_COMPANIES)
    selected_periods = tuple(periods or REQUIRED_PILOT_FIELDS_BY_PERIOD)
    if (len(set(selected_stocks)) != len(selected_stocks) or
            len(set(selected_periods)) != len(selected_periods) or
            not set(selected_stocks) <= set(INDUSTRIAL_PILOT_COMPANIES) or
            not set(selected_periods) <= set(REQUIRED_PILOT_FIELDS_BY_PERIOD)):
        raise ValueError("invalid selected stock or report period")
    requested = {(stock, period) for stock in selected_stocks
                 for period in selected_periods}
    source_rows = {}
    for row in worksheet["reports"]:
        key = (row["instrumentId"], row["periodEnd"])
        if key in source_rows:
            raise ValueError(f"duplicate candidate report {key}")
        source_rows[key] = row
    if not requested <= source_rows.keys():
        raise ValueError(f"missing candidate reports: {sorted(requested - source_rows.keys())}")
    cross_by_report = {}
    for index in cross_indexes:
        key = (index["instrumentId"], index["reportPeriod"])
        if key in cross_by_report or key not in requested:
            raise ValueError("duplicate or out-of-scope cross-category index")
        cross_by_report[key] = index

    reports = []
    for stock, period in sorted(requested):
        row = source_rows[(stock, period)]
        if (row.get("company") != INDUSTRIAL_PILOT_COMPANIES[stock] or
                row.get("status") != "UNREVIEWED_PDF_CANDIDATE_NOT_PIT_ELIGIBLE" or
                row.get("pitEligible") is not False):
            raise ValueError(f"candidate report is ineligible for review worklist: {stock} {period}")
        required = REQUIRED_PILOT_FIELDS_BY_PERIOD[period]
        stored_fields = {item["field"]: item for item in row["fields"]}
        if len(stored_fields) != len(row["fields"]) or not set(required) <= set(stored_fields):
            raise ValueError(f"candidate field coverage differs from S2 formula: {stock} {period}")
        payload, receipt = _verified_receipt(
            con, archive_root, row["archiveReceiptId"], row["contentHash"],
            expected_url=row["documentUrl"], expected_size=row["byteSize"],
            expected_seen=row["firstSeenAt"],
        )
        extracted = extract_cninfo_candidate_facts(
            payload, instrument_id=stock, company=row["company"],
            period_end=date.fromisoformat(period),
            announcement_id=row["announcementId"],
            document_url=row["documentUrl"], required_fields=required,
        )
        if (extracted.pit_eligible or extracted.pdf_sha256 != row["contentHash"] or
                set(extracted.by_field) != set(required)):
            raise ValueError(f"PDF candidate identity or coverage changed: {stock} {period}")
        fields = []
        for name in required:
            item = extracted.by_field[name]
            stored = stored_fields[name]
            expected = {
                "valueYuan": str(item.value_yuan),
                "pdfPage": item.pdf_page,
                "columnHeader": item.column_header,
                "amountUnit": item.amount_unit,
                "sourceCurrentCell": item.source_cells[item.source_current_cell_index],
                "sourcePriorCell": item.source_cells[item.source_prior_cell_index],
                "displayRowLabel": item.display_row_label,
                "sourceRow": item.source_row,
                "reviewStatus": "PENDING_HUMAN_VISUAL",
            }
            if any(stored.get(key) != value for key, value in expected.items()):
                raise ValueError(f"stored extraction differs from archived PDF: {stock} {period} {name}")
            fields.append({
                "field": name,
                "extractedValueYuan": expected["valueYuan"],
                "pdfPage": item.pdf_page,
                "statement": item.statement,
                "columnHeader": item.column_header,
                "sourceAmountUnit": item.amount_unit,
                "currency": item.currency,
                "sourceCurrentCell": expected["sourceCurrentCell"],
                "sourcePriorCell": expected["sourcePriorCell"],
                "visibleRowLabel": item.display_row_label,
                "extractedSourceRow": item.source_row,
                "reviewStatus": "PENDING_HUMAN_VISUAL",
            })
        cross = ( _cross_category_review(cross_by_report[(stock, period)], row, con, archive_root)
                  if (stock, period) in cross_by_report else None )
        reports.append({
            "instrumentId": stock,
            "company": row["company"],
            "periodEnd": period,
            "announcementId": row["announcementId"],
            "announcementDate": row["announcedOn"],
            "announcementTitle": row["title"],
            "documentUrl": row["documentUrl"],
            "pdfReceiptId": row["archiveReceiptId"],
            "pdfSha256": row["contentHash"],
            "firstSeenAt": receipt["first_seen_at"],
            "announcementDiscoveryStatus": row["linkStatus"],
            "announcementDispositionStatus": "PENDING_HUMAN_DISPOSITION",
            "regularIndex": _index_scope(row, con, archive_root),
            "crossCategoryIndexEvidenceStatus": (
                "ARCHIVED_QUERY_SCOPE_VERIFIED" if cross else "NOT_SUPPLIED_FOR_REPORT_PERIOD"
            ),
            "crossCategoryIndex": cross,
            "crossCategoryReviewStatus": (
                "PENDING_HUMAN_DISPOSITIONS" if cross else
                "PENDING_INDEX_AND_HUMAN_DISPOSITIONS"
            ),
            "versionChainStatus": "NOT_ESTABLISHED",
            "fields": fields,
        })
    return {
        "schema": "cninfo-industrial-s2-pdf-review-worklist-v1",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "sourceId": "cninfo",
        "status": "UNREVIEWED_NOT_PIT_ELIGIBLE",
        "pitEligible": False,
        "formalFactCount": 0,
        "notice": ("Extracted cells are prompts for independent PDF visual and announcement "
                   "version review. Index counts cover only their recorded scopes. This worklist "
                   "is not an attestation or a formal point-in-time fact source."),
        "summary": {"reports": len(reports),
                    "fields": sum(len(report["fields"]) for report in reports),
                    "crossCategoryIndexesSupplied": sum(
                        report["crossCategoryIndex"] is not None for report in reports
                    )},
        "reports": reports,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, default=INPUT_PATH)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    parser.add_argument("--output", type=Path,
                        help="local JSON path; defaults to a new timestamped data/s2-pdf-review file")
    parser.add_argument("--stock", action="append", choices=tuple(INDUSTRIAL_PILOT_COMPANIES))
    parser.add_argument("--period", action="append", choices=tuple(REQUIRED_PILOT_FIELDS_BY_PERIOD))
    parser.add_argument("--cross-index", type=Path, action="append",
                        help="complete regular/all-category index for one selected report period; repeat")
    args = parser.parse_args()
    worksheet = json.loads(args.candidates.read_text(encoding="utf-8"))
    cross_indexes = tuple(json.loads(path.read_text(encoding="utf-8"))
                          for path in args.cross_index or [])
    with closing(connect(args.archive_root / "meta.sqlite", read_only=True)) as con:
        worklist = build_review_worklist(
            worksheet, con, args.archive_root,
            stocks=tuple(args.stock) if args.stock else None,
            periods=tuple(args.period) if args.period else None,
            cross_indexes=cross_indexes,
        )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = (args.output or OUTPUT_ROOT / f"cninfo-industrial-review-{stamp}.json").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(worklist, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"{output}: {worklist['summary']} (all reviews pending)")


if __name__ == "__main__":
    main()
