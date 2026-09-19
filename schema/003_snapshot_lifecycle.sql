-- Snapshot lifecycle metadata.  The physical snapshot rows remain immutable;
-- this table stores only the mutable logical alias used by the running product.
-- A file pointer is written alongside it so a process can resolve the current
-- object before opening SQLite.  Both values point to a concrete snapshot_id.
CREATE TABLE IF NOT EXISTS snapshot_pointer (
    pointer_name TEXT PRIMARY KEY,
    snapshot_id  TEXT NOT NULL REFERENCES snapshot(snapshot_id),
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshot_pointer_snapshot
    ON snapshot_pointer (snapshot_id);
