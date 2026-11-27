/**
 * 与 Electron preload 桥的约定。
 *
 * 这两个类型必须与 `desktop/src/main/browser/types.ts` 保持一致——那一边是权威定义。
 * 之所以不共享一份：`web-v1` 与 `desktop` 是两个独立的 tsconfig/构建单元，
 * 为了一个类型去建 workspace 间的路径映射，会把前端构建耦合到 Electron 的类型解析上。
 * 代价是改字段要同时改两处，阶段 4 接 Python 时会加一条契约测试盯住。
 */

export type BrowserTabState = {
  tabId: string;
  url: string;
  title: string;
  loading: boolean;
  crashed: boolean;
  lastError: string;
  /** 策略拦截提示（下载、非 http(s) 导航等）。 */
  notice: string;
};

export type BrowserControlState = {
  owner: "user" | "agent";
  /** 持有控制权的 Agent run；用户控制时为 null。 */
  requestId: string | null;
};

export type BrowserState = {
  tabs: BrowserTabState[];
  activeTabId: string | null;
  canGoBack: boolean;
  canGoForward: boolean;
  visible: boolean;
  control: BrowserControlState;
};

export type BrowserCommandError = { code: string; message: string };

type RawResult = { ok: true; result: unknown } | { ok: false; error: BrowserCommandError };

export interface BrowserBridge {
  command(method: string, params?: Record<string, unknown>): Promise<RawResult>;
  onState(listener: (state: BrowserState) => void): () => void;
  onToggleRequest(listener: () => void): () => void;
}

export const EMPTY_BROWSER_STATE: BrowserState = {
  tabs: [],
  activeTabId: null,
  canGoBack: false,
  canGoForward: false,
  visible: false,
  control: { owner: "user", requestId: null },
};

/** 非桌面环境（浏览器里跑 `next dev`）时为 null，UI 据此降级而不是崩。 */
export function getBrowserBridge(): BrowserBridge | null {
  if (typeof window === "undefined") return null;
  const bridge = (window as unknown as { ethosBrowser?: BrowserBridge }).ethosBrowser;
  return bridge ?? null;
}

/**
 * 调用浏览器命令并拆掉 `{ok, error}` 外壳。
 *
 * 失败统一抛 `Error`（message 带上 code），调用方只需 `try/catch`；
 * 不把 `{ok:false}` 当正常返回值往上传，是因为 UI 里每处都判断一次 `ok` 极易漏。
 */
export async function browserCommand<T = unknown>(
  method: string,
  params?: Record<string, unknown>,
): Promise<T> {
  const bridge = getBrowserBridge();
  if (!bridge) throw new Error("内置浏览器需要在桌面应用中使用");
  const response = await bridge.command(method, params);
  if (!response.ok) {
    const error = new Error(`${response.error.message}（${response.error.code}）`);
    throw error;
  }
  return response.result as T;
}
