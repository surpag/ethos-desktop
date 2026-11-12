"""images 的 Agent 输入：把图片 ID 转为模型可访问的图片。

对应 Go 版 internal/images/agent_input.go。上游返回的是 COS 短期签名 URL，
由模型服务端自己去拉；桌面端的图片在本机磁盘上，远端模型拉不到 `127.0.0.1`
（那是模型机器的回环地址），所以改为内联 data URL。
"""

from __future__ import annotations

import base64
import logging

from .service import Service

logger = logging.getLogger(__name__)

REFERENCE_PREFIX = "edith-image://"

# 内联给模型的上限：base64 会把体积放大约 1.33 倍，10 MB 的原图会变成 13 MB
# 的请求体，超过多数视觉模型对单图的限制（常见 10 MB）。宁可明确跳过并留日志，
# 也不要发一个必然 400 的请求——那种失败会表现为「模型没反应」，很难归因。
MAX_INLINE_BYTES = 7 << 20


def to_data_url(data: bytes, mime_type: str) -> str:
    return f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"


class AgentInput:
    def __init__(self, service: Service) -> None:
        self._service = service

    async def message_image_urls(
        self, user_id: str, session_id: str, image_ids: list[str]
    ) -> list[str]:
        """把已确认图片转换为本次模型调用可用的图片地址。

        单张图片不可用时降级跳过（记 warning），对话继续：图片是附加信息，
        不该因为它丢一张就整轮失败。
        """
        if not image_ids:
            return []
        urls: list[str] = []
        seen: set[str] = set()
        for image_id in image_ids:
            image_id = image_id.strip()
            if not image_id:
                raise ValueError("imageID is required")
            if image_id in seen:
                raise ValueError(f'image "{image_id}" was supplied more than once')
            seen.add(image_id)
            try:
                data, mime_type = await self._service.read_for_session(
                    user_id, session_id, image_id
                )
                if len(data) > MAX_INLINE_BYTES:
                    logger.warning(
                        "image too large to inline, skipping user=%s session=%s image=%s bytes=%s",
                        user_id, session_id, image_id, len(data),
                    )
                    continue
                urls.append(to_data_url(data, mime_type))
            except Exception as exc:  # noqa: BLE001 - 图片不可用降级为无图
                logger.warning(
                    "image unavailable, skipping user=%s session=%s image=%s error=%s",
                    user_id, session_id, image_id, exc,
                )
        return urls


def reference(image_id: str) -> str:
    """返回写入会话历史的持久图片标记。"""
    return REFERENCE_PREFIX + image_id


def image_id_from_reference(value: str) -> tuple[str, bool]:
    """从持久图片标记取回图片 ID。"""
    image_id = value.removeprefix(REFERENCE_PREFIX)
    return image_id, value.startswith(REFERENCE_PREFIX) and bool(image_id)
