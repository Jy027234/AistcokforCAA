from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.data.pit import PitMode
from aquant.domain.data.snapshot import DataMode, SnapshotDraft, SnapshotStore, write_dataset
from aquant.domain.fundamentals.fact_repository import FinancialFactRepository
from aquant.domain.fundamentals.versioned import FinancialFact, ProfitScope, VersionedFinancialFactStore
from aquant.operations.financial_coverage import build_financial_coverage_report, security_code
from tools.initialize_live_financial_facts import initialize_live_repository

CUTOFF = datetime(2026, 10, 12, 16, tzinfo=timezone.utc)


def fact(*, version="live", seen=CUTOFF - timedelta(days=1), available=None, predecessor=None,
         mode="LIVE_OBSERVED", metric="revenue", period=date(2026, 6, 30)):
    return FinancialFact(instrument_id="600323", metric=metric, period_end=period,
        statement_scope="CONSOLIDATED", profit_scope=(ProfitScope.ATTRIBUTABLE if metric == "net_profit_attributable" else
        ProfitScope.CONSOLIDATED if metric == "net_profit_consolidated" else None), value=Decimal("100"),
        currency="CNY", raw_unit="yuan", source_id="cninfo", source_document_id=version,
        source_published_date=date(2026, 8, 1), source_published_at=None,
        timestamp_precision="DATE", first_seen_at=seen, ingested_at=seen,
        available_at=available or seen, availability_basis="OBSERVED" if mode == "LIVE_OBSERVED" else "RECONSTRUCTED",
        pit_mode=mode, content_hash="sha256:" + "1" * 64, version_id=version, supersedes_id=predecessor)


def snapshot(tmp_path):
    root = tmp_path / "snapshot"
    sid = "snap-financial-coverage"
    con = connect(root / "meta.sqlite")
    try:
        apply_migrations(con)
        datasets = []
        legacy = {"source_id": "baostock", "updated_at": "2026-09-24T16:34:28+00:00",
                  "statements": {"SH.600323": {"2026Q2": {"code": "sh.600323", "netProfit": "100",
                    "pubDate": "2026-08-21", "statDate": "2026-06-30"}}}}
        for name, data in (("instruments", [{"instrument_id": "SH.600323", "industry_code": "C39"}]),
                           ("financials", legacy),
                           ("trading_calendar", ["2026-08-24", "2026-10-12"]),
                           ("daily_quotes", [{"instrument_id": "SH.600323", "trading_day": "2026-10-12",
                             "open_cents": 100, "close_cents": 100, "high_cents": 100, "low_cents": 100,
                             "volume_shares": 100}])):
            ref = write_dataset(root / "api", f"datasets/{sid}/{name}.json", json.dumps(data).encode())
            datasets.append(replace(ref, as_of_upper_bound=CUTOFF))
        SnapshotStore(con, root / "api").publish(SnapshotDraft(snapshot_id=sid, kind="EOD",
            data_mode=DataMode.SYNTHETIC, input_cutoff_at=CUTOFF, as_of_time=CUTOFF, created_at=CUTOFF,
            code_version="test", data_version="test", watermark="TEST", datasets=datasets))
    finally:
        con.close()
    return root, sid


def report(tmp_path, monkeypatch, facts, accepted=(), historical=(), **options):
    root, sid = snapshot(tmp_path)

    class Repo:
        def __init__(self, rows):
            self.rows = rows

        def load_with_accepted_s2_pdf_reviews(self):
            return VersionedFinancialFactStore(self.rows), {version: ("proof", "hash") for version in accepted}

        def load(self):
            return VersionedFinancialFactStore(self.rows)

    monkeypatch.setattr(FinancialFactRepository, "open_existing",
                        lambda path: Repo(historical if str(path) == "historical" else facts))
    return build_financial_coverage_report(snapshot_dir=root, snapshot_id=sid,
        live_facts_db="live", reconstructed_facts_db="historical", **options)


def test_legacy_presence_is_separate_from_valid_ttm_and_official_proof(tmp_path, monkeypatch):
    r = report(tmp_path, monkeypatch, [fact()], accepted=["live"])
    assert r["s1Optional"]["statementSecurityCount"] == 1
    assert r["s1Optional"]["ttmSecurityCount"] == 0
    row = r["securities"][0]
    assert row["s1Optional"]["missingPeriods"] == ["2025Q2", "2025Q4"]
    assert r["formalS2"]["acceptedMetricPeriodCount"] == 1
    assert r["formalS2"]["completeFactSecurityCount"] == 0
    assert r["formalS2"]["strategyFamilyStatus"] == "CLOSED"


