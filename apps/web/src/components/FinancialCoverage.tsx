import { useEffect, useState } from "react";
import { api, AquantApiError } from "../lib/api";
import type { FinancialCoverageReport } from "../lib/financialCoverage";
import { formatAsOf } from "../lib/format";
import { Badge, Callout, Card, Empty, Section } from "./ui";

const metricLabels: Record<string, string> = { revenue: "营业收入", net_profit_attributable: "归母净利润",
  net_profit_consolidated: "合并净利润", operating_cashflow: "经营现金流", parent_equity: "归母权益" };
const statusLabels: Record<string, string> = { ACCEPTED: "已核验可用", FACT_MISSING: "缺少正式事实",
  NEXT_CUTOFF_PENDING: "已采集，待后续截止", FACT_AMBIGUOUS: "金额版本有歧义",
  REVIEW_MISSING: "缺少原文核验", VERSION_CHAIN_BLOCKED: "修订链待核验" };

export function FinancialCoverage({ snapshotId }: { snapshotId: string }) {
  const [report, setReport] = useState<FinancialCoverageReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true); setError(null); setReport(null);
    void api.financialCoverage().then(value => {
      if (active) setReport(value);
    }).catch((cause: unknown) => {
      if (active) setError(cause instanceof AquantApiError
        ? `${cause.message} · ${cause.envelope.repairAction}` : String(cause));
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [snapshotId, attempt]);
  return <FinancialCoverageDisplay report={report} error={error} loading={loading}
    onRefresh={() => setAttempt(value => value + 1)} />;
}

export function FinancialCoverageDisplay({ report, error, loading, onRefresh }: {
  report: FinancialCoverageReport | null; error: string | null; loading: boolean; onRefresh: () => void;
}) {
  return <Section title="财务覆盖与补齐进度" hint="按模拟池逐股核对指标、期间和可用时间" dataSource="api"
    actions={<button className="btn" disabled={loading} onClick={onRefresh}>刷新财务覆盖</button>}>
    {error && <Callout tone="danger" title="财务覆盖读取失败">{error}</Callout>}
    {loading ? <Card><Empty title="正在核对财务覆盖" /></Card> : !error && report && <Card>
      <p className="note">核对于 {formatAsOf(report.generatedAt)}；快照输入截止 {formatAsOf(report.snapshotCutoffAt)}。
        本次公式需求基准为 {report.targetPeriodEnd}，逐券报告期分别列明，新补采保留实际可用时间。</p>
      <dl className="kv">
        <dt>模拟池可选财务记录</dt><dd>{report.s1Optional.statementSecurityCount} / {report.s1Optional.poolCount} 只</dd>
        <dt>可计算归母利润 TTM</dt><dd>{report.s1Optional.ttmSecurityCount} / {report.s1Optional.poolCount} 只</dd>
        <dt>本次截止完整正式财务</dt><dd>{report.formalS2.completeFactSecurityCount} / {report.s1Optional.poolCount} 只；
          {report.formalS2.acceptedMetricPeriodCount} / {report.formalS2.requiredMetricPeriodCount} 项指标与期间</dd>
        <dt>正式四因子可用证券</dt><dd>{report.formalS2.completeFactorSecurityCount} 只</dd>
        <dt>同池完整正式排名</dt><dd>{report.formalS2.rankingPoolSize ?? 0} 只；输入范围 {report.formalS2.scopeStatus ?? "CLOSED"}</dd>
        <dt>同日市值缺失或尚未核验</dt><dd>{report.formalS2.missingMarketCapCount ?? "待刷新"} 只</dd>
        <dt>已采集、待后续截止</dt><dd>{report.formalS2.pendingMetricPeriodCount} 项正式指标与期间</dd>
        <dt>原报事实已核验完整、待后续截止</dt><dd>{report.formalS2.pendingCompleteFactSecurityCount ?? "待刷新"} 只；四因子仍需市值和有效分母</dd>
        <dt>历史重建财务齐全</dt><dd>{report.historicalReconstructed.completeFactSecurityCount} / {report.s1Optional.poolCount} 只</dd>
      </dl>
      <Callout tone="warn" title={report.formalS2.strategyFamilyStatus === "CLOSED" ? "正式 S2 尚未开放" : "正式 S2 状态待核对"}>
        历史重建财务用于研究解释。正式财务需同时通过原文核验、修订检查和可用时间检查；有财务记录不代表四因子已经就绪。
      </Callout>
      <p className="note">模拟池可选财务来自本次冻结输入，后补财务不改写已保存的决策和收益。</p>
      <details><summary>查看全部 {report.securities.length} 只证券的财务缺项（优先目标在前）</summary>
        {[...report.securities].sort((a, b) => Number(b.isPriority) - Number(a.isPriority)).map(row => <details key={row.instrumentId}>
          <summary>{row.displayName || row.code} · {row.instrumentId} {row.isPriority && <Badge tone="accent">M/E 优先目标</Badge>}
            {row.formalS2.completePendingFacts && <Badge tone="warn">原报事实完整，待后续截止</Badge>}
            {" · 正式 "}{row.formalS2.acceptedMetricPeriodCount}/{row.formalS2.requiredMetricPeriodCount}
            {" · 重建 "}{row.historicalReconstructed.availableMetricPeriodCount}/{row.formalS2.requiredMetricPeriodCount}</summary>
          <p className="note">公式需求截止：{row.targetPeriodEnd}。归母利润 TTM：{row.s1Optional.ttmAvailable ? "可计算" : "尚缺资料"}；
            缺少期间：{row.s1Optional.missingPeriods.join("、") || "无"}。
            {row.formalS2.factorExclusionCode && ` 正式因子限制：${row.formalS2.factorExclusionCode}`}</p>
          {row.targetPeriodDiscovery && <p className="note">官方最新报告期发现：
            {row.targetPeriodDiscovery.latest_period_end ?? "仍待完整索引核对"}。
            {row.targetPeriodDiscovery.index_observed_at && ` 实际观察 ${formatAsOf(row.targetPeriodDiscovery.index_observed_at)}。`}
            {row.targetPeriodDiscovery.latest_period_end && !row.targetPeriodDiscovery.effective_at_cutoff &&
              " 新发现晚于本次快照截止，保留为后续输入，不改变原需求。"}</p>}
          <FinancialInputBridge input={row.formalS2} />
          <div style={{ overflowX: "auto" }}><table className="data"><thead><tr>
            <th>指标</th><th>报告期</th><th>正式状态</th><th>原值（元）</th><th>实际首见 / 可用时间</th>
          </tr></thead><tbody>{row.formalS2.metricPeriods.map(item => <tr key={`${item.metric}-${item.periodEnd}`}>
            <td>{metricLabels[item.metric] || item.metric}</td><td>{item.periodEnd}</td>
            <td>{statusLabels[item.status] || item.status}</td><td>{item.valueYuan ?? "—"}</td>
            <td>{item.firstSeenAt ? formatAsOf(item.firstSeenAt) : "—"}<br />{item.availableAt ? formatAsOf(item.availableAt) : "—"}</td>
          </tr>)}</tbody></table></div>
        </details>)}
      </details>
    </Card>}
  </Section>;
}

