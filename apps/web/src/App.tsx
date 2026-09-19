import { useCallback, useEffect, useMemo, useState } from "react";
import type {
  DataStatus, Draft, ResearchCard, TodayChange, WorkspaceData,
} from "./lib/types";
import { formatRelative } from "./lib/format";
import { TopBar, StatusDrawer, type Tab } from "./components/TopBar";
import {
  api, AquantApiError, type CandidateResponseRow, type EventResponseRow,
  type PlanTimingSelection, type PreviewResponse, type PublishedSnapshot,
} from "./lib/api";
import { ErrorState, Loading } from "./components/ui";
import { EvidenceDrawer } from "./components/ResearchCard";
import { TodayView } from "./views/TodayView";
import { ResearchView } from "./views/ResearchView";
import { PortfolioView } from "./views/PortfolioView";
import { ExperimentsView } from "./views/ExperimentsView";
import { WorkspaceView } from "./views/WorkspaceView";
import { SettingsView } from "./views/SettingsView";

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

export default function App() {
  const demoMode = isDemoMode();
  const [state, setState] = useState<
    { kind: "loading" } | { kind: "ready"; data: WorkspaceData } |
    { kind: "error"; message: string }
  >({ kind: "loading" });
  const [tab, setTabState] = useState<Tab>(tabFromHash);
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
  const [snapshots, setSnapshots] = useState<PublishedSnapshot[]>([]);
  const [timing, setTiming] = useState<PlanTimingSelection | null>(null);

  const setTab = useCallback((next: Tab) => {
    setTabState(next);
    if (window.location.hash !== "#" + next) {
      window.history.replaceState(null, "", "#" + next);
    }
  }, []);

  useEffect(() => {
    const onHash = () => setTabState(tabFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const explain = useCallback((err: unknown): string => {
    if (err instanceof AquantApiError) {
      return err.envelope.message + "（" + err.envelope.code + "）\n修复：" +
             err.envelope.repairAction;
    }
    return err instanceof Error ? err.message : String(err);
  }, []);

  const load = useCallback(async () => {
    setState({ kind: "loading" });
    setNotice(null); setLivePreview(null); setConfirmResult(null);
    setSnapshots([]); setTiming(null); setFrozenPlanId(null);
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
      const [status, candidateResponse, eventResponse, snapshotResponse] = await Promise.all([
        api.status(), api.candidates(), api.events(), api.snapshots(),
      ]);
      const expected = status.snapshotId;
      if (candidateResponse.snapshotId !== expected || eventResponse.snapshotId !== expected) {
        throw new Error(
          "API 返回了不一致的快照：status=" + expected +
          "，candidates=" + candidateResponse.snapshotId +
          "，events=" + eventResponse.snapshotId,
        );
      }
      if (!snapshotResponse.snapshots.some((item) => item.snapshotId === expected)) {
        throw new Error("当前快照不在已发布快照目录中：" + expected);
      }
      setSnapshots(snapshotResponse.snapshots);
      setTiming(status.dataMode === "SYNTHETIC" ? null : defaultProductionTiming(
        snapshotResponse.snapshots.filter((item) => item.dataMode === status.dataMode),
        expected,
      ));
      setApiUp(true);
      setState({ kind: "ready", data: buildApiWorkspace(
        status, candidateResponse.candidates, candidateResponse.note, eventResponse.events,
      ) });
    } catch (err) {
      setApiUp(false);
      setState({ kind: "error", message:
        "实时工作台 API 加载失败；未回退到 workspace.json。\n" + explain(err) });
    }
  }, [demoMode, explain]);

  useEffect(() => { void load(); }, [load]);

  const data = state.kind === "ready" ? state.data : null;
  const displayStatus = data?.status ?? fallbackStatus();

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
    if (production && !timing) {
      setNotice("生产预览需要两个满足先后顺序的同源已发布快照。");
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
      });
      const expectedDecision = timing?.decisionSnapshotId ?? data.status.snapshotId;
      if (pv.snapshot_id !== expectedDecision) {
        throw new Error("服务端预览使用了不同快照：" + pv.snapshot_id);
      }
      setLivePreview(pv);
      setNotice("服务端预览已生成：" + pv.orders.length + " 笔订单，参考价日 " +
                (pv.reference_price_day ?? "—") + "（执行日之前）。仍未冻结。");
    } catch (err) { setNotice("预览失败：" + explain(err)); }
  }, [data, demoMode, explain, timing]);

  const onConfirm = useCallback(async () => {
    if (!data || demoMode || (data.status.dataMode !== "SYNTHETIC" && !timing)) return;
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
        });
        setLivePreview(pv);
      }
      const issued = await api.requestConfirmation(pv.planId);
      const frozen = await api.freeze(pv.planId, issued.confirmationToken);
      setFrozenPlanId(pv.planId);
      setConfirmResult({ ok: true, message: "已冻结（服务端复核通过）。计划 ID " + pv.planId +
        "，冻结时间 " + String(frozen.frozen_at ?? "") +
        "。冻结后不可修改；可在下方「账本」区执行本交易日。" });
    } catch (err) { setConfirmResult({ ok: false, message: explain(err) }); }
    finally { setConfirming(false); }
  }, [data, demoMode, livePreview, explain, timing]);

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
    setLivePreview(null); setFrozenPlanId(null); setConfirmResult(null);
  }, [snapshots]);

  const evidenceCard = useMemo(
    () => (data && evidenceFor
      ? data.researchCards.find((c) => c.instrumentId === evidenceFor) ?? null : null),
    [data, evidenceFor],
  );

  const onAction = useCallback((actionId: string, cardId: string) => {
    if (actionId === "draft.create") {
      setTab("portfolio"); setNotice("已切换到组合页查看模拟草稿。草稿需在界面中确认后才会冻结。");
    } else if (actionId === "compare") {
      setNotice(cardId + " 已加入比较（当前为会话内比较，不写入后端）。");
    } else if (actionId === "watchlist.add") {
      setNotice(cardId + " 已更新自选状态（当前为会话内标记）。自选写入不产生订单。");
    } else if (actionId === "watchlist.saved") {
      setNotice(cardId + " 已保存到服务端自选。自选写入不产生订单。");
    } else if (actionId === "watchlist.removed") {
      setNotice(cardId + " 已从服务端自选移除。自选变化不影响模拟持仓。");
    }
    window.setTimeout(() => setNotice(null), 5000);
  }, [setTab]);

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

      <main className="page">
        {state.kind === "loading" && tab !== "settings" && <Loading />}
        {state.kind === "error" && tab !== "settings" && <ErrorState message={state.message} onRetry={load} />}
        {tab === "settings" && <SettingsView />}
        {data && tab !== "settings" && (
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
            {tab === "today" && <TodayView data={data} onOpenCard={onOpenResearch}
              onOpenDraft={() => setTab("portfolio")} />}
            {tab === "research" && <ResearchView data={data} selectedId={selectedCard}
              onSelect={setSelectedCard} onOpenEvidence={setEvidenceFor} onAction={onAction}
              onRequestResearch={onOpenResearch} loadingResearchId={researchLoadingId} />}
            {tab === "portfolio" && <PortfolioView data={data} onConfirm={onConfirm}
              onRequestPreview={onRequestPreview} livePreview={livePreview} apiUp={apiUp}
              confirming={confirming} confirmResult={confirmResult} frozenPlanId={frozenPlanId}
              snapshots={snapshots} timing={timing} onTimingChange={onTimingChange} />}
            {tab === "workspace" && <WorkspaceView apiUp={apiUp}
              portfolioId={data.draft.portfolioId} tradingDay={data.draft.tradingDay} />}
            {tab === "experiments" && <ExperimentsView data={data} />}
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
