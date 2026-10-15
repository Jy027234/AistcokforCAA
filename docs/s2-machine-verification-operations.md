# S2 机器核验采集与正式准入（运行说明）

本文件说明 2026-09-28 实施方案落地后的实际入口。**这里写的是已经实现的命令，
不是设计中的 CLI。** 协议与代码边界见 `docs/adr/ADR-015-s2-alternative-data-provider.md`
与本目录 `s2-collection-implementation-plan-20260928.md`。

## 1. 这一步解决了什么问题

在此之前，唯一能把官方 PDF 数值变正式事实的路径是 v1/v2 人工协议：每个金额
都要一个 `HUMAN_VISUAL` 签署。方案要求的是"程序处理正常样本、人工只处理无法
消除的歧义"，因此新增了**有版本的机器核验协议**
`cninfo-s2-machine-verified-v1`。

机器核验不靠"模型说有把握"，而是要求每条正式金额至少有一条**与主抽取路径
不同的路径**证实，且必须是"数值相等"或"恒等式成立"：

| 路径 | 内容 | 适用字段 |
| --- | --- | --- |
| `parent_statement_mirror` | 母公司表节同一行名承载同一实数值（不同页区间） | 归母权益 |
| `equity_identity` | 归母权益 = 所有者权益合计 − 少数股东权益 | 归母权益 |
| `profit_identity` | 合并净利润 = 归母净利润 + 少数股东损益 | 归母净利润、合并净利润 |
| `revenue_identity` | 营业收入 = 营业总收入 − 利息收入 − 已赚保费 − 手续费及佣金收入 | 营业收入 |
| `revenue_note_decomposition` | **合并附注**『营业收入和营业成本情况』：主营业务 + 其他业务 = 合计（与利润表不同表、不同页） | 营业收入 |
| `cashflow_identity` | 经营现金流 = 现金净增加额 − 投资净额 − 筹资净额 − 汇率影响 | 经营现金流 |
| `cash_rollforward` | 期末现金 = 期初现金 + 现金净增加额（**只核对现金余额滚动**） | 不作为任何字段的独立路径 |

**两条已修正的假通过风险**（都比报错危险，因此单独记录）：

1. `cash_rollforward` 曾同时算作 `operating_cashflow` 的满足路径。它核对的是
   现金余额滚动，与经营现金流并不相等——实测 600519 2025H1 两者相差
   3,276,962.98 元。现在它只作为独立核对项保留，**不为经营现金流记功**。
2. 列头识别曾接受"同一行里最右两个日期"，因此正文里任何含两个日期的句子
   都可能凭空造出一个"表节"。现在要求该行必须出现 `项目` 或 `附注` 列头标记，
   否则不判为表头。

**不承认**的伪独立路径：把同一个抽取函数跑两遍、把两站同 PDF 哈希当作数值
独立、用比率反推金额、按"置信度"放行。读取侧会逐条检查"已满足"的路径是否
真的携带了另一处原文行（页码 + 原始单元格），因此手写一个
`"verified": true` 不会被放行。

机器核验者身份固定为 `machine:s2-machine-verifier-v1`，与人类 `reviewer_id`
分列存放；读写两侧都拒绝机器冒充人类签署。

## 2. 前置条件

- 本机透明代理需按进程放行伪 IP 网段（ADR-002）：
  `AQUANT_TRUSTED_PROXY_NETWORKS='198.18.0.0/15,2001:2::/64'`。
- **交易日历证据是当日件**：`calendar` 子命令要求上交所休市公告的观察日等于
  当天北京时间日期，跨日即失效。因此每次正式升仓前都要重跑 `calendar`。
- 事实库使用 append-only 的独立 SQLite，不跑应用迁移。

## 3. 实际命令

全部在仓库根目录运行。

### 3.1 冻结本期需求 manifest

字段—期间需求由 `required_s2_periods()` 生成，不写死"五份财报"：

