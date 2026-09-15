# 数据权利登记表

**性质：** 本表是**声明**，不是法律意见。
**日期：** 2026-09-15 · **依据：** §17.2（未知默认不开放）、§826
**代码：** `src/aquant/domain/data/rights.py`、`src/aquant/domain/ai/egress.py`
**强制：** `tests/security/test_model_egress_gate.py`

---

## 一、六个字段各管什么

| 字段 | 管的事 | 风险层级 |
|---|---|---|
| `research_use` | 能不能拿它做研究 | 低 |
| `local_storage` | 能不能存进本地库（含归档原文） | 低 |
| `excerpt_display` | 能不能把原文片段展示给使用者 | 中 |
| `commercial_use` | 能不能用于商用 | 高 |
| `third_party_redistribution` | 能不能再分发出去 | 高 |
| **`model_processing`** | **能不能送进模型上下文** | **最高** |

**为什么把 `model_processing` 单独隔离**：把抓来的数据发给模型厂商，
在法律上是**对外披露**，与其他五项不是一个量级；而且它是唯一
"一旦做了就无法收回"的一项——数据已经在第三方手里了。

---

## 二、确认方法（可复核，不靠记忆）

条款无法用程序读取，因此本表记录**判定依据 + 可否复核**。
修改任何一项都必须同时写明依据，否则视为未确认。

| 来源 | 上游性质 | 依据类型 |
|---|---|---|
| `baostock` | 交易所披露数据（上市公司公告的行情与财务） | 公开披露信息 |
| `cninfo` | 巨潮资讯网，证监会指定披露平台 | 公开披露信息 |
| `sse-site` / `szse-site` | 交易所官网 | 公开披露信息 |
| `tencent-ifzq` / `tencent-qt` / `sina-hq` | 消费级行情网站 | 未声明 |
| `eastmoney-direct` | 消费级行情网站 | 未声明 |

---

## 三、当前判定

| 来源 | research | storage | excerpt | model | redistrib | commercial |
|---|---|---|---|---|---|---|
| `baostock` | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| `cninfo` | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| `sse-site` / `szse-site` | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| `tencent-*` / `sina-hq` / `eastmoney-direct` | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |

### 判定理由（三类来源区别对待）

1. **公开披露信息源**（BaoStock、巨潮、交易所官网）
   数据本身是**法定公开披露**的内容，研究使用与本地保存的空间明显更大，
   因此 `research_use` / `local_storage` / `excerpt_display` 记为允许。
   但**再分发与商用不放开**：交易所对行情数据另有商业授权安排，
   不能因为原始信息是公开的就认为可以转售。

2. **消费级行情网站**（腾讯、新浪、东财）
   这些是**企业的服务条款**约束其接口，而不是公开披露制度。
   因此连 `excerpt_display` 都不标记为允许——展示片段涉及转载。
   研究使用与本地保存记为允许，理由是这属于个人研究范畴，
   但**这一条最需要使用者本人核对其条款**。

3. **`model_processing` 一律 ❌（全部来源）**
   这是唯一的硬约束：**在任何条款被书面确认之前，没有任何数据
   可以进入模型上下文**。它使得"AI 事件研究"在条款确认前无法进行——
   这是刻意的结果，不是缺陷（ADR-003 的范围收窄）。

### ❌ 不等于 "PROHIBITED"

当前值全部是 `UNKNOWN`（代码中），表中的 ❌ 表示**不开放**。
程序中 `UNKNOWN` 与 `PROHIBITED` 都返回 False，但含义不同：

* `PROHIBITED` = 已确认不允许，不应再尝试；
* `UNKNOWN` = 未确认，**可以**通过补充依据改为允许。

区分它们是必要的：否则"没查过"和"查过说不行"会混成一件事，
将来谁也不知道哪些还能争取。

---

## 四、如何放开某一项

放开 `model_processing` 需要：

1. 在该来源的条款或书面授权里找到允许"发送至第三方处理"的依据；
2. 在 `src/aquant/domain/data/rights.py` 里把对应来源的
   `model_processing` 改为 `ALLOWED`，并在本表登记依据与日期；
3. 跑 `tests/security/test_model_egress_gate.py` 确认闸门行为随之改变
   （该测试会断言"未授权来源仍被拒绝"，因此改错会失败）。

代码里 `ALLOWED` 是**唯一**能让 `can_enter_model_context()` 返回真的值。
