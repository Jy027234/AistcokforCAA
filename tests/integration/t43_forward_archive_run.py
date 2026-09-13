"""T4.3 前向归档实跑：对真实端点抓一次，无论成败都留下证据。

用法：
    python tests/integration/t43_forward_archive_run.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.eastmoney import EastmoneyClient          # noqa: E402
from aquant.adapters.providers.resilience import CircuitBreaker, RateLimiter, RetryPolicy  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive            # noqa: E402

ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"


def main() -> int:
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ARCHIVE_ROOT / "meta.sqlite", isolation_level=None)
    con.row_factory = sqlite3.Row
    archive = ForwardArchive(con, ARCHIVE_ROOT)

    client = EastmoneyClient(
        archive,
        limiter=RateLimiter(min_interval_seconds=2.0),
        breaker=CircuitBreaker(failure_threshold=3, cooldown_seconds=120.0),
        retry=RetryPolicy(max_attempts=2, base_delay_seconds=3.0, max_delay_seconds=6.0),
        timeout=20.0,
    )

    summary: dict = {"ran_at": datetime.now(timezone.utc).isoformat(), "attempts": []}

    print("== 抓取 1：全 A 股列表 ==")
    out, rows = client.universe(page_size=5)
    print(f"   ok={out.ok} status={out.http_status} detail={out.detail}")
    print(f"   rows={len(rows)}")
    summary["attempts"].append({"endpoint": "universe", "ok": out.ok,
                                "status": out.http_status, "rows": len(rows),
                                "detail": out.detail, "receipt": out.receipt_id,
                                "content_hash": out.content_hash})

    print("\n== 抓取 2：日线（不复权） ==")
    out2, bars = client.daily_quotes("1.600519", "20260901", "20260912", adjust=0)
    print(f"   ok={out2.ok} status={out2.http_status} detail={out2.detail}")
    print(f"   bars={len(bars)}")
    summary["attempts"].append({"endpoint": "daily_quotes", "ok": out2.ok,
                                "status": out2.http_status, "bars": len(bars),
                                "detail": out2.detail, "receipt": out2.receipt_id,
                                "content_hash": out2.content_hash})

    # 归档状态
    receipts = archive.receipts_for("eastmoney-direct")
    outcomes: dict[str, int] = {}
    for r in receipts:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    summary["archive"] = {
        "receipts": len(receipts),
        "outcomes": outcomes,
        "distinct_content": archive.distinct_content_count("eastmoney-direct"),
        "breaker": client.breaker.state(),
    }
    print("\n== 归档状态 ==")
    print(json.dumps(summary["archive"], ensure_ascii=False, indent=2))

    # 若成功，验证归档字节可还原且哈希一致
    if out.content_hash:
        print("   归档哈希校验:", archive.verify(out.content_hash))
        summary["archive"]["hash_verified"] = archive.verify(out.content_hash)

    outfile = ARCHIVE_ROOT / "t43-run-summary.json"
    outfile.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {outfile}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
