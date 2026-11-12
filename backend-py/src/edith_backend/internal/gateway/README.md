# gateway（渠道统一入口）模块说明

# 第一部分：模块名称与简介

## 模块名与定位

**gateway** —— 所有 Agent 渠道的统一入口（门面）：把「渠道身份 + 会话」翻译为
「EDITH 身份 + 会话」，再交给 `agentrun.Service` 启动、查询或取消 AgentRun。

桌面端只有两个进程内渠道：`web`（浏览器）与 `cron`（定时任务）。渠道是**白名单**
而不是可扩展点：不认识的渠道一律拒绝（见第四部分「新增渠道」）。

- 实现路径：`src/edith_backend/internal/gateway/`
- 装配位置：组合根 `main.py` 显式创建，被 `webadapter` 与 `cronadapter` 调用；
  本模块自身**不注册任何 HTTP 路由**

## 文件清单

| 文件 | 职责 |
| --- | --- |
| `types.py` | 渠道常量、`IncomingMessage` 渠道事实、`to_agentrun_request` 翻译函数 |
| `service.py` | 渠道白名单校验 + 启动/查询/取消/重连 AgentRun 的转发门面 |
| `__init__.py` | 模块导出：`IncomingMessage`、`Service`、`new_service` |

# 第二部分：架构设计

## ① 分层

| 层 | 载体 | 说明 |
| --- | --- | --- |
| 类型定义层 | `types.py` | `IncomingMessage`、`WEB_CHANNEL`/`CRON_CHANNEL`、`to_agentrun_request()` |
| 服务层（门面） | `service.py` `Service` | 校验渠道与入参 → 转发 `agent_runs.run/status/cancel/subscribe` |

- **HTTP 边界**：不在本模块，而在 `webadapter/adapter.py`。
- **数据访问层**：本模块没有。它不读数据库、不查任何表。

## ② 设计模式

| 模式 | 代码位置 | 说明 |
| --- | --- | --- |
| 门面 | `service.py` `Service` | 「所有 Agent 渠道的唯一入口」，屏蔽 agentrun 的准入细节 |
| 依赖注入 | `Service.__init__(self, agent_runs)` | 依赖以参数注入，不在模块内创建 |
| 工厂函数 | `new_service(agent_runs)` | 校验依赖非空后返回 `Service` |
| 适配器（由调用方实现） | `webadapter`、`cronadapter` | 各自把协议（HTTP/SSE、cron 调度）适配为 gateway 调用 |
| 白名单策略 | `service.py` `_CHANNELS` | `web` / `cron` 放行，其余 `invalid_request`（fail-closed） |
| DTO 翻译 | `types.py` `to_agentrun_request()` | `IncomingMessage` → `agentrun.Request` |

## ③ 关键类 / 函数

| 名称 | 所在文件 | 职责 / 关键签名 |
| --- | --- | --- |
| `WEB_CHANNEL`、`CRON_CHANNEL` | `types.py` | `"web"`、`"cron"` |
| `IncomingMessage` | `types.py` | 渠道事实：channel、external_user_id、session_key、request_id、message、image_ids、upload_paths、model_id、reasoning_option_id、workspace_id |
| `to_agentrun_request(msg, user_id, session_id)` | `types.py` | 字段一一映射为 `agentrun.Request` |
| `Service` | `service.py` | 构造器注入 `agent_runs: AgentRunService` |
| `Service.run(input_msg)` | `service.py` | `-> tuple[Stream \| None, Error \| None]`：规范化 → 渠道白名单 → 转发 `agent_runs.run` |
| `Service.status / cancel / resume` | `service.py` | 纯转发（归属校验在 agentrun 侧） |
| `new_service(agent_runs)` | `service.py` | 依赖为 `None` 抛 `ValueError` |

# 第三部分：与其他模块的联系

## ① 依赖关系

| 依赖 | 用途 |
| --- | --- |
| `..agentrun.service` | 启动 / 查询 / 取消 AgentRun |
| `..agentrun.types` | `Error` / `Status` / `Stream` / `Request` |

