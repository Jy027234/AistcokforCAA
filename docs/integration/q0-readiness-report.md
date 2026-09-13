# Q0 接入就绪报告：A-Quant Lab × agentctl

**状态：部分完成（存在未关闭项，不可直接进入 Q1）**
**编制日期：2026-09-13**
**执行者：DSH 编码助手（本机实测，非文档转述）**
**适用文件：`A-Quant-Lab_开发文档_v0.2.md`、接入补充 v0.2.1 / v0.2.2**

> 本报告只记录**本次实际执行并观察到**的结果。凡未执行的核验一律标注"未验证"，不以文档陈述、仓库既有证据或推断冒充通过。基线部署为本机隔离实例（`deploy/agentctl-q0`），**不是**用户生产部署；生产部署的 profile 与服务端版本在接入前仍需按同等步骤复核。

---

## 1. 结论摘要

| 项目 | 结果 |
|---|---|
| 基座提交锁定 | ✅ 已锁定 `b5cad04`（非 v0.2.2 的 `e63e3d8c`） |
| v0.2.1 锁定提交可复核 | ✅ `7e94058f` 在本地仓库存在，diff 可复现 |
| 关键源码路径有效性 | ✅ v0.2.2 §0.2 声称的 6 条路径逐条实测存在 |
| Lite Assist 实例可启动 | ✅ 隔离实例 + SQLite 存储 + 服务令牌签发成功 |
| SDK 正向最小闭环 | ❌ **未通过**：无模型凭证，Run 全部失败 |
| 越权/伪造负向核验 | ❌ **未通过**：伪造 tenant 被接受并落库 |
| manifest 加载方式 | ⏳ 未验证 |
| Core 签名上下文 | ⏳ 未验证 |
| **能否进入 Q1** | ❌ **不能**，须先关闭 §5 的 O1–O3 |

---

## 2. 基座提交锁定

| 项 | 值 |
|---|---|
| 仓库 | `E:\IT\Agent`（origin = `https://github.com/Jy027234/Agent.git`） |
| 分支 | `main` |
| **本次锁定提交** | `b5cad04836cf93b35750fb0116ab0c2d936d1f89` |
| 提交时间 | 2026-09-13T19:41:03+08:00 |
| 提交标题 | `feat: govern northbound MCP presentation access` |
| 运行时包版本 | `agentctl 0.2.0b5`` |

v0.2.1 锁定 `7e94058f`（09-11），v0.2.2 锁定 `e63e3d8c`（09-13）——**两个提交在本地仓库均可解析（`git cat-file -t` 返回 `commit`）**，diff 可复现。v0.2.2 之后 `main` 又前移 1 个提交，故本次锁定 `b5cad04`。

### 2.1 `7e94058f..b5cad04` 变更清单（实测 numstat）

```text
16    0   src/agentctl/adapters/llm/openai_adapter.py
10    3   src/agentctl/frontdesk/continuous_assist.py
25   25   src/agentctl/product_integration/adoption.py
 4    1   src/agentctl/server/handlers/frontdesk_continuous.py
