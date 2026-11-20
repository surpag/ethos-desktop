# P0-2 S4：Shell 的路径视图与文件工具同根

**状态**：已实施。前置：S2（文件工具与 Git 落 `local_root`）、S3（面板同源）。
**范围**：只改「进程类工具以哪个目录为起点」和「命令里的虚拟路径怎么翻译」。

---

## 0. 一句话结论

S2/S3 之后文件工具、Git、面板都落在用户目录，但 shell 还停在旧视图：

> cwd 固定 `work/`，`_translate` 只换 `/home/user` 一个前缀 → 命令里的
> `workspace/{id}/…` 经 `{root}/workspace` 联接落到 `workspace-volumes/{id}`。

于是同一轮里「文件工具写用户目录、shell 看到旧副本」。这不是"少了一个 if"，
而是**两个视图各自有一份根**：shell 的根来自联接，文件工具的根来自快照。

---

## 1. 分裂的两条路径

| 入口 | 改造前取到的根 | 绑定态是否正确 |
| --- | --- | --- |
| 文件工具 `resolve()` | `_workspace_volume_dir` → 快照 | ✅ S2 已改对 |
| Git（`cwd=workspace/{id}`） | 同上（同一个 `resolve()`） | ✅ S2 顺带改对 |
| 面板直读 | `lookup_workspace_path` | ✅ S3 已改对 |
| **Shell 进程** | `WORKSPACE.work_path()` + `_translate` 单前缀 | ❌ 本次 |

Shell 这一格有**两个**独立缺陷，缺一个都还是分裂：

1. **cwd**：`work_path()` 是沙箱树里的 `work/`，与工作区无关。模型 `ls` 看到的是
   空的临时目录，于是它转而写绝对路径——然后撞上第 2 条。
2. **`_translate`**：`/home/user` → 沙箱根是一次性整体替换，`/home/user/workspace/ws1`
   变成 `<sandbox>/workspace/ws1`，再经联接落到 `workspace-volumes/ws1`。
   **文件工具写哪儿它不看**。

---

## 2. 改动：三处取值同源

### 2.1 cwd 交给 `resolve()` 决定

`_command_cwd(workspace_id)` 返回**虚拟路径** `workspace/{id}`，由
`LocalCommands.run` 里既有的 `self._sb.resolve(cwd)` 落地：

| 状态 | `resolve(workspace/{id})` |
| --- | --- |
| 绑定就绪 | 用户目录（并复验仍存在） |
| 未绑定 | `workspace-volumes/{id}`，按需创建 |
| 异常态 | 抛 `WorkspaceUnavailableError`（fail-closed） |

**为什么不在工具层拼真实路径**：绑定判定只有一处（`_workspace_volume_dir`）。
在第二处拼路径，两处不同步就是"S4 修好了、下次换绑又分裂"。工具层只需要说
"我要这个工作区"，落到哪儿是沙箱层的职责。

没有工作区的对话（`workspace_id` 为空）仍然用 `work/`——那种对话没有项目文件，
`work/` 就是它的全部。

### 2.2 `_translate` 改为最长前缀分流

`_virtual_pairs()` 生成一张有序表（长的在前），`_translate` 逐条替换：

| 虚拟路径 | 绑定态 | 未绑定态 |
| --- | --- | --- |
| `/home/user/workspace/{id}` | 用户目录 | `workspace-volumes/{id}` |
| `/home/user/workspace` | `workspace-volumes` | 同左 |
| `/home/user/skills` | `data_root/skills` | 同左 |
| `/home/user` | 沙箱根 | 同左 |

**顺序不可交换**：`/home/user/workspace/ws-1` 必须由第一条吃掉。若先匹配
`/home/user/workspace`，绑定态就又被翻译回旧副本——这正是 S4 要消灭的那一格。

**边界**：前缀后面必须跟 `/`、空白、引号或命令分隔符，否则
`workspace/ws-10` 会被 `workspace/ws-1` 误伤（有测试守着）。

**顺带的变化**：`workspace/{id}` 与 `skills` 现在直接换成真实根，不再"经联接"。
两条路等价，但少一跳联接就少一种失败模式（联接被改指/被删）。联接本身**保留**：
未绑定态与面板仍在使用，见 §4。

### 2.3 `_normalize` 的反向映射

`_path_pairs()` 增加绑定根 ↔ `workspace/{id}`。少了它，`ls` 的输出里是
`C:\Users\…\my-project\a.txt`，模型照着传回来会被 `resolve()` 判越界。

