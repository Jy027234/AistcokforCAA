import type {
  DataStatus, ResearchCard, ResearchCandidatesResponse, S2DiagnosticPreviewResponse,
} from "./types";
import type { FinancialCoverageReport } from "./financialCoverage";

/** 工作台 API 客户端。
 *
 * 设计意图（主文档 §16.3、Q0 报告 §4.3）：
 *   确认主体不在这里指定——它由服务端从受信任凭证取得。
 *   前端只负责传递令牌，不负责声明"我是谁"。
 *
 * 所有错误都带 §16.4 的错误码与修复动作，界面据此显示可操作信息，
 * 而不是一句"操作失败"。
 */

export interface ApiError {
  code: string;
  message: string;
  objectId?: string;
  retryable: boolean;
  repairAction: string;
}

export interface ResearchSimulationContext {
  researchSnapshotId: string; researchContentHash: string;
  researchAvailableAt: string; researchDataAsOf: string;
  decisionSnapshotId: string | null; decisionCutoffAt: string | null;
  nextTradingDay: string | null; eligibleCandidateCount: number;
  researchCandidateCount: number;
  simulationParameters: { maxHoldings: number; initialTargetPct: string; maxSingleNamePct: string;
    maxSingleIndustryPct: string; maxTotalEquityPct: string };
  candidates: { instrumentId: string; displayName: string; position: number;
    eligible: boolean; reasons: { code: string; message: string; repairAction?: string }[] }[];
  blockingIssues: { code: string; message: string; repairAction?: string }[];
}

export interface ResearchSimulationDraft {
  draftId: string; portfolioId: string; source: string; status: string;
  researchSnapshotId: string; decisionSnapshotId: string; tradingDay: string;
  selectedInstrumentIds: string[]; selectionReason: string; createdAt?: string;
  planId: string | null; confirmationAvailable: boolean;
  executionSnapshotId?: string | null; requiresRepreview?: boolean;
  preview?: { orders: PreviewResponse["orders"]; estimated_fees_cents: number; cash_after_cents: number }; frozen?: boolean;
  blockingIssues?: { code: string; message: string }[];
  projection?: { orders: { instrumentId?: string; instrument_id?: string; side: string;
    quantity: number; priceCents?: number; price_cents?: number; grossCents?: number }[];
    estimatedFeesCents: number; cashAfterCents: number;
    targets?: { instrumentId?: string; instrument_id?: string; weightPct?: string; weight_pct?: string }[];
    notes?: string[] };
}

/** Actual engineering ledgers are distinct from user decisions and old replays. */
export interface EngineeringSimulationRun {
  runId: string; status: string; source: string; formalStrategy: false; humanConfirmed: false;
  draftId: string; portfolioId: string; tradingDay: string; initialCashCents?: number;
  decisionSnapshotId: string; decisionCutoffAt: string; executionSnapshotId?: string | null;
  executionCutoffAt?: string | null; draftCreatedAt: string; researchSnapshotId: string;
  researchContentHash: string; createdAt: string; completedAt?: string;
  planId?: string; delayedEngineeringCalculation?: boolean;
  cashCents?: number; positions?: Record<string, number>;
  orders?: { order_id: string; instrument_id: string; side: string; quantity: number;
    status: string; reject_reason?: string | null }[];
  fills?: { fill_id: string; instrument_id: string; side: string; quantity: number;
    price_cents: number; fees_total_cents: number }[];
  rejections?: Record<string, unknown>[];
  valuation?: { published: boolean; net_value_cents: number; cash_available_cents: number;
    positions_value_cents: number; receivables_cents: number; violations?: unknown[] };
  reconciliation?: { reconciled: boolean; fees_total_cents: number; fill_count: number };
  limitations: string[]; error?: { message?: string; code?: string }; blockingIssues?: { message: string }[];
}

