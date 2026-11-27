import { session, type BrowserWindow, type Session } from "electron";

import { BrowserControl, type AcquireResult } from "./browser-control";
import { BrowserController, type SnapshotResult } from "./browser-controller";
import { BrowserTab } from "./browser-tab";
import { SnapshotError, SnapshotStore } from "./browser-snapshot";
import type { BrowserBounds, BrowserState, BrowserTabState } from "./types";

/**
 * 浏览器分区名。`persist:` 前缀让 Cookie / localStorage 落在磁盘上，
 * 重启应用后登录态还在——不用 persist 的话每次启动都是一个全新访客。
 *
 * 与主窗口（`webPreferences` 未指定 partition，走 default）**必须分开**：
 * 网页与 ETHOS 自己的前端共用分区，等于让任意网页的脚本与本地应用共享 storage 与
 * 进程级状态，这条边界不能为了「省一次会话初始化」而拆掉。
 */
const PARTITION = "persist:ethos-browser";

const HIDDEN_BOUNDS: BrowserBounds = { x: 0, y: 0, width: 0, height: 0 };

/**
 * 全部标签页 + 视图挂载 + 状态下发。
 *
 * 阶段 2 的关键变化：**视口矩形不再由主进程猜**。UI 用一个占位 `div` 量出
 * `getBoundingClientRect()`，通过 IPC 交给这里。原因很实在——原生视图盖在 DOM 之上，
 * 只能靠像素对齐；主进程若按「窗口宽度 40%」自己算，遇到侧栏折叠、面板拖拽、
 * 设置弹窗这些情况必然错位。
 *
 * 主进程只做三件事：保存矩形、把矩形套到当前标签页、把其他标签页压成 0 尺寸。
 */
export class BrowserManager {
  private readonly tabs = new Map<string, BrowserTab>();
  /** 已经 `addChildView` 过的标签页，避免重复挂载导致子视图顺序漂移。 */
  private readonly attached = new Set<string>();
  private readonly snapshots = new SnapshotStore();
  private readonly controller = new BrowserController(this.snapshots);
  /** 控制权归谁：用户动手立即夺回，Agent 之间串行。 */
  private readonly control = new BrowserControl();
  private activeTabId: string | null = null;
  private viewport: BrowserBounds | null = null;
  private listener: ((state: BrowserState) => void) | null = null;
  private notifyTimer: NodeJS.Timeout | null = null;
  private disposed = false;
  private tabSeq = 0;
  private downloadSession: Session | null = null;

  constructor(private readonly win: BrowserWindow) {
    // 托盘隐藏 ≠ 关闭：窗口不可见时把视图也藏起来，恢复时原样放回来，
    // 标签页、滚动位置、Cookie 全都不用重建。
    this.win.on("show", () => this.syncVisibility());
    this.win.on("hide", () => this.syncVisibility());

    // `will-download` 是 session 级事件。每个 tab 都监听会重复触发 N 次，
    // 所以由管理器对着分区 session 注册一次。
    this.downloadSession = session.fromPartition(PARTITION);
    this.downloadSession.on("will-download", this.onWillDownload);
  }

  setStateListener(listener: (state: BrowserState) => void): void {
    this.listener = listener;
  }

  state(): BrowserState {
    const active = this.activeTab();
    return {
      tabs: [...this.tabs.values()].map((tab) => tab.snapshot),
      activeTabId: this.activeTabId,
      canGoBack: active ? active.canGoBack : false,
      canGoForward: active ? active.canGoForward : false,
      visible: this.viewport !== null && this.tabs.size > 0,
      control: this.control.snapshot(),
    };
  }

  get visible(): boolean {
    return this.viewport !== null && this.tabs.size > 0;
  }

  /**
   * 新建标签页。
   *
   * `activate=false` 用于 `window.open`：新页面在后台打开，不打断用户当前正在看的页。
   * 返回 null 只有在窗口已销毁时。
   */
  open(url = "https://example.com", activate = true): BrowserTabState | null {
    if (this.disposed || this.win.isDestroyed()) return null;
    this.tabSeq += 1;
    const tabId = `tab_${this.tabSeq}`;
    const tab = new BrowserTab(
      tabId,
      PARTITION,
      () => this.scheduleNotify(),
      // 同分区的新窗口 → 新标签页；只有拿得到 http(s) 地址才接过来。
      (target) => {
        if (/^https?:/i.test(target)) this.open(target, false);
      },
      // 主文档导航 = 整页换掉了，这个标签页的所有快照立即作废。
      (navigatedTabId) => this.controller.invalidateTab(navigatedTabId),
    );
    this.tabs.set(tabId, tab);

    if (!this.win.isDestroyed()) {
      this.win.contentView.addChildView(tab.view);
      this.attached.add(tabId);
    }

    if (activate || !this.activeTabId) {
      this.activeTabId = tabId;
    }
    this.applyBounds();
    this.syncVisibility();
    this.scheduleNotify();

    void tab.navigate(url).catch(() => {
      // 具体原因由 did-fail-load 落到 lastError 上，这里只吞掉 rejection。
    });
    return tab.snapshot;
  }

