"""Recompute the admitted research universe with newly receipted daily quotes.

Financial, industry and announcement inputs retain their own dates and hashes.
This command never fetches data and never invents a new financial observation.
"""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from aquant.domain.data.db import connect
from aquant.domain.data.forward_archive import ForwardArchive
from aquant.operations.research_candidates import file_hash
from aquant.operations.research_scan import load_research_scan, rebuild_scan
from bind_structured_to_announcements import verified_organization_map
from publish_research_scan import freeze, verify_announcements, verify_quotes, write_json


def refresh(*, baseline: Path, quotes_path: Path, archive_root: Path,
            output: Path, trading_day: str, announcements_path: Path | None = None) -> dict:
    load_research_scan(baseline, limit=1)  # Verify the admitted immutable inputs.
    prior = json.loads(baseline.read_text(encoding="utf-8"))
    quotes = json.loads(quotes_path.read_text(encoding="utf-8"))
    with closing(connect(archive_root / "meta.sqlite")) as con:
        archive = ForwardArchive(con, archive_root)
        universe, _, _ = verified_organization_map(
            ROOT / "data/s2-structured/universe-orgids.json", archive)
        verify_quotes(quotes, universe, archive, con)
        if announcements_path is not None:
            announcement_packet = json.loads(announcements_path.read_text(encoding="utf-8"))
            verify_announcements(announcement_packet, universe, archive, con)
    cst = timezone(timedelta(hours=8))
    start = datetime.fromisoformat(quotes["capture_started_at"]).astimezone(cst)
    end = datetime.fromisoformat(quotes["capture_finished_at"]).astimezone(cst)
    if (quotes.get("quote_time_kind") != "after_close_research_snapshot"
            or start.date().isoformat() != trading_day or end.date() != start.date()
            or start.hour < 15):
        raise ValueError("daily research requires a same-day after-close quote receipt")
    normalized = dict(quotes, captured_from=quotes["capture_started_at"],
        captured_to=quotes["capture_finished_at"], universe_codes=sorted(universe),
        research_quote_mode="CLOSING_RESEARCH")
    for row in normalized["rows"]:
        row["batch_lag_seconds"] = row["quote_time_lag_seconds_from_batch_latest"]
    quote_input = output.parent / f"research-quote-input-{trading_day}.json"
    write_json(quote_input, normalized)
    frozen = freeze(quote_input, output.parent / "frozen-inputs")
    sources = [s for s in prior["provenance"] if s["role"] != "quotes"]
    sources.append({"role": "quotes", "path": str(frozen.resolve()), "contentHash": file_hash(frozen)})
    if announcements_path is not None:
        sources = [s for s in sources if s["role"] != "announcements"]
        frozen_announcements = freeze(announcements_path, output.parent / "frozen-inputs")
        sources.append({"role": "announcements", "path": str(frozen_announcements.resolve()),
                        "contentHash": file_hash(frozen_announcements)})
    packet = rebuild_scan(tuple(sorted((s["role"], s["path"], s["contentHash"]) for s in sources)))
    if packet["dataAsOf"] != trading_day:
        raise ValueError("supplier returned no current trading-day observations; keep prior publication")
    packet["provenance"] = sources
    # Keep canonical fields exactly as rebuilt so tomorrow's baseline can be
    # admitted again. Each retained input already carries its own source date.
    write_json(output, packet)
    return packet


def refresh_announcements(*, baseline: Path, announcements_path: Path,
                          archive_root: Path, output: Path) -> dict:
    """Advance verified disclosure evidence while retaining the actual quote day."""
    load_research_scan(baseline, limit=1)
    prior = json.loads(baseline.read_text(encoding="utf-8"))
    evidence = json.loads(announcements_path.read_text(encoding="utf-8"))
    with closing(connect(archive_root / "meta.sqlite")) as con:
        archive = ForwardArchive(con, archive_root)
        universe, _, _ = verified_organization_map(
            ROOT / "data/s2-structured/universe-orgids.json", archive)
        verify_announcements(evidence, universe, archive, con)
    frozen = freeze(announcements_path, output.parent / "frozen-inputs")
    sources = [s for s in prior["provenance"] if s["role"] != "announcements"]
    sources.append({"role": "announcements", "path": str(frozen.resolve()), "contentHash": file_hash(frozen)})
    packet = rebuild_scan(tuple(sorted((s["role"], s["path"], s["contentHash"]) for s in sources)))
    packet["provenance"] = sources
    write_json(output, packet)
    return packet


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", type=Path, required=True)
    ap.add_argument("--quotes", type=Path)
    ap.add_argument("--announcements", type=Path)
    ap.add_argument("--archive-root", type=Path, default=ROOT / "deploy/agentctl-q0/forward-archive")
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--trading-day")
    a = ap.parse_args()
    if a.quotes is not None:
        if not a.trading_day:
            ap.error("--quotes requires --trading-day")
        p = refresh(baseline=a.baseline, quotes_path=a.quotes, archive_root=a.archive_root,
                    output=a.output, trading_day=a.trading_day, announcements_path=a.announcements)
    elif a.announcements is not None:
        p = refresh_announcements(baseline=a.baseline, announcements_path=a.announcements,
                                 archive_root=a.archive_root, output=a.output)
    else:
        ap.error("provide --quotes or --announcements")
    print(json.dumps({"snapshotId": p["snapshotId"], "dataAsOf": p["dataAsOf"],
                      "poolSize": p["poolSize"], "rankedCount": p["rankedCount"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
