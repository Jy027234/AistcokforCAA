-- Research selections are reviewable drafts, never simulation plans or fills.
CREATE TABLE IF NOT EXISTS research_simulation_draft (
    draft_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('USER_SELECTION', 'ENGINEERING_TRIAL')),
    portfolio_id TEXT NOT NULL,
    research_snapshot_id TEXT NOT NULL,
    research_content_hash TEXT NOT NULL,
    decision_snapshot_id TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    status TEXT NOT NULL,
    draft_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_research_simulation_draft_subject
    ON research_simulation_draft(subject_id, portfolio_id, created_at);
CREATE TRIGGER IF NOT EXISTS research_simulation_draft_immutable
BEFORE UPDATE ON research_simulation_draft
BEGIN
    SELECT RAISE(ABORT, 'research simulation drafts are immutable; create a new preview');
END;
