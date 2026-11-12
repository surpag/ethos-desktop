"""配置聚合：从各功能模块加载一次运行所需的配置。

对应 Go 版 internal/agentrun/configuration.go。
"""

from __future__ import annotations

import logging
import posixpath
from dataclasses import dataclass, field

from ..models.catalog import DEFAULT_MODEL_ID, Catalog, ModelDefinition
from ..session.constraint_store import (
    ConstraintStore,
    constraint_budget_chars,
    constraints_enabled,
)
from ..skills.catalog import Catalog as SkillsCatalog
from ..skills.instruction import render_skill_instruction
from ..skills.types import Skill
from ..trace import PREPARE, Tracer
from ..userconfig.mcp_runtime import MCPRuntime
from ..userconfig.providers import Providers
from ..userconfig.settings import Settings
from ..workspace.memory_store import WorkspaceMemoryStore
from .types import Error, Request

logger = logging.getLogger(__name__)


# 与 Go 版保持一致的系统身份前缀
_BASE_IDENTITY = (
    "你是 EDITH AI Agent智能助手。请全程使用中文推理和回答，包括你的内部思考过程。\n"
    # 计划纪律：只说「先列计划」不够——必须同时说清「标完成要能被核对」，
    # 否则模型会把计划当成另一种更可信的自述（打勾比说话更像证据，但同样可能没做）。
    "多步任务（超过 3 步）先用 plan_update 建立计划，之后随进展更新；"
    "把某一项标为 done 时必须给出可核对的证据（改动的文件路径，或命令的退出码），"
    "没做成不要打勾。\n\n"
)


@dataclass
class ConfiguredRun:
    """已经可以交给执行层的完整运行输入。"""

    request: Request
    definition: ModelDefinition
    api_key: str
    system_instruction: str
    image_urls: list[str]
    message: str
    # mcp_tools: MCPToolContext | None = None