```powershell
python tools/plan_s2_requirements.py `
  --latest-period 000333=2026-06-30 --latest-period 600519=2026-06-30 `
  --latest-period 601012=2026-06-30 --latest-period 000651=2026-06-30 `
  --latest-period 600276=2026-06-30 --latest-period 002415=2026-06-30 `
  --latest-period 300750=2026-06-30 --latest-period 688981=2026-06-30 `
  --output data/s2-pdf-review/s2-demand-manifest.json
```

2026H1 每只证券 16 个"字段×期间"值，八只合计 112 个。manifest 会如实标出
不在配置池内的证券（`NOT_IN_CONFIG_POOL`）与金融业排除项，不把排除算成抓取
失败。

### 3.2 刷新交易日历证据（每日一次）

```powershell
$env:AQUANT_TRUSTED_PROXY_NETWORKS='198.18.0.0/15,2001:2::/64'
python tools/admit_s2_industrial_pdf_review.py calendar `
  --archive-root deploy/agentctl-q0/forward-archive `
  --output data/s2-pdf-review/calendar-evidence-NEW.json
```

### 3.3 公司级共享公告索引（增量水位）

一次扫描服务该公司所有报告期，产出每只证券的状态与水位。**这是增量运行的
入口**：下一次运行以 ``watermarkBefore`` 为下界，只处理新增部分。

```powershell
python tools/collect_s2_company_index.py --through 2026-09-29 `
  --trusted-proxy-network 198.18.0.0/15 --trusted-proxy-network 2001:2::/64 `
  --output data/s2-pdf-review/s2-company-index.json
```

状态语义（覆盖报告直接汇总它）：

| 状态 | 含义 |
| --- | --- |
| `INDEXED` | 定报与全分类索引都在记录窗口内逐页耗尽 |
| `REVISION_REVIEW_REQUIRED` | 出现**财报更正类**标题候选（更正/追溯调整/重述/差错）→ 人工判断影响 |
| `INDEX_FAILED` / `CROSS_INDEX_FAILED` | 官方索引未翻页耗尽 → 不暴露候选 |
| `NO_LATEST_PERIOD` | manifest 没有该证券的最新已披露期间 |

标题命中数与更正候选数**分开报告**：``修订``/``补充``同样出现在公司章程修订
对照表、股东会补充通知上（实测 000651、300750），把它们算成财报修订会让正常
公司长期挂在异常队列。水位只表示"当时看到的查询窗口已翻页耗尽"，不是完整性
承诺；后续公告按增量事件处理。

### 3.4 机器核验升仓

默认是 dry run；`--commit` 才写事实库。

```powershell
python tools/promote_s2_machine_facts.py `
  --stock 000651 --period 2026-06-30 `
  --calendar-evidence data/s2-pdf-review/calendar-evidence-NEW.json `
  --fact-db data/s2-pdf-review/s2-machine-verified-facts.sqlite `
  --output data/s2-pdf-review/s2-machine-promotion/RUN.json
# 加 --commit 写入
```

每份报告的结果状态是结构化的，可直接汇总成覆盖报告：

| 状态 | 含义 |
| --- | --- |
| `PROMOTED` | 已写入正式事实与机器核验证据 |
| `DRY_RUN_NOT_WRITTEN` | 全部核对通过，但未指定 `--commit` |
| `REVISION_REVIEW_REQUIRED` | 发布窗口内出现财报更正类标题候选 → 人工异常队列 |
| `INDEX_INCOMPLETE` / `CROSS_INDEX_INCOMPLETE` | 官方索引未逐页到达末页 → 不暴露候选 |
| `REPORT_NOT_UNIQUE` | 定报分类里全文不唯一 → 失败关闭 |
| `VERIFICATION_FAILED` | 至少一个字段没有满足的独立路径 → 进异常队列 |
| `DOWNLOAD_FAILED` | 原文未取得或新归档字节不可复核 |

### 3.5 验收：四因子与排名

```powershell
python tools/verify_s2_factors.py --trading-day 2026-09-29 `
  --market-caps data/s2-pdf-review/decision-market-caps-20260929.json
```

