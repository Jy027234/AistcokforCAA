"""Private, finite 600578 H125 four-field CURRENT -> H126 PRIOR chain.

This profile preserves the existing H125 impact assessment (AFFECTS / NOT_ADMITTED)
and records only the later H126 report's explicit adjusted PRIOR values.  It does
not attribute those changes to a particular correction notice.
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
from typing import Mapping, Sequence
from urllib.parse import parse_qs, urlsplit

from ...artifact_paths import resolve_archive_member
from ...domain.data.db import connect
from ...domain.data.pit import AvailabilityBasis, PitMode, TimestampPrecision, date_only_available_at
from ...domain.data.rights import REGISTER_VERSION, default_rights
from ...domain.fundamentals.fact_repository import FactReviewBundle, FinancialFactRepository
from ...domain.fundamentals.versioned import FinancialFact, ProfitScope, StatementScope
from . import cninfo_statement_geometry as geometry
from . import cninfo_statement_sections as sections
from . import s2_calendar_evidence, s2_machine_verification, s2_revision_impact as impact
from .cninfo import parse_report_period, title_matches_period
from .pdf_promotion import _next_available_snapshot, _utc_now
from pypdf import PdfReader


SCHEMA = "aquant.cninfo.600578-h125-four-field-presented-prior-chain.v1"
VERIFIER = "machine:cninfo-600578-h125-four-field-presented-prior-chain-v1"
REVIEW_PREFIX = "cninfo_machine_600578_h125_four_field_chain_"
STOCK_CODE = "600578"
INSTRUMENT_ID = "600578"
ISSUER = "SH.600578"
COMPANY = "北京京能电力股份有限公司"
IMPACT_COMPANY_SHORT_NAME = "京能电力"
ORGANIZATION_ID = "gssh0600578"
FACT_PERIOD = date(2025, 6, 30)
PRESENTING_PERIOD = date(2026, 6, 30)
PRESENTING_ID = "1225491235"
ORIGINAL_ID = "1224562024"
METRICS = (
    "net_profit_attributable", "net_profit_consolidated",
    "operating_cashflow", "revenue",
)
STATEMENT_FOR = {
    "net_profit_attributable": "利润表",
    "net_profit_consolidated": "利润表",
    "operating_cashflow": "现金流量表",
    "revenue": "利润表",
}
PROFIT_SCOPE_FOR = {
    "net_profit_attributable": ProfitScope.ATTRIBUTABLE,
    "net_profit_consolidated": ProfitScope.CONSOLIDATED,
    "operating_cashflow": None,
    "revenue": None,
}
REVISION_IDS = (
    "1221522571", "1224671295", "1224671298", "1224671301", "1224671303",
    "1224671308", "1224671309", "1225314140", "1225429005", "1225491193",
    "1225491275",
)
REVISION_DISPOSITION_KIND = {
    "1221522571": "GOVERNANCE_COMMITTEE_SCOPE",
    "1224671295": "GOVERNANCE_BOARD_RULES",
    "1224671298": "GOVERNANCE_BOARD_RULES_DRAFT",
    "1224671301": "GOVERNANCE_SHAREHOLDER_MEETING_RULES",
    "1224671303": "GOVERNANCE_SHAREHOLDER_MEETING_RULES_DRAFT",
    "1224671308": "GOVERNANCE_CHARTER_AND_SUPERVISORY_STRUCTURE",
    "1224671309": "GOVERNANCE_CHARTER_DRAFT",
    "1225314140": "MARKET_NOTICE_TITLE_ONLY_CORRECTION",
    "1225429005": "FY2023_RESTATEMENT_H125_CAUSALITY_UNKNOWN",
    "1225491193": "GOVERNANCE_CHARTER_AMENDMENT",
    "1225491275": "GOVERNANCE_CHARTER_DRAFT",
}
REPORT_BINDINGS = {
    "ORIGINAL_CURRENT": {
        "announcementId": ORIGINAL_ID,
        "receiptId": "rcpt_378f23a6ee80979231ce462d",
        "contentHash": "sha256:3364f3e7e9408d18b337834e011eaa5b9a0dd8301470ceca61710e4f9184101a",
        "sourceReportPeriodEnd": "2025-06-30", "factPeriodEnd": "2025-06-30",
        "columnRole": "CURRENT",
    },
    "PRESENTED_PRIOR": {
        "announcementId": PRESENTING_ID,
        "receiptId": "rcpt_034ed0ab8915ed5ce7d491e7",
        "contentHash": "sha256:c61ffc6f3816d5b523059ae78f9e6206c2aee35e9d4b605c7bfaaf9fe0bac660",
        "sourceReportPeriodEnd": "2026-06-30", "factPeriodEnd": "2025-06-30",
        "columnRole": "PRIOR",
    },
}


class H125FourFieldChainError(ValueError):
    """The exact 600578 H125 chain did not satisfy its source or lineage contract."""


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _version_id(metric: str, role: str, *, predecessor_id: str | None = None) -> str:
    if metric not in METRICS:
        raise H125FourFieldChainError("metric is outside the four-field H125 profile")
    if role == "ORIGINAL_CURRENT":
        prefix = "mach_"
        identity = f"cninfo|{STOCK_CODE}|{FACT_PERIOD}|{metric}|{ORIGINAL_ID}|{REPORT_BINDINGS[role]['contentHash']}"
    elif role == "PRESENTED_PRIOR":
        if not predecessor_id:
            raise H125FourFieldChainError("presented PRIOR must supersede the original CURRENT")
        prefix = "mach_cmp_"
        identity = (f"cninfo|{STOCK_CODE}|{PRESENTING_PERIOD}|{FACT_PERIOD}|PRIOR|{metric}|"
                    f"{PRESENTING_ID}|{REPORT_BINDINGS[role]['contentHash']}")
    else:
        raise H125FourFieldChainError("unsupported version role")
    return prefix + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _receipt(archive, receipt_id: str, expected_hash: str, expected_url: str) -> tuple[dict, bytes]:
    row, body = impact._receipt(
        archive.con, Path(archive.root), receipt_id,
        expected_hash=expected_hash, expected_url=expected_url,
    )
    if _digest(body) != expected_hash or len(body) != row["byte_size"]:
        raise H125FourFieldChainError("receipt body hash/size mismatch: " + receipt_id)
    return row, body


def _normalize_impact_input_clocks(archive, original_input: Mapping) -> tuple[dict, list[dict]]:
    """Derive the verifier's exact receipt-clock spelling without changing instants."""
    derived = copy.deepcopy(original_input)
    company = derived.get("sourceOnlyCompany")
    if not isinstance(company, Mapping):
        raise H125FourFieldChainError("original impact company input is missing")
    changes = []

    def normalize(receipt_id: str, field: str, packet_text: str, archive_text: str,
                  source_kind: str) -> str:
        try:
            packet_time = datetime.fromisoformat(packet_text)
            archive_time = datetime.fromisoformat(archive_text)
        except (TypeError, ValueError) as exc:
            raise H125FourFieldChainError("impact input contains an invalid receipt clock") from exc
        if (packet_time.tzinfo is None or packet_time.utcoffset() is None or
                archive_time.tzinfo is None or archive_time.utcoffset() is None or
                packet_time != archive_time):
            raise H125FourFieldChainError("impact input clock differs from the archived instant: " + receipt_id)
        if packet_text != archive_text:
            changes.append({
                "sourceKind": source_kind, "receiptId": receipt_id, "field": field,
                "packetText": packet_text, "archiveText": archive_text,
                "sameAwareInstant": True,
            })
        return archive_text

    for page in company.get("allCategoryPages", ()):
        receipt_id = page["responseReceiptId"]
        row = archive.con.execute(
            "SELECT first_seen_at FROM fetch_receipt WHERE receipt_id=?", (receipt_id,)
        ).fetchone()
        if row is None:
            raise H125FourFieldChainError("impact index receipt is absent: " + receipt_id)
        page["firstSeenAt"] = normalize(
            receipt_id, "firstSeenAt", page["firstSeenAt"], row["first_seen_at"],
            "cross_category_index_page",
        )

    report_docs = [*company.get("selectedReportRecords", ()),
                   *company.get("relatedDocumentRecords", ())]
    for doc in report_docs:
        receipt = doc["receipt"]
        receipt_id = receipt["receipt_id"]
        row = archive.con.execute(
            "SELECT requested_at,responded_at,first_seen_at FROM fetch_receipt WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        if row is None:
            raise H125FourFieldChainError("impact report receipt is absent: " + receipt_id)
        for field in ("requested_at", "responded_at", "first_seen_at"):
            receipt[field] = normalize(
                receipt_id, field, receipt[field], row[field], "report_pdf_receipt",
            )
    if archive.con.total_changes:
        raise H125FourFieldChainError("impact input clock normalization changed the source archive")
    return derived, changes


def _verify_impact_input_binding(archive, impact_input: Mapping,
                                 original_impact_input: Mapping,
                                 provenance: Mapping) -> dict:
    if provenance.get("schema") != "aquant.600578-h125-impact-input-clock-normalization.v1":
        raise H125FourFieldChainError("impact input normalization provenance schema mismatch")
    original_hash = _digest(_canonical(original_impact_input).encode("utf-8"))
    derived_hash = _digest(_canonical(impact_input).encode("utf-8"))
    expected_derived, changes = _normalize_impact_input_clocks(archive, original_impact_input)
    if (provenance.get("originalCanonicalSha256") != original_hash or
            provenance.get("derivedCanonicalSha256") != derived_hash or
            provenance.get("normalizations") != changes or expected_derived != impact_input):
        raise H125FourFieldChainError("impact input is not the exact archived-clock derivation")
    return {"originalCanonicalSha256": original_hash,
            "derivedCanonicalSha256": derived_hash,
            "normalizationCount": len(changes)}


def _read_only_archive(database_path: str, root: str):
    root_path = Path(root).resolve()
    database = Path(database_path).resolve()
    if database != root_path / "meta.sqlite":
        raise H125FourFieldChainError("source archive must be its declared meta.sqlite")
    con = connect(database, read_only=True)
    con.execute("PRAGMA query_only=ON")
    if con.execute("PRAGMA query_only").fetchone()[0] != 1:
        con.close()
        raise H125FourFieldChainError("archive connection is not query-only")
    con.execute("BEGIN")
    return _ReadOnlyArchive(root_path, con)


class _ReadOnlyArchive:
    def __init__(self, root: Path, con):
        self.root, self.con = root, con

    def close(self):
        try:
            if self.con.in_transaction:
                self.con.execute("ROLLBACK")
        finally:
            self.con.close()

    def load_bytes(self, digest: str) -> bytes:
        row = self.con.execute(
            "SELECT stored_path,byte_size FROM raw_artifact WHERE content_hash=?", (digest,)
        ).fetchone()
        if row is None:
            raise H125FourFieldChainError("source artifact missing: " + digest)
        path = resolve_archive_member(self.root, row["stored_path"])
        raw = path.read_bytes()
        if _digest(raw) != digest or len(raw) != row["byte_size"]:
            raise H125FourFieldChainError("source artifact hash/size mismatch: " + digest)
        return raw

    def verify(self, digest: str) -> bool:
        """Match ForwardArchive.verify using this connection's read-only bytes."""
        try:
            self.load_bytes(digest)
        except (OSError, sqlite3.Error, KeyError, ValueError):
            return False
        return True


def _report(candidate: Mapping, role: str) -> Mapping:
    value = candidate.get("reports", {}).get(role)
    expected = REPORT_BINDINGS[role]
    if not isinstance(value, Mapping) or any(value.get(k) != v for k, v in expected.items()):
        raise H125FourFieldChainError("fixed H125/H126 source binding mismatch: " + role)
    return value


def _replay_revision_impact(archive, impact_input: Mapping, impact_proof: Mapping,
                            original_impact_input: Mapping, impact_input_provenance: Mapping) -> dict:
    binding = _verify_impact_input_binding(
        archive, impact_input, original_impact_input, impact_input_provenance,
    )
    company = impact_input.get("sourceOnlyCompany")
    if not isinstance(company, Mapping):
        raise H125FourFieldChainError("original complete impact source scope is missing")
    if (company.get("code") != STOCK_CODE or company.get("instrumentId") != ISSUER or
            company.get("name") != IMPACT_COMPANY_SHORT_NAME or
            company.get("publicationWindow") != ["2024-06-30", "2026-10-27"]):
        raise H125FourFieldChainError("original impact scope is outside the fixed issuer/window")
    try:
        impact.replay_revision_impact(
            impact_proof, archive_root=archive.root, company=company,
            target_period_end=FACT_PERIOD, target_metrics=METRICS, issuer_name=COMPANY,
        )
    except (OSError, sqlite3.Error, KeyError, AttributeError, TypeError, ValueError,
            ArithmeticError) as exc:
        raise H125FourFieldChainError("original H125 impact/source replay failed: " + str(exc)) from exc
    outcomes = {x["metric"]: x for x in impact_proof.get("outcomes", ())}
    if (set(outcomes) != set(METRICS) or any(
            x.get("status") != "AFFECTS" or x.get("admissionStatus") != "NOT_ADMITTED" or
            x.get("impactProof") is not None for x in outcomes.values())):
        raise H125FourFieldChainError("original AFFECTS/NOT_ADMITTED statuses were changed")
    scope = impact_proof.get("relationshipScope", {})
    if (scope.get("completeForArchivedIndexResponse") is not True or
            set(scope.get("allRecalledRevisionIds", ())) != set(REVISION_IDS)):
        raise H125FourFieldChainError("original impact assessment does not cover the fixed 11 revision IDs")
    return {"outcomes": outcomes, "relationshipScope": impact_proof["relationshipScope"],
            "impactInputBinding": binding}


def _verify_current_company(archive, impact_input: Mapping, snapshot: Mapping) -> tuple[dict, dict]:
    observation_day = snapshot.get("through")
    if observation_day not in ("2026-11-02", "2026-11-03"):
        raise H125FourFieldChainError("current index observation is outside the captured Nov 2/3 windows")
    cross = snapshot.get("cross_category")
    if not isinstance(cross, Mapping) or (
            cross.get("publication_window") != ["2024-06-30", observation_day] or
            cross.get("complete") is not True or cross.get("termination") != "has_more_false" or
            cross.get("total_announcement") != 226 or len(cross.get("pages", ())) != 8):
        raise H125FourFieldChainError("current 226-row cross-category index is incomplete for its declared day")
    old = impact_input["sourceOnlyCompany"]
    company = copy.deepcopy(old)
    company["publicationWindow"] = cross["publication_window"]
    company["orgLookupReceiptId"] = cross["org_lookup_receipt_id"]
    company["orgLookupContentHash"] = cross["org_lookup_content_hash"]
    company["allCategoryTermination"] = cross["termination"]
    company["allCategoryTotalAnnouncement"] = cross["total_announcement"]
    pages = []
    for number, page in enumerate(cross["pages"], 1):
        rec = archive.con.execute(
            "SELECT first_seen_at FROM fetch_receipt WHERE receipt_id=?", (page["receipt_id"],)
        ).fetchone()
        if rec is None or page.get("page_num") != number:
            raise H125FourFieldChainError("current cross-index receipt/page binding is incomplete")
        if observation_day == "2026-11-03":
            first_seen = datetime.fromisoformat(str(rec["first_seen_at"]).replace("Z", "+00:00"))
            if (first_seen.tzinfo is None or
                    first_seen.astimezone(impact.CST).date().isoformat() != observation_day):
                raise H125FourFieldChainError("Nov3 current cross-index receipt was not first-seen on Nov3 Beijing time")
        pages.append({
            "page": number, "responseReceiptId": page["receipt_id"],
            "responseContentHash": page["content_hash"],
            "requestBodyHash": page["request_body_hash"],
            "httpStatus": page["http_status"],
            "rawAnnouncementCount": page["raw_announcement_count"],
            "totalAnnouncement": page["total_announcement"],
            "hasMore": page["has_more"], "firstSeenAt": rec["first_seen_at"],
        })
    company["allCategoryPages"] = pages
    entries, receipts, request_hashes = impact._index(archive.con, archive.root, company)
    saved = cross.get("announcements", ())
    by_id = {str(item["announcement_id"]): item for item in saved}
    if len(by_id) != len(saved) or set(by_id) != set(entries) or len(entries) != 226:
        raise H125FourFieldChainError("current cross-index rows do not close against archived responses")
    page_by_receipt = {p["receipt_id"]: p for p in cross["pages"]}
    for aid, entry in entries.items():
        row = by_id[aid]
        p = page_by_receipt.get(entry["indexReceiptId"])
        if (p is None or row.get("sec_code") != STOCK_CODE or
                impact.normalize_title(row.get("title", "")) != entry["title"] or
                row.get("document_url") != entry["documentUrl"] or
                datetime.fromtimestamp(int(row["announcement_time_ms"]) / 1000, impact.CST).date().isoformat()
                != entry["publishedDate"] or
                row.get("page_receipt_id") != entry["indexReceiptId"] or
                row.get("page_content_hash") != entry["indexResponseHash"] or
                row.get("page_num") != p["page_num"]):
            raise H125FourFieldChainError("current cross-index source row differs: " + aid)
    fixed = set(REVISION_IDS)
    reasons = {aid: set(row.get("candidate_reasons") or ()) for aid, row in by_id.items()}
    observed_candidates = {aid for aid, value in reasons.items() if value & {
        "revision_title", "correction_title", "accounting_restatement_title"}}
    if observed_candidates != fixed:
        raise H125FourFieldChainError("current complete index has a missing or extra H125 revision candidate")
    period = parse_report_period(cross["publication_window"][0])
    recomputed = set()
    for aid, entry in entries.items():
        flags = impact.title_revision_flags(entry["title"])
        expected_reasons = tuple(reason for reason, active in (
            ("period_title", title_matches_period(entry["title"], period)),
            ("correction_title", flags[0]), ("revision_title", flags[1]),
            ("supplement_title", flags[2]),
            ("accounting_restatement_title", any(term in entry["title"]
                for term in ("会计差错", "追溯调整", "重述"))),
        ) if active)
        if tuple(by_id[aid].get("candidate_reasons") or ()) != expected_reasons:
            raise H125FourFieldChainError("current index title-reason classifier differs: " + aid)
        if any(reason in {"correction_title", "revision_title", "supplement_title",
                          "accounting_restatement_title"} for reason in expected_reasons):
            recomputed.add(aid)
    if recomputed != fixed:
        raise H125FourFieldChainError("current index title classifier does not reproduce exact 11 candidates")
    if entries.get("1225642733", {}).get("title") != "京能电力：2026年第三季度报告":
        raise H125FourFieldChainError("current index Q3 report is missing")
    return company, {
        "complete": True, "announcementCount": len(entries), "pageCount": len(cross["pages"]),
        "receiptIds": [x["receipt_id"] for x in receipts],
        "requestBodyHashes": request_hashes, "revisionCandidateIds": sorted(observed_candidates),
    }


def _verify_org_lookup(archive, receipt_id: str, content_hash: str) -> None:
    url = "https://www.cninfo.com.cn/new/information/topSearch/query?keyWord=600578&maxNum=10"
    row, body = _receipt(archive, receipt_id, content_hash, url)
    matches = [x for x in json.loads(body)
               if str(x.get("code")) == STOCK_CODE and str(x.get("orgId")) == ORGANIZATION_ID]
    if len(matches) != 1 or matches[0].get("zwjc") != "京能电力":
        raise H125FourFieldChainError("official issuer lookup differs")


def _verify_regular_index(archive, source: Mapping, required_id: str) -> dict:
    if (source.get("stock_code") != STOCK_CODE or source.get("organization_id") != ORGANIZATION_ID or
            source.get("complete") is not True or source.get("termination") != "has_more_false" or
            source.get("error") is not None):
        raise H125FourFieldChainError("regular report index is incomplete")
    _verify_org_lookup(archive, source["org_lookup_receipt_id"], source["org_lookup_content_hash"])
    total = None
    count = 0
    ids = set()
    actual = {}
    receipt_ids, request_hashes = [], []
    for number, page in enumerate(source.get("pages", ()), 1):
        if page.get("page_num") != number:
            raise H125FourFieldChainError("regular index page sequence mismatch")
        row, raw = _receipt(archive, page["receipt_id"], page["content_hash"],
                            "http://www.cninfo.com.cn/new/hisAnnouncement/query")
        request_hash = page["request_body_hash"]
        req_raw = archive.load_bytes(request_hash)
        if f"request={request_hash}" not in (row.get("detail") or ""):
            raise H125FourFieldChainError("regular index request hash is unbound")
        query = parse_qs(req_raw.decode("utf-8"), keep_blank_values=True)
        expected = {
            "stock": [f"{STOCK_CODE},{ORGANIZATION_ID}"],
            "seDate": ["~".join(source["publication_window"])],
            "pageNum": [str(number)], "category": [source["category"]],
            "column": [source["market"]], "tabName": ["fulltext"],
        }
        if any(query.get(key) != value for key, value in expected.items()):
            raise H125FourFieldChainError("regular index query scope mismatch")
        payload = json.loads(raw)
        rows = payload.get("announcements")
        more = payload.get("hasMore")
        if isinstance(more, str):
            more = {"true": True, "false": False}.get(more.lower())
        page_total = payload.get("totalAnnouncement")
        if (not isinstance(rows, list) or type(page_total) is not int or
                len(rows) != page["raw_announcement_count"] or
                page_total != page["total_announcement"] or more != page["has_more"] or
                row["http_status"] != page["http_status"] or
                (total is not None and total != page_total)):
            raise H125FourFieldChainError("regular index response/pagination differs")
        total = page_total
        if (number < len(source["pages"]) and not more) or (number == len(source["pages"]) and more):
            raise H125FourFieldChainError("regular index termination condition differs")
        for item in rows:
            aid = str(item.get("announcementId") or "")
            if (not aid or aid in ids or str(item.get("secCode")) != STOCK_CODE or
                    str(item.get("orgId")) != ORGANIZATION_ID):
                raise H125FourFieldChainError("regular index issuer/report identity differs")
            ids.add(aid)
            day = datetime.fromtimestamp(int(item["announcementTime"]) / 1000, impact.CST).date().isoformat()
            path = str(item.get("adjunctUrl") or "").lstrip("/")
            if path != f"finalpage/{day}/{aid}.PDF":
                raise H125FourFieldChainError("regular index report URL/date binding differs")
            actual[aid] = {
                "title": impact.normalize_title(str(item.get("announcementTitle") or "")),
                "url": "https://static.cninfo.com.cn/" + path,
                "date": day,
            }
        count += len(rows)
        receipt_ids.append(row["receipt_id"])
        request_hashes.append(request_hash)
    if count != total or total != source.get("total_announcement"):
        raise H125FourFieldChainError("regular index total does not close")
    matches = source.get("matches", ())
    match_ids = {str(item["announcement"]["announcement_id"]) for item in matches}
    if required_id not in match_ids or not match_ids <= ids:
        raise H125FourFieldChainError("required report is absent from regular report index")
    for match in matches:
        announcement = match["announcement"]
        aid = str(announcement["announcement_id"])
        observed = actual.get(aid)
        if (observed is None or announcement.get("sec_code") != STOCK_CODE or
                impact.normalize_title(announcement.get("title", "")) != observed["title"] or
                announcement.get("url") != observed["url"] or
                announcement.get("announcement_date_cn") != observed["date"] or
                announcement.get("report_period") != source.get("report_period") or
                announcement.get("report_kind") is None or
                match.get("page_receipt_id") not in receipt_ids):
            raise H125FourFieldChainError("regular index matched report metadata is incomplete: " + aid)
    if sum(int(v) for v in (source.get("skipped") or {}).values()) + len(match_ids) != total:
        raise H125FourFieldChainError("regular index matches/skipped rows do not close")
    return {"complete": True, "requiredId": required_id, "total": total,
            "receiptIds": receipt_ids, "requestBodyHashes": request_hashes}


def _revision_disposition_specs() -> dict[str, tuple[str, tuple[str, ...]]]:
    # The first phrase in each tuple is anchored in the official original text.
    # These are source-scope descriptions; none is a NO_EFFECT finding.
    return {
        "1221522571": ("GOVERNANCE_COMMITTEE_SCOPE", ("战略投资与 ESG 委员会", "增加 ESG 管理工作职责")),
        "1224671295": ("GOVERNANCE_BOARD_RULES", ("董事会议事规则", "修订")),
        "1224671298": ("GOVERNANCE_BOARD_RULES_DRAFT", ("董事会的议事方式", "拟修订稿")),
        "1224671301": ("GOVERNANCE_SHAREHOLDER_MEETING_RULES", ("股东大会议事规则", "修订")),
        "1224671303": ("GOVERNANCE_SHAREHOLDER_MEETING_RULES_DRAFT", ("股东会议事规则", "拟修订稿")),
        "1224671308": ("GOVERNANCE_CHARTER_AND_SUPERVISORY_STRUCTURE", ("取消监事会", "审计与法律风险管理委员会")),
        "1224671309": ("GOVERNANCE_CHARTER_DRAFT", ("公司章程", "拟修订稿")),
        "1225314140": ("MARKET_NOTICE_TITLE_ONLY_CORRECTION", ("除标题更正外，其他内容暂无修改",)),
        "1225429005": ("FY2023_RESTATEMENT_H125_CAUSALITY_UNKNOWN", (
            "追溯调整 2023 年度财务报表", "2024 年度的追溯调整已经 2025 年度审计报告披露",
            "2023 年度合并财务报表", "经营活动产生的现金流量净额")),
        "1225491193": ("GOVERNANCE_CHARTER_AMENDMENT", (
            "新时代廉洁文化建设三年行动计划", "上市公司董事会秘书监管规则", "公司章程")),
        "1225491275": ("GOVERNANCE_CHARTER_DRAFT", ("公司章程", "2026 年 月修订稿")),
    }


def _verify_historical_nov2_index(archive, audit: Mapping, snapshot: Mapping,
                                 impact_input: Mapping) -> dict:
    """Replay the preserved Nov2 pages, then compare their rows with Nov3."""
    scope = audit.get("scope", {})
    cross_window = scope.get("crossCategoryWindow")
    if (cross_window != ["2024-06-30", "2026-11-02"] or
            scope.get("crossCategoryThrough") != "2026-11-02" or
            scope.get("completeAnnouncementCount") != 226 or
            scope.get("completePages") != 8 or scope.get("laterPageHasMore") is not False):
        raise H125FourFieldChainError("historical Nov2 cross-index scope is not the captured complete set")
    summaries = audit.get("nov2CurrentCrossCategoryReceipts", ())
    if len(summaries) != 8:
        raise H125FourFieldChainError("historical Nov2 cross-index receipt set is incomplete")

    company = copy.deepcopy(impact_input.get("sourceOnlyCompany"))
    if not isinstance(company, dict):
        raise H125FourFieldChainError("Nov3 replay lacks the bound issuer for Nov2 receipt replay")
    company["publicationWindow"] = list(cross_window)
    pages = []
    for number, item in enumerate(summaries, 1):
        request_hash = item.get("requestBodyHash")
        content_hash = item.get("contentHash")
        receipt_id = item.get("receiptId")
        detail = (f"cross-category:{STOCK_CODE}:{cross_window[0]}~{cross_window[1]}:"
                  f"page={number}:request={request_hash}")
        if (item.get("detail") != detail or not isinstance(request_hash, str) or
                not request_hash.startswith("sha256:") or not isinstance(content_hash, str) or
                not content_hash.startswith("sha256:") or not isinstance(receipt_id, str)):
            raise H125FourFieldChainError("historical Nov2 receipt identity differs")
        rec, body = _receipt(
            archive, receipt_id, content_hash,
            "http://www.cninfo.com.cn/new/hisAnnouncement/query",
        )
        try:
            obj = json.loads(body)
            rows = obj["announcements"]
            more = obj.get("hasMore")
            if isinstance(more, str):
                more = {"true": True, "false": False}.get(more.lower())
            total = obj.get("totalAnnouncement")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise H125FourFieldChainError("historical Nov2 raw index page is invalid") from exc
        if (not isinstance(rows, list) or type(total) is not int or
                item.get("responseByteSize") != len(body) or
                item.get("firstSeenAt") != rec["first_seen_at"] or
                item.get("totalAnnouncement") != total or item.get("hasMore") is not more):
            raise H125FourFieldChainError("historical Nov2 receipt metadata differs from its raw page")
        pages.append({
            "page": number, "responseReceiptId": receipt_id,
            "responseContentHash": content_hash, "requestBodyHash": request_hash,
            "httpStatus": rec["http_status"], "rawAnnouncementCount": len(rows),
            "totalAnnouncement": total, "hasMore": more,
            "firstSeenAt": rec["first_seen_at"],
        })
    company["allCategoryPages"] = pages
    company["allCategoryTermination"] = "has_more_false"
    company["allCategoryTotalAnnouncement"] = scope["completeAnnouncementCount"]
    historical_entries, _, _ = impact._index(archive.con, archive.root, company)
    current_cross = snapshot.get("cross_category", {})
    current_rows = {str(row.get("announcement_id")): row
                    for row in current_cross.get("announcements", ())}
    if (len(historical_entries) != scope["completeAnnouncementCount"] or
            len(current_rows) != len(current_cross.get("announcements", ())) or
            set(historical_entries) != set(current_rows)):
        raise H125FourFieldChainError("Nov2 historical and Nov3 current announcement identities differ")
    for aid, old in historical_entries.items():
        current = current_rows[aid]
        current_day = datetime.fromtimestamp(
            int(current["announcement_time_ms"]) / 1000, impact.CST,
        ).date().isoformat()
        if (old["title"] != impact.normalize_title(current.get("title", "")) or
                old["documentUrl"] != current.get("document_url") or
                old["publishedDate"] != current_day):
            raise H125FourFieldChainError("Nov2/Nov3 announcement semantics differ: " + aid)
    return historical_entries


def _verify_revision_source_audit(archive, audit: Mapping, snapshot: Mapping,
                                  impact_proof: Mapping, *,
                                  impact_input: Mapping | None = None) -> list[dict]:
    if audit.get("schema") != "aquant.600578-h125-revision-disposition-source-audit.v1":
        raise H125FourFieldChainError("source disposition audit schema mismatch")
    scope = audit.get("scope", {})
    if (scope.get("stockCode") != STOCK_CODE or scope.get("factPeriodEnd") != FACT_PERIOD.isoformat() or
            scope.get("crossCategoryWindow") != ["2024-06-30", "2026-11-02"] or
            scope.get("crossCategoryThrough") != "2026-11-02" or
            set(scope.get("fixedRevisionCandidateIds", ())) != set(REVISION_IDS)):
        raise H125FourFieldChainError("source disposition audit is not the exact fixed 11 candidate set")
    observation_day = snapshot.get("through")
    if observation_day not in ("2026-11-02", "2026-11-03"):
        raise H125FourFieldChainError("revision audit replay is outside the captured Nov2/3 windows")
    historical_entries = None
    if observation_day == "2026-11-03":
        if not isinstance(impact_input, Mapping):
            raise H125FourFieldChainError("Nov3 replay requires the exact issuer input for historical receipt replay")
        historical_entries = _verify_historical_nov2_index(archive, audit, snapshot, impact_input)
    binding = audit.get("candidateSetBinding", {})
    impact_summary = audit.get("impactAssessment", {})
    expected_outcomes = {x["metric"]: x for x in impact_proof.get("outcomes", ())}
    audit_outcomes = {x["metric"]: x for x in impact_summary.get("outcomes", ())}
    if (binding.get("matchesNov2FullCrossIndex") is not True or
            binding.get("matchesImpactAssessorRecalledSet") is not True or
            binding.get("allOfficialPdfReceiptsBoundAndBytesVerified") is not True or
            impact_summary.get("completeForArchivedIndexResponse") is not True or
            impact_summary.get("sourceWindow") != ["2024-06-30", "2026-10-27"] or
            set(audit_outcomes) != set(METRICS) or any(
                audit_outcomes[m].get("status") != expected_outcomes[m].get("status") or
                audit_outcomes[m].get("admissionStatus") != expected_outcomes[m].get("admissionStatus") or
                audit_outcomes[m].get("impactProof") != expected_outcomes[m].get("impactProof")
                for m in METRICS)):
        raise H125FourFieldChainError("disposition audit changed the original AFFECTS assessment")
    docs = {str(x.get("announcementId")): x for x in audit.get("candidateSources", ())}
    if set(docs) != set(REVISION_IDS):
        raise H125FourFieldChainError("source audit does not contain exactly the 11 original PDFs")
    specs = _revision_disposition_specs()
    cross_rows = {str(x["announcement_id"]): x for x in snapshot["cross_category"]["announcements"]}
    impact_sources = {str(x["announcementId"]): x for x in impact_proof.get("sources", ())}
    results = []
    for aid in REVISION_IDS:
        doc = docs[aid]
        official = doc["officialPdf"]
        current_row = cross_rows.get(aid)
        old_source = impact_sources.get(aid)
        indexed_day = None if current_row is None else datetime.fromtimestamp(
            int(current_row["announcement_time_ms"]) / 1000, impact.CST
        ).date().isoformat()
        if (current_row is None or old_source is None or
                impact.normalize_title(current_row["title"]) != impact.normalize_title(doc["title"]) or
                doc.get("publishedDate") != indexed_day or
                doc.get("candidateReasons") != list(current_row.get("candidate_reasons") or ()) or
                current_row["document_url"] != doc["officialPdfUrl"] or
                old_source.get("index", {}).get("indexReceiptId") != doc["impactIndexReceiptId"] or
                old_source.get("index", {}).get("indexResponseHash") != doc["impactIndexPageContentHash"] or
                old_source.get("receipt", {}).get("receipt_id") != doc["officialPdfReceipt"].get("receipt_id") or
                old_source.get("originalHash") != official.get("contentHash")):
            raise H125FourFieldChainError("revision source is not bound to both captured index horizons: " + aid)
        if observation_day == "2026-11-02":
            if (current_row["page_receipt_id"] != doc["nov2CrossIndexReceiptId"] or
                    current_row["page_content_hash"] != doc["nov2CrossIndexPageContentHash"]):
                raise H125FourFieldChainError("revision source Nov2 current receipt differs: " + aid)
        else:
            historical = historical_entries.get(aid) if historical_entries is not None else None
            if (historical is None or
                    historical.get("indexReceiptId") != doc["nov2CrossIndexReceiptId"] or
                    historical.get("indexResponseHash") != doc["nov2CrossIndexPageContentHash"]):
                raise H125FourFieldChainError("revision source does not retain its independently replayed Nov2 receipt: " + aid)
        expected_hash = official["contentHash"]
        rec, body = _receipt(archive, doc["officialPdfReceipt"]["receipt_id"], expected_hash,
                             doc["officialPdfUrl"])
        if (doc["officialPdfReceipt"] != rec or official.get("byteSize") != len(body) or
                not body.startswith(b"%PDF-")):
            raise H125FourFieldChainError("revision PDF receipt/raw bytes mismatch: " + aid)
        from pypdf import PdfReader
        pages = [page.extract_text() or "" for page in PdfReader(io.BytesIO(body), strict=False).pages]
        full = "\n\f\n".join(pages)
        if (len(pages) != official.get("pageCount") or
                hashlib.sha256(full.encode("utf-8")).hexdigest() != official.get("extractedTextSha256") or
                len(pages) != len(official.get("pages", ())) or
                any(text != official["pages"][i].get("text") for i, text in enumerate(pages))):
            raise H125FourFieldChainError("revision PDF native text does not replay: " + aid)
        expected_kind, phrases = specs[aid]
        flat = re.sub(r"\s+", "", full)
        if any(re.sub(r"\s+", "", phrase) not in flat for phrase in phrases):
            raise H125FourFieldChainError("revision disposition original-text scope failed: " + aid)
        if expected_kind.endswith("_DRAFT") and "拟修订稿" not in doc["title"]:
            raise H125FourFieldChainError("draft disposition is not anchored in the official index title: " + aid)
        if aid == "1225429005":
            # Explicitly retain the uncertain H125 causal edge. Its FY2023 table
            # and statement about FY2024 are source-bound; no H125 cause is claimed.
            required = ("2023年12月31日", "2023年度", "48,939,226.15", "41,585,954.38",
                        "-21,566,959.39", "2024年度的追溯调整已经2025年度审计报告披露")
            if any(item not in flat for item in required):
                raise H125FourFieldChainError("1225429005 FY2023/FY2024 scope evidence is incomplete")
            kind = "FY2023_RESTATEMENT_H125_CAUSALITY_UNKNOWN"
        else:
            kind = expected_kind
        results.append({
            "announcementId": aid, "kind": kind, "receiptId": rec["receipt_id"],
            "contentHash": expected_hash, "publishedDate": doc["publishedDate"],
            "sourceIndexReceiptId": doc["impactIndexReceiptId"],
            "currentIndexReceiptId": current_row["page_receipt_id"],
            "originalTextSha256": official["extractedTextSha256"],
            "originalTextVerified": True,
            "causalAttribution": None,
            "sourceScopeEvidence": list(phrases),
        })
    return results


def _extract_prior_grid_cells(body: bytes) -> dict:
    """Re-extract the four H126 PRIOR cells from ruled cells and dated headers."""
    import pdfplumber
    digest = _digest(body)
    if digest != REPORT_BINDINGS["PRESENTED_PRIOR"]["contentHash"] or not body.startswith(b"%PDF-"):
        raise H125FourFieldChainError("H126 prior-grid input hash/PDF signature mismatch")
    result = {"cells": {}, "sourceContentHash": digest}
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        identity = geometry._frontmatter(pdf, STOCK_CODE, PRESENTING_PERIOD)
        if identity is None:
            raise H125FourFieldChainError("H126 issuer/period frontmatter not verified by grid reader")
        titles = geometry._canonical_statement_titles(body)
        if not titles:
            for index, page in enumerate(pdf.pages):
                for line in (page.extract_text() or "").splitlines():
                    match = geometry._TITLE.fullmatch(geometry._compact(line))
                    if match:
                        titles.append((index, line, match.group(1), match.group(2)))
        located = []
        for index, line, role, statement in titles:
            matches = pdf.pages[index].search(r"\s*".join(re.escape(c) for c in re.sub(r"\s+", "", line)))
            if len(matches) == 1:
                located.append((index, float(matches[0]["top"]), float(matches[0]["bottom"]), line, role, statement))
        located.sort()
        found = {metric: [] for metric in METRICS}
        for position, (start, top, bottom, heading, role, statement) in enumerate(located):
            if role != "合并" or statement not in {"利润表", "现金流量表"}:
                continue
            end = located[position + 1][:2] if position + 1 < len(located) else (start + 8, float("inf"))
            if end[0] - start > 8:
                continue
            first = pdf.pages[start]
            preamble = first.crop((0, bottom, first.width, min(first.height, bottom + 180))).extract_text() or ""
            if bottom > first.height - 120 and start + 1 < len(pdf.pages):
                next_text = pdf.pages[start + 1].crop((0, 0, pdf.pages[start + 1].width, 160)).extract_text() or ""
                if not any(geometry._TITLE.fullmatch(geometry._compact(line)) for line in next_text.splitlines()):
                    preamble += "\n" + next_text
            units = [(line, match[1]) for line in preamble.splitlines()
                     for match in [geometry._UNIT.search(line)] if match]
            if len({u for _, u in units}) != 1:
                continue
            unit_declaration, unit = units[0]
            if unit != "元" or any(re.search(r"币种\s*[:：]\s*(?!人民币|CNY)[A-Za-z\u4e00-\u9fff]+", line)
                                    for line in preamble.splitlines()):
                continue
            context, context_page = None, None
            for page_index in range(start, min(end[0], len(pdf.pages) - 1) + 1):
                page = pdf.pages[page_index]
                for table in geometry._ruled_page_tables(page, PRESENTING_PERIOD, statement, preamble):
                    if (page_index == start and table.bbox[1] < bottom) or (page_index == end[0] and table.bbox[3] > end[1]):
                        continue
                    current = geometry._header(table, PRESENTING_PERIOD, statement, preamble, PRESENTING_PERIOD)
                    if current is not None:
                        context, context_page = current, page_index
                    if context is None:
                        continue
                    for row_index, row in enumerate(table.extract()):
                        cells = table.rows[row_index].cells
                        if len(row) != len(context["labels"]) or len(cells) != len(row):
                            continue
                        bounds = tuple(None if cell is None else (round(cell[0], 3), round(cell[2], 3)) for cell in cells)
                        if not geometry._amount_columns_match_header(
                                bounds, context, table.bbox, continuation=page_index > context_page):
                            continue
                        amount = geometry._amount(row[context["prior_column"]])
                        if amount is None:
                            continue
                        for metric in METRICS:
                            if STATEMENT_FOR[metric] != statement or not geometry._field_label(metric, row[0]):
                                continue
                            found[metric].append({
                                "field": metric, "role": "合并", "statement": statement,
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
                                "priorCellBbox": None if cells[context["prior_column"]] is None else list(cells[context["prior_column"]]),
                                "headerCells": [str(x or "") for x in context["labels"]],
                                "priorColumn": context["prior_column"],
                                "sourceContentHash": digest,
                            })
        for metric, values in found.items():
            if len(values) != 1:
                raise H125FourFieldChainError(f"H126 ruled PRIOR cell missing/ambiguous: {metric}")
            result["cells"][metric] = values[0]
    return result


def _summary_triplet(text: str, label: str) -> dict:
    flat = re.sub(r"\s+", " ", text)
    number = r"(-?\d[\d,]*\.\d{2})"
    matches = list(re.finditer(label + r"\s+" + number + r"\s+" + number + r"\s+" + number, flat))
    if len(matches) != 1:
        raise H125FourFieldChainError("H126 page-6 adjusted/before summary is absent or ambiguous")
    match = matches[0]
    return {"currentYuan": match.group(1).replace(",", ""),
            "adjustedPriorYuan": match.group(2).replace(",", ""),
            "unadjustedPriorYuan": match.group(3).replace(",", "")}


def _profit_identity(section, prior: bool) -> tuple[Decimal, Decimal, Decimal]:
    mult = sections._UNIT_MULTIPLIER[section.unit]
    rows = [section.component(name) for name in ("net_profit", "attributable_profit", "minority_profit")]
    if section.unit != "元" or any(row is None or row.nil_amounts for row in rows):
        raise H125FourFieldChainError("consolidated profit identity cells are absent")
    vals = tuple((row.prior_amount if prior else row.amount) * mult for row in rows)
    if vals[0] != vals[1] + vals[2]:
        raise H125FourFieldChainError("consolidated profit != attributable + minority")
    return vals


def _replay_pdf_values(archive, candidate: Mapping) -> dict:
    source_rows = {}
    candidates = {}
    sections_by_role = {}
    for role in ("ORIGINAL_CURRENT", "PRESENTED_PRIOR"):
        report = _report(candidate, role)
        row, body = _receipt(archive, report["receiptId"], report["contentHash"], report["documentUrl"])
        if (row["receipt_id"] != report["receiptId"] or row["first_seen_at"] != report["firstSeenAt"] or
                row["byte_size"] != report["byteSize"] or row["url"] != report["documentUrl"]):
            raise H125FourFieldChainError("H125/H126 receipt metadata differs: " + role)
        period = FACT_PERIOD if role == "ORIGINAL_CURRENT" else PRESENTING_PERIOD
        source_id = ORIGINAL_ID if role == "ORIGINAL_CURRENT" else PRESENTING_ID
        parsed = sections.extract_section_facts(
            body, instrument_id=STOCK_CODE, company=COMPANY, period_end=period,
            announcement_id=source_id, document_url=report["documentUrl"], required_fields=METRICS,
        )
        verified = s2_machine_verification.verify_machine_evidence(body, candidate=parsed, expected_fields=METRICS)
        if not verified.verified or parsed.pdf_sha256 != report["contentHash"]:
            raise H125FourFieldChainError("official H125/H126 native report verification failed: " + role)
        statements = sections.extract_statement_sections(
            body, period_end=period, roles=(sections.ROLE_CONSOLIDATED,), statements=("利润表", "现金流量表"),
        )
        candidates[role], sections_by_role[role] = parsed, statements
        source_rows[role] = {"receipt": row, "body": body, "verification": verified}

    old_candidate = candidates["ORIGINAL_CURRENT"]
    new_candidate = candidates["PRESENTED_PRIOR"]
    old_profit = sections_by_role["ORIGINAL_CURRENT"].require(sections.ROLE_CONSOLIDATED, "利润表")
    old_cash = sections_by_role["ORIGINAL_CURRENT"].require(sections.ROLE_CONSOLIDATED, "现金流量表")
    new_profit = sections_by_role["PRESENTED_PRIOR"].require(sections.ROLE_CONSOLIDATED, "利润表")
    new_cash = sections_by_role["PRESENTED_PRIOR"].require(sections.ROLE_CONSOLIDATED, "现金流量表")
    if (any(x.unit != "元" for x in (old_profit, old_cash, new_profit, new_cash)) or
            old_profit.current_header != "2025年半年度" or old_cash.current_header != "2025年半年度" or
            new_profit.current_header != "2026年半年度" or new_cash.current_header != "2026年半年度" or
            new_profit.prior_header != "2025年半年度" or new_cash.prior_header != "2025年半年度"):
        raise H125FourFieldChainError("H125/H126 report headers, unit, or consolidated scope mismatch")

    grid = _extract_prior_grid_cells(source_rows["PRESENTED_PRIOR"]["body"])
    saved_events = candidate.get("versionsByMetric", {})
    if set(saved_events) != set(METRICS):
        raise H125FourFieldChainError("candidate metric set differs")
    summary_text = PdfReader(io.BytesIO(source_rows["PRESENTED_PRIOR"]["body"]), strict=False).pages[5].extract_text() or ""
    summaries = {
        "revenue": _summary_triplet(summary_text, r"营业收入"),
        "net_profit_attributable": _summary_triplet(summary_text, r"归属于上市公司股东\s*的净利润"),
        "operating_cashflow": _summary_triplet(summary_text, r"经营活动产生的现金\s*流量净额"),
    }
    values = {}
    for metric in METRICS:
        if not isinstance(saved_events[metric], list) or len(saved_events[metric]) != 2:
            raise H125FourFieldChainError("each metric must have exact original and presented versions")
        original, presented = saved_events[metric]
        native = old_candidate.by_field[metric]
        h125_row = (old_cash if metric == "operating_cashflow" else old_profit).find(metric)
        h126_section = new_cash if metric == "operating_cashflow" else new_profit
        h126_row = h126_section.find(metric)
        grid_row = grid["cells"][metric]
        scale = sections._UNIT_MULTIPLIER[h126_section.unit]
        old_value = Decimal(str(native.value_yuan))
        new_value = h126_row.prior_amount * scale
        normalized_grid_label = geometry._compact(grid_row["rowLabel"])
        normalized_native_label = geometry._compact(h126_row.label)
        if metric == "revenue":
            note = re.search(r"([一二三四五六七八九十]+[、.][0-9]+)$", normalized_native_label)
            if note:
                normalized_native_label = normalized_native_label[:note.start()]
        if (h125_row is None or h126_row is None or Decimal(str(h125_row.amount * sections._UNIT_MULTIPLIER[(old_cash if metric == "operating_cashflow" else old_profit).unit])) != old_value or
                Decimal(grid_row["amountYuan"]) != new_value or
                Decimal(str(grid_row["amountYuan"])) != Decimal(str(h126_row.prior_amount * scale)) or
                geometry._amount(grid_row["currentCell"]) != h126_row.amount or
                geometry._amount(grid_row["priorCell"]) != h126_row.prior_amount or
                grid_row.get("role") != "合并" or grid_row.get("columnRole") != "PRIOR" or
                grid_row.get("sourceReportPeriodEnd") != PRESENTING_PERIOD.isoformat() or
                grid_row.get("factPeriodEnd") != FACT_PERIOD.isoformat() or
                grid_row.get("currentHeader") != "2026年半年度" or
                grid_row.get("priorHeader") != "2025年半年度" or
                grid_row.get("statement") != STATEMENT_FOR[metric] or
                grid_row.get("sourceContentHash") != REPORT_BINDINGS["PRESENTED_PRIOR"]["contentHash"] or
                normalized_grid_label != normalized_native_label or grid_row.get("unit") != "元"):
            raise H125FourFieldChainError("native/grid values differ for " + metric)
        if (original.get("role") != "ORIGINAL_CURRENT" or presented.get("role") != "PRESENTED_PRIOR" or
                original.get("metric") != metric or presented.get("metric") != metric or
                original.get("currency") != "CNY" or presented.get("currency") != "CNY" or
                original.get("rawUnit") != "yuan" or presented.get("rawUnit") != "yuan" or
                original.get("valueYuan") != str(old_value) or presented.get("valueYuan") != str(new_value) or
                original.get("contentHash") != REPORT_BINDINGS["ORIGINAL_CURRENT"]["contentHash"] or
                presented.get("contentHash") != REPORT_BINDINGS["PRESENTED_PRIOR"]["contentHash"] or
                original.get("receiptId") != REPORT_BINDINGS["ORIGINAL_CURRENT"]["receiptId"] or
                presented.get("receiptId") != REPORT_BINDINGS["PRESENTED_PRIOR"]["receiptId"] or
                original.get("columnRole") != "CURRENT" or presented.get("columnRole") != "PRIOR" or
                original.get("sourceReportPeriodEnd") != FACT_PERIOD.isoformat() or
                presented.get("sourceReportPeriodEnd") != PRESENTING_PERIOD.isoformat() or
                original.get("factPeriodEnd") != FACT_PERIOD.isoformat() or
                presented.get("factPeriodEnd") != FACT_PERIOD.isoformat() or
                original.get("columnHeader") != "2025年半年度" or presented.get("columnHeader") != "2025年半年度" or
                original.get("unit") != "元" or presented.get("unit") != "元" or
                original.get("page") != native.pdf_page or
                original.get("displayRowLabel") != native.display_row_label or
                original.get("sourceRow") != native.source_row or
                original.get("sourceCells") != list(native.source_cells) or
                presented.get("page") != h126_row.page or
                presented.get("displayRowLabel") != h126_row.label or
                presented.get("sourceRow") != h126_row.raw_line or
                presented.get("sourceCells") != [h126_row.label, h126_row.current_cell, h126_row.prior_cell] or
                original.get("statement") != STATEMENT_FOR[metric] or presented.get("statement") != STATEMENT_FOR[metric] or
                original.get("statementScope") != "CONSOLIDATED" or presented.get("statementScope") != "CONSOLIDATED" or
                Decimal(str(original.get("valueYuan"))) == Decimal(str(presented.get("valueYuan")))):
            raise H125FourFieldChainError("source event identity/value contract failed: " + metric)
        old_id = _version_id(metric, "ORIGINAL_CURRENT")
        new_id = _version_id(metric, "PRESENTED_PRIOR", predecessor_id=old_id)
        if (original.get("versionId") != old_id or original.get("supersedesId") is not None or
                presented.get("versionId") != new_id or presented.get("supersedesId") != old_id):
            raise H125FourFieldChainError("source event version/supersedes link differs: " + metric)
        if metric in summaries:
            summary = summaries[metric]
            if (Decimal(summary["currentYuan"]) != Decimal(str(h126_row.amount * scale)) or
                    summary["adjustedPriorYuan"] != str(new_value) or
                    summary["unadjustedPriorYuan"] != str(old_value)):
                raise H125FourFieldChainError("H126 three-way adjusted/before comparison differs: " + metric)
        values[metric] = {"original": old_value, "presented": new_value,
                          "originalId": old_id, "presentedId": new_id,
                          "currentCell": original.get("sourceCells"),
                          "priorCell": [grid_row["rowLabel"], grid_row["currentCell"], grid_row["priorCell"]]}

    h125_profit_identity = _profit_identity(old_profit, False)
    h126_profit_prior_identity = _profit_identity(new_profit, True)
    h126_profit_current_identity = _profit_identity(new_profit, False)
    if (h125_profit_identity[0] != values["net_profit_consolidated"]["original"] or
            h125_profit_identity[1] != values["net_profit_attributable"]["original"] or
            h126_profit_prior_identity[0] != values["net_profit_consolidated"]["presented"] or
            h126_profit_prior_identity[1] != values["net_profit_attributable"]["presented"]):
        raise H125FourFieldChainError("H125/H126 consolidated attributable/minority identity differs")
    return {"values": values, "candidateRows": candidates, "sections": sections_by_role,
            "grid": grid, "summaries": summaries,
            "profitIdentities": {"H125_CURRENT": h125_profit_identity,
                "H126_PRIOR": h126_profit_prior_identity, "H126_CURRENT": h126_profit_current_identity},
            "sourceRows": source_rows}


def _verify_q3(archive, audit: Mapping, snapshot: Mapping) -> dict:
    q3 = audit.get("postH126OfficialQ3Report", {})
    receipt_data = q3.get("receipt", {})
    document = q3.get("document", {})
    if (q3.get("indexThrough") != "2026-11-02" or q3.get("reportPeriodEnd") != "2026-09-30" or
            receipt_data.get("receipt_id") != "rcpt_83fc90e8313de86be8ed9ab7" or
            receipt_data.get("content_hash") != "sha256:0640deb41193e771e972506da5f063e9554ed3ea59b6438bd43389e438c9dcc9"):
        raise H125FourFieldChainError("post-H126 Q3 report identity is not the fixed official report")
    row, body = _receipt(archive, receipt_data["receipt_id"], receipt_data["content_hash"], receipt_data["url"])
    if document.get("contentHash") != receipt_data["content_hash"] or document.get("byteSize") != len(body):
        raise H125FourFieldChainError("Q3 original PDF hash/size differs")
    from pypdf import PdfReader
    pages = [p.extract_text() or "" for p in PdfReader(io.BytesIO(body), strict=False).pages]
    text = "\n\f\n".join(pages)
    if (len(pages) != document.get("pageCount") or len(pages) != len(document.get("pages", ())) or
            hashlib.sha256(text.encode("utf-8")).hexdigest() != document.get("extractedTextSha256") or
            any(pages[i] != document["pages"][i].get("text") for i in range(len(pages)))):
        raise H125FourFieldChainError("Q3 original native text does not replay")
    flat = re.sub(r"\s+", "", text)
    required = ("北京京能电力股份有限公司", "2026年第三季度报告", "年初至报告期末",
                "2026年前三季度", "2025年前三季度", "2026年1—9月", "2025年前三季度（1-9月）")
    if any(token not in flat for token in required):
        raise H125FourFieldChainError("Q3 does not show its distinct 2026-09-30 / 1-9 month periods")
    if "2025年半年度" in flat or "2025年1—6月" in flat or "2025年1-6月" in flat:
        raise H125FourFieldChainError("Q3 source contains an H125 half-year column requiring separate review")
    q3_index = snapshot["regular_q3_2026"]
    q3_matches = [x["announcement"] for x in q3_index.get("matches", ())
                  if str(x["announcement"]["announcement_id"]) == "1225642733"]
    current_cross = {str(x["announcement_id"]): x for x in snapshot["cross_category"]["announcements"]}
    q3_cross = current_cross.get("1225642733")
    if (q3_index.get("report_period") != "2026-09-30" or len(q3_matches) != 1 or q3_cross is None or
            q3_matches[0].get("title") != "京能电力：2026年第三季度报告" or
            q3_matches[0].get("report_period") != "2026-09-30" or
            q3_matches[0].get("announcement_date_cn") != "2026-10-30" or
            q3_matches[0].get("url") != receipt_data["url"] or
            q3_cross.get("document_url") != receipt_data["url"]):
        raise H125FourFieldChainError("Q3 report is absent from current regular index")
    return {"announcementId": "1225642733", "reportPeriodEnd": "2026-09-30",
            "sourcePdfHash": receipt_data["content_hash"], "pageCount": len(pages),
            "noH125HalfYearColumn": True, "sourceTextHash": document["extractedTextSha256"]}


def _replay_all(archive, candidate: Mapping, impact_input: Mapping,
                impact_proof: Mapping, index_snapshot: Mapping,
                revision_audit: Mapping, calendar_packet: Mapping,
                completed: datetime | None = None, *,
                original_impact_input: Mapping,
                impact_input_provenance: Mapping) -> dict:
    if (candidate.get("schema") != "aquant.cninfo.600578-h125-presented-prior-candidate.v1" or
            candidate.get("verifierId") != "machine:cninfo-600578-h125-presented-prior-candidate-v1" or
            candidate.get("instrumentId") != ISSUER or candidate.get("issuerCode") != STOCK_CODE or
            candidate.get("issuerName") != COMPANY or candidate.get("factPeriodEnd") != FACT_PERIOD.isoformat() or
            candidate.get("metricSet") != list(METRICS) or candidate.get("causeAttribution") is not None or
            candidate.get("formalAdmission") != "NOT_RUN" or candidate.get("writeFinancialWarehouse") is not False):
        raise H125FourFieldChainError("source-only candidate identity or no-causal-claim boundary mismatch")
    original_candidate_hash = candidate.get("evidenceHash")
    without_hash = {k: v for k, v in candidate.items() if k != "evidenceHash"}
    if original_candidate_hash != _digest(_canonical(without_hash).encode("utf-8")):
        raise H125FourFieldChainError("source-only candidate canonical evidence hash mismatch")
    impact_result = _replay_revision_impact(
        archive, impact_input, impact_proof,
        original_impact_input, impact_input_provenance,
    )
    current_company, current_index = _verify_current_company(archive, impact_input, index_snapshot)
    regular_h125 = _verify_regular_index(archive, index_snapshot["regular_h125"], ORIGINAL_ID)
    regular_q3 = _verify_regular_index(archive, index_snapshot["regular_q3_2026"], "1225642733")
    revision_results = _verify_revision_source_audit(archive, revision_audit, index_snapshot, impact_proof)
    values = _replay_pdf_values(archive, candidate)
    cross_rows = {str(x["announcement_id"]): x for x in index_snapshot["cross_category"]["announcements"]}
    for role in ("ORIGINAL_CURRENT", "PRESENTED_PRIOR"):
        report = _report(candidate, role)
        indexed = cross_rows.get(report["announcementId"])
        if (indexed is None or indexed.get("document_url") != report["documentUrl"] or
                impact.normalize_title(indexed.get("title", "")) != impact.normalize_title(report["title"]) or
                datetime.fromtimestamp(int(indexed["announcement_time_ms"]) / 1000, impact.CST).date().isoformat()
                != report["sourcePublishedDate"]):
            raise H125FourFieldChainError("H125/H126 report receipt does not match current index: " + role)
    q3 = _verify_q3(archive, revision_audit, index_snapshot)
    now = completed or _utc_now()
    if now.tzinfo is None or now.utcoffset() is None:
        raise H125FourFieldChainError("coordinator time must be timezone-aware")
    trading_days, replayed_calendar = s2_calendar_evidence.verify_calendar_evidence(
        calendar_packet, archive, now=now,
    )
    if replayed_calendar != calendar_packet:
        raise H125FourFieldChainError("calendar evidence does not replay from archived receipts")
    report_observations = tuple(datetime.fromisoformat(_report(candidate, role)["firstSeenAt"])
                                for role in ("ORIGINAL_CURRENT", "PRESENTED_PRIOR"))
    if any(t.tzinfo is None or t.utcoffset() is None or t > now for t in report_observations):
        raise H125FourFieldChainError("H125/H126 archived report firstSeen time is invalid/future")
    observation_times = (*_collect_source_times(
        (impact_proof, index_snapshot, revision_audit, calendar_packet), completed=now,
    ), *report_observations)
    return {"impact": impact_result, "currentIndex": current_index,
            "regularH125": regular_h125, "regularQ3": regular_q3,
            "revisionDispositions": revision_results, "sourceValues": values,
            "laterQ3": q3, "tradingDays": trading_days,
            "calendarEvidence": replayed_calendar, "completedAt": now,
            "observationTimes": observation_times}


def _collect_source_times(values: Sequence[object], *, completed: datetime) -> tuple[datetime, ...]:
    fields = {"requested_at", "responded_at", "first_seen_at", "firstSeenAt",
              "completedAt", "completed_at", "captured_at_utc", "capturedAt",
              "generatedAtUtc", "reviewed_at", "reviewedAt"}
    found = []
    def visit(value):
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key in fields and isinstance(child, str):
                    parsed = datetime.fromisoformat(child)
                    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed > completed:
                        raise H125FourFieldChainError("source observation/review time is invalid or after writer time")
                    found.append(parsed)
                else:
                    visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)
    for value in values:
        visit(value)
    if not found:
        raise H125FourFieldChainError("source replay contains no archived observation clocks")
    return tuple(found)


