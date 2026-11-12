# edith_backend 包根：组合根与共享基础设施

## 第一部分：包结构总览

**一句话定位**：`edith_backend` 是 ETHOS 桌面版的服务端包，本目录即包根，承载
**进程入口（组合根）**与**跨模块共享基础设施**（配置加载、数据库连接、统一错误
契约、工具聚合器）；业务模块全部位于 `internal/` 下。

**顶层文件清单**：

| 文件 | 一句话职责 |
| --- | --- |
| `main.py` | 进程入口与组合根：`lifespan` 里打开数据库、创建全部模块、显式装配依赖、注册路由 |
| `config.py` | 数据根目录与环境变量读取（`ETHOS_DATA_DIR` / `EDITH_DB_PATH` / `EDITH_ADDR`） |
| `database.py` | SQLite 连接管理：单连接、`row_factory=Row`、DELETE 日志模式、忙等待超时 |
| `__init__.py` | 包文档注释 |
| `internal/http_utils.py` | 统一错误契约：`EDITHError` + 全局处理器（`{"type","message"}` JSON） |
| `internal/tools.py` | 工具注册表 `ToolRegistry`：按名汇总与分发 |
| `internal/netguard.py` | 出口地址校验（SSRF / 本地回环地址判定） |

**模块地图**（`internal/` 下 18 个模块，各自带 README）：

| 模块 | 一句话职责 |
| --- | --- |
| `agentrun` | Agent 唯一执行入口：配置聚合、流式对话、工具循环、生命周期 |
| `agentstream` | 渠道中性事件模型与流式解码器（块 ID 状态） |
| `conversation` | 会话历史读取与 Timeline 投影 |
| `cronadapter` | 定时任务到 AgentRun 的渠道适配器 |
| `cronjob` | 定时任务管理、调度、工具与 HTTP |
| `gateway` | 渠道统一入口（web / cron 白名单）与请求边界 |
| `gitops` | Git 推送编排：bundle + 临时仓 + 凭据注入 |
| `images` | 聊天图片：本机磁盘存储与 Agent 图片输入 |
| `models` | 模型目录与供应商定义（内置 + 用户自定义） |
| `sandbox` | 本机沙箱：命令执行、文件原语、目录联接 |
| `session` | 会话消息持久化（`chat_messages` 表） |
| `skills` | Skill 目录（单层）、清单注入与加载预算 |
| `systemtools` | 系统内置工具集 |
| `trace` | 链路追踪存储与 CLI viewer |
| `usage` | 用量记录与汇总 |
| `userconfig` | 用户设置、供应商密钥、MCP、Git 凭据 |
| `webadapter` | Web 渠道 HTTP/SSE 适配器（Gateway 的 HTTP 边界） |
| `workspace` | 工作区实体、本地目录与绑定用户目录 |

## 第二部分：架构设计

### ① 分层

| 层 | 载体 | 说明 |
| --- | --- | --- |
| 进程入口层 | `main.py`（`main()` + `create_app()`） | uvicorn 启动 `edith_backend.main:app` |
| 组合根/装配层 | `main.py` 的 `lifespan` | 按固定顺序：DB → 建表 → 各模块 → 显式注入 → 注册路由；退出时回收 |
| 配置层 | `config.py` | 数据根目录推导 + `.env` 容错加载（utf-8/gbk） |
| 数据访问层 | `database.py` | 唯一 SQLite 连接，注入各模块 |
| 共享基础设施 | `internal/http_utils.py`、`internal/tools.py` | 统一错误处理、工具注册表 |
| 业务模块层 | `internal/*` | 纵向功能模块，各自带类型/业务/HTTP 边界 |

### ② 设计模式

