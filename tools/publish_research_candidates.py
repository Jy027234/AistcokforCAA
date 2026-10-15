"""Build a read-only named candidate list from a frozen, verified S2 research trial."""
from __future__ import annotations
import argparse
import json
import os
import sys
from contextlib import closing
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from aquant.domain.data.db import connect
from aquant.domain.data.forward_archive import ForwardArchive
from aquant.domain.fundamentals.fact_repository import FinancialFactRepository
from aquant.operations.decision_market_caps import load_sidecar
from aquant.operations.research_candidates import build_research_candidates, file_hash
from bind_structured_to_announcements import verified_organization_map


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--factor", type=Path, required=True)
    p.add_argument("--bound", type=Path, required=True)
    p.add_argument("--org-map", type=Path, default=ROOT / "data/s2-structured/universe-orgids.json")
    p.add_argument("--industries", type=Path, default=ROOT / "configs/real-pool-csrc.yaml")
    p.add_argument("--archive-root", type=Path, default=ROOT / "deploy/agentctl-q0/forward-archive")
    p.add_argument("--output", type=Path, default=ROOT / "data/s2-structured/research-candidates.json")
    args = p.parse_args(argv)
    report = json.loads(args.factor.read_text(encoding="utf-8"))
    bound = json.loads(args.bound.read_text(encoding="utf-8"))
    capfile = Path(report["market_cap_source"]["path"])
    sidecar = load_sidecar(capfile, expected_as_of=report["trading_day"], archive_root=args.archive_root)
    if (sidecar.receipt_id != report["market_cap_source"]["receipt_id"]
            or sidecar.content_hash != report["market_cap_source"]["content_hash"]):
        raise ValueError("market-cap report provenance differs from the verified sidecar")
    store, _ = FinancialFactRepository.open_existing(Path(report["facts_db"])).load_with_accepted_s2_pdf_reviews()
    with closing(connect(args.archive_root / "meta.sqlite")) as con:
        archive = ForwardArchive(con, args.archive_root)
        _, _, orgproof = verified_organization_map(args.org_map, archive)
        name_path = args.archive_root / con.execute(
            "SELECT stored_path FROM raw_artifact WHERE content_hash=?", (orgproof["content_hash"],)).fetchone()[0]
        names = {row["code"]: row.get("zwjc", row["code"]) for row in
                 json.loads(archive.load_bytes(orgproof["content_hash"]))["stockList"]}
    import yaml
    industry_packet = yaml.safe_load(args.industries.read_text(encoding="utf-8"))
    industries = {str(row["code"])[-6:]: str(row.get("industry") or "")
                  for row in industry_packet["instruments"]}
    # The daily pipeline replaces its active sidecar. Freeze an independent
    # byte-identical copy so an ordinary next-day refresh cannot erase history.
    frozen_dir = args.output.parent / "frozen-inputs"
    frozen_dir.mkdir(parents=True, exist_ok=True)
    frozen_cap = frozen_dir / (f"market-caps-{sidecar.market_cap_as_of}-" + file_hash(capfile)[7:23] + ".json")
    if frozen_cap.exists():
        if file_hash(frozen_cap) != file_hash(capfile):
            raise ValueError("frozen market-cap copy has changed")
    else:
        temp_cap = frozen_cap.with_name(f".{frozen_cap.name}.{uuid4().hex}.tmp")
        try:
            temp_cap.write_bytes(capfile.read_bytes())
            os.replace(temp_cap, frozen_cap)
        finally:
            if temp_cap.exists(): temp_cap.unlink()
    sources = [("factor_report", args.factor), ("bound_records", args.bound),
               ("market_caps", frozen_cap), ("organization_map", args.org_map), ("company_names", name_path),
               ("industry_snapshot", args.industries), ("facts_db", Path(report["facts_db"]))]
    provenance = [{"role": role, "path": str(path.resolve()), "contentHash": file_hash(path)}
                  for role, path in sources]
    if args.output.resolve() in {Path(item["path"]) for item in provenance}:
        raise ValueError("candidate output cannot overwrite a source artifact")
    report["source_path"] = str(args.factor.resolve())
    packet = build_research_candidates(report, bound, facts={f.version_id: f for f in store.facts},
                                       names=names, industries=industries, provenance=provenance)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temp = args.output.with_name(f".{args.output.name}.{uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(packet, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        os.replace(temp, args.output)
    finally:
        if temp.exists():
            temp.unlink()
    print(f"{args.output}: {packet['rankedCount']} named candidates; dataAsOf={packet['dataAsOf']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
