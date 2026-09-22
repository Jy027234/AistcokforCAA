import { useState } from "react";
import {
  api, AquantApiError,
  type CorporateActionOutcome, type ExecuteResponse, type FillRow,
  type LedgerResponse, type ReconcileResponse, type ValuationResponse,
} from "../lib/api";
import { formatCents } from "../lib/format";
import { Badge, Callout, Card } from "./ui";

/** 冻结之后的账本段：执行 -> 日终估值 -> 对账（主文档 §12、§15.1）。
 *
 * 这一段原先在界面上是断的：冻结成功之后就没有下一步，账本区直接写着
 * "尚未接入"。于是"用户不碰数据库就能走完一次完整流程"这件事在 M3 上
 * 并不成立——冻结了一个再也执行不了的计划。
 *
 * 三条界面原则：
 *   1. **不提前显示结果**。执行前不显示成交，估值前不显示净值。
 *   2. **每一步都可单独重试**，且失败原因来自服务端错误信封
 *      （错误码 + 修复动作），不是一句"操作失败"。
 *   3. **不变量用文字说清**。净值未发布时说明是哪条不变量没过，
 *      绝不因为"数字看起来正常"就把它显示成已发布。
 */
export function LedgerPanel({
  portfolioId, snapshotId, tradingDay, planId, frozen, persistedPlanStatus,
}: {
  portfolioId: string;
  snapshotId: string;
  tradingDay: string;
  planId: string | null;
  frozen: boolean;
  persistedPlanStatus: "FROZEN" | "EXECUTED" | null;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [executed, setExecuted] = useState<ExecuteResponse | null>(null);
  const [valuation, setValuation] = useState<ValuationResponse | null>(null);
  const [recon, setRecon] = useState<ReconcileResponse | null>(null);
  const [corporateActions, setCorporateActions] = useState(false);

  const explain = (err: unknown): string => {
    if (err instanceof AquantApiError) {
      return err.envelope.message + "（" + err.envelope.code + "）· 修复：" +
             err.envelope.repairAction;
    }
    return err instanceof Error ? err.message : String(err);
  };

  async function run<T>(step: string, fn: () => Promise<T>, onOk: (v: T) => void) {
    setBusy(step);
    setError(null);
    try {
      onOk(await fn());
    } catch (err) {
      setError(step + "失败：" + explain(err));
    } finally {
      setBusy(null);
    }
  }

  const onExecute = () => {
    if (!planId) return;
    return run("执行", () => api.execute(planId), (out) => {
      setExecuted(out);
      // 账户变了，旧的估值与对账就不再是当前状态，先清掉，
      // 免得界面把上一轮的净值继续挂在这里。
      setValuation(null);
      setRecon(null);
    });
  };

  const onValue = () =>
    run("日终估值", () => api.value({
      portfolio_id: portfolioId, snapshot_id: snapshotId, trading_day: tradingDay,
      ...(planId ? { execution_plan_id: planId } : {}),
    }), setValuation);

  const onReconcile = () =>
    run("对账", () => api.reconcile(portfolioId), setRecon);

  // ------------------------------------------------------- 账本明细
  const [ledger, setLedger] = useState<LedgerResponse | null>(null);

  const onLedger = () =>
    run("载入账本", () => api.ledger(portfolioId), setLedger);

  const stale = executed !== null && valuation === null;
  const hasExecuted = executed !== null || persistedPlanStatus === "EXECUTED";

  return (
    <Card
      title="执行、估值与对账"
      actions={
        <Badge tone={frozen ? "ok" : "neutral"}>
          {frozen ? "计划已冻结" : "尚未冻结"}
        </Badge>
      }
    >
      {!frozen && (
        <Callout tone="info" title="先冻结计划">
          执行只对已冻结的计划开放。冻结是一次用户动作，模型无权代替。
        </Callout>
      )}

      {frozen && (
        <>
          {persistedPlanStatus && (
            <Callout tone="info" title="已从服务端恢复计划">
              计划 <span className="mono">{planId}</span> 当前状态为 {persistedPlanStatus}；
              页面刷新不会丢失后续执行、估值和对账入口。
            </Callout>
          )}
          <div className="row-actions">
            <button className="btn btn-primary" onClick={onExecute}
                    disabled={busy !== null || hasExecuted}>
              {busy === "执行" ? "执行中…" : hasExecuted ? "本交易日已执行" : "执行本交易日"}
            </button>
            <button className="btn" onClick={onValue} disabled={busy !== null || !hasExecuted}>
              {busy === "日终估值" ? "估值中…" : "计算日终净值"}
            </button>
            <button className="btn" onClick={onReconcile} disabled={busy !== null}>
              {busy === "对账" ? "对账中…" : "逐项对账"}
            </button>
            <button className="btn" onClick={onLedger} disabled={busy !== null}>
              {busy === "载入账本" ? "载入中…" : "查看账本明细"}
            </button>
            <label className="checkbox">
              <input type="checkbox" checked={corporateActions}
                     onChange={(e) => setCorporateActions(e.target.checked)} />
              <span>当日有现金分红（除权/到账）</span>
            </label>
          </div>

          <p className="note" style={{ marginTop: 8 }}>
            执行按冻结时的订单顺序撮合：先卖后买，买入用开盘价加滑点，卖出用开盘价减滑点；
            涨停买入与跌停卖出默认不成交。这些是**约定下的模拟结果**，不是真实成交承诺。
          </p>

          {corporateActions && (
            <Callout tone="warn" title="分红只描述公司行为，不描述谁享有多少">
              权利由服务端按**登记日收盘持仓**计算。前端不提供股数或金额输入——
              能由调用方指定金额，就等于让调用方决定账本。
            </Callout>
          )}
        </>
      )}

      {error && (
        <div style={{ marginTop: 12 }}>
          <Callout tone="danger" title="这一步没有完成">{error}</Callout>
        </div>
      )}

      {executed && (
        <div style={{ marginTop: 16 }}>
          <h4>成交与未成交</h4>
          {executed.fills.length === 0 ? (
            <Callout tone="warn" title="本日没有任何成交">
              这不等于出错：涨停买入、跌停卖出、停牌或现金不足都会导致不成交。
              具体原因见下方订单状态。
            </Callout>
          ) : (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>方向</th><th>标的</th><th className="num">数量</th>
                    <th className="num">成交价</th><th className="num">金额</th>
                    <th className="num">费用</th>
                  </tr>
                </thead>
                <tbody>
                  {executed.fills.map((f: FillRow) => (
                    <tr key={f.fill_id}>
                      <td>
                        <Badge tone={f.side === "BUY" ? "accent" : "neutral"}>
                          {f.side === "BUY" ? "买入" : "卖出"}
                        </Badge>
                      </td>
                      <td className="mono">{f.instrument_id}</td>
                      <td className="num mono">{f.quantity.toLocaleString("en-US")}</td>
                      <td className="num mono">{formatCents(f.price_cents)}</td>
                      <td className="num mono">
                        {formatCents(f.price_cents * f.quantity)}
                      </td>
                      <td className="num mono">{formatCents(f.fees_total_cents)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {executed.rejections.length > 0 && (
            <>
              <h4 style={{ marginTop: 16 }}>未成交的订单</h4>
              <ul className="list">
                {executed.rejections.map((r, i) => (
                  <li key={i} className="note">
                    <span className="mono">{r.instrument_id ?? r.order_id ?? "-"}</span>
                    {" · "}
                    {r.reason_label ?? r.reason ?? "未成交"}
                    {r.detail ? "：" + r.detail : ""}
                  </li>
                ))}
              </ul>
            </>
          )}

          {executed.corporate_actions && executed.corporate_actions.length > 0 && (
            <>
              <h4 style={{ marginTop: 16 }}>当日公司行为</h4>
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>标的</th><th>阶段</th><th className="num">权利股数</th>
                      <th className="num">应收</th><th className="num">现金变动</th><th>说明</th>
                    </tr>
                  </thead>
                  <tbody>
                    {executed.corporate_actions.map((c: CorporateActionOutcome) => (
                      <tr key={c.action_id}>
                        <td className="mono">{c.instrument_id}</td>
                        <td>
                          <Badge tone={c.stage === "PAY_DATE" ? "ok" : "accent"}>
                            {c.stage === "EX_DATE" ? "除权日确认应收"
                              : c.stage === "PAY_DATE" ? "到账日转入现金"
                              : "本日无推进"}
                          </Badge>
                        </td>
                        <td className="num mono">
                          {c.entitlement_shares.toLocaleString("en-US")}
                        </td>
                        <td className="num mono">{formatCents(c.receivable_cents)}</td>
                        <td className="num mono">{formatCents(c.cash_delta_cents)}</td>
                        <td className="note">{c.note}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}

      {stale && (
        <div style={{ marginTop: 12 }}>
          <Callout tone="warn" title="账户已变动，需要重新估值">
            成交之后上一轮的净值与对账已不是当前状态，因此这里不再显示它们。
          </Callout>
        </div>
      )}

      {valuation && (
        <div style={{ marginTop: 16 }}>
          <h4>日终净值</h4>
          <div className="grid-3" style={{ marginBottom: 12 }}>
            <div className="stat">
              <span className="stat-label">净值合计</span>
              <span className="stat-value mono">{formatCents(valuation.net_value_cents)}</span>
            </div>
            <div className="stat">
              <span className="stat-label">可用现金</span>
              <span className="stat-value mono">
                {formatCents(valuation.cash_available_cents)}
              </span>
            </div>
            <div className="stat">
              <span className="stat-label">持仓市值</span>
              <span className="stat-value mono">
                {formatCents(valuation.positions_value_cents)}
              </span>
            </div>
          </div>

          <dl className="kv">
            <dt>估值快照</dt><dd className="mono">{valuation.snapshot_id}</dd>
            <dt>执行计划</dt><dd className="mono">{valuation.execution_plan_id ?? "—"}</dd>
            <dt>数据截止</dt><dd className="mono">{valuation.as_of}</dd>
            <dt>应收（已确认未到账）</dt>
            <dd className="mono">{formatCents(valuation.receivables_cents)}</dd>
            <dt>冻结现金</dt>
            <dd className="mono">{formatCents(valuation.cash_frozen_cents)}</dd>
            <dt>应付</dt><dd className="mono">{formatCents(valuation.payables_cents)}</dd>
            <dt>是否发布</dt>
            <dd>
              <Badge tone={valuation.published ? "ok" : "danger"}>
                {valuation.published ? "已发布" : "未发布"}
              </Badge>
              <span className="note" style={{ marginLeft: 6 }}>
                任一项校验不通过时净值不得发布
              </span>
            </dd>
          </dl>

          {!valuation.published && (
            <Callout tone="danger" title="净值未发布">
              {valuation.violations && valuation.violations.length > 0 ? (
                <ul className="list">
                  {valuation.violations.map((v, i) => (
                    <li key={i}>
                      <span className="mono">{v.code ?? "INVARIANT"}</span>
                      {" · "}{v.message ?? ""}
                      {v.repair_action ? " · 修复：" + v.repair_action : ""}
                    </li>
                  ))}
                </ul>
              ) : (
                "服务端未给出具体违反项，请查看后端日志。"
              )}
            </Callout>
          )}
        </div>
      )}

      {ledger && (
        <div style={{ marginTop: 16 }}>
          <h4>账本明细</h4>
          <p className="note">
            与"逐项对账"的分工不同：对账回答"对不对"（不变量），
            账本回答"是什么"（逐条事实）。
          </p>

          <div className="grid-3" style={{ marginTop: 10 }}>
            <div className="stat">
              <span className="stat-label">现金合计</span>
              <span className="stat-value mono">{ledger.cash.display}</span>
            </div>
            <div className="stat">
              <span className="stat-label">现金分录</span>
              <span className="stat-value mono">{ledger.cash.entry_count} 条</span>
            </div>
            <div className="stat">
              <span className="stat-label">持仓批次</span>
              <span className="stat-value mono">{ledger.lots.length} 个</span>
            </div>
          </div>

          <h4 style={{ marginTop: 14 }}>现金分录</h4>
          {ledger.cash.entries.length === 0 ? (
            <p className="note">这个账户还没有任何现金分录。</p>
          ) : (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr><th>日期</th><th>类型</th><th className="num">金额</th>
                    <th>备注</th></tr>
                </thead>
                <tbody>
                  {ledger.cash.entries.map((e) => (
                    <tr key={e.entry_id}>
                      <td className="mono">{e.trading_day}</td>
                      <td className="mono">{e.entry_type}</td>
                      <td className="num mono">{e.amount.display}</td>
                      <td className="note">{e.note ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h4 style={{ marginTop: 14 }}>持仓批次（T+1 依据）</h4>
          {ledger.lots.length === 0 ? (
            <p className="note">没有持仓批次。</p>
          ) : (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr><th>批次</th><th>标的</th><th>买入日</th>
                    <th>最早可卖</th><th className="num">原始</th>
                    <th className="num">剩余</th><th className="num">成本</th></tr>
                </thead>
                <tbody>
                  {ledger.lots.map((l) => (
                    <tr key={l.lot_id}>
                      <td className="mono">{l.lot_id}</td>
                      <td className="mono">{l.instrument_id}</td>
                      <td className="mono">{l.acquired_trading_day}</td>
                      <td className="mono">{l.earliest_sellable_day}</td>
                      <td className="num mono">{l.quantity_original.toLocaleString("en-US")}</td>
                      <td className="num mono">{l.quantity_remaining.toLocaleString("en-US")}</td>
                      <td className="num mono">{l.cost_basis.display}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {ledger.receivables.length > 0 && (
            <>
              <h4 style={{ marginTop: 14 }}>应收（分红）</h4>
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr><th>标的</th><th className="num">金额</th><th>口径</th>
                      <th>确认日</th><th>预计到账</th><th>状态</th></tr>
                  </thead>
                  <tbody>
                    {ledger.receivables.map((r) => (
                      <tr key={r.receivable_id}>
                        <td className="mono">{r.instrument_id}</td>
                        <td className="num mono">{r.amount.display}</td>
                        <td><Badge tone={r.tax_treatment === "PRE_TAX" ? "warn" : "ok"}>
                          {r.tax_treatment === "PRE_TAX" ? "税前口径" : r.tax_treatment}
                        </Badge></td>
                        <td className="mono">{r.recognized_on}</td>
                        <td className="mono">{r.expected_settlement_on}</td>
                        <td className="mono">{r.status}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="note" style={{ marginTop: 6 }}>
                红利税尚未实现（§12.6），因此一律标注税前口径，
                不得当作税后收益。
              </p>
            </>
          )}
        </div>
      )}

      {recon && (
        <div style={{ marginTop: 16 }}>
          <h4>对账</h4>
          <Callout tone={recon.reconciled ? "ok" : "danger"}
                  title={recon.reconciled ? "账本逐项一致" : "对账未通过"}>
            {recon.reconciled
              ? "现金、持仓、应收、费用与账本分录全部一致。"
              : "存在不一致项，明细见下表；在一致之前不应把这份账本当作事实。"}
          </Callout>

          <div className="grid-3" style={{ marginTop: 12 }}>
            <div className="stat">
              <span className="stat-label">账本现金</span>
              <span className="stat-value mono">{formatCents(recon.cash_cents)}</span>
            </div>
            <div className="stat">
              <span className="stat-label">未结应收</span>
              <span className="stat-value mono">{formatCents(recon.receivables_cents)}</span>
            </div>
            <div className="stat">
              <span className="stat-label">累计费用</span>
              <span className="stat-value mono">{formatCents(recon.fees_total_cents)}</span>
            </div>
          </div>

          <dl className="kv">
            <dt>成交笔数</dt><dd className="mono">{recon.fill_count}</dd>
            <dt>持仓</dt>
            <dd className="mono">
              {Object.keys(recon.positions).length === 0
                ? "无持仓"
                : Object.entries(recon.positions)
                    .map(([k, v]) => k + " × " + v.toLocaleString("en-US"))
                    .join("、")}
            </dd>
            <dt>估值现金与账本一致</dt>
            <dd>
              <Badge tone={recon.valuation_cash_matches_ledger ? "ok" : "danger"}>
                {recon.valuation_cash_matches_ledger ? "一致" : "不一致"}
              </Badge>
            </dd>
            <dt>估值应收与账本一致</dt>
            <dd>
              <Badge tone={recon.valuation_receivables_matches_ledger ? "ok" : "danger"}>
                {recon.valuation_receivables_matches_ledger ? "一致" : "不一致"}
              </Badge>
            </dd>
          </dl>

          <h4 style={{ marginTop: 14 }}>不变量</h4>
          <ul className="list">
            {Object.entries(recon.invariants)
              .filter(([k]) => k !== "violations")
              .map(([k, v]) => (
                <li key={k} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <Badge tone={v ? "ok" : "danger"}>{v ? "成立" : "不成立"}</Badge>
                  <span className="mono">{k}</span>
                </li>
              ))}
          </ul>
        </div>
      )}
    </Card>
  );
}
