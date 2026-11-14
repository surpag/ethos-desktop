# P2-2 审批挂起与恢复

> 状态：**已实施**。代码在 `agentrun/approval.py` + `session/approval_store.py`，
> 验收 `tests/test_approvals.py`（16 例）。上游：`permissions-p1.md`（P1 交付时
> `approval_required` 只是拒绝理由）。
> 本文只解决一件事：**审批档下的一次工具调用怎么停下来等人、人怎么说、说完怎么接着走。**

## 0. 结论摘要

| 项 | 结论 |
| --- | --- |
| 挂起方式 | **同一个 run 停下来等**，不结束 run 再续跑（理由见 §2） |
| 批准粒度 | 绑定 `request_id + tool_call_id + 参数`；参数不同必须重新问 |
| 授权范围 | `once`（只这一次）/ `run`（本次运行内同名工具不再问），**不跨 run** |
| 无人在场 | 定时任务等 headless 执行**不等待**，直接拒绝（§9） |
| 批准 ≠ 放行 | 批准只解除档位裁决；敏感路径等安全约束照旧生效（§5） |
| 落库 | `run_approvals` 一张表，用途是审计与刷新后展示，**不是恢复依据** |
| 超时 | 10 分钟；超时按拒绝处理，run 继续（模型可换策略） |
| 进程重启 | pending 行启动时统一置 `expired`；run 的 `task_status` 仍是 `in_progress`，走既有续跑 |

## 1. 问题空间枚举

### 1.1 一次挂起涉及的三方

| 方 | 在不在一起 | 后果 |
| --- | --- | --- |
| run 的 asyncio task | 永远在（否则没人执行） | 挂起 = 这个 task 阻塞在一个 Event 上 |
| 用户（可能多个标签页） | 可能不在、可能重复点 | 决策必须幂等，第一个到达的胜出 |
| 事件流（SSE） | 可能断开 | 挂起事件必须可重放（走 `RunEventLog`） |

「用户不在」是**常态**而不是异常：定时任务、直连 API、用户关掉页面都算。所以
「等待」必须有一个「没人来」的出口，而且默认方向必须是拒绝。

### 1.2 一次挂起的全部终止路径

| # | 场景 | 机制 | 落库 | 模型看到 |
| --- | --- | --- | --- | --- |
| 1 | 用户点「允许一次」 | 唤醒，执行该调用 | `approved` / scope=once | 工具真实结果 |
| 2 | 用户点「本次运行都允许」 | 唤醒 + run 内该工具加白名单 | `approved` / scope=run | 工具真实结果 |
| 3 | 用户点「拒绝」 | 唤醒，不执行 | `denied` | `permission_denied` + `approval_denied` |
| 4 | 用户不理会直到超时 | 超时唤醒 | `expired` | `permission_denied` + `approval_timeout` |
| 5 | 用户点「停止」取消 run | task 被 cancel，等待抛出 | `canceled` | 无（run 终止） |
| 6 | 后端进程崩溃/重启 | 启动时 pending → `expired` | `expired`（restart） | 无（task 已死，下次消息续跑） |
| 7 | 前端断线/刷新 | SSE 带 `after` 重连，补发 `approval.requested` | 不变 | 不变 |
| 8 | 两个标签页同时点 | 数据库条件更新 `WHERE status='pending'`，后者收 409 | 首次决策生效 | 不变 |
| 9 | headless（定时任务） | 不问，直接拒 | 不建行 | `approval_required` + `approval_unavailable` |
| 10 | 同一 run 内多个写工具 | 写工具整批串行（既有调度），最多一个 pending | — | — |
| 11 | 批准后、执行前崩溃 | 未执行；`approved` 行留在审计里 | `approved` | 无（续跑时模型重来） |

11 条里只有 1~3 是「正常路径」，其余都是必须显式处理的终止。**没有一条依赖
`end_reason` 做判断**——恢复看 `task_status` + checkpoint，本表只管诊断与审计。

## 2. 为什么是「同 run 挂起」，不是「结束 run 再续跑」

「结束再续跑」看起来更省资源（不占 asyncio task、天然跨进程），但它有一个致命缺陷：

> 续跑 = 重新调用模型 = 模型重新决定做什么。**用户批准的是「删 work/tmp 这个目录」，
> 续跑之后模型可能去删别的东西。** 那次批准就成了一张空白支票。

要把空白支票收回来，只能把已批准的那次调用钉死成「续跑后必须先执行这一条」——
那就是「同 run 挂起 + 一次进程重启」，复杂度比原地等待高得多，收益只有「不占内存」。

