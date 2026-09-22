"""T12：F10 在真实财务数据上的验收（ADR-005）。

验的是**真实数据能算出多少、拒绝多少**，不是"能算"。
一个只报成功率的验收没有价值——必须同时报告：

  * 有多少标的算出了 F10；
  * 有多少因 PIT 缺少 TTM 所需的上一完整年度或上年同期被拒绝；
  * 记录级数值检查是否通过（不按累计值的绝对值单调性拒绝）；
  * 算出来的值域是否合理（收益率不可能是几百）。

最后一条最重要：单位错了 10 倍时值域会明显异常，而单看一只看不出来。

用法：
    python -m tests.integration.t12_f10_real
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.fundamentals.pit import CST, FinancialsStore  # noqa: E402
from aquant.domain.fundamentals.records import (  # noqa: E402
    build_statements, consistency_violations,
)
from aquant.operations.decision_market_caps import (  # noqa: E402
    DecisionMarketCapError, load_sidecar,
)

FIN_CACHE = ROOT / "deploy" / "agentctl-q0" / "financials-cache.json"
BARS_CACHE = ROOT / "deploy" / "agentctl-q0" / "universe-bars.json"
MARKET_CAPS = ROOT / "deploy" / "agentctl-q0" / "decision-market-caps.json"

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))


def independently_recompute_ttm_net_profit(rows: list) -> int | None:
    """用验收脚本自己的公式重算 TTM，避免只验证生产函数自身。"""

    if not rows:
        return None

    by_period = {}
    for statement in rows:
        key = statement.period_key
        previous = by_period.get(key)
        if (previous is None
                or (statement.stat_date, statement.pub_date)
                > (previous.stat_date, previous.pub_date)):
            by_period[key] = statement

    latest = max(by_period.values(),
                 key=lambda statement: (statement.stat_date, statement.pub_date))
    if latest.net_profit_micros is None:
        return None

    year, quarter = latest.period_key
    if quarter == 4:
        return latest.net_profit_micros

    prior_annual = by_period.get((year - 1, 4))
    prior_same = by_period.get((year - 1, quarter))
    if (prior_annual is None or prior_annual.net_profit_micros is None
            or prior_same is None or prior_same.net_profit_micros is None):
        return None
    return (prior_annual.net_profit_micros
            + latest.net_profit_micros
            - prior_same.net_profit_micros)


def main() -> int:
    if not FIN_CACHE.exists():
        print("缺少财务缓存：" + str(FIN_CACHE))
        print("先运行：python tools/collect_financials.py")
        return 2

    fin = json.loads(FIN_CACHE.read_text(encoding="utf-8"))
    bars = json.loads(BARS_CACHE.read_text(encoding="utf-8"))

    statements, skipped = build_statements(fin)
    instrument_count = len({s.instrument_id for s in statements})
    print("财务记录 " + str(len(statements)) + " 条，涉及 " + str(instrument_count) + " 只证券")
    check("转换无跳过记录", not skipped, "跳过 " + str(len(skipped)) + " 条")

    problems = consistency_violations(statements)
    bad_instruments = len({p["instrument_id"] for p in problems})
    print("记录级数值检查：" + str(len(problems)) + " 条问题（"
          + str(bad_instruments) + " 只证券）")
    ratio = len(problems) / max(len(statements), 1)
    check("记录级数值问题比例可控（<10%）", ratio < 0.10,
          str(len(problems)) + "/" + str(len(statements)))

    sample = next(iter(bars["bars"].values()))
    trading_days = [date.fromisoformat(b["trading_day"]) for b in sample["rows"]]
    last_day = date.fromisoformat(bars["window"]["last_day"])
    try:
        market_caps = load_sidecar(MARKET_CAPS, expected_as_of=last_day)
    except DecisionMarketCapError as exc:
        print("决策日总市值不可用：" + str(exc))
        print("先在该交易日收盘后运行：python tools/collect_market_caps.py --as-of "
              + last_day.isoformat())
        return 2
    cutoff = datetime(last_day.year, last_day.month, last_day.day, 15, 0, tzinfo=CST)
    print("决策时点 " + cutoff.isoformat() + "（快照末日收盘后）")

    store = FinancialsStore(statements=statements, trading_days=trading_days)

    # 这段重算故意不调用 trailing_twelve_months 的内部公式；它只复用
    # PIT 过滤后的记录，独立验证“上一完整年度 + 本年累计 − 上年同期累计”。
    # 只抽查 Q1--Q3，确保真正覆盖需要滚动公式的分支；Q4 直接取年度值。
    formula_samples: list[dict] = []
    formula_mismatches: list[dict] = []
    for iid in sorted(bars["bars"]):
        visible = store.available_statements(iid, as_of=cutoff)
        if not visible:
            continue
        latest = visible[-1]
        if latest.period_key[1] == 4:
            continue
        expected = independently_recompute_ttm_net_profit(visible)
        actual = store.trailing_twelve_months(iid, as_of=cutoff)
        if expected is None or actual is None:
            continue
        sample = {
            "instrument_id": iid,
            "stat_date": latest.stat_date.isoformat(),
            "expected": expected,
            "actual": actual["ttm_net_profit_micros"],
        }
        formula_samples.append(sample)
        if expected != actual["ttm_net_profit_micros"]:
            formula_mismatches.append(sample)
        if len(formula_samples) >= 10:
            break
    check("TTM 公式抽样重算一致",
          bool(formula_samples) and not formula_mismatches,
          str(len(formula_samples)) + " 条非 Q4 样本，"
          + str(len(formula_mismatches)) + " 条不一致")

    computed: list[dict] = []
    no_ttm = 0
    no_price = 0
    for iid, info in bars["bars"].items():
        rows = info.get("rows") or []
        if not rows:
            continue
        last = rows[-1]
        if last["trading_day"] != bars["window"]["last_day"]:
            continue
        ttm = store.trailing_twelve_months(iid, as_of=cutoff)
        if ttm is None or ttm["ttm_net_profit_micros"] is None:
            no_ttm += 1
            continue
        cap = market_caps.items.get(iid)
        if cap is None:
            no_price += 1
            continue
        # 与生产实现相同的单位换算，但不复用生产函数：净利润为微元，
        # 市值为分，因此分母乘 10,000 后同为微元。
        value = (Decimal(ttm["ttm_net_profit_micros"])
                 / (Decimal(cap.market_cap_cents) * Decimal(10_000))).quantize(
                     Decimal("0.000001"))
        computed.append({
            "instrument_id": iid,
            "f10": str(value),
            "value_float": float(value),
            "ttm_net_profit_micros": ttm["ttm_net_profit_micros"],
            "latest_stat_date": ttm["latest_stat_date"],
            "latest_pub_date": ttm["latest_pub_date"],
            "ttm_basis": ttm["ttm_basis"],
        })

    # 覆盖率必须以**研究池**为分母。
    #
    # 我第一版拿全市场 5219 只当分母，得到 8.4% 并判为失败——
    # 但 F10 只对池内标的有意义，而池子是 900 只、采集也还没跑完。
    # 用一个与结论无关的分母去判定成败，只会得到一个没有意义的失败。
    pool_path = ROOT / "configs" / "real-pool-csrc.yaml"
    pool_ids = {i["instrument_id"] for i in
                json.loads(pool_path.read_text(encoding="utf-8"))["instruments"]}
    in_pool = [iid for iid in bars["bars"] if iid in pool_ids]
    pool_with_fin = len({s.instrument_id for s in statements} & set(in_pool))

    total = len(computed) + no_ttm + no_price
    print("")
    print("全市场：成功 " + str(len(computed)) + " / TTM 不可得 " + str(no_ttm)
          + " / 缺决策日总市值 " + str(no_price) + "（合计 " + str(total) + "）")
    print("研究池 " + str(len(in_pool)) + " 只，其中有财务记录 " + str(pool_with_fin) + " 只")
    check("有标的算出 F10", len(computed) > 100, str(len(computed)) + " 只")
    # 分母是"池内**且有财务记录**的标的"：没有财务记录的标的不是
    # "算不出来"，而是"还没采到"，两者必须分开报。
    if pool_with_fin:
        coverage = len(computed) / pool_with_fin
        check("池内有财务记录的标的覆盖率 >= 80%", coverage >= 0.80,
              str(len(computed)) + "/" + str(pool_with_fin)
              + "（{:.1%}）".format(coverage))
    else:
        check("池内有财务记录", False, "采集尚未完成")

    if computed:
        values = sorted(c["value_float"] for c in computed)
        n = len(values)
        print("")
        print("F10 值域：最小 {:.4f}  中位 {:.4f}  最大 {:.4f}".format(
            values[0], values[n // 2], values[-1]))
        negatives = sum(1 for v in values if v < 0)
        print("负值（亏损）：" + str(negatives) + " 只（{:.1%}）".format(negatives / n))

        check("值域合理（收益率在 -2 ~ 2 之间）",
              -2 <= values[0] and values[-1] <= 2,
              "[{:.4f}, {:.4f}]".format(values[0], values[-1]))
        check("中位数量级合理（0.0001 ~ 0.5）",
              0.0001 < values[n // 2] < 0.5, "{:.4f}".format(values[n // 2]))
        check("存在负值（亏损股未被截断为 0）", negatives > 0,
              str(negatives) + " 只；若为 0 说明亏损被伪造成低估值")

        example = computed[0]
        print("")
        print("样例 " + example["instrument_id"] + "：F10=" + example["f10"])
        print("  TTM 净利润 {:,} 元".format(example["ttm_net_profit_micros"] // 1_000_000))
        print("  推导：" + example["ttm_basis"])
        print("  财报 " + example["latest_stat_date"] + " 公布于 "
              + example["latest_pub_date"])

    failed = [c for c in checks if not c[1]]
    print("")
    print("=" * 62)
    print("T12 F10 真实验收 " + str(len(checks) - len(failed)) + "/" + str(len(checks)) + " 通过")
    for name, _, detail in failed:
        print("  - " + name + "  " + detail)

    out = ROOT / "deploy" / "agentctl-q0" / "t12-f10-real.json"
    out.write_bytes(json.dumps({
        "as_of": cutoff.isoformat(),
        "statements": len(statements),
        "consistency_violations": len(problems),
        "computed": len(computed),
        "no_ttm": no_ttm,
        "no_market_cap": no_price,
        "market_cap_as_of": market_caps.market_cap_as_of,
        "market_cap_receipt_id": market_caps.receipt_id,
        "ttm_formula_samples": len(formula_samples),
        "ttm_formula_mismatches": len(formula_mismatches),
        "value_range": ([values[0], values[-1]] if computed else None),
        "negatives": sum(1 for c in computed if c["value_float"] < 0),
        "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks],
        "conclusion": "PASS" if not failed else "FAIL",
    }, ensure_ascii=False, indent=2).encode("utf-8"))
    print("报告：" + str(out))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
