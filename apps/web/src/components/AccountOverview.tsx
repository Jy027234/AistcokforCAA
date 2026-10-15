import { useEffect, useState } from "react";
import { api, AquantApiError, type LedgerResponse } from "../lib/api";
import { formatCents } from "../lib/format";
import { Badge, Callout, Card, Empty } from "./ui";

export function AccountOverview({ portfolioId, active, refreshKey = 0, onOpenResearch, onAvailabilityChange }: {
  portfolioId: string; active: boolean; refreshKey?: number; onOpenResearch?: (instrumentId: string) => void;
  onAvailabilityChange?: (available: boolean | null) => void;
}) {
  const [ledger, setLedger] = useState<LedgerResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [missingAccount, setMissingAccount] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!active) return;
    let current = true; setLoading(true); setError(null); setMissingAccount(false);
    void api.ledger(portfolioId).then(value => {
      if (value.portfolio_id !== portfolioId) throw new Error("账户账本与当前组合不一致，请重新读取。");
      if (current) { setLedger(value); onAvailabilityChange?.(true); }
    }).catch(cause => {
      if (current) {
        const missing = cause instanceof AquantApiError && cause.status === 404 && cause.message.includes("unknown portfolio");
        setLedger(null); setMissingAccount(missing); onAvailabilityChange?.(missing ? false : null);
        if (!missing) setError(cause instanceof AquantApiError ? `${cause.message} · ${cause.envelope.repairAction}` : String(cause));
      }
    }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [portfolioId, active, refreshKey, attempt, onAvailabilityChange]);
  return <Card title="当前现金与模拟持仓" actions={<button className="btn btn-sm" disabled={loading} onClick={() => setAttempt(value => value + 1)}>刷新账户</button>}>
    {loading ? <Empty title="正在读取账户账本" /> : missingAccount ? <Empty title="尚未创建模拟账户">
      先在下方保存股票选择；执行资料到齐并生成预览时，系统才会建立模拟账户。当前没有账户资金或持仓可展示。
    </Empty> : error ? <Callout tone="warn" title="账户账本尚未读到">{error}</Callout> : ledger && <>
      <div className="grid-2">
        <div className="stat"><span className="stat-label">账本现金</span><span className="stat-value">{ledger.cash.cents == null ? ledger.cash.display : formatCents(ledger.cash.cents)}</span></div>
        <div className="stat"><span className="stat-label">实际模拟持仓</span><span className="stat-value">{ledger.positions.length} 只</span></div>
      </div>
      <p className="note" style={{ marginTop: 10 }}><Badge tone="accent">模拟账户</Badge> 净值以完成日终估值后的发布结果为准。</p>
      {ledger.positions.length === 0 ? <p style={{ marginTop: 12 }}>当前账本没有模拟持仓。你可以研究股票，或记录继续持有现金的决定。</p> :
        <details style={{ marginTop: 12 }}><summary>查看模拟持仓</summary><ul className="list">{ledger.positions.map(position => <li key={position.instrument_id}>
          {position.instrument_id} · {position.quantity.toLocaleString()} 股
          {onOpenResearch && <button className="btn btn-sm" style={{ marginLeft: 8 }} onClick={() => onOpenResearch(position.instrument_id)}>查看研究</button>}
        </li>)}</ul></details>}
      <details style={{ marginTop: 12 }}><summary>账户来源</summary><p className="note mono">{ledger.portfolio_id} · {ledger.opened_at}</p></details>
    </>}
  </Card>;
}
