import { useEffect, useState } from "react";
import type { WorkspaceData } from "../lib/types";
import { api, AquantApiError } from "../lib/api";
import { Badge, Callout, Card, Empty, Section } from "../components/ui";
import { ResearchCard } from "../components/ResearchCard";

type Scope = "market" | "industry" | "watchlist" | "event";

const SCOPES: { id: Scope; label: string }[] = [
  { id: "market", label: "全市场" },
  { id: "industry", label: "行业" },
  { id: "watchlist", label: "自选" },
  { id: "event", label: "事件关联" },
];

/** 研究范围来自候选 API，研究卡按选中标的从同一快照按需读取。 */
export function ResearchView({
  data, selectedId, onSelect, onOpenEvidence, onAction,
  onRequestResearch, loadingResearchId,
}: {
  data: WorkspaceData;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onOpenEvidence: (id: string) => void;
  onAction: (id: string, cardId: string) => void;
  onRequestResearch?: (id: string) => void;
  loadingResearchId?: string | null;
}) {
  const [scope, setScope] = useState<Scope>("market");
  const [watchlist, setWatchlist] = useState<string[]>([]);
  const [watchBusy, setWatchBusy] = useState<string | null>(null);
  const [watchError, setWatchError] = useState<string | null>(null);
  const source = data.dataSource === "api" ? "api" : "fixture";
  const cardsById = new Map(data.researchCards.map((c) => [c.instrumentId, c]));

  useEffect(() => {
    if (source !== "api") return;
    let active = true;
    api.watchlist()
      .then((result) => {
        if (active) setWatchlist(result.items.map((item) => item.instrument_id));
      })
      .catch((error: unknown) => {
        if (!active) return;
        const message = error instanceof AquantApiError
          ? error.envelope.message + "（" + error.envelope.code + "）"
          : error instanceof Error ? error.message : String(error);
        setWatchError("自选读取失败：" + message);
      });
    return () => { active = false; };
  }, [source]);

  async function toggleWatch(instrumentId: string) {
    const watching = watchlist.includes(instrumentId);
    if (source !== "api") {
      setWatchlist((items) => watching
        ? items.filter((item) => item !== instrumentId)
        : [...items, instrumentId]);
      onAction("watchlist.add", instrumentId);
      return;
    }
    setWatchBusy(instrumentId);
    setWatchError(null);
    try {
      if (watching) {
        await api.removeWatch(instrumentId);
        setWatchlist((items) => items.filter((item) => item !== instrumentId));
        onAction("watchlist.removed", instrumentId);
      } else {
        await api.addWatch(instrumentId);
        setWatchlist((items) => [...new Set([...items, instrumentId])]);
        onAction("watchlist.saved", instrumentId);
      }
    } catch (error) {
      const message = error instanceof AquantApiError
        ? error.envelope.message + "（" + error.envelope.code + "）· 修复：" +
          error.envelope.repairAction
        : error instanceof Error ? error.message : String(error);
      setWatchError("自选更新失败：" + message);
    } finally {
      setWatchBusy(null);
    }
  }

  const scoped = data.candidates.filter((candidate) => {
    if (scope === "watchlist") return watchlist.includes(candidate.instrumentId);
    if (scope === "event") {
      return data.todayChanges.some((change) => change.subjects.includes(candidate.instrumentId));
    }
    if (scope === "industry") return candidate.industryCode !== "—";
    return true;
  });
  const selectedCandidate = scoped.find((c) => c.instrumentId === selectedId) ?? scoped[0] ?? null;
  const selected = selectedCandidate ? cardsById.get(selectedCandidate.instrumentId) ?? null : null;

  return (
    <>
      <Section title="研究" hint="同一实体使用同一卡片与证据链" dataSource={source}
        actions={<div className="tabs" role="tablist" aria-label="研究范围">
          {SCOPES.map((s) => (
            <button key={s.id} role="tab" aria-selected={scope === s.id} className="tab"
              onClick={() => setScope(s.id)}>
              {s.label}{s.id === "watchlist" && watchlist.length > 0 && " (" + watchlist.length + ")"}
            </button>
          ))}
        </div>}>
        {watchError && (
          <div style={{ marginBottom: 12 }}>
            <Callout tone="danger" title="自选没有更新">{watchError}</Callout>
          </div>
        )}
        {scoped.length === 0 ? (
          <Card><Empty title={scope === "watchlist" ? "自选列表为空" : "该范围内没有标的"}>
            {scope === "watchlist"
              ? "在研究卡上选择「加入自选」后，标的会出现在这里。自选与模拟持仓在视觉与权限上分离。"
              : "当前快照没有满足该范围的候选。"}
          </Empty></Card>
        ) : (
          <div className="grid-2" style={{ alignItems: "start" }}>
            <Card padded={false} title={"标的 · " + scoped.length + " 项"}>
              <ul className="list" style={{ padding: "0 16px" }}>
                {scoped.map((candidate) => {
                  const on = selectedCandidate?.instrumentId === candidate.instrumentId;
                  const starred = watchlist.includes(candidate.instrumentId);
                  return (
                    <li key={candidate.instrumentId}>
                      <button className="btn btn-ghost" style={{
                        width: "100%", justifyContent: "flex-start", textAlign: "left",
                        background: on ? "var(--accent-soft)" : undefined,
                        borderColor: on ? "var(--accent-border)" : "transparent", padding: "8px 10px",
                      }} onClick={() => {
                        onSelect(candidate.instrumentId);
                        onRequestResearch?.(candidate.instrumentId);
                      }}>
                        <span style={{ display: "grid", gap: 2, width: "100%" }}>
                          <span style={{ display: "flex", gap: 6, alignItems: "center" }}>
                            <strong>{candidate.displayName}</strong>
                            {starred && <Badge tone="accent">自选</Badge>}
                            {!candidate.simulatable && <Badge tone="warn">不可模拟</Badge>}
                          </span>
                          <span className="note mono">{candidate.instrumentId}</span>
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </Card>

            <div>
              {selected ? (
                <ResearchCard card={selected} onOpenEvidence={onOpenEvidence}
                  onAction={(actionId, cardId) => {
                    if (actionId === "watchlist.add") {
                      if (watchBusy === cardId) return;
                      void toggleWatch(cardId);
                      return;
                    }
                    onAction(actionId, cardId);
                  }} />
              ) : selectedCandidate ? (
                <Card>
                  <Empty title={loadingResearchId === selectedCandidate.instrumentId
                    ? "正在载入研究卡" : "尚未载入研究卡"}>
                    {source === "api"
                      ? "研究卡将从当前快照按需读取；读取完成后会显示数值、证据、反证与限制。"
                      : "点击左侧标的载入研究卡。"}
                    {source === "api" && loadingResearchId !== selectedCandidate.instrumentId && (
                      <div style={{ marginTop: 10 }}>
                        <button className="btn btn-sm btn-primary"
                          onClick={() => onRequestResearch?.(selectedCandidate.instrumentId)}>
                          载入研究卡
                        </button>
                      </div>
                    )}
                  </Empty>
                </Card>
              ) : <Card><Empty title="请选择标的" /></Card>}
            </div>
          </div>
        )}
      </Section>
    </>
  );
}
