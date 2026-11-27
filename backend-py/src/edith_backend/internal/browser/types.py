"""浏览器 RPC 的协议常量与错误类型。

错误码与 `desktop/src/main/browser/browser-rpc-server.ts` 一一对应。**分类比文案重要**：
调用方要靠 `retryable` 决定「换个参数重试」「等一下再试」还是「直接告诉用户不行」，
把这三件事混成一个 `Exception` 会让上层只能笼统地失败。
"""

from __future__ import annotations

#: 可以重试的错误：对端暂时不可用、限流、超时、浏览器还没就绪。
#: 注意 `no_active_tab` 也在这里——用户可能刚关掉标签页，重新观察一次就有页面了。
RETRYABLE_CODES = frozenset(
    {
        "timeout",
        "rate_limited",
        "internal_error",
        "unavailable",
        "browser_not_ready",
        "server_closing",
        "no_active_tab",
        "invalid_response",
    }
)

#: 不可重试：令牌不对、方法不在白名单、参数非法、请求体过大。
#: 这些是**调用方**的问题，原样重试只会得到同样的结果。
FATAL_CODES = frozenset(
    {
        "unauthorized",
        "method_not_allowed",
        "invalid_arguments",
        "invalid_json",
        "request_too_large",
        "not_found",
        "browser_disabled",
    }
)


class BrowserRpcError(Exception):
    """浏览器 RPC 的统一异常。

    `code` 是协议里的字符串（不是 HTTP 状态码），因为同一个 400 可能对应
    「参数非法」和「方法不允许」，上层需要区分。
    """

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    @property
    def retryable(self) -> bool:
        """未知错误码一律**不可重试**：宁可让模型停下来问，也不要盲目重试。"""
        return self.code in RETRYABLE_CODES

    def __repr__(self) -> str:  # pragma: no cover - 仅用于日志可读性
        return f"BrowserRpcError(code={self.code!r}, retryable={self.retryable})"
