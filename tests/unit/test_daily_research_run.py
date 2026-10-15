"""Daily refresh safety at the actual publication boundary, without providers."""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
import daily_research_run as daily
import daily_run
from aquant.domain.data.db import connect
from aquant.domain.data.forward_archive import ForwardArchive

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 12, 20, 31, tzinfo=CST)


def _json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture()
def calendar(tmp_path):
    first = date(2026, 1, 1)
    rows = [{"calendar_date": (first + timedelta(days=i)).isoformat(),
             "is_trading_day": "0" if (first + timedelta(days=i)).weekday() >= 5 else "1"}
            for i in range(365)]
    con = connect(tmp_path / "archive.sqlite")
    archive = ForwardArchive(con, tmp_path / "archive")
    body = json.dumps(rows).encode()
    digest, _ = archive.store_bytes(body)
    receipt = archive.record(source_id="baostock-socket",
        url="baostock://host/query_trade_dates?start_date=2026-01-01&end_date=2026-12-31",
        outcome="OK", requested_at=NOW - timedelta(days=1),
        responded_at=NOW - timedelta(days=1), content_hash=digest, byte_size=len(body))
    con.close()
    value = asdict(receipt)
    for key in ("requested_at", "responded_at", "first_seen_at"):
        value[key] = value[key].astimezone(timezone.utc).isoformat()
    summary = tmp_path / "summary.json"
    _json(summary, {"schema": "aquant.simulation_research_evidence.v1",
                    "trading_calendar": {"calendar_receipt": value, "official_raw_rows": rows}})
    return summary, archive


def test_calendar_uses_verified_bytes_to_skip_closed_day(calendar):
    summary, _ = calendar
    gate = daily.verified_calendar_day(summary, date(2026, 10, 11), now=NOW)
    assert gate["isTradingDay"] is False and gate["providerCalls"] == 0
    assert daily.verified_calendar_day(summary, date(2026, 10, 12), now=NOW)["isTradingDay"] is True


def test_calendar_rejects_modified_summary_and_future_receipt(calendar):
    summary, _ = calendar
    with pytest.raises(ValueError, match="not yet available"):
        daily.verified_calendar_day(summary, date(2026, 10, 11), now=NOW - timedelta(days=2))
    payload = json.loads(summary.read_text())
    payload["trading_calendar"]["official_raw_rows"][0]["is_trading_day"] = "0"
    _json(summary, payload)
    with pytest.raises(ValueError, match="original provider rows"):
        daily.verified_calendar_day(summary, date(2026, 10, 11), now=NOW)


def _coverage():
    return {"market_date": "2026-10-12", "quote_time_kind": "after_close_research_snapshot",
        "official_universe_count": 3, "official_supported_count": 3, "selected_count": 3,
        "captured_count": 2, "stopped_on_breaker": False,
        "batches": [{"requested_symbols": ["sh600001", "sz000001", "bj920001"], "status": "ok"}],
        "missing_by_exchange": {"BJ": 1},
        "excluded_quote_rows": [{"reason": "missing_response", "official_code": "920001"}]}


def test_quote_capture_allows_real_empty_response_but_requires_full_requests(tmp_path):
    baseline, coverage = tmp_path / "research.json", tmp_path / "coverage.json"
    _json(baseline, {"poolSize": 3})
    _json(coverage, _coverage())
    result = daily.verify_quote_capture(coverage, baseline=baseline, day="2026-10-12")
    assert result == {"officialUniverseCount": 3, "requestedCount": 3,
                      "capturedCount": 2, "missingResponseCount": 1}


