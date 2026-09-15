/** 用真实浏览器走一遍工作台写路径（Chrome DevTools Protocol）。
 *
 * 为什么不用截图：截图只能证明"渲染出来了"，不能证明"点下去真的发生了"。
 * 这个脚本驱动真实 Chrome：点击、等待网络往返、读回页面文本，
 * 断言的是**交互结果**，不是像素。
 *
 * 只依赖 Node 内置能力（fetch + WebSocket）与已安装的 Chrome，
 * 不引入 playwright/puppeteer——被测对象是本地页面，不值得再添一套依赖。
 *
 * 用法：
 *   node tools/browser_check.mjs [--url http://127.0.0.1:4173] [--headful]
 *
 * 退出码：0 = 全部断言通过；1 = 有断言失败；2 = 浏览器或页面起不来。
 */

import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const args = process.argv.slice(2);
const flag = (name, dflt) => {
  const i = args.indexOf("--" + name);
  return i >= 0 && args[i + 1] ? args[i + 1] : dflt;
};
const URL_BASE = flag("url", "http://127.0.0.1:4173");
const PORTFOLIO = flag("portfolio", "pf-syn-m");
/** 指向另一个后端（用于隔离验证）。留空则表示使用页面同源代理。 */
const API_BASE = flag("api", "");
const HEADFUL = args.includes("--headful");
const PORT = Number(flag("port", "9222"));

const CHROME_CANDIDATES = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function pickBrowser() {
  const { existsSync } = await import("node:fs");
  for (const p of CHROME_CANDIDATES) if (existsSync(p)) return p;
  throw new Error("找不到 Chrome/Edge");
}

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.logs = [];
    ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
      } else if (msg.method === "Runtime.consoleAPICalled") {
        this.logs.push(msg.params.args.map((a) => a.value ?? a.description ?? "").join(" "));
      } else if (msg.method === "Runtime.exceptionThrown") {
        this.logs.push("EXCEPTION: " +
          (msg.params.exceptionDetails?.exception?.description ??
           msg.params.exceptionDetails?.text ?? "unknown"));
      }
    });
  }

  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error("CDP 超时: " + method));
        }
      }, 30000);
    });
  }

  /** 在页面里求值，返回结构化结果。 */
  async evaluate(expression) {
    const res = await this.send("Runtime.evaluate", {
      expression, returnByValue: true, awaitPromise: true,
    });
    if (res.exceptionDetails) {
      throw new Error("页面内异常: " + JSON.stringify(res.exceptionDetails.text));
    }
    return res.result.value;
  }

  /** 轮询直到表达式为真或超时。 */
  async waitFor(expression, { timeout = 15000, label = expression } = {}) {
    const deadline = Date.now() + timeout;
    let last;
    while (Date.now() < deadline) {
      last = await this.evaluate(expression);
      if (last) return last;
      await sleep(200);
    }
    throw new Error("等待超时: " + label + "（最后一次 = " + JSON.stringify(last) + "）");
  }
}

const checks = [];
function check(name, ok, detail = "") {
  checks.push({ name, ok: Boolean(ok), detail });
  console.log((ok ? "  PASS  " : "  FAIL  ") + name + (detail ? "  -- " + detail : ""));
}

