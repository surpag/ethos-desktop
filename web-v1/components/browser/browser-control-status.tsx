"use client";

import { Bot, MousePointerClick } from "lucide-react";

/**
 * 当前标签页的控制权归属。
 *
 * 阶段 6 起由主进程下发（`BrowserState.control`）：用户任何操作都会立刻夺回，
 * Agent 的写入操作会在租约内串行，界面只需如实显示当前归属。
 */
export function BrowserControlStatus({ agentControlled }: { agentControlled: boolean }) {
  return (
    <span
      className={`flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium ${
        agentControlled
          ? "border-accent/40 bg-accent-soft text-accent"
          : "border-border bg-surface-subtle text-muted"
      }`}
      title={agentControlled ? "Agent 正在操作此标签页" : "你在操作此标签页"}
    >
      {agentControlled ? <Bot className="size-3" /> : <MousePointerClick className="size-3" />}
      {agentControlled ? "Agent 控制" : "用户控制"}
    </span>
  );
}