@pytest.mark.parametrize("problem", ["partial", "failed", "breaker", "small_sample"])
def test_quote_capture_rejects_request_defects(tmp_path, problem):
    value = _coverage()
    if problem == "partial":
        value["batches"][0]["requested_symbols"].pop()
    elif problem == "failed":
        value["batches"][0]["status"] = "failed"
    elif problem == "breaker":
        value["stopped_on_breaker"] = True
    else:
        value["selected_count"] = 2
    _json(tmp_path / "coverage.json", value)
    _json(tmp_path / "baseline.json", {"poolSize": 3})
    with pytest.raises(ValueError, match="incomplete or failed"):
        daily.verify_quote_capture(tmp_path / "coverage.json", baseline=tmp_path / "baseline.json", day="2026-10-12")


@pytest.fixture()
def pipeline(tmp_path, monkeypatch):
    research, data = tmp_path / "research.json", tmp_path / "data"
    _json(research, {"poolSize": 3, "snapshotId": "old-research"})
    data.mkdir()
    # A minimal real pointer table allows exercising the partial-write failure.
    con = connect(data / "meta.sqlite")
    con.execute("CREATE TABLE snapshot_pointer(pointer_name TEXT PRIMARY KEY,snapshot_id TEXT,updated_at TEXT)")
    con.execute("INSERT INTO snapshot_pointer VALUES ('current','old-snapshot','old-time')")
    con.execute("CREATE TABLE research_simulation_draft(draft_id TEXT,portfolio_id TEXT,trading_day TEXT,research_content_hash TEXT,draft_json TEXT)")
    con.execute("CREATE TABLE portfolio(portfolio_id TEXT,status TEXT)")
    con.execute("CREATE TABLE position_lot(portfolio_id TEXT,instrument_id TEXT,quantity_remaining INTEGER)")
    con.close()
    _json(data / "current_snapshot.json", {"snapshot_id": "old-snapshot", "updated_at": "old-time"})
    monkeypatch.setattr(daily, "market_now", lambda: NOW)
    monkeypatch.setattr(daily, "load_research_scan", lambda *a, **kw: {"verified": True})
    monkeypatch.setattr(daily, "verified_calendar_day", lambda *a, **kw:
                        {"isTradingDay": True, "providerCalls": 0})
    argv = ["--trading-day", "2026-10-12", "--research-file", str(research),
            "--data-dir", str(data), "--artifact-root", str(tmp_path / "artifacts"),
            "--run-log", str(tmp_path / "runs.jsonl"), "--lock", str(tmp_path / "run.lock"),
            "--json-out", str(tmp_path / "result.json"), "--alerts", str(tmp_path / "alerts.jsonl")]
    calls = []

    def step(args, *, label):
        calls.append(args)
        def arg(name):
            return Path(args[args.index(name) + 1])
        if args[0].endswith("capture_research_quotes.py"):
            _json(arg("--coverage"), _coverage())
            _json(arg("--output"), {"quotes": "same immutable receipt bytes"})
        elif args[0].endswith("refresh_research_publication.py"):
            _json(arg("--output"), {"poolSize": 3, "snapshotId": "new-research", "candidates": [
                {"instrumentId": "SH.600001", "code": "600001", "displayName": "candidate",
                 "status": "RANKED", "position": 1}]})
        elif args[0].endswith("capture_research_announcements.py"):
            selected = json.loads(arg("--targets").read_text())
            _json(arg("--output"), {"target_codes": [t["code"] for t in selected],
                "window": {"start": args[args.index("--start") + 1], "end": args[args.index("--end") + 1]},
                "queryCompleteCount": len(selected), "capturedDocumentCount": 0,
                "per_code": [{"code": t["code"], "pdfFailures": []} for t in selected]})
        elif args[0].endswith("capture_research_simulation_evidence.py"):
            out = arg("--output-dir")
            _json(out / "cache.json", {"bars": {}})
            _json(out / "pool.json", {"instruments": []})
            _json(out / "summary.json", {"evidence_market_date": "2026-10-12",
                "source_inputs": {"research_candidates": {"path": str(arg("--research").resolve()),
                                                            "sha256": daily.file_hash(arg("--research"))}},
                "ready_to_publish_files": {"cache": str(out / "cache.json"), "pool": str(out / "pool.json")}})
        elif args[0].endswith("publish_universe_snapshot.py"):
            _json(arg("--json-out"), {"snapshot_id": args[args.index("--snapshot-id") + 1],
                "checks": [{"name": name, "ok": True} for name in daily_run._S1_REQUIRED_CHECKS]})
        elif args[0].endswith("run_forward_comparison.py"):
            _json(arg("--json-out"), {"results": []})
        return {"step": label, "ok": True, "seconds": 0, "exitCode": 0}
    monkeypatch.setattr(daily, "run_step", step)
    monkeypatch.setattr(daily_run, "_read_s1_capability", lambda *a, **kw: {"ok": True})
    return argv, research, data, calls, tmp_path


