# A-Quant Lab

A股研究与模拟决策工作台。首期定位为**自用研究与模拟**，不连接券商、不自动交易、不提供付费荐股或代客理财。

## 先跑起来（Docker）

```powershell
docker compose up --build      # 构建并启动
# 打开 http://127.0.0.1:8080
docker compose down            # 停
docker compose down -v         # 停并清空数据（从零开始）
```

单容器同源：前端用相对路径请求 `/api`，因此不需要 CORS，也不需要反向代理。
容器**用内置合成夹具自举一份快照**，所以一起来界面就有内容，不需要挂载任何东西——
顶部黄色水印会写明这是虚构示例数据。

> ⚠️ **这不是生产形态。** 前端用的是演示主体开关（`user:demo`），
> **不是身份认证**。生产形态（API 与前端分开、令牌纪律）见 ADR-013，尚未落实。

想在本机开发 API 指向真实快照，设 `AQUANT_DATA_DIR` 指向发布数据根目录。
不显式设置 `AQUANT_SNAPSHOT_ID` 时，API/worker 会读取该目录的
`current_snapshot.json`；只有回放某个已发布物理快照时才需要显式指定 ID。
真实计划还需要配置券商佣金（见下文「费率」）：

```powershell
$env:AQUANT_DATA_DIR='E:\IT\A股量化交易\deploy\universe-snapshot'
# 可选：回放一个已发布的物理快照；省略时跟随 current_snapshot.json
# $env:AQUANT_SNAPSHOT_ID='snap-eod-2026-09-18-<uuid>'
$env:AQUANT_COMMISSION_RATE='0.00025'        # 你的券商费率，万分之 2.5 写作 0.00025
$env:AQUANT_COMMISSION_MIN_CENTS='500'       # 最低 5 元
$env:PYTHONPATH='src'
python -m uvicorn main:app --app-dir apps/api --host 127.0.0.1 --port 8000
```

上面的宿主机目录不会自动挂进 Docker 命名卷；默认 Compose 路径仍用于合成数据试运行。
若要让容器读取宿主机真实快照，需要先显式配置 bind mount，不能只设置 Windows 路径环境变量。

未配置佣金时，真实数据仍可用于状态、候选、研究卡和证据等只读接口；
`preview` / `freeze` / `execute` / `value` 会在领域费率闸门返回拒绝，
因为合成费率算出的成交与净值没有依据（§12.6）。要走模拟写链路，必须提供使用者实际券商约定的费率。

## 本地开发（不用 Docker）

```powershell
# --- 终端 1：启动 API（主入口所需） ---
$env:PYTHONPATH='src'
python -m uvicorn main:app --app-dir apps/api --host 127.0.0.1 --port 8000

# --- 终端 2：启动前端 ---
cd apps\web; npm install; npm run dev          # http://localhost:5173
```

前端在开发与预览两种模式下都把 `/api` 代理到 `127.0.0.1:8000`（`AQUANT_API_TARGET` 可改）。
默认入口完全读取 API；API 不在线时会明确报错，不会静默回退到夹具或伪造一次成功的冻结。
只有显式访问 `http://localhost:5173/?mode=demo` 才读取 `workspace.json`。使用该模式前运行
`python tools\build_workspace_fixture.py`，页面会显示演示模式水印并禁用写操作。

### 最小本机试运行（合成快照）

先用合成数据确认界面和 API 能启动；这一步不需要数据源、券商佣金或模型密钥。

```powershell
# 终端 1：启动 API
$env:PYTHONPATH='src'
python -m uvicorn main:app --app-dir apps/api --host 127.0.0.1 --port 8000

# 终端 2：检查健康与数据状态，再启动前端
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health
Invoke-RestMethod http://127.0.0.1:8000/api/v1/status
cd apps\web
npm install
npm run dev                         # http://localhost:5173
```

页面顶部应显示合成数据水印；`/api/v1/status` 能返回 `snap-syn-001`。
这条试运行只验证本机链路，不代表真实数据质量或生产部署已验收。

## 当前状态

**研究链路与模拟账本在合成与真实数据上都已跑通并有机器可验的断言；
数据层受免费源限制，还不足以支撑投资结论。**

