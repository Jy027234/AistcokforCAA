"""Rebuild a Luna discovery packet from archived requests/responses, offline."""
from __future__ import annotations
import argparse
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import json
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from aquant.adapters.providers.cninfo_probe import parse_report_period, title_matches_period, title_revision_flags
from aquant.domain.data.db import connect
from aquant.domain.data.forward_archive import ForwardArchive
from tools.replay_s2_bindings import ArchivedCninfoClient


class SharedArchivedCninfoClient(ArchivedCninfoClient):
    """Retain one actual company query, classify its complete rows per report."""
    def __init__(self, archive, discovery):
        super().__init__(archive)
        self.earliest = date.fromisoformat(discovery["startPeriod"])
        self.through = date.fromisoformat(discovery["observedThroughBeijingDate"])
        self.companies = {row["code"]: row for row in discovery["companies"]}
        for row in discovery["companies"]:
            if (row.get("status") == "NOT_CAPTURED_PENDING" and
                    row.get("latestPeriodEnd") is None and
                    row.get("allCategoryComplete") is False):
                # A partial capture keeps the entire configured scope visible;
                # pending identities have no archived lookup to preload.
                continue
            receipt = archive.con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?",
                                          (row.get("orgLookupReceiptId"),)).fetchone()
            if receipt is None or receipt["content_hash"] != row["orgLookupContentHash"]:
                raise ValueError("discovery official organization receipt differs from archive")
            self.responses[(receipt["url"], receipt["detail"])] = receipt
            # Organization identity is one immutable lookup shared by company
            # and regular-report queries; retain its original receipt/detail.
            self.responses[(receipt["url"], f"report-index:org:{row['code']}")] = receipt

    def shared_company_index(self, stock_code, *, max_pages=100):
        return super().cross_category_index(stock_code=stock_code,
            report_period=self.earliest.isoformat(), through=self.through, max_pages=max_pages)

    def report_index(self, *, stock_code, report_period, through=None, **kwargs):
        if through is not None and through != self.through:
            raise ValueError("offline regular-report index cannot claim another observation horizon")
        return super().report_index(stock_code=stock_code, report_period=report_period,
                                    through=min(self.through, parse_report_period(report_period).publication_window[1]), **kwargs)

    def cross_category_index(self, *, stock_code, report_period, through=None, max_pages=100, **kwargs):
        if through is not None and through != self.through:
            raise ValueError("offline company index cannot claim a different observation horizon")
        result = self.shared_company_index(stock_code, max_pages=max_pages)
        target = parse_report_period(report_period)
        if target.end_date < self.earliest:
            raise ValueError("offline shared company index starts after requested report period")
        entries = []
        for entry in result.announcements:
            correction, revision, supplement, _ = title_revision_flags(entry.title)
            reasons = tuple(name for name, active in (
                ("period_title", title_matches_period(entry.title, target)),
                ("correction_title", correction), ("revision_title", revision),
                ("supplement_title", supplement),
                ("accounting_restatement_title", any(term in entry.title for term in ("会计差错", "追溯调整", "重述"))),
            ) if active)
            entries.append(replace(entry, candidate_reasons=reasons))
        return replace(result, report_period=report_period, announcements=tuple(entries))


