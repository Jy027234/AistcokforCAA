import { Fragment, useEffect, useReducer, useState } from "react";
import type { ReactNode } from "react";
import { api, AquantApiError } from "../lib/api";
import { formatAsOf } from "../lib/format";
import type { ResearchCandidate, ResearchCandidateFactorId, ResearchCandidatesResponse, ResearchCandidatesView, ResearchValue } from "../lib/types";
import { Badge, Callout, Card, Empty, Section, Stat } from "./ui";

const PAGE_SIZE = 20;
const FACTORS: { id: ResearchCandidateFactorId; label: string }[] = [
  { id: "F07", label: "ROE TTM" }, { id: "F08", label: "经营现金流 / 合并净利润 TTM" },
  { id: "F09", label: "营业收入 TTM 同比" }, { id: "F10", label: "归母净利润 TTM / 行情总市值" },
];
const FINANCIAL_METRICS = [
  { id: "revenue", label: "营业收入（原报告累计）" },
  { id: "net_profit_attributable", label: "归母净利润（原报告累计）" },
  { id: "net_profit_consolidated", label: "合并净利润（原报告累计）" },
  { id: "operating_cashflow", label: "经营现金流（原报告累计）" },
  { id: "parent_equity", label: "归母权益（报告期末）" },
];
const percentile = (value: number | null | undefined) => value != null && Number.isFinite(value) ? (value * 100).toFixed(2) + "%" : "未参与排名";
const valueText = (value: ResearchValue | undefined) => value == null || value === "" ? "未提供" : String(value);
const signalText = (value: number | null) => value == null ? "未参与排名" : value.toFixed(6);
const boardLabel = (value: string) => ({ MAIN: "普通主板", GEM: "创业板", STAR: "科创板", BSE: "北交所" }[value] ?? value);
const industryDate = (candidate: ResearchCandidate) => candidate.industryClassification
  ? candidate.industryClassification.classificationAsOf : candidate.industryAsOf;
const isFinancial = (candidate: ResearchCandidate) => candidate.status === "FINANCIAL" ||
  candidate.industryClassification?.template === "FINANCIAL" || !!candidate.financialObservation;
const financialStatus = (status: string) => ({ COMPLETE: "五项字段已取得", PARTIAL: "部分字段缺失", MISSING: "未取得完整原报告字段" }[status] ?? status);
function errorMessage(error: unknown): string {
  return error instanceof AquantApiError ? error.envelope.message + "（" + error.envelope.code + "）· 修复：" + error.envelope.repairAction
    : error instanceof Error ? error.message : String(error);
}
function Notes({ items, empty }: { items: string[]; empty: string }) {
  return items.length ? <ul className="list">{items.map((item, index) => <li className="note" key={index}>{item}</li>)}</ul> : <p className="note">{empty}</p>;
}
interface ComparisonState {
  snapshotKey: string | null;
  publication: ResearchCandidatesResponse | null;
  cards: ResearchCandidate[];
  notice: string | null;
}
type ComparisonAction =
  | { type: "publication"; result: ResearchCandidatesResponse }
  | { type: "add"; candidate: ResearchCandidate; snapshotKey: string }
  | { type: "remove"; instrumentId: string }
  | { type: "clear" };
const emptyComparison: ComparisonState = { snapshotKey: null, publication: null, cards: [], notice: null };
function comparisonSnapshotKey(result: ResearchCandidatesResponse): string {
  return result.snapshotId ?? result.schema + ":" + result.generatedAt;
}
function comparisonReducer(state: ComparisonState, action: ComparisonAction): ComparisonState {
  if (action.type === "publication") {
    const snapshotKey = comparisonSnapshotKey(action.result);
    const changed = state.snapshotKey !== snapshotKey;
    return { snapshotKey, publication: action.result, cards: changed ? [] : state.cards,
      notice: changed && state.cards.length ? "发布快照已更新，旧比较集合已清空。请从当前发布重新选择。" : changed ? null : state.notice };
  }
  if (action.type === "clear") return { ...state, cards: [], notice: "已清空比较集合。" };
  if (action.type === "remove") return { ...state, cards: state.cards.filter((card) => card.instrumentId !== action.instrumentId), notice: null };
  if (action.snapshotKey !== state.snapshotKey) return { ...state, notice: "该证券来源与当前发布快照不同，请重新读取后选择。" };
  if (state.cards.some((card) => card.instrumentId === action.candidate.instrumentId)) return state;
  if (state.cards.length >= 3) return { ...state, notice: "最多比较 3 只证券，请先移除一只。" };
  return { ...state, cards: [...state.cards, action.candidate], notice: null };
}

