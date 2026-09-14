# Q0 接入就绪报告：A-Quant Lab × agentctl

**状态：Q0 通过；Q1 首个只读能力已端到端跑通**
**编制日期：2026-09-13（含使用 DeepSeek 官方模型后的复测，以及一次结论更正，见 §10 勘误）**
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
| SDK 正向最小闭环 | ✅ **通过**：Run `completed`，真实回答 + usage 记录（§4.2） |
| 越权/伪造负向核验 | ✅ **通过**：伪造 tenant / 错误 product / 缺 product_context / 低权令牌 全部被拒，错误码可区分（§4.3） |
| manifest 加载方式 | ✅ **通过**：validate / apply / doctor(18/18) / smoke 全部 PASS（§4.5） |
| 首个只读能力端到端 | ✅ **通过**：模型自主选中 `aquant.research_card.read` 并真实执行（§4.6） |
| Core 签名上下文 | ⏳ 未验证（O5，本轮未涉及） |
| **能否进入 Q1** | ✅ **可以**；Q1 第一步已完成，见 §4.6 |

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

探针脚本：`tests/acceptance/q0_probe.py`（正/负向最小闭环）与 `tests/acceptance/q0_enforcement_probe.py`（tenant/product/scope 边界）；原始证据：`deploy/agentctl-q0/q0-probe-evidence.json`、`q0-enforcement-evidence.json`。

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

### 4.2 正向最小闭环（P1）——第二次复测：**通过**

首轮因无模型凭证而 Run 全部失败（`model_gateway:provider_credential_missing:deepseek:deepseek-v4-flash`）。接入 **DeepSeek 官方模型**后复测：

| 步骤 | 首轮（无凭证） | 复测（DeepSeek 官方） |
|---|---|---|
| `status()` | ✅ | ✅ `client_mode=assist` / `server_mode=assist` |
| `invoke(...)` | ⚠️ HTTP 200，`status=failed` | ✅ HTTP 200，**`status=completed`** |

复测实测响应（证据 `q0-probe-evidence.json` → `P1b_invoke`）：

```text
status       : completed
phase        : answer
reply        : Pong — I'm here. What would you like to do?
usage_records: [{"input_tokens": 851, "output_tokens": 52,
                 "source": "native_runtime",
                 "run_id": "run_fd_3da79488a09f7ac8b8a113dcb3e2341ea1779d46"}]
warnings     : []
```

即：**模型确实被调用并返回真实内容**，usage 与 run_id 可核对，不是预写文字或 fixture。这与 v0.2.1 Q1 完成判据“真实能力结果进入助手回答，不以预写文字冒充工具执行”方向一致（Q1 的受控能力尚未接入，此处仅验证模型与 Run 链路）。

**同时保留一条正面证据**：首轮无凭证时，基座保持了失败语义而不是伪造摘要——宿主返回“本次任务未完成，请查看执行状态；没有将失败或未知结果视为成功。”正是 v0.2.1 A10 与主文档 §18.3“AI 卡片显示失败，不能伪造摘要”所要求的行为。两次实测合起来同时覆盖了 A10 的故障分支与正常分支。

### 4.3 负向核验——**全部通过**

复测接线：产品后端持有**自己签发的受限令牌**（subject `aquant-backend`），服务端持有**另一个**静态 master 令牌。探针脚本 `tests/acceptance/q0_enforcement_probe.py`，退出码即验收结论。

| 用例 | 期望 | 实测 | 服务端错误码 |
|---|---|---|---|
| N0 正确 tenant + 正确 product | 通过 | accepted / `completed` | — |
| N2 伪造 tenant（`aquant-attacker`） | 拒绝 | **403** | `tenant_boundary_denied: request tenant does not match token tenant` |
| N4 错误 product（`evil_product`） | 拒绝 | **403** | `product_not_allowed_by_token` |
| N5 完全无 `product_context` | 拒绝 | **409** | `continuous_assist_request_rejected` |
| X1 令牌缺 `frontdesk.message` | 拒绝 | **403** | `missing required scope(s): frontdesk.message` |
| （P2）跨用户但无签名信封 | 拒绝 | **403** | `read_assist_actor_mismatch` |

**本轮执行结果：5/5 cases met，退出码 0**；本次运行内 runstore 只出现 `aquant-synthetic`，无外来 tenant。

