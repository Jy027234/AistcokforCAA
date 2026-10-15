import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve, join } from "node:path";

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
export async function launchIndependentChrome(outputDirectory) {
  const executable = ["C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"].find(existsSync);
  if (!executable) throw new Error("独立 Chrome 不可用；不会切换到其他浏览器。");
  const root = resolve(outputDirectory);
  mkdirSync(root, { recursive: true });
  const profile = join(root, `chrome-profile-${Date.now()}`);
  mkdirSync(profile);
  const child = spawn(executable, ["--headless=new", "--remote-debugging-port=0", `--user-data-dir=${profile}`,
    "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-background-networking",
    "--disable-gpu", "--use-gl=swiftshader", "--enable-unsafe-swiftshader", "--window-size=1440,1000", "about:blank"],
    { stdio: "ignore", windowsHide: true });
  try {
  const activePort = join(profile, "DevToolsActivePort");
  let port;
  for (let count = 0; count < 80; count++) {
    if (child.exitCode != null) throw new Error(`独立 Chrome 启动退出：${child.exitCode}`);
    try {
      if (existsSync(activePort)) {
        const candidate = Number(readFileSync(activePort, "utf8").split(/\r?\n/)[0]);
        if (candidate > 0 && candidate < 65536) { port = candidate; break; }
      }
    } catch (error) {
      if (!["EBUSY", "ENOENT"].includes(error.code)) throw error;
    }
    await delay(100);
  }
  if (!port) throw new Error("独立 Chrome 调试端点未就绪。");
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const target = targets.find(item => item.type === "page" && item.url === "about:blank");
  if (!target) throw new Error("独立 Chrome 未提供新建的空白测试页。");
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.addEventListener("open", resolve, { once: true }); ws.addEventListener("error", reject, { once: true }); });
  let sequence = 0;
  const pending = new Map();
  const events = [];
  let touchInput = false;
  ws.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const item = pending.get(message.id); pending.delete(message.id); clearTimeout(item.timer);
      message.error ? item.reject(new Error(JSON.stringify(message.error))) : item.resolve(message.result);
    } else if (message.method) events.push(message);
  });
  ws.addEventListener("close", () => {
    for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error("Independent Chrome connection closed")); }
    pending.clear();
  });
  function send(method, params = {}) {
    const id = ++sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Chrome command timed out: ${method}`)); }, 15000);
      pending.set(id, { resolve, reject, timer }); ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async function evaluate(expression) {
    const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text);
    return result.result?.value;
  }
  async function waitFor(expression, label = expression, timeout = 20000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const value = await evaluate(expression); if (value) return value;
      await delay(120);
    }
    throw new Error(`等待超时：${label}`);
  }
  async function click(selector, text = null) {
    // Observe the current DOM and use a single visible target; input is sent through CDP.
    const target = await evaluate(`(() => {
      const matches = [...document.querySelectorAll(${JSON.stringify(selector)})].filter(el => el.checkVisibility() && ${text == null ? "true" : `el.textContent.trim() === ${JSON.stringify(text)}`});
      if (matches.length !== 1) throw new Error('Expected one visible control, found ' + matches.length);
      const el = matches[0]; if (el.disabled) throw new Error('Control is disabled');
      el.scrollIntoView({block:'center'}); const r = el.getBoundingClientRect();
      return {x:r.x+r.width/2,y:r.y+r.height/2,text:el.textContent.trim()};
    })()`);
    if (touchInput) {
      await send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: target.x, y: target.y }] });
      await send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    } else {
      await send("Input.dispatchMouseEvent", { type: "mousePressed", x: target.x, y: target.y, button: "left", clickCount: 1 });
      await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: target.x, y: target.y, button: "left", clickCount: 1 });
    }
    return target;
  }
  async function fill(selector, value) {
    await click(selector);
    await send("Input.dispatchKeyEvent", { type: "keyDown", key: "a", code: "KeyA", modifiers: 2, windowsVirtualKeyCode: 65 });
    await send("Input.dispatchKeyEvent", { type: "keyUp", key: "a", code: "KeyA", modifiers: 2, windowsVirtualKeyCode: 65 });
    await send("Input.insertText", { text: value });
  }
  async function viewport(width, height, mobile = false) {
    touchInput = mobile;
    await send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile });
    await send("Emulation.setTouchEmulationEnabled", { enabled: mobile, maxTouchPoints: 1 });
  }
  async function screenshot(name, fullPage = false) {
    await evaluate("document.fonts.ready.then(() => true)");
    await delay(150);
    const options = { format: "png", captureBeyondViewport: fullPage };
    if (fullPage) {
      const metrics = await send("Page.getLayoutMetrics");
      options.clip = { x: 0, y: 0, width: metrics.cssContentSize.width, height: Math.min(metrics.cssContentSize.height, 10000), scale: 1 };
    }
    const data = await send("Page.captureScreenshot", options);
    const path = join(root, name + ".png"); writeFileSync(path, Buffer.from(data.data, "base64")); return path;
  }
  await send("Runtime.enable"); await send("Page.enable"); await send("Network.enable");
  return { send, evaluate, waitFor, click, fill, viewport, screenshot, events, profile, pid: child.pid,
    async close() {
      // This endpoint and PID belong only to the fresh, isolated Chrome above.
      try { await send("Browser.close"); } catch { /* process may close before reply */ }
      ws.close();
      for (let count = 0; count < 20 && child.exitCode == null; count++) await delay(100);
      if (child.exitCode == null) child.kill();
      for (const item of pending.values()) clearTimeout(item.timer);
    },
  };
  } catch (error) {
    // Startup failures also release only this runner's freshly spawned process.
    if (child.exitCode == null) child.kill();
    throw error;
  }
}
