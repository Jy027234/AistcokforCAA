"""东方财富行情提供方。

数据流（顺序不可颠倒）：

    守卫校验 → 限速 → 熔断检查 → HTTP 抓取（逐跳复校）→ 归档原始字节 → 解析

"归档先于解析"是刻意的：解析失败不影响证据，解析器升级后还能重放历史字节。

T4 已实测该源的两个限制，本模块不假装它能做更多：
  * 不提供历史时点版本 -> 只产出 LIVE_OBSERVED 观察，不做 PIT 重建；
  * 会限流 -> 串行、间隔、退避、熔断，且把失败也归档为证据。

**本模块不向模型暴露任何网络能力**（§17.3：模型工具不继承抓取器的任意网络权限）。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

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

SOURCE_ID = "eastmoney-direct"
QUOTE_HOST = "push2his.eastmoney.com"
LIST_HOST = "push2.eastmoney.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")


#: 本机实测处于穿透式代理之后（网关 198.18.0.2，DNS fdfe:dcba:9876::2），
#: 所有域名都解析到伪 IP 网段。运维方需通过环境变量显式声明该网段：
#:     AQUANT_TRUSTED_PROXY_NETWORKS=198.18.0.0/15,fdfe:dcba:9876::/48
#: 未声明时保持默认严格模式（伪 IP 会被当作私有地址拒绝）。
TRUSTED_PROXY_ENV = "AQUANT_TRUSTED_PROXY_NETWORKS"


def trusted_proxy_networks_from_env() -> frozenset[str]:
    import os

    raw = os.environ.get(TRUSTED_PROXY_ENV, "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def default_policy() -> FetchPolicy:
    return FetchPolicy(
        resolve_dns=True,
        allowed_hosts=frozenset({QUOTE_HOST, LIST_HOST}),
        max_redirects=3,
        max_response_bytes=8 * 1024 * 1024,
        trusted_proxy_networks=trusted_proxy_networks_from_env(),
    )


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """每一跳重定向都重新过守卫。

    只校验初始 URL 不够：白名单域名可以 302 到内网地址。
    """

    def __init__(self, policy: FetchPolicy) -> None:
        self.policy = policy
        self.hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        self.hops += 1
        if self.hops > self.policy.max_redirects:
            raise FetchDenied("too-many-redirects",
                              f"exceeded {self.policy.max_redirects} redirects")
        assert_url_allowed(newurl, self.policy)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(slots=True)
class FetchOutcome:
    ok: bool
    payload: bytes | None
    receipt_id: str
    content_hash: str | None
    detail: str | None = None
    http_status: int | None = None


class EastmoneyClient:
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

    # ------------------------------------------------------------ fetch
    def fetch(self, url: str, *, label: str = "") -> FetchOutcome:
        """抓一次并归档。无论成败都产生一条 fetch_receipt。"""

        requested_at = datetime.now(timezone.utc)
        host = urlsplit(url).hostname or ""

        # 1. 熔断：拒绝而不是继续试探
        try:
            self.breaker.assert_closed()
        except CircuitOpen as exc:
            r = self.archive.record(
                source_id=SOURCE_ID, url=url, outcome="DENIED", requested_at=requested_at,
                detail=f"circuit open, retry after {exc.retry_after_seconds:.0f}s",
            )
            return FetchOutcome(False, None, r.receipt_id, None, r.detail)

        # 2. 守卫
        try:
            assert_url_allowed(url, self.policy)
        except FetchDenied as exc:
            r = self.archive.record(
                source_id=SOURCE_ID, url=url, outcome="DENIED", requested_at=requested_at,
                detail=f"{exc.reason}: {exc.detail}",
            )
            return FetchOutcome(False, None, r.receipt_id, None, r.detail)

        # 3. 限速 + 带退避的重试
        last_detail = "unknown"
        for attempt in range(1, self.retry.max_attempts + 1):
            self.limiter.wait(host, sleep=self._sleep)
            opener = urllib.request.build_opener(_ValidatingRedirectHandler(self.policy))
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": "https://quote.eastmoney.com/",
                "Accept": "*/*",
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
    def universe_page(
        self, *, page: int = 1, page_size: int = 100,
    ) -> tuple[FetchOutcome, list[dict], int | None]:
        """全 A 股列表。

        ``f20`` 是接口返回的总市值（元），与证券身份字段一起取回，供
        ``collect_market_caps.py`` 生成前向观察 sidecar。这个值没有历史
        版本，调用方必须把抓取时刻和原始响应收据一并保存，不能把它当作
        历史 PIT 数据。接口实测会把超大 ``pz`` 截断到 100 条，所以全量
        采集必须显式翻页；本方法同时返回服务端声明的总条数。
        """

        if page < 1:
            raise ValueError("page must be >= 1")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")

        url = (
            "https://" + LIST_HOST + "/api/qt/clist/get?"
            f"pn={page}&pz={page_size}&po=1&np=1&fltt=2&invt=2&fid=f12"
            "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
            "&fields=f12,f13,f14,f100,f26,f20"
        )
        out = self.fetch(url, label=f"universe:page:{page}")
        if not out.ok or out.payload is None:
            return out, [], None
        try:
            data = json.loads(out.payload).get("data") or {}
            rows = data.get("diff") or []
            total_raw = data.get("total")
            total = int(total_raw) if total_raw is not None else None
        except json.JSONDecodeError as exc:
            return FetchOutcome(False, None, out.receipt_id, out.content_hash,
                                f"malformed JSON: {exc}"), [], None
        except (TypeError, ValueError) as exc:
            return FetchOutcome(False, None, out.receipt_id, out.content_hash,
                                f"invalid universe total: {exc}"), [], None
        return out, rows, total

    def universe(self, *, page_size: int = 100) -> tuple[FetchOutcome, list[dict]]:
        """向后兼容的单页探针；生产全量采集使用 ``universe_page``。"""

        out, rows, _total = self.universe_page(page=1, page_size=page_size)
        return out, rows

    def daily_quotes(self, secid: str, begin: str, end: str, *,
                     adjust: int = 0) -> tuple[FetchOutcome, list[str]]:
        """日线序列。adjust: 0 不复权 / 1 前复权 / 2 后复权。

        注意（T4 结论）：前复权序列会随后续分红重算，
        因此只能作为当次观察归档，不得当作历史不变量。
        """

        url = (
            "https://" + QUOTE_HOST + "/api/qt/stock/kline/get?"
            f"secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
            "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
            f"&klt=101&fqt={adjust}&beg={begin}&end={end}"
        )
        out = self.fetch(url, label=f"kline:{secid}:fqt{adjust}")
        if not out.ok or out.payload is None:
            return out, []
        try:
            klines = ((json.loads(out.payload).get("data") or {}).get("klines")) or []
        except json.JSONDecodeError as exc:
            return FetchOutcome(False, None, out.receipt_id, out.content_hash,
                                f"malformed JSON: {exc}"), []
        return out, klines
