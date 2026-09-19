"""财务候选值与法定披露公告之间的显式关联。

免费结构化页面给出“当前展示值”，巨潮公告给出发布日和修订原文。两者来源
不同，不能因为证券代码和报告期相同就伪装成一条同源记录。本模块只建立可审计
的关联与版本顺序；在具体数值未与某份公告原文逐项核对前，``pit_eligible``
始终为 False。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Iterable


class DisclosureRole(str, Enum):
    ORIGINAL_REPORT = "ORIGINAL_REPORT"
    REVISED_REPORT = "REVISED_REPORT"
    CORRECTION_NOTICE = "CORRECTION_NOTICE"
    SUPPLEMENT_NOTICE = "SUPPLEMENT_NOTICE"


class LinkStatus(str, Enum):
    NO_MATCH = "NO_MATCH"
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
    LINKED_VALUES_UNVERIFIED = "LINKED_VALUES_UNVERIFIED"


@dataclass(frozen=True, slots=True)
class DisclosureCandidate:
    announcement_id: str
    sec_code: str
    title: str
    announced_on: date
    document_url: str | None


@dataclass(frozen=True, slots=True)
class FinancialDisclosureVersion:
    announcement_id: str
    role: DisclosureRole
    announced_on: date
    title: str
    document_url: str | None
    candidate_predecessor_announcement_id: str | None
    supersedes_verified: bool = False


@dataclass(frozen=True, slots=True)
class FinancialDisclosureLink:
    stock_code: str
    period_end: date
    value_source_id: str
    value_content_hashes: tuple[str, ...]
    versions: tuple[FinancialDisclosureVersion, ...]
    status: LinkStatus
    active_report_announcement_id: str | None
    pit_eligible: bool = False
    pit_blocker: str = (
        "structured values have not been verified against a specific archived "
        "announcement version"
    )


_YEAR = re.compile(r"(?P<year>20\d{2})\s*年")
_REVISED_REPORT = re.compile(r"修订版|修订稿|更正版|更正后|更新后")
_CORRECTION_NOTICE = re.compile(r"更正公告|修正公告|更正说明|修订说明")
_SUPPLEMENT_NOTICE = re.compile(r"补充公告|补充说明")
_EXCLUDED = re.compile(r"摘要|英文版|取消|提示性公告")


def report_period_from_title(title: str) -> date | None:
    """从完整定期报告或其更正公告标题提取报告期。

    只接受标题中明确出现的年度、半年度、第一/第三季度；不根据发布日期猜。
    摘要和英文版被排除，避免把同一报告重复计作多个可替代版本。
    """

    compact = re.sub(r"\s+", "", title)
    if _EXCLUDED.search(compact):
        return None
    match = _YEAR.search(compact)
    if not match or "报告" not in compact:
        return None
    year = int(match.group("year"))
    if re.search(r"第一季度|一季度", compact):
        return date(year, 3, 31)
    if re.search(r"半年度|中期报告", compact):
        return date(year, 6, 30)
    if re.search(r"第三季度|三季度", compact):
        return date(year, 9, 30)
    if re.search(r"年度报告|年报", compact):
        return date(year, 12, 31)
    return None


def disclosure_role(title: str) -> DisclosureRole:
    if _CORRECTION_NOTICE.search(title):
        return DisclosureRole.CORRECTION_NOTICE
    if _SUPPLEMENT_NOTICE.search(title):
        return DisclosureRole.SUPPLEMENT_NOTICE
    if _REVISED_REPORT.search(title):
        return DisclosureRole.REVISED_REPORT
    return DisclosureRole.ORIGINAL_REPORT


def link_statement_to_disclosures(
    *,
    stock_code: str,
    period_end: date,
    value_source_id: str,
    value_content_hashes: Iterable[str],
    announcements: Iterable[DisclosureCandidate],
) -> FinancialDisclosureLink:
    """建立报告期版本链，但不把当前结构化值伪装成历史公告中的值。"""

    hashes = tuple(sorted(set(value_content_hashes)))
    if not hashes or any(not item.startswith("sha256:") for item in hashes):
        raise ValueError("value_content_hashes must contain sha256-prefixed hashes")

    by_id: dict[str, DisclosureCandidate] = {}
    for item in announcements:
        if item.sec_code != stock_code or report_period_from_title(item.title) != period_end:
            continue
        previous = by_id.get(item.announcement_id)
        if previous is not None and previous != item:
            raise ValueError(
                f"announcement {item.announcement_id!r} has conflicting metadata")
        by_id[item.announcement_id] = item
    role_order = {
        DisclosureRole.ORIGINAL_REPORT: 0,
        DisclosureRole.CORRECTION_NOTICE: 1,
        DisclosureRole.SUPPLEMENT_NOTICE: 2,
        DisclosureRole.REVISED_REPORT: 3,
    }
    matched = sorted(
        by_id.values(),
        key=lambda item: (
            item.announced_on,
            role_order[disclosure_role(item.title)],
            item.announcement_id,
        ),
    )

    versions: list[FinancialDisclosureVersion] = []
    latest_full_report: str | None = None
    for item in matched:
        role = disclosure_role(item.title)
        predecessor = latest_full_report if role is not DisclosureRole.ORIGINAL_REPORT else None
        versions.append(FinancialDisclosureVersion(
            announcement_id=item.announcement_id,
            role=role,
            announced_on=item.announced_on,
            title=item.title,
            document_url=item.document_url,
            candidate_predecessor_announcement_id=predecessor,
        ))
        if role in (DisclosureRole.ORIGINAL_REPORT, DisclosureRole.REVISED_REPORT):
            latest_full_report = item.announcement_id

    if not versions:
        status = LinkStatus.NO_MATCH
    elif latest_full_report is None:
        status = LinkStatus.EVIDENCE_INCOMPLETE
    else:
        status = LinkStatus.LINKED_VALUES_UNVERIFIED

    return FinancialDisclosureLink(
        stock_code=stock_code,
        period_end=period_end,
        value_source_id=value_source_id,
        value_content_hashes=hashes,
        versions=tuple(versions),
        status=status,
        active_report_announcement_id=latest_full_report,
    )
