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
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Mapping
from urllib.parse import urlsplit

from aquant.adapters.providers.cninfo_probe import (
    AnnouncementMetadata,
    CninfoProbeError,
    market_for_code,
    normalize_stock_code,
    normalize_title,
    parse_listing_payload,
    parse_report_period,
    title_matches_period,
    title_revision_flags,
)
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
ORG_LOOKUP_URL = f"https://{QUERY_HOST}/new/information/topSearch/query"
REPORT_QUERY_URL = f"http://{QUERY_HOST}/new/hisAnnouncement/query"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
PROXY_ENV = "AQUANT_TRUSTED_PROXY_NETWORKS"

#: 公告原文（PDF）的体积上限。列表响应仍用策略默认的 16 MiB。
DOCUMENT_MAX_BYTES = 64 * 1024 * 1024

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


@dataclass(frozen=True, slots=True)
class ReportIndexPage:
    """一页索引的请求体及原始响应均可由哈希重新核验。"""

    page_num: int
    request_body_hash: str
    receipt_id: str
    content_hash: str | None
    http_status: int | None
    raw_announcement_count: int | None
    total_announcement: int | None
    has_more: bool | None


@dataclass(frozen=True, slots=True)
class ReportIndexMatch:
    """公告身份直接绑定到发现它的归档页；标题标记不证明版本替代。"""

    announcement: AnnouncementMetadata
    page_num: int
    page_receipt_id: str
    page_content_hash: str


@dataclass(frozen=True, slots=True)
class ReportIndexResult:
    stock_code: str
    organization_id: str | None
    report_period: str
    market: str
    category: str
    publication_window: tuple[date, date]
    org_lookup_receipt_id: str | None
    org_lookup_content_hash: str | None
    pages: tuple[ReportIndexPage, ...]
    matches: tuple[ReportIndexMatch, ...]
    skipped: Mapping[str, int]
    total_announcement: int | None
    complete: bool
    termination: str
    error: str | None

    @property
    def announcements(self) -> tuple[AnnouncementMetadata, ...]:
        """只有完整耗尽的索引才暴露可用公告候选。"""

        return tuple(match.announcement for match in self.matches) if self.complete else ()


@dataclass(frozen=True, slots=True)
class CrossCategoryIndexEntry:
    """全分类索引的一条标题记录；候选理由不构成版本替代证明。"""

    announcement_id: str
    sec_code: str
    title: str
    announcement_time_ms: int
    document_url: str
    candidate_reasons: tuple[str, ...]
    page_num: int
    page_receipt_id: str
    page_content_hash: str


@dataclass(frozen=True, slots=True)
class CrossCategoryIndexResult:
    stock_code: str
    organization_id: str | None
    report_period: str
    publication_window: tuple[date, date]
    org_lookup_receipt_id: str | None
    org_lookup_content_hash: str | None
    pages: tuple[ReportIndexPage, ...]
    announcements: tuple[CrossCategoryIndexEntry, ...]
    skipped_different_security: int
    total_announcement: int | None
    complete: bool
    termination: str
    error: str | None

    @property
    def candidates(self) -> tuple[CrossCategoryIndexEntry, ...]:
        """标题筛查名单；必须进一步阅读正文确认与目标报告的关系。"""

        return tuple(entry for entry in self.announcements if entry.candidate_reasons)


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


# ======================================================================
# 分红公告解析（§12.7）
# ======================================================================
#
# 目标：从巨潮的**权益分派实施公告**正文里取出每股现金红利与三个日期，
# 并把每个值所依据的原文一并留下，供人工复核。
#
# 为什么必须留原文而不是只留数值
# ------------------------------
# 分红公告的文字千变万化："每 10 股派发现金红利 5.96 元（含税）"、
# "A 股每股现金红利 28.02423 元"、"每 10 股派发现金红利 2.60 元（含税）"。
# 一旦解析错（例如把"每 10 股"当成"每股"），金额会差 10 倍，
# 而账面完全自洽——没有人能从数字上看出问题。
# 留原文是让复核者能在几秒内判断对错，而不是重新去读 PDF。
#
# 解析**不做单位换算猜测**：公告说"每 10 股"就按 10 股换算并在原文里
# 保留该表述；公告同时给出"每股"和"每 10 股"时优先用"每股"。

