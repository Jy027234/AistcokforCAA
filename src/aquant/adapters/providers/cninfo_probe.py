"""CNINFO 报告期公告的受限元数据探针。

这个模块只面向公告索引，不下载 PDF，也不保存接口原文。它的输入是一个
沪深 A 股代码和报告期（例如 ``2024-12-31`` 或 ``2024Q4``），输出公告标题、
``announcementTime``、公告 ID/URL 以及从标题中观察到的更正/修订标记。

CNINFO 的 ``searchkey`` 会漏掉“一季度/第一季度”等同义标题，因此探针使用
官方定期报告分类召回，收到响应后仍按证券代码和报告期标题严格过滤。标题
可能带 ``<em>`` 高亮，解析时会去除。响应
只在进程内解析，报告可保留字节数和 SHA-256 以便审计，但不会保存财务正文。

这不是财务报表解析器，也不能凭标题证明一份报告确实替代了另一份报告。
``is_correction`` / ``is_revision`` 是标题证据，真正的版本关系仍需人工或
正文层复核。
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen


SOURCE_ID = "cninfo"
QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
LOOKUP_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"
STATIC_BASE_URL = "https://static.cninfo.com.cn/"
QUERY_HOST = "www.cninfo.com.cn"
STATIC_HOST = "static.cninfo.com.cn"
USER_AGENT = "aquant-cninfo-report-probe/1"
DEFAULT_PAGE_SIZE = 30
DEFAULT_MAX_PAGES = 5
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
PUBLICATION_WINDOW_DAYS = 730

_HIGHLIGHT_RE = re.compile(r"<[^>]+>")
_CODE_RE = re.compile(r"^(?:sh|sz)?(\d{6})$", re.IGNORECASE)
_DATE_RE = re.compile(r"^(\d{4})[-/]?(\d{2})[-/]?(\d{2})$")
_QUARTER_RE = re.compile(r"^(\d{4})[- ]?[Qq]([1-4])$")

_CORRECTION_MARKERS = ("更正", "勘误", "纠正", "纠错")
_REVISION_MARKERS = ("修订",)
_SUPPLEMENT_MARKERS = ("补充",)


class CninfoProbeError(RuntimeError):
    """输入、响应或传输不满足受限探针契约。"""


@dataclass(frozen=True, slots=True)
class ReportPeriod:
    """一个可映射到 CNINFO 标题的报告期。"""

    input_value: str
    year: int
    quarter: int
    end_date: date
    report_kind: str
    category: str
    title_query: str
    title_markers: tuple[str, ...]

    @property
    def publication_window(self) -> tuple[date, date]:
        return self.end_date, self.end_date + timedelta(days=PUBLICATION_WINDOW_DAYS)

    def as_dict(self) -> dict[str, Any]:
        start, end = self.publication_window
        return {
            "input": self.input_value,
            "year": self.year,
            "quarter": self.quarter,
            "end_date": self.end_date.isoformat(),
            "report_kind": self.report_kind,
            "category": self.category,
            "title_query": self.title_query,
            "title_markers": list(self.title_markers),
            "publication_window": [start.isoformat(), end.isoformat()],
        }


@dataclass(frozen=True, slots=True)
class AnnouncementMetadata:
    """不含原始正文的一条公告元数据。"""

    announcement_id: str
    sec_code: str
    sec_name: str
    title: str
    announcement_time_ms: int
    announcement_time_utc: str
    announcement_date_cn: str
    url: str | None
    adjunct_path: str | None
    report_period: str
    report_kind: str
    is_correction: bool
    is_revision: bool
    is_supplement: bool
    has_correction_or_revision: bool
    title_markers: tuple[str, ...]
    evidence_basis: str = "title_only"

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["title_markers"] = list(self.title_markers)
        return value


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """传输层最小返回值；body 只供当前进程解析。"""

    status_code: int
    body: bytes
    content_type: str | None = None


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """可序列化的去原文探针报告。"""

    source_id: str
    endpoint: str
    stock_code: str
    market: str
    organization_id: str
    report_period: ReportPeriod
    requested_at: str
    finished_at: str
    request_count: int
    pages_fetched: int
    http_statuses: tuple[int, ...]
    response_bytes: int
    response_sha256: tuple[str, ...]
    total_announcement: int | None
    has_more: bool | None
    announcements: tuple[AnnouncementMetadata, ...]
    skipped: Mapping[str, int]
    warnings: tuple[str, ...]
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "probe": {
                "source_id": self.source_id,
                "endpoint": self.endpoint,
                "stock_code": self.stock_code,
                "market": self.market,
                "organization_id": self.organization_id,
                "report_period": self.report_period.as_dict(),
                "requested_at": self.requested_at,
                "finished_at": self.finished_at,
                "request_count": self.request_count,
                "pages_fetched": self.pages_fetched,
                "http_statuses": list(self.http_statuses),
                "response_bytes": self.response_bytes,
                "response_sha256": list(self.response_sha256),
                "total_announcement": self.total_announcement,
                "has_more": self.has_more,
                "matched_announcement_count": len(self.announcements),
                "skipped": dict(sorted(self.skipped.items())),
                "warnings": list(self.warnings),
                "error": self.error,
            },
            "announcements": [item.as_dict() for item in self.announcements],
        }


Transport = Callable[[str, bytes, Mapping[str, str], float], HttpResponse]
OrganizationResolver = Callable[[str, float], str]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat()


def normalize_title(value: Any) -> str:
    """去掉 CNINFO 高亮标签并还原实体，不保留 HTML。"""

    text = "" if value is None else str(value)
    text = _HIGHLIGHT_RE.sub("", text)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def normalize_stock_code(value: str) -> str:
    """接受六位代码或 ``sh/sz`` 前缀，统一为六位数字。"""

    text = str(value or "").strip()
    match = _CODE_RE.fullmatch(text)
    if not match:
        raise CninfoProbeError("stock code must be a six-digit A-share code")
    return match.group(1)


def market_for_code(stock_code: str) -> str:
    code = normalize_stock_code(stock_code)
    if code.startswith(("000", "001", "002", "003", "300", "301")):
        return "szse"
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return "sse"
    raise CninfoProbeError(f"unsupported A-share market prefix: {code[:3]}")


def organization_id(stock_code: str, market: str | None = None) -> str:
    code = normalize_stock_code(stock_code)
    market_name = market or market_for_code(code)
    prefix = {"sse": "gssh", "szse": "gssz"}.get(market_name)
    if prefix is None:
        raise CninfoProbeError(f"unsupported market: {market_name}")
    # CNINFO uses a seven-digit internal organization code, e.g.
    # 600519,gssh0600519 and 000001,gssz0000001.
    return prefix + code.zfill(7)


def parse_report_period(value: str) -> ReportPeriod:
    """解析 ``YYYY[-MM-DD]``、``YYYYMMDD``、``YYYYQn`` 或 ``YYYY``。

    只接受四个标准季末，避免把任意日期猜成财报期。Q4/12-31 统一到年报。
    """

    text = str(value or "").strip()
    if not text:
        raise CninfoProbeError("report period is empty")

    year: int
    quarter: int
    date_match = _DATE_RE.fullmatch(text)
    quarter_match = _QUARTER_RE.fullmatch(text)
    if text.isdigit() and len(text) == 4:
        year, quarter = int(text), 4
    elif quarter_match:
        year, quarter = int(quarter_match.group(1)), int(quarter_match.group(2))
    elif date_match:
        year, month, day = (int(part) for part in date_match.groups())
        expected = {(3, 31): 1, (6, 30): 2, (9, 30): 3, (12, 31): 4}
        quarter = expected.get((month, day), 0)
        if not quarter:
            raise CninfoProbeError("report period date must be a standard quarter end")
    else:
        raise CninfoProbeError("report period must be YYYY, YYYYQn, or a quarter-end date")

    if not 2000 <= year <= 2100:
        raise CninfoProbeError("report period year is outside the supported range")
    end_dates = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
    month, day = end_dates[quarter]
    end_date = date(year, month, day)
    labels = {
        1: ("q1", "category_yjdbg_szsh", f"{year}年第一季度报告", f"{year}年一季度报告"),
        2: ("semiannual", "category_bndbg_szsh", f"{year}年半年度报告"),
        3: ("q3", "category_sjdbg_szsh", f"{year}年第三季度报告", f"{year}年三季度报告"),
        4: ("annual", "category_ndbg_szsh", f"{year}年年度报告"),
    }
    raw = labels[quarter]
    return ReportPeriod(
        input_value=text,
        year=year,
        quarter=quarter,
        end_date=end_date,
        report_kind=raw[0],
        category=raw[1],
        title_query=raw[2],
        title_markers=tuple(raw[2:]),
    )


def title_matches_period(title: str, period: ReportPeriod) -> bool:
    canonical = normalize_title(title)
    return any(marker in canonical for marker in period.title_markers)


def title_revision_flags(title: str) -> tuple[bool, bool, bool, tuple[str, ...]]:
    """从标题提取可审计标记；不把“补充”冒充“更正”。"""

    canonical = normalize_title(title)
    markers = tuple(dict.fromkeys(
        marker for marker in (*_CORRECTION_MARKERS, *_REVISION_MARKERS,
                               *_SUPPLEMENT_MARKERS)
        if marker in canonical
    ))
    is_correction = any(marker in canonical for marker in _CORRECTION_MARKERS)
    is_revision = any(marker in canonical for marker in _REVISION_MARKERS)
    is_supplement = any(marker in canonical for marker in _SUPPLEMENT_MARKERS)
    return is_correction, is_revision, is_supplement, markers


def _adjunct_url(value: Any) -> str | None:
    if value in (None, ""):
        return None
    path = str(value).strip()
    # The response is expected to contain a relative path. Reject absolute or
    # cross-host URLs so a malformed result cannot silently redirect the probe.
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"http", "https"} or parsed.hostname != STATIC_HOST:
            return None
        return path
    return urljoin(STATIC_BASE_URL, "/" + path.lstrip("/"))


def _announcement_time(value: Any) -> tuple[int, str, str] | None:
    if isinstance(value, bool):
        return None
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    instant = datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
    china_date = instant.astimezone(timezone(timedelta(hours=8))).date().isoformat()
    return millis, instant.isoformat(), china_date


def _payload_bytes(payload: bytes | str | Mapping[str, Any]) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")


def parse_listing_payload(
    payload: bytes | str | Mapping[str, Any],
    *,
    stock_code: str,
    period: ReportPeriod,
) -> tuple[tuple[AnnouncementMetadata, ...], dict[str, int], int | None, bool | None]:
    """解析并严格过滤一页响应，不返回原始公告对象。"""

    code = normalize_stock_code(stock_code)
    if isinstance(payload, (bytes, str)):
        try:
            document = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise CninfoProbeError("CNINFO response is not valid JSON") from exc
    else:
        document = payload
    if not isinstance(document, Mapping):
        raise CninfoProbeError("CNINFO response is not an object")
    raw_announcements = document.get("announcements")
    if raw_announcements is None:
        raw_announcements = []
    if not isinstance(raw_announcements, list):
        raise CninfoProbeError("CNINFO announcements field is not a list")

    skipped: dict[str, int] = {}
    found: dict[str, AnnouncementMetadata] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for item in raw_announcements:
        if not isinstance(item, Mapping):
            skip("non_object_announcement")
            continue
        raw_code = str(item.get("secCode") or "").strip()
        try:
            item_code = normalize_stock_code(raw_code) if raw_code else ""
        except CninfoProbeError:
            skip("invalid_security_code")
            continue
        if item_code != code:
            skip("different_security")
            continue
        title = normalize_title(item.get("announcementTitle"))
        if not title_matches_period(title, period):
            skip("different_report_period")
            continue
        announcement_id = str(item.get("announcementId") or "").strip()
        if not announcement_id:
            skip("missing_announcement_id")
            continue
        when = _announcement_time(item.get("announcementTime"))
        if when is None:
            skip("missing_announcement_time")
            continue
        millis, instant, china_date = when
        is_correction, is_revision, is_supplement, markers = title_revision_flags(title)
        found[announcement_id] = AnnouncementMetadata(
            announcement_id=announcement_id,
            sec_code=item_code,
            sec_name=normalize_title(item.get("secName")),
            title=title,
            announcement_time_ms=millis,
            announcement_time_utc=instant,
            announcement_date_cn=china_date,
            url=_adjunct_url(item.get("adjunctUrl")),
            adjunct_path=(str(item.get("adjunctUrl")).strip()
                          if item.get("adjunctUrl") not in (None, "") else None),
            report_period=period.end_date.isoformat(),
            report_kind=period.report_kind,
            is_correction=is_correction,
            is_revision=is_revision,
            is_supplement=is_supplement,
            has_correction_or_revision=is_correction or is_revision,
            title_markers=markers,
        )

    rows = tuple(sorted(found.values(),
                        key=lambda row: (row.announcement_time_ms, row.announcement_id),
                        reverse=True))
    total = document.get("totalAnnouncement")
    try:
        total_value = int(total) if total is not None else None
    except (TypeError, ValueError):
        total_value = None
    raw_more = document.get("hasMore")
    if isinstance(raw_more, bool):
        has_more = raw_more
    elif isinstance(raw_more, str) and raw_more.strip().lower() in {"true", "false"}:
        has_more = raw_more.strip().lower() == "true"
    elif isinstance(raw_more, (int, float)) and not isinstance(raw_more, bool):
        has_more = bool(raw_more)
    else:
        has_more = None
    return rows, skipped, total_value, has_more


def urllib_transport(url: str, body: bytes, headers: Mapping[str, str],
                     timeout: float) -> HttpResponse:
    """向固定 CNINFO 查询端点发送一次 POST，不持久化响应。"""

    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname != QUERY_HOST or parts.port not in (None, 80):
        raise CninfoProbeError("refusing non-approved CNINFO query endpoint")
    request = Request(url, data=body, headers=dict(headers), method="POST")
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL is fixed above
        body_bytes = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body_bytes) > MAX_RESPONSE_BYTES:
            raise CninfoProbeError("CNINFO response exceeds bounded probe size")
        return HttpResponse(
            status_code=int(response.status),
            body=body_bytes,
            content_type=response.headers.get("Content-Type"),
        )


def urllib_resolve_organization_id(stock_code: str, timeout: float) -> str:
    """通过 CNINFO 官方搜索接口解析 orgId；代码不能可靠推导 orgId。"""

    code = normalize_stock_code(stock_code)
    query = urlencode({"keyWord": code, "maxNum": 10})
    request = Request(
        LOOKUP_URL + "?" + query,
        data=b"",
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Referer": "https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed URL
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise CninfoProbeError("CNINFO organization lookup exceeds bounded size")
    try:
        rows = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise CninfoProbeError("CNINFO organization lookup is not valid JSON") from exc
    if not isinstance(rows, list):
        raise CninfoProbeError("CNINFO organization lookup is not a list")
    matches = [str(row.get("orgId") or "").strip() for row in rows
               if isinstance(row, Mapping)
               and str(row.get("code") or "").strip() == code
               and str(row.get("orgId") or "").strip()]
    if len(set(matches)) != 1:
        raise CninfoProbeError(
            f"CNINFO organization lookup for {code} is missing or ambiguous")
    return matches[0]


class CninfoReportProbe:
    """按报告期查询 CNINFO 公告元数据。

    默认 transport 是惰性的离线函数；联网只能由 CLI 显式注入
    :func:`urllib_transport`，便于单元测试不会意外访问外网。
    """

    def __init__(self, *, transport: Transport | None = None,
                 organization_resolver: OrganizationResolver | None = None,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS,
                 page_size: int = DEFAULT_PAGE_SIZE,
                 max_pages: int = DEFAULT_MAX_PAGES,
                 clock: Callable[[], datetime] | None = None) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        if not 1 <= max_pages <= 100:
            raise ValueError("max_pages must be between 1 and 100")
        self._transport = transport or self._offline_transport
        self._organization_resolver = organization_resolver
        self._organization_cache: dict[str, str] = {}
        self._timeout = timeout
        self._page_size = page_size
        self._max_pages = max_pages
        self._clock = clock or _now

    @staticmethod
    def _offline_transport(url: str, body: bytes, headers: Mapping[str, str],
                           timeout: float) -> HttpResponse:
        raise CninfoProbeError("network transport is disabled; inject it explicitly")

    def _organization_id(self, code: str, market: str) -> str:
        if code not in self._organization_cache:
            self._organization_cache[code] = (
                self._organization_resolver(code, self._timeout)
                if self._organization_resolver is not None
                else organization_id(code, market)
            )
        return self._organization_cache[code]

    def _params(self, *, code: str, market: str, org_id: str,
                period: ReportPeriod,
                page_num: int) -> dict[str, Any]:
        start, end = period.publication_window
        return {
            "pageNum": page_num,
            "pageSize": self._page_size,
            "column": market,
            "tabName": "fulltext",
            "plate": "",
            "stock": code + "," + org_id,
            # 一季报标题可能写“第一季度”或“一季度”；用官方定期报告分类召回，
            # 再由 title_matches_period 严格过滤，避免 searchkey 漏掉同义标题。
            "searchkey": "",
            "secid": "",
            "category": period.category,
            "trade": "",
            "seDate": f"{start.isoformat()}~{end.isoformat()}",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }

    def query(self, *, stock_code: str, report_period: str) -> ProbeResult:
        code = normalize_stock_code(stock_code)
        market = market_for_code(code)
        period = parse_report_period(report_period)
        started = self._clock()
        statuses: list[int] = []
        response_hashes: list[str] = []
        response_bytes = 0
        skipped: dict[str, int] = {}
        warnings: list[str] = [
            "report-period matching and correction/revision flags are title-only evidence",
            "CNINFO report category is broad recall; local code/period filtering is applied",
        ]
        by_id: dict[str, AnnouncementMetadata] = {}
        total: int | None = None
        has_more: bool | None = None
        error: str | None = None
        pages = 0
        org_id = ""

        try:
            org_id = self._organization_id(code, market)
            for page_num in range(1, self._max_pages + 1):
                params = self._params(code=code, market=market, org_id=org_id,
                                      period=period,
                                      page_num=page_num)
                body = urlencode(params).encode("utf-8")
                headers = {
                    "User-Agent": USER_AGENT,
                    "Accept": "*/*",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
                    "X-Requested-With": "XMLHttpRequest",
                }
                response = self._transport(QUERY_URL, body, headers, self._timeout)
                pages += 1
                statuses.append(response.status_code)
                response_bytes += len(response.body)
                response_hashes.append("sha256:" + hashlib.sha256(response.body).hexdigest())
                if len(response.body) > MAX_RESPONSE_BYTES:
                    raise CninfoProbeError("CNINFO response exceeds bounded probe size")
                if response.status_code != 200:
                    error = f"HTTP {response.status_code}"
                    break
                rows, page_skipped, page_total, page_more = parse_listing_payload(
                    response.body, stock_code=code, period=period)
                for reason, count in page_skipped.items():
                    skipped[reason] = skipped.get(reason, 0) + count
                for row in rows:
                    by_id[row.announcement_id] = row
                if page_total is not None:
                    total = page_total
                has_more = page_more
                # When the API does not signal more pages, one page is enough.
                if page_more is False or not page_more:
                    break
        except Exception as exc:  # noqa: BLE001 - report only the exception type
            error = type(exc).__name__

        if pages == self._max_pages and has_more:
            warnings.append("max_pages reached while CNINFO reported more pages")
        finished = self._clock()
        return ProbeResult(
            source_id=SOURCE_ID,
            endpoint=QUERY_URL,
            stock_code=code,
            market=market,
            organization_id=org_id,
            report_period=period,
            requested_at=_iso(started),
            finished_at=_iso(finished),
            request_count=pages,
            pages_fetched=pages,
            http_statuses=tuple(statuses),
            response_bytes=response_bytes,
            response_sha256=tuple(response_hashes),
            total_announcement=total,
            has_more=has_more,
            announcements=tuple(sorted(
                by_id.values(),
                key=lambda row: (row.announcement_time_ms, row.announcement_id),
                reverse=True,
            )),
            skipped=skipped,
            warnings=tuple(warnings),
            error=error,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", required=True, help="六位沪深 A 股代码，例如 600519")
    parser.add_argument("--period", required=True,
                        help="报告期，例如 2024-12-31、2024Q4、2024")
    parser.add_argument("--output", type=Path,
                        help="可选：只写去原文元数据 JSON，不写原始响应")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    return parser


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        probe = CninfoReportProbe(
            transport=urllib_transport,
            organization_resolver=urllib_resolve_organization_id,
            timeout=args.timeout,
            page_size=args.page_size,
            max_pages=args.max_pages,
        )
        result = probe.query(stock_code=args.code, report_period=args.period)
        report = result.as_dict()
        if args.output:
            _write_report(args.output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if result.error is None else 1
    except (CninfoProbeError, OSError, ValueError) as exc:
        print(json.dumps({"error": {"type": type(exc).__name__}}, ensure_ascii=False),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
