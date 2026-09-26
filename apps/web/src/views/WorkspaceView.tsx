import { useCallback, useEffect, useState } from "react";
import {
  api, AquantApiError, type DecisionRow, type ExperimentRow,
  type FactorRowValue, type ResearchRunResponse, type StrategyVersionsResponse,
  type WatchItem,
} from "../lib/api";
import type { ResearchCard, S2DiagnosticPreviewResponse } from "../lib/types";
import { Badge, Callout, Card, Empty, Section } from "../components/ui";

function formatS2Percent(value: string | null): string {
  if (value === null) return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${(parsed * 100).toFixed(2)}%` : value;
}

function formatS2Multiple(value: string | null): string {
  if (value === null) return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${parsed.toFixed(2)} 倍` : value;
}

function formatBeijingTime(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", dateStyle: "medium", timeStyle: "short",
  }).format(parsed) + "（北京时间）";
}

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
  const [strategyStatus, setStrategyStatus] = useState<StrategyVersionsResponse | null>(null);
  const [s2Preview, setS2Preview] = useState<S2DiagnosticPreviewResponse | null>(null);
  const [s2PreviewLoading, setS2PreviewLoading] = useState(false);
  const [s2PreviewError, setS2PreviewError] = useState<string | null>(null);

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
  const loadStrategies = useCallback(() => step("载入策略能力",
    () => api.strategyVersions(), setStrategyStatus), []);

  const loadS2Preview = async () => {
    setS2Preview(null);
    setS2PreviewError(null);
    setS2PreviewLoading(true);
    try {
      setS2Preview(await api.s2DiagnosticPreview());
    } catch (err) {
      setS2PreviewError(err instanceof AquantApiError && err.status === 503
        ? "后端没有可用的本机 PDF 候选归档。预览未生成，也不会回退到正式研究卡或 PIT 数据。"
        : "S2 公式诊断预览加载失败：" + explain(err));
    } finally {
      setS2PreviewLoading(false);
    }
  };

  useEffect(() => {
    if (apiUp !== true) return;
    void loadWatch();
    void loadDecisions();
    void loadExperiments();
    void loadStrategies();
  }, [apiUp, loadWatch, loadDecisions, loadExperiments, loadStrategies]);

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

      {/* -------------------------------------------- 策略能力 */}
      <Section title="策略能力" hint="缺失的数据只关闭受影响的策略族" dataSource="api">
        <Card>
          {strategyStatus === null ? (
            <p className="note">正在载入策略能力…</p>
          ) : (
            <>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
                {strategyStatus.strategyVersions.map((version) => (
                  <Badge key={version.strategy_version} tone="ok">
                    {version.family} · {version.strategy_version}
                  </Badge>
                ))}
              </div>
              {strategyStatus.familyGates.map((gate) => (
                <Callout key={gate.family} tone="warn"
                  title={`${gate.family} 未启用 · ${gate.error.code}`}>
                  {gate.family === "S2" ? (
                    <>
                      F07–F09 财务输入尚未通过数据源权限、PIT、修订链和覆盖率验收。
                      这会阻断 S2 登记、S1/S2 对照及质量因子增益结论，
                      <strong>不会阻断 S1 价格研究和首期人工模拟试运行</strong>。
                    </>
                  ) : gate.error.message}
                  <div className="note" style={{ marginTop: 6 }}>
                    修复路径：{gate.error.repair_action}
                  </div>
                </Callout>
              ))}
            </>
          )}
        </Card>
      </Section>

      {/* ------------------------ S2 受限公式诊断：独立于研究卡和正式信号 */}
      <Section title="S2 公式诊断预览"
        hint="只查看本机 CNINFO PDF 候选的公式演算，不生成排名或策略信号"
        dataSource="api">
        <Card>
          <Callout tone="danger" title="候选数据未完成人工核对">
            这里的数值来自尚未人工审阅的 PDF 抽取候选，不是已验收的 PIT 财务事实，
            不能用于回测、交易或 S1/S2 对照。此预览与研究卡、因子运行、实验和模拟隔离；
            S2 策略登记闸门仍保持关闭。
          </Callout>

          <div className="row-actions" style={{ marginTop: 12 }}>
            <button className="btn btn-primary" disabled={s2PreviewLoading}
              onClick={() => void loadS2Preview()}>
              {s2PreviewLoading ? "读取候选档案…"
                : s2Preview ? "重新读取公式预览" : "读取公式预览"}
            </button>
            <span className="note">
              仅显示 F07–F09 诊断值；F10、横截面排名和回测结果均不提供。
            </span>
          </div>

          {s2PreviewLoading && <p className="note" aria-live="polite">正在读取本机 PDF 候选档案…</p>}
          {s2PreviewError && (
            <Callout tone="warn" title="预览暂不可用">{s2PreviewError}</Callout>
          )}

          {s2Preview && (
            <div style={{ marginTop: 14 }}>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
                <Badge tone="danger">仅候选诊断</Badge>
                <Badge tone="warn">非正式 PIT</Badge>
                <Badge tone="neutral">不可回测</Badge>
                <span className="note">
                  来源：{s2Preview.source} · 生成于 {formatBeijingTime(s2Preview.generatedAt)}
                </span>
              </div>

              {s2Preview.instruments.length === 0 ? (
                <Empty title="没有可预览的候选">本机候选档案没有可展示的证券。</Empty>
              ) : (
                <div className="table-wrap">
                  <table className="data">
                    <thead>
                      <tr>
                        <th>证券 / 报告期</th>
                        <th className="num">F07 ROE TTM</th>
                        <th className="num">F08 现金质量</th>
                        <th className="num">F09 营收 TTM 同比</th>
                        <th>资格与说明</th>
                      </tr>
                    </thead>
                    <tbody>
                      {s2Preview.instruments.map((item) => (
                        <tr key={item.instrumentId}>
                          <td>
                            <div className="mono">{item.instrumentId}</div>
                            <div className="note">截至 {item.latestPeriodEnd}</div>
                          </td>
                          <td className="num mono">{formatS2Percent(item.factors.F07)}</td>
                          <td className="num mono">{formatS2Multiple(item.factors.F08)}</td>
                          <td className="num mono">{formatS2Percent(item.factors.F09)}</td>
                          <td>
                            {item.exclusionCode ? (
                              <>
                                <Badge tone="warn">{item.exclusionCode}</Badge>
                                {item.exclusionReason && (
                                  <div className="note" style={{ marginTop: 4 }}>
                                    {item.exclusionReason}
                                  </div>
                                )}
                              </>
                            ) : <Badge tone="neutral">仅公式演算</Badge>}
                            {item.note && <div className="note" style={{ marginTop: 4 }}>{item.note}</div>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {s2Preview.instruments.map((item) => (
                <details key={item.instrumentId + "-sources"} style={{ marginTop: 10 }}>
                  <summary className="note">
                    {item.instrumentId} 的候选来源（{item.sourceReports.length} 份报告）
                  </summary>
                  {item.sourceReports.length === 0 ? (
                    <p className="note">接口未返回候选报告来源。</p>
                  ) : (
                    <ul className="list">
                      {item.sourceReports.map((report) => (
                        <li key={report.announcementId}>
                          <a href={report.documentUrl} target="_blank" rel="noreferrer">
                            {report.periodEnd} · {report.versionLabel} · 公告 {report.announcementId}
                          </a>
                          <div className="note">
                            首次捕获 {formatBeijingTime(report.firstSeenAt)} · SHA-256 {report.pdfSha256}
                          </div>
                        </li>
                      ))}
                    </ul>
                  )}
                </details>
              ))}
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
