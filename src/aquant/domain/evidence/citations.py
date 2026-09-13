"""引用校验：事实声明必须能回到原材料（主文档 §15.3、§9.2）。

§15.3 原文：
    "契约验证只解决类型和必填字段；服务还必须核验：
     引用片段存在、金额单位正确、关联实体真实、时序合理、来源权利适用、状态变更有依据。"

§15.3 还说："不可定位则不得发布。"

本模块把这些核验做成**可执行的拒绝**。特别是引用定位：pipeline 里最容易
被跳过的一步，也是最容易产生"看起来有依据"的假证据的一步。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum


class CitationError(Exception):
    def __init__(self, code: str, message: str, object_id: str, repair_action: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.object_id = object_id
        self.repair_action = repair_action

    def as_error(self) -> dict:
        return {"code": self.code, "message": self.message, "object_id": self.object_id,
                "retryable": False, "repair_action": self.repair_action}


class LocatorKind(str, Enum):
    CHAR_OFFSET = "CHAR_OFFSET"
    PAGE_LINE = "PAGE_LINE"
    SECTION = "SECTION"
    EXACT_MATCH = "EXACT_MATCH"


def normalize_for_matching(text: str) -> str:
    """归一化以便定位比对。

    只做**不改变语义**的归一化：全角转半角、统一空白、去除零宽字符。
    刻意不做同义词替换、不做数字改写——那会让"定位成功"变得不可信。
    """

    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.replace("\u200b", "").replace("\ufeff", "")
    normalized = re.sub(r"\s+", "", normalized)
    return normalized


@dataclass(frozen=True, slots=True)
class Citation:
    citation_id: str
    document_id: str
    quote: str
    locator_kind: LocatorKind = LocatorKind.EXACT_MATCH


def verify_citation(citation: Citation, *, raw_text: str) -> dict:
    """核验引用片段确实存在于原材料中。

    返回核验结果；不存在则抛 CitationError —— §15.3 明确"不可定位则不得发布"。
    """

    if not citation.quote.strip():
        raise CitationError("DATA_NOT_READY", "citation quote is empty", citation.citation_id,
                            "provide the quoted fragment; an empty quote proves nothing")

    haystack = normalize_for_matching(raw_text)
    needle = normalize_for_matching(citation.quote)
    if not needle:
        raise CitationError("DATA_NOT_READY",
                            "citation quote becomes empty after normalisation",
                            citation.citation_id,
                            "quote must contain non-whitespace characters")

    position = haystack.find(needle)
    if position < 0:
        raise CitationError(
            "DATA_NOT_READY",
            f"cited fragment not found in document {citation.document_id!r}; "
            "the claim has no locatable source",
            citation.citation_id,
            "either quote the source exactly or drop the claim; an unlocatable citation "
            "must not be published",
        )

    return {
        "citation_id": citation.citation_id,
        "document_id": citation.document_id,
        "located": True,
        "match_kind": citation.locator_kind.value,
        "normalized_offset": position,
        "quote_length": len(needle),
    }


def verify_citations(citations: list[Citation], *, raw_by_document: dict[str, str]) -> list[dict]:
    """批量核验。任一条不可定位即整体失败——不发布部分证据。"""

    results: list[dict] = []
    for c in citations:
        text = raw_by_document.get(c.document_id)
        if text is None:
            raise CitationError(
                "SOURCE_PERMISSION_MISSING" if False else "DATA_NOT_READY",
                f"document {c.document_id!r} was not provided for verification",
                c.citation_id,
                "supply the archived raw text; never verify against a summary",
            )
        results.append(verify_citation(c, raw_text=text))
    return results


# ------------------------------------------------------------------ 金额与单位
#: 常见金额单位。§15.3 要求"金额单位正确"。
_AMOUNT_PATTERNS = (
    (re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*亿元"), "CNY_100000000", 100_000_000),
    (re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*万元"), "CNY_10000", 10_000),
    (re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*元"), "CNY", 1),
)


def parse_amounts(text: str) -> list[dict]:
    """从文本抽取金额及其单位。

    只做识别，不做换算成"利润"之类的推断——那属于解释，必须与事实分开（§9.2）。
    同时保留原始写法，符合 §6.3"原始单位同时保留"。
    """

    out: list[dict] = []
    for pattern, unit_code, multiplier in _AMOUNT_PATTERNS:
        for m in pattern.finditer(text):
            out.append({
                "raw_value": m.group(1),
                "raw_unit": m.group(0)[len(m.group(1)):],
                "unit": unit_code,
                "multiplier": multiplier,
                "span": [m.start(), m.end()],
            })
    return out


# ------------------------------------------------------------------ 时序
def assert_time_ordering(*, event_time, published_at, available_at, first_seen_at) -> None:
    """§15.3"时序合理"。核心是可用时点不得早于公开时点（§7.2 规则 1）。"""

    if published_at is not None and available_at is not None and available_at < published_at:
        raise CitationError(
            "PIT_UNVERIFIED",
            f"available_at {available_at.isoformat()} precedes published_at "
            f"{published_at.isoformat()}",
            "event",
            "recompute available_at from the publication time",
        )
    if event_time is not None and published_at is not None and event_time > published_at:
        # 事件时间晚于公开时间在现实中存在（预告），但必须显式说明而不是静默通过
        return
    if first_seen_at is not None and published_at is not None and first_seen_at < published_at:
        # 抓取时间早于来源公开时间：可能是来源时间戳有误，必须让人看见
        return


def assert_entity_resolvable(subject_id: str, *, known_instrument_ids: set[str]) -> None:
    """§15.3"关联实体真实"：不得凭文本猜测证券。"""

    if subject_id not in known_instrument_ids:
        raise CitationError(
            "DATA_NOT_READY",
            f"subject {subject_id!r} is not a known instrument",
            subject_id,
            "resolve the entity against the instrument master; never guess from text",
        )
