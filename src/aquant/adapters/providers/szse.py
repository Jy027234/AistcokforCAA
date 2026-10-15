"""深交所公告备源适配器：只在字节同一性可证时提供备源证据。

本模块把 2026-09-28 已实测的深交所网页请求工程化：

.. code-block:: text

    POST https://www.szse.cn/api/search/content
    Content-Type: application/x-www-form-urlencoded; charset=UTF-8
    keyword=<code>  range=title  channelCode[]=fixed_disc|listedNotice_disc
    time=<ms>  endTime=<ms>  currentPage=<n>  pageSize=<n>

边界（写进代码而不是只写进文档）：

* 这是**网页请求形式，不是交易所承诺稳定的开放 API**，因此每次调用都保留
  请求体与响应字节、收据与哈希，接口变化按失败处理而不是猜。
* 列表标题可能带 HTML 标签；响应没有独立的证券代码字段，所以关键字命中
  **不等于**身份校验通过，必须另取原文核对公司身份。
* ``docpubtime`` 只有日期级含义，不得据此编造发布时刻。
* 请求 ``pageSize=100`` 曾被服务端规范为 50，因此必须按返回的分页信息遍历、
  去重并核对总数；只取第一页不算完整。
* **同站/跨站相同 PDF 哈希不是独立数值验证。** 因此 :func:`cross_source_evidence`
  返回的结论明确区分"字节一致"与"数值已独立核实"，后者永远为假：它需要
  第二条数值抽取路径，而不是同一个文件的第二份拷贝。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta, timezone
from typing import Mapping
from urllib.parse import quote_plus

from ...domain.data.forward_archive import ForwardArchive
from .eastmoney import _ValidatingRedirectHandler
from .fetch_guard import (
    FetchDenied, FetchPolicy, assert_url_allowed, check_content_length, read_bounded,
)
from .resilience import CircuitBreaker, CircuitOpen, RateLimiter, RetryPolicy


SOURCE_ID = "szse"
QUERY_URL = "https://www.szse.cn/api/search/content"
QUERY_HOST = "www.szse.cn"
STATIC_HOST = "disc.static.szse.cn"
REFERER_TEMPLATE = "https://www.szse.cn/disclosure/listed/{page}/index.html?stock={code}"
#: 定报分类与全类别分类。前者快速定位全文，后者负责发现更正/修订/补充。
CHANNEL_FIXED = "fixed_disc"
CHANNEL_ALL_NOTICES = "listedNotice_disc"
SHANGHAI = timezone(timedelta(hours=8))
#: 服务端会把更大的 pageSize 规范化到这个上限。
MAX_PAGE_SIZE = 50
_TAG = re.compile(r"<[^>]*>")


class SzseError(ValueError):
    """深交所备源未能提供可复核的证据。"""


def default_policy() -> FetchPolicy:
    """按 ``AQUANT_TRUSTED_PROXY_NETWORKS`` 构造策略；默认不放行任何伪 IP。"""

    import os

    trusted = frozenset(
        part.strip() for part in os.environ.get(
            "AQUANT_TRUSTED_PROXY_NETWORKS", "").split(",") if part.strip())
    return FetchPolicy(
        allowed_hosts=frozenset({QUERY_HOST, STATIC_HOST}),
        max_redirects=3,
        max_response_bytes=16 * 1024 * 1024,
        trusted_proxy_networks=trusted,
    )


def strip_tags(title: str) -> str:
    """去掉检索结果标题里的高亮标签。"""

    return _TAG.sub("", title or "").strip()


@dataclass(frozen=True, slots=True)
class SzseAnnouncement:
    """一条深交所公告列表记录。"""

    document_id: str
    title: str
    published_on: date | None
    document_url: str | None
    doc_type: str | None

    @property
    def is_full_report(self) -> bool:
        """标题**以**定期报告名结尾才算全文。

        仅做子串包含会把"关于参加…半年度报告业绩说明会的公告"也判成全文
        （实测 300502 的全类别结果里就有这一条），从而把一份说明性公告当成
        财报原文。这里要求标题在去掉证券简称前缀后正好落在报告名上。
        """

        title = self.title.strip()
        if "摘要" in title or "英文" in title:
            return False
        tail = title.rsplit("：", 1)[-1].strip() if "：" in title else title
        return bool(re.fullmatch(
            r"(?:\d{4}\s*年)?(?:年度|半年度|第一季度|第三季度|第[1-4]季度)"
            r"(?:报告)?", tail))


@dataclass(frozen=True, slots=True)
class SzseIndexPage:
    page: int
    request_body_hash: str
    receipt_id: str
    content_hash: str | None
    raw_count: int | None
    total: int | None
    has_more: bool | None


@dataclass(frozen=True, slots=True)
class SzseIndexResult:
    stock_code: str
    channel: str
    window: tuple[date, date]
    pages: tuple[SzseIndexPage, ...]
    announcements: tuple[SzseAnnouncement, ...]
    complete: bool
    termination: str
    error: str | None = None

    @property
    def document_ids(self) -> tuple[str, ...]:
        return tuple(item.document_id for item in self.announcements)


def _epoch_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=SHANGHAI).timestamp() * 1000)


def _published_on(value: object) -> date | None:
    """``docpubtime`` 只有日期级含义；解析不出来就保持缺失。"""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000, tz=SHANGHAI).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value or "").strip()
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def parse_index_payload(payload: bytes | str | Mapping) -> tuple[
        list[SzseAnnouncement], int | None, bool | None]:
    """解析检索响应，返回 (公告列表, 总数, 是否还有下一页)。"""

    if isinstance(payload, Mapping):
        document = dict(payload)
    else:
        text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
        try:
            document = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise SzseError("SZSE search response is not JSON") from exc
    if not isinstance(document, Mapping):
        raise SzseError("SZSE search response is not an object")
    rows = document.get("data")
    if isinstance(rows, Mapping):
        rows = rows.get("content") or rows.get("list") or rows.get("records")
    if rows is None:
        for key in ("content", "list", "records", "results"):
            if isinstance(document.get(key), list):
                rows = document[key]
                break
    if not isinstance(rows, list):
        raise SzseError("SZSE search response has no announcement list")
    announcements: list[SzseAnnouncement] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        document_id = str(row.get("id") or row.get("docId") or "").strip()
        if not document_id:
            raise SzseError("SZSE row has no document id")
        url = row.get("docpuburl") or row.get("attachPath") or None
        announcements.append(SzseAnnouncement(
            document_id=document_id,
            title=strip_tags(str(row.get("doctitle") or row.get("title") or "")),
            published_on=_published_on(row.get("docpubtime") or row.get("publishtime")),
            document_url=str(url) if url else None,
            doc_type=(str(row["doctype"]) if row.get("doctype") else None),
        ))
    total_raw = document.get("totalSize", document.get("total"))
    total = None
    if isinstance(total_raw, (int, str)) and not isinstance(total_raw, bool):
        try:
            total = int(total_raw)
        except (TypeError, ValueError):
            total = None
    has_more_raw = document.get("hasNext", document.get("hasMore"))
    has_more = None
    if isinstance(has_more_raw, bool):
        has_more = has_more_raw
    elif isinstance(has_more_raw, str) and has_more_raw.strip().lower() in ("true", "false"):
        has_more = has_more_raw.strip().lower() == "true"
    return announcements, total, has_more


class SzseClient:
    """带限速、熔断、有限重试与归档的深交所检索客户端。"""

    source_id = SOURCE_ID

    def __init__(self, archive: ForwardArchive, *, policy: FetchPolicy | None = None,
                 limiter: RateLimiter | None = None,
                 breaker: CircuitBreaker | None = None,
                 retry: RetryPolicy | None = None, sleep=None,
                 timeout: float = 25.0) -> None:
        self.archive = archive
        self.policy = policy or default_policy()
        # 深交所从 3 秒、每域并发 1 起步（见实施方案 §5.2）。
        self.limiter = limiter or RateLimiter(min_interval_seconds=3.0)
        self.breaker = breaker or CircuitBreaker(
            failure_threshold=3, cooldown_seconds=120.0)
        self.retry = retry or RetryPolicy(
            max_attempts=2, base_delay_seconds=3.0, max_delay_seconds=6.0)
        self._sleep = sleep or __import__("time").sleep
        self.timeout = timeout

    def _fetch(self, url: str, *, label: str, data: bytes,
               content_type: str) -> tuple[bytes, dict]:
        try:
            self.breaker.assert_closed()
        except CircuitOpen as exc:
            raise SzseError(f"circuit open for {url}") from exc
        try:
            assert_url_allowed(url, self.policy)
        except FetchDenied as exc:
            raise SzseError(f"{exc.reason}: {exc.detail}") from exc

        request_body_hash, _ = self.archive.store_bytes(
            data, media_type="application/x-www-form-urlencoded")
        last: str | None = None
        attempts = max(1, self.retry.max_attempts)
        for attempt in range(attempts):
            self.limiter.wait(url, sleep=self._sleep)
            opener = urllib.request.build_opener(
                _ValidatingRedirectHandler(self.policy))
            request = urllib.request.Request(
                url, data=data, method="POST",
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120 Safari/537.36"),
                    "Accept": "application/json",
                    "Content-Type": content_type,
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": REFERER_TEMPLATE.format(page="fixed", code=""),
                },
            )
            requested_at = datetime.now(timezone.utc)
            try:
                with opener.open(request, timeout=self.timeout) as response:
                    check_content_length(
                        response.headers.get("Content-Length"), self.policy)
                    payload = read_bounded(response, self.policy)
                    status = getattr(response, "status", 200)
                    responded_at = datetime.now(timezone.utc)
            except urllib.error.HTTPError as exc:
                self.breaker.record_failure()
                self.archive.record(
                    source_id=self.source_id, url=url, outcome="HTTP_ERROR",
                    requested_at=requested_at, responded_at=datetime.now(timezone.utc),
                    http_status=exc.code, detail=f"{label}: HTTP {exc.code}")
                raise SzseError(f"SZSE returned HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = f"{type(exc).__name__}: {exc}"
                self.breaker.record_failure()
                if attempt + 1 < attempts:
                    self._sleep(self.retry.delay_for(attempt + 1))
                    continue
                self.archive.record(
                    source_id=self.source_id, url=url, outcome="TRANSPORT_ERROR",
                    requested_at=requested_at, responded_at=datetime.now(timezone.utc),
                    detail=f"{label}: {last}")
                raise SzseError(f"SZSE transport failure: {last}") from exc
            content_hash, _ = self.archive.store_bytes(
                payload, media_type="application/json")
            receipt = self.archive.record(
                source_id=self.source_id, url=url, outcome="OK",
                requested_at=requested_at, responded_at=responded_at,
                http_status=status, content_hash=content_hash,
                byte_size=len(payload),
                detail=f"{label}: request_body_hash={request_body_hash}")
            self.breaker.record_success()
            info = {field.name: getattr(receipt, field.name)
                    for field in fields(receipt)}
            info["request_body_hash"] = request_body_hash
            info["payload"] = payload
            return payload, info
        raise SzseError(f"SZSE request failed: {last}")

    def download(self, url: str, *, label: str = "",
                 max_bytes: int | None = None) -> tuple[bytes, dict]:
        """抓取并归档一份官方 PDF；返回 (字节, 收据信息)。

        与检索请求共用同一套守卫、限速、熔断与有限重试，因此备源下载不会绕过
        ``FetchGuard`` 的域名、协议、端口、体积与重定向检查。
        """

        import urllib.request

        try:
            self.breaker.assert_closed()
        except CircuitOpen as exc:
            raise SzseError(f"circuit open for {url}") from exc
        try:
            assert_url_allowed(url, self.policy)
        except FetchDenied as exc:
            raise SzseError(f"{exc.reason}: {exc.detail}") from exc
        header, _, host = url.partition("://")
        host = host.split("/", 1)[0]
        if host != STATIC_HOST:
            raise SzseError(f"PDF downloads must come from {STATIC_HOST}, got {host!r}")
        if not url.lower().endswith(".pdf"):
            raise SzseError("SZSE document url must end with .PDF")

        last: str | None = None
        attempts = max(1, self.retry.max_attempts)
        for attempt in range(attempts):
            self.limiter.wait(url, sleep=self._sleep)
            opener = urllib.request.build_opener(
                _ValidatingRedirectHandler(self.policy))
            request = urllib.request.Request(url, method="GET", headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120 Safari/537.36"),
                "Accept": "application/pdf,*/*",
            })
            requested_at = datetime.now(timezone.utc)
            try:
                with opener.open(request, timeout=self.timeout) as response:
                    check_content_length(
                        response.headers.get("Content-Length"), self.policy,
                        max_bytes=max_bytes)
                    payload = read_bounded(response, self.policy, max_bytes=max_bytes)
                    status = getattr(response, "status", 200)
                    responded_at = datetime.now(timezone.utc)
            except urllib.error.HTTPError as exc:
                self.breaker.record_failure()
                self.archive.record(
                    source_id=self.source_id, url=url, outcome="HTTP_ERROR",
                    requested_at=requested_at, responded_at=datetime.now(timezone.utc),
                    http_status=exc.code, detail=f"{label or url}: HTTP {exc.code}")
                raise SzseError(f"SZSE returned HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = f"{type(exc).__name__}: {exc}"
                self.breaker.record_failure()
                if attempt + 1 < attempts:
                    self._sleep(self.retry.delay_for(attempt + 1))
                    continue
                self.archive.record(
                    source_id=self.source_id, url=url, outcome="TRANSPORT_ERROR",
                    requested_at=requested_at, responded_at=datetime.now(timezone.utc),
                    detail=f"{label or url}: {last}")
                raise SzseError(f"SZSE transport failure: {last}") from exc
            if not payload.startswith(b"%PDF-"):
                raise SzseError("SZSE document is not a PDF")
            content_hash, _ = self.archive.store_bytes(
                payload, media_type="application/pdf")
            receipt = self.archive.record(
                source_id=self.source_id, url=url, outcome="OK",
                requested_at=requested_at, responded_at=responded_at,
                http_status=status, content_hash=content_hash,
                byte_size=len(payload), detail=label or url)
            self.breaker.record_success()
            info = {field.name: getattr(receipt, field.name)
                    for field in fields(receipt)}
            info["payload"] = payload
            return payload, info
        raise SzseError(f"SZSE download failed: {last}")

    def index(self, *, stock_code: str, begin: date, end: date,
              channel: str = CHANNEL_FIXED, page_size: int = 20,
              max_pages: int = 20) -> SzseIndexResult:
        """按证券与日期窗口检索公告；分页不一致即失败关闭。"""

        if not re.fullmatch(r"\d{6}", stock_code):
            raise SzseError("stock_code must be six digits")
        if begin > end:
            raise SzseError("begin must not be after end")
        page_size = min(max(1, page_size), MAX_PAGE_SIZE)
        url = QUERY_URL
        pages: list[SzseIndexPage] = []
        announcements: list[SzseAnnouncement] = []
        seen: set[str] = set()
        total: int | None = None
        complete = False
        termination = "pagination_unknown"
        try:
            for page in range(1, max_pages + 1):
                body = urllib_parse({
                    "keyword": stock_code, "range": "title",
                    "channelCode[]": channel,
                    "time": str(_epoch_ms(begin)), "endTime": str(_epoch_ms(end)),
                    "currentPage": str(page), "pageSize": str(page_size),
                })
                payload, info = self._fetch(
                    url, label=f"szse-index:{stock_code}:p{page}",
                    data=body,
                    content_type="application/x-www-form-urlencoded; charset=UTF-8")
                found, page_total, has_more = parse_index_payload(payload)
                if page_total is not None:
                    if total is not None and total != page_total:
                        return SzseIndexResult(
                            stock_code=stock_code, channel=channel, window=(begin, end),
                            pages=tuple(pages), announcements=(),
                            complete=False, termination="pagination_inconsistent",
                            error=f"totalSize changed from {total} to {page_total}")
                    total = page_total
                pages.append(SzseIndexPage(
                    page=page, request_body_hash=info["request_body_hash"],
                    receipt_id=info["receipt_id"], content_hash=info.get("content_hash"),
                    raw_count=len(found), total=page_total, has_more=has_more))
                for item in found:
                    if item.document_id in seen:
                        return SzseIndexResult(
                            stock_code=stock_code, channel=channel, window=(begin, end),
                            pages=tuple(pages), announcements=(),
                            complete=False, termination="identity_incomplete",
                            error=f"duplicate document id {item.document_id}")
                    seen.add(item.document_id)
                    announcements.append(item)
                if has_more is False:
                    complete, termination = True, "has_more_false"
                    break
                if total is not None and len(announcements) >= total:
                    complete, termination = True, "total_reached"
                    break
                if has_more is None and total is None:
                    termination = "pagination_unknown"
                    break
            else:
                termination = "max_pages_truncated"
        except SzseError as exc:
            return SzseIndexResult(
                stock_code=stock_code, channel=channel, window=(begin, end),
                pages=tuple(pages), announcements=(), complete=False,
                termination="page_fetch_failed", error=str(exc)[:200])
        if not complete:
            # 截断的证据不得暴露候选。
            return SzseIndexResult(
                stock_code=stock_code, channel=channel, window=(begin, end),
                pages=tuple(pages), announcements=(), complete=False,
                termination=termination)
        return SzseIndexResult(
            stock_code=stock_code, channel=channel, window=(begin, end),
            pages=tuple(pages), announcements=tuple(announcements),
            complete=True, termination=termination)


def urllib_parse(fields: Mapping[str, str]) -> bytes:
    """构造表单体。``channelCode[]`` 的方括号必须原样保留。"""

    return "&".join(
        f"{quote_plus(str(key), safe='[]')}={quote_plus(str(value))}"
        for key, value in fields.items()
    ).encode("utf-8")


def cross_source_evidence(*, document_id: str, szse_bytes: bytes,
                          cninfo_bytes: bytes | None) -> dict[str, object]:
    """比较深交所与巨潮的同一份原文，并如实标注它证明了什么。

    字节一致证明**原文字节一致、来源可互证**；它**不**证明数值已经被独立
    核实——那需要第二条数值抽取路径，而不是同一份文件的第二个拷贝。因此
    ``numeric_independence`` 恒为 ``False``。
    """

    szse_hash = "sha256:" + hashlib.sha256(szse_bytes).hexdigest()
    result: dict[str, object] = {
        "szse_document_id": document_id,
        "szse_content_hash": szse_hash,
        "szse_byte_size": len(szse_bytes),
        "cninfo_content_hash": None,
        "byte_identical": False,
        "numeric_independence": False,
        "conclusion": (
            "same bytes only prove the two sites served the same document; they are "
            "not an independent numeric verification"),
    }
    if cninfo_bytes is None:
        return result
    cninfo_hash = "sha256:" + hashlib.sha256(cninfo_bytes).hexdigest()
    result["cninfo_content_hash"] = cninfo_hash
    result["byte_identical"] = (
        szse_hash == cninfo_hash and szse_bytes == cninfo_bytes)
    return result


__all__ = [
    "CHANNEL_ALL_NOTICES", "CHANNEL_FIXED", "MAX_PAGE_SIZE", "SOURCE_ID",
    "SzseAnnouncement", "SzseClient", "SzseError", "SzseIndexPage",
    "SzseIndexResult", "cross_source_evidence", "default_policy",
    "parse_index_payload", "strip_tags", "urllib_parse",
]