| 范围 | 状态 | 说明 |
|---|---|---|
| 资料包自检 | ✅ | contracts/schema/configs/examples，裸 Python 可跑 |
| M1 数据与证据底座 | 🟡 | 合成快照 D01–D08 通过；真实 current 快照 900 只 / 65 个交易日已发布 |
| M2 基线与模拟账本 | ✅ | 多日 + 跨进程重启验收通过；T+1 真的跨日生效 |
| 产品闭环 | ✅ | 预览→确认→冻结→执行→估值→对账，合成与真实数据各跑一遍 |
| 工作台界面 | 🟡 | 六页可浏览（含**设置**）；写链路走通；**界面上的数字有出处检查**（见下文） |
| 研究作业与证据 | ✅ | 作业提交/执行/幂等、模型独立抽取、引用可定位、交叉核对 |
| 每日流水线 | ✅ | 采集→快照→**因子落库**→质量闸门；界面**设置页**可配时间与启停（ADR-014） |
| 研究卡上的因子数值 | ✅ | 因子落库（`research_run`/`feature_value`）→ 接口传入卡片；真实快照上 832/900 有值 |
| agentctl 接入 | 🟡 | Q0 成立；唯一只读能力 `aquant.research_card.read` 已接 M1 真实存储（含因子值）；其余七个能力未接 |

**数据层的已知限制（这些不影响流程验证，但影响结论）：**

- 只有 **65 个交易日**，做不了回测；当前仅有 2 个真实快照，历史时点均为抓取后重建
- 行业分类**没有变更历史**，只能标注为 `RECONSTRUCTED`，回答不了历史时点
- 公司行为在真实数据里只有 **2 条**分红记录；代码路径验证过，覆盖率没验证过
- **券商佣金没有权威值**（券商约定），必须由使用者提供；印花税与过户费有权威来源并已留证
- **上市天数门槛（120 个交易日）判不完整**：快照日历只有 65 天，窗口外上市的标的
  只能记「不足以判定」而不排除（见下文「两类判定」）；能力卡片会如实说明

## 验证（全部以退出码为结论）

| 检查 | 命令 | 目的 |
|---|---|---|
| 资料包自检 | `python tests/validate_spec.py` | 契约、示例、SQL、约束 |
| 领域与接口测试 | `pytest tests` | 711 个用例（本次全量通过） |
| 界面交互 + 数字出处 | `python tools/check_ui_flow.py` | 真浏览器点击；每个区块的数字必须说得出出处 |
| 归档字节离线复核 | `python tools/verify_archive.py` | 留证字节与摘要一致 |
| 真实数据闭环 | `python tools/check_real_flow.py` | 真实快照上走完决策闭环 |
| 双侧验收 | `python tools/check_both_sides.py` | **同一套断言**在合成与真实上各跑一遍 |
| 因子落库 | `python tools/compute_factors.py` | 真实快照上 900 行落库、832 行有值，并**回读**确认 |
| 数字出处（单跑） | `node apps/web/tools/check_number_provenance.mjs --url …` | 见下文 |
| HEAD 可复现性 | `python tools/check_reproducible.py` | 干净导出后全量测试仍通过 |

联网的脚本需要放行本机透明代理网段（`AQUANT_TRUSTED_PROXY_NETWORKS`，见 ADR-002）。
全部只做只读抓取，不下单、不连券商。

## 数据流水线

```powershell
# 全市场采集（首次，可续跑）
python tools\collect_universe.py --start 2026-06-22 --end 2026-09-18
# 上市日期（逐只查 BaoStock ipoDate，只查研究池里的几百只）
python tools\collect_listing_dates.py --write
# 从采集结果重建研究池（会**保留**已采到的上市日期）
python tools\build_pool_from_universe.py --size 300 --write
# 每个交易日：采集 -> 快照 -> 因子落库 -> 质量闸门（并发锁 + 休市判断）
python tools\daily_run.py --skip-if-done
python tools\show_alerts.py --days 7      # 有 ERROR 时退出码 1
```

日常生产路径由 `daily_run.py` 串起，生产发布步骤实际调用
`tools/publish_universe_snapshot.py`，每次生成新的唯一物理 ID；通过因子落库和 F10
质量闸门后，再由 `tools/promote_snapshot.py --require-factors` 原子切换
`deploy/universe-snapshot/current_snapshot.json`。旧物理目录与 `meta.sqlite` 不会被覆盖。

