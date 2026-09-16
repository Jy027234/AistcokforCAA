/** 读取页面上真实渲染出来的文本（排查显示问题用）。
 *
 * 与 browser_check.mjs 的区别：它只读不断言、不点击，
 * 输出结构化结果（页签 / 可见文本 / 网络请求），
 * 用来回答"页面上的这个数字是从哪来的"。
 *
 * 用法：node tools/page_dump.mjs --url http://127.0.0.1:8080 --tab portfolio
 */

import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const args = process.argv.slice(2);
const flag = (name, dflt) => {
  const i = args.indexOf("--" + name);
  return i >= 0 && args[i + 1] ? args[i + 1] : dflt;
};
const URL_BASE = flag("url", "http://127.0.0.1:8080");
const TAB = flag("tab", "portfolio");
const API_BASE = flag("api", "");
const PORT = Number(flag("port", "9444"));

const CHROME = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

class Cdp {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map(); this.events = [];
    ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
      } else if (msg.method) {
        this.events.push(msg);
      }
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }
  async evaluate(expression) {
    const r = await this.send("Runtime.evaluate", {
      expression, returnByValue: true, awaitPromise: true });
    return r.result?.value;
  }
}

async function main() {
  const exe = CHROME.find((p) => existsSync(p));
  if (!exe) throw new Error("找不到 Chrome/Edge");
  const profile = mkdtempSync(join(tmpdir(), "aquant-page-"));
  const child = spawn(exe, [
    "--headless=new", "--remote-debugging-port=" + PORT,
    "--user-data-dir=" + profile, "--no-first-run", "--no-default-browser-check",
    "--disable-extensions", "--disable-background-networking",
    "--disable-gpu", "--use-gl=swiftshader", "--enable-unsafe-swiftshader",
    "--window-size=1440,1600", "about:blank",
  ], { stdio: "ignore" });

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
    if (API_BASE) {
      await cdp.send("Page.addScriptToEvaluateOnNewDocument", {
        source: "window.__AQUANT_API_BASE__ = " + JSON.stringify(API_BASE) + ";" });
    }
    await cdp.send("Page.navigate", { url: URL_BASE + "/#" + TAB });
    await sleep(3500);

    const requests = cdp.events
      .filter((e) => e.method === "Network.requestWillBeSent")
      .map((e) => e.params.request.url)
      .filter((u) => !u.startsWith("data:"));

    console.log("=== 页面请求过的 URL ===");
    for (const u of [...new Set(requests)]) console.log("  " + u);
    console.log("\n=== 渲染出来的文本 ===");
    console.log(await cdp.evaluate("document.body.innerText"));
  } finally {
    try { ws?.close(); } catch { /* ignore */ }
    child.kill();
    await sleep(800);
    try { rmSync(profile, { recursive: true, force: true }); } catch { /* ignore */ }
  }
}

main().catch((e) => { console.error(e.message); process.exit(1); });
