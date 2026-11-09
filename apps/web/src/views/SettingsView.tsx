import { useCallback, useEffect, useState } from "react";
import { api, AquantApiError, type ScheduleStatus } from "../lib/api";
import { Badge, Callout, Card, Empty, Section } from "../components/ui";
import { formatAsOf } from "../lib/format";

type ScheduleDraft = {
  enabled: boolean;
  runAtLocal: string;
  weekdaysOnly: boolean;
  interpreter: string;
  dataDir: string;
  windowStart: string;
};

function fromStatus(status: ScheduleStatus): ScheduleDraft {
  return {
    enabled: status.schedule.enabled,
    runAtLocal: status.schedule.runAtLocal,
    weekdaysOnly: status.schedule.weekdaysOnly,
    interpreter: status.schedule.interpreter,
    dataDir: status.schedule.dataDir,
    windowStart: status.schedule.windowStart,
  };
}

function errorMessage(error: unknown): string {
  return error instanceof AquantApiError ? error.message : String(error);
}

export function SettingsView() {
  const [status, setStatus] = useState<ScheduleStatus | null>(null);
  const [loadState, setLoadState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [draft, setDraft] = useState<ScheduleDraft>({
    enabled: false, runAtLocal: "", weekdaysOnly: false,
    interpreter: "", dataDir: "", windowStart: "",
  });

  const load = useCallback(async () => {
    setLoadState("loading");
    setError(null);
    setNotice(null);
    try {
      const current = await api.schedule();
      setStatus(current);
      setDraft(fromStatus(current));
      setLoadState("ready");
    } catch (err) {
      setError(errorMessage(err));
      setLoadState("error");
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const save = async () => {
    if (loadState !== "ready" || !status || busy) return;
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      const saved = await api.saveSchedule(draft);
      setStatus(saved);
      setDraft(fromStatus(saved));
      setNotice(saved.schedule.enabled
        ? "设置已保存。每日运行需要数据执行服务在线。"
        : "设置已保存，每日任务已停用。");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const runNow = async () => {
    if (loadState !== "ready" || !status || busy) return;
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      const result = await api.runPipelineNow("界面请求立刻运行");
      setStatus(result);
      setDraft(fromStatus(result));
      setNotice("已登记运行请求 " + result.requestId + "。请在待处理请求和最近运行中查看执行状态。");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  if (loadState === "loading") {
    return <Section title="设置" hint="每日任务" dataSource="api">
      <Card><p className="note" role="status" aria-live="polite">正在读取每日任务配置和运行状态…</p></Card>
    </Section>;
  }

  if (loadState === "error" || !status) {
    return <Section title="设置" hint="每日任务" dataSource="api">
      <Card>
        <Callout tone="danger" title="无法读取每日任务状态">
          <p>{error ?? "当前没有可用的调度状态。"}</p>
          <button className="btn btn-sm" onClick={() => void load()}>重新读取</button>
        </Callout>
      </Card>
    </Section>;
  }

  const schedule = status.schedule;
  const last = status.lastRun;
  const pending = status.pending;
  const disabled = busy || loadState !== "ready";

  return <>
    <Section title="每日任务" hint="设置运行时间并查看更新状态" dataSource="api">
      <Card>
        <Callout tone="info" title="运行状态">
          每日任务会按设定时间登记运行请求，实际采集需要数据执行服务在线。若请求长时间待处理，检查执行服务后重新读取状态。
        </Callout>
        <div className="grid-2" style={{ marginTop: 12 }}>
          <div>
            <h4 style={{ marginBottom: 6 }}>当前配置</h4>
            <dl className="kv">
              <dt>每日任务</dt><dd><Badge tone={schedule.enabled ? "ok" : "neutral"}>{schedule.enabled ? "已启用" : "已停用"}</Badge></dd>
              <dt>运行时间</dt><dd className="mono">{schedule.runAtLocal}（本机时间）</dd>
              <dt>运行日</dt><dd>{schedule.weekdaysOnly ? "周一至周五" : "每天"}</dd>
              <dt>下一次</dt><dd className="mono">{status.nextFireAt ? formatAsOf(status.nextFireAt) : "—"}</dd>
              <dt>配置更新</dt><dd className="note">{schedule.updatedAt
                ? formatAsOf(schedule.updatedAt) + " · " + (schedule.updatedBy ?? "—")
                : "从未修改"}</dd>
            </dl>
          </div>
          <div>
            <h4 style={{ marginBottom: 6 }}>执行情况</h4>
            <dl className="kv">
              <dt>待处理请求</dt><dd>{pending
                ? <Badge tone="warn">{pending.status} · {pending.requestId}</Badge>
                : <span className="note">无</span>}</dd>
              <dt>状态读取时间</dt><dd className="mono">{formatAsOf(status.checkedAt)}</dd>
            </dl>
          </div>
        </div>
      </Card>
    </Section>

    <Section title="修改设置" dataSource="api">
      <Card>
        <div style={{ display: "grid", gap: 10, maxWidth: 640 }}>
          <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <input type="checkbox" checked={draft.enabled} disabled={disabled}
              onChange={event => setDraft(current => ({ ...current, enabled: event.target.checked }))} />
            <span>启用每日任务</span>
          </label>
          <label style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <span className="note">运行时间</span>
            <input className="input mono" type="time" value={draft.runAtLocal} disabled={disabled}
              onChange={event => setDraft(current => ({ ...current, runAtLocal: event.target.value }))} />
            <span className="note">本机时间</span>
          </label>
          <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <input type="checkbox" checked={draft.weekdaysOnly} disabled={disabled}
              onChange={event => setDraft(current => ({ ...current, weekdaysOnly: event.target.checked }))} />
            <span>只在周一至周五运行；休市日会按计划跳过</span>
          </label>

          <details className="secondary-section">
            <summary>高级运行参数</summary>
            <div style={{ display: "grid", gap: 10, marginTop: 10 }}>
              <label style={{ display: "grid", gap: 4 }}>
                <span className="note">Python 解释器（需安装行情数据源依赖）</span>
                <input className="input mono" value={draft.interpreter} disabled={disabled}
                  placeholder={String.raw`E:\IT\Agent\.venv\Scripts\python.exe`}
                  onChange={event => setDraft(current => ({ ...current, interpreter: event.target.value }))} />
              </label>
              <label style={{ display: "grid", gap: 4 }}>
                <span className="note">数据目录（留空使用执行服务的默认目录）</span>
                <input className="input mono" value={draft.dataDir} disabled={disabled}
                  onChange={event => setDraft(current => ({ ...current, dataDir: event.target.value }))} />
              </label>
              <label style={{ display: "grid", gap: 4 }}>
                <span className="note">比较窗口起点</span>
                <input className="input mono" type="date" value={draft.windowStart} disabled={disabled}
                  onChange={event => setDraft(current => ({ ...current, windowStart: event.target.value }))} />
              </label>
              <dl className="kv">
                <dt>执行服务</dt><dd>独立运行；命令 <span className="mono">python tools/scheduler_worker.py</span></dd>
              </dl>
            </div>
          </details>

          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 4 }}>
            <button className="btn btn-primary" onClick={() => void save()} disabled={disabled}>
              {busy ? "处理中…" : "保存设置"}
            </button>
            <button className="btn" onClick={() => void runNow()} disabled={disabled}>立刻运行一次</button>
            <button className="btn btn-ghost" onClick={() => void load()} disabled={busy}>重新读取</button>
          </div>
          {notice && <Callout tone="ok" title="请求已处理"><span className="note">{notice}</span></Callout>}
          {error && <Callout tone="danger" title="操作未完成">{error}</Callout>}
        </div>
      </Card>
    </Section>

    <Section title="最近一次运行" hint="实际结果和步骤记录" dataSource="api">
      {!last ? <Card><Empty title="尚无运行记录">运行请求完成后，结果会显示在这里。</Empty></Card> : <Card>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <Badge tone={last.outcome === "PUBLISHED" ? "ok" : last.outcome === "SKIPPED" ? "neutral" : "danger"}>
            {last.outcome ?? "未知"}
          </Badge>
          <Badge tone="neutral">来源 {last.source === "scheduler" ? "到点自动" : "手工/界面"}</Badge>
          <span className="note mono">
            {last.finishedAt ? formatAsOf(last.finishedAt) : "—"}
            {last.durationSeconds != null ? ` · ${last.durationSeconds}s` : ""}
            {last.exitCode != null ? ` · 退出码 ${last.exitCode}` : ""}
          </span>
        </div>
        <dl className="kv" style={{ marginTop: 10 }}>
          <dt>交易日</dt><dd className="mono">{last.tradingDay ?? "—"}</dd>
          <dt>快照</dt><dd className="mono">{last.snapshotId ?? "—"}</dd>
          <dt>说明</dt><dd>{last.reason ?? "—"}</dd>
        </dl>
        {last.steps.length > 0 && <div className="table-wrap" style={{ marginTop: 10 }}>
          <table className="data">
            <thead><tr><th>步骤</th><th>结果</th><th className="num">耗时</th></tr></thead>
            <tbody>{last.steps.map(step => <tr key={step.step}>
              <td>{step.step}</td>
              <td><Badge tone={step.ok ? "ok" : "danger"}>{step.ok ? "成功" : "失败"}</Badge></td>
              <td className="num mono">{step.seconds != null ? step.seconds + "s" : "—"}</td>
            </tr>)}</tbody>
          </table>
        </div>}
      </Card>}
    </Section>
  </>;
}
