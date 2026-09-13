# 资料包与工程基础（T3）

**状态：已完成并自检通过**
**日期：2026-09-13**

> 主文档 §0 声明资料包应含 8 项交付物，实际**全部缺失**（见 `docs/integration/q0-readiness-report.md` §6）。
> 用户决定：**从零设计**。本目录即该决定的结果，取代不存在的那份资料包。

---

## 交付内容

| 路径 | 作用 | 对应主文档 |
|---|---|---|
| `contracts/common.schema.json` | 公共定义：PIT 时间字段、枚举、金额/股数、12 个错误码 | §7.1 §7.2 §15.2 §16.4 |
| `contracts/snapshot.schema.json` | 快照契约：不可变、`supersedes`、数据集哈希、证券池 | §8.3 §15.4 |
| `contracts/event.schema.json` | 事件与证据：引用可定位、时点字段、反证、`llm_retrospective_risk` | §9.1 §9.2 §15.3 |
| `contracts/ledger.schema.json` | 模拟账本：组合/订单/成交/批次/现金/应收/估值与日终不变量 | §12 §15.1 |
| `schema/001_metadata.sql` | SQLite 元数据与账本基础结构（事务性对象与引用索引） | §15.1 |
| `configs/research.example.yaml` | 首期研究参数、规则版本、费用（合成）、日程、助手白名单 | §11.2 §12 §8.1 §16.3 |
| `examples/snap-syn-001.yaml` | 合成数据集，覆盖 D01–D08 与 S01–S10 的判定分支 | §15.4、v0.2.2 §4.3 |
| `tests/validate_spec.py` | 资料包自检（**零第三方依赖、不需网络、不需模型密钥**） | §0 |
| `src/aquant/domain/data/pit.py` | PIT 语义：可用时点、保守顺延、模式一致性、可回测性 | §7.1 §7.2 §7.3 |
| `src/aquant/domain/data/instruments.py` | 证券身份与历史状态版本（更名不产生新证券） | §15.2 |
| `tests/pit/test_d01_d08_pit_golden.py` | **D01–D08 黄金用例，17 个测试全过** | §18.1 |

## 自检

```powershell
python tests/validate_spec.py          # 189 项检查
pytest tests/pit -q                    # 17 个 PIT 黄金用例（需 PYTHONPATH=src）
```

`validate_spec.py` 在**裸 Python 3.14（无第三方包）**下也全过——这是主文档 §18.4
"没有模型密钥也应能运行合成测试"的直接落实。

## 设计取舍（记录下来以免反复讨论）

1. **金额一律整数分，禁止 REAL**。§12.6 要求不得依赖二进制浮点保持现金平衡。
   合同层用定点十进制字符串，存储层用 INTEGER 分。
2. **时间一律带时区 UTC 文本**，比较用字典序。§7.1 禁止朴素时间。
3. **`valid_from`/`valid_to` 左闭右开**（§7.1 明文）。因此"某名称适用到 2024-06-30（含）"
   必须存为 `valid_to = 2024-07-01`；这一条在 D05 测试中被固化，避免边界歧义。
4. **用数据库约束表达不变量**，而不是只写在文档里：
   已发布快照不可 UPDATE/DELETE、已入账成交不可 DELETE、
   同一成交同一费用码不可重复计费、不变量未全过时禁止发布净值、冻结计划不可退回草稿。
   这些都有对应的触发器测试。
5. **合成数据带强制水印与免责声明**，并在自检中断言其存在（§15.4）。

## 已知未完成

- 契约只做了**结构与 `$ref` 可解析性**校验，尚未用 jsonschema 对实例做完整校验
  （需要 `jsonschema` 包；已列入 `[dev]` 可选依赖）。
- 事件/账本的领域实现（M1/M2）尚未开始，当前只有契约、SQL 与 PIT 语义。
- 未接入任何真实数据源；`configs` 中的 `eastmoney-direct` 仅为候选登记。
