"""费率溯源：把权威费率页面抓下来并留证。

为什么单独做这一件事
--------------------
费率表目前是**合成测试费率**（synthetic_test_rate: true），代码里有
assert_usable_for_formal_research() 在拦它。在补齐之前，任何盈亏数字
都不能当真——这是整个产品里唯一一项会污染所有金额结论的问题。

溯源的三条纪律
--------------
1. **区分"有权威来源"与"必须由用户给"**：
   印花税、过户费、经手费/证管费有公开的法定或行业标准；
   佣金是**券商与客户约定**的，没有"正确"值——它必须是一个参数，
   不能替用户编一个数字然后当作事实。
2. **留证**：每一条费率记下来源 URL、抓取时间与内容哈希，
   否则"这个数字哪来的"三个月后无人能答。
3. **抓不到就如实说抓不到**，不拿搜索结果摘要当原文。

用法：
    python tools/fetch_fee_sources.py            # 抓取并写出费率来源档案
    python tools/fetch_fee_sources.py --show     # 只看已知来源清单
退出码：0 = 全部来源已留证；1 = 有来源抓取失败；2 = 环境缺失。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.fetch_guard import (  # noqa: E402
    FetchDenied, FetchPolicy, assert_url_allowed, read_bounded,
)

OUT = ROOT / "deploy" / "agentctl-q0" / "fee-sources.json"
ARCHIVE = ROOT / "deploy" / "agentctl-q0" / "fee-sources"

#: 本机是穿透式代理（所有域名解析到伪 IP 网段），取证必须显式声明。
#: 声明后 DNS 层防护降级为"仅主机名白名单"，因此下面的白名单就是全部防线。
PROXY_NETWORKS = frozenset({"198.18.0.0/15", "fdfe:dcba:9876::/48"})

#: 权威来源清单。kind 区分"法定/行业标准"与"券商约定"——
#: 后者的值不是"正确值"，而是**一个必须由使用者确认的参数**。
SOURCES: list[dict] = [
    {
        "key": "stamp_duty_2023",
        "item": "证券交易印花税",
        "kind": "STATUTORY",
        "rate": "0.0005（卖出方，2023-08-28 起减半征收）",
        "authority": "财政部、税务总局公告 2023 年第 39 号",
        # 用商务部政策法规库的转载页，而不是发布机关站点：
        # 发布机关站点在本机（穿透式代理）下证书不匹配，
        # 抓不下来。转载页保留了文号、发布部门、发布日期与实施日期，
        # 足以核对费率——但**它仍是转载**，这一点写在 note 里，不假装是原件。
        "url": "https://policy.mofcom.gov.cn/claw/clawContent.shtml?id=97971",
        "note": "印花税自 2008 年起为单边征收（仅卖方），2023-08-28 起税率减半。"
                "本链接为商务部政策法规库转载页（发布机关站点本机不可达）。",
    },
    {
        "key": "transfer_fee_2022",
        "item": "过户费",
        "kind": "INDUSTRY_STANDARD",
        "rate": "0.00001（双向，成交金额的万分之 0.01）",
        "authority": "中国证券登记结算有限责任公司：关于降低股票交易过户费收费标准的通知",
        "url": "https://www.financialnews.com.cn/zq/zj/202204/t20220428_245340.html",
        "note": "2022-04-29 起总体下调 50%，原文表述为「统一下调为按照成交金额"
                "0.01‰ 双向收取」。本链接为金融时报—中国金融新闻网报道。",
    },
    {
        "key": "commission",
        "item": "券商佣金",
        "kind": "CONTRACTUAL",
        "rate": "**无权威值**：由券商与客户约定，上限为成交金额的千分之三，最低通常 5 元",
        "authority": "（无）—— 这一项必须由使用者按自己的费率确认",
        "url": None,
        "note": ("合成费率表用万分之 2.5、最低 5 元，那是一个**假设**，不是事实。"
                 "在它被替换成使用者确认的值之前，盈亏数字不能当真。"),
    },
]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
}


def fetch(url: str) -> tuple[str, str, str]:
    """抓一页。返回 (正文, sha256, 长度)。**先过 fetch_guard**。"""

    policy = FetchPolicy(allowed_hosts=frozenset({_host(url)}),
                         trusted_proxy_networks=PROXY_NETWORKS,
                         allowed_schemes=frozenset({"http", "https"}))
    assert_url_allowed(url, policy)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = read_bounded(resp, policy)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8", errors="replace")
    return text, digest, str(len(raw))


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).hostname or ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="只列出来源清单，不抓取")
    args = ap.parse_args()

    if args.show:
        for s in SOURCES:
            print(f"[{s['kind']}] {s['item']}: {s['rate']}")
            print(f"    依据：{s['authority']}")
            print(f"    链接：{s['url'] or '（无）'}")
        return 0

    ARCHIVE.mkdir(parents=True, exist_ok=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    failed = 0
    for s in SOURCES:
        rec = {k: s[k] for k in ("key", "item", "kind", "rate", "authority", "note")}
        rec["url"] = s["url"]
        if not s["url"]:
            # 佣金没有权威来源可抓——如实记录"无来源"，
            # 而不是去找一个看起来权威的页面来充数。
            rec["fetched"] = False
            rec["reason"] = "该费率由券商与客户约定，无公开权威标准"
            records.append(rec)
            print(f"  SKIP  {s['item']}：{rec['reason']}")
            continue
        try:
            text, digest, size = fetch(s["url"])
            rec.update({"fetched": True, "contentHash": digest, "bytes": int(size),
                        "fetchedAt": datetime.now(timezone.utc).isoformat()})
            target = ARCHIVE / (digest.removeprefix("sha256:")[:16] + ".html")
            target.write_bytes(text.encode("utf-8"))
            rec["archivedAt"] = str(target.relative_to(ROOT))
            # 只留证，不解析：把 HTML 当"权威数值"提取出来，
            # 等于又制造一处未经核对的事实。
            print(f"  OK    {s['item']}  {size} bytes  {digest[:23]}...")
        except (FetchDenied, urllib.error.URLError, OSError, TimeoutError) as exc:
            rec.update({"fetched": False, "reason": f"{type(exc).__name__}: {exc}"})
            failed += 1
            print(f"  FAIL  {s['item']}：{rec['reason']}")
        records.append(rec)

    OUT.write_bytes(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": ("费率来源档案。kind=CONTRACTUAL 的项**没有权威值**，"
                 "必须由使用者确认；不要把合成费率当成事实。"),
        "sources": records,
    }, ensure_ascii=False, indent=2).encode("utf-8"))
    print(f"\n档案：{OUT}")
    print(f"抓取失败 {failed}/{len([s for s in SOURCES if s['url']])} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
