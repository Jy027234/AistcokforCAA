"""模型外发闸门的验收（§17.2、§826、ADR-005）。

这个测试保护的是**唯一**的外发出口。它必须证明三件事：

  1. 未确认来源**发不出去**（默认拒绝，不是默认放行）；
  2. 未登记来源会**报错**而不是被当成"没开"；
  3. 个人信息默认不进入模型上下文，且"未声明"等同于"含"。

第 3 条容易被写反：直觉上"没声明没有个人信息"就该放行。
但 §826 要求个人信息默认不进入，因此不对称默认是**必须**的——
未声明等同于含，除非显式声明不含。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.ai.egress import (  # noqa: E402
    EgressDenied, EgressItem, assert_egress_allowed, build_model_context, evaluate,
)
from aquant.domain.data.rights import Rights, default_rights  # noqa: E402


def item(source: str, text: str = "材料正文", *, personal: bool | None = False):
    return EgressItem(source_id=source, text=text, purpose="测试",
                      contains_personal_data=personal)


# ==================================================== 默认拒绝
def test_all_market_data_sources_are_denied_by_default():
    """全部真实数据源默认不得外发。"""

    reg = default_rights()
    for entry in reg.all():
        if entry.source_id == "synthetic-fixture":
            continue
        assert not entry.can_enter_model_context(), (
            f"{entry.source_id} 竟然允许模型处理——默认必须是拒绝"
        )


def test_real_market_source_is_blocked_with_actionable_error():
    decision = evaluate([item("baostock")])
    assert not decision.allowed
    blocker = decision.blockers[0]
    assert blocker["right"] == "model_processing"
    assert "baostock" in blocker["detail"]

    with pytest.raises(EgressDenied) as exc:
        build_model_context([item("baostock")])
    assert "修复" in exc.value.as_error()["repair_action"] or \
        "确认来源条款" in exc.value.as_error()["repair_action"]


def test_synthetic_fixture_is_allowed():
    """合成数据是本资料包自带的，可用于包括模型处理的任何用途。"""

    decision = evaluate([item("synthetic-fixture")])
    assert decision.allowed, decision.blockers
    text = build_model_context([item("synthetic-fixture", "合成材料")])
    assert "source: synthetic-fixture" in text


# ============================================ 整批拒绝，不是静默过滤
def test_a_single_blocked_item_rejects_the_whole_batch():
    """一批里有一项不允许，整批拒绝。

    静默过滤掉不允许的部分会让调用方以为"材料都用上了"，
    从而给出一个基于残缺输入却看起来完整的结论。
    """

    with pytest.raises(EgressDenied):
        build_model_context([item("synthetic-fixture"),
                             item("tencent-ifzq")])


# ============================================ 未登记来源必须报错
def test_unregistered_source_raises_instead_of_defaulting_open():
    """未登记来源不得被当成"登记了但没开"，也不得默认为开。"""

    with pytest.raises(KeyError) as exc:
        evaluate([item("some-brand-new-scraper")])
    assert "no rights entry" in str(exc.value)


# ============================================ 个人信息的不对称默认
def test_personal_data_undeclared_is_treated_as_present():
    """未声明是否含个人信息 = 视为含，拒绝外发。"""

    undeclared = EgressItem(source_id="synthetic-fixture", text="材料")
    assert undeclared.contains_personal_data is None
    decision = evaluate([undeclared])
    assert not decision.allowed
    assert decision.blockers[0]["right"] == "personal_data"


def test_personal_data_explicitly_present_is_blocked_even_for_allowed_source():
    decision = evaluate([item("synthetic-fixture", personal=True)])
    assert not decision.allowed
    assert decision.blockers[0]["right"] == "personal_data"


# ==================================== 放行需要显式改登记表（可复核的动作）
def test_granting_requires_an_explicit_registry_change():
    """放行只能通过改登记表实现，且改后其它来源仍然被拒。"""

    reg = default_rights()
    granted = reg.with_right("baostock", "model_processing", Rights.ALLOWED)

    assert evaluate([item("baostock")], registry=granted).allowed
    # 其它来源不受影响
    assert not evaluate([item("tencent-ifzq")], registry=granted).allowed
    # 原登记表不变（with_right 返回新对象，不改原对象）
    assert not reg.get("baostock").can_enter_model_context()


def test_prohibited_and_unknown_both_deny_but_are_distinguishable():
    """PROHIBITED 与 UNKNOWN 都拒绝，但必须能分辨——前者不能再争取。"""

    reg = default_rights()
    unknown = reg.get("tencent-ifzq").rights["model_processing"]
    assert unknown is Rights.UNKNOWN

    prohibited = reg.with_right("tencent-ifzq", "model_processing",
                                Rights.PROHIBITED)
    entry = prohibited.get("tencent-ifzq")
    assert not entry.can_enter_model_context()
    assert entry.rights["model_processing"] is Rights.PROHIBITED
