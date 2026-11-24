import { randomBytes } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import { BrowserWindow, Menu, Tray, app, dialog, nativeImage, shell } from "electron";

import { backendDir, pythonExecutable, trayIconPath, webStandaloneDir } from "./paths";
import {
  ChildRegistry,
  findFreePort,
  startProcess,
  waitForHttp,
  type ExitInfo,
  type ManagedProcess,
} from "./process";

// 用户数据目录名与后端 `config.data_root()` 的缺省值保持一致：
// 两边独立推导出同一个路径，就不必再靠一个环境变量去「对齐」。
app.setPath("userData", path.join(app.getPath("appData"), "ETHOS"));

// Windows 靠 AppUserModelID 把窗口、托盘、任务栏归到同一个应用下；
// 不设的话任务栏会显示成一类匿名窗口，固定到任务栏也存不住。
app.setAppUserModelId("com.ethos.desktop");

const registry = new ChildRegistry(path.join(app.getPath("userData"), "runtime", "children.json"));

let backend: ManagedProcess | null = null;
let web: ManagedProcess | null = null;
let window: BrowserWindow | null = null;
let tray: Tray | null = null;
let webUrl = "";
let shuttingDown = false;
/** 服务与窗口都就绪之后才为 true：启动期的失败由 bootstrap 的 catch 统一报，避免双弹窗。 */
let ready = false;

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

  // 关窗 = 收进托盘。
  //
  // 只在托盘真的建起来之后才拦：没有托盘的话窗口一关就再没有入口能把它叫回来，
  // 那会变成一个「进程还在、用户看不见」的幽灵应用。此时放行，让它按原有语义退出。
  win.on("close", (event) => {
    if (!tray) return;
    event.preventDefault();
    win.hide();
  });

  // 外链一律交给系统浏览器：应用窗口没有地址栏，跳出去就回不来。
  win.webContents.setWindowOpenHandler(({ url: target }) => {
    void shell.openExternal(target);
    return { action: "deny" };
  });

  void win.loadURL(url);
  return win;
}

function showWindow(): void {
  // 窗口可能被销毁（渲染进程崩溃、或没有托盘时的退出路径）：
  // 重新按同一个 URL 建一个，而不是对着一个空引用调 show。
  if (!window || window.isDestroyed()) {
    if (!webUrl) return;
    window = createWindow(webUrl);
    return;
  }
  if (window.isMinimized()) window.restore();
  window.show();
  window.focus();
}

function createTray(): Tray | null {
  const iconPath = trayIconPath();
  const icon = nativeImage.createFromPath(iconPath);
  if (icon.isEmpty()) {
    process.stderr.write(`[tray] 图标读取失败：${iconPath}；关窗将直接退出\n`);
    return null;
  }

  const created = new Tray(icon);
  created.setToolTip("ETHOS");
  created.setContextMenu(
    Menu.buildFromTemplate([
      { label: "打开 ETHOS", click: showWindow },
      { type: "separator" },
      { label: "退出", click: () => app.quit() },
    ]),
  );
  created.on("click", showWindow);
  return created;
}

/**
 * 子进程在运行中挂掉时的处理。
 *
 * 托盘常驻让「窗口隐藏但服务已死」成为默认可见状态：不报出来的话，用户会
 * 对着一个看起来正常、实际每次请求都失败的界面。这里明确说清是谁停了、
 * 然后退出——重启是用户能立刻理解并执行的恢复动作，比试图原地拉起后端更可靠
 * （后端重启会让进行中的 SSE 断开、会话状态丢失，还可能再次撞上库锁）。
 */
function reportUnexpectedExit(info: ExitInfo): void {
  if (!ready || shuttingDown) return;
  const name = info.label === "backend" ? "后端服务" : "前端服务";
  const detail = `code=${info.code ?? "?"} signal=${info.signal ?? "?"}`;

  showWindow();
  dialog.showErrorBox(
    `${name}已停止`,
    `${name}意外退出（${detail}），应用将关闭。\n\n` +
      `重新启动即可恢复；若反复出现，请查看终端里以 [${info.label}] 开头的日志。`,
  );

  shuttingDown = true;
  void shutdown().finally(() => app.exit(1));
}

