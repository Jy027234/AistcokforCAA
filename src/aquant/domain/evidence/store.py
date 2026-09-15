"""事件与引用的落库（主文档 §9、§15.3）。

§15.3 的硬要求：**引用必须能定位；不可定位则不得发布**。

因此这里的写入不是"存一段文字"，而是：
  1. 拿到引用文本后，先在来源正文里**做一次字符级定位**；
  2. 定位成功才写 locator_start/end 并置 located=1；
  3. 定位失败也照写，但 located=0——**留着**比丢掉有用，
     因为"模型引用了原文里没有的句子"本身就是一条重要事实，
     丢掉它会让这条证据看起来不存在。

把不可定位的引用静默丢弃，是这个项目里最危险的一类"清理"。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from aquant.domain.data.db import write_tx


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class Citation:
    quote: str
    #: 定位结果。由 locate() 算出，调用方不得自己填。
    located: bool = False
    start: int | None = None
    end: int | None = None
    #: 定位方式。区分"逐字命中"与"只差空白"是有意义的：
    #: 前者是强证据，后者只说明这段文字在原文里出现过。
    locator_kind: str = "EXACT_MATCH"


def locate(source_text: str, quote: str) -> Citation:
    """在来源正文里定位一段引用。**唯一**的定位实现。

    两级，且**只**有这两级：

      * EXACT_MATCH —— 逐字命中。最强的证据。
      * WHITESPACE_INSENSITIVE —— 逐字命中失败，但**只差空白**。

    第二级为什么存在：模型在中文与拉丁字符之间加一个空格是很自然的
    tokenization 行为（"A 股" vs "A股"）。把这种情况一律判为
    "引用了原文里没有的话"会产生大量假警报，而假警报多了之后
    真警报就没人看了。

    但第二级**不**放宽到"近似匹配"：一旦允许改写，§15.3 的
    "可定位"就没有意义了——引用必须真的在原文里。
    """

    needle = (quote or "").strip()
    if not needle:
        return Citation(quote=quote or "", located=False)
    index = source_text.find(needle)
    if index >= 0:
        return Citation(quote=needle, located=True, start=index,
                        end=index + len(needle), locator_kind="EXACT_MATCH")
    span = _find_ignoring_whitespace(source_text, needle)
    if span is not None:
        start, end = span
        return Citation(quote=needle, located=True, start=start, end=end,
                        locator_kind="WHITESPACE_INSENSITIVE")
    return Citation(quote=needle, located=False)


def _find_ignoring_whitespace(source_text: str, needle: str) -> tuple[int, int] | None:
    """忽略空白后的定位。返回 (start, end) 在**原文**中的位置。

    做法：把 needle 按空白切成若干非空片段，在原文里按顺序找最长的那个
    片段，再用"去空白后的前后缀"夹出区间。全空白的 needle 直接不定位。
    """

    parts = [p for p in re.split(r"\s+", needle) if p]
    if not parts:
        return None
    anchor = max(parts, key=len)
    at = source_text.find(anchor)
    if at < 0:
        return None

    def squeeze(text: str) -> str:
        return re.sub(r"\s+", "", text)

    head, tail = parts[0], parts[-1]
    start = at
    if head != anchor:
        # 往前找第一个片段（去空白比较）：窗口取一段合理的前文
        window_start = max(0, at - 400)
        found = squeeze(source_text[window_start:at]).rfind(squeeze(head))
        if found < 0:
            return None
        start = window_start + found
    end = at + len(anchor)
    if tail != anchor:
        window_end = min(len(source_text), end + 400)
        found = squeeze(source_text[end:window_end]).find(squeeze(tail))
        if found < 0:
            return None
        end = end + found + len(squeeze(tail))
    return start, end


@dataclass(slots=True)
class EvidenceBundle:
    event_id: str
    document_id: str
    citations: list[dict] = field(default_factory=list)
    structured: list[dict] = field(default_factory=list)


def record_evidence(con: sqlite3.Connection, *,
                    instrument_id: str, source_id: str, source_url: str | None,
                    source_title: str, source_text: str,
                    available_at: datetime, event_category: str = "CORPORATE_ACTION",
                    fact_summary: str, verification_status: str = "UNVERIFIED",
                    model_version: str | None = None,
                    prompt_version: str | None = None,
                    raw_output_hash: str | None = None,
                    extra: dict | None = None,
                    research_run_id: str | None = None) -> EvidenceBundle:
    """写入一次证据：一份文档 + 一个事件 + 若干可定位引用。

    available_at 是 PIT 门禁的唯一判据（§7.1），必须由调用方
    按**证据自身**的可得时间给出，不能默认"现在"——默认现在
    等于让历史证据在今天才可用，回看时会凭空多出信息。
    """

    digest = "sha256:" + hashlib.sha256(
        (source_id + "|" + source_text).encode("utf-8")).hexdigest()
    document_id = "doc-" + digest.removeprefix("sha256:")[:20]
    event_id = "ev-" + hashlib.sha256(
        (instrument_id + "|" + source_title + "|" + digest).encode("utf-8")
    ).hexdigest()[:20]
    # 事件键里带上 research_run_id：同一份公告在不同研究运行下
    # 可以得出不同的抽取结果，两份都要留，不能互相覆盖
    if research_run_id:
        event_id = "ev-" + hashlib.sha256(
            (event_id + "|" + research_run_id).encode("utf-8")).hexdigest()[:20]

    now = _now()
    extra = extra or {}
    bundle = EvidenceBundle(event_id=event_id, document_id=document_id)
    with write_tx(con):
        con.execute(
            "INSERT OR IGNORE INTO document (document_id,origin,url,is_original,"
            "fetched_at,source_published_date,timestamp_precision,content_hash,"
            "license_status,source_id) VALUES (?,?,?,1,?,?,'DATE',?,'EXCERPT_ONLY',?)",
            (document_id, "EXCHANGE_DISCLOSURE", source_url, now,
             extra.get("announced_on"), digest, source_id))
        con.execute(
            "INSERT OR REPLACE INTO event (event_id,event_category,fact_summary,"
            "source_published_date,first_seen_at,ingested_at,available_at,"
            "available_basis,pit_mode,verification_status,market_direction,"
            "model_version,prompt_version,raw_output_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?,'RECONSTRUCTED','HISTORICAL_RECONSTRUCTED',?,"
            "'UNKNOWN',?,?,?,?)",
            (event_id, event_category, fact_summary,
             extra.get("announced_on"), now, now, available_at.isoformat(),
             verification_status, model_version, prompt_version, raw_output_hash,
             now))
        con.execute(
            "INSERT OR IGNORE INTO event_document (event_id,document_id,relation) "
            "VALUES (?,?,'SUPPORTS')", (event_id, document_id))
        con.execute(
            "INSERT OR REPLACE INTO event_subject (event_id,subject_type,subject_id,"
            "role) VALUES (?,'INSTRUMENT',?,'PRIMARY')", (event_id, instrument_id))

        for seq, item in enumerate(extra.get("structured", [])):
            con.execute(
                "INSERT OR REPLACE INTO event_structured_value (value_id,event_id,name,"
                "value_text,unit,raw_value,raw_unit) VALUES (?,?,?,?,?,?,?)",
                (f"{event_id}-v{seq}", event_id, item["name"], item["value_text"],
                 item.get("unit", "TEXT"), item.get("raw_value"), item.get("raw_unit")),
            )
            bundle.structured.append(dict(item))

        for seq, hold in enumerate(extra.get("citations", [])):
            located = hold if isinstance(hold, Citation) else locate(source_text, hold)
            citation_id = f"cit-{event_id}-{seq:03d}"
            con.execute(
                "INSERT OR REPLACE INTO citation (citation_id,event_id,document_id,"
                "quote,locator_kind,locator_start,locator_end,located) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (citation_id, event_id, document_id, located.quote,
                 located.locator_kind if located.located else "EXACT_MATCH",
                 located.start, located.end, 1 if located.located else 0))
            bundle.citations.append({
                "citation_id": citation_id, "quote": located.quote,
                "located": located.located, "start": located.start,
                "end": located.end, "locatorKind": located.locator_kind,
            })
    return bundle


def evidence_for(con: sqlite3.Connection, *, instrument_id: str,
                 located_only: bool = False) -> list[dict]:
    """某只标的已落库的证据与引用。located_only 用于只取可定位的。"""

    rows = con.execute(
        "SELECT e.event_id,e.event_category,e.fact_summary,e.available_at,"
        "e.verification_status,e.model_version,e.prompt_version,"
        "c.citation_id,c.quote,c.locator_kind,c.locator_start,c.locator_end,c.located,"
        "d.document_id,d.url,d.content_hash "
        "FROM event e "
        "JOIN event_subject s ON s.event_id=e.event_id AND s.subject_id=? "
        "LEFT JOIN citation c ON c.event_id=e.event_id "
        "LEFT JOIN document d ON d.document_id=c.document_id "
        "ORDER BY e.available_at DESC, c.citation_id",
        (instrument_id,)).fetchall()
    out = []
    for r in rows:
        if located_only and not r["located"]:
            continue
        out.append({
            "eventId": r["event_id"], "category": r["event_category"],
            "factSummary": r["fact_summary"], "availableAt": r["available_at"],
            "verificationStatus": r["verification_status"],
            "modelVersion": r["model_version"], "promptVersion": r["prompt_version"],
            "citationId": r["citation_id"], "quote": r["quote"],
            "located": bool(r["located"]) if r["citation_id"] else None,
            "locatorKind": r["locator_kind"] if r["located"] else None,
            "locatorStart": r["locator_start"], "locatorEnd": r["locator_end"],
            "documentId": r["document_id"], "url": r["url"],
            "documentHash": r["content_hash"],
        })
    return out
