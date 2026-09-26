"""Durable, append-only storage for :class:`FinancialFact` versions.

This repository uses its own SQLite file.  It deliberately does not run the
application's ``meta.sqlite`` migrations: a caller can populate it from a
verified announcement extractor and replay the exact facts into the existing
PIT selector after a restart.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Mapping

from ..data.db import connect, write_tx
from .versioned import FinancialFact, VersionedFinancialFactStore


_SCHEMA = """
CREATE TABLE IF NOT EXISTS financial_fact (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id TEXT NOT NULL UNIQUE,
    instrument_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    period_end TEXT NOT NULL,
    statement_scope TEXT NOT NULL,
    profit_scope TEXT,
    value_kind TEXT NOT NULL,
    value_text TEXT,
    currency TEXT NOT NULL,
    raw_unit TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_document_id TEXT NOT NULL,
    source_published_date TEXT,
    source_published_at TEXT,
    timestamp_precision TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    availability_basis TEXT NOT NULL,
    pit_mode TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    supersedes_id TEXT UNIQUE REFERENCES financial_fact(version_id)
);
CREATE INDEX IF NOT EXISTS idx_financial_fact_logical
    ON financial_fact (instrument_id, metric, period_end);
CREATE UNIQUE INDEX IF NOT EXISTS idx_financial_fact_root
    ON financial_fact (instrument_id, metric, period_end, statement_scope,
                       coalesce(profit_scope, ''), currency, raw_unit)
    WHERE supersedes_id IS NULL;
