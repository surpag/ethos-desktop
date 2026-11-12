#!/usr/bin/env node
/**
 * 启动 Electron 主进程。
 *
 * 为什么要多这一层，而不是 `electron .`：
 * 某些宿主环境（CI、IDE 插件、部分终端）会残留 `ELECTRON_RUN_AS_NODE=1`。
 * 那样 electron.exe 会以**纯 Node 模式**运行，`require("electron")` 返回的是
 * npm 包的路径字符串而不是 API 对象，主进程第一行 `app.isPackaged` 就崩：
 *   TypeError: Cannot read properties of undefined (reading 'isPackaged')
 * 这个变量只在进程启动时被读取，运行期 `delete process.env` 无效，
 * 必须在 spawn 之前从环境里摘掉。
 */
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

import electronPath from "electron";

const here = path.dirname(fileURLToPath(import.meta.url));
const appRoot = path.resolve(here, "..");

const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;

const child = spawn(electronPath, [appRoot], {
  stdio: "inherit",
  env,
  windowsHide: false,
});

child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  else process.exit(code ?? 0);
});