def test_closed_day_has_no_provider_subprocesses(pipeline, monkeypatch):
    argv, research, data, calls, root = pipeline
    before = research.read_bytes()
    monkeypatch.setattr(daily, "verified_calendar_day", lambda *a, **kw:
                        {"isTradingDay": False, "providerCalls": 0})
    assert daily.main(argv) == 0
    result = json.loads((root / "result.json").read_text())
    assert result["outcome"] == "SKIPPED"
    assert result["providerSubprocessesStarted"] == 0 and calls == []
    assert research.read_bytes() == before


def test_core_failure_never_replaces_research_or_current(pipeline, monkeypatch):
    argv, research, data, calls, root = pipeline
    previous, pointer = research.read_bytes(), (data / "current_snapshot.json").read_bytes()
    monkeypatch.setattr(daily_run, "_read_s1_capability", lambda *a, **kw: {"ok": False})
    assert daily.main(argv) == 1
    assert research.read_bytes() == previous
    assert (data / "current_snapshot.json").read_bytes() == pointer
    assert json.loads((root / "result.json").read_text())["outcome"] == "FAILED"
    assert "ERROR" in (root / "alerts.jsonl").read_text()


def test_partial_promotion_restores_both_publications(pipeline, monkeypatch):
    argv, research, data, calls, root = pipeline
    previous, pointer = research.read_bytes(), (data / "current_snapshot.json").read_bytes()
    def partial(*, data_root, snapshot_id):
        con = connect(data_root / "meta.sqlite")
        con.execute("UPDATE snapshot_pointer SET snapshot_id=?, updated_at='new-time'", (snapshot_id,))
        con.close()
        _json(data_root / "current_snapshot.json", {"snapshot_id": snapshot_id})
        raise OSError("disk failure after pointer write")
    monkeypatch.setattr(daily, "promote_universe_snapshot", partial)
    assert daily.main(argv) == 1
    assert research.read_bytes() == previous
    assert (data / "current_snapshot.json").read_bytes() == pointer
    con = connect(data / "meta.sqlite")
    assert tuple(con.execute("SELECT snapshot_id,updated_at FROM snapshot_pointer").fetchone()) == ("old-snapshot", "old-time")
    con.close()
    journal = next((root / "artifacts").glob("**/publication-commit.json"))
    assert json.loads(journal.read_text())["state"] == "ROLLED_BACK"


def test_success_requests_full_research_and_preserves_starter_pool_scope(pipeline, monkeypatch):
    argv, research, data, calls, root = pipeline
    monkeypatch.setattr(daily, "promote_universe_snapshot", lambda **kw: None)
    assert daily.main(argv) == 0
    assert json.loads(research.read_text())["snapshotId"] == "new-research"
    result = json.loads((root / "result.json").read_text())
    assert result["outcome"] == "PUBLISHED" and result["providerSubprocessesStarted"] == 3
    assert result["announcementRefresh"]["refreshed"] is True
    assert "--limit" not in calls[0]
    simulation = next(c for c in calls if c[0].endswith("capture_research_simulation_evidence.py"))
    assert "--resume" in simulation and simulation[simulation.index("--limit") + 1] == "120"
    publish = next(c for c in calls if c[0].endswith("publish_universe_snapshot.py"))
    assert "--defer-promotion" in publish
    assert (root / "run.lock").exists() is False


