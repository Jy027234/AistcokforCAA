"""把研究池的行业换成证监会口径（BaoStock 权威分类）。

为什么要用脚本改而不是手抄
--------------------------
24 只标的的行业代码与名称如果手工誊写，抄错一个字符就会让
"同行业可比"的横截面排名悄悄算错——而账面上完全看不出来。
脚本按代码精确匹配、逐条报告，且**不猜**：查不到就报错退出。

口径切换本身是研究口径的变更，因此：
  * 池文件里记录 classification_version（§7.1 要求分类可版本化）；
  * 保留原手工声明到 `declared_industry_*_legacy` 字段，便于对照与回溯；
  * 不覆盖池的选择规则与来源说明。

用法：
    python tools/apply_csrc_industry.py            # 预览
    python tools/apply_csrc_industry.py --write    # 写入

前置：deploy/agentctl-q0/baostock-industry-cache.json
（由 tests/integration/t7_baostock_industry.py 生成）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "deploy" / "agentctl-q0" / "baostock-industry-cache.json"
POOL = ROOT / "configs" / "real-pool-2026-09.yaml"

CSRC_VERSION = "CSRC-2012"
#: 证监会门类字母 -> 门类名。用于把行业代码的层级说清楚，
#: 不改变代码本身（权威给到字母+2位，就原样保留）。
GATE_NAMES = {
    "A": "农、林、牧、渔业", "B": "采矿业", "C": "制造业",
    "D": "电力、热力、燃气及水生产和供应业", "E": "建筑业",
    "F": "批发和零售业", "G": "交通运输、仓储和邮政业",
    "H": "住宿和餐饮业", "I": "信息传输、软件和信息技术服务业",
    "J": "金融业", "K": "房地产业", "L": "租赁和商务服务业",
    "M": "科学研究和技术服务业", "N": "水利、环境和公共设施管理业",
    "O": "居民服务、修理和其他服务业", "P": "教育", "Q": "卫生和社会工作",
    "R": "文化、体育和娱乐业", "S": "综合",
}


def csrc_code(industry: str) -> str:
    """从"J66货币金融服务"取出代码 J66。"""

    head = ""
    for ch in industry:
        if ch.isascii() and ch.isalnum():
            head += ch
        else:
            break
    return head.upper()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="写入池文件；默认只预览")
    args = ap.parse_args()

    if not CACHE.exists():
        print(f"缺少分类缓存：{CACHE}")
        print("先运行：python -m tests.integration.t7_baostock_industry")
        return 2

    cached = json.loads(CACHE.read_text(encoding="utf-8"))
    by_code = {r["code"]: r for r in cached["records"]}

    import yaml

    text = POOL.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    instruments = doc.get("instruments") or []

    changes = 0
    unresolved: list[str] = []
    for entry in instruments:
        code = entry["code"]
        bcode = code[:2] + "." + code[2:]
        hit = by_code.get(bcode)
        industry = (hit or {}).get("industry") or ""
        if not industry:
            unresolved.append(code)
            continue
        csrc = csrc_code(industry)
        if not csrc:
            unresolved.append(code)
            continue
        old_code = entry.get("industry_code")
        old_name = entry.get("industry_name")
        if old_code != csrc or old_name != industry:
            changes += 1
            print(f"  {code}: {old_code}/{old_name}"
                  f"  ->  {csrc}/{industry}")
        entry["classification_version"] = CSRC_VERSION
        entry["industry_code"] = csrc
        entry["industry_name"] = industry
        entry["industry_gate"] = GATE_NAMES.get(csrc[:1], "未知门类")
        # 保留原值以便回溯：口径切换是可复核的变更，不是覆盖
        if old_code and "declared_industry_code_legacy" not in entry:
            entry["declared_industry_code_legacy"] = old_code
            entry["declared_industry_name_legacy"] = old_name

    if unresolved:
        print()
        print("以下标的在权威分类里查不到，**不会**写入：")
        for code in unresolved:
            print("  - " + code)
        print("宁可失败也不要留一个猜出来的行业。")
        return 1

    doc["classification_version"] = CSRC_VERSION
    doc["classification_source"] = "BaoStock 证监会行业分类（见 docs/data-capability-baostock.md）"
    doc["classification_note"] = (
        "行业口径已从手工声明的粗分类切换为证监会分类。切换原因：手工声明"
        "只覆盖本池 24 只，扩池即失效；且实测有两处与权威不符"
        "（迈瑞医疗属 C35 专用设备制造业而非医药；爱尔眼科为 Q84 卫生）。"
        "原手工值保留在 declared_industry_*_legacy 字段，便于对照。"
    )
    doc["classification_applied_on"] = date.today().isoformat()

    print()
    print(f"共 {len(instruments)} 只，变更 {changes} 只，未解析 {len(unresolved)} 只")

    if not args.write:
        print()
        print("（预览模式，未写入。加 --write 生效）")
        return 0

    # 保留文件头注释：yaml.safe_dump 会丢掉注释，而池文件头的
    # "选择规则/来源"说明本身就是可复核内容，不能丢。
    header = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            header.append(line)
        else:
            break
    body = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False,
                          default_flow_style=False, width=100)
    POOL.write_bytes(("\n".join(header) + "\n\n" + body).encode("utf-8"))
    print(f"已写入 {POOL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
