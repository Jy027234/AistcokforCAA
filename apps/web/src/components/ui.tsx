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

export function Section({
  title, hint, actions, children,
}: { title: string; hint?: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="section">
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
