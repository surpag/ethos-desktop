# P1 权限系统技术方案（评审后修订版）

> 状态：**已实施（P1）**，代码在 `internal/permissions/`，验收 `tests/test_permissions.py`。
> 与方案的两处实际偏差记在文末 §13。
> 上一版：P0 只做事实核验，未改代码。本版按评审意见修订三处决策、补四个问题，
> 并给出副作用入口覆盖矩阵的设计口径。

## 0. 结论摘要

| 项 | 结论 |
| --- | --- |
| 完全档 | 方案 A。**结构化工具**无条件拦敏感路径；**Shell 不承诺**该保护，且命令层能力记为 `{EXEC, WRITE, EXTERNAL}` |
| 审批档 | P1 不挂起、不恢复。拒绝结果分两种 `kind`：`permission_denied` / `approval_required` |
| 敏感路径 | 扩展 `.ssh` / `*.pem` / `*.key` / `.aws` / `.config/gcloud` 等；规范化 + `..` + 符号链接 + 不可判定即拒绝 |
| 能力模型 | **集合**，不是互斥等级。`run_command = {EXEC, WRITE, EXTERNAL}`，EXEC 不「低于」WRITE |
| Cron | 创建即 `EXTERNAL`；落库权限快照；执行期用 `min(快照, 当前)` 重算，不继承创建时上下文 |
| Skills | 写入 `skills/**` 追加 `EXTERNAL`（效果跨 run），加载走既有路径校验 |
| 未知情形 | 六条 fail-closed 分支，任何一条都不进执行器 |
| 默认档位 | `full`（= 当前行为 + 既有敏感路径保护）。没有 UI 时下调默认值等于静默阉割功能 |

## 1. 问题空间枚举

### 1.1 谁能让事情发生

Agent 能产生副作用的入口只有五类。这是枚举的**上界**，P1 必须逐类给出判定位置：

| # | 入口 | 副作用的性质 | 落在哪 |
| --- | --- | --- | --- |
| E1 | 内置工具（4 个 tool set，16 个工具） | 直接改文件 / 起进程 / 写库 | `agentrun/execution.py::_run_one` |
| E2 | MCP 工具（运行时才知道名字与数量） | 完全未知 | 同上（同一分支，早于 `is_mcp` 分流） |
| E3 | `create_cron_job` | **未来某时刻**再跑一次 Agent | 同 E1（`EXTERNAL` 能力） |
| E4 | 写 `skills/**` | 影响**未来每一次 run** 的 system prompt | 同 E1（路径触发 `EXTERNAL` 追加） |
| E5 | 进程管理（`start_process` / `kill_process`） | 长期进程、端口、其它进程 | 同 E1（`EXEC`） |

E3/E4 的特别之处：它们本身不改变任何文件，但会把影响投射到**本次 run 之外**。这就是
`EXTERNAL` 这个能力的定义——不是「联网」，而是「效果不在本次执行的边界内」。

### 1.2 档位 × 能力

三个档位，五种能力。判定不是「比大小」，是**集合包含**：

| 档位 | 允许的能力集合 | 其余能力的结果 |
| --- | --- | --- |
| `read_only`（只读） | `{READ, METADATA}` | `permission_denied` |
| `approval`（审批） | `{READ, METADATA}` | `approval_required` |
| `full`（完全） | 全部 | —（不可能发生） |

15 格逐格结果：

| 工具能力 | read_only | approval | full |
| --- | --- | --- | --- |
| `{READ}` | 允许 | 允许 | 允许 |
| `{METADATA}` | 允许 | 允许 | 允许 |
| `{READ, EXTERNAL}` | 拒绝 | 待审批 | 允许 |
| `{WRITE}` | 拒绝 | 待审批 | 允许 |
| `{EXEC}` | 拒绝 | 待审批 | 允许 |
| `{EXTERNAL}` | 拒绝 | 待审批 | 允许 |
| `{WRITE, EXTERNAL}` | 拒绝 | 待审批 | 允许 |
| `{EXEC, WRITE, EXTERNAL}` | 拒绝 | 待审批 | 允许 |

