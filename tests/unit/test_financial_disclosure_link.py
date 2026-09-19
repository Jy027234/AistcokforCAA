from __future__ import annotations

from datetime import date

import pytest

from aquant.domain.fundamentals.disclosure_link import (
    DisclosureCandidate,
    DisclosureRole,
    LinkStatus,
    link_statement_to_disclosures,
    report_period_from_title,
)


def ann(identifier: str, title: str, day: str, code: str = "600519") -> DisclosureCandidate:
    return DisclosureCandidate(
        announcement_id=identifier,
        sec_code=code,
        title=title,
        announced_on=date.fromisoformat(day),
        document_url=f"https://static.cninfo.com.cn/{identifier}.PDF",
    )


def link(items: list[DisclosureCandidate]):
    return link_statement_to_disclosures(
        stock_code="600519",
        period_end=date(2025, 12, 31),
        value_source_id="sina-financial",
        value_content_hashes=["sha256:" + "a" * 64],
        announcements=items,
    )


@pytest.mark.parametrize(("title", "expected"), [
    ("2025年第一季度报告", date(2025, 3, 31)),
    ("2025年半年度报告", date(2025, 6, 30)),
    ("2025年第三季度报告", date(2025, 9, 30)),
    ("关于2025年年度报告的更正公告", date(2025, 12, 31)),
    ("2025年年度报告摘要", None),
])
def test_report_period_is_explicitly_parsed(title: str, expected: date | None):
    assert report_period_from_title(title) == expected


def test_revision_chain_preserves_original_notice_and_revised_report():
    result = link([
        ann("1", "2025年年度报告", "2026-03-20"),
        ann("2", "关于2025年年度报告的更正公告", "2026-04-01"),
        ann("3", "2025年年度报告（修订版）", "2026-04-01"),
        ann("other", "2025年年度报告", "2026-03-21", code="000001"),
    ])

    assert result.status is LinkStatus.LINKED_VALUES_UNVERIFIED
    assert [version.role for version in result.versions] == [
        DisclosureRole.ORIGINAL_REPORT,
        DisclosureRole.CORRECTION_NOTICE,
        DisclosureRole.REVISED_REPORT,
    ]
    assert result.versions[1].candidate_predecessor_announcement_id == "1"
    assert result.versions[2].candidate_predecessor_announcement_id == "1"
    assert all(version.supersedes_verified is False for version in result.versions)
    assert result.active_report_announcement_id == "3"
    assert result.pit_eligible is False


def test_correction_notice_without_full_report_is_incomplete():
    result = link([ann("2", "关于2025年年度报告的更正公告", "2026-04-01")])
    assert result.status is LinkStatus.EVIDENCE_INCOMPLETE
    assert result.active_report_announcement_id is None


def test_supplement_notice_is_not_mislabeled_as_correction_or_revision():
    result = link([
        ann("1", "2025年年度报告", "2026-03-20"),
        ann("2", "关于2025年年度报告的补充公告", "2026-03-25"),
    ])
    assert result.versions[1].role is DisclosureRole.SUPPLEMENT_NOTICE
    assert result.versions[1].supersedes_verified is False


def test_no_matching_period_does_not_guess_from_publication_date():
    result = link([ann("q3", "2025年第三季度报告", "2025-10-30")])
    assert result.status is LinkStatus.NO_MATCH
    assert result.versions == ()


def test_unhashed_structured_values_are_rejected():
    with pytest.raises(ValueError, match="sha256"):
        link_statement_to_disclosures(
            stock_code="600519",
            period_end=date(2025, 12, 31),
            value_source_id="sina-financial",
            value_content_hashes=["not-a-hash"],
            announcements=[],
        )


def test_duplicate_pages_are_deduplicated_but_conflicting_metadata_is_rejected():
    original = ann("1", "2025年年度报告", "2026-03-20")
    result = link([original, original])
    assert len(result.versions) == 1

    conflicting = ann("1", "2025年年度报告（修订版）", "2026-04-01")
    with pytest.raises(ValueError, match="conflicting metadata"):
        link([original, conflicting])
