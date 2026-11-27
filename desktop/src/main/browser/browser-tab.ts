import { WebContentsView } from "electron";

import { SnapshotError } from "./browser-snapshot";
import type { BrowserTabState } from "./types";

/** 阶段 2 只允许正常网页协议；`file:` / `javascript:` / `data:` 一律不开放给导航。 */
const ALLOWED_PROTOCOLS = new Set(["http:", "https:"]);

/**
 * 把用户/模型给的地址归一化成可加载的 URL。
 *
 * 两件事必须在这里做完，不能留给上层：
 * 1. 没有写协议时补 `https://`（否则 `new URL("example.com")` 直接抛错，
 *    用户看到的是一句莫名的 "Invalid URL"）；
 * 2. 协议白名单。`javascript:` 与 `data:` 能在页面上下文里执行任意脚本，
 *    `file:` 能读到本机任意文件——它们都不是「浏览器功能」，而是提权通道。
 */
export function normalizeUrl(raw: string): string {
  const text = (raw ?? "").trim();
  if (!text) throw new Error("地址为空");
  const hasScheme = /^[a-z][a-z0-9+.-]*:/i.test(text);
  const candidate = hasScheme ? text : `https://${text}`;

  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    throw new Error(`无法解析地址：${text}`);
  }
  if (!ALLOWED_PROTOCOLS.has(parsed.protocol)) {
    throw new Error(`不允许的协议：${parsed.protocol}（只支持 http / https）`);
  }
  return parsed.toString();
}

/**
 * 单个标签页：一个 `WebContentsView` + 它的可观测状态。
 *
 * 为什么状态要在这里维护而不是让 UI 去问 `webContents`：主进程的 webContents 在
 * 崩溃 / 销毁后调用会抛错，UI 侧也不该拿得到 webContents。这里把事件收成一份
 * 只读快照，IPC 与后续 RPC 都读这份快照。
 *
 * 阶段 2 的边界：不做 preload 注入（网页里拿不到任何 ETHOS 能力），下载走 Electron
 * 默认处理（阶段 7 再接管），`window.open` 交给上层的 `onNewWindow` 决定。
 */
export class BrowserTab {
  readonly view: WebContentsView;

  private state: BrowserTabState;
  private disposed = false;
  /**
   * 主文档导航的序号。快照会记住它，操作前比对——「取完快照之后页面跳走了」
   * 是模型点错元素最常见的原因，而这个信息只有页面自己知道。
   */
  private _pageVersion = 0;
  /**
   * 渲染进程是否崩过且还没被主动导航重置。
   *
   * 不能用 `state.crashed`：崩溃后展示的错误页会触发 `did-finish-load`，那里
   * 本来就会把 `crashed` 清回 false。这个标记必须活到用户/Agent 重新导航。
   */
  private rendererGone = false;

