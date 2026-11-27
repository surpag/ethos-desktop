/**
 * 阶段 1 前置 spike：验证 WebContentsView 在真实 Electron 33 上的行为。
 *
 * 关闭假设（见 docs/design/browser-architecture.md §8）：
 *   A1 装了 ASRT（机器级 WFP 出站过滤器）之后，Electron 自己的网页请求还能不能出网；
 *   A2 `BrowserWindow.contentView.addChildView()` + `view.setBounds()` 是否按窗口坐标生效，
 *      页面是否真的在独立渲染进程里跑起来。
 *
 * A3（原生视图之上的模态处理）涉及可见窗口的观感，只能人工验收，本脚本不覆盖。
 *
 * 用法（在 desktop/ 下）：
 *   npx electron scripts/spikes/webcontentsview.cjs
 *
 * 输出一行 JSON，`ok: true` 表示 A1 + A2 的机械部分成立。
 * 刻意不写进应用代码：它是验证工具，不是功能。
 */

const { app, BrowserWindow, WebContentsView } = require("electron");

const TARGET = "https://example.com";
const TIMEOUT_MS = 30000;

const result = {
  ok: false,
  steps: [],
  a1_network: null,
  a2_bounds: null,
  a2_render: null,
  error: "",
};

function note(step, detail) {
  result.steps.push(detail === undefined ? step : `${step}: ${detail}`);
}

function finish(code) {
  process.stdout.write("\nSPIKE_RESULT " + JSON.stringify(result) + "\n");
  app.exit(code);
}

const guard = setTimeout(() => {
  result.error = `超时 ${TIMEOUT_MS}ms，卡在最后一步：${result.steps.at(-1) || "启动"}`;
  finish(2);
}, TIMEOUT_MS);

app.whenReady().then(async () => {
  let win;
  let view;
  try {
    win = new BrowserWindow({
      width: 1000,
      height: 700,
      show: false,
      webPreferences: { contextIsolation: true, nodeIntegration: false },
    });
    win.loadURL("data:text/html,<h1>spike</h1>");
    note("window-created");

    view = new WebContentsView({
      webPreferences: {
        partition: "persist:ethos-spike",
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
      },
    });
    win.contentView.addChildView(view);
    note("view-attached");

    const bounds = { x: 120, y: 60, width: 600, height: 400 };
    view.setBounds(bounds);
    const applied = view.getBounds();
    result.a2_bounds = {
      requested: bounds,
      applied,
      matches:
        applied.x === bounds.x &&
        applied.y === bounds.y &&
        applied.width === bounds.width &&
        applied.height === bounds.height,
    };
    note("set-bounds", JSON.stringify(applied));

    // A1：真实外网请求。did-fail-load 会带 errorCode/errorDescription。
    const loadOutcome = await new Promise((resolve) => {
      const timer = setTimeout(() => resolve({ ok: false, detail: "load-timeout" }), 20000);
      view.webContents.once("did-finish-load", () => {
        clearTimeout(timer);
        resolve({ ok: true, detail: "did-finish-load" });
      });
      view.webContents.once("did-fail-load", (_e, code, desc, url) => {
        clearTimeout(timer);
        resolve({ ok: false, detail: `did-fail-load ${code} ${desc} ${url}` });
      });
      view.webContents.loadURL(TARGET).catch((error) => {
        clearTimeout(timer);
        resolve({ ok: false, detail: `loadURL rejected: ${error && error.message}` });
      });
    });
    result.a1_network = { target: TARGET, ...loadOutcome };
    note("load", loadOutcome.detail);

    // A2 渲染侧：页面真的执行了（不是被 WFP 拦掉后的错误页也算失败）
    const probe = await view.webContents.executeJavaScript(
      "({ title: document.title, textLen: document.body ? document.body.innerText.length : 0, url: location.href })",
    );
    result.a2_render = probe;
    note("dom-probe", JSON.stringify(probe));

    const prefs = view.webContents.getLastWebPreferences() || {};
    result.a2_webprefs = {
      sandbox: prefs.sandbox,
      contextIsolation: prefs.contextIsolation,
      nodeIntegration: prefs.nodeIntegration,
      partition: prefs.partition,
    };

    // 尺寸变更再次回读：验证 setBounds 是活的操作而不是一次性
    view.setBounds({ x: 0, y: 0, width: 320, height: 240 });
    result.a2_bounds.second = view.getBounds();

    result.ok = Boolean(
      result.a2_bounds.matches &&
        result.a2_bounds.second.width === 320 &&
        result.a1_network.ok &&
        result.a2_render &&
        result.a2_render.textLen > 0,
    );
    note("done");
    clearTimeout(guard);
    finish(result.ok ? 0 : 1);
  } catch (error) {
    result.error = String((error && error.stack) || error);
    clearTimeout(guard);
    finish(3);
  } finally {
    try {
      if (view) {
        win.contentView.removeChildView(view);
        view.webContents.close();
      }
      if (win) win.destroy();
    } catch {
      /* 清理失败不影响结论 */
    }
  }
});
