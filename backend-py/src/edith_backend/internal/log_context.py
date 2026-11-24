"""请求级上下文：request_id 的 ContextVar。

单独放一个模块，是为了让**下层**（隔离审计、持久化）能读到 request_id，
而不必反过来 import `main`（那会形成 `main → isolation → main` 的循环）。

只读接口是 `current_request_id()`：绑定交给 HTTP 中间件，下层一律只读。
"""

from __future__ import annotations

from contextvars import ContextVar

_request_id_var: ContextVar[str] = ContextVar("request_id", default="no-request")


def current_request_id() -> str:
    return _request_id_var.get()


def bind_request_id(value: str):
    """中间件进入时绑定，退出时用返回的 token 复原。"""
    return _request_id_var.set(value)


def reset_request_id(token) -> None:
    _request_id_var.reset(token)
