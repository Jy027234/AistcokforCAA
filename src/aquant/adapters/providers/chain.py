"""抓取降级链：主源失败时显式切换到备用源。

主文档 §6.3 要求：一个字段一个主源；备用源不得在主源失败时静默混入；
切换供应商需要新的数据版本、单位校验、差异报告并重新计算。

因此本模块的降级是**显式且留痕**的：

  * 每次尝试都写入 fetch_receipt（成功与失败都写）；
  * 降级事实记录在 ChainResult 的 degraded / used_source_id 上；
  * assert_no_silent_mix 供上层在拼接数据前调用，强行要求
    "同一序列不来自两个源，除非显式声明"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ...domain.data.source_registry import Domain, SourceHealth, SourceRegistry


class Fetchable(Protocol):
    """提供方协议。实现方负责守卫、限速、熔断与归档。"""

    source_id: str

    def fetch(self, url: str, *, label: str = "") -> Any: ...


@dataclass(slots=True)
class ChainAttempt:
    source_id: str
    ok: bool
    receipt_id: str | None = None
    detail: str | None = None


@dataclass(slots=True)
class ChainResult:
    ok: bool
    used_source_id: str | None
    value: Any = None
    attempts: list[ChainAttempt] = field(default_factory=list)
    degraded: bool = False

    def summary(self) -> dict:
        return {
            "ok": self.ok,
            "used_source_id": self.used_source_id,
            "degraded": self.degraded,
            "attempts": [
                {"source_id": a.source_id, "ok": a.ok,
                 "receipt_id": a.receipt_id, "detail": a.detail}
                for a in self.attempts
            ],
        }


class SilentMixError(Exception):
    """同一序列混用多个来源而未声明。"""


class FetchChain:
    """按优先级依次尝试候选源。"""

    def __init__(self, registry: SourceRegistry) -> None:
        self.registry = registry
        #: 失败计数必须**跨调用累计**，否则每次 run 都从 0 开始，
        #: 源永远不会被标记为不可用，降级链就会无限重试一个已知失效的源。
        self._consecutive_failures: dict[str, int] = {}

    def run(
        self,
        domain: Domain,
        clients: dict[str, Fetchable],
        call: Callable[[Fetchable], tuple[bool, Any, str | None, str | None]],
        *,
        mark_blocked_after: int = 2,
    ) -> ChainResult:
        """依次尝试候选源。

        call 接收提供方，返回 (ok, value, receipt_id, detail)。
        连续失败的源会被标记为 BLOCKED，后续调用直接跳过——
        这正是东财实测教训的编码：不要反复试探已知不可用的源。
        """

        result = ChainResult(ok=False, used_source_id=None)
        candidates = self.registry.candidates(domain)
        if not candidates:
            result.attempts.append(ChainAttempt(
                source_id="<none>", ok=False,
                detail=f"no test-passed source registered for {domain.value}",
            ))
            return result

        consecutive = self._consecutive_failures
        for spec in candidates:
            client = clients.get(spec.source_id)
            if client is None:
                result.attempts.append(ChainAttempt(
                    source_id=spec.source_id, ok=False, detail="no client bound",
                ))
                continue
            ok, value, receipt_id, detail = call(client)
            result.attempts.append(ChainAttempt(
                source_id=spec.source_id, ok=ok, receipt_id=receipt_id, detail=detail,
            ))
            if ok:
                result.ok = True
                result.used_source_id = spec.source_id
                result.value = value
                # 不是首选源即视为降级，必须让上层知道
                result.degraded = spec.source_id != candidates[0].source_id
                consecutive.pop(spec.source_id, None)
                self.registry.mark_health(spec.source_id, SourceHealth.HEALTHY)
                return result
            consecutive[spec.source_id] = consecutive.get(spec.source_id, 0) + 1
            if consecutive[spec.source_id] >= mark_blocked_after:
                self.registry.mark_health(spec.source_id, SourceHealth.BLOCKED)

        return result

    @staticmethod
    def assert_no_silent_mix(source_ids: list[str], *, declared: str | None = None) -> None:
        """拼接同一序列前调用。§6.3 禁止静默混接。"""

        distinct = {s for s in source_ids if s}
        if len(distinct) > 1 and declared is None:
            raise SilentMixError(
                "a single series would mix sources "
                f"{sorted(distinct)} without a declared data version; "
                "switching sources requires a new data version and a difference report"
            )
