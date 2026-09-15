"""DeepSeek 官方 API 适配（ADR-012 的生产实现之一）。

安全约定
--------
* 密钥从**文件**读，路径由 AQUANT_DEEPSEEK_KEY_FILE 指定；
* 密钥**绝不**出现在日志、异常消息、返回值或收据里。
  异常消息只带状态码与响应体摘要，且先做密钥替换兜底；
* 密钥不进版本库、不进快照、不进模型上下文。

为什么不用 SDK
--------------
官方接口与 OpenAI 兼容，一个 POST 就够了。引入 SDK 只是多一层依赖，
而这一层恰好是最需要看清"到底发出去了什么"的地方。
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from aquant.domain.ai.model import ModelRequest, ModelResponse, ModelUnavailable

DEFAULT_KEY_FILE = Path(r"E:\IT\文档\deepseek.txt")
DEFAULT_BASE_URL = "https://api.deepseek.com"
#: 默认模型。**必须**是账户实际可用的型号：本次核对 /models 返回
#: ['deepseek-flash', 'deepseek-v4-pro']，而 deepseek-chat 并不在其中。
#: 请求一个不存在的型号时服务端会**静默回落**到某个可用模型并照常返回 200，
#: 于是"我们用的是哪个模型"这个问题在记录里是错的——事后复现会得到
#: 另一份结果。因此这里取实际可用值，并由 check_model_egress 断言其可用性。
DEFAULT_MODEL = "deepseek-flash"


def key_file_path() -> Path:
    return Path(os.environ.get("AQUANT_DEEPSEEK_KEY_FILE", str(DEFAULT_KEY_FILE)))


def load_api_key() -> str:
    """读密钥。缺失或为空时**明确报错**，不返回空串。

    空串会让请求带着 Authorization: Bearer 发出去，
    服务端返回 401，而错误信息里看不出是"没配密钥"还是"密钥错了"。
    """

    path = key_file_path()
    if not path.exists():
        raise ModelUnavailable(
            f"模型密钥文件不存在：{path}",
            repair_action="把密钥写入该文件，或设置 AQUANT_DEEPSEEK_KEY_FILE 指向它")
    raw = path.read_text(encoding="utf-8-sig")

    # 密钥文件里常常还有一行说明（"开发常用：sk-xxx"之类）。
    # 直接把整个文件当密钥会让非 ASCII 字符进入 HTTP 头，
    # 报错出现在 urllib 的 latin-1 编码里——离原因很远。
    # 因此**只取密钥本身**：第一个以 sk- 开头的空白分隔片段。
    tokens = raw.split()
    key = next((t for t in tokens if t.startswith("sk-")), None)
    if key is None:
        if not tokens:
            raise ModelUnavailable(f"模型密钥文件为空：{path}",
                                   repair_action="写入密钥后重试")
        raise ModelUnavailable(
            f"模型密钥文件里找不到以 sk- 开头的密钥：{path}",
            repair_action="确认文件里含一行 sk- 开头的密钥（不要只写说明文字）")

    # 形状校验：密钥必须是纯 ASCII 且无空白。宁可在这里拒绝，
    # 也不要把一个含中文的"密钥"发到网络上——那既必然失败，
    # 也把文件里的说明文字送到了第三方。
    if not key.isascii() or any(c.isspace() for c in key):
        raise ModelUnavailable(
            f"模型密钥形状不合法（非 ASCII 或含空白）：{path}",
            repair_action="确认密钥是 sk- 开头的一段 ASCII 字符，且单独成行")
    return key


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


class DeepSeekProvider:
    """DeepSeek 的 chat/completions。只做一次 HTTP 调用，不重试业务错误。

    为什么不在这里重试：外发材料与调用次数是有预算约束的（§16.4
    LLM_BUDGET_EXCEEDED），无声重试会让预算与账单对不上。重试属于调用方决策。
    """

    provider_name = "deepseek"

    def __init__(self, *, model: str | None = None, base_url: str | None = None,
                 api_key: str | None = None, timeout: float = 120.0) -> None:
        self.model_name = model or os.environ.get("AQUANT_DEEPSEEK_MODEL", DEFAULT_MODEL)
        self.base_url = (base_url
                         or os.environ.get("AQUANT_DEEPSEEK_BASE_URL", DEFAULT_BASE_URL))
        # 允许测试注入密钥；生产路径一律从文件读
        self._api_key = api_key
        self.timeout = timeout

    def _key(self) -> str:
        return self._api_key or load_api_key()

    def complete(self, request: ModelRequest) -> ModelResponse:
        key = self._key()
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": request.instructions},
                {"role": "user", "content": request.context},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions", data=body,
            method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + key})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise ModelUnavailable(
                f"模型返回 HTTP {exc.code}：{_redact(detail, key)}",
                code="DATA_NOT_READY",
                repair_action=("检查密钥与额度；429/5xx 可稍后重试，"
                               "401/403 需更换密钥")) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelUnavailable(
                f"模型请求失败：{_redact(str(exc), key)}",
                repair_action="检查网络与代理设置后重试") from exc

        try:
            doc = json.loads(raw)
            text = doc["choices"][0]["message"]["content"]
            usage = doc.get("usage") or {}
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ModelUnavailable(
                f"模型响应无法解析：{_redact(raw[:300], key)}",
                repair_action="核对接口版本与响应格式") from exc

        return ModelResponse(
            text=text,
            provider=self.provider_name,
            model=doc.get("model") or self.model_name,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            content_hash="sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
            raw_meta={"id": doc.get("id"), "finish_reason":
                      (doc.get("choices") or [{}])[0].get("finish_reason")},
        )
