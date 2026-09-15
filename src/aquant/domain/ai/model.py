"""文本模型的领域侧窄接口（ADR-012）。

为什么是窄接口而不是直接用某家 SDK
----------------------------------
领域层需要"读一段文本、给一个结构化结论"的能力（证据抽取、摘要）。
把某家 SDK 焊进领域层会带来三件事：换模型要改领域代码；离线测试要联网；
模型密钥会渗进量化模块。

因此领域层只依赖这个 Protocol；生产实现（DeepSeek 官方 API）在适配层；
离线测试用确定性替身，pytest 不需要密钥、不联网。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """一次模型调用的输入。

    `instructions` 是系统侧的固定指令（含 prompt 版本语义），
    `context` 是**已经过外发闸门**的材料正文。
    两者分开是为了让审计能回答"发出去的是材料，还是我们的指令"。
    """

    instructions: str
    context: str
    #: prompt 版本标识。落进 model_call.prompt_version，用于事后复现。
    prompt_version: str = "v1"
    max_output_tokens: int = 2048
    temperature: float = 0.0


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: str
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    #: 原始响应内容哈希。用于证据不可篡改的核对（§17.4）。
    content_hash: str | None = None
    #: 供应商返回的用量/耗时等原始信息，按需留存。
    raw_meta: dict = field(default_factory=dict)


class ModelUnavailable(RuntimeError):
    """模型不可用。调用方必须显式处理，不得退化成"假装没有这一步"。"""

    def __init__(self, message: str, *, code: str = "DATA_NOT_READY",
                 repair_action: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.repair_action = repair_action


@runtime_checkable
class TextModelProvider(Protocol):
    """文本进、文本出。**不做语义判断**——语义留在领域层。

    这是刻意画窄的边界：一旦接口开始"顺便判断哪个证据更重要"，
    领域逻辑就挪到适配层去了，而适配层是会被替换的部分。
    """

    provider_name: str
    model_name: str

    def complete(self, request: ModelRequest) -> ModelResponse:
        """执行一次调用。失败必须抛 ModelUnavailable，不得返回空串。"""
        ...
