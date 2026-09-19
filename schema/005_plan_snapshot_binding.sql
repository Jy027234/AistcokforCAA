-- 决策与执行的时点绑定（004）
--
-- simulation_plan.snapshot_id 是 001 版本留下的兼容字段，历史上同时被当作
-- 决策输入和成交行情来源。新冻结计划把两种用途拆开：
--   * decision_snapshot_id / decision_cutoff_at：候选、因子和订单的依据；
--   * execution_snapshot_id / execution_cutoff_at：模拟成交所需的当日行情。
--
-- 绑定和冻结计划在同一短事务中写入，之后只读。旧计划没有绑定记录时，
-- 执行服务会明确拒绝，而不是猜一个当前快照或继续使用过期 preview。

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS plan_snapshot_binding (
    plan_id                TEXT PRIMARY KEY REFERENCES simulation_plan(plan_id),
    decision_snapshot_id   TEXT NOT NULL REFERENCES snapshot(snapshot_id),
    decision_cutoff_at     TEXT NOT NULL,
    execution_snapshot_id  TEXT NOT NULL REFERENCES snapshot(snapshot_id),
    execution_cutoff_at    TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    CHECK (length(decision_cutoff_at) > 0),
    CHECK (length(execution_cutoff_at) > 0)
);

CREATE INDEX IF NOT EXISTS idx_plan_snapshot_binding_execution
    ON plan_snapshot_binding (execution_snapshot_id, execution_cutoff_at);

CREATE TRIGGER IF NOT EXISTS trg_plan_snapshot_binding_immutable_update
BEFORE UPDATE ON plan_snapshot_binding
BEGIN
    SELECT RAISE(ABORT, 'plan snapshot binding is immutable; create a new plan version');
END;

CREATE TRIGGER IF NOT EXISTS trg_plan_snapshot_binding_no_delete
BEFORE DELETE ON plan_snapshot_binding
BEGIN
    SELECT RAISE(ABORT, 'plan snapshot binding cannot be deleted');
END;
