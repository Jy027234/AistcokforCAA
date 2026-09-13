-- A-Quant Lab 元数据与模拟账本基础结构
-- 主文档 §15.1：数值事实放 Parquet，事务性对象和引用索引放 SQLite。
-- 本迁移只承载"事务性对象与引用索引"，不承载行情/财务长表。
--
-- 设计约定（与 contracts/ 保持一致）：
--   1. 金额一律为整数分（INTEGER），禁止 REAL 记账（§12.6）。
--   2. 股数一律为整数（§12.5）。
--   3. 所有时间戳为带时区 UTC ISO 8601 文本，比较用字典序即可（同格式等长）。
--   4. 幂等键带 UNIQUE 约束，由数据库保证"重试只记一次"（§8.4、S05）。
--   5. 已发布快照与已入账成交不得物理删除；更正用新版本或冲正（§11.4、§15.4）。
--
-- 本文件仅通过语法与约束自检（tests/validate_spec.py）；
-- "基础 SQL 通过语法验证不代表已经实现领域不变量"（主文档 §15.1）。

PRAGMA foreign_keys = ON;

-- ============================================================
-- 0. 模式与迁移
-- ============================================================
CREATE TABLE IF NOT EXISTS schema_migration (
    version         TEXT PRIMARY KEY,
    applied_at      TEXT NOT NULL,
    note            TEXT
);

