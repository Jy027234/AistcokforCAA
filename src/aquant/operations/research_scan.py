"""Whole-list, current financial research; never a formal S2 or execution snapshot."""
from __future__ import annotations

import json
import hashlib
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

from aquant.artifact_paths import resolve_artifact_path
from aquant.domain.fundamentals.fact_repository import FinancialFactRepository
from aquant.domain.fundamentals.versioned import VersionedFinancialFactStore
from aquant.domain.research.s2 import build_s2_signals
from aquant.operations.research_candidates import ResearchCandidateError, file_hash
from aquant.operations.research_industry import resolve_industries, resolve_disclosures

SCHEMA = "aquant.research-candidates.v2"
FACTORS = ("F07", "F08", "F09", "F10")
FORMULA = "signal_rank = rank(F07)/6 + rank(F08)/6 + rank(F09)/6 + rank(F10)/2"
LABELS = {"F07": "归母权益回报", "F08": "现金质量", "F09": "营业收入增长", "F10": "盈利收益率"}


def _decimal(value):
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ResearchCandidateError("non-finite research input")
    return parsed


def _stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _quote_number(row, key):
    try:
        return _decimal(row[key])
    except (InvalidOperation, ValueError, TypeError):
        return None


class _IndexedFacts:
    """Keep the existing PIT selector, but select within each security's facts."""
    def __init__(self, facts):
        by_code = defaultdict(list)
        for fact in facts:
            by_code[fact.instrument_id].append(fact)
        self.stores = {code: VersionedFinancialFactStore(rows) for code, rows in by_code.items()}

    def select_pit(self, cutoff, *, instrument_id=None, **kwargs):
        store = self.stores.get(instrument_id)
        return store.select_pit(cutoff, instrument_id=instrument_id, **kwargs) if store else []


def _history(packet, iid, cutoff, *, include_cutoff_day=False, expected_as_of=None):
    entry = packet.get("bars", {}).get(iid, {})
    cutoff_day = cutoff.astimezone(_stamp("2000-01-01T00:00:00+08:00").tzinfo).date().isoformat()
    rows = sorted((r for r in entry.get("rows", []) if r["trading_day"] < cutoff_day
                   or (include_cutoff_day and r["trading_day"] == cutoff_day)),
                  key=lambda r: r["trading_day"])
    metrics = []
    if len(rows) >= 20 and all(r.get("amount_cents") is not None for r in rows[-20:]):
        amount = sum(_decimal(r["amount_cents"]) for r in rows[-20:]) / Decimal(2000)
        metrics.append({"label": "最近20个已归档交易日平均成交额", "value": str(amount), "unit": "元"})
    if len(rows) >= 21 and all(r.get("close_cents") and r["close_cents"] > 0 for r in (rows[-1], rows[-21])):
        metrics.append({"label": "20日未复权价格变化", "value": str(
            _decimal(rows[-1]["close_cents"]) / _decimal(rows[-21]["close_cents"]) - 1), "unit": "比率"})
    if len(rows) >= 61 and all(r.get("close_cents") and r["close_cents"] > 0 for r in (rows[-6], rows[-61])):
        metrics.append({"label": "60日跳过最近5日未复权价格变化", "value": str(
            _decimal(rows[-6]["close_cents"]) / _decimal(rows[-61]["close_cents"]) - 1), "unit": "比率"})
    end = rows[-1]["trading_day"] if rows else None
    expected = expected_as_of or (cutoff_day if include_cutoff_day else None)
    freshness = "无已归档日线" if end is None else "已覆盖本次研究日" if expected and end == expected else "截至较早交易日" if expected else "逐证券标注归档窗口"
    limitations = [
            "日线窗口逐证券标注；旧窗口不代表今日趋势、流动性或当前模拟资格。",
            "这些价格变化采用未复权归档价格，仅供上下文，不参与财务排名，也不是正式 S1 因子。",
            "盘中累计成交额没有代替20日平均成交额。"]
    if len(rows) < 61:
        limitations.append("未覆盖61个交易日，无法完整计算60日跳过最近5日的价格变化。")
    if len(rows) < 20 or any(r.get("amount_cents") is None for r in rows[-20:]):
        limitations.append("最近20个归档交易日成交额不完整；未计算20日平均成交额，缺项未填零。")
    source = {k: entry[k] for k in ("source", "receipt_id", "content_hash", "observed_at", "source_url") if k in entry}
    refresh = entry.get("latest_refresh")
    if refresh and rows:
        receipt = refresh["receipt"]
        proofs = {entry["receipt_id"]: dict(source), receipt["receipt_id"]: {
            "source": receipt["source_id"], "receipt_id": receipt["receipt_id"],
            "content_hash": receipt["actual_hash"], "observed_at": receipt["observed_at"],
            "source_url": receipt["source_url"]}}
        groups = defaultdict(list)
        for row in rows:
            groups[row.get("source_receipt_id", entry["receipt_id"])].append(row["trading_day"])
        if not set(groups) <= proofs.keys():
            raise ResearchCandidateError("daily history refers to an unknown source segment")
        source["receipts"] = [dict(proofs[rid], window=f"{days[0]} 至 {days[-1]}", rowCount=len(days))
                              for rid, days in groups.items()]
    return {"historyAsOf": end, "freshness": freshness, "expectedAsOf": expected,
        "tradingDayCount": len(rows), "source": source, "window": (
        f"{rows[0]['trading_day']} 至 {end}，{len(rows)} 个已归档交易日" if rows else "未覆盖"),
        "metrics": metrics, "limitations": limitations}


