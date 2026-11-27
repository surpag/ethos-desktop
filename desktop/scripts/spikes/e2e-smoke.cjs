/**
 * 阶段 8 E2E：12 项固定测试（真实 Electron + 真实 Python 工具链）。
 *
 * Python 侧（`browser_e2e_probe.py`）按阶段执行；需要先改环境的三项
 * （多标签、崩溃、RPC 断开）以及两项控制权用例（用户接管、取消后释放）
 * 由本脚本编排。
 *
 * 用法（先 `npm run build`）：
 *   npx electron scripts/spikes/e2e-smoke.cjs
 */

const http = require("node:http");
const path = require("node:path");
const { spawn } = require("node:child_process");

const { app, BrowserWindow } = require("electron");
const { BrowserManager } = require("../../dist/main/browser/browser-manager");
const { startBrowserRpcServer } = require("../../dist/main/browser/browser-rpc-server");

const PYTHON = path.join(__dirname, "../../../backend-py/.venv/Scripts/python.exe");
const BACKEND_DIR = path.join(__dirname, "../../../backend-py");
const RUN_A = "e2e-run";
const RUN_B = "e2e-run-2";

const checks = [];
const timeout = setTimeout(() => report("timeout"), 240000);

function check(name, ok, detail) {
  checks.push({ name, ok: Boolean(ok), ...(detail === undefined ? {} : { detail }) });
}

function report(fatal) {
  clearTimeout(timeout);
  const failed = checks.filter((item) => !item.ok);
  process.stdout.write(
    "\nE2E_RESULT " +
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

const APP_PAGE = `<!doctype html><html><head><meta charset="utf-8"><title>E2E 测试页</title></head>
<body style="font:16px system-ui;padding:24px">
  <h1>E2E 测试页</h1>
  <p id="status">初始状态</p>
  <button id="switch">切换文字</button>
  <a id="to-second" href="/second">去第二页</a>
  <form action="/results" method="get">
    <input name="q" placeholder="搜索关键词">
    <button type="submit">提交</button>
  </form>
  <div style="height:4000px"></div>
  <script>
    document.getElementById('switch').addEventListener('click', function () {
      document.getElementById('status').textContent = '已切换';
      document.title = '已切换-1';
    });
  </script>
</body></html>`;

const RESULTS_PAGE = `<!doctype html><html><head><meta charset="utf-8"><title>结果页</title></head>
<body style="font:16px system-ui;padding:24px">
  <h1>结果页</h1>
  <a href="/second">去第二页</a>
  <div style="height:5000px"></div>
</body></html>`;

const SECOND_PAGE = `<!doctype html><html><head><meta charset="utf-8"><title>第二页</title></head>
<body style="font:16px system-ui;padding:24px">
  <h1>第二页</h1>
  <button id="second-btn">第二页按钮</button>
  <script>
    document.getElementById('second-btn').addEventListener('click', function () {
      document.title = 'clicked-second';
    });
  </script>
</body></html>`;

function startFixtureServer() {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url || "/", "http://127.0.0.1");
    res.setHeader("content-type", "text/html; charset=utf-8");
    if (url.pathname === "/results") {
      res.end(RESULTS_PAGE);
      return;
    }
    if (url.pathname === "/second") {
      res.end(SECOND_PAGE);
      return;
    }
    res.end(APP_PAGE);
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve({ server, port: server.address().port }));
  });
}

function runProbe(env) {
  return new Promise((resolve) => {
    const child = spawn(PYTHON, ["scripts/browser_e2e_probe.py"], {
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
        .filter((item) => item.startsWith("E2E_PROBE "))
        .pop();
      let result = null;
      try {
        result = line ? JSON.parse(line.slice("E2E_PROBE ".length)) : null;
      } catch {
        result = null;
      }
      resolve({ code, result, stdout: stdout.slice(-4000), stderr: stderr.slice(-3000) });
    });
  });
}

function rpcCall(rpc, method, params, requestId) {
  return fetch(`${rpc.url}/rpc`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${rpc.token}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({ method, params: params || {}, request_id: requestId }),
  }).then((response) => response.json());
}

