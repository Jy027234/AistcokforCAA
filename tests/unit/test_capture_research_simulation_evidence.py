"""Offline tests for the daily simulation evidence capture helpers."""
from datetime import date
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "capture_research_simulation_evidence.py"
SPEC = importlib.util.spec_from_file_location("capture_simulation_evidence", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)
from aquant.adapters.providers.baostock import BaostockUnavailable


def test_calendar_range_uses_target_year():
    assert MODULE.calendar_range(date(2027, 2, 3)) == ("2027-01-01", "2027-12-31")


def test_trading_window_uses_only_dates_through_target():
    rows = [{"calendar_date": f"2026-01-{day:02d}", "is_trading_day": "1"}
            for day in range(1, 6)]
    rows.append({"calendar_date": "2026-01-06", "is_trading_day": "1"})
    days, start = MODULE.trading_window(rows, "2026-01-05", count=3)
    assert days == ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]
    assert start == "2026-01-03"


def test_resume_requires_same_date_research_hash_and_pool(tmp_path):
    research = tmp_path / "staged.json"
    research.write_text("{}", encoding="utf-8")
    checkpoint = {"capture_binding": {"target_day": "2026-10-12",
        "research_path": str(research.resolve()), "research_hash": "sha256:abc",
        "selected_instrument_ids": ["SZ.000001"], "retained_path": None,
        "retained_hash": None}}
    MODULE.checkpoint_for_resume(checkpoint, target="2026-10-12", research_path=research,
        research_hash="sha256:abc", selected_ids=["SZ.000001"])
    with pytest.raises(ValueError, match="different target day"):
        MODULE.checkpoint_for_resume(checkpoint, target="2026-10-13", research_path=research,
            research_hash="sha256:abc", selected_ids=["SZ.000001"])
    with pytest.raises(ValueError, match="different target day"):
        MODULE.checkpoint_for_resume(checkpoint, target="2026-10-12", research_path=research,
            research_hash="sha256:changed", selected_ids=["SZ.000001"])


def test_target_bar_requires_real_ohlc_and_status_fields():
    complete = [{"trading_day": "2026-10-12", "open_cents": 101, "high_cents": 105,
                 "low_cents": 100, "close_cents": 104, "tradestatus": "1", "isST": "0"}]
    assert MODULE.target_bar_is_complete(complete, "2026-10-12")
    for key in ("open_cents", "high_cents", "low_cents", "close_cents", "tradestatus", "isST"):
        row = dict(complete[0])
        row[key] = None
        assert not MODULE.target_bar_is_complete([row], "2026-10-12")
    assert not MODULE.target_bar_is_complete(complete, "2026-10-13")


def test_resume_skips_only_complete_verified_checkpoint_item():
    item = {"bars": [{"trading_day": "2026-10-12", "open_cents": 100, "high_cents": 101,
             "low_cents": 99, "close_cents": 100, "tradestatus": "1", "isST": "0"}],
        "simulation_eligibility": {"status": "VERIFIED", "as_of_date": "2026-10-12"},
        "listed_on": "2000-01-01", "listed_on_source": {"receipt_id": "basic"},
        "identity": {"official_basic_row": {"code": "sz.000001"},
                     "active_in_all_stock_on_target_day": True}}
    assert MODULE.checkpoint_item_is_complete(item, "2026-10-12")
    unknown = dict(item, simulation_eligibility={"status": "UNKNOWN", "as_of_date": "2026-10-12"})
    assert not MODULE.checkpoint_item_is_complete(unknown, "2026-10-12")


