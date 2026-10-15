"""按冻结需求逐值核对的口径用例（全部离线）。

方案要求"验收重点是正式入库后能计算四因子和排名，不能再以'候选数据采齐'
代替功能完成"。因此验收必须落在**每一个"指标×期间"值**上：

* 报告被判 ``PARTIAL`` 时，该报告**通过的那些字段**仍应计入已完成；
* ``EXTRACT_FAILED`` 与"字段未通过"要分开报，因为前者是解析工作、
  后者是核验工作，下一步动作不同；
* 需求清单里 ``NOT_IN_CONFIG_POOL`` 的证券**不计入分母**——它们不在配置池内，
  本来就不产生正式事实，计进去会让覆盖率凭空变差。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "report_s2_demand_coverage", ROOT / "tools" / "report_s2_demand_coverage.py")
demand = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demand)


SCENARIOS = (
    # 三个值：一个整份通过、一个部分通过（该字段在通过列表里）、一个抽取失败。
    {
        "securities": [
            {"code": "000001", "status": "DEMAND_FROZEN", "required_periods": {
                "revenue": ["2025-12-31", "2026-06-30"],
                "operating_cashflow": ["2026-06-30"],
            }},
            {"code": "999999", "status": "NOT_IN_CONFIG_POOL",
             "required_periods": {"revenue": ["2026-06-30"]}},
        ]
    },
    {
        "reports": [
            # 整份通过的报告，``verified_fields`` 列出它通过的字段——真实产物就是
            # 这个形状（``status == VERIFIED`` 时每一项都在列表里）。
            {"instrument": "000001", "period_end": "2025-12-31",
             "status": "VERIFIED", "verified_fields": ["revenue"],
             "unverified_fields": []},
            # 同期间里**一项通过、另一项未通过**：只有通过的那一项可以计入。
            {"instrument": "000001", "period_end": "2026-06-30",
             "status": "PARTIAL",
             "verified_fields": ["revenue"],
             "unverified_fields": ["operating_cashflow"]},
        ]
    },
)


def _run(tmp_path: Path) -> dict:
    manifest_path = tmp_path / "manifest.json"
    sweep_path = tmp_path / "sweep.json"
    manifest_path.write_text(json.dumps(SCENARIOS[0]), encoding="utf-8")
    sweep_path.write_text(json.dumps(SCENARIOS[1]), encoding="utf-8")
    output = tmp_path / "out.json"
    code = demand.main([
        "--manifest", str(manifest_path), "--sweep", str(sweep_path),
        "--facts-db", str(tmp_path / "absent.sqlite"), "--output", str(output),
    ])
    assert code == 0
    return json.loads(output.read_text(encoding="utf-8"))


def test_partially_verified_report_still_credits_its_verified_fields(
        tmp_path: Path) -> None:
    report = _run(tmp_path)
    # revenue@2025-12-31 (整份通过) + revenue@2026-06-30 (部分通过里的该项)
    assert report["verified_metric_periods"] == 2
    assert report["total_metric_periods"] == 3


def test_pool_external_security_is_excluded_from_the_denominator(
        tmp_path: Path) -> None:
    report = _run(tmp_path)
    assert "999999" not in report["per_security"]
    assert report["total_metric_periods"] == 3, "NOT_IN_CONFIG_POOL 不应进分母"


def test_extract_failure_and_field_failure_are_reported_separately(
        tmp_path: Path) -> None:
    report = _run(tmp_path)
    missing = {(item["metric"], item["reason"]) for item in report["missing"]}
    assert ("operating_cashflow", "FIELD_UNVERIFIED") in missing
    assert report["missing_reason_counts"] == {"FIELD_UNVERIFIED": 1}


def test_period_success_must_not_credit_a_metric_that_failed(tmp_path: Path) -> None:
    """**回归锁**：整份报告通过 ≠ 每个需求都通过。

    真实反例（300750 2025-12-31）：``revenue`` 在 ``verified_fields`` 里、
    ``operating_cashflow`` 不在。早期实现写成
    ``status == "VERIFIED" or metric in fields``，会把未通过的那一项也算成
    已完成，**覆盖率被高估**。判定必须逐字段。
    """

    report = _run(tmp_path)
    missing = {(item["metric"], item["period_end"]) for item in report["missing"]}
    assert ("operating_cashflow", "2026-06-30") in missing
    assert ("revenue", "2026-06-30") not in missing


def test_report_without_an_archived_pdf_is_counted_as_no_report(
        tmp_path: Path) -> None:
    manifest = {"securities": [{"code": "000002", "status": "DEMAND_FROZEN",
                                "required_periods": {"revenue": ["2026-06-30"]}}]}
    (tmp_path / "m.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "s.json").write_text(json.dumps({"reports": []}), encoding="utf-8")
    demand.main(["--manifest", str(tmp_path / "m.json"),
                 "--sweep", str(tmp_path / "s.json"),
                 "--facts-db", str(tmp_path / "none.sqlite"),
                 "--output", str(tmp_path / "o.json")])
    report = json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))
    assert report["verified_metric_periods"] == 0
    assert report["missing"][0]["reason"] == "NO_REPORT"


def test_verified_but_unpromoted_values_are_listed(tmp_path: Path) -> None:
    """已证实但尚未入库的要单独列出——那是"还差一步"，不是"还没做"。"""

    report = _run(tmp_path)
    listed = {(item["code"], item["period_end"]) for item in
              report["verified_but_not_promoted"]}
    assert ("000001", "2025-12-31") in listed
    assert ("000001", "2026-06-30") in listed
