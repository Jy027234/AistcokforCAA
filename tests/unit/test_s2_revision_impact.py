"""Original evidence, relationship closure and subsequent changes bound NO_EFFECT."""
from copy import deepcopy
from datetime import date, datetime, timezone
import json
import sqlite3

import pytest

from aquant.domain.data.forward_archive import ForwardArchive
from aquant.adapters.providers import s2_revision_impact as impact

ISSUER = "新洋丰农业科技股份有限公司"


@pytest.fixture
def sources(tmp_path, monkeypatch):
    # Only PDF transport/layout is substituted. Semantic scope, byte/receipt/
    # request/index joins and current/prior comparisons exercise the real module.
    monkeypatch.setattr(impact, "_pages", lambda body, **kwargs: tuple(json.loads(body[5:])["pages"]))
    monkeypatch.setattr(impact, "_cells", lambda body, period, metrics, **kwargs: {
        k: v for k, v in json.loads(body[5:]).get("cells", {}).items() if k in metrics})
    con = sqlite3.connect(tmp_path / "meta.sqlite")
    con.row_factory = sqlite3.Row
    archive = ForwardArchive(con, tmp_path)
    counter = [0]
    def receipt(body, url, detail=None):
        counter[0] += 1
        digest, _ = archive.store_bytes(body)
        start = datetime(2026, 10, 12, 12, 0, counter[0], tzinfo=timezone.utc)
        row = archive.record(source_id="cninfo", url=url, outcome="OK", http_status=200,
            requested_at=start, responded_at=start, content_hash=digest, byte_size=len(body), detail=detail)
        stored = con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?", (row.receipt_id,)).fetchone()
        return dict(stored)
    lookup = receipt(json.dumps([{"code": "000902", "orgId": "gssz0000902", "zwjc": "新洋丰"}]).encode(),
                     "https://www.cninfo.com.cn/new/information/topSearch/query?keyWord=000902&maxNum=10")
    docs, entries, selected = [], [], []
    def document(aid, title, period, pages, published, cells=None):
        body = b"%PDF-" + json.dumps({"pages": pages, "cells": cells or {}}, ensure_ascii=False).encode()
        url = f"https://static.cninfo.com.cn/finalpage/{published}/{aid}.PDF"
        rec = receipt(body, url)
        doc = {"announcementId": aid, "title": title, "periodEnd": period, "code": "000902",
            "instrumentId": "SZ.000902", "documentUrl": url, "contentHash": rec["content_hash"],
            "byteSize": len(body), "receipt": rec}
        docs.append(doc)
        entries.append({"announcementId": aid, "announcementTitle": title, "secCode": "000902",
            "orgId": "gssz0000902", "announcementTime": int(datetime.fromisoformat(published).replace(tzinfo=impact.CST).timestamp()*1000),
            "adjunctUrl": url.split(".cninfo.com.cn/", 1)[1]})
        return doc
    for number, period in enumerate(sorted(impact.PERIODS), 1):
        half = period.endswith("06-30")
        title = period[:4] + ("年半年度报告" if half else "年年度报告")
        cell = {"metric": "revenue", "statementScope": "CONSOLIDATED", "statement": "利润表", "factPeriodEnd": period,
            "columnRole": "CURRENT", "currentHeader": period[:4] + ("年半年度" if half else "年度"),
            "priorHeader": str(int(period[:4])-1)+( "年半年度" if half else "年度"),
            "priorFactPeriodEnd": str(int(period[:4])-1)+period[4:], "unitRaw": "元", "amountYuan": "20.00",
            "priorAmountYuan": "20.00", "currentCellRaw": "20.00", "priorCellRaw": "20.00", "rowLabel": "营业收入",
            "page": 2, "sourceLine": "其中：营业收入 20.00 20.00", "tablePages": [2, 3]}
        published = f"{period[:4]}-08-20" if half else f"{int(period[:4])+1}-04-28"
        selected.append(document("10000" + str(number), title, period,
            [ISSUER + " 证券代码：000902 " + title, "合并利润表 单位：元 其中：营业收入 20.00 20.00"],
            published, {"revenue": cell}))
    primary = ISSUER + " 本次差错更正为追溯调整，将影响2021年期初所有者权益项目，并延续影响2021-2024年度资产负债表及所有者权益变动表：资本公积：累计调增85,161.12元；未分配利润：累计调减85,161.12元。上述调整不影响公司各期净利润，仅对所有者权益内部结构产生影响。"
    corrected = ISSUER + " 公司本次会计差错更正涉及2018年至2020年度财务报表及附注，追溯调整涉及2021-2024年度财务报表及附注。"
    document("1225198545", "关于前期会计差错更正的公告", None, [primary], "2026-04-28")
    document("1225198546", "关于前期会计差错更正后的财务报表及相关附注", None, [corrected], "2026-04-28")
    document("1225198547", "信永中和关于新洋丰前期差错更正专项说明的鉴证报告", None, [""], "2026-04-28")
    from urllib.parse import urlencode
    request = {"stock": "000902,gssz0000902", "seDate": "2024-06-30~2026-10-13", "pageNum": "1",
               "pageSize": "30", "category": "", "searchkey": "", "column": "szse", "tabName": "fulltext"}
    request_hash, _ = archive.store_bytes(urlencode(request).encode())
    def index():
        body = json.dumps({"announcements": entries, "totalAnnouncement": len(entries), "hasMore": False}).encode()
        rec = receipt(body, "http://www.cninfo.com.cn/new/hisAnnouncement/query", "fixture:request=" + request_hash)
        return {"page": 1, "requestBodyHash": request_hash, "responseReceiptId": rec["receipt_id"],
            "responseContentHash": rec["content_hash"], "httpStatus": 200, "rawAnnouncementCount": len(entries),
            "totalAnnouncement": len(entries), "hasMore": False, "firstSeenAt": rec["first_seen_at"]}
    company = {"code": "000902", "instrumentId": "SZ.000902", "name": "新洋丰", "organizationId": "gssz0000902",
        "orgLookupReceiptId": lookup["receipt_id"], "orgLookupContentHash": lookup["content_hash"],
        "publicationWindow": ["2024-06-30", "2026-10-13"], "allCategoryPages": [index()],
        "allCategoryTermination": "has_more_false", "allCategoryTotalAnnouncement": len(entries),
        "selectedReportRecords": selected, "relatedDocumentRecords": docs[5:]}
    con.close()
    return company, docs, tmp_path


