"""证据层测试：引用定位、金额单位、时序、官方渠道时点。

对应主文档 §15.3（引用片段存在、金额单位正确、关联实体真实、时序合理）、
§7.3（只有日期时的保守顺延）、§9.2（事实与解释分开）。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from aquant.adapters.providers.cninfo import (
    Announcement,
    parse_announcements,
    to_pit_record,
)
from aquant.domain.evidence.citations import (
    Citation,
    CitationError,
    LocatorKind,
    assert_entity_resolvable,
    normalize_for_matching,
    parse_amounts,
    verify_citation,
    verify_citations,
)
from aquant.domain.data.pit import AvailabilityBasis, PitMode, TimestampPrecision


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


# ================================================================== 引用定位
RAW = "本公司2026年半年度报告：实现营业收入1.23亿元，同比增长15.6%。董事会决议公告。"


def test_exact_quote_is_located():
    c = Citation("cit-1", "doc-1", "实现营业收入1.23亿元")
    r = verify_citation(c, raw_text=RAW)
    assert r["located"] is True
    assert r["normalized_offset"] >= 0


def test_quote_absent_from_source_is_rejected():
    """§15.3 不可定位则不得发布。"""

    c = Citation("cit-2", "doc-1", "实现净利润8.88亿元")
    with pytest.raises(CitationError) as exc:
        verify_citation(c, raw_text=RAW)
    assert "not found" in exc.value.message
    assert "must not be published" in exc.value.repair_action


def test_empty_quote_proves_nothing():
    with pytest.raises(CitationError) as exc:
        verify_citation(Citation("cit-3", "doc-1", "   "), raw_text=RAW)
    assert "empty" in exc.value.message


def test_whitespace_and_fullwidth_differences_do_not_break_location():
    """归一化只抹平排版差异，不改语义。"""

    c = Citation("cit-4", "doc-1", "实现营业收入 1.23 亿元")
    assert verify_citation(c, raw_text=RAW)["located"] is True


def test_normalization_does_not_rewrite_numbers():
    """数字与语义不得被归一化改写，否则定位成功也不可信。"""

    assert normalize_for_matching("1.23亿元") == normalize_for_matching("1.23亿元")
    assert normalize_for_matching("1.23亿元") != normalize_for_matching("1.32亿元")


def test_missing_document_is_rejected():
    with pytest.raises(CitationError) as exc:
        verify_citations([Citation("cit-5", "doc-x", "营业收入")], raw_by_document={})
    assert "never verify against a summary" in exc.value.repair_action


def test_batch_fails_whole_if_any_citation_is_unlocatable():
    """不发布部分证据：一条不可定位即整体失败。"""

    with pytest.raises(CitationError):
        verify_citations(
            [Citation("c1", "d1", "实现营业收入1.23亿元"),
             Citation("c2", "d1", "净利润增长50%")],
            raw_by_document={"d1": RAW},
        )


# ================================================================== 金额单位
def test_amounts_are_parsed_with_units_and_raw_forms_kept():
    """§15.3 金额单位正确；§6.3 原始单位同时保留。"""

    amounts = parse_amounts("营业收入1.23亿元，净利润5000万元，每股收益0.5元")
    units = {a["unit"] for a in amounts}
    assert {"CNY_100000000", "CNY_10000", "CNY"} <= units
    for a in amounts:
        assert a["raw_unit"] and a["raw_value"]
        assert a["multiplier"] > 0


def test_amount_parsing_does_not_infer_meaning():
    """只识别金额，不推断"利润"之类的语义——解释必须与事实分开（§9.2）。"""

    amounts = parse_amounts("营业收入1.23亿元")
    assert all(set(a) >= {"raw_value", "unit", "multiplier"} for a in amounts)
    assert all("meaning" not in a and "label" not in a for a in amounts)


# ================================================================== 实体
def test_unknown_subject_is_rejected():
    with pytest.raises(CitationError) as exc:
        assert_entity_resolvable("SYN.A.999999",
                                 known_instrument_ids={"SYN.A.600519"})
    assert "never guess from text" in exc.value.repair_action


def test_known_subject_passes():
    assert_entity_resolvable("SYN.A.600519", known_instrument_ids={"SYN.A.600519"}) is None


# ================================================================== 巨潮解析
SAMPLE = {
    "announcements": [
        {"announcementId": "1220000001", "secCode": "600519", "secName": "贵州茅台",
         "announcementTitle": "2026年半年度报告", "announcementTime": 1789142400000,
         "adjunctUrl": "finalpage/2026-09-12/1220000001.PDF",
         "announcementTypeName": "半年报"},
        {"announcementId": "1220000002", "secCode": "000001", "secName": "平安银行",
         "announcementTitle": "关于召开股东大会的通知", "announcementTime": 1789056000000,
         "adjunctUrl": "finalpage/2026-09-11/1220000002.PDF",
         "announcementTypeName": "股东大会"},
        {"announcementId": "bad", "secCode": "x", "announcementTitle": "无时间戳",
         "announcementTime": None},
    ]
}


def test_announcements_are_parsed_from_the_official_shape():
    anns = parse_announcements(SAMPLE)
    assert len(anns) == 2, "无发布时间的条目必须跳过，它无法作为时点证据"
    assert anns[0].announcement_id == "1220000001"
    assert anns[0].sec_code == "600519"
    assert anns[0].title == "2026年半年度报告"
    assert anns[0].announced_on == date(2026, 9, 12)


def test_detail_url_is_built_from_the_adjunct_path():
    ann = parse_announcements(SAMPLE)[0]
    assert ann.detail_url() == ("http://static.cninfo.com.cn/finalpage/2026-09-12/"
                                "1220000001.PDF")


def test_adjunct_path_with_leading_slash_is_not_doubled():
    ann = Announcement("id", "600519", "x", "t", date(2026, 9, 12),
                       "/finalpage/a.PDF", None, {})
    assert "//finalpage" not in ann.detail_url()
    assert ann.detail_url().endswith("/finalpage/a.PDF")


# ================================================================== 时点（D04 在真实渠道上的落点）
CALENDAR = [date(2026, 9, 11), date(2026, 9, 14), date(2026, 9, 15)]


def test_announcement_is_not_available_on_its_own_preopen():
    """§7.3：接口只给日期，**不得**假定当日开盘前可用。"""

    ann = parse_announcements(SAMPLE)[0]     # 2026-09-12 发布（周六）
    rec = to_pit_record(ann, trading_calendar=CALENDAR, first_seen_at=utc(2026, 9, 13, 1, 0))

    assert rec["timestamp_precision"] == TimestampPrecision.DATE.value
    assert rec["source_published_at"] is None, "日期级时间不得伪造出具体时刻"
    assert rec["source_published_date"] == "2026-09-12"
    # 09-12 是周六 -> 次一交易日为 09-14，盘前 00:45Z
    assert rec["available_at"] == "2026-09-14T00:45:00+00:00"
    # 抓取时刻（09-13）早于窗口打开（09-14 00:45Z），所以我们**亲眼看到**了它，
    # 依据是 OBSERVED 而非推断（依据与模式都由同一事实决定）
    assert rec["available_basis"] == AvailabilityBasis.OBSERVED.value
    assert rec["pit_mode"] == PitMode.LIVE_OBSERVED.value


def test_first_seen_is_the_capture_time_not_backdated():
    ann = parse_announcements(SAMPLE)[0]
    seen = utc(2026, 9, 13, 1, 0)
    rec = to_pit_record(ann, trading_calendar=CALENDAR, first_seen_at=seen)
    assert rec["first_seen_at"] == seen.isoformat()
    # 抓取时间晚于发布日，这是正常的（我们周五之后才抓）
    assert rec["first_seen_at"] > rec["source_published_date"]


def test_naive_first_seen_is_rejected():
    ann = parse_announcements(SAMPLE)[0]
    with pytest.raises(ValueError, match="timezone-aware"):
        to_pit_record(ann, trading_calendar=CALENDAR,
                      first_seen_at=datetime(2026, 9, 13, 1, 0))


def test_unverified_until_content_is_archived_and_verified():
    """§15.3 正文未归档与核验前不得声称已核验。"""

    ann = parse_announcements(SAMPLE)[0]
    rec = to_pit_record(ann, trading_calendar=CALENDAR, first_seen_at=utc(2026, 9, 13, 1, 0))
    assert rec["verification_status"] == "UNVERIFIED"
    assert rec["market_direction"] == "UNKNOWN"
    assert rec["availability_rationale"], "顺延理由必须保留，供研究卡展示"


def test_backfilled_capture_is_marked_historical_not_forward():
    """§7.2：在可用时点之后才补抓的公告是对过去的重建，不得标成前向观察。

    这条区分很重要：把重建数据当成真实前向记录，会让"当时就能知道"这个
    结论凭空成立——而它恰恰是回测里最容易自欺的一步。
    """

    ann = parse_announcements(SAMPLE)[0]
    late = utc(2026, 9, 20, 1, 0)      # 远晚于 available_at（09-14 00:45Z）
    rec = to_pit_record(ann, trading_calendar=CALENDAR, first_seen_at=late)
    assert rec["pit_mode"] == PitMode.HISTORICAL_RECONSTRUCTED.value
    assert "not a forward observation" in rec["pit_mode_note"]
    assert rec["available_basis"] == AvailabilityBasis.RECONSTRUCTED.value


def test_capture_before_usable_time_is_forward_observation():
    """在可用时点之前抓到 -> 真实前向观察。"""

    ann = parse_announcements(SAMPLE)[0]
    early = utc(2026, 9, 13, 1, 0)     # 早于 available_at
    rec = to_pit_record(ann, trading_calendar=CALENDAR, first_seen_at=early)
    assert rec["pit_mode"] == PitMode.LIVE_OBSERVED.value
    assert "genuine forward observation" in rec["pit_mode_note"]


def test_two_capture_modes_cannot_share_one_conclusion():
    """同一批公告若混了两种模式，必须被 §7.2 的一致性检查拦住。"""

    from aquant.domain.data.pit import PitRecord, assert_mode_consistent, PitViolation

    ann = parse_announcements(SAMPLE)[0]
    early = to_pit_record(ann, trading_calendar=CALENDAR,
                          first_seen_at=utc(2026, 9, 13, 1, 0))
    late = to_pit_record(ann, trading_calendar=CALENDAR,
                         first_seen_at=utc(2026, 9, 20, 1, 0))

    def as_record(rec):
        return PitRecord(
            record_id=rec["announcement_id"],
            source_published_at=None,
            timestamp_precision=TimestampPrecision(rec["timestamp_precision"]),
            first_seen_at=datetime.fromisoformat(rec["first_seen_at"]),
            available_at=datetime.fromisoformat(rec["available_at"]),
            availability_basis=AvailabilityBasis(rec["available_basis"]),
            pit_mode=PitMode(rec["pit_mode"]),
        )

    with pytest.raises(PitViolation) as exc:
        assert_mode_consistent([as_record(early), as_record(late)], "announcement-set")
    assert "mixes PIT modes" in exc.value.message


def test_availability_rationale_mentions_the_deferral():
    ann = parse_announcements(SAMPLE)[0]
    rec = to_pit_record(ann, trading_calendar=CALENDAR, first_seen_at=utc(2026, 9, 13, 1, 0))
    assert "next trading day" in rec["availability_rationale"]


def test_malformed_payload_yields_no_announcements():
    assert parse_announcements({"announcements": None}) == []
    assert parse_announcements({}) == []
