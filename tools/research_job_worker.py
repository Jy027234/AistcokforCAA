"""Dedicated single-job worker for version-bound financial coverage checks.

The API only enqueues and serves job state.  This process claims at most one
``FINANCIAL_CHECK`` per poll, so it cannot consume factor/evidence jobs or hold
an API thread during the long read-only verification.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.fundamentals.fact_repository import shutdown_review_workers  # noqa: E402
from aquant.operations.jobs import JobStore  # noqa: E402
from aquant.operations.research_jobs import JOB_FINANCIAL_CHECK, run_pending  # noqa: E402


HEARTBEAT_FILE = "financial-worker.json"
WORKER_PULSE_SECONDS = 10
JOB_LEASE_SECONDS = 300
JOB_RENEW_SECONDS = 45
DEFAULT_POLL_SECONDS = 5


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _heartbeat_path(data_dir: Path) -> Path:
    configured = os.environ.get("AQUANT_FINANCIAL_WORKER_HEARTBEAT_FILE", "").strip()
    return Path(configured).expanduser().resolve() if configured else data_dir / HEARTBEAT_FILE


def _write_heartbeat(path: Path, *, worker_id: str, started_at: datetime,
                     status: str = "RUNNING") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "workerId": worker_id,
        "pid": os.getpid(),
        "status": status,
        "startedAt": started_at.astimezone(timezone.utc).isoformat(),
        "heartbeatAt": _utc_now().isoformat(),
        "intervalSeconds": WORKER_PULSE_SECONDS,
    }
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(path.parent),
            prefix=".financial-worker-", suffix=".tmp", delete=False,
        ) as handle:
            temp_name = handle.name
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        if temp_name:
            try:
                Path(temp_name).unlink()
            except OSError:
                pass
        raise


def _heartbeat_loop(meta_path: Path, heartbeat_path: Path, worker_id: str,
                    started_at: datetime, stop: threading.Event) -> None:
    next_lease_renewal = time.monotonic()
    while not stop.is_set():
        try:
            _write_heartbeat(heartbeat_path, worker_id=worker_id,
                             started_at=started_at)
        except OSError as exc:
            print(f"[financial-worker] heartbeat file error: {type(exc).__name__}",
                  flush=True)

        now = time.monotonic()
        if now >= next_lease_renewal:
            try:
                with closing(connect(meta_path)) as con:
                    rows = con.execute(
                        "SELECT job_id,attempt_count FROM job WHERE status='RUNNING' "
                        "AND job_type=? AND lease_owner=?",
                        (JOB_FINANCIAL_CHECK, worker_id),
                    ).fetchall()
                    store = JobStore(con)
                    for row in rows:
                        store.heartbeat(
                            row["job_id"], worker_id, row["attempt_count"],
                            lease_seconds=JOB_LEASE_SECONDS,
                        )
            except Exception as exc:  # noqa: BLE001 - the next pulse retries renewal.
                print(f"[financial-worker] lease heartbeat error: {type(exc).__name__}",
                      flush=True)
            next_lease_renewal = now + JOB_RENEW_SECONDS
        stop.wait(WORKER_PULSE_SECONDS)


def _healthcheck(path: Path) -> int:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        stamp = datetime.fromisoformat(str(payload["heartbeatAt"]))
        if stamp.tzinfo is None or str(payload.get("status")).upper() != "RUNNING":
            return 1
        age = (_utc_now() - stamp.astimezone(timezone.utc)).total_seconds()
        stale_after = max(90.0, float(payload.get("intervalSeconds") or 0) * 3)
        if age > stale_after:
            return 1
        pid = int(payload["pid"])
        os.kill(pid, 0)
        return 0
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return 1


def _data_dir(explicit: str | None) -> Path | None:
    raw = (explicit or os.environ.get("AQUANT_DATA_DIR", "")).strip()
    return Path(raw).expanduser().resolve() if raw else None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Process only FINANCIAL_CHECK research jobs")
    parser.add_argument("--data-dir", help="same shared snapshot/job data directory as the API")
    parser.add_argument("--once", action="store_true", help="claim at most one job and exit")
    parser.add_argument("--healthcheck", action="store_true", help="check the worker liveness file")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    data_dir = _data_dir(args.data_dir)
    if data_dir is None:
        print("[financial-worker] AQUANT_DATA_DIR or --data-dir is required", flush=True)
        return 2
    heartbeat_path = _heartbeat_path(data_dir)
    if args.healthcheck:
        return _healthcheck(heartbeat_path)
    meta_path = data_dir / "meta.sqlite"
    if not data_dir.is_dir() or not meta_path.is_file():
        print("[financial-worker] shared snapshot data directory is unavailable", flush=True)
        return 2
    try:
        with closing(connect(meta_path, read_only=True)) as probe:
            if not probe.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job'"
            ).fetchone():
                print("[financial-worker] job table is unavailable", flush=True)
                return 2
    except Exception as exc:  # noqa: BLE001 - avoid exposing local paths or DSNs.
        print(f"[financial-worker] database preflight failed: {type(exc).__name__}",
              flush=True)
        return 2

    worker_id = "financial-" + uuid.uuid4().hex[:12]
    started_at = _utc_now()
    stop = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat_loop,
        args=(meta_path, heartbeat_path, worker_id, started_at, stop),
        name="financial-job-heartbeat", daemon=True,
    )
    heartbeat.start()
    exit_code = 0
    try:
        with closing(connect(meta_path)) as con:
            while True:
                results = run_pending(
                    con, None, limit=1, worker_id=worker_id,
                    job_types=[JOB_FINANCIAL_CHECK],
                )
                if results:
                    # This worker runs one job at a time. Reap its review children
                    # before waiting for the next job, which starts its own pool.
                    shutdown_review_workers()
                    item = results[0]
                    print(
                        f"[financial-worker] job={item.get('jobId')} "
                        f"status={item.get('status')}", flush=True,
                    )
                    if args.once:
                        if item.get("status") != "SUCCEEDED":
                            exit_code = 1
                        break
                elif args.once:
                    break
                if stop.wait(max(0.25, args.poll_seconds)):
                    break
    except KeyboardInterrupt:
        exit_code = 0
    except Exception as exc:  # noqa: BLE001 - state/error detail is already in the job row.
        print(f"[financial-worker] execution loop failed: {type(exc).__name__}", flush=True)
        exit_code = 1
    finally:
        stop.set()
        heartbeat.join(timeout=WORKER_PULSE_SECONDS + 2)
        try:
            _write_heartbeat(heartbeat_path, worker_id=worker_id,
                             started_at=started_at, status="STOPPED")
        except OSError as exc:
            print(f"[financial-worker] heartbeat shutdown error: {type(exc).__name__}",
                  flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
