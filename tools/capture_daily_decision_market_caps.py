"""Capture receipt-backed closing market caps for one frozen simulation pool.

This wrapper prepares the provider's bare-code universe from the supplied pool,
refreshes the official SSE calendar through the existing CLI, and then runs the
existing Tencent closing-cap collector. It records the frozen pool hash and
provider evidence in an attempt-local coverage report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
BEIJING = ZoneInfo("Asia/Shanghai")
TRUST_ENV = "AQUANT_TRUSTED_PROXY_NETWORKS"


def _now() -> datetime:
    return datetime.now(BEIJING)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                        sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_pool(path: Path) -> tuple[dict, bytes, list[str]]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        import yaml
        value = yaml.safe_load(raw)
    if not isinstance(value, dict) or not isinstance(value.get("instruments"), list):
        raise ValueError("pool must be an object with an instruments array")
    codes: list[str] = []
    seen: set[str] = set()
    for item in value["instruments"]:
        if not isinstance(item, dict):
            raise ValueError("pool instruments must be objects")
        iid = str(item.get("instrument_id") or "").upper()
        code = str(item.get("code") or "").lower()
        match = re.fullmatch(r"(SH|SZ)\.([0-9]{6})", iid)
        if match is None:
            raise ValueError(f"unsupported instrument_id in pool: {iid!r}")
        exchange, numeric_code = match.groups()
        supported = ((exchange == "SH" and numeric_code.startswith(("60", "68"))) or
                     (exchange == "SZ" and numeric_code.startswith(("00", "30"))))
        if not supported:
            raise ValueError(f"instrument_id is outside the supported Shanghai/Shenzhen stock scope: {iid}")
        expected = ("sh" if exchange == "SH" else "sz") + numeric_code
        if code != expected:
            raise ValueError(f"pool code does not match instrument_id: {iid!r}")
        if expected in seen:
            raise ValueError(f"duplicate instrument in pool: {iid}")
        seen.add(expected)
        codes.append(match.group(2))
    if not codes:
        raise ValueError("pool instruments must be non-empty")
    return value, raw, codes


def _trusted_networks(env_file: Path, base_env: dict[str, str]) -> dict[str, str]:
    """Load only the explicitly authorized proxy-trust setting, never echo it."""
    if not env_file.is_file():
        raise ValueError(f"required saved network env file is missing: {env_file}")
    saved: str | None = None
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, val = text.split("=", 1)
        if key.strip() == TRUST_ENV:
            saved = val.strip().strip("\"'")
            break
    if not saved:
        raise ValueError(f"{TRUST_ENV} is empty or missing from the saved network env")
    result = dict(base_env)
    result[TRUST_ENV] = saved
    return result


def _reusable_sidecar(output: Path, coverage: Path, archive_root: Path,
                      expected_day: str, codes: list[str], pool_hash: str) -> dict | None:
    if not output.is_file() or not coverage.is_file():
        return None
    from aquant.operations.decision_market_caps import load_sidecar

    try:
        sidecar = load_sidecar(output, expected_as_of=expected_day,
                               archive_root=archive_root)
        actual_ids = sorted(sidecar.items)
        expected_ids = sorted(("SH." if code.startswith(("6", "68")) else "SZ.") + code
                              for code in codes)
        if actual_ids != expected_ids:
            return None
        report = json.loads(coverage.read_text(encoding="utf-8"))
        if report.get("pool_sha256") != pool_hash or report.get("market_day") != expected_day:
            return None
        if report.get("status") not in {"complete", "reused"}:
            return None
        provider_report = report.get("provider_report")
        if not isinstance(provider_report, dict) or provider_report.get("published") is not True:
            return None
        return {"sidecar": sidecar, "coverage": report}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _validate_attempt_outputs(output: Path, calendar: Path, coverage: Path) -> None:
    parents = {path.resolve().parent for path in (output, calendar, coverage)}
    if len(parents) != 1:
        raise ValueError("sidecar, calendar and coverage outputs must share one attempt directory")
    attempt_dir = next(iter(parents))
    if not attempt_dir.name.startswith("attempt"):
        raise ValueError("capture outputs must be placed directly in an attempt directory")


def capture(*, pool_path: Path, day: date, output: Path, archive_root: Path,
            calendar_path: Path, coverage_output: Path,
            env_file: Path = ROOT / "real-trial.env") -> tuple[int, dict]:
    started = _now()
    report: dict = {
        "schema": "aquant.daily_decision_market_cap_capture.v1",
        "market_day": day.isoformat(), "started_at": started.isoformat(),
        "status": "failed", "pool_path": str(pool_path.resolve()),
        "output": str(output.resolve()), "archive_root": str(archive_root.resolve()),
        "calendar_path": str(calendar_path.resolve()),
        "coverage_output": str(coverage_output.resolve()),
        "steps": [], "accepted_count": 0, "requested_count": 0,
    }
    try:
        pool, pool_bytes, codes = _load_pool(pool_path)
        pool_hash = _sha256(pool_bytes)
        report.update({"pool_id": pool.get("pool_id"), "pool_sha256": pool_hash,
                       "pool_count": len(codes), "requested_count": len(codes),
                       "instrument_ids": sorted(str(x["instrument_id"]).upper()
                                                 for x in pool["instruments"])})
        env = _trusted_networks(env_file, dict(os.environ))
        _validate_attempt_outputs(output, calendar_path, coverage_output)
        if day != started.date() or started.time() < time(15, 0):
            raise ValueError("capture requires the requested Beijing date and must start at/after 15:00")

        reusable = _reusable_sidecar(output, coverage_output, archive_root,
                                     day.isoformat(), codes, pool_hash)
        if reusable is not None:
            original = reusable["coverage"]
            metadata = dict(original)
            metadata.update({"status": "reused", "reused_existing": True,
                             "reuse_checked_at": _now().isoformat(),
                             "coverage_artifact_path": str(coverage_output.resolve()),
                             "coverage_artifact_sha256": _sha256(coverage_output.read_bytes())})
            return 0, metadata

        universe_path = output.with_suffix(output.suffix + ".universe.txt")
        universe_path.parent.mkdir(parents=True, exist_ok=True)
        universe_path.write_text("\n".join(codes) + "\n", encoding="ascii")
        report["universe_path"] = str(universe_path.resolve())
        report["universe_sha256"] = _sha256(universe_path.read_bytes())

        commands = [
            ("calendar", [sys.executable, str(ROOT / "tools" / "admit_s2_industrial_pdf_review.py"),
                          "calendar", "--archive-root", str(archive_root), "--output", str(calendar_path)]),
            ("market_caps", [sys.executable, str(ROOT / "tools" / "collect_market_caps_tencent.py"),
                             "--universe", str(universe_path), "--archive-root", str(archive_root),
                             "--output", str(output), "--coverage-output", str(coverage_output),
                             "--calendar", str(calendar_path), "--as-of", day.isoformat(),
                             "--min-coverage", "1"]),
        ]
        for name, command in commands:
            step_started = _now()
            completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                                       text=True, encoding="utf-8", errors="replace", check=False)
            step = {"name": name, "started_at": step_started.isoformat(),
                    "finished_at": _now().isoformat(), "exit_code": completed.returncode}
            report["steps"].append(step)
            if completed.returncode != 0:
                report["failed_step"] = name
                report["failure"] = f"{name} CLI exited {completed.returncode}"
                provider_report = None
                if name == "market_caps" and coverage_output.is_file():
                    try:
                        provider_report = json.loads(coverage_output.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        provider_report = None
                if provider_report is not None:
                    report["provider_report"] = provider_report
                    report["accepted_count"] = int(provider_report.get("accepted_count") or 0)
                report["finished_at"] = _now().isoformat()
                _write_json(coverage_output, report)
                return completed.returncode or 1, report

        provider_report = json.loads(coverage_output.read_text(encoding="utf-8"))
        report["provider_report"] = provider_report
        report["accepted_count"] = int(provider_report.get("accepted_count") or 0)
        if provider_report.get("accepted_count") != len(codes) or not provider_report.get("complete_coverage"):
            report["failure"] = "provider coverage does not match the frozen 120-security pool"
            report["finished_at"] = _now().isoformat()
            _write_json(coverage_output, report)
            return 1, report
        report["receipt_id"] = provider_report.get("receipt_id")
        report["content_hash"] = provider_report.get("content_hash")
        sidecar = json.loads(output.read_text(encoding="utf-8"))
        report["observed_at"] = sidecar.get("observed_at")
        report["status"] = "complete"
        report["finished_at"] = _now().isoformat()
        _write_json(coverage_output, report)
        return 0, report
    except Exception as exc:  # Preserve a truthful missing/failed coverage record.
        report["failure"] = f"{type(exc).__name__}: {exc}"
        report["failed_step"] = report.get("failed_step", "preflight")
        report["finished_at"] = _now().isoformat()
        _write_json(coverage_output, report)
        return 1, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--day", required=True, help="YYYY-MM-DD, Beijing market day")
    parser.add_argument("--output", type=Path, required=True, help="attempt-local sidecar output")
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True, help="attempt-local calendar packet")
    parser.add_argument("--coverage-output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=ROOT / "real-trial.env")
    args = parser.parse_args(argv)
    try:
        market_day = date.fromisoformat(args.day)
    except ValueError:
        parser.error("--day must be YYYY-MM-DD")
    code, report = capture(pool_path=args.pool, day=market_day, output=args.output,
                           archive_root=args.archive_root, calendar_path=args.calendar,
                           coverage_output=args.coverage_output, env_file=args.env_file)
    print(json.dumps(report, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
