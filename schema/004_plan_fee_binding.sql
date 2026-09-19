-- Exact fee inputs used by a frozen plan. fee_version alone does not encode
-- the user-specific commission rate/minimum, so it is insufficient for replay.
CREATE TABLE IF NOT EXISTS plan_fee_binding (
    plan_id                    TEXT PRIMARY KEY REFERENCES simulation_plan(plan_id),
    fee_version                TEXT NOT NULL,
    effective_from             TEXT NOT NULL,
    effective_to               TEXT,
    commission_rate            TEXT NOT NULL,
    commission_min_cents       INTEGER NOT NULL,
    stamp_duty_rate_sell       TEXT NOT NULL,
    transfer_fee_rate          TEXT NOT NULL,
    synthetic_test_rate        INTEGER NOT NULL CHECK (synthetic_test_rate IN (0,1)),
    commission_source          TEXT NOT NULL
);
