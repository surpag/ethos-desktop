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

偏离已经大到「逐文件对照」不再有意义（删掉的能力就有数千行），所以按**主题**
列，而不是按文件：

### 一、删掉的上游能力（不可回退）

| 能力 | 上游做法 | 现在 | 提交 |
| --- | --- | --- | --- |
| 远端沙箱 | E2B Sandbox + Volume 挂载 | 本机目录（`sandbox/local.py`），`workspace/{id}` 与 `skills/` 用目录联接指向权威副本 | `0846507` |
| 工作区同步 | 双副本 push/pull + 三道护栏 + 熔断 + 删除传播 | 不存在：磁盘上只有一份文件 | `0846507` |
| 认证 | Clerk + 双模开关 | 固定 `local-user` | `c575b17` |
| 图片存储 | 腾讯云 COS 预签名直传 | 本机磁盘 + 模型侧 data URL | `20d9ece` |
| 渠道绑定 | `user_channel_bindings` 映射外部账号 | 渠道白名单（web / cron），未知渠道拒绝 | `95a46c9` |
| Skill 作用域 | platform/user/project 三层 + 裁决 + 冲突快照 + 迁移工具 | 单层目录（`<data_root>/skills`），只保留清单预算与加载预算 | `4335b48` |

### 二、保留结构、换了实现

| 位置 | 改动 |
| --- | --- |
| `config.py` | `project_root()` frozen 感知；新增 `is_desktop()` / `data_root()`；`database_path()` 缺省走 `data_root()` |
| `database.py` | `open_database()` 建父目录（首次启动 `%APPDATA%\ETHOS` 不存在 → `unable to open database file`） |
| `sandbox/local.py` | **新增**：本机实现（文件/命令原语）、目录联接、`/home/user` 双向路径翻译、Git Bash + 显式 PATH、GBK 解码 |
| `sandbox/service.py` | 只收 `(db, workspaces, workspace_volumes)`，没有后端分派 |
| `images/` | `cos.py` → `storage.py`；新增 `PUT /internal/images/{id}/blob`；`object_key` 只取末段（老记录无需迁移） |
| `workspace/volume_backend.py` | 本地根目录改为 `data_root()/workspace-volumes` |
| `skills/` | `catalog.py` / `instruction.py` / `budget.py` 重写为单层；删 `resolver.py`、`storage/`、`migrate.py` |
| `gateway/service.py` | 去掉 `bindings` 依赖；`_is_trusted_channel` → 白名单常量 |
| `cronjob/` | `clerk_user_id` → `user_id`（含 `ALTER TABLE RENAME COLUMN` 迁移） |
| `main.py` | 装配收敛：无 E2B / COS / Volume 服务，`new_gateway_service(agent_runs)` |
| `pyproject.toml` | 删 `e2b`、`cos-python-sdk-v5`；补 `mcp>=2.0`（上游漏声明） |
| `web-v1/lib/auth/current-user.ts` | **新增**：`currentUserId()` 替代 51 处直接调用 Clerk `auth()`；现在返回常量 |
| `web-v1/app/api/**` | `auth()` → `currentUserId()`，机械替换 |
| `web-v1/app/(authenticated)`、`proxy.ts`、`sign-in`、`sign-up`、`account-menu` | 删除 |
| `backend-py/tests/test_local_sandbox.py`、`tests/test_images.py`、`tests/test_skills.py` | **新增/重写**：覆盖本地沙箱原语、图片生命周期、Skill 单层与预算 |
| `scripts/e2e_check.py`、`scripts/probe_local.py` | **新增**：端到端验收与本地沙箱探针 |

## 从上游取后续修复

```bash
# 在 ETHOS 仓库里找到目标 commit，导出对应文件后覆盖本仓库同名路径
cd /d/agent/EDITH/EDITH
git show <commit>:backend-py/src/edith_backend/internal/agentrun/execution.py \
  > /d/agent/ethos-desktop/backend-py/src/edith_backend/internal/agentrun/execution.py
```

注意：直接覆盖会冲掉上面的偏离。**更容易踩的坑是拿上游文件替代「本仓库已经
删掉的东西的调用方」**——例如覆盖 `main.py` 会把 E2B / COS 的装配重新带回来，
而它们依赖的模块在这里已经不存在。移植单个修复时优先只取被修的**函数体**，
不要整文件覆盖 `main.py` / `sandbox/service.py` / `gateway/service.py` /
`images/*` / `skills/*`。
