# A-Quant Lab × agentctl：接入评估与开发补充（可执行化版）

**版本：v0.2.2（在 v0.2.1 基础上的可执行化补充，不是产品已实现版本）**
**编制日期：2026-09-13**
**适用主文档：《A-Quant Lab 开发文档 v0.2》、增补文档 v0.2.1**
**本次核验基座源码：`Jy027234/Agent`，`main` 在本次读取时为 `e63e3d8c872d1afc1869621a64425967cb072be1`（2026-09-13T02:39Z）。**

> 本版不改写 v0.2.1 的架构结论，只做三件事：
> 1. 用 GitHub 工具复核 v0.2.1 锁定的基座提交与引用路径，标出已失效的假设；
> 2. 把 Q0–Q5 从"任务描述"落到"可执行卡片"（前置条件、步骤、命令/工件、完成判据）；
> 3. 把 A01–A16 验收映射到测试骨架，并给出数据源层的决策框架与本次插件实测结论。
>
> 本次核验为文档与源码静态核对 + 一次金融数据插件连通性实测。没有登录部署服务器、没有运行 agentctl 服务、没有跑通任何真实接入调用。文中工作量数字均为估算，需在 Q0 后校准。

## 0. 本次核验记录（2026-09-13）

### 0.1 基座提交已前移，v0.2.1 的锁定提交失效

v0.2.1 锁定 `7e94058f96a4de4b27552a3c46c4529043e1d7e1`（2026-09-11T01:22Z）。本次读取时 `main` 已在 `e63e3d8c`，中间新增 5 个提交：

| 提交 | 日期(UTC) | 内容 | 对本项目的影响 |
|---|---|---|---|
| `e63e3d8c` | 09-13 02:39 | docs: 记录分支合并就绪与可选 worker 验证限制 | 部署验证范围有更新，Q0 需重读 |
| `454beef0` | 09-13 02:24 | fix(deepseek): 转换受限 thinking 请求控制 | 模型调用行为变化，影响 Q2 抽取任务 |
| `05e111d2` | 09-13 02:24 | docs(integration): 复用当前评估并限定验证范围 | 接入评估口径更新 |
| `ba6d0041` | 09-11 08:49 | fix(frontdesk): 按当前请求对工具排序 | **直接影响 assist 工具选择行为** |
| `e38fb544` | 09-11 07:59 | fix(frontdesk): 搜索缺省声明保持 grounded | 影响助手回答的证据口径 |

结论：两天内基座前移 5 个提交，其中 2 个是 frontdesk 行为修复，正好落在本项目依赖的 assist 路径上。**Q0 的第一步必须是重新锁定提交并重读 diff，不能沿用 v0.2.1 的静态核对结论直接开发。**

### 0.2 v0.2.1 引用的源码路径在当前 main 上仍然有效

本次逐一核验，以下路径在 `main` 上均存在：[C2] `docs/AGENTCTL_PREFERRED_SDK.md`；[C3] `src/agentctl/embed/__init__.py`（同目录另有 `kit.py / mcp.py / preferred.py / module_catalog.py / _shared.py / _deprecations.py`）；[C4] `src/agentctl/product_integration/capability_apply.py`（同目录另有 `capability_manifest.py / models.py / stores.py / adoption.py`）。v0.2.1 第 2 章的接入基础判断方向仍成立，但细节需以新锁定提交重读。

### 0.3 当前 SDK 文档相比 v0.2.1 引用时的关键新增事实

本次重读 `AGENTCTL_PREFERRED_SDK.md`（当前 main 版本），以下事实应补入设计：

1. **持续交互的控制契约已具体化**：恢复/取消/回答/计划控制通过 `invoke(control=FrontdeskRunControlV1)` 与 `status(run=FrontdeskRunQueryV1)` 完成，且控制调用只支持 `response_protocol="native"`，不能与普通消息的 `application_events_v1` 混用。v0.2.1 §5.2 的"按当前 SDK 的控制契约回答"现在有了确切类型名。
2. **断线续传有了明确的 fail-closed 开关**：需要断线续传的调用方必须使用 `parse_application_event_page(..., require_conversation_replay=True)`，在部署提供 `conversation_replay` 传输前 fail closed。A14 验收可直接以此为准。
3. **响应体上限与错误类型已命名**：首选客户端默认最多读取 16 MiB 响应体，超限在 JSON 解码前抛 `AgentctlResponseTooLargeError`（异常不含响应正文）；非 2xx 抛 `AgentctlHTTPError`（异常字符串不回显错误体）。适配层 §5.3 的"响应体上限"可直接引用这两个类型。
4. **旧客户端正式弃用**：直接构造 `AgentctlClient(...)` 自 `0.2.0b2` 起产生 `AgentctlSDKDeprecationWarning`；`admin.legacy` 是唯一显式逃生口。适配层不得再新引入旧客户端。
5. **合同等级**：SDK 文档自述为 "Beta/controlled-pilot 合同"，源码测试通过不代表公开发行、clean-install、外部后端与容量已验证。这与 v0.2.1 的谨慎口径一致，且进一步支持"Q0 先做部署核验"。

