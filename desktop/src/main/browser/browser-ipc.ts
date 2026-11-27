import { ipcMain, type BrowserWindow } from "electron";

import type { BrowserManager } from "./browser-manager";
import type { BrowserBounds, BrowserIpcResult } from "./types";

/**
 * 渲染进程可以调用的方法白名单。
 *
 * 为什么不是「一个方法一个 `ipcMain.handle`」，而是单入口 + method 字段：
 * 桥暴露给页面的能力越宽，越容易在某天被人顺手加一条 `executeJavaScript`。
 * 白名单集中在一处，审查时只看这个集合就能回答「页面到底能干什么」。
 */
const METHODS = new Set([
  "state",
  "tabs.open",
  "tabs.close",
  "tabs.activate",
  "nav.navigate",
  "nav.back",
  "nav.forward",
  "nav.reload",
  "nav.stop",
  "view.set",
]);

/**
 * 用户在面板上的动作。这些一发生就立刻夺回控制权（阶段 6）：
 * Agent 的下一步写操作会收到 `user_took_control`，而不是继续在用户眼前改页面。
 *
 * `state` 与 `view.set` 不在其中：那是面板在报告自己的布局，不是「用户在操作网页」。
 */
const USER_ACTIONS = new Set([
  "tabs.open",
  "tabs.close",
  "tabs.activate",
  "nav.navigate",
  "nav.back",
  "nav.forward",
  "nav.reload",
  "nav.stop",
]);

function ok<T>(result: T): BrowserIpcResult<T> {
  return { ok: true, result };
}

function fail(code: string, message: string): BrowserIpcResult<never> {
  return { ok: false, error: { code, message } };
}

function readBounds(raw: unknown): BrowserBounds | null {
  if (raw === null || raw === undefined) return null;
  if (typeof raw !== "object") return null;
  const value = raw as Record<string, unknown>;
  const keys = ["x", "y", "width", "height"] as const;
  for (const key of keys) {
    if (typeof value[key] !== "number" || !Number.isFinite(value[key])) {
      throw new Error(`视口字段 ${key} 不是有效数字`);
    }
  }
  const bounds: BrowserBounds = {
    x: Math.round(value.x as number),
    y: Math.round(value.y as number),
    width: Math.max(0, Math.round(value.width as number)),
    height: Math.max(0, Math.round(value.height as number)),
  };
  return bounds;
}

function requiredString(raw: unknown, field: string): string {
  if (typeof raw !== "string" || !raw.trim()) throw new Error(`缺少 ${field}`);
  return raw.trim();
}

/**
 * 挂上浏览器 IPC。
 *
 * 两道闸：
 * 1. `event.sender` 必须是主窗口自己的 webContents——浏览器视图与任何 iframe 都不该
 *    能调到这里；
 * 2. `method` 必须在白名单里，参数逐个校验（不当成 any 直接透传）。
 */
export function registerBrowserIpc(win: BrowserWindow, manager: BrowserManager): () => void {
  const send = (channel: string, payload?: unknown): void => {
    if (win.isDestroyed() || win.webContents.isDestroyed()) return;
    win.webContents.send(channel, payload);
  };

  manager.setStateListener((state) => send("browser:state", state));

  ipcMain.handle(
    "browser:command",
    async (event, raw: unknown): Promise<BrowserIpcResult<unknown>> => {
      if (event.sender !== win.webContents) {
        return fail("forbidden", "不允许的调用来源");
      }
      if (!raw || typeof raw !== "object") return fail("invalid_arguments", "命令格式错误");
      const { method, params } = raw as { method?: unknown; params?: unknown };
      if (typeof method !== "string" || !METHODS.has(method)) {
        return fail("unknown_method", `不支持的方法：${String(method)}`);
      }
      const payload = (params ?? {}) as Record<string, unknown>;
      if (USER_ACTIONS.has(method)) manager.userTakeover();

      try {
        switch (method) {
          case "state":
            return ok(manager.state());
          case "tabs.open": {
            const url = typeof payload.url === "string" && payload.url.trim() ? payload.url : undefined;
            return ok(manager.open(url, true));
          }
          case "tabs.close":
            manager.close(requiredString(payload.tabId, "tabId"));
            return ok(manager.state());
          case "tabs.activate":
            manager.activate(requiredString(payload.tabId, "tabId"));
            return ok(manager.state());
          case "nav.navigate":
            return ok(await manager.navigate(requiredString(payload.url, "url")));
          case "nav.back":
            manager.goBack();
            return ok(manager.state());
          case "nav.forward":
            manager.goForward();
            return ok(manager.state());
          case "nav.reload":
            manager.reload();
            return ok(manager.state());
          case "nav.stop":
            manager.stop();
            return ok(manager.state());
          case "view.set":
            manager.setViewport(readBounds(payload.bounds));
            return ok(manager.state());
          default:
            return fail("unknown_method", `不支持的方法：${method}`);
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        return fail("command_failed", message);
      }
    },
  );

  return () => {
    ipcMain.removeHandler("browser:command");
    manager.setStateListener(() => undefined);
  };
}
