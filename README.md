# A-Quant Lab

A股研究与模拟决策工作台。首期定位为**自用研究与模拟**，不连接券商、不自动交易、不提供付费荐股或代客理财。

2026-10-15 株冶与独立凭证续批：株冶九项及招商轮船/星宇股份/山推股份三份报告13项均正常入库，本次自328补22、剩306；全仓现1711条，完整API总验收进行中。甘肃能源整份五字段严格外部源证明完成，旧24项专项最后缺口关闭，原canonical链接及585计数不改。15日20:30原日更失败后，Luna定点恢复原始证据，修复海通发展回执错绑，125只原严格准入通过；15日快照及M/E/B第三观察日已保存并只读核验，M/E净值990,338.86元（−0.966114%）、B977,064.29元（−2.293571%），当日无新订单/成交/费用，累计M−B13,274.57元、归因残差零。两实际API均返回3/5日。最后S2报告仍运行，主API未重启，已修复scheduler下次仍16日20:30。见[完整批次、实际数字和验收边界](docs/zhuye-gansu-and-meb-20261015.md)。

2026-10-15 现金流完整批次结算：自用户提出剩371项后正式补齐43项，现剩328；原585累计补257。当前主Docker完整API1689/1689接受（44.803秒），固定120只1592/1920、89个API财务计算桥，121条续批SQL与89桥SSR通过。完成现金流断行/跨页、完整括注和归母权益粘行修复；235项相关检查、旧8/19和3份补审重放、隔离20项及整批独立复核通过。旧CFO外部源审计24项金额全一致、23项严格独立通过，另1项甘肃能源旧凭证缺口单列，89桥不等于该专项全部通过。本轮43项最早16日08:45可用；今晚20:30日更与M/E/B第三日等待实际档案，原ACTIVE跟进保持。见[实际金额、财务计算与完整验收](docs/cashflow-formal-batch-and-meb-20261015.md)。

2026-10-15 现金流续批：东鹏饮料605499 H126五项18:41正常入库，主Docker完整API1651/1651接受（99.981秒）。修复原文三行CFO布局，122测试、旧8份证明/19条事实完整重放、隔离晋升及独立复核通过；原1646条及旧审核/链接保持。原585累计补219、剩366，固定原120只1554/1920核验、85只完整；新增五项最早16日08:45可用，旧快照1539项可用/15项待截止。83条续批SQL及85桥生产SSR通过。今晚15日20:30日更和M/E/B第三观察日仍待实际保存，现有跟进保持。见[金额、正式验收与后续缺项](docs/dongpeng-cashflow-and-meb-followup-20261015.md)。

2026-10-15 最新财务结算：渤海租赁000415 H126五项15日12:49正常入库，主Docker完整API1646/1646接受（125.532秒），原585累计补214、剩371；固定120只1549/1920核验、85只完整、优先20只320/320。五项最早16日08:45可用，当前旧快照1539项可用、10项待后续截止；原对象、旧链接及78条续批SQL核对通过，85个生产计算桥重新渲染通过。用户明确要求今晚跟进后，已恢复原有跟进任务；产品日更仍为15日20:30，M/E/B已保存2/5日，第三日等待今晚实际档案。见[本次五项金额、正式验收与试运行汇总](docs/bohai-financial-and-meb-followup-20261015.md)。

2026-10-15 续批：修复14日日更的跨平台来源路径，复用原证据于15日08:29补跑保存14日实际价格快照与M/E/B第2/5日观察，08:47全流程PUBLISHED/exit0，9步骤通过、提供方调用0次。M/E净值1,006,264.86元（+0.626486%）、B996,557.29元（−0.344271%），14日无新成交/费用，累计M−B9,707.57元、归因残差零。山推股份000680 H126五项正常入库，当前完整API1641/1641接受；固定原120只1544/1920核验、85只完整，原585累计补209、剩376，新增五项15日08:45后可用。14日原行情中已有124只市值原值，但缺当日正式聚合收据链，S2保持关闭。scheduler来源修复及60分钟总预算已部署，68项来源测试、31项调度测试及独立复核通过，预热API未重启；下一产品日更15日20:30。见[恢复过程、实际金额和完整验收](docs/daily-recovery-and-formal-s2-stage6-20261015.md)。

