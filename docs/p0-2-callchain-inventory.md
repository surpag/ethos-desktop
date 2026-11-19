# P0-2 只读核验：绑定目录在三条执行链上的真实落点

**基线**：`9fdff49`（P0-1 提交）。**本文只记录事实**，不含改造方案。
状态标签：`[已实测]` / `[源码已确认]` / `[待验证]`。

---

## 0. 结论先行

绑定本地目录后，**同一个虚拟路径 `/home/user/workspace/{id}` 对应两份内容**：

| 消费者 | 实际落点 | 证据 |
| --- | --- | --- |
| 文件工具（6 个 `sandbox_*`） | `<data_root>/workspace-volumes/{id}`（空目录） | `[已实测]` |
| Shell / `run_command` | 同上（经 `{root}/workspace` 联接） | `[已实测]` |
| Git（`git` / `git_push`） | 同上 | `[已实测]` |
| 文件面板 / 工作区上传 | **用户绑定的目录** | `[已实测]` |

即：**执行链三者彼此一致，但整体与用户目录分裂**。P0-1 文档里写的「绑定后文件工具与 shell 分叉」需要更正——它们不分叉，一起偏离了绑定目录。

**在未配置 `EDITH_WORKSPACE_VOLUME_DIR` 的默认运行条件下**，未绑定工作区不存在这个问题：此时「注册表 A」与「硬编码 B」指向同一个目录。

该前提不能省略——`volume_backend` 认这个环境变量（`volume_backend.py:27-32`），沙箱层三处硬编码 `data_root()`。一旦配置它，**未绑定工作区同样分裂**（见 F-5）。目前它是休眠配置：全仓仅 `volume_backend.py:27` 读取，文档、测试、启动脚本均未提及。

---

## 1. 实测证据

探针脚本：临时 `ETHOS_DATA_DIR`，真实 `LocalVolumeBackend` + 真实 `LocalSandbox`，绑定一个含 `seed.txt` 的临时目录。

```
注册表 local_root_of = ...\bound

1. files.list("/home/user/workspace/ws-bound")        → 列出: []            ← 看不到 seed.txt
2. files.write(.../from-tool.txt)                     → 绑定目录: False | volume: True
   workspace-volumes/ws-bound 是联接? False | 是目录? True                  ← 普通空目录，不是联接
3. commands.run("pwd; ls", cwd=.../ws-bound)          → /home/user/workspace/ws-bound | from-tool.txt
4. commands.run("echo shell > from-shell.txt")        → 绑定目录: False | volume: True
5. commands.run("git init -q . && git status ...")    → .git 落绑定目录: False | 落 volume: True
6. backend.resolve_workspace_path("workspace/ws-bound/seed.txt") → ...\bound\seed.txt
7. {root}/workspace 联接 → ...\data\workspace-volumes                     ← 挂载本身是健康的
```

第 7 行说明：**这不是挂载损坏**。P0-1 的自检会正确判定「联接指向预期目标」并复用——因为预期目标就是 `workspace-volumes`。缺的是「`{id}` 这一级如何指向绑定目录」的约定。

---

## 2. 三条链的完整调用链

### 2.1 文件工具

