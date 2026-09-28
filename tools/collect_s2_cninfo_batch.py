#!/usr/bin/env python3
"""Batch-archive official CNINFO report PDFs and extract S2 candidates.

This tool is deliberately candidate-only. Every invocation must name a finite
stock and report-period subset; it never scans the configured pool by default.
Official index and PDF responses are archived through the existing guarded,
rate-limited ``CninfoClient``. Failed security-periods are kept in the output
and do not stop the rest of the batch.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.cninfo import CninfoClient  # noqa: E402
from aquant.adapters.providers.cninfo_financial_candidate import (  # noqa: E402
    REQUIRED_PILOT_FIELDS_BY_PERIOD,
    extract_cninfo_candidate_facts,
)
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402

ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"
CONFIG_PATH = ROOT / "configs" / "real-pool-csrc.yaml"
OUTPUT_PATH = ROOT / "data" / "s2-pdf-review" / "cninfo-batch-candidates.json"
SCHEMA = "cninfo-s2-batch-candidates-v1"
SHANGHAI = timezone(timedelta(hours=8))
PERIODS = tuple(REQUIRED_PILOT_FIELDS_BY_PERIOD)
LEGAL_SUFFIXES = ("股份有限公司", "有限责任公司", "有限公司")


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _load_industrial_pool(path: Path) -> dict[str, dict[str, str]]:
    """Read the finite configured pool and keep CSRC manufacturing (C) rows."""
    document = json.loads(path.read_text(encoding="utf-8"))
    instruments = document.get("instruments")
    if not isinstance(instruments, list):
        raise ValueError("pool config has no instruments list")
    pool: dict[str, dict[str, str]] = {}
    for item in instruments:
        if not isinstance(item, dict):
            continue
        instrument_id = str(item.get("instrument_id") or "")
        code = str(item.get("code") or "")
        industry = str(item.get("industry") or "")
        name = str(item.get("name") or "").strip()
        found = re.search(r"(?:^|\.)(\d{6})$", instrument_id)
        if found is None:
            found = re.search(r"(?:sh|sz)(\d{6})$", code, re.IGNORECASE)
        if found is None or not name or not industry.startswith("C"):
            continue
        stock = found.group(1)
        if stock in pool:
            raise ValueError(f"duplicate security in pool config: {stock}")
        pool[stock] = {"name": name, "industry": industry,
                       "instrumentId": instrument_id}
    return pool


def _company_name_from_cover_text(
    cover_text: str, *, short_name: str, explicit_name: str | None = None,
) -> str:
    """Resolve a legal company name from the official PDF cover.

    An explicitly supplied full name is accepted only when it occurs on the
    cover. Automatic matching requires the configured pool alias to occur in
    the legal name, so a mismatched cover fails closed.
    """
    compact_cover = _compact(cover_text)
    if explicit_name:
        company = _compact(explicit_name)
        if (len(company) < 4 or _compact(short_name) not in company or
                not company.endswith(LEGAL_SUFFIXES) or company not in compact_cover):
            raise ValueError("supplied full company name must match stock alias, legal suffix, and official PDF cover")
        return company

    alias = _compact(short_name)
    names: set[str] = set()
    for raw_line in cover_text.splitlines():
        line = _compact(raw_line)
        # Covers sometimes prefix the entity with a labeled field. Strip only
        # that explicit label boundary; never start the name at the short name,
        # since the legal name may contain preceding characters (for example
        # a city name).
        if ":" in line or "：" in line:
            line = re.split(r"[:：]", line)[-1]
        for suffix in LEGAL_SUFFIXES:
            end = line.find(suffix)
            if end < 0:
                continue
            company = line[:end + len(suffix)]
            if (alias in company and len(company) >= len(alias) + len(suffix) and
                    not re.search(r"\d|报告|证券代码|股票代码|摘要|[—–―•]", company)):
                names.add(company)
    if len(names) != 1:
        raise ValueError("official PDF cover legal company name is missing or ambiguous; "
                         "supply --company-full-name CODE=FULL_NAME after checking the cover")
    return names.pop()


def _pdf_cover_text(pdf_bytes: bytes) -> str:
    try:
        import pdfplumber
    except ImportError as exc:
        raise ValueError("pdfplumber is required for PDF candidate extraction") from exc
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if not pdf.pages:
                raise ValueError("official PDF has no pages")
            return pdf.pages[0].extract_text() or ""
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - malformed cover must fail closed
        raise ValueError(f"PDF cover parsing failed: {type(exc).__name__}") from exc


def _index_evidence(index) -> dict:  # noqa: ANN001
    return {
        "organizationId": index.organization_id,
        "organizationLookupReceiptId": index.org_lookup_receipt_id,
        "organizationLookupContentHash": index.org_lookup_content_hash,
        "publicationWindow": [day.isoformat() for day in index.publication_window],
        "complete": index.complete,
        "termination": index.termination,
        "error": index.error,
        "pages": [{
            "page": page.page_num,
            "requestBodyHash": page.request_body_hash,
            "responseReceiptId": page.receipt_id,
            "responseContentHash": page.content_hash,
            "rawAnnouncementCount": page.raw_announcement_count,
            "totalAnnouncement": page.total_announcement,
            "hasMore": page.has_more,
        } for page in index.pages],
    }


def _discover_report(client: CninfoClient, *, stock: str, short_name: str,
                     period: str, through: date) -> dict:
    index = client.report_index(stock_code=stock, report_period=period,
                                max_pages=20, through=through)
    evidence = _index_evidence(index)
    if not index.complete:
        raise ValueError(f"official report index incomplete: {index.termination}: {index.error}")
    year = date.fromisoformat(period).year
    marker = f"{year}年半年度报告" if period.endswith("06-30") else f"{year}年年度报告"
    matches = [item for item in index.matches
               if marker in item.announcement.title
               and "摘要" not in item.announcement.title
               and "英文" not in item.announcement.title
               and item.announcement.url]
    if len(matches) != 1:
        raise ValueError(f"official full-report candidates ambiguous: {len(matches)}")
    match = matches[0]
    announcement = match.announcement
    if announcement.sec_code != stock or short_name not in announcement.sec_name:
        raise ValueError("official index security code/name differs from configured pool")
    indexed = {
        "announcementId": announcement.announcement_id,
        "announcedOn": announcement.announcement_date_cn,
        "documentUrl": announcement.url,
        "title": announcement.title,
        "indexEvidence": evidence,
        "indexMatch": {
            "page": match.page_num,
            "pageReceiptId": match.page_receipt_id,
            "pageContentHash": match.page_content_hash,
        },
        "versionReviewStatus": "PENDING_CROSS_CATEGORY_AND_HUMAN_DISPOSITION",
    }
    return indexed


def _verified_archive(archive: ForwardArchive, url: str) -> tuple[bytes, dict] | None:
    receipt = archive.con.execute(
        "SELECT receipt_id,source_id,url,outcome,http_status,content_hash,byte_size,first_seen_at "
        "FROM fetch_receipt WHERE source_id='cninfo' AND url=? AND outcome='OK' "
        "ORDER BY first_seen_at DESC LIMIT 1", (url,),
    ).fetchone()
    if receipt is None:
        return None
    digest = receipt["content_hash"]
    if (receipt["url"] != url or receipt["http_status"] != 200 or
            not isinstance(digest, str) or not digest.startswith("sha256:") or
            not archive.verify(digest)):
        raise ValueError("existing CNINFO PDF receipt or content is invalid")
    payload = archive.load_bytes(digest)
    if len(payload) != receipt["byte_size"]:
        raise ValueError("existing CNINFO PDF receipt size differs from bytes")
    return payload, dict(receipt)


def _obtain_pdf(archive: ForwardArchive, client: CninfoClient,
                url: str) -> tuple[bytes, dict]:
    existing = _verified_archive(archive, url)
    if existing is not None:
        return existing
    fetched = client.document(url, label="s2-batch-candidate")
    if not fetched.ok or fetched.payload is None or fetched.content_hash is None:
        raise ValueError(f"CNINFO PDF fetch failed: {fetched.detail or 'unknown error'}")
    verified = _verified_archive(archive, url)
    if verified is None or verified[1]["receipt_id"] != fetched.receipt_id:
        raise ValueError("new PDF was not durably archived")
    return verified


def _reusable_candidate(archive: ForwardArchive, prior: dict | None,
                        indexed: dict, required_fields: tuple[str, ...]) -> dict | None:
    if (prior is None or
            prior.get("status") != "UNREVIEWED_PDF_CANDIDATE_NOT_PIT_ELIGIBLE" or
            prior.get("pitEligible") is not False or
            prior.get("announcementId") != indexed["announcementId"] or
            prior.get("documentUrl") != indexed["documentUrl"] or
            {item.get("field") for item in prior.get("fields", [])} != set(required_fields)):
        return None
    archived = _verified_archive(archive, indexed["documentUrl"])
    if (archived is None or archived[1]["content_hash"] != prior.get("contentHash") or
            archived[1]["receipt_id"] != prior.get("archiveReceiptId")):
        return None
    return {**prior, **indexed}


def collect(*, config_path: Path, archive_root: Path,
            stocks: tuple[str, ...], periods: tuple[str, ...],
            previous: dict | None = None,
            company_full_names: dict[str, str] | None = None) -> dict:
    if not stocks or not periods:
        raise ValueError("explicit --stocks and --periods are required")
    if len(set(stocks)) != len(stocks) or len(set(periods)) != len(periods):
        raise ValueError("duplicate stocks or periods are not allowed")
    if any(period not in PERIODS for period in periods):
        raise ValueError("unsupported S2 report period")
    pool = _load_industrial_pool(config_path)
    if any(stock not in pool for stock in stocks):
        invalid = [stock for stock in stocks if stock not in pool]
        raise ValueError(f"stocks must be selected from configured CSRC manufacturing pool: {invalid}")
    if previous is not None and (previous.get("schema") != SCHEMA or
                                 previous.get("pitEligible") is not False):
        raise ValueError("resume file is not an unreviewed CNINFO batch candidate worksheet")
    prior_rows = {(row["instrumentId"], row["periodEnd"]): row
                  for row in (previous or {}).get("reports", [])}
    explicit_names = company_full_names or {}
    unknown_names = set(explicit_names) - set(stocks)
    if unknown_names:
        raise ValueError(f"full company name supplied for unselected stock: {sorted(unknown_names)}")

    output = []
    through = datetime.now(SHANGHAI).date()
    with closing(connect(archive_root / "meta.sqlite")) as con:
        archive = ForwardArchive(con, archive_root)
        client = CninfoClient(archive)
        for stock in stocks:
            instrument = pool[stock]
            for period in periods:
                row = {
                    "instrumentId": stock,
                    "poolInstrumentId": instrument["instrumentId"],
                    "companyShortName": instrument["name"],
                    "industry": instrument["industry"],
                    "periodEnd": period,
                    "status": "EXTRACTION_FAILED_NOT_PIT_ELIGIBLE",
                    "pitEligible": False,
                }
                try:
                    indexed = _discover_report(
                        client, stock=stock, short_name=instrument["name"],
                        period=period, through=through,
                    )
                    row.update(indexed)
                    prior = prior_rows.get((stock, period))
                    reused = _reusable_candidate(
                        archive, prior, indexed, REQUIRED_PILOT_FIELDS_BY_PERIOD[period],
                    )
                    if reused is not None:
                        output.append(reused)
                        print(f"{stock} {period}: reused verified candidate", flush=True)
                        continue

                    pdf_bytes, receipt = _obtain_pdf(archive, client, indexed["documentUrl"])
                    cover_text = _pdf_cover_text(pdf_bytes)
                    legal_name = _company_name_from_cover_text(
                        cover_text, short_name=instrument["name"],
                        explicit_name=explicit_names.get(stock),
                    )
                    row["coverCompanyName"] = legal_name
                    row.update({
                        "archiveReceiptId": receipt["receipt_id"],
                        "contentHash": receipt["content_hash"],
                        "byteSize": receipt["byte_size"],
                        "firstSeenAt": receipt["first_seen_at"],
                    })
                    candidate = extract_cninfo_candidate_facts(
                        pdf_bytes, instrument_id=stock, company=legal_name,
                        period_end=date.fromisoformat(period),
                        announcement_id=indexed["announcementId"],
                        document_url=indexed["documentUrl"],
                        required_fields=REQUIRED_PILOT_FIELDS_BY_PERIOD[period],
                    )
                    if (candidate.pit_eligible or
                            set(candidate.by_field) != set(REQUIRED_PILOT_FIELDS_BY_PERIOD[period]) or
                            candidate.pdf_sha256 != receipt["content_hash"]):
                        raise ValueError("candidate status, required fields, or hash differs from archive")
                    row["status"] = "UNREVIEWED_PDF_CANDIDATE_NOT_PIT_ELIGIBLE"
                    row["fields"] = [{
                        "field": item.field,
                        "valueYuan": str(item.value_yuan),
                        "pdfPage": item.pdf_page,
                        "columnHeader": item.column_header,
                        "amountUnit": item.amount_unit,
                        "currency": item.currency,
                        "sourceCurrentCell": item.source_cells[item.source_current_cell_index],
                        "sourcePriorCell": item.source_cells[item.source_prior_cell_index],
                        "displayRowLabel": item.display_row_label,
                        "sourceRow": item.source_row,
                        "pdfSha256": item.pdf_sha256,
                        "reviewStatus": "PENDING_HUMAN_VISUAL",
                    } for item in candidate.candidates]
                except Exception as exc:  # noqa: BLE001 - isolate each security-period
                    row["error"] = f"{type(exc).__name__}: {exc}"
                output.append(row)
                print(f"{stock} {period}: {row['status']}", flush=True)

    return {
        "schema": SCHEMA,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "sourceId": "cninfo",
        "status": "UNREVIEWED_NOT_PIT_ELIGIBLE",
        "pitEligible": False,
        "formalFactCount": 0,
        "configPath": str(config_path),
        "selectedStocks": list(stocks),
        "selectedPeriods": list(periods),
        "reports": output,
        "summary": {
            "reports": len(output),
            "extracted": sum(row["status"] == "UNREVIEWED_PDF_CANDIDATE_NOT_PIT_ELIGIBLE"
                              for row in output),
            "failed": sum(row["status"] != "UNREVIEWED_PDF_CANDIDATE_NOT_PIT_ELIGIBLE"
                          for row in output),
        },
    }


def _parse_full_names(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        stock, separator, name = value.partition("=")
        if not separator or not re.fullmatch(r"\d{6}", stock) or not name.strip():
            raise ValueError("--company-full-name must use CODE=FULL_NAME")
        if stock in result:
            raise ValueError(f"duplicate --company-full-name for {stock}")
        result[stock] = name.strip()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--stocks", nargs="+", required=True,
                        help="explicit six-digit stock codes from the configured C-sector pool")
    parser.add_argument("--periods", nargs="+", required=True,
                        choices=PERIODS, help="explicit S2 report periods")
    parser.add_argument("--company-full-name", action="append", default=[],
                        metavar="CODE=FULL_NAME",
                        help="optional legal name; it must match the official PDF cover")
    parser.add_argument("--resume", action="store_true",
                        help="requery the complete regular index and reuse only hash-verified archived candidates")
    parser.add_argument("--trusted-proxy-network", action="append", default=[],
                        help="explicitly trusted local proxy CIDR for this process only")
    args = parser.parse_args()
    if args.trusted_proxy_network:
        os.environ["AQUANT_TRUSTED_PROXY_NETWORKS"] = ",".join(args.trusted_proxy_network)
    try:
        full_names = _parse_full_names(args.company_full_name)
        previous = (json.loads(args.output.read_text(encoding="utf-8"))
                    if args.resume and args.output.is_file() else None)
        result = collect(
            config_path=args.config, archive_root=args.archive_root,
            stocks=tuple(args.stocks), periods=tuple(args.periods),
            previous=previous, company_full_names=full_names,
        )
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
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
