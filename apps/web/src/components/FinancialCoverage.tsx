import { useEffect, useState, useSyncExternalStore } from "react";
import { api, AquantApiError } from "../lib/api";
import type {
  FinancialCoverageInputBinding, FinancialCoverageJobResult, FinancialCoverageJobState,
  FinancialCoverageJobSubmission, FinancialCoverageReport,
} from "../lib/financialCoverage";
import { formatAsOf } from "../lib/format";
import { Badge, Callout, Card, Section } from "./ui";

const metricLabels: Record<string, string> = { revenue: "营业收入", net_profit_attributable: "归母净利润",
  net_profit_consolidated: "合并净利润", operating_cashflow: "经营现金流", parent_equity: "归母权益" };
const statusLabels: Record<string, string> = { ACCEPTED: "已核验可用", FACT_MISSING: "缺少正式事实",
  NEXT_CUTOFF_PENDING: "已采集，待后续截止", FACT_AMBIGUOUS: "金额版本有歧义",
  REVIEW_MISSING: "缺少原文核验", VERSION_CHAIN_BLOCKED: "修订链待核验" };

type Phase = "idle" | "submitting" | "polling" | "poll-error" | "retrying" | "retry-error"
  | "submission-error" | "succeeded" | "failed";
interface UiError { code: string; message: string; repairAction: string }
interface VerifiedCoverage { jobId: string; result: FinancialCoverageJobResult; isCurrent: boolean | null }
interface CoverageSession {
  phase: Phase;
  submission: FinancialCoverageJobSubmission | null;
  job: FinancialCoverageJobState | null;
  verified: VerifiedCoverage | null;
  startedAtMs: number | null;
  lastStatusReadAtMs: number | null;
  error: UiError | null;
  checkingCurrent: boolean;
}
const initialSession = (): CoverageSession => ({ phase: "idle", submission: null, job: null,
  verified: null, startedAtMs: null, lastStatusReadAtMs: null, error: null, checkingCurrent: false });
