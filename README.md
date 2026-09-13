# A-Quant Lab

A股研究与模拟决策工作台。首期定位为**自用研究与模拟**，不连接券商、不自动交易、不提供付费荐股或代客理财。

## 文档

| 文件 | 性质 |
|---|---|
| `A-Quant-Lab_开发文档_v0.2.md` | **主规格**（981 行，2026-09-12）：需求、技术架构、研究规范、验收、阶段 |
| `A-Quant-Lab_v0.2_结构化摘要.md` | 主规格结构化导读（含章节行号索引，便于导航） |
| `A-Quant-Lab_agentctl接入评估与开发补充_v0.2.1.md` | 接入架构结论：复用 agentctl 基座，量化领域核心独立 |
| `A-Quant-Lab_agentctl接入评估与开发补充_v0.2.2.md` | 可执行化：Q0–Q5 卡片、manifest 骨架、A01–A16 用例映射 |
| `docs/integration/q0-readiness-report.md` | **Q0 接入就绪报告**（本机实测，未关闭项见其中 §5） |

## 当前状态

**Q0 通过；Q1 首个只读能力已端到端跑通。** 详见 `docs/integration/q0-readiness-report.md` 与 `docs/implementation-baseline.md`。

- 基座锁定提交：`b5cad04836cf93b35750fb0116ab0c2d936d1f89`（`E:\IT\Agent`，`main`）
- ✅ **正向闭环**：接入 DeepSeek 官方模型后 Run `completed`，真实回答 + usage + run_id 可核对。
- ✅ **边界核验 5/5**：伪造 tenant → 403 `tenant_boundary_denied`；错误 product → 403；缺 product_context → 409；低权令牌 → 403；跨用户无签名信封 → 403。
- ✅ **首个能力端到端**：模型自主选中 `aquant.research_card.read` 并真实执行；`integration doctor` 18/18 PASS。
- ✅ **已修复的部署坑**：`assist.runtime.yaml` 缺 `frontdesk.profile` 段会导致所有 assist 调用 400（见报告 §3.3）。
- ⚠️ **部署纪律**：静态 `--token` 是 master 凭证，不得下发到产品；部署一律用 `--token-env`。
- ❌ **主文档 §0 资料包 8 项仍缺失**（`contracts/`、`schema/001_metadata.sql`、`CODEX_TASKS.md` 等），见报告 §6 与基线 T3。

## 目录

```text
docs/implementation-baseline.md   M/Q 对照任务表（统一实施基线；含依赖矩阵与验收口径）
docs/integration/                 接入报告、证据与勘误记录
capabilities/                     agentctl 能力 manifest + product-owned handler
src/aquant/adapters/agentctl/     适配层（当前含 onboard：产品准入接线）
deploy/agentctl-q0/               Q0 隔离 agentctl 实例与探针证据
tests/acceptance/                 接入验收探针（退出码即结论）
```

**验收口径**（基线 §5）：结论须标注 L1 现象 / L2 行为 / L3 推断；探针必须用退出码表达结论；测试接线本身是安全前提，必须断言而非假定。

尚未建立（主文档 §14.4）：`apps/web`、`apps/api`、`src/aquant/{domain,application,operations}`、`contracts/`、`configs/`、`migrations/`、`tests/{unit,integration,golden,pit,security,e2e}` —— 见基线 T3（最高优先，不依赖供应商与模型密钥）。

## Q0 复核

```powershell
# 基座锁定与 diff
git -C E:\IT\Agent log -1 --format='%H %cI %s'

# 启动隔离实例（须先确认 8765 端口已释放）
agentctl serve --config deploy\agentctl-q0\runtime.config.yaml --host 127.0.0.1 --port 8765 --token <TOKEN>

# 正/负向探针
python tests\acceptance\q0_probe.py --base-url http://127.0.0.1:8765 --token <TOKEN> \
  --json-out deploy\agentctl-q0\q0-probe-evidence.json
```

## 安全边界

模型默认**没有** `execute_order`、`write_ledger`、`run_shell`、`raw_sql`、`fetch_arbitrary_url` 权限，也没有冻结模拟计划的权限（主文档 §16.3）。密钥仅在后端读取，不进入前端、日志或仓库。
