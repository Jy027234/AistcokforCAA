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

    # 交易日历（本机无真实日历源，这里用一段明确的近似并在输出中标注）
    today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).date()
    calendar = []
    d = today - timedelta(days=20)
    while d <= today + timedelta(days=10):
        if d.weekday() < 5:      # 仅作占位，真实日历须来自快照（§7.3）
            calendar.append(d)
        d += timedelta(days=1)

    begin = today - timedelta(days=5)
    end = today

    print(f"== 抓取巨潮公告 {begin} ~ {end} ==")
    out, anns = client.announcements(begin=begin, end=end, column="szse", page_size=10)
    print(f"   ok={out.ok} status={out.http_status} detail={out.detail}")
    print(f"   公告数={len(anns)}")

    first_seen = datetime.now(timezone.utc)
    records = []
    for a in anns[:5]:
        rec = to_pit_record(a, trading_calendar=calendar, first_seen_at=first_seen)
        records.append(rec)
        print(f"   - {rec['source_published_date']}  {rec['title'][:40]}")
        print(f"       available_at={rec['available_at']}  basis={rec['available_basis']}")

    out_doc = {"ran_at": first_seen.isoformat(),
               "window": [begin.isoformat(), end.isoformat()],
               "fetch_ok": out.ok, "http_status": out.http_status,
               "detail": out.detail, "receipt_id": out.receipt_id,
               "content_hash": out.content_hash,
               "announcement_count": len(anns),
               "records": records,
               "calendar_note": "placeholder weekday calendar; a real calendar must come from the snapshot"}
    path = ARCHIVE_ROOT / "t5-cninfo-run.json"
    path.write_text(json.dumps(out_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {path}")

    receipts = archive.receipts_for("cninfo")
    print(f"cninfo 归档 receipts={len(receipts)} "
          f"distinct_content={archive.distinct_content_count('cninfo')}")
    if out.content_hash:
        print("归档哈希校验:", archive.verify(out.content_hash))
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
