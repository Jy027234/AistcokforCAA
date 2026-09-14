import type { ResearchCard as ResearchCardModel } from "../lib/types";
import { formatAsOf } from "../lib/format";
import { Badge, Callout, RankBar } from "./ui";

/** 股票研究卡片（主文档 §5.3）。
 *
 * §5.3 要求的每一项都在这里，且**不允许**出现的东西一律不渲染：
 *   - 不显示任何"概率"或"预期收益"
 *   - 不显示综合评分
 *   - 排名一律标注为排名
 *   - 不可模拟时显示规则、适用日期与修复方法，而不是一句"失败"
 */
export function ResearchCard({
  card, onOpenEvidence, onAction,
}: {
  card: ResearchCardModel;
  onOpenEvidence: (id: string) => void;
  onAction: (id: string, cardId: string) => void;
}) {
  const t = card.tradability;

  return (
    <article className="card" aria-labelledby={"rc-" + card.instrumentId}>
      <header className="card-head">
        <h3 id={"rc-" + card.instrumentId}>{card.displayName}</h3>
        <span className="mono" style={{ color: "var(--text-3)" }}>{card.instrumentId}</span>
        <div className="spacer" style={{ display: "flex", gap: 6 }}>
          {t.simulatable
            ? <Badge tone="ok">可模拟</Badge>
            : <Badge tone="warn">{t.reasonLabel}</Badge>}
        </div>
      </header>

      <div className="card-pad" style={{ display: "grid", gap: 14 }}>
        {/* 证券身份 */}
        <dl className="kv">
          <dt>交易所 / 板块</dt>
          <dd>{card.exchange} · {card.board}</dd>
          <dt>快照截止</dt>
          <dd>{formatAsOf(card.asOfTime)}</dd>
          <dt>报告生成</dt>
          <dd>{formatAsOf(card.generatedAt)}</dd>
          <dt>数据完整度</dt>
          <dd>{card.dataCompleteness}</dd>
          <dt>数据级别</dt>
          <dd><Badge tone={card.timeLabel.includes("虚构") ? "warn" : "neutral"}>
            {card.timeLabel}</Badge></dd>
        </dl>

        {/* 数值：排名，不是概率 */}
        <div>
          <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 6 }}>
            <h4>因子依据</h4>
            <span className="note">{card.comparisonScope}</span>
          </div>
          <Callout tone="info">
            <span className="note" style={{ color: "inherit" }}>{card.rankSemantics}</span>
          </Callout>
          {card.rankBreakdown.length === 0 ? (
            <p className="note" style={{ marginTop: 8 }}>
              本次未计算因子值。原因见下方限制项，不以空值代替结论。
            </p>
          ) : (
            <div style={{ marginTop: 8, display: "grid", gap: 8 }}>
              {card.rankBreakdown.map((f) => (
                <div key={f.factorId}>
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 10 }}>
                    <span>{f.name} <span className="mono" style={{ color: "var(--text-4)" }}>{f.factorId}</span></span>
                    <span className="mono">
                      {f.value}
                      {f.unit ? " " + f.unit : ""}
                    </span>
                  </div>
                  <RankBar rankPct={f.rankPct} label="横截面排名" />
                </div>
              ))}
            </div>
          )}
        </div>

        {/* 证据与反证 */}
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
            <h4>依据与反证</h4>
            <button className="btn btn-sm btn-ghost" onClick={() => onOpenEvidence(card.instrumentId)}>
              查看证据链
            </button>
          </div>
          <ul className="list">
            {card.evidence.length === 0 && (
              <li className="note">
                尚未接入该证券的事件证据。没有依据时不生成依据文案。
              </li>
            )}
            {card.evidence.slice(0, 3).map((e, i) => (
              <li key={e.citationId ?? i} style={{ fontSize: 13 }}>
                <span style={{ color: "var(--text-3)", marginRight: 6 }}>依据 {i + 1}</span>
                {e.statement ?? e.quote}
              </li>
            ))}
          </ul>
          <div style={{ marginTop: 8 }}>
            {card.counterEvidence.map((c, i) => (
              <Callout key={i} tone={c.noneFound ? "warn" : "info"} title={c.noneFound ? "未找到反证" : "反证"}>
                <span className="note" style={{ color: "inherit" }}>
                  {c.statement ?? c.note}
                  {c.noneFound && "——这只说明本次检索范围内未发现，不等于不存在。"}
                </span>
              </Callout>
            ))}
          </div>
        </div>

        {/* 不确定性 */}
        <div>
          <h4 style={{ marginBottom: 4 }}>不确定性</h4>
          <ul className="list">
            {card.uncertainties.map((u, i) => (
              <li key={i} className="note">{u}</li>
            ))}
          </ul>
        </div>

        {/* 限制：说清规则、适用日期与修复方法 */}
        {!t.simulatable && (
          <Callout tone="warn" title="当日不可模拟">
            <p>{t.detail}</p>
            <dl className="kv" style={{ marginTop: 6 }}>
              <dt>规则</dt><dd className="mono">{t.rule}</dd>
              <dt>适用日期</dt><dd>{t.effectiveFrom ?? "未定义"}</dd>
              <dt>修复方法</dt><dd>{t.repair ?? "—"}</dd>
            </dl>
          </Callout>
        )}

        {/* 行动入口：刻意没有"一键真实买入" */}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {card.actions.map((a) => (
            <button
              key={a.id}
              className={"btn btn-sm" + (a.id === "draft.create" ? " btn-primary" : "")}
              onClick={() => onAction(a.id, card.instrumentId)}
              title={a.note ?? a.sideEffect}
            >
              {a.label}
            </button>
          ))}
          <span className="note" style={{ alignSelf: "center" }}>
            自选写入不产生订单；草稿需在界面中显式确认后才会冻结
          </span>
        </div>
      </div>
    </article>
  );
}

