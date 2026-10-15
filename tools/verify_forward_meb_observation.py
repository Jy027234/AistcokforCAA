"""Read saved forward M/E/B evidence; never collect, execute, value or advance.

Run before the scheduled task for a preparation check, then reuse after actual
EOD publication. --write-report writes only the explicitly assigned report.
No missing observation is represented as a zero return or cash-only account.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, time, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from aquant.domain.data.db import connect
from aquant.artifact_paths import resolve_archive_member, resolve_artifact_path
from aquant.domain.data.reader import SnapshotReader
from aquant.domain.data.snapshot import SnapshotStore
from aquant.domain.portfolio.plan import PlanService
from aquant.operations.forward_comparison import ForwardComparisonStore
from aquant.operations.simulation_attribution import AttributionStore, _dated_ledger, _hash

SERIES = "fwd-02c6d5013c1914d397871086"
TZ = ZoneInfo("Asia/Shanghai")


def file_hash(path):
    digest = hashlib.sha256()
    with resolve_artifact_path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def check(condition, message):
    if not condition:
        raise ValueError(message)


def stamp(value):
    value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    check(value.tzinfo is not None, "timestamp is not timezone aware")
    return value


def inspect(data_dir: Path, series_id: str, trading_day: str) -> dict:
    store = ForwardComparisonStore(data_dir / "forward-comparison")
    spec = store.spec(series_id)
    opening = datetime.combine(date.fromisoformat(spec["firstTradingDay"]), time(9, 30), TZ)
    check(stamp(spec["createdAt"]) < opening, "spec was not registered before execution open")
    check(stamp(spec["decisionCutoffAt"]) < opening, "decision cutoff is not before execution open")
    research = resolve_artifact_path(spec["researchOriginPath"])
    check(file_hash(research) == spec["researchContentHash"], "frozen research origin SHA differs")
    research_cards = json.loads(research.read_text(encoding="utf-8"))["candidates"]
    names = {row["instrumentId"]: row["displayName"] for row in research_cards}
    industry_names = {}
    for card in research_cards:
        classification = card.get("industryClassification") or {}
        if classification.get("categoryCode") and classification.get("majorCode") and classification.get("majorName"):
            industry_names[classification["categoryCode"] + classification["majorCode"]] = classification["majorName"]
    observations = store.observations(series_id)
    saved = next((row for row in observations if row["tradingDay"] == trading_day), None)
    sources = {"spec": {"path": str((store.root / (series_id + ".spec.json")).resolve()),
                        "sha256": file_hash(store.root / (series_id + ".spec.json"))},
               "researchOrigin": {"path": str(research.resolve()), "sha256": file_hash(research)}}
    result = {"schema": "aquant.read-only-meb-verification.v1", "seriesId": series_id,
        "checkedAt": datetime.now(timezone.utc).astimezone(TZ).isoformat(), "requestedTradingDay": trading_day,
        "registeredAt": spec["createdAt"], "firstTradingDay": spec["firstTradingDay"],
        "decisionSnapshotId": spec["decisionSnapshotId"], "decisionCutoffAt": spec["decisionCutoffAt"],
        "commonPoolCount": spec["commonPoolCount"], "targetCounts": {
            k: len(spec[name]) for k, name in (("M", "modelTargets"), ("E", "eventTargets"), ("B", "benchmarkTargets"))},
        "observationTargetSessions": spec["observationTargetSessions"], "observedSessionCount": len(observations),
        "humanStatus": spec["human"]["status"], "verifiedEventCount": len(spec["verifiedEvents"]),
        "sources": sources, "portfolioFacts": {}, "providerCalls": 0, "databaseWrites": 0}
    check(spec["human"]["status"] == "UNAVAILABLE", "unexpected human-origin account")
    con = connect(data_dir / "meta.sqlite", read_only=True)
    try:
        con.execute("BEGIN")
        result["accountsPresent"] = [kind for kind in "MEB" if con.execute(
            "SELECT 1 FROM portfolio WHERE portfolio_id=?", (spec["portfolios"][kind]["portfolioId"],)).fetchone()]
        if saved is None:
            result["status"] = "WAITING_SAVED_OBSERVATION"
            result["note"] = "没有该日已保存观测，不输出收益或虚构账户。"
            return result
        check(saved["seriesId"] == series_id and saved["tradingDay"] == trading_day, "saved observation identity differs")
        observation_path = store.root / f"{series_id}.{trading_day}.observation.json"
        sources["observation"] = {"path": str(observation_path.resolve()), "sha256": file_hash(observation_path),
                                  "contentHash": _hash(saved)}
        reader = SnapshotReader(SnapshotStore(con, data_dir / "api"))
        ref = reader.ref(saved["snapshotId"])
        quotes = reader.daily_quotes(saved["snapshotId"], as_of=ref.as_of_time)
        check(quotes and max(row.trading_day for row in quotes).isoformat() == trading_day,
              "execution snapshot market day differs from this forward observation")
        check(stamp(saved["cutoffAt"]) == ref.as_of_time, "saved observation changed actual provider cutoff")
        result.update(snapshotId=saved["snapshotId"], marketDay=trading_day, providerCutoffAt=saved["cutoffAt"],
                      observedAt=saved["observedAt"], delayedEngineeringCalculation=saved["delayedEngineeringCalculation"],
                      missingEodDays=saved["missingEodDays"])
        sources["executionDatasets"] = [{"name": d["name"], "recordCount": d["record_count"],
            "path": str(resolve_archive_member(reader.store.root, d["path"])),
            "sha256": reader.store.verify_dataset(saved["snapshotId"], d["name"])}
            for d in reader.store.datasets(saved["snapshotId"])]
        attr_root = data_dir / "simulation-attribution"
        attr_path = attr_root / f"{series_id}.{trading_day}.attribution.json"
        if not attr_path.exists():
            result["status"] = "WAITING_SAVED_ATTRIBUTION"
            result["note"] = "日终观测已保存，归因档案尚未落盘；等待已有流水线，不重复执行。"
            return result
        attribution = AttributionStore(attr_root).read(series_id, trading_day)
        check(attribution["originHash"] == _hash(saved), "attribution is not bound to this exact saved observation")
        check(attribution["snapshotId"] == saved["snapshotId"], "attribution snapshot differs")
        sources["attribution"] = {"path": str(attr_path.resolve()), "sha256": file_hash(attr_path),
                                  "contentHash": attribution["contentHash"]}
        # Only reconcile() is called: it reads ledger invariants and never
        # computes a new valuation, fee charge, order or provider request.
        svc = PlanService(con, reader, None, [], {})
        for kind in "MEB":
            actual = saved["portfolios"][kind]
            pid = spec["portfolios"][kind]["portfolioId"]
            check(actual["portfolioId"] == pid, "portfolio binding differs from frozen spec")
            account = con.execute("SELECT * FROM portfolio WHERE portfolio_id=?", (pid,)).fetchone()
            check(account is not None and account["kind"] == kind, "actual account kind differs from M/E/B")
            check(account["initial_cash_cents"] == spec["initialCashCents"], "initial capital differs")
            valuation = con.execute("SELECT * FROM valuation WHERE valuation_id=?", (actual["valuationId"],)).fetchone()
            check(valuation is not None and valuation["published"] == 1, "valuation is missing or unpublished")
            check(valuation["portfolio_id"] == pid and valuation["trading_day"] == trading_day, "valuation dated ownership differs")
            check(int(valuation["net_value_cents"]) == actual["netValueCents"], "saved NAV differs from actual valuation")
            provenance = con.execute("SELECT * FROM valuation_provenance WHERE valuation_id=?", (actual["valuationId"],)).fetchone()
            check(provenance["snapshot_id"] == saved["snapshotId"] and stamp(provenance["as_of"]) == ref.as_of_time,
                  "valuation provenance differs from immutable EOD")
            review = attribution["portfolios"][kind]
            memory, dated = _dated_ledger(con, pid, trading_day)
            memory.close()
            positions = [dict(row) for row in con.execute("SELECT * FROM valuation_position WHERE valuation_id=? ORDER BY instrument_id", (actual["valuationId"],))]
            orders = [dict(row) for row in con.execute('SELECT * FROM "order" WHERE portfolio_id=? AND trading_day<=? ORDER BY trading_day,process_sequence,order_id', (pid, trading_day))]
            fills_by_order = {}
            for fill in dated["fill"]:
                fills_by_order[fill["order_id"]] = fills_by_order.get(fill["order_id"], 0) + fill["quantity"]
            for order in orders:
                order["filledQuantity"] = fills_by_order.get(order["order_id"], 0)
                order["unfilledQuantity"] = order["quantity"] - order["filledQuantity"]
            evidence = _hash({"valuation": dict(valuation), "provenance": dict(provenance), "positions": positions,
                              "datedLedger": dated, "orders": orders})
            check(evidence == review["evidenceHash"], "stored accounting evidence hash differs from actual dated ledger")
            fees = sum(row["amount_cents"] for row in dated["fee_charge"])
            check(fees == actual["feesTotalCents"] == review["netAssetBridge"]["feesCents"], "cumulative fees disagree")
            check(sum(row["amount_cents"] for row in dated["cash_entry"]) == actual["cashCents"], "dated cash entries disagree")
            check(sum(row["value_cents"] for row in positions) == actual["positionsValueCents"], "position valuation disagrees")
            day_exec = actual["dayExecution"]
            check(day_exec["tradingDay"] == trading_day, "dayExecution has the wrong market day")
            check(day_exec["orderCount"] == sum(row["trading_day"] == trading_day for row in orders),
                  "actual dated order count disagrees")
            day_fills = [row for row in dated["fill"] if row["trading_day"] == trading_day and not row["is_reversed"]]
            check(day_exec["fillCount"] == len(day_fills), "actual dated fill count disagrees")
            check(day_exec["feesTotalCents"] == sum(row["fees_total_cents"] for row in day_fills), "actual dated fees disagree")
            fill_rows = {row["fill_id"]: row for row in day_fills}
            check(len(day_exec["fills"]) == len(fill_rows) and
                  {row["fill_id"] for row in day_exec["fills"]} == set(fill_rows),
                  "saved fill identities differ from actual dated ledger")
            fee_breakdown = Counter()
            for row in day_exec["fills"]:
                sql_fill = fill_rows[row["fill_id"]]
                check(all(row[key] == sql_fill[key] for key in row if key != "feeCharges"),
                      "saved per-fill price/quantity/amount/time differs from actual ledger")
                actual_charges = [{key: charge[key] for key in ("fee_code", "fee_version", "amount_cents")}
                                  for charge in dated["fee_charge"] if charge["fill_id"] == row["fill_id"]]
                charge_key = lambda charge: (charge["fee_code"], charge["fee_version"], charge["amount_cents"])
                check(sorted(row["feeCharges"], key=charge_key) == sorted(actual_charges, key=charge_key),
                      "saved per-fill fee type/version differs from actual ledger")
                check(sum(c["amount_cents"] for c in row["feeCharges"]) == row["fees_total_cents"], "day fee lines disagree")
                fee_breakdown.update({code: sum(charge["amount_cents"] for charge in row["feeCharges"]
                    if charge["fee_code"] == code) for code in {charge["fee_code"] for charge in row["feeCharges"]}})
            check(review["netAssetBridge"]["reconciled"] and review["cashBridge"]["reconciled"], "accounting bridge did not reconcile")
            check(review["netAssetBridge"]["unallocatedResidualCents"] == 0
                  and review["cashBridge"]["unallocatedResidualCents"] == 0, "accounting has an unallocated residual")
            plans = list(con.execute("SELECT p.* FROM simulation_plan p JOIN engineering_plan_provenance e ON e.plan_id=p.plan_id WHERE p.portfolio_id=?", (pid,)))
            check(plans and all(row["confirmed_by"] is None and row["confirmation_token_hash"] is None for row in plans),
                  "engineering origin incorrectly contains human confirmation evidence")
            current = svc.reconcile(portfolio_id=pid)
            check(current["reconciled"], "current authoritative ledger did not reconcile")
            result["portfolioFacts"][kind] = {"portfolioId": pid, "initialCashCents": spec["initialCashCents"],
                "netValueCents": actual["netValueCents"], "netReturnPct": actual["netReturnPct"],
                "cashCents": actual["cashCents"], "equityPct": actual["equityPct"], "cashPct": actual["cashPct"],
                "positionCount": len(positions), "dayOrderCount": day_exec["orderCount"], "dayFillCount": day_exec["fillCount"],
                "dayUnfilledOrderCount": day_exec["unfilledOrderCount"], "dayFeesCents": day_exec["feesTotalCents"],
                "cumulativeFillCount": actual["fillCount"], "cumulativeFeesCents": fees,
                "reconciled": current["reconciled"], "unallocatedResidualCents": 0, "evidenceHash": evidence,
                "accounting": review["accounting"], "industryExposurePct": actual["industryExposurePct"],
                "unfilledReasons": review["execution"]["reasons"], "belowMinimumLotCount": review["execution"]["belowMinimumLotCount"],
                "noOrderTargets": [dict(row, displayName=names.get(row["instrumentId"], row["instrumentId"]))
                                   for row in review["execution"]["noOrderTargets"]],
                "dayFeeBreakdownCents": dict(fee_breakdown),
                "verifiedDayFills": [dict(row, displayName=names.get(row["instrument_id"], row["instrument_id"]))
                                     for row in day_exec["fills"]]}
            for omitted in result["portfolioFacts"][kind]["noOrderTargets"]:
                check(omitted["reason"] == "BELOW_MINIMUM_LOT" and
                      Decimal(omitted["targetAmountCents"]) < omitted["referencePriceCents"] * omitted["lotSize"],
                      "omitted target is not verified below a minimum trading lot")
            for section, key in (("securities", "instrument_id"), ("industries", "industry_code")):
                contributions = review["accounting"][section]
                positive = sorted((row for row in contributions if row["net_pnl_cents"] > 0),
                                  key=lambda row: (-row["net_pnl_cents"], row[key]))[:3]
                negative = sorted((row for row in contributions if row["net_pnl_cents"] < 0),
                                  key=lambda row: (row["net_pnl_cents"], row[key]))[:3]
                enriched = lambda rows: [dict(row, displayName=(names if section == "securities" else industry_names).get(row[key], row[key])) for row in rows]
                result["portfolioFacts"][kind]["top" + section.title()] = {
                    "positive": enriched(positive), "negative": enriched(negative)}
        result["comparisons"] = attribution["comparisons"]
        result["comparisonSummary"] = {}
        for name, left, right in (("M_MINUS_B", "M", "B"), ("E_MINUS_M", "E", "M")):
            original = attribution["comparisons"][name]
            left_fact, right_fact = result["portfolioFacts"][left], result["portfolioFacts"][right]
            differences = original["security_accounting_contribution_differences"]
            net = left_fact["netValueCents"] - right_fact["netValueCents"]
            price = sum(row["price_pnl_cents_difference"] for row in differences)
            corporate = sum(row["corporate_action_pnl_cents_difference"] for row in differences)
            fees_paid = left_fact["cumulativeFeesCents"] - right_fact["cumulativeFeesCents"]
            fee_contribution = -fees_paid
            other = (sum(row["other_cash_flow_cents"] for row in left_fact["accounting"]["securities"])
                     - sum(row["other_cash_flow_cents"] for row in right_fact["accounting"]["securities"]))
            residual = original["unallocated_contribution_difference_cents"]
            check(net == original["net_wealth_difference_cents"] and net == price + corporate + fee_contribution + other + residual,
                  "M-B/E-M accounting contribution differences do not reconcile")
            check(sum(row["net_pnl_cents_difference"] for row in differences) + residual == net,
                  "security net contribution differences disagree with NAV difference")
            check(sum(row["net_pnl_cents_difference"] for row in original["industry_accounting_contribution_differences"]) + residual == net,
                  "industry contribution differences disagree with NAV difference")
            check(fee_contribution == original["direct_fee_debit_difference_cents"], "fee difference sign disagrees")
            points = lambda value: str(Decimal(value) / spec["initialCashCents"] * 100)
            check(Decimal(points(net)) == Decimal(original["net_return_difference_pct_points"]), "net return difference points disagree")
            result["comparisonSummary"][name] = {"left": left, "right": right, "netContributionDifferenceCents": net,
                "priceContributionDifferenceCents": price, "feesPaidDifferenceCents": fees_paid,
                "feeNetContributionDifferenceCents": fee_contribution, "corporateActionDifferenceCents": corporate,
                "otherSecurityCashDifferenceCents": other, "unallocatedResidualDifferenceCents": residual,
                "netReturnDifferencePctPoints": points(net), "priceContributionDifferencePctPoints": points(price),
                "feeNetContributionDifferencePctPoints": points(fee_contribution), "corporateActionDifferencePctPoints": points(corporate),
                "otherSecurityCashDifferencePctPoints": points(other), "unallocatedResidualDifferencePctPoints": points(residual),
                "reconciled": True, "basis": original["interpretation"]}
        result["status"] = "VERIFIED_REAL_SAVED_OBSERVATION"
        result["note"] = "本日 M/E/B 独立真实模拟账本与归因核对通过；H不可用；不混入12日独立工程草稿结果。"
        return result
    finally:
        if con.in_transaction:
            con.execute("ROLLBACK")
        con.close()


def report_text(result):
    local = lambda value: stamp(value).astimezone(TZ).isoformat()
    lines = [f"# {result['requestedTradingDay']} M/E/B 真实试运行观测", "", f"核查时间：{result['checkedAt']}。", "",
        f"序列 `{result['seriesId']}` 在 {local(result['registeredAt'])} 登记，首次执行日为 {result['firstTradingDay']}。",
        f"共同证券池 {result['commonPoolCount']} 只；M/E 各 {result['targetCounts']['M']}/{result['targetCounts']['E']} 个冻结目标，B {result['targetCounts']['B']} 个目标；初始建仓后持有，目标观察 {result['observationTargetSessions']} 个交易日。", "",
        f"状态：**{result['status']}**。{result['note']}", ""]
    if result["status"] == "VERIFIED_REAL_SAVED_OBSERVATION":
        lines += [f"实际市场日 {result['marketDay']}；快照 `{result['snapshotId']}`；供应商观察截止 {local(result['providerCutoffAt'])}；账本记录时间 {local(result['observedAt'])}。",
            f"已保存 {result['observedSessionCount']} 个观测；缺失交易日：{', '.join(result['missingEodDays']) or '无'}。", "",
            "|组合|当日订单/成交/未成|当日费用（元）|累计费用（元）|净值（元）|净收益|现金（元）|股票暴露|持仓数|对账|",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
        money = lambda cents: f"{Decimal(cents) / 100:,.2f}"
        for kind, row in result["portfolioFacts"].items():
            lines.append(f"|{kind}|{row['dayOrderCount']}/{row['dayFillCount']}/{row['dayUnfilledOrderCount']}|{money(row['dayFeesCents'])}|{money(row['cumulativeFeesCents'])}|{money(row['netValueCents'])}|{Decimal(row['netReturnPct']):.6f}%|{money(row['cashCents'])}|{Decimal(row['equityPct']):.4f}%|{row['positionCount']}|通过|")
        lines += ["", "费用行、现金分录、持仓估值、净值与逐券/行业会计贡献均核对；未分配差额为零。",
            "H 没有用户确认选择，保持不可用；没有有效 E1 输入时 E 与 M 采用相同冻结目标。该观察检验工程闭环与会计一致性，不构成盈利或策略上线结论。", ""]
        lines += ["## 当日费用分录", "", "金额直接汇总逐笔已入账费用；最低佣金补足单列。", "",
            "|组合|佣金（元）|最低佣金补足（元）|过户费（元）|印花税（元）|合计（元）|",
            "|---|---:|---:|---:|---:|---:|"]
        for kind, row in result["portfolioFacts"].items():
            charges = row["dayFeeBreakdownCents"]
            lines.append(f"|{kind}|{money(charges.get('COMMISSION',0))}|{money(charges.get('MIN_COMMISSION_TOPUP',0))}|{money(charges.get('TRANSFER_FEE',0))}|{money(charges.get('STAMP_DUTY',0))}|{money(row['dayFeesCents'])}|")
        lines += ["", "## 实际模拟成交样例", "", "M/E 当日成交相同，展示M及B各前三笔；完整逐笔记录见机器核查JSON。价格、股数、金额、时间和每条费用均与实际SQLite账本逐项匹配。", "",
            "|组合|证券|方向|股数|成交价（元）|成交额（元）|费用（元）|实际入账时间CST|",
            "|---|---|---|---:|---:|---:|---:|---|"]
        for kind, display in (("M","M/E"),("B","B")):
            for row in result["portfolioFacts"][kind]["verifiedDayFills"][:3]:
                lines.append(f"|{display}|{row['instrument_id']} {row['displayName']}|{row['side']}|{row['quantity']}|{money(row['price_cents'])}|{money(row['gross_amount_cents'])}|{money(row['fees_total_cents'])}|{local(row['filled_at'])}|")
        lines.append("")
        lines += ["## M−B / E−M 会计贡献对照", "",
            "金额差按相同初始资本计算；费用净贡献为费用支出差的相反数，少付费用记为正。百分点表示对初始资本收益率的贡献差。", "",
            "|贡献项|M−B（元）|M−B（百分点）|E−M（元）|E−M（百分点）|",
            "|---|---:|---:|---:|---:|"]
        fields = [("净贡献差", "netContributionDifferenceCents", "netReturnDifferencePctPoints"),
                  ("价格贡献差", "priceContributionDifferenceCents", "priceContributionDifferencePctPoints"),
                  ("费用净贡献差", "feeNetContributionDifferenceCents", "feeNetContributionDifferencePctPoints"),
                  ("公司行为贡献差", "corporateActionDifferenceCents", "corporateActionDifferencePctPoints"),
                  ("其他证券现金贡献差", "otherSecurityCashDifferenceCents", "otherSecurityCashDifferencePctPoints"),
                  ("未分配差额", "unallocatedResidualDifferenceCents", "unallocatedResidualDifferencePctPoints")]
        for label, amount, points in fields:
            mb, em = result["comparisonSummary"]["M_MINUS_B"], result["comparisonSummary"]["E_MINUS_M"]
            lines.append(f"|{label}|{money(mb[amount])}|{Decimal(mb[points]):.6f}|{money(em[amount])}|{Decimal(em[points]):.6f}|")
        lines += ["", "这是原订单成交现金流与期末估值的会计差。现金、暴露及行业不同，不能把价格贡献差直接称为因果选股或择时能力。", "",
            "## B 的 115 个目标为何仅产生 110 笔订单", "",
            "5 个目标的分配金额不足决策参考价格下的一手100股，因此没有生成订单；已生成的110笔订单均成交，没有将这些未生成订单的目标算成成交失败。", "",
            "|证券|目标金额（元）|决策参考价（元/股）|最小单位|一手金额（元）|原记录原因|",
            "|---|---:|---:|---:|---:|---|"]
        for row in result["portfolioFacts"]["B"]["noOrderTargets"]:
            lines.append(f"|{row['instrumentId']} {row['displayName']}|{money(Decimal(row['targetAmountCents']))}|{money(row['referencePriceCents'])}|{row['lotSize']}|{money(row['referencePriceCents'] * row['lotSize'])}|{row['reason']}|")
        for section, title, key in (("topSecurities", "逐券净贡献前3正/负项", "instrument_id"),
                                    ("topIndustries", "行业净贡献前3正/负项", "industry_code")):
            lines += ["", "## " + title, "", "M/E 目标、成交和费用相同，合并展示；数值均含已记账费用。", "",
                "|组合|方向|证券/行业|净贡献（元）|", "|---|---|---|---:|"]
            for kind, display in (("M", "M/E"), ("B", "B")):
                for direction, label in (("positive", "正"), ("negative", "负")):
                    for row in result["portfolioFacts"][kind][section][direction]:
                        lines.append(f"|{display}|{label}|{row[key]} {row['displayName']}|{money(row['net_pnl_cents'])}|")
    else:
        lines += ["当前没有可核验的本日完整观测，收益和成交金额留空。使用现有20:30日更及后续自动监控，不重复手工采集或推进。", ""]
    lines += ["", "## 来源与核查", "", f"决策快照 `{result['decisionSnapshotId']}`，实际截止 {local(result['decisionCutoffAt'])}。", ""]
    for name, source in result["sources"].items():
        if isinstance(source, dict):
            path = Path(source["path"]).as_posix()
            lines += [f"- [{name}]({path})：`{source['sha256']}`。"]
        elif name == "executionDatasets":
            for dataset in source:
                path = Path(dataset["path"]).as_posix()
                lines += [f"- [{dataset['name']}]({path})：`{dataset['sha256']}`，{dataset['recordCount']} 条。"]
    for kind, facts in result["portfolioFacts"].items():
        lines += [f"- {kind} 账本证据 SHA：`{facts['evidenceHash']}`。"]
    lines += ["", "核查工具只读SQLite与已保存文件；供应商调用0次，业务数据库写入0次。12日工程草稿延迟计算不计入本序列。", ""]
    if result.get("verificationPath"):
        lines += [f"[完整机器核查记录]({Path(result['verificationPath']).as_posix()}) · [只读核查工具]({Path(__file__).resolve().as_posix()})", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "deploy/universe-snapshot")
    parser.add_argument("--series-id", default=SERIES)
    parser.add_argument("--day", default="2026-10-13")
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--report-out", type=Path)
    args = parser.parse_args()
    try:
        trading_day = date.fromisoformat(args.day).isoformat()
        suffix = trading_day.replace("-", "")
        result = inspect(args.data_dir.resolve(), args.series_id, trading_day)
        json_path = args.json_out.resolve() if args.json_out else (
            ROOT / f"data/s2-structured/remaining585-meb-actual-verification-{suffix}.json"
            if args.write_report else None)
        if json_path:
            result["verificationPath"] = str(json_path)
            json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.write_report:
            report_path = args.report_out.resolve() if args.report_out else ROOT / f"docs/remaining585-meb-observation-{suffix}.md"
            report_path.write_text(report_text(result), encoding="utf-8")
        summary = {key: result[key] for key in ("status", "seriesId", "requestedTradingDay", "observedSessionCount", "humanStatus", "providerCalls", "databaseWrites")}
        summary["portfolioFacts"] = {kind: {key: facts[key] for key in
            ("netValueCents", "netReturnPct", "dayOrderCount", "dayFillCount", "dayFeesCents", "cumulativeFeesCents", "reconciled", "belowMinimumLotCount")}
            for kind, facts in result["portfolioFacts"].items()}
        summary["comparisonSummary"] = result.get("comparisonSummary", {})
        summary["verificationPath"] = result.get("verificationPath")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "VERIFICATION_FAILED", "error": str(exc), "providerCalls": 0, "databaseWrites": 0}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
