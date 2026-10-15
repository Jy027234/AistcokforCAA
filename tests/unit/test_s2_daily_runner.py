"""S2 日更入口的状态判定用例（全部离线）。

这里固定本轮发现的一个真实缺陷：**退出码 0 不等于每只证券都成功**。实测
不带代理跑一遍日更，四个子命令全部 exit 0，而 5 只证券的状态全是
``INDEX_INCOMPLETE``。因此运行结论必须从产物里的状态分布判定。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "run_s2_daily", ROOT / "tools" / "run_s2_daily.py")
run_s2_daily = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_s2_daily)

_step_outcome = run_s2_daily._step_outcome
_overall = run_s2_daily._overall


def _steps(*codes: int) -> list[dict]:
    return [{"step": f"s{index}", "exit_code": code}
            for index, code in enumerate(codes)]


def test_only_success_statuses_count_as_ok() -> None:
    outcome, failures = _step_outcome("promote", {"ALREADY_PROMOTED": 5})
    assert outcome == "OK" and failures == {}


def test_all_failed_is_distinguished_from_partial() -> None:
    assert _step_outcome("promote", {"INDEX_INCOMPLETE": 5})[0] == "ALL_FAILED"
    assert _step_outcome(
        "promote", {"PROMOTED": 3, "VERIFICATION_FAILED": 1})[0] == "PARTIAL"


def test_review_required_counts_as_a_non_clean_outcome() -> None:
    """人工复核不是崩溃，但它也不是"干净"的成功。"""

    outcome, failures = _step_outcome(
        "company_index", {"INDEXED": 5, "REVISION_REVIEW_REQUIRED": 2})
    assert outcome == "PARTIAL"
    assert failures == {"REVISION_REVIEW_REQUIRED": 2}


def test_missing_artifact_is_not_silently_successful() -> None:
    outcome, failures = _step_outcome("promote", None)
    assert outcome == "NO_ARTIFACT" and failures == {}
    assert _overall(_steps(0, 0), {"coverage": (outcome, failures)}) == "ARTIFACT_MISSING"


def test_exit_zero_with_every_security_failed_is_not_overall_ok() -> None:
    """这正是实测到的情形：四步 exit 0，但没有任何证券成功。

    总评要指向**根因**（索引全失败），而不是只报"没有证券升仓"——后者会把
    读者引去查升仓，而真正坏掉的是索引。
    """

    outcomes = {
        "company_index": _step_outcome("company_index", {"INDEX_FAILED": 1}),
        "promote": _step_outcome("promote", {"INDEX_INCOMPLETE": 5}),
        "coverage": _step_outcome("coverage", {"VERIFICATION_FAILED": 5}),
    }
    assert _overall(_steps(0, 0, 0, 0), outcomes) == "INDEX_ALL_FAILED"


def test_promotion_all_failed_with_a_healthy_index_reports_no_promotion() -> None:
    outcomes = {
        "company_index": _step_outcome("company_index", {"INDEXED": 7}),
        "promote": _step_outcome("promote", {"DOWNLOAD_FAILED": 5}),
        "coverage": _step_outcome("coverage", {"VERIFICATION_FAILED": 5}),
    }
    assert _overall(_steps(0, 0, 0, 0), outcomes) == "NO_SECURITY_PROMOTED"


def test_step_failure_outranks_artifact_outcomes() -> None:
    outcomes = {"promote": _step_outcome("promote", {"PROMOTED": 1})}
    assert _overall(_steps(0, 0, 2), outcomes) == "STEP_FAILED"


def test_clean_run_reports_ok() -> None:
    outcomes = {
        "company_index": _step_outcome("company_index", {"INDEXED": 7}),
        "promote": _step_outcome("promote", {"PROMOTED": 2, "ALREADY_PROMOTED": 3}),
        "coverage": _step_outcome("coverage", {"FORMAL_FACTS_PRESENT": 1}),
    }
    assert _overall(_steps(0, 0, 0, 0), outcomes) == "OK"


def test_index_all_failed_has_its_own_status() -> None:
    outcomes = {
        "company_index": _step_outcome("company_index", {"INDEX_FAILED": 7}),
        "promote": _step_outcome("promote", {"INDEX_INCOMPLETE": 7}),
    }
    assert _overall(_steps(0, 0, 0, 0), outcomes) == "INDEX_ALL_FAILED"


def test_step_attempts_are_bounded() -> None:
    """有限重试：不允许无限循环。"""

    assert run_s2_daily.STEP_ATTEMPTS >= 1
    assert run_s2_daily.STEP_ATTEMPTS <= 5
    assert run_s2_daily.STEP_BACKOFF_SECONDS > 0
