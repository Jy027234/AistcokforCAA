# 每日流水线的调度接入

**脚本：** `tools/daily_run.py` · **护栏：** `src/aquant/operations/pipeline.py`
**留痕：** `deploy/agentctl-q0/daily-runs.jsonl`

---

## 它做什么

```
采集行情（窗口起点固定） -> 发布不可变物理快照（暂不切 current） -> 因子落库
-> F10 质量闸门 -> 提升 current_snapshot.json -> 追加运行留痕
```

三件手工流程没有的东西：**休市判断、并发锁、运行留痕**。

生产发布由 `tools/publish_universe_snapshot.py` 完成：每次写入
`<data-root>/api/datasets/<snapshot-id>/` 的新目录，禁止覆盖已有物理快照；
`tools/promote_snapshot.py` 只负责在后续闸门通过后原子替换
`<data-root>/current_snapshot.json`。`daily_run.py` 默认延迟提升，只有行情、因子和
质量闸门全部通过才会切换 current。

## 最小本机试运行（合成数据）

先确认 API 与合成快照可读，再接入真实采集。这个步骤不联网、不需要佣金，也不跑全量测试：

```powershell
# 终端 1
$env:PYTHONPATH='src'
python -m uvicorn main:app --app-dir apps/api --host 127.0.0.1 --port 8000

# 终端 2
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health
Invoke-RestMethod http://127.0.0.1:8000/api/v1/status
```

状态响应应包含 `snap-syn-001`，并标记合成数据水印。需要看主界面时，在第三个终端直接到
`apps\web` 运行 `npm install` 和 `npm run dev`；默认页面读取上面的 API。只有显式访问
`?mode=demo` 时才需要先运行 `python tools\build_workspace_fixture.py`。真实数据流水线前，
先停止这份合成 API，避免把演示数据目录与真实目录混用。

### 因子那一步为什么是"落库"而不是"算一遍"

第三步原先跑的是 `tests.integration.t12_f10_real`：它读采集缓存、在内存里算一遍、
写一份验收报告 JSON。结果是两个都自洽、却互不相干的事实：

| 看到的现象 | 实际状态 |
|---|---|
| 运行留痕里 `计算 F10 因子 OK`，报告写 `computed: 832/900, PASS` | 快照库里 `research_run` / `feature_value` **都是 0 行** |
| 研究卡接口一切正常 | 卡片上永远没有数值（`该快照上尚未计算任何因子`） |

研究卡读的是 `research_run` / `feature_value`，而验收脚本从来不写库——
**"算过"与"算出东西给产品用"是两件事**，中间那一步没人做。

现在分成两步：

| 步骤 | 命令 | 作用 |
|---|---|---|
| 因子落库 | `tools/compute_factors.py` | 走产品自己的路径（与 `POST /api/v1/research/jobs` 的因子作业同一条代码），写 `research_run` + `feature_value`，并**回读**确认行数 |
| F10 质量闸门 | `tests.integration.t12_f10_real` | 值域、中位数量级、亏损股是否被截断为 0、覆盖率——判数值本身可不可信 |

两步都失败不改变"快照已发布"这个事实，但各自告警：前者意味着**研究卡没有数值**，
后者意味着**数值可能不可信**。这两句话不一样，因此不能合成一条。

前提：快照必须**带上 financials 数据集**。生产发布 CLI 从 `--financials` 指定的
`financials-cache.json` 取数据（默认是 `deploy/agentctl-q0/financials-cache.json`），
再按研究池过滤。没有它，因子会全部标成
「快照未包含财务数据」——落库成功、900 行，一行值都没有。

## 怎么接

现在有**两条**路，选一条即可（也可以都留着：锁与"每天至多一次"的判据都是幂等的）。

### 方式一：产品内调度（推荐，ADR-014）

配置在界面**设置**页（`GET/POST /api/v1/schedule`），执行交给独立 worker：

```powershell
# 长驻 worker：到点自动跑，也执行界面上「立刻运行一次」的请求
python tools\scheduler_worker.py

# 只处理一轮就退出（cron / 排查用）
python tools\scheduler_worker.py --once

# 立刻跑一次（不等到点）——与界面按钮是同一条通道
python tools\scheduler_worker.py --now
```

| 项 | 行为 |
|---|---|
| 默认 | **停用**。装上就自动每天抓数据，是使用者没做过的决定 |
| 启用条件 | 必须填解释器；留空会被拒绝（见下） |
| 到点判断 | 每天至多一次；周一至周五（可关）；worker 晚起也会补当天那一次 |
| 界面按钮 | 只**登记请求**，执行仍由 worker 做——两条入口共用一条通道 |
| 结果 | 留痕文件仍是权威；`pipeline_last_run` 是给界面读的摘要 |

**它不在跑的时候，界面上的配置不会让任何东西自动跑。** 这句话写在设置页上，
因为它的反面（"我配了 20:30，数据却三天没动"）是这类功能最常见的失败方式。

解释器为什么必填：这条流水线依赖 baostock 与 pytest，而启动 worker 的那个
python 未必装了它们。用错的后果是每天都失败，而失败信息是
「baostock 未安装」——看起来像数据源坏了，不像配置写错了。

