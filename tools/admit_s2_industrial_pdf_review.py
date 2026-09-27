#!/usr/bin/env python3
"""Local human-review intake for one frozen S2 stock/report cohort.

``prepare`` verifies a prepare_s2_industrial_pdf_review worklist, captures full
official indexes, and emits EMPTY human attestations. ``admit`` defaults to a
dry run in an isolated temporary fact database. Only --commit appends the one
complete report bundle atomically to the explicitly named fact database.

Human fields must be filled independently after reading the PDF and notices.
Neither preparation nor a successful dry run signs a review. No historical
first_seen_at or available_at can be supplied in the human review section.
Both commands use the network and append fresh fetch receipts to the forward
archive. Dry-run never creates or modifies the explicitly named fact database.

Example (repeat separately for each reviewed report):
  python tools/admit_s2_industrial_pdf_review.py prepare --worklist WORKLIST.json
    --candidates CANDIDATES.json --archive-root ARCHIVE --stock 000651
    --period 2024-06-30 --output REVIEW.json
  # Human reads archived PDF cells and all title candidates, then fills REVIEW.
  python tools/admit_s2_industrial_pdf_review.py admit --worklist WORKLIST.json
    --candidates CANDIDATES.json --archive-root ARCHIVE --review REVIEW.json
    --fact-db FACTS.sqlite --trading-calendar CALENDAR.json
  # Append --commit to the same admit command only after the review is complete.

The calendar is an explicit JSON array of sorted unique trading-date strings.
Required related notices must already have successful archived PDF receipts;
use CninfoClient.document to archive them before the human review. A related
amendment affecting an original report can still be rejected by the existing
promotion policy; this CLI does not override that policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from aquant.adapters.providers.cninfo import (  # noqa: E402
    CninfoClient, CrossCategoryIndexEntry, CrossCategoryIndexResult, ReportIndexPage,
)
from aquant.adapters.providers.cninfo_financial_candidate import (  # noqa: E402
    extract_cninfo_candidate_facts,
)
from aquant.adapters.providers.cninfo_probe import market_for_code, parse_report_period  # noqa: E402
from aquant.adapters.providers.pdf_promotion import (  # noqa: E402
    CrossCategoryDisposition, PdfFieldReview, PdfVersionReview,
    _archived_ok, _verified_index_request, _verified_org_lookup,
    cninfo_query_digest, promote_reviewed_cninfo_subset_bundle,
)
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402
from aquant.domain.fundamentals.fact_repository import (  # noqa: E402
    FactReviewBundle, FinancialFactRepository,
)
from aquant.domain.fundamentals.s2_pdf_review_policy import (  # noqa: E402
    S2_PDF_PILOT_COMPANIES, S2_PDF_REQUIRED_FIELDS,
)
from tools.prepare_s2_industrial_pdf_review import build_review_worklist  # noqa: E402

SCHEMA = "cninfo-industrial-s2-human-intake-v1"
BEIJING = timezone(timedelta(hours=8))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _jsonable(value):
    return json.loads(json.dumps(value, default=lambda item: item.isoformat(),
                                ensure_ascii=False))


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _load(path: Path) -> tuple[dict, str]:
    payload = path.read_bytes()
    return json.loads(payload), _digest(payload)


def _required_text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be explicitly filled by the human reviewer")
    return value


def _review_time(value, label: str) -> datetime:
    result = datetime.fromisoformat(_required_text(value, label))
    if result.tzinfo is None or result.utcoffset() is None or result > _now():
        raise ValueError(f"{label} must be timezone-aware and not in the future")
    return result


def _base_report(report: dict) -> dict:
    # Cross-index prompts in the old worklist may be stale; this tool captures
    # new indexes before human review. All PDF/candidate prompts stay bound.
    return {key: value for key, value in report.items()
            if not key.startswith("crossCategory")}


def _verified_report(worklist_path: Path, candidates_path: Path, archive: ForwardArchive,
                     stock: str, period: str) -> tuple[dict, str, str]:
    if stock not in S2_PDF_PILOT_COMPANIES or date.fromisoformat(period) not in S2_PDF_REQUIRED_FIELDS:
        raise ValueError("outside the fixed five-stock/five-period S2 review scope")
    worklist, worklist_hash = _load(worklist_path)
    worksheet, candidates_hash = _load(candidates_path)
    if (worklist.get("schema") != "cninfo-industrial-s2-pdf-review-worklist-v1" or
            worklist.get("status") != "UNREVIEWED_NOT_PIT_ELIGIBLE" or
            worklist.get("pitEligible") is not False or worklist.get("formalFactCount") != 0):
        raise ValueError("input must be the unreviewed industrial PDF worklist")
    matching = [item for item in worklist["reports"]
                if (item["instrumentId"], item["periodEnd"]) == (stock, period)]
    if len(matching) != 1:
        raise ValueError("worklist must identify exactly one selected report")
    rebuilt = build_review_worklist(worksheet, archive.con, archive.root,
                                   stocks=(stock,), periods=(period,))["reports"][0]
    if _base_report(matching[0]) != _base_report(rebuilt):
        raise ValueError("worklist candidate changed; rebuild and repeat human review")
    return _base_report(rebuilt), worklist_hash, candidates_hash


def _index_signature(snapshot: dict, archive: ForwardArchive, *, cross: bool) -> list:
    """Rebuild a complete semantic index from archived responses, not JSON claims.

    Receipt IDs and pagination can change on refresh; exact announcement rows,
    organization and query scope cannot. Dynamic transport IDs are excluded.
    """
    stock, period_text = snapshot["stock_code"], snapshot["report_period"]
    period = parse_report_period(period_text)
    window = tuple(date.fromisoformat(item) for item in snapshot["publication_window"])
    if (snapshot["complete"] is not True or snapshot["error"] is not None or
            not snapshot["pages"] or len(snapshot["pages"]) > 100 or
            (not cross and window != period.publication_window) or
            (cross and (window[0] != period.end_date or window[1] > _now().astimezone(BEIJING).date()))):
        raise ValueError("complete official index with the required query scope is missing")
    organization = snapshot["organization_id"]
    org = _verified_org_lookup(archive, receipt_id=snapshot["org_lookup_receipt_id"],
                               stock_code=stock, organization_id=organization)
    if org["content_hash"] != snapshot["org_lookup_content_hash"]:
        raise ValueError("organization lookup hash changed")
    rows, ids, totals, sizes = [], set(), set(), set()
    for number, page in enumerate(snapshot["pages"], 1):
        receipt, payload = _archived_ok(archive, page["receipt_id"], expected_host="www.cninfo.com.cn")
        if (page["page_num"] != number or receipt["content_hash"] != page["content_hash"] or
                receipt["http_status"] != page["http_status"] or
                urlsplit(receipt["url"]).path != "/new/hisAnnouncement/query"):
            raise ValueError("index page identity differs from archive")
        prefix = (f"cross-category:{stock}:{period_text}~{window[1]}" if cross else
                  f"report-index:{stock}:{period_text}")
        body_hash, size = _verified_index_request(
            archive, receipt, stock_code=stock, organization_id=organization,
            page_number=number, category="" if cross else period.category,
            market=market_for_code(stock), period_start=window[0], period_end=window[1],
            detail_prefix=prefix,
        )
        if body_hash != page["request_body_hash"]:
            raise ValueError("index request hash changed")
        sizes.add(size)
        response = json.loads(payload)
        current = response["announcements"]
        more = response.get("hasMore")
        if isinstance(more, str) and more.strip().lower() in ("true", "false"):
            more = more.strip().lower() == "true"
        elif type(more) is int and more in (0, 1):
            more = bool(more)
        raw_total = response.get("totalAnnouncement")
        total = None if raw_total is None else int(raw_total)
        if (not isinstance(current, list) or isinstance(raw_total, bool) or
                (total is not None and total < 0) or
                (more is not None and type(more) is not bool) or
                page["raw_announcement_count"] != len(current) or
                page["total_announcement"] != total or page["has_more"] is not more):
            raise ValueError("index pagination is incomplete or differs from archive")
        if total is not None:
            totals.add(total)
        known_total = next(iter(totals)) if len(totals) == 1 else None
        cumulative = len(rows) + len(current)
        last = number == len(snapshot["pages"])
        if ((more is True and (last or (known_total is not None and cumulative >= known_total))) or
                (more is False and (not last or (known_total is not None and cumulative != known_total))) or
                (more is None and (known_total is None or
                    (cumulative != known_total if last else cumulative >= known_total)))):
            raise ValueError("index pagination is incomplete or ambiguous")
        for row in current:
            ann_id = str(row.get("announcementId") or "")
            code = str(row.get("secCode") or "").strip()
            if (not ann_id or ann_id in ids or not re.fullmatch(r"\d{6}", code) or
                    (not cross and code != stock)):
                raise ValueError("index identity missing, repeated or wrong-stock")
            ids.add(ann_id)
            rows.append(row)
    if ((totals and totals != {len(rows)}) or len(sizes) != 1 or
            snapshot["total_announcement"] != (next(iter(totals)) if totals else None) or
            snapshot["termination"] != ("has_more_false" if more is False else "total_reached")):
        raise ValueError("complete index exhaustion is not proven")
    return [stock, organization, period_text, snapshot["publication_window"],
            sorted(rows, key=lambda row: str(row["announcementId"]))]


def _capture(client: CninfoClient, stock: str, period: str, through: date) -> tuple[dict, dict]:
    # The promotion contract requires the full regular-report publication
    # window. The independent all-category search ends on the review day.
    regular = client.report_index(stock_code=stock, report_period=period, max_pages=100)
    cross = client.cross_category_index(stock_code=stock, report_period=period, through=through)
    if not regular.complete or not cross.complete:
        raise ValueError(f"official indexes incomplete: {regular.error or cross.error}")
    return _jsonable(asdict(regular)), _jsonable(asdict(cross))


def _cross_result(snapshot: dict) -> CrossCategoryIndexResult:
    values = dict(snapshot)
    values["publication_window"] = tuple(date.fromisoformat(day) for day in values["publication_window"])
    values["pages"] = tuple(ReportIndexPage(**page) for page in values["pages"])
    values["announcements"] = tuple(CrossCategoryIndexEntry(
        **{**entry, "candidate_reasons": tuple(entry["candidate_reasons"])})
        for entry in values["announcements"])
    return CrossCategoryIndexResult(**values)


def prepare_packet(*, worklist_path: Path, candidates_path: Path, archive: ForwardArchive,
                   client: CninfoClient, stock: str, period: str) -> dict:
    report, worklist_hash, candidates_hash = _verified_report(
        worklist_path, candidates_path, archive, stock, period)
    regular, cross = _capture(client, stock, period, _now().astimezone(BEIJING).date())
    _index_signature(regular, archive, cross=False)
    _index_signature(cross, archive, cross=True)
    if regular["organization_id"] != cross["organization_id"]:
        raise ValueError("regular and cross-category organizations disagree")
    matches = [item for item in regular["matches"]
               if item["announcement"]["announcement_id"] == report["announcementId"]]
    if len(matches) != 1:
        raise ValueError("selected PDF is absent from the complete regular index")
    return {
        "schema": SCHEMA,
        "notice": "Machine evidence is not a signature. Fill human_review only after independent visual and notice review. Re-prepare if indexes change or review moves to another Beijing date.",
        "worklist_sha256": worklist_hash, "candidates_sha256": candidates_hash,
        "report": report, "regular_index": regular, "cross_category_index": cross,
        "human_review": {
            "fields": [{"field": item["field"], "reviewed_value_yuan": None,
                        "pdf_page": None, "current_cell": None, "row_label": None,
                        "source_amount_unit": None, "reviewer_id": None, "reviewed_at": None}
                       for item in report["fields"]],
            "version": {"reviewer_id": None, "reviewed_at": None,
                        "complete_search_attested": False, "rationale": None,
                        "predecessor_announcement_id": None,
                        "correction_receipt_id": None, "correction_excerpt": None},
            "cross_category": {"reviewer_id": None, "reviewed_at": None,
                "dispositions": [{"announcement_id": entry["announcement_id"],
                    "relevance": None, "reviewer_id": None, "reviewed_at": None,
                    "rationale": None, "document_receipt_id": None,
                    "related_report_announcement_ids": [], "relation_receipt_id": None,
                    "relation_excerpt": None} for entry in cross["announcements"]
                    if entry["candidate_reasons"]]},
        },
    }


def _review_arguments(packet: dict) -> dict:
    human, report, regular = packet["human_review"], packet["report"], packet["regular_index"]
    fields = []
    for item in human["fields"]:
        value = Decimal(_required_text(item["reviewed_value_yuan"], "reviewed_value_yuan"))
        if not value.is_finite() or type(item["pdf_page"]) is not int or item["pdf_page"] < 1:
            raise ValueError("reviewed amount/page is invalid")
        fields.append(PdfFieldReview(
            field=_required_text(item["field"], "field"), reviewed_value_yuan=value,
            pdf_page=item["pdf_page"], current_cell=_required_text(item["current_cell"], "current_cell"),
            row_label=_required_text(item["row_label"], "row_label"),
            source_amount_unit=_required_text(item["source_amount_unit"], "source_amount_unit"),
            reviewer_id=_required_text(item["reviewer_id"], "field reviewer_id"),
            reviewed_at=_review_time(item["reviewed_at"], "field reviewed_at"),
        ))
    version = human["version"]
    if version["complete_search_attested"] is not True:
        raise ValueError("version search must be explicitly attested")
    _required_text(version["rationale"], "version rationale")
    selected = [item for item in regular["matches"]
                if item["announcement"]["announcement_id"] == report["announcementId"]]
    if len(selected) != 1:
        raise ValueError("exactly one selected regular report is required")
    start, end = (date.fromisoformat(day) for day in regular["publication_window"])
    version_review = PdfVersionReview(
        announcement_id=report["announcementId"], org_lookup_receipt_id=regular["org_lookup_receipt_id"],
        index_receipt_id=selected[0]["page_receipt_id"],
        search_receipt_ids=tuple(page["receipt_id"] for page in regular["pages"]),
        search_page_numbers=tuple(page["page_num"] for page in regular["pages"]),
        query_stock_code=report["instrumentId"], query_organization_id=regular["organization_id"],
        query_period_start=start, query_period_end=end,
        query_digest=cninfo_query_digest(stock_code=report["instrumentId"],
            organization_id=regular["organization_id"], period_start=start, period_end=end),
        reviewer_id=_required_text(version["reviewer_id"], "version reviewer_id"),
        reviewed_at=_review_time(version["reviewed_at"], "version reviewed_at"),
        complete_search_attested=True, predecessor_announcement_id=version["predecessor_announcement_id"],
        correction_receipt_id=version["correction_receipt_id"], correction_excerpt=version["correction_excerpt"],
    )
    cross = human["cross_category"]
    dispositions = []
    for item in cross["dispositions"]:
        ids = item["related_report_announcement_ids"]
        if not isinstance(ids, list) or any(not isinstance(value, str) or not value for value in ids):
            raise ValueError("related_report_announcement_ids must be an explicit string list")
        dispositions.append(CrossCategoryDisposition(
            announcement_id=_required_text(item["announcement_id"], "disposition announcement_id"),
            relevance=_required_text(item["relevance"], "disposition relevance"),
            reviewer_id=_required_text(item["reviewer_id"], "disposition reviewer_id"),
            reviewed_at=_review_time(item["reviewed_at"], "disposition reviewed_at"),
            rationale=_required_text(item["rationale"], "disposition rationale"),
            document_receipt_id=item["document_receipt_id"], related_report_announcement_ids=tuple(ids),
            relation_receipt_id=item["relation_receipt_id"], relation_excerpt=item["relation_excerpt"],
        ))
    return dict(version_review=version_review, field_reviews=tuple(fields),
                cross_category_index=_cross_result(packet["cross_category_index"]),
                cross_category_dispositions=tuple(dispositions),
                cross_category_reviewed_by=_required_text(cross["reviewer_id"], "cross-category reviewer_id"),
                cross_category_reviewed_at=_review_time(cross["reviewed_at"], "cross-category reviewed_at"))


def admit_packet(*, packet: dict, worklist_path: Path, candidates_path: Path,
                 archive: ForwardArchive, client: CninfoClient, fact_db: Path,
                 trading_calendar: tuple[date, ...], commit: bool = False) -> dict:
    if packet.get("schema") != SCHEMA:
        raise ValueError("unknown human review input schema")
    report = packet["report"]
    rebuilt, worklist_hash, candidates_hash = _verified_report(
        worklist_path, candidates_path, archive, report["instrumentId"], report["periodEnd"])
    if (report != rebuilt or worklist_hash != packet["worklist_sha256"] or
            candidates_hash != packet["candidates_sha256"]):
        raise ValueError("candidate/worklist changed; rebuild and repeat human review")
    arguments = _review_arguments(packet)
    reviewed_day = arguments["cross_category_reviewed_at"].astimezone(BEIJING).date()
    if (reviewed_day != _now().astimezone(BEIJING).date() or
            packet["cross_category_index"]["publication_window"][1] != reviewed_day.isoformat()):
        raise ValueError("review/index must be from today's Beijing date; re-prepare and re-review")
    old_regular = _index_signature(packet["regular_index"], archive, cross=False)
    old_cross = _index_signature(packet["cross_category_index"], archive, cross=True)
    current_regular, current_cross = _capture(client, report["instrumentId"], report["periodEnd"], reviewed_day)
    if (old_regular != _index_signature(current_regular, archive, cross=False) or
            old_cross != _index_signature(current_cross, archive, cross=True)):
        raise ValueError("official index changed; rebuild packet and repeat human review")
    if _now().astimezone(BEIJING).date() != reviewed_day:
        raise ValueError("Beijing date changed during verification; repeat review")
    _, payload = _archived_ok(archive, report["pdfReceiptId"], expected_url=report["documentUrl"])
    candidate = extract_cninfo_candidate_facts(
        payload, instrument_id=report["instrumentId"], company=report["company"],
        period_end=date.fromisoformat(report["periodEnd"]), announcement_id=report["announcementId"],
        document_url=report["documentUrl"],
        required_fields=tuple(sorted(S2_PDF_REQUIRED_FIELDS[date.fromisoformat(report["periodEnd"])])),
    )
    # Preflight against a consistent SQLite backup. No writes to the requested
    # fact DB until all provider, PDF, field, lineage and calendar checks pass.
    with tempfile.TemporaryDirectory(prefix="aquant-s2-review-") as directory:
        staged_path = Path(directory) / "facts.sqlite"
        if fact_db.exists():
            FinancialFactRepository.open_existing(fact_db)
            with closing(connect(fact_db, read_only=True)) as source, closing(connect(staged_path)) as target:
                source.backup(target)
        staged = FinancialFactRepository(staged_path)
        facts = promote_reviewed_cninfo_subset_bundle(
            candidate=candidate, pdf_receipt_id=report["pdfReceiptId"], archive=archive,
            repository=staged, trading_calendar=trading_calendar, **arguments)
        with closing(connect(staged_path, read_only=True)) as con:
            rows = con.execute(
                "SELECT r.bundle_id,r.evidence_json FROM financial_fact_review r "
                "JOIN financial_fact_review_member m USING(bundle_id) WHERE m.version_id=?",
                (facts[0].version_id,),
            ).fetchall()
        if len(rows) != 1:
            raise ValueError("preflight did not produce exactly one reviewed report bundle")
        evidence = json.loads(rows[0]["evidence_json"])
        evidence["local_intake"] = {
            "schema": SCHEMA, "review_input_sha256": _digest(json.dumps(packet, ensure_ascii=False,
                sort_keys=True, separators=(",", ":")).encode()),
            "version_rationale": packet["human_review"]["version"]["rationale"],
            "freshness_regular_receipts": [page["receipt_id"] for page in current_regular["pages"]],
            "freshness_cross_category_receipts": [page["receipt_id"] for page in current_cross["pages"]],
            "verified_at": _now().isoformat(),
        }
        bundle = FactReviewBundle(rows[0]["bundle_id"], json.dumps(evidence, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")))
        if commit:
            if _now() >= min(fact.available_at for fact in facts):
                raise ValueError("next decision snapshot elapsed during preflight; rerun admission")
            FinancialFactRepository(fact_db).append_many(facts, review_bundle=bundle)
    return {"mode": "committed" if commit else "dry-run", "instrument_id": report["instrumentId"],
            "period_end": report["periodEnd"], "validated_fact_count": len(facts),
            "written_fact_count": len(facts) if commit else 0,
            "available_at": max(fact.available_at for fact in facts).isoformat(),
            "fact_db": str(fact_db.resolve())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "admit"):
        child = sub.add_parser(command)
        child.add_argument("--worklist", type=Path, required=True)
        child.add_argument("--candidates", type=Path, required=True)
        child.add_argument("--archive-root", type=Path, required=True)
        if command == "prepare":
            child.add_argument("--stock", choices=tuple(S2_PDF_PILOT_COMPANIES), required=True)
            child.add_argument("--period", choices=tuple(day.isoformat() for day in S2_PDF_REQUIRED_FIELDS), required=True)
            child.add_argument("--output", type=Path, required=True)
        else:
            child.add_argument("--review", type=Path, required=True)
            child.add_argument("--fact-db", type=Path, required=True)
            child.add_argument("--trading-calendar", type=Path, required=True,
                               help="JSON array of sorted unique ISO trading dates, covering publication and next snapshot")
            child.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    if not (args.archive_root / "meta.sqlite").is_file():
        parser.error("an existing forward archive is required")
    with closing(connect(args.archive_root / "meta.sqlite")) as con:
        archive = ForwardArchive(con, args.archive_root)
        client = CninfoClient(archive)
        common = dict(worklist_path=args.worklist, candidates_path=args.candidates,
                      archive=archive, client=client)
        if args.command == "prepare":
            result = prepare_packet(stock=args.stock, period=args.period, **common)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            print(f"Prepared {args.output}; all human attestations are empty; no facts written.")
        else:
            raw_calendar = json.loads(args.trading_calendar.read_text(encoding="utf-8"))
            if not isinstance(raw_calendar, list):
                parser.error("trading calendar must be an explicit JSON date array")
            calendar = tuple(date.fromisoformat(day) for day in raw_calendar)
            result = admit_packet(packet=_load(args.review)[0], fact_db=args.fact_db,
                trading_calendar=calendar, commit=args.commit, **common)
            print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
