#!/usr/bin/env python3
"""把 12×8 新浪候选期与 CNINFO 公告元数据显式关联；不下载 PDF。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.cninfo_probe import (  # noqa: E402
    CninfoReportProbe,
    urllib_resolve_organization_id,
    urllib_transport,
)
from aquant.domain.fundamentals.disclosure_link import (  # noqa: E402
    DisclosureCandidate,
    link_statement_to_disclosures,
)


DEFAULT_COVERAGE = ROOT / "deploy" / "agentctl-q0" / "sina-s2-coverage-12x8.json"
DEFAULT_OUTPUT = ROOT / "deploy" / "agentctl-q0" / "s2-disclosure-links-12x8.json"
DEFAULT_PERIODS = (
    "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30",
    "2025-06-30", "2025-03-31", "2024-12-31", "2024-09-30",
)


def _hashes_for_period(sample: dict, period: str) -> tuple[str, ...]:
    return tuple(sorted({
        str(page["contentHash"])
        for page in sample.get("pages", [])
        if page.get("status") == "PARSED"
        and period in page.get("periodEnds", [])
        and str(page.get("contentHash", "")).startswith("sha256:")
    }))


def _complete_periods(sample: dict) -> set[str]:
    return set(sample.get("completeRequiredPeriodEnds") or [])


def run(*, coverage_path: Path, output_path: Path, periods: Sequence[str],
        pause_seconds: float, timeout_seconds: float) -> dict:
    coverage = json.loads(coverage_path.read_text(encoding="utf-8-sig"))
    samples = coverage.get("samples") or []
    probe = CninfoReportProbe(
        transport=urllib_transport,
        organization_resolver=urllib_resolve_organization_id,
        timeout=timeout_seconds,
        page_size=30,
        max_pages=3,
    )
    records: list[dict] = []
    status_counts: Counter[str] = Counter()
    role_counts: Counter[str] = Counter()
    errors = 0
    matched_announcements = 0
    samples_with_all_periods = 0

    for sample_index, sample in enumerate(samples):
        symbol = str(sample["symbol"])
        sample_records: list[dict] = []
        complete_periods = _complete_periods(sample)
        for period_index, period in enumerate(periods):
            result = probe.query(stock_code=symbol, report_period=period)
            if result.error:
                errors += 1
            matched_announcements += len(result.announcements)
            hashes = _hashes_for_period(sample, period)
            structured_complete = period in complete_periods
            base = {
                "period": period,
                "structuredValuesComplete": structured_complete,
                "query": {
                    "status": "FAILED" if result.error else "OK",
                    "errorType": result.error,
                    "requestCount": result.request_count,
                    "httpStatuses": list(result.http_statuses),
                    "responseBytes": result.response_bytes,
                    "responseHashes": list(result.response_sha256),
                    "matchedAnnouncements": len(result.announcements),
                },
            }
            if not structured_complete or not hashes:
                status = "STRUCTURED_VALUES_UNAVAILABLE"
                base["link"] = {
                    "status": status,
                    "pitEligible": False,
                    "pitBlocker": "required structured values are incomplete for this period",
                    "versions": [],
                }
            else:
                link = link_statement_to_disclosures(
                    stock_code=symbol,
                    period_end=date.fromisoformat(period),
                    value_source_id="sina-financial",
                    value_content_hashes=hashes,
                    announcements=[
                        DisclosureCandidate(
                            announcement_id=item.announcement_id,
                            sec_code=item.sec_code,
                            title=item.title,
                            announced_on=date.fromisoformat(item.announcement_date_cn),
                            document_url=item.url,
                        )
                        for item in result.announcements
                    ],
                )
                status = link.status.value
                versions = [{
                    "announcementId": version.announcement_id,
                    "role": version.role.value,
                    "announcedOn": version.announced_on.isoformat(),
                    "title": version.title,
                    "documentUrl": version.document_url,
                    "candidatePredecessorAnnouncementId": (
                        version.candidate_predecessor_announcement_id
                    ),
                    "supersedesVerified": version.supersedes_verified,
                } for version in link.versions]
                for version in link.versions:
                    role_counts[version.role.value] += 1
                base["link"] = {
                    "status": status,
                    "pitEligible": link.pit_eligible,
                    "pitBlocker": link.pit_blocker,
                    "activeReportAnnouncementId": link.active_report_announcement_id,
                    "valuePageHashes": list(link.value_content_hashes),
                    "versions": versions,
                }
            status_counts[status] += 1
            sample_records.append(base)
            is_last = (sample_index == len(samples) - 1
                       and period_index == len(periods) - 1)
            if pause_seconds > 0 and not is_last:
                time.sleep(pause_seconds)
        if all(record["link"]["status"] == "LINKED_VALUES_UNVERIFIED"
               for record in sample_records):
            samples_with_all_periods += 1
        records.append({
            "symbol": symbol,
            "role": sample.get("role"),
            "market": sample.get("market"),
            "periods": sample_records,
        })

    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "probeMode": "HISTORICAL_RECONSTRUCTED_METADATA_ONLY",
        "sources": ["sina-financial", "cninfo"],
        "rawDocumentsDownloaded": False,
        "financialValuesPersisted": False,
        "samples": records,
        "summary": {
            "requestedSamples": len(samples),
            "requestedPeriodsPerSample": len(periods),
            "queries": len(samples) * len(periods),
            "queryErrors": errors,
            "matchedAnnouncements": matched_announcements,
            "linkStatusCounts": dict(sorted(status_counts.items())),
            "versionRoleCounts": dict(sorted(role_counts.items())),
            "samplesLinkedForAllPeriods": samples_with_all_periods,
            "s2Enabled": False,
            "blockingGates": [
                "STRUCTURED_VALUES_NOT_VERIFIED_AGAINST_ARCHIVED_DISCLOSURE",
                "SUPERSEDES_RELATIONSHIP_NOT_VERIFIED_FROM_DOCUMENTS",
                "HISTORICAL_CAPTURE_IS_RECONSTRUCTED_NOT_FORWARD_OBSERVED",
                "FINANCIAL_INDUSTRY_TEMPLATE_INCOMPLETE",
                "RIGHTS_NOT_REVIEWED",
            ],
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--coverage", type=Path, default=DEFAULT_COVERAGE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--periods", nargs="+", default=list(DEFAULT_PERIODS))
    parser.add_argument("--pause-seconds", type=float, default=0.5)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    args = parser.parse_args()
    if args.pause_seconds < 0:
        parser.error("pause-seconds must be non-negative")
    report = run(
        coverage_path=args.coverage,
        output_path=args.output,
        periods=args.periods,
        pause_seconds=args.pause_seconds,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0 if report["summary"]["queryErrors"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
