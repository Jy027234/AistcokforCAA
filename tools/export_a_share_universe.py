"""导出全市场 A 股证券清单（研究/内部用途）。

**为什么用巨潮这个端点**：东财的清单族接口（``push2.eastmoney.com/api/qt/clist``）
在本机**不可达**（``RemoteDisconnected``，3 次重试全失败），而巨潮的
``/new/data/szse_stock.json`` 一次 GET 就返回全量（实测 6259 条，含 code、
orgId、名称、类别），且与已集成的 ``CninfoClient`` **同域**——不用新开域名、
不用改策略白名单。

走的仍是 ``CninfoClient.document()``，因此响应**照样归档并留收据**：
"清单从哪来、什么时候取的"可复核，与其余巨潮证据同一套。

只取 ``category == "A股"``；B 股、基金、债券等不进入 S2 范围。

用法::

    python tools/export_a_share_universe.py --output data/s2-structured/universe.txt
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.cninfo import (  # noqa: E402
    CninfoClient, default_policy,
)
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402

ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"
DEFAULT_OUTPUT = ROOT / "data" / "s2-structured" / "universe.txt"
LIST_URL = "http://www.cninfo.com.cn/new/data/szse_stock.json"
#: 清单约 580 KiB；给足余量但仍是有界读取。
MAX_BYTES = 8 * 1024 * 1024
WANTED_CATEGORY = "A股"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--trusted-proxy-network", action="append", default=[])
    args = parser.parse_args(argv)
    if args.trusted_proxy_network:
        os.environ["AQUANT_TRUSTED_PROXY_NETWORKS"] = ",".join(args.trusted_proxy_network)

    archive_root = args.archive_root.resolve()
    with closing(connect(archive_root / "meta.sqlite")) as con:
        client = CninfoClient(ForwardArchive(con, archive_root),
                              policy=default_policy())
        outcome = client.document(LIST_URL, label="universe:a-share",
                                  max_bytes=MAX_BYTES)
    if not outcome.ok or not outcome.payload:
        print(f"universe list fetch failed: {outcome.detail}", file=sys.stderr)
        return 2
    try:
        document = json.loads(outcome.payload)
    except ValueError as exc:
        print(f"universe list is not JSON: {exc}", file=sys.stderr)
        return 2
    rows = document.get("stockList") if isinstance(document, dict) else document
    if not isinstance(rows, list) or not rows:
        print("universe list carries no stockList", file=sys.stderr)
        return 2

    codes: dict[str, str] = {}
    org_ids: dict[str, str] = {}
    skipped: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        category = str(row.get("category") or "").strip()
        if category != WANTED_CATEGORY:
            skipped[category or "<empty>"] = skipped.get(category or "<empty>", 0) + 1
            continue
        code = str(row.get("code") or "").strip()
        if len(code) != 6 or not code.isdigit():
            skipped["<bad code>"] = skipped.get("<bad code>", 0) + 1
            continue
        codes[code] = str(row.get("zwjc") or "").strip()
        org_id = str(row.get("orgId") or "").strip()
        if org_id:
            org_ids[code] = org_id

    if not codes:
        print("no A-share code in universe list", file=sys.stderr)
        return 2
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(sorted(codes)) + "\n", encoding="utf-8")
    # orgId 映射单独落盘：绑定时逐期重复查 orgId 是纯浪费（同一只证券每次
    # 都一样），全市场 6179 只 × 3 期会因此多出约 18537 次请求（约 12.9 小时）。
    # 这里把它连同来源证据一起存下来，绑定工具直接复用。
    org_path = output.with_name(output.stem + "-orgids.json")
    org_path.write_text(json.dumps({
        "source_url": LIST_URL,
        "receipt_id": outcome.receipt_id,
        "content_hash": outcome.content_hash,
        "category": WANTED_CATEGORY,
        "organization_ids": dict(sorted(org_ids.items())),
    }, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{output}: {len(codes)} 只 A 股（清单共 {len(rows)} 条）")
    print(f"{org_path}: {len(org_ids)} 条 orgId")
    if skipped:
        print(f"   已排除: {dict(sorted(skipped.items(), key=lambda kv: -kv[1])[:6])}")
    # 证据：清单响应已归档，这里把收据与哈希一并报出来，便于事后复核。
    print(f"   证据: receipt={outcome.receipt_id} hash={outcome.content_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
