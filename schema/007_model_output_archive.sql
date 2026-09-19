-- Q5 A12: immutable model output archives and per-run registration.
--
-- A model_call row used to retain only the provider-reported digest.  That is
-- enough for an audit index, but it is not enough to replay a historical
-- result.  Keep the exact UTF-8 response in a content-addressed archive and
-- register every attempt (including a replay) separately.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS model_output_archive (
    archive_id          TEXT PRIMARY KEY,
    subject_id          TEXT NOT NULL DEFAULT 'system',
    input_hash          TEXT NOT NULL,
    output_hash         TEXT NOT NULL,
    artifact_hash       TEXT,
    output_text         TEXT NOT NULL,
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    prompt_version      TEXT NOT NULL,
    source_model_call_id TEXT,
    archived_at         TEXT NOT NULL,
    UNIQUE (subject_id, input_hash, output_hash, provider, model, prompt_version)
);

CREATE INDEX IF NOT EXISTS idx_model_output_archive_hash
    ON model_output_archive (output_hash);
CREATE INDEX IF NOT EXISTS idx_model_output_archive_input
    ON model_output_archive (input_hash, prompt_version);
CREATE INDEX IF NOT EXISTS idx_model_output_archive_subject
    ON model_output_archive (subject_id, archived_at);

CREATE TABLE IF NOT EXISTS model_run (
    model_run_id        TEXT PRIMARY KEY,
    model_call_id       TEXT NOT NULL UNIQUE REFERENCES model_call(model_call_id),
    subject_id          TEXT NOT NULL DEFAULT 'system',
    research_run_id     TEXT,
    archive_id          TEXT REFERENCES model_output_archive(archive_id),
    input_hash          TEXT NOT NULL,
    output_hash         TEXT,
    artifact_hash       TEXT,
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    prompt_version      TEXT NOT NULL,
    run_kind            TEXT NOT NULL CHECK (run_kind IN ('ORIGINAL','REPLAY')),
    registered_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_model_run_research
    ON model_run (research_run_id, registered_at);
CREATE INDEX IF NOT EXISTS idx_model_run_archive
    ON model_run (archive_id, registered_at);

-- model_call is part of the pre-existing metadata schema.  Keep its owner
-- binding in a side table so this migration remains safe on databases where
-- that table has already been created; the API always scopes reads by this
-- verified subject value.
CREATE TABLE IF NOT EXISTS model_call_owner (
    model_call_id TEXT PRIMARY KEY REFERENCES model_call(model_call_id),
    subject_id    TEXT NOT NULL,
    bound_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_call_owner_subject
    ON model_call_owner (subject_id, bound_at);

-- Existing model_call rows predate subject ownership.  They belong to the
-- system scope until an explicit owner binding exists.  INSERT OR IGNORE is
-- deliberate: rerunning this migration must be idempotent and must never
-- overwrite a verified owner that was bound after the first run.
INSERT OR IGNORE INTO model_call_owner (model_call_id, subject_id, bound_at)
SELECT model_call_id, 'system', called_at
FROM model_call;

INSERT OR IGNORE INTO schema_migration (version, applied_at, note)
VALUES ('007_model_output_archive', strftime('%Y-%m-%dT%H:%M:%SZ','now'),
        'Q5 A12 固定输入与模型输出归档回放及独立模型运行登记');
