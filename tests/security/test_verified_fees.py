"""经验证费率表与"合成费率不得用于真实数据"的闸门（§12.6）。

这个文件保护的是一个曾经**只在测试里存在**的规则：
assert_usable_for_formal_research() 写了、也配了测试，
但生产路径上没有任何地方调用它——于是真实快照上的成交与盈亏
一直在用合成费率计算。**数字算得出来，只是没有依据。**

因此这里有两个层次：

  1. 费率表本身：法定/行业标准费率与出处对得上，佣金必须是参数；
  2. **闸门真的会被执行**：真实数据上走一次 preview 就会被拒。
     第 2 条是关键——第 1 条再正确，没有执行路径也等于不存在。
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.simulation.fees import (  # noqa: E402
    FeeError, synthetic_fee_table,
)
from aquant.domain.simulation.verified_fees import (  # noqa: E402
    STAMP_DUTY_EFFECTIVE_FROM, STAMP_DUTY_RATE_SELL, TRANSFER_FEE_NOTICE,
    TRANSFER_FEE_RATE, fee_table_from_env, load_fee_sources, provenance,
    verified_fee_table,
)

DAY = date(2026, 9, 8)


def table():
    return verified_fee_table(commission_rate=Decimal("0.00025"),
                              commission_min_cents=500)


# ============================================================ 费率表本身
def test_verified_table_is_not_marked_synthetic():
    assert table().is_synthetic is False


def test_statutory_rates_match_the_recorded_notices():
    s = table().schedule_for(DAY)
    assert s.stamp_duty_rate_sell == STAMP_DUTY_RATE_SELL == Decimal("0.0005")
    assert s.transfer_fee_rate == TRANSFER_FEE_RATE == Decimal("0.00001")


def test_stamp_duty_history_is_kept():
    """§7.1 按生效日取用相应配置：减半之前必须仍有档位可匹配。"""

    before = table().schedule_for(date(2023, 1, 10))
    assert before.stamp_duty_rate_sell == Decimal("0.001")
    assert before.effective_to == STAMP_DUTY_EFFECTIVE_FROM


def test_commission_is_a_parameter_not_a_default():
    """佣金是券商约定，没有"正确值"——必须显式给出，且不设默认。"""

    with pytest.raises(TypeError):
        verified_fee_table(commission_min_cents=500)      # type: ignore[call-arg]


def test_wrong_unit_is_rejected():
    """把万分之 2.5 写成 0.025 是最常见的填错方式，必须被拦。"""

    with pytest.raises(ValueError) as exc:
        verified_fee_table(commission_rate=Decimal("0.025"),
                           commission_min_cents=500)
    assert "千分之三" in str(exc.value)


def test_float_rate_is_rejected():
    with pytest.raises(ValueError):
        verified_fee_table(commission_rate=0.00025,       # type: ignore[arg-type]
                           commission_min_cents=500)


@pytest.mark.parametrize(
    ("rate", "minimum", "missing"),
    [
        ("0.00025", None, "AQUANT_COMMISSION_MIN_CENTS"),
        (None, "500", "AQUANT_COMMISSION_RATE"),
    ],
)
def test_partial_env_commission_is_not_user_configured(
    monkeypatch, rate, minimum, missing,
):
    """佣金率和最低佣金必须成对配置，缺一不能被当成真实费率。"""

    for name in ("AQUANT_COMMISSION_RATE", "AQUANT_COMMISSION_MIN_CENTS"):
        monkeypatch.delenv(name, raising=False)
    if rate is not None:
        monkeypatch.setenv("AQUANT_COMMISSION_RATE", rate)
    if minimum is not None:
        monkeypatch.setenv("AQUANT_COMMISSION_MIN_CENTS", minimum)

    fees, note = fee_table_from_env()
    assert fees.commission_source == "UNCONFIGURED_DEFAULT"
    assert missing in note
    with pytest.raises(FeeError):
        fees.assert_usable_for_data_mode("PRODUCTION", trading_day=DAY)


def test_provenance_names_an_authority_for_every_non_contractual_item():
    """每一项费率要么有出处，要么明确标为"必须由使用者给"。"""

    for item in provenance():
        if item["kind"] == "CONTRACTUAL":
            assert item["value"] is None and item["authority"] is None
            assert "无权威值" in item["note"]
        else:
            assert item["authority"], item
            assert item["value"], item


def test_provenance_refers_to_a_recorded_source_when_archive_exists():
    """有来源档案时，出处必须能对到档案里的记录——否则就是无依据的数字。"""

    archive = load_fee_sources()
    if not archive.get("sources"):
        pytest.skip("来源档案尚未生成（跑 tools/fetch_fee_sources.py）")
    recorded = {s["key"] for s in archive["sources"]}
    assert "stamp_duty_2023" in recorded
    assert "transfer_fee_2022" in recorded
    assert any(s["kind"] == "CONTRACTUAL" and not s["url"]
               for s in archive["sources"]), "佣金项必须明确记为无来源"


def test_archived_evidence_bytes_match_their_recorded_hash():
    """留证文件必须与档案里记录的哈希一致。

    这批文件是**证据**：行尾归一化、编辑器自动格式化、任何一次
    "顺手整理一下"都会让哈希对不上，而档案里的哈希看起来仍然正确——
    那是最糟的状态：证据看起来还在，实际已经不是那一份了。
    """

    import hashlib

    archive = load_fee_sources()
    checked = 0
    for s in archive.get("sources") or []:
        if not s.get("fetched") or not s.get("contentHash") or not s.get("archivedAt"):
            continue
        path = ROOT / s["archivedAt"]
        assert path.exists(), f"档案里记着 {s['archivedAt']}，文件却不存在"
        actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == s["contentHash"], (
            f"{s['key']} 的留证字节已变：记录 {s['contentHash'][:23]}，"
            f"实际 {actual[:23]}")
        checked += 1
    if checked == 0:
        pytest.skip("来源档案尚未抓取（跑 tools/fetch_fee_sources.py）")


# ============================================================ 闸门
def test_synthetic_table_is_blocked_on_production_data():
    with pytest.raises(FeeError) as exc:
        synthetic_fee_table().assert_usable_for_data_mode(
            "PRODUCTION", trading_day=DAY)
    assert exc.value.code == "FEE_VERSION_UNVERIFIED"
    assert "合成" in exc.value.message


def test_synthetic_table_is_allowed_on_synthetic_data():
    synthetic_fee_table().assert_usable_for_data_mode("SYNTHETIC", trading_day=DAY)


def test_verified_table_passes_on_production_data():
    table().assert_usable_for_data_mode("PRODUCTION", trading_day=DAY)


def test_unconfigured_non_synthetic_table_is_blocked_on_production_data(monkeypatch):
    """未配置表即使没有 synthetic 标记，也不能绕过真实数据闸门。"""

    monkeypatch.delenv("AQUANT_COMMISSION_RATE", raising=False)
    monkeypatch.delenv("AQUANT_COMMISSION_MIN_CENTS", raising=False)
    fees, _ = fee_table_from_env(commission_rate=None,
                                 commission_min_cents=None)
    assert fees.is_synthetic is False
    assert fees.commission_source == "UNCONFIGURED_DEFAULT"
    with pytest.raises(FeeError) as exc:
        fees.assert_usable_for_data_mode("PRODUCTION", trading_day=DAY)
    assert "用户确认" in exc.value.message


def test_mixed_table_is_treated_as_synthetic():
    """整张表里只要有**一档**是合成的，就按合成处理。

    混着的表最容易被忽略：查到的那天恰好是经验证档时，看起来一切正常。
    """

    from aquant.domain.simulation.fees import FeeSchedule, FeeTable

    mixed = FeeTable([
        FeeSchedule(fee_version="ok", effective_from=date(2020, 1, 1),
                    effective_to=date(2024, 1, 1),
                    commission_rate=Decimal("0.00025"), commission_min_cents=500,
                    stamp_duty_rate_sell=Decimal("0.0005"),
                    transfer_fee_rate=Decimal("0.00001"),
                    synthetic_test_rate=True),
        FeeSchedule(fee_version="real", effective_from=date(2024, 1, 1),
                    effective_to=None,
                    commission_rate=Decimal("0.00025"), commission_min_cents=500,
                    stamp_duty_rate_sell=Decimal("0.0005"),
                    transfer_fee_rate=Decimal("0.00001"),
                    synthetic_test_rate=False),
    ])
    assert mixed.is_synthetic is True
    with pytest.raises(FeeError):
        # 查的那天恰好是经验证档，但整张表不干净 -> 仍然拒绝
        mixed.assert_usable_for_data_mode("PRODUCTION", trading_day=date(2025, 6, 2))
