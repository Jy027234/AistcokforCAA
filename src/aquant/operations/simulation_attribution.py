"""Append-only accounting reviews of actually saved engineering ledgers.

Historical reviews read dated cash/fills and reconstruct outstanding receivables
at the requested EOD. Later settlement and trading rows cannot rewrite an old
day's contribution. No order, confirmation, valuation or provider call is made.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from aquant.domain.experiments.comparison import ComparisonError, accounting_contributions, human_difference

VERSION = "aquant.simulation-attribution.v1"
ORIGIN = re.compile(r"^(?:eng|fwd)-[0-9a-f]{24}$")


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(value) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _pct(value, denominator):
    return str(Decimal(value) / Decimal(denominator) * 100) if denominator else None


class AttributionStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, origin_id: str, trading_day: str) -> Path:
        if not ORIGIN.fullmatch(origin_id):
            raise ComparisonError("invalid attribution origin id")
        day = date.fromisoformat(trading_day).isoformat()
        return self.root / f"{origin_id}.{day}.attribution.json"

    def read(self, origin_id: str, trading_day: str) -> dict | None:
        path = self._path(origin_id, trading_day)
        if not path.exists():
            return None
        doc = json.loads(path.read_text(encoding="utf-8"))
        body = {key: value for key, value in doc.items() if key != "contentHash"}
        if doc.get("contentHash") != _hash(body) or body["originId"] != origin_id or body["tradingDay"] != trading_day:
            raise ComparisonError("immutable accounting review hash or origin mismatch")
        return doc

    def save(self, report: dict) -> dict:
        path = self._path(report["originId"], report["tradingDay"])
        previous = self.read(report["originId"], report["tradingDay"])
        if previous:
            # The actual original reporting timestamp must remain unchanged.
            old = {key: value for key, value in previous.items() if key not in {"createdAt", "contentHash"}}
            new = {key: value for key, value in report.items() if key != "createdAt"}
            if old != new:
                raise ComparisonError("accounting review already exists with different dated evidence")
            return previous
        body = dict(report, contentHash=_hash(report))
        temporary = self.root / ("." + path.name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_bytes(_canonical(body))
        try:
            try:
                os.link(temporary, path)
            except FileExistsError:
                return self.save(report)
        finally:
            temporary.unlink(missing_ok=True)
        return body

    def list_reports(self, *, subject: str | None = None) -> list[dict]:
        rows = []
        for path in sorted(self.root.glob("*.attribution.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            report = self.read(doc["originId"], doc["tradingDay"])
            if report and (subject is None or report["subjectId"] == subject):
                rows.append(report)
        return sorted(rows, key=lambda row: (row["tradingDay"], row["createdAt"]), reverse=True)


def _dated_ledger(con, portfolio_id: str, day: str):
    """Read-only in-memory projection for the existing contribution function."""
    memory = sqlite3.connect(":memory:")
    memory.row_factory = sqlite3.Row
    rows = {
        "fill": [dict(row) for row in con.execute("SELECT * FROM fill WHERE portfolio_id=? AND trading_day<=? ORDER BY fill_id", (portfolio_id, day))],
        "cash_entry": [dict(row) for row in con.execute("SELECT * FROM cash_entry WHERE portfolio_id=? AND (entry_type='INITIAL_DEPOSIT' OR trading_day<=?) ORDER BY entry_id", (portfolio_id, day))],
        "fee_charge": [dict(row) for row in con.execute("SELECT fc.* FROM fee_charge fc JOIN fill f ON f.fill_id=fc.fill_id WHERE f.portfolio_id=? AND f.trading_day<=? ORDER BY fc.fee_charge_id", (portfolio_id, day))],
        "receivable": [],
    }
    for row in con.execute("SELECT * FROM receivable WHERE portfolio_id=? AND recognized_on<=? ORDER BY receivable_id", (portfolio_id, day)):
        item = dict(row)
        if item["status"] not in {"RECOGNIZED", "SETTLED"}:
            raise ComparisonError("receivable history lacks a supported dated reconstruction")
        if item["settled_on"] is None or item["settled_on"] > day:
            item["status"] = "RECOGNIZED"
            item["settled_on"] = None
            rows["receivable"].append(item)
    for table, records in rows.items():
        columns = [row["name"] for row in con.execute(f'PRAGMA table_info("{table}")')]
        names = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
        memory.execute(f'CREATE TABLE "{table}" ({names})')
        if records:
            memory.executemany(f'INSERT INTO "{table}" ({names}) VALUES ({",".join("?" for _ in columns)})',
                               [tuple(row[name] for name in columns) for row in records])
    return memory, rows


def _portfolio_review(service, *, portfolio_id: str, trading_day: str, snapshot_id: str,
                      initial_cash_cents: int, targets: list[dict], decision_snapshot_id: str,
                      first_trading_day: str, original_proposal: dict) -> dict:
    con, reader = service.con, service.reader
    valuation = con.execute("SELECT * FROM valuation WHERE portfolio_id=? AND trading_day=?", (portfolio_id, trading_day)).fetchone()
    if valuation is None or not valuation["published"]:
        raise ComparisonError("accounting review requires an actually published dated valuation")
    provenance = con.execute("SELECT * FROM valuation_provenance WHERE valuation_id=?", (valuation["valuation_id"],)).fetchone()
    ref = reader.ref(snapshot_id)
    if provenance is None or provenance["snapshot_id"] != snapshot_id or datetime.fromisoformat(provenance["as_of"]) != ref.as_of_time:
        raise ComparisonError("accounting review valuation does not bind the exact original EOD snapshot")
    positions = [dict(row) for row in con.execute("SELECT * FROM valuation_position WHERE valuation_id=? ORDER BY instrument_id", (valuation["valuation_id"],))]
    instruments = {row["instrument_id"]: row for row in reader.instruments(snapshot_id, as_of=ref.as_of_time)}
    industries = {iid: row.get("industry_code") or "UNKNOWN" for iid, row in instruments.items()}
    memory, dated = _dated_ledger(con, portfolio_id, trading_day)
    try:
        accounting = accounting_contributions(memory, portfolio_id=portfolio_id, initial_cash_cents=initial_cash_cents,
            final_valuation={"net_value_cents": int(valuation["net_value_cents"]), "positions": positions}, industries=industries)
    finally:
        memory.close()
    categories = {key: 0 for key in ("initialCashCents", "buySettlementCents", "sellSettlementCents", "feeCashCents", "corporateActionCashCents", "otherCashCents")}
    fee_keys = {(row["fill_id"], row["fee_code"]) for row in dated["fee_charge"]}
    for row in dated["cash_entry"]:
        key = ("initialCashCents" if row["entry_type"] == "INITIAL_DEPOSIT" else
               "feeCashCents" if (row["related_fill_id"], row["entry_type"]) in fee_keys else
               "buySettlementCents" if row["entry_type"] == "TRADE_SETTLEMENT" and row["amount_cents"] < 0 else
               "sellSettlementCents" if row["entry_type"] == "TRADE_SETTLEMENT" else
               "corporateActionCashCents" if row["entry_type"] in {"DIVIDEND_RECEIVABLE_SETTLED", "DIVIDEND_TAX"} else "otherCashCents")
        categories[key] += int(row["amount_cents"])
    cash_residual = int(valuation["cash_available_cents"]) - sum(categories.values())
    if cash_residual:
        raise ComparisonError("dated cash bridge does not reconcile with the original EOD")
    fees = sum(int(row["amount_cents"]) for row in dated["fee_charge"])
    if categories["feeCashCents"] != -fees:
        raise ComparisonError("dated fee charges and cash fee debits disagree")
    orders = [dict(row) for row in con.execute('SELECT * FROM "order" WHERE portfolio_id=? AND trading_day<=? ORDER BY trading_day,process_sequence,order_id', (portfolio_id, trading_day))]
    filled = {}
    for fill in dated["fill"]:
        filled[fill["order_id"]] = filled.get(fill["order_id"], 0) + int(fill["quantity"])
    for order in orders:
        order["filledQuantity"] = filled.get(order["order_id"], 0)
        order["unfilledQuantity"] = int(order["quantity"]) - order["filledQuantity"]
    unfilled = [row for row in orders if row["unfilledQuantity"] > 0]
    requested = sum(int(row["quantity"]) for row in orders)
    reasons = {}
    for order in unfilled:
        reason = order["reject_reason"] or order["status"]
        reasons[reason] = reasons.get(reason, 0) + 1
    initial_orders = {row["instrument_id"] for row in orders if row["trading_day"] == first_trading_day}
    decision_ref = reader.ref(decision_snapshot_id)
    prices = {}
    for quote in sorted(reader.daily_quotes(decision_snapshot_id, as_of=decision_ref.as_of_time), key=lambda row: (row.instrument_id, row.trading_day)):
        if quote.trading_day < date.fromisoformat(first_trading_day):
            prices[quote.instrument_id] = quote
    omissions = []
    for target in targets:
        iid = target["instrument_id"]
        if iid in initial_orders:
            continue
        bar = prices.get(iid)
        lot_size = service._lot_size(iid, date.fromisoformat(first_trading_day)) if bar else None
        amount = Decimal(initial_cash_cents) * Decimal(target["weight_pct"]) / 100
        below_lot = bar is not None and amount < bar.close_cents * lot_size
        omissions.append({"instrumentId": iid, "weightPct": target["weight_pct"], "targetAmountCents": str(amount),
            "referencePriceCents": bar.close_cents if bar else None, "lotSize": lot_size,
            "reason": "BELOW_MINIMUM_LOT" if below_lot else "NO_INITIAL_ORDER_REASON_UNKNOWN"})
    total, equity = int(valuation["net_value_cents"]), int(valuation["positions_value_cents"])
    industry_values = {}
    for position in positions:
        industry = industries.get(position["instrument_id"], "UNKNOWN")
        industry_values[industry] = industry_values.get(industry, 0) + int(position["value_cents"])
    gross = sum(abs(int(row["gross_amount_cents"])) for row in dated["fill"])
    priced_pnl = sum(row["price_pnl_cents"] for row in accounting["securities"])
    action_pnl = sum(row["corporate_action_pnl_cents"] for row in accounting["securities"])
    other = sum(row["other_cash_flow_cents"] for row in accounting["securities"])
    nav_identity = int(valuation["cash_available_cents"]) + int(valuation["cash_frozen_cents"]) + equity + int(valuation["receivables_cents"]) - int(valuation["payables_cents"])
    if nav_identity != total or sum(row["open_receivables_cents"] for row in accounting["securities"]) != int(valuation["receivables_cents"]):
        raise ComparisonError("dated holdings or corporate-action bridge differs from original valuation")
    latest = {"trading_day": trading_day, "net_value_cents": total, "cash_pct": _pct(int(valuation["cash_available_cents"]), total),
        "industry_exposure_pct": {key: _pct(value, total) for key, value in sorted(industry_values.items())}, "positions": positions}
    return {"portfolioId": portfolio_id, "initialCashCents": initial_cash_cents, "valuationId": valuation["valuation_id"],
        "netValueCents": total, "netReturnPct": _pct(total - initial_cash_cents, initial_cash_cents),
        "accounting": accounting,
        "netAssetBridge": {"initialCashCents": initial_cash_cents, "pricePnlCents": priced_pnl, "corporateActionPnlCents": action_pnl,
            "otherSecurityCashCents": other, "feesCents": fees, "unallocatedResidualCents": accounting["unallocated_residual_cents"],
            "finalNetValueCents": total, "reconciled": accounting["reconciled"], "identity": "initial cash + price P&L + corporate-action P&L + other security cash - fees + unallocated residual = final NAV"},
        "cashBridge": {**categories, "finalCashCents": int(valuation["cash_available_cents"]), "unallocatedResidualCents": cash_residual, "reconciled": cash_residual == 0},
        "navComponents": {key: int(valuation[key]) for key in ("cash_available_cents", "cash_frozen_cents", "positions_value_cents", "receivables_cents", "payables_cents", "net_value_cents")},
        "execution": {"orderCount": len(orders), "fillCount": len(dated["fill"]), "unfilledOrderCount": len(unfilled),
            "unfilledOrderPct": _pct(len(unfilled), len(orders)), "unfilledQuantityPct": _pct(sum(row["unfilledQuantity"] for row in unfilled), requested),
            "reasons": reasons, "orders": orders, "targetCount": len(targets), "noOrderTargets": omissions,
            "belowMinimumLotCount": sum(row["reason"] == "BELOW_MINIMUM_LOT" for row in omissions),
            "grossTradedCents": gross, "turnoverGrossPctInitialCapital": _pct(gross, initial_cash_cents)},
        "exposure": {"cashPct": latest["cash_pct"], "equityPct": _pct(equity, total), "industryExposurePct": latest["industry_exposure_pct"],
            "largestIndustryPctEquity": _pct(max(industry_values.values(), default=0), equity),
            "industryHhiEquity": str(sum((Decimal(value) / equity) ** 2 for value in industry_values.values())) if equity else None},
        "originalProposal": original_proposal, "originalTargets": targets,
        "evidenceHash": _hash({"valuation": dict(valuation), "provenance": dict(provenance), "positions": positions,
            "datedLedger": dated, "orders": orders}),
        "differenceInputs": {"initial_cash_cents": initial_cash_cents, "final_net_value_cents": total, "fees_total_cents": fees,
            "series": [latest], "accounting_contributions": accounting},
    }


def _report_header(origin_id, day, snapshot_id, subject, origin_hash):
    return {"schema": VERSION, "originId": origin_id, "originHash": origin_hash, "subjectId": subject,
        "tradingDay": day, "snapshotId": snapshot_id, "createdAt": datetime.now(timezone.utc).isoformat(),
        "formalStrategy": False, "humanConfirmed": False, "basis": "原冻结判断与实际保存账本的会计复盘，不是因果选股、择时或人工能力评价",
        "limitations": ["逐券损益是成交现金流与期末估值的会计恒等式；未识别因果选股/择时/现金效应。",
            "行业按本次不可变 EOD 分类归集，原目标行业另保留。",
            "费用、滑点与成交上限沿用实际工程计划；未验广义市场参考及 H 不计算比较收益。",
            "现金未计未提供的利息；已核验支持的公司行为按账本现金/应收计入，覆盖缺口仍存在。"],
        "causalAttribution": {"selection": None, "timing": None, "holdingPeriod": None, "cash": None, "reason": "尚不足以归因"}}


def save_engineering_attribution(service, *, store: AttributionStore, run: dict) -> dict:
    if run.get("status") != "COMPLETED" or run.get("source") != "ENGINEERING_TRIAL" or run.get("humanConfirmed") is not False:
        raise ComparisonError("accounting review needs an actually completed, explicitly isolated engineering run")
    original = service.con.execute("SELECT * FROM engineering_simulation_run WHERE run_id=?", (run["runId"],)).fetchone()
    if original is None or original["status"] != "COMPLETED":
        raise ComparisonError("engineering result lacks its actual authoritative completed record")
    persisted = json.loads(original["result_json"])
    if _hash(persisted) != original["result_hash"]:
        raise ComparisonError("engineering original result hash mismatch")
    for key in ("runId", "portfolioId", "tradingDay", "executionSnapshotId", "researchContentHash", "initialCashCents"):
        if persisted[key] != run[key]:
            raise ComparisonError("requested engineering review differs from its original result")
    result = _portfolio_review(service, portfolio_id=run["portfolioId"], trading_day=run["tradingDay"],
        snapshot_id=run["executionSnapshotId"], initial_cash_cents=run["initialCashCents"],
        targets=persisted["preview"]["targets"], decision_snapshot_id=run["decisionSnapshotId"],
        first_trading_day=run["tradingDay"], original_proposal={"draftId": run["draftId"],
            "savedAt": run["draftCreatedAt"], "selectionReason": persisted["draft"].get("selectionReason"),
            "researchProposed": persisted["draft"].get("researchProposed"), "projection": persisted["draft"].get("projection")})
    if result["netValueCents"] != persisted["valuation"]["net_value_cents"]:
        raise ComparisonError("dated review differs from the immutable original run valuation")
    report = _report_header(run["runId"], run["tradingDay"], run["executionSnapshotId"], run["subjectId"], original["result_hash"])
    report.update(kind="ENGINEERING_RUN_ACCOUNTING_REVIEW", portfolios={"ENGINEERING_SELECTION": result},
        comparisons={}, comparisonStatus="UNAVAILABLE: standalone engineering choice is not a same-period M/E/H/B comparison")
    return store.save(report)


def save_forward_attribution(service, *, store: AttributionStore, spec: dict, observation: dict) -> dict:
    if observation["seriesId"] != spec["seriesId"]:
        raise ComparisonError("forward observation differs from its actual registered spec")
    portfolios = {}
    for kind, targets_key in (("M", "modelTargets"), ("E", "eventTargets"), ("B", "benchmarkTargets")):
        pid = spec["portfolios"][kind]["portfolioId"]
        if observation["portfolios"][kind]["portfolioId"] != pid:
            raise ComparisonError("forward review account differs from its registered origin")
        portfolios[kind] = _portfolio_review(service, portfolio_id=pid, trading_day=observation["tradingDay"],
            snapshot_id=observation["snapshotId"], initial_cash_cents=spec["initialCashCents"], targets=spec[targets_key],
            decision_snapshot_id=spec["decisionSnapshotId"], first_trading_day=spec["firstTradingDay"],
            original_proposal={"registeredAt": spec["createdAt"], "strategy": spec["experimentVersion"],
                "decisionSnapshotId": spec["decisionSnapshotId"], "decisionCutoffAt": spec["decisionCutoffAt"],
                "eventBuyBlockedInstrumentIds": spec["eventBuyBlockedInstrumentIds"] if kind == "E" else []})
        if portfolios[kind]["netValueCents"] != observation["portfolios"][kind]["netValueCents"]:
            raise ComparisonError("forward review differs from the immutable saved observation")
    differences = {}
    for name, left, right in (("M_MINUS_B", "M", "B"), ("E_MINUS_M", "E", "M")):
        diff = human_difference(portfolios[right]["differenceInputs"], portfolios[left]["differenceInputs"],
                                origin="ENGINEERING_REPLAY", decision_refs=())
        diff.pop("human_origin"); diff.pop("human_decision_refs")
        diff.update(left=left, right=right, interpretation="同日同初始资金的会计贡献差；暴露和现金不同，不等于因果选股超额收益")
        differences[name] = diff
    report = _report_header(spec["seriesId"], observation["tradingDay"], observation["snapshotId"], spec["authorizationSubject"], _hash(observation))
    report.update(kind="FORWARD_MEB_ACCOUNTING_REVIEW", portfolios=portfolios, comparisons=differences,
        human={"status": "UNAVAILABLE", "reason": spec["human"]["reason"]},
        missingEodDays=observation["missingEodDays"], comparisonStatus="AVAILABLE_ACCOUNTING_ONLY")
    return store.save(report)
