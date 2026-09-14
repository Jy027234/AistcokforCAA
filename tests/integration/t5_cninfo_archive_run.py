"""T5 官方渠道实跑：从巨潮抓取公告并生成带时点语义的证据记录。

用法（必须在仓库根目录以模块方式运行，脚本会导入 tests.integration）：
    $env:AQUANT_TRUSTED_PROXY_NETWORKS='198.18.0.0/15,fdfe:dcba:9876::/48'
    python -m tests.integration.t5_cninfo_archive_run

环境变量：
    T5_LOOKBACK_DAYS  公告窗口相对今天回看的天数，默认 40
    T5_COLUMN         szse（深市）或 sse（沪市），默认 szse
    T5_LIMIT          取样公告条数，默认 5

日历来自多源降级（先腾讯、后东财），不再押在单一免费源上——
实测东财会按出口 IP 长时段封锁，单源依赖会让本脚本时好时坏。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from aquant.adapters.providers.calendar import load_trading_calendar          # noqa: E402
from aquant.adapters.providers.cninfo import CninfoClient, to_pit_record    # noqa: E402
from aquant.adapters.providers.resilience import (                           # noqa: E402
    CircuitBreaker, RateLimiter, RetryPolicy,
)
from aquant.domain.data.forward_archive import ForwardArchive                # noqa: E402

ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"


def main() -> int:
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ARCHIVE_ROOT / "meta.sqlite", isolation_level=None)
    con.row_factory = sqlite3.Row
    archive = ForwardArchive(con, ARCHIVE_ROOT)

    # 公告客户端与日历客户端共用同一套守卫/限速/熔断，但互不影响熔断状态
    def make(kind):
        return kind(
            archive,
            limiter=RateLimiter(min_interval_seconds=3.0),
            breaker=CircuitBreaker(failure_threshold=3, cooldown_seconds=120.0),
            retry=RetryPolicy(max_attempts=2, base_delay_seconds=3.0, max_delay_seconds=6.0),
            timeout=25.0,
        )

    from aquant.adapters.providers.eastmoney import EastmoneyClient
    from aquant.adapters.providers.tencent import TencentClient

    today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).date()
    lookback = int(os.environ.get("T5_LOOKBACK_DAYS", "40"))
    column = os.environ.get("T5_COLUMN", "szse")
    limit = int(os.environ.get("T5_LIMIT", "5"))
    begin = today - timedelta(days=lookback)
    end = today - timedelta(days=max(lookback - 6, 0))

    print(f"== 交易日历（多源降级）==")
    try:
        cal = load_trading_calendar(
            archive,
            begin=today - timedelta(days=400), end=today + timedelta(days=20),
            tencent=make(TencentClient), eastmoney=make(EastmoneyClient),
            # 公告的可用时点是"次一交易日盘前"，因此日历必须能证明窗口之后的交易日
            require_after=end,
        )
    except RuntimeError as exc:
        print(f"   FAIL: {str(exc)[:200]}")
        con.close()
        return 1
    print(f"   来源={cal.source_id} 指数={cal.symbol} 交易日={len(cal.trading_days)} "
          f"({cal.trading_days[0]} ~ {cal.trading_days[-1]})")
    calendar = cal.trading_days

    client = make(CninfoClient)
    print(f"\n== 抓取巨潮公告 {begin} ~ {end}（column={column}）==")
    out, anns = client.announcements(begin=begin, end=end, column=column, page_size=10)
    print(f"   ok={out.ok} status={out.http_status} detail={out.detail}")
    print(f"   公告数={len(anns)}")

    first_seen = datetime.now(timezone.utc)
    records = []
    for a in anns[:limit]:
        try:
            rec = to_pit_record(a, trading_calendar=calendar, first_seen_at=first_seen)
            rec["pit_status"] = "READY"
        except ValueError as exc:
            # 指数日线无法证明未来交易日。原文照常归档，可用时点明确推迟，
            # 而不是编造一份工作日日历。
            rec = {
                "source_id": "cninfo", "announcement_id": a.announcement_id,
                "instrument_code": a.sec_code, "title": a.title,
                "source_published_date": a.announced_on.isoformat(),
                "first_seen_at": first_seen.isoformat(), "available_at": None,
                "pit_status": "CALENDAR_NOT_READY", "pit_error": str(exc),
            }
        document_url = a.detail_url()
        document = (client.document(document_url, label=f"announcement:{a.announcement_id}")
                    if document_url else None)
        rec["document_receipt_id"] = document.receipt_id if document else None
        rec["document_content_hash"] = document.content_hash if document else None
        rec["document_bytes"] = (len(document.payload)
                                 if document and document.payload else None)
        rec["document_archived"] = bool(document and document.ok and document.content_hash)
        rec["document_hash_verified"] = bool(
            document and document.content_hash and archive.verify(document.content_hash))
        records.append(rec)
        print(f"   - {rec['source_published_date']}  {rec['title'][:38]}")
        print(f"       available_at={rec.get('available_at')}  "
              f"basis={rec.get('available_basis', rec['pit_status'])}  "
              f"pdf={rec['document_archived']}/{rec['document_hash_verified']}")

    ready = sum(1 for r in records if r.get("pit_status") == "READY")
    archived = sum(1 for r in records if r["document_archived"])
    verified = sum(1 for r in records if r["document_hash_verified"])

    out_doc = {
        "ran_at": first_seen.isoformat(),
        "window": [begin.isoformat(), end.isoformat()],
        "fetch_ok": out.ok, "http_status": out.http_status,
        "detail": out.detail, "receipt_id": out.receipt_id,
        "content_hash": out.content_hash,
        "announcement_count": len(anns),
        "records": records,
        "calendar": cal.summary(),
        "pit_ready_count": ready,
        "pit_deferred_count": len(records) - ready,
        "archived_document_count": archived,
        "verified_document_count": verified,
    }
    path = ARCHIVE_ROOT / "t5-cninfo-run.json"
    # 显式 LF：Windows 的文本模式会把 \n 翻成 \r\n，
    # 那样同一份证据在两种平台上的字节不同，git diff --check 也会
    # 把每一行都当成行尾空白。用 write_bytes 固定下来。
    path.write_bytes(
        json.dumps(out_doc, ensure_ascii=False, indent=2).encode("utf-8")
    )
    print(f"\nwrote {path}")

    receipts = archive.receipts_for("cninfo")
    print(f"cninfo 归档 receipts={len(receipts)} "
          f"distinct_content={archive.distinct_content_count('cninfo')}")
    if out.content_hash:
        print("列表响应哈希校验:", archive.verify(out.content_hash))
    print(f"PIT 就绪 {ready}/{len(records)}；PDF 归档 {archived}，哈希可验证 {verified}")

    # 成功判据：列表抓到、每条原文都归档、归档哈希可复算。
    # PIT 是否为 READY 取决于日历能否证明，不作为成功与否的条件。
    success = out.ok and bool(records) and archived == len(records) and verified == len(records)
    con.close()
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