#: 分红公告标题特征。只认**实施公告**——"利润分配方案""预案"里的
#: 日期是待股东大会审议的，不能当已确定的分派执行，两者混淆会让系统
#: 提前按未生效的方案记账。
DIVIDEND_TITLE_PATTERN = re.compile(r"(权益分派实施|分红派息实施)")

#: 需要人工复核的标题：方案/预案类，日期尚未确定
DIVIDEND_PROPOSAL_TITLE_PATTERN = re.compile(r"(利润分配方案|利润分配预案|分红方案)")

_CN_NUM = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_number(text: str) -> int | None:
    """解析公告里出现的简单中文数字（一~九十九）。"""

    text = text.strip()
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN_NUM.get(head, 1) if head else 1
        ones = _CN_NUM.get(tail, 0) if tail else 0
        if head and head not in _CN_NUM:
            return None
        if tail and tail not in _CN_NUM:
            return None
        return tens * 10 + ones
    return _CN_NUM.get(text)


def _context(flat: str, start: int, end: int, *, span: int = 60) -> str:
    """取匹配位置附近的原文片段（已去空白）作为留证。

    直接按位置取，不做 find：正则在去空白串上匹配，回到原文里 find
    通常会命中别处，证据就变成了无关文字——而"留证"的全部意义
    在于让人能一眼核对数值对不对。
    """

    return flat[max(0, start - span):end + span]

def _slice_sentence(text: str, index: int, *, span: int = 90) -> str:
    """取包含该位置的一句话，作为留证原文。"""

    start = max(0, index - span)
    end = min(len(text), index + span)
    snippet = text[start:end].replace("\n", " ")
    return re.sub(r"\s+", " ", snippet).strip()


@dataclass(frozen=True, slots=True)
class DividendParse:
    """从一份分红公告里解析出的结果。

    status:
      * `OK`             —— 金额与三个日期齐全
      * `INCOMPLETE`     —— 是分红公告，但关键字段没解析全（需人工）
      * `NOT_APPLICABLE` —— 不是分红实施公告
    """

    status: str
    action_id: str
    announced_on: date | None
    record_date: date | None
    ex_date: date | None
    pay_date: date | None
    cash_per_share_micros: int | None
    per_share_cents_exact: int | None
    tax_treatment: str
    evidence: dict[str, str]
    notes: list[str]

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "action_id": self.action_id,
            "announced_on": self.announced_on.isoformat() if self.announced_on else None,
            "record_date": self.record_date.isoformat() if self.record_date else None,
            "ex_date": self.ex_date.isoformat() if self.ex_date else None,
            "pay_date": self.pay_date.isoformat() if self.pay_date else None,
            "cash_per_share_micros": self.cash_per_share_micros,
            "per_share_cents_exact": self.per_share_cents_exact,
            "tax_treatment": self.tax_treatment,
            "evidence": self.evidence,
            "notes": self.notes,
        }


