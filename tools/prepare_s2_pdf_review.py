"""Prepare a local, unreviewed CNINFO S2 PDF worksheet from archived bytes.

The worksheet contains extracted financial values, so it stays under ignored
``data/``.  It is evidence for a human reviewer to inspect, not a review
attestation and never writes FinancialFact records.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.cninfo_financial_pdf import (  # noqa: E402
    extract_s2_candidate_facts,
)
from aquant.domain.data.db import connect  # noqa: E402

ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"
OUTPUT_ROOT = ROOT / "data" / "s2-pdf-review"


def _archived_pdf(con, archive_root: Path, report: dict) -> tuple[bytes, str]:
    receipt = con.execute(
        "SELECT source_id, url, outcome, http_status, content_hash, byte_size, "
        "first_seen_at FROM fetch_receipt WHERE receipt_id = ?",
        (report["archiveReceiptId"],),
    ).fetchone()
    if receipt is None:
        raise ValueError(f"missing PDF receipt {report['archiveReceiptId']}")
    digest = report["contentHash"]
    if (receipt["source_id"] != "cninfo" or receipt["outcome"] != "OK" or
            receipt["http_status"] != 200 or receipt["url"] != report["documentUrl"] or
            receipt["content_hash"] != digest or
            receipt["first_seen_at"] != report["firstSeenAt"] or
            not digest.startswith("sha256:")):
        raise ValueError(f"PDF receipt differs from pilot {report['archiveReceiptId']}")
    hexdigest = digest.removeprefix("sha256:")
    if len(hexdigest) != 64 or any(char not in "0123456789abcdef" for char in hexdigest):
        raise ValueError("invalid SHA-256 in pilot")
    payload = (archive_root / "raw" / hexdigest[:2] / hexdigest).read_bytes()
    if (len(payload) != receipt["byte_size"] or
            hashlib.sha256(payload).hexdigest() != hexdigest):
        raise ValueError(f"archived PDF bytes fail verification {report['archiveReceiptId']}")
    return payload, receipt["first_seen_at"]


def build_worklist(pilot: dict, con, archive_root: Path,
                   *, report_period: date | None = None,
                   cross_pilot: dict | None = None) -> dict:
    instrument_id = pilot["instrumentId"]
    if pilot["sourceId"] != "cninfo" or not instrument_id.isdigit():
        raise ValueError("pilot must identify one CNINFO security")
    if cross_pilot is not None:
        index = (cross_pilot["allCategoryIndex"] if
                 cross_pilot.get("schema") == "cninfo-s2-disclosure-index-v1" else
                 cross_pilot)
        complete = index.get("complete", index.get("completeWithinQueryScope"))
        if (cross_pilot["instrumentId"] != instrument_id or not complete or
                cross_pilot.get("sourceId") != "cninfo"):
            raise ValueError("cross-category index identity or completeness mismatch")
    reports = []
    for report in pilot["reports"]:
        period = date.fromisoformat(report["periodEnd"])
        if report_period is not None and period != report_period:
            continue
        if not report["candidateOnly"]:
            raise ValueError("worksheet accepts candidate-only reports")
        payload, first_seen = _archived_pdf(con, archive_root, report)
        candidate = extract_s2_candidate_facts(
            payload, instrument_id=instrument_id, period_end=period,
        )
        if (candidate.pdf_sha256 != report["contentHash"] or
                candidate.document_url != report["documentUrl"] or
                len(candidate.candidates) != 5):
            raise ValueError(f"PDF extraction differs from pilot {period}")
        fields = []
        for item in sorted(candidate.candidates, key=lambda value: value.field):
            if item.pdf_page != report["candidateFields"][item.field]:
                raise ValueError(f"pilot page differs from PDF for {item.field}")
            fields.append({
                "field": item.field,
                "extractedValueYuan": str(item.value_yuan),
                "pdfPage": item.pdf_page,
                "visibleRowLabel": item.display_row_label or item.source_cells[0],
                "extractedFirstCell": item.source_cells[0],
                "sourceCurrentCell": item.source_cells[item.source_current_cell_index],
                "sourceAmountUnit": item.amount_unit,
                "statement": item.statement,
                "columnHeader": item.column_header,
                "extractedSourceRow": item.source_row,
                "reviewStatus": "PENDING_HUMAN_VISUAL",
            })
        cross_summary = None
        if cross_pilot is not None and cross_pilot["reportPeriod"] == period.isoformat():
            index = (cross_pilot["allCategoryIndex"] if
                     cross_pilot.get("schema") == "cninfo-s2-disclosure-index-v1" else
                     cross_pilot)
            cross_summary = {
                "observedThroughDate": cross_pilot["observedThroughDate"],
                "indexClaimsCompleteWithinQueryScope": True,
                "mustRefreshIfHumanReviewOccursOnLaterBeijingDate": True,
                "candidateAnnouncements": [
                    {**item, "reviewStatus": "PENDING_HUMAN_DISPOSITION"}
                    for item in index["candidateAnnouncements"]
                ],
            }
        reports.append({
            "periodEnd": period.isoformat(),
            "announcementId": report["announcementId"],
            "documentUrl": candidate.document_url,
            "pdfReceiptId": report["archiveReceiptId"],
            "pdfSha256": candidate.pdf_sha256,
            "firstSeenAt": first_seen,
            "fields": fields,
            "regularIndexArchived": report.get("index", {}).get("complete") is True,
            "regularIndexReviewStatus": "PENDING_HUMAN_REVIEW",
            "crossCategoryPilot": cross_summary,
            "crossCategoryReviewStatus": (
                "PENDING_HUMAN_DISPOSITIONS" if cross_summary else
                "PENDING_INDEX_AND_HUMAN_DISPOSITIONS"
            ),
        })
    if not reports:
        raise ValueError("no matching candidate reports in pilot")
    return {
        "schema": "cninfo-s2-pdf-worklist-v1",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "instrumentId": instrument_id,
        "status": "UNREVIEWED_NOT_PIT_ELIGIBLE",
        "notice": "Extracted cells are prompts for independent human visual and version review; this file is not an attestation.",
        "reports": reports,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", type=Path, required=True)
    parser.add_argument("--period", type=date.fromisoformat)
    parser.add_argument("--cross-pilot", type=Path)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    args = parser.parse_args()
    pilot = json.loads(args.pilot.read_text(encoding="utf-8"))
    cross_pilot = (json.loads(args.cross_pilot.read_text(encoding="utf-8"))
                   if args.cross_pilot else None)
    archive_root = args.archive_root.resolve()
    with closing(connect(archive_root / "meta.sqlite", read_only=True)) as con:
        worklist = build_worklist(
            pilot, con, archive_root, report_period=args.period,
            cross_pilot=cross_pilot,
        )
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    scope = args.period.isoformat() if args.period else "five-periods"
    out = OUTPUT_ROOT / f"{worklist['instrumentId']}-{scope}-{stamp}.json"
    with out.open("x", encoding="utf-8") as stream:
        json.dump(worklist, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"{out} ({len(worklist['reports'])} reports, "
          f"{sum(len(report['fields']) for report in worklist['reports'])} extracted cells; "
          "all reviews pending)")


if __name__ == "__main__":
    main()
