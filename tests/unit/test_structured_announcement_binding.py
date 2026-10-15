"""绑定工具（历史重建生产端）与 readiness 消费端的契约。

方案 C 包要求"有原始档案证据的有限区间可回放"。消费端
（``admissible_facts`` 的模式分支）早已实现并有测试，但**此前从未有任何东西
产出过 ``HISTORICAL_RECONSTRUCTED`` 事实**——生产端是缺的。
``tools/bind_structured_to_announcements.py`` 补上了这一端：它把结构化值绑到
巨潮公告时点，并按仓内约定由公告日推导 ``available_at``。

这个测试锁住两端的衔接。**任何一端改动都会让它失败**，这正是目的：

* 工具写出的 ``pit_mode`` / ``availability_basis`` 字符串必须是消费端枚举的
  名字（消费端用 ``is`` 对枚举成员做同一性比较，字符串对不上就静默落进
  "UNKNOWN" 分支被排除）；
* 用工具声明的值构造的事实必须被 ``allow_reconstructed=True`` 放行，
  且**不被首见过滤挡住**（历史分支刻意不做首见约束）。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from aquant.domain.data.pit import AvailabilityBasis, PitMode, PitViolation
from aquant.domain.fundamentals.versioned import (
    FinancialFact, StatementScope,
)
from aquant.operations.s2_readiness import admissible_facts

TOOL = Path(__file__).resolve().parents[2] / "tools" / "bind_structured_to_announcements.py"

UTC = timezone.utc
#: 公告日在 cutoff 之前，但**首见在 cutoff 之后**：历史重建必须仍被放行。
ANNOUNCED = date(2026, 8, 26)
AVAILABLE_AT = datetime(2026, 8, 27, 1, 0, tzinfo=UTC)
FIRST_SEEN = datetime(2026, 9, 29, 0, 30, tzinfo=UTC)
CUTOFF = datetime(2026, 9, 24, 7, 0, tzinfo=UTC)


def _declared(name: str) -> str:
    """读出工具里 ``entry["<name>"] = "VALUE"`` 实际写入的字符串。"""

    source = TOOL.read_text(encoding="utf-8")
    match = re.search(rf'entry\["{name}"\]\s*=\s*"([A-Z_]+)"', source)
    assert match, f"{name} is not declared as a literal in the binding tool"
    return match.group(1)


def test_binding_tool_declares_consumer_compatible_pit_identifiers() -> None:
    # 消费端只认这些名字；工具写别的字符串会让事实悄悄落进 UNKNOWN 被排除。
    assert _declared("pit_mode") == PitMode.HISTORICAL_RECONSTRUCTED.name
    assert _declared("availability_basis") == AvailabilityBasis.RECONSTRUCTED.name


def test_bound_fact_is_admitted_only_when_reconstruction_is_requested() -> None:
    fact = FinancialFact(
        instrument_id="000021", metric="revenue", period_end=date(2026, 6, 30),
        statement_scope=StatementScope.CONSOLIDATED, profit_scope=None,
        value=Decimal("1000"), currency="CNY", raw_unit="yuan",
        source_id="eastmoney-structured-f10", source_document_id="1223421477",
        source_published_date=ANNOUNCED, source_published_at=None,
        timestamp_precision="DATE", first_seen_at=FIRST_SEEN,
        ingested_at=FIRST_SEEN, available_at=AVAILABLE_AT,
        availability_basis=AvailabilityBasis[_declared("availability_basis")],
        pit_mode=PitMode[_declared("pit_mode")],
        content_hash="sha256:" + "c" * 64, version_id="v-reconstructed",
    )
    forward, _ = admissible_facts(facts=[fact], cutoff=CUTOFF,
                                  allow_reconstructed=False)
    historical, rejected = admissible_facts(facts=[fact], cutoff=CUTOFF,
                                            allow_reconstructed=True)
    assert forward == []
    assert historical == [fact]
    assert rejected == []
    # 首见在 cutoff 之后——若历史分支误加首见过滤，这一条会掉出去。
    assert fact.first_seen_at > CUTOFF


def test_reconstructed_basis_is_not_accepted_under_forward_mode() -> None:
    """反向守卫：重建依据不能挂到前向模式上。

    这一组合在**域层就被拒绝**（``available_at`` 早于 ``first_seen_at`` 的
    "前向观测"不成立），所以连事实都构造不出来——比"构造出来之后再被筛掉"
    是更强的保证。这里锁住这个拒绝，防止有人把校验放松掉。
    """

    with pytest.raises(PitViolation) as caught:
        FinancialFact(
            instrument_id="000021", metric="revenue", period_end=date(2026, 6, 30),
            statement_scope=StatementScope.CONSOLIDATED, profit_scope=None,
            value=Decimal("1000"), currency="CNY", raw_unit="yuan",
            source_id="eastmoney-structured-f10", source_document_id="1223421477",
            source_published_date=ANNOUNCED, source_published_at=None,
            timestamp_precision="DATE", first_seen_at=FIRST_SEEN,
            ingested_at=FIRST_SEEN, available_at=AVAILABLE_AT,
            availability_basis=AvailabilityBasis.RECONSTRUCTED,
            pit_mode=PitMode.LIVE_OBSERVED,
            content_hash="sha256:" + "d" * 64, version_id="v-mismatched",
        )
    assert "available_at precedes first_seen_at" in str(caught.value)