_DATE_RE = re.compile(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?")


def _dates_in(text: str) -> list[date]:
    out: list[date] = []
    for m in _DATE_RE.finditer(text):
        try:
            out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            continue
    return out


def parse_cash_dividend(*, text: str, title: str, announcement_id: str,
                        announced_on: date | None) -> DividendParse:
    """从公告正文解析现金分红。"""

    evidence: dict[str, str] = {}
    notes: list[str] = []

    if not DIVIDEND_TITLE_PATTERN.search(title or ""):
        if DIVIDEND_PROPOSAL_TITLE_PATTERN.search(title or ""):
            return DividendParse(
                "NOT_APPLICABLE", f"ca-{announcement_id}", announced_on, None, None, None,
                None, None, "PRE_TAX", {}, [
                    "标题是利润分配方案/预案而非实施公告：其中的日期尚需股东会审议，"
                    "不得当作已确定的分派执行",
                ])
        return DividendParse("NOT_APPLICABLE", f"ca-{announcement_id}", announced_on,
                             None, None, None, None, None, "PRE_TAX", {}, [])

    flat = re.sub(r"\s+", "", text or "")

    # ---------------------------------------------------------- 每股金额
    micros: int | None = None
    cents_exact: int | None = None

    # 优先"每股"表述：它是权威口径，无需换算
    m = re.search(r"每股(?:现金)?(?:红利|股利|股息)?(?:为|人民币|：|:)?([0-9]+(?:\.[0-9]+)?)元",
                  flat)
    if m:
        yuan = Decimal(m.group(1))
        micros = int(yuan * 1_000_000)
        # 证据直接取**匹配位置**的上下文，而不是回原文里 find 一次：
        # 正则在去空白串上匹配，位置与原文对不上，find 常常命中别处，
        # 于是证据里是一段与金额无关的文字。
        evidence["per_share_amount"] = _context(flat, m.start(), m.end())
        notes.append("金额取自「每股…元」表述，无需换算")
    else:
        # "每 10 股…元"：按 10 股换算，并在原文里保留该表述供复核
        m = re.search(r"每\s*([0-9]+|十|[一二三四五六七八九十]+)\s*股"
                      r"(?:派发|派送|派|送)?(?:现金)?(?:红利|股利|股息)?"
                      r"(?:为|人民币|：|:)?([0-9]+(?:\.[0-9]+)?)元", flat)
        if m:
            per_n = _cn_number(m.group(1))
            if per_n:
                yuan_per_n = Decimal(m.group(2))
                # 先乘后除：5.96/10*1e6 的除法中间结果会产生循环小数，
                # 而 5.96*1e6/10 是精确的整数运算。
                micros = int(yuan_per_n * Decimal(1_000_000) / Decimal(per_n))
                evidence["per_share_amount"] = _context(flat, m.start(), m.end())
                notes.append(f"金额为「每 {per_n} 股 {yuan_per_n} 元」，已按 {per_n} 股换算为每股")

    if micros is not None and micros % 10_000 == 0:
        cents_exact = micros // 10_000

    # ---------------------------------------------------------- 三个日期
    # 表格形态：| 股权登记日 | 最后交易日 | 除权（息）日 | 现金红利发放日 |
    #           | 2026/6/25  |     -      |  2026/6/26   |   2026/6/26     |
    # 取值规则：找到标签，取**该标签之后**窗口里第一个日期。
    #
    # 不能用"标签 + 任意非数字 + 第一个日期"这种正则：跨度里会包含
    # 逗号和后续标签，于是「除权除息日为：2026 年 6 月 12 日」中的
    # "除权除息日"会先吃掉前面「股权登记日为：2026 年 6 月 11 日」的日期，
    # 把除权日解析成登记日——两个日期都合理，账面上完全看不出来。
    label_patterns: dict[str, list[str]] = {
        "record_date": [r"股权登记日", r"登记日"],
        "ex_date": [r"除权除息日", r"除权（息）日", r"除息日"],
        "pay_date": [r"现金红利发放日", r"红利发放日"],
    }

    #: 标签与其日期之间允许的最大间隔字符数。
    #: "股权登记日为：2026年6月11日" 的间隔是 "为：" 两个字符；
    #: 留一点余量以容忍连接词差异，但不能大到跨过另一个日期：
    #: 平安的句子是"股权登记日为6月11日，除权除息日为6月12日"，
    #: 若允许任意跨度，"除权除息日"后面第一个日期会是**登记日**，
    #: 于是除权日被解析成登记日——两个日期都合理，账面上看不出来。
    LABEL_DATE_MAX_GAP = 15

    def first_date_after(labels: list[str], *,
                         window: int = 120) -> tuple[date, str] | None:
        """取**紧贴标签之后**的日期。"""

        best: tuple[date, str, int] | None = None
        for label in labels:
            for m in re.finditer(re.escape(label), flat):
                # 密集段：标签与日期之间只隔连接词与单位
                dense = flat[m.end():m.end() + LABEL_DATE_MAX_GAP]
                dates = _dates_in(dense)
                if not dates:
                    continue
                candidate = (dates[0], dense[:40], m.end())
                if best is None or candidate[2] < best[2]:
                    best = candidate
                break
            # 宽松窗口仅用于兜底：标签与值之间隔着别的列名（表格）
            if best is None:
                m = re.search(re.escape(label), flat)
                if m is not None:
                    tail = flat[m.end():m.end() + window]
                    dates = _dates_in(tail)
                    if dates:
                        best = (dates[0], tail[:40], m.end() + 10_000)
        if best is None:
            return None
        return best[0], best[1]

    found: dict[str, date] = {}

    # 表格形态优先：表头给出列顺序，紧随其后的取值行按**列位置**对应。
    #
    # 表格里日期是挤在一起的（"2026/6/25－2026/6/262026/6/26"），
    # 按"标签后第一个日期"取值会让三列全部落到第一个日期上——
    # 登记日、除权日、发放日都变成同一天，而且看起来完全合理。
    header = re.search(r"股权登记日.{0,10}?最后交易日.{0,10}?除权（?息）?日.{0,10}?现金红利发放日",
                       flat)
    if header:
        tail = flat[header.end():header.end() + 80]
        table_dates = _dates_in(tail)
        if len(table_dates) >= 3:
            found["record_date"], found["ex_date"], found["pay_date"] = table_dates[:3]
            pos = flat.find(tail[:12]) if tail[:12] in flat else header.start()
            evidence["table_dates"] = _slice_sentence(text, pos)
            notes.append("三个日期取自表头「股权登记日/最后交易日/除权（息）日/现金红利发放日」"
                         "之后的取值行，按列位置对应")

    # 非表格形态：按标签取该标签之后窗口里第一个日期
    if len(found) < 3:
        for key, labels in label_patterns.items():
            if key in found:
                continue
            hit = first_date_after(labels)
            if hit is None:
                continue
            found[key], snippet = hit
            evidence[key] = snippet

    # 表格形态兜底：标题行给出列顺序，下一段给出值
    if len(found) < 3:
        header = re.search(r"股权登记日.{0,20}?除权（?息）?日.{0,20}?现金红利发放日", flat)
        if header:
            tail = flat[header.end():header.end() + 80]
            dates = _dates_in(tail)
            if len(dates) >= 3:
                found.setdefault("record_date", dates[0])
                found.setdefault("ex_date", dates[1])
                found.setdefault("pay_date", dates[2])
                evidence.setdefault("table_dates", _slice_sentence(
                    text, text.find(dates[0].isoformat()) if dates[0].isoformat() in text else 0))
                notes.append("三个日期取自表头「股权登记日/除权（息）日/现金红利发放日」后的取值行")

    # 只有"股权登记日"与"除权除息日"而没写发放日的，按同日处理是**错的**：
    # 很多公告确实同日，但那是事实而非规则。缺失就报不完整。
    tax = "PRE_TAX"
    if "（含税）" in flat or "(含税)" in flat:
        notes.append("公告标注「含税」，按税前口径记账（§12.6）")

    record = found.get("record_date")
    ex = found.get("ex_date")
    pay = found.get("pay_date")

    if micros is None or not (record and ex and pay):
        missing = [k for k, v in (("每股金额", micros),
                                  ("股权登记日", record), ("除权除息日", ex),
                                  ("现金红利发放日", pay)) if not v]
        notes.append("未能解析：" + "、".join(missing) + "；需人工复核")
        return DividendParse("INCOMPLETE", f"ca-{announcement_id}", announced_on,
                             record, ex, pay, micros, cents_exact, tax, evidence, notes)

    if not (record <= ex <= pay):
        notes.append(f"日期顺序异常：登记日 {record}、除权日 {ex}、发放日 {pay}")
        return DividendParse("INCOMPLETE", f"ca-{announcement_id}", announced_on,
                             record, ex, pay, micros, cents_exact, tax, evidence, notes)

    return DividendParse("OK", f"ca-{announcement_id}", announced_on,
                         record, ex, pay, micros, cents_exact, tax, evidence, notes)


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
               content_type: str | None = None,
               max_bytes: int | None = None) -> FetchOutcome:
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
                    check_content_length(resp.headers.get("Content-Length"), self.policy,
                                         max_bytes=max_bytes)
                    body = read_bounded(resp, self.policy, max_bytes=max_bytes)
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

    def report_index(self, *, stock_code: str, report_period: str,
                     page_size: int = 30, max_pages: int = 5) -> ReportIndexResult:
        """归档证券/报告期的官方索引，直到有证据表明分页已耗尽。

        官方 ``topSearch`` 先确定 orgId；其请求 URL 和响应也归档。每页
        ``hisAnnouncement`` 的 POST body 作为独立 raw artifact 留存，响应收据
        ``detail`` 与页结果绑定该请求哈希。只把严格匹配证券和报告期的公告
        绑定到其发现页。标题中的“修订”仍仅是标题证据，不构成版本替代关系。

        ``complete=False`` 时保留页收据供诊断，但 ``announcements`` 和
        ``matches`` 均为空，避免截断索引被当成完整版本链。
        """

        if not 1 <= page_size <= 100 or not 1 <= max_pages <= 100:
            raise ValueError("page_size and max_pages must be between 1 and 100")
        code = normalize_stock_code(stock_code)
        market = market_for_code(code)
        period = parse_report_period(report_period)
        window = period.publication_window
        pages: list[ReportIndexPage] = []
        matches: dict[str, ReportIndexMatch] = {}
        skipped: dict[str, int] = {}
        organization: str | None = None
        lookup_receipt: str | None = None
        lookup_hash: str | None = None
        total: int | None = None
        raw_seen = 0

        def finish(termination: str, error: str | None = None) -> ReportIndexResult:
            complete = termination in {"has_more_false", "total_reached"}
            return ReportIndexResult(
                stock_code=code, organization_id=organization,
                report_period=period.end_date.isoformat(), market=market,
                category=period.category, publication_window=window,
                org_lookup_receipt_id=lookup_receipt,
                org_lookup_content_hash=lookup_hash,
                pages=tuple(pages),
                matches=(tuple(sorted(matches.values(),
                                      key=lambda hit: (hit.announcement.announcement_time_ms,
                                                       hit.announcement.announcement_id),
                                      reverse=True)) if complete else ()),
                skipped=dict(sorted(skipped.items())),
                total_announcement=total, complete=complete,
                termination=termination, error=error,
            )

        lookup_url = ORG_LOOKUP_URL + "?" + urllib.parse.urlencode(
            {"keyWord": code, "maxNum": 10})
        lookup = self._fetch(lookup_url, label=f"report-index:org:{code}", data=b"")
        lookup_receipt, lookup_hash = lookup.receipt_id, lookup.content_hash
        if not lookup.ok or lookup.payload is None:
            return finish("org_lookup_failed", lookup.detail or "organization lookup failed")
        try:
            organizations = json.loads(lookup.payload)
            if not isinstance(organizations, list):
                raise ValueError("organization lookup is not a list")
            candidates = {str(row.get("orgId") or "").strip()
                          for row in organizations if isinstance(row, dict)
                          and str(row.get("code") or "").strip() == code
                          and str(row.get("orgId") or "").strip()}
            if len(candidates) != 1:
                raise ValueError("organization ID missing or ambiguous")
            organization = candidates.pop()
            # CNINFO also issues numeric orgIds (for example 000333); the
            # lookup is authoritative and the security code above is exact.
            if not re.fullmatch(r"(?:gss[hz]\d{7}|\d{10})", organization):
                raise ValueError("organization ID has an unsupported format")
        except (ValueError, TypeError) as exc:
            return finish("org_lookup_invalid", str(exc))

        for page_num in range(1, max_pages + 1):
            body = urllib.parse.urlencode({
                "pageNum": page_num, "pageSize": page_size, "column": market,
                "tabName": "fulltext", "plate": "",
                "stock": f"{code},{organization}", "searchkey": "",
                "secid": "", "category": period.category, "trade": "",
                "seDate": f"{window[0].isoformat()}~{window[1].isoformat()}",
                "sortName": "", "sortType": "", "isHLtitle": "true",
            }).encode("utf-8")
            body_hash, _ = self.archive.store_bytes(
                body, media_type="application/x-www-form-urlencoded")
            fetched = self._fetch(
                REPORT_QUERY_URL,
                label=(f"report-index:{code}:{period.end_date}:page={page_num}:"
                       f"request={body_hash}"),
                data=body,
                content_type="application/x-www-form-urlencoded; charset=UTF-8",
            )
            if not fetched.ok or fetched.payload is None or fetched.content_hash is None:
                pages.append(ReportIndexPage(page_num, body_hash, fetched.receipt_id,
                                             fetched.content_hash, fetched.http_status,
                                             None, None, None))
                return finish("page_fetch_failed", fetched.detail or "page fetch failed")
            try:
                found, page_skipped, page_total, has_more = parse_listing_payload(
                    fetched.payload, stock_code=code, period=period)
                document = json.loads(fetched.payload)
                raw_count = len(document.get("announcements") or [])
            except (CninfoProbeError, ValueError, TypeError, OverflowError) as exc:
                pages.append(ReportIndexPage(page_num, body_hash, fetched.receipt_id,
                                             fetched.content_hash, fetched.http_status,
                                             None, None, None))
                return finish("page_parse_failed", str(exc))
            pages.append(ReportIndexPage(page_num, body_hash, fetched.receipt_id,
                                         fetched.content_hash, fetched.http_status,
                                         raw_count, page_total, has_more))
            raw_seen += raw_count
            for reason, count in page_skipped.items():
                skipped[reason] = skipped.get(reason, 0) + count
            if len(found) + sum(page_skipped.values()) != raw_count:
                return finish("identity_incomplete", "announcement identity repeated within page")
            if any(skipped.get(reason, 0) for reason in (
                    "non_object_announcement", "invalid_security_code",
                    "missing_announcement_id", "missing_announcement_time")):
                return finish("identity_incomplete", "matching announcement identity is incomplete")
            if any(not ann.url for ann in found):
                return finish("identity_incomplete", "matching announcement has no official document URL")
            if page_total is not None:
                if total is not None and page_total != total:
                    return finish("pagination_inconsistent", "totalAnnouncement changed between pages")
                total = page_total
            for ann in found:
                if ann.announcement_id in matches:
                    return finish("pagination_inconsistent", "announcement repeated across pages")
                matches[ann.announcement_id] = ReportIndexMatch(
                    announcement=ann, page_num=page_num,
                    page_receipt_id=fetched.receipt_id,
                    page_content_hash=fetched.content_hash,
                )
            if total is not None and raw_seen > total:
                return finish("pagination_inconsistent", "raw page rows exceed totalAnnouncement")
            if has_more is False:
                if total is not None and raw_seen < total:
                    return finish("pagination_inconsistent", "hasMore=false before totalAnnouncement")
                return finish("has_more_false")
            if has_more is True and total is not None and raw_seen >= total:
                return finish("pagination_inconsistent", "hasMore=true after totalAnnouncement")
            if has_more is None and total is not None and raw_seen == total:
                return finish("total_reached")
            if has_more is None and total is None:
                return finish("pagination_unknown", "neither hasMore nor totalAnnouncement is available")
        return finish("max_pages_truncated", "pagination did not exhaust within max_pages")

    def cross_category_index(self, *, stock_code: str, report_period: str,
                             through: date, page_size: int = 30,
                             max_pages: int = 100) -> CrossCategoryIndexResult:
        """完整翻页查询同证券在显式截止日前的**全分类标题索引**。

        不使用 ``searchkey``，因为“更正公告”标题可能没有报告期文字，
        且定期报告分类不包含该公告。返回所有标题及受限筛查候选；索引完整
        仅表示该请求窗口内页数耗尽，不证明任何公告替代了哪份报告，也不
        证明窗口之外不存在更正。PDF 正文及版本关系仍需独立核验。
        """

        if not isinstance(through, date) or isinstance(through, datetime):
            raise ValueError("through must be an explicit calendar date")
        if not 1 <= page_size <= 100 or not 1 <= max_pages <= 100:
            raise ValueError("page_size and max_pages must be between 1 and 100")
        code = normalize_stock_code(stock_code)
        market = market_for_code(code)
        period = parse_report_period(report_period)
        if through < period.end_date:
            raise ValueError("through precedes the report period end")
        window = (period.end_date, through)
        pages: list[ReportIndexPage] = []
        entries: dict[str, CrossCategoryIndexEntry] = {}
        organization: str | None = None
        lookup_receipt: str | None = None
        lookup_hash: str | None = None
        total: int | None = None
        raw_seen = 0
        skipped_other = 0

        def finish(termination: str, error: str | None = None) -> CrossCategoryIndexResult:
            complete = termination in {"has_more_false", "total_reached"}
            return CrossCategoryIndexResult(
                stock_code=code, organization_id=organization,
                report_period=period.end_date.isoformat(), publication_window=window,
                org_lookup_receipt_id=lookup_receipt,
                org_lookup_content_hash=lookup_hash,
                pages=tuple(pages),
                announcements=(tuple(sorted(
                    entries.values(),
                    key=lambda entry: (entry.announcement_time_ms, entry.announcement_id),
                    reverse=True)) if complete else ()),
                skipped_different_security=skipped_other,
                total_announcement=total, complete=complete,
                termination=termination, error=error,
            )

        lookup_url = ORG_LOOKUP_URL + "?" + urllib.parse.urlencode(
            {"keyWord": code, "maxNum": 10})
        lookup = self._fetch(lookup_url, label=f"cross-category:org:{code}", data=b"")
        lookup_receipt, lookup_hash = lookup.receipt_id, lookup.content_hash
        if not lookup.ok or lookup.payload is None:
            return finish("org_lookup_failed", lookup.detail or "organization lookup failed")
        try:
            organizations = json.loads(lookup.payload)
            if not isinstance(organizations, list):
                raise ValueError("organization lookup is not a list")
            candidates = {str(row.get("orgId") or "").strip()
                          for row in organizations if isinstance(row, dict)
                          and str(row.get("code") or "").strip() == code
                          and str(row.get("orgId") or "").strip()}
            if len(candidates) != 1:
                raise ValueError("organization ID missing or ambiguous")
            organization = candidates.pop()
            if not re.fullmatch(r"(?:gss[hz]\d{7}|\d{10})", organization):
                raise ValueError("organization ID has an unsupported format")
        except (ValueError, TypeError) as exc:
            return finish("org_lookup_invalid", str(exc))

        for page_num in range(1, max_pages + 1):
            body = urllib.parse.urlencode({
                "pageNum": page_num, "pageSize": page_size, "column": market,
                "tabName": "fulltext", "plate": "",
                "stock": f"{code},{organization}", "searchkey": "",
                "secid": "", "category": "", "trade": "",
                "seDate": f"{window[0].isoformat()}~{window[1].isoformat()}",
                "sortName": "", "sortType": "", "isHLtitle": "true",
            }).encode("utf-8")
            body_hash, _ = self.archive.store_bytes(
                body, media_type="application/x-www-form-urlencoded")
            fetched = self._fetch(
                REPORT_QUERY_URL,
                label=(f"cross-category:{code}:{period.end_date}~{through}:"
                       f"page={page_num}:request={body_hash}"),
                data=body,
                content_type="application/x-www-form-urlencoded; charset=UTF-8",
            )
            if not fetched.ok or fetched.payload is None or fetched.content_hash is None:
                pages.append(ReportIndexPage(page_num, body_hash, fetched.receipt_id,
                                             fetched.content_hash, fetched.http_status,
                                             None, None, None))
                return finish("page_fetch_failed", fetched.detail or "page fetch failed")
            try:
                document = json.loads(fetched.payload)
                if not isinstance(document, dict):
                    raise ValueError("index response is not an object")
                rows = document.get("announcements")
                if rows is None:
                    rows = []
                if not isinstance(rows, list):
                    raise ValueError("announcements is not a list")
                raw_total = document.get("totalAnnouncement")
                if raw_total is None:
                    page_total = None
                else:
                    page_total = int(raw_total)
                    if isinstance(raw_total, bool) or page_total < 0:
                        raise ValueError("invalid totalAnnouncement")
                raw_more = document.get("hasMore")
                if isinstance(raw_more, bool):
                    has_more = raw_more
                elif isinstance(raw_more, str) and raw_more.strip().lower() in {
                        "true", "false"}:
                    has_more = raw_more.strip().lower() == "true"
                elif isinstance(raw_more, int) and raw_more in (0, 1):
                    has_more = bool(raw_more)
                elif raw_more is None:
                    has_more = None
                else:
                    raise ValueError("invalid hasMore")
            except (ValueError, TypeError, OverflowError) as exc:
                pages.append(ReportIndexPage(page_num, body_hash, fetched.receipt_id,
                                             fetched.content_hash, fetched.http_status,
                                             None, None, None))
                return finish("page_parse_failed", str(exc))
            pages.append(ReportIndexPage(page_num, body_hash, fetched.receipt_id,
                                         fetched.content_hash, fetched.http_status,
                                         len(rows), page_total, has_more))
            raw_seen += len(rows)
            if page_total is not None:
                if total is not None and total != page_total:
                    return finish("pagination_inconsistent", "totalAnnouncement changed between pages")
                total = page_total

            for row in rows:
                if not isinstance(row, dict):
                    return finish("identity_incomplete", "announcement is not an object")
                row_code = str(row.get("secCode") or "").strip()
                if not re.fullmatch(r"\d{6}", row_code):
                    return finish("identity_incomplete", "announcement security code is missing")
                if row_code != code:
                    skipped_other += 1
                    continue
                announcement_id = str(row.get("announcementId") or "").strip()
                title = normalize_title(row.get("announcementTitle"))
                if not announcement_id or not title:
                    return finish("identity_incomplete", "announcement ID or title is missing")
                try:
                    millis = int(row.get("announcementTime"))
                    if isinstance(row.get("announcementTime"), bool) or millis <= 0:
                        raise ValueError("invalid announcement time")
                    announced_on = datetime.fromtimestamp(
                        millis / 1000, tz=timezone.utc).astimezone(CST).date()
                except (ValueError, TypeError, OverflowError) as exc:
                    return finish("identity_incomplete", f"invalid announcement time: {exc}")
                if not window[0] <= announced_on <= window[1]:
                    return finish("window_inconsistent", "announcement date outside query window")
                path = str(row.get("adjunctUrl") or "").strip()
                parsed = urlsplit(path)
                if not path or parsed.scheme or parsed.netloc:
                    return finish("identity_incomplete", "missing or non-relative document path")
                document_url = f"https://{STATIC_HOST}/{path.lstrip('/')}"
                is_correction, is_revision, is_supplement, _ = title_revision_flags(title)
                reasons = tuple(reason for reason, active in (
                    ("period_title", title_matches_period(title, period)),
                    ("correction_title", is_correction),
                    ("revision_title", is_revision),
                    ("supplement_title", is_supplement),
                    ("accounting_restatement_title", any(term in title for term in (
                        "会计差错", "追溯调整", "重述"))),
                ) if active)
                if announcement_id in entries:
                    return finish("pagination_inconsistent", "announcement repeated across pages")
                entries[announcement_id] = CrossCategoryIndexEntry(
                    announcement_id=announcement_id, sec_code=code, title=title,
                    announcement_time_ms=millis, document_url=document_url,
                    candidate_reasons=reasons, page_num=page_num,
                    page_receipt_id=fetched.receipt_id,
                    page_content_hash=fetched.content_hash,
                )
            if total is not None and raw_seen > total:
                return finish("pagination_inconsistent", "raw page rows exceed totalAnnouncement")
            if has_more is False:
                if total is not None and raw_seen < total:
                    return finish("pagination_inconsistent", "hasMore=false before totalAnnouncement")
                return finish("has_more_false")
            if has_more is True and total is not None and raw_seen >= total:
                return finish("pagination_inconsistent", "hasMore=true after totalAnnouncement")
            if has_more is None and total is not None and raw_seen == total:
                return finish("total_reached")
            if has_more is None and total is None:
                return finish("pagination_unknown", "neither hasMore nor totalAnnouncement is available")
        return finish("max_pages_truncated", "all-category pagination did not exhaust")

    def document(self, url: str, *, label: str = "",
                 max_bytes: int | None = None) -> FetchOutcome:
        """抓取公告原文（通常是 PDF）并归档。引用核验必须针对原文，而不是标题。

        公告 PDF 天然大于 JSON 列表（年报、募集说明书可达数十 MiB），
        因此原文字节上限单独放宽——但**仍然有上限**，不使用无界读取（§17.3）。
        实测 18.8 MiB 的公告会被默认 16 MiB 拒绝，这里默认给到 64 MiB。
        """

        limit = DOCUMENT_MAX_BYTES if max_bytes is None else max_bytes
        return self._fetch(url, label=label or f"document:{url.rsplit('/', 1)[-1]}",
                           max_bytes=limit)
