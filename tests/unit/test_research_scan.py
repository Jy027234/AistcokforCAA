"""Research pool suitability, explanations and whole-list query semantics."""
import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from test_s2_formula_engine import _facts
from aquant.domain.data.pit import AvailabilityBasis, PitMode
from aquant.operations.research_candidates import ResearchCandidateError, file_hash
from aquant.operations.research_scan import build_research_scan, load_research_scan, _refresh_history


def inputs():
    codes = ["600001", "600002", "600003", "920001"]
    stamp = datetime(2026, 8, 17, tzinfo=timezone.utc)
    facts = [replace(f, value=Decimal(f.value), content_hash="sha256:" + "a" * 64,
        available_at=stamp, first_seen_at=stamp, ingested_at=stamp,
        availability_basis=AvailabilityBasis.RECONSTRUCTED, pit_mode=PitMode.HISTORICAL_RECONSTRUCTED)
        for code in codes[:3] for f in _facts(code)]
    bound = {}
    for fact in facts:
        key = (fact.instrument_id, fact.period_end.isoformat())
        row = bound.setdefault(key, {"instrument_id": key[0], "period_end": key[1],
            "status": "RECONSTRUCTED_READY", "values": {}, "announcement": {
                "announcement_id": fact.source_document_id, "title": "fixture report", "published_date": "2026-08-15",
                "url": "https://static.cninfo.com.cn/fixture.PDF", "index_receipt_id": "fixture-receipt",
                "index_page_hash": fact.content_hash}})
        row["values"][fact.metric] = str(fact.value)
    rows = [{"code": c, "org_id": c, "display_name": "fixture", "quote_time_raw": "20261009114530",
        "volume_raw": "100", "last_price_raw": "10", "total_market_cap_raw": "0.00001",
        "receipt_id": "receipt-" + c, "content_hash": "sha256:" + "b" * 64,
        "responded_at": "2026-10-09T03:46:00+00:00", "source_url": "https://qt.gtimg.cn/q=sh" + c}
        for c in codes]
    quotes = {"universe_codes": codes, "rows": rows,
        "captured_from": "2026-10-09T03:45:30+00:00", "captured_to": "2026-10-09T03:46:00+00:00"}
    industries = {"records": [{"code": c, "industry": industry, "updateDate": "2026-09-14"}
        for c, industry in zip(codes, ("C39工业", "C39工业", "J66金融", ""))]}
    return quotes, dict(facts=facts, bound={"records": list(bound.values())}, industries=industries,
                        history={"bars": {}}, names={c: "fixture-" + c for c in codes}, provenance=[])


def test_full_observation_list_and_financial_template_are_separate():
    quotes, kwargs = inputs()
    packet = build_research_scan(quotes, **kwargs)
    assert packet["poolSize"] == 4 and packet["rankedCount"] == 2
    financial = next(r for r in packet["candidates"] if r["code"] == "600003")
    assert financial["status"] == "FINANCIAL" and financial["signalRank"] is None
    assert financial["financialBridge"] and not financial["factors"]
    assert not financial["counterEvidence"][0]["noneFound"]
    assert packet["scope"]["unknownIndustryCount"] == 1
    assert not packet["simulationAvailable"]


def test_later_disclosure_refresh_keeps_quote_market_day_and_ranked_candidates():
    quotes, kwargs = inputs()
    prior = build_research_scan(quotes, **kwargs)
    later = build_research_scan(quotes, **kwargs, announcements={
        "captured_from": "2026-10-11T15:25:43+00:00", "captured_to": "2026-10-11T15:26:31+00:00",
        "window": {"start": "2026-09-11", "end": "2026-10-11"}, "per_code": [], "documents": []})
    assert later["dataAsOf"] == prior["dataAsOf"] == "2026-10-09"
    assert later["asOfTime"].startswith("2026-10-11")
    assert later["rankedCount"] == prior["rankedCount"] == 2
    assert [(c["code"], c["signalRank"]) for c in later["candidates"]] == [
        (c["code"], c["signalRank"]) for c in prior["candidates"]]


