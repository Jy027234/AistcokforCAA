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

**Q0 部分完成：正向链路已通过，但存在 1 项高危安全发现，未进入 Q1。** 详见 `docs/integration/q0-readiness-report.md`。

- 基座锁定提交：`b5cad04836cf93b35750fb0116ab0c2d936d1f89`（`E:\IT\Agent`，`main`）
- ✅ **正向闭环通过**：接入 DeepSeek 官方模型后，assist 调用返回真实回答，`status=completed`，usage 与 run_id 可核对。
- ❌ **高危未关闭**：frontdesk HTTP 路径的 tenant/product/scope 边界在本机两种启动模式下均不生效，伪造 tenant 的 Run 真实执行完成。根因见报告 §4.4。
- ✅ **已修复的部署坑**：基座 `lite-product-validation/assist.runtime.yaml` 缺 `frontdesk.profile` 段，会导致所有 assist 调用 400；本项目配置已补齐。
- **主文档 §0 声明的资料包（`contracts/`、`schema/001_metadata.sql`、`CODEX_TASKS.md` 等 8 项）缺失**，见报告 §6。

## 目录

```text
docs/integration/          接入报告与证据
deploy/agentctl-q0/        Q0 隔离 agentctl 实例（runtime.config.yaml 已补 coordinator_v2）
tests/acceptance/          接入验收探针
```

目标仓库布局（主文档 §14.4）尚未建立：`apps/web`、`apps/api`、`src/aquant/{domain,application,adapters,operations}`、`contracts/`、`configs/`、`migrations/`、`tests/{unit,integration,golden,pit,security,e2e}`、`docs/{adr,runbooks,research}`。

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
