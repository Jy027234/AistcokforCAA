import type { WorkspaceData } from "../lib/types";
import type {
  CandidateResponseRow, PlanTimingSelection, PreviewResponse, PublishedSnapshot,
} from "../lib/api";
import { Badge, Callout, Card, Empty, Section } from "../components/ui";
import { DraftPanel } from "../components/DraftPanel";
import { LedgerPanel } from "../components/LedgerPanel";

/** 组合：模型、事件影子、用户组合；草稿、模拟订单、持仓、账本（§5.2）。
 *  交互原则：自选与模拟持仓在视觉与权限上分离。
 *
 *  冻结之后的执行、估值与对账由 LedgerPanel 接通；账本数字全部来自服务端，
 *  界面只做展示换算。API 离线时账本段明确显示不可用，不用占位数字冒充。
 */
export function PortfolioView({
  data, onConfirm, onRequestPreview, livePreview, apiUp, confirming, confirmResult,
  frozenPlanId, snapshots, timing, onTimingChange,
  planCandidates, selectedInstrumentIds, onPlanSelectionChange,
}: {
  data: WorkspaceData;
  onConfirm: () => void;
  onRequestPreview: () => void;
  livePreview: PreviewResponse | null;
  apiUp: boolean | null;
  confirming: boolean;
  confirmResult: { ok: boolean; message: string } | null;
  /** 已成功冻结的计划 ID；为空表示还没有可执行的计划。 */
  frozenPlanId: string | null;
  snapshots: PublishedSnapshot[];
  timing: PlanTimingSelection | null;
  onTimingChange: (role: "decision" | "execution", snapshotId: string) => void;
  planCandidates: CandidateResponseRow[];
  selectedInstrumentIds: string[] | null;
  onPlanSelectionChange: (ids: string[] | null) => void;
}) {
  const source = data.dataSource === "api" ? "api" : "fixture";
  const production = data.dataSource === "api" && data.status.dataMode !== "SYNTHETIC";
  const productionTimingUnavailable = production && timing === null;
  const productionSnapshots = snapshots.filter(
    (snapshot) => snapshot.dataMode === data.status.dataMode,
  );
  const decisionSnapshots = productionSnapshots.filter(
    (snapshot) => snapshot.capabilities.s1Decision.available,
  );
  const decisionBlocker = productionSnapshots.find(
    (snapshot) => !snapshot.capabilities.s1Decision.available,
  )?.capabilities.s1Decision;
  const manualSelection = selectedInstrumentIds !== null;
  const manualIds = selectedInstrumentIds ?? [];
  const manualSelectionEmpty = manualSelection && manualIds.length === 0;
  const selectableCandidates = planCandidates.filter((candidate) => candidate.simulatable).slice(0, 30);
  const previewBlocked = productionTimingUnavailable || manualSelectionEmpty;
  const previewBlockedReason = productionTimingUnavailable
    ? "没有找到具备 S1 决策能力、在执行日开盘前已发布且满足先后顺序的同源快照对；请等待下一次合格日终流水线。"
    : manualSelectionEmpty
      ? "人工点选模式至少需要选择一只可模拟的 S1 候选。"
      : null;
  return (
    <>
      <Section
        title="组合"
        hint="全部为模拟账户；自选与模拟持仓分离"
        dataSource={source}
        actions={<Badge tone="accent">模拟</Badge>}
      >
        <div className="grid-3">
          {data.portfolios.map((p) => (
            <Card key={p.portfolioId}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
                <h3>{p.label}</h3>
                <Badge tone="neutral">{p.kind} 组合</Badge>
              </div>
              <dl className="kv">
                <dt>组合 ID</dt><dd className="mono">{p.portfolioId}</dd>
                <dt>模拟净值</dt><dd className="mono">{p.netValue}</dd>
                <dt>可用现金</dt><dd className="mono">{p.cash}</dd>
                <dt>持仓数</dt><dd>{p.positions}</dd>
              </dl>
              <p className="note" style={{ marginTop: 8 }}>{p.note}</p>
            </Card>
          ))}
        </div>
      </Section>

      <Section
        title="草稿与确认"
        hint={apiUp === null ? "正在检测服务…" : undefined}
        dataSource={source}
        actions={
          apiUp === false ? <Badge tone="warn">API 离线 · 仅只读夹具</Badge>
          : apiUp === true ? <Badge tone="ok">API 在线</Badge>
          : <Badge tone="neutral">检测中</Badge>
        }
      >
        {production && (
          <Card title="生产时点绑定">
            <Callout tone="info" title="候选与成交使用不同快照">
              决策快照决定候选与参考价，执行快照只提供执行日收盘行情。
              截止时间和交易日都从已发布快照读取，不能手工改写；决策快照还必须在
              执行日 09:30 前实际发布，事后补发的历史回放不会进入试运行计划。
            </Callout>
            {productionSnapshots.length < 2 ? (
              <Callout tone="warn" title="已发布快照不足">
                生产预览至少需要两个同来源的已发布快照：执行日前的决策快照，
                以及执行日收盘快照。
              </Callout>
            ) : decisionSnapshots.length === 0 ? (
              <Callout tone="warn" title="没有可用的 S1 决策快照">
                {decisionBlocker?.message ?? "已发布快照不满足 S1 决策数据条件。"}
                {decisionBlocker?.repairAction ? " 修复：" + decisionBlocker.repairAction : ""}
              </Callout>
            ) : (
              <div className="grid-2" style={{ marginTop: 12 }}>
                <label>
                  <span className="note">决策快照</span>
                  <select className="input mono" value={timing?.decisionSnapshotId ?? ""}
                    onChange={(event) => onTimingChange("decision", event.target.value)}>
                    {decisionSnapshots.map((snapshot) => (
                      <option key={snapshot.snapshotId} value={snapshot.snapshotId}>
                        {snapshot.tradingDay} · {snapshot.snapshotId}
                      </option>
                    ))}
                  </select>
                  <span className="note">截止：{timing?.decisionCutoffAt ?? "—"}</span>
                </label>
                <label>
                  <span className="note">执行快照</span>
                  <select className="input mono" value={timing?.executionSnapshotId ?? ""}
                    onChange={(event) => onTimingChange("execution", event.target.value)}>
                    {productionSnapshots.map((snapshot) => (
                      <option key={snapshot.snapshotId} value={snapshot.snapshotId}>
                        {snapshot.tradingDay} · {snapshot.snapshotId}
                      </option>
                    ))}
                  </select>
                  <span className="note">交易日：{timing?.tradingDay ?? "—"} · 收盘：{timing?.executionCutoffAt ?? "—"}</span>
                </label>
              </div>
            )}
          </Card>
        )}
        <Card title="候选选择">
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
            <button className={"btn btn-sm " + (!manualSelection ? "btn-primary" : "")}
              onClick={() => onPlanSelectionChange(null)}>
              接受模型方案
            </button>
            <button className={"btn btn-sm " + (manualSelection ? "btn-primary" : "")}
              onClick={() => onPlanSelectionChange([])} disabled={apiUp !== true}>
              人工点选
            </button>
            <span className="note">
              {manualSelection ? `已选择 ${manualIds.length} 只` : "由 S1 排名和组合约束自动构建"}
            </span>
          </div>
          {manualSelection && (
            <>
              <Callout tone="info" title="人工最终方案会单独留痕">
                这里只能选择当前决策快照中的 S1 候选。冻结后，模型原方案、人工最终方案和差异会绑定同一计划写入决策日志。
              </Callout>
              {selectableCandidates.length === 0 ? (
                <p className="note">当前决策快照没有可模拟候选，不能生成计划。</p>
              ) : (
                <div className="table-wrap" style={{ marginTop: 10, maxHeight: 360 }}>
                  <table className="data">
                    <thead><tr><th>选择</th><th>名称</th><th className="num">S1 排名</th><th>行业</th></tr></thead>
                    <tbody>
                      {selectableCandidates.map((candidate) => {
                        const checked = manualIds.includes(candidate.instrumentId);
                        return (
                          <tr key={candidate.instrumentId}>
                            <td><input type="checkbox" checked={checked}
                              aria-label={`选择 ${candidate.displayName ?? candidate.instrumentId}`}
                              onChange={() => onPlanSelectionChange(checked
                                ? manualIds.filter((id) => id !== candidate.instrumentId)
                                : [...manualIds, candidate.instrumentId])} /></td>
                            <td>{candidate.displayName ?? candidate.instrumentId}
                              <div className="note mono">{candidate.instrumentId}</div></td>
                            <td className="num mono">{candidate.signalRank.toFixed(2)}</td>
                            <td className="mono">{candidate.industryCode ?? "—"}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
              {planCandidates.length > selectableCandidates.length && (
                <p className="note" style={{ marginTop: 8 }}>
                  当前只展示排名靠前的 30 只可模拟候选；不可模拟标的不进入人工计划。
                </p>
              )}
            </>
          )}
        </Card>
        <DraftPanel
          draft={data.draft}
          livePreview={livePreview}
          apiUp={apiUp}
          onRequestPreview={onRequestPreview}
          onConfirm={onConfirm}
          confirming={confirming}
          confirmResult={confirmResult}
          previewEnabled={!previewBlocked}
          previewDisabledReason={previewBlockedReason}
        />
      </Section>

      <Section
        title="账本"
        hint="执行、估值、对账全部来自服务端账本"
        dataSource={source}
      >
        {apiUp === false ? (
          <Card>
            <Callout tone="warn" title="写操作不可用">
              工作台 API 未运行，因此执行、估值与对账都不可用。
              界面不会在离线时显示任何未经服务端计算的账本数字。
            </Callout>
          </Card>
        ) : (
          <LedgerPanel
            portfolioId={data.draft.portfolioId}
            snapshotId={timing?.executionSnapshotId ?? data.status.snapshotId}
            tradingDay={timing?.tradingDay ?? data.draft.tradingDay}
            planId={frozenPlanId}
            frozen={frozenPlanId !== null}
          />
        )}
      </Section>

      <Section title="自选" dataSource={source}>
        <Card>
          <Empty title="自选与模拟持仓已分离">
            自选列表在研究页维护，视觉与权限上与模拟持仓分开：自选变化不被视为交易。
          </Empty>
        </Card>
      </Section>
    </>
  );
}
