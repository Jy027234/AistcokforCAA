"""生产发布前，总市值数值必须能回溯到已归档收据。"""

from __future__ import annotations

import json
import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from aquant.domain.data.db import connect
from aquant.domain.data.forward_archive import ForwardArchive
from aquant.adapters.providers.eastmoney import FetchOutcome
from aquant.operations.universe_snapshot import UniverseSnapshotError, _build_document
from aquant.operations.decision_market_caps import (
    DecisionMarketCapError, load_sidecar,
    sidecar_from_market_cap_values,
    sidecar_from_replay_manifest, verify_frozen_market_caps_archive,
    market_cap_items_hash, market_cap_pool_ids_hash, normalize_instrument_id,
    inject_sidecar, REPLAY_MANIFEST_SCHEMA_VERSION,
)
from tools.collect_market_caps import collect_tencent_pool


def _archived_sidecar(tmp_path, monkeypatch, *, raw_override=None):
    archive_root = tmp_path / "forward-archive"
    con = connect(archive_root / "meta.sqlite")
    archive = ForwardArchive(con, archive_root)
    now = datetime(2026, 9, 22, 8, 30, tzinfo=timezone.utc)
    fields = [""] * 46
    fields[2], fields[30], fields[45] = "600519", "20260922163000", "0.0000123456"
    raw = ('v_sh600519="' + "~".join(fields) + '";').encode("gbk")
    if raw_override is not None:
        raw = raw_override
    raw_hash, _ = archive.store_bytes(raw)
    raw_receipt = archive.record(
        source_id="tencent-qt", url="https://qt.gtimg.cn/q=sh600519",
        outcome="OK", requested_at=now, responded_at=now,
        content_hash=raw_hash,
        byte_size=len(raw))
    original_record = archive.record
    monkeypatch.setattr(archive, "record", lambda **kwargs: original_record(
        **{**kwargs, "requested_at": now, "responded_at": now}))
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


@pytest.mark.parametrize("kind", ["no_fields", "wrong_day", "before_close", "wrong_value", "wrong_identity"])
def test_sidecar_self_claim_cannot_replace_actual_raw_market_cap_evidence(tmp_path, monkeypatch, kind):
    fields = [""] * 46
    fields[2], fields[30], fields[45] = "600519", "20260922163000", "0.0000123456"
    if kind == "wrong_day":
        fields[30] = "20260921163000"
    elif kind == "before_close":
        fields[30] = "20260922145959"
    elif kind == "wrong_value":
        fields[45] = "0.0000223456"
    elif kind == "wrong_identity":
        fields[2] = "600000"
    payload = (b'v_sh600519="archived provider response";' if kind == "no_fields" else
               ('v_sh600519="' + "~".join(fields) + '";').encode("gbk"))
    path, archive_root, _ = _archived_sidecar(tmp_path, monkeypatch, raw_override=payload)
    with pytest.raises(DecisionMarketCapError, match="原始数值不匹配"):
        load_sidecar(path, expected_as_of="2026-09-22", archive_root=archive_root)


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


