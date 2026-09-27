# S2 官方 PDF 人工复核与单报告准入（本机试点）

本流程只面向 ADR-015 固定的五只普通工商样本和五个公式必需期间。机器抽出的金额、页码和公告标题都是**待核对线索**；填写 `reviewer_id` 表示复核人已实际查看原文并承担该条判断。空白包、预检成功和官方计划开市日都不会开启 S2 策略。

## 已备齐的复核材料

本机忽略目录 `data/s2-pdf-review/` 中的 `cninfo-industrial-review-20260927T050842Z.json` 是 25 份报告的总工作包。`cninfo-intake-600276-<期间>-20260927.json` 已覆盖恒瑞医药五期：2024-06-30、2024-12-31、2025-06-30、2025-12-31、2026-06-30，共有 16 个待核对财务值和 67 条逐期公告处置。五份文件的人工金额、版本签署与公告结论全为空；重复标题也必须针对各报告期给出结论。

复核包的 `report.documentUrl` 指向官方 PDF，`report.fields` 保留机器候选的原格、行、单位和页码。复核人须打开原文，在**合并口径、当期列**核对每个字段，再填写 `human_review.fields` 中的 `reviewed_value_yuan`、`pdf_page`、`current_cell`、`row_label`、`source_amount_unit`、`reviewer_id` 和带时区的 `reviewed_at`。不能确认的值保留空白，由准入程序拒绝，不用零或估算代替。

`human_review.version` 要结合完整定期报告索引和必要的更正原文，说明原版/修订版关系，再由复核人设置 `complete_search_attested=true`。`human_review.cross_category.dispositions` 必须逐条填写 `RELATED` 或 `UNRELATED`、理由、复核人和时间；相关公告还须有对应官方 PDF 的归档收据与报告关系证据。标题筛出候选不等于公告确实更正了财务数据。发现与原报告相关的更正公告时，当前准入桥会拒绝把原报告直接升仓，不能只凭一段理由放行。

## 生成与准入

在仓库根目录运行。所有输出位于本机忽略目录；`prepare` 和 `calendar` 会抓取网页并追加前向归档收据。输出文件必须使用不存在的新名字，命令不会覆盖旧证据。

```powershell
python tools/admit_s2_industrial_pdf_review.py calendar `
  --archive-root deploy/agentctl-q0/forward-archive `
  --output data/s2-pdf-review/calendar-evidence-NEW.json
python tools/admit_s2_industrial_pdf_review.py prepare `
  --worklist data/s2-pdf-review/cninfo-industrial-review-20260927T050842Z.json `
  --candidates data/s2-pdf-review/cninfo-industrial-candidates.json `
  --archive-root deploy/agentctl-q0/forward-archive `
  --stock 600276 --period 2026-06-30 `
  --output data/s2-pdf-review/review-600276-2026H1-NEW.json
```

复核完成后，用新的 `review` 文件和当日 `calendar-evidence` 文件运行同一条 `admit` 命令。**不加 `--commit` 为预检**：它使用临时 SQLite 副本，不创建或修改指定的正式事实库。全部核验通过后才可加 `--commit`，一次写入该报告的固定字段及完整凭证。

```powershell
python tools/admit_s2_industrial_pdf_review.py admit `
  --worklist data/s2-pdf-review/cninfo-industrial-review-20260927T050842Z.json `
  --candidates data/s2-pdf-review/cninfo-industrial-candidates.json `
  --archive-root deploy/agentctl-q0/forward-archive `
  --review data/s2-pdf-review/review-600276-2026H1-NEW.json `
  --calendar-evidence data/s2-pdf-review/calendar-evidence-NEW.json `
  --fact-db data/s2-pdf-review/s2-reviewed-facts.sqlite
```

全分类索引必须截至**准入当日北京时间**。跨日、出现新公告、PDF/候选工作包变更时，重新生成复核包并核对新增或变化部分；不能把刷新收据当成旧复核的签署。日历证据把已观察交易日与交易所公告的 2026 年**计划**交易日分开，不能证明临时停市未发生；最终研究决策仍需有效发布快照。2026 年以外的计划日历尚未接入，失败关闭。
