"""看一眼最近有没有需要人管的事。

为什么不用"打开 alerts.jsonl 看"：那需要人记得路径、还得自己过滤级别。
告警的价值在于**被看见**，因此给它一个明确的入口，并在有问题时用退出码
表达出来（可以接进 CI 或别的检查里）。

用法：
    python tools/show_alerts.py                # 最近 20 条
    python tools/show_alerts.py --days 7       # 最近 7 天的 ERROR
    python tools/show_alerts.py --errors-only
退出码：0 = 无 ERROR；1 = 有 ERROR；2 = 环境缺失。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.operations.alerting import (  # noqa: E402
    LEVEL_ERROR, AlertLog,
)

ALERTS = ROOT / "deploy" / "agentctl-q0" / "alerts.jsonl"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--days", type=int, default=None,
                    help="只看最近 N 天的 ERROR（按告警时间）")
    ap.add_argument("--errors-only", action="store_true")
    args = ap.parse_args()

    log = AlertLog(ALERTS)
    entries = log.recent(limit=max(1, args.limit))
    if args.days is not None:
        since = (date.today() - timedelta(days=args.days)).isoformat()
        entries = [e for e in entries
                   if e.get("level") == LEVEL_ERROR
                   and (e.get("raised_at") or "")[:10] >= since]
    if args.errors_only:
        entries = [e for e in entries if e.get("level") == LEVEL_ERROR]

    if not entries:
        print("没有告警。")
        return 0

    for e in entries:
        print(f"{e['raised_at'][:19]}  {e['level']:<7} {e['source']}: {e['message']}")
        detail = e.get("detail") or {}
        if detail:
            for key, value in detail.items():
                text = str(value)
                print(f"    {key}: {text[:160]}")

    errors = [e for e in entries if e.get("level") == LEVEL_ERROR]
    print()
    print(f"共 {len(entries)} 条，其中 ERROR {len(errors)} 条")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
