"""隔离后端实现。

目前只有 ASRT（CLI 方式）。AppContainer 后端在下一阶段补上，接口保持一致：
拿到 `SandboxPolicy`，产出 argv 与 env。
"""

from . import asrt

__all__ = ["asrt"]
