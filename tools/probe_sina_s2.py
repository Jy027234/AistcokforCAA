#!/usr/bin/env python3
"""对新浪免费三表做 12 只样本的受限 S2 覆盖探针。

新浪 ``ctrl/part`` 页面只展示最近几期；本探针顺序读取 ``part``、2025 和
2024 页面，将同一报表的期间合并后再检查 S2 必需字段。它只把计数、报告期
元数据、响应哈希和缺失/失败原因写入报告，绝不写入具体财务数值。

这不是生产采集器。公告时间、历史修订链、全市场覆盖和权利状态仍然是阻断
条件；页面请求按固定间隔串行执行，不并发轰炸免费源。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.sina_financial import (  # noqa: E402
    SOURCE_ID,
    SinaFinancialClient,
    SinaFinancialError,
    SinaStatementPage,
    merge_s2_pages,
    parse_statement_page,
)


DEFAULT_OUTPUT = ROOT / "deploy" / "agentctl-q0" / "sina-s2-coverage-12x8.json"
DEFAULT_CONTROLS = ("part", "2025", "2024")
DEFAULT_PAUSE_SECONDS = 1.0
MIN_REPORT_PERIODS = 8
_STATEMENT_PATHS = {
    "profit": "vFD_ProfitStatement",
    "balance": "vFD_BalanceSheet",
    "cashflow": "vFD_CashFlow",
}
_REQUIRED_ATTRIBUTES = (
    "revenue",
    "net_income",
    "net_income_attributable",
    "parent_equity",
    "operating_cashflow",
)

# 8 普通工商 + 2 银行 + 1 保险 + 1 证券；集合同时含沪市和深市。
DEFAULT_SAMPLE_SPECS: tuple[dict[str, str], ...] = (
    {"symbol": "600519", "role": "ordinary_industrial", "market": "SH"},
    {"symbol": "000333", "role": "ordinary_industrial", "market": "SZ"},
    {"symbol": "000651", "role": "ordinary_industrial", "market": "SZ"},
    {"symbol": "600276", "role": "ordinary_industrial", "market": "SH"},
    {"symbol": "601012", "role": "ordinary_industrial", "market": "SH"},
    {"symbol": "002415", "role": "ordinary_industrial", "market": "SZ"},
    {"symbol": "300750", "role": "ordinary_industrial", "market": "SZ"},
    {"symbol": "688981", "role": "ordinary_industrial", "market": "SH"},
    {"symbol": "600036", "role": "bank", "market": "SH"},
    {"symbol": "000001", "role": "bank", "market": "SZ"},
    {"symbol": "601318", "role": "insurance", "market": "SH"},
    {"symbol": "600030", "role": "securities", "market": "SH"},
)
_DEFAULT_SAMPLE_MAP = {item["symbol"]: item for item in DEFAULT_SAMPLE_SPECS}


def _sample_specs(symbols: Sequence[str]) -> list[dict[str, str]]:
    """保留 CLI 顺序；自定义代码只推导市场，不虚构行业分类。"""

    specs: list[dict[str, str]] = []
    for symbol in symbols:
        known = _DEFAULT_SAMPLE_MAP.get(symbol)
        if known is not None:
            specs.append(dict(known))
            continue
        specs.append({
            "symbol": symbol,
            "role": "unspecified",
            "market": "SH" if symbol.startswith(("5", "6", "688")) else "SZ",
        })
    return specs


def _reason_code(message: str) -> str:
    """将异常文本归一为不含响应内容的原因代码。"""

    checks = (
        ("failed to fetch", "fetch_failed"),
        ("unsupported or missing amount unit", "amount_unit_missing_or_unsupported"),
        ("invalid amount", "invalid_amount_format"),
        ("no report-date row", "report_date_row_missing"),
        ("invalid report date", "report_date_invalid"),
        ("no financial rows", "financial_rows_missing"),
        ("missing required row", "required_row_missing"),
        ("cross-instrument", "cross_instrument_merge_forbidden"),
        ("profit/balance/cashflow", "statement_bundle_incomplete"),
    )
    for marker, reason in checks:
        if marker in message:
            return reason
    return "provider_or_parser_error"


def _failure(exc: Exception) -> dict[str, str]:
    return {
        "errorType": type(exc).__name__,
        "reason": _reason_code(str(exc)),
    }


def _fetch_year_page(
    symbol: str,
    statement: str,
    control: str,
    *,
    timeout_seconds: float,
) -> SinaStatementPage:
    """读取固定新浪 HTTPS 页面；``control`` 只允许 part 或四位年份。"""

    if statement not in _STATEMENT_PATHS:
        raise SinaFinancialError(f"unknown statement {statement!r}")
    if control != "part" and (len(control) != 4 or not control.isdigit()):
        raise SinaFinancialError("invalid historical page control")
    url = (
        "https://vip.stock.finance.sina.com.cn/corp/go.php/"
        f"{_STATEMENT_PATHS[statement]}/stockid/{symbol}/ctrl/{control}/displaytype/4.phtml"
    )
    request = Request(url, headers={
        "User-Agent": "AQuant-Lab/0.1 local provider probe",
        "Referer": "https://finance.sina.com.cn/",
    })
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - host fixed above
            payload = response.read()
    except Exception as exc:  # noqa: BLE001 - normalize at report boundary
        raise SinaFinancialError(f"failed to fetch {statement} for {symbol}") from exc
    return parse_statement_page(payload, stock_code=symbol, statement=statement)


def _merge_pages(
    pages: Sequence[SinaStatementPage],
) -> tuple[SinaStatementPage, int]:
    """按报告期合并多张同一报表页面并统计重复期冲突。"""

    if not pages:
        raise SinaFinancialError("no statement pages to merge")
    symbols = {page.stock_code for page in pages}
    statements = {page.statement for page in pages}
    if len(symbols) != 1 or len(statements) != 1:
        raise SinaFinancialError("cross-instrument or cross-statement merge forbidden")

    periods = tuple(sorted({period for page in pages for period in page.periods}, reverse=True))
    by_label: dict[str, dict[date, Decimal | None]] = {}
    conflicts = 0
    for page in pages:
        for label, values in page.rows.items():
            target = by_label.setdefault(label, {})
            for period, value in zip(page.periods, values):
                if period in target and target[period] != value:
                    conflicts += 1
                    continue  # first page is the preferred current view
                target[period] = value

    rows = {
        label: tuple(values.get(period) for period in periods)
        for label, values in by_label.items()
    }
    digest_input = "|".join(page.content_hash for page in pages).encode("utf-8")
    return SinaStatementPage(
        stock_code=pages[0].stock_code,
        statement=pages[0].statement,
        periods=periods,
        rows=rows,
        content_hash="sha256:" + hashlib.sha256(digest_input).hexdigest(),
        retrieved_at=max(page.retrieved_at for page in pages),
    ), conflicts


def _page_summary(
    *,
    statement: str,
    control: str,
    page: SinaStatementPage,
) -> dict[str, object]:
    return {
        "statement": statement,
        "control": control,
        "status": "PARSED",
        "periodCount": len(page.periods),
        "periodEnds": [period.isoformat() for period in page.periods],
        "contentHash": page.content_hash,
    }


def _probe_sample(
    spec: Mapping[str, str],
    *,
    controls: Sequence[str],
    pause_seconds: float,
    timeout_seconds: float,
    request_stats: Counter[str],
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    symbol = spec["symbol"]
    client = SinaFinancialClient(timeout_seconds=timeout_seconds)
    fetched: dict[str, list[SinaStatementPage]] = {name: [] for name in _STATEMENT_PATHS}
    page_records: list[dict[str, object]] = []

    for statement in _STATEMENT_PATHS:
        for control in controls:
            request_stats["attempted"] += 1
            try:
                if control == "part":
                    page = client.fetch_page(symbol, statement)
                else:
                    page = _fetch_year_page(
                        symbol, statement, control, timeout_seconds=timeout_seconds,
                    )
                fetched[statement].append(page)
                request_stats["successful"] += 1
                page_records.append(_page_summary(statement=statement, control=control, page=page))
            except SinaFinancialError as exc:
                request_stats["failed"] += 1
                failure = _failure(exc)
                page_records.append({
                    "statement": statement,
                    "control": control,
                    "status": "FAILED",
                    **failure,
                })
            finally:
                if pause_seconds > 0:
                    sleep_fn(pause_seconds)

    merged: dict[str, SinaStatementPage] = {}
    merge_conflicts: dict[str, int] = {}
    statement_status: dict[str, dict[str, object]] = {}
    for statement, pages in fetched.items():
        if not pages:
            statement_status[statement] = {
                "status": "FAILED",
                "successfulPages": 0,
                "requestedPages": len(controls),
            }
            continue
        try:
            combined, conflicts = _merge_pages(pages)
        except SinaFinancialError as exc:
            statement_status[statement] = {
                "status": "FAILED",
                "successfulPages": len(pages),
                "requestedPages": len(controls),
                **_failure(exc),
            }
            continue
        merged[statement] = combined
        merge_conflicts[statement] = conflicts
        statement_status[statement] = {
            "status": "PARSED",
            "successfulPages": len(pages),
            "requestedPages": len(controls),
            "mergedPeriodCount": len(combined.periods),
            "mergedPeriodEnds": [period.isoformat() for period in combined.periods],
            "duplicatePeriodConflictCount": conflicts,
        }

    result: dict[str, object] = {
        "symbol": symbol,
        "role": spec["role"],
        "market": spec["market"],
        "status": "FAILED",
        "statementStatus": statement_status,
        "pages": page_records,
        "mergeConflicts": merge_conflicts,
    }
    if set(merged) != set(_STATEMENT_PATHS):
        result["errorType"] = "SinaFinancialError"
        result["reason"] = "not_all_statements_available"
        return result

    try:
        rows = merge_s2_pages(
            profit=merged["profit"],
            balance=merged["balance"],
            cashflow=merged["cashflow"],
        )
    except SinaFinancialError as exc:
        result.update({"errorType": type(exc).__name__, **_failure(exc)})
        return result

    complete_count = 0
    missing_by_field: Counter[str] = Counter()
    missing_by_period: list[dict[str, object]] = []
    complete_periods: list[str] = []
    for row in rows:
        missing = [
            name for name in _REQUIRED_ATTRIBUTES
            if getattr(row, name) is None
        ]
        if not missing:
            complete_count += 1
            complete_periods.append(row.end_date.isoformat())
            continue
        missing_by_period.append({
            "period": row.end_date.isoformat(),
            "missingFields": missing,
        })
        missing_by_field.update(missing)

    common_period_count = len(rows)
    result.update({
        "status": "PARSED",
        "commonPeriodCount": common_period_count,
        "commonPeriodEnds": [row.end_date.isoformat() for row in rows],
        "completeRequiredPeriodCount": complete_count,
        "completeRequiredPeriodEnds": complete_periods,
        "missingRequiredFieldCounts": dict(sorted(missing_by_field.items())),
        "missingRequiredPeriods": missing_by_period,
        "minimumPeriodCount": MIN_REPORT_PERIODS,
        "minimumPeriodGate": (
            "MEETS_MINIMUM" if common_period_count >= MIN_REPORT_PERIODS
            else "BELOW_MINIMUM"
        ),
        "pitEligible": False,
        "pitBlockers": [
            "ANNOUNCEMENT_DATE_NOT_IN_SOURCE",
            "REVISION_CHAIN_NOT_IN_SOURCE",
        ],
    })
    return result


def run_probe(
    *,
    symbols: Sequence[str] | None = None,
    controls: Sequence[str] = DEFAULT_CONTROLS,
    pause_seconds: float = DEFAULT_PAUSE_SECONDS,
    timeout_seconds: float = 15.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    specs = _sample_specs(symbols or [item["symbol"] for item in DEFAULT_SAMPLE_SPECS])
    if not specs:
        raise ValueError("symbols must not be empty")
    if not controls:
        raise ValueError("controls must not be empty")
    if pause_seconds < 0:
        raise ValueError("pause_seconds must be non-negative")
    request_stats: Counter[str] = Counter()
    samples = [
        _probe_sample(
            spec,
            controls=controls,
            pause_seconds=pause_seconds,
            timeout_seconds=timeout_seconds,
            request_stats=request_stats,
            sleep_fn=sleep_fn,
        )
        for spec in specs
    ]
    parsed = [sample for sample in samples if sample["status"] == "PARSED"]
    meets_minimum = [
        sample for sample in parsed
        if sample.get("minimumPeriodGate") == "MEETS_MINIMUM"
    ]
    complete = [
        sample for sample in parsed
        if int(sample.get("completeRequiredPeriodCount", 0)) >= MIN_REPORT_PERIODS
    ]
    role_counts = Counter(spec["role"] for spec in specs)
    market_counts = Counter(spec["market"] for spec in specs)
    blocking = [
        "ANNOUNCEMENT_DATE_NOT_IN_SOURCE",
        "REVISION_CHAIN_NOT_IN_SOURCE",
        "FULL_MARKET_COVERAGE_NOT_VALIDATED",
        "RIGHTS_NOT_REVIEWED",
    ]
    if len(meets_minimum) != len(specs):
        blocking.append("REPORT_PERIOD_MINIMUM_UNMET")
    if len(complete) != len(specs):
        blocking.append("REQUIRED_FIELD_COMPLETENESS_UNMET")
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "sourceId": SOURCE_ID,
        "probeMode": "LOCAL_LIMITED_SPIKE_12_SAMPLES_X_8_PERIODS",
        "rightsStatus": "UNKNOWN_LOCAL_PROBE_ONLY",
        "financialValuesPersisted": False,
        "requestPolicy": {
            "transport": "sequential_https",
            "pauseSeconds": pause_seconds,
            "timeoutSeconds": timeout_seconds,
            "controls": list(controls),
            "statements": list(_STATEMENT_PATHS),
        },
        "samples": samples,
        "summary": {
            "requestedSamples": len(specs),
            "parsedSamples": len(parsed),
            "samplesMeetingMinimumPeriods": len(meets_minimum),
            "samplesWithAtLeastEightCompletePeriods": len(complete),
            "minimumReportPeriods": MIN_REPORT_PERIODS,
            "requestedStatementsPerSample": len(_STATEMENT_PATHS),
            "requestedPagesPerStatement": len(controls),
            "requestAttempts": request_stats["attempted"],
            "successfulPageRequests": request_stats["successful"],
            "failedPageRequests": request_stats["failed"],
            "roleCounts": dict(sorted(role_counts.items())),
            "marketCounts": dict(sorted(market_counts.items())),
            "s2Enabled": False,
            "blockingGates": blocking,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--symbols", nargs="+",
        default=[item["symbol"] for item in DEFAULT_SAMPLE_SPECS],
        help="证券代码；默认使用 12 只分行业沪深样本",
    )
    ap.add_argument(
        "--controls", nargs="+", default=list(DEFAULT_CONTROLS),
        help="新浪页面 ctrl 段；默认 part 及 2025、2024 历史页",
    )
    ap.add_argument("--pause-seconds", type=float, default=DEFAULT_PAUSE_SECONDS)
    ap.add_argument("--timeout-seconds", type=float, default=15.0)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = ap.parse_args()

    report = run_probe(
        symbols=args.symbols,
        controls=args.controls,
        pause_seconds=args.pause_seconds,
        timeout_seconds=args.timeout_seconds,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0 if report["summary"]["samplesMeetingMinimumPeriods"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