def assess(sources, **changes):
    company, docs, root = sources
    return impact.assess_revision_impact(archive_root=root, company=company,
        target_period_end=changes.pop("target_period_end", date(2025, 6, 30)), target_metrics=("revenue",),
        issuer_name=changes.pop("issuer_name", ISSUER), documents=changes.pop("documents", docs), **changes)


def test_body_scope_and_whole_captured_set_support_no_effect(sources):
    result = assess(sources).as_dict()
    outcome = result["outcomes"][0]
    assert outcome["status"] == "NO_EFFECT"
    assert outcome["admissionStatus"] == "NOT_ADMITTED"
    assert outcome["currentSource"]["columnRole"] == "CURRENT"
    assert outcome["comparisons"][0]["columnRole"] == "PRIOR"
    assert outcome["comparisons"][0]["factPeriodEnd"] == "2025-06-30"
    assert len(result["relationshipScope"]["financialCauseIds"]) == 3
    assert len(result["relationshipScope"]["financialCandidateIds"]) == 8
    assert not result["relationshipScope"]["excludesLaterUnobservedAnnouncements"]
    assert not result["writesFinancialWarehouse"]


@pytest.mark.parametrize("fault", ["raw", "hash", "bytes", "url", "time", "issuer", "period", "missing_relation", "missing_report", "index_metadata"])
def test_false_identity_or_source_claim_is_rejected(sources, fault):
    company, docs, root = deepcopy(sources)
    if fault == "raw":
        digest = docs[0]["contentHash"][7:]
        (root / "raw" / digest[:2] / digest).write_bytes(b"changed")
    elif fault in ("hash", "bytes", "url", "time"):
        if fault == "hash": docs[0]["contentHash"] = docs[1]["contentHash"]
        elif fault == "bytes": docs[0]["byteSize"] += 1
        elif fault == "url": docs[0]["documentUrl"] = "https://static.cninfo.com.cn/wrong.PDF"
        else: docs[0]["receipt"]["responded_at"] = "2026-10-13T01:00:00+00:00"
    elif fault == "issuer":
        with pytest.raises(impact.RevisionImpactError, match="issuer"):
            assess((company, docs, root), issuer_name="错误公司股份有限公司")
        return
    elif fault == "period": docs[0]["periodEnd"] = "2024-12-31"
    elif fault == "missing_relation": docs.pop()
    elif fault == "missing_report": docs.pop(0)
    else: company["allCategoryPages"][0]["hasMore"] = True
    with pytest.raises(impact.RevisionImpactError):
        assess((company, docs, root))


