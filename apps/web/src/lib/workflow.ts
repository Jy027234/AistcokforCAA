import type { DecisionRow, PersistedPlanSummary, PreviewResponse, ResearchSimulationContext, ResearchSimulationDraft } from "./api";
import type { Draft } from "./types";

export interface ResearchSelection {
  requestId: number;
  instrumentId: string;
  researchSnapshotId: string | null;
}

/** A research selection is a request to check eligibility, never an order. */
export function checkResearchSelection(selection: ResearchSelection, context: ResearchSimulationContext): {
  acceptedId: string | null; message: string;
} {
  if (!selection.researchSnapshotId || selection.researchSnapshotId !== context.researchSnapshotId) {
    return { acceptedId: null, message: "研究资料已更新或缺少来源版本，请返回研究页重新查看后选择。原选择没有加入模拟方案。" };
  }
  const candidate = context.candidates.find(item => item.instrumentId === selection.instrumentId);
  if (!candidate) return { acceptedId: null, message: "这只证券尚未进入已核验的模拟范围。你可以继续研究，补齐资格后再生成草稿。" };
  if (!candidate.eligible) return { acceptedId: null, message: candidate.reasons.map(item => item.message).join("；") || "这只证券的模拟资格尚未通过。" };
  return { acceptedId: candidate.instrumentId, message: `${candidate.displayName} 已通过本次模拟资格核验，已加入待选方案；保存草稿后仍需你确认。` };
}

/** Original saved drafts stay immutable; current progress uses exact durable bindings. */
export function restoreResearchDrafts(drafts: ResearchSimulationDraft[], plans: PersistedPlanSummary[], decisions: DecisionRow[]) {
  return drafts.map(draft => {
    const originDecision = decisions.find(decision => {
      if (decision.portfolio_id !== draft.portfolioId || decision.snapshot_id !== draft.decisionSnapshotId || !decision.plan_id) return false;
      const payload = decision.human_final as { research_origin?: { sourceDraftId?: string; researchSnapshotId?: string } } | null;
      return payload?.research_origin?.sourceDraftId === draft.draftId && payload.research_origin.researchSnapshotId === draft.researchSnapshotId;
    });
    const planId = originDecision?.plan_id ?? draft.planId;
    const plan = plans.find(item => item.planId === planId && item.portfolioId === draft.portfolioId &&
      item.decisionSnapshotId === draft.decisionSnapshotId && item.tradingDay === draft.tradingDay);
    return plan ? { ...draft, planId: plan.planId, status: plan.status, frozen: true, confirmationAvailable: false,
      executionSnapshotId: plan.executionSnapshotId } : draft;
  });
}

export function summarizePlan(draft: Draft, livePreview: PreviewResponse | null,
  persistedStatus: "FROZEN" | "EXECUTED" | null, source: "api" | "fixture") {
  const computed = livePreview !== null || (source === "fixture" && draft.planId !== "");
  const orders = livePreview?.orders.length ?? draft.orders.length;
  const checks = livePreview?.rule_checks ?? draft.ruleChecks;
  const failed = checks.filter(item => !item.passed).length;
  const checked = computed && checks.length > 0;
  if (persistedStatus === "EXECUTED") return { computed, orders, failed, checked, label: "已模拟执行", detail: "查看成交、净值与对账结果，并在复盘中回看原判断。", action: "查看模拟结果", tone: "ok" as const };
  if (persistedStatus === "FROZEN" || draft.frozen) return { computed, orders, failed, checked, label: "已确认，待模拟执行", detail: "原方案已冻结，请按绑定的执行日继续模拟。", action: "继续模拟计划", tone: "accent" as const };
  if (!computed) return { computed, orders, failed, checked, label: "尚未评估模拟方案", detail: "先研究候选或继续已保存草稿；尚未计算不能判断是否需要调整。", action: "查看或创建模拟方案", tone: "neutral" as const };
  if (failed) return { computed, orders, failed, checked, label: "需要处理规则限制", detail: `${failed} 项检查未通过，查看原因后再决定如何调整。`, action: "查看限制与草稿", tone: "warn" as const };
  if (!orders) return { computed, orders, failed, checked, label: "本次预览没有订单", detail: "查看计算依据后，可明确记录维持当前组合的决定。", action: "查看预览依据", tone: "neutral" as const };
  return { computed, orders, failed, checked, label: "草稿待你确认", detail: "检查调整前后、预计费用与资金占用，确认后才会冻结。", action: "继续检查草稿", tone: "accent" as const };
}
