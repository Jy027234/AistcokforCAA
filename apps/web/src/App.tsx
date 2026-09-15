import { useCallback, useEffect, useMemo, useState } from "react";
import type { LoadState, WorkspaceData } from "./lib/types";
import { formatRelative } from "./lib/format";
import { TopBar, StatusDrawer, type Tab } from "./components/TopBar";
import { api, AquantApiError, type PreviewResponse } from "./lib/api";
import { ErrorState, Loading } from "./components/ui";
import { EvidenceDrawer } from "./components/ResearchCard";
import { TodayView } from "./views/TodayView";
import { ResearchView } from "./views/ResearchView";
import { PortfolioView } from "./views/PortfolioView";
import { ExperimentsView } from "./views/ExperimentsView";
import { WorkspaceView } from "./views/WorkspaceView";

const WS_URL = "/workspace.json";

const TAB_IDS: Tab[] = ["today", "research", "portfolio", "workspace", "experiments"];

function tabFromHash(): Tab {
  const h = window.location.hash.replace(/^#\/?/, "").split("?")[0] ?? "";
  return (TAB_IDS as string[]).includes(h) ? (h as Tab) : "today";
}

export default function App() {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [tab, setTabState] = useState<Tab>(tabFromHash);

  // 视图可深链：刷新与分享都能回到同一页
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
  const [statusOpen, setStatusOpen] = useState(false);
  const [evidenceFor, setEvidenceFor] = useState<string | null>(null);
  const [selectedCard, setSelectedCard] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [confirmResult, setConfirmResult] = useState<{ ok: boolean; message: string } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  //: 服务端实时预览。为 null 时界面回退到只读夹具，并明确标注来源。
  const [livePreview, setLivePreview] = useState<PreviewResponse | null>(null);
  const [apiUp, setApiUp] = useState<boolean | null>(null);
  //: 已成功冻结的计划。只有它才能被执行——界面不提供"跳过冻结直接执行"。
  const [frozenPlanId, setFrozenPlanId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      const res = await fetch(WS_URL, { cache: "no-store" });
      if (!res.ok) {
        throw new Error("服务返回 " + res.status + " " + res.statusText);
      }
      const data = (await res.json()) as WorkspaceData;
      setState({ kind: "ready", data });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setState({
        kind: "error",
        message:
          message + "。请确认工作台数据已生成（tools/build_workspace_fixture.py）。",
      });
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const data = state.kind === "ready" ? state.data : null;

  // 探测 API。界面不假装它在线——离线时明确显示为只读预览。
  useEffect(() => {
    if (!data) return;
    let alive = true;
    api.health()
      .then(() => { if (alive) setApiUp(true); })
      .catch(() => { if (alive) setApiUp(false); });
    return () => { alive = false; };
  }, [data]);

  const explain = useCallback((err: unknown): string => {
    if (err instanceof AquantApiError) {
      return err.envelope.message + "（" + err.envelope.code + "）\n修复：" +
             err.envelope.repairAction;
    }
    return err instanceof Error ? err.message : String(err);
  }, []);

  /** 第一步：向服务端请求预览。只算不冻。 */
  const onRequestPreview = useCallback(async () => {
    if (!data) return;
    setNotice(null);
    setConfirmResult(null);
    try {
      const pv = await api.preview({
        portfolio_id: data.draft.portfolioId,
        snapshot_id: data.status.snapshotId,
        trading_day: data.draft.tradingDay,
      });
      setLivePreview(pv);
      setNotice("服务端预览已生成：" + pv.orders.length + " 笔订单，参考价日 " +
                (pv.reference_price_day ?? "—") + "（执行日之前）。仍未冻结。");
    } catch (err) {
      setNotice("预览失败：" + explain(err));
    }
  }, [data, explain]);

  const evidenceCard = useMemo(
    () => (data && evidenceFor
      ? data.researchCards.find((c) => c.instrumentId === evidenceFor) ?? null
      : null),
    [data, evidenceFor],
  );

  const onAction = useCallback((actionId: string, cardId: string) => {
    if (actionId === "draft.create") {
      setTab("portfolio");
      setNotice("已切换到组合页查看模拟草稿。草稿需在界面中确认后才会冻结。");
      window.setTimeout(() => setNotice(null), 6000);
    } else if (actionId === "compare") {
      setNotice(cardId + " 已加入比较（当前为会话内比较，不写入后端）。");
      window.setTimeout(() => setNotice(null), 5000);
    } else if (actionId === "watchlist.add") {
      setNotice(cardId + " 已更新自选状态（会话内）。自选写入不产生订单。");
      window.setTimeout(() => setNotice(null), 5000);
    }
  }, []);

  /** 第二步：取服务端签发的一次性令牌，第三步用它冻结。
   *
   * 令牌由服务端绑定主体、计划、快照、账户版本与完整预览哈希；
   * 前端只负责传递，不负责声明身份。
   */
  const onConfirm = useCallback(async () => {
    if (!data) return;
    setConfirming(true);
    setConfirmResult(null);
    try {
      let pv = livePreview;
      if (!pv) {
        pv = await api.preview({
          portfolio_id: data.draft.portfolioId,
          snapshot_id: data.status.snapshotId,
          trading_day: data.draft.tradingDay,
        });
        setLivePreview(pv);
      }
      const issued = await api.requestConfirmation(pv.planId);
      const frozen = await api.freeze(pv.planId, issued.confirmationToken);
      // 记下这一个计划：账本区的执行按钮只对它开放。
      setFrozenPlanId(pv.planId);
      setConfirmResult({
        ok: true,
        message: "已冻结（服务端复核通过）。计划 ID " + pv.planId +
                 "，冻结时间 " + String(frozen.frozen_at ?? "") +
                 "。冻结后不可修改；可在下方「账本」区执行本交易日。",
      });
    } catch (err) {
      setConfirmResult({ ok: false, message: explain(err) });
    } finally {
      setConfirming(false);
    }
  }, [data, livePreview, explain]);

  return (
    <div className="app">
      <TopBar
        status={data?.status ?? {
          snapshotId: "—", kind: "—", asOfTime: new Date().toISOString(),
          publishedAt: null, dataMode: "—", watermark: null, qualityStatus: "—",
          readiness: "PARTIAL", readinessLabel: "载入中", blockingIssues: [],
          datasetSummary: [], timeLabel: "—", accountLabel: "模拟账户",
        }}
        tab={tab}
        onTab={setTab}
        onOpenStatus={() => (data ? setStatusOpen(true) : void load())}
        lastLoadedAt={data ? formatRelative(data.generatedAt) : null}
        loading={state.kind === "loading"}
      />

      <main className="page">
        {state.kind === "loading" && <Loading />}
        {state.kind === "error" && <ErrorState message={state.message} onRetry={load} />}
        {data && (
          <>
            {data.status.dataMode === "SYNTHETIC" && (
              <div className="section">
                <div className="callout callout-warn" role="note">
                  <span className="icon" aria-hidden="true">!</span>
                  <div>
                    <strong>当前为虚构示例数据</strong>
                    <div className="note" style={{ color: "inherit" }}>
                      {data.status.watermark ?? "SYNTHETIC"}。全部数值仅用于契约与确定性测试，
                      不得用于任何收益结论，也不得与真实行情混成同一曲线。
                    </div>
                  </div>
                </div>
              </div>
            )}

            {notice && (
              <div className="section">
                <div className="callout callout-info" role="status">
                  <span className="icon" aria-hidden="true">i</span>
                  <div>{notice}</div>
                </div>
              </div>
            )}

            {tab === "today" && (
              <TodayView
                data={data}
                onOpenCard={(id) => { setSelectedCard(id); setTab("research"); }}
                onOpenDraft={() => setTab("portfolio")}
              />
            )}
            {tab === "research" && (
              <ResearchView
                data={data}
                selectedId={selectedCard}
                onSelect={setSelectedCard}
                onOpenEvidence={setEvidenceFor}
                onAction={onAction}
              />
            )}
            {tab === "portfolio" && (
              <PortfolioView
                data={data}
                onConfirm={onConfirm}
                onRequestPreview={onRequestPreview}
                livePreview={livePreview}
                apiUp={apiUp}
                confirming={confirming}
                confirmResult={confirmResult}
                frozenPlanId={frozenPlanId}
              />
            )}
            {tab === "workspace" && (
              <WorkspaceView apiUp={apiUp} portfolioId={data.draft.portfolioId}
                              tradingDay={data.draft.tradingDay} />
            )}
            {tab === "experiments" && <ExperimentsView data={data} />}
          </>
        )}
      </main>

      {statusOpen && data && (
        <StatusDrawer status={data.status} onClose={() => setStatusOpen(false)} />
      )}
      {evidenceCard && (
        <EvidenceDrawer card={evidenceCard} onClose={() => setEvidenceFor(null)} />
      )}

      <footer
        style={{
          padding: "16px 20px 28px", textAlign: "center",
          color: "var(--text-4)", fontSize: 12, borderTop: "1px solid var(--border)",
        }}
      >
        研究与模拟用途 · 不连接券商 · 不自动交易 · 不构成投资建议
      </footer>
    </div>
  );
}
