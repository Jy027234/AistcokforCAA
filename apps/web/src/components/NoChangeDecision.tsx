import { useEffect, useRef, useState } from "react";
import { api, AquantApiError, type PreviewResponse } from "../lib/api";
import { Callout, Card } from "./ui";
import { formatAsOf } from "../lib/format";

export function NoChangeDecision({ portfolioId, snapshotId, preview, enabled, pendingFrozen,
  onSaved, onOpenReview, researchAsOf, disabledReason }: {
  portfolioId: string; snapshotId: string; preview: PreviewResponse | null; enabled: boolean;
  pendingFrozen: boolean; onSaved?: () => void; onOpenReview?: () => void;
  researchAsOf?: string;
  disabledReason?: string;
}) {
  const [reason, setReason] = useState("");
  const [external, setExternal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [savedId, setSavedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const submitting = useRef(false);
  const binding = `${portfolioId}:${snapshotId}`;
  const visibleBinding = useRef(binding);
  visibleBinding.current = binding;
  useEffect(() => { setSavedId(null); setError(null); setReason(""); setExternal(false); }, [portfolioId, snapshotId]);
  async function save() {
    if (submitting.current || savedId || !enabled || pendingFrozen || !reason.trim()) return;
    const submittedBinding = binding;
    submitting.current = true; setBusy(true); setError(null);
    try {
      const result = await api.recordNoChange({ portfolio_id: portfolioId, snapshot_id: snapshotId,
        reason_note: reason.trim(), external_information_used: external,
        model_proposed: preview ? { orders: preview.orders, snapshot_id: preview.snapshot_id,
          plan_id: preview.planId } : null,
        rule_check: preview ? { checks: preview.rule_checks } : null,
      });
      onSaved?.();
      if (visibleBinding.current === submittedBinding) setSavedId(result.decision_id);
    } catch (cause) {
      if (visibleBinding.current === submittedBinding) setError(cause instanceof AquantApiError ? `${cause.message} · ${cause.envelope.repairAction}` : String(cause));
    } finally { submitting.current = false; setBusy(false); }
  }
  return <Card title="记录维持当前模拟组合">
    <p>保存你此刻不调整的理由，供后续复盘。记录不会创建订单或撤销已有计划。</p>
    {researchAsOf && <p className="note" style={{ marginTop: 8 }}>本次依据的资料截止：{formatAsOf(researchAsOf)}</p>}
    {pendingFrozen && <Callout tone="warn" title="先处理已冻结计划">当前有待执行的冻结计划。请先确认其执行状态，再记录本次维持决定。</Callout>}
    {!enabled && <Callout tone="warn" title="当前不能保存决定">{disabledReason ?? "需要可用的工作台服务与已发布研究资料；演示数据保持只读。"}</Callout>}
    {savedId ? <Callout tone="ok" title="维持决定已保存">
      <p>{reason}</p><p className="note">你可以在复盘中查看这份原判断。</p>
      {onOpenReview && <div className="task-actions"><button className="btn" onClick={onOpenReview}>回看这次判断</button></div>}
      <details><summary>原记录标识</summary><p className="note mono">{savedId} · {snapshotId}</p></details>
    </Callout> : <>
      <label style={{ display: "grid", gap: 8, marginTop: 16 }}>维持理由
        <textarea className="input" rows={3} maxLength={1000} value={reason} onChange={event => setReason(event.target.value)}
          disabled={busy} placeholder="记录当前依据、主要风险以及继续观察的条件" />
      </label>
      <label className="checkbox" style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <input type="checkbox" checked={external} disabled={busy} onChange={event => setExternal(event.target.checked)} />使用了工作台以外的信息
      </label>
      <div className="task-actions"><button className="btn btn-primary" disabled={!enabled || pendingFrozen || busy || !reason.trim()} onClick={() => void save()}>
        {busy ? "保存中…" : "保存维持决定"}
      </button></div>
    </>}
    {error && <Callout tone="danger" title="决定尚未保存">{error}</Callout>}
  </Card>;
}
