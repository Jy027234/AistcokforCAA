from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode

import pytest

from aquant.adapters.providers import pdf_promotion as promotion
from aquant.adapters.providers.cninfo import (
    CrossCategoryIndexEntry, CrossCategoryIndexResult, ReportIndexPage,
)
from aquant.adapters.providers.cninfo_financial_pdf import (
    CninfoS2CandidateFacts, PdfCandidateFact,
)
from aquant.adapters.providers.cninfo_probe import (
    parse_report_period, title_matches_period, title_revision_flags,
)
from aquant.domain.data.db import connect
from aquant.domain.data.forward_archive import ForwardArchive
from aquant.domain.data.rights import Rights, default_rights
from aquant.domain.fundamentals.fact_repository import FinancialFactRepository


UTC = timezone.utc
CALENDAR = (date(2026, 9, 24), date(2026, 9, 28), date(2026, 9, 29))
FIELDS = (
    "net_profit_attributable", "net_profit_consolidated", "operating_cashflow",
    "revenue", "parent_equity",
)


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


def _candidate(announcement_id: str, payload: bytes, *, revised: bool = False,
               source_unit: str = "元"):
    from hashlib import sha256

    digest = "sha256:" + sha256(payload).hexdigest()
    url = f"https://static.cninfo.com.cn/finalpage/2026-09-24/{announcement_id}.PDF"
    rows = []
    for page, field in enumerate(FIELDS, start=30):
        cells = (field, "", f"{page}.00", "10.00")
        rows.append(PdfCandidateFact(
            field=field, value_yuan=Decimal(f"{page}.00") * (
                1000 if source_unit == "千元" else 1),
            period_end=date(2026, 6, 30), report_period_text="2026年半年度",
            statement="profit" if "profit" in field or field == "revenue" else
                      "balance" if field == "parent_equity" else "cashflow",
            pdf_page=page, column_header="2026年半年度", amount_unit=source_unit,
            currency="CNY", source_row="\t".join(cells), source_cells=cells,
            source_current_cell_index=2, source_prior_cell_index=3,
            pdf_sha256=digest,
        ))
    return CninfoS2CandidateFacts(
        instrument_id="600519", period_end=date(2026, 6, 30),
        version_label="revised" if revised else "original",
        document_url=url, pdf_sha256=digest, candidates=tuple(rows),
    )


def _record(archive: ForwardArchive, payload: bytes, *, url: str,
            seen: datetime, detail: str | None = None) -> str:
    digest, _ = archive.store_bytes(payload)
    return archive.record(
        source_id="cninfo", url=url, outcome="OK",
        requested_at=seen - timedelta(seconds=1), responded_at=seen,
        http_status=200, content_hash=digest, byte_size=len(payload), detail=detail,
    ).receipt_id


def _index_payload(*announcements: tuple[str, str, datetime],
                   has_more: bool = False) -> bytes:
    return json.dumps({"hasMore": has_more,
                       "totalAnnouncement": len(announcements), "announcements": [
        {"announcementId": ann_id, "secCode": "600519", "secName": "贵州茅台",
         "announcementTitle": title,
         "announcementTime": int(published.timestamp() * 1000),
         "adjunctUrl": f"/finalpage/2026-09-24/{ann_id}.PDF"}
        for ann_id, title, published in announcements
    ]}, ensure_ascii=False).encode()


def _index_record(archive: ForwardArchive, payload: bytes, *, seen: datetime,
                  page: int = 1, stock: str = "600519",
                  org: str = "gssh0600519", page_size: int = 30,
                  category: str = "category_bndbg_szsh",
                  period_range: str = "2026-06-30~2028-06-29") -> str:
    body = urlencode({
        "pageNum": page, "pageSize": page_size, "column": "sse",
        "tabName": "fulltext", "plate": "", "stock": f"{stock},{org}",
        "searchkey": "", "secid": "", "category": category, "trade": "",
        "seDate": period_range, "sortName": "", "sortType": "",
        "isHLtitle": "true",
    }).encode("utf-8")
    request_hash, _ = archive.store_bytes(body)
    return _record(
        archive, payload,
        url="http://www.cninfo.com.cn/new/hisAnnouncement/query", seen=seen,
        detail=f"report-index:{stock}:2026-06-30:page={page}:request={request_hash}",
    )