/** 研究数据保持只读；跨页动作仅传证券和研究来源，模拟页另行核验资格。 */
export function ResearchCandidates({ onPrepareSimulation, focusedInstrumentId }: {
  onPrepareSimulation?: (instrumentId: string, researchSnapshotId: string | null) => void;
  focusedInstrumentId?: string | null;
}) {
  const [result, setResult] = useState<ResearchCandidatesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const [query, setQuery] = useState("");
  const [industry, setIndustry] = useState("");
  const [board, setBoard] = useState("");
  const [view, setView] = useState<ResearchCandidatesView>("candidates");
  const [filters, setFilters] = useState({ q: "", industry: "", board: "", view: "candidates" as ResearchCandidatesView });
  const [offset, setOffset] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [comparison, dispatchComparison] = useReducer(comparisonReducer, emptyComparison);
  useEffect(() => {
    if (!focusedInstrumentId) return;
    setQuery(focusedInstrumentId); setIndustry(""); setBoard(""); setView("all");
    setFilters({ q: focusedInstrumentId, industry: "", board: "", view: "all" });
    setOffset(0); setSelectedId(focusedInstrumentId);
  }, [focusedInstrumentId]);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      setFilters((previous) => previous.q === query.trim() && previous.industry === industry.trim() && previous.board === board && previous.view === view
        ? previous : { q: query.trim(), industry: industry.trim(), board, view });
      setOffset(0); setSelectedId(null);
    }, 350);
    return () => window.clearTimeout(timer);
  }, [query, industry, board, view]);
  useEffect(() => {
    let active = true; setLoading(true); setError(null);
    api.researchCandidates({ limit: PAGE_SIZE, offset, ...filters }).then((response) => {
      if (!["aquant.research-candidates.v1", "aquant.research-candidates.v2"].includes(response?.schema) ||
        response.strategy !== "S2_RESEARCH" || response.pitMode !== "HISTORICAL_RECONSTRUCTED" ||
        response.researchOnly !== true || response.simulationAvailable !== false ||
        !Array.isArray(response.candidates) || !Array.isArray(response.limitations) || !Array.isArray(response.exclusions) || !response.source ||
        (response.schema === "aquant.research-candidates.v2" && (!response.scope || !Array.isArray(response.scope.byBoard) ||
          !["INTRADAY_RESEARCH", "CLOSING_RESEARCH"].includes(response.quoteMode ?? "") || typeof response.filteredCount !== "number" || typeof response.offset !== "number"))) {
        throw new Error("研究候选响应不符合只读研究契约，请检查服务端发布文件后重试。");
      }
      if (active) { dispatchComparison({ type: "publication", result: response }); setResult(response); }
    }).catch((cause: unknown) => { if (active) { setResult(null); setError(errorMessage(cause)); } })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [attempt, offset, filters]);
  const v2 = result?.schema === "aquant.research-candidates.v2";
  // v1 服务忽略分页参数；对完整发布数组做兼容检索，不能先截前20。
  const legacy = result?.candidates.filter((candidate) =>
    (!filters.q || [candidate.code, candidate.displayName, candidate.instrumentId].some((item) => item.toLocaleLowerCase().includes(filters.q.toLocaleLowerCase()))) &&
    (!filters.board || candidate.board === filters.board) && (!filters.industry || candidate.industry?.includes(filters.industry)) && filters.view !== "financial") ?? [];
  const candidates = v2 ? result?.candidates ?? [] : legacy.slice(offset, offset + PAGE_SIZE);
  const total = v2 ? result?.filteredCount ?? 0 : legacy.length;
  const pageOffset = v2 ? result?.offset ?? offset : offset;
  const nextOffset = v2 ? result?.nextOffset ?? null : offset + PAGE_SIZE < total ? offset + PAGE_SIZE : null;
  const selected = candidates.find((item) => item.instrumentId === selectedId) ?? candidates[0] ?? null;
  const busy = loading || filters.q !== query.trim() || filters.industry !== industry.trim() || filters.board !== board || filters.view !== view;
  const comparisonAvailable = !!result && comparison.snapshotKey === comparisonSnapshotKey(result);
  const toggleComparison = (candidate: ResearchCandidate) => {
    if (!result) return;
    dispatchComparison(comparison.cards.some((card) => card.instrumentId === candidate.instrumentId)
      ? { type: "remove", instrumentId: candidate.instrumentId }
      : { type: "add", candidate, snapshotKey: comparisonSnapshotKey(result) });
  };
  return <Section title="寻找值得研究的证券" hint="先看依据与反证，再决定是否检查模拟资格" dataSource="api">
    <div style={{ display: "grid", gap: 16 }}>
      <Card>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 12 }}>
          <label className="note" htmlFor="research-candidates-search">搜索发布范围中的代码或名称
            <input id="research-candidates-search" type="search" className="input" style={{ width: "100%", marginTop: 6 }} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="证券代码或名称" />
          </label>
          <label className="note" htmlFor="research-candidates-board">板块
            <select id="research-candidates-board" className="input" style={{ width: "100%", marginTop: 6 }} value={board} onChange={(event) => setBoard(event.target.value)}>
              <option value="">全部板块</option>{(result?.scope?.byBoard ?? []).map((item) => <option key={item.board} value={item.board}>{boardLabel(item.board)}</option>)}
            </select>
          </label>
          <label className="note" htmlFor="research-candidates-industry">行业筛选
            <input id="research-candidates-industry" type="search" className="input" style={{ width: "100%", marginTop: 6 }} value={industry} onChange={(event) => setIndustry(event.target.value)} placeholder="输入行业名称" />
          </label>
          <label className="note" htmlFor="research-candidates-view">研究视图
            <select id="research-candidates-view" className="input" style={{ width: "100%", marginTop: 6 }} value={view} onChange={(event) => setView(event.target.value as ResearchCandidatesView)}>
              <option value="candidates">普通工商排名</option><option value="all">全部观察证券</option><option value="financial">金融单列研究</option>
            </select>
          </label>
        </div>
        <p className="note" style={{ marginTop: 10 }}>搜索与筛选覆盖服务端整个已发布范围，每页 20 只。未采集成功的证券不以占位行情补齐。</p>
      </Card>
      {busy ? <Card><div aria-busy="true" aria-live="polite"><Empty title="正在读取研究候选名单">读取筛选结果与公告证据。</Empty></div></Card>
        : error || !result ? <Card><Callout tone="danger" title="无候选数据"><p>{error ?? "尚未发布研究候选数据。"}</p>
          <p className="note" style={{ marginTop: 6 }}>没有可用的 API 结果，不展示替代名单。</p>
          <button className="btn btn-sm" style={{ marginTop: 10 }} onClick={() => setAttempt((item) => item + 1)}>重试读取</button>
        </Callout></Card> : <>
          <Coverage result={result} />
          <div className="grid-2 research-grid" style={{ alignItems: "start" }}>
            <Card title={view === "all" ? "全部观察证券" : view === "financial" ? "金融单列研究" : "普通工商候选排名"}>
              <p className="note" style={{ marginBottom: 12 }}>{total ? `${pageOffset + 1}–${pageOffset + candidates.length} / ${total} 只` : "筛选结果 0 只"}{!v2 && " · 旧版发布，仅支持已有候选检索"}</p>
              {candidates.length === 0 ? <Empty title="没有匹配的证券">调整代码、行业、板块或研究视图重新检索。</Empty> :
                <ul className="list">{candidates.map((candidate) => {
                  const on = selected?.instrumentId === candidate.instrumentId;
                  return <li key={candidate.instrumentId}><button className="btn btn-ghost" aria-pressed={on} onClick={() => setSelectedId(candidate.instrumentId)}
                    style={{ width: "100%", textAlign: "left", justifyContent: "flex-start", background: on ? "var(--accent-soft)" : undefined, borderColor: on ? "var(--accent-border)" : "transparent" }}>
                    <span style={{ display: "grid", gap: 4, width: "100%" }}>
                      <span>{candidate.position != null && <span className="mono">#{candidate.position} </span>}<strong>{candidate.displayName}</strong></span>
                      <span className="note mono">{candidate.instrumentId}</span><span className="note">{candidate.board ? boardLabel(candidate.board) : "板块未提供"} · {candidate.industry ?? "行业未知"}</span>
                      {candidate.exclusionReason && <span className="note">未排名原因：{candidate.exclusionReason}</span>}
                      {candidate.basis[0] && <span className="note">{candidate.basis[0]}</span>}
                    </span>
                  </button><button className="btn btn-sm" style={{ margin: "6px 0" }} onClick={() => toggleComparison(candidate)}
                    disabled={!comparisonAvailable || (comparison.cards.length >= 3 && !comparison.cards.some((card) => card.instrumentId === candidate.instrumentId))}>
                    {comparison.cards.some((card) => card.instrumentId === candidate.instrumentId) ? "移出比较" : "加入比较"}
                  </button></li>;
                })}</ul>}
              <div style={{ display: "flex", justifyContent: "space-between", gap: 8, marginTop: 14 }}>
                <button className="btn btn-sm" disabled={pageOffset === 0} onClick={() => { setOffset(Math.max(0, pageOffset - PAGE_SIZE)); setSelectedId(null); }}>上一页</button>
                <button className="btn btn-sm" disabled={nextOffset == null} onClick={() => { if (nextOffset != null) setOffset(nextOffset); setSelectedId(null); }}>下一页</button>
              </div>
            </Card>
            {selected ? <CandidateDetail candidate={selected} result={result} onCompare={() => toggleComparison(selected)}
              comparing={comparison.cards.some((card) => card.instrumentId === selected.instrumentId)}
              comparisonFull={comparison.cards.length >= 3}
              onPrepareSimulation={onPrepareSimulation ? () => onPrepareSimulation(selected.instrumentId, result.snapshotId ?? null) : undefined} /> : <Card><Empty title="暂无可查看的研究依据" /></Card>}
          </div>
        </>}
      <details open={comparison.cards.length > 0 || !!comparison.notice}>
        <summary>证券比较{comparison.cards.length > 0 ? ` · 已选 ${comparison.cards.length} 只` : " · 从研究卡加入"}</summary>
        <div style={{ marginTop: 12 }}><ComparisonPanel state={comparison} available={comparisonAvailable}
          onRemove={(instrumentId) => dispatchComparison({ type: "remove", instrumentId })}
          onClear={() => dispatchComparison({ type: "clear" })} /></div>
      </details>
    </div>
  </Section>;
}
function Coverage({ result }: { result: ResearchCandidatesResponse }) {
  const scope = result.scope;
  return <Card>
    <p><strong>研究数据截至 {result.dataAsOf}</strong><span className="note"> · 研究输入截止 {result.asOfTime ? formatAsOf(result.asOfTime) : "未记录"} · 模拟资格须另行核验</span></p>
    <details style={{ marginTop: 10 }}><summary>数据范围、来源与研究限制</summary>
    <div style={{ marginTop: 12 }}>
    <Callout tone="info" title="研究报价与模拟执行数据分别核验">
      <p>{result.schema === "aquant.research-candidates.v2" ? result.quoteMode === "CLOSING_RESEARCH"
        ? "真实盘后采集行情与已归档财报用于研究；供应商报价时点与日线新鲜度逐证券标注，盘后采集不代表所有报价已更新至收盘。研究报价不等同正式收盘执行快照，不能据此确认交易资格。"
        : "真实分批盘中行情与已归档财报用于当前研究；历史日线窗口另列，研究报价不能直接用于模拟成交。"
        : "本名单为历史重建研究候选，不能直接用作模拟执行数据。"}</p>
      <p className="note" style={{ color: "inherit", marginTop: 4 }}>因子百分位表示比较范围内的相对位置；加权排名值按公开公式汇合，名次按该值排序。均不表示上涨概率或未来收益。</p>
    </Callout>
    <div style={{ display: "flex", gap: 24, flexWrap: "wrap", marginTop: 14 }}>
      <Stat label={scope ? "证券代码范围" : "研究池"} value={(scope?.universeCodeCount ?? result.poolSize) + " 只"} />
      {scope && <Stat label="真实行情观察覆盖" value={scope.observedCount + " 只"} />}
      <Stat label="普通工商参与排名" value={result.rankedCount + " 只"} /><Stat label="未参与排名" value={result.excludedCount + " 只"} />
      {scope && <><Stat label="金融单列" value={scope.financialCount + " 只"} /><Stat label="全部行业未知" value={(result.industryCoverage?.unknownCount ?? scope.unknownIndustryCount) + " 只"} /><Stat label="行情未采集 / 被拒" value={scope.quoteRejectedCount + " 只"} /></>}
    </div>
    {result.industryCoverage && <div style={{ marginTop: 14 }}>
      <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
        <Stat label="主源行业分类" value={result.industryCoverage.primaryCount + " 只"} />
        <Stat label="补充行业分类" value={result.industryCoverage.supplementCount + " 只"} />
        <Stat label="活跃报价中行业未知" value={result.industryCoverage.activeUnknownCount + " 只"} />
      </div>
      <p className="note" style={{ marginTop: 8 }}>选择规则：{result.industryCoverage.selectionRule}</p>
      <p className="note">行业补齐用于识别研究模板，不等于历史行业映射或可模拟资格。</p>
      <details style={{ marginTop: 8 }}><summary className="note" style={{ cursor: "pointer" }}>行业分类体系与版本覆盖</summary>
        <div className="table-wrap" style={{ marginTop: 6 }}><table className="data"><thead><tr><th>体系</th><th>版本</th><th className="num">采用数量</th></tr></thead>
          <tbody>{result.industryCoverage.systems.map((item) => <tr key={item.system + ":" + item.version}><td>{item.system}</td><td>{item.version}</td><td className="num">{item.count}</td></tr>)}</tbody>
        </table></div>
      </details>
    </div>}
    {result.historyCoverage && <p className="note" style={{ marginTop: 12 }}>最新日线：本次刷新 {result.historyCoverage.refreshedCount} 个；当前排名池 {result.historyCoverage.rankedCurrentCount}/{result.historyCoverage.rankedTotal} 个截至研究日（{result.dataAsOf}）。</p>}
    <dl className="kv" style={{ marginTop: 14 }}>
      <dt>研究输入截止</dt><dd>{result.asOfTime ? formatAsOf(result.asOfTime) : "旧版未记录研究输入截止"}</dd>
      <dt>整体采集窗口</dt><dd>{result.capturedFrom && result.capturedTo ? formatAsOf(result.capturedFrom) + " 至 " + formatAsOf(result.capturedTo) : "未提供"}</dd>
      <dt>报价采集窗口</dt><dd>{result.quoteCapturedFrom && result.quoteCapturedTo ? formatAsOf(result.quoteCapturedFrom) + " 至 " + formatAsOf(result.quoteCapturedTo) : "未单独记录"}</dd>
      <dt>比较范围</dt><dd>{result.comparisonScope ?? "当前历史重建研究数据集"}</dd><dt>发布时刻</dt><dd>{formatAsOf(result.generatedAt)}</dd>
      {result.snapshotId && <><dt>研究快照</dt><dd className="mono" style={{ overflowWrap: "anywhere" }}>{result.snapshotId}</dd></>}
    </dl>
    {scope && <div className="table-wrap" style={{ marginTop: 12 }}><table className="data">
      <caption style={{ textAlign: "left", paddingBottom: 8 }}>分板块观察覆盖</caption>
      <thead><tr><th>板块</th><th className="num">请求代码</th><th className="num">真实观察</th><th className="num">参与排名</th></tr></thead>
      <tbody>{scope.byBoard.map((item) => <tr key={item.board}><td>{boardLabel(item.board)}</td><td className="num">{item.requested}</td><td className="num">{item.observed}</td><td className="num">{item.ranked}</td></tr>)}</tbody>
    </table></div>}
    <details style={{ marginTop: 14 }}><summary style={{ cursor: "pointer", fontSize: 13 }}>数据来源、排除原因与研究限制</summary>
      <dl className="kv" style={{ marginTop: 10, overflowWrap: "anywhere" }}>
        <dt>研究策略 / PIT 模式</dt><dd>{result.strategy} / {result.pitMode}</dd><dt>因子报告</dt><dd className="mono">{result.source.factorReport}</dd>
        <dt>总市值来源与日期</dt><dd>{result.source.marketCapSource} · {result.source.marketCapAsOf}</dd>
        <dt>归档核验</dt><dd>{result.source.archiveVerified ? "已通过归档核验" : "归档核验未通过"}</dd><dt>占位数据</dt><dd>{result.source.placeholderUsed ? "使用了占位数据" : "未使用占位数据"}</dd>
      </dl>
      <Notes items={result.exclusions.map((item) => item.code + " · " + item.count + " 只")} empty="无排除原因统计。" />
      <Notes items={result.limitations} empty="未提供研究限制。" />
    </details>
    </div>
    </details>
  </Card>;
}
function SourceRefs({ ids, candidate }: { ids: string[]; candidate: ResearchCandidate }) {
  return <span style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 4, overflowWrap: "anywhere" }}>
    {ids.length ? ids.map((id) => {
      const source = candidate.evidence.find((item) => item.announcementId === id);
      const event = candidate.events?.find((item) => item.sourceId === id);
      if (event?.url.startsWith("https://")) {
        return <a key={id} href={event.url} target="_blank" rel="noopener noreferrer" className="note">{event.title} · {id}</a>;
      }
      if (candidate.quote?.evidenceId === id && candidate.quote.url?.startsWith("https://")) {
        return <a key={id} href={candidate.quote.url} target="_blank" rel="noopener noreferrer" className="note">行情原文来源 · {id}</a>;
      }
      return source?.url.startsWith("https://") ? <a key={id} href={source.url} target="_blank" rel="noopener noreferrer" className="note">{source.title} · {id}</a>
        : <span key={id} className="note mono">来源 ID：{id}</span>;
    }) : <span className="note">未提供可定位来源。</span>}
  </span>;
}
function ComparisonPanel({ state, available, onRemove, onClear }: {
  state: ComparisonState; available: boolean; onRemove: (instrumentId: string) => void; onClear: () => void;
}) {
  const { cards, publication } = state;
  const applicability = (candidate: ResearchCandidate) => isFinancial(candidate) ? "金融单列，普通工商排名不适用"
    : candidate.position == null ? "观察证券，未参与排名" : "普通工商完整财务排名";
  const rows: { label: string; render: (candidate: ResearchCandidate) => ReactNode }[] = [
    { label: "证券身份", render: (candidate) => <>{candidate.code} · {candidate.exchange ?? "交易所未提供"}<br />{candidate.board ? boardLabel(candidate.board) : "板块未提供"}</> },
    { label: "行业与分类日期", render: (candidate) => <>{candidate.industry ?? "行业未知"}<br />分类日期：{industryDate(candidate) ?? "未提供"}<IndustryClassification candidate={candidate} /></> },
    { label: "研究模板 / 状态", render: (candidate) => <>{applicability(candidate)}{candidate.exclusionReason && <p className="note">{candidate.exclusionCode} · {candidate.exclusionReason}</p>}</> },
    { label: "研究数据日期 / 行情时点", render: (candidate) => <>{publication?.dataAsOf ?? "未提供"}<br />行情：{candidate.quote?.asOfTime ? formatAsOf(candidate.quote.asOfTime) : "未采集"}</> },
    { label: "同一比较池内名次", render: (candidate) => candidate.position == null ? "不适用（未参与普通工商排名）" : "第 " + candidate.position + " 名" },
    { label: "加权排名值（0–1）", render: (candidate) => candidate.signalRank == null ? "不适用（未排名）" : signalText(candidate.signalRank) },
    ...FACTORS.map((factor) => ({ label: factor.id + " · " + factor.label, render: (candidate: ResearchCandidate) => {
      if (isFinancial(candidate)) return <p className="note">不适用（金融单列）</p>;
      const row = candidate.rankBreakdown?.find((item) => item.factorId === factor.id);
      const actualValue = row?.value ?? candidate.factors[factor.id];
      return <><span className="mono">{valueText(actualValue)}</span><p className="note">{candidate.position == null ? "未排名，未获得完整适用输入" : "因子池内百分位：" + percentile(row?.rankPct ?? candidate.factorRanks[factor.id])}</p>
        {row && <SourceRefs ids={[...new Set(row.inputs.flatMap((input) => input.evidenceIds))]} candidate={candidate} />}</>;
    } })),
    { label: "主要依据（最多三条）", render: (candidate) => candidate.basisEvidence?.length
      ? <ul className="list">{candidate.basisEvidence.slice(0, 3).map((item, index) => <li className="note" key={index}>{item.statement}<SourceRefs ids={item.sourceIds} candidate={candidate} /></li>)}</ul>
      : <Notes items={candidate.basis.slice(0, 3)} empty="未提供可核验依据。" /> },
    { label: "反证与检索范围", render: (candidate) => candidate.counterEvidence?.length
      ? <ul className="list">{candidate.counterEvidence.map((item, index) => <li className="note" key={index}>{item.noneFound && "未找到反证（限定以下范围）："}{item.statement}<SourceRefs ids={item.sourceIds} candidate={candidate} /></li>)}</ul>
      : <p className="note">未提供检索记录，不代表没有反证。</p> },
    { label: "风险、未知项与缺失", render: (candidate) => <Notes items={[...candidate.risks, ...(candidate.uncertainties ?? [])]} empty="未提供，不代表无风险。" /> },
    { label: "日线截止与窗口", render: (candidate) => candidate.marketContext ? <>
      {candidate.marketContext.historyAsOf ?? "未覆盖日线"}<p className="note">{candidate.marketContext.window}</p>
      {candidate.marketContext.freshness && <p className="note">新鲜度：{candidate.marketContext.freshness}</p>}
      {candidate.marketContext.expectedAsOf && <p className="note">应截至：{candidate.marketContext.expectedAsOf}</p>}
      {candidate.marketContext.tradingDayCount != null && <p className="note">{candidate.marketContext.tradingDayCount} 个交易日</p>}
      <HistorySource source={candidate.marketContext.source} />
    </> : "未覆盖日线，不能判断当前趋势" },
    { label: "原文来源", render: (candidate) => <SourceRefs candidate={candidate} ids={[
      ...candidate.evidence.map((item) => item.announcementId), ...(candidate.events ?? []).map((item) => item.sourceId),
      ...(candidate.quote?.evidenceId ? [candidate.quote.evidenceId] : []),
    ]} /> },
  ];
  return <Card title="只读候选比较" actions={<button className="btn btn-sm" disabled={!cards.length} onClick={onClear}>清空比较</button>}>
    <p className="note">已选择 {cards.length} / 3 只。可从整个发布范围搜索、翻页或切换视图加入比较；选择会保留。</p>
    {state.notice && <p className="note" role="status" style={{ marginTop: 8 }}>{state.notice}</p>}
    {!cards.length ? <Empty title="尚未选择比较证券">在候选名单或深度研究卡点击“加入比较”。</Empty> : !available || !publication ? <>
      <p className="note" style={{ marginTop: 8 }}>当前 API 结果不可用或发布快照不同，比较内容暂停展示。重新读取后仅显示同一快照的证券。</p>
      {cards.map((candidate) => <button key={candidate.instrumentId} className="btn btn-sm" onClick={() => onRemove(candidate.instrumentId)} style={{ margin: "8px 8px 0 0" }}>移除 {candidate.displayName}</button>)}
    </> : <>
      <p className="note" style={{ marginTop: 8 }}>共同来源：{publication.snapshotId ?? "旧版发布 " + publication.generatedAt}</p>
      <p className="note">比较池：{publication.comparisonScope ?? "当前历史重建研究数据集"}金融单列与未排名证券不参与普通工商排序；缺失值不按零比较。</p>
      <div className="table-wrap" style={{ marginTop: 12 }}><table className="data" aria-label="同一研究快照候选并列比较">
        <thead><tr><th scope="col">比较项目</th>{cards.map((candidate) => <th scope="col" key={candidate.instrumentId} style={{ minWidth: 230 }}>
          {candidate.displayName}<div className="note mono">{candidate.instrumentId}</div>
          <button className="btn btn-sm" style={{ marginTop: 6 }} onClick={() => onRemove(candidate.instrumentId)}>移除 {candidate.displayName}</button>
        </th>)}</tr></thead>
        <tbody>{rows.map((row) => <tr key={row.label}><th scope="row" style={{ verticalAlign: "top", minWidth: 150 }}>{row.label}</th>
          {cards.map((candidate) => <td key={candidate.instrumentId} style={{ verticalAlign: "top", overflowWrap: "anywhere" }}>{row.render(candidate)}</td>)}
        </tr>)}</tbody>
      </table></div>
      <FinancialComparison cards={cards} />
    </>}
  </Card>;
}
function IndustryClassification({ candidate }: { candidate: ResearchCandidate }) {
  const classification = candidate.industryClassification;
  if (!classification) return null;
  const source = classification.source;
  let link: string | null = null;
  if (source?.url && source.receiptId && source.contentHash) {
    try {
      const url = new URL(source.url);
      if (url.protocol === "https:" && url.hostname && !url.username && !url.password) link = url.href;
    } catch { /* 分类来源标识不能解析时保留文字，不猜测原文位置。 */ }
  }
  return <details style={{ marginTop: 8 }}><summary className="note" style={{ cursor: "pointer" }}>行业分类体系、版本与来源</summary>
    <dl className="kv" style={{ marginTop: 6, overflowWrap: "anywhere" }}>
      <dt>分类体系 / 版本</dt><dd>{classification.system} / {classification.version}</dd>
      <dt>类别</dt><dd>{valueText(classification.categoryCode)} {classification.categoryName}</dd>
      {(classification.subcategoryCode || classification.subcategoryName) && <><dt>子类别</dt><dd>{classification.subcategoryCode} {classification.subcategoryName}</dd></>}
      {(classification.majorCode || classification.majorName) && <><dt>大类</dt><dd>{classification.majorCode} {classification.majorName}</dd></>}
      <dt>分类日期</dt><dd>{classification.classificationAsOf ?? "未提供分类生效日期"}</dd>
      <dt>资料参考期</dt><dd>{classification.referencePeriod ?? "未提供"}</dd>
      <dt>原始发布日期</dt><dd>{classification.publicationDate ?? "未提供"}</dd>
      <dt>采集观测时点</dt><dd>{classification.observedAt ? formatAsOf(classification.observedAt) : "未提供"}</dd>
      <dt>来源选择</dt><dd>{classification.selection}</dd>
      <dt>研究模板</dt><dd>{{ ORDINARY: "普通工商", FINANCIAL: "金融单列", UNKNOWN: "未知，未推定适用模板" }[classification.template]}</dd>
      {source && <><dt>分类回执</dt><dd className="mono">{source.receiptId ?? "未提供回执 ID"}</dd><dt>内容哈希</dt><dd className="mono">{source.contentHash}</dd>
        {source.receiptLabel && <><dt>来源记录标签</dt><dd>{source.receiptLabel}</dd></>}
        {source.storedPath && <><dt>已归档来源路径</dt><dd className="mono">{source.storedPath}</dd></>}
        {source.page != null && <><dt>原文页码</dt><dd>{source.page}</dd></>}
        {source.sourceRow != null && <><dt>原始行标识</dt><dd>{source.sourceRow}</dd></>}
        {source.queryComplete != null && <><dt>本次索引查询</dt><dd>{source.queryComplete
          ? "所记录范围内的查询已完成；完整性以检索范围说明为准"
          : "有限检索，公告索引未穷尽"}</dd></>}
        {source.queryScope && <><dt>披露检索范围</dt><dd>{source.queryScope}</dd></>}
        <dt>原始分类来源</dt><dd className="mono">{source.url}</dd>
      </>}
    </dl>
    <p className="note" style={{ marginTop: 6 }}>资料参考期、发布日期与采集时点分别记录，不能代替缺失的历史分类生效日期。不同体系按各自版本展示，补充分类不改称主源分类。</p>
    {(source?.queryScope || source?.queryComplete != null) && <p className="note" style={{ marginTop: 6 }}>公司披露仅代表所列已归档材料中的分类描述；单份依据或关键词查询完成，不证明此后行业未变更，也不代表全公告已读。</p>}
    {link && <a href={link} target="_blank" rel="noopener noreferrer" className="btn btn-sm" style={{ marginTop: 6 }}>查看行业分类原文</a>}
  </details>;
}
function FinancialValue({ candidate, metric }: { candidate: ResearchCandidate; metric: string }) {
  const value = candidate.financialObservation?.values.find((item) => item.metric === metric);
  if (!value || value.value == null) return <span className="note">缺失（未补零）</span>;
  return <><span className="mono">{value.value} {value.unit}</span><SourceRefs ids={value.evidenceIds} candidate={candidate} /></>;
}
function financialMissingText(candidate: ResearchCandidate): string {
  const observation = candidate.financialObservation;
  if (!observation) return "尚未发布专用金融原始值观察模板。";
  return observation.missingMetrics.length ? observation.missingMetrics.map((metric) =>
    FINANCIAL_METRICS.find((item) => item.id === metric)?.label ?? metric).join("、") : "本次五字段未记录缺项；不代表金融专用研究资料已完整。";
}
function FinancialObservation({ candidate }: { candidate: ResearchCandidate }) {
  if (!isFinancial(candidate)) return null;
  const observation = candidate.financialObservation;
  return <div>
    <h4>金融原始财报观察（单列，不评分）</h4>
    <p className="note" style={{ marginTop: 6 }}>金额为原报告累计值及权益期末值，不进行 TTM 转换，不使用普通工商现金质量解释。</p>
    {observation ? <>
      <dl className="kv" style={{ marginTop: 6 }}>
        <dt>金融分组模板</dt><dd>{observation.templateLabel} · {observation.template}</dd>
        <dt>报告期 / 报表族</dt><dd>{observation.periodEnd ?? "未提供"} / {observation.reportFamily ?? "未提供"}</dd>
        <dt>字段完整度</dt><dd>{financialStatus(observation.status)}</dd>
        <dt>缺少指标</dt><dd>{financialMissingText(candidate)}</dd>
      </dl>
      <div className="table-wrap" style={{ marginTop: 8 }}><table className="data"><thead><tr><th>原始字段</th><th>实际金额与公告引用</th></tr></thead>
        <tbody>{FINANCIAL_METRICS.map((metric) => <tr key={metric.id}><th scope="row">{metric.label}</th><td><FinancialValue candidate={candidate} metric={metric.id} /></td></tr>)}</tbody>
      </table></div>
      {observation.comparisonKey ? <details style={{ marginTop: 8 }}><summary className="note" style={{ cursor: "pointer" }}>同口径比较标识</summary><p className="note mono" style={{ overflowWrap: "anywhere" }}>{observation.comparisonKey}</p></details>
        : <p className="note" style={{ marginTop: 8 }}>缺少可比口径标识，不能标记为同口径财报观察。</p>}
      {!!observation.limitations.length && <Notes items={observation.limitations} empty="" />}
    </> : <p className="note" style={{ marginTop: 6 }}>{financialMissingText(candidate)}</p>}
  </div>;
}
function financialComparisonMode(cards: ResearchCandidate[]): "EMPTY" | "SINGLE" | "SAME" | "MIXED" {
  const financial = cards.filter(isFinancial);
  if (!financial.length) return "EMPTY";
  if (financial.length === 1) return financial[0]?.financialObservation?.comparisonKey?.trim() ? "SINGLE" : "MIXED";
  const keys = financial.map((candidate) => candidate.financialObservation?.comparisonKey);
  return keys.every((key) => !!key?.trim() && key === keys[0]) ? "SAME" : "MIXED";
}
function FinancialComparison({ cards }: { cards: ResearchCandidate[] }) {
  const financial = cards.filter(isFinancial);
  const mode = financialComparisonMode(cards);
  if (mode === "EMPTY") return null;
  const rows: { label: string; render: (candidate: ResearchCandidate) => ReactNode }[] = [
    { label: "金融分组模板", render: (candidate) => <>{candidate.financialObservation?.templateLabel ?? "未提供"}<p className="note mono">{candidate.financialObservation?.template}</p></> },
    { label: "报告期", render: (candidate) => candidate.financialObservation?.periodEnd ?? "未提供" },
    { label: "报表族", render: (candidate) => candidate.financialObservation?.reportFamily ?? "未提供" },
    { label: "原始字段完整度", render: (candidate) => candidate.financialObservation ? financialStatus(candidate.financialObservation.status) : "缺少专用金融观察模板" },
    ...FINANCIAL_METRICS.map((metric) => ({ label: metric.label, render: (candidate: ResearchCandidate) => <FinancialValue candidate={candidate} metric={metric.id} /> })),
    { label: "缺少指标", render: financialMissingText },
    { label: "口径与资料限制", render: (candidate) => <Notes items={candidate.financialObservation?.limitations ?? []} empty="未提供额外限制，不代表专用金融资料完整。" /> },
  ];
  return <div style={{ marginTop: 20 }}>
    <h4>金融原始值并列观察（不评分）</h4>
    <p className="note" style={{ marginTop: 6 }}>下表仅列已选金融证券；原报告累计值及权益期末值不按普通工商因子排名，缺项不填零。</p>
    <p className="note" style={{ marginTop: 6 }} role="status">{mode === "SAME" ? "同口径财报观察：金融分组、报告期与报表族对应同一非空比较标识。"
      : mode === "SINGLE" ? "已选择一只金融证券；添加同分组、同报告期与同报表族的证券后再检查比较口径。"
      : "口径不同或缺少口径标识，不能作同口径比较；请分别核对金融分组、报告期与报表族。"}</p>
    <div className="table-wrap" style={{ marginTop: 8 }}><table className="data" aria-label="金融原始财报值并列观察">
      <thead><tr><th scope="col">原始财报项目</th>{financial.map((candidate) => <th scope="col" key={candidate.instrumentId} style={{ minWidth: 230 }}>{candidate.displayName}<div className="note mono">{candidate.instrumentId}</div></th>)}</tr></thead>
      <tbody>{rows.map((row) => <tr key={row.label}><th scope="row" style={{ verticalAlign: "top" }}>{row.label}</th>{financial.map((candidate) =>
        <td key={candidate.instrumentId} style={{ verticalAlign: "top", overflowWrap: "anywhere" }}>{row.render(candidate)}</td>)}</tr>)}</tbody>
    </table></div>
  </div>;
}
function HistorySource({ source }: { source: NonNullable<ResearchCandidate["marketContext"]>["source"] }) {
  if (!source || !Object.values(source).some(Boolean)) return null;
  const segments = source.receipts ?? [source];
  return <details style={{ marginTop: 8 }}><summary className="note" style={{ cursor: "pointer" }}>查看真实日线来源与采集留痕</summary>
    <p className="note" style={{ marginTop: 6 }}>未复权日线只提供市场历史上下文，不参与财务信号排名，不确认模拟资格。</p>
    {source.receipts && <p className="note" style={{ marginTop: 6 }}>本窗口由 {segments.length} 段来源记录组成；每段回执与内容哈希仅指认对应采集窗口。</p>}
    {segments.map((segment, index) => {
      let link: string | null = null;
      if (segment.source_url && segment.receipt_id && segment.content_hash) {
        try {
          const url = new URL(segment.source_url);
          if (url.protocol === "https:" && url.hostname && !url.username && !url.password) link = url.href;
        } catch { /* 非HTTP原始请求标识保留为文本，不构造替代网址。 */ }
      }
      return <div key={(segment.receipt_id ?? "source") + ":" + index} style={{ marginTop: 10 }}>
        {source.receipts && <strong className="note">第 {index + 1} 段日线来源</strong>}
        <dl className="kv" style={{ marginTop: 6, overflowWrap: "anywhere" }}>
          {"window" in segment && <><dt>来源窗口</dt><dd>{segment.window}</dd></>}
          {"rowCount" in segment && <><dt>本段行数</dt><dd>{segment.rowCount} 行</dd></>}
          <dt>日线来源</dt><dd>{segment.source ?? "未提供"}</dd>
          <dt>采集回执</dt><dd className="mono">{segment.receipt_id ?? "未提供"}</dd>
          <dt>内容哈希</dt><dd className="mono">{segment.content_hash ?? "未提供"}</dd>
          <dt>采集观察时点</dt><dd>{segment.observed_at ? formatAsOf(segment.observed_at) : "未提供"}</dd>
          <dt>原始日线请求</dt><dd className="mono">{segment.source_url ?? "未提供"}</dd>
        </dl>
        {link && <a href={link} target="_blank" rel="noopener noreferrer" className="btn btn-sm" style={{ marginTop: 6 }}>查看日线原始来源</a>}
      </div>;
    })}
  </details>;
}
function RecentAnnouncements({ candidate }: { candidate: ResearchCandidate }) {
  const context = candidate.eventContext;
  const events = candidate.events ?? [];
  return <div>
    <h4>近期官方公告</h4>
    <p className="note" style={{ marginTop: 6 }}>原文摘录与影响假设分别展示；公告不参与本次财务信号排序。</p>
    <dl className="kv" style={{ marginTop: 6 }}>
      <dt>检索窗口</dt><dd>{context?.window ?? "未提供"}</dd>
      <dt>官方索引检索</dt><dd>{context?.queryComplete ? "本代码窗口索引检索已完成" : "本代码尚未完成检索；不代表无公告或无风险"}</dd>
      <dt>已归档全文数量</dt><dd>{context?.documentCount ?? 0} 份</dd>
      <dt>采集时点</dt><dd>{context?.observedAt ? formatAsOf(context.observedAt) : "尚未采集"}</dd>
      <dt>检索与阅读范围</dt><dd>{context?.scope ?? "未记录检索范围，不能判断公告覆盖"}</dd>
      {context?.missing && <><dt>缺失与覆盖限制</dt><dd>{context.missing}</dd></>}
    </dl>
    {events.length ? <ul className="list" style={{ marginTop: 10 }}>{events.map((event) => {
      const characters = Array.from(event.quote);
      const excerpt = characters.length > 100 ? characters.slice(0, 99).join("") + "…" : event.quote;
      return <li key={event.sourceId}>
        <strong style={{ fontSize: 13 }}>{event.title}</strong>
        <p className="note">公告日期 {event.publishedDate} · {event.page != null ? "原文第 " + event.page + " 页" : "页码未定位"}</p>
        <Badge>{event.verificationStatus === "ARCHIVED_PDF_TEXT" ? "已归档 PDF 正文摘录" : event.verificationStatus}</Badge>
        {excerpt ? <blockquote className="note" style={{ margin: "8px 0", whiteSpace: "pre-line" }}><strong>原文短引用：</strong>{excerpt}</blockquote>
          : <p className="note">未提供可定位的正文引用，需核验原文。</p>}
        {event.url.startsWith("https://") && <a href={event.url} target="_blank" rel="noopener noreferrer" className="btn btn-sm">查看官方公告原文</a>}
        <details style={{ marginTop: 6 }}><summary className="note" style={{ cursor: "pointer" }}>查看公告归档标识</summary>
          <dl className="kv" style={{ marginTop: 6, overflowWrap: "anywhere" }}><dt>来源 ID</dt><dd className="mono">{event.sourceId}</dd>
            {event.receiptId && <><dt>采集回执</dt><dd className="mono">{event.receiptId}</dd></>}
            {event.contentHash && <><dt>内容哈希</dt><dd className="mono">{event.contentHash}</dd></>}
          </dl>
        </details>
      </li>;
    })}</ul> : <p className="note" style={{ marginTop: 8 }}>{context?.queryComplete
      ? "本次检索与有限全文选择范围内未归档可展示公告；不代表窗口内没有公告。"
      : "本代码尚未检索近期公告，不代表无公告或无风险。"}</p>}
  </div>;
}
function CandidateDetail({ candidate, result, onCompare, comparing, comparisonFull, onPrepareSimulation }: {
  candidate: ResearchCandidate; result: ResearchCandidatesResponse; onCompare?: () => void;
  comparing?: boolean; comparisonFull?: boolean;
  onPrepareSimulation?: () => void;
}) {
  const basis = candidate.basisEvidence?.slice(0, 3);
  return <Card title={candidate.displayName + " · 研究卡"} actions={<><Badge tone="neutral">研究资料</Badge>
    {onCompare && <button className="btn btn-sm" style={{ marginLeft: 8 }} disabled={comparisonFull && !comparing} onClick={onCompare}>{comparing ? "移出比较" : "加入比较"}</button>}</>}>
    <div style={{ display: "grid", gap: 16, minWidth: 0 }}>
      <dl className="kv">
        <dt>证券身份</dt><dd className="mono">{candidate.instrumentId} · {candidate.exchange ?? "交易所未提供"}</dd>
        <dt>板块 / 行业</dt><dd>{candidate.board ? boardLabel(candidate.board) : "未提供"} / {candidate.industry ?? "未知"}</dd>
        <dt>研究状态</dt><dd>{candidate.status ?? "历史研究候选"}</dd><dt>数据日期 / 发布</dt><dd>{result.dataAsOf} / {formatAsOf(result.generatedAt)}</dd>
        {candidate.exclusionReason && <><dt>未排名原因</dt><dd>{candidate.exclusionCode} · {candidate.exclusionReason}</dd></>}
      </dl>
      <div><h4 style={{ marginBottom: 6 }}>关键依据（最多三条）</h4>
        {basis?.length ? <ul className="list">{basis.map((item, index) => <li className="note" key={index}>{item.statement}<SourceRefs ids={item.sourceIds} candidate={candidate} /></li>)}</ul> : <Notes items={candidate.basis.slice(0, 3)} empty="本次未提供可核验依据。" />}
      </div>
      <div><h4 style={{ marginBottom: 6 }}>反证与检索范围</h4>
        {candidate.counterEvidence?.length ? <ul className="list">{candidate.counterEvidence.map((item, index) => <li className="note" key={index}>{item.noneFound && <strong>明确未找到反证 · </strong>}{item.statement}<SourceRefs ids={item.sourceIds} candidate={candidate} /></li>)}</ul> : <p className="note">未提供反证检索记录，不能据此声称不存在反证。</p>}
      </div>
      <Callout tone="warn" title="风险、未知项与数据缺失"><Notes items={[...candidate.risks, ...(candidate.uncertainties ?? [])]} empty="未提供风险说明，不表示不存在风险。" /></Callout>
      <div>
        {onPrepareSimulation && <button className="btn btn-primary" onClick={onPrepareSimulation}>检查模拟资格</button>}
        <p className="note" style={{ marginTop: 8 }}>研究报价不能直接用于模拟成交。进入模拟组合后，按这份研究来源核验资格，再由你选择并确认草稿。</p>
        <details style={{ marginTop: 8 }}><summary>本研究资料的模拟限制与修复条件</summary>
          <dl className="kv" style={{ marginTop: 8 }}><dt>具体规则</dt><dd>{candidate.tradability?.rule ?? "研究来源需另行核验模拟资格"}</dd><dt>适用日期</dt><dd>{candidate.tradability?.effectiveFrom ?? result.dataAsOf}</dd>
            <dt>限制说明</dt><dd>{candidate.tradability?.detail ?? "历史重建研究数据不能用于正式 PIT 回测或模拟订单。"}</dd><dt>修复条件</dt><dd>{candidate.tradability?.repair ?? "补齐 PIT 财务、历史行业适用性及交易状态验证后，另行验收模拟接入。"}</dd></dl>
        </details>
      </div>
      {candidate.quote && <div><h4>{result.quoteMode === "CLOSING_RESEARCH" ? "盘后采集的供应商报价" : "真实盘中行情"}</h4><dl className="kv" style={{ marginTop: 6 }}>
        <dt>行情时点</dt><dd>{formatAsOf(candidate.quote.asOfTime)}</dd><dt>最新价 / 总市值</dt><dd>{valueText(candidate.quote.lastPrice)} 元 / {valueText(candidate.quote.marketCapYuan)} 元</dd>
      </dl></div>}
      <details><summary>排名公式与因子贡献</summary>
        <dl className="kv" style={{ marginTop: 8 }}><dt>比较范围</dt><dd>{result.comparisonScope ?? "当前历史重建研究数据集"}</dd><dt>池内候选名次</dt><dd>{candidate.position ?? "未排名"}</dd>
          <dt>加权排名值（0–1）</dt><dd>{signalText(candidate.signalRank)}</dd></dl>
        <p className="note" style={{ marginTop: 6 }}>排名反映同一比较范围内的相对位置，不表示上涨概率或未来收益。</p>
        {isFinancial(candidate) && <p className="note">金融单列研究不使用以下普通工商配方，不计算因子排名或贡献。</p>}
        <p className="note mono" style={{ marginTop: 6, overflowWrap: "anywhere" }}>{result.rankingFormula ?? "旧版未提供可展开的排名公式。"}</p>
        {candidate.rankBreakdown?.length ? <div className="table-wrap" style={{ marginTop: 8 }}><table className="data">
          <thead><tr><th>因子 / 公式与输入</th><th className="num">实际值</th><th className="num">排名百分位</th><th className="num">权重</th><th className="num">贡献</th></tr></thead>
          <tbody>{candidate.rankBreakdown.map((item) => <tr key={item.factorId}>
            <td>{item.name} <span className="mono note">{item.factorId}</span><p className="note mono">{item.formula}</p>
              <details><summary className="note" style={{ cursor: "pointer" }}>展开财务输入与引用</summary><ul className="list">{item.inputs.map((input, index) =>
                <li key={index} className="note">{input.label}：{valueText(input.value)} {input.unit}{input.periodEnd && " · " + input.periodEnd}<SourceRefs ids={input.evidenceIds} candidate={candidate} /></li>)}</ul></details>
            </td><td className="num mono">{valueText(item.value)}</td><td className="num">{percentile(item.rankPct)}</td><td className="num">{(item.weight * 100).toFixed(2)}%</td><td className="num mono">{valueText(item.contribution)}</td>
          </tr>)}</tbody>
        </table></div> : candidate.signalRank == null ? <p className="note">此证券未进入普通工商排名。{candidate.exclusionReason}</p> :
          <div className="table-wrap" style={{ marginTop: 8 }}><table className="data"><thead><tr><th>旧版因子</th><th>实际值</th><th>排名百分位</th></tr></thead>
            <tbody>{FACTORS.map((factor) => <tr key={factor.id}><td>{factor.label}</td><td className="mono">{valueText(candidate.factors[factor.id])}</td><td>{percentile(candidate.factorRanks[factor.id])}</td></tr>)}</tbody></table></div>}
      </details>
      <details><summary>{isFinancial(candidate) ? "金融原报告指标与公告引用（单列研究）" : "TTM 财务桥与公告引用"}</summary>
        <div style={{ marginTop: 8 }}><FinancialObservation candidate={candidate} /></div>
        {candidate.financialBridge?.length ? <div style={{ display: "grid", gap: 10, marginTop: 8 }}>{candidate.financialBridge.map((item) =>
          <details key={item.metric + ":" + item.label}><summary style={{ cursor: "pointer", fontSize: 13 }}>{item.label}：{valueText(item.value)} {item.unit}</summary><p className="note mono" style={{ margin: "6px 0", overflowWrap: "anywhere" }}>{item.formula}</p>
            <ul className="list">{item.components.map((part, index) => <li className="note" key={index}>{part.sign < 0 ? "减" : "加"} {Math.abs(part.sign) !== 1 && Math.abs(part.sign) + " × "}{part.periodEnd}：{valueText(part.value)} {item.unit}<SourceRefs ids={[part.evidenceId]} candidate={candidate} /></li>)}</ul>
          </details>)}</div> : <p className="note">{isFinancial(candidate) ? "金融原报告指标尚未完整发布；需补齐报告期字段与原文核验，不使用普通工商现金质量解释。" : "未提供可复算的 TTM 财务桥；需补齐各报告期数据及公告引用。"}</p>}
      </details>
      <RecentAnnouncements candidate={candidate} />
      <details><summary>市场历史上下文（独立日线窗口）</summary>
        {candidate.marketContext ? <><dl className="kv" style={{ marginTop: 6 }}><dt>日线截至日期</dt><dd>{candidate.marketContext.historyAsOf ?? "无历史日线"}</dd><dt>各自观察窗口</dt><dd>{candidate.marketContext.window}</dd>
          {candidate.marketContext.freshness && <><dt>日线新鲜度</dt><dd>{candidate.marketContext.freshness}</dd></>}
          {candidate.marketContext.expectedAsOf && <><dt>应截至交易日</dt><dd>{candidate.marketContext.expectedAsOf}</dd></>}
          {candidate.marketContext.tradingDayCount != null && <><dt>覆盖交易日</dt><dd>{candidate.marketContext.tradingDayCount} 个交易日</dd></>}
          {candidate.marketContext.metrics.map((item, index) => <Fragment key={index}><dt>{item.label}</dt><dd>{valueText(item.value)} {item.unit}</dd></Fragment>)}
        </dl><Notes items={candidate.marketContext.limitations} empty="未提供历史窗口限制。" /><HistorySource source={candidate.marketContext.source} /></> : <p className="note">缺少历史日线窗口，无法判断价格是否已消化财报信息。</p>}
      </details>
      <div><h4>财报与公告的影响路径及验证条件</h4>
        {candidate.impactPaths?.length ? <ul className="list">{candidate.impactPaths.map((item, index) => <li key={index} className="note"><p><strong>观察事实：</strong>{item.observation}</p><p><strong>待验证假设：</strong>{item.hypothesis}</p><p><strong>验证条件：</strong>{item.verify}</p><SourceRefs ids={item.sourceIds} candidate={candidate} /></li>)}</ul> : <p className="note">未提供经过来源绑定的影响路径；不生成事件或 AI 收益结论。</p>}
      </div>
      <details><summary>来源详情、公告原文与核验记录</summary>
        <div style={{ display: "grid", gap: 12, marginTop: 12 }}>
        <dl className="kv"><dt>行业分类日期</dt><dd>{industryDate(candidate) ?? "未提供；不能作为历史行业映射"}</dd></dl>
        <IndustryClassification candidate={candidate} />
        {candidate.quote && <details><summary>报价来源与采集记录</summary><dl className="kv" style={{ marginTop: 8, overflowWrap: "anywhere" }}>
          <dt>行情时点</dt><dd>{formatAsOf(candidate.quote.asOfTime)}</dd><dt>采集回执</dt><dd className="mono">{candidate.quote.receiptId}</dd><dt>内容哈希</dt><dd className="mono">{candidate.quote.contentHash}</dd>
        </dl><SourceRefs ids={candidate.quote.evidenceId ? [candidate.quote.evidenceId] : []} candidate={candidate} /></details>}
        <div><h4 style={{ marginBottom: 6 }}>公告原文与核验记录</h4>
        {!candidate.evidence.length ? <p className="note">本次未提供可定位的原文证据。</p> : <ul className="list">{candidate.evidence.map((item, index) =>
          <li key={item.announcementId + ":" + item.periodEnd + ":" + index}><strong style={{ fontSize: 13 }}>{item.title}</strong><p className="note">报告期 {item.periodEnd} · 公告日期 {item.publishedDate}</p>
            {item.excerpt && <p className="note">摘录：{item.excerpt}</p>}
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8, margin: "6px 0" }}><Badge tone={item.valueVerification === "MATCH" ? "ok" : "neutral"}>{item.valueVerification === "MATCH" ? "数值核对匹配" : "结构化数值已绑定公告"}</Badge>
              {item.url.startsWith("https://") && <a href={item.url} target="_blank" rel="noopener noreferrer" className="btn btn-sm">查看公告原文</a>}
            </div>
            <details><summary className="note" style={{ cursor: "pointer" }}>查看证据标识与结构化值</summary><dl className="kv" style={{ marginTop: 6, overflowWrap: "anywhere" }}><dt>公告 ID</dt><dd className="mono">{item.announcementId}</dd><dt>核验回执</dt><dd className="mono">{item.receiptId}</dd><dt>内容哈希</dt><dd className="mono">{item.contentHash}</dd>
              {item.availableAt && <><dt>研究可用时点</dt><dd>{formatAsOf(item.availableAt)}</dd></>}
              {item.observedAt && <><dt>原始材料采集</dt><dd>{formatAsOf(item.observedAt)}</dd></>}
              {Array.isArray(item.metrics) ? <><dt>绑定财务指标</dt><dd>{item.metrics.join("、")}</dd></>
                : Object.entries(item.metrics ?? {}).map(([label, metric]) => <Fragment key={label}><dt>{label}</dt><dd className="mono">{valueText(metric)}</dd></Fragment>)}
            </dl></details>
          </li>)}</ul>}
        </div>
        </div>
      </details>
    </div>
  </Card>;
}
