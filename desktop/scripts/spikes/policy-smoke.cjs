/**
 * 阶段 7 验收：下载拒绝 / 非 http(s) 导航拦截 / 新窗口分流。
 *
 * 真实 Electron + 本地固定测试页。这里不需要 Python：Agent 没有下载工具，
 * 这些策略完全在主进程裁决。
 *
 * 用法（先 `npm run build`）：
 *   npx electron scripts/spikes/policy-smoke.cjs
 */

const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const { app, BrowserWindow, session } = require("electron");
const { BrowserManager } = require("../../dist/main/browser/browser-manager");

const checks = [];
const timeout = setTimeout(() => report("timeout"), 120000);

function check(name, ok, detail) {
  checks.push({ name, ok: Boolean(ok), ...(detail === undefined ? {} : { detail }) });
}

function report(fatal) {
  clearTimeout(timeout);
  const failed = checks.filter((item) => !item.ok);
  process.stdout.write(
    "\nPOLICY_RESULT " +
      JSON.stringify({
        ok: !fatal && failed.length === 0,
        fatal: fatal || "",
        total: checks.length,
        failed: failed.length,
        checks,
      }) +
      "\n",
  );
  app.exit(!fatal && failed.length === 0 ? 0 : 1);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function waitFor(predicate, ms = 20000) {
  return new Promise((resolve) => {
    const deadline = Date.now() + ms;
    const tick = () => {
      if (predicate()) return resolve(true);
      if (Date.now() > deadline) return resolve(false);
      setTimeout(tick, 100);
    };
    tick();
  });
}

function page(origin) {
  return `<!doctype html><html><head><meta charset="utf-8"><title>策略测试页</title></head>
<body style="font:16px system-ui;padding:24px">
  <h1>策略测试页</h1>
  <p><a id="dl" href="/download" download>下载一个文件</a></p>
  <p><a id="file" href="file:///C:/Windows/win.ini">打开本地文件</a></p>
  <p><a id="popup" href="/popup" target="_blank">新窗口（正常）</a></p>
  <p><a id="badpopup" href="file:///C:/Windows/win.ini" target="_blank">新窗口（本地文件）</a></p>
</body></html>`;
}

function startFixtureServer() {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url || "/", "http://127.0.0.1");
    if (url.pathname === "/download") {
      res.writeHead(200, {
        "content-type": "application/octet-stream",
        "content-disposition": 'attachment; filename="blocked.txt"',
      });
      res.end("this must never reach disk");
      return;
    }
    res.setHeader("content-type", "text/html; charset=utf-8");
    if (url.pathname === "/popup") {
      res.end('<!doctype html><html><head><meta charset="utf-8"><title>弹窗页</title></head><body><h1>弹窗页</h1></body></html>');
      return;
    }
    res.end(page("http://127.0.0.1"));
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve({ server, port: server.address().port }));
  });
}

