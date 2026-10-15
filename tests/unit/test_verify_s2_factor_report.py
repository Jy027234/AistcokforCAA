"""研究试运行报告：信号统计、PDF 复核口径及正式市值证据。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from aquant.domain.data.db import connect
from aquant.domain.data.forward_archive import ForwardArchive
from aquant.domain.data.pit import PitMode
from aquant.operations.decision_market_caps import (
    DecisionMarketCapError, market_cap_items_hash,
    sidecar_from_market_cap_values,
)
from tools import verify_s2_factors as verifier


@pytest.fixture
def archived_caps(tmp_path):
    """只在测试目录造证据，不调用供应商或写生产归档。"""
    root = tmp_path / "archive"
    con = connect(root / "meta.sqlite")
    archive = ForwardArchive(con, root)
    observed = datetime(2026, 9, 24, 8, tzinfo=timezone.utc)
    # 正式加载器会回放原始响应；合成字段也必须绑定证券、收盘时点和亿元值。
    fields = [""] * 60
    fields[2], fields[30], fields[45] = "600519", "20260924160000", "0.0000123456"
    raw = ('v_sh600519="' + "~".join(fields) + '";').encode("gbk")
    raw_hash, _ = archive.store_bytes(raw)
    receipt = archive.record(
        source_id="tencent-qt", url="https://example.invalid/raw",
        outcome="OK", requested_at=observed, responded_at=observed,
        content_hash=raw_hash, byte_size=len(raw))
    manifest = json.dumps({
        "schema_version": "aquant.tencent_market_cap_batches.v1",
        "market_day": "2026-09-24",
        "items_hash": market_cap_items_hash({"SH.600519": 123456}),
        "batches": [{"receipt_id": receipt.receipt_id, "content_hash": raw_hash}],
    }).encode()
    manifest_hash, _ = archive.store_bytes(manifest)
    manifest_receipt = archive.record(
        source_id="tencent-qt", url="https://example.invalid/manifest",
        outcome="OK", requested_at=observed, responded_at=observed,
        content_hash=manifest_hash, byte_size=len(manifest))
    sidecar = sidecar_from_market_cap_values(
        {"SH.600519": "1234.56"}, market_cap_as_of="2026-09-24",
        observed_at=observed, receipt_id=manifest_receipt.receipt_id,
        content_hash=manifest_hash, source_id="tencent-qt")
    path = tmp_path / "caps.json"
    path.write_text(json.dumps(sidecar.as_dict()), encoding="utf-8")
    raw_path = root / con.execute(
        "SELECT stored_path FROM raw_artifact WHERE content_hash=?",
        (raw_hash,)).fetchone()[0]
    con.close()
    return path, root, raw_path


@pytest.mark.parametrize("trading_day", ["2026-09-24", None])
def test_real_caps_use_formal_loader_and_archive(archived_caps, trading_day):
    path, root, _ = archived_caps
    caps, sidecar = verifier.load_market_caps(
        path, trading_day=trading_day, archive_root=root)
    assert caps == {"SH.600519": Decimal("1234.56")}
    assert sidecar["source_id"] == "tencent-qt"


@pytest.mark.parametrize("change, message", [
    ({"schema_version": "unverified"}, "schema_version"),
    ({"observed_at": "2026-09-24T06:59:00+00:00"}, "收盘后"),
    ({"receipt_id": "missing-receipt"}, "收据不匹配"),
    ({"items": {"SH.600519": {"market_cap_cents": -1}}}, "正整数"),
    ({"items": {"SH.600519": {"market_cap_cents": 123456,
                               "market_cap_as_of": "2026-09-23"}}}, "日期"),
    ({"items": {"SH.600519": {"market_cap_cents": 123457}}}, "数值与归档清单"),
])
def test_real_caps_refuse_invalid_sidecar(archived_caps, change, message):
    path, root, _ = archived_caps
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(change)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DecisionMarketCapError, match=message):
        verifier.load_market_caps(path, trading_day="2026-09-24", archive_root=root)


def test_real_caps_refuse_different_decision_day(archived_caps):
    path, root, _ = archived_caps
    with pytest.raises(DecisionMarketCapError, match="不一致"):
        verifier.load_market_caps(path, trading_day="2026-09-25", archive_root=root)


def test_real_caps_refuse_missing_archive(archived_caps, tmp_path):
    path, _, _ = archived_caps
    with pytest.raises(DecisionMarketCapError, match="归档数据库缺失"):
        verifier.load_market_caps(
            path, trading_day="2026-09-24", archive_root=tmp_path / "absent")


def test_real_caps_refuse_missing_raw_response(archived_caps):
    path, root, raw_path = archived_caps
    raw_path.unlink()
    with pytest.raises(DecisionMarketCapError, match="无法读取总市值原始归档"):
        verifier.load_market_caps(path, trading_day="2026-09-24", archive_root=root)


def _stub_computation(monkeypatch, mode, accepted):
    codes = ["600519", "000651", "000001"]
    facts = [SimpleNamespace(instrument_id=code, pit_mode=mode, version_id=code)
             for code in codes]
    repository = SimpleNamespace(
        load_with_accepted_s2_pdf_reviews=lambda: (SimpleNamespace(facts=facts), accepted))
    monkeypatch.setattr(verifier.FinancialFactRepository, "open_existing",
                        lambda path: repository)
    monkeypatch.setattr(verifier, "VersionedFinancialFactStore",
                        lambda rows: SimpleNamespace(facts=rows))

    def compute(**kwargs):
        assert all(fact.pit_mode is mode for fact in kwargs["store"].facts)
        rows = []
        for code in kwargs["instrument_ids"]:
            factors = SimpleNamespace(
                fact_version_ids=[code],
                as_dict=lambda: {key: "0.1" for key in ("F07", "F08", "F09", "F10")})
            signal = SimpleNamespace(**{field: 1 for field in (
                "f07_rank", "f08_rank", "f09_rank", "f10_rank",
                "quality_rank", "value_rank", "signal_rank")})
            missing = code == "000001"
            rows.append(SimpleNamespace(
                instrument_id=code, factors=None if missing else factors,
                signal=None if missing else signal,
                exclusion_code="S2_TTM_INCOMPLETE" if missing else None,
                exclusion_reason="missing facts" if missing else None))
        return rows

    monkeypatch.setattr(verifier, "build_s2_signals", compute)
    return codes


@pytest.mark.parametrize("mode, accepted, expected_reviewed", [
    (PitMode.HISTORICAL_RECONSTRUCTED, set(), 0),
    (PitMode.HISTORICAL_RECONSTRUCTED, {"600519", "000651"}, 0),
    (PitMode.LIVE_OBSERVED, {"600519"}, 1),
])
def test_ranked_signals_and_pdf_reviews_are_separate(
        tmp_path, monkeypatch, mode, accepted, expected_reviewed):
    codes = _stub_computation(monkeypatch, mode, accepted)
    output = tmp_path / "report.json"
    args = ["--pit-mode", mode.value, "--trading-day", "2026-09-24",
            "--market-caps", str(tmp_path / "does-not-exist.json"),
            "--output", str(output)]
    for code in codes:
        args.extend(["--instrument", code, "--placeholder-market-cap", f"{code}=1000"])
    assert verifier.main(args) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["pit_mode"] == mode.value
    assert report["ranked_count"] == sum(
        item["signal"] is not None for item in report["instruments"]) == 2
    assert report["complete_factor_count"] == 2
    assert report["reviewed_complete_factor_count"] == expected_reviewed
    assert sum(item["reviewed"] for item in report["instruments"]) == expected_reviewed
    assert report["market_cap_source"]["placeholder_used"] is True
    assert report["market_cap_source"]["archive_verified"] is False
    assert report["market_cap_source"]["path"] is None
    assert "EXPLICITLY SYNTHETIC" in report["boundary"]
    if mode is PitMode.HISTORICAL_RECONSTRUCTED:
        assert "research reconstruction" in report["boundary"]


def test_real_caps_report_records_verified_archive(archived_caps, tmp_path, monkeypatch):
    path, root, _ = archived_caps
    _stub_computation(monkeypatch, PitMode.HISTORICAL_RECONSTRUCTED, set())
    output = tmp_path / "report.json"
    assert verifier.main([
        "--pit-mode", "HISTORICAL_RECONSTRUCTED", "--instrument", "600519",
        "--market-caps", str(path), "--archive-root", str(root),
        "--trading-day", "2026-09-24", "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["market_cap_source"]["archive_verified"] is True
    assert report["market_cap_source"]["archive_root"] == str(root.resolve())
    assert report["market_cap_source"]["placeholder_used"] is False
    assert report["market_cap_matched"] == report["ranked_count"] == 1