-- ============================================================
-- 1. 运维域：任务、幂等、模型调用、审计
--    主文档 §8.4 / §15.1
-- ============================================================
CREATE TABLE IF NOT EXISTS job (
    job_id          TEXT PRIMARY KEY,
    job_type        TEXT NOT NULL,
    trading_day     TEXT,
    config_version  TEXT,
    input_snapshot_id TEXT,
    idempotency_key TEXT NOT NULL,
    status          TEXT NOT NULL
                    CHECK (status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','BLOCKED')),
    attempt_count   INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    lease_owner     TEXT,
    lease_expires_at TEXT,
    payload_json    TEXT,
    result_json     TEXT,
    error_code      TEXT,
    error_detail    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    heartbeat_at    TEXT,
    -- §8.4 幂等键 = 作业类型 + 交易日 + 配置版本 + 输入快照
    UNIQUE (idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_job_status ON job (status, created_at);
CREATE INDEX IF NOT EXISTS idx_job_lease  ON job (status, lease_expires_at);

CREATE TABLE IF NOT EXISTS model_call (
    model_call_id   TEXT PRIMARY KEY,
    job_id          TEXT REFERENCES job(job_id),
    research_run_id TEXT,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    prompt_version  TEXT,
    contract_version TEXT,
    content_hash    TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cost_micro_cny  INTEGER,
    outcome         TEXT NOT NULL CHECK (outcome IN ('OK','TIMEOUT','BUDGET_EXCEEDED','ERROR','REJECTED')),
    error_code      TEXT,
    called_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_model_call_hash ON model_call (content_hash, model, prompt_version);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id        TEXT PRIMARY KEY,
    occurred_at     TEXT NOT NULL,
    actor_type      TEXT NOT NULL CHECK (actor_type IN ('USER','SYSTEM','MODEL','WORKER','AGENTCTL')),
    actor_id        TEXT,
    action          TEXT NOT NULL,
    object_type     TEXT,
    object_id       TEXT,
    request_id      TEXT,
    detail_json     TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_object ON audit_log (object_type, object_id, occurred_at);

-- ============================================================
-- 2. 数据域：来源、数据版本、证券、日历、快照
--    主文档 §6.1 / §7 / §8.3 / §15.2
-- ============================================================
CREATE TABLE IF NOT EXISTS source_registry (
    source_id       TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    vendor          TEXT,
    endpoint        TEXT,
    -- §17.2 逐项登记权利；UNKNOWN 默认不开放对应能力
    research_use            TEXT NOT NULL DEFAULT 'UNKNOWN'
                            CHECK (research_use IN ('ALLOWED','PROHIBITED','UNKNOWN')),
    local_storage           TEXT NOT NULL DEFAULT 'UNKNOWN'
                            CHECK (local_storage IN ('ALLOWED','PROHIBITED','UNKNOWN')),
    retention_policy        TEXT,
    model_processing        TEXT NOT NULL DEFAULT 'UNKNOWN'
                            CHECK (model_processing IN ('ALLOWED','PROHIBITED','UNKNOWN')),
    excerpt_display         TEXT NOT NULL DEFAULT 'UNKNOWN'
                            CHECK (excerpt_display IN ('ALLOWED','PROHIBITED','UNKNOWN')),
    third_party_redistribution TEXT NOT NULL DEFAULT 'UNKNOWN'
                            CHECK (third_party_redistribution IN ('ALLOWED','PROHIBITED','UNKNOWN')),
    commercial_use          TEXT NOT NULL DEFAULT 'UNKNOWN'
                            CHECK (commercial_use IN ('ALLOWED','PROHIBITED','UNKNOWN')),
    -- §6.2 M0 四态：计划接入 / 凭证已配置 / 测试通过 / 用于正式研究
    integration_state TEXT NOT NULL DEFAULT 'PLANNED'
                            CHECK (integration_state IN
                              ('PLANNED','CREDENTIALS_CONFIGURED','TEST_PASSED','IN_PRODUCTION_USE')),
    cost_model      TEXT CHECK (cost_model IN ('FREE','PAID','FREEMIUM','UNKNOWN')),
    notes           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS data_capability_card (
    card_id         TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES source_registry(source_id),
    domain          TEXT NOT NULL CHECK (domain IN
                      ('CALENDAR_IDENTITY','DAILY_QUOTES','ADJUSTMENTS','CORPORATE_ACTIONS',
                       'INDUSTRY_CONSTITUENTS','FINANCIALS','ANNOUNCEMENTS','MACRO','NEWS','SOCIAL_HEAT')),
    fields_json     TEXT,
    unit_notes      TEXT,
    time_semantics  TEXT,
    coverage_start  TEXT,
    coverage_end    TEXT,
    -- §7.2 是否提供历史时点版本；决定能否做正式 PIT 回测
    pit_available   TEXT NOT NULL DEFAULT 'NO'
                    CHECK (pit_available IN ('YES','PARTIAL','NO','UNKNOWN')),
    pit_basis       TEXT CHECK (pit_basis IN ('OBSERVED','VENDOR_PIT','RECONSTRUCTED','UNKNOWN')),
    pagination_notes TEXT,
    quota_notes     TEXT,
    measured_at     TEXT,
    evidence_json   TEXT,
    UNIQUE (source_id, domain)
);

CREATE TABLE IF NOT EXISTS data_version (
    data_version    TEXT PRIMARY KEY,
    source_id       TEXT REFERENCES source_registry(source_id),
    domain          TEXT NOT NULL,
    ingested_at     TEXT NOT NULL,
    as_of_upper_bound TEXT,
    record_count    INTEGER NOT NULL DEFAULT 0,
    content_hash    TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS trading_calendar (
    exchange        TEXT NOT NULL CHECK (exchange IN ('SSE','SZSE','BSE','OTHER')),
    calendar_date   TEXT NOT NULL,
    is_trading_day  INTEGER NOT NULL CHECK (is_trading_day IN (0,1)),
    prev_trading_day TEXT,
    next_trading_day TEXT,
    source_id       TEXT REFERENCES source_registry(source_id),
    data_version    TEXT REFERENCES data_version(data_version),
    PRIMARY KEY (exchange, calendar_date)
);

CREATE TABLE IF NOT EXISTS instrument (
    instrument_id   TEXT PRIMARY KEY,
    exchange        TEXT NOT NULL CHECK (exchange IN ('SSE','SZSE','BSE','OTHER')),
    board           TEXT NOT NULL CHECK (board IN ('MAIN','GEM','STAR','BSE','OTHER')),
    security_class  TEXT NOT NULL DEFAULT 'EQUITY',
    -- §15.2 供应商代码只是映射，交易所与证券类别独立保存
    legal_name      TEXT,
    short_name      TEXT,
    currency        TEXT NOT NULL DEFAULT 'CNY',
    listed_on       TEXT,
    delisted_on     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_instrument_board ON instrument (exchange, board);

CREATE TABLE IF NOT EXISTS instrument_code_map (
    instrument_id   TEXT NOT NULL REFERENCES instrument(instrument_id),
    source_id       TEXT NOT NULL REFERENCES source_registry(source_id),
    vendor_code     TEXT NOT NULL,
    valid_from      TEXT NOT NULL,
    valid_to        TEXT,
    PRIMARY KEY (source_id, vendor_code, valid_from)
);

-- §7.1/§15.2 名称与状态按有效期版本化：更名不产生新证券
CREATE TABLE IF NOT EXISTS instrument_status_version (
    instrument_id   TEXT NOT NULL REFERENCES instrument(instrument_id),
    valid_from      TEXT NOT NULL,
    valid_to        TEXT,
    name            TEXT,
    status          TEXT NOT NULL CHECK (status IN
                      ('LISTED','SUSPENDED','RISK_WARNING','DELISTING','DELISTED','UNKNOWN')),
    industry_code   TEXT,
    industry_name   TEXT,
    classification_version TEXT,
    source_id       TEXT REFERENCES source_registry(source_id),
    PRIMARY KEY (instrument_id, valid_from)
);

CREATE TABLE IF NOT EXISTS snapshot (
    snapshot_id     TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('EOD','PREOPEN','ADHOC')),
    data_mode       TEXT NOT NULL CHECK (data_mode IN ('PRODUCTION','SYNTHETIC')),
    status          TEXT NOT NULL CHECK (status IN
                      ('DRAFT','VALIDATING','PUBLISHED','SUPERSEDED','REJECTED')),
    input_cutoff_at TEXT NOT NULL,
    as_of_time      TEXT,
    published_at    TEXT,
    created_at      TEXT NOT NULL,
    parent_snapshot_id TEXT REFERENCES snapshot(snapshot_id),
    supersedes      TEXT REFERENCES snapshot(snapshot_id),
    watermark       TEXT,
    code_version    TEXT NOT NULL,
    data_version    TEXT NOT NULL,
    strategy_version TEXT,
    feature_version TEXT,
    rule_version    TEXT,
    fee_version     TEXT,
    quality_status  TEXT NOT NULL CHECK (quality_status IN ('OK','DEGRADED','BLOCKING')),
    pool_hash       TEXT
);

CREATE INDEX IF NOT EXISTS idx_snapshot_status ON snapshot (status, kind, created_at);

CREATE TABLE IF NOT EXISTS snapshot_dataset (
    snapshot_id     TEXT NOT NULL REFERENCES snapshot(snapshot_id),
    name            TEXT NOT NULL,
    path            TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    record_count    INTEGER NOT NULL DEFAULT 0,
    as_of_upper_bound TEXT,
    coverage_ratio  REAL CHECK (coverage_ratio IS NULL OR (coverage_ratio >= 0 AND coverage_ratio <= 1)),
    missing_key_fields_json TEXT,
    notes_json      TEXT,
    PRIMARY KEY (snapshot_id, name)
);

CREATE TABLE IF NOT EXISTS snapshot_blocking_issue (
    snapshot_id     TEXT NOT NULL REFERENCES snapshot(snapshot_id),
    issue_seq       INTEGER NOT NULL,
    error_code      TEXT NOT NULL,
    message         TEXT NOT NULL,
    object_id       TEXT,
    retryable       INTEGER NOT NULL CHECK (retryable IN (0,1)),
    repair_action   TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, issue_seq)
);

-- 已发布快照不可变：禁止 UPDATE/DELETE（由触发器强制）
CREATE TRIGGER IF NOT EXISTS trg_snapshot_published_immutable_update
BEFORE UPDATE ON snapshot
WHEN OLD.status IN ('PUBLISHED','SUPERSEDED')
BEGIN
    SELECT RAISE(ABORT, 'published snapshot is immutable; create a new snapshot with supersedes');
END;

CREATE TRIGGER IF NOT EXISTS trg_snapshot_published_immutable_delete
BEFORE DELETE ON snapshot
WHEN OLD.status IN ('PUBLISHED','SUPERSEDED')
BEGIN
    SELECT RAISE(ABORT, 'published snapshot cannot be deleted; create a new snapshot with supersedes');
END;

-- ============================================================
-- 3. 证据域：文档、引用、事件
--    主文档 §9 / §15.3
-- ============================================================
CREATE TABLE IF NOT EXISTS document (
    document_id     TEXT PRIMARY KEY,
    origin          TEXT NOT NULL,
    url             TEXT,
    is_original     INTEGER NOT NULL CHECK (is_original IN (0,1)),
    repost_of       TEXT REFERENCES document(document_id),
    fetched_at      TEXT NOT NULL,
    source_published_at TEXT,
    source_published_date TEXT,
    timestamp_precision TEXT NOT NULL CHECK (timestamp_precision IN ('SECOND','MINUTE','DATE','UNKNOWN')),
    content_hash    TEXT,
    raw_path        TEXT,
    license_status  TEXT NOT NULL DEFAULT 'UNKNOWN'
                    CHECK (license_status IN ('PERMITTED','EXCERPT_ONLY','INDEX_ONLY','UNKNOWN')),
    withdrawn_at    TEXT,
    source_id       TEXT REFERENCES source_registry(source_id)
);

CREATE INDEX IF NOT EXISTS idx_document_hash ON document (content_hash);

CREATE TABLE IF NOT EXISTS event (
    event_id        TEXT PRIMARY KEY,
    event_category  TEXT NOT NULL,
    fact_summary    TEXT NOT NULL,
    event_time      TEXT,
    event_time_precision TEXT CHECK (event_time_precision IN ('SECOND','MINUTE','DATE','UNKNOWN')),
    source_published_at TEXT,
    source_published_date TEXT,
    first_seen_at   TEXT NOT NULL,
    ingested_at     TEXT NOT NULL,
    -- §7.1 available_at 是 PIT 门禁的唯一判据
    available_at    TEXT NOT NULL,
    available_basis TEXT NOT NULL CHECK (available_basis IN
                      ('OBSERVED','VENDOR_PIT','RECONSTRUCTED','UNKNOWN')),
    pit_mode        TEXT NOT NULL CHECK (pit_mode IN ('LIVE_OBSERVED','HISTORICAL_RECONSTRUCTED')),
    valid_from      TEXT,
    valid_to        TEXT,
    supersedes_id   TEXT REFERENCES event(event_id),
    verification_status TEXT NOT NULL CHECK (verification_status IN
                      ('UNVERIFIED','VERIFIED','DISPUTED','WITHDRAWN','UNKNOWN')),
    market_direction TEXT NOT NULL CHECK (market_direction IN
                      ('BULLISH','BEARISH','UNCERTAIN','UNKNOWN')),
    research_window_start TEXT,
    research_window_end   TEXT,
    model_version   TEXT,
    prompt_version  TEXT,
    raw_output_hash TEXT,
    llm_retrospective_risk INTEGER CHECK (llm_retrospective_risk IN (0,1)),
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_available ON event (available_at, event_category);

CREATE TABLE IF NOT EXISTS event_document (
    event_id        TEXT NOT NULL REFERENCES event(event_id),
    document_id     TEXT NOT NULL REFERENCES document(document_id),
    relation        TEXT NOT NULL DEFAULT 'SUPPORTS'
                    CHECK (relation IN ('SUPPORTS','CONTRADICTS','CONTEXT')),
    PRIMARY KEY (event_id, document_id, relation)
);

CREATE TABLE IF NOT EXISTS citation (
    citation_id     TEXT PRIMARY KEY,
    event_id        TEXT NOT NULL REFERENCES event(event_id),
    document_id     TEXT NOT NULL REFERENCES document(document_id),
    quote           TEXT NOT NULL,
    locator_kind    TEXT NOT NULL CHECK (locator_kind IN
                      ('CHAR_OFFSET','PAGE_LINE','SECTION','EXACT_MATCH')),
    locator_start   INTEGER,
    locator_end     INTEGER,
    locator_page    INTEGER,
    locator_line    INTEGER,
    locator_section TEXT,
    -- §15.3 引用必须能定位；不可定位则不得发布
    located         INTEGER NOT NULL DEFAULT 0 CHECK (located IN (0,1))
);

CREATE TABLE IF NOT EXISTS event_subject (
    event_id        TEXT NOT NULL REFERENCES event(event_id),
    subject_type    TEXT NOT NULL CHECK (subject_type IN
                      ('INSTRUMENT','INDUSTRY','MACRO_INDICATOR','OTHER')),
    subject_id      TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'PRIMARY'
                    CHECK (role IN ('PRIMARY','RELATED','AFFECTED','COUNTERPARTY')),
    PRIMARY KEY (event_id, subject_type, subject_id)
);

CREATE TABLE IF NOT EXISTS event_structured_value (
    value_id        TEXT PRIMARY KEY,
    event_id        TEXT NOT NULL REFERENCES event(event_id),
    name            TEXT NOT NULL,
    value_text      TEXT,
    unit            TEXT NOT NULL,
    raw_value       TEXT,
    raw_unit        TEXT
);

CREATE TABLE IF NOT EXISTS event_impact_hypothesis (
    hypothesis_id   TEXT PRIMARY KEY,
    event_id        TEXT NOT NULL REFERENCES event(event_id),
    hypothesis      TEXT NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN ('BULLISH','BEARISH','UNCERTAIN','UNKNOWN')),
    transmission_path TEXT,
    is_model_generated INTEGER NOT NULL DEFAULT 0 CHECK (is_model_generated IN (0,1))
);

CREATE TABLE IF NOT EXISTS event_counter_evidence (
    counter_id      TEXT PRIMARY KEY,
    event_id        TEXT NOT NULL REFERENCES event(event_id),
    statement       TEXT NOT NULL,
    citation_id     TEXT REFERENCES citation(citation_id),
    none_found      INTEGER NOT NULL DEFAULT 0 CHECK (none_found IN (0,1))
);

-- ============================================================
-- 4. 研究域：策略版本、实验、研究运行
--    主文档 §10 / §13 / §15.1
-- ============================================================
CREATE TABLE IF NOT EXISTS strategy_version (
    strategy_version TEXT PRIMARY KEY,
    family          TEXT NOT NULL CHECK (family IN ('S1','S2','E1','CUSTOM')),
    spec_json       TEXT NOT NULL,
    spec_hash       TEXT NOT NULL,
    frozen_at       TEXT NOT NULL,
    parent_version  TEXT REFERENCES strategy_version(strategy_version),
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS experiment (
    experiment_id   TEXT PRIMARY KEY,
    hypothesis      TEXT NOT NULL,
    -- §13.2 实验登记先于看结果
    registered_at   TEXT NOT NULL,
    data_range_start TEXT,
    data_range_end   TEXT,
    universe_json   TEXT,
    feature_version TEXT,
    strategy_version TEXT REFERENCES strategy_version(strategy_version),
    train_start     TEXT, train_end TEXT,
    valid_start     TEXT, valid_end TEXT,
    test_start      TEXT, test_end TEXT,
    preprocessing_json TEXT,
    label_window    TEXT,
    fee_version     TEXT,
    slippage_bps    INTEGER,
    comparison_json TEXT,
    primary_metric  TEXT,
    stopping_condition TEXT,
    status          TEXT NOT NULL DEFAULT 'REGISTERED'
                    CHECK (status IN ('REGISTERED','RUNNING','COMPLETED','FAILED','ABANDONED')),
    -- §13.2 失败的与负面的结果同样保留
    outcome_notes   TEXT,
    test_set_access_count INTEGER NOT NULL DEFAULT 0 CHECK (test_set_access_count >= 0)
);

CREATE TABLE IF NOT EXISTS research_run (
    research_run_id TEXT PRIMARY KEY,
    experiment_id   TEXT REFERENCES experiment(experiment_id),
    snapshot_id     TEXT NOT NULL REFERENCES snapshot(snapshot_id),
    as_of_time      TEXT NOT NULL,
    code_version    TEXT NOT NULL,
    strategy_version TEXT,
    feature_version TEXT,
    rule_version    TEXT,
    fee_version     TEXT,
    model_version   TEXT,
    prompt_version  TEXT,
    config_hash     TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL CHECK (status IN ('RUNNING','SUCCEEDED','FAILED','BLOCKED')),
    output_hash     TEXT,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_research_run_snapshot ON research_run (snapshot_id, as_of_time);

CREATE TABLE IF NOT EXISTS research_card (
    card_id         TEXT PRIMARY KEY,
    research_run_id TEXT REFERENCES research_run(research_run_id),
    snapshot_id     TEXT NOT NULL REFERENCES snapshot(snapshot_id),
    instrument_id   TEXT NOT NULL REFERENCES instrument(instrument_id),
    generated_at    TEXT NOT NULL,
    data_mode       TEXT NOT NULL CHECK (data_mode IN ('PRODUCTION','SYNTHETIC')),
    quality_label   TEXT NOT NULL,
    numeric_json    TEXT,
    evidence_json   TEXT,
    counter_evidence_json TEXT,
    uncertainty_json TEXT,
    limitations_json TEXT
);

CREATE TABLE IF NOT EXISTS feature_value (
    research_run_id TEXT NOT NULL REFERENCES research_run(research_run_id),
    instrument_id   TEXT NOT NULL REFERENCES instrument(instrument_id),
    factor_id       TEXT NOT NULL,
    raw_value       REAL,
    transformed_value REAL,
    cross_sectional_rank REAL,
    -- §10.2 缺失值不得用 0 填充；必须输出显式原因
    exclusion_reason TEXT,
    coverage_ratio  REAL,
    PRIMARY KEY (research_run_id, instrument_id, factor_id)
);

-- ============================================================
-- 5. 决策域：研究决策、模拟草稿、冻结计划
--    主文档 §5.5 / §8.2 / §11 / §15.1
-- ============================================================
CREATE TABLE IF NOT EXISTS simulation_plan (
    plan_id         TEXT PRIMARY KEY,
    portfolio_id    TEXT NOT NULL,
    snapshot_id     TEXT NOT NULL REFERENCES snapshot(snapshot_id),
    account_version INTEGER NOT NULL,
    plan_version    INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN
                      ('DRAFT','PREVIEWED','FROZEN','EXECUTING','EXECUTED','CANCELLED','EXPIRED','SUPERSEDED')),
    created_at      TEXT NOT NULL,
    frozen_at       TEXT,
    -- §16.2 冻结必须复核时点、快照、规则、账户版本；有效期过后拒绝
    expires_at      TEXT,
    confirmed_by    TEXT,
    confirmation_token_hash TEXT,
    rule_version    TEXT,
    fee_version     TEXT,
    diff_preview_json TEXT,
    estimated_fees_cents INTEGER,
    idempotency_key TEXT NOT NULL,
    UNIQUE (idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_plan_status ON simulation_plan (portfolio_id, status, created_at);

-- 冻结后计划主体不可变（订单列表另表且在冻结时一次性写入）
CREATE TRIGGER IF NOT EXISTS trg_plan_frozen_immutable
BEFORE UPDATE ON simulation_plan
WHEN OLD.status IN ('FROZEN','EXECUTING','EXECUTED')
     AND NEW.status NOT IN ('EXECUTING','EXECUTED','CANCELLED','EXPIRED','SUPERSEDED')
BEGIN
    SELECT RAISE(ABORT, 'frozen plan is immutable; create a new plan version');
END;

CREATE TABLE IF NOT EXISTS decision_log (
    decision_id     TEXT PRIMARY KEY,
    portfolio_id    TEXT NOT NULL,
    plan_id         TEXT REFERENCES simulation_plan(plan_id),
    snapshot_id     TEXT NOT NULL REFERENCES snapshot(snapshot_id),
    decision_type   TEXT NOT NULL CHECK (decision_type IN
                      ('ACCEPT_MODEL','MODIFY_MODEL','NO_CHANGE','TIMEOUT','REJECT','CANCEL')),
    -- §11.3 保存模型原方案与人工差异，不事后美化
    model_proposed_json TEXT,
    human_final_json    TEXT,
    diff_json           TEXT,
    reason_category TEXT,
    reason_note     TEXT,
    external_information_used INTEGER NOT NULL DEFAULT 0
                      CHECK (external_information_used IN (0,1)),
    submitted_at    TEXT NOT NULL,
    rule_check_json TEXT
);

-- ============================================================
-- 6. 账户域：模拟组合、订单、成交、批次、现金/应收账本、估值
--    主文档 §12 / §15.1
-- ============================================================
CREATE TABLE IF NOT EXISTS portfolio (
    portfolio_id    TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('M','E','H','B')),
    account_type    TEXT NOT NULL DEFAULT 'SIMULATED' CHECK (account_type = 'SIMULATED'),
    base_currency   TEXT NOT NULL DEFAULT 'CNY',
    initial_cash_cents INTEGER NOT NULL CHECK (initial_cash_cents >= 0),
    opened_at       TEXT NOT NULL,
    experiment_id   TEXT REFERENCES experiment(experiment_id),
    external_information_used INTEGER NOT NULL DEFAULT 0
                    CHECK (external_information_used IN (0,1)),
    status          TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','CLOSED'))
);

CREATE TABLE IF NOT EXISTS "order" (
    order_id        TEXT PRIMARY KEY,
    portfolio_id    TEXT NOT NULL REFERENCES portfolio(portfolio_id),
    plan_id         TEXT NOT NULL REFERENCES simulation_plan(plan_id),
    snapshot_id     TEXT NOT NULL REFERENCES snapshot(snapshot_id),
    instrument_id   TEXT NOT NULL REFERENCES instrument(instrument_id),
    side            TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    limit_price_cents INTEGER,
    status          TEXT NOT NULL CHECK (status IN
                      ('PENDING','REJECTED','NO_FILL','PARTIAL','FILLED','CANCELLED')),
    trading_day     TEXT NOT NULL,
    process_sequence INTEGER NOT NULL DEFAULT 0,
    reject_reason   TEXT,
    declared_rule_version TEXT,
    declared_fee_version  TEXT,
    created_at      TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    UNIQUE (idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_order_portfolio_day ON "order" (portfolio_id, trading_day, process_sequence);

CREATE TABLE IF NOT EXISTS fill (
    fill_id         TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL REFERENCES "order"(order_id),
    portfolio_id    TEXT NOT NULL REFERENCES portfolio(portfolio_id),
    instrument_id   TEXT NOT NULL REFERENCES instrument(instrument_id),
    side            TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    price_cents     INTEGER NOT NULL CHECK (price_cents > 0),
    gross_amount_cents INTEGER NOT NULL,
    fees_total_cents   INTEGER NOT NULL DEFAULT 0 CHECK (fees_total_cents >= 0),
    trading_day     TEXT NOT NULL,
    filled_at       TEXT NOT NULL,
    lot_id          TEXT,
    reversal_of     TEXT REFERENCES fill(fill_id),
    -- §11.4 已入账成交不得物理删除
    is_reversed     INTEGER NOT NULL DEFAULT 0 CHECK (is_reversed IN (0,1))
);

CREATE INDEX IF NOT EXISTS idx_fill_order ON fill (order_id);
CREATE INDEX IF NOT EXISTS idx_fill_portfolio_day ON fill (portfolio_id, trading_day);

CREATE TRIGGER IF NOT EXISTS trg_fill_no_delete
BEFORE DELETE ON fill
BEGIN
    SELECT RAISE(ABORT, 'booked fills cannot be deleted; use reversal plus a new version');
END;

CREATE TABLE IF NOT EXISTS fee_charge (
    fee_charge_id   TEXT PRIMARY KEY,
    fill_id         TEXT NOT NULL REFERENCES fill(fill_id),
    fee_code        TEXT NOT NULL CHECK (fee_code IN
                      ('COMMISSION','MIN_COMMISSION_TOPUP','STAMP_DUTY','TRANSFER_FEE','OTHER')),
    amount_cents    INTEGER NOT NULL,
    fee_version     TEXT NOT NULL,
    rate_basis      TEXT,
    -- §12.6 同一订单多次查看不得重复计费
    UNIQUE (fill_id, fee_code, fee_version)
);

CREATE TABLE IF NOT EXISTS position_lot (
    lot_id          TEXT PRIMARY KEY,
    portfolio_id    TEXT NOT NULL REFERENCES portfolio(portfolio_id),
    instrument_id   TEXT NOT NULL REFERENCES instrument(instrument_id),
    acquired_trading_day TEXT NOT NULL,
    -- §12.5 T+1：当日买入批次最早可卖日为下一交易日
    earliest_sellable_day TEXT NOT NULL,
    quantity_original  INTEGER NOT NULL CHECK (quantity_original > 0),
    quantity_remaining INTEGER NOT NULL CHECK (quantity_remaining >= 0),
    cost_basis_cents_per_share INTEGER NOT NULL,
    adjusted_cost_basis_cents_per_share INTEGER,
    source_fill_id  TEXT REFERENCES fill(fill_id),
    CHECK (quantity_remaining <= quantity_original)
);

CREATE INDEX IF NOT EXISTS idx_lot_sellable
    ON position_lot (portfolio_id, instrument_id, earliest_sellable_day);

CREATE TABLE IF NOT EXISTS lot_consumption (
    consumption_id  TEXT PRIMARY KEY,
    lot_id          TEXT NOT NULL REFERENCES position_lot(lot_id),
    fill_id         TEXT NOT NULL REFERENCES fill(fill_id),
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    trading_day     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cash_entry (
    entry_id        TEXT PRIMARY KEY,
    portfolio_id    TEXT NOT NULL REFERENCES portfolio(portfolio_id),
    entry_type      TEXT NOT NULL CHECK (entry_type IN
                      ('INITIAL_DEPOSIT','TRADE_SETTLEMENT','COMMISSION','STAMP_DUTY',
                       'TRANSFER_FEE','DIVIDEND_RECEIVABLE_RECOGNIZED',
                       'DIVIDEND_RECEIVABLE_SETTLED','DIVIDEND_TAX','REVERSAL','OTHER')),
    -- §12.6 金额为整数分；符号表示方向
    amount_cents    INTEGER NOT NULL,
    trading_day     TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,
    related_fill_id TEXT REFERENCES fill(fill_id),
    related_instrument_id TEXT REFERENCES instrument(instrument_id),
    reversal_of     TEXT REFERENCES cash_entry(entry_id),
    note            TEXT
);

CREATE INDEX IF NOT EXISTS idx_cash_portfolio_day ON cash_entry (portfolio_id, trading_day);

CREATE TABLE IF NOT EXISTS receivable (
    receivable_id   TEXT PRIMARY KEY,
    portfolio_id    TEXT NOT NULL REFERENCES portfolio(portfolio_id),
    instrument_id   TEXT NOT NULL REFERENCES instrument(instrument_id),
    kind            TEXT NOT NULL CHECK (kind IN ('DIVIDEND','OTHER')),
    amount_cents    INTEGER NOT NULL,
    -- §12.6 未实现完整红利税处理前必须标注 PRE_TAX / CONSERVATIVE
    tax_treatment   TEXT NOT NULL CHECK (tax_treatment IN ('PRE_TAX','CONSERVATIVE','VERIFIED')),
    recognized_on   TEXT NOT NULL,
    expected_settlement_on TEXT NOT NULL,
    settled_on      TEXT,
    status          TEXT NOT NULL CHECK (status IN ('RECOGNIZED','SETTLED','REVERSED'))
);

CREATE TABLE IF NOT EXISTS corporate_action (
    action_id       TEXT PRIMARY KEY,
    instrument_id   TEXT NOT NULL REFERENCES instrument(instrument_id),
    action_type     TEXT NOT NULL CHECK (action_type IN
                      ('CASH_DIVIDEND','BONUS_SHARE','RIGHTS_ISSUE','SPLIT','MERGER',
                       'SPIN_OFF','DELISTING','OTHER')),
    announced_on    TEXT,
    record_date     TEXT,
    ex_date         TEXT,
    pay_date        TEXT,
    cash_per_share_cents INTEGER,
    bonus_ratio     REAL,
    rights_price_cents INTEGER,
    -- §12.7 无法正确核验的复杂公司行为必须标记为未支持，而不是近似处理
    supported       INTEGER NOT NULL DEFAULT 0 CHECK (supported IN (0,1)),
    source_id       TEXT REFERENCES source_registry(source_id),
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS valuation (
    valuation_id    TEXT PRIMARY KEY,
    portfolio_id    TEXT NOT NULL REFERENCES portfolio(portfolio_id),
    trading_day     TEXT NOT NULL,
    cash_available_cents INTEGER NOT NULL,
    cash_frozen_cents    INTEGER NOT NULL DEFAULT 0,
    receivables_cents    INTEGER NOT NULL DEFAULT 0,
    positions_value_cents INTEGER NOT NULL DEFAULT 0,
    payables_cents       INTEGER NOT NULL DEFAULT 0,
    net_value_cents      INTEGER NOT NULL,
    -- §12.8 不变量失败必须阻断净值发布
    invariant_cash_not_overdrawn    INTEGER NOT NULL CHECK (invariant_cash_not_overdrawn IN (0,1)),
    invariant_positions_not_negative INTEGER NOT NULL CHECK (invariant_positions_not_negative IN (0,1)),
    invariant_shares_match_lots     INTEGER NOT NULL CHECK (invariant_shares_match_lots IN (0,1)),
    invariant_fill_le_order         INTEGER NOT NULL CHECK (invariant_fill_le_order IN (0,1)),
    invariant_fees_booked_once      INTEGER NOT NULL CHECK (invariant_fees_booked_once IN (0,1)),
    invariant_cash_lines_sum        INTEGER NOT NULL CHECK (invariant_cash_lines_sum IN (0,1)),
    published       INTEGER NOT NULL DEFAULT 0 CHECK (published IN (0,1)),
    violations_json TEXT,
    computed_at     TEXT NOT NULL,
    UNIQUE (portfolio_id, trading_day)
);

-- 不变量未全过时禁止发布净值
CREATE TRIGGER IF NOT EXISTS trg_valuation_publish_guard
BEFORE UPDATE OF published ON valuation
WHEN NEW.published = 1 AND (
        NEW.invariant_cash_not_overdrawn = 0
     OR NEW.invariant_positions_not_negative = 0
     OR NEW.invariant_shares_match_lots = 0
     OR NEW.invariant_fill_le_order = 0
     OR NEW.invariant_fees_booked_once = 0
     OR NEW.invariant_cash_lines_sum = 0
)
BEGIN
    SELECT RAISE(ABORT, 'valuation has failing invariants; net value must not be published');
END;

CREATE TABLE IF NOT EXISTS valuation_position (
    valuation_id    TEXT NOT NULL REFERENCES valuation(valuation_id),
    instrument_id   TEXT NOT NULL REFERENCES instrument(instrument_id),
    quantity        INTEGER NOT NULL CHECK (quantity >= 0),
    price_cents     INTEGER NOT NULL CHECK (price_cents >= 0),
    -- §12.8 停牌股票用显式记录的最近有效价并标注停牌天数，禁止编造当日行情
    price_basis     TEXT NOT NULL CHECK (price_basis IN ('CLOSE','SUSPENDED_LAST_VALID','UNSUPPORTED')),
    staleness_days  INTEGER NOT NULL DEFAULT 0 CHECK (staleness_days >= 0),
    value_cents     INTEGER NOT NULL,
    PRIMARY KEY (valuation_id, instrument_id)
);

-- ============================================================
-- 7. 只读研究视图
-- ============================================================
CREATE VIEW IF NOT EXISTS v_sellable_lots AS
SELECT  l.portfolio_id,
        l.instrument_id,
        l.lot_id,
        l.quantity_remaining,
        l.earliest_sellable_day,
        l.acquired_trading_day
FROM    position_lot l
WHERE   l.quantity_remaining > 0;

CREATE VIEW IF NOT EXISTS v_portfolio_cash AS
SELECT  portfolio_id,
        SUM(amount_cents) AS cash_cents
FROM    cash_entry
GROUP BY portfolio_id;

-- ============================================================
-- 8. 迁移登记
-- ============================================================
INSERT OR IGNORE INTO schema_migration (version, applied_at, note)
VALUES ('001_metadata', strftime('%Y-%m-%dT%H:%M:%SZ','now'),
        'A-Quant Lab 元数据与模拟账本基础结构（主文档 §15.1）');
