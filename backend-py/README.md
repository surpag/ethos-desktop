# ETHOS backend (Python)

ETHOS 桌面版的服务端：一个跑在本机的单用户 Agent 运行时，由 Electron 壳拉起，
前端是 `web-v1/`（Next.js BFF）。

它脱胎于一个云端多租户 Agent 平台，但**不再为多租户服务**：没有登录、没有远端
沙箱、没有对象存储。身份固定为本机用户，工作区就是磁盘上的目录。

## 架构

```
Web 前端 ──(BFF, 同源)──┐
                        ▼
                     Adapter        ← 协议翻译（HTTP SSE / Cron）
                        │
                        ▼
                     Gateway        ← 渠道白名单 + 请求边界
                        │
                        ▼
                     AgentRun       ← 聚合本次运行配置
                        │
                        ▼
              OpenAI SDK 流式循环    ← 手写编排，不用 LangChain
                        │
                        ▼
                    中性 Event      ← 渠道无关的流式事件
```

## 目录结构

```
src/edith_backend/
├── main.py                 # 组合根 + FastAPI 应用
├── config.py               # 数据目录与环境变量
├── database.py             # SQLite 连接（DELETE journal + busy_timeout）
└── internal/
    ├── agentrun/           # 核心编排：准入 → 配置聚合 → 流式执行
    ├── agentstream/        # 中性事件模型与流式解码
    ├── gateway/            # 渠道统一入口（web / cron）
    ├── webadapter/         # HTTP/SSE 适配器
    ├── conversation/       # Timeline 投影
    ├── session/            # 消息、任务状态、检查点
    ├── models/             # 内置 + 用户自定义模型目录
    ├── userconfig/         # 设置、供应商密钥、MCP、Git 凭据
    ├── sandbox/            # 本机沙箱：命令执行与文件原语
    ├── workspace/          # 工作区实体与本地目录（含绑定用户目录）
    ├── skills/             # Skill 目录（单层）
    ├── images/             # 聊天图片（本机磁盘）
    ├── gitops/             # Git 推送与凭据注入
    ├── cronjob/            # 定时任务存储与调度
    ├── cronadapter/        # 定时执行适配
    ├── usage/              # Token 用量
    ├── trace/              # 链路追踪（含 CLI viewer）
    ├── systemtools/        # 系统工具
    ├── tools.py            # 工具注册中心
    └── netguard.py         # 共享 SSRF / 出口校验
```

## 技术栈

- 后端：Python 3.13、FastAPI、uvicorn、Pydantic v2、openai SDK
- 存储：SQLite（aiosqlite）+ 本地文件系统
- 调度：自实现轮询调度器（10 秒轮询 + SQL 原子抢占）
- 身份：单用户，固定 `local-user`（BFF 与服务端各一处常量）
- 沙箱：本机进程与目录，不经远端
- 图片：本机磁盘；喂给模型时内联为 data URL

## 数据落点

`data_root()` 决定一切可变数据的根目录，优先级：

1. `ETHOS_DATA_DIR`（壳注入 / 测试用临时目录）；
2. 桌面模式（`ETHOS_DESKTOP=1`）→ `%APPDATA%\ETHOS`；
3. 否则 → `backend-py/`（脱离壳单独调试时）。

底下是 `edith-v2.db`、`workspace-volumes/{workspace}/`、`skills/`、`images/`。

## 本地启动

```powershell
cd backend-py
.venv\Scripts\python.exe -m uvicorn edith_backend.main:app --host 127.0.0.1 --port 8080
```

`curl http://127.0.0.1:8080/health` 应返回 ok。

> 同一份数据库只能有一个后端进程：`database.py` 用 `busy_timeout` 而非 WAL，
> 另一个进程长期持锁会让新进程启动即 `database is locked`。

## 运行测试

```powershell
.venv\Scripts\python.exe -m pytest tests -q --basetemp=.pytest-tmp-<随机>
```

`--basetemp` 必须指向一个**尚不存在**的目录（或每次换名）：pytest 会先尝试清空
它，而本机的删除护栏可能拒绝，从而让所有 `tmp_path` 用例在 setup 阶段失败。

## 环境变量

桌面模式下 `.env` 全部留空即可（模型 Key 在应用内设置页写入数据库）。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `EDITH_ADDR` | `127.0.0.1:8080` | 监听地址（壳会覆盖为随机端口） |
| `EDITH_DB_PATH` | `data_root()/edith-v2.db` | 填相对路径会相对**当前目录**解析 |
| `ETHOS_DATA_DIR` | 见上 | 可变数据根目录 |
| `EDITH_RUNTIME_TOKEN` | 空（不鉴权） | 本地 Runtime 的入站令牌；桌面壳每次启动随机生成并注入 backend 与 web。留空仅供开发与测试 |
| `ETHOS_SANDBOX_BACKEND` | `auto` | 执行隔离后端：`auto` / `asrt` / `appcontainer` / `host` / `off`。**`off` 表示拒绝执行，不是裸跑** |
| `ETHOS_SANDBOX_ALLOWED_DOMAINS` | 空（全禁网） | 沙箱内允许访问的域名，逗号分隔；空值 = 完全禁止外网 |
| `ETHOS_SANDBOX_ALLOW_UNSAFE_HOST` | 未设置 | 仅 pytest 使用：与 `ETHOS_SANDBOX_BACKEND=host` 同时在场才允许宿主直执行 |
| `EDITH_SECRET_KEY` | 内置 fallback | **仅用于解 v1 旧密文**。新写入的凭据由系统密钥保护（Windows DPAPI / 其他平台主密钥 AES-GCM），不读这个变量 |
| `EDITH_MAX_TOOL_ROUNDS` | `30` | 单轮工具调用上限 |
| `EDITH_WORKSPACE_VOLUME_DIR` | `workspace-volumes` | 工作区目录名（相对 `data_root()`） |
| `EDITH_WORKSPACE_LOCAL_ROOTS` | 空 | 可绑定为工作区的本地目录允许清单（`os.pathsep` 分隔）；**留空 = 放行任意目录**，只打一条 warning |
| `EDITH_GIT_AUTHOR_NAME` / `_EMAIL` | 空 | 用户设置里没填时的提交身份兜底 |
| `EDITH_TRACE_FULL` / `_SLOW_MS` / `_RETENTION_DAYS` | `false` / `10000` / `14` | 追踪详略、慢阈值、保留天数 |

> 不要把 `ETHOS_DESKTOP` / `ETHOS_DATA_DIR` 写进 `.env`：pytest 走同一条装配路径，
> 写死会让测试把数据写进真实的 `%APPDATA%\ETHOS`。
