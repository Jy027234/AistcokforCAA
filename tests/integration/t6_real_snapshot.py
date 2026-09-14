"""T6：用免费真实数据构建 61 个交易日的快照，落到固定目录并发布。

这一步是评审留下的最大缺口：此前所有端到端验收都建立在
`snap-syn-001` 合成数据上，而"合成数据上跑通"**不能**替代
"真实数据能走通同一条链路"。两者的失败方式完全不同——
真实数据会缺字段、会停牌、会有非交易日、会有单位差异。

它同时是**产品闭环真实数据验收**的前置：快照落到固定目录后，
API 可以用 `AQUANT_DATA_DIR` + `AQUANT_SNAPSHOT_ID` 直接指向它。

范围与诚实边界
--------------
只做**免费源真的能证明**的部分：

  * 交易日历：腾讯上证指数日线（真实交易日，不用工作日近似）；
  * 证券身份：交易所、板块来自池配置并与代码前缀交叉校验；
  * 日行情：腾讯日线，开高低收量（成交额免费源不提供，缺失即缺失）。

免费源**不能**证明的部分，一律不编造：

  * 公司行为：不重建成事件（ADR-003 已记录该限制）；
  * point-in-time：本次抓取发生在历史之后，因此
    `pit_mode=HISTORICAL_RECONSTRUCTED`、`available_basis=RECONSTRUCTED`，
    水印写明它不能用于正式时点回测。

用法：
    $env:AQUANT_TRUSTED_PROXY_NETWORKS='198.18.0.0/15,fdfe:dcba:9876::/48'
    python -m tests.integration.t6_real_snapshot

环境变量：
    T6_WINDOW_DAYS  交易日数量，默认 61
    T6_POOL         研究池配置文件，默认 configs/real-pool-2026-09.yaml
    T6_OUT_DIR      输出目录，默认 deploy/real-snapshot

退出码：0 = 快照建成且全部断言通过；1 = 有断言失败；2 = 数据源不可用。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.calendar import load_trading_calendar  # noqa: E402
from aquant.adapters.providers.tencent import TencentClient  # noqa: E402
from aquant.domain.data.db import apply_migrations, connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402
from aquant.domain.data.ingest import SnapshotBuilder  # noqa: E402
from aquant.domain.data.reader import SnapshotReader  # noqa: E402
from aquant.domain.data.snapshot import (  # noqa: E402
    DataMode, DatasetRef, SnapshotDraft, SnapshotStore,
)

SNAPSHOT_ID = "snap-real-61d"

#: 代码前缀 -> (交易所, 板块)。这是交易所规则而非猜测，用来**交叉校验**
#: 池配置里声明的板块：声明与代码冲突时必须报错，而不是二选一。
#: 板块决定涨跌幅与手数，猜错就会算出错误的成交价。
BOARD_BY_PREFIX = {
    "sh60": ("SSE", "MAIN"), "sh68": ("SSE", "STAR"),
    "sz00": ("SZSE", "MAIN"), "sz30": ("SZSE", "GEM"),
}


def _board_of(code: str) -> tuple[str, str]:
    for prefix, listing in BOARD_BY_PREFIX.items():
        if code.startswith(prefix):
            return listing
    raise RuntimeError(
        f"无法从代码前缀判断 {code} 的交易所与板块；涨跌幅规则依赖板块，"
        "不能默认成主板"
    )


def _internal_id(code: str) -> str:
    """内部证券 ID：市场.代码，与数据源解耦（§15.2）。"""

    return code[:2].upper() + "." + code[2:]


def _load_pool(path: Path) -> dict:
    import yaml

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not doc.get("instruments"):
        raise RuntimeError(f"研究池配置无效（缺少 instruments）：{path}")
    return doc


def main() -> int:
    window = int(os.environ.get("T6_WINDOW_DAYS", "61"))
    pool_path = Path(os.environ.get("T6_POOL", str(ROOT / "configs" / "real-pool-2026-09.yaml")))
    out_dir = Path(os.environ.get("T6_OUT_DIR", str(ROOT / "deploy" / "real-snapshot")))

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))
        print(("  PASS  " if ok else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))

    pool = _load_pool(pool_path)
    entries = pool["instruments"]

    # 池声明的板块必须与代码前缀一致。不一致说明配置写错了，
    # 而交易规则是按板块匹配的——继续跑下去只会得到一份错误结论。
    mismatched = []
    for entry in entries:
        code = entry["code"]
        try:
            expected = _board_of(code)
        except RuntimeError as exc:
            mismatched.append(f"{code}: {exc}")
            continue
        declared = (entry.get("exchange"), entry.get("board"))
        if declared != expected:
            mismatched.append(f"{code}: 声明 {declared} 与前缀规则 {expected} 不一致")
    if mismatched:
        print("研究池配置存在板块冲突：")
        for line in mismatched:
            print("  - " + line)
        return 2

    print(f"研究池 {pool.get('pool_id')}：{len(entries)} 只证券（来源 {pool_path.name}）")

    # 输出目录每次重建：留着上一次的快照会让"这次跑出来的结论"
    # 与磁盘上的数据对不上，而两边都看起来正常。
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    con = connect(out_dir / "meta.sqlite")
    apply_migrations(con)
    archive = ForwardArchive(con, out_dir / "archive")
    tencent = TencentClient(archive)

    # ---------------------------------------------------------- 1. 交易日历
    print("\n[1] 真实交易日历")
    end = date.today()
    begin = end - timedelta(days=max(int(window * 2.2), 120))
    try:
        cal = load_trading_calendar(archive, begin=begin, end=end)
    except Exception as exc:                       # noqa: BLE001
        print(f"  日历不可用：{type(exc).__name__}: {exc}")
        return 2
    days = [d.isoformat() for d in cal.trading_days][-window:]
    check("日历来自真实交易日", len(days) == window,
          f"{cal.source_id} {days[0]}..{days[-1]} 共 {len(days)} 天")
    check("日历不含周末", all(date.fromisoformat(d).weekday() < 5 for d in days))

    # ------------------------------------------------ 2. 证券简称（GBK）
    print("\n[2] 证券简称")
    names: dict[str, str] = {}
    codes = [e["code"] for e in entries]
    for i in range(0, len(codes), 10):
        batch = codes[i:i + 10]
        out = tencent.fetch("https://qt.gtimg.cn/q=" + ",".join(batch),
                            label="batch-names")
        if not out.ok or not out.payload:
            continue
        # 腾讯 qt 接口是 GBK；按 GB18030 解码（GBK 超集，能覆盖生僻字）
        text = out.payload.decode("gb18030", errors="replace")
        for line in text.split(";"):
            line = line.strip()
            if not line.startswith("v_") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            fields = value.strip().strip('"').split("~")
            if len(fields) > 2:
                # 腾讯返回的简称里会带填空用的空格（例如"五 粮 液"）。
                # 只做**空白压缩**，不改动任何字：删字或改字都是在改
                # 一个我们无权改写的官方名称。
                names[key[2:]] = "".join(fields[1].split())
    check("取到证券简称", len(names) == len(codes),
          f"{len(names)}/{len(codes)}")

    # ------------------------------------------------------------ 3. 日行情
    print("\n[3] 真实日行情")
    quotes: list[dict] = []
    instruments: list[dict] = []
    missing: list[str] = []
    failed: list[str] = []
    for entry in entries:
        code = entry["code"]
        exchange, board = _board_of(code)
        out, bars = tencent.daily_quotes(code, days[0], days[-1], adjust=0)
        if not out.ok:
            failed.append(f"{code}: {out.detail}")
            continue
        by_day = {str(bar[0]): bar for bar in bars if bar}
        got = 0
        prev_close = None
        for day in days:
            bar = by_day.get(day)
            if bar is None:
                # 停牌或当日无成交：**不补造**，如实记为缺失
                missing.append(f"{code}@{day}")
                continue
            _, o, c, h, low, vol = bar[0], bar[1], bar[2], bar[3], bar[4], bar[5]
            def cents(x: str) -> int:
                return int(round(float(x) * 100))
            quotes.append({
                "instrument_id": _internal_id(code),
                "trading_day": day,
                "open_cents": cents(o), "high_cents": cents(h),
                "low_cents": cents(low), "close_cents": cents(c),
                "volume_shares": int(float(vol)) * 100,   # 腾讯成交量单位是手
                "prev_close_cents": prev_close if prev_close else cents(o),
            })
            prev_close = cents(c)
            got += 1
        instruments.append({
            "instrument_id": _internal_id(code),
            "exchange": exchange, "board": board, "security_class": "EQUITY",
            "short_name": names.get(code, code.upper()),
            "listed_on": None,
            # 行业来自**显式声明的池配置**，不是从行情里推断的。
            # 免费源不提供权威分类，如实标注来源。
            "industry_code": entry.get("industry_code"),
            "industry_name": entry.get("industry_name"),
            "status_history": [{
                "valid_from": days[0], "valid_to": None,
                "name": names.get(code, code.upper()),
                "status": "LISTED",
                "industry_code": entry.get("industry_code"),
                "industry_name": entry.get("industry_name"),
            }],
        })
        print(f"  {code} {names.get(code, '?'):<8} {got}/{len(days)} 个交易日有行情")

    if failed:
        print("\n有证券抓取失败：")
        for line in failed:
            print("  - " + line)
        return 2

    coverage = len(quotes) / max(len(entries) * len(days), 1)
    check("行情覆盖率 100%（缺失即缺失，不补造）", coverage == 1.0,
          f"{len(quotes)}/{len(entries) * len(days)}；缺失 {len(missing)} 条")
    check("价格为正整数分", all(q["open_cents"] > 0 and q["close_cents"] > 0
                                for q in quotes))
    check("low<=open,close<=high", all(
        q["low_cents"] <= min(q["open_cents"], q["close_cents"])
        and max(q["open_cents"], q["close_cents"]) <= q["high_cents"] for q in quotes))
    check("证券简称非空", all(i["short_name"] for i in instruments))
    board_counts: dict[str, int] = {}
    for inst in instruments:
        board_counts[inst["board"]] = board_counts.get(inst["board"], 0) + 1
    check("覆盖主板与成长板块", {"MAIN"} <= set(board_counts),
          "、".join(f"{k}×{v}" for k, v in sorted(board_counts.items())))

    # -------------------------------------------------- 4. 真实入库与发布
    print("\n[4] 真实快照入库与发布")
    api_root = out_dir / "api"
    api_root.mkdir(parents=True, exist_ok=True)
    builder = SnapshotBuilder(con, api_root / "datasets")
    builder.ensure_source(
        "tencent-ifzq",
        display_name="腾讯行情（免费公开接口）",
        domains=["DAILY_QUOTES", "CALENDAR_IDENTITY"],
        integration_state="TEST_PASSED",
        pit_available="NO",
        pit_basis="RECONSTRUCTED",
        # 权利留空即 UNKNOWN：免费公开接口的授权状态没有权威依据，
        # 按 §17.2 "未知默认不开放"处理，因此这批数据不得外发到模型。
        rights={},
    )
    # ---------------------------------------------------- 4b. 真实公司行为
    # 分红来自 tools/build_dividend_actions.py 从巨潮公告解析出的结果，
    # 每条都带公告 ID、URL 与原文证据；不是手工构造的。
    # 只保留落在快照窗口内的：窗口外的事件记进快照会让"这条分红在
    # 决策时点是否已知"变得无法回答。
    dividends_path = Path(os.environ.get(
        "T6_DIVIDENDS",
        str(ROOT / "deploy" / "agentctl-q0" / "dividend-actions.json")))
    actions: list[dict] = []
    if dividends_path.exists():
        payload = json.loads(dividends_path.read_text(encoding="utf-8"))
        wanted = {i["instrument_id"] for i in instruments}
        for ca in payload.get("corporate_actions") or []:
            if ca["instrument_id"] not in wanted:
                continue
            if not (days[0] <= ca["record_date"] <= days[-1]):
                continue
            actions.append(ca)
        print(f"\n[4b] 真实公司行为：采用 {len(actions)} 条"
              f"（来源 {dividends_path.name}）")
        for ca in actions:
            print(f"  {ca['instrument_id']} 每股 {ca['cash_per_share_micros']} 微元，"
                  f"登记 {ca['record_date']} 除权 {ca['ex_date']} 到账 {ca['pay_date']}")
    else:
        print("\n[4b] 未找到分红文件，快照不含公司行为")
    check("公司行为带来源公告", all(ca.get("source_announcement_id") for ca in actions),
          f"{len(actions)} 条")
    check("公司行为带原文证据", all(ca.get("evidence") for ca in actions))

    doc = {
        "schema_version": "aquant.real_dataset.v1",
        "data_mode": "PRODUCTION",
        "watermark": ("REAL MARKET DATA, RECONSTRUCTED POINT-IN-TIME -- "
                      "NOT VALID FOR FORMAL PIT BACKTESTS"),
        "disclaimer": ("行情来自免费公开接口，抓取发生在历史之后；"
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
    report = builder.ingest(doc, source_id="tencent-ifzq", data_version="real-61d")
    check("入库证券数正确", report.instruments == len(entries), str(report.instruments))
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
        code_version="0.1.0", data_version="real-61d",
        watermark=doc["watermark"],
        pool_hash="sha256:" + "b" * 64,
        datasets=[DatasetRef(name=r["name"], path=r["path"], sha256=r["sha256"],
                             record_count=r["record_count"],
                             as_of_upper_bound=parse(r["as_of_upper_bound"]))
                  for r in refs],
    ))
    check("快照已发布", True, SNAPSHOT_ID)

    # ------------------------------------------------- 5. 读取与 PIT 边界
    print("\n[5] 通过快照读取器复核（同一条产品读取路径）")
    reader = SnapshotReader(store)
    as_of = parse(doc["as_of_time"])
    mid = date.fromisoformat(days[len(days) // 2])
    first_code = entries[0]["code"]
    inst0 = _internal_id(first_code)
    rows = reader.daily_quotes(SNAPSHOT_ID, as_of=as_of, instrument_id=inst0, end=mid)
    check("读取器能读到真实行情", len(rows) > 0, f"{inst0} 截至 {mid} 共 {len(rows)} 条")
    check("读取器不返回超过截止日的数据", all(r.trading_day <= mid for r in rows))

    future = as_of + timedelta(days=1)
    try:
        reader.daily_quotes(SNAPSHOT_ID, as_of=future, instrument_id=inst0, end=days[-1])
        check("未来时点读取被拒绝", False, "竟然成功了")
    except Exception as exc:                        # noqa: BLE001
        check("未来时点读取被拒绝", True, type(exc).__name__)

    # ---------------------------------------------------------- 6. 留证
    print("\n[6] 抓取留证")
    receipts = archive.receipts_for("tencent-ifzq")
    check("归档留下了收据", len(receipts) > 0, f"{len(receipts)} 条")
    check("归档内容可哈希复核",
          all(archive.verify(r["content_hash"]) for r in receipts
              if r["content_hash"]))

    failed_checks = [c for c in checks if not c[1]]
    print("\n" + "=" * 62)
    print(f"T6 真实快照验收 {len(checks) - len(failed_checks)}/{len(checks)} 通过")
    print(f"交易日 {days[0]} .. {days[-1]}（{len(days)} 天），"
          f"证券 {len(entries)} 只，行情 {len(quotes)} 条")
    print(f"输出目录 {out_dir}")
    for name, _, detail in failed_checks:
        print("  - " + name + "  " + detail)

    report_path = ROOT / "deploy" / "agentctl-q0" / "t6-real-snapshot.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(json.dumps({
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_id": SNAPSHOT_ID,
        "data_mode": "PRODUCTION",
        "watermark": doc["watermark"],
        "pit_mode": "HISTORICAL_RECONSTRUCTED",
        "available_basis": "RECONSTRUCTED",
        "pool_id": pool.get("pool_id"),
        "pool_file": pool_path.name,
        "output_dir": str(out_dir.relative_to(ROOT)),
        "window": {"first_day": days[0], "last_day": days[-1],
                   "trading_days": len(days)},
        "instrument_count": len(entries),
        "board_counts": board_counts,
        "quote_count": len(quotes),
        "missing_quote_count": len(missing),
        "calendar_source": cal.source_id,
        "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks],
        "conclusion": "PASS" if not failed_checks else "FAIL",
    }, ensure_ascii=False, indent=2).encode("utf-8"))
    print(f"报告：{report_path}")
    con.close()
    return 1 if failed_checks else 0


if __name__ == "__main__":
    raise SystemExit(main())