def _real_frozen_cap(tmp_path):
    archive_root = tmp_path / "real-archive"
    con = connect(archive_root / "meta.sqlite")
    archive = ForwardArchive(con, archive_root)
    now = datetime(2026, 9, 22, 8, 30, tzinfo=timezone.utc)
    lines = []
    values = {"SH.600519": 123456, "SZ.000333": 246912}
    for iid, cents in values.items():
        fields = [""] * 46
        fields[2] = iid[3:]
        fields[30] = "20260922163000"
        fields[45] = str(cents / 10000000000)
        lines.append(f'v_{iid[:2].lower()}{iid[3:]}="' + "~".join(fields) + '";')
    payload = "".join(lines).encode("gbk")
    raw_hash, _ = archive.store_bytes(payload)
    raw_receipt = archive.record(source_id="tencent-qt", url="https://qt.gtimg.cn/q=sh600519,sz000333",
        outcome="OK", requested_at=now, responded_at=now, content_hash=raw_hash, byte_size=len(payload))
    manifest = {"schema_version": "aquant.tencent_market_cap_batches.v1", "market_day": "2026-09-22",
                "items_hash": market_cap_items_hash(values), "batches": [{
                    "receipt_id": raw_receipt.receipt_id, "content_hash": raw_hash,
                    "accepted_ids": list(values)}]}
    body = json.dumps(manifest).encode()
    digest, _ = archive.store_bytes(body)
    receipt = archive.record(source_id="tencent-qt", url="https://qt.gtimg.cn/q=#manifest",
        outcome="OK", requested_at=now, responded_at=now, content_hash=digest, byte_size=len(body))
    row = {"instrument_id": "SH.600519", "market_cap_cents": 123456,
           "market_cap_as_of": "2026-09-22", "market_cap_source_id": "tencent-qt",
           "market_cap_receipt_id": receipt.receipt_id, "market_cap_content_hash": digest,
           "market_cap_observed_at": now.isoformat()}
    con.close()
    return archive_root, row, now


def test_frozen_subset_cap_replays_exact_raw_values_and_receipts(tmp_path):
    archive, row, cutoff = _real_frozen_cap(tmp_path)
    verify_frozen_market_caps_archive([row], archive_root=archive,
                                      expected_as_of="2026-09-22", cutoff=cutoff)
    row["market_cap_cents"] += 1
    with pytest.raises(DecisionMarketCapError, match="与原始响应不匹配"):
        verify_frozen_market_caps_archive([row], archive_root=archive,
                                          expected_as_of="2026-09-22", cutoff=cutoff)


def test_frozen_cap_cannot_backfill_an_earlier_cutoff(tmp_path):
    archive, row, cutoff = _real_frozen_cap(tmp_path)
    with pytest.raises(DecisionMarketCapError, match="晚于决策cutoff"):
        verify_frozen_market_caps_archive([row], archive_root=archive,
            expected_as_of="2026-09-22", cutoff=cutoff - timedelta(seconds=1))


