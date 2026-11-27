/**
 * 阶段 6 验收：request_id 透传 + 控制权。
 *
 * 两段验证：
 * 1. 真实 Python 工具链（`browser_control_probe.py`）：两个 request_id 的写操作
 *    必须串行，只读的 `page.url` 不受限；
 * 2. Electron 进程内：用户夺回 → Agent 立即被拒 → 抑制窗口过后自动重接管 →
 *    租约过期自动释放（用短租约实例验证，避免等 20 秒）。
 *
 * 用法（先 `npm run build`）：
 *   npx electron scripts/spikes/control-smoke.cjs
 */

const http = require("node:http");
const path = require("node:path");
const { spawn } = require("node:child_process");

const { app, BrowserWindow } = require("electron");
const { BrowserControl } = require("../../dist/main/browser/browser-control");
const { BrowserManager } = require("../../dist/main/browser/browser-manager");
const { startBrowserRpcServer } = require("../../dist/main/browser/browser-rpc-server");

const PYTHON = path.join(__dirname, "../../../backend-py/.venv/Scripts/python.exe");
const BACKEND_DIR = path.join(__dirname, "../../../backend-py");

const RUN_A = "req-probe-a";
const checks = [];
const timeout = setTimeout(() => report("timeout"), 180000);

function check(name, ok, detail) {
  checks.push({ name, ok: Boolean(ok), ...(detail === undefined ? {} : { detail }) });
}

function report(fatal) {
  clearTimeout(timeout);
  const failed = checks.filter((item) => !item.ok);
  process.stdout.write(
    "\nCONTROL_RESULT " +
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

const PAGE = `<!doctype html><html><head><meta charset="utf-8"><title>控制权测试页</title></head>
<body style="font:16px system-ui;padding:24px"><h1>控制权测试页</h1><button>确定</button></body></html>`;

function startFixtureServer() {
  const server = http.createServer((_req, res) => {
    res.setHeader("content-type", "text/html; charset=utf-8");
    res.end(PAGE);
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve({ server, port: server.address().port }));
  });
}

function runProbe(env) {
  return new Promise((resolve) => {
    const child = spawn(PYTHON, ["scripts/browser_control_probe.py"], {
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
        .filter((item) => item.startsWith("CONTROL_PROBE "))
        .pop();
      let result = null;
      try {
        result = line ? JSON.parse(line.slice("CONTROL_PROBE ".length)) : null;
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

app.whenReady().then(async () => {
  const fixture = await startFixtureServer();
  const origin = `http://127.0.0.1:${fixture.port}`;
  check("fixture-listening", fixture.port > 0, origin);

  const rpc = await startBrowserRpcServer();
  const win = new BrowserWindow({ width: 1100, height: 760, show: false });
  win.loadURL("data:text/html,<h1>control-smoke</h1>");
  const manager = new BrowserManager(win);
  rpc.attach(manager);
  manager.setViewport({ x: 620, y: 56, width: 460, height: 640 });
  manager.open(`${origin}/control`);

  const loaded = await waitFor(() => {
    const tab = manager.state().tabs[0];
    return Boolean(tab && !tab.loading && tab.title.includes("控制权测试页"));
  });
  check("fixture-page-loaded", loaded, manager.state().tabs[0]);

  const probe = await runProbe({
    ETHOS_BROWSER_RPC_URL: rpc.url,
    ETHOS_BROWSER_RPC_TOKEN: rpc.token,
    BROWSER_CONTROL_ORIGIN: origin,
  });
  check("probe-exited", probe.code === 0, probe.result?.error || probe.stderr);
  check("probe-ok", probe.result?.ok === true, probe.result?.error || probe.stderr);
  const cases = probe.result?.cases || [];
  check("probe-case-count", cases.length === 5, cases.map((item) => item.id));
  for (const item of cases) {
    check(`${item.id} ${item.name}`, item.ok === true, item.detail);
  }

  // ── 用户夺回：Agent 立即被拒，抑制窗口过后自动重接管 ──────────────────
  const ownedByProbe = manager.state().control;
  check(
    "state-agent-owned-after-probe",
    ownedByProbe.owner === "agent" && ownedByProbe.requestId === RUN_A,
    ownedByProbe,
  );

  manager.userTakeover();
  const afterUser = manager.state().control;
  check("user-takeover-visible-in-state", afterUser.owner === "user", afterUser);

  const blocked = await rpcCall(rpc, "page.snapshot", {}, RUN_A);
  check(
    "agent-blocked-right-after-user",
    blocked.ok === false && blocked.error?.code === "user_took_control",
    blocked,
  );

  await sleep(1800);
  const regranted = await rpcCall(rpc, "page.snapshot", {}, RUN_A);
  check("agent-regrants-after-guard", regranted.ok === true, regranted);
  const afterRegrant = manager.state().control;
  check(
    "state-agent-owned-again",
    afterRegrant.owner === "agent" && afterRegrant.requestId === RUN_A,
    afterRegrant,
  );

  // ── 租约过期（短租约实例，避免等待生产值 20s）────────────────────────
  const short = new BrowserControl({ leaseMs: 200, grabGuardMs: 50 });
  const t0 = 1_000_000;
  check("lease-first-run-granted", short.acquire("run-a", t0) === "granted");
  check("lease-second-run-busy", short.acquire("run-b", t0 + 100) === "busy");
  check("lease-expired-then-granted", short.acquire("run-b", t0 + 400) === "granted");
  check(
    "lease-expired-owner-is-run-b",
    short.snapshot(t0 + 400).owner === "agent" &&
      short.snapshot(t0 + 400).requestId === "run-b",
    short.snapshot(t0 + 400),
  );

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