def _cross_fixture(
    archive: ForwardArchive, announcements: tuple[tuple[str, str, datetime], ...],
    *, document_receipts: dict[str, str], org_receipt: str,
    seen: datetime, reviewed_at: datetime,
    related_report_ids: dict[str, tuple[str, ...]] | None = None,
    relation_receipt_ids: dict[str, str] | None = None,
    relation_excerpt: str | None = None,
):
    through = reviewed_at.astimezone(timezone(timedelta(hours=8))).date()
    body = urlencode({
        "pageNum": 1, "pageSize": 30, "column": "sse",
        "tabName": "fulltext", "plate": "", "stock": "600519,gssh0600519",
        "searchkey": "", "secid": "", "category": "", "trade": "",
        "seDate": f"2026-06-30~{through.isoformat()}",
        "sortName": "", "sortType": "", "isHLtitle": "true",
    }).encode()
    body_hash, _ = archive.store_bytes(body)
    response = _index_payload(*announcements)
    receipt_id = _record(
        archive, response,
        url="http://www.cninfo.com.cn/new/hisAnnouncement/query",
        seen=seen,
        detail=(f"cross-category:600519:2026-06-30~{through.isoformat()}:"
                f"page=1:request={body_hash}"),
    )
    response_hash = archive.con.execute(
        "SELECT content_hash FROM fetch_receipt WHERE receipt_id = ?", (receipt_id,),
    ).fetchone()["content_hash"]
    period = parse_report_period("2026-06-30")
    entries = []
    dispositions = []
    for announcement_id, title, published in announcements:
        correction, revision, supplement, _ = title_revision_flags(title)
        reasons = tuple(name for name, active in (
            ("period_title", title_matches_period(title, period)),
            ("correction_title", correction), ("revision_title", revision),
            ("supplement_title", supplement),
            ("accounting_restatement_title", any(term in title for term in (
                "会计差错", "追溯调整", "重述"))),
        ) if active)
        entries.append(CrossCategoryIndexEntry(
            announcement_id=announcement_id, sec_code="600519", title=title,
            announcement_time_ms=int(published.timestamp() * 1000),
            document_url=("https://static.cninfo.com.cn/finalpage/2026-09-24/"
                          f"{announcement_id}.PDF"),
            candidate_reasons=reasons, page_num=1,
            page_receipt_id=receipt_id, page_content_hash=response_hash,
        ))
        if reasons:
            needs_relation = any(reason != "period_title" for reason in reasons)
            dispositions.append(promotion.CrossCategoryDisposition(
                announcement_id=announcement_id, relevance="RELATED",
                reviewer_id="human-reviewer-1", reviewed_at=reviewed_at,
                rationale="已核对公告正文与报告期、版本的对应关系",
                document_receipt_id=document_receipts[announcement_id],
                related_report_announcement_ids=(
                    (related_report_ids or {}).get(announcement_id,
                                                     (announcement_id,))),
                relation_receipt_id=(relation_receipt_ids or {}).get(announcement_id)
                if needs_relation else None,
                relation_excerpt=relation_excerpt if needs_relation else None,
            ))
    result = CrossCategoryIndexResult(
        stock_code="600519", organization_id="gssh0600519",
        report_period="2026-06-30",
        publication_window=(date(2026, 6, 30), through),
        org_lookup_receipt_id=org_receipt,
        org_lookup_content_hash=archive.con.execute(
            "SELECT content_hash FROM fetch_receipt WHERE receipt_id = ?",
            (org_receipt,),
        ).fetchone()["content_hash"],
        pages=(ReportIndexPage(
            page_num=1, request_body_hash=body_hash, receipt_id=receipt_id,
            content_hash=response_hash, http_status=200,
            raw_announcement_count=len(announcements),
            total_announcement=len(announcements), has_more=False,
        ),),
        announcements=tuple(entries), skipped_different_security=0,
        total_announcement=len(announcements), complete=True,
        termination="has_more_false", error=None,
    )
    return result, tuple(dispositions)


