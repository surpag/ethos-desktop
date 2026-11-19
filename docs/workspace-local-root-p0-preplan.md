# 工作区 local_root 补绑定：P0 pre-plan（只读核验与预验证报告）

> **文档性质**：核心是只读核验 + 实验报告（§0–§11.7，不含实施代码）。
> §11.8 是唯一的实施记录：**P0-1 路径契约阶段已完成，P0-2 沙箱层接入尚未开始**。
> 即：绑定目录**还没有**接入命令执行与 Git，其余按 §8.7 未开工。
> 目的是把「接下来要改什么」建立在实测事实与源码定位上，而不是推论上。
>
> 标注约定（全文每条结论都带其中之一）：
>
> | 标签 | 含义 |
> | --- | --- |
> | `[已实测]` | 本轮或前序在本机真实跑过，有命令与输出 |
> | `[源码已确认]` | 读过代码、定位到具体行，行为可由代码推出 |
> | `[设计建议]` | 我的判断与方案，尚未实现、也未验证 |
> | `[尚待验证]` | 显式未知项，进入 P0 前必须先测 |
>
> 四种标签不可混用。后文凡是写「会」「能」「必须」的地方，先看它挂的是哪个标签。

---

## 0. 结论摘要

四件事被确认，其中一件是**与 junction 无关的独立缺陷**；第二轮探针（§11）又补出三条，并**推翻了本文最初关于大小写的判断**：

| # | 发现 | 状态 | 后果 | 归属 |
| --- | --- | --- | --- | --- |
| 1 | 文件工具与 Shell 指向不同目录 | `[已实测]` | 文件工具写用户目录，`ls`/`git` 看 Volume 空目录 | P0 核心 |
| 2 | 子层 junction 作 `find` 起点时返回空、exit=0 | `[已实测]` | 模型误判「目录是空的」，重扫/重写 | P0 需配套规范 cwd 与遍历写法 |
| 3 | `_resolve_workspace_volume` 比较前只 resolve 了一侧 | `[已实测]`+`[源码已确认]` | 一旦 Volume 目录变成 junction，文件工具**直接 403** | P0 硬前提 |
| 4 | 命令输出里的宿主真实路径只被部分还原 | `[已实测]`+`[源码已确认]` | 模型拿到它无法再次传入的 `C:\...` 路径 | P0 配套（输出虚拟化） |
| 5 | **`resolve("workspace")` 挂载点自身抛 403** | `[已实测]` | `files.list("workspace")` / `ls /home/user/workspace` 全失败 | P0-1 附带（同一分支） |
| 6 | **`workspace/./{id}/x` 被误判越界** | `[已实测]` | 合法路径被拒 | P0-1 附带（同一段守卫） |
| 7 | `is_relative_to` 在 Windows **大小写不敏感**，但**对 8.3 短名无感知** | `[已实测]` | 未归一化的 `local_root` 会产生假 403 | P0-2 必须先把 base 归一 |

第 5、6 条是探针新发现，与 junction 无关，即使不补绑定也应当修；第 7 条推翻了我原先「大小写敏感、风险高」的判断，详见 §11.3。

第 3 条最关键：**它不是 junction 的兼容问题，是路径校验本身的缺陷**。当前生产环境没炸，只是因为 `workspace-volumes/{id}` 恰好是普通目录（没有链接可跟穿）。补绑定会让它变成链接，于是这个缺陷立刻变成硬阻塞。

一句话总结现状：

> 文件面板已经做对了（`_dir()` 直连用户目录），命令与 Git 没有；补绑定不是「从零引入本地目录」，而是把已经验证过的 `_dir()` 语义延伸到沙箱层，同时先修掉一个会挡住它的判定缺陷。

---

## 1. 问题背景与目标架构

### 1.1 为什么现在要做

`[源码已确认]` 桌面版的「沙箱」是一个目录（`LocalSandbox`），不是容器。工作区支持绑定用户本地目录，`LocalVolumeBackend._dir()` 已经实现了「绑定即消除副本」：

```python
# workspace/volume_backend.py:85-87
def _dir(self, workspace_id: str) -> Path:
    # 读写直连：绑定 local_root 时所有存储操作都落在用户目录，否则退回副本
    return self._local_roots.get(workspace_id) or (self._root / workspace_id)
```

`[源码已确认]`+`[已实测]` 但沙箱层完全不看这个绑定：`_ensure_workspace_mount` 硬编码 `data_root()/workspace-volumes`（`local.py:431`），于是同一会话出现两个「工作区根」（实验 C 探针 1 已复现）。

### 1.2 目标（P0 范围）

唯一目标：**同一个 Workspace，文件工具、Shell、Git 看到同一个根目录。**

不是目标：删除 Volume 兜底、做 OS 级隔离、迁移历史数据。这三件都不在 P0。

### 1.3 为什么保留 Volume 兜底

`[设计建议]` 未绑定工作区继续落 `workspace-volumes/{id}`。理由是可验证的：删掉兜底会同时动「创建流程（强制选目录）+ 初始化 + 历史数据 + 所有 `legacy_dir` 引用」，而补绑定可以先验证最关键的一件事——文件工具写的文件，Shell 立刻能读能改能删，反之亦然。

**架构原则（本轮确认，作为后续所有相关改动的判据）**：

> 一个 Workspace 只能有一个权威文件根目录。文件工具、Shell、Git、Skills 都从统一的路径解析机制取根，任何模块不得自己拼 `workspace-volumes` 或 `local_root`。

---

## 2. 当前真实调用链

### 2.1 三个入口，两条链

`[源码已确认]` 全部工作区路径解析只有两个函数，但入口分成两类：

```
【链 A：已做对】文件面板
  sandbox/http.py:list_files
    → Service.local_workspace_path()            sandbox/service.py:95
    → LocalVolumeBackend.resolve_workspace_path()  volume_backend.py:89
    → _dir()                                     volume_backend.py:85   ✅ 感知 local_root

【链 B：未打通】文件工具 / Shell / Git
  sandbox_read_file / sandbox_write_file / files.list
    → LocalSandbox.resolve()                     local.py:293
    → _resolve_workspace_volume()                local.py:350
    → _ensure_workspace_mount()                  local.py:412
    → <data_root>/workspace-volumes/{id}         local.py:431-433       ❌ 不感知 local_root

  run_command
    → LocalCommands.run(cwd=...)                 local.py:615
    → self._sb.resolve(cwd or work_path)         local.py:624
    → 同上链 B                                                          ❌

  git（gitops）
    → orchestrator: WORKSPACE.workspace_path(id)  gitops/orchestrator.py:131
    → executor: sandbox.commands.run(cwd=虚拟路径) gitops/executor.py:53,84
    → LocalCommands.run → resolve()              local.py:624
    → 同上链 B                                                          ❌
```

`[源码已确认]` 关键点：**Git 没有自己的路径逻辑**。它拿的是虚拟路径 `/home/user/workspace/{id}`（`workspace/layout.py:27`），作为 `cwd` 交给 `commands.run`，最终和 `run_command` 走同一行 `resolve()`。

所以「Git 路径不对」不是第二个 bug，是同一个 bug。修链 B 即同时修 Git。

### 2.2 逐入口核验表

| 入口 | 解析函数 | 感知 `local_root` | 依据 |
| --- | --- | --- | --- |
| 文件读写工具 | `resolve()` → `_resolve_workspace_volume` | 否 | `[源码已确认]` `local.py:352` |
| `run_command` | 同上（`cwd` 先过 `resolve`） | 否 | `[源码已确认]` `local.py:624` |
| Git（status/diff/add/commit…） | 同上 | 否 | `[源码已确认]` `gitops/executor.py:53,84` |
| 文件面板（HTTP） | `_dir()` | **是** | `[源码已确认]`+`[已实测]` |
| 文件搜索 | 无独立搜索工具；`files.list` 走 `resolve()` | 否 | `[源码已确认]` |
| 项目级 Skills | 无此层（桌面版单层 `skills/`） | 无关 | `[源码已确认]` `skills/types.py:20` |

`[源码已确认]` Skills 与工作区无关：桌面版把云端的三层作用域砍成单层目录 `<data_root>/skills`，`_ensure_skills_mount` 只挂一个 junction（`local.py:377-410`），不参与 workspace 绑定，P0 不需要动它。

### 2.3 Skills 分支与本缺陷的对照（说明缺陷不是「所有分支都这样」）

`[源码已确认]` `resolve()` 里三条分支的比较写法一致，但只有 workspace 分支会炸：

