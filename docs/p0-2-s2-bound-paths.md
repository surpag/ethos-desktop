# P0-2 S2：绑定状态接入执行链（文件工具 + Git）

**状态**：已实施并提交。前置：S1（`workspace/context.py` + `capture()` + `_run` 快照）。
**范围**：只改文件工具与 Git 的路径落点。Shell（S4）、面板（S3）未动。

---

## 0. 一句话结论

S1 让「绑定状态」进入了 run 的决策过程（只用于 fail-closed），S2 让它**进入了路径解析**：

> `workspace/{id}` 在绑定态落到用户目录，未绑定态落到 `workspace-volumes/{id}`，
> 异常态直接失败——三种结果由**一次执行的快照**决定，不由当前注册表决定。

---

## 1. 注入形态：按调用传，不用隐式状态

`WorkspaceContext` 从 `_run` 一路显式传下去：

```
_run (S1 已抓快照)
  └─ chat(..., workspace_context=ctx)
       └─ tool_executor.execute(..., context=ctx)      # 每次工具调用都传
            └─ Service.workspace(user, session, context=ctx)
                 └─ LocalSandbox.open(..., context=ctx)
                      └─ resolve() → _contextual_workspace_dir()
```

**为什么不用 ContextVar**：这里需要的作用域是「某一次工具调用的工作区」，
不是「某个 run」。用隐式状态会把「授权层带了 `workspace_id`、执行层没带」
那个老缺口换个形式请回来——两层读同一份上下文，才谈得上"同一个 id"。
`SkillLoadBudget` 用 ContextVar 是因为它的作用域天然等于 run，且账本只有消费者没有生产者。

**为什么不让 `resolve()` 直接读注册表**：注册表是运行期缓存，实时变化。
读它等于让"执行中被换绑"改变当前 run 的落点，一次 run 会跨两个工作区。
快照是「这次执行认定哪个根」，run 内只读一次。

`context=None` 是显式的"没有上下文"，只出现在面板与旧调用路径，行为与改造前逐字节相同。

---

## 2. 落点表（唯一决策点：`LocalSandbox._contextual_workspace_dir`）

| 输入 | 落点 | 创建 | 失败方式 |
| --- | --- | --- | --- |
| `context is None` | `workspace-volumes/{id}` | 按需 | — |
| `UNBOUND` | `workspace-volumes/{id}` | 按需（工作区初始化） | — |
| `BOUND_READY` | `local_root` | **不创建** | 目录已不在 → `WorkspaceUnavailableError` |
| `BOUND_MISSING` / `DENIED` / `OFFLINE` | 无 | 不创建 | `WorkspaceUnavailableError` |
| `BOUND_READY` 且 `prefix` 非空 | 无 | 不创建 | `SandboxPathError`（见 §4） |
| 路径指向另一个工作区 | 无 | 不创建 | `SandboxPathError`（见 §4） |

两条刻意的不对称：

- **`UNBOUND` 保留 mkdir**。「执行链不得创建权威根」只对绑定态成立；未绑定态的
  `volumes/{id}` 按需创建就是工作区初始化，删掉它新工作区就没有目录。
- **异常态不给任何根**。`root=None`，调用方必须先看 `state`。给一个"看起来能用"
  的 Volume 路径就等于允许静默回退——消费者只读 `root` 不看 `state` 时就会用错。

---

## 3. 实施中发现、必须一起修的三处

**① `virtual()` 也必须认识绑定根。**
解析换根之后，`files.write` 返回的 `path` 会变成宿主绝对路径（`C:\…\my-project\a.txt`）。
模型下一次把它原样传回来会被 `resolve()` 判成越界——表现是"刚写完就读不到"，
然后反复重试同一路径直到熔断。已在 `virtual()` 里加一段：绑定态下把
`local_root/…` 还原成 `/home/user/workspace/{id}/…`。
**这条不是锦上添花**：只改 `resolve()` 不改 `virtual()`，等于把路径分裂从"读"挪到"读回来的路上"。

**② `BOUND_READY` 要在每次解析时复验目录仍在。**
快照说 ready 不代表现在还 ready。下游有多个"看到路径就 mkdir 父目录"的调用点
（`LocalFiles.write` 的 `parent.mkdir`、`LocalCommands.run` 对 cwd 的 `mkdir`），
不在这里拦住，目录被改名后它们会在原路径重建一条空目录树——用户恢复原目录时
会看到两份内容不同。已改为：返回根之前 `is_dir()` 复验，失败抛
`WorkspaceUnavailableError`（带运行期原因，不用快照时的 detail）。
这对应核验记录 §1.4 的时序 ②：**运行期失效的产物是一次失败，不是一个新状态**。

**③ `WorkspaceUnavailableError` 增加可选 `detail`。**
快照时的探测原因与运行期失效原因不是一回事。硬塞进 `state` 就等于允许路径语义
在 run 内漂移；用 `detail` 覆盖文案则可以两者都说清。默认行为不变（`None` 用快照详情）。

---

## 4. 两种显式拒绝

