"""收盘后扩采现有正式支持的沪深股票总市值，保存原始批次和覆盖证据。

必须在行情日当天北京时间 15:00 后运行，并提供当天刷新的归档日历证据。
默认允许最多 5% 的请求证券因旧行情等原因排除；这不是全覆盖报告。
--dry-run 只列出支持范围和采集计划，不访问网络，盘前也可执行。
旧 quote_symbol / parse_market_caps 仅为历史离线解析兼容，不用于采集发布。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.tencent import TencentClient  # noqa: E402
from aquant.adapters.providers.s2_calendar_evidence import (  # noqa: E402
    _scheduled, verify_calendar_evidence,
)
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402
from aquant.operations.decision_market_caps import (  # noqa: E402
    DecisionMarketCapError, MARKET_CLOSE, _MARKET_TZ,
    market_cap_cents_from_values, market_cap_items_hash, normalize_instrument_id,
    sidecar_from_market_cap_values, verify_sidecar_archive,
)

SOURCE_ID = "tencent-qt"
HOST = "qt.gtimg.cn"
QUOTE_URL = "https://qt.gtimg.cn/q="
MANIFEST_SCHEMA = "aquant.tencent_market_cap_batches.v1"
ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"
UNIVERSE = ROOT / "data" / "s2-structured" / "universe.txt"
BATCH_SIZE = 60
MIN_COVERAGE = Decimal("0.95")
_ROW = re.compile(r'v_([a-z]{2}[0-9]{6})="([^"]*)";')


def _now() -> datetime:
    return datetime.now(timezone.utc)


def quote_symbol(code: str) -> str:
    """历史解析兼容：腾讯六位代码符号；正式范围由 prepare_universe 决定。"""
    if not re.fullmatch(r"[0-9]{6}", code):
        raise ValueError(f"not a six-digit code: {code!r}")
    if code.startswith("92") or code[0] in "48":
        return "bj" + code
    if code[0] in "69":
        return "sh" + code
    if code[0] in "023":
        return "sz" + code
    raise ValueError(f"cannot map {code!r} to a tencent quote symbol")


def parse_market_caps(payload: bytes) -> dict[str, int]:
    """旧离线接口：取字段45总市值亿元；不表示通过收盘时间核验。"""
    found: dict[str, int] = {}
    for match in _ROW.finditer(payload.decode("gbk", "replace")):
        parts = match[2].split("~")
        if len(parts) <= 45 or not re.fullmatch(r"[0-9]{6}", parts[2].strip()):
            continue
        try:
            cap = Decimal(parts[45].strip())
            if cap.is_finite() and cap > 0:
                found[parts[2].strip()] = int((cap * Decimal("100000000")).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP))
        except (InvalidOperation, ValueError):
            continue
    return found


def prepare_universe(path: Path) -> dict:
    """严格六位 ASCII 股票代码、去重，并公开正式范围外的证券。"""
    entries = path.read_text(encoding="utf-8-sig").split()
    symbols: list[str] = []
    unsupported: list[dict] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    for code in entries:
        if code in seen:
            duplicates.append(code)
            continue
        seen.add(code)
        try:
            if not re.fullmatch(r"[0-9]{6}", code):
                raise DecisionMarketCapError("必须为六位 ASCII 数字代码")
            iid = normalize_instrument_id(code)
        except DecisionMarketCapError as exc:
            unsupported.append({"code": code, "reason": str(exc)})
            continue
        symbols.append(iid[:2].lower() + iid[3:])
    return {"scope": "SH.60/68, SZ.00/30 (现有正式支持沪深股票)",
            "input_count": len(entries), "supported_count": len(symbols),
            "unsupported_count": len(unsupported), "unsupported": unsupported,
            "duplicate_count": len(duplicates), "duplicates": duplicates,
            "symbols": symbols}


def validate_collection_time(market_day: date, now: datetime) -> None:
    local = now.astimezone(_MARKET_TZ)
    if local.date() != market_day:
        raise ValueError("--as-of 必须是北京当日；免费当前行情不能回填历史收盘市值")
    if local.time() < MARKET_CLOSE:
        raise ValueError("必须在北京当日 15:00 收盘后采集；尚未请求网络")


def validate_calendar(path: Path, archive: ForwardArchive, *, now: datetime,
                      market_day: date) -> dict:
    packet = json.loads(path.read_text(encoding="utf-8-sig"))
    try:
        _, verified = verify_calendar_evidence(packet, archive, now=now)
    except (KeyError, TypeError) as exc:
        raise ValueError("日历证据结构不完整或字段类型错误") from exc
    # verify_calendar_evidence 的返回集合服务于下一次 preopen，盘后不包含今天；
    # 复用同一份已核验官方年度计划核对本次行情日，不把 planned 当成 observed。
    _, planned = _scheduled(archive, verified["scheduled"], now)
    if market_day not in planned:
        raise ValueError("--as-of 不在已核验官方交易日计划中")
    return verified


def parse_closing_market_caps(payload: bytes, *, symbols: list[str],
                              market_day: date) -> tuple[dict[str, Decimal], list[dict]]:
    """请求符号、body identity、行情时间和金额全部匹配才接纳。"""
    requested = set(symbols)
    values: dict[str, Decimal] = {}
    reasons = {symbol: {"reason": "missing_response"} for symbol in symbols}
    rejected: list[dict] = []
    seen: set[str] = set()
    for match in _ROW.finditer(payload.decode("gbk", "replace")):
        symbol, body = match.groups()
        fields = body.split("~")
        detail = {"symbol": symbol}
        if len(fields) > 30:
            detail["market_time"] = fields[30]
        if symbol not in requested:
            rejected.append({**detail, "reason": "unexpected_symbol"})
            continue
        iid = symbol[:2].upper() + "." + symbol[2:]
        if symbol in seen:
            values.pop(iid, None)
            reasons[symbol] = {**detail, "reason": "duplicate_response"}
            continue
        seen.add(symbol)
        reason = None
        if len(fields) <= 45:
            reason = "missing_fields"
        elif fields[2].strip() != symbol[2:]:
            reason = "identity_mismatch"
        elif not re.fullmatch(r"[0-9]{14}", fields[30]):
            reason = "invalid_market_time"
        else:
            try:
                stamp = datetime.strptime(fields[30], "%Y%m%d%H%M%S")
            except ValueError:
                reason = "invalid_market_time"
            else:
                if stamp.date() != market_day:
                    reason = "stale_or_wrong_market_day"
                elif stamp.time() < MARKET_CLOSE:
                    reason = "before_market_close"
                else:
                    try:
                        cap = Decimal(fields[45].strip()) * Decimal("100000000")
                    except InvalidOperation:
                        reason = "invalid_market_cap"
                        cap = Decimal(0)
                    if not cap.is_finite() or cap <= 0:
                        reason = "invalid_market_cap"
                    else:
                        # 正式转换还须可换算为有效的正整数分。
                        cents = market_cap_cents_from_values({iid: cap})
                        if iid not in cents:
                            reason = "invalid_market_cap"
                        else:
                            values[iid] = cap
        if reason:
            reasons[symbol] = {**detail, "reason": reason}
        else:
            reasons.pop(symbol, None)
    exclusions = [{"symbol": symbol, "instrument_id": symbol[:2].upper() + "." + symbol[2:],
                   **detail} for symbol, detail in sorted(reasons.items())]
    return values, exclusions + rejected


def fetch_batch(symbols: list[str], archive: ForwardArchive, *, timeout: float = 25.0,
                client: TencentClient | None = None) -> tuple[bytes, str, str]:
    """正式守卫/限速/熔断/原始字节收据链；URL 保留实际 q= 请求。"""
    client = client or TencentClient(archive, timeout=timeout)
    outcome = client.fetch(QUOTE_URL + ",".join(symbols), label=f"market-caps:{len(symbols)}")
    if not outcome.ok or outcome.payload is None or not outcome.content_hash:
        raise ValueError(f"采集批次失败 ({outcome.receipt_id}): {outcome.detail}")
    return outcome.payload, outcome.receipt_id, outcome.content_hash


def collect_closing_caps(archive: ForwardArchive, *, symbols: list[str], market_day: date,
                         batch_size: int, client: TencentClient, report: dict) -> dict[str, Decimal]:
    values: dict[str, Decimal] = {}
    report["batches"] = []
    for start in range(0, len(symbols), batch_size):
        validate_collection_time(market_day, _now())
        batch = symbols[start:start + batch_size]
        payload, receipt_id, digest = fetch_batch(batch, archive, client=client)
        accepted, exclusions = parse_closing_market_caps(payload, symbols=batch, market_day=market_day)
        values.update(accepted)
        report["batches"].append({"batch": len(report["batches"]) + 1,
            "requested_symbols": batch, "requested_count": len(batch),
            "accepted_count": len(accepted), "accepted_ids": sorted(accepted),
            "exclusions": exclusions, "receipt_id": receipt_id, "content_hash": digest})
        print(f"  {min(start + batch_size, len(symbols))}/{len(symbols)} 请求，{len(values)} 接纳", flush=True)
    return values


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", type=Path, default=UNIVERSE)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    parser.add_argument("--output", type=Path, help="默认 data/s2-structured/decision-market-caps-YYYYMMDD.json")
    parser.add_argument("--coverage-output", type=Path, help="默认 sidecar 同路径 .coverage.json")
    parser.add_argument("--calendar", type=Path, help="当天已归档刷新日历证据，实采必需")
    parser.add_argument("--as-of", help="YYYY-MM-DD，默认北京当日；实采必须当日收盘后")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="1..80，默认60")
    parser.add_argument("--min-coverage", type=Decimal, default=MIN_COVERAGE,
                        help="接纳数/支持请求数最低比例，默认0.95；排除明细始终留档")
    parser.add_argument("--dry-run", action="store_true", help="离线范围准备，不采集/发布")
    parser.add_argument("--trusted-proxy-network", action="append", default=[])
    args = parser.parse_args(argv)
    report: dict = {}
    coverage_output: Path | None = None
    try:
        now = _now()
        market_day = date.fromisoformat(args.as_of) if args.as_of else now.astimezone(_MARKET_TZ).date()
        if not 1 <= args.batch_size <= 80:
            raise ValueError("--batch-size 必须为1..80")
        if not args.min_coverage.is_finite() or not 0 < args.min_coverage <= 1:
            raise ValueError("--min-coverage 必须为 (0,1] 正有限数")
        if not args.dry_run:
            validate_collection_time(market_day, now)  # 必须早于任何网络/日历/归档操作
            if args.calendar is None:
                raise ValueError("实采必须提供 --calendar 当天已归档日历证据")
        plan = prepare_universe(args.universe.resolve())
        if not plan["supported_count"]:
            raise ValueError("universe 没有正式支持的沪深股票代码")
        output = args.output or ROOT / "data" / "s2-structured" / f"decision-market-caps-{market_day:%Y%m%d}.json"
        coverage_output = args.coverage_output or output.with_suffix(".coverage.json")
        if coverage_output.resolve() == output.resolve():
            raise ValueError("覆盖报告与sidecar必须使用不同路径")
        report.update({"schema_version": "aquant.market_cap_expansion_coverage.v1",
            "market_day": market_day.isoformat(), "prepared_at": now.isoformat(),
            "plan": plan, "min_coverage": str(args.min_coverage), "output": str(output.resolve()),
            "complete_coverage": False, "published": False})
        if args.dry_run:
            report["dry_run"] = True
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.trusted_proxy_network:
            os.environ["AQUANT_TRUSTED_PROXY_NETWORKS"] = ",".join(args.trusted_proxy_network)
        archive_root = args.archive_root.resolve()
        archive_root.mkdir(parents=True, exist_ok=True)
        with closing(connect(archive_root / "meta.sqlite")) as con:
            archive = ForwardArchive(con, archive_root)
            report["calendar"] = validate_calendar(args.calendar, archive, now=now, market_day=market_day)
            values = collect_closing_caps(archive, symbols=plan["symbols"], market_day=market_day,
                batch_size=args.batch_size, client=TencentClient(archive), report=report)
            coverage = Decimal(len(values)) / Decimal(plan["supported_count"])
            report.update({"accepted_count": len(values), "requested_count": plan["supported_count"],
                "excluded_count": plan["supported_count"] - len(values), "coverage": str(coverage),
                "complete_coverage": len(values) == plan["supported_count"]})
            if not values or coverage < args.min_coverage:
                raise ValueError(f"收盘市值覆盖率 {len(values)}/{plan['supported_count']} 低于阈值 {args.min_coverage}")
            observed_at = _now()
            validate_collection_time(market_day, observed_at)
            manifest = {"schema_version": MANIFEST_SCHEMA, "market_day": market_day.isoformat(),
                "items_hash": market_cap_items_hash(market_cap_cents_from_values(values)),
                "source_id": SOURCE_ID, "requested_count": plan["supported_count"],
                "accepted_count": len(values),
                "coverage_report": {key: value for key, value in report.items() if key != "published"},
                "batches": report["batches"]}
            body = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            digest, _ = archive.store_bytes(body, media_type="application/json")
            receipt = archive.record(source_id=SOURCE_ID, url=f"{QUOTE_URL}#market-cap-batch-manifest",
                outcome="OK", requested_at=observed_at, responded_at=observed_at,
                http_status=200, content_hash=digest, byte_size=len(body),
                detail=f"market-cap batch manifest: {len(report['batches'])} batches")
            sidecar = sidecar_from_market_cap_values(values, market_cap_as_of=market_day,
                observed_at=observed_at, receipt_id=receipt.receipt_id, content_hash=digest, source_id=SOURCE_ID)
            verify_sidecar_archive(sidecar, archive_root)
            _write_json(output, sidecar.as_dict())
            report.update({"published": True, "receipt_id": receipt.receipt_id, "content_hash": digest})
        _write_json(coverage_output, report)
        print(f"{output}: {len(values)}/{plan['supported_count']} 正式支持沪深股票；覆盖报告 {coverage_output}")
        return 0
    except (ValueError, OSError) as exc:
        report["error"] = str(exc)
        if coverage_output is not None and report.get("plan"):
            _write_json(coverage_output, report)
        print(f"收盘市值未发布：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
