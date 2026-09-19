#!/usr/bin/env python3
"""受限探测 mootdx 财务包的 S2 字段；不保存任何财务数值。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "deploy" / "agentctl-q0" / "mootdx-s2-probe.json"
FIELD_MAP = {
    96: "net_income_attributable_cumulative",
    107: "operating_cashflow_cumulative",
    230: "revenue_single_quarter",
    232: "net_income_attributable_single_quarter",
    234: "operating_cashflow_single_quarter",
    271: "parent_equity",
    314: "report_announcement_date",
}


def _server(value: str) -> tuple[str, int]:
    host, separator, port = value.rpartition(":")
    if not separator or not host:
        raise argparse.ArgumentTypeError("server must be HOST:PORT")
    try:
        parsed_port = int(port)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("server port must be an integer") from exc
    if not 1 <= parsed_port <= 65535:
        raise argparse.ArgumentTypeError("server port is out of range")
    return host, parsed_port


def _package_date(filename: str) -> str | None:
    match = re.fullmatch(r"gpcw(\d{8})\.zip", filename)
    return match.group(1) if match else None


def _dependency_versions() -> dict[str, str]:
    result: dict[str, str] = {}
    for package in ("mootdx", "tdxpy", "pandas"):
        try:
            result[package] = version(package)
        except PackageNotFoundError:
            result[package] = "NOT_INSTALLED"
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--server", required=True, type=_server,
        metavar="HOST:PORT",
        help="explicit public TDX financial server; never written to the report",
    )
    parser.add_argument("--period", help="YYYYMMDD; defaults to latest non-placeholder package")
    parser.add_argument("--symbols", nargs="+", default=["600519", "000001"])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    report: dict = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "sourceId": "mootdx-tdx",
        "probeMode": "LOCAL_LIMITED_SPIKE_EXPLICIT_SERVER",
        "rightsStatus": "UNKNOWN_LOCAL_PROBE_ONLY",
        "dependencyVersions": _dependency_versions(),
    }
    exit_code = 2
    try:
        from mootdx.financial.financial import (  # type: ignore[import-not-found]
            Financial,
            FinancialList,
            FinancialReader,
        )

        listing_client = FinancialList()
        listing_client.bestip = args.server
        listing = listing_client.fetch_and_parse()
        if not listing:
            raise RuntimeError("financial package listing is empty")

        dated = [(item, _package_date(str(item["filename"]))) for item in listing]
        dated = [(item, period) for item, period in dated if period]
        usable = [(item, period) for item, period in dated
                  if int(item["filesize"]) >= 1024]
        if args.period:
            matches = [item for item, period in usable if period == args.period]
            if not matches:
                raise RuntimeError(f"no usable financial package for {args.period}")
            selected = matches[-1]
        else:
            selected = max(usable, key=lambda pair: pair[1])[0]

        with tempfile.TemporaryDirectory(prefix="aquant-mootdx-") as directory:
            downloader = Financial()
            downloader.bestip = args.server
            downloader.fetch_only(
                downdir=directory,
                filename=selected["filename"],
                filesize=int(selected["filesize"]),
            )
            archive = Path(directory) / str(selected["filename"])
            archive_hash = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
            frame = FinancialReader.to_data(str(archive), header="en")

        fields: dict[str, dict[str, int | str]] = {}
        for number, meaning in FIELD_MAP.items():
            column = f"col{number}"
            if column not in frame.columns:
                raise RuntimeError(f"parsed package has no required field {column}")
            series = frame[column]
            fields[str(number)] = {
                "meaning": meaning,
                "zeroCount": int((series == 0).sum()),
                "missingCount": int(series.isna().sum()),
            }

        index_strings = {str(index) for index in frame.index}
        sample_presence = {
            symbol: {"rowPresent": symbol in index_strings}
            for symbol in args.symbols
        }
        periods = sorted(period for _, period in dated)
        report.update({
            "packageListing": {
                "count": len(listing),
                "earliestPeriod": periods[0] if periods else None,
                "latestPeriod": periods[-1] if periods else None,
                "latestUsablePeriod": max(period for _, period in usable),
            },
            "selectedPackage": {
                "filename": selected["filename"],
                "filesize": int(selected["filesize"]),
                "contentHash": archive_hash,
            },
            "parsedShape": {"rows": int(frame.shape[0]), "columns": int(frame.shape[1])},
            "fieldQuality": fields,
            "samples": sample_presence,
            "summary": {
                "status": "PARSED",
                "s2Enabled": False,
                "blockingGates": [
                    "ZERO_MISSINGNESS_AMBIGUOUS_FOR_SINGLE_QUARTER_FIELDS",
                    "REVISION_CHAIN_NOT_IN_SOURCE",
                    "FULL_MARKET_COVERAGE_NOT_VALIDATED",
                    "RIGHTS_NOT_REVIEWED",
                ],
            },
        })
        exit_code = 0
    except Exception as exc:  # noqa: BLE001 - probe must persist the failure class
        report["summary"] = {
            "status": "FAILED",
            "s2Enabled": False,
            "errorType": type(exc).__name__,
            "detail": str(exc),
        }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