| 模式 | 代码位置 | 说明 |
| --- | --- | --- |
| 组合根 | `main.py` `lifespan` | 全部模块在此创建并以构造器注入互连；模块之间不互相装配，避免循环依赖 |
| 依赖注入 | `Service(Dependencies(...))`、`Adapter(gateway)`、`new_gateway_service(agent_runs)` | 模块面向对象协作，不查全局 |
| 工厂函数 | `new_*`（`new_userconfig_module`、`new_images_module` 等） | 创建并校验依赖后返回模块对象 |
| 统一错误契约 | `http_utils.register_error_handler` | `EDITHError` → `{"type","message"}`；`RequestValidationError` → 400 `invalid_request` |
| 注册表 | `internal/tools.py` `ToolRegistry` | 汇总多个工具集，按 `function.name` 分发 |
| 单连接数据访问 | `database.py` | aiosqlite 单连接线程模型天然串行 |

### ③ 关键类 / 函数

| 名称 | 所在文件 | 职责 |
| --- | --- | --- |
| `data_root` | `config.py` | 可变数据根目录：`ETHOS_DATA_DIR` → 桌面 `%APPDATA%\ETHOS` → `backend-py/` |
| `database_path` | `config.py` | `EDITH_DB_PATH`，缺省 `data_root()/edith-v2.db` 的**绝对**路径 |
| `runtime_address` | `config.py` | `EDITH_ADDR`，默认 `127.0.0.1:8080` |
| `open_database` | `database.py` | `row_factory=Row`；`journal_mode=DELETE`（不用 WAL，规避 Windows 下 `-shm`/`-wal` 锁残留）；`busy_timeout=5000` |
| `lifespan` | `main.py` | 装配全部模块并 yield；退出时取消调度器、关闭 DB |
| `create_app` | `main.py` | 创建 app、注册错误处理器、挂 `/health` |
| `EDITHError` | `http_utils.py` | `(status_code, error_type, message)` 三元组 |
| `ToolRegistry` | `tools.py` | `tool_definitions()` / `async execute(name, arguments, user_id, session_id)` |

## 第三部分：与其他模块的联系

### ① 装配顺序（`main.py` `lifespan`）

```
open_database(config.database_path())
  → create_session_schema / create_workspace_schema / create_sandbox_schema
    / create_cronjob_schema / create_models_schema / create_trace_schema
  → TraceStore(db) + 保留期清理（EDITH_TRACE_RETENTION_DAYS，失败不阻塞启动）
  → new_userconfig_module(db, DEFAULT_MODEL_ID)     # 设置 / 供应商 / MCP / Git 凭据
  → CustomModels + new_models_module                 # 模型目录（依赖 userconfig）
  → new_usage_module(db)
  → SessionStore(db) / RunPlanStore(db) / RunPlanRegistry()
  → new_conversation_module(session_store, usage.reader, plan_store)
  → WorkspaceStore(db) / WorkspaceVolumeService / WorkspaceMemoryStore(db)
  → seed_builtin(skills_root) + SkillsCatalog(skills_root)
  → SandboxService(db, workspaces, workspace_volumes)
  → WorkspacePush + SandboxToolSet + SandboxAgentInput
  → new_cronjob_module(db, users.settings)
  → new_images_module(db, data_root()/"images")
  → ToolRegistry([SystemToolSet(), sandbox_tools, cron_jobs.tools, PlanToolSet])
  → MCPRuntime(users.mcp)
  → agentrun.Service(Dependencies(...))
  → new_gateway_service(agent_runs)
  → Adapter(gateway) / CronAdapter(gateway) / cron_jobs.new_scheduler(cron_adapter)
  → include_router(全部模块)
```

### ② 请求入口总览

```
Web 前端（BFF 同源代理，服务端注入 userId）
  ├─ POST /internal/gateway/messages:stream        → webadapter → gateway → agentrun → SSE
  ├─ GET/POST /internal/gateway/runs/{id}[/cancel] → webadapter → gateway → agentrun
  ├─ GET /internal/conversations[/{session_id}]    → conversation → session + usage
  ├─ GET/PUT /internal/user-settings, /internal/mcp-servers* → userconfig
  ├─ GET /internal/models, /internal/available-models → models
  ├─ GET /internal/skills                          → skills（读目录 + 清单注入）
  ├─ /internal/sandbox/files*                      → sandbox（真实磁盘路径）
  ├─ /internal/workspaces*, /internal/memories*     → workspace
  ├─ PUT /internal/images/{id}/blob, /internal/images* → images（本机磁盘）
  └─ /internal/cron-jobs*                          → cronjob（经 cronadapter → gateway）
定时器：cron scheduler ──► CronAdapter ──► gateway ──► agentrun
```