52    6   src/agentctl/server/mcp.py
538   0   src/agentctl/server/mcp_northbound.py        (新增)
125  17   src/agentctl/server/mcp_official.py
60    3   src/agentctl/server/mcp_projection.py
```

注意：v0.2.2 §0.1 表中列出的 5 个提交，其**最终文件级影响**并非 5 个提交的简单叠加。实际触碰 `frontdesk/embed/product_integration` 的只有 `continuous_assist.py`、`adoption.py`、`frontdesk_continuous.py` 三个文件。

### 2.2 v0.2.2 §0.1 声称的两个 frontdesk 修复——实测确认

| 提交 | 内容 | 实测 |
|---|---|---|
| `ba6d004` | `fix(frontdesk): rank tools by current request` | ✅ `CONTINUOUS_ASSIST_VERSION` 1.2.4 → 1.2.5 |
| `e38fb54` | `fix(frontdesk): keep search absence claims grounded` | ✅ 1.2.3 → 1.2.4，提示词新增反"缺席断言"与工具参数分解约束 |

当前锁定版本 `CONTINUOUS_ASSIST_VERSION = "1.2.5"`（`src/agentctl/frontdesk/continuous_assist.py`）。

**对 Q2 的影响（正向）**：`e38fb54` 明文要求"若使用推断或过窄过滤条件搜索无结果，不得声称记录或事实不存在；仅在明确穷尽性权威查询下才可支持缺席结论"、"工具参数必须从请求或选中对象上下文的对应字段构造，不得把公司/对象/主题/版本/日期拼接成猜测的字面量过滤器"。这与 v0.2.1 §7.5、v0.2.2 Q2"失败路径不产生伪证据"直接同向，**本项目在证据抽取任务上可直接受益，不需要自行发明该约束**。

### 2.3 新增 northbound MCP 治理面（`b5cad04`，v0.2.2 未涵盖）

新增 `src/agentctl/server/mcp_northbound.py`（538 行）与 `docs/AGENTCTL_NORTHBOUND_MCP_V2.md`。要点：

- `McpNorthboundPolicy`：网关从已验证令牌解析 `McpPrincipal`，`tools/list` / `tools/call` / `resources/list` / `resources/read` **共享同一主体边界**；策略配置后主体缺失即 fail closed。
- **工具参数绑定器可用已验证主体覆盖模型提供的 tenant/user 字段**；适配器不读取请求 `_meta` 作为身份。
- `presentation_contracts_by_tool` / `presentation_resources`：按 `agentctl.mcp.presentation.v1` 只投影显式声明的出向字段，handler 未列出的内部字段不会到达呈现面。

**对本项目的意义**：这正是 v0.2.1 §8"tenant/user 从受信任身份映射"、A03"浏览器伪造 tenant 被拒绝"所需要的机制。**但见 §5 O1：本次实测显示 HTTP frontdesk 路径尚未应用同等边界。** 若采用 MCP 北向接入，该机制可复用；若走 HTTP `/frontdesk/messages`，则不能假定已有同等保护。

---

## 3. 部署 profile 核验

### 3.1 实测证据

用修正后的 SDK 调用 `status()`，服务端自报：

```json
{
  "connected": true,
  "client_mode": "assist",
  "server_mode": "assist",
  "mode_compatible": true,
  "server": {
    "active_profile": "lite",
    "contract_version": "0.2.0b5",
    "control_plane_version": "0.1.0",
    "schema_version": "aios.v0.1",
    "descriptor": { "deployment_shape": "single node with SQLite stores, persisted scoped tokens, and real provider configuration" }
  }
}
```

客户端 mode 与服务端 Lite mode 一致，部署为**嵌入式轻量单机**，与 v0.2.1 §3.1 的目标形态一致。

### 3.2 实例与令牌

隔离实例位于 `deploy/agentctl-q0/`，从 `runtime.config.yaml`（源自基座 `.agentctl/lite-product-validation/assist.runtime.yaml`）bootstrap 而来，落盘存储：`runs / ledger / meta / runtime_dispatch / product_integration / integration / frontdesk / interaction / autonomy / control_plane / tokens` 共 11 个 SQLite。

签发令牌（仅存后端，**不得进入前端、日志或仓库**）：

```text
token_id   tok_c19edf5a32664780
tenant_id  aquant-synthetic
subject    aquant-backend
scopes     [agent.run, model_gateway.complete]
metadata.allowed_products  [aquant_lab]
expires_at 2026-09-14T15:06:30Z   (ttl 86400s)
```

### 3.3 ⚠️ 阻塞坑：Assist 模式默认配置**不满足 coordinator_v2 前置**

这是本次最耗时的排障，也是**最值得写进部署文档的一条**。

- 基座 `runtime.config.yaml` 默认 `lite_mode: gateway` + `frontdesk: disabled`；本项目需要 `assist`。
- 但即使切到 `lite_mode: assist` + `frontdesk: enabled`，**沿用 `.agentctl/lite-product-validation/assist.runtime.yaml` 仍会 100% 失败**，所有 `/frontdesk/messages` 返回 HTTP 400：
  ```json
  { "error": "read_assist_requires_coordinator_v2_without_execute_plan" }
  ```
- 根因：`src/agentctl/server/handlers/frontdesk_messages.py:90` 要求
  `api.frontdesk_engine.runtime_profile.coordinator_v2` 为真。而"权威"校验配置 `assist.runtime.yaml` **没有 `frontdesk.profile` 段**，`FrontdeskRuntimeProfile.from_config()` 因 `semantic_profile` 为空而令 `coordinator_v2` 默认 `False`。
- 对照：**示例**配置 `docs/AGENTCTL_LITE_ASSIST_RUNTIME_CONFIG.example.yaml:130-133` 是正确的，含
  ```yaml
  frontdesk:
    profile:
      name: coordinator_v2
      core_state_mode: standalone
    features:
      coordinator_v2: true
      progressive_capability_context: true
  ```
- 本项目已在 `deploy/agentctl-q0/runtime.config.yaml` 补齐该段，补齐后 frontdesk Run 才真正创建。

**建议回写基座**：把 `lite-product-validation/assist.runtime.yaml` 与示例对齐，否则任何照该文件起步的新产品都会撞同一个 400。**本报告未修改基座仓库。**

### 3.4 另一个排障坑：`job_kill` 不保证释放端口

`agentctl serve` 由 PowerShell 启动后，杀死 job 仅终止父 `agentctl` 进程，子 `python` 进程继续占用 8765 端口，导致后续探针静默打到**旧配置的实例**上，产生"改了配置但错误不变"的假象。重启实例前必须显式确认端口已释放。这条已写入 `deploy/agentctl-q0/` 运维备注。

---

## 4. 正/负向调用记录

探针脚本：`tests/acceptance/q0_probe.py`；原始证据：`deploy/agentctl-q0/q0-probe-evidence.json`。

### 4.1 v0.2.2 §1 Q0 步骤 3 的示例代码——逐字执行结果

v0.2.2 给出的片段（转录自文档）：

```python
client = connect(base_url, mode="assist", api_key=..., tenant_resolver=static_tenant("aquant-synthetic"))
health = client.status()
resp = client.invoke({"user_id": "syn-1"}, user_id="syn-1", text="ping", permission_scope=[])
```

**逐字执行结果：可以运行**（HTTP 200）。此处需要更正我在首次静态核验时形成的判断：

```text
# 实际签名（src/agentctl/embed/preferred.py:332）
def invoke(self, principal, *, user_id: str, text: str = "",
           product_id=None, caller_user_id=None, source_session_id=None,
           permission_scope=(), context_ref=None, conversation_id=None,
           conversation_context=None, context_envelope=None,
           response_protocol="native", interaction_mode="continuous_assist",
           control=None, request_id=None) -> dict
