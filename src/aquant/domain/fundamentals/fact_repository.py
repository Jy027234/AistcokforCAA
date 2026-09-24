"""Durable, append-only storage for :class:`FinancialFact` versions.

This repository uses its own SQLite file.  It deliberately does not run the
application's ``meta.sqlite`` migrations: a caller can populate it from a
verified announcement extractor and replay the exact facts into the existing
PIT selector after a restart.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable

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


class FinancialFactRepository:
    """Own an independent SQLite fact file and replay it into the PIT store.

    ``append_many`` is atomic and returns the number of newly inserted
    versions. An identical ``version_id`` retry returns zero; a changed retry
    raises ``ValueError``. Revisions may be supplied out of order within one
    batch, provided the complete chain is present and has one successor per
    version.
    """

    def __init__(self, db_path: str | Path) -> None:
        if str(db_path) == ":memory:":
            raise ValueError("financial fact repository requires a persistent file")
        self.db_path = Path(db_path)
        with closing(connect(self.db_path)) as con:
            # executescript commits implicitly, so schema setup stays outside
            # the short append transaction. DDL is idempotent on restart.
            con.executescript(_SCHEMA)

    def append(self, fact: FinancialFact) -> bool:
        """Append one version; return whether it was newly inserted."""

        return bool(self.append_many((fact,)))

    def append_many(self, facts: Iterable[FinancialFact]) -> int:
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
            return 0

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
            return len(ordered)

    def load(self) -> VersionedFinancialFactStore:
        """Rebuild the existing in-memory store, including every old version."""

        with closing(connect(self.db_path, read_only=True)) as con:
            rows = con.execute(_SELECT + " ORDER BY sequence").fetchall()
        return VersionedFinancialFactStore(_decode(row) for row in rows)


__all__ = ["FinancialFactRepository"]