除此之外无外部 SDK、无数据库访问。

## ② 数据流向

**Web 渠道**：

```
Web BFF ──POST /internal/gateway/messages:stream──▶ webadapter.Adapter
  （body 携带 BFF 服务端注入的 userId / sessionId）
  ──IncomingMessage(channel="web", external_user_id=userId, session_key=sessionId)──▶ gateway.Service.run
  ──user_id=external_user_id，session_id=session_key──▶ to_agentrun_request
  ──agentrun.Request──▶ agent_runs.run ──▶ Stream（事件日志 + 多订阅者）
  ◀── SSE 事件流（by_alias=True 输出 camelCase）── webadapter 逐事件 yield
```

**cron 渠道**：

```
cronjob 调度器 ──▶ cronadapter.Adapter.run_job(job)
  ──IncomingMessage(channel="cron", external_user_id=job.user_id,
                     session_key=f"cron:{job.id}", request_id=uuid4().hex, message=job.prompt)──▶ gateway.Service.run
  ──▶ agent_runs.run ──▶ 消费事件直到 None 哨兵；遇 run.error 抛 RuntimeError
```

**未知渠道**：

```
任何 Adapter ──IncomingMessage(channel="wechat", ...)──▶
  Error("invalid_request", 'unsupported channel "wechat"')
```

## ③ 接口调用

| Method | Path（注册在 webadapter） | gateway 方法 |
| --- | --- | --- |
| POST | `/internal/gateway/messages:stream` | `run` |
| GET | `/internal/gateway/runs/{request_id}?userId=` | `status` |
| POST | `/internal/gateway/runs/{request_id}/cancel?userId=` | `cancel` |
| GET | `/internal/gateway/runs/{request_id}/events?userId=&after=` | `resume` |

组合根：`new_gateway_service(agent_runs)` → `Adapter(gateway)` / `CronAdapter(gateway)`
→ `cron_jobs.new_scheduler(cron_adapter)`。

# 第四部分：模块内容扩展说明

## ① 核心逻辑要点

1. **输入规范化**：`channel` / `external_user_id` / `session_key` / `model_id` 先 `strip()`；
   channel 或 external_user_id 为空 → `invalid_request`。
2. **渠道白名单**：不在 `_CHANNELS` 里 → `invalid_request`。**不做 fallback**：把
   unknown channel 当成某个默认用户，等于让任何能调到这个入口的东西以那个用户身份
   跑任务。
3. **sessionKey 必填**：空值 → `invalid_request`（web 由 BFF 给会话 id，cron 给
   `cron:{job.id}`）。
4. **转发执行**：`to_agentrun_request` 翻译后调用 `agent_runs.run`；`status` / `cancel`
   / `resume` 为纯转发，归属校验在 agentrun。
5. **无状态**：本模块不持有任务状态；活跃任务、会话车道、停止标记全在
   `agentrun.Service`。

## ② 配置项

**无**。gateway 不读任何环境变量。

## ③ 异常处理

| 场景 | 策略 |
| --- | --- |
| 依赖为空 | `new_service` 抛 `ValueError`（装配期错误） |
| 业务失败 | 一律以 `(result, Error)` 返回，不抛异常 |
| 错误映射 | `webadapter._gateway_error_to_http`：`invalid_request`→400、`session_busy`/`workspace_busy`/`request_conflict`→409、`not_found`→404、其他→500；cronadapter 把 `run_error` 包成 `RuntimeError` |

## ④ 扩展点

- **新增渠道必须同时想清楚身份从哪来。** 上游的做法是「渠道账号 → 用户」的绑定表
  （`user_channel_bindings`），已随多租户一起删除。因此现在新增渠道的代价不是加一个
  常量，而是回答：这个渠道的调用方凭什么代表某个用户？在回答之前，白名单会拒绝它
  ——这是刻意 fail-closed 的。
- **新增转发方法**：在 `Service` 加代理方法即可，并在 `__init__.py` 确认导出。
- **无本模块专属表**：gateway 不持有任何持久状态。
