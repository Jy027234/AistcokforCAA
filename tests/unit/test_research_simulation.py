"""Local contract fixtures; none of these tests is real-provider validation."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.data.reader import QuoteRow
from aquant.domain.portfolio.construction import ConstructionParams
from aquant.domain.portfolio.plan import PlanError, PlanService
from aquant.domain.simulation.board_rules import BOARD_RULES
from aquant.domain.simulation.fees import FeeSchedule, FeeTable
from aquant.operations.research_candidates import ResearchCandidateError, file_hash
from aquant.operations.research_simulation import (
    assert_draft_binding, assert_execution_eligibility, assert_user_selection_timing,
    load_simulation_research, project_pending_draft,
    research_draft_origin, research_drafts, save_research_draft, simulation_context,
)

IIDS = ["SH.600001", "SZ.000001"]
DAY = date(2026, 10, 9)
CUTOFF = datetime.fromisoformat("2026-10-11T12:00:00+08:00")


def packet():
    return {"snapshotId": "research-fixture", "dataAsOf": "2026-10-09",
            "generatedAt": "2026-10-09T21:30:00+08:00",
            "asOfTime": "2026-10-09T17:00:00+08:00", "capturedTo": "2026-10-09T17:00:00+08:00",
            "candidates": [{"instrumentId": iid, "displayName": "fixture", "position": i + 1,
                "exchange": "SSE" if iid.startswith("SH") else "SZSE", "board": "MAIN",
                "status": "RANKED", "signalRank": 1 - i / 2,
                "quote": {"providerName": "fixture", "observedAt": "2026-10-09T17:00:00+08:00"},
                "evidence": []} for i, iid in enumerate(IIDS)]}


def binding():
    return {"researchSnapshotId": "research-fixture", "researchContentHash": "sha256:" + "a" * 64,
            "researchDataAsOf": "2026-10-09", "researchObservedAt": "2026-10-09T17:00:00+08:00",
            "researchAvailableAt": "2026-10-09T21:30:00+08:00", "researchOriginPath": "fixture.json"}


class Reader:
    """Explicitly synthetic in-memory input for temporal and rule contract tests."""
    def __init__(self):
        self.cutoff = CUTOFF
        self.mode = "PRODUCTION"  # Exercise production guards using local fixtures.
        self.calendar = [(date(2026, 1, 1) + timedelta(days=i)).isoformat()
                         for i in range((DAY - date(2026, 1, 1)).days + 1)] + ["2026-10-12"]
        self.insts = [{"instrument_id": iid, "exchange": "SSE" if iid.startswith("SH") else "SZSE",
            "board": "MAIN", "industry_code": "C39", "listed_on": "2000-01-01",
            "simulation_eligibility": {"status": "VERIFIED", "as_of_date": "2026-10-09",
                "available_at": "2026-10-09T17:00:00+08:00", "observed_at": "2026-10-09T17:00:00+08:00",
                "is_st": False, "is_suspended": False,
                "sources": [{"source_id": "fixture", "receipt_id": "test-local", "content_hash": "sha256:" + "c" * 64}]}}
            for iid in IIDS]
        self.rows = [QuoteRow(instrument_id=iid, trading_day=DAY - timedelta(days=i),
            open_cents=1000, high_cents=1020, low_cents=980, close_cents=1000,
            adjusted_close_cents=1000, volume_shares=1_000_000, amount_cents=2_000_000_000,
            prev_close_cents=1000) for iid in IIDS for i in range(21)]

    def ref(self, _):
        return SimpleNamespace(as_of_time=self.cutoff, published_at=self.cutoff, data_mode=self.mode)

    def instruments(self, _, **kwargs):
        return self.insts

    def trading_calendar(self, _, **kwargs):
        return self.calendar

    def daily_quotes(self, _, *, as_of, instrument_id=None, start=None, end=None):
        return sorted([r for r in self.rows if (not instrument_id or r.instrument_id == instrument_id)
                       and (not start or r.trading_day >= start) and (not end or r.trading_day <= end)],
                      key=lambda r: (r.instrument_id, r.trading_day))


def params():
    return ConstructionParams(max_holdings=4, liquidity_min_avg_amount_cents=100_000_000)


def test_saved_user_choice_uses_actual_saved_time_and_engineering_is_not_forward():
    assert_user_selection_timing(source="USER_SELECTION", trading_day=date(2026, 10, 12),
                                 saved_at="2026-10-12T09:29:59.999999+08:00")
    with pytest.raises(PlanError) as refused:
        assert_user_selection_timing(source="USER_SELECTION", trading_day=date(2026, 10, 12),
                                     saved_at="2026-10-12T01:30:00+00:00")
    assert refused.value.code == "PIT_UNVERIFIED"
    assert_user_selection_timing(source="ENGINEERING_TRIAL", trading_day=date(2026, 10, 12),
                                 saved_at="2026-10-12T20:30:00+08:00")


def context(reader=None):
    return simulation_context(packet(), binding(), reader=reader or Reader(), snapshot_id="formal-fixture", params=params())


def test_exact_source_hash_and_actual_available_time_are_bound(tmp_path, monkeypatch):
    original = packet()
    original["candidates"][0]["evidence"] = [{"availableAt": "2026-10-10T08:00:00+08:00",
                                               "observedAt": "2026-10-10T07:00:00+08:00"}]
    source = tmp_path / "research.json"
    source.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr("aquant.operations.research_simulation.load_research_scan", lambda *a, **k: {})
    loaded, bound = load_simulation_research(source)
    assert loaded == original and bound["researchContentHash"] == file_hash(source)
    assert bound["researchAvailableAt"] == "2026-10-10T08:00:00+08:00"


def test_context_accepts_verified_current_inputs_and_next_calendar_session():
    result, eligible, _ = context()
    assert result["nextTradingDay"] == "2026-10-12"
    assert result["eligibleCandidateCount"] == 2
    assert [c.instrument_id for c in eligible] == IIDS
    assert not result["formalStrategy"] and not result["blockingIssues"]
    assert all(row["eligible"] for row in result["candidates"])


@pytest.mark.parametrize("defect,code", [
    ("missing", "INSTRUMENT_NOT_IN_DECISION_SNAPSHOT"),
    ("status", "SECURITY_STATUS_UNVERIFIED"),
    ("listed", "LISTING_AGE_UNVERIFIED"),
    ("amount", "LIQUIDITY_WINDOW_INCOMPLETE"),
    ("current", "CURRENT_REFERENCE_PRICE_MISSING"),
    ("board", "BOARD_NOT_SIMULATABLE_IN_PHASE_1"),
    ("source", "SECURITY_STATUS_UNVERIFIED"),
    ("future_status", "SECURITY_STATUS_UNVERIFIED"),
    ("history_gap", "LIQUIDITY_WINDOW_INCOMPLETE"),
])
def test_unknown_or_incomplete_qualification_never_falls_back(defect, code):
    reader = Reader()
    if defect == "missing": reader.insts.pop(0)
    if defect == "status": reader.insts[0].pop("simulation_eligibility")
    if defect == "listed": reader.insts[0]["listed_on"] = None
    if defect == "amount": reader.rows[0] = replace(reader.rows[0], amount_cents=None)
    if defect == "current": reader.rows = [r for r in reader.rows if not (r.instrument_id == IIDS[0] and r.trading_day == DAY)]
    if defect == "board": reader.insts[0]["board"] = "STAR"
    if defect == "source": reader.insts[0]["simulation_eligibility"]["sources"][0]["content_hash"] = "unknown"
    if defect == "future_status": reader.insts[0]["simulation_eligibility"]["observed_at"] = "2026-10-12T17:00:00+08:00"
    if defect == "history_gap": reader.rows = [r for r in reader.rows if not (r.instrument_id == IIDS[0] and r.trading_day == DAY - timedelta(days=3))]
    result, eligible, _ = context(reader)
    bad = result["candidates"][0]
    assert not bad["eligible"]
    assert code in {i["code"] for i in bad["blockingIssues"]}
    assert IIDS[0] not in {c.instrument_id for c in eligible}


def test_latest_ranking_cannot_be_backdated_into_old_eod():
    reader = Reader()
    reader.cutoff = datetime.fromisoformat("2026-10-09T15:00:00+08:00")
    result, eligible, _ = context(reader)
    assert not eligible
    with pytest.raises(PlanError) as exc:
        assert_draft_binding(result, research_snapshot_id="research-fixture", research_content_hash=binding()["researchContentHash"],
                             decision_cutoff_at=reader.cutoff, trading_day=date(2026, 10, 12))
    assert exc.value.code == "PIT_UNVERIFIED"


def test_missing_future_calendar_is_explicit_and_not_inferred_from_weekday():
    reader = Reader()
    reader.calendar.remove("2026-10-12")
    result, eligible, _ = context(reader)
    assert result["nextTradingDay"] is None and not eligible
    assert any(i["code"] == "CALENDAR_NOT_COVERED" for i in result["blockingIssues"])


def test_prior_status_cannot_be_reused_for_future_execution():
    reader = Reader()
    with pytest.raises(PlanError, match="execution-day security status"):
        assert_execution_eligibility(reader, decision_snapshot_id="formal-fixture",
            execution_snapshot_id="future-fixture", trading_day=date(2026, 10, 12), instrument_ids=[IIDS[0]])


@pytest.mark.parametrize("change,code", [("hash", "STALE_SNAPSHOT"), ("cutoff", "PIT_UNVERIFIED"), ("date", "PIT_UNVERIFIED")])
def test_explicit_binding_rejects_changed_hash_cutoff_or_skipped_session(change, code):
    result, _, _ = context()
    kwargs = {"research_snapshot_id": "research-fixture", "research_content_hash": binding()["researchContentHash"],
              "decision_cutoff_at": CUTOFF, "trading_day": date(2026, 10, 12)}
    if change == "hash": kwargs["research_content_hash"] = "sha256:" + "b" * 64
    if change == "cutoff": kwargs["decision_cutoff_at"] = CUTOFF - timedelta(seconds=1)
    if change == "date": kwargs["trading_day"] = date(2026, 10, 13)
    with pytest.raises(PlanError) as exc: assert_draft_binding(result, **kwargs)
    assert exc.value.code == code


def test_pending_projection_uses_reference_close_and_actual_fee_guard_without_writes(tmp_path):
    con = connect(tmp_path / "projection.db")
    apply_migrations(con)
    result, eligible, indexed = context()
    fees = FeeTable([FeeSchedule("fixture-real-fee", date(2023, 8, 28), None, Decimal("0.00025"),
        500, Decimal("0.0005"), Decimal("0.00001"), synthetic_test_rate=False)], commission_source="USER_APPROVED_ASSUMPTION")
    service = PlanService(con, indexed, fees, BOARD_RULES, {IIDS[0]: ("SSE", "MAIN"), IIDS[1]: ("SZSE", "MAIN")}, params())
    projection = project_pending_draft(service, portfolio_id="pf-fixture", snapshot_id="formal-fixture",
        trading_day=date(2026, 10, 12), candidates=eligible, cash_cents=100_000_000, lots=[])
    assert projection["orders"] and projection["estimatedFeesCents"] > 0
    assert all(o["reference_price_day"] == "2026-10-09" for o in projection["orders"])
    assert not projection["executionVerified"]
    assert projection["cashAfterCents"] == 100_000_000 - sum(o["gross_cents"] for o in projection["orders"]) - projection["estimatedFeesCents"]
    for table in ("simulation_plan", "order", "fill", "cash_entry", "portfolio"):
        assert con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0
    con.close()


def test_durable_drafts_archive_original_and_isolate_subject_and_engineering_source(tmp_path):
    con = connect(tmp_path / "drafts.db")
    apply_migrations(con)
    artifact = tmp_path / "origin.json"
    artifact.write_text(json.dumps(packet()), encoding="utf-8")
    document = {**binding(), "researchContentHash": file_hash(artifact), "source": "ENGINEERING_TRIAL",
                "portfolioId": "pf-engineering", "decisionSnapshotId": "formal-fixture", "tradingDay": "2026-10-12",
                "status": "WAITING_EXECUTION_SNAPSHOT", "planId": None, "confirmationAvailable": False}
    first = save_research_draft(con, data_dir=tmp_path, research_path=artifact, subject="user:alice", payload=document)
    second = save_research_draft(con, data_dir=tmp_path, research_path=artifact, subject="user:alice", payload=document)
    assert first == second
    origin = Path(first["researchOriginPath"])
    assert file_hash(origin) == document["researchContentHash"]
    artifact.write_text("new source", encoding="utf-8")
    con.close()
    con = connect(tmp_path / "drafts.db")
    rows = research_drafts(con, subject="user:alice")
    assert rows[0]["source"] == "ENGINEERING_TRIAL" and rows[0]["requiresRepreview"]
    assert not rows[0]["confirmationAvailable"] and not research_drafts(con, subject="user:bob")
    assert not research_drafts(con, subject="user:alice", portfolio_id="pf-other")
    loaded, path = research_draft_origin(con, data_dir=tmp_path, subject="user:alice", draft_id=first["draftId"])
    assert loaded == first and path.resolve() == origin
    with pytest.raises(KeyError):
        research_draft_origin(con, data_dir=tmp_path, subject="user:bob", draft_id=first["draftId"])
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE research_simulation_draft SET status='READY' WHERE draft_id=?", (first["draftId"],))
    origin.write_text("changed archived input", encoding="utf-8")
    with pytest.raises(ResearchCandidateError, match="immutable content hash"):
        research_draft_origin(con, data_dir=tmp_path, subject="user:alice", draft_id=first["draftId"])
    con.close()