代价也要如实说：挂起期间这个 run 占用 `session_busy` 锁，用户在同会话里发不了新消息
（换会话、换工作区不受影响）。这是**故意的**：有一条待批准的操作悬着时，
让用户继续下指令只会制造两个真相。超时（§0）负责兜住「用户就是不理」的情况。

## 3. 状态与存储

```
run_approvals
  approval_id TEXT PRIMARY KEY
  user_id / session_id / request_id
  tool_call_id TEXT          -- 绑定到具体那一次调用
  tool_name TEXT
  capabilities TEXT          -- JSON 数组，UI 展示「它要什么权限」
  arguments TEXT             -- 原始 JSON 串（截断到 4KB），UI 展示「它要做什么」
  status TEXT                -- pending / approved / denied / expired / canceled
  scope TEXT                 -- once / run（仅 approved 有意义）
  reason TEXT                -- 终止原因：approval_denied / approval_timeout / restart / ...
  decided_by TEXT            -- 决策者（当前为 user_id；P3 多端后可区分设备）
  created_at / decided_at
```

索引：`(user_id, request_id)`、`(user_id, session_id, status)`。

**它不被用作恢复依据**，理由与 `run_plans` 相反：计划是「run 做到第几步」——不知道它就
会重做；批准是「某一次调用的外部许可」——进程死了那次调用也已经不存在了，唯一正确的
处理是让 pending 变成 `expired`，而不是让下一个进程去兑现一张过期的支票。

## 4. 挂起点与判定顺序

位置仍在 `agentrun/execution.py::_run_one`，与 P1 的裁决点同一个地方：

```
解析参数 → 裁决(capabilities ⊆ allowed)
  ├─ 允许 → 执行
  ├─ 拒绝且 kind != approval_required → 拒绝结果（P1 行为不变）
  └─ 拒绝且 kind == approval_required
       ├─ headless → 拒绝结果（reason=approval_unavailable）
       ├─ 本 run 已授权该工具（scope=run）→ 直接执行，trace 记 grantedBy=run_grant
       └─ 挂起 → 批准则执行 / 否则拒绝结果
```

「已授权该工具」放在「挂起」之前而不是「裁决」之前：**授权不能替代裁决**。
如果哪天有人把 `full` 改成可撤销，run 级授权仍然只对同一工具生效，不会顺带放开别的。

## 5. 批准 ≠ 放行

两层结构在 P1 就定死了：档位裁决（可配置）+ 安全约束（不可配置）。批准只作用于前者。

具体地，用户在审批档批准 `sandbox_write_file work/.env` 之后，工具内部的敏感路径
检查仍然返回 `permission_denied / sensitive_path`。这条不是靠「实现时记得」保证的，
而是结构上保证的：`policy` 不认识路径，`sandbox/tools.py` 不认识档位。

## 6. 授权范围与它的风险

| scope | 含义 | 风险 |
| --- | --- | --- |
| `once` | 只这一次调用 | 长任务里连续 20 次写文件 = 20 次弹窗 |
| `run` | 本次运行内同名工具不再问 | 批准 `sandbox_run_command` 一次 = 本轮所有命令都放行 |

`run` 是必要的：一次 run 里写十几个文件是常态，逐次确认会让人直接切到 `full`——
那才是真正的安全损失。但 UI 文案必须写清「包括后续所有 `sandbox_run_command`」，
不能只写「允许」。

授权只活在内存（`chat()` 的局部 set），run 结束即失效；落库的 `scope=run` 行只用于
审计回放，**不被读取用于放行**。理由：一个能被读回来放行的内存等价物，就是一个
跨 run 的提权开关。

## 7. 事件契约

两个新事件，走既有 `RunEventLog`（可重放、带 seq）：

| type | 携带 | 消费方行为 |
| --- | --- | --- |
| `approval.requested` | `approval`（id / toolCallId / toolName / arguments / capabilities / expiresAt） | 弹确认框；刷新后靠重放恢复 |
| `approval.resolved` | 同上 + `status` | 关闭确认框；被别的标签页处理时也能同步 |

事件里**不含**决策接口的 URL 或令牌——前端按 `approvalId` 调固定的
`POST /internal/approvals/{id}`，接口自己按 `userId` 鉴权。

## 8. HTTP 接口

| 方法 | 路径 | 语义 |
| --- | --- | --- |
| GET | `/internal/approvals?userId=&requestId=` | 该 run 的待批准列表（刷新兜底） |
| POST | `/internal/approvals/{approval_id}` | `{userId, decision: approve\|deny, scope: once\|run}` |

返回：200 = 决策生效；404 = 不存在或不属于该用户；409 = 已被处理（含过期、取消）。
409 的语义是「你的点击来晚了」，不是错误——前端应静默关闭并等待 `approval.resolved`。

