/**
 * 阶段 2 集成验证：真实 Electron 里跑 `BrowserManager`（不是 Mock）。
 *
 * 覆盖：
 *   - 由外部（模拟渲染进程）提供视口矩形，视图按矩形挂载
 *   - 多标签页：新建、切换（非当前页压成 0 尺寸）、关闭
 *   - 导航、地址栏跳转、前进/后退可用性
 *   - viewport 置空 = 收起（隐藏但**不销毁**标签页）
 *   - dispose 后再关窗口不炸
 *
 * 用法（先 `npm run build`）：
 *   npx electron scripts/spikes/manager-smoke.cjs
 */

const { app, BrowserWindow } = require("electron");
const { BrowserManager } = require("../../dist/main/browser/browser-manager");

const checks = [];
const timeout = setTimeout(() => report("timeout"), 90000);

function check(name, ok, detail) {
  checks.push({ name, ok, ...(detail === undefined ? {} : { detail }) });
}

function report(fatal) {
  clearTimeout(timeout);
  const failed = checks.filter((c) => !c.ok);
  process.stdout.write(
    "\nSMOKE_RESULT " +
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

async function waitFor(predicate, ms = 25000) {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    if (predicate()) return true;
    await new Promise((r) => setTimeout(r, 150));
  }
  return false;
}

const VIEWPORT = { x: 600, y: 56, width: 400, height: 600 };

app.whenReady().then(async () => {
  const win = new BrowserWindow({ width: 1000, height: 700, show: false });
  win.loadURL("data:text/html,<h1>smoke</h1>");
  const manager = new BrowserManager(win);
  const changes = [];
  manager.setStateListener((state) => changes.push(state.tabs.length));

  // 只设视口、还没有标签页时 `visible` 应为 false：visible 的语义是
  // 「有视图真的在显示」，不是「面板占位被打开了」。
  manager.setViewport(VIEWPORT);
  check("viewport-without-tab-not-visible", manager.visible === false);

  manager.open("https://example.com");
  const firstLoaded = await waitFor(() => {
    const tab = manager.state().tabs[0];
    return Boolean(tab && !tab.loading && tab.title);
  });
  const afterFirst = manager.state();
  check("first-tab-loaded", firstLoaded && afterFirst.tabs[0].title === "Example Domain", afterFirst.tabs[0]);
  check("first-tab-active", afterFirst.activeTabId === afterFirst.tabs[0].tabId);
  check("visible-after-first-tab", manager.visible === true && afterFirst.visible === true);

  const firstTabId = afterFirst.tabs[0].tabId;
  const firstTab = manager.tabs.get(firstTabId);
  const boundsA = firstTab.view.getBounds();
  check(
    "bounds-from-viewport",
    boundsA.x === 600 && boundsA.y === 56 && boundsA.width === 400 && boundsA.height === 600,
    boundsA,
  );

  // 第二个标签页：后台打开，不抢当前页
  manager.open("https://example.org", false);
  const secondLoaded = await waitFor(() => {
    const state = manager.state();
    const second = state.tabs[1];
    return Boolean(second && !second.loading && second.title);
  });
  const afterSecond = manager.state();
  check("second-tab-loaded", secondLoaded && afterSecond.tabs.length === 2, afterSecond.tabs.map((t) => t.title));
  check("background-open-keeps-active", afterSecond.activeTabId === firstTabId);

  const secondTabId = afterSecond.tabs[1].tabId;
  const secondTab = manager.tabs.get(secondTabId);
  check(
    "inactive-tab-collapsed",
    secondTab.view.getBounds().width === 0 && secondTab.view.getBounds().height === 0,
    secondTab.view.getBounds(),
  );

  // 切换：可见视图跟着换
  manager.activate(secondTabId);
  const afterActivate = manager.state();
  check("activate-switches-tab", afterActivate.activeTabId === secondTabId);
  check(
    "activate-swaps-bounds",
    secondTab.view.getBounds().width === 400 && firstTab.view.getBounds().width === 0,
    { second: secondTab.view.getBounds(), first: firstTab.view.getBounds() },
  );

  // 导航 + 历史
  manager.activate(firstTabId);
  await manager.navigate("https://example.org");
  const afterNavigate = manager.state();
  check(
    "navigate-updates-url",
    afterNavigate.tabs[0].url.includes("example.org"),
    afterNavigate.tabs[0].url,
  );
  check("can-go-back", afterNavigate.canGoBack === true);
  manager.goBack();
  const wentBack = await waitFor(() => manager.state().tabs[0].url.includes("example.com"), 15000);
  check("go-back-works", wentBack, manager.state().tabs[0].url);
  check("can-go-forward-after-back", manager.state().canGoForward === true);

  // 收起：隐藏但保留标签页
  manager.setViewport(null);
  const afterHide = manager.state();
  check("hidden-keeps-tabs", afterHide.tabs.length === 2 && afterHide.visible === false);
  check(
    "hidden-collapses-bounds",
    firstTab.view.getBounds().width === 0 && secondTab.view.getBounds().width === 0,
  );

  // 关闭一个标签页
  manager.close(secondTabId);
  const afterClose = manager.state();
  check("close-removes-tab", afterClose.tabs.length === 1 && afterClose.tabs[0].tabId === firstTabId);
  check("close-keeps-other-tab", firstTab.alive === true);

  let disposedError = "";
  try {
    manager.dispose();
    win.destroy();
  } catch (error) {
    disposedError = String((error && error.message) || error);
  }
  check("dispose-clean", disposedError === "", disposedError);
  check("listener-received-updates", changes.length > 0, changes.length);

  report("");
});
