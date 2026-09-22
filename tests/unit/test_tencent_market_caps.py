from __future__ import annotations

from aquant.adapters.providers.eastmoney import FetchOutcome
from aquant.adapters.providers.tencent import TencentClient


def test_tencent_market_cap_parser_keeps_market_time_and_total_cap():
    payload = (
        'v_sh600519="1~贵州茅台~600519~1253.80~1252.57~1252.15~24573~'
        + "~" * 22
        + '~20260922161442~1.23~0.10~1265.88~1248.10~x~24573~308853~'
        + '0.20~19.25~~1265.88~1248.10~1.42~15673.52~15673.52~6.24";'
    ).encode("gbk")
    client = object.__new__(TencentClient)
    client.fetch = lambda url, label="": FetchOutcome(  # type: ignore[method-assign]
        True, payload, "rcpt", "sha256:test", None, 200)

    outcome, rows = client.market_caps(["sh600519"])

    assert outcome.ok
    assert rows == [{
        "instrument_id": "SH.600519",
        "symbol": "sh600519",
        "market_time": "2026-09-22T16:14:42",
        "last_price_yuan": "1253.80",
        "market_cap_yuan": "1567352000000.00",
    }]


def test_tencent_market_cap_parser_rejects_invalid_symbol_before_fetch():
    client = object.__new__(TencentClient)
    try:
        client.market_caps(["https://example.invalid/"])
    except ValueError as exc:
        assert "invalid" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("invalid symbol was accepted")
