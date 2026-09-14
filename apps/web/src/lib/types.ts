/** 工作台数据类型。
 *
 * 刻意不存在的字段：
 *   - 任何 probability / expectedReturn / totalScore
 *     （主文档 §5.3 禁止把排名改写为概率，禁止"综合投资价值 xx 分"）
 * 契约测试会断言这些字段不出现。
 */

export type Readiness = "READY" | "PARTIAL" | "BLOCKING";

export interface BlockingIssue {
  code: string;
  message: string;
  objectId: string | null;
  retryable: boolean;
  repair: string;
}

export interface DatasetSummary {
  name: string;
  recordCount: number;
  coverage: number | null;
  asOfUpperBound: string;
}

export interface DataStatus {
  snapshotId: string;
  kind: string;
  asOfTime: string;
  publishedAt: string | null;
  dataMode: "SYNTHETIC" | "PRODUCTION" | string;
  watermark: string | null;
  qualityStatus: string;
  readiness: Readiness;
  readinessLabel: string;
  blockingIssues: BlockingIssue[];
  datasetSummary: DatasetSummary[];
  timeLabel: string;
  accountLabel: string;
}

export interface FactorRow {
  factorId: string;
  name: string;
  value: number | null;
  unit: string | null;
  rankPct: number | null;
  coverage: number | null;
  contribution: number | null;
}

export interface EvidenceItem {
  statement?: string;
  noneFound?: boolean;
  note?: string;
  citationId?: string;
  documentId?: string;
  quote?: string;
}

export interface Tradability {
  simulatable: boolean;
  reason: string | null;
  reasonLabel: string;
  detail: string;
  rule: string;
  effectiveFrom: string | null;
  repair: string | null;
}

export interface ActionItem {
  id: string;
  label: string;
  sideEffect: string;
  note: string | null;
}

export interface ResearchCard {
  instrumentId: string;
  displayName: string;
  exchange: string;
  board: string;
  snapshotId: string;
  asOfTime: string;
  generatedAt: string;
  dataCompleteness: string;
  timeLabel: string;
  rankSemantics: string;
  rankBreakdown: FactorRow[];
  comparisonScope: string;
  evidence: EvidenceItem[];
  counterEvidence: EvidenceItem[];
  uncertainties: string[];
  actions: ActionItem[];
  tradability: Tradability;
  limitations: string[];
}

export interface CandidateRow {
  instrumentId: string;
  displayName: string;
  signalRank: number;
  industryCode: string;
  basis: string;
  counterEvidence: string;
  dataQuality: string;
  simulatable: boolean;
  simulatableLabel: string;
  lastClose: string;
}

export interface DraftOrder {
  instrumentId: string;
  side: "BUY" | "SELL";
  quantity: number;
  price: string;
  gross: string;
  estimatedFee: string;
  rationale: string | null;
  targetWeightPct: string | null;
}

export interface RuleCheck { name: string; passed: boolean }
export interface IndustryRow {
  industryCode: string; value: string; sharePct: string; overCap: boolean;
}

export interface Draft {
  planId: string;
  portfolioId: string;
  tradingDay: string;
  frozen: boolean;
  frozenLabel: string;
  orders: DraftOrder[];
  estimatedFees: string;
  cashBefore: string;
  cashAfter: string;
  cashAfterCents: number;
  buyTotal: string;
  sellTotal: string;
  industryCapPct: string;
  industry: IndustryRow[];
  excluded: { instrument_id?: string; instrumentId?: string; reason: string; detail?: string }[];
  ruleChecks: RuleCheck[];
  confirmAction: {
    id: string; label: string; requirement: string; revalidate: string[];
  };
}

export interface TodayChange {
  eventId: string;
  category: string;
  summary: string;
  availableAt: string | null;
  verification: string;
  direction: string;
  subjects: string[];
}

export interface PortfolioRow {
  portfolioId: string; kind: string; label: string;
  netValue: string; cash: string; positions: number; note: string;
}

export interface ExperimentRow {
  experimentId: string; hypothesis: string; strategyVersion: string;
  status: string; sampleWindow: string; dataLevel: string; limitations: string[];
}

export interface DecisionRow {
  decisionId: string; date: string; type: string; reason: string;
  modelProposed: string; humanFinal: string; externalInfoUsed: boolean;
}

export interface WorkspaceData {
  generatedAt: string;
  generator: string;
  note: string;
  status: DataStatus;
  candidates: CandidateRow[];
  researchCards: ResearchCard[];
  draft: Draft;
  todayChanges: TodayChange[];
  portfolios: PortfolioRow[];
  experiments: ExperimentRow[];
  decisionLog: DecisionRow[];
}

export type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; data: WorkspaceData }
  | { kind: "error"; message: string };
