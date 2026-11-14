"""conversation 的类型定义。

对应 Go 版 internal/conversation/types.go。
Timeline 的块用 Pydantic 联合类型表达多态。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from ..agentstream.types import PlanSnapshot
from ..usage.types import Summary


class Conversation(BaseModel):
    """侧边栏展示的一段会话摘要。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    title: str
    updated_at: str
    workspace_id: str = ""  # 空 = 普通对话（不属于任何工作区）
    archived: bool = False  # 本次新增：是否已归档


class ConversationListResponse(BaseModel):
    """会话列表 HTTP 响应。"""

    conversations: list[Conversation]


class WorkspaceSessionResponse(BaseModel):
    """某个工作区当前的活跃会话。

    sessionId 为空表示该工作区还没有会话（可以新建）。非空时前端必须直接打开它 ——
    一个工作区只允许一个会话，否则两个沙箱会互相覆盖同一份目录镜像。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    session_id: str = ""


class ConversationResponse(BaseModel):
    """一个会话的 Timeline 和用量 HTTP 响应。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    timeline: Timeline
    usage: Summary
    # 该会话最近一轮的计划快照。计划是 run 级状态、一轮一份：只给「最近一份」
    # 就够——前端按 requestId 把它挂到产生它的那一轮下面，新的一轮不会继承它。
    plan: PlanSnapshot | None = None
    # 任务状态（in_progress/completed/canceled）；None = 从未有 run。
    # 这是「还有没跑完的活」的**权威来源**：刷新后仍然存在，
    # 不依赖前端 sessionStorage 里那份一次性的 pending 记录。
    task_status: str | None = None
    # 本会话设置的档位**上限**（空 = 未设置）。
    permission_level: str = ""
    # 实际生效档位：与用户级 / 工作区级取最严者。**界面应当展示这个**，
    # 否则用户在会话里选了「完全」、看到的下拉也是「完全」，而实际按只读在跑。
    effective_permission_level: str = ""
    # 把档位压到 effective 的那一级（user/workspace/session）；空 = 没人收紧。
    # 用途只有一个：让「为什么不是完全档」有答案，而不是让用户以为开关坏了。
    permission_binding: str = ""


class ConversationLevelRequest(BaseModel):
    """PUT /internal/conversations/{id}/permission-level 的请求体。

    传三档之一 = 设置本会话上限；传 `""` = 清除（回到「本会话不额外收紧」）。
    非法值 400，不静默降级。
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    user_id: str = Field(..., min_length=1)
    permission_level: str = ""


class ConversationLevelResponse(BaseModel):
    """写入后的档位状态：设置值 + 生效值 + 收紧者（三者的区别见 History.level_state）。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    session_id: str
    permission_level: str = ""
    effective_permission_level: str = ""
    permission_binding: str = ""


class UserImage(BaseModel):
    """用户消息中引用的 EDITH 图片。"""

    id: str


class UserBlock(BaseModel):
    """一条用户消息卡片。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    type: str = "user"
    id: str
    content: str
    images: list[UserImage] = Field(default_factory=list)
    created_at: str


class AssistantContentBlock(BaseModel):
    """reasoning、text 或 tool 的展示块。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    type: str
    id: str
    content: str = ""
    tool_name: str = ""
    arguments: str = ""
    status: str = ""
    result: str = ""


class AssistantBlock(BaseModel):
    """一整段 AI 回复卡片。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    type: str = "assistant"
    id: str
    created_at: str
    blocks: list[AssistantContentBlock] = Field(default_factory=list)
    # 本轮耗时（毫秒）；旧数据或未记录时为 None
    duration_ms: int | None = None
    # 完成证据等级（mutated/external/read_only/none）。
    # None = 未知（该字段引入前的历史消息），UI 不应把它显示成「无工具调用」。
    completion_evidence: str | None = None
    # 任务语义（complete/incomplete）；None = 未知（该字段引入前的历史消息）。
    # 由服务端从 end_reason 派生后下发，UI 不参与判定，只负责呈现。
    outcome: str | None = None
    # 终止原因，仅用于选择提示文案；未知原因必须走兜底文案，不能静默。
    end_reason: str | None = None
    # 产生这条回复的 run。计划按 run 归档（run_plans.request_id），前端据此把
    # 计划面板定位到它所属的那一轮；None = 旧数据，前端退化为按会话展示。
    request_id: str | None = None


class ErrorBlock(BaseModel):
    """一次历史运行错误卡片。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    type: str = "error"
    id: str
    message: str
    created_at: str


class Timeline(BaseModel):
    """浏览器按时间顺序展示的一段对话。"""

    blocks: list[UserBlock | AssistantBlock | ErrorBlock] = Field(default_factory=list)
