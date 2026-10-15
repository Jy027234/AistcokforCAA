"""Append an independently replayed proof without replacing facts or old reviews."""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from ...artifact_paths import resolve_archive_member, resolve_artifact_path
from ..data.db import connect, write_tx

SCHEMA = "cninfo-s2-machine-proof-supplement-v1"
STORAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS financial_fact_proof_supplement (
    supplement_id TEXT PRIMARY KEY,
    original_bundle_id TEXT NOT NULL UNIQUE REFERENCES financial_fact_review(bundle_id),
    evidence_hash TEXT NOT NULL UNIQUE,
    evidence_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS financial_fact_proof_supplement_no_update
BEFORE UPDATE ON financial_fact_proof_supplement BEGIN
    SELECT RAISE(ABORT, 'financial proof supplements are append-only');
END;
CREATE TRIGGER IF NOT EXISTS financial_fact_proof_supplement_no_delete
BEFORE DELETE ON financial_fact_proof_supplement BEGIN
    SELECT RAISE(ABORT, 'financial proof supplements are append-only');
END;
"""
TRIGGERS = frozenset({"financial_fact_proof_supplement_no_update",
                      "financial_fact_proof_supplement_no_delete"})


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(body):
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _now():
    return datetime.now(timezone.utc)


def _stamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("proof completion must have an explicit timezone")
    return stamp


def facts_digest(member_ids, facts):
    from .fact_repository import _encode
    return digest(canonical([_encode(facts[i]) for i in sorted(member_ids)]).encode())


def _original_pdf(archive, original, facts, member_ids):
    """Check current SQL and bytes, even when a numerical replay is cached."""
    receipt_id = original["pdf_receipt_id"]
    if archive["pdf_receipt_id"] != receipt_id or archive["document_url"] != original["document_url"]:
        raise ValueError("supplement archive differs from the original report")
    with closing(connect(resolve_artifact_path(archive["database_path"]), read_only=True)) as con:
        receipt = con.execute("SELECT * FROM fetch_receipt WHERE receipt_id=?", (receipt_id,)).fetchone()
        raw = con.execute("SELECT * FROM raw_artifact WHERE content_hash=?", (original["content_hash"],)).fetchone()
    if receipt is None or raw is None:
        raise ValueError("supplement original receipt or raw artifact is missing")
    url = urlsplit(receipt["url"])
    if (receipt["source_id"] != "cninfo" or receipt["outcome"] != "OK" or receipt["http_status"] != 200
            or receipt["url"] != archive["document_url"] or url.hostname != "static.cninfo.com.cn"
            or url.scheme not in ("http", "https") or url.username or url.password
            or url.path.rsplit("/", 1)[-1].lower() != original["announcement_id"] + ".pdf"
            or receipt["content_hash"] != original["content_hash"]):
        raise ValueError("supplement original receipt identity is inconsistent")
    root = resolve_artifact_path(archive["root"]).resolve()
    try:
        path = resolve_archive_member(root, raw["stored_path"])
    except ValueError as exc:
        raise ValueError("supplement raw path escapes its archive") from exc
    body = path.read_bytes()
    if (not body.startswith(b"%PDF-") or digest(body) != original["content_hash"]
            or len(body) != receipt["byte_size"] or len(body) != raw["byte_size"]):
        raise ValueError("supplement original bytes do not match their receipt")
    for i in member_ids:
        fact = facts[i]
        if (fact.content_hash != receipt["content_hash"] or fact.source_document_id != original["announcement_id"]
                or fact.first_seen_at != _stamp(receipt["first_seen_at"])):
            raise ValueError("supplement fact differs from its original observation")
    return body


@lru_cache(maxsize=8)
def _rederive_source(body, instrument_id, company, period_end, announcement_id,
                     document_url, fields):
    """Cache only immutable results from the exact original bytes in memory.

    The caller must verify the current receipt and raw bytes before every call.
    Bytes, issuer, period, announcement, URL and complete field scope form the
    key. No persisted proof JSON is a cache input or substitutes for a replay.
    """
    from ...adapters.providers.cninfo_statement_sections import extract_section_facts
    from ...adapters.providers.s2_machine_verification import verify_machine_evidence
    candidate = extract_section_facts(body, instrument_id=instrument_id,
        company=company, period_end=date.fromisoformat(period_end),
        announcement_id=announcement_id, document_url=document_url,
        required_fields=fields)
    verification = verify_machine_evidence(body, candidate=candidate, expected_fields=fields)
    if not verification.verified:
        raise ValueError("new original-byte proof is incomplete")
    # Values and canonical proof strings are immutable; each caller receives a
    # fresh dictionary, so mutating a returned proof cannot poison the cache.
    return tuple((field,candidate.by_field[field].value_yuan) for field in fields), canonical(verification.as_evidence())


def _rederive(body, original, facts, member_ids):
    fields = tuple(sorted(facts[i].metric for i in member_ids))
    amounts, proof = _rederive_source(body, original["instrument_id"], original["company"],
        original["period_end"], original["announcement_id"], original["document_url"], fields)
    values = dict(amounts)
    if any(values[facts[i].metric] != facts[i].value for i in member_ids):
        raise ValueError("a proof supplement cannot change a financial amount")
    return json.loads(proof)


def _calendar(path, expected_hash, archive, completed_at):
    from ...adapters.providers.s2_calendar_evidence import verify_calendar_evidence
    from ..data.forward_archive import ForwardArchive
    body = resolve_artifact_path(path).read_bytes()
    if digest(body) != expected_hash:
        raise ValueError("supplement calendar bytes changed")
    with closing(connect(resolve_artifact_path(archive["database_path"]), read_only=True)) as con:
        source = ForwardArchive(con, resolve_artifact_path(archive["root"]))
        days, _ = verify_calendar_evidence(json.loads(body), source, now=completed_at)
    return days


def _available(completed_at, facts, member_ids, days):
    from ..data.pit import preopen_instant
    floor = max([completed_at] + [facts[i].available_at for i in member_ids])
    return next((preopen_instant(day) for day in days if preopen_instant(day) >= floor), None)


def validate(row, originals, members, facts):
    """Return a validated whole-cohort proof and its effective availability."""
    from .fact_repository import MACHINE_VERIFICATION_SCHEMA, _accepted_machine_verified_review
    value = json.loads(row["evidence_json"])
    if (canonical(value) != row["evidence_json"] or digest(row["evidence_json"].encode()) != row["evidence_hash"]
            or value["schema"] != SCHEMA or row["supplement_id"] != "cninfo_proof_supplement_" + row["evidence_hash"][7:]):
        raise ValueError("proof supplement canonical identity is invalid")
    original_id = value["original_bundle_id"]
    base = originals[original_id]
    cohort = members[original_id]
    if (row["original_bundle_id"] != original_id or not cohort or not cohort <= facts.keys()
            or value["original_evidence_hash"] != base["evidence_hash"]
            or digest(base["evidence_json"].encode()) != base["evidence_hash"]
            or value["version_ids"] != sorted(cohort)
            or value["original_facts_hash"] != facts_digest(cohort, facts)):
        raise ValueError("proof supplement does not bind its complete immutable cohort")
    original = json.loads(base["evidence_json"])
    replacement = value["replacement_evidence"]
    mutable_proof_keys = {"fields", "sections", "identities", "geometry_archive"}
    if (original["schema"] != MACHINE_VERIFICATION_SCHEMA
            or {k:v for k,v in original.items() if k not in mutable_proof_keys} !=
               {k:v for k,v in replacement.items() if k not in mutable_proof_keys}):
        raise ValueError("proof supplement changed original fact or source metadata")
    completed = _stamp(value["completed_at"])
    ingested = _stamp(value["ingested_at"])
    if (completed > ingested or ingested > _now()
            or any(completed < facts[i].ingested_at for i in cohort)):
        raise ValueError("proof completion is not a real later observation")
    archive = value["archive"]
    body = _original_pdf(archive, original, facts, cohort)
    replay = _rederive(body, original, facts, cohort)
    if any(replacement[key] != replay[key] for key in ("fields", "sections", "identities")):
        raise ValueError("proof supplement differs from independent original-byte replay")
    if replacement.get("geometry_archive") is not None and replacement["geometry_archive"] != archive:
        raise ValueError("new geometry path differs from the verified original archive")
    payload = canonical(replacement)
    proof = {"bundle_id": original_id, "evidence_json": payload, "evidence_hash": digest(payload.encode())}
    if not _accepted_machine_verified_review(proof, cohort, facts):
        raise ValueError("new independent proof does not pass the unchanged machine gate")
    days = _calendar(value["calendar_path"], value["calendar_hash"], archive, ingested)
    available = _available(ingested, facts, cohort, days)
    if available is None or _stamp(value["available_at"]) != available:
        raise ValueError("proof supplement availability does not follow its actual completion")
    return original_id, cohort, available


def append(repository, *, original_bundle_id, replacement_evidence, archive, calendar_path):
    """Recompute before recording completion; write only a new immutable proof."""
    from .fact_repository import _SELECT, _decode, _accepted_machine_verified_review
    if repository._read_only:
        raise RuntimeError("financial fact repository was opened read-only")
    with closing(connect(repository.db_path, read_only=True)) as con:
        base = con.execute("SELECT * FROM financial_fact_review WHERE bundle_id=?", (original_bundle_id,)).fetchone()
        ids = {r[0] for r in con.execute("SELECT version_id FROM financial_fact_review_member WHERE bundle_id=?", (original_bundle_id,))}
        facts = {r["version_id"]:_decode(r) for r in con.execute(_SELECT) if r["version_id"] in ids}
        existing = con.execute("SELECT * FROM financial_fact_proof_supplement WHERE original_bundle_id=?",
                               (original_bundle_id,)).fetchone()
    if base is None or not ids or ids != facts.keys():
        raise ValueError("original proof cohort is missing")
    if existing is not None:
        saved = json.loads(existing["evidence_json"])
        if (saved["replacement_evidence"] != replacement_evidence or saved["archive"] != archive
                or saved["calendar_path"] != str(Path(calendar_path).resolve())):
            raise ValueError("immutable proof supplement retry differs")
        validate(existing, {original_bundle_id:base}, {original_bundle_id:ids}, facts)
        return dict(existing)
    if _accepted_machine_verified_review(base, ids, facts):
        raise ValueError("an already valid proof does not require a supplement")
    original = json.loads(base["evidence_json"])
    body = _original_pdf(archive, original, facts, ids)
    replay = _rederive(body, original, facts, ids)
    if any(replacement_evidence[k] != replay[k] for k in ("fields", "sections", "identities")):
        raise ValueError("candidate supplement does not match current original-byte verification")
    completed = _now()
    calendar_path = str(Path(calendar_path).resolve())
    calendar_hash = digest(Path(calendar_path).read_bytes())
    available = _available(completed, facts, ids, _calendar(calendar_path, calendar_hash, archive, completed))
    if available is None:
        raise ValueError("calendar has no legal future proof availability")
    value = {"schema":SCHEMA, "original_bundle_id":original_bundle_id,
        "original_evidence_hash":base["evidence_hash"], "original_facts_hash":facts_digest(ids,facts),
        "version_ids":sorted(ids), "completed_at":completed.isoformat(), "ingested_at":completed.isoformat(),
        "available_at":available.isoformat(),
        "archive":archive, "calendar_path":calendar_path, "calendar_hash":calendar_hash,
        "replacement_evidence":replacement_evidence}
    payload = canonical(value); evidence_hash = digest(payload.encode())
    row = {"supplement_id":"cninfo_proof_supplement_"+evidence_hash[7:], "original_bundle_id":original_bundle_id,
           "evidence_hash":evidence_hash, "evidence_json":payload}
    validate(row, {original_bundle_id:base}, {original_bundle_id:ids}, facts)
    # Final source verification can cross the pre-open boundary. Record proof
    # ingestion afterwards and recompute the floor before the short SQL write.
    calendar_check = _now()
    days = _calendar(calendar_path, calendar_hash, archive, calendar_check)
    ingested = _now()
    market_tz = timezone(timedelta(hours=8))
    if calendar_check.astimezone(market_tz).date() != ingested.astimezone(market_tz).date():
        raise ValueError("calendar verification crossed its source observation day; refresh evidence")
    available = _available(ingested, facts, ids, days)
    if available is None:
        raise ValueError("calendar has no post-ingestion proof availability")
    value["ingested_at"] = ingested.isoformat()
    value["available_at"] = available.isoformat()
    payload = canonical(value); evidence_hash = digest(payload.encode())
    row.update(supplement_id="cninfo_proof_supplement_"+evidence_hash[7:],
               evidence_hash=evidence_hash, evidence_json=payload)
    with closing(connect(repository.db_path)) as con, write_tx(con):
        existing = con.execute("SELECT * FROM financial_fact_proof_supplement WHERE original_bundle_id=?", (original_bundle_id,)).fetchone()
        if existing is not None:
            if (existing["evidence_json"] == payload and existing["evidence_hash"] == evidence_hash):
                return dict(existing)
            raise ValueError("immutable proof supplement already exists for this cohort")
        con.execute("INSERT INTO financial_fact_proof_supplement VALUES (?,?,?,?)",
                    tuple(row[k] for k in ("supplement_id","original_bundle_id","evidence_hash","evidence_json")))
    return row
