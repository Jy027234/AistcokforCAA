/** 数字出处检查：界面上的数字必须说得出自己从哪来。
 *
 * 为什么需要它
 * ------------
 * 组合页曾经整块「我的模拟草稿」都来自随前端分发的只读夹具，
 * 而徽章写着「API 在线」——页面根本没请求过预览接口。
 * 这不是肉眼能发现的问题：夹具的数字看起来完全正常。
 *
 * 检查什么
 * --------
 * 每个区块用 data-source 声明来源（见 components/ui.tsx）：
 *
 *   * api     —— 区块内的数字必须能在**某次服务端响应**里找到；
 *   * fixture —— 必须同时出现演示标注（否则使用者会当成真实数据）；
 *   * static  —— 常量文案，不查数字。
 *
 * 刻意不做的
 * ----------
 * 不去证明"每个数字都来自服务端"。那严格来说不可判定：格式化、
 * 汇总、日期都会打断等值匹配，强行做会得到一堆假警报，
 * 而假警报多了真警报就没人看了。这里只验**声明与事实是否一致**。
 *
 * 用法：
 *   node tools/check_number_provenance.mjs --url http://127.0.0.1:8080
 * 退出码：0 = 全部一致；1 = 有矛盾；2 = 环境起不来。
 */

import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const args = process.argv.slice(2);
const flag = (name, dflt) => {
  const i = args.indexOf("--" + name);
  return i >= 0 && args[i + 1] ? args[i + 1] : dflt;
};
const URL_BASE = flag("url", "http://127.0.0.1:8080");
const PORT = Number(flag("port", "9666"));
const TABS = (flag("tabs", "today,research,portfolio,workspace,experiments")).split(",");
/** 在组合页点一次「请求服务端预览」后再检查（用于验证"拿预览后表格必须换"）。 */
const CLICK_PREVIEW = args.includes("--click-preview");
const OUT = flag("out", "");
/** 指向另一个后端（隔离验证用）。留空表示同源。 */
const API_BASE = flag("api", "");

const CHROME = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
];

/** 区块内出现这些词即视为"已标注为演示数据"。 */
const DEMO_MARKERS = ["演示", "虚构", "只读夹具", "示例数据", "非服务端计算"];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

class Cdp {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map(); this.events = [];
    ws.addEventListener("message", (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && this.pending.has(m.id)) {
        const p = this.pending.get(m.id);
        this.pending.delete(m.id);
        m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result);
      } else if (m.method) this.events.push(m);
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((res, rej) => this.pending.set(id, { res, rej }));
  }
  async evaluate(expression) {
    const r = await this.send("Runtime.evaluate", {
      expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 200));
    return r.result?.value;
  }
}

/** 页面侧探针：记录 fetch 的 URL/方法/状态/正文，并抓各区块的文本。 */
const PROBE = [
  "(() => {",
  "  if (window.__AQ_PROBE__) return;",
  "  const log = [];",
  "  const orig = window.fetch;",
  "  window.fetch = async function (...a) {",
  "    const url = typeof a[0] === 'string' ? a[0] : (a[0] && a[0].url) || '';",
  "    const method = (a[1] && a[1].method) || 'GET';",
  "    const res = await orig.apply(this, a);",
  "    let body = null;",
  "    try { body = await res.clone().text(); } catch (e) { body = null; }",
  "    log.push({ url: url, method: method, status: res.status, body: body });",
  "    return res;",
  "  };",
  "  window.__AQ_PROBE__ = log;",
  "})()",
].join("\n");

const COLLECT = [
  "(() => {",
  "  const out = [];",
  "  document.querySelectorAll('[data-source]').forEach((el) => {",
  "    // 只取**最内层**的声明区块：父区块的 innerText 会把子区块的数字",
  "    // 也算成自己的，于是工作区父区块明明没有自己的数字，却因为",
  "    // 子区块的数字而误判为命中——那是假通过。",
  "    if (el.querySelector('[data-source]')) return;",
  "    const head = el.querySelector('.section-head h2, .card-head h3');",
  "    out.push({",
  "      source: el.getAttribute('data-source'),",
  "      noNumeric: el.getAttribute('data-no-numeric-value') === '1',",
  "      title: head ? head.textContent.trim() : '(无标题)',",
  "      text: el.innerText,",
  "    });",
  "  });",
  "  // 页面级演示标注：在整页范围内找，因为水印是页面顶部的横幅。",
  "  const pageText = document.body.innerText || '';",
  "  const demo = /演示|虚构|只读夹具|示例数据|非服务端计算/.test(pageText);",
  "  window.__AQ_PROBE__ = window.__AQ_PROBE__ || [];",
  "  window.__AQ_PROBE__.pageDemo = demo;",
  "  return { sections: out, requests: window.__AQ_PROBE__ || [],",
  "           pageDemo: demo };",
  "})()",
].join("\n");