### 0.4 金融数据插件实测结论（诚实记录）

本次会话对东方财富妙想数据插件（`mx_finance_data`，覆盖 A 股/港/美行情、财务估值、公告等）做了连通性实测：CLI 本体可正常启动，但对数据网关的两次实际查询（上证指数近 5 日行情、贵州茅台最新收盘价）均在 120s/280s 超时未返回。**本次未能取回任何实时数据，不据此声称数据通路可用。**

对本项目的推论：

- 该类交互式金融数据插件定位为**研究辅助工具**（人工核验、Q2 证据抽取的材料交叉验证、研究卡手工抽查），**不是** A-Quant Lab 数据接入层的生产管道。生产管道需要 SLA、批量许可与可重放的时点版本，交互式插件均不提供。
- 数据接入层（快照、交易日历、因子输入）的供应商选择仍是 Q1 前必须关闭的开放决策，见本文 §4。
- 若后续要在研究流里使用此类插件，须先解决本次观察到的网关超时问题，并把"插件不可用"纳入 A10 降级用例。

## 1. Q0–Q5 可执行卡片

每张卡片：前置条件 → 执行步骤 → 必交工件 → 完成判据 → 工作量估算（人日，Q0 后校准）。

### Q0 锁定部署与可接入性（预估 3–5 人日）

**前置条件**：可访问部署服务器的只读账号；agentctl 服务地址；一个无私人持仓的合成租户。

**执行步骤**：
1. 重新锁定基座提交（当前候选 `e63e3d8c`，以执行日 `main` 为准），`git diff 7e94058f..<new>` 逐条过 §0.1 表中 frontdesk/deepseek 相关变更。
2. 核验服务端实际运行版本与 profile（Lite Gateway / Lite Assist / Lite Growth），与锁定提交的部署文档比对；落后则先定升级范围。
3. 用首选 SDK 跑通最小闭环：
   ```python
   from agentctl.embed import connect, static_tenant
   client = connect(base_url, mode="assist", api_key=..., tenant_resolver=static_tenant("aquant-synthetic"))
   health = client.status()   # 比对客户端 mode 与服务端 Lite mode
   resp = client.invoke({"user_id": "syn-1"}, user_id="syn-1", text="ping", permission_scope=[])
   client.close()
   ```
4. 负向核验：无 token、错 tenant、越权 scope 各发一次，确认均被拒绝且拒绝语义可区分。
5. 核验 manifest 加载方式：`agentctl integration validate`（仅文件校验）+ 一次 `dry_run=True`（须传入显式已有 store）。
6. 核验 Core 签名上下文是否在目标部署可用；不可用则记录哪些操作降级。

**必交工件**：`docs/integration/q0-readiness-report.md`，含锁定提交、diff 摘要、服务端 profile 证据、正/负向调用记录、未关闭事项清单。

**完成判据**：真实身份通过；无权请求被拒且错误类型可区分（`AgentctlHTTPError` 及公开错误体）；`status` 返回与服务端一致；报告可被他人在同等权限下复核。

### Q1 适配层与一条只读能力（预估 5–8 人日）

**前置条件**：Q0 报告关闭；合成快照数据（固定的 `snapshot_id`，下文 §4.3 的合成数据集）。

**执行步骤**：
1. 产品仓库建立骨架：
   ```text
   adapters/agentctl/
     client.py          # 唯一封装 connect/invoke/status/close；仅依赖 *ClientProtocol
     errors.py          # 映射 AgentctlHTTPError / AgentctlResponseTooLargeError / 超时
     config.py          # base_url、token env、max_response_body_bytes、超时、并发上限
   capabilities/
     aquant.capabilities.yaml   # agentctl.product_capabilities.v1 manifest（见 §2）
     handlers/
       research_card.py # aquant.research_card.read 的受控 handler
   runtime.config.yaml
   tests/acceptance/
   ```
