"""研究运行与因子落库的验收（§10.2）。

重点在两件容易被写错的事：

  1. **同分排名必须确定性**。排名是选股的直接依据，
     如果同分时按字典顺序给不同名次，同一份数据两次运行会得到
     不同排名——而这种不稳定不会报错。
  2. **缺失值不参与排名**。把"算不出"的标的也放进排名，
     等于给缺失值一个名次，那是最隐蔽的一种 0 填充（§10.2 禁止）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.research.runs import (  # noqa: E402
    FactorValue, cross_sectional_ranks,
)


# ==================================================== 排名
def test_ranks_are_descending_percentiles():
    ranks = cross_sectional_ranks({"A": 0.10, "B": 0.05, "C": 0.01})
    assert ranks["A"] == 1.0
    assert ranks["C"] == 0.0
    assert ranks["A"] > ranks["B"] > ranks["C"]


def test_ties_get_the_average_rank():
    """同分取平均名次，而不是任意先后。"""

    ranks = cross_sectional_ranks({"A": 0.10, "B": 0.05, "C": 0.05, "D": 0.01})
    assert ranks["B"] == ranks["C"], "同分必须同名次"
    # B、C 占据第 2、3 名（0 起），平均 1.5；n=4 -> 1 - 1.5/3 = 0.5
    assert ranks["B"] == 0.5


def test_all_ties_give_everyone_the_same_rank():
    ranks = cross_sectional_ranks({"A": 0.05, "B": 0.05, "C": 0.05})
    assert len(set(ranks.values())) == 1


def test_single_element_is_rank_one():
    assert cross_sectional_ranks({"A": 0.1}) == {"A": 1.0}


def test_empty_input_returns_empty():
    assert cross_sectional_ranks({}) == {}


def test_ranks_are_deterministic_regardless_of_insertion_order():
    """同一份数据、不同插入顺序，排名必须完全一致。"""

    first = cross_sectional_ranks({"A": 0.05, "B": 0.05, "C": 0.10})
    second = cross_sectional_ranks({"C": 0.10, "B": 0.05, "A": 0.05})
    assert first == second


# ============================================ 因子值的互斥约束
def test_factor_value_must_carry_value_or_reason():
    """有值或无原因、无值无原因都不允许——那是一行无法解释的数据。"""

    FactorValue(instrument_id="A", factor_id="F10", raw_value=0.05)
    FactorValue(instrument_id="A", factor_id="F10", raw_value=None,
                exclusion_reason="缺财务数据")

    with pytest.raises(ValueError):
        FactorValue(instrument_id="A", factor_id="F10", raw_value=None)
    with pytest.raises(ValueError):
        FactorValue(instrument_id="A", factor_id="F10", raw_value=0.05,
                    exclusion_reason="同时也给了原因")
