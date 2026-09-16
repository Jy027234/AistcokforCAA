import type { DataStatus } from "../lib/types";
import { formatAsOf, readinessTone } from "../lib/format";
import { Badge } from "./ui";

export type Tab = "today" | "research" | "portfolio" | "workspace" | "experiments";

const TABS: { id: Tab; label: string }[] = [
  { id: "today", label: "今日" },
  { id: "research", label: "研究" },
  { id: "portfolio", label: "组合" },
  { id: "workspace", label: "工作区" },
  { id: "experiments", label: "实验" },
];

/** 顶栏 + 四项顶层导航（主文档 §5.2、§5.4）。
 *
 * §5.4 要求顶栏包含：研究日期、数据就绪状态、账户类型、刷新状态。
 * 数据源与规则版本等放在"运行状态"抽屉里，不铺成管理菜单。
 */
export function TopBar({
  status, tab, onTab, onOpenStatus, lastLoadedAt, loading,
}: {
  status: DataStatus;
  tab: Tab;
  onTab: (t: Tab) => void;
  onOpenStatus: () => void;
  lastLoadedAt: string | null;
  loading: boolean;
}) {
  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">AQ</span>
        <span className="brand-name">A-Quant Lab</span>
        <span className="brand-sub">研究与模拟决策工作台</span>
      </div>

      <nav className="tabs" role="tablist" aria-label="顶层导航">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            className="tab"
            aria-selected={tab === t.id}
            onClick={() => onTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>

      <div className="topbar-status">
        <Badge tone="neutral">研究日期 {formatAsOf(status.asOfTime)}</Badge>
        <Badge tone={readinessTone(status.readiness)}>{status.readinessLabel}</Badge>
        {/* 数据新鲜度。**落后了必须显眼**：界面一切正常、数字自洽、
            对账通过，而它们基于的是几天前的数据——没人会自己注意到。
            读不出新鲜度时不显示徽章，也不声称"数据是新的"。 */}
        {status.freshness?.stale && (
          <Badge tone="warn">数据落后 {status.freshness.stalenessDays} 天</Badge>
        )}
        <Badge tone="accent">{status.accountLabel}</Badge>
        <button className="btn btn-sm" onClick={onOpenStatus}>
          {loading ? "刷新中…" : lastLoadedAt ? "已更新 " + lastLoadedAt : "运行状态"}
        </button>
      </div>
    </header>
  );
}

/** 运行状态抽屉：数据源、规则版本、费用、模型额度（§5.2 末段）。 */
export function StatusDrawer({
  status, onClose,
}: { status: DataStatus; onClose: () => void }) {
  return (
    <>
      <div className="drawer-scrim" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="运行状态">
        <header className="drawer-head">
          <h3>运行状态</h3>
          <div className="spacer" />
          <button className="btn btn-sm btn-ghost" onClick={onClose}>关闭</button>
        </header>
        <div className="drawer-body">
          <h4 style={{ marginBottom: 6 }}>当前快照</h4>
          <dl className="kv">
            <dt>快照 ID</dt><dd className="mono">{status.snapshotId}</dd>
            <dt>类型</dt><dd>{status.kind}</dd>
            <dt>截止时点</dt><dd>{formatAsOf(status.asOfTime)}</dd>
            <dt>发布时间</dt><dd>{status.publishedAt ? formatAsOf(status.publishedAt) : "—"}</dd>
            <dt>数据模式</dt><dd><Badge tone={status.dataMode === "SYNTHETIC" ? "warn" : "neutral"}>{status.dataMode}</Badge></dd>
            <dt>数据新鲜度</dt>
            <dd>
              {status.freshness?.detail ?? "未记录运行留痕，无法判断新鲜度"}
              {status.freshness?.lastPublishedDay && (
                <span className="note">
                  （上次成功发布：{status.freshness.lastPublishedDay}）
                </span>
              )}
            </dd>
            <dt>质量状态</dt><dd>{status.qualityStatus}</dd>
          </dl>

          {status.watermark && (
            <div style={{ marginTop: 12 }}>
              <div className="callout callout-warn">
                <span className="icon" aria-hidden="true">!</span>
                <div><strong>数据水印</strong><div className="note" style={{ color: "inherit" }}>{status.watermark}</div></div>
              </div>
            </div>
          )}

          <h4 style={{ margin: "18px 0 6px" }}>数据集覆盖</h4>
          <div className="table-wrap">
            <table className="data">
              <thead><tr><th>数据集</th><th className="num">记录数</th><th>时点上界</th></tr></thead>
              <tbody>
                {status.datasetSummary.map((d) => (
                  <tr key={d.name}>
                    <td className="mono">{d.name}</td>
                    <td className="num">{d.recordCount.toLocaleString()}</td>
                    <td className="mono">{formatAsOf(d.asOfUpperBound)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <h4 style={{ margin: "18px 0 6px" }}>阻断项</h4>
          {status.blockingIssues.length === 0 ? (
            <p className="note">无阻断项。</p>
          ) : (
            <ul className="list">
              {status.blockingIssues.map((b, i) => (
                <li key={i}>
                  <Badge tone="danger">{b.code}</Badge>
                  <div style={{ marginTop: 4 }}>{b.message}</div>
                  <div className="note">修复：{b.repair}</div>
                </li>
              ))}
            </ul>
          )}

          <h4 style={{ margin: "18px 0 6px" }}>说明</h4>
          <p className="note">
            本工作台为研究与模拟用途。不连接券商、不自动交易，所有账户均为模拟账户。
            数据源权利状态未确认前，材料不进入模型外发链路。
          </p>
        </div>
      </aside>
    </>
  );
}
