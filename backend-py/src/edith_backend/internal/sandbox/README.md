# sandbox 模块 —— 本机沙箱：目录、命令与工具

## 第一部分：模块名称与简介

| 项 | 内容 |
| --- | --- |
| 模块名 | `sandbox` |
| 一句话定位 | 按「用户 + 会话」隔离的**本机目录**：懒创建、虚拟路径解析、文件原语、命令执行，以及暴露给 Agent 的 `sandbox_*` 工具集 |
| 实现路径 | `src/edith_backend/internal/sandbox/` |

**没有远端执行后端。** 桌面版的沙箱就是磁盘上的一个目录：
`workspace/{id}` 与 `skills/` 都是**目录联接（junction）**，直指权威副本
（`<data_root>/workspace-volumes/{id}` 与 `<data_root>/skills`）。因此磁盘上只有
一份文件——凡是要在「沙箱副本」和「权威副本」之间搬运、比对、熔断的设计，在这里
都没有存在前提（那些代码已经删除）。

## 文件清单

| 文件 | 职责 |
| --- | --- |
| `local.py` | `LocalSandbox`（根目录 + 生命周期）、`LocalFiles`（读写/列表/移动/删除）、`LocalCommands`（执行/后台进程/路径翻译） |
| `service.py` | `Service`：会话 → 沙箱映射（`user_sandboxes` 表）、只读打开、`workspace/{id}` 的本地路径 |
| `http.py` | 文件面板的 4 条路由：列表 / 内容 / 上传 / 下载 |
| `tools.py` | `SandboxToolSet`：`sandbox_*` 工具的声明与执行 |
| `agent_input.py` | `AgentInput.validate_uploads`：校验本次运行可引用的上传文件（限 `uploads/`、限当前会话） |
| `workspace.py` | 路径安全校验（`workspace/{id}` 段绑定、白名单）与 `WORKSPACE` 布局 re-export |
| `schema.py` | `user_sandboxes` 表 |
| `types.py` | `FileType`（纯 Enum）、`CommandResult`、`EntryInfo`、`CommandInfo` |

## 第二部分：架构设计

### ① 分层

| 层 | 文件 | 说明 |
| --- | --- | --- |
| HTTP 边界 | `http.py` | 参数校验、异常 → `EDITHError`（404 / 403） |
| 服务 | `service.py` | 会话映射与「打开 / 只读打开」策略 |
| 沙箱实现 | `local.py` | 文件系统与进程；虚拟路径 → 真实路径的**唯一**入口 |
| 工具适配 | `tools.py` | 把沙箱能力包装成模型可调用的工具 |

### ② 虚拟路径与联接

模型看到的是虚拟路径（`/home/user/work/a.txt` 或 `work/a.txt`），`resolve()` 是
唯一的翻译点，越界立即抛 `SandboxPathError`：

| 虚拟路径 | 真实落点 | 建联时机 |
| --- | --- | --- |
| `work/` `uploads/` `artifacts/` | `<sandbox_root>/{name}` | `open()` 时平铺创建 |
| `skills/` | `<data_root>/skills` | `open()` 时建联接 |
| `workspace/{id}/…` | `<data_root>/workspace-volumes/{id}` | **首次解析时**按需建联接 |

`workspace/{id}` 的联接刻意不在 `open()` 时建：会话可以换绑工作区，预先按当前
`workspace_id` 建一次，换绑后会留下指向旧工作区的联接。

`run_command` 是真实进程，只认文件系统，所以命令里的路径要在执行前翻译成真实
路径（`LocalCommands._translate`），结束后再把输出里的真实路径还原成虚拟路径
——否则模型会拿到 `D:\...\workspace-volumes\...` 这种它自己无法再次传入的字符串。

### ③ 关键类

| 名称 | 职责 |
| --- | --- |
| `LocalSandbox.open(user, session)` | 打开或**重建**（目录被外部删掉时直接重建，代价为零） |
| `LocalSandbox.attach(...)` | 只打开已存在的；不存在抛 `FileNotFoundError` |
| `LocalSandbox.kill()` | 只结束后台进程，**不删目录**（工作区文件是用户的） |
| `LocalFiles.read/write/list/make_dir/remove/rename/get_info` | 文件原语；`remove` 对目录递归 |
| `LocalCommands.run(...)` | 执行命令，返回 `CommandResult`（`exitCode` 为 camelCase） |
| `LocalCommands.start/list/kill` | 后台进程登记（pid → 子进程），进程退出后自动摘除 |

## 第三部分：与其他模块的联系

| 依赖方 | 用途 |
| --- | --- |
| `agentrun` | 通过 `SandboxToolSet` 让模型读写文件、跑命令 |
| `workspace` | `workspace/{id}` 指向 `WorkspaceVolumeService` 管理的本地目录 |
| `skills` | `skills/` 联接到 `<data_root>/skills`，与 `SkillsCatalog` 读的是同一份 |
| 组合根 | `SandboxService(db, workspaces, workspace_volumes)` + `include_router` |

## 第四部分：边界与已知缺口

- **没有进程级隔离**：命令以宿主用户身份运行，`resolve()` 管的是路径而不是权限。
  隔离边界是「本机单用户」，不是「容器」。
- **没有出口网络限制**：沙箱里的命令能访问任何网络地址（`netguard.py` 只约束
  Agent 工具发起的 HTTP，不约束 shell）。
- **`user_sandboxes.sandbox_id` 是派生值**（`local-{user}-{session}`）：它只用来让
  面板与 `existing_workspace` 判断「这个会话有没有沙箱」，不代表任何远端资源。
- **后台进程不跨重启**：`_background` 是进程内字典，后端退出后 orphan 进程仍在，
  但列表里看不到。
