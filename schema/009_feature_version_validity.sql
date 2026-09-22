-- P0-D: 持久化因子版本注册与研究运行的科学有效性。
--
-- research_run / feature_value 是审计记录，不能通过迁移删除或重算。
-- 使用旁路注册表与关联表，既能兼容已经存在的 001 数据库，也不会对
-- 旧运行的状态或因子值做原地改写。

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS feature_version (
    feature_version  TEXT PRIMARY KEY,
    factor_id         TEXT,
    status            TEXT NOT NULL CHECK (status IN
                      ('ACTIVE','WITHDRAWN','UNVERIFIED')),
    validity_status   TEXT NOT NULL CHECK (validity_status IN
                      ('VALID','WITHDRAWN','UNVERIFIED')),
    withdrawal_reason TEXT,
    registered_at     TEXT NOT NULL,
    notes             TEXT
);

CREATE INDEX IF NOT EXISTS idx_feature_version_status
    ON feature_version (status, validity_status, factor_id);

CREATE TABLE IF NOT EXISTS research_run_feature_validity (
    research_run_id  TEXT PRIMARY KEY
                     REFERENCES research_run(research_run_id) ON DELETE CASCADE,
    feature_version  TEXT NOT NULL REFERENCES feature_version(feature_version),
    validity_status  TEXT NOT NULL CHECK (validity_status IN
                      ('VALID','WITHDRAWN','UNVERIFIED')),
    withdrawal_reason TEXT,
    associated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_run_feature_validity_version
    ON research_run_feature_validity (feature_version, validity_status);

-- Canonical registrations are defaults for versions that are not registered
-- yet.  INSERT OR IGNORE is intentional: a later operational withdrawal is
-- durable and must survive every replay of apply_migrations().  No
-- research_run or feature_value row is touched here.
INSERT OR IGNORE INTO feature_version
    (feature_version, factor_id, status, validity_status,
     withdrawal_reason, registered_at, notes)
VALUES
    ('f10-v1', 'F10', 'WITHDRAWN', 'WITHDRAWN',
     'F10 v1 的累计报表 TTM 公式漏加上一完整年度，结果已撤回',
     strftime('%Y-%m-%dT%H:%M:%SZ','now'),
     '历史版本仅供审计，不得作为默认研究结果'),
    ('f10-v2', 'F10', 'ACTIVE', 'VALID', NULL,
     strftime('%Y-%m-%dT%H:%M:%SZ','now'),
     'F10 当前生产版本');

UPDATE feature_version
SET factor_id='F10', status='WITHDRAWN', validity_status='WITHDRAWN',
    withdrawal_reason='F10 v1 的累计报表 TTM 公式漏加上一完整年度，结果已撤回'
WHERE feature_version='f10-v1';

-- 旧库里可能已经有其它研究版本。注册为 UNVERIFIED，保留可审计性，
-- 但默认快照读取不会把它们当成当前有效因子。
INSERT OR IGNORE INTO feature_version
    (feature_version, factor_id, status, validity_status,
     withdrawal_reason, registered_at, notes)
SELECT DISTINCT rr.feature_version,
       CASE WHEN rr.feature_version LIKE 'f10-%' THEN 'F10' ELSE NULL END,
       'UNVERIFIED', 'UNVERIFIED', NULL,
       strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       '迁移发现的历史版本，未完成科学有效性登记'
FROM research_run AS rr
WHERE rr.feature_version IS NOT NULL
  AND rr.feature_version <> '';

-- 关联所有历史运行。f10-v1 的 SUCCEEDED 状态刻意保持在 research_run，
-- 这里只把科学有效性标成 WITHDRAWN；这样旧因子值仍可按 run_id 审计读取。
INSERT OR IGNORE INTO research_run_feature_validity
    (research_run_id, feature_version, validity_status,
     withdrawal_reason, associated_at)
SELECT rr.research_run_id, rr.feature_version,
       fv.validity_status, fv.withdrawal_reason,
       strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM research_run AS rr
JOIN feature_version AS fv ON fv.feature_version=rr.feature_version
WHERE rr.feature_version IS NOT NULL
  AND rr.feature_version <> '';

UPDATE research_run_feature_validity
SET validity_status='WITHDRAWN',
    withdrawal_reason='F10 v1 的累计报表 TTM 公式漏加上一完整年度，结果已撤回'
WHERE feature_version='f10-v1';

-- Compatibility views make the association discoverable under both the
-- domain name and the migration-facing name used by older tooling.
CREATE VIEW IF NOT EXISTS feature_version_registry AS
SELECT feature_version, factor_id, status, validity_status,
       withdrawal_reason, registered_at, notes
FROM feature_version;

CREATE VIEW IF NOT EXISTS research_run_feature_version AS
SELECT research_run_id, feature_version, validity_status,
       withdrawal_reason, associated_at
FROM research_run_feature_validity;

INSERT OR IGNORE INTO schema_migration (version, applied_at, note)
VALUES ('009_feature_version_validity', strftime('%Y-%m-%dT%H:%M:%SZ','now'),
        'P0-D 因子版本注册、研究运行科学有效性与 F10 v1 撤回登记');