def _replay_sidecar(tmp_path, *, manifest_mutator=None,
                    raw_url="https://qt.gtimg.cn/q=sh600519,sz000333"):
    archive_root = tmp_path / "replay-archive"
    con = connect(archive_root / "meta.sqlite")
    archive = ForwardArchive(con, archive_root)
    observed = datetime(2026, 10, 23, 8, 35, tzinfo=timezone.utc)
    derived = datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc)
    pool_ids = ["SH.600519", "SZ.000333"]
    values = {"SH.600519": 123456, "SZ.000333": 246912}
    fields_by_id = {}
    for iid, cents in values.items():
        fields = [""] * 46
        fields[2] = iid[3:]
        fields[30] = "20261023163000"
        fields[45] = str(Decimal(cents) / Decimal(10_000_000_000))
        fields_by_id[iid] = fields
    raw = "".join(
        f'v_{iid[:2].lower()}{iid[3:]}="' + "~".join(fields) + '";'
        for iid, fields in fields_by_id.items()
    ).encode("gbk")
    raw_hash, _ = archive.store_bytes(raw)
    raw_receipt = archive.record(
        source_id="tencent-qt",
        url=raw_url,
        outcome="OK", requested_at=observed - timedelta(seconds=2),
        responded_at=observed, http_status=200,
        content_hash=raw_hash, byte_size=len(raw),
    )
    pool_bytes = b"SH.600519\nSZ.000333\n"
    snapshot_files = {
        "quotes": b'{"market_date":"2026-10-23","rows":2}',
        "coverage": b'{"batches":1}',
        "stage_manifest": b'{"files":{}}',
    }
    manifest = {
        "schema_version": REPLAY_MANIFEST_SCHEMA_VERSION,
        "source_id": "tencent-qt",
        "market_day": "2026-10-23",
        "source_observed_at": observed.isoformat(),
        "derived_at": derived.isoformat(),
        "requested_count": len(pool_ids),
        "accepted_count": len(pool_ids),
        "items_hash": market_cap_items_hash(values),
        "pool": {
            "instrument_ids": pool_ids,
            "instrument_ids_hash": market_cap_pool_ids_hash(pool_ids),
            "content_hash": "sha256:" + hashlib.sha256(pool_bytes).hexdigest(),
            "requested_count": len(pool_ids),
            "accepted_count": len(pool_ids),
        },
        "source_quote_snapshot": {
            "market_date": "2026-10-23",
            "quote_time_kind": "after_close_research_snapshot",
            "quotes_sha256": "sha256:" + hashlib.sha256(snapshot_files["quotes"]).hexdigest(),
            "quotes_bytes": len(snapshot_files["quotes"]),
            "coverage_sha256": "sha256:" + hashlib.sha256(snapshot_files["coverage"]).hexdigest(),
            "coverage_bytes": len(snapshot_files["coverage"]),
            "stage_manifest_sha256": "sha256:" + hashlib.sha256(snapshot_files["stage_manifest"]).hexdigest(),
            "stage_manifest_bytes": len(snapshot_files["stage_manifest"]),
            "row_count": 6174,
            "batch_count": 1,
        },
        "batches": [{
            "receipt_id": raw_receipt.receipt_id,
            "content_hash": raw_hash,
            "url": raw_url,
            "requested_symbols": ["sh600519", "sz000333"],
            "accepted_ids": pool_ids,
            "requested_at": (observed - timedelta(seconds=2)).isoformat(),
            "responded_at": observed.isoformat(),
            "byte_size": len(raw),
        }],
        "items": {
            iid: {
                "market_cap_cents": cents,
                "quote_time": "2026-10-23T16:30:00+08:00",
                "receipt_id": raw_receipt.receipt_id,
                "content_hash": raw_hash,
            }
            for iid, cents in values.items()
        },
    }
    if manifest_mutator is not None:
        manifest_mutator(manifest)
    body = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    digest, _ = archive.store_bytes(body)
    derived_receipt = archive.record(
        source_id="tencent-qt", url="https://qt.gtimg.cn/q=#market-cap-replay-manifest",
        outcome="OK", requested_at=derived, responded_at=derived,
        http_status=200, content_hash=digest, byte_size=len(body),
    )
    sidecar = sidecar_from_replay_manifest(
        manifest, receipt_id=derived_receipt.receipt_id, content_hash=digest)
    path = tmp_path / "decision-market-caps-replay.json"
    path.write_text(json.dumps(sidecar.as_dict()), encoding="utf-8")
    con.close()
    return path, archive_root, sidecar, raw_hash, observed, derived, pool_ids