def test_market_cap_failure_keeps_s1_and_forward_publication(pipeline, monkeypatch):
    argv, _, _, calls, root = pipeline
    original = daily.run_step
    def failed_cap(args, *, label):
        if args[0].endswith("capture_daily_decision_market_caps.py"):
            calls.append(args)
            return {"step": label, "ok": False, "seconds": 0, "exitCode": 2}
        value = original(args, label=label)
        if args[0].endswith("capture_research_simulation_evidence.py"):
            out = Path(args[args.index("--output-dir") + 1])
            _json(out / "pool.json", {"instruments": [{"instrument_id": "SH.600001"}]})
        return value
    monkeypatch.setattr(daily, "run_step", failed_cap)
    monkeypatch.setattr(daily, "promote_universe_snapshot", lambda **kw: None)
    assert daily.main(argv) == 0
    result = json.loads((root / "result.json").read_text())
    assert result["outcome"] == "PUBLISHED"
    assert not result["marketCapRefresh"]["admitted"]
    assert result["forwardComparison"]["ok"]
    publish = next(c for c in calls if c[0].endswith("publish_universe_snapshot.py"))
    assert "no-verified-market-cap" in publish[publish.index("--market-caps") + 1]
    assert "--market-caps-archive-root" in publish


def test_missing_admitted_market_cap_checkpoint_degrades_without_recapture(tmp_path, monkeypatch):
    attempt = tmp_path / "attempt-test"
    attempt.mkdir()
    pool = attempt / "pool.json"
    _json(pool, {"instruments": [{"instrument_id": "SH.600001"}]})
    files = {"pool": pool, "sidecar": attempt / "decision-market-caps.json",
             "coverage": attempt / "decision-market-caps.coverage.json",
             "calendar": attempt / "market-cap-calendar.json"}
    for role, path in files.items():
        if role != "pool":
            _json(path, {"test": role})
    manifest = attempt / "market-cap-manifest.json"
    _json(manifest, {"schema": "aquant.daily_stage_checkpoint.v1", "stage": "MARKET_CAPS",
        "files": daily.stage_binding(files), "admitted": True, "result": {"admitted": True}})
    before = manifest.read_bytes()
    files["sidecar"].unlink()
    def no_network(*args, **kwargs):
        raise AssertionError("must not start a provider while degrading missing checkpoint")
    monkeypatch.setattr(daily, "run_step", no_network)
    record = daily.RunRecord(trading_day="2026-10-12", snapshot_id="pending",
        outcome="FAILED", source="manual", started_at=NOW.isoformat())
    extra = {"providerSubprocessesStarted": 0}
    missing = daily.refresh_daily_market_caps(attempt=attempt, pool=pool, target=NOW.date(),
        archive_root=tmp_path / "archive", alerts=tmp_path / "alerts.jsonl", record=record, extra=extra)
    assert not missing.exists() and not extra["marketCapRefresh"]["admitted"]
    assert extra["providerSubprocessesStarted"] == 0
    assert manifest.read_bytes() == before
    assert "WARNING" in (tmp_path / "alerts.jsonl").read_text()


def test_resume_recovers_interrupted_pair_before_retry(pipeline):
    _, research, data, _, root = pipeline
    attempt = root / "attempt"
    attempt.mkdir()
    old = research.read_bytes()
    (attempt / "previous-research.json").write_bytes(old)
    before = {"database": ["old-snapshot", "old-time"],
              "file": (data / "current_snapshot.json").read_text()}
    _json(research, {"poolSize": 3, "snapshotId": "new-research"})
    _json(attempt / "publication-commit.json", {"state": "PREPARED", "snapshotId": "new-snapshot",
        "previousResearchHash": daily.file_hash(attempt / "previous-research.json"),
        "stagedResearchHash": daily.file_hash(research), "previousCurrent": before})
    _json(data / "current_snapshot.json", {"snapshot_id": "new-snapshot"})
    assert daily.recover_publication(attempt, research=research, data_dir=data) is None
    assert research.read_bytes() == old
    assert json.loads((data / "current_snapshot.json").read_text())["snapshot_id"] == "old-snapshot"


