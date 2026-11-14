> **已回退（2026-11-14 归档）**：会话级 / 工作区级档位已全部删除。
> 档位的控制点只有一个——用户设置（`user_agents.permission_level`），
> 定时任务快照只能压低。`session_meta.permission_level` 与 `workspaces.permission_level`
> 两列停止使用，对应 HTTP 路由、响应字段、前端切换器一并删除。
> 原因：档位回答的是「我允许 Agent 对我的机器做什么」，这个问题只有一个回答者；
> 多一层作用域不会更安全，只会让用户在每个对话里重新对账一次。
> 当前实现见 `backend-py/src/edith_backend/internal/permissions/README.md`。
>
> 本文仅作设计档案保留，**不要按它理解当前代码**；其中「为什么只能收紧」的论证
> 仍然适用于 `ceiling`（定时任务快照）与用户级的关系。

# P3：会话级 / 工作区级档位上限

> 状态：已实施。上游：`permissions-p1.md`（能力与档位裁决）、`permissions-p2-approval.md`（审批挂起）。
> 本文只解决一件事：**同一个用户在「全局设置」之外，还能不能为某一段对话、某个工作区单独设档位。**

## 1. 结论

**能设，但只能收紧。**

```
effective = min(user, workspace, session, ceiling)
```

四个来源取最严者，没有优先级、没有「谁覆盖谁」。`ceiling` 是 P1 就有的「本次执行上限」
（定时任务的权限快照），现在与两级作用域是同一条链上的普通成员，不是特例分支。

这条判断决定了其余全部设计：既然不能放宽，就不需要继承快照、不需要在换绑工作区时
重算授权、不需要「生效时刻快照」这类机制——任何时候按下式取最小值，结果都只会更安全。

## 2. 为什么只能收紧

这是本文唯一的策略判断，值得单独说清。

**能放宽的话，「只读」就是一个假承诺。** 只读档的用处是「我确认这段对话不会写东西」。
如果会话级能把用户级的只读放宽回完全，那用户设只读只是在表达偏好，任何一次会话内
操作（或被注入的模型输出）都可以把它撤销。用户级设置于是从边界降级为建议。

同构的做法到处都是：

| 体系 | 子层能做什么 |
| --- | --- |
| OAuth scope | 只能缩减，不能扩增 |
| AWS 权限边界 / STS | 只能减，交集生效 |
| SELinux bounding set | 只能 drop |
| Unix setuid 后 | 只能降权 |

**推论：命名必须准确。** 叫「覆盖（override）」会让人以为后者胜，下一个改代码的人
看到 `min` 会以为是 bug 顺手改成「取最后一级」，而且改完一切照常运行、界面照常显示，
只有安全边界悄悄消失。所以代码里一律叫 **ceiling / 上限**，`resolve_chain` 的注释
把这句话写死了。

## 3. 问题空间枚举

一次 run 的档位来源可以有任意组合，逐个列：

| # | user | workspace | session | ceiling | 生效 | binding | 说明 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 空 | 空 | 空 | 空 | full | — | 默认：什么都没设 |
| 2 | full | 空 | read_only | 空 | read_only | session | 典型：普通对话临时只读 |
| 3 | full | approval | 空 | 空 | approval | workspace | 典型：这个项目只做代码审查 |
| 4 | full | approval | read_only | 空 | read_only | session | 两级同时收紧，取更严 |
| 5 | read_only | full | full | 空 | **read_only** | user | **子作用域不得放宽**（核心断言） |
| 6 | full | 空 | 空 | approval | approval | ceiling | 定时任务快照仍生效 |
| 7 | full | `god_mode` | 空 | 空 | read_only | workspace | 脏数据 → 最严 + `invalid` 记录 |
| 8 | 空 | 空 | 空 | `god_mode` | read_only | ceiling | 同上（快照也可能坏） |
| 9 | 读失败 | 空 | 空 | 空 | read_only | user | DB 异常 → 最严 + `errors` 记录 |

补充的生命周期情形：

| 情形 | 结果 | 为什么 |
| --- | --- | --- |
| 会话换绑到另一个工作区 | 取新工作区的上限 | 都是收紧，不需要「绑定时快照」；快照只在允许放宽时才有意义 |
| 工作区被删除 | 该级按未设置 | 删一个项目不该把这个位置变成永久的只读陷阱 |
| 用户后来把全局调低 | 所有会话立即跟着降 | 这正是「全局是边界」的含义 |
| 用户后来把全局调高 | 会话/工作区上限仍然生效 | 否则一次全局提权会顺手抹掉所有子级收紧 |
| 会话被归档 / 删除 | 不影响 | 档位只影响读写，不影响数据保留 |

