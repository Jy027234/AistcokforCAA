import assert from "node:assert/strict";
import test from "node:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { renderToStaticMarkup } from "react-dom/server";
import { api } from "../src/lib/api";
import { checkResearchSelection, restoreResearchDrafts, summarizePlan } from "../src/lib/workflow";
import { TodayView } from "../src/views/TodayView";
import { ResearchView } from "../src/views/ResearchView";
import { DraftPanel } from "../src/components/DraftPanel";
import { StatusDrawer, TopBar } from "../src/components/TopBar";
import { ExperimentsApiView } from "../src/components/ExperimentHistory";
import { NoChangeDecision } from "../src/components/NoChangeDecision";
import { ResearchSimulationPanel } from "../src/components/ResearchSimulationPanel";
import { EvidenceDrawer, ResearchCard } from "../src/components/ResearchCard";
import { ResearchAssistantPanel } from "../src/components/ResearchAssistantPanel";
import { ResearchCandidates } from "../src/components/ResearchCandidates";
import { LedgerPanel } from "../src/components/LedgerPanel";
import { ProposalSummary } from "../src/components/DecisionJourney";
import App from "../src/App";
import { PortfolioView } from "../src/views/PortfolioView";
import { SettingsView } from "../src/views/SettingsView";
import { WorkspaceView } from "../src/views/WorkspaceView";
import type { Draft, WorkspaceData } from "../src/lib/types";
import type { PreviewResponse, ResearchSimulationContext, ScheduleStatus } from "../src/lib/api";

const noop = () => {};
const draft: Draft = { planId: "", portfolioId: "pf-user-sim", tradingDay: "2026-10-13", frozen: false,
  frozenLabel: "尚未请求服务端预览", orders: [], estimatedFees: "—", cashBefore: "—", cashAfter: "—",
  cashAfterCents: 0, buyTotal: "—", sellTotal: "—", industryCapPct: "—", industry: [], excluded: [], ruleChecks: [],
  confirmAction: { id: "plan.freeze", label: "确认并冻结计划", requirement: "需要人类用户显式确认", revalidate: [] },
};
const data = { dataSource: "api", draft, candidates: [], todayChanges: [], portfolios: [], experiments: [], decisionLog: [], researchCards: [],
  status: { snapshotId: "decision-1", asOfTime: "2026-10-12T16:00:00+08:00", blockingIssues: [], dataMode: "REAL", accountLabel: "模拟账户", readiness: "PARTIAL", readinessLabel: "部分可用" },
} as unknown as WorkspaceData;
const context: ResearchSimulationContext = { researchSnapshotId: "research-1", researchContentHash: "hash-1",
  researchAvailableAt: "2026-10-12T19:00:00+08:00", researchDataAsOf: "2026-10-12", decisionSnapshotId: "decision-1",
  decisionCutoffAt: "2026-10-12T16:00:00+08:00", nextTradingDay: "2026-10-13", eligibleCandidateCount: 1,
  researchCandidateCount: 2, simulationParameters: { maxHoldings: 20, initialTargetPct: "5", maxSingleNamePct: "10", maxSingleIndustryPct: "30", maxTotalEquityPct: "90" },
  candidates: [{ instrumentId: "SH.600001", displayName: "测试证券", position: 1, eligible: true, reasons: [] },
    { instrumentId: "SH.600002", displayName: "受限证券", position: 2, eligible: false, reasons: [{ code: "NO_DATA", message: "缺少执行资格" }] }], blockingIssues: [],
};
const scheduleStatus: ScheduleStatus = {
  schedule: { enabled: true, runAtLocal: "20:30", weekdaysOnly: true, interpreter: "python.exe",
    dataDir: "", windowStart: "2026-06-22", updatedAt: "2026-10-31T20:30:00+08:00", updatedBy: "user" },
  nextFireAt: "2026-11-02T20:30:00+08:00", lastRun: null, pending: null,
  checkedAt: "2026-11-01T10:00:00+08:00",
};
function text(renderer: ReactTestRenderer) { return JSON.stringify(renderer.toJSON()); }
function button(renderer: ReactTestRenderer, label: string) {
  return renderer.root.findAllByType("button").find(item => item.children.join("").includes(label))!;
}

test("snapshot catalog requests are shared only while in flight", async () => {
  const originalFetch = globalThis.fetch;
  let fetchCalls = 0;
  const pendingFetches: { resolve: (response: Response) => void; reject: (reason: Error) => void }[] = [];
  const response = (snapshotId: string) => ({ ok: true, status: 200,
    text: async () => JSON.stringify({ count: 1, snapshots: [{ snapshotId }] }) } as Response);
  globalThis.fetch = (async () => {
    fetchCalls++;
    if (fetchCalls === 1 || fetchCalls === 3) {
      return new Promise<Response>((resolve, reject) => pendingFetches.push({ resolve, reject }));
    }
    if (fetchCalls === 2) return response("fresh-after-success");
    if (fetchCalls === 4) return response("fresh-after-failure");
    throw new Error("unexpected snapshot catalog fetch");
  }) as typeof fetch;

  try {
    const firstRead = api.snapshots();
    const joinedRead = api.snapshots();
    assert.equal(fetchCalls, 1, "overlapping reads make a single HTTP request");
    pendingFetches[0].resolve(response("shared-result"));
    const [firstResult, joinedResult] = await Promise.all([firstRead, joinedRead]);
    assert.strictEqual(firstResult, joinedResult);

    const nextRead = await api.snapshots();
    assert.equal(fetchCalls, 2, "a completed request is not cached");
    assert.equal(nextRead.snapshots[0].snapshotId, "fresh-after-success");
    assert.notStrictEqual(nextRead, firstResult);

    const failedReads = Promise.allSettled([api.snapshots(), api.snapshots()]);
    assert.equal(fetchCalls, 3, "overlapping reads share the in-flight retryable request");
    pendingFetches[1].reject(new Error("offline"));
    const failures = await failedReads;
    assert.deepEqual(failures.map(result => result.status), ["rejected", "rejected"]);
    assert.strictEqual((failures[0] as PromiseRejectedResult).reason,
      (failures[1] as PromiseRejectedResult).reason, "both callers receive the shared failure");

    const retry = await api.snapshots();
    assert.equal(fetchCalls, 4, "a failed request is cleared before retry");
    assert.equal(retry.snapshots[0].snapshotId, "fresh-after-failure");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("uncomputed API state does not imply passed checks, no adjustment or demo orders", () => {
  const today = renderToStaticMarkup(<TodayView data={data} onOpenCard={noop} onOpenDraft={noop} />);
  const panel = renderToStaticMarkup(<DraftPanel draft={draft} livePreview={null} apiUp={true}
    onRequestPreview={noop} onConfirm={noop} confirming={false} confirmResult={null} />);
  assert.match(today, /尚未评估模拟方案/);
  assert.doesNotMatch(today, /全部通过|今日无需调整/);
  assert.match(today, /<button[^>]*>查看或创建模拟方案<\/button>/);
  assert.match(panel, /暂不能判断是否需要调整/);
  assert.doesNotMatch(panel, /当前没有需要调整的持仓|（演示数据）/);
  const withoutChecks = { orders: [], rule_checks: [] } as unknown as PreviewResponse;
  assert.equal(summarizePlan(draft, withoutChecks, null, "api").checked, false);
});

test("four task entries keep legacy tools available as secondary navigation", () => {
  const markup = renderToStaticMarkup(<TopBar status={data.status} tab="portfolio" onTab={noop}
    onOpenStatus={noop} lastLoadedAt={null} loading={false} />);
  const nav = markup.match(/<nav[^>]*aria-label="主要任务"[^>]*>(.*?)<\/nav>/s)![1];
  assert.equal((nav.match(/<button/g) ?? []).length, 4);
  for (const label of ["今日", "研究", "模拟组合", "复盘"]) assert.ok(nav.includes(label));
  assert.ok(markup.includes("数据诊断与高级工具"));
  assert.ok(markup.includes('aria-current="page"'));
});

test("research handoff fails closed on changed source or missing eligibility", () => {
  const request = { requestId: 1, instrumentId: "SH.600001", researchSnapshotId: "research-1" };
  assert.equal(checkResearchSelection(request, context).acceptedId, request.instrumentId);
  assert.equal(checkResearchSelection({ ...request, researchSnapshotId: "older" }, context).acceptedId, null);
  assert.equal(checkResearchSelection({ ...request, researchSnapshotId: null }, context).acceptedId, null);
  assert.equal(checkResearchSelection({ ...request, instrumentId: "SH.600003" }, context).acceptedId, null);
  assert.match(checkResearchSelection({ ...request, instrumentId: "SH.600002" }, context).message, /缺少执行资格/);
});

test("NO_CHANGE binds visible snapshot and reason, rejects duplicate click and waits for server success", async () => {
  const original = api.recordNoChange;
  const calls: unknown[] = [];
  let resolveSave!: (value: { decision_id: string; decision_type: string }) => void;
  api.recordNoChange = body => { calls.push(body); return new Promise(resolve => { resolveSave = resolve; }); };
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<NoChangeDecision portfolioId="pf-user-sim" snapshotId="decision-visible"
      preview={null} enabled={true} pendingFrozen={false} />); });
    await act(async () => { renderer.root.findByType("textarea").props.onChange({ target: { value: "等待新的可核验依据" } }); });
    const save = button(renderer, "保存维持决定");
    await act(async () => { save.props.onClick(); save.props.onClick(); });
    assert.equal(calls.length, 1);
    assert.equal((calls[0] as any).snapshot_id, "decision-visible");
    assert.equal((calls[0] as any).reason_note, "等待新的可核验依据");
    assert.equal((calls[0] as any).model_proposed, null);
    assert.equal((calls[0] as any).rule_check, null);
    assert.doesNotMatch(text(renderer), /维持决定已保存/);
    await act(async () => { resolveSave({ decision_id: "saved-1", decision_type: "NO_CHANGE" }); });
    assert.match(text(renderer), /维持决定已保存/);
    assert.equal(button(renderer, "保存维持决定"), undefined);
  } finally { renderer?.unmount(); api.recordNoChange = original; }
});

test("a rejected NO_CHANGE request leaves a retryable unsaved decision", async () => {
  const original = api.recordNoChange;
  api.recordNoChange = async () => { throw new Error("server unavailable"); };
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<NoChangeDecision portfolioId="pf-user-sim" snapshotId="decision-1"
      preview={null} enabled={true} pendingFrozen={false} />); });
    await act(async () => { renderer.root.findByType("textarea").props.onChange({ target: { value: "保持观察" } }); });
    await act(async () => { await button(renderer, "保存维持决定").props.onClick(); });
    assert.match(text(renderer), /决定尚未保存/);
    assert.doesNotMatch(text(renderer), /维持决定已保存/);
    assert.equal(button(renderer, "保存维持决定").props.disabled, false);
  } finally { renderer?.unmount(); api.recordNoChange = original; }
});

