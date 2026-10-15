import { useEffect, useState } from "react";
import { api, AquantApiError, type EngineeringSimulationRun, type ForwardComparisonSeries, type SimulationAttributionReport } from "../lib/api";
import { formatAsOf, formatCents } from "../lib/format";
import { Badge, Callout, Card, Empty, Section } from "./ui";

const pct = (value: string | number | undefined | null) => value == null ? "未计算" : `${Number(value).toFixed(4)}%`;
const feeLabel = (code: string) => ({ COMMISSION: "佣金", MIN_COMMISSION_TOPUP: "最低佣金补足",
  STAMP_DUTY: "印花税", TRANSFER_FEE: "过户费" }[code] ?? code);
const errorText = (error: unknown) => error instanceof AquantApiError
  ? `${error.message} · ${error.envelope.repairAction}` : String(error);
const statusLabel = (status: string) => ({ COMPLETED: "账本已完成", OBSERVING: "持续观测中",
  WAITING_EXECUTION_SNAPSHOT: "等待执行日行情", DATA_NOT_READY: "实际数据待补齐" }[status] ?? status);

/** The view reads stored evidence independently; there is no synthetic fallback. */
export function EngineeringForwardResults() {
  const [runs, setRuns] = useState<EngineeringSimulationRun[]>([]);
  const [series, setSeries] = useState<ForwardComparisonSeries[]>([]);
  const [reports, setReports] = useState<SimulationAttributionReport[]>([]);
  const [errors, setErrors] = useState<{ engineering: string | null; forward: string | null; attribution?: string | null }>({ engineering: null, forward: null });
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true);
    void Promise.allSettled([api.engineeringSimulationRuns(), api.forwardComparisons(), api.engineeringAttributions()]).then(([engineering, forward, accounting]) => {
      if (!active) return;
      setRuns(engineering.status === "fulfilled" ? engineering.value.runs : []);
      setSeries(forward.status === "fulfilled" ? forward.value.series : []);
      setReports(accounting.status === "fulfilled" ? accounting.value.reports : []);
      setErrors({ engineering: engineering.status === "rejected" ? errorText(engineering.reason) : null,
        forward: forward.status === "rejected" ? errorText(forward.reason) : null,
        attribution: accounting.status === "rejected" ? errorText(accounting.reason) : null });
      setLoading(false);
    });
    return () => { active = false; };
  }, [attempt]);
  return <EngineeringForwardResultsDisplay runs={runs} series={series} reports={reports} errors={errors} loading={loading}
    onRefresh={() => setAttempt(value => value + 1)} />;
}

