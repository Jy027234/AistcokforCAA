import { useState } from "react";
import { api, AquantApiError, type DecisionRow, type PersistedPlanSummary } from "../lib/api";
import { decisionTypeLabel, formatAsOf, formatCents } from "../lib/format";
import { Badge, Callout } from "./ui";

function record(value: unknown): Record<string, unknown> | null {
  return value != null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}
const textValue = (value: unknown): string | null => typeof value === "string" ? value : null;
const numberValue = (value: unknown): number | null => typeof value === "number" && Number.isFinite(value) ? value : null;
const fieldLabels: Record<string, string> = {
  targets: "目标持仓", orders: "拟交易订单", estimated_fees_cents: "预计费用",
  estimatedFeesCents: "预计费用", cash_after_cents: "预计剩余现金", cashAfterCents: "预计剩余现金",
  proposal: "保存的估算方案", selection: "保存的证券选择",
};

export function OriginalDocument({ title, value }: { title: string; value: unknown }) {
  return <details><summary>{title}</summary>{value == null
    ? <p className="note">原记录未提供，不能用当前方案补写。</p>
    : <pre className="note mono" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", marginTop: 8 }}>{JSON.stringify(value, null, 2)}</pre>}</details>;
}

/** Render known, recorded proposal fields only; an unknown document remains inspectable in its original form. */
export function ProposalSummary({ value }: { value: unknown }) {
  const saved = record(value);
  if (!saved) return <p className="note">原记录未提供可展示的方案。</p>;
  // Frozen research decisions wrap the saved proposal and the independent human selection.
  // Read that envelope directly; targets are never used to reconstruct a missing selection.
  const proposal = Object.prototype.hasOwnProperty.call(saved, "proposal") ? record(saved.proposal) : saved;
  const selection = record(saved.selection);
  const selectedIds = Array.isArray(selection?.selectedInstrumentIds)
    && selection.selectedInstrumentIds.every(id => typeof id === "string") ? selection.selectedInstrumentIds as string[] : null;
  const selectionReason = textValue(selection?.selectionReason);
  const targets = Array.isArray(proposal?.targets) ? proposal.targets : null;
  const orders = Array.isArray(proposal?.orders) ? proposal.orders : null;
  const fees = numberValue(proposal?.estimatedFeesCents ?? proposal?.estimated_fees_cents);
  const cash = numberValue(proposal?.cashAfterCents ?? proposal?.cash_after_cents);
  if (targets == null && orders == null && fees == null && cash == null && selectedIds == null && selectionReason == null) {
    return <p className="note">此记录未提供标准持仓与订单字段，可展开原方案查看当时内容。</p>;
  }
  return <>
    {selectedIds != null && <p>已保存选择：<span className="mono">{selectedIds.join("、") || "原记录选择证券为空"}</span></p>}
    {selectionReason && <p>保存的选择理由：{selectionReason}</p>}
    <p className="note">{targets != null ? `目标持仓 ${targets.length} 只` : "未记录目标持仓"} · {orders != null ? `拟交易 ${orders.length} 笔` : "未记录拟交易订单"}</p>
    {(fees != null || cash != null) && <dl className="kv">{fees != null && <><dt>预计费用</dt><dd>{formatCents(fees)}</dd></>}
      {cash != null && <><dt>预计剩余现金</dt><dd>{formatCents(cash)}</dd></>}</dl>}
    {(targets != null || orders != null) && <details><summary>查看当时持仓与拟交易明细</summary>
    {targets != null && <><h4>目标持仓</h4>{targets.length === 0 ? <p className="note">原方案没有目标持仓。</p>
      : <ul className="list">{targets.map((target, i) => {
        const row = record(target);
        const id = textValue(row?.instrumentId ?? row?.instrument_id);
        const weight = row?.weightPct ?? row?.weight_pct;
        return <li key={i}>{id ?? "未记录证券"} · {typeof weight === "string" || typeof weight === "number" ? `${weight}%` : "未记录权重"}</li>;
      })}</ul>}</>}
    {orders != null && <><h4>拟交易订单</h4>{orders.length === 0 ? <p className="note">原方案没有拟交易订单。</p>
      : <div className="table-wrap"><table className="data"><thead><tr><th>证券</th><th>方向</th><th>数量</th><th>参考价</th></tr></thead>
        <tbody>{orders.map((order, i) => {
          const row = record(order);
          const side = textValue(row?.side);
          return <tr key={i}><td>{textValue(row?.instrumentId ?? row?.instrument_id) ?? "未记录"}</td>
            <td>{side === "BUY" ? "买入" : side === "SELL" ? "卖出" : side ?? "未记录"}</td>
            <td>{numberValue(row?.quantity) ?? "未记录"}</td><td>{formatCents(numberValue(row?.priceCents ?? row?.price_cents))}</td></tr>;
        })}</tbody></table></div>}</>}
    </details>}
    <p className="note">以上为当时方案中的估算，实际模拟成交与费用以对应计划账本为准。</p>
  </>;
}

function PlanResultEvidence({ decision, onOpenPortfolio }: { decision: DecisionRow; onOpenPortfolio?: () => void }) {
  const [loading, setLoading] = useState(false);
  const [checked, setChecked] = useState(false);
  const [plan, setPlan] = useState<PersistedPlanSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const check = async () => {
    setLoading(true); setError(null);
    try {
      const result = await api.plans(decision.portfolio_id);
      setPlan(result.plans.find(p => p.planId === decision.plan_id && p.portfolioId === decision.portfolio_id) ?? null);
      setChecked(true);
    } catch (e) {
      setError(e instanceof AquantApiError ? `${e.message} · ${e.envelope.repairAction}` : String(e));
    } finally { setLoading(false); }
  };
  return <>
    {decision.plan_id == null ? <p>本次记录未关联冻结计划，尚无可核验的执行结果。</p>
      : <><p>对应计划：<span className="mono">{decision.plan_id}</span></p>
        {plan ? <><Badge tone={plan.status === "EXECUTED" ? "ok" : "warn"}>
          {plan.status === "EXECUTED" ? "对应计划已完成模拟执行" : "对应计划已确认，等待模拟执行"}</Badge>
          <dl className="kv"><dt>执行交易日</dt><dd>{plan.tradingDay}</dd><dt>确认时刻</dt><dd>{plan.frozenAt ? formatAsOf(plan.frozenAt) : "记录未提供"}</dd></dl></>
          : <p className="note">{checked ? "计划列表没有找到本次关联计划，执行结果尚待核验。" : "尚未读取对应计划状态。"}</p>}
        <button className="btn" disabled={loading} onClick={() => void check()}>{loading ? "正在核验…" : "核验对应计划状态"}</button>
      </>}
    {error && <Callout tone="danger" title="计划状态读取失败">{error}</Callout>}
    <p className="note">成交与收益需由对应计划账本验证；本记录未提供计划级成交收益，不能用账户累计结果代替本次效果。</p>
    {onOpenPortfolio && <button className="btn" onClick={onOpenPortfolio}>前往模拟组合查看账本</button>}
  </>;
}

export function DecisionJourney({ decision: d, onOpenPortfolio }: { decision: DecisionRow; onOpenPortfolio?: () => void }) {
  const reasonLabels: Record<string, string> = { ACCEPT_MODEL: "采纳规则方案", MANUAL_SELECTION: "人工选择",
    MANUAL_RESEARCH_SELECTION: "基于研究的人工选择", NO_CHANGE: "维持当前组合" };
  const changed = [...new Set([...(d.diff.changed_keys ?? []), ...(d.diff.added_keys ?? []), ...(d.diff.removed_keys ?? [])])];
  return <>
    <div className="decision-journey-heading"><h3>{decisionTypeLabel(d.decision_type)}</h3>
      <Badge tone={d.external_information_used ? "warn" : "neutral"}>{d.external_information_used ? "声明使用外部信息" : "声明未使用外部信息"}</Badge></div>
    <p className="note">{formatAsOf(d.submitted_at)} · 账户 <span className="mono">{d.portfolio_id}</span></p>
    <div className="decision-journey">
      <div className="decision-journey-step"><h4>当时的规则方案</h4><ProposalSummary value={d.model_proposed} /></div>
      <div className="decision-journey-step"><h4>我的选择与理由</h4><p>{d.reason_note ?? "原记录未提供理由"}</p>
        {d.reason_category && <p className="note">理由分类：{reasonLabels[d.reason_category] ?? d.reason_category}</p>}
        <ProposalSummary value={d.human_final} />
        <p>{d.diff.identical ? "已保存的两份方案一致。" : d.diff.comparable
          ? `已保存的方案差异：${changed.length ? changed.map(key => fieldLabels[key] ?? key).join("、") : "记录未列出变化字段"}。`
          : d.diff.reason ?? "原方案不完整，无法比较。"}</p>
      </div>
      <div className="decision-journey-step"><h4>结果证据</h4><PlanResultEvidence decision={d} onOpenPortfolio={onOpenPortfolio} /></div>
    </div>
    <details><summary>原判断、方案与来源记录</summary>
      <dl className="kv"><dt>决策 ID</dt><dd className="mono">{d.decision_id}</dd>
        <dt>决策快照</dt><dd className="mono">{d.snapshot_id}</dd>
        <dt>冻结计划 ID</dt><dd className="mono">{d.plan_id ?? "未关联冻结计划"}</dd>
        <dt>提交原时刻</dt><dd className="mono">{d.submitted_at}</dd></dl>
      <OriginalDocument title="展开模型原方案" value={d.model_proposed} />
      <OriginalDocument title="展开人工最终方案" value={d.human_final} />
      <OriginalDocument title="展开当时规则检查" value={d.rule_check} />
      <OriginalDocument title="展开已保存差异与内容哈希" value={d.diff} />
    </details>
    <p className="note">未操作、选择、时机、持有期和暴露的收益效应尚不足以归因。原理由与方案保持保存时的内容。</p>
  </>;
}
