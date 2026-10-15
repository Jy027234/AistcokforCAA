import { useCallback, useEffect, useRef, useState } from "react";
import { api, AquantApiError, type PublishedSnapshot, type ResearchSimulationContext, type ResearchSimulationDraft } from "../lib/api";
import { formatAsOf, formatCents } from "../lib/format";
import { LedgerPanel } from "./LedgerPanel";
import { Badge, Callout, Card, Empty, Section } from "./ui";
import { checkResearchSelection, restoreResearchDrafts, type ResearchSelection } from "../lib/workflow";

const statusLabel = (status: string) => ({ WAITING_EXECUTION_SNAPSHOT: "等待执行日行情", BLOCKED: "资格或配置待补齐", READY_FOR_REVIEW: "已复核，待你确认", FROZEN: "已确认，待模拟执行", EXECUTED: "已模拟执行" }[status] ?? status);
const errorText = (e: unknown) => e instanceof AquantApiError ? `${e.message}（${e.envelope.code}）· ${e.envelope.repairAction}` : String(e);

export function ResearchSimulationPanel({ portfolioId, selectionRequest = null, active = true,
  onDraftsChange, onOpenResearch }: { portfolioId: string; selectionRequest?: ResearchSelection | null;
  active?: boolean; onDraftsChange?: (drafts: ResearchSimulationDraft[]) => void;
  onOpenResearch?: (instrumentId: string) => void;
}) {
  const [context, setContext] = useState<ResearchSimulationContext | null>(null);
  const [drafts, setDrafts] = useState<ResearchSimulationDraft[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [reason, setReason] = useState("");
  const [cash, setCash] = useState("1000000");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [snapshots, setSnapshots] = useState<PublishedSnapshot[]>([]);
  const [executionChoice, setExecutionChoice] = useState<Record<string, string>>({});
  const [frozenDraft, setFrozenDraft] = useState<ResearchSimulationDraft | null>(null);
  const bindingRef = useRef<string | null>(null);
  const handledRequest = useRef<number | null>(null);
  const liveReviews = useRef(new Map<string, ResearchSimulationDraft>());
  const [selectionNotice, setSelectionNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const readInFlight = useRef<Promise<void> | null>(null);
  const loadedPortfolio = useRef<string | null>(null);
  const requestedSelection = useRef<number | null>(null);
  const load = useCallback((refreshQualifications = true): Promise<void> => {
    if (readInFlight.current) {
      // A write must observe facts read after the write, not join an older read.
      return refreshQualifications ? readInFlight.current : readInFlight.current.then(() => load(false));
    }
    const work = (async () => {
    if (refreshQualifications) setLoading(true);
    // Resume saved work before starting the expensive current-qualification read.
    const [saved, catalog, plans, decisions] = await Promise.allSettled([
      api.researchSimulationDrafts(), api.snapshots(), api.plans(portfolioId), api.decisions(portfolioId),
    ]);
    const failures: string[] = [];
    if (saved.status === "fulfilled") {
      const rows = restoreResearchDrafts(saved.value.drafts.filter(item => item.portfolioId === portfolioId),
        plans.status === "fulfilled" ? plans.value.plans : [], decisions.status === "fulfilled" ? decisions.value.decisions : []).map(item => {
          const live = liveReviews.current.get(item.draftId);
          return !["FROZEN", "EXECUTED"].includes(item.status) && live && live.researchSnapshotId === item.researchSnapshotId &&
            live.decisionSnapshotId === item.decisionSnapshotId && live.tradingDay === item.tradingDay ? live : item;
        });
      setDrafts(rows); onDraftsChange?.(rows);
      setFrozenDraft(rows.find(item => item.source === "USER_SELECTION" && item.planId &&
        ["FROZEN", "EXECUTED"].includes(item.status) && item.executionSnapshotId) ?? null);
    }
    else { setDrafts([]); failures.push(errorText(saved.reason)); }
    if (catalog.status === "fulfilled") setSnapshots(catalog.value.snapshots);
    else { setSnapshots([]); failures.push(errorText(catalog.reason)); }
    if (plans.status === "rejected") failures.push("已确认计划的当前状态未读到：" + errorText(plans.reason));
    if (decisions.status === "rejected") failures.push("原判断与草稿绑定未读到：" + errorText(decisions.reason));
    setError(failures.length ? failures.join("；") : null);
    if (refreshQualifications) {
      try {
        const next = await api.researchSimulationContext();
        const binding = `${next.researchContentHash}:${next.decisionSnapshotId}`;
        if (bindingRef.current !== null && bindingRef.current !== binding) {
          setSelected([]); setReason(""); setSelectionNotice("研究来源已更新，原待选集合已清空；已保存草稿保留原来源。");
        }
        bindingRef.current = binding; setContext(next);
      } catch (error) { setContext(null); failures.push(errorText(error)); }
      setError(failures.length ? failures.join("；") : null);
      setLoading(false);
    }
    loadedPortfolio.current = portfolioId;
    })();
    readInFlight.current = work.finally(() => { readInFlight.current = null; });
    return readInFlight.current;
  }, [portfolioId, onDraftsChange]);
  useEffect(() => {
    if (!active) return;
    const newSelection = selectionRequest != null && requestedSelection.current !== selectionRequest.requestId;
    if (loadedPortfolio.current !== portfolioId || newSelection) {
      requestedSelection.current = selectionRequest?.requestId ?? null; void load();
    }
  }, [active, load, portfolioId, selectionRequest?.requestId]);
  useEffect(() => {
    if (!active || loading || !context || !selectionRequest || handledRequest.current === selectionRequest.requestId) return;
    handledRequest.current = selectionRequest.requestId;
    const checked = checkResearchSelection(selectionRequest, context);
    if (checked.acceptedId && !selected.includes(checked.acceptedId) && selected.length >= context.simulationParameters.maxHoldings) {
      setSelectionNotice("待选方案已达到持仓数量上限，请先移除一只证券后重新从研究页加入。"); return;
    }
    setSelectionNotice(checked.message);
    if (checked.acceptedId) setSelected(items => items.includes(checked.acceptedId!) ? items :
      items.length >= context.simulationParameters.maxHoldings ? items : [...items, checked.acceptedId!]);
  }, [active, loading, context, selectionRequest, selected]);
  async function create() {
    if (!context?.decisionSnapshotId || !context.decisionCutoffAt || !context.nextTradingDay) return;
    const cents = Math.round(Number(cash) * 100);
    if (!Number.isSafeInteger(cents) || cents <= 0) { setError("请输入有效的模拟初始现金。"); return; }
    setBusy(true); setNotice(null);
    try {
      const d = await api.createResearchSimulationDraft({ portfolioId, researchSnapshotId: context.researchSnapshotId,
        researchContentHash: context.researchContentHash, decisionSnapshotId: context.decisionSnapshotId,
        decisionCutoffAt: context.decisionCutoffAt,
        tradingDay: context.nextTradingDay, selectedInstrumentIds: selected, selectionReason: reason.trim(),
        excludedReasons: Object.fromEntries(context.candidates.filter(c => c.eligible && !selected.includes(c.instrumentId))
          .map(c => [c.instrumentId, "本次人工选择未纳入"])), initialCashCents: cents, source: "USER_SELECTION" });
      setNotice(`草稿已保存：${statusLabel(d.status)}。`); await load(false);
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  async function confirm(d: ResearchSimulationDraft) {
    if (!d.planId || !d.confirmationAvailable) return;
    setBusy(true);
    try {
      const token = await api.requestConfirmation(d.planId);
      const result = await api.freeze(d.planId, token.confirmationToken);
      const persistedId = typeof result.plan_id === "string" ? result.plan_id : d.planId;
      liveReviews.current.delete(d.draftId);
      setFrozenDraft({ ...d, planId: persistedId, frozen: true, status: "FROZEN", confirmationAvailable: false });
      setNotice("计划已确认并冻结，可在组合页查看账本与执行状态。"); await load(false);
    } catch (e) {
      if (e instanceof AquantApiError && [404, 409, 410].includes(e.status)) {
        liveReviews.current.delete(d.draftId); await load(false);
      }
      setError(errorText(e));
    } finally { setBusy(false); }
  }
  async function repreview(d: ResearchSimulationDraft, executionSnapshotId: string) {
    setBusy(true); setError(null); setNotice(null);
    try {
      const reviewed = await api.previewSavedResearchDraft(d.draftId, executionSnapshotId);
      liveReviews.current.set(d.draftId, reviewed);
      setDrafts(items => items.map(item => item.draftId === d.draftId ? reviewed : item));
      onDraftsChange?.(drafts.map(item => item.draftId === d.draftId ? reviewed : item));
      setNotice("已按原草稿与执行日快照重新复核。确认前请检查订单、费用和剩余现金。");
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  const maxHoldings = context?.simulationParameters.maxHoldings ?? 20;
  const shownCandidates = context?.candidates.filter(c => !c.reasons.some(r => r.code === "INSTRUMENT_NOT_IN_DECISION_SNAPSHOT")) ?? [];
  return <Section title="我的研究模拟方案" hint="延续研究选择，核验资格后保存原方案" dataSource="api"
    actions={<button className="btn" onClick={() => void load()} disabled={busy}>刷新资格与草稿</button>}>
    {error && <Callout tone="danger" title="模拟输入尚未就绪">{error}</Callout>}
    {notice && <Callout tone="info" title="模拟草稿">{notice}</Callout>}
    {selectionNotice && <Callout tone="info" title="研究选择已接续">{selectionNotice}</Callout>}
    {loading && <p className="note" aria-live="polite">正在核验当前资格与已保存方案…</p>}
    {context && <Card>
      <dl className="kv"><dt>研究行情日期</dt><dd>{context.researchDataAsOf}</dd>
        <dt>研究可得时间</dt><dd>{formatAsOf(context.researchAvailableAt)}</dd>
        <dt>已核验可模拟候选</dt><dd>{context.eligibleCandidateCount} 只</dd>
        <dt>下一执行交易日</dt><dd>{context.nextTradingDay ?? "交易日历尚未覆盖"}</dd></dl>
      {context.blockingIssues.map(i => <Callout key={i.code} tone="warn" title={i.code}>{i.message}{i.repairAction && ` · ${i.repairAction}`}</Callout>)}
      <p className="note">草稿使用已公布研究与参考收盘价。执行日行情未到达时，估算订单保留为待执行，不产生模拟成交。</p>
      <p className="note">你的研究选择与模拟资格分别核验。下面显示已有模拟输入的 {shownCandidates.length} 只，其他研究证券仍需补齐模拟证据。</p>
      <details style={{ marginTop: 10 }}><summary>仓位规则与完整研究范围</summary>
        <p className="note">初始目标每券 {context.simulationParameters.initialTargetPct}%，单券风险上限 {context.simulationParameters.maxSingleNamePct}%，行业上限 {context.simulationParameters.maxSingleIndustryPct}%，总股票仓位上限 {context.simulationParameters.maxTotalEquityPct}%。完整研究排名共 {context.researchCandidateCount} 只。</p>
      </details>
      <div className="table-wrap"><table className="data"><thead><tr><th>选择</th><th>候选</th><th>资格</th></tr></thead><tbody>
        {shownCandidates.map(c => <tr key={c.instrumentId}><td><input type="checkbox" aria-label={`选择 ${c.displayName}`}
          checked={selected.includes(c.instrumentId)} disabled={!c.eligible || busy || (!selected.includes(c.instrumentId) && selected.length >= maxHoldings)}
          onChange={e => setSelected(v => e.target.checked ? [...v, c.instrumentId] : v.filter(id => id !== c.instrumentId))} /></td>
          <td>{c.position}. {c.displayName}<div className="note mono">{c.instrumentId}</div>
            {onOpenResearch && <button className="btn btn-sm btn-ghost" onClick={() => onOpenResearch(c.instrumentId)}>研究依据</button>}</td>
          <td>{c.eligible ? <Badge tone="ok">可生成草稿</Badge> : <span className="note">{c.reasons.map(r => r.message).join("；")}</span>}</td></tr>)}
      </tbody></table></div>
      <div className="grid-2" style={{ marginTop: 12 }}><label>新账户初始现金（元）<input className="input" inputMode="decimal" value={cash} onChange={e => setCash(e.target.value)} /></label>
        <label>选择理由<input className="input" value={reason} onChange={e => setReason(e.target.value)} placeholder="记录本次选择依据" /></label></div>
      <p className="note">初始现金只用于新模拟账户；已有账户沿用账本资金。最多选择 {maxHoldings} 只。</p>
      <button className="btn btn-primary" disabled={loading || busy || !selected.length || !reason.trim() || !context.decisionSnapshotId || !context.decisionCutoffAt || !context.nextTradingDay || context.blockingIssues.length > 0}
        onClick={() => void create()}>生成并保存模拟草稿（{selected.length} 只）</button>
    </Card>}
    <div className="grid-2" style={{ marginTop: 12 }}>{drafts.length === 0 ? !loading && <Card><Empty title="尚无已保存方案">选择合格证券并写下理由后，保存第一份模拟草稿。</Empty></Card> : [...drafts].sort((a, b) => Number(a.source === "ENGINEERING_TRIAL") - Number(b.source === "ENGINEERING_TRIAL")).map(d => {
      const choices = snapshots.filter(s => s.tradingDay === d.tradingDay && s.qualityStatus !== "BLOCKING");
      const chosen = executionChoice[d.draftId] ?? choices[0]?.snapshotId ?? "";
      const projection = d.preview ? { orders: d.preview.orders.map(o => ({ instrumentId: o.instrument_id,
        side: o.side, quantity: o.quantity, priceCents: o.price_cents, instrument_id: undefined, price_cents: undefined })),
        estimatedFeesCents: d.preview.estimated_fees_cents, cashAfterCents: d.preview.cash_after_cents } : d.projection;
      const card = <Card key={d.draftId}>
      <div style={{ display: "flex", gap: 8 }}><h3>{d.source === "ENGINEERING_TRIAL" ? "真实数据工程试运行" : "人工选择草稿"}</h3><Badge tone="neutral">{statusLabel(d.status)}</Badge></div>
      <p>{d.selectionReason}</p><p className="note">执行日 {d.tradingDay} · {d.selectedInstrumentIds.join("、")}</p>
      {projection && <><dl className="kv"><dt>预计费用</dt><dd>{formatCents(projection.estimatedFeesCents)}</dd><dt>预计剩余现金</dt><dd>{formatCents(projection.cashAfterCents)}</dd></dl>
        <ul className="list">{projection.orders.map((o, i) => <li key={i}>{o.instrumentId ?? o.instrument_id} · {o.side} {o.quantity} 股 · 参考价 {formatCents(o.priceCents ?? o.price_cents ?? 0)}</li>)}</ul></>}
      {d.blockingIssues?.map(i => <p className="note" key={i.code}>{i.message}</p>)}
      <ol className="progress-steps" aria-label="方案进度">
        <li>1 保存选择</li><li aria-current={d.status === "WAITING_EXECUTION_SNAPSHOT" ? "step" : undefined}>2 等待执行行情</li>
        <li aria-current={d.status === "READY_FOR_REVIEW" ? "step" : undefined}>3 检查并确认</li>
        <li aria-current={["FROZEN", "EXECUTED"].includes(d.status) ? "step" : undefined}>4 模拟结果</li>
      </ol>
      {d.confirmationAvailable && <button className="btn btn-primary" disabled={busy} onClick={() => void confirm(d)}>确认并冻结此模拟计划</button>}
      {!d.confirmationAvailable && !["FROZEN", "EXECUTED"].includes(d.status) && choices.length > 0 && <>
        <button className="btn" disabled={busy || !chosen} onClick={() => void repreview(d, chosen)}>检查 {d.tradingDay} 模拟预览</button>
        <details><summary>查看或更换执行资料版本</summary>
        <label>执行日已发布快照<select className="input" value={chosen} disabled={busy}
          onChange={e => setExecutionChoice(v => ({ ...v, [d.draftId]: e.target.value }))}>
          {choices.map(s => <option key={s.snapshotId} value={s.snapshotId}>{s.tradingDay} · {s.snapshotId}</option>)}
        </select></label>
        </details>
      </>}
      {choices.length === 0 && !["FROZEN", "EXECUTED"].includes(d.status) && <p className="note">尚无 {d.tradingDay} 的执行行情。每日更新到齐后，刷新这里可复核原草稿；当前没有模拟成交。</p>}
      {d.source === "ENGINEERING_TRIAL" && <p className="note">工程选择用于验证链路，人工确认须来自你保存的选择。</p>}
      <details><summary>绑定来源</summary><p className="note mono">{d.draftId}<br />{d.researchSnapshotId}<br />{d.decisionSnapshotId}</p></details>
    </Card>;
      return d.source === "ENGINEERING_TRIAL" ? <details key={d.draftId} className="secondary-section"><summary>工程试运行方案 · {d.tradingDay}</summary>{card}</details> : card;
    })}</div>
    {frozenDraft?.planId && frozenDraft.executionSnapshotId && <LedgerPanel
      key={`${frozenDraft.planId}:${frozenDraft.executionSnapshotId}:${frozenDraft.tradingDay}`}
      portfolioId={frozenDraft.portfolioId} snapshotId={frozenDraft.executionSnapshotId}
      tradingDay={frozenDraft.tradingDay} planId={frozenDraft.planId} frozen={true}
      persistedPlanStatus={frozenDraft.status === "EXECUTED" ? "EXECUTED" : "FROZEN"}
      onChanged={() => { void load(false); }} />}
  </Section>;
}
