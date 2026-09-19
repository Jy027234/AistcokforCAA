"""T10：用全市场采集结果建成并发布快照。

与 T6 的区别
------------
T6 用**手工研究池**（24 只）现场联网取数，用来验证数据源与链路；
T10 用**全市场采集缓存**（5219 只）离线建快照，用来消除选样偏差——
S1 的横截面排名必须建立在全市场上，否则得到的是"这几十只里的排名"。

离线建快照还有一个好处：快照可重建、可复查，不依赖当时的网络状况。

用法：
    python -m tests.integration.t10_universe_snapshot
    python -m tests.integration.t10_universe_snapshot --pool configs/real-pool-csrc.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.data.db import apply_migrations, connect  # noqa: E402
from aquant.domain.data.ingest import SnapshotBuilder  # noqa: E402
from aquant.domain.data.reader import SnapshotReader  # noqa: E402
from aquant.domain.data.snapshot import (  # noqa: E402
    DataMode, DatasetRef, SnapshotDraft, SnapshotStore,
)
from aquant.operations.snapshot_lifecycle import (  # noqa: E402
    new_snapshot_id, record_current_snapshot, write_current_pointer,
)

SNAPSHOT_ID = "snap-universe"
CACHE = ROOT / "deploy" / "agentctl-q0" / "universe-bars.json"

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default=str(ROOT / "configs" / "real-pool-csrc.yaml"))
    ap.add_argument("--out", default=str(ROOT / "deploy" / "universe-snapshot"))
    ap.add_argument("--snapshot-id", default=None,
                    help="快照 ID；省略时按窗口末日生成唯一物理 ID。")
    ap.add_argument("--window-start", default=None,
                    help="窗口第一天。指定后只保留该日及以后的行情，"
                         "用于让每日快照覆盖固定起点。")
    ap.add_argument("--window", type=int, default=None,
                    help="窗口交易日数量；与 --window-start 二选一。")
    args = ap.parse_args()

    pool_path = Path(args.pool)
    out_dir = Path(args.out)
    if not CACHE.exists():
        print(f"缺少采集缓存：{CACHE}")
        return 2
    if not pool_path.exists():
        print(f"缺少研究池：{pool_path}")
        return 2

    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    bars_all = cache["bars"]
    window = cache["window"]
    first = bars_all[next(iter(bars_all))]["rows"]
    days = [b["trading_day"] for b in first]

    # 窗口可以用参数收窄。**日常增量必须收窄**：缓存会一直加新交易日，
    # 不收窄的话快照窗口会越拉越长，两次运行的日期范围不同、无法比较。
    if args.window_start:
        days = [d for d in days if d >= args.window_start]
    if args.window:
        days = days[-args.window:]
    if not days:
        print("窗口为空：检查 --window-start / --window 与缓存窗口是否重叠")
        return 2
    print(f"缓存 {len(bars_all)} 只，窗口 {days[0]} .. {days[-1]}（{len(days)} 天）")
    kept = set(days)

    pool = json.loads(pool_path.read_text(encoding="utf-8"))
    picked = pool["instruments"]
    print(f"研究池 {pool['pool_id']}：{len(picked)} 只")

    # 物理快照按 ID 追加写入。历史快照目录和共享 meta.sqlite 都必须保留，
    # 当前对象由独立指针解析；这里绝不能再清空整个输出目录。
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot_id = args.snapshot_id or new_snapshot_id(days[-1])

    instruments: list[dict] = []
    quotes: list[dict] = []
    missing = 0
    for entry in picked:
        iid = entry["instrument_id"]
        info = bars_all.get(iid)
        if not info:
            missing += 1
            continue
        instruments.append({
            "instrument_id": iid,
            "exchange": entry["exchange"],
            "board": entry["board"],
            "security_class": "EQUITY",
            "short_name": entry["name"],
            # 上市日期来自研究池（池由 build_pool_from_universe.py 从
            # BaoStock ipoDate 带过来）。**不再写死 None**：写死会让
            # §3.1「新上市不足规定交易日」与「决策时点是否已上市」
            # 两条判定永远无法生效，而快照看起来完全正常。
            "listed_on": entry.get("listed_on"),
            # 行业取自**权威分类缓存**，不是手工声明
            "industry_code": (entry["industry"][:3] if entry.get("industry") else None),
            "industry_name": entry.get("industry") or None,
            "classification_version": pool.get("classification_version"),
            "status_history": [{
                "valid_from": days[0], "valid_to": None,
                "name": entry["name"], "status": "LISTED",
                "industry_code": (entry["industry"][:3]
                                  if entry.get("industry") else None),
                "industry_name": entry.get("industry") or None,
                "classification_version": pool.get("classification_version"),
            }],
        })
        # 窗口首行的前收取自采集时额外抓的那一小段（collect_universe 的
        # prev_close_before_window）。**没有就是没有**：先前这里用当日开盘价
        # 当占位，等于把"未知"说成"前收=开盘"，于是首行的"开盘是否即涨停"
        # 永远算不出 True。
        prev = info.get("prev_close_before_window")
        for b in info["rows"]:
            if b["trading_day"] not in kept:
                # 窗口外的行只用于推进前收，不进快照
                prev = b["close_cents"]
                continue
            quotes.append({
                "instrument_id": iid,
                "trading_day": b["trading_day"],
                "open_cents": b["open_cents"],
                "high_cents": b["high_cents"],
                "low_cents": b["low_cents"],
                "close_cents": b["close_cents"],
                "volume_shares": b["volume_shares"],
                "amount_cents": b.get("amount_cents"),
                "prev_close_cents": prev,
            })
            prev = b["close_cents"]

    check("池内标的都有行情", missing == 0, f"缺失 {missing} 只")
    # 阈值随窗口缩放，不能写死 61 天窗口的条数：
    # 日常增量会把窗口收窄，写死的常量会把"窗口更短"误报成"数据不足"。
    # 断言的是"每只标的的行情基本齐全"，与窗口长短无关。
    expected_rows = len(instruments) * len(days)
    check("行情条数充足", len(quotes) >= expected_rows * 0.95,
          f"{len(quotes)} 条（窗口 {len(days)} 天 × {len(instruments)} 只 "
          f"= {expected_rows}，允许 5% 因停牌缺失）")
    # 成交额允许极少数缺失：当日无成交（停牌前后、零成交）时
    # BaoStock 返回空值。**缺失即缺失**，不得用价格×成交量补造。
    # 因此这里断言的是覆盖率而不是"全部都有"——
    # 把缺失当成失败会逼着实现方去补造，那才是真正的错误。
    with_amount = sum(1 for q in quotes if q.get("amount_cents"))
    coverage = with_amount / max(len(quotes), 1)
    check("成交额覆盖率 >= 99%", coverage >= 0.99,
          f"{with_amount}/{len(quotes)}（缺失 {len(quotes) - with_amount} 条，真实无成交）")
    # 窗口首行的前收来自采集时额外抓的那一小段；缺失即缺失，不得用开盘价冒充
    seeded = sum(1 for i in instruments
                 if bars_all[i["instrument_id"]].get("prev_close_before_window"))
    check("窗口首行前收已从行情补齐", seeded >= len(instruments) * 0.99,
          f"{seeded}/{len(instruments)} 只有真实前收")
    no_prev = sum(1 for q in quotes if not q.get("prev_close_cents"))
    check("前收缺失只可能出现在窗口首行", no_prev <= len(instruments),
          f"{no_prev} 行无前收（{len(instruments)} 只，首行至多这么多）")
    check("行业齐全", all(i.get("industry_code") for i in instruments),
          f"{sum(1 for i in instruments if i.get('industry_code'))}/{len(instruments)}")

    boards: dict[str, int] = {}
    for i in instruments:
        boards[i["board"]] = boards.get(i["board"], 0) + 1
    check("覆盖主板（可模拟）", boards.get("MAIN", 0) >= 100, str(boards))

    con = connect(out_dir / "meta.sqlite")
    apply_migrations(con)
    api_root = out_dir / "api"
    api_root.mkdir(parents=True, exist_ok=True)
    builder = SnapshotBuilder(con, api_root / "datasets")
    builder.ensure_source(
        "baostock",
        display_name="BaoStock（免费公开接口）",
        domains=["DAILY_QUOTES", "CALENDAR_IDENTITY", "INDUSTRY_CONSTITUENTS",
                 "FINANCIALS"],
        integration_state="TEST_PASSED",
        pit_available="NO",
        pit_basis="RECONSTRUCTED",
        rights={},   # 权利未确认：按 §17.2 未知默认不开放
    )
    # 公司行为的原文证据来自**巨潮公告**，因此 cninfo 也是这个快照的来源。
    # 原先只登记了 baostock，于是 document.source_id='cninfo' 的外键插入失败——
    # 而 cninfo 在 docs/data-rights-register.md 里是权威来源，
    # 快照的登记表里缺了它本身就是不一致。来源登记必须覆盖数据里真正
    # 出现过的每一个来源，而不是"我们主要用哪个"。
    builder.ensure_source(
        "cninfo",
        display_name="巨潮资讯（法定信息披露平台）",
        # 域名必须取自受控词表（data_capability_card 的 CHECK）
        domains=["ANNOUNCEMENTS", "CORPORATE_ACTIONS"],
        integration_state="TEST_PASSED",
        pit_available="NO",
        pit_basis="RECONSTRUCTED",
        rights={},
    )
    # 公司行为：从公告解析结果里取，只保留落在池内且在窗口内的。
    # 与 T6 同源（tools/build_dividend_actions.py），不是手工构造。
    actions: list[dict] = []
    sidecar = ROOT / "deploy" / "agentctl-q0" / "dividend-actions.json"
    if sidecar.exists():
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        wanted = {i["instrument_id"] for i in instruments}
        for ca in payload.get("corporate_actions") or []:
            if ca["instrument_id"] in wanted and days[0] <= ca["record_date"] <= days[-1]:
                actions.append(ca)
    check("公司行为带来源公告", all(a.get("source_announcement_id") for a in actions),
          f"{len(actions)} 条")
    # 快照里每一个被引用的 source_id 都必须在 source_registry 里。
    # 这条卡口来自一次真实失败：公司行为来自 cninfo，但只登记了 baostock，
    # 于是写证据时 document.source_id 的外键失败——报错点在很远的地方。
    registered = {r[0] for r in con.execute("SELECT source_id FROM source_registry")}
    referenced = {a["source_id"] for a in actions if a.get("source_id")}
    check("公司行为的来源已登记", referenced <= registered,
          f"已登记 {sorted(registered)}；被引用 {sorted(referenced)}")

    doc = {
        "schema_version": "aquant.real_dataset.v1",
        "data_mode": "PRODUCTION",
        "watermark": ("REAL MARKET DATA, RECONSTRUCTED POINT-IN-TIME -- "
                      "NOT VALID FOR FORMAL PIT BACKTESTS"),
        "disclaimer": ("行情与行业分类来自 BaoStock 免费接口，抓取发生在历史之后；"
                       "可得时点为重建而非当时观察，不得用于正式时点回测。"),
        "as_of_time": days[-1] + "T15:00:00+08:00",
        "input_cutoff_at": days[-1] + "T07:00:00Z",
        "published_at": datetime.now(timezone.utc).isoformat(),
        "trading_days": days,
        "instruments": instruments,
        "daily_quotes": quotes,
        "corporate_actions": actions,
        "events": [],
    }
    # --- 财务数据随快照冻结（因子落库的前提） ---
    #
    # 没有它，`compute_f10_for_snapshot` 只能把每只标的标成
    # "快照未包含财务数据"：F10 会一天不落地算出 0 个值，
    # 而流水线的每一步都报成功。财务按季度更新、抓取也慢，
    # 但**快照里必须有它**，否则研究卡上的数值无法回答
    # "这是哪个时点的财报"。
    fin_path = ROOT / "deploy" / "agentctl-q0" / "financials-cache.json"
    if fin_path.exists():
        fin_cache = json.loads(fin_path.read_text(encoding="utf-8"))
        pool_ids = {i["instrument_id"] for i in instruments}
        statements = {iid: periods
                      for iid, periods in (fin_cache.get("statements") or {}).items()
                      if iid in pool_ids}
        doc["financials"] = {
            "created_at": fin_cache.get("created_at"),
            "updated_at": fin_cache.get("updated_at"),
            "source_id": "baostock",
            "unit_notes": "netProfit 单位为元；totalShare 为股；均由 records.py 归一",
            "statements": statements,
        }
        check("财务数据覆盖池内标的 >= 90%",
              len(statements) >= len(pool_ids) * 0.9,
              f"{len(statements)}/{len(pool_ids)} 只（源：financials-cache.json）")
    else:
        check("财务缓存存在", False, f"缺少 {fin_path}，先跑 tools/collect_financials.py")

    report = builder.ingest(doc, source_id="baostock", data_version="universe")
    check("入库证券数正确", report.instruments == len(instruments),
          str(report.instruments))
    check("入库交易日数正确", report.trading_days == len(days), str(report.trading_days))
    check("入库公司行为数正确", report.corporate_actions == len(actions),
          str(report.corporate_actions))

    refs = builder.write_datasets(doc, snapshot_id=snapshot_id)
    store = SnapshotStore(con, api_root)

    def parse(ts: str) -> datetime:
        return datetime.fromisoformat(ts)

    store.publish(SnapshotDraft(
        snapshot_id=snapshot_id, kind="EOD", data_mode=DataMode.PRODUCTION,
        input_cutoff_at=parse(doc["input_cutoff_at"]),
        as_of_time=parse(doc["as_of_time"]),
        created_at=datetime.now(timezone.utc),
        published_at=parse(doc["published_at"]),
        code_version="0.1.0", data_version="universe",
        watermark=doc["watermark"],
        pool_hash="sha256:" + "c" * 64,
        datasets=[DatasetRef(name=r["name"], path=r["path"], sha256=r["sha256"],
                             record_count=r["record_count"],
                             as_of_upper_bound=parse(r["as_of_upper_bound"]))
                  for r in refs],
    ))
    pointer = write_current_pointer(out_dir, snapshot_id)
    record_current_snapshot(con, snapshot_id,
                            updated_at=datetime.fromisoformat(pointer.updated_at))
    check("快照已发布", True, snapshot_id)

    reader = SnapshotReader(store)
    as_of = parse(doc["as_of_time"])
    probe = instruments[0]["instrument_id"]
    rows = reader.daily_quotes(snapshot_id, as_of=as_of, instrument_id=probe)
    check("读取器可读", len(rows) > 0, f"{probe} {len(rows)} 条")
    listed = reader.instruments(snapshot_id, as_of=as_of)
    check("读取器返回全部证券", len(listed) == len(instruments), str(len(listed)))

    # --- 派生事实：开盘即涨停必须随快照冻结（§12.3 / S02）
    #
    # 这个字段原先只存在于示例 YAML 的手写行里，生产链路从不计算，
    # 读取器取默认 False，于是"开盘涨停不得假定买入成交"在真实数据上
    # 从未生效。这里用**独立算法**重算一遍并与快照里的标记对照，
    # 而不是只断言"字段存在"。
    limit_pct = {("SSE", "MAIN"): "10", ("SZSE", "MAIN"): "10",
                 ("SSE", "STAR"): "20", ("SZSE", "GEM"): "20"}
    board_of = {i["instrument_id"]: (i["exchange"], i["board"]) for i in instruments}
    expected: set[tuple[str, str]] = set()
    for q in quotes:
        pct = limit_pct.get(board_of[q["instrument_id"]])
        prev = q.get("prev_close_cents")
        if pct is None or not prev:
            continue
        # 涨停价 = 前收 + round(前收 × 涨跌幅)，四舍五入到分（与模拟器同口径）
        cap = prev + int((Decimal(prev) * Decimal(pct) / Decimal(100))
                         .quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        # 开盘价达到涨停价才算——涨停收盘但开盘更低时，开盘是可成交的
        if q["open_cents"] >= cap:
            expected.add((q["instrument_id"], q["trading_day"]))
    actual = {(q["instrument_id"], q["trading_day"])
              for q in quotes if q.get("board_limit_up")}
    check("快照冻结了开盘涨停标记", bool(expected),
          f"{len(expected)} 行开盘即涨停")
    check("涨停标记与独立重算一致", actual == expected,
          f"标记 {len(actual)} 行，重算 {len(expected)} 行，"
          f"差 {sorted(actual ^ expected)[:3]}")
    # 反向：被标记的行，开盘价必须**恰好等于**涨停价。
    #
    # 这里原先写的是"收盘价不低于开盘价"——那是错的判据：开盘一字涨停后
    # 回落（close < open）是常见走势，不是数据错误。而且它并不检验
    # "标记得对不对"，只检验"走势像不像一字板"。改成按规则精确核对：
    # 标记为真的行，open 必须等于按板块规则算出的涨停上限。
    mismatched: list[tuple[str, str, int, int]] = []
    for q in quotes:
        if not q.get("board_limit_up"):
            continue
        pct = limit_pct.get(board_of[q["instrument_id"]])
        prev = q.get("prev_close_cents")
        if pct is None or not prev:
            mismatched.append((q["instrument_id"], q["trading_day"],
                               q["open_cents"], -1))
            continue
        cap = prev + int((Decimal(prev) * Decimal(pct) / Decimal(100))
                         .quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if q["open_cents"] != cap:
            mismatched.append((q["instrument_id"], q["trading_day"],
                               q["open_cents"], cap))
    check("被标记的行开盘价恰好等于涨停价", not mismatched, str(mismatched[:3]))

    con.close()

    failed = [c for c in checks if not c[1]]
    total_rows = sum(len(v["rows"]) for v in bars_all.values())
    print()
    print("=" * 62)
    print(f"T10 全市场快照 {len(checks) - len(failed)}/{len(checks)} 通过")
    print(f"证券 {len(instruments)} 只（{boards}），行情 {len(quotes)} 条")
    print(f"输出 {out_dir}")
    for name, _, detail in failed:
        print("  - " + name + "  " + detail)

    out = ROOT / "deploy" / "agentctl-q0" / "t10-universe-snapshot.json"
    out.write_bytes(json.dumps({
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_id": snapshot_id,
        "pool_id": pool["pool_id"],
        "instruments": len(instruments),
        "boards": boards,
        "quotes": len(quotes),
        "window": {"first_day": days[0], "last_day": days[-1], "trading_days": len(days)},
        "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks],
        "conclusion": "PASS" if not failed else "FAIL",
    }, ensure_ascii=False, indent=2).encode("utf-8"))
    print(f"报告：{out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
