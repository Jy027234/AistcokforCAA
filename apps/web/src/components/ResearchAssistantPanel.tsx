import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, AquantApiError, type AssistantAnswer, type AssistantQuestionId,
  type AssistantReadiness, type AssistantBudgetResult } from "../lib/api";
import { formatAsOf } from "../lib/format";
import { Callout } from "./ui";

export interface ResearchAssistantOption {
  selectionId: string;
  label: string;
  quote: string;
  note?: string;
  eligible: boolean;
}

export type ResearchAssistantBinding =
  | { kind: "ARCHIVED_CARD"; cardId: string | null }
  | { kind: "VERIFIED_SCAN"; snapshotId: string | null; instrumentId: string };

export function ResearchAssistantPanel({ binding, options, onOpenEvidence }: {
  binding: ResearchAssistantBinding;
  options: ResearchAssistantOption[];
  onOpenEvidence?: () => void;
}) {
  const [readiness, setReadiness] = useState<AssistantReadiness | null>(null);
  const [readinessError, setReadinessError] = useState<string | null>(null);
  const [loadingReadiness, setLoadingReadiness] = useState(true);
  const [questionId, setQuestionId] = useState<AssistantQuestionId>("EVIDENCE_SUMMARY");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [confirmed, setConfirmed] = useState(false);
  const [sending, setSending] = useState(false);
  const [answer, setAnswer] = useState<AssistantAnswer | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  const [sendBudget, setSendBudget] = useState<AssistantBudgetResult | null>(null);
  const [readinessAttempt, setReadinessAttempt] = useState(0);
  const bindingKey = binding.kind === "ARCHIVED_CARD"
    ? `card:${binding.cardId ?? ""}`
    : `scan:${binding.snapshotId ?? ""}:${binding.instrumentId}`;
  const mountedRef = useRef(false);
  const bindingKeyRef = useRef(bindingKey);
  const bindingReady = binding.kind === "ARCHIVED_CARD"
    ? Boolean(binding.cardId)
    : Boolean(binding.snapshotId && binding.instrumentId);
  const eligible = options.filter((item) => item.eligible && Boolean(item.selectionId) &&
    Boolean(item.quote.trim()) && item.quote.length <= 1000);

  useLayoutEffect(() => {
    bindingKeyRef.current = bindingKey;
  }, [bindingKey]);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  useEffect(() => {
    setSelectedIds([]);
    setConfirmed(false);
    setAnswer(null);
    setSendError(null);
    setSendBudget(null);
    setSending(false);
  }, [bindingKey]);

  useEffect(() => {
    let active = true;
    setReadiness(null);
    setReadinessError(null);
    if (!bindingReady) {
      setLoadingReadiness(false);
      return () => { active = false; };
    }
    setLoadingReadiness(true);
    api.assistantReadiness()
      .then((result) => { if (active) setReadiness(result); })
      .catch((error: unknown) => {
        if (!active) return;
        const message = error instanceof AquantApiError
          ? error.envelope.message
          : error instanceof Error ? error.message : String(error);
        setReadinessError(message);
      })
      .finally(() => { if (active) setLoadingReadiness(false); });
    return () => { active = false; };
  }, [bindingKey, bindingReady, readinessAttempt]);

  function toggleSelection(selectionId: string) {
    setSelectedIds((current) => {
      const next = current.includes(selectionId)
        ? current.filter((id) => id !== selectionId)
        : current.length < 3 ? [...current, selectionId] : current;
      setConfirmed(false);
      setAnswer(null);
      setSendError(null);
      setSendBudget(null);
      return next;
    });
  }

  async function sendQuestion() {
    if (!bindingReady || !readiness?.enabled || selectedIds.length === 0 || !confirmed) return;
    const requestBindingKey = bindingKey;
    setSending(true);
    setAnswer(null);
    setSendError(null);
    setSendBudget(null);
    try {
      const common = { question_id: questionId, confirm_external_send: true };
      const body = binding.kind === "ARCHIVED_CARD"
        ? { ...common, card_id: binding.cardId!, citation_ids: selectedIds }
        : { ...common, snapshot_id: binding.snapshotId!,
          instrument_id: binding.instrumentId, receipt_ids: selectedIds };
      const result = await api.assistantMessage(body);
      if (mountedRef.current && bindingKeyRef.current === requestBindingKey) {
        setAnswer(result);
        setSendBudget(null);
      }
    } catch (error) {
      if (!mountedRef.current || bindingKeyRef.current !== requestBindingKey) return;
      if (error instanceof AquantApiError) {
        setSendBudget(error.envelope.budget ?? null);
        setSendError(error.envelope.message +
          (error.envelope.repairAction ? " · " + error.envelope.repairAction : "") +
          (error.envelope.blockers?.map((blocker) => blocker.message).filter(Boolean).join("；")
            ? " · " + error.envelope.blockers.map((blocker) => blocker.message).filter(Boolean).join("；")
            : ""));
      } else {
        setSendBudget(null);
        setSendError(error instanceof Error ? error.message : String(error));
      }
    } finally {
      if (mountedRef.current && bindingKeyRef.current === requestBindingKey) setSending(false);
    }
  }

  const sendDisabled = !bindingReady || loadingReadiness || !readiness?.enabled ||
    eligible.length === 0 || selectedIds.length === 0 || !confirmed || sending;

  return (
    <section aria-label="受限研究助手" style={{ display: "grid", gap: 8,
      borderTop: "1px solid var(--border)", paddingTop: 12 }}>
      <h4 style={{ margin: 0 }}>受限研究助手</h4>
      <p className="note" style={{ margin: 0 }}>
        只分析这张研究卡中选定的可定位公开摘录；不发送完整文档、账户、持仓或成交资料。
      </p>
      {!bindingReady && (
        <Callout tone="warn" title="当前研究资料不可用于助手">
          请重新载入当前已验证的研究卡后再试。
        </Callout>
      )}
      {loadingReadiness && <p className="note" role="status">正在读取模型与费用配置状态…</p>}
      {readinessError && (
        <Callout tone="danger" title="助手状态暂不可用">
          {readinessError}。未发送任何材料。
          <div style={{ marginTop: 8 }}>
            <button className="btn btn-sm" disabled={loadingReadiness}
              onClick={() => setReadinessAttempt((attempt) => attempt + 1)}>
              {loadingReadiness ? "正在重试…" : "重试读取助手状态"}
            </button>
          </div>
        </Callout>
      )}
      {readiness?.budget?.configured && readiness.budget.usage?.status === "AVAILABLE" && (
        <p className="note">
          今日预算占用上界：已用 {typeof readiness.budget.usage.usageCostUpperBoundMicroCny === "number"
            ? formatAssistantMicroCny(readiness.budget.usage.usageCostUpperBoundMicroCny)
            : "暂无法核算"}，
          可用 {typeof readiness.budget.usage.remainingBudgetMicroCny === "number"
            ? formatAssistantMicroCny(readiness.budget.usage.remainingBudgetMicroCny)
            : "暂无法核算"}，
          每日上限 {formatAssistantMicroCny(readiness.budget.usage.dailyCapMicroCny)}。
          按登记的价格上界单价与供应商 usage 估算，不代表账单实扣金额。
          价格表 {readiness.budget.priceVersion}（{readiness.budget.priceEffectiveFrom}）。
          {readiness.budget.priceSource && (
            <> <a href={readiness.budget.priceSource} target="_blank" rel="noreferrer">查看官方价格来源</a></>
          )}
        </p>
      )}
      {readiness && !readiness.enabled && (
        <Callout tone="warn" title="助手暂不可用">
          <ul className="list">
            {readiness.blockingIssues.map((issue) => (
              <li key={issue.code}>{issue.message}{issue.repairAction ? " · " + issue.repairAction : ""}</li>
            ))}
          </ul>
        </Callout>
      )}
      {eligible.length === 0 ? (
        <p className="note">这张研究卡没有符合条件的可定位公开短摘录，不能发起助手请求。</p>
      ) : (
        <fieldset style={{ border: 0, padding: 0, margin: 0, display: "grid", gap: 6 }}
          disabled={!readiness?.enabled || !bindingReady || sending}>
          <legend className="note">选择最多 3 条依据（每条不超过 1,000 字）</legend>
          {eligible.map((item) => (
            <label key={item.selectionId} style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: 8 }}>
              <input type="checkbox" checked={selectedIds.includes(item.selectionId)}
                disabled={!selectedIds.includes(item.selectionId) && selectedIds.length >= 3}
                onChange={() => toggleSelection(item.selectionId)} />
              <span>
                <span>{item.label}</span>
                {item.note && <span className="note" style={{ display: "block" }}>{item.note}</span>}
                <span className="note" style={{ display: "block" }}>{item.quote}</span>
              </span>
            </label>
          ))}
        </fieldset>
      )}
      <label style={{ display: "grid", gap: 4 }}>
        <span className="note">研究问题</span>
        <select value={questionId} disabled={!readiness?.enabled || sending}
          onChange={(event) => {
            setQuestionId(event.target.value as AssistantQuestionId);
            setConfirmed(false);
            setAnswer(null);
            setSendError(null);
            setSendBudget(null);
          }}>
          {ASSISTANT_QUESTION_OPTIONS.map((option) => (
            <option key={option.id} value={option.id}>{option.label}</option>
          ))}
        </select>
      </label>
      <label style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
        <input type="checkbox" checked={confirmed}
          disabled={!readiness?.enabled || selectedIds.length === 0 || sending}
          onChange={(event) => setConfirmed(event.target.checked)} />
        <span className="note">
          我确认所选摘录为公开材料，并同意将固定研究问题和这些摘录发送至当前配置的第三方模型。
        </span>
      </label>
      <div>
        <button className="btn btn-sm btn-primary" disabled={sendDisabled}
          onClick={() => void sendQuestion()}>
          {sending ? "正在核验并发送…" : "询问所选依据"}
        </button>
      </div>
      {sendError && (
        <Callout tone="danger" title="助手没有返回可用回答">
          {sendError}
          {sendBudget?.status === "HELD" && (
            <p className="note">
              {sendBudget.message ?? "费用未知，预留额度仍占用；这不代表零费用。"}
              {typeof sendBudget.reservedUsageCostUpperBoundMicroCny === "number" &&
                ` 预算占用上界预留 ${formatAssistantMicroCny(
                  sendBudget.reservedUsageCostUpperBoundMicroCny)}。`}
            </p>
          )}
          {sendBudget?.status === "UNAVAILABLE" &&
            typeof sendBudget.usageCostUpperBoundMicroCny === "number" &&
            typeof sendBudget.dailyCapMicroCny === "number" && (
              <p className="note">
                今日预算占用上界已用 {formatAssistantMicroCny(
                  sendBudget.usageCostUpperBoundMicroCny)}，
                每日上限 {formatAssistantMicroCny(sendBudget.dailyCapMicroCny)}。
                按登记价格上界单价计算，不代表账单实扣金额。
                {typeof sendBudget.requestedUsageCostUpperBoundMicroCny === "number" &&
                  ` 本次预算占用上界 ${formatAssistantMicroCny(
                    sendBudget.requestedUsageCostUpperBoundMicroCny)}。`}
              </p>
            )}
        </Callout>
      )}
      {answer && (
        <Callout tone="info" title="助手回答">
          <p>{answer.text}</p>
          {answer.researchBinding && (
            <p className="note">研究资料快照：{answer.researchBinding.snapshotId} ·
              {answer.researchBinding.instrumentId} · 数据截至 {answer.researchBinding.dataAsOf}</p>
          )}
          <p className="note">正式数据快照：{answer.basedOn.snapshotId} · 截止 {formatAsOf(answer.basedOn.asOfTime)}</p>
          {answer.budget?.status === "SETTLED" && (
            <p className="note">
              本次预算占用上界 {typeof answer.budget.usageCostUpperBoundMicroCny === "number"
                ? formatAssistantMicroCny(answer.budget.usageCostUpperBoundMicroCny)
                : "暂无法核算"}；
              按登记价格上界单价与供应商 usage 计算，不代表账单实扣金额。
              {answer.budget.reservationExceeded &&
                (answer.budget.message ?? "模型 usage 超过调用前上界，已按登记价格上界计算预算占用。")}
            </p>
          )}
          {answer.budget?.status === "HELD" && (
            <p className="note" role="status">
              {answer.budget.message ?? "费用未知，预留额度仍占用；这不代表零费用。"}
              {typeof answer.budget.reservedUsageCostUpperBoundMicroCny === "number" &&
                ` 预算占用上界预留 ${formatAssistantMicroCny(
                  answer.budget.reservedUsageCostUpperBoundMicroCny)}。`}
            </p>
          )}
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {answer.citations.map((citation) => citation.url ? (
              <a key={citation.citationId} className="btn btn-sm btn-ghost"
                href={citation.url} target="_blank" rel="noreferrer">
                查看公告 {citation.title ?? citation.citationId}
              </a>
            ) : onOpenEvidence ? (
              <button key={citation.citationId} className="btn btn-sm btn-ghost"
                onClick={onOpenEvidence}>
                查看依据 {citation.citationId}
              </button>
            ) : null)}
          </div>
        </Callout>
      )}
    </section>
  );
}

const ASSISTANT_QUESTION_OPTIONS: { id: AssistantQuestionId; label: string }[] = [
  { id: "EVIDENCE_SUMMARY", label: "概括所选依据" },
  { id: "EVIDENCE_LIMITATIONS", label: "指出尚未证实的事项" },
  { id: "MISSING_INFORMATION", label: "说明还缺少哪些资料" },
];

function formatAssistantMicroCny(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value)
    ? `¥${(value / 1_000_000).toFixed(6)}` : "预算金额未知";
}