def _industry_records(packet):
    if packet.get("schema") == "aquant.baostock_research_industry.v1":
        return [{"code": r["baostock_code_raw"], "industry": r["industry_raw"],
                 "updateDate": r["classification_asof_raw"]} for r in packet["rows"]]
    return packet["records"]


def _refresh_history(base, refresh):
    """Fresh queries replace their securities; other archived windows remain visible."""
    return dict(base, bars=dict(base.get("bars", {}), **refresh["bars"]),
                captured_from=refresh["captured_from"], captured_to=refresh["captured_to"],
                expected_as_of=refresh["requested_window"]["end"], refreshed_count=len(refresh["bars"]))


def _financial_detail(factors, facts, bindings, quote):
    selected = {facts[v].logical_key: facts[v] for v in factors.fact_version_ids}
    keyed = {(f.metric, f.period_end): f for f in selected.values()}
    end = factors.latest_period_end
    prior = date(end.year - 1, end.month, end.day)
    evidence = []
    for period in sorted({f.period_end for f in selected.values()}):
        binding = bindings[(factors.instrument_id, period.isoformat())]
        ann = binding["announcement"]
        evidence.append({"periodEnd": period.isoformat(), "announcementId": ann["announcement_id"],
            "title": ann["title"], "publishedDate": ann["published_date"], "url": ann["url"],
            "valueVerification": "MATCH" if binding.get("report_content_hash") else "STRUCTURED_BOUND",
            "receiptId": (binding.get("revision_value_verification") or {}).get("receipt_id") or ann["index_receipt_id"],
            "contentHash": binding.get("report_content_hash") or ann["index_page_hash"],
            "availableAt": max(f.available_at for f in selected.values() if f.period_end == period).isoformat(),
            "observedAt": max(f.first_seen_at for f in selected.values() if f.period_end == period).isoformat(),
            "metrics": sorted({f.metric for f in selected.values() if f.period_end == period})})
    eid = {item["periodEnd"]: item["announcementId"] for item in evidence}
    bridge = []

    def ttm(metric, label, endpoint):
        periods = [(endpoint, 1)] if endpoint.month == 12 else [
            (date(endpoint.year - 1, 12, 31), 1), (endpoint, 1),
            (date(endpoint.year - 1, endpoint.month, endpoint.day), -1)]
        components = [{"periodEnd": p.isoformat(), "value": str(keyed[(metric, p)].value),
                       "sign": sign, "evidenceId": eid[p.isoformat()]} for p, sign in periods]
        value = sum((_decimal(c["value"]) * c["sign"] for c in components), Decimal(0))
        result = {"metric": metric, "label": label, "unit": "元", "value": str(value),
            "formula": "上一完整年度 + 本年累计 - 上年同期累计" if len(periods) == 3 else "完整年度",
            "components": components}
        bridge.append(result)
        return result

    attr = ttm("net_profit_attributable", "归母净利润 TTM", end)
    profit = ttm("net_profit_consolidated", "合并净利润 TTM", end)
    cash = ttm("operating_cashflow", "经营现金流 TTM", end)
    revenue = ttm("revenue", "营业收入 TTM", end)
    prior_revenue = ttm("revenue", "上年同期营业收入 TTM", prior)
    equity = {"metric": "average_parent_equity", "label": "期初期末平均归母权益", "unit": "元",
        "value": str((keyed[("parent_equity", prior)].value + keyed[("parent_equity", end)].value) / 2),
        "formula": "(期初归母权益 + 期末归母权益) / 2",
        "components": [{"periodEnd": p.isoformat(), "value": str(keyed[("parent_equity", p)].value),
                        "sign": 0.5, "evidenceId": eid[p.isoformat()]} for p in (prior, end)]}
    bridge.append(equity)
    cap = {"label": "采集时点总市值", "value": quote["marketCapYuan"], "unit": "元",
           "evidenceIds": [quote["evidenceId"]]}

    def input_of(item):
        return {"label": item["label"], "value": item["value"], "unit": item["unit"],
                "evidenceIds": sorted({c["evidenceId"] for c in item["components"]})}

    inputs = {"F07": [input_of(attr), input_of(equity)], "F08": [input_of(cash), input_of(profit)],
              "F09": [input_of(revenue), input_of(prior_revenue)], "F10": [input_of(attr), cap]}
    return bridge, evidence, inputs