def test_complete_ttm_bridge_formula_contribution_and_sourced_counter_evidence():
    quotes, kwargs = inputs()
    card = build_research_scan(quotes, **kwargs)["candidates"][0]
    assert Decimal(card["factors"]["F08"]) == Decimal("0.5")
    assert next(x for x in card["financialBridge"] if x["label"] == "归母净利润 TTM")["value"] == "110"
    assert sum(x["contribution"] for x in card["rankBreakdown"]) == pytest.approx(card["signalRank"])
    assert [x["weight"] for x in card["rankBreakdown"]] == [1/6, 1/6, 1/6, 1/2]
    assert len(card["basisEvidence"]) <= 3
    assert any("现金生成低于账面利润" in c["statement"] and c["sourceIds"] for c in card["counterEvidence"])


@pytest.mark.parametrize("change,expected", [("inactive", "QUOTE_INACTIVE"), ("cap_missing", "MARKET_CAP_INVALID"),
    ("stale", "QUOTE_STALE_IN_BATCH"), ("wrong_day", "QUOTE_WRONG_DAY"), ("alias", "ACTIVE_ALIAS_AMBIGUOUS")])
def test_bad_current_quotes_remain_observable_without_ranking(change, expected):
    quotes, kwargs = inputs()
    row = quotes["rows"][0]
    if change == "inactive": row["volume_raw"] = "0"
    if change == "cap_missing": row["total_market_cap_raw"] = ""
    if change == "stale": row["batch_lag_seconds"] = 301
    if change == "wrong_day": row["quote_time_raw"] = "20261008114530"
    if change == "alias": quotes["rows"][1]["org_id"] = row["org_id"]
    packet = build_research_scan(quotes, **kwargs)
    card = next(c for c in packet["candidates"] if c["code"] == row["code"])
    assert card["signalRank"] is None and card["exclusionCode"] == expected
    assert packet["poolSize"] == 4


def test_unavailable_facts_are_not_replaced_by_zero_or_another_report():
    quotes, kwargs = inputs()
    kwargs["facts"] = [f for f in kwargs["facts"] if not (f.instrument_id == "600001" and f.metric == "revenue" and f.period_end.year == 2024)]
    card = next(c for c in build_research_scan(quotes, **kwargs)["candidates"] if c["code"] == "600001")
    assert card["signalRank"] is None and card["exclusionCode"] == "S2_TTM_INCOMPLETE"


def test_query_searches_entire_frozen_list_and_tampered_explanations_are_rejected(tmp_path, monkeypatch):
    quotes, kwargs = inputs()
    packet = build_research_scan(quotes, **kwargs)
    source = tmp_path / "frozen.json"
    source.write_text("{}", encoding="utf-8")
    packet["provenance"] = [{"role": "fixture", "path": str(source), "contentHash": file_hash(source)}]
    expected = dict(packet)
    monkeypatch.setattr("aquant.operations.research_scan.rebuild_scan", lambda s: expected)
    path = tmp_path / "scan.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    assert load_research_scan(path, limit=1, offset=1)["candidates"][0]["position"] == 2
    result = load_research_scan(path, view="all", q="920001")
    assert result["filteredCount"] == 1 and result["candidates"][0]["signalRank"] is None
    assert load_research_scan(path, view="financial")["candidates"][0]["code"] == "600003"
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["candidates"][0]["basisEvidence"][0]["statement"] = "invented claim"
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ResearchCandidateError, match="explanations"):
        load_research_scan(path)


