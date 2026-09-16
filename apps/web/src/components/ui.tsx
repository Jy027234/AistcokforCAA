import type { ReactNode } from "react";
import type { CalloutTone, Tone } from "../lib/format";

/** 状态徽标。颜色 + 文字双通道——主文档 §5.3 要求风险状态必须同时有文字。 */
export function Badge({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span className={"badge badge-" + tone}>
      {tone !== "neutral" && <span className="dot" aria-hidden="true" />}
      {children}
    </span>
  );
}

export function Callout({
  tone = "info", title, children,
}: { tone?: CalloutTone; title?: string; children: ReactNode }) {
  const icon = tone === "danger" ? "!" : tone === "warn" ? "!" : tone === "ok" ? "✓" : "i";
  return (
    <div className={"callout callout-" + tone} role={tone === "danger" ? "alert" : undefined}>
      <span className="icon" aria-hidden="true">{icon}</span>
      <div>
        {title && <strong style={{ display: "block", marginBottom: 2 }}>{title}</strong>}
        {children}
      </div>
    </div>
  );
}

export function Card({
  title, actions, children, padded = true,
}: { title?: ReactNode; actions?: ReactNode; children: ReactNode; padded?: boolean }) {
  return (
    <section className="card">
      {title && (
        <header className="card-head">
          <h3>{title}</h3>
          {actions && <div className="spacer">{actions}</div>}
        </header>
      )}
      <div className={padded ? "card-pad" : undefined}>{children}</div>
    </section>
  );
}

export function Stat({ label, value, mono = false }: {
  label: string; value: ReactNode; mono?: boolean;
}) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className={"stat-value" + (mono ? " mono" : "")}>{value}</span>
    </div>
  );
}

/** 排名条。语义固定为"排名百分位"，绝不渲染成概率。 */
export function RankBar({ rankPct, label }: { rankPct: number | null; label: string }) {
  const pct = rankPct === null ? 0 : Math.max(0, Math.min(1, rankPct)) * 100;
  return (
    <div className="rankbar">
      <span className="rankbar-label" style={{ minWidth: 0, textAlign: "left", color: "var(--text-3)" }}>
        {label}
      </span>
      <span className="rankbar-track" aria-hidden="true">
        <span className="rankbar-fill" style={{ width: pct + "%" }} />
      </span>
      <span className="rankbar-label">
        {rankPct === null ? "—" : (rankPct * 100).toFixed(0) + "%"}
      </span>
    </div>
  );
}

/** 区块的数据来源声明。
 *
 * 这不是装饰：它是**可被机器读取的契约**。界面上的数字必须说得出
 * 自己从哪来，而"从哪来"靠人看是看不出来的——组合页曾经整块草稿
 * 都来自随前端分发的只读夹具，而徽章写着「API 在线」。
 *
 *   * "api"     —— 数字必须能在某次服务端响应里找到（由 check 核对）；
 *   * "fixture" —— 随前端分发的示例数据，必须同时带演示标注；
 *   * "static"  —— 常量文案/标签，不含业务数字。
 */
export type DataSource = "api" | "fixture" | "static";

export function Section({
  title, hint, actions, children, dataSource, noNumericValue = false,
}: {
  title: string; hint?: string; actions?: ReactNode; children: ReactNode;
  dataSource: DataSource;
  /** 声明"本区块没有自己的业务数字"（只有常量文案与规格引用）。
   *
   * 用途是给出处检查器一个**显式**的排除依据，而不是让它去猜哪个数字
   * 是业务数字。显式声明的价值在于：将来这个区块真的显示了一个金额，
   * 那句声明就成了假话——而检查器会因此再次报红。
   */
  noNumericValue?: boolean;
}) {
  return (
    <section className="section" data-source={dataSource}
      data-no-numeric-value={noNumericValue ? "1" : undefined}>
      <div className="section-head">
        <h2>{title}</h2>
        {hint && <span className="hint">{hint}</span>}
        {actions && <div className="spacer">{actions}</div>}
      </div>
      {children}
    </section>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <div className="title">{title}</div>
      {children}
    </div>
  );
}

export function Loading() {
  return (
    <div className="card card-pad" aria-busy="true" aria-live="polite">
      <div className="skeleton" style={{ width: "38%", marginBottom: 12 }} />
      <div className="skeleton" style={{ width: "72%", marginBottom: 8 }} />
      <div className="skeleton" style={{ width: "56%" }} />
      <p className="note" style={{ marginTop: 12 }}>正在载入工作台数据…</p>
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="card card-pad">
      <Callout tone="danger" title="无法载入数据">
        <p>{message}</p>
        <p className="note" style={{ marginTop: 6 }}>
          工作台不会用缓存或猜测的数据替代缺失结果。
        </p>
        {onRetry && (
          <button className="btn btn-sm" style={{ marginTop: 10 }} onClick={onRetry}>
            重试
          </button>
        )}
      </Callout>
    </div>
  );
}