  constructor(
    readonly tabId: string,
    partition: string,
    private readonly onChange: () => void,
    private readonly onNewWindow?: (url: string) => void,
    private readonly onNavigate?: (tabId: string) => void,
  ) {
    this.view = new WebContentsView({
      webPreferences: {
        partition,
        // 三条都是「网页是外部内容」的最低要求：不注入 Node、不共享上下文、
        // 渲染进程跑在 Chromium 自己的 sandbox 里。
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        // 页面在后台时不要节流定时器。Agent 点击后常依赖页面自己的 setTimeout
        // 完成跳转/渲染；被节流到 1Hz 甚至更慢后，「点完等结果」会莫名其妙地
        // 超时，而用户眼前明明已经变了。
        backgroundThrottling: false,
      },
    });
    this.view.setBackgroundColor("#0b0b0c");
    this.state = {
      tabId,
      url: "",
      title: "",
      loading: false,
      crashed: false,
      lastError: "",
      notice: "",
    };

    const wc = this.view.webContents;

    // 同窗口打开的新页面收进自己的标签页，而不是交出去。
    // 交给 `shell.openExternal` 会把「网页里的一个链接」变成「桌面上打开任意 URL」，
    // 而且用户的浏览上下文就断了（新页面在系统浏览器里，Agent 完全看不到）。
    wc.setWindowOpenHandler(({ url: target }) => {
      if (!/^https?:/i.test(target)) {
        this.patch({
          notice: `已拦截非网页窗口：${describeUrl(target)}（只允许 http/https）`,
        });
        return { action: "deny" };
      }
      if (this.onNewWindow) this.onNewWindow(target);
      return { action: "deny" };
    });

    // 页面自己发起的导航也要过协议白名单。
    // `normalizeUrl()` 只管用户在地址栏输入的地址；页面里的 `location.href =
    // "file:///C:/..."` 或 `data:` 链接走的是另一条路，不拦就能读到本机文件。
    wc.on("will-navigate", (event, target) => {
      if (/^https?:/i.test(target)) return;
      event.preventDefault();
      this.patch({
        notice: `已拦截不安全的跳转：${describeUrl(target)}（只允许 http/https）`,
      });
    });

    wc.on("did-start-loading", () => {
      // 崩溃后加载的是内部错误页：不能把「渲染进程退出」这条错误清掉，
      // 否则 UI 只剩一个空页面，用户完全不知道发生了什么。
      this.patch(this.rendererGone ? { loading: true } : { loading: true, lastError: "" });
    });
    wc.on("did-stop-loading", () => {
      this.patch({ loading: false });
    });
    wc.on("did-finish-load", () => {
      if (this.rendererGone) {
        // 错误页的地址是 data:，覆盖 state.url 会让「崩的是哪一页」彻底丢失。
        this.patch({ loading: false });
        return;
      }
      this.patch({ url: wc.getURL(), lastError: "", crashed: false });
    });
    wc.on("page-title-updated", (_event, title) => {
      this.patch({ title });
    });
    wc.on("did-navigate", (_event, url) => {
      this._pageVersion += 1;
      this.onNavigate?.(this.tabId);
      // 成功导航后旧提示就过期了：用户已经去了别处，横幅还挂着只会造成误解。
      // 崩溃后的错误页是 data: 地址，不覆盖原 URL（重试需要知道崩的是哪一页）。
      this.patch(this.rendererGone ? { notice: "" } : { url, notice: "" });
    });
    wc.on("did-navigate-in-page", (_event, url) => {
      this.patch({ url });
    });
    // 加载失败不抛给主进程：主窗口不能因为一个网页打不开就崩。
    // 错误信息留给 UI 判断，页面换成本地错误页，保证视图不是一片空白。
    wc.on("did-fail-load", (_event, errorCode, errorDescription, validatedURL, isMainFrame) => {
      if (!isMainFrame) return;
      if (errorCode === -3) return; // ERR_ABORTED：被新的导航打断，不是失败
      this.patch({
        loading: false,
        crashed: false,
        lastError: `${errorCode} ${errorDescription}`,
        url: validatedURL,
      });
      this.showErrorPage(validatedURL, `${errorDescription}（${errorCode}）`);
    });
    wc.on("render-process-gone", (_event, details) => {
      // 独立字段而非常规 state：错误页会触发 did-finish-load，那里会把
      // `crashed` 清成 false；但「这个标签页的渲染进程已经崩过」这件事必须
      // 保留到用户/Agent 主动重新导航为止。
      this.rendererGone = true;
      this.patch({ crashed: true, loading: false, lastError: `渲染进程退出：${details.reason}` });
      this.showErrorPage(this.state.url, `页面崩溃：${details.reason}`);
    });

    this.onChange();
  }

  get snapshot(): BrowserTabState {
    return { ...this.state };
  }

  get alive(): boolean {
    return !this.disposed && !this.view.webContents.isDestroyed();
  }

  get pageVersion(): number {
    return this._pageVersion;
  }

  /** 策略拦截提示（下载等由管理器统一裁决的事件）。 */
  setNotice(message: string): void {
    this.patch({ notice: message });
  }

