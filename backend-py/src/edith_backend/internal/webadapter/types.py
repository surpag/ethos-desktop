"""webadapter 的类型定义。

对应 Go 版 internal/webadapter/types.go。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class MessageRequest(BaseModel):
    """Web BFF 发给后端的流式消息契约。userId 由 BFF 在服务端注入。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    request_id: str
    user_id: str
    session_id: str
    message: str = ""
    image_ids: list[str] = Field(default_factory=list)
    upload_paths: list[str] = Field(default_factory=list)
    model_id: str = ""
    reasoning_option_id: str = ""
    workspace_id: str = ""


class ApprovalDecisionRequest(BaseModel):
    """一次审批决策。`userId` 由 BFF 在服务端注入，不信任浏览器传来的身份。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    user_id: str
    decision: str  # approve / deny
    #: once = 只这一次；run = 本次运行内同名工具不再问（不跨 run）
    scope: str = "once"