def _financial_observation(selected, bindings):
    """Financial companies get original report values, never ordinary-template ratios."""
    if not selected:
        return [], []
    latest = max(f.period_end for f in selected)
    values = [f for f in selected if f.period_end == latest]
    evidence, bridge = {}, []
    labels = {"net_profit_attributable": "原报告期归母净利润", "net_profit_consolidated": "原报告期合并净利润",
              "operating_cashflow": "原报告期经营现金流", "revenue": "原报告期营业收入", "parent_equity": "期末归母权益"}
    for fact in values:
        binding = bindings.get((fact.instrument_id, fact.period_end.isoformat()), {})
        ann = binding.get("announcement", {})
        if (binding.get("status") != "RECONSTRUCTED_READY" or str(ann.get("announcement_id")) != fact.source_document_id
                or _decimal(binding.get("values", {}).get(fact.metric)) != fact.value
                or (binding.get("report_content_hash") or ann.get("index_page_hash")) != fact.content_hash
                or (binding.get("revision_value_verification") and binding["revision_value_verification"].get("status") != "MATCH")
                or fact.currency != "CNY" or fact.raw_unit != "yuan"):
            raise ResearchCandidateError("financial observation differs from admitted report")
        identifier = ann["announcement_id"]
        evidence[identifier] = {"periodEnd": fact.period_end.isoformat(), "announcementId": identifier,
            "title": ann["title"], "publishedDate": ann["published_date"], "url": ann["url"],
            "valueVerification": "MATCH" if binding.get("report_content_hash") else "STRUCTURED_BOUND",
            "receiptId": (binding.get("revision_value_verification") or {}).get("receipt_id") or ann["index_receipt_id"],
            "contentHash": binding.get("report_content_hash") or ann["index_page_hash"]}
        bridge.append({"metric": fact.metric, "label": labels.get(fact.metric, fact.metric), "unit": "元",
            "value": str(fact.value), "formula": "原报告年内累计值；权益为期末值。金融单列，不套用普通工商TTM排名。",
            "components": [{"periodEnd": fact.period_end.isoformat(), "value": str(fact.value), "sign": 1,
                            "evidenceId": identifier}]})
    return bridge, list(evidence.values())


def _financial_template(classification, bridge, binding):
    """Describe comparable original reports without assigning financial scores."""
    groups = {"66": "货币金融服务", "67": "资本市场服务", "68": "保险业", "69": "其他金融业"}
    major = classification.get("majorCode")
    period = bridge[0]["components"][0]["periodEnd"] if bridge else None
    def report_family(proof):
        explicit = proof.get("family")
        match = re.fullmatch(r"RPT_F10_FINANCE_([GBIS])(?:INCOME|CASHFLOW|BALANCE)", proof.get("report", ""))
        encoded = match[1] if match else None
        if explicit is not None and (explicit not in {"G", "B", "I", "S"} or (encoded and explicit != encoded)):
            return None
        return explicit or encoded

    families = {report_family(binding.get("evidence", {}).get(item["metric"], {})) for item in bridge}
    family = next(iter(families)) if len(families) == 1 and None not in families else None
    values = {item["metric"]: item for item in bridge}
    if len(values) != len(bridge):
        raise ResearchCandidateError("financial observation has ambiguous metric scope")
    labels = {"revenue": "原报告期营业收入", "net_profit_attributable": "原报告期归母净利润",
        "net_profit_consolidated": "原报告期合并净利润", "operating_cashflow": "原报告期经营现金流",
        "parent_equity": "期末归母权益"}
    missing = [metric for metric in labels if metric not in values]
    key = "|".join((classification["system"], major, period, family)) if major in groups and period and family else None
    return {"template": "FINANCIAL_" + (major or "UNSPECIFIED"),
        "templateLabel": groups.get(major, "金融企业") + "原报告观察",
        "periodEnd": period, "reportFamily": family,
        "status": "MISSING" if not bridge else "PARTIAL" if missing else "COMPLETE",
        "comparisonKey": key, "missingMetrics": missing,
        "values": [{"metric": metric, "label": label, "unit": "元",
            "value": values[metric]["value"] if metric in values else None,
            "evidenceIds": sorted({c["evidenceId"] for c in values[metric]["components"]}) if metric in values else []}
            for metric, label in labels.items()],
        "limitations": ["收入、利润与现金流为同一原报告的年内累计值；权益为期末值，未年化、未套用普通工商F08。",
            "仅在金融分组、报告期间及报表族一致且有完整口径标记时对照；绝对规模不等于经营质量或推荐分数。",
            "尚未覆盖资本充足率、不良贷款、偿付能力、承保或证券业务分项等专属经营指标。"]}


