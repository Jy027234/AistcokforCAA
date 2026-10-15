# 结构化财报数据源（东财）——接入说明与质检结论

**用途边界：研究 / 内部使用**（用户明确确认）。商用需另行确认权利状态，见
`docs/data-rights-register.md` 中该来源条目当前为 `UNKNOWN`。

## 为什么换主数据源

逐份解析官方 PDF 是"证据级"路径，但边际成本是"新增一家公司 ≈ 若干轮版式
适配"。**实测 40 轮只覆盖 8 只，这个斜率到不了全市场 5400 只。**

方案 `docs/data-requirement-matrix.md` 本就规定 S2 走"交易所/XBRL + 巨潮时点
与修订"，PDF 只是证据层。本次把主数据源换回结构化接口，**PDF 与机器核验降级
为抽样质检**——产能不再受版式适配限制。

## 实测数据

**吞吐**（904 只 × 3 期，全池实跑）：

```
CANDIDATE_COMPLETE             2623   (96.7%)
FINANCIAL_TEMPLATE_UNSUPPORTED   78   (26 只银行/保险/券商，正确排除)
SOURCE_UNAVAILABLE                9   (0.3%，真失败)
PARTIAL_FIELDS                    2
耗时 2122 秒（35 分钟），约 2.3 秒/只
外推全市场 5400 只约 3.5 小时
```

**与官方 PDF 的抽样质检**（抽 20 只**此前从未核验过**的证券，走完整 PDF 机器
核验后逐值对拍）：

```
MATCH              8
PDF_NOT_FOUND      6   ← CNINFO org 查询失败（小盘股）
PDF_UNVERIFIED     6   ← PDF 侧版式限制，非结构化源问题
MISMATCH           0   ← 最严重的一类，零
```

**另有 23/23** 与已入库 PDF 事实逐值一致的对照（000651 三期、600519 两期）。

## 采集到的坑（都已在代码里注明）

0. **巨潮 orgId 不止一种形态。** 早期白名单只允许 `gss[hz]`/`gshk`/`GD`/纯数字，
   而实际归档响应里还有 `gfbj`、`nssc`、`fzqy`、`DR`、`gqtg`——全池 904 只里
   **139 只（15.4%）**因此拿不到公告索引。**失败还是静默的**：索引返回空，
   看上去像"这家没有公告"。已按**族**改成 `(?:[A-Za-z]{2,4}\d{6,7}|\d{10})`，
   覆盖全部实测形态；安全性来自同一函数里**先按证券代码精确匹配**——
   orgId 只是匹配结果的载荷，不是身份判据。
   （实测归档响应里各形态条数：10 位数字 1934、`gssh` 326、`gfbj` 253、
   `gssz` 243、`nssc` 152、`gshk` 76、`GD` 63、`fzqy` 12、`DR` 6、`gqtg` 3。）

1. **合并净利润要直取 `NETPROFIT`**，不要用"归母 + 少数股东"相加——
   `MINORITY_INTEREST` 在很多期间为空，早期用相加导致 504 个期间被误判为
   "缺字段"（实际数据都在）。
2. **三张表每只只请求一次**，多期共用同一响应。早期 `period()` 每期重拉
   三张表，取 3 期就是 9 次请求，**请求数乘了期数**；修掉后提速 3.0 倍
   （138.8s → 45.8s / 20 只）。
3. **CNINFO 索引里的 `sec_name` 是简称**（"深科技"），PDF 封面是全称
   （"深圳长城开发科技股份有限公司"），不能直接用于 `check_report_cover`。
   抽样工具改为**从封面自取全称**，取不到多个候选时返回 None 交回失败关闭。
4. **金融企业用另一套科目**，一般企业模板返回"返回数据为空"。已单独分类为
   `FINANCIAL_TEMPLATE_UNSUPPORTED`，与"来源故障"分开计数——两者处置不同。

## 尚未完成（重要）

**这批数据还不是 PIT 可用事实。** 输出里每条都带
`pit_state: AWAITING_ANNOUNCEMENT_BINDING`——它只有：

* 五个字段值与单位（元）；
* 每值的来源报表、字段名、**收据 ID 与内容哈希**（可回溯到原始响应字节）；
* 采集时刻。

**不含"何时披露、有无更正"**，因此还不能按 PIT 语义使用。补齐这一步要接
**巨潮公告索引**（本仓已有 `client.report_index()` 与
`AnnouncementMetadata`，后者已带 `report_period`、`is_correction`、
`is_revision`、`announcement_date_cn`，正是所需字段）。

---

# 全市场落地结果（实测）

## 清单来源

**巨潮 `/new/data/szse_stock.json`**（与 `CninfoClient` 同域，一次 GET，响应归档留收据）：

```
6179 只 A 股（清单共 6259 条，排除 B股 79、CDR 1）
收据 rcpt_99688d9c...  哈希 sha256:56827339...
两次抓取内容哈希完全相同 → 清单稳定
```

