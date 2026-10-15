# 2026-10-11 每日研究与模拟更新恢复

用户授权恢复原设计中的每日更新。当前路径为 `AQUANT_DAILY_MODE=RESEARCH_SIMULATION`，API 只登记请求，独立 worker 调用同一 `tools/daily_run.py` 入口。

## 实际运行证据

- 2026-10-11 21:30:56（北京时间）真实 worker 手工运行一次，结果 `SKIPPED`：已归档、经原始响应哈希核验的完整 2026 年交易日历标记休市；`providerSubprocessesStarted=0`。
- 日历 receipt：`rcpt_ef478d38230f60fe708fe36e`；原始字节哈希：`sha256:d92e18f9a98c67452d1017495ceb1afb620477421e6e1e853dad9fa185f6047b`。
- 产品调度启用，工作日 20:30，北京时间下一次为 **2026-10-12 20:30**。解释器修正为 `C:/Users/admin/AppData/Local/Programs/Python/Python314/python.exe`；数据目录 `deploy/universe-snapshot`。
- Windows `AQuant Scheduler Worker` 已重新注册：登录触发与每 5 分钟恢复检查，Hidden 启动，最近恢复检查返回 0。持久启动载入 `real-trial.env` 的已有费率假设。
- 本次恢复产生单一 worker PID **44120**，`scheduler-0072ee89` 心跳已持续更新；重复执行启动脚本保留该进程。启动脚本用具名 Mutex 防止恢复任务与手工启动同时产生两个 worker。
- 旧 `daily-run.lock` 的 PID 95043 已确认不存在，且当时没有本项目采集/流水线/worker 进程；锁原文保留在 `deploy/agentctl-q0/lock-recovery/daily-run-20261011T133031Z.lock`，检查与移动记录在该目录 `audit.jsonl`。

机器证据见 `tmp/daily-runtime-evidence-20261011.json`、`tmp/daily-schedule-restored-20261011.json`、`deploy/agentctl-q0/last-run.json`。PID 是本次观察值，root 集成重启后应读取新心跳。

## 更新与发布契约

1. 先离线核验年度日历；休市立即跳过，无数据源调用。
2. 调用 Luna 编写的行情采集 CLI，请求全部当前官方范围代码。完整请求计数必须等于 baseline `poolSize`；请求失败、断路器提前停止或漏请求拒绝发布。真实空响应仍逐券披露，不虚构报价。
3. 离线按新行情重算候选；财务、行业、公告及其他静态输入沿用其真实日期和哈希。
4. 模拟池为最新前 120 个 MAIN 候选，另加不可变待执行草稿与非四组合工程回放账户的 ACTIVE 现持仓。旧选择跌出最新排序时仍保留其执行证据需求，不伪造当前排名；上市身份、日线与实际执行日状态仍须真实重核。
5. Luna 模拟证据 CLI 接收 `--retain-instruments attempt/retained-instruments.json`。文件 schema 为 `aquant.simulation_retained_instruments.v1`，含 `trading_day`、排序后的精确 `instrument_ids`、逐券 `reasons` 和明确披露的非 MAIN 跳过项；collector 须将它的路径与 SHA 记入 `source_inputs`。
6. 离线准入证据、发布不可变候选快照并检查 S1 核心质量，全部通过后才切换研究出版和 current。可选财务缺项保留 DEGRADED 标记。

每次尝试保存独立目录。成功的行情与研究刷新分别写 hash manifest；重试核验 baseline/quotes/coverage/staged SHA，并对 staged 研究执行 canonical 准入后复用，避免 `generatedAt` 重写造成 simulation checkpoint SHA 冲突。若重试期间新增需保留 MAIN 股票，创建新尝试并保留旧来源包。失败记录到本地运行与告警 JSONL，旧研究和 current 保留；promotion 部分写入异常时恢复两侧指针，PREPARED 提交可离线恢复。

## 验证与边界

`tests/unit/test_daily_research_run.py` 的 17 个用例通过，覆盖日历原文/时点拒绝、零调用休市、全范围请求缺陷、核心质量失败、部分提升回滚、稳定 SHA 断点续跑、草稿/持仓保留与新选择切换尝试。XML 结果：`tmp/daily-research-tests-20261011.xml`。调度/API/流水线已有相关测试在本批初步集成中全部通过，后续只重验受影响的日更用例。

**尚未发生的验证**：10 月 12 日真实 provider 日更和执行数据尚未产生，不能把恢复的调度或工程回放当作这次未来更新成功。直接采集仍由 Luna 执行；本次调度恢复只跑已验证休市路径。