export interface ForwardComparisonObservation {
  tradingDay: string; snapshotId: string; cutoffAt: string; observedAt: string;
  missingEodDays: string[]; delayedEngineeringCalculation: boolean;
  observationTargetReached: boolean; interpretation: string;
  attribution?: SimulationAttributionReport | null;
  portfolios: Record<string, { portfolioId: string | null; status: string; reason?: string;
    netValueCents?: number; cashCents?: number; netReturnPct?: string; cashPct?: string;
    equityPct?: string; feesTotalCents?: number; fillCount?: number; reconciled?: boolean;
    industryExposurePct?: Record<string, string | null>; executionPlanId?: string | null;
    dayExecution?: { tradingDay: string; recordedAt: string; orderCount: number; fillCount: number;
      unfilledOrderCount: number; feesTotalCents: number; grossTurnoverCents: number;
      orders: { order_id: string; instrument_id: string; side: string; quantity: number;
        status: string; reject_reason: string | null; created_at: string }[];
      fills: { fill_id: string; order_id: string; instrument_id: string; side: string; quantity: number;
        price_cents: number; gross_amount_cents: number; fees_total_cents: number; filled_at: string;
        feeCharges: { fee_code: string; amount_cents: number; fee_version: string }[] }[] } }>;
}
export interface SimulationAccountingPortfolio {
  portfolioId: string; initialCashCents: number; netValueCents: number; netReturnPct: string;
  accounting: { reconciled: boolean; total_net_pnl_cents: number; allocated_net_pnl_cents: number;
    unallocated_residual_cents: number; unallocated_reason: string | null;
    securities: { instrument_id: string; industry_code: string; price_pnl_cents: number;
      corporate_action_pnl_cents: number; fees_cents: number; net_pnl_cents: number; net_contribution_pct_initial_capital: string }[];
    industries: { industry_code: string; price_pnl_cents: number; corporate_action_pnl_cents: number; fees_cents: number; net_pnl_cents: number }[] };
  netAssetBridge: { initialCashCents: number; pricePnlCents: number; corporateActionPnlCents: number;
    otherSecurityCashCents: number; feesCents: number; unallocatedResidualCents: number; finalNetValueCents: number; reconciled: boolean };
  cashBridge: { initialCashCents: number; buySettlementCents: number; sellSettlementCents: number;
    feeCashCents: number; corporateActionCashCents: number; otherCashCents: number; finalCashCents: number; unallocatedResidualCents: number; reconciled: boolean };
  execution: { orderCount: number; fillCount: number; unfilledOrderCount: number;
    unfilledOrderPct: string | null; unfilledQuantityPct: string | null; reasons: Record<string, number>;
    targetCount: number; belowMinimumLotCount: number; grossTradedCents: number; turnoverGrossPctInitialCapital: string | null;
    noOrderTargets: { instrumentId: string; reason: string; weightPct: string; targetAmountCents: string; referencePriceCents: number | null; lotSize: number | null }[] };
  exposure: { cashPct: string; equityPct: string; industryExposurePct: Record<string, string>;
    largestIndustryPctEquity: string | null; industryHhiEquity: string | null };
  originalProposal: Record<string, unknown>; originalTargets: { instrument_id: string; industry_code: string; weight_pct: string }[];
  evidenceHash: string;
}
export interface SimulationAttributionReport {
  originId: string; originHash: string; subjectId: string; tradingDay: string; snapshotId: string;
  contentHash: string; createdAt: string; kind: string; basis: string; limitations: string[];
  portfolios: Record<string, SimulationAccountingPortfolio>;
  comparisons: Record<string, { left: string; right: string; interpretation: string;
    net_wealth_difference_cents: number; net_return_difference_pct_points: string;
    direct_fee_debit_difference_cents: number; remaining_wealth_difference_cents: number;
    unallocated_contribution_difference_cents: number; accounting_contribution_differences_reconciled: boolean;
    cash_pct_difference_points: string; added_final_holdings: string[]; removed_final_holdings: string[];
    security_accounting_contribution_differences: { instrument_id: string; industry_code: string;
      price_pnl_cents_difference: number; corporate_action_pnl_cents_difference: number; fees_cents_difference: number; net_pnl_cents_difference: number }[];
    industry_accounting_contribution_differences: { industry_code: string; net_pnl_cents_difference: number }[] }>;
  causalAttribution: { reason: string }; comparisonStatus: string;
}
export interface ForwardComparisonSeries {
  seriesId: string; experimentVersion: string; runKind: string; status: string;
  createdAt: string; firstTradingDay: string; decisionSnapshotId: string; decisionCutoffAt: string;
  researchSnapshotId: string; researchContentHash: string; researchAvailableAt: string;
  commonPool: string[]; commonPoolCount: number; initialCashCents: number;
  observedSessionCount: number; observationTargetSessions: number;
  human: { status: string; reason: string }; eventInterpretation: string;
  eventBuyBlockedInstrumentIds: string[]; benchmarkBudget: { equal_weight_pct: string; allocated_equity_pct: string };
  modelTargets: { instrument_id: string; industry_code: string; weight_pct: string }[];
  eventTargets: { instrument_id: string; industry_code: string; weight_pct: string }[];
  benchmarkTargets: { instrument_id: string; industry_code: string; weight_pct: string }[];
  constructionParams: Record<string, unknown>; fees: { commissionSource: string; firstDaySchedule: Record<string, unknown> };
  rebalancingPolicy: string; observations: ForwardComparisonObservation[]; limitations: string[];
  broadMarketReference: { status: string; reason: string };
}