`METADATA`（当前只有 `plan_update`）在三档都放行，理由与完成证据里的
`metadata_only` 完全一致：它只写我们自己库里的计划行，对用户可见的世界没有任何
影响。**如果把它判成 WRITE，只读档下模型连「列个计划」都做不到，而计划的丢失
正是续跑断裂的起点。**

### 1.3 未知情形（全部 fail-closed）

| 未知 | 结果 | 为什么不能默认放行 |
| --- | --- | --- |
| 工具没注册（模型幻觉出名字） | `permission_denied` + `tool_unknown` | 今天它走 `TOOL_FAILURE`，会被**算成系统故障**，三次就熔断整轮 |
| 工具没声明能力 | `permission_denied` + `capability_unknown` | 新增工具忘记登记 = 越权 |
| 参数不是合法 JSON | `invalid_arguments`，不执行 | 今天解析失败静默退化成 `{}` 并照常执行 |
| 路径无法判定（NUL / 绝对路径 / 解析异常） | `permission_denied` + `path_unresolvable` | 「说不清它指向哪」只能拒绝 |
| MCP 工具能力元数据 | 一律按 `{WRITE, EXEC, EXTERNAL}` | MCP 的 `readOnlyHint` 是**服务端自述**，不是可验证事实 |
| 档位值非法（库里脏数据） | 降级为 `read_only` 并告警 | 非法值当 full 等于把配置错误变成提权 |

## 2. 能力模型

### 2.1 五个能力

| 能力 | 含义 | 判据 |
| --- | --- | --- |
| `READ` | 只读取，不改变任何状态 | 无副作用 |
| `WRITE` | 改变本机文件/我们自己的业务数据 | 效果在本次 run 内可见 |
| `EXEC` | 执行任意程序，副作用由程序决定 | 无法在工具层枚举它做了什么 |
| `EXTERNAL` | 效果越出本次 run：远端、未来 run、跨 run 上下文 | 不联网也可能 EXTERNAL（cron / skills） |
| `METADATA` | 只动我们自己的元数据（计划行） | 对用户可见世界零影响 |

**EXEC 不是「比 WRITE 高一级」，它们是并列的两把刀。** 写成等级会立刻出错：
`if needed > WRITE: deny` 会放行 `{EXEC}`——而 Shell 能读写删文件、能联网、
能起后台进程。所以判定用 `needed ⊆ allowed`，不做任何大小比较。

### 2.2 声明表（唯一权威源 `permissions/catalog.py`）

| 工具 | 能力 | 备注 |
| --- | --- | --- |
| `get_current_time` | `{READ}` | |
| `sandbox_list_files` | `{READ}` | |
| `sandbox_read_file` | `{READ}` | 含 Skill 正文（L2 预算另算） |
| `sandbox_list_processes` | `{READ}` | 上一版漏在只读清单外（F3） |
| `sandbox_write_file` | `{WRITE}` | 目标在 `skills/**` → `+EXTERNAL` |
| `sandbox_make_directory` | `{WRITE}` | 同上 |
| `sandbox_move_path` | `{WRITE}` | `from`/`to` 任一在 `skills/**` → `+EXTERNAL` |
| `sandbox_delete_path` | `{WRITE}` | 同上 |
| `sandbox_run_command` | `{EXEC, WRITE, EXTERNAL}` | 不承诺敏感路径保护 |
| `sandbox_start_process` | `{EXEC, WRITE, EXTERNAL}` | 长期进程会占端口 |
| `sandbox_kill_process` | `{EXEC}` | 作用于本机进程 |
| `git` | **按子命令**：只读子命令 `{READ}`；`clone` `{WRITE, EXTERNAL}`；`add/commit` `{WRITE}`；`push/pull/fetch` `{WRITE, EXTERNAL}`；未知 `{WRITE, EXEC, EXTERNAL}` | 分类直接复用 `gitops.policy` 的四张白名单 |
| `git_push` | `{WRITE, EXTERNAL}` | 带用户凭据出网；改变的是远端仓库的状态 |
| `git_remote_branches` | `{READ, EXTERNAL}` | 只读，但出网 |
| `create_cron_job` | `{WRITE, EXTERNAL}` | 创建未来 run |
| `plan_update` | `{METADATA}` | |
| MCP 任意工具 | `{WRITE, EXEC, EXTERNAL}` | 无可信元数据 |

