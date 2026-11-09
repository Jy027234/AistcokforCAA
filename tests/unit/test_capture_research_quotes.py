import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import capture_research_quotes as capture

CST = timezone(timedelta(hours=8))


def test_past_close_date_rejects_uncovered_symbols_but_reuses_complete_checkpoint():
    now = datetime(2026, 11, 2, 1, 0, tzinfo=CST)

    with pytest.raises(ValueError, match="complete verified quote checkpoint"):
        capture.validate_close_capture_window(date(2026, 10, 29), now=now, pending_count=1)

    capture.validate_close_capture_window(date(2026, 10, 29), now=now, pending_count=0)


@pytest.mark.parametrize("now,target", [
    (datetime(2026, 11, 2, 14, 59, tzinfo=CST), date(2026, 11, 2)),
    (datetime(2026, 11, 2, 1, 0, tzinfo=CST), date(2026, 11, 3)),
])
def test_close_capture_rejects_before_close_or_for_a_future_day(now, target):
    with pytest.raises(ValueError, match="market close has not occurred"):
        capture.validate_close_capture_window(target, now=now, pending_count=1)


def test_close_capture_accepts_current_day_after_close():
    capture.validate_close_capture_window(
        date(2026, 11, 2), now=datetime(2026, 11, 2, 15, 0, tzinfo=CST), pending_count=3)


def _install_main_fakes(monkeypatch, *, checkpoint_entry=None):
    instrument = {
        "official_code": "600000", "instrument_id": "SH.600000", "exchange": "SH",
        "board": "SH_MAIN", "exchange_mapping_basis": "SH.60 code pattern",
        "organization_id": "org-600000", "symbol": "sh600000",
    }
    payload_fields = ["x"] * 46
    payload_fields[1] = "浦发银行"
    payload_fields[2] = "600000"
    payload_fields[3] = "10.5"
    payload_fields[4] = "10.4"
    payload_fields[6] = "100"
    payload_fields[30] = "20261029150000"
    payload_fields[37] = "1000"
    payload_fields[45] = "100000"
    payload = f'v_sh600000="{"~".join(payload_fields)}";'.encode("gbk")
    entry = checkpoint_entry or {
        "batch_key": "test-batch", "batch": 1, "requested_symbols": ["sh600000"],
        "requested_count": 1, "status": "ok", "capture_mode": "network_request",
        "receipt_id": "quote-receipt", "url": capture.QUOTE_URL + "sh600000",
        "requested_at": "2026-10-29T12:30:00+00:00",
        "responded_at": "2026-10-29T12:30:01+00:00",
        "first_seen_at": "2026-10-29T12:30:01+00:00",
        "content_hash": "quote-hash", "byte_size": len(payload), "http_status": 200,
        "outcome": "OK", "detail": None,
    }

    class Row(dict):
        pass

    class Connection:
        closed = False

        def execute(self, _query, params):
            if params == ("org-map-receipt",):
                return self._result(Row({
                    "source_id": "cninfo", "url": "org-map-url", "outcome": "OK",
                    "http_status": 200, "content_hash": "org-map-hash",
                }))
            if params == ("quote-receipt",):
                return self._result(Row({
                    "source_id": "tencent-qt", "url": capture.QUOTE_URL + "sh600000",
                    "outcome": "OK", "http_status": 200, "content_hash": "quote-hash",
                    "byte_size": len(payload),
                }))
            raise AssertionError(f"unexpected query parameters: {params!r}")

        @staticmethod
        def _result(row):
            class Result:
                def fetchone(self):
                    return row
            return Result()

        def close(self):
            self.closed = True

    class Archive:
        def __init__(self, _con, _root):
            pass

        @staticmethod
        def verify(content_hash):
            return content_hash in {"org-map-hash", "quote-hash"}

        @staticmethod
        def load_bytes(content_hash):
            assert content_hash == "quote-hash"
            return payload

    client_events = {"constructed": 0, "fetches": 0}

    class Client:
        def __init__(self, *_args, **_kwargs):
            client_events["constructed"] += 1

        def fetch(self, *_args, **_kwargs):
            client_events["fetches"] += 1
            raise AssertionError("network fetch must not run in this test")

    monkeypatch.setattr(capture, "prepare", lambda: ([instrument], [], {
        "receipt_id": "org-map-receipt", "source_url": "org-map-url",
        "content_hash": "org-map-hash", "organization_ids": {"600000": "org-600000"},
    }))
    monkeypatch.setattr(capture, "connect", lambda _path: Connection())
    monkeypatch.setattr(capture, "ForwardArchive", Archive)
    monkeypatch.setattr(capture, "TencentClient", Client)
    return entry, payload, client_events


def test_main_rejects_past_date_with_pending_quotes_before_constructing_client(monkeypatch, tmp_path):
    _entry, _payload, client_events = _install_main_fakes(monkeypatch)
    monkeypatch.setattr(capture, "market_now", lambda: datetime(2026, 11, 2, 1, 0, tzinfo=CST))
    checkpoint = tmp_path / "not-created" / "quote-checkpoint.jsonl"
    monkeypatch.setattr(sys, "argv", [
        "capture_research_quotes", "--close-date", "2026-10-29",
        "--archive-root", str(tmp_path / "archive"), "--output", str(tmp_path / "quotes.json"),
        "--checkpoint", str(checkpoint), "--coverage", str(tmp_path / "coverage.json"),
    ])

    with pytest.raises(ValueError, match="complete verified quote checkpoint"):
        capture.main()

    assert client_events == {"constructed": 0, "fetches": 0}
    assert not checkpoint.parent.exists()
    assert not (tmp_path / "quotes.json").exists()


def test_main_reuses_complete_verified_checkpoint_without_network_client(monkeypatch, tmp_path):
    entry, _payload, client_events = _install_main_fakes(monkeypatch)
    monkeypatch.setattr(capture, "market_now", lambda: datetime(2026, 11, 2, 1, 0, tzinfo=CST))
    checkpoint = tmp_path / "quote-checkpoint.jsonl"
    checkpoint.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    output = tmp_path / "quotes.json"
    coverage = tmp_path / "coverage.json"
    monkeypatch.setattr(sys, "argv", [
        "capture_research_quotes", "--close-date", "2026-10-29",
        "--archive-root", str(tmp_path / "archive"), "--output", str(output),
        "--checkpoint", str(checkpoint), "--coverage", str(coverage),
    ])

    assert capture.main() == 0

    assert client_events == {"constructed": 0, "fetches": 0}
    assert json.loads(output.read_text(encoding="utf-8"))["rows"][0]["close_time_eligible"]
    saved_coverage = json.loads(coverage.read_text(encoding="utf-8"))
    assert saved_coverage["market_date"] == "2026-10-29"
    assert saved_coverage["network_batch_count"] == 1
    assert saved_coverage["batches"][0]["reused_from_verified_checkpoint"]