def _event_for(candidate: Mapping, metric: str, role: str) -> Mapping:
    entries = candidate["versionsByMetric"][metric]
    matches = [x for x in entries if x.get("role") == role]
    if len(matches) != 1:
        raise H125FourFieldChainError("candidate version role is absent/ambiguous")
    return matches[0]


def _matches_replayed_source_clock(fact: FinancialFact, event: Mapping,
                                   source_report: Mapping) -> bool:
    """Bind persisted source clocks to the report revalidated from the archive."""
    try:
        source_date_text = source_report["sourcePublishedDate"]
        source_date = date.fromisoformat(source_date_text)
        source_first_seen_text = source_report["firstSeenAt"]
        source_first_seen = datetime.fromisoformat(source_first_seen_text)
        event_first_seen_text = event["firstSeenAt"]
        event_first_seen = datetime.fromisoformat(event_first_seen_text)
    except (KeyError, TypeError, ValueError):
        return False
    return (
        source_date.isoformat() == source_date_text and
        source_first_seen.tzinfo is not None and source_first_seen.utcoffset() is not None and
        event_first_seen.tzinfo is not None and event_first_seen.utcoffset() is not None and
        fact.source_published_date == source_date and
        fact.source_published_at is None and
        fact.timestamp_precision is TimestampPrecision.DATE and
        event.get("sourcePublishedDate") == source_date_text and
        fact.first_seen_at == source_first_seen and
        event_first_seen == source_first_seen and
        event_first_seen_text == source_first_seen_text
    )