希望 worker 开机/登录就起来，把它挂成一条**登录时启动**的任务即可
（不是定点跑脚本——那会让"到点没跑"重新变成没人看得见的失败）：

```powershell
$action  = New-ScheduledTaskAction -Execute "E:\IT\Agent\.venv\Scripts\python.exe" `
  -Argument "tools\scheduler_worker.py" -WorkingDirectory "E:\IT\A股量化交易"
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "AQuant 调度 worker" -Action $action -Trigger $trigger
```

### 方式二：系统调度直接跑流水线

**Windows 任务计划程序**（每日 20:30 北京时间，收盘后）：

```powershell
$action  = New-ScheduledTaskAction -Execute "E:\IT\Agent\.venv\Scripts\python.exe" `
  -Argument "tools/daily_run.py --skip-if-done" -WorkingDirectory "E:\IT\A股量化交易"
$trigger = New-ScheduledTaskTrigger -Daily -At 20:30
Register-ScheduledTask -TaskName "AQuant 每日流水线" -Action $action -Trigger $trigger
```

**cron**（Linux/WSL）：

```cron
30 20 * * 1-5 cd /path/to/repo && /path/to/python tools/daily_run.py --skip-if-done >> deploy/agentctl-q0/cron.log 2>&1
```

`--skip-if-done` 是给定时任务用的：当天已成功发布过就直接退出，
避免重复触发时又跑一遍采集。

## 退出码

| 码 | 含义 | 调度方应如何对待 |
|---|---|---|
| 0 | 已发布，**或**按计划跳过（休市、已有运行在跑） | 正常 |
| 1 | 失败（采集失败 / 快照未通过校验） | **应当告警** |
| 2 | 环境缺失（缓存或研究池不存在） | **应当告警** |

`tools/scheduler_worker.py` 自己也有退出码：`--once` / `--now` 时
0 = 本轮无事可做或执行成功，1 = 本轮执行失败，2 = 数据目录不可用
（没有 `meta.sqlite`——它连"该写哪本账"都不知道）。

0 里包含"跳过"：休市不是错误，报失败会让任务重试到天亮而结果不变。
但**跳过与发布都会写留痕**，因此"昨天到底跑了没有"永远可查：

```powershell
Get-Content deploy/agentctl-q0/daily-runs.jsonl -Tail 5
```

## 需要你知道的事

### 1. 物理快照不可变，current 是独立指针

每次发布都会生成新的唯一物理 ID，形如 `snap-eod-<日期>-<随机串>`，内容写入
`api/datasets/<snapshot-id>/`。已发布目录和 `meta.sqlite` 都不会被覆盖；
当前使用哪个对象由同一数据根目录下的 `current_snapshot.json` 指向。
API/worker 默认解析这个指针，`AQUANT_SNAPSHOT_ID` 只用于显式回放一个已经发布的 ID。

手工发布时可用 `--defer-promotion` 留下候选物理快照，等因子落库与质量检查完成后执行：

```powershell
python tools\promote_snapshot.py `
  --data-dir deploy\universe-snapshot `
  --snapshot-id $snapshotId `
  --require-factors