`--placeholder-market-cap CODE=YUAN` 只在决策日真实总市值确实取不到时使用；
报告会把 F10 与全部排名显式标成**合成占位**，避免被读成产品数值。

### 3.6 只读覆盖报告（已发布快照）
```powershell
python tools/report_s2_readiness.py `
  --snapshot-dir deploy/universe-snapshot `
  --snapshot-id <已发布快照 ID> `
  --facts-db data/s2-pdf-review/s2-machine-verified-facts.sqlite `
  [--allow-reconstructed]
```

`--allow-reconstructed` 打开历史重建分支：保留真实 `first_seen_at`，按
`available_at` 与显式 `RECONSTRUCTED` 依据放行。**不加该开关时前向行为不变**
（仍要求 `first_seen_at <= cutoff`），因此今天补采的历史财报不会倒灌进昨天
已发布的前向决策。

### 3.7 日更入口（接入既有收盘后/盘前作业位置）

```powershell
python tools/run_s2_daily.py `
  --trusted-proxy-network 198.18.0.0/15 --trusted-proxy-network 2001:2::/64
```

它**不新建调度器**，只是把已经实现的步骤按顺序跑一遍并留下运行记录：
`calendar` → `collect_s2_company_index` → `promote_s2_machine_facts` →
`report_s2_coverage`。约束：

* 每步有限重试（默认 2 次）加退避，不做无限循环；
* 某只证券失败不终止整批，但日历证据拿不到就不做升仓（`available_at` 依赖它）；
* `--dry-run` 只做索引与核验，不写事实库；`--skip-calendar` 复用当日已有证据。

**结论必须从产物判定，不能只看退出码。** 实测不带代理跑一遍：四个子命令全部
exit 0，而 5 只证券状态全是 `INDEX_INCOMPLETE`。因此运行记录里有
`step_outcomes` 与 `overall_status`，按根因优先给出
`OK / PARTIAL / INDEX_ALL_FAILED / NO_SECURITY_PROMOTED / STEP_FAILED`，
非 OK 时进程返回 1。

### 3.8 覆盖报告（方案交付清单第 3 项）

汇总目标证券数 → 报告发现/归档数 → 必需字段数 → 已验证字段数 → 正式事实数
→ 有正式事实的证券数，并把**业务排除**与采集/核验缺口分开计数：

```powershell
python tools/report_s2_coverage.py `
  --dir data/s2-pdf-review `
  --facts-db data/s2-pdf-review/s2-machine-verified-facts.sqlite
```

它只读三份产物（需求 manifest、公司级索引报告、升仓运行报告）加事实库，不
重新核验 PDF、不写事实。两点实现细节值得注意，因为都曾导致**误报缺口**：

- 每个"证券×期间"取**最后生成**的运行结果，排序键是 `generated_at` 而不是
  文件名——`000651-run.json` 与 `scope-run.json` 的字典序与时间序无关。
- `ALREADY_PROMOTED`（重复运行的幂等结果）与 `PROMOTED` 一样算成功；否则
  已入库的证券会被报成核验失败。

## 4. PIT 语义

| 模式 | 首见过滤 | 可用性依据 | 用途 |
| --- | --- | --- | --- |
| `LIVE_OBSERVED` | 要求 `first_seen_at <= cutoff` | `OBSERVED` / `VENDOR_PIT` | 前向决策 |
| `HISTORICAL_RECONSTRUCTED` | **不过滤**首见 | 必须 `RECONSTRUCTED` | 有原始档案证据的历史回放 |

只有公告日期时，`available_at` 取"公告日之后第一个交易日盘前 08:45"，且不早于
来源公开、真实首见、核验完成与入仓时刻。

## 5. 备源：深交所适配器

`src/aquant/adapters/providers/szse.py` 把 2026-09-28 已实测的深交所检索请求
工程化，边界写进代码而不是只写在文档里：

- 请求形如 ``POST https://www.szse.cn/api/search/content``，字段
  ``keyword / range=title / channelCode[]=fixed_disc|listedNotice_disc /
  time / endTime / currentPage / pageSize``；方括号**原样**送出，与已归档
  探针请求体保持一致。
- 分页必须按服务端返回的 ``totalSize`` 遍历并去重：``pageSize=100`` 曾被规范
  为 50。分页不一致或出现重复文档 ID 即**失败关闭且不暴露候选**。
- 标题可能带 HTML 标签，解析时剥离；列表**没有独立证券代码字段**，所以关键字
  命中不等于身份校验通过，必须另取原文核对公司身份。
- ``docpubtime`` 只有日期级含义，解析不出来就保持缺失，不得编造发布时刻。
- ``is_full_report`` 要求标题**以**报告名结尾。仅做子串包含会把"关于参加…
  半年度报告业绩说明会的公告"判成财报原文（实测 300502 全类别结果里有这一条）。

`cross_source_evidence()` 比较两站同一份原文，并如实标注它证明了什么：
``byte_identical`` 可以有，``numeric_independence`` **恒为 False**——同哈希只
证明两站服务了同一份文件，不是独立的数值核实。

### 5.1 备源可用性核对

```powershell
python tools/check_szse_fallback.py `
  --trusted-proxy-network 198.18.0.0/15 --trusted-proxy-network 2001:2::/64 `
  --output data/s2-pdf-review/szse-fallback-check.json