`git` 是唯一需要**看参数**才能定能力的工具——这正是
`Decision = decide(level, name, capabilities, arguments)` 里 `arguments` 那一项存在的理由。

### 2.3 一次声明，四处消费

能力声明同时驱动三件原本各有一份手工清单的事：

| 消费方 | 旧实现 | 新实现 |
| --- | --- | --- |
| 权限裁决 | 无 | `policy.evaluate` |
| 只读工具并发 / 结果缓存 | `_READ_ONLY_TOOLS` 手写集合（已漏 2 个） | `caps ⊆ {READ}` |
| 完成证据分级 | 名字前缀 `"__"` 判 MCP + 第二份只读清单 | 同上（`WRITE/EXEC`→mutated，`EXTERNAL`→external，`{METADATA}`→metadata） |
| 计划证据校验（`run_plan.py`） | 第三份只读清单 | 同上（`is_readonly`） |

第四份手工清单（`execution.py:136`）就此消失。这是 P1 里唯一一处「顺带重构」，
理由是：**留着它，权限判断与并发调度、证据分级会在同一天漂移成三个答案。**

## 3. 裁决点与不变量

### 3.1 位置

`agentrun/execution.py::_run_one` 内，**参数解析之后、`is_mcp` 分流之前**：

```
tool.started → 解析参数 → 【裁决】 → 只读缓存 → MCP / Registry 分发 → tool.finished
```

E1~E5 全部从这一个点过。MCP 不在 `ToolRegistry` 里（它每次 run 现开），所以裁决
**不能**放在 Registry 内——放那里等于 MCP 永远绕过。反过来，放在 `chat()` 之外
（例如 service 层）也做不到：那时还不知道模型要调哪个工具。

### 3.2 不变量

1. 一次工具调用最多经过**一次**裁决；
2. 裁决失败的结果**不进入**任何执行分支（含只读缓存——缓存命中也在裁决之后）；
3. 被拒绝的调用**计数**（可观测）、**不进模型历史以外的任何状态**（不改文件、不进
   计划证据、不推进 checkpoint 的业务结论）；
4. 拒绝不参与熔断（见 §7）；
5. 声明表没覆盖的工具名，运行时一律拒绝，构建期由测试报红（见 §9 最后一行）。

## 4. 敏感路径（安全约束，与档位正交）

### 4.1 归属

这是**不可配置的安全约束**，不是档位判定的一部分：三档都生效，`full` 也生效。
它留在路径语义所在的 `sandbox/tools.py`（那里才知道虚拟路径 → 宿主真实路径的
映射），档位裁决留在 `permissions/`。两者是两道独立的闸门，谁也不替代谁。

### 4.2 规则

