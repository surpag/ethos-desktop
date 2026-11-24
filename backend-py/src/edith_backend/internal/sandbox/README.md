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
| `LocalSandbox.kill()` | 结束**本会话**的全部后台进程，**不删目录**（工作区文件是用户的） |
| `LocalFiles.read/write/list/make_dir/remove/rename/get_info` | 文件原语；`remove` 对目录递归 |
| `LocalCommands.run(...)` | 执行命令，返回 `CommandResult`（`exitCode` 为 camelCase） |
| `LocalCommands.run/list/kill`（后台） | 经 `processes` 台账登记（pid + 元数据，**跨实例共享**）；只允许结束本会话登记的 pid |

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
- **后台进程的生命周期是会话，不是 run**：`sandbox_start_process` 起的进程在对话
  结束后继续跑（`dev server` 这类用法就是靠它）。删会话与后端正常退出时按归属回收。
- **异常退出靠落盘登记恢复**：后端被强杀时 `lifespan` 不执行，登记写在
  `<data_root>/sandbox/background-processes.json`。下次启动 `processes.recover()`
  读回，只有「创建时刻 + 镜像路径」都对得上才终结；拿不到证据的保留为
  `suspected`（日志 warning + 审计行），**不杀**。台账本身只是本进程的内存表，
  不要把它描述成"跨进程共享注册表"。
- **登记失败必须回滚**：`register` 落盘失败会抛 `PersistenceFailed`，工具层据此
  终结刚启动的进程树、等待退出，再向模型返回 `background_registration_failed`。
  终结失败时写 `severity=high` 的审计（pid / 创建时刻 / 镜像 / 命令 / 错误）。
- **终结必须走核验绑定路径**：后台进程一律 `terminate_verified(pid, create_time=...,
  image=...)`——它在同一个进程句柄（Windows）/ `pidfd`（Linux）上再次复核身份并终结，
  消除"核验之后 pid 被复用"的窗口。`terminate_tree(pid)` 只留给前台命令的超时/取消。
- **多实例写入**：`process_store.save(..., instance_id=...)` 只替换本实例的条目；
  删除或修改**其他实例**的遗留记录必须显式传 `drop_pids` / `patch_pids`，否则合并写
  会把它们当成"不是我的，别动"。
- **遗留记录的人工处置**：`edith-processes inspect`（只读）/ `retry <pid>`
  （重新核验，只有确认归属才终结）/ `acknowledge <pid> --reason ...`（暂不处理：
  不杀、不删记录与审计，启动时不再告警）。**没有 `kill` 子命令是刻意的**——
  人工按 pid 直接杀会绕过归属校验。
