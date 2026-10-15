import type { WorkspaceData } from "../lib/types";
import { categoryLabel, directionLabel, formatAsOf, verificationLabel } from "../lib/format";
import { Badge, Callout, Card, Empty, Section } from "../components/ui";
import type { PreviewResponse, ResearchSimulationDraft } from "../lib/api";
import { summarizePlan } from "../lib/workflow";

/** 今日：数据状态、重要变化、持仓事件、研究候选、待处理项（§5.2）。
 *  交互原则：先变化后排行；允许"今日无需调整"。
 */
export function TodayView({
  data, onOpenCard, onOpenDraft, onOpenResearch, livePreview = null,
  persistedPlanStatus = null, researchDrafts = [], draftReadError = null, watchIds = [],
}: { data: WorkspaceData; onOpenCard: (id: string) => void; onOpenDraft: () => void;
  onOpenResearch?: () => void; livePreview?: PreviewResponse | null;
  persistedPlanStatus?: "FROZEN" | "EXECUTED" | null;
  researchDrafts?: ResearchSimulationDraft[]; draftReadError?: string | null; watchIds?: string[];
}) {
  const { status, todayChanges, candidates, draft } = data;
  const source = data.dataSource === "api" ? "api" : "fixture";
  const plan = summarizePlan(draft, livePreview, persistedPlanStatus, source);
  const pending = (livePreview?.rule_checks ?? draft.ruleChecks).filter(item => !item.passed);
  const userDrafts = researchDrafts.filter(item => item.source === "USER_SELECTION" && item.portfolioId === draft.portfolioId);
  const outstanding = userDrafts.filter(item => ["WAITING_EXECUTION_SNAPSHOT", "READY_FOR_REVIEW", "BLOCKED", "FROZEN"].includes(item.status));
  const relevant = todayChanges.filter(item => item.subjects.some(id => watchIds.includes(id)));
  const orderedChanges = [...relevant, ...todayChanges.filter(item => !relevant.includes(item))];
  const nothingToDo = plan.computed && plan.checked && plan.orders === 0 && pending.length === 0 &&
    !persistedPlanStatus && outstanding.length === 0 && status.blockingIssues.length === 0;

  return (
    <>
      <div className="page-intro">
        <h1>今天从这里开始</h1>
        <p>先看与你关注的股票有关的变化，再继续研究或处理模拟方案。研究时间：{formatAsOf(status.asOfTime)}。</p>
      </div>
      {status.blockingIssues.length > 0 && (
        <div className="section">
          <Callout tone="warn" title="当前数据有待处理项">
            {status.blockingIssues.map((b, i) => (
              <div key={i} style={{ marginTop: i ? 6 : 0 }}>
                <span className="mono">{b.code}</span> · {b.message}
                <div className="note" style={{ color: "inherit" }}>修复：{b.repair}</div>
              </div>
            ))}
          </Callout>
        </div>
      )}

      <Section title="今日变化与待办" dataSource={source}>
      <div className="grid-2">
        <Card title="今天改变了什么" >
          {todayChanges.length === 0 ? (
            <Empty title="今日没有新的可用变化">
              当前发布资料没有新的变化摘要；这不表示市场没有变化，也不代表已评估组合。
            </Empty>
          ) : (
            <ul className="list">
              {orderedChanges.map((c) => {
                const v = verificationLabel(c.verification);
                const d = directionLabel(c.direction);
                return (
                  <li key={c.eventId}>
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 4 }}>
                      <Badge tone="accent">{categoryLabel(c.category)}</Badge>
                      <Badge tone={v.tone}>{v.text}</Badge>
                      <Badge tone={d.tone}>{d.text}</Badge>
                      {relevant.includes(c) && <Badge tone="accent">关联我的自选</Badge>}
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

        <Card title="接下来做什么">
          <Badge tone={plan.tone}>{plan.label}</Badge>
          <p style={{ margin: "10px 0" }}>{plan.detail}</p>
          <dl className="kv">
            <dt>草稿订单</dt><dd>{plan.computed ? `${plan.orders} 笔` : "尚未计算"}</dd>
            <dt>规则检查</dt>
            <dd>
              {!plan.checked ? <Badge tone="neutral">尚无检查结果</Badge> : pending.length === 0
                ? <Badge tone="ok">全部通过</Badge>
                : <Badge tone="warn">{pending.length} 项未通过</Badge>}
            </dd>
            {outstanding.length > 0 && <><dt>已保存方案</dt><dd>{outstanding.length} 份待继续</dd></>}
          </dl>
          {pending.length > 0 && (
            <div style={{ marginTop: 10 }}>
              <Callout tone="warn" title="以下检查未通过">
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {pending.map((c, index) => <li key={index}>{"name" in c ? c.name : c.check || c.order}</li>)}
                </ul>
              </Callout>
            </div>
          )}
          <div style={{ marginTop: 12 }}>
            <button className="btn btn-primary" onClick={onOpenDraft}>
              {outstanding.length ? "继续已保存方案" : plan.action}
            </button>
            {onOpenResearch && <button className="btn" style={{ marginLeft: 8 }} onClick={onOpenResearch}>研究股票</button>}
          </div>
          {outstanding.map(item => <p className="note" key={item.draftId} style={{ marginTop: 8 }}>
            {item.tradingDay} · {item.status === "WAITING_EXECUTION_SNAPSHOT" ? "等待执行日行情" : item.status === "READY_FOR_REVIEW" ? "已复核，待你确认" : item.status === "FROZEN" ? "已确认，继续查看执行状态" : "资格或配置待补齐"}
          </p>)}
          {draftReadError && <Callout tone="warn" title="保存方案状态暂未读到">{draftReadError}</Callout>}
        </Card>
      </div>
      </Section>

      <Section
        title="可继续研究的股票"
        hint="当前规则快照候选；完整研究名单在研究页"
        dataSource={data.dataSource === "api" ? "api" : "fixture"}
        actions={onOpenResearch && <button className="btn btn-sm" onClick={onOpenResearch}>查看全部研究</button>}
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
                  {candidates.slice(0, 5).map((c) => (
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
        <Callout tone="ok" title="本次预览没有调整订单">
          已计算且已有规则检查。你仍可在模拟组合中明确记录维持不动的决定及理由。
        </Callout>
      )}
    </>
  );
}