所有渠道（Web / cron）最终收敛到 `gateway → agentrun` 这一条执行链；gateway 只
放行这两个渠道，其余一律 `invalid_request`。

## 第四部分：扩展与约束

### ① 核心约束

1. **单进程单连接 SQLite**：只开一个 aiosqlite 连接注入所有模块。同一份数据库
   同时跑两个后端进程会撞锁（`database is locked`）。
2. **建表集中在启动前置**：session / workspace / sandbox / cronjob / models / trace
   六张 schema 建完，模块才能使用 DB。
3. **模块创建顺序即依赖顺序**：被依赖方先建，执行链最后组装。
4. **工具三层聚合**：静态工具（系统 + 沙箱 + cron + plan）经 `ToolRegistry` 聚合；
   动态工具（用户启用的 MCP）由 `MCPRuntime` 按 run 打开，执行时 MCP 优先。
5. **统一错误契约**：业务错误一律 `EDITHError`；请求体校验失败（含未知字段）
   统一 400 `invalid_request`。

### ② 配置项

| 环境变量 | 默认值 | 读取位置 | 用途 |
| --- | --- | --- | --- |
| `ETHOS_DATA_DIR` | 见 `data_root()` | `config.py` | 可变数据根目录（DB / 工作区 / skills / images） |
| `ETHOS_DESKTOP` | 未设置 | `config.py` | `"1"` 表示跑在壳内，数据落 `%APPDATA%\ETHOS` |
| `EDITH_DB_PATH` | `data_root()/edith-v2.db` | `config.database_path()` | SQLite 文件路径（相对路径相对 cwd，勿用） |
| `EDITH_ADDR` | `127.0.0.1:8080` | `config.runtime_address()` | HTTP 监听地址 |
| `EDITH_SECRET_KEY` | 内置 fallback | `userconfig/secret.py` | 混淆落盘的 API Key / MCP Header（非强加密） |
| `EDITH_TRACE_RETENTION_DAYS` | `14` | `main.py` | trace 保留天数 |
| `EDITH_LOG_FILE` | 未设置 | `main.py` | 设置后日志滚动落盘（10MB × 5） |

### ③ 异常处理

| 场景 | 策略 | 位置 |
| --- | --- | --- |
| `EDITHError(status, type, message)` | 全局处理器 → HTTP `status` + `{"type","message"}` | `http_utils` |
| 请求体校验失败（含未知字段） | 400 `invalid_request` | `http_utils` |
| `.env` 解码失败（utf-8/gbk 均失败） | 抛 `RuntimeError`，启动失败 | `config.py` |
| DB 打开/PRAGMA 失败 | 关闭连接后重新抛出 | `database.py` |
| 未知工具名 | 返回 `{"error":"unknown tool: <name>"}` JSON，不抛异常 | `tools.py` |

### ④ 扩展点

| 方向 | 做法 |
| --- | --- |
| 新增业务模块 | `internal/<new>/` 建包（类型 + 服务 + `module.py` 工厂 + 可选 `http.py`），在 `lifespan` 按依赖顺序创建并 `include_router` |
| 新增 Agent 工具集 | 实现 `tool_definitions()` 与 `execute(...)`，加入 `ToolRegistry([...])` |
| 新增 HTTP 路由 | 在模块 `_build_router()` 内追加处理器，`include_router` 无需改动 |
| 新增环境变量 | 在 `config.py` 增加读取函数（`EDITH_*` 命名），由 `main.py` 注入目标模块 |
| 更换数据库实现 | 替换 `database.open_database` 返回的连接（各模块只收 `aiosqlite.Connection`） |