def test_announcement_failure_keeps_prior_evidence_without_blocking_price_simulation(pipeline, monkeypatch):
    argv, research, data, calls, root = pipeline
    normal_step = daily.run_step
    def failed_announcements(args, *, label):
        if args[0].endswith("capture_research_announcements.py"):
            calls.append(args)
            return {"step": label, "ok": False, "seconds": 0, "exitCode": 2,
                    "tail": ["provider address not admitted"]}
        return normal_step(args, label=label)
    monkeypatch.setattr(daily, "run_step", failed_announcements)
    monkeypatch.setattr(daily, "promote_universe_snapshot", lambda **kw: None)
    assert daily.main(argv) == 0
    result = json.loads((root / "result.json").read_text())
    assert result["outcome"] == "PUBLISHED"
    assert result["announcementRefresh"]["refreshed"] is False
    assert "WARNING" in (root / "alerts.jsonl").read_text()
    ranking = next((root / "artifacts").glob("**/research-ranking.json"))
    assert research.read_bytes() == ranking.read_bytes()
    assert not any("--announcements" in command for command in calls)


def test_new_announcement_packet_cannot_replace_other_target_or_window(pipeline, monkeypatch):
    argv, research, data, calls, root = pipeline
    normal_step = daily.run_step
    def wrong_packet(args, *, label):
        step = normal_step(args, label=label)
        if args[0].endswith("capture_research_announcements.py"):
            out = Path(args[args.index("--output") + 1])
            packet = json.loads(out.read_text())
            packet["target_codes"] = ["600002"]
            _json(out, packet)
        return step
    monkeypatch.setattr(daily, "run_step", wrong_packet)
    monkeypatch.setattr(daily, "promote_universe_snapshot", lambda **kw: None)
    assert daily.main(argv) == 0
    result = json.loads((root / "result.json").read_text())
    assert not result["announcementRefresh"]["refreshed"]
    assert "targets/window" in result["announcementRefresh"]["reason"]
    assert not any("--announcements" in command for command in calls)


def test_announcement_budget_prioritizes_retained_selection_then_current_rank(tmp_path):
    source = tmp_path / "ranking.json"
    _json(source, {"candidates": [
        {"instrumentId": "SH.600003", "code": "600003", "displayName": "held",
         "status": "OBSERVATION", "position": None},
        {"instrumentId": "SH.600002", "code": "600002", "displayName": "second",
         "status": "RANKED", "position": 2},
        {"instrumentId": "SH.600001", "code": "600001", "displayName": "first",
         "status": "RANKED", "position": 1}]})
    targets = daily.announcement_targets(source, {"instrument_ids": ["SH.600003", "SH.600001"]}, limit=2)
    assert [row["code"] for row in targets] == ["600003", "600001"]


