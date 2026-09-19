# Q5 接入侧验收报告

> 生成时间：2026-09-19T11:45:18.223101+00:00
> 本报告只引用本次 runner 新启动的 server 与新生成的去敏证据；历史 Q0/Q5 文件不作为本次结论输入。

## 实测范围

- 拓扑：live_http；端口：127.0.0.1:18765。
- 配置来源：capabilities/agentctl.capabilities.yaml 与 deploy/agentctl-q0/runtime.config.yaml。运行时 store 使用临时目录副本，避免污染既有 Q0 实例。
- 凭证：本次创建临时 master、产品受限令牌和低权限令牌；报告不保存令牌值、哈希、ID 或 HMAC；结束时撤销两个受限令牌。
- live_topology 才计入接入侧覆盖；offline_contract 只作为辅助证据，不能冒充真实拓扑。

## agentctl 基座限制

- **基座漂移阻断**：LOCKED_BASE=b5cad04836cf93b35750fb0116ab0c2d936d1f89，当前 checkout=f10bb6ac42d4a30a9a521194fb27a950e7384456，src/agentctl 已变更 11 个文件；本次 A01–A16 观察仅代表当前 checkout，不能默认为锁定基线兼容，需显式升级基座后重跑受影响用例。

## A01–A16 覆盖矩阵

| 用例 | 结论 | 证据范围 | 观察 |
|---|---|---|---|
| A01 | uncovered | live_topology | 仅观察到 profile mismatch，未观察到明确拒绝 |
| A02 | failed | offline_contract | manifest doctor 失败 |
| A03 | passed | live_topology | 边界拒绝与正确租户路径均符合预期 |
| A04 | passed | live_topology | 研究能力通过 agentctl 返回真实 snapshot、数据和执行关联 |
| A05 | passed | live_topology | 恶意证据作为受授权数据返回，引用被权限门禁屏蔽且产品状态未写入 |
| A06 | uncovered | live_topology | 模型请求未完成，暂未形成声明与组合状态不变的完整实时证据 |
| A07 | passed | live_topology | 十次并发提交收敛到同一产品 job，状态查询成功且计算尚未启动 |
| A08 | passed | live_topology | 预览通过真实 agentctl HTTP 入口且产品元数据库全部用户表指纹不变 |
| A09 | uncovered | not_executed | 未执行：真实 agentctl 入口被结构性阻断 |
| A10 | passed | live_topology | 模型失败分支保留真实失败语义 |
| A11 | passed | live_topology | 当前事件被旧快照 PIT 门禁拦截，旧实验读取与产品状态均未污染 |
| A12 | passed | offline_contract | 真实产品 assistant 入口按固定输入回放归档输出，受信任主体隔离、结构化量化产物哈希一致且回放单独登记 |
| A13 | passed | live_topology | 跨进程取消、迟到、重复与乱序回调均被状态机拒绝，非作业表未变化 |
| A14 | uncovered | live_topology | 真实 agentctl turn_projection 已 fail closed；缺少 Core replay 传输，A14 未覆盖 |
| A15 | passed | offline_contract | 运行日志、构建产物、最终 evidence 与 report 均无已知密钥模式 |
| A16 | passed | live_topology | 崩溃租约由新进程接管并完成真实因子写入；重复提交与执行未改变产品库 |

真实拓扑覆盖：9/12 通过；未覆盖：A01, A06, A09, A14；已执行失败：A02。

## 证据与限制

- A03 复用了 tests/acceptance/q0_probe.py 与 tests/acceptance/q0_enforcement_probe.py，并通过本次临时受限令牌验证 tenant、product、scope 和签名上下文边界。
- A01 只有 mode_compatible=false 且 live invoke 收到服务端 HTTP 4xx 才记为明确拒绝；状态标记、客户端异常、网络错误或 5xx 均不算拒绝。
- A04 通过真实 `/frontdesk/capabilities/invoke` 调用研究 handler；只有返回真实 snapshot、数据和 invocation/trace/idempotency 关联时才算覆盖。
- A05–A09 的真实拓扑结论按上方覆盖矩阵记录；仅未覆盖项依据实际 manifest 与 onboarding 事实记录阻断，离线领域测试或样例数据不能冒充 agentctl 真实拓扑通过。
- A10–A16 仅按各行本次证据计级；其中跨进程状态演练必须同时经过持久 JobStore 与 live job.status，离线领域测试不会被自动升级。
- A12 的主体隔离只验证产品的受信任主体映射合同；当前演示 X-Aquant-Subject 头不是生产认证，本项也未声明为 agentctl live capability。
- 本报告与量化领域回归报告分开，不能互相替代。

## A05–A09 真实拓扑阻断

- **A09**：当前 onboarding 只绑定 frontdesk.message，清单也明确不声明计划冻结；没有可供 agentctl 调用的确认/冻结入口，无法在真实拓扑中提交陈旧确认。
  - blocker：`product_entrypoint_not_bound`；所需能力：`aquant.simulation_plan.preview`；当前缺失：无（需核对产品入口）。
  - 需要的真实入口：product-owned confirmation/freeze HTTP entrypoint bound to agentctl。

## A14 会话重开边界

- **A14**：当前 agentctl server 只返回 turn_projection；未配置真实 Core conversation_replay 传输，A14 必须 fail closed。
  - blocker：`conversation_replay_transport_unconfigured`；真实入口：Platform Core application-conversation publish + paginated GET。
  - `turn_projection` 只能用于当前响应；只有 Core 返回 `conversation_replay` 且独立分页读取成功，才可记为通过。

## 凭证清理

- 临时受限令牌已撤销：True。
- server 已停止：True。
- 运行日志、构建产物（若存在）、最终 evidence 与 report 均纳入 A15 扫描；evidence 与 report 均经过凭证字段检查：token_value_exposed=false。
