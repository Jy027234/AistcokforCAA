import type { WorkspaceData } from "../lib/types";
import { Badge, Callout, Card, Empty, Section } from "../components/ui";
import { decisionTypeLabel, formatDate } from "../lib/format";
import { ExperimentsApiView } from "../components/ExperimentHistory";

/** 复盘：先看个人原判断与模拟进度，深入时查看实验与工程对照。
 * 所有结果保留版本、样本期、数据级别和限制。
 */
export function ExperimentsView({ data, onOpenPortfolio }: { data: WorkspaceData; onOpenPortfolio?: () => void }) {
  if (data.dataSource === "api") return <ExperimentsApiView onOpenPortfolio={onOpenPortfolio} />;
  const source = "fixture";
  return (
    <>
      <Section title="回看我的判断" hint="先看当时依据与选择，再核验模拟结果" dataSource="static" noNumericValue>
        <Callout tone="warn" title="示例复盘">以下个人判断来自随前端分发的示例记录，用于说明复盘方式。真实决策、草稿和执行结果应读取已保存的记录。</Callout>
      </Section>
      <Section title="人工决策复盘" hint="保留原判断，不用新解释覆盖旧理由" dataSource={source}>
        {data.decisionLog.length === 0 ? (
          <Card><Empty title="尚无决策记录">保存研究选择并在模拟组合确认后，可以回看当时的理由与方案。
            {onOpenPortfolio && <div style={{ marginTop: 12 }}><button className="btn" onClick={onOpenPortfolio}>前往模拟组合</button></div>}</Empty></Card>
        ) : data.decisionLog.map(d => (
          <Card key={d.decisionId}>
            <h3>{formatDate(d.date)} · {decisionTypeLabel(d.type)} <Badge tone="warn">示例记录</Badge></h3>
            <div className="decision-journey">
              <div className="decision-journey-step"><h4>当时的规则方案</h4><p>{d.modelProposed}</p></div>
              <div className="decision-journey-step"><h4>我的选择与理由</h4><p>{d.humanFinal}</p><p>{d.reason}</p>
                <Badge tone={d.externalInfoUsed ? "warn" : "neutral"}>{d.externalInfoUsed ? "声明使用外部信息" : "声明未使用外部信息"}</Badge></div>
              <div className="decision-journey-step"><h4>结果证据</h4><p className="note">该示例未提供对应的执行账本与收益记录。</p></div>
            </div>
            <p className="note mono">原记录 ID：{d.decisionId}</p>
          </Card>
        ))}
      </Section>
      <Section title="对照与工程验证" hint="深入查看实验登记与规则对照" dataSource="static" noNumericValue>
        <details className="secondary-details"><summary>展开对照与工程验证</summary>
      <Section title="实验登记" hint="先登记，后看结果" dataSource={source}>
        {data.experiments.length === 0 ? (
          <Card><Empty title="尚无实验登记" /></Card>
        ) : (
          <div className="grid-2">
            {data.experiments.map((e) => (
              <Card key={e.experimentId}>
                <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
                  <h3>{e.hypothesis}</h3>
                  <Badge tone="neutral">{e.status}</Badge>
                </div>
                <dl className="kv">
                  <dt>实验 ID</dt><dd className="mono">{e.experimentId}</dd>
                  <dt>策略版本</dt><dd className="mono">{e.strategyVersion}</dd>
                  <dt>样本期</dt><dd>{e.sampleWindow}</dd>
                  <dt>数据级别</dt>
                  <dd><Badge tone={e.dataLevel.includes("虚构") ? "warn" : "neutral"}>{e.dataLevel}</Badge></dd>
                </dl>
                <div style={{ marginTop: 10 }}>
                  <h4 style={{ marginBottom: 4 }}>不足与限制</h4>
                  <ul className="list">
                    {e.limitations.map((l, i) => <li key={i} className="note">{l}</li>)}
                  </ul>
                </div>
              </Card>
            ))}
          </div>
        )}
      </Section>

      <Section title="对照结果" dataSource="static">
        <Card>
          <Callout tone="warn" title="尚无对照结果">
            M/E/H/B 四类对照需要先完成前向模拟记录。当前没有已保存的对照结果，
            因此这里不展示任何收益率数字。
          </Callout>
        </Card>
      </Section>

        </details>
      </Section>
    </>
  );
}
