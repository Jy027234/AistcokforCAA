import { writeFileSync } from "node:fs";
import { resolve, join } from "node:path";
import { launchIndependentChrome } from "./independent_chrome.mjs";

const argumentsList = process.argv.slice(2);
const flag = (name, fallback) => argumentsList.includes(`--${name}`) ? argumentsList[argumentsList.indexOf(`--${name}`) + 1] : fallback;
const url = flag("url", "http://127.0.0.1:8000");
const output = resolve(flag("out", "../../tmp/frontend-browser-acceptance-20261013"));
const report = { url, startedAt: new Date().toISOString(), mode: "live-read-only", checks: [], screenshots: [], layouts: [] };
const chrome = await launchIndependentChrome(output);
const labels = { today: "今日", research: "研究", portfolio: "模拟组合", experiments: "复盘" };
const devices = flag("devices", "1440x1000,390x844,360x800").split(",").map(size => {
  const [width, height] = size.split("x").map(Number);
  return { name: `${width}px`, width, height, mobile: width <= 720 };
});
try {
  for (const device of devices) {
    await chrome.viewport(device.width, device.height, device.mobile);
    for (const page of ["today", "research", "portfolio", "experiments"]) {
      await chrome.send("Page.navigate", { url: `${url}/#${page}` });
      await chrome.waitFor(`document.readyState === 'complete' && document.querySelector('main section[aria-label="${labels[page]}"]')?.checkVisibility()`, `${device.name} ${page} ready`, 45000);
      await chrome.waitFor("!document.body.innerText.includes('正在读取研究候选名单') && !document.body.innerText.includes('正在核验当前资格') && !document.body.innerText.includes('正在读取账户账本') && !document.body.innerText.includes('正在读取原始决策')", "loaded data", 45000);
      const layout = await chrome.evaluate(`(() => ({ width:innerWidth, documentWidth:document.documentElement.scrollWidth,
        page:${JSON.stringify(page)}, device:${JSON.stringify(device.name)}, title:[...document.querySelectorAll('main h1,main h2')].find(el=>el.checkVisibility())?.textContent,
        requestedWidth:${device.width},
        horizontalOverflow:document.documentElement.scrollWidth>${device.width}+1 || innerWidth>${device.width}+1,
        headerOverflow:[...document.querySelector('.topbar').children].some(el=>{const r=el.getBoundingClientRect(),h=document.querySelector('.topbar').getBoundingClientRect();return r.top<h.top-1||r.bottom>h.bottom+1}),
        offenders:[...document.querySelectorAll('body *')].filter(el=>el.checkVisibility()).map(el=>({el,r:el.getBoundingClientRect()}))
          .filter(({el,r})=>r.right>innerWidth+2 && !el.closest('.table-wrap,.tabs')).slice(0,12)
          .map(({el,r})=>({tag:el.tagName,class:el.className,text:el.textContent.trim().slice(0,55),right:Math.round(r.right)}))
      }))()`);
      report.layouts.push(layout);
      report.screenshots.push({ device: device.name, page, path: await chrome.screenshot(`${device.name}-${page}`) });
      report.checks.push({ name: `${device.name}-${page}-no-document-overflow`, ok: !layout.horizontalOverflow });
      report.checks.push({ name: `${device.name}-${page}-header-contained`, ok: !layout.headerOverflow });
      console.log(`${device.name} ${page}: width=${layout.width} document=${layout.documentWidth} ${layout.horizontalOverflow ? "OVERFLOW" : "OK"}`);
    }
  }
  if (!argumentsList.includes("--layout-only")) {
  // A real navigation click and utility menu are checked without posting user decisions.
  await chrome.viewport(390, 844, true);
  await chrome.send("Page.navigate", { url: `${url}/#today` });
  await chrome.waitFor("!!document.querySelector('nav[aria-label=\"主要任务\"]')", "navigation");
  await chrome.click('nav[aria-label="主要任务"] button', "研究");
  report.checks.push({ name: "mobile-navigation-click", ok: await chrome.waitFor("location.hash === '#research'", "research navigation") });
  await chrome.click(".utility-menu > summary");
  report.screenshots.push({ device: "mobile", page: "utility-open", path: await chrome.screenshot("mobile-utility-open") });
  await chrome.click(".utility-menu button", "每日更新设置");
  report.checks.push({ name: "mobile-settings-click", ok: await chrome.waitFor("location.hash === '#settings'", "settings navigation") });
  }
  report.checks.push({ name: "no-user-data-write", ok: !chrome.events.some(event => event.method === "Network.requestWillBeSent" && /^(POST|PUT|DELETE|PATCH)$/.test(event.params.request.method)) });
  const exceptions = chrome.events.filter(event => event.method === "Runtime.exceptionThrown").map(event => event.params.exceptionDetails.text);
  report.checks.push({ name: "no-runtime-exceptions", ok: exceptions.length === 0, exceptions });
} catch (error) {
  report.error = error.message;
  report.pageText = await chrome.evaluate("document.body.innerText.slice(0,5000)").catch(() => "unavailable");
  report.networkFailures = chrome.events.filter(event => event.method === "Network.responseReceived" && event.params.response.status >= 400)
    .map(event => ({ url: event.params.response.url, status: event.params.response.status }));
  report.screenshots.push({ page: "blocked", path: await chrome.screenshot("blocked-state").catch(() => "unavailable") });
  console.error(error.message);
  process.exitCode = 1;
} finally {
  writeFileSync(join(output, "live-report.json"), JSON.stringify(report, null, 2));
  await chrome.close();
}
if (report.checks.some(item => !item.ok)) process.exitCode = 1;
