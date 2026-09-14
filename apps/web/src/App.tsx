import { useCallback, useEffect, useMemo, useState } from "react";
import type { LoadState, WorkspaceData } from "./lib/types";
import { formatRelative } from "./lib/format";
import { TopBar, StatusDrawer, type Tab } from "./components/TopBar";
import { ErrorState, Loading } from "./components/ui";
import { EvidenceDrawer } from "./components/ResearchCard";
import { TodayView } from "./views/TodayView";
import { ResearchView } from "./views/ResearchView";
import { PortfolioView } from "./views/PortfolioView";
import { ExperimentsView } from "./views/ExperimentsView";

const WS_URL = "/workspace.json";

const TAB_IDS: Tab[] = ["today", "research", "portfolio", "experiments"];

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

  const onConfirm = useCallback(() => {
    setConfirming(true);
    setConfirmResult(null);
    // 当前阶段前端没有冻结接口；这里明确说明，而不是假装成功。
    window.setTimeout(() => {
      setConfirming(false);
      setConfirmResult({
        ok: false,
        message:
          "冻结接口尚未接入前端。后端已实现 freeze（含五项复核与人类主体校验），" +
          "但本工作台当前为只读数据视图，不会伪造一次成功的冻结。",
      });
    }, 600);
  }, []);

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
                confirming={confirming}
                confirmResult={confirmResult}
              />
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