def test_newly_observed_accepted_fact_is_pending_without_backfilling_snapshot(tmp_path, monkeypatch):
    future = fact(seen=CUTOFF + timedelta(hours=1), available=CUTOFF + timedelta(days=1))
    r = report(tmp_path, monkeypatch, [future], accepted=[future.version_id])
    assert r["formalS2"]["acceptedMetricPeriodCount"] == 0
    assert r["formalS2"]["pendingMetricPeriodCount"] == 1
    pairs = r["securities"][0]["formalS2"]["metricPeriods"]
    pending = next(p for p in pairs if p["metric"] == "revenue" and p["periodEnd"] == "2026-06-30")
    assert pending["status"] == "NEXT_CUTOFF_PENDING"
    assert pending["firstSeenAt"] == future.first_seen_at.isoformat()
    assert pending["availableAt"] == future.available_at.isoformat()
    assert pending["valueYuan"] == "100"


def test_unreviewed_current_successor_never_resurrects_accepted_prior(tmp_path, monkeypatch):
    old = fact()
    successor = fact(version="revised", seen=CUTOFF, predecessor=old.version_id)
    r = report(tmp_path, monkeypatch, [old, successor], accepted=[old.version_id])
    assert r["formalS2"]["acceptedMetricPeriodCount"] == 0
    pair = next(p for p in r["securities"][0]["formalS2"]["metricPeriods"]
                if p["metric"] == "revenue" and p["periodEnd"] == "2026-06-30")
    assert pair["status"] == "REVIEW_MISSING"
    assert pair["versionId"] == successor.version_id


def test_reconstruction_never_counts_as_live_even_in_a_mixed_repository(tmp_path, monkeypatch):
    reconstructed = fact(version="reconstructed", mode="HISTORICAL_RECONSTRUCTED")
    r = report(tmp_path, monkeypatch, [reconstructed], historical=[reconstructed])
    assert r["formalS2"]["liveFactCount"] == 0
    assert r["formalS2"]["acceptedMetricPeriodCount"] == 0
    assert r["historicalReconstructed"]["availableMetricPeriodCount"] == 1


def test_explicit_security_latest_discovery_changes_only_its_demand(tmp_path, monkeypatch):
    r = report(tmp_path, monkeypatch, [], latest_period_ends={"600323": date(2026, 9, 30)},
               target_period_basis="official indexed report discovery")
    row = r["securities"][0]
    assert row["targetPeriodEnd"] == "2026-09-30"
    assert {p["periodEnd"] for p in row["formalS2"]["metricPeriods"]} == {
        "2024-09-30", "2024-12-31", "2025-09-30", "2025-12-31", "2026-09-30"}
    assert row["s1Optional"]["missingPeriods"] == ["2025Q3", "2025Q4", "2026Q3"]


def test_pending_bridge_exposes_real_denominators_without_formal_signal(tmp_path, monkeypatch):
    from aquant.domain.research.s2 import required_s2_periods
    facts = [replace(fact(metric=metric, period=period, version=f"{metric}-{period}",
                         seen=CUTOFF + timedelta(hours=1), available=CUTOFF + timedelta(days=1)),
                     source_document_id=period.isoformat())
             for metric, periods in required_s2_periods(date(2026, 6, 30)).items()
             for period in periods]
    r = report(tmp_path, monkeypatch, facts, accepted=[row.version_id for row in facts])
    formal = r["securities"][0]["formalS2"]
    assert formal["financialInputStatus"] == "NEXT_CUTOFF_PENDING"
    assert formal["financialInputs"]["average_parent_equity"] == "100"
    assert formal["financialInputs"]["consolidated_net_profit_ttm"] == "100"
    assert formal["financialInputs"]["prior_revenue_ttm"] == "100"
    assert set(formal["financialInputs"]["fact_version_ids"]) == {f.version_id for f in facts}
    assert formal["marketCap"]["status"] == "MISSING_OR_UNVERIFIED"
    assert formal["signal"] is None and not formal["completeFactors"]
    assert r["formalS2"]["acceptedMetricPeriodCount"] == 0
    assert r["formalS2"]["pendingMetricPeriodCount"] == 16
    assert r["formalS2"]["rankingPoolSize"] == 0


