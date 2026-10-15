import { useEffect, useState } from "react";
import { api, AquantApiError, type ComparisonResult } from "../lib/api";
import { formatCents } from "../lib/format";
import { Badge, Callout, Card, Empty, Section } from "./ui";

const labels: Record<string, string> = { M: "M 模型基线", E: "E 事件影子", H: "H 选择组合", B: "B 同池等权" };
const pct = (v: string | null | undefined) => v != null && Number.isFinite(Number(v)) ? Number(v).toFixed(4) + "%" : "未计算";
const decimal = (v: string | null | undefined) => v != null && Number.isFinite(Number(v)) ? Number(v).toFixed(4) : "未计算";

function EvaluationDisplay({ result }: { result: ComparisonResult }) {
  if (!result.portfolios) return null;
  const portfolios = ["M", "E", "H", "B"].flatMap(k => result.portfolios?.[k] ? [{ k, p: result.portfolios[k] }] : []);
  return <details open><summary>暴露、执行与风险统计</summary>
    <div className="table-wrap"><table className="data"><thead><tr><th>组合</th><th>权益暴露：期末 / 观测均值</th>
      <th>最大行业 / 权益占比</th><th>行业 HHI</th><th>双边换手</th><th>未成交：订单 / 股数</th>
      <th>相对 B 收益差</th><th>观测区间波动</th><th>最大请求 / ADV</th></tr></thead><tbody>
      {portfolios.map(({ k, p }) => { const e = p.evaluation; return <tr key={k}><td>{labels[k]}</td>
        <td>{pct(e?.market_exposure.final_equity_pct ?? p.series.at(-1)?.equity_pct)} / {pct(e?.market_exposure.mean_observed_equity_pct)}</td>
        <td>{e?.industry_concentration.final_largest_industry_code ?? "未保存"} / {pct(e?.industry_concentration.final_largest_industry_pct_equity)}</td>
        <td>{decimal(e?.industry_concentration.final_hhi_equity)}</td>
        <td>{pct(e?.turnover.gross_pct_mean_observed_assets)}</td>
        <td>{pct(e?.unfilled.unfilled_order_pct)} / {pct(e?.unfilled.unfilled_quantity_pct)}</td>
        <td>{p.benchmark_difference ? `${decimal(p.benchmark_difference.net_return_difference_pct_points)} 个百分点` : "未保存"}</td>
        <td>{pct(e?.volatility.observed_interval_sample_std_pct)}</td><td>{pct(e?.capacity_proxy.max_requested_participation_pct)}</td>
      </tr>; })}
    </tbody></table></div>
    <p className="note">权益暴露为股票市值占净资产，观测均值按已有 EOD 等权；行业 HHI 按股票行业权重平方求和，现金不作为行业。双边换手含建仓，分母为观测日净资产均值。相对 B 的差额未调整不同现金与市场暴露。</p>
    <p className="note">观测区间波动是相邻观测净收益的样本标准差；缺失交易日时不能称日波动或据此年化。容量只展示冻结历史成交量参与率代理。</p>
    {portfolios.map(({ k, p }) => { const e = p.evaluation; return <details key={k}><summary>{labels[k]} · 指标口径与未计算原因</summary>
      <dl className="kv"><dt>订单 / 未成交订单</dt><dd>{p.order_count ?? "未保存"} / {p.unfilled_order_count ?? "未保存"}</dd>
        <dt>成交额 / 初始资金</dt><dd>{pct(p.turnover_gross_pct_initial_capital)}</dd>
        <dt>年化波动</dt><dd>{pct(p.annualized_volatility_pct)} · {p.volatility_basis}</dd>
        {e && <><dt>买入 / 卖出成交额</dt><dd>{formatCents(e.turnover.buy_traded_cents)} / {formatCents(e.turnover.sell_traded_cents)}</dd>
          <dt>权益暴露观测范围</dt><dd>{pct(e.market_exposure.min_observed_equity_pct)} – {pct(e.market_exposure.max_observed_equity_pct)}</dd>
          <dt>最大行业占净资产</dt><dd>{pct(e.industry_concentration.final_largest_industry_pct_net_assets)}</dd>
          <dt>未知行业占权益</dt><dd>{pct(e.industry_concentration.unknown_industry_pct_equity)}</dd>
          <dt>未成交原因 / 订单数</dt><dd>{Object.entries(e.unfilled.reasons).map(([reason, count]) => `${reason}: ${count}`).join("；") || (e.unfilled.order_count ? "全部成交" : "无订单，未成交率未计算")}</dd>
          <dt>容量代理证据</dt><dd>{e.capacity_proxy.status} · 完整 {e.capacity_proxy.lookback_trading_days} 日 ADV {e.capacity_proxy.full_lookback_order_count}/{e.capacity_proxy.order_count} 单</dd>
          <dt>最大实际成交参与率 / 模拟上限</dt><dd>{pct(e.capacity_proxy.max_filled_participation_pct)} / {pct(e.capacity_proxy.simulator_participation_cap_pct)}</dd>
          <dt>成交量上限拒绝单数</dt><dd>{e.capacity_proxy.volume_cap_rejection_count}</dd>
          <dt>Sharpe</dt><dd>未计算 · {e.volatility.sharpe_unavailable_reason}</dd></>}
        {p.benchmark_difference && <><dt>相对 B 期末净资产差</dt><dd>{formatCents(p.benchmark_difference.net_wealth_difference_cents)}</dd>
          <dt>广义市场指数差额</dt><dd>未计算 · {p.benchmark_difference.broad_market_unavailable_reason}</dd></>}
      </dl>
      {e ? <ul className="list">{[e.market_exposure.basis, e.industry_concentration.basis, e.turnover.basis,
        e.unfilled.basis, e.volatility.basis, e.capacity_proxy.basis, e.capacity_proxy.reason,
        e.capacity_proxy.capital_capacity_unavailable_reason, p.benchmark_difference?.basis].filter(Boolean).map((text, i) => <li className="note" key={i}>{text}</li>)}</ul>
        : <p className="note">旧版不可变记录没有保存完整暴露、执行与风险统计；缺项显示未计算。</p>}
      <details><summary>逐观测日净值、暴露与整手限制</summary><div className="table-wrap"><table className="data"><thead><tr><th>日期</th><th>净资产</th><th>现金</th><th>股票权益</th><th>最大行业占权益</th><th>行业 HHI</th><th>目标数 / 低于整手数</th></tr></thead><tbody>
        {p.series.map(day => { const run = result.portfolio_runs?.[k]?.days.find(value => value.trading_day === day.trading_day);
          return <tr key={day.trading_day}><td>{day.trading_day}</td><td>{formatCents(day.net_value_cents)}</td><td>{pct(day.cash_pct)}</td><td>{pct(day.equity_pct)}</td><td>{pct(day.largest_industry_pct_equity)}</td><td>{decimal(day.industry_hhi_equity)}</td><td>{run?.target_count ?? "未保存"} / {run?.below_minimum_lot_target_count ?? "未保存"}</td></tr>; })}
      </tbody></table></div><p className="note">低于整手的目标未生成订单，不能计入已提交订单的未成交率，也不能视为已持仓。</p></details>
    </details>; })}
  </details>;
}

