"""扩采路径离线验证；合成行情/日历收据不表示真实供应商采集。"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from aquant.adapters.providers import s2_calendar_evidence as calendars
from aquant.adapters.providers.eastmoney import FetchOutcome
from aquant.domain.data.db import connect
from aquant.domain.data.forward_archive import ForwardArchive
from aquant.operations.decision_market_caps import load_sidecar
from tools import collect_market_caps_tencent as tool

DAY = date(2026, 10, 9)
NOW = datetime(2026, 10, 9, 7, 5, tzinfo=timezone.utc)


def quote(symbol="sh600519", *, code=None, stamp="20261009150000", cap="123.456789123"):
    fields = [""] * 60
    fields[2], fields[3], fields[30], fields[45] = code or symbol[2:], "12.34", stamp, cap
    return (f'v_{symbol}="' + "~".join(fields) + '";').encode("gbk")


@pytest.mark.parametrize("stamp,cap,code,reason", [
    ("20261008150000", "123", None, "stale_or_wrong_market_day"),
    ("20261009145959", "123", None, "before_market_close"),
    ("20261032150000", "123", None, "invalid_market_time"),
    ("20261009150000", "123", "000651", "identity_mismatch"),
    ("", "123", None, "invalid_market_time"),
    ("20261009150000", "NaN", None, "invalid_market_cap"),
    ("20261009150000", "Infinity", None, "invalid_market_cap"),
    ("20261009150000", "-1", None, "invalid_market_cap"),
    ("20261009150000", "0", None, "invalid_market_cap"),
    ("20261009150000", "n/a", None, "invalid_market_cap"),
])
def test_parser_rejects_actual_wrong_time_identity_and_cap(stamp, cap, code, reason):
    values, excluded = tool.parse_closing_market_caps(
        quote(stamp=stamp, cap=cap, code=code), symbols=["sh600519"], market_day=DAY)
    assert values == {}
    assert excluded[0]["reason"] == reason
    assert excluded[0]["instrument_id"] == "SH.600519"


def test_exact_decimal_requested_identity_and_missing_response():
    values, excluded = tool.parse_closing_market_caps(
        quote() + quote("sz000651"), symbols=["sh600519", "sh600000"], market_day=DAY)
    assert values == {"SH.600519": Decimal("12345678912.3")}
    assert {row["reason"] for row in excluded} == {"missing_response", "unexpected_symbol"}


def test_duplicate_response_cannot_choose_arbitrary_cap():
    values, excluded = tool.parse_closing_market_caps(
        quote() + quote(cap="999"), symbols=["sh600519"], market_day=DAY)
    assert not values
    assert excluded[0]["reason"] == "duplicate_response"


def test_universe_scope_is_strict_explicit_and_deduplicated(tmp_path):
    path = tmp_path / "universe.txt"
    path.write_text("600519 000651 300750 688981 920002 430017 900001 200002 600519 sh600519 ６００５１９ 651")
    plan = tool.prepare_universe(path)
    assert plan["symbols"] == ["sh600519", "sz000651", "sz300750", "sh688981"]
    assert plan["supported_count"] == 4 and plan["unsupported_count"] == 7
    assert plan["duplicate_count"] == 1
    assert {row["code"] for row in plan["unsupported"]} >= {"920002", "430017", "sh600519", "６００５１９"}


@pytest.mark.parametrize("now,as_of", [
    (datetime(2026, 10, 9, 6, 59, tzinfo=timezone.utc), "2026-10-09"),
    (NOW, "2026-10-08"), (NOW, "2026-10-10"),
])
def test_cli_rejects_preclose_and_wrong_day_before_archive_or_network(tmp_path, monkeypatch, now, as_of):
    monkeypatch.setattr(tool, "_now", lambda: now)
    monkeypatch.setattr(tool, "TencentClient", lambda *a, **k: pytest.fail("network path entered"))
    archive = tmp_path / "absent_archive"
    assert tool.main(["--as-of", as_of, "--archive-root", str(archive)]) == 2
    assert not archive.exists()


def test_dry_preparation_works_preclose_offline_and_displays_default_threshold(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tool, "_now", lambda: NOW - timedelta(hours=1))
    monkeypatch.setattr(tool, "TencentClient", lambda *a, **k: pytest.fail("network path entered"))
    universe = tmp_path / "universe.txt"
    universe.write_text("600519 920002")
    assert tool.main(["--dry-run", "--universe", str(universe)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["plan"]["supported_count"] == 1
    assert report["plan"]["unsupported"][0]["code"] == "920002"
    assert report["min_coverage"] == "0.95"
    assert "data" in report["output"] and "20261009" in report["output"]
    assert not report["published"]


def _calendar(archive, now):
    seen = now - timedelta(minutes=1)
    def record(payload, source, url):
        digest, _ = archive.store_bytes(payload)
        return archive.record(source_id=source, url=url, outcome="OK", requested_at=seen,
            responded_at=seen, http_status=200, content_hash=digest, byte_size=len(payload)).receipt_id
    # Historical dates are purely synthetic, but real archive and evidence verification are used.
    last_day = "2026-10-08" if now.date() == DAY else "2026-10-09"
    hist = record(json.dumps({"data": {"sh000001": {"day": [
        [day, "1", "1", "1", "1", "1"] for day in ("2024-07-01", last_day)
    ]}}}).encode(), "tencent-ifzq",
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,2024-07-01,{last_day},640,")
    official = record(("关于2026年中秋节、国庆节休市安排的公告 上证公告〔2026〕22号 "
        "9月25日（星期五）至9月27日（星期日）休市，9月28日（星期一）起照常开市。"
        "上海证券交易所 2026年9月17日").encode(), "sse-site", calendars.SSE_URL)
    rules = record(calendars.WEEKDAY_RULE.encode(), "sse-site", calendars.RULES_URL)
    holiday_text = "关于上海证券交易所2026年部分节假日休市安排的通知 上证公告〔2025〕45号 "
    for name, begin, end, resumed in calendars.HOLIDAYS:
        first, last, resume = map(date.fromisoformat, (begin, end, resumed))
        holiday_text += (f"{name}：{first.month}月{first.day}日至{last.month}月{last.day}日休市，"
                         f"{resume.month}月{resume.day}日起照常开市。")
    annual = record(holiday_text.encode(), "sse-site", calendars.ANNUAL_URL)
    observed, _ = calendars._historical(archive, hist, now)
    scheduled, _ = calendars._scheduled(archive, {"receipt_id": official,
        "rules": {"receipt_id": rules}, "annual": {"receipt_id": annual}}, now)
    return {"schema": calendars.SCHEMA, "observed": observed, "scheduled": scheduled}


@pytest.fixture
def collection(tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "_now", lambda: NOW)
    archive_root = tmp_path / "archive"
    archive = ForwardArchive(connect(archive_root / "meta.sqlite"), archive_root)
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(_calendar(archive, NOW)), encoding="utf-8")
    archive.con.close()
    universe = tmp_path / "universe.txt"
    universe.write_text("\n".join([f"{600000 + i:06}" for i in range(20)]) + "\n920002")
    output = tmp_path / "caps.json"
    calls = []
    def install(*, stale_count=1, transport_failure=False):
        def fetch(client, url, **kwargs):
            calls.append(url)
            if transport_failure:
                return FetchOutcome(False, None, "failed-fixture", None, "transport error", 503)
            symbols = url.split("q=", 1)[1].split(",")
            payload = b"".join(quote(symbol, stamp="20261008150000" if int(symbol[2:]) < 600000 + stale_count
                else "20261009150000") for symbol in symbols)
            # An out-of-batch known-universe ID must not be accepted in this batch.
            if "sh600019" not in symbols:
                payload += quote("sh600019")
            digest, _ = client.archive.store_bytes(payload)
            seen = NOW - timedelta(seconds=20 - len(calls))
            receipt = client.archive.record(source_id=tool.SOURCE_ID, url=url, outcome="OK",
                requested_at=seen, responded_at=seen, http_status=200,
                content_hash=digest, byte_size=len(payload))
            return FetchOutcome(True, payload, receipt.receipt_id, digest, None, 200)
        monkeypatch.setattr(tool.TencentClient, "fetch", fetch)
    args = ["--universe", str(universe), "--calendar", str(path), "--as-of", DAY.isoformat(),
            "--archive-root", str(archive_root), "--output", str(output), "--batch-size", "10"]
    return args, output, archive_root, calls, install, path


def test_published_partial_sidecar_passes_real_load_archive_and_keeps_raw_receipts(collection):
    args, output, archive_root, calls, install, _ = collection
    install()
    assert tool.main(args) == 0
    sidecar = load_sidecar(output, expected_as_of=DAY, archive_root=archive_root)
    assert len(sidecar.items) == 19
    assert "SH.600000" not in sidecar.items
    assert sidecar.items["SH.600001"].market_cap_cents == 1234567891230
    report = json.loads(output.with_suffix(".coverage.json").read_text(encoding="utf-8"))
    assert report["published"] and not report["complete_coverage"]
    assert report["coverage"] == "0.95" and report["excluded_count"] == 1
    assert report["plan"]["unsupported_count"] == 1
    assert report["batches"][0]["exclusions"][0]["market_time"] == "20261008150000"
    archive = ForwardArchive(connect(archive_root / "meta.sqlite"), archive_root)
    try:
        manifest = json.loads(archive.load_bytes(sidecar.content_hash))
        assert manifest["items_hash"] and len(manifest["batches"]) == 2
        for batch, actual_url in zip(manifest["batches"], calls):
            receipt = archive.con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?",
                                          (batch["receipt_id"],)).fetchone()
            assert receipt["url"] == actual_url
            assert archive.verify(batch["content_hash"])
            assert b"20261009150000" in archive.load_bytes(batch["content_hash"])
    finally:
        archive.con.close()


def test_below_default_threshold_cannot_replace_existing_sidecar(collection):
    args, output, _, _, install, _ = collection
    output.write_text("existing-sidecar", encoding="utf-8")
    install(stale_count=2)
    assert tool.main(args) == 2
    assert output.read_text(encoding="utf-8") == "existing-sidecar"
    report = json.loads(output.with_suffix(".coverage.json").read_text(encoding="utf-8"))
    assert not report["published"] and report["accepted_count"] == 18
    assert "低于阈值" in report["error"]


def test_failed_batch_cannot_publish(collection):
    args, output, _, _, install, _ = collection
    install(transport_failure=True)
    assert tool.main(args) == 2
    assert not output.exists()
    assert "failed-fixture" in output.with_suffix(".coverage.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("change", ["bare", "forged", "incomplete"])
def test_bare_or_forged_calendar_rejects_before_network(collection, change):
    args, output, _, calls, install, path = collection
    install()
    packet = json.loads(path.read_text(encoding="utf-8"))
    if change == "bare":
        packet = ["2026-10-09"]
    elif change == "forged":
        packet["scheduled"]["content_hash"] = "sha256:forged"
    else:
        packet.pop("scheduled")
    path.write_text(json.dumps(packet), encoding="utf-8")
    assert tool.main(args) == 2
    assert not calls and not output.exists()


def test_official_weekend_calendar_rejects_before_network(tmp_path, monkeypatch):
    saturday = NOW + timedelta(days=1)
    monkeypatch.setattr(tool, "_now", lambda: saturday)
    monkeypatch.setattr(tool.TencentClient, "fetch", lambda *a, **k: pytest.fail("network entered"))
    archive_root = tmp_path / "archive"
    archive = ForwardArchive(connect(archive_root / "meta.sqlite"), archive_root)
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(_calendar(archive, saturday)), encoding="utf-8")
    archive.con.close()
    universe = tmp_path / "universe.txt"
    universe.write_text("600519")
    assert tool.main(["--universe", str(universe), "--calendar", str(calendar),
                      "--archive-root", str(archive_root), "--output", str(tmp_path / "caps.json")]) == 2
    assert not (tmp_path / "caps.json").exists()
