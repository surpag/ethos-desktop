"""images 的 HTTP 接口。

对应 Go 版 internal/images/http.go + routes.go。与上游的差别只在存储：
上游 create 返回 COS 预签名 PUT 地址、open 返回 302 到签名 GET 地址；
桌面端两者都指向本进程——PUT 收字节，GET 直接吐字节。
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from ..http_utils import EDITHError
from .service import ImageTooLarge, Service
from .store import ImageNotFound
from .types import (
    CompleteUploadRequest,
    CompleteUploadResponse,
    CreateUploadRequest,
    CreateUploadResponse,
    UploadInput,
)


def _image_dict(image) -> dict:
    return {"id": image.id, "mimeType": image.mime_type}


class HTTP:
    def __init__(self, service: Service) -> None:
        self._service = service
        self.router = self._build_router()

    def _build_router(self) -> APIRouter:
        router = APIRouter()

        @router.post("/internal/images", response_model=CreateUploadResponse)
        async def create_upload(body: CreateUploadRequest) -> CreateUploadResponse:
            if not body.user_id.strip() or not body.session_id.strip() or not body.mime_type.strip() or body.size_bytes <= 0:
                raise EDITHError(400, "invalid_request", "userId, sessionId, mimeType, and sizeBytes are required")
            try:
                image, target = await self._service.create_upload(
                    body.user_id,
                    UploadInput(session_id=body.session_id, mime_type=body.mime_type, size_bytes=body.size_bytes),
                )
            except ValueError as exc:
                raise EDITHError(400, "image_upload_rejected", str(exc))
            return CreateUploadResponse(image=_image_dict(image), upload_url=target)

        @router.put("/internal/images/{image_id}/blob", status_code=204)
        async def put_blob(image_id: str, request: Request, userId: str = "") -> Response:
            if not userId.strip() or not image_id.strip():
                raise EDITHError(400, "invalid_request", "userId and imageID are required")
            try:
                await self._service.upload_blob(
                    userId,
                    image_id,
                    request.headers.get("content-type", ""),
                    request.stream(),
                )
            except ImageTooLarge as exc:
                # 413 而不是 400：字节是合法的，只是太大，客户端换小图即可重试。
                raise EDITHError(413, "image_too_large", str(exc))
            except ImageNotFound:
                # 没有这次预约（或不属于该用户）→ 404，别再透露 id 是否存在。
                raise EDITHError(404, "image_not_found", "image not found")
            except ValueError as exc:
                raise EDITHError(400, "image_upload_rejected", str(exc))
            return Response(status_code=204, headers={"Cache-Control": "no-store"})

        @router.post("/internal/images/{image_id}/complete", response_model=CompleteUploadResponse)
        async def complete_upload(image_id: str, body: CompleteUploadRequest) -> CompleteUploadResponse:
            if not body.user_id.strip() or not image_id.strip():
                raise EDITHError(400, "invalid_request", "userId and imageID are required")
            try:
                image = await self._service.complete_upload(body.user_id, image_id)
            except ImageNotFound:
                raise EDITHError(404, "image_not_found", "image not found")
            except ValueError as exc:
                raise EDITHError(400, "image_upload_rejected", str(exc))
            return CompleteUploadResponse(image=_image_dict(image))

        @router.get("/internal/images/{image_id}")
        async def open_image(image_id: str, userId: str = "") -> Response:
            if not userId.strip() or not image_id.strip():
                raise EDITHError(400, "invalid_request", "userId and imageID are required")
            try:
                data, mime_type = await self._service.read_for_user(userId, image_id)
            except Exception:  # noqa: BLE001 - 读取失败统一映射为 not_found
                raise EDITHError(404, "image_not_found", "image not found")
            # 图片 id 一旦 ready 就不再变化，可以长期缓存：会话里滚动时不必重下。
            return Response(
                content=data,
                media_type=mime_type,
                headers={"Cache-Control": "private, max-age=31536000, immutable"},
            )

        return router
