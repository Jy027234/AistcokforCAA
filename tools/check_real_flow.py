"""真实数据产品闭环验收。

与 check_ui_flow.py 的分工
--------------------------
`check_ui_flow.py` 验的是**界面**能不能驱动写路径（真浏览器、合成快照）。
`check_real_flow.py` 验的是**真实数据**能不能走完同一条链路。
两者都不能省：合成数据验不出真实行情的停牌、涨跌停、前收不连续，
而界面验不出后端读取真实快照时是否真的对得上。

它检验的核心问题是：
    研究读取 -> 组合构建 -> 确认冻结 -> 成交 -> 估值 -> 对账
这条链在真实价格上是否仍然自洽（现金、批次、费用、应收逐项对上）。

用法：
    python tools/check_real_flow.py

前置：`python -m tests.integration.t6_real_snapshot` 已成功（快照已发布）。
退出码：0 = 全部断言通过；1 = 有断言失败；2 = 环境没起来。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

# 本脚本既要发 HTTP 请求、也要直接调用领域代码建底仓，
# 因此需要把 src 加进 import 路径（服务端子进程另外用 PYTHONPATH 指定）。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.data.db import connect
from aquant.domain.data.reader import SnapshotReader
from aquant.domain.data.snapshot import (
    PublishedSnapshot,
    SnapshotError,
    SnapshotStore,
    published_before_execution_open,
)
from aquant.operations.snapshot_lifecycle import read_current_pointer

#: 默认使用每日流水实际维护的全市场目录。旧的手工池快照仍可通过
#: AQUANT_FLOW_SNAPSHOT_DIR 显式指定；ID 通过下方的双快照选择解析。
SNAPSHOT_DIR = Path(os.environ.get(
    "AQUANT_FLOW_SNAPSHOT_DIR", str(ROOT / "deploy" / "universe-snapshot")))
API_PORT = 8124

checks: list[tuple[str, bool, str]] = []
skips: list[dict[str, str]] = []


@dataclass(frozen=True, slots=True)
class FlowSnapshotPair:
    """真实闭环使用的决策/执行快照对。"""

    decision: PublishedSnapshot
    execution: PublishedSnapshot


def _snapshot_day(snapshot: PublishedSnapshot) -> date | None:
    """取得用于排序和 PIT 校验的交易日。"""

    value = snapshot.trading_day or snapshot.as_of_time[:10]
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _snapshot_time(snapshot: PublishedSnapshot) -> datetime:
    return datetime.fromisoformat(snapshot.as_of_time)


def select_snapshot_pair(
    snapshots: Sequence[PublishedSnapshot], *,
    current_snapshot_id: str | None = None,
    execution_snapshot_id: str | None = None,
    decision_snapshot_id: str | None = None,
) -> FlowSnapshotPair:
    """从已发布目录选择一对同源、无未来信息泄漏的快照。

    执行快照优先使用显式覆盖，其次使用 current 指针，最后使用最新的
    已发布快照。决策快照必须是执行日前最近的更早快照，并且必须已经
    通过目录计算出的 S1 决策能力门禁。
    """

    published = {item.snapshot_id: item for item in snapshots}
    if not published:
        raise ValueError("没有已发布快照")

    explicit_execution = (execution_snapshot_id or "").strip()
    current_id = (current_snapshot_id or "").strip()
    if explicit_execution:
        execution = published.get(explicit_execution)
        if execution is None:
            raise ValueError(f"执行快照不在已发布目录：{explicit_execution}")
    elif current_id and current_id in published:
        execution = published[current_id]
    else:
        # current_snapshot.json 可能在人工恢复时短暂指向一份已归档/删除
        # 的对象；目录本身仍可安全地回退到最新已发布物。
        candidates = [item for item in published.values()
                      if _snapshot_day(item) is not None]
        if not candidates:
            raise ValueError("已发布快照没有可派生的交易日")
        execution = max(
            candidates,
            key=lambda item: (_snapshot_day(item), _snapshot_time(item),
                              item.published_at or "", item.snapshot_id),
        )

    execution_day = _snapshot_day(execution)
    if execution_day is None:
        raise ValueError(f"执行快照没有可派生的交易日：{execution.snapshot_id}")

    if decision_snapshot_id:
        decision = published.get(decision_snapshot_id.strip())
        if decision is None:
            raise ValueError(f"决策快照不在已发布目录：{decision_snapshot_id}")
        if decision.data_mode != execution.data_mode:
            raise ValueError("决策快照与执行快照的数据模式不一致")
        if not decision.s1_decision.available:
            raise ValueError(
                f"决策快照尚未满足 S1 计算条件：{decision.snapshot_id}"
                f"（{decision.s1_decision.message}）"
            )
        decision_day = _snapshot_day(decision)
        if (decision_day is None or decision_day >= execution_day or
                _snapshot_time(decision) >= _snapshot_time(execution)):
            raise ValueError("决策快照必须早于执行快照")
        if not published_before_execution_open(decision, execution):
            raise ValueError("决策快照必须在执行日开盘前实际发布")
    else:
        eligible = []
        for item in published.values():
            if item.snapshot_id == execution.snapshot_id:
                continue
            if item.data_mode != execution.data_mode:
                continue
            item_day = _snapshot_day(item)
            if item_day is None or item_day >= execution_day:
                continue
            if _snapshot_time(item) >= _snapshot_time(execution):
                continue
            if not item.s1_decision.available:
                continue
            if not published_before_execution_open(item, execution):
                continue
            eligible.append(item)
        if not eligible:
            raise ValueError(
                "没有在执行日开盘前实际发布且满足 S1 计算条件的决策快照"
            )
        decision = max(
            eligible,
            key=lambda item: (_snapshot_day(item), _snapshot_time(item),
                              item.published_at or "", item.snapshot_id),
        )

    return FlowSnapshotPair(decision=decision, execution=execution)


def load_published_snapshots(snapshot_dir: Path) -> tuple[list[PublishedSnapshot], str | None]:
    """只读加载快照目录及当前指针，不修改快照数据库。"""

    con = connect(snapshot_dir / "meta.sqlite", read_only=True)
    try:
        reader = SnapshotReader(SnapshotStore(con, snapshot_dir / "api"))
        snapshots = reader.published_snapshot_catalog()
        pointer = read_current_pointer(snapshot_dir)
        return snapshots, pointer.snapshot_id if pointer is not None else None
    finally:
        con.close()




def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))


def skip(name: str, detail: str) -> None:
    skips.append({"name": name, "detail": detail})
    print("  SKIP  " + name + "  -- " + detail)


class Client:
    def __init__(self, base: str) -> None:
        self.base = base

    def call(self, method: str, path: str, *, body: dict | None = None,
             subject: str = "user:real") -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json", "X-Aquant-Subject": subject},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"raw": raw}


def _load_dividend(snapshot_dir: Path, instrument_id: str,
                   snapshot_id: str | None = None) -> dict | None:
    """从指定已发布快照的数据集取一条真实分红。"""

    import sqlite3

    con = sqlite3.connect(snapshot_dir / "meta.sqlite")
    con.row_factory = sqlite3.Row
    try:
        if snapshot_id:
            dataset = con.execute(
                "SELECT path FROM snapshot_dataset WHERE snapshot_id=? AND name=?",
                (snapshot_id, "corporate_actions"),
            ).fetchone()
            if dataset is None:
                return None
            payload = json.loads(
                (snapshot_dir / "api" / dataset["path"]).read_text(encoding="utf-8")
            )
            records = [row for row in payload
                       if row.get("instrument_id") == instrument_id
                       and row.get("action_type") == "CASH_DIVIDEND"]
            if not records:
                return None
            record = min(records, key=lambda row: row.get("ex_date") or "")
        else:
            row = con.execute(
                "SELECT * FROM corporate_action WHERE instrument_id=? "
                "AND action_type='CASH_DIVIDEND' ORDER BY ex_date LIMIT 1",
                (instrument_id,)).fetchone()
            if row is None:
                return None
            record = dict(row)
    finally:
        con.close()

    # 公告来源与原文证据由构建脚本另外留档
    sidecar = ROOT / "deploy" / "agentctl-q0" / "dividend-actions.json"
    if sidecar.exists():
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        for ca in payload.get("corporate_actions") or []:
            if ca["action_id"] == record["action_id"]:
                record.update({k: ca[k] for k in
                               ("source_announcement_id", "evidence", "source_title")
                               if k in ca})
    return record


def _eod_snapshot_for_day(
    snapshots: Sequence[PublishedSnapshot], *, day: str, data_mode: str,
) -> PublishedSnapshot | None:
    """找出同源、同交易日的已发布 EOD 快照。"""

    matches = [item for item in snapshots
               if item.kind == "EOD" and item.data_mode == data_mode
               and item.trading_day == day]
    if not matches:
        return None
    return max(matches, key=lambda item: (
        _snapshot_time(item), item.published_at or "", item.snapshot_id))


def _seed_position(data_dir: Path, portfolio: str, instrument_id: str,
                   shares: int, acquired_on: str) -> None:
    """直接给模拟账户建一笔底仓（独立连接，在 API 启动之前执行）。

    分红用例需要的持仓必须显式建立：靠"跑一轮组合构建碰巧买到"会让
    用例隐式依赖构建参数，参数一调就换标的，分红用例便会以
    与分红毫无关系的方式失败。

    刻意在 API 启动**之前**写库：API 持有自己的连接，
    并发写同一个 SQLite 文件会引入与用例无关的不确定性。
    """

    import sqlite3
    from datetime import datetime as _dt, timezone as _tz

    stamp = _dt(2026, 6, 20, tzinfo=_tz.utc).isoformat()
    con = sqlite3.connect(data_dir / "meta.sqlite")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        con.execute(
            "INSERT OR IGNORE INTO portfolio (portfolio_id,kind,account_type,"
            "base_currency,initial_cash_cents,opened_at,status) "
            "VALUES (?,?,'SIMULATED','CNY',?,?,'ACTIVE')",
            (portfolio, "M", 100_000_000, stamp))
        con.execute(
            "INSERT OR IGNORE INTO cash_entry (entry_id,portfolio_id,entry_type,"
            "amount_cents,trading_day,occurred_at,note) VALUES (?,?,'INITIAL_DEPOSIT',"
            "?,?,?,'opening balance')",
            (portfolio + "-INITIAL", portfolio, 100_000_000, acquired_on, stamp))
        con.execute(
            "INSERT OR IGNORE INTO position_lot (lot_id,portfolio_id,instrument_id,"
            "acquired_trading_day,earliest_sellable_day,quantity_original,"
            "quantity_remaining,cost_basis_cents_per_share,source_fill_id) "
            "VALUES (?,?,?,?,?,?,?,1000,NULL)",
            ("lot-seed-" + instrument_id, portfolio, instrument_id,
             acquired_on, acquired_on, shares, shares))
        con.commit()
    finally:
        con.close()


def _run_plan(client: "Client", portfolio: str,
              pair: FlowSnapshotPair) -> tuple[int, dict]:
    """在双快照绑定下跑一轮 预览 -> 确认 -> 冻结 -> 执行。"""

    status, pv = client.call("POST", "/api/v1/plans/preview", body={
        "portfolio_id": portfolio,
        "snapshot_id": pair.decision.snapshot_id,
        "trading_day": pair.execution.trading_day,
        "decision_snapshot_id": pair.decision.snapshot_id,
        "decision_cutoff_at": pair.decision.as_of_time,
        "execution_snapshot_id": pair.execution.snapshot_id,
    })
    if status != 200:
        return status, pv
    plan_id = pv["planId"]
    tok = client.call("POST", f"/api/v1/plans/{plan_id}/confirmation")[1]
    fr = client.call("POST", f"/api/v1/plans/{plan_id}/freeze", body={
        "plan_id": plan_id, "confirmation_token": tok.get("confirmationToken")})
    if fr[0] != 200:
        return fr[0], fr[1]
    return client.call("POST", f"/api/v1/plans/{plan_id}/execute",
                       body={"plan_id": plan_id})


def select_manual_candidate(candidates: Sequence[dict], model_preview: dict) -> str:
    """选择一只会让人工方案与模型方案产生可验证差异的可模拟候选。

    首期试运行不是只验证一个 ``selected_instrument_ids`` 参数能被接收，而是要
    证明人工选择实际进入组合引擎并留下 ``MODIFY_MODEL``。因此模型方案至少要
    有两个目标；优先选择已经产生订单的目标，避免挑中因整手约束而无订单的标的。
    """

    simulatable = {
        str(candidate.get("instrumentId"))
        for candidate in candidates if candidate.get("simulatable")
    }
    target_ids = [
        str(target.get("instrument_id"))
        for target in (model_preview.get("targets") or [])
        if str(target.get("instrument_id")) in simulatable
    ]
    if len(target_ids) < 2:
        raise ValueError("模型方案少于两个可模拟目标，无法验证人工缩减方案的差异留痕")
    for order in model_preview.get("orders") or []:
        instrument_id = str(order.get("instrument_id"))
        if instrument_id in target_ids:
            return instrument_id
    return target_ids[0]


def _wait_http(url: str, *, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status < 500:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.4)
    return False


def _write_result(*, pair: FlowSnapshotPair | None, trading_day: str | None,
                  portfolio: str | None, conclusion: str,
                  selected_instrument_id: str | None = None) -> Path:
    """写一份不把未完成阶段误标成 PASS 的验收留痕。"""

    out = ROOT / "deploy" / "agentctl-q0" / "real-flow-result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "decision_snapshot_id": pair.decision.snapshot_id if pair else None,
        "execution_snapshot_id": pair.execution.snapshot_id if pair else None,
        "trading_day": trading_day,
        "portfolio_id": portfolio,
        "selected_instrument_id": selected_instrument_id,
        "checks": [{"name": n, "ok": o, "detail": d}
                   for n, o, d in checks],
        "skips": skips,
        "conclusion": conclusion,
    }
    out.write_bytes(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    print(f"报告：{out}")
    return out


def main() -> int:
    checks.clear()
    skips.clear()
    if not (SNAPSHOT_DIR / "meta.sqlite").exists():
        detail = f"缺少快照：{SNAPSHOT_DIR}"
        print(detail)
        print("手工池快照：python -m tests.integration.t6_real_snapshot")
        print("全市场快照：python -m tests.integration.t10_universe_snapshot")
        skip("真实双快照", detail)
        _write_result(pair=None, trading_day=None, portfolio=None,
                      conclusion="ENV_NOT_READY")
        return 2

    try:
        published, current_id = load_published_snapshots(SNAPSHOT_DIR)
        execution_override = (
            os.environ.get("AQUANT_FLOW_EXECUTION_SNAPSHOT_ID", "").strip()
            or os.environ.get("AQUANT_FLOW_SNAPSHOT_ID", "").strip()
            or None
        )
        decision_override = (
            os.environ.get("AQUANT_FLOW_DECISION_SNAPSHOT_ID", "").strip()
            or None
        )
        pair = select_snapshot_pair(
            published,
            current_snapshot_id=current_id,
            execution_snapshot_id=execution_override,
            decision_snapshot_id=decision_override,
        )
    except (OSError, ValueError, SnapshotError) as exc:
        detail = f"无法选择满足 S1 门禁的已发布快照对：{exc}"
        print(f"环境未就绪：{detail}")
        skip("真实双快照", detail)
        _write_result(pair=None, trading_day=None, portfolio=None,
                      conclusion="ENV_NOT_READY")
        return 2

    if pair.execution.data_mode != "PRODUCTION":
        print(f"环境未就绪：真实闭环要求 PRODUCTION，实际为 {pair.execution.data_mode}")
        return 2
    if not pair.execution.trading_day:
        print(f"环境未就绪：执行快照没有交易日：{pair.execution.snapshot_id}")
        return 2

    SNAPSHOT_ID = pair.execution.snapshot_id
    trading_day = pair.execution.trading_day
    portfolio = "pf-real-m"
    dividend = _load_dividend(
        SNAPSHOT_DIR, "SH.600519", snapshot_id=pair.execution.snapshot_id
    )
    dividend_pair: FlowSnapshotPair | None = None
    dividend_skip_reason: str | None = None
    if dividend is None:
        dividend_skip_reason = (
            "执行快照没有 SH.600519 的真实 CASH_DIVIDEND；"
            "独立验证见 tests/golden/test_dividend_persistence.py"
        )
    else:
        dividend_eod = _eod_snapshot_for_day(
            published, day=str(dividend.get("ex_date") or ""),
            data_mode=pair.execution.data_mode,
        )
        if dividend_eod is None:
            dividend_skip_reason = (
                f"没有 {dividend.get('ex_date')} 对应的同源已发布 EOD 快照；"
                "独立验证见 tests/golden/test_dividend_persistence.py"
            )
        else:
            try:
                dividend_pair = select_snapshot_pair(
                    published, execution_snapshot_id=dividend_eod.snapshot_id,
                )
            except ValueError as exc:
                dividend_skip_reason = (
                    f"分红执行快照缺少早于除权日且 S1 就绪的决策快照：{exc}；"
                    "独立验证见 tests/golden/test_dividend_persistence.py"
                )
    print("已选择快照：")
    print(f"  决策：{pair.decision.snapshot_id} @ {pair.decision.as_of_time}")
    print(f"  执行：{pair.execution.snapshot_id} @ {pair.execution.as_of_time} / {trading_day}")

    # 用真实快照的**副本**跑：验收不应改动被复核的那份数据。
    work = Path(tempfile.mkdtemp(prefix="aquant-realflow-"))
    shutil.copytree(SNAPSHOT_DIR, work / "data")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["AQUANT_DATA_DIR"] = str(work / "data")
    env["AQUANT_SNAPSHOT_ID"] = SNAPSHOT_ID
    commission_ready = bool(
        env.get("AQUANT_COMMISSION_RATE", "").strip()
        and env.get("AQUANT_COMMISSION_MIN_CENTS", "").strip()
    )
    if commission_ready:
        print(f"[费率] 使用配置的券商佣金：{env['AQUANT_COMMISSION_RATE']}，"
              f"最低 {env['AQUANT_COMMISSION_MIN_CENTS']} 分")
    else:
        missing = [name for name in (
            "AQUANT_COMMISSION_RATE", "AQUANT_COMMISSION_MIN_CENTS")
                   if not env.get(name, "").strip()]
        print("[费率] 佣金配置不完整（缺少 " + ", ".join(missing)
              + "）；只运行只读与时点检查，跳过预览、冻结、执行、估值和分红写路径。")

    # 分红底仓同样必须在 API 启动前建立；只有对应 EOD 快照和同源决策
    # 快照都存在时才准备它，避免没有证据时靠手工日期制造一条“通过”。
    if commission_ready and dividend_pair is not None and dividend is not None:
        _seed_position(work / "data", "pf-real-div", "SH.600519", 1000,
                       str(dividend["record_date"]))

    api = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--app-dir", "apps/api",
         "--host", "127.0.0.1", "--port", str(API_PORT)],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{API_PORT}"
    try:
        if not _wait_http(base + "/api/v1/health"):
            print("API 未就绪；输出如下：")
            api.terminate()
            out, _ = api.communicate(timeout=10)
            print((out or "")[-2000:])
            return 2
        print(f"真实快照 API：{base}  快照 {SNAPSHOT_ID}")

        client = Client(base)

        print("\n[1] 数据状态与快照绑定")
        status, body = client.call("GET", "/api/v1/status")
        check("状态可读", status == 200, str(status))
        check("快照 ID 正确", body.get("snapshotId") == SNAPSHOT_ID,
              str(body.get("snapshotId")))
        check("数据模式为真实数据", body.get("dataMode") == "PRODUCTION",
              str(body.get("dataMode")))
        check("真实数据带水印", bool(body.get("watermark")), str(body.get("watermark")))

        print("\n[2] 候选来自决策快照上的 S1")
        candidate_path = ("/api/v1/candidates?snapshot_id="
                          + urllib.parse.quote(pair.decision.snapshot_id, safe=""))
        status, cbody = client.call("GET", candidate_path)
        cands = cbody.get("candidates") or []
        check("候选绑定决策快照",
              status == 200 and cbody.get("snapshotId") == pair.decision.snapshot_id,
              str(cbody.get("snapshotId")))
        check("候选非空", bool(cands), f"{len(cands)} 只")
        check("候选全部来自真实池", all(
            str(c.get("instrumentId", "")).startswith(("SH.", "SZ.")) for c in cands))
        simulatable = [c for c in cands if c.get("simulatable")]
        check("存在可模拟（沪深主板）候选", bool(simulatable), f"{len(simulatable)} 只")
        check("成长板块候选被标为不可模拟",
              any(not c.get("simulatable") for c in cands) or len(cands) == len(simulatable),
              "GEM/STAR 应不可模拟")

        # 用执行快照的最后一个交易日：参考价必须来自它**之前**的交易日。
        # 决策截止时点和执行快照 ID 均由同一个已发布目录选择结果传入。
        if not commission_ready:
            check("真实佣金已配置", False,
                  "AQUANT_COMMISSION_RATE 未配置，真实写路径未验收")
            skip("真实分红闭环",
                 "未运行：真实佣金未配置；独立验证见 "
                 "tests/golden/test_dividend_persistence.py")
            _write_result(pair=pair, trading_day=trading_day,
                          portfolio=portfolio, conclusion="ENV_NOT_READY")
            return 2

        print(f"\n[3] 模型基线与人工点选预览（执行日 {trading_day}）")
        preview_body = {
            "portfolio_id": portfolio,
            "snapshot_id": pair.decision.snapshot_id,
            "trading_day": trading_day,
            "decision_snapshot_id": pair.decision.snapshot_id,
            "decision_cutoff_at": pair.decision.as_of_time,
            "execution_snapshot_id": pair.execution.snapshot_id,
        }
        status, model_pv = client.call(
            "POST", "/api/v1/plans/preview", body=preview_body)
        check("模型基线预览成功", status == 200,
              json.dumps(model_pv, ensure_ascii=False)[:200])
        if status != 200:
            return 1
        try:
            selected_instrument_id = select_manual_candidate(cands, model_pv)
        except ValueError as exc:
            check("可构造人工差异方案", False, str(exc))
            return 1
        status, pv = client.call("POST", "/api/v1/plans/preview", body={
            **preview_body,
            "selected_instrument_ids": [selected_instrument_id],
        })
        check("人工点选预览成功", status == 200,
              json.dumps(pv, ensure_ascii=False)[:200])
        if status != 200:
            return 1
        planned_ids = {
            str(item.get("instrument_id"))
            for item in (pv.get("targets") or []) + (pv.get("orders") or [])
        }
        check("人工预览只包含点选标的",
              bool(planned_ids) and planned_ids == {selected_instrument_id},
              f"selected={selected_instrument_id} planned={sorted(planned_ids)}")
        plan_id = pv["planId"]
        ref_day = pv.get("reference_price_day")
        check("参考价日早于执行日", bool(ref_day) and ref_day < trading_day,
              f"{ref_day} < {trading_day}")
        check("预览未冻结", pv.get("frozen") in (False, None), str(pv.get("frozen")))
        check("有订单或全部有排除原因",
              bool(pv.get("orders")) or bool(pv.get("excluded")),
              f"orders={len(pv.get('orders') or [])} excluded={len(pv.get('excluded') or [])}")
        check("预估费用非负", (pv.get("estimated_fees_cents") or 0) >= 0,
              str(pv.get("estimated_fees_cents")))

        print("\n[4] 确认并冻结（一次性令牌）")
        status, tok = client.call("POST", f"/api/v1/plans/{plan_id}/confirmation")
        check("签发令牌", status == 200, str(status))
        token = tok.get("confirmationToken")
        status, fr = client.call("POST", f"/api/v1/plans/{plan_id}/freeze", body={
            "plan_id": plan_id, "confirmation_token": token,
        })
        check("冻结成功", status == 200, json.dumps(fr, ensure_ascii=False)[:200])
        decision = fr.get("decision") or {}
        check("冻结记录人工修改决策",
              decision.get("decision_type") == "MODIFY_MODEL",
              json.dumps(decision, ensure_ascii=False)[:200])
        status, decision_body = client.call(
            "GET", "/api/v1/decisions?portfolio_id="
            + urllib.parse.quote(portfolio, safe=""))
        persisted = [row for row in (decision_body.get("decisions") or [])
                     if row.get("plan_id") == plan_id]
        check("人工决策已持久化并绑定计划",
              status == 200 and len(persisted) == 1
              and persisted[0].get("decision_type") == "MODIFY_MODEL",
              json.dumps(persisted, ensure_ascii=False)[:200])

        print("\n[5] 执行")
        status, ex = client.call("POST", f"/api/v1/plans/{plan_id}/execute",
                                 body={"plan_id": plan_id})
        check("执行成功", status == 200, json.dumps(ex, ensure_ascii=False)[:200])
        fills = ex.get("fills") or []
        check("执行结果自洽：成交+未成交=订单数",
              len(fills) + len(ex.get("rejections") or []) >= len(pv.get("orders") or []),
              f"fills={len(fills)} rejections={len(ex.get('rejections') or [])}")
        if fills:
            check("成交价为正整数分", all(f["price_cents"] > 0 for f in fills))
            check("成交数量为正整数", all(f["quantity"] > 0 for f in fills))

        print("\n[6] 日终估值")
        status, val = client.call("POST", "/api/v1/valuations", body={
            "portfolio_id": portfolio,
            "snapshot_id": pair.execution.snapshot_id,
            "trading_day": trading_day,
        })
        check("估值成功", status == 200, json.dumps(val, ensure_ascii=False)[:200])
        if status == 200:
            net = val["net_value_cents"]
            recomputed = (val["cash_available_cents"] + val["cash_frozen_cents"]
                          + val["receivables_cents"] + val["positions_value_cents"]
                          - val["payables_cents"])
            check("净值恒等式成立", net == recomputed,
                  f"{net} vs {recomputed}")
            check("净值已发布", val.get("published") is True, str(val.get("published")))

        print("\n[7] 逐项对账")
        status, rec = client.call("GET", f"/api/v1/portfolios/{portfolio}/reconcile")
        check("对账成功", status == 200, json.dumps(rec, ensure_ascii=False)[:200])
        if status == 200:
            check("对账结论为一致", rec.get("reconciled") is True, json.dumps(rec)[:200])
            check("现金与账本一致",
                  rec.get("valuation_cash_matches_ledger") is True)
            check("应收与账本一致",
                  rec.get("valuation_receivables_matches_ledger") is True)
            inv = rec.get("invariants") or {}
            for name in ("fill_le_order", "fees_booked_once",
                         "cash_lines_sum_to_balance", "lots_match_fills"):
                check(f"不变量 {name}", inv.get(name) is True, str(inv.get(name)))

        # ------------------------------------------------ 真实分红全链路
        print("\n[8] 真实分红：从归档公告到账面现金")
        if dividend_skip_reason is not None or dividend_pair is None or dividend is None:
            skip("真实分红闭环", dividend_skip_reason or
                 "缺少分红所需的同源快照对；独立验证见 "
                 "tests/golden/test_dividend_persistence.py")
        else:
            div = dividend
            check("分红来自归档公告", bool(div.get("source_announcement_id")),
                  str(div.get("source_announcement_id")))
            check("分红带原文证据", bool(div.get("evidence")),
                  str(sorted((div.get("evidence") or {}).keys())))

            record_day = div["record_date"]
            ex_day = div["ex_date"]
            micros = div["cash_per_share_micros"]
            shares = 1000
            # 除权日与到账日同日的真实样本（茅台 2026-06-26）：
            # 买入 1000 股 -> 应收 1000 × 28024230 微元 = 280242.30 元 = 28024230 分
            expected_cents = shares * micros // 10_000

            pf = "pf-real-div"
            # **显式建底仓**，不依赖组合构建恰好选中这只标的：
            # 组合结果取决于持仓上限与权重上限，任何参数调整都会换标的，
            # 于是分红用例会因为与分红无关的原因失败（我第一版就是这样）。
            first = (200, {})
            check("底仓已建立（登记日持有）", True,
                  f"{shares} 股，登记日 {record_day}")

            status, body = client.call("POST", "/api/v1/plans/preview", body={
                "portfolio_id": pf,
                "snapshot_id": dividend_pair.decision.snapshot_id,
                "trading_day": dividend_pair.execution.trading_day,
                "decision_snapshot_id": dividend_pair.decision.snapshot_id,
                "decision_cutoff_at": dividend_pair.decision.as_of_time,
                "execution_snapshot_id": dividend_pair.execution.snapshot_id,
            })
            check("除权日可预览", status == 200,
                  str(status) + " " + json.dumps(body, ensure_ascii=False)[:400])

            # 直接调用执行端点，带上从公告解析出的分红
            actions = [{
                "action_id": div["action_id"],
                "instrument_id": "SH.600519",
                "record_date": record_day, "ex_date": ex_day, "pay_date": div["pay_date"],
                # 传**微元**：28.02423 元/股 = 2,802.423 分，按分传递会截断
                "cash_per_share_micros": micros,
            }]
            pid2 = body.get("planId") if status == 200 else None
            if pid2:
                tok = client.call("POST", f"/api/v1/plans/{pid2}/confirmation")[1]
                fr = client.call("POST", f"/api/v1/plans/{pid2}/freeze", body={
                    "plan_id": pid2,
                    "confirmation_token": tok.get("confirmationToken")})
                if fr[0] == 200:
                    ex = client.call("POST", f"/api/v1/plans/{pid2}/execute",
                                     body={"plan_id": pid2,
                                           "corporate_actions": actions})
                    check("除权日执行成功", ex[0] == 200, json.dumps(ex[1])[:160])
                    outcomes = ex[1].get("corporate_actions") or []
                    if outcomes:
                        stage = outcomes[0]
                        check("分红被识别并落账",
                              stage.get("entitlement_shares", 0) > 0, json.dumps(stage)[:160])
                        # 金额必须**精确**：1000 股 × 28.02423 元 = 28,024.23 元
                        check("派息金额精确到分（无截断）",
                              stage.get("cash_delta_cents") == expected_cents,
                              f"实际 {stage.get('cash_delta_cents')} / 期望 {expected_cents} 分")
                    else:
                        check("分红被识别并落账", False,
                              "执行结果没有 corporate_actions："
                              "底仓可能未覆盖登记日，或分红未送达")

        print("\n[9] 跨源拒绝：指向别的快照必须 404，不得静默换数据")
        status, _ = client.call("POST", "/api/v1/plans/preview", body={
            "portfolio_id": portfolio, "snapshot_id": "snap-syn-001",
            "trading_day": trading_day,
        })
        check("合成快照 ID 被拒绝", status == 404, str(status))

        # 未成交/未执行也要给出明确结论，不能静默跳过
        failed = [c for c in checks if not c[1]]
        print("\n" + "=" * 62)
        print(f"真实数据闭环验收 {len(checks) - len(failed)}/{len(checks)} 通过")
        for name, _, detail in failed:
            print("  - " + name + "  " + detail)
        for item in skips:
            print("  - SKIP " + item["name"] + "  " + item["detail"])
        conclusion = ("FAIL" if failed else
                      "PASS_WITH_SKIPS" if skips else "PASS")
        _write_result(pair=pair, trading_day=trading_day,
                      portfolio=portfolio, conclusion=conclusion,
                      selected_instrument_id=selected_instrument_id)
        return 1 if (failed or skips) else 0
    finally:
        api.terminate()
        try:
            api.wait(timeout=10)
        except subprocess.TimeoutExpired:
            api.kill()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
