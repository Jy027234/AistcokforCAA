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
from typing import Callable

from ..data.db import write_tx


# 只作为迁移前调用方的兼容回退。正常读取必须以 feature_version 表为准，
# 这样撤回状态不会随着 Python 发布包变化而丢失。
LEGACY_WITHDRAWN_FEATURE_VERSIONS: dict[str, str] = {
    "f10-v1": "F10 v1 的累计报表 TTM 公式漏加上一完整年度，结果已撤回",
}


def feature_version_metadata(con: sqlite3.Connection,
                             feature_version: str | None) -> dict | None:
    """读取持久化的特征版本登记。"""

    if not feature_version:
        return None
    row = con.execute(
        "SELECT feature_version, factor_id, status, validity_status, "
        "withdrawal_reason, registered_at, notes "
        "FROM feature_version WHERE feature_version=?", (feature_version,)
    ).fetchone()
    return dict(row) if row is not None else None


def feature_version_withdrawal_reason(
        feature_version: str | None,
        con: sqlite3.Connection | None = None) -> str | None:
    """返回撤回理由；有连接时以数据库注册状态为唯一事实来源。"""

    if con is not None:
        metadata = feature_version_metadata(con, feature_version)
        return (str(metadata["withdrawal_reason"])
                if metadata and metadata.get("withdrawal_reason") else None)
    return LEGACY_WITHDRAWN_FEATURE_VERSIONS.get(feature_version or "")


def research_run_metadata(con: sqlite3.Connection,
                          research_run_id: str) -> dict | None:
    """返回研究运行及其科学有效性，供显式 run_id 读取和 API 留痕。"""

    row = con.execute(
        "SELECT rr.research_run_id, rr.feature_version, rr.status, "
        "       rr.output_hash, fv.status AS version_status, "
        "       COALESCE(rv.validity_status, fv.validity_status, 'UNVERIFIED') "
        "           AS validity_status, "
        "       COALESCE(rv.withdrawal_reason, fv.withdrawal_reason) "
        "           AS withdrawal_reason "
        "FROM research_run AS rr "
        "LEFT JOIN feature_version AS fv "
        "  ON fv.feature_version=rr.feature_version "
        "LEFT JOIN research_run_feature_validity AS rv "
        "  ON rv.research_run_id=rr.research_run_id "
        "WHERE rr.research_run_id=?", (research_run_id,)
    ).fetchone()
    if row is None:
        return None
    out = dict(row)
    # A legacy database without the migration can still be read explicitly;
    # it is never considered valid for the default snapshot view.
    if not out.get("withdrawal_reason"):
        out["withdrawal_reason"] = feature_version_withdrawal_reason(
            out.get("feature_version"))
    return out


def _ensure_feature_version(con: sqlite3.Connection,
                            feature_version: str) -> dict:
    """登记未知版本为 UNVERIFIED，避免运行与版本状态脱钩。"""

    metadata = feature_version_metadata(con, feature_version)
    if metadata is None:
        con.execute(
            "INSERT INTO feature_version "
            "(feature_version, factor_id, status, validity_status, "
            " withdrawal_reason, registered_at, notes) "
            "VALUES (?,NULL,'UNVERIFIED','UNVERIFIED',NULL,?,?)",
            (feature_version, _now(), "运行创建时发现，尚未完成科学有效性登记"),
        )
        metadata = feature_version_metadata(con, feature_version)
    assert metadata is not None
    return metadata


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
                        notes: str | None = None,
                        write_guard: Callable[[], None] | None = None) -> str:
    """开一次研究运行。绑定快照与时点——缺了它们排名无法解释。"""

    known = con.execute("SELECT 1 FROM snapshot WHERE snapshot_id=?",
                        (snapshot_id,)).fetchone()
    if known is None:
        raise KeyError("unknown snapshot " + repr(snapshot_id))
    if not feature_version:
        raise ValueError("feature_version is required")

    run_id = "rr-" + hashlib.sha256(
        (snapshot_id + "|" + as_of_time.isoformat() + "|" + feature_version)
        .encode()).hexdigest()[:24]

    with write_tx(con):
        if write_guard is not None:
            write_guard()
        version = _ensure_feature_version(con, feature_version)
        con.execute(
            "INSERT INTO research_run (research_run_id,experiment_id,"
            "snapshot_id,as_of_time,code_version,strategy_version,feature_version,"
            "started_at,status,notes) VALUES (?,?,?,?,?,?,?,?,'RUNNING',?) "
            "ON CONFLICT(research_run_id) DO UPDATE SET "
            "experiment_id=excluded.experiment_id, snapshot_id=excluded.snapshot_id, "
            "as_of_time=excluded.as_of_time, code_version=excluded.code_version, "
            "strategy_version=excluded.strategy_version, "
            "feature_version=excluded.feature_version, started_at=excluded.started_at, "
            "status='RUNNING', finished_at=NULL, output_hash=NULL, notes=excluded.notes",
            (run_id, experiment_id, snapshot_id, as_of_time.isoformat(),
             code_version, strategy_version, feature_version, _now(), notes))
        con.execute(
            "INSERT INTO research_run_feature_validity "
            "(research_run_id,feature_version,validity_status,withdrawal_reason,"
            " associated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(research_run_id) DO UPDATE SET "
            "feature_version=excluded.feature_version, "
            "validity_status=excluded.validity_status, "
            "withdrawal_reason=excluded.withdrawal_reason, "
            "associated_at=excluded.associated_at",
            (run_id, feature_version, version["validity_status"],
             version.get("withdrawal_reason"), _now()),
        )
    return run_id


