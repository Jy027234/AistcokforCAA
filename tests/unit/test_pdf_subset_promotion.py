"""Fixed-cohort CNINFO PDF admission; evidence fixtures are synthetic."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from aquant.adapters.providers import pdf_promotion as promotion
from aquant.adapters.providers.cninfo import CrossCategoryIndexResult
from aquant.adapters.providers.cninfo_financial_candidate import (
    INDUSTRIAL_PILOT_COMPANIES, REQUIRED_PILOT_FIELDS_BY_PERIOD,
)
from aquant.adapters.providers.cninfo_financial_pdf import (
    CninfoS2CandidateFacts, PdfCandidateFact,
)
from aquant.adapters.providers.cninfo_probe import parse_report_period
from aquant.domain.data.db import connect
from aquant.domain.data.forward_archive import ForwardArchive
from aquant.domain.fundamentals.disclosure_link import DisclosureRole
from aquant.domain.fundamentals.fact_repository import (
    FactReviewBundle, FinancialFactRepository,
)
from aquant.domain.fundamentals.s2_pdf_review_policy import (
    S2_PDF_PILOT_COMPANIES, S2_PDF_REQUIRED_FIELDS,
)
from aquant.domain.research.s2 import required_s2_periods


UTC = timezone.utc
SEEN = datetime(2026, 9, 24, 16, tzinfo=UTC)
REVIEWED = datetime(2026, 9, 25, 16, tzinfo=UTC)
CALENDAR = (date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30))


def _candidate(period: date, announcement: str, *, value_offset: int = 0,
               unit: str = "元", negative: bool = False) -> tuple[CninfoS2CandidateFacts, bytes]:
    payload = f"%PDF-synthetic-{announcement}".encode()
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    rows = []
    for field in sorted(S2_PDF_REQUIRED_FIELDS[period]):
        cell = "(10.00)" if negative and field == "revenue" else f"{10 + value_offset}.00"
        raw = -(Decimal(10)) if cell.startswith("(") else Decimal(10 + value_offset)
        scale = {"元": 1, "千元": 1000, "万元": 10000}[unit]
        cells = (field, "", cell, "9.00")
        rows.append(PdfCandidateFact(
            field=field, value_yuan=raw * scale, period_end=period,
            report_period_text=str(period), statement="profit",
            pdf_page=2, column_header=str(period), amount_unit=unit,
            currency="CNY", source_row="\t".join(cells), source_cells=cells,
            source_current_cell_index=2, source_prior_cell_index=3,
            pdf_sha256=digest, display_row_label=field,
        ))
    return CninfoS2CandidateFacts(
        instrument_id="000651", period_end=period,
        version_label="indexed_full_report_unreviewed",
        document_url=("https://static.cninfo.com.cn/finalpage/2026-09-24/"
                      f"{announcement}.PDF"),
        pdf_sha256=digest, candidates=tuple(rows),
    ), payload


def _record(archive: ForwardArchive, candidate: CninfoS2CandidateFacts,
            payload: bytes, *, seen: datetime) -> str:
    digest, _ = archive.store_bytes(payload)
    return archive.record(
        source_id="cninfo", url=candidate.document_url, outcome="OK",
        requested_at=seen - timedelta(seconds=1), responded_at=seen,
        http_status=200, content_hash=digest, byte_size=len(payload),
    ).receipt_id


def _field_reviews(candidate: CninfoS2CandidateFacts, at: datetime):
    return tuple(promotion.PdfFieldReview(
        field=item.field, reviewed_value_yuan=item.value_yuan,
        pdf_page=item.pdf_page,
        current_cell=item.source_cells[item.source_current_cell_index],
        row_label=item.display_row_label, source_amount_unit=item.amount_unit,
        reviewer_id="real-human-in-synthetic-fixture", reviewed_at=at,
    ) for item in candidate.candidates)


def _version_review(announcement: str, period: date, *,
                    predecessor: str | None = None, at: datetime = REVIEWED):
    return promotion.PdfVersionReview(
        announcement_id=announcement, org_lookup_receipt_id="lookup",
        index_receipt_id="index", search_receipt_ids=("index",),
        search_page_numbers=(1,), query_stock_code="000651",
        query_organization_id="GD165627", query_period_start=period,
        query_period_end=period, query_digest="synthetic",
        reviewer_id="real-human-in-synthetic-fixture", reviewed_at=at,
        complete_search_attested=True,
        predecessor_announcement_id=predecessor,
    )


def _cross(period: date) -> CrossCategoryIndexResult:
    return CrossCategoryIndexResult(
        stock_code="000651", organization_id="GD165627",
        report_period=period.isoformat(),
        publication_window=(period, date(2026, 9, 25)),
        org_lookup_receipt_id="lookup", org_lookup_content_hash="synthetic",
        pages=(), announcements=(), skipped_different_security=0,
        total_announcement=0, complete=True, termination="has_more_false",
        error=None,
    )


def _fixture(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    con = connect(root / "meta.sqlite")
    archive = ForwardArchive(con, root)
    repository = FinancialFactRepository(tmp_path / "facts.sqlite")
    extracted = {}
    extraction_calls = []
    clock = [REVIEWED + timedelta(hours=1)]
    monkeypatch.setattr(promotion, "_utc_now", lambda: clock[0])

    def extract(payload, **kwargs):
        extraction_calls.append(kwargs)
        return extracted[payload]

    monkeypatch.setattr(promotion, "extract_cninfo_candidate_facts", extract)
    monkeypatch.setattr(promotion, "extract_s2_candidate_facts", extract)

    def version_evidence(source, candidate, review, *, now):
        role = (DisclosureRole.REVISED_REPORT if review.predecessor_announcement_id
                else DisclosureRole.ORIGINAL_REPORT)
        receipt = dict(source.con.execute(
            "SELECT * FROM fetch_receipt WHERE url = ? ORDER BY first_seen_at DESC LIMIT 1",
            (candidate.document_url,),
        ).fetchone())
        return date(2026, 9, 24), role, [receipt], []

    def cross_evidence(source, candidate, result, dispositions, **kwargs):
        receipt = dict(source.con.execute(
            "SELECT * FROM fetch_receipt WHERE url = ? ORDER BY first_seen_at DESC LIMIT 1",
            (candidate.document_url,),
        ).fetchone())
        return [receipt], [], []

    # Existing dedicated tests exercise the real official-index evidence paths.
    # These focused tests exercise only the v2 cohort, ledger, and PIT bridge.
    monkeypatch.setattr(promotion, "_version_evidence", version_evidence)
    monkeypatch.setattr(promotion, "_cross_category_evidence", cross_evidence)
    return con, archive, repository, extracted, extraction_calls, clock


def _promote(archive, repository, candidate, receipt_id, *,
             at=REVIEWED, predecessor=None, reviews=None, v1=False):
    fn = (promotion.promote_reviewed_pdf_bundle if v1 else
          promotion.promote_reviewed_cninfo_subset_bundle)
    return fn(
        candidate=candidate, pdf_receipt_id=receipt_id,
        version_review=_version_review(
            candidate.document_url.rsplit("/", 1)[-1].split(".")[0],
            candidate.period_end, predecessor=predecessor, at=at,
        ),
        field_reviews=reviews if reviews is not None else _field_reviews(candidate, at),
        cross_category_index=_cross(candidate.period_end),
        cross_category_dispositions=(),
        cross_category_reviewed_by="real-human-in-synthetic-fixture",
        cross_category_reviewed_at=at,
        archive=archive, repository=repository, trading_calendar=CALENDAR,
    )


def test_subset_policy_matches_candidate_collection_contract():
    assert dict(S2_PDF_PILOT_COMPANIES) == INDUSTRIAL_PILOT_COMPANIES
    assert {period.isoformat(): tuple(sorted(fields))
            for period, fields in S2_PDF_REQUIRED_FIELDS.items()} == {
                period: tuple(sorted(fields))
                for period, fields in REQUIRED_PILOT_FIELDS_BY_PERIOD.items()
            }
    formula_fields: dict[date, set[str]] = {}
    for field, periods in required_s2_periods(date(2026, 6, 30)).items():
        for period in periods:
            formula_fields.setdefault(period, set()).add(field)
    assert {period: frozenset(fields) for period, fields in formula_fields.items()} == dict(
        S2_PDF_REQUIRED_FIELDS
    )


@pytest.mark.parametrize("stock,org", [
    ("000651", "GD165627"), ("688981", "gshk0000981"),
])
def test_promotion_version_lookup_accepts_observed_official_org_id_shapes(
    tmp_path, stock, org,
):
    root = tmp_path / "archive"
    con = connect(root / "meta.sqlite")
    archive = ForwardArchive(con, root)
    try:
        candidate, _ = _candidate(date(2026, 6, 30), "1001")
        candidate = replace(candidate, instrument_id=stock)
        payload = json.dumps([{"code": stock, "orgId": org}]).encode()
        digest, _ = archive.store_bytes(payload)
        lookup = archive.record(
            source_id="cninfo",
            url=("https://www.cninfo.com.cn/new/information/topSearch/query"
                 f"?keyWord={stock}&maxNum=10"),
            outcome="OK", requested_at=SEEN - timedelta(seconds=1),
            responded_at=SEEN, http_status=200,
            content_hash=digest, byte_size=len(payload),
        ).receipt_id
        window = parse_report_period("2026-06-30").publication_window
        review = replace(
            _version_review("1001", date(2026, 6, 30)),
            org_lookup_receipt_id=lookup, index_receipt_id="missing-index",
            search_receipt_ids=("missing-index",), query_stock_code=stock,
            query_organization_id=org, query_period_start=window[0],
            query_period_end=window[1],
            query_digest=promotion.cninfo_query_digest(
                stock_code=stock, organization_id=org,
                period_start=window[0], period_end=window[1],
            ),
        )
        # The missing index is intentional: reaching it proves the real
        # promotion path accepted both the syntax and exact official lookup.
        with pytest.raises(promotion.PdfPromotionError, match="missing archive receipt"):
            promotion._version_evidence(
                archive, candidate, review, now=REVIEWED + timedelta(hours=1),
            )
    finally:
        con.close()


@pytest.mark.parametrize("period,count", [
    (date(2024, 6, 30), 1),
    (date(2025, 12, 31), 4),
    (date(2026, 6, 30), 5),
])
def test_exact_subset_is_atomic_and_accepted_after_future_preopen(
    tmp_path, monkeypatch, period, count,
):
    con, archive, repository, extracted, calls, _ = _fixture(tmp_path, monkeypatch)
    try:
        candidate, payload = _candidate(period, "1001", unit="万元" if count == 1 else "元",
                                        negative=count == 1)
        extracted[payload] = candidate
        receipt = _record(archive, candidate, payload, seen=SEEN)
        facts = _promote(archive, repository, candidate, receipt)
        assert len(facts) == count
        assert calls[0]["company"] == S2_PDF_PILOT_COMPANIES["000651"]
        assert calls[0]["announcement_id"] == "1001"
        assert calls[0]["required_fields"] == tuple(sorted(S2_PDF_REQUIRED_FIELDS[period]))
        store, accepted = repository.load_with_accepted_s2_pdf_reviews()
        assert {fact.version_id for fact in facts} == set(accepted)
        assert all(accepted[fact.version_id][0].startswith("cninfo_pdf_v2_") for fact in facts)
        assert store.select_pit(datetime(2026, 9, 28, 0, 44, tzinfo=UTC)) == []
        assert len(store.select_pit(datetime(2026, 9, 28, 0, 45, tzinfo=UTC))) == count
        if count == 1:
            assert facts[0].value == Decimal("-100000.00")
    finally:
        con.close()


def test_missing_review_and_conflicting_bundle_roll_back_all_subset_facts(
    tmp_path, monkeypatch,
):
    con, archive, repository, extracted, _, _ = _fixture(tmp_path, monkeypatch)
    try:
        candidate, payload = _candidate(date(2025, 12, 31), "1001")
        extracted[payload] = candidate
        receipt = _record(archive, candidate, payload, seen=SEEN)
        with pytest.raises(promotion.PdfPromotionError, match="exact required"):
            _promote(archive, repository, candidate, receipt,
                     reviews=_field_reviews(candidate, REVIEWED)[:-1])
        assert repository.load().facts == ()

        bundle_id = "cninfo_pdf_v2_" + hashlib.sha256(
            f"000651|{candidate.period_end}|1001".encode(),
        ).hexdigest()[:32]
        from aquant.domain.fundamentals.versioned import FinancialFact, StatementScope
        from aquant.domain.data.pit import AvailabilityBasis, PitMode, TimestampPrecision
        unrelated = FinancialFact(
            instrument_id="000651", metric="unrelated", period_end=candidate.period_end,
            statement_scope=StatementScope.CONSOLIDATED, profit_scope=None,
            value=Decimal(1), currency="CNY", raw_unit="yuan",
            source_id="synthetic", source_document_id="other",
            source_published_date=date(2026, 9, 24), source_published_at=None,
            timestamp_precision=TimestampPrecision.DATE,
            first_seen_at=SEEN, ingested_at=REVIEWED,
            available_at=datetime(2026, 9, 28, 0, 45, tzinfo=UTC),
            availability_basis=AvailabilityBasis.OBSERVED,
            pit_mode=PitMode.LIVE_OBSERVED, content_hash="sha256:other",
            version_id="unrelated-fact",
        )
        repository.append_many((unrelated,), review_bundle=FactReviewBundle(
            bundle_id, json.dumps({"reserved": True}, separators=(",", ":")),
        ))
        with pytest.raises(ValueError, match="conflicting financial fact review bundle"):
            _promote(archive, repository, candidate, receipt)
        assert repository.load().facts == (unrelated,)
    finally:
        con.close()


def test_v2_revision_preserves_old_pit_cutoff(
    tmp_path, monkeypatch,
):
    con, archive, repository, extracted, _, clock = _fixture(tmp_path, monkeypatch)
    try:
        period = date(2026, 6, 30)
        original, original_bytes = _candidate(period, "1001")
        extracted[original_bytes] = original
        original_receipt = _record(archive, original, original_bytes, seen=SEEN)
        old = _promote(archive, repository, original, original_receipt)

        revised, revised_bytes = _candidate(period, "1002", value_offset=1)
        extracted[revised_bytes] = revised
        revised_seen = datetime(2026, 9, 28, 16, tzinfo=UTC)
        revised_reviewed = datetime(2026, 9, 28, 17, tzinfo=UTC)
        revised_receipt = _record(archive, revised, revised_bytes, seen=revised_seen)
        clock[0] = revised_reviewed + timedelta(minutes=1)
        new = _promote(archive, repository, revised, revised_receipt,
                       at=revised_reviewed, predecessor="1001")
        assert {fact.supersedes_id for fact in new} == {fact.version_id for fact in old}
        store, accepted = repository.load_with_accepted_s2_pdf_reviews()
        assert len(accepted) == 10
        assert {fact.source_document_id for fact in store.select_pit(
            datetime(2026, 9, 28, 0, 45, tzinfo=UTC))} == {"1001"}
        assert {fact.source_document_id for fact in store.select_pit(
            datetime(2026, 9, 29, 0, 45, tzinfo=UTC))} == {"1002"}
    finally:
        con.close()


def test_reader_rejects_competing_v1_v2_reviews_of_one_announcement(
    tmp_path, monkeypatch,
):
    con, archive, repository, extracted, _, _ = _fixture(tmp_path, monkeypatch)
    try:
        period = date(2024, 6, 30)
        subset, subset_bytes = _candidate(period, "1001")
        extracted[subset_bytes] = subset
        receipt = _record(archive, subset, subset_bytes, seen=SEEN)
        subset_facts = _promote(archive, repository, subset, receipt)
        assert len(subset_facts) == 1
        assert len(repository.load_with_accepted_s2_pdf_reviews()[1]) == 1

        full, _ = _candidate(date(2026, 6, 30), "1001")
        full_bytes = b"%PDF-synthetic-competing-v1-content"
        full_digest = "sha256:" + hashlib.sha256(full_bytes).hexdigest()
        full = replace(
            full, period_end=period, pdf_sha256=full_digest,
            candidates=tuple(replace(item, period_end=period,
                                     pdf_sha256=full_digest)
                             for item in full.candidates),
        )
        extracted[full_bytes] = full
        full_receipt = _record(
            archive, full, full_bytes, seen=SEEN + timedelta(seconds=1),
        )
        other_repository = FinancialFactRepository(tmp_path / "separate-v1.sqlite")
        full_facts = _promote(
            archive, other_repository, full, full_receipt, v1=True,
        )
        assert len(other_repository.load_with_accepted_s2_pdf_reviews()[1]) == 5
        with sqlite3.connect(other_repository.db_path) as db:
            bundle_id, evidence_json = db.execute(
                "SELECT bundle_id,evidence_json FROM financial_fact_review"
            ).fetchone()
        # Repository writes are public to other adapters. Simulate a second
        # writer creating an independently valid proof for the same report.
        mixed = tuple(
            replace(fact, supersedes_id=subset_facts[0].version_id)
            if fact.metric == "revenue" else fact
            for fact in full_facts
        )
        repository.append_many(
            mixed, review_bundle=FactReviewBundle(bundle_id, evidence_json),
        )
        store, accepted = repository.load_with_accepted_s2_pdf_reviews()
        assert len(store.facts) == 6
        assert accepted == {}
    finally:
        con.close()


def test_v2_rejects_v1_predecessor(tmp_path, monkeypatch):
    con, archive, repository, extracted, _, clock = _fixture(tmp_path / "v1", monkeypatch)
    try:
        original, original_bytes = _candidate(date(2026, 6, 30), "1001")
        extracted[original_bytes] = original
        original_receipt = _record(archive, original, original_bytes, seen=SEEN)
        v1 = _promote(archive, repository, original, original_receipt, v1=True)
        assert len(v1) == 5
        revised, revised_bytes = _candidate(date(2026, 6, 30), "1002", value_offset=1)
        extracted[revised_bytes] = revised
        receipt = _record(archive, revised, revised_bytes,
                          seen=datetime(2026, 9, 28, 16, tzinfo=UTC))
        clock[0] = datetime(2026, 9, 28, 17, 1, tzinfo=UTC)
        with pytest.raises(promotion.PdfPromotionError, match="v2 revision lineage"):
            _promote(archive, repository, revised, receipt,
                     at=datetime(2026, 9, 28, 17, tzinfo=UTC), predecessor="1001")
        assert len(repository.load().facts) == 5
    finally:
        con.close()


def test_v2_reader_rejects_review_cell_amount_mismatch(tmp_path, monkeypatch):
    con, archive, repository, extracted, _, _ = _fixture(tmp_path, monkeypatch)
    try:
        candidate, payload = _candidate(date(2024, 6, 30), "1001")
        extracted[payload] = candidate
        receipt = _record(archive, candidate, payload, seen=SEEN)
        facts = _promote(archive, repository, candidate, receipt)
        with sqlite3.connect(repository.db_path) as db:
            bundle_id, evidence_json = db.execute(
                "SELECT bundle_id,evidence_json FROM financial_fact_review"
            ).fetchone()
        forged = json.loads(evidence_json)
        forged["field_reviews"][0]["current_cell"] = "1.00"
        forged_json = json.dumps(forged, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"))
        direct_write = FinancialFactRepository(tmp_path / "direct-write.sqlite")
        direct_write.append_many(
            facts, review_bundle=FactReviewBundle(bundle_id, forged_json),
        )
        assert direct_write.load_with_accepted_s2_pdf_reviews()[1] == {}
    finally:
        con.close()
