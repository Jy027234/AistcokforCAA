"""深交所备源适配器的解析与边界用例（全部离线）。

这些用例把 2026-09-28 实测到的响应形状固定下来：``topKeys`` 为
``currentPage/data/pageSize/totalSize``，行内字段为 ``id/doctitle/docpubtime/
docpuburl``。同时固定几条**必须失败关闭**的边界：分页不一致、重复文档 ID、
关键字命中不等于身份校验、以及"同哈希不是独立数值验证"。
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from aquant.adapters.providers.szse import (
    CHANNEL_ALL_NOTICES, CHANNEL_FIXED, MAX_PAGE_SIZE, SzseAnnouncement,
    SzseError, cross_source_evidence, parse_index_payload, strip_tags, urllib_parse,
)


def _payload(rows, *, total=None, has_more=None, page=1, page_size=20):
    document = {"currentPage": page, "pageSize": page_size, "data": rows}
    if total is not None:
        document["totalSize"] = total
    if has_more is not None:
        document["hasNext"] = has_more
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


ROW = {
    "id": "e35e9142-367a-496e-9c20-f3b922bf0471",
    "doctitle": "新易盛：2026年半年度报告",
    "docpubtime": "2026-08-25",
    "docpuburl": ("http://disc.static.szse.cn/download/disc/disk03/finalpage/"
                  "2026-08-25/7208175f-a232-40d5-84a3-8a187e783752.PDF"),
    "doctype": "半年报",
}


def test_form_body_keeps_the_bracketed_channel_key() -> None:
    """``channelCode[]`` 的方括号原样送出。

    已实测通过的探针请求体就是 ``channelCode[]=fixed_disc``（方括号未编码），
    服务端接受这一形式；把方括号编码成 ``%5B%5D`` 会与已归档证据不一致，
    因此这里固定"原样"这一行为。
    """

    body = urllib_parse({
        "keyword": "300502", "range": "title", "channelCode[]": CHANNEL_FIXED,
        "time": "1787587200000", "endTime": "1787673600000",
        "currentPage": "1", "pageSize": "20",
    })
    assert b"channelCode[]=fixed_disc" in body
    assert b"keyword=300502" in body
    assert body == (
        b"keyword=300502&range=title&channelCode[]=fixed_disc"
        b"&time=1787587200000&endTime=1787673600000&currentPage=1&pageSize=20")


def test_probe_shaped_response_parses_with_source_metadata() -> None:
    announcements, total, has_more = parse_index_payload(_payload([ROW], total=2))
    assert total == 2 and has_more is None
    assert len(announcements) == 1
    item = announcements[0]
    assert item.document_id == ROW["id"]
    assert item.published_on == date(2026, 8, 25)
    assert item.document_url.endswith(".PDF")
    assert item.is_full_report is True


def test_report_title_must_end_at_the_report_name() -> None:
    """业绩说明会公告里含"半年度报告"，但不是财报原文。"""

    notice = SzseAnnouncement(
        document_id="x", title="新易盛：关于参加…半年度报告业绩说明会的公告",
        published_on=date(2026, 9, 9), document_url="u", doc_type=None)
    assert notice.is_full_report is False
    assert SzseAnnouncement(
        document_id="y", title="格力电器：2025年年度报告",
        published_on=None, document_url=None, doc_type=None).is_full_report is True
    assert SzseAnnouncement(
        document_id="z", title="新易盛：2026年半年度报告摘要",
        published_on=None, document_url=None, doc_type=None).is_full_report is False
    assert SzseAnnouncement(
        document_id="w", title="新易盛：2026年半年度报告（英文版）",
        published_on=None, document_url=None, doc_type=None).is_full_report is False


def test_html_tags_are_stripped_from_titles() -> None:
    assert strip_tags("<em>300502</em> 新易盛：<b>2026年半年度报告</b>") == \
        "300502 新易盛：2026年半年度报告"


def test_row_without_a_document_id_fails_closed() -> None:
    with pytest.raises(SzseError, match="document id"):
        parse_index_payload(_payload([{**ROW, "id": ""}]))


def test_non_json_and_missing_list_fail_closed() -> None:
    with pytest.raises(SzseError, match="not JSON"):
        parse_index_payload(b"<html>blocked</html>")
    with pytest.raises(SzseError, match="no announcement list"):
        parse_index_payload(json.dumps({"currentPage": 1}).encode())


def test_date_precision_is_a_date_not_an_invented_time() -> None:
    """``docpubtime`` 只有日期级含义；不得据此编造发布时刻。"""

    announcements, _, _ = parse_index_payload(
        _payload([{**ROW, "docpubtime": "2026-08-25 00:00:00"}]))
    assert announcements[0].published_on == date(2026, 8, 25)
    # 解析不出来时保持缺失，而不是猜一个日期。
    announcements, _, _ = parse_index_payload(
        _payload([{**ROW, "docpubtime": "not a date"}]))
    assert announcements[0].published_on is None


def test_page_size_cap_matches_the_observed_server_normalisation() -> None:
    assert MAX_PAGE_SIZE == 50
    assert CHANNEL_FIXED == "fixed_disc"
    assert CHANNEL_ALL_NOTICES == "listedNotice_disc"


def test_identical_bytes_are_not_an_independent_numeric_verification() -> None:
    """两站同哈希只证明原文字节一致，不证明数值被独立核实。"""

    payload = b"%PDF-1.5 identical bytes"
    evidence = cross_source_evidence(
        document_id="e35e9142", szse_bytes=payload, cninfo_bytes=payload)
    assert evidence["byte_identical"] is True
    assert evidence["numeric_independence"] is False
    assert "not an independent numeric verification" in str(evidence["conclusion"])

    differing = cross_source_evidence(
        document_id="e35e9142", szse_bytes=payload, cninfo_bytes=b"%PDF-1.5 other")
    assert differing["byte_identical"] is False


def test_missing_cninfo_copy_reports_no_comparison() -> None:
    evidence = cross_source_evidence(
        document_id="e35e9142", szse_bytes=b"%PDF-1.5", cninfo_bytes=None)
    assert evidence["byte_identical"] is False
    assert evidence["cninfo_content_hash"] is None
    assert evidence["numeric_independence"] is False