def test_complete_simulation_package_reuses_only_admitted_exact_selection(tmp_path, monkeypatch):
    from aquant.operations import simulation_evidence
    research, retained = tmp_path / "research.json", tmp_path / "retained.json"
    _json(research, {"candidates": [{"instrumentId": "SH.600001", "board": "MAIN", "status": "RANKED"}]})
    _json(retained, {"instrument_ids": []})
    summary, cache, pool = tmp_path / "summary.json", tmp_path / "cache.json", tmp_path / "pool.json"
    _json(cache, {})
    _json(pool, {})
    packet = {"evidence_market_date": "2026-10-12", "failures": [],
        "source_inputs": {"research_candidates": {"path": str(research.resolve()), "sha256": daily.file_hash(research)}},
        "ready_to_publish_files": {"cache": str(cache), "pool": str(pool)},
        "instruments": [{"instrument_id": "SH.600001"}]}
    _json(summary, packet)
    admissions = []
    monkeypatch.setattr(simulation_evidence, "admit_simulation_evidence", lambda *a, **kw: admissions.append((a, kw)))
    assert daily.reuse_complete_simulation_evidence(summary, research=research, day="2026-10-12", limit=120, retained=retained)
    assert len(admissions) == 1 and admissions[0][1]["cutoff"].tzinfo is not None
    packet["instruments"] = [{"instrument_id": "SH.600002"}]
    _json(summary, packet)
    with pytest.raises(ValueError, match="retained selection"):
        daily.reuse_complete_simulation_evidence(summary, research=research, day="2026-10-12", limit=120, retained=retained)
    assert len(admissions) == 1
    packet["failures"] = [{"stage": "history"}]
    _json(summary, packet)
    assert not daily.reuse_complete_simulation_evidence(summary, research=research, day="2026-10-12", limit=120, retained=retained)
    assert len(admissions) == 1


def test_forward_tracking_failure_does_not_undo_admitted_market_publication(pipeline, monkeypatch):
    argv, research, data, calls, root = pipeline
    normal = daily.run_step
    def broken_tracking(args, *, label):
        if args[0].endswith("run_forward_comparison.py"):
            calls.append(args)
            raise OSError("forward subprocess unavailable")
        return normal(args, label=label)
    monkeypatch.setattr(daily, "run_step", broken_tracking)
    monkeypatch.setattr(daily, "promote_universe_snapshot", lambda **kw: None)
    assert daily.main(argv) == 0
    result = json.loads((root / "result.json").read_text())
    assert result["outcome"] == "PUBLISHED" and not result["forwardComparison"]["ok"]
    assert json.loads(research.read_text())["snapshotId"] == "new-research"
    assert "WARNING" in (root / "alerts.jsonl").read_text()


def test_pending_forward_common_pool_retained_before_account_has_holdings(pipeline, monkeypatch):
    from aquant.operations.forward_comparison import ForwardComparisonStore
    _, _, data, _, root = pipeline
    series = {"seriesId": "fwd-" + "a" * 24, "firstTradingDay": "2026-10-12",
              "commonPool": ["SH.600011", "SZ.000012"], "observedSessionCount": 0}
    spec_file = data / 'forward-comparison' / (series['seriesId'] + '.spec.json')
    _json(spec_file, {'explicit': 'local retention contract fixture'})
    monkeypatch.setattr(ForwardComparisonStore, 'list_series', lambda self: [series])
    monkeypatch.setattr(ForwardComparisonStore, 'spec', lambda self, sid: series)
    kept = daily.retained_instruments(data, target=date(2026, 10, 12))
    assert kept['instrument_ids'] == series['commonPool']
    for iid in kept['instrument_ids']:
        reason = kept['reasons'][iid][0]
        assert reason['kind'] == 'FORWARD_COMPARISON_COMMON_POOL'
        assert reason['spec_content_hash'] == daily.file_hash(spec_file)
    series['observedSessionCount'] = 1
    assert daily.retained_instruments(data, target=date(2026, 10, 12))['instrument_ids'] == []


def test_saved_simulation_summary_must_bind_staged_research(tmp_path):
    research = tmp_path / "research.json"
    _json(research, {"snapshotId": "new"})
    summary = tmp_path / "summary.json"
    _json(summary, {"evidence_market_date": "2026-10-12", "source_inputs": {
        "research_candidates": {"path": str(research.resolve()), "sha256": "sha256:wrong"}}})
    with pytest.raises(ValueError, match="this staged research"):
        daily.publication_files(summary, research=research, day="2026-10-12")


