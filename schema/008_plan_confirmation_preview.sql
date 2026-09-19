-- 跨进程的产品确认桥只保存服务端已签发的完整预览。
--
-- 明文确认令牌仍只返回给用户界面；这里保存的是预览 JSON，令牌表只保存
-- 哈希。agentctl 受信用户 handler 可以据此恢复同一份预览，再进入现有
-- PlanService.freeze 五项复核，模型没有任何直接冻结入口。
CREATE TABLE IF NOT EXISTS plan_confirmation_preview (
    confirmation_id TEXT PRIMARY KEY REFERENCES plan_confirmation(confirmation_id),
    plan_id         TEXT NOT NULL,
    preview_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plan_confirmation_preview_plan
    ON plan_confirmation_preview (plan_id, created_at);