let coverageSession = initialSession();
const coverageListeners = new Set<() => void>();
let shouldPollCoverage = false;
let coveragePollController: AbortController | null = null;
let coveragePollDelay: ReturnType<typeof setTimeout> | null = null;
let coverageSubmitPromise: Promise<void> | null = null;
let coverageRetryPromise: Promise<void> | null = null;
const subscribeCoverage = (listener: () => void) => {
  coverageListeners.add(listener);
  return () => coverageListeners.delete(listener);
};
const getCoverageSession = () => coverageSession;
function updateCoverageSession(patch: Partial<CoverageSession>) {
  coverageSession = { ...coverageSession, ...patch };
  coverageListeners.forEach(listener => listener());
}
function apiError(error: unknown): UiError {
  return error instanceof AquantApiError
    ? { code: error.envelope.code, message: error.envelope.message, repairAction: error.envelope.repairAction }
    : { code: "CLIENT_ERROR", message: error instanceof Error ? error.message : String(error), repairAction: "检查连接后重试" };
}
function sameBinding(a: FinancialCoverageInputBinding, b: FinancialCoverageInputBinding): boolean {
  const keys: (keyof FinancialCoverageInputBinding)[] = ["inputHash", "snapshotId", "snapshotCutoffAt", "tradingDay",
    "researchSnapshotId", "researchContentHash", "liveFactsVersion", "proofVersion", "reconstructedFactsVersion",
    "targetMapHash", "priorityInstrumentIdsHash", "codeVersion"];
  return keys.every(key => a[key] === b[key]);
}
function activeStatus(status: string | undefined): boolean { return status === "PENDING" || status === "RUNNING"; }
function serverJobStartMs(createdAt: string | undefined): number | null {
  if (!createdAt || !/(?:Z|[+-]\d{2}:\d{2})$/i.test(createdAt)) return null;
  const timestamp = Date.parse(createdAt);
  return Number.isFinite(timestamp) ? timestamp : null;
}
function waitForPoll(signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.resolve();
  return new Promise(resolve => {
    const finish = () => {
      if (coveragePollDelay !== null) clearTimeout(coveragePollDelay);
      coveragePollDelay = null;
      signal.removeEventListener("abort", finish);
      resolve();
    };
    coveragePollDelay = setTimeout(finish, 2500);
    signal.addEventListener("abort", finish, { once: true });
  });
}
function stopCoveragePolling() {
  shouldPollCoverage = false;
  if (coveragePollDelay !== null) clearTimeout(coveragePollDelay);
  coveragePollDelay = null;
  const controller = coveragePollController;
  coveragePollController = null;
  controller?.abort();
}
function startCoveragePolling() {
  shouldPollCoverage = true;
  if (coveragePollController) return;
  const jobId = coverageSession.submission?.jobId ?? coverageSession.job?.jobId;
  if (!jobId) return;
  const controller = new AbortController();
  coveragePollController = controller;
  void (async () => {
    try {
      while (!controller.signal.aborted && shouldPollCoverage) {
        let job: FinancialCoverageJobState;
        try {
          job = await api.getFinancialCoverageJob(jobId, controller.signal);
        } catch (cause) {
          if (controller.signal.aborted) return;
          const error = apiError(cause);
          const verified = coverageSession.verified;
          updateCoverageSession({ phase: "poll-error", error, checkingCurrent: false,
            verified: verified ? { ...verified, isCurrent: null } : null });
          return;
        }
        if (job.jobId !== jobId || job.jobType !== "FINANCIAL_CHECK") {
          updateCoverageSession({ phase: "poll-error", checkingCurrent: false, lastStatusReadAtMs: Date.now(),
            error: { code: "FINANCIAL_CHECK_JOB_MISMATCH", message: "返回的作业与本次财务核验不匹配。",
              repairAction: "重新读取作业状态；不要用其他作业结果替代。" } });
          return;
        }
        const startedAtMs = serverJobStartMs(job.createdAt) ?? coverageSession.startedAtMs;
        if (job.status === "SUCCEEDED") {
          if (!job.result || job.result.binding.snapshotId !== job.snapshotId ||
              !sameBinding(job.inputBinding, job.result.binding) || job.result.report.snapshotId !== job.snapshotId) {
            updateCoverageSession({ job, startedAtMs, phase: "poll-error", checkingCurrent: false, lastStatusReadAtMs: Date.now(),
              error: { code: "FINANCIAL_CHECK_BINDING_MISMATCH", message: "核验结果与作业输入版本不一致，未显示为当前结果。",
                repairAction: "重新读取作业状态；若问题持续，请检查服务端作业记录。" } });
            return;
          }
          updateCoverageSession({ job, startedAtMs, phase: "succeeded", error: null, checkingCurrent: false, lastStatusReadAtMs: Date.now(),
            verified: { jobId: job.jobId, result: job.result, isCurrent: job.isCurrent } });
          return;
        }
        if (job.status === "FAILED" || job.status === "BLOCKED") {
          updateCoverageSession({ job, startedAtMs, phase: "failed", error: null, checkingCurrent: false, lastStatusReadAtMs: Date.now() });
          return;
        }
        if (job.status !== "PENDING" && job.status !== "RUNNING") {
          updateCoverageSession({ job, startedAtMs, phase: "poll-error", checkingCurrent: false, lastStatusReadAtMs: Date.now(),
            error: { code: "FINANCIAL_CHECK_UNKNOWN_STATUS", message: `财务核验返回未识别状态：${job.status}。`,
              repairAction: "重新读取作业状态。" } });
          return;
        }
        updateCoverageSession({ job, startedAtMs, phase: "polling", error: null, checkingCurrent: false, lastStatusReadAtMs: Date.now() });
        await waitForPoll(controller.signal);
      }
    } finally {
      if (coveragePollController === controller) coveragePollController = null;
    }
  })();
}
function submitCoverageJob() {
  if (coverageSubmitPromise) return coverageSubmitPromise;
  updateCoverageSession({ phase: "submitting", submission: null, job: null, startedAtMs: Date.now(),
    error: null, checkingCurrent: false });
  coverageSubmitPromise = (async () => {
    try {
      const submission = await api.submitFinancialCoverageJob();
      if (!submission.jobId || submission.jobType !== "FINANCIAL_CHECK") throw new Error("财务核验提交响应缺少有效作业标识。");
      updateCoverageSession({ phase: "polling", submission, job: null, error: null, checkingCurrent: false });
      if (shouldPollCoverage) startCoveragePolling();
    } catch (cause) {
      updateCoverageSession({ phase: "submission-error", error: apiError(cause), checkingCurrent: false });
    } finally {
      coverageSubmitPromise = null;
    }
  })();
  return coverageSubmitPromise;
}
function retryCoverageJob() {
  if (coverageRetryPromise) return coverageRetryPromise;
  const jobId = coverageSession.job?.status === "FAILED" ? coverageSession.job.jobId : null;
  if (!jobId) return Promise.resolve();
  updateCoverageSession({ phase: "retrying", startedAtMs: Date.now(), error: null, checkingCurrent: false });
  coverageRetryPromise = (async () => {
    try {
      const submission = await api.retryFinancialCoverageJob(jobId);
      if (!submission.jobId || submission.jobType !== "FINANCIAL_CHECK") throw new Error("财务核验重试响应缺少有效作业标识。");
      updateCoverageSession({ phase: "polling", submission, job: null, error: null, checkingCurrent: false });
      if (shouldPollCoverage) startCoveragePolling();
    } catch (cause) {
      updateCoverageSession({ phase: "retry-error", error: apiError(cause), checkingCurrent: false });
    } finally {
      coverageRetryPromise = null;
    }
  })();
  return coverageRetryPromise;
}
function resumeCoveragePolling() {
  updateCoverageSession({ phase: "polling", error: null,
    checkingCurrent: coverageSession.job?.status === "SUCCEEDED" });
  startCoveragePolling();
}
function attachCoverageSession() {
  shouldPollCoverage = true;
  if (coverageSession.phase === "idle") void submitCoverageJob();
  else if (coverageSession.job || coverageSession.submission) {
    const status = coverageSession.job?.status ?? coverageSession.submission?.status;
    if (activeStatus(status) || status === "SUCCEEDED") {
      updateCoverageSession({ checkingCurrent: status === "SUCCEEDED" });
      startCoveragePolling();
    }
  }
  return stopCoveragePolling;
}
function elapsedText(milliseconds: number): string {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}
function BindingDetails({ binding, resultHash }: { binding: FinancialCoverageInputBinding; resultHash?: string }) {
  return <details style={{ marginTop: 8 }}><summary>查看服务器返回的输入版本</summary>
    <dl className="kv" style={{ marginTop: 8 }}><dt>输入摘要</dt><dd className="mono">{binding.inputHash}</dd>
      <dt>快照 / 研究快照</dt><dd className="mono">{binding.snapshotId} / {binding.researchSnapshotId}</dd>
      <dt>研究内容哈希</dt><dd className="mono">{binding.researchContentHash}</dd>
      <dt>输入截止 / 交易日</dt><dd>{formatAsOf(binding.snapshotCutoffAt)} / {binding.tradingDay}</dd>
      <dt>事实 / 证明版本</dt><dd className="mono">{binding.liveFactsVersion} / {binding.proofVersion}</dd>
      <dt>历史重建版本</dt><dd className="mono">{binding.reconstructedFactsVersion}</dd>
      <dt>目标映射 / 优先证券摘要</dt><dd className="mono">{binding.targetMapHash} / {binding.priorityInstrumentIdsHash}</dd>
      <dt>代码版本</dt><dd className="mono">{binding.codeVersion}</dd>
      {resultHash && <><dt>结果哈希</dt><dd className="mono">{resultHash}</dd></>}
    </dl>
  </details>;
}

