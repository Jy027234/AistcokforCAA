import json
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from aquant.domain.data.pit import AvailabilityBasis, PitMode
from aquant.domain.fundamentals.fact_repository import FinancialFactRepository
from aquant.domain.fundamentals.versioned import FinancialFact, StatementScope
from aquant.operations.research_candidates import (
    ResearchCandidateError, build_research_candidates, file_hash, load_research_candidates,
)


def fixture_inputs(tmp_path):
    fact = FinancialFact(instrument_id="600519", period_end=date(2026, 6, 30),
        available_at=datetime(2026, 8, 17, tzinfo=timezone.utc), source_document_id="report",
        metric="revenue", value=Decimal("100"), content_hash="sha256:" + "a" * 64,
        statement_scope=StatementScope.CONSOLIDATED, profit_scope=None,
        currency="CNY", raw_unit="yuan", source_id="eastmoney-structured-f10",
        source_published_date=date(2026, 8, 15), source_published_at=None, timestamp_precision="DATE",
        first_seen_at=datetime(2026, 10, 7, tzinfo=timezone.utc),
        ingested_at=datetime(2026, 10, 9, tzinfo=timezone.utc),
        availability_basis=AvailabilityBasis.RECONSTRUCTED,
        pit_mode=PitMode.HISTORICAL_RECONSTRUCTED, version_id="fact1")
    row = {"instrument_id": "600519", "factors": {key: "0.5" for key in ("F07", "F08", "F09", "F10")},
        "signal": {key + "_rank": 0.5 for key in ("F07", "F08", "F09", "F10")}}
    row["signal"]["signal_rank"] = 0.5
    row["factors"]["fact_version_ids"] = ["fact1"]
    report = {"pit_mode": "HISTORICAL_RECONSTRUCTED", "trading_day": "2026-09-24",
        "market_cap_source": {"archive_verified": True, "placeholder_used": False,
            "market_cap_as_of": "2026-09-24", "source_id": "tencent-qt"},
        "decision_cutoff_at": "2026-09-24T07:00:00+00:00", "instruments": [row],
        "pool_size": 1, "ranked_count": 1, "complete_factor_count": 1}
    bound = {"records": [{"instrument_id": "600519", "period_end": "2026-06-30",
        "status": "RECONSTRUCTED_READY", "values": {"revenue": "100"},
        "announcement": {"announcement_id": "report", "index_page_hash": fact.content_hash,
            "title": "2026年半年度报告", "published_date": "2026-08-15", "url": "https://static.cninfo.com.cn/report.PDF",
            "index_receipt_id": "receipt1"}}]}
    source = tmp_path / "trial.json"
    report["source_path"] = str(source)
    source.write_text(json.dumps(report), encoding="utf-8")
    boundfile = tmp_path / "bound.json"
    boundfile.write_text(json.dumps(bound), encoding="utf-8")
    namefile = tmp_path / "names.json"
    namefile.write_text(json.dumps({"stockList": [{"code": "600519", "zwjc": "贵州茅台"}]}), encoding="utf-8")
    industryfile = tmp_path / "industry.json"
    industryfile.write_text('{"instruments": []}', encoding="utf-8")
    capfile = tmp_path / "caps.json"
    capfile.write_text('{}', encoding="utf-8")
    factfile = tmp_path / "facts.sqlite"
    FinancialFactRepository(factfile).append_many([fact])
    provenance = [{"role": role, "path": str(file), "contentHash": file_hash(file)}
                  for role, file in [("factor_report", source), ("bound_records", boundfile),
                    ("company_names", namefile), ("industry_snapshot", industryfile),
                    ("market_caps", capfile), ("facts_db", factfile)]]
    return report, bound, {"fact1": fact}, provenance


def _packet(tmp_path):
    report, bound, facts, provenance = fixture_inputs(tmp_path)
    packet = build_research_candidates(report, bound, facts=facts, names={"600519": "贵州茅台"},
        industries={}, provenance=provenance)
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    return path, packet


def test_named_research_candidates_are_readonly_and_use_actual_factor_order(tmp_path):
    path, _ = _packet(tmp_path)
    packet = load_research_candidates(path)
    row = packet["candidates"][0]
    assert row["displayName"] == "贵州茅台"
    assert row["evidence"][0]["announcementId"] == "report"
    assert packet["dataAsOf"] == "2026-09-24"
    assert packet["researchOnly"] and packet["simulationAvailable"] is False
    assert row["basis"] and row["risks"]


@pytest.mark.parametrize("change", ["mode", "placeholder", "fact_identity", "future", "unproven_revision"])
def test_invalid_inputs_do_not_become_research_recommendations(tmp_path, change):
    report, bound, facts, provenance = fixture_inputs(tmp_path)
    if change == "mode": report["pit_mode"] = "LIVE_OBSERVED"
    if change == "placeholder": report["market_cap_source"]["placeholder_used"] = True
    if change == "fact_identity": facts["fact1"] = SimpleNamespace(instrument_id="000001")
    if change == "future": facts["fact1"] = SimpleNamespace(instrument_id="600519", available_at=datetime(2026, 10, 9, tzinfo=timezone.utc))
    if change == "unproven_revision": bound["records"][0]["revision_value_verification"] = {"status": "MISMATCH"}
    with pytest.raises(ResearchCandidateError):
        build_research_candidates(report, bound, facts=facts, names={}, industries={}, provenance=provenance)


@pytest.mark.parametrize("field,value", [("dataAsOf", "2026-10-09"), ("simulationAvailable", True),
                                       ("researchOnly", False), ("rankedCount", 2)])
def test_payload_cannot_claim_current_date_or_promote_itself(tmp_path, field, value):
    path, packet = _packet(tmp_path)
    packet[field] = value
    path.write_text(json.dumps(packet), encoding="utf-8")
    with pytest.raises(ResearchCandidateError): load_research_candidates(path)


def test_candidate_number_tampering_and_changed_frozen_source_are_rejected(tmp_path):
    path, packet = _packet(tmp_path)
    packet["candidates"][0]["factors"]["F10"] = "9.0"
    path.write_text(json.dumps(packet), encoding="utf-8")
    with pytest.raises(ResearchCandidateError, match="values or order"): load_research_candidates(path)
    source = tmp_path / "trial.json"
    source.write_text("{}", encoding="utf-8")
    with pytest.raises(ResearchCandidateError, match="input has changed"): load_research_candidates(path)


@pytest.mark.parametrize("change", ["identity", "name", "basis", "risks", "evidence", "source"])
def test_user_visible_identity_reasons_and_evidence_cannot_be_edited_independently(tmp_path, change):
    path, packet = _packet(tmp_path)
    row = packet["candidates"][0]
    if change == "identity": row["instrumentId"] = "SZ.000001"
    if change == "name": row["displayName"] = "平安银行"
    if change == "basis": row["basis"] = ["收入同比1000%"]
    if change == "risks": row["risks"] = []
    if change == "evidence": row["evidence"][0]["valueVerification"] = "MATCH"
    if change == "source": packet["source"]["marketCapSource"] = "fake-provider"
    path.write_text(json.dumps(packet), encoding="utf-8")
    with pytest.raises(ResearchCandidateError, match="identity, explanations or evidence"):
        load_research_candidates(path)