2. handler 实现：输入校验（证券 ID、snapshot_id、主体范围）→ 只读查询合成快照 → 结构化输出；handler 内不做任何写、不临时抓取供应商。
3. 部署阶段执行 `assistant.apply("capabilities/aquant.capabilities.yaml", runtime_config="runtime.config.yaml")`，确认返回 `runtime_activation=startup_or_reload_required`，重载后 smoke。
4. 通过 assist 发一条需要该工具的研究问题，确认回答引用了真实工具结果而非预写文字。

**必交工件**：上述代码 + apply 的 CLI JSON 证据 + smoke 记录。

**完成判据**：同快照两次读取结果一致；篡改 tenant/instrument/权限被拒；重载后能力真实可用；`status` 中 mode 一致。

### Q2 AI 证据研究（预估 8–12 人日）

**前置条件**：Q1 关闭；合成材料库（公告/新闻样本，含一份"恶意注入"样本：文本中要求忽略权限、改仓、泄密）。

**执行步骤**：
1. 抽取任务定义：输入 = 受许可材料 + 固定 snapshot 结构化数值；输出 = JSON Schema 约束的证据卡（来源、引用片段、影响假设、反证、时点）。
2. 输出验证管线：schema 校验 → 引用可定位性校验（片段须能在原材料中定位）→ 时点校验（材料时间 ≤ `as_of_time`）→ 实体校验（证券 ID 在快照覆盖内）。任一失败不发布。
3. 原始模型输出连同内容哈希归档（关联 `research_run_id / snapshot_id / as_of_time / 模型与提示版本`）。
4. 降级路径：模型超时/额度拒绝/网关错误时，返回既有数值基线 + 明确失败标记；测试 `AgentctlResponseTooLargeError` 与超时分支。
5. 恶意材料测试：注入样本不得触发任何工具写入。

**必交工件**：抽取 handler、验证管线、归档存储、恶意样本测试记录。

**完成判据**：对应 A05/A06/A10/A15；失败路径不产生伪证据；归档哈希可复算。

### Q3 模拟预览与产品确认（预估 5–8 人日）

**前置条件**：Q1 关闭；合成账户（含持仓批次与现金）。

**执行步骤**：
1. `aquant.simulation_plan.preview` handler：只算不冻——规则检查、预计费用、草稿标记；输出明确"未冻结"。
2. `aquant.portfolio.read`、`aquant.watchlist.add`（幂等键 + 审计）。
3. 产品确认入口（独立于模型）：确认时再次校验计划版本、快照版本、账户状态版本、确认主体、有效期；过期/变更即拒绝并要求重新预览。
4. 重复提交测试：同一幂等键 N 次 → 仅一次入账。

**必交工件**：三个 handler + 确认接口 + 幂等去重存储。

**完成判据**：A07/A08/A09 全过；模型无冻结/写账工具权限（manifest 层面就不存在这些能力）。

### Q4 作业与追踪联动（预估 5–8 人日）

**前置条件**：Q2 关闭。

**执行步骤**：
1. `aquant.experiment.submit` / `aquant.job.status` handler；提交即返 job ID，不用单请求等完回测。
2. 关联表：产品 `job_id / research_run_id / decision_id` ↔ 基座 `run_id / trace_id`，显式一对一/一对多，不互相伪造完成。
3. 调度所有权：每日批次入口唯一（产品作业入口），基座不做第二份日常调度。
4. 回调健壮性：重复回调、乱序回调、取消请求按领域状态处理；状态机覆盖 排队/运行/等待确认/阻断/失败/取消/完成。

**必交工件**：两个 handler、关联表 schema、状态机实现与测试。

**完成判据**：A13/A14 全过；七种状态在 UI 与 API 可区分。

### Q5 运行与研究双重验收（预估 5–8 人日）

**前置条件**：Q1–Q4 关闭。

**执行步骤**：执行 §3 的 A01–A16 全量接入验收，同时回归 v0.2 的时点、模拟成交、费用、公司行为、资金对账黄金用例；接入验收与量化领域验收分别出报告，互不替代。

**必交工件**：`q5-acceptance-report.md`（接入侧）+ 量化领域回归报告。

## 2. manifest 骨架（Q1 直接可用）

以下为 `aquant.research_card.read` 的骨架示例。**字段名、枚举值与必填项必须以 Q0 锁定版本的 `agentctl.product_capabilities.v1` schema 校验为准**，此处仅固定本产品侧的业务语义：