**不是**东财清单接口：`push2.eastmoney.com/api/qt/clist` 在本机**不可达**
（`RemoteDisconnected`，3 次重试全失败）。备份方案是深交所/上交所官网
（实测可达，但要写两套解析）。

## 采集（6179 只 × 3 期 = 18537 条，耗时 3.8 小时）

```
五字段齐全                    15,874   (85.6%)
金融模板不适用                 2,115   (11.4%)  ← 705 只银行/保险/券商，按 S2 口径排除
无报告                          526    (2.8%)   ← 新上市等，非抓取失败
缺字段                           22    (0.1%)
来源失败                          0
```

按证券看：**5,270 只三期全部齐全**；排除金融行业后合格 5,315 只，其中
5,270 只齐全 = **99.2%**。

## 绑定（最新一期 2026-06-30，5279 条，耗时 3.7 小时）

```
RECONSTRUCTED_READY        5,041
NO_PERIODIC_REPORT           191
REVISION_REVIEW_REQUIRED      44   ← 有更正/修订，不猜哪份是当期值
INDEX_FAILED                   3
```

**orgId 预解析优化**：巨潮清单自带每只证券的官方 orgId，而同一只证券的 orgId
逐期相同。原实现每个期间都重查一次 `topSearch`，全市场 6179 只 × 3 期会多出
**18537 次请求（约 12.9 小时）**。给 `report_index` 加了**向后兼容**的可选参数
`organization_id`（不传时行为逐字节不变），绑定单条 **4.7s → 2.13s**。
不传时 `org_lookup_receipt_id` 记 `None`——那一步**确实没发生**，如实留空比
伪造收据诚实。

## 最终事实库

```
总事实        33,580
证券数         5,047          ← 目标 4000+
PIT 模式       97 LIVE_OBSERVED（官方 PDF 核验）
              33,483 HISTORICAL_RECONSTRUCTED
```

**消费端 PIT 门禁验证**（关键：证明无前视偏差）：

```
截止 2026-09-30(今)   放行 33,569 条 / 5,047 只   拒绝    11
截止 2026-07-15      放行  8,288 条 /   859 只   拒绝 25,292
                     └ 全部 S2_FACT_NOT_YET_AVAILABLE
```

以 7 月 15 日为截止时 **75% 的事实被正确隐藏**——那时半年报尚未披露。

## 数据保留

采集只在**结束时**写候选 JSON，中途失败会丢解析结果。但原始响应**逐条归档**，
因此写了 `tools/rebuild_structured_from_archive.py`：**不碰网络**从归档重建，
与在线采集**共用同一套解析代码**（`eastmoney_structured` 的字段映射），
不会出现两套口径。实测重建值与在线值逐值一致。

## 金融企业（银行/保险/证券）：**已打通**

早期只发一般企业模板（``RPT_F10_FINANCE_G*``），金融企业全部返回
"返回数据为空"，被整只判为"模板不适用"丢弃。**那不是数据缺失，是报表族选错。**

实测命名规律是 ``RPT_F10_FINANCE_<族><表>``，四族齐全：

| 族 | 前缀 | 报表名 | 实测 |
|---|---|---|---|
| 一般企业 | `G` | GBALANCE / GINCOME / GCASHFLOW | 000651 格力电器 |
| 银行 | `B` | BBALANCE / BINCOME / BCASHFLOW | 000001 平安银行 |
| 保险 | `I` | IBALANCE / IINCOME / ICASHFLOW | 601318 中国平安 |
| 证券 | `S` | SBALANCE / SINCOME / SCASHFLOW | 600030 中信证券 |

**关键：五个必需字段在四族里字段名完全相同**（``TOTAL_PARENT_EQUITY``、
``OPERATE_INCOME``、``PARENT_NETPROFIT``、``NETPROFIT``、``NETCASH_OPERATE``
在三家金融企业的报表里全部命中）。所以切换族**不需要改字段映射**，只需选对族。

族的判定：从 ``G`` 起逐个探测**资产负债表**，第一个有数据的族即为该证券类型，
随后三张表都用这个族。在线采集与离线重建**同序同规则**，结果必然一致。

**实测结果**：705 只里 **701 只拿到三期全齐**（修前只有 100 只）：

```
G 一般企业  601 只   ← 实为北交所公司，见下
B 银行       45 只
S 证券       49 只
I 保险        6 只
```

## 两个静默失效：交易所后缀与巨潮市场前缀

这两处都**不报错**——返回"返回数据为空"，与"该公司没有报表"无法区分。

### 1. 东财交易所后缀（影响 604 只）

后缀不是只按代码首位就能定完：

| 代码段 | 后缀 | 实测 |
|---|---|---|
| 6xxxxx / 9xxxxx（非 92） | `.SH` | 沪市 |
| 0xxxxx / 2xxxxx / 3xxxxx | `.SZ` | 深市 |
| 4xxxxx / 8xxxxx | **`.NQ`** | ``430017.NQ`` 有数据，``.BJ``/``.SZ`` 都空 |
| **92xxxx** | **`.BJ`** | ``920002.BJ`` 有数据（万达轴承），``.SH``/``.NQ`` 都空 |

