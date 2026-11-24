"""EDITH backend-v2 的 Python 重写版进程入口。

对应 Go 版 cmd/server/main.go 的组合根职责：
打开数据库、创建顶层模块、显式连接依赖、注册路由、启动服务。
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request

from . import config
from .database import open_database
from .internal import http_utils
from .internal import isolation as isolation_layer
from .internal import log_context
from .internal.agentrun import Dependencies, Service
from .internal.agentrun.plan_tools import PlanToolSet
from .internal.agentrun.run_plan import RunPlanRegistry
from .internal.conversation import new_module as new_conversation_module
from .internal.cronadapter import Adapter as CronAdapter
from .internal.cronjob import create_schema as create_cronjob_schema
from .internal.cronjob import new_module as new_cronjob_module
from .internal.gateway import new_service as new_gateway_service
from .internal.gitops import resolve_git_identity
from .internal.gitops.http import HTTP as GitopsHTTP
from .internal.gitops.orchestrator import WorkspacePush
from .internal.images import new_module as new_images_module
from .internal.models import DEFAULT_MODEL_ID, CustomModels
from .internal.models import create_schema as create_models_schema
from .internal.models import new_module as new_models_module
from .internal.sandbox import HTTP as SandboxHTTP
from .internal.sandbox import AgentInput as SandboxAgentInput
from .internal.sandbox import SandboxToolSet
from .internal.sandbox import Service as SandboxService
from .internal.sandbox import create_schema as create_sandbox_schema
from .internal.sandbox import processes as sandbox_processes
from .internal.session import SessionStore
from .internal.session import create_schema as create_session_schema
from .internal.session.approval_store import ApprovalStore
from .internal.session.constraint_store import ConstraintStore
from .internal.session.plan_store import RunPlanStore
from .internal.skills import HTTP as SkillsHTTP
from .internal.skills import Catalog as SkillsCatalog
from .internal.skills import seed_builtin
from .internal.systemtools import SystemToolSet
from .internal.tools import ToolRegistry
from .internal.trace import Store as TraceStore
from .internal.trace import create_schema as create_trace_schema
from .internal.usage import new_module as new_usage_module
from .internal.userconfig import new_module as new_userconfig_module
from .internal.userconfig.mcp_http import build_mcp_router
from .internal.userconfig.mcp_runtime import MCPRuntime
from .internal.webadapter import Adapter
from .internal.workspace import (
    WorkspaceMemoryStore,
    WorkspaceStore,
    WorkspaceVolumeService,
    build_instruction_router,
    build_memory_router,
    build_workspace_router,
)
from .internal.workspace import (
    create_schema as create_workspace_schema,
)

# 日志基础配置：模块级一次配好，所有 logger 统一格式并携带 request_id
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
)

# 自定义日志过滤器，自动给所有日志加上 request_id
class RequestIDFilter(logging.Filter):
    def filter(self, record):
        record.request_id = log_context.current_request_id()
        return True


# 关键：filter 必须挂在 root 的 handler 上，而不是 root logger 上。
# logging 的 logger 级 filter 只对「发出日志的 logger 自身」生效；
# 子模块 logger 的 record 传播到 root 后由 handler 处理，handler 级 filter
# 才对所有经过的 record 生效（含第三方库日志）。
for _handler in logging.getLogger().handlers:
    _handler.addFilter(RequestIDFilter())

# 日志落盘（可选）：设置 EDITH_LOG_FILE 后，同一格式的日志滚动写入文件
# （10MB × 5 个备份）。「跨 run 视角」（错误率 / 截断率 / P95 统计）依赖落盘——
# stdout 随终端滚动、重启即失。未设置时维持纯 stdout（dev 行为不变）。
_log_file = os.environ.get("EDITH_LOG_FILE")
if _log_file:
    from logging.handlers import RotatingFileHandler

    # handler 不建父目录，路径不存在会启动即崩——先建好再挂
    _log_dir = os.path.dirname(os.path.abspath(_log_file))
    os.makedirs(_log_dir, exist_ok=True)
    _file_handler = RotatingFileHandler(
        _log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    _file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    _file_handler.addFilter(RequestIDFilter())
    logging.getLogger().addHandler(_file_handler)

logger = logging.getLogger(__name__)

#: 未配置令牌时只警告一次：每个请求刷一条会把有价值的日志淹掉。
_warned_no_runtime_token = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时组装模块，退出时关闭资源。"""
    config.load_environment()
    db = await open_database(config.database_path())
    await create_session_schema(db)
    await create_workspace_schema(db)
    await create_sandbox_schema(db)
    await create_cronjob_schema(db)
    await create_models_schema(db)
    await create_trace_schema(db)

    # 上一次运行可能是被强杀的：`lifespan` 没机会执行下面的 `release_all()`，
    # 后台进程还在跑，而内存台账已经随进程消失。这里读回持久化登记并尝试回收。
    # 只有"创建时刻 + 镜像路径"都对得上的进程才会被终结，其余只留记录（结果与
    # 审计由 `processes.recover()` 自己记录，这里只补一条人工处置的指引——
    # 恢复报告里 unknown 不计入成功，必须有可执行的下一步）。
    recovery = sandbox_processes.recover()
    if recovery.registry_status not in ("ok", "missing"):
        # 空集合在这里的含义是"读不出来"，不是"没有遗留进程"。这条日志是启动
        # 序列里唯一能把它与"系统干净"区分开的地方。
        logger.error(
            "sandbox_process_registry_unreadable status=%s —— 本轮未执行任何恢复；"
            "用 `edith-processes inspect` 查看损坏备份与详情",
            recovery.registry_status,
        )
    if recovery.unknown:
        logger.warning(
            "sandbox_process_recovery_pending pids=%s —— 归属无法确认，未做任何处理；"
            "用 `edith-processes inspect` 查看详情，`retry <pid>` 重新核验，"
            "`acknowledge <pid> --reason ...` 确认暂不处理",
            list(recovery.unknown),
        )

    # 沙箱执行审计：表由隔离层自己维护（它比 trace 更靠近执行链），
    # 连接注入后 `isolation.record()` 才真正落库；未注入时只写日志。
    await isolation_layer.store.configure(db).ensure_schema()

    # trace 保留期：默认 14 天（失败/降级 run 内部翻倍）。多租户下不清理会无限增长。
    traces = TraceStore(db)
    try:
        removed = await traces.cleanup(int(os.getenv("EDITH_TRACE_RETENTION_DAYS", "14")))
        if removed:
            logger.info("trace_cleanup removed=%d", removed)
    except Exception:  # 清理失败不阻塞启动
        logger.warning("trace cleanup failed", exc_info=True)

    users = await new_userconfig_module(db, DEFAULT_MODEL_ID)
    custom_models = CustomModels(db, users.providers)
    model_catalog = new_models_module(users.providers, custom_models)
    usage = await new_usage_module(db)
    session_store = SessionStore(db)
    plan_store = RunPlanStore(db)
    # 活跃计划注册表：plan_update 工具写入、run 收尾读取、历史走 plan_store，
    # 三者共用一个实例，避免出现「工具改了、收尾看不到」的双份状态。
    run_plans = RunPlanRegistry()

    workspaces = WorkspaceStore(db)
    conversations = new_conversation_module(session_store, usage.reader, plan_store)
    # 工作区文件就是磁盘上的一个目录：未绑定用户目录时落在
    # `<data_root>/workspace-volumes/{id}`，绑定后读写直连用户目录本身。
    # 本地沙箱把 `workspace/{id}` junction 到同一个位置，所以两边看到的是同一份文件。
    workspace_volumes = WorkspaceVolumeService(workspaces)
    workspace_memories = WorkspaceMemoryStore(db)

    # Skill：一份目录，没有作用域。内置 Skill 在启动时铺进去（目标已存在就跳过，
    # 用户改过的内容不会被升级覆盖回去）。目录本身由 LocalSandbox junction 到
    # 沙箱的 `/home/user/skills`，所以这里只负责「宿主机上有什么」。
    skills_root = config.data_root() / "skills"
    seed_builtin(skills_root)
    skills = SkillsCatalog(skills_root)

    sandboxes = SandboxService(db, workspaces, workspace_volumes)
    async def _git_identity(user_id: str) -> tuple[str, str]:
        """解析用户的 git 提交身份：用户设置优先，留空回退平台默认。"""
        settings = await users.settings.load(user_id)
        return resolve_git_identity(settings.git_author_name, settings.git_author_email)

    # 推送编排只建一份，HTTP「推送到 GitHub」与 Agent 的 git_push 工具共用
    git_push = WorkspacePush(sandboxes, users.git_credentials, workspaces)
    sandbox_tools = SandboxToolSet(
        sandboxes, _git_identity, git_push.push, git_push.remote_info
    )
    sandbox_input = SandboxAgentInput(sandboxes)
    cron_jobs = await new_cronjob_module(db, users.settings)
    images = await new_images_module(db, config.data_root() / "images")

    tools = ToolRegistry(
        [SystemToolSet(), sandbox_tools, cron_jobs.tools, PlanToolSet(run_plans, plan_store)]
    )
    mcp_runtime = MCPRuntime(users.mcp)

    agent_runs = Service(
        Dependencies(
            models=model_catalog.catalog,
            settings=users.settings,
            providers=users.providers,
            store=session_store,
            usage=usage.recorder,
            skills=skills,
            images=images.agent_input,
            files=sandbox_input,
            mcp=mcp_runtime,
            memories=workspace_memories,
            constraints=ConstraintStore(db),
            plans=run_plans,
            plan_store=plan_store,
            approval_store=ApprovalStore(db),
            # 项目级档位的取数端：有工作区的 run 先看项目设置，没有就跟着用户级走
            workspaces=workspaces,
            # 工作区上下文快照的取数端：绑定状态必须在 run 开始时进入执行链，
            # 否则「绑定目录」只活在面板那条链里（P0-2 的根因）
            workspace_contexts=workspace_volumes,
            tool_definitions=tools.tool_definitions(),
            tool_executor=tools,
            traces=traces,
        )
    )
    # 上一个进程遗留的待批准已经没有等待者了：留着它们，界面上会出现一张
    # 「永远在等你确认」的幽灵卡片。run 侧的 task_status 仍是 in_progress，
    # 走既有的续跑，与这里的过期处理互不依赖（见 docs/permissions-p2-approval.md）。
    if agent_runs.approvals is not None:
        try:
            expired = await agent_runs.approvals.expire_stale()
            if expired:
                logger.info("approval_expired_on_startup count=%d", expired)
        except Exception:  # 清理失败不阻塞启动
            logger.warning("expire stale approvals failed", exc_info=True)

    gateway = new_gateway_service(agent_runs)
    web = Adapter(gateway)
    cron_adapter = CronAdapter(gateway)
    cron_scheduler = cron_jobs.new_scheduler(cron_adapter)

    app.include_router(users.http.router)
    app.include_router(build_mcp_router(users.mcp))
    app.include_router(model_catalog.http.router)
    app.include_router(build_workspace_router(workspaces, workspace_volumes))
    app.include_router(build_instruction_router(workspace_memories))
    app.include_router(build_memory_router(workspace_memories))
    app.include_router(conversations.http.router)
    app.include_router(SkillsHTTP(skills).router)
    app.include_router(SandboxHTTP(sandboxes).router)
    app.include_router(GitopsHTTP(git_push).router)
    app.include_router(cron_jobs.http.router)
    app.include_router(images.http.router)
    app.include_router(web.router)

    app.state.db = db
    scheduler_task = asyncio.create_task(cron_scheduler.run())

    yield

    scheduler_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass
    # 后台进程不随会话对象消失：run 结束不回收（`sandbox_start_process` 的语义就是
    # "长期进程"），但**后端退出必须一起带走**。否则它们成为没有主人的孤儿，
    # 继续占着工作区文件——Windows 上表现为"目录删不掉"，且没有任何界面能看见它们。
    # 每个会话的回收条数由 `processes.release()` 自己记日志，这里不再重复统计。
    sandbox_processes.release_all()
    # 先摘掉审计单例再关连接：否则后续若有命令落一条记录，会写到一个已关闭的
    # 连接上（表现为 "Cannot operate on a closed database" 的噪声日志）。
    isolation_layer.store.reset()
    await db.close()


