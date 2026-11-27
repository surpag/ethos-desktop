"use client";

import {
  ArrowLeft,
  ArrowRight,
  PanelRightClose,
  RotateCw,
  ShieldAlert,
  X,
} from "lucide-react";
import { useEffect, useState } from "react";

import { BrowserControlStatus } from "./browser-control-status";

/**
 * 导航工具栏：前进/后退/刷新（或停止）+ 地址栏 + 控制权 + 收起。
 *
 * 地址栏是**受控但可编辑**的：页面导航后要跟着变（用户看得到自己在哪），
 * 但用户正在输入时不能被后台上报的 URL 覆盖（输入到一半跳光标是最恼人的体验之一）。
 * 所以用「聚焦时不同步」这一条规则解决，而不是加一堆 dirty 标记。
 */
export function BrowserToolbar({
  url,
  loading,
  canGoBack,
  canGoForward,
  onNavigate,
  onBack,
  onForward,
  onReload,
  onStop,
  onClose,
  agentControlled,
}: {
  url: string;
  loading: boolean;
  canGoBack: boolean;
  canGoForward: boolean;
  onNavigate: (url: string) => void;
  onBack: () => void;
  onForward: () => void;
  onReload: () => void;
  onStop: () => void;
  onClose: () => void;
  /** 阶段 6：控制权归某个 Agent run 时为 true。 */
  agentControlled: boolean;
}) {
  const [draft, setDraft] = useState(url);
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    if (!editing) setDraft(url);
  }, [url, editing]);

  return (
    <div className="flex h-11 shrink-0 items-center gap-1 border-b border-border px-1.5">
      <button
        aria-label="后退"
        className="ui-icon-button disabled:opacity-35"
        disabled={!canGoBack}
        onClick={onBack}
        title="后退"
        type="button"
      >
        <ArrowLeft className="size-4" />
      </button>
      <button
        aria-label="前进"
        className="ui-icon-button disabled:opacity-35"
        disabled={!canGoForward}
        onClick={onForward}
        title="前进"
        type="button"
      >
        <ArrowRight className="size-4" />
      </button>
      <button
        aria-label={loading ? "停止加载" : "刷新"}
        className="ui-icon-button"
        onClick={loading ? onStop : onReload}
        title={loading ? "停止加载" : "刷新"}
        type="button"
      >
        {loading ? <X className="size-4" /> : <RotateCw className="size-4" />}
      </button>

      <form
        className="mx-1 flex min-w-0 flex-1 items-center gap-1.5 rounded-md border border-border bg-surface-subtle px-2"
        onSubmit={(event) => {
          event.preventDefault();
          const target = draft.trim();
          if (!target) return;
          setEditing(false);
          onNavigate(target);
        }}
      >
        <ShieldAlert className="size-3 shrink-0 text-faint" aria-hidden />
        <input
          aria-label="地址栏"
          className="h-7 min-w-0 flex-1 bg-transparent text-xs text-ink outline-none placeholder:text-faint"
          onBlur={() => setEditing(false)}
          onChange={(event) => setDraft(event.target.value)}
          onFocus={() => setEditing(true)}
          placeholder="输入地址后回车"
          spellCheck={false}
          value={draft}
        />
      </form>

      <BrowserControlStatus agentControlled={agentControlled} />
      <button
        aria-label="收起浏览器"
        className="ui-icon-button"
        onClick={onClose}
        title="收起浏览器"
        type="button"
      >
        <PanelRightClose className="size-4" />
      </button>
    </div>
  );
}