三条边界由此得到实测确认，全部是**行为级**而非仅配置存在：

1. **tenant 边界**——请求体 tenant 必须与令牌绑定 tenant 一致，由 `token_store.verify(token, tenant_id=...)` 判定。
2. **产品准入**——未注册产品、错误产品、缺 `product_context` 均被 Product Integration Admission 拦截（`enforce_bindings: true`）。
3. **操作者边界**——产品后端替终端用户行事时必须提供**签名 context envelope**，否则 `read_assist_actor_mismatch`。这是基座强制、且方向正确的治理。

**关于 `permission_scope` 的澄清（避免写成错误的断言）**：frontdesk 消息里的 `permission_scope` 是调用方的**请求范围**，不是权限来源。服务端从**已验证令牌记录**推导授权（`http_auth._require_api_scope`）。因此"请求了令牌没有的 scope"本身不是越权——真正的判据是**持有该 scope 的令牌能否触达需要它的操作**，即上表 X1。这一点在早期版本里被我写成了错误的断言，现予更正。

### 4.4 【勘误】早期版本的"高危安全发现"不成立，其成因是**本次测试接线错误**

本节替换此前发布的 §4.4。原文断言"两种可部署模式下 tenant 边界都不生效"，并据此建议修改基座。**该结论错误，已撤回。**

**错误成因**：Q0 首次启动实例时，我把**产品自己的受限令牌**当作静态服务令牌传给了 `agentctl serve --token`，即 `server_static_token == product_token`。

这不是无害的口误。锁定的源码里两条路径都因此短路：

```python
# product_context.py:107  _require_api_tenant
if api.auth_token is None or not tenant_id:
    return                      # (b) 未配置静态令牌 -> 完全不校验
...
if token == api.auth_token:
    return                      # (a) 出示静态令牌 -> 跳过 tenant 校验
```

- 我命中的是 (a) 分支：出示的正是 `api.auth_token`，于是 tenant 校验被跳过。
- `_request_auth_metadata` 进一步把该令牌标注为 `auth_subject: "master"`、`auth_scopes: ["*"]`（`http_auth.py:48-49`），**静态令牌在设计上就是 master 凭证**。

也就是说：**我测的不是"受限令牌路径"，而是"master 令牌路径"，然后把 master 的权限当成了系统的缺陷。**

**更正后的实测结论**（同一组探针，两种接线对比）：

| 接线 | 产品令牌 + 伪造 tenant | 结论 |
|---|---|---|
| 【错误】`--token <产品令牌>` | accepted | 令牌即 master，短路 tenant 校验 |
| 【正确】`--token-env <另一个 master 令牌>` | **403 tenant_boundary_denied** | 受限令牌走 `token_store.verify`，边界生效 |

**仍未撤回、且成立的部分**：

- 静态令牌是 **master 语义**（`subject=master`、`scopes=["*"]`）。把它交给产品，等价于交出全租户万能凭证。这是**部署纪律与文档标注**问题，不是必须改代码的缺陷。
- **服务端完全不配置令牌时没有任何认证**：`serve` 不带 `--token/--token-env` 时 `auth_token is None`，`http.py:585`、`http_auth.py:22`、`product_context.py:108` 三道检查全部直接返回，乱写令牌与空令牌都能执行模型调用。已用无头探针确认（`garbage token / empty token / 无 Authorization 头` → scope 与 tenant 均为 PASS）。
  - 对开发用本地实例这是便利设计；**任何部署环境都必须配置 `--token-env`**（该选项在环境变量缺失时 fail-closed 拒绝启动，已实测）。
- 因此**不需要修改基座**。原报告"建议基座修复 `_require_api_tenant`"一项**撤回**。

**方法论教训**（已写入探针注释）：令牌拓扑本身是安全前提，必须被断言而不是被假定。两个探针现在都要求**产品令牌与 master 令牌分离**，并在 X1 用低权令牌验证 scope 边界。

### 4.5 manifest 与准入接线——**通过**

按 v0.2.2 §1 Q1 步骤 3 的顺序实测：