test("research choice enters a same-source draft without automatically confirming or executing", async () => {
  const originals = { context: api.researchSimulationContext, drafts: api.researchSimulationDrafts, snapshots: api.snapshots,
    plans: api.plans, decisions: api.decisions, create: api.createResearchSimulationDraft, confirm: api.requestConfirmation, execute: api.execute };
  const created: any[] = [];
  api.researchSimulationContext = async () => context;
  api.researchSimulationDrafts = async () => ({ drafts: [] });
  api.snapshots = async () => ({ snapshots: [] } as any);
  api.plans = async () => ({ plans: [] } as any);
  api.decisions = async () => ({ decisions: [] } as any);
  api.createResearchSimulationDraft = async body => { created.push(body); return { draftId: "saved-draft", status: "WAITING_EXECUTION_SNAPSHOT" } as any; };
  api.requestConfirmation = async () => { throw new Error("should not confirm"); };
  api.execute = async () => { throw new Error("should not execute"); };
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<ResearchSimulationPanel portfolioId="pf-user-sim"
      selectionRequest={{ requestId: 1, instrumentId: "SH.600001", researchSnapshotId: "research-1" }} />); });
    const checkbox = renderer.root.findAllByType("input").find(item => item.props["aria-label"] === "选择 测试证券")!;
    assert.equal(checkbox.props.checked, true);
    await act(async () => { renderer.root.findAllByType("input").find(item => item.props.placeholder === "记录本次选择依据")!.props.onChange({ target: { value: "核验原报告后观察" } }); });
    await act(async () => { button(renderer, "生成并保存模拟草稿").props.onClick(); });
    assert.equal(created.length, 1);
    assert.deepEqual(created[0].selectedInstrumentIds, ["SH.600001"]);
    assert.equal(created[0].researchSnapshotId, "research-1");
    assert.equal(created[0].decisionSnapshotId, "decision-1");
    assert.equal(created[0].source, "USER_SELECTION");
    assert.match(text(renderer), /等待执行日行情/);
  } finally {
    renderer?.unmount(); api.researchSimulationContext = originals.context; api.researchSimulationDrafts = originals.drafts;
    api.snapshots = originals.snapshots; api.plans = originals.plans; api.createResearchSimulationDraft = originals.create;
    api.decisions = originals.decisions;
    api.requestConfirmation = originals.confirm; api.execute = originals.execute;
  }
});

test("late NO_CHANGE success cannot be relabeled as a newer visible snapshot", async () => {
  const original = api.recordNoChange;
  let resolveSave!: (value: any) => void;
  api.recordNoChange = async () => new Promise(resolve => { resolveSave = resolve; });
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<NoChangeDecision portfolioId="pf-user-sim" snapshotId="OLD"
      preview={null} enabled={true} pendingFrozen={false} />); });
    await act(async () => { renderer.root.findByType("textarea").props.onChange({ target: { value: "OLD的原判断" } }); });
    await act(async () => { button(renderer, "保存维持决定").props.onClick(); });
    await act(async () => { renderer.update(<NoChangeDecision portfolioId="pf-user-sim" snapshotId="NEW"
      preview={null} enabled={true} pendingFrozen={false} />); });
    await act(async () => { resolveSave({ decision_id: "OLD-record", decision_type: "NO_CHANGE" }); });
    assert.doesNotMatch(text(renderer), /维持决定已保存|OLD-record/);
    assert.equal(renderer.root.findByType("textarea").props.value, "");
  } finally { renderer?.unmount(); api.recordNoChange = original; }
});

test("a new frozen plan does not inherit the previous plan's execution result", async () => {
  const original = api.execute;
  api.execute = async () => ({ fills: [], rejections: [], corporate_actions: [] } as any);
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<LedgerPanel portfolioId="pf-user-sim" snapshotId="exec-1"
      tradingDay="2026-10-13" planId="plan-A" frozen={true} persistedPlanStatus="FROZEN" />); });
    await act(async () => { button(renderer, "执行本交易日").props.onClick(); });
    assert.match(text(renderer), /本交易日已执行/);
    await act(async () => { renderer.update(<LedgerPanel portfolioId="pf-user-sim" snapshotId="exec-2"
      tradingDay="2026-10-14" planId="plan-B" frozen={true} persistedPlanStatus="FROZEN" />); });
    assert.doesNotMatch(text(renderer), /本交易日已执行|本日没有任何成交/);
    assert.equal(button(renderer, "执行本交易日").props.disabled, false);
  } finally { renderer?.unmount(); api.execute = original; }
});

test("saved immutable choice recovers only through its exact decision and durable plan", async () => {
  const saved = { draftId: "source-draft", portfolioId: "pf-user-sim", researchSnapshotId: "research-1",
    decisionSnapshotId: "decision-1", tradingDay: "2026-10-13", source: "USER_SELECTION", status: "WAITING_EXECUTION_SNAPSHOT",
    planId: null, confirmationAvailable: false, selectedInstrumentIds: ["SH.600001"], selectionReason: "原理由" } as any;
  const decision = { portfolio_id: "pf-user-sim", snapshot_id: "decision-1", plan_id: "durable-plan",
    human_final: { research_origin: { sourceDraftId: "source-draft", researchSnapshotId: "research-1" } } } as any;
  const plan = { planId: "durable-plan", portfolioId: "pf-user-sim", decisionSnapshotId: "decision-1",
    executionSnapshotId: "exec-1", tradingDay: "2026-10-13", status: "FROZEN" } as any;
  const restored = restoreResearchDrafts([saved], [plan], [decision])[0];
  assert.equal(restored.planId, "durable-plan");
  assert.equal(restored.status, "FROZEN");
  assert.equal(saved.planId, null);
  assert.equal(saved.status, "WAITING_EXECUTION_SNAPSHOT");
  assert.equal(restoreResearchDrafts([saved], [plan], [{ ...decision, portfolio_id: "other" }])[0].planId, null);
  const originals = { context: api.researchSimulationContext, drafts: api.researchSimulationDrafts, snapshots: api.snapshots,
    plans: api.plans, decisions: api.decisions, preview: api.previewSavedResearchDraft, confirm: api.requestConfirmation, freeze: api.freeze };
  let frozen = false;
  let qualificationReads = 0;
  let holdQualification = false;
  let finishQualification!: (context: ResearchSimulationContext) => void;
  let draftReads = 0;
  api.researchSimulationContext = async () => { qualificationReads++; return holdQualification ? new Promise(resolve => { finishQualification = resolve; }) : context; };
  api.researchSimulationDrafts = async () => { draftReads++; return { drafts: [saved] }; };
  api.snapshots = async () => ({ snapshots: [{ snapshotId: "exec-1", tradingDay: saved.tradingDay, qualityStatus: "OK" }] } as any);
  api.plans = async () => ({ plans: frozen ? [plan] : [] } as any);
  api.decisions = async () => ({ decisions: frozen ? [decision] : [] } as any);
  api.previewSavedResearchDraft = async () => ({ ...saved, planId: "temporary-preview", executionSnapshotId: "exec-1",
    status: "READY_FOR_REVIEW", confirmationAvailable: true });
  api.requestConfirmation = async () => ({ confirmationToken: "one-time" } as any);
  api.freeze = async () => { frozen = true; return { plan_id: "durable-plan" }; };
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<ResearchSimulationPanel portfolioId="pf-user-sim" />); });
    await act(async () => { button(renderer, "检查 2026-10-13 模拟预览").props.onClick(); });
    assert.ok(button(renderer, "确认并冻结此模拟计划"));
    await act(async () => { renderer.update(<ResearchSimulationPanel portfolioId="pf-user-sim" active={false} />); });
    await act(async () => { renderer.update(<ResearchSimulationPanel portfolioId="pf-user-sim" active={true} />); });
    assert.equal(qualificationReads, 1, "pure task navigation does not repeat the full qualification read");
    assert.ok(button(renderer, "确认并冻结此模拟计划"), "returning from research retains the reviewed choice");
    holdQualification = true;
    await act(async () => { button(renderer, "刷新资格与草稿").props.onClick(); });
    const readsBeforeConfirmation = draftReads;
    await act(async () => { button(renderer, "确认并冻结此模拟计划").props.onClick(); });
    assert.equal(frozen, true);
    await act(async () => { holdQualification = false; finishQualification(context); });
    assert.ok(draftReads > readsBeforeConfirmation, "write completion re-reads facts after any older in-flight refresh");
    assert.ok(button(renderer, "执行本交易日"), "durable freeze leaves a reachable execution entry");
    assert.equal(renderer.root.findByType(LedgerPanel).props.planId, "durable-plan");
    renderer.unmount();
    await act(async () => { renderer = create(<ResearchSimulationPanel portfolioId="pf-user-sim" />); });
    assert.equal(renderer.root.findByType(LedgerPanel).props.planId, "durable-plan", "page reload recovers via original source binding");
  } finally {
    renderer?.unmount(); api.researchSimulationContext = originals.context; api.researchSimulationDrafts = originals.drafts;
    api.snapshots = originals.snapshots; api.plans = originals.plans; api.decisions = originals.decisions;
    api.previewSavedResearchDraft = originals.preview; api.requestConfirmation = originals.confirm; api.freeze = originals.freeze;
  }
});

test("review reads actual nested research proposal and explicit selection before raw JSON", () => {
  const markup = renderToStaticMarkup(<ProposalSummary value={{
    proposal: { orders: [{ instrument_id: "SH.600001", side: "BUY", quantity: 100, price_cents: 1500 }], estimated_fees_cents: 500, cash_after_cents: 98500 },
    selection: { selectedInstrumentIds: ["SH.600001"], selectionReason: "原报告核验后纳入" },
  }} />);
  assert.match(markup, /SH\.600001/);
  assert.match(markup, /原报告核验后纳入/);
  assert.doesNotMatch(markup, /未提供标准字段/);
});