def replay_discovery(discovery_path: Path, archive_root: Path):
    discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
    if discovery.get("schema") != "aquant.s2-official-index-discovery.v1":
        raise ValueError("unsupported official discovery schema")
    receipts = set()
    output = []
    with closing(connect(archive_root / "meta.sqlite")) as con:
        archive = ForwardArchive(con, archive_root)
        client = SharedArchivedCninfoClient(archive, discovery)
        for company in discovery["companies"]:
            code = company["code"]
            if (company.get("status") == "NOT_CAPTURED_PENDING" and
                    company.get("latestPeriodEnd") is None and
                    company.get("allCategoryComplete") is False):
                continue
            cross = client.shared_company_index(code)
            if (not cross.complete or cross.organization_id != company["organizationId"] or
                    cross.org_lookup_receipt_id != company["orgLookupReceiptId"] or
                    cross.publication_window != tuple(date.fromisoformat(day) for day in company["publicationWindow"]) or
                    cross.termination != company["allCategoryTermination"] or
                    cross.total_announcement != company["allCategoryTotalAnnouncement"] or
                    len(cross.pages) != len(company["allCategoryPages"])):
                raise ValueError(f"{code}: complete company index differs from archived discovery")
            for page, declared in zip(cross.pages, company["allCategoryPages"]):
                actual = {"page": page.page_num, "requestBodyHash": page.request_body_hash,
                    "responseReceiptId": page.receipt_id, "responseContentHash": page.content_hash,
                    "httpStatus": page.http_status, "rawAnnouncementCount": page.raw_announcement_count,
                    "totalAnnouncement": page.total_announcement, "hasMore": page.has_more}
                if "firstSeenAt" in declared:
                    actual["firstSeenAt"] = archive.con.execute(
                        "SELECT first_seen_at FROM fetch_receipt WHERE receipt_id=?", (page.receipt_id,)).fetchone()[0]
                if actual != declared:
                    raise ValueError(f"{code}: declared company page differs from replay")
                receipts.add(page.receipt_id)
            receipts.add(cross.org_lookup_receipt_id)
            # Latest discovery is derived from every actual exhausted company
            # page, rather than trusting a new quarter from the packet header.
            discovered = []
            for entry in cross.announcements:
                match = re.search(r"(20\d{2})年", entry.title)
                if match:
                    for quarter in (1, 2, 3, 4):
                        period = parse_report_period(f"{match[1]}Q{quarter}")
                        if title_matches_period(entry.title, period):
                            discovered.append(period.end_date)
            latest = max(period for period in discovered if period is not None)
            if latest.isoformat() != company["latestPeriodEnd"]:
                raise ValueError(f"{code}: latest report period differs from exhaustive actual titles")
            regular = client.report_index(stock_code=code, report_period=latest.isoformat(), max_pages=100)
            if not regular.complete or len(regular.pages) != len(company["regularReportPages"]):
                raise ValueError(f"{code}: latest regular report index is incomplete")
            for page, declared in zip(regular.pages, company["regularReportPages"]):
                actual = {"page": page.page_num, "requestBodyHash": page.request_body_hash,
                    "responseReceiptId": page.receipt_id, "responseContentHash": page.content_hash,
                    "httpStatus": page.http_status, "rawAnnouncementCount": page.raw_announcement_count,
                    "totalAnnouncement": page.total_announcement, "hasMore": page.has_more}
                if "firstSeenAt" in declared:
                    actual["firstSeenAt"] = archive.con.execute(
                        "SELECT first_seen_at FROM fetch_receipt WHERE receipt_id=?", (page.receipt_id,)).fetchone()[0]
                if actual != declared:
                    raise ValueError(f"{code}: latest regular-report page differs from archived discovery")
                receipts.add(page.receipt_id)
            output.append({"code": code, "latestPeriodEnd": latest.isoformat(),
                "companyIndexPageCount": len(cross.pages), "companyIndexRowCount": len(cross.announcements),
                "latestRegularPageCount": len(regular.pages), "queryWindow": company["publicationWindow"],
                "revisionCandidateCount": sum(any(reason != "period_title" for reason in entry.candidate_reasons)
                                              for entry in cross.candidates), "status": "ARCHIVE_REPLAY_VERIFIED"})
    return {"schema": "aquant.official-financial-index-replay.v1",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "discoveryPath": str(discovery_path.resolve()),
        "discoveryHash": "sha256:" + hashlib.sha256(discovery_path.read_bytes()).hexdigest(),
        "archiveRoot": str(archive_root.resolve()), "companyCount": len(output),
        "discoveryScopeSecurityCount": len(discovery["companies"]),
        "pendingCompanyCount": len(discovery["companies"]) - len(output),
        "verifiedReceiptCount": len(receipts), "providerRequestsStarted": 0,
        "companies": output, "boundary": "Discovery/index evidence only; no PDF value or official S2 fact is implied."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, default=ROOT / "deploy/agentctl-q0/forward-archive")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = replay_discovery(args.discovery, args.archive_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({key: value for key, value in report.items() if key != "companies"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
