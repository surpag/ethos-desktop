"""images 服务：图片预留、上传确认与读取。

对应 Go 版 internal/images/service.go，但存储从 COS 换成本机磁盘：
桌面端没有对象存储，也没有「浏览器直传第三方」的必要。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

from .storage import ImageCorrupt, ImageTooLarge, LocalStorage
from .store import PENDING, READY, ImageRecord, Store
from .types import Image, UploadInput

MAX_IMAGE_BYTES = 10 << 20

_SUPPORTED_MIME = {"image/jpeg", "image/png", "image/webp"}


def supported_mime_type(mime_type: str) -> bool:
    return mime_type in _SUPPORTED_MIME


def max_image_bytes() -> int:
    return MAX_IMAGE_BYTES


def upload_path(image_id: str) -> str:
    """浏览器 PUT 字节的目标地址。

    返回的是 **BFF 的**相对路径而不是 runtime 的：浏览器与 Next 同源，同源
    才能「相对路径 fetch」；runtime 在另一个端口上，直连要处理 CORS，而这个
    地址还会被写进 `<img src>` 之外的上传链路，越简单越不容易错。
    """
    return f"/api/images/{image_id}/blob"


class Service:
    def __init__(self, store: Store, storage: LocalStorage) -> None:
        self._store = store
        self._storage = storage

    async def create_upload(self, user_id: str, input_: UploadInput) -> tuple[Image, str]:
        user_id = user_id.strip()
        input_.session_id = input_.session_id.strip()
        input_.mime_type = input_.mime_type.lower().strip()
        if not user_id or not input_.session_id:
            raise ValueError("userID and sessionID are required")
        if not supported_mime_type(input_.mime_type):
            raise ValueError(f'unsupported image mime type "{input_.mime_type}"')
        if input_.size_bytes <= 0 or input_.size_bytes > MAX_IMAGE_BYTES:
            raise ValueError(f"image size must be between 1 and {MAX_IMAGE_BYTES} bytes")

        image = Image(id="img_" + uuid4().hex, mime_type=input_.mime_type)
        record = ImageRecord(
            image=image, user_id=user_id, session_id=input_.session_id,
            object_key="images/" + image.id, size_bytes=input_.size_bytes, status=PENDING,
        )
        await self._store.insert(record)
        # 预约阶段不再向存储要任何东西：落盘路径由 id 决定，失败面都在写入与确认。
        return image, upload_path(image.id)

    async def upload_blob(
        self, user_id: str, image_id: str, mime_type: str, chunks: AsyncIterator[bytes]
    ) -> None:
        """接收字节并落盘（尚未确认）。

        用户/会话归属在预约时已记录，这里按 image_id 找回记录，避免「谁都能往
        任意 id 写」——写之前先证明这个 id 是一次真实预约。
        """
        record = await self._store.for_user(user_id, image_id, ready_only=False)
        declared = mime_type.split(";")[0].strip().lower()
        if declared and declared != record.image.mime_type:
            raise ValueError(
                f'content type "{declared}" does not match reservation '
                f'"{record.image.mime_type}"'
            )
        try:
            await self._storage.write(
                record.object_key, chunks, MAX_IMAGE_BYTES,
                expect_mime=record.image.mime_type,
            )
        except ImageCorrupt as exc:
            # 字节不是图片：文件已经清掉了，记录也没有保留价值，一起丢，
            # 免得这张 id 永远停在 pending 且永远 complete 不了。
            await self._discard(record)
            raise ValueError(str(exc)) from exc

    async def complete_upload(self, user_id: str, image_id: str) -> Image:
        record = await self._store.for_user(user_id, image_id, ready_only=False)
        if record.status == READY:
            return record.image
        try:
            obj = self._storage.stat(record.object_key)
        except FileNotFoundError:
            await self._discard(record)
            raise ValueError("uploaded image is missing")
        except ImageCorrupt as exc:
            await self._discard(record)
            raise ValueError(str(exc)) from exc
        # 大小与类型都必须对上预约：类型按魔数判，所以「改个后缀」绕不过去。
        if obj.size_bytes != record.size_bytes:
            await self._discard(record)
            raise ValueError("uploaded image does not match reservation")
        if obj.mime_type != record.image.mime_type:
            await self._discard(record)
            raise ValueError("uploaded image does not match reservation")
        await self._store.mark_ready(record.image.id)
        return record.image

    async def read_for_user(self, user_id: str, image_id: str) -> tuple[bytes, str]:
        record = await self._store.for_user(user_id, image_id, ready_only=True)
        return self._storage.read(record.object_key)

    async def read_for_session(
        self, user_id: str, session_id: str, image_id: str
    ) -> tuple[bytes, str]:
        """Agent 视野内的读取：必须同属该会话，防止把别人的图喂给模型。"""
        record = await self._store.for_session(user_id, session_id, image_id)
        return self._storage.read(record.object_key)

    async def _discard(self, record: ImageRecord) -> None:
        """记录与文件一起丢：先删文件，删不掉也要把记录删掉。

        反过来的顺序会留下「记录没了但文件永远占着盘」的孤儿——文件删失败
        只影响空间，记录残留却会让用户重试时撞到 `upload does not match`。
        """
        self._storage.delete(record.object_key)
        await self._store.delete(record.image.id)


__all__ = [
    "MAX_IMAGE_BYTES",
    "ImageCorrupt",
    "ImageTooLarge",
    "Service",
    "max_image_bytes",
    "supported_mime_type",
    "upload_path",
]
