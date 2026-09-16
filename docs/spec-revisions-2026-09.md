# 主规格修订记录（2026-09）

**性质：** 本文件记录对《A-Quant-Lab_开发文档_v0.2.md》的**定点修订**。
**日期：** 2026-09-16 · **依据：** `docs/implementation-baseline.md` §6、ADR-011/012/013

## 为什么不直接改主规格

主规格是**需求与验收的基准**，被多处引用（基线、ADR、实施文档）。
在 981 行里就地改动，会让"主规格说了什么"这件事失去唯一性——
而它是所有争议的最终裁决依据（基线 §0：冲突时以主规格为准）。

因此改用**定点修订**：主规格保持原样，本文件逐条声明哪一段被取代、
取代它的是什么、依据在哪。这样"当前有效的规格"= 主规格 − 本文件列出的条目 + 修订内容，
而每一条修订都可追溯到一次决策。

**优先顺序：** 本文件与主规格冲突时，以本文件为准（它更晚、且有决策记录）。

---

## R1 · §14.3 领域模块：补 agentctl 适配层

**原文（保留）：** `data` / `evidence` / `research` / `strategy` / `portfolio` /
`simulation` / `experiments` / `assistant` / `operations` 九个模块的职责划分不变。

**修订：** 适配层**不属于领域模块**。领域层不得 import agentctl；
agentctl 是 `adapters/` 下的一个实现，与 `providers/`、`models/` 同级。

**依据：** ADR-011。**强制：** `tests/security/test_agentctl_boundary.py`
（用注入违规的方式验证过守卫确实会失败——"干净所以通过"与"守卫没生效"
看起来一样，必须区分）。

**同时明确一条原文没写的分界：** 模型只参与**证据抽取**，
不参与任何组合构建、下单或记账。模型输出可以成为证据，不能成为指令。

## R2 · §14.4 仓库布局：与实际结构对齐

**原文（保留）：** 布局草图作为**目标形态**继续有效。

**修订：** 三处与实际不符，以实际为准：

| 原文 | 实际 | 说明 |
|---|---|---|
| `migrations/` | `schema/001_metadata.sql` | 单一 schema 文件，由 `apply_migrations()` 全部重放。**见 R2.1 的重要限制** |
| `domain/{statistics}` | `domain/ai/`、`domain/simulation/` | 实测领域划分：`ai`（模型窄接口与外发闸门）、`simulation`（费用/模拟器/公司行为/板块规则） |
| `tests/{unit,...}` | `tests/{api,golden,pit,security,integration,acceptance}` | 按**验收对象**而非测试粒度分目录 |

### R2.1 schema 变更不会自动应用到已有部署

`apply_migrations()` 的实现是：按序执行 `schema/*.sql`，靠 `IF NOT EXISTS` 保证可重入。
**没有版本表**，因此：

* 新增**表**会生效（`CREATE TABLE IF NOT EXISTS`）；
* 已有表的**列变更与约束变更不会生效**——`CREATE TABLE IF NOT EXISTS` 见到表已存在就跳过。

SQLite 无法直接修改 CHECK 约束，这类变更需要重建表。
**当前的实际做法是重建数据目录**（`AQUANT_RESET_DATA=1` 或重建快照），
因为账本与快照都能从数据源重建。

**这意味着：** 修改 schema 后，**必须显式重建数据目录**，否则运行的仍是旧结构。
本项目已经踩过一次：给 `cash_entry.entry_type` 的 CHECK 白名单加取值、
给 `corporate_action` 加列，都是靠重建快照才生效的。

**未做的选择：** 引入带版本表的真迁移机制。理由是当前只有一个部署形态、
且数据可重建；一旦出现"不能重建的数据"（例如长期积累的真实账本），
这条就要重新评估。**在那种情况下，靠重建是危险的。**

## R3 · §16.3 助手允许工具：能力 ID 取代工具名

**原文（保留）：** 允许清单（`get_snapshot` / `query_research_card` /
`get_event_evidence` / `compare_instruments` / `explain_factor` / `get_portfolio` /
`get_decision_history` / `preview_simulation_plan`）与禁止清单
（`execute_order` / `write_ledger` / `run_shell` / `raw_sql` / `fetch_arbitrary_url`）
**语义不变**。

**修订：** 名称改为 `aquant.*` 形式的能力 ID（如 `aquant.research_card.read`），
由 agentctl 的能力注册表声明与治理。**语义一一对应，不新增权限。**

## R4 · §20 关键架构决策记录：增补 ADR

**增补三条已采纳的决策**（文件在 `docs/adr/`）：

| 编号 | 决策 | 日期 |
|---|---|---|
| ADR-011 | 复用 agentctl 作为受治理的 AI 运行与助手能力，**不**让其接管量化事实、计算与账本 | 2026-09-15 |
| ADR-012 | `TextModelProvider` 保留为领域窄接口，agentctl 网关是其生产实现之一；离线测试用确定性替身 | 2026-09-15 |
| ADR-013 | 静态 master 令牌不下发到产品；部署一律 `--token-env`；产品使用独立签发的受限令牌 | 2026-09-15 |

另有 ADR-001…005、ADR-011…013 共八条在 `docs/adr/` 下；
本表只增补主规格成文时（v0.2）尚不存在的三条。

---

## 尚未修订但已确认过时的原文

以下原文与实现不符，但**没有**列入上面的修订——因为改它们需要先有决策，
而不是先改文档：

| 位置 | 原文 | 实际 | 状态 |
|---|---|---|---|
| §6.1 | 首选 Tushare 或授权供应商 | 实际用 BaoStock + 腾讯 + 巨潮（免费源） | 已由 ADR-001/003/004 覆盖，主规格未改 |
| §18.3 | 证据研究要求"≥200 份人工标注材料" | 当前只有真实公告的少量样本 | **未达成**，见下 |
| §12.6 | 完整红利税处理 | 只做 PRE_TAX 标注（规格允许） | 未实现税制 |

§18.3 的 200 份标注材料是**真实缺口**，不是文档问题：
它决定证据抽取的准确率能否被量化。当前只有真实公告的少量样本，
因此只能说"链路可跑通"，不能说"抽取准确率已知"。