def test_real_event_excerpt_is_separate_from_hypothesis_and_unsearched_is_not_no_events():
    quotes, kwargs = inputs()
    text = "本次权益分派方案：每10股派发现金1元；具体股权登记日请查阅完整公告。"
    kwargs["announcements"] = {"captured_from": "2026-10-09T04:01:00+00:00", "captured_to": "2026-10-09T04:03:00+00:00",
        "window": {"start": "2026-09-09", "end": "2026-10-09"},
        "per_code": [{"code": "600001", "queryComplete": True, "pdfFailures": []}],
        "documents": [{"code": "600001", "announcementId": "eventdoc", "title": "权益分派实施公告",
            "publishedDate": "2026-09-22", "url": "https://static.cninfo.com.cn/event.PDF", "extractedText": text,
            "pdfReceiptId": "pdfr", "pdfContentHash": "sha256:" + "c" * 64,
            "pages": [{"page": 1, "start_char": 0, "end_char": len(text)}]}]}
    packet = build_research_scan(quotes, **kwargs)
    one = next(r for r in packet["candidates"] if r["code"] == "600001")
    assert one["eventContext"]["queryComplete"] and one["events"][0]["page"] == 1
    ev = one["events"][0]
    assert ev["quote"] == text[ev["startChar"]:ev["endChar"]]
    assert "不能" in one["impactPaths"][-1]["hypothesis"]
    two = next(r for r in packet["candidates"] if r["code"] == "600002")
    assert not two["eventContext"]["queryComplete"] and "不表示无公告" in two["eventContext"]["missing"]
    assert packet["asOfTime"] == "2026-10-09T04:03:00+00:00"
    assert packet["quoteCapturedTo"] == "2026-10-09T03:46:00+00:00"
    quotes.update(captured_from="2026-10-09T09:11:00+00:00", captured_to="2026-10-09T09:14:00+00:00")
    closing_capture = build_research_scan(quotes, **kwargs)
    assert closing_capture["capturedFrom"] == "2026-10-09T04:01:00+00:00"
    assert closing_capture["quoteCapturedFrom"] == "2026-10-09T09:11:00+00:00"
    assert closing_capture["capturedTo"] == closing_capture["asOfTime"] == "2026-10-09T09:14:00+00:00"


def test_event_quote_locator_never_labels_cross_page_text_as_one_page():
    from aquant.operations.research_scan import _event_context
    text = "每10股派发现金1元。" + "第2页内容" * 20
    boundary = 15
    announcements = {"window": {"start": "2026-09-09", "end": "2026-10-09"}, "captured_to": "2026-10-09T04:03:00+00:00",
        "per_code": [{"code": "600001", "queryComplete": True}], "documents": [{"code": "600001",
            "announcementId": "split", "title": "权益分派", "publishedDate": "2026-09-22", "url": "https://static.cninfo.com.cn/f.PDF",
            "extractedText": text, "pdfReceiptId": "r", "pdfContentHash": "sha256:" + "c" * 64,
            "pages": [{"page": 1, "start_char": 0, "end_char": boundary},
                      {"page": 2, "start_char": boundary, "end_char": len(text)}]}]}
    event = _event_context("600001", announcements)[1][0]
    assert event["page"] == 1 and event["endChar"] == boundary
    assert event["quote"] == text[:boundary]


