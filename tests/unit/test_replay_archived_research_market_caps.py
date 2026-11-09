"""The archived quote replay CLI accepts only complete, locally saved evidence."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aquant.domain.data.db import connect
from aquant.domain.data.forward_archive import ForwardArchive
from aquant.operations.decision_market_caps import load_sidecar
from tools.replay_archived_research_market_caps import main


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _json_file(path, value) -> bytes:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    path.write_bytes(payload)
    return payload


def _make_sources(tmp_path):
    market_day = "2026-10-23"
    observed = datetime(2026, 10, 23, 8, 35, tzinfo=timezone.utc)
    archive_root = tmp_path / "archive"
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True)
    symbols = ["sh600000", "sh600001", "sh600002", "sz000001",
               "sz000002", "sz000003", "sh600003", "sz000004"]
    symbol_indexes = {symbol: index for index, symbol in enumerate(symbols)}
    pool_ids = ["SH.600000", "SZ.000001", "SZ.000002"]
    pool_path = source_root / "pool.json"
    pool_bytes = _json_file(pool_path, {
        "instruments": [{"instrument_id": iid,
                         "code": ("sh" if iid.startswith("SH.") else "sz") + iid[3:]}
                        for iid in pool_ids],
    })

    quotes_path = source_root / "closing-quotes.json"
    coverage_path = source_root / "quote-coverage.json"
    stage_path = source_root / "quotes-stage.json"
    quote_rows = []
    coverage_batches = []
    con = connect(archive_root / "meta.sqlite")
    archive = ForwardArchive(con, archive_root)
    try:
        for offset in range(0, len(symbols), 2):
            requested = symbols[offset:offset + 2]
            lines = []
            for symbol in requested:
                index = symbol_indexes[symbol]
                cents = 123456 + index
                fields = [""] * 46
                fields[2] = symbol[2:]
                fields[30] = "20261023163000"
                fields[45] = str(Decimal(cents) / Decimal(10_000_000_000))
                lines.append(f'v_{symbol}="' + "~".join(fields) + '";')
            raw = "".join(lines).encode("gbk")
            url = "https://qt.gtimg.cn/q=" + ",".join(requested)
            requested_at = observed - timedelta(seconds=2)
            digest, _created = archive.store_bytes(raw)
            receipt = archive.record(
                source_id="tencent-qt", url=url, outcome="OK",
                requested_at=requested_at, responded_at=observed,
                http_status=200, content_hash=digest, byte_size=len(raw),
            )
            for symbol in requested:
                index = symbol_indexes[symbol]
                cents = 123456 + index
                row = {
                    "symbol": symbol,
                    "instrument_id": ("SH." if symbol.startswith("sh") else "SZ.") + symbol[2:],
                    "official_code": symbol[2:],
                    "quote_time_raw": "20261023163000",
                    "quote_time_parsed": "2026-10-23T16:30:00",
                    "close_time_eligible": True,
                    "total_market_cap_raw": str(Decimal(cents) / Decimal(10_000_000_000)),
                    "receipt_id": receipt.receipt_id,
                    "request_url": url,
                    "content_hash": digest,
                    "requested_at": receipt.requested_at.isoformat(),
                    "responded_at": receipt.responded_at.isoformat(),
                    "first_seen_at": receipt.first_seen_at.isoformat(),
                }
                quote_rows.append(row)
            coverage_batches.append({
                "status": "ok", "requested_symbols": requested,
                "capture_mode": "network_request",
                "receipt_id": receipt.receipt_id, "url": url,
                "content_hash": digest, "byte_size": len(raw),
                "requested_at": receipt.requested_at.isoformat(),
                "responded_at": receipt.responded_at.isoformat(),
                "first_seen_at": receipt.first_seen_at.isoformat(),
            })
    finally:
        con.close()

    quotes_bytes = _json_file(quotes_path, {
        "schema": "aquant.tencent_research_quotes.v1",
        "market_date": market_day,
        "quote_time_kind": "after_close_research_snapshot",
        "rows": quote_rows,
    })
    coverage_bytes = _json_file(coverage_path, {
        "schema": "aquant.tencent_research_quote_coverage.v1",
        "market_date": market_day,
        "quote_time_kind": "after_close_research_snapshot",
        "captured_count": len(quote_rows),
        "network_batch_count": len(coverage_batches),
        "batches": coverage_batches,
    })
    baseline_path = source_root / "baseline.json"
    baseline_path.write_text("{}", encoding="utf-8")
    stage = {
        "schema": "aquant.daily_stage_checkpoint.v1",
        "stage": "QUOTES",
        "files": {
            "baseline": {"path": str(baseline_path.resolve()),
                         "sha256": _sha256(baseline_path.read_bytes())},
            "quotes": {"path": str(quotes_path.resolve()),
                       "sha256": _sha256(quotes_bytes)},
            "coverage": {"path": str(coverage_path.resolve()),
                         "sha256": _sha256(coverage_bytes)},
        },
    }
    _json_file(stage_path, stage)
    output = tmp_path / "decision-market-caps.json"
    evidence = tmp_path / "replay-evidence.json"
    return {
        "quotes": quotes_path, "coverage": coverage_path,
        "stage_manifest": stage_path, "pool": pool_path,
        "archive_root": archive_root, "output": output,
        "evidence_output": evidence, "pool_ids": pool_ids,
        "market_day": market_day, "row_count": len(quote_rows),
        "source_batch_count": len(coverage_batches),
        "baseline": baseline_path,
    }


def _argv(paths):
    return [argument for key in ("quotes", "coverage", "stage_manifest", "pool",
                                 "archive_root", "output", "evidence_output")
            for argument in ("--" + key.replace("_", "-"), str(paths[key]))]


def _source_receipt_count(archive_root):
    con = connect(archive_root / "meta.sqlite", read_only=True)
    try:
        con.execute("PRAGMA query_only=ON")
        return con.execute(
            "SELECT COUNT(*) FROM fetch_receipt WHERE source_id='tencent-qt'"
        ).fetchone()[0]
    finally:
        con.close()


def test_replay_cli_appends_and_reloads_the_complete_input_pool(tmp_path):
    paths = _make_sources(tmp_path)
    assert _source_receipt_count(paths["archive_root"]) == paths["source_batch_count"]

    assert main(_argv(paths)) == 0

    sidecar = load_sidecar(
        paths["output"], expected_as_of=paths["market_day"],
        archive_root=paths["archive_root"], cutoff=datetime.now(timezone.utc) + timedelta(seconds=1),
        expected_instrument_ids=paths["pool_ids"],
    )
    assert len(sidecar.items) == len(paths["pool_ids"])
    assert sidecar.observed_at_dt.date().isoformat() == paths["market_day"]
    assert sidecar.derived_at_dt >= sidecar.observed_at_dt
    assert _source_receipt_count(paths["archive_root"]) == paths["source_batch_count"] + 1
    evidence = json.loads(paths["evidence_output"].read_text(encoding="utf-8"))
    assert evidence["source_batch_count"] == paths["source_batch_count"]
    assert evidence["network_requests"] == 0


def test_replay_cli_rejects_changed_quote_rows_before_archive_append(tmp_path):
    paths = _make_sources(tmp_path)
    quotes = json.loads(paths["quotes"].read_text(encoding="utf-8"))
    quotes["rows"][0]["total_market_cap_raw"] = "0.0009999999"
    quotes_bytes = _json_file(paths["quotes"], quotes)
    stage = json.loads(paths["stage_manifest"].read_text(encoding="utf-8"))
    stage["files"]["quotes"]["sha256"] = _sha256(quotes_bytes)
    _json_file(paths["stage_manifest"], stage)

    assert main(_argv(paths)) == 1
    assert _source_receipt_count(paths["archive_root"]) == paths["source_batch_count"]
    assert not paths["output"].exists()
    assert not paths["evidence_output"].exists()


def test_replay_cli_checks_baseline_bytes_and_network_batch_count(tmp_path):
    paths = _make_sources(tmp_path)
    paths["baseline"].write_text("changed", encoding="utf-8")
    assert main(_argv(paths)) == 1
    assert _source_receipt_count(paths["archive_root"]) == paths["source_batch_count"]

    paths = _make_sources(tmp_path / "network-count")
    coverage = json.loads(paths["coverage"].read_text(encoding="utf-8"))
    coverage["network_batch_count"] -= 1
    coverage_bytes = _json_file(paths["coverage"], coverage)
    stage = json.loads(paths["stage_manifest"].read_text(encoding="utf-8"))
    stage["files"]["coverage"]["sha256"] = _sha256(coverage_bytes)
    _json_file(paths["stage_manifest"], stage)
    assert main(_argv(paths)) == 1
    assert _source_receipt_count(paths["archive_root"]) == paths["source_batch_count"]


def test_replay_cli_refuses_existing_output_without_appending(tmp_path):
    paths = _make_sources(tmp_path)
    paths["output"].write_text("keep", encoding="utf-8")

    try:
        main(_argv(paths))
    except SystemExit as exc:
        assert "overwrite" in str(exc)
    else:
        raise AssertionError("existing output should be refused")

    assert paths["output"].read_text(encoding="utf-8") == "keep"
    assert _source_receipt_count(paths["archive_root"]) == paths["source_batch_count"]