async function expectProbe(name, env, expectedCount) {
  const probe = await runProbe(env);
  check(`${name}-exited`, probe.code === 0, probe.result?.error || probe.stderr);
  check(`${name}-ok`, probe.result?.ok === true, probe.result?.error || probe.stderr);
  const cases = probe.result?.cases || [];
  check(`${name}-case-count`, cases.length === expectedCount, cases.map((item) => item.id));
  for (const item of cases) {
    check(`${item.id} ${item.name}`, item.ok === true, item.detail);
  }
  return probe;
}

app.whenReady().then(async () => {
  const fixture = await startFixtureServer();
  const origin = `http://127.0.0.1:${fixture.port}`;
  const baseEnv = { BROWSER_E2E_ORIGIN: origin };

  const rpc = await startBrowserRpcServer();
  const win = new BrowserWindow({ width: 1100, height: 760, show: false });
  win.loadURL("data:text/html,<h1>e2e-smoke</h1>");
  const manager = new BrowserManager(win);
  rpc.attach(manager);
  manager.setViewport({ x: 620, y: 56, width: 460, height: 640 });
  manager.open(`${origin}/app`);

  const loaded = await waitFor(() => {
    const tab = manager.state().tabs[0];
    return Boolean(tab && !tab.loading && tab.title.includes("E2E 测试页"));
  });
  check("fixture-page-loaded", loaded, manager.state().tabs[0]);

  const env = {
    ...baseEnv,
    ETHOS_BROWSER_RPC_URL: rpc.url,
    ETHOS_BROWSER_RPC_TOKEN: rpc.token,
    E2E_RUN_ID: RUN_A,
  };

  // E1-E6 + E8
  await expectProbe("main", { ...env, E2E_PHASE: "main" }, 7);

  // E7：打开第二页并激活，工具必须操作「当前激活标签页」。
  const second = manager.open(`${origin}/second`, false);
  check("second-tab-opened", Boolean(second?.tabId), second);
  if (second?.tabId) manager.activate(second.tabId);
  await waitFor(() => {
    const tab = manager.state().tabs.find((item) => item.tabId === second?.tabId);
    return Boolean(tab && !tab.loading && tab.title.includes("第二页"));
  });
  await expectProbe("multitab", { ...env, E2E_PHASE: "multitab" }, 1);

  // E9：用户接管后，Agent 的写操作必须被拒。
  manager.userTakeover();
  const blocked = await rpcCall(rpc, "page.snapshot", {}, RUN_A);
  check(
    "E9 user-takeover-blocks-agent",
    blocked.ok === false && blocked.error?.code === "user_took_control",
    blocked,
  );

  // E10：控制权释放后，另一个 run 可以立刻接管（不必等 20s 租约）。
  await sleep(1800);
  const regrant = await rpcCall(rpc, "page.snapshot", {}, RUN_A);
  check("E10 agent-regrants-before-release", regrant.ok === true, regrant);
  const released = await rpcCall(rpc, "control.release", {}, RUN_A);
  check("E10 release-acknowledged", released.ok === true && released.result?.released === true, released);
  check("E10 state-back-to-user", manager.state().control.owner === "user", manager.state().control);
  const nextRun = await rpcCall(rpc, "page.snapshot", {}, RUN_B);
  check(
    "E10 next-run-operates-immediately",
    nextRun.ok === true && manager.state().control.requestId === RUN_B,
    { ok: nextRun.ok, control: manager.state().control },
  );

  // E12：崩溃 → 明确报错 → 重新打开恢复。
  const activeTabId = manager.state().activeTabId;
  const activeTab = manager["tabs"].get(activeTabId);
  activeTab?.view.webContents.forcefullyCrashRenderer();
  const crashed = await waitFor(() => {
    const tab = manager.state().tabs.find((item) => item.tabId === activeTabId);
    return Boolean(tab && tab.crashed);
  }, 10000);
  check("E12 renderer-reported-crashed", crashed, manager.state().tabs);
  await expectProbe(
    "crash",
    { ...env, E2E_PHASE: "crash", E2E_RUN_ID: RUN_B },
    2,
  );

  // E11：RPC 断开（放在最后，因为之后服务不再可用）。
  rpc.dispose();
  await sleep(200);
  await expectProbe("offline", { ...env, E2E_PHASE: "offline", E2E_RUN_ID: RUN_B }, 1);

  try {
    manager.dispose();
    win.destroy();
  } catch {
    /* 清理失败不影响结论 */
  }
  fixture.server.close();
  report("");
});