async function startServices(): Promise<string> {
  const dataDir = app.getPath("userData");
  fs.mkdirSync(dataDir, { recursive: true });

  // 上次没有走正常退出（任务管理器强杀、崩溃）时，python / node 子进程会活下来
  // 继续占着 SQLite 锁，这次启动会在建表阶段就 `database is locked`，表现成
  // 「双击没反应」。先按台账清掉——正常退出时台账是空的，这一步什么都不做。
  const reaped = await registry.reapStale();
  for (const record of reaped) {
    process.stderr.write(`[startup] 回收上次遗留的 ${record.label} 进程 pid=${record.pid}\n`);
  }

  const backendPort = await findFreePort();
  const webPort = await findFreePort();

  // Runtime 的入站令牌：每次启动重新生成，只存在于这两个子进程的 env 里。
  // 回环端口同机的任何进程（包括任意网页的跨站请求）都连得上，端口随机只是
  // 遮挡而不是边界；令牌把「连得上」与「被允许调用」分开。不落盘：重启即换。
  const runtimeToken = randomBytes(32).toString("hex");

  // 启动期任何一个子进程挂掉都意味着起不来了：用它中断 waitForHttp 的等待，
  // 不必等满 60 秒超时。失败最终由 bootstrap 的 catch 弹一次框。
  const startupAbort = new AbortController();
  const onChildExit = (info: ExitInfo) => {
    if (!ready) startupAbort.abort();
    reportUnexpectedExit(info);
  };

  const python = pythonExecutable();
  backend = startProcess(
    python,
    ["-m", "uvicorn", "edith_backend.main:app", "--host", "127.0.0.1", "--port", String(backendPort)],
    {
      cwd: backendDir(),
      label: "backend",
      signature: "edith_backend.main:app",
      registry,
      onUnexpectedExit: onChildExit,
      env: {
        ...process.env,
        // 桌面模式：无 E2B、数据落 %APPDATA%\ETHOS。鉴权由随机令牌承担，
        // 令牌只给本进程拉起的 BFF——外部进程拿不到，也猜不出。
        ETHOS_DESKTOP: "1",
        ETHOS_DATA_DIR: dataDir,
        EDITH_RUNTIME_TOKEN: runtimeToken,
        PYTHONPATH: path.join(backendDir(), "src"),
        PYTHONUNBUFFERED: "1",
        PYTHONIOENCODING: "utf-8",
      },
    },
  );

  await waitForHttp(`http://127.0.0.1:${backendPort}/health`, 60_000, startupAbort.signal);

  const standaloneDir = webStandaloneDir();
  if (!fs.existsSync(path.join(standaloneDir, "server.js"))) {
    throw new Error(
      `找不到 Next standalone 产物：${path.join(standaloneDir, "server.js")}\n` +
        `请先执行 desktop 下的构建脚本生成 standalone 产物。`,
    );
  }

  web = startProcess(process.execPath, [path.join(standaloneDir, "server.js")], {
    cwd: standaloneDir,
    label: "web",
    signature: "server.js",
    registry,
    onUnexpectedExit: onChildExit,
    env: {
      ...process.env,
      // 让 electron.exe 以 Node 模式运行，避免再依赖一个外部 node 安装
      ELECTRON_RUN_AS_NODE: "1",
      NODE_ENV: "production",
      PORT: String(webPort),
      HOSTNAME: "127.0.0.1",
      ETHOS_DESKTOP: "1",
      EDITH_RUNTIME_URL: `http://127.0.0.1:${backendPort}`,
      // BFF 用它给每个转发到 Runtime 的请求加 `X-ETHOS-RUNTIME-TOKEN`
      EDITH_RUNTIME_TOKEN: runtimeToken,
    },
  });

  const url = `http://127.0.0.1:${webPort}`;
  await waitForHttp(url, 60_000, startupAbort.signal);
  return url;
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
    webUrl = await startServices();
    window = createWindow(webUrl);
    tray = createTray();
    ready = true;
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
  app.on("second-instance", showWindow);

  // 有托盘时窗口不会真的关闭（close 被拦成 hide），这里只兜「托盘没建起来」的情况：
  // 那种时候窗口一关就再无入口，必须退出。
  app.on("window-all-closed", () => {
    if (!tray) app.quit();
  });

  app.on("activate", showWindow);

  app.on("before-quit", (event) => {
    if (shuttingDown) return;
    shuttingDown = true;
    event.preventDefault();
    void shutdown().finally(() => app.exit(0));
  });

  void app.whenReady().then(bootstrap);
}
