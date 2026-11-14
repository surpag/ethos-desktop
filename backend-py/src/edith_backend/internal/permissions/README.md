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

## ⚠️ 新增工具时必须做的事

1. 在 `catalog.TOOL_CAPABILITIES` 加一行（否则运行时默认拒绝）；
2. 若工具接收**路径参数**，在 `sandbox/tools.py` 的 `_PATH_FIELDS` 登记（敏感路径
   检查靠它知道哪些参数是路径）；
3. 跑 `tests/test_permissions.py::test_every_registered_tool_is_declared`。

## 已知边界（路径层挡不住「容器层」）

敏感检查保护的是「这次操作指向哪个路径」，不是「这个路径里面每一条」：

- `sandbox_delete_path <目录>`：目录名干净、内含 `.env`，会被连带删掉；
- `git add <目录>`：同上，暂存集合里会出现 `.env`，之后 `commit + push` 就能外送。

堵它们要在「按条目判定」的位置做（删除前遍历、`add` 后核对 `git diff --cached
--name-only`），不是再加一层路径字符串判断。详见 `docs/permissions-p1.md` §14。
