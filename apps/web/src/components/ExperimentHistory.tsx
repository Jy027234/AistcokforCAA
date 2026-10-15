import { useEffect, useState } from "react";
import { api, AquantApiError, type DecisionRow, type ExperimentRow, type ResearchSimulationDraft } from "../lib/api";
import { formatAsOf, formatCents } from "../lib/format";
import { Badge, Callout, Card, Empty, Section } from "./ui";
import { ComparisonResults } from "./ComparisonResults";
import { EngineeringForwardResults } from "./EngineeringForwardResults";
import { DecisionJourney, OriginalDocument, ProposalSummary } from "./DecisionJourney";

type ReadState<T> = { rows: T[]; error: string | null; loading: boolean };
export type SavedResearchDraft = ResearchSimulationDraft & {
  researchContentHash?: string; researchAvailableAt?: string; decisionCutoffAt?: string;
  initialCashCents?: number; researchProposed?: unknown;
  originalRanking?: { instrumentId: string; position: number; signalRank: number | null }[];
  selection?: { excludedReasons?: Record<string, string> };
};
const readError = (e: unknown) => e instanceof AquantApiError
  ? `${e.message}（${e.envelope.code}）· ${e.envelope.repairAction}` : String(e);
const emptyRead = <T,>(): ReadState<T> => ({ rows: [], error: null, loading: true });
const draftStatus = (value: string) => ({ WAITING_EXECUTION_SNAPSHOT: "等待执行日行情", BLOCKED: "资格或配置待补齐",
  READY_FOR_REVIEW: "已复核，尚需确认", FROZEN: "已确认冻结" }[value] ?? value);

function ReadFailure({ title, error, onRetry }: { title: string; error: string | null; onRetry: () => void }) {
  return error ? <Callout tone="danger" title={title}>{error}<div style={{ marginTop: 8 }}>
    <button className="btn" onClick={onRetry}>重试读取</button></div></Callout> : null;
}

/** Independent API reads: a missing endpoint never falls back to fixture rows. */
export function ExperimentsApiView({ onOpenPortfolio }: { onOpenPortfolio?: () => void } = {}) {
  const [attempt, setAttempt] = useState(0);
  const [experiments, setExperiments] = useState<ReadState<ExperimentRow>>(emptyRead);
  const [decisions, setDecisions] = useState<ReadState<DecisionRow>>(emptyRead);
  const [drafts, setDrafts] = useState<ReadState<SavedResearchDraft>>(emptyRead);
  const refresh = () => setAttempt(n => n + 1);
  useEffect(() => {
    let active = true;
    setExperiments(emptyRead()); setDecisions(emptyRead()); setDrafts(emptyRead());
    void Promise.allSettled([api.experiments(), api.decisions(), api.researchSimulationDrafts()]).then(([e, d, s]) => {
      if (!active) return;
      setExperiments(e.status === "fulfilled" ? { rows: e.value.experiments, error: null, loading: false }
        : { rows: [], error: readError(e.reason), loading: false });
      setDecisions(d.status === "fulfilled" ? { rows: d.value.decisions, error: null, loading: false }
        : { rows: [], error: readError(d.reason), loading: false });
      setDrafts(s.status === "fulfilled" ? { rows: s.value.drafts, error: null, loading: false }
        : { rows: [], error: readError(s.reason), loading: false });
    });
    return () => { active = false; };
  }, [attempt]);
  const personalDrafts = drafts.rows.filter(d => d.source === "USER_SELECTION");
  const otherDrafts = drafts.rows.filter(d => d.source !== "USER_SELECTION");
  return <>
    <Section title="回看我的判断" hint="先看当时依据与选择，再核验模拟结果" dataSource="static" noNumericValue
      actions={<button className="btn" disabled={decisions.loading || drafts.loading} onClick={refresh}>刷新原判断与草稿</button>}>
      <Callout title="从一次决定开始复盘">先回看原理由和方案，确认与模拟执行分别留痕。草稿和决策按原始 ID 分别保留；缺少明确绑定时，不按日期或股票相似猜测关联。</Callout>
    </Section>
    <DecisionHistoryDisplay {...decisions} onRefresh={refresh} onOpenPortfolio={onOpenPortfolio} />
    <ResearchDraftHistoryDisplay {...drafts} rows={personalDrafts} onRefresh={refresh} onOpenPortfolio={onOpenPortfolio} />
    <Section title="对照与工程验证" hint="深入查看规则对照、工程结果与实验登记" dataSource="static" noNumericValue>
      <details className="secondary-details"><summary>展开对照与工程验证</summary>
        <p className="note">工程选择、历史回放与规则对照不代表你完成了一次个人决策；真实行情、历史重建与示例数据沿用各记录的来源声明。</p>
        <EngineeringForwardResults />
        <ComparisonResults />
        <ExperimentRegistrationDisplay {...experiments} onRefresh={refresh} />
        <ResearchDraftHistoryDisplay {...drafts} rows={otherDrafts} onRefresh={refresh} engineering />
      </details>
    </Section>
  </>;
}

