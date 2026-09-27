"""Local intake tests use synthetic PDFs/indexes; no provider or real reviewer."""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs

import pytest

from aquant.adapters.providers import pdf_promotion
from aquant.adapters.providers import s2_calendar_evidence as calendars
from aquant.adapters.providers.cninfo import CninfoClient
from aquant.adapters.providers.cninfo_financial_pdf import CninfoS2CandidateFacts, PdfCandidateFact
from aquant.adapters.providers.eastmoney import FetchOutcome
from aquant.domain.data.db import connect
from aquant.domain.data.forward_archive import ForwardArchive
from aquant.domain.fundamentals.fact_repository import FinancialFactRepository
from tools import admit_s2_industrial_pdf_review as intake
from tools import prepare_s2_industrial_pdf_review as worklists

UTC = timezone.utc
CAPTURED = datetime(2026, 9, 27, 1, tzinfo=UTC)
REVIEWED = CAPTURED + timedelta(hours=1)


@pytest.fixture
def case(tmp_path, monkeypatch):
    archive = ForwardArchive(connect(tmp_path / "archive" / "meta.sqlite"), tmp_path / "archive")
    clock = [CAPTURED]
    monkeypatch.setattr(intake, "_now", lambda: clock[0])
    monkeypatch.setattr(pdf_promotion, "_utc_now", lambda: clock[0])
    payload = b"%PDF-synthetic-intake-report"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    url = "https://static.cninfo.com.cn/finalpage/2026-08-31/1001.PDF"
    field = PdfCandidateFact(
        field="revenue", value_yuan=Decimal("100.00"), period_end=date(2024, 6, 30),
        report_period_text="2024年半年度", statement="profit", pdf_page=12,
        column_header="2024年半年度", amount_unit="元", currency="CNY",
        source_row="营业收入\t100.00\t90.00", source_cells=("营业收入", "100.00", "90.00"),
        source_current_cell_index=1, source_prior_cell_index=2, pdf_sha256=digest,
        display_row_label="营业收入",
    )
    candidate = CninfoS2CandidateFacts(instrument_id="000651", period_end=date(2024, 6, 30),
        version_label="indexed_full_report_unreviewed", document_url=url,
        pdf_sha256=digest, candidates=(field,))

    def extract(data, **kwargs):
        assert data == payload
        assert kwargs["instrument_id"] == "000651"
        assert kwargs["required_fields"] == ("revenue",)
        return candidate

    for module in (intake, worklists, pdf_promotion):
        monkeypatch.setattr(module, "extract_cninfo_candidate_facts", extract)
    counter = [0]

    def record(data, target_url, label=None):
        counter[0] += 1
        seen = clock[0] + timedelta(microseconds=counter[0])
        content_hash, _ = archive.store_bytes(data)
        return archive.record(source_id="cninfo", url=target_url, outcome="OK",
            requested_at=seen - timedelta(microseconds=1), responded_at=seen,
            http_status=200, content_hash=content_hash, byte_size=len(data), detail=label)

    receipt = record(payload, url)
    rows = [{"announcementId": "1001", "secCode": "000651", "secName": "格力电器",
        "announcementTitle": "2024年半年度报告",
        "announcementTime": int(datetime(2024, 8, 31, tzinfo=UTC).timestamp() * 1000),
        "adjunctUrl": "/finalpage/2026-08-31/1001.PDF"}]
    client = CninfoClient(archive)
    client._test_more = False
    client._test_split = False
    client._test_total = None

    def fetch(target_url, *, label, **kwargs):
        selected_rows = rows
        if client._test_split:
            page = int(parse_qs(kwargs["data"].decode())["pageNum"][0]) if "topSearch" not in target_url else 1
            selected_rows = rows[page - 1:page]
        data = (json.dumps([{"code": "000651", "orgId": "GD165627"}]).encode()
                if "topSearch" in target_url else json.dumps({
                    "announcements": selected_rows,
                    "totalAnnouncement": client._test_total if client._test_total is not None else len(rows),
                    "hasMore": client._test_more,
                }, ensure_ascii=False).encode())
        archived = record(data, target_url, label)
        return FetchOutcome(True, data, archived.receipt_id, archived.content_hash,
                            None, http_status=200)

    monkeypatch.setattr(client, "_fetch", fetch)
    worksheet = {"schema": "cninfo-industrial-s2-candidates-v1", "sourceId": "cninfo",
        "status": "UNREVIEWED_NOT_PIT_ELIGIBLE", "pitEligible": False, "formalFactCount": 0,
        "reports": [{"instrumentId": "000651", "company": "珠海格力电器股份有限公司",
            "periodEnd": "2024-06-30", "status": "UNREVIEWED_PDF_CANDIDATE_NOT_PIT_ELIGIBLE",
            "pitEligible": False, "announcementId": "1001", "announcedOn": "2024-08-31",
            "title": "2024年半年度报告", "documentUrl": url, "linkStatus": "LINKED_VALUES_UNVERIFIED",
            "versionCount": 1, "versionCountScope": "linked_period_candidates",
            "archiveReceiptId": receipt.receipt_id, "contentHash": digest,
            "byteSize": len(payload), "firstSeenAt": receipt.first_seen_at.isoformat(),
            "fields": [{"field": "revenue", "valueYuan": "100.00", "pdfPage": 12,
                "columnHeader": "2024年半年度", "amountUnit": "元", "sourceCurrentCell": "100.00",
                "sourcePriorCell": "90.00", "displayRowLabel": "营业收入",
                "sourceRow": field.source_row, "reviewStatus": "PENDING_HUMAN_VISUAL"}]}]}
    worklist = worklists.build_review_worklist(worksheet, archive.con, archive.root,
                                             stocks=("000651",), periods=("2024-06-30",))
    paths = {"candidates_path": tmp_path / "candidates.json", "worklist_path": tmp_path / "worklist.json"}
    paths["candidates_path"].write_text(json.dumps(worksheet), encoding="utf-8")
    paths["worklist_path"].write_text(json.dumps(worklist), encoding="utf-8")
    packet = intake.prepare_packet(**paths, archive=archive, client=client,
                                   stock="000651", period="2024-06-30")
    clock[0] = REVIEWED
    def calendar_record(body, source_id, target_url):
        content_hash, _ = archive.store_bytes(body)
        return archive.record(source_id=source_id, url=target_url, outcome="OK",
            requested_at=CAPTURED, responded_at=CAPTURED, http_status=200,
            content_hash=content_hash, byte_size=len(body)).receipt_id

    historical_id = calendar_record(json.dumps({"data": {"sh000001": {"day": [
        [day, "1", "1", "1", "1", "1"] for day in ("2024-07-01", "2024-09-02", "2026-09-24")
    ]}}}).encode(), "tencent-ifzq",
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,2024-07-01,2026-09-24,640,")
    official_id = calendar_record(("<html>关于2026年中秋节、国庆节休市安排的公告 上证公告〔2026〕22号 "
        "9月25日（星期五）至9月27日（星期日）休市，9月28日（星期一）起照常开市。"
        "上海证券交易所 2026年9月17日</html>").encode(), "sse-site", calendars.SSE_URL)
    rules_id = calendar_record(calendars.WEEKDAY_RULE.encode(), "sse-site", calendars.RULES_URL)
    annual_id = calendar_record(("关于上海证券交易所2026年部分节假日休市安排的通知 上证公告〔2025〕45号 "
        "元旦：1月1日至1月3日休市，1月5日起照常开市。春节：2月15日至2月23日休市，2月24日起照常开市。"
        "清明节：4月4日至4月6日休市，4月7日起照常开市。劳动节：5月1日至5月5日休市，5月6日起照常开市。"
        "端午节：6月19日至6月21日休市，6月22日起照常开市。中秋节：9月25日至9月27日休市，9月28日起照常开市。"
        "国庆节：10月1日至10月7日休市，10月8日起照常开市。").encode(), "sse-site", calendars.ANNUAL_URL)
    observed, _ = calendars._historical(archive, historical_id, REVIEWED)
    scheduled, _ = calendars._scheduled(archive, {"receipt_id": official_id,
        "rules": {"receipt_id": rules_id}, "annual": {"receipt_id": annual_id}}, REVIEWED)
    calendar_evidence = {"schema": calendars.SCHEMA, "observed": observed,
        "scheduled": scheduled}
    value = dict(paths, archive=archive, client=client, packet=packet,
                 fact_db=tmp_path / "formal.sqlite", calendar_evidence=calendar_evidence)
    yield value, clock, rows, receipt
    archive.con.close()


def _human_fill(packet, receipt):
    """A synthetic human input fixture, deliberately separate from production code."""
    human = packet["human_review"]
    common = {"reviewer_id": "synthetic-test-human", "reviewed_at": REVIEWED.isoformat()}
    human["fields"][0].update(common, reviewed_value_yuan="100.00", pdf_page=12,
        current_cell="100.00", row_label="营业收入", source_amount_unit="元")
    human["version"].update(common, complete_search_attested=True,
                            rationale="Synthetic original-report relationship inspected.")
    human["cross_category"].update(common)
    for disposition in human["cross_category"]["dispositions"]:
        disposition.update(common)
        if disposition["announcement_id"] == "1001":
            disposition.update(relevance="RELATED", rationale="Synthetic full report for the reviewed period.",
                document_receipt_id=receipt.receipt_id, related_report_announcement_ids=["1001"])
        else:
            disposition.update(relevance="UNRELATED",
                rationale="Synthetic notice body checked; concerns another unrelated reporting period.")


def test_prepare_never_signs_and_blank_review_cannot_write(case):
    args, clock, rows, receipt = case
    human = args["packet"]["human_review"]
    assert human["fields"][0]["reviewer_id"] is None
    assert human["fields"][0]["reviewed_value_yuan"] is None
    assert human["version"]["complete_search_attested"] is False
    assert human["cross_category"]["dispositions"][0]["relevance"] is None
    with pytest.raises(ValueError, match="explicitly filled"):
        intake.admit_packet(**args, commit=True)
    assert not args["fact_db"].exists()


def test_default_dry_run_does_not_create_or_change_formal_database(case):
    args, clock, rows, receipt = case
    _human_fill(args["packet"], receipt)
    clock[0] = REVIEWED + timedelta(minutes=1)
    count = len(args["archive"].receipts_for("cninfo"))
    result = intake.admit_packet(**args)
    assert result["mode"] == "dry-run" and result["written_fact_count"] == 0
    assert result["validated_fact_count"] == 1
    assert not args["fact_db"].exists()
    assert len(args["archive"].receipts_for("cninfo")) > count
    FinancialFactRepository(args["fact_db"])
    before = args["fact_db"].read_bytes()
    intake.admit_packet(**args)
    assert args["fact_db"].read_bytes() == before
    assert FinancialFactRepository.open_existing(args["fact_db"]).load().facts == ()


def test_explicit_commit_preserves_observation_and_reviewed_old_receipts(case):
    args, clock, rows, receipt = case
    _human_fill(args["packet"], receipt)
    clock[0] = REVIEWED + timedelta(minutes=1)
    result = intake.admit_packet(**args, commit=True)
    assert result["written_fact_count"] == 1
    repo = FinancialFactRepository.open_existing(args["fact_db"])
    store, accepted = repo.load_with_accepted_s2_pdf_reviews()
    assert len(store.facts) == 1 and len(accepted) == 1
    fact = store.facts[0]
    assert fact.first_seen_at == receipt.first_seen_at
    assert fact.ingested_at == clock[0] and fact.available_at > clock[0]
    assert fact.source_published_date == date(2024, 8, 31)
    with closing(connect(args["fact_db"], read_only=True)) as con:
        evidence = json.loads(con.execute("SELECT evidence_json FROM financial_fact_review").fetchone()[0])
    reviewed_receipts = [page["receipt_id"] for page in args["packet"]["regular_index"]["pages"]]
    assert evidence["index_receipt_ids"] == reviewed_receipts
    assert evidence["local_intake"]["freshness_regular_receipts"] != reviewed_receipts
    assert evidence["local_intake"]["version_rationale"].startswith("Synthetic")
    assert evidence["local_intake"]["calendar_evidence"] == args["calendar_evidence"]
    assert fact.available_at == datetime(2026, 9, 28, 0, 45, tzinfo=UTC)


@pytest.mark.parametrize("mutation", ["cell", "missing_disposition", "future_time", "candidate_file"])
def test_invalid_reviews_never_partially_commit(case, mutation):
    args, clock, rows, receipt = case
    _human_fill(args["packet"], receipt)
    clock[0] = REVIEWED + timedelta(minutes=1)
    human = args["packet"]["human_review"]
    if mutation == "cell":
        human["fields"][0]["current_cell"] = "90.00"
    elif mutation == "missing_disposition":
        human["cross_category"]["dispositions"] = []
    elif mutation == "future_time":
        human["version"]["reviewed_at"] = (clock[0] + timedelta(hours=1)).isoformat()
    else:
        args["candidates_path"].write_text(args["candidates_path"].read_text() + "\n")
    with pytest.raises(ValueError):
        intake.admit_packet(**args, commit=True)
    assert not args["fact_db"].exists()


def test_added_correction_or_next_day_requires_new_review(case):
    args, clock, rows, receipt = case
    _human_fill(args["packet"], receipt)
    clock[0] = REVIEWED + timedelta(minutes=1)
    rows.append({**rows[0], "announcementId": "1002", "announcementTitle": "会计差错更正公告",
                 "adjunctUrl": "/finalpage/2026-08-31/1002.PDF"})
    with pytest.raises(ValueError, match="official index changed"):
        intake.admit_packet(**args, commit=True)
    clock[0] += timedelta(days=1)
    with pytest.raises(ValueError, match="today's Beijing date"):
        intake.admit_packet(**args, commit=True)
    assert not args["fact_db"].exists()


def test_forged_index_scope_is_rejected_against_archived_post(case):
    args, clock, rows, receipt = case
    _human_fill(args["packet"], receipt)
    clock[0] = REVIEWED + timedelta(minutes=1)
    args["packet"]["regular_index"]["organization_id"] = "gssz0000651"
    with pytest.raises(ValueError, match="orgId"):
        intake.admit_packet(**args, commit=True)
    assert not args["fact_db"].exists()


def _reprepare(args, clock):
    clock[0] = CAPTURED + timedelta(minutes=10)
    args["packet"] = intake.prepare_packet(worklist_path=args["worklist_path"],
        candidates_path=args["candidates_path"], archive=args["archive"], client=args["client"],
        stock="000651", period="2024-06-30")
    clock[0] = REVIEWED + timedelta(minutes=1)


@pytest.mark.parametrize("split", [False, True])
def test_total_reached_with_missing_has_more_is_verified_end_to_end(case, split):
    args, clock, rows, receipt = case
    args["client"]._test_more = None
    args["client"]._test_split = split
    if split:
        rows.append({**rows[0], "announcementId": "1002", "announcementTitle": "董事会决议公告",
                     "adjunctUrl": "/finalpage/2026-08-31/1002.PDF"})
    _reprepare(args, clock)
    assert args["packet"]["regular_index"]["termination"] == "total_reached"
    assert len(args["packet"]["regular_index"]["pages"]) == (2 if split else 1)
    _human_fill(args["packet"], receipt)
    assert intake.admit_packet(**args, commit=True)["written_fact_count"] == 1


def test_removed_packet_candidate_cannot_hide_archived_correction(case):
    args, clock, rows, receipt = case
    rows.append({**rows[0], "announcementId": "1002", "announcementTitle": "会计差错更正公告",
                 "adjunctUrl": "/finalpage/2026-08-31/1002.PDF"})
    _reprepare(args, clock)
    _human_fill(args["packet"], receipt)
    args["packet"]["cross_category_index"]["announcements"] = [item for item in
        args["packet"]["cross_category_index"]["announcements"] if item["announcement_id"] != "1002"]
    args["packet"]["human_review"]["cross_category"]["dispositions"] = [item for item in
        args["packet"]["human_review"]["cross_category"]["dispositions"] if item["announcement_id"] != "1002"]
    with pytest.raises(ValueError, match="differs from archived pages"):
        intake.admit_packet(**args, commit=True)
    assert not args["fact_db"].exists()


def test_total_reached_omitted_page_or_changed_total_cannot_commit(case):
    args, clock, rows, receipt = case
    args["client"]._test_more = None
    _reprepare(args, clock)
    _human_fill(args["packet"], receipt)
    args["client"]._test_total = 2
    with pytest.raises(ValueError, match="indexes incomplete"):
        intake.admit_packet(**args, commit=True)
    assert not args["fact_db"].exists()


@pytest.mark.parametrize("change", ["bare_array", "future_day", "hash", "url", "observed_time"])
def test_calendar_evidence_cannot_be_forged_into_committed_facts(case, change):
    args, clock, rows, receipt = case
    _human_fill(args["packet"], receipt)
    if change == "bare_array":
        args["calendar_evidence"] = ["2026-09-28", "2026-09-29"]
    elif change == "future_day":
        args["calendar_evidence"]["observed"]["trading_days"].append("2026-09-29")
    else:
        key = {"hash": "content_hash", "url": "url", "observed_time": "first_seen_at"}[change]
        args["calendar_evidence"]["scheduled"][key] = "forged"
    with pytest.raises(ValueError, match="calendar"):
        intake.admit_packet(**args, commit=True)
    assert not args["fact_db"].exists()


def test_official_rules_support_planned_weekdays_and_year_boundary(case):
    args, *_ = case
    _, planned = calendars._scheduled(args["archive"], args["calendar_evidence"]["scheduled"], REVIEWED)
    assert date(2026, 9, 28) in planned and date(2026, 9, 29) in planned
    assert date(2026, 10, 8) in planned
    assert not any(date(2026, 10, 1) <= day <= date(2026, 10, 7) for day in planned)
    assert date(2026, 10, 10) not in planned  # Government make-up Saturday remains closed.
    with pytest.raises(ValueError, match="year/window"):
        calendars.verify_calendar_evidence(args["calendar_evidence"], args["archive"],
            now=datetime(2027, 1, 1, 0, 45, tzinfo=UTC))
    with pytest.raises(ValueError, match="publication-date floor"):
        calendars.verify_calendar_evidence(args["calendar_evidence"], args["archive"],
            now=REVIEWED, published_on=date(2024, 6, 1))


@pytest.mark.parametrize("bad", ["2025", "港股通", "9月29日"])
def test_official_notice_wrong_year_scope_or_reopening_rejected(case, bad):
    args, *_ = case
    archive = args["archive"]
    old = args["calendar_evidence"]["scheduled"]
    body = archive.load_bytes(old["content_hash"]).decode()
    before = {"2025": "2026", "港股通": "休市安排", "9月29日": "9月28日"}[bad]
    changed = body.replace(before, bad).encode()
    digest, _ = archive.store_bytes(changed)
    new = archive.record(source_id="sse-site", url=calendars.SSE_URL, outcome="OK",
        requested_at=CAPTURED + timedelta(minutes=1), responded_at=CAPTURED + timedelta(minutes=1),
        http_status=200, content_hash=digest, byte_size=len(changed))
    with pytest.raises(ValueError, match="SSE notice"):
        calendars._reopening(archive, new.receipt_id, REVIEWED)


@pytest.mark.parametrize("now,last_day,next_day", [
    (datetime(2026, 9, 28, 0, 46, tzinfo=UTC), "2026-09-24", "2026-09-29"),
    (datetime(2026, 9, 30, 2, tzinfo=UTC), "2026-09-29", "2026-10-08"),
    (datetime(2026, 12, 31, 2, tzinfo=UTC), "2026-12-30", None),
])
def test_fresh_calendar_crosses_cutoff_and_holiday_but_not_year(case, now, last_day, next_day):
    args, *_ = case
    archive = args["archive"]
    packet = args["calendar_evidence"]

    def refresh(ref, body=None, url=None):
        payload = body or archive.load_bytes(ref["content_hash"])
        digest, _ = archive.store_bytes(payload)
        return archive.record(source_id=ref["source_id"], url=url or ref["url"], outcome="OK",
            requested_at=now - timedelta(seconds=1), responded_at=now - timedelta(seconds=1),
            http_status=200, content_hash=digest, byte_size=len(payload)).receipt_id

    refs = {"receipt_id": refresh(packet["scheduled"]),
        "rules": {"receipt_id": refresh(packet["scheduled"]["rules"])},
        "annual": {"receipt_id": refresh(packet["scheduled"]["annual"])}}
    scheduled, _ = calendars._scheduled(archive, refs, now)
    payload = json.loads(archive.load_bytes(packet["observed"]["content_hash"]))
    payload["data"]["sh000001"]["day"][-1][0] = last_day
    hist_id = refresh(packet["observed"], body=json.dumps(payload).encode(),
        url=packet["observed"]["url"].replace("2026-09-24", last_day))
    observed, _ = calendars._historical(archive, hist_id, now)
    fresh = {"schema": calendars.SCHEMA, "scheduled": scheduled, "observed": observed}
    if next_day is None:
        with pytest.raises(ValueError, match="exhausted"):
            calendars.verify_calendar_evidence(fresh, archive, now=now)
    else:
        days, _ = calendars.verify_calendar_evidence(fresh, archive, now=now)
        assert min(day for day in days if calendars.preopen_instant(day) > now).isoformat() == next_day


def test_official_capture_checks_and_archives_raw_response(case, monkeypatch):
    from io import BytesIO

    args, *_ = case
    archive = args["archive"]
    body = archive.load_bytes(args["calendar_evidence"]["scheduled"]["content_hash"])

    class Response(BytesIO):
        status = 200
        headers = {"Content-Length": str(len(body))}

        def geturl(self):
            return calendars.SSE_URL

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == calendars.SSE_URL and timeout == 20
            return Response(body)

    monkeypatch.setattr(calendars.urllib.request, "build_opener", lambda handler: Opener())
    policy = calendars.FetchPolicy(allowed_hosts=frozenset({"www.sse.com.cn"}), resolve_dns=False)
    receipt_id = calendars.capture_reopening(archive, policy=policy)
    ref, captured = calendars._reference(archive, receipt_id, datetime.now(UTC))
    assert captured == body and ref["url"] == calendars.SSE_URL
    with pytest.raises(ValueError, match="unsupported"):
        calendars.capture_reopening(archive, url="https://example.com/calendar", policy=policy)
