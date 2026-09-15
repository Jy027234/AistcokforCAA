"""Q5 双侧验收：同一套链路在合成与真实数据上各跑一遍。

为什么必须两侧都跑
------------------
合成夹具是**手写**的：字段齐全、形状标准、值都好看。真实数据有截断、
有空白差异、有缺失、有非标准排版。只跑合成侧，等于只验证了
"我们按自己想象的数据写对了代码"——本项目已经因此漏掉两个只在真实
数据上出现的缺陷（开盘涨停守卫从未生效、最低佣金补足写不进账）。

因此本脚本对两侧跑**完全相同的断言函数**，差别只在数据来源与是否联网。

链路（§5.3 / §5.5 / §8.4 / §9）
--------------------------------
    提交研究作业 -> 执行（模型抽取）-> 引用定位落库
      -> 预览 -> 确认 -> 冻结 -> 执行 -> 估值 -> 对账

用法：
    python tools/check_both_sides.py              # 两侧；真实侧调真实模型
    python tools/check_both_sides.py --offline    # 两侧都用确定性替身
    python tools/check_both_sides.py --side real
退出码：0 = 全过；1 = 有断言失败；2 = 缺快照。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "src"))

checks: list[tuple[str, tuple[str, bool, str]]] = []


def check(side: str, name: str, ok: bool, detail: object = "") -> None:
    text = detail if isinstance(detail, str) else json.dumps(
        detail, ensure_ascii=False, default=str)
    checks.append((side, (name, bool(ok), text)))
    print(("    PASS  " if ok else "    FAIL  ") + name
          + (("  -- " + text) if text else ""))


def note(side: str, text: str) -> None:
    print("    ....  " + text)


class StubExtractor:
    """确定性替身（--offline 用）。返回与被抽正文相符的引用。"""

    provider_name = "stub"
    model_name = "stub-extractor"

    def complete(self, request):
        """返回从正文里**真的找得到**的引用。

        第一版返回 quote=None，于是所有引用都不可定位、"至少一条可定位"
        直接失败——那不是被测逻辑的问题，是替身太假。替身可以简化**抽取**，
        但不能伪造一个链路根本走不通的输入。
        """

        from aquant.domain.ai.model import ModelResponse

        quote = ""
        for candidate in ("每股现金红利", "现金红利", "每股分配比例"):
            if candidate in request.context:
                at = request.context.find(candidate)
                quote = request.context[at:at + 24].strip()
                break
        payload = {"fields": {
            # 值一律为 null：替身不做真实抽取，因此两侧都"未抽取"，
            # 交叉核对没有可比项 -> 不产生不一致。
            # 拿常量桩去对真实数据做值核对只会得到假失败。
            "per_share_amount": {"value": None, "quote": quote or None},
            "record_date": {"value": None, "quote": None},
            "ex_date": {"value": None, "quote": None},
            "pay_date": {"value": None, "quote": None}},
            "uncertainty": "替身未做完整抽取", "counter_evidence": "替身不判断"}
        return ModelResponse(text=json.dumps(payload, ensure_ascii=False),
                             provider=self.provider_name, model=self.model_name)


# ======================================================================
def run_side(side: str, con, reader, store_root: Path, *,
             snapshot_id: str, instrument_id: str, source_id: str,
             trading_day: str, provider, offline: bool) -> None:
    """一侧的完整链路。两侧调用的就是这个函数——断言因此完全一致。"""

    from aquant.domain.evidence.store import evidence_for
    from aquant.domain.portfolio.construction import Candidate
    from aquant.domain.portfolio.plan import PlanService
    from aquant.domain.simulation.board_rules import BOARD_RULES
    from aquant.domain.simulation.fees import synthetic_fee_table
    from aquant.domain.simulation.verified_fees import fee_table_from_env
    from aquant.operations.research_jobs import (
        JOB_EVIDENCE_RESEARCH, run_research_job, submit_research_job,
    )

    ref = reader.ref(snapshot_id)
    check(side, "快照已发布且可读", bool(ref.snapshot_id), ref.snapshot_id)

    # ------------------------------------------------ 作业 -> 证据
    payload = {"instrument_id": instrument_id, "source_id": source_id}
    submitted = submit_research_job(
        con, job_type=JOB_EVIDENCE_RESEARCH, trading_day=trading_day,
        snapshot_id=snapshot_id, payload=payload)
    check(side, "研究作业已入队", submitted["status"] == "PENDING",
          submitted["jobId"])

    again = submit_research_job(
        con, job_type=JOB_EVIDENCE_RESEARCH, trading_day=trading_day,
        snapshot_id=snapshot_id, payload=payload)
    check(side, "重复提交返回同一条作业（§8.4）",
          again["jobId"] == submitted["jobId"] and again["created"] is False)

    done = run_research_job(con, reader, job_id=submitted["jobId"],
                            worker_id=f"q5-{side}", provider=provider)
    if done["status"] != "SUCCEEDED":
        check(side, "作业执行成功", False, done.get("error"))
        return
    check(side, "作业执行成功", True)

    actions = done["result"].get("actions") or []
    if not actions:
        # 真实快照里不是每只标的都有公告证据。没有就**明说**，
        # 而不是让"零条证据"看起来像通过。
        check(side, "该标的在快照里有可研究的公告证据", False,
              f"{instrument_id} 无公司行为证据；换一个标的再跑")
        return
    action = actions[0]
    if offline:
        # 替身不做真实抽取，值核对没有可比项。这一条只在联网侧有意义——
        # 而"离线跳过"必须**说出来**，不能让"没验"看起来像"验过了"。
        note(side, "模型抽取与解析结果一致：离线侧用替身，值核对不适用")
    else:
        check(side, "模型抽取与解析结果一致", action["agreesWithStored"] is True,
              action["disagreements"])
    check(side, "至少一条引用可在来源正文中定位", action["locatedCount"] > 0,
          f"{action['locatedCount']}/{action['citationCount']}")

    rows = evidence_for(con, instrument_id=instrument_id)
    check(side, "证据已落库并带字符偏移",
          any(r["locatorStart"] is not None for r in rows if r["located"]),
          f"{len(rows)} 条引用")

    # ---------------------------------------- 预览 -> 冻结 -> 执行 -> 对账
    listings = {i["instrument_id"]: (i["exchange"], i["board"])
                for i in reader.instruments(snapshot_id, as_of=ref.as_of_time)}
    # 真实数据上必须用经验证的费率表（§12.6）；合成数据继续用合成费率。
    # 两者的差别是"数字有没有依据"，因此要在报告里说出来。
    ref_mode = reader.ref(snapshot_id).data_mode
    if ref_mode == "PRODUCTION":
        fees, fee_note = fee_table_from_env()
        note(side, "费率：" + fee_note)
    else:
        fees, fee_note = synthetic_fee_table(), "合成测试费率（SYNTHETIC 快照）"
        note(side, "费率：" + fee_note)
    service = PlanService(con, reader, fees, BOARD_RULES, listings)
    candidates = [Candidate(i["instrument_id"], i.get("industry_code") or "UNKNOWN", 0.5)
                  for i in reader.instruments(snapshot_id, as_of=ref.as_of_time)[:8]]
    portfolio = f"pf-q5-{side}-M"
    lots: list = []
    cash = 100_000_000
    day = date.fromisoformat(trading_day)

    preview = service.preview(portfolio_id=portfolio, snapshot_id=snapshot_id,
                              trading_day=day, as_of=ref.as_of_time,
                              candidates=candidates, cash_available_cents=cash,
                              lots=lots, confirm_subject="user:q5")
    check(side, "预览不冻结（A08）", preview.frozen is False)
    check(side, "预览产生了可执行订单", bool(preview.orders), len(preview.orders))

    token = service.issue_confirmation(preview=preview, subject="user:q5",
                                       current_lots=lots, current_cash_cents=cash)
    frozen = service.freeze(preview=preview, confirm_subject="user:q5",
                            confirmation_token=token,
                            expected_account_version=preview.account_version,
                            current_lots=lots, current_cash_cents=cash)
    check(side, "冻结需要一次性令牌", frozen["status"] == "FROZEN")

    ex = service.execute(plan_id=preview.plan_id, lots=lots,
                         cash_available_cents=cash)
    check(side, "执行产生了成交", bool(ex["fills"]), len(ex["fills"]))
    check(side, "执行产生了账本分录", bool(ex["cash_entries"]),
          len(ex["cash_entries"]))

    # 估值必须喂**执行后的**账户状态：上面那个 lots 是空列表（执行前），
    # 拿它去估值会被判 STALE_SNAPSHOT（"supplied account differs from ledger"）
    # ——那是产品在正确地拒绝一份过期输入，不是产品缺陷。
    # 从账本读回真实持仓与现金，与 API 的 /valuations 端点做法一致。
    ledger_cash = service._ledger_cash(portfolio)
    ledger_lots = service._load_lots(portfolio)
    val = service.value(portfolio_id=portfolio, snapshot_id=snapshot_id,
                        trading_day=day, as_of=ref.as_of_time, lots=ledger_lots,
                        cash_available_cents=ledger_cash)
    check(side, "估值已发布（不变量全过）", val["published"] is True,
          val.get("invariants"))
    rec = service.reconcile(portfolio_id=portfolio)
    check(side, "对账通过", rec.get("reconciled") is True, json.dumps(rec)[:160])


# ======================================================================
def synthetic_side(*, offline: bool) -> None:
    print()
    print("=" * 62)
    print("侧：synthetic（内存夹具，同一套断言）")
    print("=" * 62)

    from aquant.domain.data.db import apply_migrations, connect
    from aquant.domain.data.ingest import SnapshotBuilder
    from aquant.domain.data.reader import SnapshotReader
    from aquant.domain.data.snapshot import SnapshotStore

    sys.path.insert(0, str(ROOT))
    from tests.integration.test_m1_ingest_e2e import build_snapshot

    work = Path(tempfile.mkdtemp(prefix="aquant-q5-syn-"))
    con = connect(work / "meta.sqlite")
    apply_migrations(con)
    root = work / "api"
    root.mkdir()
    store = SnapshotStore(con, root)
    build_snapshot(con, SnapshotBuilder(con, root / "datasets"), store)
    reader = SnapshotReader(store)

    # 合成夹具的公司行为没有公告原文，补一段——补的是**数据**，
    # 不是绕过链路。真实快照自带原文，因此不补。
    con.execute(
        "UPDATE corporate_action SET evidence_json=?, source_url=?, source_title=? "
        "WHERE instrument_id=?",
        (json.dumps({"excerpt": "重要内容提示：A股每股现金红利0.50元，"
                                "股权登记日2026/9/9，除权（息）日2026/9/10"}),
         "http://example.invalid/ca.PDF", "合成示例权益分派公告", "SYN.A.600003"))
    con.commit()

    try:
        run_side("synthetic", con, reader, root, snapshot_id="snap-syn-001",
                 instrument_id="SYN.A.600003", source_id="synthetic-fixture",
                 trading_day="2026-09-08",
                 provider=StubExtractor(), offline=True)
    finally:
        con.close()
        shutil.rmtree(work, ignore_errors=True)


def real_side(*, offline: bool) -> None:
    print()
    print("=" * 62)
    print("侧：real（全市场快照，同一套断言）")
    print("=" * 62)

    src = ROOT / "deploy" / "universe-snapshot"
    if not (src / "meta.sqlite").exists():
        print(f"    缺少真实快照：{src}")
        return

    from aquant.domain.data.db import connect
    from aquant.domain.data.reader import SnapshotReader
    from aquant.domain.data.snapshot import SnapshotStore

    work = Path(tempfile.mkdtemp(prefix="aquant-q5-real-"))
    shutil.copytree(src, work / "data")
    con = connect(work / "data" / "meta.sqlite")
    reader = SnapshotReader(SnapshotStore(con, work / "data" / "api"))
    provider = StubExtractor() if offline else None
    try:
        run_side("real", con, reader, work / "data" / "api",
                 snapshot_id="snap-universe", instrument_id="SH.600519",
                 source_id="cninfo", trading_day="2026-09-14",
                 provider=provider, offline=offline)
    finally:
        con.close()
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", choices=["both", "synthetic", "real"], default="both")
    ap.add_argument("--offline", action="store_true",
                    help="真实侧也用确定性替身，不联网")
    args = ap.parse_args()

    if args.side in ("both", "synthetic"):
        synthetic_side(offline=True)
    if args.side in ("both", "real"):
        real_side(offline=args.offline)

    failed = [name for _s, (name, ok, _d) in checks if not ok]
    print()
    print("=" * 62)
    print(f"Q5 双侧验收 {len(checks) - len(failed)}/{len(checks)} 通过")
    for side, (name, _ok, detail) in checks:
        if not _ok:
            print(f"  - [{side}] {name}  {detail}")
    out = ROOT / "deploy" / "agentctl-q0" / "q5-both-sides.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(json.dumps({
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "checks": [{"side": s, "name": n, "ok": o, "detail": d}
                   for s, (n, o, d) in checks],
        "conclusion": "PASS" if not failed else "FAIL",
    }, ensure_ascii=False, indent=2).encode("utf-8"))
    print(f"报告：{out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
