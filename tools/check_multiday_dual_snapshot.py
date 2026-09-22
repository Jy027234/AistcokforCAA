"""Deterministic two-day dual-snapshot acceptance.

This runner exercises the durable domain path without starting an API process,
using a small synthetic fixture and a fresh SQLite connection at each restart
boundary.  It is deliberately *not* real-production evidence: no network,
broker, scheduler, or wall-clock market wait is involved.

The acceptance covers two consecutive synthetic trading days.  Each day has
an explicit decision snapshot and execution-day EOD snapshot.  The first
day's EOD snapshot is reused as the second day's decision snapshot, which also
checks that a published EOD object can be carried across a process/database
reopen without consulting a moving current pointer.

Usage::

    python tools/check_multiday_dual_snapshot.py
    python tools/check_multiday_dual_snapshot.py --json-out path/report.json

Exit codes: 0 = all checks passed; 1 = acceptance failure; 2 = setup error.
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.data.db import apply_migrations, connect  # noqa: E402
from aquant.domain.data.ingest import SnapshotBuilder  # noqa: E402
from aquant.domain.data.reader import SnapshotReader  # noqa: E402
from aquant.domain.data.snapshot import (  # noqa: E402
    DataMode,
    DatasetRef,
    SnapshotDraft,
    SnapshotStore,
)
from aquant.domain.portfolio.construction import (  # noqa: E402
    Candidate,
    ConstructionParams,
)
from aquant.domain.portfolio.plan import PlanService  # noqa: E402
from aquant.domain.simulation.fees import synthetic_fee_table  # noqa: E402
from aquant.domain.simulation.simulator import BoardRule, Lot  # noqa: E402


PORTFOLIO = "pf-dual-snapshot-M"
SUBJECT = "user:dual-snapshot-acceptance"
INITIAL_CASH_CENTS = 100_000_000
FIXTURE = ROOT / "examples" / "snap-syn-001.yaml"

RULES = [
    BoardRule(
        exchange="SSE", board="MAIN", price_limit_pct=Decimal("10"),
        lot_size=100, effective_from=date(2026, 7, 6),
    ),
    BoardRule(
        exchange="SZSE", board="MAIN", price_limit_pct=Decimal("10"),
        lot_size=100, effective_from=date(2026, 7, 6),
    ),
]

LISTINGS = {
    "SYN.A.600519": ("SSE", "MAIN"),
    "SYN.A.000001": ("SZSE", "MAIN"),
    "SYN.A.600003": ("SSE", "MAIN"),
}

DAY1 = date(2026, 9, 9)
DAY2 = date(2026, 9, 10)


def _utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _service(con, api_root: Path) -> PlanService:
    reader = SnapshotReader(SnapshotStore(con, api_root))
    return PlanService(
        con,
        reader,
        synthetic_fee_table(),
        RULES,
        LISTINGS,
        ConstructionParams(
            max_holdings=3,
            max_single_name_pct=Decimal("20"),
            max_single_industry_pct=Decimal("50"),
        ),
    )


def _load_account(con, portfolio_id: str) -> tuple[list[Lot], int]:
    rows = con.execute(
        "SELECT lot_id,instrument_id,acquired_trading_day,earliest_sellable_day,"
        "quantity_original,quantity_remaining,cost_basis_cents_per_share "
        "FROM position_lot WHERE portfolio_id=? ORDER BY lot_id",
        (portfolio_id,),
    ).fetchall()
    lots = [
        Lot(
            lot_id=row["lot_id"],
            instrument_id=row["instrument_id"],
            acquired_trading_day=date.fromisoformat(row["acquired_trading_day"]),
            earliest_sellable_day=date.fromisoformat(row["earliest_sellable_day"]),
            quantity_original=int(row["quantity_original"]),
            quantity_remaining=int(row["quantity_remaining"]),
            cost_basis_cents_per_share=int(row["cost_basis_cents_per_share"]),
        )
        for row in rows
    ]
    cash = int(con.execute(
        "SELECT COALESCE(SUM(amount_cents),0) FROM cash_entry WHERE portfolio_id=?",
        (portfolio_id,),
    ).fetchone()[0])
    return lots, cash


def _publish_daily_snapshots(con, api_root: Path) -> dict[str, dict[str, Any]]:
    """Publish three filtered synthetic objects for the two-day run.

    The source manifest contains five days.  Every generated object is filtered
    to its own cutoff before writing, so the decision object cannot answer with
    a later bar merely because the fixture contains one.
    """

    builder = SnapshotBuilder(con, api_root / "datasets")
    base = builder.load_manifest(FIXTURE)
    builder.ensure_source(
        "dual-snapshot-synthetic",
        display_name="Deterministic dual-snapshot acceptance fixture",
        domains=["DAILY_QUOTES", "CALENDAR_IDENTITY", "CORPORATE_ACTIONS"],
        integration_state="TEST_PASSED",
        pit_available="NO",
        pit_basis="RECONSTRUCTED",
    )
    # Transactional identity tables are shared by the immutable dataset files.
    # Only the dataset contents and snapshot metadata vary by cutoff.
    builder.ingest(base, source_id="dual-snapshot-synthetic", data_version="dual-base")
    store = SnapshotStore(con, api_root)

    specs = [
        # decision for DAY1: available after DAY1-1 close, before DAY1 open
        ("dual-decision-2026-09-08", date(2026, 9, 8),
         _utc("2026-09-08T12:30:00Z"), _utc("2026-09-08T12:31:00Z")),
        # execution for DAY1 and decision for DAY2
        ("dual-eod-2026-09-09", DAY1,
         _utc("2026-09-09T12:30:00Z"), _utc("2026-09-09T12:31:00Z")),
        # execution for DAY2
        ("dual-eod-2026-09-10", DAY2,
         _utc("2026-09-10T12:30:00Z"), _utc("2026-09-10T12:31:00Z")),
    ]
    published: dict[str, dict[str, Any]] = {}
    for snapshot_id, end_day, as_of, published_at in specs:
        doc = copy.deepcopy(base)
        doc["snapshot_id"] = snapshot_id
        doc["trading_days"] = [
            day for day in base["trading_days"] if date.fromisoformat(day) <= end_day
        ]
        doc["daily_quotes"] = [
            quote for quote in base["daily_quotes"]
            if date.fromisoformat(quote["trading_day"]) <= end_day
        ]
        cutoff = _iso(as_of)
        doc["input_cutoff_at"] = cutoff
        doc["as_of_time"] = cutoff
        doc["published_at"] = _iso(published_at)
        # Do not carry evidence that was not available by this cutoff.  These
        # datasets are not used by PlanService in this acceptance, but keeping
        # the fixture honest makes the generated objects useful for inspection.
        doc["events"] = [
            event for event in base.get("events", [])
            if not event.get("available_at") or _utc(event["available_at"]) <= as_of
        ]
        doc["corporate_actions"] = [
            action for action in base.get("corporate_actions", [])
            if not action.get("announced_on")
            or date.fromisoformat(action["announced_on"]) <= end_day
        ]
        refs = builder.write_datasets(doc, snapshot_id=snapshot_id)
        draft = SnapshotDraft(
            snapshot_id=snapshot_id,
            kind="EOD",
            data_mode=DataMode.SYNTHETIC,
            input_cutoff_at=as_of,
            as_of_time=as_of,
            created_at=published_at - timedelta(minutes=1),
            published_at=published_at,
            code_version="dual-snapshot-acceptance",
            data_version=f"dual-{end_day.isoformat()}",
            watermark=(
                "SYNTHETIC DATA -- DUAL-SNAPSHOT ACCEPTANCE ONLY; "
                f"cutoff={end_day.isoformat()}"
            ),
            pool_hash="sha256:" + "d" * 64,
            datasets=[
                DatasetRef(
                    name=ref["name"],
                    path=ref["path"],
                    sha256=ref["sha256"],
                    record_count=int(ref["record_count"]),
                    as_of_upper_bound=_utc(ref["as_of_upper_bound"]),
                )
                for ref in refs
            ],
        )
        store.publish(draft)
        published[snapshot_id] = {
            "trading_day": end_day.isoformat(),
            "as_of": _iso(as_of),
            "published_at": _iso(published_at),
        }
    return published


def _valuation_identity(con, *, valuation_id: str, requested_snapshot_id: str,
                        requested_plan_id: str, requested_as_of: datetime) -> dict[str, Any]:
    """Read and strictly compare the durable valuation provenance sidecar."""

    row = con.execute(
        "SELECT snapshot_id,execution_plan_id,as_of FROM valuation_provenance "
        "WHERE valuation_id=?",
        (valuation_id,),
    ).fetchone()
    expected_as_of = _iso(requested_as_of)
    persisted = {
        "snapshot_id": row["snapshot_id"] if row else None,
        "execution_plan_id": row["execution_plan_id"] if row else None,
        "as_of": row["as_of"] if row else None,
    }
    return {
        "requested_snapshot_id": requested_snapshot_id,
        "requested_execution_plan_id": requested_plan_id,
        "requested_as_of": expected_as_of,
        "persisted_snapshot_id": persisted["snapshot_id"],
        "persisted_execution_plan_id": persisted["execution_plan_id"],
        "persisted_as_of": persisted["as_of"],
        "mode": "persisted_sidecar",
        "ok": (
            row is not None
            and persisted["snapshot_id"] == requested_snapshot_id
            and persisted["execution_plan_id"] == requested_plan_id
            and persisted["as_of"] == expected_as_of
        ),
    }


def run_acceptance(work_dir: str | Path) -> dict[str, Any]:
    """Run the deterministic acceptance in ``work_dir`` and return JSON data."""

    root = Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    db_path = root / "meta.sqlite"
    api_root = root / "api"
    api_root.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema_version": "aquant.multiday.dual_snapshot_acceptance.v1",
        "evidence_scope": "SYNTHETIC_DETERMINISTIC_DOMAIN_TEST",
        "production_evidence": False,
        "network_used": False,
        "wall_clock_market_wait": False,
        "process_reopens": 0,
        "snapshot_catalog": {},
        "days": [],
        "checks": [],
    }
    con = None

    def check(name: str, ok: bool, detail: str = "") -> None:
        item = {"name": name, "ok": bool(ok)}
        if detail:
            item["detail"] = detail
        report["checks"].append(item)

    try:
        con = connect(db_path)
        apply_migrations(con)
        report["snapshot_catalog"] = _publish_daily_snapshots(con, api_root)
        check(
            "three immutable synthetic snapshots published",
            len(report["snapshot_catalog"]) == 3,
            ", ".join(sorted(report["snapshot_catalog"])),
        )
        svc = _service(con, api_root)
        previous_end: dict[str, Any] | None = None
        day_specs = [
            {
                "trading_day": DAY1,
                "decision_snapshot_id": "dual-decision-2026-09-08",
                "execution_snapshot_id": "dual-eod-2026-09-09",
                "candidates": [
                    Candidate("SYN.A.600519", "SW_SYN_01", 0.90),
                    Candidate("SYN.A.000001", "SW_SYN_02", 0.70),
                ],
            },
            {
                "trading_day": DAY2,
                # DAY1 EOD is the explicitly published decision input for DAY2.
                "decision_snapshot_id": "dual-eod-2026-09-09",
                "execution_snapshot_id": "dual-eod-2026-09-10",
                "candidates": [
                    Candidate("SYN.A.600519", "SW_SYN_01", 0.90),
                    Candidate("SYN.A.000001", "SW_SYN_02", 0.70),
                    Candidate("SYN.A.600003", "SW_SYN_05", 0.60),
                ],
            },
        ]

        for index, spec in enumerate(day_specs, start=1):
            day = spec["trading_day"]
            decision_id = str(spec["decision_snapshot_id"])
            execution_id = str(spec["execution_snapshot_id"])
            decision_as_of = _utc(report["snapshot_catalog"][decision_id]["as_of"])
            execution_as_of = _utc(report["snapshot_catalog"][execution_id]["as_of"])
            lots, cash = _load_account(con, PORTFOLIO)
            if con.execute(
                "SELECT 1 FROM portfolio WHERE portfolio_id=?", (PORTFOLIO,)
            ).fetchone() is None:
                # The first preview creates the simulated account during the
                # confirmation path.  Seed only the input cash here; all
                # subsequent days must use the SQLite ledger value.
                cash = INITIAL_CASH_CENTS
            day_report: dict[str, Any] = {
                "trading_day": day.isoformat(),
                "decision_snapshot_id": decision_id,
                "execution_snapshot_id": execution_id,
                "decision_cutoff_at": _iso(decision_as_of),
                "execution_cutoff_at": _iso(execution_as_of),
                "checks": [],
            }

            def day_check(name: str, ok: bool, detail: str = "") -> None:
                item = {"name": name, "ok": bool(ok)}
                if detail:
                    item["detail"] = detail
                day_report["checks"].append(item)
                check(f"{day.isoformat()} {name}", ok, detail)

            if previous_end is not None:
                day_check(
                    "reopened account matches prior EOD cash",
                    cash == previous_end["cash_cents"],
                    f"{cash} vs {previous_end['cash_cents']}",
                )
                day_check(
                    "reopened account matches prior EOD positions",
                    {lot.instrument_id: lot.quantity_remaining for lot in lots}
                    == previous_end["positions"],
                    json.dumps(
                        {
                            "current": {lot.instrument_id: lot.quantity_remaining for lot in lots},
                            "prior": previous_end["positions"],
                        },
                        ensure_ascii=False,
                    ),
                )

            preview = svc.preview(
                portfolio_id=PORTFOLIO,
                snapshot_id=decision_id,
                decision_snapshot_id=decision_id,
                decision_cutoff_at=decision_as_of,
                execution_snapshot_id=execution_id,
                trading_day=day,
                as_of=decision_as_of,
                candidates=list(spec["candidates"]),
                cash_available_cents=cash,
                lots=lots,
                confirm_subject=SUBJECT,
            )
            day_check(
                "preview carries explicit dual snapshot identity",
                preview.decision_snapshot_id == decision_id
                and preview.execution_snapshot_id == execution_id
                and preview.decision_cutoff_at == decision_as_of
                and preview.execution_cutoff_at == execution_as_of,
                json.dumps(preview.as_dict(), ensure_ascii=False)[:500],
            )

            freeze_now = execution_as_of - timedelta(hours=12)
            token = svc.issue_confirmation(
                preview=preview,
                subject=SUBJECT,
                current_lots=lots,
                current_cash_cents=cash,
                ttl=timedelta(days=2),
                now=freeze_now,
            )
            frozen = svc.freeze(
                preview=preview,
                confirm_subject=SUBJECT,
                confirmation_token=token,
                expected_account_version=preview.account_version,
                current_lots=lots,
                current_cash_cents=cash,
                ttl=timedelta(days=2),
                now=freeze_now,
            )
            binding = con.execute(
                "SELECT decision_snapshot_id,decision_cutoff_at,"
                "execution_snapshot_id,execution_cutoff_at "
                "FROM plan_snapshot_binding WHERE plan_id=?",
                (preview.plan_id,),
            ).fetchone()
            binding_dict = dict(binding) if binding else {}
            day_check(
                "freeze persists both snapshot IDs and cutoffs",
                frozen.get("status") == "FROZEN"
                and binding_dict == {
                    "decision_snapshot_id": decision_id,
                    "decision_cutoff_at": _iso(decision_as_of),
                    "execution_snapshot_id": execution_id,
                    "execution_cutoff_at": _iso(execution_as_of),
                },
                json.dumps(binding_dict, ensure_ascii=False),
            )

            # Simulate an API process and database connection restart.  The
            # execute path is intentionally given no in-memory account state.
            con.close()
            con = connect(db_path)
            svc = _service(con, api_root)
            report["process_reopens"] += 1
            execute_now = execution_as_of + timedelta(minutes=5)
            executed = svc.execute(
                plan_id=preview.plan_id,
                subject=SUBJECT,
                now=execute_now,
            )
            day_check(
                "reopened process executes frozen plan",
                executed.get("status") == "EXECUTED"
                and executed.get("decision_snapshot_id") == decision_id
                and executed.get("execution_snapshot_id") == execution_id,
                json.dumps(
                    {
                        "fills": len(executed.get("fills") or []),
                        "decision_snapshot_id": executed.get("decision_snapshot_id"),
                        "execution_snapshot_id": executed.get("execution_snapshot_id"),
                    },
                    ensure_ascii=False,
                ),
            )

            # Reopen once more before EOD valuation.  This proves valuation and
            # reconciliation reconstruct the post-fill account from SQLite.
            con.close()
            con = connect(db_path)
            svc = _service(con, api_root)
            report["process_reopens"] += 1
            after_lots, after_cash = _load_account(con, PORTFOLIO)
            valuation = svc.value(
                portfolio_id=PORTFOLIO,
                snapshot_id=execution_id,
                trading_day=day,
                as_of=execution_as_of,
                execution_plan_id=preview.plan_id,
                lots=after_lots,
                cash_available_cents=after_cash,
            )
            valuation_id = f"val-{PORTFOLIO}-{day.isoformat()}"
            identity = _valuation_identity(
                con,
                valuation_id=valuation_id,
                requested_snapshot_id=execution_id,
                requested_plan_id=preview.plan_id,
                requested_as_of=execution_as_of,
            )
            reconcile = svc.reconcile(portfolio_id=PORTFOLIO)
            day_check(
                "EOD valuation is published and reconciled",
                valuation.get("published") is True and reconcile.get("reconciled") is True,
                json.dumps(
                    {
                        "published": valuation.get("published"),
                        "net_value_cents": valuation.get("net_value_cents"),
                        "reconciled": reconcile.get("reconciled"),
                    },
                    ensure_ascii=False,
                ),
            )
            day_check(
                "valuation call is bound to the day's EOD snapshot",
                identity["ok"] and identity["requested_snapshot_id"] == execution_id,
                json.dumps(identity, ensure_ascii=False),
            )
            day_report.update({
                "plan_id": preview.plan_id,
                "frozen": frozen,
                "execute": executed,
                "valuation": {
                    "published": bool(valuation.get("published")),
                    "net_value_cents": valuation.get("net_value_cents"),
                    "identity": identity,
                },
                "reconcile": reconcile,
                "fills": len(executed.get("fills") or []),
                "checks": day_report["checks"],
            })
            report["days"].append(day_report)
            previous_end = {
                "cash_cents": int(reconcile["cash_cents"]),
                "positions": dict(reconcile.get("positions") or {}),
            }

        check(
            "two consecutive trading days completed",
            len(report["days"]) == 2
            and [item["trading_day"] for item in report["days"]]
            == [DAY1.isoformat(), DAY2.isoformat()],
        )
        check(
            "each day has distinct decision and EOD execution snapshots",
            all(
                item["decision_snapshot_id"] != item["execution_snapshot_id"]
                and item["execution_snapshot_id"].startswith("dual-eod-")
                for item in report["days"]
            ),
        )
        check(
            "second-day decision reuses first-day published EOD snapshot",
            report["days"][1]["decision_snapshot_id"]
            == report["days"][0]["execution_snapshot_id"],
        )
    except Exception as exc:  # noqa: BLE001 - acceptance boundary reports failure
        report["error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
    finally:
        if con is not None:
            con.close()

    report["conclusion"] = (
        "PASS"
        if not report.get("error") and all(item["ok"] for item in report["checks"])
        else "FAIL"
    )
    report["checks_passed"] = sum(1 for item in report["checks"] if item["ok"])
    report["checks_total"] = len(report["checks"])
    return report


def _write_report(report: dict[str, Any], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json-out",
        type=Path,
        default=ROOT / "deploy" / "agentctl-q0" / "multiday-dual-snapshot.json",
        help="structured report path (default: deploy/agentctl-q0/multiday-dual-snapshot.json)",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        help="retain the temporary SQLite/snapshot workspace at this path",
    )
    args = parser.parse_args(argv)

    if args.work_dir:
        report = run_acceptance(args.work_dir)
        _write_report(report, args.json_out)
    else:
        temp = Path(tempfile.mkdtemp(prefix="aquant-dual-snapshot-"))
        try:
            report = run_acceptance(temp)
            _write_report(report, args.json_out)
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    print(
        f"双快照多日合成验收 {report['checks_passed']}/"
        f"{report['checks_total']} 通过，结论：{report['conclusion']}"
    )
    print(f"报告：{args.json_out}")
    return 0 if report["conclusion"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
