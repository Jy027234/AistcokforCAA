"""Bounded canonical source packages for four audited CNINFO edge cases.

This is a separate, versioned review protocol.  It replays the archived report
and company indexes, source PDFs, rights, and trading calendar on both write and
read.  It does not reuse the normal-current-column or no-effect meanings for
periods whose source column or revision package has different semantics.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import fields, is_dataclass, replace
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Mapping, Sequence
from urllib.parse import urlsplit

from aquant.artifact_paths import resolve_archive_member, resolve_artifact_path
from aquant.domain.data.db import connect
from aquant.domain.data.pit import (
    AvailabilityBasis, PitMode, TimestampPrecision, date_only_available_at,
    preopen_instant,
)
from aquant.domain.data.rights import REGISTER_VERSION, default_rights
from aquant.domain.fundamentals.fact_repository import (
    FactReviewBundle, FinancialFactRepository,
)
from aquant.domain.fundamentals.versioned import (
    FinancialFact, ProfitScope, StatementScope,
)
from aquant.domain.fundamentals.disclosure_link import is_financial_correction_title
from aquant.adapters.providers.cninfo import (
    Announcement, CrossCategoryIndexEntry, CrossCategoryIndexResult,
    ReportIndexMatch, ReportIndexPage, ReportIndexResult,
)
from aquant.adapters.providers.cninfo_probe import (
    market_for_code, parse_report_period, title_matches_period, title_revision_flags,
)
from aquant.adapters.providers.pdf_promotion import (
    CrossCategoryDisposition, PdfVersionReview, _cross_category_evidence, _version_evidence,
    _same_document_url,
    cninfo_query_digest,
)
from aquant.adapters.providers.s2_calendar_evidence import verify_calendar_evidence


SCHEMA = "cninfo-s2-canonical-report-package-v1"
VERIFIER_ID = "machine:cninfo-canonical-report-package-v1"
VERIFIER_VERSION = "canonical-report-package-v1"
BUNDLE_PREFIX = "cninfo_machine_canonical_package_"
_BEIJING = timezone(timedelta(hours=8))
_FIELDS = {
    "TIANSHAN_AUDIT_2025": ("revenue", "net_profit_attributable", "net_profit_consolidated", "operating_cashflow"),
    "GANNENG_H125_RESTATED_PRIOR": ("revenue", "net_profit_attributable", "net_profit_consolidated", "operating_cashflow", "parent_equity"),
    "YIHUA_H126_CANONICAL_CURRENT": ("parent_equity",),
    "XIBU_FY25_FIRST_ANNUAL": ("net_profit_consolidated",),
}
_CASES = {
    "TIANSHAN_AUDIT_2025": {
        "instrument_id": "002532", "company": "天山铝业集团股份有限公司",
        "fact_period_end": "2025-12-31", "source_report_period_end": "2025-12-31",
        "column_role": "CURRENT", "canonical_report_id": "1225041399",
        "primary_document_id": "1225041401", "companion_ids": ("1225041401",),
        "cause_ids": (), "source_ids": ("1225041399", "1225041401"),
    },
    "GANNENG_H125_RESTATED_PRIOR": {
        "instrument_id": "000899", "company": "江西赣能股份有限公司",
        "fact_period_end": "2025-06-30", "source_report_period_end": "2026-06-30",
        "column_role": "PRESENTED_PRIOR", "canonical_report_id": "1225490300",
        "primary_document_id": "1225490300", "companion_ids": (),
        "cause_ids": ("1223101826", "1225490303", "1225619745"),
        "source_ids": ("1225490300", "1223101826", "1225490303", "1225619745"),
    },
    "YIHUA_H126_CANONICAL_CURRENT": {
        "instrument_id": "000422", "company": "湖北宜化化工股份有限公司",
        "fact_period_end": "2026-06-30", "source_report_period_end": "2026-06-30",
        "column_role": "CURRENT", "canonical_report_id": "1225468007",
        "primary_document_id": "1225468007", "companion_ids": ("1225183616",),
        "cause_ids": ("1222991641", "1222991666", "1225183650", "1225183652"),
        "source_ids": ("1225468007", "1225183616", "1222991641", "1222991666", "1225183650", "1225183652"),
    },
    "XIBU_FY25_FIRST_ANNUAL": {
        "instrument_id": "601069", "company": "西部黄金股份有限公司",
        "fact_period_end": "2025-12-31", "source_report_period_end": "2025-12-31",
        "column_role": "CURRENT", "canonical_report_id": "1225082554",
        "primary_document_id": "1225082554", "companion_ids": ("1225082568",),
        "cause_ids": ("1225082555", "1225082560", "1225082606"),
        "source_ids": ("1225082554", "1225082555", "1225082560", "1225082606", "1225082568"),
    },
}
_SOURCE_HASHES = {
    "1225041399": "sha256:157fc338eee0e4e9b5cbd8c3d57994bb1f0323fadd49611230f5700a510cb419",
    "1225041401": "sha256:03b73777aaa647123ae629aa3ae1307b098290f271c016fb6966d9ccf77e7082",
    "1225490300": "sha256:0908a0080bdd676f3fbddcd3180cf4b3a91c32c980fd3b7643b313972f7587b8",
    "1225490303": "sha256:0b75d933852173488cba6234372688137dd2f7d23a0fff44c87ab5024b17bd45",
    "1225619745": "sha256:eb7fc614c808bd26667baad8f963c5baebddd3e1e48a9c74225f066927fee040",
    "1223101826": "sha256:96d6449988010bf69ba24e5b38c591ee1113ae906aa33d199234ef509d6c4831",
    "1225468007": "sha256:6963a7b5ca322c8fe53d59eafe383bb882689ad9e2a9b78aa669373f58de4e12",
    "1225183616": "sha256:13067dd7ca378659a5d01a6600c911dda9335afc970ac10f554dfd79a945987f",
    "1222991641": "sha256:9378ef0cd160da7ead1881ca1e4db5e50b00d5c45db6c0f773eab2a12406743a",
    "1222991666": "sha256:18a7701e0d52d83f6c5d86eb4dab7c55c4be3af2283c30bf8a3ae1c9f135c207",
    "1225183650": "sha256:f46e817c54a818eae59a42579b4a0f5d8bef85707473d441c9a339e735168cb0",
    "1225183652": "sha256:b090c18e1fae1a1335140676251959070a262d880d18cd10e1d15b639163fac1",
    "1225082554": "sha256:dd14bb62f8d912618a937251f875853aea1368f21fe1cce8462d183e886cb518",
    "1225082555": "sha256:b963f3f01e773dedfdedf7e9fab6e79259be45dc029b6d00ec7a195b9ebffeef",
    "1225082560": "sha256:ad2fb88d66457c794f32d15ed80e274815ed1d7d48fc3f87932c0d7063e760e6",
    "1225082606": "sha256:9dc75e2b7697cb2a40118036e73de66b4ad795b76488e1422ec332863ead097a",
    "1225082568": "sha256:12b0b7362275c7ab76f763a98fac036f0b75bc9d13b4f3ad4946bb56478126d6",
}


class CanonicalPackageError(ValueError):
    """Archived sources or the bounded source package do not close."""


def _jsonable(value):
    if is_dataclass(value):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def _date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _tuple_dates(value) -> tuple[date, ...]:
    return tuple(_date(item) for item in value)


def _regular(value) -> ReportIndexResult:
    if isinstance(value, ReportIndexResult):
        value = _jsonable(value)
    if not isinstance(value, Mapping):
        raise CanonicalPackageError("regular index context must be a frozen result or mapping")
    pages = tuple(ReportIndexPage(**item) for item in value.get("pages", ()))
    matches = []
    for item in value.get("matches", ()):
        announcement = item.get("announcement")
        if isinstance(announcement, Mapping):
            announcement = dict(announcement)
            if "announced_on" not in announcement:
                if announcement.get("announcement_date_cn"):
                    announcement["announced_on"] = _date(announcement["announcement_date_cn"])
                else:
                    announcement["announced_on"] = datetime.fromtimestamp(
                        int(announcement["announcement_time_ms"]) / 1000, timezone.utc,
                    ).astimezone(_BEIJING).date()
            else:
                announcement["announced_on"] = _date(announcement["announced_on"])
            if "adjunct_url" not in announcement:
                adjunct = announcement.get("adjunct_path")
                indexed_url = announcement.get("url")
                announcement["adjunct_url"] = adjunct or (urlsplit(str(indexed_url)).path if indexed_url else None)
            if "announcement_type" not in announcement:
                announcement["announcement_type"] = announcement.get("report_kind")
            announcement = Announcement(
                announcement_id=str(announcement["announcement_id"]),
                sec_code=str(announcement.get("sec_code", announcement.get("instrument_id", ""))),
                sec_name=str(announcement.get("sec_name", "")), title=str(announcement["title"]),
                announced_on=announcement["announced_on"], adjunct_url=announcement.get("adjunct_url"),
                announcement_type=announcement.get("announcement_type"), raw=dict(announcement.get("raw", {})),
            )
        matches.append(ReportIndexMatch(
            announcement=announcement, page_num=int(item["page_num"]),
            page_receipt_id=str(item["page_receipt_id"]),
            page_content_hash=str(item["page_content_hash"]),
        ))
    return ReportIndexResult(
        stock_code=str(value["stock_code"]), organization_id=value.get("organization_id"),
        report_period=str(value["report_period"]), market=str(value["market"]),
        category=str(value["category"]), publication_window=_tuple_dates(value["publication_window"]),
        org_lookup_receipt_id=value.get("org_lookup_receipt_id"),
        org_lookup_content_hash=value.get("org_lookup_content_hash"), pages=pages,
        matches=tuple(matches), skipped=dict(value.get("skipped", {})),
        total_announcement=value.get("total_announcement"), complete=bool(value["complete"]),
        termination=str(value["termination"]), error=value.get("error"),
    )


def _cross(value, source_period: str) -> CrossCategoryIndexResult:
    if isinstance(value, CrossCategoryIndexResult):
        result = value
    elif isinstance(value, Mapping):
        pages = tuple(ReportIndexPage(**item) for item in value.get("pages", ()))
        entries = tuple(CrossCategoryIndexEntry(
            announcement_id=str(item["announcement_id"]), sec_code=str(item["sec_code"]),
            title=str(item["title"]), announcement_time_ms=int(item["announcement_time_ms"]),
            document_url=str(item["document_url"]),
            candidate_reasons=tuple(item.get("candidate_reasons", ())),
            page_num=int(item["page_num"]), page_receipt_id=str(item["page_receipt_id"]),
            page_content_hash=str(item["page_content_hash"]),
        ) for item in value.get("announcements", ()))
        result = CrossCategoryIndexResult(
            stock_code=str(value["stock_code"]), organization_id=value.get("organization_id"),
            report_period=str(value["report_period"]),
            publication_window=_tuple_dates(value["publication_window"]),
            org_lookup_receipt_id=value.get("org_lookup_receipt_id"),
            org_lookup_content_hash=value.get("org_lookup_content_hash"), pages=pages,
            announcements=entries, skipped_different_security=int(value.get("skipped_different_security", 0)),
            total_announcement=value.get("total_announcement"), complete=bool(value["complete"]),
            termination=str(value["termination"]), error=value.get("error"),
        )
    else:
        raise CanonicalPackageError("all-category index context must be a frozen result or mapping")
    period = parse_report_period(source_period)
    entries = []
    for entry in result.announcements:
        correction, revision, supplement, _ = title_revision_flags(entry.title)
        reasons = tuple(name for name, active in (
            ("period_title", title_matches_period(entry.title, period)),
            ("correction_title", correction), ("revision_title", revision),
            ("supplement_title", supplement),
            ("accounting_restatement_title", any(term in entry.title for term in ("会计差错", "追溯调整", "重述"))),
        ) if active)
        entries.append(replace(entry, candidate_reasons=reasons))
    return replace(result, report_period=source_period, announcements=tuple(entries))


def _context(index_context, source_period: str) -> tuple[ReportIndexResult, CrossCategoryIndexResult, dict]:
    if not isinstance(index_context, Mapping):
        raise CanonicalPackageError("index_context must contain the frozen regular and all-category results")
    regular = _regular(index_context.get("regular", index_context.get("report_index")))
    cross = _cross(index_context.get("cross_category", index_context.get("all_category")), source_period)
    if regular.report_period != source_period:
        raise CanonicalPackageError("regular report index period is not the source report period")
    if (regular.stock_code != cross.stock_code or regular.organization_id != cross.organization_id or
            not regular.complete or regular.error is not None or not cross.complete or cross.error is not None):
        raise CanonicalPackageError("regular/all-category index identities or completeness differ")
    packet = {"regular": _jsonable(regular), "cross_category": _jsonable(cross)}
    return regular, cross, packet


class _ReadOnlyArchive:
    """Read-only structural subset of ForwardArchive; never runs DDL or mkdir."""
    def __init__(self, con: sqlite3.Connection, root: str | Path):
        self.con = con
        self.root = resolve_artifact_path(root).resolve()

    def load_bytes(self, content_hash: str) -> bytes:
        row = self.con.execute(
            "SELECT stored_path,byte_size FROM raw_artifact WHERE content_hash=?", (content_hash,)
        ).fetchone()
        if row is None:
            raise KeyError(content_hash)
        payload = resolve_archive_member(self.root, row["stored_path"]).read_bytes()
        if (len(payload) != row["byte_size"] or
                "sha256:" + hashlib.sha256(payload).hexdigest() != content_hash):
            raise CanonicalPackageError("archived raw bytes fail their stored hash/size")
        return payload

    def verify(self, content_hash: str) -> bool:
        try:
            self.load_bytes(content_hash)
            return True
        except (KeyError, OSError, ValueError):
            return False


def _archive_binding(archive) -> dict[str, str]:
    try:
        root = Path(archive.root).resolve()
        row = next(item for item in archive.con.execute("PRAGMA database_list") if item[1] == "main")
        database = Path(row[2]).resolve()
    except (AttributeError, OSError, StopIteration, TypeError) as exc:
        raise CanonicalPackageError("archive must expose its actual root and main SQLite path") from exc
    if database != (root / "meta.sqlite").resolve():
        raise CanonicalPackageError("archive database must be the exact meta.sqlite under its root")
    return {"root": str(root), "database_path": str(database)}


def _source_index_record(regular: ReportIndexResult, cross: CrossCategoryIndexResult, announcement_id: str) -> dict:
    hits = []
    for match in regular.matches:
        ann = match.announcement
        if ann.announcement_id == announcement_id:
            hits.append({"announcement_id": ann.announcement_id, "sec_code": ann.sec_code,
                         "title": ann.title, "published_date": ann.announced_on.isoformat(),
                         "url": (ann.detail_url() or "").replace("http://", "https://", 1), "page_num": match.page_num,
                         "page_receipt_id": match.page_receipt_id})
    for entry in cross.announcements:
        if entry.announcement_id == announcement_id:
            published = datetime.fromtimestamp(entry.announcement_time_ms / 1000, timezone.utc).astimezone(_BEIJING).date()
            hits.append({"announcement_id": entry.announcement_id, "sec_code": entry.sec_code,
                         "title": entry.title, "published_date": published.isoformat(),
                         "url": entry.document_url, "page_num": entry.page_num,
                         "page_receipt_id": entry.page_receipt_id})
    if not hits:
        raise CanonicalPackageError(f"required source {announcement_id} is absent from the archived company index")
    normalized = {(item["sec_code"], item["title"], item["published_date"], item["url"]) for item in hits}
    if len(normalized) != 1:
        raise CanonicalPackageError(f"source {announcement_id} has conflicting index identities")
    return hits[0]


def _source_document(archive, record: Mapping[str, object], *, expected_code: str,
                     observed_at: datetime | None = None) -> tuple[dict, bytes]:
    ann_id = str(record["announcement_id"])
    url = str(record["url"] or "")
    parts = urlsplit(url)
    if (record["sec_code"] != expected_code or parts.scheme != "https" or
            parts.hostname != "static.cninfo.com.cn" or parts.query or parts.fragment or
            not re.search(rf"/{re.escape(ann_id)}\.PDF\Z", parts.path, flags=re.IGNORECASE)):
        raise CanonicalPackageError(f"official index source {ann_id} has an invalid CNINFO identity or URL")
    rows = archive.con.execute(
        "SELECT * FROM fetch_receipt WHERE source_id='cninfo' AND url=? AND http_status=200 AND outcome='OK' ORDER BY first_seen_at,receipt_id",
        (url,),
    ).fetchall()
    if not rows:
        raise CanonicalPackageError(f"official PDF receipt for {ann_id} is missing")
    parsed = [dict(row) for row in rows]
    if observed_at is not None:
        parsed = [row for row in parsed
                  if datetime.fromisoformat(row["first_seen_at"]) <= observed_at]
    if not parsed:
        raise CanonicalPackageError(f"official PDF receipt for {ann_id} was not observed at this review")
    hashes = {row["content_hash"] for row in parsed}
    if len(hashes) != 1:
        raise CanonicalPackageError(f"official PDF URL {ann_id} has conflicting archived bytes")
    chosen = parsed[0]
    payload = archive.load_bytes(chosen["content_hash"])
    if (not payload.startswith(b"%PDF-") or len(payload) != chosen["byte_size"] or
            "sha256:" + hashlib.sha256(payload).hexdigest() != chosen["content_hash"]):
        raise CanonicalPackageError(f"official PDF {ann_id} is not a complete PDF")
    try:
        first_seen = datetime.fromisoformat(chosen["first_seen_at"])
        requested = datetime.fromisoformat(chosen["requested_at"])
    except (TypeError, ValueError) as exc:
        raise CanonicalPackageError(f"official PDF {ann_id} has invalid receipt time") from exc
    if first_seen.tzinfo is None or requested.tzinfo is None or first_seen < requested:
        raise CanonicalPackageError(f"official PDF {ann_id} receipt time is invalid")
    source = {
        **dict(record), "receipt_id": chosen["receipt_id"],
        "receipt_ids": [row["receipt_id"] for row in parsed],
        "content_hash": chosen["content_hash"], "byte_size": chosen["byte_size"],
        "first_seen_at": first_seen.isoformat(), "source_id": "cninfo",
        "http_status": 200, "outcome": "OK",
    }
    return source, payload


def _verify_regular_sources(archive, regular: ReportIndexResult, source_ids: Sequence[str], *, source_period: str) -> None:
    """Verify every cited regular-index identity against its archived response bytes."""
    from aquant.adapters.providers.pdf_promotion import _archived_ok, _verified_index_request

    if regular.category == "" or not regular.pages or regular.termination not in ("has_more_false", "total_reached"):
        raise CanonicalPackageError("regular index pagination is incomplete")
    period = parse_report_period(source_period)
    market = market_for_code(regular.stock_code)
    found: dict[str, list[tuple[object, str]]] = {ann: [] for ann in source_ids}
    for number, page in enumerate(regular.pages, 1):
        if page.page_num != number:
            raise CanonicalPackageError("regular index pages are not consecutive")
        receipt, payload = _archived_ok(archive, page.receipt_id, expected_host="www.cninfo.com.cn")
        if receipt["content_hash"] != page.content_hash or receipt["http_status"] != page.http_status:
            raise CanonicalPackageError("regular index page differs from its archive receipt")
        body_hash, _ = _verified_index_request(
            archive, receipt, stock_code=regular.stock_code, organization_id=regular.organization_id or "",
            page_number=number, category=regular.category, market=market,
            period_start=regular.publication_window[0], period_end=regular.publication_window[1],
            detail_prefix=f"report-index:{regular.stock_code}:{source_period}",
        )
        if body_hash != page.request_body_hash:
            raise CanonicalPackageError("regular index POST hash differs from frozen context")
        try:
            import json as _json
            from aquant.adapters.providers.cninfo import parse_announcements
            document = _json.loads(payload)
            rows = document.get("announcements")
            if not isinstance(rows, list):
                raise ValueError("missing announcements")
            announcements = parse_announcements(payload)
            if len(announcements) != len(rows) or any(item.sec_code != regular.stock_code for item in announcements):
                raise ValueError("unparsed or wrong-security rows")
        except (TypeError, ValueError, KeyError) as exc:
            raise CanonicalPackageError("regular index page cannot be parsed") from exc
        for item in announcements:
            if item.announcement_id in found:
                found[item.announcement_id].append((item, page.receipt_id))
    for ann_id, matches in found.items():
        if len(matches) != 1:
            raise CanonicalPackageError(f"regular index does not contain exactly one raw row for {ann_id}")
        ann, page_receipt = matches[0]
        expected = [match for match in regular.matches if match.announcement.announcement_id == ann_id]
        if len(expected) != 1:
            raise CanonicalPackageError(f"frozen regular context lacks exactly one {ann_id} row")
        match = expected[0]
        if (page_receipt != match.page_receipt_id or ann.sec_code != match.announcement.sec_code or
                ann.title != match.announcement.title or ann.announced_on != match.announcement.announced_on or
                not _same_document_url(ann.detail_url(), match.announcement.detail_url()) or
                ann.announcement_id != ann_id or ann.announced_on < regular.publication_window[0] or
                ann.announced_on > regular.publication_window[1] or
                not title_matches_period(ann.title, period)):
            raise CanonicalPackageError(f"regular index raw row differs for {ann_id}")


def _cross_dispositions(archive, cross: CrossCategoryIndexResult, *, report_id: str,
                        related_ids: Sequence[str], completed_at: datetime,
                        source_records: Mapping[str, Mapping[str, object]],
                        relation_source_overrides: Mapping[str, str] | None = None,
                        rationale_overrides: Mapping[str, str] | None = None) -> tuple[CrossCategoryDisposition, ...]:
    related = set(related_ids) | {report_id}
    relation_source_overrides = relation_source_overrides or {}
    rationale_overrides = rationale_overrides or {}
    out = []
    for entry in cross.candidates:
        receipt_id = None
        excerpt = None
        if entry.announcement_id in related:
            indexed = {"announcement_id": entry.announcement_id, "sec_code": entry.sec_code,
                       "title": entry.title, "url": entry.document_url}
            source, payload = _source_document(archive, indexed, expected_code=cross.stock_code,
                                               observed_at=completed_at)
            receipt_id = source["receipt_id"]
            if entry.announcement_id != report_id and any(reason != "period_title" for reason in entry.candidate_reasons):
                relation_id = relation_source_overrides.get(entry.announcement_id, entry.announcement_id)
                if relation_id != entry.announcement_id:
                    source = dict(source_records[relation_id])
                    source, relation_payload = _source_document(archive, source, expected_code=cross.stock_code,
                                                                observed_at=completed_at)
                else:
                    relation_payload = payload
                text = _native_pdf_page_text(relation_payload, (1,))
                excerpt = "".join(text.split())[:160]
                if len(excerpt) < 12:
                    raise CanonicalPackageError(f"related correction {entry.announcement_id} has no native text excerpt")
                relation_receipt = source["receipt_id"]
            else:
                relation_receipt = None
        elif is_financial_correction_title(entry.title):
            raise CanonicalPackageError(f"all-category index contains an unresolved financial correction: {entry.announcement_id}")
        if entry.announcement_id in related:
            out.append(CrossCategoryDisposition(
                announcement_id=entry.announcement_id, relevance="RELATED",
                reviewer_id=VERIFIER_ID, reviewed_at=completed_at,
                rationale=rationale_overrides.get(
                    entry.announcement_id,
                    "machine: source bytes are bound to the explicitly versioned canonical report package",
                ),
                document_receipt_id=receipt_id,
                related_report_announcement_ids=(report_id,),
                relation_receipt_id=relation_receipt,
                relation_excerpt=excerpt,
            ))
        else:
            out.append(CrossCategoryDisposition(
                announcement_id=entry.announcement_id, relevance="UNRELATED",
                reviewer_id=VERIFIER_ID, reviewed_at=completed_at,
                rationale=("machine: the existing narrow financial-title rule does not classify this "
                           "candidate as a financial correction; the complete index entry is retained"),
            ))
    return tuple(out)


def _version_review(regular: ReportIndexResult, report_id: str, reviewed_at: datetime) -> PdfVersionReview:
    matches = [match for match in regular.matches if match.announcement.announcement_id == report_id]
    if len(matches) != 1 or not regular.organization_id:
        raise CanonicalPackageError("canonical full report must appear exactly once in its regular index")
    return PdfVersionReview(
        announcement_id=report_id, org_lookup_receipt_id=regular.org_lookup_receipt_id or "",
        index_receipt_id=matches[0].page_receipt_id,
        search_receipt_ids=tuple(page.receipt_id for page in regular.pages),
        search_page_numbers=tuple(page.page_num for page in regular.pages),
        query_stock_code=regular.stock_code, query_organization_id=regular.organization_id,
        query_period_start=regular.publication_window[0], query_period_end=regular.publication_window[1],
        query_digest=cninfo_query_digest(stock_code=regular.stock_code, organization_id=regular.organization_id,
                                         period_start=regular.publication_window[0], period_end=regular.publication_window[1]),
        reviewer_id=VERIFIER_ID, reviewed_at=reviewed_at, complete_search_attested=True,
    )


def _native_pdf_page_text(payload: bytes, page_numbers: Sequence[int]) -> str:
    """Read only cited pages from bounded searchable source PDFs."""
    try:
        import io
        import pdfplumber
        with pdfplumber.open(io.BytesIO(payload)) as pdf:
            if not page_numbers or min(page_numbers) < 1 or max(page_numbers) > len(pdf.pages):
                raise CanonicalPackageError("cited native PDF page is outside its source")
            return "\n".join(pdf.pages[number - 1].extract_text(layout=False) or "" for number in page_numbers)
    except CanonicalPackageError:
        raise
    except Exception as exc:  # fail closed; no OCR fallback
        raise CanonicalPackageError("a cited canonical source page cannot be read as native text") from exc


def _ganneng_cause_proof(cause_sources: Mapping[str, tuple[dict, bytes]], h126: bytes, helper_proof: dict) -> dict:
    def text(ann_id: str) -> str:
        return "".join(_native_pdf_page_text(cause_sources[ann_id][1], (1,)).split())
    early, august, october = text("1223101826"), text("1225490303"), text("1225619745")
    # The subsidiary name wraps vertically across its native grid cell;
    # page-wide reading order interleaves other columns with that name.
    import io
    import pdfplumber
    with pdfplumber.open(io.BytesIO(h126)) as pdf:
        peak_rows = []
        for table in pdf.pages[110].find_tables({"vertical_strategy": "lines", "horizontal_strategy": "lines"}):
            for row in table.extract():
                cells = ["".join(str(cell or "").split()) for cell in row]
                if cells and cells[0] == "江西峰山抽水蓄能有限公司":
                    peak_rows.append(cells)
    if (len(peak_rows) != 1 or "50.00%" not in peak_rows[0] or
            "同一控制下企业合并" not in peak_rows[0]):
        raise CanonicalPackageError("Ganneng native subsidiary grid does not bind the prior Fengshan acquisition")
    october_period = any(token in october for token in ("2025年1-9月", "2025年1—9月", "2025年前三季度"))
    required = (
        (early, ("峰山抽水蓄能有限公司", "50%", "2023")),
        (august, ("2026年5月8日", "东津", "100%", "2025年半年度")),
        (october, ("东津", "100%")),
    )
    if any(not all(token in document for token in tokens) for document, tokens in required) or not october_period:
        raise CanonicalPackageError("Ganneng cause PDFs do not bind the two native same-control events")
    event = helper_proof.get("event_binding")
    if (not isinstance(event, Mapping) or
            event.get("oct_notice_scope", {}).get("same_event_as_h126_acquisition") is not True):
        raise CanonicalPackageError("Ganneng H126 and October notice event binding failed")
    return {"schema": "aquant.cninfo.000899-h125-cause-closure.v1",
            "cause_ids": ["1223101826", "1225490303", "1225619745"],
            "early_peak_storage_acquisition": {"source_document_id": "1223101826", "source_company_table": "江西峰山抽水蓄能有限公司", "ownership_percent": "50%", "target_period": "2023-12-31"},
            "same_control_dongjin_acquisition": {"announcement_ids": ["1225490303", "1225619745"], "event_date": "2026-05-08", "target_company": "东津", "ownership_percent": "100%", "h126_native_presence": True, "august_notice_scopes": ["2026 opening data", "2025 H1 restatement"], "october_notice_scopes": ["2025 9M restatement"], "october_is_new_h1_event": False},
            "h126_fengshan_native_grid": {"physical_page": 111, "cells": peak_rows[0]},
            "helper_event_proof": event}


def _case_proof(case_id: str, sources: Mapping[str, tuple[dict, bytes]]) -> tuple[dict[str, Decimal], dict]:
    spec = _CASES[case_id]
    code, fact_period = spec["instrument_id"], date.fromisoformat(spec["fact_period_end"])
    primary = sources[spec["primary_document_id"]][1]
    if case_id == "TIANSHAN_AUDIT_2025":
        from aquant.adapters.providers.tianshan_audit_native_notes import (
            extract_native_note_facts, verify_native_note_evidence,
        )
        source = sources["1225041401"][0]
        candidate = extract_native_note_facts(
            primary, instrument_id=code, company=spec["company"], period_end=fact_period,
            announcement_id="1225041401", document_url=source["url"],
            required_fields=tuple(sorted(_FIELDS[case_id])),
        )
        verification = verify_native_note_evidence(primary, candidate=candidate,
                                                   expected_fields=tuple(sorted(_FIELDS[case_id])))
        if not verification.verified:
            raise CanonicalPackageError("Tianshan audit note fields did not independently verify")
        values = {item.field: item.value_yuan for item in candidate.candidates}
        proof = {"source_candidate": _jsonable(candidate), "independent_verification": verification.as_evidence(),
                 "legal_identity_limitation": "A-share code is supplied by the exact official index/audit association; it is not printed on the audit PDF cover."}
    elif case_id == "GANNENG_H125_RESTATED_PRIOR":
        from aquant.adapters.providers.ganneng_h125_presented_prior import extract_presented_prior_evidence
        base = extract_presented_prior_evidence(
            primary, sources["1225619745"][1], instrument_id=code, target_period_end=fact_period,
        )
        cause_proof = _ganneng_cause_proof(
            {key: sources[key] for key in spec["cause_ids"]}, primary, base,
        )
        values = {metric: Decimal(str(value["value_yuan"])) for metric, value in base["values"].items()}
        if set(values) != set(_FIELDS[case_id]) or base.get("column_role") != "PRESENTED_PRIOR":
            raise CanonicalPackageError("Ganneng extractor did not return the exact H125 presented-prior field set")
        proof = {"source_candidate": base, "cause_closure": cause_proof}
    elif case_id == "YIHUA_H126_CANONICAL_CURRENT":
        from aquant.adapters.providers.yihua_h126_canonical import extract_canonical_current_evidence
        source_pdfs = {ann_id: sources[ann_id][1] for ann_id in spec["source_ids"]}
        proof = extract_canonical_current_evidence(source_pdfs, instrument_id="SZ." + code,
                                                   target_period_end=spec["fact_period_end"])
        candidate = proof.get("values", {}).get("parent_equity") if isinstance(proof, Mapping) else None
        if not isinstance(candidate, Mapping) or candidate.get("value_yuan") is None:
            raise CanonicalPackageError("Yihua helper did not return a current parent-equity source cell")
        values = {"parent_equity": Decimal(str(candidate["value_yuan"]))}
        if proof.get("column_role") not in (None, "CURRENT") or proof.get("pit_eligible") not in (None, False):
            raise CanonicalPackageError("Yihua helper carries a conflicting column role or pre-admission PIT claim")
    else:
        from aquant.adapters.providers import xibu_fy25_canonical as xibu
        # The caller supplies a verified typed closure through the third argument.
        # It is constructed below from replayed archive pages, never from JSON flags.
        closure = sources.get("__xibu_closure__")
        if closure is None:
            raise CanonicalPackageError("Xibu source closure was not built from archived index receipts")
        source_pdfs = {key: sources[key][1] for key in spec["source_ids"]}
        proof = xibu.extract_canonical_annual_evidence(
            annual_pdf=source_pdfs["1225082554"],
            correction_notice=source_pdfs["1225082606"],
            board_notice=source_pdfs["1225082555"],
            corrected_financials=source_pdfs["1225082560"],
            instrument_id=code, target_period_end=spec["fact_period_end"],
            annual_board_resolution_pdf=source_pdfs["1225082568"], index_evidence=closure,
        )
        if (proof.get("caseClassification") != "CONCURRENT_FULL_YEAR_AND_INTERIM_CORRECTION_PACKAGE" or
                proof.get("canonicalSourceProof") != "PASS"):
            raise CanonicalPackageError("Xibu annual board/index/correction package is not proven")
        raw = proof.get("annualSourceEvidence", {}).get("annualCurrentCell", {}).get("valueYuan")
        if raw is None:
            # Preserve the bounded helper interface: the primary amount must be
            # explicitly present in its native annual-source evidence.
            raise CanonicalPackageError("Xibu annual helper did not return the consolidated net-profit cell")
        values = {"net_profit_consolidated": Decimal(str(raw))}
    if set(values) != set(_FIELDS[case_id]) or any(not value.is_finite() for value in values.values()):
        raise CanonicalPackageError(f"{case_id} did not rederive the exact finite native field set")
    return values, proof


def _xibu_closure(index_source_map: Mapping[str, Mapping[str, object]], regular: ReportIndexResult,
                  cross: CrossCategoryIndexResult):
    from aquant.adapters.providers.xibu_fy25_canonical import ValidatedIndexEvidence
    regular_rows = {"page_receipt_ids": [page.receipt_id for page in regular.pages],
                    "request_body_hashes": [page.request_body_hash for page in regular.pages],
                    "publication_window": [day.isoformat() for day in regular.publication_window]}
    cross_rows = {"complete": cross.complete,
                  "page_receipt_ids": [page.receipt_id for page in cross.pages],
                  "request_body_hashes": [page.request_body_hash for page in cross.pages],
                  "publication_window": [day.isoformat() for day in cross.publication_window]}
    announcement_map = {key: dict(index_source_map[key]) for key in (
        "1225082554", "1225082555", "1225082560", "1225082606", "1225082568")}
    return ValidatedIndexEvidence(
        schema="aquant.cninfo.canonical-source-index-closure.v1",
        instrument_id="601069", fact_period_end="2025-12-31", source_report_period_end="2025-12-31",
        canonical_full_report_ids=("1225082554",),
        financial_correction_ids=("1225082555", "1225082560", "1225082606"),
        regular=regular_rows, all_category=cross_rows, announcement_map=announcement_map,
        evidence_reference=": ".join(cross_rows["page_receipt_ids"]),
    )


def _index_evidence(archive, case_id: str, regular: ReportIndexResult, cross: CrossCategoryIndexResult,
                    primary_source: dict, source_records: Mapping[str, dict], completed_at: datetime) -> dict:
    spec = _CASES[case_id]
    # The PDF promotion primitives are used only for source/index replay.  Their
    # ordinary promotion method is intentionally not called for this schema.
    _verify_regular_sources(archive, regular, (spec["canonical_report_id"],),
                            source_period=spec["source_report_period_end"])
    review = _version_review(regular, spec["canonical_report_id"], completed_at)
    metadata_candidate = __import__(
        "aquant.adapters.providers.cninfo_financial_pdf", fromlist=["CninfoS2CandidateFacts"]
    ).CninfoS2CandidateFacts(
        instrument_id=spec["instrument_id"], period_end=date.fromisoformat(spec["source_report_period_end"]),
        version_label="canonical-package-metadata-only", document_url=review_doc_url(regular, spec["canonical_report_id"]),
        pdf_sha256=primary_source["content_hash"], candidates=(),
    )
    publication, role, regular_receipts, regular_hashes = _version_evidence(archive, metadata_candidate, review, now=completed_at)
    related = set(spec["cause_ids"]) | set(spec["companion_ids"])
    dispositions = _cross_dispositions(archive, cross, report_id=spec["canonical_report_id"],
                                       related_ids=tuple(sorted(related)), completed_at=completed_at,
                                       source_records=source_records,
                                       relation_source_overrides=(
                                           {"1225183650": "1225183652"}
                                           if case_id == "YIHUA_H126_CANONICAL_CURRENT" else None),
                                       rationale_overrides=(
                                           {"1225183650": "machine: CPA attachment is identity-only; financial scope and current amount are sourced from the readable board cause and the H126 annual/current proof"}
                                           if case_id == "YIHUA_H126_CANONICAL_CURRENT" else None))
    cross_receipts, cross_hashes, disposition_proof = _cross_category_evidence(
        archive, metadata_candidate, cross, dispositions, review=review,
        reviewed_by=VERIFIER_ID, reviewed_at=completed_at,
        pdf_receipt_id=primary_source["receipt_id"], now=completed_at,
    )
    return {
        "regular": {
            "report_period_end": regular.report_period,
            "publication_window": [day.isoformat() for day in regular.publication_window],
            "organization_id": regular.organization_id,
            "organization_lookup_receipt_id": regular.org_lookup_receipt_id,
            "page_receipt_ids": [page.receipt_id for page in regular.pages],
            "request_body_hashes": regular_hashes,
            "canonical_report_id": spec["canonical_report_id"],
            "source_published_date": publication.isoformat(),
            "disclosure_role": role.value,
            "verified_receipt_ids": [row["receipt_id"] for row in regular_receipts],
            "reviewed_at": completed_at.isoformat(),
        },
        "all_category": {
            "report_period_end": cross.report_period,
            "publication_window": [day.isoformat() for day in cross.publication_window],
            "page_receipt_ids": [page.receipt_id for page in cross.pages],
            "request_body_hashes": cross_hashes,
            "verified_receipt_ids": [row["receipt_id"] for row in cross_receipts],
            "candidate_dispositions": disposition_proof,
            "reviewed_at": completed_at.isoformat(),
        },
    }


def review_doc_url(regular: ReportIndexResult, announcement_id: str) -> str:
    found = [item.announcement.detail_url() for item in regular.matches
             if item.announcement.announcement_id == announcement_id]
    if len(found) != 1 or not found[0]:
        raise CanonicalPackageError("canonical report URL is not uniquely indexed")
    return found[0].replace("http://", "https://", 1)


def _facts(case_id: str, values: Mapping[str, Decimal], source: Mapping[str, object],
           completed_at: datetime, available_at: datetime) -> tuple[FinancialFact, ...]:
    spec = _CASES[case_id]
    facts = []
    for metric in sorted(values):
        identity = (f"cninfo|canonical-package|{case_id}|{spec['instrument_id']}|"
                    f"{spec['fact_period_end']}|{metric}|{source['announcement_id']}|"
                    f"{source['content_hash']}|{spec['column_role']}")
        version_id = "cpk_" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        scope = (ProfitScope.ATTRIBUTABLE if metric == "net_profit_attributable" else
                 ProfitScope.CONSOLIDATED if metric == "net_profit_consolidated" else None)
        facts.append(FinancialFact(
            instrument_id=spec["instrument_id"], metric=metric,
            period_end=date.fromisoformat(spec["fact_period_end"]),
            statement_scope=StatementScope.CONSOLIDATED, profit_scope=scope,
            value=values[metric], currency="CNY", raw_unit="yuan", source_id="cninfo",
            source_document_id=str(source["announcement_id"]),
            source_published_date=date.fromisoformat(str(source["published_date"])),
            source_published_at=None, timestamp_precision=TimestampPrecision.DATE,
            first_seen_at=datetime.fromisoformat(str(source["first_seen_at"])),
            ingested_at=completed_at, available_at=available_at,
            availability_basis=AvailabilityBasis.OBSERVED, pit_mode=PitMode.LIVE_OBSERVED,
            content_hash=str(source["content_hash"]), version_id=version_id,
        ))
    return tuple(facts)


def _build(case_id: str, archive, index_context, calendar_evidence: dict,
           *, completed_at: datetime | None, reviewed_at: datetime | None = None) -> tuple[tuple[FinancialFact, ...], dict]:
    if case_id not in _CASES:
        raise CanonicalPackageError("unknown canonical report-package case")
    if completed_at is not None and (completed_at.tzinfo is None or completed_at.utcoffset() is None):
        raise CanonicalPackageError("package completion time must be timezone-aware")
    if reviewed_at is None:
        reviewed_at = completed_at or datetime.now(timezone.utc)
    if reviewed_at.tzinfo is None or reviewed_at.utcoffset() is None:
        raise CanonicalPackageError("index review time must be timezone-aware")
    spec = _CASES[case_id]
    regular, cross, context_packet = _context(index_context, spec["source_report_period_end"])
    if regular.stock_code != spec["instrument_id"]:
        raise CanonicalPackageError("company index is for another security")
    expected_causes = set(spec["cause_ids"])
    indexed_causes = {entry.announcement_id for entry in cross.candidates
                      if is_financial_correction_title(entry.title)}
    if indexed_causes != expected_causes:
        raise CanonicalPackageError("full company index correction IDs differ from the exact case closure")

    source_records: dict[str, dict] = {}
    source_bytes: dict[str, bytes] = {}
    for announcement_id in spec["source_ids"]:
        index_record = _source_index_record(regular, cross, announcement_id)
        source, payload = _source_document(archive, index_record, expected_code=spec["instrument_id"],
                                          observed_at=reviewed_at)
        source_records[announcement_id], source_bytes[announcement_id] = source, payload
    primary_source = source_records[spec["primary_document_id"]]
    canonical_source = source_records[spec["canonical_report_id"]]
    for announcement_id, source in source_records.items():
        expected_hash = _SOURCE_HASHES.get(announcement_id)
        if expected_hash is not None and source["content_hash"] != expected_hash:
            raise CanonicalPackageError(f"official source {announcement_id} differs from the bounded reviewed PDF")
    _verify_regular_sources(archive, regular, (spec["canonical_report_id"],),
        source_period=spec["source_report_period_end"])

    if case_id == "XIBU_FY25_FIRST_ANNUAL":
        closure = _xibu_closure(source_records, regular, cross)
        # The helper takes the typed closure as a fifth-source proof, not an amount source.
        source_tuple = dict((key, (value, source_bytes[key])) for key, value in source_records.items())
        source_tuple["__xibu_closure__"] = closure
        values, case_proof = _case_proof(case_id, source_tuple)
    else:
        values, case_proof = _case_proof(case_id, {key: (value, source_bytes[key]) for key, value in source_records.items()})

    # Cross-category replay binds every exact cause/companion PDF before PIT is
    # computed.  These are source-package relationships, not metric no-effect claims.
    index_review = _index_evidence(archive, case_id, regular, cross, canonical_source,
                                   source_records, reviewed_at)
    calendar_days, replayed_calendar = verify_calendar_evidence(
        calendar_evidence, archive, now=completed_at or reviewed_at,
    )
    rights = default_rights().get("cninfo")
    if not (rights.open_for("research_use") and rights.open_for("local_storage")):
        raise CanonicalPackageError("CNINFO source rights do not permit research and local storage")
    date_floor, floor_reason = date_only_available_at(
        date.fromisoformat(canonical_source["published_date"]), calendar_days,
    )
    seen_times = [datetime.fromisoformat(source["first_seen_at"]) for source in source_records.values()]
    all_receipt_ids = set(index_review["regular"]["verified_receipt_ids"] + index_review["all_category"]["verified_receipt_ids"])
    for source in source_records.values():
        all_receipt_ids.update(source["receipt_ids"])
    for receipt_id in all_receipt_ids:
        receipt = archive.con.execute("SELECT first_seen_at FROM fetch_receipt WHERE receipt_id=?", (receipt_id,)).fetchone()
        if receipt is None:
            raise CanonicalPackageError("an index or source receipt disappeared during admission")
        seen_times.append(datetime.fromisoformat(receipt["first_seen_at"]))
    # Calendar receipts are transitively replayed by verify_calendar_evidence;
    # include every referenced archived receipt in the availability lower bound.
    def calendar_receipts(value):
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key == "receipt_id" and isinstance(item, str):
                    yield item
                else:
                    yield from calendar_receipts(item)
        elif isinstance(value, list):
            for item in value:
                yield from calendar_receipts(item)
    for receipt_id in set(calendar_receipts(replayed_calendar)):
        receipt = archive.con.execute("SELECT first_seen_at FROM fetch_receipt WHERE receipt_id=?", (receipt_id,)).fetchone()
        if receipt is None:
            raise CanonicalPackageError("calendar source receipt is absent from the official archive")
        seen_times.append(datetime.fromisoformat(receipt["first_seen_at"]))
    completed_at = completed_at or datetime.now(timezone.utc)
    if completed_at < reviewed_at:
        raise CanonicalPackageError("package completion time precedes its source/index review")
    # Recheck the calendar at the actual completed time so a boundary crossing
    # during source extraction cannot reuse a stale day context.
    calendar_days, replayed_calendar = verify_calendar_evidence(calendar_evidence, archive, now=completed_at)
    available_after = max([date_floor, completed_at, *seen_times])
    available_at = next((preopen_instant(day) for day in calendar_days
                         if preopen_instant(day) >= available_after), None)
    if available_at is None:
        raise CanonicalPackageError("verified trading calendar does not reach the next available snapshot")
    facts = _facts(case_id, values, primary_source, completed_at, available_at)
    cause_closure = {
        "known_financial_correction_ids": list(spec["cause_ids"]),
        "source_records": [source_records[key] for key in spec["cause_ids"]],
        "classification": {
            "TIANSHAN_AUDIT_2025": "NO_FINANCIAL_CORRECTION_CAUSE_IN_COMPLETE_WINDOW",
            "GANNENG_H125_RESTATED_PRIOR": "RESTATED_PRIOR_COLUMN_WITH_TWO_NATIVE_CAUSAL_EVENTS",
            "YIHUA_H126_CANONICAL_CURRENT": "CANONICAL_CURRENT_AFTER_REVISION",
            "XIBU_FY25_FIRST_ANNUAL": "FIRST_AND_SOLE_FULL_ANNUAL_WITH_SCOPED_INTERIM_CORRECTIONS",
        }[case_id],
        "metric_specific_annual_effect": "NOT_ASSERTED",
    }
    evidence = {
        "schema": SCHEMA, "bundle_prefix": BUNDLE_PREFIX, "case_id": case_id,
        "verifier_id": VERIFIER_ID, "verifier_version": VERIFIER_VERSION,
        "human_review_claimed": None, "rights_register_version": REGISTER_VERSION,
        "source_id": "cninfo", "instrument_id": spec["instrument_id"], "company": spec["company"],
        "fact_period_end": spec["fact_period_end"],
        "source_report_period_end": spec["source_report_period_end"],
        "column_role": spec["column_role"], "canonical_report_id": spec["canonical_report_id"],
        "primary_source_document_id": spec["primary_document_id"],
        "primary_source": primary_source, "source_records": [source_records[key] for key in spec["source_ids"]],
        "field_values_yuan": {key: str(values[key]) for key in sorted(values)},
        "case_source_proof": case_proof, "cause_closure": cause_closure,
        "revision_disposition": "CANONICAL_PACKAGE_EXPLICITLY_VERSIONED",
        "metric_specific_impact_asserted": False,
        "index_context": context_packet, "index_replay": index_review,
        "calendar_evidence": replayed_calendar,
        "archive_binding": _archive_binding(archive),
        "available_at": available_at.isoformat(), "availability_floor": date_floor.isoformat(),
        "availability_floor_reason": floor_reason,
        "first_seen_at": primary_source["first_seen_at"],
        "source_published_date": primary_source["published_date"],
        "verification_completed_at": completed_at.isoformat(),
        "ingested_at": completed_at.isoformat(), "availability_basis": "OBSERVED",
        "index_reviewed_at": reviewed_at.isoformat(),
        "pit_mode": "LIVE_OBSERVED", "currency": "CNY", "raw_unit": "yuan",
        "statement_scope": "CONSOLIDATED",
        "profit_scope_by_metric": {metric: ("ATTRIBUTABLE" if metric == "net_profit_attributable" else
                                              "CONSOLIDATED" if metric == "net_profit_consolidated" else None)
                                   for metric in sorted(values)},
        "version_ids": [fact.version_id for fact in facts],
    }
    bundle_key = "|".join((case_id, spec["instrument_id"], spec["fact_period_end"],
                           spec["primary_document_id"], primary_source["content_hash"]))
    bundle_id = BUNDLE_PREFIX + hashlib.sha256(bundle_key.encode()).hexdigest()[:32]
    evidence["bundle_id"] = bundle_id
    return facts, evidence


def promote_canonical_report_package(*, case_id: str, archive, repository: FinancialFactRepository,
                                     index_context: Mapping[str, object], calendar_evidence: dict):
    """Re-derive a bounded case, then append its entire fact/review package atomically."""
    facts, evidence = _build(case_id, archive, index_context, calendar_evidence, completed_at=None)
    evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    inserted = repository.append_many(
        facts, review_bundle=FactReviewBundle(evidence["bundle_id"], evidence_json),
    )
    evidence = {**evidence, "inserted_fact_count": inserted}
    # ``inserted_fact_count`` is diagnostic only and is not persisted in evidence.
    return facts, evidence


def _fact_matches(actual: FinancialFact, expected: FinancialFact) -> bool:
    return actual == expected


def accepts_canonical_report_package(row, member_ids: set[str], facts: Mapping[str, FinancialFact]) -> bool:
    """Fresh reader: reopen the pinned archive read-only and rederive every proof/value."""
    try:
        evidence_json = row["evidence_json"]
        evidence = json.loads(evidence_json)
        if not isinstance(evidence, dict) or evidence.get("schema") != SCHEMA:
            return False
        canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if canonical != evidence_json or row["evidence_hash"] != "sha256:" + hashlib.sha256(evidence_json.encode("utf-8")).hexdigest():
            return False
        case_id = evidence["case_id"]
        if case_id not in _CASES or evidence.get("bundle_prefix") != BUNDLE_PREFIX:
            return False
        completed = datetime.fromisoformat(evidence["verification_completed_at"])
        if completed.tzinfo is None or completed > datetime.now(timezone.utc):
            return False
        binding = evidence["archive_binding"]
        root = resolve_artifact_path(binding["root"]).resolve()
        database = resolve_artifact_path(binding["database_path"]).resolve()
        if database != (root / "meta.sqlite").resolve() or not database.is_file():
            return False
        # SQLite URI read-only plus query_only; no ForwardArchive constructor/DDL.
        with closing(connect(database, read_only=True)) as con:
            con.execute("PRAGMA query_only=ON")
            archive = _ReadOnlyArchive(con, root)
            for source in evidence["source_records"]:
                fresh, _payload = _source_document(archive, source,
                    expected_code=_CASES[case_id]["instrument_id"],
                    observed_at=datetime.fromisoformat(evidence["index_reviewed_at"]))
                if fresh != source:
                    return False
            facts_expected, evidence_expected = _build(
                case_id, archive, evidence["index_context"], evidence["calendar_evidence"],
                completed_at=completed, reviewed_at=datetime.fromisoformat(evidence["index_reviewed_at"]),
            )
        if evidence_expected != evidence or row["bundle_id"] != evidence["bundle_id"]:
            return False
        expected_by_id = {fact.version_id: fact for fact in facts_expected}
        if set(member_ids) != set(expected_by_id) or not member_ids <= facts.keys():
            return False
        return all(_fact_matches(facts[version_id], expected_by_id[version_id]) for version_id in member_ids)
    except (ArithmeticError, AttributeError, KeyError, OSError, TypeError, ValueError, sqlite3.Error):
        return False


__all__ = [
    "SCHEMA", "BUNDLE_PREFIX", "CanonicalPackageError",
    "promote_canonical_report_package", "accepts_canonical_report_package",
]