test("App connects research selection to simulation and retains research filters on return", async () => {
  const keys = ["status", "candidates", "events", "snapshots", "plans", "watchlist", "researchSimulationDrafts", "decisions",
    "researchSimulationContext", "researchCandidates", "financialCoverage", "submitFinancialCoverageJob",
    "getFinancialCoverageJob", "retryFinancialCoverageJob", "ledger"] as const;
  const originals = Object.fromEntries(keys.map(key => [key, api[key]]));
  const oldWindow = globalThis.window;
  const fakeWindow: any = { location: { hash: "#research", search: "" }, history: { pushState: (_: any, __: any, hash: string) => { fakeWindow.location.hash = hash; } },
    addEventListener: noop, removeEventListener: noop, setTimeout, clearTimeout };
  globalThis.window = fakeWindow;
  api.status = async () => data.status;
  api.candidates = async () => ({ snapshotId: "decision-1", candidates: [], note: "test" } as any);
  api.events = async () => ({ snapshotId: "decision-1", events: [] } as any);
  api.snapshots = async () => ({ snapshots: [{ snapshotId: "decision-1", dataMode: "REAL", asOfTime: data.status.asOfTime,
    tradingDay: "2026-10-12", capabilities: { s1Decision: { available: true } } }] } as any);
  api.plans = async () => ({ plans: [] } as any);
  api.watchlist = async () => ({ items: [] } as any);
  api.researchSimulationDrafts = async () => ({ drafts: [] });
  api.decisions = async () => ({ decisions: [] } as any);
  api.researchSimulationContext = async () => context;
  let legacyCoverageReads = 0;
  let coverageSubmissions = 0;
  let coverageRetries = 0;
  let coverageStatusReads = 0;
  let firstCoverageSignalAborted = false;
  const coverageBinding = { inputHash: "input-hash-1", snapshotId: "coverage-snapshot-1",
    snapshotCutoffAt: "2026-10-12T16:00:00+08:00", tradingDay: "2026-10-12",
    researchSnapshotId: "research-1", researchContentHash: "research-hash-1", liveFactsVersion: "facts-v1",
    proofVersion: "proof-v1", reconstructedFactsVersion: "rebuild-v1", targetMapHash: "target-hash-1",
    priorityInstrumentIdsHash: "priority-hash-1", codeVersion: "code-v1" };
  const oldJobCreatedAt = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
  const retryJobCreatedAt = new Date(Date.now() - 10 * 60 * 1000).toISOString();
  const coverageReport: any = { schema: "aquant.financial-coverage.v1", snapshotId: "coverage-snapshot-1",
    snapshotCutoffAt: coverageBinding.snapshotCutoffAt, generatedAt: "2026-10-12T19:00:00+08:00",
    latestPeriodEnd: "2026-09-30", targetPeriodEnd: "2026-09-30", targetPeriodBasis: "test",
    s1Optional: { statementSecurityCount: 1, ttmSecurityCount: 1, poolCount: 1, presenceRatio: 1, ttmRatio: 1, note: "" },
    formalS2: { requiredMetricPeriodCount: 0, acceptedMetricPeriodCount: 0, completeFactSecurityCount: 0,
      completeFactorSecurityCount: 0, acceptedFactCount: 0, liveFactCount: 0, strategyFamilyStatus: "CLOSED",
      pendingMetricPeriodCount: 0, pendingCompleteFactSecurityCount: 0, scopeStatus: "CLOSED", rankingPoolSize: 0,
      missingMarketCapCount: 0 },
    historicalReconstructed: { requiredMetricPeriodCount: 0, availableMetricPeriodCount: 0,
      completeFactSecurityCount: 0, pitMode: "HISTORICAL_RECONSTRUCTED" }, securities: [] };
  const coverageSubmission = (jobId: string) => ({ jobId, jobType: "FINANCIAL_CHECK", status: "PENDING",
    created: true, snapshotId: coverageBinding.snapshotId, inputBinding: coverageBinding,
    submittedAt: "2026-10-12T19:00:00+08:00" });
  const coverageJob = (jobId: string, status: "PENDING" | "RUNNING" | "FAILED" | "SUCCEEDED") => ({
    jobId, jobType: "FINANCIAL_CHECK", status,
    createdAt: jobId === "coverage-job-2" ? retryJobCreatedAt : oldJobCreatedAt,
    updatedAt: "2026-10-12T19:00:01+08:00", heartbeatAt: status === "PENDING" ? "2026-10-12T19:00:01+08:00" : null,
    snapshotId: coverageBinding.snapshotId, inputBinding: coverageBinding,
    result: status === "SUCCEEDED" ? { report: coverageReport, verifiedAt: "2026-10-12T19:00:03+08:00",
      binding: coverageBinding, resultHash: "result-hash-1" } : null,
    error: status === "FAILED" ? { code: "FINANCIAL_CHECK_FAILED", message: "财务来源核验未完成", repairAction: "检查核验输入后重试。" } : null,
    isCurrent: status === "SUCCEEDED" ? false : null,
  });
  api.financialCoverage = async () => { legacyCoverageReads++; throw new Error("legacy synchronous diagnostic must not run"); };
  api.submitFinancialCoverageJob = async () => { coverageSubmissions++; return coverageSubmission("coverage-job-1") as any; };
  let originalJobStatusReads = 0;
  let retryJobStatusReads = 0;
  api.getFinancialCoverageJob = async (jobId, signal) => {
    coverageStatusReads++;
    if (jobId === "coverage-job-1" && coverageStatusReads === 1) {
      return new Promise((_resolve, reject) => {
        signal?.addEventListener("abort", () => { firstCoverageSignalAborted = true; reject(new Error("aborted")); }, { once: true });
      }) as any;
    }
    if (jobId === "coverage-job-1") {
      originalJobStatusReads++;
      return coverageJob(jobId, originalJobStatusReads === 1 ? "RUNNING" : "FAILED") as any;
    }
    retryJobStatusReads++;
    return coverageJob(jobId, retryJobStatusReads === 1 ? "RUNNING" : "SUCCEEDED") as any;
  };
  api.retryFinancialCoverageJob = async jobId => {
    assert.equal(jobId, "coverage-job-1");
    coverageRetries++;
    return coverageSubmission("coverage-job-2") as any;
  };
  api.ledger = async () => ({ portfolio_id: "pf-user-sim", cash: { cents: 1000000 }, positions: [], opened_at: "2026-10-12" } as any);
  api.researchCandidates = async () => ({ schema: "aquant.research-candidates.v1", strategy: "S2_RESEARCH", pitMode: "HISTORICAL_RECONSTRUCTED",
    researchOnly: true, simulationAvailable: false, snapshotId: "research-1", dataAsOf: "2026-10-12", generatedAt: "2026-10-12T19:00:00+08:00",
    poolSize: 1, rankedCount: 1, excludedCount: 0, exclusions: [], limitations: [], source: { factorReport: "test", archiveVerified: true },
    candidates: [{ instrumentId: "SH.600001", code: "600001", displayName: "测试证券", board: "MAIN", position: 1, signalRank: 0.8,
      factors: {}, factorRanks: {}, basis: [], risks: [], evidence: [] }],
  } as any);
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<App />); });
    assert.equal(coverageSubmissions, 0, "folded diagnostic does not submit a job");
    assert.equal(legacyCoverageReads, 0);
    await act(async () => { renderer.root.findByProps({ id: "research-candidates-search" }).props.onChange({ target: { value: "测试" } }); await new Promise(resolve => setTimeout(resolve, 370)); });
    await act(async () => { button(renderer, "检查模拟资格").props.onClick(); });
    assert.equal(fakeWindow.location.hash, "#portfolio");
    assert.equal(renderer.root.findByType(PortfolioView).props.mode, "research");
    assert.equal(renderer.root.findAllByType("input").find(item => item.props["aria-label"] === "选择 测试证券")!.props.checked, true);
    await act(async () => { renderer.root.findByProps({ "aria-label": "主要任务" }).findAllByType("button").find(item => item.children.join("") === "研究")!.props.onClick(); });
    assert.equal(renderer.root.findByProps({ id: "research-candidates-search" }).props.value, "测试");
    await act(async () => { renderer.root.findByProps({ "aria-label": "主要任务" }).findAllByType("button").find(item => item.children.join("") === "模拟组合")!.props.onClick(); });
    assert.equal(renderer.root.findAllByType("input").find(item => item.props["aria-label"] === "选择 测试证券")!.props.checked, true);
    await act(async () => { renderer.root.findByProps({ "aria-label": "主要任务" }).findAllByType("button").find(item => item.children.join("") === "研究")!.props.onClick(); });
    const coverageDetails = renderer.root.findAllByType("details").find(item => item.findAllByType("summary")[0]?.children.join("").includes("财务数据覆盖"))!;
    await act(async () => { coverageDetails.props.onToggle({ currentTarget: { open: true } }); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.equal(coverageSubmissions, 1);
    assert.equal(coverageStatusReads, 1, "the first status read may be slow without blocking the candidate list");
    assert.match(text(renderer), /已排队，等待执行/);
    assert.match(text(renderer), /已用/);
    assert.match(text(renderer), /input-hash-1/, "the server-generated input binding is visible before the report completes");
    assert.ok(renderer.root.findByProps({ id: "research-candidates-search" }));
    await act(async () => { coverageDetails.props.onToggle({ currentTarget: { open: false } }); coverageDetails.props.onToggle({ currentTarget: { open: true } }); });
    assert.equal(coverageSubmissions, 1, "closing and reopening does not submit a duplicate job");
    await act(async () => { renderer.unmount(); renderer = undefined as any; });
    await new Promise(resolve => setTimeout(resolve, 2600));
    assert.equal(coverageStatusReads, 1, "unmount stops the timer and does not keep polling in the background");
    assert.equal(firstCoverageSignalAborted, true, "unmount aborts the pending status GET");
    await act(async () => { renderer = create(<App />); await new Promise(resolve => setTimeout(resolve, 0)); });
    const resumedCoverageDetails = renderer.root.findAllByType("details").find(item => item.findAllByType("summary")[0]?.children.join("").includes("财务数据覆盖"))!;
    await act(async () => { resumedCoverageDetails.props.onToggle({ currentTarget: { open: true } }); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.equal(coverageSubmissions, 1, "returning to an active job resumes its existing job instead of resubmitting");
    assert.equal(coverageStatusReads, 2);
    const elapsedMinutes = () => {
      const match = text(renderer).match(/已用[^0-9]*(\d+):(\d{2})/);
      assert.ok(match, "an active financial check shows elapsed time");
      return Number(match[1]) + Number(match[2]) / 60;
    };
    const reusedJobElapsedMinutes = elapsedMinutes();
    assert.ok(reusedJobElapsedMinutes > 1000,
      "a reused long-running job measures from the server createdAt instead of restarting from zero");
    await act(async () => { renderer.unmount(); renderer = undefined as any; });
    await act(async () => { renderer = create(<App />); await new Promise(resolve => setTimeout(resolve, 0)); });
    const failedCoverageDetails = renderer.root.findAllByType("details").find(item => item.findAllByType("summary")[0]?.children.join("").includes("财务数据覆盖"))!;
    await act(async () => { failedCoverageDetails.props.onToggle({ currentTarget: { open: true } }); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.equal(coverageStatusReads, 3);
    assert.match(text(renderer), /财务来源核验未完成/);
    assert.ok(button(renderer, "重试失败作业"));
    await act(async () => { button(renderer, "重试失败作业").props.onClick(); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.equal(coverageRetries, 1);
    assert.equal(coverageStatusReads, 4);
    const retryElapsedMinutes = elapsedMinutes();
    assert.ok(retryElapsedMinutes > 2 && retryElapsedMinutes < reusedJobElapsedMinutes / 2,
      "retry elapsed time starts from the new child job createdAt, not its failed parent");
    await act(async () => { renderer.unmount(); renderer = undefined as any; });
    await act(async () => { renderer = create(<App />); await new Promise(resolve => setTimeout(resolve, 0)); });
    const retryCoverageDetails = renderer.root.findAllByType("details").find(item => item.findAllByType("summary")[0]?.children.join("").includes("财务数据覆盖"))!;
    await act(async () => { retryCoverageDetails.props.onToggle({ currentTarget: { open: true } }); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.equal(coverageStatusReads, 5);
    assert.match(text(renderer), /核验于/);
    assert.match(text(renderer), /coverage-snapshot-1/);
    assert.match(text(renderer), /result-hash-1/);
    assert.match(text(renderer), /输入版本已变化/);
    assert.doesNotMatch(text(renderer), /服务端确认本次核验输入仍是当前版本/);
    assert.equal(legacyCoverageReads, 0, "the synchronous endpoint is not used");
    await act(async () => { retryCoverageDetails.props.onToggle({ currentTarget: { open: false } }); retryCoverageDetails.props.onToggle({ currentTarget: { open: true } }); });
    assert.deepEqual([coverageSubmissions, coverageRetries, coverageStatusReads], [1, 1, 5],
      "folding and reopening keeps the completed report and source binding");

    await act(async () => { renderer.unmount(); renderer = undefined as any; });
    await act(async () => { renderer = create(<App />); await new Promise(resolve => setTimeout(resolve, 0)); });
    const restoredCoverageDetails = renderer.root.findAllByType("details").find(item => item.findAllByType("summary")[0]?.children.join("").includes("财务数据覆盖"))!;
    await act(async () => { restoredCoverageDetails.props.onToggle({ currentTarget: { open: true } }); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.deepEqual([coverageSubmissions, coverageRetries, coverageStatusReads], [1, 1, 6],
      "a saved result is revalidated by job ID on return without creating a new job");
    assert.match(text(renderer), /输入版本已变化/);
    let finishResultRead!: (result: any) => void;
    api.submitFinancialCoverageJob = async () => ({ ...coverageSubmission("coverage-job-3"),
      status: "SUCCEEDED", created: false } as any);
    api.getFinancialCoverageJob = async () => new Promise(resolve => { finishResultRead = resolve; }) as any;
    await act(async () => {
      button(renderer, "核验当前输入版本").props.onClick();
      await new Promise(resolve => setTimeout(resolve, 0));
    });
    assert.match(text(renderer), /核验已完成，正在读取结果/,
      "a completed job must still show that its report is being read");
    await act(async () => {
      finishResultRead(coverageJob("coverage-job-3", "SUCCEEDED"));
      await new Promise(resolve => setTimeout(resolve, 0));
    });
    assert.match(text(renderer), /核验完成/);
    assert.doesNotMatch(text(renderer), /正在读取结果/);
  } finally {
    await act(async () => { renderer?.unmount(); });
    keys.forEach(key => { (api as any)[key] = originals[key]; });
    globalThis.window = oldWindow;
  }
});

test("a pending snapshot history request cannot hold today's or research content", async () => {
  const keys = ["status", "candidates", "events", "snapshots", "plans", "watchlist", "researchSimulationDrafts",
    "decisions", "researchSimulationContext", "researchCandidates"] as const;
  const originals = Object.fromEntries(keys.map(key => [key, api[key]]));
  const oldWindow = globalThis.window;
  const fakeWindow: any = { location: { hash: "#today", search: "" }, history: { pushState: (_: any, __: any, hash: string) => { fakeWindow.location.hash = hash; } },
    addEventListener: noop, removeEventListener: noop, setTimeout, clearTimeout };
  globalThis.window = fakeWindow;
  let snapshotReads = 0;
  api.status = async () => data.status;
  api.candidates = async (snapshotId?: string) => ({ snapshotId: snapshotId ?? "decision-1", candidates: [], note: "test" } as any);
  api.events = async () => ({ snapshotId: "decision-1", events: [] } as any);
  api.snapshots = () => { snapshotReads++; return new Promise(() => {}) as any; };
  api.plans = async () => ({ plans: [] } as any);
  api.watchlist = async () => ({ items: [] } as any);
  api.researchSimulationDrafts = async () => ({ drafts: [] });
  api.decisions = async () => ({ decisions: [] } as any);
  api.researchSimulationContext = async () => context;
  api.researchCandidates = async () => ({ schema: "aquant.research-candidates.v1", strategy: "S2_RESEARCH", pitMode: "HISTORICAL_RECONSTRUCTED",
    researchOnly: true, simulationAvailable: false, snapshotId: "research-1", dataAsOf: "2026-10-12", generatedAt: "2026-10-12T19:00:00+08:00",
    poolSize: 0, rankedCount: 0, excludedCount: 0, exclusions: [], limitations: [], source: { factorReport: "test", archiveVerified: true }, candidates: [] } as any);
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<App />); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.match(text(renderer), /今天从这里开始/);
    assert.equal(snapshotReads, 0, "the home content does not request historical snapshots");
    await act(async () => {
      renderer.root.findByProps({ "aria-label": "主要任务" }).findAllByType("button")
        .find(item => item.children.join("") === "研究")!.props.onClick();
      await new Promise(resolve => setTimeout(resolve, 0));
    });
    assert.ok(renderer.root.findByProps({ id: "research-candidates-search" }));
    assert.equal(snapshotReads, 0, "research content does not request historical snapshots");
  } finally {
    await act(async () => { renderer?.unmount(); });
    keys.forEach(key => { (api as any)[key] = originals[key]; });
    globalThis.window = oldWindow;
  }
});

test("direct research entry skips legacy S1 reads until expanded and rejects mismatched legacy results", async () => {
  const keys = ["status", "candidates", "events", "watchlist", "researchCandidates"] as const;
  const originals = Object.fromEntries(keys.map(key => [key, api[key]]));
  const oldWindow = globalThis.window;
  const fakeWindow: any = { location: { hash: "#research", search: "" }, history: { pushState: (_: any, __: any, hash: string) => { fakeWindow.location.hash = hash; } },
    addEventListener: noop, removeEventListener: noop, setTimeout, clearTimeout };
  globalThis.window = fakeWindow;
  let candidateReads = 0;
  let eventReads = 0;
  const candidateResolvers: ((value: any) => void)[] = [];
  const eventResolvers: ((value: any) => void)[] = [];
  api.status = async () => data.status;
  api.candidates = async () => {
    candidateReads++;
    return new Promise(resolve => candidateResolvers.push(resolve)) as any;
  };
  api.events = async () => {
    eventReads++;
    return new Promise(resolve => eventResolvers.push(resolve)) as any;
  };
  api.watchlist = async () => ({ items: [] } as any);
  api.researchCandidates = async () => ({ schema: "aquant.research-candidates.v1", strategy: "S2_RESEARCH",
    pitMode: "HISTORICAL_RECONSTRUCTED", researchOnly: true, simulationAvailable: false,
    snapshotId: "research-current", dataAsOf: "2026-10-12", generatedAt: "2026-10-12T19:00:00+08:00",
    poolSize: 1, rankedCount: 1, excludedCount: 0, exclusions: [], limitations: [],
    source: { factorReport: "test", archiveVerified: true }, candidates: [{ instrumentId: "SH.600010",
      code: "600010", displayName: "研究入口证券", board: "MAIN", position: 1, signalRank: 0.8,
      factors: {}, factorRanks: {}, basis: [], risks: [], evidence: [] }],
  } as any);
  const candidateResponse = (snapshotId: string, displayName: string) => ({ snapshotId, note: "S1 测试依据",
    candidates: [{ instrumentId: "SH.600001", displayName, signalRank: 0.7, industryCode: "I1", simulatable: true }] });
  const eventResponse = (snapshotId: string, summary: string) => ({ snapshotId, events: [{ event_id: "event-1",
    category: "DISCLOSURE", summary, available_at: "2026-10-12T18:00:00+08:00",
    verification_status: "VERIFIED", market_direction: "UNKNOWN", subjects: [] }] });
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<App />); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.ok(renderer.root.findByProps({ id: "research-candidates-search" }));
    assert.match(text(renderer), /研究入口证券/);
    assert.deepEqual([candidateReads, eventReads], [0, 0],
      "the independent research publication renders without the slow Today/S1 endpoints");

    const fold = renderer.root.findAllByType("details").find(item =>
      item.findAllByType("summary")[0]?.children.join("").includes("查看规则基线研究与自选"))!;
    await act(async () => { fold.props.onToggle({ currentTarget: { open: true } }); });
    assert.deepEqual([candidateReads, eventReads], [1, 1],
      "the legacy candidate and event reads begin only when their section is opened");
    candidateResolvers[0](candidateResponse("decision-stale", "过期规则候选"));
    eventResolvers[0](eventResponse("decision-stale", "过期事件"));
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.match(text(renderer), /API 返回了不一致的快照/);
    assert.doesNotMatch(text(renderer), /过期规则候选|过期事件/,
      "neither half of a mismatched candidate/event pair is admitted");

    await act(async () => { button(renderer, "重试").props.onClick(); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.deepEqual([candidateReads, eventReads], [2, 2]);
    candidateResolvers[1](candidateResponse("decision-1", "规则候选已恢复"));
    eventResolvers[1](eventResponse("decision-1", "事件已恢复"));
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.match(text(renderer), /规则候选已恢复/);
    await act(async () => {
      renderer.root.findByProps({ "aria-label": "主要任务" }).findAllByType("button")
        .find(item => item.children.join("") === "今日")!.props.onClick();
      await new Promise(resolve => setTimeout(resolve, 0));
    });
    assert.match(text(renderer), /事件已恢复/);
    assert.deepEqual([candidateReads, eventReads], [2, 2], "tab navigation reuses the matched legacy response");
  } finally {
    await act(async () => { renderer?.unmount(); });
    keys.forEach(key => { (api as any)[key] = originals[key]; });
    globalThis.window = oldWindow;
  }
});

test("closing the selected S1 fold persists across parent callback updates", async () => {
  const keys = ["watchlist", "researchCandidates"] as const;
  const originals = Object.fromEntries(keys.map(key => [key, api[key]]));
  const oldWindow = globalThis.window;
  globalThis.window = { setTimeout, clearTimeout } as any;
  api.watchlist = async () => ({ items: [] } as any);
  api.researchCandidates = async () => ({ schema: "aquant.research-candidates.v1", strategy: "S2_RESEARCH",
    pitMode: "HISTORICAL_RECONSTRUCTED", researchOnly: true, simulationAvailable: false,
    snapshotId: "research-current", dataAsOf: "2026-10-12", generatedAt: "2026-10-12T19:00:00+08:00",
    poolSize: 0, rankedCount: 0, excludedCount: 0, exclusions: [], limitations: [],
    source: { factorReport: "test", archiveVerified: true }, candidates: [],
  } as any);
  let legacyReads = 0;
  const makeView = () => <ResearchView data={data} selectedId="SH.600001" onSelect={noop}
    onOpenEvidence={noop} onAction={noop} onLoadLegacyS1={() => { legacyReads++; }} />;
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(makeView()); await new Promise(resolve => setTimeout(resolve, 0)); });
    const fold = () => renderer.root.findAllByType("details").find(item =>
      item.findAllByType("summary")[0]?.children.join("").includes("查看规则基线研究与自选"))!;
    assert.equal(fold().props.open, true, "a selected legacy instrument initially opens its S1 context");
    assert.equal(legacyReads, 1);

    await act(async () => { fold().props.onToggle({ currentTarget: { open: false } }); });
    assert.equal(fold().props.open, false);
    await act(async () => { renderer.update(makeView()); await Promise.resolve(); });
    assert.equal(fold().props.open, false,
      "an unrelated parent render with a new callback identity does not reopen the user's closed fold");
    assert.equal(legacyReads, 1, "the closed fold does not restart its legacy read");
  } finally {
    await act(async () => { renderer?.unmount(); });
    keys.forEach(key => { (api as any)[key] = originals[key]; });
    globalThis.window = oldWindow;
  }
});

test("Today keeps legacy S1 data pending or failed out of empty states and retries the matched snapshot", async () => {
  const keys = ["status", "candidates", "events", "plans", "watchlist", "researchSimulationDrafts", "decisions", "researchCandidates"] as const;
  const originals = Object.fromEntries(keys.map(key => [key, api[key]]));
  const oldWindow = globalThis.window;
  const fakeWindow: any = { location: { hash: "#today", search: "" }, history: { pushState: (_: any, __: any, hash: string) => { fakeWindow.location.hash = hash; } },
    addEventListener: noop, removeEventListener: noop, setTimeout, clearTimeout };
  globalThis.window = fakeWindow;
  let candidateReads = 0;
  let eventReads = 0;
  const candidateResolvers: ((value: any) => void)[] = [];
  const eventResolvers: ((value: any) => void)[] = [];
  api.status = async () => data.status;
  api.candidates = async () => {
    candidateReads++;
    return new Promise(resolve => candidateResolvers.push(resolve)) as any;
  };
  api.events = async () => {
    eventReads++;
    return new Promise(resolve => eventResolvers.push(resolve)) as any;
  };
  api.plans = async () => ({ plans: [] } as any);
  api.watchlist = async () => ({ items: [] } as any);
  api.researchSimulationDrafts = async () => ({ drafts: [] });
  api.decisions = async () => ({ decisions: [] } as any);
  api.researchCandidates = async () => ({ schema: "aquant.research-candidates.v1", strategy: "S2_RESEARCH",
    pitMode: "HISTORICAL_RECONSTRUCTED", researchOnly: true, simulationAvailable: false,
    snapshotId: "research-current", dataAsOf: "2026-10-12", generatedAt: "2026-10-12T19:00:00+08:00",
    poolSize: 0, rankedCount: 0, excludedCount: 0, exclusions: [], limitations: [],
    source: { factorReport: "test", archiveVerified: true }, candidates: [],
  } as any);
  const candidateResponse = (snapshotId: string, displayName: string) => ({ snapshotId, note: "今日规则依据",
    candidates: [{ instrumentId: "SH.600001", displayName, signalRank: 0.7, industryCode: "I1", simulatable: true }] });
  const eventResponse = (snapshotId: string, summary: string) => ({ snapshotId, events: [{ event_id: "event-today",
    category: "DISCLOSURE", summary, available_at: "2026-10-12T18:00:00+08:00",
    verification_status: "VERIFIED", market_direction: "UNKNOWN", subjects: [] }] });
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<App />); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.match(text(renderer), /今天从这里开始/);
    assert.equal(candidateReads, 1);
    assert.equal(eventReads, 1);
    assert.doesNotMatch(text(renderer), /今日没有新的可用变化|当前没有候选/,
      "pending S1 reads are not represented as a verified empty result");

    await act(async () => {
      renderer.root.findByProps({ "aria-label": "主要任务" }).findAllByType("button")
        .find(item => item.children.join("") === "研究")!.props.onClick();
      await new Promise(resolve => setTimeout(resolve, 0));
    });
    assert.ok(renderer.root.findByProps({ id: "research-candidates-search" }),
      "slow Today/S1 responses do not hold the independent research tab after navigation");
    assert.deepEqual([candidateReads, eventReads], [1, 1]);
    await act(async () => {
      renderer.root.findByProps({ "aria-label": "主要任务" }).findAllByType("button")
        .find(item => item.children.join("") === "今日")!.props.onClick();
    });

    candidateResolvers[0](candidateResponse("decision-1", "不应单独接纳的候选"));
    eventResolvers[0](eventResponse("decision-stale", "不应接纳的事件"));
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.match(text(renderer), /API 返回了不一致的快照/);
    assert.doesNotMatch(text(renderer), /不应单独接纳的候选|不应接纳的事件|当前没有候选|今日没有新的可用变化/);

    await act(async () => { button(renderer, "重试").props.onClick(); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.deepEqual([candidateReads, eventReads], [2, 2]);
    candidateResolvers[1](candidateResponse("decision-1", "今日候选已恢复"));
    eventResolvers[1](eventResponse("decision-1", "今日事件已恢复"));
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.match(text(renderer), /今日候选已恢复/);
    assert.match(text(renderer), /今日事件已恢复/);
  } finally {
    await act(async () => { renderer?.unmount(); });
    keys.forEach(key => { (api as any)[key] = originals[key]; });
    globalThis.window = oldWindow;
  }
});

test("snapshot history failure stays local and blocks production preview", async () => {
  const keys = ["status", "candidates", "events", "snapshots", "plans", "watchlist", "researchSimulationDrafts",
    "decisions", "researchSimulationContext", "researchCandidates", "preview", "requestConfirmation", "freeze"] as const;
  const originals = Object.fromEntries(keys.map(key => [key, api[key]]));
  const oldWindow = globalThis.window;
  const fakeWindow: any = { location: { hash: "#today", search: "" }, history: { pushState: (_: any, __: any, hash: string) => { fakeWindow.location.hash = hash; } },
    addEventListener: noop, removeEventListener: noop, setTimeout, clearTimeout };
  globalThis.window = fakeWindow;
  let snapshotReads = 0;
  let writeCalls = 0;
  api.status = async () => data.status;
  api.candidates = async (snapshotId?: string) => ({ snapshotId: snapshotId ?? "decision-1", candidates: [], note: "test" } as any);
  api.events = async () => ({ snapshotId: "decision-1", events: [] } as any);
  api.snapshots = async () => { snapshotReads++; throw new Error("历史目录暂不可用"); };
  api.plans = async () => ({ plans: [] } as any);
  api.watchlist = async () => ({ items: [] } as any);
  api.researchSimulationDrafts = async () => ({ drafts: [] });
  api.decisions = async () => ({ decisions: [] } as any);
  api.researchSimulationContext = async () => context;
  api.researchCandidates = async () => ({ schema: "aquant.research-candidates.v1", strategy: "S2_RESEARCH", pitMode: "HISTORICAL_RECONSTRUCTED",
    researchOnly: true, simulationAvailable: false, snapshotId: "research-1", dataAsOf: "2026-10-12", generatedAt: "2026-10-12T19:00:00+08:00",
    poolSize: 0, rankedCount: 0, excludedCount: 0, exclusions: [], limitations: [], source: { factorReport: "test", archiveVerified: true }, candidates: [] } as any);
  api.preview = async () => { writeCalls++; throw new Error("unexpected preview"); };
  api.requestConfirmation = async () => { writeCalls++; throw new Error("unexpected confirmation"); };
  api.freeze = async () => { writeCalls++; throw new Error("unexpected freeze"); };
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<App />); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.match(text(renderer), /今天从这里开始/);
    await act(async () => {
      renderer.root.findByProps({ "aria-label": "主要任务" }).findAllByType("button")
        .find(item => item.children.join("") === "模拟组合")!.props.onClick();
      await new Promise(resolve => setTimeout(resolve, 0));
    });
    await act(async () => { button(renderer, "采用或调整规则方案").props.onClick(); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.equal(snapshotReads, 2, "draft recovery and baseline history are each isolated from initial page loading");
    assert.match(text(renderer), /历史目录暂不可用/);
    assert.ok(button(renderer, "重读快照历史"));
    assert.equal(button(renderer, "计算调整预览").props.disabled, true);
    assert.equal(button(renderer, "确认并冻结计划").props.disabled, true);
    assert.equal(writeCalls, 0);
    await act(async () => {
      renderer.root.findByProps({ "aria-label": "主要任务" }).findAllByType("button")
        .find(item => item.children.join("") === "研究")!.props.onClick();
      await new Promise(resolve => setTimeout(resolve, 0));
    });
    assert.ok(renderer.root.findByProps({ id: "research-candidates-search" }));
    assert.equal(writeCalls, 0);
  } finally {
    await act(async () => { renderer?.unmount(); });
    keys.forEach(key => { (api as any)[key] = originals[key]; });
    globalThis.window = oldWindow;
  }
});

test("schedule settings show no assumed values while loading and retry after a read error", async () => {
  const originals = { schedule: api.schedule, saveSchedule: api.saveSchedule, runPipelineNow: api.runPipelineNow };
  let rejectSchedule!: (reason: Error) => void;
  let writeCalls = 0;
  let submittedBody: any;
  api.schedule = () => new Promise((resolve, reject) => { rejectSchedule = reject; });
  api.saveSchedule = async body => {
    writeCalls++;
    submittedBody = body;
    return { ...scheduleStatus, schedule: { ...scheduleStatus.schedule, enabled: false, runAtLocal: "19:45" } };
  };
  api.runPipelineNow = async () => { writeCalls++; return { ...scheduleStatus, requestId: "request-1", note: "test" } as any; };
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<SettingsView />); await Promise.resolve(); });
    assert.match(text(renderer), /正在读取每日任务配置和运行状态/);
    assert.equal(renderer.root.findAllByType("input").length, 0, "editable defaults are not shown during the first read");
    assert.equal(renderer.root.findAllByType("button").length, 0, "no save or run action is available during loading");
    await act(async () => { rejectSchedule(new Error("调度服务暂不可用")); await Promise.resolve(); });
    assert.match(text(renderer), /无法读取每日任务状态/);
    assert.match(text(renderer), /调度服务暂不可用/);
    assert.equal(renderer.root.findAllByType("button").length, 1, "read failure offers only a retry");
    assert.equal(writeCalls, 0);

    api.schedule = async () => scheduleStatus;
    await act(async () => { button(renderer, "重新读取").props.onClick(); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.match(text(renderer), /已启用/);
    assert.equal(renderer.root.findAllByType("input").find(item => item.props.type === "checkbox")!.props.checked, true);
    assert.ok(button(renderer, "保存设置"));
    assert.equal(writeCalls, 0, "retry reads server state without submitting changes");
    const timeInput = renderer.root.findAllByType("input").find(item => item.props.type === "time")!;
    await act(async () => { timeInput.props.onChange({ target: { value: "18:15" } }); });
    await act(async () => { button(renderer, "保存设置").props.onClick(); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.equal(writeCalls, 1);
    assert.deepEqual(Object.keys(submittedBody).sort(), ["dataDir", "enabled", "interpreter", "runAtLocal", "weekdaysOnly", "windowStart"]);
    assert.equal(renderer.root.findAllByType("input").find(item => item.props.type === "time")!.props.value, "19:45",
      "the saved server value replaces the local draft");
    assert.match(text(renderer), /已停用/);
  } finally {
    await act(async () => { renderer?.unmount(); });
    api.schedule = originals.schedule; api.saveSchedule = originals.saveSchedule; api.runPipelineNow = originals.runPipelineNow;
  }
});

test("background plan recovery keeps its exact decision and execution snapshot bindings", async () => {
  const keys = ["status", "candidates", "events", "snapshots", "plans", "watchlist", "researchSimulationDrafts",
    "decisions", "researchSimulationContext", "ledger"] as const;
  const originals = Object.fromEntries(keys.map(key => [key, api[key]]));
  const oldWindow = globalThis.window;
  const fakeWindow: any = { location: { hash: "#today", search: "" }, history: { pushState: (_: any, __: any, hash: string) => { fakeWindow.location.hash = hash; } },
    addEventListener: noop, removeEventListener: noop, setTimeout, clearTimeout };
  globalThis.window = fakeWindow;
  const persisted = { planId: "persisted-plan", portfolioId: "pf-user-sim", status: "FROZEN",
    snapshotId: "decision-old", decisionSnapshotId: "decision-old", decisionCutoffAt: "2026-10-12T16:00:00+08:00",
    executionSnapshotId: "execution-old", executionCutoffAt: "2026-10-13T15:00:00+08:00", tradingDay: "2026-10-13",
    createdAt: "2026-10-12T19:00:00+08:00", frozenAt: "2026-10-12T20:00:00+08:00", expiresAt: null,
    confirmedBy: "user", planVersion: "plan-v1" } as any;
  let resolveFirstPlanRead!: (value: any) => void;
  let planReads = 0;
  api.status = async () => data.status;
  api.candidates = async (snapshotId?: string) => ({ snapshotId: snapshotId ?? "decision-1", candidates: [], note: "test" } as any);
  api.events = async () => ({ snapshotId: "decision-1", events: [] } as any);
  api.snapshots = async () => ({ snapshots: [] } as any);
  api.plans = () => {
    planReads++;
    return planReads === 1
      ? new Promise(resolve => { resolveFirstPlanRead = resolve; }) as any
      : Promise.resolve({ plans: [persisted] } as any);
  };
  api.watchlist = async () => ({ items: [] } as any);
  api.researchSimulationDrafts = async () => ({ drafts: [] });
  api.decisions = async () => ({ decisions: [] } as any);
  api.researchSimulationContext = async () => context;
  api.ledger = async () => ({ portfolio_id: "pf-user-sim", cash: { cents: 1000000 }, positions: [], opened_at: "2026-10-12" } as any);
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<App />); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.match(text(renderer), /今天从这里开始/, "slow plan history does not hold the primary page");
    assert.equal(planReads, 1);
    await act(async () => { resolveFirstPlanRead({ plans: [persisted] }); await new Promise(resolve => setTimeout(resolve, 0)); });
    await act(async () => {
      renderer.root.findByProps({ "aria-label": "主要任务" }).findAllByType("button")
        .find(item => item.children.join("") === "模拟组合")!.props.onClick();
      await new Promise(resolve => setTimeout(resolve, 0));
    });
    const portfolio = renderer.root.findByType(PortfolioView);
    assert.equal(portfolio.props.frozenPlanId, "persisted-plan");
    assert.equal(portfolio.props.persistedPlanStatus, "FROZEN");
    assert.deepEqual(portfolio.props.timing, {
      decisionSnapshotId: "decision-old", decisionCutoffAt: "2026-10-12T16:00:00+08:00",
      executionSnapshotId: "execution-old", executionCutoffAt: "2026-10-13T15:00:00+08:00",
      tradingDay: "2026-10-13",
    });
  } finally {
    await act(async () => { renderer?.unmount(); });
    keys.forEach(key => { (api as any)[key] = originals[key]; });
    globalThis.window = oldWindow;
  }
});

test("retrospective engineering records load only after the fold is first opened", async () => {
  const keys = ["experiments", "decisions", "researchSimulationDrafts", "engineeringSimulationRuns",
    "forwardComparisons", "engineeringAttributions", "comparisons"] as const;
  const originals = Object.fromEntries(keys.map(key => [key, api[key]]));
  const reads = { runs: 0, forward: 0, attributions: 0, comparisons: 0 };
  api.experiments = async () => ({ experiments: [] } as any);
  api.decisions = async () => ({ decisions: [] } as any);
  api.researchSimulationDrafts = async () => ({ drafts: [] });
  api.engineeringSimulationRuns = async () => { reads.runs++; return { runs: [] }; };
  api.forwardComparisons = async () => { reads.forward++; return { series: [] }; };
  api.engineeringAttributions = async () => { reads.attributions++; return { reports: [] }; };
  api.comparisons = async () => { reads.comparisons++; return { results: [] }; };
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<ExperimentsApiView />); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.deepEqual(reads, { runs: 0, forward: 0, attributions: 0, comparisons: 0 });
    const fold = renderer.root.findAllByType("details").find(item =>
      item.findByType("summary").children.join("").includes("展开对照与工程验证"))!;
    await act(async () => { fold.props.onToggle({ currentTarget: { open: true } }); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.deepEqual(reads, { runs: 1, forward: 1, attributions: 1, comparisons: 1 });
    await act(async () => { fold.props.onToggle({ currentTarget: { open: false } }); });
    await act(async () => { fold.props.onToggle({ currentTarget: { open: true } }); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.deepEqual(reads, { runs: 1, forward: 1, attributions: 1, comparisons: 1 },
      "collapsing and reopening keeps the loaded panels mounted");
  } finally {
    await act(async () => { renderer?.unmount(); });
    keys.forEach(key => { (api as any)[key] = originals[key]; });
  }
});

test("workspace read failures stay local and each initial read and research card can retry", async () => {
  const keys = ["watchlist", "decisions", "experiments", "strategyVersions", "research"] as const;
  const originals = Object.fromEntries(keys.map(key => [key, api[key]]));
  const reads = { watchlist: 0, decisions: 0, experiments: 0, strategies: 0, research: 0 };
  api.watchlist = async () => {
    reads.watchlist++;
    if (reads.watchlist === 1) throw new Error("自选暂不可用");
    return { items: [{ instrument_id: "SH.600001", added_at: "2026-10-31T00:00:00Z", note: null,
      short_name: "测试证券", exchange: "SSE", board: "MAIN" }] } as any;
  };
  api.decisions = async () => { reads.decisions++; if (reads.decisions === 1) throw new Error("决策日志暂不可用"); return { decisions: [] } as any; };
  api.experiments = async () => { reads.experiments++; if (reads.experiments === 1) throw new Error("实验登记暂不可用"); return { experiments: [] } as any; };
  api.strategyVersions = async () => {
    reads.strategies++;
    if (reads.strategies === 1) throw new Error("策略能力暂不可用");
    return { strategyVersions: [], familyGates: [], featureVersions: [], note: "" } as any;
  };
  api.research = async () => {
    reads.research++;
    if (reads.research === 1) throw new Error("研究卡暂不可用");
    return { instrumentId: "SH.600001", displayName: "测试研究卡", exchange: "SSE", board: "MAIN",
      snapshotId: "snapshot-1", asOfTime: "2026-10-31T16:00:00+08:00", generatedAt: "2026-10-31T19:00:00+08:00",
      dataCompleteness: "COMPLETE", timeLabel: "历史研究", rankSemantics: "横截面排名",
      rankBreakdown: [], comparisonScope: "同一快照", evidence: [], counterEvidence: [], uncertainties: [], actions: [],
      tradability: { simulatable: false, reason: "NOT_READY", reasonLabel: "不可进入模拟池", detail: "测试资格限制",
        rule: "test", effectiveFrom: null, repair: null }, limitations: [] } as any;
  };
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<WorkspaceView apiUp={true} portfolioId="pf-user-sim" tradingDay="2026-10-31" />);
      await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.equal(renderer.root.findAllByType("button").filter(item => item.children.join("") === "重试读取").length, 4);
    assert.match(text(renderer), /自选读取失败/);
    assert.match(text(renderer), /决策日志读取失败/);
    assert.match(text(renderer), /实验登记读取失败/);
    assert.match(text(renderer), /策略能力读取失败/);
    assert.doesNotMatch(text(renderer), /自选为空|还没有决策记录|还没有登记的实验/,
      "a failed request cannot be mistaken for an empty saved list");
    assert.doesNotMatch(text(renderer), /有一步没有完成/, "read failures do not replace one another in the global mutation banner");

    await act(async () => {
      for (const retry of renderer.root.findAllByType("button").filter(item => item.children.join("") === "重试读取")) {
        retry.props.onClick();
      }
      await new Promise(resolve => setTimeout(resolve, 0));
    });
    assert.deepEqual(reads, { watchlist: 2, decisions: 2, experiments: 2, strategies: 2, research: 0 });
    assert.match(text(renderer), /测试证券/);
    assert.doesNotMatch(text(renderer), /自选读取失败/);
    assert.match(text(renderer), /还没有决策记录/);
    assert.match(text(renderer), /还没有登记的实验/);

    await act(async () => { button(renderer, "研究卡").props.onClick(); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.match(text(renderer), /研究卡读取失败：研究卡暂不可用/);
    assert.doesNotMatch(text(renderer), /有一步没有完成/);
    await act(async () => { button(renderer, "重试读取").props.onClick(); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.equal(reads.research, 2);
    assert.match(text(renderer), /测试研究卡/);
  } finally {
    await act(async () => { renderer?.unmount(); });
    keys.forEach(key => { (api as any)[key] = originals[key]; });
  }
});

test("status drawer keeps the full fee source label in a narrow-layout wrapper", async () => {
  const keys = ["fees", "readiness"] as const;
  const originals = Object.fromEntries(keys.map(key => [key, api[key]]));
  api.fees = async () => ({ snapshotId: "snapshot-1", feeVersion: "fees-v1", syntheticTestRate: false,
    commissionSource: "USER_APPROVED_ASSUMPTION", commissionRate: "0.0003", commissionMinCents: 500,
    stampDutyRateSell: "0.0005", transferFeeRate: "0.00001", provenance: [], note: "" });
  api.readiness = async () => ({ trial: { ready: false, blockingIssues: [], operationalWarnings: [] } } as any);
  let renderer!: ReactTestRenderer;
  try {
    const drawerStatus = { ...data.status, kind: "daily", publishedAt: null, dataMode: "PRODUCTION",
      qualityStatus: "OK", datasetSummary: [], watermark: null, freshness: null };
    await act(async () => { renderer = create(<StatusDrawer status={drawerStatus as any} onClose={noop} />);
      await new Promise(resolve => setTimeout(resolve, 0)); });
    const feeList = renderer.root.findAllByType("dl").find(item => item.props.className === "kv fee-source-kv");
    assert.ok(feeList, "fee labels use the scoped narrow-layout grid");
    assert.match(text(renderer), /USER_APPROVED_ASSUMPTION · 已批准试运行假设/);
  } finally {
    await act(async () => { renderer?.unmount(); });
    keys.forEach(key => { (api as any)[key] = originals[key]; });
  }
});

test("research assistant reports disabled readiness and only sends fixed questions", async () => {
  const originals = { readiness: api.assistantReadiness, message: api.assistantMessage };
  let sent: unknown = null;
  api.assistantReadiness = async () => ({ enabled: false, mode: "DISABLED",
    blockingIssues: [{ code: "ASSISTANT_BUDGET_CONFIG_MISSING", message: "有效价格或每日费用上限尚未配置",
      repairAction: "配置所选模型的有效价格和每日上限" }] });
  api.assistantMessage = async (body) => {
    sent = body;
    return { modelCallId: "mc-test", text: "材料记录每股现金红利为 0.50 元。",
      citationIds: ["cite-1"], citations: [{ citationId: "cite-1", documentId: "doc-1",
        documentHash: "sha256:doc", quoteHash: "sha256:quote", sourceId: "cninfo",
        availableAt: "2026-09-07T15:00:00+00:00", locatorKind: "EXACT",
        locatorStart: 0, locatorEnd: 10 }],
      basedOn: { snapshotId: "snapshot-1", dataMode: "PRODUCTION", asOfTime: "2026-09-08T16:00:00+08:00" },
      budget: { status: "SETTLED", reservedUsageCostUpperBoundMicroCny: 200,
        usageCostUpperBoundMicroCny: 16, dailyCapMicroCny: 1000000,
        priceVersion: "unit-test-only", costBasis: "REGISTERED_PRICE_UPPER_BOUND",
        reservationExceeded: false },
      note: "" };
  };
  const card = { instrumentId: "SH.600519", displayName: "测试证券", exchange: "SSE", board: "MAIN",
    snapshotId: "snapshot-1", asOfTime: "2026-09-08T16:00:00+08:00", generatedAt: "2026-09-08T17:00:00+08:00",
    cardId: "card-1", dataCompleteness: "完整", timeLabel: "真实数据", rankSemantics: "横截面排名",
    rankBreakdown: [], comparisonScope: "当前快照", evidence: [{ citationId: "cite-1", documentId: "doc-1",
      statement: "每股现金红利 0.50 元", quote: "A股每股现金红利0.50元", located: true,
      availableAt: "2026-09-07T15:00:00+00:00", locatorKind: "EXACT" }],
    counterEvidence: [], uncertainties: [], actions: [], limitations: [],
    tradability: { simulatable: true, reason: null, reasonLabel: "可模拟", detail: "", rule: "", effectiveFrom: null, repair: null } } as any;
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<ResearchCard card={card} allowAssistant={true}
      onOpenEvidence={noop} onAction={noop} />); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.match(text(renderer), /受限研究助手/);
    assert.match(text(renderer), /有效价格或每日费用上限尚未配置/);
    assert.equal(button(renderer, "询问所选依据").props.disabled, true);
    assert.equal(sent, null);
    await act(async () => { renderer.unmount(); });

    api.assistantReadiness = async () => ({ enabled: true, mode: "LIVE", blockingIssues: [],
      budget: { configured: true, model: "unit-test-model", priceVersion: "unit-test-only",
        priceSource: "https://www.deepseek.com/pricing", priceEffectiveFrom: "2026-01-01",
        costBasis: "REGISTERED_PRICE_UPPER_BOUND",
        accountingNote: "按登记的价格上界单价估算预算占用，不代表供应商账单实扣金额",
        dailyCapMicroCny: 1000000, usage: { status: "AVAILABLE", budgetDay: "2026-11-02",
          usageCostUpperBoundMicroCny: 20, remainingBudgetMicroCny: 999980,
          dailyCapMicroCny: 1000000, unaccountedLegacyCalls: 0, currency: "CNY",
          costBasis: "REGISTERED_PRICE_UPPER_BOUND" } } });
    await act(async () => { renderer = create(<ResearchCard card={card} allowAssistant={true}
      onOpenEvidence={noop} onAction={noop} />); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.match(text(renderer), /概括所选依据/);
    assert.match(text(renderer), /今日预算占用上界/);
    assert.match(text(renderer), /不代表账单实扣金额/);
    assert.match(text(renderer), /unit-test-only/);
    assert.doesNotMatch(text(renderer), /textarea|请输入问题/);
    const evidenceCheck = renderer.root.findAllByType("input")[0];
    const consentCheck = renderer.root.findAllByType("input")[1];
    await act(async () => { evidenceCheck.props.onChange({ target: { checked: true } }); });
    await act(async () => { consentCheck.props.onChange({ target: { checked: true } }); });
    await act(async () => { button(renderer, "询问所选依据").props.onClick(); await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.deepEqual(sent, { card_id: "card-1", question_id: "EVIDENCE_SUMMARY",
      citation_ids: ["cite-1"], confirm_external_send: true });
    assert.match(text(renderer), /材料记录每股现金红利为 0.50 元/);
    assert.match(text(renderer), /¥0\.000016/);
    assert.ok(button(renderer, "查看依据 cite-1"), "verified citation links back to card evidence");
  } finally {
    await act(async () => { renderer?.unmount(); });
    api.assistantReadiness = originals.readiness;
    api.assistantMessage = originals.message;
  }
});

test("research assistant readiness can be retried without sending materials", async () => {
  const originals = { readiness: api.assistantReadiness, message: api.assistantMessage };
  let reads = 0;
  let sends = 0;
  api.assistantReadiness = async () => {
    reads += 1;
    if (reads === 1) throw new Error("状态服务暂时不可用");
    return { enabled: false, mode: "DISABLED", blockingIssues: [] };
  };
  api.assistantMessage = async () => { sends += 1; throw new Error("should not send"); };
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => {
      renderer = create(<ResearchAssistantPanel binding={{ kind: "ARCHIVED_CARD", cardId: "card-a" }}
        options={[{ selectionId: "cite-a", label: "公开公告", quote: "公开短摘录", eligible: true }]} />);
      await new Promise(resolve => setTimeout(resolve, 0));
    });
    assert.match(text(renderer), /状态服务暂时不可用/);
    assert.match(text(renderer), /未发送任何材料/);
    await act(async () => {
      button(renderer, "重试读取助手状态").props.onClick();
      await new Promise(resolve => setTimeout(resolve, 0));
    });
    assert.equal(reads, 2);
    assert.equal(sends, 0);
    assert.match(text(renderer), /助手暂不可用/);
  } finally {
    await act(async () => { renderer?.unmount(); });
    api.assistantReadiness = originals.readiness;
    api.assistantMessage = originals.message;
  }
});

test("research assistant ignores an answer after its research binding changes", async () => {
  const originals = { readiness: api.assistantReadiness, message: api.assistantMessage };
  api.assistantReadiness = async () => ({ enabled: true, mode: "LIVE", blockingIssues: [] });
  let resolveMessage!: (answer: any) => void;
  api.assistantMessage = async () => new Promise((resolve) => { resolveMessage = resolve; });
  let renderer!: ReactTestRenderer;
  const options = [{ selectionId: "cite-a", label: "公开公告", quote: "公开短摘录", eligible: true }];
  try {
    await act(async () => {
      renderer = create(<ResearchAssistantPanel binding={{ kind: "ARCHIVED_CARD", cardId: "card-a" }}
        options={options} />);
      await new Promise(resolve => setTimeout(resolve, 0));
    });
    const evidenceCheck = renderer.root.findAllByType("input")
      .find((input) => input.props.type === "checkbox")!;
    const consentCheck = renderer.root.findAllByType("input")
      .find((input) => input.props.type === "checkbox" && input !== evidenceCheck)!;
    await act(async () => { evidenceCheck.props.onChange({ target: { checked: true } }); });
    await act(async () => { consentCheck.props.onChange({ target: { checked: true } }); });
    await act(async () => { button(renderer, "询问所选依据").props.onClick(); });

    await act(async () => {
      renderer.update(<ResearchAssistantPanel binding={{ kind: "ARCHIVED_CARD", cardId: "card-b" }}
        options={options} />);
      await new Promise(resolve => setTimeout(resolve, 0));
    });
    await act(async () => {
      resolveMessage({ text: "旧卡回答不应显示", citations: [], basedOn: { snapshotId: "old" } });
      await new Promise(resolve => setTimeout(resolve, 0));
    });
    assert.doesNotMatch(text(renderer), /旧卡回答不应显示/);
    assert.equal(button(renderer, "询问所选依据").props.disabled, true);
  } finally {
    await act(async () => { renderer?.unmount(); });
    api.assistantReadiness = originals.readiness;
    api.assistantMessage = originals.message;
  }
});

test("status and evidence drawers keep keyboard focus inside and restore the opener", async () => {
  const originalStatusReads = { fees: api.fees, readiness: api.readiness };
  api.fees = async () => ({}) as any;
  api.readiness = async () => ({ trial: { ready: false, blockingIssues: [], operationalWarnings: [] } }) as any;
  const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  const makeFocusTarget = (document: any) => ({
    isConnected: true,
    getAttribute() { return null; },
    focus() { document.activeElement = this; },
  });
  let keydownHandler: ((event: any) => void) | null = null;
  const fakeDocument: any = {
    activeElement: null,
    addEventListener(type: string, listener: (event: any) => void) {
      if (type === "keydown") keydownHandler = listener;
    },
    removeEventListener(type: string, listener: (event: any) => void) {
      if (type === "keydown" && keydownHandler === listener) keydownHandler = null;
    },
  };
  const opener = makeFocusTarget(fakeDocument);
  fakeDocument.activeElement = opener;
  Object.defineProperty(globalThis, "document", { configurable: true, writable: true, value: fakeDocument });

  const card = { displayName: "测试证券", instrumentId: "SH.600519", snapshotId: "snapshot-1",
    asOfTime: "2026-10-30T16:00:00+08:00", evidence: [], counterEvidence: [], rankBreakdown: [] } as any;
  const status = { ...data.status, kind: "daily", publishedAt: null, dataMode: "PRODUCTION",
    qualityStatus: "OK", datasetSummary: [], watermark: null, freshness: null, blockingIssues: [] } as any;
  const drawers = [
    (onClose: () => void) => <StatusDrawer status={status} onClose={onClose} />,
    (onClose: () => void) => <EvidenceDrawer card={card} onClose={onClose} />,
  ];
  let renderer!: ReactTestRenderer;
  try {
    for (const makeDrawer of drawers) {
      let closeCount = 0;
      const first = makeFocusTarget(fakeDocument);
      const last = makeFocusTarget(fakeDocument);
      const dialog: any = {
        ownerDocument: fakeDocument,
        focus() { fakeDocument.activeElement = this; },
        querySelectorAll() { return [first, last]; },
        contains(element: unknown) { return element === this || element === first || element === last; },
      };
      await act(async () => {
        renderer = create(makeDrawer(() => { closeCount += 1; }), {
          createNodeMock: (element) => element.type === "aside" ? dialog : {},
        });
      });
      assert.equal(fakeDocument.activeElement, first);
      assert.ok(keydownHandler);

      const forwardTab = { key: "Tab", shiftKey: false, prevented: false,
        preventDefault() { this.prevented = true; } };
      fakeDocument.activeElement = last;
      keydownHandler!(forwardTab);
      assert.equal(forwardTab.prevented, true);
      assert.equal(fakeDocument.activeElement, first);

      const backwardTab = { key: "Tab", shiftKey: true, prevented: false,
        preventDefault() { this.prevented = true; } };
      fakeDocument.activeElement = first;
      keydownHandler!(backwardTab);
      assert.equal(backwardTab.prevented, true);
      assert.equal(fakeDocument.activeElement, last);

      const escape = { key: "Escape", shiftKey: false, prevented: false,
        preventDefault() { this.prevented = true; } };
      keydownHandler!(escape);
      assert.equal(escape.prevented, true);
      assert.equal(closeCount, 1);

      await act(async () => { renderer.unmount(); });
      assert.equal(fakeDocument.activeElement, opener);
      assert.equal(keydownHandler, null);
    }
  } finally {
    await act(async () => { renderer?.unmount(); });
    api.fees = originalStatusReads.fees;
    api.readiness = originalStatusReads.readiness;
    if (originalDocument) Object.defineProperty(globalThis, "document", originalDocument);
    else delete (globalThis as any).document;
  }
});

test("current research candidate assistant sends only its verified scan receipt binding", async () => {
  const originals = { candidates: api.researchCandidates,
    readiness: api.assistantReadiness, message: api.assistantMessage };
  const priorWindow = (globalThis as any).window;
  (globalThis as any).window = { setTimeout, clearTimeout };
  const event = { title: "关于召开2026年第三季度业绩说明会的公告",
    publishedDate: "2026-10-20",
    url: "https://static.cninfo.com.cn/finalpage/2026-10-20/1225610070.PDF",
    quote: "会议召开时间：2026 年10 月27 日（周二）15:00-16:00\n 会议召开地点：上海证券交易所上证路演中心（网址：\nhttps://roadshow",
    page: 1, sourceId: "event-1225610070", verificationStatus: "ARCHIVED_PDF_TEXT",
    receiptId: "rcpt_8a3aa89a458c6337bde683cf",
    contentHash: "sha256:6909ada3f907114b87ff18b381a32702f16983c2329999138104fc120081ef10",
    startChar: 155, endChar: 235 };
  const scan = { schema: "aquant.research-candidates.v2", snapshotId: "research-closing-test",
    generatedAt: "2026-10-29T12:35:25.748685+00:00", dataAsOf: "2026-10-29",
    asOfTime: "2026-10-29T12:34:55.854556+00:00", strategy: "S2_RESEARCH",
    pitMode: "HISTORICAL_RECONSTRUCTED", researchOnly: true, simulationAvailable: false,
    poolSize: 1, rankedCount: 1, excludedCount: 0, returnedCount: 1,
    exclusions: [], limitations: [], rankingFormula: "测试", comparisonScope: "测试范围",
    quoteMode: "CLOSING_RESEARCH", filteredCount: 1, offset: 0, nextOffset: null,
    source: { factorReport: "factor", marketCapSource: "quotes",
      marketCapAsOf: "2026-10-29", archiveVerified: true, placeholderUsed: false },
    scope: { universeCodeCount: 1, observedCount: 1, quoteRejectedCount: 0,
      financialCount: 0, unknownIndustryCount: 0, byBoard: [] },
    candidates: [{ instrumentId: "SH.600415", code: "600415", displayName: "小商品城",
      exchange: "SSE", board: "MAIN", industry: "商贸零售", position: 1, signalRank: 0.5,
      factors: {}, factorRanks: {}, basis: [], risks: [], evidence: [], counterEvidence: [],
      uncertainties: [], events: [event], eventContext: { window: "2026-10-01 至 2026-10-29",
        queryComplete: true, documentCount: 2, observedAt: "2026-10-29T12:34:55+00:00",
        scope: "有限公告摘录", missing: null } }],
  } as any;
  let sent: unknown = null;
  api.researchCandidates = async () => scan;
  api.assistantReadiness = async () => ({ enabled: true, mode: "LIVE", blockingIssues: [],
    budget: { configured: true, model: "unit-test-model", priceVersion: "unit-test-only",
      priceSource: "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
      priceEffectiveFrom: "2026-10-01", dailyCapMicroCny: 100000,
      usage: { status: "AVAILABLE", budgetDay: "2026-11-02",
        usageCostUpperBoundMicroCny: 0, remainingBudgetMicroCny: 100000,
        dailyCapMicroCny: 100000, unaccountedLegacyCalls: 0,
        currency: "CNY", costBasis: "REGISTERED_PRICE_UPPER_BOUND" } } });
  api.assistantMessage = async (body) => {
    sent = body;
    return { modelCallId: "mc-scan-test", text: "所选公告说明了业绩说明会的时间与地点。",
      citationIds: ["scan-citation"], citations: [{ citationId: "scan-citation",
        announcementId: "1225610070", eventId: event.sourceId, receiptId: event.receiptId,
        documentHash: event.contentHash, quoteHash: "sha256:quote", sourceId: "cninfo",
        availableAt: event.publishedDate, locatorKind: "PDF_PAGE_CHARS", locatorStart: 155,
        locatorEnd: 235, page: 1, url: event.url, title: event.title }],
      basedOn: { snapshotId: "formal-snapshot", dataMode: "PRODUCTION",
        asOfTime: "2026-10-30T12:00:00+08:00" },
      researchBinding: { snapshotId: scan.snapshotId, asOfTime: scan.asOfTime,
        dataAsOf: scan.dataAsOf, instrumentId: "SH.600415" },
      budget: null, note: "" };
  };
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<ResearchCandidates />);
      await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.match(text(renderer), /受限研究助手/);
    assert.match(text(renderer), /会议召开时间/);
    const evidenceCheck = renderer.root.findAllByType("input")
      .find((item) => item.props.type === "checkbox")!;
    const consentCheck = renderer.root.findAllByType("input")
      .filter((item) => item.props.type === "checkbox")[1];
    await act(async () => { evidenceCheck.props.onChange({ target: { checked: true } }); });
    await act(async () => { consentCheck.props.onChange({ target: { checked: true } }); });
    await act(async () => { button(renderer, "询问所选依据").props.onClick();
      await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.deepEqual(sent, { question_id: "EVIDENCE_SUMMARY", confirm_external_send: true,
      snapshot_id: scan.snapshotId, instrument_id: "SH.600415",
      receipt_ids: [event.receiptId] });
    assert.match(text(renderer), /研究资料快照/);
    assert.match(text(renderer), /research-closing-test/);
    assert.ok(renderer.root.findAllByType("a").some((link) =>
      link.props.href === event.url), "verified citation links to the selected official PDF");
    await act(async () => { renderer.unmount(); });

    const empty = { ...scan, candidates: [{ ...scan.candidates[0], events: [] }] };
    api.researchCandidates = async () => empty;
    sent = null;
    await act(async () => { renderer = create(<ResearchCandidates />);
      await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.match(text(renderer), /没有符合条件的可定位公开短摘录/);
    assert.equal(button(renderer, "询问所选依据").props.disabled, true);
    assert.equal(sent, null);
  } finally {
    await act(async () => { renderer?.unmount(); });
    api.researchCandidates = originals.candidates;
    api.assistantReadiness = originals.readiness;
    api.assistantMessage = originals.message;
    if (priorWindow === undefined) delete (globalThis as any).window;
    else (globalThis as any).window = priorWindow;
  }
});
