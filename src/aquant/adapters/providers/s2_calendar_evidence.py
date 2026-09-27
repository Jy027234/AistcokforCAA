"""S2 pilot calendar: observed index history plus evidence-backed 2026 plans.

Official rules and all seven holiday intervals support planned weekdays; these
are never represented as observed trading. Unsupported years fail closed.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html import unescape
from urllib.parse import parse_qs, urlsplit

from aquant.adapters.providers.calendar import INDEX_SYMBOLS, load_trading_calendar
from aquant.adapters.providers.eastmoney import (
    UA, _ValidatingRedirectHandler, trusted_proxy_networks_from_env,
)
from aquant.adapters.providers.fetch_guard import (
    FetchDenied, FetchPolicy, assert_url_allowed, check_content_length, read_bounded,
)
from aquant.domain.data.forward_archive import ForwardArchive
from aquant.domain.data.pit import preopen_instant

SCHEMA = "s2-observed-plus-sse-2026-plan-v1"
SSE_URL = "https://www.sse.com.cn/disclosure/announcement/general/c/c_20260915_10832273.shtml"
RULES_URL = "https://one.sse.com.cn/onething/gptz/"
ANNUAL_URL = "https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml"
WEEKDAY_RULE = "本所交易日为每周一至周五。国家法定假日和本所公告的休市日，本所市场休市。"
HOLIDAYS = (
    ("元旦", "2026-01-01", "2026-01-03", "2026-01-05"),
    ("春节", "2026-02-15", "2026-02-23", "2026-02-24"),
    ("清明节", "2026-04-04", "2026-04-06", "2026-04-07"),
    ("劳动节", "2026-05-01", "2026-05-05", "2026-05-06"),
    ("端午节", "2026-06-19", "2026-06-21", "2026-06-22"),
    ("中秋节", "2026-09-25", "2026-09-27", "2026-09-28"),
    ("国庆节", "2026-10-01", "2026-10-07", "2026-10-08"),
)
REOPEN = date(2026, 9, 28)
CLOSED_FROM = date(2026, 9, 25)
CLOSED_THROUGH = date(2026, 9, 27)
PUBLISHED = date(2026, 9, 17)
BEIJING = timezone(timedelta(hours=8))


def _reference(archive: ForwardArchive, receipt_id: str, now: datetime) -> tuple[dict, bytes]:
    row = archive.con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?", (receipt_id,)).fetchone()
    if row is None:
        raise ValueError("calendar receipt missing")
    row = dict(row)
    observed = datetime.fromisoformat(row["first_seen_at"])
    if (row["outcome"] != "OK" or row["http_status"] != 200 or
            observed.tzinfo is None or observed > now or
            not row["content_hash"] or not archive.verify(row["content_hash"])):
        raise ValueError("calendar receipt/hash/observation is invalid")
    ref = {key: row[key] for key in ("receipt_id", "source_id", "url", "content_hash", "first_seen_at")}
    return ref, archive.load_bytes(row["content_hash"])


def _reopening(archive: ForwardArchive, receipt_id: str, now: datetime) -> dict:
    ref, body = _reference(archive, receipt_id, now)
    if ref["source_id"] != "sse-site" or ref["url"] != SSE_URL:
        raise ValueError("calendar requires the exact approved SSE reopening notice")
    text = re.sub(r"\s+", "", unescape(re.sub(r"<[^>]*>", "", body.decode("utf-8"))))
    required = (
        "关于2026年中秋节、国庆节休市安排的公告", "上证公告〔2026〕22号",
        "9月25日（星期五）至9月27日（星期日）休市，9月28日（星期一）起照常开市。",
        "上海证券交易所", "2026年9月17日",
    )
    if any(part not in text for part in required):
        raise ValueError("SSE notice year/closure/reopening does not match the bounded policy")
    observed_day = datetime.fromisoformat(ref["first_seen_at"]).astimezone(BEIJING).date()
    if observed_day < PUBLISHED or observed_day != now.astimezone(BEIJING).date():
        raise ValueError("SSE notice must be observed on today's Beijing date")
    return {**ref, "published_on": PUBLISHED.isoformat(), "closed_from": CLOSED_FROM.isoformat(),
            "closed_through": CLOSED_THROUGH.isoformat(), "scheduled_open": REOPEN.isoformat(),
            "basis": "official_scheduled_reopening_not_observed_trading"}


def _official_text(archive: ForwardArchive, receipt_id: str, url: str, now: datetime) -> tuple[dict, str]:
    ref, body = _reference(archive, receipt_id, now)
    if (ref["source_id"] != "sse-site" or ref["url"] != url or
            datetime.fromisoformat(ref["first_seen_at"]).astimezone(BEIJING).date() != now.astimezone(BEIJING).date()):
        raise ValueError("official calendar source URL or observation date differs")
    return ref, re.sub(r"\s+", "", unescape(re.sub(r"<[^>]*>", "", body.decode("utf-8"))))


def _scheduled(archive: ForwardArchive, refs: dict, now: datetime) -> tuple[dict, tuple[date, ...]]:
    reopening = _reopening(archive, refs["receipt_id"], now)
    rules, rule_text = _official_text(archive, refs["rules"]["receipt_id"], RULES_URL, now)
    annual, annual_text = _official_text(archive, refs["annual"]["receipt_id"], ANNUAL_URL, now)
    if WEEKDAY_RULE not in rule_text:
        raise ValueError("official weekday rule including closure exceptions is absent")
    if ("关于上海证券交易所2026年部分节假日休市安排的通知" not in annual_text or
            "上证公告〔2025〕45号" not in annual_text):
        raise ValueError("official annual calendar year/identity differs")
    clean = re.sub(r"（星期[一二三四五六日天]）", "", annual_text)
    closed = set()
    for name, start, end, resume in HOLIDAYS:
        begin, finish, reopened = (date.fromisoformat(item) for item in (start, end, resume))
        clause = (f"{name}：{begin.month}月{begin.day}日至{finish.month}月{finish.day}日休市，"
                  f"{reopened.month}月{reopened.day}日起照常开市。")
        if clause not in clean:
            raise ValueError("official annual holiday schedule is incomplete or changed")
        closed.update(begin + timedelta(days=i) for i in range((finish - begin).days + 1))
    days = tuple(day for i in range(365) if (day := date(2026, 1, 1) + timedelta(days=i)).weekday() < 5 and day not in closed)
    return {**reopening, "rules": rules, "annual": annual,
            "basis": "official_weekday_rule_minus_announced_holidays_planned_not_observed",
            "covered_through": "2026-12-31",
            "temporary_closure_monitoring": "not_proven_by_these_documents; require_actual_decision_snapshot"}, days


def _historical(archive: ForwardArchive, receipt_id: str, now: datetime) -> tuple[dict, tuple[date, ...]]:
    ref, body = _reference(archive, receipt_id, now)
    parts = urlsplit(ref["url"])
    query = parse_qs(parts.query, keep_blank_values=True)
    doc = json.loads(body)
    if parts.scheme != "https" or parts.port not in (None, 443) or parts.username or parts.fragment:
        raise ValueError("invalid historical index URL")
    if ref["source_id"] == "tencent-ifzq":
        if parts.hostname != "web.ifzq.gtimg.cn" or parts.path != "/appstock/app/fqkline/get" or set(query) != {"param"}:
            raise ValueError("historical calendar is not an approved index query")
        params = query["param"][0].split(",")
        if len(query["param"]) != 1 or len(params) != 6 or params[0] not in INDEX_SYMBOLS or params[1] != "day" or params[4:] != ["640", ""]:
            raise ValueError("historical calendar requires unadjusted index bars")
        begin, end = date.fromisoformat(params[2]), date.fromisoformat(params[3])
        rows = doc["data"][params[0]]["day"]
    elif ref["source_id"] == "eastmoney-direct":
        if parts.hostname != "push2his.eastmoney.com" or parts.path != "/api/qt/stock/kline/get":
            raise ValueError("historical calendar is not an approved index query")
        if any(len(values) != 1 for values in query.values()) or query.get("secid") not in (["1.000001"], ["0.399001"]) or query.get("klt") != ["101"] or query.get("fqt") != ["0"]:
            raise ValueError("historical calendar requires unadjusted index bars")
        begin, end = (datetime.strptime(query[key][0], "%Y%m%d").date() for key in ("beg", "end"))
        expected_code = query["secid"][0].split(".")[1]
        if doc["data"].get("code") != expected_code:
            raise ValueError("historical index response identity differs")
        rows = [row.split(",") for row in doc["data"]["klines"]]
    else:
        raise ValueError("unsupported observed-calendar source")
    if not isinstance(rows, list) or not rows or any(not isinstance(row, list) or len(row) < 6 for row in rows):
        raise ValueError("historical index rows missing")
    days = tuple(date.fromisoformat(row[0]) for row in rows)
    observed_day = datetime.fromisoformat(ref["first_seen_at"]).astimezone(BEIJING).date()
    if (days != tuple(sorted(set(days))) or begin > end or
            any(day < begin or day > end or day > observed_day or day > now.astimezone(BEIJING).date() for day in days) or
            any(CLOSED_FROM <= day <= CLOSED_THROUGH for day in days)):
        raise ValueError("historical index dates are unordered, future, or contradictory")
    return {**ref, "query_begin": begin.isoformat(), "query_end": end.isoformat(),
            "trading_days": [day.isoformat() for day in days]}, days


def verify_calendar_evidence(packet: dict, archive: ForwardArchive, *, now: datetime,
                             published_on: date | None = None) -> tuple[tuple[date, ...], dict]:
    if not isinstance(packet, dict) or packet.get("schema") != SCHEMA:
        raise ValueError("calendar requires archived evidence; bare date arrays are not accepted")
    if now.tzinfo is None or not PUBLISHED <= now.astimezone(BEIJING).date() <= date(2026, 12, 31):
        raise ValueError("outside the supported official calendar year/window")
    reopening, planned = _scheduled(archive, packet["scheduled"], now)
    historical, days = _historical(archive, packet["observed"]["receipt_id"], now)
    rebuilt = {"schema": SCHEMA, "observed": historical, "scheduled": reopening}
    if packet != rebuilt:
        raise ValueError("calendar evidence differs from archived source bytes")
    if any(day.year == 2026 and day not in planned for day in days):
        raise ValueError("observed index contradicts the official planned calendar")
    today = now.astimezone(BEIJING).date()
    if days[-1] < max(day for day in planned if day < today):
        raise ValueError("observed history must reach the last completed planned trading date")
    if published_on is not None and (days[0] > published_on or not any(day > published_on for day in days)):
        raise ValueError("observed history does not cover the publication-date floor")
    upcoming = tuple(day for day in planned if preopen_instant(day) > now)
    if not upcoming:
        raise ValueError("official calendar exhausted; no next snapshot is proven")
    return tuple(sorted(set(days) | set(upcoming))), rebuilt


def capture_reopening(archive: ForwardArchive, *, url: str = SSE_URL,
                      policy: FetchPolicy | None = None) -> str:
    """One bounded GET, archived on success and failure; no automatic retries."""
    if url not in (SSE_URL, RULES_URL, ANNUAL_URL):
        raise ValueError("unsupported official calendar URL")
    policy = policy or FetchPolicy(allowed_hosts=frozenset({"www.sse.com.cn", "one.sse.com.cn"}),
        allowed_schemes=frozenset({"https"}), max_redirects=0, max_response_bytes=2 * 1024 * 1024,
        trusted_proxy_networks=trusted_proxy_networks_from_env())
    started = datetime.now(timezone.utc)
    try:
        assert_url_allowed(url, policy)
        opener = urllib.request.build_opener(_ValidatingRedirectHandler(policy))
        with opener.open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20) as response:
            if response.geturl() != url or response.status != 200:
                raise ValueError("SSE response URL/status changed")
            check_content_length(response.headers.get("Content-Length"), policy)
            body = read_bounded(response, policy)
        digest, _ = archive.store_bytes(body, media_type="text/html")
        receipt = archive.record(source_id="sse-site", url=url, outcome="OK",
            requested_at=started, http_status=200, content_hash=digest, byte_size=len(body))
        return receipt.receipt_id
    except (FetchDenied, OSError, ValueError) as exc:
        archive.record(source_id="sse-site", url=url,
            outcome="DENIED" if isinstance(exc, FetchDenied) else "TRANSPORT_ERROR",
            requested_at=started, detail=str(exc))
        raise ValueError(f"official calendar capture failed: {exc}") from exc


def capture_calendar_evidence(archive: ForwardArchive) -> dict:
    """Capture existing historical source chain and the exact reopening notice."""
    now = datetime.now(timezone.utc)
    if not PUBLISHED <= now.astimezone(BEIJING).date() <= date(2026, 12, 31):
        raise ValueError("outside the supported official calendar year/window")
    old_ids = {row[0] for row in archive.con.execute("SELECT receipt_id FROM fetch_receipt")}
    history = load_trading_calendar(archive, begin=date(2024, 7, 1),
                                    end=now.astimezone(BEIJING).date() - timedelta(days=1))
    latest = [row for row in archive.receipts_for(history.source_id) if row["receipt_id"] not in old_ids and row["outcome"] == "OK"]
    if not latest:
        raise ValueError("historical calendar did not archive its source")
    scheduled_id = capture_reopening(archive)
    rules_id = capture_reopening(archive, url=RULES_URL)
    annual_id = capture_reopening(archive, url=ANNUAL_URL)
    now = datetime.now(timezone.utc)
    observed, days = _historical(archive, latest[-1]["receipt_id"], now)
    if list(days) != history.trading_days:
        raise ValueError("historical calendar disagrees with its source receipt")
    scheduled, _ = _scheduled(archive, {"receipt_id": scheduled_id,
        "rules": {"receipt_id": rules_id}, "annual": {"receipt_id": annual_id}}, now)
    packet = {"schema": SCHEMA, "observed": observed, "scheduled": scheduled}
    verify_calendar_evidence(packet, archive, now=now)
    return packet
