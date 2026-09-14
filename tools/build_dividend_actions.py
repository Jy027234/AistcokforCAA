"""从巨潮分红公告生成快照用的 corporate_actions 条目。

为什么要有这一步
----------------
此前"真实分红"只能靠手工构造 CashDividend 对象才跑得通。本脚本把
公告解析（aquant.adapters.providers.cninfo.parse_cash_dividend）接到
快照清单上，使分红真正来自**已归档、可哈希复核**的公告原文。

它同时回答一个必须回答的问题：**这条分红是哪份公告说的**。
每一条输出都带 announcement_id / 标题 / 原文片段，事后可逐条回溯。

用法：
    $env:AQUANT_TRUSTED_PROXY_NETWORKS='198.18.0.0/15,fdfe:dcba:9876::/48'
    python tools/build_dividend_actions.py --out deploy/real-snapshot/dividends.json

只解析 status=OK 的公告；INCOMPLETE（缺字段）与 NOT_APPLICABLE（方案类、
无关公告）会被记录在报告的 rejected 里，**不进快照**——
宁可少一条分红，也不要把一个猜出来的日期或金额写进账本。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.cninfo import (  # noqa: E402
    DIVIDEND_PROPOSAL_TITLE_PATTERN, DIVIDEND_TITLE_PATTERN, parse_cash_dividend,
)
from aquant.domain.data.snapshot import DataMode  # noqa: E402

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36"

#: 取样证券：巨潮的 stock 参数格式是 "<代码>,<orgId>"，
#: 而内部证券 ID 用的是腾讯风格代码（sh600519）。两者不能混用——
#: 我第一版从 stock 参数里 split 出 "600519" 当代码，
#: 于是 `_internal_id` 把 "60" 当市场前缀，生成了 "60.0519" 这种
#: 任何证券都对不上的 ID，快照里一条分红都进不去。
TARGETS = [
    ("sse", "sh600519", "600519,gssh0600519"),    # 贵州茅台
    ("sse", "sh600900", "600900,gssh0600900"),    # 长江电力
    ("szse", "sz000001", "000001,gssz0000001"),   # 平安银行
    ("szse", "sz000333", "000333,gssz0000333"),   # 美的集团
    ("szse", "sz000651", "000651,gssz0000651"),   # 格力电器
]


def list_announcements(column: str, stock: str, se_date: str) -> list[dict]:
    body = urllib.parse.urlencode({
        "pageNum": 1, "pageSize": 60, "column": column, "tabName": "fulltext",
        "stock": stock, "seDate": se_date, "isHLtitle": "true"}).encode()
    req = urllib.request.Request(
        "http://www.cninfo.com.cn/new/hisAnnouncement/query", data=body,
        headers={"User-Agent": UA,
                 "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
                 "X-Requested-With": "XMLHttpRequest"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8")).get("announcements") or []


def _internal_id(code: str) -> str:
    return code[:2].upper() + "." + code[2:]


def main() -> int:
    parser = argparse.ArgumentParser()
    # 输出到 deploy/agentctl-q0/ 而不是快照目录：快照目录会被 T6 清空重建，
    # 放在那里会在下一次跑 T6 时被删掉，而它恰恰是 T6 的输入之一。
    parser.add_argument("--out", default=str(ROOT / "deploy" / "agentctl-q0"
                                            / "dividend-actions.json"))
    parser.add_argument("--window", default="2026-06-01~2026-09-14",
                        help="公告披露窗口")
    args = parser.parse_args()

    try:
        from pypdf import PdfReader
    except ImportError:
        print("需要 pypdf：pip install pypdf")
        return 2

    actions: list[dict] = []
    rejected: list[dict] = []

    for column, code, stock in TARGETS:
        try:
            anns = list_announcements(column, stock, args.window)
        except Exception as exc:                       # noqa: BLE001
            print(f"{code}: 列表查询失败 {type(exc).__name__}: {str(exc)[:80]}")
            return 2

        for ann in anns:
            title = ann.get("announcementTitle") or ""
            if not (DIVIDEND_TITLE_PATTERN.search(title)
                    or DIVIDEND_PROPOSAL_TITLE_PATTERN.search(title)):
                continue
            url = "http://static.cninfo.com.cn/" + (ann.get("adjunctUrl") or "")
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    pdf = resp.read()
                text = "\n".join(p.extract_text() or ""
                                 for p in PdfReader(io.BytesIO(pdf)).pages)
            except Exception as exc:                   # noqa: BLE001
                rejected.append({"code": code, "title": title,
                                 "reason": f"PDF 抓取/解析失败: {type(exc).__name__}"})
                continue

            announced = None
            ts = ann.get("announcementTime")
            if isinstance(ts, (int, float)):
                announced = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()

            result = parse_cash_dividend(
                text=text, title=title,
                announcement_id=str(ann.get("announcementId")), announced_on=announced)

            if result.status != "OK":
                rejected.append({
                    "code": code, "title": title,
                    "announcement_id": str(ann.get("announcementId")),
                    "status": result.status, "notes": result.notes,
                })
                print(f"  [跳过] {code} {title[:34]} -> {result.status}")
                continue

            actions.append({
                "action_id": result.action_id,
                "instrument_id": _internal_id(code),
                "action_type": "CASH_DIVIDEND",
                "announced_on": result.announced_on.isoformat() if result.announced_on else None,
                "record_date": result.record_date.isoformat(),
                "ex_date": result.ex_date.isoformat(),
                "pay_date": result.pay_date.isoformat(),
                # 微元是权威单位：整数分装不下 28.02423 元这样的真实分红
                "cash_per_share_micros": result.cash_per_share_micros,
                "supported": True,
                "tax_treatment": result.tax_treatment,
                "source_announcement_id": str(ann.get("announcementId")),
                "source_url": url,
                "source_title": title,
                "evidence": result.evidence,
                "notes": result.notes,
            })
            print(f"  [采用] {code} {title[:34]} "
                  f"{result.cash_per_share_micros} 微元/股 "
                  f"登记 {result.record_date} 除权 {result.ex_date} 到账 {result.pay_date}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": args.window,
        "mode": DataMode.PRODUCTION.value,
        "note": ("由 tools/build_dividend_actions.py 从巨潮公告解析生成；"
                 "每条都带公告 ID、URL 与原文证据片段，可逐条回溯。"),
        "corporate_actions": actions,
        "rejected": rejected,
    }, ensure_ascii=False, indent=2).encode("utf-8"))

    print()
    print(f"采用 {len(actions)} 条，跳过 {len(rejected)} 条 -> {out}")
    for item in rejected:
        print(f"  跳过 {item['code']} {item['title'][:30]}（{item.get('status') or item.get('reason')}）")
    return 0 if actions else 1


if __name__ == "__main__":
    raise SystemExit(main())
