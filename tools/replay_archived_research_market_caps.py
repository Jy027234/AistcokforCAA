#!/usr/bin/env python3
"""Replay a frozen pool's market caps from saved after-close Tencent quotes.

This command performs no HTTP requests. It validates the saved QUOTES stage,
the supplied nonempty security pool, every original quote receipt and raw payload,
then appends one new, honestly timed replay manifest receipt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.data.db import connect
from aquant.domain.data.forward_archive import ForwardArchive
from aquant.artifact_paths import resolve_artifact_path
from aquant.operations.decision_market_caps import (
    DecisionMarketCapError,
    REPLAY_MANIFEST_SCHEMA_VERSION,
    _parse_aware_datetime,
    _parse_tencent_replay_batch,
    _tencent_replay_requested_symbols,
    _validate_observation_time,
    _verified_archived_payload,
    load_sidecar,
    market_cap_cents_from_yuan,
    market_cap_items_hash,
    market_cap_pool_ids_hash,
    normalize_instrument_id,
    sidecar_from_replay_manifest,
)

_MARKET_TZ = ZoneInfo("Asia/Shanghai")
_QUOTE_ROW = re.compile(r'v_([a-z]{2}[0-9]{6})="([^"]*)";')
_HASH = re.compile(r"sha256:[0-9a-f]{64}")


class ReplayError(ValueError):
    """Saved inputs or archived receipts fail replay validation."""


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        payload = path.read_bytes()
        value = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayError(f"无法读取{label}：{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReplayError(f"{label}必须是 JSON object：{path}")
    return value, payload


def _load_pool(path: Path) -> tuple[dict[str, Any], bytes, list[str]]:
    payload = path.read_bytes()
    try:
        pool = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        try:
            import yaml
            pool = yaml.safe_load(payload)
        except Exception as exc:
            raise ReplayError(f"无法解析研究池：{path}: {exc}") from exc
    if not isinstance(pool, dict) or not isinstance(pool.get("instruments"), list):
        raise ReplayError("研究池必须包含 instruments 数组")
    ids: list[str] = []
    for item in pool["instruments"]:
        if not isinstance(item, dict):
            raise ReplayError("研究池证券必须是 object")
        iid = normalize_instrument_id(item.get("instrument_id"))
        expected_code = ("sh" if iid.startswith("SH.") else "sz") + iid[3:]
        if str(item.get("code") or "").strip().lower() != expected_code:
            raise ReplayError(f"研究池 code 与 instrument_id 不匹配：{iid}")
        ids.append(iid)
    if not ids or len(ids) != len(set(ids)):
        raise ReplayError("历史重放证券池必须非空且证券唯一")
    return pool, payload, sorted(ids)


def _verify_stage_binding(
    stage: dict[str, Any], *, quotes_path: Path, quotes_bytes: bytes,
    coverage_path: Path, coverage_bytes: bytes,
) -> None:
    if (stage.get("schema") != "aquant.daily_stage_checkpoint.v1"
            or stage.get("stage") != "QUOTES"):
        raise ReplayError("stage manifest 不是已完成的 QUOTES checkpoint")
    files = stage.get("files")
    if not isinstance(files, dict):
        raise ReplayError("QUOTES checkpoint 缺少 files 绑定")
    for role, path, body in (
        ("quotes", quotes_path, quotes_bytes),
        ("coverage", coverage_path, coverage_bytes),
    ):
        binding = files.get(role)
        if not isinstance(binding, dict):
            raise ReplayError(f"QUOTES checkpoint 缺少 {role} 文件绑定")
        try:
            saved_path = Path(str(binding.get("path") or "")).resolve(strict=True)
        except OSError as exc:
            raise ReplayError(f"QUOTES checkpoint {role} 路径不可用") from exc
        if saved_path != path.resolve(strict=True):
            raise ReplayError(f"QUOTES checkpoint {role} 路径与输入文件不一致")
        if binding.get("sha256") != _sha256(body):
            raise ReplayError(f"QUOTES checkpoint {role} SHA-256 不匹配")
    baseline = files.get("baseline")
    if (not isinstance(baseline, dict)
            or not isinstance(baseline.get("path"), str)
            or not isinstance(baseline.get("sha256"), str)
            or not _HASH.fullmatch(baseline["sha256"])):
        raise ReplayError("QUOTES checkpoint 缺少 baseline 路径/哈希绑定")
    try:
        baseline_path = resolve_artifact_path(baseline["path"]).resolve(strict=True)
        baseline_bytes = baseline_path.read_bytes()
    except (OSError, ValueError) as exc:
        raise ReplayError("QUOTES checkpoint baseline 路径不可用") from exc
    if _sha256(baseline_bytes) != baseline["sha256"]:
        raise ReplayError("QUOTES checkpoint baseline SHA-256 不匹配")


def _symbol_instrument_id(symbol: str) -> str:
    if not re.fullmatch(r"(?:sh|sz|bj)[0-9]{6}", symbol):
        raise ReplayError(f"无效的 Tencent symbol：{symbol!r}")
    return symbol[:2].upper() + "." + symbol[2:]


def _quote_rows_from_payload(payload: bytes, *, symbols: list[str],
                             market_day: date) -> dict[str, dict[str, Any]]:
    wanted = set(symbols)
    rows: dict[str, dict[str, Any]] = {}
    for match in _QUOTE_ROW.finditer(payload.decode("gbk", "replace")):
        symbol, raw_body = match.groups()
        if symbol not in wanted:
            raise ReplayError(f"Tencent 原始响应包含未请求证券：{symbol}")
        if symbol in rows:
            raise ReplayError(f"Tencent 原始响应证券重复：{symbol}")
        fields = raw_body.split("~")
        if len(fields) <= 45 or fields[2].strip() != symbol[2:]:
            raise ReplayError(f"Tencent 原始响应证券字段不完整或代码不符：{symbol}")
        quote_raw = fields[30].strip()
        quote_parsed = None
        close_eligible = False
        try:
            parsed = datetime.strptime(quote_raw, "%Y%m%d%H%M%S")
        except ValueError:
            parsed = None
        if parsed is not None:
            quote_parsed = parsed.isoformat()
            close_eligible = parsed.date() == market_day and parsed.time().isoformat() >= "15:00:00"
        rows[symbol] = {
            "symbol": symbol,
            "instrument_id": _symbol_instrument_id(symbol),
            "official_code": symbol[2:],
            "quote_time_raw": quote_raw,
            "quote_time_parsed": quote_parsed,
            "close_time_eligible": close_eligible,
            "total_market_cap_raw": fields[45].strip(),
        }
    return rows


def _verify_saved_quote_rows(
    quotes: dict[str, Any], coverage: dict[str, Any], *, market_day: date,
    rows_by_symbol: dict[str, dict[str, Any]], batch_rows: dict[str, dict[str, dict[str, Any]]],
) -> None:
    saved_rows = quotes.get("rows")
    if not isinstance(saved_rows, list) or not saved_rows:
        raise ReplayError("原始 closing-quotes.json rows 必须非空数组")
    captured_count = coverage.get("captured_count")
    if (isinstance(captured_count, bool) or not isinstance(captured_count, int)
            or captured_count != len(saved_rows)):
        raise ReplayError("quote coverage captured_count 与原始行数不一致")
    actual_symbols: set[str] = set()
    for row in saved_rows:
        if not isinstance(row, dict):
            raise ReplayError("closing-quotes.json 行格式错误")
        symbol = str(row.get("symbol") or "").lower()
        if not symbol or symbol in actual_symbols:
            raise ReplayError("closing-quotes.json 存在空白或重复 symbol")
        actual_symbols.add(symbol)
        source = rows_by_symbol.get(symbol)
        if source is None:
            raise ReplayError(f"closing-quotes.json 行没有对应原始回包：{symbol}")
        if (row.get("instrument_id") != source["instrument_id"]
                or row.get("official_code") != source["official_code"]
                or row.get("quote_time_raw") != source["quote_time_raw"]
                or row.get("quote_time_parsed") != source["quote_time_parsed"]
                or row.get("close_time_eligible") is not source["close_time_eligible"]
                or str(row.get("total_market_cap_raw") or "").strip()
                != source["total_market_cap_raw"]):
            raise ReplayError(f"closing-quotes.json 行字段与原始回包不一致：{symbol}")
        receipt_id = str(row.get("receipt_id") or "")
        batch = batch_rows.get(receipt_id, {}).get(symbol)
        if batch is None:
            raise ReplayError(f"closing-quotes.json 行与批次收据不匹配：{symbol}")
        for qfield, bfield in (("content_hash", "content_hash"),
                               ("request_url", "url"),
                               ("requested_at", "requested_at"),
                               ("responded_at", "responded_at"),
                               ("first_seen_at", "first_seen_at")):
            if row.get(qfield) != batch.get(bfield):
                raise ReplayError(f"closing-quotes.json {qfield} 与收据不匹配：{symbol}")
    if len(rows_by_symbol) != len(saved_rows) or actual_symbols != set(rows_by_symbol):
        raise ReplayError("closing-quotes.json symbol 集合与原始回包不一致")


def _verify_source_inputs(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any],
                                                               list[str], bytes, dict[str, Any],
                                                               datetime, dict[str, int], dict[str, dict[str, Any]]]:
    quotes_path = Path(args.quotes).resolve(strict=True)
    coverage_path = Path(args.coverage).resolve(strict=True)
    stage_path = Path(args.stage_manifest).resolve(strict=True)
    pool_path = Path(args.pool).resolve(strict=True)
    archive_root = Path(args.archive_root).resolve(strict=True)
    quotes, quotes_bytes = _read_json(quotes_path, "原始收盘报价")
    coverage, coverage_bytes = _read_json(coverage_path, "原始报价 coverage")
    stage, _stage_bytes = _read_json(stage_path, "QUOTES stage manifest")
    _verify_stage_binding(stage, quotes_path=quotes_path, quotes_bytes=quotes_bytes,
                          coverage_path=coverage_path, coverage_bytes=coverage_bytes)
    pool, pool_bytes, pool_ids = _load_pool(pool_path)
    market_day_text = quotes.get("market_date")
    try:
        market_day = date.fromisoformat(str(market_day_text))
    except ValueError as exc:
        raise ReplayError("closing-quotes.json market_date 无效") from exc
    if (quotes.get("schema") != "aquant.tencent_research_quotes.v1"
            or quotes.get("quote_time_kind") != "after_close_research_snapshot"
            or coverage.get("schema") != "aquant.tencent_research_quote_coverage.v1"
            or coverage.get("market_date") != market_day.isoformat()
            or coverage.get("quote_time_kind") != "after_close_research_snapshot"):
        raise ReplayError("原始报价日期、类型或 schema 不匹配")
    batches = coverage.get("batches")
    if not isinstance(batches, list) or not batches:
        raise ReplayError("原始 quote coverage 必须包含非空 batch receipts")
    declared_network_batches = coverage.get("network_batch_count")
    if declared_network_batches is not None:
        actual_network_batches = sum(
            1 for batch in batches if isinstance(batch, dict)
            and batch.get("capture_mode") == "network_request")
        if (isinstance(declared_network_batches, bool)
                or not isinstance(declared_network_batches, int)
                or declared_network_batches != actual_network_batches):
            raise ReplayError("quote coverage network_batch_count 与 batches 不一致")
    if not isinstance(quotes.get("rows"), list) or not quotes["rows"]:
        raise ReplayError("原始 closing-quotes.json rows 必须非空数组")

    pool_symbols = {
        ("sh" if iid.startswith("SH.") else "sz") + iid[3:]: iid
        for iid in pool_ids
    }
    items: dict[str, int] = {}
    item_rows: dict[str, dict[str, Any]] = {}
    rows_by_symbol: dict[str, dict[str, Any]] = {}
    batch_rows: dict[str, dict[str, dict[str, Any]]] = {}
    manifest_batches: list[dict[str, Any]] = []
    source_receipts: list[dict[str, Any]] = []
    source_observation_times: list[datetime] = []
    seen_receipts: set[str] = set()
    con = connect(archive_root / "meta.sqlite", read_only=True)
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        for batch in batches:
            if not isinstance(batch, dict) or batch.get("status") != "ok":
                raise ReplayError("原始 quote batch 未完成或格式错误")
            receipt_id = str(batch.get("receipt_id") or "")
            digest = str(batch.get("content_hash") or "")
            requested_symbols = batch.get("requested_symbols")
            url = str(batch.get("url") or "")
            if not receipt_id or receipt_id in seen_receipts:
                raise ReplayError("原始 quote batch 收据 ID 为空或重复")
            seen_receipts.add(receipt_id)
            if not isinstance(requested_symbols, list) or not requested_symbols:
                raise ReplayError(f"原始 quote batch 缺少 requested_symbols：{receipt_id}")
            if _tencent_replay_requested_symbols(url) != requested_symbols:
                raise ReplayError(f"原始 quote batch URL 与 requested_symbols 不一致：{receipt_id}")
            receipt, payload = _verified_archived_payload(
                con, archive_root, receipt_id=receipt_id,
                content_hash=digest, source_id="tencent-qt")
            if int(receipt["http_status"] or 0) != 200:
                raise ReplayError(f"原始 quote batch HTTP status 不是 200：{receipt_id}")
            for field in ("url", "content_hash", "byte_size"):
                if batch.get(field) != receipt[field]:
                    if field == "byte_size" and int(batch.get(field, -1)) == int(receipt[field]):
                        continue
                    raise ReplayError(f"原始 quote coverage {field} 与 SQL 收据不一致：{receipt_id}")
            for field in ("requested_at", "responded_at", "first_seen_at"):
                saved = _parse_aware_datetime(batch.get(field), f"coverage {field}")
                actual = _parse_aware_datetime(receipt[field], f"SQL {field}")
                if saved != actual:
                    raise ReplayError(f"原始 quote coverage {field} 与 SQL 收据不一致：{receipt_id}")
            requested_at = _parse_aware_datetime(receipt["requested_at"], "SQL requested_at")
            responded_at = _parse_aware_datetime(receipt["responded_at"], "SQL responded_at")
            first_seen_at = _parse_aware_datetime(receipt["first_seen_at"], "SQL first_seen_at")
            if requested_at > responded_at or first_seen_at != responded_at:
                raise ReplayError(f"原始 quote SQL 时钟不一致：{receipt_id}")
            try:
                _validate_observation_time(responded_at, market_day)
            except DecisionMarketCapError as exc:
                raise ReplayError(f"原始 quote receipt 不是当日收盘后观测：{receipt_id}: {exc}") from exc
            parsed_rows = _quote_rows_from_payload(payload, symbols=requested_symbols,
                                                    market_day=market_day)
            batch_rows[receipt_id] = {}
            requested_pool_ids = {
                iid for symbol, iid in pool_symbols.items() if symbol in requested_symbols
            }
            selected_symbols = {symbol for symbol, iid in pool_symbols.items()
                                if iid in requested_pool_ids}
            selected_raw = _parse_tencent_replay_batch(
                payload, market_day=market_day, expected_symbols=selected_symbols,
                receipt_id=receipt_id)
            accepted_ids = sorted(requested_pool_ids)
            for symbol, row in parsed_rows.items():
                row.update({
                    "receipt_id": receipt_id,
                    "content_hash": digest,
                    "url": url,
                    "requested_at": receipt["requested_at"],
                    "responded_at": receipt["responded_at"],
                    "first_seen_at": receipt["first_seen_at"],
                })
                rows_by_symbol[symbol] = row
                batch_rows[receipt_id][symbol] = row
            for iid in requested_pool_ids:
                symbol = ("sh" if iid.startswith("SH.") else "sz") + iid[3:]
                raw_row = parsed_rows.get(symbol)
                if raw_row is None or not raw_row["close_time_eligible"]:
                    raise ReplayError(f"pool 行情缺失或不是收盘后：{iid}")
                cents, quote_time = selected_raw[symbol]
                try:
                    normalized_cents = market_cap_cents_from_yuan(
                        Decimal(raw_row["total_market_cap_raw"]) * Decimal("100000000"))
                except (InvalidOperation, DecisionMarketCapError) as exc:
                    raise ReplayError(f"pool 总市值不可解析：{iid}") from exc
                if normalized_cents != cents:
                    raise ReplayError(f"pool 市值字段与 raw replay 不一致：{iid}")
                if iid in items:
                    raise ReplayError(f"证券出现在多个 raw receipt：{iid}")
                items[iid] = cents
                item_rows[iid] = {
                    "market_cap_cents": cents,
                    "quote_time": quote_time,
                    "receipt_id": receipt_id,
                    "content_hash": digest,
                }
            manifest_batches.append({
                "receipt_id": receipt_id,
                "content_hash": digest,
                "url": url,
                "requested_symbols": requested_symbols,
                "requested_count": len(requested_symbols),
                "accepted_ids": accepted_ids,
                "accepted_count": len(accepted_ids),
                "requested_at": receipt["requested_at"],
                "responded_at": receipt["responded_at"],
                "byte_size": int(receipt["byte_size"]),
            })
            source_observation_times.append(responded_at)
            source_receipts.append({
                "receipt_id": receipt_id,
                "content_hash": digest,
                "url": url,
                "requested_at": receipt["requested_at"],
                "responded_at": receipt["responded_at"],
                "first_seen_at": receipt["first_seen_at"],
                "byte_size": int(receipt["byte_size"]),
            })
        con.rollback()
    finally:
        con.close()
    if set(items) != set(pool_ids):
        missing = sorted(set(pool_ids) - set(items))
        extra = sorted(set(items) - set(pool_ids))
        raise ReplayError(f"原始批次未完整覆盖输入证券池 missing={missing[:10]} extra={extra[:10]}")
    _verify_saved_quote_rows(
        quotes, coverage, market_day=market_day,
        rows_by_symbol=rows_by_symbol, batch_rows=batch_rows)
    observation = max(source_observation_times)
    source_snapshot = {
        "quotes_path": str(quotes_path), "quotes_sha256": _sha256(quotes_bytes),
        "quotes_bytes": len(quotes_bytes),
        "coverage_path": str(coverage_path), "coverage_sha256": _sha256(coverage_bytes),
        "coverage_bytes": len(coverage_bytes),
        "stage_manifest_path": str(stage_path),
        "stage_manifest_sha256": _sha256(stage_path.read_bytes()),
        "stage_manifest_bytes": stage_path.stat().st_size,
        "market_date": market_day.isoformat(),
        "quote_time_kind": quotes["quote_time_kind"],
        "row_count": len(quotes["rows"]),
        "batch_count": len(batches),
        "original_source_receipts": source_receipts,
    }
    return quotes, coverage, pool_ids, pool_bytes, {
        "market_day": market_day.isoformat(),
        "source_observed_at": observation.isoformat(),
        "pool": {
            "instrument_ids": pool_ids,
            "instrument_ids_hash": market_cap_pool_ids_hash(pool_ids),
            "content_hash": _sha256(pool_bytes),
            "requested_count": len(pool_ids), "accepted_count": len(pool_ids),
        },
        "source_quote_snapshot": source_snapshot,
        "batches": manifest_batches,
        "items": item_rows,
        "requested_count": len(pool_ids), "accepted_count": len(pool_ids),
        "items_hash": market_cap_items_hash(items),
    }, observation, items, item_rows


def _write_json_no_overwrite(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("quotes", "coverage", "stage-manifest", "pool", "archive-root",
                 "output", "evidence-output"):
        parser.add_argument("--" + name, required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output = args.output.resolve()
    evidence_output = args.evidence_output.resolve()
    if output == evidence_output:
        raise SystemExit("--output and --evidence-output must be different paths")
    if output.exists() or evidence_output.exists():
        raise SystemExit("refusing to overwrite an existing replay output")
    if not output.parent.is_dir() or not evidence_output.parent.is_dir():
        raise SystemExit("output directories must already exist")
    try:
        (_quotes, _coverage, ids, pool_bytes, manifest_base, _observed,
         values, items) = _verify_source_inputs(args)
        derived_at = datetime.now(timezone.utc)
        manifest = {
            "schema_version": REPLAY_MANIFEST_SCHEMA_VERSION,
            "source_id": "tencent-qt",
            "derived_at": derived_at.isoformat(),
            **manifest_base,
        }
        body = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
        digest = _sha256(body)
        archive_root = args.archive_root.resolve(strict=True)
        con = connect(archive_root / "meta.sqlite")
        try:
            archive = ForwardArchive(con, archive_root)
            stored_hash, _created = archive.store_bytes(body, media_type="application/json")
            if stored_hash != digest:
                raise ReplayError("派生清单归档哈希不一致")
            receipt = archive.record(
                source_id="tencent-qt",
                url="https://qt.gtimg.cn/q=#market-cap-replay-manifest",
                outcome="OK", requested_at=derived_at, responded_at=derived_at,
                http_status=200, content_hash=digest, byte_size=len(body),
                detail=f"historical Tencent quote replay for {len(ids)} pool securities",
            )
        finally:
            con.close()
        sidecar = sidecar_from_replay_manifest(
            manifest, receipt_id=receipt.receipt_id, content_hash=digest)
        _write_json_no_overwrite(output, sidecar.as_dict())
        verified_at = datetime.now(timezone.utc)
        loaded = load_sidecar(
            output, expected_as_of=manifest["market_day"],
            archive_root=archive_root, cutoff=verified_at,
            expected_instrument_ids=ids)
        if ({iid: item.market_cap_cents for iid, item in loaded.items.items()}
                != values or loaded.derived_at_dt != derived_at):
            raise ReplayError("输出 sidecar 重载与已重算数值/派生时钟不一致")
        evidence = {
            "schema": "aquant.decision_market_caps.replay_evidence.v1",
            "status": "complete",
            "market_day": manifest["market_day"],
            "source_observed_at": manifest["source_observed_at"],
            "derived_at": manifest["derived_at"],
            "verified_at": verified_at.isoformat(),
            "pool_count": len(ids),
            "pool_content_hash": _sha256(pool_bytes),
            "source_quote_snapshot": manifest["source_quote_snapshot"],
            "source_batch_count": len(manifest["batches"]),
            "source_receipt_ids": [batch["receipt_id"] for batch in manifest["batches"]],
            "source_items_hash": manifest["items_hash"],
            "derived_receipt_id": receipt.receipt_id,
            "derived_content_hash": digest,
            "output": str(output),
            "network_requests": 0,
        }
        _write_json_no_overwrite(evidence_output, evidence)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    print(json.dumps({
        "status": "complete", "market_day": manifest["market_day"],
        "pool_count": len(ids), "source_batch_count": len(manifest["batches"]),
        "derived_receipt_id": receipt.receipt_id, "content_hash": digest,
        "output": str(output), "evidence_output": str(evidence_output),
        "network_requests": 0,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
