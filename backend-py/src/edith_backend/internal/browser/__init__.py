"""内置浏览器：后端通过 Electron 的受控 RPC 操作用户正在看的那个浏览器。

阶段 3 提供通信层（客户端 + 服务）；工具、快照与控制权在后续阶段接入。
"""

from .client import BrowserRpcClient
from .runtime import bind as bind_browser_service
from .runtime import release_control as release_browser_control
from .service import BrowserService
from .tools import BrowserToolSet
from .types import BrowserRpcError

__all__ = [
    "BrowserRpcClient",
    "BrowserRpcError",
    "BrowserService",
    "BrowserToolSet",
    "bind_browser_service",
    "release_browser_control",
]
