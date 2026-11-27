"""浏览器工具集的进程级入口。

`BrowserToolSet` 只是注册表里的一个工具集，execution 通过 `ToolRegistry` 拿到它；
但「run 收尾时交还控制权」不是一次工具调用（不能指望模型自己调），也不属于任何
单个工具。这里保存装配时创建的 service，供 AgentRun 的 finally 使用。

失败一律吞掉：控制权还有 20s 租约兜底，释放失败不该影响 run 的终态。
"""

from __future__ import annotations

import logging

from .service import BrowserService

logger = logging.getLogger(__name__)

_default: BrowserService | None = None


def bind(service: BrowserService) -> None:
    global _default
    _default = service


async def release_control(request_id: str) -> None:
    service = _default
    if service is None or not service.enabled or not request_id:
        return
    try:
        await service.release_control(request_id)
        logger.info("browser_control_released request_id=%s", request_id)
    except Exception as exc:  # noqa: BLE001 - 释放失败不能影响 run 收尾
        logger.debug(
            "browser_control_release_failed request_id=%s error=%s", request_id, exc
        )
