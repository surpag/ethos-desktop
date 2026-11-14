"use client";

import { Check, ChevronDown, Shield } from "lucide-react";
import { useEffect, useRef, useState } from "react";

// 本会话的档位**上限**。空串 = 不额外收紧。
const OPTIONS: { value: string; label: string; hint: string }[] = [
  { value: "", label: "不限制", hint: "跟随全局与工作区设置" },
  { value: "read_only", label: "只读", hint: "只看文件与仓库状态，不写、不执行" },
  { value: "approval", label: "每次确认", hint: "写入或执行前停下来等你点确认" },
  { value: "full", label: "完全", hint: "本会话不额外收紧（仍受敏感路径保护）" },
];

const SHORT: Record<string, string> = {
  read_only: "只读",
  approval: "确认",
  full: "完全",
  "": "默认",
};

const BINDING_LABEL: Record<string, string> = {
  user: "你的全局设置",
  workspace: "这个工作区的上限",
  session: "本会话的上限",
};

type Props = {
  sessionID: string;
  /** 本会话设置的上限（空 = 未设置） */
  setting: string;
  /** 实际生效档位（已与用户级/工作区级取最严） */
  effective: string;
  /** 把档位压下来的那一级 */
  binding: string;
  onChange: (next: {
    permissionLevel: string;
    effectivePermissionLevel: string;
    permissionBinding: string;
  }) => void;
};

// 聊天页顶部：显示「这个对话实际按几档跑」，并允许把它收得更严。
//
// 为什么按钮显示**生效值**而不是用户在这个下拉里选的值：两者可能不同
// （用户级只读 + 会话选完全 → 实际只读）。显示选中值等于骗人，用户会以为
// Agent 坏了；显示生效值再配一句解释，他才知道该去哪里改。
export function PermissionSwitcher({
  sessionID,
  setting,
  effective,
  binding,
  onChange,
}: Props) {
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onDocClick(event: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, []);

  async function choose(value: string) {
    if (saving) return;
    setSaving(true);
    setMessage("");
    try {
      const response = await fetch(
        `/api/conversations/${encodeURIComponent(sessionID)}/permission-level`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ permissionLevel: value }),
        },
      );
      if (!response.ok) {
        setMessage("保存失败，请重试。");
        return;
      }
      const body = (await response.json()) as {
        permissionLevel: string;
        effectivePermissionLevel: string;
        permissionBinding: string;
      };
      // 不做乐观更新：服务端会把生效值算回来（可能被更严的一级压住），
      // 界面直接采用它，避免出现「显示完全、实际只读」这种自说自话。
      onChange(body);
      setOpen(false);
    } finally {
      setSaving(false);
    }
  }

  // 被更严的一级压住了：只有设置值与生效值不同时才提示，否则每一屏都在喊。
  const pressed = setting !== "" && effective !== "" && setting !== effective;

  return (
    <div className="relative" ref={menuRef}>
      <button
        className="flex h-7 items-center gap-1.5 rounded-lg border border-border bg-surface px-2.5 text-[11px] font-medium text-ink transition-colors hover:bg-surface-subtle"
        onClick={() => setOpen((current) => !current)}
        title={
          pressed
            ? `本会话设为「${SHORT[setting] ?? setting}」，实际按「${SHORT[effective] ?? effective}」运行`
            : `本会话执行权限：${SHORT[effective] ?? effective}`
        }
        type="button"
      >
        <Shield className={`size-3.5 ${pressed ? "text-warning" : "text-accent"}`} />
        {SHORT[effective] ?? effective}
        {pressed && <span className="text-warning">·</span>}
        <ChevronDown className="size-3 text-muted" />
      </button>

      {open && (
        <div className="absolute right-0 top-9 z-30 min-w-64 rounded-xl border border-border bg-surface p-1.5 shadow-lg">
          <p className="px-2.5 py-1.5 text-[11px] leading-4 text-muted">
            这是<strong className="text-ink">上限</strong>：只能让本对话更严，
            不能越过你的全局设置。
          </p>
          {OPTIONS.map((option) => {
            const selected = setting === option.value;
            return (
              <button
                className="flex w-full items-start justify-between gap-2 rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-surface-subtle"
                disabled={saving}
                key={option.value}
                onClick={() => void choose(option.value)}
                type="button"
              >
                <span className="flex min-w-0 flex-col">
                  <span className="text-xs font-medium text-ink">{option.label}</span>
                  <span className="mt-0.5 text-[11px] leading-4 text-muted">
                    {option.hint}
                  </span>
                </span>
                {selected && <Check className="mt-0.5 size-3.5 shrink-0 text-accent" />}
              </button>
            );
          })}
          {pressed && (
            <p className="mt-1 border-t border-border px-2.5 pt-2 text-[11px] leading-4 text-warning">
              实际按「{SHORT[effective] ?? effective}」运行：
              {BINDING_LABEL[binding] ?? "更严的一级设置"}比本会话更严。
              要放宽请先改那一级。
            </p>
          )}
          {message && (
            <p className="mt-1 px-2.5 text-[11px] leading-4 text-danger">{message}</p>
          )}
        </div>
      )}
    </div>
  );
}
