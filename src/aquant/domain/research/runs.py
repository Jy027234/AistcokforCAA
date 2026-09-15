"""研究运行与因子落库（§10.2、§13）。

为什么需要这一层
--------------
因子算出来只是中间结果。§10.2 要求落库时同时记录：

  * **横截面排名**（`cross_sectional_rank`）——因子的意义全在排名，
    没有排名的原始值在选股时用不上；
  * **缺失原因**（`exclusion_reason`）——§10.2 明确禁止用 0 填充缺失值，
    因此每个算不出的标的都必须带一条可读原因。

只存一个数值数组是不够的：事后无法回答"这只为什么没进排名"。

研究运行必须绑定快照与时点
--------------------------
`research_run` 带 `snapshot_id` 与 `as_of_time`。这不是冗余——
因子值只有在"用哪个快照、哪个时点"确定之后才有意义。
缺了它们，两个不同时点的排名会被混成一份数据。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from ..data.db import write_tx


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class FactorValue:
    """一个标的在一个因子上的取值。

    `raw_value` 与 `exclusion_reason` 互斥：算得出就给值，
    算不出就给原因。**不允许两者都为空**——那是一个无法解释的行。
    """

    instrument_id: str
    factor_id: str
    raw_value: float | None
    exclusion_reason: str | None = None
    coverage_ratio: float | None = None

    def __post_init__(self) -> None:
        if (self.raw_value is None) == (self.exclusion_reason is None):
            raise ValueError(
                "factor value must have exactly one of raw_value / "
                "exclusion_reason; got instrument=" + self.instrument_id
                + " factor=" + self.factor_id)


def cross_sectional_ranks(values: dict[str, float]) -> dict[str, float]:
    """平均同分百分位排名，降序（值越大排名越高）。

    同分取平均名次而不是任意先后：任意先后会让排名依赖字典顺序，
    同一份数据两次运行得到不同排名，而排名是选股的直接依据。
    """

    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))
    n = len(ordered)
    out: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        # 名次区间 [i, j] 的平均，再转成百分位
        average_rank = (i + j) / 2
        percentile = 1.0 - (average_rank / (n - 1)) if n > 1 else 1.0
        for k in range(i, j + 1):
            out[ordered[k][0]] = round(percentile, 6)
        i = j + 1
    return out


def create_research_run(con: sqlite3.Connection, *, snapshot_id: str,
                        as_of_time: datetime, code_version: str,
                        feature_version: str,
                        strategy_version: str | None = None,
                        experiment_id: str | None = None,
                        notes: str | None = None) -> str:
    """开一次研究运行。绑定快照与时点——缺了它们排名无法解释。"""

    known = con.execute("SELECT 1 FROM snapshot WHERE snapshot_id=?",
                        (snapshot_id,)).fetchone()
    if known is None:
        raise KeyError("unknown snapshot " + repr(snapshot_id))

    run_id = "rr-" + hashlib.sha256(
        (snapshot_id + "|" + as_of_time.isoformat() + "|" + feature_version)
        .encode()).hexdigest()[:24]

    with write_tx(con):
        con.execute(
            "INSERT OR REPLACE INTO research_run (research_run_id,experiment_id,"
            "snapshot_id,as_of_time,code_version,strategy_version,feature_version,"
            "started_at,status,notes) VALUES (?,?,?,?,?,?,?,?,'RUNNING',?)",
            (run_id, experiment_id, snapshot_id, as_of_time.isoformat(),
             code_version, strategy_version, feature_version, _now(), notes))
    return run_id


def store_factor_values(con: sqlite3.Connection, *, research_run_id: str,
                        values: list[FactorValue]) -> dict:
    """落库因子值，并**在同一次写入内**算好横截面排名。

    排名只对有值的标的计算：把"算不出"的标的也放进排名，
    等于给缺失值一个名次，那是最隐蔽的一种 0 填充。
    """

    by_factor: dict[str, dict[str, float]] = {}
    for v in values:
        if v.raw_value is not None:
            by_factor.setdefault(v.factor_id, {})[v.instrument_id] = v.raw_value

    ranks: dict[tuple[str, str], float] = {}
    for factor_id, series in by_factor.items():
        for instrument_id, rank in cross_sectional_ranks(series).items():
            ranks[(factor_id, instrument_id)] = rank

    with write_tx(con):
        for v in values:
            con.execute(
                "INSERT OR REPLACE INTO feature_value (research_run_id,"
                "instrument_id,factor_id,raw_value,transformed_value,"
                "cross_sectional_rank,exclusion_reason,coverage_ratio) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (research_run_id, v.instrument_id, v.factor_id, v.raw_value,
                 None, ranks.get((v.factor_id, v.instrument_id)),
                 v.exclusion_reason, v.coverage_ratio))
        con.execute(
            "UPDATE research_run SET status='SUCCEEDED', finished_at=?,"
            "output_hash=? WHERE research_run_id=?",
            (_now(), _output_hash(values), research_run_id))

    counted = sum(1 for v in values if v.raw_value is not None)
    return {
        "research_run_id": research_run_id,
        "stored": len(values),
        "valued": counted,
        "excluded": len(values) - counted,
        "factors": sorted(by_factor),
    }


def _output_hash(values: list[FactorValue]) -> str:
    """结果哈希：排序后序列化，保证同一份结果每次得到同一哈希。"""

    payload = sorted(
        (v.instrument_id, v.factor_id,
         "" if v.raw_value is None else str(v.raw_value),
         v.exclusion_reason or "") for v in values)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def factor_values(con: sqlite3.Connection, *, research_run_id: str,
                  factor_id: str | None = None) -> list[dict]:
    """取某次研究运行的因子值。含排名与缺失原因。"""

    sql = [
        "SELECT instrument_id,factor_id,raw_value,cross_sectional_rank,"
        "exclusion_reason,coverage_ratio FROM feature_value "
        "WHERE research_run_id=?",
    ]
    params: list = [research_run_id]
    if factor_id:
        sql.append("AND factor_id=?")
        params.append(factor_id)
    sql.append("ORDER BY factor_id, cross_sectional_rank DESC, instrument_id")
    return [dict(r) for r in con.execute(" ".join(sql), params).fetchall()]