def _event_context(code, announcements):
    """Separate archived disclosures and bounded hypotheses from financial ranking."""
    if announcements is None:
        return {"window": "尚未覆盖", "queryComplete": False, "documentCount": 0, "observedAt": None,
                "scope": "近期官方公告与外部新闻尚未检索核验。", "missing": "缺少本代码的事件研究输入。"}, [], []
    queried = next((r for r in announcements["per_code"] if r["code"] == code), None)
    window = f"{announcements['window']['start']} 至 {announcements['window']['end']}"
    docs = [r for r in announcements["documents"] if r["code"] == code]
    context = {"window": window, "queryComplete": bool(queried and queried["queryComplete"]),
        "documentCount": len(docs), "observedAt": announcements["captured_to"] if queried else None,
        "scope": "全分类官方索引；仅取最新两份中文非定期报告公告，正文抽取前3000字符。外部新闻未覆盖。",
        "missing": ("本代码未纳入首20候选公告检索，不表示无公告或无风险。" if queried is None else
                    "索引查询未完整结束。" if not queried["queryComplete"] else
                    "部分选中PDF未成功抽取，见采集缺口。" if queried.get("pdfFailures") else
                    "选中全文仅为有限子集，不代表已读窗口内全部公告。")}
    events, paths = [], []
    for doc in docs:
        text = doc["extractedText"]
        match = re.search(r"每(?:10|十)股|发行总额|快递服务业务收入|业务量|本次变动|拟使用|已完成|收到.{0,25}辞职|会议召开时间|现场会议召开", text)
        start = match.start() if match else next((text.find(line) for line in text.splitlines()
            if len(line.strip()) > 20 and not any(word in line for word in ("证券代码", "证券简称", "保证", "公告编号"))
            and not line.strip().startswith("关于")), 0)
        location = next((p for p in doc["pages"] if p["start_char"] <= start < p["end_char"]), None)
        end = min(start + 80, len(text), location["end_char"] if location else len(text))
        page = location["page"] if location else None
        identifier = "event-" + doc["announcementId"]
        events.append({"title": doc["title"], "publishedDate": doc["publishedDate"], "url": doc["url"],
            "quote": text[start:end], "page": page, "startChar": start, "endChar": end,
            "sourceId": identifier, "verificationStatus": "ARCHIVED_PDF_TEXT",
            "receiptId": doc["pdfReceiptId"], "contentHash": doc["pdfContentHash"]})
        title = doc["title"]
        if any(word in title for word in ("分派", "分红", "派息")):
            hypothesis = "权益分派可能改变现金与除权价格；不能仅因分派公告推断价格上涨或新增盈利。"
            verify = "核对原文登记日、除权日与实际方案，并在同口径价格及公司行为数据中确认。"
        elif "债" in title:
            hypothesis = "债券发行或上市可能关联融资结构与后续利息支出；公告标题不能证明净现金已增加。"
            verify = "核对实际发行/上市事项、到账、用途、期限与利率，结合下一财报验证现金和负债变化。"
        elif any(word in title for word in ("减持", "增持", "回购")):
            hypothesis = "持股或回购事项可能改变股份与持有结构；计划不等于完成，不能推断主体交易意图。"
            verify = "区分计划、进展与完成公告，核验实际数量、期间及归档股份变化。"
        elif any(word in title for word in ("董事", "股东会", "股东大会")):
            hypothesis = "该事项涉及公司治理或议案流程；会议提示或董事变更本身不能证明经营改善。"
            verify = "核对议案内容、表决与任职生效情况，区分通知、决议和实际实施。"
        else:
            hypothesis = "该公告提供新的公司事项观察线索；尚未建立经过验证的盈利、现金流或价格影响。"
            verify = "阅读完整原文、明确事项的实际发生状态，再与后续公告和财报交叉核对。"
        paths.append({"observation": f"{doc['publishedDate']} 官方公告：{title}",
                      "hypothesis": hypothesis, "verify": verify, "sourceIds": [identifier]})
    return context, events, paths