def test_replay_sidecar_checks_raw_and_derived_clocks_against_pit_cutoff(tmp_path):
    path, archive, sidecar, _raw_hash, observed, derived, pool_ids = _replay_sidecar(tmp_path)
    assert sidecar.observed_at_dt == observed
    assert sidecar.derived_at_dt == derived
    for cutoff in (
        datetime(2026, 10, 23, 23, 59, tzinfo=timezone(timedelta(hours=8))),
        datetime(2026, 10, 24, 23, 59, tzinfo=timezone(timedelta(hours=8))),
    ):
        with pytest.raises(DecisionMarketCapError, match="派生清单可用时间晚于决策cutoff"):
            load_sidecar(path, expected_as_of="2026-10-23", archive_root=archive,
                         cutoff=cutoff, expected_instrument_ids=pool_ids)
    loaded = load_sidecar(
        path, expected_as_of="2026-10-23", archive_root=archive,
        cutoff=derived + timedelta(minutes=1), expected_instrument_ids=pool_ids)
    instruments = [{"instrument_id": iid} for iid in pool_ids]
    assert inject_sidecar(instruments, loaded) == (2, 0)
    assert instruments[0]["market_cap_derived_at"] == derived.isoformat()
    verify_frozen_market_caps_archive(
        instruments, archive_root=archive, expected_as_of="2026-10-23",
        cutoff=derived + timedelta(minutes=1), expected_instrument_ids=pool_ids)
    with pytest.raises(DecisionMarketCapError, match="snapshot完整证券池不一致"):
        verify_frozen_market_caps_archive(
            instruments, archive_root=archive, expected_as_of="2026-10-23",
            cutoff=derived + timedelta(minutes=1), expected_instrument_ids=pool_ids[:1])
    instruments[0]["market_cap_derived_at"] = (derived - timedelta(days=1)).isoformat()
    with pytest.raises(DecisionMarketCapError, match="冻结派生时钟与真实清单收据不匹配"):
        verify_frozen_market_caps_archive(
            instruments, archive_root=archive, expected_as_of="2026-10-23",
            cutoff=derived + timedelta(minutes=1))
    instruments[0]["market_cap_derived_at"] = derived.isoformat()
    with pytest.raises(DecisionMarketCapError, match="派生清单可用时间晚于决策cutoff"):
        verify_frozen_market_caps_archive(
            instruments, archive_root=archive, expected_as_of="2026-10-23",
            cutoff=datetime(2026, 10, 24, 23, 59, tzinfo=timezone(timedelta(hours=8))))


def test_publisher_rejects_late_replay_and_injects_only_when_available(tmp_path):
    path, archive, _sidecar, _raw_hash, _observed, derived, pool_ids = _replay_sidecar(tmp_path)
    day = "2026-10-23"
    pool = {"classification_version": "test", "instruments": [
        {"instrument_id": iid,
         "exchange": "SSE" if iid.startswith("SH.") else "SZSE",
         "board": "MAIN", "name": iid, "industry": "C39 测试行业"}
        for iid in pool_ids
    ]}
    cache = {
        "bars": {
            iid: {
                "exchange": "SSE" if iid.startswith("SH.") else "SZSE",
                "board": "MAIN", "prev_close_before_window": 100,
                "rows": [{
                    "trading_day": day, "open_cents": 100, "high_cents": 101,
                    "low_cents": 99, "close_cents": 100,
                    "adjusted_close_cents": 100, "volume_shares": 100,
                    "amount_cents": 1000,
                }],
            }
            for iid in pool_ids
        },
        "trading_calendar": [day],
    }
    common = {
        "cache": cache, "pool": pool, "window_start": None,
        "window_end": None, "window": None,
        "financials_path": tmp_path / "missing-financials.json",
        "actions_path": tmp_path / "missing-actions.json",
        "market_caps_path": path, "check_market_caps": True,
        "market_caps_archive_root": archive,
    }
    old_cutoff = datetime(2026, 10, 24, 23, 59,
                          tzinfo=timezone(timedelta(hours=8)))
    with pytest.raises(UniverseSnapshotError, match="派生清单可用时间晚于决策cutoff"):
        _build_document(**common, checks=[], financial_cutoff=old_cutoff)

    cutoff = derived + timedelta(minutes=1)
    _doc, instruments, _quotes, _days = _build_document(
        **common, checks=[], financial_cutoff=cutoff)
    assert {row["market_cap_derived_at"] for row in instruments} == {derived.isoformat()}
    verify_frozen_market_caps_archive(
        instruments, archive_root=archive, expected_as_of=day, cutoff=cutoff,
        expected_instrument_ids=pool_ids)


def test_replay_sidecar_requires_archive_and_exact_snapshot_pool(tmp_path):
    path, archive, _sidecar, _raw_hash, _observed, derived, pool_ids = _replay_sidecar(tmp_path)
    with pytest.raises(DecisionMarketCapError, match="必须绑定归档收据"):
        load_sidecar(path, expected_as_of="2026-10-23")
    with pytest.raises(DecisionMarketCapError, match="证券集合与冻结证券池不一致"):
        load_sidecar(path, expected_as_of="2026-10-23", archive_root=archive,
                     cutoff=derived + timedelta(minutes=1),
                     expected_instrument_ids=pool_ids[:1])
    loaded = load_sidecar(path, expected_as_of="2026-10-23", archive_root=archive)
    with pytest.raises(DecisionMarketCapError, match="snapshot证券池不一致"):
        inject_sidecar([{"instrument_id": pool_ids[0]}], loaded)


