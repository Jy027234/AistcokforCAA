"""研究卡片的持久化（主文档 §5.3、§14.1）。

为什么卡片必须落库
------------------
卡片原先每次请求都**现算**、算完即弃。后果不是"少了个功能"，而是：

  * 无法回答"我在那天看到的是什么"。卡片里的排名、覆盖率、数据完整度
    都绑定快照与生成时点；现算的话，同一只标的在不同时刻打开会得到
    不同的卡片，而界面上看不出这是两份不同的东西。
  * 决策日志里的"当时依据"无从对照。§11.3 要求保存模型原方案与人工差异，
    如果卡片不留存，事后复盘只能靠记忆。

因此卡片按 (instrument_id, snapshot_id, trading_day) 冻结：
同一天同一快照同一标的重复打开得到**同一张**卡片，时间戳不刷新。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone

from aquant.domain.data.db import write_tx


def research_card_id(instrument_id: str, snapshot_id: str, trading_day: date) -> str:
    """卡片的稳定标识。

    刻意**不含生成时刻**：含了就等于"每次打开都是新卡片"，
    而我们要的恰恰是同一天同一快照只有一份证据。
    """

    return f"card-{instrument_id}-{snapshot_id}-{trading_day.isoformat()}"


def persist_card(con: sqlite3.Connection, *, snapshot_id: str, trading_day: date,
                 card: dict, data_mode: str,
                 research_run_id: str | None = None) -> dict:
    """把一张卡片落库。已存在则原样返回已存的那份。

    **不覆盖**已存在的卡片：它是"当时看到的证据"，事后被后来的数据盖掉，
    就失去了作为证据的意义。要新的就换快照或换交易日。

    入参口径与视图层一致（camelCase，见 ResearchCardVM.as_dict），
    交易日与数据模式由调用方显式传入——卡片视图里没有这两个字段，
    而它们正是卡片身份的一部分，不能从别处猜。
    """

    instrument_id = card["instrumentId"]
    card_id = research_card_id(instrument_id, snapshot_id, trading_day)

    existing = get_card(con, card_id)
    if existing is not None:
        return existing

    numeric = {
        "rankSemantics": card.get("rankSemantics"),
        "rankBreakdown": card.get("rankBreakdown") or [],
        "comparisonScope": card.get("comparisonScope"),
        # 可模拟性也是"当时看到的结论"：规则版本或状态一变，
        # 同一只标的的可模拟性就会不同，事后无法还原
        "tradability": card.get("tradability"),
    }
    with write_tx(con):
        con.execute(
            "INSERT OR IGNORE INTO research_card (card_id,research_run_id,snapshot_id,"
            "instrument_id,generated_at,data_mode,quality_label,numeric_json,"
            "evidence_json,counter_evidence_json,uncertainty_json,limitations_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (card_id, research_run_id, snapshot_id, instrument_id,
             card.get("generatedAt") or datetime.now(timezone.utc).isoformat(),
             # 数据模式由调用方从**快照**读出后传入：合成数据必须一直带着
             # 这个标记，不能因为调用方忘了传就默认为真实数据
             data_mode,
             card.get("dataCompleteness") or "",
             json.dumps(numeric, ensure_ascii=False),
             json.dumps(card.get("evidence") or [], ensure_ascii=False),
             json.dumps(card.get("counterEvidence") or [], ensure_ascii=False),
             json.dumps(card.get("uncertainties") or [], ensure_ascii=False),
             json.dumps(card.get("limitations") or [], ensure_ascii=False)),
        )
    stored = get_card(con, card_id)
    assert stored is not None, "写入后必须能读回；读不到说明列名或事务有问题"
    return stored


def get_card(con: sqlite3.Connection, card_id: str) -> dict | None:
    row = con.execute("SELECT * FROM research_card WHERE card_id=?", (card_id,)).fetchone()
    return _to_dict(row) if row is not None else None


def research_cards(con: sqlite3.Connection, *, instrument_id: str | None = None,
                   snapshot_id: str | None = None, limit: int = 50) -> list[dict]:
    """已留存的研究卡片，最近生成的在前面。"""

    sql = "SELECT * FROM research_card WHERE 1=1"
    args: list[object] = []
    if instrument_id:
        sql += " AND instrument_id=?"
        args.append(instrument_id)
    if snapshot_id:
        sql += " AND snapshot_id=?"
        args.append(snapshot_id)
    sql += " ORDER BY generated_at DESC, card_id LIMIT ?"
    args.append(int(limit))
    return [_to_dict(r) for r in con.execute(sql, args)]


def _to_dict(row: sqlite3.Row) -> dict:
    def load(key: str):
        raw = row[key]
        return json.loads(raw) if raw else None

    numeric = load("numeric_json") or {}
    return {
        "card_id": row["card_id"],
        "research_run_id": row["research_run_id"],
        "snapshot_id": row["snapshot_id"],
        "instrument_id": row["instrument_id"],
        "generated_at": row["generated_at"],
        "data_mode": row["data_mode"],
        "quality_label": row["quality_label"],
        "rank_semantics": numeric.get("rankSemantics"),
        "rank_breakdown": numeric.get("rankBreakdown") or [],
        "comparison_scope": numeric.get("comparisonScope"),
        "tradability": numeric.get("tradability"),
        "evidence": load("evidence_json") or [],
        "counter_evidence": load("counter_evidence_json") or [],
        "uncertainties": load("uncertainty_json") or [],
        "limitations": load("limitations_json") or [],
    }
