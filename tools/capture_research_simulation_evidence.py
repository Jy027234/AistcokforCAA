"""Capture a dated, receipt-backed evidence pack for simulation research.

This writes a sidecar under data/s2-structured and never publishes research or
simulation inputs. Staged research provenance is retained at its original
paths and dates; target-day BaoStock identity, calendar and daily bars are newly
captured and receipted.
"""
from __future__ import annotations

import hashlib
import importlib
import io
import json
import sqlite3
import sys
import argparse
import re
import socket
import time
from contextlib import contextmanager, redirect_stdout
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.baostock import BaostockClient  # noqa: E402
from aquant.artifact_paths import resolve_artifact_path  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402
from aquant.domain.simulation.board_rules import BOARD_RULES  # noqa: E402

ASOF = date.today().isoformat()
OUT = None
RESEARCH = ROOT / "data/s2-structured/research-candidates.json"
LOGIN_RETRY_DELAY_SECONDS = 5


def calendar_range(target: date) -> tuple[str, str]:
    return f"{target.year}-01-01", f"{target.year}-12-31"


def trading_window(calendar_rows: list[dict], target: str, count: int = 120) -> tuple[list[str], str]:
    days = sorted({row["calendar_date"] for row in calendar_rows
                   if row.get("is_trading_day") == "1" and row.get("calendar_date")
                   and row["calendar_date"] <= target})
    if len(days) < count:
        raise ValueError(f"BaoStock calendar returned only {len(days)} days through {target}")
    return days, days[-count]


def checkpoint_for_resume(cp: dict, *, target: str, research_path: Path,
                          research_hash: str, selected_ids: list[str],
                          retained_path: Path | None = None,
                          retained_hash: str | None = None) -> None:
    expected = {"target_day": target, "research_path": str(research_path.resolve()),
                "research_hash": research_hash, "selected_instrument_ids": selected_ids,
                "retained_path": str(retained_path.resolve()) if retained_path else None,
                "retained_hash": retained_hash}
    if cp.get("capture_binding") != expected:
        raise ValueError("resume checkpoint is bound to a different target day, research input, or selected pool")


def target_bar_is_complete(bars: list[dict], target: str) -> bool:
    bar = next((row for row in bars if row.get("trading_day") == target), None)
    return bool(bar and bar.get("tradestatus") in {"0", "1"}
                and bar.get("isST") in {"0", "1"}
                and (bar["tradestatus"] == "0" or all(bar.get(key) is not None for key in
                     ("open_cents", "high_cents", "low_cents", "close_cents"))))


def checkpoint_item_is_complete(item: dict | None, target: str) -> bool:
    if not item or not target_bar_is_complete(item.get("bars", []), target):
        return False
    eligibility = item.get("simulation_eligibility", {})
    identity = item.get("identity", {})
    return bool(eligibility.get("status") == "VERIFIED"
                and eligibility.get("as_of_date") == target
                and item.get("listed_on") and item["listed_on"] <= target
                and item.get("listed_on_source")
                and identity.get("official_basic_row")
                and identity.get("active_in_all_stock_on_target_day"))


def listed_on_source_from_basic_receipt(basic_receipt: dict) -> dict:
    """Bind the listing date to the basic response selected for this item."""
    return {"source_id": "baostock-socket", "receipt_id": basic_receipt["receipt_id"],
            "content_hash": basic_receipt["content_hash"],
            "available_at": basic_receipt["first_seen_at"]}


def validate_retained_document(document: dict, target: str) -> list[str]:
    if (document.get("schema") != "aquant.simulation_retained_instruments.v1"
            or document.get("trading_day") != target):
        raise ValueError("retained-instruments document has the wrong schema or trading day")
    ids = document.get("instrument_ids")
    reasons = document.get("reasons")
    if (not isinstance(ids, list) or ids != sorted(set(ids))
            or not isinstance(reasons, dict) or set(reasons) != set(ids)
            or not isinstance(document.get("skipped_instruments"), list)):
        raise ValueError("retained-instruments ids must be sorted, unique, and match reasons")
    pattern = re.compile(r"(?:SH\.60\d{4}|SZ\.00\d{4})\Z")
    for iid in ids:
        if not isinstance(iid, str) or not pattern.fullmatch(iid):
            raise ValueError(f"retained instrument is outside exact MAIN identifier scope: {iid}")
        if not isinstance(reasons[iid], list) or not reasons[iid]:
            raise ValueError(f"retained instrument has no retention reason: {iid}")
        for reason in reasons[iid]:
            kind = reason.get("kind") if isinstance(reason, dict) else None
            required_by_kind = {
                "PENDING_DRAFT": {"draft_id", "portfolio_id", "expected_execution_day", "research_content_hash"},
                "ACTIVE_HOLDING": {"portfolio_id", "quantity_remaining"},
                "FORWARD_COMPARISON_COMMON_POOL": {"series_id", "first_trading_day", "spec_content_hash"},
            }
            required = required_by_kind.get(kind)
            if required is None or not required.issubset(reason):
                raise ValueError(f"retained instrument has an invalid reason record: {iid}")
    return ids


