import type { WorkspaceData } from "../lib/types";
import type { PreviewResponse } from "../lib/api";
import { Badge, Callout, Card, Empty, Section } from "../components/ui";
import { DraftPanel } from "../components/DraftPanel";

/** 组合：模型、事件影子、用户组合；草稿、模拟订单、持仓、账本（§5.2）。
 *  交互原则：自选与模拟持仓在视觉与权限上分离。
 *
 *  账本数据在当前阶段尚未从后端读取，因此这里**明确显示未接入**，
 *  而不是用占位数字冒充。
 */
export function PortfolioView({
  data, onConfirm, onRequestPreview, livePreview, apiUp, confirming, confirmResult,
}: {
  data: WorkspaceData;
  onConfirm: () => void;
  onRequestPreview: () => void;
  livePreview: PreviewResponse | null;
  apiUp: boolean | null;
  confirming: boolean;
  confirmResult: { ok: boolean; message: string } | null;
}) {
  return (
    <>
      <Section
        title="组合"
        hint="全部为模拟账户；自选与模拟持仓分离"
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
        />
      </Section>

      <Section title="账本">
        <Card>
          <Callout tone="warn" title="账本尚未接入此视图">
            成交、费用明细、持仓批次、现金与应收账本目前只在后端账本中可查。
            此视图**不会**用占位数字冒充账本数据。
          </Callout>
        </Card>
      </Section>

      <Section title="自选">
        <Card>
          <Empty title="自选与模拟持仓已分离">
            自选列表在研究页维护，视觉与权限上与模拟持仓分开：自选变化不被视为交易。
          </Empty>
        </Card>
      </Section>
    </>
  );
}
