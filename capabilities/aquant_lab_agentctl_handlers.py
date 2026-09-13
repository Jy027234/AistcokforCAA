"""A-Quant Lab product-owned agentctl capability handlers.

These handlers are the product side of the agentctl integration. They receive
and return JSON-compatible mappings and must NOT import agentctl server
internals, so the same contract can later be hosted in-process or behind an
HTTP sidecar.

Design constraints inherited from the A-Quant Lab spec:
  * Read handlers never fetch a provider on the fly. They read an already
    published, immutable snapshot keyed by snapshot_id (main doc 15.4, 16.2).
  * Synthetic data is always marked SYNTHETIC and carries a watermark. The
    production research path refuses to mix synthetic and real series
    (main doc 15.4).
  * Unknown snapshot / unknown instrument return an explicit error code from
    the main doc 16.4 vocabulary instead of partial or invented data.
  * No handler here holds execute_order / write_ledger / raw_sql / run_shell /
    fetch_arbitrary_url authority (main doc 16.3). Those capabilities are
    deliberately absent from the manifest, so the model has no mechanism to
    reach them.
"""

from __future__ import annotations

from typing import Any

# --- synthetic snapshot fixture -------------------------------------------
# Deliberately tiny and explicit. This is the Q0/Q1 stand-in for the real
# snapshot store that M1 will provide. It exists so that acceptance cases can
# run deterministically without any market-data vendor contract (v0.2.2 4.3).
SNAPSHOT_ID = "snap-syn-001"
AS_OF_TIME = "2026-09-11T20:30:00+08:00"
DATA_MODE = "SYNTHETIC"

_SNAPSHOT: dict[str, Any] = {
    "snapshot_id": SNAPSHOT_ID,
    "as_of_time": AS_OF_TIME,
    "data_mode": DATA_MODE,
    "watermark": "SYNTHETIC DATA -- NOT VALID FOR RESEARCH CONCLUSIONS",
    "status": "PUBLISHED",
    "quality": {"coverage": 1.0, "missing_domains": []},
}

# instrument_id -> research card payload
_INSTRUMENTS: dict[str, dict[str, Any]] = {
    "SYN.A.600519": {
        "instrument_id": "SYN.A.600519",
        "exchange": "SSE",
        "board": "MAIN",
        "display_name": "合成示例·消费龙头",
        "factors": [
            {"factor_id": "F01", "name": "20日动量", "value": 0.0412, "unit": "ratio",
             "rank_pct": 0.71, "coverage": 1.0},
            {"factor_id": "F04", "name": "20日波动率", "value": 0.2287, "unit": "annualized",
             "rank_pct": 0.38, "coverage": 1.0},
        ],
        "limitations": [
            "合成为虚构数据，仅用于确定性与契约测试",
            "不构成投资建议，不得用于收益结论",
            "首期不支持盘中序列，仅日频",
        ],
    },
    "SYN.A.000001": {
        "instrument_id": "SYN.A.000001",
        "exchange": "SZSE",
        "board": "MAIN",
        "display_name": "合成示例·银行",
        "factors": [
            {"factor_id": "F01", "name": "20日动量", "value": -0.0155, "unit": "ratio",
             "rank_pct": 0.22, "coverage": 1.0},
            {"factor_id": "F04", "name": "20日波动率", "value": 0.1402, "unit": "annualized",
             "rank_pct": 0.83, "coverage": 1.0},
        ],
        "limitations": [
            "合成为虚构数据，仅用于确定性与契约测试",
            "不构成投资建议，不得用于收益结论",
        ],
    },
}


def _error(code: str, message: str, object_id: str, retryable: bool, repair: str) -> dict[str, Any]:
    """Main doc 16.4: every error carries an explanation, object id,
    retryability and a repair action -- never just 'analysis failed'."""
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "object_id": object_id,
            "retryable": retryable,
            "repair_action": repair,
        },
    }


async def research_card_read(invocation: dict[str, Any]) -> dict[str, Any]:
    """aquant.research_card.read -- read-only research card for one instrument
    under one fixed snapshot. No side effects, no provider access."""

    args = dict(invocation.get("validated_arguments") or {})
    instrument_id = str(args.get("instrument_id") or "").strip()
    snapshot_id = str(args.get("snapshot_id") or "").strip()

    if snapshot_id != SNAPSHOT_ID:
        return _error(
            "STALE_SNAPSHOT",
            f"snapshot {snapshot_id!r} is not published; only {SNAPSHOT_ID!r} is available",
            snapshot_id or "<empty>",
            True,
            f"retry with snapshot_id={SNAPSHOT_ID}",
        )

    card = _INSTRUMENTS.get(instrument_id)
    if card is None:
        return _error(
            "DATA_NOT_READY",
            f"instrument {instrument_id!r} is not covered by snapshot {SNAPSHOT_ID}",
            instrument_id or "<empty>",
            False,
            "use an instrument_id listed by quant.market_snapshot.read",
        )

    return {
        "ok": True,
        "snapshot_id": SNAPSHOT_ID,
        "as_of_time": AS_OF_TIME,
        "data_mode": DATA_MODE,
        "watermark": _SNAPSHOT["watermark"],
        "instrument_id": card["instrument_id"],
        "exchange": card["exchange"],
        "board": card["board"],
        "display_name": card["display_name"],
        "factors": card["factors"],
        "limitations": card["limitations"],
    }