app.whenReady().then(async () => {
  const fixture = await startFixtureServer();
  const origin = `http://127.0.0.1:${fixture.port}`;
  const downloadDir = fs.mkdtempSync(path.join(os.tmpdir(), "ethos-dl-"));
  // 用临时目录当下载目录：万一 preventDefault 失效，断言能直接看到落盘文件。
  session.fromPartition("persist:ethos-browser").setDownloadPath(downloadDir);

  const win = new BrowserWindow({ width: 1100, height: 760, show: false });
  win.loadURL("data:text/html,<h1>policy-smoke</h1>");
  const manager = new BrowserManager(win);
  manager.setViewport({ x: 620, y: 56, width: 460, height: 640 });
  manager.open(`${origin}/`);

  const loaded = await waitFor(() => {
    const tab = manager.state().tabs[0];
    return Boolean(tab && !tab.loading && tab.title.includes("策略测试页"));
  });
  check("fixture-page-loaded", loaded, manager.state().tabs[0]);

  const tabId = manager.state().activeTabId;
  const tab = manager["tabs"].get(tabId);

  // ── 下载：必须被拒绝且不落盘，同时给出可见提示 ────────────────────────
  await tab.runScript('document.getElementById("dl").click()');
  await sleep(1200);
  const filesAfterDownload = fs.readdirSync(downloadDir);
  check("download-blocked-no-file", filesAfterDownload.length === 0, filesAfterDownload);
  const downloadNotice = manager.state().tabs.find((item) => item.tabId === tabId)?.notice || "";
  check("download-blocked-notice", downloadNotice.includes("已拦截下载"), downloadNotice);

  // ── 页面内导航到 file://：必须被拦，URL 保持原地 ─────────────────────
  const beforeNav = manager.state().tabs.find((item) => item.tabId === tabId)?.url || "";
  await tab.runScript('document.getElementById("file").click()');
  await sleep(600);
  const afterNav = manager.state().tabs.find((item) => item.tabId === tabId)?.url || "";
  const afterTitle = manager.state().tabs.find((item) => item.tabId === tabId)?.title || "";
  check("file-navigation-blocked", afterNav === beforeNav, { beforeNav, afterNav });
  // 这条是实测补上的认知：http → file:// 在渲染进程就被 Chromium 拒绝，
  // Electron 侧连 `will-navigate` 都收不到，所以不会有 notice。安全目标仍然达成。
  check("file-navigation-page-intact", afterTitle.includes("策略测试页"), afterTitle);

  // ── window.open 正常目标：新标签页接管 ───────────────────────────────
  const tabsBeforePopup = manager.state().tabs.length;
  await tab.runScript('document.getElementById("popup").click()');
  const popupOpened = await waitFor(() => {
    const tabs = manager.state().tabs;
    return tabs.length === tabsBeforePopup + 1 && tabs.some((item) => item.url.includes("/popup"));
  }, 8000);
  check("window-open-http-creates-tab", popupOpened, manager.state().tabs.map((item) => item.url));

  // ── window.open 到 file://：不新增标签页，源页给出提示 ───────────────
  const beforeBad = manager.state().tabs.length;
  const sourceTab = manager["tabs"].get(tabId);
  await sourceTab.runScript('document.getElementById("badpopup").click()');
  await sleep(800);
  const badUrls = manager.state().tabs.map((item) => item.url);
  check("window-open-file-rejected", manager.state().tabs.length === beforeBad, badUrls);
  check(
    "window-open-file-no-file-url",
    badUrls.every((url) => !url.startsWith("file:")),
    badUrls,
  );

  // ── 页面脚本直接改 location 到 file://：同样被拦 ─────────────────────
  const beforeScript = manager.state().tabs.find((item) => item.tabId === tabId)?.url || "";
  await sourceTab.runScript('location.href = "file:///C:/Windows/win.ini"');
  await sleep(600);
  const afterScript = manager.state().tabs.find((item) => item.tabId === tabId)?.url || "";
  check("location-file-blocked", afterScript === beforeScript, { beforeScript, afterScript });

  // ── 正常 http 导航不能被误伤 ─────────────────────────────────────────
  await sourceTab.runScript('location.href = "/popup"');
  const httpNavOk = await waitFor(() => {
    const item = manager.state().tabs.find((entry) => entry.tabId === tabId);
    return Boolean(item && item.title.includes("弹窗页"));
  }, 8000);
  check(
    "http-navigation-still-works",
    httpNavOk,
    manager.state().tabs.find((entry) => entry.tabId === tabId),
  );

  check("download-dir-still-empty", fs.readdirSync(downloadDir).length === 0, fs.readdirSync(downloadDir));

  try {
    manager.dispose();
    win.destroy();
  } catch {
    /* 清理失败不影响结论 */
  }
  fixture.server.close();
  try {
    fs.rmSync(downloadDir, { recursive: true, force: true });
  } catch {
    /* 临时目录清理失败不影响结论 */
  }
  report("");
});