class RunConfigurations:
    """聚合模型、用户设置、供应商凭据、Skill 摘要、图片与上传文件。"""

    def __init__(
        self,
        models: Catalog,
        settings: Settings,
        providers: Providers,
        skills: SkillsCatalog | None = None,
        images=None,
        files=None,
        mcp: MCPRuntime | None = None,
        memories: WorkspaceMemoryStore | None = None,
        constraints: ConstraintStore | None = None,
    ) -> None:
        self.models = models
        self.settings = settings
        self.providers = providers
        self.skills = skills
        self.images = images
        self.files = files
        self.mcp = mcp
        self.memories = memories
        self.constraints = constraints

    async def load(self, request: Request, tracer: Tracer) -> ConfiguredRun | Error:
        # 每个阶段一个 prepare 子步骤：准备阶段曾经是整体黑盒，
        # 「点了发送半天没反应」时无法区分是这一步慢还是模型慢。
        # 1. 解析模型：请求未指定则用用户默认，再兜底平台默认
        async with tracer.step(PREPARE, "model.resolve") as model_step:
            # 优先级：请求显式指定 > 用户默认 > 平台默认。
            # 后两者是「可能失效的引用」——用户删掉自己加的自定义模型后，默认值会
            # 指向一个已不存在的 ID；显式指定则不做回退（用户选的就是它，报错更诚实）。
            explicit = bool(request.model_id)
            if not request.model_id:
                request.model_id = await self.settings.load_default_model_id(
                    request.user_id
                )
            if not request.model_id:
                request.model_id = DEFAULT_MODEL_ID

            definition = await self.models.find_for(request.user_id, request.model_id)
            if definition is None and not explicit and request.model_id != DEFAULT_MODEL_ID:
                request.model_id = DEFAULT_MODEL_ID
                definition = await self.models.find_for(
                    request.user_id, request.model_id
                )
            if definition is None:
                return Error("invalid_request", "unsupported modelId")

            # 2. 图片输入需模型支持视觉
            if request.image_ids and not definition.vision:
                return Error(
                    "invalid_request", "selected model does not support image input"
                )
            model_step.metadata["model"] = definition.id

        # 3. 加载供应商密钥
        try:
            async with tracer.step(PREPARE, "provider.api_key"):
                api_key = await self.providers.load_api_key(
                    request.user_id, definition.provider_id
                )
        except LookupError as exc:
            return Error("invalid_request", f"load model credential: {exc}")

        # 4. 加载人格提示词
        async with tracer.step(PREPARE, "settings.personality"):
            personality = await self.settings.load_personality(request.user_id)

        # 5. 校验上传文件（uploads/ 目录内已存在的普通文件）
        uploads: list[str] = []
        if request.upload_paths and self.files is not None:
            try:
                async with tracer.step(PREPARE, "uploads.validate"):
                    uploads = await self.files.validate_uploads(
                        request.user_id, request.session_id, request.upload_paths
                    )
            except Exception as exc:  # noqa: BLE001 - 文件校验失败统一转 invalid_request
                return Error("invalid_request", f"prepare uploaded files: {exc}")

        # 6. 扫描 Skill 目录（单层，没有作用域可解析）
        skills: list[Skill] = []
        if self.skills is not None:
            async with tracer.step(PREPARE, "skills.summaries") as skills_step:
                scan = self.skills.scan()
                skills = scan.skills
                # 三件事都要落 trace：扫到几个、目录在不在、有没有坏掉的条目。
                # 只记「注入了几个」的话，「一个都没注进去」在 trace 里完全没有痕迹
                # ——那恰恰是最需要看 trace 的时刻。
                skills_step.metadata["count"] = len(skills)
                skills_step.metadata["status"] = "ok" if scan.exists else "missing"
                if scan.skipped:
                    skills_step.metadata["skipped"] = len(scan.skipped)
                    skills_step.metadata["skippedNames"] = ",".join(
                        name for name, _ in scan.skipped
                    )

        # 6.5 加载工作区记忆（指令 + 自动记忆）
        workspace_memory = ""
        if request.workspace_id and self.memories is not None:
            async with tracer.step(PREPARE, "workspace.memory") as memory_step:
                workspace_memory = await self.memories.load_injectable(
                    request.user_id, request.workspace_id
                )
                memory_step.metadata["chars"] = len(workspace_memory)

        system_instruction = _BASE_IDENTITY + personality
        # 6.6 约束层：历史之外的一层，永不压缩。
        # 位置刻意靠前且独立成段——它和【项目背景】的性质不同：项目背景是「背景信息」，
        # 约束是「不许违反的边界」。混在一起写会让模型把硬约束当参考资料。
        if constraints_enabled() and self.constraints is not None:
            async with tracer.step(PREPARE, "constraints.inject") as constraint_step:
                constraint_block, injected, omitted = await self.constraints.render_for_prompt(
                    request.user_id,
                    session_id=request.session_id,
                    workspace_id=request.workspace_id,
                    budget_chars=constraint_budget_chars(),
                )
                constraint_step.metadata["injected"] = injected
                constraint_step.metadata["omitted"] = omitted
            if constraint_block:
                system_instruction += "\n\n" + constraint_block
            if omitted:
                # 绝不静默丢弃：预算截断必须可见，否则「约束没生效」无法归因。
                logger.warning(
                    "constraints_truncated user=%s session=%s injected=%d omitted=%d",
                    request.user_id,
                    request.session_id,
                    injected,
                    omitted,
                )

        if workspace_memory:
            system_instruction += "\n\n【项目背景】\n" + workspace_memory
        # 6.7 注入渲染：唯一来源是上面扫出来的 skills。
        # 单独一个 trace step 是因为 `omitted` 必须同时出现在指令与 trace 里，
        # 缺任何一边都是「静默丢内容」。这里把这两个数一并交给调用方。
        async with tracer.step(PREPARE, "skills.inject") as inject_step:
            skills_render = render_skill_instruction(skills)
            instruction = append_uploads(skills_render.text, uploads)
            inject_step.metadata["injected"] = len(skills_render.injected)
            inject_step.metadata["budgetChars"] = skills_render.budget_chars
            if skills_render.omitted:
                inject_step.metadata["omitted"] = len(skills_render.omitted)
                # 名字一起记：只有数字时，"哪些被截了"要靠人重新算预算才知道
                inject_step.metadata["omittedNames"] = ",".join(skills_render.omitted)

        # 7. 把图片 ID 转为短期 URL
        image_urls: list[str] = []
        if request.image_ids and self.images is not None:
            try:
                async with tracer.step(PREPARE, "images.urls") as image_step:
                    image_urls = await self.images.message_image_urls(
                        request.user_id, request.session_id, request.image_ids
                    )
                    image_step.metadata["images"] = len(image_urls)
            except Exception as exc:  # noqa: BLE001 - 图片加载失败统一转 invalid_request
                return Error("invalid_request", f"load image input: {exc}")

        # 8. 拼装最终 system prompt
        # 本次修改：删除下方对 system_instruction 的重复整体赋值（曾覆盖掉第 6.5 步注入的
        # 【项目背景】/workspace_memory，属既有 bug），改为只追加 instruction 与工作区上下文。
        if instruction:
            system_instruction += "\n\n" + instruction

        # 9. 注入当前会话工作区上下文（配合工具层自动补全，模型无需拼 workspace_id）
        # 本次新增：Web 场景模型无「当前目录」概念，显式告知其文件操作基座，减少误判。
        if request.workspace_id:
            system_instruction += (
                "\n\n【当前会话工作区】\n"
                f"当前会话关联工作区 ID：{request.workspace_id}。\n"
                "你的文件操作基于该工作区（Sandbox 内路径 workspace/{workspace_id}/...）。"
                "调用 Sandbox 文件工具时无需关心路径前缀，直接传相对路径"
                "（如 README.md、src/main.py）即可，工具会自动定位到该工作区。"
            )

        # 8. 打开用户启用的 MCP 服务工具
        # mcp_tools: MCPToolContext | None = None
        # if self.mcp is not None:
        #     mcp_tools = await self.mcp.open_tools(request.user_id)

        return ConfiguredRun(
            request=request,
            definition=definition,
            api_key=api_key,
            system_instruction=system_instruction,
            image_urls=image_urls,
            message=message_with_uploads(request.message, uploads),
            # mcp_tools=mcp_tools,
        )


def message_with_uploads(content: str, uploads: list[str]) -> str:
    """把附件名称写入用户消息，保证会话历史也能展示本次上传。"""
    content = content.strip()
    if not uploads:
        return content
    files = [f"- `{posixpath.basename(upload)}`" for upload in uploads]
    attachments = "附件：\n" + "\n".join(files)
    if not content:
        return attachments
    return content + "\n\n" + attachments


def append_uploads(instruction: str, uploads: list[str]) -> str:
    """把本次上传文件清单接在 Skill 段落之后（无附件时原样返回）。"""
    if not uploads:
        return instruction
    files = (
        "本次用户上传文件：\n- "
        + "\n- ".join(uploads)
        + "\n请通过 Sandbox 文件工具从 uploads/ 读取这些文件。"
    )
    if not instruction:
        return files
    return instruction + "\n\n" + files