```yaml
schema_version: agentctl.product_capabilities.v1
product:
  id: aquant_lab
  display_name: A-Quant Lab
capabilities:
  - name: aquant.research_card.read
    kind: read
    description: 读取指定证券在固定快照下的研究卡、因子依据与限制
    input_schema:
      type: object
      required: [instrument_id, snapshot_id]
      properties:
        instrument_id: { type: string }
        snapshot_id:   { type: string }
    output_schema:
      type: object
      required: [instrument_id, snapshot_id, as_of_time, factors, limitations]
    scopes: ["aquant.research.read"]
    risk: low
    data_sensitivity: D2
    confirmation: none
    idempotent: true
    side_effects: none
    handler:
      type: http
      endpoint: "${AQUANT_API_BASE}/internal/capabilities/research_card.read"
      auth: service_signature        # 具体签名合同按 Q0 锁定代码补齐
    timeout_ms: 8000
```

其余七个能力的差异化字段（骨架相同，仅列关键项）：

| 能力 | kind | side_effects | confirmation | idempotent | 关键限制 |
|---|---|---|---|---|---|
| `aquant.market_snapshot.read` | read | none | none | true | 读取中禁止临时抓取供应商 |
| `aquant.event_evidence.read` | read | none | none | true | 校验材料授权与时点 |
| `aquant.portfolio.read` | read | none | none | true | 主体范围=本人组合 |
| `aquant.simulation_plan.preview` | compute | none（草稿须显式另存） | none | true | 不冻结、不记成交 |
| `aquant.experiment.submit` | job | 创建实验 job | policy | 业务幂等键 | 受控配置与额度 |
| `aquant.job.status` | read | none | none | true | 排队不得显示为完成 |
| `aquant.watchlist.add` | write | 自选写入 | user_intent | 业务幂等键 | 不产生交易 |

**不在 manifest 中出现的能力**（即模型在机制上无权调用）：`execute_order`、`write_ledger`、任意 SQL、任意 Shell、任意 URL 抓取、计划冻结。

## 3. A01–A16 测试落地映射

建议目录 `tests/acceptance/`，每个用例一个文件，共享 fixture：合成租户 `aquant-synthetic`、合成快照 `snap-syn-001`、合成账户 `acct-syn-001`、恶意材料样本、基座不可用的故障注入开关（代理层断连）。

| 用例 | 建议测试文件 | 关键断言 |
|---|---|---|
| A01 | `test_a01_profile_mismatch.py` | SDK mode ≠ 服务端 profile → 明确拒绝，客户端未静默切换 |
| A02 | `test_a02_activation_reload.py` | apply 返回 `startup_or_reload_required`；重载前调用失败，重载后 smoke 通过 |
| A03 | `test_a03_tenant_forgery.py` | 浏览器侧篡改 tenant/权限 → 服务端映射拒绝 |
| A04 | `test_a04_real_tool_call.py` | 回答含 snapshot_id 与执行关联，可与归档对账 |
| A05 | `test_a05_malicious_material.py` | 注入材料未触发任何写 handler 调用（调用日志为证） |
| A06 | `test_a06_model_claims.py` | 模型自称 completed/approved 时领域状态不变 |
| A07 | `test_a07_idempotent_submit.py` | 同键 10 次提交 → 同一 job_id，计算只启动一次 |
| A08 | `test_a08_preview_no_fill.py` | 预览后成交表与现金表 diff 为空 |
| A09 | `test_a09_stale_confirmation.py` | 确认时快照/账户版本已变 → 拒绝并提示重新预览 |
| A10 | `test_a10_ai_outage.py` | 故障注入下 AI 部分显示真实失败；确定性只读按策略可用 |
| A11 | `test_a11_point_in_time.py` | 旧时点实验检索被门禁拦截，实验数据未污染 |
| A12 | `test_a12_replay.py` | 固定输入 + 归档输出回放，量化产物哈希一致 |
| A13 | `test_a13_callback_chaos.py` | 重复/乱序/取消回调后状态机不倒退、账本无误回滚 |
| A14 | `test_a14_session_recovery.py` | 重开助手后历史来自持久记录；`require_conversation_replay=True` 在无 replay 传输时 fail closed |
| A15 | `test_a15_secret_scan.py` | 前端包、日志、manifest、产物中扫描无明文密钥 |
| A16 | `test_a16_crash_recovery.py` | 杀进程重启后快照与账本一致，任务重试不重复入账 |

