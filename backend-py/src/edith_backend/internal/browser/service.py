"""浏览器服务：客户端生命周期 + 「未启用」的统一表达。

阶段 3 只有只读与导航；阶段 4 的工具会在这层之上做权限、Trace 与错误归一。
把「地址/令牌从哪来」和「什么时候建连接」收在这里，工具体不用每次自己去读环境变量。
"""

from __future__ import annotations

import time

from ...config import browser_rpc_token, browser_rpc_url
from ..tools import record_tool_trace
from .client import DEFAULT_TIMEOUT, NAVIGATE_TIMEOUT, BrowserRpcClient
from .types import BrowserRpcError


def _record_rpc_ms(started: float) -> None:
    """RPC 耗时单独入 trace：工具总耗时里还掺着参数解析与权限裁决，
    不拆开就分不清「浏览器慢」还是「平台慢」。"""
    record_tool_trace(rpc_ms=round((time.monotonic() - started) * 1000, 2))


class BrowserService:
    """桌面壳注入了地址与令牌才可用。

    非桌面模式（`uv run edith-backend`）下 `enabled=False`，任何调用都抛
    `browser_disabled`——**不降级成假的浏览器**：让模型以为浏览器能用、然后
    一连串操作全部失败，比一开始就明确「这里没有浏览器」糟糕得多。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = (base_url if base_url is not None else browser_rpc_url()).strip()
        self._token = (token if token is not None else browser_rpc_token()).strip()
        self._timeout = timeout
        self._client: BrowserRpcClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._base_url and self._token)

    def require_client(self) -> BrowserRpcClient:
        if not self.enabled:
            raise BrowserRpcError(
                "browser_disabled",
                "内置浏览器不可用：当前不在 ETHOS Desktop 中运行，或浏览器服务未就绪",
            )
        if self._client is None:
            self._client = BrowserRpcClient(self._base_url, self._token, timeout=self._timeout)
        return self._client

    async def call(
        self, method: str, params: dict | None = None, *, request_id: str = ""
    ) -> dict:
        """透传一个 RPC 方法。工具层用它，避免为每个动作再加一个包装方法。"""
        started = time.monotonic()
        try:
            return await self.require_client().call(method, params, request_id=request_id)
        finally:
            _record_rpc_ms(started)

    async def health(self) -> dict:
        return await self.require_client().health()

    async def tabs(self) -> list[dict]:
        return await self.require_client().tabs()

    async def active_tab(self) -> dict:
        return await self.require_client().active_tab()

    async def navigate(self, url: str, *, request_id: str = "") -> dict:
        started = time.monotonic()
        try:
            return await self.require_client().call(
                "page.navigate",
                {"url": url},
                timeout=NAVIGATE_TIMEOUT,
                request_id=request_id,
            )
        finally:
            _record_rpc_ms(started)

    async def current_url(self) -> dict:
        started = time.monotonic()
        try:
            return await self.require_client().current_url()
        finally:
            _record_rpc_ms(started)

    async def release_control(self, request_id: str) -> None:
        """run 结束时主动交还控制权。

        取消/异常路径上若不释放，下一个 run 必须等 20s 租约过期才能操作——
        E2E 里表现为「用户点了取消，Agent 立刻重试却被判 busy」。
        失败不抛：控制权还有租约兜底。
        """
        if not request_id:
            return
        try:
            await self.call("control.release", {}, request_id=request_id)
        except BrowserRpcError:
            return
        except Exception:  # noqa: BLE001 - 释放失败不能影响 run 收尾
            return

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
