"""webadapter 适配器：HTTP/SSE 协议 → Gateway 调用。

对应 Go 版 internal/webadapter/adapter.go + stream.go + control.go。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ..agentrun.types import Error
from ..agentstream.types import Event
from ..gateway.service import Service as GatewayService
from ..gateway.types import WEB_CHANNEL, IncomingMessage
from ..http_utils import EDITHError
from .types import ApprovalDecisionRequest, MessageRequest


def _gateway_error_to_http(error: Error) -> EDITHError:
    """把 agentrun.Error 映射为 HTTP 状态码与统一错误响应。"""
    status = 500
    if error.type == "invalid_request":
        status = 400
    # workspace_busy 同样是「用户可自行化解的操作冲突」（换个工作区或等它跑完），
    # 不是服务端故障。落进 500 会让前端只会弹通用错误，也无法给出 holder 提示。
    elif error.type in ("session_busy", "workspace_busy", "request_conflict"):
        status = 409
    elif error.type == "not_found":
        status = 404
    return EDITHError(status, error.type, error.message)


def _sse_response(events: AsyncIterator[Event | None]) -> StreamingResponse:
    """把事件流包成 SSE 响应。

    序列化口径统一在这里：`by_alias=True` 输出 camelCase（assistantId/blockId/
    toolCallId…），与 Go 版 agentstream.Event 的 json tag 保持一致。
    """

    async def event_generator() -> AsyncIterator[str]:
        async for event in events:
            if event is None:
                break
            yield f"data: {event.model_dump_json(exclude_none=True, by_alias=True)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


class Adapter:
    """Web 渠道入口，只理解 HTTP、JSON 和 SSE。"""

    def __init__(self, gateway: GatewayService) -> None:
        self.gateway = gateway
        self.router = self._build_router()

    def _build_router(self) -> APIRouter:
        router = APIRouter()

        @router.post("/internal/gateway/messages:stream")
        async def stream_message(body: MessageRequest) -> StreamingResponse:
            stream, run_error = await self.gateway.run(
                IncomingMessage(
                    channel=WEB_CHANNEL,
                    external_user_id=body.user_id,
                    session_key=body.session_id,
                    request_id=body.request_id,
                    message=body.message,
                    image_ids=body.image_ids,
                    upload_paths=body.upload_paths,
                    model_id=body.model_id,
                    reasoning_option_id=body.reasoning_option_id,
                    workspace_id=body.workspace_id,
                )
            )
            if run_error is not None:
                raise _gateway_error_to_http(run_error)
            assert stream is not None
            return _sse_response(stream.open(0))

        @router.get("/internal/gateway/runs/{request_id}/events")
        async def resume_run_events(
            request_id: str, userId: str = "", after: int = 0
        ) -> StreamingResponse:
            """断流重连：补发 after 之后的事件，再接实时。

            刷新页面时前端用 sessionStorage 里记下的最大 seq 回到这里，
            于是已经发生的工具调用与执行过程立刻可见，而不是只剩一句
            「任务仍在后台执行」。run 已结束时同样可用（日志会暂存一段时间），
            补完剩余事件即关闭。
            """
            stream, run_error = self.gateway.resume(userId, request_id, max(after, 0))
            if run_error is not None:
                raise _gateway_error_to_http(run_error)
            assert stream is not None
            return _sse_response(stream.open(max(after, 0)))

        @router.get("/internal/gateway/runs/{request_id}")
        async def run_status(request_id: str, userId: str = "") -> dict:
            status, run_error = self.gateway.status(userId, request_id)
            if run_error is not None:
                raise _gateway_error_to_http(run_error)
            return {"requestId": status.request_id, "status": status.status}

        @router.post("/internal/gateway/runs/{request_id}/cancel")
        async def cancel_run(request_id: str, userId: str = "") -> None:
            run_error = self.gateway.cancel(userId, request_id)
            if run_error is not None:
                raise _gateway_error_to_http(run_error)

        @router.get("/internal/approvals")
        async def list_approvals(
            userId: str = "", requestId: str = "", sessionId: str = ""
        ) -> dict:
            """待批准列表（刷新兜底）。

            主路径是 SSE 的 `approval.requested`；这个接口只解决「刷新之后事件
            日志已经归档」的情况，以及「切回会话时看一眼还有没有悬着的操作」。
            """
            items = await self.gateway.pending_approvals(
                userId, request_id=requestId, session_id=sessionId
            )
            return {"approvals": [_approval_to_dict(item) for item in items]}

        @router.post("/internal/approvals/{approval_id}")
        async def decide_approval(
            approval_id: str, body: ApprovalDecisionRequest
        ) -> None:
            """记录一次审批决策。

            409 的语义是「你的点击来晚了」（另一个标签页先处理 / 已超时 / run 已取消），
            不是错误：前端应静默关闭确认框，等 `approval.resolved` 事件同步终态。
            """
            decision = (body.decision or "").strip().lower()
            if decision not in ("approve", "deny"):
                raise EDITHError(
                    400, "invalid_request", "decision must be 'approve' or 'deny'"
                )
            scope = (body.scope or "once").strip().lower()
            if scope not in ("once", "run"):
                scope = "once"
            result = await self.gateway.decide_approval(
                body.user_id, approval_id, approve=decision == "approve", scope=scope
            )
            if result == "not_found":
                raise EDITHError(404, "not_found", "approval not found")
            if result == "previous":
                raise EDITHError(
                    409, "approval_already_decided", "approval already decided"
                )

        return router


def _approval_to_dict(row: dict) -> dict:
    """审批记录 → 前端契约（camelCase，与事件里的 approval 同构）。

    刻意不带 `expiresAt`：落库时没有记（过期只对「正在等的那张票」有意义，
    而那张票在进程内）。宁可不给，也不给一个永远是空串的字段——那会让前端
    以为「这条不会过期」。
    """
    return {
        "approvalId": row.get("approval_id", ""),
        "requestId": row.get("request_id", ""),
        "sessionId": row.get("session_id", ""),
        "toolCallId": row.get("tool_call_id", ""),
        "toolName": row.get("tool_name", ""),
        "arguments": row.get("arguments", ""),
        "capabilities": row.get("capabilities") or [],
        "status": row.get("status", ""),
        "scope": row.get("scope", ""),
    }
