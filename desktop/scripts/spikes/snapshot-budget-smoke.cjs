/**
 * 阶段 5 验收：快照预算 / 分页 / 元素级复核。
 *
 * 真实 Electron + 真实 Python 工具链 + 本地固定测试页（不依赖外部站点）。
 * 页面由本脚本自己起，11 条用例在 `backend-py/scripts/browser_snapshot_probe.py`
 * 里通过 `BrowserToolSet` 驱动，这里只负责搭环境 + 把结果转成断言。
 *
 * 用法（先 `npm run build`）：
 *   npx electron scripts/spikes/snapshot-budget-smoke.cjs
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
const timeout = setTimeout(() => report("timeout"), 180000);

function check(name, ok, detail) {
  checks.push({ name, ok: Boolean(ok), ...(detail === undefined ? {} : { detail }) });
}

function report(fatal) {
  clearTimeout(timeout);
  const failed = checks.filter((item) => !item.ok);
  process.stdout.write(
    "\nSNAPSHOT_RESULT " +
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

function bigPage(count) {
  const buttons = Array.from({ length: count }, (_, index) => {
    const label = `B${String(index + 1).padStart(3, "0")}`;
    return `<button style="display:block;margin:3px" onclick="document.title='clicked:${label}'">${label}</button>`;
  }).join("\n");
  return (
    `<!doctype html><html><head><meta charset="utf-8"><title>大列表</title></head>` +
    `<body style="font:14px system-ui;padding:16px">${buttons}</body></html>`
  );
}

function hugePage(count, length) {
  const text = "H" + "x".repeat(Math.max(1, length - 1));
  const buttons = Array.from(
    { length: count },
    () => `<button style="display:block;margin:2px">${text}</button>`,
  ).join("\n");
  return (
    `<!doctype html><html><head><meta charset="utf-8"><title>超长名称</title></head>` +
    `<body style="font:14px system-ui;padding:16px">${buttons}</body></html>`
  );
}

const MUTATE_PAGE = `<!doctype html><html><head><meta charset="utf-8"><title>局部刷新</title></head>
<body style="font:14px system-ui;padding:16px">
  <button id="starter">开始变化</button>
  <button id="changing">稍后变化</button>
  <button id="vanishing">稍后消失</button>
  <script>
    document.getElementById('starter').addEventListener('click', function () {
      setTimeout(function () { document.getElementById('changing').textContent = '已变化'; }, 300);
      setTimeout(function () { document.getElementById('vanishing').remove(); }, 600);
    });
  </script>
</body></html>`;

function startFixtureServer() {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url || "/", "http://127.0.0.1");
    res.setHeader("content-type", "text/html; charset=utf-8");
    if (url.pathname === "/big") {
      res.end(bigPage(Number(url.searchParams.get("n") || 300)));
      return;
    }
    if (url.pathname === "/huge") {
      res.end(
        hugePage(
          Number(url.searchParams.get("n") || 200),
          Number(url.searchParams.get("len") || 200),
        ),
      );
      return;
    }
    if (url.pathname === "/mutate") {
      res.end(MUTATE_PAGE);
      return;
    }
    res.end("<!doctype html><html><body><h1>ok</h1></body></html>");
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve({ server, port: server.address().port }));
  });
}

function runProbe(env) {
  return new Promise((resolve) => {
    const child = spawn(PYTHON, ["scripts/browser_snapshot_probe.py"], {
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
        .filter((item) => item.startsWith("SNAPSHOT_PROBE "))
        .pop();
      let result = null;
      try {
        result = line ? JSON.parse(line.slice("SNAPSHOT_PROBE ".length)) : null;
      } catch {
        result = null;
      }
      resolve({ code, result, stdout: stdout.slice(-4000), stderr: stderr.slice(-3000) });
    });
  });
}

app.whenReady().then(async () => {
  const fixture = await startFixtureServer();
  const origin = `http://127.0.0.1:${fixture.port}`;
  check("fixture-listening", fixture.port > 0, origin);

  const rpc = await startBrowserRpcServer();
  const win = new BrowserWindow({ width: 1100, height: 760, show: false });
  win.loadURL("data:text/html,<h1>snapshot-budget-smoke</h1>");
  const manager = new BrowserManager(win);
  rpc.attach(manager);
  manager.setViewport({ x: 620, y: 56, width: 460, height: 640 });
  manager.open(`${origin}/big?n=300`);

  const loaded = await waitFor(() => {
    const tab = manager.state().tabs[0];
    return Boolean(tab && !tab.loading && tab.title.includes("大列表"));
  });
  check("fixture-page-loaded", loaded, manager.state().tabs[0]);

  const probe = await runProbe({
    ETHOS_BROWSER_RPC_URL: rpc.url,
    ETHOS_BROWSER_RPC_TOKEN: rpc.token,
    BROWSER_SNAPSHOT_ORIGIN: origin,
  });

  check("probe-exited", probe.code === 0, probe.result?.error || probe.stderr);
  check("probe-ok", probe.result?.ok === true, probe.result?.error || probe.stderr);

  const cases = probe.result?.cases || [];
  check("probe-case-count", cases.length === 11, cases.map((item) => item.id));
  for (const item of cases) {
    check(`${item.id} ${item.name}`, item.ok === true, item.detail);
  }

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