export function FinancialCoverage() {
  const session = useSyncExternalStore(subscribeCoverage, getCoverageSession, getCoverageSession);
  const [now, setNow] = useState(() => Date.now());
  const status = session.job?.status ?? session.submission?.status;
  const inputBinding = session.job?.inputBinding ?? session.submission?.inputBinding;
  const elapsedActive = session.phase === "submitting" || session.phase === "retrying" ||
    (activeStatus(status) && ["polling", "poll-error"].includes(session.phase));
  useEffect(() => attachCoverageSession(), []);
  useEffect(() => {
    if (!elapsedActive) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [elapsedActive]);
  const retryAllowed = session.job?.status === "FAILED" && session.phase !== "retrying";
  const retrying = session.phase === "retrying";
  const retryInputChanged = session.error?.code === "FINANCIAL_CHECK_INPUT_CHANGED";
  const statusRefreshAllowed = session.phase === "poll-error" && !!(session.job || session.submission);
  const needsInitialRetry = session.phase === "submission-error";
  const report = session.job?.result?.report ?? session.verified?.result.report ?? null;
  const verified = session.verified;
  const reportIsCurrentJob = !!verified && !!(session.job?.jobId ?? session.submission?.jobId) &&
    verified.jobId === (session.job?.jobId ?? session.submission?.jobId);
  const showElapsed = session.startedAtMs !== null && elapsedActive;
  const stage = session.checkingCurrent ? "正在重新确认输入版本"
    : session.phase === "submitting" ? "正在登记财务核验作业"
    : session.phase === "retrying" ? "正在提交失败作业的重试"
      : session.phase === "poll-error" && activeStatus(status) ? "状态暂不可读，作业可能仍在执行"
        : status === "PENDING" ? "已排队，等待执行"
          : status === "RUNNING" ? "正在核验财务输入"
            : status === "SUCCEEDED" ? (session.phase === "succeeded" ? "核验完成"
              : session.phase === "poll-error" ? "核验已完成，结果暂不可读"
                : "核验已完成，正在读取结果")
              : status === "FAILED" ? "核验失败"
                : status === "BLOCKED" ? "核验受阻" : "等待提交";
  return <>
    <Section title="财务覆盖与补齐进度" hint="按模拟池逐股核对指标、期间和可用时间" dataSource="api">
      <Card>
        <p role="status" aria-live="polite"><strong>{stage}</strong>
          {showElapsed && <span className="note"> · 已用 {elapsedText(now - (session.startedAtMs ?? now))}</span>}
        </p>
        {session.submission && <p className="note" style={{ marginTop: 4 }}>作业 {session.submission.jobId} · 服务端受理 {formatAsOf(session.submission.submittedAt)}
          {session.submission.retryOf && ` · 重试自 ${session.submission.retryOf}`}
          {!session.submission.created && " · 已复用同输入作业"}</p>}
        {session.job?.heartbeatAt && activeStatus(session.job.status) &&
          <p className="note">服务端最近心跳 {formatAsOf(session.job.heartbeatAt)}；没有百分比进度口径。</p>}
        {session.lastStatusReadAtMs !== null && <p className="note">作业状态最近读取于 {formatAsOf(new Date(session.lastStatusReadAtMs).toISOString())}。</p>}
        {inputBinding && <BindingDetails binding={inputBinding} />}
        {session.error && <Callout tone="danger" title={retryInputChanged ? "核验输入版本已变化" : "财务核验操作未完成"}>
          <p>{session.error.message}</p>{session.error.repairAction && <p className="note">恢复建议：{session.error.repairAction}</p>}
        </Callout>}
        {session.job?.error && <Callout tone="danger" title={session.job.status === "BLOCKED" ? "核验受到阻塞" : "财务核验失败"}>
          <p>{session.job.error.message}</p>{session.job.error.repairAction && <p className="note">恢复建议：{session.job.error.repairAction}</p>}
        </Callout>}
        {session.phase === "failed" && session.job?.status === "FAILED" && <button className="btn btn-primary"
          disabled={retrying} onClick={() => void retryCoverageJob()}>重试失败作业</button>}
        {retryAllowed && session.phase === "retry-error" && !retryInputChanged && <button className="btn btn-primary"
          disabled={retrying} onClick={() => void retryCoverageJob()}>再次提交重试</button>}
        {retryInputChanged && <button className="btn btn-primary" onClick={() => void submitCoverageJob()}>核验最新输入版本</button>}
        {needsInitialRetry && <button className="btn btn-primary" onClick={() => void submitCoverageJob()}>重新提交核验</button>}
        {statusRefreshAllowed && <button className="btn" onClick={resumeCoveragePolling}>继续读取作业状态</button>}
        {session.job?.status === "SUCCEEDED" && session.phase === "succeeded" && <button className="btn"
          style={{ marginLeft: 8 }} onClick={resumeCoveragePolling}>重新读取版本状态</button>}
        {verified && verified.isCurrent === false && !session.checkingCurrent && <button className="btn" style={{ marginLeft: 8 }}
          disabled={elapsedActive} onClick={() => void submitCoverageJob()}>核验当前输入版本</button>}
        {verified && verified.isCurrent === true && reportIsCurrentJob && session.phase === "succeeded" && !session.checkingCurrent &&
          <p className="note" style={{ marginTop: 6 }}>最近一次状态读取时，服务端确认本次核验输入与当前版本一致；此结论不开放正式 S2。</p>}
      </Card>
    </Section>
    {report && verified && <FinancialCoverageDisplay report={report} result={verified.result}
      isCurrent={reportIsCurrentJob && session.phase === "succeeded" ? verified.isCurrent : null}
      isPrevious={!reportIsCurrentJob} checkingCurrent={session.checkingCurrent} />}
  </>;
}

export function FinancialCoverageDisplay({ report, result, isCurrent, isPrevious, checkingCurrent }: {
  report: FinancialCoverageReport; result: FinancialCoverageJobResult; isCurrent: boolean | null;
  isPrevious: boolean; checkingCurrent: boolean;
}) {
  const binding = result.binding;
  return <Section title="财务核验报告" hint="仅展示作业绑定的输入版本，不作为正式策略准入" dataSource="api">
    {isPrevious && <Callout tone="info" title="最近一次成功核验记录">新作业完成前，这份旧报告不代表当前作业的输入版本。</Callout>}
    {checkingCurrent && <Callout tone="info" title="正在确认输入版本">报告暂按历史核验记录展示，服务端尚未重新确认当前性。</Callout>}
    {!checkingCurrent && !isPrevious && isCurrent === false &&
      <Callout tone="warn" title="输入版本已变化">以下报告保留为历史核验记录，不代表当前财务输入或任何准入状态。</Callout>}
    {!checkingCurrent && !isPrevious && isCurrent === null &&
      <Callout tone="warn" title="当前版本尚未确认">以下报告是已核验记录；当前性未知，不能作为当前输入状态。</Callout>}
    <Card>
      <p className="note">核验于 {formatAsOf(result.verifiedAt)}；核验快照 {binding.snapshotId}；输入截止 {formatAsOf(report.snapshotCutoffAt)}。
        公式需求基准为 {report.targetPeriodEnd}，逐券报告期分别列明，新补采保留实际可用时间。</p>
      <dl className="kv">
        <dt>模拟池可选财务记录</dt><dd>{report.s1Optional.statementSecurityCount} / {report.s1Optional.poolCount} 只</dd>
        <dt>可计算归母利润 TTM</dt><dd>{report.s1Optional.ttmSecurityCount} / {report.s1Optional.poolCount} 只</dd>
        <dt>本次截止完整正式财务</dt><dd>{report.formalS2.completeFactSecurityCount} / {report.s1Optional.poolCount} 只；
          {report.formalS2.acceptedMetricPeriodCount} / {report.formalS2.requiredMetricPeriodCount} 项指标与期间</dd>
        <dt>正式四因子可用证券</dt><dd>{report.formalS2.completeFactorSecurityCount} 只</dd>
        <dt>同池完整正式排名</dt><dd>{report.formalS2.rankingPoolSize ?? 0} 只；输入范围 {report.formalS2.scopeStatus ?? "CLOSED"}</dd>
        <dt>同日市值缺失或尚未核验</dt><dd>{report.formalS2.missingMarketCapCount ?? "待刷新"} 只</dd>
        <dt>已采集、待后续截止</dt><dd>{report.formalS2.pendingMetricPeriodCount} 项正式指标与期间</dd>
        <dt>原报事实已核验完整、待后续截止</dt><dd>{report.formalS2.pendingCompleteFactSecurityCount ?? "待刷新"} 只；四因子仍需市值和有效分母</dd>
        <dt>历史重建财务齐全</dt><dd>{report.historicalReconstructed.completeFactSecurityCount} / {report.s1Optional.poolCount} 只</dd>
      </dl>
      <Callout tone="warn" title={report.formalS2.strategyFamilyStatus === "CLOSED" ? "正式 S2 尚未开放" : "正式 S2 状态待核对"}>
        历史重建财务用于研究解释。正式财务需同时通过原文核验、修订检查和可用时间检查；有财务记录不代表四因子已经就绪。
      </Callout>
      <p className="note">模拟池可选财务来自本次冻结输入，后补财务不改写已保存的决策和收益。</p>
      <BindingDetails binding={binding} resultHash={result.resultHash} />
      <details><summary>查看全部 {report.securities.length} 只证券的财务缺项（优先目标在前）</summary>
        {[...report.securities].sort((a, b) => Number(b.isPriority) - Number(a.isPriority)).map(row => <details key={row.instrumentId}>
          <summary>{row.displayName || row.code} · {row.instrumentId} {row.isPriority && <Badge tone="accent">M/E 优先目标</Badge>}
            {row.formalS2.completePendingFacts && <Badge tone="warn">原报事实完整，待后续截止</Badge>}
            {" · 正式 "}{row.formalS2.acceptedMetricPeriodCount}/{row.formalS2.requiredMetricPeriodCount}
            {" · 重建 "}{row.historicalReconstructed.availableMetricPeriodCount}/{row.formalS2.requiredMetricPeriodCount}</summary>
          <p className="note">公式需求截止：{row.targetPeriodEnd}。归母利润 TTM：{row.s1Optional.ttmAvailable ? "可计算" : "尚缺资料"}；
            缺少期间：{row.s1Optional.missingPeriods.join("、") || "无"}。
            {row.formalS2.factorExclusionCode && ` 正式因子限制：${row.formalS2.factorExclusionCode}`}</p>
          {row.targetPeriodDiscovery && <p className="note">官方最新报告期发现：
            {row.targetPeriodDiscovery.latest_period_end ?? "仍待完整索引核对"}。
            {row.targetPeriodDiscovery.index_observed_at && ` 实际观察 ${formatAsOf(row.targetPeriodDiscovery.index_observed_at)}。`}
            {row.targetPeriodDiscovery.latest_period_end && !row.targetPeriodDiscovery.effective_at_cutoff &&
              " 新发现晚于本次快照截止，保留为后续输入，不改变原需求。"}</p>}
          <FinancialInputBridge input={row.formalS2} />
          <div style={{ overflowX: "auto" }}><table className="data"><thead><tr>
            <th>指标</th><th>报告期</th><th>正式状态</th><th>原值（元）</th><th>实际首见 / 可用时间</th>
          </tr></thead><tbody>{row.formalS2.metricPeriods.map(item => <tr key={`${item.metric}-${item.periodEnd}`}>
            <td>{metricLabels[item.metric] || item.metric}</td><td>{item.periodEnd}</td>
            <td>{statusLabels[item.status] || item.status}</td><td>{item.valueYuan ?? "—"}</td>
            <td>{item.firstSeenAt ? formatAsOf(item.firstSeenAt) : "—"}<br />{item.availableAt ? formatAsOf(item.availableAt) : "—"}</td>
          </tr>)}</tbody></table></div>
        </details>)}
      </details>
    </Card>
  </Section>;
}

export function FinancialInputBridge({ input }: { input: FinancialCoverageReport["securities"][number]["formalS2"] }) {
  const values = input.financialInputs;
  const cap = input.marketCap;
  const pending = input.financialInputStatus === "NEXT_CUTOFF_PENDING";
  const inputReason = input.financialInputReason?.trim();
  return <div>
    <p><strong>财务计算与真实分母（元）</strong>{pending && <Badge tone="warn">待后续截止，尚未用于当前决策</Badge>}</p>
    {values ? <>
      <p className="note">TTM 区间端点 {values.ttm_start_period_end} → {values.latest_period_end}；
        {pending ? "以下为已核验原报的后续截止输入。" : "以下为当前快照可用输入。"}</p>
      <div style={{ overflowX: "auto" }}><table className="data"><thead><tr><th>因子</th><th>分子</th><th>分母及计算</th></tr></thead>
        <tbody>
          <tr><td>F07 归母 ROE</td><td>归母利润 TTM {values.attributable_net_profit_ttm}</td>
            <td>平均归母权益 {values.average_parent_equity}<br />（{values.start_parent_equity} + {values.end_parent_equity}）÷ 2</td></tr>
          <tr><td>F08 现金质量</td><td>经营现金流 TTM {values.operating_cashflow_ttm}</td>
            <td>合并净利润 TTM {values.consolidated_net_profit_ttm}</td></tr>
          <tr><td>F09 收入增长</td><td>收入 TTM {values.revenue_ttm}</td>
            <td>上年收入 TTM {values.prior_revenue_ttm}；两者比值减 1</td></tr>
          <tr><td>F10 盈利收益率</td><td>归母利润 TTM {values.attributable_net_profit_ttm}</td>
            <td>{cap?.status === "AVAILABLE" ? `同日总市值 ${cap.value_yuan}` : "同日总市值缺失或尚未核验"}</td></tr>
        </tbody></table></div>
      {!!values.non_positive_denominator_factors?.length && <p className="note">
        业务排除：{values.non_positive_denominator_factors.join("、")} 分母非正，不进入普通工商 S2 排名。</p>}
    </> : <p className="note">{inputReason || "完整同口径、同版本原报事实尚未齐全；分母不填零。"}</p>}
    {cap?.status === "AVAILABLE" && <p className="note">市值日期 {cap.as_of}；来源 {cap.source_id}；
      实际观察 {cap.observed_at ? formatAsOf(cap.observed_at) : "—"}。</p>}
    {input.signal && <p className="note">完整正式 S2 同池百分位 {input.signal.signal_rank.toFixed(6)}；
      F07 {input.signal.factors.F07}、F08 {input.signal.factors.F08}、F09 {input.signal.factors.F09}、F10 {input.signal.factors.F10}。</p>}
  </div>;
}
