"""T5 官方渠道实跑：从巨潮抓取公告并生成带时点语义的证据记录。

用法：
    $env:AQUANT_TRUSTED_PROXY_NETWORKS='198.18.0.0/15,fdfe:dcba:9876::/48'
    python tests/integration/t5_cninfo_archive_run.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.cninfo import CninfoClient, to_pit_record   # noqa: E402
from aquant.adapters.providers.resilience import (                          # noqa: E402
    CircuitBreaker, RateLimiter, RetryPolicy,
)
from aquant.domain.data.forward_archive import ForwardArchive               # noqa: E402
from tests.integration.t4_free_source_probe import probe_calendar            # noqa: E402

ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"


def main() -> int:
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ARCHIVE_ROOT / "meta.sqlite", isolation_level=None)
    con.row_factory = sqlite3.Row
    archive = ForwardArchive(con, ARCHIVE_ROOT)

    client = CninfoClient(
        archive,
        limiter=RateLimiter(min_interval_seconds=3.0),
        breaker=CircuitBreaker(failure_threshold=3, cooldown_seconds=120.0),
        retry=RetryPolicy(max_attempts=2, base_delay_seconds=3.0, max_delay_seconds=6.0),
        timeout=25.0,
    )

    today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).date()
    calendar_probe = probe_calendar(
        (today - timedelta(days=40)).strftime("%Y%m%d"),
        (today + timedelta(days=20)).strftime("%Y%m%d"),
    )
    calendar = [date.fromisoformat(day) for day in calendar_probe["trading_days"]]
    if not calendar:
        raise RuntimeError("the exchange-index calendar returned no trading days")

    begin = today - timedelta(days=5)
    end = today

    print(f"== 抓取巨潮公告 {begin} ~ {end} ==")
    out, anns = client.announcements(begin=begin, end=end, column="szse", page_size=10)
    print(f"   ok={out.ok} status={out.http_status} detail={out.detail}")
    print(f"   公告数={len(anns)}")

    first_seen = datetime.now(timezone.utc)
    records = []
    for a in anns[:5]:
        try:
            rec = to_pit_record(a, trading_calendar=calendar, first_seen_at=first_seen)
            rec["pit_status"] = "READY"
        except ValueError as exc:
            # Index bars cannot prove a future trading day. Archive the original now and defer
            # availability rather than manufacturing a weekday calendar.
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
        rec["document_archived"] = bool(document and document.ok and document.content_hash)
        rec["document_hash_verified"] = bool(
            document and document.content_hash and archive.verify(document.content_hash))
        records.append(rec)
        print(f"   - {rec['source_published_date']}  {rec['title'][:40]}")
        print(f"       available_at={rec.get('available_at')}  "
              f"basis={rec.get('available_basis', rec['pit_status'])}")

    out_doc = {"ran_at": first_seen.isoformat(),
               "window": [begin.isoformat(), end.isoformat()],
               "fetch_ok": out.ok, "http_status": out.http_status,
               "detail": out.detail, "receipt_id": out.receipt_id,
               "content_hash": out.content_hash,
               "announcement_count": len(anns),
               "records": records,
               "calendar_source": "eastmoney SSE index daily bars",
               "calendar_first_day": calendar[0].isoformat(),
               "calendar_last_day": calendar[-1].isoformat(),
               "archived_document_count": sum(r["document_archived"] for r in records)}
    path = ARCHIVE_ROOT / "t5-cninfo-run.json"
    path.write_text(json.dumps(out_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {path}")

    receipts = archive.receipts_for("cninfo")
    print(f"cninfo 归档 receipts={len(receipts)} "
          f"distinct_content={archive.distinct_content_count('cninfo')}")
    if out.content_hash:
        print("归档哈希校验:", archive.verify(out.content_hash))
    success = out.ok and bool(records) and all(r["document_archived"] for r in records)
    con.close()
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
