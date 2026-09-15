"""工作台只读查询（账本、事件）。

为什么单独一层
--------------
这些是**只读视图**，不是业务动作。写成视图函数而不是塞进 PlanService，
理由是 PlanService 承载的是"会改变账本状态"的编排（预览/冻结/执行/估值），
只读查询混进去会让"哪些方法会写库"变得难以一眼看清。

两条贯穿全部查询的规则：

  1. **金额一律整数分**（§12.6），本层不做单位换算以外的算术；
  2. **事件必须带 PIT 门禁**：按 available_at 过滤，
     绝不因为"数据库里有"就返回——那是把未来信息当成已知。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime


def _money(cents: int | None) -> dict:
    """金额的统一表示：整数分 + 展示串。

    同时给两种形态，是为了让调用方无法在展示时"顺手"做浮点运算——
    展示串由服务端算好，前端不再算第二遍。
    """

    if cents is None:
        return {"cents": None, "display": "—"}
    sign = "-" if cents < 0 else ""
    absolute = abs(int(cents))
    yuan, fen = divmod(absolute, 100)
    return {
        "cents": int(cents),
        "display": sign + format(yuan, ",") + "." + str(fen).zfill(2) + " 元",
    }


def portfolio_ledger(con: sqlite3.Connection, portfolio_id: str) -> dict:
    """组合账本：现金分录、持仓批次、成交、费用、应收。

    全部只读。返回的是**账本事实**，不是估值结果——
    估值另有 /valuations 端点；分开是为了让"账本"与"按市价算的净值"
    在接口层面就不混在一起。
    """

    portfolio = con.execute(
        "SELECT portfolio_id,kind,account_type,initial_cash_cents,opened_at,status "
        "FROM portfolio WHERE portfolio_id=?", (portfolio_id,),
    ).fetchone()
    if portfolio is None:
        raise KeyError("unknown portfolio " + repr(portfolio_id))

    cash_rows = con.execute(
        "SELECT entry_id,entry_type,amount_cents,trading_day,occurred_at,note "
        "FROM cash_entry WHERE portfolio_id=? ORDER BY trading_day, entry_id",
        (portfolio_id,),
    ).fetchall()
    cash_total = sum(int(r["amount_cents"]) for r in cash_rows)

    lot_rows = con.execute(
        "SELECT lot_id,instrument_id,acquired_trading_day,earliest_sellable_day,"
        "quantity_original,quantity_remaining,cost_basis_cents_per_share "
        "FROM position_lot WHERE portfolio_id=? ORDER BY acquired_trading_day, lot_id",
        (portfolio_id,),
    ).fetchall()

    fill_rows = con.execute(
        "SELECT fill_id,instrument_id,side,quantity,price_cents,fees_total_cents,"
        "trading_day FROM fill WHERE portfolio_id=? ORDER BY trading_day, fill_id",
        (portfolio_id,),
    ).fetchall()

    fee_rows = con.execute(
        "SELECT fc.fee_code, SUM(fc.amount_cents) AS total FROM fee_charge fc "
        "JOIN fill f ON f.fill_id=fc.fill_id WHERE f.portfolio_id=? "
        "GROUP BY fc.fee_code ORDER BY fc.fee_code", (portfolio_id,),
    ).fetchall()

    receivable_rows = con.execute(
        "SELECT receivable_id,instrument_id,kind,amount_cents,tax_treatment,"
        "recognized_on,expected_settlement_on,settled_on,status "
        "FROM receivable WHERE portfolio_id=? ORDER BY recognized_on, receivable_id",
        (portfolio_id,),
    ).fetchall()

    positions: dict[str, int] = {}
    for r in lot_rows:
        if int(r["quantity_remaining"]) > 0:
            iid = r["instrument_id"]
            positions[iid] = positions.get(iid, 0) + int(r["quantity_remaining"])

    return {
        "portfolio_id": portfolio_id,
        "kind": portfolio["kind"],
        "account_type": portfolio["account_type"],
        "status": portfolio["status"],
        "opened_at": portfolio["opened_at"],
        "cash": {
            "cents": _money(cash_total)["cents"],
            "display": _money(cash_total)["display"],
            "entry_count": len(cash_rows),
            "entries": [
                {
                    "entry_id": r["entry_id"],
                    "entry_type": r["entry_type"],
                    "amount": _money(int(r["amount_cents"])),
                    "trading_day": r["trading_day"],
                    "occurred_at": r["occurred_at"],
                    "note": r["note"],
                }
                for r in cash_rows
            ],
        },
        "positions": [
            {"instrument_id": k, "quantity": v} for k, v in sorted(positions.items())
        ],
        "lots": [
            {
                "lot_id": r["lot_id"],
                "instrument_id": r["instrument_id"],
                "acquired_trading_day": r["acquired_trading_day"],
                "earliest_sellable_day": r["earliest_sellable_day"],
                "quantity_original": int(r["quantity_original"]),
                "quantity_remaining": int(r["quantity_remaining"]),
                "cost_basis": _money(int(r["cost_basis_cents_per_share"])),
            }
            for r in lot_rows
        ],
        "fills": [
            {
                "fill_id": r["fill_id"],
                "instrument_id": r["instrument_id"],
                "side": r["side"],
                "quantity": int(r["quantity"]),
                "price": _money(int(r["price_cents"])),
                "fees_total": _money(int(r["fees_total_cents"])),
                "trading_day": r["trading_day"],
            }
            for r in fill_rows
        ],
        "fees_by_code": [
            {"fee_code": r["fee_code"], "total": _money(int(r["total"]))}
            for r in fee_rows
        ],
        "receivables": [
            {
                "receivable_id": r["receivable_id"],
                "instrument_id": r["instrument_id"],
                "kind": r["kind"],
                "amount": _money(int(r["amount_cents"])),
                "tax_treatment": r["tax_treatment"],
                "recognized_on": r["recognized_on"],
                "expected_settlement_on": r["expected_settlement_on"],
                "settled_on": r["settled_on"],
                "status": r["status"],
            }
            for r in receivable_rows
        ],
    }


def events(con: sqlite3.Connection, *, as_of: datetime,
           instrument_id: str | None = None,
           category: str | None = None, limit: int = 100) -> list[dict]:
    """事件列表，**按 available_at 门禁过滤**。

    这是 PIT 在读取侧的落点：数据库里有事件不等于决策时点就能看到它。
    过滤条件是 available_at <= as_of，与 D04/D05 的判定一致。
    """

    sql = [
        "SELECT event_id,event_category,fact_summary,event_time,"
        "event_time_precision,source_published_date,source_published_at,"
        "first_seen_at,available_at,available_basis,pit_mode,"
        "verification_status,market_direction FROM event "
        "WHERE available_at <= ?",
    ]
    params: list = [as_of.isoformat()]
    if category:
        sql.append("AND event_category = ?")
        params.append(category)
    if instrument_id:
        sql.append("AND event_id IN (SELECT event_id FROM event_subject "
                   "WHERE subject_type=? AND subject_id = ?)")
        params.append("INSTRUMENT")
        params.append(instrument_id)
    sql.append("ORDER BY available_at DESC, event_id LIMIT ?")
    params.append(limit)

    rows = con.execute(" ".join(sql), params).fetchall()
    out: list[dict] = []
    for r in rows:
        citations = con.execute(
            "SELECT citation_id,document_id,quote,locator_kind,located "
            "FROM citation WHERE event_id=?", (r["event_id"],),
        ).fetchall()
        subjects = con.execute(
            "SELECT subject_type,subject_id,role FROM event_subject WHERE event_id=?",
            (r["event_id"],),
        ).fetchall()
        out.append({
            "event_id": r["event_id"],
            "category": r["event_category"],
            "summary": r["fact_summary"],
            "event_time": r["event_time"],
            "event_time_precision": r["event_time_precision"],
            "source_published_date": r["source_published_date"],
            "first_seen_at": r["first_seen_at"],
            "available_at": r["available_at"],
            "available_basis": r["available_basis"],
            "pit_mode": r["pit_mode"],
            "verification_status": r["verification_status"],
            "market_direction": r["market_direction"],
            "subjects": [dict(s) for s in subjects],
            # 引用必须可定位（§15.3）。located=0 的引用不得当成证据。
            "citations": [dict(c) for c in citations],
            "has_located_citation": any(int(c["located"]) == 1 for c in citations),
        })
    return out