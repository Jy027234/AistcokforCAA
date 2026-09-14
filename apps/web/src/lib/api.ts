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
      message: envelope?.message ?? "请求被拒绝（HTTP " + res.status + "）",
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
  reference_price_day: string | null;
  plan_version: string;
  account_version: string;
  frozen: boolean;
  frozenLabel: string;
  orders: {
    instrument_id: string; side: string; quantity: number;
    price_cents: number; rationale?: string;
    reference_price_day?: string;
  }[];
  estimatedFeesCents: number;
  estimated_fees_cents: number;
  rule_checks: { order: string; check: string; passed: boolean; detail?: string }[];
  excluded: { instrument_id?: string; instrumentId?: string; reason: string; detail?: string }[];
  cash_weight_pct: string;
  notes: string[];
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

export const api = {
  health: () => request<{ status: string }>("/api/v1/health"),

  status: () => request<Record<string, unknown>>("/api/v1/status"),

  preview: (body: { portfolio_id: string; snapshot_id: string; trading_day: string }) =>
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

  value: (body: { portfolio_id: string; snapshot_id: string; trading_day: string }) =>
    request<ValuationResponse>("/api/v1/valuations", {
      method: "POST", body: JSON.stringify(body),
    }),

  reconcile: (portfolioId: string) =>
    request<ReconcileResponse>(
      "/api/v1/portfolios/" + encodeURIComponent(portfolioId) + "/reconcile",
    ),
};