```

旧部署里出现的 `snap-universe` 仍可作为显式兼容 ID 或合成默认值；生产 current
不再依赖这个固定 ID。要回放历史，只需保留对应物理目录并显式指定其 ID。

### 2. 时点是"重建"，不是"当时观察"

每日快照的 `as_of_time` 是当天收盘，而数据是当晚抓的。
这一天的快照在当天就是重建的，`available_basis` 记为
`RECONSTRUCTED` 是准确的。

**真正的 `LIVE_OBSERVED` 从这个脚本第一次被定时执行时才开始累积。**
在此之前的所有快照都是重建的——这一点无法通过补抓历史来改变，
只能从现在开始攒。

### 3. 窗口起点固定，不会自己往前长

`WINDOW_START`（默认 `2026-06-22`）是硬编码的。窗口跟着当天滑动的话，
两次运行覆盖的日期范围不同，快照之间就没法比较。
需要更长的历史时**显式改它并重新采集**。

### 4. 快照 ID 由**实际拿到的末日**校验后才发布

脚本在采集之后核对缓存里的实际末日：目标日休市或数据尚未更新时，
采集不会报错（只是没有新行），此时按计划跳过并留痕，
而不是发布一个"ID 说 A、内容是 B"的快照。

### 5. 采集缓存 v3 会触发一次全量重采

采集器要求的当前 `rows_format` 是 **3**，行情行包含
`adjusted_close_cents`。采集器发现某只标的仍有 `rows_format=2`（或没有该字段）的旧行时，
会忽略这只标的的旧行，按目标窗口重新抓原始收盘和前复权收盘；成功后写回 v3，
后续运行才恢复增量续跑。这个升级是按标的进行的一次全量窗口重采，不需要手工删除缓存。

升级期间如果请求失败，旧行不会被静默当成新口径，失败会留在缓存的 `failed` 中；
重新运行同一窗口即可继续。只有需要无条件重抓整个缓存时才使用 `--refresh`。

### 6. 真实生产计划必须绑定两份快照

真实数据上的计划必须同时提供 `decision_snapshot_id`、`decision_cutoff_at` 和
`execution_snapshot_id`，并且决策快照与执行快照必须是不同的已发布物理快照。
决策截止时间必须早于执行日开盘；执行快照必须是执行日收盘快照。单快照兼容路径只
给合成演示使用。冻结时会把两份快照及截止时间固化，后续 current 指针变化不会改写计划。

## 崩了留下的锁要人来清

`deploy/agentctl-q0/daily-run.lock` 存在时，新的运行会**立刻被拒**
（不是排队等待）。程序**不会**自动夺取陈旧锁：无法区分
"上一次还在跑"与"上一次崩了"，自动夺取会在长任务上制造两次并发写。

确认上一次确实已经不在跑之后，手工删除该文件即可。

## 已知缺口

* **失败告警没有接**。脚本只写留痕与退出码，把它接到邮件/IM
  是运维侧的事（本项目不引入消息中间件，见 §14.2）。
* **调度 worker 默认不在跑**。脚本、界面配置、退出码与留痕都齐了，
  但"每天到点跑"这件事仍需要一个长驻进程：没有它，界面上配置**不会**
  让任何东西自动跑（设置页会把这句写出来）。
* **财报与公司行为没有进每日流程**。F10 依赖财报缓存，
  而财报按季度更新，不该每天抓。当前是手工在季报季跑一次——
  这是一个**已知的、有意的**手工环节，不是遗漏。但注意：快照必须带上
  `financials` 数据集，因子才算得出值（见上文）。
* **`t12_f10_real` 失败不会撤回物理快照**：候选快照和已落库的因子仍可审计，
  但当前指针不会切换，运行留痕会记录步骤失败；上一份完整 current 继续提供服务。

## 失败告警

流水线失败时会**告警**，顺序是固定的：**先落盘，再外发**。
先外发再落盘的话，外发一失败告警本身就没了——而那正是最需要它的时候。

`powershell
python tools/show_alerts.py --days 7      # 最近 7 天的 ERROR；有 ERROR 则退出码 1
`

落盘位置 deploy/agentctl-q0/alerts.jsonl（JSONL，可直接 grep）。

**外发通道是可选的**：设 AQUANT_ALERT_WEBHOOK 为一个接受 POST JSON 的地址
（企业微信/钉钉/自建网关都可以），不设就只落盘。
本项目**不替使用者决定用哪家 IM**，也不引入消息中间件（§14.2）。

三件刻意为之的行为：

| 行为 | 理由 |
|---|---|
| 外发失败**不**让流水线失败 | 通道坏了不等于流水线坏了 |
| 但外发失败会记一条 WARNING | 不记的话，下次会被误以为告警已经发出去了 |
| 没配通道时返回 delivered: false | 调用方不该以为告警已经发出去了 |

**哪些情况会告警**：缺采集缓存 / 缺研究池、采集失败、缓存里没有任何行情、
快照构建未通过校验、**快照已发布但因子未落库**、**F10 质量闸门未通过**。
中间两条容易漏：快照发布成功会让人以为一切都好，而研究卡上没有任何数值；
数值质量有问题则更隐蔽——数字看得见，只是不可信。

**哪些情况不告警**：休市、数据尚未更新、已有一次运行在进行中。
这些是**按计划跳过**，写在运行留痕里但不告警——把正常状态做成告警，
会让真正的告警被忽略。

## 实跑留痕（2026-09-19，数据日 2026-09-18）

```
[07:17:35] OK   采集行情（1028.6s）   5219 只，321132 条行情，失败 0 只
[07:17:38] OK   发布快照（1.8s）      证券 900 只，行情 58484 条
[07:17:42] OK   因子落库（3.9s）      回读：feature_value 900 行，其中 832 行有值
[07:17:43] OK   F10 质量闸门（0.7s）  T12 F10 真实验收 7/7 通过
[07:17:43] OK   切换当前快照（0.3s） snap-eod-2026-09-18-53df1f23e8754984
[07:17:43] 结果 PUBLISHED
```

落库后的复核（不是看返回值，而是重新查库、再走一遍产品接口）：

| 检查 | 结果 |
|---|---|
| 最新 `research_run` | `status=SUCCEEDED`，`feature_version=f10-v1` |
| 最新 `feature_value` | 900 行，其中 832 行 `raw_value` 非空 |
| `instrument.listed_on` 非空 | 900 / 900 |
| `snapshot_dataset` | 含 `financials`（此前没有这个数据集） |
| `GET /status` | current 为新物理 ID，`dataMode=PRODUCTION` |
| `GET /candidates` | 899 只候选，响应快照与 current 一致 |
| `GET /instruments/SH.601169/research` | 200，研究卡快照与 current 一致并留档 |
| `POST /valuations`（未配置佣金） | 422 `FEE_VERSION_UNVERIFIED`，只读服务不受影响 |