早期把 4/8 段写成 ``.BJ``（错，251 只）、把 9 段一律当沪市（错，353 只）。

### 2. 巨潮市场前缀（影响 600 只）

``market_for_code`` 只列了深沪两市的三位前缀，**完全没有北交所**，于是北交所
证券在绑定公告时全部报 ``unsupported A-share market prefix``——结构化财报取得到，
但"何时披露"过不去，拿不到 PIT 时点。已补 43/83/87/88/92 段（按两位划分）
与深市 ``302`` 段。

### 教训

这两处都是**同一类缺陷的又一次出现**：一个数据源有多个取值维度（报表族、
交易所后缀、市场前缀），每加一个就漏一处，而失败长得像"数据不存在"。
**排查这类问题要看"失败集里有没有成片的代码段"，而不是逐个样本试错。**

## 尚未处理

* **459 只无定期报告**（多为新上市）、**44 条 `REVISION_REVIEW_REQUIRED`**
  （有更正/修订公告，**不猜哪份是当期值**，修订链未实现）；
* **老期间（2025-06-30 / 2025-12-31）全市场绑定未做**：只绑了最新一期；
* **决策日总市值**仍未接通，F10 与排名仍用标注过的占位市值。

---

# 附：池内阶段（904 只）的记录

## 为什么换主数据源

## 为什么这是 C 包的关键缺口

方案 C 包要求"有原始档案证据的有限区间可回放"。消费端
（`admissible_facts` 的 PIT 模式分支）早就实现且有测试，但**全项目从未产出过
一条 `HISTORICAL_RECONSTRUCTED` 事实**——生产端是空的。本阶段补上了它。

## 两步

1. `tools/bind_structured_to_announcements.py`：把每条结构化候选绑到巨潮公告
   索引，产出 `published_date`、`available_at`、`is_correction`/`is_revision`。
2. `tools/ingest_reconstructed_facts.py`：写成 `FinancialFact`
   （`pit_mode=HISTORICAL_RECONSTRUCTED`、`availability_basis=RECONSTRUCTED`），
   默认 dry-run，`--commit` 才写库。

## 时点纪律（不可放宽）

* `available_at` 由**公告日**经 `aquant.domain.fundamentals.pit.available_at()`
  推导，取**下一交易日盘前**——用工作日近似会在长假前后算出错误的可得时点，
  而错误方向恰好是"**提前可用**"。
* 交易日历复用 `verify_calendar_evidence()`：它拿归档原始字节重建整包并逐字
  比对，"裸日期数组"或改过的日历都不接受；日历过期时**本工具失败关闭**，
  正确做法是重跑 `admit_s2_industrial_pdf_review.py calendar` 刷新。
* 期间存在更正/修订公告时标 `REVISION_REVIEW_REQUIRED` 并**交出去**，
  **不猜哪份是当期值**。

## 端到端验证（实测，8 只 × 5 字段 = 40 条）

```
allow_reconstructed=True,  cutoff=2026-09-01   放行 40 / 拒绝 0
allow_reconstructed=False                      放行  0 / 拒绝 40
                                               └ S2_FACT_RECONSTRUCTION_NOT_REQUESTED
allow_reconstructed=True,  cutoff=2026-08-01   放行 25 / 拒绝 15
                                               └ S2_FACT_NOT_YET_AVAILABLE
```

**第三行是关键**：公告在截止日之后的 15 条（3 家 × 5 字段，即 2026 半年报在
8 月下旬才披露的那三家）被**正确隐藏**——证明 PIT 可见性门禁真的在工作，
而不是只要声明了重建依据就一律放行。

契约测试 `tests/unit/test_structured_announcement_binding.py` 锁住两端衔接：
工具写入的 `pit_mode`/`availability_basis` 字符串必须是消费端枚举的名字
（消费端用 `is` 对枚举成员比较，字符串对不上会静默落进 UNKNOWN 被排除）。

## 相关文件（第二阶段）

| 文件 | 作用 |
|---|---|
| `tools/bind_structured_to_announcements.py` | 绑官方公告时点与修订链 |
| `tools/ingest_reconstructed_facts.py` | 写入历史重建事实（默认 dry-run） |
| `tests/unit/test_structured_announcement_binding.py` | 生产端↔消费端契约 |
| `data/s2-structured/bound-facts.json` | 全池绑定产物 |

## 相关文件

| 文件 | 作用 |
|---|---|
| `src/aquant/adapters/providers/eastmoney_structured.py` | 适配器：三表五字段、限速、归档、收据 |
| `tools/collect_structured_financials.py` | 全市场批量采集 |
| `tools/qc_structured_against_pdf.py` | 与官方 PDF 的抽样质检 |
| `data/s2-structured/structured-candidates.json` | 全池产物（2623 条完整） |
| `data/s2-structured/qc-vs-pdf.json` | 质检结论 |
