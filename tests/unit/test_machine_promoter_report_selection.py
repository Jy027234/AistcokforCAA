"""The final promotion stage selects the actual full report, not its summary."""
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from tools.promote_s2_machine_facts import _matching_full_reports, _promote_one


def _entry(title, *, period="2026-06-30", code="603365", identifier="original"):
    return SimpleNamespace(announcement=SimpleNamespace(
        title=title, report_period=period, sec_code=code, announcement_id=identifier))


@pytest.mark.parametrize("title", [
    "2026年度半年报", "2026年度半年报（修订版）", "2026年半年度报告",
])
def test_final_promoter_selects_full_h1_and_excludes_its_summary(title):
    original = _entry(title, identifier="1225505896")
    index = SimpleNamespace(matches=(original, _entry(
        "2026年度半年报摘要", identifier="1225505240")))
    assert _matching_full_reports(index, date(2026, 6, 30)) == [original.announcement]


@pytest.mark.parametrize("title", [
    "2026年度半年报摘要", "2026年度半年报英文版", "2026年度半年报业绩预告",
    "2026年度半年报提示性公告", "2025年度半年报", "2026年年度报告",
    "2026年半年度报告更正公告", "2026年半年度报告（英文）",
    "2026年度半年报（英文）", "2026年半年度报告业绩预告",
    "2026年半年度报告提示性公告",
])
def test_final_promoter_rejects_non_full_or_other_period_rows(title):
    assert not _matching_full_reports(
        SimpleNamespace(matches=(_entry(title),)), date(2026, 6, 30))


def test_final_promoter_keeps_metadata_period_and_report_uniqueness_gates():
    wrong_period = _entry("2026年度半年报", period="2025-06-30")
    assert not _matching_full_reports(
        SimpleNamespace(matches=(wrong_period,)), date(2026, 6, 30))
    original = _entry("2026年度半年报")
    revised = _entry("2026年度半年报（修订版）", identifier="revised")
    assert len(_matching_full_reports(
        SimpleNamespace(matches=(original, revised)), date(2026, 6, 30))) == 2


def test_final_promoter_keeps_exact_security_identity_check():
    index = SimpleNamespace(complete=True, matches=(
        _entry("2026年度半年报", code="600000"),))
    result = _promote_one(
        client=SimpleNamespace(report_index=lambda **kwargs: index), archive=None,
        repository=None, code="603365", company="上海水星家用纺织品股份有限公司",
        period_end=date(2026, 6, 30), fields=("revenue",), calendar=None,
        cross_through=date(2026, 10, 13), max_pages=100, commit=False,
        now=datetime.now(timezone.utc))
    assert result["status"] == "IDENTITY_MISMATCH"