def replace_pdf(sources, doc, alter):
    _, _, root = sources
    with sqlite3.connect(root / "meta.sqlite") as con:
        con.row_factory = sqlite3.Row
        archive = ForwardArchive(con, root)
        value = json.loads(archive.load_bytes(doc["contentHash"])[5:])
        alter(value)
        body = b"%PDF-" + json.dumps(value, ensure_ascii=False).encode()
        digest, _ = archive.store_bytes(body)
        instant = datetime(2026, 10, 12, 14, tzinfo=timezone.utc)
        rec = archive.record(source_id="cninfo", url=doc["documentUrl"], outcome="OK", http_status=200,
            requested_at=instant, responded_at=instant, content_hash=digest, byte_size=len(body))
        doc.update(contentHash=digest, byteSize=len(body), receipt=dict(con.execute(
            "SELECT * FROM fetch_receipt WHERE receipt_id=?", (rec.receipt_id,)).fetchone()))


def test_original_body_scope_not_title_year_or_authored_verified_flag(sources):
    company, docs, root = deepcopy(sources)
    doc = next(x for x in docs if x["announcementId"] == "1225198545")
    replace_pdf((company, docs, root), doc, lambda value: value.update(pages=[ISSUER + " 2023年度更正，verified:true"]))
    assert assess((company, docs, root)).as_dict()["outcomes"][0]["status"] == "UNRESOLVED"


def test_hash_valid_notice_for_other_issuer_is_rejected(sources):
    company, docs, root = deepcopy(sources)
    doc = next(x for x in docs if x["announcementId"] == "1225198545")
    replace_pdf((company, docs, root), doc, lambda value: value.update(pages=[value["pages"][0].replace(ISSUER, "其他公司股份有限公司")]))
    with pytest.raises(impact.RevisionImpactError, match="relation exact issuer"):
        assess((company, docs, root))


def test_actual_later_comparative_change_is_affects_not_no_effect(sources):
    company, docs, root = deepcopy(sources)
    doc = next(x for x in docs if x["periodEnd"] == "2026-06-30")
    replace_pdf((company, docs, root), doc, lambda v: v["cells"]["revenue"].update(priorAmountYuan="21.00", priorCellRaw="21.00", sourceLine="营业收入 20.00 21.00"))
    outcome = assess((company, docs, root)).as_dict()["outcomes"][0]
    assert outcome["status"] == "AFFECTS"
    assert outcome["admissionStatus"] == "NOT_ADMITTED"


def test_machine_result_is_replayed_and_self_authored_verdict_rejected(sources):
    company, docs, root = sources
    original = assess(sources).as_dict()
    inputs = dict(archive_root=root, company=company, target_period_end=date(2025, 6, 30),
                  target_metrics=("revenue",), issuer_name=ISSUER, documents=docs)
    assert impact.replay_revision_impact(original, **inputs).as_dict()["outcomes"][0]["status"] == "NO_EFFECT"
    original["outcomes"][0]["status"] = "AFFECTS"
    payload = dict(original); payload.pop("evidenceHash")
    original["evidenceHash"] = impact._hash(impact._canonical(payload).encode())
    with pytest.raises(impact.RevisionImpactError, match="differs from original"):
        impact.replay_revision_impact(original, **inputs)


def test_target_outside_fixed_periods_is_rejected(sources):
    with pytest.raises(impact.RevisionImpactError, match="bounded prototype"):
        assess(sources, target_period_end=date(2025, 3, 31))


def replace_index(sources, alter):
    company, _, root = sources
    page = company["allCategoryPages"][0]
    with sqlite3.connect(root / "meta.sqlite") as con:
        con.row_factory = sqlite3.Row
        archive = ForwardArchive(con, root)
        value = json.loads(archive.load_bytes(page["responseContentHash"]))
        alter(value["announcements"])
        value["totalAnnouncement"] = len(value["announcements"])
        body = json.dumps(value).encode()
        digest, _ = archive.store_bytes(body)
        instant = datetime(2026, 10, 12, 15, tzinfo=timezone.utc)
        rec = archive.record(source_id="cninfo", url="http://www.cninfo.com.cn/new/hisAnnouncement/query",
            outcome="OK", http_status=200, requested_at=instant, responded_at=instant, content_hash=digest,
            byte_size=len(body), detail="fixture:request=" + page["requestBodyHash"])
        page.update(responseReceiptId=rec.receipt_id, responseContentHash=digest,
            rawAnnouncementCount=len(value["announcements"]), totalAnnouncement=len(value["announcements"]),
            firstSeenAt=instant.isoformat())
        company["allCategoryTotalAnnouncement"] = len(value["announcements"])


