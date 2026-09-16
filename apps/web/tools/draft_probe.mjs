/** 点击「请求服务端预览」后，把草稿面板的文本打出来（验证差异说明用）。 */
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const URL_BASE = process.argv[2] || "http://127.0.0.1:8080";
const PORT = 9888;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const CAND = ["C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
              "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"];
const exe = CAND.find((p) => existsSync(p));
const profile = mkdtempSync(join(tmpdir(), "draft-"));
const child = spawn(exe, ["--headless=new", "--remote-debugging-port=" + PORT,
  "--user-data-dir=" + profile, "--no-first-run", "--disable-gpu",
  "--use-gl=swiftshader", "--enable-unsafe-swiftshader",
  "--window-size=1440,1600", "about:blank"], { stdio: "ignore" });

const EXTRACT = [
  "(() => {",
  "  const secs = document.querySelectorAll('[data-source]');",
  "  for (const el of secs) {",
  "    const h = el.querySelector('.section-head h2');",
  "    if (h && h.textContent.includes('草稿与确认')) return el.innerText;",
  "  }",
  "  return '(未找到草稿区块)';",
  "})()",
].join("\n");

(async () => {
  let target = null;
  for (let i = 0; i < 60 && !target; i++) {
    await sleep(250);
    try {
      const l = await (await fetch("http://127.0.0.1:" + PORT + "/json/list")).json();
      target = l.find((t) => t.type === "page");
    } catch { /* 未就绪 */ }
  }
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res) => ws.addEventListener("open", res, { once: true }));
  let id = 0; const pending = new Map();
  ws.addEventListener("message", (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id); pending.delete(m.id);
      m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result);
    }
  });
  const send = (method, params = {}) => {
    const i = ++id; ws.send(JSON.stringify({ id: i, method, params }));
    return new Promise((res, rej) => pending.set(i, { res, rej }));
  };
  await send("Runtime.enable"); await send("Page.enable");
  const evalx = async (e) => (await send("Runtime.evaluate",
    { expression: e, returnByValue: true })).result?.value;

  await send("Page.navigate", { url: URL_BASE + "/#portfolio" });
  await sleep(3500);
  await evalx("(() => { const b = [...document.querySelectorAll('button')]" +
    ".find((x) => x.textContent.includes('请求服务端预览')); if (b) b.click(); })()");
  await sleep(3000);
  console.log(await evalx(EXTRACT));
  ws.close(); child.kill(); await sleep(600);
  try { rmSync(profile, { recursive: true, force: true }); } catch { /* ignore */ }
  process.exit(0);
})();
