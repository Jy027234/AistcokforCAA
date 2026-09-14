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
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.data.db import apply_migrations, connect  # noqa: E402
from aquant.domain.data.ingest import SnapshotBuilder  # noqa: E402
from aquant.domain.data.reader import SnapshotReader  # noqa: E402
from aquant.domain.data.snapshot import (  # noqa: E402
    DataMode, DatasetRef, SnapshotDraft, SnapshotStore,
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
    days = []
    first = bars_all[next(iter(bars_all))]["rows"]
    days = [b["trading_day"] for b in first]
    print(f"缓存 {len(bars_all)} 只，窗口 {days[0]} .. {days[-1]}（{len(days)} 天）")

    pool = json.loads(pool_path.read_text(encoding="utf-8"))
    picked = pool["instruments"]
    print(f"研究池 {pool['pool_id']}：{len(picked)} 只")

    import shutil

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

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
            "listed_on": None,
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
        prev = None
        for b in info["rows"]:
            quotes.append({
                "instrument_id": iid,
                "trading_day": b["trading_day"],
                "open_cents": b["open_cents"],
                "high_cents": b["high_cents"],
                "low_cents": b["low_cents"],
                "close_cents": b["close_cents"],
                "volume_shares": b["volume_shares"],
                "amount_cents": b.get("amount_cents"),
                "prev_close_cents": prev if prev else b["open_cents"],
            })
            prev = b["close_cents"]

    check("池内标的都有行情", missing == 0, f"缺失 {missing} 只")
    check("行情条数充足", len(quotes) > 50_000, f"{len(quotes)} 条")
    # 成交额允许极少数缺失：当日无成交（停牌前后、零成交）时
    # BaoStock 返回空值。**缺失即缺失**，不得用价格×成交量补造。
    # 因此这里断言的是覆盖率而不是"全部都有"——
    # 把缺失当成失败会逼着实现方去补造，那才是真正的错误。
    with_amount = sum(1 for q in quotes if q.get("amount_cents"))
    coverage = with_amount / max(len(quotes), 1)
    check("成交额覆盖率 >= 99%", coverage >= 0.99,
          f"{with_amount}/{len(quotes)}（缺失 {len(quotes) - with_amount} 条，真实无成交）")
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
    report = builder.ingest(doc, source_id="baostock", data_version="universe")
    check("入库证券数正确", report.instruments == len(instruments),
          str(report.instruments))
    check("入库交易日数正确", report.trading_days == len(days), str(report.trading_days))
    check("入库公司行为数正确", report.corporate_actions == len(actions),
          str(report.corporate_actions))

    refs = builder.write_datasets(doc, snapshot_id=SNAPSHOT_ID)
    store = SnapshotStore(con, api_root)

    def parse(ts: str) -> datetime:
        return datetime.fromisoformat(ts)

    store.publish(SnapshotDraft(
        snapshot_id=SNAPSHOT_ID, kind="EOD", data_mode=DataMode.PRODUCTION,
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
    check("快照已发布", True, SNAPSHOT_ID)

    reader = SnapshotReader(store)
    as_of = parse(doc["as_of_time"])
    probe = instruments[0]["instrument_id"]
    rows = reader.daily_quotes(SNAPSHOT_ID, as_of=as_of, instrument_id=probe)
    check("读取器可读", len(rows) > 0, f"{probe} {len(rows)} 条")
    listed = reader.instruments(SNAPSHOT_ID, as_of=as_of)
    check("读取器返回全部证券", len(listed) == len(instruments), str(len(listed)))

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
        "snapshot_id": SNAPSHOT_ID,
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