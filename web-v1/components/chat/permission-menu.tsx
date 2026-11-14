"use client";

import { ShieldCheck } from "lucide-react";
import { useEffect, useRef, useState } from "react";

type Level = "read_only" | "approval" | "full";

const LABELS: Record<Level, string> = {
  read_only: "只读",
  approval: "确认",
  full: "完全",
};

const DESCRIPTIONS: Record<Level, string> = {
  read_only: "只能读；写文件与执行命令都会被拒。",
  approval: "每次写入或执行前，一次一次问你。",
  full: "不再确认，直接读写与执行。",
};

// 三档的视觉分量刻意和危险程度对齐：只读最安全、完全最危险。
const STYLES: Record<Level, string> = {
  read_only: "border-success/40 bg-success-soft text-success",
  approval: "border-warning/40 bg-warning-soft text-warning",
  full: "border-danger/40 bg-danger-soft text-danger",
};

const LEVELS: Level[] = ["read_only", "approval", "full"];

function isLevel(value: unknown): value is Level {
  return value === "read_only" || value === "approval" || value === "full";
}

type PermissionMenuProps = {
  // 当前会话绑定的工作区；null = 普通对话，档位由全局设置决定
  workspaceID: string | null;
  workspaceName?: string;
  disabled?: boolean;
};

/**
 * 输入框工具栏里的执行档位菜单。
 *
 * 语义只有一个：**改这一轮对话实际会用的那一档**。
 *   - 有工作区 → 改这个工作区的档位（覆盖全局）；
 *   - 没有工作区 → 改全局档位。
 * 菜单底部会写明落点（「作用于工作区『米核』」/「作用于全局」），因为同一个
 * 下拉落在两个地方，不写清楚就会出现「我在这里点了一下，为什么全局变了」。
 *
 * 为什么不放进设置页当唯一入口：档位是「这一轮我要它多谨慎」的临场判断，
 * 和「选哪个模型」同一类。要求用户为此打开设置、滚到第三块、再关掉，
 * 他会干脆一直用完全档——那才是真正的安全损失。
 */