| 分支 | base 是否已 resolve | 结论 |
| --- | --- | --- |
| 沙箱根 | `self._root.resolve()` (`:342`) | 正确 |
| `skills` | `_ensure_skills_mount()` 返回 `target`，而 `target` 在 `:388` 已 `.resolve()` | 正确 |
| `workspace/{id}` | `_ensure_workspace_mount()` 返回 `volumes / id`（未 resolve） | **缺陷** |

这不是「写法风格不一致」，是**返回值契约不一致**：两个 `_ensure_*_mount` 一个返回解析后路径、一个返回未解析路径，而调用方按同一套写法比较。

---

## 3. 路径分裂的证据

### 3.1 分裂的具体形态

`[已实测]` 用真实 `LocalVolumeBackend` + `LocalSandbox`（临时 `ETHOS_DATA_DIR`，完全隔离）验证：绑定 `local_root` 后，

- 文件面板读到 `D:\...\bound-target\`（用户目录）
- `sandbox.resolve("workspace/{id}")` 返回 `<tmp>/data/workspace-volumes/{id}`（Volume）

两者不同源。

### 3.2 用户可见的三种表现

`[设计建议]`（后果由代码推出，未逐一在 UI 上复现）：

| 场景 | 表现 |
| --- | --- |
| 文件工具写入 → `ls` | `ls` 看到空目录；模型认为写入失败 |
| `run_command("git status")` | 报「not a git repository」（Volume 里没有 `.git`） |
| 文件面板上传 → Shell 读取 | 上传落用户目录，`cat` 找不到 |

`[已实测]` 第三种有一个补充证据：文件面板走链 A，所以**它是唯一看到的正确视图**——这解释了为什么「面板里有文件、Agent 说没有」这类报告会同时出现。

### 3.3 只改挂载、不改判定会更糟

`[源码已确认]` 命令的路径翻译是纯字符串替换（`local.py:701-715` `_translate`），不做校验；文件工具走 `resolve()`。因此若只把 Volume 目录换成 junction 而不修判定，会得到：

- `cat /home/user/workspace/{id}/a.txt` 正常（shell 直接读文件系统）
- `sandbox_read_file("workspace/{id}/a.txt")` **403**

「shell 能读、文件工具不能」比现在的双目录分裂更难排查。这条是 P0-1 必须先于 P0-2 的原因。

---

## 4. 实验环境

| 项 | 值 | 状态 |
| --- | --- | --- |
| OS | Windows（`sys.platform == "win32"`），数据根 `%APPDATA%\ETHOS` | 已观测 |
| Shell | Git Bash 5.2.15（MSYS2），路径 `C:\Program Files\Git\bin\bash.exe` | `[已实测]` |
| Python | 后端 `.venv`，CPython 3.13（`__pycache__/*.cpython-313.pyc`） | 已观测 |
| 实验根 | `%LOCALAPPDATA%\Temp\jetest`（已删除） | `[已实测]` |
| 隔离手段 | 实验 C 用临时 `ETHOS_DATA_DIR`，不碰真实 `%APPDATA%\ETHOS` | `[已实测]` |

**实验边界（遵守情况）**：只用临时目录与临时文件；未改任何源码；未触碰用户真实项目；每条命令记录退出码；清理后核对真实数据完好（`workspace-volumes/951cc…/app.py` 仍存在）。

**方法说明（两处环境干扰，如实记录）**：

1. `[已实测]` 从 Git Bash 调 `cmd //c mklink /J ...` 时，`/J` 被 MSYS 路径转换改写成盘符，创建失败；关掉参数转换（`MSYS2_ARG_CONV_EXCL='*'`）后 `//c` 又变成非法参数、cmd 退化成交互模式。
2. `[已实测]` 其中两条含 `mklink` 的复合命令被环境安全策略拦下（未返回具体资源）。**未尝试绕过**，改用 PowerShell 原生 `New-Item -ItemType Junction` 完成，该方式无需管理员权限。

因此 junction 的**创建**由 PowerShell 完成；**使用**由 Git Bash 与真实 ETHOS 命令链完成——后者才是需要证明的对象。

---

## 5. 实验 A / B / C 结果

### 5.1 实验 A：Windows 原生（对照组）

`[已实测]`

| 项 | 结果 |
| --- | --- |
| `New-Item -ItemType Junction` 创建 | 成功，无需管理员 |
| `Get-ChildItem` 查看 | `LinkType=Junction`，`Target` 正确 |
| 断开链接 `[System.IO.Directory]::Delete(path,false)` | 成功（递归删除前必须先断开，否则穿透到 target） |

结论：OS 层无障碍。这一组只证明「junction 能用」，不证明「MSYS 能用好它」——所以它是对照，不是依据。

### 5.2 实验 B：Git Bash / MSYS（核心）

两种结构分别测，避免把「结构差异」误读成「junction 缺陷」：

- **case1**：`workspace` 是 junction → `volumes`，`volumes/bound-ws` 再 junction → `target`（父层 + 子层嵌套，最小改动形态）
- **case2**：`workspace` 是实体目录，`workspace/bound-ws` 是 junction → `target`（每工作区单独挂）
- **基线**：`workspace` 是 junction，子目录全是实体目录（= 当前生产结构）

| 命令 | case2 | case1 | 基线 | 说明 |
| --- | --- | --- | --- | --- |
| `cat <junc>/a.txt` | ✅ | ✅ | ✅ | 显式路径正常 |
| `ls <junc>` | ✅ | ✅ | ✅ | 列内容正常 |
| `ls -la <junc>` | ⚠️ 只显示链接行 | 未单测 | 未单测 | 必须写 `<junc>/` |
| `ls -la <junc>/` | ✅ | 未单测 | 未单测 | 尾斜杠即正常 |
| `find <junc> -name "*.txt"` | ❌ 空、exit=0 | ❌ 空、exit=0 | ❌ 空、exit=0 | 起点是 symlink，`-P` 不跟 |
| `find <junc>/ -name "*.txt"` | ✅ | 未单测 | ✅ | 尾斜杠强制进入 |
| `find <junc>/. -name` | ✅ | 未单测 | 未单测 | 同上 |
| `find workspace/ -name`（父层） | 进入实体子目录 | 未单测 | ✅ | 是否递归进子 junction 未作结论，见附录 C |
| `grep -r pat <junc>` | ✅ | ✅ | 未单测 | 起点会被进入 |
| `grep -r pat <父>/` | ❌ exit=1 | ❌ exit=1 | 未单测 | 不进入子 junction；`-R` 才递归 |
| `cd <junc> && find .` | ✅ | 未单测 | 未单测 | **cwd 是真实目录，绕开起点限制** |
| `cd <junc> && grep -r pat .` | ✅ | 未单测 | 未单测 | 同上 |
| `cd <junc> && ls -la` | ✅ | 未单测 | 未单测 | 同上 |
| `cd <junc> && echo x > t.txt` | ✅ 落到 target | 未单测 | — | 写入位置正确 |
| `git rev-parse --show-toplevel`（cwd 在内） | ✅ 识别到 target | 未单测 | — | Git 可用 |
| `cat ../plain-ws/c.txt`（cwd 在内） | ⚠️ 成功（按物理路径） | 未单测 | — | **未拒绝**：shell 层无工作区边界 |
| `cat <junc>/../../../target/a.txt` | ⚠️ 成功 | 未单测 | — | 同上 |

**读表规则**：标「未单测」的格子不参与任何结论。本文所有结论只依赖标了 ✅ / ❌ / ⚠️ 的单元格；`case1` 的作用是证明「嵌套 junction 至少 `ls`/`cat`/`grep` 起点可用」，不足以支撑更细的断言。

**三个必须分开看的事实**：

1. `[已实测]` `find <junc>` 无尾斜杠返回**空**且 **exit=0**。不报错、不提示，模型只会认为「目录里没有文件」。这是本轮最危险的一条。
2. `[已实测]` **基线同样如此**（当前生产结构下 `find workspace` 无尾斜杠也返回空）。所以这不是 junction 引入的缺陷，是 MSYS 对 symlink 起点的一贯行为。代码注释里「挂在父层，`workspace/{id}` 就是普通目录，`ls`/`find` 都能正常进入」只说对了 `{id}` 这一层，`workspace` 本身仍是链接。
3. `[已实测]` **真回归只有一处**：今天 `find workspace/{id}` 能工作（`{id}` 是实体目录）；改成子层 junction 后失效——而这恰是模型最自然的写法。

`[已实测]` 同时证明 `cd` 方案有效：`cd <工作区> && find .` / `grep -r .` / `ls -la` 全部正常，因为进程 cwd 是解析后的真实目录。

### 5.3 实验 C：真实 ETHOS 命令链

用真实 `LocalVolumeBackend` + `LocalSandbox`（临时 `ETHOS_DATA_DIR`），探针脚本直接调 `sb.resolve()` 与 `commands.run()`。

| # | 探针 | 结果 | 状态 |
| --- | --- | --- | --- |
| 1 | 绑定 `local_root` 后比对文件面板路径与 `resolve()` 结果 | 两个不同的目录 | `[已实测]` |
| 2 | `workspace-volumes/{id}` 做成 junction → `resolve("workspace/{id}/a.txt")` | 抛 `SandboxPathError`（403） | `[已实测]` |
| 3 | 逐步拆解判定 | 见 §6.2 的 base/target 值 | `[已实测]` |
| 4 | 同一条路径用 `commands.run("cat ...")` | 成功读到内容 | `[已实测]` |
| 5 | `git rev-parse --show-toplevel`（经 junction） | 返回 `C:/Users/<user>/.../target` | `[已实测]` |

第 4 与第 2 并列，就是 §3.3 说的「shell 能读、文件工具 403」。

第 5 条的机制见 §7。

---

## 6. `resolve()` 路径校验缺陷

### 6.1 现状代码

`[源码已确认]` `sandbox/local.py:350-361`：

```python
def _resolve_workspace_volume(self, workspace_id: str, rest: list[str], virtual: str) -> Path:
    base = self._ensure_workspace_mount(workspace_id)
    if not rest or rest == [""]:
        return base
    normalized = posixpath.normpath("/".join(rest))
    if normalized == ".." or normalized.startswith("../"):
        raise SandboxPathError(f"路径不能上溯到工作区根之外: {virtual}")
    target = (base / normalized).resolve()
    if target != base and not target.is_relative_to(base):   # ← 两侧不对称
        raise SandboxPathError(f"路径解析后越出工作区根: {virtual} → {target}")
    return target
```

`base` 来自 `_ensure_workspace_mount`（`local.py:412-456`），返回 `volumes / _safe_segment(workspace_id)`，**未 resolve**；`target` 明确 `.resolve()`。

### 6.2 实测到的值

`[已实测]`（临时目录，`workspace-volumes/ws-test` 为 junction → `jetest/target`）：

```
base                                    = C:\...\data\workspace-volumes\ws-test      (未解析)
target                                  = C:\...\jetest\target\a.txt                  (已解析，跟穿链接)
target.is_relative_to(base)             = False      ← 当前实现 → 抛 SandboxPathError
target.is_relative_to(base.resolve())   = True       ← 修复假设成立
```

### 6.3 为什么这不是「两行就够」

`[设计建议]` 修复方向是把比较的两侧都落到解析后的真实路径：

```python
base = self._ensure_workspace_mount(workspace_id).resolve()
...
if target != base and not target.is_relative_to(base):
```

`[已实测]` 探针已按下列用例跑完（§11 有原始输入/输出）。结论是：**两侧 resolve 能修掉「绑定目录后必然 403」，但不足以让 `resolve()` 正确**——另有两处误拒与一处归一化缺口，见 §11.4。

| 用例 | 期望 | 当前状态 |
| --- | --- | --- |
| 目标文件不存在 | 正常返回规范化路径 | **通过**：`[已实测]` 单级与多级不存在路径均正常返回；`files.write` 自动建父目录并读回成功 |
| 工作区内 symlink 指向工作区外 | 拒绝（403） | **通过**：`[已实测]` 目录 junction 逃逸在「两侧 resolve」判定下为 False，正确拒绝 |
| `workspace/{id}/../..` | 拒绝 | **通过**：`[源码已确认]` `local.py:313-331` 两道守卫；`[已实测]` 三种变体全部抛错 |
| Windows 大小写差异（`D:\Proj` vs `d:\proj`） | 应视为同一根 | **通过**：`[已实测]` `PureWindowsPath` 比较会归一大小写，`is_relative_to` 大小写不敏感（见 §11.3），**原判断错误，已更正** |
| 盘符差异 / `/` 与 `\` 混用 | 应归一 | **部分**：`[已实测]` `\` 与 `/` 等价；绝对路径（含 `\\?\`、`//?/`）一律拒绝；跨盘 `is_relative_to` 返回 False 而不抛错 |
| 8.3 短名 / 别名前缀（探针新增） | 同一目录应判为同一根 | **失败**：`[已实测]` base 未归一、target 已归一 → `is_relative_to` 为 False（§11.3） |
| `local_root` 本身是 symlink/junction | 边界仍以最终真实根为准 | **通过（组件级）**：`[已实测]` 两侧 resolve 后，内部子路径 True、嵌套逃逸 False |
| 网络盘 / 映射盘（`Z:`） | 与本地盘同语义 | **未验证**：本机只有 C/D 两个本地盘（DriveType=3），无网络盘，未用模拟替代 |

**必须坚持的原则**：路径校验针对**解析后的真实路径**，不是字符串。当前缺陷的本质是「一半解析一半没解析」，任何只修一处的补丁都会换个条件复发。

### 6.4 另一处泄漏：错误信息里带真实路径

`[源码已确认]` 上面两处 `SandboxPathError` 的消息把 `target`（解析后的真实路径）直接拼进文本：

```python
raise SandboxPathError(f"路径解析后越出工作区根: {virtual} → {target}")
```

这条错误会作为工具结果交给模型。也就是说**即使挂载和判定都修好了，越界报错仍在泄漏宿主路径**。同类问题在 `local.py:347`（沙箱根分支）也存在。

`[设计建议]` 报错只用虚拟路径；真实路径只进日志。这条要写进 P0，否则「修完功能，泄漏照旧」。

---

## 7. 宿主真实路径泄漏

### 7.1 现状机制

`[源码已确认]` `LocalCommands.run` 只对 stdout/stderr 做一次 `_normalize`（`local.py:665-666`），其还原表来自 `_path_pairs`（`local.py:689-699`）：

```python
for real_path, virtual in ((volumes, .../workspace), (root, /home/user)):
    for form in (self._shell_path_of(real_path), real_path.as_posix(), str(real_path)):
        pairs.append((form, virtual))
```

只含两项：`workspace-volumes`、沙箱根。**不含 `local_root`**。

`[已实测]` 所以 `git rev-parse --show-toplevel` 经 junction 返回 `C:/Users/<user>/.../target`，模型拿到一个它无法再传给工具的真实路径（`resolve()` 会判越界）。

### 7.2 不能只用「加一行 local_root」收尾

`[设计建议]` 建议确立原则：

> 内部允许使用真实宿主路径；**Agent 可见的路径一律是虚拟 Workspace 路径**。

据此要覆盖的消费者不止 Git：

| 消费者 | 泄漏路径 | 当前是否经过 `_normalize` |
| --- | --- | --- |
| `pwd` / `pwd -P` | stdout | 是 |
| `git rev-parse` / `status` / `diff --stat` | stdout | 是（`redact_git_output` 只做密钥脱敏，不做路径还原） |
| `find` / `grep` / `rg` | stdout | 是 |
| 命令的 stderr（编译错误、Traceback） | stderr | 是 |
| Python / Node 打印的文件路径 | stdout/stderr | 是 |
| 文件工具返回的条目路径（`files.list`） | 结构化字段 | 否——走 `virtual()` |
| 工具错误消息（§6.4） | 异常文本 | 否 |
| 后台进程 `cwd`（`commands.list`） | 结构化字段 | 否——走 `virtual()`，见下 |

`[源码已确认]` `virtual()`（`local.py:458-502`）只映射沙箱根、`workspace-volumes`、`skills`，末尾 `return str(raw)`——**`local_root` 下的路径会原样返回**。目前文件面板没暴露，是因为它走链 A 自己拼虚拟路径（`_local_entries(path, local_dir)`），不是靠 `virtual()`。一旦绑定工作区走沙箱回退分支（本地目录不存在时），这条兜底就会把真实路径交出去。`[尚待验证]` 该回退分支在绑定工作区的实际触发频率与输出。

### 7.3 文本替换的固有边界

`[源码已确认]`+`[设计建议]`：`_normalize` 是正则文本替换，且是**大小写敏感的字符串包含**（与 `Path` 比较的大小写不敏感不同，两者不要混）；能处理写法枚举到的路径，处理不了：大小写变更、8.3 短名、被工具转义成 `C:\\Users\\...`（JSON/转义序列）、相对路径输出、被 base64/编码后的路径。

所以：**`_normalize` 是可用性修复，不是安全边界**。安全边界只能是 §6 的 `resolve()` 判定 + §9 的 OS 层约束。文档里不能把「输出看着是虚拟路径」当成「隔离成立」。

---

## 8. P0 改造范围与依赖

### 8.1 依赖图

```
P0-1 判定对齐（硬前提）
  └─> P0-2 沙箱感知 local_root（核心）
        ├─> P0-3 cwd 默认工作区根（配套，依赖「当前工作区」上下文）
        └─> P0-4 输出路径虚拟化（配套）
              └─> P0-5 测试与回归（含未绑定回归）
```

P0-1 先于 P0-2 的理由已在 §3.3 说明：顺序反了会先得到「403 比分裂更难查」的中间态。

### 8.2 P0-1 判定对齐

| 项 | 内容 |
| --- | --- |
| 改动点 | `_resolve_workspace_volume`（`local.py:350`）比较前把 `base` 也 resolve |
| 同时修 | `SandboxPathError` 消息去掉真实路径（`local.py:347`、`:360`） |
| 影响面 | 所有走 `resolve()` 的工作区路径：文件工具、Shell、Git |
| 风险 | 中——§6.3 八条里 6 条已通过，失败项是 8.3 别名（§11.3）；另有 D-5/D-6 两处误拒要一起修 |
| 状态 | `[设计建议]`，用例 `[尚待验证]` |

### 8.3 P0-2 沙箱感知 local_root

`[源码已确认]` 当前 `LocalSandbox.__init__(root, sandbox_id)` 完全不知道 volume 层存在；`Service` 持有 `_workspace_volumes`（`sandbox/service.py:36`）但 `LocalSandbox.open(user_id, session_id)` 没把它传下去。

`[设计建议]` 注入一个「`workspace_id` → 本地根」的解析回调（只要 `local_root_of` 一个方法，不要整个 service），在 `_ensure_workspace_mount` 里分流：

- 未绑定 → 现状（`workspace` 父层 junction → `workspace-volumes`）
- 已绑定 → `workspace/{id}` 子层 junction → `local_root`

**这里有一个必须先决策的结构问题**（`[设计建议]`，两种都成立，取舍不同）：

| 方案 | 形态 | 优点 | 代价 |
| --- | --- | --- | --- |
| A 混合 | 未绑定用父层 junction；绑定工作区在 `workspace/` 下单独建子 junction | 改动最小，不影响未绑定工作区与历史会话 | 结构两种形态并存；`find workspace/{id}` 对绑定工作区失效（§5.2 真回归） |
| B 统一 | `workspace` 改为实体目录，每个工作区各自 junction | 语义统一，符合「一个 Workspace 一个根」 | 要替换现有父层 junction（可能非空/被占用），未绑定工作区行为也变 |

`[已实测]` 两种结构都跑过 `ls` / `cat` / `grep`（显式路径起点），均可用；`cd` + 遍历与 Git 只在 case2 单测（case1 未单测，见 §5.2 读表规则）。两种结构唯一的共同失效项是「junction 作 `find` 起点」。

`[设计建议]` 我倾向 **A**（增量、可回退、不破坏未绑定路径），并把「子层 junction 的 `find` 起点问题」用 P0-3 的 cwd 规范吸收，而不是改结构去绕。

**未绑定工作区必须继续可用**——这是 P0-5 的强制回归项，不是可选项。

### 8.4 P0-3 cwd 默认工作区根

`[源码已确认]` 现在默认 cwd 是 `work/`：`local.py:624` `self._sb.resolve(cwd or WORKSPACE.work_path())`。

`[设计建议]` 语义按四点落地，**不做无条件全局默认**：

1. Agent 未指定 cwd → 用当前 Workspace 根
2. Agent 指定工作区内子目录 → 尊重（不覆盖）
3. cwd 必须过路径边界校验（走 `resolve()`，不绕过）
4. 递归类命令的起点用真实工作目录（cwd + `.`），不是把 junction 路径直接丢给 `find`

`[尚待验证]` 一个实现前提：`LocalCommands.run` 拿不到「当前工作区」——它的签名只有 `cmd/envs/cwd`，`LocalSandbox` 也不知道会话绑了哪个工作区。要把 workspace 上下文传进命令层，涉及会话→工作区的绑定关系（会话可换绑）。**这不是一行改动，必须先定注入点**，否则会退化成「全局默认 `work/` 换成全局默认某个 workspace」。

`[设计建议]` 特别声明：**cwd 解决的是路径一致性与 MSYS 遍历兼容性，不是安全隔离。** `cd ..`、绝对路径访问依然可以越界（§5.2 已实测未拒绝）。把这条写进文档，是为了防止后面有人拿「cwd 已指向工作区」当安全结论。

### 8.5 P0-4 输出路径虚拟化

| 项 | 内容 |
| --- | --- |
| `_path_pairs` | 加入 `local_root`（长路径优先），覆盖 `/c/...`、`C:/...`、`C:\...` 三种写法 |
| `virtual()` | 加入 `local_root` → `workspace/{id}` 映射（`local.py:502` 的 `str(raw)` 兜底） |
| 原则 | 内部真实路径、外部虚拟路径，单点收敛，不在各工具里各写一遍 |
| 边界 | 文本替换是可用性修复，不是隔离（§7.3） |

`[尚待验证]` `local_root` 与 `workspace-volumes` 同时存在时，前缀替换的先后与交叠（`workspace-volumes/{id}` 是 `local_root` 的父目录或反之）要有测试，不能只靠「按长度倒序」。

### 8.6 P0-5 测试与回归

`[设计建议]` 新增测试分两层：

- **单元**：`resolve()` 的判定矩阵（§6.3 八条 + 未绑定/已绑定两种根）
- **集成**：真实 `LocalSandbox` + 临时 `ETHOS_DATA_DIR`，跑 §10 的验收表

强制回归（缺一即不算完成）：**未绑定工作区**的文件工具、Shell、Git 三条链全部仍走 `workspace-volumes`。

### 8.7 明确不在 P0

| 项 | 原因 |
| --- | --- |
| 删除 `workspace-volumes` 兜底 | 影响创建流程与历史数据，需单独决策 |
| `legacy_dir` 数据迁移策略 | 属 P4；`bind_local_root` 现在默认 `merge_existing=False`（只新增不覆盖），策略未定 |
| OS 级命令隔离 | 独立任务，见 §9 |
| 云端/E2B 路径 | 桌面版已移除 |

---

## 9. 安全边界与非目标

### 9.1 本轮确认的边界事实

`[源码已确认]`+`[已实测]`：

- **没有进程级隔离**：命令以当前宿主用户身份运行，`resolve()` 管路径不管权限。
- **没有出口网络限制**：`netguard.py` 只约束 Agent 工具发的 HTTP，不约束 shell。
- **权限档位拦的是「调用权」**，不拦「调用后能碰什么」。
- **cwd 不是边界**：`cd ..`、绝对路径、`$()` 都能出去（§5.2）。

### 9.2 命令工具安全治理：独立任务，不与 P0 混做

`[设计建议]` 三种 Windows 手段的定位必须说清楚，否则「上了 Job Object 就安全了」会成为下一个错误结论：

| 手段 | 管什么 | 不管什么 |
| --- | --- | --- |
| Job Object | 进程生命周期、进程树、资源（内存/CPU/句柄） | **不限制文件访问** |
| 受限 Token | 降低进程权限（去特权、降完整性级别） | 能不能读某目录仍取决于 ACL，需要配套配置 |
| AppContainer | 较严格的资源访问隔离（文件/网络/注册表按 capability 授权） | 需要能力声明与目录授权，接入成本最高 |

`[设计建议]` 因此：「命令工具的文件访问边界」应作为独立任务立项，威胁模型先定（防误操作 / 防分发后滥用 / 防不可信代码），再选手段。P0 只做「让三条链看到同一个目录」，不承诺「命令被关在目录里」。

### 9.3 相邻但未纳入的已知缺口

`[源码已确认]` 以下问题真实存在，本轮**只记录不处理**，避免 P0 范围蔓延：

1. **子进程环境变量全量继承**：`local.py:627` `env = {**os.environ}` → `run_command("env")` 可打印宿主密钥。这绕过权限档位（档位拦执行，不拦读取）。
2. **敏感路径是字符串匹配**：`permissions/paths.py` 对命令串做匹配，编码/间接引用可绕。
3. `workspace/http.py:332-338` 有一段检测「403 + volumes → `volume_not_enabled`」的分支，桌面版 `LocalVolumeBackend` 已不产生该异常，属不可达代码。

---

## 10. 验收测试清单

`[设计建议]` 每条都要有可执行的判定方式；**不接受「跑了几条命令没报错」作为完成标准**。

| # | 验收项 | 预期结果 | 判定方式 |
| --- | --- | --- | --- |
| 1 | 文件工具写入 → Shell 读取 | 成功，内容逐字节一致 | `sandbox_write_file` 写 → `commands.run("cat ...")` |
| 2 | Shell 写入 → 文件工具读取 | 成功，内容一致 | `commands.run("echo ... > f")` → `sandbox_read_file` |
| 3 | Git 识别绑定目录里的仓库 | `rev-parse --show-toplevel` 落在该工作区 | 绑定已有 `.git` 的目录后跑 `git status` |
| 4 | `find .` | 能找到工作区内文件 | cwd 为工作区根，`find . -name "*"` 非空 |
| 5 | `grep -r` | 能搜到工作区内文件 | 写入可识别串后 `grep -r <串> .` |
| 6 | Git 输出路径 | 不含 `C:\` / `/c/` 宿主路径 | 断言输出匹配 `^/home/user/workspace/` |
| 7 | `../` 越界 | 拒绝 | `resolve("workspace/{id}/../..")` 抛错 |
| 8 | 内部 symlink 指向外部 | 拒绝 | 工作区内建 symlink → 外部文件，`sandbox_read_file` 403 |
| 9 | 未绑定 Workspace | 继续走 Volume 兜底 | 文件工具 + Shell + Git 三链均在 `workspace-volumes/{id}` |
| 10 | 绑定与解绑 | 不丢、不覆盖已有数据 | 绑定前 Volume 有文件、目标目录有同名文件 → 分别验证 `merge_existing` 两态 |
| 11 | 目标文件不存在 | 不误判越界 | `resolve` 一个不存在的子路径，返回规范化路径 |
| 12 | 大小写 / 盘符 / 分隔符混用 | 判为同一根 | `D:\Proj` 与 `d:\proj` 各调一次（已知通过，§11.2）；跨盘应判否 |
| 13 | `local_root` 自身是链接 | 边界以真实根为准 | 绑定一个 junction 目录，越界用例仍拒绝 |
| 14 | 错误信息 | 不含宿主真实路径 | 触发 403，断言消息里无 `C:\` |
| 15 | `workspace` 挂载点自身 | 可解析、可列目录，不抛 403 | `resolve("workspace")` / `files.list("workspace")`（D-5） |
| 16 | `workspace/./{id}/x` | 不误拒 | `resolve` 返回工作区内路径（D-6） |
| 17 | 8.3 别名 / 未归一前缀 | 同一目录判为同一根 | `GetShortPathNameW` 造短名 base，断言判定为 True |
| 18 | `files.write` 不存在的多级路径 | 建父目录、读回一致 | 写 `deep/a/b/c.txt` 后 `files.read` 验证 |

**第 9 条是硬性回归**：补绑定的目标是**增加一种解析方式**，不是替换原来的 Volume 模式。未绑定路径坏了，等于把「可选能力」做成了「破坏性变更」。

---

## 11. 第二轮探针结果（路径解析 + 返回值契约）

### 11.1 方法与纪律

| 项 | 内容 |
| --- | --- |
| 被测对象 | 真实 `LocalSandbox`（`LocalSandbox.open`），不是自造替身 |
| 隔离 | 每个用例独立 session（`s1`、`s2`…）+ 临时 `ETHOS_DATA_DIR` |
| 链接创建 | 用产品自己的 `_make_link`（`cmd /c mklink /J`），不用 PowerShell，避免"测试建法 ≠ 运行建法" |
| 记录 | 每条记录输入、`base`/`base_resolved`/`target`、三种判定结果、异常类型与消息 |
| 清理 | 先自底向上断链接，再删树；核对链接目标内容仍在后才删 |
| 清理结果 | `root_removed=True`、`targets_intact=True`，无临时目录残留 |
| 脚本位置 | 临时目录，按纪律随用随删；重跑方式见 §11.7 |

三种判定并列记录，用于区分「规范化问题」和「包含关系问题」：

1. `target.is_relative_to(base)` —— 现状（base 未解析）
2. `target.is_relative_to(base.resolve())` —— 修复假设（两侧解析）
3. `normcase` 字符串前缀比较 —— 只用来判断「是否仅差大小写/写法」

### 11.2 第一组：路径解析与边界

| 用例 | 实测 | 结论 |
| --- | --- | --- |
| 目标文件不存在（单级） | 返回 `...\ws-plain\new-file.txt`，`exists=false`，父目录存在 | 正常 |
| 目标文件不存在（多级） | 返回 `...\deep\a\b\c.txt`，父目录不存在 | 正常（写入时建父目录） |
| 写入不存在的多级路径 | `files.write("workspace/ws-plain/deep/a/b/c.txt")` → 磁盘存在、父目录已建、读回 `"hello"`、返回 `path=/home/user/workspace/...`（虚拟） | 正常 |
| 目录 junction 逃逸（工作区内链接 → 外部目录） | 两侧 resolve 判定：`is_relative_to_base_resolved = false` | 正确拒绝 |
| 同一链接下的正常子路径 | `is_relative_to_base_resolved = true` | 正确接受 |
| 文件级 symlink | `os.symlink` → `OSError: 1314`（需要管理员/开发者模式） | **未验证**（未提权，不伪造） |
| `workspace/{id}/sub/../a.txt` | 归一化到工作区内，放行 | 正确 |
| `workspace/{id}/..` | 403「路径不能跳出工作区边界」 | 正确 |
| `workspace/{id}/../ws-other/a.txt` | 同上 | 正确 |
| `../outside.txt` | 403「路径不能上溯到沙箱根之外」 | 正确 |
| `/etc/passwd` | 403「路径越出沙箱根 /home/user」 | 正确 |
| 盘符/分隔符混用 `workspace\{id}\a.txt` | 与 `/` 写法返回同一路径 | 正确 |
| **`workspace/./{id}//a.txt`** | **403「路径不能跳出工作区边界」** | **误拒（§11.4 D-6）** |
| 绝对路径 `C:/Windows/win.ini`、`D:\agent` | 403 | 正确 |
| 扩展前缀 `//?/C:/...`、`\\?\C:\...` | 403（在「绝对路径」守卫处拒绝） | 正确 |
| 跨盘比较 `D:/x/a.txt` vs `C:/x` | `is_relative_to=false`，不抛错；`os.path.commonpath` 抛 `ValueError: Paths don't have the same drive` | 保留 `is_relative_to`，不要换 `commonpath` |
| 网络盘 | 本机 `Win32_LogicalDisk`：C、D 均 `DriveType=3`（本地），无网络映射盘 | **未验证** |
| `local_root` 自身是 junction | 内部子路径 True、嵌套逃逸 False | 两侧 resolve 后语义正确（组件级） |

`..` 的一个语义细节（`[已实测]`）：从 junction 根出发，`..` 解析到**链接所在目录的父级**，不是链接目标的父级。所以 `bound/../realws/a.txt` 会落回目标自身（判定 True），而 `bound/../outside/secret.txt` 会离开目标（判定 False）。这不影响虚拟路径（`..` 在字符串层已被拦），但解释了 shell 侧的越界面貌。

### 11.3 关键纠正：大小写不敏感，真正的问题是别名

**原文写错了。** §6.3 原先记的是「`is_relative_to` 是纯字符串比较，大小写敏感 → 风险高」。实测：

```
PureWindowsPath("C:/A/B") == PureWindowsPath("c:/a/b")      → True
Path("C:/Temp/X.txt").is_relative_to(Path("c:/temp"))        → True
磁盘上是 WS-CASE，product_resolve("workspace/ws-case/a.txt")  → 成功返回 ...\WS-CASE\a.txt
```

`[已实测]`+`[源码已确认]`：`PurePath` 在 Windows 上用 normcase 比较，**大小写不敏感**。所以「大小写混用会导致 403」这个担心不成立，也不需要为此重写比较逻辑。

但同一组数据里出现了真正的失败：**8.3 短名**。

```
base          = C:\Users\WS2101~1\...\Temp\P0PROB~1\realws     （未解析，短名）
base_resolved = C:\Users\ws21018374\...\Temp\p0probe2-xxx\realws （已解析，长名）
target        = C:\Users\ws21018374\...\Temp\p0probe2-xxx\realws\a.txt
is_relative_to(base)          = False   ← 同一个目录，判成越界
is_relative_to(base.resolve()) = True
```

`[已实测]` 短名与长名指向同一目录，但**任何未归一的前缀都会让判定失败**。当前产品没爆，是因为 `_ensure_workspace_mount` 的第一段做了 `(data_root()/"workspace-volumes").resolve()`，把前缀归一了，只有 `id` 段是拼上去的（应用生成、无别名）。而 P0-2 要引入的 `local_root` 来自数据库/用户选择，**不保证已归一**——这正是补绑定会踩到的假 403，且它看起来像"路径校验坏了"，实际是"一半归一"。

> 这也说明「规范化」和「包含关系」是两件事：`is_relative_to` 的包含关系是对的（跨盘、大小写、分隔符都处理了），错的是**喂给它的两个路径不在同一个规范化平面上**。

### 11.4 两条新缺陷（与 junction 无关，现状即可复现）

**D-5 `resolve("workspace")` 挂载点自身抛 403**

```
resolve("workspace")            → SandboxPathError: 路径解析后越出沙箱根: workspace → ...\workspace-volumes
resolve("workspace/")           → 同上
files.list("workspace")         → 同上（同一入口）
resolve("/home/user")           → 正常（沙箱根）
resolve("skills") / ("skills/") → 正常
```

`[源码已确认]` 机制：`resolve()` 只在 `parts[0]=="workspace" and len(parts)>=2` 时分流到 `_resolve_workspace_volume`（`local.py:334`）。`workspace` 只有一段，于是落到沙箱根分支；而 `{root}/workspace` 是链接，`target.resolve()` 得到 `workspace-volumes`，不在 `{root}` 下 → 403。

`skills` 没有这个问题，因为它在 `:339` 被无条件分流，与段数无关。**这是分支条件不一致，不是 junction 问题**：即使不补绑定，模型只要 `ls /home/user/workspace` 就会拿到 403。

**D-6 `workspace/./{id}/x` 被误判越界**

```
resolve("workspace/./ws-plain//a.txt") → SandboxPathError: 路径不能跳出工作区边界
```

`[源码已确认]` 机制（`local.py:320-331`）：守卫拿**原始** `split` 结果 `original` 与**归一化**后的 `normalized_parts` 比位置：

```
original         = ["workspace", ".", "ws-plain", "a.txt"]
normalized_parts = ["workspace", "ws-plain", "a.txt"]
normalized_parts[1] ("ws-plain") != original[1] (".")  → 抛错
```

即：这条守卫本意是「归一化不能把路径带出同一 `{id}`」，但它用未归一化的片段做对照，于是一个合法的 `./` 前缀就被当成跳界。修法是对照归一化后的片段（`posixpath.normpath` 处理过的同一份），不是放宽条件。

### 11.5 第二组：挂载函数返回值契约

| 函数 | 输入 | 返回形态 | 绝对 | `str(p) == str(p.resolve())` | 是链接 | 语义 |
| --- | --- | --- | --- | --- | --- | --- |
| `_ensure_workspace_mount` | 普通卷 `ws-plain` | 真实宿主路径 | 是 | **是** | 否 | 绝对 + 已归一 |
| `_ensure_workspace_mount` | junction 卷 `ws-link` | 真实宿主路径 | 是 | **否** | 是 | **绝对 + 未归一** |
| `_ensure_skills_mount` | — | 真实宿主路径 | 是 | **是** | 否 | 绝对 + 已归一（返回的是**目标**，不是挂载点） |
| `_resolve_workspace_volume` | `("ws-link", [])` | 真实宿主路径 | 是 | **否** | 是 | 返回未归一的 base |
| `_resolve_workspace_volume` | `("ws-link", ["a.txt"])` | — | — | — | — | 抛 `SandboxPathError` |

四条结论：

1. **同一个函数有两种返回语义**：`rest` 为空返回未归一的 base，`rest` 非空返回归一后的 target。调用方拿到的路径"是不是已经归一"取决于它查的是根还是子路径。
2. **两个 `_ensure_*_mount` 契约不一致**：workspace 返回「可能未归一的绝对路径」，skills 返回「已归一的绝对路径」；但两个调用方（`local.py:352`、`:365`）用的是同一句 `is_relative_to` 比较，**默认它们语义相同**。
3. **都返回真实宿主路径，从不返回虚拟路径**。反向映射在 `virtual()`，而它对 `local_root` 不认（§7.2）。
4. 契约不一致今天不可见，只因为「未绑定卷恰好是普通目录，未归一 == 已归一」。一旦卷是链接（补绑定的形态），差异立刻变成 403。

`_safe_segment` 契约（`[已实测]`）：`""`/`".."` → `ValueError`；`a/b`、`a\b` → `a_b`；`C:` → `C_`；`  x  ` → `x`；`工-作` 保留；`CON` **原样返回**（Windows 保留设备名未处理，取决于后续 `mkdir` 行为，本轮未建目录验证）。

### 11.6 结论分类（对应你给的 A / B / C）

| 假设 | 判定 | 依据 |
| --- | --- | --- |
| **A 只存在返回值不一致** | **不成立** | 除契约不一致外，还有 D-5、D-6 两处独立误拒，与归一化无关 |
| **B 还存在 Windows 路径比较问题** | **成立，但形态要改** | 不是大小写（实测不敏感），是**别名/短名 + 未归一前缀**；比较原语本身正确，问题在两个操作数不在同一规范化平面 |
| **C 存在无法安全判定的边界** | **部分成立** | 目录 junction 可判定（逃逸正确拒绝）；**文件级 symlink 未验证**（`OSError 1314`）；**网络盘未验证**（本机无） |

所以：这不是「两行 `.resolve()`」，也**不需要**自研一套跨平台路径比较；需要的是三件事——统一返回值契约、修两处守卫误拒、把"输入必须先归一"写成契约（并在 bind 时落实）。

### 11.7 对 P0-1 的判据（路径契约阶段已实施，见 §11.8；沙箱层接入属 P0-2）

按优先级：

1. **统一挂载函数返回契约**：`_ensure_workspace_mount` / `_ensure_skills_mount` 都返回「绝对 + 已归一」；`_resolve_workspace_volume` 对「根」和「子路径」返回同一语义。调用方不再需要猜测。
2. **修 D-5**：`parts[0]=="workspace"` 且无 `{id}` 时，应返回挂载点（`workspace-volumes` 根），而不是落到沙箱根分支。
3. **修 D-6**：守卫的对照改用归一化后的片段。
4. **归一化契约前置**：`local_root` 在 `bind_local_root` 写库前归一一次（同时保留用户原始写法用于展示），否则 8.3 别名会在绑定工作区上产生假 403。
5. **不做的事**：不换 `commonpath`；不声称覆盖网络盘/文件级 symlink；不把「两侧 resolve 后比较」包装成 OS 级隔离（TOCTOU 仍在——校验通过到打开文件之间链接可变）。

重跑方式：按 §11.1 的四条纪律，用 `LocalSandbox.open` + 临时 `ETHOS_DATA_DIR`，`_make_link` 建 junction，逐条断言 `is_relative_to(base_raw)` / `is_relative_to(base_resolved)` 与异常类型；探针脚本按纪律留在临时目录、用后即删。

### 11.8 P0-1 路径契约阶段实施记录（2026-11-18）

> **阶段边界（写在这里，防止被读成"补绑定做完了"）**
> P0-1 完成的是：Workspace 路径解析契约统一、绑定入口归一化、两处路径解析缺陷
> （D-5 / D-6）修复、挂载点完整性自检。
> **绑定目录尚未接入沙箱命令执行与 Git** —— 那两项属于 P0-2。所以现在**不能**对外说
> 「本地项目目录已经和 Agent 沙箱完全打通」；准确说法是上面那一句。

**只做一件事：让"路径"在一个阶段内只有一种含义。** 8.3 短名 / 网络盘 / 文件级 symlink
**没有**被写成"不支持"——短名纳入修复（入口归一），后两者继续标记「未验证」。

#### 两种路径，各有归属（不再靠注释约定）

| 类型 | 形态 | 谁用 |
| --- | --- | --- |
| Virtual Workspace Path | `/home/user/...` | 模型、工具结果、事件、前端 |
| Resolved Host Path | 绝对 + 已 `resolve()` + 大小写/短名已展开 | 文件系统访问、命令执行、边界判断 |

**返回值契约（两个 `_ensure_*_mount` 共用）**：返回「绝对 + 已归一 + 存在」的目录，
且是**联接指向的目标**而不是联接本身。Skills 与 Workspace 的返回值语义因此一致，
调用方不再需要猜。

#### 绑定路径的三个存放位置（不是三份权威）

| 位置 | 存什么 | 谁是权威 |
| --- | --- | --- |
| 数据库 `workspaces.local_root` | **持久化绑定记录**（用户当时选的路径，可能是短名/别名写法） | 只在加载时读取，不作比较依据 |
| 注册表 `LocalVolumeBackend._local_roots` | **运行时规范化路径**（加载/绑定时归一后的结果） | ✅ 运行时唯一权威 |
| 沙箱 `resolve()` | 从注册表/Volume 推出的真实根 | 由上一行派生，不独立存储 |

加载时以**规范化结果**作为运行时权威值；本轮**不在加载阶段写库**——那会引入新的
失败场景与事务边界。因此库与注册表**表示**可以不同（库存短名、注册表存长名），
**语义**必须一致。将来若要把库里的旧写法迁移成归一值，应作为独立的数据迁移设计，
而不是混在读取路径里。

#### 七处改动

| 位置 | 改动 | 对应缺陷 |
| --- | --- | --- |
| `sandbox/local.py:resolve()` | `workspace`（不带 `{id}`）直接返回挂载根，不再落到沙箱根分支 | D-5 |
| `sandbox/local.py:resolve()` | 守卫改用**与归一化同规则**的片段（丢掉空段与 `.`）比对 | D-6 |
| `sandbox/local.py` | `_ensure_workspace_mount()` 去掉 `workspace_id` 形参、返回已归一的 `workspace-volumes`；新增 `_workspace_volume_dir(id)` 返回已归一的 `{id}` 目录 | 契约不一致（= 联接工作区假 403 的根因） |
| `sandbox/local.py` | 两个 `_ensure_*_mount` 收敛到共用 `_ensure_mount()`：联接在但指向不对 → 只摘链接再重建；**任何**无法保证一致的结局（摘不掉 / 重建失败 / 非空目录占位 / 首次创建失败）都抛 `SandboxMountError` + 原因码 | 静默分裂（本轮引入的行为退化） |
| `sandbox/local.py` | `_link_target_text()` 区分三种 `\\?\` 读回形态（盘符剥前缀 / UNC 还原 / Volume GUID 与 GLOBALROOT **原样保留**）；`_link_target()` 只对真正相对的目标拼链接父目录 | Volume GUID 被剥成相对路径 → 拼出伪造路径、反复重建 |
| `workspace/volume_backend.py` | 新增 `LocalRootError` 四子类（不在/不是目录/相对路径/不可用）+ `normalize_local_root(strict=)`；`bind_local_root` **登记即归一** | 8.3 别名 |
| `workspace/volume_service.py` | 归一 + 校验**先于写库**；库与注册表存同一份归一值 | 失败不覆盖旧绑定 |
| `workspace/http.py` | `_validate_local_root` 复用同一实现，只做 400/403 语义翻译 | 去重复实现 |

`strict` 的两种用法是刻意的：绑定新目录用 `strict=True`（存在性必须失败得快），
从库读回用 `strict=False`（目录被删不该让会话起不来，错误留给真实读写去暴露）。
`resolve(strict=True)` 的失败按原因分三类报出，不糊成一句「绑定失败」。

#### 挂载点自检为什么必须在 P0-1 内修

改动前 `_ensure_workspace_mount` 见到联接就早退（`if _is_link(mount): return volumes`），
从不校验联接指向。D-5 让 `resolve("workspace")` 开始返回这个挂载根之后，这个早退
就从"无害"变成"静默分裂"：文件工具拿到 `workspace-volumes`，shell 走那个坏联接，
两边看到不同目录，**且没有任何报错**。原来这一格是 403（大声失败），改完变成静默，
属于本次改动引入的退化，因此归属本轮，不留到 P0-2。

不变量（两个 mount 共用 `_ensure_mount`，语义逐条相同）：

| 情况 | 行为 |
| --- | --- |
| 联接指向预期目录（**回读确认**） | 复用，不重建（`_link_target` + `_same_dir`） |
| 目标被删 / 指向别处 | 只摘**链接本身**（`rmdir` → `unlink`，绝不 `rmtree`），再重建 + 回读 |
| 摘链接失败 | 抛 `SandboxMountError`（`unlink_failed`） |
| 重建失败 | 抛 `SandboxMountError`（`relink_failed`） |
| 建完回读仍不符 | 抛 `SandboxMountError`（`verify_failed`） |
| 空实体目录占位（无数据） | 删掉占位后建联接 |
| 非空目录 / 同名文件占位 | 抛 `SandboxMountError`（`occupied`），其中内容一个不动 |
| 占位目录因权限/占用删不掉 | 抛 `SandboxMountError`（`prepare_failed`，**不是** `occupied`） |
| 目标 / 父目录建不出 | 抛 `SandboxMountError`（`prepare_failed`） |
| 从未建过 / 首次创建失败 | 抛 `SandboxMountError`（`create_failed`） |

其中三行是 2026-11-19 补的：

- **回读确认**：承诺不再依赖"我刚调用过 `_make_link`"。建链接返回成功但落点不对
  （旧版本行为差异、中间被改指）同样要报 `verify_failed`——否则函数在骗调用方。
- **`prepare_failed` 与 `occupied` 分列**：`rmdir` 失败 ≠ 目录非空。权限不足、
  文件系统不可用、被其他进程占用都会失败，混成"目录非空"会让排查方向彻底跑偏。
  只有 `ERROR_DIR_NOT_EMPTY`(145) / `ENOTEMPTY` 判 `occupied`；`FileNotFoundError`
  视为"检查后被并发删除"，继续建链接而不是报错。
- **`occupied` 覆盖同名文件**：挂载点被一个普通文件占着时，`rmdir` 报 `ENOTDIR`，
  同样属于"不能为了挂载删用户东西"。

`create_failed` 的来历：原先的区分是「首次创建失败只告警、重建失败才抛错」，
判据是"有没有弄坏既有状态"。这个判据站不住：**真正的判据是一致性**——
联接没建成时文件工具走 `workspace-volumes`、shell 走 `workspace`，两者从第一秒起
就是两个目录，而这正是 P0-1 要消灭的视图分裂，与它是不是"刚建的"无关。
所以现在只有两种结局：返回一个已验证一致的根，或者抛错；`reason` 仅用于诊断
（日志 / HTTP 细节 / 测试断言），**不构成控制流分支**。

#### 旧布局（非空实体目录）的恢复方式 —— 不自动迁移

本机实测存在两种旧形态，更新后会以 `occupied` 明确报错（不是静默分裂）：

| 沙箱 | `workspace` 现状 | 更新后的表现 |
| --- | --- | --- |
| `e2e-sess-3ecd4d33` | 实体目录 + 一个按工作区建的子联接（`ce2e1fee…`） | 访问工作区报 `occupied` |
| `e2e-sess-b59c0812` | 实体目录 + 一个真实子目录（`daafa703…`） | 访问工作区报 `occupied` |
| 其余（`2fa2b316…` / `8223e1cd…` / `e2e-sess-6e5fcff5…`） | 正确联接 | 正常复用 |

**恢复动作（人工，一次）**：把 `%APPDATA%\ETHOS\sandbox\local-user\<会话>\workspace`
整目录**改名**（如 `workspace.legacy`，不要删），下次访问工作区时会自动建好正确联接。
改名只移动目录项，目录内子联接指向的 `workspace-volumes\{id}` 不受影响；
确认无遗漏后再自行处理 `workspace.legacy`。

**为什么不做自动迁移**：程序无法区分"用户真实文件 / 旧版遗留副本 / 子联接 / 三者混合"，
自动搬运的代价是某天搬错一次。旧布局迁移单独立项（检查 → 出清单 → 保留原目录 →
用户确认 → 迁移 → 校验文件数与内容 → 最后切换），**不属于 P0-1**。

#### 留在风险清单里的三条（不在本轮修，也不得当成已解决）

1. **`_same_dir` 不是对象身份验证**。`resolve()` 之后的 `normcase + abspath` 兜底
   仍是字符串比较，没有 file id / volume serial 参与。它只回答"要不要保留已存在的
   挂载"，**不参与任何删除决策**；删除路径的唯一依据是 `_unlink_mount` 的
   「只摘链接项、不递归」。
2. **`_unlink_mount` 的检查与删除之间存在竞态窗口**。最坏情形是删掉一个在窗口内
   出现的**空**目录（`rmdir` 对非空目录必失败，且不做递归），不会顺着链接删目标；
   但不能描述为"完全没有竞态"。
3. **Volume GUID 目标能否被 `resolve()` 归一到盘符写法未实测**（要建独立卷 +
   `mklink` 才能证）。当前实现保证它不会被拼成相对路径；若归一失败，表现是
   **一次**重建（重建后指向规范的 `workspace-volumes`，此后稳定），不是每次访问
   都重建。真机验证列入 P0-2 的验证清单。

#### 测试证据（红 → 绿）

新增 `tests/test_sandbox_path_contract.py`（23 例）、`tests/test_local_root_binding.py`（12 例）、
`tests/test_workspace_mount_integrity.py`（29 例），合计 64 条新回归：

| 阶段 | 结果 |
| --- | --- |
| 未修复（local.py 回退到 HEAD） | `10 failed / 13 passed`：D-5 四条、D-6 两条、契约两条、联接读写与 shell 往返各一条 |
| 未修复（挂载自检回退成"见联接就早退"） | `5 failed / 4 passed`：损坏修复、错误指向不复用、shell/文件工具同步、重建失败报错、摘链接失败报错 |
| 未修复（失败语义回退成"首次创建告警 / 非空目录降级 / UNC 剥 4 字符"） | `6 failed / 14 passed`：首次创建报错、`open()` 拦截、非空目录报错、skills 两条、UNC 前缀一条 |
| 未修复（Volume GUID 剥前缀 + 去掉回读验证 + rmdir 失败全判 occupied） | `6 failed / 23 passed`：GUID 两条（字符串级 + 路径语义级）× 2 例、`verify_failed` 一条、`prepare_failed` 一条 |
| 未修复（绑定归一化） | 收集期 `ImportError`（`LocalRootMissing` 等符号不存在 = 能力不存在） |
| 修复后 | `593 passed / 1 skipped`（跳过项是既有的 symlink 环境跳过） |



**未随本次改动放宽的边界**（同时回归）：跨工作区 `..`、归一化后跳出工作区树、
挂载根上溯、`/etc/passwd`、`..` 起点共 7 条仍抛 `SandboxPathError`；
另跑 26 例路径矩阵（10 允许 / 16 拒绝）0 偏差；未绑定工作区仍写
`workspace-volumes/{id}`（2 条）。

#### 明确**不在**本轮（仍按 §8.7）

沙箱感知 `local_root`（P0-2）、cwd 默认工作区根（P0-3）、命令输出路径虚拟化（P0-4）、
命令工具 OS 级隔离（§9.2）、`legacy_dir` 迁移（P4）。

**一句如实的话**：P0-1 之后，绑定本地目录的工作区仍然只修好了「文件工具能读写」，
**sandbox 的 `resolve()` 根本不看 `local_root`**，shell 与 Git 还指向
`workspace-volumes/{id}` —— §3.1 的分裂在绑定工作区上依旧存在。这是 P0-2 的活，
不是本轮漏做。


---

## 附录 A：源码索引（本文所有 `[源码已确认]` 的落点）

| 位置 | 内容 |
| --- | --- |
| `sandbox/local.py:293-348` | `resolve()` 主分流；沙箱根分支比较两侧均解析 |
| `sandbox/local.py:320-331` | `workspace/` 归一化后必须仍在同一 `{id}` 下 |
| `sandbox/local.py:350-361` | `_resolve_workspace_volume`：**base 未解析**的比较（缺陷） |
| `sandbox/local.py:377-410` | `_ensure_skills_mount`：返回已解析 target |
| `sandbox/local.py:412-456` | `_ensure_workspace_mount`：硬编码 `workspace-volumes`，返回未解析 `volume` |
| `sandbox/local.py:458-502` | `virtual()`：只映射沙箱根 / volumes / skills，末尾 `str(raw)` |
| `sandbox/local.py:615-668` | `LocalCommands.run`：默认 cwd `work/`、`env={**os.environ}`、输出过 `_normalize` |
| `sandbox/local.py:670-699` | `_normalize` + `_path_pairs`（无 `local_root`） |
| `sandbox/local.py:701-715` | `_translate`：纯字符串替换，不校验 |
| `sandbox/service.py:78-95` | `local_workspace_path` → `backend.resolve_workspace_path`（懒绑定） |
| `sandbox/http.py:72-92` | 文件面板：本地直读优先，`_local_entries` 拼虚拟路径 |
| `workspace/volume_backend.py:46-60` | `bind_local_root` / `unbind` / `local_root_of` |
| `workspace/volume_backend.py:66-68` | `legacy_dir` |
| `workspace/volume_backend.py:85-87` | `_dir()`：绑定优先，否则 Volume |
| `workspace/volume_service.py:89-116` | 绑定服务：默认不合并，返回 `merged`/`unmerged` |
| `workspace/layout.py:27` | `workspace_path(id)` 生成虚拟路径 |
| `gitops/orchestrator.py:131` | Git 用虚拟路径作 cwd |
| `gitops/executor.py:53,84` | `sandbox.commands.run(cwd=workspace_path)` |
| `skills/types.py:20` | 桌面版单层 Skills，无 project 层 |

## 附录 B：实验原始命令（可复现）

```bash
# 实验 B（Git Bash 5.2.15），cwd = 结构根
ls workspace/bound-ws
ls -la workspace/bound-ws
find workspace/bound-ws -name "*.txt"
find workspace/bound-ws/ -name "*.txt"
find workspace/ -name "*.txt"
grep -r needle workspace/
grep -r needle workspace/bound-ws
cd workspace/bound-ws && find . -name "*.txt"
cd workspace/bound-ws && grep -r needle .
cd workspace/bound-ws && git rev-parse --show-toplevel
cd workspace/bound-ws && cat ../plain-ws/c.txt
```

```powershell
# junction 创建（PowerShell 原生，无需管理员）
New-Item -ItemType Junction -Path "$T\volumes\bound-ws" -Target "$T\target" -Confirm:$false
Get-Item "$T\volumes\bound-ws" | Select-Object FullName, LinkType, Target
```

```python
# 实验 C 探针（简化）：真实 LocalSandbox + 临时 ETHOS_DATA_DIR
sb = LocalSandbox.open("local-user", "probe-session")
sb.resolve("workspace/ws-test/a.txt")        # → 抛 SandboxPathError（base 未解析）
await sb.commands.run("cat /home/user/workspace/ws-test/a.txt", cwd="/home/user")
```

## 附录 C：待办与下一步

0. `[已完成]` **P0-1 路径契约阶段已完成**（§11.8）：契约统一、D-5、D-6、bind 入口归一、挂载点自检；64 条新回归，全量 `593 passed / 1 skipped`。**P0-2 沙箱层接入尚未开始**（`resolve()` 仍不看 `local_root`）
1. `[已实测]` §6.3 八条已跑完：6 条通过、1 条失败（8.3 别名，§11.3）、1 条未验证（网络盘）。P0-1 的新判据见 §11.7
2. `[尚待验证]` **明确未覆盖的两项**：文件级 symlink（本机 `OSError 1314`，需管理员/开发者模式）与网络盘/UNC（本机无网络盘）。在验证前不得声称支持
3. `[尚待验证]` §5.2 中标注「未单测」的单元格：case1（嵌套 junction）下的 `ls -la`、`find <junc>/`、`cd` + 遍历、Git；以及父层 `find` 是否递归进入子 junction。这些**不是已知失败**，是没测过，不能当结论用
4. `[设计建议]` 结构方案 A/B 二选一（§8.3）
5. `[尚待验证]` P0-3 的「当前工作区」上下文注入点（§8.4）
6. `[设计建议]` 命令工具安全治理单独立项（§9.2）
7. `[设计建议]` `legacy_dir` 迁移策略（P4）

**下一步是 P0-2**（沙箱感知 `local_root`）：只有它做完，绑定工作区的文件工具与
shell 才真正指向同一个根；在那之前 §3.1 的分裂仍在，别把 §11.8 的绿灯读成「补绑定完成」。
