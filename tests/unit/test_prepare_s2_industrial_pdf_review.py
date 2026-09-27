"""The industrial PDF worklist preserves unreviewed evidence and index scope."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from aquant.adapters.providers.cninfo_financial_pdf import (
    CninfoS2CandidateFacts,
    PdfCandidateFact,
)
from tools import prepare_s2_industrial_pdf_review as review


STOCK = "000651"
PERIOD = "2024-06-30"
URL = "https://static.cninfo.com.cn/finalpage/2024-08-31/1221092767.PDF"


def _archive_blob(root: Path, payload: bytes) -> str:
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    target = root / "raw" / digest[7:9] / digest[7:]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return digest


def _case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, sqlite3.Connection, Path]:
    root = tmp_path / "archive"
    pdf = b"%PDF-fixture"
    digest = _archive_blob(root, pdf)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE fetch_receipt (receipt_id TEXT, source_id TEXT, url TEXT, "
                "outcome TEXT, http_status INTEGER, content_hash TEXT, byte_size INTEGER, "
                "first_seen_at TEXT, detail TEXT)")
    seen = "2026-09-27T04:18:21+00:00"
    con.execute("INSERT INTO fetch_receipt VALUES (?,?,?,?,?,?,?,?,?)",
                ("pdf-r1", "cninfo", URL, "OK", 200, digest, len(pdf), seen, None))
    cells = ("其中：营业收入", "99,783,116,496.59", "99,236,741,189.50")
    item = PdfCandidateFact(
        field="revenue", value_yuan=Decimal("99783116496.59"),
        period_end=date(2024, 6, 30), report_period_text="2024年半年度",
        statement="profit", pdf_page=75, column_header="2024年半年度",
        amount_unit="元", currency="CNY", source_row="\t".join(cells),
        source_cells=cells, source_current_cell_index=1,
        source_prior_cell_index=2, pdf_sha256=digest,
        display_row_label="其中：营业收入",
    )
    extracted = CninfoS2CandidateFacts(
        instrument_id=STOCK, period_end=date(2024, 6, 30),
        version_label="indexed_full_report_unreviewed",
        document_url=URL, pdf_sha256=digest, candidates=(item,),
    )

    def fake_extract(payload: bytes, **kwargs):  # noqa: ANN003
        assert payload == pdf
        assert kwargs == {
            "instrument_id": STOCK,
            "company": "珠海格力电器股份有限公司",
            "period_end": date(2024, 6, 30),
            "announcement_id": "1221092767",
            "document_url": URL,
            "required_fields": ("revenue",),
        }
        return extracted

    monkeypatch.setattr(review, "extract_cninfo_candidate_facts", fake_extract)
    worksheet = {
        "schema": "cninfo-industrial-s2-candidates-v1",
        "sourceId": "cninfo", "status": "UNREVIEWED_NOT_PIT_ELIGIBLE",
        "pitEligible": False, "formalFactCount": 0,
        "reports": [{
            "instrumentId": STOCK, "company": "珠海格力电器股份有限公司",
            "periodEnd": PERIOD, "status": "UNREVIEWED_PDF_CANDIDATE_NOT_PIT_ELIGIBLE",
            "pitEligible": False, "announcementId": "1221092767",
            "announcedOn": "2024-08-31", "title": "2024年半年度报告",
            "documentUrl": URL, "linkStatus": "LINKED_VALUES_UNVERIFIED",
            "versionCount": 1, "versionCountScope": "linked_period_candidates",
            "archiveReceiptId": "pdf-r1", "contentHash": digest,
            "byteSize": len(pdf), "firstSeenAt": seen,
            "fields": [{
                "field": "revenue", "valueYuan": "99783116496.59",
                "pdfPage": 75, "columnHeader": "2024年半年度", "amountUnit": "元",
                "sourceCurrentCell": cells[1], "sourcePriorCell": cells[2],
                "displayRowLabel": cells[0], "sourceRow": "\t".join(cells),
                "reviewStatus": "PENDING_HUMAN_VISUAL",
            }],
        }],
    }
    return worksheet, con, root


def _build(worksheet: dict, con: sqlite3.Connection, root: Path) -> dict:
    return review.build_review_worklist(
        worksheet, con, root, stocks=(STOCK,), periods=(PERIOD,),
    )


def test_worklist_keeps_each_field_and_announcement_unreviewed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    worksheet, con, root = _case(tmp_path, monkeypatch)
    result = _build(worksheet, con, root)
    assert result["summary"] == {"reports": 1, "fields": 1,
                                 "crossCategoryIndexesSupplied": 0}
    assert result["pitEligible"] is False and result["formalFactCount"] == 0
    report = result["reports"][0]
    assert report["announcementId"] == "1221092767"
    assert report["pdfSha256"] == worksheet["reports"][0]["contentHash"]
    assert report["regularIndex"]["regularIndexEvidenceStatus"] == "LINKED_PERIOD_CANDIDATES_ONLY"
    assert report["regularIndex"]["completeWithinQueryScope"] is None
    assert report["crossCategoryIndexEvidenceStatus"] == "NOT_SUPPLIED_FOR_REPORT_PERIOD"
    assert report["crossCategoryReviewStatus"] == "PENDING_INDEX_AND_HUMAN_DISPOSITIONS"
    assert report["versionChainStatus"] == "NOT_ESTABLISHED"
    assert report["announcementDispositionStatus"] == "PENDING_HUMAN_DISPOSITION"
    assert report["fields"][0]["reviewStatus"] == "PENDING_HUMAN_VISUAL"
    assert report["fields"][0]["extractedSourceRow"].startswith("其中：营业收入")


def test_worklist_rejects_stored_cell_drift_and_missing_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    worksheet, con, root = _case(tmp_path, monkeypatch)
    worksheet["reports"][0]["fields"][0]["sourceCurrentCell"] = "0.00"
    with pytest.raises(ValueError, match="stored extraction differs"):
        _build(worksheet, con, root)
    with pytest.raises(ValueError, match="missing candidate reports"):
        review.build_review_worklist(
            worksheet, con, root, stocks=(STOCK,),
            periods=(PERIOD, "2024-12-31"),
        )


def test_formula_scope_ignores_extra_stored_candidate_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    worksheet, con, root = _case(tmp_path, monkeypatch)
    worksheet["reports"][0]["fields"].append({"field": "parent_equity"})
    result = _build(worksheet, con, root)
    assert result["summary"]["fields"] == 1
    assert [item["field"] for item in result["reports"][0]["fields"]] == ["revenue"]


def test_regular_index_window_comes_from_archived_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    worksheet, con, root = _case(tmp_path, monkeypatch)
    request_hash = _archive_blob(root, b"seDate=2024-06-30~2026-06-30")
    index_payload = b"{\"announcements\":[]}"
    index_hash = _archive_blob(root, index_payload)
    con.execute("INSERT INTO fetch_receipt VALUES (?,?,?,?,?,?,?,?,?)",
                ("index-r1", "cninfo", "https://www.cninfo.com.cn/new/hisAnnouncement/query",
                 "OK", 200, index_hash, len(index_payload),
                 "2026-09-27T04:00:00+00:00", f"request={request_hash}"))
    row = worksheet["reports"][0]
    row.update({
        "versionCountScope": "regular_index_query_window",
        "indexReceiptId": "index-r1", "indexContentHash": index_hash,
        "indexPageNum": 1,
        "indexPublicationWindow": ["2024-06-30", "2026-06-30"],
        "indexCompleteWithinQueryScope": None,
    })
    index = _build(worksheet, con, root)["reports"][0]["regularIndex"]
    assert index["publicationWindow"] == ["2024-06-30", "2026-06-30"]
    assert index["completeWithinQueryScope"] is None
    assert index["regularIndexEvidenceStatus"] == "ARCHIVED_PAGE_AND_QUERY_VERIFIED"
    row["indexPublicationWindow"] = ["2024-06-30", "2024-09-30"]
    with pytest.raises(ValueError, match="query window differs"):
        _build(worksheet, con, root)


def test_complete_cross_index_keeps_titles_pending_and_refresh_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    worksheet, con, root = _case(tmp_path, monkeypatch)
    page_row = {
        "secCode": STOCK, "announcementId": "1221092767",
        "announcementTitle": "2024年半年度报告",
        "adjunctUrl": "finalpage/2024-08-31/1221092767.PDF",
    }
    index = {
        "schema": "cninfo-s2-disclosure-index-v1", "sourceId": "cninfo",
        "instrumentId": STOCK, "reportPeriod": PERIOD,
        "status": "TITLE_CANDIDATES_ONLY_NOT_PIT_ELIGIBLE",
        "observedThroughDate": "2026-09-27",
    }
    for name, category, window in (
        ("regularIndex", "category_bndbg_szsh", "2024-06-30~2028-06-29"),
        ("allCategoryIndex", "", "2024-06-30~2026-09-27"),
    ):
        body = (f"stock={STOCK}%2Cgssz0000651&pageNum=1&category={category}"
                f"&seDate={window}").encode()
        body_hash = _archive_blob(root, body)
        response = json.dumps({"announcements": [page_row],
                               "totalAnnouncement": 1, "hasMore": False}).encode()
        response_hash = _archive_blob(root, response)
        receipt_id = f"{name}-r1"
        con.execute("INSERT INTO fetch_receipt VALUES (?,?,?,?,?,?,?,?,?)",
                    (receipt_id, "cninfo", "https://www.cninfo.com.cn/new/hisAnnouncement/query",
                     "OK", 200, response_hash, len(response),
                     "2026-09-27T04:00:00+00:00", f"request={body_hash}"))
        index[name] = {
            "complete": True, "organizationId": "gssz0000651",
            "pages": [{"page": 1, "requestBodyHash": body_hash,
                       "responseReceiptId": receipt_id,
                       "responseContentHash": response_hash,
                       "rawAnnouncementCount": 1, "totalAnnouncement": 1,
                       "hasMore": False}],
        }
    index["regularIndex"]["matchedReportAnnouncements"] = [{
        "announcementId": "1221092767", "title": "2024年半年度报告",
        "page": 1, "pageReceiptId": "regularIndex-r1",
    }]
    index["allCategoryIndex"]["totalAnnouncement"] = 1
    index["allCategoryIndex"]["candidateAnnouncements"] = [{
        "announcementId": "1221092767", "title": "2024年半年度报告",
        "documentUrl": URL, "candidateReasons": ["period_title"],
        "page": 1, "pageReceiptId": "allCategoryIndex-r1",
    }]
    result = review.build_review_worklist(
        worksheet, con, root, stocks=(STOCK,), periods=(PERIOD,),
        cross_indexes=(index,),
    )
    report = result["reports"][0]
    assert report["crossCategoryIndexEvidenceStatus"] == "ARCHIVED_QUERY_SCOPE_VERIFIED"
    assert report["crossCategoryIndex"]["observedThroughBeijingDate"] == "2026-09-27"
    assert report["crossCategoryIndex"]["mustRefreshIfHumanReviewOccursOnLaterBeijingDate"]
    assert report["crossCategoryIndex"]["candidateAnnouncements"][0]["reviewStatus"] == \
        "PENDING_HUMAN_DISPOSITION"
    assert report["versionChainStatus"] == "NOT_ESTABLISHED"
