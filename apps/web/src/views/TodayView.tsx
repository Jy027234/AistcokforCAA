import type { WorkspaceData } from "../lib/types";
import { categoryLabel, directionLabel, formatAsOf, verificationLabel } from "../lib/format";
import { Badge, Callout, Card, Empty, Section } from "../components/ui";

/** 今日：数据状态、重要变化、持仓事件、研究候选、待处理项（§5.2）。
 *  交互原则：先变化后排行；允许"今日无需调整"。
 */
export function TodayView({
  data, onOpenCard, onOpenDraft,
}: { data: WorkspaceData; onOpenCard: (id: string) => void; onOpenDraft: () => void }) {
  const { status, todayChanges, candidates, draft } = data;
  const pending = draft.ruleChecks.filter((c) => !c.passed);
  const nothingToDo = todayChanges.length === 0 && pending.length === 0;

  return (
    <>
      {status.blockingIssues.length > 0 && (
        <div className="section">
          <Callout tone="danger" title="数据不完整，已阻断正式研究">
            {status.blockingIssues.map((b, i) => (
              <div key={i} style={{ marginTop: i ? 6 : 0 }}>
                <span className="mono">{b.code}</span> · {b.message}
                <div className="note" style={{ color: "inherit" }}>修复：{b.repair}</div>
              </div>
            ))}
          </Callout>
        </div>
      )}

      <div className="grid-2 section">
        <Card title="今天改变了什么" >
          {todayChanges.length === 0 ? (
            <Empty title="今日没有新的可用变化">
              没有新证据时系统不会生成变化摘要。
            </Empty>
          ) : (
            <ul className="list">
              {todayChanges.map((c) => {
                const v = verificationLabel(c.verification);
                const d = directionLabel(c.direction);
                return (
                  <li key={c.eventId}>
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 4 }}>
                      <Badge tone="accent">{categoryLabel(c.category)}</Badge>
                      <Badge tone={v.tone}>{v.text}</Badge>
                      <Badge tone={d.tone}>{d.text}</Badge>
                    </div>
                    <div>{c.summary}</div>
                    <div className="note">
                      可用时点 {c.availableAt ? formatAsOf(c.availableAt) : "—"}
                      {c.subjects.length > 0 && " · 关联 " + c.subjects.join("、")}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </Card>

        <Card title="我的组合需要关注什么">
          <dl className="kv">
            <dt>草稿订单</dt><dd>{draft.orders.length} 笔</dd>
            <dt>预计费用</dt><dd>{draft.estimatedFees}</dd>
            <dt>规则检查</dt>
            <dd>
              {pending.length === 0
                ? <Badge tone="ok">全部通过</Badge>
                : <Badge tone="warn">{pending.length} 项未通过</Badge>}
            </dd>
            <dt>计划状态</dt><dd><Badge tone="neutral">{draft.frozenLabel}</Badge></dd>
          </dl>
          {pending.length > 0 && (
            <div style={{ marginTop: 10 }}>
              <Callout tone="warn" title="以下检查未通过">
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {pending.map((c) => <li key={c.name}>{c.name}</li>)}
                </ul>
              </Callout>
            </div>
          )}
          <div style={{ marginTop: 12 }}>
            <button className="btn btn-sm btn-primary" onClick={onOpenDraft} disabled={draft.orders.length === 0}>
              查看模拟草稿
            </button>
          </div>
        </Card>
      </div>

      <Section
        title="研究候选"
        hint="先变化后排行"
        dataSource="fixture"
        actions={<span className="note">全部数值绑定同一快照 {status.snapshotId}</span>}
      >
        <Card padded={false}>
          {candidates.length === 0 ? (
            <Empty title="当前没有候选">
              候选为空是正常状态，不表示系统异常。
            </Empty>
          ) : (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>名称</th>
                    <th className="num">信号排名</th>
                    <th>依据</th>
                    <th>反证 / 风险</th>
                    <th>数据质量</th>
                    <th>是否可模拟</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {candidates.map((c) => (
                    <tr key={c.instrumentId}>
                      <td>
                        <div style={{ fontWeight: 550 }}>{c.displayName}</div>
                        <div className="note mono">{c.instrumentId}</div>
                      </td>
                      <td className="num mono">{c.signalRank.toFixed(2)}</td>
                      <td>{c.basis}</td>
                      <td className="note">{c.counterEvidence}</td>
                      <td><Badge tone="neutral">{c.dataQuality}</Badge></td>
                      <td>
                        {c.simulatable
                          ? <Badge tone="ok">{c.simulatableLabel}</Badge>
                          : <Badge tone="warn">{c.simulatableLabel}</Badge>}
                      </td>
                      <td>
                        <button className="btn btn-sm" onClick={() => onOpenCard(c.instrumentId)}>
                          研究卡
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </Section>

      {nothingToDo && (
        <Callout tone="ok" title="今日无需调整">
          没有新的可用证据，也没有未通过的规则检查。保持观察是有效结论。
        </Callout>
      )}
    </>
  );
}
