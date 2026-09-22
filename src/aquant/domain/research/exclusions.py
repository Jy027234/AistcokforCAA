"""因子排除原因的**唯一**取值表（§10.2）。

为什么需要一份共用表
-------------------
算不出因子时必须给出**原因**，不能给 0 或省略。F10 的风险就在这里：
"被质量门排除"与"值为空"在界面上长得一样，而两者要采取的行动完全不同。

原因码有两个消费者，它们必须看到同一种说法：

  * **界面**（`aquant.application.workspace_view`）——研究卡上的因子表；
  * **模型**（`capabilities/aquant_lab_agentctl_handlers.py`）——能力返回的卡片。

原先两处各抄了一份表，且两份都只覆盖 `MISSING_*` 这类英文码；
F10 实际产出的中文原因（"TTM 不可得……"）在两处都**没有**说明，
于是界面上只会显示一个破折号。共用一份表，并由
`tests/api/test_research_card_factors.py` 对着它断言，才不会再次分叉。

**认不出的码原样返回**：编一句解释比不解释更糟——那会把"没登记的原因"
讲成一个具体结论。
"""

from __future__ import annotations

#: 稳定原因码 -> 人类可读说明。**加了新码就必须同时加说明**。
EXCLUSION_LABELS: dict[str, str] = {
    # 财务口径（记录层）
    "MISSING_NET_PROFIT": "缺少净利润，无法计算盈利收益率",
    "MISSING_TOTAL_SHARE": "缺少总股本，无法计算每股口径",
    "MISSING_STATEMENT": "该期财报缺失或被质量门排除",
    "NO_FINANCIAL_STATEMENT_BEFORE_AS_OF": "决策时点之前没有已公布的财报（PIT 闸门）",
    "NON_POSITIVE_MARKET_CAP": "市值非正，比率无意义",
    # F10 在快照上实际产出的原因（见 domain/research/f10.py）
    "快照内无行情": "该标的在本快照内没有行情",
    "快照未包含财务数据": "本快照未包含财务数据（季度数据需随快照冻结）",
    "TTM 不可得（缺上年同期或口径不成立）": "TTM 不可得：缺上年同期或累计口径不成立",
    "缺总股本或价格无效": "缺总股本或价格无效",
    "缺决策日总市值": "缺少快照冻结的决策日总市值",
    "缺决策日总市值日期": "缺少决策日总市值对应的行情日期",
    "决策日总市值无效": "决策日总市值必须为有限正数",
    "决策日总市值日期无效": "决策日总市值日期格式无效",
    "决策日总市值日期与最后行情日不一致": (
        "决策日总市值日期必须与快照最后行情交易日一致"
    ),
}


def exclusion_label(reason: str | None) -> str | None:
    """原因码 -> 说明。认不出的码**原样返回**，不编解释。"""

    if not reason:
        return None
    return EXCLUSION_LABELS.get(reason, reason)