**`prefix` 非空**：绑定时自动探测出前缀（`Data/` 这类）意味着虚拟视图比本地目录多一层。
而 Agent 的相对路径补全**不带**这一层，半映射的结果是"读得到一部分、写错另一部分"。
选择停住并给出可执行提示，而不是猜。这是既有设计的缺口（`_strip_prefix` 对空相对路径
返回 `None`，即"有前缀时虚拟根不对应本地目录"），不是 S2 引入的。

**路径指向别的工作区**：一次执行固定在一个工作区上。授权层 `_scope_violation`
正常情况下已拦住，这里是执行层第二道——两层用同一个 id 才谈得上"授权通过、执行同处"。

---

## 5. 已知过渡态（S4 之前）

绑定态下当前存在**视图分裂**，必须写清，不能当成已完成：

| 入口 | 绑定态看到什么 | 何时修 |
| --- | --- | --- |
| 文件工具（`files.*`） | 用户目录 | 已修（S2） |
| Git（`executor.py` → `commands.run`） | 用户目录（共用 `resolve()`） | 已修（S2） |
| Shell（`run_command`） | cwd 硬编码 `/home/user/work`；`workspace/{id}` 经联接看到 `workspace-volumes/{id}` | S4 |
| 面板（`local_workspace_path`） | 走 `resolve_workspace_path`，未消费快照 | S3 |
| `ls workspace`（shell 与文件工具都会） | `workspace-volumes` 根；绑定态不再创建 `{id}`，所以可能列不到当前工作区 | S4 |

三条边界：

1. **不是回归**：改造前文件工具与 Git 也看不到用户目录，现在它们能看到；
   剩下的入口维持原样，没有被新代码改坏。
2. **`_ensure_workspace_mount()` 在绑定态仍会执行**：它只确保 `{root}/workspace`
   联接与 `workspace-volumes` 根存在，**不创建 `{id}`**。撤掉它会让 shell 从
   "看到旧副本"退化成"目录不存在"，后者更难解释。
3. **`workspace/{id}` 是联接这条路仍然关闭**：S1 的实测结论没变（POSIX `find`
   默认不进入链接，且失败是静默的）。S4 需要另找机制，不能靠在这里建联接绕过。

---

## 6. 改动面

| 文件 | 改动 |
| --- | --- |
| `sandbox/local.py` | `_contextual_workspace_dir`（新）、`_volume_copy_dir`（原逻辑抽出）、`__init__`/`open`/`attach` 收 `context`、`virtual()` 加绑定分支、`workspace_context` 属性 |
| `sandbox/service.py` | `workspace(..., context=None)` 透传 |
| `sandbox/tools.py` | `execute(..., context=None)` 透传给 `Service.workspace` |
| `agentrun/execution.py` | `chat(..., workspace_context=None)` → 工具调用点透传 |
| `agentrun/service.py` | `_run` 接住快照并传给 `chat` |
| `workspace/context.py` | `WorkspaceUnavailableError` 增加可选 `detail` |

**未改**：`workspace/http.py`（面板）、`workspace/volume_service.py`、`volume_backend.py`、
`_op_sandbox_run_command` 的 cwd、`_translate`。

---

## 7. 验收

| 项 | 结果 |
| --- | --- |
| 新增 `tests/test_bound_workspace_paths.py` | 13 passed |
| 全量 `pytest tests` | **642 passed, 1 skipped**（S1 后 629 + 13） |
| 未绑定 18 条基线（S1 冻结，sha256 校验） | 原样通过 |
| 绑定态不创建 `workspace-volumes/{id}` | 断言覆盖（含写文件后） |
| 目录在 run 中期被删 | 抛 `WorkspaceUnavailableError`，且不重建 |
| 工具层端到端（write → list） | 文件落在绑定目录，返回路径是 `workspace/ws1/report.md` |
| **Git 真实执行**（手动探针，绑定一个 `git init` 过的目录） | `git status` / `git log` 均 `exit=0`；文件工具写入 `new.txt` 后 `git status` 返回 `?? new.txt`——**文件工具与 Git 看到同一片字节**；`workspace-volumes/ws1` 未创建 |

Git 这条是 S2 的核心证据：`_op_git` → `workspace_path()` → `executor.py` →
`commands.run(cwd="workspace/ws1")` → `resolve()`，与文件工具共用同一个决策点，
**没有为 Git 写第二份路径逻辑**。S5 只需在此基础上做 `status/log/diff/commit/push`
的完整回归，不需要改代码（前提是这个调用链不变）。

---

## 8. 下一步

- **S3**：面板与执行链同源；去掉"本地不存在 → 回落沙箱"。
- **S4**：Shell 的 cwd 与虚拟绝对路径映射取同一根（依赖按调用注入，已完成）；
  不宣称越界隔离。
- **S5**：Git 独立回归（`status/log/diff/commit/push` 在绑定仓库上跑通）。
- **不在这条线上**：P0-SEC-1（面板绕过敏感检查）、`_background` 生命周期、
  `_run` 首次调度前取消导致的租约残留。
