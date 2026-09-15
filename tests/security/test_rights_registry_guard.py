"""权利登记表的守卫：防止模型外发范围被静默改动。

为什么需要这个守卫
------------------
"哪些来源可以进模型"是一个**安全边界**，而它的改动不会产生任何报错：
把某个来源的 model_processing 从 UNKNOWN 改成 ALLOWED，程序照常运行，
只是从此以后真实数据会被发到第三方。反过来，误改回 UNKNOWN 会让
外发静默失败，而失败看起来像"这批材料没内容"。

因此这里把**当前已授权的来源清单**固定下来。任何变动都必须显式改这个
清单，而改清单是一个会被 code review 看到、也会出现在 git diff 里的动作。

同时区分两种依据：
  * USER_AUTHORIZED —— 使用者就该项直接表过态；
  * INFERRED_FROM_USER_INTENT —— 我方按其意图推论得出。
分开记录的理由：推论可能是错的，而"谁说的"决定了复核时该问谁。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.data.rights import Basis, Rights, default_rights  # noqa: E402

#: 当前允许进入模型上下文的来源。**改动此清单即改动安全边界**。
MODEL_EGRESS_SOURCES = {
    "baostock",        # 公开披露；依据为推论
    "cninfo",          # 公开披露；依据为推论
    "sse-site",        # 公开披露；依据为推论
    "szse-site",       # 公开披露；依据为推论
    "tencent-ifzq",    # 使用者直接授权
    "tencent-qt",      # 使用者直接授权
    "sina-hq",         # 使用者直接授权
    "eastmoney-direct",  # 使用者直接授权
    "synthetic-fixture",  # 本资料包自带，书面许可
}

#: 仍然不允许外发的权利项。这些不随"允许送进模型"一起放开。
STILL_CLOSED = ("third_party_redistribution", "commercial_use")


def test_model_egress_set_matches_the_authorised_list():
    """可外发来源必须与授权清单逐字一致。"""

    actual = {e.source_id for e in default_rights().all()
              if e.can_enter_model_context()}
    added = actual - MODEL_EGRESS_SOURCES
    removed = MODEL_EGRESS_SOURCES - actual
    assert not added, (
        f"这些来源被允许进模型但不在授权清单里：{sorted(added)}；"
        "若是有意放开，请同时更新 MODEL_EGRESS_SOURCES 与 "
        "docs/data-rights-register.md")
    assert not removed, (
        f"这些来源不再允许进模型：{sorted(removed)}；"
        "外发会静默失败，看起来像\"这批材料没内容\"")


def test_redistribution_and_commercial_use_stay_closed():
    """外部来源的再分发与商用不随模型外发一起放开。

    合成数据是我们的测试数据，是我们自己的。它的书面许可允许任何用途，
    因此**不适用**"交易所数据不可转售"这条理由——
    把豁免写清楚，比让断言为了通过而放宽要好。
    """

    own_data = {"synthetic-fixture"}
    for entry in default_rights().all():
        if entry.source_id in own_data:
            continue
        for purpose in STILL_CLOSED:
            assert entry.rights[purpose] is not Rights.ALLOWED, (
                f"{entry.source_id} 的 {purpose} 被放开了；"
                "交易所对行情数据另有商业授权安排，"
                "原始信息公开不等于可以转售")


def test_inferred_basis_is_not_recorded_as_user_authorised():
    """推论不得被记成使用者的直接授权。"""

    reg = default_rights()
    for source_id in ("baostock", "cninfo", "sse-site", "szse-site"):
        entry = reg.get(source_id)
        assert entry.basis is not Basis.USER_AUTHORIZED, (
            f"{source_id} 的模型外发是推论，不得记成使用者直接授权——"
            "复核时若问错了人，就会得到一个不存在的确认")
    for source_id in ("tencent-ifzq", "tencent-qt", "sina-hq",
                      "eastmoney-direct"):
        assert reg.get(source_id).basis is Basis.USER_AUTHORIZED


def test_no_real_source_is_prohibited_without_a_record():
    """PROHIBITED 是"查过说不行"，必须有记录；默认应为 UNKNOWN。"""

    for entry in default_rights().all():
        for purpose, value in entry.rights.items():
            assert value is not Rights.PROHIBITED or entry.note, (
                f"{entry.source_id}.{purpose} 被标为 PROHIBITED 但没写依据")