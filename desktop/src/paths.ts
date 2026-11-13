import fs from "node:fs";
import path from "node:path";

import { app } from "electron";

/** 未打包（开发 / 直接 electron .）时为 true。 */
export const isDev = !app.isPackaged;

/**
 * 应用包根目录。
 *
 * - 开发：仓库根（desktop/ 的上一级），backend-py 与 web-v1 都是源码目录。
 * - 打包：`resources/app`，由 electron-builder 的 extraResources 放进去。
 *
 * 刻意不用 `app.getAppPath()`：打包后它指向 asar 内部，而 Python 与 Node
 * 子进程都无法从 asar 里读取文件。
 */
export function bundleRoot(): string {
  return app.isPackaged
    ? path.join(process.resourcesPath, "app")
    : path.resolve(__dirname, "..", "..");
}

export function backendDir(): string {
  return path.join(bundleRoot(), "backend-py");
}

export function webDir(): string {
  return path.join(bundleRoot(), "web-v1");
}

/** Next standalone 产物的入口目录（server.js 与 .next 同级）。 */
export function webStandaloneDir(): string {
  return path.join(webDir(), ".next", "standalone");
}

/**
 * 托盘图标。
 *
 * 直接复用前端的品牌图标（`web-v1/app/favicon.ico`，多尺寸 ico）：托盘需要
 * 16×16，任务栏需要 32×32，分开维护两份图早晚会不一致。读不到时调用方
 * 必须退回「关窗即退出」，否则窗口一关就再没有入口能把它叫回来。
 */
export function trayIconPath(): string {
  return path.join(webDir(), "app", "favicon.ico");
}

/**
 * Python 解释器。
 *
 * 开发期用 backend-py 自己的 venv；打包后由 extraResources 带一份嵌入式
 * Python（`python/python.exe`）。找不到就抛错——静默回退到系统 python
 * 会让用户机器上莫名其妙的解释器/依赖版本参与进来。
 */
export function pythonExecutable(): string {
  const exe = process.platform === "win32" ? "python.exe" : "python";
  const candidates = app.isPackaged
    ? [path.join(process.resourcesPath, "python", exe)]
    : [path.join(backendDir(), ".venv", "Scripts", exe), path.join(backendDir(), ".venv", "bin", "python")];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  throw new Error(
    `找不到 Python 解释器。已尝试：\n${candidates.join("\n")}\n` +
      `开发期请先在 backend-py 下创建 .venv。`,
  );
}