def test_incomplete_checkpoint_recapture_replaces_full_window_from_new_receipt():
    # Model a resumed item whose old receipt ends before the target session.
    old_bars = [{"trading_day": "2026-10-09", "source_receipt_id": "old-tail-receipt"}]
    raw_rows = [
        {"date": day, "open": "1.00", "high": "1.05", "low": "0.95", "close": "1.02",
         "preclose": "1.00", "volume": "10", "amount": "10.2", "tradestatus": "1", "isST": "0"}
        for day in ("2026-10-08", "2026-10-09", "2026-10-12")]
    new_receipt = {"receipt_id": "full-window-receipt", "content_hash": "sha256:new",
                   "first_seen_at": "2026-10-12T08:00:00+00:00"}
    recaptured = MODULE.build_captured_bars(raw_rows, {row["date"]: 102 for row in raw_rows},
        start_day="2026-10-08", target_day="2026-10-12", receipt=new_receipt)
    # Recovery installs this complete response in place of old_bars; every row is
    # now backed by the one receipt referenced from the instrument item.
    assert [bar["trading_day"] for bar in recaptured] == ["2026-10-08", "2026-10-09", "2026-10-12"]
    assert all(bar["source_receipt_id"] == new_receipt["receipt_id"] for bar in recaptured)
    assert old_bars[0]["source_receipt_id"] not in {bar["source_receipt_id"] for bar in recaptured}


def test_fresh_basic_listing_source_replaces_incomplete_checkpoint_source():
    old_item = {"listed_on": None, "listed_on_source": {
        "source_id": "baostock-socket", "receipt_id": "old-date-dictionary",
        "content_hash": "sha256:old", "available_at": "2026-10-14T08:00:00+00:00"}}
    selected_basic = {"receipt_id": "fresh-basic", "content_hash": "sha256:new",
                      "first_seen_at": "2026-10-15T13:00:00+00:00"}
    source = MODULE.listed_on_source_from_basic_receipt(selected_basic)
    assert source == {"source_id": "baostock-socket", "receipt_id": "fresh-basic",
                      "content_hash": "sha256:new", "available_at": selected_basic["first_seen_at"]}
    assert source != old_item["listed_on_source"]
    assert old_item["listed_on_source"]["receipt_id"] == "old-date-dictionary"


def test_reused_valid_basic_listing_source_preserves_original_provenance():
    old_source = {"source_id": "baostock-socket", "receipt_id": "original-basic",
                  "content_hash": "sha256:original", "available_at": "2026-10-14T08:00:00+00:00"}
    # The existing resume branch reconstructs basic_rcpt from this saved source.
    selected_basic = {"receipt_id": old_source["receipt_id"],
                      "content_hash": old_source["content_hash"],
                      "first_seen_at": old_source["available_at"]}
    assert MODULE.listed_on_source_from_basic_receipt(selected_basic) == old_source


def test_top_ranked_selection_unions_nonranked_retained_main_candidate():
    research = {"candidates": [
        {"instrumentId": "SZ.000001", "code": "000001", "status": "RANKED", "board": "MAIN",
         "exchange": "SZSE", "industry": "Finance", "position": 1, "signalRank": 0.9},
        {"instrumentId": "SH.600002", "code": "600002", "status": "WATCH", "board": "MAIN",
         "exchange": "SSE", "industry": "Industry", "position": None, "signalRank": None},
    ]}
    selected = MODULE.select_candidates(research, limit=1, retained_ids=["SH.600002"])
    assert [row["instrumentId"] for row in selected] == ["SZ.000001", "SH.600002"]
    assert selected[-1]["position"] is None and selected[-1]["signalRank"] is None


def test_retained_candidate_without_exact_source_identity_fails():
    research = {"candidates": [{"instrumentId": "SH.600002", "code": "600002",
        "status": "WATCH", "board": "MAIN", "exchange": "SSE"}]}
    with pytest.raises(ValueError, match="lacks exact MAIN identity/source"):
        MODULE.select_candidates(research, limit=1, retained_ids=["SH.600002"])


def test_retained_document_requires_exact_sorted_main_ids():
    document = {"schema": "aquant.simulation_retained_instruments.v1", "trading_day": "2026-10-12",
        "instrument_ids": ["SZ.000001", "SH.600002"],
        "reasons": {"SZ.000001": [{"kind": "ACTIVE_HOLDING", "portfolio_id": "pf",
                                      "quantity_remaining": 100}],
                    "SH.600002": [{"kind": "PENDING_DRAFT", "draft_id": "draft", "portfolio_id": "pf",
                                    "expected_execution_day": "2026-10-12", "research_content_hash": "sha256:x"}]},
        "skipped_instruments": []}
    with pytest.raises(ValueError, match="sorted, unique"):
        MODULE.validate_retained_document(document, "2026-10-12")