export function ExperimentRegistrationDisplay({ rows, error, loading, onRefresh }: ReadState<ExperimentRow> & { onRefresh: () => void }) {
  return <Section title="实验登记" hint="先登记，后看结果；原登记与失败记录保留" dataSource="api"
    actions={<button className="btn" disabled={loading} onClick={onRefresh}>刷新登记与原判断</button>}>
    <ReadFailure title="实验登记读取失败" error={error} onRetry={onRefresh} />
    {loading ? <Card><Empty title="正在读取已保存实验" /></Card> : !error && (rows.length === 0
      ? <Card><Empty title="尚无实验登记">四组合工程回放的冻结运行版本见下方对照结果。</Empty></Card>
      : <div className="grid-2">{rows.map(e => <Card key={e.experiment_id}>
        <h3>{e.hypothesis} <Badge tone="neutral">{e.status}</Badge></h3>
        <dl className="kv"><dt>实验 ID</dt><dd className="mono">{e.experiment_id}</dd>
          <dt>实际登记时间</dt><dd>{formatAsOf(e.registered_at)}</dd>
          <dt>主要评价指标</dt><dd>{e.primary_metric ?? "原登记未提供"}</dd>
          <dt>测试集查看次数</dt><dd>{e.test_set_access_count}</dd>
          <dt>已保存结论</dt><dd>{e.outcome_notes ?? "尚无结论"}</dd></dl>
        <p className="note mono">登记原时刻：{e.registered_at}</p>
      </Card>)}</div>)}
  </Section>;
}

export function DecisionHistoryDisplay({ rows, error, loading, onRefresh, onOpenPortfolio }: ReadState<DecisionRow> & {
  onRefresh: () => void; onOpenPortfolio?: () => void;
}) {
  return <Section title="人工决策复盘" hint="只读已保存判断；原理由、方案和差异不随新研究改写" dataSource="api">
    <ReadFailure title="原判断读取失败" error={error} onRetry={onRefresh} />
    {loading ? <Card><Empty title="正在读取原始决策" /></Card> : !error && (rows.length === 0
      ? <Card><Empty title="尚无已保存的决策记录">可以先保存研究草稿，再到模拟组合复核与确认。保存草稿并不等于确认、成交或完成一次用户决策。
        {onOpenPortfolio && <div style={{ marginTop: 12 }}><button className="btn" onClick={onOpenPortfolio}>前往模拟组合</button></div>}</Empty></Card>
      : rows.map(d => <Card key={d.decision_id}>
        <DecisionJourney decision={d} onOpenPortfolio={onOpenPortfolio} />
      </Card>))}
  </Section>;
}

