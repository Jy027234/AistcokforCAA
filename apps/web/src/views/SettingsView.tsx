import { useCallback, useEffect, useState } from "react";
import { api, AquantApiError, type ScheduleStatus } from "../lib/api";
import { Badge, Callout, Card, Empty, Section } from "../components/ui";
import { formatAsOf } from "../lib/format";

/** 设置：每日任务的调度（§14.2）。
 *
 * 为什么这个页面存在
 * ----------------
 * 规格把调度放在**产品之外**（Windows 任务计划程序 / cron），理由是
 * 产品不该假设自己常驻。但"每天几点跑"是一个使用者每天都会关心的设置，
 * 让它只能去改系统计划任务，等于把一个产品设置藏进了运维。
 *
 * 折中做法：**配置在界面上，执行在独立 worker 里**。
 * 界面写入的只是一条运行请求，真正的采集与建快照由
 * `tools/scheduler_worker.py` 执行——与手工运行同一条路径。
 *
 * 三条界面约束（都不是文案约定，而是数据形状）：
 *   * 调度状态一律「颜色 + 文字」双通道；
 *   * 没有运行记录时显示"尚无运行记录"，**不显示 0**；
 *   * worker 不在跑时**明确说出来**——否则使用者会以为任务在跑，
 *     而数据其实一直停在最后一天。
 */