def test_closing_research_rejects_early_quotes_and_uses_completed_day_context():
    quotes, kwargs = inputs()
    quotes.update(research_quote_mode="CLOSING_RESEARCH", captured_to="2026-10-09T09:15:00+00:00")
    quotes["rows"][0]["quote_time_raw"] = "20261009161500"
    kwargs["history"] = {"bars": {"SH.600001": {"rows": [
        {"trading_day": "2026-10-08", "close_cents": 1000, "amount_cents": 500},
        {"trading_day": "2026-10-09", "close_cents": 1100, "amount_cents": 600},
        {"trading_day": "2026-10-12", "close_cents": 9999, "amount_cents": 9999}]}}}
    packet = build_research_scan(quotes, **kwargs)
    cards = {r["code"]: r for r in packet["candidates"]}
    assert packet["quoteMode"] == "CLOSING_RESEARCH" and not packet["simulationAvailable"]
    assert packet["rankedCount"] == 1
    assert cards["600002"]["exclusionCode"] == "QUOTE_BEFORE_CLOSE"
    context = cards["600001"]["marketContext"]
    assert context["historyAsOf"] == context["expectedAsOf"] == "2026-10-09"
    assert context["tradingDayCount"] == 2
    assert context["freshness"] == "已覆盖本次研究日"
    assert packet["historyCoverage"]["rankedCurrentCount"] == 1
    # During trading, a provider's provisional same-day bar is not a full-day bar.
    quotes["research_quote_mode"] = "INTRADAY_RESEARCH"
    intraday = build_research_scan(quotes, **kwargs)
    old = next(r for r in intraday["candidates"] if r["code"] == "600001")
    assert old["marketContext"]["historyAsOf"] == "2026-10-08"


def test_history_refresh_replaces_selected_windows_preserves_others_and_does_not_fill_missing_amount():
    from aquant.operations.research_scan import _history
    base = {"bars": {"SH.600001": {"rows": [{"trading_day": "2026-09-01"}]},
                      "SH.600002": {"rows": [{"trading_day": "2026-09-14"}]}}}
    dates = [f"2026-09-{day:02d}" for day in range(1, 21)]
    refresh = {"bars": {"SH.600001": {"source": "tencent-ifzq", "receipt_id": "raw-r",
        "content_hash": "hash", "observed_at": "2026-10-09T09:21:00+00:00",
        "rows": [{"trading_day": d, "close_cents": 1000, "amount_cents": None} for d in dates]}},
        "captured_from": "2026-10-09T09:20:00+00:00", "captured_to": "2026-10-09T09:21:00+00:00",
        "requested_window": {"end": "2026-10-09"}}
    merged = _refresh_history(base, refresh)
    assert merged["bars"]["SH.600002"] == base["bars"]["SH.600002"]
    assert len(merged["bars"]["SH.600001"]["rows"]) == 20 and merged["refreshed_count"] == 1
    context = _history(merged, "SH.600001", datetime(2026, 10, 9, 9, tzinfo=timezone.utc),
                       include_cutoff_day=True, expected_as_of=merged["expected_as_of"])
    assert context["historyAsOf"] == "2026-09-20" and context["freshness"] == "截至较早交易日"
    assert context["metrics"] == [] and context["source"]["receipt_id"] == "raw-r"
    assert any("未填零" in text for text in context["limitations"])


def test_current_industry_preserves_provider_classification_date_and_latest_input_cutoff():
    quotes, kwargs = inputs()
    records = kwargs["industries"]["records"]
    kwargs["industries"] = {"schema": "aquant.baostock_research_industry.v1",
        "captured_to": "2026-10-09T09:14:39+00:00", "rows": [
            {"baostock_code_raw": "sh." + r["code"], "industry_raw": r["industry"],
             "classification_asof_raw": "2026-10-05"} for r in records]}
    kwargs["history"].update(captured_to="2026-10-09T09:30:00+00:00")
    packet = build_research_scan(quotes, **kwargs)
    assert packet["asOfTime"] == "2026-10-09T09:30:00+00:00"
    assert all(r["industryAsOf"] == "2026-10-05" for r in packet["candidates"])
    assert packet["scope"]["financialCount"] == 1