def test_retained_document_accepts_existing_and_forward_pool_reasons():
    document = {"schema": "aquant.simulation_retained_instruments.v1", "trading_day": "2026-10-12",
        "instrument_ids": ["SH.600001"],
        "reasons": {"SH.600001": [
            {"kind": "PENDING_DRAFT", "draft_id": "draft", "portfolio_id": "pf",
             "expected_execution_day": "2026-10-12", "research_content_hash": "sha256:x"},
            {"kind": "ACTIVE_HOLDING", "portfolio_id": "pf", "quantity_remaining": 100},
            {"kind": "FORWARD_COMPARISON_COMMON_POOL", "series_id": "series",
             "first_trading_day": "2026-10-12", "spec_content_hash": "sha256:" + "a" * 64},
        ]}, "skipped_instruments": []}
    assert MODULE.validate_retained_document(document, "2026-10-12") == ["SH.600001"]


@pytest.mark.parametrize("missing", ["series_id", "first_trading_day", "spec_content_hash"])
def test_forward_pool_reason_requires_all_spec_fields(missing):
    reason = {"kind": "FORWARD_COMPARISON_COMMON_POOL", "series_id": "series",
              "first_trading_day": "2026-10-12", "spec_content_hash": "sha256:" + "a" * 64}
    del reason[missing]
    document = {"schema": "aquant.simulation_retained_instruments.v1", "trading_day": "2026-10-12",
        "instrument_ids": ["SH.600001"], "reasons": {"SH.600001": [reason]},
        "skipped_instruments": []}
    with pytest.raises(ValueError, match="invalid reason record"):
        MODULE.validate_retained_document(document, "2026-10-12")


def test_retained_document_rejects_unknown_reason_kind():
    document = {"schema": "aquant.simulation_retained_instruments.v1", "trading_day": "2026-10-12",
        "instrument_ids": ["SH.600001"],
        "reasons": {"SH.600001": [{"kind": "SOMETHING_NEW", "series_id": "series"}]},
        "skipped_instruments": []}
    with pytest.raises(ValueError, match="invalid reason record"):
        MODULE.validate_retained_document(document, "2026-10-12")


def test_retained_input_path_and_hash_are_recorded(tmp_path):
    research = tmp_path / "research.json"
    retained = tmp_path / "retained.json"
    research.write_text("{}", encoding="utf-8")
    retained.write_text('{"schema":"fixture"}', encoding="utf-8")
    sources = MODULE.research_source_inputs({}, research, retained)
    assert sources["retained_instruments"] == {
        "path": str(retained.resolve()), "sha256": MODULE.digest(retained)}


def test_research_provenance_uses_configured_host_relocation_and_checks_bytes(tmp_path, monkeypatch):
    research = tmp_path / "research.json"
    source = tmp_path / "bound.json"
    research.write_text("{}", encoding="utf-8")
    source.write_text('{"records":[]}', encoding="utf-8")
    declared_path = r"E:\IT\A股量化交易\bound.json"
    packet = {"provenance": [{"role": "bound_records", "path": declared_path,
                              "contentHash": MODULE.digest(source)}]}
    original = json.dumps(packet, ensure_ascii=False)
    monkeypatch.setenv("AQUANT_ARTIFACT_PATH_MAP", json.dumps({
        r"E:\IT\A股量化交易": str(tmp_path)}))
    sources = MODULE.research_source_inputs(packet, research)
    assert sources["provenance:bound_records"]["path"] == str(source.resolve())
    assert sources["provenance:bound_records"]["sha256"] == MODULE.digest(source)
    assert json.dumps(packet, ensure_ascii=False) == original
    source.write_text('{"records":["changed"]}', encoding="utf-8")
    with pytest.raises(ValueError, match="provenance hash mismatch"):
        MODULE.research_source_inputs(packet, research)


