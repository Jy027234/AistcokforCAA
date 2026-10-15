# 新版前端独立 Chrome 验收

2026-10-13，用户授权独立 Chrome 测试运行器，验收新版前端的视觉、真实点击和手机体验，并直接修复本批范围内的问题。范围沿用[个人研究与模拟流程目标](frontend-user-flow-20261013.md)：今日 → 研究 → 模拟组合 → 复盘。

本批前端布局和真实点击闭环通过。最终版本在独立 FastAPI 服务上完成五种宽度的 44 项检查及 16 项真实点击检查；既有本机真实数据服务恢复响应后，也在最终构建上完成五种宽度的 44 项只读检查。验收期间遇到的服务冷启动等待单独记录，不将其解释为已完成性能优化。

## 执行方式与边界

- 直接启动已安装的 Chrome，使用新建独立 profile、独立调试端口和 Headless 模式；鼠标/触摸/输入经 Chrome DevTools Protocol 发出，未用 DOM `.click()` 代替点击。
- 没有连接统一桌面 Chrome/PiP 或 in-app Browser，没有更改、关闭既有浏览器标签或桌面应用。仅释放本次运行器自己启动的 Chrome。
- `127.0.0.1:8000` 使用已有真实研究资料，只读浏览及导航；网络记录确认未发送 POST/PUT/PATCH/DELETE。
- `127.0.0.1:8877` 为本次新建的独立 FastAPI 服务和临时 SQLite 账本。浏览器请求实际产品 API，未 mock fetch；输入为明确标注的合成证券、历史测试时钟和测试快照。资格读取采用测试适配，交易写端点保留实际校验。只在这份账本中生成订单、成交和净值。
- 手机为 Chrome 触摸和视口仿真，未验证实体手机、Safari、系统软键盘或读屏器。截图与点击不构成完整无障碍符合性或真实供应商验证。

## 修复与截图依据

| 具体问题 | 本批处理 | 验证依据 |
| --- | --- | --- |
| 手机工具菜单越过左边界，设置入口无法触达 | 菜单按顶栏两侧定位；导航后收起 | 步骤 09；触摸进入每日更新设置通过 |
| 820px 平板固定高度顶栏遮挡品牌/导航 | 顶栏允许换行，状态区独占一行 | 五种宽度、四页面的顶栏边界检查；820px 截图 |
| 小屏标题和提示文字相互挤压 | 标题不收缩，提示允许换行 | 360/390px 今日、研究、模拟组合、复盘截图 |
| 账户未创建显示 HTTP404 错误，容易被理解为页面故障 | 显示“尚未创建模拟账户”；无账户不能记录维持决定 | 真实服务账户空态截图；既有行为检查 |
| 切换任务沿用旧滚动位置，进入复盘时跳过标题 | 切页回到顶部并将焦点移至主内容；保留筛选与草稿 | 步骤 03、08，真实导航点击 |
| 折叠的财务诊断仍自动读取，拖住其他 API | 第一次展开后才挂载；纯导航复用已加载资格 | 实际加载调查及相关组件回归；服务端校验保持 |
| 确认后的事实刷新可能复用确认前仍在进行的读取 | 等旧读取结束后重新读取持久化事实 | 原有草稿恢复检查中新增在途读取回归；独立复核通过 |
| 保存后的长草稿 ID 撑宽手机页面；移动浏览器会扩展布局宽度，使旧断言漏检 | 长等宽标识允许任意位置换行；按指定设备宽度检查页面与 innerWidth | 最终步骤 06–09、360/390px 已保存记录页面检查 |

## 验证结果

| 检查 | 结果与证据 |
| --- | --- |
| 最终 TypeScript / Vite 构建 | 通过；当前静态文件引用 `index-clw8TavG.js`、`index-BJZ4LHPB.css` |
| 用户流程组件检查 | 11/11 通过；涵盖导航保留、来源拒绝、维持失败/提交锁、原计划恢复及写后事实重读；最后仅 CSS/浏览器脚本变化，复用仍有效结果 |
| 相关 API / 界面契约 | 59 项通过；最终 CSS 构建后重跑界面产物契约 19/19，其余后端检查未因本批末次 CSS 改动失效 |
| 最终隔离 FastAPI 五种宽度 | 44/44 通过；1440×1000、1280×900、820×1180、390×844、360×800，各含四个主要页面；另含手机触摸导航、工具入口、无额外写入及无运行时异常 |
| 最终真实点击闭环 | 16/16 通过；真实 HTTP 写入独立账本，顺序见下方九步 |
| 最终真实数据服务只读验收 | 恢复响应后五种宽度 44/44 通过，21 张截图；最终版本静态产物逐字节一致，未写入用户账本 |
| 独立复核 | 写后读取分支独立运行 1/1 通过；最终四张手机截图独立复核通过 |

浏览器证据保存在：

- 最终五宽度：[live-report.json](../tmp/frontend-browser-acceptance-20261013/final-accepted-fixture/live-report.json)。
- 最终真实点击：[write-report.json](../tmp/frontend-browser-acceptance-20261013/final-written-mobile/write-report.json)。
- 最终真实数据只读：[live-report.json](../tmp/frontend-browser-acceptance-20261013/final-live-after-recovery/live-report.json)。
- 前次真实数据只读：[live-report.json](../tmp/frontend-browser-acceptance-20261013/final-all-widths/live-report.json)。

最终隔离账本有 1 份草稿、1 份冻结计划、2 次决策、1 笔订单、1 笔模拟成交、4 条现金记录和 1 份净值。维持决定只增加决策记录，订单、成交、现金、计划与净值数量不变；相关 API 测试另确认 12 张交易事实表逐行不变。