需要手工发布或回放时，先延迟切换 current，记下命令输出的 `snapshot_id`，再完成后续闸门：

```powershell
python tools\publish_universe_snapshot.py `
  --data-dir deploy\universe-snapshot `
  --cache deploy\agentctl-q0\universe-bars.json `
  --pool configs\real-pool-csrc.yaml `
  --financials deploy\agentctl-q0\financials-cache.json `
  --actions deploy\agentctl-q0\dividend-actions.json `
  --window-start 2026-06-22 `
  --window-end 2026-09-18 `
  --defer-promotion

$snapshotId='<上一步输出的 snap-eod-...>'
python tools\compute_factors.py `
  --snapshot-dir deploy\universe-snapshot `
  --snapshot-id $snapshotId `
  --json-out deploy\agentctl-q0\factors-persist.json
python -m tests.integration.t12_f10_real
python tools\promote_snapshot.py `
  --data-dir deploy\universe-snapshot `
  --snapshot-id $snapshotId `
  --require-factors
```

### 每天自动跑：界面配置 + 独立 worker（ADR-014）

在界面**设置**页填时间与解释器并保存，然后让 worker 长驻：

```powershell
python tools\scheduler_worker.py            # 到点自动跑，也执行界面的「立刻运行一次」
python tools\scheduler_worker.py --once     # 只处理一轮（cron / 排查）
python tools\scheduler_worker.py --now      # 立刻跑一次
```

Windows 上建议把长驻 worker 注册成带恢复触发器的计划任务：

```powershell
& tools\install_scheduler_task.ps1 `
  -PythonPath "E:\IT\Agent\.venv\Scripts\python.exe" `
  -DataDir "E:\IT\A股量化交易\deploy\universe-snapshot"
```

该脚本会先验证解释器可导入 `baostock` 与 `pytest`，再注册登录启动和异常退出后的
5 分钟恢复检查；worker 存活时不会重复启动，也不会改变界面中配置的实际采集时间。

**worker 不在跑时，界面上的配置不会让任何东西自动跑**——这句话写在设置页上。
界面上的「立刻运行一次」只登记请求，执行仍由 worker 做，因此点按钮与到点自动跑
走的是同一条路径（并发锁、休市判断、留痕、告警都在那条路上）。
调度默认**停用**，启用时必须填解释器：默认 python 没装 baostock，
用错的失败信息看起来像数据源坏了。

**因子必须落库，否则研究卡上没有数值。** `tools/compute_factors.py` 走的是产品
自己的落库路径（与 `POST /api/v1/research/jobs` 的因子作业同一条代码路径），
写 `research_run` 与 `feature_value`：

```powershell
python tools\compute_factors.py `
  --snapshot-dir deploy\universe-snapshot `
  --snapshot-id $snapshotId `
  --json-out deploy\agentctl-q0\factors-persist.json
