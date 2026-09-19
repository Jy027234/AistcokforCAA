-- Immutable full API payload for research cards (005)
--
-- research_card keeps the queryable/indexed fields used by history and audits.
-- This companion table keeps the exact complete response that was shown when
-- the card was first created.  A later evidence arrival or view-model change
-- must never rewrite that response for the same card identity.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS research_card_payload (
    card_id       TEXT PRIMARY KEY REFERENCES research_card(card_id) ON DELETE CASCADE,
    payload_json  TEXT NOT NULL,
    payload_hash  TEXT NOT NULL,
    persisted_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_card_payload_hash
    ON research_card_payload (payload_hash);

INSERT OR IGNORE INTO schema_migration (version, applied_at, note)
VALUES ('006_research_card_payload', strftime('%Y-%m-%dT%H:%M:%SZ','now'),
        '研究卡首次完整 API payload 不可变留档');