export interface ComparisonResult {
  run_id: string; status: string; run_kind: string; human_origin: string; experiment_version: string;
  limitations: string[]; error?: { message: string }; pending_reason?: string;
  portfolios?: Record<string, { initial_cash_cents: number; final_net_value_cents: number;
    net_return_pct: string; max_drawdown_pct: string; fees_total_cents: number; fill_count: number;
    order_count?: number; unfilled_order_count?: number; turnover_gross_pct_initial_capital?: string | null;
    annualized_volatility_pct?: string | null; volatility_basis?: string;
    series: { trading_day: string; cash_pct: string; equity_pct: string; net_value_cents: number;
      industry_exposure_pct?: Record<string, string | null>; largest_industry_code?: string | null;
      largest_industry_pct_net_assets?: string | null; largest_industry_pct_equity?: string | null;
      industry_hhi_equity?: string | null }[];
    benchmark_difference?: { net_wealth_difference_cents: number; net_return_difference_pct_points: string;
      basis: string; broad_market_unavailable_reason: string };
    evaluation?: {
      market_exposure: { final_equity_pct: string; mean_observed_equity_pct: string;
        min_observed_equity_pct: string; max_observed_equity_pct: string; basis: string };
      industry_concentration: { final_largest_industry_code: string | null;
        final_largest_industry_pct_net_assets: string | null; final_largest_industry_pct_equity: string | null;
        final_hhi_equity: string | null; unknown_industry_pct_equity: string | null; basis: string };
      turnover: { gross_traded_cents: number; buy_traded_cents: number; sell_traded_cents: number;
        mean_observed_net_assets_cents: string; gross_pct_mean_observed_assets: string | null; basis: string };
      unfilled: { order_count: number; unfilled_order_count: number; unfilled_order_pct: string | null;
        requested_quantity_shares: number; filled_quantity_shares: number; unfilled_quantity_pct: string | null;
        reasons: Record<string, number>; basis: string };
      capacity_proxy: { status: string; lookback_trading_days: number; simulator_participation_cap_pct: string;
        order_count: number; usable_adv_order_count: number; full_lookback_order_count: number;
        max_requested_participation_pct: string | null; max_filled_participation_pct: string | null;
        volume_cap_rejection_count: number; basis: string; reason: string | null;
        capital_capacity_unavailable_reason: string };
      volatility: { observed_interval_sample_std_pct: string | null; return_interval_count: number;
        daily_intervals: boolean; annualized_pct: string | null; annualized_unavailable_reason: string | null;
        basis: string; sharpe_unavailable_reason: string };
    } }>;
  observation_basis?: { sample_count?: number; missing_eod_days?: string[]; continuous_eod?: boolean;
    drawdown_basis?: string; observed_days?: string[]; contiguous_daily_eod?: boolean };
  portfolio_runs?: Record<string, { days: { trading_day: string; target_count: number; order_count: number;
    fill_count: number; below_minimum_lot_target_count?: number; below_minimum_lot_instruments?: string[] }[] }>;
  event_shadow?: { interpretation?: string; rule_triggered?: boolean };
  human_difference?: { interpretation: string; net_wealth_difference_cents: number;
    direct_fee_debit_difference_cents: number; remaining_wealth_difference_cents: number;
    added_final_holdings?: string[]; removed_final_holdings?: string[]; cash_pct_difference_points?: string;
    industry_exposure_difference_pct_points?: Record<string, string>;
    unknown_reason: string; accounting_contribution_differences_reconciled?: boolean;
    security_accounting_contribution_differences?: { instrument_id: string; industry_code: string;
      price_pnl_cents_difference: number; corporate_action_pnl_cents_difference: number;
      fees_cents_difference: number; net_pnl_cents_difference: number }[];
    industry_accounting_contribution_differences?: { industry_code: string; net_pnl_cents_difference: number }[] };
}

