from __future__ import annotations

import pytest

from aquant.operations.snapshot_lifecycle import validate_snapshot_id
from aquant.operations.universe_snapshot import _cache_days


@pytest.mark.parametrize("value", ["../outside", "a/b", "C:\\outside", ""])
def test_snapshot_id_rejects_path_components(value: str) -> None:
    with pytest.raises(ValueError):
        validate_snapshot_id(value)


def test_snapshot_window_uses_market_union_and_honours_end_cutoff() -> None:
    bars = {
        "SH.600000": {"rows": [{"trading_day": "2026-09-11"}]},
        "SZ.000001": {"rows": [
            {"trading_day": "2026-09-14"},
            {"trading_day": "2026-09-15"},
        ]},
    }

    assert _cache_days(
        bars, window_start="2026-09-01", window_end="2026-09-14", window=None,
    ) == ["2026-09-11", "2026-09-14"]
