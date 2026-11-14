"use client";

import { useEffect, useState } from "react";

type Workspace = {
  workspaceId: string;
  name: string;
  // 项目级执行档位（空 = 未设置，跟随用户级）
  permissionLevel?: string;
};

// 空串在这份列表里是有含义的值（跟随用户设置），不是「缺省」——所以用
// `option.value || "inherit"` 生成 key，避免 React 把空串 key 当成没给。
const OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "跟随用户设置" },
  { value: "read_only", label: "只读" },
  { value: "approval", label: "每次确认" },
  { value: "full", label: "完全" },
];

/**
 * 按项目覆盖执行权限：一段列表，每行一个工作区，改完即生效。
 *
 * 为什么放在「执行权限」区块里、而不是塞进「工作区指令」的编辑器：
 * 档位是「Agent 能对这堆文件做什么」，提问的地方就该和全局档位挨着。
 * 上一版把它埋在工作区编辑器里、还要求先在下拉里选一个工作区才会出现，
 * 结果用户打开设置根本找不到它——一个必须「先选中才有可能看到」的开关，
 * 等于默认不存在。
 *
 * 为什么不做「保存」按钮：一行一个下拉，改完就发请求。多一个保存键会让
 * 「我以为已经生效了」变成一个额外的失败模式，而这里的动作是可逆的单项选择。
 */
export function WorkspacePermissions() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [loaded, setLoaded] = useState(false);
  // 正在保存的工作区 ID（用于禁用该行下拉，避免连点造成两个写请求互抢）
  const [pendingId, setPendingId] = useState("");
  const [message, setMessage] = useState("");

  useEffect(() => {
    async function load() {
      const response = await fetch("/api/workspaces");
      if (!response.ok) {
        setLoaded(true);
        setMessage("无法加载工作区。");
        return;
      }
      const value = (await response.json()) as { workspaces: Workspace[] };
      setWorkspaces(value.workspaces ?? []);
      setLoaded(true);
    }
    void load();
  }, []);

  async function change(workspace: Workspace, level: string) {
    const previous = workspace.permissionLevel ?? "";
    setPendingId(workspace.workspaceId);
    setMessage("");
    // 乐观更新：下拉立刻停在用户选的那一项。失败时回滚——让控件停在
    // 一个没保存成功的值上，比报错更糟：下次打开设置会看到「设过了」。
    setWorkspaces((current) =>
      current.map((item) =>
        item.workspaceId === workspace.workspaceId
          ? { ...item, permissionLevel: level }
          : item,
      ),
    );

    let ok = false;
    try {
      const response = await fetch(
        `/api/workspaces/${encodeURIComponent(workspace.workspaceId)}/permission-level`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ permissionLevel: level }),
        },
      );
      ok = response.ok;
      if (!ok) {
        const body = (await response.json().catch(() => null)) as { message?: string } | null;
        setMessage(body?.message ?? "保存失败，已回滚。");
      }
    } catch {
      setMessage("保存失败，已回滚（运行时不可用）。");
    }

    if (!ok) {
      setWorkspaces((current) =>
        current.map((item) =>
          item.workspaceId === workspace.workspaceId
            ? { ...item, permissionLevel: previous }
            : item,
        ),
      );
    } else {
      setMessage(
        level
          ? `「${workspace.name}」已设为${OPTIONS.find((o) => o.value === level)?.label ?? level}，对之后在这个工作区里的对话生效。`
          : `「${workspace.name}」已恢复为跟随用户设置。`,
      );
    }
    setPendingId("");
  }

  return (
    <div className="border-t border-border px-5 py-4">
      <p className="text-xs font-medium text-ink">按项目覆盖</p>
      <p className="mt-1 text-[11px] leading-5 text-muted">
        有工作区的项目可以单独设一档：
        <strong className="font-medium text-ink">设了就以它为准</strong>
        ，既可以更严（只读），也可以更宽（完全）；选「跟随用户设置」才用上面那一档。
      </p>

      {!loaded ? (
        <p className="mt-3 text-[11px] text-muted">正在读取工作区…</p>
      ) : workspaces.length === 0 ? (
        <p className="mt-3 text-[11px] leading-5 text-muted">
          还没有工作区。在聊天页顶部创建工作区后，这里会出现对应的项目。
        </p>
      ) : (
        <ul className="mt-3 space-y-2">
          {workspaces.map((workspace) => (
            <li
              className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-border bg-surface-subtle px-3 py-2"
              key={workspace.workspaceId}
            >
              <span className="min-w-0 truncate text-xs font-medium text-ink">
                {workspace.name}
              </span>
              <select
                aria-label={`${workspace.name} 的执行权限`}
                className="ui-field h-8 w-36 shrink-0 px-2 text-xs"
                disabled={pendingId === workspace.workspaceId}
                value={workspace.permissionLevel ?? ""}
                onChange={(event) => void change(workspace, event.target.value)}
              >
                {OPTIONS.map((option) => (
                  <option key={option.value || "inherit"} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </li>
          ))}
        </ul>
      )}

      {message && <p className="mt-2 text-[11px] leading-5 text-muted">{message}</p>}
    </div>
  );
}
