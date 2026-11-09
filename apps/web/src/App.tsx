import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  DataStatus, Draft, ResearchCard, TodayChange, WorkspaceData,
} from "./lib/types";
import { formatRelative } from "./lib/format";
import { TopBar, StatusDrawer, type Tab } from "./components/TopBar";
import {
  api, AquantApiError, type CandidateResponseRow, type EventResponseRow,
  type PlanTimingSelection, type PreviewResponse, type PublishedSnapshot,
} from "./lib/api";
import { Callout, ErrorState, Loading } from "./components/ui";
import { EvidenceDrawer } from "./components/ResearchCard";
import { TodayView } from "./views/TodayView";
import { ResearchView } from "./views/ResearchView";
import { PortfolioView, type PortfolioMode } from "./views/PortfolioView";
import { ExperimentsView } from "./views/ExperimentsView";
import { WorkspaceView } from "./views/WorkspaceView";
import { SettingsView } from "./views/SettingsView";
import type { ResearchSimulationDraft } from "./lib/api";
import { restoreResearchDrafts, type ResearchSelection } from "./lib/workflow";

const WS_URL = "/workspace.json";
const TAB_IDS: Tab[] = ["today", "research", "portfolio", "workspace",
                        "experiments", "settings"];

function tabFromHash(): Tab {
  const h = window.location.hash.replace(/^#\/?/, "").split("?")[0] ?? "";
  return (TAB_IDS as string[]).includes(h) ? (h as Tab) : "today";
}

/** 夹具只能通过显式 URL 开关启用。API 失败不会静默回退到夹具。 */
function isDemoMode(): boolean {
  return new URLSearchParams(window.location.search).get("mode") === "demo";
}

function configuredPortfolioId(): string {
  const value = (window as Window & { __AQUANT_PORTFOLIO_ID__?: unknown })
    .__AQUANT_PORTFOLIO_ID__;
  return typeof value === "string" && value.trim() ? value.trim() : "pf-user-sim";
}

function tradingDayFromStatus(status: DataStatus): string {
  return status.freshness?.snapshotDay ?? status.asOfTime.slice(0, 10);
}

function emptyApiDraft(status: DataStatus, portfolioId: string): Draft {
  return {
    planId: "", portfolioId, tradingDay: tradingDayFromStatus(status), frozen: false,
    frozenLabel: "尚未请求服务端预览", orders: [], estimatedFees: "—",
    cashBefore: "—", cashAfter: "—", cashAfterCents: 0, buyTotal: "—", sellTotal: "—",
    industryCapPct: "—", industry: [], excluded: [], ruleChecks: [],
    confirmAction: {
      id: "plan.freeze", label: "确认并冻结计划",
      requirement: "需要人类用户显式确认；确认主体不能是模型",
      revalidate: ["计划版本", "快照版本", "账户状态版本", "确认主体", "有效期"],
    },
  };
}

function timingFrom(
  decision: PublishedSnapshot | undefined,
  execution: PublishedSnapshot | undefined,
): PlanTimingSelection | null {
  if (!decision || !execution || decision.snapshotId === execution.snapshotId) return null;
  if (!decision.capabilities.s1Decision.available) return null;
  if (!decision.tradingDay || !execution.tradingDay) return null;
  if (decision.dataMode !== execution.dataMode) return null;
  if (decision.asOfTime >= execution.asOfTime || decision.tradingDay >= execution.tradingDay) {
    return null;
  }
  if (!decision.publishedAt) return null;
  const publishedAt = Date.parse(decision.publishedAt);
  const executionOpen = Date.parse(execution.tradingDay + "T09:30:00+08:00");
  if (!Number.isFinite(publishedAt) || !Number.isFinite(executionOpen) ||
      publishedAt >= executionOpen) {
    return null;
  }
  return {
    decisionSnapshotId: decision.snapshotId,
    decisionCutoffAt: decision.asOfTime,
    executionSnapshotId: execution.snapshotId,
    executionCutoffAt: execution.asOfTime,
    tradingDay: execution.tradingDay,
  };
}

function defaultProductionTiming(
  snapshots: PublishedSnapshot[], currentSnapshotId: string,
): PlanTimingSelection | null {
  const ordered = [...snapshots].sort((a, b) => a.asOfTime.localeCompare(b.asOfTime));
  const execution = ordered.find((item) => item.snapshotId === currentSnapshotId) ??
    ordered.at(-1);
  if (!execution || !execution.tradingDay) return null;
  const executionDay = execution.tradingDay;
  const decision = ordered.filter(
    (item) => item.dataMode === execution.dataMode &&
      item.capabilities.s1Decision.available &&
      item.snapshotId !== execution.snapshotId &&
      item.asOfTime < execution.asOfTime && item.tradingDay !== null &&
      item.tradingDay < executionDay,
  ).at(-1);
  return timingFrom(decision, execution);
}

function mapCandidate(row: CandidateResponseRow, status: DataStatus, note: string) {
  return {
    instrumentId: row.instrumentId,
    displayName: row.displayName ?? row.instrumentId,
    signalRank: row.signalRank,
    industryCode: row.industryCode ?? "—",
    basis: note || "服务端 S1 信号",
    counterEvidence: "打开研究卡查看反证与限制",
    dataQuality: status.qualityStatus,
    simulatable: row.simulatable,
    simulatableLabel: row.simulatable ? "可模拟" : "不可模拟",
    lastClose: "—",
  };
}

function mapEvent(row: EventResponseRow): TodayChange {
  return {
    eventId: row.event_id,
    category: row.category,
    summary: row.summary,
    availableAt: row.available_at,
    verification: row.verification_status,
    direction: row.market_direction ?? "UNKNOWN",
    subjects: row.subjects
      .filter((s) => s.subject_type === "INSTRUMENT")
      .map((s) => s.subject_id),
  };
}

function buildApiWorkspace(
  status: DataStatus,
  candidates: CandidateResponseRow[],
  candidateNote: string,
  events: EventResponseRow[],
): WorkspaceData {
  const portfolioId = configuredPortfolioId();
  return {
    generatedAt: new Date().toISOString(), generator: "api",
    note: candidateNote || "候选与状态来自工作台 API", dataSource: "api", status,
    candidates: candidates.map((c) => mapCandidate(c, status, candidateNote)),
    // 研究卡按需读取，避免启动时对整个候选池发起一串请求。
    researchCards: [],
    draft: emptyApiDraft(status, portfolioId),
    todayChanges: events.map(mapEvent),
    portfolios: [{
      portfolioId, kind: "SIMULATED", label: "用户模拟账户", netValue: "—", cash: "—",
      positions: 0,
      note: "余额与持仓由服务端账本读取；请求预览后才创建或读取账户。",
    }],
    experiments: [], decisionLog: [],
  };
}

function fallbackStatus(): DataStatus {
  return {
    snapshotId: "—", kind: "—", asOfTime: new Date().toISOString(),
    publishedAt: null, dataMode: "—", watermark: null, qualityStatus: "—",
    readiness: "PARTIAL", readinessLabel: "载入中", blockingIssues: [],
    datasetSummary: [], timeLabel: "—", accountLabel: "模拟账户", freshness: null,
  };
}

type LegacyS1State =
  | { kind: "idle" | "loading"; snapshotId: string | null }
  | { kind: "ready"; snapshotId: string; workspace: WorkspaceData }
  | { kind: "error"; snapshotId: string; message: string };

export default function App() {
  const demoMode = isDemoMode();
  const [state, setState] = useState<
    { kind: "loading" } | { kind: "ready"; data: WorkspaceData } |
    { kind: "error"; message: string }
  >({ kind: "loading" });
  const [tab, setTabState] = useState<Tab>(tabFromHash);
  const [visitedTabs, setVisitedTabs] = useState<Tab[]>(() => [tabFromHash()]);
  const [portfolioMode, setPortfolioMode] = useState<PortfolioMode>("research");
  const [researchSelection, setResearchSelection] = useState<ResearchSelection | null>(null);
  const [focusedInstrumentId, setFocusedInstrumentId] = useState<string | null>(null);
  const [researchDrafts, setResearchDrafts] = useState<ResearchSimulationDraft[]>([]);
  const [draftReadError, setDraftReadError] = useState<string | null>(null);
  const [watchIds, setWatchIds] = useState<string[]>([]);
  const [reviewRevision, setReviewRevision] = useState(0);
  const [statusOpen, setStatusOpen] = useState(false);
  const [evidenceFor, setEvidenceFor] = useState<string | null>(null);
  const [selectedCard, setSelectedCard] = useState<string | null>(null);
  const [researchLoadingId, setResearchLoadingId] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [confirmResult, setConfirmResult] = useState<{ ok: boolean; message: string } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [livePreview, setLivePreview] = useState<PreviewResponse | null>(null);
  const [apiUp, setApiUp] = useState<boolean | null>(null);
  const [frozenPlanId, setFrozenPlanId] = useState<string | null>(null);
  const [persistedPlanStatus, setPersistedPlanStatus] = useState<"FROZEN" | "EXECUTED" | null>(null);
  const [snapshots, setSnapshots] = useState<PublishedSnapshot[]>([]);
  const [snapshotCatalogState, setSnapshotCatalogState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [snapshotCatalogError, setSnapshotCatalogError] = useState<string | null>(null);
  const [timing, setTiming] = useState<PlanTimingSelection | null>(null);
  const [planCandidates, setPlanCandidates] = useState<CandidateResponseRow[]>([]);
  const [planCandidatesSnapshotId, setPlanCandidatesSnapshotId] = useState<string | null>(null);
  const [legacyS1, setLegacyS1] = useState<LegacyS1State>({ kind: "idle", snapshotId: null });
  const appLoadGeneration = useRef(0);
  const legacyLoadGeneration = useRef(0);
  const currentSnapshotId = useRef<string | null>(null);
  const legacyS1Ref = useRef<LegacyS1State>(legacyS1);
  /** null = 接受模型方案；数组 = 人工点选的 S1 候选子集。 */
  const [selectedInstrumentIds, setSelectedInstrumentIds] = useState<string[] | null>(null);

  const setLegacyS1State = useCallback((next: LegacyS1State) => {
    legacyS1Ref.current = next;
    setLegacyS1(next);
  }, []);

  const setTab = useCallback((next: Tab) => {
    setTabState(next);
    setVisitedTabs(previous => previous.includes(next) ? previous : [...previous, next]);
    if (window.location.hash !== "#" + next) {
      window.history.pushState(null, "", "#" + next);
    }
  }, []);

  useEffect(() => {
    const onHash = () => {
      const next = tabFromHash(); setTabState(next);
      setVisitedTabs(previous => previous.includes(next) ? previous : [...previous, next]);
    };
    window.addEventListener("hashchange", onHash);
    window.addEventListener("popstate", onHash);
    return () => { window.removeEventListener("hashchange", onHash); window.removeEventListener("popstate", onHash); };
  }, []);

  useEffect(() => {
    // Each task starts at its heading; retained controls keep their values.
    window.scrollTo?.(0, 0);
    if (typeof document !== "undefined") document.querySelector<HTMLElement>("main")?.focus({ preventScroll: true });
  }, [tab]);

  const explain = useCallback((err: unknown): string => {
    if (err instanceof AquantApiError) {
      return err.envelope.message + "（" + err.envelope.code + "）\n修复：" +
             err.envelope.repairAction;
    }
    return err instanceof Error ? err.message : String(err);
  }, []);

  const load = useCallback(async () => {
    const generation = ++appLoadGeneration.current;
    legacyLoadGeneration.current++;
    currentSnapshotId.current = null;
    setLegacyS1State({ kind: "idle", snapshotId: null });
    setState({ kind: "loading" });
    setNotice(null); setLivePreview(null); setConfirmResult(null);
    setSnapshots([]); setTiming(null); setFrozenPlanId(null); setPersistedPlanStatus(null);
    setSnapshotCatalogState("idle"); setSnapshotCatalogError(null);
    setPlanCandidates([]); setPlanCandidatesSnapshotId(null); setSelectedInstrumentIds(null);
    if (demoMode) {
      try {
        const res = await fetch(WS_URL, { cache: "no-store" });
        if (!res.ok) throw new Error("服务返回 " + res.status + " " + res.statusText);
        const fixture = await res.json() as WorkspaceData;
        setApiUp(false);
        setState({ kind: "ready", data: { ...fixture, dataSource: "fixture" } });
      } catch (err) {
        setState({ kind: "error", message: "显式演示模式的数据夹具无法读取：" + explain(err) });
      }
      return;
    }
    try {
      const status = await api.status();
      if (generation !== appLoadGeneration.current) return;
      currentSnapshotId.current = status.snapshotId;
      setApiUp(true);
      setState({ kind: "ready", data: buildApiWorkspace(
        status, [], "", [],
      ) });
    } catch (err) {
      if (generation !== appLoadGeneration.current) return;
      setApiUp(false);
      setState({ kind: "error", message:
        "工作台状态读取失败，请重试。\n" + explain(err) });
    }
  }, [demoMode, explain, setLegacyS1State]);

  useEffect(() => { void load(); }, [load]);

  const baseData = state.kind === "ready" ? state.data : null;
  const matchingLegacyS1 = baseData && legacyS1.kind === "ready" &&
    legacyS1.snapshotId === baseData.status.snapshotId ? legacyS1.workspace : null;
  const data = useMemo(() => {
    if (!baseData || !matchingLegacyS1) return baseData;
    return { ...baseData, candidates: matchingLegacyS1.candidates,
      todayChanges: matchingLegacyS1.todayChanges, note: matchingLegacyS1.note };
  }, [baseData, matchingLegacyS1]);
  const legacyS1ViewState = !baseData || demoMode ? "ready" :
    legacyS1.snapshotId !== baseData.status.snapshotId ? "idle" : legacyS1.kind;
  const legacyS1Error = legacyS1.kind === "error" && legacyS1.snapshotId === baseData?.status.snapshotId
    ? legacyS1.message : null;
  const displayStatus = data?.status ?? fallbackStatus();

  const loadLegacyS1 = useCallback(async (retry = false) => {
    if (!baseData || demoMode || apiUp !== true) return;
    const expectedSnapshotId = baseData.status.snapshotId;
    if (currentSnapshotId.current !== expectedSnapshotId) return;
    const previous = legacyS1Ref.current;
    if (previous.snapshotId === expectedSnapshotId &&
        (previous.kind === "loading" || previous.kind === "ready" || (!retry && previous.kind === "error"))) return;
    const appGeneration = appLoadGeneration.current;
    const requestGeneration = ++legacyLoadGeneration.current;
    setLegacyS1State({ kind: "loading", snapshotId: expectedSnapshotId });
    try {
      const [candidateResponse, eventResponse] = await Promise.all([
        api.candidates(expectedSnapshotId), api.events(),
      ]);
      if (appGeneration !== appLoadGeneration.current ||
          requestGeneration !== legacyLoadGeneration.current ||
          currentSnapshotId.current !== expectedSnapshotId) return;
      if (candidateResponse.snapshotId !== expectedSnapshotId || eventResponse.snapshotId !== expectedSnapshotId) {
        throw new Error(
          "API 返回了不一致的快照：status=" + expectedSnapshotId +
          "，candidates=" + candidateResponse.snapshotId +
          "，events=" + eventResponse.snapshotId,
        );
      }
      setLegacyS1State({ kind: "ready", snapshotId: expectedSnapshotId,
        workspace: buildApiWorkspace(baseData.status, candidateResponse.candidates,
          candidateResponse.note, eventResponse.events) });
    } catch (err) {
      if (appGeneration !== appLoadGeneration.current ||
          requestGeneration !== legacyLoadGeneration.current ||
          currentSnapshotId.current !== expectedSnapshotId) return;
      setLegacyS1State({ kind: "error", snapshotId: expectedSnapshotId,
        message: "今日与规则基线资料读取失败，请重试。\n" + explain(err) });
    }
  }, [baseData, demoMode, apiUp, explain, setLegacyS1State]);

  useEffect(() => {
    if (tab === "today") void loadLegacyS1();
  }, [tab, loadLegacyS1]);

  const loadSnapshotCatalog = useCallback(async () => {
    if (!data || demoMode) return;
    setSnapshotCatalogState("loading");
    setSnapshotCatalogError(null);
    try {
      const response = await api.snapshots();
      const expected = data.status.snapshotId;
      if (!response.snapshots.some((item) => item.snapshotId === expected)) {
        throw new Error("当前快照不在已发布快照目录中：" + expected);
      }
      setSnapshots(response.snapshots);
      setSnapshotCatalogState("ready");
      if (data.status.dataMode !== "SYNTHETIC") {
        const initialTiming = defaultProductionTiming(
          response.snapshots.filter((item) => item.dataMode === data.status.dataMode), expected,
        );
        setTiming(previous => previous ?? initialTiming);
      }
    } catch (err) {
      setSnapshotCatalogError("已发布快照历史暂不可用。模拟预览需要可核验的同源决策与执行快照。" + explain(err));
      setSnapshotCatalogState("error");
    }
  }, [data?.status.snapshotId, data?.status.dataMode, demoMode, explain]);

  useEffect(() => {
    if (tab !== "portfolio" || portfolioMode !== "baseline" || !data || demoMode ||
        snapshotCatalogState !== "idle") return;
    void loadSnapshotCatalog();
  }, [tab, portfolioMode, data, demoMode, snapshotCatalogState, loadSnapshotCatalog]);

  useEffect(() => {
    if (apiUp !== true || !data || (tab !== "today" && tab !== "portfolio")) return;
    let active = true;
    void Promise.allSettled([
      api.researchSimulationDrafts(), api.plans(data.draft.portfolioId), api.decisions(data.draft.portfolioId),
    ]).then(([draftResult, planResult, decisionResult]) => {
      if (!active) return;
      const errors: string[] = [];
      const plans = planResult.status === "fulfilled" ? planResult.value.plans : [];
      const decisions = decisionResult.status === "fulfilled" ? decisionResult.value.decisions : [];
      if (planResult.status === "fulfilled") {
        const recovered = plans.find(item => item.portfolioId === data.draft.portfolioId);
        if (recovered?.decisionSnapshotId && recovered.executionSnapshotId &&
            recovered.decisionCutoffAt && recovered.executionCutoffAt && recovered.tradingDay) {
          setFrozenPlanId(recovered.planId);
          setPersistedPlanStatus(recovered.status);
          setTiming({
            decisionSnapshotId: recovered.decisionSnapshotId,
            decisionCutoffAt: recovered.decisionCutoffAt,
            executionSnapshotId: recovered.executionSnapshotId,
            executionCutoffAt: recovered.executionCutoffAt,
            tradingDay: recovered.tradingDay,
          });
        }
      } else {
        errors.push("已确认计划状态尚未恢复：" + explain(planResult.reason));
      }
      if (draftResult.status === "fulfilled") {
        const restored = restoreResearchDrafts(
          draftResult.value.drafts.filter(item => item.portfolioId === data.draft.portfolioId), plans, decisions,
        );
        setResearchDrafts(previous => restored.map(item => {
          const reviewed = previous.find(old => old.draftId === item.draftId && old.status === "READY_FOR_REVIEW" &&
            old.researchSnapshotId === item.researchSnapshotId && old.decisionSnapshotId === item.decisionSnapshotId && old.tradingDay === item.tradingDay);
          return item.status === "WAITING_EXECUTION_SNAPSHOT" && reviewed ? reviewed : item;
        }));
      } else {
        errors.push("已保存方案尚未恢复：" + explain(draftResult.reason));
      }
      if (decisionResult.status === "rejected") {
        errors.push("原判断与计划绑定尚未读取：" + explain(decisionResult.reason));
      }
      setDraftReadError(errors.length ? errors.join("；") : null);
    });
    void api.watchlist().then(response => { if (active) setWatchIds(response.items.map(item => item.instrument_id)); })
      .catch(() => { if (active) setWatchIds([]); });
    return () => { active = false; };
  }, [apiUp, data?.status.snapshotId, data?.draft.portfolioId, reviewRevision, tab, explain]);

  const onPrepareResearchSimulation = useCallback((instrumentId: string, researchSnapshotId: string | null) => {
    setResearchSelection(previous => ({ requestId: (previous?.requestId ?? 0) + 1, instrumentId, researchSnapshotId }));
    setPortfolioMode("research"); setTab("portfolio");
  }, [setTab]);

  const onFocusResearch = useCallback((instrumentId: string) => {
    setFocusedInstrumentId(instrumentId); setTab("research");
  }, [setTab]);

  const onDraftsChange = useCallback((drafts: ResearchSimulationDraft[]) => {
    setResearchDrafts(drafts); setDraftReadError(null); setReviewRevision(previous => previous + 1);
  }, []);

  useEffect(() => {
    const currentSnapshotId = data?.status.snapshotId;
    const production = data?.status.dataMode !== "SYNTHETIC";
    const decisionSnapshotId = production ? timing?.decisionSnapshotId : timing?.decisionSnapshotId ?? currentSnapshotId;
    if (apiUp !== true || !currentSnapshotId || tab !== "portfolio" || portfolioMode !== "baseline") return;
    if (!decisionSnapshotId) {
      if (production && planCandidatesSnapshotId !== currentSnapshotId) {
        setPlanCandidates([]);
        setPlanCandidatesSnapshotId(null);
        setSelectedInstrumentIds(null);
      }
      return;
    }
    if (planCandidatesSnapshotId === decisionSnapshotId) return;
    let active = true;
    setPlanCandidates([]);
    setPlanCandidatesSnapshotId(null);
    setSelectedInstrumentIds(null);
    void api.candidates(decisionSnapshotId)
      .then((response) => {
        if (!active) return;
        if (response.snapshotId !== decisionSnapshotId) {
          throw new Error("候选快照与计划决策快照不一致：" + response.snapshotId);
        }
        setPlanCandidates(response.candidates);
        setPlanCandidatesSnapshotId(response.snapshotId);
        setSelectedInstrumentIds(null);
      })
      .catch((err) => {
        if (active) setNotice("决策快照候选加载失败：" + explain(err));
      });
    return () => { active = false; };
  }, [apiUp, data?.status.snapshotId, data?.status.dataMode, timing?.decisionSnapshotId, tab, portfolioMode,
      planCandidatesSnapshotId, explain]);

  const onOpenResearch = useCallback((instrumentId: string) => {
    setSelectedCard(instrumentId); setTab("research");
    if (demoMode || !data) return;
    setResearchLoadingId(instrumentId); setNotice(null);
    void api.research(instrumentId, data.draft.tradingDay)
      .then((card: ResearchCard) => {
        if (card.snapshotId !== data.status.snapshotId) {
          throw new Error("研究卡快照与当前状态不一致：" + card.snapshotId +
                          " ≠ " + data.status.snapshotId);
        }
        setState((previous) => {
          if (previous.kind !== "ready") return previous;
          const cards = previous.data.researchCards.filter(
            (c) => c.instrumentId !== card.instrumentId,
          );
          return { kind: "ready", data: { ...previous.data, researchCards: [...cards, card] } };
        });
      })
      .catch((err) => setNotice("研究卡加载失败：" + explain(err)))
      .finally(() => setResearchLoadingId(null));
  }, [data, demoMode, explain, setTab]);

  const onRequestPreview = useCallback(async () => {
    if (!data || demoMode) return;
    const production = data.status.dataMode !== "SYNTHETIC";
    if (production && snapshotCatalogState !== "ready") {
      setNotice(snapshotCatalogState === "error"
        ? "已发布快照历史读取失败，尚未生成模拟预览。请先重读快照历史。"
        : "正在读取已发布快照历史；完成前不能生成模拟预览。");
      return;
    }
    if (production && !timing) {
      setNotice("生产预览需要两个满足先后顺序的同源已发布快照。");
      return;
    }
    if (selectedInstrumentIds !== null && timing && planCandidatesSnapshotId !== timing.decisionSnapshotId) {
      setNotice("决策快照候选尚未按当前版本读取完成；不能使用其他版本的候选生成预览。");
      return;
    }
    setNotice(null); setConfirmResult(null);
    try {
      const pv = await api.preview({
        portfolio_id: data.draft.portfolioId,
        snapshot_id: timing?.decisionSnapshotId ?? data.status.snapshotId,
        trading_day: timing?.tradingDay ?? data.draft.tradingDay,
        ...(timing ? {
          decision_snapshot_id: timing.decisionSnapshotId,
          decision_cutoff_at: timing.decisionCutoffAt,
          execution_snapshot_id: timing.executionSnapshotId,
        } : {}),
        ...(selectedInstrumentIds !== null
          ? { selected_instrument_ids: selectedInstrumentIds } : {}),
      });
      const expectedDecision = timing?.decisionSnapshotId ?? data.status.snapshotId;
      if (pv.snapshot_id !== expectedDecision) {
        throw new Error("服务端预览使用了不同快照：" + pv.snapshot_id);
      }
      setLivePreview(pv);
      setNotice("服务端预览已生成：" + pv.orders.length + " 笔订单，参考价日 " +
                (pv.reference_price_day ?? "—") + "（执行日之前）。仍未冻结。");
    } catch (err) { setNotice("预览失败：" + explain(err)); }
  }, [data, demoMode, explain, timing, selectedInstrumentIds, snapshotCatalogState, planCandidatesSnapshotId]);

  const onConfirm = useCallback(async () => {
    if (!data || demoMode || (data.status.dataMode !== "SYNTHETIC" && !timing)) return;
    if (data.status.dataMode !== "SYNTHETIC" && snapshotCatalogState !== "ready") {
      setNotice("已发布快照历史尚未读取完成，不能确认模拟计划。");
      return;
    }
    setConfirming(true); setConfirmResult(null);
    try {
      let pv = livePreview;
      if (!pv) {
        pv = await api.preview({
          portfolio_id: data.draft.portfolioId,
          snapshot_id: timing?.decisionSnapshotId ?? data.status.snapshotId,
          trading_day: timing?.tradingDay ?? data.draft.tradingDay,
          ...(timing ? {
            decision_snapshot_id: timing.decisionSnapshotId,
            decision_cutoff_at: timing.decisionCutoffAt,
            execution_snapshot_id: timing.executionSnapshotId,
          } : {}),
          ...(selectedInstrumentIds !== null
            ? { selected_instrument_ids: selectedInstrumentIds } : {}),
        });
        setLivePreview(pv);
      }
      const issued = await api.requestConfirmation(pv.planId);
      const frozen = await api.freeze(pv.planId, issued.confirmationToken);
      const persistedId = typeof frozen.plan_id === "string" ? frozen.plan_id : pv.planId;
      setFrozenPlanId(persistedId);
      setPersistedPlanStatus("FROZEN");
      setConfirmResult({ ok: true, message: "已冻结（服务端复核通过）。计划 ID " + persistedId +
        "，冻结时间 " + String(frozen.frozen_at ?? "") +
        "。冻结后不可修改；可在下方「账本」区执行本交易日。" });
    } catch (err) { setConfirmResult({ ok: false, message: explain(err) }); }
    finally { setConfirming(false); }
  }, [data, demoMode, livePreview, explain, timing, selectedInstrumentIds, snapshotCatalogState]);

  const onPlanSelectionChange = useCallback((ids: string[] | null) => {
    setSelectedInstrumentIds(ids);
    setLivePreview(null); setFrozenPlanId(null); setPersistedPlanStatus(null); setConfirmResult(null);
  }, []);

  const onTimingChange = useCallback((role: "decision" | "execution", snapshotId: string) => {
    setTiming((previous) => {
      const selected = snapshots.find((item) => item.snapshotId === snapshotId);
      if (!selected) return null;
      if (role === "decision") {
        if (!selected.capabilities.s1Decision.available) return null;
        const currentExecution = snapshots.find(
          (item) => item.snapshotId === previous?.executionSnapshotId,
        );
        const compatibleExecution = currentExecution && timingFrom(selected, currentExecution)
          ? currentExecution
          : [...snapshots].filter(
              (item) => item.dataMode === selected.dataMode &&
                item.tradingDay !== null && selected.tradingDay !== null &&
                item.tradingDay > selected.tradingDay && item.asOfTime > selected.asOfTime,
            ).sort((a, b) => a.asOfTime.localeCompare(b.asOfTime)).at(-1);
        return timingFrom(selected, compatibleExecution);
      }
      const currentDecision = snapshots.find(
        (item) => item.snapshotId === previous?.decisionSnapshotId,
      );
      const compatibleDecision = currentDecision && timingFrom(currentDecision, selected)
        ? currentDecision
        : [...snapshots].filter(
            (item) => item.dataMode === selected.dataMode &&
              item.capabilities.s1Decision.available &&
              item.tradingDay !== null && selected.tradingDay !== null &&
              item.tradingDay < selected.tradingDay && item.asOfTime < selected.asOfTime,
          ).sort((a, b) => a.asOfTime.localeCompare(b.asOfTime)).at(-1);
      return timingFrom(compatibleDecision, selected);
    });
    setLivePreview(null); setFrozenPlanId(null); setPersistedPlanStatus(null); setConfirmResult(null);
    setSelectedInstrumentIds(null);
  }, [snapshots]);

  const evidenceCard = useMemo(
    () => (data && evidenceFor
      ? data.researchCards.find((c) => c.instrumentId === evidenceFor) ?? null : null),
    [data, evidenceFor],
  );

  const onAction = useCallback((actionId: string, cardId: string) => {
    if (actionId === "draft.create") {
      const card = data?.researchCards.find(item => item.instrumentId === cardId);
      const decisionSnapshotId = timing?.decisionSnapshotId ?? data?.status.snapshotId;
      if (!card || card.snapshotId !== decisionSnapshotId) {
        setNotice("本研究卡与规则方案的决策资料版本不同。请在模拟组合查看该方案的原决策候选；当前研究依据不会被静默替换。"); return;
      }
      const expectedCandidatesSnapshot = timing?.decisionSnapshotId ?? data?.status.snapshotId;
      if (planCandidatesSnapshotId !== expectedCandidatesSnapshot) {
        setNotice("当前模拟候选尚未按所选决策版本读取完成；请等待读取或重试后再选择。"); return;
      }
      const candidate = planCandidates.find(item => item.instrumentId === cardId && item.simulatable);
      if (!candidate) { setNotice("当前股票不在本次规则决策的可模拟范围。请查看当前资格与适用日期后再选择。"); return; }
      onPlanSelectionChange([cardId]); setPortfolioMode("baseline"); setTab("portfolio");
      setNotice("研究中的股票已带入规则方案。计算调整预览后，请检查资金、费用与规则；确认后才冻结。");
    } else if (actionId === "compare") {
      setNotice("当前是原快照研究卡。请在研究页主要名单中选择「加入比较」，查看同一研究版本的实际对比。");
    } else if (actionId === "watchlist.add") {
      setNotice(cardId + " 已更新自选状态（当前为会话内标记）。自选写入不产生订单。");
    } else if (actionId === "watchlist.saved") {
      setNotice(cardId + " 已保存到服务端自选。自选写入不产生订单。");
    } else if (actionId === "watchlist.removed") {
      setNotice(cardId + " 已从服务端自选移除。自选变化不影响模拟持仓。");
    }
    window.setTimeout(() => setNotice(null), 5000);
  }, [setTab, planCandidates, planCandidatesSnapshotId, onPlanSelectionChange, data, timing]);

  const onOpenPortfolio = useCallback(() => {
    const hasUserDraft = researchDrafts.some(item => item.source === "USER_SELECTION" &&
      ["WAITING_EXECUTION_SNAPSHOT", "READY_FOR_REVIEW", "BLOCKED", "FROZEN"].includes(item.status));
    setPortfolioMode(hasUserDraft ? "research" : frozenPlanId || livePreview ? "baseline" : "research");
    setTab("portfolio");
  }, [researchDrafts, frozenPlanId, livePreview, setTab]);

  return (
    <div className="app">
      <TopBar status={displayStatus} tab={tab} onTab={setTab}
        onOpenStatus={() => (data ? setStatusOpen(true) : void load())}
        lastLoadedAt={data ? formatRelative(data.generatedAt) : null}
        loading={state.kind === "loading"} />

      {demoMode && (
        <div className="section" role="note"><div className="callout callout-warn">
          <span className="icon" aria-hidden="true">!</span><div><strong>显式演示模式</strong>
            <div className="note" style={{ color: "inherit" }}>
              当前通过 <span className="mono">?mode=demo</span> 读取 workspace.json；这是只读夹具，实时 API 不参与本页数据与写操作。
            </div></div>
        </div></div>
      )}

      <main className="page" tabIndex={-1}>
        {state.kind === "loading" && tab !== "settings" && <Loading />}
        {state.kind === "error" && tab !== "settings" && <ErrorState message={state.message} onRetry={load} />}
        {(tab === "today" || tab === "portfolio") && draftReadError && (
          <div className="section"><Callout tone="warn" title="已保存方案状态尚未完整恢复">
            {draftReadError}
            <div style={{ marginTop: 8 }}>
              <button className="btn btn-sm" onClick={() => setReviewRevision(value => value + 1)}>
                重读已保存方案
              </button>
            </div>
          </Callout></div>
        )}
        {tab === "portfolio" && portfolioMode === "baseline" && snapshotCatalogState === "loading" && (
          <div className="section" role="status"><Callout title="正在读取已发布快照">
            读取完成前，模拟预览和确认不会使用其他版本的资料。
          </Callout></div>
        )}
        {tab === "portfolio" && portfolioMode === "baseline" && snapshotCatalogState === "error" && (
          <div className="section"><Callout tone="warn" title="模拟预览所需快照暂不可用">
            {snapshotCatalogError}
            <div style={{ marginTop: 8 }}>
              <button className="btn btn-sm" onClick={() => void loadSnapshotCatalog()}>重读快照历史</button>
            </div>
          </Callout></div>
        )}
        {tab === "settings" && <SettingsView />}
        {data && (
          <>
            {data.status.dataMode === "SYNTHETIC" && (
              <div className="section"><div className="callout callout-warn" role="note">
                <span className="icon" aria-hidden="true">!</span><div><strong>当前为虚构示例数据</strong>
                  <div className="note" style={{ color: "inherit" }}>{data.status.watermark ?? "SYNTHETIC"}。全部数值仅用于契约与确定性测试，不得用于任何收益结论，也不得与真实行情混成同一曲线。</div>
                </div>
              </div></div>
            )}
            {notice && <div className="section"><div className="callout callout-info" role="status">
              <span className="icon" aria-hidden="true">i</span><div>{notice}</div>
            </div></div>}
            {visitedTabs.includes("today") && <section hidden={tab !== "today"} aria-label="今日">
              <TodayView data={data} onOpenCard={onOpenResearch}
                onOpenDraft={onOpenPortfolio} onOpenResearch={() => setTab("research")}
                livePreview={livePreview} persistedPlanStatus={persistedPlanStatus} researchDrafts={researchDrafts}
                watchIds={watchIds} legacyS1State={legacyS1ViewState} legacyS1Error={legacyS1Error}
                onRetryLegacyS1={() => void loadLegacyS1(true)} />
            </section>}
            {visitedTabs.includes("research") && <section hidden={tab !== "research"} aria-label="研究">
              <ResearchView data={data} selectedId={selectedCard}
              onSelect={setSelectedCard} onOpenEvidence={setEvidenceFor} onAction={onAction}
              onRequestResearch={onOpenResearch} loadingResearchId={researchLoadingId}
              legacyS1State={legacyS1ViewState} legacyS1Error={legacyS1Error}
              onLoadLegacyS1={() => void loadLegacyS1()} onRetryLegacyS1={() => void loadLegacyS1(true)}
              focusedInstrumentId={focusedInstrumentId} onPrepareSimulation={onPrepareResearchSimulation} />
            </section>}
            {visitedTabs.includes("portfolio") && <section hidden={tab !== "portfolio"} aria-label="模拟组合">
              <PortfolioView data={data} onConfirm={onConfirm}
              onRequestPreview={onRequestPreview} livePreview={livePreview} apiUp={apiUp}
              confirming={confirming} confirmResult={confirmResult} frozenPlanId={frozenPlanId}
              persistedPlanStatus={persistedPlanStatus}
              snapshots={snapshots} timing={timing} onTimingChange={onTimingChange}
              planCandidates={planCandidates} selectedInstrumentIds={selectedInstrumentIds}
              onPlanSelectionChange={onPlanSelectionChange} mode={portfolioMode} onModeChange={setPortfolioMode}
              selectionRequest={researchSelection} active={tab === "portfolio"} onDraftsChange={onDraftsChange}
              onOpenResearch={onFocusResearch} onDecisionSaved={() => setReviewRevision(previous => previous + 1)}
              pendingResearchPlan={researchDrafts.some(item => item.source === "USER_SELECTION" && item.status === "FROZEN")}
              accountRefreshKey={reviewRevision}
              onPlanChanged={() => {
                setReviewRevision(previous => previous + 1);
                void api.plans(data.draft.portfolioId).then(response => {
                  const plan = response.plans.find(item => item.planId === frozenPlanId);
                  if (plan) setPersistedPlanStatus(plan.status);
                }).catch(error => setNotice(explain(error)));
              }}
              onOpenReview={() => setTab("experiments")} />
            </section>}
            {tab === "workspace" && <WorkspaceView apiUp={apiUp}
              portfolioId={data.draft.portfolioId} tradingDay={data.draft.tradingDay} />}
            {visitedTabs.includes("experiments") && <section hidden={tab !== "experiments"} aria-label="复盘">
              <ExperimentsView key={reviewRevision} data={data} onOpenPortfolio={onOpenPortfolio} />
            </section>}
          </>
        )}
      </main>

      {statusOpen && data && <StatusDrawer status={data.status} onClose={() => setStatusOpen(false)} />}
      {evidenceCard && <EvidenceDrawer card={evidenceCard} onClose={() => setEvidenceFor(null)} />}
      <footer style={{ padding: "16px 20px 28px", textAlign: "center",
        color: "var(--text-4)", fontSize: 12, borderTop: "1px solid var(--border)" }}>
        研究与模拟用途 · 不连接券商 · 不自动交易 · 不构成投资建议
      </footer>
    </div>
  );
}