def create_app() -> FastAPI:
    """创建并组装 FastAPI 应用。"""
    app = FastAPI(title="EDITH backend", version="0.1.0", lifespan=lifespan)
    http_utils.register_error_handler(app)

    @app.middleware("http")
    async def require_runtime_token(request: Request, call_next):
        """本地 Runtime 的入站鉴权：只放行带令牌的 BFF。

        这个进程监听回环端口，同机任意进程（包括任意网页发起的跨站请求）都能
        连上来。端口随机只是遮挡，不是边界；令牌把「连得上」和「被允许调用」
        分开。令牌由 Electron 随机生成，分别注入 backend 与 web 的 env。

        放行的三种情况：
        - 没配令牌（开发直连 / pytest）：放行并警告一次——**不降级成内置默认值**，
          那样等于没有鉴权；
        - `/health`：Electron 在拿到任何业务接口之前就要确认进程起来了；
        - `OPTIONS`：CORS 预检本身不带自定义头，由外层 CORSMiddleware 处理。
        """
        global _warned_no_runtime_token

        token = config.runtime_token()
        if not token:
            if not _warned_no_runtime_token:
                _warned_no_runtime_token = True
                logger.warning(
                    "EDITH_RUNTIME_TOKEN 未设置：本地 Runtime 接口未鉴权，仅适用于开发与测试"
                )
            return await call_next(request)
        if request.url.path == "/health" or request.method == "OPTIONS":
            return await call_next(request)

        supplied = request.headers.get("X-ETHOS-RUNTIME-TOKEN", "").strip()
        if not supplied:
            scheme, _, value = request.headers.get("Authorization", "").partition(" ")
            value = value.strip()
            if scheme.lower() == "bearer":
                supplied = value
            elif scheme.lower() == "basic":
                # 兼容 `http://user:token@127.0.0.1:port` 形式的 base URL
                try:
                    decoded = base64.b64decode(value).decode("utf-8", "replace")
                except Exception:  # noqa: BLE001 - 解析不了就当作没带
                    decoded = ""
                supplied = decoded.partition(":")[2]

        if not hmac.compare_digest(supplied, token):
            logger.warning(
                "runtime_token_rejected path=%s client=%s",
                request.url.path,
                request.client.host if request.client else None,
            )
            return JSONResponse(
                {"type": "unauthorized", "message": "missing or invalid runtime token"},
                status_code=401,
            )
        return await call_next(request)

    # 收紧到本地 Web 源。桌面端 Web 端口是随机分配的（`findFreePort()`），
    # 所以用正则而不是固定白名单：`http://127.0.0.1:<任意端口>` / `localhost`。
    # 原来的 `allow_origins=["*"]` 是为「浏览器直传大文件」（方案 C）留的，
    # 该方案已废弃——现在所有请求都经 Next BFF 同源转发，浏览器不再直连 Runtime。
    # 顺序：CORS 在外层，预检与 401 响应都能带上 CORS 头。
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^http://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    @app.middleware("http")
    async def add_observability(request: Request, call_next):
        # 生成或提取 request_id
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())

        # 绑定到 ContextVar（之后所有日志自动带上它）
        token = log_context.bind_request_id(request_id)

        # 记录请求开始（结构化日志）
        logger.info(
            "http_request_start method=%s path=%s client_ip=%s",
            request.method,
            request.url.path,
            request.client.host if request.client else None,
        )

        start_time = time.time()
        try:
            response = await call_next(request)

            # 记录请求结束（包含耗时）
            duration_ms = (time.time() - start_time) * 1000
            logger.info(
                "http_request_end status=%d duration_ms=%.2f",
                response.status_code,
                duration_ms,
            )

            # 把 request_id 原样返回给客户端（方便前端联调）
            response.headers["X-Request-ID"] = request_id
            return response

        except Exception:
            # 捕获未处理的异常（防止中间件把异常吞掉，但记录日志）
            duration_ms = (time.time() - start_time) * 1000
            logger.exception("http_request_exception duration_ms=%.2f", duration_ms)
            raise  # 交给 FastAPI 的全局异常处理器
        finally:
            # 重置 ContextVar，避免污染其他请求
            log_context.reset_request_id(token)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/internal/sandbox/health")
    async def sandbox_health() -> dict:
        """执行隔离的能力与选择结果（设置页 / 诊断用）。

        只报告后端类型、版本、能力缺口与拒绝原因，不返回策略内容与用户绝对路径。
        注意它与 `/health` 不同：`/health` 是进程存活探针（免令牌、必须极快），
        这里是**安全状态**，走正常鉴权。
        """
        return isolation_layer.health()

    @app.get("/internal/sandbox/audit")
    async def sandbox_audit(
        sessionId: str = "",
        requestId: str = "",
        workspaceId: str = "",
        limit: int = 50,
    ) -> dict:
        """按会话 / 请求 / 工作区查询沙箱执行审计（Trace 面板数据源）。

        只返回已脱敏的字段：命令文本在入库前已处理，环境变量只有键名。
        """
        target = isolation_layer.store.current()
        if target is None:
            return {"entries": []}
        entries = await target.query(
            session_id=sessionId,
            request_id=requestId,
            workspace_id=workspaceId,
            limit=limit,
        )
        return {"entries": entries}

    return app


app = create_app()


def main() -> None:
    """开发启动入口：`uv run edith-backend`。"""
    import uvicorn

    config.load_environment()
    host, _, port = config.runtime_address().rpartition(":")
    uvicorn.run(
        "edith_backend.main:app",
        host=host or "127.0.0.1",
        port=int(port) if port else 8080,
        reload=True,
    )