# 真实快照上：财务记录 5397 条 -> 900 行落库，其中 832 行有值，
# 68 只标注「TTM 不可得（缺上年同期或口径不成立）」
```

`tests/integration/t12_f10_real` 仍然跑，但它的角色是**质量闸门**（值域、亏损股
是否被截断为 0、覆盖率），不是"算因子"。二者曾经被合成一步：流水线每天报成功，
而 `research_run` 一直是 0 行。

### 采集缓存 v3 与升级注意事项

采集器当前要求 `rows_format=3`，每个行情行还包括
`adjusted_close_cents`。采集器遇到仍含旧 `rows_format=2` 行的标的时，会对该标的按目标
窗口做一次全量重采，并在成功后写回 v3；之后才恢复增量续跑。升级期间不要用
`--prev-close-only` 跳过这次重采；网络中断会保留旧行并记入 `failed`，下次继续补齐。
不需要为了这个升级手工删除缓存，只有确实要重抓全部数据时才使用 `--refresh`。

### 两类用上市日期的判定

| 判定 | 依据 | 快照窗口不足 120 天时 |
|---|---|---|
| 决策时点是否已上市 | `listed_on` 与决策日比较 | 可判（与窗口无关） |
| 上市未满 120 个**交易日** | 窗口内实际交易日计数 | 上市日落在窗口内的可判（单调）；窗口外的记「不足以判定」,**不排除** |

窗口外的标的不会被误排除，但门槛也没有被验证过——这句话写在预览的 `notes` 里，
不藏在文档里。

调度接入、退出码语义、运行限制与升级注意事项见 **`docs/daily-pipeline.md`**。

分红金额以**整数微元**（10⁻⁶ 元）记账：真实分红常常不是整数分
（茅台 2025 年度每股 28.02423 元），按分存储只能截断，误差随股数放大。

## 费率（§12.6）

费率分两类，不能混为一谈：

| 项 | 取值 | 性质 |
|---|---|---|
| 印花税（卖出） | 0.0005 | 法定——财政部/税务总局公告 2023 年第 39 号 |
| 过户费（双向） | 0.00001 | 行业标准——中国结算通知，2022-04-29 起 |
| **券商佣金** | **无权威值** | **合同约定**，必须由使用者提供 |

```powershell
python tools\fetch_fee_sources.py    # 抓取并留证（URL + 时间 + 内容哈希）
```

合成费率**不得**用于真实数据：`preview` / `freeze` / `execute` / `value` 四个入口都会拦。
真实快照 + 未配置佣金时 API 仍可启动并提供只读接口；只有依赖费用的模拟与估值入口拒绝。
配置费率是放行这些入口的唯一方式，不能用开关绕过。

## 界面上的数字从哪来

每个区块用 `data-source` 声明来源（`apps/web/src/components/ui.tsx`）：

| 声明 | 含义 | 检查 |
|---|---|---|
| `api` | 数字来自服务端响应 | 带小数的金额**必须全部**能在响应里找到 |
| `fixture` | 随前端分发的示例数据 | 必须带演示标注（区块内或页面级水印） |
| `static` | 常量文案 | 不查数字 |
| `noNumericValue` | 本区块没有业务数字 | **声明必须属实**——出现金额形状即报红 |

为什么需要它：组合页曾经整块「我的模拟草稿」显示的都是夹具数字，
而徽章写着「API 在线」——页面根本没请求过预览接口。
夹具的数字看起来完全正常，这不是肉眼能发现的问题。

**改界面时必读：新增区块必须声明 `dataSource`，否则检查会报红。**
声明为 `api` 的区块，其金额必须真的来自服务端——不能拿夹具的数字填空。

## 工作台

五项顶层导航（主文档 §5.2）：**今日 / 研究 / 组合 / 工作区 / 实验**。
视图可深链：`/#research`、`/#portfolio`、`/#workspace`、`/#experiments`。

### 写链路

| 步骤 | 接口 | 说明 |
|---|---|---|
| 预览 | `POST /api/v1/plans/preview` | 只算不冻；订单只用执行日**之前**的价格 |
| 取令牌 | `POST /api/v1/plans/{id}/confirmation` | 服务端签发，绑定主体/计划/快照/账户版本/预览哈希 |
| 冻结 | `POST /api/v1/plans/{id}/freeze` | 五项复核；令牌一次性消费 |
| 执行 | `POST /api/v1/plans/{id}/execute` | 按 §12 规则模拟成交 |
| 估值 | `POST /api/v1/valuations` | 不变量失败则不发布净值 |
| 对账 | `GET /api/v1/portfolios/{id}/reconcile` | 逐项核验订单、费用、现金、批次 |

真实生产计划必须显式绑定两份已发布且不同的物理快照：
`decision_snapshot_id`（决策截止时间必须早于执行日开盘）和
`execution_snapshot_id`（必须是执行日收盘快照）。单快照兼容路径只用于合成演示；
冻结时会把两份快照及各自截止时间持久化，之后切换 `current_snapshot.json` 不会改写已冻结计划。

### 研究作业与证据

| 步骤 | 接口 |
|---|---|
| 提交作业 | `POST /api/v1/research/jobs`（只入队；幂等键由服务端算） |
| 执行作业 | `POST /api/v1/research/jobs/{id}/run`（生产由独立 worker 调用 `run_pending()`） |
| 研究卡 | `GET /api/v1/instruments/{id}/research`（按标的/快照/交易日**留档**，重复打开不刷新） |
| 证据 | `GET /api/v1/instruments/{id}/evidence`（引用带字符偏移，可定位） |
| 助手 | `POST /api/v1/assistant/messages`（材料先过外发闸门；只给草稿，不执行） |
| 助手回放 | `POST /api/v1/assistant/replay`（仅同一受信任主体映射、同一固定输入可读取已归档输出；回放单独登记） |
| 模型审计 | `GET /api/v1/assistant/calls`（按受信任主体映射返回成功、拒绝与失败调用） |
| 费率口径 | `GET /api/v1/fees`（当前费率与出处） |

