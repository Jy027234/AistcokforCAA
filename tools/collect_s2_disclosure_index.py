"""Archive one CNINFO period's regular and all-category S2 review indexes.

This is a candidate discovery step. It never marks an announcement as related,
reviews a PDF, or writes formal financial facts. Raw responses stay in the
local ForwardArchive; the local index summary is written under ignored data/.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.cninfo import CninfoClient  # noqa: E402
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402

ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"
OUTPUT_ROOT = ROOT / "data" / "s2-pdf-review"
SHANGHAI = timezone(timedelta(hours=8))


def _pages(pages) -> list[dict]:  # noqa: ANN001
    return [{
        "page": page.page_num,
        "requestBodyHash": page.request_body_hash,
        "responseReceiptId": page.receipt_id,
        "responseContentHash": page.content_hash,
        "rawAnnouncementCount": page.raw_announcement_count,
        "totalAnnouncement": page.total_announcement,
        "hasMore": page.has_more,
    } for page in pages]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock", required=True)
    parser.add_argument("--period", required=True, type=date.fromisoformat)
    parser.add_argument("--through", required=True, type=date.fromisoformat)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    parser.add_argument("--trusted-proxy-network", action="append", default=[],
                        help="explicitly trusted local proxy CIDR for this process only")
    args = parser.parse_args()
    if args.through > datetime.now(SHANGHAI).date():
        parser.error("through cannot be a future Beijing date")
    if args.trusted_proxy_network:
        os.environ["AQUANT_TRUSTED_PROXY_NETWORKS"] = ",".join(args.trusted_proxy_network)
    archive_root = args.archive_root.resolve()
    with closing(connect(archive_root / "meta.sqlite")) as con:
        archive = ForwardArchive(con, archive_root)
        client = CninfoClient(archive)
        regular = client.report_index(
            stock_code=args.stock, report_period=args.period.isoformat(),
            max_pages=20,
        )
        cross = client.cross_category_index(
            stock_code=args.stock, report_period=args.period.isoformat(),
            through=args.through, max_pages=100,
        )
    summary = {
        "schema": "cninfo-s2-disclosure-index-v1",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "sourceId": "cninfo",
        "instrumentId": args.stock,
        "reportPeriod": args.period.isoformat(),
        "observedThroughDate": args.through.isoformat(),
        "status": "TITLE_CANDIDATES_ONLY_NOT_PIT_ELIGIBLE",
        "regularIndex": {
            "complete": regular.complete, "termination": regular.termination,
            "error": regular.error,
            "organizationId": regular.organization_id,
            "orgLookupReceiptId": regular.org_lookup_receipt_id,
            "pages": _pages(regular.pages),
            "matchedReportAnnouncements": [{
                "announcementId": match.announcement.announcement_id,
                "title": match.announcement.title,
                "page": match.page_num,
                "pageReceiptId": match.page_receipt_id,
            } for match in regular.matches],
        },
        "allCategoryIndex": {
            "complete": cross.complete, "termination": cross.termination,
            "error": cross.error,
            "organizationId": cross.organization_id,
            "orgLookupReceiptId": cross.org_lookup_receipt_id,
            "totalAnnouncement": cross.total_announcement,
            "pages": _pages(cross.pages),
            "candidateAnnouncements": [{
                "announcementId": item.announcement_id,
                "title": item.title,
                "candidateReasons": list(item.candidate_reasons),
                "documentUrl": item.document_url,
                "page": item.page_num,
                "pageReceiptId": item.page_receipt_id,
            } for item in cross.candidates],
        },
    }
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = OUTPUT_ROOT / f"index-{args.stock}-{args.period}-{stamp}.json"
    with out.open("x", encoding="utf-8") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"{out}: regular={regular.complete}, all-category={cross.complete}, "
          f"pages={len(cross.pages)}, candidates={len(cross.candidates)}")
    if not regular.complete or not cross.complete:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