def test_incremental_day_has_its_own_source_proof_and_intraday_does_not_show_unused_segment():
    from aquant.operations.research_scan import _history
    receipt = {"source_id": "baostock-socket", "receipt_id": "today-r", "actual_hash": "today-h",
               "observed_at": "2026-10-09T09:44:00+00:00", "source_url": "baostock://today"}
    history = {"bars": {"SH.600001": {"source": "baostock-socket", "receipt_id": "history-r",
        "content_hash": "history-h", "observed_at": "2026-10-09T09:16:00+00:00",
        "source_url": "baostock://history", "latest_refresh": {"receipt": receipt}, "rows": [
            {"trading_day": "2026-10-08", "close_cents": 1000},
            {"trading_day": "2026-10-09", "close_cents": 1100, "source_receipt_id": "today-r"}]}}}
    cutoff = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)
    closed = _history(history, "SH.600001", cutoff, include_cutoff_day=True)
    proofs = closed["source"]["receipts"]
    assert [p["receipt_id"] for p in proofs] == ["history-r", "today-r"]
    assert [p["window"] for p in proofs] == ["2026-10-08 至 2026-10-08", "2026-10-09 至 2026-10-09"]
    assert proofs[1]["content_hash"] == "today-h" and proofs[1]["rowCount"] == 1
    intraday = _history(history, "SH.600001", cutoff)
    assert len(intraday["source"]["receipts"]) == 1
    assert intraday["source"]["receipts"][0]["receipt_id"] == "history-r"


def supplement_fixture(code="920001", category="C"):
    label = "金融业" if category == "J" else "制造业"
    return {"schema": "aquant.capco_research_industry_supplement.v1",
        "captured_from": "2026-10-09T12:57:00+00:00", "captured_to": "2026-10-09T13:00:00+00:00",
        "taxonomy": {"classification_asof_date": None, "reference_period": "2026年上半年",
            "result_publication_date": "2026-09-30", "financial_category": {"code": "J", "label": "金融业"},
            "category_pairs_in_pdf": [{"code": category, "label": label}]},
        "sources": {"classification_pdf": {"url": "https://sp.capco.org.cn:82/file/fixture.pdf",
            "receipt_id": "capco-r", "content_hash": "sha256:" + "c" * 64, "responded_at": "2026-10-09T12:58:00+00:00"}},
        "rows": [{"official_code": code, "industry_category_code": category,
            "industry_category_label": label, "industry_subcategory_code": "CG" if category == "C" else None,
            "industry_subcategory_label": "专用、通用及交通运输设备" if category == "C" else None,
            "industry_major_code": "35" if category == "C" else "66", "industry_major_label": "fixture分类",
            "source_page": 151, "source_row_raw": "fixture原表行", "parse_status": "parsed"}]}


def test_official_supplement_adds_exact_bse_to_existing_ranking_with_separate_taxonomy():
    quotes, kwargs = inputs()
    original = [f for f in kwargs["facts"] if f.instrument_id == "600001"]
    kwargs["facts"] += [replace(f, instrument_id="920001", version_id="bj-" + f.version_id) for f in original]
    kwargs["bound"]["records"] += [dict(r, instrument_id="920001") for r in list(kwargs["bound"]["records"])
                                 if r["instrument_id"] == "600001"]
    kwargs["industry_supplement"] = supplement_fixture()
    packet = build_research_scan(quotes, **kwargs)
    card = next(r for r in packet["candidates"] if r["code"] == "920001")
    assert card["status"] == "RANKED" and card["board"] == "BSE" and card["factors"]
    classification = card["industryClassification"]
    assert card["industry"].startswith("CAPCO C / CG / 35")
    assert classification["system"] == "CAPCO 上市公司行业统计分类"
    assert classification["classificationAsOf"] is None and card["industryAsOf"] is None
    assert classification["publicationDate"] == "2026-09-30" and classification["source"]["page"] == 151
    assert packet["rankedCount"] == 3 and packet["industryCoverage"]["activeUnknownCount"] == 0
    assert packet["industryCoverage"]["supplementCount"] == 1
    assert packet["asOfTime"] == "2026-10-09T13:00:00+00:00"
    assert packet["historyCoverage"]["rankedCurrentCount"] == 0 and not packet["simulationAvailable"]