export interface FeeStatus {
  snapshotId: string;
  feeVersion: string;
  syntheticTestRate: boolean;
  commissionSource: "USER_CONFIGURED" | "USER_APPROVED_ASSUMPTION" |
    "UNCONFIGURED_DEFAULT" | string;
  commissionRate: string;
  commissionMinCents: number;
  stampDutyRateSell: string;
  transferFeeRate: string;
  provenance: {
    item: string;
    value: string | null;
    effectiveFrom: string | null;
    authority: string | null;
    kind: string;
    note: string;
  }[];
  note: string;
}

export interface TrialReadiness {
  mode: string;
  ready: boolean;
  blockingIssues: {
    code: string;
    message: string;
    repairAction: string;
  }[];
  operationalWarnings: {
    code: string;
    message: string;
    repairAction: string;
  }[];
}

export interface ReadinessResponse {
  ready: boolean;
  identity: Record<string, unknown>;
  data: Record<string, unknown>;
  jobs: Record<string, number>;
  trial: TrialReadiness;
  note: string;
}

/** API 基地址。
 *
 * 优先取运行时注入的 `window.__AQUANT_API_BASE__`，其次取构建期的
 * `VITE_API_BASE`，最后回落到同源（由 Vite 代理或反向代理转发 /api）。
 *
 * 为什么要有运行时那一层：同一份构建产物需要在不同环境指向不同后端。
 * 只认构建期变量的话，验证时就得为每个环境重新构建一次，
 * 而"验证用的产物"和"部署用的产物"不是同一份，验证的意义就打折了。
 */
declare global {
  interface Window { __AQUANT_API_BASE__?: string }
}

const BASE =
  (typeof window !== "undefined" ? window.__AQUANT_API_BASE__ : undefined) ??
  (import.meta.env.VITE_API_BASE as string | undefined) ??
  "";

/** 演示用的受信任主体头。
 *
 * 真实部署应由服务端从已验证凭证映射（见 Q0 报告 §4.3：
 * tenant 与 user 必须来自令牌，而不是请求体）。这里保留一个显式开关，
 * 便于在没有身份提供方的本地环境里演示，并让"这是演示接线"一望可知。
 */
const DEMO_SUBJECT = "user:demo";

function headers(): HeadersInit {
  return { "Content-Type": "application/json", "X-Aquant-Subject": DEMO_SUBJECT };
}

export class AquantApiError extends Error {
  readonly status: number;
  readonly envelope: ApiError;

  constructor(status: number, envelope: ApiError) {
    super(envelope.message);
    this.name = "AquantApiError";
    this.status = status;
    this.envelope = envelope;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(BASE + path, { ...init, headers: headers() });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    throw new AquantApiError(0, {
      code: "NETWORK_ERROR",
      message: "无法连接到工作台服务：" + message,
      retryable: true,
      repairAction: "确认 API 已启动，然后重试",
    });
  }

  const text = await res.text();
  let payload: unknown = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }

  if (!res.ok) {
    const body = payload as Record<string, unknown> | null;
    const nested = (body?.detail ?? body) as Record<string, unknown> | null;
    const envelope = (nested?.error ?? nested) as Partial<ApiError> | null;
    throw new AquantApiError(res.status, {
      code: envelope?.code ?? "HTTP_" + res.status,
      message: envelope?.message ?? (typeof body?.detail === "string" ? body.detail : "请求被拒绝（HTTP " + res.status + "）"),
      objectId: envelope?.objectId,
      retryable: envelope?.retryable ?? false,
      repairAction: envelope?.repairAction ?? "检查输入后重试",
    });
  }
  return payload as T;
}

export interface PreviewResponse {
  planId: string;
  plan_id: string;
  portfolio_id: string;
  snapshot_id: string;
  trading_day: string;
  decisionSnapshotId: string | null;
  decisionCutoffAt: string | null;
  executionSnapshotId: string | null;
  executionCutoffAt: string | null;
  reference_price_day: string | null;
  plan_version: string;
  account_version: string;
  frozen: boolean;
  frozenLabel: string;
  orders: {
    instrument_id: string; side: string; quantity: number;
    price_cents: number; gross_cents: number; rationale?: string;
    reference_price_day?: string;
  }[];
  estimatedFeesCents: number;
  estimated_fees_cents: number;
  /** 按预览订单执行后的可用现金（分）。**服务端算**，界面只做显示换算。 */
  cashAfterCents: number;
  /** 行业分布（服务端算）。缺了它，界面只能拿夹具的行业数值填空。 */
  industry?: {
    industryCode: string; valueCents: number;
    sharePct: string | null; overCap: boolean;
  }[];
  industryCapPct?: string;
  rule_checks: { order: string; check: string; passed: boolean; detail?: string }[];
  excluded: { instrument_id?: string; instrumentId?: string; reason: string; detail?: string }[];
  cash_weight_pct: string;
  notes: string[];
}