```

`text=` 与 `user_id=` **确实是合法关键字参数**，文档片段并无 API 名错误。但它**仍不足以完成闭环**，原因见 4.2。

### 4.2 正向最小闭环（P1）

| 步骤 | 观察 |
|---|---|
| `status()` | ✅ 返回 profile 与 mode，见 §3.1 |
| `invoke(...)` | ⚠️ HTTP 200，但 `status = "failed"` |

Run 记录（`runs.sqlite`，共 5 条）：

```text
agent_id  frontdesk.continuous_assist
status    failed
error     model_gateway:provider_credential_missing:deepseek:deepseek-v4-flash
```

宿主返回的 `reply` 为中文失败说明，且**未把失败或未知结果当作成功**：

> 本次任务未完成，请查看执行状态；没有将失败或未知结果视为成功。

**这一点值得记录为正面证据**：基座在模型不可用时保持了失败语义，没有伪造摘要——正是 v0.2.1 A10 与主文档 §18.3"AI 卡片显示失败，不能伪造摘要"所要求的行为。

**但 P1 作为"最小闭环"判定为未通过**：需要真实模型凭证才能完成一次可信 Run 完成。

### 4.3 负向核验——**关键发现**

| 用例 | 期望（v0.2.1 A03 / v0.2.2） | 实测 | 判定 |
|---|---|---|---|
| N1 无 token | 401 拒绝 | HTTP **401** `{"error":"unauthorized"}` | ✅ **通过** |
| N2 伪造 tenant（`aquant-attacker`） | 服务端映射拒绝 | HTTP **200 接受**，Run 落库 `tenant="aquant-attacker"` | ❌ **失败** |
| N3 越权 `permission_scope`（`ledger.write`/`execute_order`） | 拒绝 | HTTP **200 接受** | ❌ **失败** |
| N4 错误 `allowed_product`（`evil_product`） | 拒绝 | HTTP **200 接受** | ❌ **失败** |
| N5 完全无 `product_context` | 拒绝 | HTTP **200 接受** | ❌ **失败** |

**Run store 中已实际落库两个 tenant：**

```text
('aquant-attacker', 2, 'failed')
('aquant-synthetic', 8, 'failed')
```

即：**请求体中的 `tenant` 未经与令牌绑定 tenant 的一致性校验即被接受并持久化。**

- 令牌 tenant 为 `aquant-synthetic`，而 `aquant-attacker` 是一次**从未在令牌存储中签发过**的任意字符串。
- 该结果直接落在 v0.2.1 §8"tenant/user 从受信任身份映射"、§4"模型提出的 tenant、permission_scope 或对象所有者不能覆盖后端身份"与 A03 验收之上。
- 需要说明的边界：本次 Run 全部因**缺凭证**而失败，因此**无法区分**"admission 在 Run 失败前尚未执行"与"admission 未生效"。N3/N4/N5 的"接受"可能只是尚未走到准入门禁。**该歧义必须在关闭 O1 时用可用凭证重测。**
- 但 N2 不同：tenant **已被接受并落库到该 tenant 名下**，这本身就是在准入之前发生的事实。

---

## 5. 未关闭项

| 编号 | 未关闭项 | 影响 | 关闭条件 |
|---|---|---|---|
| **O1** | frontdesk HTTP 路径的 tenant / product / scope 边界未验证通过，且有伪造 tenant 落库证据 | **A03、Q0 完成判据不满足**；阻塞 Q1 | 配置真实模型凭证后重跑 §4.3；若仍接受，须在适配层强制后端 tenant 映射，或改用 `b5cad04` 的 `McpNorthboundPolicy` 路径 |
| **O2** | 无模型凭证（`DEEPSEEK_API_KEY` 等全部未设置） | 正向闭环、AI 语义质量、Q2 全部不可验证 | 提供凭证，或在受控 profile 下改用 `echo` 供应商仅验证编排（**不得据此声称模型链路通过**） |
| **O3** | 本次核验对象是**本机隔离实例**，非用户生产部署 | 生产 profile、认证与 Core 上下文仍未知 | 对生产部署重复 §3.1–3.2、§4.3 |
| **O4** | manifest 加载方式未验证（`integration validate` / `dry_run`） | Q1 步骤 3 无依据 | 执行 `agentctl integration validate --manifest ...` 与一次 `dry_run=True`（须传显式已存在 store） |
| **O5** | Core 签名上下文可用性未验证 | v0.2.1 §3.3 | 按锁定代码核验，不可用则记录降级范围 |
| **O6** | 主文档 §0 声称的资料包**全部缺失** | 见 §6 | 见 §6 |

### 5.1 A01 的实测澄清（供 Q5 使用）

v0.2.1 A01 要求"SDK 模式与服务端 profile 不匹配 → 明确拒绝，不静默切换"。实测：

| 客户端 mode | 服务端 mode | `mode_compatible` |
|---|---|---|
| `gateway` | assist | `true` |
| `assist` | assist | `true` |
| `growth` | assist | **`false`** |

因此 `mode_compatible` **不是相等判断，而是包含判断**：gateway 的能力面是 assist 的子集，故 assist 部署可服务 gateway 客户端；growth 需要 assist 不具备的能力面，故报不兼容。

**对 A01 的影响**：A01 不能写成"mode 字符串不相等即须拒绝"。应以 `mode_compatible=false` 且**调用被拒绝**为判据，需在 Q5 重写用例断言。v0.2.2 §3 的 A01 描述（"SDK mode ≠ 服务端 profile → 明确拒绝"）按字面实现会与实际契约冲突。

---

## 6. 资料包缺失（独立发现，影响 M0/M1）

主文档 §0（第 16–24 行）声明资料包另含 8 项交付物。实测工作区**只有 3 个 .md 文件**，以下**全部不存在**：

| 声明路径 | 状态 | 影响的验收 |
|---|---|---|
| `CODEX_TASKS.md` | ❌ 缺失 | §19 详细任务卡 |
| `AGENTS.md` | ❌ 缺失 | 编程助手强制约束 |
| `configs/research.example.yaml` | ❌ 缺失 | §11.2 初始参数可执行化 |
| `contracts/{event,snapshot}.schema.json` | ❌ 缺失 | §15.3/15.4 契约验证 |
| `examples/`（虚构样例） | ❌ 缺失 | §4.3 合成数据集参照 |
| `schema/001_metadata.sql` | ❌ 缺失 | §15.1 元数据与账本基础结构 |
| `tests/validate_spec.py` | ❌ 缺失 | 资料包自检 |
| `docs/SOURCES.md` | ❌ 缺失 | §23 完整检查说明 |

**后果**：
1. 主文档 §15.1 说"资料包 SQL 是元数据和账本基础结构"——该基础结构不存在，`migrations/` 需从零设计。
2. `contracts/` 缺失使 §15.3/15.4 的契约验证无法执行，D01–D08 无法起步。
3. v0.2.2 §4.3 明确要求"Q0–Q2 不应等供应商合同，先用合成数据集"，但 `examples/` 与 `validate_spec.py` 都不在，合成数据集需自行设计——**需确认是用户未放入工作区，还是本未生成**。

---

## 7. 架构口径冲突（需 ADR，非阻塞但必须记录）

v0.2.2 §7 称"§1–§3 架构与职责不变"。**逐字核对主文档后，该表述低估了差异**：

| 议题 | 主文档 v0.2 | 补充 v0.2.1/v0.2.2 |
|---|---|---|
| 是否提及 agentctl | **0 次**（`agentctl` 字面零命中） | 核心 |
| 部署基线 | §0.1："独立轻量部署，**不依赖已有企业平台底座**" | "优先复用已有 agentctl" |
| 复用路线 | §2.3 路线 3；ADR 004"自建窄领域核心" | 基座承担 AI 运行与助手能力 |
| 仓库布局 | §14.4 无 agentctl 适配层 | 新增 `adapters/agentctl/` |
| ADR | §20 共 10 条，**无一条**覆盖基座集成 | v0.2.1 §12 要求新增 ADR |
| 模型访问 | §14.1 自有窄接口 `TextModelProvider` | 生产优先接 agentctl 模型网关 |
| 工具命名 | §16.3 无前缀：`get_snapshot`、`query_research_card`… | `aquant.*` 能力 ID + manifest |
| 身份/tenant/scope | **零命中**（主文档从未定义） | 服务端 tenant 映射、scopes、service_signature |

**判定**：主文档 §0.1 自身留有衔接——"**不在未检查现有代码的情况下设计强耦合集成**"。补充文档正是"已检查现有代码"后的产物，因此接入**不违背**主文档，但**必须显式修订**：

1. §14.1 模型访问：`TextModelProvider` 保留为领域窄接口，新增 agentctl 网关为其**生产实现**（离线测试替身不变）。这一层关系与原设计兼容，无需推翻。
2. §14.4 布局：新增 `src/aquant/adapters/agentctl/`（注意：v0.2.2 写的顶层 `adapters/agentctl/` 与 §14.4 的 `src/aquant/adapters/` **不一致，需统一**）。
3. §16.3 工具名 → `aquant.*` 能力 ID 的映射表需正式落文。
4. §20 新增 ADR-011"复用运行基座但不让其接管量化事实与账本"。
5. §17 安全：新增后端 tenant 映射与 MCP 主体边界（依据 §5 O1 的实测结论）。

---

## 8. 复核方式

```powershell
# 1. 基座锁定与 diff 复现
git -C E:\IT\Agent log -1 --format='%H %cI %s'
git -C E:\IT\Agent diff --numstat 7e94058f96a4de4b27552a3c46c4529043e1d7e1..b5cad04836cf93b35750fb0116ab0c2d936d1f89 -- src/agentctl

# 2. 启动隔离实例（端口释放确认后再启动）
agentctl serve --config deploy\agentctl-q0\runtime.config.yaml --host 127.0.0.1 --port 8765 --token <TOKEN>

# 3. 正/负向探针
python tests\acceptance\q0_probe.py --base-url http://127.0.0.1:8765 --token <TOKEN> --json-out deploy\agentctl-q0\q0-probe-evidence.json
```

同等权限的第三方应能复现 §2–§4 的全部结论。

---

## 9. 声明

- 本报告**未修改基座仓库** `E:\IT\Agent`。
- 本次**未登录用户生产部署**，未跑通任何真实接入调用，未执行 Q1 及以后任务。
- §3.3 与 §3.4 的坑来自本机实测，建议回写基座文档；本报告只做记录。
- 未验证项一律标注"未验证"；不因基座测试通过而推断本项目通过验收。