## 九步真实点击与截图

### 1. 研究与比较 — 通过

实际搜索并将两只候选加入比较，研究卡把关键依据、反证与风险放在模拟入口之前。当前可见资料明确为合成输入；不能将这些数值解释为投资结论。

![01 研究与比较](E:/IT/A股量化交易/tmp/frontend-browser-acceptance-20261013/final-written-mobile/01-desktop-research-compare.png)

### 2. 保存选择并等待行情 — 通过

证券和研究来源随选择交接；保存理由后显示等待执行日行情。真实数据库中此时有草稿，没有计划或成交。等待状态使用文字说明，不靠颜色表达。

![02 保存后等待行情](E:/IT/A股量化交易/tmp/frontend-browser-acceptance-20261013/final-written-mobile/02-desktop-saved-waiting.png)

### 3. 复核并返回继续 — 通过

测试服务发布执行快照后复核；往返研究和模拟组合，筛选与已复核状态保留。此截图展示返回后的页面上部，确认按钮是否可操作由实际 DOM 和后续点击验证。

![03 复核后返回](E:/IT/A股量化交易/tmp/frontend-browser-acceptance-20261013/final-written-mobile/03-desktop-reviewed-choice.png)

### 4. 人工确认并冻结 — 通过

确认后保存真正的持久计划 ID，出现执行入口；数据库仍没有成交。冻结与执行分别显示，未将确认伪装成成交。

![04 人工确认与冻结](E:/IT/A股量化交易/tmp/frontend-browser-acceptance-20261013/final-written-mobile/04-desktop-frozen-plan.png)

### 5. 模拟执行、净值与对账 — 通过

实际调用执行、估值和对账端点，生成 1 笔模拟成交及已发布净值，逐项一致。页面区分预计费用、实际成交和估值来源；不是真实交易承诺。

![05 成交净值与对账](E:/IT/A股量化交易/tmp/frontend-browser-acceptance-20261013/final-written-mobile/05-desktop-valued-reconciled.png)

### 6. 手机刷新并查看账本 — 通过

390px 触摸仿真下刷新，已执行计划从服务端恢复，仍可进入估值、对账和明细。长标识换行，未撑宽页面。

![06 手机恢复执行账本](E:/IT/A股量化交易/tmp/frontend-browser-acceptance-20261013/final-written-mobile/06-mobile-executed-ledger.png)

### 7. 手机保存维持决定 — 通过

输入维持理由并实际提交；成功状态、原理由和回看入口可读。保存没有改变订单、成交、现金或估值。

![07 手机维持决定](E:/IT/A股量化交易/tmp/frontend-browser-acceptance-20261013/final-written-mobile/07-mobile-keep-decision.png)

### 8. 回看原判断 — 通过

复盘读取原理由，说明未关联冻结计划的维持决定没有可归属的执行结果。后续草稿 ID 可换行；不会用账户累计收益填补这次判断的效果。

![08 手机原判断复盘](E:/IT/A股量化交易/tmp/frontend-browser-acceptance-20261013/final-written-mobile/08-mobile-review-original.png)

### 9. 手机工具与设置 — 通过

菜单完整位于屏幕内，实际触摸进入每日更新设置后收起。截图只能确认可见布局，完整键盘 Tab 顺序和读屏语义没有在本批独立验收。

![09 手机工具菜单](E:/IT/A股量化交易/tmp/frontend-browser-acceptance-20261013/final-written-mobile/09-mobile-utility-menu.png)

## 真实服务冷启动观察

在最后长标识换行修复期间，8000 服务于本机时间 13:19:54 发生重启。静态文件正常提供，随后两次浏览器复测在启动数据加载页等待 45 秒超时，`/api/v1/status`、`/api/v1/candidates`、`/api/v1/events` 的只读探测各等待 5 秒也未返回。没有把这些骨架屏接受为视觉通过截图。

同一进程随后恢复，独立只读核验 `/api/v1/health` 与 `/api/v1/status` 均为 200，约 0.04 秒返回；最终五宽度 44 项复测通过。进程先前累计 CPU 约 265 秒、内存约 1.48GB，目录有 18 份已发布快照，现象符合冷缓存和昂贵请求串行排队；没有当时的调用栈，不能确认具体持锁请求。未发现持续死锁或新的 Traceback。

保留加载失败记录：[首轮](../tmp/frontend-browser-acceptance-20261013/final-accepted-live/live-report.json)、[复测](../tmp/frontend-browser-acceptance-20261013/final-accepted-live-retry/live-report.json)。本批未停止真实服务、改写其数据或松开共享 SQLite 请求锁。冷启动响应时间与目录计算内存可作为后续运行性能事项，不扩展本次已通过的前端交付范围。

## 重复运行

在仓库根目录执行，只读检查可直接使用当前本机服务：

```powershell
node apps/web/tools/browser_acceptance.mjs --url http://127.0.0.1:8000 --out tmp/frontend-browser-acceptance-next/live --devices 1440x1000,1280x900,820x1180,390x844,360x800
```

写入验收须在另一终端启动新建隔离服务，不能指向用户账本。夹具每次默认创建新临时目录；写入运行器拒绝已经存在决策或草稿的账本：

```powershell
python tools/frontend_acceptance_fixture.py --port 8877
node apps/web/tools/browser_write_acceptance.mjs --url http://127.0.0.1:8877 --out tmp/frontend-browser-acceptance-next/isolated-write
```
