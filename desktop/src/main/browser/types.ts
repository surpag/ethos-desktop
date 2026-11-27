/** 浏览器模块内部类型。阶段 2：多标签页 + 由渲染进程提供的视口矩形。 */

/** 视图矩形，坐标系是**窗口内容区**左上角（CSS px = DIP，zoomFactor=1 时等价）。 */
export interface BrowserBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * 一个标签页对外暴露的状态。
 *
 * 只放 UI 真会用到、并且主进程能可靠维护的字段：URL 与标题来自 webContents 事件，
 * `crashed` 来自 `render-process-gone`，`lastError` 来自 `did-fail-load`。
 */
export interface BrowserTabState {
  tabId: string;
  url: string;
  title: string;
  loading: boolean;
  crashed: boolean;
  /** 加载失败（网络错误、证书、渲染进程退出）。 */
  lastError: string;
  /**
   * 策略拦截提示（下载、非 http(s) 导航、被拒绝的新窗口）。
   *
   * 与 `lastError` 分开：下载被拒时页面并没有加载失败，若复用 `lastError`
   * 会被下一次 `did-start-loading` 立刻清掉，用户什么都看不到。
   */
  notice: string;
}

/** 推给渲染进程的整份状态（单一下发源，避免 UI 自己拼）。 */
export interface BrowserState {
  tabs: BrowserTabState[];
  activeTabId: string | null;
  canGoBack: boolean;
  canGoForward: boolean;
  /** 视图是否真的挂在窗口上（面板打开且拿到了非空视口）。 */
  visible: boolean;
  /** 当前控制权归属（阶段 6）：用户或某个 Agent run。 */
  control: { owner: "user" | "agent"; requestId: string | null };
}

/** IPC 命令的统一返回外壳。 */
export type BrowserIpcResult<T> =
  | { ok: true; result: T }
  | { ok: false; error: { code: string; message: string } };