export function ResearchDraftHistoryDisplay({ rows, error, loading, onRefresh, onOpenPortfolio, engineering = false }: ReadState<SavedResearchDraft> & {
  onRefresh: () => void; onOpenPortfolio?: () => void; engineering?: boolean;
}) {
  return <Section title={engineering ? "工程与其他来源草稿" : "已保存的研究模拟草稿"} hint="固定原候选与选择，后续复核沿用同一来源" dataSource="api">
    <ReadFailure title="草稿原记录读取失败" error={error} onRetry={onRefresh} />
    {loading ? <Card><Empty title="正在读取原草稿" /></Card> : !error && (rows.length === 0
      ? <Card><Empty title={engineering ? "尚无工程与其他来源草稿" : "尚无已保存研究草稿"}>保存研究选择后，可在这里回看当时依据。</Empty></Card>
      : rows.map(d => <Card key={d.draftId}>
        <h3>{d.source === "ENGINEERING_TRIAL" ? "工程试运行草稿" : d.source === "USER_SELECTION" ? "用户保存的选择草稿" : "其他来源草稿"} <Badge tone="neutral">保存时：{draftStatus(d.status)}</Badge></h3>
        {d.source === "ENGINEERING_TRIAL" && <p className="note">工程选择，非用户决定；该草稿不能计入已完成用户决策。</p>}
        <p className="note">草稿 <span className="mono">{d.draftId}</span> · {d.createdAt ? formatAsOf(d.createdAt) : "原记录未提供保存时刻"}</p>
        <div className="decision-journey">
          <div className="decision-journey-step"><h4>当时的研究建议</h4><ProposalSummary value={d.researchProposed} /></div>
          <div className="decision-journey-step"><h4>保存的选择与理由</h4><p>{d.selectionReason || "原记录未提供选择理由"}</p>
            <p>选择证券：<span className="mono">{d.selectedInstrumentIds.join("、") || "原记录未提供选择证券"}</span></p>
            <ProposalSummary value={d.projection} />
            {d.selection?.excludedReasons && Object.keys(d.selection.excludedReasons).length > 0 && <details><summary>未纳入的证券与当时理由</summary>
              <ul className="list">{Object.entries(d.selection.excludedReasons).map(([id, reason]) => <li key={id}>{id}：{reason}</li>)}</ul></details>}
          </div>
          <div className="decision-journey-step"><h4>当前可核验的进度</h4><p>原记录保存时：{draftStatus(d.status)}。</p>
            {d.blockingIssues && d.blockingIssues.length > 0 && <ul className="list">{d.blockingIssues.map((issue, i) => <li key={i}>{issue.message}</li>)}</ul>}
            <p className="note">这里显示不可变的保存记录与估算，尚不能据此认定已成交。最新进度以模拟组合重新复核的结果为准。</p>
            {d.source === "USER_SELECTION" && onOpenPortfolio && <button className="btn" onClick={onOpenPortfolio}>
              {d.status === "WAITING_EXECUTION_SNAPSHOT" ? "到模拟组合检查行情" : d.status === "READY_FOR_REVIEW" ? "到模拟组合继续确认" : "到模拟组合复核草稿"}</button>}
          </div>
        </div>
        <details><summary>草稿资金与账户记录</summary><dl className="kv"><dt>保存时刻</dt><dd>{d.createdAt ? formatAsOf(d.createdAt) : "原记录未提供"}</dd>
          <dt>执行交易日</dt><dd>{d.tradingDay}</dd><dt>账户</dt><dd className="mono">{d.portfolioId}</dd>
          <dt>原选择</dt><dd className="mono">{d.selectedInstrumentIds.join("、")}</dd>
          <dt>保存时计划 ID</dt><dd className="mono">{d.planId ?? "尚未关联冻结计划"}</dd>
          {d.initialCashCents != null && <><dt>原始参考资金</dt><dd>{formatCents(d.initialCashCents)}</dd></>}
          {d.projection && <><dt>原草稿预计费用</dt><dd>{formatCents(d.projection.estimatedFeesCents)}</dd>
            <dt>原草稿预计剩余现金</dt><dd>{formatCents(d.projection.cashAfterCents)}</dd></>}
        </dl></details>
        <OriginalDocument title="展开原研究建议方案" value={d.researchProposed} />
        <OriginalDocument title="展开当时选择后的估算方案（非成交）" value={d.projection} />
        {d.originalRanking && <OriginalDocument title="展开原选择对应的研究排名" value={d.originalRanking.filter(r => d.selectedInstrumentIds.includes(r.instrumentId))} />}
        <details><summary>展开不可变来源与原始时刻</summary><dl className="kv">
          <dt>草稿 ID</dt><dd className="mono">{d.draftId}</dd>
          <dt>研究快照 / 内容哈希</dt><dd className="mono">{d.researchSnapshotId}<br />{d.researchContentHash ?? "原记录未提供"}</dd>
          <dt>决策快照</dt><dd className="mono">{d.decisionSnapshotId}</dd>
          <dt>研究可得时间</dt><dd className="mono">{d.researchAvailableAt ?? "原记录未提供"}</dd>
          <dt>决策截止时刻</dt><dd className="mono">{d.decisionCutoffAt ?? "原记录未提供"}</dd>
          <dt>实际保存时刻</dt><dd className="mono">{d.createdAt ?? "原记录未提供"}</dd>
          <dt>原来源标签</dt><dd className="mono">{d.source}</dd>
        </dl></details>
        <p className="note">草稿与决策的关联需以明确的计划与决策绑定核验，不能按相同证券或日期合并为一次已完成的决策。确认记录与执行账本分别留痕。</p>
      </Card>))}
  </Section>;
}
