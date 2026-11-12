"use client";

import { UserButton } from "@clerk/nextjs";
import { Settings } from "lucide-react";

import { isDesktopMode } from "@/lib/auth/desktop";

import { ThemeToggle } from "./theme-toggle";

type AccountMenuProps = {
  onOpenSettings: () => void;
};

// Clerk owns account actions in cloud mode. ETHOS only adds its own settings entry.
// 桌面端没有账号体系：显示本机标识，不渲染 UserButton（它依赖 ClerkProvider）。
export function AccountMenu({ onOpenSettings }: AccountMenuProps) {
  const desktop = isDesktopMode();

  return (
    <div className="flex items-center justify-between rounded-xl px-2 py-1 hover:bg-surface-subtle">
      <div className="min-w-0 flex-1 truncate">
        {desktop ? (
          <span className="text-sm text-muted" title="桌面端为单用户模式">
            本机
          </span>
        ) : (
          <UserButton showName userProfileMode="modal" />
        )}
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <ThemeToggle />
        <button aria-label="ETHOS 设置" className="ui-icon-button shrink-0" onClick={onOpenSettings} title="ETHOS 设置" type="button"><Settings className="size-4" /></button>
      </div>
    </div>
  );
}