  close(tabId: string): void {
    const tab = this.tabs.get(tabId);
    if (!tab) return;
    this.tabs.delete(tabId);
    this.attached.delete(tabId);
    this.controller.invalidateTab(tabId);
    this.detach(tab);
    tab.destroy();

    if (this.activeTabId === tabId) {
      // 关掉当前页后，优先回到它右边那个（浏览器的通行做法）；没有就回到最左。
      const remaining = [...this.tabs.keys()];
      this.activeTabId = remaining[0] ?? null;
    }
    if (this.tabs.size === 0) this.viewport = null;
    this.applyBounds();
    this.syncVisibility();
    this.scheduleNotify();
  }

  activate(tabId: string): void {
    if (!this.tabs.has(tabId) || this.activeTabId === tabId) return;
    this.activeTabId = tabId;
    this.applyBounds();
    this.syncVisibility();
    this.scheduleNotify();
  }

  async navigate(rawUrl: string): Promise<BrowserTabState> {
    const tab = this.activeTab();
    if (!tab) throw new Error("没有打开的标签页");
    return tab.navigate(rawUrl);
  }

  goBack(): void {
    this.activeTab()?.goBack();
    this.scheduleNotify();
  }

  goForward(): void {
    this.activeTab()?.goForward();
    this.scheduleNotify();
  }

  reload(): void {
    this.activeTab()?.reload();
  }

  stop(): void {
    this.activeTab()?.stop();
  }

  // ── 策略拦截（阶段 7）────────────────────────────────────────────────
  //
  // 下载一律拒绝：方案第 5 节把「下载目录」写进了明确不做的清单——内置浏览器是
  // Agent 的操作对象，如果任意网页都能往磁盘写文件，它就变成了一条「网页内容 →
  // 本机文件」的投递通道。拒绝之后必须有可见反馈，否则用户只看到「点了没反应」。
  //
  // 参数用结构类型而不是 Electron 命名空间：这里只用到 preventDefault / 文件名 /
  // webContents.id，结构类型更窄，也让这段逻辑不依赖 Electron 的全局声明。

  private readonly onWillDownload = (
    event: { preventDefault: () => void },
    item: { getFilename: () => string },
    webContents: { id: number },
  ): void => {
    event.preventDefault();
    const filename = describeDownload(item.getFilename());
    const target = [...this.tabs.values()].find(
      (tab) => tab.view.webContents.id === webContents.id,
    );
    target?.setNotice(`已拦截下载：${filename}（内置浏览器不保存文件）`);
  };

  // ── 控制权（阶段 6）──────────────────────────────────────────────────
  //
  // 只由两个入口调用：IPC 的用户命令 → `userTakeover()`；
  // RPC 的页面操作 → `acquireControl(requestId)`。
  // 不放在 navigate/click 这些方法内部，是因为它们同时被两条通路使用：
  // Agent 的导航若也被当成「用户动手」，它会立刻把自己的控制权弄丢。

  userTakeover(): void {
    this.control.userTakeover();
    this.scheduleNotify();
  }

  acquireControl(requestId: string): AcquireResult {
    return this.control.acquire(requestId);
  }

  /** run 收尾时主动交还（`control.release` RPC 入口）。 */
  releaseControl(requestId: string): boolean {
    const released = this.control.release(requestId);
    if (released) this.scheduleNotify();
    return released;
  }

  // ── Agent 侧动作：全部落在「当前选中的标签页」上 ──────────────────────
  //
  // 不让调用方指定 tabId 是刻意的：阶段 6 的控制权模型要求「Agent 操作的就是用户
  // 正在看的那一页」。一旦允许指定后台标签页，用户就完全看不见发生了什么。

  async snapshot(
    options: { maxElements?: unknown; cursor?: unknown } = {},
  ): Promise<SnapshotResult> {
    return this.controller.snapshot(this.requireActive(), options);
  }