def test_new_captured_financial_cause_keeps_whole_fact_unresolved(sources):
    company, docs, root = deepcopy(sources)
    extra = deepcopy(docs[-1])
    extra.update(announcementId="999999", title="关于2025年半年度财务报表更正的公告",
        documentUrl="https://static.cninfo.com.cn/finalpage/2026-09-01/999999.PDF")
    replace_pdf((company, docs, root), extra, lambda v: v.update(pages=[ISSUER + " 新更正事项影响营业收入"]))
    docs.append(extra)
    def add(rows):
        row = deepcopy(rows[-1])
        row.update(announcementId="999999", announcementTitle=extra["title"],
            announcementTime=int(datetime(2026, 9, 1, tzinfo=impact.CST).timestamp()*1000),
            adjunctUrl="finalpage/2026-09-01/999999.PDF")
        rows.append(row)
    replace_index((company, docs, root), add)
    outcome = assess((company, docs, root)).as_dict()["outcomes"][0]
    assert outcome["status"] == "UNRESOLVED"
    assert "relationship set differs" in outcome["reason"]


def test_same_day_changed_versions_do_not_invent_order(sources):
    company, docs, root = deepcopy(sources)
    target = next(d for d in docs if d["periodEnd"] == "2025-06-30")
    target["documentUrl"] = target["documentUrl"].replace("2025-08-20", "2026-08-20")
    replace_pdf((company, docs, root), target, lambda v: None)
    def republish(rows):
        row = next(r for r in rows if r["announcementId"] == target["announcementId"])
        row.update(announcementTime=int(datetime(2026, 8, 20, tzinfo=impact.CST).timestamp()*1000),
            adjunctUrl=f"finalpage/2026-08-20/{target['announcementId']}.PDF")
    replace_index((company, docs, root), republish)
    later = next(d for d in docs if d["periodEnd"] == "2026-06-30")
    replace_pdf((company, docs, root), later, lambda v: v["cells"]["revenue"].update(priorAmountYuan="21.00"))
    outcome = assess((company, docs, root)).as_dict()["outcomes"][0]
    assert outcome["status"] == "UNRESOLVED"
    assert "same-day" in outcome["reason"]


def test_existing_later_report_missing_same_fact_cell_is_not_no_effect(sources):
    company, docs, root = deepcopy(sources)
    later = next(d for d in docs if d["periodEnd"] == "2026-06-30")
    replace_pdf((company, docs, root), later, lambda v: v.update(cells={}))
    outcome = assess((company, docs, root)).as_dict()["outcomes"][0]
    assert outcome["status"] == "UNRESOLVED"
    assert outcome["unresolvedSuccessorIds"] == [later["announcementId"]]


def add_indexed_report_alternate(sources, *, period, supplied=True, alter=None):
    company, docs, root = sources
    original = next(d for d in docs if d["periodEnd"] == period)
    extra = deepcopy(original)
    extra.update(announcementId="888888", title=original["title"].replace("报告", "财务报告"),
                 documentUrl=original["documentUrl"].replace(original["announcementId"], "888888"))
    if supplied:
        replace_pdf(sources, extra, alter or (lambda value: None))
        docs.append(extra)
    def add(rows):
        row = deepcopy(next(r for r in rows if r["announcementId"] == original["announcementId"]))
        row.update(announcementId="888888", announcementTitle=extra["title"],
                   adjunctUrl=extra["documentUrl"].split(".cninfo.com.cn/", 1)[1])
        rows.append(row)
    replace_index(sources, add)
    return extra


def test_indexed_full_financial_report_missing_original_blocks_no_effect(sources):
    current = deepcopy(sources)
    add_indexed_report_alternate(current, period="2026-06-30", supplied=False)
    result = assess(current).as_dict()
    assert result["outcomes"][0]["status"] == "UNRESOLVED"
    assert result["outcomes"][0]["missingReportSourceIds"] == ["888888"]
    assert not result["relationshipScope"]["completeForArchivedIndexResponse"]
    assert result["relationshipScope"]["reportCandidatePeriods"]["888888"] == "2026-06-30"
    assert "888888" in result["relationshipScope"]["financialCandidateIds"]


