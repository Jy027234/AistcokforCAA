"""Capture exact, preselected CNINFO revision PDFs into the forward archive.

This tool only downloads the PDF URLs carried by a candidate manifest after
checking each URL against the selected announcement in the bound dataset. It
does not parse or promote PDF contents. Successful, verified items are
checkpointed; failed items remain eligible for the next run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.cninfo import (  # noqa: E402
    CircuitBreaker, CninfoClient, RateLimiter, default_policy,
)
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.forward_archive import ForwardArchive  # noqa: E402

ARCHIVE_ROOT = ROOT / "deploy" / "agentctl-q0" / "forward-archive"
TARGETS_PATH = ROOT / "tmp" / "revision-trial-targets-20261009.json"
BOUND_PATH = ROOT / "data" / "s2-structured" / "bound-v5-20261007.json"
CHECKPOINT_PATH = ROOT / "data" / "s2-structured" / "revision-pdf-capture-20261009.checkpoint.jsonl"
OUTPUT_PATH = ROOT / "data" / "s2-structured" / "revision-pdf-capture-20261009.json"
MAX_BYTES = 64 * 1024 * 1024
BEIJING = timezone(timedelta(hours=8))


def now_beijing() -> str:
    return datetime.now(BEIJING).isoformat()


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _load_targets(target_path: Path, bound_path: Path, limit: int) -> list[dict]:
    manifest = json.loads(target_path.read_text(encoding="utf-8"))
    bound = json.loads(bound_path.read_text(encoding="utf-8"))
    records = {
        (str(row["instrument_id"]).zfill(6), str(row["period_end"])[:10]): row
        for row in bound["records"]
    }
    selected: list[dict] = []
    candidates = manifest.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("target manifest must contain a candidates array")
    for candidate in candidates[:limit]:
        code = str(candidate["instrument_id"]).zfill(6)
        pairs = candidate.get("pairs")
        if candidate.get("needed_count") != 1 or not isinstance(pairs, list) or len(pairs) != 1:
            raise ValueError(f"expected exactly one required PDF for {code}")
        period, announcement_id, url = pairs[0]
        period = str(period)[:10]
        announcement_id = str(announcement_id)
        url = str(url)
        record = records.get((code, period))
        announcement = (record or {}).get("announcement") or {}
        title = str(announcement.get("title") or "")
        if (record is None
                or record.get("status") != "REVISION_REVIEW_REQUIRED"
                or str(announcement.get("announcement_id") or "") != announcement_id
                or str(announcement.get("url") or "") != url
                or announcement.get("report_kind") not in {"annual", "semiannual", "quarterly"}
                or "报告" not in title
                or "摘要" in title
                or not url.startswith("https://static.cninfo.com.cn/finalpage/")
                or not url.lower().endswith(".pdf")):
            raise ValueError(f"candidate does not exactly match a selected full-report PDF: {code} {period}")
        selected.append({
            "instrument_id": code,
            "period_end": period,
            "announcement_id": announcement_id,
            "report_kind": announcement["report_kind"],
            "title": title,
            "url": url,
        })
    if len(selected) != limit:
        raise ValueError(f"requested {limit} targets but manifest supplied {len(selected)}")
    return selected


def _receipt_for_url(con, archive: ForwardArchive, url: str):
    rows = con.execute(
        "SELECT receipt_id,source_id,url,requested_at,responded_at,first_seen_at,"
        "outcome,http_status,content_hash,byte_size,detail FROM fetch_receipt "
        "WHERE source_id='cninfo' AND url=? AND outcome='OK' AND http_status=200 "
        "AND content_hash IS NOT NULL ORDER BY first_seen_at DESC",
        (url,),
    ).fetchall()
    for row in rows:
        if archive.verify(row["content_hash"]):
            return dict(row)
    return None


def _checkpoint_success_is_valid(item: dict, target: dict, con,
                                 archive: ForwardArchive) -> bool:
    if (item.get("status") != "ok"
            or item.get("url") != target["url"]
            or not item.get("actual_receipt_id")
            or not item.get("content_hash")):
        return False
    row = con.execute(
        "SELECT receipt_id,url,outcome,http_status,content_hash,byte_size,requested_at,first_seen_at "
        "FROM fetch_receipt WHERE receipt_id=? AND source_id='cninfo'",
        (item["actual_receipt_id"],),
    ).fetchone()
    return bool(
        row
        and row["url"] == target["url"]
        and row["outcome"] == "OK"
        and row["http_status"] == 200
        and row["content_hash"] == item["content_hash"]
        and row["byte_size"] == item.get("size_bytes")
        and row["requested_at"] == item.get("requested_at")
        and row["first_seen_at"] == item.get("first_seen_at")
        and archive.verify(row["content_hash"])
    )


def _read_checkpoint(path: Path) -> dict[tuple[str, str], dict]:
    latest: dict[tuple[str, str], dict] = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    item = json.loads(line)
                    key = (str(item["instrument_id"]).zfill(6), str(item["period_end"])[:10])
                    latest[key] = item
    return latest


def run_capture(targets: list[dict], checkpoint_path: Path, output_path: Path,
                archive_root: Path, *, archive_factory=ForwardArchive,
                client_factory=CninfoClient) -> dict:
    checkpointed = _read_checkpoint(checkpoint_path)
    target_keys = {(item["instrument_id"], item["period_end"]) for item in targets}
    checkpointed = {key: value for key, value in checkpointed.items() if key in target_keys}
    started = time.monotonic()
    requests = 0
    stopped_on_breaker = False
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    with closing(connect(archive_root / "meta.sqlite")) as con:
        archive = archive_factory(con, archive_root)
        client = client_factory(
            archive,
            policy=default_policy(),
            limiter=RateLimiter(min_interval_seconds=4.5),
            breaker=CircuitBreaker(failure_threshold=15, cooldown_seconds=120),
        )
        with checkpoint_path.open("a", encoding="utf-8", newline="\n") as checkpoint:
            for index, target in enumerate(targets, 1):
                key = (target["instrument_id"], target["period_end"])
                previous = checkpointed.get(key)
                if previous and _checkpoint_success_is_valid(previous, target, con, archive):
                    print(f"{index}/{len(targets)} {key[0]} {key[1]} checkpoint-ok", flush=True)
                    continue

                existing = _receipt_for_url(con, archive, target["url"])
                if existing:
                    result = {
                        **target,
                        "captured_at_beijing": now_beijing(),
                        "capture_source": "reused_verified_receipt",
                        "status": "ok",
                        "actual_receipt_id": existing["receipt_id"],
                        "receipt_url": existing["url"],
                        "content_hash": existing["content_hash"],
                        "size_bytes": existing["byte_size"],
                        "requested_at": existing["requested_at"],
                        "first_seen_at": existing["first_seen_at"],
                        "http_status": existing["http_status"],
                        "outcome": existing["outcome"],
                        "detail": existing["detail"],
                    }
                else:
                    requests += 1
                    captured_at = now_beijing()
                    try:
                        out = client.document(
                            target["url"],
                            label=f"revision-full-report:{target['instrument_id']}:{target['period_end']}",
                            max_bytes=MAX_BYTES,
                        )
                        receipt = con.execute(
                            "SELECT receipt_id,url,requested_at,responded_at,first_seen_at,outcome,"
                            "http_status,content_hash,byte_size,detail FROM fetch_receipt WHERE receipt_id=?",
                            (out.receipt_id,),
                        ).fetchone() if out.receipt_id else None
                        valid = bool(
                            out.ok and out.payload is not None and out.content_hash
                            and receipt
                            and receipt["url"] == target["url"]
                            and receipt["outcome"] == "OK"
                            and receipt["http_status"] == 200
                            and receipt["content_hash"] == out.content_hash
                            and receipt["byte_size"] == len(out.payload)
                            and archive.verify(out.content_hash)
                        )
                        result = {
                            **target,
                            "captured_at_beijing": captured_at,
                            "capture_source": "network_request",
                            "status": "ok" if valid else "failed",
                            "actual_receipt_id": out.receipt_id,
                            "receipt_url": receipt["url"] if receipt else target["url"],
                            "content_hash": out.content_hash,
                            "size_bytes": len(out.payload) if out.payload is not None else None,
                            "requested_at": receipt["requested_at"] if receipt else None,
                            "responded_at": receipt["responded_at"] if receipt else None,
                            "first_seen_at": receipt["first_seen_at"] if receipt else None,
                            "http_status": receipt["http_status"] if receipt else out.http_status,
                            "outcome": receipt["outcome"] if receipt else None,
                            "detail": None if valid else (receipt["detail"] if receipt else out.detail),
                        }
                    except Exception as exc:  # checkpoint each failed attempt for truthful audit
                        result = {
                            **target,
                            "captured_at_beijing": captured_at,
                            "capture_source": "network_request",
                            "status": "failed",
                            "actual_receipt_id": None,
                            "receipt_url": target["url"],
                            "content_hash": None,
                            "size_bytes": None,
                            "requested_at": None,
                            "first_seen_at": None,
                            "detail": f"{type(exc).__name__}: {exc}",
                        }

                checkpoint.write(json.dumps(result, ensure_ascii=False) + "\n")
                checkpoint.flush()
                checkpointed[key] = result
                print(f"{index}/{len(targets)} {key[0]} {key[1]} {result['status']} "
                      f"{result.get('size_bytes')} bytes", flush=True)
                if client.breaker.is_open:
                    stopped_on_breaker = True
                    break

    results = []
    for target in targets:
        key = (target["instrument_id"], target["period_end"])
        item = checkpointed.get(key)
        if item is None:
            item = {**target, "status": "not_attempted", "capture_source": None,
                    "actual_receipt_id": None, "content_hash": None, "size_bytes": None,
                    "requested_at": None, "first_seen_at": None,
                    "detail": "collector stopped before this item"}
        results.append(item)
    summary = {
        "schema": "aquant.s2-revision-pdf-capture.v2",
        "generated_at_beijing": now_beijing(),
        "requested_count": len(targets),
        "network_request_count": requests,
        "successful_count": sum(item.get("status") == "ok" for item in results),
        "failed_count": sum(item.get("status") != "ok" for item in results),
        "stopped_on_breaker": stopped_on_breaker,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "checkpoint": str(checkpoint_path),
        "results": results,
    }
    atomic_json(output_path, summary)
    print(json.dumps({key: summary[key] for key in (
        "requested_count", "network_request_count", "successful_count",
        "failed_count", "stopped_on_breaker", "elapsed_seconds", "checkpoint",
    )}, ensure_ascii=False))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", type=Path, default=TARGETS_PATH)
    parser.add_argument("--bound", type=Path, default=BOUND_PATH)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    parser.add_argument("--checkpoint", type=Path, default=CHECKPOINT_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()
    targets = _load_targets(args.targets, args.bound, args.limit)
    summary = run_capture(targets, args.checkpoint, args.output, args.archive_root)
    return 0 if summary["failed_count"] == 0 and not summary["stopped_on_breaker"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