CREATE TRIGGER IF NOT EXISTS financial_fact_no_update
BEFORE UPDATE ON financial_fact BEGIN
    SELECT RAISE(ABORT, 'financial facts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS financial_fact_no_delete
BEFORE DELETE ON financial_fact BEGIN
    SELECT RAISE(ABORT, 'financial facts are append-only');
END;
CREATE TABLE IF NOT EXISTS financial_fact_review (
    bundle_id TEXT PRIMARY KEY,
    evidence_hash TEXT NOT NULL UNIQUE,
    evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS financial_fact_review_member (
    bundle_id TEXT NOT NULL REFERENCES financial_fact_review(bundle_id),
    version_id TEXT NOT NULL UNIQUE REFERENCES financial_fact(version_id),
    PRIMARY KEY (bundle_id, version_id)
);
CREATE TRIGGER IF NOT EXISTS financial_fact_review_no_update
BEFORE UPDATE ON financial_fact_review BEGIN
    SELECT RAISE(ABORT, 'financial fact reviews are append-only');
END;
CREATE TRIGGER IF NOT EXISTS financial_fact_review_no_delete
BEFORE DELETE ON financial_fact_review BEGIN
    SELECT RAISE(ABORT, 'financial fact reviews are append-only');
END;
CREATE TRIGGER IF NOT EXISTS financial_fact_review_member_no_update
BEFORE UPDATE ON financial_fact_review_member BEGIN
    SELECT RAISE(ABORT, 'financial fact reviews are append-only');
END;
CREATE TRIGGER IF NOT EXISTS financial_fact_review_member_no_delete
BEFORE DELETE ON financial_fact_review_member BEGIN
    SELECT RAISE(ABORT, 'financial fact reviews are append-only');
END;
"""

_COLUMNS = (
    "version_id", "instrument_id", "metric", "period_end", "statement_scope",
    "profit_scope", "value_kind", "value_text", "currency", "raw_unit",
    "source_id", "source_document_id", "source_published_date",
    "source_published_at", "timestamp_precision", "first_seen_at",
    "ingested_at", "available_at", "availability_basis", "pit_mode",
    "content_hash", "supersedes_id",
)
_SELECT = "SELECT " + ", ".join(_COLUMNS) + " FROM financial_fact"
_INSERT = ("INSERT INTO financial_fact (" + ", ".join(_COLUMNS) + ") VALUES ("
           + ", ".join("?" for _ in _COLUMNS) + ")")


def _value_to_text(value: object) -> tuple[str, str | None]:
    if value is None:
        return "none", None
    if isinstance(value, Decimal):
        return "decimal", str(value)
    if isinstance(value, bool):
        return "bool", "1" if value else "0"
    if isinstance(value, int):
        return "int", str(value)
    if isinstance(value, float):
        return "float", repr(value)
    if isinstance(value, str):
        return "str", value
    raise TypeError(f"unsupported financial fact value type: {type(value).__name__}")


def _value_from_text(kind: str, text: str | None) -> Decimal | int | float | str | None:
    if kind == "none" and text is None:
        return None
    if text is None:
        raise ValueError(f"financial fact {kind!r} value has no text")
    if kind == "decimal":
        return Decimal(text)
    if kind == "bool":
        if text not in ("0", "1"):
            raise ValueError("invalid financial fact bool value")
        return bool(int(text))
    if kind == "int":
        return int(text)
    if kind == "float":
        return float(text)
    if kind == "str":
        return text
    raise ValueError(f"unknown financial fact value kind {kind!r}")


def _encode(fact: FinancialFact) -> tuple[object, ...]:
    kind, value = _value_to_text(fact.value)
    return (
        fact.version_id, fact.instrument_id, fact.metric, fact.period_end.isoformat(),
        fact.statement_scope.value,
        fact.profit_scope.value if fact.profit_scope is not None else None,
        kind, value, fact.currency, fact.raw_unit, fact.source_id,
        fact.source_document_id,
        fact.source_published_date.isoformat() if fact.source_published_date else None,
        fact.source_published_at.isoformat() if fact.source_published_at else None,
        fact.timestamp_precision.value, fact.first_seen_at.isoformat(),
        fact.ingested_at.isoformat(), fact.available_at.isoformat(),
        fact.availability_basis.value, fact.pit_mode.value, fact.content_hash,
        fact.supersedes_id,
    )


def _decode(row: sqlite3.Row) -> FinancialFact:
    return FinancialFact(
        version_id=row["version_id"], instrument_id=row["instrument_id"],
        metric=row["metric"], period_end=date.fromisoformat(row["period_end"]),
        statement_scope=row["statement_scope"], profit_scope=row["profit_scope"],
        value=_value_from_text(row["value_kind"], row["value_text"]),
        currency=row["currency"], raw_unit=row["raw_unit"],
        source_id=row["source_id"], source_document_id=row["source_document_id"],
        source_published_date=(date.fromisoformat(row["source_published_date"])
                               if row["source_published_date"] else None),
        source_published_at=(datetime.fromisoformat(row["source_published_at"])
                             if row["source_published_at"] else None),
        timestamp_precision=row["timestamp_precision"],
        first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
        ingested_at=datetime.fromisoformat(row["ingested_at"]),
        available_at=datetime.fromisoformat(row["available_at"]),
        availability_basis=row["availability_basis"], pit_mode=row["pit_mode"],
        content_hash=row["content_hash"], supersedes_id=row["supersedes_id"],
    )


@dataclass(frozen=True, slots=True)
class FactReviewBundle:
    """Canonical, immutable evidence for one atomic reviewed report bundle."""

    bundle_id: str
    evidence_json: str

    @property
    def evidence_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.evidence_json.encode("utf-8")).hexdigest()


_S2_PDF_FIELDS = frozenset({
    "net_profit_attributable", "net_profit_consolidated", "operating_cashflow",
    "revenue", "parent_equity",
})
_IMMUTABLE_REVIEW_TRIGGERS = frozenset({
    "financial_fact_no_update", "financial_fact_no_delete",
    "financial_fact_review_no_update", "financial_fact_review_no_delete",
    "financial_fact_review_member_no_update",
    "financial_fact_review_member_no_delete",
})


def _accepted_s2_pdf_review(
    row: sqlite3.Row, member_ids: set[str], facts: Mapping[str, FinancialFact],
) -> bool:
    """Recognize the immutable five-field proof written by PDF promotion."""

    try:
        evidence_json = row["evidence_json"]
        evidence = json.loads(evidence_json)
        if not isinstance(evidence, dict):
            return False
        canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
        if canonical != evidence_json or row["evidence_hash"] != (
            "sha256:" + hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
        ):
            return False
        if (evidence.get("schema") != "cninfo-s2-pdf-review-v1" or
                evidence.get("source_id") != "cninfo" or
                evidence.get("complete_search_attested") is not True or
                evidence.get("index_query", {}).get("request_body_verified") is not True):
            return False
        version_ids = evidence.get("version_ids")
        if (not isinstance(version_ids, list) or len(version_ids) != 5 or
                len(set(version_ids)) != 5 or set(version_ids) != member_ids or
                not member_ids <= facts.keys()):
            return False
        iid = evidence["instrument_id"]
        period = date.fromisoformat(evidence["period_end"])
        announcement = evidence["announcement_id"]
        content_hash = evidence["content_hash"]
        if (row["bundle_id"] != "cninfo_pdf_" + hashlib.sha256(
            f"{iid}|{period}|{announcement}".encode()
        ).hexdigest()[:32] or evidence.get("announcement_role") not in
                ("ORIGINAL_REPORT", "REVISED_REPORT") or
                not all(evidence.get(key) for key in (
                    "rights_register_version", "pdf_receipt_id",
                    "org_lookup_receipt_id", "version_reviewer_id",
                    "version_reviewed_at", "available_at", "first_seen_at",
                    "ingested_at", "source_published_date"))):
            return False
        cross = evidence.get("cross_category_index")
        if (not isinstance(cross, dict) or not cross.get("reviewer_id") or
                not cross.get("reviewed_at") or
                not isinstance(cross.get("candidate_dispositions"), list)):
            return False
        reviews = evidence.get("field_reviews")
        if (not isinstance(reviews, list) or len(reviews) != 5 or
                {review.get("field") for review in reviews if isinstance(review, dict)}
                != _S2_PDF_FIELDS):
            return False
        by_metric = {facts[version_id].metric: facts[version_id]
                     for version_id in member_ids}
        if set(by_metric) != _S2_PDF_FIELDS:
            return False
        for field, fact in by_metric.items():
            identity = (f"cninfo|{iid}|{period}|{field}|{announcement}|{content_hash}")
            expected_id = "pdf_" + hashlib.sha256(identity.encode()).hexdigest()[:32]
            expected_scope = ("ATTRIBUTABLE" if field == "net_profit_attributable" else
                              "CONSOLIDATED" if field == "net_profit_consolidated" else None)
            if (fact.version_id != expected_id or fact.instrument_id != iid or
                    fact.period_end != period or fact.source_id != "cninfo" or
                    fact.source_document_id != announcement or
                    fact.content_hash != content_hash or fact.currency != "CNY" or
                    fact.raw_unit != "yuan" or fact.statement_scope.value != "CONSOLIDATED" or
                    (fact.profit_scope.value if fact.profit_scope else None) != expected_scope or
                    fact.source_published_date.isoformat() != evidence["source_published_date"] or
                    fact.source_published_at is not None or
                    fact.timestamp_precision.value != "DATE" or
                    fact.first_seen_at.isoformat() != evidence["first_seen_at"] or
                    fact.ingested_at.isoformat() != evidence["ingested_at"] or
                    fact.available_at.isoformat() != evidence["available_at"] or
                    fact.availability_basis.value != "OBSERVED" or
                    fact.pit_mode.value != "LIVE_OBSERVED"):
                return False
        for review in reviews:
            fact = by_metric[review["field"]]
            if (review.get("method") != "HUMAN_VISUAL" or
                    not review.get("reviewer_id") or not review.get("reviewed_at") or
                    not isinstance(review.get("pdf_page"), int) or
                    review["pdf_page"] < 1 or
                    review.get("source_amount_unit") not in ("元", "千元") or
                    not review.get("current_cell") or not review.get("row_label") or
                    Decimal(review["value_yuan"]) != Decimal(str(fact.value))):
                return False
        return True
    except (KeyError, AttributeError, TypeError, ValueError, ArithmeticError):
        return False


class FinancialFactRepository:
    """Own an independent SQLite fact file and replay it into the PIT store.

    ``append_many`` is atomic and returns the number of newly inserted
    versions. An identical ``version_id`` retry returns zero; a changed retry
    raises ``ValueError``. Revisions may be supplied out of order within one
    batch, provided the complete chain is present and has one successor per
    version.
    """

    def __init__(self, db_path: str | Path, *, _create_schema: bool = True) -> None:
        if str(db_path) == ":memory:":
            raise ValueError("financial fact repository requires a persistent file")
        self.db_path = Path(db_path)
        self._read_only = not _create_schema
        if not _create_schema:
            if not self.db_path.is_file():
                raise FileNotFoundError(f"financial fact repository does not exist: {self.db_path}")
            try:
                with closing(connect(self.db_path, read_only=True)) as con:
                    con.execute(_SELECT + " LIMIT 0")
            except sqlite3.Error as exc:
                raise ValueError(
                    f"invalid financial fact repository schema: {self.db_path}: {exc}"
                ) from exc
            return
        with closing(connect(self.db_path)) as con:
            # executescript commits implicitly, so schema setup stays outside
            # the short append transaction. DDL is idempotent on restart.
            con.executescript(_SCHEMA)

    @classmethod
    def open_existing(cls, db_path: str | Path) -> FinancialFactRepository:
        """Open a validated fact file without creating a file or running DDL."""

        return cls(db_path, _create_schema=False)

    def append(self, fact: FinancialFact) -> bool:
        """Append one version; return whether it was newly inserted."""

        return bool(self.append_many((fact,)))

    def append_many(self, facts: Iterable[FinancialFact], *,
                    review_bundle: FactReviewBundle | None = None) -> int:
        if self._read_only:
            raise RuntimeError("financial fact repository was opened read-only")
        incoming: dict[str, FinancialFact] = {}
        encoded: dict[str, tuple[object, ...]] = {}
        for fact in facts:
            if not isinstance(fact, FinancialFact):
                raise TypeError("append_many requires FinancialFact instances")
            payload = _encode(fact)
            previous = encoded.get(fact.version_id)
            if previous is not None and previous != payload:
                raise ValueError(f"conflicting financial fact version_id {fact.version_id!r}")
            incoming[fact.version_id] = fact
            encoded[fact.version_id] = payload
        if not incoming:
            if review_bundle is not None:
                raise ValueError("review bundle requires financial facts")
            return 0
        if review_bundle is not None:
            if not review_bundle.bundle_id or not review_bundle.evidence_json:
                raise ValueError("review bundle id and evidence are required")
            # Reject non-canonical payloads so an identical logical review has
            # one stable hash and one idempotent retry representation.
            try:
                decoded_review = json.loads(review_bundle.evidence_json)
            except (TypeError, ValueError) as exc:
                raise ValueError("review evidence must be JSON") from exc
            canonical = json.dumps(decoded_review, ensure_ascii=False,
                                   sort_keys=True, separators=(",", ":"))
            if canonical != review_bundle.evidence_json:
                raise ValueError("review evidence must use canonical JSON")

        with closing(connect(self.db_path)) as con, write_tx(con):
            existing: dict[str, FinancialFact] = {}
            new: dict[str, FinancialFact] = {}
            for version_id, fact in incoming.items():
                row = con.execute(_SELECT + " WHERE version_id = ?", (version_id,)).fetchone()
                if row is None:
                    new[version_id] = fact
                elif tuple(row[column] for column in _COLUMNS) != encoded[version_id]:
                    raise ValueError(f"conflicting financial fact version_id {version_id!r}")
                else:
                    existing[version_id] = _decode(row)

            roots: set[tuple[object, ...]] = set()
            successors: dict[str, str] = {}
            for fact in new.values():
                predecessor_id = fact.supersedes_id
                if predecessor_id is None:
                    if fact.logical_key in roots:
                        raise ValueError("financial fact has two roots for one logical fact")
                    roots.add(fact.logical_key)
                    existing_root = con.execute(
                        "SELECT version_id FROM financial_fact WHERE "
                        "instrument_id = ? AND metric = ? AND period_end = ? "
                        "AND statement_scope = ? AND profit_scope IS ? "
                        "AND currency = ? AND raw_unit = ? AND supersedes_id IS NULL",
                        (fact.instrument_id, fact.metric, fact.period_end.isoformat(),
                         fact.statement_scope.value,
                         fact.profit_scope.value if fact.profit_scope else None,
                         fact.currency, fact.raw_unit),
                    ).fetchone()
                    if existing_root is not None:
                        raise ValueError("financial fact has two roots for one logical fact")
                    continue
                earlier = successors.setdefault(predecessor_id, fact.version_id)
                if earlier != fact.version_id:
                    raise ValueError(f"financial fact {predecessor_id!r} has two successors")
                row = con.execute(
                    "SELECT version_id FROM financial_fact WHERE supersedes_id = ?",
                    (predecessor_id,),
                ).fetchone()
                if row is not None:
                    raise ValueError(f"financial fact {predecessor_id!r} already has a successor")
                prior = new.get(predecessor_id) or existing.get(predecessor_id)
                if prior is None:
                    prior_row = con.execute(
                        _SELECT + " WHERE version_id = ?", (predecessor_id,)
                    ).fetchone()
                    if prior_row is None:
                        raise ValueError(f"supersedes_id {predecessor_id!r} is not present")
                    prior = _decode(prior_row)
                if fact.logical_key != prior.logical_key:
                    raise ValueError(
                        f"financial fact {fact.version_id!r} supersedes a different logical fact")
                if fact.available_at < prior.available_at:
                    raise ValueError(
                        f"financial fact {fact.version_id!r} is available before its superseded version")

            # Sort dependencies before INSERT, satisfying the immediate FK and
            # rejecting cycles even when a whole cycle arrives in one batch.
            ordered: list[FinancialFact] = []
            visiting: set[str] = set()
            visited: set[str] = set()

            def visit(version_id: str) -> None:
                if version_id in visiting:
                    raise ValueError("financial fact revision chain contains a cycle")
                if version_id in visited:
                    return
                visiting.add(version_id)
                predecessor_id = new[version_id].supersedes_id
                if predecessor_id in new:
                    visit(predecessor_id)
                visiting.remove(version_id)
                visited.add(version_id)
                ordered.append(new[version_id])

            for version_id in new:
                visit(version_id)
            for fact in ordered:
                con.execute(_INSERT, encoded[fact.version_id])
            if review_bundle is not None:
                row = con.execute(
                    "SELECT evidence_hash, evidence_json FROM financial_fact_review "
                    "WHERE bundle_id = ?", (review_bundle.bundle_id,),
                ).fetchone()
                if row is None:
                    con.execute(
                        "INSERT INTO financial_fact_review "
                        "(bundle_id, evidence_hash, evidence_json) VALUES (?, ?, ?)",
                        (review_bundle.bundle_id, review_bundle.evidence_hash,
                         review_bundle.evidence_json),
                    )
                elif (row["evidence_hash"] != review_bundle.evidence_hash or
                      row["evidence_json"] != review_bundle.evidence_json):
                    raise ValueError("conflicting financial fact review bundle")
                existing_members = {
                    row["version_id"] for row in con.execute(
                        "SELECT version_id FROM financial_fact_review_member WHERE bundle_id = ?",
                        (review_bundle.bundle_id,),
                    )
                }
                if existing_members and existing_members != set(incoming):
                    raise ValueError("conflicting financial fact review members")
                if not existing_members:
                    con.executemany(
                        "INSERT INTO financial_fact_review_member (bundle_id, version_id) "
                        "VALUES (?, ?)",
                        ((review_bundle.bundle_id, version_id) for version_id in incoming),
                    )
            return len(ordered)

    def load(self) -> VersionedFinancialFactStore:
        """Rebuild the existing in-memory store, including every old version."""

        with closing(connect(self.db_path, read_only=True)) as con:
            rows = con.execute(_SELECT + " ORDER BY sequence").fetchall()
        return VersionedFinancialFactStore(_decode(row) for row in rows)

    def load_with_accepted_s2_pdf_reviews(
        self,
    ) -> tuple[VersionedFinancialFactStore, dict[str, tuple[str, str]]]:
        """Read facts and accepted CNINFO S2 PDF review links in one read snapshot.

        A review is admitted only when its canonical evidence, hash, five DB
        members, and five actual fact versions agree. Invalid or incomplete
        reviews yield no accepted links; they never manufacture PIT facts.
        """

        with closing(connect(self.db_path, read_only=True)) as con:
            con.execute("BEGIN")
            triggers = {row["name"] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )}
            if not _IMMUTABLE_REVIEW_TRIGGERS <= triggers:
                raise ValueError("financial fact review append-only triggers are missing")
            fact_rows = con.execute(_SELECT + " ORDER BY sequence").fetchall()
            reviews = con.execute(
                "SELECT bundle_id,evidence_hash,evidence_json "
                "FROM financial_fact_review ORDER BY bundle_id"
            ).fetchall()
            memberships = con.execute(
                "SELECT bundle_id,version_id FROM financial_fact_review_member "
                "ORDER BY bundle_id,version_id"
            ).fetchall()
            con.execute("COMMIT")
        store = VersionedFinancialFactStore(_decode(row) for row in fact_rows)
        by_id = {fact.version_id: fact for fact in store.facts}
        members: dict[str, set[str]] = {}
        for row in memberships:
            members.setdefault(row["bundle_id"], set()).add(row["version_id"])
        accepted: dict[str, tuple[str, str]] = {}
        for row in reviews:
            member_ids = members.get(row["bundle_id"], set())
            if _accepted_s2_pdf_review(row, member_ids, by_id):
                for version_id in member_ids:
                    accepted[version_id] = (row["bundle_id"], row["evidence_hash"])
        return store, accepted


__all__ = ["FactReviewBundle", "FinancialFactRepository"]
