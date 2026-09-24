"""策略版本注册（§10.4、§13.2）。

为什么实验登记需要它
------------------
`experiment.strategy_version` 有外键约束，指向 `strategy_version` 表。
这不是形式要求：**登记一个从未冻结过的策略版本，实验就无法复现**——
事后没人知道当时跑的是哪份参数。

因此本模块提供显式注册，并且是幂等的：同一个版本重复注册不报错，
但**内容不同会报错**，而不是静默覆盖——静默覆盖会让已登记的实验
指向一份从未跑过的参数。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from ..data.db import write_tx

#: §10.4 的策略族。S1 是价格基线；S2 是质量-估值挑战者
#: （当前数据源下做不成，见 ADR-005）。
FAMILIES = ("S1", "S2", "E1", "CUSTOM")

# S2 不是一个只差实现开关的策略。官方原文的 F07--F09 数值、
# 报告版本与覆盖尚未形成可运行的产品事实链。官方披露来源在本机研究
# 与存储的权利状态已单独登记，故这里不能把数据未就绪误报成来源无权限。
# 允许登记 S2 版本会把“名字合法”误写成“策略可运行”。
# 这里把关闭状态放在领域层，避免 API、脚本或未来 worker 绕过同一规则。
_CLOSED_FAMILIES: dict[str, dict[str, str]] = {
    "S2": {
        "code": "DATA_NOT_READY",
        "message": (
            "S2 is disabled: complete official F07-F09 financial facts, "
            "announcement versions and eligible-universe coverage are not yet validated"
        ),
        "repair_action": (
            "ingest and verify official reports for the required periods, "
            "freeze their availability and revision evidence, then register "
            "a new immutable S2 version"
        ),
    },
}

_FAMILY_TRIAL_IMPACT: dict[str, dict] = {
    "S2": {
        "blocksInitialS1Trial": False,
        "blockedCapabilities": [
            "S2_STRATEGY_REGISTRATION",
            "S1_VS_S2_COMPARISON",
            "QUALITY_FACTOR_INCREMENT_CLAIMS",
        ],
        "availableCapabilities": [
            "S1_PRICE_RESEARCH",
            "DAILY_SNAPSHOTS",
            "RESEARCH_CARDS",
            "MANUAL_SIMULATION",
            "PORTFOLIO_LEDGER",
        ],
    },
}


class StrategyVersionError(Exception):
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


def spec_hash(spec: dict) -> str:
    """参数哈希。同一份参数每次得到同一哈希，用于检测静默覆盖。"""

    body = json.dumps(spec, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def require_strategy_family_available(family: str, *, object_id: str) -> None:
    """拒绝仍处于数据能力关闭状态的策略族。"""

    gate = _CLOSED_FAMILIES.get(family)
    if gate is not None:
        raise StrategyVersionError(
            gate["code"], gate["message"], object_id, gate["repair_action"])


def require_strategy_version_available(con: sqlite3.Connection,
                                       strategy_version: str) -> None:
    """实验引用策略版本前再次检查，覆盖升级前遗留的 S2 记录。"""

    row = con.execute(
        "SELECT family FROM strategy_version WHERE strategy_version=?",
        (strategy_version,),
    ).fetchone()
    if row is None:
        raise StrategyVersionError(
            "DATA_NOT_READY", "unknown strategy version " + repr(strategy_version),
            strategy_version, "register an available immutable strategy version first")
    require_strategy_family_available(row["family"], object_id=strategy_version)


def strategy_family_gates() -> list[dict]:
    """给 API/UI 的机器可读关闭原因；不把可登记误称为可运行。"""

    return [
        {
            "family": family,
            "registrationAvailable": False,
            "error": {
                "code": gate["code"],
                "message": gate["message"],
                "repair_action": gate["repair_action"],
            },
            "trialImpact": _FAMILY_TRIAL_IMPACT.get(family),
        }
        for family, gate in sorted(_CLOSED_FAMILIES.items())
    ]


def ensure_strategy_version(con: sqlite3.Connection, *, strategy_version: str,
                            family: str, spec: dict,
                            parent_version: str | None = None,
                            notes: str | None = None) -> dict:
    """注册策略版本。幂等；**内容不同则报错**。"""

    if family not in FAMILIES:
        raise StrategyVersionError(
            "DATA_NOT_READY", "unknown family " + repr(family),
            strategy_version, "use one of: " + ", ".join(FAMILIES))
    require_strategy_family_available(family, object_id=strategy_version)

    digest = spec_hash(spec)
    existing = con.execute(
        "SELECT family, spec_hash, frozen_at FROM strategy_version "
        "WHERE strategy_version=?", (strategy_version,)).fetchone()

    if existing is not None:
        if (existing["family"] == family
                and existing["spec_hash"] == digest):
            return {"strategy_version": strategy_version, "created": False,
                    "spec_hash": digest, "frozen_at": existing["frozen_at"]}
        raise StrategyVersionError(
            "STALE_SNAPSHOT",
            ("strategy version " + repr(strategy_version)
             + " already exists with different content"),
            strategy_version,
            ("strategy versions are immutable; register a new version name "
             "instead of overwriting — otherwise registered experiments point "
             "at parameters that never ran"))

    with write_tx(con):
        con.execute(
            "INSERT INTO strategy_version (strategy_version,family,spec_json,"
            "spec_hash,frozen_at,parent_version,notes) VALUES (?,?,?,?,?,?,?)",
            (strategy_version, family,
             json.dumps(spec, ensure_ascii=False, sort_keys=True),
             digest, _now(), parent_version, notes))
    return {"strategy_version": strategy_version, "created": True,
            "spec_hash": digest, "frozen_at": _now()}


def strategy_versions(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute(
        "SELECT strategy_version,family,frozen_at,spec_hash,notes "
        "FROM strategy_version ORDER BY frozen_at DESC, strategy_version"
    ).fetchall()
    return [dict(r) for r in rows]


#: 已知的策略版本。S1 的三个因子来自主文档 §10.1：
#: F01 20 日动量、F02 60 日动量（跳过近 5 日）、F04 20 日波动率。
#: 权重取自规范 §10.3 的 S1 合成排名。
KNOWN_STRATEGY_SPECS: dict[str, tuple[str, dict]] = {
    "s1-v1": ("S1", {
        "factors": ["F01", "F02", "F04"],
        "weights": {"momentum": 0.5, "low_volatility": 0.5},
        "momentum_split": {"F01": 0.5, "F02": 0.5},
        "rank_method": "percentile_average_ties",
        "note": "price-only baseline; no financial inputs",
    }),
}

#: 已知的特征版本。F10 的口径写在这里，便于实验登记引用。
KNOWN_FEATURE_SPECS: dict[str, dict] = {
    "f10-v1": {
        "factor_id": "F10",
        "formula": "归母净利润TTM / 时点总市值",
        "ttm_rule": "当期累计 − 上年同期累计",
        "pit_basis": "RECONSTRUCTED",
        "availability_rule": "公布日之后第一个交易日的盘前",
        "status": "WITHDRAWN",
        "withdrawal_reason": "累计报表 TTM 公式漏加上一完整年度",
    },
    "f10-v2": {
        "factor_id": "F10",
        "formula": "归母净利润TTM / 时点总市值",
        "ttm_rule": "上一完整年度 + 本年累计 − 上年同期累计；Q4 直接使用年度值",
        "pit_basis": "RECONSTRUCTED",
        "availability_rule": "公布日之后第一个交易日的盘前",
        "status": "ACTIVE",
    },
}


def seed_known_versions(con: sqlite3.Connection) -> dict:
    """把已知的策略与特征版本登记进去。幂等。"""

    created = []
    for name, (family, spec) in KNOWN_STRATEGY_SPECS.items():
        result = ensure_strategy_version(con, strategy_version=name,
                                         family=family, spec=spec)
        if result["created"]:
            created.append(name)
    return {"created": created,
            "strategyVersions": [r["strategy_version"]
                                 for r in strategy_versions(con)]}