def test_alternate_documents_override_keeps_company_packet_and_compares_same_fact(sources):
    current = deepcopy(sources)
    frozen = deepcopy(current[0]["selectedReportRecords"])
    add_indexed_report_alternate(current, period="2025-06-30")
    result = assess(current).as_dict()
    assert current[0]["selectedReportRecords"] == frozen
    assert result["outcomes"][0]["status"] == "NO_EFFECT"
    alternate = next(c for c in result["outcomes"][0]["comparisons"] if c["announcementId"] == "888888")
    assert alternate["columnRole"] == "CURRENT"
    assert alternate["factPeriodEnd"] == "2025-06-30"


def test_same_day_alternate_current_values_cannot_be_assigned_order(sources):
    current = deepcopy(sources)
    add_indexed_report_alternate(current, period="2025-06-30",
        alter=lambda v: v["cells"]["revenue"].update(amountYuan="21.00", currentCellRaw="21.00"))
    outcome = assess(current).as_dict()["outcomes"][0]
    assert outcome["status"] == "UNRESOLVED"
    assert "same-day" in outcome["reason"]


def test_same_day_successor_alternates_also_require_consistent_prior_values(sources):
    current = deepcopy(sources)
    add_indexed_report_alternate(current, period="2026-06-30",
        alter=lambda v: v["cells"]["revenue"].update(priorAmountYuan="21.00", priorCellRaw="21.00"))
    outcome = assess(current).as_dict()["outcomes"][0]
    assert outcome["status"] == "UNRESOLVED"
    assert "same-day" in outcome["reason"]


def test_existing_alternate_missing_applicable_cell_is_not_no_effect(sources):
    current = deepcopy(sources)
    add_indexed_report_alternate(current, period="2025-06-30", alter=lambda v: v.update(cells={}))
    outcome = assess(current).as_dict()["outcomes"][0]
    assert outcome["status"] == "UNRESOLVED"
    assert outcome["unresolvedSuccessorIds"] == ["888888"]


@pytest.mark.parametrize("title,expected", [
    ("2024年年度财务报告", date(2024, 12, 31)),
    ("2024 年 年度 财务报告", date(2024, 12, 31)),
    ("2024年年度财务报告摘要", None), ("2024年年度财务报告英文版", None),
    ("2024年年度财务报告业绩预告", None), ("2024年年度财务报告提示性公告", None),
])
def test_bounded_annual_financial_alias_is_a_complete_report_only(title, expected):
    assert impact._report_period(title) == expected


@pytest.mark.parametrize("code,issuer", [("000899", "江西赣能股份有限公司"),
    ("002602", "浙江世纪华通集团股份有限公司")])
@pytest.mark.parametrize("period,kind", [(date(2024, 12, 31), "年度"), (date(2025, 6, 30), "半年度")])
def test_financial_only_annex_has_independent_cover_and_statement_identity(code, issuer, period, kind):
    title = f"{period.year}年{kind}财务报告"
    pages = [f"{issuer}{period.year}年{kind}财务会计报告（经审计）",
        f"1、合并资产负债表 编制单位：{issuer} {period.year}年{period.month:02}月{period.day:02}日 单位：元"]
    assert impact._auxiliary_financial_identity(pages, title, period, issuer, code)
    assert impact._auxiliary_financial_identity(["1 " + pages[0], pages[1]], title, period, issuer, code)
    for bad in ([pages[0], pages[1].replace(issuer, "其他公司股份有限公司")],
                [pages[0], pages[1].replace(f"{period.year}年", "2023年")],
                [pages[0], pages[1].replace("合并", "母公司")],
                [pages[0], pages[1], "证券代码：600000"],
                [pages[0].replace(issuer, "其他公司股份有限公司"), pages[1]]):
        assert not impact._auxiliary_financial_identity(bad, title, period, issuer, code)
    assert not impact._auxiliary_financial_identity(pages, title.replace("财务", ""), period, issuer, code)
    assert not impact._auxiliary_financial_identity(pages, title, period, issuer, "000902")


def test_annual_financial_report_missing_original_blocks_whole_relationship_scope(sources):
    current = deepcopy(sources)
    add_indexed_report_alternate(current, period="2024-12-31", supplied=False)
    outcome = assess(current).as_dict()["outcomes"][0]
    assert outcome["status"] == "UNRESOLVED"
    assert outcome["missingReportSourceIds"] == ["888888"]


