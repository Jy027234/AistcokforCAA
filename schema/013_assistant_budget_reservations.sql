-- A short-lived reservation ledger for the single governed assistant egress path.
-- Daily caps are installation-wide; subject_id is retained for audit ownership.
CREATE TABLE IF NOT EXISTS assistant_budget_reservation (
    reservation_id TEXT PRIMARY KEY,
    budget_day TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    price_version TEXT NOT NULL,
    price_source TEXT NOT NULL,
    price_effective_from TEXT NOT NULL,
    input_price_micro_cny_per_million_tokens INTEGER NOT NULL
        CHECK (input_price_micro_cny_per_million_tokens > 0),
    output_price_micro_cny_per_million_tokens INTEGER NOT NULL
        CHECK (output_price_micro_cny_per_million_tokens > 0),
    daily_cap_micro_cny INTEGER NOT NULL CHECK (daily_cap_micro_cny > 0),
    input_tokens_reserved INTEGER NOT NULL CHECK (input_tokens_reserved > 0),
    output_tokens_reserved INTEGER NOT NULL CHECK (output_tokens_reserved > 0),
    reserved_micro_cny INTEGER NOT NULL CHECK (reserved_micro_cny > 0),
    settled_micro_cny INTEGER CHECK (settled_micro_cny >= 0),
    status TEXT NOT NULL CHECK (status IN ('RESERVED','SETTLED','HELD')),
    model_call_id TEXT UNIQUE REFERENCES model_call(model_call_id),
    created_at TEXT NOT NULL,
    closed_at TEXT,
    CHECK (
        (status = 'RESERVED' AND model_call_id IS NULL
            AND settled_micro_cny IS NULL AND closed_at IS NULL)
        OR (status = 'SETTLED' AND model_call_id IS NOT NULL
            AND settled_micro_cny IS NOT NULL AND closed_at IS NOT NULL)
        OR (status = 'HELD' AND model_call_id IS NOT NULL
            AND settled_micro_cny IS NULL AND closed_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_assistant_budget_day
    ON assistant_budget_reservation (budget_day, status, provider, model);