def test_capco_financial_category_never_uses_ordinary_f08_and_primary_is_not_overwritten():
    quotes, kwargs = inputs()
    kwargs["industry_supplement"] = supplement_fixture(category="J")
    card = next(r for r in build_research_scan(quotes, **kwargs)["candidates"] if r["code"] == "920001")
    assert card["status"] == "FINANCIAL" and card["signalRank"] is None and card["factors"] == {}
    assert card["financialObservation"]["status"] == "MISSING"
    assert all(v["value"] is None for v in card["financialObservation"]["values"])
    kwargs["industry_supplement"] = supplement_fixture(code="600001", category="J")
    primary = next(r for r in build_research_scan(quotes, **kwargs)["candidates"] if r["code"] == "600001")
    assert primary["status"] == "RANKED" and primary["industry"] == "C39工业"
    assert primary["industryClassification"]["selection"] == "PRIMARY"
    assert primary["industryClassification"]["alternatives"][0]["system"].startswith("CAPCO")


def test_supplement_invented_effective_date_and_duplicate_rows_are_rejected():
    quotes, kwargs = inputs()
    supplement = supplement_fixture()
    kwargs["industry_supplement"] = supplement
    supplement["taxonomy"]["classification_asof_date"] = "2026-06-30"
    with pytest.raises(ResearchCandidateError, match="invented classification date"):
        build_research_scan(quotes, **kwargs)
    supplement["taxonomy"]["classification_asof_date"] = None
    supplement["rows"].append(dict(supplement["rows"][0]))
    with pytest.raises(ResearchCandidateError, match="duplicate supplement"):
        build_research_scan(quotes, **kwargs)


def test_financial_observation_requires_same_period_group_family_and_keeps_missing_null():
    quotes, kwargs = inputs()
    for binding in kwargs["bound"]["records"]:
        binding["evidence"] = {metric: {"family": "B"} for metric in binding["values"]}
    packet = build_research_scan(quotes, **kwargs)
    card = next(r for r in packet["candidates"] if r["code"] == "600003")
    observation = card["financialObservation"]
    assert observation["status"] == "COMPLETE" and observation["reportFamily"] == "B"
    assert observation["comparisonKey"] == "证监会行业分类|66|2026-06-30|B"
    assert {r["metric"] for r in observation["values"]} == {r["metric"] for r in card["financialBridge"]}
    assert all(v["evidenceIds"] for v in observation["values"])
    latest = next(r for r in kwargs["bound"]["records"] if r["instrument_id"] == "600003" and r["period_end"] == "2026-06-30")
    latest["evidence"]["operating_cashflow"]["family"] = "I"
    mixed = next(r for r in build_research_scan(quotes, **kwargs)["candidates"] if r["code"] == "600003")
    assert mixed["financialObservation"]["comparisonKey"] is None
    kwargs["facts"] = [f for f in kwargs["facts"] if not (f.instrument_id == "600003" and f.period_end.isoformat() == "2026-06-30" and f.metric == "revenue")]
    partial = next(r for r in build_research_scan(quotes, **kwargs)["candidates"] if r["code"] == "600003")
    assert partial["financialObservation"]["status"] == "PARTIAL"
    assert partial["financialObservation"]["missingMetrics"] == ["revenue"]
    assert next(v for v in partial["financialObservation"]["values"] if v["metric"] == "revenue")["value"] is None


def test_financial_report_family_can_use_exact_legacy_report_names_but_conflicts_disable_comparison():
    quotes, kwargs = inputs()
    kinds = {"operating_cashflow": "CASHFLOW", "parent_equity": "BALANCE"}
    for binding in kwargs["bound"]["records"]:
        binding["evidence"] = {metric: {"report": "RPT_F10_FINANCE_G" + kinds.get(metric, "INCOME")}
                               for metric in binding["values"]}
    card = next(r for r in build_research_scan(quotes, **kwargs)["candidates"] if r["code"] == "600003")
    assert card["financialObservation"]["reportFamily"] == "G"
    assert card["financialObservation"]["comparisonKey"] == "证监会行业分类|66|2026-06-30|G"
    latest = next(r for r in kwargs["bound"]["records"] if r["instrument_id"] == "600003" and r["period_end"] == "2026-06-30")
    latest["evidence"]["parent_equity"]["family"] = "B"
    conflict = next(r for r in build_research_scan(quotes, **kwargs)["candidates"] if r["code"] == "600003")
    assert conflict["financialObservation"]["reportFamily"] is None
    assert conflict["financialObservation"]["comparisonKey"] is None


