import type { Draft } from "../lib/types";
import type { PreviewResponse } from "../lib/api";
import { formatCents } from "../lib/format";
import { Badge, Callout, Card } from "../components/ui";

/** 模拟草稿与差异预览（§5.4 底部、§5.5）。
 *
 * 明确标注未冻结。确认入口说明它需要人类显式确认，并列出冻结时要复核的五项。
 * 自选与模拟持仓在这里保持分离：本面板只展示模拟持仓相关草稿。
 */
export function DraftPanel({
  draft, livePreview, apiUp, onRequestPreview, onConfirm, confirming, confirmResult,
}: {
  draft: Draft;
  livePreview: PreviewResponse | null;
  apiUp: boolean | null;
  onRequestPreview: () => void;
  onConfirm: () => void;
  confirming: boolean;
  confirmResult: { ok: boolean; message: string } | null;
}) {
  const failed = draft.ruleChecks.filter((c) => !c.passed);
  const offline = apiUp === false;

  /**
   * 显示哪一份草稿：**有服务端预览就显示它**。
   *
   * 原先不管有没有服务端预览，表格与数字都来自只读夹具，
   * 服务端结果只在下方一个小方块里出现。后果很严重：
   * 冻结按钮冻结的是**服务端**那份计划，而使用者看着的是夹具那张表——
   * 看到的是 A，冻结的是 B。
   *
   * 这条也违反项目自己的原则："界面不会在离线时显示任何未经服务端计算的
   * 账本数字"。修复方式不是加一句说明，而是让**表格本身就来自服务端**。
   */
  const usingLive = livePreview !== null;

  const rows = usingLive
    ? livePreview.orders.map((o) => {
        const gross = o.quantity * o.price_cents;
        return {
          instrumentId: o.instrument_id, side: o.side, quantity: o.quantity,
          // 账本与预览一律用**整数分**传输，前端只做显示换算（formatCents）。
          // 不在这里做任何金额再计算——算第二遍就会出现"界面上的数字
          // 和账本不一样"这种最难查的问题。
          price: formatCents(o.price_cents),
          gross: formatCents(gross), grossCents: gross,
          // 逐笔费用需要按订单重算，属于服务端的事；这里留空而不是猜一个，
          // 面板上下的关键金额（预计费用、执行后现金）都来自服务端。
          estimatedFee: "—",
          rationale: o.rationale,
        };
      })
    : draft.orders.map((o) => ({
        instrumentId: o.instrumentId, side: o.side, quantity: o.quantity,
        price: o.price, gross: o.gross, grossCents: 0,
        estimatedFee: o.estimatedFee, rationale: o.rationale,
      }));

  const shownFees = usingLive
    ? formatCents(livePreview.estimatedFeesCents) : draft.estimatedFees;
  const shownBuyTotal = usingLive
    ? formatCents(rows.reduce((sum, r) => sum + (r.grossCents ?? 0), 0))
    : draft.buyTotal;
  const shownCashAfter = usingLive
    ? formatCents(livePreview.cashAfterCents) : draft.cashAfter;

  /** 行业分布同样必须跟着订单表切换来源。
   *  这一块原先在服务端态下仍显示夹具数值——我把订单表改成服务端来源时
   *  漏了它，而两者在同一个面板里。 */
  const industryRows = usingLive
    ? (livePreview.industry ?? []).map((r) => ({
        industryCode: r.industryCode, value: formatCents(r.valueCents),
        sharePct: r.sharePct === null ? "—" : r.sharePct + "%",
        overCap: r.overCap,
      }))
    : draft.industry.map((r) => ({
        industryCode: r.industryCode, value: r.value,
        sharePct: r.sharePct, overCap: r.overCap,
      }));

  return (
    <Card
      title="我的模拟草稿"
      actions={
        usingLive
          ? <Badge tone="ok">服务端预览</Badge>
          : <Badge tone="warn">只读夹具 · 非服务端计算</Badge>
      }
    >
      {/* 用夹具时必须显眼地说出来：这些数字没有经过任何服务端计算，
          而下面的冻结按钮一旦按下，冻结的是服务端另算的一份计划。 */}
      {!usingLive && (
        <Callout tone="warn" title="当前显示的是只读演示数据，不是服务端计算结果">
          下方订单与金额来自随前端分发的示例夹具。点「请求服务端预览」后，
          这里会换成服务端算出的那一份——冻结的也是那一份。
          在此之前不要按这两个数字判断资金占用。
        </Callout>
      )}
      <div className="grid-3" style={{ marginBottom: 14 }}>
        <div className="stat">
          <span className="stat-label">预计资金占用</span>
          <span className="stat-value mono">{shownBuyTotal}</span>
        </div>
        <div className="stat">
          <span className="stat-label">预计费用</span>
          <span className="stat-value mono">{shownFees}</span>
        </div>
        <div className="stat">
          <span className="stat-label">执行后可用现金</span>
          <span className="stat-value mono">{shownCashAfter}</span>
        </div>
      </div>

      <h4 style={{ marginBottom: 6 }}>
        订单差异预览
        <span className="note">
          {usingLive ? "（来自服务端预览，冻结的就是这一份）"
                     : "（演示数据）"}
        </span>
      </h4>
      {rows.length === 0 ? (
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
              {rows.map((o) => (
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

      <h4 style={{ margin: "16px 0 6px" }}>
        行业分布（上限 {usingLive ? livePreview.industryCapPct : draft.industryCapPct}）
      </h4>
      {industryRows.length === 0 ? (
        <p className="note">暂无行业敞口。</p>
      ) : (
        <div className="table-wrap">
          <table className="data">
            <thead><tr><th>行业</th><th className="num">市值</th><th className="num">占比</th><th /></tr></thead>
            <tbody>
              {industryRows.map((r) => (
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

      {/* 服务端预览：与夹具来源不同，必须让使用者分得清哪份数字是服务端算的 */}
      <div style={{ marginTop: 16 }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
          <button className="btn btn-sm" onClick={onRequestPreview} disabled={offline}>
            请求服务端预览
          </button>
          {livePreview ? (
            <Badge tone="ok">服务端预览已生成 · {livePreview.orders.length} 笔</Badge>
          ) : (
            <span className="note">当前显示的是只读夹具数据</span>
          )}
        </div>
        {livePreview && (
          <Callout tone="info" title="服务端预览结果">
            <dl className="kv">
              <dt>计划 ID</dt><dd className="mono">{livePreview.planId}</dd>
              <dt>参考价日</dt>
              <dd>{livePreview.reference_price_day ?? "—"}
                <span className="note">（执行日之前，不使用当日价格）</span></dd>
              <dt>预计费用</dt><dd className="mono">{livePreview.estimatedFeesCents} 分</dd>
              <dt>状态</dt><dd><Badge tone="neutral">{livePreview.frozenLabel}</Badge></dd>
            </dl>
          </Callout>
        )}
        {offline && (
          <Callout tone="warn" title="写操作不可用">
            工作台 API 未运行，因此预览、确认与冻结都不可用。
            界面不会在离线时伪造一次成功的冻结。
          </Callout>
        )}
      </div>

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
            disabled={confirming || failed.length > 0 || draft.orders.length === 0 || offline}
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
