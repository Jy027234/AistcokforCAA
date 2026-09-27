#!/usr/bin/env python3
"""Archive and extract candidate-only S2 values for five industrial samples.

This local worksheet is deliberately not a review attestation or a source of
FinancialFact. It uses the existing CNINFO announcement-link artifact, rechecks
archived receipts and PDF bytes, and leaves unsupported layouts as errors.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.cninfo import CninfoClient  # noqa: E402
from aquant.adapters.providers.cninfo_financial_candidate import (  # noqa: E402
    INDUSTRIAL_PILOT_COMPANIES,
    REQUIRED_PILOT_FIELDS_BY_PERIOD,
    extract_cninfo_candidate_facts,
)
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402

ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"
LINKS_PATH = ROOT / "deploy" / "agentctl-q0" / "s2-disclosure-links-12x8.json"
OUTPUT_PATH = ROOT / "data" / "s2-pdf-review" / "cninfo-industrial-candidates.json"
COMPANIES = INDUSTRIAL_PILOT_COMPANIES
DEFAULT_PERIODS = ("2024-06-30", "2024-12-31", "2025-06-30",
                   "2025-12-31", "2026-06-30")
REQUIRED_FIELDS = REQUIRED_PILOT_FIELDS_BY_PERIOD


def _indexed_report(sample: dict, period: str) -> dict:
    rows = [item for item in sample["periods"] if item["period"] == period]
    if len(rows) != 1:
        raise ValueError("announcement-link artifact period is missing or duplicated")
    link = rows[0]["link"]
    active = link.get("activeReportAnnouncementId")
    versions = [item for item in link.get("versions", [])
                if item["announcementId"] == active]
    if (link.get("status") != "LINKED_VALUES_UNVERIFIED" or
            link.get("pitEligible") is not False or not active or
            len(versions) != 1):
        raise ValueError("announcement identity or link status is ambiguous")
    selected = versions[0]
    if not selected["documentUrl"].endswith(f"/{active}.PDF"):
        raise ValueError("announcement URL does not contain its identity")
    return {
        "announcementId": active,
        "announcedOn": selected["announcedOn"],
        "documentUrl": selected["documentUrl"],
        "title": selected["title"],
        "linkStatus": link["status"],
        "versionCount": len(link["versions"]),
        "versionCountScope": "linked_period_candidates",
        "versionReviewStatus": "PENDING_HUMAN_DISPOSITION",
    }


def _discover_report(client: CninfoClient, stock: str, period: str) -> dict:
    """Discover a missing report period without guessing among revisions."""
    # The first H1 report is normally published by August. A bounded
    # discovery window avoids an expensive two-year historical list query;
    # later corrections still require a separate all-category review.
    through = date(2024, 9, 30) if period == "2024-06-30" else None
    index = client.report_index(stock_code=stock, report_period=period,
                                max_pages=20, through=through)
    if not index.complete:
        raise ValueError(f"official report index incomplete: {index.termination}: {index.error}")
    year = date.fromisoformat(period).year
    marker = f"{year}年半年度报告" if period.endswith("06-30") else f"{year}年年度报告"
    matches = [item for item in index.matches if marker in item.announcement.title
               and "摘要" not in item.announcement.title
               and "英文" not in item.announcement.title
               and item.announcement.url]
    if len(matches) != 1:
        raise ValueError(f"official full-report versions ambiguous: {len(matches)}")
    match = matches[0]
    announcement = match.announcement
    return {
        "announcementId": announcement.announcement_id,
        "announcedOn": announcement.announcement_date_cn,
        "documentUrl": announcement.url,
        "title": announcement.title,
        "linkStatus": "INDEXED_PDF_VALUES_UNVERIFIED",
        "versionCount": len(matches),
        "versionCountScope": "regular_index_query_window",
        "versionReviewStatus": "PENDING_CROSS_CATEGORY_AND_HUMAN_DISPOSITION",
        "indexPublicationWindow": [day.isoformat() for day in index.publication_window],
        "indexCompleteWithinQueryScope": True,
        "indexPageNum": match.page_num,
        "indexReceiptId": match.page_receipt_id,
        "indexContentHash": match.page_content_hash,
    }


def _reused_index(archive: ForwardArchive, prior: dict) -> dict | None:
    receipt_id = prior.get("indexReceiptId")
    digest = prior.get("indexContentHash")
    if not receipt_id or not digest or not isinstance(digest, str):
        return None
    receipt = archive.con.execute(
        "SELECT source_id,outcome,http_status,content_hash,detail FROM fetch_receipt "
        "WHERE receipt_id=?", (receipt_id,),
    ).fetchone()
    if (receipt is None or receipt["source_id"] != "cninfo" or
            receipt["outcome"] != "OK" or receipt["http_status"] != 200 or
            receipt["content_hash"] != digest or not archive.verify(digest)):
        raise ValueError("saved report index evidence differs from archive")
    request = re.search(r"request=(sha256:[0-9a-f]{64})", receipt["detail"] or "")
    if request is None or not archive.verify(request.group(1)):
        raise ValueError("saved report index request body is unverified")
    body = urllib.parse.parse_qs(archive.load_bytes(request.group(1)).decode("utf-8"))
    window = body.get("seDate", [])
    if len(window) != 1 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}~\d{4}-\d{2}-\d{2}", window[0]):
        raise ValueError("saved report index query window is ambiguous")
    result = {key: prior[key] for key in (
        "announcementId", "announcedOn", "documentUrl", "title",
        "linkStatus", "versionCount", "versionReviewStatus", "indexPageNum",
        "indexReceiptId", "indexContentHash",
    )}
    result["versionCountScope"] = "regular_index_query_window"
    result["indexPublicationWindow"] = window[0].split("~")
    result["indexCompleteWithinQueryScope"] = prior.get("indexCompleteWithinQueryScope")
    return result


def _verified_archive(archive: ForwardArchive, url: str) -> tuple[bytes, dict] | None:
    receipt = archive.con.execute(
        "SELECT receipt_id,source_id,url,outcome,http_status,content_hash,byte_size,first_seen_at "
        "FROM fetch_receipt WHERE source_id='cninfo' AND url=? AND outcome='OK' "
        "ORDER BY first_seen_at DESC LIMIT 1", (url,),
    ).fetchone()
    if receipt is None:
        return None
    if (receipt["source_id"] != "cninfo" or receipt["url"] != url or
            receipt["http_status"] != 200 or
            not isinstance(receipt["content_hash"], str) or
            not receipt["content_hash"].startswith("sha256:") or
            not archive.verify(receipt["content_hash"])):
        raise ValueError("existing CNINFO PDF receipt or content is invalid")
    payload = archive.load_bytes(receipt["content_hash"])
    if len(payload) != receipt["byte_size"]:
        raise ValueError("existing CNINFO PDF receipt size differs from bytes")
    return payload, dict(receipt)


def _obtain_pdf(archive: ForwardArchive, client: CninfoClient,
                url: str) -> tuple[bytes, dict]:
    existing = _verified_archive(archive, url)
    if existing is not None:
        return existing
    fetched = client.document(url, label="s2-industrial-candidate")
    if not fetched.ok or fetched.payload is None or fetched.content_hash is None:
        raise ValueError(f"CNINFO fetch failed: {fetched.detail or 'unknown error'}")
    verified = _verified_archive(archive, url)
    if verified is None or verified[1]["receipt_id"] != fetched.receipt_id:
        raise ValueError("new PDF was not durably archived")
    return verified


def collect(*, links_path: Path, archive_root: Path,
            stocks: tuple[str, ...], periods: tuple[str, ...],
            previous: dict | None = None) -> dict:
    links = json.loads(links_path.read_text(encoding="utf-8"))
    samples = {sample["symbol"]: sample for sample in links["samples"]}
    if previous is not None and (previous.get("schema") != "cninfo-industrial-s2-candidates-v1" or
                                 previous.get("pitEligible") is not False):
        raise ValueError("resume file is not an unreviewed CNINFO candidate worksheet")
    prior_rows = {(row["instrumentId"], row["periodEnd"]): row
                  for row in (previous or {}).get("reports", [])}
    output = []
    with closing(connect(archive_root / "meta.sqlite")) as con:
        archive = ForwardArchive(con, archive_root)
        client = CninfoClient(archive)
        for stock in stocks:
            if stock not in COMPANIES or stock not in samples:
                raise ValueError(f"unsupported batch stock: {stock}")
            for period in periods:
                prior = prior_rows.get((stock, period))
                row = {
                    "instrumentId": stock, "company": COMPANIES[stock],
                    "periodEnd": period,
                    "status": "EXTRACTION_FAILED_NOT_PIT_ELIGIBLE",
                    "pitEligible": False,
                }
                try:
                    indexed = (_indexed_report(samples[stock], period)
                               if any(item["period"] == period
                                      for item in samples[stock]["periods"])
                               else (_reused_index(archive, prior) if prior else None)
                               or _discover_report(client, stock, period))
                    row.update(indexed)
                    if (prior is not None and
                            prior.get("status") == "UNREVIEWED_PDF_CANDIDATE_NOT_PIT_ELIGIBLE" and
                            prior.get("pitEligible") is False and
                            prior.get("documentUrl") == indexed["documentUrl"] and
                            set(REQUIRED_FIELDS[period]) <= {item["field"] for item in prior["fields"]}):
                        archived = _verified_archive(archive, indexed["documentUrl"])
                        if (archived is not None and
                                archived[1]["content_hash"] == prior.get("contentHash") and
                                archived[1]["receipt_id"] == prior.get("archiveReceiptId")):
                            output.append({**prior, **indexed})
                            print(f"{stock} {period}: reused verified candidate", flush=True)
                            continue
                    pdf_bytes, receipt = _obtain_pdf(archive, client, indexed["documentUrl"])
                    row.update({
                        "archiveReceiptId": receipt["receipt_id"],
                        "contentHash": receipt["content_hash"],
                        "byteSize": receipt["byte_size"],
                        "firstSeenAt": receipt["first_seen_at"],
                    })
                    candidate = extract_cninfo_candidate_facts(
                        pdf_bytes, instrument_id=stock, company=COMPANIES[stock],
                        period_end=date.fromisoformat(period),
                        announcement_id=indexed["announcementId"],
                        document_url=indexed["documentUrl"],
                        required_fields=REQUIRED_FIELDS[period],
                    )
                    if (candidate.pit_eligible or
                            set(candidate.by_field) != set(REQUIRED_FIELDS[period]) or
                            candidate.pdf_sha256 != receipt["content_hash"]):
                        raise ValueError("candidate status or hash differs from archive")
                    row["status"] = "UNREVIEWED_PDF_CANDIDATE_NOT_PIT_ELIGIBLE"
                    row["fields"] = [{
                        "field": item.field,
                        "valueYuan": str(item.value_yuan),
                        "pdfPage": item.pdf_page,
                        "columnHeader": item.column_header,
                        "amountUnit": item.amount_unit,
                        "sourceCurrentCell": item.source_cells[item.source_current_cell_index],
                        "sourcePriorCell": item.source_cells[item.source_prior_cell_index],
                        "displayRowLabel": item.display_row_label,
                        "sourceRow": item.source_row,
                        "reviewStatus": "PENDING_HUMAN_VISUAL",
                    } for item in candidate.candidates]
                except Exception as exc:  # noqa: BLE001 - candidate batch fails per PDF
                    row["error"] = f"{type(exc).__name__}: {exc}"
                output.append(row)
                print(f"{stock} {period}: {row['status']}", flush=True)
    return {
        "schema": "cninfo-industrial-s2-candidates-v1",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "sourceId": "cninfo", "status": "UNREVIEWED_NOT_PIT_ELIGIBLE",
        "pitEligible": False, "formalFactCount": 0,
        "reports": output,
        "summary": {
            "reports": len(output),
            "extracted": sum(row["status"] == "UNREVIEWED_PDF_CANDIDATE_NOT_PIT_ELIGIBLE"
                             for row in output),
            "failed": sum(row["status"] != "UNREVIEWED_PDF_CANDIDATE_NOT_PIT_ELIGIBLE"
                          for row in output),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--links", type=Path, default=LINKS_PATH)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--resume", action="store_true",
                        help="reuse successful rows only after rechecking receipt and PDF hash")
    parser.add_argument("--stock", action="append", choices=tuple(COMPANIES))
    parser.add_argument("--period", action="append", choices=DEFAULT_PERIODS)
    parser.add_argument("--trusted-proxy-network", action="append", default=[])
    args = parser.parse_args()
    if args.trusted_proxy_network:
        os.environ["AQUANT_TRUSTED_PROXY_NETWORKS"] = ",".join(args.trusted_proxy_network)
    previous = (json.loads(args.output.read_text(encoding="utf-8"))
                if args.resume and args.output.is_file() else None)
    result = collect(
        links_path=args.links, archive_root=args.archive_root,
        stocks=tuple(args.stock or COMPANIES),
        periods=tuple(args.period or DEFAULT_PERIODS),
        previous=previous,
    )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    print(f"{output}: {result['summary']}")


if __name__ == "__main__":
    main()
