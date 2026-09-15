/** 给工作台各页签截图。
 *
 * 为什么需要它：文本断言能证明"某个字符串出现了"，不能证明
 * **它看起来是对的**——布局塌了、颜色对比不足、字叠在一起，
 * innerText 全都正常。截图是唯一能回答"人能看懂吗"的手段。
 *
 * 用法：
 *   node tools/screenshot.mjs --url http://127.0.0.1:4174 --out ../../deploy/screens
 */

import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const args = process.argv.slice(2);
const flag = (name, dflt) => {
  const i = args.indexOf("--" + name);
  return i >= 0 && args[i + 1] ? args[i + 1] : dflt;
};
const URL_BASE = flag("url", "http://127.0.0.1:4173");
const OUT = resolve(flag("out", "../../deploy/screens"));
const API_BASE = flag("api", "");
const PORT = Number(flag("port", "9333"));
const WIDTH = Number(flag("width", "1440"));
const HEIGHT = Number(flag("height", "1600"));

const CHROME_CANDIDATES = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function pickBrowser() {
  for (const p of CHROME_CANDIDATES) if (existsSync(p)) return p;
  throw new Error("找不到 Chrome/Edge");
}

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.sessionId = null;
    ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
      }
    });
  }

  send(method, params = {}, sessionId = undefined) {
    const id = ++this.id;
    const payload = { id, method, params };
    if (sessionId ?? this.sessionId) payload.sessionId = sessionId ?? this.sessionId;
    this.ws.send(JSON.stringify(payload));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }

  async evaluate(expression) {
    const r = await this.send("Runtime.evaluate", {
      expression, returnByValue: true, awaitPromise: true,
    });
    return r.result?.value;
  }

  async waitFor(expression, { label, timeout = 20000 } = {}) {
    const deadline = Date.now() + timeout;
    let last;
    while (Date.now() < deadline) {
      last = await this.evaluate(expression);
      if (last) return last;
      await sleep(200);
    }
    throw new Error("等待超时: " + label + "（最后 = " + JSON.stringify(last) + "）");
  }
}

async function main() {
  const exe = pickBrowser();
  const profile = mkdtempSync(join(tmpdir(), "aquant-shot-"));
  mkdirSync(OUT, { recursive: true });
  const child = spawn(exe, [
    "--headless=new",
    "--remote-debugging-port=" + PORT,
    "--user-data-dir=" + profile,
    "--no-first-run", "--no-default-browser-check", "--disable-extensions",
    "--disable-background-networking",
    // 无 GPU 的机器上必须走软件渲染，否则整块区域不绘制（截图会是空白）
    "--disable-gpu", "--use-gl=swiftshader", "--enable-unsafe-swiftshader",
    "--window-size=" + WIDTH + "," + HEIGHT,
    "about:blank",
  ], { stdio: "ignore" });

  let ws;
  try {
    let target = null;
    for (let i = 0; i < 60 && !target; i++) {
      await sleep(250);
      try {
        const list = await (await fetch("http://127.0.0.1:" + PORT + "/json/list")).json();
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
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: WIDTH, height: HEIGHT, deviceScaleFactor: 1, mobile: false,
    });
    if (API_BASE) {
      await cdp.send("Page.addScriptToEvaluateOnNewDocument", {
        source: "window.__AQUANT_API_BASE__ = " + JSON.stringify(API_BASE) + ";",
      });
    }

    const tabs = ["today", "research", "portfolio", "workspace", "experiments"];
    for (const tab of tabs) {
      await cdp.send("Page.navigate", { url: URL_BASE + "/#" + tab });
      await cdp.waitFor("document.readyState === 'complete'", { label: "加载 " + tab });
      await sleep(1500);            // 等数据往返与图表落位
      const shot = await cdp.send("Page.captureScreenshot", {
        format: "png", captureBeyondViewport: true,
      });
      const file = join(OUT, tab + ".png");
      writeFileSync(file, Buffer.from(shot.data, "base64"));
      const text = (await cdp.evaluate("document.body.innerText")) || "";
      console.log("  " + tab.padEnd(12) + " -> " + file
        + "  (" + text.length + " 字符文本)");
    }
    console.log("\n截图目录：" + OUT);
  } finally {
    try { ws?.close(); } catch { /* ignore */ }
    child.kill();
    await sleep(1000);
    try { rmSync(profile, { recursive: true, force: true }); } catch { /* ignore */ }
  }
}

main().catch((err) => { console.error(err.message); process.exit(1); });