  /**
   * 在页面上下文里执行一段**平台自带的**固定脚本，返回结构化结果。
   *
   * 这是唯一能把脚本送进页面的入口，脚本内容全部来自 `browser-page-scripts.ts`
   * 的常量；调用方只能通过参数（`ref`、`key` 等）影响它，不能拼脚本。
   */
  async runScript<T>(script: string): Promise<T> {
    if (this.disposed) throw new Error("标签页已销毁");
    // 崩溃后错误页是可以执行脚本的（它就是一个 data: 页面），直接跑只会返回
    // 0 个元素——模型会以为「页面是空的」，而不是「页面崩了」。
    if (this.rendererGone) {
      throw new SnapshotError(
        "page_crashed",
        "页面渲染进程已崩溃，当前显示的是错误页；请用 browser_open_url 重新打开目标页面",
      );
    }
    return (await this.view.webContents.executeJavaScript(script, true)) as T;
  }

  get canGoBack(): boolean {
    if (!this.alive) return false;
    return this.view.webContents.navigationHistory.canGoBack();
  }

  get canGoForward(): boolean {
    if (!this.alive) return false;
    return this.view.webContents.navigationHistory.canGoForward();
  }

  async navigate(rawUrl: string): Promise<BrowserTabState> {
    if (this.disposed) throw new Error("标签页已销毁");
    const url = normalizeUrl(rawUrl);
    // 主动导航是崩溃后唯一的恢复动作：从这里清掉标记与崩溃提示。
    this.rendererGone = false;
    this.patch({ lastError: "", crashed: false });
    await this.view.webContents.loadURL(url);
    return this.snapshot;
  }

  reload(): void {
    if (this.disposed) return;
    this.view.webContents.reload();
  }

  stop(): void {
    if (this.disposed) return;
    this.view.webContents.stop();
  }

  goBack(): void {
    if (!this.canGoBack) return;
    this.view.webContents.navigationHistory.goBack();
  }

  goForward(): void {
    if (!this.canGoForward) return;
    this.view.webContents.navigationHistory.goForward();
  }

  setVisible(visible: boolean): void {
    if (this.disposed) return;
    this.view.setVisible(visible);
  }

  destroy(): void {
    if (this.disposed) return;
    this.disposed = true;
    // close() 之后 webContents 上再调用任何方法都会抛 "Object has been destroyed"，
    // 所以先置 disposed，再关。
    this.view.webContents.close();
  }

  /** 内部错误页：只用固定模板 + 纯文本转义，不拼接任意 HTML。 */
  private showErrorPage(url: string, message: string): void {
    if (this.disposed || this.view.webContents.isDestroyed()) return;
    const safeUrl = escapeHtml(url || "(未知地址)");
    const safeMessage = escapeHtml(message);
    const html =
      `<html><head><meta charset="utf-8"><title>无法加载页面</title></head>` +
      `<body style="margin:0;padding:32px;font:14px/1.6 system-ui;background:#0b0b0c;color:#e6e6e6">` +
      `<h2 style="margin:0 0 12px;font-size:16px">无法加载页面</h2>` +
      `<p style="margin:0 0 8px;color:#9a9a9a">${safeUrl}</p>` +
      `<p style="margin:0;color:#c25b5b">${safeMessage}</p>` +
      `<p style="margin:16px 0 0;color:#7a7a7a">检查网络后重试，或换一个地址。</p>` +
      `</body></html>`;
    void this.view.webContents.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
  }

  private patch(next: Partial<BrowserTabState>): void {
    if (this.disposed) return;
    this.state = { ...this.state, ...next };
    this.onChange();
  }
}

/** 提示里只放可读的短地址：完整 URL（尤其 file:// 长路径）会撑爆横幅。 */
function describeUrl(raw: string): string {
  const text = (raw || "").replace(/[\u0000-\u001f\u007f]/g, "").trim();
  if (!text) return "(空地址)";
  return text.length > 80 ? `${text.slice(0, 77)}...` : text;
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
