import fs from "node:fs";
import path from "node:path";

import { BrowserWindow, app, dialog, shell } from "electron";

import { backendDir, pythonExecutable, webStandaloneDir } from "./paths";
import { findFreePort, startProcess, waitForHttp, type ManagedProcess } from "./process";

// 用户数据目录名与后端 `config.data_root()` 的缺省值保持一致：
// 两边独立推导出同一个路径，就不必再靠一个环境变量去「对齐」。
app.setPath("userData", path.join(app.getPath("appData"), "ETHOS"));

let backend: ManagedProcess | null = null;
let web: ManagedProcess | null = null;
let window: BrowserWindow | null = null;
let shuttingDown = false;

function createWindow(url: string): BrowserWindow {
  const win = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 960,
    minHeight: 640,
    backgroundColor: "#0b0b0c",
    show: false,
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.js"),
    },
  });

  win.once("ready-to-show", () => win.show());

  // 外链一律交给系统浏览器：应用窗口没有地址栏，跳出去就回不来。
  win.webContents.setWindowOpenHandler(({ url: target }) => {
    void shell.openExternal(target);
    return { action: "deny" };
  });

  void win.loadURL(url);
  return win;
}

async function startServices(): Promise<string> {
  const dataDir = app.getPath("userData");
  fs.mkdirSync(dataDir, { recursive: true });

  const backendPort = await findFreePort();
  const webPort = await findFreePort();

  const python = pythonExecutable();
  backend = startProcess(
    python,
    ["-m", "uvicorn", "edith_backend.main:app", "--host", "127.0.0.1", "--port", String(backendPort)],
    {
      cwd: backendDir(),
      label: "backend",
      env: {
        ...process.env,
        // 桌面模式：无认证、无 E2B、数据落 %APPDATA%\ETHOS
        ETHOS_DESKTOP: "1",
        ETHOS_DATA_DIR: dataDir,
        PYTHONPATH: path.join(backendDir(), "src"),
        PYTHONUNBUFFERED: "1",
        PYTHONIOENCODING: "utf-8",
      },
    },
  );

  await waitForHttp(`http://127.0.0.1:${backendPort}/health`);

  const standaloneDir = webStandaloneDir();
  if (!fs.existsSync(path.join(standaloneDir, "server.js"))) {
    throw new Error(
      `找不到 Next standalone 产物：${path.join(standaloneDir, "server.js")}\n` +
        `请先在 web-v1 下执行 npm run build（桌面版需带 NEXT_PUBLIC_ETHOS_DESKTOP=1）。`,
    );
  }

  web = startProcess(process.execPath, [path.join(standaloneDir, "server.js")], {
    cwd: standaloneDir,
    label: "web",
    env: {
      ...process.env,
      // 让 electron.exe 以 Node 模式运行，避免再依赖一个外部 node 安装
      ELECTRON_RUN_AS_NODE: "1",
      NODE_ENV: "production",
      PORT: String(webPort),
      HOSTNAME: "127.0.0.1",
      ETHOS_DESKTOP: "1",
      NEXT_PUBLIC_ETHOS_DESKTOP: "1",
      EDITH_RUNTIME_URL: `http://127.0.0.1:${backendPort}`,
    },
  });

  const webUrl = `http://127.0.0.1:${webPort}`;
  await waitForHttp(webUrl);
  return webUrl;
}

async function shutdown(): Promise<void> {
  // 先停 web 再停 backend：web 仍可能持有进行中的 SSE 连接，
  // 反过来会让后端在关闭时打印一堆客户端断开错误。
  await web?.stop();
  web = null;
  await backend?.stop();
  backend = null;
}

async function bootstrap(): Promise<void> {
  try {
    const url = await startServices();
    window = createWindow(url);
  } catch (error) {
    dialog.showErrorBox("ETHOS 启动失败", String(error instanceof Error ? error.stack ?? error.message : error));
    await shutdown();
    app.exit(1);
  }
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  // 第二个实例直接退出：后端用的是 SQLite（非 WAL）+ 单实例语义，
  // 两个进程同时启动必然 `database is locked`。
  app.quit();
} else {
  app.on("second-instance", () => {
    if (window) {
      if (window.isMinimized()) window.restore();
      window.focus();
    }
  });

  app.on("window-all-closed", () => {
    app.quit();
  });

  app.on("before-quit", (event) => {
    if (shuttingDown) return;
    shuttingDown = true;
    event.preventDefault();
    void shutdown().finally(() => app.exit(0));
  });

  void app.whenReady().then(bootstrap);
}