```
模型调用 sandbox_read_file / write / list / make_directory / move / delete
  └─ tools.py:533        name in _FILE_TOOLS（tools.py:73-76，共 6 个）
     └─ tools.py:535     _complete_arguments → _complete_workspace_path（tools.py:121）
                         相对路径自动补成 workspace/{id}/…（模型不拼 ID）
     └─ tools.py:537     _scope_violation（tools.py:138）          跨工作区硬拒
     └─ tools.py:567     _sensitive_path_violation（参数层）
     └─ tools.py:570     _sensitive_real_violation（`tools.py:199` 用
                         `os.path.realpath(sandbox.resolve(...))` 再判一次 → 同样落 volume）
     └─ tools.py:577     _sensitive_subtree_violation（仅 delete，递归展开）
  └─ _op_sandbox_xxx     → workspace_path()（sandbox/workspace.py:47，纯字符串）
     └─ sandbox.files.read/write/list/...（local.py:787-868）
        └─ self._sb.resolve(path)                    ← 唯一的落点决定点
           └─ local.py:608-614  以 workspace/ 开头 → _resolve_workspace_volume
              └─ local.py:629  base = _workspace_volume_dir(workspace_id)
                 └─ local.py:718   volumes = _ensure_workspace_mount()
                    └─ local.py:714   volumes = data_root() / "workspace-volumes"   ← 硬编码
                 └─ local.py:731   volume.mkdir(parents=True, exist_ok=True)        ← 只 mkdir
                 └─ local.py:732   return volume.resolve()
              └─ local.py:641  target = (base / normalized).resolve()
                 └─ local.py:642  is_relative_to(base) 越界必拒
```

关键事实：`_workspace_volume_dir`（`local.py:718-732`）**只建目录，不建联接**。注册表里记的绑定目录在这一层完全没有出现。

### 2.2 Shell / `run_command`

```
模型调用 sandbox_run_command
  └─ tools.py:836   _op_sandbox_run_command
     └─ tools.py:845  显式拦截 program 为 git（引导去 git 工具，非硬边界）
     └─ tools.py:859  sandbox.commands.run(_join_argv(...), envs=..., timeout=...)
                       ← 不传 cwd，用默认值
  └─ local.py:891   LocalCommands.run()
     └─ local.py:900  work_dir = resolve(cwd or WORKSPACE.work_path())   （默认 /home/user/work）
     └─ local.py:901  work_dir.mkdir(parents=True, exist_ok=True)
     └─ local.py:914  command = _translate(cmd)
        └─ local.py:977  /home/user → _shell_path_of(self._sb.root)      ← 换成**沙箱根**
     └─ local.py:925  _spawn(..., cwd=work_dir, env=...)                 ← 真实进程
     └─ local.py:941  stdout/stderr 经 _normalize
        └─ local.py:965  _path_pairs：data_root()/workspace-volumes → /home/user/workspace
```

所以 shell 到达工作区的路径是**联接链**，不是绑定目录：

```
<sandbox>/workspace        ── junction ──▶ <data_root>/workspace-volumes
<data_root>/workspace-volumes/{id}   ← 普通空目录（绑定后依旧如此）
```

`_translate` 只翻译到沙箱根这一级；`{id}` 之后的部分交给文件系统。绑定目录在这条链上**没有任何入口**。

### 2.3 Git（`git` 工具 / `git_push`）

```
模型调用 git
  └─ tools.py:908   _op_git
     └─ tools.py:916  workspace_id 为空 → 直接拒绝
     └─ tools.py:921  root = WORKSPACE.workspace_path(workspace_id)   = /home/user/workspace/{id}
     └─ tools.py:926  _git_add_sensitive → plan_git_add（git add -n 问 git 自己）
     └─ tools.py:931  run_git(sandbox, root, subcmd, argv, identity=...)
        └─ gitops/executor.py:51   sandbox.commands.run(command, cwd=workspace_path)
           └─ 与 2.2 同一条 resolve 链 → workspace-volumes/{id}

模型调用 git_push
  └─ gitops/orchestrator.py:131   work_path = WORKSPACE.workspace_path(workspace_id)
  └─ gitops/bundle.py:27/34       run_git(..., "rev-parse"/"bundle", ...)
     └─ 同一个 cwd → volume 仓库
  └─ 后端读 bundle → 临时仓 fetch → 带凭据 push（凭据不进沙箱，这一点没变）
```

结论：`git_push` 推的是 **volume 里那个仓库**。若用户绑定目录本身已经是 git 仓库（有自己的 `.git`、历史、远端），它完全不在链上——实测 `git init` 在 volume 里新建了一个 `.git`，与此无关。

