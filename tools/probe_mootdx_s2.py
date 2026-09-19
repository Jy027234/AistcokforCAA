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
    period_group = parser.add_mutually_exclusive_group()
    period_group.add_argument("--period", metavar="YYYYMMDD",
                              help="one report period (compatibility alias)")
    period_group.add_argument("--periods", nargs="+", metavar="YYYYMMDD")
    period_group.add_argument(
        "--period-count", type=int, default=1,
        help="latest usable report periods to inspect (default: 1)",
    )
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
        requested_periods = args.periods or ([args.period] if args.period else None)
        if requested_periods:
            selected: list[tuple[dict, str]] = []
            for requested in requested_periods:
                matches = [(item, period) for item, period in usable
                           if period == requested]
                if not matches:
                    raise RuntimeError(f"no usable financial package for {requested}")
                selected.append(matches[-1])
        else:
            if args.period_count < 1:
                raise RuntimeError("period-count must be positive")
            selected = sorted(usable, key=lambda pair: pair[1], reverse=True)[
                :args.period_count]

        packages: list[dict] = []
        sample_coverage = {
            symbol: {
                "periodsPresent": 0,
                "periodsWithCumulativeInputs": 0,
                "periodsWithAnnouncementDate": 0,
                "singleQuarterZeroObservations": 0,
                "ambiguousCodePeriods": 0,
            }
            for symbol in args.symbols
        }

        with tempfile.TemporaryDirectory(prefix="aquant-mootdx-") as directory:
            downloader = Financial()
            downloader.bestip = args.server
            for selected_item, selected_period in selected:
                downloader.fetch_only(
                    downdir=directory,
                    filename=selected_item["filename"],
                    filesize=int(selected_item["filesize"]),
                )
                archive = Path(directory) / str(selected_item["filename"])
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
                samples: dict[str, dict[str, bool | int]] = {}
                for symbol in args.symbols:
                    present = symbol in index_strings
                    cumulative = False
                    announcement_date = False
                    zero_observations = 0
                    row_count = 0
                    if present:
                        symbol_rows = frame.loc[[symbol]]
                        row_count = len(symbol_rows)
                        sample_coverage[symbol]["periodsPresent"] += 1
                        if row_count != 1:
                            sample_coverage[symbol]["ambiguousCodePeriods"] += 1
                        else:
                            row = symbol_rows.iloc[0]
                            cumulative = all(
                                not bool(symbol_rows[f"col{number}"].isna().any())
                                and float(row[f"col{number}"]) != 0
                                for number in (96, 107, 271)
                            )
                            announcement_date = (
                                not bool(symbol_rows["col314"].isna().any())
                                and float(row["col314"]) != 0
                            )
                            zero_observations = sum(
                                float(row[f"col{number}"]) == 0
                                for number in (230, 232, 234)
                            )
                            sample_coverage[symbol]["periodsWithCumulativeInputs"] += int(
                                cumulative)
                            sample_coverage[symbol]["periodsWithAnnouncementDate"] += int(
                                announcement_date)
                            sample_coverage[symbol]["singleQuarterZeroObservations"] += int(
                                zero_observations)
                    samples[symbol] = {
                        "rowPresent": present,
                        "rowCount": row_count,
                        "cumulativeInputsPresent": cumulative,
                        "announcementDatePresent": announcement_date,
                        "singleQuarterZeroObservations": zero_observations,
                    }

                packages.append({
                    "period": selected_period,
                    "filename": selected_item["filename"],
                    "filesize": int(selected_item["filesize"]),
                    "contentHash": archive_hash,
                    "parsedShape": {
                        "rows": int(frame.shape[0]),
                        "columns": int(frame.shape[1]),
                    },
                    "fieldQuality": fields,
                    "samples": samples,
                })

        periods = sorted(period for _, period in dated)
        listed_period_counts: dict[str, int] = {}
        for _, period in dated:
            listed_period_counts[period] = listed_period_counts.get(period, 0) + 1
        blocking_gates = [
            "ZERO_MISSINGNESS_AMBIGUOUS_FOR_SINGLE_QUARTER_FIELDS",
            "ONE_PACKAGE_PER_PERIOD_NO_REVISION_CHAIN",
            "FULL_MARKET_COVERAGE_NOT_VALIDATED",
            "RIGHTS_NOT_REVIEWED",
        ]
        if any(item["ambiguousCodePeriods"] for item in sample_coverage.values()):
            blocking_gates.insert(0, "DUPLICATE_CODE_ROWS_WITHOUT_VERSION_IDENTITY")
        report.update({
            "packageListing": {
                "count": len(listing),
                "earliestPeriod": periods[0] if periods else None,
                "latestPeriod": periods[-1] if periods else None,
                "latestUsablePeriod": max(period for _, period in usable),
                "periodsWithMultiplePackages": sorted(
                    period for period, count in listed_period_counts.items() if count > 1
                ),
            },
            "packages": packages,
            "sampleCoverage": sample_coverage,
            "summary": {
                "status": "PARSED",
                "periodsRequested": len(selected),
                "periodsParsed": len(packages),
                "s2Enabled": False,
                "blockingGates": blocking_gates,
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