def select_candidates(research: dict, *, limit: int, retained_ids: list[str]) -> list[dict]:
    candidates = research.get("candidates", [])
    ranked = [row for row in candidates if row.get("status") == "RANKED" and row.get("board") == "MAIN"]
    selected = list(ranked[:limit] if limit else ranked)
    selected_ids = {row.get("instrumentId") for row in selected}
    by_id: dict[str, dict] = {}
    for row in candidates:
        iid = row.get("instrumentId")
        if iid in retained_ids:
            if iid in by_id:
                raise ValueError(f"research candidates contain duplicate retained identity: {iid}")
            by_id[iid] = row
    for iid in retained_ids:
        row = by_id.get(iid)
        exchange, _, code = iid.partition(".")
        expected_exchange = "SSE" if exchange == "SH" else "SZSE"
        if (row is None or row.get("board") != "MAIN" or row.get("exchange") != expected_exchange
                or row.get("code") != code or not row.get("industry")):
            raise ValueError(f"retained instrument lacks exact MAIN identity/source fields in research: {iid}")
        if iid not in selected_ids:
            selected.append(row)
            selected_ids.add(iid)
    return selected


def research_source_inputs(research: dict, research_path: Path,
                           retained_path: Path | None = None) -> dict:
    sources = {"research_candidates": {"path": str(research_path.resolve()),
        "sha256": digest(research_path), "snapshot_id": research.get("snapshotId"),
        "data_as_of": research.get("dataAsOf")}}
    for source in research.get("provenance", []):
        path = resolve_artifact_path(source["path"]).resolve()
        role = source.get("role", "source")
        if not path.is_file():
            raise FileNotFoundError(f"research provenance source is missing: {path}")
        actual_hash = digest(path)
        if source.get("contentHash") and source["contentHash"] != actual_hash:
            raise ValueError(f"research provenance hash mismatch: {path}")
        sources[f"provenance:{role}"] = {"path": str(path), "sha256": actual_hash,
            "declared_sha256": source.get("contentHash"), "role": role}
    if retained_path is not None:
        sources["retained_instruments"] = {"path": str(retained_path.resolve()),
            "sha256": digest(retained_path)}
    return sources


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, obj: object) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _login_error_code(exc: Exception) -> str | None:
    match = re.search(r"\b(\d{8})\b", str(exc))
    return match.group(1) if match else None


def _retryable_login_error(exc: Exception, diagnostics: str) -> bool:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    # BaoStock's login API collapses a recv socket timeout into 10002007.
    # Retry only when its socket helper also reported the timeout itself.
    return (_login_error_code(exc) == "10002007"
            and re.search(r"timed\s*out|timeout|超时", diagnostics, re.IGNORECASE) is not None)