`plan_git_add`（`executor.py:82`）同样以 volume 为 cwd，所以敏感路径判定看到的也是 volume 的文件列表。

---

## 3. 两套「权威根」

| | 根 | 谁在用 |
| --- | --- | --- |
| A 运行时注册表 | `LocalVolumeBackend._local_roots[id]`（`volume_backend.py:104`，bind 时归一） | `_dir()`（`volume_backend.py:160-162`）→ `resolve_workspace_path` / `write_files` / `destroy` |
| B 硬编码路径 | `data_root()/"workspace-volumes"` | 沙箱全部执行链：`local.py:714`、`local.py:763`、`local.py:968` |

A 的消费者只有 3 个（面板读、工作区上传写、销毁）；B 的消费者是**全部执行链**。绑定发生时 A 被更新，B 不动。

`workspace-volumes/{id}` 这一级在两个根之间是「桥」，但它现在是普通目录而非联接——`[已实测]`。

补充：`volume_backend._LOCAL_ROOT` 支持 `EDITH_WORKSPACE_VOLUME_DIR` 覆盖（`volume_backend.py:27-32`），而 `local.py` 三处硬编码 `data_root()`。全仓仅 `volume_backend.py:27` 读这个变量，文档、测试、启动脚本都没提。也就是说：**一旦配置它，连未绑定工作区都会分裂**。目前是休眠配置。`[源码已确认]`

---

## 4. 副作用入口全景（谁写哪里）

| 入口 | 落点 | 依据 |
| --- | --- | --- |
| `sandbox_write_file` / `make_directory` / `move` / `delete` | volume | 2.1 |
| `sandbox_run_command` | volume（经联接） | 2.2 |
| `git` / `git_push` | volume | 2.3 |
| 工作区上传 `POST /internal/workspaces/{id}/files`（`workspace/http.py:332`） | `write_files` → `_dir()` → **绑定目录** | `[源码已确认]` |
| 会话上传 `POST /internal/sandbox/files/upload`（`sandbox/http.py:145`） | `uploads/{name}` → 沙箱根下的 `uploads/`，与工作区无关 | `[源码已确认]` |
| 文件面板列表/预览（`sandbox/http.py:77-79`、`service.py:93`） | `local_workspace_path` → `_dir()` → **绑定目录**；未命中才回落沙箱 | `[源码已确认]` |
| `agent_input.validate_uploads`（`agent_input.py:31`） | `uploads/` → `files.get_info` → 沙箱根 | `[源码已确认]` |
| 工作区销毁 `destroy`（`volume_backend.py:223`） | 绑定态只解绑，不删文件 | `[源码已确认]` |
| `legacy_dir` / `merge` / 前缀探测（`volume_backend.py:141`、`workspace/http.py:92`） | 读旧副本 `workspace-volumes/{id}`，用于绑定时的合并与前缀判定 | `[源码已确认]` |

用户可见的后果（实测支撑，非推测）：

1. 绑定后**面板里有的文件，Agent 说找不到**（`files.list` → `[]`）。
2. **Agent 写的文件，面板刷新后看不到**（写进 volume，面板读绑定目录）。
3. 绑定目录里已有仓库时，`git` 工具操作的是另一个空仓库。

---

## 5. 现有测试覆盖

| 测试文件 | 覆盖 | 是否覆盖「绑定 + 沙箱」 |
| --- | --- | --- |
| `test_local_root_binding.py` | 归一化、四个 `LocalRootError`、加载不回写库 | **否**（不构造 `LocalSandbox`） |
| `test_workspace_bind.py` | service 层绑定、legacy 合并、前缀 | **否** |
| `test_sandbox_path_contract.py` | `resolve()` 契约、D-5/D-6、越界矩阵 | **否**（全部未绑定工作区，含 `test_unbound_workspace_still_uses_volume`） |
| `test_workspace_mount_integrity.py` | 挂载点自检 / 重建 / `occupied` | **否** |

