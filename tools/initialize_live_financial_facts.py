"""Copy existing LIVE_OBSERVED facts and their original proofs to a live-only log.

The old mixed log is retained. Source/PIT mode is not part of its unique root
key, so an independently verified official live value cannot be appended beside
an existing reconstructed root. Separate logs preserve both proven histories
without inventing a supersedes relationship between unrelated evidence paths.
"""
from __future__ import annotations
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from aquant.domain.data.db import connect
from aquant.domain.data.pit import PitMode
from aquant.domain.fundamentals.fact_repository import FactReviewBundle, FinancialFactRepository


def initialize_live_repository(*, source: Path, destination: Path) -> dict:
    if source.resolve() == destination.resolve():
        raise ValueError("live-only destination must differ from the retained mixed source")
    old = FinancialFactRepository.open_existing(source)
    original, accepted = old.load_with_accepted_s2_pdf_reviews()
    facts = [fact for fact in original.facts if fact.pit_mode is PitMode.LIVE_OBSERVED]
    by_id = {fact.version_id: fact for fact in facts}
    with closing(connect(source, read_only=True)) as con:
        con.execute("BEGIN")
        reviews = [dict(row) for row in con.execute("SELECT * FROM financial_fact_review")]
        members = {}
        for row in con.execute("SELECT bundle_id,version_id FROM financial_fact_review_member"):
            members.setdefault(row["bundle_id"], set()).add(row["version_id"])
        con.execute("COMMIT")
    copied = []
    for review in reviews:
        version_ids = members.get(review["bundle_id"], set())
        if not version_ids.intersection(by_id):
            continue
        if not version_ids <= by_id.keys():
            raise ValueError("source proof cohort mixes live and reconstructed facts")
        copied.append((review, version_ids))
    target = FinancialFactRepository(destination)
    if any(fact.pit_mode is not PitMode.LIVE_OBSERVED for fact in target.load().facts):
        raise ValueError("live-only destination already contains another PIT mode")
    inserted = target.append_many(facts)
    for review, version_ids in copied:
        bundle = FactReviewBundle(review["bundle_id"], review["evidence_json"])
        if bundle.evidence_hash != review["evidence_hash"]:
            raise ValueError("source review hash differs from its canonical original evidence")
        target.append_many([by_id[version] for version in sorted(version_ids)], review_bundle=bundle)
    after, after_accepted = target.load_with_accepted_s2_pdf_reviews()
    inherited = {fact.version_id: fact for fact in after.facts if fact.version_id in by_id}
    if inherited != by_id:
        raise ValueError("live-only inheritance changed an original fact")
    if any(after_accepted.get(version) != accepted.get(version) for version in by_id):
        raise ValueError("live-only inheritance changed an original review acceptance")
    return {"schema": "aquant.live-financial-repository-inheritance.v1",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "sourceDb": str(source.resolve()), "destinationDb": str(destination.resolve()),
            "inheritedFactCount": len(facts), "newlyInsertedFactCount": inserted,
            "inheritedReviewCount": len(copied),
            "inheritedAcceptedFactCount": sum(version in after_accepted for version in by_id),
            "factAndReviewIdentityPreserved": True, "originalTimestampsPreserved": True,
            "excludedReconstructedFactCount": len(original.facts) - len(facts),
            "note": "Separate live evidence log; old mixed log and historical roots retained; no fabricated revisions."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data/s2-pdf-review/s2-machine-verified-facts.sqlite")
    parser.add_argument("--destination", type=Path, default=ROOT / "data/s2-pdf-review/s2-live-observed-facts.sqlite")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = initialize_live_repository(source=args.source, destination=args.destination)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
