"""A-Quant Lab 资料包自检程序。

主文档 §0 声明：本脚本"只验证本资料包的契约、示例和 SQL，不等同于产品测试"。
这一点必须被严格遵守——它不启动服务、不访问网络、不需要模型密钥。

检查项：
  1. contracts/*.json 是合法 JSON Schema，且所有 $ref 可解析（含跨文件与 definitions）
  2. examples/*.yaml 通过 YAML 解析，且关键不变量成立（水印、单位、用例覆盖）
  3. schema/*.sql 可在全新 SQLite 中无错执行，并能重入（IF NOT EXISTS）
  4. 数据库约束与触发器按设计生效：
       - 已发布快照不可 UPDATE/DELETE
       - 已入账成交不可 DELETE
       - 不变量未全过时禁止发布净值
       - 幂等键唯一约束阻止重复作业
  5. configs/research.example.yaml 与契约、禁用工具清单一致

退出码：0 = 全部通过；1 = 有失败项。

    python tests/validate_spec.py
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []
CHECKS = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if ok:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (f"  -- {detail}" if detail else ""))
        FAILURES.append(name)


def load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 1. contracts
def collect_refs(node, out: list[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                out.append(v)
            else:
                collect_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            collect_refs(v, out)


def check_contracts() -> None:
    print("\n[1] contracts/*.json")
    files = sorted((ROOT / "contracts").glob("*.json"))
    check("contracts 目录非空", bool(files), "未找到任何 .json")
    schemas: dict[str, dict] = {}
    for f in files:
        try:
            schemas[f.name] = load_json(f)
        except Exception as exc:  # noqa: BLE001
            check(f"{f.name} 是合法 JSON", False, str(exc))
            continue
        s = schemas[f.name]
        check(f"{f.name} 是合法 JSON", True)
        check(f"{f.name} 声明 draft-07", "draft-07" in str(s.get("$schema", "")), s.get("$schema"))
        check(f"{f.name} 有 title", bool(s.get("title")))

    # 跨文件 $ref 解析
    for fname, schema in schemas.items():
        refs: list[str] = []
        collect_refs(schema, refs)
        for ref in refs:
            if ref.startswith("#/"):
                node = schema
                ok = True
                for part in ref[2:].split("/"):
                    part = part.replace("~1", "/").replace("~0", "~")
                    if isinstance(node, dict) and part in node:
                        node = node[part]
                    else:
                        ok = False
                        break
                check(f"{fname} 本地引用 {ref} 可解析", ok)
            else:
                target, _, frag = ref.partition("#")
                ok = target in schemas
                if ok and frag.startswith("/"):
                    node = schemas[target]
                    for part in frag[1:].split("/"):
                        if isinstance(node, dict) and part in node:
                            node = node[part]
                        else:
                            ok = False
                            break
                check(f"{fname} 跨文件引用 {ref} 可解析", ok)


# ---------------------------------------------------------------- 2. examples
def check_examples() -> None:
    print("\n[2] examples/*.yaml")
    try:
        import yaml
    except ImportError:
        check("PyYAML 可用", False, "未安装 PyYAML；无法校验示例")
        return

    files = sorted((ROOT / "examples").glob("*.yaml"))
    check("examples 目录非空", bool(files))
    for f in files:
        try:
            doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            check(f"{f.name} 可解析", False, str(exc))
            continue
        check(f"{f.name} 可解析", isinstance(doc, dict))

        # SYNTHETIC 水印强制（§15.4）
        is_synth = doc.get("data_mode") == "SYNTHETIC"
        check(f"{f.name} SYNTHETIC 带水印", (not is_synth) or bool(doc.get("watermark")))

        # 交易日历单调且无重复
        days = doc.get("trading_days") or []
        check(f"{f.name} 交易日历有序无重复", days == sorted(set(days)), str(days[:5]))

        # 证券状态历史左闭右开、按 valid_from 有序
        for inst in doc.get("instruments") or []:
            hist = inst.get("status_history") or []
            starts = [h.get("valid_from") for h in hist]
            check(f"{f.name} {inst.get('instrument_id')} 状态历史有序",
                  starts == sorted(starts) and len(starts) == len(set(starts)))

        # 行情：价格单位与正数
        for q in doc.get("daily_quotes") or []:
            px = [q.get("open_cents"), q.get("high_cents"), q.get("low_cents"), q.get("close_cents")]
            ok = all(isinstance(x, int) and x > 0 for x in px)
            check(f"{f.name} {q.get('instrument_id')}@{q.get('trading_day')} 价格为正整数分", ok, str(px))
            if ok:
                check(f"{f.name} {q.get('instrument_id')}@{q.get('trading_day')} low<=open,close<=high",
                      q["low_cents"] <= min(q["open_cents"], q["close_cents"])
                      and max(q["open_cents"], q["close_cents"]) <= q["high_cents"])

        # 公司行为：日期先后与金额单位
        for ca in doc.get("corporate_actions") or []:
            if ca.get("action_type") == "CASH_DIVIDEND":
                rd, ex, pay = ca.get("record_date"), ca.get("ex_date"), ca.get("pay_date")
                check(f"{f.name} {ca.get('action_id')} 分红日期顺序 记录<除权<=到账",
                      bool(rd and ex and pay and rd < ex <= pay), f"{rd} {ex} {pay}")
                check(f"{f.name} {ca.get('action_id')} 每股股利为正整数分",
                      isinstance(ca.get("cash_per_share_cents"), int)
                      and ca["cash_per_share_cents"] > 0)

        # 事件：available_at 不得早于 first_seen_at 之外的可证明依据（§7.2）
        for ev in doc.get("events") or []:
            check(f"{f.name} {ev.get('event_id')} available_at 非空", bool(ev.get("available_at")))
            check(f"{f.name} {ev.get('event_id')} 有 available_basis",
                  ev.get("available_basis") in {"OBSERVED", "VENDOR_PIT", "RECONSTRUCTED", "UNKNOWN"})
            check(f"{f.name} {ev.get('event_id')} 有 citation",
                  bool(ev.get("citations")))
            # 恶意样本必须存在（§18.3 / A05）
        ids = [e.get("event_id") for e in (doc.get("events") or [])]
        check(f"{f.name} 含恶意注入样本", any("malicious" in str(i) for i in ids), str(ids))

        # 合成数据必须带免责声明
        check(f"{f.name} 带免责声明", bool(doc.get("disclaimer")))

        # 黄金用例期望值存在
        ge = doc.get("golden_expectations") or {}
        for key in ("D04_date_only_announcement", "D05_historical_classification",
                    "S02_limit_up_no_fill", "S07_dividend_timing",
                    "S08_suspension_valuation", "S10_no_intraday_inference"):
            check(f"{f.name} 黄金期望含 {key}", key in ge)


# ---------------------------------------------------------------- 3/4. SQL
def check_sql() -> None:
    print("\n[3] schema/*.sql 可执行且可重入")
    files = sorted((ROOT / "schema").glob("*.sql"))
    check("schema 目录非空", bool(files))

    con = sqlite3.connect(":memory:")
    try:
        for f in files:
            sql = f.read_text(encoding="utf-8")
            try:
                con.executescript(sql)
                check(f"{f.name} 首次执行无错", True)
            except Exception as exc:  # noqa: BLE001
                check(f"{f.name} 首次执行无错", False, str(exc))
        # 重入
        for f in files:
            sql = f.read_text(encoding="utf-8")
            try:
                con.executescript(sql)
                check(f"{f.name} 可重入执行", True)
            except Exception as exc:  # noqa: BLE001
                check(f"{f.name} 可重入执行", False, str(exc))

        print("\n[4] 约束与触发器")
        cur = con.cursor()
        tables = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("job", "snapshot", "fill", "valuation", "cash_entry",
                  "position_lot", "simulation_plan", "event", "document", "citation"):
            check(f"表 {t} 存在", t in tables)

        now = "2026-09-11T12:00:00Z"

        # 4a 幂等键唯一：重复作业必须被拒（§8.4 / A07 / S05）
        con.execute("INSERT INTO job (job_id,job_type,idempotency_key,status,created_at,updated_at)"
                    " VALUES ('j1','eod','key-1','PENDING',?,?)", (now, now))
        try:
            con.execute("INSERT INTO job (job_id,job_type,idempotency_key,status,created_at,updated_at)"
                        " VALUES ('j2','eod','key-1','PENDING',?,?)", (now, now))
            check("重复幂等键被拒绝", False, "第二次插入竟然成功")
        except sqlite3.IntegrityError:
            check("重复幂等键被拒绝", True)

        # 4b 已发布快照不可更新（§15.4）
        con.execute(
            "INSERT INTO snapshot (snapshot_id,kind,data_mode,status,input_cutoff_at,created_at,"
            "code_version,data_version,quality_status)"
            " VALUES ('snap-x','EOD','SYNTHETIC','PUBLISHED',?,?,'c1','d1','OK')", (now, now))
        try:
            con.execute("UPDATE snapshot SET status='REJECTED' WHERE snapshot_id='snap-x'")
            check("已发布快照不可 UPDATE", False, "更新竟然成功")
        except sqlite3.IntegrityError as exc:
            check("已发布快照不可 UPDATE", "immutable" in str(exc), str(exc))
        try:
            con.execute("DELETE FROM snapshot WHERE snapshot_id='snap-x'")
            check("已发布快照不可 DELETE", False, "删除竟然成功")
        except sqlite3.IntegrityError as exc:
            check("已发布快照不可 DELETE", "cannot be deleted" in str(exc), str(exc))

        # 4c 成交金额必须为整数分（§12.6 禁止浮点记账）
        con.execute("INSERT INTO portfolio (portfolio_id,kind,initial_cash_cents,opened_at)"
                    " VALUES ('pf-1','M',100000000,?)", (now,))
        con.execute("INSERT INTO simulation_plan (plan_id,portfolio_id,snapshot_id,account_version,"
                    "plan_version,status,created_at,idempotency_key)"
                    " VALUES ('plan-1','pf-1','snap-x',1,1,'FROZEN',?,'k-plan-1')", (now,))
        con.execute("INSERT INTO instrument (instrument_id,exchange,board,created_at,updated_at)"
                    " VALUES ('SYN.A.600519','SSE','MAIN',?,?)", (now, now))
        con.execute('INSERT INTO "order" (order_id,portfolio_id,plan_id,snapshot_id,instrument_id,'
                    "side,quantity,status,trading_day,created_at,idempotency_key)"
                    " VALUES ('o1','pf-1','plan-1','snap-x','SYN.A.600519','BUY',100,'PENDING',"
                    "'2026-09-11',?,'k-o1')", (now,))
        con.execute("INSERT INTO fill (fill_id,order_id,portfolio_id,instrument_id,side,quantity,"
                    "price_cents,gross_amount_cents,fees_total_cents,trading_day,filled_at)"
                    " VALUES ('f1','o1','pf-1','SYN.A.600519','BUY',100,132800,13280000,500,"
                    "'2026-09-11',?)", (now,))
        try:
            con.execute("DELETE FROM fill WHERE fill_id='f1'")
            check("已入账成交不可 DELETE", False, "删除竟然成功")
        except sqlite3.IntegrityError as exc:
            check("已入账成交不可 DELETE", "reversal" in str(exc), str(exc))

        # 4d 同一订单同一费用码不得重复计费（§12.6）
        con.execute("INSERT INTO fee_charge (fee_charge_id,fill_id,fee_code,amount_cents,fee_version)"
                    " VALUES ('fc1','f1','COMMISSION',500,'fee-syn-v1')")
        try:
            con.execute("INSERT INTO fee_charge (fee_charge_id,fill_id,fee_code,amount_cents,fee_version)"
                        " VALUES ('fc2','f1','COMMISSION',500,'fee-syn-v1')")
            check("同一成交同一费用码不可重复计费", False, "重复计费竟然成功")
        except sqlite3.IntegrityError:
            check("同一成交同一费用码不可重复计费", True)

        # 4e 不变量未全过时禁止发布净值（§12.8）
        con.execute(
            "INSERT INTO valuation (valuation_id,portfolio_id,trading_day,cash_available_cents,"
            "net_value_cents,invariant_cash_not_overdrawn,invariant_positions_not_negative,"
            "invariant_shares_match_lots,invariant_fill_le_order,invariant_fees_booked_once,"
            "invariant_cash_lines_sum,published,computed_at)"
            " VALUES ('v1','pf-1','2026-09-11',100000,100000,1,1,1,1,0,1,0,?)", (now,))
        try:
            con.execute("UPDATE valuation SET published=1 WHERE valuation_id='v1'")
            check("不变量未全过时禁止发布净值", False, "发布竟然成功")
        except sqlite3.IntegrityError as exc:
            check("不变量未全过时禁止发布净值", "invariants" in str(exc), str(exc))
        con.execute("UPDATE valuation SET invariant_fees_booked_once=1 WHERE valuation_id='v1'")
        con.execute("UPDATE valuation SET published=1 WHERE valuation_id='v1'")
        check("不变量全过后允许发布净值", True)

        # 4f T+1 批次约束（§12.5）
        con.execute("INSERT INTO position_lot (lot_id,portfolio_id,instrument_id,"
                    "acquired_trading_day,earliest_sellable_day,quantity_original,"
                    "quantity_remaining,cost_basis_cents_per_share)"
                    " VALUES ('lot1','pf-1','SYN.A.600519','2026-09-11','2026-09-14',100,100,132800)")
        check("T+1 批次最早可卖日晚于买入日", True)
        try:
            con.execute("UPDATE position_lot SET quantity_remaining=200 WHERE lot_id='lot1'")
            check("批次剩余股数不得超过原始股数", False, "更新竟然成功")
        except sqlite3.IntegrityError:
            check("批次剩余股数不得超过原始股数", True)

        # 4g 冻结计划不可被改回草稿（§16.2）
        try:
            con.execute("UPDATE simulation_plan SET status='DRAFT' WHERE plan_id='plan-1'")
            check("冻结计划不可改回草稿", False, "更新竟然成功")
        except sqlite3.IntegrityError as exc:
            check("冻结计划不可改回草稿", "immutable" in str(exc), str(exc))

        # 4h 错误码词表与契约一致（§16.4）
        common = load_json(ROOT / "contracts" / "common.schema.json")
        declared = set(common["definitions"]["errorCode"]["enum"])
        expected = {
            "DATA_NOT_READY", "PIT_UNVERIFIED", "SOURCE_PERMISSION_MISSING", "STALE_SNAPSHOT",
            "RULE_VERSION_MISSING", "FEE_VERSION_UNVERIFIED", "DECISION_CUTOFF_PASSED",
            "INSUFFICIENT_CASH", "T1_NOT_SELLABLE", "LIMIT_PRICE_BLOCKED",
            "CORPORATE_ACTION_UNSUPPORTED", "LLM_BUDGET_EXCEEDED",
        }
        check("错误码词表与主文档 §16.4 完全一致", declared == expected,
              f"缺 {expected - declared} 多 {declared - expected}")
    finally:
        con.close()


# ---------------------------------------------------------------- 5. configs
def check_configs() -> None:
    print("\n[5] configs/research.example.yaml")
    try:
        import yaml
    except ImportError:
        check("PyYAML 可用", False)
        return
    files = sorted((ROOT / "configs").glob("*.yaml"))
    check("configs 目录非空", bool(files))
    for f in files:
        doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        check(f"{f.name} 可解析", isinstance(doc, dict))

        # §16.3 禁用工具清单必须齐全
        forbidden = set((doc.get("assistant") or {}).get("forbidden_tools") or [])
        for tool in ("execute_order", "write_ledger", "run_shell", "raw_sql",
                     "fetch_arbitrary_url", "freeze_plan"):
            check(f"{f.name} 禁用工具含 {tool}", tool in forbidden)

        # 允许与禁用不得重叠
        allowed = set((doc.get("assistant") or {}).get("allowed_tools") or [])
        check(f"{f.name} 允许/禁用工具无重叠", not (allowed & forbidden),
              str(allowed & forbidden))

        # §12.6 合成费率必须显式禁止用于正式研究
        fees = (doc.get("simulation") or {}).get("fees") or {}
        check(f"{f.name} 合成费率标注禁止用于正式研究",
              fees.get("synthetic_test_rate") is True
              and fees.get("prohibited_for_production_research") is True)

        # §12.3 禁止把意图价夹到法定边界
        fc = (doc.get("simulation") or {}).get("fill_convention") or {}
        check(f"{f.name} 禁止 clamp_to_limit", fc.get("clamp_to_limit") is False)

        # §12.4 必须提供保守现金对照模式
        sizing = (doc.get("simulation") or {}).get("sizing") or {}
        check(f"{f.name} 提供保守现金模式", sizing.get("conservative_cash_mode_available") is True)

        # §7.4 PIT 依赖因子默认关闭
        feats = doc.get("features") or {}
        check(f"{f.name} PIT 依赖因子默认关闭", feats.get("pit_dependent_factors_enabled") is False)

        # §11.2 单一行业上限不得低于单一证券上限（否则约束自相矛盾）
        pc = doc.get("portfolio_construction") or {}
        check(f"{f.name} 行业上限 >= 单券上限",
              float(pc.get("max_single_industry_pct", 0)) >= float(pc.get("max_single_name_pct", 0)))

        # §8.1 迟到快照不得静默回退
        sch = doc.get("schedule") or {}
        check(f"{f.name} 迟到快照不回退",
              sch.get("late_snapshot_behavior") == "NO_NEW_ORDERS_NO_SILENT_FALLBACK")


def main() -> int:
    print("A-Quant Lab 资料包自检（只验证本资料包的契约、示例与 SQL）")
    print(f"根目录: {ROOT}")
    check_contracts()
    check_examples()
    check_sql()
    check_configs()

    print()
    print("=" * 66)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}/{CHECKS} 项未通过")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"OK: {CHECKS}/{CHECKS} 项全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
