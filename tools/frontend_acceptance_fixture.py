"""Serve the real web/API flow on isolated, explicitly synthetic acceptance data.

Run after ``npm --prefix apps/web run build``::

    python tools/frontend_acceptance_fixture.py --port 8877
    python tools/frontend_acceptance_fixture.py --self-check

The printed directory is newly allocated by default. ``--data-dir`` can resume
only a directory bearing this fixture's marker; an existing unmarked directory
is refused. This process never uses deploy/local-trial or provider credentials.
The HTML banner and every fixture candidate explicitly identify synthetic data.

The production research path requires production-shaped input, so only this
isolated process wraps reader refs/eligibility and freezes the selection clock.
Actual snapshot rows remain SYNTHETIC. Orders, confirmation tokens, decisions,
fills, valuations and reconciliation use the unchanged product endpoints.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "apps/api"))

LABEL = "隔离本地验收 · 全部为合成测试数据 · 无真实供应商"
EXECUTION_ID = "snap-isolated-acceptance-execution"
RESEARCH_ID = "research-isolated-acceptance-v1"
PORTFOLIO_ID = "pf-user-sim"
USER = {"X-Aquant-Subject": "user:demo"}
IIDS = ("SYN.A.000001", "SYN.A.600519")
MARKER = ".frontend-acceptance-fixture.json"


def _prepare_directory(value: Path | None) -> Path:
    root = value.resolve() if value else Path(tempfile.mkdtemp(prefix="aquant-frontend-acceptance-"))
    root.mkdir(parents=True, exist_ok=True)
    marker = root / MARKER
    if marker.exists():
        if json.loads(marker.read_text(encoding="utf-8")).get("fixture") != RESEARCH_ID:
            raise RuntimeError("This directory has a different acceptance marker")
    elif any(root.iterdir()):
        raise RuntimeError("Refusing existing nonempty unmarked directory: " + str(root))
    else:
        marker.write_text(json.dumps({"fixture": RESEARCH_ID, "label": LABEL}, ensure_ascii=False), encoding="utf-8")
    return root


def _packet() -> dict:
    candidates = []
    names = ("合成示例·银行（隔离验收）", "合成示例·消费龙头（隔离验收）")
    for position, (iid, name) in enumerate(zip(IIDS, names), 1):
        candidates.append({
            "instrumentId": iid, "code": iid.rsplit(".", 1)[-1], "displayName": name,
            "exchange": "SZSE" if position == 1 else "SSE", "board": "MAIN",
            "industry": "合成银行" if position == 1 else "合成食品饮料",
            "status": "RANKED", "position": position, "signalRank": 1 - position / 4,
            "factors": {key: None for key in ("F03", "F04", "F05", "F06", "F07", "F08", "F09", "F10")},
            "factorRanks": {}, "basis": [LABEL, "固定历史样例用于核验选择与模拟流程，不构成真实研究结论。"],
            "risks": ["合成价格、行业与资格，仅供隔离验收。"], "evidence": [],
            "basisEvidence": [], "counterEvidence": [], "uncertainties": ["未连接真实供应商或财报。"],
            "exclusionReason": None, "financialBridge": [],
        })
    return {
        "schema": "aquant.research-candidates.v2", "snapshotId": RESEARCH_ID,
        "generatedAt": "2026-09-11T20:00:00+08:00", "dataAsOf": "2026-09-11",
        "asOfTime": "2026-09-11T20:00:00+08:00", "capturedFrom": "2026-09-11T19:00:00+08:00",
        "capturedTo": "2026-09-11T19:00:00+08:00", "quoteMode": "CLOSING_RESEARCH",
        "strategy": "S2_RESEARCH", "pitMode": "HISTORICAL_RECONSTRUCTED",
        "researchOnly": True, "simulationAvailable": False,
        "poolSize": 2, "rankedCount": 2, "excludedCount": 0, "returnedCount": 2,
        "filteredCount": 2, "offset": 0, "nextOffset": None, "exclusions": [],
        "comparisonScope": LABEL, "rankingFormula": "固定合成样例排序，无真实因子排名。",
        "scope": {"universeCodeCount": 2, "observedCount": 2, "quoteRejectedCount": 0,
            "financialCount": 0, "unknownIndustryCount": 0,
            "byBoard": [{"board": "MAIN", "requested": 2, "observed": 2, "ranked": 2}]},
        "source": {"factorReport": "isolated-local-fixture", "marketCapSource": "isolated-synthetic-fixture",
            "marketCapAsOf": "2026-09-11", "archiveVerified": False, "placeholderUsed": False},
        "limitations": [LABEL, "实际数据库快照为 SYNTHETIC；仅进程内包装输入以验收生产研究路径。"],
        "candidates": candidates,
    }


def build_fixture(data_dir: Path | None = None):
    """Return a loopback-only test app, with no browser or background process."""
    root = _prepare_directory(data_dir)
    app_dir = root / "app"
    artifact = root / "research-fixture.json"
    if not artifact.exists():
        artifact.write_text(json.dumps(_packet(), ensure_ascii=False, sort_keys=True), encoding="utf-8")
    # main's import constructs an app. Make that import isolated too, and reuse
    # its connection instead of accidentally creating a production data state.
    os.environ.update({"AQUANT_DATA_DIR": str(app_dir), "AQUANT_SNAPSHOT_ID": "snap-syn-001",
        "AQUANT_IDENTITY_MODE": "LOCAL_LOOPBACK_DEMO", "AQUANT_TRIAL_SUBJECT": "user:demo",
        "AQUANT_RESEARCH_CANDIDATES_FILE": str(artifact), "AQUANT_RESET_DATA": "0",
        "AQUANT_DAILY_RUN_LOG": str(root / "no-provider-daily-run.json"),
        "AQUANT_LIVE_FINANCIAL_FACTS_DB": str(root / "no-provider-financial-facts.sqlite"),
        "AQUANT_FORWARD_ARCHIVE_ROOT": str(root / "no-provider-forward-archive")})
    os.environ.pop("AQUANT_S2_TARGET_PERIOD_MAP", None)
    import main
    from aquant.domain.data.db import write_tx
    from aquant.domain.data.snapshot import SnapshotCapability
    from aquant.domain.portfolio.construction import ConstructionParams
    from aquant.domain.portfolio.plan import PlanError
    from aquant.domain.simulation.fees import FeeSchedule, FeeTable
    from aquant.operations import research_simulation as operations
    from aquant.operations.research_candidates import file_hash

    state = main.app.state.aquant
    if state.data_dir.resolve() != app_dir.resolve():
        # Programmatic self-check callers can have imported main already.
        state = main.build_state(app_dir)

    class FixedSelectionClock(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime.fromisoformat("2026-09-11T21:00:00+08:00")
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    operations.datetime = FixedSelectionClock
    state.params = ConstructionParams(max_holdings=4, exclude_listing_days=1,
        liquidity_lookback_days=3, liquidity_min_avg_amount_cents=None)
    fees = FeeTable([FeeSchedule("isolated-synthetic-acceptance-fee", date(2023, 8, 28), None,
        Decimal("0.00025"), 500, Decimal("0.0005"), Decimal("0.00001"))],
        commission_source="USER_APPROVED_ASSUMPTION")
    state.fees = state.service.fee_table = fees
    state.service.params = state.params
    main.resolve_fee_table = lambda snapshot_id: fees
    def no_model_provider():
        raise PlanError("DATA_NOT_READY", "isolated acceptance fixture has no model provider",
                        "isolated-fixture", "Use the local research and simulation acceptance flow.")
    state.provider = no_model_provider

    def load_local_research(path):
        source = json.loads(path.read_text(encoding="utf-8"))
        return source, {"researchSnapshotId": source["snapshotId"], "researchContentHash": file_hash(path),
            "researchObservedAt": "2026-09-11T19:00:00+08:00", "researchAvailableAt": "2026-09-11T20:00:00+08:00",
            "researchDataAsOf": "2026-09-11", "researchOriginPath": str(path.resolve())}

    def load_local_scan(path, *, limit=20, offset=0, q="", board="", industry="", view="candidates"):
        packet = json.loads(path.read_text(encoding="utf-8"))
        rows = [row for row in packet["candidates"]
            if (not q or q.casefold() in " ".join((row["instrumentId"], row["code"], row["displayName"])).casefold())
            and (not board or row["board"] == board) and (not industry or industry in row["industry"])
            and view != "financial"]
        packet.update(candidates=rows[offset:offset + limit], filteredCount=len(rows), offset=offset,
            returnedCount=len(rows[offset:offset + limit]), nextOffset=offset + limit if offset + limit < len(rows) else None)
        return packet

    main.load_simulation_research = load_local_research
    main.load_research_scan = load_local_scan
    original_ref = state.reader.ref
    original_instruments = state.reader.instruments
    original_calendar = state.reader.trading_calendar
    original_published = state.reader.published_snapshot
    state.reader.ref = lambda sid: replace(original_ref(sid), data_mode="PRODUCTION", watermark=LABEL)

    def instruments(sid, *, as_of):
        rows = deepcopy(original_instruments(sid, as_of=as_of))
        execution = sid == EXECUTION_ID
        for row in rows:
            stamp = "2026-09-14T15:00:00+08:00" if execution else "2026-09-11T19:00:00+08:00"
            row["simulation_eligibility"] = {"status": "VERIFIED", "as_of_date": "2026-09-14" if execution else "2026-09-11",
                "available_at": stamp, "observed_at": stamp, "is_st": False, "is_suspended": False,
                "sources": [{"source_id": "isolated-synthetic-fixture", "receipt_id": "not-provider-evidence",
                    "content_hash": "sha256:" + "b" * 64}]}
        return rows

    state.reader.instruments = instruments
    state.reader.trading_calendar = lambda sid, **kwargs: sorted(set([*original_calendar(sid, **kwargs), "2026-09-14"]))
    state.reader.published_snapshot = lambda sid: replace(original_published(sid), data_mode="PRODUCTION",
        s1_decision=SnapshotCapability(sid == main.SNAPSHOT_ID, "ISOLATED_FIXTURE", LABEL))

    # Give execution real local, hash-checked dataset files. The synthetic input
    # day is fixed; no external prices or provider receipts are fabricated.
    if state.con.execute("SELECT 1 FROM snapshot WHERE snapshot_id=?", (EXECUTION_ID,)).fetchone() is None:
        state.con.execute("INSERT INTO snapshot(snapshot_id,kind,data_mode,status,input_cutoff_at,as_of_time,published_at,created_at,"
            "code_version,data_version,quality_status,watermark) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (EXECUTION_ID, "EOD", "SYNTHETIC", "DRAFT", "2026-09-14T15:00:00+08:00", "2026-09-14T15:00:00+08:00",
             "2026-09-14T16:00:00+08:00", "2026-09-14T16:00:00+08:00", "isolated-fixture", "isolated-fixture", "OK", LABEL))
        for dataset in state.store.datasets(main.SNAPSHOT_ID):
            contents = json.loads((state.store.root / dataset["path"]).read_text(encoding="utf-8"))
            if dataset["name"] == "daily_quotes":
                last = {row["instrument_id"]: row for row in contents}
                contents = []
                for row in last.values():
                    price = int(row["close_cents"])
                    contents.append({**row, "trading_day": "2026-09-14", "prev_close_cents": price,
                        "open_cents": price, "high_cents": price + 10, "low_cents": max(1, price - 10),
                        "close_cents": price + 5, "adjusted_close_cents": price + 5, "board_limit_up": False})
            elif dataset["name"] == "trading_calendar":
                contents = [*contents, "2026-09-14"]
            target = state.store.root / "datasets" / EXECUTION_ID / (dataset["name"] + ".json")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(contents, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            state.con.execute("INSERT INTO snapshot_dataset(snapshot_id,name,path,sha256,record_count,as_of_upper_bound,coverage_ratio) "
                "VALUES (?,?,?,?,?,?,?)", (EXECUTION_ID, dataset["name"], target.relative_to(state.store.root).as_posix(),
                 "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest(), len(contents), "2026-09-14T15:00:00+08:00", 1.0))

    # Register controls before the static catch-all mount.
    original_mount = main._mount_web
    main._mount_web = lambda app: None
    try:
        app = main.create_app(state=state)
    finally:
        main._mount_web = original_mount
    from fastapi import Header, HTTPException
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles

    def facts():
        return {"label": LABEL, "dataDir": str(root), "decisionSnapshotId": main.SNAPSHOT_ID,
            "executionSnapshotId": EXECUTION_ID, "researchSnapshotId": RESEARCH_ID,
            "executionPublished": state.con.execute("SELECT status FROM snapshot WHERE snapshot_id=?", (EXECUTION_ID,)).fetchone()[0] == "PUBLISHED",
            "portfolioId": PORTFOLIO_ID, "subject": "user:demo",
            "clock": "2026-09-11T21:00:00+08:00", "executionDay": "2026-09-14",
            "counts": {table: state.con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in ("research_simulation_draft", "simulation_plan", "decision_log", "order", "fill", "cash_entry", "valuation")}}

    @app.get("/__acceptance__/state")
    def acceptance_state():
        return facts()

    @app.post("/__acceptance__/publish-execution")
    def publish_execution(x_aquant_subject: str | None = Header(default=None)):
        if x_aquant_subject != "user:demo":
            raise HTTPException(status_code=403, detail="isolated fixture subject required")
        with write_tx(state.con):
            state.con.execute("UPDATE snapshot SET status='PUBLISHED' WHERE snapshot_id=? AND status='DRAFT'", (EXECUTION_ID,))
        return facts()

    index = ROOT / "apps/web/dist/index.html"
    if index.exists():
        @app.get("/", response_class=HTMLResponse)
        def frontend():
            fixture_style = '<style>body{padding-top:60px!important}.topbar{top:60px!important}</style>'
            banner = ('<div role="note" style="background:#713f12;color:white;padding:8px 16px;'
                'font:14px/20px sans-serif;position:fixed;top:0;left:0;right:0;'
                'height:60px;box-sizing:border-box;display:flex;align-items:center;z-index:9999">' + LABEL +
                ' · 固定测试时点 2026-09-11 → 2026-09-14 · 独立模拟账本</div>')
            return index.read_text(encoding="utf-8").replace("<body>", "<body>" + fixture_style + banner, 1)
        app.mount("/", StaticFiles(directory=str(index.parent), html=True), name="acceptance-web")
    app.state.acceptance_root = root
    app.state.acceptance_label = LABEL
    return app


def self_check(app):
    """Exercise the real HTTP routes without a browser or provider."""
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        def get(path, **kwargs):
            response = client.get(path, headers=USER, **kwargs)
            assert response.status_code == 200, (path, response.status_code, response.text)
            return response.json()
        def post(path, body=None):
            response = client.post(path, headers=USER, json=body)
            assert response.status_code == 200, (path, response.status_code, response.text)
            return response.json()
        get("/api/v1/status")
        get("/api/v1/candidates")
        get("/api/v1/events")
        get("/api/v1/snapshots")
        assert get("/api/v1/research/candidates")["candidates"][0]["instrumentId"] == IIDS[0]
        context = get("/api/v1/research/simulation/context")
        assert context["eligibleCandidateCount"] == 2, context
        initial = get("/__acceptance__/state")
        assert initial["counts"]["fill"] == 0, initial
        body = {"portfolioId": PORTFOLIO_ID, "researchSnapshotId": context["researchSnapshotId"],
            "researchContentHash": context["researchContentHash"], "decisionSnapshotId": context["decisionSnapshotId"],
            "decisionCutoffAt": context["decisionCutoffAt"], "tradingDay": context["nextTradingDay"],
            "selectedInstrumentIds": [IIDS[0]], "selectionReason": "隔离验收：保留原理由",
            "excludedReasons": {IIDS[1]: "隔离验收未纳入"}, "initialCashCents": 100_000_000}
        saved = post("/api/v1/research/simulation/draft", body)
        assert saved["status"] == "WAITING_EXECUTION_SNAPSHOT" and not saved["planId"]
        post("/__acceptance__/publish-execution")
        reviewed = post(f"/api/v1/research/simulation/drafts/{saved['draftId']}/preview", {"executionSnapshotId": EXECUTION_ID})
        assert reviewed["status"] == "READY_FOR_REVIEW" and reviewed["confirmationAvailable"]
        temporary = reviewed["planId"]
        token = post(f"/api/v1/plans/{temporary}/confirmation")["confirmationToken"]
        frozen = post(f"/api/v1/plans/{temporary}/freeze", {"plan_id": temporary, "confirmation_token": token})
        plan_id = frozen["plan_id"]
        assert get("/__acceptance__/state")["counts"]["fill"] == 0
        executed = post(f"/api/v1/plans/{plan_id}/execute", {"plan_id": plan_id, "corporate_actions": []})
        assert executed["fills"], executed
        valuation = post("/api/v1/valuations", {"portfolio_id": PORTFOLIO_ID, "snapshot_id": EXECUTION_ID,
            "trading_day": "2026-09-14", "execution_plan_id": plan_id})
        assert valuation["published"], valuation
        reconciliation = get(f"/api/v1/portfolios/{PORTFOLIO_ID}/reconcile")
        assert reconciliation["reconciled"], reconciliation
        ledger_before = get(f"/api/v1/portfolios/{PORTFOLIO_ID}/ledger")
        original = get("/api/v1/research/simulation/drafts")["drafts"][0]
        assert original["draftId"] == saved["draftId"] and original["status"] == "WAITING_EXECUTION_SNAPSHOT"
        restored = get("/api/v1/plans", params={"portfolio_id": PORTFOLIO_ID})["plans"][0]
        assert restored["planId"] == plan_id and restored["status"] == "EXECUTED"
        before = get("/__acceptance__/state")["counts"]
        post("/api/v1/decisions", {"portfolio_id": PORTFOLIO_ID, "snapshot_id": context["decisionSnapshotId"],
            "decision_type": "NO_CHANGE", "human_final": {"action": "NO_CHANGE", "orders": []},
            "reason_note": "隔离验收：继续观察", "external_information_used": False})
        after = get("/__acceptance__/state")["counts"]
        for table in ("order", "fill", "cash_entry", "valuation", "simulation_plan"):
            assert before[table] == after[table], (table, before, after)
        assert get(f"/api/v1/portfolios/{PORTFOLIO_ID}/ledger") == ledger_before
        assert len(get("/api/v1/decisions", params={"portfolio_id": PORTFOLIO_ID})["decisions"]) == 2
        print(json.dumps({"selfCheck": "PASS", "planId": plan_id, "fills": len(executed["fills"]),
            "valuationPublished": valuation["published"], "reconciled": reconciliation["reconciled"],
            **get("/__acceptance__/state")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="new empty directory, or previously marked fixture directory")
    parser.add_argument("--port", type=int, default=8877)
    parser.add_argument("--self-check", action="store_true", help="real API route check without starting a server")
    args = parser.parse_args()
    app = build_fixture(args.data_dir)
    print(f"{LABEL}\nData: {app.state.acceptance_root}\nURL: http://127.0.0.1:{args.port}", flush=True)
    if args.self_check:
        self_check(app)
    else:
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=True)