@pytest.mark.parametrize("tamper", ["value", "pool", "parent_hash", "quote_time"])
def test_replay_manifest_claims_cannot_replace_source_reparse(tmp_path, tamper):
    def mutate(manifest):
        if tamper == "value":
            manifest["items"]["SH.600519"]["market_cap_cents"] += 1
        elif tamper == "pool":
            manifest["pool"]["instrument_ids"].pop()
            manifest["pool"]["instrument_ids_hash"] = market_cap_pool_ids_hash(
                manifest["pool"]["instrument_ids"])
            manifest["pool"]["requested_count"] -= 1
            manifest["pool"]["accepted_count"] -= 1
            manifest["requested_count"] -= 1
            manifest["accepted_count"] -= 1
        elif tamper == "parent_hash":
            manifest["batches"][0]["content_hash"] = "sha256:" + "0" * 64
        elif tamper == "quote_time":
            manifest["items"]["SH.600519"]["quote_time"] = "2026-10-23T14:59:59+08:00"

    path, archive, _sidecar, _raw_hash, _observed, _derived, _pool_ids = _replay_sidecar(
        tmp_path, manifest_mutator=mutate)
    with pytest.raises(DecisionMarketCapError):
        load_sidecar(path, expected_as_of="2026-10-23", archive_root=archive)


@pytest.mark.parametrize("url", [
    "http://qt.gtimg.cn/q=sh600519,sz000333",
    "https://other.example/q=sh600519,sz000333",
    "https://qt.gtimg.cn.evil.example/q=sh600519,sz000333",
])
def test_replay_parent_receipts_require_the_tencent_https_origin(tmp_path, url):
    path, archive, _sidecar, _raw_hash, _observed, _derived, _pool_ids = _replay_sidecar(
        tmp_path, raw_url=url)
    with pytest.raises(DecisionMarketCapError, match="URL 必须是 https://qt.gtimg.cn"):
        load_sidecar(path, expected_as_of="2026-10-23", archive_root=archive)


