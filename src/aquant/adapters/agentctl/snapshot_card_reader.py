"""把 M1 快照存储适配成 agentctl 能力所需的只读形状。

为什么需要这一层
----------------
Q1 的能力 handler 原先读的是模块内写死的字典，注释自己写着
"Q0/Q1 的 stand-in for the real snapshot store that M1 will provide"。
M1 已经交付了，因此这一步是把替身换成真实读取。

但**不能直接在 handler 里 import M1**：那是把集成层与领域存储耦合起来，
而能力契约（agentctl 侧）要的是"给一个 snapshot_id 与 instrument_id，
返回一张卡片"这种形状。中间放一个适配器，两边各自稳定。

边界（ADR-011）
---------------
本模块在 `adapters/agentctl/` 下，**允许** import agentctl 与领域层；
反向（领域层 import agentctl）由 `tests/security/test_agentctl_boundary.py` 强制禁止。
能力 handler 本身不 import 本模块——它只依赖一个注入的"读取器"。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from aquant.domain.data.reader import SnapshotReader
from aquant.domain.research.runs import factor_values_for_snapshot


class SnapshotCardReader:
    """按 snapshot_id 回答"这张卡片是什么"。

    只读。任何一次缺失都返回**结构化的缺失说明**，不返回部分编造的数据——
    能力契约那侧会把它转成 §16.4 的错误码。
    """

    def __init__(self, con: sqlite3.Connection, reader: SnapshotReader) -> None:
        self.con = con
        self.reader = reader

    # ------------------------------------------------------------ 快照
    def snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        """已发布的快照引用。未发布或不存在返回 None（**不抛**——
        调用方要把它变成错误码，而不是 500）。"""

        try:
            ref = self.reader.ref(snapshot_id)
        except Exception:                                    # noqa: BLE001
            return None
        try:
            snap = self.reader.store.require_published(snapshot_id)
        except Exception:                                    # noqa: BLE001
            return None
        return {
            "snapshot_id": ref.snapshot_id,
            "as_of_time": ref.as_of_time.isoformat(),
            "data_mode": ref.data_mode,
            "watermark": ref.watermark,
            "quality": self._quality(snapshot_id),
        }

    def _quality(self, snapshot_id: str) -> dict[str, Any]:
        """行情覆盖度。用来回答"这份快照的数据完整吗"。

        用数据集**已登记的条数**算，不逐个标的读行情：后者在大快照上是
        几百次全量数据集查找（每次都要哈希校验），而结果完全一样——
        条数在发布时就写进了 manifest 的 record_count。

        只报能算出来的，不编造别的指标。
        """

        ref = self.reader.ref(snapshot_id)
        expected = self._expected_bars(snapshot_id, ref.as_of_time)
        recorded = {d["name"]: d.get("record_count")
                    for d in self.reader.store.datasets(snapshot_id)}
        bars = recorded.get("daily_quotes")
        coverage = (bars / expected) if (bars is not None and expected) else None
        missing: list[str] = []
        # 缺口超过 5% 才登记：停牌造成的零星缺失是正常的，
        # 把它当成"数据域缺失"会让这个字段永远非空，从而没人看。
        if coverage is not None and coverage < 0.95:
            missing.append("DAILY_QUOTES")
        return {"coverage": coverage, "bars": bars, "expected_bars": expected,
                "missing_domains": missing}

    def _expected_bars(self, snapshot_id: str, as_of: datetime) -> int | None:
        """理论条数 = 标的数 × 交易日数。缺任一项就返回 None（不猜）。"""

        try:
            instruments = self.reader.instruments(snapshot_id, as_of=as_of)
            days = self.reader.trading_calendar(snapshot_id, as_of=as_of)
        except Exception:                                    # noqa: BLE001
            return None
        if not instruments or not days:
            return None
        return len(instruments) * len(days)

    # ------------------------------------------------------------ 卡片
    def card(self, snapshot_id: str, instrument_id: str) -> dict[str, Any] | None:
        """一张研究卡片的原始素材。找不到该标的返回 None。"""

        snap = self.snapshot(snapshot_id)
        if snap is None:
            return None
        as_of = datetime.fromisoformat(snap["as_of_time"])
        instruments = {i["instrument_id"]: i
                       for i in self.reader.instruments(snapshot_id, as_of=as_of)}
        inst = instruments.get(instrument_id)
        if inst is None:
            return None

        factors, factor_note = self._factors(snapshot_id, instrument_id)
        return {
            "instrument_id": instrument_id,
            "exchange": inst.get("exchange", "OTHER"),
            "board": inst.get("board", "OTHER"),
            "display_name": inst.get("short_name") or instrument_id,
            "industry_code": inst.get("industry_code"),
            "industry_name": inst.get("industry_name"),
            "classification_version": inst.get("classification_version"),
            # status 不在证券记录顶层，而在 status_history 里（左闭右开，§7.1）。
            # 取"当下生效"的那一条；一条都没有时返回 UNKNOWN——
            # 不猜"上市"，那会把"状态未知"说成"正常交易"。
            "status": self._current_status(inst, as_of),
            "listed_on": inst.get("listed_on"),
            "factors": factors,
            "factor_note": factor_note,
        }

    @staticmethod
    def _current_status(inst: dict[str, Any], as_of: datetime) -> str:
        """取 as_of 时点生效的状态。历史条目是左闭右开区间。"""

        history = inst.get("status_history") or []
        day = as_of.date().isoformat()
        for version in history:
            start = version.get("valid_from")
            end = version.get("valid_to")
            if start and day < str(start):
                continue
            if end and day >= str(end):
                continue
            return str(version.get("status") or "UNKNOWN")
        return "UNKNOWN"

    def _factors(self, snapshot_id: str,
                 instrument_id: str) -> tuple[list[dict], str | None]:
        """该标的在**该快照上**已计算的因子值。

        没算过就返回空表并说明原因——**不现算**：
        能力 handler 是只读的，在这里跑一遍全市场因子计算会让一次
        "读一张卡片"变成几秒的写操作，而且它会把 feature_value 写进库里，
        与"只读能力"的契约冲突。
        """

        return factor_values_for_snapshot(
            self.con, snapshot_id=snapshot_id, instrument_id=instrument_id)