```

对候选清单里每份报告：深交所检索 → 在公告日 ±2 天窗口内列出全文 → 逐份下载
→ 与巨潮归档**逐字节**比较。2026-09-29 实跑结果 **14/25 字节同一**（000651 五期
全部、002415 五期全部、300750 四期）。

两点实现决定值得记下：

- **身份由内容哈希判定，不靠窗口唯一性或标题。** 公告日窗口里出现多份全文是
  正常的（年报窗口会同时命中上年年报、一季报），早期版本因此把 12 份报成
  "不唯一"。现在逐份下载比对，找到字节同一的那一份为止，并为每次比较留下
  收据与哈希。
- **11 份"未找到"全部是沪市证券**（600276、688981）。深交所不服务沪市公司，
  这是来源市场的正常边界，不是工具缺陷——报告里保留每条的具体原因，不合并
  成一个笼统的失败数。

同哈希只支持把深交所当作**字节级备源**（发现与归档），它仍不是独立的数值核实；
本工具不做逐字段拼源，也不按"时间更晚"选择版本。

## 6. 已知边界（不要读成已解决）

- **机器核验的版式覆盖仍有限。** 实测 000651 三个半年期间五字段全部通过
  （两个年报期间只剩 `revenue` 未通过）；601012 两个年报期间 4/5 字段通过，
  只差 `operating_cashflow`。失败集中在表节/列头定位与行名换行，属于抽取器
  版式覆盖问题，不是数值或语义问题。失败项都带具体原因，不会被静默跳过。
  已识别但未解决的具体版式：
  - `合并及公司资产负债表`（000333）：合并与母公司同表，无法用表节区分口径，
    需要按列分区才能核对；在做到之前**不纳入机器核验**，以免把母公司数当合并数。
  - 同一表节里同时出现带附注的 `利息收入 五、55` 与一个裸 `利息收入`
    （000651 年报）：两者都匹配术语，取裸行会拿另一层口径去扣减营业总收入，
    使 `revenue_identity` 被错误判负。已加入"优先取带附注行"的规则，但该样本
    仍未通过，需继续查。
  - 现金流量表恒等式分量定位失败（002415 各期、300750 2025 年报、601012 年报、
    688981 各期）：报告未按"净增加额 − 投资 − 筹资 − 汇率"分列时，该恒等式
    无法建立。这是当前剩余缺口里最大的一块。
  - **主表 `revenue_identity` 在四家公司上无法成立，已由附注路径闭合其中三家。**
    这些公司的合并利润表同时列示 ``利息收入`` 与**与"其中：营业收入"完全相等**
    的 ``营业总收入``，两个事实互相矛盾，原文不足以判定：

    | 公司 | 营业总收入 | 其中：营业收入 | 利息收入 | 附注路径结果 |
    | --- | ---: | ---: | ---: | --- |
    | 688981 2026 半年报 | 38,635,115（千元） | 38,635,115（千元） | 912,210（千元） | **已通过** |
    | 300750 2025 年报 | 423,701,834（千元） | 423,701,834（千元） | 10,594,657（千元） | **已通过** |
    | 002415 2024 半年报 | 无该行 | 41,209,096,206.36 | — | **已通过** |
    | 601012 2024 年报 | 82,582,273,118.72 | 82,582,273,118.72 | 950,045,457.25 | 附注未定位，仍记为未通过 |

    做法不是"挑一个能算通的解释"，而是换一处原文：``revenue_note_decomposition``
    读**合并附注**的『营业收入和营业成本情况』，要求
    ``主营业务 + 其他业务 = 合计 = 候选``，并且附注页**不得**是利润表候选所在页
    ——否则同页不构成第二处原文。附注定位不到时如实报告未通过，**绝不**回退到
    利润表数值（那正是待核对的候选本身）。
  - 601012 的附注路径未定位成功，因此收入仍记为未通过；该分支已由其余三家的
    实跑证明可用，601012 需要单独查版式。
  - 空金额占位符已改为显式识别：中文财报用 ``-``/``——``/``不适用`` 表示
    "本期无此项"（实测 688981 ``利息收入 - -``）。早期把它们当数字会拿一个
    不存在的金额参与恒等式。``StatementRow.nil_amounts`` 现在标记这种行，
    ``component`` 直接跳过它们。
  - 附注引用有三种真实写法，归一化必须都覆盖：``五、54``（年报无边框版式）、
    ``(五)46``（实测 002415）与**不带章节号的裸编号** ``41``（实测 600519
    ``其中：营业收入 41 89,389,354,416.84``）。只认第一种会让 002415 整份报告
    无法定位表节；漏掉裸编号会让 600519 的收入行变成 ``其中:营业收入41``。
    两处合计把全库抽取失败数从 21 降到 14。
  - 行名**不会以裸数字结尾**，因此 ``_normalize_label`` 最后统一剥掉结尾数字。
    这条兜底很重要：附注编号究竟在抽取阶段被当成金额、还是在归一化阶段残留，
    都会让行名以数字收尾，而两种来源不需要分别处理。
  - 002415 的**合并利润表本身没有"营业总收入"行**（收入以"营业收入"为首行），
    扣减恒等式在这份原文上无法建立。这不是定位缺陷，因此该字段如实记为未通过。
  - 002415 部分期间的利润表列头只给出 ``本期发生额`` 一列（比较列被页眉打断或
    拆到相邻行），无法判定哪一列是当期列，因此失败关闭而不是猜一列。
- **`--trusted-proxy-network` 早期不生效。** 它只设置了 `os.environ`，而
  `default_policy()` 在构造时就已读过环境变量，实测仍报 blocked-address。
  现已改为显式重建策略，各工具可独立运行。
- **同一份报告重复升仓会被拒绝。** 首期事实已经是根版本，再次升仓报
  "an original report would create a second fact root"；修订场景需要版本包
  支持，尚未实现。
- **决策日真实总市值依赖网络。** 采集端腾讯主路径 + 东方财富分页备源；两侧
  都失败时不发布部分市场，也不拿别的日期市值顶替。2026-09-29 实跑两侧均失败，
  因此当次四因子验证用显式合成占位市值，报告已标出。
- 权威披露原文的用途权利仍按 `docs/data-rights-register.md` 的既有登记执行。

## 7. 可复现性说明

事实库中的 16 条事实由 `PROMOTED` 运行时写入；核验协议其后又收紧过两处
（见 §1 的假通过风险）。收紧只影响**新升仓**，已写入的证据仍能通过当前读取侧
校验（`load_with_accepted_s2_pdf_reviews()` 返回 16/16 全部接受）。若一定要用
最新协议重算，需要先删除该事实库再重新升仓，不能用同一事实库原地覆盖。
