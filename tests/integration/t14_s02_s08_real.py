"""T14：涨停不成交（S02）与停牌估值（S08）的真实数据验收。

为什么要在**真实数据**上再验一遍
--------------------------------
S02/S08 在合成夹具里已经有单元测试，而且一直是绿的。问题恰恰在这里：
合成夹具的行情行里手写了 `board_limit_up: true`，而真实快照的行情行
**没有这个字段**——读取器取默认值 False，于是模拟器里那条

    if order.side is Side.BUY and bar.board_limit_up:  ->  NO_FILL

在真实数据上从未被触发过。全市场快照实测涨停标记 0 行，而按板块规则
重算，窗口内有上百行开盘即涨停。"测试全绿 + 真实路径失效"，
是这类"派生事实没人计算"缺陷的典型形态。

本脚本用真实快照验证三件事：

  1. **S02**：开盘即涨停的标的，买入不得成交；
  2. **S02 反向对照**：同一标的在非涨停日必须能成交——
     否则"不成交"可能只是因为别的原因，这条守卫就没被证明有作用；
  3. **S08**：持仓标的当日无行情（停牌）时，估值用**此前最后一个有效
     收盘价**，并如实记录价格基准与停牌天数，绝不当作 0、也不静默丢弃。

用法：
    python -m tests.integration.t14_s02_s08_real
退出码：0 = 全部通过；1 = 有断言失败；2 = 缺快照。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.simulation.board_rules import BOARD_RULES      # noqa: E402
from aquant.domain.simulation.fees import synthetic_fee_table      # noqa: E402
from aquant.domain.simulation.simulator import (                   # noqa: E402
    Bar, DailySimulator, Lot, Order, OrderStatus, Side,
)

SNAPSHOT_DIR = ROOT / "deploy" / "universe-snapshot"
SNAPSHOT_ID = "snap-universe"
DATASET = SNAPSHOT_DIR / "api" / "datasets" / SNAPSHOT_ID
AS_OF = "2026-09-14T15:00:00+08:00"

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))


def _cents(value) -> int | None:
    return None if value in (None, "") else int(value)


def main() -> int:
    quotes_path = DATASET / "daily_quotes.json"
    if not quotes_path.exists():
        print("缺少快照：" + str(SNAPSHOT_DIR))
        print("先运行：python -m tests.integration.t10_universe_snapshot")
        return 2

    quotes = json.loads(quotes_path.read_text(encoding="utf-8"))
    instruments = json.loads((DATASET / "instruments.json").read_text(encoding="utf-8"))
    listing = {i["instrument_id"]: (i["exchange"], i["board"]) for i in instruments}
    days = sorted({q["trading_day"] for q in quotes})
    last_day = days[-1]
    print(f"快照 {SNAPSHOT_ID}：{len(quotes)} 行，{len(instruments)} 只，"
          f"{days[0]} .. {last_day}")

    # ---------------------------------------------------------- 素材：真实涨停行
    rows = [q for q in quotes if q.get("board_limit_up")]
    check("快照里存在开盘即涨停的行", bool(rows),
          f"{len(rows)} 行（为 0 说明派生事实仍未被计算，S02 守卫不会生效）")
    if not rows:
        return _finish()

    sim = DailySimulator(fee_table=synthetic_fee_table(), board_rules=BOARD_RULES,
                         listings=listing)

    def bars_for(instrument: str, day: str) -> dict[str, Bar]:
        """按 PlanService._bars 的同一口径构造 Bar（这里是唯一的真相来源）。"""
        for q in quotes:
            if q["instrument_id"] == instrument and q["trading_day"] == day:
                exchange, board = listing[instrument]
                return {instrument: Bar(
                    instrument_id=instrument, trading_day=datetime.fromisoformat(day).date(),
                    open_cents=_cents(q["open_cents"]), high_cents=_cents(q["high_cents"]),
                    low_cents=_cents(q["low_cents"]), close_cents=_cents(q["close_cents"]),
                    prev_close_cents=_cents(q["prev_close_cents"]) or _cents(q["open_cents"]),
                    volume_shares=int(q["volume_shares"]),
                    board_limit_up=bool(q.get("board_limit_up")),
                )}
        return {}

    hit = rows[-1]
    iid, day = hit["instrument_id"], hit["trading_day"]
    exchange, board = listing[iid]
    pct = next(r.price_limit_pct for r in BOARD_RULES
               if r.exchange == exchange and r.board == board
               and r.covers(datetime.fromisoformat(day).date()))
    limit_price = hit["prev_close_cents"] + int(
        Decimal(hit["prev_close_cents"]) * pct / Decimal(100)
        + Decimal("0.5")) // 1
    print(f"素材：{iid}（{board}，{pct}%）{day} 前收 {hit['prev_close_cents']} "
          f"涨停价 {limit_price} 开盘 {hit['open_cents']}")

    # ---------------------------------------------------------- S02：涨停不成交
    r = sim.simulate(
        trading_day=datetime.fromisoformat(day).date(),
        orders=[Order(order_id="buy-limit-up", instrument_id=iid,
                      side=Side.BUY, quantity=100)],
        bars=bars_for(iid, day), cash_available_cents=100_000_000, lots=[],
    )
    check("S02 开盘涨停买入不成交", not r.fills, f"成交 {len(r.fills)} 笔")
    reasons = [x["reason"] for x in r.rejections]
    check("S02 拒绝原因为 LIMIT_PRICE_BLOCKED", reasons == ["LIMIT_PRICE_BLOCKED"],
          str(reasons))
    check("S02 未凭空产生现金分录", not r.cash_entries, str(len(r.cash_entries)))
    check("S02 订单状态为 NO_FILL",
          r.orders and r.orders[0].status is OrderStatus.NO_FILL)

    # --------------------- S02 反向对照：同一标的在非涨停日必须真的能成交
    #
    # 这一条是本脚本的关键。只断言"涨停日不成交"是不够的：
    # 如果这个标的因为停牌、缺规则、代码形态不匹配等原因本来就不会成交，
    # 那条断言在守卫完全失效时也会通过。必须证明**同一个订单**在
    # 同一条链路上、换一个非涨停日就会成交。
    candidates = [q for q in quotes
                  if q["instrument_id"] == iid and q["trading_day"] != day
                  and not q.get("board_limit_up") and q["open_cents"]]
    check("该标的在窗口内有非涨停日可作对照", bool(candidates),
          f"{len(candidates)} 天")
    if candidates:
        control = candidates[-1]
        r2 = sim.simulate(
            trading_day=datetime.fromisoformat(control["trading_day"]).date(),
            orders=[Order(order_id="buy-control", instrument_id=iid,
                          side=Side.BUY, quantity=100)],
            bars=bars_for(iid, control["trading_day"]),
            cash_available_cents=100_000_000, lots=[],
        )
        check("S02 反向对照：非涨停日同一标的可成交", bool(r2.fills),
              f"对照日 {control['trading_day']}，成交 {len(r2.fills)} 笔，"
              f"拒绝 {r2.rejections}")

    # ---------------------------------------------------------- S08：停牌不成交
    have = {(q["instrument_id"], q["trading_day"]) for q in quotes}
    # 停牌有两个方向，都要覆盖：
    #   * 窗口**中途/末尾**缺行情：此前有收盘价，估值要用它（本用例主目标）；
    #   * 窗口**开头**缺行情：此前没有窗口内数据，估值无价可用。
    # 第一版只找"末尾"，于是 0 命中——只覆盖一个方向会漏掉另一类的行为。
    gaps: dict[str, dict] = {}
    for iid in sorted({q["instrument_id"] for q in quotes}):
        traded = sorted(q["trading_day"] for q in quotes if q["instrument_id"] == iid)
        missing = [d for d in days if (iid, d) not in have]
        if not missing:
            continue
        gaps[iid] = {
            "traded": traded,
            "trailing": [d for d in missing if d > traded[-1]],
            "leading": [d for d in missing if d < traded[0]],
        }
    trailing_ids = [i for i, g in gaps.items() if g["trailing"]]
    leading_ids = [i for i, g in gaps.items() if g["leading"]]
    # 这里是**事实报告**，不是断言：真实数据里有没有中途停牌不由我们决定。
    # 池内 900 只恰好全部覆盖 61 天，所以这一段通常没有素材——
    # 把它写成"必须存在"会让用例依赖一次幸运的取样。
    # 中途停牌的 carry-forward 不变量另由 tests/golden 的受控用例锁定。
    print(f"  停牌事实：窗口中途/末尾缺行情 {len(trailing_ids)} 只"
          f"{trailing_ids[:3]}；窗口开头缺行情 {len(leading_ids)} 只"
          f"{leading_ids[:3]}")
    suspended = None
    if trailing_ids:
        siid0 = trailing_ids[0]
        suspended = (siid0, gaps[siid0]["traded"][-1], gaps[siid0]["trailing"])
    if suspended:
        siid, last_traded, missing_days = suspended
        print(f"素材：{siid} 最后有行情 {last_traded}，随后停牌 "
              f"{missing_days[0]} .. {missing_days[-1]}")
        gap_day = missing_days[0]
        r3 = sim.simulate(
            trading_day=datetime.fromisoformat(gap_day).date(),
            orders=[Order(order_id="sell-suspended", instrument_id=siid,
                          side=Side.SELL, quantity=100)],
            bars=bars_for(siid, gap_day), cash_available_cents=100_000_000,
            lots=[Lot(lot_id="lot-susp", instrument_id=siid,
                      acquired_trading_day=datetime.fromisoformat(last_traded).date(),
                      earliest_sellable_day=datetime.fromisoformat(last_traded).date(),
                      quantity_original=100, quantity_remaining=100,
                      cost_basis_cents_per_share=1000)],
        )
        check("S08 无行情（停牌）时不成交", not r3.fills, f"成交 {len(r3.fills)} 笔")
        check("S08 拒绝原因为 NO_VALID_OPEN_PRICE",
              [x["reason"] for x in r3.rejections] == ["NO_VALID_OPEN_PRICE"],
              str([x["reason"] for x in r3.rejections]))

        # 估值：停牌日必须沿用此前最后一个有效收盘价
        from aquant.domain.data.reader import SnapshotReader
        from aquant.domain.data.snapshot import SnapshotStore
        from aquant.domain.data.db import connect as _connect
        con = _connect(SNAPSHOT_DIR / "meta.sqlite")
        try:
            reader = SnapshotReader(SnapshotStore(con, SNAPSHOT_DIR / "api"))
            as_of = datetime.fromisoformat(AS_OF)
            history = reader.daily_quotes(SNAPSHOT_ID, as_of=as_of,
                                          instrument_id=siid)
            prior = [h for h in history
                     if h.trading_day <= datetime.fromisoformat(gap_day).date()]
            check("S08 停牌前有可用收盘价", bool(prior),
                  str(prior[-1].trading_day) if prior else "无")
            if prior:
                from aquant.domain.portfolio.construction import value_positions
                positions, issues = value_positions(
                    lots=[Lot(lot_id="lot-susp", instrument_id=siid,
                              acquired_trading_day=datetime.fromisoformat(last_traded).date(),
                              earliest_sellable_day=datetime.fromisoformat(last_traded).date(),
                              quantity_original=100, quantity_remaining=100,
                              cost_basis_cents_per_share=1000)],
                    bars={},      # 停牌：没有 Bar
                    last_valid_price={siid: (prior[-1].close_cents, prior[-1].trading_day)},
                    trading_day=datetime.fromisoformat(gap_day).date(),
                )
                check("S08 停牌持仓仍被计入估值", len(positions) == 1, str(len(positions)))
                if positions:
                    p = positions[0]
                    check("S08 估值用此前最后一个有效收盘价",
                          p.price_cents == prior[-1].close_cents,
                          f"估值价 {p.price_cents} vs 前收 {prior[-1].close_cents}")
                    check("S08 记录价格基准与停牌天数",
                          bool(p.price_basis) and p.staleness_days >= 1,
                          f"basis={p.price_basis} staleness={p.staleness_days}")
                    check("S08 持仓价值不为 0（不得静默丢弃）",
                          p.value_cents == p.quantity * p.price_cents and p.value_cents > 0,
                          str(p.value_cents))
        finally:
            con.close()

    # ------------------- S08 边界：窗口开头就缺行情时不得编造价格
    if leading_ids:
        liid = leading_ids[0]
        gap_day = gaps[liid]["leading"][0]
        first_traded = gaps[liid]["traded"][0]
        from aquant.domain.portfolio.construction import value_positions
        positions, issues = value_positions(
            lots=[Lot(lot_id="lot-nolead", instrument_id=liid,
                      acquired_trading_day=datetime.fromisoformat(first_traded).date(),
                      earliest_sellable_day=datetime.fromisoformat(first_traded).date(),
                      quantity_original=100, quantity_remaining=100,
                      cost_basis_cents_per_share=1000)],
            bars={}, last_valid_price={},
            trading_day=datetime.fromisoformat(gap_day).date(),
        )
        # 无可用价格时必须**有结论**：要么持仓带价，要么给出问题说明。
        # 断言"有结论"而不是"必须报错"——报错与否是实现选择，
        # 但沉默地当作 0 不是。
        print(f"素材：{liid} 在窗口开头 {gap_day} 缺行情（首个有行情日 {first_traded}）")
        check("S08 无可用价格时有明确结论（持仓带价或报问题）",
              (bool(positions) and positions[0].price_cents is not None) or bool(issues),
              f"positions={len(positions)} issues={len(issues)}")

    return _finish()


def _finish() -> int:
    failed = [c for c in checks if not c[1]]
    print()
    print("=" * 62)
    print(f"T14 真实数据 S02/S08 验收 {len(checks) - len(failed)}/{len(checks)} 通过")
    for name, _, detail in failed:
        print("  - " + name + "  " + detail)
    out = ROOT / "deploy" / "agentctl-q0" / "t14-s02-s08.json"
    out.write_bytes(json.dumps({
        "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks],
        "conclusion": "PASS" if not failed else "FAIL",
    }, ensure_ascii=False, indent=2).encode("utf-8"))
    print(f"报告：{out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