def build_research_scan(quote_packet, *, facts, bound, industries, history, names, provenance, announcements=None,
                        industry_supplement=None, industry_disclosures=None):
    """Rank only the stated financial observation pool, retaining every scanned code."""
    rows = quote_packet["rows"]
    if len({r["code"] for r in rows}) != len(rows):
        raise ResearchCandidateError("duplicate codes in current quote snapshot")
    cutoff = max(_stamp(value) for value in [quote_packet["captured_to"],
        (announcements or {}).get("captured_to"), industries.get("captured_to"), history.get("captured_to"),
        (industry_supplement or {}).get("captured_to"), (industry_disclosures or {}).get("captured_to")] if value)
    capture_start = min(_stamp(value) for value in [quote_packet["captured_from"],
        (announcements or {}).get("captured_from"), industries.get("captured_from"), history.get("captured_from"),
        (industry_supplement or {}).get("captured_from"), (industry_disclosures or {}).get("captured_from")] if value)
    # New disclosure observations advance information availability, not the
    # market date of the retained quote packet (including weekend refreshes).
    day = _stamp(quote_packet["captured_to"]).astimezone(
        _stamp("2000-01-01T00:00:00+08:00").tzinfo).date().isoformat()
    quote_mode = quote_packet.get("research_quote_mode", "INTRADAY_RESEARCH")
    closing = quote_mode == "CLOSING_RESEARCH"
    if quote_mode not in {"INTRADAY_RESEARCH", "CLOSING_RESEARCH"}:
        raise ResearchCandidateError("unknown research quote mode")
    universe = quote_packet["universe_codes"]
    if len(universe) != len(set(universe)) or not {r["code"] for r in rows} <= set(universe):
        raise ResearchCandidateError("quote identity differs from the frozen observation list")
    bindings = {(r["instrument_id"], r["period_end"]): r for r in bound["records"]}
    by_version = {f.version_id: f for f in facts}
    indexed = _IndexedFacts(facts)
    industry_by_code = resolve_disclosures(resolve_industries(industries, industry_supplement), industry_disclosures)
    by_code = {r["code"]: r for r in rows}
    candidate_inputs, rejects, templates, caps = [], {}, {}, {}
    aliases = defaultdict(list)
    for row in rows:
        if row.get("org_id"):
            aliases[row["org_id"]].append(row)
    ambiguous = set()
    for alias_rows in aliases.values():
        active = [r for r in alias_rows if (_quote_number(r, "volume_raw") or 0) > 0
                  and r["quote_time_raw"].startswith(day.replace("-", ""))]
        if len(active) > 1:
            ambiguous.update(r["code"] for r in active)
    for code in universe:
        row = by_code.get(code)
        classification = industry_by_code.get(code, {})
        reason = None
        if row is None:
            reason = ("QUOTE_NOT_RETURNED", "本次请求未取得可定位的行情响应。")
        elif row["quote_time_raw"][:8] != day.replace("-", ""):
            reason = ("QUOTE_WRONG_DAY", "报价日期与本次研究日期不同。")
        elif closing and row["quote_time_raw"][8:] < "150000":
            reason = ("QUOTE_BEFORE_CLOSE", "供应商时间尚早于当日15:00，不作为收盘研究排名的市值输入。")
        elif code in ambiguous:
            reason = ("ACTIVE_ALIAS_AMBIGUOUS", "同一官方发行人多个代码同时返回活跃行情，身份尚未唯一确认。")
        elif (_quote_number(row, "volume_raw") or 0) <= 0 or (_quote_number(row, "last_price_raw") or 0) <= 0:
            reason = ("QUOTE_INACTIVE", "本次行情没有当日成交或有效价格；不能据此确认当前市值已更新。")
        elif row.get("batch_lag_seconds", 0) > 300:
            reason = ("QUOTE_STALE_IN_BATCH", "报价时间比同批最新报价落后超过5分钟。")
        elif (_quote_number(row, "total_market_cap_raw") or 0) <= 0:
            reason = ("MARKET_CAP_INVALID", "未取得有限正数的时点总市值。")
        elif classification.get("template", "UNKNOWN") == "UNKNOWN":
            reason = ("INDUSTRY_UNKNOWN", "缺少可核验的行业分类，未推定为普通工商企业。")
        elif classification["template"] == "FINANCIAL":
            reason = ("FINANCIAL_TEMPLATE_SEPARATE", "金融企业单列观察，不使用普通工商现金质量及四因子配方。")
        if reason:
            rejects[code] = reason
        else:
            candidate_inputs.append(code)
            templates[code] = "ORDINARY"
            caps[code] = _decimal(row["total_market_cap_raw"]) * Decimal("100000000")
    evaluated = build_s2_signals(store=indexed, instrument_ids=candidate_inputs, cutoff=cutoff,
        market_cap_by_instrument=caps, industry_template_by_instrument=templates)
    signals = {}
    for result in evaluated:
        if result.signal is None:
            rejects[result.instrument_id] = (result.exclusion_code, result.exclusion_reason)
        else:
            signals[result.instrument_id] = result.signal
    ordered = sorted(signals, key=lambda c: (-signals[c].signal_rank, c))
    positions = {code: pos for pos, code in enumerate(ordered, 1)}
    result_rows = []
    board_counts = defaultdict(Counter)
    for code in universe:
        raw = by_code.get(code)
        prefix = "SH" if code.startswith(("60", "68")) else "SZ" if code.startswith(("00", "30")) else "BJ"
        iid = prefix + "." + code
        board = "STAR" if code.startswith("68") else "GEM" if code.startswith("30") else "BSE" if prefix == "BJ" else "MAIN"
        classification = industry_by_code.get(code, {})
        industry = classification.get("label") or ""
        quote = None
        if raw:
            parsed_cap = _quote_number(raw, "total_market_cap_raw")
            quote = {"asOfTime": datetime.strptime(raw["quote_time_raw"], "%Y%m%d%H%M%S").isoformat() + "+08:00",
                "lastPrice": raw["last_price_raw"], "marketCapYuan": str(parsed_cap * Decimal("100000000")) if parsed_cap is not None else None,
                "receiptId": raw["receipt_id"], "contentHash": raw["content_hash"],
                "evidenceId": f"quote-{code}-{raw['content_hash'][7:19]}", "url": raw["source_url"],
                "providerName": raw["display_name"], "volumeRaw": raw["volume_raw"],
                "observedAt": raw["responded_at"]}
        signal = signals.get(code)
        reason = rejects.get(code)
        card = {"instrumentId": iid, "code": code, "displayName": names.get(code) or (raw or {}).get("display_name") or code,
            "exchange": {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}[prefix], "board": board,
            "industry": industry or "未知行业", "industryAsOf": classification.get("classificationAsOf"),
            "industryClassification": {k: v for k, v in classification.items() if k != "label"},
            "position": positions.get(code), "signalRank": signal.signal_rank if signal else None,
            "status": "RANKED" if signal else "FINANCIAL" if classification.get("template") == "FINANCIAL" else "OBSERVATION",
            "exclusionCode": reason[0] if reason else None, "exclusionReason": reason[1] if reason else None,
            "factors": {}, "factorRanks": {}, "basis": [], "basisEvidence": [], "rankBreakdown": [],
            "financialBridge": [], "evidence": [], "quote": quote, "counterEvidence": [], "impactPaths": [],
            "marketContext": _history(history, iid, cutoff, include_cutoff_day=closing,
                expected_as_of=history.get("expected_as_of")),
            "uncertainties": ["预期是否已被价格消化：没有一致预期及当前完整复权行情，无法判断。",
                "观察窗口：日频财务研究，滚动四季度；本次报价是分批采集的盘后研究输入。" if closing else
                "观察窗口：日频财务研究，滚动四季度；本次报价是分批采集的盘中时点。",
                "未检索核验本次期间的政策、新闻、宏观及监管事件；财报变化不等于事件利好。",
                "行业为归档分类快照，不代表历史行业生效版本。"],
            "risks": [], "tradability": {"simulatable": False, "rule": "RESEARCH_ONLY_NO_EXECUTION_SNAPSHOT",
                "effectiveFrom": day, "detail": "研究观察池独立于可模拟池；当前证券状态、上市年限、流动性及执行快照未全部验证。",
                "repair": "补齐时点证券状态、上市日期、最新日线和板块规则，验证决策/执行快照后再接入模拟。"}}
        if signal:
            for version in signal.factors.fact_version_ids:
                fact = by_version[version]
                binding = bindings.get((code, fact.period_end.isoformat()), {})
                ann = binding.get("announcement", {})
                if (binding.get("status") != "RECONSTRUCTED_READY" or fact.available_at > cutoff
                        or str(ann.get("announcement_id")) != fact.source_document_id
                        or _decimal(binding.get("values", {}).get(fact.metric)) != fact.value
                        or (binding.get("report_content_hash") or ann.get("index_page_hash")) != fact.content_hash
                        or (binding.get("revision_value_verification") and binding["revision_value_verification"].get("status") != "MATCH")):
                    raise ResearchCandidateError(f"{code}: admitted fact differs from the frozen report bundle")
            values = signal.factors.values
            ranks = {"F07": signal.f07_rank, "F08": signal.f08_rank, "F09": signal.f09_rank, "F10": signal.f10_rank}
            bridge, evidence, inputs = _financial_detail(signal.factors, by_version, bindings, quote)
            card.update(factors={k: str(v) for k, v in values.items()}, factorRanks=ranks,
                        financialBridge=bridge, evidence=evidence)
            for key in FACTORS:
                weight = 0.5 if key == "F10" else 1 / 6
                card["rankBreakdown"].append({"factorId": key, "name": LABELS[key], "value": str(values[key]),
                    "rankPct": ranks[key], "weight": weight, "contribution": weight * ranks[key],
                    "formula": {"F07": "归母净利润TTM / 期初期末平均归母权益", "F08": "经营现金流TTM / 合并净利润TTM",
                                "F09": "营业收入TTM / 上年同期TTM - 1", "F10": "归母净利润TTM / 采集时点总市值"}[key],
                    "inputs": inputs[key]})
            strongest = sorted(card["rankBreakdown"], key=lambda x: (-x["contribution"], x["factorId"]))[:3]
            for item in strongest:
                statement = f"{item['name']}：比率 {values[item['factorId']]:.6f}，在 {len(signals)} 只普通工商完整财务观察池的因子百分位为 {item['rankPct'] * 100:.2f}%；按权重 {item['weight']:.4f} 贡献 {item['contribution']:.6f}。"
                sources = sorted({eid for inp in item["inputs"] for eid in inp["evidenceIds"]})
                card["basis"].append(statement)
                card["basisEvidence"].append({"statement": statement, "sourceIds": sources})
            if values["F09"] < 0:
                card["counterEvidence"].append({"statement": f"收入TTM同比 {values['F09'] * 100:.2f}%，收入收缩削弱增长解释。",
                    "noneFound": False, "sourceIds": sorted({e for inp in inputs["F09"] for e in inp["evidenceIds"]})})
            if values["F08"] < 1:
                card["counterEvidence"].append({"statement": f"经营现金流/合并利润 {values['F08']:.6f}，现金生成低于账面利润；需核对营运资本与现金流构成。",
                    "noneFound": False, "sourceIds": sorted({e for inp in inputs["F08"] for e in inp["evidenceIds"]})})
            weak = min(FACTORS, key=lambda k: (ranks[k], k))
            if ranks[weak] < 0.5:
                card["counterEvidence"].append({"statement": f"{LABELS[weak]}的池内百分位仅 {ranks[weak] * 100:.2f}%，低于中位位置，四项表现并不一致。",
                    "noneFound": False, "sourceIds": sorted({e for inp in inputs[weak] for e in inp["evidenceIds"]})})
            if not card["counterEvidence"]:
                card["counterEvidence"] = [{"statement": "在本次归档财报数值与四因子比较范围内未找到上述反证；未覆盖新闻、监管事件与预期变化，不表示不存在反证。",
                    "noneFound": True, "sourceIds": [e["announcementId"] for e in evidence]}]
            card["impactPaths"] = [{"observation": f"收入TTM同比 {values['F09'] * 100:.2f}%；现金质量 {values['F08']:.6f}。",
                "hypothesis": "财报收入变化可能通过利润与现金生成影响权益回报；盈利收益率还受市场价格变化影响。这是待检验的财务路径，不是上涨结论。",
                "verify": "下一定期报告核对收入、合并利润及经营现金流；结合最新复权价格与公告检验市场是否已反映。",
                "sourceIds": [e["announcementId"] for e in evidence]}]
            if any(e["valueVerification"] == "STRUCTURED_BOUND" for e in evidence):
                card["risks"].append("部分数值是结构化财报与公告绑定，尚未逐份完成PDF数值复核。")
        else:
            if classification.get("template") == "FINANCIAL":
                selected = indexed.select_pit(cutoff, instrument_id=code)
                card["financialBridge"], card["evidence"] = _financial_observation(selected, bindings)
                period = card["financialBridge"][0]["components"][0]["periodEnd"] if card["financialBridge"] else None
                card["financialObservation"] = _financial_template(classification, card["financialBridge"], bindings.get((code, period), {}))
                card["uncertainties"].append("银行、保险、证券业务口径分别适用；本版只列原始财报值，尚未实现独立金融行业排名模板。")
            card["counterEvidence"] = [{"statement": "尚未形成适用且完整的财务解释，不能宣称已检索到利好或已排除反证。", "noneFound": False, "sourceIds": []}]
            card["uncertainties"].append(reason[1])
        if raw and "ST" in raw["display_name"].upper():
            card["risks"].append("供应商当前名称含ST风险线索；尚未用官方状态版本核验，不具备模拟资格。")
        card["risks"].append("盘后报价和当前观察的历史行情、重建财务用于研究；不是正式决策/执行快照或时点正确的S2回测。" if closing else
            "盘中报价和历史重建财务共同用于当前研究；不是收盘快照或时点正确的正式S2回测。")
        card["eventContext"], card["events"], event_paths = _event_context(code, announcements)
        card["impactPaths"].extend(event_paths)
        if card["eventContext"]["queryComplete"]:
            card["uncertainties"][2] = "事件研究仅覆盖所列官方索引窗口与最新两份非定期公告全文片段；政策、外部新闻、宏观及其它全文未全面核验。"
        result_rows.append(card)
        board_counts[board]["requested"] += 1
        board_counts[board]["observed"] += bool(raw)
        board_counts[board]["ranked"] += bool(signal)
    result_rows.sort(key=lambda r: (r["position"] is None, r["position"] or 0, r["code"]))
    return {"schema": SCHEMA, "strategy": "S2_RESEARCH", "pitMode": "HISTORICAL_RECONSTRUCTED",
        "quoteMode": quote_mode, "researchOnly": True, "simulationAvailable": False,
        "generatedAt": datetime.now(timezone.utc).isoformat(), "dataAsOf": day, "asOfTime": cutoff.isoformat(),
        "capturedFrom": capture_start.isoformat(), "capturedTo": cutoff.isoformat(),
        "quoteCapturedFrom": quote_packet["captured_from"], "quoteCapturedTo": quote_packet["captured_to"],
        "poolSize": len(universe), "rankedCount": len(signals), "excludedCount": len(universe) - len(signals),
        "returnedCount": len(result_rows), "candidates": result_rows, "rankingFormula": FORMULA,
        "comparisonScope": f"{len(signals)} 只身份与当前有效市值明确、归档非金融行业且四因子完整的普通工商财务观察证券；不等于可模拟池。",
        "scope": {"universeCodeCount": len(universe), "observedCount": len(rows), "quoteRejectedCount": len(universe) - len(rows),
            "financialCount": sum(r["status"] == "FINANCIAL" for r in result_rows),
            "unknownIndustryCount": sum(r["industryClassification"].get("template", "UNKNOWN") == "UNKNOWN" for r in result_rows),
            "byBoard": [{"board": board, **counts} for board, counts in sorted(board_counts.items())]},
        "exclusions": [{"code": code, "count": count} for code, count in sorted(Counter(x[0] for x in rejects.values()).items())],
        "source": {"marketCapSource": "tencent-qt", "marketCapAsOf": day, "archiveVerified": True,
                   "placeholderUsed": False, "factorReport": "本版直接由冻结盘后行情与重建事实库计算" if closing else "本版直接由冻结盘中行情与重建事实库计算"},
        "limitations": ["官方观察清单含历史代码和旧新代码，覆盖数是证券代码数，不是独立上市公司数。",
            "普通工商财务观察排名与金融单列、缺项观察分开；没有套用旧900只工程样本。",
            "本次为分批盘后研究，没有发布正式决策/执行快照、正式S2、模拟订单或收益结论。" if closing else
            "本次为分批盘中研究，没有发布今日收盘sidecar、正式S2、模拟订单或收益结论。",
            "事件全文仅覆盖此前首批20个候选代码的有限官方公告子集，未随本版排序自动扩展；政策、新闻、宏观与监管事件未全范围检索核验。"], "provenance": provenance,
        "historyCoverage": {"refreshedCount": history.get("refreshed_count", 0),
            "rankedCurrentCount": sum(r["status"] == "RANKED" and r["marketContext"]["historyAsOf"] == day for r in result_rows),
            "rankedTotal": len(signals)},
        "industryCoverage": {"primaryCount": sum(r["industryClassification"].get("selection") == "PRIMARY" for r in result_rows),
            "supplementCount": sum(r["industryClassification"].get("selection") in {
                "OFFICIAL_SUPPLEMENT_FOR_PRIMARY_GAP", "OFFICIAL_DISCLOSURE_FOR_PRIMARY_GAP"} for r in result_rows),
            "unknownCount": sum(r["industryClassification"].get("template", "UNKNOWN") == "UNKNOWN" for r in result_rows),
            "activeUnknownCount": sum(r["exclusionCode"] == "INDUSTRY_UNKNOWN" for r in result_rows),
            "systems": [{"system": system, "version": version, "count": count} for (system, version), count in sorted(Counter(
                (r["industryClassification"]["system"], r["industryClassification"]["version"]) for r in result_rows
                if r["industryClassification"].get("template", "UNKNOWN") != "UNKNOWN").items())],
            "selectionRule": "优先保留非空BaoStock证监会行业快照；官方补充只填精确代码缺项，CAPCO与公司披露GB/T口径独立标记，不推断历史生效日。"},
        "eventCoverage": {"queriedCodes": len(announcements["per_code"]) if announcements else 0,
            "completeQueries": sum(p["queryComplete"] for p in announcements["per_code"]) if announcements else 0,
            "archivedDocuments": len(announcements["documents"]) if announcements else 0}}


