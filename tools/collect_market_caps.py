"""采集免费决策日总市值，写出可审计 sidecar。

腾讯收盘快照覆盖当前 900 只研究池，作为低请求量主路径；东方财富全市场
分页作为显式备源。该工具只适合行情日收盘后运行。它保存 ``source_id``、
抓取收据、原始响应哈希和观测时间；没有这些证据就不写 sidecar。

用法：
    python tools/collect_market_caps.py --as-of 2026-09-18
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.eastmoney import (  # noqa: E402
    LIST_HOST,
    SOURCE_ID as EASTMONEY_SOURCE_ID,
    EastmoneyClient,
    FetchOutcome,
)
from aquant.adapters.providers.tencent import (  # noqa: E402
    SNAPSHOT_SOURCE_ID,
    SNACK_HOST,
    TencentClient,
)
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402
from aquant.operations.decision_market_caps import (  # noqa: E402
    DecisionMarketCapError,
    MARKET_CLOSE,
    market_cap_cents_from_eastmoney_rows,
    market_cap_cents_from_values,
    market_cap_items_hash,
    sidecar_from_eastmoney_rows,
    sidecar_from_market_cap_values,
)


OUT = ROOT / "deploy" / "agentctl-q0" / "decision-market-caps.json"
ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"
POOL = ROOT / "configs" / "real-pool-csrc.yaml"


def collect_all_rows(
    client: EastmoneyClient, archive: ForwardArchive, *,
    page_size: int = 100, max_pages: int = 100,
    market_day: date | None = None,
) -> tuple[FetchOutcome, list[dict[str, Any]]]:
    """分页取得全市场，并把所有原始页收据固化成一个清单收据。

    服务端会把大页静默截断为 100 条；只抓第一页会生成一份结构正确但
    覆盖错误的 sidecar。这里要求各页 ``total`` 一致、代码不冲突且最终
    数量达到服务端声明值，否则整批失败，不发布部分市场。
    """

    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    pages: list[dict[str, Any]] = []
    expected_total: int | None = None
    for page in range(1, max_pages + 1):
        outcome, rows, total = client.universe_page(
            page=page, page_size=page_size)
        if not outcome.ok:
            return outcome, []
        if total is None or total <= 0:
            return FetchOutcome(
                False, None, outcome.receipt_id, outcome.content_hash,
                "全市场响应缺少有效 total", outcome.http_status,
            ), []
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            return FetchOutcome(
                False, None, outcome.receipt_id, outcome.content_hash,
                f"分页期间 total 变化：{expected_total} -> {total}",
                outcome.http_status,
            ), []
        if not rows:
            return FetchOutcome(
                False, None, outcome.receipt_id, outcome.content_hash,
                f"第 {page} 页为空，尚未达到 total={expected_total}",
                outcome.http_status,
            ), []

        pages.append({
            "page": page,
            "row_count": len(rows),
            "receipt_id": outcome.receipt_id,
            "content_hash": outcome.content_hash,
        })
        for row in rows:
            key = (str(row.get("f13") or ""), str(row.get("f12") or ""))
            previous = rows_by_key.get(key)
            if previous is not None and previous != row:
                return FetchOutcome(
                    False, None, outcome.receipt_id, outcome.content_hash,
                    f"分页期间证券 {key!r} 出现冲突值", outcome.http_status,
                ), []
            rows_by_key[key] = row

        if len(rows_by_key) >= expected_total:
            break
    if expected_total is None or len(rows_by_key) != expected_total:
        return FetchOutcome(
            False, None, "", None,
            f"分页不完整：取得 {len(rows_by_key)}/{expected_total or 0} 条",
        ), []

    manifest = json.dumps({
        "schema_version": "aquant.eastmoney_market_cap_pages.v1",
        "market_day": market_day.isoformat() if market_day else None,
        "items_hash": market_cap_items_hash(market_cap_cents_from_eastmoney_rows(
            list(rows_by_key.values()))),
        "total": expected_total,
        "pages": pages,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest, _ = archive.store_bytes(manifest, media_type="application/json")
    requested_at = datetime.now(timezone.utc)
    receipt = archive.record(
        source_id=EASTMONEY_SOURCE_ID,
        url=f"https://{LIST_HOST}/api/qt/clist/get#market-cap-page-manifest",
        outcome="OK",
        requested_at=requested_at,
        http_status=200,
        content_hash=digest,
        byte_size=len(manifest),
        detail=f"paginated universe manifest: {len(pages)} pages",
    )
    return FetchOutcome(
        True, manifest, receipt.receipt_id, digest, None, 200,
    ), list(rows_by_key.values())


def _pool_symbols(path: Path) -> list[str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    symbols = [str(item.get("code") or "").strip().lower()
               for item in raw.get("instruments") or []]
    valid = [symbol for symbol in symbols
             if len(symbol) == 8 and symbol[:2] in {"sh", "sz"}
             and symbol[2:].isdigit()]
    if not valid or len(valid) != len(symbols):
        raise DecisionMarketCapError(f"研究池含无效腾讯证券代码：{path}")
    return valid


def collect_tencent_pool(
    client: TencentClient, archive: ForwardArchive, *, symbols: list[str],
    market_day: date, batch_size: int = 50,
) -> tuple[FetchOutcome, dict[str, object]]:
    """按批抓取研究池总市值，并冻结每批收据清单。"""

    values: dict[str, object] = {}
    batches: list[dict[str, Any]] = []
    for start in range(0, len(symbols), batch_size):
        batch = symbols[start:start + batch_size]
        requested_ids = {symbol[:2].upper() + "." + symbol[2:]
                         for symbol in batch}
        outcome, rows = client.market_caps(batch)
        if not outcome.ok:
            return outcome, {}
        accepted_ids: set[str] = set()
        for row in rows:
            instrument_id = str(row.get("instrument_id") or "")
            if instrument_id not in requested_ids:
                continue
            try:
                quote_time = datetime.fromisoformat(str(row["market_time"]))
            except (KeyError, ValueError):
                continue
            if (quote_time.date() != market_day
                    or quote_time.time() < MARKET_CLOSE):
                continue
            values[instrument_id] = row["market_cap_yuan"]
            accepted_ids.add(instrument_id)
        batches.append({
            "batch": len(batches) + 1,
            "requested_count": len(batch),
            "accepted_count": len(accepted_ids),
            "receipt_id": outcome.receipt_id,
            "content_hash": outcome.content_hash,
        })

    manifest = json.dumps({
        "schema_version": "aquant.tencent_market_cap_batches.v1",
        "market_day": market_day.isoformat(),
        "items_hash": market_cap_items_hash(market_cap_cents_from_values(values)),
        "requested_count": len(symbols),
        "accepted_count": len(values),
        "batches": batches,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest, _ = archive.store_bytes(manifest, media_type="application/json")
    receipt = archive.record(
        source_id=SNAPSHOT_SOURCE_ID,
        url=f"https://{SNACK_HOST}/q=#market-cap-batch-manifest",
        outcome="OK",
        requested_at=datetime.now(timezone.utc),
        http_status=200,
        content_hash=digest,
        byte_size=len(manifest),
        detail=f"market-cap batch manifest: {len(batches)} batches",
    )
    return FetchOutcome(
        True, manifest, receipt.receipt_id, digest, None, 200,
    ), values


def main() -> int:
    ap = argparse.ArgumentParser(description="收盘后采集决策日总市值 sidecar")
    ap.add_argument("--as-of", required=True, help="行情日 YYYY-MM-DD")
    ap.add_argument("--output", default=str(OUT), help="sidecar 输出路径")
    ap.add_argument("--pool", default=str(POOL), help="研究池 YAML（腾讯主路径）")
    ap.add_argument("--page-size", type=int, default=100,
                    help="东财单页条数（1-100；接口会截断更大值）")
    ap.add_argument("--max-pages", type=int, default=100,
                    help="安全页数上限；达到服务端 total 后自动停止")
    args = ap.parse_args()

    archive_root = ARCHIVE_ROOT
    archive_root.mkdir(parents=True, exist_ok=True)
    con = connect(archive_root / "meta.sqlite")
    try:
        archive = ForwardArchive(con, archive_root)
        market_day = date.fromisoformat(args.as_of)
        symbols = _pool_symbols(Path(args.pool))
        outcome, values = collect_tencent_pool(
            TencentClient(archive), archive,
            symbols=symbols, market_day=market_day)
        source = SNAPSHOT_SOURCE_ID
        rows: list[dict[str, Any]] = []
        if not outcome.ok or len(values) != len(symbols):
            detail = (outcome.detail or "请求失败") if not outcome.ok else (
                f"仅覆盖 {len(values)}/{len(symbols)} 只研究池证券")
            print("腾讯总市值主路径未完整覆盖，切换东方财富分页备源："
                  + detail)
            outcome, rows = collect_all_rows(
                EastmoneyClient(archive), archive,
                page_size=args.page_size, max_pages=args.max_pages,
                market_day=market_day)
            source = EASTMONEY_SOURCE_ID
        if not outcome.ok or (not values and not rows):
            print(f"总市值采集失败：{outcome.detail or '空响应'}")
            return 1
        try:
            if source == SNAPSHOT_SOURCE_ID:
                sidecar = sidecar_from_market_cap_values(
                    values, market_cap_as_of=market_day,
                    observed_at=datetime.now(timezone.utc),
                    receipt_id=outcome.receipt_id,
                    content_hash=outcome.content_hash or "",
                    source_id=source,
                )
            else:
                sidecar = sidecar_from_eastmoney_rows(
                    rows, market_cap_as_of=market_day,
                    observed_at=datetime.now(timezone.utc),
                    receipt_id=outcome.receipt_id,
                    content_hash=outcome.content_hash or "",
                )
        except DecisionMarketCapError as exc:
            print(f"总市值 sidecar 未生成：{exc}")
            return 1
    finally:
        con.close()

    payload = sidecar.as_dict()
    payload["row_count"] = len(values) if source == SNAPSHOT_SOURCE_ID else len(rows)
    payload["accepted_count"] = len(sidecar.items)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(output)
    requested = len(symbols) if source == SNAPSHOT_SOURCE_ID else len(rows)
    print(f"已写出 {output}：{len(sidecar.items)}/{requested} 只，来源 {source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
