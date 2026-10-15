"""Offline fixtures for newly admitted status evidence, never provider validation."""
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/api"))
from main import _s1_candidates
from aquant.domain.portfolio.construction import Candidate, ConstructionParams, construct_targets, weights_to_orders

IDS = ["SH.600001", "SZ.000001"]
PRICE_DAY = date(2026, 10, 9)
CUTOFF = datetime.fromisoformat("2026-10-11T12:00:00+08:00")


class StatusReader:
    def __init__(self, case):
        status = {"status": "VERIFIED", "as_of_date": PRICE_DAY.isoformat(),
            "available_at": "2026-10-11T10:00:00+08:00", "observed_at": "2026-10-11T10:00:00+08:00",
            "is_st": False, "is_suspended": False,
            "sources": [{"source_id": "local-fixture", "receipt_id": "local-receipt", "content_hash": "sha256:" + "a" * 64}]}
        self.insts = [{"instrument_id": iid, "industry_code": "C39", "board": "MAIN",
                       "simulation_eligibility": deepcopy(status)} for iid in IDS]
        bad = self.insts[0]
        if case == "ST": bad["simulation_eligibility"]["is_st"] = True
        if case == "SUSPENDED": bad["simulation_eligibility"]["is_suspended"] = True
        if case == "UNKNOWN": bad["simulation_eligibility"] = None
        if case == "MISSING": bad.pop("simulation_eligibility")
        if case == "OBSERVATION_DATE": bad["simulation_eligibility"]["as_of_date"] = CUTOFF.date().isoformat()
        if case == "LEGACY":
            for inst in self.insts: inst.pop("simulation_eligibility")

    def ref(self, _):
        return SimpleNamespace(as_of_time=CUTOFF)

    def instruments(self, _, **kwargs):
        return self.insts

    def daily_quotes(self, _, *, instrument_id=None, **kwargs):
        return [SimpleNamespace(trading_day=PRICE_DAY - timedelta(days=60-i), adjusted_close_cents=1000+i*(j+1))
                for j, iid in enumerate(IDS) if instrument_id is None or iid == instrument_id
                for i in range(61)]


@pytest.mark.parametrize("case", ["ST", "SUSPENDED", "UNKNOWN", "MISSING", "OBSERVATION_DATE"])
def test_s1_new_evidence_blocks_unsafe_status_using_price_day_not_observation_day(case):
    ranked, _ = _s1_candidates(SimpleNamespace(reader=StatusReader(case)), "local-new-status-fixture")
    flags = {candidate.instrument_id: candidate.simulatable for candidate in ranked}
    assert flags[IDS[0]] is False
    assert flags[IDS[1]] is True
    params = ConstructionParams(initial_target_pct=Decimal("4"))
    targets = construct_targets(candidates=[Candidate(row.instrument_id, row.industry_code,
        row.signal_rank, simulatable=row.simulatable) for row in ranked], params=params,
        bar_by_instrument={iid: SimpleNamespace(close_cents=1000) for iid in IDS})
    orders = weights_to_orders(targets=targets.targets, params=params,
                               price_by_instrument={iid: 1000 for iid in IDS})
    assert any(order["instrument_id"] == IDS[1] and order["side"] == "BUY" for order in orders)
    assert all(order["instrument_id"] != IDS[0] for order in orders)


def test_s1_legacy_archive_without_any_status_field_keeps_existing_board_semantics():
    ranked, _ = _s1_candidates(SimpleNamespace(reader=StatusReader("LEGACY")), "local-legacy-status-fixture")
    assert len(ranked) == 2 and all(candidate.simulatable for candidate in ranked)