@pytest.mark.parametrize("tamper", ["observed_at", "derived_at", "market_cap_cents"])
def test_replay_sidecar_claim_tampering_is_rejected(tmp_path, tamper):
    path, archive, _sidecar, _raw_hash, observed, derived, _pool_ids = _replay_sidecar(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if tamper == "observed_at":
        raw["observed_at"] = (observed + timedelta(seconds=1)).isoformat()
    elif tamper == "derived_at":
        raw["derived_at"] = (derived - timedelta(days=1)).isoformat()
    else:
        raw["items"]["SH.600519"]["market_cap_cents"] += 1
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(DecisionMarketCapError):
        load_sidecar(path, expected_as_of="2026-10-23", archive_root=archive)


def test_replay_raw_source_and_sql_receipt_tampering_are_rejected(tmp_path):
    path, archive, _sidecar, raw_hash, _observed, _derived, _pool_ids = _replay_sidecar(tmp_path)
    con = connect(archive / "meta.sqlite")
    stored_path = con.execute(
        "SELECT stored_path FROM raw_artifact WHERE content_hash=?", (raw_hash,)
    ).fetchone()[0]
    con.close()
    raw_path = archive / stored_path
    raw_path.write_bytes(raw_path.read_bytes() + b" ")
    with pytest.raises(DecisionMarketCapError, match="原始归档哈希不匹配"):
        load_sidecar(path, expected_as_of="2026-10-23", archive_root=archive)

    path, archive, _sidecar, _raw_hash, _observed, _derived, _pool_ids = _replay_sidecar(
        tmp_path / "sql-time",)
    con = connect(archive / "meta.sqlite")
    receipt_id = con.execute(
        "SELECT receipt_id FROM fetch_receipt WHERE url=?",
        ("https://qt.gtimg.cn/q=sh600519,sz000333",),
    ).fetchone()[0]
    con.execute("DROP TRIGGER IF EXISTS fetch_receipt_no_update")
    con.execute("UPDATE fetch_receipt SET responded_at=? WHERE receipt_id=?",
                ("2026-10-24T08:35:00+00:00", receipt_id))
    con.commit()
    con.close()
    with pytest.raises(DecisionMarketCapError, match="原始报价收据时间不匹配|原始观测"):
        load_sidecar(path, expected_as_of="2026-10-23", archive_root=archive)

    path, archive, _sidecar, _raw_hash, _observed, _derived, _pool_ids = _replay_sidecar(
        tmp_path / "derived-sql-time")
    con = connect(archive / "meta.sqlite")
    derived_receipt_id = con.execute(
        "SELECT receipt_id FROM fetch_receipt WHERE url=?",
        ("https://qt.gtimg.cn/q=#market-cap-replay-manifest",),
    ).fetchone()[0]
    con.execute("DROP TRIGGER IF EXISTS fetch_receipt_no_update")
    con.execute("UPDATE fetch_receipt SET responded_at=?, first_seen_at=? WHERE receipt_id=?",
                ("2026-10-26T02:00:00+00:00", "2026-10-26T02:00:00+00:00",
                 derived_receipt_id))
    con.commit()
    con.close()
    with pytest.raises(DecisionMarketCapError, match="derived_at 与派生清单 SQL 收据不一致"):
        load_sidecar(path, expected_as_of="2026-10-23", archive_root=archive)

    path, archive, _sidecar, _raw_hash, _observed, _derived, _pool_ids = _replay_sidecar(
        tmp_path / "raw-first-seen")
    con = connect(archive / "meta.sqlite")
    receipt_id = con.execute(
        "SELECT receipt_id FROM fetch_receipt WHERE url=?",
        ("https://qt.gtimg.cn/q=sh600519,sz000333",),
    ).fetchone()[0]
    con.execute("DROP TRIGGER IF EXISTS fetch_receipt_no_update")
    con.execute("UPDATE fetch_receipt SET first_seen_at=? WHERE receipt_id=?",
                ("2026-10-24T08:35:00+00:00", receipt_id))
    con.commit()
    con.close()
    with pytest.raises(DecisionMarketCapError, match="原始报价 SQL first_seen_at 与 responded_at 不一致"):
        load_sidecar(path, expected_as_of="2026-10-23", archive_root=archive)

    path, archive, _sidecar, _raw_hash, _observed, _derived, _pool_ids = _replay_sidecar(
        tmp_path / "derived-first-seen")
    con = connect(archive / "meta.sqlite")
    derived_receipt_id = con.execute(
        "SELECT receipt_id FROM fetch_receipt WHERE url=?",
        ("https://qt.gtimg.cn/q=#market-cap-replay-manifest",),
    ).fetchone()[0]
    con.execute("DROP TRIGGER IF EXISTS fetch_receipt_no_update")
    con.execute("UPDATE fetch_receipt SET first_seen_at=? WHERE receipt_id=?",
                ("2026-10-26T02:00:00+00:00", derived_receipt_id))
    con.commit()
    con.close()
    with pytest.raises(DecisionMarketCapError, match="派生清单 SQL first_seen_at 与 responded_at 不一致"):
        load_sidecar(path, expected_as_of="2026-10-23", archive_root=archive)


@pytest.mark.parametrize("code", ["SH.6005191", "600５１９", "SZ.600519"])
def test_security_identity_requires_exact_exchange_and_six_ascii_digits(code):
    with pytest.raises(DecisionMarketCapError):
        normalize_instrument_id(code)