export function PermissionMenu({
  workspaceID,
  workspaceName,
  disabled = false,
}: PermissionMenuProps) {
  const [open, setOpen] = useState(false);
  // null = 还没读到（不要用 "full" 当初始值：读失败时界面会显示一个并不成立的档位）
  const [userLevel, setUserLevel] = useState<Level | null>(null);
  // 空串 = 工作区没单独设（跟随用户级）
  const [workspaceLevel, setWorkspaceLevel] = useState<Level | "">("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const menu = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      const [settingsResponse, workspacesResponse] = await Promise.all([
        fetch("/api/settings"),
        fetch("/api/workspaces"),
      ]);
      if (cancelled) return;

      if (settingsResponse.ok) {
        const settings = (await settingsResponse.json()) as { permissionLevel?: unknown };
        if (!cancelled && isLevel(settings.permissionLevel)) {
          setUserLevel(settings.permissionLevel);
        }
      }

      if (workspacesResponse.ok) {
        const value = (await workspacesResponse.json()) as {
          workspaces?: { workspaceId: string; permissionLevel?: string }[];
        };
        if (cancelled) return;
        const match = value.workspaces?.find((item) => item.workspaceId === workspaceID);
        setWorkspaceLevel(isLevel(match?.permissionLevel) ? match.permissionLevel : "");
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, [workspaceID]);

  useEffect(() => {
    function onOutsideClick(event: MouseEvent) {
      if (!menu.current?.contains(event.target as Node)) setOpen(false);
    }
    function onEscape(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onOutsideClick);
    document.addEventListener("keydown", onEscape);
    return () => {
      document.removeEventListener("mousedown", onOutsideClick);
      document.removeEventListener("keydown", onEscape);
    };
  }, []);

  // 生效值：工作区设了就盖住全局。定时任务的授权快照只压定时任务，
  // 不参与这里的展示（聊天页不是定时任务）。
  const effective: Level | null = workspaceID
    ? workspaceLevel || userLevel
    : userLevel;
  const fromWorkspace = Boolean(workspaceID && workspaceLevel);
  const currentValue: Level | "" = fromWorkspace ? workspaceLevel : "";

  async function choose(level: Level | "") {
    if (saving) return;
    const previous = currentValue;
    setSaving(true);
    setError("");
    // 乐观更新：下拉立刻反映选择；失败再回滚。
    if (workspaceID) {
      setWorkspaceLevel(level);
    } else if (level) {
      setUserLevel(level);
    }

    try {
      const response = await fetch("/api/permission-level", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          permissionLevel: level,
          ...(workspaceID ? { workspaceId: workspaceID } : {}),
        }),
      });
      if (!response.ok) {
        const body = (await response.json().catch(() => null)) as { message?: string } | null;
        setError(body?.message ?? "保存失败，已回滚。");
        if (workspaceID) setWorkspaceLevel(previous);
        else if (effective) setUserLevel(effective);
        return;
      }
      setOpen(false);
    } catch {
      setError("保存失败，已回滚（运行时不可用）。");
      if (workspaceID) setWorkspaceLevel(previous);
      else if (effective) setUserLevel(effective);
    } finally {
      setSaving(false);
    }
  }

  const label = effective ? LABELS[effective] : "权限";
  const style = effective ? STYLES[effective] : "border-border bg-surface-subtle text-muted";

  return (
    <div className="relative" ref={menu}>
      <button
        aria-expanded={open}
        aria-haspopup="menu"
        className={`flex h-7 items-center gap-1 rounded-md border px-2 text-xs font-medium transition-colors hover:brightness-95 disabled:cursor-not-allowed disabled:opacity-60 ${style}`}
        disabled={disabled || saving}
        onClick={() => setOpen((current) => !current)}
        title={
          effective
            ? `执行权限：${LABELS[effective]}${fromWorkspace ? `（工作区「${workspaceName ?? workspaceID}」覆盖全局）` : ""}`
            : "正在读取执行权限…"
        }
        type="button"
      >
        <ShieldCheck className="size-3.5" />
        <span>{saving ? "保存中" : label}</span>
      </button>

      {open && (
        <div
          className="absolute bottom-10 left-0 z-20 w-72 overflow-hidden rounded-xl border border-border bg-surface shadow-xl shadow-black/10"
          role="menu"
        >
          <div className="border-b border-border px-3 py-2.5">
            <p className="text-sm font-medium text-ink">执行权限</p>
            <p className="mt-0.5 text-xs leading-4 text-muted">
              {workspaceID
                ? `作用于工作区「${workspaceName ?? workspaceID}」里的对话；不设则跟随全局。`
                : "作用于所有对话与定时任务（没有工作区时由它决定）。"}
            </p>
          </div>

          <div className="p-1.5">
            {workspaceID && (
              <button
                aria-checked={!fromWorkspace}
                className={`flex w-full items-start gap-2 rounded-lg border px-2.5 py-2 text-left transition-colors ${
                  fromWorkspace
                    ? "border-transparent text-muted hover:bg-surface-subtle"
                    : "border-accent-soft bg-accent-soft text-accent"
                }`}
                onClick={() => void choose("")}
                role="menuitemradio"
                type="button"
              >
                <span className="min-w-0 flex-1">
                  <span className="block text-xs font-medium">跟随用户设置</span>
                  <span className="mt-0.5 block text-[11px] leading-4 text-muted">
                    全局现在是「{userLevel ? LABELS[userLevel] : "读取中"}」，这个工作区不单独设。
                  </span>
                </span>
                {!fromWorkspace && <span className="text-accent">✓</span>}
              </button>
            )}

            {LEVELS.map((level) => {
              const selected = workspaceID ? currentValue === level : effective === level;
              return (
                <button
                  aria-checked={selected}
                  className={`flex w-full items-start gap-2 rounded-lg border px-2.5 py-2 text-left transition-colors ${
                    selected
                      ? "border-accent-soft bg-accent-soft text-accent"
                      : "border-transparent text-muted hover:bg-surface-subtle hover:text-ink"
                  }`}
                  key={level}
                  onClick={() => void choose(level)}
                  role="menuitemradio"
                  type="button"
                >
                  <span className="min-w-0 flex-1">
                    <span className="block text-xs font-medium">{LABELS[level]}</span>
                    <span className="mt-0.5 block text-[11px] leading-4 text-muted">
                      {DESCRIPTIONS[level]}
                    </span>
                  </span>
                  {selected && <span className="text-accent">✓</span>}
                </button>
              );
            })}
          </div>

          {error && (
            <p className="border-t border-border px-3 py-2 text-[11px] leading-4 text-danger">
              {error}
            </p>
          )}
          <p className="border-t border-border px-3 py-2 text-[11px] leading-4 text-muted">
            敏感路径（.git、.ssh、密钥）在任何档位下都不可读写。
          </p>
        </div>
      )}
    </div>
  );
}
