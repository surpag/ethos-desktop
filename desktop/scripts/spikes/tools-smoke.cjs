/**
 * 阶段 4 验收：真实 Electron + 真实 Python 工具链 + 真实本地测试页。
 *
 * 链路：`BrowserToolSet → BrowserService → BrowserRpcClient → HTTP → BrowserManager
 * → WebContentsView → Chromium`。页面是本脚本自己起的固定测试页（方案 §8 要求
 * 不依赖外部站点），因此结果可重复。
 *
 * 覆盖：打开 URL、取快照、填写输入框、点击提交、导航后回读 URL（表单真的带上了
 * 填写的值）、旧快照必须失效。
 *
 * 用法（先 `npm run build`）：
 *   npx electron scripts/spikes/tools-smoke.cjs
 */

const http = require("node:http");
const path = require("node:path");
const { spawn } = require("node:child_process");

const { app, BrowserWindow } = require("electron");
const { BrowserManager } = require("../../dist/main/browser/browser-manager");
const { startBrowserRpcServer } = require("../../dist/main/browser/browser-rpc-server");

const PYTHON = path.join(__dirname, "../../../backend-py/.venv/Scripts/python.exe");
const BACKEND_DIR = path.join(__dirname, "../../../backend-py");

const checks = [];
const timeout = setTimeout(() => report("timeout"), 150000);

function check(name, ok, detail) {
  checks.push({ name, ok, ...(detail === undefined ? {} : { detail }) });
}

function report(fatal) {
  clearTimeout(timeout);
  const failed = checks.filter((c) => !c.ok);
  process.stdout.write(
    "\nTOOLS_RESULT " +
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

function waitFor(predicate, ms = 25000) {
  return new Promise((resolve) => {
    const deadline = Date.now() + ms;
    const tick = () => {
      if (predicate()) return resolve(true);
      if (Date.now() > deadline) return resolve(false);
      setTimeout(tick, 120);
    };
    tick();
  });
}

const FORM_PAGE = `<!doctype html><html><head><meta charset="utf-8"><title>工具测试页</title></head>
<body style="font:16px system-ui;padding:24px">
  <h1>工具测试页</h1>
  <form action="/result" method="get">
    <input name="q" placeholder="搜索词" aria-label="搜索词">
    <button type="submit">提交</button>
  </form>
  <p><a href="/second">第二页</a></p>
</body></html>`;

const SECOND_PAGE = `<!doctype html><html><head><meta charset="utf-8"><title>第二页</title></head>
<body style="font:16px system-ui;padding:24px"><h1>第二页</h1></body></html>`;

function startFixtureServer() {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url || "/", "http://127.0.0.1");
    res.setHeader("content-type", "text/html; charset=utf-8");
    if (url.pathname === "/result") {
      const q = url.searchParams.get("q") || "";
      res.end(
        `<!doctype html><html><head><meta charset="utf-8"><title>结果</title></head>` +
          `<body style="font:16px system-ui;padding:24px"><h1>结果</h1>` +
          `<p id="echo">q=${q}</p></body></html>`,
      );
      return;
    }
    if (url.pathname === "/second") {
      res.end(SECOND_PAGE);
      return;
    }
    res.end(FORM_PAGE);
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve({ server, port: server.address().port }));
  });
}

function runProbe(env) {
  return new Promise((resolve) => {
    const child = spawn(PYTHON, ["scripts/browser_tools_probe.py"], {
      cwd: BACKEND_DIR,
      env: { ...process.env, PYTHONPATH: path.join(BACKEND_DIR, "src"), ...env },
      windowsHide: true,
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => (stdout += chunk.toString()));
    child.stderr.on("data", (chunk) => (stderr += chunk.toString()));
    child.on("close", (code) => {
      const line = stdout
        .split(/\r?\n/)
        .filter((item) => item.startsWith("TOOLS_PROBE "))
        .pop();
      let result = null;
      try {
        result = line ? JSON.parse(line.slice("TOOLS_PROBE ".length)) : null;
      } catch {
        result = null;
      }
      resolve({ code, result, stdout: stdout.slice(-3000), stderr: stderr.slice(-2000) });
    });
  });
}

app.whenReady().then(async () => {
  const fixture = await startFixtureServer();
  const target = `http://127.0.0.1:${fixture.port}/`;
  check("fixture-listening", fixture.port > 0, target);

  const rpc = await startBrowserRpcServer();
  const win = new BrowserWindow({ width: 1100, height: 760, show: false });
  win.loadURL("data:text/html,<h1>tools-smoke</h1>");
  const manager = new BrowserManager(win);
  rpc.attach(manager);
  manager.setViewport({ x: 620, y: 56, width: 460, height: 640 });
  manager.open(target);

  const loaded = await waitFor(() => {
    const tab = manager.state().tabs[0];
    return Boolean(tab && !tab.loading && tab.title.includes("工具测试页"));
  });
  check("fixture-page-loaded", loaded, manager.state().tabs[0]);

  const probe = await runProbe({
    ETHOS_BROWSER_RPC_URL: rpc.url,
    ETHOS_BROWSER_RPC_TOKEN: rpc.token,
    BROWSER_TOOLS_TARGET: target,
  });

  const result = probe.result;
  check("probe-ok", result?.ok === true, result || probe.stderr);
  check("probe-final-url", String(result?.final_url || "").includes("q=hello"), result?.final_url);
  check("probe-stale-detected", result?.stale_code === "stale_observation", result?.stale_code);

  const fillStep = result?.steps?.find((step) => step.tool === "browser_fill");
  check("fill-reported-length", fillStep?.result?.filled === true && fillStep?.result?.length === 5, fillStep?.result);
  const clickStep = result?.steps?.find((step) => step.tool === "browser_click" && step.result?.clicked);
  check("click-succeeded", Boolean(clickStep), clickStep?.result);
  const snapshotStep = result?.steps?.find((step) => step.tool === "browser_get_page_snapshot");
  check(
    "snapshot-had-elements",
    (snapshotStep?.result?.elements || []).length >= 3,
    (snapshotStep?.result?.elements || []).length,
  );

  // Electron 侧看到同一个页面：证明工具操作的就是用户可见的视图
  const electronUrl = manager.state().tabs[0]?.url || "";
  check("same-view-both-sides", electronUrl.includes("q=hello"), electronUrl);

  try {
    rpc.dispose();
    manager.dispose();
    win.destroy();
  } catch {
    /* 清理失败不影响结论 */
  }
  fixture.server.close();
  report("");
});