反斜杠形式（`str(path)`）替换后会在虚拟前缀后留下 `\`，所以再拉平一次：
`/home/user/workspace/ws1\app.py` → `/home/user/workspace/ws1/app.py`。

---

### 2.4 工具描述必须同步改

描述不改，模型仍然会按旧文案往 `work/` 写，然后奇怪为什么文件不在项目里。改了两处：

- `tool_guide()`：明确「文件工具的路径相对 Sandbox 根」与「run_command 的 cwd 是工作区根」
  是两套基准，沙箱级的 `work/`、`artifacts/` 要用绝对虚拟路径 `/home/user/work/`。
- `run_command` / `start_process` 的动作描述：`work/` → 「当前工作区目录」。

**代价**：工具描述进 system prompt，这一改会让所有会话的跨 run 缓存失效一次。
和 B2-3 那次一样，是刻意的**一次性全量**——两套文案并存时没法用一套断言覆盖。

---

## 3. 两处行为变更（不是局限，是刻意的）

| 变更 | 改造前 | 现在 | 为什么 |
| --- | --- | --- | --- |
| 未绑定工作区的 shell cwd | 沙箱树 `work/` | `workspace-volumes/{id}` | 两个状态两个 cwd 就是新的分裂；统一到"工作区根"，绑定与否只换根的位置 |
| shell 里相对 `artifacts/`、`uploads/` | 落到 `work/artifacts/…` | 落到工作区下 | 同上。要访问沙箱级目录必须写 `/home/user/artifacts/…`，描述里已写明 |

对既有会话的影响：模型此前在 `work/` 下建的文件仍在 `work/`（不迁移、不删除），
但它的默认 cwd 变了。模型会先用 `ls` 探索，看到的是项目文件而不是临时产物——
这正是想要的方向。

---

## 4. 为什么不把 `workspace/{id}` 做成指向绑定根的联接

这是最直觉的方案，也最糟：

| 问题 | 说明 |
| --- | --- |
| 目标位已有真实目录 | `{root}/workspace/{id}` 经父联接落在 `workspace-volumes/{id}`，是**真实目录**（可能有旧副本）。要改成联接必须先删/移走它——静默动用户数据 |
| 两种状态要正反维护 | 绑定态指向用户目录，解除绑定后必须摘回，否则 `workspace/{id}` 悬空或指向旧路径 |
| 悬空联接的表现更差 | 用户目录被删后联接还在，`ls` 报"设备不可用"，比"目录不存在"更难解释 |
| 与失败关闭冲突 | 联接一旦建好，进程能绕过 `resolve()` 直接走文件系统——绑定根不可用时的 fail-closed 就失效了 |

改成"路径翻译"之后，绑定态**没有任何磁盘状态**：换绑、解绑、目录消失，
下一次 `_translate`/`resolve` 自然得到新结论。

---

## 5. 不宣称越界隔离

`run_command` 仍然是 `unrestricted`：命令能读整台机器，`cd ../../../` 能出沙箱。
S4 只保证一件事——**模型写的虚拟路径指向它以为的那份数据**。

这也是保留 `{root}/workspace` 联接的原因：S3 之前面板与未绑定态都靠它，
撤掉会让 `ls workspace` 从"看到旧副本"退化成"目录不存在"，后者更难解释。

---

## 6. 已知局限（诚实列出）

| 局限 | 影响 | 为什么不在 S4 修 |
| --- | --- | --- |
| 原生程序不认 `/d/…` | `python /home/user/workspace/{id}/x.py` 会失败（`_shell_path_of` 输出 MSYS 形式） | **既有问题**，S4 之前同样存在。改翻译形式会影响所有命令，且需要先枚举"哪些程序是原生、哪些是 MSYS"；实测 `D:/…` 形式两类都认，可作后续独立项 |
| `cd /home/user && ls workspace/{id}` 仍看旧副本 | 先上溯到沙箱根、再用相对路径进工作区 | 联接还在（见 §4）。模型最常用的是「当前目录」和「绝对虚拟路径」，两种都已同源 |
| `cd ../../../` 可出沙箱 | 既有语义 | `run_command` 本来就不是沙箱边界，M3 runner 才是 |
| 绑定根被删后 shell 起不来 | 抛 `WorkspaceUnavailableError`，run 失败 | 这是刻意的 fail-closed：宁可停下来，也不在旧副本上继续跑 |

---

## 7. 改动面

| 文件 | 改动 |
| --- | --- |
| `sandbox/tools.py` | 新增 `_command_cwd`；`run_command` / `start_process` 接受 `workspace_id` 并用它做 cwd；新增 `_WORKSPACE_SCOPED_TOOLS`（原先只有文件类工具拿得到 `workspace_id`）；两处工具描述；**`_sensitive_real_violation` / `_sensitive_subtree_violation` 不再把 `WorkspaceUnavailableError` 吞成「路径无法解析为真实位置」**（S4 之后 cwd 也走 `resolve()`，绑定失效会更容易撞上这个兜底，详见 S5/S6 文档 §2） |
| `sandbox/local.py` | `_translate` 改为最长前缀分流（新增 `_virtual_pairs`、`_replace_virtual_prefix`）；`_path_pairs` 增加绑定根与分隔符拉平 |
| `workspace/layout.py` | `tool_guide()` 写清两套路径基准 |
| `tests/test_shell_path_view.py` | 新增 7 项 |

**未改**：`_shell_path_of` 的形式、联接维护、前端、Git 路径逻辑。

`LocalCommands.run` 里对 `work_dir` 的 `mkdir` **保留**：绑定态的执行到不了那一行
（`resolve()` 已经复验目录存在并返回），未绑定态它是目录初始化的唯一入口。

---

## 8. 验收

| 项 | 结果 |
| --- | --- |
| `tests/test_shell_path_view.py` | 7 passed |
| 未绑定 18 条基线 | 逐字节通过（`_translate` 不参与基线，基线守的是 `resolve()`） |
| 全量 `pytest tests` | **672 passed, 1 skipped**（S3 后 659 → S4 +7 → S5 +3 → S6 +3） |

覆盖的是行为而不是实现：`_command_cwd` 的两个分支、工具层默认 cwd 落到绑定根、
绝对虚拟路径读到用户文件、兄弟工作区前缀不误伤、`work/` 不跟着漂、
输出归一化无混合分隔符、shell 写的文件当轮被文件工具读到。

---

## 9. 下一步

S5（Git 回归）与 S6（组合验收）已完成，见 `p0-2-s5-s6-acceptance.md`。
P0-2 主体到此收尾；P0-SEC-1（面板绕过敏感检查）仍独立。
