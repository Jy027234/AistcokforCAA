"""Local, current-only formula preview from archived CNINFO PDF candidates.

This never constructs FinancialFact, emits ranks, or changes the S2 gate. The
archived reports were first captured after the latest published snapshot, so
their numbers cannot be projected back into that snapshot's PIT research.
"""

from __future__ import annotations

import json
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..adapters.providers.cninfo_financial_pdf import extract_s2_candidate_facts
from ..domain.data.db import connect
from ..domain.data.forward_archive import ForwardArchive


SCHEMA_VERSION = "aquant.s2_candidate_preview.v1"
PREVIEW_STATUS = "CANDIDATE_DIAGNOSTIC_ONLY"
PILOT_STOCKS = ("000333", "600519", "601012")


class S2CandidatePreviewError(ValueError):
    """Local candidate inputs are missing or have lost their evidence binding."""


def _archived_candidate(
    archive: ForwardArchive, report: dict[str, Any], instrument_id: str,
) -> dict[str, Any]:
    receipt = archive.con.execute(
        "SELECT source_id,url,outcome,http_status,content_hash,byte_size,first_seen_at "
        "FROM fetch_receipt WHERE receipt_id=?", (report["archiveReceiptId"],),
    ).fetchone()
    digest = report["contentHash"]
    if (receipt is None or receipt["source_id"] != "cninfo" or
            receipt["url"] != report["documentUrl"] or
            receipt["outcome"] != "OK" or receipt["http_status"] != 200 or
            receipt["content_hash"] != digest or
            receipt["byte_size"] != report["byteSize"] or
            receipt["first_seen_at"] != report["firstSeenAt"] or
            not isinstance(digest, str) or not digest.startswith("sha256:")):
        raise S2CandidatePreviewError(
            f"{instrument_id} {report['periodEnd']}: PDF receipt differs from pilot"
        )
    if not archive.verify(digest):
        raise S2CandidatePreviewError(
            f"{instrument_id} {report['periodEnd']}: archived PDF hash mismatch"
        )
    payload = archive.load_bytes(digest)
    if len(payload) != report["byteSize"]:
        raise S2CandidatePreviewError("archived PDF size mismatch")
    candidate = extract_s2_candidate_facts(
        payload, instrument_id=instrument_id,
        period_end=date.fromisoformat(report["periodEnd"]),
    )
    if (candidate.pit_eligible or
            candidate.review_status != "requires_manual_verification" or
            candidate.pdf_sha256 != digest or
            candidate.document_url != report["documentUrl"] or
            len(candidate.candidates) != 5 or
            set(candidate.by_field) != set(report["candidateFields"])):
        raise S2CandidatePreviewError("PDF extraction differs from pilot")
    for field, item in candidate.by_field.items():
        if item.pdf_page != report["candidateFields"][field]:
            raise S2CandidatePreviewError(f"PDF page mismatch for {field}")
    return {
        "periodEnd": report["periodEnd"],
        "announcementId": report["announcementId"],
        "versionLabel": report["version"],
        "documentUrl": report["documentUrl"],
        "pdfSha256": digest,
        "firstSeenAt": receipt["first_seen_at"],
        "values": {field: item.value_yuan for field, item in candidate.by_field.items()},
    }


def _periods(latest: date) -> tuple[str, ...]:
    if (latest.month, latest.day) != (6, 30):
        raise S2CandidatePreviewError("preview currently supports H1 endpoints only")
    year = latest.year
    return (
        f"{year - 2}-06-30", f"{year - 2}-12-31",
        f"{year - 1}-06-30", f"{year - 1}-12-31",
        latest.isoformat(),
    )


def _choose_reports(reports: list[dict[str, Any]], latest: date) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for report in reports:
        grouped[report["periodEnd"]].append(report)
    expected = _periods(latest)
    if set(grouped) != set(expected):
        raise S2CandidatePreviewError("pilot does not have exactly the five required H1 periods")
    chosen = []
    for period in expected:
        versions = grouped[period]
        if len(versions) == 1:
            chosen.append(versions[0])
            continue
        revised = [item for item in versions if "revised" in item["versionLabel"].lower()]
        if len(revised) != 1:
            raise S2CandidatePreviewError(f"ambiguous report versions for {period}")
        chosen.append(revised[0])
    return chosen