## 4. 存储

| 级别 | 位置 | 空值含义 |
| --- | --- | --- |
| 用户级 | `user_agents.permission_level`（P1 已有） | 空 = 未配置 → 默认档 |
| 工作区级 | `workspaces.permission_level TEXT NOT NULL DEFAULT ''` | 空 = 不额外收紧 |
| 会话级 | `session_meta.permission_level TEXT NOT NULL DEFAULT ''` | 空 = 不额外收紧 |

**独立成列，不塞 metadata。** `workspaces.metadata` 是整体替换的（`update_metadata`
直接覆盖 JSON），一次无关的文件元信息更新就会把档位冲掉——而冲掉的后果是**放宽**。

**DEFAULT '' 而不是 DEFAULT 'full'。** 给历史数据补一个默认值等于替用户做了一次
授权决定，而且他永远不会知道。老库迁移只补列，不写值。

## 5. 解析链

```
resolve_chain(user=…, workspace=…, session=…, ceiling=…) -> Resolution
```

`Resolution` 三个字段各有用途，缺一个就会逼调用方去猜：

| 字段 | 用途 |
| --- | --- |
| `level` | 真的按它执行 |
| `binding` | 界面解释「为什么不是完全档」 |
| `sources` / `invalid` / `errors` | 排障：谁设了什么、谁的值坏了、谁读不到 |

`binding` 只在**确实有来源把档位压下来**时才有值。全员等于默认档时留空——否则日志里
每条都是「binding=user」，看三个月就不会再看它。

### 取数端：`LevelChain`

同一句「这个 run 按几档跑」有三处要问：run 本身（执行）、会话详情（界面）、排障日志。
三处各写一遍 `min(...)` 迟早漂移成「界面显示完全、实际按只读跑」，而这类偏差没有任何
报错——用户只会发现 Agent 突然什么都干不了。所以取数统一走 `permissions/chain.py`。

**读失败按只读，不按未设置。** 这两件事必须分开：

- 没设置 = 中性（不参与收紧），这是绝大多数情况；
- 读失败 = **按只读**。当成未设置等于「数据库抖一下就能提权」。

代价是 DB 异常时用户会遇到「明明设了完全却写不了」。所以 `errors` 单独记字段、
日志单独打一条 `permission_read_failed`——这种情况必须能在日志里一眼看到。

### 为什么 `resolve_chain` 用显式关键字参数

不用 `**sources`：拼错的来源名应当立刻 `TypeError`。这条链每轮 run 都跑，一个打错的
kwarg 被静默忽略等于「这一级上限永远不生效」，而界面上看不出任何异常。

## 6. HTTP

| 接口 | 语义 |
| --- | --- |
| `PUT /internal/conversations/{id}/permission-level` | 设置/清除本会话上限 |
| `PUT /internal/workspaces/{id}/permission-level` | 设置/清除本工作区上限 |
| `GET /internal/conversations/{id}` | 响应新增 `permissionLevel` / `effectivePermissionLevel` / `permissionBinding` |
| `GET /internal/workspaces` | 每项新增 `permissionLevel` |

**写侧严格校验，非法值 400。** 与 `parse_level` 的分工必须分清：

- `parse_level` 是**读**侧容错：库里的历史脏数据，非法值降级到最严（「读不懂」不能变成「放行」）；
- `parse_level_input` 是**写**侧：值来自本次请求，非法就拒绝。静默改成最严等于骗人——
  用户选了「完全」，存进去是「只读」，界面显示和行为不一致，他会以为自己没点对。

**响应必须同时回设置值与生效值。** 只回一个就必然在「显示用户的选择」和「显示真实行为」
之间二选一，两者都会误导。前端拿到差异后有一句现成的话可以说：*实际按 X 运行，
Y 比本会话更严*。

**不存在的 workspace 返回 404 而不是 403**，与 `WorkspaceStore.exists` 的既有口径一致：
区分开就等于提供一个「这个 id 存不存在」的探测器。

## 7. 前端

| 位置 | 控件 | 显示什么 |
| --- | --- | --- |
| 聊天页顶部 | 盾牌 + 档位短名，点开选三档 + 不限制 | **生效值**，被压住时加一个警示点 |
| 设置页 · 工作区指令 | 「执行权限上限」下拉 | 该工作区的设置值 |

