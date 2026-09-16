import { useState } from "react";
import type { WorkspaceData } from "../lib/types";
import { Badge, Card, Empty, Section } from "../components/ui";
import { ResearchCard } from "../components/ResearchCard";

type Scope = "market" | "industry" | "watchlist" | "event";

const SCOPES: { id: Scope; label: string }[] = [
  { id: "market", label: "全市场" },
  { id: "industry", label: "行业" },
  { id: "watchlist", label: "自选" },
  { id: "event", label: "事件关联" },
];

/** 研究：行业/主题/股票、事件证据、对比分析、自选（§5.2）。
 *  交互原则：同一实体使用同一卡片和证据链。
 *  §5.4：桌面支持双栏研究和证据抽屉。
 */
export function ResearchView({
  data, selectedId, onSelect, onOpenEvidence, onAction,
}: {
  data: WorkspaceData;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onOpenEvidence: (id: string) => void;
  onAction: (id: string, cardId: string) => void;
}) {
  const [scope, setScope] = useState<Scope>("market");
  const [watchlist, setWatchlist] = useState<string[]>([]);

  const cards = data.researchCards;
  const watched = cards.filter((c) => watchlist.includes(c.instrumentId));
  const scoped =
    scope === "watchlist"
      ? watched
      : scope === "event"
        ? cards.filter((c) =>
            data.todayChanges.some((ch) => ch.subjects.includes(c.instrumentId)))
        : scope === "industry"
          ? cards.filter((c) => c.board === "MAIN")
          : cards;

  const selected = scoped.find((c) => c.instrumentId === selectedId) ?? scoped[0] ?? null;

  return (
    <>
      <Section
        title="研究"
        hint="同一实体使用同一卡片与证据链"
        dataSource="fixture"
        actions={
          <div className="tabs" role="tablist" aria-label="研究范围">
            {SCOPES.map((s) => (
              <button
                key={s.id}
                role="tab"
                aria-selected={scope === s.id}
                className="tab"
                onClick={() => setScope(s.id)}
              >
                {s.label}
                {s.id === "watchlist" && watchlist.length > 0 && " (" + watchlist.length + ")"}
              </button>
            ))}
          </div>
        }
      >
        {scoped.length === 0 ? (
          <Card>
            <Empty title={scope === "watchlist" ? "自选列表为空" : "该范围内没有标的"}>
              {scope === "watchlist"
                ? "在研究卡上选择「加入自选」后，标的会出现在这里。自选与模拟持仓在视觉与权限上分离。"
                : "调整范围或等待新的数据快照。"}
            </Empty>
          </Card>
        ) : (
          <div className="grid-2" style={{ alignItems: "start" }}>
            {/* 左：标的列表 */}
            <Card padded={false} title={"标的 · " + scoped.length + " 项"}>
              <ul className="list" style={{ padding: "0 16px" }}>
                {scoped.map((c) => {
                  const on = selected?.instrumentId === c.instrumentId;
                  const starred = watchlist.includes(c.instrumentId);
                  return (
                    <li key={c.instrumentId}>
                      <button
                        className="btn btn-ghost"
                        style={{
                          width: "100%", justifyContent: "flex-start", textAlign: "left",
                          background: on ? "var(--accent-soft)" : undefined,
                          borderColor: on ? "var(--accent-border)" : "transparent",
                          padding: "8px 10px",
                        }}
                        onClick={() => onSelect(c.instrumentId)}
                      >
                        <span style={{ display: "grid", gap: 2, width: "100%" }}>
                          <span style={{ display: "flex", gap: 6, alignItems: "center" }}>
                            <strong>{c.displayName}</strong>
                            {starred && <Badge tone="accent">自选</Badge>}
                            {!c.tradability.simulatable && <Badge tone="warn">不可模拟</Badge>}
                          </span>
                          <span className="note mono">{c.instrumentId}</span>
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </Card>

            {/* 右：研究卡 */}
            <div>
              {selected ? (
                <ResearchCard
                  card={selected}
                  onOpenEvidence={onOpenEvidence}
                  onAction={(actionId, cardId) => {
                    if (actionId === "watchlist.add") {
                      setWatchlist((w) =>
                        w.includes(cardId) ? w.filter((x) => x !== cardId) : [...w, cardId]);
                    }
                    onAction(actionId, cardId);
                  }}
                />
              ) : (
                <Card><Empty title="请选择标的" /></Card>
              )}
            </div>
          </div>
        )}
      </Section>
    </>
  );
}
