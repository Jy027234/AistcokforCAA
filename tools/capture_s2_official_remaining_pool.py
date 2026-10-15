"""Capture shared CNINFO indexes and official S2 source reports for a frozen shard.

The source indexes and PDFs are archived by the existing guarded CninfoClient.
Exact, still-valid archived request/response pairs and PDF receipts are reused;
new provider calls are limited to missing current-scope evidence. No facts are
extracted or promoted by this command.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.cninfo import (  # noqa: E402
    CninfoClient, default_policy,
)
from aquant.adapters.providers.cninfo_probe import (  # noqa: E402
    parse_report_period, title_revision_flags,
)
from aquant.adapters.providers.eastmoney import FetchOutcome  # noqa: E402
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402
from aquant.domain.fundamentals.disclosure_link import report_period_from_title  # noqa: E402
from aquant.domain.research.s2 import required_s2_periods  # noqa: E402

BEIJING = ZoneInfo("Asia/Shanghai")
TRUST_ENV = "AQUANT_TRUSTED_PROXY_NETWORKS"
EARLIEST_PERIOD = date(2024, 6, 30)
DISCOVERY_SCHEMA = "aquant.s2-official-index-discovery.v1"
PDF_SCHEMA = "aquant.s2-official-financial-pdfs.v1"
CHECKPOINT_SCHEMA = "aquant.s2-remaining-pool-capture-checkpoint.v1"
FOLLOWUP_DEFAULT = ROOT / "tmp" / "s2-nonpriority-official-followup-20261013.json"
POOL_DEFAULT = ROOT / "data/s2-structured/daily/20261012/attempt-edbe0d955ae8/simulation-evidence/pool-120-main.yaml"
PRIORITY_DEFAULT = ROOT / "data/s2-structured/financial-priority-20261013.json"
ARCHIVE_DEFAULT = ROOT / "deploy/agentctl-q0/forward-archive"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: date | datetime) -> str:
    return value.isoformat()


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _json(path: Path) -> tuple[dict, bytes]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        import yaml
        value = yaml.safe_load(raw)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON/YAML object: {path}")
    return value, raw


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                     dir=path.parent, prefix=f".{path.name}.",
                                     suffix=".tmp", delete=False) as stream:
        tmp = Path(stream.name)
        json.dump(data, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _trusted_env_file(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"saved network env file is missing: {path}")
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        name, value = text.split("=", 1)
        if name.strip() == TRUST_ENV:
            value = value.strip().strip("\"'")
            if value:
                return value
            break
    raise ValueError(f"{TRUST_ENV} is empty or missing in the saved env file")


def load_targets(pool_path: Path, followup_path: Path,
                 priority_path: Path) -> tuple[list[dict], dict]:
    pool, pool_raw = _json(pool_path)
    followup, followup_raw = _json(followup_path)
    priority, priority_raw = _json(priority_path)
    if not isinstance(pool.get("instruments"), list):
        raise ValueError("pool has no instruments array")
    all_by_code: dict[str, dict] = {}
    for item in pool["instruments"]:
        if not isinstance(item, dict):
            raise ValueError("pool instruments must be objects")
        iid = str(item.get("instrument_id") or "").upper()
        match = re.fullmatch(r"(SH|SZ)\.([0-9]{6})", iid)
        if match is None:
            raise ValueError(f"pool has invalid instrument identity: {iid!r}")
        exchange, code = match.groups()
        supported = ((exchange == "SH" and code.startswith(("60", "68"))) or
                     (exchange == "SZ" and code.startswith(("00", "30"))))
        if not supported:
            raise ValueError(f"pool identity is outside supported SH/SZ stocks: {iid}")
        if code in all_by_code:
            raise ValueError(f"duplicate pool code: {code}")
        expected = ("sh" if exchange == "SH" else "sz") + code
        raw_code = str(item.get("code") or "").lower()
        if raw_code != expected:
            raise ValueError(f"pool code and instrument_id disagree: {iid}")
        all_by_code[code] = {
            "code": code, "instrumentId": iid, "name": str(item.get("name") or ""),
            "industry": str(item.get("industry") or ""),
            "researchRank": item.get("research_rank"),
        }
    expected_pool_hash = followup.get("pool_sha256")
    actual_pool_hash = _sha(pool_raw)
    if expected_pool_hash != actual_pool_hash:
        raise ValueError("input pool hash differs from the frozen followup manifest")
    expected_priority_hash = followup.get("priority_manifest_sha256")
    if expected_priority_hash != _sha(priority_raw):
        raise ValueError("priority manifest hash differs from frozen followup manifest")
    priority_ids = {str(value).upper() for value in priority.get("priorityInstrumentIds", [])}
    followup_rows = followup.get("remaining_in_pool_order")
    if not isinstance(followup_rows, list) or not followup_rows:
        raise ValueError("fixed followup manifest has no remaining target rows")
    listed = [str(row.get("code") or "") for row in followup_rows]
    if len(set(listed)) != len(listed) or any(code not in all_by_code for code in listed):
        raise ValueError("followup codes are duplicated or outside the frozen pool")
    expected_remaining = [code for code, item in all_by_code.items()
                          if item["instrumentId"] not in priority_ids]
    if set(listed) != set(expected_remaining):
        raise ValueError("fixed followup list does not equal pool minus priority scope")
    targets = []
    for row in followup_rows:
        code = str(row["code"])
        target = dict(all_by_code[code])
        if row.get("instrument_id", "").upper() != target["instrumentId"]:
            raise ValueError(f"followup instrument identity changed for {code}")
        targets.append(target)
    binding = {
        "poolPath": str(pool_path.resolve()), "poolSha256": actual_pool_hash,
        "followupPath": str(followup_path.resolve()),
        "followupSha256": _sha(followup_raw),
        "priorityPath": str(priority_path.resolve()),
        "prioritySha256": _sha(priority_raw),
        "targetCodes": listed,
        "targetInstrumentIds": [row["instrumentId"] for row in targets],
        "poolId": pool.get("pool_id"),
        "poolAsOf": pool.get("as_of"),
    }
    return targets, binding


class ArchiveFirstCninfoClient(CninfoClient):
    """Replay exact archived requests when their scope still proves the cutoff."""

    def __init__(self, archive: ForwardArchive, *, observed_day: date) -> None:
        super().__init__(archive, policy=default_policy())
        self.observed_day = observed_day

    def _cached_receipt_is_current(self, row, *, url: str, data: bytes | None) -> bool:
        stamp = datetime.fromisoformat(str(row["first_seen_at"]).replace("Z", "+00:00"))
        if urlsplit_path(url) == "/new/information/topSearch/query":
            return True  # official issuer identity lookup is not a period snapshot
        if data is None:
            return stamp.astimezone(BEIJING).date() == self.observed_day
        query = parse_qs(data.decode("utf-8", "replace"))
        date_range = query.get("seDate", [""])[0]
        end_text = date_range.partition("~")[2]
        try:
            requested_end = date.fromisoformat(end_text)
        except ValueError:
            return stamp.astimezone(BEIJING).date() == self.observed_day
        # A fully exhausted regular-report window with a closed two-year bound
        # is immutable for this query. Open windows are reusable only from
        # today's observation, and cross-category queries always end today.
        return (requested_end < self.observed_day or
                stamp.astimezone(BEIJING).date() == self.observed_day)

    def _fetch(self, url: str, *, label: str, data: bytes | None = None,
               content_type: str | None = None, max_bytes: int | None = None) -> FetchOutcome:
        row = self.archive.con.execute(
            "SELECT receipt_id,http_status,content_hash,byte_size,first_seen_at "
            "FROM fetch_receipt WHERE source_id='cninfo' AND outcome='OK' "
            "AND http_status=200 AND content_hash IS NOT NULL AND url=? AND detail=? "
            "ORDER BY first_seen_at DESC LIMIT 1", (url, label),
        ).fetchone()
        if row is not None and self._cached_receipt_is_current(row, url=url, data=data):
            digest = str(row["content_hash"])
            if self.archive.verify(digest):
                payload = self.archive.load_bytes(digest)
                if len(payload) == row["byte_size"] and (max_bytes is None or len(payload) <= max_bytes):
                    return FetchOutcome(True, payload, row["receipt_id"], digest, None, 200)
        return super()._fetch(url, label=label, data=data,
                              content_type=content_type, max_bytes=max_bytes)


def urlsplit_path(url: str) -> str:
    from urllib.parse import urlsplit
    return urlsplit(url).path


def _receipt(archive: ForwardArchive, receipt_id: str | None) -> dict | None:
    if not receipt_id:
        return None
    row = archive.con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?", (receipt_id,)).fetchone()
    return dict(row) if row else None


def _page_rows(result) -> list[dict]:
    return [{
        "page": item.page_num,
        "requestBodyHash": item.request_body_hash,
        "responseReceiptId": item.receipt_id,
        "responseContentHash": item.content_hash,
        "httpStatus": item.http_status,
        "rawAnnouncementCount": item.raw_announcement_count,
        "totalAnnouncement": item.total_announcement,
        "hasMore": item.has_more,
        "firstSeenAt": None,
    } for item in result.pages]


def _set_page_times(rows: list[dict], archive: ForwardArchive) -> None:
    for item in rows:
        receipt = _receipt(archive, item.get("responseReceiptId"))
        item["firstSeenAt"] = receipt.get("first_seen_at") if receipt else None


def _candidate_periods(announcements, *, through: date) -> list[date]:
    # Use the same complete-title grammar as formal disclosure admission. A
    # substring test here would promote previews, summaries and notice titles
    # into a falsely newer reporting period.
    return sorted({period for entry in announcements
                   if (period := report_period_from_title(entry.title)) is not None
                   and EARLIEST_PERIOD <= period <= through})


def _instrument_id(exchange_code: str, code: str) -> str:
    if code.startswith(("60", "68")):
        return "SH." + code
    if code.startswith(("00", "30")):
        return "SZ." + code
    raise ValueError(f"unsupported code: {code}")


def _index_pages(result, archive: ForwardArchive) -> list[dict]:
    pages = _page_rows(result)
    _set_page_times(pages, archive)
    return pages


def _all_page_receipt_times(company: dict) -> list[str]:
    found = []
    for row in company.get("allCategoryPages", []):
        if row.get("firstSeenAt"):
            found.append(row["firstSeenAt"])
    for index in company.get("periodReportIndexes", {}).values():
        for row in index.get("pages", []):
            if row.get("firstSeenAt"):
                found.append(row["firstSeenAt"])
    for row in company.get("documentRecords", []):
        stamp = (row.get("receipt") or {}).get("first_seen_at")
        if stamp:
            found.append(stamp)
    return found


def _receipt_record(archive: ForwardArchive, receipt_id: str, *, url: str,
                    content_hash: str, byte_size: int) -> dict:
    row = _receipt(archive, receipt_id)
    if (row is None or row.get("source_id") != "cninfo" or row.get("outcome") != "OK"
            or row.get("http_status") != 200 or row.get("url") != url
            or row.get("content_hash") != content_hash or row.get("byte_size") != byte_size
            or not archive.verify(content_hash)):
        raise ValueError(f"archived PDF receipt failed validation: {receipt_id}")
    return row


def _document_record(*, archive: ForwardArchive, client: CninfoClient,
                     instrument: dict, period_end: str | None,
                     announcement_id: str, title: str, url: str,
                     index_page: int | None, index_receipt_id: str | None,
                     index_response_hash: str | None) -> dict:
    existing = archive.con.execute(
        "SELECT receipt_id,content_hash,byte_size FROM fetch_receipt "
        "WHERE source_id='cninfo' AND outcome='OK' AND http_status=200 AND url=? "
        "ORDER BY first_seen_at DESC LIMIT 1", (url,),
    ).fetchone()
    if existing is None:
        outcome = client.document(url, label=f"s2-official-financial:{instrument['code']}:{period_end or 'related'}:{announcement_id}")
        if not outcome.ok or outcome.payload is None or outcome.content_hash is None:
            raise ValueError(f"official PDF download failed ({outcome.receipt_id}): {outcome.detail}")
        payload = outcome.payload
        receipt_id, digest = outcome.receipt_id, outcome.content_hash
    else:
        receipt_id, digest = existing["receipt_id"], existing["content_hash"]
        if not archive.verify(digest):
            raise ValueError(f"existing official PDF bytes failed hash: {digest}")
        payload = archive.load_bytes(digest)
    if not payload.startswith(b"%PDF-"):
        raise ValueError(f"official URL did not return PDF bytes: {url}")
    rec = _receipt_record(archive, receipt_id, url=url, content_hash=digest, byte_size=len(payload))
    artifact = archive.con.execute("SELECT stored_path FROM raw_artifact WHERE content_hash=?", (digest,)).fetchone()
    if artifact is None:
        raise ValueError(f"official PDF raw artifact is missing: {digest}")
    rel = str(artifact["stored_path"])
    raw_path = (archive.root / rel).resolve()
    if archive.root.resolve() not in raw_path.parents or _sha(raw_path.read_bytes()) != digest:
        raise ValueError("official PDF artifact path/hash differs from archive")
    return {
        "instrumentId": instrument["instrumentId"], "code": instrument["code"],
        "periodEnd": period_end, "announcementId": announcement_id,
        "documentUrl": url, "title": title,
        "indexPage": index_page, "indexReceiptId": index_receipt_id,
        "indexResponseHash": index_response_hash,
        "indexFirstSeenAt": (_receipt(archive, index_receipt_id) or {}).get("first_seen_at"),
        "rawArtifactPath": str(raw_path), "contentHash": digest,
        "byteSize": len(payload), "headerHex": payload[:12].hex(),
        "mediaType": "application/pdf", "status": "COLLECTED",
        "firstSeenAt": rec["first_seen_at"], "receipt": rec,
    }


def _full_report_matches(index) -> list:
    rows = [item for item in index.matches
            if item.announcement.url and "摘要" not in item.announcement.title
            and "英文" not in item.announcement.title]
    return sorted(rows, key=lambda item: (item.announcement.announcement_time_ms,
                                          item.announcement.announcement_id))


def capture_company(*, instrument: dict, client: CninfoClient,
                    archive: ForwardArchive, through: date) -> dict:
    code = instrument["code"]
    began = _now()
    row: dict = {
        **instrument,
        "status": "INDEX_FAILED",
        "startedAt": began.isoformat(),
        "captureThroughDate": through.isoformat(),
        "allCategoryComplete": False,
        "allCategoryError": None,
        "requiredPeriods": [],
        "periodReportIndexes": {},
        "documentRecords": [],
        "revisionCandidates": [],
    }
    try:
        company_index = client.cross_category_index(
            stock_code=code, report_period=EARLIEST_PERIOD.isoformat(), through=through,
            page_size=30, max_pages=100,
        )
        row.update({
            "organizationId": company_index.organization_id,
            "orgLookupReceiptId": company_index.org_lookup_receipt_id,
            "orgLookupContentHash": company_index.org_lookup_content_hash,
            "publicationWindow": [day.isoformat() for day in company_index.publication_window],
            "allCategoryPages": _index_pages(company_index, archive),
            "allCategoryTermination": company_index.termination,
            "allCategoryTotalAnnouncement": company_index.total_announcement,
            "allCategoryComplete": company_index.complete,
            "allCategoryError": company_index.error,
            "allCategoryAnnouncementCount": len(company_index.announcements),
        })
        row["allCategoryFirstSeenAt"] = min(
            (page["firstSeenAt"] for page in row["allCategoryPages"] if page.get("firstSeenAt")),
            default=None,
        )
        row["allCategoryObservedAt"] = max(
            (page["firstSeenAt"] for page in row["allCategoryPages"] if page.get("firstSeenAt")),
            default=None,
        )
        corrections = []
        for item in company_index.candidates:
            reasons = list(item.candidate_reasons)
            if any(reason in {"correction_title", "revision_title", "supplement_title",
                              "accounting_restatement_title"} for reason in reasons):
                corrections.append({
                    "announcementId": item.announcement_id, "title": item.title,
                    "documentUrl": item.document_url, "candidateReasons": reasons,
                    "page": item.page_num, "pageReceiptId": item.page_receipt_id,
                    "pageResponseHash": item.page_content_hash,
                })
        row["revisionCandidates"] = corrections
        if not company_index.complete:
            row["status"] = "ALL_CATEGORY_INDEX_INCOMPLETE"
            row["finishedAt"] = _now().isoformat()
            return row

        disclosed_periods = _candidate_periods(company_index.announcements, through=through)
        if not disclosed_periods:
            row["status"] = "NO_DISCLOSED_S2_PERIOD"
            row["finishedAt"] = _now().isoformat()
            return row
        latest = max(disclosed_periods)
        demands = required_s2_periods(latest)
        required = sorted({period for periods in demands.values() for period in periods})
        row.update({
            "latestPeriodEnd": latest.isoformat(),
            "disclosedPeriodEnds": [period.isoformat() for period in disclosed_periods],
            "requiredMetricPeriodCount": sum(len(periods) for periods in demands.values()),
            "requiredPeriods": [period.isoformat() for period in required],
        })

        selected_by_key: dict[str, dict] = {}
        all_full_records: list[dict] = []
        period_statuses: list[bool] = []
        for period_end in required:
            period = parse_report_period(period_end.isoformat())
            window_end = min(through, period.publication_window[1])
            try:
                result = client.report_index(
                    stock_code=code, report_period=period_end.isoformat(), page_size=30,
                    max_pages=100, through=window_end, organization_id=company_index.organization_id,
                )
            except Exception as exc:  # isolate a failed period from its company
                row["periodReportIndexes"][period_end.isoformat()] = {
                    "periodEnd": period_end.isoformat(), "complete": False,
                    "termination": "exception", "error": f"{type(exc).__name__}: {exc}"[:250],
                    "pages": [], "matches": [],
                }
                period_statuses.append(False)
                continue
            pages = _index_pages(result, archive)
            full_matches = _full_report_matches(result)
            period_index = {
                "periodEnd": period_end.isoformat(),
                "complete": result.complete, "termination": result.termination,
                "error": result.error, "organizationId": result.organization_id,
                "orgLookupReceiptId": result.org_lookup_receipt_id,
                "publicationWindow": [day.isoformat() for day in result.publication_window],
                "firstSeenAt": min((p["firstSeenAt"] for p in pages if p.get("firstSeenAt")), default=None),
                "observedAt": max((p["firstSeenAt"] for p in pages if p.get("firstSeenAt")), default=None),
                "totalAnnouncement": result.total_announcement,
                "pages": pages,
                "matches": [{
                    "announcementId": hit.announcement.announcement_id,
                    "secCode": hit.announcement.sec_code,
                    "secName": hit.announcement.sec_name,
                    "title": hit.announcement.title,
                    "announcementTimeMs": hit.announcement.announcement_time_ms,
                    "announcedOn": hit.announcement.announcement_date_cn,
                    "documentUrl": hit.announcement.url,
                    "page": hit.page_num, "pageReceiptId": hit.page_receipt_id,
                    "pageContentHash": hit.page_content_hash,
                    "isSummary": "摘要" in hit.announcement.title,
                } for hit in result.matches],
                "fullReportCandidateCount": len(full_matches),
            }
            row["periodReportIndexes"][period_end.isoformat()] = period_index
            period_statuses.append(result.complete)
            period_records = []
            for hit in full_matches:
                announcement = hit.announcement
                try:
                    record = _document_record(
                        archive=archive, client=client, instrument=instrument,
                        period_end=period_end.isoformat(),
                        announcement_id=announcement.announcement_id,
                        title=announcement.title, url=announcement.url,
                        index_page=hit.page_num, index_receipt_id=hit.page_receipt_id,
                        index_response_hash=hit.page_content_hash,
                    )
                    record["status"] = "COLLECTED"
                    period_records.append(record)
                    all_full_records.append(record)
                except Exception as exc:  # preserve each report-period failure
                    row.setdefault("documentFailures", []).append({
                        "instrumentId": instrument["instrumentId"], "code": code,
                        "periodEnd": period_end.isoformat(),
                        "announcementId": announcement.announcement_id,
                        "documentUrl": announcement.url,
                        "title": announcement.title,
                        "status": "PDF_CAPTURE_FAILED",
                        "error": f"{type(exc).__name__}: {exc}"[:250],
                    })
            if period_records:
                current = max(period_records, key=lambda x: (
                    int(next(hit.announcement.announcement_time_ms for hit in full_matches
                             if hit.announcement.announcement_id == x["announcementId"])),
                    x["announcementId"],
                ))
                selected_by_key[period_end.isoformat()] = current
            elif result.complete:
                selected_by_key[period_end.isoformat()] = {
                    "instrumentId": instrument["instrumentId"], "code": code,
                    "periodEnd": period_end.isoformat(), "status": "FULL_REPORT_MISSING",
                    "reason": "complete regular index has no non-summary full-report PDF URL",
                }
        # Preserve every discovered period full-report version. The admission
        # packet selects one latest full report per security-period and exposes
        # alternates separately so the unique-pair admission contract remains intact.
        row["documentRecords"] = all_full_records
        row["selectedReportRecords"] = [
            selected_by_key[key] for key in sorted(selected_by_key)
            if selected_by_key[key].get("status") == "COLLECTED"
        ]
        row["regularReportComplete"] = bool(row["periodReportIndexes"].get(latest.isoformat(), {}).get("complete"))
        latest_index = row["periodReportIndexes"].get(latest.isoformat(), {})
        row["regularReportPages"] = latest_index.get("pages", [])
        row["regularReportTermination"] = latest_index.get("termination")
        row["regularReportTotalAnnouncement"] = latest_index.get("totalAnnouncement")
        row["latestReportDocuments"] = [
            item for item in row["documentRecords"] if item.get("periodEnd") == latest.isoformat()
        ]

        # Capture independent official revision/supplement originals from the
        # exhausted shared index; these remain separate from fact promotion.
        related: set[str] = set()
        for notice in row["revisionCandidates"]:
            url = str(notice.get("documentUrl") or "")
            ann_id = str(notice.get("announcementId") or "")
            if not url or not ann_id or any(item.get("documentUrl") == url for item in all_full_records):
                continue
            if url in related:
                continue
            related.add(url)
            try:
                extra = _document_record(
                    archive=archive, client=client, instrument=instrument, period_end=None,
                    announcement_id=ann_id, title=str(notice.get("title") or ""), url=url,
                    index_page=notice.get("page"), index_receipt_id=notice.get("pageReceiptId"),
                    index_response_hash=notice.get("pageResponseHash"),
                )
                extra["documentKind"] = "RELATED_REVISION_OR_SUPPLEMENT"
                row.setdefault("relatedDocumentRecords", []).append(extra)
            except Exception as exc:
                row.setdefault("documentFailures", []).append({
                    "instrumentId": instrument["instrumentId"], "code": code,
                    "announcementId": ann_id, "documentUrl": url,
                    "status": "RELATED_NOTICE_CAPTURE_FAILED",
                    "error": f"{type(exc).__name__}: {exc}"[:250],
                })
        row["regularReportCompleteCount"] = sum(period_statuses)
        row["requiredPeriodCount"] = len(required)
        complete_full = len(row["selectedReportRecords"]) == len(required)
        row["status"] = (
            "INDEXED_AND_FULL_REPORTS_COLLECTED" if all(period_statuses) and complete_full
            else "INDEXED_WITH_PERIOD_OR_PDF_GAPS"
        )
        row["finishedAt"] = _now().isoformat()
        return row
    except Exception as exc:  # retain a company status while allowing the shard to continue
        row["error"] = f"{type(exc).__name__}: {exc}"[:400]
        row["finishedAt"] = _now().isoformat()
        return row


def _receipt_status(archive: ForwardArchive, receipt_id: str, content_hash: str) -> bool:
    row = archive.con.execute("SELECT source_id,outcome,http_status,content_hash FROM fetch_receipt WHERE receipt_id=?",
                              (receipt_id,)).fetchone()
    return bool(row and row["source_id"] == "cninfo" and row["outcome"] == "OK"
                and row["http_status"] == 200 and row["content_hash"] == content_hash
                and archive.verify(content_hash))


def checkpoint_company_valid(company: dict, archive: ForwardArchive) -> bool:
    if company.get("allCategoryComplete") is not True or not company.get("latestPeriodEnd"):
        return False
    required = company.get("requiredPeriods") or []
    if not required or set(required) != set(company.get("periodReportIndexes", {})):
        return False
    pages = list(company.get("allCategoryPages") or [])
    for period in company.get("periodReportIndexes", {}).values():
        if period.get("complete") is not True:
            return False
        pages.extend(period.get("pages") or [])
    if not pages:
        return False
    for item in pages:
        if not item.get("responseReceiptId") or not _receipt_status(
                archive, item["responseReceiptId"], str(item.get("responseContentHash") or "")):
            return False
    for item in [*(company.get("documentRecords") or []),
                 *(company.get("relatedDocumentRecords") or [])]:
        receipt = item.get("receipt") or {}
        if not _receipt_status(archive, str(receipt.get("receipt_id") or ""),
                               str(item.get("contentHash") or "")):
            return False
    return True


def _packet(*, targets: list[dict], binding: dict, companies: list[dict],
            archive_root: Path, discovery_path: Path, pdf_path: Path,
            started_at: str, finished_at: str) -> tuple[dict, dict]:
    by_code = {row["code"]: row for row in companies}
    full_rows = [record for company in companies for record in company.get("selectedReportRecords", [])]
    failures = [
        {"instrumentId": company["instrumentId"], "code": company["code"],
         "periodEnd": period, "status": "FULL_REPORT_MISSING"}
        for company in companies for period, index in company.get("periodReportIndexes", {}).items()
        if index.get("complete") and not any(x.get("periodEnd") == period for x in company.get("selectedReportRecords", []))
    ]
    captured_keys = {(row["instrumentId"], row["periodEnd"]) for row in full_rows}
    all_companies = []
    for target in targets:
        company = by_code.get(target["code"])
        if company is None:
            all_companies.append({**target, "status": "NOT_CAPTURED_PENDING",
                                  "allCategoryComplete": False})
        else:
            all_companies.append(company)
    companies_with_all_index = sum(x.get("allCategoryComplete") is True for x in companies)
    latest_complete = sum(x.get("regularReportComplete") is True for x in companies)
    errors = sum(x.get("status") not in {"INDEXED_AND_FULL_REPORTS_COLLECTED"} for x in companies)
    periods = sorted({period for x in companies for period in x.get("requiredPeriods", [])})
    target_count = sum(len(x.get("requiredPeriods", [])) for x in companies)
    discovery = {
        "schema": DISCOVERY_SCHEMA,
        "sourcePriorityFile": binding["priorityPath"],
        "sourcePoolPath": binding["poolPath"], "sourcePoolSha256": binding["poolSha256"],
        "sourceFollowupPath": binding["followupPath"], "sourceFollowupSha256": binding["followupSha256"],
        "sourceSnapshotId": "snap-eod-2026-10-12-20e83f1611314b79",
        "sourceSeriesId": "fwd-02c6d5013c1914d397871086",
        "scope": [x["code"] for x in targets], "scopeSize": len(targets),
        "capturedScope": [x["code"] for x in companies], "capturedCount": len(companies),
        "allCategoryCompleteCount": companies_with_all_index,
        "latestRegularReportCompleteCount": latest_complete,
        "failureCount": errors,
        "maxPagesPerIndex": 100, "pageSize": 30,
        "startPeriod": EARLIEST_PERIOD.isoformat(),
        "observedThroughAtBeijing": datetime.fromisoformat(finished_at).astimezone(BEIJING).isoformat(),
        "observedThroughBeijingDate": datetime.fromisoformat(finished_at).astimezone(BEIJING).date().isoformat(),
        "startedAt": started_at, "finishedAt": finished_at,
        "companies": all_companies,
        "boundary": "Official CNINFO indexes and original PDF bytes only. Each page has an archived receipt and hash. Exact archived requests are replayed only when the request scope is still complete for this observation horizon. No financial facts are extracted or promoted.",
    }
    for company in discovery["companies"]:
        if company.get("status") == "NOT_CAPTURED_PENDING":
            continue
        company.setdefault("latestReportDocuments", [])
        company.setdefault("periodReportIndexes", {})
        company.setdefault("requiredPeriods", [])
    packet = {
        "schema": PDF_SCHEMA,
        "archiveDbPath": str((archive_root / "meta.sqlite").resolve()),
        "archiveRoot": str(archive_root.resolve()),
        "sourceDiscoveryPath": str(discovery_path.resolve()),
        "sourceDiscoverySha256": None,
        "sourcePeriodIndexesPath": str(discovery_path.resolve()),
        "sourcePeriodIndexesSha256": None,
        "capturedFrom": min((stamp for x in companies for stamp in _all_page_receipt_times(x)), default=None),
        "capturedTo": max((stamp for x in companies for stamp in _all_page_receipt_times(x)), default=None),
        "scopeSize": len(targets), "capturedScopeSize": len(companies),
        "periods": periods, "targetCount": target_count,
        "counts": {"COLLECTED": len(full_rows), "FAILED": len(failures),
                   "PENDING": max(0, target_count - len(companies) * 0 - len(full_rows) - len(failures))},
        "pdfRecords": full_rows,
        "relatedDocumentRecords": [record for x in companies for record in x.get("relatedDocumentRecords", [])],
        "failures": failures,
        "scopeBinding": binding,
        "boundary": "Non-summary official periodic-report PDFs are archived as source candidates. Distinct revised originals and correction/supplement notices are preserved in relatedDocumentRecords. Raw bytes and SQL receipts are the source evidence; no field extraction or promotion occurs here.",
    }
    return discovery, packet


def run(*, pool_path: Path, followup_path: Path, priority_path: Path,
        archive_root: Path, discovery_path: Path, pdf_path: Path,
        checkpoint_path: Path, codes: list[str], through: date,
        env_file: Path) -> dict:
    targets, binding = load_targets(pool_path, followup_path, priority_path)
    codes = codes or [item["code"] for item in targets]
    if len(codes) != len(set(codes)) or not set(codes) <= {item["code"] for item in targets}:
        raise ValueError("requested shard contains duplicate or out-of-scope codes")
    trusted_networks = _trusted_env_file(env_file)
    os.environ[TRUST_ENV] = trusted_networks
    binding = {**binding, "archiveRoot": str(archive_root.resolve()),
               "observationDate": through.isoformat()}
    if checkpoint_path.exists():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint.get("schema") != CHECKPOINT_SCHEMA or checkpoint.get("binding") != binding:
            raise ValueError("capture checkpoint binding differs from frozen inputs/date")
        completed = dict(checkpoint.get("companies") or {})
    else:
        completed = {}
    started = _now().isoformat()
    archive_root.mkdir(parents=True, exist_ok=True)
    with closing(connect(archive_root / "meta.sqlite")) as con:
        archive = ForwardArchive(con, archive_root)
        client = ArchiveFirstCninfoClient(archive, observed_day=through)
        for index, code in enumerate(codes, 1):
            instrument = next(item for item in targets if item["code"] == code)
            existing = completed.get(code)
            if existing and checkpoint_company_valid(existing, archive):
                print(json.dumps({"code": code, "status": "REUSED_CHECKPOINT",
                                  "latestPeriodEnd": existing.get("latestPeriodEnd")}, ensure_ascii=False), flush=True)
                continue
            row = capture_company(instrument=instrument, client=client, archive=archive, through=through)
            completed[code] = row
            _write_json(checkpoint_path, {"schema": CHECKPOINT_SCHEMA, "binding": binding,
                                          "startedAt": started, "updatedAt": _now().isoformat(),
                                          "companies": completed})
            companies = [completed[item["code"]] for item in targets if item["code"] in completed]
            current = _now().isoformat()
            discovery, packet = _packet(
                targets=targets, binding=binding, companies=companies,
                archive_root=archive_root, discovery_path=discovery_path,
                pdf_path=pdf_path, started_at=started, finished_at=current,
            )
            packet["sourceDiscoverySha256"] = _sha(json.dumps(
                discovery, ensure_ascii=False, indent=2, default=str).encode("utf-8") + b"\n")
            packet["sourcePeriodIndexesSha256"] = packet["sourceDiscoverySha256"]
            _write_json(discovery_path, discovery)
            _write_json(pdf_path, packet)
            print(json.dumps({"shardIndex": index, "shardCount": len(codes), "code": code,
                              "status": row.get("status"), "latestPeriodEnd": row.get("latestPeriodEnd"),
                              "requiredPeriodCount": row.get("requiredPeriodCount"),
                              "periodsComplete": row.get("regularReportCompleteCount"),
                              "pdfCount": len(row.get("selectedReportRecords", [])),
                              "allCategoryObservedAt": row.get("allCategoryObservedAt"),
                              "error": row.get("error")}, ensure_ascii=False), flush=True)
            if client.breaker.is_open:
                break
    return {"checkpoint": str(checkpoint_path.resolve()),
            "discovery": str(discovery_path.resolve()), "pdfPacket": str(pdf_path.resolve()),
            "completedCodes": sorted(completed),
            "requestedCodes": codes}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", type=Path, default=POOL_DEFAULT)
    parser.add_argument("--followup", type=Path, default=FOLLOWUP_DEFAULT)
    parser.add_argument("--priority", type=Path, default=PRIORITY_DEFAULT)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_DEFAULT)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--pdf-packet", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--stock", action="append", default=[], help="repeatable exact six-digit code")
    parser.add_argument("--through", type=date.fromisoformat,
                        default=datetime.now(BEIJING).date())
    parser.add_argument("--env-file", type=Path, default=ROOT / "real-trial.env")
    args = parser.parse_args(argv)
    try:
        report = run(pool_path=args.pool, followup_path=args.followup, priority_path=args.priority,
                     archive_root=args.archive_root, discovery_path=args.discovery,
                     pdf_path=args.pdf_packet, checkpoint_path=args.checkpoint,
                     codes=args.stock, through=args.through, env_file=args.env_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"capture failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