export interface PublishedSnapshot {
  snapshotId: string;
  kind: string;
  asOfTime: string;
  inputCutoffAt: string;
  publishedAt: string | null;
  dataMode: string;
  qualityStatus: string;
  /** 执行交易日由服务端根据快照元数据给出，不能由用户自由填写。 */
  tradingDay: string | null;
  datasetSummary: {
    name: string; recordCount: number; coverage: number | null;
    asOfUpperBound: string;
  }[];
  capabilities: {
    s1Decision: {
      available: boolean;
      code: string;
      message: string;
      repairAction: string | null;
    };
  };
}

export interface SnapshotCatalogResponse {
  count: number;
  snapshots: PublishedSnapshot[];
}

export interface PlanTimingSelection {
  decisionSnapshotId: string;
  decisionCutoffAt: string;
  executionSnapshotId: string;
  executionCutoffAt: string;
  tradingDay: string;
}

export interface PersistedPlanSummary {
  planId: string;
  portfolioId: string;
  status: "FROZEN" | "EXECUTED";
  snapshotId: string;
  decisionSnapshotId: string;
  decisionCutoffAt: string;
  executionSnapshotId: string;
  executionCutoffAt: string;
  tradingDay: string;
  createdAt: string;
  frozenAt: string | null;
  expiresAt: string | null;
  confirmedBy: string | null;
  planVersion: string;
}

export interface DividendInput {
  action_id: string;
  instrument_id: string;
  record_date: string;
  ex_date: string;
  pay_date: string;
  cash_per_share_cents: number;
  tax_treatment?: "PRE_TAX" | "CONSERVATIVE" | "VERIFIED";
}

export interface FillRow {
  fill_id: string;
  order_id: string;
  instrument_id: string;
  side: string;
  quantity: number;
  price_cents: number;
  fees_total_cents: number;
}

export interface CorporateActionOutcome {
  action_id: string;
  instrument_id: string;
  /** EX_DATE = 当日确认应收；PAY_DATE = 当日转入现金；null = 当日无推进 */
  stage: "EX_DATE" | "PAY_DATE" | null;
  receivable_cents: number;
  cash_delta_cents: number;
  entitlement_shares: number;
  note: string;
}

export interface ExecuteResponse {
  plan_id: string;
  status: string;
  trading_day: string;
  fills: FillRow[];
  rejections: { order_id?: string; instrument_id?: string; reason?: string;
                reason_label?: string; detail?: string }[];
  cash_entries: { entry_type: string; amount_cents: number }[];
  lots_created: string[];
  corporate_actions?: CorporateActionOutcome[];
}

export interface ValuationResponse {
  portfolio_id?: string;
  snapshot_id: string;
  execution_plan_id: string | null;
  as_of: string;
  trading_day: string;
  cash_available_cents: number;
  cash_frozen_cents: number;
  receivables_cents: number;
  positions_value_cents: number;
  payables_cents: number;
  net_value_cents: number;
  published: boolean;
  invariants?: Record<string, boolean>;
  violations?: { code?: string; message?: string; repair_action?: string }[];
}

export interface ReconcileResponse {
  portfolio_id: string;
  cash_cents: number;
  positions: Record<string, number>;
  receivables_cents: number;
  fill_count: number;
  fees_total_cents: number;
  invariants: Record<string, unknown> & { violations?: unknown[] };
  valuation_cash_matches_ledger: boolean;
  valuation_receivables_matches_ledger: boolean;
  reconciled: boolean;
}

export interface LedgerEntry {
  entry_id: string;
  entry_type: string;
  amount: { cents: number | null; display: string };
  trading_day: string;
  occurred_at: string;
  note: string | null;
}

export interface LedgerLot {
  lot_id: string;
  instrument_id: string;
  acquired_trading_day: string;
  earliest_sellable_day: string;
  quantity_original: number;
  quantity_remaining: number;
  cost_basis: { cents: number | null; display: string };
}

export interface LedgerResponse {
  portfolio_id: string;
  kind: string;
  account_type: string;
  status: string;
  opened_at: string;
  cash: {
    cents: number | null;
    display: string;
    entry_count: number;
    entries: LedgerEntry[];
  };
  positions: { instrument_id: string; quantity: number }[];
  lots: LedgerLot[];
  fills: {
    fill_id: string; instrument_id: string; side: string; quantity: number;
    price: { cents: number | null; display: string };
    fees_total: { cents: number | null; display: string };
    trading_day: string;
  }[];
  fees_by_code: { fee_code: string; total: { cents: number | null; display: string } }[];
  receivables: {
    receivable_id: string; instrument_id: string; kind: string;
    amount: { cents: number | null; display: string };
    tax_treatment: string; recognized_on: string;
    expected_settlement_on: string; settled_on: string | null; status: string;
  }[];
}

