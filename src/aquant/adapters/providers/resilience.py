"""抓取韧性：礼貌间隔、指数退避、熔断。

为什么需要（T4 实测）：东方财富接口在约十余次请求后开始统一拒绝连接，
且**换客户端、换子域、等待十余分钟均未恢复**。这不是可以靠重试解决的瞬时抖动，
而是无 SLA 服务的真实行为。因此：

  * 默认**串行 + 间隔**，不并发轰炸；
  * 失败按指数退避重试，但**有上限**；
  * 连续失败达阈值即**熔断**一段时间，避免把限流升级为封禁；
  * 熔断期间直接拒绝调用，而不是继续试探。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


class CircuitOpen(Exception):
    """熔断中。调用方应降级，而不是继续重试。"""

    def __init__(self, retry_after_seconds: float) -> None:
        super().__init__(f"circuit open; retry after {retry_after_seconds:.1f}s")
        self.retry_after_seconds = retry_after_seconds


@dataclass(slots=True)
class CircuitBreaker:
    """连续失败达阈值即断开，冷却后放行一次试探。"""

    failure_threshold: int = 3
    cooldown_seconds: float = 60.0
    consecutive_failures: int = 0
    opened_at: datetime | None = None

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if self._now() - self.opened_at >= timedelta(seconds=self.cooldown_seconds):
            # 冷却结束：半开，放行下一次调用
            self.opened_at = None
            self.consecutive_failures = 0
            return False
        return True

    def retry_after(self) -> float:
        if self.opened_at is None:
            return 0.0
        elapsed = (self._now() - self.opened_at).total_seconds()
        return max(0.0, self.cooldown_seconds - elapsed)

    def assert_closed(self) -> None:
        if self.is_open:
            raise CircuitOpen(self.retry_after())

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold:
            self.opened_at = self._now()

    def state(self) -> dict:
        return {
            "open": self.is_open,
            "consecutive_failures": self.consecutive_failures,
            "retry_after_seconds": self.retry_after(),
        }


@dataclass(slots=True)
class RateLimiter:
    """每主机最小间隔。默认 1.5s，比 T4 触发限流的节奏慢得多。"""

    min_interval_seconds: float = 1.5
    _last_call: dict[str, float] = field(default_factory=dict)

    def wait(self, host: str, *, sleep=time.sleep) -> float:
        now = time.monotonic()
        last = self._last_call.get(host)
        waited = 0.0
        if last is not None:
            elapsed = now - last
            if elapsed < self.min_interval_seconds:
                waited = self.min_interval_seconds - elapsed
                sleep(waited)
        self._last_call[host] = time.monotonic()
        return waited


@dataclass(slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 30.0

    def delay_for(self, attempt: int) -> float:
        """attempt 从 1 开始。指数退避并封顶。"""

        return min(self.max_delay_seconds, self.base_delay_seconds * (2 ** (attempt - 1)))
