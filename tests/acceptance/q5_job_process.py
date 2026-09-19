"""One-shot product JobStore worker used by the live Q5 crash exercise.

Each invocation opens the durable SQLite store in a fresh process, performs
one lease/state operation, prints a credential-free JSON result, and exits.
The parent acceptance runner uses separate invocations to prove that recovery
does not depend on in-process objects.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aquant.domain.data.db import connect
from aquant.operations.jobs import JobError, JobStatus, JobStore


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--meta-path", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument(
        "--action",
        choices=(
            "claim", "release", "finish", "finish-expect-rejected",
            "run-research", "status",
        ),
        required=True,
    )
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--worker-id", default="")
    parser.add_argument("--lease-seconds", type=int, default=300)
    parser.add_argument("--attempt-count", type=int)
    parser.add_argument("--status", choices=tuple(item.value for item in JobStatus))
    return parser.parse_args()


def main() -> int:
    args = _args()
    con = connect(args.meta_path)
    store = JobStore(con)
    output: dict[str, object]
    try:
        if args.action == "claim":
            job = store.claim(
                args.worker_id,
                lease_seconds=args.lease_seconds,
                job_id=args.job_id,
            )
            if job is None:
                raise RuntimeError(f"job {args.job_id} was not claimable")
            output = {
                "ok": True,
                "action": args.action,
                "job_id": job.job_id,
                "status": job.status.value,
                "attempt_count": job.attempt_count,
                "lease_owner": job.lease_owner,
            }
        elif args.action == "release":
            store.release(
                args.job_id,
                args.worker_id,
                attempt_count=args.attempt_count,
                reason="Q5 cancellation request",
            )
            job = store.get(args.job_id)
            output = {
                "ok": True,
                "action": args.action,
                "job_id": job.job_id,
                "status": job.status.value,
                "attempt_count": job.attempt_count,
                "lease_owner": job.lease_owner,
            }
        elif args.action in {"finish", "finish-expect-rejected"}:
            if args.status is None:
                raise ValueError("--status is required for finish actions")
            target = JobStatus(args.status)
            try:
                store.finish(
                    args.job_id,
                    target,
                    worker_id=args.worker_id or None,
                    attempt_count=args.attempt_count,
                    result={"q5": "completed"} if target is JobStatus.SUCCEEDED else None,
                    error_code="DATA_NOT_READY" if target is JobStatus.FAILED else None,
                    error_detail="Q5 deliberate failure" if target is JobStatus.FAILED else None,
                )
                rejected = False
                error_code = None
            except JobError as exc:
                rejected = True
                error_code = exc.code
                if args.action != "finish-expect-rejected":
                    raise
            if args.action == "finish-expect-rejected" and not rejected:
                raise RuntimeError("callback was accepted but rejection was required")
            job = store.get(args.job_id)
            output = {
                "ok": True,
                "action": args.action,
                "rejected": rejected,
                "error_code": error_code,
                "job_id": job.job_id,
                "status": job.status.value,
                "attempt_count": job.attempt_count,
                "lease_owner": job.lease_owner,
            }
        elif args.action == "run-research":
            if args.data_dir is None:
                raise ValueError("--data-dir is required for run-research")
            from aquant.domain.data.reader import SnapshotReader
            from aquant.domain.data.snapshot import SnapshotStore
            from aquant.operations.research_jobs import run_research_job

            reader = SnapshotReader(SnapshotStore(con, args.data_dir / "api"))
            result = run_research_job(
                con,
                reader,
                job_id=args.job_id,
                worker_id=args.worker_id,
            )
            job = store.get(args.job_id)
            output = {
                "ok": True,
                "action": args.action,
                "job_id": job.job_id,
                "status": job.status.value,
                "attempt_count": job.attempt_count,
                "lease_owner": job.lease_owner,
                "reused": result.get("reused"),
                "research_run_id": (result.get("result") or {}).get("researchRunId"),
            }
        else:
            job = store.get(args.job_id)
            output = {
                "ok": True,
                "action": args.action,
                "job_id": job.job_id,
                "status": job.status.value,
                "attempt_count": job.attempt_count,
                "lease_owner": job.lease_owner,
            }
    finally:
        con.close()
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
