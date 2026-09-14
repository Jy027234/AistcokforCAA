import type { Draft } from "../lib/types";
import { Badge, Callout, Card } from "../components/ui";

/** 模拟草稿与差异预览（§5.4 底部、§5.5）。
 *
 * 明确标注未冻结。确认入口说明它需要人类显式确认，并列出冻结时要复核的五项。
 * 自选与模拟持仓在这里保持分离：本面板只展示模拟持仓相关草稿。
 */
export function DraftPanel({
  draft, onConfirm, confirming, confirmResult,
}: {
  draft: Draft;
  onConfirm: () => void;
  confirming: boolean;
  confirmResult: { ok: boolean; message: string } | null;
}) {
  const failed = draft.ruleChecks.filter((c) => !c.passed);

  return (
    <Card
      title="我的模拟草稿"
      actions={<Badge tone="neutral">{draft.frozenLabel}</Badge>}
    >
      <div className="grid-3" style={{ marginBottom: 14 }}>
        <div className="stat">
          <span className="stat-label">预计资金占用</span>
          <span className="stat-value mono">{draft.buyTotal}</span>
        </div>
        <div className="stat">
          <span className="stat-label">预计费用</span>
          <span className="stat-value mono">{draft.estimatedFees}</span>
        </div>
        <div className="stat">
          <span className="stat-label">执行后可用现金</span>
          <span className="stat-value mono">{draft.cashAfter}</span>
        </div>
      </div>

      <h4 style={{ marginBottom: 6 }}>订单差异预览</h4>
      {draft.orders.length === 0 ? (
        <p className="note">当前没有需要调整的持仓，草稿为空。</p>
      ) : (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>方向</th><th>标的</th><th className="num">数量</th>
                <th className="num">价格</th><th className="num">金额</th>
                <th className="num">预计费用</th><th>理由</th>
              </tr>
            </thead>
            <tbody>
              {draft.orders.map((o) => (
                <tr key={o.instrumentId + o.side}>
                  <td>
                    <Badge tone={o.side === "BUY" ? "accent" : "neutral"}>
                      {o.side === "BUY" ? "买入" : "卖出"}
                    </Badge>
                  </td>
                  <td className="mono">{o.instrumentId}</td>
                  <td className="num mono">{o.quantity.toLocaleString()}</td>
                  <td className="num mono">{o.price}</td>
                  <td className="num mono">{o.gross}</td>
                  <td className="num mono">{o.estimatedFee}</td>
                  <td className="note">{o.rationale ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h4 style={{ margin: "16px 0 6px" }}>行业分布（上限 {draft.industryCapPct}）</h4>
      {draft.industry.length === 0 ? (
        <p className="note">暂无行业敞口。</p>
      ) : (
        <div className="table-wrap">
          <table className="data">
            <thead><tr><th>行业</th><th className="num">市值</th><th className="num">占比</th><th /></tr></thead>
            <tbody>
              {draft.industry.map((r) => (
                <tr key={r.industryCode}>
                  <td className="mono">{r.industryCode}</td>
                  <td className="num mono">{r.value}</td>
                  <td className="num mono">{r.sharePct}</td>
                  <td>{r.overCap && <Badge tone="warn">超过上限</Badge>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h4 style={{ margin: "16px 0 6px" }}>规则检查</h4>
      <ul className="list">
        {draft.ruleChecks.map((c) => (
          <li key={c.name} style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <Badge tone={c.passed ? "ok" : "warn"}>{c.passed ? "通过" : "未通过"}</Badge>
            <span>{c.name}</span>
          </li>
        ))}
      </ul>

      {draft.excluded.length > 0 && (
        <>
          <h4 style={{ margin: "16px 0 6px" }}>被排除的标的</h4>
          <ul className="list">
            {draft.excluded.map((e, i) => (
              <li key={i} className="note">
                <span className="mono">{e.instrumentId ?? e.instrument_id ?? "-"}</span> · {e.reason}
                {e.detail ? " · " + e.detail : ""}
              </li>
            ))}
          </ul>
        </>
      )}

      <div style={{ marginTop: 16 }}>
        {failed.length > 0 ? (
          <Callout tone="warn" title="存在未通过的规则检查，不能冻结">
            请先处理上述检查项。系统不会带着未通过的检查冻结计划。
          </Callout>
        ) : (
          <Callout tone="info" title="确认后才会冻结">
            {draft.confirmAction.requirement}。冻结时将复核：
            {draft.confirmAction.revalidate.join("、")}。
          </Callout>
        )}

        {confirmResult && (
          <div style={{ marginTop: 10 }}>
            <Callout tone={confirmResult.ok ? "ok" : "danger"}>
              {confirmResult.message}
            </Callout>
          </div>
        )}

        <div style={{ marginTop: 12, display: "flex", gap: 8, alignItems: "center" }}>
          <button
            className="btn btn-primary"
            onClick={onConfirm}
            disabled={confirming || failed.length > 0 || draft.orders.length === 0}
          >
            {confirming ? "冻结中…" : draft.confirmAction.label}
          </button>
          <span className="note">
            预览不产生成交；冻结后计划不可修改，修正需新建版本。
          </span>
        </div>
      </div>
    </Card>
  );
}
