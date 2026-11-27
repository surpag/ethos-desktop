"use client";

import { LoaderCircle, Plus, X } from "lucide-react";

import type { BrowserTabState } from "@/lib/browser/bridge";

/** 标签页条。标题为空时回落到 URL，再回落到「新标签页」。 */
export function BrowserTabs({
  tabs,
  activeTabId,
  onActivate,
  onClose,
  onNew,
}: {
  tabs: BrowserTabState[];
  activeTabId: string | null;
  onActivate: (tabId: string) => void;
  onClose: (tabId: string) => void;
  onNew: () => void;
}) {
  return (
    <div className="flex h-9 shrink-0 items-center gap-1 overflow-x-auto border-b border-border px-1.5">
      {tabs.map((tab) => {
        const active = tab.tabId === activeTabId;
        const label = tab.title || tab.url || "新标签页";
        return (
          <div
            key={tab.tabId}
            className={`group flex h-7 max-w-44 shrink-0 items-center gap-1.5 rounded-md px-2 text-xs ${
              active
                ? "bg-surface-subtle text-ink"
                : "text-muted hover:bg-surface-subtle/60 hover:text-ink"
            }`}
          >
            <button
              className="flex min-w-0 items-center gap-1.5"
              onClick={() => onActivate(tab.tabId)}
              title={label}
              type="button"
            >
              {tab.loading ? (
                <LoaderCircle className="size-3 shrink-0 animate-spin" />
              ) : (
                <span
                  className={`size-1.5 shrink-0 rounded-full ${
                    tab.crashed ? "bg-danger" : active ? "bg-accent" : "bg-faint"
                  }`}
                />
              )}
              <span className="truncate">{label}</span>
            </button>
            <button
              aria-label={`关闭 ${label}`}
              className="shrink-0 rounded p-0.5 text-faint opacity-0 transition-opacity hover:bg-surface-hover hover:text-ink group-hover:opacity-100"
              onClick={() => onClose(tab.tabId)}
              type="button"
            >
              <X className="size-3" />
            </button>
          </div>
        );
      })}
      <button
        aria-label="新建标签页"
        className="flex size-7 shrink-0 items-center justify-center rounded-md text-muted hover:bg-surface-subtle hover:text-ink"
        onClick={onNew}
        title="新建标签页"
        type="button"
      >
        <Plus className="size-3.5" />
      </button>
    </div>
  );
}