def test_retry_reuses_successful_quotes_and_research_sha(pipeline, monkeypatch):
    argv, research, data, calls, root = pipeline
    original = daily.run_step
    def failed_evidence(args, *, label):
        if args[0].endswith("capture_research_simulation_evidence.py"):
            calls.append(args)
            return {"step": label, "ok": False, "seconds": 0, "exitCode": 1}
        return original(args, label=label)
    monkeypatch.setattr(daily, "run_step", failed_evidence)
    assert daily.main(argv) == 1
    staged = next((root / "artifacts").glob("**/research-candidates.json"))
    prior_sha = daily.file_hash(staged)
    calls.clear()
    monkeypatch.setattr(daily, "run_step", original)
    monkeypatch.setattr(daily, "promote_universe_snapshot", lambda **kw: None)
    assert daily.main(argv) == 0
    assert daily.file_hash(staged) == prior_sha
    assert [Path(c[0]).name for c in calls] == ["capture_research_simulation_evidence.py", "publish_universe_snapshot.py",
                                             "run_forward_comparison.py"]
    result = json.loads((root / "result.json").read_text())
    assert result["providerSubprocessesStarted"] == 1
    assert all(s["reusedVerifiedCheckpoint"] for s in result["steps"][1:3])


def test_modified_successful_quote_stage_fails_before_any_resume_provider(pipeline, monkeypatch):
    argv, research, data, calls, root = pipeline
    monkeypatch.setattr(daily_run, "_read_s1_capability", lambda *a, **kw: {"ok": False})
    assert daily.main(argv) == 1
    quotes = next((root / "artifacts").glob("**/closing-quotes.json"))
    _json(quotes, {"changed": True})
    calls.clear()
    assert daily.main(argv) == 1
    assert calls == []
    assert "inputs changed" in json.loads((root / "result.json").read_text())["reason"]


def test_retention_includes_saved_choices_and_active_holdings_not_comparisons(pipeline):
    _, _, data, _, _ = pipeline
    con = connect(data / "meta.sqlite")
    for pid, status in [("pf-user", "ACTIVE"), ("pf-cmp-run-m", "ACTIVE"), ("pf-closed", "CLOSED")]:
        con.execute("INSERT INTO portfolio VALUES (?,?)", (pid, status))
    for pid, iid, q in [("pf-user", "SH.600002", 100), ("pf-user", "SH.600004", 0),
                        ("pf-cmp-run-m", "SH.600005", 100), ("pf-closed", "SH.600006", 100)]:
        con.execute("INSERT INTO position_lot VALUES (?,?,?)", (pid, iid, q))
    selected = ["SZ.000001", "SZ.300001"]
    document = {"portfolioId": "pf-user", "tradingDay": "2026-10-12", "researchContentHash": "sha256:old",
                "selectedInstrumentIds": selected, "selection": {"selectedInstrumentIds": selected}}
    con.execute("INSERT INTO research_simulation_draft VALUES (?,?,?,?,?)",
                ("draft-current", "pf-user", "2026-10-12", "sha256:old", json.dumps(document)))
    # Past selections alone do not indefinitely retain disposed securities.
    con.execute("INSERT INTO research_simulation_draft VALUES (?,?,?,?,?)",
                ("draft-past", "pf-user", "2026-10-09", "sha256:old", "{}"))
    con.close()
    result = daily.retained_instruments(data, target=date(2026, 10, 12))
    assert result["instrument_ids"] == ["SH.600002", "SZ.000001"]
    assert result["reasons"]["SZ.000001"][0]["draft_id"] == "draft-current"
    assert result["skipped_instruments"][0]["instrument_id"] == "SZ.300001"