async function main() {
  const exe = await pickBrowser();
  const profile = mkdtempSync(join(tmpdir(), "aquant-cdp-"));
  const child = spawn(exe, [
    HEADFUL ? "--headless=new" : "--headless=new",
    "--remote-debugging-port=" + PORT,
    "--user-data-dir=" + profile,
    "--no-first-run", "--no-default-browser-check",
    "--disable-extensions", "--disable-background-networking",
    // 无 GPU 的机器上必须走软件渲染，否则页面可能整块不绘制
    "--disable-gpu", "--use-gl=swiftshader", "--enable-unsafe-swiftshader",
    "--window-size=1440,1000",
    "about:blank",
  ], { stdio: "ignore" });

  let ws;
  try {
    // 等 DevTools 端点起来
    let target = null;
    for (let i = 0; i < 60 && !target; i++) {
      await sleep(250);
      try {
        const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
        target = list.find((t) => t.type === "page");
      } catch { /* 还没起来 */ }
    }
    if (!target) throw new Error("DevTools 端点未就绪");

    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
      ws.addEventListener("open", res, { once: true });
      ws.addEventListener("error", rej, { once: true });
    });
    const cdp = new Cdp(ws);
    await cdp.send("Runtime.enable");
    await cdp.send("Page.enable");
    if (API_BASE) {
      // 必须在页面脚本之前注入，否则模块已经读过空值了
      await cdp.send("Page.addScriptToEvaluateOnNewDocument", {
        source: "window.__AQUANT_API_BASE__ = " + JSON.stringify(API_BASE) + ";",
      });
      console.log("已注入 API 基地址: " + API_BASE);
    }

    console.log("\n[1] 载入工作台 " + URL_BASE);
    await cdp.send("Page.navigate", { url: URL_BASE + "/#portfolio" });
    await cdp.waitFor("document.readyState === 'complete'", { label: "页面加载" });
    // 等夹具载入完成：出现组合页的标题
    await cdp.waitFor(
      "document.body.innerText.includes('草稿与确认')",
      { label: "组合页渲染", timeout: 20000 },
    );
    const title = await cdp.evaluate("document.title");
    check("页面已载入", true, title);

    const apiOnline = await cdp.waitFor(
      "document.body.innerText.includes('API 在线')",
      { label: "API 在线标记", timeout: 20000 },
    );
    check("界面识别出 API 在线", apiOnline);

    console.log("\n[2] 空态：还没冻结时不得显示账本数字");
    const beforeFreeze = await cdp.evaluate(
      "document.body.innerText.includes('先冻结计划')",
    );
    check("未冻结时提示先冻结", beforeFreeze);
    const noExecute = await cdp.evaluate(
      "(() => { const b = [...document.querySelectorAll('button')]" +
      ".find(x => x.textContent.trim() === '执行本交易日');" +
      "return !b; })()",
    );
    check("未冻结时没有执行按钮", noExecute);

    console.log("\n[3] 点“请求服务端预览”");
    await cdp.evaluate(
      "(() => { const b = [...document.querySelectorAll('button')]" +
      ".find(x => x.textContent.includes('请求服务端预览')); b.click(); return true; })()",
    );
    await cdp.waitFor(
      "document.body.innerText.includes('服务端预览已生成')",
      { label: "服务端预览返回", timeout: 25000 },
    );
    const previewText = await cdp.evaluate("document.body.innerText");
    check("服务端预览已生成", previewText.includes("服务端预览已生成"));
    check("预览给出了参考价日（执行日之前）", /参考价日/.test(previewText));

    console.log("\n[4] 点确认冻结（一次性令牌 -> 冻结）");
    const clickLabel = await cdp.evaluate(
      "(() => { const b = [...document.querySelectorAll('button')]" +
      ".find(x => /确认|冻结/.test(x.textContent) && !x.disabled);" +
      "return b ? b.textContent.trim() : null; })()",
    );
    check("找到可用的确认按钮", Boolean(clickLabel), String(clickLabel));
    await cdp.evaluate(
      "(() => { const b = [...document.querySelectorAll('button')]" +
      ".find(x => /确认|冻结/.test(x.textContent) && !x.disabled); b.click(); return true; })()",
    );
    // 不能用 includes('已冻结')：'尚未冻结' 也包含它，会立刻通过。
    await cdp.waitFor(
      "document.body.innerText.includes('服务端复核通过')",
      { label: "冻结完成", timeout: 25000 },
    );
    const frozenText = await cdp.evaluate("document.body.innerText");
    check("冻结成功并显示计划 ID", /已冻结/.test(frozenText) && /计划 ID/.test(frozenText));

    console.log("\n[5] 冻结后出现执行按钮，点它");
    await cdp.waitFor(
      "(() => [...document.querySelectorAll('button')]" +
      ".some(b => b.textContent.trim() === '执行本交易日' && !b.disabled))()",
      { label: "执行按钮可用", timeout: 15000 },
    );
    check("冻结后执行按钮出现且可用", true);
    await cdp.evaluate(
      "(() => { const b = [...document.querySelectorAll('button')]" +
      ".find(x => x.textContent.trim() === '执行本交易日'); b.click(); return true; })()",
    );
    await cdp.waitFor(
      "document.body.innerText.includes('成交与未成交')",
      { label: "执行结果", timeout: 25000 },
    );
    const execText = await cdp.evaluate("document.body.innerText");
    check("执行返回并可渲染", execText.includes("成交与未成交"));

    console.log("\n[6] 点“计算日终净值”");
    await cdp.evaluate(
      "(() => { const b = [...document.querySelectorAll('button')]" +
      ".find(x => x.textContent.includes('计算日终净值')); b.click(); return true; })()",
    );
    await cdp.waitFor(
      "document.body.innerText.includes('日终净值')",
      { label: "估值结果", timeout: 25000 },
    );
    const valText = await cdp.evaluate("document.body.innerText");
    check("估值返回并可渲染", /净值合计/.test(valText));
    check("净值带金额单位（元）", /净值合计[\s\S]{0,40}元/.test(valText));
    check("发布状态有文字说明", /已发布|未发布/.test(valText));

    console.log("\n[7] 点“逐项对账”");
    await cdp.evaluate(
      "(() => { const b = [...document.querySelectorAll('button')]" +
      ".find(x => x.textContent.includes('逐项对账')); b.click(); return true; })()",
    );
    // 等**对账自己的**结果标记，而不是等"不变量"这三个字：
    // 估值区里有一句"不变量失败时净值不得发布"，会让等待在对账仍在
    // 进行时就通过，然后断言看到的是一份还没更新的界面。
    await cdp.waitFor(
      "document.body.innerText.includes('账本现金')",
      { label: "对账结果", timeout: 25000 },
    );
    // 注意取的是**最后一个**"对账"：按钮文字里也有这两个字，
    // 用 indexOf 会停在按钮上，把真正的结果段整段切掉。
    const recText = await cdp.evaluate(
      "(() => { const t = document.body.innerText; return t.slice(t.lastIndexOf('对账')); })()",
    );
    check("对账返回并可渲染", /对账/.test(recText) && /不变量/.test(recText));
    const probe = await cdp.evaluate(
      "(() => ({ len: document.body.innerText.length," +
      " hasLabel: document.body.innerText.includes('未结应收')," +
      " hasRecLabel: document.body.innerText.includes('估值应收与账本一致')," +
      " sliceLen: document.body.innerText.slice(document.body.innerText.lastIndexOf('对账')).length," +
      " bundle: [...document.querySelectorAll('script')].map(s=>s.src).join(',') }))()",
    );
    check("界面确实渲染了应收字段（不只是接口有）",
          probe.hasLabel && probe.hasRecLabel,
          "未结应收=" + probe.hasLabel + " 估值应收与账本一致=" + probe.hasRecLabel);
    check("对账列出了应收（不只是现金和持仓）",
          /未结应收/.test(recText) && /估值应收与账本一致/.test(recText));

    // 界面文案只是判据之一。真正要确认的是**服务端回应里有没有这些字段**：
    // 少一个字段时界面会静默显示"—"，看起来一样正常。
    const recJson = await cdp.evaluate(
      "fetch('/api/v1/portfolios/" + PORTFOLIO + "/reconcile', { headers: " +
      "{ 'X-Aquant-Subject': 'user:demo' } }).then(r => r.json())",
    );
    check("对账响应含 receivables_cents",
          typeof recJson.receivables_cents === "number",
          JSON.stringify(recJson.receivables_cents));
    check("对账响应含 valuation_receivables_matches_ledger",
          recJson.valuation_receivables_matches_ledger === true,
          JSON.stringify(recJson.valuation_receivables_matches_ledger));

    console.log("\n[8] 页面无脚本异常");
    const bad = cdp.logs.filter((l) => l.startsWith("EXCEPTION") || /error/i.test(l));
    check("控制台无异常", bad.length === 0, bad.slice(0, 3).join(" | "));

    console.log("\n[9] 工作区页签：新接的后端能力必须真的可见");
    await cdp.evaluate("location.hash = 'workspace'");
    await sleep(900);
    const wsText = await cdp.evaluate("document.body.innerText");
    check("工作区页渲染", wsText.includes("自选") && wsText.includes("因子排名"),
          wsText.slice(0, 60).replace(/\n/g, " "));
    check("自选说明了不产生订单", wsText.includes("不产生订单"));
    check("因子排名标注为排名而非概率",
          wsText.includes("横截面排名") && wsText.includes("不是概率"));
    check("决策日志与实验登记都在", wsText.includes("决策日志") &&
          wsText.includes("实验登记"));
    // 空态必须说清是"没有"而不是"失败"
    check("空态给出可读说明", wsText.includes("自选为空") ||
          wsText.includes("还没有"));

    console.log("\n[9b] 工作区真交互：加自选必须落库");
    const typed = await cdp.evaluate(
      "(() => { const i = document.querySelector('input.input');" +
      "if (!i) return false;" +
      "const setter = Object.getOwnPropertyDescriptor(" +
      "window.HTMLInputElement.prototype, 'value').set;" +
      "setter.call(i, 'SYN.A.600519');" +
      "i.dispatchEvent(new Event('input', { bubbles: true }));" +
      "return true; })()",
    );
    check("找到自选输入框并可填入", typed);
    await cdp.evaluate(
      "(() => { const b = [...document.querySelectorAll('button')]" +
      ".find(x => x.textContent.trim() === '加入自选' && !x.disabled);" +
      "if (!b) return false; b.click(); return true; })()",
    );
    // 等**输入框被清空**：组件在加入成功后清空输入框，
    // 所以这是"服务端接受了"的判据。不能等 innerText 出现该代码——
    // 输入框里本来就有这段文本，等待会立刻通过（假通过）。
    await cdp.waitFor(
      "(() => { const i = document.querySelector('input.input');" +
      "return !i || i.value === ''; })()",
      { label: "输入框已清空（加入成功）", timeout: 15000 },
    );
    check("加入自选后输入框清空", true);
    const listed = await cdp.evaluate(
      "(() => { const rows = [...document.querySelectorAll('table.data tbody tr')];" +
      "return rows.some(r => r.innerText.includes('SYN.A.600519')); })()",
    );
    check("自选表格中出现该证券", listed);

    // 界面显示不算数——必须确认服务端真的存了
    // 必须用页面注入的 API 基地址：前端与 API 不同端口时，
    // 相对路径会打到前端服务器上（返回 404），
    // 而"自选没落库"这个结论就完全错了。
    const stored = await cdp.evaluate(
      "fetch((window.__AQUANT_API_BASE__ || '') + '/api/v1/watchlist'," +
      " { headers: { 'X-Aquant-Subject': 'user:demo' } })" +
      ".then(async r => ({ status: r.status, body: await r.text() }))",
    );
    console.log("    自选端点回答:", JSON.stringify(stored).slice(0, 300));
    let count = null;
    try { count = JSON.parse(stored.body).count; } catch { /* 非 JSON */ }
    check("自选已落库（服务端计数 > 0）", typeof count === "number" && count > 0,
          "count=" + count + " status=" + stored.status);

    console.log("\n[10] 其余页签可切换");
    for (const [hash, marker] of [["today", "今日"], ["research", "研究"],
                                  ["experiments", "实验"]]) {
      await cdp.evaluate(`location.hash = '${hash}'`);
      await sleep(600);
      const text = await cdp.evaluate("document.body.innerText");
      check("页签可渲染: " + hash, text.length > 80 && text.includes(marker),
            text.slice(0, 40).replace(/\n/g, " "));
    }
  } finally {
    try { ws?.close(); } catch { /* ignore */ }
    child.kill();
    await sleep(1200);
    // Chrome 关闭后仍会短暂占着 profile 里的文件（Windows 上表现为 EBUSY）。
    // 临时目录清不掉只是卫生问题，不该把已经跑完的断言变成"无法完成"。
    try { rmSync(profile, { recursive: true, force: true }); }
    catch { /* 让系统自己回收临时目录 */ }
  }

  const failed = checks.filter((c) => !c.ok);
  console.log("\n" + "=".repeat(60));
  console.log(`浏览器交互检查 ${checks.length - failed.length}/${checks.length} 通过`);
  for (const f of failed) console.log("  - " + f.name + "  " + f.detail);
  return failed.length ? 1 : 0;
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    console.error("\n浏览器检查无法完成: " + (err?.stack ?? err));
    process.exit(2);
  });