grep 证据：`bind_local_root` 只出现在前两个文件，且两者都不 import `LocalSandbox`。

所以这个分裂不是「测试写错了」，而是**两条链从来没有在同一个测试里相遇**。未绑定路径的契约测试全绿，绑定路径根本没有断言。

---

## 6. 已确认的缺陷（只编号，不设计）

| 编号 | 事实 | 状态 |
| --- | --- | --- |
| F-1 | 绑定后文件工具读写落在 `workspace-volumes/{id}`，与绑定目录无关 | `[已实测]` |
| F-2 | Shell 与文件工具一致（都落 volume），但与面板/上传不一致 → 同一虚拟路径两份内容 | `[已实测]` |
| F-3 | `git` 在 volume 里操作；用户绑定目录若是仓库，完全不可见 | `[已实测]` |
| F-4 | `git_push` 的 bundle 来自 volume 仓库 | `[源码已确认]` |
| F-5 | `EDITH_WORKSPACE_VOLUME_DIR` 只有 `volume_backend` 认，沙箱层硬编码 → 配置后未绑定工作区也分裂 | `[源码已确认]`（休眠配置） |
| F-6 | `sandbox/service.py:102-104` 注释称「`workspace/{id}` 的联接是首次解析时按需建立」，实际 `_workspace_volume_dir` 只 `mkdir` | `[源码已确认]`（注释与实现不符） |
| F-7 | `_normalize` 的输出映射只认 `data_root()/workspace-volumes`；权威根一旦换成绑定目录，shell 输出会漏出宿主绝对路径 | `[源码已确认]`（当前不可达，因为 shell 看不到绑定目录） |

---

## 7. 尚待验证

1. `[已实测]`（**优先级最高**）用户绑定目录本身是 git 仓库时，`git` / `git_push` 的完整行为（含 `.git` 会不会被敏感路径规则拦掉）。它直接决定「绑定一个已有仓库」是不是产品路径。→ 见 §9.1，已补齐（`git_push` 远端段除外）。
2. `[待验证]` 网络盘 / UNC 作为 `local_root`。本机 C、D 均 `DriveType=3`（本地盘），此环境无法构造，需要真实 `\\server\share`。
3. `[已确认]`（源码）`merge_existing=True` **在 UI 上可达**：`web-v1/components/chat/workspace-switcher.tsx:487-503` 有勾选框「合并存量文件」，条件是 `create.mode === "bind" && create.targetWorkspaceId`（绑定已有工作区，不是新建），默认 `false`，经 `app/api/workspaces/[id]/bind-local-root/route.ts:44` 透传。→ `legacy_dir` 的合并路径是**活代码**，不是死分支；见 §9.2。

---

## 8. 「不破坏未绑定行为」的具体含义

未绑定工作区里（默认配置，即未设置 `EDITH_WORKSPACE_VOLUME_DIR`），A（`_dir()` → `_root/{id}`）与 B（`data_root()/workspace-volumes/{id}`）指向同一目录，所以：

- 文件工具、Shell、Git、面板、上传**全部一致**；
- `test_sandbox_path_contract.py` 的 26 条路径矩阵与 `test_unbound_workspace_still_uses_volume` 就是在钉这个状态。

也就是说，任何让「绑定态」收敛到单一根的做法，都必须让「未绑定态」的落点、越界判定、以及 `workspace-volumes/{id}` 这个物理位置保持不变——这是 P0-2 的硬约束，不是可选项。

---

## 9. 待验证项补齐（2026-11-19 追加，仍为只读探针）

### 9.1 绑定目录本身是 git 仓库

