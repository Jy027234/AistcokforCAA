from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from aquant.operations.universe_snapshot import (
    UniverseSnapshotError,
    _build_document,
    publish_universe_snapshot,
)
from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.data.snapshot import SnapshotStore


def _write_inputs(tmp_path, *, financials: str = "{not json") -> dict[str, object]:
    days = [
        (date(2026, 1, 1) + timedelta(days=index)).isoformat()
        for index in range(120)
    ]
    instruments = []
    bars = {}
    for index in range(100):
        instrument_id = f"SH.{600000 + index:06d}"
        instruments.append({
            "instrument_id": instrument_id,
            "exchange": "SSE",
            "board": "MAIN",
            "name": f"测试{index}",
            "industry": "银行",
        })
        bars[instrument_id] = {
            "exchange": "SSE",
            "board": "MAIN",
            "prev_close_before_window": 100,
            "rows": [
                {
                    "trading_day": day,
                    "open_cents": 100,
                    "high_cents": 101,
                    "low_cents": 99,
                    "close_cents": 101,
                    "adjusted_close_cents": 101,
                    "volume_shares": 100,
                    "amount_cents": 1000,
                }
                for day in days
            ],
        }

    cache_path = tmp_path / "universe-bars.json"
    cache_path.write_text(
        json.dumps({"bars": bars, "trading_calendar": days}), encoding="utf-8")
    pool_path = tmp_path / "pool.json"
    pool_path.write_text(
        json.dumps({"classification_version": "test", "instruments": instruments}),
        encoding="utf-8",
    )
    financials_path = tmp_path / "financials-cache.json"
    financials_path.write_text(financials, encoding="utf-8")
    return {
        "cache": cache_path,
        "pool": pool_path,
        "financials": financials_path,
        "actions": tmp_path / "missing-actions.json",
        "market_caps": tmp_path / "missing-market-caps.json",
    }


def test_corrupt_financial_cache_is_an_optional_check_and_is_omitted(tmp_path):
    paths = _write_inputs(tmp_path)
    checks = []

    doc, _instruments, _quotes, _days = _build_document(
        cache=json.loads(paths["cache"].read_text(encoding="utf-8")),
        pool=json.loads(paths["pool"].read_text(encoding="utf-8")),
        window_start=None,
        window_end=None,
        window=None,
        financials_path=paths["financials"],
        actions_path=paths["actions"],
        market_caps_path=paths["market_caps"],
        check_market_caps=False,
        checks=checks,
    )

    assert "financials" not in doc
    financial_check = next(check for check in checks if check.name == "财务缓存存在")
    assert not financial_check.ok
    assert "无法读取或解析财务缓存" in financial_check.detail


def test_allow_degraded_publishes_snapshot_without_corrupt_financial_dataset(tmp_path):
    paths = _write_inputs(tmp_path)
    data_root = tmp_path / "snapshot"

    result = publish_universe_snapshot(
        data_root=data_root,
        cache_path=paths["cache"],
        pool_path=paths["pool"],
        snapshot_id="snap-financial-cache-degraded",
        financials_path=paths["financials"],
        actions_path=paths["actions"],
        market_caps_path=paths["market_caps"],
        strict_quality=False,
        promote=True,
    )

    assert result.quality_status == "DEGRADED"
    assert result.promoted
    assert "financials" not in {
        path.stem for path in result.dataset_dir.glob("*.json")
    }
    con = connect(data_root / "meta.sqlite")
    try:
        apply_migrations(con)
        datasets = SnapshotStore(con, data_root / "api").datasets(result.snapshot_id)
    finally:
        con.close()
    assert "financials" not in {item["name"] for item in datasets}


def test_corrupt_core_company_actions_cache_still_fails_closed(tmp_path):
    paths = _write_inputs(tmp_path)
    actions_path = paths["actions"]
    actions_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(UniverseSnapshotError, match="公司行为缓存"):
        _build_document(
            cache=json.loads(paths["cache"].read_text(encoding="utf-8")),
            pool=json.loads(paths["pool"].read_text(encoding="utf-8")),
            window_start=None,
            window_end=None,
            window=None,
            financials_path=paths["financials"],
            actions_path=actions_path,
            market_caps_path=paths["market_caps"],
            check_market_caps=False,
            checks=[],
        )