def _target_facts_exist(repository: FinancialFactRepository) -> bool:
    """Check only the fixed target keys through a read-only rawfacts query.

    A whole-warehouse accepted-review load would replay every prior machine
    bundle just to establish whether these eight keys already have roots.
    """
    with closing(connect(repository.db_path, read_only=True)) as con:
        con.execute("PRAGMA query_only=ON")
        if con.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise H125FourFieldChainError("target-key connection is not query-only")
        con.execute("BEGIN")
        before = con.total_changes
        found = con.execute(
            "SELECT 1 FROM financial_fact WHERE instrument_id IN (?,?) "
            "AND period_end=? AND metric IN (?,?,?,?) LIMIT 1",
            (STOCK_CODE, ISSUER, FACT_PERIOD.isoformat(), *METRICS),
        ).fetchone() is not None
        if con.total_changes != before:
            raise H125FourFieldChainError("target-key existence query changed rawfacts")
        con.execute("ROLLBACK")
    return found


def promote_600578_h125_four_field_version_chain(
    *, candidate: Mapping, impact_input: Mapping, impact_proof: Mapping,
    original_impact_input: Mapping, impact_input_provenance: Mapping,
    index_snapshot: Mapping, revision_audit: Mapping, calendar_packet: Mapping,
    archive, repository: FinancialFactRepository,
) -> tuple[FinancialFact, ...]:
    """Re-open all finite sources and atomically append the eight source versions.

    The function is only for a private clone until root integrates and authorizes
    the separate live writer. All new ingestion/availability times are computed
    at the invocation's actual clock; archive observation clocks are preserved.
    """
    rights = default_rights().get("cninfo")
    if not (rights.open_for("research_use") and rights.open_for("local_storage")):
        raise H125FourFieldChainError("CNINFO research/storage rights are closed")
    source_checked = _utc_now()
    replay = _replay_all(archive, candidate, impact_input, impact_proof,
                         index_snapshot, revision_audit, calendar_packet, source_checked,
                         original_impact_input=original_impact_input,
                         impact_input_provenance=impact_input_provenance)
    completed = _utc_now()
    if any(value > completed for value in replay["observationTimes"]):
        raise H125FourFieldChainError("an archived source observation follows the actual append clock")
    trading_days, replayed_calendar = s2_calendar_evidence.verify_calendar_evidence(
        calendar_packet, archive, now=completed,
    )
    if replayed_calendar != calendar_packet:
        raise H125FourFieldChainError("calendar recheck at actual append clock differs")
    replay["tradingDays"] = trading_days
    replay["calendarEvidence"] = replayed_calendar
    if _target_facts_exist(repository):
        raise H125FourFieldChainError("H125 target keys already exist; forward-root writer refuses a second root")
    source_times = []
    date_floors = []
    for role in ("ORIGINAL_CURRENT", "PRESENTED_PRIOR"):
        report = _report(candidate, role)
        source_date = date.fromisoformat(report["sourcePublishedDate"])
        receipt_first_seen = datetime.fromisoformat(report["firstSeenAt"])
        source_times.append(receipt_first_seen)
        date_floors.append(date_only_available_at(source_date, replay["tradingDays"])[0])
    if not source_times or any(t.tzinfo is None or t > completed for t in source_times):
        raise H125FourFieldChainError("archived report source clock is invalid/future")
    calendar_times = []
    def visit(value):
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key == "first_seen_at" and isinstance(child, str):
                    stamp = datetime.fromisoformat(child)
                    if stamp.tzinfo is None:
                        raise H125FourFieldChainError("calendar receipt time lacks timezone")
                    calendar_times.append(stamp)
                else:
                    visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)
    visit(replay["calendarEvidence"])
    floors = [completed, *date_floors, *source_times, *calendar_times,
              *replay["observationTimes"]]
    availability = _next_available_snapshot(max(floors), replay["tradingDays"])
    facts: list[FinancialFact] = []
    version_sources = {}
    for metric in METRICS:
        pair = []
        predecessor = None
        for role in ("ORIGINAL_CURRENT", "PRESENTED_PRIOR"):
            event = _event_for(candidate, metric, role)
            report = _report(candidate, role)
            value = replay["sourceValues"]["values"][metric]["original" if role == "ORIGINAL_CURRENT" else "presented"]
            version_id = event["versionId"]
            expected_id = _version_id(metric, role, predecessor_id=predecessor)
            if (version_id != expected_id or event.get("supersedesId") != predecessor or
                    Decimal(str(event.get("valueYuan"))) != value):
                raise H125FourFieldChainError("event does not match the exact native source value")
            fact = FinancialFact(
                version_id=version_id, instrument_id=INSTRUMENT_ID, metric=metric,
                period_end=FACT_PERIOD, statement_scope=StatementScope.CONSOLIDATED,
                profit_scope=PROFIT_SCOPE_FOR[metric], value=value, currency="CNY", raw_unit="yuan",
                source_id="cninfo", source_document_id=report["announcementId"],
                source_published_date=date.fromisoformat(report["sourcePublishedDate"]),
                source_published_at=None, timestamp_precision=TimestampPrecision.DATE,
                first_seen_at=datetime.fromisoformat(report["firstSeenAt"]),
                ingested_at=completed, available_at=availability,
                availability_basis=AvailabilityBasis.OBSERVED, pit_mode=PitMode.LIVE_OBSERVED,
                content_hash=report["contentHash"], supersedes_id=predecessor,
            )
            facts.append(fact)
            pair.append({
                "role": role, "versionId": version_id, "supersedesId": predecessor,
                "announcementId": report["announcementId"], "receiptId": report["receiptId"],
                "contentHash": report["contentHash"], "valueYuan": str(value),
                "sourceReportPeriodEnd": report["sourceReportPeriodEnd"],
                "factPeriodEnd": FACT_PERIOD.isoformat(), "columnRole": event["columnRole"],
                "sourcePublishedDate": report["sourcePublishedDate"],
                "firstSeenAt": report["firstSeenAt"],
                "ingestedAt": completed.isoformat(), "availableAt": availability.isoformat(),
            })
            predecessor = version_id
        version_sources[metric] = pair
    facts_by_metric = {metric: [x["versionId"] for x in version_sources[metric]] for metric in METRICS}
    member_ids = [version for metric in METRICS for version in facts_by_metric[metric]]
    # Persist the raw source inputs needed by the independent reader. This is a
    # normal append_many review bundle; no database rows are handcrafted.
    evidence = {
        "schema": SCHEMA, "source_id": "cninfo", "instrument_id": INSTRUMENT_ID,
        "issuer_id": ISSUER, "issuer_name": COMPANY, "period_end": FACT_PERIOD.isoformat(),
        "announcement_id": PRESENTING_ID, "rights_register_version": REGISTER_VERSION,
        "verifier_id": VERIFIER, "verifier_version": "1", "human_review_claimed": None,
        "formal_admission": "NORMAL_READER_REPLAY_REQUIRED",
        "candidate_disposition": "PRESENTED_PRIOR_CHANGED_VALUE_AFFECTS_VERSION_CHAIN",
        "causal_attribution": None, "fact_version_ids": member_ids,
        "version_ids_by_metric": facts_by_metric, "version_sources_by_metric": version_sources,
        "source_candidate": candidate, "impact_input": impact_input,
        "impact_input_original": original_impact_input,
        "impact_input_provenance": impact_input_provenance,
        "impact_proof": impact_proof, "current_index_snapshot": index_snapshot,
        "revision_source_audit": revision_audit, "calendar_evidence": calendar_packet,
        "archive": {"root": str(Path(archive.root).resolve()),
                    "database_path": str(Path(archive.root).resolve() / "meta.sqlite")},
        "replay_receipt": {
            "completed_at": completed.isoformat(), "available_at": availability.isoformat(),
            "source_replay": {
                "impactOutcomes": {m: {"status": replay["impact"]["outcomes"][m]["status"],
                    "admissionStatus": replay["impact"]["outcomes"][m]["admissionStatus"],
                    "impactProof": replay["impact"]["outcomes"][m]["impactProof"]} for m in METRICS},
                "impactInputBinding": replay["impact"]["impactInputBinding"],
                "currentIndex": replay["currentIndex"], "regularH125": replay["regularH125"],
                "regularQ3": replay["regularQ3"], "revisionDispositions": replay["revisionDispositions"],
                "laterQ3": replay["laterQ3"], "calendarCheckedAt": completed.isoformat(),
            },
        },
    }
    bundle_id = REVIEW_PREFIX + hashlib.sha256(
        f"{SCHEMA}|{INSTRUMENT_ID}|{FACT_PERIOD.isoformat()}|{'|'.join(sorted(member_ids))}".encode()
    ).hexdigest()[:32]
    repository.append_many(facts, review_bundle=FactReviewBundle(bundle_id, _canonical(evidence)))
    return tuple(facts)