## 9. 无人在场的执行

`Request.interactive`（默认 `True`）由调用方声明。`cronadapter` 传 `False`。

为什么不用「当前用户是否在线」来判断：在线状态是不可靠的瞬时事实，而「这个 run 是
谁发起的」是确定的事实。用前者会出现「用户刚关页面，定时任务恰好开始等一个没人会
点的确认框」。用后者，同一条 cron 无论何时跑都走同一条路径。

headless 下 `approval` 档的行为：拒绝（`approval_required` + `approval_unavailable`）。
**不自动批准、也不自动降级到 full**——两者都等于让「没人看着」变成「不用看着」。

## 10. 不做 / 已知边界

- **前端 UI**（P3）：本轮只交付事件与接口。没有 UI 时审批档的实际行为是「等到超时」，
  这是已知的中间态，不是缺陷——`full` / `read_only` 两档不受影响。
- **跨进程恢复**：多副本部署下 `ApprovalDesk` 在进程内，另一副本上看不到 ticket。
  与 `SessionLanes` / `RunPlanRegistry` 同一类边界，换共享存储时一起换。
- **审批历史查询**：只落库不开放列表接口。要「谁在什么时候批了什么」时再加。
- **按参数记忆授权**（如「以后 `git status` 都不用问」）：不做。参数是模型给的，
  按参数记忆等于让模型自己定义白名单。

## 11. 改动清单

| 文件 | 改动 |
| --- | --- |
| `session/schema.py` | `run_approvals` 表 + 索引 |
| `session/approval_store.py` | 新增：建行、条件更新决策、列待批、启动过期 |
| `agentrun/approval.py` | 新增：`ApprovalDesk`（ticket 注册表 + 超时 + 唤醒）、`ApprovalDecision` |
| `permissions/policy.py` | `Verdict.granted()`、`approval_refusal()`、三个新 reason |
| `agentrun/execution.py` | `_run_one` 接入挂起；`chat(approvals=, interactive=)`；run 级授权 |
| `agentrun/service.py` | 装配 `ApprovalDesk`、传入 chat、`decide_approval` / `pending_approvals`、cancel 时收尾 |
| `agentrun/types.py` / `gateway/types.py` | `interactive` 字段透传 |
| `cronadapter/adapter.py` | 定时任务 `interactive=False` |
| `agentstream/types.py` | `ApprovalSnapshot` + `Event.approval` |
| `webadapter/adapter.py` | 两个审批接口 |
| `tests/test_approvals.py` | 新增：状态机、竞态、超时、取消、headless、授权范围 |

## 12. 验收

```bash
cd backend-py
./.venv/Scripts/python.exe -m pytest tests -q
# 508 passed, 1 skipped（其中 tests/test_approvals.py 16 例）
```

自动化覆盖 §1.2 表中 1/2/3/4/5/8/9/11 八条（6/7 依赖重启与断线，走手工）：

| 用例 | 对应 §1.2 |
| --- | --- |
| `test_desk_waits_then_wakes_on_approve` | 1 |
| `test_run_scope_grant_is_added_after_approval` | 2 |
| `test_approval_flow_end_to_end_deny` | 3 |
| `test_timeout_is_a_deterministic_refusal` | 4 |
| `test_cancel_run_only_wakes_its_own_tickets` | 5 |
| `test_desk_second_decision_is_previous` | 8 |
| `test_headless_denies_without_asking` | 9 |
| `test_store_expires_stale_on_startup` | 6（重启的落库侧） |
| `test_approval_cannot_bypass_sensitive_paths` | §5 的断言（在 `test_permissions.py`） |

另有三条**不做自动化**的手工验证（要真模型 + 真沙箱 + 真浏览器）：

1. 审批档下发「在 README 加一行」→ 界面出现确认卡片，run 未结束；点「允许」后文件被改；
2. 同一条消息点「停止」→ run 结束，卡片消失，`run_approvals.status='canceled'`；
3. 批准后立刻重启后端 → 该行已是终态，不会在新进程里留下待批。

## 13. 一个刻意没做的决定

**`task_status` 不新增 `awaiting_approval`。** 挂起期间它仍是 `in_progress`——
run 确实还在进行，只是暂停在一个 `await` 上。加一个状态要同时改 `session_meta`
的取值域、`derive_outcome` 的映射、以及前端所有判空分支，而「有没有待批」这件事
已经由事件与 `/internal/approvals` 两个来源回答了。

判断口径还是那条：**`task_status` 回答「该不该续跑」，不回答「现在卡在哪」。**
后者是诊断信息，`end_reason` / 审批表 / trace 各自负责，不要挤进状态机。
