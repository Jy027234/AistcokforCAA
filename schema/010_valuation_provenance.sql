-- P0: bind every valuation to the exact requested market snapshot.
--
-- 001_metadata.sql is already used by existing databases and its valuation
-- table has a UNIQUE (portfolio_id, trading_day) constraint.  Keep that
-- historical table intact and store the new immutable provenance beside it.
-- The domain layer rejects a different binding for the same portfolio/day
-- before touching the historical valuation row.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS valuation_provenance (
    valuation_id       TEXT PRIMARY KEY REFERENCES valuation(valuation_id),
    portfolio_id       TEXT NOT NULL REFERENCES portfolio(portfolio_id),
    trading_day        TEXT NOT NULL,
    snapshot_id        TEXT NOT NULL REFERENCES snapshot(snapshot_id),
    execution_plan_id  TEXT REFERENCES simulation_plan(plan_id),
    as_of              TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    UNIQUE (portfolio_id, trading_day),
    CHECK (length(as_of) > 0)
);

CREATE INDEX IF NOT EXISTS idx_valuation_provenance_snapshot
    ON valuation_provenance (snapshot_id, as_of);

CREATE INDEX IF NOT EXISTS idx_valuation_provenance_plan
    ON valuation_provenance (execution_plan_id)
    WHERE execution_plan_id IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS trg_valuation_provenance_immutable_update
BEFORE UPDATE ON valuation_provenance
BEGIN
    SELECT RAISE(ABORT,
        'valuation provenance is immutable; create a new valuation version');
END;

CREATE TRIGGER IF NOT EXISTS trg_valuation_provenance_no_delete
BEFORE DELETE ON valuation_provenance
BEGIN
    SELECT RAISE(ABORT,
        'valuation provenance cannot be deleted; create a new valuation version');
END;

INSERT OR IGNORE INTO schema_migration (version, applied_at, note)
VALUES ('010_valuation_provenance', strftime('%Y-%m-%dT%H:%M:%SZ','now'),
        '估值绑定请求快照并持久化 snapshot、execution plan 与 as_of 来源');
