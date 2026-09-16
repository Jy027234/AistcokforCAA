"""§3.1 默认可模拟池：上市/退市日期**不能反过来当排除条件**。

为什么这条用例存在
------------------
`Instrument.is_simulatable` 曾经写着：

    if self.listed_on is not None:
        return False   # 上市天数门槛由组合构建层按交易日计算

即「只要知道上市日期，就判为不可模拟」。注释说明了理由（门槛在别处算），
但代码做的是另一件事：它把一条**信息**当成了**排除条件**。

这条缺陷长期不可见，因为免费源没有采集上市日期，`listed_on` 恒为 None，
那段分支从未执行过。一旦补上数据源（BaoStock 的 ipoDate），
整个池子会在一瞬间变成空的，而所有既有测试仍然全绿——
它们都用 listed_on=None 的标的。

这与「数据拿得到但没被用上」是同一类问题的镜像：
**数据一旦拿到，就会被误解。**
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.data.instruments import (  # noqa: E402
    Exchange,
    Instrument,
    SecurityStatus,
    StatusVersion,
)
from aquant.domain.data.instruments import Board  # noqa: E402


def _main_board(**kwargs) -> Instrument:
    defaults = dict(
        instrument_id="SYN.A.600519",
        exchange=Exchange.SSE,
        board=Board.MAIN,
        status_history=(StatusVersion(valid_from=date(2010, 1, 1), valid_to=None,
                                      name="合成示例", status=SecurityStatus.LISTED),),
    )
    defaults.update(kwargs)
    return Instrument(**defaults)


def test_known_listing_date_does_not_exclude_the_instrument():
    """知道上市日期 ≠ 不可模拟。这是修复前会失败的那一条。"""

    inst = _main_board(listed_on=date(2001, 8, 27))
    assert inst.is_simulatable(date(2026, 9, 11))


def test_missing_listing_date_is_not_a_reason_to_include_or_exclude():
    """缺上市日期时沿用既有行为（可模拟），门槛由组合构建层负责。"""

    assert _main_board().is_simulatable(date(2026, 9, 11))


def test_decision_before_the_listing_date_is_not_simulatable():
    """决策时点早于上市日：那时它还不是可交易证券。

    这条不是「上市天数门槛」的替代——它只排除**根本还没上市**的时点。
    """

    inst = _main_board(listed_on=date(2026, 9, 10))
    assert not inst.is_simulatable(date(2026, 9, 9))
    assert inst.is_simulatable(date(2026, 9, 10))   # 上市当日起可模拟


def test_delisting_still_bounds_the_simulatable_window():
    """退市日的语义保持不变（左闭右开：退市当日不可模拟）。"""

    inst = _main_board(listed_on=date(2001, 8, 27), delisted_on=date(2026, 1, 5))
    assert inst.is_simulatable(date(2026, 1, 4))
    assert not inst.is_simulatable(date(2026, 1, 5))


def test_listing_age_threshold_is_not_decided_here():
    """上市天数门槛必须在**交易日**口径下判断，本方法没有日历，故不判。

    如果把 120 个自然日近似成 120 个交易日，会静默地多留下新股；
    反之若在这里硬拒，就回到了修复前的错误。因此这里只固定契约：
    刚上市一天的主板股票在**状态上**是可模拟的。
    """

    inst = _main_board(listed_on=date(2026, 9, 10))
    assert inst.is_simulatable(date(2026, 9, 11))


@pytest.mark.parametrize("board", [Board.GEM, Board.STAR, Board.BSE])
def test_non_main_boards_stay_out_of_the_default_pool(board):
    """§3.1：创业板/科创板/北交所首期可展示但不进入默认可模拟池。"""

    inst = _main_board(board=board, listed_on=date(2010, 1, 1))
    assert not inst.is_simulatable(date(2026, 9, 11))

