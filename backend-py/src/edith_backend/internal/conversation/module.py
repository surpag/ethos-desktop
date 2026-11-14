"""conversation 模块的组装入口。

对应 Go 版 internal/conversation/module.go。
"""

from __future__ import annotations

from ..session.plan_store import RunPlanStore
from ..session.store import SessionStore
from ..usage.reader import Reader
from .history import History
from .http import HTTP


class Module:
    """会话模块对外提供的能力集合。"""

    def __init__(self, http: HTTP) -> None:
        self.http = http


def new_module(
    store: SessionStore,
    usage: Reader,
    plans: RunPlanStore | None = None,
) -> Module:
    """创建会话模块；会话存储和用量读取器都由调用方持有。

    这里没有档位参数：档位是用户级设置，会话既不持有也不展示它。界面要知道
    「现在实际按几档跑」时，答案与执行时同一个来源（用户设置），不需要在会话上
    再存一份投影——投影迟早与真相不一致。
    """
    if store is None:
        raise ValueError("conversation session store is required")
    if usage is None:
        raise ValueError("conversation usage reader is required")
    history = History(store, usage, plans)
    return Module(HTTP(history))