export interface WatchItem {
  instrument_id: string;
  added_at: string;
  note: string | null;
  short_name: string | null;
  exchange: string | null;
  board: string | null;
}

export interface DecisionRow {
  decision_id: string;
  portfolio_id: string;
  plan_id: string | null;
  snapshot_id: string;
  decision_type: string;
  model_proposed: unknown | null;
  human_final: unknown | null;
  rule_check: unknown | null;
  diff: {
    comparable?: boolean; identical?: boolean; changed_keys?: string[];
    removed_keys?: string[]; added_keys?: string[];
    model_hash?: string; human_hash?: string; reason?: string;
  };
  reason_category: string | null;
  reason_note: string | null;
  external_information_used: boolean;
  submitted_at: string;
}

export interface StrategyVersionRow {
  strategy_version: string;
  family: string;
  frozen_at: string;
  spec_hash: string;
  notes: string | null;
}

export interface StrategyFamilyGate {
  family: string;
  registrationAvailable: boolean;
  error: { code: string; message: string; repair_action: string };
  trialImpact: {
    blocksInitialS1Trial: boolean;
    blockedCapabilities: string[];
    availableCapabilities: string[];
  } | null;
}

export interface StrategyVersionsResponse {
  strategyVersions: StrategyVersionRow[];
  familyGates: StrategyFamilyGate[];
  featureVersions: { featureVersion: string; factor_id?: string }[];
  note: string;
}

export interface FactorRowValue {
  instrument_id: string;
  factor_id: string;
  raw_value: number | null;
  cross_sectional_rank: number | null;
  exclusion_reason: string | null;
  coverage_ratio: number | null;
}

export interface CandidateResponseRow {
  instrumentId: string;
  displayName: string | null;
  industryCode: string | null;
  signalRank: number;
  simulatable: boolean;
}

export interface CandidatesResponse {
  snapshotId: string;
  candidates: CandidateResponseRow[];
  note: string;
}

export interface EventResponseRow {
  event_id: string;
  category: string;
  summary: string;
  available_at: string | null;
  verification_status: string;
  market_direction: string | null;
  subjects: { subject_type: string; subject_id: string; role: string | null }[];
}

export interface EventsResponse {
  snapshotId: string;
  asOfTime: string;
  count: number;
  note: string;
  events: EventResponseRow[];
}

export interface ResearchRunResponse {
  research_run_id: string;
  researchRunId: string;
  snapshotId: string;
  asOfTime: string;
  factorId: string;
  factorName: string;
  stored: number;
  valued: number;
  excluded: number;
  factors: string[];
  financialStatements: number;
  exclusionBreakdown: Record<string, number>;
  note: string;
}

export interface ExperimentRow {
  experiment_id: string;
  hypothesis: string;
  registered_at: string;
  status: string;
  test_set_access_count: number;
  primary_metric: string | null;
  outcome_notes: string | null;
}

/** 每日任务的调度状态（§14.2）。
 *
 * 注意 `lastRun` 为 null 表示**没有任何运行记录**，而不是"跑了但结果是 0"：
 * 两者在界面上必须区分开。 */
export interface ScheduleStatus {
  schedule: {
    enabled: boolean;
    runAtLocal: string;
    weekdaysOnly: boolean;
    interpreter: string;
    dataDir: string;
    windowStart: string;
    updatedAt: string | null;
    updatedBy: string | null;
  };
  /** 下一次触发时刻（ISO）。停用时为 null——不显示一个假的下一次。 */
  nextFireAt: string | null;
  lastRun: {
    requestId: string | null;
    source: string | null;
    tradingDay: string | null;
    snapshotId: string | null;
    outcome: string | null;
    reason: string | null;
    exitCode: number | null;
    startedAt: string | null;
    finishedAt: string | null;
    durationSeconds: number | null;
    steps: { step: string; ok: boolean; seconds: number | null }[];
  } | null;
  /** 已登记但尚未执行完的请求（界面按钮与到点触发共用这一条通道）。 */
  pending: {
    requestId: string; source: string; status: string;
    requestedAt: string; requestedBy: string | null; reason: string | null;
  } | null;
  checkedAt: string;
}