@pytest.mark.parametrize("unit,amount", [("元", "1234.5678"), ("千元", "1234567.8000"),
    ("万元", "12345678.0000"), ("百万元", "1234567800.0000")])
def test_inline_consolidated_table_keeps_exact_units_and_columns(unit, amount):
    text = f"母公司资产负债表 负债 8.00 9.00 3、合并利润表 单位：{unit} 项目 2025 年度 2024 年度 一、营业总收入 1,234.5678 999.1200 其中：营业收入 1,234.5678 999.1200 二、营业成本 5.00 6.00"
    row = impact._revenue_columns([text], "2025-12-31")
    assert row["amountYuan"] == amount
    assert row["priorAmountYuan"] == str(impact.Decimal("999.1200") * impact.SCALE[unit])
    assert row["currentCellRaw"] == "1,234.5678"
    assert row["priorFactPeriodEnd"] == "2024-12-31"
    assert row["columnRole"] == "CURRENT" and row["statementScope"] == "CONSOLIDATED"
    assert impact._revenue_columns([text], "2024-12-31") is None
    assert impact._revenue_columns([text.replace("合并利润表", "母公司利润表")], "2025-12-31") is None
    assert impact._revenue_columns([text, text], "2025-12-31") is None


def test_horizontal_stock_identity_is_bound_to_exact_company():
    page = "公司股票简况 股票种类 股票上市交易所 股票简称 股票代码 变更前股票简称 A股 上海证券交易所 西藏珠峰 600338 六、其他资料"
    assert impact._security_code([page], "600338", "西藏珠峰")
    assert not impact._security_code([page], "600338", "其他公司")
    assert not impact._security_code([page], "600339", "西藏珠峰")


def test_hash_valid_other_period_pdf_cannot_borrow_old_year_mention(sources):
    company, docs, root = deepcopy(sources)
    doc = next(d for d in docs if d["periodEnd"] == "2025-06-30")
    replace_pdf((company, docs, root), doc, lambda v: v.update(pages=[
        ISSUER + " 证券代码：000902 2024年半年度报告，2025年财务概况"]))
    with pytest.raises(impact.RevisionImpactError, match="exact year/interim period"):
        assess((company, docs, root))


def test_600338_requires_full_group_and_closed_actual_income_impact_table():
    ids = {"1220732174", "1220732167", "1220732169", "1220732175", "1220732166"}
    prefix = "本次更正事项对公司2023年度财务报告的合并资产负债表、合并利润表和合并现金流量表的相关科目和相关财务指标产生影响。具体如下 "
    table = "（二）对2023年度合并利润表报表项目的影响金额：单位：元 报表项目 更正前金额 更正金额 更正后金额 "
    table += " ".join(label + " 1.00 -2.00 3.00" for label in ("管理费用", "营业利润", "利润总额", "所得税费用", "净利润"))
    table += " （三）对2023年度合并现金流量表项目的影响金额"
    link = "根据上述更正，公司已同步调整2023年年度报告全文第二节、第三节、第十节以及摘要第二节中的对应内容或表述。具体详见公司同日在上海证券交易所网站披露的更正后2023年年度报告全文与摘要"
    docs = {"1220732174": {"pages": [prefix + table + " " + link]},
        "1220732167": {"pages": ["对公司2023年年度报告的合并财务 报表及相关附注进行更正"]},
        "1220732169": {"pages": ["鉴证报告"]},
        "1220732175": {"pages": ["公司代码：600338 公司简称：西藏珠峰 西藏珠峰资源股份有限公司 2023年年度报告"]},
        "1220732166": {"pages": ["公司代码：600338 公司简称：西藏珠峰 西藏珠峰资源股份有限公司 2023年年度报告摘要"]}}
    proof, error = impact._policy("600338", "revenue", "2024-06-30", docs, ids)
    assert proof and error is None
    assert len(proof["sourceQuotes"]) == 6
    assert impact._policy("600338", "revenue", "2024-06-30", docs, ids - {"1220732175"})[0] is None
    changed = deepcopy(docs)
    changed["1220732174"]["pages"][0] = changed["1220732174"]["pages"][0].replace(
        "管理费用", "营业收入 1.00 -2.00 3.00 管理费用")
    assert impact._policy("600338", "revenue", "2024-06-30", changed, ids)[0] is None
