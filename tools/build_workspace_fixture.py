"""生成工作台夹具：用真实的领域链路产出前端数据。

这不是 mock：它跑完整的
    源清单 -> 入库 -> 快照发布 -> 研究读取 -> 组合构建 -> 草稿预览
链路，然后把视图模型序列化给前端。前端因此展示的是真实计算结果，
而不是手写的假数据。

用法：
    python tools/build_workspace_fixture.py --out apps/web/public/workspace.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "integration"))

from aquant.application.workspace_view import (                      # noqa: E402
    build_data_status, build_draft_vm, build_research_card,
)
from aquant.domain.data.db import apply_migrations, connect          # noqa: E402
from aquant.domain.data.ingest import SnapshotBuilder                # noqa: E402
from aquant.domain.data.reader import SnapshotReader                 # noqa: E402
from aquant.domain.data.snapshot import SnapshotStore                # noqa: E402
from aquant.domain.portfolio.construction import (                   # noqa: E402
    Candidate, ConstructionParams,
)
from aquant.domain.portfolio.plan import PlanService                 # noqa: E402
from aquant.domain.simulation.fees import synthetic_fee_table        # noqa: E402
from aquant.domain.simulation.simulator import Bar                   # noqa: E402
from test_m1_ingest_e2e import build_snapshot                        # noqa: E402

SNAPSHOT = "snap-syn-001"
AS_OF = datetime(2026, 9, 11, 12, 30, tzinfo=timezone.utc)
TRADING_DAY = date(2026, 9, 8)

LISTINGS = {
    "SYN.A.600519": ("SSE", "MAIN"),
    "SYN.A.000001": ("SZSE", "MAIN"),
    "SYN.A.600003": ("SSE", "MAIN"),
    "SYN.A.300001": ("SZSE", "GEM"),
    "SYN.A.600002": ("SSE", "MAIN"),
}


def _bar(reader: SnapshotReader, iid: str, day: date) -> Bar | None:
    rows = reader.daily_quotes(SNAPSHOT, as_of=AS_OF, instrument_id=iid, end=day)
    m = next((r for r in rows if r.trading_day == day), None)
    if m is None:
        return None
    return Bar(instrument_id=iid, trading_day=day, open_cents=m.open_cents,
               high_cents=m.high_cents, low_cents=m.low_cents,
               close_cents=m.close_cents,
               prev_close_cents=m.prev_close_cents or m.open_cents,
               volume_shares=m.volume_shares, board_limit_up=m.board_limit_up)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "apps" / "web" / "public" / "workspace.json"))
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp())
    con = connect(tmp / "meta.sqlite")
    apply_migrations(con)
    root = tmp / "api"
    root.mkdir()
    store = SnapshotStore(con, root)
    builder = SnapshotBuilder(con, root / "datasets")
    build_snapshot(con, builder, store)
    reader = SnapshotReader(store)

    fees = synthetic_fee_table()
    params = ConstructionParams(max_holdings=4, max_single_name_pct=Decimal("20"),
                                max_single_industry_pct=Decimal("50"))
    svc = PlanService(con, reader, fees, _rules(), LISTINGS, params)

    # --- 顶栏数据状态 ---
    status = build_data_status(reader, SNAPSHOT).as_dict()

    # --- 研究候选（含一个不可模拟的停牌标的与一个非主板标的，用于展示限制态）---
    candidates = [
        Candidate("SYN.A.600519", "SW_SYN_01", 0.90),
        Candidate("SYN.A.000001", "SW_SYN_02", 0.70),
        Candidate("SYN.A.600003", "SW_SYN_05", 0.55),
    ]
    factor_map = {
        "SYN.A.600519": [
            {"factor_id": "F01", "name": "20日动量", "value": 0.0412, "unit": "ratio",
             "rank_pct": 0.71, "coverage": 1.0, "contribution": 0.355},
            {"factor_id": "F04", "name": "20日波动率", "value": 0.2287, "unit": "annualized",
             "rank_pct": 0.38, "coverage": 1.0, "contribution": 0.190},
        ],
        "SYN.A.000001": [
            {"factor_id": "F01", "name": "20日动量", "value": -0.0155, "unit": "ratio",
             "rank_pct": 0.22, "coverage": 1.0, "contribution": 0.110},
            {"factor_id": "F04", "name": "20日波动率", "value": 0.1402, "unit": "annualized",
             "rank_pct": 0.83, "coverage": 1.0, "contribution": 0.415},
        ],
    }

    cards = []
    for c in candidates:
        bar = _bar(reader, c.instrument_id, TRADING_DAY)
        cards.append(build_research_card(
            reader, snapshot_id=SNAPSHOT, as_of=AS_OF,
            instrument_id=c.instrument_id, trading_day=TRADING_DAY,
            board_rules=_rules(), listings=LISTINGS, bar=bar,
            factor_values=factor_map.get(c.instrument_id, []),
        ).as_dict())

    # 停牌与创业板的限制态各来一张，让界面能展示"不能交易的原因"
    for iid, day in (("SYN.A.600002", date(2026, 9, 10)), ("SYN.A.300001", TRADING_DAY)):
        bar = _bar(reader, iid, day)
        cards.append(build_research_card(
            reader, snapshot_id=SNAPSHOT, as_of=AS_OF, instrument_id=iid,
            trading_day=day, board_rules=_rules(), listings=LISTINGS, bar=bar,
            factor_values=[],
        ).as_dict())

    # --- 候选表 ---
    rows = []
    for c in candidates:
        bar = _bar(reader, c.instrument_id, TRADING_DAY)
        card = next(k for k in cards if k["instrumentId"] == c.instrument_id)
        rows.append({
            "instrumentId": c.instrument_id,
            "displayName": card["displayName"],
            "signalRank": c.signal_rank,
            "industryCode": c.industry_code,
            "basis": "、".join(f["name"] for f in card["rankBreakdown"]) or "—",
            "counterEvidence": (card["counterEvidence"][0]["statement"]
                                if card["counterEvidence"] else "未找到反证"),
            "dataQuality": status["readinessLabel"],
            "simulatable": card["tradability"]["simulatable"],
            "simulatableLabel": card["tradability"]["reasonLabel"],
            "lastClose": (f"{bar.close_cents / 100:,.2f}" if bar else "—"),
        })

    # --- 草稿预览（真实调用 PlanService）---
    pv = svc.preview(portfolio_id="pf-syn-m", snapshot_id=SNAPSHOT,
                     trading_day=TRADING_DAY, as_of=AS_OF, candidates=candidates,
                     cash_available_cents=100_000_000, lots=[],
                     confirm_subject="user:demo")
    draft = build_draft_vm(
        plan_id=pv.plan_id, portfolio_id="pf-syn-m", trading_day=TRADING_DAY,
        cash_before_cents=100_000_000, orders=pv.orders,
        targets=[{"instrument_id": t.instrument_id, "industry_code": t.industry_code,
                  "weight_pct": str(t.weight_pct)} for t in pv.targets],
        excluded=pv.excluded, fee_table=fees, industry_cap_pct=Decimal("50"),
        equity_value_cents=100_000_000,
    )

    # --- 今日变化（来自事件的可用时点）---
    events = reader.events(SNAPSHOT, as_of=AS_OF)
    changes = [
        {"eventId": e["event_id"], "category": e["event_category"],
         "summary": e["fact_summary"],
         "availableAt": e.get("available_at"),
         "verification": e.get("verification_status"),
         "direction": e.get("market_direction"),
         "subjects": [s.get("subject_id") for s in (e.get("subjects") or [])]}
        for e in events
    ]

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "generator": "tools/build_workspace_fixture.py",
        "note": "由真实领域链路生成，不是手写假数据",
        "status": status,
        "candidates": rows,
        "researchCards": cards,
        "draft": draft,
        "todayChanges": changes,
        "portfolios": [
            {"portfolioId": "pf-syn-m", "kind": "M", "label": "模型基线",
             "netValue": "1,000,000.00", "cash": draft["cashAfter"],
             "positions": 0, "note": "S1 固定模型基线"},
            {"portfolioId": "pf-syn-h", "kind": "H", "label": "用户组合",
             "netValue": "1,000,000.00", "cash": "1,000,000.00",
             "positions": 0, "note": "人工选择对照；自选与持仓分离"},
        ],
        "experiments": [
            {"experimentId": "exp-syn-001", "hypothesis": "S1 趋势-低波动基线是否优于等权",
             "strategyVersion": "S1-syn-v1", "status": "REGISTERED",
             "sampleWindow": "未开始", "dataLevel": "虚构示例",
             "limitations": ["虚构数据，不构成收益结论", "样本期未定义"]},
        ],
        "decisionLog": [
            {"decisionId": "dec-syn-001", "date": "2026-09-08",
             "type": "NO_CHANGE", "reason": "观察期，无新增证据",
             "modelProposed": "维持持仓", "humanFinal": "维持持仓",
             "externalInfoUsed": False},
        ],
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    print(f"  candidates={len(rows)} cards={len(cards)} orders={len(draft['orders'])} "
          f"changes={len(changes)}")
    con.close()
    return 0


def _rules():
    from aquant.domain.simulation.simulator import BoardRule
    return [
        BoardRule(exchange="SSE", board="MAIN", price_limit_pct=Decimal("10"),
                  lot_size=100, effective_from=date(2026, 7, 6)),
        BoardRule(exchange="SZSE", board="MAIN", price_limit_pct=Decimal("10"),
                  lot_size=100, effective_from=date(2026, 7, 6)),
    ]


if __name__ == "__main__":
    raise SystemExit(main())