def store_factor_values(con: sqlite3.Connection, *, research_run_id: str,
                        values: list[FactorValue],
                        write_guard: Callable[[], None] | None = None) -> dict:
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
        if write_guard is not None:
            write_guard()
        # A deterministic research_run_id may be recomputed after a recovered
        # attempt or with a narrower explicit universe.  Replace the complete
        # result set so rows absent from the new run cannot survive as stale
        # factor values from an earlier attempt.
        con.execute(
            "DELETE FROM feature_value WHERE research_run_id=?",
            (research_run_id,),
        )
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
    output_hash = _output_hash(values)
    return {
        "research_run_id": research_run_id,
        "stored": len(values),
        "valued": counted,
        "excluded": len(values) - counted,
        "factors": sorted(by_factor),
        "output_hash": output_hash,
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


def factor_values_for_snapshot(con: sqlite3.Connection, *, snapshot_id: str,
                               instrument_id: str | None = None) -> tuple[list[dict], str | None]:
    """某个快照上**最近一次成功的研究运行**的因子值。

    为什么需要这个入口：`factor_values` 要求调用方先知道 research_run_id，
    而研究卡的使用者只知道"哪个快照、哪只股票"——他不知道也不需要知道
    运行 ID。产品侧（`/api/v1/instruments/{id}/research`）因此一直没把一个
    `research_run_id` 传进 `build_research_card`，卡片上的因子区永远是空的：
    **库里有值，界面上没有**，而两侧各自的测试都是绿的。

    选"最近一次**成功**"而不是"最近一次"：失败或仍在 RUNNING 的运行没有
    因子值，若按时间取最新，一次失败就会让卡片把所有数值藏起来——
    而失败的那次运行并没有让上一次的结果失效。

    返回值第二项是**给人看的说明**：没有运行、或该标的被质量门排除时，
    调用方要如实展示原因，不能显示成"值为空"。
    """

    runs = con.execute(
        "SELECT rr.research_run_id, rr.feature_version, rr.status, rr.started_at, "
        "       fv.status AS version_status, rv.validity_status, "
        "       COALESCE(rv.withdrawal_reason, fv.withdrawal_reason) "
        "           AS withdrawal_reason "
        "FROM research_run AS rr "
        "JOIN research_run_feature_validity AS rv "
        "  ON rv.research_run_id=rr.research_run_id "
        "JOIN feature_version AS fv ON fv.feature_version=rv.feature_version "
        "WHERE rr.snapshot_id=? AND rr.status='SUCCEEDED' "
        "ORDER BY rr.started_at DESC", (snapshot_id,)).fetchall()
    run = next((candidate for candidate in runs
                if candidate["version_status"] == "ACTIVE"
                and candidate["validity_status"] == "VALID"), None)
    if run is None:
        if runs:
            retired = sorted({str(r["feature_version"]) for r in runs
                              if r["validity_status"] == "WITHDRAWN"})
            if retired:
                return [], ("该快照只有已撤回的因子版本：" + ", ".join(retired)
                            + "。请用当前公式重新运行因子作业；旧记录仅供审计。")
            return [], "该快照上的因子运行尚未通过科学有效性校验；请使用当前有效版本重新运行。"
        any_run = con.execute(
            "SELECT COUNT(*) FROM research_run WHERE snapshot_id=?",
            (snapshot_id,)).fetchone()[0]
        if any_run:
            return [], ("该快照上已有研究运行，但没有一次成功；"
                        "因子数值只能来自成功的研究运行。")
        return [], ("该快照上尚未计算任何因子。"
                    "如需卡片数值，请在快照上运行因子作业："
                    "`python tools/compute_factors.py`（或 "
                    "POST /api/v1/research/jobs 的 f10 作业）。")

    rows = con.execute(
        "SELECT instrument_id, factor_id, raw_value, cross_sectional_rank, "
        "       exclusion_reason, coverage_ratio FROM feature_value "
        "WHERE research_run_id=? ORDER BY factor_id, instrument_id",
        (run["research_run_id"],)).fetchall()
    out: list[dict] = []
    for r in rows:
        if instrument_id is not None and r["instrument_id"] != instrument_id:
            continue
        out.append({
            "instrument_id": r["instrument_id"],
            "research_run_id": run["research_run_id"],
            "factor_id": r["factor_id"],
            "feature_version": run["feature_version"],
            "validity_status": run["validity_status"],
            "withdrawal_reason": run["withdrawal_reason"],
            # 视图层的命名约定（见 build_research_card 的 breakdown）
            "value": r["raw_value"],
            "rank_pct": r["cross_sectional_rank"],
            "coverage": r["coverage_ratio"],
            "exclusion_reason": r["exclusion_reason"],
        })
    note = None
    if instrument_id is not None and not out:
        note = ("该快照上有成功的研究运行，但本标的没有因子值"
                "（多半被质量门排除：见排除原因的取值表）。")
    return out, note
