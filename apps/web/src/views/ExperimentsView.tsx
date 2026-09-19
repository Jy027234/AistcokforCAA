import type { WorkspaceData } from "../lib/types";
import { Badge, Callout, Card, Empty, Section } from "../components/ui";
import { decisionTypeLabel, formatDate } from "../lib/format";

/** 实验：历史回放、实验登记、对照结果、人工决策复盘（§5.2）。
 *  交互原则：所有结果显示版本、样本期、数据级别和不足。
 */
export function ExperimentsView({ data }: { data: WorkspaceData }) {
  const source = data.dataSource === "api" ? "api" : "fixture";
  return (
    <>
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

      <Section title="人工决策复盘" hint="保留原判断，不用新解释覆盖旧理由"
        dataSource={source}>
        {data.decisionLog.length === 0 ? (
          <Card><Empty title="尚无决策记录" /></Card>
        ) : (
          <Card padded={false}>
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>日期</th><th>决定</th><th>理由</th>
                    <th>模型原方案</th><th>人工最终</th><th>外部信息</th>
                  </tr>
                </thead>
                <tbody>
                  {data.decisionLog.map((d) => (
                    <tr key={d.decisionId}>
                      <td className="mono">{formatDate(d.date)}</td>
                      <td><Badge tone="neutral">{decisionTypeLabel(d.type)}</Badge></td>
                      <td>{d.reason}</td>
                      <td className="note">{d.modelProposed}</td>
                      <td>{d.humanFinal}</td>
                      <td>
                        {d.externalInfoUsed
                          ? <Badge tone="warn">使用了外部信息</Badge>
                          : <span className="note">否</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}
      </Section>
    </>
  );
}