export function SettingsView() {
  const [status, setStatus] = useState<ScheduleStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [draft, setDraft] = useState({
    enabled: false, runAtLocal: "20:30", weekdaysOnly: true,
    interpreter: "", dataDir: "", windowStart: "2026-06-22",
  });

  const load = useCallback(async () => {
    try {
      const s = await api.schedule();
      setStatus(s);
      setDraft({
        enabled: s.schedule.enabled,
        runAtLocal: s.schedule.runAtLocal,
        weekdaysOnly: s.schedule.weekdaysOnly,
        interpreter: s.schedule.interpreter ?? "",
        dataDir: s.schedule.dataDir ?? "",
        windowStart: s.schedule.windowStart ?? "2026-06-22",
      });
      setError(null);
    } catch (err) {
      setError(err instanceof AquantApiError ? err.message : String(err));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const save = async () => {
    setBusy(true);
    setNotice(null);
    try {
      const s = await api.saveSchedule(draft);
      setStatus(s);
      setNotice(draft.enabled
        ? "已保存。到点执行由 worker 负责——确认它正在运行，否则不会自动跑。"
        : "已保存：每日任务停用。");
      setError(null);
    } catch (err) {
      // 校验失败带 repair（§16.4），直接显示它，不显示"操作失败"
      setError(err instanceof AquantApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const runNow = async () => {
    setBusy(true);
    setNotice(null);
    try {
      const r = await api.runPipelineNow("界面请求立刻运行");
      setStatus(r);
      setNotice("已登记运行请求 " + r.requestId +
        "。执行者是独立 worker；它不在跑时这条请求会一直等待。");
      setError(null);
    } catch (err) {
      setError(err instanceof AquantApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  if (error && !status) {
    return (
      <Section title="设置" hint="每日任务" dataSource="api">
        <Card><Callout tone="danger" title="无法读取调度状态">{error}</Callout></Card>
      </Section>
    );
  }

  const last = status?.lastRun ?? null;
  const pending = status?.pending ?? null;

  return (
    <>
      <Section title="每日任务" hint="配置在界面，执行在独立 worker"
        dataSource="api">
        <Card>
          <Callout tone="info" title="为什么不能在界面里直接跑">
            这个按钮只**登记一次运行请求**。真正的采集与建快照由
            <span className="mono"> tools/scheduler_worker.py </span>
            执行——与手工运行同一条路径（并发锁、休市判断、留痕、告警都在那条路上）。
            把定时器放进 API 进程的后果是：服务一重启，当天那次运行就没了，而没人会知道。
          </Callout>

          <div className="grid-2" style={{ marginTop: 12 }}>
            <div>
              <h4 style={{ marginBottom: 6 }}>调度状态</h4>
              <dl className="kv">
                <dt>每日任务</dt>
                <dd>
                  <Badge tone={draft.enabled ? "ok" : "neutral"}>
                    {draft.enabled ? "已启用" : "已停用"}
                  </Badge>
                </dd>
                <dt>运行时间</dt>
                <dd className="mono">{draft.runAtLocal}（本机时间）</dd>
                <dt>运行日</dt>
                <dd>{draft.weekdaysOnly ? "周一至周五" : "每天"}</dd>
                <dt>下一次</dt>
                <dd className="mono">{status?.nextFireAt ? formatAsOf(status.nextFireAt) : "—"}</dd>
                <dt>配置更新</dt>
                <dd className="note">
                  {status?.schedule.updatedAt
                    ? formatAsOf(status.schedule.updatedAt) + " · " +
                      (status.schedule.updatedBy ?? "—")
                    : "从未修改（默认停用）"}
                </dd>
              </dl>
            </div>

            <div>
              <h4 style={{ marginBottom: 6 }}>执行者</h4>
              <dl className="kv">
                <dt>解释器</dt>
                <dd className="mono" style={{ wordBreak: "break-all" }}>
                  {draft.interpreter || <span className="note">未设置（启用时必填）</span>}
                </dd>
                <dt>数据目录</dt>
                <dd className="mono" style={{ wordBreak: "break-all" }}>
                  {draft.dataDir || <span className="note">worker 默认目录</span>}
                </dd>
                <dt>窗口起点</dt>
                <dd className="mono">{draft.windowStart}</dd>
                <dt>待处理请求</dt>
                <dd>
                  {pending
                    ? <Badge tone="warn">{pending.status} · {pending.requestId}</Badge>
                    : <span className="note">无</span>}
                </dd>
              </dl>
              <p className="note" style={{ marginTop: 8 }}>
                worker 命令：<span className="mono">python tools/scheduler_worker.py</span>
                （长驻）。它不在运行时，上面这些设置**不会**让任何东西自动跑。
              </p>
            </div>
          </div>
        </Card>
      </Section>

      <Section title="修改设置" dataSource="api">
        <Card>
          <div style={{ display: "grid", gap: 10, maxWidth: 640 }}>
            <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <input type="checkbox" checked={draft.enabled}
                onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />
              <span>启用每日任务</span>
            </label>

            <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <span style={{ width: 96 }} className="note">运行时间</span>
              <input className="input mono" type="time" value={draft.runAtLocal}
                onChange={(e) => setDraft({ ...draft, runAtLocal: e.target.value })} />
              <span className="note">本机时间；收盘后建议 20:30</span>
            </label>

            <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <input type="checkbox" checked={draft.weekdaysOnly}
                onChange={(e) => setDraft({ ...draft, weekdaysOnly: e.target.checked })} />
              <span>只在周一至周五运行（不看交易日历：休市日会按计划跳过）</span>
            </label>

            <label style={{ display: "grid", gap: 4 }}>
              <span className="note">解释器（必须装了 baostock，否则每天都会失败）</span>
              <input className="input mono" value={draft.interpreter}
                placeholder={String.raw`E:\IT\Agent\.venv\Scripts\python.exe`}
                onChange={(e) => setDraft({ ...draft, interpreter: e.target.value })} />
            </label>

            <label style={{ display: "grid", gap: 4 }}>
              <span className="note">数据目录（留空 = worker 的默认目录）</span>
              <input className="input mono" value={draft.dataDir}
                onChange={(e) => setDraft({ ...draft, dataDir: e.target.value })} />
            </label>

            <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <span style={{ width: 96 }} className="note">窗口起点</span>
              <input className="input mono" type="date" value={draft.windowStart}
                onChange={(e) => setDraft({ ...draft, windowStart: e.target.value })} />
              <span className="note">固定不动，改它等于换了比较范围</span>
            </label>

            <div style={{ display: "flex", gap: 8, marginTop: 4 }}>
              <button className="btn btn-primary" onClick={() => void save()} disabled={busy}>
                {busy ? "处理中…" : "保存设置"}
              </button>
              <button className="btn" onClick={() => void runNow()} disabled={busy}>
                立刻运行一次
              </button>
              <button className="btn btn-ghost" onClick={() => void load()} disabled={busy}>
                重新读取
              </button>
            </div>

            {notice && (
              <Callout tone="ok" title="已提交"><span className="note">{notice}</span></Callout>
            )}
            {error && (
              <Callout tone="danger" title="未能保存">
                {/* 服务端返回的是可操作的修复建议，不是"操作失败" */}
                {error}
              </Callout>
            )}
          </div>
        </Card>
      </Section>

      <Section title="最近一次运行" hint="结果来自 worker 落库的摘要"
        dataSource="api">
        {!last ? (
          <Card>
            <Empty title="尚无运行记录">
              还没有任何一次运行被记录。这不等于"跑了但没成功"——
              两者在界面上必须区分开，因此这里显示"没有记录"而不是 0。
            </Empty>
          </Card>
        ) : (
          <Card>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <Badge tone={
                last.outcome === "PUBLISHED" ? "ok"
                  : last.outcome === "SKIPPED" ? "neutral" : "danger"}>
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
            <div className="table-wrap" style={{ marginTop: 10 }}>
              <table className="data">
                <thead><tr><th>步骤</th><th>结果</th><th className="num">耗时</th></tr></thead>
                <tbody>
                  {last.steps.map((s) => (
                    <tr key={s.step}>
                      <td>{s.step}</td>
                      <td>
                        <Badge tone={s.ok ? "ok" : "danger"}>{s.ok ? "成功" : "失败"}</Badge>
                      </td>
                      <td className="num mono">{s.seconds != null ? s.seconds + "s" : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}
      </Section>

      <Section title="这个页面不做什么" dataSource="static" noNumericValue>
        <Card>
          <ul className="list">
            <li className="note">
              不连接券商、不下单。这里调度的只有"采集行情 → 发布快照 → 因子落库"。
            </li>
            <li className="note">
              不自动重试失败的运行。失败会告警并留痕，自动重试会把
              "数据源坏了"变成"每 30 秒重试一次"，把真正的告警淹没。
            </li>
            <li className="note">
              不替使用者决定运行时间。默认停用：装上就自动开始每天抓数据，
              是一个使用者没做过的决定。
            </li>
          </ul>
        </Card>
      </Section>
    </>
  );
}