2026-10-13 Docker 真实部署已更新：当前入口 [http://127.0.0.1:8080](http://127.0.0.1:8080/)，API 与调度器均健康。原数据和账本保留，Windows API/worker及恢复任务已停用。前端产物逐字节匹配，五宽度浏览器 44 项和实际证券交接 5 项通过；首次财务深查的长时 PDF 重放单独记录。见[Docker 更新与验收](docs/docker-frontend-release-20261013.md)。

2026-10-14 正式财务第五批：渤海租赁000415 H125五项经正常门禁实际入库，当前Docker完整API1636/1636事实接受。固定原120只核验1539/1920、85只完整，原585已补204、剩381；优先20只320/320保持。新增事实最早14日08:45可用，13日快照仍225项、11只完整，全局S2门禁保持。Luna补采14日真实日历和完整索引，旧原文、旧事实/审核/链接及85个计算输入保持；52测试、独立复核、旧19/8源重放和68条精确SQL通过。实际完整API耗时163.630秒，保留首次延迟限制，未重启已预热服务。产品14日20:30日更继续；聊天跟进自动化当前PAUSED，保持暂停。见[本批实际金额、来源与验收](docs/formal-s2-stage5-20261014.md)。

2026-10-13 剩余585项续批：已补齐199项、仍缺386项，固定120只正式核验1534/1920、85只财务输入完整，优先20只320/320保持。当前Docker完整API1631/1631事实通过，63条续批SQL及旧对象保持逐项核对，85桥生产组件实际渲染通过；首次冷读仍1800秒超时，后续查询129秒，耗时限制单独记录。新增事实14日08:45或更晚才可用。今晚日更PUBLISHED，13日实际价格模拟M/E各20笔成交、费用203.14元、净值1,001,325.86元；B110笔、费用556.71元、净值1,002,095.29元。M−B为−769.43元（价格贡献−1,123.00、少付费用+353.57），E−M为零，账本与归因残差为零，已观察1/5日；Docker迁移后已再次逐账核对。当前快照正式S2实际可用225项、11只完整，全局门禁保持；下次日更14日20:30，由Docker scheduler执行。下一批渤海租赁续表边界、春秋航空扫描财报源和真实修订版本已完成只读分组，原文直接采集由Luna负责。见 [财务实际阶段结果](docs/formal-s2-remaining585-20261013.md)及[首日成交净值与归因](docs/remaining585-meb-observation-20261013.md)。

2026-10-13 正式财务补缺收束：原 95 项全部补齐，优先 20 只达到 320/320；其余 100 只的 500 个报告期首轮处理完成，正式接受 1015/1600 项、49 只财务完整，全池合计 1335/1920 项、69 只完整。未通过的身份、修订和核验缺口保留，11 条原始事实未计入通过数。财务读取隔离及单次清单/原文标题加速通过独立复核，新服务冷读 HTTP200 为 258.2 秒，刷新 9.0 秒，生产组件 69 个真实计算桥渲染通过。新增事实最早 14 日 08:45 可用；今晚 20:30 日更和 M/E/B 成交、费用、净值及归因跟进保持。见 [完整实际结果与证据](docs/formal-s2-priority-and-pool-expansion-20261013.md)。

2026-10-13 个人使用流程优化：主要入口收敛为今日、研究、模拟组合、复盘；研究选择接入同版本模拟资格核验，维持不动可保存原判断与理由。未计算状态、草稿复核和确认后的计划恢复已修正；数据诊断、原快照工具和工程对照按需展开。构建、11 项组件交互检查及相关接口/界面契约验证通过；独立 Chrome 最终真实服务和隔离服务五种宽度各 44 项、隔离真实点击 16 项通过，服务冷启动等待单独记录。见 [前端流程优化记录](docs/frontend-user-flow-20261013.md)及[浏览器验收记录](docs/frontend-browser-acceptance-20261013.md)。

2026-10-13 正式 S2 输入续批：复用原报补核 8 个失败证券期间，新增 18 条正式事实；优先 20 只需求覆盖 225/320，完整 120 模拟池为 225/1920，公式财务完整证券从 7 增至 11。逐股真实分母、精确代码映射、原始市值收据重放和有限完整输入排名已接入研究页；旧研究严格重放保持 6,179 个代码和 3,268 个排名。日更新增 Luna 同日收盘市值工具和实际 S2 输入报告，市值失败不阻断 S1/M/E/B；13 日 20:30 的真实采集与成交净值仍待运行，自动跟进已更新。原快照、全局 S2 门禁及前向规格保留。见 [正式 S2 与市值续批记录](docs/formal-s2-market-inputs-20261013.md)。

2026-10-13 财务与归因续批：Luna 补采 92 只证券、三期间共 276 条实际利润记录，模拟池可选财务及有效 TTM 从 28/120 提升至 120/120；新快照保留 12 日行情及 13 日实际 cutoff，旧账本和前向方案不改。20 家五期间的 100 份官方 PDF 已完成核验：28 份全字段通过、34 份部分通过、38 份无字段入库，新增 207 条正式事实。7 只证券公式财务事实完整但待后续 cutoff，正式 S2 仍关闭。12 日逐券/行业归因、现金与净资产桥已上线，残差为零；下一日更及五日 M/E/B 已设置自动跟进。实际数据、核验和剩余范围见 [财务与归因交付记录](docs/financial-attribution-followup-20261013.md)。

2026-10-13 最新候选工程账本已完成 10 月 12 日实际价格模拟：20 笔成交、0 拒单、费用 200.33 元、净资产 986,677.67 元、净收益 −1.332233%，对账通过。行情在 13 日补采，明确标为延迟工程计算。日更恢复后发布 6,172 条报价、3,268 个排名，120 只模拟证据中 115 只合格；M/E/B 已在 13 日开盘前登记连续五交易日观察，当前 0 个观察日，H 等待用户选择与确认。下一更新为 13 日 20:30。实际数字、核验和限制见 [本批交付记录](docs/engineering-forward-trial-20261013.md)。

2026-10-11 原设计续批已完成优先候选官方公告刷新（20 个完整查询、27 份核验原文）、真实四组合暴露/行业集中度/换手/未成交与容量参与率统计，以及实验页原判断和工程草稿的真实读取。新信息日期与旧行情日期分别保留，旧草稿不改写；日更已接公告刷新和非阻断降级。实际数字、来源与下一交易日验证见 [原设计续批记录](docs/original-plan-next-batch-20261011.md)。

2026-10-11 最新研究已接入人工模拟：真实核验 120 只主板输入，116 只通过当前资格；20 券工程草稿已保存，等待 10 月 12 日执行行情。每日工作日 20:30 更新与独立 Windows worker 已恢复。九月 M/E/H/B 真实归档回放及逐券、逐行业会计贡献已接入实验页。当前本机入口为 `http://127.0.0.1:8000/#research`、`#experiments`；启动入口 `tools/start_local_trial.ps1` 沿用已存费率配置。范围、真实试运行数字和未完成验证见 [本批交付记录](docs/latest-simulation-trial-20261011.md)。

2026-10-09 研究候选 v2 已补齐 349 个活跃行业缺项：观察 6,179 个官方代码、取得 6,172 条报价，3,267 只普通工商排名，其中北交所 166 只；活跃报价行业未知降至 0。行业体系、版本与逐代码原文证据分别展示，金融单列新增五项原报告值及同分组、同期间、同报表族比较。原 3,101 个排名代码日线已覆盖研究日，新增 166 个仍缺历史窗口，页面明确标注；研究卡及三股比较可查看财务计算桥、权重贡献、反证、有限公告与日线留痕。范围、缺口及试运行证据见 [S2 研究候选交付记录](docs/s2-structured-delivery.md)。该观察清单独立于现有 S1 快照和模拟资格，不以旧 900 只样本代表全部范围。

## 先跑起来（Docker）

```powershell
docker compose up --build      # 构建并启动
# 打开 http://127.0.0.1:8080
docker compose down            # 停
docker compose down -v         # 停并清空数据（从零开始）
```

单容器同源：前端用相对路径请求 `/api`，因此不需要 CORS，也不需要反向代理。
Compose 把端口绑定到 `127.0.0.1`，只用于本机试运行。
容器**用内置合成夹具自举一份快照**，所以一起来界面就有内容，不需要挂载任何东西——
顶部黄色水印会写明这是虚构示例数据。

> ⚠️ **这不是生产形态。** Compose 显式使用 `AQUANT_IDENTITY_MODE=LOCAL_LOOPBACK_DEMO`，
> 并把 API 端口绑定到宿主机 `127.0.0.1`；API 把 `user:demo` 作为固定的单用户主体标签。
> `X-Aquant-Subject` 仍然是自报 header，不是身份认证、Bearer 令牌或生产凭证。
> 未声明身份模式的 API 会保留开发兼容接线，但 `/api/v1/readiness` 的 `trial.ready` 会被身份门禁阻断。
> 生产形态（API 与前端分开、令牌纪律）见 ADR-013，尚未落实。

身份模式通过 `AQUANT_IDENTITY_MODE` 显式声明。可用值是：

- `LOCAL_LOOPBACK_DEMO`：本机单用户试运行；同时满足部署端口只绑定宿主机 loopback，
  以及固定 `AQUANT_TRIAL_SUBJECT`。容器内的 TCP 对端可能是 bridge 地址，不能把它当作宿主机绑定证明。
- `DEVELOPMENT_SELF_REPORTED`：开发测试用自报 `X-Aquant-Subject`，不具备试运行身份 assurance。

`AQUANT_TRIAL_SUBJECT` 只是主体标签，不是共享秘密，不应被当作前端凭据。API 未设置
`AQUANT_IDENTITY_MODE` 时会报告 `UNDECLARED_SELF_REPORTED`，因此不会把自报 header
误报成可试运行的身份边界。示例配置见 [`configs/api.example.env`](configs/api.example.env)。

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
$env:AQUANT_IDENTITY_MODE='LOCAL_LOOPBACK_DEMO'
$env:AQUANT_TRIAL_SUBJECT='user:demo'         # 标签，不是共享秘密
$env:PYTHONPATH='src'
# 费率在 API 启动时读取；修改环境变量后必须停止并重新启动 API。
python -m uvicorn main:app --app-dir apps/api --host 127.0.0.1 --port 8000
```

### 用 Docker 读取调度器维护的真实快照

基础 `docker-compose.yml` 使用独立命名卷和合成快照。真实试运行必须显式叠加
`docker-compose.real.yml`；覆盖层同时启动 API 与独立 scheduler，并让它们在同一 Linux
运行环境中访问 `/data`。不要让 Windows worker 同时持有这份 SQLite：Windows 与 Linux
容器跨系统并发打开同一个 WAL 文件会产生 `disk I/O error`。

```powershell
if (-not (Test-Path real-trial.env)) { Copy-Item configs/real-trial.example.env real-trial.env }
# 编辑实际配置；已存在的 real-trial.env 不要覆盖。
python tools/docker_runtime_env.py

docker compose --env-file deploy/local-trial/docker-runtime.env `
  -f docker-compose.yml -f docker-compose.real.yml build aquant

# 首次从 Windows 迁移：核验原绑定/进程，停止宿主运行、备份后启动 Docker。
# Docker 已运行时，此命令会拒绝 Windows 再操作它持有的 WAL。
& tools/switch_to_docker_trial.ps1

# 已在 Docker 运行时更新：不重复上面的跨系统交接。
docker compose --env-file deploy/local-trial/docker-runtime.env `
  -f docker-compose.yml -f docker-compose.real.yml up --build -d

Invoke-RestMethod http://127.0.0.1:8080/api/v1/readiness | ConvertTo-Json -Depth 8

# 明确停止真实 Docker 后，才能恢复宿主 worker。
docker compose --env-file deploy/local-trial/docker-runtime.env `
  -f docker-compose.yml -f docker-compose.real.yml down
Enable-ScheduledTask -TaskName 'AQuant Scheduler Worker'
Start-ScheduledTask -TaskName 'AQuant Scheduler Worker'
```

覆盖层要求 `AQUANT_HOST_DATA_DIR` 与 `AQUANT_HOST_PIPELINE_DIR` 已存在，并继续沿用
基础 Compose 的 `127.0.0.1:8080` 绑定。前者保存快照、账本与 worker 心跳，后者保存行情
缓存和流水线留痕。停止容器不会删除宿主机真实数据；不要对这条真实目录使用
`docker compose down -v` 作为清理手段。

未配置佣金时，真实数据仍可用于状态、候选、研究卡和证据等只读接口；
`preview` / `freeze` / `execute` / `value` 会在领域费率闸门返回拒绝，
因为合成费率算出的成交与净值没有依据（§12.6）。要走模拟写链路，必须提供使用者实际券商约定的费率。

## 本地开发（不用 Docker）

```powershell
# --- 终端 1：启动 API（主入口所需） ---
$env:PYTHONPATH='src'
$env:AQUANT_IDENTITY_MODE='LOCAL_LOOPBACK_DEMO'
$env:AQUANT_TRIAL_SUBJECT='user:demo'
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
Invoke-RestMethod http://127.0.0.1:8000/api/v1/readiness
cd apps\web
npm install
npm run dev                         # http://localhost:5173
```

页面顶部应显示合成数据水印；`/api/v1/status` 能返回 `snap-syn-001`。
`/api/v1/readiness` 会显示 `identity.assurance=LOCAL_SINGLE_USER_LOOPBACK`，
并明确 `productionAuthentication=false`；这条试运行只验证本机链路，
不代表真实数据质量或生产部署已验收。

## 早期开发基线（保留历史记录）

以下状态与 900 只/65 日等数字属于此前开发基线；2026-10-11 的实际研究、模拟及日更状态以顶部交付记录为准。

**研究链路与模拟账本在合成与真实数据上都已跑通并有机器可验的断言；
数据层受免费源限制，还不足以支撑投资结论。**

| 范围 | 状态 | 说明 |
|---|---|---|
| 资料包自检 | ✅ | contracts/schema/configs/examples，裸 Python 可跑 |
| M1 数据与证据底座 | 🟡 | 合成快照 D01–D08 通过；真实 current 快照 900 只 / 65 日行情 / 535 日交易日历已发布 |
| M2 基线与模拟账本 | ✅ | 双快照两日 + 跨进程重启的合成验收 16/16 通过；T+1 真的跨日生效 |
| 产品闭环 | 🟡 | 预览→确认→冻结→执行→估值→对账的合成闭环通过；冻结/已执行计划可在刷新后恢复；真实双快照跨日证据仍待下一交易日形成 |
| 工作台界面 | 🟡 | 四个主要任务入口，研究—模拟—复盘衔接已优化；独立 Chrome 五宽度/真实点击通过，冷启动性能观察见浏览器记录；**界面上的数字有出处检查**（见下文） |
| 研究作业与证据 | ✅ | 作业提交/执行/幂等、模型独立抽取、引用可定位、交叉核对 |
| 每日流水线 | ✅ | 采集→快照→**因子落库**→质量闸门；界面**设置页**可配时间与启停（ADR-014） |
| 研究卡上的因子数值 | 🟡 | 因子落库与卡片读取链已接通；旧 `f10-v1` 因 TTM 公式错误已撤回，须以 `f10-v2` 和决策日总市值重新验收 |
| agentctl 接入 | 🟡 | Q0 成立；唯一只读能力 `aquant.research_card.read` 已接 M1 真实存储（含因子值）；其余七个能力未接 |

**数据层的已知限制（这些不影响流程验证，但影响结论）：**

- 只有 **65 个交易日**，做不了回测；当前仅有 2 个真实快照，历史时点均为抓取后重建
- 行业分类**没有变更历史**，只能标注为 `RECONSTRUCTED`，回答不了历史时点
- 公司行为在真实数据里只有 **2 条**分红记录；代码路径验证过，覆盖率没验证过
- **券商佣金没有权威值**（券商约定），必须由使用者提供；印花税与过户费有权威来源并已留证
- **上市天数门槛（120 个交易日）已接长日历**：行情仍只保存因子所需的 65 日，
  快照另带至少 120 个交易日的权威日历；旧快照仍按其自身短日历披露「不足以判定」

## 验证（全部以退出码为结论）

| 检查 | 命令 | 目的 |
|---|---|---|
| 资料包自检 | `python tests/validate_spec.py` | 契约、示例、SQL、约束 |
| 领域与接口测试 | `pytest tests` | 711 个用例（本次全量通过） |
| 界面交互 + 数字出处 | `python tools/check_ui_flow.py` | 真浏览器点击；每个区块的数字必须说得出出处 |
| 归档字节离线复核 | `python tools/verify_archive.py` | 留证字节与摘要一致 |
| 真实数据闭环 | `python tools/check_real_flow.py` | 真实快照上走完人工点选、差异留痕与模拟账本闭环；条件不足时写 `ENV_NOT_READY` |
| 双侧验收 | `python tools/check_both_sides.py` | **同一套断言**在合成与真实上各跑一遍 |
| 因子落库 | `python tools/compute_factors.py` | 结果带特征版本并回读；`f10-v1` 结果只供审计，不能沿用旧 832/900 结论 |
| 数字出处（单跑） | `node apps/web/tools/check_number_provenance.mjs --url …` | 见下文 |
| HEAD 可复现性 | `python tools/check_reproducible.py` | 干净导出后全量测试仍通过 |

联网的脚本需要放行本机透明代理网段（`AQUANT_TRUSTED_PROXY_NETWORKS`，见 ADR-002）。
全部只做只读抓取，不下单、不连券商。

各类数据对 S1、F10/S2、模拟账本和 AI 分析的必要性、缺失时的降级边界及当前
主备来源，见 [`docs/data-requirement-matrix.md`](docs/data-requirement-matrix.md)。

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
`tools/publish_universe_snapshot.py`，每次生成新的唯一物理 ID。S1 行情门槛通过后
即可原子切换 `deploy/universe-snapshot/current_snapshot.json`；F10 缺失或失败会
标记降级并告警，不再拖停价格型 S1。旧物理目录与 `meta.sqlite` 不会被覆盖。

需要手工发布或回放时，先延迟切换 current，记下命令输出的 `snapshot_id`，再完成后续闸门：

```powershell
python tools\collect_market_caps.py --as-of 2026-09-18
python tools\publish_universe_snapshot.py `
  --data-dir deploy\universe-snapshot `
  --cache deploy\agentctl-q0\universe-bars.json `
  --pool configs\real-pool-csrc.yaml `
  --financials deploy\agentctl-q0\financials-cache.json `
  --actions deploy\agentctl-q0\dividend-actions.json `
  --market-caps deploy\agentctl-q0\decision-market-caps.json `
  --check-market-caps `
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
# 旧 f10-v1 的 832/900 结果已撤回；修正公式与决策日总市值输入后重新验收，
# 不再沿用旧版本的覆盖率或排除数量。
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

| 判定 | 依据 | 长日历的作用 |
|---|---|---|
| 决策时点是否已上市 | `listed_on` 与决策日比较 | 可判（与窗口无关） |
| 上市未满 120 个**交易日** | 快照内独立交易日历计数 | 上市日在日历范围内精确计算；早于日历起点且日历已覆盖 120 日时可由单调性判定达标 |

旧快照或降级回放若没有足够长的日历，窗口外标的仍不会被误排除，且预览会在
`notes` 明示「不足以判定」；严格生产发布会拒绝少于 120 个交易日的日历。

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

### 真实 S1 试运行怎样录入和复核佣金

佣金是券商与客户的合同参数，没有统一的公开权威值；优先由使用者从自己的券商约定中填写。费率用小数比例表示，
例如万分之 2.5 写成 `0.00025`；最低佣金用整数分表示，5 元写成 `500`。两个环境变量必须同时设置，
缺少任意一个时仍只能读真实数据，`trial.ready` 不会放行真实模拟写链路，也不会把缺失的最低佣金当成 0：

```powershell
$env:AQUANT_COMMISSION_RATE='0.00025'
$env:AQUANT_COMMISSION_MIN_CENTS='500'
# 已运行的 API 不会热加载；停止旧进程后，用同一组环境变量重新启动 API。
```

无法立即取得合同费率时，只有在使用者明确接受的情况下，才可把万 2.5、最低 5 元作为试运行假设，并额外设置：

```powershell
$env:AQUANT_COMMISSION_SOURCE='USER_APPROVED_ASSUMPTION'
```

该来源可以放行本地试运行费用计算，但界面会持续提示它不代表真实合同或账单。取得实际费率后，替换两个数值并把
`AQUANT_COMMISSION_SOURCE` 改为 `USER_CONFIGURED`。不设置来源时，为兼容既有部署，成对填写的数值仍按
`USER_CONFIGURED` 处理。

重启后用 API 回读实际生效的费率和来源：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/fees | ConvertTo-Json -Depth 5
Invoke-RestMethod http://127.0.0.1:8000/api/v1/readiness | ConvertTo-Json -Depth 8
```

真实 S1 试运行的硬门槛是 `readiness.trial.ready=true`，而不是顶层 `ready` 或
`trial` 之外的 `data.readiness=READY`。合同费率回读应显示 `commissionSource=USER_CONFIGURED`；明确批准的临时
假设显示 `commissionSource=USER_APPROVED_ASSUMPTION`。两者都必须同时满足 `syntheticTestRate=false` 并回显
实际生效的 `commissionRate` 和 `commissionMinCents`；
`commissionSource=UNCONFIGURED_DEFAULT` 表示示例假设，不能当作券商费率。`provenance` 会把印花税和过户费列为
有出处的规则，把佣金列为 `CONTRACTUAL`（没有权威来源）；它证明的是来源分类，不替用户证明合同数字。

当前可编辑位置是仓库根目录、不会提交的 `real-trial.env`；修改后重建 API 容器。后续设置页将提供同一组字段的可视化
编辑入口。运行状态抽屉会区分 `USER_CONFIGURED`、`USER_APPROVED_ASSUMPTION` 与
`UNCONFIGURED_DEFAULT`。`python tools\check_real_flow.py` 也要求两个数值成对出现；
不完整时只做只读与时点检查并明确跳过真实写路径。

决策快照除了 `asOfTime` 必须早于执行日，还必须在执行日 09:30（北京时间）之前已经实际发布。
事后补发的历史重建快照会保留 `RECONSTRUCTED` 水印，可用于工程复核，但不会被 `trial.ready`
或 `tools/check_real_flow.py` 当成当时可用的决策证据。

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

四个主要任务入口（主文档 §5.2）：**今日 / 研究 / 模拟组合 / 复盘**。数据诊断与高级工具、每日更新设置在「数据与设置」入口。
视图可深链：`/#today`、`/#research`、`/#portfolio`、`/#experiments`；旧工具深链 `/#workspace`、`/#settings` 继续有效。

### 写链路

| 步骤 | 接口 | 说明 |
|---|---|---|
| 预览 | `POST /api/v1/plans/preview` | 只算不冻；订单只用执行日**之前**的价格；可用 `selected_instrument_ids` 提交当次 S1 候选子集 |
| 取令牌 | `POST /api/v1/plans/{id}/confirmation` | 服务端签发，绑定主体/计划/快照/账户版本/预览哈希 |
| 冻结 | `POST /api/v1/plans/{id}/freeze` | 五项复核；令牌一次性消费；成功后写入与计划绑定的决策日志 |
| 恢复 | `GET /api/v1/plans?portfolio_id=...` | 返回冻结/已执行计划及双快照绑定；页面刷新后可继续执行、估值和对账 |
| 执行 | `POST /api/v1/plans/{id}/execute` | 按 §12 规则模拟成交 |
| 估值 | `POST /api/v1/valuations` | 强制使用请求中的已发布 EOD `snapshot_id`，保存快照、执行计划和 `as_of`；不变量失败则不发布净值 |
| 对账 | `GET /api/v1/portfolios/{id}/reconcile` | 逐项核验订单、费用、现金、批次 |

真实生产计划必须显式绑定两份已发布且不同的物理快照：
`decision_snapshot_id`（决策截止时间必须早于执行日开盘）和
`execution_snapshot_id`（必须是执行日收盘快照）。单快照兼容路径只用于合成演示；
冻结时会把两份快照及各自截止时间持久化，之后切换 `current_snapshot.json` 不会改写已冻结计划。
人工计划不会让客户端提交任意证券：服务端会用决策快照重新计算 S1，并拒绝不在该候选集内或重复的标的。
冻结后，模型原方案、人工最终方案与客观差异分别留档；直接接受模型方案记为 `ACCEPT_MODEL`，
点选后方案发生变化记为 `MODIFY_MODEL`。

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
node apps\web\tools\check_user_flow.mjs  # 组件交互与状态检查，不启动浏览器
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
