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
  /** 数据新鲜度。**null 表示未记录运行留痕**——那时不该声称"数据是新的"。 */
  freshness: {
    snapshotDay: string | null;
    dataLastDay: string | null;
    lastPublishedDay: string | null;
    lastAttempt: {
      tradingDay: string | null; outcome: string; reason: string | null;
      finishedAt: string | null;
      steps: { step: string; ok: boolean }[];
    } | null;
    stalenessDays: number | null;
    stale: boolean;
    detail: string;
  } | null;
}

/** 因子行。数值以**展示串**给出（视图模型已格式化），
 *  同时保留 valueRaw 供审计与后续计算；前端不做数值格式化。 */
export interface FactorRow {
  factorId: string;
  name: string;
  value: string;
  valueRaw: number | null;
  unit: string | null;
  rankPct: number | null;
  rankLabel: string;
  coverage: number | null;
  coverageLabel: string;
  /** 被质量门排除时的原因码与说明（§10.2：算不出必须给原因）。
   *  有值时 value 为 "—"，界面必须显示原因而不是只显示一个破折号。 */
  exclusionReason: string | null;
  exclusionLabel: string | null;
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
  /** 卡片**留档**时刻。同一 (标的, 快照, 交易日) 重复打开不会刷新它——
   *  它是"当时看到的证据"的时间戳，不是这次请求的时间。 */
  generatedAt: string;
  /** 卡片在数据库中的稳定标识（服务端留档后返回）。 */
  cardId?: string;
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

/**
 * S2 候选 PDF 的只读公式诊断预览。
 *
 * 这是独立于正式 PIT 研究卡、因子运行、实验和交易模拟的数据类型；
 * 后端契约明确保证这些值不能用于回测或排名。
 */
export interface S2DiagnosticSourceReport {
  periodEnd: string;
  versionLabel: string;
  announcementId: string;
  documentUrl: string;
  pdfSha256: string;
  firstSeenAt: string;
}

export interface S2DiagnosticPreviewInstrument {
  instrumentId: string;
  latestPeriodEnd: string;
  sourceReports: S2DiagnosticSourceReport[];
  factors: {
    F07: string | null;
    F08: string | null;
    F09: string | null;
    F10: null;
  };
  exclusionCode: string | null;
  exclusionReason: string | null;
  note: string | null;
}

export interface S2DiagnosticPreviewResponse {
  schemaVersion: "aquant.s2_candidate_preview.v1";
  status: "CANDIDATE_DIAGNOSTIC_ONLY";
  formalPitEligible: false;
  backtestable: false;
  rank: null;
  source: "cninfo";
  generatedAt: string;
  instruments: S2DiagnosticPreviewInstrument[];
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
  /** 主入口的数据来源。API 是默认产品路径，fixture 只能由显式演示模式启用。 */
  dataSource?: "api" | "fixture";
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
