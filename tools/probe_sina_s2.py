#!/usr/bin/env python3
"""小规模探测新浪三表对 S2 绝对字段的覆盖；不保存原始财务值。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.sina_financial import (  # noqa: E402
    SOURCE_ID,
    SinaFinancialClient,
    SinaFinancialError,
    merge_s2_pages,
)


DEFAULT_OUTPUT = ROOT / "deploy" / "agentctl-q0" / "sina-s2-probe.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["688489", "600519"])
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = ap.parse_args()

    client = SinaFinancialClient()
    samples: list[dict] = []
    successes = 0
    for symbol in args.symbols:
        try:
            pages = {
                name: client.fetch_page(symbol, name)
                for name in ("profit", "balance", "cashflow")
            }
            rows = merge_s2_pages(
                profit=pages["profit"], balance=pages["balance"],
                cashflow=pages["cashflow"],
            )
            complete = [row for row in rows if row.required_fields_present]
            samples.append({
                "symbol": symbol,
                "status": "PARSED",
                "pageHashes": {name: page.content_hash
                               for name, page in pages.items()},
                "commonPeriods": len(rows),
                "completeRequiredPeriods": len(complete),
                "latestPeriod": rows[0].end_date.isoformat() if rows else None,
                "pitEligible": False,
                "pitBlocker": (rows[0].pit_blocker if rows else
                               "no common report period"),
            })
            successes += 1
        except SinaFinancialError as exc:
            samples.append({
                "symbol": symbol,
                "status": "FAILED",
                "errorType": type(exc).__name__,
                "detail": str(exc),
            })

    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "sourceId": SOURCE_ID,
        "probeMode": "LOCAL_LIMITED_SPIKE",
        "rightsStatus": "UNKNOWN_LOCAL_PROBE_ONLY",
        "samples": samples,
        "summary": {
            "requested": len(args.symbols),
            "parsed": successes,
            "s2Enabled": False,
            "blockingGates": [
                "ANNOUNCEMENT_DATE_NOT_IN_SOURCE",
                "REVISION_CHAIN_NOT_IN_SOURCE",
                "FULL_MARKET_COVERAGE_NOT_VALIDATED",
                "RIGHTS_NOT_REVIEWED",
            ],
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0 if successes else 2


if __name__ == "__main__":
    raise SystemExit(main())
