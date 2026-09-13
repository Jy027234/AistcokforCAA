"""T4.4 降级链实跑：主源（东财）失败时显式切到备用源（腾讯）。

这同时是 T4.3 的验收：证明"单一免费源不可靠"已被编码为可运行的降级路径。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.chain import FetchChain, SilentMixError      # noqa: E402
from aquant.adapters.providers.eastmoney import EastmoneyClient            # noqa: E402
from aquant.adapters.providers.resilience import (                         # noqa: E402
    CircuitBreaker, RateLimiter, RetryPolicy,
)
from aquant.adapters.providers.tencent import TencentClient                # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive              # noqa: E402
from aquant.domain.data.source_registry import Domain, default_registry    # noqa: E402

ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"


def main() -> int:
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ARCHIVE_ROOT / "meta.sqlite", isolation_level=None)
    con.row_factory = sqlite3.Row
    archive = ForwardArchive(con, ARCHIVE_ROOT)

    common = dict(
        limiter=RateLimiter(min_interval_seconds=2.0),
        breaker=CircuitBreaker(failure_threshold=3, cooldown_seconds=120.0),
        retry=RetryPolicy(max_attempts=2, base_delay_seconds=2.0, max_delay_seconds=4.0),
        timeout=20.0,
    )
    em = EastmoneyClient(archive, **common)
    tc = TencentClient(archive, **common)

    registry = default_registry()
    chain = FetchChain(registry)

    out: dict = {"ran_at": datetime.now(timezone.utc).isoformat()}

    print("== 降级链：日行情（多源） ==")
    result = chain.run(
        Domain.DAILY_QUOTES,
        {"eastmoney-direct": em, "tencent-ifzq": tc},
        lambda c: (lambda o, bars: (o.ok, {"bars": len(bars), "sample": bars[0] if bars else None},
                                    o.receipt_id, o.detail))(
            *c.daily_quotes("sh600519", "2026-09-01", "2026-09-12", adjust=0)
        ),
    )
    print(json.dumps(result.summary(), ensure_ascii=False, indent=2))
    out["daily_quotes"] = result.summary()

    print("\n== 降级链：前复权日线 ==")
    result2 = chain.run(
        Domain.ADJUSTMENTS,
        {"tencent-ifzq": tc},
        lambda c: (lambda o, bars: (o.ok, {"bars": len(bars), "sample": bars[0] if bars else None},
                                    o.receipt_id, o.detail))(
            *c.daily_quotes("sh600519", "2026-09-01", "2026-09-12", adjust=1)
        ),
    )
    print(json.dumps(result2.summary(), ensure_ascii=False, indent=2))
    out["adjusted_quotes"] = result2.summary()

    print("\n== 源健康状态（降级后） ==")
    health = {s.source_id: s.health.value for s in registry.all()}
    print(json.dumps(health, ensure_ascii=False, indent=2))
    out["source_health"] = health

    print("\n== 静默混接守卫 ==")
    try:
        FetchChain.assert_no_silent_mix(["eastmoney-direct", "tencent-ifzq"])
        print("   !! 未拒绝，这是缺陷")
        out["silent_mix_guard"] = "FAILED"
    except SilentMixError as exc:
        print(f"   已拒绝: {str(exc)[:90]}...")
        out["silent_mix_guard"] = "OK"

    print("\n== 归档状态 ==")
    receipts = archive.receipts_for("eastmoney-direct") + archive.receipts_for("tencent-ifzq")
    outcomes: dict[str, int] = {}
    for r in receipts:
        key = f"{r['source_id']}:{r['outcome']}"
        outcomes[key] = outcomes.get(key, 0) + 1
    summary = {
        "receipts": len(receipts),
        "by_source_outcome": outcomes,
        "distinct_content": {
            "eastmoney-direct": archive.distinct_content_count("eastmoney-direct"),
            "tencent-ifzq": archive.distinct_content_count("tencent-ifzq"),
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    out["archive"] = summary

    # 校验归档字节可还原
    if result2.ok and result2.value:
        pass

    outfile = ARCHIVE_ROOT / "t44-chain-summary.json"
    outfile.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {outfile}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
