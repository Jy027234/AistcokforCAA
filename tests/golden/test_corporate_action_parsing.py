"""公司行为解析验收：送转、配股、退市（§12.7）。

重点不在"能解析"，而在两件容易做错的事：

  1. **送股与转增必须相加**。「每 10 股送 3 股转增 2 股」若只取一个，
     股数少算一半——而账面上完全看不出来。
  2. **未支持必须显式标出**。§12.7 要求配股、复杂换股、分拆出现时
     "停止产生新的有效绩效结论并列为待处理"。把"解析成功"当成
     "已正确处理"，会让一个不能核验的行为悄悄进入绩效。
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.simulation.corporate_action_parsing import (  # noqa: E402
    parse_bonus_and_transfer, parse_corporate_action, parse_delisting,
    parse_rights_issue,
)


def bonus(text: str, title: str = "某某2025年年度权益分派实施公告"):
    return parse_bonus_and_transfer(text=text, title=title,
                                    instrument_id="SH.600519",
                                    announcement_id="a1")


# ==================================================== 送转比例
def test_bonus_and_transfer_are_summed():
    """送 3 股 + 转增 2 股 = 每股 0.5，不是 0.3 或 0.2。"""

    result = bonus("每 10 股送 3 股转增 2 股。股权登记日为：2026 年 6 月 11 日，"
                   "除权除息日为：2026 年 6 月 12 日。")

    assert result is not None
    assert result.bonus_ratio == Decimal("0.5"), result.bonus_ratio
    assert result.action_type == "BONUS_SHARE"
    assert result.record_date == date(2026, 6, 11)
    assert result.ex_date == date(2026, 6, 12)


def test_bonus_only():
    result = bonus("每 10 股派送 5 股。")
    assert result is not None
    assert result.bonus_ratio == Decimal("0.5")


def test_transfer_only():
    result = bonus("每 10 股转增 8 股。")
    assert result is not None
    assert result.bonus_ratio == Decimal("0.8")


def test_fractional_bonus_ratio_is_preserved():
    """每 10 股送 2.5 股 -> 0.25，不能被取整。"""

    result = bonus("每 10 股送 2.5 股。")
    assert result is not None
    assert result.bonus_ratio == Decimal("0.25"), result.bonus_ratio


def test_chinese_numerals_are_parsed():
    result = bonus("每 10 股送三股转增二股。")
    assert result is not None
    assert result.bonus_ratio == Decimal("0.5")


def test_absolute_share_counts_are_not_treated_as_ratios():
    """「共送 5 亿股」不是比例，不能被当成每股 5 股。"""

    result = bonus("本次共送 5 亿股，股权登记日为：2026 年 6 月 11 日。")
    assert result is None, "没有「每 10 股」语境时不应解析出比例"


def test_cash_only_announcement_is_not_a_bonus_action():
    assert bonus("每股现金红利 0.50 元。") is None


# ============================================ 未支持：必须显式标出
def test_bonus_is_parsed_but_marked_unsupported():
    """比例解析出来了，但**处理**还没实现，必须标为未支持。

    "解析成功"与"已正确处理"是两件事；混淆会让不能核验的行为
    悄悄进入绩效结论。
    """

    result = bonus("每 10 股送 3 股。")
    assert result is not None
    assert result.bonus_ratio is not None, "比例应该解析出来"
    assert result.supported is False, "但处理未实现，必须标为未支持"
    assert "待处理" in result.reason


def test_rights_issue_is_always_unsupported():
    """§12.7：配股出现时停止产生新的绩效结论并列为待处理。"""

    result = parse_rights_issue(
        text="本次配股价格为人民币 8.50 元，股权登记日为：2026 年 6 月 11 日。",
        title="某某配股发行公告", instrument_id="SH.600519",
        announcement_id="r1")

    assert result is not None
    assert result.action_type == "RIGHTS_ISSUE"
    assert result.supported is False
    assert result.rights_price_cents == 850
    assert "待处理" in result.reason


def test_delisting_is_recorded_but_unsupported():
    """退市要记录，但终值口径不能默认按最后收盘价。"""

    result = parse_delisting(
        text="本公司股票自 2026 年 6 月 11 日起进入退市整理期。",
        title="某某终止上市公告", instrument_id="SH.600519",
        announcement_id="d1")

    assert result is not None
    assert result.action_type == "DELISTING"
    assert result.supported is False
    assert "终值" in result.reason


# ==================================================== 分派
def test_dispatch_picks_the_matching_parser():
    assert parse_corporate_action(
        text="本次配股价格为人民币 8.50 元。", title="某某配股发行公告",
        instrument_id="X", announcement_id="1").action_type == "RIGHTS_ISSUE"
    assert parse_corporate_action(
        text="每 10 股送 3 股。", title="某某2025年年度权益分派实施公告",
        instrument_id="X", announcement_id="2").action_type == "BONUS_SHARE"
    assert parse_corporate_action(
        text="无关内容。", title="股票交易异常波动公告",
        instrument_id="X", announcement_id="3") is None