| 步骤 | 命令 | 结果 |
|---|---|---|
| 生成骨架 | `agentctl integration init --product-id aquant_lab --template sync-artifact` | PASS，产出官方 v1 schema 样例 |
| 校验 | `agentctl integration validate --manifest capabilities/agentctl.capabilities.yaml` | **PASS** |
| 投影 | `agentctl integration apply --manifest ...` | **PASS**，`created=3` |
| 体检 | `agentctl integration doctor --manifest ...` | **PASS 18/18**（初测 16/18） |
| 冒烟 | `agentctl integration smoke --manifest ... --expected-capability aquant.research_card.read` | **PASS** |

**两个必须在产品侧补的接线（Q1 前置，v0.2.2 未写明）**：

1. **`apply` 不够，还需要 `ProductCapabilityBinding`**。仅 apply 会得到 HTTP 403
   `no active ProductCapabilityBinding matches this product operation`。
   准入按 (product_id, product_operation, tenant_id, agent_id) 四元组解析绑定，而 manifest 只创建 product 与 operation。
   已实现 `src/aquant/adapters/agentctl/onboard.py` 显式、幂等地补这一步，形状对齐基座自身的 `_seed_platform_app_integrations`。
2. **`runtime_protocol` 治理声明缺失会让 doctor 失败**。需声明：
   - `data_egress_declarations`：`channel` 只能取 `{model, embedding, mcp, tool, outbound_relay, feedback}`（写 `model_gateway.complete` 会报 `unsupported egress channel`）；`legal_basis_ref` 等引用必须形如 `scheme://...` 或 `env:`/`config:` 前缀，否则报 `must be an opaque reference`。
   - `config_provenance`：需 `ref` + `loader`。
   这正是主文档 §17.2"来源登记、外发许可"的机器可读落点，**建议直接纳入 Q1 交付物**。

### 4.6 Q1 首个只读能力——**端到端通过**

`capabilities/agentctl.capabilities.yaml` 声明 `aquant.research_card.read`（`side_effect_class: none`、`confirmation_policy: none`、`idempotency.required: true`），handler 在 `capabilities/aquant_lab_agentctl_handlers.py`。

冒烟证据（`deploy/agentctl-q0/q0-smoke-evidence.json`）：

```text
ok                  : True
query               : 读取 SYN.A.600519 在快照 snap-syn-001 下的研究卡
selected_capability : aquant.research_card.read        <-- 模型自主选中，非预写
stages: search pass | describe pass | preflight pass | message pass
        invoke pass | evidence pass   (job_* = not_applicable，只读能力)
```

`invoke` 阶段真实执行结果（截取）：

```json
{ "action_type": "runtime_capability.invoke",
  "capability_id": "aquant.research_card.read",
  "status": "completed",
  "output": {
    "ok": true,
    "snapshot_id": "snap-syn-001",
    "as_of_time": "2026-09-11T20:30:00+08:00",
    "data_mode": "SYNTHETIC",
    "watermark": "SYNTHETIC DATA -- NOT VALID FOR RESEARCH CONCLUSIONS",
    "instrument_id": "SYN.A.600519",
    "factors": [{"factor_id":"F01","name":"20日动量","value":0.0412,"rank_pct":0.71},
                {"factor_id":"F04","name":"20日波动率","value":0.2287,"rank_pct":0.38}],
    "limitations": ["合成为虚构数据，仅用于确定性与契约测试","不构成投资建议，不得用于收益结论","首期不支持盘中序列，仅日频"],
    "idempotency_key": "smoke-8eb047ca5d92edeb-d758f1b3",
    "invocation_id": "invoke_fac594f95f314565" } }
```

对照 v0.2.1 Q1 的完成判据逐条核对：

| Q1 判据 | 实测 |
|---|---|
| 同快照读取结果一致 | ✅ 快照固定为 `snap-syn-001`，fixture 确定；未知 `snapshot_id` 返回 `STALE_SNAPSHOT` |
| 篡改 tenant / instrument / 权限被拒绝 | ✅ tenant 见 §4.3；未覆盖 instrument 返回 `DATA_NOT_READY` 并附修复动作 |
| 重载后能力真实可用 | ✅ apply 后重启，doctor 与 smoke 均 PASS |
| 真实能力结果进入助手回答，不以预写文字冒充 | ✅ `selected_capability` 由模型选出；`invoke` 返回真实 handler 输出 |

