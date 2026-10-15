-- Engineering executions have no user confirmation or human decision log.
CREATE TABLE IF NOT EXISTS engineering_plan_provenance (
    plan_id TEXT PRIMARY KEY REFERENCES simulation_plan(plan_id),
    source TEXT NOT NULL CHECK (source IN ('ENGINEERING_TRIAL','FORWARD_ENGINEERING_SIMULATION')),
    origin_ref TEXT NOT NULL UNIQUE,
    original_registered_at TEXT NOT NULL,
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS engineering_plan_provenance_immutable
BEFORE UPDATE ON engineering_plan_provenance
BEGIN
    SELECT RAISE(ABORT, 'engineering plan provenance is immutable');
END;
CREATE TABLE IF NOT EXISTS engineering_simulation_run (
    run_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL UNIQUE REFERENCES research_simulation_draft(draft_id),
    subject_id TEXT NOT NULL,
    portfolio_id TEXT NOT NULL UNIQUE REFERENCES portfolio(portfolio_id),
    plan_id TEXT NOT NULL UNIQUE REFERENCES simulation_plan(plan_id),
    execution_snapshot_id TEXT NOT NULL REFERENCES snapshot(snapshot_id),
    spec_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PREPARED','EXECUTED','COMPLETED','DATA_NOT_READY')),
    result_json TEXT,
    result_hash TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TRIGGER IF NOT EXISTS engineering_simulation_binding_immutable
BEFORE UPDATE OF run_id,draft_id,subject_id,portfolio_id,plan_id,execution_snapshot_id,spec_json,created_at
ON engineering_simulation_run
BEGIN
    SELECT RAISE(ABORT, 'engineering run binding is immutable');
END;
CREATE TRIGGER IF NOT EXISTS engineering_simulation_result_immutable
BEFORE UPDATE ON engineering_simulation_run
WHEN OLD.status='COMPLETED'
BEGIN
    SELECT RAISE(ABORT, 'completed engineering result is immutable');
END;
INSERT OR IGNORE INTO schema_migration (version,applied_at,note)
VALUES ('012_engineering_simulation_runs',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
        'Isolated engineering execution provenance and resumable immutable result records');
