"""巨潮资讯网（cninfo）公告渠道。

为什么优先它（T4 结论）：
    免费行情源不提供历史时点，而公告页面**自带发布时间**。
    实测：巨潮 hisAnnouncement 接口可达，返回 announcementTime（epoch 毫秒），
    这正是本项目目前唯一能取得的 PARTIAL 级时点证据。
    巨潮是证监会指定的信息披露平台，覆盖沪深两市，因此作为公告主渠道。

三个必须做对的地方：

1. **发布时间到可用时点的转换**（§7.3）：
   接口给的是"日期"级时间戳（北京时间当日 00:00）。公告在当日盘中发布，
   因此**不得**假定当日开盘前可用——必须顺延到次一交易日盘前。
   这是 D04 在真实渠道上的落点。

2. **抓取时间与可用时间分开**（§7.2）：
   first_seen_at 是本次抓取时刻，绝不回填；available_at 按上述规则重建。

3. **原文与索引分开**：
   列表接口只给标题与附件地址。正文必须另行抓取并归档，
   否则引用无法定位（§15.3）。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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
from aquant.domain.data.pit import (
    AvailabilityBasis,
    PitMode,
    TimestampPrecision,
    date_only_available_at,
)

SOURCE_ID = "cninfo"
QUERY_HOST = "www.cninfo.com.cn"
STATIC_HOST = "static.cninfo.com.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
PROXY_ENV = "AQUANT_TRUSTED_PROXY_NETWORKS"

#: 北京时间为 UTC+8
CST = timezone(timedelta(hours=8))


def default_policy() -> FetchPolicy:
    import os

    raw = os.environ.get(PROXY_ENV, "")
    return FetchPolicy(
        resolve_dns=True,
        allowed_hosts=frozenset({QUERY_HOST, STATIC_HOST}),
        max_redirects=3,
        max_response_bytes=16 * 1024 * 1024,
        trusted_proxy_networks=frozenset(
            part.strip() for part in raw.split(",") if part.strip()
        ),
    )


@dataclass(frozen=True, slots=True)
class Announcement:
    announcement_id: str
    sec_code: str
    sec_name: str
    title: str
    #: 接口给出的时间戳（北京时间当日的起点）
    announced_on: date
    adjunct_url: str | None
    announcement_type: str | None
    raw: dict

    def detail_url(self) -> str | None:
        if not self.adjunct_url:
            return None
        path = self.adjunct_url if self.adjunct_url.startswith("/") else "/" + self.adjunct_url
        return f"http://{STATIC_HOST}{path}"


def parse_announcements(payload: bytes | str | dict) -> list[Announcement]:
    """把 hisAnnouncement 响应解析为公告列表。

    只解析，不改写：无法解析的条目跳过而不是猜。时间戳缺失的条目也跳过，
    因为没有发布时间的公告无法作为时点证据。
    """

    if isinstance(payload, (bytes, str)):
        doc = json.loads(payload)
    else:
        doc = payload
    out: list[Announcement] = []
    for item in doc.get("announcements") or []:
        ts = item.get("announcementTime")
        if ts in (None, ""):
            continue
        # epoch 毫秒 -> 北京时间日期
        announced = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).astimezone(CST).date()
        out.append(Announcement(
            announcement_id=str(item.get("announcementId") or ""),
            sec_code=str(item.get("secCode") or ""),
            sec_name=str(item.get("secName") or ""),
            title=str(item.get("announcementTitle") or "").strip(),
            announced_on=announced,
            adjunct_url=item.get("adjunctUrl"),
            announcement_type=item.get("announcementTypeName"),
            raw=dict(item),
        ))
    return out


def to_pit_record(
    ann: Announcement,
    *,
    trading_calendar: list[date],
    first_seen_at: datetime,
) -> dict:
    """把一条公告转成带时点语义的证据记录。

    关键：接口只给到**日期**精度，因此可用时点必须走 §7.3 的保守顺延
    ——次一交易日盘前，绝不假定当日开盘前可用（D04）。

    模式判定（§7.2）：如果在可用时点**之前**就抓到了这条公告，那是真实前向观察
    （LIVE_OBSERVED）；如果是在可用时点**之后**才补抓的，那是对过去的重建
    （HISTORICAL_RECONSTRUCTED）。两者不得混入同一比较结论，因此按事实判定，
    而不是一律标成前向观察。

    可用性依据（§7.2 availability_basis）按事实区分：
      * 在可用时点之前抓到的 -> OBSERVED（我们**亲眼看到**它在那一刻就在那里），
        模式为 LIVE_OBSERVED；
      * 在可用时点之后才补抓的 -> RECONSTRUCTED（从发布日按保守规则推出来的），
        模式为 HISTORICAL_RECONSTRUCTED。
    后者不得用于正式 PIT 回测（§7.2）。
    """

    if first_seen_at.tzinfo is None:
        raise ValueError("first_seen_at must be timezone-aware (§7.1)")

    available_at, rationale = date_only_available_at(
        ann.announced_on, trading_calendar, preopen_already_captured=True
    )

    captured_at = first_seen_at.astimezone(timezone.utc)
    if captured_at <= available_at.astimezone(timezone.utc):
        # 我们在窗口打开之前就看到了它：这是直接观察，不是推断
        pit_mode = PitMode.LIVE_OBSERVED
        available_basis = AvailabilityBasis.OBSERVED
        mode_note = ("captured before the usable time point; a genuine forward observation, "
                     "so the basis is OBSERVED rather than reconstructed")
    else:
        # 事后补抓：可用时点只能从发布日按保守规则推出来
        pit_mode = PitMode.HISTORICAL_RECONSTRUCTED
        available_basis = AvailabilityBasis.RECONSTRUCTED
        mode_note = ("captured after the usable time point; availability was reconstructed "
                     "from the publication date, so this is not a forward observation and "
                     "must not be used for formal point-in-time backtests")

    return {
        "source_id": SOURCE_ID,
        "announcement_id": ann.announcement_id,
        "instrument_code": ann.sec_code,
        "title": ann.title,
        "announcement_type": ann.announcement_type,
        "document_url": ann.detail_url(),
        "event_time": ann.announced_on.isoformat(),
        "timestamp_precision": TimestampPrecision.DATE.value,
        "source_published_date": ann.announced_on.isoformat(),
        "source_published_at": None,            # 日期级，无具体时刻
        "first_seen_at": first_seen_at.astimezone(timezone.utc).isoformat(),
        "available_at": available_at.astimezone(timezone.utc).isoformat(),
        "available_basis": available_basis.value,
        "pit_mode": pit_mode.value,
        "pit_mode_note": mode_note,
        "availability_rationale": rationale,
        "verification_status": "UNVERIFIED",    # 正文归档与引用核验之前不得声称已核验
        "market_direction": "UNKNOWN",          # §15.3 允许保留未知方向
    }


class CninfoClient:
    source_id = SOURCE_ID

    def __init__(self, archive: ForwardArchive, *, policy: FetchPolicy | None = None,
                 limiter: RateLimiter | None = None, breaker: CircuitBreaker | None = None,
                 retry: RetryPolicy | None = None, sleep=None, timeout: float = 25.0) -> None:
        self.archive = archive
        self.policy = policy or default_policy()
        self.limiter = limiter or RateLimiter(min_interval_seconds=2.5)
        self.breaker = breaker or CircuitBreaker(failure_threshold=3, cooldown_seconds=120.0)
        self.retry = retry or RetryPolicy(max_attempts=2, base_delay_seconds=3.0,
                                          max_delay_seconds=6.0)
        import time as _time
        self._sleep = sleep or _time.sleep
        self.timeout = timeout

    def _fetch(self, url: str, *, label: str, data: bytes | None = None,
               content_type: str | None = None) -> FetchOutcome:
        requested_at = datetime.now(timezone.utc)
        host = urlsplit(url).hostname or ""

        try:
            self.breaker.assert_closed()
        except CircuitOpen as exc:
            r = self.archive.record(source_id=SOURCE_ID, url=url, outcome="DENIED",
                                    requested_at=requested_at,
                                    detail=f"circuit open, retry after {exc.retry_after_seconds:.0f}s")
            return FetchOutcome(False, None, r.receipt_id, None, r.detail)

        try:
            assert_url_allowed(url, self.policy)
        except FetchDenied as exc:
            r = self.archive.record(source_id=SOURCE_ID, url=url, outcome="DENIED",
                                    requested_at=requested_at,
                                    detail=f"{exc.reason}: {exc.detail}")
            return FetchOutcome(False, None, r.receipt_id, None, r.detail)

        last = "unknown"
        for attempt in range(1, self.retry.max_attempts + 1):
            self.limiter.wait(host, sleep=self._sleep)
            opener = urllib.request.build_opener(_ValidatingRedirectHandler(self.policy))
            headers = {"User-Agent": UA, "Accept": "*/*",
                       "Referer": f"http://{QUERY_HOST}/new/commonUrl?url=disclosure/list/notice",
                       "X-Requested-With": "XMLHttpRequest"}
            if content_type:
                headers["Content-Type"] = content_type
            req = urllib.request.Request(url, data=data, headers=headers)
            try:
                with opener.open(req, timeout=self.timeout) as resp:
                    check_content_length(resp.headers.get("Content-Length"), self.policy)
                    body = read_bounded(resp, self.policy)
                    status = resp.status
                self.breaker.record_success()
                media = "application/pdf" if url.lower().endswith(".pdf") else "application/json"
                digest, _ = self.archive.store_bytes(body, media_type=media)
                r = self.archive.record(source_id=SOURCE_ID, url=url, outcome="OK",
                                        requested_at=requested_at, http_status=status,
                                        content_hash=digest, byte_size=len(body),
                                        detail=label)
                return FetchOutcome(True, body, r.receipt_id, digest, None, status)
            except urllib.error.HTTPError as exc:
                last = f"HTTP {exc.code}"
                self.breaker.record_failure()
                r = self.archive.record(source_id=SOURCE_ID, url=url, outcome="HTTP_ERROR",
                                        requested_at=requested_at, http_status=exc.code,
                                        detail=last)
                return FetchOutcome(False, None, r.receipt_id, None, last, exc.code)
            except (urllib.error.URLError, socket.timeout, TimeoutError,
                    ConnectionError, OSError) as exc:
                last = f"{type(exc).__name__}: {exc}"
                self.breaker.record_failure()
                if attempt < self.retry.max_attempts:
                    self._sleep(self.retry.delay_for(attempt))
                    continue
                r = self.archive.record(source_id=SOURCE_ID, url=url,
                                        outcome="TRANSPORT_ERROR", requested_at=requested_at,
                                        detail=f"{last} (after {attempt} attempts)")
                return FetchOutcome(False, None, r.receipt_id, None, r.detail)
        return FetchOutcome(False, None, "", None, last)

    def announcements(self, *, begin: date, end: date, column: str = "szse",
                      page_size: int = 30) -> tuple[FetchOutcome, list[Announcement]]:
        """查询公告列表。

        column: szse（深市）/ sse（沪市）。日期区间按北京时间自然日。
        """

        body = urllib.parse.urlencode({
            "pageNum": 1, "pageSize": page_size, "column": column, "tabName": "fulltext",
            "plate": "", "stock": "", "searchkey": "", "secid": "", "category": "",
            "trade": "", "seDate": f"{begin.isoformat()}~{end.isoformat()}",
            "sortName": "", "sortType": "", "isHLtitle": "true",
        }).encode()
        url = f"http://{QUERY_HOST}/new/hisAnnouncement/query"
        out = self._fetch(url, label=f"announcements:{column}:{begin}~{end}", data=body,
                          content_type="application/x-www-form-urlencoded; charset=UTF-8")
        if not out.ok or out.payload is None:
            return out, []
        try:
            return out, parse_announcements(out.payload)
        except json.JSONDecodeError as exc:
            return FetchOutcome(False, None, out.receipt_id, out.content_hash,
                                f"malformed JSON: {exc}"), []

    def document(self, url: str, *, label: str = "") -> FetchOutcome:
        """抓取公告原文（通常是 PDF）并归档。引用核验必须针对原文，而不是标题。"""

        return self._fetch(url, label=label or f"document:{url.rsplit('/', 1)[-1]}")