def test_new_retained_security_uses_new_attempt_preserving_old_package(pipeline, monkeypatch):
    argv, research, data, calls, root = pipeline
    monkeypatch.setattr(daily_run, "_read_s1_capability", lambda *a, **kw: {"ok": False})
    assert daily.main(argv) == 1
    old = next((root / "artifacts").glob("**/retained-instruments.json"))
    old_hash = daily.file_hash(old)
    con = connect(data / "meta.sqlite")
    con.execute("INSERT INTO portfolio VALUES ('pf-user','ACTIVE')")
    con.execute("INSERT INTO position_lot VALUES ('pf-user','SH.600009',100)")
    con.close()
    calls.clear()
    assert daily.main(argv) == 1
    retention_files = list((root / "artifacts").glob("**/retained-instruments.json"))
    assert len(retention_files) == 2 and daily.file_hash(old) == old_hash
    new = next(p for p in retention_files if p != old)
    assert json.loads(new.read_text())["instrument_ids"] == ["SH.600009"]
    capture = next(c for c in calls if c[0].endswith("capture_research_simulation_evidence.py"))
    assert Path(capture[capture.index("--retain-instruments") + 1]) == new


def test_financial_cache_and_receipt_reach_the_same_snapshot_publication(pipeline, monkeypatch):
    argv, _research, _data, calls, root = pipeline
    cache, evidence = root / 'new-profit-cache.json', root / 'profit-receipts.json'
    _json(cache, {'statements': {}})
    _json(evidence, {'schema': 'aquant.simulation-financial-evidence.v1'})
    monkeypatch.setattr(daily, 'promote_universe_snapshot', lambda **kw: None)
    assert daily.main([*argv, '--financials', str(cache), '--financial-evidence', str(evidence)]) == 0
    command = next(c for c in calls if c[0].endswith('publish_universe_snapshot.py'))
    assert command[command.index('--financials') + 1] == str(cache)
    assert command[command.index('--financial-evidence') + 1] == str(evidence)
    result = json.loads((root / 'result.json').read_text())
    assert result['optionalFinancialEvidence'] == str(evidence.resolve())


def test_formal_s2_retry_reuses_frozen_target_source_when_progress_map_changes(tmp_path, monkeypatch):
    from aquant.operations.s2_readiness import load_s2_target_period_map
    source = tmp_path / "current-target-map.json"
    _json(source, {"schema": "aquant.s2_target_periods.v1",
        "source_scope_ids": ["SH.600000"], "latest_period_ends": {"SH.600000": "2026-06-30"},
        "records": [{"instrument_id": "SH.600000", "latest_period_end": "2026-06-30"}],
        "source_artifacts": []})
    attempt = tmp_path / "attempt"
    commands = []
    def report_step(command, *, label):
        commands.append(command)
        frozen = Path(command[command.index("--target-period-map") + 1])
        assert load_s2_target_period_map(frozen)["latest_period_ends"] == {"SH.600000": "2026-06-30"}
        _json(Path(command[command.index("--json-out") + 1]),
              {"snapshot_id": "snap-fixture", "scope_status": "CLOSED", "pool_size": 1})
        return {"step": label, "ok": True}
    monkeypatch.setattr(daily, "run_step", report_step)
    record = daily.RunRecord(trading_day=NOW.date().isoformat(), snapshot_id="snap-fixture",
        outcome="PUBLISHED", source="manual", started_at=NOW.isoformat())
    extra = {}
    kwargs = dict(data_dir=tmp_path, snapshot_id="snap-fixture", facts_db=tmp_path / "facts.sqlite",
        archive_root=tmp_path / "archive", attempt=attempt, record=record,
        extra=extra, alerts=tmp_path / "alerts.jsonl", target_period_map=source)
    daily.report_daily_formal_s2(**kwargs)
    frozen_body = (attempt / "s2-target-period-map.json").read_bytes()
    assert extra["formalS2"]["ok"]
    source.write_text("changed while capture advances", encoding="utf-8")
    daily.report_daily_formal_s2(**kwargs)
    assert extra["formalS2"]["ok"] and len(commands) == 2
    assert (attempt / "s2-target-period-map.json").read_bytes() == frozen_body