def _factors(reports: list[dict[str, Any]]) -> tuple[dict[str, str | None], str | None, str | None]:
    rows = [report["values"] for report in reports]
    old_h1, old_fy, prior_h1, prior_fy, latest_h1 = rows
    attr_ttm = (prior_fy["net_profit_attributable"] +
                latest_h1["net_profit_attributable"] -
                prior_h1["net_profit_attributable"])
    consolidated_ttm = (prior_fy["net_profit_consolidated"] +
                        latest_h1["net_profit_consolidated"] -
                        prior_h1["net_profit_consolidated"])
    cash_ttm = (prior_fy["operating_cashflow"] +
                latest_h1["operating_cashflow"] -
                prior_h1["operating_cashflow"])
    revenue_ttm = (prior_fy["revenue"] + latest_h1["revenue"] - prior_h1["revenue"])
    prior_revenue_ttm = (old_fy["revenue"] + prior_h1["revenue"] - old_h1["revenue"])
    average_equity = ((prior_h1["parent_equity"] + latest_h1["parent_equity"])
                      / Decimal(2))
    empty = {"F07": None, "F08": None, "F09": None, "F10": None}
    if average_equity <= 0:
        return empty, "S2_NON_POSITIVE_DENOMINATOR", "F07 平均归母权益非正"
    if consolidated_ttm <= 0:
        return empty, "S2_NON_POSITIVE_DENOMINATOR", "F08 合并净利润 TTM 非正"
    if prior_revenue_ttm <= 0:
        return empty, "S2_NON_POSITIVE_DENOMINATOR", "F09 上年同期营业收入 TTM 非正"
    return {
        "F07": str(attr_ttm / average_equity),
        "F08": str(cash_ttm / consolidated_ttm),
        "F09": str(revenue_ttm / prior_revenue_ttm - Decimal(1)),
        "F10": None,
    }, None, None


def build_s2_candidate_preview(
    *, pilot_dir: str | Path, archive_root: str | Path,
) -> dict[str, Any]:
    """Recheck local PDF bytes and compute three-sample formula diagnostics."""
    root = Path(archive_root)
    db = root / "meta.sqlite"
    if not db.is_file():
        raise S2CandidatePreviewError(f"PDF archive database is absent: {db}")
    pilot_root = Path(pilot_dir)
    instruments = []
    with closing(connect(db, read_only=True)) as con:
        archive = ForwardArchive(con, root)
        for stock in PILOT_STOCKS:
            path = pilot_root / f"s2-official-pdf-pilot-{stock}.json"
            if not path.is_file():
                raise S2CandidatePreviewError(f"PDF pilot is absent: {path}")
            pilot = json.loads(path.read_text(encoding="utf-8"))
            if (pilot["sourceId"] != "cninfo" or pilot["instrumentId"] != stock or
                    pilot["formalPitFactCount"] != 0 or
                    pilot["pitEligible"] is not False):
                raise S2CandidatePreviewError(f"unexpected pilot identity or status: {stock}")
            latest = date.fromisoformat(pilot["latestPeriodEnd"])
            reports = []
            for report in pilot["reports"]:
                if not report["candidateOnly"]:
                    raise S2CandidatePreviewError("pilot contains a non-candidate report")
                reports.append(_archived_candidate(archive, report, stock))
            selected = _choose_reports(reports, latest)
            factors, exclusion_code, exclusion_reason = _factors(selected)
            instruments.append({
                "instrumentId": stock,
                "latestPeriodEnd": latest.isoformat(),
                "sourceReports": [
                    {key: value for key, value in report.items() if key != "values"}
                    for report in selected
                ],
                "factors": factors,
                "exclusionCode": exclusion_code,
                "exclusionReason": exclusion_reason,
                "note": (
                    "合并现金流包含金融子公司业务，解读时需注意口径。"
                    if stock == "600519" else None
                ),
            })
    return {
        "schemaVersion": SCHEMA_VERSION,
        "status": PREVIEW_STATUS,
        "source": "cninfo",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "formalPitEligible": False,
        "backtestable": False,
        "rank": None,
        "marketCapStatus": "NOT_USED_PDF_FIRST_SEEN_AFTER_LATEST_PUBLISHED_SNAPSHOT",
        "sampleSize": len(instruments),
        "completeFormulaPreviewCount": sum(
            item["exclusionCode"] is None for item in instruments
        ),
        "instruments": instruments,
    }


def load_s2_candidate_preview(path: str | Path) -> dict[str, Any]:
    """Reject stale or altered status flags before serving a local preview file."""
    file = Path(path)
    if not file.is_file():
        raise S2CandidatePreviewError(f"candidate preview is absent: {file}")
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise S2CandidatePreviewError("candidate preview file is unreadable") from exc
    if (payload.get("schemaVersion") != SCHEMA_VERSION or
            payload.get("status") != PREVIEW_STATUS or
            payload.get("formalPitEligible") is not False or
            payload.get("backtestable") is not False or
            payload.get("rank") is not None or
            payload.get("marketCapStatus") !=
            "NOT_USED_PDF_FIRST_SEEN_AFTER_LATEST_PUBLISHED_SNAPSHOT"):
        raise S2CandidatePreviewError("candidate preview status is invalid")
    if (len(payload.get("instruments", [])) != len(PILOT_STOCKS) or
            {item.get("instrumentId") for item in payload["instruments"]} !=
            set(PILOT_STOCKS) or
            any(item.get("factors", {}).get("F10") is not None
                for item in payload["instruments"])):
        raise S2CandidatePreviewError("candidate preview instrument or F10 status is invalid")
    return payload


__all__ = [
    "SCHEMA_VERSION", "PREVIEW_STATUS", "S2CandidatePreviewError",
    "build_s2_candidate_preview", "load_s2_candidate_preview",
]
