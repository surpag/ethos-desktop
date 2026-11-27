/**
 * 阶段 3 验收：真实 Electron ↔ 真实 Python 双进程通信。
 *
 * 不 mock 任何一侧：
 *   - Electron 侧起真的 `startBrowserRpcServer()` + 真的 `BrowserManager` + 真的网页；
 *   - Python 侧由 `child_process.spawn` 拉起 `backend-py/scripts/browser_rpc_probe.py`，
 *     用真实的 `BrowserRpcClient` 通过 127.0.0.1 HTTP 调用。
 *
 * 覆盖：令牌注入、health、当前标签页、导航、导航结果在 Electron 侧可见、
 * 错误令牌 401、未知方法 404、超大请求体 413、服务关闭后 Python 立即感知。
 *
 * 用法（先 `npm run build`）：
 *   npx electron scripts/spikes/rpc-smoke.cjs
 */

const http = require("node:http");
const path = require("node:path");
const { spawn } = require("node:child_process");

const { app, BrowserWindow } = require("electron");
const { BrowserManager } = require("../../dist/main/browser/browser-manager");
const { startBrowserRpcServer } = require("../../dist/main/browser/browser-rpc-server");

const PYTHON = path.join(__dirname, "../../../backend-py/.venv/Scripts/python.exe");
const BACKEND_DIR = path.join(__dirname, "../../../backend-py");
const TARGET = "https://example.org/";

const checks = [];
const timeout = setTimeout(() => report("timeout"), 150000);

function check(name, ok, detail) {
  checks.push({ name, ok, ...(detail === undefined ? {} : { detail }) });
}

function report(fatal) {
  clearTimeout(timeout);
  const failed = checks.filter((c) => !c.ok);
  process.stdout.write(
    "\nRPC_RESULT " +
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

function waitFor(predicate, ms = 30000) {
  return new Promise((resolve) => {
    const deadline = Date.now() + ms;
    const tick = () => {
      if (predicate()) return resolve(true);
      if (Date.now() > deadline) return resolve(false);
      setTimeout(tick, 150);
    };
    tick();
  });
}

function rpcRequest(baseUrl, token, body) {
  return new Promise((resolve) => {
    const target = new URL(`${baseUrl}/rpc`);
    const data = Buffer.from(typeof body === "string" ? body : JSON.stringify(body), "utf8");
    const req = http.request(
      {
        hostname: target.hostname,
        port: target.port,
        path: target.pathname,
        method: "POST",
        headers: {
          "content-type": "application/json",
          "content-length": data.byteLength,
          authorization: `Bearer ${token}`,
        },
        timeout: 8000,
      },
      (res) => {
        const chunks = [];
        res.on("data", (chunk) => chunks.push(chunk));
        res.on("end", () =>
          resolve({ status: res.statusCode, body: Buffer.concat(chunks).toString("utf8") }),
        );
      },
    );
    req.on("error", (error) => resolve({ status: 0, body: "", error: String(error) }));
    req.on("timeout", () => {
      req.destroy();
      resolve({ status: 0, body: "", error: "timeout" });
    });
    req.write(data);
    req.end();
  });
}

function runProbe(env) {
  return new Promise((resolve) => {
    const child = spawn(PYTHON, ["scripts/browser_rpc_probe.py"], {
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
        .filter((item) => item.startsWith("PROBE_RESULT "))
        .pop();
      let result = null;
      try {
        result = line ? JSON.parse(line.slice("PROBE_RESULT ".length)) : null;
      } catch {
        result = null;
      }
      resolve({ code, result, stdout: stdout.slice(-2000), stderr: stderr.slice(-2000) });
    });
  });
}

app.whenReady().then(async () => {
  const rpc = await startBrowserRpcServer();
  check("rpc-listening", rpc.url.startsWith("http://127.0.0.1:"), rpc.url);
  check("token-not-in-url", !rpc.url.includes(rpc.token));
  check("token-length", rpc.token.length === 64, rpc.token.length);

  const win = new BrowserWindow({ width: 1000, height: 700, show: false });
  win.loadURL("data:text/html,<h1>rpc-smoke</h1>");
  const manager = new BrowserManager(win);
  rpc.attach(manager);
  manager.setViewport({ x: 600, y: 56, width: 400, height: 600 });
  manager.open("https://example.com");

  const loaded = await waitFor(() => {
    const tab = manager.state().tabs[0];
    return Boolean(tab && !tab.loading && tab.title);
  });
  check("desktop-tab-loaded", loaded, manager.state().tabs[0]);

  const env = {
    ETHOS_BROWSER_RPC_URL: rpc.url,
    ETHOS_BROWSER_RPC_TOKEN: rpc.token,
    BROWSER_PROBE_TARGET: TARGET,
  };

  // 1) 正常路径：Python 真实调用
  const probe = await runProbe(env);
  check("probe-ok", probe.result?.ok === true, probe.result || probe.stderr);
  const healthStep = probe.result?.steps?.find((item) => item.step === "health");
  check("probe-health-manager-ready", healthStep?.result?.managerReady === true, healthStep?.result);
  const afterStep = probe.result?.steps?.find((item) => item.step === "after");
  check("probe-url-after-navigate", String(afterStep?.result?.url || "").includes("example.org"), afterStep?.result);

  // 2) Electron 侧必须看到同一页：证明两边是同一个浏览器，不是各开一个
  const electronUrl = manager.state().tabs[0]?.url || "";
  check("same-view-both-sides", electronUrl.includes("example.org"), electronUrl);

  // 3) 错误令牌
  const badToken = await rpcRequest(rpc.url, "deadbeef", { method: "health" });
  check("wrong-token-401", badToken.status === 401, badToken.status);

  // 4) 未知方法
  const unknown = await rpcRequest(rpc.url, rpc.token, { method: "system.exec", params: {} });
  check("unknown-method-404", unknown.status === 404, unknown.status);

  // 5) 超大请求体（>64KB）
  const huge = await rpcRequest(rpc.url, rpc.token, {
    method: "health",
    params: { blob: "x".repeat(70000) },
  });
  check("oversize-body-413", huge.status === 413, huge.status);

  // 6) 服务关闭后 Python 必须立刻感知（不能静默挂住）
  rpc.dispose();
  const closed = await runProbe(env);
  check("probe-after-close", closed.result?.ok === false, closed.result);
  check(
    "probe-after-close-code",
    closed.result?.error?.code === "unavailable",
    closed.result?.error,
  );

  try {
    manager.dispose();
    win.destroy();
  } catch {
    /* 清理失败不影响结论 */
  }
  report("");
});