def _reviews(candidate: CninfoS2CandidateFacts, at: datetime):
    return tuple(promotion.PdfFieldReview(
        field=item.field, reviewed_value_yuan=item.value_yuan,
        pdf_page=item.pdf_page,
        current_cell=item.source_cells[item.source_current_cell_index],
        row_label=item.source_cells[0], source_amount_unit=item.amount_unit,
        reviewer_id="human-reviewer-1",
        reviewed_at=at,
    ) for item in candidate.candidates)


def _setup(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    con = connect(root / "meta.sqlite")
    archive = ForwardArchive(con, root)
    repository = FinancialFactRepository(tmp_path / "facts.sqlite")
    payload = b"%PDF-synthetic-original"
    candidate = _candidate("1001", payload)
    pdf_receipt = _record(archive, payload, url=candidate.document_url,
                          seen=_at(24, 16, 10))
    index_payload = _index_payload(
        ("1001", "2026年半年度报告", _at(24, 12)),
    )
    index_receipt = _index_record(
        archive, index_payload, seen=_at(24, 16, 12),
    )
    lookup_receipt = _record(
        archive, b'[{"code":"600519","orgId":"gssh0600519"}]',
        url=("https://www.cninfo.com.cn/new/information/topSearch/query"
             "?keyWord=600519&maxNum=10"),
        seen=_at(24, 16, 11), detail="report-index:org:600519",
    )
    window = parse_report_period("2026-06-30").publication_window
    review = promotion.PdfVersionReview(
        announcement_id="1001", org_lookup_receipt_id=lookup_receipt,
        index_receipt_id=index_receipt,
        search_receipt_ids=(index_receipt,), search_page_numbers=(1,),
        query_stock_code="600519", query_organization_id="gssh0600519",
        query_period_start=window[0], query_period_end=window[1],
        query_digest=promotion.cninfo_query_digest(
            stock_code="600519", organization_id="gssh0600519",
            period_start=window[0], period_end=window[1],
        ), reviewer_id="human-reviewer-1",
        reviewed_at=_at(25, 16), complete_search_attested=True,
    )
    cross_result, cross_dispositions = _cross_fixture(
        archive, (("1001", "2026年半年度报告", _at(24, 12)),),
        document_receipts={"1001": pdf_receipt}, org_receipt=lookup_receipt,
        seen=_at(25, 16, 5), reviewed_at=_at(25, 16, 30),
    )
    archive._test_cross = (cross_result, cross_dispositions, _at(25, 16, 30))
    monkeypatch.setattr(promotion, "_utc_now", lambda: _at(25, 17))
    monkeypatch.setattr(
        promotion, "extract_s2_candidate_facts",
        lambda raw, **_: candidate if raw == payload else None,
    )
    return con, archive, repository, candidate, pdf_receipt, review


def _promote(archive, repository, candidate, pdf_receipt, review, *,
             field_reviews=None, rights=None, cross_index=None,
             cross_dispositions=None, cross_reviewed_at=None):
    stored_cross, stored_dispositions, stored_reviewed_at = archive._test_cross
    return promotion.promote_reviewed_pdf_bundle(
        candidate=candidate, pdf_receipt_id=pdf_receipt,
        version_review=review,
        field_reviews=field_reviews or _reviews(candidate, _at(25, 16)),
        cross_category_index=(stored_cross if cross_index is None else cross_index),
        cross_category_dispositions=(stored_dispositions if cross_dispositions is None
                                     else cross_dispositions),
        cross_category_reviewed_by="human-reviewer-1",
        cross_category_reviewed_at=(stored_reviewed_at if cross_reviewed_at is None
                                    else cross_reviewed_at),
        archive=archive, repository=repository, trading_calendar=CALENDAR,
        rights=rights,
    )


def test_reviewed_pdfs_wait_until_next_real_trading_preopen_and_persist_evidence(
    tmp_path, monkeypatch,
):
    con, archive, repository, candidate, pdf_receipt, review = _setup(tmp_path, monkeypatch)
    try:
        facts = _promote(archive, repository, candidate, pdf_receipt, review)
        assert len(facts) == 5
        assert {fact.available_at for fact in facts} == {_at(28, 0, 45)}
        assert {fact.source_published_date for fact in facts} == {date(2026, 9, 24)}
        assert {fact.first_seen_at for fact in facts} == {_at(24, 16, 10)}
        assert {fact.content_hash for fact in facts} == {candidate.pdf_sha256}
        assert {fact.source_document_id for fact in facts} == {"1001"}
        restarted = FinancialFactRepository(tmp_path / "facts.sqlite")
        assert restarted.load().select_pit(_at(25, 17)) == []
        assert len(restarted.load().select_pit(_at(28, 0, 45))) == 5
        with sqlite3.connect(tmp_path / "facts.sqlite") as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM financial_fact_review").fetchone()
            evidence = json.loads(row["evidence_json"])
            assert evidence["pdf_receipt_id"] == pdf_receipt
            assert evidence["index_query"]["request_body_verified"] is True
            assert len(evidence["index_request_body_hashes"]) == 1
            assert db.execute("SELECT count(*) FROM financial_fact_review_member").fetchone()[0] == 5
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute("UPDATE financial_fact_review SET evidence_json='{}'")
    finally:
        con.close()


def test_revised_report_adds_five_new_versions_and_preserves_old_cutoff(tmp_path, monkeypatch):
    con, archive, repository, original, receipt, review = _setup(tmp_path, monkeypatch)
    try:
        originals = _promote(archive, repository, original, receipt, review)
        revised_raw = b"%PDF-synthetic-revised"
        revised = _candidate("1002", revised_raw, revised=True)
        revised_receipt = _record(archive, revised_raw, url=revised.document_url,
                                  seen=_at(28, 2))
        index_receipt = _index_record(
            archive,
            _index_payload(
                ("1001", "2026年半年度报告", _at(24, 12)),
                ("1002", "2026年半年度报告（修订版）", _at(28, 0)),
            ),
            seen=_at(28, 2, 5),
        )
        excerpt = "2026年半年度报告更正为修订版"
        correction_receipt = _record(
            archive, b"%PDF-synthetic-correction",
            url="https://static.cninfo.com.cn/finalpage/2026-09-24/1003.PDF",
            seen=_at(28, 2, 7),
        )
        monkeypatch.setattr(promotion, "_pdf_text", lambda _: excerpt)
        monkeypatch.setattr(promotion, "extract_s2_candidate_facts",
                            lambda raw, **_: revised if raw == revised_raw else original)
        monkeypatch.setattr(promotion, "_utc_now", lambda: _at(28, 4))
        revised_review = promotion.PdfVersionReview(
            announcement_id="1002", org_lookup_receipt_id=review.org_lookup_receipt_id,
            index_receipt_id=index_receipt,
            search_receipt_ids=(index_receipt,), search_page_numbers=(1,),
            query_stock_code="600519", query_organization_id="gssh0600519",
            query_period_start=review.query_period_start,
            query_period_end=review.query_period_end,
            query_digest=promotion.cninfo_query_digest(
                stock_code="600519", organization_id="gssh0600519",
                period_start=review.query_period_start,
                period_end=review.query_period_end,
            ), reviewer_id="human-reviewer-1",
            reviewed_at=_at(28, 3), complete_search_attested=True,
            predecessor_announcement_id="1001",
            correction_receipt_id=correction_receipt, correction_excerpt=excerpt,
        )
        cross_result, cross_dispositions = _cross_fixture(
            archive,
            (("1001", "2026年半年度报告", _at(24, 12)),
             ("1002", "2026年半年度报告（修订版）", _at(28, 0)),
             ("1003", "2026年半年度报告更正公告", _at(28, 0))),
            document_receipts={"1001": receipt, "1002": revised_receipt,
                               "1003": correction_receipt},
            org_receipt=review.org_lookup_receipt_id,
            seen=_at(28, 3, 5), reviewed_at=_at(28, 3, 30),
            related_report_ids={"1002": ("1001", "1002"),
                                "1003": ("1001", "1002")},
            relation_receipt_ids={"1002": correction_receipt,
                                  "1003": correction_receipt},
            relation_excerpt=excerpt,
        )
        archive._test_cross = (cross_result, cross_dispositions, _at(28, 3, 30))
        with pytest.raises(promotion.PdfPromotionError, match="every all-category candidate"):
            _promote(
                archive, repository, revised, revised_receipt, revised_review,
                field_reviews=_reviews(revised, _at(28, 3)),
                cross_dispositions=cross_dispositions[:-1],
            )
        unrelated_correction = replace(
            cross_dispositions[-1], relevance="UNRELATED",
            rationale="已核对原文：该更正公告与本次修订报表无关",
            document_receipt_id=None, related_report_announcement_ids=(),
            relation_receipt_id=None, relation_excerpt=None,
        )
        with pytest.raises(promotion.PdfPromotionError,
                           match="correction PDF must be a related"):
            _promote(
                archive, repository, revised, revised_receipt, revised_review,
                field_reviews=_reviews(revised, _at(28, 3)),
                cross_dispositions=(*cross_dispositions[:-1], unrelated_correction),
            )
        assert len(repository.load().facts) == 5  # original bundle remains untouched
        revised_facts = _promote(
            archive, repository, revised, revised_receipt, revised_review,
            field_reviews=_reviews(revised, _at(28, 3)),
        )
        assert {fact.available_at for fact in revised_facts} == {_at(29, 0, 45)}
        assert {fact.supersedes_id for fact in revised_facts} == {
            fact.version_id for fact in originals
        }
        store = repository.load()
        assert {f.source_document_id for f in store.select_pit(_at(28, 0, 45))} == {"1001"}
        assert {f.source_document_id for f in store.select_pit(_at(29, 0, 45))} == {"1002"}
    finally:
        con.close()


def test_unverified_numeric_or_official_identity_never_writes_facts(tmp_path, monkeypatch):
    con, archive, repository, candidate, receipt, review = _setup(tmp_path, monkeypatch)
    try:
        bad_reviews = list(_reviews(candidate, _at(25, 16)))
        bad_reviews[0] = replace(bad_reviews[0], reviewed_value_yuan=Decimal("999"))
        with pytest.raises(promotion.PdfPromotionError, match="reviewed amount"):
            _promote(archive, repository, candidate, receipt, review,
                     field_reviews=bad_reviews)
        with pytest.raises(promotion.PdfPromotionError, match="complete official version search"):
            _promote(archive, repository, candidate, receipt,
                     replace(review, complete_search_attested=False))
        with pytest.raises(promotion.PdfPromotionError, match="official announcement index row"):
            _promote(archive, repository, candidate, receipt,
                     replace(review, announcement_id="wrong"))
        assert repository.load().facts == ()
    finally:
        con.close()


def test_original_cannot_be_admitted_after_another_full_report_version_is_indexed(
    tmp_path, monkeypatch,
):
    con, archive, repository, candidate, receipt, review = _setup(tmp_path, monkeypatch)
    try:
        newer_index = _index_record(
            archive,
            _index_payload(
                ("1001", "2026年半年度报告", _at(24, 12)),
                ("1002", "2026年半年度报告（修订版）", _at(25, 0)),
            ),
            seen=_at(25, 15),
        )
        with pytest.raises(promotion.PdfPromotionError, match="another full version"):
            _promote(archive, repository, candidate, receipt,
                     replace(review, index_receipt_id=newer_index,
                             search_receipt_ids=(newer_index,)))
        assert repository.load().facts == ()
    finally:
        con.close()


def test_archive_hash_and_rights_are_hard_gates(tmp_path, monkeypatch):
    con, archive, repository, candidate, receipt, review = _setup(tmp_path, monkeypatch)
    try:
        denied = default_rights().with_right("cninfo", "local_storage", Rights.UNKNOWN)
        with pytest.raises(promotion.PdfPromotionError, match="rights"):
            _promote(archive, repository, candidate, receipt, review, rights=denied)
        row = con.execute("SELECT stored_path FROM raw_artifact WHERE content_hash = ?",
                          (candidate.pdf_sha256,)).fetchone()
        (archive.root / row["stored_path"]).write_bytes(b"%PDF-tampered")
        with pytest.raises(promotion.PdfPromotionError, match="SHA-256"):
            _promote(archive, repository, candidate, receipt, review)
        assert repository.load().facts == ()
    finally:
        con.close()


def test_review_cannot_predate_pdf_observation_or_index_observation(tmp_path, monkeypatch):
    con, archive, repository, candidate, receipt, review = _setup(tmp_path, monkeypatch)
    try:
        reviews = list(_reviews(candidate, _at(25, 16)))
        reviews[0] = replace(reviews[0], reviewed_at=_at(24, 15))
        with pytest.raises(promotion.PdfPromotionError, match="human review"):
            _promote(archive, repository, candidate, receipt, review,
                     field_reviews=reviews)
        with pytest.raises(promotion.PdfPromotionError, match="predates official index"):
            _promote(archive, repository, candidate, receipt,
                     replace(review, reviewed_at=_at(24, 16, 11)))
        assert repository.load().facts == ()
    finally:
        con.close()


def test_reviewed_thousand_yuan_is_converted_and_raw_unit_is_preserved_in_evidence(
    tmp_path, monkeypatch,
):
    con, archive, repository, candidate, receipt, review = _setup(tmp_path, monkeypatch)
    try:
        thousand = replace(candidate, candidates=tuple(
            replace(item, value_yuan=item.value_yuan * 1000, amount_unit="千元")
            for item in candidate.candidates
        ))
        monkeypatch.setattr(promotion, "extract_s2_candidate_facts",
                            lambda *_args, **_kwargs: thousand)
        facts = _promote(archive, repository, thousand, receipt, review,
                         field_reviews=_reviews(thousand, _at(25, 16)))
        assert {fact.raw_unit for fact in facts} == {"yuan"}
        assert {fact.value for fact in facts} == {
            Decimal("30000.00"), Decimal("31000.00"), Decimal("32000.00"),
            Decimal("33000.00"), Decimal("34000.00"),
        }
        with sqlite3.connect(tmp_path / "facts.sqlite") as db:
            evidence = json.loads(db.execute(
                "SELECT evidence_json FROM financial_fact_review"
            ).fetchone()[0])
        assert {row["source_amount_unit"] for row in evidence["field_reviews"]} == {"千元"}
    finally:
        con.close()


def test_index_last_page_must_explicitly_end_and_page_numbers_are_contiguous(
    tmp_path, monkeypatch,
):
    con, archive, repository, candidate, receipt, review = _setup(tmp_path, monkeypatch)
    try:
        page_with_more = _index_record(
            archive,
            _index_payload(("1001", "2026年半年度报告", _at(24, 12)), has_more=True),
            seen=_at(24, 16, 13),
        )
        with pytest.raises(promotion.PdfPromotionError, match="pagination"):
            _promote(archive, repository, candidate, receipt,
                     replace(review, index_receipt_id=page_with_more,
                             search_receipt_ids=(page_with_more,)))
        with pytest.raises(promotion.PdfPromotionError, match="query digest/pages"):
            _promote(archive, repository, candidate, receipt,
                     replace(review, search_page_numbers=(2,)))
        assert repository.load().facts == ()
    finally:
        con.close()


def test_review_ledger_conflict_rolls_back_all_five_fact_inserts(tmp_path, monkeypatch):
    con, archive, repository, candidate, receipt, review = _setup(tmp_path, monkeypatch)
    try:
        bundle_id = "cninfo_pdf_" + sha256(
            b"600519|2026-06-30|1001"
        ).hexdigest()[:32]
        with sqlite3.connect(tmp_path / "facts.sqlite") as db:
            db.execute(
                "INSERT INTO financial_fact_review "
                "(bundle_id,evidence_hash,evidence_json) VALUES (?,?,?)",
                (bundle_id, "sha256:existing", "{}"),
            )
        with pytest.raises(ValueError, match="conflicting financial fact review bundle"):
            _promote(archive, repository, candidate, receipt, review)
        assert repository.load().facts == ()
    finally:
        con.close()


def test_index_request_body_tampering_and_wrong_scope_block_admission(
    tmp_path, monkeypatch,
):
    con, archive, repository, candidate, receipt, review = _setup(tmp_path, monkeypatch)
    try:
        wrong_scope = _index_record(
            archive,
            _index_payload(("1001", "2026年半年度报告", _at(24, 12))),
            seen=_at(24, 16, 13),
            period_range="2026-07-01~2026-09-30",
        )
        with pytest.raises(promotion.PdfPromotionError, match="POST request .*scope"):
            _promote(archive, repository, candidate, receipt,
                     replace(review, index_receipt_id=wrong_scope,
                             search_receipt_ids=(wrong_scope,)))

        index_row = con.execute(
            "SELECT detail FROM fetch_receipt WHERE receipt_id = ?",
            (review.index_receipt_id,),
        ).fetchone()
        body_hash = index_row["detail"].split("request=", 1)[1]
        artifact_row = con.execute(
            "SELECT stored_path FROM raw_artifact WHERE content_hash = ?", (body_hash,),
        ).fetchone()
        (archive.root / artifact_row["stored_path"]).write_bytes(b"pageNum=0&stock=other")
        with pytest.raises(promotion.PdfPromotionError, match="POST request body fails SHA-256"):
            _promote(archive, repository, candidate, receipt, review)
        assert repository.load().facts == ()
    finally:
        con.close()


def test_all_category_index_and_each_correction_candidate_are_required(
    tmp_path, monkeypatch,
):
    con, archive, repository, candidate, receipt, review = _setup(tmp_path, monkeypatch)
    try:
        with pytest.raises(promotion.PdfPromotionError, match="all-category index"):
            _promote(archive, repository, candidate, receipt, review,
                     cross_index=replace(archive._test_cross[0], complete=False))
        correction_receipt = _record(
            archive, b"%PDF-unrelated-correction",
            url="https://static.cninfo.com.cn/finalpage/2026-09-24/1004.PDF",
            seen=_at(25, 16, 10),
        )
        cross_index, dispositions = _cross_fixture(
            archive,
            (("1001", "2026年半年度报告", _at(24, 12)),
             ("1004", "2024年年度报告更正公告", _at(25, 8))),
            document_receipts={"1001": receipt, "1004": correction_receipt},
            org_receipt=review.org_lookup_receipt_id,
            seen=_at(25, 16, 15), reviewed_at=_at(25, 16, 40),
        )
        with pytest.raises(promotion.PdfPromotionError, match="every all-category candidate"):
            _promote(archive, repository, candidate, receipt, review,
                     cross_index=cross_index,
                     cross_dispositions=(dispositions[0],),
                     cross_reviewed_at=_at(25, 16, 40))
        unrelated = replace(
            dispositions[1], relevance="UNRELATED",
            rationale="已核对标题：该公告仅更正2024年年报，与2026半年报无关",
            document_receipt_id=None, related_report_announcement_ids=(),
            relation_receipt_id=None, relation_excerpt=None,
        )
        facts = _promote(
            archive, repository, candidate, receipt, review,
            cross_index=cross_index,
            cross_dispositions=(dispositions[0], unrelated),
            cross_reviewed_at=_at(25, 16, 40),
        )
        assert len(facts) == 5
        assert {fact.available_at for fact in facts} == {_at(28, 0, 45)}
    finally:
        con.close()


def test_promoted_report_cannot_be_disposed_as_unrelated(tmp_path, monkeypatch):
    con, archive, repository, candidate, receipt, review = _setup(tmp_path, monkeypatch)
    try:
        cross_index, dispositions, reviewed_at = archive._test_cross
        active = replace(
            dispositions[0], relevance="UNRELATED",
            rationale="已核对原文：这份定期报告与待升仓目标无关",
            document_receipt_id=None, related_report_announcement_ids=(),
            relation_receipt_id=None, relation_excerpt=None,
        )
        with pytest.raises(promotion.PdfPromotionError,
                           match="promoted report and predecessor"):
            _promote(archive, repository, candidate, receipt, review,
                     cross_index=cross_index,
                     cross_dispositions=(active, *dispositions[1:]),
                     cross_reviewed_at=reviewed_at)
        assert repository.load().facts == ()
    finally:
        con.close()


def test_related_correction_without_field_impact_proof_blocks_original_bundle(
    tmp_path, monkeypatch,
):
    con, archive, repository, candidate, receipt, review = _setup(tmp_path, monkeypatch)
    try:
        excerpt = "更正本公司二零二六年半年度报告中的财务数据"
        correction_receipt = _record(
            archive, b"%PDF-synthetic-related-correction",
            url="https://static.cninfo.com.cn/finalpage/2026-09-24/1004.PDF",
            seen=_at(25, 16, 10),
        )
        monkeypatch.setattr(promotion, "_pdf_text", lambda _: excerpt)
        cross_index, dispositions = _cross_fixture(
            archive,
            (("1001", "2026年半年度报告", _at(24, 12)),
             ("1004", "2026年半年度报告更正公告", _at(25, 8))),
            document_receipts={"1001": receipt, "1004": correction_receipt},
            org_receipt=review.org_lookup_receipt_id,
            seen=_at(25, 16, 15), reviewed_at=_at(25, 16, 40),
            related_report_ids={"1004": ("1001",)},
            relation_receipt_ids={"1004": correction_receipt},
            relation_excerpt=excerpt,
        )
        with pytest.raises(promotion.PdfPromotionError,
                           match="related amendment without S2 field-impact proof"):
            _promote(archive, repository, candidate, receipt, review,
                     cross_index=cross_index,
                     cross_dispositions=dispositions,
                     cross_reviewed_at=_at(25, 16, 40))
        assert repository.load().facts == ()
    finally:
        con.close()


def test_all_category_post_body_and_pagination_cannot_be_spoofed(tmp_path, monkeypatch):
    con, archive, repository, candidate, receipt, review = _setup(tmp_path, monkeypatch)
    try:
        result, dispositions, reviewed_at = archive._test_cross
        with pytest.raises(promotion.PdfPromotionError, match="metadata differs"):
            _promote(
                archive, repository, candidate, receipt, review,
                cross_index=replace(result, pages=(replace(result.pages[0], has_more=True),)),
                cross_dispositions=dispositions,
                cross_reviewed_at=reviewed_at,
            )
        path_row = con.execute(
            "SELECT stored_path FROM raw_artifact WHERE content_hash = ?",
            (result.pages[0].request_body_hash,),
        ).fetchone()
        (archive.root / path_row["stored_path"]).write_bytes(
            b"pageNum=1&stock=600519%2Cwrong&category="
        )
        with pytest.raises(promotion.PdfPromotionError, match="POST request body fails SHA-256"):
            _promote(archive, repository, candidate, receipt, review)
        assert repository.load().facts == ()
    finally:
        con.close()