构造：临时 `ETHOS_DATA_DIR`；绑定目录里 `git init -b main` + 1 个提交（`app.py`）+ 一个未跟踪的 `.env` + `origin` 指向本地 bare 仓库并已推送 `main`。然后 `bind_local_root` → `LocalSandbox` → 走工具层。全程不改代码、不碰真实数据。

| # | 调用 | 实测结果 | 状态 |
| --- | --- | --- | --- |
| A | `files.list("/home/user/workspace/ws-repo")` | `[]`（`app.py` / `.env` / `.git` 一个都看不到） | `[已实测]` |
| B | `git status` | `exitCode=128`，`fatal: not a git repository (or any of the parent directories): .git` | `[已实测]` |
| C | `git log --oneline -3` / `git commit -m x` | 同 B（`exitCode=128` + 同一句 fatal） | `[已实测]` |
| D | `git add app.py` | `kind=permission_denied`、`retryable=false`，消息：「无法核对这次 add 会暂存什么，已拒绝：fatal: not a git repository …」 | `[已实测]` |
| E | `git add .` | 参数层先拒：`git add only accepts explicit paths, got '.'`（`kind=command_error`） | `[已实测]` |
| F | `check_path("workspace/ws-repo/.git/config")` | 命中 `.git`（任何档位都拦；同判 `.git` 目录本身） | `[已实测]` |
| G | `check_path("workspace/ws-repo/.env")` / `app.py` | 命中 `.env` / 放行 | `[已实测]` |
| H | 绑定目录的 HEAD / 工作区状态 / 远端分支 | `HEAD` 前后一致、状态仍只有 `?? .env`、远端 `main` 仍在 → **整条链路对该目录零写入** | `[已实测]` |
| I | `workspace-volumes/ws-repo` | 空目录，`.git` 不存在 | `[已实测]` |

由此得到三条与设计无关的事实：

1. **「绑定一个已有仓库」当前不是可用路径**：Agent 既看不到工作树（A），也看不到历史（B/C），`git status` 的报错还是「不是仓库」而不是「你绑定的目录不在这里」——模型无从纠正。
2. **D 是一处诊断失真**（不是新缺陷，是既有行为被这条路径暴露）：真实原因是「cwd 不是仓库」，返回给模型的却是 `permission_denied` + 「无法核对这次 add 会暂存什么」。模型读到「权限」会转去换路径，而问题根本不在权限。记录事实，不在 P0-1/P0-2 里顺手改。
3. **`.git` 的拦截与绑定无关，且改造后依然生效**：F 是 `permissions/paths.py` 的固定段规则（不可配置、`full` 档也生效）。所以即便 P0-2 把权威根切到绑定目录，文件工具读 `.git` 这条路仍然永久不可用——历史只能经 `git` 工具（真实 git 进程，不受字符串规则约束）拿。这决定了「Agent 能否理解一个已有仓库」必须靠 git 工具，而不是读文件。

未覆盖：`git_push` 的远端段（需要 DB + 注入的 push 能力 + 真实远端凭据），当前只能按 F-4 认定它推的是 volume 仓库；E 也说明 Agent 想提交真实仓库时必须逐个文件 add。

### 9.2 `merge_existing` 是活路径

`workspace-switcher.tsx:487-503` 的勾选框「合并存量文件」只在 `mode === "bind" && targetWorkspaceId`（绑定**已有**工作区）时出现，默认关闭，经 `bind-local-root/route.ts:44` 透传到 `volume_service.bind(... merge_existing=...)`。所以 `merge_dir_add_only` / `legacy_dir` 是**产品可达**的分支——绑定已有工作区时，「旧 volume 副本里的文件要不要搬进本地目录」是用户可选的开关。

对 P0-2 的含义（只陈述约束，不给方案）：绑定态一旦换根，**未勾选合并的旧 volume 内容会变成不可见**。这不是新行为（现在旧内容本来也不可见，只是反过来），但换根后它从「volume 里有、面板看不到」变成「volume 里有、谁都不看它」，需要一个明确处置口径。
