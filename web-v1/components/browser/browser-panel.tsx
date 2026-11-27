"use client";

import { MonitorX } from "lucide-react";
import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";

import { useBrowser } from "@/lib/browser/use-browser";

import { BrowserTabs } from "./browser-tabs";
import { BrowserToolbar } from "./browser-toolbar";

/**
 * 内置浏览器面板。
 *
 * 这里最关键的一件事：**Electron 的 WebContentsView 不是 DOM 元素**。
 * React 只渲染一块占位 `div`，把它的 `getBoundingClientRect()` 通过 IPC 交给主进程，
 * 主进程再对原生视图 `setBounds()`。所以：
 *
 * - 不能用 `z-index`、`overflow: hidden`、`transform` 去裁剪它——DOM 管不到原生视图；
 * - 面板一收起（或弹窗打开）就必须把 bounds 置空，否则网页会盖在弹窗上面；
 * - 占位 `div` 的尺寸变化要靠 `ResizeObserver` 上报，不能在 CSS 里「对齐」。
 */
export function BrowserPanel({
  open,
  suspended,
  onClose,
}: {
  open: boolean;
  /** 有模态层（如设置弹窗）打开时为 true：原生视图必须让位，否则会盖住弹窗。 */
  suspended: boolean;
  onClose: () => void;
}) {
  const { state, available, ready, error, call } = useBrowser();
  const [panelWidth, setPanelWidth] = useState(460);
  const surfaceRef = useRef<HTMLDivElement | null>(null);

  const activeTab = state.tabs.find((tab) => tab.tabId === state.activeTabId) ?? null;

  // 首次打开自动开一个标签页：用户点一下图标就应该看到网页，
  // 而不是先看到空白页面再去找「+」。
  useEffect(() => {
    if (!open || !available || !ready) return;
    if (state.tabs.length === 0) void call("tabs.open");
  }, [open, available, ready, state.tabs.length, call]);

  /**
   * 上报视口矩形。
   *
   * 依赖里刻意**没有** `panelWidth`：拖动宽度时每一帧都会改 state，若进依赖会反复重建
   * ResizeObserver。宽度变化本来就会被 observer 捕获，不需要额外触发。
   * 60ms 合并一次，与主进程的下发节流同量级。
   */
  useEffect(() => {
    if (!open) return;
    const element = surfaceRef.current;
    if (!element) return;

    let timer: number | null = null;
    const push = () => {
      const rect = element.getBoundingClientRect();
      if (suspended || rect.width < 2 || rect.height < 2) {
        void call("view.set", { bounds: null });
        return;
      }
      void call("view.set", {
        bounds: {
          x: Math.round(rect.left),
          y: Math.round(rect.top),
          width: Math.round(rect.width),
          height: Math.round(rect.height),
        },
      });
    };
    const schedule = () => {
      if (timer !== null) return;
      timer = window.setTimeout(() => {
        timer = null;
        push();
      }, 60);
    };

    const observer = new ResizeObserver(schedule);
    observer.observe(element);
    window.addEventListener("resize", schedule);
    push();

    return () => {
      observer.disconnect();
      window.removeEventListener("resize", schedule);
      if (timer !== null) window.clearTimeout(timer);
      // 收起时必须把视图摘掉：组件卸载后没人再上报，视图会一直浮在最上层。
      void call("view.set", { bounds: null });
    };
  }, [open, suspended, call]);

  function beginResize(event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = panelWidth;

    function resize(moveEvent: globalThis.PointerEvent) {
      const nextWidth = startWidth + startX - moveEvent.clientX;
      setPanelWidth(Math.min(900, Math.max(360, nextWidth)));
    }
    function finishResize() {
      window.removeEventListener("pointermove", resize);
      window.removeEventListener("pointerup", finishResize);
    }
    window.addEventListener("pointermove", resize);
    window.addEventListener("pointerup", finishResize);
  }

  if (!open) return null;

  return (
    <aside
      aria-label="内置浏览器"
      className="relative flex shrink-0 flex-col border-l border-border bg-surface"
      style={{ width: panelWidth }}
    >
      <div
        aria-label="调整浏览器面板宽度"
        className="absolute inset-y-0 -left-1 z-10 w-2 cursor-col-resize touch-none hover:bg-accent/30"
        onPointerDown={beginResize}
      />

      {available ? (
        <>
          <BrowserTabs
            activeTabId={state.activeTabId}
            onActivate={(tabId) => void call("tabs.activate", { tabId })}
            onClose={(tabId) => void call("tabs.close", { tabId })}
            onNew={() => void call("tabs.open")}
            tabs={state.tabs}
          />
          <BrowserToolbar
            canGoBack={state.canGoBack}
            canGoForward={state.canGoForward}
            loading={Boolean(activeTab?.loading)}
            onBack={() => void call("nav.back")}
            onClose={onClose}
            onForward={() => void call("nav.forward")}
            onNavigate={(url) => void call("nav.navigate", { url })}
            onReload={() => void call("nav.reload")}
            onStop={() => void call("nav.stop")}
            url={activeTab?.url ?? ""}
            agentControlled={state.control.owner === "agent"}
          />
        </>
      ) : null}

      {error ? (
        <p className="shrink-0 border-b border-border bg-danger/10 px-3 py-1.5 text-[11px] text-danger">
          {error}
        </p>
      ) : null}

      {/* 下载被拒、非 http(s) 跳转被拦：必须让用户看到，否则就是「点了没反应」。 */}
      {activeTab?.notice ? (
        <p className="shrink-0 border-b border-border bg-warning/10 px-3 py-1.5 text-[11px] text-warning">
          {activeTab.notice}
        </p>
      ) : null}

      <div className="relative min-h-0 flex-1 bg-canvas" ref={surfaceRef}>
        {!available ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center text-sm text-muted">
            <MonitorX className="size-6" />
            <p>内置浏览器需要在桌面应用中使用。</p>
            <p className="text-xs text-faint">在 ETHOS Desktop 里打开此页面即可。</p>
          </div>
        ) : !activeTab ? (
          <p className="px-4 py-6 text-sm text-muted">正在打开浏览器…</p>
        ) : (
          <p className="pointer-events-none px-4 py-6 text-xs text-faint">
            网页视图由 Electron 覆盖在此区域上方。
          </p>
        )}
      </div>
    </aside>
  );
}
