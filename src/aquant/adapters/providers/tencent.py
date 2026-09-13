"""腾讯证券行情提供方（东方财富的降级备用源）。

实测（2026-09-13）：该源在本机可用，提供日线的不复权 / qfq / hfq 三种序列，
字段与东方财富可比对。**它不是替代品，而是为了避免单点**——
东财已实测会对出口 IP 实施长时段封锁，届时必须有第二条路。

与 eastmoney.py 相同的铁律：守卫 → 限速 → 熔断 → 抓取 → 归档原始字节 → 解析。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from aquant.adapters.providers.eastmoney import FetchOutcome, _ValidatingRedirectHandler
from aquant.adapters.providers.fetch_guard import (
    FetchDenied,
    FetchPolicy,
    assert_url_allowed,
    check_content_length,
    read_bounded,
)
from aquant.adapters.providers.resilience import (
    CircuitBreaker,
    CircuitOpen,
    RateLimiter,
    RetryPolicy,
)
from aquant.domain.data.forward_archive import ForwardArchive

SOURCE_ID = "tencent-ifzq"
QUOTE_HOST = "web.ifzq.gtimg.cn"
SNACK_HOST = "qt.gtimg.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

#: 复权参数（腾讯命名）：0 不复权 / 1 前复权 / 2 后复权
ADJUST_PARAM = {0: "", 1: "qfq", 2: "hfq"}
PROXY_ENV = "AQUANT_TRUSTED_PROXY_NETWORKS"


def default_policy() -> FetchPolicy:
    import os

    raw = os.environ.get(PROXY_ENV, "")
    return FetchPolicy(
        resolve_dns=True,
        allowed_hosts=frozenset({QUOTE_HOST, SNACK_HOST}),
        max_redirects=3,
        max_response_bytes=8 * 1024 * 1024,
        trusted_proxy_networks=frozenset(
            part.strip() for part in raw.split(",") if part.strip()
        ),
    )


class TencentClient:
    source_id = SOURCE_ID

    def __init__(
        self,
        archive: ForwardArchive,
        *,
        policy: FetchPolicy | None = None,
        limiter: RateLimiter | None = None,
        breaker: CircuitBreaker | None = None,
        retry: RetryPolicy | None = None,
        sleep=None,
        timeout: float = 20.0,
    ) -> None:
        self.archive = archive
        self.policy = policy or default_policy()
        self.limiter = limiter or RateLimiter(min_interval_seconds=1.5)
        self.breaker = breaker or CircuitBreaker(failure_threshold=3, cooldown_seconds=60.0)
        self.retry = retry or RetryPolicy(max_attempts=3, base_delay_seconds=2.0,
                                          max_delay_seconds=15.0)
        import time as _time
        self._sleep = sleep or _time.sleep
        self.timeout = timeout

    def fetch(self, url: str, *, label: str = "") -> FetchOutcome:
        requested_at = datetime.now(timezone.utc)
        host = urlsplit(url).hostname or ""

        try:
            self.breaker.assert_closed()
        except CircuitOpen as exc:
            r = self.archive.record(
                source_id=SOURCE_ID, url=url, outcome="DENIED", requested_at=requested_at,
                detail=f"circuit open, retry after {exc.retry_after_seconds:.0f}s",
            )
            return FetchOutcome(False, None, r.receipt_id, None, r.detail)

        try:
            assert_url_allowed(url, self.policy)
        except FetchDenied as exc:
            r = self.archive.record(
                source_id=SOURCE_ID, url=url, outcome="DENIED", requested_at=requested_at,
                detail=f"{exc.reason}: {exc.detail}",
            )
            return FetchOutcome(False, None, r.receipt_id, None, r.detail)

        last_detail = "unknown"
        for attempt in range(1, self.retry.max_attempts + 1):
            self.limiter.wait(host, sleep=self._sleep)
            opener = urllib.request.build_opener(_ValidatingRedirectHandler(self.policy))
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Referer": "https://gu.qq.com/", "Accept": "*/*",
            })
            try:
                with opener.open(req, timeout=self.timeout) as resp:
                    check_content_length(resp.headers.get("Content-Length"), self.policy)
                    body = read_bounded(resp, self.policy)
                    status = resp.status
                self.breaker.record_success()
                digest, _ = self.archive.store_bytes(body, media_type="application/json")
                r = self.archive.record(
                    source_id=SOURCE_ID, url=url, outcome="OK",
                    requested_at=requested_at, http_status=status,
                    content_hash=digest, byte_size=len(body), detail=label or None,
                )
                return FetchOutcome(True, body, r.receipt_id, digest, None, status)
            except FetchDenied as exc:
                self.breaker.record_failure()
                r = self.archive.record(
                    source_id=SOURCE_ID, url=url,
                    outcome="TOO_LARGE" if exc.reason == "response-too-large" else "DENIED",
                    requested_at=requested_at, detail=f"{exc.reason}: {exc.detail}",
                )
                return FetchOutcome(False, None, r.receipt_id, None, r.detail)
            except urllib.error.HTTPError as exc:
                last_detail = f"HTTP {exc.code}"
                self.breaker.record_failure()
                r = self.archive.record(
                    source_id=SOURCE_ID, url=url, outcome="HTTP_ERROR",
                    requested_at=requested_at, http_status=exc.code, detail=last_detail,
                )
                return FetchOutcome(False, None, r.receipt_id, None, last_detail, exc.code)
            except (urllib.error.URLError, socket.timeout, TimeoutError,
                    ConnectionError, OSError) as exc:
                last_detail = f"{type(exc).__name__}: {exc}"
                self.breaker.record_failure()
                if attempt < self.retry.max_attempts:
                    self._sleep(self.retry.delay_for(attempt))
                    continue
                r = self.archive.record(
                    source_id=SOURCE_ID, url=url, outcome="TRANSPORT_ERROR",
                    requested_at=requested_at,
                    detail=f"{last_detail} (after {attempt} attempts)",
                )
                return FetchOutcome(False, None, r.receipt_id, None, r.detail)
        return FetchOutcome(False, None, "", None, last_detail)

    # ------------------------------------------------------------ endpoints
    def daily_quotes(self, symbol: str, begin: str, end: str, *,
                     adjust: int = 0) -> tuple[FetchOutcome, list[list[str]]]:
        """日线。symbol 形如 sh600519；adjust: 0 不复权 / 1 qfq / 2 hfq。

        返回 (outcome, bars)，bars 每项为 [日期, 开, 收, 高, 低, 成交量(手), ...]。
        """

        param = ADJUST_PARAM.get(adjust, "")
        path = f"{param}day" if param else "day"
        url = (
            f"https://{QUOTE_HOST}/appstock/app/fqkline/get?"
            f"param={symbol},{path},{begin},{end},640,{param}"
        )
        out = self.fetch(url, label=f"kline:{symbol}:{param or 'none'}")
        if not out.ok or out.payload is None:
            return out, []
        try:
            doc = json.loads(out.payload)
            node = (doc.get("data") or {}).get(symbol) or {}
            bars = node.get(path) or node.get("day") or []
        except json.JSONDecodeError as exc:
            return FetchOutcome(False, None, out.receipt_id, out.content_hash,
                                f"malformed JSON: {exc}"), []
        return out, bars
