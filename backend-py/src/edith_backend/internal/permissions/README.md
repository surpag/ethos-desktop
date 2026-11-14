# permissions

Agent 的权限系统。**执行层只负责执行，是否允许执行只在这里回答。**

方案与裁决矩阵见 `docs/permissions-p1.md`；本文件只讲代码怎么读。

## 两层，不可互相替代

| 层 | 文件 | 可配置 | 作用域 |
| --- | --- | --- | --- |
| 档位裁决 | `policy.py` | 是（用户设置） | 只读 / 审批 / 完全 |
| 安全约束 | `paths.py` | **否** | 三档皆拦，`full` 也拦 |

档位是「用户想让我做多少事」，安全约束是「无论用户想做什么都不做」。把后者做成
前者的一部分，就等于宣布「完全档没有任何底线」。

## 能力是集合，不是等级

```python
run_command = {EXEC, WRITE, EXTERNAL}
git_push    = {WRITE, EXTERNAL}
plan_update = {METADATA}
```

判定是 `needed ⊆ allowed`，不做任何大小比较。写成等级会立刻出错：`EXEC > WRITE`
会让一条 shell 命令（能读写删文件、能联网、能起后台进程）被当成「只需要写权限」。

`EXTERNAL` 不是「联网」，而是「效果不在本次执行的边界内」：创建定时任务（未来跑
一次 Agent）、写 `skills/**`（进未来每一次 run 的 system prompt）都是 EXTERNAL，
尽管它们一个字节都没出本机。

## 一次声明，四处消费

`catalog.TOOL_CAPABILITIES` 是唯一权威源，被四处读取：

| 消费方 | 位置 |
| --- | --- |
| 权限裁决 | `policy.evaluate` |
| 只读工具并发 / 结果缓存 | `agentrun/execution.py`（原 `_READ_ONLY_TOOLS`） |
| 完成证据分级 | `execution.completion_evidence`（原 `"__" in name` 判 MCP） |
| 计划证据校验 | `agentrun/run_plan.py`（原第三个手写只读清单） |

上一版有**四份**手工清单各自演化，其中两份已经漏登记 `git_remote_branches` 与
`sandbox_list_processes`，后果是「查一下」被算成「改过」。

## 判定顺序（不可交换）

1. 参数能否解析 → 不能则拒绝（`invalid_arguments`，不再退化成 `{}` 继续执行）；
2. 工具是否有能力声明 → 没有则拒绝（`capability_unknown` / `tool_unknown`）；
3. 能力是否被当前档位允许 → 不允许时，审批档报 `approval_required`，其余
   `permission_denied`。

先确认「我知不知道它要干什么」，再问「用户允不允许」。反过来就会出现「未知工具
因为档位是 full 而被放行」。

## 工具结果分类（`types.py`）

| `kind` | retryable | 计熔断 | UI |
| --- | --- | --- | --- |
| `tool_failure` | false | **是** | failed |
| `command_error` | true | 否 | completed |
| `permission_denied` | false | 否 | failed |
| `approval_required` | false | 否 | failed |
| `invalid_arguments` | true | 否 | failed |

熔断判据是 `kind == tool_failure`。旧实现用 `retryable is False` 反推，于是只读档下
连试三次写文件 = 三次「系统故障」= 整轮任务被杀。

## 上下文（`context.py`）

只用于**记录**（`create_cron_job` 写权限快照），不用于**判断**。默认值是 `None`：

- `None` → 调用方必须显式决定（cron 工具回落到用户设置并写进结果）；
- 默认成 `FULL` 等于给任何漏传的长路径发一张全额授权书。

## 容器操作要按条目判（`paths.first_sensitive_entry`）

`check_path` 只看得到信封。`rm -rf work/archive`、`git add work/archive` 的目标路径
一个敏感段都没有，真正被删/被暂存的是目录里的每一条。所以这两个入口有两层问法：

| 入口 | 问谁 | 位置 |
| --- | --- | --- |
| `sandbox_delete_path <目录>` | 问文件系统（`first_sensitive_entry` 遍历） | `sandbox/tools.py::_sensitive_subtree_violation` |
| `git add <目录>` | 问 git（`git add -n` 只出计划、不写索引） | `gitops/executor.py::plan_git_add` |

为什么不是都用遍历：`git add` 的展开结果受 `.gitignore` 与 pathspec 影响，自己走目录
会把被忽略的 `.env.bak` 也算成「会被暂存」——误拒比漏判安全，但会让人开始怀疑这条
规则在保护什么。删除没有「先报告再执行」的通道，只能自己遍历。

仍未被覆盖的两条（如实记录，不是遗漏）：

- `sandbox_run_command` 与 `bash -c "git ..."` 是**通用绕道**，路径层与条目层都挡不住
  ——这是方案 A 的明示取舍（命令层无法可靠证明副作用），真正的解法是别把密钥放在
  Agent 可达的磁盘上；
- 提交前不复核索引：如果索引里已经有敏感文件（只能靠 shell 绕进去），`commit` 不会
  再拦一次。加这一层没有意义——同一条 shell 能直接 `commit && push`。

## ⚠️ 新增工具时必须做的事

1. 在 `catalog.TOOL_CAPABILITIES` 加一行（否则运行时默认拒绝）；
2. 若工具接收**路径参数**，在 `sandbox/tools.py` 的 `_PATH_FIELDS` 登记（敏感路径
   检查靠它知道哪些参数是路径）；
3. 跑 `tests/test_permissions.py::test_every_registered_tool_is_declared`。

## 已知边界（路径层 + 条目层之外）

敏感检查保护的是「这次操作指向哪个路径」以及「这个路径展开后有哪些条目」，
但不能阻止一条命令自己去找东西：`sandbox_run_command` 在 `full` 档仍能读到 `.env`
（方案 A 的明示取舍，见 `docs/permissions-p1.md` §13 末）。