def disclosure_fixture():
    return {"schema": "aquant.research_industry_missing.v1",
        "captured_from": "2026-10-09T13:01:00+00:00", "captured_to": "2026-10-09T13:02:00+00:00",
        "sources": {"pdf": {"receipt_id": "company-r", "responded_at": "2026-10-09T13:02:00+00:00"}},
        "rows": [{"official_code": "920001", "industry_code_raw": "C35", "industry_label_raw": "专用设备制造业",
            "classification_system": "国民经济行业分类", "standard_id": "GB/T 4754-2017", "classification_asof_date": None,
            "published_date": "2026-09-10", "pdf_receipt_id": "company-r", "document_url": "https://static.cninfo.com.cn/fixture.PDF",
            "pdf_content_hash": "sha256:" + "d" * 64, "industry_field_page": 18,
            "evidence_excerpts": [{"field": "所属行业名称", "page": 18, "excerpt": "证券代码 920001 所属行业名称 专用设备制造业 所属行业代码 C35"}],
            "announcement_id": "company-ann", "index_receipt_id": "company-index-r", "index_content_hash": "sha256:" + "e" * 64,
            "query_complete": False}]}


def test_exact_company_disclosure_is_separate_from_capco_and_partial_index_is_not_complete():
    quotes, kwargs = inputs()
    kwargs["industry_disclosures"] = disclosure_fixture()
    scan = build_research_scan(quotes, **kwargs)
    card = next(r for r in scan["candidates"] if r["code"] == "920001")
    assert card["industry"] == "GB/T 4754-2017 C35 专用设备制造业"
    assert card["industryClassification"]["system"] == "国民经济行业分类 / 公司披露"
    assert card["industryClassification"]["classificationAsOf"] is None
    assert card["industryClassification"]["publicationDate"] == "2026-09-10"
    assert card["industryClassification"]["source"]["queryComplete"] is False
    assert card["exclusionCode"] != "INDUSTRY_UNKNOWN"
    assert card["signalRank"] is None  # Classification cannot fill missing facts.
    assert scan["industryCoverage"]["supplementCount"] == 1 and scan["industryCoverage"]["activeUnknownCount"] == 0
    assert scan["asOfTime"] == "2026-10-09T13:02:00+00:00" and not scan["simulationAvailable"]
    kwargs["industry_supplement"] = supplement_fixture()
    kept = next(r for r in build_research_scan(quotes, **kwargs)["candidates"] if r["code"] == "920001")
    assert kept["industry"].startswith("CAPCO ")
    assert kept["industryClassification"]["alternatives"][0]["system"] == "国民经济行业分类 / 公司披露"


@pytest.mark.parametrize("change", ["unknown_standard", "invented_date", "ambiguous_receipt"])
def test_unresolved_company_classification_is_not_admitted(change):
    quotes, kwargs = inputs()
    packet = disclosure_fixture()
    kwargs["industry_disclosures"] = packet
    if change == "unknown_standard": packet["rows"][0]["standard_id"] = "provider label"
    if change == "invented_date": packet["rows"][0]["classification_asof_date"] = "2026-09-10"
    if change == "ambiguous_receipt": packet["sources"]["duplicate"] = dict(packet["sources"]["pdf"])
    with pytest.raises(ResearchCandidateError):
        build_research_scan(quotes, **kwargs)
