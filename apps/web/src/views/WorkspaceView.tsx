import { useCallback, useEffect, useState } from "react";
import {
  api, AquantApiError, type DecisionRow, type ExperimentRow,
  type FactorRowValue, type ResearchRunResponse, type WatchItem,
} from "../lib/api";
import type { ResearchCard } from "../lib/types";
import { Badge, Callout, Card, Empty, Section } from "../components/ui";

/** 工作区：自选、因子排名、研究卡、决策日志、实验登记。
 *
 * 这一页存在的理由：后端此前已经能算因子、记决策、登记实验、管自选，
 * 但界面上完全看不到——使用者能感知到的只有四页只读演示。
 * **能力没有出口就等于不存在。**
 *
 * 三条界面原则：
 *   1. **缺失必须说清原因**，而不是显示 0 或留空；
 *   2. **不把排名渲染成概率**（§5.3）；
 *   3. **失败与负结果一样显示**：实验列表里 FAILED 不是错误状态，
 *      而是一条有价值的记录。
 */
export function WorkspaceView({
  apiUp, portfolioId, tradingDay,
}: { apiUp: boolean | null; portfolioId: string; tradingDay: string }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [watch, setWatch] = useState<WatchItem[] | null>(null);
  const [watchInput, setWatchInput] = useState("");
  const [run, setRun] = useState<ResearchRunResponse | null>(null);
  const [factorRows, setFactorRows] = useState<FactorRowValue[] | null>(null);
  const [card, setCard] = useState<ResearchCard | null>(null);
  const [cardError, setCardError] = useState<string | null>(null);
  const [decisions, setDecisions] = useState<DecisionRow[] | null>(null);
  const [experiments, setExperiments] = useState<ExperimentRow[] | null>(null);

  const explain = (err: unknown): string => {
    if (err instanceof AquantApiError) {
      return err.envelope.message + "（" + err.envelope.code + "）· 修复：" +
             err.envelope.repairAction;
    }
    return err instanceof Error ? err.message : String(err);
  };

  async function step<T>(name: string, fn: () => Promise<T>, onOk: (v: T) => void) {
    setBusy(name);
    setError(null);
    try {
      onOk(await fn());
    } catch (err) {
      setError(name + "失败：" + explain(err));
    } finally {
      setBusy(null);
    }
  }

  const loadWatch = useCallback(() => step("载入自选",
    () => api.watchlist(), (r) => setWatch(r.items)), []);
  const loadDecisions = useCallback(() => step("载入决策日志",
    () => api.decisions(portfolioId), (r) => setDecisions(r.decisions)), [portfolioId]);
  const loadExperiments = useCallback(() => step("载入实验",
    () => api.experiments(), (r) => setExperiments(r.experiments)), []);

  useEffect(() => {
    if (apiUp !== true) return;
    void loadWatch();
    void loadDecisions();
    void loadExperiments();
  }, [apiUp, loadWatch, loadDecisions, loadExperiments]);

  /** 看某只证券的研究卡。此时不再只看排名，而是看数值、证据、反证与限制。 */
  const onOpenCard = (instrumentId: string) => {
    setCardError(null);
    setCard(null);
    void step("载入研究卡",
      () => api.research(instrumentId, tradingDay),
      setCard).catch(() => undefined);
  };

  if (apiUp === false) {
    return (
      <Section title="工作区" dataSource="static">
        <Card>
          <Callout tone="warn" title="写操作不可用">
            工作台 API 未运行，因此自选、因子计算与实验登记都不可用。
            界面不会在离线时伪造结果。
          </Callout>
        </Card>
      </Section>
    );
  }

  const ranked = (factorRows ?? []).filter((r) => r.raw_value !== null);
  const excluded = (factorRows ?? []).filter((r) => r.raw_value === null);

  return (
    <>
      <Section
        title="工作区"
        hint="自选、因子排名、研究卡、决策日志、实验登记"
        dataSource="api"
        actions={<Badge tone={apiUp === true ? "ok" : "neutral"}>
          {apiUp === true ? "API 在线" : "检测中"}</Badge>}
      >
        {error && <Callout tone="danger" title="有一步没有完成">{error}</Callout>}
      </Section>

      {/* ------------------------------------------------ 自选 */}
      <Section title="自选" hint="自选不产生订单，也不影响模拟持仓" dataSource="api"
        noNumericValue>
        <Card>
          <div className="row-actions">
            <input
              className="input"
              placeholder="证券 ID，例如 SYN.A.600519"
              value={watchInput}
              onChange={(e) => setWatchInput(e.target.value)}
              style={{ flex: "1 1 260px" }}
            />
            <button
              className="btn btn-primary"
              disabled={busy !== null || !watchInput.trim()}
              onClick={() => step("加入自选",
                () => api.addWatch(watchInput.trim()),
                () => { setWatchInput(""); void loadWatch(); })}
            >加入自选</button>
          </div>

          {watch === null ? (
            <p className="note" style={{ marginTop: 10 }}>正在载入自选…</p>
          ) : watch.length === 0 ? (
            <div style={{ marginTop: 12 }}>
              <Empty title="自选为空">
                加入自选只是标记关注，不产生订单，也不改变账本。
              </Empty>
            </div>
          ) : (
            <div className="table-wrap" style={{ marginTop: 12 }}>
              <table className="data">
                <thead>
                  <tr><th>证券</th><th>加入时间</th><th>备注</th><th /></tr>
                </thead>
                <tbody>
                  {watch.map((w) => (
                    <tr key={w.instrument_id}>
                      <td className="mono">{w.instrument_id}
                        {w.short_name ? <span className="note">　{w.short_name}</span> : null}
                      </td>
                      <td className="mono">{w.added_at.slice(0, 10)}</td>
                      <td className="note">{w.note ?? "—"}</td>
                      <td style={{ display: "flex", gap: 6 }}>
                        <button className="btn btn-sm" disabled={busy !== null}
                          onClick={() => onOpenCard(w.instrument_id)}>研究卡</button>
                        <button className="btn btn-sm" disabled={busy !== null}
                          onClick={() => step("移出自选",
                            () => api.removeWatch(w.instrument_id), loadWatch)}>
                          移出
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </Section>

      {/* ---------------------------------------------- 研究卡 */}
      {(card || cardError || busy === "载入研究卡") && (
        <Section title="研究卡" hint="数值、证据、反证与限制（§5.1）" dataSource="api">
          <Card>
            {busy === "载入研究卡" && <p className="note">正在载入研究卡…</p>}
            {cardError && <Callout tone="danger" title="无法载入研究卡">{cardError}</Callout>}
            {card && (
              <>
                <div style={{ display: "flex", alignItems: "center", gap: 8,
                              flexWrap: "wrap", marginBottom: 10 }}>
                  <h3>{card.displayName}</h3>
                  <span className="mono">{card.instrumentId}</span>
                  <Badge tone="neutral">{card.exchange}/{card.board}</Badge>
                  <Badge tone={card.tradability.simulatable ? "ok" : "warn"}>
                    {card.tradability.reasonLabel}</Badge>
                </div>

                {/* 留档时刻：卡片按 (标的, 快照, 交易日) 冻结，
                    重复打开不会刷新。写清楚这一点，否则用户会以为
                    这是"刚刚生成的"，从而把一份旧证据当成当前判断。 */}
                <p className="note">
                  留档时刻 {card.generatedAt.slice(0, 19).replace("T", " ")}
                  {card.cardId ? "　（" + card.cardId + "）" : ""}
                  　· 同一标的、同一快照、同一交易日重复打开得到同一张卡片
                </p>

                <Callout tone={card.tradability.simulatable ? "ok" : "warn"}
                         title={card.tradability.simulatable ? "可进入模拟池" : "不可进入模拟池"}>
                  {card.tradability.detail}
                  {card.tradability.effectiveFrom && (
                    <div className="note">规则生效日：{card.tradability.effectiveFrom}</div>
                  )}
                  {card.tradability.repair && (
                    <div className="note">修复：{card.tradability.repair}</div>
                  )}
                </Callout>

                <h4 style={{ marginTop: 14 }}>排名构成</h4>
                <p className="note">{card.rankSemantics}</p>
                {card.rankBreakdown.length === 0 ? (
                  <p className="note">本快照未计算该标的的因子值。</p>
                ) : (
                  <div className="table-wrap">
                    <table className="data">
                      <thead><tr><th>因子</th><th className="num">数值</th>
                        <th className="num">排名</th></tr></thead>
                      <tbody>
                        {card.rankBreakdown.map((f) => (
                          <tr key={f.factorId}>
                            <td>{f.name} <span className="mono">{f.factorId}</span></td>
                            <td className="num mono">{f.value}</td>
                            {/* 未进排名时说明原因，而不是只显示一个"—"（§10.2） */}
                            <td className="num mono">{f.exclusionLabel ? "未进排名" : f.rankLabel}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}

                {card.uncertainties.length > 0 && (
                  <>
                    <h4 style={{ marginTop: 14 }}>不确定性</h4>
                    <ul className="list">
                      {card.uncertainties.map((u, i) => (
                        <li key={i} className="note">{u}</li>
                      ))}
                    </ul>
                  </>
                )}

                <h4 style={{ marginTop: 14 }}>反证</h4>
                {card.counterEvidence.length === 0 ? (
                  <p className="note">未记录反证。</p>
                ) : (
                  <ul className="list">
                    {card.counterEvidence.map((e, i) => (
                      <li key={i} className="note">
                        {e.noneFound ? "未找到反证" : (e.statement ?? "")}
                        {e.note ? "　" + e.note : ""}
                      </li>
                    ))}
                  </ul>
                )}

                {card.limitations.length > 0 && (
                  <>
                    <h4 style={{ marginTop: 14 }}>限制</h4>
                    <ul className="list">
                      {card.limitations.map((l, i) => (
                        <li key={i} className="note">{l}</li>
                      ))}
                    </ul>
                  </>
                )}
              </>
            )}
          </Card>
        </Section>
      )}

      {/* -------------------------------------------- 因子排名 */}
      <Section title="因子排名" hint="横截面排名，不是概率（§5.3）" dataSource="api"
        noNumericValue>
        <Card>
          <div className="row-actions">
            <button className="btn btn-primary" disabled={busy !== null}
              onClick={() => step("计算因子", async () => {
                const r = await api.runFactors();
                const values = await api.factorValues(r.researchRunId);
                setFactorRows(values.factors);
                return r;
              }, setRun)}>
              {busy === "计算因子" ? "计算中…" : "在本快照上计算 F10"}</button>
            <span className="note">
              财报可用性按保守规则推导：公布日之后第一个交易日的盘前。
            </span>
          </div>

          {run && (
            <div style={{ marginTop: 12 }}>
              <dl className="kv">
                <dt>研究运行</dt><dd className="mono">{run.researchRunId}</dd>
                <dt>快照 / 时点</dt>
                <dd className="mono">{run.snapshotId} · {run.asOfTime.slice(0, 19)}</dd>
                <dt>有值 / 排除</dt>
                <dd>{run.valued} 只 / {run.excluded} 只</dd>
              </dl>
              {Object.keys(run.exclusionBreakdown).length > 0 && (
                <Callout tone="info" title="被排除的标的与原因">
                  <ul className="list">
                    {Object.entries(run.exclusionBreakdown).map(([reason, n]) => (
                      <li key={reason}><span className="mono">{n}</span> 只 · {reason}</li>
                    ))}
                  </ul>
                  <p className="note">
                    缺失值不参与排名——给它一个名次等于把缺失当成最小值。
                  </p>
                </Callout>
              )}
            </div>
          )}

          {factorRows !== null && (
            <>
              <h4 style={{ marginTop: 16 }}>排名（前 20）</h4>
              {ranked.length === 0 ? (
                <Empty title="没有可排名的标的">
                  本快照没有财务数据，因此 F10 全部被排除。
                  这是数据缺失，不是计算失败。
                </Empty>
              ) : (
                <div className="table-wrap">
                  <table className="data">
                    <thead>
                      <tr><th>证券</th><th className="num">盈利收益率</th>
                        <th className="num">横截面排名</th></tr>
                    </thead>
                    <tbody>
                      {ranked.slice(0, 20).map((r) => (
                        <tr key={r.instrument_id}>
                          <td className="mono">{r.instrument_id}</td>
                          <td className="num mono">
                            {r.raw_value === null ? "—" : (r.raw_value * 100).toFixed(2) + "%"}
                          </td>
                          <td className="num mono">
                            {r.cross_sectional_rank === null ? "—"
                              : (r.cross_sectional_rank * 100).toFixed(1) + "%"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {excluded.length > 0 && (
                <p className="note" style={{ marginTop: 8 }}>
                  另有 {excluded.length} 只因缺失被排除，原因见上方分类。
                </p>
              )}
            </>
          )}
        </Card>
      </Section>

      {/* -------------------------------------------- 决策日志 */}
      <Section title="决策日志" hint="模型原方案与人工方案分开保存（§11.3）"
        dataSource="api" noNumericValue>
        <Card>
          {decisions === null ? (
            <p className="note">正在载入决策日志…</p>
          ) : decisions.length === 0 ? (
            <Empty title="还没有决策记录">
              冻结计划时的选择会记在这里：模型建议了什么、人最终做了什么、
              差在哪。失败的与未采纳的方案同样保留。
            </Empty>
          ) : (
            <ul className="list">
              {decisions.map((d) => (
                <li key={d.decision_id}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8,
                                flexWrap: "wrap" }}>
                    <Badge tone={d.decision_type === "REJECT" ? "warn" : "accent"}>
                      {d.decision_type}</Badge>
                    <span className="mono">{d.decision_id}</span>
                    <span className="note">{d.submitted_at.slice(0, 19)}</span>
                    {d.external_information_used && (
                      <Badge tone="warn">用了外部信息</Badge>
                    )}
                  </div>
                  <div className="note" style={{ marginTop: 4 }}>
                    {d.diff?.identical
                      ? "人工方案与模型方案完全一致。"
                      : d.diff?.comparable
                        ? "改动字段：" + (d.diff.changed_keys ?? []).join("、")
                        : (d.diff?.reason ?? "无法比较。")}
                    {d.reason_note ? "　理由：" + d.reason_note : ""}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </Section>

      {/* -------------------------------------------- 实验登记 */}
      <Section title="实验登记" hint="先登记、后看结果（§13.2）" dataSource="api"
        noNumericValue>
        <Card>
          {experiments === null ? (
            <p className="note">正在载入实验…</p>
          ) : experiments.length === 0 ? (
            <Empty title="还没有登记的实验">
              实验必须在看结果之前登记输入条件；失败与负收益的实验同样保留。
            </Empty>
          ) : (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr><th>实验</th><th>状态</th><th>主要指标</th>
                    <th className="num">测试集访问</th><th>结论</th></tr>
                </thead>
                <tbody>
                  {experiments.map((e) => (
                    <tr key={e.experiment_id}>
                      <td>
                        <div className="mono">{e.experiment_id}</div>
                        <div className="note">{e.hypothesis}</div>
                      </td>
                      <td>
                        <Badge tone={e.status === "FAILED" ? "warn"
                          : e.status === "COMPLETED" ? "ok" : "neutral"}>
                          {e.status}</Badge>
                      </td>
                      <td className="mono">{e.primary_metric ?? "—"}</td>
                      <td className="num mono">
                        {e.test_set_access_count}
                        {e.test_set_access_count > 1 && (
                          <span className="note">　已非未触碰</span>
                        )}
                      </td>
                      <td className="note">{e.outcome_notes ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </Section>
    </>
  );
}