export function FinancialInputBridge({ input }: { input: FinancialCoverageReport["securities"][number]["formalS2"] }) {
  const values = input.financialInputs;
  const cap = input.marketCap;
  const pending = input.financialInputStatus === "NEXT_CUTOFF_PENDING";
  return <div>
    <p><strong>财务计算与真实分母（元）</strong>{pending && <Badge tone="warn">待后续截止，尚未用于当前决策</Badge>}</p>
    {values ? <>
      <p className="note">TTM 区间端点 {values.ttm_start_period_end} → {values.latest_period_end}；
        {pending ? "以下为已核验原报的后续截止输入。" : "以下为当前快照可用输入。"}</p>
      <div style={{ overflowX: "auto" }}><table className="data"><thead><tr><th>因子</th><th>分子</th><th>分母及计算</th></tr></thead>
        <tbody>
          <tr><td>F07 归母 ROE</td><td>归母利润 TTM {values.attributable_net_profit_ttm}</td>
            <td>平均归母权益 {values.average_parent_equity}<br />（{values.start_parent_equity} + {values.end_parent_equity}）÷ 2</td></tr>
          <tr><td>F08 现金质量</td><td>经营现金流 TTM {values.operating_cashflow_ttm}</td>
            <td>合并净利润 TTM {values.consolidated_net_profit_ttm}</td></tr>
          <tr><td>F09 收入增长</td><td>收入 TTM {values.revenue_ttm}</td>
            <td>上年收入 TTM {values.prior_revenue_ttm}；两者比值减 1</td></tr>
          <tr><td>F10 盈利收益率</td><td>归母利润 TTM {values.attributable_net_profit_ttm}</td>
            <td>{cap?.status === "AVAILABLE" ? `同日总市值 ${cap.value_yuan}` : "同日总市值缺失或尚未核验"}</td></tr>
        </tbody></table></div>
      {!!values.non_positive_denominator_factors?.length && <p className="note">
        业务排除：{values.non_positive_denominator_factors.join("、")} 分母非正，不进入普通工商 S2 排名。</p>}
    </> : <p className="note">完整同口径、同版本原报事实尚未齐全；分母不填零。</p>}
    {cap?.status === "AVAILABLE" && <p className="note">市值日期 {cap.as_of}；来源 {cap.source_id}；
      实际观察 {cap.observed_at ? formatAsOf(cap.observed_at) : "—"}。</p>}
    {input.signal && <p className="note">完整正式 S2 同池百分位 {input.signal.signal_rank.toFixed(6)}；
      F07 {input.signal.factors.F07}、F08 {input.signal.factors.F08}、F09 {input.signal.factors.F09}、F10 {input.signal.factors.F10}。</p>}
  </div>;
}
