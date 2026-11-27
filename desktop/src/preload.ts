import { contextBridge, ipcRenderer, type IpcRendererEvent } from "electron";

/**
 * 主窗口与 Electron Main 之间唯一的桥。
 *
 * 三条原则，别为了「少写一个方法」破坏它们：
 * 1. **不暴露通用 `invoke`**：只暴露 `command(method, params)`，方法名由主进程白名单
 *    校验。暴露通用 invoke 等于把整个 ipcMain 面交给页面。
 * 2. **不暴露 `require` / `process` / 任何 Node 对象**：`contextIsolation` 已经隔离了
 *    上下文，这里只过去纯函数。
 * 3. **返回值只走结构化数据**：不传 Error 实例、不传函数、不传 Buffer，
 *    跨桥序列化会静默降级成 `{}`，排查起来很浪费时间。
 *
 * 注意：这份 preload 只挂在**主窗口**上。浏览器标签页的 webContents 明确不注入任何
 * preload —— 网页拿不到这里的任何东西。
 */

type BrowserCommandResult =
  | { ok: true; result: unknown }
  | { ok: false; error: { code: string; message: string } };

const browserApi = {
  command(method: string, params?: Record<string, unknown>): Promise<BrowserCommandResult> {
    return ipcRenderer.invoke("browser:command", { method, params });
  },
  /** 订阅主进程下发的整份浏览器状态；返回取消订阅函数。 */
  onState(listener: (state: unknown) => void): () => void {
    const wrapped = (_event: IpcRendererEvent, state: unknown) => listener(state);
    ipcRenderer.on("browser:state", wrapped);
    return () => {
      ipcRenderer.removeListener("browser:state", wrapped);
    };
  },
  /** 窗口内快捷键（Ctrl+Shift+B）要求切换面板，由 React 决定显隐。 */
  onToggleRequest(listener: () => void): () => void {
    const wrapped = () => listener();
    ipcRenderer.on("browser:toggle-request", wrapped);
    return () => {
      ipcRenderer.removeListener("browser:toggle-request", wrapped);
    };
  },
};

contextBridge.exposeInMainWorld("ethosBrowser", browserApi);