@lru_cache(maxsize=2)
def rebuild_scan(sources):
    paths = {role: resolve_artifact_path(path) for role, path, _ in sources}
    required = {"quotes", "bound_records", "facts_db", "industry_snapshot", "price_history", "company_names"}
    if not required <= paths.keys():
        raise ResearchCandidateError("missing research scan inputs")
    store, _ = FinancialFactRepository.open_existing(paths["facts_db"]).load_with_accepted_s2_pdf_reviews()
    read = lambda role: json.loads(paths[role].read_text(encoding="utf-8"))
    names = {r["code"]: r.get("zwjc", r["code"]) for r in read("company_names")["stockList"]}
    history = read("price_history")
    if "price_history_refresh" in paths:
        history = _refresh_history(history, read("price_history_refresh"))
    packet = build_research_scan(read("quotes"), facts=store.facts, bound=read("bound_records"),
        industries=read("industry_snapshot"), history=history, names=names, provenance=[],
        announcements=read("announcements") if "announcements" in paths else None,
        industry_supplement=read("industry_supplement") if "industry_supplement" in paths else None,
        industry_disclosures=read("industry_disclosures") if "industry_disclosures" in paths else None)
    packet["capturedTo"] = packet["asOfTime"]
    packet["snapshotId"] = ("research-closing-" if packet["quoteMode"] == "CLOSING_RESEARCH" else "research-intraday-") + hashlib.sha256(
        "|".join(digest for _, _, digest in sources).encode()).hexdigest()[:20]
    return packet