export function EngineeringForwardResultsDisplay({ runs, series, reports = [], errors, loading, onRefresh }: {
  runs: EngineeringSimulationRun[]; series: ForwardComparisonSeries[];
  reports?: SimulationAttributionReport[];
  errors: { engineering: string | null; forward: string | null; attribution?: string | null }; loading: boolean; onRefresh: () => void;
}) {
  return <>
    <Section title="最新候选实际模拟账本" hint="保存于开盘前的原草稿 · 独立工程账户" dataSource="api"
      actions={<button className="btn" disabled={loading} onClick={onRefresh}>刷新最新试运行</button>}>
      {errors.engineering && <Callout tone="danger" title="工程账本读取失败">{errors.engineering}</Callout>}
      {errors.attribution && <Callout tone="warn" title="会计复盘读取失败">{errors.attribution}</Callout>}
      {loading ? <Card><Empty title="正在读取实际模拟结果" /></Card> : !errors.engineering && (runs.length === 0
        ? <Card><Empty title="尚无已保存的最新候选执行结果">待真实执行日行情到齐后，独立工程账户才会产生订单、成交和净值。</Empty></Card>
        : runs.map(run => <Card key={run.runId}>
          <h3>{run.tradingDay} 最新候选试运行 <Badge tone={run.status === "COMPLETED" ? "ok" : "warn"}>{statusLabel(run.status)}</Badge></h3>
          <p className="note">工程模拟，用户确认：无；正式策略：未开放。
            {run.delayedEngineeringCalculation && " 本次行情后补，属于延迟工程计算，原草稿和实际观察时刻保留。"}</p>
          {run.error?.message && <Callout tone="warn" title="本次执行待补齐">{run.error.message}</Callout>}
          {run.blockingIssues?.map((issue, index) => <p className="note" key={index}>{issue.message}</p>)}
          <dl className="kv"><dt>原草稿实际保存</dt><dd>{formatAsOf(run.draftCreatedAt)}</dd>
            <dt>决策截止</dt><dd>{formatAsOf(run.decisionCutoffAt)}</dd>
            <dt>执行行情实际截止</dt><dd>{run.executionCutoffAt ? formatAsOf(run.executionCutoffAt) : "尚未到齐"}</dd>
            <dt>订单 / 成交 / 拒单</dt><dd>{run.orders?.length ?? 0} / {run.fills?.length ?? 0} / {run.rejections?.length ?? 0}</dd>
            <dt>实际累计费用</dt><dd>{run.reconciliation ? formatCents(run.reconciliation.fees_total_cents) : "尚无账本"}</dd>
            <dt>期末现金</dt><dd>{run.cashCents != null ? formatCents(run.cashCents) : "尚无账本"}</dd>
            <dt>期末净资产</dt><dd>{run.valuation?.published ? formatCents(run.valuation.net_value_cents) : "尚未发布"}</dd>
            <dt>净收益</dt><dd>{run.valuation?.published && run.initialCashCents
              ? pct((run.valuation.net_value_cents / run.initialCashCents - 1) * 100) : "尚未计算"}</dd>
            <dt>现金、持仓、费用对账</dt><dd>{run.reconciliation ? run.reconciliation.reconciled ? "一致" : "未通过" : "待执行"}</dd></dl>
          {!!run.orders?.length && <details open><summary>真实订单与拒单原因</summary><div className="table-wrap"><table className="data">
            <thead><tr><th>证券</th><th>方向</th><th>股数</th><th>状态</th><th>拒单原因</th></tr></thead>
            <tbody>{run.orders.map(order => <tr key={order.order_id}><td className="mono">{order.instrument_id}</td>
              <td>{order.side}</td><td>{order.quantity}</td><td>{order.status}</td><td>{order.reject_reason ?? "—"}</td></tr>)}</tbody>
          </table></div></details>}
          {!!run.fills?.length && <details><summary>真实模拟成交与费用</summary><div className="table-wrap"><table className="data">
            <thead><tr><th>证券</th><th>方向</th><th>股数</th><th>成交价</th><th>费用</th></tr></thead>
            <tbody>{run.fills.map(fill => <tr key={fill.fill_id}><td className="mono">{fill.instrument_id}</td><td>{fill.side}</td>
              <td>{fill.quantity}</td><td>{formatCents(fill.price_cents)}</td><td>{formatCents(fill.fees_total_cents)}</td></tr>)}</tbody>
          </table></div></details>}
          {!!run.positions && <details><summary>期末持仓</summary><ul className="list">{Object.entries(run.positions).map(([iid, quantity]) => <li key={iid}>{iid} · {quantity} 股</li>)}</ul></details>}
          <AccountingReviewDisplay report={reports.find(report => report.originId === run.runId && report.tradingDay === run.tradingDay)} />
          <details><summary>原来源与实际执行时刻</summary><dl className="kv"><dt>工程账户 / 运行 ID</dt><dd className="mono">{run.portfolioId}<br />{run.runId}</dd>
            <dt>原草稿</dt><dd className="mono">{run.draftId}</dd><dt>原研究 / 哈希</dt><dd className="mono">{run.researchSnapshotId}<br />{run.researchContentHash}</dd>
            <dt>决策 / 执行快照</dt><dd className="mono">{run.decisionSnapshotId}<br />{run.executionSnapshotId ?? "尚无"}</dd>
            <dt>实际完成时刻</dt><dd>{run.completedAt ? formatAsOf(run.completedAt) : "尚未完成"}</dd></dl></details>
          <ul className="list">{run.limitations.map((note, index) => <li className="note" key={index}>{note}</li>)}</ul>
        </Card>))}
    </Section>
    <Section title="M/E/H/B 持续前向观察" hint="先固定真实原信息和规则，再等待后续行情" dataSource="api">
      {errors.forward && <Callout tone="danger" title="前向观察读取失败">{errors.forward}</Callout>}
      {loading ? <Card><Empty title="正在读取冻结前向方案" /></Card> : !errors.forward && (series.length === 0
        ? <Card><Empty title="尚无实际登记的前向观察方案" /></Card>
        : series.map(record => <Card key={record.seriesId}>
          <h3>{record.experimentVersion} <Badge tone={record.observations.length ? "ok" : "warn"}>{statusLabel(record.status)}</Badge></h3>
          <dl className="kv"><dt>实际登记时刻</dt><dd>{formatAsOf(record.createdAt)}</dd><dt>首次执行交易日</dt><dd>{record.firstTradingDay}</dd>
            <dt>冻结共同证券池</dt><dd>{record.commonPoolCount} 只</dd><dt>每账户初始资金</dt><dd>{formatCents(record.initialCashCents)}</dd>
            <dt>已保存交易日 / 观察目标</dt><dd>{record.observedSessionCount} / {record.observationTargetSessions}，只统计已有真实 EOD</dd>
            <dt>用户 H 组合</dt><dd>{record.human.status === "UNAVAILABLE" ? "尚不可用" : record.human.status} · {record.human.reason}</dd>
            <dt>广义市场参考</dt><dd>尚不可用 · {record.broadMarketReference.reason}</dd></dl>
          <p className="note">{record.eventInterpretation} 当前 E1 阻断 {record.eventBuyBlockedInstrumentIds.length} 只；登记时 M 目标 {record.modelTargets.length} 只，E 目标 {record.eventTargets.length} 只，B 同池等权目标 {record.benchmarkTargets.length} 只。</p>
          {record.observations.length === 0 ? <Callout tone="warn" title="尚无实际成交或前向收益">首次执行日快照尚未到齐，未创建用于展示收益的虚拟账户。</Callout>
            : record.observations.map(day => <details open key={day.tradingDay}><summary>{day.tradingDay} · 实际 EOD 与对账</summary>
              <p className="note">保存于 {formatAsOf(day.observedAt)} · 实际截止 {formatAsOf(day.cutoffAt)}
                {day.delayedEngineeringCalculation && " · 行情后补的延迟工程计算"}</p>
              <div className="table-wrap"><table className="data"><thead><tr><th>组合</th><th>净收益</th><th>净资产</th><th>累计费用</th><th>现金比例</th><th>股票暴露</th><th>累计成交</th><th>对账</th></tr></thead>
                <tbody>{["M", "E", "H", "B"].map(kind => { const portfolio = day.portfolios[kind]; return <tr key={kind}>
                  <td>{kind}</td><td>{portfolio?.status === "UNAVAILABLE" ? "尚不可用" : pct(portfolio?.netReturnPct)}</td>
                  <td>{portfolio?.netValueCents != null ? formatCents(portfolio.netValueCents) : "—"}</td>
                  <td>{portfolio?.feesTotalCents != null ? formatCents(portfolio.feesTotalCents) : "—"}</td><td>{pct(portfolio?.cashPct)}</td>
                  <td>{pct(portfolio?.equityPct)}</td><td>{portfolio?.fillCount ?? "—"}</td><td>{portfolio?.reconciled ? "一致" : "—"}</td></tr>; })}</tbody>
              </table></div>
              {["M", "E", "B"].map(kind => {
                const execution = day.portfolios[kind]?.dayExecution;
                return <details key={kind}><summary>{kind} · 当日真实模拟订单与成交明细</summary>
                  {!execution ? <p className="note">原观测未保存当日明细；不以累计成交数代替。</p> : <>
                    <p className="note">{execution.tradingDay} · 订单 {execution.orderCount} / 成交 {execution.fillCount} / 未完全成交订单 {execution.unfilledOrderCount}；
                      当日费用 {formatCents(execution.feesTotalCents)}，双边成交额 {formatCents(execution.grossTurnoverCents)}。
                      实际记录于 {formatAsOf(execution.recordedAt)}。</p>
                    {execution.orderCount === 0 && execution.fillCount === 0 && <p className="note">本日没有新增订单或成交；净值仍按实际日终行情估值。</p>}
                    {!!execution.orders.length && <div className="table-wrap"><table className="data"><thead><tr>
                      <th>证券</th><th>方向</th><th>股数</th><th>状态</th><th>拒单或未成交原因</th></tr></thead><tbody>
                      {execution.orders.map(order => <tr key={order.order_id}><td className="mono">{order.instrument_id}</td>
                        <td>{order.side}</td><td>{order.quantity}</td><td>{order.status}</td><td>{order.reject_reason ?? "—"}</td></tr>)}
                    </tbody></table></div>}
                    {!!execution.fills.length && <div className="table-wrap"><table className="data"><thead><tr>
                      <th>证券</th><th>方向</th><th>股数</th><th>成交价</th><th>成交额</th><th>费用及项目</th><th>实际入账</th></tr></thead><tbody>
                      {execution.fills.map(fill => <tr key={fill.fill_id}><td className="mono">{fill.instrument_id}</td><td>{fill.side}</td>
                        <td>{fill.quantity}</td><td>{formatCents(fill.price_cents)}</td><td>{formatCents(fill.gross_amount_cents)}</td>
                        <td>{formatCents(fill.fees_total_cents)}<br />{fill.feeCharges.map(charge => `${feeLabel(charge.fee_code)} ${formatCents(charge.amount_cents)}`).join("；")}</td>
                        <td>{formatAsOf(fill.filled_at)}</td></tr>)}
                    </tbody></table></div>}
                  </>}
                </details>;
              })}
              {day.missingEodDays.length > 0 && <p className="note">缺少日终：{day.missingEodDays.join("、")}；不能把观测样本称为连续交易日。</p>}
              <details><summary>实际行业暴露与快照</summary>{["M", "E", "B"].map(kind => <p className="note" key={kind}>{kind}：{Object.entries(day.portfolios[kind]?.industryExposurePct ?? {}).map(([industry, weight]) => `${industry} ${pct(weight)}`).join("；") || "尚无持仓"}</p>)}<p className="mono note">{day.snapshotId}</p></details>
              <AccountingReviewDisplay report={day.attribution ?? undefined} />
            </details>)}
          <details><summary>固定规则、费用和原来源</summary><dl className="kv"><dt>观察 ID</dt><dd className="mono">{record.seriesId}</dd>
            <dt>原研究 / 内容哈希</dt><dd className="mono">{record.researchSnapshotId}<br />{record.researchContentHash}</dd><dt>研究实际可得</dt><dd>{formatAsOf(record.researchAvailableAt)}</dd>
            <dt>决策快照 / 截止</dt><dd className="mono">{record.decisionSnapshotId}<br />{record.decisionCutoffAt}</dd><dt>执行政策</dt><dd>初次固定建仓后持有；每日估值，不自动生成用户新订单。</dd>
            <dt>佣金来源</dt><dd>{record.fees.commissionSource}</dd></dl><pre className="note mono" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{JSON.stringify({ parameters: record.constructionParams, fees: record.fees.firstDaySchedule }, null, 2)}</pre></details>
          <ul className="list">{record.limitations.map((note, index) => <li className="note" key={index}>{note}</li>)}</ul>
        </Card>))}
    </Section>
  </>;
}

