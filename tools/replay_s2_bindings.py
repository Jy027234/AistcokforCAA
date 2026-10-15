"""离线恢复中断的 S2 公告绑定；缺页不会触发联网或伪造新收据。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.cninfo import CninfoClient, FetchOutcome
from aquant.domain.data.db import connect
from aquant.domain.data.forward_archive import ForwardArchive
from bind_structured_to_announcements import (
    bind_record, _verified_trading_days, verified_organization_map, attach_alias_evidence,
    receipt_observed_at,
)


class ArchivedCninfoClient(CninfoClient):
    """复用原客户端的身份、分页及解析契约，仅把传输替换成归档读取。"""

    def __init__(self, archive: ForwardArchive):
        super().__init__(archive)
        self.responses = {}
        for receipt in archive.receipts_for("cninfo"):
            if (receipt["outcome"] == "OK" and receipt["http_status"] == 200
                    and receipt["content_hash"]):
                key = (receipt["url"], receipt["detail"])
                previous = self.responses.get(key)
                if previous is None or (receipt["requested_at"], receipt["receipt_id"]) > (
                        previous["requested_at"], previous["receipt_id"]):
                    self.responses[key] = receipt

    def _fetch(self, url: str, *, label: str, data: bytes | None = None,
               content_type=None, max_bytes=None) -> FetchOutcome:
        receipt = self.responses.get((url, label))
        if receipt is None:
            return FetchOutcome(False, None, "", None, "ARCHIVED_INDEX_PAGE_MISSING")
        if data is not None and "request=" in label:
            expected = "sha256:" + hashlib.sha256(data).hexdigest()
            if label.rsplit("request=", 1)[-1] != expected or not self.archive.verify(expected):
                return FetchOutcome(False, None, receipt["receipt_id"], None,
                                    "ARCHIVED_REQUEST_HASH_INVALID")
        digest = receipt["content_hash"]
        if not self.archive.verify(digest):
            return FetchOutcome(False, None, receipt["receipt_id"], digest,
                                "ARCHIVED_RESPONSE_HASH_INVALID")
        return FetchOutcome(True, self.archive.load_bytes(digest),
                            receipt["receipt_id"], digest, None, receipt["http_status"])


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=1, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path,
                        default=ROOT / "deploy/agentctl-q0/forward-archive")
    parser.add_argument("--org-map", type=Path,
                        default=ROOT / "data/s2-structured/universe-orgids.json")
    parser.add_argument("--status", action="append", default=[])
    parser.add_argument("--pairs-file", type=Path, default=None,
                        help="JSON records/results with instrument_id and period_end; replay only these exact pairs")
    parser.add_argument("--include-alias-records", action="store_true",
                        help="also repair already-bound/missing records for verified same-issuer code aliases")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, default=None)
    args = parser.parse_args(argv)
    base = json.loads(args.base.read_text(encoding="utf-8"))
    statuses = set(args.status or ["INDEX_INCOMPLETE", "REVISION_REVIEW_REQUIRED"])
    records = base["records"]
    pairs = None
    if args.pairs_file:
        packet = json.loads(args.pairs_file.read_text(encoding="utf-8"))
        selected = packet if isinstance(packet, list) else packet.get("results", packet.get("records", []))
        pairs = {(str(row["instrument_id"]), str(row["period_end"])) for row in selected}
        if not pairs:
            raise SystemExit("pairs-file contains no exact pairs")
        known = {(row["instrument_id"], row["period_end"]) for row in records}
        if not pairs <= known:
            raise SystemExit("pairs-file contains pairs absent from the base binding")
    checkpoint = args.checkpoint or args.output.with_suffix(".checkpoint.jsonl")
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    changed = []
    started = datetime.now(timezone.utc)
    with closing(connect(args.archive_root / "meta.sqlite")) as con:
        archive = ForwardArchive(con, args.archive_root)
        orgs, groups, provenance = verified_organization_map(args.org_map, archive)
        calendar = _verified_trading_days(args.calendar, archive)
        client = ArchivedCninfoClient(archive)
        # 每次重放重新核验原始字节；checkpoint只是恢复证据，不是可信准入来源。
        with checkpoint.open("w", encoding="utf-8") as stream:
            for index, row in enumerate(records):
                if pairs is not None and (row["instrument_id"], row["period_end"]) not in pairs:
                    continue
                organization = orgs.get(row["instrument_id"])
                aliases = groups.get(organization, ())
                if row.get("status") not in statuses and not (
                        args.include_alias_records and len(aliases) > 1):
                    continue
                result = client.report_index(stock_code=row["instrument_id"],
                    report_period=row["period_end"],
                    organization_id=organization, security_aliases=aliases)
                entry = bind_record(row, result, calendar, archive)
                attach_alias_evidence(entry, result, groups, provenance)
                entry["replayed_from_status"] = row["status"]
                records[index] = entry
                changed.append(entry)
                stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
                stream.flush()
                if len(changed) % 100 == 0:
                    print(f"replayed {len(changed)} pairs", flush=True)
        observations_refreshed = 0
        for row in records:
            if row.get("status") != "RECONSTRUCTED_READY":
                continue
            observed = receipt_observed_at(row, archive)
            if observed is None:
                row["status"] = "SOURCE_OBSERVATION_UNRESOLVED"
                row["reason"] = "required source receipt is missing"
                row.pop("observed_at", None)
            else:
                row["observed_at"] = observed
                observations_refreshed += 1
    report = dict(base, records=records,
        counts=dict(sorted(Counter(row["status"] for row in records).items())),
        generated_at=datetime.now(timezone.utc).isoformat(),
        replay={"base": str(args.base.resolve()), "network_requests": 0,
                "started_at": started.isoformat(), "pairs_replayed": len(changed),
                "statuses": sorted(statuses),
                "pairs_file": str(args.pairs_file.resolve()) if args.pairs_file else None,
                "include_alias_records": args.include_alias_records,
                "observations_refreshed": observations_refreshed,
                "result_counts": dict(sorted(Counter(row["status"] for row in changed).items())),
                "checkpoint": str(checkpoint.resolve())},
        boundary="Historical reconstruction for research. Revised values are admitted only "
                 "after an archived selected-report PDF matches all five verified fields.")
    if base.get("replay"):
        report["replay_history"] = [*base.get("replay_history", []), base["replay"]]
    write_report(args.output, report)
    print(json.dumps(report["replay"], ensure_ascii=False))
    print(json.dumps(report["counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