export function ComparisonResults() {
  const [results, setResults] = useState<ComparisonResult[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let active = true; setLoading(true);
    api.comparisons().then(r => { if (active) { setResults(r.results); setError(null); } })
      .catch(e => { if (active) { setResults([]); setError(e instanceof AquantApiError ? `${e.message} · ${e.envelope.repairAction}` : String(e)); } })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [attempt]);
  return <ComparisonResultsDisplay results={results} error={error} loading={loading}
    onRefresh={() => setAttempt(v => v + 1)} />;
}

export function ComparisonResultsDisplay({ results, error, loading, onRefresh }: {
  results: ComparisonResult[]; error: string | null; loading: boolean; onRefresh: () => void;
}) {
  return <Section title="四组合对照与归因复盘" hint="同一初始资金、信息、费用与执行规则" dataSource="api"
    actions={<button className="btn" disabled={loading} onClick={onRefresh}>刷新运行结果</button>}>
    {error && <Callout tone="danger" title="对照结果读取失败">{error}</Callout>}
    {loading ? <Card><Empty title="正在读取对照记录" /></Card> : results.length === 0 ? <Card><Empty title="尚无已保存的四组合试运行" /></Card> : results.map(r => <Card key={r.run_id}>
      <h3>{r.experiment_version} <Badge tone={r.status === "COMPLETED" ? "ok" : "warn"}>{r.status}</Badge></h3>
      <p className="note">{r.run_kind === "ENGINEERING_REPLAY" ? "真实行情工程回放" : "前向模拟"} · {r.human_origin === "ENGINEERING_REPLAY" ? "H 为工程选择样本" : "H 为有记录的人工选择"}</p>
      {r.error && <Callout tone="danger" title="该次运行未完成">{r.error.message}</Callout>}
      {r.pending_reason && <Callout tone="warn" title="待执行">{r.pending_reason}</Callout>}
      {r.portfolios && <div className="table-wrap"><table className="data"><thead><tr><th>组合</th><th>净收益</th><th>期末资产</th><th>累计费用</th><th>成交笔数</th><th>期末现金</th><th>观测回撤</th></tr></thead><tbody>
        {["M", "E", "H", "B"].map(k => { const p = r.portfolios?.[k]; if (!p) return null;
          return <tr key={k}><td>{labels[k]}</td><td className="num mono">{pct(p.net_return_pct)}</td><td className="num mono">{formatCents(p.final_net_value_cents)}</td>
            <td className="num mono">{formatCents(p.fees_total_cents)}</td><td>{p.fill_count}</td><td>{pct(p.series.at(-1)?.cash_pct)}</td><td>{pct(p.max_drawdown_pct)}</td></tr>; })}
      </tbody></table><p className="note">初始资金 {formatCents(r.portfolios.M?.initial_cash_cents ?? 0)}；观测日 {r.portfolios.M?.series.map(s => s.trading_day).join("、")}</p></div>}
      {r.observation_basis && <p className="note">EOD 样本 {r.observation_basis.sample_count ?? "未保存"}；{r.observation_basis.continuous_eod ? "连续" : "非连续"}。
        {r.observation_basis.missing_eod_days?.length ? `缺少 ${r.observation_basis.missing_eod_days.join("、")}；观测回撤可能低估。` : r.observation_basis.drawdown_basis}</p>}
      <EvaluationDisplay result={r} />
      {r.event_shadow?.interpretation && <p className="note">{r.event_shadow.interpretation}</p>}
      {r.human_difference && <details open><summary>H 相对 M 的损益贡献</summary><p>{r.human_difference.interpretation}</p>
        <dl className="kv"><dt>期末净资产差</dt><dd>{formatCents(r.human_difference.net_wealth_difference_cents)}</dd>
          <dt>直接费用差贡献</dt><dd>{formatCents(r.human_difference.direct_fee_debit_difference_cents)}</dd>
          <dt>其余损益差</dt><dd>{formatCents(r.human_difference.remaining_wealth_difference_cents)}</dd>
          <dt>贡献与账本对账</dt><dd>{r.human_difference.accounting_contribution_differences_reconciled ? "一致" : "未验证"}</dd></dl>
        <details open><summary>H 与 M 的期末持仓、现金和行业暴露差</summary>
          <p className="note">这是期末账户状态差，不能据此还原用户增删股或判断择时能力；H 的来源见运行标识。</p>
          <dl className="kv"><dt>仅 H 持有</dt><dd>{r.human_difference.added_final_holdings?.join("、") || "无 / 未保存"}</dd>
            <dt>仅 M 持有</dt><dd>{r.human_difference.removed_final_holdings?.join("、") || "无 / 未保存"}</dd>
            <dt>H−M 现金比例</dt><dd>{decimal(r.human_difference.cash_pct_difference_points)} 个百分点</dd></dl>
          <ul className="list">{Object.entries(r.human_difference.industry_exposure_difference_pct_points ?? {}).map(([industry, difference]) => <li key={industry}>{industry} · {decimal(difference)} 个百分点（占净资产）</li>)}</ul>
        </details>
        <div className="table-wrap"><table className="data"><thead><tr><th>证券</th><th>行业</th><th>价格损益差</th><th>公司行为差</th><th>净贡献差</th></tr></thead><tbody>
          {r.human_difference.security_accounting_contribution_differences?.map(c => <tr key={c.instrument_id}><td className="mono">{c.instrument_id}</td><td>{c.industry_code}</td>
            <td>{formatCents(c.price_pnl_cents_difference)}</td><td>{formatCents(c.corporate_action_pnl_cents_difference)}</td><td>{formatCents(c.net_pnl_cents_difference)}</td></tr>)}
        </tbody></table></div>
        <details><summary>行业贡献汇总</summary><ul className="list">{r.human_difference.industry_accounting_contribution_differences?.map(c => <li key={c.industry_code}>{c.industry_code} · {formatCents(c.net_pnl_cents_difference)}</li>)}</ul></details>
        <p className="note">{r.human_difference.unknown_reason}</p>
      </details>}
      <ul className="list">{r.limitations.map((l, i) => <li className="note" key={i}>{l}</li>)}</ul>
      <details><summary>冻结运行版本</summary><p className="mono note">{r.run_id}</p></details>
    </Card>)}
  </Section>;
}