def test_halted_target_bar_preserves_nullable_prices():
    halted = [{"trading_day": "2026-10-12", "open_cents": None, "high_cents": None,
               "low_cents": None, "close_cents": None, "tradestatus": "0", "isST": "0"}]
    assert MODULE.target_bar_is_complete(halted, "2026-10-12")
    traded_missing_price = [dict(halted[0], tradestatus="1")]
    assert not MODULE.target_bar_is_complete(traded_missing_price, "2026-10-12")


def test_login_timeout_closes_stale_socket_then_retries_once(tmp_path, monkeypatch, capsys):
    stale_socket = SimpleNamespace(closed=False)
    def close_old_socket():
        stale_socket.closed = True
    stale_socket.close = close_old_socket
    context = SimpleNamespace(default_socket=None, user_id="anonymous")
    monkeypatch.setattr(MODULE.importlib, "import_module",
                        lambda name: SimpleNamespace(conx=context))

    class Client:
        _bs = None
        def __init__(self):
            self.calls = 0
        def _restore_socket_timeout(self):
            pass
        def login(self):
            self.calls += 1
            if self.calls == 1:
                context.default_socket = stale_socket
                print("timed out")
                raise BaostockUnavailable(
                    "baostock login failed: 10002007 网络接收错误。")
            assert context.default_socket is None
            assert not hasattr(context, "user_id")
            self._bs = object()
            print("login success!")

    client = Client()
    delays = []
    log_path = tmp_path / "login-attempts.json"
    attempts = MODULE.login_with_single_retry(client, log_path, sleep_fn=delays.append)

    saved = json.loads(log_path.read_text(encoding="utf-8"))
    assert stale_socket.closed
    assert client.calls == 2 and delays == [5]
    assert [x["status"] for x in attempts] == ["failed", "connected"]
    assert attempts[0]["error_code"] == "10002007"
    assert attempts[0]["retryable"] is True
    assert attempts[0]["cleanup"]["socket_closed"] is True
    assert saved["status"] == "CONNECTED" and saved["connected_attempt"] == 2
    assert "login success!" in capsys.readouterr().out


def test_login_non_timeout_error_is_logged_without_retry(tmp_path, monkeypatch):
    context = SimpleNamespace(default_socket=None, user_id="anonymous")
    monkeypatch.setattr(MODULE.importlib, "import_module",
                        lambda name: SimpleNamespace(conx=context))
    class Client:
        _bs = None
        def __init__(self):
            self.calls = 0
        def _restore_socket_timeout(self):
            pass
        def login(self):
            self.calls += 1
            raise BaostockUnavailable("baostock login failed: 10001003 access denied")
    client = Client()
    delays = []
    log_path = tmp_path / "login-attempts.json"
    with pytest.raises(BaostockUnavailable, match="10001003"):
        MODULE.login_with_single_retry(client, log_path, sleep_fn=delays.append)
    saved = json.loads(log_path.read_text(encoding="utf-8"))
    assert client.calls == 1 and delays == []
    assert saved["status"] == "FAILED"
    assert saved["attempts"][0]["retryable"] is False


def test_login_timeout_stops_after_the_single_retry(tmp_path, monkeypatch):
    context = SimpleNamespace(default_socket=None, user_id="anonymous")
    monkeypatch.setattr(MODULE.importlib, "import_module",
                        lambda name: SimpleNamespace(conx=context))
    class Client:
        _bs = None
        def __init__(self):
            self.calls = 0
        def _restore_socket_timeout(self):
            pass
        def login(self):
            self.calls += 1
            print("timed out")
            raise BaostockUnavailable("baostock login failed: 10002007 网络接收错误。")
    client = Client()
    delays = []
    log_path = tmp_path / "login-attempts.json"
    with pytest.raises(RuntimeError, match="after one retry"):
        MODULE.login_with_single_retry(client, log_path, sleep_fn=delays.append)
    saved = json.loads(log_path.read_text(encoding="utf-8"))
    assert client.calls == 2 and delays == [5]
    assert len(saved["attempts"]) == 2
    assert saved["status"] == "FAILED"