export const api = {
  financialCoverage: () => request<FinancialCoverageReport>("/api/v1/research/financial-coverage"),
  engineeringSimulationRuns: () => request<{ runs: EngineeringSimulationRun[] }>("/api/v1/engineering/simulation/runs"),
  engineeringAttributions: () => request<{ reports: SimulationAttributionReport[] }>("/api/v1/engineering/simulation/attributions"),
  forwardComparisons: () => request<{ series: ForwardComparisonSeries[] }>("/api/v1/comparisons/forward"),
  comparisons: () => request<{ results: ComparisonResult[] }>("/api/v1/comparisons"),
  researchSimulationContext: () => request<ResearchSimulationContext>("/api/v1/research/simulation/context?limit=5000"),
  researchSimulationDrafts: () => request<{ drafts: ResearchSimulationDraft[] }>("/api/v1/research/simulation/drafts"),
  previewSavedResearchDraft: (draftId: string, executionSnapshotId: string) =>
    request<ResearchSimulationDraft>(`/api/v1/research/simulation/drafts/${encodeURIComponent(draftId)}/preview`, {
      method: "POST", body: JSON.stringify({ executionSnapshotId }),
    }),
  createResearchSimulationDraft: (body: {
    portfolioId: string; researchSnapshotId: string; researchContentHash: string;
    decisionSnapshotId: string; decisionCutoffAt: string; tradingDay: string; selectedInstrumentIds: string[];
    selectionReason: string; excludedReasons: Record<string, string>; initialCashCents: number;
    source: "USER_SELECTION" | "ENGINEERING_TRIAL";
  }) => request<ResearchSimulationDraft>("/api/v1/research/simulation/draft", {
    method: "POST", body: JSON.stringify(body),
  }),
  health: () => request<{ status: string }>("/api/v1/health"),

  status: () => request<DataStatus>("/api/v1/status"),

  readiness: () => request<ReadinessResponse>("/api/v1/readiness"),

  fees: () => request<FeeStatus>("/api/v1/fees"),

  snapshots: () => request<SnapshotCatalogResponse>("/api/v1/snapshots"),

  candidates: (snapshotId?: string) => request<CandidatesResponse>(
    "/api/v1/candidates" + (snapshotId
      ? "?snapshot_id=" + encodeURIComponent(snapshotId) : ""),
  ),

  events: () => request<EventsResponse>("/api/v1/events"),

  preview: (body: {
    portfolio_id: string; snapshot_id: string; trading_day: string;
    decision_snapshot_id?: string; decision_cutoff_at?: string;
    execution_snapshot_id?: string; selected_instrument_ids?: string[];
  }) =>
    request<PreviewResponse>("/api/v1/plans/preview", {
      method: "POST", body: JSON.stringify(body),
    }),

  /** 签发一次性确认令牌。令牌由服务端生成，绑定该次预览。 */
  requestConfirmation: (planId: string) =>
    request<{ planId: string; confirmationToken: string; subject: string; note: string }>(
      "/api/v1/plans/" + encodeURIComponent(planId) + "/confirmation",
      { method: "POST" },
    ),

  freeze: (planId: string, token: string) =>
    request<Record<string, unknown>>(
      "/api/v1/plans/" + encodeURIComponent(planId) + "/freeze",
      { method: "POST", body: JSON.stringify({ plan_id: planId, confirmation_token: token }) },
    ),

  plans: (portfolioId: string) =>
    request<{ portfolioId: string; count: number; plans: PersistedPlanSummary[] }>(
      "/api/v1/plans?portfolio_id=" + encodeURIComponent(portfolioId),
    ),

  /** 执行已冻结的计划。
   *
   * corporate_actions 只描述"当天有哪些公司行为"，**不描述谁享有多少**：
   * 权利由服务端按登记日收盘持仓计算。前端能提供股数或金额就等于
   * 让调用方决定账本，那正是这条链路要防住的事。
   */
  execute: (planId: string, corporateActions: DividendInput[] = []) =>
    request<ExecuteResponse>(
      "/api/v1/plans/" + encodeURIComponent(planId) + "/execute",
      {
        method: "POST",
        body: JSON.stringify({ plan_id: planId, corporate_actions: corporateActions }),
      },
    ),

  value: (body: { portfolio_id: string; snapshot_id: string; trading_day: string;
                  execution_plan_id?: string }) =>
    request<ValuationResponse>("/api/v1/valuations", {
      method: "POST", body: JSON.stringify(body),
    }),

  reconcile: (portfolioId: string) =>
    request<ReconcileResponse>(
      "/api/v1/portfolios/" + encodeURIComponent(portfolioId) + "/reconcile",
    ),

  ledger: (portfolioId: string) =>
    request<LedgerResponse>(
      "/api/v1/portfolios/" + encodeURIComponent(portfolioId) + "/ledger",
    ),

  watchlist: () => request<{ subjectId: string; count: number; items: WatchItem[]; note: string }>(
    "/api/v1/watchlist",
  ),

  addWatch: (instrumentId: string, note?: string) =>
    request<{ instrument_id: string; watching: boolean }>("/api/v1/watchlist/items", {
      method: "POST", body: JSON.stringify({ instrument_id: instrumentId, note: note ?? null }),
    }),

  removeWatch: (instrumentId: string) =>
    request<{ instrument_id: string; watching: boolean }>(
      "/api/v1/watchlist/items/" + encodeURIComponent(instrumentId),
      { method: "DELETE" },
    ),

  decisions: (portfolioId?: string) =>
    request<{ count: number; decisions: DecisionRow[]; note: string }>(
      "/api/v1/decisions" + (portfolioId ? "?portfolio_id=" + encodeURIComponent(portfolioId) : ""),
    ),

  recordNoChange: (body: {
    portfolio_id: string; snapshot_id: string; reason_note: string;
    model_proposed: Record<string, unknown> | null; rule_check: Record<string, unknown> | null;
    external_information_used: boolean;
  }) => request<{ decision_id: string; decision_type: string }>("/api/v1/decisions", {
    method: "POST", body: JSON.stringify({ ...body, decision_type: "NO_CHANGE", plan_id: null,
      human_final: { action: "NO_CHANGE", orders: [] }, reason_category: "NO_CHANGE" }),
  }),

  /** 研究卡。字段名与 workspace.json 的 ResearchCard 一致，可直接复用类型。 */
  research: (instrumentId: string, tradingDay: string) =>
    request<ResearchCard>(
      "/api/v1/instruments/" + encodeURIComponent(instrumentId) + "/research" +
      "?trading_day=" + encodeURIComponent(tradingDay),
    ),

  /** 未审阅 PDF 候选的独立诊断预览，不进入研究卡、正式因子或实验。 */
  s2DiagnosticPreview: () =>
    request<S2DiagnosticPreviewResponse>(
      "/api/v1/research/s2/diagnostic-preview",
    ),

  /** 只读研究名单；数据日期与 S1 工作台快照独立。 */
  researchCandidates: (options: { limit?: number; offset?: number; q?: string;
    board?: string; industry?: string; view?: "candidates" | "all" | "financial" } = {}) => {
    const params = new URLSearchParams({
      limit: String(options.limit ?? 20), offset: String(options.offset ?? 0),
      q: options.q ?? "", board: options.board ?? "", industry: options.industry ?? "",
      view: options.view ?? "candidates",
    });
    return request<ResearchCandidatesResponse>("/api/v1/research/candidates?" + params);
  },

  runFactors: (limit = 0) =>
    request<ResearchRunResponse>("/api/v1/research/runs", {
      method: "POST", body: JSON.stringify({ limit }),
    }),

  factorValues: (runId: string) =>
    request<{ researchRunId: string; count: number; factors: FactorRowValue[] }>(
      "/api/v1/research/runs/" + encodeURIComponent(runId) + "/factors",
    ),

  experiments: () =>
    request<{ count: number; experiments: ExperimentRow[]; note: string }>("/api/v1/experiments"),

  strategyVersions: () =>
    request<StrategyVersionsResponse>("/api/v1/strategy-versions"),

  /** 每日任务的调度配置与运行状态（§14.2）。 */
  schedule: () => request<ScheduleStatus>("/api/v1/schedule"),

  saveSchedule: (body: {
    enabled: boolean; runAtLocal: string; weekdaysOnly: boolean;
    interpreter: string; dataDir: string; windowStart: string;
  }) => request<ScheduleStatus>("/api/v1/schedule", {
    method: "POST", body: JSON.stringify(body),
  }),

  /** 登记一次立刻运行。**只登记**：执行由独立 worker 负责。 */
  runPipelineNow: (reason?: string) =>
    request<ScheduleStatus & { requestId: string; note: string }>(
      "/api/v1/schedule/run",
      { method: "POST", body: JSON.stringify({ reason: reason ?? null }) },
    ),
};