| 类别 | 命中条件（段级，大小写不敏感，`\` 与 `/` 等价） |
| --- | --- |
| 版本库元数据 | 段等于 `.git` / `.hg` / `.svn` |
| 环境变量 | 段以 `.env` 开头（`.env`、`.env.local`、`.env.production`） |
| SSH | 段等于 `.ssh` / `id_rsa` / `id_ed25519` / `id_ecdsa` / `id_dsa` |
| 私钥 | 段以 `.pem` / `.key` / `.p12` / `.pfx` 结尾 |
| 云凭据 | 段等于 `.aws` / `.azure` / `.gnupg` / `credentials` / `credentials.json` / `auth.json` / `.netrc` |
| gcloud | 连续两段等于 `.config` + `gcloud` |

### 4.3 三个必须处理的细节

| 情况 | 处理 |
| --- | --- |
| 分隔符 / 大小写 | 统一 `\`→`/`、转小写后比较；`C:\Users\x\.SSH\id_rsa` 与 `/home/u/.ssh/id_rsa` 同判 |
| `..` 穿越 | 含 `..` 段 → 拒绝（不依赖上游 `workspace_path` 已经拦过；纵深防御） |
| 符号链接 | 逻辑路径未命中后，用 `LocalSandbox.resolve()` 取宿主真实路径再判一次；真实路径命中 = 拒绝 |
| 无法解析 | NUL 字符、含 `..`、真实路径越出沙箱根、`resolve` 抛异常 → 拒绝 |

符号链接这一条不是理论风险：工作区在宿主磁盘上，模型完全可以先
`sandbox_run_command ln -s ~/.ssh work/k`（`EXEC` 在 `full` 下放行），再
`sandbox_read_file work/k/id_rsa`。逻辑路径 `work/k/id_rsa` 命中了 `id_rsa` 所以
会被拦；但换成 `work/k/config` 就绕过了。真实路径复核把这条路堵死。

实现备注：`LocalSandbox.resolve()` 本身也会拒绝解析到沙箱根之外的路径，但那一层
的语义是「沙箱边界」而不是「敏感路径」——所以我们仍在自己的检查里先拒绝，
不把安全性建立在「下游碰巧也拦」之上。

### 4.4 覆盖面（F1/F2 修正）

| 旧行为 | 新行为 |
| --- | --- |
| 检查挂在 `if name in _FILE_TOOLS and workspace_id`（无工作区的会话可写 `work/.env`） | 去掉 `and workspace_id`，文件类工具无条件检查 |
| `run_command` 完全不受检查（`cat .env` 实测能读到） | **保持不受检查**，但它的能力是 `{EXEC, WRITE, EXTERNAL}`——只在 `full` 可达，且档位说明里明确写「Shell 不受该路径保护」 |
| `git add .env && git commit && git_push` 可把密钥推到远端 | `git add` 的显式路径参数纳入敏感检查（`add` 是白名单里唯一收路径参数的子命令） |

## 5. Skills 治理

单层目录（`skills/` = 宿主 `<data_root>/skills`，junction 进沙箱），所以问题不是
「作用域」，而是**跨 run 的持久化指令注入**：写一次 `skills/x/SKILL.md`，之后每一次
run 的 system prompt 都会带上它。

| 动作 | 能力 | 判定 |
| --- | --- | --- |
| 写 / 移入 / 删除 `skills/**` | `{WRITE, EXTERNAL}` | 只读拒绝、审批待审批、完全放行（并过敏感检查） |
| 读 `skills/**`（含 SKILL.md、scripts/、references/） | `{READ}` | 三档都放行；路径仍受 `workspace_path` 白名单与 junction 约束 |
| 改 `skills/` 之外的文件 | `{WRITE}` | 不受影响 |

追加 `EXTERNAL` 而不是判成「普通写入」，理由是**影响半径**：普通写入的效果在本
次 run 内可复核（下一步就能读到），而 Skill 写入的效果在**下一次 run 的 system
prompt 里**，本次 run 永远看不到自己造成了什么。

「Skill 内容不能提升权限」：Skill 正文只是注入到 system prompt 末尾的文本，
档位裁决在执行层、与 prompt 内容无关；`skills/**` 里也不可能出现新的工具定义
（工具 schema 来自 `tool_definitions()`，不读磁盘）。这一点 P1 用测试锁住：
在只读档下，即便目录里存在一个「声称已授权」的 Skill，`sandbox_write_file` 仍然被拒。

## 6. Cron 治理

### 6.1 三个问题

1. 创建时是 `full`，用户后来改成 `read_only` → 任务是否还能写？
2. 创建时是 `read_only`，用户后来改成 `full` → 任务是否自动获得写权限？
3. 任务以谁的身份跑？

### 6.2 结论

| 问题 | 结论 |
| --- | --- |
| 1 | 不能。执行时重算，取**更严**的一档 |
| 2 | 不能。快照是上限，升级不回溯 |
| 3 | 任务所有者（`job.user_id`），并且**显式传入**快照档位；不读「当前 run 的上下文」 |

```
effective = min(current_user_level, job.permission_level_snapshot)   # 序：read_only < approval < full
```

为什么要快照而不是只看当前值：否则「改一次全局设置」就等于给所有历史任务重新授权
——那是一次**不需要经过任何准入检查的批量提权**。

为什么还要和当前值取最小：用户把档位调低，是对**所有**未来执行的意图表达，定时
任务不能成为这条意图的例外。

### 6.3 落库与兼容

- `cron_jobs.permission_level TEXT NOT NULL DEFAULT ''`（老库 `ALTER TABLE ADD COLUMN`）；
- 创建时写入当前档位（`permissions.context.current_level()`，缺失时回落到用户设置）；
- 启动时 `initialize_next_runs` 把空快照的**历史行**回填为当时的用户档位，并打日志；
  之后不再有「空快照」这种状态；
- 空快照在执行期按「当前档位」处理（只对本次启动前就已存在的行可能发生，一次性）。

创建入口治理：`create_cron_job` 是 `{WRITE, EXTERNAL}`，只在 `full` 可达；工具结果
里回显 `permissionLevel`，让模型与用户都能看到「这条任务被授权到什么程度」。

## 7. 错误分类与熔断

### 7.1 显式枚举（不再靠 `retryable` 反推）

| `kind` | 含义 | `retryable` | 计熔断 | UI 状态 |
| --- | --- | --- | --- | --- |
| `tool_failure` | 系统故障（SDK/连接/超时） | false | **是** | failed |
| `command_error` | 业务错误（路径/参数/命令失败） | true | 否 | completed |
| `permission_denied` | 当前档位不允许 | false | **否** | failed |
| `approval_required` | 需要审批，P1 未实现 | false | **否** | failed |
| `invalid_arguments` | 参数不是合法 JSON | true | 否 | failed |
| 无 `kind`（MCP 原文） | 第三方结果 | — | 否 | completed |

熔断判据从 `retryable is False` 改为 `kind == tool_failure`。这一条是 F4 的修复：
否则只读档下模型试三次写文件，整轮就被熔断杀掉。

`approval_required` 与 `permission_denied` 分开，是为了 P2 接真审批时**不需要重设
错误体系**：前者将来会变成「挂起 + 等用户点」，后者永远是「换个档位或换条路」。

### 7.2 给模型的话

拒绝结果必须让模型**停止重试**而不是换着花样重试：

```
{"error": "...", "kind": "permission_denied", "retryable": false,
 "level": "read_only", "needed": ["write"],
 "suggestion": "这是用户的权限设置，不是参数问题，重试无效。请说明你需要做什么，由用户决定是否切换档位。"}
```

## 8. 档位语义与对外承诺

| 档位 | 实际承诺（写进设置页文案） |
| --- | --- |
| 只读 | 仅允许读取和查询（含列目录、看进程、看计划）；不允许写入、建/删/移文件、执行命令、起进程、创建定时任务、写 Skill。**Shell 一律不放行**，即使命令看起来只读——命令层无法可靠证明无副作用 |
| 审批 | 与只读相同的实际放行面，但拒绝理由是「待审批」（P1 未实现审批，P2 补） |
| 完全 | 无需审批即可执行允许的操作。**结构化工具仍拦敏感路径**；Shell 不受该路径保护（`cat .env` 可读），这是已知且明示的取舍 |

敏感路径保护**不是**档位承诺的一部分，而是三档共有的硬约束——所以它写在
「安全约束」而不是「档位说明」里，避免用户以为「完全档 = 什么都不管」。

## 9. 副作用入口覆盖矩阵

实施完成后逐行填「测试用例」，P1 验收时这张表就是交付物本体。

| # | 入口 | 能力 | 裁决位置 | 只读档 | 审批档 | 完全档 | 测试 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `sandbox_read_file` | `{READ}` | `_run_one` | 允许 | 允许 | 允许 | `test_read_tools_allowed_at_every_level` |
| 2 | `sandbox_list_files` / `list_processes` / `get_current_time` | `{READ}` | 同上 | 允许 | 允许 | 允许 | 同上 |
| 3 | `sandbox_write_file` | `{WRITE}` | 同上 | 拒绝 | 待审批 | 允许 | `test_write_tools_matrix` |
| 4 | `sandbox_make_directory` / `move_path` / `delete_path` | `{WRITE}` | 同上 | 拒绝 | 待审批 | 允许 | 同上 |
| 5 | `sandbox_run_command` | `{EXEC,WRITE,EXTERNAL}` | 同上 | 拒绝 | 待审批 | 允许（**无**路径保护） | `test_shell_is_full_only` |
| 6 | `sandbox_start_process` / `kill_process` | `{EXEC,…}` | 同上 | 拒绝 | 待审批 | 允许 | 同上 |
| 7 | `git status/log/diff` | `{READ}` | 同上（看参数） | 允许 | 允许 | 允许 | `test_git_capabilities_follow_subcommand` |
| 8 | `git add/commit` | `{WRITE}` | 同上 | 拒绝 | 待审批 | 允许 | 同上 |
| 9 | `git clone` / `push`(应被白名单拒) | `{WRITE,EXTERNAL}` | 同上 | 拒绝 | 待审批 | 允许 | 同上 |
| 10 | `git_push` | `{EXTERNAL}` | 同上 | 拒绝 | 待审批 | 允许 | `test_external_tools_matrix` |
| 11 | `git_remote_branches` | `{READ,EXTERNAL}` | 同上 | 拒绝 | 待审批 | 允许 | 同上 |
| 12 | `create_cron_job` | `{WRITE,EXTERNAL}` | 同上 | 拒绝 | 待审批 | 允许 + 记快照 | `test_cron_job_records_permission_snapshot` / `test_cron_effective_level_is_min_of_snapshot_and_current` |
| 13 | 写 `skills/**` | `{WRITE,EXTERNAL}` | 同上（参数追加） | 拒绝 | 待审批 | 允许 | `test_skills_write_is_external` |
| 14 | MCP 任意工具 | `{WRITE,EXEC,EXTERNAL}` | 同上（`is_mcp` 之前） | 拒绝 | 待审批 | 允许 | `test_mcp_tools_are_never_allowed_below_full` |
| 15 | `plan_update` | `{METADATA}` | 同上 | 允许 | 允许 | 允许 | `test_metadata_allowed_at_every_level` |
| 16 | 敏感路径（文件类） | — | `sandbox/tools.py` | 拒绝* | 拒绝* | **拒绝** | `test_sensitive_paths_are_matched` / `test_sensitive_path_enforced_without_workspace` |
| 17 | 敏感路径（`git add` 显式路径） | — | `sandbox/tools.py` | 拒绝* | 拒绝* | **拒绝** | `test_git_add_sensitive_path` |
| 18 | 符号链接 / 目录联接指向敏感路径 | — | `sandbox/tools.py`（真实路径复核） | 拒绝* | 拒绝* | **拒绝** | `test_symlink_escape_to_ssh_key` |
| 19 | 未注册工具名 | 未知 | `_run_one` | 拒绝 | 拒绝 | 拒绝 | `test_unknown_tool_denied_at_every_level` |
| 20 | 未声明能力的已注册工具 | 未知 | `_run_one` | 拒绝 | 拒绝 | 拒绝 | `test_undeclared_tool_denied` |
| 21 | 参数非 JSON | 未知 | `_run_one` | 拒绝 | 拒绝 | 拒绝 | `test_invalid_arguments_denied` |
| 22 | `sandbox_delete_path <目录>`（目录内含敏感条目） | `{WRITE}` | `sandbox/tools.py`（条目展开） | 拒绝* | 拒绝* | **拒绝** | `test_delete_path_checks_entries` |
| 23 | `git add <目录>`（展开后含敏感条目） | `{WRITE}` | `gitops.plan_git_add`（问 git） | 拒绝* | 拒绝* | **拒绝** | `test_git_add_directory_checks_expansion` / `test_git_add_directory_ignores_gitignored_secrets` |
| 24 | `git add` 无法核对暂存计划 | 未知 | 同上 | 拒绝 | 拒绝 | 拒绝 | `test_plan_git_add_fails_closed` / `test_plan_git_add_surfaces_git_error` |

\* 16~18、22~23 在低档位下被档位裁决先拦住，测的是「`full` 也拦」这一列。
22~24 是 P2 补的（P1 交付时这三条是敞开的，见 §14/§15）。

## 10. P1 不做

- 审批挂起 / 恢复（`approval_required` 只是拒绝理由）；
- 会话级、工作区级覆盖（只有用户级全局档位）；
- 前端档位切换 UI（只加后端字段与接口）。**已在 P2-3 完成**：BFF 的
  `parseSaveUserSettingsRequest` 已放行 `permissionLevel`（严格三档，非法整份拒绝），
  设置页有「执行权限」三选一；见 `permissions-p2-approval.md` §14.4；
- Shell 命令层的敏感路径保护；
- **用户自己的操作**不经过档位：文件面板、图片上传、设置页是人在操作，档位约束的
  是 Agent（`sandbox/http.py` 因此一行都没改）；
- 大规模重构：只把「裁决 + 能力声明」抽成新模块，其余复用现有执行链。

## 11. 改动清单

| 文件 | 改动 |
| --- | --- |
| `internal/permissions/{types,catalog,policy,context,paths}.py` + `README.md` | 新增：能力枚举、声明表、裁决、上下文、敏感路径匹配 |
| `internal/tools.py` | 错误 `kind` 枚举与 `kind → retryable/熔断/状态` 的唯一映射；未注册工具改判 `command_error` |
| `internal/agentrun/execution.py` | `_run_one` 加裁决；`chat(permission_level=)`；删 `_READ_ONLY_TOOLS`；证据分级改声明驱动；熔断改按 `kind`；参数解析失败不再执行 |
| `internal/agentrun/run_plan.py` | 第三份手写只读清单 → `is_readonly`（P0 未发现的第四份清单） |
| `internal/agentrun/service.py` | 解析生效档位（`min(当前, 请求上限)`）、绑定上下文、传进 `chat` |
| `internal/agentrun/types.py` | `Request.permission_level`（执行上限） |
| `internal/gateway/types.py` | `IncomingMessage.permission_level` |
| `internal/cronjob/{schema,store,tools,types}.py` | 快照列、创建时写入、回填、结果回显 |
| `internal/cronadapter/adapter.py` | 传快照档位（执行身份 = 任务所有者） |
| `internal/userconfig/{schema,settings,types,http}.py` | `user_agents.permission_level` 列与读写 |
| `internal/sandbox/tools.py` | 敏感检查去 `workspace_id` 条件、扩展规则、真实路径复核、`git add` 路径检查 |
| `tests/test_permissions.py` | 新增覆盖矩阵 24 行 + 声明完整性 + HTTP 边界（60 例） |

## 12. 验收

```bash
cd backend-py
./.venv/Scripts/python.exe -m pytest tests --basetemp=.pytest-tmp-<新名字> -q
# 491 passed, 1 skipped（其中 tests/test_permissions.py 60 例）
```

外加三条手工验证（不进单测，因为要真沙箱 + 真模型）：

1. 只读档下发一句「在 README 里加一行」→ 工具卡显示 `permission_denied`，文件未变，
   没有熔断，run 正常结束（`end_reason=completed`）；
2. `full` 档下 `sandbox_read_file work/.env` → 拒绝；`sandbox_run_command cat work/.env`
   → 能读到（这是明示取舍，不是 bug）；
3. 创建一个 `full` 档的 cron 任务 → 改用户档位为 `read_only` → 到点执行时该任务
   的写操作被拒，`cron_jobs.permission_level` 仍是 `full`（快照未被改写）。

## 13. 实施中与方案的偏差（如实记录）

| 项 | 方案 | 实际 | 原因 |
| --- | --- | --- | --- |
| 手写只读清单的数量 | P0 认定 3 份（`execution` / `run_plan` / 证据分级） | **4 份**：`run_plan.py` 还有一份独立的 `_READ_ONLY_TOOLS` | 它是「计划证据校验」用的，与执行层的清单各自演化；这次一并换成能力声明 |
| 敏感路径拒绝的 `kind` | 沿用 `command_error` | 改为 `permission_denied`（`reason: sensitive_path`） | 命中敏感段时**换哪个档位都成功不了**，是可确定的拒绝；写成业务错误会让模型按「换个路径再试」处理，还会把一次被拒的写入算进「已产生变更」的证据 |
| 符号链接保护 | 靠真实路径复核兜住 | 复核先拒绝；`LocalSandbox.resolve()` 本身也拒绝解析到沙箱根之外 | 两层都在，但语义不同：一层是沙箱边界，一层是敏感路径。不把安全性建立在「下游碰巧也拦」之上 |
| 参数解析失败 | 新增 `invalid_arguments` 拒绝 | 同方案 | 旧行为是静默退化成 `{}` 继续执行——格式错误会被放大成一次破坏 |

未做但值得记住的一条：`sandbox_run_command` 在完全档下**仍可读 `.env`**。这不是遗漏，
是方案 A 明示的取舍（命令层无法可靠证明无副作用）。要堵它只有一条路——把 shell 也
纳入路径检查，那需要命令解析器，且注定被 `bash -c` 绕过；真正的解法是别把密钥放在
Agent 可达的磁盘上。

## 14. 已知边界（P1 交付时敞开，P2 已关闭）

P1 只判「这次操作指向哪个路径」。下面两个入口的目标路径可以完全干净，真正起作用的是
目录里的每一条：

| 入口 | 现象 | 为什么路径检查挡不住 | 影响 |
| --- | --- | --- | --- |
| `sandbox_delete_path <目录>` | 目录名干净，内含 `.env` / `.git` | 敏感判定是**逐条路径**的，而递删是**容器操作**：目标路径本身没有任何敏感段 | 破坏：密钥文件被连带删除；不构成泄露 |
| `git add <目录>` | 目录名干净，内含 `.env` | 同上：白名单只看参数里的路径串，不看被展开的文件集合 | **外送前置**：暂存后 `commit + git_push` 就能把密钥推到远端 |

P1 交付时这两条是敞开的（当时只有档位兜着：只读/审批档根本用不了这两个工具）。
P2 用「按条目判定」把它们关掉了，见 §15。

## 15. P2：按条目判定（关闭 §14 的两条）

### 15.1 两个入口，两种问法

同一个原则（看展开后的每一条），但取证方式必须不同：

| 入口 | 问谁 | 实现 | 为什么不能互换 |
| --- | --- | --- | --- |
| `sandbox_delete_path <目录>` | 文件系统 | `paths.first_sensitive_entry` 遍历子树，`sandbox/tools.py::_sensitive_subtree_violation` | 递删没有「先报告再执行」的通道，只能自己走一遍 |
| `git add <目录>` | git | `gitops.plan_git_add`（`git add -n`，只出计划不写索引） | 展开结果受 `.gitignore` 与 pathspec 影响，自己走目录会把被忽略的 `.env.bak` 也当成「会被暂存」 |

被忽略的文件其实不会被暂存，所以用遍历去判 `add` 会**误拒**。误拒比漏判安全，但会让人
开始怀疑这条规则到底在保护什么——一旦如此，下一个人就会把它注释掉。

### 15.2 拒绝发生在写入之前

`git add` 采用「先问计划、再执行」，不是「先暂存、发现敏感再撤回」：

- 不产生「已暂存、又被撤回」的中间态。那种中间态解释不清（谁能证明撤干净了？），
  而撤回本身是第二次写入——安全检查不该以先破坏为前提；
- 失败时 `git diff --cached --name-only` 必须为空 —— 这是可断言的事实，写进了
  `test_git_add_directory_checks_expansion`。

计划拿不到（非仓库、路径不存在、输出无法解析）时 `reason` 是 `sensitive_path_unverified`
而不是 `sensitive_path`：前者是「换个问法」（换成具体文件），后者是「换个路径」。两者
都是 `permission_denied` + `retryable=false`，都不计熔断。

### 15.3 仍然敞开的三条

| 敞开的入口 | 为什么不再堵 |
| --- | --- |
| `sandbox_run_command` 读 `.env` | 方案 A 的明示取舍（§13 末）：命令层无法可靠证明副作用 |
| `bash -c "git add .env && git commit && git push"` | 通用绕道。给 git 再加检查不影响这条，只会让人以为已经堵住了 |
| 索引里已有敏感文件时的 `commit` | 只能靠上面那条绕进去。同一入口既然能直接 `commit && push`，在 `commit` 上再加一道就是剧场 |

一句话：**容器层的洞可以逐个堵，命令层的洞堵不住。** 所以真正的位置是「别把密钥放在
Agent 可达的磁盘上」，而不是继续在路径字符串上加判断。

有一条副作用要提前说清：**删一个内含 `.git` 的项目目录现在会被拒**（`.git` 在敏感表里）。
这是刻意的——整目录删除不该是「顺手」操作，而 `.git` 被连带删掉会让 git 能力整段失效。
代价可控：`sandbox_delete_path` 与 `sandbox_run_command` 都只在 `full` 档可用，所以
任何「能删」的档位也「能跑 shell」，没有真正的功能损失，只是把一次误删变成了必须显式
选择的行为。

### 15.4 验收

```bash
./.venv/Scripts/python.exe -m pytest tests/test_permissions.py tests/test_gitops.py -q
# 105 passed（新增 5 例入口级 + 3 例 plan 解析）
```

