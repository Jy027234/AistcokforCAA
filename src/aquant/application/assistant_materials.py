"""Resolve the small, card-bound evidence set used by the research assistant."""

from __future__ import annotations

import hashlib
import sqlite3
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from aquant.application.assistant import AssistantError, Material
from aquant.application.research_cards import get_card, get_card_payload
from aquant.domain.ai.egress import EgressItem, evaluate
from aquant.domain.data.rights import Basis, default_rights
from aquant.domain.evidence.store import evidence_for
from aquant.operations.research_scan import (
    ResearchCandidateError, ResearchScanSchemaMismatch,
    load_verified_research_scan_packet,
)

MAX_EXCERPT_CHARS = 1_000
MAX_SELECTED_EXCERPT_CHARS = 3_000
_CNINFO_PDF_PATH = re.compile(
    r"/finalpage/(?P<day>\d{4}-\d{2}-\d{2})/(?P<announcement>[0-9]+)\.PDF\Z")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RECEIPT = re.compile(r"rcpt_[0-9a-f]{24}\Z")
_EVENT = re.compile(r"event-(?P<announcement>[0-9]+)\Z")


def resolve_research_card_materials(
    con: sqlite3.Connection,
    *,
    card_id: str,
    current_snapshot_id: str,
    data_mode: str,
    as_of_time: datetime,
    citation_ids: list[str],
    allow_test_source: bool = False,
) -> tuple[list[Material], list[dict]]:
    """Re-read and verify selected located excerpts from the current card.

    The request contributes only identifiers. Quote text and source identifiers
    are resolved from the archived card and current evidence rows on the server.
    """

    if not citation_ids or len(citation_ids) > 3 or len(set(citation_ids)) != len(citation_ids):
        raise AssistantError(
            "PIT_UNVERIFIED", "请从这张研究卡选择 1 到 3 条不同的依据",
            "选择卡片上可定位的短摘录后重试")

    card = get_card(con, card_id)
    if card is None:
        raise AssistantError(
            "RESEARCH_CARD_NOT_FOUND", "研究卡不存在或尚未留档",
            "重新打开当前研究卡后重试")
    if card["snapshot_id"] != current_snapshot_id:
        raise AssistantError(
            "STALE_RESEARCH_CARD", "研究卡不属于当前已发布快照",
            "重新载入当前研究卡并重新选择依据")
    try:
        payload = get_card_payload(con, card_id)
    except (ValueError, TypeError) as exc:
        raise AssistantError(
            "PIT_UNVERIFIED", "研究卡留档完整性校验失败",
            "重新生成并留档研究卡后重试") from exc
    if (not payload or payload.get("cardId") != card_id
            or payload.get("snapshotId") != current_snapshot_id
            or payload.get("instrumentId") != card["instrument_id"]
            or card.get("data_mode") != data_mode):
        raise AssistantError(
            "PIT_UNVERIFIED", "研究卡留档与当前快照绑定不一致",
            "重新载入当前研究卡后重试")
    try:
        payload_as_of = datetime.fromisoformat(
            str(payload.get("asOfTime", "")).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AssistantError(
            "PIT_UNVERIFIED", "研究卡缺少可验证的快照截止时点",
            "重新生成并留档研究卡后重试") from exc
    if payload_as_of != as_of_time:
        raise AssistantError(
            "STALE_RESEARCH_CARD", "研究卡截止时点与当前快照不一致",
            "重新载入当前研究卡并重新选择依据")

    card_evidence = payload.get("evidence")
    if not isinstance(card_evidence, list):
        raise AssistantError(
            "PIT_UNVERIFIED", "研究卡没有可验证的证据列表",
            "重新生成并留档研究卡后重试")
    by_card_id: dict[str, list[dict]] = {}
    for item in card_evidence:
        if isinstance(item, dict) and isinstance(item.get("citationId"), str):
            by_card_id.setdefault(item["citationId"], []).append(item)

    current_rows = evidence_for(
        con, instrument_id=str(payload["instrumentId"]), located_only=True,
        as_of=as_of_time)
    by_current_id: dict[str, list[dict]] = {}
    for item in current_rows:
        citation_id = item.get("citationId")
        if isinstance(citation_id, str):
            by_current_id.setdefault(citation_id, []).append(item)

    materials: list[Material] = []
    audit_refs: list[dict] = []
    total_chars = 0
    for citation_id in citation_ids:
        card_matches = by_card_id.get(citation_id, [])
        fresh_matches = by_current_id.get(citation_id, [])
        if len(card_matches) != 1 or len(fresh_matches) != 1:
            raise AssistantError(
                "PIT_UNVERIFIED", "所选引用不唯一、已过期或不属于这张研究卡",
                "只选择当前研究卡上仍可定位的引用")
        card_item, fresh = card_matches[0], fresh_matches[0]
        quote = fresh.get("quote")
        document_id = fresh.get("documentId")
        document_hash = fresh.get("documentHash")
        source_id = None
        if document_id and document_hash:
            document = con.execute(
                "SELECT source_id,content_hash FROM document WHERE document_id=?",
                (document_id,),
            ).fetchone()
            if (document is not None and document["content_hash"] == document_hash
                    and document["source_id"]):
                source_id = str(document["source_id"])

        if (card_item.get("located") is not True
                or fresh.get("located") is not True
                or not isinstance(quote, str) or not quote.strip()
                or len(quote) > MAX_EXCERPT_CHARS
                or fresh.get("locatorKind") is None
                or not document_id or not document_hash or not source_id
                or card_item.get("quote") != quote
                or card_item.get("documentId") != document_id
                or card_item.get("availableAt") != fresh.get("availableAt")):
            raise AssistantError(
                "PIT_UNVERIFIED", "所选引用无法证明是当前快照前可定位的公开短摘录",
                "只选择当前卡片中可定位、短于 1,000 字且文档哈希一致的依据")

        try:
            rights = default_rights().get(source_id)
        except KeyError as exc:
            raise AssistantError(
                "SOURCE_PERMISSION_MISSING", "引用来源未登记，不能用于助手外发",
                "先核对并登记该来源的模型处理权利") from exc
        if (not allow_test_source and rights.basis is not Basis.PUBLIC_DISCLOSURE):
            raise AssistantError(
                "SOURCE_PERMISSION_MISSING", "助手只接受已登记的公开披露来源摘录",
                "改选交易所或法定信息披露平台的公开证据")

        egress = evaluate([EgressItem(
            source_id=source_id, text=quote, purpose="解释当前研究卡的已选公开摘录",
            contains_personal_data=False,
        )])
        if not egress.allowed:
            raise AssistantError(
                "SOURCE_PERMISSION_MISSING", "引用来源未通过项目外发权利闸门",
                "检查来源权利登记后再重试", blockers=egress.blockers)

        total_chars += len(quote)
        if total_chars > MAX_SELECTED_EXCERPT_CHARS:
            raise AssistantError(
                "PIT_UNVERIFIED", "所选摘录总长度超过 3,000 字上限",
                "减少所选引用后重试")

        materials.append(Material(
            source_id=source_id,
            text=f"[citation_id: {citation_id}]\n{quote}",
            contains_personal_data=False,
        ))
        audit_refs.append({
            "citationId": citation_id,
            "documentId": str(document_id),
            "documentHash": str(document_hash),
            "quoteHash": "sha256:" + hashlib.sha256(quote.encode("utf-8")).hexdigest(),
            "sourceId": source_id,
            "availableAt": fresh.get("availableAt"),
            "locatorKind": fresh.get("locatorKind"),
            "locatorStart": fresh.get("locatorStart"),
            "locatorEnd": fresh.get("locatorEnd"),
        })

    return materials, audit_refs


def resolve_verified_research_scan_materials(
    *,
    artifact_path: Path,
    snapshot_id: str,
    instrument_id: str,
    receipt_ids: list[str],
    current_as_of_time: datetime,
    data_mode: str,
    allow_test_source: bool = False,
) -> tuple[list[Material], list[dict], dict]:
    """Resolve current scan event receipts from the verified artifact only.

    The request supplies the displayed scan snapshot, instrument and receipt
    identifiers. It never supplies quote text, source identifiers, PDF data or
    a personal-data declaration. The scan loader rechecks the full artifact
    and its source provenance before any selected excerpt is used.
    """

    if (not snapshot_id or not instrument_id or not receipt_ids
            or len(receipt_ids) > 3 or len(set(receipt_ids)) != len(receipt_ids)):
        raise AssistantError(
            "PIT_UNVERIFIED", "请从当前研究卡选择 1 到 3 条不同的公告摘录",
            "重新载入研究卡并选择已定位的公开公告摘录")
    if data_mode != "PRODUCTION" and not allow_test_source:
        raise AssistantError(
            "DATA_NOT_READY", "当前正式快照不是可外发的真实数据快照",
            "切换到已发布的真实数据快照后重试")

    try:
        packet, artifact_hash = load_verified_research_scan_packet(artifact_path)
    except (ResearchCandidateError, ResearchScanSchemaMismatch, OSError,
            ValueError, ArithmeticError, KeyError, TypeError) as exc:
        raise AssistantError(
            "DATA_NOT_READY", "当前研究资料或其公告来源校验失败，未发送材料",
            "检查已配置研究产物及其来源文件，再重新载入研究卡") from exc

    if (packet.get("schema") != "aquant.research-candidates.v2"
            or packet.get("researchOnly") is not True
            or packet.get("simulationAvailable") is not False
            or packet.get("pitMode") != "HISTORICAL_RECONSTRUCTED"):
        raise AssistantError(
            "PIT_UNVERIFIED", "当前研究资料版本不支持已定位公告摘录",
            "重新发布并载入已验证的研究资料后重试")
    if packet.get("snapshotId") != snapshot_id:
        raise AssistantError(
            "STALE_RESEARCH_CARD", "所选研究卡已不是当前已验证的研究版本",
            "重新载入当前研究名单并选择公告摘录")

    try:
        scan_as_of = datetime.fromisoformat(
            str(packet.get("asOfTime", "")).replace("Z", "+00:00"))
        data_as_of = date.fromisoformat(str(packet.get("dataAsOf", "")))
    except ValueError as exc:
        raise AssistantError(
            "PIT_UNVERIFIED", "研究资料缺少可验证的 PIT 截止时点",
            "重新生成并核验当前研究资料") from exc
    if (scan_as_of.tzinfo is None or current_as_of_time.tzinfo is None
            or scan_as_of > current_as_of_time
            or data_as_of > current_as_of_time.date()):
        raise AssistantError(
            "PIT_UNVERIFIED", "研究资料晚于当前正式快照，不能用于本次助手请求",
            "重新发布当前正式快照对应的研究资料后重试")

    candidates = packet.get("candidates")
    if not isinstance(candidates, list):
        raise AssistantError(
            "PIT_UNVERIFIED", "当前研究资料没有可验证的证券列表",
            "重新生成并核验当前研究资料")
    matches = [item for item in candidates
               if isinstance(item, dict) and item.get("instrumentId") == instrument_id]
    if len(matches) != 1:
        raise AssistantError(
            "STALE_RESEARCH_CARD", "所选证券不属于当前研究资料",
            "重新载入当前研究名单并选择其中的证券")

    events = matches[0].get("events")
    if not isinstance(events, list):
        events = []
    by_receipt: dict[str, list[dict]] = {}
    for item in events:
        if isinstance(item, dict) and isinstance(item.get("receiptId"), str):
            by_receipt.setdefault(item["receiptId"], []).append(item)

    materials: list[Material] = []
    audit_refs: list[dict] = []
    total_chars = 0
    for receipt_id in receipt_ids:
        selected = by_receipt.get(receipt_id, [])
        if len(selected) != 1:
            raise AssistantError(
                "PIT_UNVERIFIED", "所选公告回执不存在、重复或不属于这张研究卡",
                "重新载入当前研究卡并选择仍可定位的公告摘录")
        event = selected[0]
        title = event.get("title")
        published = event.get("publishedDate")
        url = event.get("url")
        quote = event.get("quote")
        source_event_id = event.get("sourceId")
        content_hash = event.get("contentHash")
        page, start, end = event.get("page"), event.get("startChar"), event.get("endChar")
        try:
            published_date = date.fromisoformat(str(published))
            parsed_url = urlsplit(str(url))
            url_match = _CNINFO_PDF_PATH.fullmatch(parsed_url.path)
            event_match = _EVENT.fullmatch(str(source_event_id))
            locator_valid = (
                type(page) is int and page > 0
                and type(start) is int and start >= 0
                and type(end) is int and end > start)
            event_valid = (
                isinstance(title, str) and bool(title.strip()) and len(title) <= 300
                and isinstance(quote, str) and bool(quote.strip())
                and len(quote) <= MAX_EXCERPT_CHARS
                and locator_valid and len(quote) == end - start
                and _RECEIPT.fullmatch(receipt_id) is not None
                and _SHA256.fullmatch(str(content_hash)) is not None
                and parsed_url.scheme == "https"
                and parsed_url.hostname == "static.cninfo.com.cn"
                and parsed_url.port in (None, 443)
                and not parsed_url.username and not parsed_url.password
                and not parsed_url.query and not parsed_url.fragment
                and url_match is not None and event_match is not None
                and url_match.group("announcement") == event_match.group("announcement")
                and url_match.group("day") == published_date.isoformat()
                and published_date <= data_as_of
                and published_date <= scan_as_of.date()
                and published_date <= current_as_of_time.date())
        except (ValueError, TypeError, AttributeError):
            event_valid = False
        if not event_valid:
            raise AssistantError(
                "PIT_UNVERIFIED", "所选公告的来源、哈希、PIT 或页内字符定位无法核验",
                "只选择当前研究卡中已验证的短公告摘录")

        try:
            rights = default_rights().get("cninfo")
        except KeyError as exc:
            raise AssistantError(
                "SOURCE_PERMISSION_MISSING", "巨潮资讯来源未登记，不能用于助手外发",
                "先核对公开披露来源的模型处理权利登记") from exc
        if rights.basis is not Basis.PUBLIC_DISCLOSURE:
            raise AssistantError(
                "SOURCE_PERMISSION_MISSING", "巨潮资讯来源未登记为公开披露来源",
                "核对公开披露来源的权利登记后再重试")
        egress = evaluate([EgressItem(
            source_id="cninfo", text=quote,
            purpose="解释当前研究卡的已选公开公告摘录",
            contains_personal_data=False,
        )])
        if not egress.allowed:
            raise AssistantError(
                "SOURCE_PERMISSION_MISSING", "公告来源未通过项目外发权利闸门",
                "检查来源权利登记后再重试", blockers=egress.blockers)

        total_chars += len(quote)
        if total_chars > MAX_SELECTED_EXCERPT_CHARS:
            raise AssistantError(
                "PIT_UNVERIFIED", "所选公告摘录总长度超过 3,000 字上限",
                "减少所选摘录后重试")

        citation_id = "scan-" + hashlib.sha256(
            f"{snapshot_id}|{instrument_id}|{receipt_id}".encode("utf-8")
        ).hexdigest()[:24]
        announcement_id = event_match.group("announcement")
        materials.append(Material(
            source_id="cninfo",
            text=f"[citation_id: {citation_id}]\n{quote}",
            contains_personal_data=False,
        ))
        audit_refs.append({
            "citationId": citation_id,
            "announcementId": announcement_id,
            "eventId": source_event_id,
            "receiptId": receipt_id,
            "documentHash": content_hash,
            "quoteHash": "sha256:" + hashlib.sha256(quote.encode("utf-8")).hexdigest(),
            "sourceId": "cninfo",
            "url": url,
            "title": title,
            "publishedDate": published,
            "availableAt": published,
            "locatorKind": "PDF_PAGE_CHARS",
            "page": page,
            "locatorStart": start,
            "locatorEnd": end,
            "snapshotId": snapshot_id,
            "instrumentId": instrument_id,
        })

    binding = {
        "researchSnapshotId": snapshot_id,
        "researchAsOfTime": scan_as_of.isoformat(),
        "dataAsOf": data_as_of.isoformat(),
        "artifactHash": artifact_hash,
        "instrumentId": instrument_id,
        "receiptIds": receipt_ids,
    }
    return materials, audit_refs, binding