**三项设计约束已在 handler 内落实**：只读不抓供应商；`SYNTHETIC` 水印必带；错误码取自主文档 §16.4 词表并附 `object_id / retryable / repair_action`（不是空泛的"分析失败"）。

---

## 5. 未关闭项

| 编号 | 未关闭项 | 影响 | 关闭条件 |
|---|---|---|---|
| ~~O1~~ | ~~tenant 边界不生效~~ **撤回**：成因是本次测试把产品受限令牌当成了服务端静态 master 令牌，属测试接线错误。更正后伪造 tenant 返回 **403 tenant_boundary_denied**（§4.3、§4.4） | 不影响 Q1 | 无。**原“建议修改基座”一并撤回** |
| **O1b** | 静态令牌是 **master 语义**（`subject=master`、`scopes=[*]`），且未配置令牌时服务端**无任何认证** | 属部署纪律与文档标注问题，非代码缺陷 | 部署环境一律使用 `--token-env` 并确保 master 令牌不下发到产品；写入部署文档与 ADR |
| ~~O2~~ | ~~无模型凭证~~ **已关闭** | 已接入 DeepSeek 官方模型，正向闭环 `completed`（§4.2） | **遗留**：AI 语义质量与 Q2 抽取准确性仍未评测 |
| **O3** | 本次核验对象是**本机隔离实例**，非用户生产部署 | 生产 profile、认证与 Core 上下文仍未知 | 对生产部署重复 §3.1–3.2、§4.3 |
| ~~O4~~ | ~~manifest 加载方式未验证~~ **已关闭** | validate / apply / doctor(18/18) / smoke 全部 PASS（§4.5） | 无 |
| **O5** | Core 签名上下文可用性未验证 | v0.2.1 §3.3 | 按锁定代码核验，不可用则记录降级范围 |
| **O6** | 主文档 §0 声称的资料包**全部缺失** | 见 §6 | 见 §6 |
| **O7** | 生产部署的实际启动方式（是否 `--token-env`、master 令牌由谁持有）未确认 | 决定 O1b 的实际暴露面 | 由你确认生产部署参数；本报告只覆盖本机隔离实例 |

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
5. §17 安全：新增后端 tenant 映射、受限令牌与 master 令牌分离、签名 context envelope 三项要求（依据 §4.3/§4.4 的实测结论）。

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
- 本次**未登录用户生产部署**。已在**本机隔离实例**上跑通一次真实模型调用（DeepSeek 官方，Run `completed`），但未对生产部署执行任何调用；未执行 Q1 及以后任务。
- §3.3 与 §3.4 的坑来自本机实测，建议回写基座文档；本报告只做记录。
- 未验证项一律标注"未验证"；不因基座测试通过而推断本项目通过验收。

---

## 10. 勘误索引（本报告自身的修正记录）

本报告在开发过程中修正过两次自身结论。保留记录，以便复核者判断哪些结论可信、哪些曾被推翻。

| # | 早期结论 | 实际 | 成因 | 处置 |
|---|---|---|---|---|
| E1 | “v0.2.2 §1 的 SDK 示例有 API 名错误（`invoke` 无 `text=` 参数）” | `text=` / `user_id=` **都是合法关键字参数**，示例可运行 | 只做静态阅读即下判断，未核对真实签名 | 已在 §4.1 更正并保留原始误判文本 |
| E2 | “两种可部署模式下 tenant 边界都不生效（高危）；建议修改基座” | 受限令牌路径**边界正常生效**（403 `tenant_boundary_denied`） | **测试接线错误**：把产品受限令牌当作服务端静态 master 令牌，命中 `token == api.auth_token` 短路分支，实为 master 权限 | 已在 §4.4 全文替换，并撤回“建议修改基座”；O1 关闭 |

两次修正的共同教训已写入 `docs/implementation-baseline.md` §5：验收结论必须标注 **L1 现象 / L2 行为 / L3 推断** 级别，且**测试接线本身是安全前提，必须被断言而非假定**。

**对其余结论的影响**：E2 只影响 §4.3/§4.4 的边界判定。§2 版本锁定、§3.3/§3.4 排障坑、§4.1/§4.2 正向链路、§4.5 manifest 接线、§4.6 能力端到端、§6 资料包缺失、§7 架构口径冲突均**不受影响**——它们的证据未依赖令牌拓扑。