def accepts_600578_h125_four_field_version_chain(
    row: Mapping, member_ids: set[str], facts: Mapping[str, FinancialFact], *,
    accepted_links: Mapping[str, tuple[str, str]],
) -> bool:
    """Independent, source-bound reader replay for only this eight-version chain."""
    try:
        raw = row["evidence_json"]
        evidence = json.loads(raw)
        if not isinstance(evidence, dict):
            return False
        if (_canonical(evidence) != raw or row["evidence_hash"] != _digest(raw.encode("utf-8")) or
                evidence.get("schema") != SCHEMA or evidence.get("source_id") != "cninfo" or
                evidence.get("instrument_id") != INSTRUMENT_ID or evidence.get("issuer_id") != ISSUER or
                evidence.get("issuer_name") != COMPANY or evidence.get("period_end") != FACT_PERIOD.isoformat() or
                evidence.get("announcement_id") != PRESENTING_ID or
                evidence.get("rights_register_version") != REGISTER_VERSION or
                evidence.get("verifier_id") != VERIFIER or evidence.get("human_review_claimed") is not None or
                evidence.get("candidate_disposition") != "PRESENTED_PRIOR_CHANGED_VALUE_AFFECTS_VERSION_CHAIN" or
                evidence.get("causal_attribution") is not None or
                evidence.get("formal_admission") != "NORMAL_READER_REPLAY_REQUIRED" or
                not row["bundle_id"].startswith(REVIEW_PREFIX)):
            return False
        source_sources = evidence["version_sources_by_metric"]
        ids_by_metric = evidence["version_ids_by_metric"]
        all_ids = {v for group in ids_by_metric.values() for v in group}
        if (set(ids_by_metric) != set(METRICS) or set(source_sources) != set(METRICS) or
                all_ids != member_ids or member_ids != set(evidence["fact_version_ids"]) or
                not member_ids <= facts.keys() or member_ids & accepted_links.keys()):
            return False
        expected_bundle = REVIEW_PREFIX + hashlib.sha256(
            f"{SCHEMA}|{INSTRUMENT_ID}|{FACT_PERIOD.isoformat()}|{'|'.join(sorted(member_ids))}".encode()
        ).hexdigest()[:32]
        if row["bundle_id"] != expected_bundle:
            return False
        archive_data = evidence["archive"]
        with closing(_read_only_archive(archive_data["database_path"], archive_data["root"])) as archive:
            before = archive.con.total_changes
            replay = _replay_all(
                archive, evidence["source_candidate"], evidence["impact_input"],
                evidence["impact_proof"], evidence["current_index_snapshot"],
                evidence["revision_source_audit"], evidence["calendar_evidence"],
                datetime.fromisoformat(evidence["replay_receipt"]["completed_at"]),
                original_impact_input=evidence["impact_input_original"],
                impact_input_provenance=evidence["impact_input_provenance"],
            )
            if archive.con.total_changes != before:
                return False
        replay_record = evidence["replay_receipt"]["source_replay"]
        if (replay_record.get("currentIndex") != replay["currentIndex"] or
                replay_record.get("impactInputBinding") != replay["impact"]["impactInputBinding"] or
                replay_record.get("regularH125") != replay["regularH125"] or
                replay_record.get("regularQ3") != replay["regularQ3"] or
                replay_record.get("revisionDispositions") != replay["revisionDispositions"] or
                replay_record.get("laterQ3") != replay["laterQ3"] or
                {m: replay_record["impactOutcomes"][m] for m in METRICS} != {
                    m: {"status": replay["impact"]["outcomes"][m]["status"],
                        "admissionStatus": replay["impact"]["outcomes"][m]["admissionStatus"],
                        "impactProof": replay["impact"]["outcomes"][m]["impactProof"]} for m in METRICS}):
            return False
        completed = datetime.fromisoformat(evidence["replay_receipt"]["completed_at"])
        available = datetime.fromisoformat(evidence["replay_receipt"]["available_at"])
        if (completed.tzinfo is None or available.tzinfo is None or completed > datetime.now(timezone.utc) or
                available <= completed):
            return False
        date_floors = [date_only_available_at(
            date.fromisoformat(_report(evidence["source_candidate"], role)["sourcePublishedDate"]),
            replay["tradingDays"],
        )[0] for role in ("ORIGINAL_CURRENT", "PRESENTED_PRIOR")]
        if _next_available_snapshot(
                max([completed, *date_floors, *replay["observationTimes"]]),
                replay["tradingDays"],
        ) != available:
            return False
        for metric in METRICS:
            group = source_sources[metric]
            if len(group) != 2 or ids_by_metric[metric] != [x["versionId"] for x in group]:
                return False
            previous = None
            for event in group:
                role = event.get("role")
                source_role = "ORIGINAL_CURRENT" if role == "ORIGINAL_CURRENT" else "PRESENTED_PRIOR"
                report_binding = REPORT_BINDINGS[source_role]
                source_report = _report(evidence["source_candidate"], source_role)
                fact = facts.get(event.get("versionId"))
                amount = replay["sourceValues"]["values"][metric][
                    "original" if role == "ORIGINAL_CURRENT" else "presented"]
                expected_id = _version_id(metric, role, predecessor_id=previous)
                if (fact is None or event.get("versionId") != expected_id or
                        event.get("supersedesId") != previous or fact.supersedes_id != previous or
                        fact.version_id != expected_id or fact.instrument_id != INSTRUMENT_ID or
                        fact.metric != metric or fact.period_end != FACT_PERIOD or
                        fact.statement_scope is not StatementScope.CONSOLIDATED or
                        fact.profit_scope is not PROFIT_SCOPE_FOR[metric] or
                        fact.currency != "CNY" or fact.raw_unit != "yuan" or
                        fact.source_id != "cninfo" or fact.source_document_id != report_binding["announcementId"] or
                        fact.content_hash != report_binding["contentHash"] or fact.value != amount or
                        event.get("receiptId") != report_binding["receiptId"] or
                        event.get("contentHash") != report_binding["contentHash"] or
                        event.get("valueYuan") != str(amount) or
                        event.get("announcementId") != report_binding["announcementId"] or
                        event.get("sourceReportPeriodEnd") != report_binding["sourceReportPeriodEnd"] or
                        event.get("columnRole") != ("CURRENT" if role == "ORIGINAL_CURRENT" else "PRIOR") or
                        event.get("factPeriodEnd") != FACT_PERIOD.isoformat() or
                        not _matches_replayed_source_clock(fact, event, source_report) or
                        fact.ingested_at != completed or fact.available_at != available or
                        event.get("ingestedAt") != completed.isoformat() or
                        event.get("availableAt") != available.isoformat() or
                        fact.timestamp_precision is not TimestampPrecision.DATE or
                        fact.availability_basis is not AvailabilityBasis.OBSERVED or
                        fact.pit_mode is not PitMode.LIVE_OBSERVED):
                    return False
                previous = expected_id
        return True
    except (OSError, sqlite3.Error, KeyError, AttributeError, TypeError, ValueError,
            ArithmeticError):
        return False


__all__ = [
    "SCHEMA", "VERIFIER", "REVIEW_PREFIX", "H125FourFieldChainError",
    "promote_600578_h125_four_field_version_chain",
    "accepts_600578_h125_four_field_version_chain",
]


