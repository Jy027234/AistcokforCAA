"""研究卡片的持久化（主文档 §5.3、§14.1）。

为什么卡片必须落库
------------------
卡片原先每次请求都**现算**、算完即弃。后果不是"少了个功能"，而是：

  * 无法回答"我在那天看到的是什么"。卡片里的排名、覆盖率、数据完整度
    都绑定快照与生成时点；现算的话，同一只标的在不同时刻打开会得到
    不同的卡片，而界面上看不出这是两份不同的东西。
  * 决策日志里的"当时依据"无从对照。§11.3 要求保存模型原方案与人工差异，
    如果卡片不留存，事后复盘只能靠记忆。

因此卡片按 (instrument_id, snapshot_id, trading_day, research_basis) 冻结：
同一天同一快照同一标的、同一研究版本重复打开得到**同一张**卡片，时间戳
不刷新；公式修订产生新的研究运行和新卡片，旧卡仍保留供审计。
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import date, datetime, timezone

from aquant.domain.data.db import write_tx
from aquant.domain.research.runs import feature_version_withdrawal_reason


def _payload_json(payload: dict) -> str:
    """用稳定编码保存完整 API 响应，便于哈希校验和复现。"""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def research_card_id(instrument_id: str, snapshot_id: str, trading_day: date,
                     *, research_basis: str | None = None) -> str:
    """卡片的稳定标识。

    刻意**不含生成时刻**：含了就等于"每次打开都是新卡片"。
    ``research_basis`` 只在研究公式或运行变化时改变，防止已撤回版本冻结的
    卡片遮住修正后的结果。
    """

    base = f"card-{instrument_id}-{snapshot_id}-{trading_day.isoformat()}"
    if research_basis is None:
        return base
    # 新因子版本或新研究运行必须产生新卡片；旧卡片仍保留为当时所见证据。
    # 只放短哈希，避免把任意上游标识直接拼进业务主键。
    suffix = hashlib.sha256(research_basis.encode("utf-8")).hexdigest()[:12]
    return f"{base}-basis-{suffix}"


def persist_card(con: sqlite3.Connection, *, snapshot_id: str, trading_day: date,
                 card: dict, data_mode: str,
                 research_run_id: str | None = None,
                 research_basis: str | None = None) -> dict:
    """把一张卡片落库。已存在则原样返回已存的那份。

    **不覆盖**已存在的卡片：它是"当时看到的证据"，事后被后来的数据盖掉，
    就失去了作为证据的意义。要新的就换快照或换交易日。

    入参口径与视图层一致（camelCase，见 ResearchCardVM.as_dict），
    交易日与数据模式由调用方显式传入——卡片视图里没有这两个字段，
    而它们正是卡片身份的一部分，不能从别处猜。
    """

    instrument_id = card["instrumentId"]
    card_id = research_card_id(
        instrument_id, snapshot_id, trading_day, research_basis=research_basis)

    existing = get_card(con, card_id)
    generated_at = (existing["generated_at"] if existing is not None
                    else card.get("generatedAt")
                    or datetime.now(timezone.utc).isoformat())
    payload = dict(card)
    payload["cardId"] = card_id
    payload["generatedAt"] = generated_at
    payload_json = _payload_json(payload)
    payload_hash = "sha256:" + hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

    if existing is not None:
        # 迁移前的卡片没有完整 payload。第一次在新版本读取时把当前可重建
        # 的完整响应冻结下来；之后所有请求都只读这一份，不再随新证据变化。
        with write_tx(con):
            con.execute(
                "INSERT OR IGNORE INTO research_card_payload "
                "(card_id,payload_json,payload_hash,persisted_at) VALUES (?,?,?,?)",
                (card_id, payload_json, payload_hash,
                 datetime.now(timezone.utc).isoformat()),
            )
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
        inserted = con.execute(
            "INSERT OR IGNORE INTO research_card (card_id,research_run_id,snapshot_id,"
            "instrument_id,generated_at,data_mode,quality_label,numeric_json,"
            "evidence_json,counter_evidence_json,uncertainty_json,limitations_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (card_id, research_run_id, snapshot_id, instrument_id,
             generated_at,
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
        # Only the transaction that inserted the card may create its payload.
        # This keeps a concurrent first request from having its response replaced
        # by a second request that observed the same card identity.
        if inserted.rowcount:
            con.execute(
                "INSERT OR IGNORE INTO research_card_payload "
                "(card_id,payload_json,payload_hash,persisted_at) VALUES (?,?,?,?)",
                (card_id, payload_json, payload_hash,
                 datetime.now(timezone.utc).isoformat()),
            )
    stored = get_card(con, card_id)
    assert stored is not None, "写入后必须能读回；读不到说明列名或事务有问题"
    return stored


def get_card(con: sqlite3.Connection, card_id: str) -> dict | None:
    row = con.execute("SELECT * FROM research_card WHERE card_id=?", (card_id,)).fetchone()
    return _to_dict(row) if row is not None else None


def get_card_payload(con: sqlite3.Connection, card_id: str) -> dict | None:
    """读取首次展示时保存的完整 camelCase 卡片响应。

    校验 payload_hash 是为了让历史读取在 payload 被手工改动时显式失败，
    而不是静默返回一份无法证明来源的卡片。
    """

    row = con.execute(
        "SELECT payload_json,payload_hash FROM research_card_payload WHERE card_id=?",
        (card_id,),
    ).fetchone()
    if row is None:
        return None
    payload_json = row["payload_json"]
    expected = "sha256:" + hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    if expected != row["payload_hash"]:
        raise ValueError(f"research card payload hash mismatch: {card_id}")
    payload = json.loads(payload_json)
    if not isinstance(payload, dict):
        raise ValueError(f"research card payload is not an object: {card_id}")
    return payload


def research_cards(con: sqlite3.Connection, *, instrument_id: str | None = None,
                   snapshot_id: str | None = None, limit: int = 50) -> list[dict]:
    """已留存的研究卡片，最近生成的在前面。

    新版卡片优先返回首次展示时冻结的完整 API payload；同时保留旧列表
    使用的 snake_case 审计字段，避免历史调用方失去快照和数据模式信息。
    迁移前没有完整 payload 的记录仍按旧摘要返回，不能臆造当时未保存的字段。
    """

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
    cards: list[dict] = []
    for row in con.execute(sql, args):
        summary = _to_dict(row)
        payload = get_card_payload(con, row["card_id"])
        if payload is None:
            _mark_retired_or_unversioned_f10(con, row, summary)
            cards.append(summary)
            continue
        complete = dict(payload)
        complete.update({
            "card_id": summary["card_id"],
            "research_run_id": summary["research_run_id"],
            "snapshot_id": summary["snapshot_id"],
            "instrument_id": summary["instrument_id"],
            "generated_at": summary["generated_at"],
            "data_mode": summary["data_mode"],
        })
        _mark_retired_or_unversioned_f10(con, row, complete)
        cards.append(complete)
    return cards


def _mark_retired_or_unversioned_f10(con: sqlite3.Connection, row: sqlite3.Row,
                                      card: dict) -> None:
    """历史卡片保持原样可读，同时显式标出已知失效的财务版本。"""

    run_id = row["research_run_id"]
    reason = None
    if run_id:
        run = con.execute(
            "SELECT feature_version FROM research_run WHERE research_run_id=?",
            (run_id,),
        ).fetchone()
        if run is not None:
            reason = feature_version_withdrawal_reason(
                run["feature_version"], con)
    else:
        breakdown = card.get("rankBreakdown") or card.get("rank_breakdown") or []
        if any((item.get("factorId") or item.get("factor_id")) == "F10"
               for item in breakdown if isinstance(item, dict)):
            reason = ("该历史卡片含 F10，但未绑定 research_run_id，无法证明其公式"
                      "版本；不得作为当前财务研究依据")
    if reason is None:
        return
    key = "limitations" if "rankBreakdown" in card else "limitations"
    limitations = list(card.get(key) or [])
    if reason not in limitations:
        limitations.append(reason)
    card[key] = limitations
    card["factorValidity"] = "WITHDRAWN_OR_UNVERIFIED"


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