  async click(observationId: string, ref: string) {
    return this.controller.click(this.requireActive(), observationId, ref);
  }

  async fill(observationId: string, ref: string, value: string) {
    return this.controller.fill(this.requireActive(), observationId, ref, value);
  }

  async press(observationId: string, ref: string, key: string) {
    return this.controller.press(this.requireActive(), observationId, ref, key);
  }

  async scroll(direction: string, amount: number) {
    return this.controller.scroll(this.requireActive(), direction, amount);
  }

  /**
   * 设置视图矩形；传 `null` 表示面板收起（隐藏视图但**不销毁**标签页）。
   *
   * 隐藏与销毁是两件事：收起面板后用户的登录态、滚动位置、页面里填了一半的表单都要
   * 保留，再打开是瞬间的；销毁会把这些全丢掉。
   */
  setViewport(bounds: BrowserBounds | null): void {
    if (this.disposed) return;
    this.viewport = bounds;
    if (bounds) this.ensureAttached();
    this.applyBounds();
    this.syncVisibility();
    this.scheduleNotify();
  }

  /** 退出路径：先摘视图再关 webContents，顺序反了会留下一个已销毁对象的监听。 */
  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    if (this.notifyTimer) {
      clearTimeout(this.notifyTimer);
      this.notifyTimer = null;
    }
    for (const tab of this.tabs.values()) {
      this.detach(tab);
      tab.destroy();
    }
    this.downloadSession?.removeListener("will-download", this.onWillDownload);
    this.downloadSession = null;
    this.tabs.clear();
    this.attached.clear();
    this.controller.clear();
    this.control.reset();
    this.activeTabId = null;
    this.viewport = null;
    this.listener = null;
  }

  private activeTab(): BrowserTab | null {
    return this.activeTabId ? (this.tabs.get(this.activeTabId) ?? null) : null;
  }

  private requireActive(): BrowserTab {
    const tab = this.activeTab();
    if (!tab) {
      throw new SnapshotError(
        "no_active_tab",
        "当前没有打开的标签页：请让用户在浏览器面板里打开一个页面后再操作",
      );
    }
    return tab;
  }

  private ensureAttached(): void {
    if (this.win.isDestroyed()) return;
    for (const [tabId, tab] of this.tabs) {
      if (this.attached.has(tabId)) continue;
      this.win.contentView.addChildView(tab.view);
      this.attached.add(tabId);
    }
  }

  private detach(tab: BrowserTab): void {
    if (this.win.isDestroyed()) return;
    try {
      this.win.contentView.removeChildView(tab.view);
    } catch {
      // 视图可能已经随窗口一起销毁，忽略。
    }
  }

  /**
   * 非当前标签页一律给 0 尺寸：WebContentsView 不能靠 z-index 隐藏，
   * 叠在一起时后加的那个在上，容易出现「关掉一个还剩一个」的错觉。
   */
  private applyBounds(): void {
    if (this.disposed || this.win.isDestroyed()) return;
    const viewport = this.viewport;
    for (const [tabId, tab] of this.tabs) {
      const active = tabId === this.activeTabId && viewport !== null;
      tab.view.setBounds(active && viewport ? viewport : HIDDEN_BOUNDS);
    }
  }

  private syncVisibility(): void {
    if (this.disposed) return;
    const windowVisible = !this.win.isDestroyed() && this.win.isVisible();
    for (const [tabId, tab] of this.tabs) {
      const shown = this.visible && windowVisible && tabId === this.activeTabId;
      tab.setVisible(shown);
    }
  }

  /**
   * 事件合并：一次导航会连续打出 start-loading / title / did-navigate 三四个事件，
   * 逐个推给渲染进程会让 React 连着重渲染好几轮（工具栏闪烁）。30ms 合并一次足够。
   */
  private scheduleNotify(): void {
    if (this.disposed || !this.listener || this.notifyTimer) return;
    this.notifyTimer = setTimeout(() => {
      this.notifyTimer = null;
      if (!this.disposed && this.listener) this.listener(this.state());
    }, 30);
  }
}

/** 下载提示里只放文件名：去掉路径与控制字符，避免把整条 URL 带进 UI。 */
function describeDownload(raw: string): string {
  const name = (raw || "").split(/[\\/]/).pop() || "";
  const cleaned = name.replace(/[\u0000-\u001f\u007f]/g, "").trim().slice(0, 80);
  return cleaned || "未命名文件";
}
