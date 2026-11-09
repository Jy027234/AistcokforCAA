CREATE TABLE IF NOT EXISTS pipeline_run_history (
    request_id       TEXT PRIMARY KEY NOT NULL REFERENCES pipeline_run_request(request_id),
    source           TEXT NOT NULL CHECK (source IN ('scheduler', 'manual')),
    trading_day      TEXT,
    snapshot_id      TEXT,
    outcome          TEXT,
    reason           TEXT,
    exit_code        INTEGER,
    started_at       TEXT,
    finished_at      TEXT NOT NULL,
    duration_seconds REAL,
    steps_json       TEXT NOT NULL,
    data_last_day      TEXT,
    calendar_gate_json TEXT,
    artifact_dir       TEXT,
    retryable_kind     TEXT
);
