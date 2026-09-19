# Q5 A14：助手会话重开与持久回放

产品侧的接线位于 `src/aquant/adapters/agentctl/conversation_replay.py`。
Frontdesk 的 `response_protocol="application_events_v1"` 只用于解析当前响应的
`turn_projection`。需要重开助手时，调用方必须再经过
`require_conversation_replay(...)`；如果页面没有 `conversation_replay` scope，调用
会以 `CONVERSATION_REPLAY_UNAVAILABLE` fail closed。

真实持久路径是：

1. `AssistClient.invoke(..., conversation_id=..., response_protocol="application_events_v1")` 返回一个经过 native SDK 校验的 turn projection。
2. 通过 `PlatformCoreApplicationConversationTransport.publish(...)` 交给 Platform Core 保存；产品不在本地把 projection 改标成 replay。
3. 助手重开后，使用 Core 的 `/application-conversations/{product_id}/{conversation_id}/events` 分页入口重新读取，并逐页验证租户、产品、会话、cursor、sequence、gap 与 `conversation_replay` scope。

Q5 runner 的 A14 使用同一条真实 Frontdesk HTTP 路径。只有关闭原 assistant client
后，独立的 Core 分页读取返回持久 replay 事件，才记为 `live_topology/passed`。
只有 Core publish transport（地址或服务令牌）或独立 reader（读取令牌）未配置时，
runner 才会记录结构化 blocker 并保持 `uncovered`。一旦这些边界已配置，publish、
read、分页合同或事件证据的任何失败都记为 `failed`；本地内存页、静态 fixture、
turn projection 或产品侧缓存都不会升级为 A14 通过。恢复事件还必须同时匹配本次
`request_id` 与 `conversation_id`。

当前外部 agentctl checkout 提供 SDK parser 与 publisher 类型，但运行中的 Lite
server 未提供 Core conversation endpoint。因此本机 Q5 拓扑可证明产品侧严格拒绝
缺失 replay 传输，却不能宣称 A14 已完成。部署 Core transport 后，设置以下运行时配置
再执行 Q5 runner：

* `AQUANT_CONVERSATION_REPLAY_BASE_URL`
* `AQUANT_CONVERSATION_REPLAY_SERVICE_TOKEN`
* `AQUANT_CONVERSATION_REPLAY_READ_TOKEN`
* 可选：`AQUANT_CONVERSATION_REPLAY_READ_AUTH_HEADER`、`AQUANT_CONVERSATION_REPLAY_READ_AUTH_PREFIX`
