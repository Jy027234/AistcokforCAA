/** 展示格式化。所有换算在视图模型层完成，这里只做日期与文案处理。 */

const CST_OFFSET_MIN = 8 * 60;

function toCst(iso: string): Date | null {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return new Date(d.getTime() + CST_OFFSET_MIN * 60_000);
}

/** 语义色调。callout 额外支持 info；badge 不使用 info。 */
export type Tone = "ok" | "warn" | "danger" | "neutral" | "accent";
export type CalloutTone = Tone | "info";

export function formatAsOf(iso: string): string {
  const d = toCst(iso);
  if (!d) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return (
    String(d.getUTCFullYear()) + "-" + p(d.getUTCMonth() + 1) + "-" + p(d.getUTCDate()) +
    " " + p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()) + " CST"
  );
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = toCst(iso);
  if (!d) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return String(d.getUTCFullYear()) + "-" + p(d.getUTCMonth() + 1) + "-" + p(d.getUTCDate());
}

export function formatRelative(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const min = Math.round((Date.now() - d.getTime()) / 60_000);
  if (Math.abs(min) < 1) return "刚刚";
  if (Math.abs(min) < 60) return String(min) + " 分钟前";
  const h = Math.round(min / 60);
  if (Math.abs(h) < 24) return String(h) + " 小时前";
  return String(Math.round(h / 24)) + " 天前";
}

export function categoryLabel(category: string): string {
  const map: Record<string, string> = {
    ANNOUNCEMENT: "公告",
    EARNINGS: "业绩",
    DIVIDEND: "分红",
    CAPITAL_ACTION: "资本运作",
    REGULATORY: "监管",
    MANAGEMENT_CHANGE: "管理层变动",
    INDUSTRY_POLICY: "行业政策",
    MACRO: "宏观",
    NEWS: "新闻",
    OTHER: "其他",
  };
  return map[category] ?? category;
}

export function verificationLabel(v: string): { text: string; tone: Tone } {
  switch (v) {
    case "VERIFIED": return { text: "已核验", tone: "ok" };
    case "UNVERIFIED": return { text: "未核验", tone: "neutral" };
    case "DISPUTED": return { text: "存在争议", tone: "warn" };
    case "WITHDRAWN": return { text: "已撤回", tone: "danger" };
    default: return { text: "状态未知", tone: "neutral" };
  }
}

export function directionLabel(d: string): { text: string; tone: Tone } {
  switch (d) {
    case "BULLISH": return { text: "可能利好", tone: "warn" };
    case "BEARISH": return { text: "可能不利", tone: "warn" };
    case "UNCERTAIN": return { text: "方向不确定", tone: "neutral" };
    default: return { text: "方向未知", tone: "neutral" };
  }
}

export function readinessTone(r: string): Tone {
  if (r === "READY") return "ok";
  if (r === "PARTIAL") return "warn";
  if (r === "BLOCKING") return "danger";
  return "neutral";
}

export function decisionTypeLabel(t: string): string {
  const map: Record<string, string> = {
    ACCEPT_MODEL: "采纳模型方案",
    MODIFY_MODEL: "修改模型方案",
    NO_CHANGE: "不调整",
    TIMEOUT: "超时未处理",
    REJECT: "拒绝",
    CANCEL: "取消",
  };
  return map[t] ?? t;
}
