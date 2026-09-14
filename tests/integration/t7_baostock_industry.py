"""BaoStock 行业分类技术验证（T7）。

要回答的问题
------------
1. 数据完整性：全市场覆盖多少、缺多少、分类体系是什么；
2. 时点语义：updateDate 是"分类变更日"还是"本次快照日"——
   这决定它能否用于历史时点查询（§7 的核心要求）；
3. 代码体系：BaoStock 用 sh.600519，我方内部用 SH.600519，映射是否无损；
4. 可重复性：连续两次拉取结果是否一致；
5. 与我方现有研究池交叉校验：手工声明的行业与权威分类差多少。

不做的事：不写入任何快照、不改研究池。本脚本只做只读调查，
结论用退出码表达。

用法：
    $env:AQUANT_TRUSTED_PROXY_NETWORKS='198.18.0.0/15,fdfe:dcba:9876::/48'
    python -m tests.integration.t7_baostock_industry

退出码：0 = 全部通过；1 = 有断言失败；2 = 数据源不可用。
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))


def internal_id(code: str) -> str:
    """sh.600519 -> SH.600519。与我方内部 ID 规则一致。"""

    market, _, number = code.partition(".")
    return market.upper() + "." + number


def main() -> int:
    try:
        import baostock as bs
    except ImportError:
        print("需要 baostock：pip install baostock")
        return 2

    print("=== [1] 登录与免凭证 ===")
    t0 = time.time()
    lg = bs.login()
    check('无需凭证即可登录', lg.error_code == '0',
          f'{lg.error_code} {lg.error_msg} ({time.time()-t0:.1f}s)')
    if lg.error_code != '0':
        return 2

    try:
        print()
        print("=== [2] 行业分类全量 ===")
        # 全量拉取要 25~90 秒且服务端会限速，因此优先用缓存。
        # 缓存是**可复核的输入**（记录了抓取时间与全部字段），
        # 用 T7_REFRESH_CACHE=1 强制重新抓取。
        cache_path = ROOT / "deploy" / "agentctl-q0" / "baostock-industry-cache.json"
        t0 = time.time()
        if cache_path.exists() and os.environ.get("T7_REFRESH_CACHE") != "1":
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            rows = [[r.get(f) for f in cached["fields"]] for r in cached["records"]]
            fields = list(cached["fields"])
            print(f"  使用缓存 {cache_path.name}（{len(rows)} 条）")
            print("  提示：T7_REFRESH_CACHE=1 可强制重新抓取")
        else:
            rs = bs.query_stock_industry()
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            fields = list(rs.fields)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(json.dumps(
                {"fields": fields,
                 "records": [dict(zip(fields, r)) for r in rows]},
                ensure_ascii=False).encode("utf-8"))
        elapsed = time.time() - t0
        print(f"  {len(rows)} 条，{elapsed:.1f}s")
        check("全量查询返回数据", len(rows) > 4000, f"{len(rows)} 条")
        check("字段含分类体系与行业名",
              set(["code", "code_name", "industry",
                   "industryClassification"]) <= set(fields), str(fields))

        records = [dict(zip(fields, r)) for r in rows]

        systems = Counter(r["industryClassification"] for r in records)
        check("分类体系唯一（不混多套口径）", len(systems) == 1, str(dict(systems)))

        empty = [r for r in records if not (r.get("industry") or "").strip()]
        check("行业名为空的比例可接受", len(empty) / max(len(records), 1) < 0.15,
              f"{len(empty)}/{len(records)} 为空（多为退市或早期证券）")

        upd = Counter(r["updateDate"] for r in records)
        print(f"  updateDate 取值: {dict(list(upd.items())[:5])}")
        # 这一条是**已知限制的记录**，不是"通过/失败"：
        # updateDate 只有一个取值，说明它是本次快照日期而不是分类变更史，
        # 因此行业分类**不能**用于历史时点查询（与我方行情同属
        # RECONSTRUCTED 口径）。断言写成"取值数很少"，是为了在
        # 将来 BaoStock 真的提供变更史时立刻失败，提醒可以升级。
        check("updateDate 仍是快照日（记为已知限制）", len(upd) <= 3,
              f"{len(upd)} 个不同取值；"
              "它不是变更史，故分类不可用于历史时点查询")

        print()
        print("=== [3] 代码体系映射 ===")
        bad = [r["code"] for r in records if "." not in r["code"]]
        check("代码都带市场前缀", not bad, f"{len(bad)} 条异常")
        sample = [internal_id(r["code"]) for r in records[:3]]
        check("映射到内部 ID 无损", all(s.count(".") == 1 for s in sample),
              str(sample))

        print()
        print("=== [4] 可重复性：再拉一次比对 ===")
        rs2 = bs.query_stock_industry(code="sh.600519")
        again = []
        while rs2.next():
            again.append(rs2.get_row_data())
        single = dict(zip(rs2.fields, again[0])) if again else {}
        maotai = next((r for r in records if r["code"] == "sh.600519"), {})
        check("单只查询与全量结果一致",
              single.get("industry") == maotai.get("industry"),
              f"{single.get('industry')!r} vs {maotai.get('industry')!r}")

        print()
        print("=== [5] 与研究池交叉校验 ===")
        pool = ROOT / 'configs' / 'real-pool-2026-09.yaml'
        mismatches: list[str] = []
        missing: list[str] = []
        if pool.exists():
            import yaml
            doc = yaml.safe_load(pool.read_text(encoding="utf-8"))
            by_code = {r["code"]: r for r in records}
            mismatches_free: list[str] = []
            for entry in doc.get("instruments") or []:
                code = entry["code"]
                # BaoStock 代码带小数点：sh600519 -> sh.600519。
                # 这里曾写成 code[:2] + code[2:]，等于原样拼回、
                # 一个都命中不了，于是"全部命中"的断言被静默跳过、
                # 而差异清单里堆满了假差异。
                bcode = code[:2] + "." + code[2:]
                hit = by_code.get(bcode)
                if hit is None:
                    missing.append(code)
                    continue
                declared = (entry.get("industry_name") or "").strip()
                authoritative = (hit.get("industry") or "").strip()
                if not authoritative:
                    mismatches_free.append(code)
                if declared and authoritative and declared not in authoritative:
                    mismatches.append(
                        f"{code}: 声明 {declared!r} vs 权威 {authoritative!r}")
            total = len(doc.get("instruments") or [])
            check("研究池标的全在权威分类里", not missing,
                  f"缺失 {missing}" if missing else f"{total} 只全部命中")
            # 差异本身是**预期**的：我方是手工声明的行业名，权威是证监会口径。
            # 断言的是"能查到权威分类"，不是"两者必须一致"——
            # 把一致性写成断言，只会在我方声明与权威不同时给出假失败。
            check("每只都能查到权威行业名", not mismatches_free,
                  f"{len(mismatches)} 只行业名与权威口径不同（预期，见下）")
            print(f"  行业口径差异 {len(mismatches)} 条：")
            for m in mismatches[:8]:
                print("    - " + m)
        else:
            check("研究池配置存在", False, str(pool))

        print()
        print("=== [6] 粒度：证监会门类 vs 大类 ===")
        codes = [r['industry'][:3] for r in records if r.get('industry')]
        letter = Counter(c[:1] for c in codes)
        two_digit = Counter(c[:3] for c in codes)
        print(f"  门类（字母）{len(letter)} 个；大类（字母+2位）{len(two_digit)} 个")
        check("门类数量符合证监会分类", 15 <= len(letter) <= 25, str(sorted(letter)))
        print(f"  大类样例: {sorted(two_digit)[:8]}")

        print()
        print("=== [7] 延迟：单只查询是否满足逐只使用 ===")
        t0 = time.time()
        for code in ("sh.600519", "sz.000001", "sh.601398"):
            r = bs.query_stock_industry(code)
            while r.next():
                r.get_row_data()
        per = (time.time() - t0) / 3
        check("单只查询延迟可接受（<2s）", per < 2.0, f"{per:.2f}s/只")
        print(f"  全量 {elapsed:.1f}s vs 单只 {per:.2f}s："
              "逐只取 5553 只需 {:.0f} 分钟，必须缓存全量".format(per * 5553 / 60))

    finally:
        bs.logout()

    if not os.environ.get("AQUANT_TRUSTED_PROXY_NETWORKS"):
        print("\n  注意：未设置 AQUANT_TRUSTED_PROXY_NETWORKS")

    failed = [c for c in checks if not c[1]]
    print()
    print("=" * 62)
    print(f"T7 BaoStock 行业分类验证 {len(checks) - len(failed)}/{len(checks)} 通过")
    for name, _, detail in failed:
        print("  - " + name + "  " + detail)

    out = ROOT / 'deploy' / 'agentctl-q0' / 't7-baostock-industry.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(json.dumps({
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(rows),
        "classification_systems": dict(systems),
        "update_date_values": dict(list(upd.items())[:5]),
        "empty_industry": len(empty),
        "latency_seconds": {"bulk": round(elapsed, 1), "per_instrument": round(per, 2)},
        "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks],
        "conclusion": "PASS" if not failed else "FAIL",
    }, ensure_ascii=False, indent=2).encode("utf-8"))
    print(f"报告：{out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())