def test_concurrent_revision_cannot_mix_inventory_pairs_and_financial_bridge(tmp_path, monkeypatch):
    from aquant.domain.research.s2 import required_s2_periods
    root, sid = snapshot(tmp_path)
    initial = [replace(fact(metric=metric, period=period, version=f"{metric}-{period}"),
                       source_document_id=period.isoformat())
               for metric, periods in required_s2_periods(date(2026, 6, 30)).items()
               for period in periods]
    revisions = [replace(row, version_id=row.version_id + "-revision",
                         supersedes_id=row.version_id,
                         source_document_id="concurrent-report-revision",
                         content_hash="sha256:" + "2" * 64,
                         value=Decimal("200") if row.metric == "revenue" else row.value,
                         first_seen_at=row.first_seen_at + timedelta(hours=1),
                         ingested_at=row.ingested_at + timedelta(hours=1),
                         available_at=row.available_at + timedelta(hours=1))
                 for row in initial if row.period_end == date(2026, 6, 30)]
    backing = list(initial)

    class ChangingRepo:
        def load_with_accepted_s2_pdf_reviews(self):
            captured = list(backing)
            if revisions[0] not in backing:
                backing.extend(revisions)  # Another promoter commits after this read.
            return (VersionedFinancialFactStore(captured),
                    {row.version_id: ("proof", "hash") for row in captured})

    monkeypatch.setattr(FinancialFactRepository, "open_existing", lambda path: ChangingRepo())
    result = build_financial_coverage_report(snapshot_dir=root, snapshot_id=sid, live_facts_db="live")
    formal = result["securities"][0]["formalS2"]
    pair_versions = {row["versionId"] for row in formal["metricPeriods"]}
    assert formal["financialInputStatus"] == "ACCEPTED"
    assert set(formal["financialInputs"]["fact_version_ids"]) == pair_versions
    assert not {row.version_id for row in revisions} & pair_versions
    assert result["formalS2"]["liveFactCount"] == len(initial)


def test_future_report_discovery_keeps_original_cutoff_demand(tmp_path, monkeypatch):
    r = report(tmp_path, monkeypatch, [], latest_period_ends={"600323": date(2026, 9, 30)},
        target_period_records=[{"instrument_id": "SH.600323", "latest_period_end": "2026-09-30",
            "index_observed_at": (CUTOFF + timedelta(hours=1)).isoformat(),
            "discovery_status": "DISCOVERED", "all_category_complete": True}])
    row = r["securities"][0]
    assert row["targetPeriodEnd"] == "2026-06-30"
    assert row["targetPeriodDiscovery"]["latest_period_end"] == "2026-09-30"
    assert not row["targetPeriodDiscovery"]["effective_at_cutoff"]
    assert r["formalS2"]["requiredMetricPeriodCount"] == 16
    assert row["formalS2"]["signal"] is None


def test_live_inheritance_preserves_facts_and_rejects_same_path_or_mixed_target(tmp_path):
    source = tmp_path / "source.sqlite"
    target = tmp_path / "live.sqlite"
    live = fact()
    reconstructed = fact(version="hist", mode="HISTORICAL_RECONSTRUCTED", period=date(2025, 6, 30))
    FinancialFactRepository(source).append_many([live, reconstructed])
    before = source.read_bytes()
    first = initialize_live_repository(source=source, destination=target)
    assert first["newlyInsertedFactCount"] == 1
    assert first["excludedReconstructedFactCount"] == 1
    assert FinancialFactRepository.open_existing(target).load().facts == (live,)
    assert source.read_bytes() == before
    assert initialize_live_repository(source=source, destination=target)["newlyInsertedFactCount"] == 0
    with pytest.raises(ValueError, match="differ"):
        initialize_live_repository(source=source, destination=source)
    mixed = tmp_path / "mixed.sqlite"
    FinancialFactRepository(mixed).append_many([reconstructed])
    with pytest.raises(ValueError, match="another PIT mode"):
        initialize_live_repository(source=source, destination=mixed)


@pytest.mark.parametrize("value", ["600323.X", "SH.60032", "C600323", "600323000"])
def test_identity_requires_exact_security_code(value):
    with pytest.raises(ValueError):
        security_code(value)