def _clear_failed_baostock_login(client: BaostockClient,
                                 *, context_module=None,
                                 original_socket_timeout=None) -> dict:
    """Close a failed login's singleton socket without reading from it again."""
    cleanup = {"socket_closed": False, "context_cleared": False,
               "default_timeout_restored": False}
    client._restore_socket_timeout()
    if socket.getdefaulttimeout() != original_socket_timeout:
        socket.setdefaulttimeout(original_socket_timeout)
        cleanup["default_timeout_restored"] = True
    if context_module is None:
        try:
            context_module = importlib.import_module("baostock.login.loginout").conx
        except (ImportError, AttributeError):
            return cleanup
    stale_socket = getattr(context_module, "default_socket", None)
    if stale_socket is not None:
        try:
            stale_socket.close()
            cleanup["socket_closed"] = True
        except Exception as exc:
            cleanup["socket_close_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        finally:
            context_module.default_socket = None
            cleanup["context_cleared"] = True
    if hasattr(context_module, "user_id"):
        delattr(context_module, "user_id")
        cleanup["context_cleared"] = True
    return cleanup


def login_with_single_retry(client: BaostockClient, attempts_path: Path, *,
                            sleep_fn=time.sleep,
                            delay_seconds: int = LOGIN_RETRY_DELAY_SECONDS) -> list[dict]:
    """Log in once, retrying one confirmed socket timeout after a fixed pause."""
    attempts: list[dict] = []
    original_socket_timeout = socket.getdefaulttimeout()
    client._capture_original_socket_timeout = original_socket_timeout
    document = {"schema": "aquant.baostock_login_attempts.v1", "provider": "baostock",
                "retry_policy": {"max_attempts": 2, "retryable": "login socket timeout only",
                                 "delay_seconds": delay_seconds},
                "status": "IN_PROGRESS", "attempts": attempts}
    for attempt_number in (1, 2):
        started_at = now()
        transcript = io.StringIO()
        try:
            with redirect_stdout(transcript):
                client.login()
            diagnostic = transcript.getvalue()
            if diagnostic:
                print(diagnostic, end="", flush=True)
            attempts.append({"attempt": attempt_number, "started_at": started_at,
                             "finished_at": now(), "status": "connected",
                             "diagnostic": diagnostic[-1000:]})
            document["status"] = "CONNECTED"
            document["connected_attempt"] = attempt_number
            save(attempts_path, document)
            return attempts
        except Exception as exc:
            diagnostic = transcript.getvalue()
            if diagnostic:
                print(diagnostic, end="", flush=True)
            retryable = _retryable_login_error(exc, diagnostic)
            cleanup = _clear_failed_baostock_login(
                client, original_socket_timeout=original_socket_timeout)
            entry = {"attempt": attempt_number, "started_at": started_at,
                     "finished_at": now(), "status": "failed",
                     "error_type": type(exc).__name__, "error": str(exc)[:500],
                     "error_code": _login_error_code(exc),
                     "diagnostic": diagnostic[-1000:], "retryable": retryable,
                     "cleanup": cleanup}
            if retryable and attempt_number == 1:
                entry["retry_delay_seconds"] = delay_seconds
                attempts.append(entry)
                document["status"] = "RETRY_WAIT"
                document["next_attempt"] = 2
                save(attempts_path, document)
                sleep_fn(delay_seconds)
                continue
            attempts.append(entry)
            document["status"] = "FAILED"
            save(attempts_path, document)
            if retryable:
                raise RuntimeError(
                    f"BaoStock login timed out after one retry; see {attempts_path}") from exc
            raise
    raise RuntimeError(f"BaoStock login did not connect; see {attempts_path}")


@contextmanager
def logged_in_client(client: BaostockClient):
    """Logout and restore the caller's socket timeout after a successful login."""
    original_socket_timeout = getattr(client, "_capture_original_socket_timeout",
                                      socket.getdefaulttimeout())
    try:
        yield client
    finally:
        try:
            client.logout()
        finally:
            if socket.getdefaulttimeout() != original_socket_timeout:
                socket.setdefaulttimeout(original_socket_timeout)


def iid_to_bs(iid: str) -> str:
    exchange, code = iid.split(".", 1)
    return ("sh." if exchange == "SH" else "sz.") + code


def receipt_summary(client: BaostockClient, receipt, url: str, archive: ForwardArchive) -> dict:
    body = (client.root / receipt.stored_path).read_bytes()
    expected = "sha256:" + hashlib.sha256(body).hexdigest()
    if expected != receipt.content_hash:
        raise ValueError(f"BaoStock raw response hash mismatch for {receipt.label}")
    content_hash, _ = archive.store_bytes(body, media_type="application/json")
    if content_hash != expected:
        raise ValueError(f"ForwardArchive hash mismatch for {receipt.label}")
    observed = datetime.now(timezone.utc)
    wrapped = archive.record(source_id="baostock-socket", url=url, outcome="OK",
                             requested_at=receipt.requested_at, responded_at=observed,
                             content_hash=content_hash, byte_size=len(body),
                             detail=f"{receipt.label}; raw={receipt.stored_path}")
    return {"receipt_id": wrapped.receipt_id, "source_id": "baostock-socket",
            "url": url, "requested_at": receipt.requested_at.isoformat(),
            "responded_at": observed.isoformat(), "first_seen_at": wrapped.first_seen_at.isoformat(),
            "content_hash": content_hash, "byte_size": len(body),
            "row_count": receipt.record_count, "raw_path": receipt.stored_path}


def cents(v: object) -> int | None:
    if v in (None, ""):
        return None
    return int((Decimal(str(v)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def fetch_status_bars(client: BaostockClient, archive: ForwardArchive, code: str,
                      start: str, end: str, adjust: str) -> tuple[list[dict], dict]:
    """Capture exact BaoStock daily fields including tradestatus and isST."""
    client._guard()
    label = f"simulation_daily_status:{code}:{start}..{end}:adjust={adjust}"
    rs = client._run(lambda: client._bs.query_history_k_data_plus(
        code, "date,open,high,low,close,preclose,volume,amount,tradestatus,pctChg,isST",
        start_date=start, end_date=end, frequency="d", adjustflag=adjust), label=label)
    _fields, raw = client._drain(rs)
    rec = client._record(label, raw)
    url = ("baostock://www.baostock.com:10030/query_history_k_data_plus?"
           f"code={code}&start={start}&end={end}&adjustflag={adjust}&fields="
           "date,open,high,low,close,preclose,volume,amount,tradestatus,pctChg,isST")
    receipt = receipt_summary(client, rec, url, archive)
    return raw, receipt


def build_captured_bars(raw_rows: list[dict], adjusted_by_day: dict[str, int | None],
                        *, start_day: str, target_day: str, receipt: dict) -> list[dict]:
    bars = []
    for row in raw_rows:
        day = row.get("date")
        if not day or day < start_day or day > target_day:
            continue
        bars.append({
            "trading_day": day, "open_cents": cents(row.get("open")),
            "high_cents": cents(row.get("high")), "low_cents": cents(row.get("low")),
            "close_cents": cents(row.get("close")),
            "prev_close_cents": cents(row.get("preclose")),
            "adjusted_close_cents": adjusted_by_day.get(day),
            "volume_shares": (int(Decimal(row["volume"]).quantize(Decimal("1")))
                               if row.get("volume") not in (None, "") else None),
            "amount_cents": cents(row.get("amount")),
            "tradestatus": row.get("tradestatus"), "isST": row.get("isST"),
            "source_receipt_id": receipt["receipt_id"],
            "source_content_hash": receipt["content_hash"],
            "source_observed_at": receipt["first_seen_at"],
        })
    return bars


def main() -> int:
    global ASOF, OUT, RESEARCH
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=ASOF, help="target market date YYYY-MM-DD")
    ap.add_argument("--output-dir", type=Path, default=None)
    ap.add_argument("--research", type=Path, default=RESEARCH)
    ap.add_argument("--limit", type=int, default=0, help="top N ranked MAIN candidates; 0 means all")
    ap.add_argument("--retain-instruments", type=Path, default=None,
                    help="dated retained instrument set emitted by daily_research_run")
    ap.add_argument("--resume", action="store_true", help="resume completed candidate captures")
    args = ap.parse_args()
    target_date = date.fromisoformat(args.day)
    ASOF = target_date.isoformat()
    OUT = (args.output_dir if args.output_dir and args.output_dir.is_absolute()
           else ROOT / args.output_dir if args.output_dir
           else ROOT / "data/s2-structured" / f"simulation-evidence-{ASOF.replace('-', '')}")
    RESEARCH = args.research if args.research.is_absolute() else ROOT / args.research
    OUT.mkdir(parents=True, exist_ok=True)
    research = json.loads(RESEARCH.read_text(encoding="utf-8"))
    retained_path = (args.retain_instruments if args.retain_instruments is None or args.retain_instruments.is_absolute()
                     else ROOT / args.retain_instruments)
    retained_doc = (json.loads(retained_path.read_text(encoding="utf-8"))
                    if retained_path is not None else None)
    retained_ids = validate_retained_document(retained_doc, ASOF) if retained_doc is not None else []
    selected = select_candidates(research, limit=args.limit, retained_ids=retained_ids)
    ids = {x["instrumentId"] for x in selected}

    con = sqlite3.connect(OUT / "archive.sqlite", timeout=30)
    archive = ForwardArchive(con, OUT / "archive")
    cp_path = OUT / "checkpoint.json"
    selected_ids = [x["instrumentId"] for x in selected]
    research_hash = digest(RESEARCH)
    retained_hash = digest(retained_path) if retained_path is not None else None
    cp = json.loads(cp_path.read_text(encoding="utf-8")) if args.resume and cp_path.exists() else {
        "schema": "aquant.simulation_evidence_checkpoint.v1", "created_at": now(),
        "research_input_hash": research_hash, "instruments": {}, "source_receipts": [],
        "failures": []}
    if args.resume and cp_path.exists():
        checkpoint_for_resume(cp, target=ASOF, research_path=RESEARCH,
                              research_hash=research_hash, selected_ids=selected_ids,
                              retained_path=retained_path, retained_hash=retained_hash)
        cp["failures"] = []
    elif cp_path.exists() and (OUT / "summary.json").exists():
        raise ValueError(f"refusing to overwrite existing evidence package: {OUT}")
    cp["research_input_hash"] = research_hash
    cp["research_input_path"] = str(RESEARCH.resolve())
    cp["capture_market_date"] = ASOF
    cp["capture_binding"] = {"target_day": ASOF, "research_path": str(RESEARCH.resolve()),
        "research_hash": research_hash, "selected_instrument_ids": selected_ids,
        "retained_path": str(retained_path.resolve()) if retained_path else None,
        "retained_hash": retained_hash}
    receipts: list[dict] = cp.setdefault("source_receipts", [])
    identity: dict[str, dict] = {k: v.get("identity", {}) for k, v in cp.get("instruments", {}).items()}
    calendar_rows: list[dict] = []
    allstock_rows: list[dict] = []
    failures: list[dict] = []
    bs = BaostockClient(OUT / "baostock-raw", min_interval_seconds=0.25,
                        query_timeout=35.0)
    login_attempts_path = OUT / "login-attempts.json"
    login_attempts = login_with_single_retry(bs, login_attempts_path)
    with logged_in_client(bs):
        prior_summary = json.loads((OUT / "summary.json").read_text(encoding="utf-8")) if (OUT / "summary.json").exists() else {}
        prior_cal = prior_summary.get("trading_calendar", {})
        year_start, year_end = calendar_range(target_date)
        if (prior_cal.get("requested_range") == [year_start, year_end]
                and prior_cal.get("official_raw_rows") and prior_cal.get("calendar_receipt")):
            calendar_raw = prior_cal["official_raw_rows"]
            trade_calendar_receipt = prior_cal["calendar_receipt"]
            calendar_available_at = prior_cal["calendar_available_at"]
        else:
            bs._guard()
            cal_rs = bs._run(lambda: bs._bs.query_trade_dates(
                start_date=year_start, end_date=year_end),
                label=f"simulation_trade_calendar:{target_date.year}")
            _cal_fields, calendar_raw = bs._drain(cal_rs)
            cal_receipt_raw = bs._record(f"simulation_trade_calendar:{target_date.year}", calendar_raw)
            trade_calendar_receipt = receipt_summary(
                bs, cal_receipt_raw,
                f"baostock://www.baostock.com:10030/query_trade_dates?start_date={year_start}&end_date={year_end}",
                archive)
            receipts.append(trade_calendar_receipt)
            calendar_available_at = trade_calendar_receipt["first_seen_at"]
        official_trading_dates = [x.get("calendar_date") for x in calendar_raw
                                  if x.get("is_trading_day") == "1" and x.get("calendar_date")]
        # Returned index bars are actual exchange trading dates, not weekday estimates.
        index_rows, rec = bs.daily_bars("sh.000001", start=year_start, end=ASOF, adjust="3")
        calendar_receipt = receipt_summary(bs, rec,
            f"baostock://www.baostock.com:10030/query_history_k_data_plus?code=sh.000001&start={year_start}&end={ASOF}&adjustflag=3",
            archive)
        receipts.append(calendar_receipt)
        calendar_rows = [{"trading_day": x["trading_day"], "index_close_cents": x["close_cents"]}
                         for x in index_rows]
        trading_days, start_day = trading_window(calendar_raw, ASOF)

        stocks, rec = bs.all_stock(ASOF)
        allstock_receipt = receipt_summary(bs, rec,
            f"baostock://www.baostock.com:10030/query_all_stock?day={ASOF}", archive)
        receipts.append(allstock_receipt)
        present_codes = {row.get("code") for row in stocks}
        allstock_rows = [row for row in stocks if row.get("code") in {iid_to_bs(i) for i in ids}]

        for n, candidate in enumerate(selected, 1):
            iid = candidate["instrumentId"]
            old_item = cp["instruments"].get(iid)
            old_bars = list(old_item.get("bars", [])) if old_item else []
            if args.resume and checkpoint_item_is_complete(old_item, ASOF):
                continue
            code = iid_to_bs(iid)
            try:
                if old_item and old_item.get("listed_on"):
                    hit = (old_item.get("identity") or {}).get("official_basic_row")
                    basic_rcpt = {"receipt_id": old_item["listed_on_source"]["receipt_id"],
                                  "content_hash": old_item["listed_on_source"]["content_hash"],
                                  "first_seen_at": old_item["listed_on_source"]["available_at"]}
                    identity[iid] = old_item.get("identity", {})
                    identity[iid] = {**identity[iid],
                        "active_in_all_stock_on_target_day": code in present_codes,
                        "all_stock_row": next((x for x in allstock_rows if x.get("code") == code), None),
                        "all_stock_receipt_id": allstock_receipt["receipt_id"]}
                else:
                    basic, rec = bs.stock_basic(code)
                    basic_rcpt = receipt_summary(bs, rec,
                        f"baostock://www.baostock.com:10030/query_stock_basic?code={code}", archive)
                    receipts.append(basic_rcpt)
                    hit = next((x for x in basic if x.get("code") == code), None)
                    identity[iid] = {
                        "instrument_id": iid, "requested_code": code,
                        "official_basic_row": hit,
                        "active_in_all_stock_on_target_day": code in present_codes,
                        "all_stock_row": next((x for x in allstock_rows if x.get("code") == code), None),
                        "basic_receipt_id": basic_rcpt["receipt_id"],
                        "available_at": basic_rcpt["first_seen_at"],
                    }
                    if hit is None:
                        cp["failures"].append({"instrument_id": iid, "stage": "stock_basic", "detail": "no exact code row"})
                # A single history receipt must cover every emitted bar because admission
                # reconstructs the full window from that response. Incomplete checkpoint
                # items are therefore recaptured over the complete window and replaced.
                raw, raw_rcpt = fetch_status_bars(bs, archive, code, start_day, ASOF, "3")
                adj, adj_rcpt = fetch_status_bars(bs, archive, code, start_day, ASOF, "2")
                receipts.extend([raw_rcpt, adj_rcpt])
                adjusted_by_day = {x.get("date"): cents(x.get("close")) for x in adj}
                # Replace all old item bars: its previous single receipt cannot
                # authenticate an appended window whose old rows came from another response.
                bars = build_captured_bars(raw, adjusted_by_day, start_day=start_day,
                                           target_day=ASOF, receipt=raw_rcpt)
                by_day = {x["trading_day"]: x for x in bars}
                bars = [by_day[k] for k in sorted(by_day)]
                if len(bars) > 120:
                    bars = bars[-120:]
                bars.sort(key=lambda x: x["trading_day"])
                last = next((x for x in reversed(bars) if x["trading_day"] == ASOF), None)
                listed_on = (hit or {}).get("ipoDate")
                status_verified = bool(last and last.get("tradestatus") in {"0", "1"}
                                       and last.get("isST") in {"0", "1"}
                                       and listed_on and listed_on <= ASOF
                                       and code in present_codes)
                current_status_sources = [
                    {"source_id": "baostock-socket", "receipt_id": raw_rcpt["receipt_id"],
                     "content_hash": raw_rcpt["content_hash"]},
                    {"source_id": "baostock-socket", "receipt_id": basic_rcpt["receipt_id"],
                     "content_hash": basic_rcpt["content_hash"]},
                    {"source_id": "baostock-socket", "receipt_id": allstock_receipt["receipt_id"],
                     "content_hash": allstock_receipt["content_hash"]},
                ]
                if last and status_verified and target_bar_is_complete(bars, ASOF):
                    eligibility = {
                        "status": "VERIFIED", "as_of_date": ASOF,
                        "available_at": max(raw_rcpt["first_seen_at"], basic_rcpt["first_seen_at"]),
                        "observed_at": raw_rcpt["first_seen_at"],
                        "is_st": last["isST"] == "1", "is_suspended": last["tradestatus"] == "0",
                        "tradestatus": last["tradestatus"], "sources": current_status_sources,
                    }
                else:
                    eligibility = {"status": "UNKNOWN", "as_of_date": ASOF,
                                   "available_at": raw_rcpt["first_seen_at"],
                                   "is_st": None, "is_suspended": None,
                                   "sources": current_status_sources}
                cp["instruments"][iid] = {
                    "rank": n, "instrument_id": iid, "code": candidate["code"],
                    "research_display_name": candidate.get("displayName"),
                    "board": candidate.get("board"), "exchange": candidate.get("exchange"),
                    "position": candidate.get("position"), "signal_rank": candidate.get("signalRank"),
                    "identity": identity[iid], "listed_on": listed_on,
                    "listed_on_source": listed_on_source_from_basic_receipt(basic_rcpt),
                    "simulation_eligibility": eligibility,
                    "bars": bars,
                    "history_receipt": raw_rcpt, "adjusted_history_receipt": adj_rcpt,
                    "history_window": {"start": bars[0]["trading_day"] if bars else None,
                                       "end": bars[-1]["trading_day"] if bars else None,
                                       "bar_count": len(bars)},
                }
                if not target_bar_is_complete(bars, ASOF):
                    cp["failures"].append({"instrument_id": iid, "stage": "target_day_bar",
                        "detail": f"missing complete target-day OHLC/tradestatus/isST for {ASOF}"})
                cp["source_receipts"] = receipts
            except Exception as exc:
                cp["failures"].append({"instrument_id": iid, "stage": "history",
                                       "detail": f"{type(exc).__name__}: {str(exc)[:200]}"})
            if n % 10 == 0 or n == len(selected):
                cp["updated_at"] = now()
                save(cp_path, cp)
                print(json.dumps({"processed": n, "captured": len(cp["instruments"]),
                                  "failures": len(cp["failures"])}, ensure_ascii=False), flush=True)

        # Explicitly test the mistaken SH alias from the user's note once.
        if "alias_probe" not in cp:
            try:
                alias_rows, rec = bs.stock_basic("sh.002039")
                receipt = receipt_summary(bs, rec,
                    "baostock://www.baostock.com:10030/query_stock_basic?code=sh.002039", archive)
                receipts.append(receipt)
                cp["alias_probe"] = {"requested_code": "sh.002039", "official_basic_row": next(
                    (x for x in alias_rows if x.get("code") == "sh.002039"), None),
                    "interpretation": "Alias probe only; ranked identifier remains SZ.002039"}
            except Exception as exc:
                failures.append({"instrument_id": "SH.002039", "stage": "alias_probe",
                                 "detail": f"{type(exc).__name__}: {str(exc)[:200]}"})
    con.close()

    return finalize_capture_package(cp=cp, research=research, selected=selected,
        retained_ids=retained_ids, retained_path=retained_path, research_path=RESEARCH,
        output_dir=OUT, target_day=ASOF, calendar_raw=calendar_raw,
        calendar_rows=calendar_rows, trade_calendar_receipt=trade_calendar_receipt,
        calendar_receipt=calendar_receipt, calendar_available_at=calendar_available_at,
        allstock_rows=allstock_rows, allstock_receipt=allstock_receipt,
        source_receipts=receipts, login_attempts=login_attempts, limit=args.limit, failures=failures)


def finalize_capture_package(*, cp: dict, research: dict, selected: list[dict],
                             retained_ids: list[str], retained_path: Path | None,
                             research_path: Path, output_dir: Path, target_day: str,
                             calendar_raw: list[dict], calendar_rows: list[dict],
                             trade_calendar_receipt: dict, calendar_receipt: dict,
                             calendar_available_at: str, allstock_rows: list[dict],
                             allstock_receipt: dict, source_receipts: list[dict],
                             login_attempts: list[dict], limit: int,
                             failures: list[dict] | None = None,
                             preserve_existing: bool = False) -> int:
    """Finish the ordinary package from captured inputs, without any provider call.

    The strict recovery mode preserves the original checkpoint/cache/pool bytes;
    it may only create the previously missing final summary and hash receipt.
    Both modes use the same original packaging and rule derivation below.
    """
    from copy import deepcopy
    cp = deepcopy(cp)
    ASOF, OUT, RESEARCH = target_day, Path(output_dir), Path(research_path)
    target_date = date.fromisoformat(ASOF)
    year_start, year_end = calendar_range(target_date)
    _trading_days, start_day = trading_window(calendar_raw, ASOF)
    cp_path = OUT / "checkpoint.json"
    login_attempts_path = OUT / "login-attempts.json"
    receipts = deepcopy(source_receipts)
    failures = list(failures or ())
    identity = {key: value.get("identity", {}) for key, value in cp.get("instruments", {}).items()}
    selected_ids = [item["instrumentId"] for item in selected]
    research_hash = cp["capture_binding"]["research_hash"]
    retained_hash = cp["capture_binding"].get("retained_hash")
    official_trading_dates = [row["calendar_date"] for row in calendar_raw if row.get("is_trading_day") == "1"]
    if preserve_existing and any((OUT / name).exists() for name in ("summary.json", "hash-receipt.json")):
        raise ValueError("recovery refuses to overwrite an existing summary or hash receipt")
    instruments = [cp["instruments"][x["instrumentId"]] for x in selected
                   if x["instrumentId"] in cp["instruments"]]
    incomplete = [iid for iid in selected_ids if iid not in cp["instruments"] or
                  not target_bar_is_complete(cp["instruments"][iid].get("bars", []), ASOF) or
                  cp["instruments"][iid].get("simulation_eligibility", {}).get("status") != "VERIFIED"]
    if incomplete or cp.get("failures"):
        if not preserve_existing:
            save(cp_path, cp)
        raise SystemExit(f"evidence capture incomplete; refusing to publish package: "
                         f"missing_target_bars={incomplete[:10]}, failures={len(cp.get('failures', []))}")
    if digest(RESEARCH) != research_hash or (retained_path is not None and digest(retained_path) != retained_hash):
        raise SystemExit("research or retained-instruments input changed during capture; refusing to publish package")
    # The simulator uses the repository's effective exchange/board rules.
    # Prices are rounded to fen, and unknown/ST dates are left uncomputed.
    for item in instruments:
        main_rule = next(r for r in BOARD_RULES if r.exchange == item["exchange"] and r.board == item["board"]
                         and r.effective_from.isoformat() <= ASOF
                         and (r.effective_to is None or ASOF < r.effective_to.isoformat()))
        for bar in item["bars"]:
            previous = bar.get("prev_close_cents")
            if previous is None or bar.get("isST") != "0":
                bar.update({"limit_up_cents": None, "limit_down_cents": None,
                            "board_limit_up": None, "limit_rule_source": None})
                continue
            pct = main_rule.price_limit_pct
            bar["limit_up_cents"] = int((Decimal(previous) * (1 + pct / 100)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP))
            bar["limit_down_cents"] = int((Decimal(previous) * (1 - pct / 100)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP))
            # ST candidates remain explicitly excluded; these values use the
            # non-ST MAIN rule selected by the verified target-day isST field.
            bar["board_limit_up"] = (bar.get("open_cents") == bar["limit_up_cents"]
                                     and bar.get("isST") == "0")
            bar["limit_rule_source"] = {
                "registry": "aquant.domain.simulation.board_rules.BOARD_RULES",
                "file": "src/aquant/domain/simulation/board_rules.py",
                "exchange": main_rule.exchange, "board": main_rule.board,
                "price_limit_pct": str(pct),
                "effective_from": main_rule.effective_from.isoformat(),
                "effective_to": main_rule.effective_to.isoformat() if main_rule.effective_to else None,
                "derivation": "round(previous close × (1 ± rate) to nearest fen; simulator rule registry",
            }
    cp["source_receipts"] = receipts
    if not preserve_existing:
        save(cp_path, cp)

    # Direct input contract for publish_universe_snapshot. Retain the existing
    # cache keys and attach the newly verified identity/status evidence.
    cache_bars = {}
    pool_instruments = []
    for candidate in selected:
        iid = candidate["instrumentId"]
        item = cp["instruments"].get(iid)
        if not item:
            continue
        ident = item.get("identity", {})
        official_name = ((ident.get("all_stock_row") or {}).get("code_name")
                         or (ident.get("official_basic_row") or {}).get("code_name")
                         or candidate.get("displayName"))
        cache_bars[iid] = {
            "exchange": candidate.get("exchange"), "board": candidate.get("board"),
            "rows": item["bars"],
            "prev_close_before_window": item["bars"][0].get("prev_close_cents") if item["bars"] else None,
            "prev_close_attempted": True,
            "window": f"{start_day}..{ASOF}", "rows_format": 2,
            "listed_on": item.get("listed_on"),
            "simulation_eligibility": item["simulation_eligibility"],
            "identity_receipt": item.get("listed_on_source"),
        }
        pool_instruments.append({
            "instrument_id": iid,
            "code": iid_to_bs(iid).replace(".", ""),
            "exchange": candidate.get("exchange"), "board": candidate.get("board"),
            "name": official_name, "short_name": official_name,
            "listed_on": item.get("listed_on"), "industry": candidate.get("industry"),
            "research_rank": candidate.get("position"),
        })
    verified_calendar = sorted({x for x in official_trading_dates if x <= ASOF})
    next_trading_day = next((x for x in official_trading_dates if x > ASOF), None)
    previous_trading_day = next((x for x in reversed(verified_calendar) if x < ASOF), None)
    cache_doc = {
        "schema": "aquant.simulation_universe_cache.v1", "created_at": now(), "updated_at": now(),
        "window": {"first_day": start_day, "last_day": ASOF, "trading_days": 120},
        "calendar_source": f"baostock:query_trade_dates:{target_date.year}",
        "trading_calendar": verified_calendar,
        "calendar_available_at": calendar_available_at,
        "calendar_receipt": trade_calendar_receipt,
        "bars": cache_bars,
    }
    if preserve_existing and (OUT / "cache-120-main.json").exists():
        previous_cache = json.loads((OUT / "cache-120-main.json").read_text(encoding="utf-8"))
        for timestamp_key in ("created_at", "updated_at"):
            cache_doc[timestamp_key] = previous_cache.get(timestamp_key)
        if previous_cache != cache_doc:
            raise ValueError("existing cache differs from the complete frozen capture checkpoint")
        cache_doc = previous_cache
    else:
        save(OUT / "cache-120-main.json", cache_doc)
    pool_doc = {
        "pool_id": f"simulation-research-top{len(pool_instruments)}-main-{ASOF.replace('-', '')}",
        "as_of": ASOF,
        "source": f"{RESEARCH.name} + BaoStock verified security identity",
        "selection_rule": "top N RANKED research candidates with board MAIN, union retained MAIN instruments; simulation scope only",
        "classification_version": "CSRC-2012",
        "window": cache_doc["window"], "calendar_available_at": calendar_available_at,
        "instruments": pool_instruments,
    }
    if preserve_existing and (OUT / "pool-120-main.yaml").exists():
        if json.loads((OUT / "pool-120-main.yaml").read_text(encoding="utf-8")) != pool_doc:
            raise ValueError("existing pool differs from the complete frozen capture checkpoint")
    else:
        save(OUT / "pool-120-main.yaml", pool_doc)

    output = {
        "schema": "aquant.simulation_research_evidence.v1",
        "artifact_created_at": now(), "evidence_market_date": ASOF,
        "requested_future_execution_date": next_trading_day,
        "future_execution_status": "NOT_YET_OCCURRED_AS_OF_CAPTURE; no execution or fill data exists",
        "source_inputs": research_source_inputs(research, RESEARCH, retained_path),
        "selection": {"rule": f"top {limit or 'all'} ranked MAIN candidates union retained MAIN instruments",
                      "count": len(instruments), "includes_top": [x["instrumentId"] for x in selected
                          if x.get("status") == "RANKED" and x.get("board") == "MAIN"][:3],
                      "retained_instrument_count": len(retained_ids),
                      "retained_instrument_ids": retained_ids,
                      "status": "all rows have VERIFIED identity/status; ST and halted rows remain context and are excluded from new buys"},
        "prior_day_decision_replay": {
            "decision_date": previous_trading_day, "execution_date": ASOF,
            "status": "UNAVAILABLE_FROM_EXISTING_ARCHIVES",
            "reason": "No separate decision snapshot for the prior trading day was found; this capture does not backfill today's ranking to an earlier decision date.",
        },
        "trading_calendar": {"source": "BaoStock query_trade_dates, cross-checked against sh.000001 index bars",
                             "requested_range": [year_start, year_end],
                             "official_raw_rows": calendar_raw,
                             "trading_dates_through_target_date": verified_calendar,
                             "index_dates_through_target_date": [x["trading_day"] for x in calendar_rows if x["trading_day"] <= ASOF],
                             "next_trading_day": next_trading_day,
                             "next_trading_day_is_verified_calendar_entry": next_trading_day in official_trading_dates if next_trading_day else False,
                             "calendar_available_at": calendar_available_at,
                             "calendar_receipt": trade_calendar_receipt,
                             "index_receipt": calendar_receipt},
        "official_security_list": {"source": f"BaoStock all_stock({ASOF})",
                                   "selected_rows": allstock_rows,
                                   "receipt": allstock_receipt},
        "instruments": instruments,
        "source_receipts": receipts,
        "baostock_login": {"attempt_log": str(login_attempts_path.resolve()),
                           "attempt_log_hash": digest(login_attempts_path),
                           "attempts": login_attempts},
        "failures": cp.get("failures", []) + failures,
        "coverage_and_gaps": {
            "ranked_main_board_selected": len(instruments),
            "official_basic_exact_rows": sum(bool(identity.get(x["instrument_id"], {}).get("official_basic_row")) for x in instruments),
            "verified_simulation_eligibility": sum(x.get("simulation_eligibility", {}).get("status") == "VERIFIED" for x in instruments),
            "unknown_simulation_eligibility": sum(x.get("simulation_eligibility", {}).get("status") != "VERIFIED" for x in instruments),
            "authoritative_historical_st_state": sum(x.get("simulation_eligibility", {}).get("is_st") is not None for x in instruments),
            "authoritative_historical_suspension_status": sum(x.get("simulation_eligibility", {}).get("is_suspended") is not None for x in instruments),
            "official_daily_limit_price_parameters": 0,
            "derived_daily_limit_parameters": sum(any(b.get("trading_day") == ASOF and b.get("limit_rule_source") for b in x["bars"]) for x in instruments),
            "target_execution_rows": 0,
            "notes": ["BaoStock historical bars include raw tradestatus/isST, OHLC, preclose, share volume, amount and exact response receipt.",
                      "Simulation status is VERIFIED only when the target-day row, tradestatus/isST, ipoDate and all_stock identity all resolve.",
                      "BaoStock does not return per-security daily limit prices; those values are derived from verified preclose/isST plus the effective simulator exchange rule.",
                      f"The next trading date is {next_trading_day}, known from the annual calendar; its prices and execution do not exist until that date occurs."],
        },
        "ready_to_publish_files": {"cache": str(OUT / "cache-120-main.json"),
                                    "pool": str(OUT / "pool-120-main.yaml"),
                                    "contract_note": "cache preserves bars/window/trading_calendar and adds listed_on, per-day tradestatus/isST and simulation_eligibility provenance"},
    }
    save(OUT / "summary.json", output)
    artifact_hash = digest(OUT / "summary.json")
    save(OUT / "hash-receipt.json", {"artifact_path": str(OUT / "summary.json"),
         "content_hash": artifact_hash, "archived_at": now(), "source_receipt_count": len(receipts)})
    print(json.dumps({"output": str(OUT / "summary.json"), "instrument_count": len(instruments),
                      "calendar": [x["trading_day"] for x in calendar_rows],
                      "receipt_count": len(receipts), "failures": len(failures),
                      "artifact_hash": artifact_hash}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