**安全边界**：确认主体由服务端从受信任凭证取得，**不接受请求体自报**；
模型外发只有一条路径且先过闸门（未登记来源、未声明个人信息一律整批拒绝，**没有绕过开关**）；
API 中没有下单、写账本、Shell、SQL 或任意抓取路径（有测试断言路由表里不存在）。

### 界面遵循的硬约束（都不是文案约定，而是数据形状）

- **不出现任何概率或预期收益字段**；排名一律标注为「横截面排名百分位」。
- **不出现综合评分**（无 `totalScore` 之类字段）。
- **不可模拟时显示规则、适用日期与修复方法**，不显示「失败」。
- **未接入的后端能力明确显示未接入**，不用占位数字冒充。
- 风险状态一律「颜色 + 文字」双通道；虚构数据全程带水印横幅。

## 检查界面

```powershell
python tools\check_ui_flow.py            # 交互 + 数字出处（推荐）
cd apps\web
node tools\screenshot.mjs --url http://127.0.0.1:8080 --out ../../deploy/screens
node tools\page_dump.mjs --url http://127.0.0.1:8080 --tab portfolio   # 页面请求了哪些 URL
```

`page_dump` 回答的是「页面上的数字从哪来」——截图回答不了这个。

## 文档

**阅读顺序：** 主规格 → **修订记录** → 实施状态 → 专题文档。

| 文件 | 性质 |
|---|---|
| `A-Quant-Lab_开发文档_v0.2.md` | **主规格**：需求、技术架构、研究规范、验收、阶段 |
| `docs/spec-revisions-2026-09.md` | **主规格定点修订**：哪一段被取代、取代它的是什么、依据在哪。**冲突时以它为准** |
| `A-Quant-Lab_v0.2_结构化摘要.md` | 主规格结构化导读（含章节行号索引） |
| `docs/implementation-baseline.md` | 任务表 + **§8 实施状态**（每条都指向可复核的证据） |
| `docs/daily-pipeline.md` | 每日流水线：调度接入、退出码、告警、已知限制 |
| `docs/data-rights-register.md` | 数据权利登记表 + 费率来源 |
| `docs/adr/` | 架构决策记录（ADR-001…014） |
| `docs/integration/q0-readiness-report.md` | Q0 接入就绪报告（本机实测） |

## 目录

```text
apps/web/                         工作台前端（React + TS + Vite，浅色主题）
apps/api/                         工作台 API（FastAPI；容器里同时提供前端静态文件）
src/aquant/domain/data/           证券、日历、行情、快照、前向归档、研究读取
src/aquant/domain/evidence/       引用定位与证据落库
src/aquant/domain/ai/             模型窄接口、外发闸门、证据抽取
src/aquant/domain/simulation/     费用、模拟器、公司行为、板块规则
src/aquant/domain/portfolio/      组合构建、日终估值、计划生命周期
src/aquant/application/           视图模型、研究卡留档、助手编排
src/aquant/operations/            作业机制、每日流水线的锁与留痕、告警
src/aquant/adapters/              数据源守卫与降级链、模型提供方
contracts/  schema/  configs/  examples/    资料包
tests/{pit,golden,integration,security,api,acceptance}/
tools/                            采集、建快照、验收、每日流水线
docs/                             基线、ADR、数据权利、流水线说明
```

**验收口径**（基线 §5）：结论须标注 L1 现象 / L2 行为 / L3 推断；
探针必须用退出码表达结论；测试接线本身是安全前提，必须断言而非假定。

## Q0 复核

```powershell
git -C E:\IT\Agent log -1 --format='%H %cI %s'
agentctl serve --config deploy\agentctl-q0\runtime.config.yaml --host 127.0.0.1 --port 8765 --token <TOKEN>
python tests\acceptance\q0_probe.py --base-url http://127.0.0.1:8765 --token <TOKEN> \
  --json-out deploy\agentctl-q0\q0-probe-evidence.json
```

**部署纪律**：静态 `--token` 是 master 凭证，不得下发到产品；部署一律用 `--token-env`。
模型密钥仅在后端读取，不进入前端、日志或仓库。
