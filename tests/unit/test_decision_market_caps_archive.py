"""生产发布前，总市值数值必须能回溯到已归档收据。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from aquant.domain.data.db import connect
from aquant.domain.data.forward_archive import ForwardArchive
from aquant.adapters.providers.eastmoney import FetchOutcome
from aquant.operations.decision_market_caps import (
    DecisionMarketCapError, load_sidecar,
    sidecar_from_market_cap_values,
)
from tools.collect_market_caps import collect_tencent_pool


def _archived_sidecar(tmp_path, monkeypatch):
    archive_root = tmp_path / "forward-archive"
    con = connect(archive_root / "meta.sqlite")
    archive = ForwardArchive(con, archive_root)
    now = datetime(2026, 9, 22, 8, 30, tzinfo=timezone.utc)
    raw = b'v_sh600519="archived provider response";'
    raw_hash, _ = archive.store_bytes(raw)
    raw_receipt = archive.record(
        source_id="tencent-qt", url="https://qt.gtimg.cn/q=sh600519",
        outcome="OK", requested_at=now, responded_at=now,
        content_hash=raw_hash,
        byte_size=len(raw))
    original_record = archive.record
    monkeypatch.setattr(archive, "record", lambda **kwargs: original_record(
        responded_at=now, **kwargs))
    class Client:
        def market_caps(self, symbols):
            assert symbols == ["sh600519"]
            return (FetchOutcome(True, raw, raw_receipt.receipt_id,
                                 raw_hash, None, 200), [{
                "instrument_id": "SH.600519",
                "market_time": "2026-09-22T16:30:00",
                "market_cap_yuan": "1234.56",
            }])

    outcome, values = collect_tencent_pool(
        Client(), archive, symbols=["sh600519"],
        market_day=now.date())
    sidecar = sidecar_from_market_cap_values(
        values, market_cap_as_of="2026-09-22",
        observed_at=now, receipt_id=outcome.receipt_id,
        content_hash=outcome.content_hash, source_id="tencent-qt")
    path = tmp_path / "decision-market-caps.json"
    path.write_text(json.dumps(sidecar.as_dict()), encoding="utf-8")
    con.close()
    return path, archive_root, raw_hash


def test_archived_market_cap_sidecar_is_accepted(tmp_path, monkeypatch):
    path, archive_root, _ = _archived_sidecar(tmp_path, monkeypatch)
    loaded = load_sidecar(path, expected_as_of="2026-09-22",
                          archive_root=archive_root)
    assert loaded.items["SH.600519"].market_cap_cents == 123456


def test_market_cap_value_tampering_is_rejected(tmp_path, monkeypatch):
    path, archive_root, _ = _archived_sidecar(tmp_path, monkeypatch)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["items"]["SH.600519"]["market_cap_cents"] = 123457
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DecisionMarketCapError, match="数值与归档清单不匹配"):
        load_sidecar(path, expected_as_of="2026-09-22",
                     archive_root=archive_root)


def test_missing_raw_response_is_rejected(tmp_path, monkeypatch):
    path, archive_root, raw_hash = _archived_sidecar(tmp_path, monkeypatch)
    con = connect(archive_root / "meta.sqlite")
    stored_path = con.execute(
        "SELECT stored_path FROM raw_artifact WHERE content_hash=?",
        (raw_hash,)).fetchone()[0]
    con.close()
    (archive_root / stored_path).unlink()
    with pytest.raises(DecisionMarketCapError, match="无法读取总市值原始归档"):
        load_sidecar(path, expected_as_of="2026-09-22",
                     archive_root=archive_root)


def test_tencent_intraday_quote_is_not_accepted_as_closing_cap(tmp_path):
    archive_root = tmp_path / "archive"
    con = connect(archive_root / "meta.sqlite")
    try:
        archive = ForwardArchive(con, archive_root)

        class Client:
            def market_caps(self, symbols):
                return (FetchOutcome(True, b"response", "rcpt_intraday",
                                     "sha256:intraday", None, 200), [{
                    "instrument_id": "SH.600519",
                    "market_time": "2026-09-22T14:59:59",
                    "market_cap_yuan": "1234.56",
                }])

        _, values = collect_tencent_pool(
            Client(), archive, symbols=["sh600519"],
            market_day=datetime(2026, 9, 22).date())
        assert values == {}
    finally:
        con.close()