/** 一个显示出来的小数金额，能否在服务端响应里找到它对得上的原值。
 *
 * 允许**一层**换算：界面按元显示，而响应里是整数分
 * （900,128.04 元 <-> 90012804 分）。这是账本的统一口径，
 * 不是任意缩放——因此只试这一种，不做通用匹配。
 *
 * 刻意不做的事：不允许"差了 100 倍就算命中"之外的任何近似。
 * 一旦放宽到"大约相等"，这条检查就再也发现不了真正的问题。
 */
function hitsResponse(display, apiText) {
  const plain = display.replace(/,/g, "");
  if (apiText.includes(plain)) return true;
  // 分 -> 元：把显示值乘 100 取整，看响应里有没有这个整数分
  const cents = Math.round(Number(plain) * 100);
  if (Number.isFinite(cents) && apiText.includes(String(cents))) return true;
  return false;
}

function check(name, ok, detail = "") {
  console.log((ok ? "  PASS  " : "  FAIL  ") + name + (detail ? "  -- " + detail : ""));
  return { name, ok: Boolean(ok), detail };
}

async function main() {
  const exe = CHROME.find((p) => existsSync(p));
  if (!exe) throw new Error("找不到 Chrome/Edge");
  const profile = mkdtempSync(join(tmpdir(), "aquant-prov-"));
  const child = spawn(exe, [
    "--headless=new", "--remote-debugging-port=" + PORT,
    "--user-data-dir=" + profile, "--no-first-run", "--no-default-browser-check",
    "--disable-extensions", "--disable-background-networking",
    "--disable-gpu", "--use-gl=swiftshader", "--enable-unsafe-swiftshader",
    "--window-size=1440,1600", "about:blank",
  ], { stdio: "ignore" });

  const results = [];
  let ws;
  try {
    let target = null;
    for (let i = 0; i < 60 && !target; i++) {
      await sleep(250);
      try {
        const list = await (await fetch("http://127.0.0.1:" + PORT + "/json/list")).json();
        target = list.find((t) => t.type === "page");
      } catch { /* 未就绪 */ }
    }
    if (!target) throw new Error("DevTools 未就绪");

    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
      ws.addEventListener("open", res, { once: true });
      ws.addEventListener("error", rej, { once: true });
    });
    const cdp = new Cdp(ws);
    await cdp.send("Runtime.enable");
    await cdp.send("Page.enable");
    await cdp.send("Network.enable");
    await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: PROBE });
    if (API_BASE) {
      // 必须在页面脚本之前注入，否则模块已经读过空值了
      await cdp.send("Page.addScriptToEvaluateOnNewDocument", {
        source: "window.__AQUANT_API_BASE__ = " + JSON.stringify(API_BASE) + ";",
      });
      console.log("已注入 API 基地址: " + API_BASE);
    }

    for (const tab of TABS) {
      console.log("\n[" + tab + "]");
      await cdp.send("Page.navigate", { url: URL_BASE + "/#" + tab });
      await sleep(3000);
      if (CLICK_PREVIEW && tab === "portfolio") {
        const clicked = await cdp.evaluate(
          "(() => { const b = [...document.querySelectorAll('button')]" +
          ".find((x) => x.textContent.includes('请求服务端预览'));" +
          "if (!b) return false; b.click(); return true; })()");
        console.log("  （已点击「请求服务端预览」：" + clicked + "）");
        await sleep(2500);
      }
      const dump = await cdp.evaluate(COLLECT);

      const apiText = dump.requests
        .filter((r) => r.url.includes("/api/") && r.body)
        .map((r) => r.body).join("\n");
      const apiCalls = dump.requests.filter((r) => r.url.includes("/api/"));

      const apiSections = dump.sections.filter((s) => s.source === "api");
      const fixtureSections = dump.sections.filter((s) => s.source === "fixture");
      const unknown = dump.sections.filter(
        (s) => !["api", "fixture", "static"].includes(s.source));

      results.push(check(tab + "：区块都声明了数据来源", unknown.length === 0,
        unknown.map((s) => s.title + "=" + s.source).join(", ")));

      // 声明"无业务数字"的区块：不核对数字，但**声明必须是真的**。
      // 金额形状（"1,234.56 元"）一旦出现，那句声明就成了假话——
      // 这比漏检更糟，因为它是有人有意写下的。
      for (const s of dump.sections.filter((x) => x.noNumeric)) {
        const moneyish = s.text.match(/\d[\d,]*\.\d\d\s*元?/g) || [];
        results.push(check(
          tab + " · " + s.title + "：声明无业务数字且属实",
          moneyish.length === 0,
          moneyish.length
            ? "出现金额形状的数字 " + JSON.stringify(moneyish.slice(0, 3)) : ""));
      }

      // api 区块：**带小数的数字必须全部**能在服务端响应里找到。
      //
      // 为什么判据是"全部"而不是"至少一个"：第一版用"至少一个命中"，
      // 结果带 bug 运行时 35 个数字里 7 个偶然命中（"100"、"0" 这类碎片），
      // 检查照样通过——**假通过比漏检更糟**。
      //
      // 为什么只看带小数的数字：整数常是证券代码、日期、手数，
      // 它们出现在响应里是必然的，拿它们判等毫无信息量。
      // 金额与比率都带两位小数，那才是业务数字。
      for (const s of apiSections.filter((x) => !x.noNumeric)) {
        const decimals = (s.text.match(/\d[\d,]*\.\d+/g) || [])
          .map((n) => n.replace(/,/g, ""));
        if (decimals.length === 0) {
          results.push(check(tab + " · " + s.title + "：区块内无小数金额", true));
          continue;
        }
        const missing = decimals.filter((n) => !hitsResponse(n, apiText));
        results.push(check(
          tab + " · " + s.title + "：金额全部来自服务端响应",
          missing.length === 0,
          apiCalls.length === 0
            ? "该区块声明为 api，但页面**没有发出任何 /api 请求**"
            : "响应里找不到 " + JSON.stringify(missing.slice(0, 4))
              + "（共 " + decimals.length + " 个小数金额）"));
      }

      // fixture 区块：必须带演示标注。
      //
      // 接受两种位置：区块内自带标注，或页面级横幅（水印）可见。
      // **不接受"数据来自夹具而页面上没有任何标注"**——那正是这条检查
      // 存在的理由。第一版只看区块内，于是"组合"与"今天"被判失败，
      // 而那两条其实有页面级水印；真正的缺陷是当时我还没给它们加标注。
      for (const s of fixtureSections) {
        const inBlock = DEMO_MARKERS.some((m) => s.text.includes(m));
        const marked = inBlock || dump.pageDemo;
        results.push(check(
          tab + " · " + s.title + "：演示数据有标注",
          marked,
          inBlock ? "" : (dump.pageDemo ? "（页面级水印）" : "既无区块标注也无页面水印")));
      }
    }
  } finally {
    try { ws?.close(); } catch { /* ignore */ }
    child.kill();
    await sleep(800);
    try { rmSync(profile, { recursive: true, force: true }); } catch { /* ignore */ }
  }

  const failed = results.filter((r) => !r.ok);
  console.log("\n" + "=".repeat(60));
  console.log("数字出处检查 " + (results.length - failed.length) + "/" + results.length + " 通过");
  for (const f of failed) console.log("  - " + f.name + "  " + f.detail);
  if (OUT) {
    writeFileSync(OUT, JSON.stringify({ checks: results }, null, 2), "utf-8");
    console.log("报告：" + OUT);
  }
  return failed.length ? 1 : 0;
}

main().then((c) => process.exit(c)).catch((e) => { console.error(e.message); process.exit(2); });