**按钮显示生效值而不是选中值。** 用户级只读 + 会话选完全 → 实际只读。显示选中值等于
骗人，用户会以为 Agent 坏了；显示生效值再配一句「你的全局设置比本会话更严」，
他才知道该去哪里改。

**不做乐观更新。** 保存后以服务端返回的生效值为准，理由同上：前端算不出 `min`，
自己猜一个就是在制造两个真相。

**空串是「不限制」，不是「完全」。** 工作区设置里的初始 state 是 `""`：读取失败时
带着猜出来的档位去保存，等于替用户做了一次授权决定。

## 8. 与定时任务的关系（已知边界）

cron 任务当前**没有** `workspace_id`，也没有用户会话级设置，所以只受 `user + ceiling`
约束。这是如实的结构限制，不是遗漏：

- 定时任务跑在自己的会话（`cron:{id}`）里，用户不会去给它设会话级上限；
- 它不绑工作区，所以工作区级上限对它不生效。

**若将来 cron 支持绑定工作区，解析链必须一并加上那一级**——否则「给项目设了只读，
但定时任务仍能写」会成为一个很难查的口子。

## 9. 不做

- **放宽型覆盖**：不做，且不应做（见 §2）。
- **子作用域的「授权记录」**：不需要。授权记录（cron 快照）存在的理由是「创建时的
  上下文不能代表执行时的意图」；而收紧不存在这个问题——它只会让执行时的权限更少。
- **按工具细分到作用域**：不做。档位是粗粒度边界，细粒度限制属于能力声明与敏感路径。

## 10. 改动清单

| 文件 | 改动 |
| --- | --- |
| `permissions/policy.py` | `resolve_level` → `resolve_chain`（四来源 fold）+ `Resolution` + `parse_level_input` |
| `permissions/chain.py` | 新增：`LevelChain` 取数端（读失败降级 + `errors`） |
| `permissions/__init__.py` | 导出更新 |
| `session/schema.py` | `session_meta.permission_level` + 迁移 |
| `session/store.py` | `get/set_permission_level` |
| `workspace/schema.py` | `workspaces.permission_level` + 迁移 |
| `workspace/store.py` | `get/set_permission_level`；`get`/`list` 带上限 |
| `workspace/types.py` / `http.py` | `WorkspaceLevelRequest`、`PUT …/permission-level`、响应字段 |
| `conversation/types.py` / `history.py` / `http.py` / `module.py` | 会话级读写、`level_state`、响应字段 |
| `agentrun/service.py` | `Dependencies.levels`；run 前解析四来源并记 `permission_resolved` |
| `main.py` | 装配 `LevelChain`（一份，run 与读会话共用） |
| `tests/test_permission_scopes.py` | 新增 20 例 |
| `web-v1/lib/chat/type.ts` | `ConversationResponse` 三个字段 |
| `web-v1/components/chat/permission-switcher.tsx` | 新增：会话级档位控件 |
| `web-v1/components/chat/chat-page.tsx` | 接线 |
| `web-v1/components/settings/workspace-settings.tsx` | 工作区级下拉 |
| `web-v1/app/api/{conversations,workspaces}/…/permission-level/route.ts` | 新增 BFF（严格校验） |

## 11. 验收

```bash
cd backend-py && ./.venv/Scripts/python.exe -m pytest tests -q
cd web-v1 && npx tsc --noEmit && npm test
```

`tests/test_permission_scopes.py` 20 例，逐条对应 §3 的表：

- **核心断言** `test_session_can_tighten_but_never_loosen`：`user=read_only, session=full → read_only`。
  它保护的是一条设计判断，不是某个实现细节——所以哪怕别的用例全绿，这条挂了也不许合。
- 解析链 9 例（组合、binding、脏数据、读失败）
- 存储往返 4 例（会话/工作区/跨用户隔离/老库迁移后为空串）
- 写入校验 1 例
- HTTP 边界 6 例（往返、清除、400、400 缺 userId、工作区 404）

## 12. 已知边界

1. **cron 不受工作区上限约束**（§8）。
2. **界面只显示当前会话的生效值**。同一用户的其他会话、或同一工作区的其他位置，
   需要各自打开才看得到——刻意不做「全局档位总览」，那是设置页的职责。
3. **`LevelChain` 每次 run 三次查询**（user / workspace / session）。SQLite 本地读，
   与一轮 run 的模型调用相比可忽略；不做缓存，因为缓存意味着「用户改了设置，正在跑的
   下一轮还用旧值」，而这个错误的代价恰好是安全方向。
