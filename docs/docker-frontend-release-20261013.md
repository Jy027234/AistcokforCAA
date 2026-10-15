# Docker 新版前端部署与验收

本批按用户授权完成 Docker 重建、保留原数据并启动，当前入口为 [http://127.0.0.1:8080/](http://127.0.0.1:8080/)。API `a-aquant-1` 和调度器 `a-scheduler-1` 均运行且健康，镜像为 `aquant-lab:local`，ID `sha256:4942e0f7475f772d77a40606f7fc46af271b55dfe2fa8adbbe0922485a2997c4`。

## 新版与原数据

8080 提供的 `index-clw8TavG.js`、`index-BJZ4LHPB.css` 与工作区已验收构建逐字节一致。实际研究身份保持 `research-closing-d3b4023adce25ae4d74d`，6,179 个代码、3,268 个普通工商排名；当前模拟资格 117 只，决策快照为 `snap-eod-2026-10-13-b650098e11644f0e`。没有重新采集供应商资料或生成测试订单。

保留三项真实绑定：

| 宿主项目目录 | 容器目录 |
| --- | --- |
| `deploy/universe-snapshot` | `/data` |
| `deploy/agentctl-q0` | `/app/deploy/agentctl-q0` |
| `data` | `/app/data` |

交接先核验原绑定、项目 PID/进程及恢复任务，停止 Windows API/worker、禁用 `AQuant Scheduler Worker`，完成 WAL checkpoint 和一致性 SQLite 备份，再启动 Linux API/worker。原调度和费率不改写；容器每轮使用 `/usr/local/bin/python`，沿用 `RESEARCH_SIMULATION` 日更。今日任务已完成且无待请求，没有因切换额外补跑。

启动后及全部验收后两次核对：**30 张业务/调度表、160 个原始文件完全一致**。主要记录保持 16 个账户、52 份计划、1,115 笔订单、1,112 笔模拟成交、4,248 条现金记录、52 份净值、3 份研究草稿。没有删除卷、重置库或重写原判断。

备份和证据：

- [SQLite 一致性备份](../tmp/docker-redeploy-20261013-220239/meta-backup.sqlite)
- [交接前清单](../tmp/docker-redeploy-20261013-220239/before.json)
- [启动后清单](../tmp/docker-redeploy-20261013-220239/after-start.json)
- [验收后清单](../tmp/docker-redeploy-20261013-220239/after-acceptance.json)
- [原运行状态](../tmp/docker-redeploy-20261013-220239/handoff.json)

## 必要部署修复

旧配置未接入当前财务资料和研究日更，原归档又保存 Windows 绝对路径，直接重建会使 Linux 读取失败。本批补齐当前输入挂载和环境、PDF 复核依赖 `pypdf 6.16.2 / pdfplumber 0.11.9 / pypdfium2 5.7.1`，并显式设置 `AQUANT_RESET_DATA=0`。

显式路径映射将原数据目录定位到 `/data`、项目根定位到 `/app`，最长根优先并拒绝越界。研究重建、模拟来源、财务证据、PDF/修订/补证、日历与归档成员接入该读侧映射；**原 JSON、SQLite、理由、草稿、哈希、公式和可用时点校验保持不变**。未用改写原资料或跳过校验的方式适配。

私有容器配置由 `tools/docker_runtime_env.py` 根据原 `real-trial.env` 生成，原 env 不改写。实时数据和私有 env 不进入镜像。`tools/start_local_trial.ps1` 拒绝在项目 Docker 正持有数据时启动 Windows API/worker；交接脚本也拒绝重复由 Windows 操作 Linux 正持有的 WAL。

## 验收

| 检查 | 结果 |
| --- | --- |
| Docker 构建、导入、上海时区及 PDF 依赖 | 通过 |
| 相关本地研究/模拟/事实读取回归 | 初轮 277 项、受影响补测 44 项及补证相关 65 项通过；集合有重叠，不相加 |
| 独立路径/私有配置复核 | 28 项通过，保留原字节、时点及篡改拒绝 |
| Linux 镜像相关测试 | 73 项通过，使用隔离库；路径/研究/补证/NO_CHANGE/模拟/共享请求 |
| HTTP 与前端文件 | 8 项通过；健康、状态、readiness、快照、研究、模拟 context、两份前端文件 |
| readiness | `trial.ready=true`，调度 worker 心跳正常 |
| 五宽度浏览器 | 44/44，1440/1280/820/390/360px，四主要页面、手机触摸、布局及运行异常检查 |
| 实际证券交接 | 5/5，桌面/手机真实点击，同来源证券保留、手机布局、无写入及异常 |
| 数据保留 | 两次比较均为 30 表、160 文件不变；原研究产物字节也不变 |
| Windows/Docker 互斥 | 重复交接和宿主 API 启动在 SQLite 操作前拒绝 |

原始记录：[HTTP/资产](../tmp/docker-api-acceptance-20261013/report.json)、[五宽度浏览器](../tmp/docker-browser-acceptance-20261013/live-report.json)、[实际证券交接](../tmp/docker-browser-acceptance-20261013/handoff-final/report.json)。

实际流程：

1. 今日 — 通过，未计算状态如实显示。
2. 研究 — 通过，原名单、依据、反证与日期正常读取。
3. 模拟组合 — 通过，未创建个人账户显示正常空态。
4. 复盘 — 通过，原判断与工程记录分别呈现。
5. 研究进入模拟 — 通过，桌面/手机均接续同来源证券；不保存草稿或创建订单。
6. 手机工具与设置 — 通过，菜单在屏幕内，可真实触摸导航。

代表截图已保存并实际检查：

![Docker 实际研究](E:/IT/A股量化交易/tmp/docker-browser-acceptance-20261013/1440px-research.png)

![Docker 桌面证券交接](E:/IT/A股量化交易/tmp/docker-browser-acceptance-20261013/handoff-final/desktop-selection-handoff.png)

![Docker 手机证券交接](E:/IT/A股量化交易/tmp/docker-browser-acceptance-20261013/handoff-final/mobile-selection-handoff.png)

![Docker 手机工具菜单](E:/IT/A股量化交易/tmp/docker-browser-acceptance-20261013/mobile-utility-open.png)

## 单独保留的性能观察

补充财务深查 `/api/v1/research/financial-coverage` 首次在 240 秒未返回，两条并发线程仍在原有 PDF 重放解析，额外页面加载受影响。没有把这项写为通过。[只读调用栈](../tmp/docker-api-acceptance-20261013/slow-financial-read-stack.txt)显示耗时在 pypdf/pdfminer；路径映射未改变按原文哈希、证券、期间及解析器版本构成的缓存键。

保存证据后重新创建同镜像 API 容器以清理验收重负载，调度器保持运行。随后真实证券交接与健康检查通过，并再次核对全部保留事实不变。深查性能作为后续事项单列，本批未降低原文校验或重构财务规则。

只使用授权的独立 Chrome profile/调试端口；未连接或关闭 in-app Browser/统一桌面浏览器，未停止 Codex、ChatGPT 或 OpenAI AppX。手机是 Chrome 触摸及视口仿真，未宣称实体手机/Safari/完整无障碍验收。
