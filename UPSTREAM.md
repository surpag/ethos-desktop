# 上游基线

本仓库是 **ETHOS 的独立快照**，用于实现桌面端。它不共享 ETHOS 的 git 历史，
也不回写 ETHOS。两边从此独立演进。

| 项 | 值 |
|---|---|
| 上游仓库 | `D:\agent\EDITH\EDITH` |
| 基线 commit | `c4d112f` — fix(e2b-template): 构建只读仓库根 .env，并收窄构建上下文 |
| 快照日期 | 2026-11-12 |
| 导出方式 | `git archive HEAD backend-py web-v1 docs` |
| 导出文件数 | 375 |

## 为什么是快照而不是 submodule / 软链

「不影响现在的 ETHOS」是硬约束。submodule 与软链都会让两边共享工作树：
在桌面端改一行代码，ETHOS 的未提交状态就被污染了。快照是唯一能做到
「随手改、随便错、上游零感知」的形态。

代价是**上游改进不会自动流下来**。取舍是刻意的：桌面端要做的事（本地沙箱、
删同步、数据目录、认证单点）本身就是**上游没有的分支**，能自动合并的部分很少。

## 已做的偏离

改动都集中在这几处，便于将来对照上游 diff：

| 文件 | 改动 |
|---|---|
| `backend-py/src/edith_backend/config.py` | `project_root()` frozen 感知；新增 `is_desktop()` / `data_root()`；`database_path()` 缺省值改走 `data_root()` |
| `backend-py/src/edith_backend/database.py` | `open_database()` 建父目录（桌面端首次启动 `%APPDATA%\ETHOS` 不存在 → `unable to open database file`） |
| `backend-py/pyproject.toml` | 补 `mcp>=2.0`（上游漏声明，干净环境装完启动即 `ModuleNotFoundError`） |
| `backend-py/src/edith_backend/internal/workspace/volume_backend.py` | `_LOCAL_ROOT` 由相对串改为 `data_root()/workspace-volumes` |
| `backend-py/src/edith_backend/internal/skills/storage/local.py` | 用户级 skill 缺省根目录改为 `data_root()/user-skills` |
| `backend-py/src/edith_backend/internal/skills/storage/project.py` | 项目级 skill 缺省根目录改为 `data_root()/project-skills` |
| `backend-py/src/edith_backend/internal/sandbox/local.py` | **新增**：`LocalSandbox`（11 个原语的本机实现，形状兼容 E2B `AsyncSandbox`）；`workspace/` 目录联接、`/home/user` 双向路径翻译、POSIX shell（Git Bash）+ 显式 PATH、GBK 输出解码 |
| `backend-py/src/edith_backend/internal/sandbox/service.py` | 加 `backend` 参数；`workspace()` / `existing_workspace()` 按后端分派（分歧只在这两处） |
| `backend-py/src/edith_backend/internal/sandbox/tools.py` | `_read_file_slice` 尊重 `supports_large_read`（跳过依赖 `sed`/`wc` 的大文件分支，Windows 上没有这两个命令） |
| `backend-py/src/edith_backend/main.py` | `EDITH_SANDBOX_BACKEND` / `EDITH_WORKSPACE_VOLUME_BACKEND` 在桌面模式下默认 `local` |
| `backend-py/tests/test_local_sandbox.py` | **新增**：32 个用例覆盖 11 个原语、路径边界、shell/文件同视图、路径双向翻译、GBK 解码、Service 层登记 |
| `scripts/e2e_check.py` | **新增**：两轮端到端验收（真实模型 + 真实工具 + 跨 run 持久化） |
| `scripts/probe_local.py` | **新增**：本地沙箱探针（不花模型调用就能验证四条等价性） |
| `web-v1/lib/auth/desktop.ts` | 新增：桌面模式判断（server + client 同构） |
| `web-v1/lib/auth/current-user.ts` | 新增：`currentUserId()`，替代 51 处直接调用 Clerk `auth()` |
| `web-v1/app/api/**`（38 个文件） | `auth()` → `currentUserId()`，机械替换 |
| `web-v1/proxy.ts` | 桌面模式短路，不构造 `clerkMiddleware()` |
| `web-v1/app/layout.tsx` | 桌面模式不包 `ClerkProvider` |
| `web-v1/components/account-menu.tsx` | 桌面模式不渲染 `UserButton` |

以上改动在非桌面模式下**行为不变**（`data_root() == project_root()`，
`isDesktopMode()` 为 false 时全部走原路径）。

## 从上游取后续修复

```bash
# 在 ETHOS 仓库里找到目标 commit，导出对应文件后覆盖本仓库同名路径
cd /d/agent/EDITH/EDITH
git show <commit>:backend-py/src/edith_backend/internal/agentrun/execution.py \
  > /d/agent/ethos-desktop/backend-py/src/edith_backend/internal/agentrun/execution.py
```

注意：直接覆盖会冲掉上表里的偏离，覆盖 `config.py` / `volume_backend.py` /
`skills/storage/{local,project}.py` / web 的 auth 相关文件后需要重新应用。
