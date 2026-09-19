"""实验登记（§13.2）。

这条规则的全部意义在于**顺序**：先登记、后看结果。

如果实验是"跑完再补登记"，那么登记表里只会留下成功的那批——
失败的、负收益的、中途改过参数的实验都不会出现，
而评审者看到的是一张精心筛选过的历史。

因此这里的设计处处围绕"不可事后调整"：

  * **没有 UPDATE 路径**。改一条权重、过滤条件或事件窗口就产生新实验——
    旧记录原样保留，因此"改过什么"永远可查。
  * **登记时校验输入范围**，不校验结果：登记发生在看结果之前，
    那时还没有结果可校验。
  * **测试集访问次数单独计数**：§13.2 明确"测试集被多次用于选择方案后，
    不再是未触碰测试集"。计数必须与方案选择行为绑定，不能靠自觉。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone

from ..data.db import write_tx
from .strategies import require_strategy_version_available


class ExperimentError(Exception):
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


@dataclass(frozen=True, slots=True)
class ExperimentSpec:
    """一次实验登记。**冻结**，登记后不可改。"""

    hypothesis: str
    data_range_start: date
    data_range_end: date
    universe: list[str]
    feature_version: str
    strategy_version: str
    primary_metric: str
    stopping_condition: str
    train: tuple[date, date] | None = None
    valid: tuple[date, date] | None = None
    test: tuple[date, date] | None = None
    preprocessing: dict = field(default_factory=dict)
    label_window: str | None = None
    fee_version: str | None = None
    slippage_bps: int | None = None
    comparison: dict = field(default_factory=dict)

    def validate(self) -> None:
        """登记前的输入校验。**不校验结果**——此时还没有结果。"""

        if not self.hypothesis.strip():
            raise ExperimentError("DATA_NOT_READY", "hypothesis is empty",
                                  "hypothesis", "state what the experiment tests")
        if self.data_range_start > self.data_range_end:
            raise ExperimentError("DATA_NOT_READY",
                                  "data range start is after end",
                                  "data_range", "correct the data range")
        if not self.universe:
            raise ExperimentError("DATA_NOT_READY", "universe is empty",
                                  "universe",
                                  "list the instruments or name the universe")
        for label, window in (("train", self.train), ("valid", self.valid),
                              ("test", self.test)):
            if window and window[0] > window[1]:
                raise ExperimentError("DATA_NOT_READY",
                                      label + " window start is after end",
                                      label, "correct the window")
        # §13.3 训练/验证/测试必须时间有序且不重叠
        ordered = [(n, w) for n, w in (("train", self.train),
                                       ("valid", self.valid),
                                       ("test", self.test)) if w]
        for (n1, w1), (n2, w2) in zip(ordered, ordered[1:]):
            if w1[1] >= w2[0]:
                raise ExperimentError(
                    "DATA_NOT_READY", n1 + " overlaps or touches " + n2,
                    n1 + "/" + n2,
                    "split windows are ordered and non-overlapping (no shuffling)")


def fingerprint(spec: ExperimentSpec) -> str:
    """实验指纹。**改变任一输入即产生新实验**（§13.2）。

    日期序列化成 ISO 字符串，保证同一份规格每次得到同一指纹——
    否则"这条是不是同一次实验"就答不出来。
    """

    payload = asdict(spec)
    for key in ("data_range_start", "data_range_end"):
        payload[key] = payload[key].isoformat()
    for key in ("train", "valid", "test"):
        if payload.get(key):
            payload[key] = [d.isoformat() for d in payload[key]]
    payload["universe"] = sorted(payload["universe"])
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def register_experiment(con: sqlite3.Connection, spec: ExperimentSpec, *,
                        experiment_id: str | None = None) -> dict:
    """登记一次实验。**只有 INSERT，没有 UPDATE**。

    重复登记同一份规格（指纹相同）返回既有记录且 created=False，
    而不是报错——重复提交同一实验不是错误。
    指纹不同则必然是新记录：这正是"改一条权重就产生新实验"。
    """

    spec.validate()
    # 外键只能证明“这个名字存在”，不能证明对应策略仍获准运行。升级前可能
    # 已经留下 S2 记录，因此登记实验时必须再次经过当前能力闸门。
    require_strategy_version_available(con, spec.strategy_version)
    fp = fingerprint(spec)

    existing = con.execute(
        "SELECT experiment_id, registered_at FROM experiment "
        "WHERE comparison_json LIKE ?", ("%" + fp + "%",),
    ).fetchone()
    if existing is not None:
        return {"experiment_id": existing["experiment_id"], "created": False,
                "fingerprint": fp, "registered_at": existing["registered_at"]}

    identifier = experiment_id or ("exp-" + fp.removeprefix("sha256:")[:20])
    comparison = dict(spec.comparison)
    comparison["fingerprint"] = fp

    with write_tx(con):
        con.execute(
            "INSERT INTO experiment (experiment_id,hypothesis,registered_at,"
            "data_range_start,data_range_end,universe_json,feature_version,"
            "strategy_version,train_start,train_end,valid_start,valid_end,"
            "test_start,test_end,preprocessing_json,label_window,fee_version,"
            "slippage_bps,comparison_json,primary_metric,stopping_condition,"
            "status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'REGISTERED')",
            (identifier, spec.hypothesis, _now(),
             spec.data_range_start.isoformat(), spec.data_range_end.isoformat(),
             json.dumps(sorted(spec.universe), ensure_ascii=False),
             spec.feature_version, spec.strategy_version,
             spec.train[0].isoformat() if spec.train else None,
             spec.train[1].isoformat() if spec.train else None,
             spec.valid[0].isoformat() if spec.valid else None,
             spec.valid[1].isoformat() if spec.valid else None,
             spec.test[0].isoformat() if spec.test else None,
             spec.test[1].isoformat() if spec.test else None,
             json.dumps(spec.preprocessing, ensure_ascii=False),
             spec.label_window, spec.fee_version, spec.slippage_bps,
             json.dumps(comparison, ensure_ascii=False),
             spec.primary_metric, spec.stopping_condition))

    return {"experiment_id": identifier, "created": True, "fingerprint": fp,
            "registered_at": _now()}


def note_test_set_access(con: sqlite3.Connection, experiment_id: str, *,
                         reason: str) -> dict:
    """记录一次测试集访问（§13.2）。

    规范原文：「测试集被多次用于选择方案后，不再是未触碰测试集；
    界面必须记录它被查看和比较的次数。」
    因此每次访问都要计数并留原因，不能只在报告里口头声明"只用了一次"。
    """

    with write_tx(con):
        cur = con.execute(
            "UPDATE experiment SET test_set_access_count = test_set_access_count + 1 "
            "WHERE experiment_id=?", (experiment_id,))
        if cur.rowcount == 0:
            raise ExperimentError("DATA_NOT_READY",
                                  "unknown experiment " + repr(experiment_id),
                                  experiment_id, "register the experiment first")
        row = con.execute(
            "SELECT test_set_access_count, outcome_notes FROM experiment "
            "WHERE experiment_id=?", (experiment_id,)).fetchone()
        count = int(row["test_set_access_count"])
        note = ((row["outcome_notes"] or "")
                + "\n[测试集访问 " + str(count) + "] " + _now() + " " + reason)
        con.execute("UPDATE experiment SET outcome_notes=? WHERE experiment_id=?",
                    (note.strip(), experiment_id))

    return {"experiment_id": experiment_id, "testSetAccessCount": count,
            "reason": reason,
            "warning": ("测试集已被访问 " + str(count) + " 次；"
                        "多次用于选择方案后它不再是未触碰测试集")}


def record_outcome(con: sqlite3.Connection, experiment_id: str, *,
                   status: str, outcome_notes: str) -> dict:
    """记录实验结论。**失败与负收益同样保存**（§13.2）。

    这个函数只写结论与状态，**不修改任何输入**——
    输入一旦登记就冻结，否则"先登记后看结果"就没有意义。
    """

    allowed = ("REGISTERED", "RUNNING", "COMPLETED", "FAILED", "ABANDONED")
    if status not in allowed:
        raise ExperimentError("DATA_NOT_READY",
                              "unknown status " + repr(status),
                              experiment_id, "use one of: " + ", ".join(allowed))
    if status in ("COMPLETED", "FAILED", "ABANDONED") and not outcome_notes.strip():
        raise ExperimentError(
            "DATA_NOT_READY", "outcome notes are required to close an experiment",
            experiment_id, "record what happened, including negative results")

    with write_tx(con):
        cur = con.execute(
            "UPDATE experiment SET status=?, outcome_notes=? WHERE experiment_id=?",
            (status, outcome_notes, experiment_id))
        if cur.rowcount == 0:
            raise ExperimentError("DATA_NOT_READY",
                                  "unknown experiment " + repr(experiment_id),
                                  experiment_id, "register the experiment first")
    return {"experiment_id": experiment_id, "status": status}


def experiment(con: sqlite3.Connection, experiment_id: str) -> dict:
    row = con.execute("SELECT * FROM experiment WHERE experiment_id=?",
                      (experiment_id,)).fetchone()
    if row is None:
        raise ExperimentError("DATA_NOT_READY",
                              "unknown experiment " + repr(experiment_id),
                              experiment_id, "check the experiment id")
    out = dict(row)
    for key in ("universe_json", "preprocessing_json", "comparison_json"):
        if out.get(key):
            try:
                out[key.removesuffix("_json")] = json.loads(out[key])
            except json.JSONDecodeError:
                out[key.removesuffix("_json")] = None
        out.pop(key, None)
    return out


def experiments(con: sqlite3.Connection, *, limit: int = 100) -> list[dict]:
    rows = con.execute(
        "SELECT experiment_id,hypothesis,registered_at,status,"
        "test_set_access_count,primary_metric,outcome_notes "
        "FROM experiment ORDER BY registered_at DESC, experiment_id LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]
