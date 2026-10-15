from __future__ import annotations

import json
import hashlib
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.data.pit import AvailabilityBasis, PitMode, TimestampPrecision
from aquant.domain.data.snapshot import (
    DataMode, SnapshotDraft, SnapshotStore, write_dataset,
)
from aquant.domain.fundamentals.fact_repository import (
    FactReviewBundle, FinancialFactRepository,
)
from aquant.domain.fundamentals.versioned import (
    FinancialFact, ProfitScope, StatementScope,
)
from aquant.operations.s2_readiness import S2ReadinessError, build_s2_readiness_report
from tools.report_s2_readiness import main as cli_main


CUTOFF = datetime(2026, 9, 24, 7, tzinfo=timezone.utc)
SEEN = datetime(2026, 1, 2, 8, tzinfo=timezone.utc)


def _facts(iid: str, *, seen: datetime = SEEN) -> list[FinancialFact]:
    rows = [
        ("net_profit_attributable", date(2024, 12, 31), ProfitScope.ATTRIBUTABLE, "80"),
        ("net_profit_attributable", date(2025, 12, 31), ProfitScope.ATTRIBUTABLE, "100"),
        ("net_profit_consolidated", date(2025, 12, 31), ProfitScope.CONSOLIDATED, "120"),
        ("operating_cashflow", date(2025, 12, 31), None, "90"),
        ("revenue", date(2024, 12, 31), None, "400"),
        ("revenue", date(2025, 12, 31), None, "500"),
        ("parent_equity", date(2024, 12, 31), None, "400"),
        ("parent_equity", date(2025, 12, 31), None, "500"),
    ]
    return [
        FinancialFact(
            instrument_id=iid, metric=metric, period_end=period,
            statement_scope=StatementScope.CONSOLIDATED, profit_scope=scope,
            value=Decimal(value), currency="CNY", raw_unit="yuan",
            source_id="official-pdf",
            source_document_id=f"{iid}-report-{period.isoformat()}",
            source_published_date=date(2026, 1, 1),
            source_published_at=datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
            timestamp_precision=TimestampPrecision.MINUTE,
            first_seen_at=seen, ingested_at=seen, available_at=SEEN,
            availability_basis=AvailabilityBasis.OBSERVED,
            pit_mode=(PitMode.LIVE_OBSERVED if seen == SEEN
                      else PitMode.HISTORICAL_RECONSTRUCTED),
            content_hash=f"sha256:{iid}-{period.isoformat()}",
            version_id=f"{iid}-{metric}-{period.isoformat()}",
        )
        for metric, period, scope, value in rows
    ]


