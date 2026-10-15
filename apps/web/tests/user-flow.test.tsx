import assert from "node:assert/strict";
import test from "node:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { renderToStaticMarkup } from "react-dom/server";
import { api } from "../src/lib/api";
import { checkResearchSelection, restoreResearchDrafts, summarizePlan } from "../src/lib/workflow";
import { TodayView } from "../src/views/TodayView";
import { DraftPanel } from "../src/components/DraftPanel";
import { TopBar } from "../src/components/TopBar";
import { NoChangeDecision } from "../src/components/NoChangeDecision";
import { ResearchSimulationPanel } from "../src/components/ResearchSimulationPanel";
import { LedgerPanel } from "../src/components/LedgerPanel";
import { ProposalSummary } from "../src/components/DecisionJourney";
import App from "../src/App";
import { PortfolioView } from "../src/views/PortfolioView";
import type { Draft, WorkspaceData } from "../src/lib/types";
import type { PreviewResponse, ResearchSimulationContext } from "../src/lib/api";

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
function text(renderer: ReactTestRenderer) { return JSON.stringify(renderer.toJSON()); }
function button(renderer: ReactTestRenderer, label: string) {
  return renderer.root.findAllByType("button").find(item => item.children.join("").includes(label))!;
}

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
    "researchSimulationContext", "researchCandidates", "financialCoverage", "ledger"] as const;
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
  let coverageReads = 0;
  api.financialCoverage = async () => { coverageReads++; return null as any; };
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
    assert.equal(coverageReads, 0, "folded diagnostic does not start a background financial verification");
    await act(async () => { renderer.root.findByProps({ id: "research-candidates-search" }).props.onChange({ target: { value: "测试" } }); await new Promise(resolve => setTimeout(resolve, 370)); });
    await act(async () => { button(renderer, "检查模拟资格").props.onClick(); });
    assert.equal(fakeWindow.location.hash, "#portfolio");
    assert.equal(renderer.root.findByType(PortfolioView).props.mode, "research");
    assert.equal(renderer.root.findAllByType("input").find(item => item.props["aria-label"] === "选择 测试证券")!.props.checked, true);
    await act(async () => { renderer.root.findByProps({ "aria-label": "主要任务" }).findAllByType("button").find(item => item.children.join("") === "研究")!.props.onClick(); });
    const coverageDetails = renderer.root.findAllByType("details").find(item => item.findAllByType("summary")[0]?.children.join("").includes("财务数据覆盖"))!;
    await act(async () => { coverageDetails.props.onToggle({ currentTarget: { open: true } }); });
    assert.equal(coverageReads, 1);
    await act(async () => { coverageDetails.props.onToggle({ currentTarget: { open: false } }); coverageDetails.props.onToggle({ currentTarget: { open: true } }); });
    assert.equal(coverageReads, 1, "closing and reopening keeps the already loaded diagnostic");
    await act(async () => { renderer.root.findByProps({ "aria-label": "主要任务" }).findAllByType("button").find(item => item.children.join("") === "研究")!.props.onClick(); });
    assert.equal(renderer.root.findByProps({ id: "research-candidates-search" }).props.value, "测试");
    await act(async () => { renderer.root.findByProps({ "aria-label": "主要任务" }).findAllByType("button").find(item => item.children.join("") === "模拟组合")!.props.onClick(); });
    assert.equal(renderer.root.findAllByType("input").find(item => item.props["aria-label"] === "选择 测试证券")!.props.checked, true);
  } finally {
    await act(async () => { renderer?.unmount(); });
    keys.forEach(key => { (api as any)[key] = originals[key]; });
    globalThis.window = oldWindow;
  }
});
