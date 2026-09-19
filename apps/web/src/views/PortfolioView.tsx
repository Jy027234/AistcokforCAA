import type { WorkspaceData } from "../lib/types";
import type { PreviewResponse } from "../lib/api";
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
  frozenPlanId,
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
}) {
  const source = data.dataSource === "api" ? "api" : "fixture";
  const productionTimingUnavailable = data.dataSource === "api" &&
    data.status.dataMode !== "SYNTHETIC";
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
        <DraftPanel
          draft={data.draft}
          livePreview={livePreview}
          apiUp={apiUp}
          onRequestPreview={onRequestPreview}
          onConfirm={onConfirm}
          confirming={confirming}
          confirmResult={confirmResult}
          previewEnabled={!productionTimingUnavailable}
          previewDisabledReason={productionTimingUnavailable
            ? "生产计划要求独立的决策快照、执行快照和明确截止时点；当前接口尚未提供快照列表与选择入口。"
            : null}
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
            snapshotId={data.status.snapshotId}
            tradingDay={data.draft.tradingDay}
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
