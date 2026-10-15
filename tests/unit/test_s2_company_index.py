"""公司级 S2 公告索引与增量水位的针对性用例。

覆盖本轮新增的行为：

* 标题命中分两级：``修订``/``补充`` 是召回噪声，只有财报更正类标记才触发
  人工复核；否则正常公司会长期挂在异常队列里；
* 水位来自本次索引自己的收据时间，并且能从上一份报告读回，形成可复现的
  增量下界；
* 任一索引未翻页耗尽即失败关闭，并给出结构化的失败状态。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from tools.collect_s2_company_index import (
    load_previous_watermarks, status_for,
)

UTC = timezone.utc


def _company(**overrides) -> dict:
    row = {"code": "000651", "titleCandidates": [], "revisionCandidates": []}
    row.update(overrides)
    return row


def test_exhausted_indexes_with_no_candidates_is_indexed() -> None:
    status, reason = status_for(_company(), report_index_ok=True, cross_index_ok=True)
    assert status == "INDEXED"
    assert "exhausted" in reason


def test_revision_titled_but_non_financial_candidates_do_not_force_review() -> None:
    """公司章程修订对照表、股东会补充通知不是财报更正，不能触发人工复核。"""

    company = _company(titleCandidates=[
        {"announcementId": "1", "title": "《公司章程》修订对照表",
         "candidateReasons": ["revision_title"]},
        {"announcementId": "2", "title": "关于年度股东会增加临时提案的补充通知",
         "candidateReasons": ["supplement_title"]},
    ])
    status, reason = status_for(company, report_index_ok=True, cross_index_ok=True)
    assert status == "INDEXED"
    assert "no financial-correction marker" in reason
    assert "2 revision/supplement-titled" in reason


def test_financial_correction_title_requires_human_disposition() -> None:
    company = _company(revisionCandidates=[
        {"announcementId": "3", "title": "2025年年度报告更正公告",
         "candidateReasons": ["correction_title"]},
    ])
    status, reason = status_for(company, report_index_ok=True, cross_index_ok=True)
    assert status == "REVISION_REVIEW_REQUIRED"
    assert "1 correction-titled" in reason


@pytest.mark.parametrize(
    "report_ok,cross_ok,expected",
    [(False, True, "INDEX_FAILED"), (True, False, "CROSS_INDEX_FAILED"),
     (False, False, "INDEX_FAILED")],
)
def test_incomplete_index_fails_closed_with_a_specific_status(
    report_ok: bool, cross_ok: bool, expected: str,
) -> None:
    status, _ = status_for(_company(), report_index_ok=report_ok,
                           cross_index_ok=cross_ok)
    assert status == expected


def test_marker_rule_is_shared_with_the_promotion_library() -> None:
    """索引层与升仓层必须用同一份会计更正判据，避免规则漂移。"""

    from aquant.adapters.providers import pdf_promotion
    from aquant.domain.fundamentals import disclosure_link

    assert pdf_promotion.FINANCIAL_CORRECTION_MARKERS == \
        disclosure_link.FINANCIAL_CORRECTION_PHRASES
    assert pdf_promotion.FINANCIAL_CORRECTION_MARKERS is not None


@pytest.mark.parametrize("title,expected", [
    # 会计口径的更正：需要人工判断影响。
    ("2025年年度报告更正公告", True),
    ("关于前期会计差错更正的公告", True),
    ("2024年半年度报告追溯调整的说明", True),
    # 实测反例：这些不含会计更正含义，不能挡住正常样本。
    ("中芯国际经修订及重述组织章程大纲及细则", False),
    ("港股公告：证券变动月报表（更正）", False),
    ("《公司章程》修订对照表", False),
    ("关于年度股东会增加临时提案的补充通知", False),
])
def test_financial_correction_title_rule(title: str, expected: bool) -> None:
    from aquant.domain.fundamentals.disclosure_link import (
        is_financial_correction_title,
    )

    assert is_financial_correction_title(title) is expected


def test_previous_watermarks_are_read_back_per_security(tmp_path: Path) -> None:
    path = tmp_path / "index.json"
    path.write_text(
        '{"companies": [{"code": "000651", "watermarkAfter": "2026-09-28T18:19:24+00:00"},'
        ' {"code": "600519"}, {"watermarkAfter": "x"}]}',
        encoding="utf-8")
    assert load_previous_watermarks(path) == {"000651": "2026-09-28T18:19:24+00:00"}


def test_missing_or_broken_previous_report_is_not_fatal(tmp_path: Path) -> None:
    assert load_previous_watermarks(tmp_path / "absent.json") == {}
    broken = tmp_path / "broken.json"
    broken.write_text("not json", encoding="utf-8")
    assert load_previous_watermarks(broken) == {}
