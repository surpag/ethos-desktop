"""images 模块的组装入口。

对应 Go 版 internal/images/module.go。
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from .agent_input import AgentInput
from .http import HTTP
from .service import Service
from .storage import LocalStorage
from .store import Store, create_schema


class Module:
    """图片模块对外提供的能力集合。"""

    def __init__(self, agent_input: AgentInput, http: HTTP) -> None:
        self.agent_input = agent_input
        self.http = http


async def new_module(db: aiosqlite.Connection, images_root: Path) -> Module:
    """创建图片模块。

    图片字节落在 `images_root`（调用方给 `data_root()/images`），与数据库同在
    用户数据目录下：备份/迁移时「库」和「图」是一个整体，不会一半走丢。
    """
    if db is None:
        raise ValueError("image database is required")
    await create_schema(db)
    store = Store(db)
    service = Service(store, LocalStorage(images_root))
    return Module(AgentInput(service), HTTP(service))
