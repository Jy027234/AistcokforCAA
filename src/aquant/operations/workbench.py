"""自选与决策日志的写入路径（§16.2、§5.5）。

两者共同点：都是**记录用户动作**，不是记录模型输出。

  * 自选：关心某只证券，但**不产生订单**（§16.2）。
    它与模拟持仓在数据上完全分离，不参与组合构建。
  * 决策日志：记录"模型建议了什么、人最终做了什么、差在哪"（§5.5）。
    价值全在**不事后美化**——模型原方案与人工方案分开存，
    差异单独算。若只存最终方案，复盘时无法知道人改了什么。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from ..domain.data.db import write_tx

#: §11.3 的决策类型。枚举与 schema 的 CHECK 一致。
DECISION_TYPES = ("ACCEPT_MODEL", "MODIFY_MODEL", "NO_CHANGE",
                  "TIMEOUT", "REJECT", "CANCEL")


class WorkbenchError(Exception):
    def __init__(self, code: str, message: str, object_id: str,
                 repair_action: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.object_id = object_id
        self.repair_action = repair_action

    def as_error(self) -> dict:
        return {"code": self.code, "message": self.message,
                "object_id": self.object_id, "retryable": False,
                "repair_action": self.repair_action}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ======================================================================
# 自选
# ======================================================================
def add_watchlist_item(con: sqlite3.Connection, *, subject_id: str,
                       instrument_id: str, note: str | None = None) -> dict:
    """加入自选。重复加入是幂等的，不报错也不重复插入。"""

    if not subject_id or not subject_id.strip():
        raise WorkbenchError("DATA_NOT_READY", "subject_id is required",
                             instrument_id, "identify the user adding the item")

    # 证券必须存在：往自选里塞一个不存在的代码，界面会显示一个查不到的标的
    known = con.execute("SELECT 1 FROM instrument WHERE instrument_id=?",
                        (instrument_id,)).fetchone()
    if known is None:
        raise WorkbenchError("DATA_NOT_READY",
                             "unknown instrument " + repr(instrument_id),
                             instrument_id,
                             "add the instrument to a snapshot before watching it")

    with write_tx(con):
        con.execute(
            "INSERT OR IGNORE INTO watchlist_item "
            "(subject_id,instrument_id,added_at,note) VALUES (?,?,?,?)",
            (subject_id, instrument_id, _now(), note))
    return {"subject_id": subject_id, "instrument_id": instrument_id,
            "watching": True, "note": note}


def remove_watchlist_item(con: sqlite3.Connection, *, subject_id: str,
                          instrument_id: str) -> dict:
    """移出自选。**不产生订单**，也不影响任何持仓或账本（§16.2）。"""

    with write_tx(con):
        cur = con.execute(
            "DELETE FROM watchlist_item WHERE subject_id=? AND instrument_id=?",
            (subject_id, instrument_id))
    return {"subject_id": subject_id, "instrument_id": instrument_id,
            "watching": False, "removed": cur.rowcount,
            "note": "自选变化不产生订单，也不影响模拟持仓"}


def watchlist(con: sqlite3.Connection, *, subject_id: str) -> list[dict]:
    rows = con.execute(
        "SELECT w.instrument_id, w.added_at, w.note, i.short_name, i.exchange, i.board "
        "FROM watchlist_item w LEFT JOIN instrument i ON i.instrument_id=w.instrument_id "
        "WHERE w.subject_id=? ORDER BY w.added_at DESC, w.instrument_id",
        (subject_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ======================================================================
# 决策日志
# ======================================================================
def _canonical(payload: object) -> str:
    """稳定的序列化：键排序 + 紧凑分隔符。

    差异必须可比：同一份内容每次算出的哈希必须相同，
    否则"这次和上次是不是同一个方案"就答不出来。
    """

    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _diff(model_proposed: object, human_final: object) -> dict:
    """模型方案与人工方案的差异。

    §11.3 要求"保存模型原方案与人工差异，不事后美化"。
    因此这里**只做客观差异**，不判断谁对：
    记录增删改了哪些字段，以及内容是否完全相同。
    """

    if model_proposed is None or human_final is None:
        return {"comparable": False,
                "reason": "模型方案或人工方案缺失，无法比较"}
    if _canonical(model_proposed) == _canonical(human_final):
        return {"comparable": True, "identical": True, "changed_keys": []}

    proposed = model_proposed if isinstance(model_proposed, dict) else {}
    final = human_final if isinstance(human_final, dict) else {}
    keys = sorted(set(proposed) | set(final))
    changed = [k for k in keys if _canonical(proposed.get(k)) != _canonical(final.get(k))]
    removed = [k for k in keys if k in proposed and k not in final]
    added = [k for k in keys if k in final and k not in proposed]
    return {
        "comparable": True, "identical": False,
        "changed_keys": changed, "removed_keys": removed, "added_keys": added,
        "model_hash": "sha256:" + hashlib.sha256(
            _canonical(model_proposed).encode()).hexdigest(),
        "human_hash": "sha256:" + hashlib.sha256(
            _canonical(human_final).encode()).hexdigest(),
    }


def record_decision(con: sqlite3.Connection, *, portfolio_id: str,
                    snapshot_id: str, decision_type: str,
                    plan_id: str | None = None,
                    model_proposed: object | None = None,
                    human_final: object | None = None,
                    reason_category: str | None = None,
                    reason_note: str | None = None,
                    external_information_used: bool = False,
                    rule_check: object | None = None,
                    decision_id: str | None = None) -> dict:
    """记录一次决策。**模型方案与人工方案分开存**，差异单独算。

    external_information_used 必须显式声明：用了模型上下文之外的信息
    会改变这次决策的可复现性，事后无法从数据里反推，只能靠记录。
    """

    if decision_type not in DECISION_TYPES:
        raise WorkbenchError(
            "DATA_NOT_READY",
            "unknown decision_type " + repr(decision_type),
            portfolio_id,
            "use one of: " + ", ".join(DECISION_TYPES))

    snapshot_exists = con.execute(
        "SELECT 1 FROM snapshot WHERE snapshot_id=?", (snapshot_id,)).fetchone()
    if snapshot_exists is None:
        raise WorkbenchError("DATA_NOT_READY",
                             "unknown snapshot " + repr(snapshot_id),
                             snapshot_id, "record decisions against a real snapshot")

    diff = _diff(model_proposed, human_final)
    identifier = decision_id or ("dec-" + hashlib.sha256(
        (portfolio_id + "|" + snapshot_id + "|" + str(plan_id) + "|"
         + _now()).encode()).hexdigest()[:24])

    with write_tx(con):
        con.execute(
            "INSERT INTO decision_log (decision_id,portfolio_id,plan_id,snapshot_id,"
            "decision_type,model_proposed_json,human_final_json,diff_json,"
            "reason_category,reason_note,external_information_used,submitted_at,"
            "rule_check_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (identifier, portfolio_id, plan_id, snapshot_id, decision_type,
             _canonical(model_proposed) if model_proposed is not None else None,
             _canonical(human_final) if human_final is not None else None,
             json.dumps(diff, ensure_ascii=False),
             reason_category, reason_note,
             1 if external_information_used else 0, _now(),
             json.dumps(rule_check, ensure_ascii=False) if rule_check else None))

    return {"decision_id": identifier, "decision_type": decision_type,
            "diff": diff, "recorded_at": _now()}


def decisions(con: sqlite3.Connection, *, portfolio_id: str | None = None,
              limit: int = 100) -> list[dict]:
    sql = ["SELECT decision_id,portfolio_id,plan_id,snapshot_id,decision_type,"
           "diff_json,reason_category,reason_note,external_information_used,"
           "submitted_at FROM decision_log"]
    params: list = []
    if portfolio_id:
        sql.append("WHERE portfolio_id=?")
        params.append(portfolio_id)
    sql.append("ORDER BY submitted_at DESC, decision_id LIMIT ?")
    params.append(limit)
    rows = con.execute(" ".join(sql), params).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["diff"] = json.loads(item.pop("diff_json") or "{}")
        item["external_information_used"] = bool(item["external_information_used"])
        out.append(item)
    return out