/** 证据抽屉：引用必须可定位，链回原材料。 */
export function EvidenceDrawer({
  card, onClose,
}: { card: ResearchCardModel; onClose: () => void }) {
  return (
    <>
      <div className="drawer-scrim" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="证据链">
        <header className="drawer-head">
          <h3>{card.displayName} · 证据链</h3>
          <div className="spacer" />
          <button className="btn btn-sm btn-ghost" onClick={onClose}>关闭</button>
        </header>
        <div className="drawer-body">
          <dl className="kv" style={{ marginBottom: 14 }}>
            <dt>证券</dt><dd className="mono">{card.instrumentId}</dd>
            <dt>快照</dt><dd className="mono">{card.snapshotId}</dd>
            <dt>快照截止</dt><dd>{formatAsOf(card.asOfTime)}</dd>
          </dl>

          <h4 style={{ marginBottom: 6 }}>依据</h4>
          {card.evidence.length === 0 ? (
            <Callout tone="warn">
              该证券尚无已归档证据。系统不会为了填满卡片而生成依据。
            </Callout>
          ) : (
            <ul className="list">{card.evidence.map((e, i) => (
              <li key={i}>{e.statement ?? e.quote}</li>
            ))}</ul>
          )}

          <h4 style={{ margin: "16px 0 6px" }}>反证</h4>
          <ul className="list">{card.counterEvidence.map((c, i) => (
            <li key={i} className="note">{c.statement ?? c.note}</li>
          ))}</ul>

          <h4 style={{ margin: "16px 0 6px" }}>排名明细</h4>
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>因子</th><th className="num">值</th>
                  <th className="num">排名百分位</th><th className="num">覆盖</th>
                </tr>
              </thead>
              <tbody>
                {card.rankBreakdown.map((f) => (
                  <tr key={f.factorId}>
                    <td>{f.name}<div className="note mono">{f.factorId}</div></td>
                    <td className="num mono">{f.value}</td>
                    <td className="num">{f.rankLabel}</td>
                    <td className="num">{f.coverageLabel}</td>
                  </tr>
                ))}
                {card.rankBreakdown.length === 0 && (
                  <tr><td colSpan={4} className="note">本次未计算因子值</td></tr>
                )}
              </tbody>
            </table>
          </div>
          <Callout tone="info">
            <span className="note" style={{ color: "inherit" }}>
              "排名百分位"是本快照内的横截面名次，不是上涨概率，也不表示预期收益。
            </span>
          </Callout>
        </div>
        <footer className="drawer-foot">
          <span className="note">所有引用均可回溯至已归档原文；不可定位的引用不会出现在这里。</span>
        </footer>
      </aside>
    </>
  );
}
