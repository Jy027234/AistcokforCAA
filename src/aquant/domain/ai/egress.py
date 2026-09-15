"""模型外发的唯一闸门（§17.2、ADR-005、数据权利登记表）。

这个模块存在的理由
------------------
"不要把未授权数据发给模型"如果只写在文档里，它会在第一次赶工时失效——
因为发数据给模型是最容易做的一步：拼一个字符串、调一次接口。

因此这里提供**唯一**的出口函数 :func:`build_model_context`，
以及**唯一**的准入判定 :func:`assert_egress_allowed`。
调用方必须先把材料登记为 :class:`EgressItem`（带 source_id），
再由闸门决定放行与否。

刻意不提供"绕过"的开关
----------------------
不做 force=True 之类的参数。一个"紧急情况可以跳过"的开关，
在真实项目里会变成常规路径。要放行就改权利登记表——
那是一个有记录、可复核的动作，而不是一次参数调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..data.rights import RightsRegistry, default_rights


class EgressDenied(RuntimeError):
    """材料不允许进入模型上下文。调用方必须显式处理，不得静默跳过。"""

    def __init__(self, message: str, *, blockers: list[dict],
                 repair_action: str) -> None:
        super().__init__(message)
        self.blockers = blockers
        self.repair_action = repair_action

    def as_error(self) -> dict:
        return {
            "code": "PIT_UNVERIFIED",
            "message": str(self),
            "object_id": "-",
            "retryable": False,
            "repair_action": self.repair_action,
            "blockers": self.blockers,
        }


@dataclass(frozen=True, slots=True)
class EgressItem:
    """一份准备发给模型的材料。**必须**声明来源。"""

    source_id: str
    text: str
    #: 人类可读的用途说明，用于审计（例如"抽取分红公告字段"）
    purpose: str = ""
    #: 该材料是否含个人信息。默认 None = 未声明，按"含"处理。
    contains_personal_data: bool | None = None


@dataclass
class EgressDecision:
    allowed: bool
    blockers: list[dict] = field(default_factory=list)
    allowed_sources: list[str] = field(default_factory=list)
    note: str = ""


def evaluate(items: list[EgressItem], *,
             registry: RightsRegistry | None = None) -> EgressDecision:
    """判定这批材料能否外发。**不抛异常**，供预览与审计使用。"""

    reg = registry or default_rights()
    blockers: list[dict] = []
    allowed_sources: list[str] = []

    for item in items:
        spec = reg.get(item.source_id)      # 未登记来源会抛 KeyError
        if not spec.can_enter_model_context():
            value = spec.rights.get("model_processing")
            blockers.append({
                "source_id": item.source_id,
                "right": "model_processing",
                "value": getattr(value, "value", str(value)),
                "detail": (f"来源 {item.source_id} 的模型处理权利未确认为 ALLOWED"
                           f"（当前 {getattr(value, 'value', value)}）"),
            })
            continue
        # §826：研究账户与个人信息默认不进入模型上下文。
        # 未声明等同于"含"，这是刻意的不对称默认。
        if item.contains_personal_data is not False:
            blockers.append({
                "source_id": item.source_id,
                "right": "personal_data",
                "value": "UNDECLARED_OR_PRESENT",
                "detail": ("材料未显式声明不含个人信息；"
                           "个人信息默认不进入模型上下文（§826）"),
            })
            continue
        allowed_sources.append(item.source_id)

    return EgressDecision(
        allowed=not blockers,
        blockers=blockers,
        allowed_sources=sorted(set(allowed_sources)),
        note=("全部材料均已确认允许模型处理" if not blockers
              else "存在未确认或不允许的材料，整批拒绝"),
    )


def assert_egress_allowed(items: list[EgressItem], *,
                          registry: RightsRegistry | None = None) -> EgressDecision:
    """与 evaluate 相同，但不允许时抛 EgressDenied。

    **整批拒绝**而不是过滤掉不允许的部分：静默过滤会让调用方以为
    "材料都用上了"，从而给出一个基于残缺输入却看起来完整的结论。
    """

    decision = evaluate(items, registry=registry)
    if not decision.allowed:
        raise EgressDenied(
            f"{len(decision.blockers)} 项材料不允许进入模型上下文",
            blockers=decision.blockers,
            repair_action=("在 docs/data-rights-register.md 中确认来源条款，"
                           "并把 src/aquant/domain/data/rights.py 里对应来源的 "
                           "model_processing 改为 ALLOWED"),
        )
    return decision


def build_model_context(items: list[EgressItem], *,
                        registry: RightsRegistry | None = None) -> str:
    """唯一的外发入口：先过闸门，再拼接。"""

    assert_egress_allowed(items, registry=registry)
    parts = []
    for item in items:
        parts.append(f"[source: {item.source_id}]\n{item.text}")
    return "\n\n".join(parts)