def load_research_scan(path, *, limit=20, offset=0, q="", board="", industry="", view="candidates"):
    if not 1 <= limit <= 100 or offset < 0 or view not in {"candidates", "all", "financial"}:
        raise ResearchCandidateError("invalid research scan query")
    packet = json.loads(resolve_artifact_path(path).read_text(encoding="utf-8"))
    if packet.get("schema") != SCHEMA or packet.get("researchOnly") is not True or packet.get("simulationAvailable") is not False:
        raise ResearchCandidateError("not a read-only current research scan")
    provenance = packet["provenance"]
    for item in provenance:
        if file_hash(Path(item["path"])) != item["contentHash"]:
            raise ResearchCandidateError("frozen research input has changed: " + item["role"])
    expected = rebuild_scan(tuple(sorted((s["role"], s["path"], s["contentHash"]) for s in provenance)))
    if any(packet.get(k) != v for k, v in expected.items() if k not in {"generatedAt", "provenance"}):
        raise ResearchCandidateError("research explanations differ from frozen inputs")
    query = q.strip().casefold()
    chosen = [r for r in packet["candidates"] if
        (view == "all" or (view == "candidates" and r["status"] == "RANKED") or (view == "financial" and r["status"] == "FINANCIAL"))
        and (not query or any(query in str(r[k]).casefold() for k in ("code", "instrumentId", "displayName")))
        and (not board or r["board"] == board)
        and (not industry or industry.casefold() in r["industry"].casefold())]
    return dict(packet, candidates=chosen[offset:offset + limit], offset=offset, filteredCount=len(chosen),
                returnedCount=len(chosen[offset:offset + limit]), nextOffset=offset + limit if offset + limit < len(chosen) else None)
