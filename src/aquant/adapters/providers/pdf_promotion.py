"""Explicit, default-closed admission of reviewed CNINFO PDF S2 facts.

This is a narrow bridge from the SHA-locked PDF candidate extractor to formal
forward PIT facts.  A pilot JSON, a candidate dataclass, or an announcement URL
alone cannot call this bridge successfully: archived bytes, a successful
receipt, an official index row, and independent field/version attestations are
all required.  Historical PDFs become usable only after *this* observation and
review, never at their old publication date.

The paginated official index archives every POST request body separately and
binds its hash to the matching response receipt.  Admission verifies that
binding and all request parameters again; the reviewer's version relationship
attestation remains distinct from title-only revision hints.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Sequence
from urllib.parse import parse_qs, urlsplit

from ...domain.data.forward_archive import ForwardArchive
from ...domain.data.pit import (
    AvailabilityBasis, PitMode, TimestampPrecision, date_only_available_at,
    preopen_instant,
)
from ...domain.data.rights import REGISTER_VERSION, RightsRegistry, default_rights
from ...domain.fundamentals.disclosure_link import (
    DisclosureRole, disclosure_role, report_period_from_title,
)
from ...domain.fundamentals.fact_repository import FactReviewBundle, FinancialFactRepository
from ...domain.fundamentals.versioned import FinancialFact, ProfitScope, StatementScope
from .cninfo import CrossCategoryIndexEntry, CrossCategoryIndexResult, parse_announcements
from .cninfo_financial_pdf import CninfoS2CandidateFacts, extract_s2_candidate_facts
from .cninfo_probe import (
    market_for_code, normalize_title, parse_report_period,
    title_matches_period, title_revision_flags,
)


_FIELDS = frozenset({
    "net_profit_attributable", "net_profit_consolidated", "operating_cashflow",
    "revenue", "parent_equity",
})


class PdfPromotionError(ValueError):
    """The reviewed report lacks a required admission proof."""


@dataclass(frozen=True, slots=True)
class PdfFieldReview:
    field: str
    reviewed_value_yuan: Decimal
    pdf_page: int
    current_cell: str
    row_label: str
    source_amount_unit: str
    reviewer_id: str
    reviewed_at: datetime
    method: str = "HUMAN_VISUAL"


@dataclass(frozen=True, slots=True)
class PdfVersionReview:
    announcement_id: str
    org_lookup_receipt_id: str
    index_receipt_id: str
    search_receipt_ids: tuple[str, ...]
    search_page_numbers: tuple[int, ...]
    query_stock_code: str
    query_organization_id: str
    query_period_start: date
    query_period_end: date
    query_digest: str
    reviewer_id: str
    reviewed_at: datetime
    complete_search_attested: bool = False
    predecessor_announcement_id: str | None = None
    correction_receipt_id: str | None = None
    correction_excerpt: str | None = None


@dataclass(frozen=True, slots=True)
class CrossCategoryDisposition:
    """Human disposition of one all-category title-screening candidate."""

    announcement_id: str
    relevance: str  # RELATED or UNRELATED
    reviewer_id: str
    reviewed_at: datetime
    rationale: str
    document_receipt_id: str | None = None
    related_report_announcement_ids: tuple[str, ...] = ()
    relation_receipt_id: str | None = None
    relation_excerpt: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PdfPromotionError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _archived_ok(archive: ForwardArchive, receipt_id: str, *,
                 expected_url: str | None = None,
                 expected_host: str | None = None) -> tuple[dict, bytes]:
    row = archive.con.execute(
        "SELECT * FROM fetch_receipt WHERE receipt_id = ?", (receipt_id,),
    ).fetchone()
    if row is None:
        raise PdfPromotionError(f"missing archive receipt {receipt_id!r}")
    receipt = dict(row)
    digest = receipt["content_hash"]
    if (receipt["source_id"] != "cninfo" or receipt["outcome"] != "OK" or
            receipt["http_status"] != 200 or not isinstance(digest, str) or
            not digest.startswith("sha256:") or receipt["byte_size"] is None):
        raise PdfPromotionError("archive receipt is not a successful CNINFO response")
    url = receipt["url"]
    if expected_url is not None and url != expected_url:
        raise PdfPromotionError("archive receipt URL does not match reviewed document")
    parts = urlsplit(url)
    if (parts.scheme not in ("http", "https") or
            parts.hostname not in ("www.cninfo.com.cn", "static.cninfo.com.cn") or
            (expected_host is not None and parts.hostname != expected_host)):
        raise PdfPromotionError("archive receipt is not from the expected official host")
    if not archive.verify(digest):
        raise PdfPromotionError("archived bytes fail SHA-256 verification")
    payload = archive.load_bytes(digest)
    if len(payload) != receipt["byte_size"]:
        raise PdfPromotionError("archive receipt byte size does not match archived bytes")
    first_seen = _aware(datetime.fromisoformat(receipt["first_seen_at"]), "first_seen_at")
    requested = _aware(datetime.fromisoformat(receipt["requested_at"]), "requested_at")
    if first_seen < requested:
        raise PdfPromotionError("archive receipt predates its request")
    return receipt, payload


def _same_document_url(index_url: str | None, pdf_url: str) -> bool:
    if index_url is None:
        return False
    a, b = urlsplit(index_url), urlsplit(pdf_url)
    return (a.hostname == b.hostname == "static.cninfo.com.cn" and
            a.path == b.path and not a.query and not b.query)


def cninfo_query_digest(*, stock_code: str, organization_id: str,
                        period_start: date, period_end: date) -> str:
    """Digest the declared query scope; request-body capture is still required
    for machine proof that this exact query was actually sent.
    """

    value = json.dumps({
        "stock_code": stock_code, "organization_id": organization_id,
        "period_start": period_start.isoformat(), "period_end": period_end.isoformat(),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _pdf_text(payload: bytes) -> str:
    """Extract a short correction notice for an exact reviewer excerpt check."""

    import io

    import pdfplumber

    with pdfplumber.open(io.BytesIO(payload)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def _verified_org_lookup(archive: ForwardArchive, *, receipt_id: str,
                         stock_code: str, organization_id: str) -> dict:
    receipt, payload = _archived_ok(
        archive, receipt_id, expected_host="www.cninfo.com.cn",
    )
    url = urlsplit(receipt["url"])
    query = parse_qs(url.query)
    if (url.path != "/new/information/topSearch/query" or
            query.get("keyWord") != [stock_code] or
            query.get("maxNum") != ["10"]):
        raise PdfPromotionError("organization lookup request does not match security")
    try:
        rows = json.loads(payload)
    except (ValueError, TypeError) as exc:
        raise PdfPromotionError("organization lookup response cannot be parsed") from exc
    if not isinstance(rows, list):
        raise PdfPromotionError("organization lookup response is not a list")
    matches = {
        str(item.get("orgId") or "").strip()
        for item in rows if isinstance(item, dict)
        and str(item.get("code") or "").strip() == stock_code
        and str(item.get("orgId") or "").strip()
    }
    if matches != {organization_id}:
        raise PdfPromotionError("organization lookup does not prove the reviewed orgId")
    return receipt


def _verified_index_request(
    archive: ForwardArchive, receipt: dict, *,
    stock_code: str, organization_id: str, page_number: int,
    category: str, market: str, period_start: date, period_end: date,
    detail_prefix: str,
) -> tuple[str, int]:
    prefix = f"{detail_prefix}:page={page_number}:request="
    detail = receipt["detail"] or ""
    if not detail.startswith(prefix):
        raise PdfPromotionError("index response receipt lacks matching request binding")
    body_hash = detail.removeprefix(prefix)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", body_hash):
        raise PdfPromotionError("index request hash in receipt is malformed")
    if not archive.verify(body_hash):
        raise PdfPromotionError("archived index POST request body fails SHA-256")
    try:
        params = parse_qs(archive.load_bytes(body_hash).decode("utf-8"),
                          keep_blank_values=True, strict_parsing=True)
        page_size = int(params["pageSize"][0])
    except (UnicodeError, ValueError, KeyError, IndexError) as exc:
        raise PdfPromotionError("archived index POST request body is malformed") from exc
    if not 1 <= page_size <= 100:
        raise PdfPromotionError("index pageSize is outside approved bounds")
    expected = {
        "pageNum": str(page_number), "pageSize": str(page_size),
        "column": market, "tabName": "fulltext", "plate": "",
        "stock": f"{stock_code},{organization_id}",
        "searchkey": "", "secid": "", "category": category, "trade": "",
        "seDate": f"{period_start.isoformat()}~{period_end.isoformat()}",
        "sortName": "", "sortType": "", "isHLtitle": "true",
    }
    if params != {key: [value] for key, value in expected.items()}:
        raise PdfPromotionError("index POST request stock/org/period/page scope mismatch")
    return body_hash, page_size


def _check_reviews(candidate: CninfoS2CandidateFacts,
                   reviews: Sequence[PdfFieldReview], *,
                   first_seen: datetime, now: datetime) -> None:
    by_field = candidate.by_field
    if (set(by_field) != _FIELDS or len(candidate.candidates) != len(_FIELDS) or
            len(reviews) != len(_FIELDS) or {r.field for r in reviews} != _FIELDS):
        raise PdfPromotionError("five distinct S2 fields and five field reviews are required")
    for review in reviews:
        item = by_field[review.field]
        if (not review.reviewer_id.strip() or review.method != "HUMAN_VISUAL" or
                _aware(review.reviewed_at, "reviewed_at") < first_seen or
                _aware(review.reviewed_at, "reviewed_at") > now):
            raise PdfPromotionError(f"{review.field}: human review identity/time is invalid")
        if item.amount_unit not in ("元", "千元") or review.source_amount_unit != item.amount_unit:
            raise PdfPromotionError(f"{review.field}: source amount unit is unverified")
        try:
            reviewed_raw = Decimal(review.current_cell.replace(",", "").strip())
        except (ValueError, ArithmeticError) as exc:
            raise PdfPromotionError(f"{review.field}: reviewed source cell is not numeric") from exc
        scale = Decimal(1000) if item.amount_unit == "千元" else Decimal(1)
        if (review.reviewed_value_yuan != item.value_yuan or
                reviewed_raw * scale != review.reviewed_value_yuan or
                review.pdf_page != item.pdf_page or
                review.current_cell != item.source_cells[item.source_current_cell_index] or
                review.row_label != item.source_cells[0] or
                item.pdf_sha256 != candidate.pdf_sha256 or
                item.period_end != candidate.period_end or
                item.currency != "CNY"):
            raise PdfPromotionError(f"{review.field}: reviewed amount/cell/page differs from PDF")


def _version_evidence(
    archive: ForwardArchive, candidate: CninfoS2CandidateFacts,
    review: PdfVersionReview, *, now: datetime,
) -> tuple[date, DisclosureRole, list[dict], list[str]]:
    if (not review.complete_search_attested or not review.reviewer_id.strip() or
            not review.search_receipt_ids or
            review.index_receipt_id not in review.search_receipt_ids):
        raise PdfPromotionError("complete official version search must be attested")
    period = parse_report_period(candidate.period_end.isoformat())
    market = market_for_code(candidate.instrument_id)
    if (review.query_stock_code != candidate.instrument_id or
            not review.query_organization_id.strip() or
            not re.fullmatch(r"(?:gss[hz]\d{7}|\d{10})",
                             review.query_organization_id) or
            (review.query_organization_id.startswith("gss") and
             not review.query_organization_id.startswith(
                 "gssh" if market == "sse" else "gssz")) or
            (review.query_period_start, review.query_period_end) !=
            period.publication_window or
            review.query_digest != cninfo_query_digest(
                stock_code=review.query_stock_code,
                organization_id=review.query_organization_id,
                period_start=review.query_period_start,
                period_end=review.query_period_end,
            ) or
            review.search_page_numbers != tuple(range(1, len(review.search_receipt_ids) + 1))):
        raise PdfPromotionError("official index query digest/pages/scope are inconsistent")
    if _aware(review.reviewed_at, "version reviewed_at") > now:
        raise PdfPromotionError("version review cannot be in the future")

    org_receipt = _verified_org_lookup(
        archive, receipt_id=review.org_lookup_receipt_id,
        stock_code=review.query_stock_code,
        organization_id=review.query_organization_id,
    )
    found = []
    evidence_receipts = [org_receipt]
    request_hashes = []
    common_page_size = None
    for page_number, receipt_id in enumerate(review.search_receipt_ids, start=1):
        receipt, payload = _archived_ok(
            archive, receipt_id, expected_host="www.cninfo.com.cn",
        )
        if urlsplit(receipt["url"]).path != "/new/hisAnnouncement/query":
            raise PdfPromotionError("version search receipt is not a CNINFO announcement index")
        body_hash, page_size = _verified_index_request(
            archive, receipt, stock_code=review.query_stock_code,
            organization_id=review.query_organization_id,
            page_number=page_number, category=period.category, market=market,
            period_start=review.query_period_start,
            period_end=review.query_period_end,
            detail_prefix=(f"report-index:{review.query_stock_code}:"
                           f"{candidate.period_end.isoformat()}"),
        )
        if common_page_size is not None and page_size != common_page_size:
            raise PdfPromotionError("index pageSize changes between pages")
        common_page_size = page_size
        request_hashes.append(body_hash)
        try:
            document = json.loads(payload)
            raw_rows = document.get("announcements")
            if not isinstance(raw_rows, list):
                raise PdfPromotionError("CNINFO index rows are missing")
            has_more = document.get("hasMore")
            if isinstance(has_more, str) and has_more.lower() in ("true", "false"):
                has_more = has_more.lower() == "true"
            if not isinstance(has_more, bool) or has_more != (
                page_number < len(review.search_receipt_ids)
            ):
                raise PdfPromotionError("CNINFO index pagination is incomplete or ambiguous")
            announcements = parse_announcements(payload)
            if (len(announcements) != len(raw_rows) or
                    any(ann.sec_code != candidate.instrument_id for ann in announcements)):
                raise PdfPromotionError("CNINFO index contains unparsed or wrong-stock rows")
        except PdfPromotionError:
            raise
        except (ValueError, TypeError, KeyError) as exc:
            raise PdfPromotionError("official announcement index cannot be parsed") from exc
        found.extend((ann, receipt_id) for ann in announcements)
        evidence_receipts.append(receipt)
    review_time = _aware(review.reviewed_at, "version reviewed_at")
    if any(review_time < _aware(datetime.fromisoformat(item["first_seen_at"]),
                                "index first_seen_at") for item in evidence_receipts):
        raise PdfPromotionError("version review predates official index evidence")
    matches = [ann for ann, receipt_id in found
               if ann.announcement_id == review.announcement_id
               and receipt_id == review.index_receipt_id]
    if len(matches) != 1:
        raise PdfPromotionError("exactly one official announcement index row is required")
    ann = matches[0]
    if (ann.sec_code != candidate.instrument_id or
            not review.query_period_start <= ann.announced_on <= review.query_period_end or
            report_period_from_title(ann.title) != candidate.period_end or
            not _same_document_url(ann.detail_url(), candidate.document_url)):
        raise PdfPromotionError("official announcement identity/period/PDF URL mismatch")
    role = disclosure_role(ann.title)
    if role not in (DisclosureRole.ORIGINAL_REPORT, DisclosureRole.REVISED_REPORT):
        raise PdfPromotionError("candidate must be a full original or revised report")
    other_full_reports = [item for item, _receipt_id in found
                          if item.announcement_id != ann.announcement_id
                          and report_period_from_title(item.title) == candidate.period_end
                          and disclosure_role(item.title) in (
                              DisclosureRole.ORIGINAL_REPORT,
                              DisclosureRole.REVISED_REPORT,
                          )]
    if (role is DisclosureRole.ORIGINAL_REPORT and other_full_reports):
        raise PdfPromotionError("original report has another full version in official index")
    if (role is DisclosureRole.REVISED_REPORT and
            any(item.announced_on >= ann.announced_on and
                item.announcement_id != review.predecessor_announcement_id
                for item in other_full_reports)):
        raise PdfPromotionError("revised report is not the latest unambiguous full version")
    if role is DisclosureRole.ORIGINAL_REPORT:
        if (review.predecessor_announcement_id is not None or
                review.correction_receipt_id is not None):
            raise PdfPromotionError("original report cannot claim a predecessor")
    else:
        if (not review.predecessor_announcement_id or
                not review.correction_receipt_id or
                not review.correction_excerpt or
                len(review.correction_excerpt.strip()) < 12):
            raise PdfPromotionError("revised report requires reviewed correction evidence")
        prior = [item for item, _receipt_id in found if
                 item.announcement_id == review.predecessor_announcement_id and
                 item.sec_code == candidate.instrument_id and
                 report_period_from_title(item.title) == candidate.period_end]
        if len(prior) != 1 or prior[0].announced_on > ann.announced_on:
            raise PdfPromotionError("revision predecessor is absent or dated after revision")
        if not review.query_period_start <= prior[0].announced_on <= review.query_period_end:
            raise PdfPromotionError("revision predecessor lies outside attested query period")
        correction, correction_bytes = _archived_ok(
            archive, review.correction_receipt_id, expected_host="static.cninfo.com.cn",
        )
        if not correction_bytes.startswith(b"%PDF-"):
            raise PdfPromotionError("correction evidence is not an archived PDF")
        if review_time < _aware(datetime.fromisoformat(correction["first_seen_at"]),
                                "correction first_seen_at"):
            raise PdfPromotionError("version review predates correction evidence")
        try:
            correction_text = _pdf_text(correction_bytes)
        except Exception as exc:  # noqa: BLE001 - malformed correction PDFs fail closed
            raise PdfPromotionError("correction PDF text cannot be verified") from exc
        if "".join(review.correction_excerpt.split()) not in "".join(correction_text.split()):
            raise PdfPromotionError("reviewed correction excerpt is absent from archived PDF")
        evidence_receipts.append(correction)
    return ann.announced_on, role, evidence_receipts, request_hashes


def _cross_category_evidence(
    archive: ForwardArchive, candidate: CninfoS2CandidateFacts,
    result: CrossCategoryIndexResult,
    dispositions: Sequence[CrossCategoryDisposition], *,
    review: PdfVersionReview, reviewed_by: str, reviewed_at: datetime,
    pdf_receipt_id: str, now: datetime,
) -> tuple[list[dict], list[str], list[dict]]:
    """Replay every archived all-category page and dispose of every candidate.

    Exhaustion describes the response observed at capture.  A query ending on
    the review's Beijing date cannot rule out announcements published later
    that same natural day.
    """

    completed_at = _aware(reviewed_at, "cross-category reviewed_at")
    if not reviewed_by.strip() or completed_at > now:
        raise PdfPromotionError("cross-category review identity/time is invalid")
    period = parse_report_period(candidate.period_end.isoformat())
    market = market_for_code(candidate.instrument_id)
    china_day = completed_at.astimezone(timezone(timedelta(hours=8))).date()
    if (not result.complete or result.error is not None or
            result.termination not in ("has_more_false", "total_reached") or
            result.stock_code != candidate.instrument_id or
            result.organization_id != review.query_organization_id or
            result.report_period != candidate.period_end.isoformat() or
            result.publication_window != (candidate.period_end, china_day) or
            not result.pages or len(result.pages) > 100):
        raise PdfPromotionError("complete all-category index through review day is required")
    org_receipt = _verified_org_lookup(
        archive, receipt_id=result.org_lookup_receipt_id or "",
        stock_code=candidate.instrument_id,
        organization_id=review.query_organization_id,
    )
    if org_receipt["content_hash"] != result.org_lookup_content_hash:
        raise PdfPromotionError("all-category organization lookup hash differs from archive")
    receipts = [org_receipt]
    request_hashes: list[str] = []
    entries: dict[str, CrossCategoryIndexEntry] = {}
    raw_count = 0
    skipped_other = 0
    total: int | None = None
    page_size: int | None = None
    for page_number, page in enumerate(result.pages, start=1):
        if page.page_num != page_number:
            raise PdfPromotionError("all-category page numbers are not consecutive")
        receipt, payload = _archived_ok(
            archive, page.receipt_id, expected_host="www.cninfo.com.cn",
        )
        if (urlsplit(receipt["url"]).path != "/new/hisAnnouncement/query" or
                receipt["content_hash"] != page.content_hash or
                receipt["http_status"] != page.http_status):
            raise PdfPromotionError("all-category response page differs from archive receipt")
        body_hash, size = _verified_index_request(
            archive, receipt, stock_code=candidate.instrument_id,
            organization_id=review.query_organization_id,
            page_number=page_number, category="", market=market,
            period_start=candidate.period_end,
            period_end=result.publication_window[1],
            detail_prefix=(f"cross-category:{candidate.instrument_id}:"
                           f"{candidate.period_end.isoformat()}~"
                           f"{result.publication_window[1].isoformat()}"),
        )
        if body_hash != page.request_body_hash:
            raise PdfPromotionError("all-category page request hash differs from result")
        if page_size is not None and size != page_size:
            raise PdfPromotionError("all-category pageSize changes between pages")
        page_size = size
        request_hashes.append(body_hash)
        receipts.append(receipt)
        try:
            document = json.loads(payload)
            rows = document["announcements"]
            if not isinstance(rows, list):
                raise ValueError("announcements is not a list")
            raw_total = document.get("totalAnnouncement")
            page_total = None if raw_total is None else int(raw_total)
            if (isinstance(raw_total, bool) or
                    (page_total is not None and page_total < 0)):
                raise ValueError("invalid totalAnnouncement")
            has_more = document.get("hasMore")
            if isinstance(has_more, str) and has_more.strip().lower() in ("true", "false"):
                has_more = has_more.strip().lower() == "true"
            elif isinstance(has_more, int) and not isinstance(has_more, bool):
                if has_more not in (0, 1):
                    raise ValueError("invalid hasMore")
                has_more = bool(has_more)
            if has_more is not None and not isinstance(has_more, bool):
                raise ValueError("invalid hasMore")
        except (TypeError, ValueError, KeyError, OverflowError) as exc:
            raise PdfPromotionError("all-category response page cannot be parsed") from exc
        if (page.raw_announcement_count != len(rows) or
                page.total_announcement != page_total or
                page.has_more != has_more):
            raise PdfPromotionError("all-category result page metadata differs from archive")
        raw_count += len(rows)
        if page_total is not None:
            if total is not None and total != page_total:
                raise PdfPromotionError("all-category totalAnnouncement changes between pages")
            total = page_total
        if page_number < len(result.pages) and has_more is not True:
            raise PdfPromotionError("all-category pagination ended before next archived page")
        if page_number == len(result.pages):
            if has_more is not False and not (has_more is None and total == raw_count):
                raise PdfPromotionError("all-category last page does not prove exhaustion")
            if result.termination != (
                "has_more_false" if has_more is False else "total_reached"
            ):
                raise PdfPromotionError("all-category termination differs from archived last page")
            if total is not None and total != raw_count:
                raise PdfPromotionError("all-category totalAnnouncement does not match rows")
        for row in rows:
            if not isinstance(row, dict):
                raise PdfPromotionError("all-category row is not an object")
            code = str(row.get("secCode") or "").strip()
            if not re.fullmatch(r"\d{6}", code):
                raise PdfPromotionError("all-category row lacks a valid security code")
            if code != candidate.instrument_id:
                skipped_other += 1
                continue
            ann_id = str(row.get("announcementId") or "").strip()
            title = normalize_title(row.get("announcementTitle"))
            try:
                millis = int(row.get("announcementTime"))
            except (TypeError, ValueError, OverflowError) as exc:
                raise PdfPromotionError("all-category row lacks a valid announcement time") from exc
            adjunct = str(row.get("adjunctUrl") or "").strip()
            parts = urlsplit(adjunct)
            if (not ann_id or not title or millis <= 0 or
                    not adjunct or parts.scheme or parts.netloc or parts.query):
                raise PdfPromotionError("all-category row identity or document path is incomplete")
            announced = datetime.fromtimestamp(
                millis / 1000, tz=timezone.utc,
            ).astimezone(timezone(timedelta(hours=8))).date()
            if not candidate.period_end <= announced <= result.publication_window[1]:
                raise PdfPromotionError("all-category announcement is outside query dates")
            is_correction, is_revision, is_supplement, _ = title_revision_flags(title)
            reasons = tuple(name for name, active in (
                ("period_title", title_matches_period(title, period)),
                ("correction_title", is_correction),
                ("revision_title", is_revision),
                ("supplement_title", is_supplement),
                ("accounting_restatement_title", any(term in title for term in (
                    "会计差错", "追溯调整", "重述"))),
            ) if active)
            if ann_id in entries:
                raise PdfPromotionError("all-category announcement repeats across pages")
            entries[ann_id] = CrossCategoryIndexEntry(
                announcement_id=ann_id, sec_code=code, title=title,
                announcement_time_ms=millis,
                document_url=f"https://static.cninfo.com.cn/{adjunct.lstrip('/')}",
                candidate_reasons=reasons, page_num=page_number,
                page_receipt_id=receipt["receipt_id"],
                page_content_hash=receipt["content_hash"],
            )
    if (len(result.announcements) != len(entries) or
            result.total_announcement != total or
            result.skipped_different_security != skipped_other or
            {entry.announcement_id: entry for entry in result.announcements} != entries):
        raise PdfPromotionError("all-category index result differs from archived pages")
    if completed_at < max(_aware(datetime.fromisoformat(row["first_seen_at"]),
                                 "all-category first_seen_at") for row in receipts):
        raise PdfPromotionError("all-category review predates archived index evidence")

    candidate_ids = {entry.announcement_id for entry in entries.values()
                     if entry.candidate_reasons}
    by_id = {item.announcement_id: item for item in dispositions}
    if (len(dispositions) != len(by_id) or set(by_id) != candidate_ids):
        raise PdfPromotionError("every all-category candidate needs one explicit disposition")
    required_report_ids = {review.announcement_id}
    if review.predecessor_announcement_id is not None:
        required_report_ids.add(review.predecessor_announcement_id)
    if (not required_report_ids.issubset(candidate_ids) or
            any(by_id[ann_id].relevance != "RELATED" for ann_id in required_report_ids)):
        raise PdfPromotionError("promoted report and predecessor must be related index candidates")
    if (review.correction_receipt_id is not None and
            not any(item.relevance == "RELATED" and
                    item.document_receipt_id == review.correction_receipt_id
                    for item in dispositions)):
        raise PdfPromotionError("correction PDF must be a related all-category candidate")
    disposition_evidence: list[dict] = []
    for ann_id in sorted(candidate_ids):
        entry = entries[ann_id]
        item = by_id[ann_id]
        disposition_time = _aware(item.reviewed_at, "disposition reviewed_at")
        page_receipt = next(row for row in receipts
                            if row["receipt_id"] == entry.page_receipt_id)
        page_seen = _aware(datetime.fromisoformat(page_receipt["first_seen_at"]),
                           "candidate first_seen_at")
        if (not item.reviewer_id.strip() or not item.rationale.strip() or
                disposition_time < page_seen or disposition_time > completed_at):
            raise PdfPromotionError("all-category candidate disposition lacks review evidence")
        if item.relevance == "UNRELATED":
            if (ann_id in {review.announcement_id,
                           review.predecessor_announcement_id} or
                    len(item.rationale.strip()) < 12 or
                    item.related_report_announcement_ids):
                raise PdfPromotionError("unrelated candidate needs a traceable review rationale")
        elif item.relevance == "RELATED":
            if not item.document_receipt_id or not item.related_report_announcement_ids:
                raise PdfPromotionError("related candidate needs its PDF and report relation")
            document_receipt, document_bytes = _archived_ok(
                archive, item.document_receipt_id,
                expected_url=entry.document_url,
                expected_host="static.cninfo.com.cn",
            )
            document_seen = _aware(datetime.fromisoformat(document_receipt["first_seen_at"]),
                                   "related document first_seen_at")
            if document_seen > disposition_time or not document_bytes.startswith(b"%PDF-"):
                raise PdfPromotionError("related candidate PDF was not reviewed after capture")
            receipts.append(document_receipt)
            if ann_id == review.announcement_id and item.document_receipt_id != pdf_receipt_id:
                raise PdfPromotionError("active report disposition must bind promoted PDF")
            needs_relation = any(reason != "period_title" for reason in entry.candidate_reasons)
            if needs_relation:
                relation_ids = set(item.related_report_announcement_ids)
                required_ids = {review.announcement_id}
                if review.predecessor_announcement_id is not None:
                    required_ids.add(review.predecessor_announcement_id)
                if not required_ids.issubset(relation_ids):
                    raise PdfPromotionError("related notice omits the reviewed report relation")
                if not item.relation_receipt_id or not item.relation_excerpt:
                    raise PdfPromotionError("related notice lacks correction PDF text evidence")
                if (ann_id == review.announcement_id and
                        review.correction_receipt_id is not None and
                        item.relation_receipt_id != review.correction_receipt_id):
                    raise PdfPromotionError("active revision uses conflicting correction evidence")
                relation_receipt, relation_bytes = _archived_ok(
                    archive, item.relation_receipt_id,
                    expected_host="static.cninfo.com.cn",
                )
                if (not relation_bytes.startswith(b"%PDF-") or
                        _aware(datetime.fromisoformat(relation_receipt["first_seen_at"]),
                               "relation first_seen_at") > disposition_time):
                    raise PdfPromotionError("relation PDF was not reviewed after capture")
                try:
                    text = _pdf_text(relation_bytes)
                except Exception as exc:  # noqa: BLE001 - PDF failure closes admission
                    raise PdfPromotionError("relation PDF text cannot be verified") from exc
                if "".join(item.relation_excerpt.split()) not in "".join(text.split()):
                    raise PdfPromotionError("relation excerpt is absent from archived PDF")
                receipts.append(relation_receipt)
            elif ann_id not in item.related_report_announcement_ids:
                raise PdfPromotionError("full report disposition does not identify itself")
        else:
            raise PdfPromotionError("all-category candidate relevance must be explicit")
        disposition_evidence.append({
            "announcement_id": ann_id, "title": entry.title,
            "page_receipt_id": entry.page_receipt_id,
            "candidate_reasons": list(entry.candidate_reasons),
            "relevance": item.relevance, "rationale": item.rationale,
            "reviewer_id": item.reviewer_id,
            "reviewed_at": item.reviewed_at.isoformat(),
            "document_receipt_id": item.document_receipt_id,
            "related_report_announcement_ids": list(item.related_report_announcement_ids),
            "relation_receipt_id": item.relation_receipt_id,
            "relation_excerpt": item.relation_excerpt,
        })
    return receipts, request_hashes, disposition_evidence


def _next_available_snapshot(after: datetime, calendar: Sequence[date]) -> datetime:
    if not calendar or tuple(calendar) != tuple(sorted(set(calendar))):
        raise PdfPromotionError("trading calendar must be nonempty, unique and sorted")
    for day in calendar:
        instant = preopen_instant(day)
        if instant >= after:
            return instant
    raise PdfPromotionError("trading calendar does not reach the next available snapshot")


def promote_reviewed_pdf_bundle(
    *, candidate: CninfoS2CandidateFacts, pdf_receipt_id: str,
    version_review: PdfVersionReview, field_reviews: Sequence[PdfFieldReview],
    cross_category_index: CrossCategoryIndexResult,
    cross_category_dispositions: Sequence[CrossCategoryDisposition],
    cross_category_reviewed_by: str, cross_category_reviewed_at: datetime,
    archive: ForwardArchive, repository: FinancialFactRepository,
    trading_calendar: Sequence[date], rights: RightsRegistry | None = None,
) -> tuple[FinancialFact, ...]:
    """Admit one reviewed five-field PDF report as five forward-only PIT facts.

    The resulting ``available_at`` is the first future 08:45 Shanghai decision
    snapshot after every required observation/review/ingestion and the
    publication-date floor.  No historical availability is reconstructed.
    Archived POST bodies and response receipts prove the pagination query;
    ``complete_search_attested`` separately records the human conclusion that
    the candidate's revision relationship was reviewed.
    """

    now = _utc_now()
    if not isinstance(cross_category_index, CrossCategoryIndexResult):
        raise PdfPromotionError("complete all-category index is required before admission")
    if cross_category_dispositions is None:
        raise PdfPromotionError("all-category candidate dispositions are required")
    rights_entry = (rights or default_rights()).get("cninfo")
    if not (rights_entry.open_for("research_use") and
            rights_entry.open_for("local_storage")):
        raise PdfPromotionError("CNINFO research and local-storage rights must be allowed")
    if candidate.pit_eligible or candidate.review_status != "requires_manual_verification":
        raise PdfPromotionError("only unpromoted, review-required PDF candidates are accepted")
    pdf_receipt, payload = _archived_ok(
        archive, pdf_receipt_id, expected_url=candidate.document_url,
        expected_host="static.cninfo.com.cn",
    )
    if pdf_receipt["content_hash"] != candidate.pdf_sha256:
        raise PdfPromotionError("candidate PDF hash differs from archive receipt")
    # Re-extract from archived bytes; a hand-built candidate object alone is not
    # evidence.  The extractor's SHA allowlist and statement layout checks run
    # again at the admission boundary.
    extracted = extract_s2_candidate_facts(
        payload, instrument_id=candidate.instrument_id,
        period_end=candidate.period_end,
    )
    if extracted != candidate:
        raise PdfPromotionError("candidate facts differ from archived PDF extraction")

    first_seen = _aware(datetime.fromisoformat(pdf_receipt["first_seen_at"]), "first_seen_at")
    if first_seen > now:
        raise PdfPromotionError("PDF cannot be observed after admission")
    _check_reviews(candidate, field_reviews, first_seen=first_seen, now=now)
    published_on, role, index_receipts, request_hashes = _version_evidence(
        archive, candidate, version_review, now=now,
    )
    cross_receipts, cross_request_hashes, cross_dispositions = _cross_category_evidence(
        archive, candidate, cross_category_index, cross_category_dispositions,
        review=version_review, reviewed_by=cross_category_reviewed_by,
        reviewed_at=cross_category_reviewed_at, pdf_receipt_id=pdf_receipt_id,
        now=now,
    )
    evidence_seen = [first_seen]
    for row in (*index_receipts, *cross_receipts):
        seen = _aware(datetime.fromisoformat(row["first_seen_at"]), "evidence first_seen_at")
        if seen > now:
            raise PdfPromotionError("version evidence cannot be observed after admission")
        evidence_seen.append(seen)
    review_times = [_aware(r.reviewed_at, "reviewed_at") for r in field_reviews]
    review_times.append(_aware(version_review.reviewed_at, "version reviewed_at"))
    review_times.append(_aware(cross_category_reviewed_at,
                               "cross-category reviewed_at"))
    try:
        date_floor, _ = date_only_available_at(published_on, trading_calendar)
    except ValueError as exc:
        raise PdfPromotionError("trading calendar lacks publication-date floor") from exc
    available_at = _next_available_snapshot(
        max(date_floor, *evidence_seen, *review_times, now), trading_calendar,
    )

    previous_by_field: dict[str, FinancialFact] = {}
    selected = repository.load().select(  # Avoid a second private DB protocol.
        datetime.max.replace(tzinfo=timezone.utc),
        instrument_id=candidate.instrument_id, period_end=candidate.period_end,
        formal=False,
    )
    for fact in selected:
        if fact.metric in _FIELDS:
            previous_by_field[fact.metric] = fact
    if role is DisclosureRole.ORIGINAL_REPORT:
        if previous_by_field:
            raise PdfPromotionError("an original report would create a second fact root")
    elif (set(previous_by_field) != _FIELDS or
          {fact.source_document_id for fact in previous_by_field.values()} !=
          {version_review.predecessor_announcement_id} or
          {fact.source_id for fact in previous_by_field.values()} != {"cninfo"}):
        raise PdfPromotionError("verified five-field predecessor bundle is required")

    facts = []
    for field in sorted(_FIELDS):
        item = candidate.by_field[field]
        profit_scope = (
            ProfitScope.ATTRIBUTABLE if field == "net_profit_attributable" else
            ProfitScope.CONSOLIDATED if field == "net_profit_consolidated" else None
        )
        identity = (f"cninfo|{candidate.instrument_id}|{candidate.period_end}|"
                    f"{field}|{version_review.announcement_id}|{candidate.pdf_sha256}")
        facts.append(FinancialFact(
            instrument_id=candidate.instrument_id, metric=field,
            period_end=candidate.period_end,
            statement_scope=StatementScope.CONSOLIDATED, profit_scope=profit_scope,
            value=item.value_yuan, currency="CNY", raw_unit="yuan",
            source_id="cninfo", source_document_id=version_review.announcement_id,
            source_published_date=published_on, source_published_at=None,
            timestamp_precision=TimestampPrecision.DATE,
            first_seen_at=first_seen, ingested_at=now, available_at=available_at,
            availability_basis=AvailabilityBasis.OBSERVED,
            pit_mode=PitMode.LIVE_OBSERVED, content_hash=candidate.pdf_sha256,
            version_id="pdf_" + hashlib.sha256(identity.encode()).hexdigest()[:32],
            supersedes_id=(previous_by_field[field].version_id
                           if role is DisclosureRole.REVISED_REPORT else None),
        ))

    evidence = {
        "schema": "cninfo-s2-pdf-review-v1",
        "source_id": "cninfo",
        "rights_register_version": REGISTER_VERSION,
        "instrument_id": candidate.instrument_id,
        "period_end": candidate.period_end.isoformat(),
        "announcement_id": version_review.announcement_id,
        "announcement_role": role.value,
        "source_published_date": published_on.isoformat(),
        "document_url": candidate.document_url,
        "content_hash": candidate.pdf_sha256,
        "pdf_receipt_id": pdf_receipt_id,
        "index_receipt_ids": list(version_review.search_receipt_ids),
        "org_lookup_receipt_id": version_review.org_lookup_receipt_id,
        "index_request_body_hashes": request_hashes,
        "cross_category_index": {
            "org_lookup_receipt_id": cross_category_index.org_lookup_receipt_id,
            "page_receipt_ids": [page.receipt_id for page in cross_category_index.pages],
            "request_body_hashes": cross_request_hashes,
            "index_observed_as_of_at": max(
                datetime.fromisoformat(item["first_seen_at"])
                for item in cross_receipts[:1 + len(cross_category_index.pages)]
            ).isoformat(),
            "publication_window": [day.isoformat()
                                   for day in cross_category_index.publication_window],
            "candidate_dispositions": cross_dispositions,
            "reviewer_id": cross_category_reviewed_by,
            "reviewed_at": cross_category_reviewed_at.isoformat(),
        },
        "index_page_numbers": list(version_review.search_page_numbers),
        "index_query": {
            "stock_code": version_review.query_stock_code,
            "organization_id": version_review.query_organization_id,
            "period_start": version_review.query_period_start.isoformat(),
            "period_end": version_review.query_period_end.isoformat(),
            "digest": version_review.query_digest,
            "request_body_verified": True,
        },
        "complete_search_attested": version_review.complete_search_attested,
        "version_reviewer_id": version_review.reviewer_id,
        "version_reviewed_at": version_review.reviewed_at.isoformat(),
        "predecessor_announcement_id": version_review.predecessor_announcement_id,
        "correction_receipt_id": version_review.correction_receipt_id,
        "correction_excerpt": version_review.correction_excerpt,
        "field_reviews": [
            {"field": review.field, "value_yuan": str(review.reviewed_value_yuan),
             "pdf_page": review.pdf_page, "current_cell": review.current_cell,
             "row_label": review.row_label,
             "source_amount_unit": review.source_amount_unit,
             "reviewer_id": review.reviewer_id,
             "reviewed_at": review.reviewed_at.isoformat(), "method": review.method}
            for review in sorted(field_reviews, key=lambda r: r.field)
        ],
        "first_seen_at": first_seen.isoformat(), "ingested_at": now.isoformat(),
        "available_at": available_at.isoformat(),
        "version_ids": [fact.version_id for fact in facts],
    }
    evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
    bundle_id = ("cninfo_pdf_" + hashlib.sha256(
        f"{candidate.instrument_id}|{candidate.period_end}|{version_review.announcement_id}"
        .encode(),
    ).hexdigest()[:32])
    repository.append_many(facts, review_bundle=FactReviewBundle(bundle_id, evidence_json))
    return tuple(facts)


__all__ = [
    "PdfFieldReview", "PdfPromotionError", "PdfVersionReview",
    "cninfo_query_digest", "promote_reviewed_pdf_bundle",
]