## 4. 数据源层决策框架（Q1 前必须关闭）

v0.2.1 §6 的"抓取/校验/归档/发布快照"依赖一个尚未选定的行情与基本面数据源。本节给出决策框架与本次实测证据，不替产品做决定。

### 4.1 候选路线对比

| 路线 | 举例 | 时点版本/可重放 | 批量许可 | 稳定性/SLA | 成本 | 适配结论 |
|---|---|---|---|---|---|---|
| 商业终端/数据 API | Wind、iFinD、东财 Choice | 好 | 需购买批量授权 | 好 | 高 | 生产首选候选 |
| 交易软件/券商接口 | 券商行情 API | 中 | 限本人使用 | 中 | 低 | 个人规模可评估，注意许可 |
| 开源抓取库 | akshare / tushare / baostock | 弱-中（需自建归档） | 各自源站条款 | 弱-中 | 低 | 原型可用，生产需自建快照归档补强 |
| 交互式 AI 数据插件 | 本会话东财妙想插件 | 不提供 | 不提供 | 本次实测网关超时 | - | **仅作研究辅助与人工核验，不作生产管道** |

### 4.2 决策检查单（选定供应商前逐项回答）

1. 是否提供历史时点（point-in-time）数据，还是只能查"最新"？这直接决定 §7.1/7.2 的时点门禁能否成立。
2. 许可是否允许批量落库、快照归档与在研究产物中引用数值？
3. 交易日历、停牌、公司行为（分红送配）的覆盖与修正机制？
4. 数据发布时点：收盘后多久齐全？§6 的"数据就绪门禁"按此定义，不能凭"收盘"假设。
5. 故障时的人工补救流程（补数、重发快照、更正记录）？

### 4.3 不依赖供应商的前置产物：合成数据集

Q0–Q2 不应等供应商合同。先用一份**自造的合成数据集**（固定证券、固定快照、已知正确答案的行情/财务/材料），使 Q1 的"同快照读取一致"、A04/A08/A12 等用例可以确定性运行。真实供应商接入作为独立任务排在 Q3 前后，接入后用同一批验收用例重跑（黄金用例不变，数据源替换）。

## 5. 基座版本管理规程

本次核验证明基座两天可前移 5 个提交且含行为修复。建议：

1. **锁定**：Q0 锁定提交写入 `adapters/agentctl/LOCKED_BASE`（含提交 SHA、锁定日期、diff 摘要）。
2. **监测**：每周（或每次开发迭代启动时）比对 `main`；有 frontdesk / embed / product_integration / model-gateway 路径变更即触发重读。
3. **升级**：升级基座 = 显式任务：更新 LOCKED_BASE → 重读变更 → 重跑 A01–A16 中受影响的用例 → 更新本文 §0 核验记录。
4. **禁止**：不得以"pull 最新"方式隐式升级；不得在未重跑验收时宣称兼容。

## 6. 落地顺序与时间盒

```text
第 1 周    Q0（重锁提交 + 部署核验 + readiness 报告）
第 2–3 周  Q1（适配层 + research_card.read + manifest apply 冒烟）
第 3–5 周  Q2（证据研究管线 + 恶意样本与降级测试）
第 5–7 周  Q3（预览/组合/自选 + 确认闭环）｜ 并行：供应商决策（§4）
第 7–9 周  Q4（实验/作业 + 追踪关联 + 状态机）
第 9–10 周 Q5（A01–A16 + 量化领域黄金用例双重验收）
```

时间盒为估算，前提是一个人全职且 Q0 无重大部署缺口；Q0 报告出来后应立即校准本表。

## 7. 与 v0.2.1 的对应关系

| v0.2.1 章节 | 本版动作 |
|---|---|
| §1–§3 架构与职责 | 不变；§0.3 补充 SDK 新事实 |
| §4 第一批能力 | §2 给出 manifest 骨架与字段表 |
| §5 SDK 与部署约束 | §0.3 落实具体类型名；§5 增加版本管理规程 |
| §6 每日研究流程 | §4.2 落实数据就绪门禁的决策检查单 |
| §9 Q0–Q5 | §1 展开为可执行卡片 |
| §10 A01–A16 | §3 映射到测试文件与断言 |

本文件为增补设计，未覆盖或改写 v0.2 与 v0.2.1 原文件，也未修改 GitHub 仓库。