export function AccountingReviewDisplay({ report }: { report?: SimulationAttributionReport }) {
  if (!report) return <p className="note">尚无此日已保存的归因复盘；不会用当前行情补写。</p>;
  return <details open><summary>{report.tradingDay} · 原判断、实际执行与会计归因</summary>
    <p className="note">{report.basis}</p>
    <p className="note">贡献、成交、费用及换手从账户初始资金累计至此日 EOD；尚不足以据此解释单日择时或人工能力。</p>
    {Object.entries(report.portfolios).map(([kind, p]) => <details open key={kind}><summary>{kind === "ENGINEERING_SELECTION" ? "工程选择账户" : kind} · 净资产与现金桥</summary>
      <dl className="kv"><dt>初始资金</dt><dd>{formatCents(p.netAssetBridge.initialCashCents)}</dd>
        <dt>价格损益</dt><dd>{formatCents(p.netAssetBridge.pricePnlCents)}</dd><dt>公司行为现金与应收损益</dt><dd>{formatCents(p.netAssetBridge.corporateActionPnlCents)}</dd>
        <dt>其他证券现金 / 扣除费用</dt><dd>{formatCents(p.netAssetBridge.otherSecurityCashCents)} / {formatCents(p.netAssetBridge.feesCents)}</dd>
        <dt>未分配损益残差</dt><dd>{formatCents(p.netAssetBridge.unallocatedResidualCents)}</dd>
        <dt>实际期末净资产</dt><dd>{formatCents(p.netAssetBridge.finalNetValueCents)}</dd>
        <dt>净资产 / 现金桥对账</dt><dd>{p.netAssetBridge.reconciled && p.cashBridge.reconciled ? "一致" : "未通过"}</dd>
        <dt>买入 / 卖出结算现金</dt><dd>{formatCents(p.cashBridge.buySettlementCents)} / {formatCents(p.cashBridge.sellSettlementCents)}</dd>
        <dt>费用 / 公司行为现金</dt><dd>{formatCents(p.cashBridge.feeCashCents)} / {formatCents(p.cashBridge.corporateActionCashCents)}</dd>
        <dt>期末现金 / 现金桥残差</dt><dd>{formatCents(p.cashBridge.finalCashCents)} / {formatCents(p.cashBridge.unallocatedResidualCents)}</dd>
        <dt>现金 / 股票暴露</dt><dd>{pct(p.exposure.cashPct)} / {pct(p.exposure.equityPct)}</dd>
        <dt>最大行业占权益 / HHI</dt><dd>{pct(p.exposure.largestIndustryPctEquity)} / {p.exposure.industryHhiEquity == null ? "未计算" : Number(p.exposure.industryHhiEquity).toFixed(4)}</dd>
        <dt>原目标 / 提交订单 / 成交</dt><dd>{p.execution.targetCount} / {p.execution.orderCount} / {p.execution.fillCount}</dd>
        <dt>未成交订单 / 股数比例</dt><dd>{pct(p.execution.unfilledOrderPct)} / {pct(p.execution.unfilledQuantityPct)}</dd>
        <dt>整手不足未生成订单</dt><dd>{p.execution.belowMinimumLotCount} 个目标；与未成交订单分开统计</dd>
        <dt>双边成交额 / 初始资金</dt><dd>{pct(p.execution.turnoverGrossPctInitialCapital)}，含建仓</dd></dl>
      <div className="table-wrap"><table className="data"><thead><tr><th>证券</th><th>行业</th><th>价格损益</th><th>公司行为</th><th>费用</th><th>净贡献</th><th>占初始资金</th></tr></thead><tbody>
        {p.accounting.securities.map(row => <tr key={row.instrument_id}><td className="mono">{row.instrument_id}</td><td>{row.industry_code}</td><td>{formatCents(row.price_pnl_cents)}</td><td>{formatCents(row.corporate_action_pnl_cents)}</td><td>{formatCents(row.fees_cents)}</td><td>{formatCents(row.net_pnl_cents)}</td><td>{pct(row.net_contribution_pct_initial_capital)}</td></tr>)}
      </tbody></table></div>
      <details><summary>行业贡献与原冻结目标</summary><div className="table-wrap"><table className="data"><thead><tr><th>行业</th><th>价格损益</th><th>公司行为</th><th>费用</th><th>净贡献</th></tr></thead><tbody>
        {p.accounting.industries.map(row => <tr key={row.industry_code}><td>{row.industry_code}</td><td>{formatCents(row.price_pnl_cents)}</td><td>{formatCents(row.corporate_action_pnl_cents)}</td><td>{formatCents(row.fees_cents)}</td><td>{formatCents(row.net_pnl_cents)}</td></tr>)}
      </tbody></table></div><ul className="list">{p.originalTargets.map(row => <li key={row.instrument_id}>{row.instrument_id} · 原行业 {row.industry_code} · 原权重 {pct(row.weight_pct)}</li>)}</ul></details>
      {!!p.execution.noOrderTargets.length && <details><summary>未生成订单的目标与证据</summary><ul className="list">{p.execution.noOrderTargets.map(row => <li key={row.instrumentId}>{row.instrumentId} · {row.reason === "BELOW_MINIMUM_LOT" ? "目标资金不足一整手" : "原记录缺少可核验原因"} · 原权重 {pct(row.weightPct)} · 参考价 {row.referencePriceCents != null ? formatCents(row.referencePriceCents) : "未知"} · 每手 {row.lotSize ?? "未知"} 股</li>)}</ul></details>}
      <details><summary>原判断与账本证据哈希</summary><pre className="note mono" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{JSON.stringify(p.originalProposal, null, 2)}</pre><p className="mono note">{p.evidenceHash}</p></details>
    </details>)}
    {Object.entries(report.comparisons).map(([name, d]) => <details open key={name}><summary>{d.left} − {d.right} · 可核对的会计差异</summary>
      <p className="note">{d.interpretation}</p><dl className="kv"><dt>净资产 / 净收益差</dt><dd>{formatCents(d.net_wealth_difference_cents)} / {Number(d.net_return_difference_pct_points).toFixed(4)} 个百分点</dd>
        <dt>直接费用差贡献 / 其余损益差</dt><dd>{formatCents(d.direct_fee_debit_difference_cents)} / {formatCents(d.remaining_wealth_difference_cents)}</dd>
        <dt>未分配贡献差 / 对账</dt><dd>{formatCents(d.unallocated_contribution_difference_cents)} / {d.accounting_contribution_differences_reconciled ? "一致" : "未通过"}</dd>
        <dt>现金比例差</dt><dd>{Number(d.cash_pct_difference_points).toFixed(4)} 个百分点</dd><dt>仅左侧 / 仅右侧持有</dt><dd>{d.added_final_holdings.join("、") || "无"} / {d.removed_final_holdings.join("、") || "无"}</dd></dl>
      <div className="table-wrap"><table className="data"><thead><tr><th>证券</th><th>行业</th><th>价格损益差</th><th>公司行为差</th><th>费用差</th><th>净贡献差</th></tr></thead><tbody>
        {d.security_accounting_contribution_differences.map(row => <tr key={row.instrument_id}><td>{row.instrument_id}</td><td>{row.industry_code}</td><td>{formatCents(row.price_pnl_cents_difference)}</td><td>{formatCents(row.corporate_action_pnl_cents_difference)}</td><td>{formatCents(row.fees_cents_difference)}</td><td>{formatCents(row.net_pnl_cents_difference)}</td></tr>)}
      </tbody></table></div><ul className="list">{d.industry_accounting_contribution_differences.map(row => <li key={row.industry_code}>{row.industry_code} · {formatCents(row.net_pnl_cents_difference)}</li>)}</ul>
    </details>)}
    <p className="note">因果选股、择时、持有期及现金效应：{report.causalAttribution.reason}。未核验广义市场参考和 H 不补写比较收益。</p>
    <ul className="list">{report.limitations.map((note, index) => <li className="note" key={index}>{note}</li>)}</ul>
    <p className="note mono">原结果哈希 {report.originHash}<br />独立归因哈希 {report.contentHash}</p>
  </details>;
}
