"""conversation 的 HTTP 边界。

对应 Go 版 internal/conversation/http.go + routes.go。
"""

from __future__ import annotations

from fastapi import APIRouter, status

from ..http_utils import EDITHError
from ..permissions.policy import InvalidLevelValue, parse_level_input
from ..session.store import WorkspaceSessionConflict
from .history import ConversationNotFound, History
from .types import (
    ConversationLevelRequest,
    ConversationLevelResponse,
    ConversationListResponse,
    ConversationResponse,
    WorkspaceSessionResponse,
)


class HTTP:
    def __init__(self, history: History) -> None:
        self.history = history
        self.router = self._build_router()

    def _build_router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/internal/conversations", response_model=ConversationListResponse)
        async def list_conversations(
            userId: str = "", includeArchived: bool = False
        ) -> ConversationListResponse:
            if not userId.strip():
                raise EDITHError(400, "invalid_request", "userId is required")
            conversations = await self.history.list(userId, include_archived=includeArchived)
            return ConversationListResponse(conversations=conversations)

        # 静态子路径必须排在 `/{session_id}` 之前：否则 "by-workspace" 会被当成会话 ID。
        @router.get(
            "/internal/conversations/by-workspace/{workspace_id}",
            response_model=WorkspaceSessionResponse,
        )
        async def workspace_session(
            workspace_id: str, userId: str = ""
        ) -> WorkspaceSessionResponse:
            """查询工作区当前的活跃会话（一个工作区只允许一个会话）。"""
            if not userId.strip() or not workspace_id.strip():
                raise EDITHError(
                    400, "invalid_request", "userId and workspaceId are required"
                )
            session_id = await self.history.active_in_workspace(userId, workspace_id)
            return WorkspaceSessionResponse(session_id=session_id)

        @router.get(
            "/internal/conversations/{session_id}",
            response_model=ConversationResponse,
        )
        async def get_conversation(session_id: str, userId: str = "") -> ConversationResponse:
            if not userId.strip() or not session_id.strip():
                raise EDITHError(400, "invalid_request", "userId and sessionID are required")
            try:
                return await self.history.get(userId, session_id)
            except ConversationNotFound:
                raise EDITHError(404, "conversation_not_found", "conversation not found")

        @router.put(
            "/internal/conversations/{session_id}/permission-level",
            response_model=ConversationLevelResponse,
        )
        async def set_permission_level(session_id: str, req: ConversationLevelRequest):
            """设置/清除本会话的档位上限。

            语义是**上限**不是覆盖：它只能把这一档收得更严，不能把用户级的只读
            放宽回完全（见 permissions/policy.py::resolve_chain）。

            响应同时回「设置值」与「生效值」：用户把本会话设成完全、但用户级是
            只读时，`permissionLevel` 是完全、`effectivePermissionLevel` 是只读。
            正因为两个都要回，界面才不需要猜——它只要把差异说出来。
            """
            if not req.user_id.strip() or not session_id.strip():
                raise EDITHError(
                    400, "invalid_request", "userId and sessionID are required"
                )
            try:
                level = parse_level_input(req.permission_level)
            except InvalidLevelValue as exc:
                raise EDITHError(
                    400,
                    "invalid_permission_level",
                    "permissionLevel 只能是 read_only / approval / full 或空",
                ) from exc
            session_level, effective, binding = await self.history.set_permission_level(
                req.user_id, session_id, level
            )
            return ConversationLevelResponse(
                session_id=session_id,
                permission_level=session_level,
                effective_permission_level=effective,
                permission_binding=binding,
            )

        @router.post(
            "/internal/conversations/{session_id}/archive",
            status_code=status.HTTP_204_NO_CONTENT,
        )
        async def archive_conversation(session_id: str, userId: str = ""):
            """本次新增：归档会话（从侧边栏隐藏，保留数据）。"""
            if not userId.strip() or not session_id.strip():
                raise EDITHError(400, "invalid_request", "userId and sessionID are required")
            await self.history.archive(userId, session_id)

        @router.post(
            "/internal/conversations/{session_id}/unarchive",
            status_code=status.HTTP_204_NO_CONTENT,
        )
        async def unarchive_conversation(session_id: str, userId: str = ""):
            """本次新增：取消归档（恢复会话到侧边栏）。"""
            if not userId.strip() or not session_id.strip():
                raise EDITHError(400, "invalid_request", "userId and sessionID are required")
            try:
                await self.history.unarchive(userId, session_id)
            except WorkspaceSessionConflict as exc:
                # 该工作区已有活跃会话：不能恢复，否则就出现两个会话共用一份镜像目录
                raise EDITHError(
                    409,
                    "workspace_session_conflict",
                    "该工作区已有会话，请先归档或删除它再恢复此会话。",
                ) from exc

        @router.delete(
            "/internal/conversations/{session_id}",
            status_code=status.HTTP_204_NO_CONTENT,
        )
        async def delete_conversation(session_id: str, userId: str = ""):
            """本次新增：删除整个会话（消息 + 摘要 + 归档状态）。"""
            if not userId.strip() or not session_id.strip():
                raise EDITHError(400, "invalid_request", "userId and sessionID are required")
            deleted = await self.history.delete(userId, session_id)
            if not deleted:
                raise EDITHError(404, "conversation_not_found", "conversation not found")

        return router