def _reviewed_bundles(iid: str) -> list[tuple[list[FinancialFact], FactReviewBundle]]:
    """Synthetic test fixture shaped like PDF promotion output, never production evidence."""

    values = {
        2024: {"net_profit_attributable": "80", "net_profit_consolidated": "90",
               "operating_cashflow": "60", "revenue": "400", "parent_equity": "400"},
        2025: {"net_profit_attributable": "100", "net_profit_consolidated": "120",
               "operating_cashflow": "90", "revenue": "500", "parent_equity": "500"},
    }
    bundles = []
    for year, by_field in values.items():
        period = date(year, 12, 31)
        announcement = f"test-announcement-{year}"
        content_hash = "sha256:" + hashlib.sha256(
            f"{iid}|{year}".encode()).hexdigest()
        facts = []
        for field, value in sorted(by_field.items()):
            identity = f"cninfo|{iid}|{period}|{field}|{announcement}|{content_hash}"
            facts.append(FinancialFact(
                instrument_id=iid, metric=field, period_end=period,
                statement_scope=StatementScope.CONSOLIDATED,
                profit_scope=(ProfitScope.ATTRIBUTABLE if field == "net_profit_attributable"
                              else ProfitScope.CONSOLIDATED
                              if field == "net_profit_consolidated" else None),
                value=Decimal(value), currency="CNY", raw_unit="yuan",
                source_id="cninfo", source_document_id=announcement,
                source_published_date=date(2026, 1, 1), source_published_at=None,
                timestamp_precision=TimestampPrecision.DATE,
                first_seen_at=SEEN, ingested_at=SEEN, available_at=SEEN,
                availability_basis=AvailabilityBasis.OBSERVED,
                pit_mode=PitMode.LIVE_OBSERVED, content_hash=content_hash,
                version_id="pdf_" + hashlib.sha256(identity.encode()).hexdigest()[:32],
            ))
        evidence = {
            "schema": "cninfo-s2-pdf-review-v1", "source_id": "cninfo",
            "rights_register_version": "test", "instrument_id": iid,
            "period_end": period.isoformat(), "announcement_id": announcement,
            "announcement_role": "ORIGINAL_REPORT", "content_hash": content_hash,
            "source_published_date": "2026-01-01", "pdf_receipt_id": "test-pdf-receipt",
            "org_lookup_receipt_id": "test-org-receipt",
            "index_query": {"request_body_verified": True},
            "complete_search_attested": True,
            "version_reviewer_id": "synthetic-test-reviewer",
            "version_reviewed_at": SEEN.isoformat(),
            "cross_category_index": {
                "reviewer_id": "synthetic-test-reviewer",
                "reviewed_at": SEEN.isoformat(), "candidate_dispositions": [],
            },
            "first_seen_at": SEEN.isoformat(), "ingested_at": SEEN.isoformat(),
            "available_at": SEEN.isoformat(),
            "version_ids": [fact.version_id for fact in facts],
            "field_reviews": [
                {"field": fact.metric, "value_yuan": str(fact.value),
                 "pdf_page": 1, "current_cell": str(fact.value),
                 "row_label": fact.metric, "source_amount_unit": "元",
                 "reviewer_id": "synthetic-test-reviewer",
                 "reviewed_at": SEEN.isoformat(), "method": "HUMAN_VISUAL"}
                for fact in facts
            ],
        }
        bundle_id = "cninfo_pdf_" + hashlib.sha256(
            f"{iid}|{period}|{announcement}".encode()).hexdigest()[:32]
        bundles.append((facts, FactReviewBundle(
            bundle_id, json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")))))
    return bundles


def _snapshot(tmp_path, instruments: list[dict], *, data_mode=DataMode.SYNTHETIC,
              trading_day="2026-09-24", cutoff=CUTOFF) -> tuple[object, str]:
    root = tmp_path / "snapshot"
    api = root / "api"
    sid = "snap-s2-test"
    con = connect(root / "meta.sqlite")
    try:
        apply_migrations(con)
        refs = []
        for name, payload in (
            ("instruments", instruments),
            ("trading_calendar", [trading_day]),
            ("daily_quotes", [
                {"instrument_id": row["instrument_id"], "trading_day": trading_day,
                 "open_cents": 100, "high_cents": 100, "low_cents": 100,
                 "close_cents": 100, "volume_shares": 100}
                for row in instruments
            ]),
        ):
            refs.append(write_dataset(
                api, f"datasets/{sid}/{name}.json",
                json.dumps(payload).encode("utf-8"),
            ))
        refs = [replace(ref, as_of_upper_bound=cutoff) for ref in refs]
        SnapshotStore(con, api).publish(SnapshotDraft(
            snapshot_id=sid, kind="EOD", data_mode=data_mode,
            input_cutoff_at=cutoff, as_of_time=cutoff,
            created_at=cutoff, code_version="test", data_version="test",
            watermark="TEST", datasets=refs,
        ))
    finally:
        con.close()
    return root, sid


def _instrument(iid: str, industry: str, *, cents: int | None = 100_000) -> dict:
    row = {"instrument_id": iid, "industry_code": industry}
    if cents is not None:
        row.update(market_cap_cents=cents, market_cap_as_of="2026-09-24")
    return row


def test_unaudited_facts_never_count_as_complete_and_future_facts_do_not_backfill(tmp_path):
    good = "SH.600000"
    future = "SH.600002"
    root, sid = _snapshot(tmp_path, [
        _instrument(good, "C39"),
        _instrument("SH.600001", "J66"),
        _instrument(future, "C39"),
        _instrument("SH.600003", "C39", cents=None),
    ])
    facts_db = tmp_path / "facts.sqlite"
    FinancialFactRepository(facts_db).append_many(
        _facts(good) + _facts(future, seen=datetime(2026, 10, 1, tzinfo=timezone.utc))
    )

    report = build_s2_readiness_report(
        snapshot_dir=root, snapshot_id=sid, facts_db=facts_db)

    assert report["pool_size"] == 4
    assert report["complete_s2_count"] == 0
    assert report["coverage_ratio"] == 0
    assert report["strategy_family_status"] == "CLOSED"
    assert report["registration_gate"] == "SEPARATE_ACCEPTANCE_REQUIRED"
    assert report["missing_market_cap_count"] == 1
    assert report["fact_versions_in_pool"] == 16
    assert report["fact_versions_first_seen_after_cutoff"] == 8
    assert report["fact_versions_without_accepted_review"] == 8
    assert report["engine_reported_fact_versions"] == 0
    assert report["source_documents_used"] == 0
    assert report["content_hashes_used"] == 0
    assert report["accepted_review_bundles_used"] == 0
    assert report["exclusion_counts"]["S2_REVIEW_MISSING"] == 1
    assert report["exclusion_counts"]["S2_INDUSTRY_TEMPLATE_UNSUPPORTED"] == 1
    assert report["exclusion_counts"]["S2_FINANCIAL_FACT_MISSING"] == 2
    assert report["evidence"] == []
    assert "signal_rank" not in json.dumps(report)


def test_consistent_reviewed_bundles_count_and_expose_only_proof_refs(tmp_path):
    iid = "SH.600000"
    root, sid = _snapshot(tmp_path, [_instrument(iid, "C39")])
    facts_db = tmp_path / "facts.sqlite"
    repository = FinancialFactRepository(facts_db)
    bundles = _reviewed_bundles(iid)
    for facts, review in bundles:
        repository.append_many(facts, review_bundle=review)

    report = build_s2_readiness_report(
        snapshot_dir=root, snapshot_id=sid, facts_db=facts_db)

    assert report["complete_s2_count"] == 1
    assert report["coverage_ratio"] == 1
    assert report["exclusion_counts"] == {}
    assert report["fact_versions_without_accepted_review"] == 0
    assert report["accepted_review_bundles_used"] == 2
    assert report["engine_reported_fact_versions"] == 8
    assert {item["bundle_id"] for item in report["evidence"][0]["review_bundles"]} == {
        review.bundle_id for _, review in bundles}
    assert "signal_rank" not in json.dumps(report)


def test_unreviewed_successors_do_not_resurrect_reviewed_originals(tmp_path):
    iid = "SH.600000"
    root, sid = _snapshot(tmp_path, [_instrument(iid, "C39")])
    repository = FinancialFactRepository(tmp_path / "facts.sqlite")
    bundles = _reviewed_bundles(iid)
    for facts, review in bundles:
        repository.append_many(facts, review_bundle=review)
    latest_facts = bundles[1][0]
    later = SEEN + timedelta(days=1)
    revisions = [replace(
        fact, source_document_id="unreviewed-revision",
        content_hash="sha256:unreviewed-revision",
        version_id=f"unreviewed-{fact.metric}",
        first_seen_at=later, ingested_at=later, available_at=later,
        supersedes_id=fact.version_id,
    ) for fact in latest_facts]
    repository.append_many(revisions)

    report = build_s2_readiness_report(
        snapshot_dir=root, snapshot_id=sid, facts_db=repository.db_path)

    assert report["complete_s2_count"] == 0
    assert report["exclusion_counts"] == {"S2_REVIEW_MISSING": 1}
    assert report["engine_reported_fact_versions"] == 0
    assert report["fact_versions_without_accepted_review"] == 5


def test_mismatched_review_members_do_not_count(tmp_path):
    iid = "SH.600000"
    root, sid = _snapshot(tmp_path, [_instrument(iid, "C39")])
    repository = FinancialFactRepository(tmp_path / "facts.sqlite")
    bundles = _reviewed_bundles(iid)
    for facts, review in bundles:
        if facts is bundles[1][0]:
            evidence = json.loads(review.evidence_json)
            evidence["version_ids"][0] = "not-a-member"
            review = FactReviewBundle(
                review.bundle_id,
                json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")))
        repository.append_many(facts, review_bundle=review)

    report = build_s2_readiness_report(
        snapshot_dir=root, snapshot_id=sid, facts_db=repository.db_path)

    assert report["complete_s2_count"] == 0
    assert report["exclusion_counts"] == {"S2_REVIEW_MISSING": 1}


def test_real_pdf_promotion_evidence_is_recognized(tmp_path, monkeypatch):
    from tests.unit.test_pdf_promotion import _promote, _setup

    con, archive, repository, candidate, receipt, review = _setup(
        tmp_path, monkeypatch)
    try:
        facts = _promote(archive, repository, candidate, receipt, review)
        store, accepted = FinancialFactRepository.open_existing(
            repository.db_path).load_with_accepted_s2_pdf_reviews()
        assert len(store.facts) == 5
        assert {fact.version_id for fact in facts} == set(accepted)
    finally:
        con.close()


def test_review_links_require_append_only_schema(tmp_path):
    path = tmp_path / "facts.sqlite"
    FinancialFactRepository(path)
    with sqlite3.connect(path) as con:
        con.execute("DROP TRIGGER financial_fact_review_member_no_update")
    repository = FinancialFactRepository.open_existing(path)
    with pytest.raises(ValueError, match="append-only triggers"):
        repository.load_with_accepted_s2_pdf_reviews()


def test_missing_fact_file_and_inconsistent_frozen_cap_fail_closed(tmp_path, capsys):
    root, sid = _snapshot(tmp_path, [_instrument("SH.600000", "C39")])
    missing = tmp_path / "missing-facts.sqlite"
    assert cli_main(["--snapshot-dir", str(root), "--snapshot-id", sid,
                     "--facts-db", str(missing)]) == 2
    assert "does not exist" in capsys.readouterr().err
    assert not missing.exists()

    facts_db = tmp_path / "facts.sqlite"
    FinancialFactRepository(facts_db).append_many(_facts("SH.600000"))
    root_bad, sid_bad = _snapshot(
        tmp_path / "bad", [{**_instrument("SH.600000", "C39"),
                            "market_cap_as_of": "2026-09-23"}])
    with pytest.raises(S2ReadinessError, match="differs from frozen trading day"):
        build_s2_readiness_report(
            snapshot_dir=root_bad, snapshot_id=sid_bad, facts_db=facts_db)


def test_existing_fact_repository_opener_is_read_only_and_validates_schema(tmp_path):
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(FileNotFoundError):
        FinancialFactRepository.open_existing(missing)
    assert not missing.exists()

    invalid = tmp_path / "invalid.sqlite"
    invalid.write_bytes(b"not a sqlite database")
    with pytest.raises(ValueError, match="invalid financial fact repository schema"):
        FinancialFactRepository.open_existing(invalid)

    path = tmp_path / "facts.sqlite"
    FinancialFactRepository(path).append_many(_facts("SH.600000"))
    repository = FinancialFactRepository.open_existing(path)
    assert len(repository.load().facts) == 8
    with pytest.raises(RuntimeError, match="read-only"):
        repository.append_many(_facts("SH.600001"))


def test_exact_bare_code_mapping_retains_formal_reviews_and_reports_denominator(tmp_path):
    iid = "SH.600000"
    root, sid = _snapshot(tmp_path, [_instrument(iid, "C39")])
    repository = FinancialFactRepository(tmp_path / "facts.sqlite")
    for facts, review in _reviewed_bundles("600000"):
        repository.append_many(facts, review_bundle=review)
    report = build_s2_readiness_report(snapshot_dir=root, snapshot_id=sid,
        facts_db=repository.db_path, latest_period_end=date(2025, 12, 31), include_signals=True)
    assert report["complete_s2_count"] == report["ranking_pool_size"] == 1
    assert report["required_metric_period_count"] == report["accepted_metric_period_count"] == 8
    assert report["scope_status"] == "READY"
    assert report["signals"][0]["instrument_id"] == iid
    bridge = report["security_demand"][0]["financial_inputs"]
    assert bridge["average_parent_equity"] == "450"
    assert bridge["consolidated_net_profit_ttm"] == "120"
    assert {fact.instrument_id for fact in repository.load().facts} == {"600000"}


def test_fixed_h1_denominator_keeps_securities_without_facts(tmp_path):
    root, sid = _snapshot(tmp_path, [_instrument("SH.600000", "C39"),
                                   _instrument("SZ.000001", "J66")])
    repository = FinancialFactRepository(tmp_path / "facts.sqlite")
    report = build_s2_readiness_report(snapshot_dir=root, snapshot_id=sid,
        facts_db=repository.db_path, latest_period_end=date(2026, 6, 30))
    assert report["pool_size"] == 2
    assert report["required_metric_period_count"] == 32
    assert report["accepted_metric_period_count"] == 0
    assert len(report["security_demand"]) == 2
    assert all(row["financial_inputs"] is None for row in report["security_demand"])


def test_readiness_ranks_only_accepted_pool_and_keeps_unreviewed_successors(tmp_path):
    root, sid = _snapshot(tmp_path, [_instrument("SH.600000", "C39"),
                                   _instrument("SH.600002", "C39")])
    repository = FinancialFactRepository(tmp_path / "facts.sqlite")
    for facts, review in _reviewed_bundles("600000"):
        repository.append_many(facts, review_bundle=review)
    repository.append_many(_facts("600002"))
    report = build_s2_readiness_report(snapshot_dir=root, snapshot_id=sid,
        facts_db=repository.db_path, include_signals=True)
    assert report["ranking_pool_size"] == 1
    assert report["signals"][0]["signal_rank"] == 0.5
    assert report["scope_status"] == "PARTIAL_READY"
    assert report["exclusion_counts"] == {"S2_REVIEW_MISSING": 1}


def test_wrong_exchange_never_maps_into_snapshot(tmp_path):
    root, sid = _snapshot(tmp_path, [_instrument("SH.600000", "C39")])
    repository = FinancialFactRepository(tmp_path / "facts.sqlite")
    repository.append_many(_facts("SZ.600000"))
    report = build_s2_readiness_report(snapshot_dir=root, snapshot_id=sid, facts_db=repository.db_path)
    assert report["fact_versions_in_pool"] == 0


def test_production_unproven_cap_is_not_a_real_denominator(tmp_path):
    root, sid = _snapshot(tmp_path, [_instrument("SH.600000", "C39")], data_mode=DataMode.PRODUCTION)
    repository = FinancialFactRepository(tmp_path / "facts.sqlite")
    for facts, review in _reviewed_bundles("600000"):
        repository.append_many(facts, review_bundle=review)
    report = build_s2_readiness_report(snapshot_dir=root, snapshot_id=sid, facts_db=repository.db_path)
    assert report["complete_s2_count"] == 0
    assert report["missing_market_cap_count"] == 1
    assert report["security_demand"][0]["financial_input_status"] == "ACCEPTED"
    assert report["security_demand"][0]["market_cap"]["value_yuan"] is None


def test_production_frozen_cap_matches_original_archive_and_real_f10(tmp_path):
    from tests.unit.test_decision_market_caps_archive import _real_frozen_cap
    archive, instrument, cutoff = _real_frozen_cap(tmp_path)
    instrument["industry_code"] = "C39"
    root, sid = _snapshot(tmp_path, [instrument], data_mode=DataMode.PRODUCTION,
                          trading_day="2026-09-22", cutoff=cutoff)
    repository = FinancialFactRepository(tmp_path / "facts.sqlite")
    for facts, review in _reviewed_bundles("600519"):
        repository.append_many(facts, review_bundle=review)
    report = build_s2_readiness_report(snapshot_dir=root, snapshot_id=sid,
        facts_db=repository.db_path, market_caps_archive_root=archive, include_signals=True)
    assert report["scope_status"] == "READY"
    assert report["market_cap_evidence"]["mode"] == "RECEIPT_VERIFIED_FROZEN_SNAPSHOT"
    assert report["security_demand"][0]["market_cap"]["value_yuan"] == "1234.56"
    assert report["signals"][0]["factors"]["F10"] == str(Decimal(100) / Decimal("1234.56"))


def test_per_security_target_applies_to_denominator_bridge_and_ready_scope(tmp_path):
    root, sid = _snapshot(tmp_path, [_instrument("SH.600000", "C39"),
                                   _instrument("SH.600002", "C39")])
    repository = FinancialFactRepository(tmp_path / "facts.sqlite")
    for code in ("600000", "600002"):
        for facts, review in _reviewed_bundles(code):
            repository.append_many(facts, review_bundle=review)
    report = build_s2_readiness_report(snapshot_dir=root, snapshot_id=sid,
        facts_db=repository.db_path, latest_period_end=date(2026, 6, 30),
        latest_period_ends={"600000": date(2025, 12, 31)}, include_signals=True)
    assert report["required_metric_period_count"] == 8 + 16
    assert report["complete_s2_count"] == report["ranking_pool_size"] == 1
    assert report["eligible_scope"]["ranking_pool_ids"] == ["SH.600000"]
    rows = {row["instrument_id"]: row for row in report["security_demand"]}
    assert rows["SH.600000"]["financial_input_status"] == "ACCEPTED"
    assert rows["SH.600002"]["financial_input_status"] == "S2_TARGET_PERIOD_MISMATCH"
    assert rows["SH.600000"]["target_period_basis"] == "EXPLICIT_SECURITY_TARGET"


def test_larger_shared_target_source_is_projected_without_injecting_extra_ids(tmp_path):
    root, sid = _snapshot(tmp_path, [_instrument("SH.600000", "C39")])
    repository = FinancialFactRepository(tmp_path / "facts.sqlite")
    report = build_s2_readiness_report(snapshot_dir=root, snapshot_id=sid,
        facts_db=repository.db_path, latest_period_end=date(2026, 6, 30),
        latest_period_ends={"SH.600000": date(2026, 6, 30), "SZ.000333": date(2026, 9, 30)})
    assert report["pool_size"] == 1
    assert report["required_metric_period_count"] == 16
    assert report["target_map_projection"]["unused_source_ids"] == ["SZ.000333"]
    assert report["eligible_scope"]["requested_pool_ids"] == ["SH.600000"]
    with pytest.raises(ValueError, match="无法归一|不支持"):
        build_s2_readiness_report(snapshot_dir=root, snapshot_id=sid,
            facts_db=repository.db_path, latest_period_ends={"SH.000333": date(2026, 6, 30)})


def test_future_q3_discovery_keeps_original_decision_baseline_and_names_future_target(tmp_path):
    root, sid = _snapshot(tmp_path, [_instrument("SH.600000", "C39")])
    repository = FinancialFactRepository(tmp_path / "facts.sqlite")
    for facts, review in _reviewed_bundles("600000"):
        repository.append_many(facts, review_bundle=review)
    report = build_s2_readiness_report(snapshot_dir=root, snapshot_id=sid,
        facts_db=repository.db_path, latest_period_end=date(2025, 12, 31),
        latest_period_ends={"600000": date(2026, 9, 30)},
        target_period_records=[{"instrument_id": "SH.600000", "latest_period_end": "2026-09-30",
            "index_observed_at": (CUTOFF + timedelta(seconds=1)).isoformat(), "discovery_status": "INDEX_COMPLETE"}])
    assert report["complete_s2_count"] == 1
    assert report["required_metric_period_count"] == 8
    assert report["security_demand"][0]["target_period_end"] == "2025-12-31"
    assert report["security_demand"][0]["target_discovery"]["latest_period_end"] == "2026-09-30"
    assert report["target_map_projection"]["future_observed_target_ids"] == ["SH.600000"]


def test_frozen_target_map_validates_declared_scope_and_source_hash(tmp_path):
    from aquant.operations.s2_readiness import load_s2_target_period_map
    artifact = tmp_path / "discovery.json"
    artifact.write_text("{}", encoding="utf-8")
    payload = {"schema": "aquant.s2_target_periods.v1", "source_scope_ids": ["SH.600000"],
               "latest_period_ends": {"600000": "2026-06-30"},
               "records": [{"instrument_id": "SH.600000", "latest_period_end": "2026-06-30"}],
               "source_artifacts": [{"role": "official_discovery", "path": str(artifact),
                    "content_hash": "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()}]}
    path = tmp_path / "target-map.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_s2_target_period_map(path)["latest_period_ends"] == {"SH.600000": "2026-06-30"}
    artifact.write_text("changed", encoding="utf-8")
    with pytest.raises(S2ReadinessError, match="source artifact hash changed"):
        load_s2_target_period_map(path)


def test_target_map_scope_cannot_misstate_its_hash_bound_frozen_source(tmp_path):
    from aquant.operations.s2_readiness import load_s2_target_period_map
    artifact = tmp_path / "source-instruments.json"
    artifact.write_text(json.dumps([{"instrument_id": "SH.600000"}, {"instrument_id": "SH.600002"}]), encoding="utf-8")
    payload = {"schema": "aquant.s2_target_periods.v1", "source_scope_ids": ["SH.600000"],
               "latest_period_ends": {"SH.600000": "2026-06-30"},
               "records": [{"instrument_id": "SH.600000", "latest_period_end": "2026-06-30"}],
               "source_artifacts": [{"role": "frozen_pool_instruments", "path": str(artifact),
                    "content_hash": "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()}]}
    path = tmp_path / "target-map.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(S2ReadinessError, match="declared scope differs from frozen source instruments"):
        load_s2_target_period_map(path)


@pytest.mark.parametrize("changed_claim", ["observation", "period", "undiscovered"])
def test_official_target_claims_are_rebuilt_from_unchanged_source(tmp_path, changed_claim):
    from aquant.domain.research.s2 import required_s2_periods
    from aquant.operations.s2_readiness import load_s2_target_period_map

    observed = "2026-10-13T04:30:00Z"
    artifact = tmp_path / "official-discovery.json"
    artifact.write_text(json.dumps({"companies": [{"code": "600000",
        "allCategoryComplete": True, "latestPeriodEnd": "2026-06-30",
        "allCategoryObservedAt": observed}]}), encoding="utf-8")

    def demand(period):
        required = required_s2_periods(date.fromisoformat(period))
        return {"required_periods": {key: [value.isoformat() for value in values]
                                    for key, values in required.items()},
                "required_metric_period_count": sum(map(len, required.values()))}

    payload = {"schema": "aquant.s2_target_periods.v1",
        "source_scope_ids": ["SH.600000", "SH.600002"],
        "latest_period_ends": {"SH.600000": "2026-06-30"},
        "records": [{"instrument_id": "SH.600000", "scope": "OTHER_100",
            "latest_period_end": "2026-06-30", "all_category_complete": True,
            "index_observed_at": observed, **demand("2026-06-30")},
            {"instrument_id": "SH.600002", "scope": "OTHER_100",
             "latest_period_end": None, "all_category_complete": False}],
        "source_artifacts": [{"role": "other100_official_discovery", "path": str(artifact),
            "content_hash": "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()}]}
    path = tmp_path / "target-map.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_s2_target_period_map(path)["latest_period_ends"] == {"SH.600000": "2026-06-30"}
    if changed_claim == "observation":
        payload["records"][0]["index_observed_at"] = "2026-01-01T00:00:00Z"
        expected = "observation time differs"
    else:
        iid = "SH.600002" if changed_claim == "undiscovered" else "SH.600000"
        row = payload["records"][1 if changed_claim == "undiscovered" else 0]
        row.update(latest_period_end="2025-12-31", all_category_complete=True,
                   index_observed_at=observed, **demand("2025-12-31"))
        payload["latest_period_ends"][iid] = "2025-12-31"
        expected = "period differs"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(S2ReadinessError, match=expected):
        load_s2_target_period_map(path)
