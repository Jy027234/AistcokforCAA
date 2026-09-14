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

**M1 与 M2 已完成并通过黄金用例，产品闭环已跑通，工作台界面已可用。** 详见 `docs/implementation-baseline.md`。

| 范围 | 状态 | 证据 |
|---|---|---|
| 资料包（contracts/schema/configs/examples/自检） | ✅ | `tests/validate_spec.py` 199/199，裸 Python 可跑 |
| 免费数据源验证 | ✅ | `docs/data-capability-eastmoney-direct.md` + ADR-001/002/003 |
| M1 数据与证据底座 | ✅ | D01–D08 端到端；官方公告渠道实跑归档 |
| M2 基线与模拟账本 | ✅ | S01–S10 全过；账本可逐项对账 |
| 产品闭环 | ✅ | 快照→读取→草稿→冻结→成交→估值→对账 |
| 工作台界面（M3） | ✅ | 四项顶层导航，浅色主题，见下 |
| agentctl 接入 | ✅ | Q0 报告；首个只读能力端到端 |

**测试：249 个全过（退出码 0）；`validate_spec.py` 199/199。**

### 关键约束（已实测，非文档转述）

- ⚠️ **免费源不提供历史时点（PIT）**：正式历史回测与财务因子 F07–F10 默认关闭（ADR-003）。
- ⚠️ **免费源会按出口 IP 长时段封锁**：已编码为多源降级（ADR-001）。
- ⚠️ **本机处于穿透式代理之后**（所有域名解析到 `198.18.0.0/15`）：DNS 防护需显式声明可信代理网段，属真实的安全降级（ADR-002）。
- ⚠️ **部署纪律**：静态 `--token` 是 master 凭证，不得下发到产品；部署一律用 `--token-env`。

## 目录

```text
apps/web/                         工作台前端（React + TS + Vite，浅色主题）
src/aquant/domain/data/           证券、日历、行情、快照、前向归档、研究读取
src/aquant/domain/evidence/       引用定位与证据核验
src/aquant/domain/simulation/     费用、有限日频模拟器、公司行为
src/aquant/domain/portfolio/      组合构建、日终估值、计划生命周期
src/aquant/application/           工作台视图模型（前端不复制计算逻辑）
src/aquant/operations/            任务机制（状态机、租约、幂等）
src/aquant/adapters/providers/    数据源守卫、降级链、东方财富/腾讯/巨潮
contracts/  schema/  configs/  examples/    资料包
tests/{pit,golden,integration,security,acceptance}/
tools/build_workspace_fixture.py  用真实链路生成工作台数据
docs/                             基线、能力卡、ADR、Q0 报告
```

**验收口径**（基线 §5）：结论须标注 L1 现象 / L2 行为 / L3 推断；探针必须用退出码表达结论；测试接线本身是安全前提，必须断言而非假定。

## 工作台界面

```powershell
# 1. 生成数据（跑真实领域链路，不是假数据）
$env:PYTHONPATH='src;tests\integration'
python tools\build_workspace_fixture.py

# 2. 启动界面
cd apps\web
npm install      # 首次
npm run dev      # http://localhost:5173
# 或 npm run build && npm run preview
```

四项顶层导航（主文档 §5.2）：**今日 / 研究 / 组合 / 实验**。视图可深链：`/#research`、`/#portfolio`、`/#experiments`。

界面遵循的硬约束（都不是文案约定，而是数据形状）：

- **不出现任何概率或预期收益字段**；排名一律标注为“横截面排名百分位”。
- **不出现综合评分**（无 `totalScore` 之类字段）。
- **不可模拟时显示规则、适用日期与修复方法**，不显示“失败”。
- **未接入的后端能力明确显示未接入**，不用占位数字冒充。
- 风险状态一律「颜色 + 文字」双通道；虚构数据全程带水印横幅。

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
